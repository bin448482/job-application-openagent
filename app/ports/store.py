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
        "action_conflict",
        "action_not_found",
        "action_not_ready",
        "quota_exceeded",
        "cooldown_active",
        "text_send_blocked",
        "invalid_action_key",
        "invalid_observation",
        "scene_unsettled",
        "recovery_not_confirmed",
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

    def current_lease_expiry(self, lease: Lease, *, now: str) -> str | None:
        """只读重核任务/设备租约与取得时 Lease/Owner/nonce/到期。"""
        ...

    def begin_action(
        self,
        lease: Lease,
        *,
        action_kind: str,
        action_key: str,
        target_key: str,
        conversation_key: str,
        authorization_json: str,
        daily_sends: int,
        reply_cooldown_seconds: int,
        ime_original: str | None,
        now: str,
    ) -> dict[str, object]:
        """原子校验准入、文本额度冷却并写入 pending 与唯一动作键锁。"""
        ...

    def complete_action(
        self,
        lease: Lease,
        *,
        action_key: str,
        outcome: object,
        now: str,
    ) -> dict[str, object]:
        """在同一事务提交动作证据、任务终态、设备行与审计事件。"""
        ...

    def freeze_attempted_action(
        self,
        lease: Lease,
        *,
        action_key: str,
        reason_code: str,
        now: str,
    ) -> dict[str, object]:
        """执行后条件失效时按原身份冻结 unknown/blocked 并保留隔离。"""
        ...

    def reconcile_action(
        self,
        *,
        action_key: str,
        resolution: str,
        evidence: Evidence | None,
        command_quiet: bool,
        scene_settled: bool,
        confirmation_open: bool,
        ime_restored: bool,
        explicit_confirm: bool,
        command_stopped: bool,
        now: str,
    ) -> dict[str, object]:
        """经本轮确认与现场收敛后收敛 pending/unknown 动作。"""
        ...

    def get_action(self, action_key: str) -> dict[str, object] | None:
        """只读返回一条动作记录；不存在返回 None。"""
        ...

    def pending_actions(self, app: str = "boss") -> list[dict[str, object]]:
        """只读返回 pending/unknown/cancelled 动作记录。"""
        ...

    def text_admission_status(
        self,
        *,
        now: str,
        daily_sends: int,
        reply_cooldown_seconds: int,
        conversation_key: str,
    ) -> dict[str, object]:
        """只读计算文本日额度、全局 pending 与按会话冷却资格。"""
        ...

    def load_ime_state(self) -> dict[str, object] | None:
        """只读返回切换前持久化的原 IME 状态；不存在返回 None。"""
        ...

    def save_ime_state(self, action_key: str, original_ime: str, *, now: str) -> None:
        """在切换 IME 前持久化原值，只允许当前 pending 动作写入。"""
        ...

    def task_conversation(self, lease: Lease, *, now: str) -> str | None:
        """只读返回 running 任务的会话键；租约不符返回 None。"""
        ...

    def require_running(self, lease: Lease, *, now: str) -> bool:
        """只读确认任务已 start 为 running 且任务/设备租约完整有效。"""
        ...
