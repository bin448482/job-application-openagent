"""声明 M6 实际运行需要的场景、原始读取、决策桥与批次持久化能力。"""

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
class RawScene:
    """表示设备现场原始刺激：输入、确认框与在途命令，不直接宣告安全点。"""

    input_text: str
    confirmation_title: str | None
    inflight_commands: int
    ime_current: str | None = None

    def __post_init__(self) -> None:
        """校验原始现场字段类型，不把缺失当空。"""
        if not isinstance(self.input_text, str):
            raise ValueError("input text is invalid")
        if self.confirmation_title is not None and not isinstance(
            self.confirmation_title, str
        ):
            raise ValueError("confirmation title is invalid")
        if type(self.inflight_commands) is not int or self.inflight_commands < 0:
            raise ValueError("inflight commands are invalid")
        if self.ime_current is not None and not isinstance(self.ime_current, str):
            raise ValueError("ime is invalid")


@dataclass(frozen=True)
class DecisionStimulus:
    """表示一次合成 bridge transport 响应字段；不是已校验 Decision。"""

    intent: str
    sub_intents: tuple[str, ...]
    reply: str
    escalate: str | None = None

    def __post_init__(self) -> None:
        """只做原始响应形状校验；词表/档位/证据由唯一 decide 校验。"""
        if not isinstance(self.intent, str):
            raise ValueError("decision intent is invalid")
        if not isinstance(self.sub_intents, tuple) or any(
            not isinstance(item, str) for item in self.sub_intents
        ):
            raise ValueError("decision sub intents are invalid")
        if not isinstance(self.reply, str):
            raise ValueError("decision reply is invalid")
        if self.escalate is not None and not isinstance(self.escalate, str):
            raise ValueError("decision escalate is invalid")


@dataclass(frozen=True)
class SceneStep:
    """表示安全点处的一次原始场景刺激；不含已终态的 read/decision 判决。"""

    read_fixture: str
    raw_scene: RawScene
    device_calls: int = 0
    conversation_fixture: str | None = None
    job_fixture: str | None = None
    # 定向核对使用的独立 M3 原始输入引用；缺省表示当前场景没有采集能力。
    targeted_read_fixture: str | None = None
    decision: DecisionStimulus | None = None
    # 兼容旧调用方的刺激字段；生产用例不把这些当作最终判决。
    wall_now: str = "1970-01-01T00:00:00.000Z"
    monotonic_ms: int = 0
    elapsed_seconds: int = 0
    candidates: SceneCandidates | None = None
    safe_point: SafePoint | None = None
    task_outcome: str = "done"
    reason_code: str | None = None
    unread_verification: str = "none"
    read_result: ReadResult | None = None
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
    """按实际调度提供原始场景、M3 读取引用与 bridge 响应刺激。"""

    def next_step(self) -> SceneStep | None:
        """返回下一次刷新；没有更多刺激时返回 None。"""
        ...


class RuntimeCommandControlError(Exception):
    """逐命令控制/续租失败的固定安全原因，供 execute_run 结构化停止。"""

    def __init__(self, reason_code: str = "precondition_failed") -> None:
        """保存固定原因码，不携带设备、路径或异常原文。"""
        if not isinstance(reason_code, str) or not reason_code:
            raise ValueError("command control reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


class RuntimeCommandContext(Protocol):
    """逐命令复核/续租上下文；由 execute_run 绑定，适配器只回调。"""

    def before(self) -> None:
        """命令启动前复核共享门禁、当前任务、完整 Lease 与 nonce。"""
        ...

    def after(self, *, failed: bool) -> None:
        """命令结束后取独立可信时钟，按需续租并保存最新 Lease。"""
        ...


class RuntimeReadPort(Protocol):
    """按项目内 M3 manifest 调用真实解析/门禁，返回三态 ReadResult。"""

    def read(self, relative: str) -> ReadResult:
        """读取并解析一个 M3 fixture；不得读取 expected/答案。"""
        ...

    def take_device_calls(self) -> int:
        """返回自上次调用以来实际发生的设备读取/命令数。"""
        ...

    def bind_command_context(self, context: object | None) -> None:
        """绑定/解绑逐命令复核与续租上下文；fixture 实现可省略。"""
        ...


class RuntimeStorePort(Protocol):
    """批次游标、职位任务、草稿与决策记忆的最小持久化能力。"""

    def ensure_batch(
        self, batch_id: str, limits: BudgetLimits, now: str
    ) -> dict[str, object]:
        """幂等创建批次，不重置既有预算。"""
        ...

    def resume_batch(self, batch_id: str, now: str) -> dict[str, object]:
        """把 paused/completed 批次原子转回 active，保留游标与预算。"""
        ...

    def complete_batch(self, batch_id: str, cursor: BatchCursor, now: str) -> dict[str, object]:
        """把安全结束的批次标记 completed，保留可恢复游标与预算。"""
        ...

    def load_batch(self, batch_id: str) -> dict[str, object] | None:
        """只读批次状态。"""
        ...

    def checkpoint_status(self, batch_id: str) -> dict[str, object]:
        """只读返回 last_checkpoint_id 与 checkpoint 事件计数。"""
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

    def safety_stop_after_clock_change(
        self, batch_id: str, cursor: BatchCursor, reason_code: str, *, now: str
    ) -> dict[str, object]:
        """时钟异常后的专用安全停止：保存最后可靠游标/预算，不续租或放宽租约。"""
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

    def finish_semantic_block(
        self, lease: Lease, now: str, reason_code: str
    ) -> str:
        """仅语义未决时阻塞任务并释放安全设备行。"""
        ...

    def last_action_state(self, task_key: str) -> str | None:
        """只读返回该任务最近动作状态；会话键按真实发现关联，无动作返回 None。"""
        ...

    def queued_new_message_keys(self) -> tuple[str, ...]:
        """只读返回同一 profile 中已持久化且仍 queued 的 M2 新消息会话键。"""
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

    def find_draft_by_identity(self, **kwargs: object) -> dict[str, object] | None:
        """按完整身份检索可重审草稿关联。"""
        ...

    def get_draft(self, draft_id: str) -> dict[str, object] | None:
        """只读草稿。"""
        ...
