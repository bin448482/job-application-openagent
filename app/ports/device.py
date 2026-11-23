"""定义 M0 只读设备探针的领域快照与能力端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeviceEntry:
    """表示清单中的一个设备逻辑别名及其归一化状态。"""

    alias: str
    state: str


@dataclass(frozen=True)
class ProbeSnapshot:
    """保存一次探针得到的清单、选择结果以及解锁与前台字段。"""

    devices: tuple[DeviceEntry, ...]
    selected_alias: str | None
    unlocked: bool | None
    foreground_app: str


class DeviceProbe(Protocol):
    """抽象一次只读设备探针；实现不得把真实序列号或窗口原文交给输出层。"""

    def probe(self, serial: str | None = None) -> ProbeSnapshot:
        """执行一次探针并返回已归一化的设备快照。"""
        ...
