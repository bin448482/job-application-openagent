"""声明固定 OS 位置共享 SQLite 设备门禁的会话端口与安全错误。"""

from __future__ import annotations

from typing import Protocol

from app.domain.models import Owner

DEVICE_GATE_REASON_CODES = frozenset(
    {
        "device_gate_unavailable",
        "device_gate_io_error",
        "device_busy",
        "device_not_held",
        "device_gate_mismatch",
        "device_quarantined",
    }
)


class DeviceGateError(Exception):
    """携带固定门禁原因码；不携带设备序列号、路径、PID 或原始异常文本。"""

    def __init__(self, reason_code: str) -> None:
        """校验并保存白名单原因码，异常文本只含该码。"""
        if reason_code not in DEVICE_GATE_REASON_CODES:
            raise ValueError("device gate reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


class SharedDeviceGatePort(Protocol):
    """抽象整个真实会话共享唯一 primary 设备占用的取得/复核/释放。"""

    def acquire(
        self, *, profile_id: str, profile_db_path: str, owner: Owner, now: str
    ) -> str:
        """在共享库 BEGIN IMMEDIATE 条件插入唯一 primary 行，返回本次 nonce。"""
        ...

    def verify(self, *, now: str | None = None) -> None:
        """复核共享身份、profile 绑定与 held 状态；失败停止新动作。"""
        ...

    def release(self, *, now: str, converged: bool) -> None:
        """仅在本次 owner/nonce 且已收敛时删除行，否则保留并隔离。"""
        ...

    def inspect(self) -> dict[str, object] | None:
        """只读返回固定白名单占用行；无行返回 None，不自动释放。"""
        ...
