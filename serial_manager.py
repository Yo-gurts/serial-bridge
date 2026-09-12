"""串口封装:枚举、打开/关闭、写入、后台读线程,以及数据/状态回调。

这一层不依赖 asyncio,保持纯粹的串口逻辑。server 层通过注入的回调
(on_data / on_status)把数据桥接到事件循环并广播给 WebSocket 客户端。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

import serial
from serial.tools import list_ports


# 回调类型
OnData = Callable[[bytes], None]
OnStatus = Callable[[dict], None]


class SerialManager:
    """独占一个串口,后台线程持续读取并通过回调分发。"""

    def __init__(self, on_data: OnData, on_status: OnStatus) -> None:
        self._on_data = on_data
        self._on_status = on_status

        self._ser: Optional[serial.Serial] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # 保护 self._ser 的打开/关闭/写入,避免多个 WS 客户端并发操作时打架
        self._lock = threading.Lock()

    # ---- 端口枚举 ----
    @staticmethod
    def list_ports() -> List[dict]:
        ports = []
        for p in list_ports.comports():
            ports.append({"device": p.device, "description": p.description or ""})
        return ports

    # ---- 状态 ----
    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def status(self) -> dict:
        if self.is_open:
            assert self._ser is not None
            return {
                "type": "status",
                "open": True,
                "port": self._ser.port,
                "baudrate": self._ser.baudrate,
                "error": None,
            }
        return {"type": "status", "open": False, "port": None, "baudrate": None, "error": None}

    def _emit_status(self, error: Optional[str] = None) -> None:
        st = self.status()
        if error is not None:
            st["error"] = error
        self._on_status(st)

    # ---- 打开 / 关闭 ----
    def open(
        self,
        port: str,
        baudrate: int = 115200,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
    ) -> None:
        with self._lock:
            # 先关闭已有连接(切换端口时)
            self._close_locked()
            try:
                self._ser = serial.Serial(
                    port=port,
                    baudrate=baudrate,
                    bytesize=bytesize,
                    parity=parity,
                    stopbits=stopbits,
                    timeout=0.1,  # 读超时,让读线程能周期性检查 stop 标志
                )
            except Exception as e:  # 打开失败:清理并上报错误状态
                self._ser = None
                self._emit_status(error=str(e))
                return

            self._stop.clear()
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
        self._emit_status()

    def close(self) -> None:
        with self._lock:
            self._close_locked()
        self._emit_status()

    def _close_locked(self) -> None:
        """在持有 _lock 的前提下关闭串口并停掉读线程。"""
        self._stop.set()
        reader = self._reader
        self._reader = None
        ser = self._ser
        self._ser = None

        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        # 等读线程退出(它最多阻塞一个 read timeout)。不在锁外 join 以简化,
        # 但读线程内部不再获取 _lock,避免死锁。
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)

    # ---- 写入 ----
    def write(self, data: bytes) -> None:
        with self._lock:
            if not self.is_open:
                raise RuntimeError("串口未打开")
            assert self._ser is not None
            self._ser.write(data)

    # ---- 后台读循环 ----
    def _read_loop(self) -> None:
        ser = self._ser
        while not self._stop.is_set() and ser is not None:
            try:
                # 有多少读多少;in_waiting 为 0 时 read(1) 会阻塞到 timeout
                n = ser.in_waiting
                data = ser.read(n if n > 0 else 1)
                if data:
                    self._on_data(data)
            except Exception as e:
                # 设备拔出等异常:上报并结束读循环
                if not self._stop.is_set():
                    self._on_status(
                        {"type": "status", "open": False, "port": None, "baudrate": None, "error": str(e)}
                    )
                break
            # 轻微让步,避免 in_waiting 为 0 时空转(read timeout 已提供大部分让步)
            time.sleep(0)
