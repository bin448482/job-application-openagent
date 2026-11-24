"""声明 M6 实际运行需要的场景、决策刺激与批次持久化能力。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.models import Lease, Owner, ReadResult
from app.domain.runtime import (
    BatchCursor,
    BudgetLimits,
    SafePoint,
    SceneCandidates,
    TaskCandidate,
)


@dataclass(frozen=True)
class DecisionStimulus:
    """表示一次已校验的模型决策刺激；不是断言答案或授权。"""

    intent: str
    sub_intents: tuple[str, ...]
    policy_tier: str
    reply: str
    evidence_hash: str


@dataclass(frozen=True)
class SceneStep:
    """表示安全点处的一次可见场景与本次任务结果刺激。"""

    wall_now: str
    monotonic_ms: int
    elapsed_seconds: int
    device_calls: int
    candidates: SceneCandidates
    safe_point: SafePoint
    task_outcome: str
    reason_code: str | None
    unread_verification: str
    decision: DecisionStimulus | None
    read_result: ReadResult | None
    in_progress: bool = False


@dataclass(frozen=True)
class DraftRecord:
    """表示可重审的本机草稿，不携带授权或执行租约。"""

    draft_id: str
    task_key: str
    subject_key: str
    input_hash: str
    facts_hash: str
    intent: str
    sub_intents: tuple[str, ...]
    policy_tier: str
    reply: str


class RuntimeScenePort(Protocol):
    """按实际调度提供安全点场景、一次定向核对与任务结果。"""

    def next_step(self) -> SceneStep | None:
        """返回下一次刷新；没有更多刺激时返回 None。"""
        ...


class RuntimeDecisionPort(Protocol):
    """把当前输入身份交给唯一决策权威；只返回已校验摘要/回复。"""

    def decide(self, *, task: TaskCandidate, input_hash: str, facts_hash: str) -> DecisionStimulus:
        """生成一次决策刺激；不得返回授权、nonce 或租约。"""
        ...


class RuntimeStorePort(Protocol):
    """批次游标、职位任务、草稿与决策记忆的最小持久化能力。"""

    def ensure_batch(
        self, batch_id: str, limits: BudgetLimits, now: str
    ) -> dict[str, object]:
        """幂等创建批次，不重置既有预算。"""
        ...

    def load_batch(self, batch_id: str) -> dict[str, object] | None:
        """只读批次状态。"""
        ...

    def enqueue_jobs(
        self, batch_id: str, job_keys: tuple[str, ...], now: str
    ) -> int:
        """把当前可见职位身份加入队列。"""
        ...

    def claim_job(
        self, batch_id: str, owner: Owner, now: str, key: str | None = None
    ) -> Lease | None:
        """领取一条职位任务。"""
        ...

    def start_job(self, lease: Lease, now: str) -> Lease:
        """提交职位任务开始。"""
        ...

    def renew_job(self, lease: Lease, now: str) -> Lease:
        """延长职位任务与设备租约。"""
        ...

    def finish_job(
        self, lease: Lease, now: str, state: str, reason_code: str | None
    ) -> str:
        """收敛职位任务。"""
        ...

    def save_checkpoint(
        self, batch_id: str, cursor: BatchCursor, *, now: str
    ) -> dict[str, object]:
        """原子提交游标与检查点。"""
        ...

    def pause_batch(
        self, batch_id: str, cursor: BatchCursor, reason_code: str, *, now: str
    ) -> dict[str, object]:
        """原子保存游标并暂停。"""
        ...

    def record_runtime_event(
        self,
        batch_id: str,
        event: str,
        *,
        now: str,
        task_key: str | None = None,
        reason_code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        """追加白名单调度事件。"""
        ...

    def require_running(self, lease: Lease, *, now: str) -> bool:
        """只读确认当前任务仍 running 且任务/设备租约有效。"""
        ...

    def save_draft(self, **kwargs: object) -> dict[str, object]:
        """保存本机草稿。"""
        ...

    def save_runtime_decision(self, **kwargs: object) -> None:
        """保存最小决策记忆摘要。"""
        ...

    def find_runtime_decision(self, **kwargs: object) -> dict[str, object] | None:
        """按当前完整身份检索决策记忆。"""
        ...
