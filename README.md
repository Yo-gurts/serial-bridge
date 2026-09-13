# 串口网络网关 (Serial Bridge)

在 Windows 本地跑一个网关进程,它独占本机串口,同时:

- 托管一个浏览器 **Web UI**,本地实时查看收发内容、手动发命令;
- 提供 **WebSocket** 接口,让**远程服务器**实时收到串口数据、也能发命令。

**单进程、单端口,可同时打开多个串口**,用 URL 路径区分。每个 COM 口是独占的(同一时刻只能被一个进程打开),但不同口互相独立、可并存;同一个口的多个客户端(浏览器多标签 + 远程)共享一条串口连接。

```
  [设备A] ─COM9─┐
  [设备B] ─COM7─┤   [网关进程 (本机, 单端口)]
                ├── http://本机:8000/         选择串口(启动页)
                ├── http://本机:8000/COM9     直达 COM9 终端(打开即自动连接)
                ├── ws://本机:8000/ws/COM9    COM9 的实时收发(连上自动开口)
                └── ws://本机:8000/ws         旧接口:靠 {type:open,port} 绑定(rcmd 用)
```

**多串口用法**:浏览器打开 `http://本机:8000/` 从下拉框选口,会跳到该口的专属页
`http://本机:8000/COM9`——这个地址可直接收藏,以后打开就自动连上 COM9,不用每次点「打开」。
要同时看多个口,就在不同标签页打开 `/COM9`、`/COM7` 即可。波特率可在页面上改(记住到该口)。

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
