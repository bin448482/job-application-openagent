"""编排 M6 实际运行：共享门禁整轮持有、M3 读取门禁、唯一决策与安全点调度。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Callable

from app.adapters.sqlite.session import ProfileSessionLease
from app.domain.models import (
    Lease,
    Owner,
    ReadResult,
    canonical_hash,
    is_conversation_key,
    parse_utc,
    validate_read_result,
)
from app.domain.policy import (
    PolicyPack,
    build_envelope,
    compute_base_hash,
    compute_pack_hash,
    effective_limits,
    render_request,
    resolve_policy_tier,
    v1_policy_tier,
)
from app.domain.profile import select_profile_facts
from app.domain.reading import (
    Conversation,
    JobDetail,
    JobList,
    PageSnapshot,
    UnreadRead,
)
from app.domain.runtime import (
    BatchCursor,
    BudgetLimits,
    PreemptionObservation,
    PAUSE_REASONS,
    SafePoint,
    SAFE_POINT_FIELDS,
    SceneCandidates,
    Selection,
    TaskCandidate,
    build_stage_preempted,
    clock_drift_exceeded,
    detection_interval_ms,
    is_runtime_key,
    observe_unread,
    select_task,
)
from app.ports.decision import DecisionBridgePort, DecisionSnapshotPort
from app.ports.device_gate import DeviceGateError, SharedDeviceGatePort
from app.ports.eval import EvalStorePort
from app.ports.policy import PolicyError, PolicyIssue
from app.ports.profile import ProfileStorePort
from app.ports.runtime import (
    RawScene,
    RuntimeCommandContext,
    RuntimeCommandControlError,
    RuntimeReadPort,
    RuntimeScenePort,
    RuntimeStorePort,
    SceneStep,
)
from app.ports.store import StoreError
from app.process_log import ProcessLog
from app.profile_open import open_profile_store
from app.usecases.acceptance import check_acceptance
from app.usecases.decision import DecisionRunError, decide
from app.usecases.read import is_actionable, targeted_verification_outcome

RUN_MODES = ("read_only", "draft_only", "auto_send_tier_a")

def _issue(field: str, code: str) -> PolicyIssue:
    """构造固定运行诊断，不携带事实值、键或路径。"""
    return PolicyIssue(file=None, field=field, line=None, code=code)


class _ClockPort:
    """把调用方可信 UTC 时钟适配为 DecisionClockPort。"""

    def __init__(self, clock: Callable[[], str]) -> None:
        """保存可调用 UTC 时钟。"""
        self._clock = clock

    def now(self) -> str:
        """返回当前规范 UTC 时间。"""
        return self._clock()


def _acceptance(
    *,
    mode: str,
    eval_store: EvalStorePort | None,
    credential_rel: str | None,
    provider_id: str | None,
    model_id: str | None,
    model_config_hash: str | None,
    base_files: dict[str, str],
    pack: PolicyPack,
) -> dict[str, object] | None:
    """按模式复核当前验收；draft_only 缺任何身份都失败关闭。"""
    if mode != "draft_only":
        return None
    if (
        eval_store is None
        or not credential_rel
        or not provider_id
        or not model_id
        or not model_config_hash
    ):
        raise PolicyError("authorization_required", [_issue("acceptance", "missing")])
    return check_acceptance(
        eval_store,
        credential_rel,
        base_files=base_files,
        pack=pack,
        provider_id=provider_id,
        model_id=model_id,
        model_config_hash=model_config_hash,
    )


def prepare_profile(
    *,
    base_files: dict[str, str],
    pack: PolicyPack,
    gate: SharedDeviceGatePort,
    profile_store: ProfileStorePort,
    owner: Owner,
    profile_db_path: str,
    now: str,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """显式准备/升级 profile 库：先取得共享排他，再打开并迁移。"""
    gate.acquire(
        profile_id=pack.profile_id,
        profile_db_path=profile_db_path,
        owner=owner,
        now=now,
    )
    converged = False
    try:
        gate.verify(now=now)
        open_profile_store(profile_store, process_log)
        converged = True
    finally:
        if converged:
            profile_store.close()
        gate.release(now=now, converged=converged)
    return {
        "prepared": True,
        "profile_id": pack.profile_id,
        "base_hash": compute_base_hash(base_files),
        "pack_hash": pack.pack_hash,
        "gate": "released",
    }


def _facts(profile_store: ProfileStorePort, pack: PolicyPack, *, now: str) -> dict[str, object]:
    """读取当前 active 事实并交给唯一事实筛选；不在此处推断事实。"""
    return select_profile_facts(profile_store.active_fact_rows(), pack, now=now)


def _facts_hash(selected: dict[str, object]) -> str:
    """对允许事实（含有效期）与待办原因计算摘要。"""
    return canonical_hash(selected)


def _iter_read_values(read_results: tuple[object, ...]) -> Iterator[object]:
    """只迭代严格有效且 status=ok 的真实读取值，坏结果不冒充现场。"""
    for result in read_results:
        if not isinstance(result, ReadResult):
            continue
        try:
            validate_read_result(result)
        except ValueError:
            continue
        if result.status == "ok" and result.value is not None:
            yield result.value


def _list_returned(read_results: tuple[object, ...]) -> bool:
    """只把真实列表页/列表值视为已回到列表；详情、会话与 unknown 页面不算。

    这里判断的是页面/导航事实，不要求当前列表已经存在可处理目标：合法
    全覆盖 absent 的 `UnreadRead` 仍是回到列表，只是没有任务可做。
    """
    for value in _iter_read_values(read_results):
        if isinstance(value, (JobList, UnreadRead)):
            return True
        if isinstance(value, PageSnapshot) and value.page == "chat_list":
            return True
    return False


def _action_state_committed(
    store: RuntimeStorePort,
    task_key: str | None,
) -> bool:
    """用本轮最近一次真实成功 start 的任务身份读取最近动作状态。

    `None` 只表示本轮尚未成功 start 任何任务：没有可归属动作，真正的未决
    动作仍由全局 `no_pending_action` 拦截；历史已收敛动作不会阻断新轮。
    非法身份保守为假，不回退全局最近动作，也不要求任务仍在当前读取现场。
    """
    if task_key is None:
        return True
    if not is_runtime_key(task_key):
        return False
    state = store.last_action_state(task_key)
    return state in {None, "performed", "not_performed"}


def derive_safe_point(
    *,
    store: RuntimeStorePort,
    batch_id: str,
    task_key: str | None,
    read_results: tuple[object, ...],
    raw: RawScene,
) -> SafePoint:
    """从真实读取类型、持久 pending/动作/检查点与原始现场派生七条件。

    该入口同时服务生产 `execute_run` 与独立模块探针；不接受调用方直接
    传入 safe/committed 布尔答案，也不把 fixture hint 当作安全点证据。
    `task_key` 必须是本轮最近一次真实 claim/start 成功的任务身份；`None`
    仅用于尚未 start 任何任务或只读观察。
    """
    pending = store.pending_actions("boss")
    checkpoint = store.checkpoint_status(batch_id)
    return SafePoint(
        list_returned=_list_returned(read_results),
        input_empty=raw.input_text == "",
        confirmation_closed=raw.confirmation_title is None,
        no_inflight_command=raw.inflight_commands == 0,
        no_pending_action=not pending,
        action_state_committed=_action_state_committed(store, task_key),
        checkpoint_committed=checkpoint["last_checkpoint_id"] is not None
        and int(checkpoint["checkpoint_events"]) > 0,
    )


def _candidates(
    read_results: tuple[object, ...],
    *,
    hints: SceneCandidates | None,
    unread_unknown: bool,
    queued_new_messages: tuple[str, ...] = (),
) -> SceneCandidates:
    """只从实际 M3 值类型与已持久化 M2 新消息发现派生候选；hints 不参与。"""
    del hints
    unread: list[TaskCandidate] = []
    jobs: list[TaskCandidate] = []
    new_messages: list[TaskCandidate] = []
    seen_unread: set[str] = set()
    seen_jobs: set[str] = set()
    seen_new: set[str] = set()
    for value in read_results:
        if isinstance(value, UnreadRead):
            for row in value.rows:
                if (
                    row.conversation_key is not None
                    and row.state == "present"
                    and row.conversation_key not in seen_unread
                ):
                    unread.append(TaskCandidate("unread", row.conversation_key))
                    seen_unread.add(row.conversation_key)
        elif isinstance(value, JobList):
            for card in value.cards:
                if card.job_key is not None and card.job_key not in seen_jobs:
                    jobs.append(TaskCandidate("job", card.job_key))
                    seen_jobs.add(card.job_key)
        elif isinstance(value, JobDetail):
            if value.job_key not in seen_jobs:
                jobs.append(TaskCandidate("job", value.job_key))
                seen_jobs.add(value.job_key)
    for key in queued_new_messages:
        if (
            is_conversation_key("boss", key)
            and key not in seen_unread
            and key not in seen_new
        ):
            new_messages.append(TaskCandidate("new_message", key))
            seen_new.add(key)
    signal_without_target = any(
        isinstance(value, UnreadRead)
        and value.unread_state == "present"
        and not any(
            row.conversation_key is not None and row.state == "present"
            for row in value.rows
        )
        for value in read_results
    )
    unknown_state = any(
        isinstance(value, UnreadRead) and value.unread_state == "unknown"
        for value in read_results
    )
    return SceneCandidates(
        unread=tuple(unread),
        new_messages=tuple(new_messages),
        jobs=tuple(jobs),
        cleanup=(),
        unread_unknown=unread_unknown or unknown_state,
        unread_signal_without_target=signal_without_target,
    )


def _targeted_unread_candidates(value: UnreadRead) -> tuple[TaskCandidate, ...]:
    """只把定向采集里真实 present 且可定位的行投影为未读候选。"""
    seen: set[str] = set()
    items: list[TaskCandidate] = []
    for row in value.rows:
        key = row.conversation_key
        if row.state != "present" or key is None or key in seen:
            continue
        seen.add(key)
        items.append(TaskCandidate("unread", key))
    return tuple(items)


_TARGETED_LOG_REASONS = frozenset(
    {
        "unread_unverified",
        "stale_snapshot",
        "identity_ambiguous",
        "not_ok",
        "wrong_page",
        "invalid_xml",
        "invalid_fixture",
        "device_disconnected",
        "device_offline",
        "device_unauthorized",
    }
)


def _read_target(
    read: RuntimeReadPort, step: SceneStep
) -> tuple[tuple[object, ...], object, object | None, tuple[object, ...]]:
    """读取可见页面与可选会话；返回值与原始 ReadResult 分开。"""
    values: list[object] = []
    raw: list[object] = []
    primary = read.read(step.read_fixture)
    raw.append(primary)
    if primary.status == "ok":
        values.append(primary.value)
    conversation = None
    if step.conversation_fixture is not None:
        conv_result = read.read(step.conversation_fixture)
        raw.append(conv_result)
        if conv_result.status == "ok":
            conversation = conv_result.value
            values.append(conversation)
    return tuple(values), primary, conversation, tuple(raw)


def _inbound_for(conversation: Conversation | None) -> str | None:
    """取已读全会话中最新非系统 incoming 文本；缺失不猜。"""
    if conversation is None or not conversation.read_complete:
        return None
    for message in reversed(conversation.messages):
        if message.kind != "system" and message.direction == "incoming":
            if isinstance(message.text, str) and message.text:
                return message.text
    return None


def _decision_for(
    *,
    store: RuntimeStorePort,
    batch_id: str,
    lease: Lease,
    task: TaskCandidate,
    conversation: Conversation,
    primary: object,
    step: SceneStep,
    pack: PolicyPack,
    base_files: dict[str, str],
    profile_store: ProfileStorePort,
    gate: SharedDeviceGatePort,
    profile_db_path: str,
    snapshot: DecisionSnapshotPort,
    bridge_factory: Callable[[object], DecisionBridgePort],
    provider_id: str,
    model_id: str,
    model_config_hash: str,
    now: str,
    clock: Callable[[], str],
    process_log: ProcessLog | None,
    acceptance: dict[str, object] | None = None,
    acceptance_recheck: Callable[[], dict[str, object] | None] | None = None,
) -> dict[str, object]:
    """经唯一 build/render/decide 路径生成或复用可重审草稿。"""
    inbound = _inbound_for(conversation)
    if inbound is None:
        raise DecisionRunError(
            "invalid_decision", submitted=False, attempts=0, submissions=0
        )
    selected = _facts(profile_store, pack, now=now)
    base_hash = compute_base_hash(base_files)
    envelope = build_envelope(
        task_id=lease.key,
        nonce=str(uuid.uuid4()),
        kind="hr_message",
        subject={
            "conversation_key": conversation.conversation_key,
            "job_brief": None,
        },
        inbound=inbound,
        context=[],
        allowed_facts=list(selected["allowed"]),  # type: ignore[arg-type]
        base_files=base_files,
        pack=pack,
        now=now,
    )
    rendered = render_request(base_files, pack, envelope, now=now)
    facts_hash = _facts_hash(selected)
    evidence_hash = canonical_hash(
        {
            "conversation_key": conversation.conversation_key,
            "latest_inbound_key": conversation.latest_inbound_key,
            "messages": [
                {
                    "kind": message.kind,
                    "direction": message.direction,
                    "content_hash": message.content_hash,
                    "stable_identity_hash": message.stable_identity_hash,
                    "message_key": message.message_key,
                    "time_hint": message.time_hint,
                }
                for message in conversation.messages
            ],
        }
    )
    input_hash = canonical_hash(
        {
            "provider_id": provider_id,
            "prompt_hash": rendered.prompt_hash,
            "facts_hash": facts_hash,
            "evidence_hash": evidence_hash,
        }
    )
    memory = store.find_runtime_decision(
        subject_key=task.key,
        task_kind="discovery",
        input_hash=input_hash,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        model_id=model_id,
        model_config_hash=model_config_hash,
        facts_hash=facts_hash,
    )
    draft = store.find_draft_by_identity(
        task_key=task.key,
        subject_key=task.key,
        input_hash=input_hash,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        model_id=model_id,
        model_config_hash=model_config_hash,
        facts_hash=facts_hash,
    )
    if memory is not None and draft is not None:
        current_base, current_pack_files = snapshot.reload()
        if (
            compute_base_hash(current_base) != base_hash
            or compute_pack_hash(current_pack_files) != pack.pack_hash
        ):
            raise DecisionRunError(
                "policy_changed", submitted=False, attempts=0, submissions=0
            )
        current_lease = ProfileSessionLease(
            store,
            lease,
            profile_id=pack.profile_id,
            gate=gate,
            profile_db_path=Path(profile_db_path),
            clock=clock,
        )
        if current_lease.current_expiry() is None:
            raise DecisionRunError(
                "authorization_required", submitted=False, attempts=0, submissions=0
            )
        if acceptance_recheck is not None:
            if acceptance_recheck() != acceptance:
                raise DecisionRunError(
                    "policy_changed", submitted=False, attempts=0, submissions=0
                )
        _validate_cached_semantics(
            memory, draft, pack=pack, intent=str(memory["intent"]),
            sub_intents=tuple(str(item) for item in memory["sub_intents"]),
        )
        if not _draft_valid(draft, pack=pack, facts=selected):
            raise DecisionRunError(
                "invalid_decision", submitted=False, attempts=0, submissions=0
            )
        _validate_cached_summary(
            memory,
            draft,
            intent=str(memory["intent"]),
            sub_intents=tuple(str(item) for item in memory["sub_intents"]),
            tier=str(memory["policy_tier"]),
        )
        store.record_runtime_event(
            batch_id,
            "decision_reused",
            now=clock(),
            task_key=lease.key if lease.key.startswith("boss:job:") else None,
            payload={"draft_id": draft["draft_id"]},
        )
        return {
            "draft": draft,
            "reused": True,
            "needs_human": str(memory["policy_tier"]) == "C"
            and str(memory["intent"]) in {"needs_human", "uncertain"},
        }
    bridge = bridge_factory(step.decision)
    result = decide(
        rendered=rendered,
        envelope=envelope,
        base_files=base_files,
        pack=pack,
        bridge=bridge,
        snapshot=snapshot,
        lease=ProfileSessionLease(
            store,
            lease,
            profile_id=pack.profile_id,
            gate=gate,
            profile_db_path=Path(profile_db_path),
            clock=clock,
        ),
        clock=_ClockPort(clock),
        process_log=process_log,
    )
    audit = result["audit"]
    if (
        audit.get("provider_id") != provider_id
        or audit.get("model_id") != model_id
        or audit.get("model_config_hash") != model_config_hash
    ):
        raise DecisionRunError(
            "policy_changed", submitted=False, attempts=0, submissions=0
        )
    decision = result["decision"]
    intent = str(decision["intent"])
    sub_intents = tuple(str(item) for item in decision["sub_intents"])
    tier = str(result["resolved_tier"])
    reply = str(result["reply"])
    draft_id = str(uuid.uuid4())
    saved = store.save_draft(
        draft_id=draft_id,
        task_kind="discovery",
        task_key=task.key,
        subject_key=task.key,
        input_hash=input_hash,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        model_id=model_id,
        model_config_hash=model_config_hash,
        facts_hash=facts_hash,
        intent=intent,
        sub_intents=sub_intents,
        policy_tier=tier,
        reply=reply,
        now=clock(),
    )
    store.save_runtime_decision(
        subject_key=task.key,
        task_kind="discovery",
        input_hash=input_hash,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        model_id=model_id,
        model_config_hash=model_config_hash,
        facts_hash=facts_hash,
        intent=intent,
        sub_intents=sub_intents,
        policy_tier=tier,
        now=clock(),
    )
    return {
        "draft": saved,
        "reused": False,
        "needs_human": tier == "C" and intent in {"needs_human", "uncertain"},
    }


def _validate_cached_semantics(
    memory: dict[str, object],
    draft: dict[str, object],
    *,
    pack: PolicyPack,
    intent: str,
    sub_intents: tuple[str, ...],
) -> None:
    """用当前 pack 重算档位/词表/硬下限；损坏但一致的两侧也不放行。"""
    if intent not in {
        "rejected",
        "declined",
        "interested",
        "needs_human",
        "uncertain",
    } or any(
        item
        not in {
            "greeting",
            "resume_request",
            "job_question",
            "interview",
            "salary",
            "jd_request",
            "sensitive",
            "non_text",
            "ambiguous",
        }
        for item in sub_intents
    ):
        raise DecisionRunError(
            "invalid_decision", submitted=False, attempts=0, submissions=0
        )
    try:
        expected_tier = resolve_policy_tier(
            pack,
            intent,
            list(sub_intents),
            None,
            v1_policy_tier(),
        )
    except ValueError:
        raise DecisionRunError(
            "invalid_decision", submitted=False, attempts=0, submissions=0
        ) from None
    if memory.get("policy_tier") != expected_tier or draft.get("policy_tier") != expected_tier:
        raise DecisionRunError(
            "invalid_decision", submitted=False, attempts=0, submissions=0
        )


def _draft_valid(
    draft: dict[str, object],
    *,
    pack: PolicyPack,
    facts: dict[str, object],
) -> bool:
    """重核草稿状态/回复；任一失效返回 False，不沿用旧决策。"""
    if draft.get("state") != "pending_review":
        return False
    try:
        from app.domain.policy import append_and_validate_reply

        limits = effective_limits(pack)
        append_and_validate_reply(
            str(draft["reply"]),
            disclosure=str(limits["disclosure"]),
            max_chars=int(limits["max_chars"]),
            never_say=limits["never_say"],
            extra_topics=limits["extra_topics"],
            allowed_facts=list(facts["allowed"]),  # type: ignore[arg-type]
        )
    except Exception:
        return False
    return True


def _validate_cached_summary(
    memory: dict[str, object],
    draft: dict[str, object],
    *,
    intent: str,
    sub_intents: tuple[str, ...],
    tier: str,
) -> None:
    """校验记忆摘要与草稿关联逐项一致；损坏摘要不得静默复用。"""
    if (
        not isinstance(memory, dict)
        or memory.get("intent") != intent
        or tuple(str(item) for item in memory.get("sub_intents", ())) != sub_intents
        or memory.get("policy_tier") != tier
        or draft.get("intent") != intent
        or tuple(str(item) for item in draft.get("sub_intents", ())) != sub_intents
        or draft.get("policy_tier") != tier
    ):
        raise DecisionRunError(
            "invalid_decision", submitted=False, attempts=0, submissions=0
        )


def _commit_checkpoint(
    store: RuntimeStorePort,
    batch_id: str,
    cursor: BatchCursor,
    *,
    now: str,
) -> dict[str, object]:
    """提交游标与检查点；失败向上传播，调用方不得继续取任务。"""
    return store.save_checkpoint(batch_id, cursor, now=now)

def _monotonic_default() -> int:
    """返回进程单调时钟毫秒，供实际耗时与超支计算。"""
    return int(time.monotonic() * 1000)


def _spend(
    cursor: BatchCursor,
    *,
    seconds: int = 0,
    device_calls: int = 0,
    limits: BudgetLimits,
    overruns: list[dict[str, object]],
) -> BatchCursor:
    """扣除真实耗时/设备调用并保留负数超支证据，不静默钳零。"""
    budget = cursor.budget
    if seconds:
        budget = budget.spend_seconds(seconds)
    if device_calls:
        budget = budget.spend_device_calls(device_calls)
    updated = replace(cursor, budget=budget)
    for name in budget.overrun(limits):
        overruns.append(
            {
                "kind": name,
                "remaining": getattr(budget, f"remaining_{name}")
                if hasattr(budget, f"remaining_{name}")
                else None,
            }
        )
    return updated


class _LeaseCommandContext:
    """在每条真实命令前后复核 Lease 并按 20 秒保存最新续租结果。"""

    def __init__(
        self,
        *,
        store: RuntimeStorePort,
        gate: SharedDeviceGatePort,
        clock: Callable[[], str],
        monotonic_ms: Callable[[], int],
        lease: Lease,
    ) -> None:
        """保存当前 Lease 与可信时钟。"""
        self._store = store
        self._gate = gate
        self._clock = clock
        self._monotonic = monotonic_ms
        self.lease = lease
        self._last_renew_mono = monotonic_ms()

    def before(self) -> None:
        """命令启动前复核共享门禁与当前任务完整 Lease/nonce。"""
        try:
            self._gate.verify(now=self._clock())
            running = self._store.require_running(self.lease, now=self._clock())
        except StoreError as error:
            raise RuntimeCommandControlError(error.reason_code) from None
        except Exception:
            raise RuntimeCommandControlError("device_gate_mismatch") from None
        if not running:
            raise RuntimeCommandControlError("stale_lease")

    def after(self, *, failed: bool) -> None:
        """命令结束后按独立可信时钟检查 20 秒并保存最新 Lease。"""
        if failed:
            return
        current = self._monotonic()
        if current - self._last_renew_mono < 20_000:
            return
        try:
            self.lease = self._store.renew_job(self.lease, self._clock())
        except StoreError as error:
            raise RuntimeCommandControlError(error.reason_code) from None
        self._last_renew_mono = current


def _handle_job(
    *,
    store: RuntimeStorePort,
    read: RuntimeReadPort,
    batch_id: str,
    cursor: BatchCursor,
    step: SceneStep,
    task: TaskCandidate,
    primary: object,
    owner: Owner,
    monotonic_ms: Callable[[], int],
    clock: Callable[[], str],
    gate: SharedDeviceGatePort,
    limits: BudgetLimits,
    overruns: list[dict[str, object]],
    on_started: Callable[[], None] | None = None,
) -> tuple[BatchCursor, str | None, bool]:
    """认领/开始/收敛职位；必须由实际 M3 页面包含该 task.key。"""
    lease = store.claim_job(batch_id, owner, clock(), task.key)
    if lease is None:
        return cursor, None, False
    lease = store.start_job(lease, clock())
    if on_started is not None:
        on_started()
    started_mono = monotonic_ms()
    actionable, reason = (
        is_actionable(primary) if hasattr(primary, "status") else (False, "not_ok")
    )
    value = primary.value if getattr(primary, "status", None) == "ok" else None
    target_present = (
        isinstance(value, JobList)
        and actionable
        and any(card.job_key == task.key for card in value.cards)
    ) or (
        isinstance(value, JobDetail)
        and actionable
        and value.job_key == task.key
        and value.complete
    )
    if not actionable or not target_present:
        store.finish_semantic_block(lease, clock(), "identity_ambiguous")
        return cursor.visit_job(task.key), "semantic_block", True
    if step.job_fixture is not None:
        # 每条后续采集命令前复核共享门禁与当前完整任务/Lease 身份；nonce 变化即零启动。
        gate.verify(now=clock())
        if not store.require_running(lease, now=clock()):
            return cursor, "precondition_failed", True
        bind = getattr(read, "bind_command_context", None)
        context: _LeaseCommandContext | None = None
        if callable(bind):
            context = _LeaseCommandContext(
                store=store,
                gate=gate,
                clock=clock,
                monotonic_ms=monotonic_ms,
                lease=lease,
            )
            bind(context)
        try:
            detail = read.read(step.job_fixture)
        except RuntimeCommandControlError as error:
            reason = error.reason_code
            if reason not in PAUSE_REASONS:
                reason = "precondition_failed"
            return cursor, reason, True
        finally:
            if callable(bind):
                bind(None)
        if context is not None:
            lease = context.lease
        detail_ok, detail_reason = is_actionable(detail)
        detail_value = detail.value if getattr(detail, "status", None) == "ok" else None
        if isinstance(detail_value, JobList):
            detail_present = any(
                card.job_key == task.key for card in detail_value.cards
            )
        elif isinstance(detail_value, JobDetail):
            detail_present = detail_value.job_key == task.key and detail_value.complete
        else:
            detail_present = False
        if not detail_ok or not detail_present:
            store.finish_semantic_block(
                lease, clock(), detail_reason or "incomplete_snapshot"
            )
            return cursor.visit_job(task.key), "semantic_block", True
    elapsed = max(0, monotonic_ms() - started_mono)
    if elapsed >= 20_000:
        lease = store.renew_job(lease, clock())
    store.finish_job(lease, clock(), "done", None)
    if elapsed >= limits.job_seconds * 1000:
        overruns.append(
            {
                "kind": "job_seconds",
                "elapsed_ms": elapsed,
                "limit_seconds": limits.job_seconds,
            }
        )
        return cursor.visit_job(task.key), "budget_exhausted", True
    return cursor.visit_job(task.key), None, True


def _handle_discovery(
    *,
    store: RuntimeStorePort,
    read: RuntimeReadPort,
    task: TaskCandidate,
    step: SceneStep,
    primary: object,
    conversation: object | None,
    owner: Owner,
    cursor: BatchCursor,
    batch_id: str,
    pack: PolicyPack,
    base_files: dict[str, str],
    profile_store: ProfileStorePort,
    gate: SharedDeviceGatePort,
    profile_db_path: str,
    snapshot: DecisionSnapshotPort,
    bridge_factory: Callable[[object], DecisionBridgePort],
    provider_id: str,
    model_id: str,
    model_config_hash: str,
    clock: Callable[[], str],
    process_log: ProcessLog | None,
    acceptance: dict[str, object] | None = None,
    acceptance_recheck: Callable[[], dict[str, object] | None] | None = None,
    conversation_result: object | None = None,
    on_started: Callable[[], None] | None = None,
) -> tuple[BatchCursor, str | None, bool, dict[str, object] | None]:
    """认领发现，经 M3 门禁与唯一 decide 后收敛；未决不标 done。"""
    lease = store.claim(
        "boss", task.kind, owner, clock(), conversation_key=task.key
    )
    if lease is None:
        return cursor, None, False, None
    lease = store.start(lease, clock())
    if on_started is not None:
        on_started()
    cursor = replace(cursor, budget=cursor.budget.spend_candidate())
    actionable, reason = (
        is_actionable(primary)
        if hasattr(primary, "status")
        else (False, "not_ok")
    )
    value = primary.value if getattr(primary, "status", None) == "ok" else None
    target_present = False
    if task.kind == "new_message":
        target_present = (
            isinstance(conversation, Conversation)
            and conversation.conversation_key == task.key
        )
    elif isinstance(value, UnreadRead) and actionable:
        target_present = any(
            row.conversation_key == task.key and row.state == "present"
            for row in value.rows
        )
    if not actionable or not target_present:
        if task.kind == "new_message":
            store.finish_semantic_block(lease, clock(), "identity_ambiguous")
            return cursor, "semantic_block", True, None
        code = "unread_unverified"
        if getattr(primary, "status", None) == "failed":
            code = str(getattr(primary, "reason_code", "wrong_page"))
            store.finish(lease, clock(), "failed", code, primary)  # type: ignore[arg-type]
            return cursor, "precondition_failed", True, None
        store.finish_semantic_block(lease, clock(), "unread_unverified")
        return cursor, "unread_unverified", True, None
    if not isinstance(conversation, Conversation) or (
        conversation.conversation_key != task.key
    ):
        store.finish_semantic_block(lease, clock(), "unread_unverified")
        return cursor, "unread_unverified", True, None
    conv_ok = (
        conversation.read_complete
        and conversation.inbound_state == "known"
        and conversation.latest_inbound_key is not None
    )
    if not conv_ok:
        store.finish_semantic_block(lease, clock(), "unread_unverified")
        return cursor, "unread_unverified", True, None
    ledger: dict[str, object] | None = None
    needs_human = False
    if step.decision is not None:
        gate.verify(now=clock())
        lease = store.renew(lease, clock())
        try:
            outcome = _decision_for(
                store=store,
                batch_id=batch_id,
                lease=lease,
                task=task,
                conversation=conversation,
                primary=primary,
                step=step,
                pack=pack,
                base_files=base_files,
                profile_store=profile_store,
                gate=gate,
                profile_db_path=profile_db_path,
                snapshot=snapshot,
                bridge_factory=bridge_factory,
                provider_id=provider_id,
                model_id=model_id,
                model_config_hash=model_config_hash,
                now=clock(),
                clock=clock,
                process_log=process_log,
                acceptance=acceptance,
                acceptance_recheck=acceptance_recheck,
            )
            ledger = outcome
            needs_human = bool(outcome.get("needs_human"))
            if needs_human:
                store.finish_semantic_block(
                    lease, clock(), "human_review_required"
                )
                return cursor, "semantic_block", True, ledger
        except DecisionRunError as error:
            reason = error.reason_code
            if reason not in {
                "invalid_decision",
                "stale_decision",
                "policy_changed",
                "input_too_large",
                "decision_timeout",
            }:
                reason = "invalid_decision"
            store.finish_semantic_block(lease, clock(), reason)
            return cursor, "semantic_block", True, {
                "draft": None,
                "reused": False,
                "decision_reason": reason,
            }
    proof = (
        conversation_result
        if task.kind == "new_message" and conversation_result is not None
        else primary
    )
    store.finish(lease, clock(), "done", None, proof)  # type: ignore[arg-type]
    return cursor, None, True, ledger


def execute_run(
    *,
    base_files: dict[str, str],
    pack: PolicyPack,
    mode: str,
    confirm_device: bool,
    gate: SharedDeviceGatePort,
    profile_store: ProfileStorePort,
    profile_db_path: str,
    owner: Owner,
    now: str,
    scene: RuntimeScenePort,
    read: RuntimeReadPort,
    bridge_factory: Callable[[object], DecisionBridgePort],
    snapshot: DecisionSnapshotPort,
    open_store: Callable[[], RuntimeStorePort],
    limits: BudgetLimits | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    model_config_hash: str | None = None,
    eval_store: EvalStorePort | None = None,
    credential_rel: str | None = None,
    process_log: ProcessLog | None = None,
    clock: Callable[[], str] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    on_boundary: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """执行一次有界 run：先占共享门禁，再打开 profile 与仓储。

    `on_boundary` 只在固定生产边界被调用（after_checkpoint/after_refresh/
    before_safe_point/before_claim），返回值被忽略；它只能改变外界原始刺激、
    推进注入的可信时钟或记录轨迹，不能返回任务、安全点、决策或终态答案。
    """
    from app.domain.policy import utc_now as clock_impl

    if mode not in RUN_MODES:
        raise PolicyError("invalid_input", [_issue("mode", "vocabulary")])
    if mode == "auto_send_tier_a":
        raise PolicyError("authorization_required", [_issue("mode", "safety_floor")])
    if not confirm_device:
        raise PolicyError("authorization_required", [_issue("device", "confirmation")])
    acceptance = _acceptance(
        mode=mode,
        eval_store=eval_store,
        credential_rel=credential_rel,
        provider_id=provider_id,
        model_id=model_id,
        model_config_hash=model_config_hash,
        base_files=base_files,
        pack=pack,
    )
    limits = limits or BudgetLimits()
    active_clock = clock if clock is not None else clock_impl
    active_mono = monotonic_ms if monotonic_ms is not None else _monotonic_default

    def _notify(name: str) -> None:
        """调用固定边界观察者；忽略返回值，避免测试缝变成业务答案入口。"""
        if on_boundary is not None:
            on_boundary(name)

    def _recheck_acceptance() -> dict[str, object] | None:
        """缓存命中时重新读取当前验收，不沿用本轮起始快照。"""
        return _acceptance(
            mode=mode,
            eval_store=eval_store,
            credential_rel=credential_rel,
            provider_id=provider_id,
            model_id=model_id,
            model_config_hash=model_config_hash,
            base_files=base_files,
            pack=pack,
        )

    gate.acquire(
        profile_id=pack.profile_id,
        profile_db_path=profile_db_path,
        owner=owner,
        now=now,
    )
    store: RuntimeStorePort | None = None
    converged = False
    try:
        gate.verify(now=active_clock())
        open_profile_store(profile_store, process_log)
        pending = profile_store.pending_activity()
        if (
            pending["leased"]
            or pending["running"]
            or pending["blocked"]
            or pending["device_lease"]
            or pending.get("actions")
            or pending.get("jobs")
        ):
            raise PolicyError(
                "authorization_required", [_issue("activity", "unresolved")]
            )
        if mode == "read_only":
            step = scene.next_step()
            observed = None
            if step is not None:
                results, _primary, _conv, raw_results = _read_target(read, step)
                candidates = _candidates(results, hints=step.candidates if step else None, unread_unknown=False)
                safe = derive_safe_point(
                    store=_ReadOnlyStore(),
                    batch_id="00000000-0000-4000-8000-000000000000",
                    task_key=None,
                    read_results=raw_results,
                    raw=step.raw_scene,
                )
                observed = {
                    "unread": len(candidates.unread),
                    "new_messages": len(candidates.new_messages),
                    "jobs": len(candidates.jobs),
                    "safe_point": all(
                        getattr(safe, name) for name in SAFE_POINT_FIELDS[1:]
                    ),
                }
            converged = True
            return {
                "run": {
                    "mode": mode,
                    "read_only": True,
                    "observed": observed,
                    "business_writes": 0,
                    "navigation": 0,
                    "model_calls": 0,
                    "actions_performed": 0,
                },
                "acceptance": acceptance,
                "limitations": [
                    "fixture_m3_read_only",
                    "real_device_read_not_calibrated",
                    "real_provider_not_called",
                ],
            }
        store = open_store()
        batch_id = getattr(scene, "batch_id", None)
        if not isinstance(batch_id, str):
            raise PolicyError("invalid_input", [_issue("batch", "missing")])
        batch = store.ensure_batch(batch_id, limits, active_clock())
        if batch.get("state") in {"paused", "completed"}:
            batch = store.resume_batch(batch_id, active_clock())
        cursor = batch["cursor"]
        assert isinstance(cursor, BatchCursor)
        status = store.checkpoint_status(batch_id)
        if (
            status["last_checkpoint_id"] is None
            or int(status["checkpoint_events"]) == 0
        ):
            store.save_checkpoint(batch_id, cursor, now=active_clock())
        store.record_runtime_event(batch_id, "run_started", now=active_clock())
        wall_start_ms = _wall_ms(active_clock())
        mono_start = active_mono()
        last_kind: str | None = None
        last_task_key: str | None = None
        first_observed: PreemptionObservation | None = None
        previous_check_mono: int | None = None
        drafts: list[dict[str, object]] = []
        preemptions: list[dict[str, object]] = []
        overruns: list[dict[str, object]] = []
        processed = 0
        reused = 0
        pause_reason: str | None = None
        last_boundary = active_mono()
        pending_ms = 0

        def _account(elapsed_mono: int) -> None:
            """按累计毫秒扣整秒预算，保留亚秒余量，避免多次短耗时永久不计费。"""
            nonlocal cursor, pending_ms
            pending_ms += max(0, elapsed_mono)
            seconds = pending_ms // 1000
            if seconds:
                pending_ms -= seconds * 1000
                cursor = _spend(
                    cursor, seconds=seconds, limits=limits, overruns=overruns
                )

        while True:
            step = scene.next_step()
            if step is None:
                break
            gate.verify(now=active_clock())
            now_value = active_clock()
            current_mono = active_mono()
            if clock_drift_exceeded(
                wall_start_ms=wall_start_ms,
                wall_now_ms=_wall_ms(now_value),
                monotonic_start_ms=mono_start,
                monotonic_now_ms=current_mono,
            ):
                pause_reason = "clock_changed"
                store.safety_stop_after_clock_change(
                    batch_id, cursor, pause_reason, now=now_value
                )
                break
            _account(current_mono - last_boundary)
            last_boundary = current_mono
            if not cursor.budget.can_start_work():
                pause_reason = "budget_exhausted"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            results, primary, conversation, raw_results = _read_target(read, step)
            read_mono = active_mono()
            _account(read_mono - last_boundary)
            last_boundary = read_mono
            now_value = active_clock()
            current_mono = read_mono
            # 真实观察发生在本次原始采集/解析完成之后；after_refresh 注入不得倒灌。
            observed_at = now_value
            observed_mono = read_mono
            calls = (
                read.take_device_calls()
                if hasattr(read, "take_device_calls")
                else 0
            )
            cursor = _spend(
                cursor,
                device_calls=calls,
                limits=limits,
                overruns=overruns,
            )
            if not cursor.budget.can_start_work():
                pause_reason = "budget_exhausted"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            candidates = _candidates(
                results,
                hints=step.candidates,
                unread_unknown=any(
                    getattr(item, "status", None) in {"failed", "undetermined"}
                    for item in results
                ),
                queued_new_messages=(
                    store.queued_new_message_keys()
                    if hasattr(store, "queued_new_message_keys")
                    else ()
                ),
            )
            _notify("after_refresh")
            if step.in_progress:
                if candidates.unread and last_kind == "job":
                    first_observed = observe_unread(
                        first_observed,
                        wall_now=observed_at,
                        monotonic_ms=observed_mono,
                    )
                    cursor = cursor.with_first_observed(observed_at)
                previous_check_mono = observed_mono
                continue
            selection = select_task(
                candidates,
                switching_from_job=last_kind == "job",
                preemptions_used=cursor.preemptions_used,
                preemption_limit=limits.preemptions,
                unread_verification_used=False,
                unread_verified_unknown=False,
            )
            if selection.verify_unread:
                outcome = "missing_capability"
                calls = 0
                elapsed_ms = 0
                target_result = None
                initial_unread = next(
                    (
                        item
                        for item in raw_results
                        if isinstance(item, ReadResult)
                        and isinstance(item.value, UnreadRead)
                    ),
                    None,
                )
                target_fixture = step.targeted_read_fixture
                targeted_before = active_mono()
                now_value = active_clock()
                if clock_drift_exceeded(
                    wall_start_ms=wall_start_ms,
                    wall_now_ms=_wall_ms(now_value),
                    monotonic_start_ms=mono_start,
                    monotonic_now_ms=targeted_before,
                ):
                    pause_reason = "clock_changed"
                    store.safety_stop_after_clock_change(
                        batch_id, cursor, pause_reason, now=now_value
                    )
                    break
                gate.verify(now=active_clock())
                _account(targeted_before - last_boundary)
                last_boundary = targeted_before
                current_mono = targeted_before
                if not cursor.budget.can_start_work():
                    pause_reason = "budget_exhausted"
                    store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                    break
                if process_log is not None:
                    process_log.record(stage="run", operation="unread", result="started")
                if target_fixture is not None and initial_unread is not None:
                    mono_before = targeted_before
                    try:
                        target_result = read.read(target_fixture)
                    except Exception:
                        target_result = None
                    mono_after = active_mono()
                    elapsed_ms = max(0, mono_after - mono_before)
                    calls = (
                        read.take_device_calls()
                        if hasattr(read, "take_device_calls")
                        else 0
                    )
                    _account(elapsed_ms)
                    last_boundary = mono_after
                    now_value = active_clock()
                    current_mono = mono_after
                    cursor = _spend(
                        cursor,
                        device_calls=calls,
                        limits=limits,
                        overruns=overruns,
                    )
                    if target_result is None or target_result.status != "ok":
                        outcome = (
                            target_result.reason_code
                            if target_result is not None
                            and target_result.reason_code is not None
                            else "read_failed"
                        )
                    else:
                        try:
                            outcome = (
                                targeted_verification_outcome(
                                    initial_unread,
                                    target_result,
                                    [],
                                    now=active_clock(),
                                )
                                or "cleared"
                            )
                        except ValueError:
                            outcome = "invalid_read"
                terminal = "ok" if outcome == "cleared" else "undetermined"
                if target_result is not None and target_result.status == "failed":
                    terminal = "failed"
                if process_log is not None:
                    process_log.record(
                        stage="run",
                        operation="unread",
                        result=terminal,
                        reason_code=(
                            outcome if outcome in _TARGETED_LOG_REASONS else None
                        ),
                        error_category=None if terminal == "ok" else "read",
                        duration_ms=elapsed_ms,
                        device_count=calls,
                    )
                store.record_runtime_event(
                    batch_id,
                    "unread_verified",
                    now=active_clock(),
                    payload={
                        "outcome": outcome,
                        "device_calls": calls,
                        "elapsed_ms": elapsed_ms,
                    },
                )
                if not cursor.budget.can_start_work():
                    pause_reason = "budget_exhausted"
                    store.pause_batch(batch_id, cursor, pause_reason, now=active_clock())
                    break
                if outcome != "cleared":
                    pause_reason = "unread_unverified"
                    store.pause_batch(batch_id, cursor, pause_reason, now=active_clock())
                    break
                target_value = (
                    target_result.value
                    if target_result is not None and target_result.status == "ok"
                    else None
                )
                if not isinstance(target_value, UnreadRead):
                    pause_reason = "unread_unverified"
                    store.pause_batch(batch_id, cursor, pause_reason, now=active_clock())
                    break
                candidates = replace(
                    candidates,
                    unread=_targeted_unread_candidates(target_value),
                    unread_unknown=False,
                    unread_signal_without_target=False,
                )
                # 定向采集首次带来可见未读时，首次观察取本次采集完成边界；
                # 既有真实正观察由 observe_unread 保留，不被无条件覆盖。
                if candidates.unread and last_kind == "job":
                    first_observed = observe_unread(
                        first_observed,
                        wall_now=now_value,
                        monotonic_ms=current_mono,
                    )
                    cursor = cursor.with_first_observed(now_value)
                observed_at = now_value
                observed_mono = current_mono
                # 安全点与后续处理只消费刚发生的真实定向采集，不用被替换前的旧读取。
                raw_results = tuple(
                    target_result if item is initial_unread else item
                    for item in raw_results
                )
                primary = target_result
                selection = select_task(
                    candidates,
                    switching_from_job=last_kind == "job",
                    preemptions_used=cursor.preemptions_used,
                    preemption_limit=limits.preemptions,
                    unread_verification_used=True,
                    unread_verified_unknown=False,
                )
            if selection.pause_reason is not None:
                pause_reason = selection.pause_reason
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            task = selection.task
            if task is None:
                break
            if task.kind == "job":
                ordered = tuple(candidate.key for candidate in candidates.jobs)
                chosen = cursor.next_job(ordered)
                if chosen is None:
                    # 当前可见职位都已在真实游标中访问过；等待下一次刷新或结束。
                    previous_check_mono = observed_mono
                    continue
                if chosen != task.key:
                    task = TaskCandidate("job", chosen)
            _notify("before_safe_point")
            safe = derive_safe_point(
                store=store,
                batch_id=batch_id,
                task_key=last_task_key,
                read_results=raw_results,
                raw=step.raw_scene,
            )
            if not safe.is_safe():
                pause_reason = "precondition_failed"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            switching_unread = bool(candidates.unread) and last_kind == "job"
            if switching_unread and cursor.budget.remaining_preemptions <= 0:
                pause_reason = "preemption_limit"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            if task.kind == "unread" and last_kind == "job":
                first_observed = observe_unread(
                    first_observed,
                    wall_now=observed_at,
                    monotonic_ms=observed_mono,
                )
                cursor = cursor.with_first_observed(observed_at)
            store.enqueue_jobs(
                batch_id,
                tuple(candidate.key for candidate in candidates.jobs),
                active_clock(),
            )
            _notify("before_claim")
            gate.verify(now=active_clock())
            now_value = active_clock()
            claim_mono = active_mono()
            if clock_drift_exceeded(
                wall_start_ms=wall_start_ms,
                wall_now_ms=_wall_ms(now_value),
                monotonic_start_ms=mono_start,
                monotonic_now_ms=claim_mono,
            ):
                pause_reason = "clock_changed"
                store.safety_stop_after_clock_change(
                    batch_id, cursor, pause_reason, now=now_value
                )
                break
            _account(claim_mono - last_boundary)
            last_boundary = claim_mono
            if not cursor.budget.can_start_work():
                pause_reason = "budget_exhausted"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            task_ledger: dict[str, object] | None = None
            observed_start: dict[str, object] = {}

            def _capture_start() -> None:
                """在真实 claim/start 成功后立即取样，避免使用完成后的旧时刻。"""
                observed_start["at"] = active_clock()
                observed_start["mono"] = active_mono()

            conversation_result = (
                next(
                    (
                        item
                        for item in raw_results
                        if getattr(item, "value", None) is conversation
                    ),
                    None,
                )
                if conversation is not None
                else None
            )
            if task.kind == "job":
                cursor, task_pause, started = _handle_job(
                    store=store,
                    read=read,
                    batch_id=batch_id,
                    cursor=cursor,
                    step=step,
                    task=task,
                    primary=primary,
                    owner=owner,
                    monotonic_ms=active_mono,
                    clock=active_clock,
                    gate=gate,
                    limits=limits,
                    overruns=overruns,
                    on_started=_capture_start,
                )
            else:
                cursor, task_pause, started, task_ledger = _handle_discovery(
                    store=store,
                    read=read,
                    task=task,
                    step=step,
                    primary=primary,
                    conversation=conversation,
                    owner=owner,
                    cursor=cursor,
                    batch_id=batch_id,
                    pack=pack,
                    base_files=base_files,
                    profile_store=profile_store,
                    gate=gate,
                    profile_db_path=profile_db_path,
                    snapshot=snapshot,
                    bridge_factory=bridge_factory,
                    provider_id=provider_id or "",
                    model_id=model_id or "",
                    model_config_hash=model_config_hash or "",
                    clock=active_clock,
                    process_log=process_log,
                    acceptance=acceptance,
                    acceptance_recheck=_recheck_acceptance,
                    conversation_result=conversation_result,
                    on_started=_capture_start,
                )
                if task_ledger is not None:
                    if task_ledger.get("reused"):
                        reused += 1
                    elif task_ledger.get("draft") is not None:
                        drafts.append(task_ledger["draft"])
            started_mono = observed_start.get("mono")
            if started and type(started_mono) is int:
                _account(started_mono - last_boundary)
                last_boundary = started_mono
            post_calls = (
                read.take_device_calls()
                if hasattr(read, "take_device_calls")
                else 0
            )
            cursor = _spend(
                cursor,
                device_calls=post_calls,
                limits=limits,
                overruns=overruns,
            )
            end_mono = active_mono()
            _account(end_mono - last_boundary)
            last_boundary = end_mono
            if started:
                processed += 1
                last_task_key = task.key
                if task.kind == "unread" and last_kind == "job":
                    assert first_observed is not None
                    start_at = observed_start.get("at")
                    if not isinstance(start_at, str) or type(started_mono) is not int:
                        pause_reason = "precondition_failed"
                        store.pause_batch(
                            batch_id, cursor, pause_reason, now=active_clock()
                        )
                        break
                    event = build_stage_preempted(
                        job_key=_last_job_key(cursor),
                        trigger="unread_count>0",
                        first=first_observed,
                        handling_started_at=start_at,
                        handling_started_monotonic_ms=started_mono,
                    )
                    if previous_check_mono is not None:
                        event["detection_ms"] = detection_interval_ms(
                            previous_check_mono, current_mono
                        )
                    store.record_runtime_event(
                        batch_id,
                        "stage_preempted",
                        now=active_clock(),
                        payload=event,
                    )
                    cursor = cursor.record_preemption()
                    preemptions.append(event)
                    first_observed = None
                store.save_checkpoint(batch_id, cursor, now=active_clock())
                _notify("after_checkpoint")
            previous_check_mono = current_mono
            last_kind = task.kind
            if task_pause is not None:
                pause_reason = task_pause
                store.pause_batch(batch_id, cursor, pause_reason, now=active_clock())
                break
        if pause_reason != "clock_changed":
            store.record_runtime_event(
                batch_id,
                "run_finished",
                now=active_clock(),
                reason_code=pause_reason,
                payload={"processed": processed, "preemptions": len(preemptions)},
            )
            if pause_reason is None:
                store.complete_batch(batch_id, cursor, active_clock())
        converged = pause_reason in {
            None,
            "budget_exhausted",
            "preemption_limit",
            "unread_unverified",
            "semantic_block",
        }
        return {
            "run": {
                "mode": mode,
                "batch_id": batch_id,
                "processed": processed,
                "pause_reason": pause_reason,
                "drafts": drafts,
                "decisions_reused": reused,
                "preemptions": preemptions,
                "overruns": overruns,
                "remaining_budget": cursor.budget.to_json(),
                "preemptions_used": cursor.preemptions_used,
                "actions_performed": 0,
                "model_calls": len(drafts),
                "business_writes": processed,
            },
            "acceptance": acceptance,
            "limitations": [
                "fixture_m3_composition",
                "real_device_read_not_calibrated",
                "real_provider_not_called",
                "apply_quota_contract_not_defined",
            ],
        }
    finally:
        close_error: Exception | None = None
        if store is not None:
            try:
                store.close()  # type: ignore[attr-defined]
            except Exception as error:  # pragma: no cover - defensive close
                close_error = error
        try:
            profile_store.close()
        except Exception as error:  # pragma: no cover - defensive close
            close_error = close_error or error
        try:
            gate.release(now=active_clock(), converged=converged and close_error is None)
        except Exception:
            if close_error is not None:
                raise close_error
            raise


class _ReadOnlyStore:
    """read_only 的只读安全点证据；不写业务表、不创建批次。"""

    def pending_actions(self, app: str = "boss") -> list[dict[str, object]]:
        """只读模式下没有可用业务仓储，保守报告无 pending。"""
        del app
        return []

    def checkpoint_status(self, batch_id: str) -> dict[str, object]:
        """read_only 不创建检查点，因此 checkpoint 条件为 false。"""
        del batch_id
        return {"last_checkpoint_id": None, "checkpoint_events": 0}

    def last_action_state(self, task_key: str) -> str | None:
        """read_only 不读取业务动作表，保守报告无最近动作。"""
        del task_key
        return None


def _wall_ms(value: str) -> int:
    """把规范 UTC 时间换算为毫秒，供双时钟漂移比较。"""
    return int(parse_utc(value).timestamp() * 1000)


def _last_job_key(cursor: BatchCursor) -> str:
    """返回最近一次已处理的职位键；抢占事件必须绑定真实职位。"""
    if cursor.last_job_dedupe_key is None:
        raise PolicyError("invalid_input", [_issue("job", "missing")])
    return cursor.last_job_dedupe_key
