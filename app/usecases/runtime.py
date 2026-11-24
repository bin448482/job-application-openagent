"""编排 M6 实际运行：共享门禁整轮持有、M3 读取门禁、唯一决策与安全点调度。"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable

from app.adapters.sqlite.session import ProfileSessionLease
from app.domain.models import Lease, Owner, canonical_hash, parse_utc
from app.domain.policy import (
    PolicyPack,
    build_envelope,
    compute_base_hash,
    effective_limits,
    render_request,
)
from app.domain.profile import select_profile_facts
from app.domain.reading import Conversation, JobDetail, JobList, PageSnapshot, UnreadRead
from app.domain.runtime import (
    BatchCursor,
    BudgetLimits,
    PreemptionObservation,
    SafePoint,
    SceneCandidates,
    Selection,
    TaskCandidate,
    build_stage_preempted,
    clock_drift_exceeded,
    detection_interval_ms,
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
    RuntimeReadPort,
    RuntimeScenePort,
    RuntimeStorePort,
    SceneStep,
)
from app.process_log import ProcessLog
from app.profile_open import open_profile_store
from app.usecases.acceptance import check_acceptance
from app.usecases.decision import DecisionRunError, decide
from app.usecases.read import is_actionable

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


def _safe_scene(
    *,
    store: RuntimeStorePort,
    batch_id: str,
    task_key: str,
    read_results: tuple[object, ...],
    raw: RawScene,
) -> tuple[object, ...]:
    """从真实读取类型、仓储检查点/pending 与原始现场派生七条件。"""
    del task_key
    page_ok = any(
        (
            isinstance(value, UnreadRead)
            or isinstance(value, JobList)
            or isinstance(value, JobDetail)
            or isinstance(value, PageSnapshot)
        )
        and getattr(value, "coverage", "viewport") in {"viewport", "all"}
        for value in read_results
    )
    pending = store.pending_actions("boss")
    checkpoint = store.checkpoint_status(batch_id)
    safe = (
        page_ok,
        raw.input_text == "",
        raw.confirmation_title is None,
        raw.inflight_commands == 0,
        not pending,
        not pending,
        checkpoint["last_checkpoint_id"] is not None
        and int(checkpoint["checkpoint_events"]) > 0,
    )
    return safe


def _candidates(
    read_results: tuple[object, ...],
    *,
    hints: SceneCandidates | None,
    unread_unknown: bool,
) -> SceneCandidates:
    """从实际 M3 值类型派生候选，并保留显式外部提示；缺失不当零。"""
    unread: list[TaskCandidate] = []
    jobs: list[TaskCandidate] = []
    new_messages: list[TaskCandidate] = list(hints.new_messages) if hints else []
    cleanup: list[TaskCandidate] = list(hints.cleanup) if hints else []
    seen_unread: set[str] = set()
    seen_jobs: set[str] = set()
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
    if hints is not None:
        for candidate in hints.unread:
            if candidate.key not in seen_unread:
                unread.append(candidate)
                seen_unread.add(candidate.key)
        for candidate in hints.jobs:
            if candidate.key not in seen_jobs:
                jobs.append(candidate)
                seen_jobs.add(candidate.key)
    signal_without_target = any(
        isinstance(value, UnreadRead)
        and value.unread_state == "present"
        and not any(
            row.conversation_key is not None and row.state == "present"
            for row in value.rows
        )
        for value in read_results
    )
    return SceneCandidates(
        unread=tuple(unread),
        new_messages=tuple(new_messages),
        jobs=tuple(jobs),
        cleanup=tuple(cleanup),
        unread_unknown=unread_unknown
        or bool(hints.unread_unknown if hints else False),
        unread_signal_without_target=signal_without_target
        or bool(hints.unread_signal_without_target if hints else False),
    )


def _read_target(
    read: RuntimeReadPort, step: SceneStep
) -> tuple[tuple[object, ...], object, object | None]:
    """读取可见页面与可选会话；失败结果保留给门禁判断。"""
    results: list[object] = []
    primary = read.read(step.read_fixture)
    results.append(primary.value if primary.status == "ok" else primary)
    conversation = None
    if step.conversation_fixture is not None:
        conv_result = read.read(step.conversation_fixture)
        if conv_result.status == "ok":
            conversation = conv_result.value
            results.append(conversation)
        else:
            results.append(conv_result)
    return tuple(results), primary, conversation


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
    input_hash = canonical_hash(
        {"prompt_hash": rendered.prompt_hash, "facts_hash": facts_hash}
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
            now=now,
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
        now=now,
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
        now=now,
    )
    return {
        "draft": saved,
        "reused": False,
        "needs_human": tier == "C" and intent in {"needs_human", "uncertain"},
    }


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


def _handle_job(
    *,
    store: RuntimeStorePort,
    read: RuntimeReadPort,
    batch_id: str,
    cursor: BatchCursor,
    step: SceneStep,
    task: TaskCandidate,
    owner: Owner,
    now_value: str,
    monotonic_ms: Callable[[], int],
    limits: BudgetLimits,
    overruns: list[dict[str, object]],
) -> tuple[BatchCursor, str | None, bool]:
    """认领/开始/收敛职位；只在实际 start 后计 processed。"""
    lease = store.claim_job(batch_id, owner, now_value, task.key)
    if lease is None:
        return cursor, None, False
    lease = store.start_job(lease, now_value)
    started_mono = monotonic_ms()
    if step.job_fixture is not None:
        result = read.read(step.job_fixture)
        actionable, reason = is_actionable(result)
        if not actionable or not isinstance(result.value, (JobList, JobDetail)):
            store.finish_job(
                lease, now_value, "failed", reason or "incomplete_snapshot"
            )
            return cursor.visit_job(task.key), "precondition_failed", True
        if isinstance(result.value, JobDetail) and result.value.job_key != task.key:
            store.finish_job(lease, now_value, "failed", "identity_ambiguous")
            return cursor.visit_job(task.key), "precondition_failed", True
    elapsed = max(0, monotonic_ms() - started_mono)
    if elapsed >= 20_000:
        lease = store.renew_job(lease, now_value)
    store.finish_job(lease, now_value, "done", None)
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
    now_value: str,
    clock: Callable[[], str],
    process_log: ProcessLog | None,
) -> tuple[BatchCursor, str | None, bool, dict[str, object] | None]:
    """认领发现，经 M3 门禁与唯一 decide 后收敛；未决不标 done。"""
    lease = store.claim(
        "boss", task.kind, owner, now_value, conversation_key=task.key
    )
    if lease is None:
        return cursor, None, False, None
    lease = store.start(lease, now_value)
    cursor = replace(cursor, budget=cursor.budget.spend_candidate())
    actionable, reason = (
        is_actionable(primary)
        if hasattr(primary, "status")
        else (False, "not_ok")
    )
    value = primary.value if getattr(primary, "status", None) == "ok" else None
    target_present = False
    if isinstance(value, UnreadRead) and actionable:
        target_present = any(
            row.conversation_key == task.key and row.state == "present"
            for row in value.rows
        )
    if not actionable or not target_present:
        code = "unread_unverified"
        if getattr(primary, "status", None) == "failed":
            code = str(getattr(primary, "reason_code", "wrong_page"))
            store.finish(lease, now_value, "failed", code, primary)  # type: ignore[arg-type]
            return cursor, "precondition_failed", True, None
        store.finish_semantic_block(lease, now_value, "unread_unverified")
        return cursor, "unread_unverified", True, None
    if not isinstance(conversation, Conversation) or (
        conversation.conversation_key != task.key
    ):
        store.finish_semantic_block(lease, now_value, "unread_unverified")
        return cursor, "unread_unverified", True, None
    conv_ok = (
        conversation.read_complete
        and conversation.inbound_state == "known"
        and conversation.latest_inbound_key is not None
    )
    if not conv_ok:
        store.finish_semantic_block(lease, now_value, "unread_unverified")
        return cursor, "unread_unverified", True, None
    ledger: dict[str, object] | None = None
    needs_human = False
    if step.decision is not None:
        gate.verify(now=now_value)
        lease = store.renew(lease, now_value)
        try:
            outcome = _decision_for(
                store=store,
                batch_id=batch_id,
                lease=lease,
                task=task,
                conversation=conversation,
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
                now=now_value,
                clock=clock,
                process_log=process_log,
            )
            ledger = outcome
            needs_human = bool(outcome.get("needs_human"))
            if needs_human:
                store.finish_semantic_block(
                    lease, now_value, "human_review_required"
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
            store.finish_semantic_block(lease, now_value, reason)
            return cursor, "semantic_block", True, {
                "draft": None,
                "reused": False,
                "decision_reason": reason,
            }
    store.finish(lease, now_value, "done", None, primary)  # type: ignore[arg-type]
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
) -> dict[str, object]:
    """执行一次有界 run：先占共享门禁，再打开 profile 与仓储。"""
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
                results, _primary, _conv = _read_target(read, step)
                candidates = _candidates(results, hints=step.candidates if step else None, unread_unknown=False)
                safe = _safe_scene(
                    store=_ReadOnlyStore(),
                    batch_id="00000000-0000-4000-8000-000000000000",
                    task_key="",
                    read_results=results,
                    raw=step.raw_scene,
                )
                observed = {
                    "unread": len(candidates.unread),
                    "new_messages": len(candidates.new_messages),
                    "jobs": len(candidates.jobs),
                    "safe_point": all(safe[1:]),
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
        if batch.get("state") == "paused":
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
        first_observed: PreemptionObservation | None = None
        previous_check_mono: int | None = None
        drafts: list[dict[str, object]] = []
        preemptions: list[dict[str, object]] = []
        overruns: list[dict[str, object]] = []
        processed = 0
        reused = 0
        pause_reason: str | None = None
        last_boundary = active_mono()
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
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            elapsed_seconds = max(0, (current_mono - last_boundary) // 1000)
            cursor = _spend(
                cursor,
                seconds=elapsed_seconds,
                limits=limits,
                overruns=overruns,
            )
            last_boundary = current_mono
            if not cursor.budget.can_start_work():
                pause_reason = "budget_exhausted"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            results, primary, conversation = _read_target(read, step)
            cursor = _spend(
                cursor,
                device_calls=step.device_calls,
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
            )
            if step.in_progress:
                if candidates.unread and last_kind == "job":
                    first_observed = observe_unread(
                        first_observed,
                        wall_now=now_value,
                        monotonic_ms=step.monotonic_ms,
                    )
                    cursor = cursor.with_first_observed(now_value)
                previous_check_mono = step.monotonic_ms
                continue
            safe_tuple = _safe_scene(
                store=store,
                batch_id=batch_id,
                task_key="",
                read_results=results,
                raw=step.raw_scene,
            )
            safe = SafePoint(*safe_tuple)  # type: ignore[arg-type]
            if not safe.is_safe():
                pause_reason = "precondition_failed"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            switching_unread = bool(candidates.unread) and last_kind == "job"
            if switching_unread and cursor.budget.remaining_preemptions <= 0:
                pause_reason = "preemption_limit"
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
            selection = select_task(
                candidates,
                switching_from_job=last_kind == "job",
                preemptions_used=cursor.preemptions_used,
                preemption_limit=limits.preemptions,
                unread_verification_used=False,
                unread_verified_unknown=False,
            )
            if selection.verify_unread:
                store.record_runtime_event(
                    batch_id,
                    "unread_verified",
                    now=now_value,
                    payload={"result": step.unread_verification},
                )
                if step.unread_verification != "cleared":
                    pause_reason = "unread_unverified"
                    store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                    break
                candidates = replace(
                    candidates,
                    unread_unknown=False,
                    unread_signal_without_target=False,
                )
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
            if task.kind == "unread" and last_kind == "job":
                first_observed = observe_unread(
                    first_observed,
                    wall_now=now_value,
                    monotonic_ms=step.monotonic_ms,
                )
                cursor = cursor.with_first_observed(now_value)
            store.enqueue_jobs(
                batch_id,
                tuple(candidate.key for candidate in candidates.jobs),
                now_value,
            )
            gate.verify(now=active_clock())
            task_ledger: dict[str, object] | None = None
            if task.kind == "job":
                cursor, task_pause, started = _handle_job(
                    store=store,
                    read=read,
                    batch_id=batch_id,
                    cursor=cursor,
                    step=step,
                    task=task,
                    owner=owner,
                    now_value=now_value,
                    monotonic_ms=active_mono,
                    limits=limits,
                    overruns=overruns,
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
                    now_value=now_value,
                    clock=active_clock,
                    process_log=process_log,
                )
                if task_ledger is not None:
                    if task_ledger.get("reused"):
                        reused += 1
                    elif task_ledger.get("draft") is not None:
                        drafts.append(task_ledger["draft"])
            if started:
                processed += 1
                if (
                    task.kind == "unread"
                    and last_kind == "job"
                ):
                    assert first_observed is not None
                    event = build_stage_preempted(
                        job_key=_last_job_key(cursor),
                        trigger="unread_count>0",
                        first=first_observed,
                        handling_started_at=now_value,
                        handling_started_monotonic_ms=step.monotonic_ms,
                    )
                    if previous_check_mono is not None:
                        event["detection_ms"] = detection_interval_ms(
                            previous_check_mono, step.monotonic_ms
                        )
                    store.record_runtime_event(
                        batch_id,
                        "stage_preempted",
                        now=now_value,
                        payload=event,
                    )
                    cursor = cursor.record_preemption()
                    preemptions.append(event)
                    first_observed = None
                store.save_checkpoint(batch_id, cursor, now=active_clock())
            previous_check_mono = step.monotonic_ms
            last_kind = task.kind
            if task_pause is not None:
                pause_reason = task_pause
                store.pause_batch(batch_id, cursor, pause_reason, now=now_value)
                break
        store.record_runtime_event(
            batch_id,
            "run_finished",
            now=active_clock(),
            reason_code=pause_reason,
            payload={"processed": processed, "preemptions": len(preemptions)},
        )
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


def _wall_ms(value: str) -> int:
    """把规范 UTC 时间换算为毫秒，供双时钟漂移比较。"""
    return int(parse_utc(value).timestamp() * 1000)


def _last_job_key(cursor: BatchCursor) -> str:
    """返回最近一次已处理的职位键；抢占事件必须绑定真实职位。"""
    if cursor.last_job_dedupe_key is None:
        raise PolicyError("invalid_input", [_issue("job", "missing")])
    return cursor.last_job_dedupe_key
