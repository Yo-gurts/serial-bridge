"""串口网络网关 —— FastAPI + WebSocket(多串口)。

单进程、单 HTTP 端口,可同时打开多个串口;用 URL 路径区分:
  - 浏览器 http://host:port/COM9    → 绑定并自动打开 COM9 的终端页
  - WebSocket ws://host:port/ws/COM9 → 该串口的实时收发(连上即自动开口)
  - 旧接口 ws://host:port/ws          → 不带端口,靠 {type:open,port} 绑定
    (rcmd 的 serial_bridge/prompt_bridge 仍走这个,协议不变)

不同 COM 口互相独立,可并存;同一个口的多个客户端共享一个串口连接。

启动:
    python server.py --host 0.0.0.0 --port 8000 --token mysecret
不指定 --token 时视为免密,任何人都能连接。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Dict, Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from serial_manager import SerialManager

STATIC_DIR = Path(__file__).parent / "static"
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


def to_hex(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


def to_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def make_frame(kind: str, data: bytes) -> dict:
    """把一段字节封装成 rx/tx 消息(同时带 hex 与 text)。"""
    return {"type": kind, "ts": time.time(), "hex": to_hex(data), "text": to_text(data)}


class PortEntry:
    """一个串口的运行态:独占该串口的 SerialManager + 订阅它的客户端集合。"""

    def __init__(self, gateway: "Gateway", name: str) -> None:
        self.name = name
        self.clients: Set[WebSocket] = set()
        # on_data/on_status 在后台读线程触发,闭包捕获端口名以便按口路由
        self.manager = SerialManager(
            on_data=lambda data: gateway._dispatch(name, make_frame("rx", data)),
            on_status=lambda status: gateway._dispatch(name, status),
        )


class Gateway:
    def __init__(self, token: Optional[str]) -> None:
        self.token = token or None  # None/空 表示免密
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ports: Dict[str, PortEntry] = {}

    def get_port(self, name: str) -> PortEntry:
        if name not in self.ports:
            self.ports[name] = PortEntry(self, name)
        return self.ports[name]

    # ---- 后台线程 -> 事件循环 -> 按口广播 ----
    def _dispatch(self, name: str, msg: dict) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(name, msg)))

    async def broadcast(self, name: str, msg: dict) -> None:
        entry = self.ports.get(name)
        if not entry:
            return
        dead = []
        for ws in list(entry.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            entry.clients.discard(ws)


async def _ws_serve(gateway: Gateway, ws: WebSocket, path_port: Optional[str]) -> None:
    """处理一个 WebSocket 连接。

    path_port 非空(来自 /ws/<port>):连上即自动打开该口并绑定本连接;
    path_port 为空(旧 /ws):等 {type:open,port} 再绑定(rcmd 走这里)。
    """
    if gateway.token is not None and ws.query_params.get("token") != gateway.token:
        await ws.close(code=4401)
        return
    await ws.accept()
    default_baud = int(ws.query_params.get("baud", "115200"))
    entry: Optional[PortEntry] = None
    try:
        if path_port:
            entry = gateway.get_port(path_port)
            entry.clients.add(ws)
            if not entry.manager.is_open:               # 自动开口(已开则复用,不打断)
                entry.manager.open(port=path_port, baudrate=default_baud)
            await ws.send_json(entry.manager.status())

        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "open":
                name = msg.get("port") or (entry.name if entry else None)
                if not name:
                    continue
                if entry is not None and entry.name != name:  # 换口:退订旧口
                    entry.clients.discard(ws)
                entry = gateway.get_port(name)
                entry.clients.add(ws)
                baudrate = int(msg.get("baudrate", default_baud))
                # 已开且波特率一致就不重复开 —— 避免与共享同口的其它客户端
                # (如浏览器 + rcmd)互相打断、触发设备 DTR 复位
                cur = entry.manager.status()
                if not (cur.get("open") and int(cur.get("baudrate") or 0) == baudrate):
                    entry.manager.open(
                        port=name,
                        baudrate=baudrate,
                        bytesize=int(msg.get("bytesize", 8)),
                        parity=msg.get("parity", "N"),
                        stopbits=float(msg.get("stopbits", 1)),
                    )
                else:
                    await ws.send_json(entry.manager.status())  # 已开:直接回状态
                continue

            if entry is None:
                continue  # 尚未绑定端口,忽略其它消息

            if mtype == "tx":
                encoding = msg.get("encoding", "utf8")
                raw = msg.get("data", "")
                try:
                    if encoding == "hex":
                        data = bytes.fromhex(raw.replace(" ", "").replace("\n", "").replace("\r", ""))
                    else:
                        data = raw.encode("utf-8")
                except ValueError as e:
                    await gateway.broadcast(entry.name, {"type": "status", "open": entry.manager.is_open,
                                                         "port": entry.name, "baudrate": None, "error": f"数据解析失败: {e}"})
                    continue
                try:
                    entry.manager.write(data)
                except Exception as e:
                    await gateway.broadcast(entry.name, {"type": "status", "open": entry.manager.is_open,
                                                         "port": entry.name, "baudrate": None, "error": str(e)})
                    continue
                await gateway.broadcast(entry.name, make_frame("tx", data))

            elif mtype == "close":
                entry.manager.close()

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if entry is not None:
            entry.clients.discard(ws)


def create_app(gateway: Gateway) -> FastAPI:
    api = FastAPI()

    @api.on_event("startup")
    async def _startup() -> None:
        gateway.loop = asyncio.get_running_loop()

    def _index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

    @api.get("/")
    async def index() -> FileResponse:
        return _index()

    @api.get("/api/config")
    async def config() -> JSONResponse:
        return JSONResponse({"auth_required": gateway.token is not None})

    @api.get("/api/ports")
    async def ports() -> JSONResponse:
        return JSONResponse(SerialManager.list_ports())

    api.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @api.websocket("/ws")
    async def ws_root(ws: WebSocket) -> None:
        await _ws_serve(gateway, ws, None)

    @api.websocket("/ws/{port:path}")
    async def ws_port(ws: WebSocket, port: str) -> None:
        await _ws_serve(gateway, ws, port)

    # 兜底:/<port> 之类的单/多段路径都返回 UI(前端从 location.pathname 取端口)。
    # 放最后,exact 的 /api、/static、/ws 已在前面优先匹配。
    @api.get("/{port:path}")
    async def index_bound(port: str) -> FileResponse:
        return _index()

    return api


def main() -> None:
    parser = argparse.ArgumentParser(description="串口网络网关(多串口)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(远程访问用 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP/WebSocket 端口")
    parser.add_argument("--token", default=None, help="访问密钥;不填则免密,任何人可连接")
    args = parser.parse_args()

    gateway = Gateway(token=args.token)
    api = create_app(gateway)

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    suffix = f"?token={args.token}" if gateway.token else ""
    print("=" * 60)
    print("  串口网络网关(多串口)已启动")
    print(f"  选择串口 : http://{shown_host}:{args.port}/{suffix}")
    print(f"  直达某口 : http://{shown_host}:{args.port}/COM9{suffix}  (示例,自动打开)")
    print(f"  WebSocket: ws://{shown_host}:{args.port}/ws/<PORT>{suffix}")
    if gateway.token:
        print(f"  Token    : {args.token}")
    else:
        print("  Token    : (未设置,免密访问 —— 如需限制请加 --token)")
    if args.host in ("0.0.0.0", "::"):
        print("  (已监听所有网卡,远程可用本机局域网 IP 替换上面的地址)")
    print("=" * 60)

    uvicorn.run(api, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
