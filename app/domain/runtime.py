"""定义 M6 调度预算、安全点、候选优先级、批次身份游标与抢占审计的纯规则。

本模块不访问数据库、设备、模型或文件；调用方负责持久化与时钟。所有判定
只使用显式输入，拒绝把缺失信号当成零、把计划切换当成已切换，或把墙钟
增量当成单调等待。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.domain.models import (
    APP,
    is_conversation_key,
    is_hash,
    is_nonempty_text,
    is_uuid,
    parse_utc,
)
from app.domain.reading import is_job_key

DEFAULT_ROUND_SECONDS = 900
DEFAULT_DEVICE_CALLS = 200
DEFAULT_CANDIDATES = 20
DEFAULT_JOB_SECONDS = 30
DEFAULT_PREEMPTIONS = 5

TASK_KINDS = ("unread", "new_message", "job", "cleanup")
PAUSE_REASONS = frozenset(
    {
        "unread_unverified",
        "preemption_limit",
        "budget_exhausted",
        "clock_changed",
        "device_unavailable",
        "precondition_failed",
    }
)
BUDGET_FIELDS = (
    "seconds",
    "device_calls",
    "candidates",
    "preemptions",
)
SAFE_POINT_FIELDS = (
    "list_returned",
    "input_empty",
    "confirmation_closed",
    "no_inflight_command",
    "no_pending_action",
    "action_state_committed",
    "checkpoint_committed",
)


def _require_bool(name: str, value: object) -> None:
    """要求字段为真正的布尔值，不接受整数或其它真值。"""
    if type(value) is not bool:
        raise ValueError(f"{name} is invalid")


def _require_nonnegative_int(name: str, value: object) -> None:
    """要求字段为非负整数且不接受 bool。"""
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} is invalid")


@dataclass(frozen=True)
class BudgetLimits:
    """表示一轮开发的固定上限；仅由显式配置或默认基线构造。"""

    round_seconds: int = DEFAULT_ROUND_SECONDS
    device_calls: int = DEFAULT_DEVICE_CALLS
    candidates: int = DEFAULT_CANDIDATES
    job_seconds: int = DEFAULT_JOB_SECONDS
    preemptions: int = DEFAULT_PREEMPTIONS

    def __post_init__(self) -> None:
        """校验全部上限为正整数，拒绝 bool 和 0。"""
        for name in (
            "round_seconds",
            "device_calls",
            "candidates",
            "job_seconds",
            "preemptions",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} is invalid")


@dataclass(frozen=True)
class BudgetState:
    """表示当前批次剩余预算；等号即停止发起新工作，在途核实可单列超支。"""

    remaining_seconds: int
    remaining_device_calls: int
    remaining_candidates: int
    remaining_preemptions: int

    @classmethod
    def initial(cls, limits: BudgetLimits) -> "BudgetState":
        """按固定上限创建初始剩余预算。"""
        return cls(
            remaining_seconds=limits.round_seconds,
            remaining_device_calls=limits.device_calls,
            remaining_candidates=limits.candidates,
            remaining_preemptions=limits.preemptions,
        )

    def __post_init__(self) -> None:
        """允许在途核实的负数超支，但拒绝 bool 与非整数。"""
        for name in (
            "remaining_seconds",
            "remaining_device_calls",
            "remaining_candidates",
            "remaining_preemptions",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise ValueError(f"{name} is invalid")

    def can_start_work(self) -> bool:
        """时间/设备调用/候选仍为正才允许新工作；抢占额度单独判定。"""
        return (
            self.remaining_seconds > 0
            and self.remaining_device_calls > 0
            and self.remaining_candidates > 0
        )

    def exhausted_reason(self) -> str | None:
        """返回首个耗尽的预算项；全部为正返回 None，不把未知当成功。"""
        if self.remaining_seconds <= 0:
            return "seconds"
        if self.remaining_device_calls <= 0:
            return "device_calls"
        if self.remaining_candidates <= 0:
            return "candidates"
        if self.remaining_preemptions <= 0:
            return "preemptions"
        return None

    def spend_seconds(self, seconds: int) -> "BudgetState":
        """扣除已用秒数；在途核实超支保留负值供报告，不静默归零。"""
        _require_nonnegative_int("seconds", seconds)
        return replace(self, remaining_seconds=self.remaining_seconds - seconds)

    def spend_device_calls(self, calls: int) -> "BudgetState":
        """扣除设备调用次数，允许记录超过上限的核实在途。"""
        _require_nonnegative_int("calls", calls)
        return replace(
            self, remaining_device_calls=self.remaining_device_calls - calls
        )

    def spend_candidate(self) -> "BudgetState":
        """开始一条新候选时扣除一个名额。"""
        return replace(
            self, remaining_candidates=self.remaining_candidates - 1
        )

    def spend_preemption(self) -> "BudgetState":
        """实际完成一次职位→未读切换后扣除一次抢占额度。"""
        return replace(
            self, remaining_preemptions=self.remaining_preemptions - 1
        )

    def to_json(self) -> dict[str, int]:
        """投影为固定四键剩余预算对象；持久化不写负数，超支由报告单列。"""
        return {
            "seconds": max(0, self.remaining_seconds),
            "device_calls": max(0, self.remaining_device_calls),
            "candidates": max(0, self.remaining_candidates),
            "preemptions": max(0, self.remaining_preemptions),
        }

    @classmethod
    def from_json(cls, value: object) -> "BudgetState":
        """严格解析四键剩余预算，拒绝缺键、额外键、bool 与负数。"""
        if not isinstance(value, dict) or set(value) != set(BUDGET_FIELDS):
            raise ValueError("budget is invalid")
        numbers: dict[str, int] = {}
        for name in BUDGET_FIELDS:
            item = value[name]
            _require_nonnegative_int(name, item)
            numbers[name] = item
        return cls(
            remaining_seconds=numbers["seconds"],
            remaining_device_calls=numbers["device_calls"],
            remaining_candidates=numbers["candidates"],
            remaining_preemptions=numbers["preemptions"],
        )

    def overrun(self, limits: BudgetLimits) -> tuple[str, ...]:
        """列出已低于零的预算项，供暂停报告单列在途超支。"""
        names: list[str] = []
        if self.remaining_seconds < 0:
            names.append("seconds")
        if self.remaining_device_calls < 0:
            names.append("device_calls")
        if self.remaining_candidates < 0:
            names.append("candidates")
        if self.remaining_preemptions < 0:
            names.append("preemptions")
        return tuple(names)


@dataclass(frozen=True)
class SafePoint:
    """表示安全点的七个原始条件；必须全部为真才允许切换任务。"""

    list_returned: bool
    input_empty: bool
    confirmation_closed: bool
    no_inflight_command: bool
    no_pending_action: bool
    action_state_committed: bool
    checkpoint_committed: bool

    def __post_init__(self) -> None:
        """逐项校验七个条件都是真正的布尔值。"""
        for name in SAFE_POINT_FIELDS:
            _require_bool(name, getattr(self, name))

    def failed_conditions(self) -> tuple[str, ...]:
        """按固定顺序返回未成立条件名，便于独立断言每项。"""
        return tuple(
            name for name in SAFE_POINT_FIELDS if not getattr(self, name)
        )

    def is_safe(self) -> bool:
        """仅七项全部成立才为真；任一缺失都不把现场当作安全。"""
        return not self.failed_conditions()


@dataclass(frozen=True)
class TaskCandidate:
    """表示一条已定位候选的稳定身份；不包含正文或点击坐标。"""

    kind: str
    key: str

    def __post_init__(self) -> None:
        """校验候选类型与规范身份键。"""
        if self.kind not in TASK_KINDS:
            raise ValueError("candidate kind is invalid")
        if self.kind in {"unread", "new_message"}:
            if not is_conversation_key(APP, self.key):
                raise ValueError("candidate key is invalid")
        elif self.kind == "job":
            if not is_job_key(self.key):
                raise ValueError("candidate key is invalid")
        elif not is_nonempty_text(self.key):
            raise ValueError("candidate key is invalid")


@dataclass(frozen=True)
class SceneCandidates:
    """表示一次刷新得到的候选与未读信号状态，不携带读取正文。"""

    unread: tuple[TaskCandidate, ...]
    new_messages: tuple[TaskCandidate, ...]
    jobs: tuple[TaskCandidate, ...]
    cleanup: tuple[TaskCandidate, ...]
    unread_unknown: bool
    unread_signal_without_target: bool

    def __post_init__(self) -> None:
        """校验候选类型、去重与两个独立未读状态布尔值。"""
        for name, kind in (
            ("unread", "unread"),
            ("new_messages", "new_message"),
            ("jobs", "job"),
            ("cleanup", "cleanup"),
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ValueError(f"{name} is invalid")
            seen: set[str] = set()
            for item in values:
                if not isinstance(item, TaskCandidate) or item.kind != kind:
                    raise ValueError(f"{name} is invalid")
                if item.key in seen:
                    raise ValueError(f"{name} is duplicated")
                seen.add(item.key)
        _require_bool("unread_unknown", self.unread_unknown)
        _require_bool(
            "unread_signal_without_target", self.unread_signal_without_target
        )


@dataclass(frozen=True)
class Selection:
    """表示一次调度结果：任务、需要一次定向核对，或固定暂停原因。"""

    task: TaskCandidate | None
    verify_unread: bool
    pause_reason: str | None

    def __post_init__(self) -> None:
        """校验三种互斥出口，避免同时声称任务与暂停。"""
        _require_bool("verify_unread", self.verify_unread)
        if self.pause_reason is not None and self.pause_reason not in PAUSE_REASONS:
            raise ValueError("pause reason is invalid")
        if self.task is not None and not isinstance(self.task, TaskCandidate):
            raise ValueError("task is invalid")
        active = sum(
            (
                self.task is not None,
                self.verify_unread,
                self.pause_reason is not None,
            )
        )
        if active > 1:
            raise ValueError("selection is ambiguous")


def select_task(
    scene: SceneCandidates,
    *,
    switching_from_job: bool,
    preemptions_used: int,
    preemption_limit: int,
    unread_verification_used: bool,
    unread_verified_unknown: bool,
) -> Selection:
    """按未读→新消息→职位→清理的固定优先级选择一条任务。

    仅当没有可定位未读目标且状态未知或“有计数无定位”时要求一次定向
    核对；已核对仍未知则暂停 `unread_unverified`。职位进行中切换到未读
    会先检查抢占上限，达到或超过上限即暂停 `preemption_limit`。
    """
    if not isinstance(scene, SceneCandidates):
        raise ValueError("scene is invalid")
    _require_bool("switching_from_job", switching_from_job)
    _require_bool("unread_verification_used", unread_verification_used)
    _require_bool("unread_verified_unknown", unread_verified_unknown)
    _require_nonnegative_int("preemptions_used", preemptions_used)
    if type(preemption_limit) is not int or preemption_limit < 1:
        raise ValueError("preemption limit is invalid")

    unread = scene.unread[0] if scene.unread else None
    if unread is None and (
        scene.unread_unknown or scene.unread_signal_without_target
    ):
        if unread_verification_used or unread_verified_unknown:
            return Selection(
                task=None, verify_unread=False, pause_reason="unread_unverified"
            )
        return Selection(task=None, verify_unread=True, pause_reason=None)
    if unread is not None:
        if switching_from_job and preemptions_used >= preemption_limit:
            return Selection(
                task=None, verify_unread=False, pause_reason="preemption_limit"
            )
        return Selection(task=unread, verify_unread=False, pause_reason=None)
    if scene.new_messages:
        return Selection(
            task=scene.new_messages[0], verify_unread=False, pause_reason=None
        )
    if scene.jobs:
        return Selection(task=scene.jobs[0], verify_unread=False, pause_reason=None)
    if scene.cleanup:
        return Selection(
            task=scene.cleanup[0], verify_unread=False, pause_reason=None
        )
    return Selection(task=None, verify_unread=False, pause_reason=None)


@dataclass(frozen=True)
class BatchCursor:
    """表示按职位身份恢复的批次游标与持久预算；绝不是一条任务。"""

    batch_id: str
    last_job_dedupe_key: str | None
    visited_keys: tuple[str, ...]
    budget: BudgetState
    preemptions_used: int
    first_observed_at: str | None

    def __post_init__(self) -> None:
        """校验批次身份、职位键去重、预算与首次观察时间。"""
        if not is_uuid(self.batch_id):
            raise ValueError("batch id is invalid")
        if self.last_job_dedupe_key is not None:
            if not is_job_key(self.last_job_dedupe_key):
                raise ValueError("last job key is invalid")
        if not isinstance(self.visited_keys, tuple) or any(
            not is_job_key(key) for key in self.visited_keys
        ):
            raise ValueError("visited keys are invalid")
        if len(set(self.visited_keys)) != len(self.visited_keys):
            raise ValueError("visited keys are duplicated")
        if (
            self.last_job_dedupe_key is not None
            and self.last_job_dedupe_key not in self.visited_keys
        ):
            raise ValueError("last job key is not visited")
        if not isinstance(self.budget, BudgetState):
            raise ValueError("budget is invalid")
        _require_nonnegative_int("preemptions_used", self.preemptions_used)
        if self.first_observed_at is not None:
            parse_utc(self.first_observed_at)

    @classmethod
    def initial(cls, batch_id: str, limits: BudgetLimits) -> "BatchCursor":
        """按固定预算创建还没有处理任何职位的批次游标。"""
        return cls(
            batch_id=batch_id,
            last_job_dedupe_key=None,
            visited_keys=(),
            budget=BudgetState.initial(limits),
            preemptions_used=0,
            first_observed_at=None,
        )

    def visit_job(self, job_key: str) -> "BatchCursor":
        """记录首次处理完成的职位身份；重复处理不重复登记。"""
        if not is_job_key(job_key):
            raise ValueError("job key is invalid")
        visited = self.visited_keys
        if job_key in visited:
            return replace(self, last_job_dedupe_key=job_key)
        return replace(
            self,
            last_job_dedupe_key=job_key,
            visited_keys=visited + (job_key,),
            budget=self.budget.spend_candidate(),
        )

    def next_job(self, ordered_keys: tuple[str, ...]) -> str | None:
        """从当前身份顺序恢复下一条未访问职位；消失跳过、插入不按下标。"""
        if not isinstance(ordered_keys, tuple) or any(
            not is_job_key(key) for key in ordered_keys
        ):
            raise ValueError("ordered keys are invalid")
        if len(set(ordered_keys)) != len(ordered_keys):
            raise ValueError("ordered keys are duplicated")
        visited = set(self.visited_keys)
        if self.last_job_dedupe_key in ordered_keys:
            index = ordered_keys.index(self.last_job_dedupe_key)
            rotated = ordered_keys[index + 1 :] + ordered_keys[: index + 1]
        else:
            rotated = ordered_keys
        for key in rotated:
            if key not in visited:
                return key
        return None

    def record_preemption(self) -> "BatchCursor":
        """实际完成一次职位→未读切换后登记并扣除抢占额度。"""
        return replace(
            self,
            preemptions_used=self.preemptions_used + 1,
            budget=self.budget.spend_preemption(),
        )

    def with_first_observed(self, observed_at: str) -> "BatchCursor":
        """保留首次观察到未读的墙钟时间，刷新不覆盖已有值。"""
        parse_utc(observed_at)
        if self.first_observed_at is not None:
            return self
        return replace(self, first_observed_at=observed_at)

    def to_json(self) -> dict[str, object]:
        """投影为持久化游标对象；不包含列表下标。"""
        return {
            "batch_id": self.batch_id,
            "last_job_dedupe_key": self.last_job_dedupe_key,
            "visited_keys": list(self.visited_keys),
            "remaining_budget": self.budget.to_json(),
            "preemptions_used": self.preemptions_used,
            "first_observed_at": self.first_observed_at,
        }

    @classmethod
    def from_json(cls, value: object) -> "BatchCursor":
        """严格解析持久化游标；缺键、额外键或类型错误一律拒绝。"""
        fields = {
            "batch_id",
            "last_job_dedupe_key",
            "visited_keys",
            "remaining_budget",
            "preemptions_used",
            "first_observed_at",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("cursor is invalid")
        visited = value["visited_keys"]
        if not isinstance(visited, list):
            raise ValueError("visited keys are invalid")
        return cls(
            batch_id=value["batch_id"],  # type: ignore[arg-type]
            last_job_dedupe_key=value["last_job_dedupe_key"],  # type: ignore[arg-type]
            visited_keys=tuple(visited),
            budget=BudgetState.from_json(value["remaining_budget"]),
            preemptions_used=value["preemptions_used"],  # type: ignore[arg-type]
            first_observed_at=value["first_observed_at"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class PreemptionObservation:
    """表示首次未读观察的墙钟与单调时钟，跨刷新保持原值。"""

    first_observed_at: str
    first_observed_monotonic_ms: int

    def __post_init__(self) -> None:
        """校验首次观察时间与单调毫秒为非负整数。"""
        parse_utc(self.first_observed_at)
        _require_nonnegative_int(
            "first observed monotonic", self.first_observed_monotonic_ms
        )


def observe_unread(
    previous: PreemptionObservation | None,
    *,
    wall_now: str,
    monotonic_ms: int,
) -> PreemptionObservation:
    """返回首次观察；已有记录时保留原时间，不因刷新把等待清零。"""
    parse_utc(wall_now)
    _require_nonnegative_int("monotonic ms", monotonic_ms)
    if previous is not None:
        return previous
    return PreemptionObservation(
        first_observed_at=wall_now,
        first_observed_monotonic_ms=monotonic_ms,
    )


def build_stage_preempted(
    *,
    job_key: str,
    trigger: str,
    first: PreemptionObservation,
    handling_started_at: str,
    handling_started_monotonic_ms: int,
) -> dict[str, object]:
    """在实际切换且开始处理后构造 stage_preempted；不伪造未发生切换。"""
    if not is_job_key(job_key):
        raise ValueError("job key is invalid")
    if not is_nonempty_text(trigger):
        raise ValueError("trigger is invalid")
    if not isinstance(first, PreemptionObservation):
        raise ValueError("first observation is invalid")
    parse_utc(handling_started_at)
    _require_nonnegative_int(
        "handling started monotonic", handling_started_monotonic_ms
    )
    waited_ms = handling_started_monotonic_ms - first.first_observed_monotonic_ms
    if waited_ms < 0:
        raise ValueError("monotonic order is invalid")
    return {
        "event": "stage_preempted",
        "from": "job",
        "to": "unread",
        "job_key": job_key,
        "trigger": trigger,
        "first_observed_at": first.first_observed_at,
        "handling_started_at": handling_started_at,
        "waited_ms": waited_ms,
    }


def detection_interval_ms(previous_monotonic_ms: int, current_monotonic_ms: int) -> int:
    """计算连续两次未读检查的单调时间差；检测与调度等待分开记录。"""
    _require_nonnegative_int("previous monotonic", previous_monotonic_ms)
    _require_nonnegative_int("current monotonic", current_monotonic_ms)
    if current_monotonic_ms < previous_monotonic_ms:
        raise ValueError("monotonic order is invalid")
    return current_monotonic_ms - previous_monotonic_ms


def clock_drift_exceeded(
    *,
    wall_start_ms: int,
    wall_now_ms: int,
    monotonic_start_ms: int,
    monotonic_now_ms: int,
    tolerance_ms: int = 1000,
) -> bool:
    """比较墙钟与单调时钟增量，任一项为负或差超过容差即判漂移。"""
    for name, value in (
        ("wall start", wall_start_ms),
        ("wall now", wall_now_ms),
        ("monotonic start", monotonic_start_ms),
        ("monotonic now", monotonic_now_ms),
    ):
        _require_nonnegative_int(name, value)
    if type(tolerance_ms) is not int or tolerance_ms < 0:
        raise ValueError("tolerance is invalid")
    wall_delta = wall_now_ms - wall_start_ms
    monotonic_delta = monotonic_now_ms - monotonic_start_ms
    if wall_delta < 0 or monotonic_delta < 0:
        return True
    return abs(wall_delta - monotonic_delta) > tolerance_ms


def job_dedupe_key(job_key: str) -> str:
    """职位去重键就是规范职位键，不另造摘要或复用列表下标。"""
    if not is_job_key(job_key):
        raise ValueError("job key is invalid")
    return job_key


def is_runtime_key(value: object) -> bool:
    """判断是否为调度可接受的会话或职位身份键。"""
    return isinstance(value, str) and (
        is_conversation_key(APP, value) or is_job_key(value)
    )


def is_cursor_hash(value: object) -> bool:
    """判断是否为游标中的 64 位摘要（供输入/策略身份复用）。"""
    return is_hash(value)
