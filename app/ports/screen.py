"""声明 M3 层级/截图采集端口、共享设备门禁与安全错误。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

SCREEN_ERROR_REASONS = frozenset({"device_disconnected", "invalid_fixture"})


@dataclass(frozen=True)
class HierarchyCapture:
    """保存一次层级 dump 的内存文本与受控本机证据标识。"""

    xml_text: str
    evidence_name: str
    byte_size: int


@dataclass(frozen=True)
class ScreenCapture:
    """保存一次原始 RGBA 截图的内存字节与受控本机证据标识。"""

    raw: bytes
    evidence_name: str
    byte_size: int


class ScreenReadError(Exception):
    """携带固定安全原因码的采集失败，不输出 serial、路径或原始异常。"""

    def __init__(self, reason_code: str) -> None:
        """校验并保存白名单原因码。"""
        if reason_code not in SCREEN_ERROR_REASONS:
            raise ValueError("screen reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


class DeviceGateUnavailable(Exception):
    """表示共享 OS 设备门禁未交付或未取得占用。"""

    def __init__(self) -> None:
        """使用固定原因码，不携带设备或路径信息。"""
        super().__init__("device_gate_unavailable")
        self.reason_code = "device_gate_unavailable"


class ScreenPort(Protocol):
    """抽象一次显式层级与截图采集；实现不得把 serial 或原文交给输出层。"""

    def dump_hierarchy(self, serial: str) -> HierarchyCapture:
        """执行一次层级 dump/cat 并保存本机证据。"""
        ...

    def capture_screen(self, serial: str) -> ScreenCapture:
        """执行一次原始截图并保存本机证据。"""
        ...


class DeviceGate(Protocol):
    """抽象跨 profile 的共享设备门禁；未取得占用时真实采集必须拒绝。"""

    def require_available(self) -> None:
        """要求本会话已持共享占用并通过一次完整复核。"""
        ...

    def verify(self) -> None:
        """每条设备子命令前复核共享身份与 profile 绑定。"""
        ...

    def release(self) -> None:
        """结束真实 run 后按收敛状态释放或保留共享占用。"""
        ...
