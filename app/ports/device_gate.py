"""声明固定 OS 位置共享 SQLite 设备门禁的会话端口与安全错误。"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class DeviceSessionIdentity:
    """表示共享门禁当前 held 会话的固定白名单身份。"""

    profile_id: str
    profile_db_path: str
    owner: Owner
    nonce: str
    state: str


class DeviceWriteGuard(Protocol):
    """覆盖事实写全过程的固定门禁排他事务；不插入或改写占用行。"""

    def __enter__(self) -> "DeviceWriteGuard":
        """取得门禁写事务并复核同 profile 占用后返回自身。"""
        ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        """结束门禁写事务并释放 SQLite 锁；不吞掉 body 异常。"""
        ...


class SharedDeviceGatePort(Protocol):
    """抽象整个真实会话共享唯一 primary 设备占用的取得/复核/释放。"""

    def acquire(
        self, *, profile_id: str, profile_db_path: str, owner: Owner, now: str
    ) -> str:
        """在共享库 BEGIN IMMEDIATE 条件插入唯一 primary 行，返回本次 nonce。"""
        ...

    def write_guard(self, *, create: bool = True) -> DeviceWriteGuard:
        """返回覆盖事实写的固定门禁排他事务守卫；不改变占用行。"""
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

    def session_identity(self) -> DeviceSessionIdentity:
        """复核本会话并返回 profile/原库/owner/nonce 的完整可信身份。"""
        ...

    def recover(
        self,
        *,
        profile_id: str,
        profile_db_path: str,
        owner: Owner,
        confirm_stopped: bool,
        stop_checker: object,
        now: str,
    ) -> str:
        """受控接管原 profile 的 held/quarantined 占用并返回新 nonce。"""
        ...
