"""声明 M1 状态仓储的能力端口与安全错误类型。"""

from __future__ import annotations

from typing import Protocol

from app.domain.models import Evidence, Lease, Owner, ReadResult

STORE_REASON_CODES = frozenset(
    {
        "invalid_input",
        "lease_busy",
        "stale_lease",
        "owner_not_stopped",
        "clock_changed",
        "store_busy",
        "store_io_error",
        "db_version_unsupported",
        "migration_failed",
    }
)


class StoreError(Exception):
    """携带固定仓储原因码的失败；调用方不得输出 SQL 参数或原始异常文本。"""

    def __init__(self, reason_code: str) -> None:
        """保存白名单原因码，异常消息只含该码。"""
        if reason_code not in STORE_REASON_CODES:
            raise ValueError("store reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


class StorePort(Protocol):
    """抽象 M1 的候选入库、任务租约、恢复与未关闭会话查询。"""

    def upsert_snapshot(self, result: ReadResult) -> list[str]:
        """保存完整未读快照，并返回写后仍 queued 的发现键（升序）。"""
        ...

    def claim(self, app: str, kind: str, owner: Owner, now: str) -> Lease | None:
        """原子领取一条 queued 发现并持有全局 primary 设备租约。"""
        ...

    def start(self, lease: Lease, now: str) -> Lease:
        """把已领取任务从 leased 提交为 running，不延长截止时间。"""
        ...

    def renew(self, lease: Lease, now: str) -> Lease:
        """同时延长任务与设备租约，并返回新截止时间的 Lease。"""
        ...

    def finish(
        self,
        lease: Lease,
        now: str,
        state: str,
        reason_code: str | None,
        proof: ReadResult | Evidence,
    ) -> str:
        """按读取或动作证据收敛 running 任务，并处理设备行。"""
        ...

    def recover(self, lease: Lease, confirm_stopped: bool, now: str) -> dict[str, str]:
        """在确认原持有进程已停止后恢复过期租约。"""
        ...

    def unresolved_conversations(self, app: str) -> list[str]:
        """只读返回未关闭发现涉及的会话键（升序去重）。"""
        ...
