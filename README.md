# 串口网络网关 (Serial Bridge)

在 Windows 本地跑一个网关进程,它**独占串口**,同时:

- 托管一个浏览器 **Web UI**,本地实时查看收发内容、手动发命令;
- 提供 **WebSocket** 接口,让**远程服务器**实时收到串口数据、也能发命令。

Windows 的 COM 口是独占的(同一时刻只能被一个进程打开),所以本工具用单进程独占串口,本地界面和所有远程客户端都连到这一个进程,共享同一套 WebSocket 协议。

```
  [设备] ──串口/COM──> [网关进程 (本机)]
                          ├── http://本机:8000/        浏览器 Web UI
                          └── ws://本机:8000/ws         远程服务器 / 本地 UI 共用
```

## 安装

```bash
pip install -r requirements.txt
```

## 启动

仅本机使用:

```bash
python server.py
```

允许远程服务器访问(监听所有网卡):

```bash
python server.py --host 0.0.0.0 --port 8000 --token mysecret
```

启动参数:

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址;远程访问需设为 `0.0.0.0` |
| `--port` | `8000` | HTTP / WebSocket 端口 |
| `--token` | 随机生成 | 访问密钥;不指定则启动时随机生成并打印 |

启动后终端会打印带 token 的访问地址。浏览器打开
`http://127.0.0.1:8000/?token=<你的token>`,选择 COM 口和波特率,点「打开」即可收发。

> **注意:直接在 Windows 上运行**(用 Windows 的 Python),不要在 WSL2 里跑。WSL2 是独立 VM,无法打开宿主机的 COM 口(蓝牙串口尤其如此)。

## Web UI

- 顶部:选端口、波特率,打开/关闭串口;右侧显示服务连接与串口状态。
- 中部:收发日志,`RX ←` 绿色 / `TX →` 蓝色,带毫秒时间戳;可切「HEX 视图」、自动滚屏、清屏。
- 底部:输入框回车即发;勾「HEX」按十六进制发送;「结尾」选择追加的行结尾(无/CR/LF/CRLF,HEX 模式下不追加)。

## 远程访问

远程服务器连到 `ws://<本机IP>:8000/ws?token=<token>`,消息为 JSON。

**服务端 → 客户端**

```jsonc
{ "type": "rx",     "ts": 1699999999.1, "hex": "48 65 6c 6c 6f", "text": "Hello" }  // 串口收到
{ "type": "tx",     "ts": ...,          "hex": "...", "text": "..." }               // 有人发出(回显)
{ "type": "status", "open": true, "port": "COM3", "baudrate": 115200, "error": null }
```

**客户端 → 服务端**

```jsonc
{ "type": "tx",   "data": "AT\r\n", "encoding": "utf8" }   // encoding: "utf8" | "hex"
{ "type": "open", "port": "COM3", "baudrate": 115200 }
{ "type": "close" }
```

命令行示例(需要 [websocat](https://github.com/vi/websocat)):

```bash
# 实时接收串口数据
websocat "ws://192.168.1.10:8000/ws?token=mysecret"

# 发送命令(另开一个连接)
echo '{"type":"tx","data":"AT\r\n","encoding":"utf8"}' | websocat "ws://192.168.1.10:8000/ws?token=mysecret"
```

Python 客户端示例:

```python
import asyncio, json, websockets

async def main():
    url = "ws://192.168.1.10:8000/ws?token=mysecret"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "tx", "data": "AT\r\n", "encoding": "utf8"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "rx":
                print("RX:", msg["text"])

asyncio.run(main())
```

## 无真实设备时的测试

用虚拟串口对(一端给网关连,另一端模拟设备):

- Windows:安装 [com0com](https://sourceforge.net/projects/com0com/),建一对如 `COM10 ↔ COM11`。
- Linux/macOS:`socat -d -d pty,raw,echo=0 pty,raw,echo=0`,得到两个 `/dev/pts/N`。

网关打开其中一端,另一端用串口助手或脚本收发,即可在 Web UI 上看到往来数据。

## 打包成单 exe(可选)

```bash
pip install pyinstaller
pyinstaller --onefile --add-data "static;static" server.py
```
