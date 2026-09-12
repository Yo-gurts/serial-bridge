"""串口网络网关 —— FastAPI + WebSocket。

单进程独占一个串口,同时:
  - 托管一个浏览器 Web UI(本地查看收发、手动发命令)
  - 提供 WebSocket 接口(本地 UI 与远程服务器共用),实时收发 + 控制串口

启动:
    python server.py --host 0.0.0.0 --port 8000 --token mysecret
不指定 --token 时视为免密,任何人都能连接。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from serial_manager import SerialManager

STATIC_DIR = Path(__file__).parent / "static"


def to_hex(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


def to_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def make_frame(kind: str, data: bytes) -> dict:
    """把一段字节封装成 rx/tx 消息(同时带 hex 与 text)。"""
    return {"type": kind, "ts": time.time(), "hex": to_hex(data), "text": to_text(data)}


class App:
    def __init__(self, token: Optional[str]) -> None:
        # token 为 None/空 表示免密
        self.token = token or None
        self.clients: Set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # 串口回调在后台线程触发,需转发到事件循环再广播
        self.serial = SerialManager(on_data=self._on_serial_data, on_status=self._on_serial_status)

    # ---- 串口回调(后台线程上下文)----
    def _on_serial_data(self, data: bytes) -> None:
        self._dispatch(make_frame("rx", data))

    def _on_serial_status(self, status: dict) -> None:
        self._dispatch(status)

    def _dispatch(self, msg: dict) -> None:
        """线程安全地把消息投递到事件循环并广播。"""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(msg)))

    # ---- 广播 ----
    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ---- 处理单条客户端消息 ----
    async def handle_client_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "tx":
            encoding = msg.get("encoding", "utf8")
            raw = msg.get("data", "")
            try:
                if encoding == "hex":
                    cleaned = raw.replace(" ", "").replace("\n", "").replace("\r", "")
                    data = bytes.fromhex(cleaned)
                else:
                    data = raw.encode("utf-8")
            except ValueError as e:
                await self.broadcast({"type": "status", "open": self.serial.is_open,
                                      "port": None, "baudrate": None, "error": f"数据解析失败: {e}"})
                return
            try:
                self.serial.write(data)
            except Exception as e:
                await self.broadcast({"type": "status", "open": self.serial.is_open,
                                      "port": None, "baudrate": None, "error": str(e)})
                return
            # 回显给所有客户端
            await self.broadcast(make_frame("tx", data))

        elif mtype == "open":
            port = msg.get("port")
            if not port:
                return
            self.serial.open(
                port=port,
                baudrate=int(msg.get("baudrate", 115200)),
                bytesize=int(msg.get("bytesize", 8)),
                parity=msg.get("parity", "N"),
                stopbits=float(msg.get("stopbits", 1)),
            )

        elif mtype == "close":
            self.serial.close()


def create_app(state: App) -> FastAPI:
    api = FastAPI()

    @api.on_event("startup")
    async def _startup() -> None:
        state.loop = asyncio.get_running_loop()

    @api.get("/")
    async def index() -> FileResponse:
        # 禁止缓存,避免改了前端后浏览器仍显示旧页面
        headers = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
        return FileResponse(STATIC_DIR / "index.html", headers=headers)

    @api.get("/api/config")
    async def config() -> JSONResponse:
        # 前端据此决定是否需要输入 token
        return JSONResponse({"auth_required": state.token is not None})

    @api.get("/api/ports")
    async def ports() -> JSONResponse:
        return JSONResponse(SerialManager.list_ports())

    @api.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # 设了 token 才校验;不匹配用 4401 关闭(前端据此提示密码错误)
        if state.token is not None and ws.query_params.get("token") != state.token:
            await ws.close(code=4401)
            return
        await ws.accept()
        state.clients.add(ws)
        # 进连接先推一条当前状态
        try:
            await ws.send_json(state.serial.status())
            while True:
                msg = await ws.receive_json()
                await state.handle_client_message(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            state.clients.discard(ws)

    return api


def main() -> None:
    parser = argparse.ArgumentParser(description="串口网络网关")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(远程访问用 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP/WebSocket 端口")
    parser.add_argument("--token", default=None, help="访问密钥;不填则免密,任何人可连接")
    args = parser.parse_args()

    state = App(token=args.token)
    api = create_app(state)

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    suffix = f"?token={args.token}" if state.token else ""
    print("=" * 56)
    print("  串口网络网关已启动")
    print(f"  本地界面 : http://{shown_host}:{args.port}/{suffix}")
    print(f"  WebSocket: ws://{shown_host}:{args.port}/ws{suffix}")
    if state.token:
        print(f"  Token    : {args.token}")
    else:
        print("  Token    : (未设置,免密访问 —— 如需限制请加 --token)")
    if args.host in ("0.0.0.0", "::"):
        print("  (已监听所有网卡,远程可用本机局域网 IP 替换上面的地址)")
    print("=" * 56)

    uvicorn.run(api, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
