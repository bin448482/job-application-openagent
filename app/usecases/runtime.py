"""编排 M6 实际运行：共享门禁整轮持有、安全点调度、批次预算、草稿与决策记忆。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from app.domain.models import Lease, Owner, ReadResult, canonical_hash
from app.domain.policy import PolicyPack, compute_base_hash
from app.domain.profile import select_profile_facts
from app.domain.runtime import (
    BatchCursor,
    BudgetLimits,
    PreemptionObservation,
    SceneCandidates,
    Selection,
    TaskCandidate,
    build_stage_preempted,
    clock_drift_exceeded,
    detection_interval_ms,
    observe_unread,
    select_task,
)
from app.ports.device_gate import DeviceGateError, SharedDeviceGatePort
from app.ports.eval import EvalStorePort
from app.ports.policy import PolicyError, PolicyIssue
from app.ports.profile import ProfileStorePort
from app.ports.runtime import (
    RuntimeDecisionPort,
    RuntimeScenePort,
    RuntimeStorePort,
    SceneStep,
)
from app.process_log import ProcessLog
from app.profile_open import open_profile_store
from app.usecases.acceptance import check_acceptance

RUN_MODES = ("read_only", "draft_only", "auto_send_tier_a")


def _issue(field: str, code: str) -> PolicyIssue:
    """构造固定运行诊断，不携带事实值、键或路径。"""
    return PolicyIssue(file=None, field=field, line=None, code=code)


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


def _facts_hash(profile_store: ProfileStorePort, pack: PolicyPack, *, now: str) -> str:
    """对当前允许事实（含值与有效期）计算摘要；撤销/到期会改变摘要。"""
    rows = profile_store.active_fact_rows()
    selected = select_profile_facts(rows, pack, now=now)
    return canonical_hash(selected)


def _input_hash(task: TaskCandidate, decision_evidence: str, facts_hash: str) -> str:
    """绑定任务、可见输入证据与允许事实，避免只绑定消息文本。"""
    return canonical_hash(
        {
            "task_key": task.key,
            "task_kind": task.kind,
            "evidence_hash": decision_evidence,
            "facts_hash": facts_hash,
        }
    )


def _scene_without_unread_unknown(scene: SceneCandidates) -> SceneCandidates:
    """一次定向核对明确无未读后，清掉未知标志再按同优先级选择。"""
    return SceneCandidates(
        unread=scene.unread,
        new_messages=scene.new_messages,
        jobs=scene.jobs,
        cleanup=scene.cleanup,
        unread_unknown=False,
        unread_signal_without_target=False,
    )


def _resume_jobs(cursor: BatchCursor, scene: SceneCandidates) -> tuple[TaskCandidate, ...]:
    """按身份游标恢复职位顺序；已访问/消失跳过，不按下标点击。"""
    keys = tuple(candidate.key for candidate in scene.jobs)
    if cursor.last_job_dedupe_key in keys:
        index = keys.index(cursor.last_job_dedupe_key)
        ordered = keys[index + 1 :] + keys[: index + 1]
    else:
        ordered = keys
    visited = set(cursor.visited_keys)
    return tuple(
        TaskCandidate("job", key) for key in ordered if key not in visited
    )


def _selection(
    scene: SceneCandidates,
    *,
    last_kind: str | None,
    preemptions_used: int,
    limits: BudgetLimits,
) -> Selection:
    """用唯一纯函数按当前场景重算优先级。"""
    return select_task(
        scene,
        switching_from_job=last_kind == "job",
        preemptions_used=preemptions_used,
        preemption_limit=limits.preemptions,
        unread_verification_used=False,
        unread_verified_unknown=False,
    )


def _step_costs(cursor: BatchCursor, step: SceneStep) -> BatchCursor:
    """把本次刷新/检测耗时与设备调用记入持久预算。"""
    return replace(
        cursor,
        budget=cursor.budget.spend_seconds(step.elapsed_seconds).spend_device_calls(
            step.device_calls
        ),
    )


def _pause(
    store: RuntimeStorePort,
    batch_id: str,
    cursor: BatchCursor,
    reason: str,
    *,
    now: str,
    process_log: ProcessLog | None,
) -> None:
    """原子保存游标与暂停原因，并记固定运行日志。"""
    store.pause_batch(batch_id, cursor, reason, now=now)
    if process_log is not None:
        process_log.record(
            stage="run", operation="pause", result="undetermined", reason_code=reason
        )


def _handle_job(
    *,
    store: RuntimeStorePort,
    batch_id: str,
    cursor: BatchCursor,
    step: SceneStep,
    task: TaskCandidate,
    owner: Owner,
    now_value: str,
) -> tuple[BatchCursor, str | None, str | None]:
    """认领/开始/收敛一条职位任务，返回新游标与暂停原因。"""
    lease = store.claim_job(batch_id, owner, now_value, task.key)
    if lease is None or lease.key != task.key:
        return cursor, "precondition_failed", None
    store.start_job(lease, now_value)
    outcome = step.task_outcome
    if outcome == "done":
        store.finish_job(lease, now_value, "done", None)
        cursor = cursor.visit_job(lease.key)
        return cursor, None, lease.key
    if outcome == "failed":
        reason = step.reason_code or "wrong_page"
        store.finish_job(lease, now_value, "failed", reason)
        return cursor.visit_job(lease.key), "precondition_failed", lease.key
    reason = step.reason_code or "incomplete_snapshot"
    store.finish_job(lease, now_value, "blocked", reason)
    return cursor.visit_job(lease.key), "precondition_failed", lease.key


def _handle_discovery(
    *,
    store: RuntimeStorePort,
    cursor: BatchCursor,
    step: SceneStep,
    task: TaskCandidate,
    owner: Owner,
    now_value: str,
    on_read_ok: Callable[[str, Lease], None] | None = None,
) -> tuple[BatchCursor, str | None, str | None]:
    """认领/开始/收敛一条真实发现任务；决策在 running 租约内完成。"""
    lease = store.claim(  # type: ignore[attr-defined]
        "boss", task.kind, owner, now_value, conversation_key=task.key
    )
    if lease is None:
        return cursor, "unread_unverified", None
    store.start(lease, now_value)  # type: ignore[attr-defined]
    result = step.read_result
    if result is None:
        result = ReadResult(
            status="undetermined",
            value=None,
            reason_code="incomplete_snapshot",
            snapshot_id=None,
            observed_at=now_value,
            source="fixture",
            confidence="low",
        )
    if result.status == "ok":
        if on_read_ok is not None:
            on_read_ok(now_value, lease)
        store.finish(lease, now_value, "done", None, result)  # type: ignore[attr-defined]
        pause = None
    elif result.status == "failed":
        store.finish(lease, now_value, "failed", result.reason_code, result)  # type: ignore[attr-defined]
        pause = "precondition_failed"
    else:
        store.finish(lease, now_value, "blocked", result.reason_code, result)  # type: ignore[attr-defined]
        pause = "unread_unverified"
    cursor = replace(cursor, budget=cursor.budget.spend_candidate())
    return cursor, pause, lease.key


def _save_decision(
    *,
    store: RuntimeStorePort,
    batch_id: str,
    profile_store: ProfileStorePort,
    pack: PolicyPack,
    task: TaskCandidate,
    step: SceneStep,
    decision: RuntimeDecisionPort,
    provider_id: str,
    model_id: str,
    model_config_hash: str,
    base_hash: str,
    lease: Lease,
    process_log: ProcessLog | None,
    now_value: str,
) -> dict[str, object]:
    """按当前事实/策略身份检索或生成一次决策，并保存可重审草稿。"""
    if step.decision is None:
        return {"draft": None, "reused": False}
    task_kind = "job" if task.kind == "job" else "discovery"
    facts_hash = _facts_hash(profile_store, pack, now=now_value)
    input_hash = _input_hash(task, step.decision.evidence_hash, facts_hash)
    if not store.require_running(lease, now=now_value):
        raise PolicyError("authorization_required", [_issue("lease", "stale")])
    cached = store.find_runtime_decision(
        subject_key=task.key,
        task_kind=task_kind,
        input_hash=input_hash,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        model_id=model_id,
        model_config_hash=model_config_hash,
        facts_hash=facts_hash,
    )
    if cached is not None:
        store.record_runtime_event(
            batch_id,
            "decision_reused",
            now=now_value,
            task_key=task.key if task.key.startswith("boss:job:") else None,
            payload={"input_hash": input_hash, "facts_hash": facts_hash},
        )
        if process_log is not None:
            process_log.record(stage="run", operation="decision_reused", result="ok")
        return {"draft": None, "reused": True}
    stimulus = decision.decide(
        task=task, input_hash=input_hash, facts_hash=facts_hash
    )
    draft_id = __import__("uuid").uuid4()
    store.save_draft(
        draft_id=str(draft_id),
        task_kind=task_kind,
        task_key=task.key,
        subject_key=task.key,
        input_hash=input_hash,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        model_id=model_id,
        model_config_hash=model_config_hash,
        facts_hash=facts_hash,
        intent=stimulus.intent,
        sub_intents=stimulus.sub_intents,
        policy_tier=stimulus.policy_tier,
        reply=stimulus.reply,
        now=now_value,
    )
    store.save_runtime_decision(
        subject_key=task.key,
        task_kind=task_kind,
        input_hash=input_hash,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        model_id=model_id,
        model_config_hash=model_config_hash,
        facts_hash=facts_hash,
        intent=stimulus.intent,
        sub_intents=stimulus.sub_intents,
        policy_tier=stimulus.policy_tier,
        now=now_value,
    )
    if process_log is not None:
        process_log.record(stage="run", operation="draft_saved", result="ok")
    return {"draft": str(draft_id), "reused": False}


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
    decision: RuntimeDecisionPort,
    open_store: Callable[[], RuntimeStorePort],
    limits: BudgetLimits | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    model_config_hash: str | None = None,
    eval_store: EvalStorePort | None = None,
    credential_rel: str | None = None,
    process_log: ProcessLog | None = None,
    clock: Callable[[], str] | None = None,
) -> dict[str, object]:
    """执行一次有界 run：先占共享门禁，再打开 profile 与仓储。"""
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
    run_base_hash = compute_base_hash(base_files)
    if clock is None:
        from app.domain.policy import utc_now as clock

    gate.acquire(
        profile_id=pack.profile_id,
        profile_db_path=profile_db_path,
        owner=owner,
        now=now,
    )
    store: RuntimeStorePort | None = None
    converged = False
    try:
        gate.verify(now=now)
        open_profile_store(profile_store, process_log)
        pending = profile_store.pending_activity()
        if (
            pending["leased"]
            or pending["running"]
            or pending["blocked"]
            or pending["device_lease"]
            or pending.get("actions")
        ):
            raise PolicyError(
                "authorization_required", [_issue("activity", "unresolved")]
            )
        if mode == "read_only":
            step = scene.next_step()
            result: dict[str, object] = {
                "mode": mode,
                "read_only": True,
                "observed": (
                    None
                    if step is None
                    else {
                        "unread": len(step.candidates.unread),
                        "new_messages": len(step.candidates.new_messages),
                        "jobs": len(step.candidates.jobs),
                        "safe_point": step.safe_point.is_safe(),
                    }
                ),
                "business_writes": 0,
                "navigation": 0,
                "model_calls": 0,
                "actions_performed": 0,
            }
            converged = True
            return {
                "run": result,
                "acceptance": acceptance,
                "limitations": [
                    "fixture_stimulus_only",
                    "real_device_read_not_calibrated",
                    "real_provider_not_called",
                ],
            }

        store = open_store()
        batch_id = _batch_id(scene)
        batch = store.ensure_batch(batch_id, limits, clock())
        cursor = batch["cursor"]
        assert isinstance(cursor, BatchCursor)
        store.record_runtime_event(batch_id, "run_started", now=clock())
        last_kind: str | None = None
        first_observed: PreemptionObservation | None = None
        previous_check_mono: int | None = None
        wall_start_ms = 0
        monotonic_start_ms: int | None = None
        drafts: list[dict[str, object]] = []
        preemptions: list[dict[str, object]] = []
        overruns: list[dict[str, object]] = []
        decisions_reused = 0
        processed = 0
        pause_reason: str | None = None
        while True:
            step = scene.next_step()
            if step is None:
                break
            now_value = clock()
            if monotonic_start_ms is None:
                monotonic_start_ms = step.monotonic_ms
                wall_start_ms = _wall_ms(step.wall_now)
            else:
                if clock_drift_exceeded(
                    wall_start_ms=wall_start_ms,
                    wall_now_ms=_wall_ms(step.wall_now),
                    monotonic_start_ms=monotonic_start_ms,
                    monotonic_now_ms=step.monotonic_ms,
                ):
                    pause_reason = "clock_changed"
                    _pause(
                        store,
                        batch_id,
                        cursor,
                        pause_reason,
                        now=now_value,
                        process_log=process_log,
                    )
                    break
            cursor = _step_costs(cursor, step)
            effective = replace(step.candidates, jobs=_resume_jobs(cursor, step.candidates))
            if step.in_progress:
                if effective.unread and last_kind == "job":
                    first_observed = observe_unread(
                        first_observed,
                        wall_now=now_value,
                        monotonic_ms=step.monotonic_ms,
                    )
                    cursor = cursor.with_first_observed(now_value)
                previous_check_mono = step.monotonic_ms
                continue
            if step.safe_point.failed_conditions():
                pause_reason = "precondition_failed"
                _pause(
                    store,
                    batch_id,
                    cursor,
                    pause_reason,
                    now=now_value,
                    process_log=process_log,
                )
                break
            switching_unread = bool(effective.unread) and last_kind == "job"
            if switching_unread and cursor.budget.remaining_preemptions <= 0:
                pause_reason = "preemption_limit"
                _pause(
                    store,
                    batch_id,
                    cursor,
                    pause_reason,
                    now=now_value,
                    process_log=process_log,
                )
                break
            if not cursor.budget.can_start_work():
                pause_reason = "budget_exhausted"
                _pause(
                    store,
                    batch_id,
                    cursor,
                    pause_reason,
                    now=now_value,
                    process_log=process_log,
                )
                break
            selection = _selection(
                effective,
                last_kind=last_kind,
                preemptions_used=cursor.preemptions_used,
                limits=limits,
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
                    _pause(
                        store,
                        batch_id,
                        cursor,
                        pause_reason,
                        now=now_value,
                        process_log=process_log,
                    )
                    break
                selection = _selection(
                    _scene_without_unread_unknown(effective),
                    last_kind=last_kind,
                    preemptions_used=cursor.preemptions_used,
                    limits=limits,
                )
            if selection.pause_reason is not None:
                pause_reason = selection.pause_reason
                _pause(
                    store,
                    batch_id,
                    cursor,
                    pause_reason,
                    now=now_value,
                    process_log=process_log,
                )
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
                tuple(candidate.key for candidate in step.candidates.jobs),
                now_value,
            )
            if task.kind == "job":
                cursor, task_pause, lease_key = _handle_job(
                    store=store,
                    batch_id=batch_id,
                    cursor=cursor,
                    step=step,
                    task=task,
                    owner=owner,
                    now_value=now_value,
                )
            else:
                def on_read_ok(now_value: str, lease: Lease) -> None:
                    """在 running 租约内按当前事实/验收身份保存或检索决策。"""
                    nonlocal decisions_reused
                    if (
                        mode != "draft_only"
                        or step.decision is None
                        or step.read_result is None
                        or step.read_result.status != "ok"
                    ):
                        return
                    _acceptance(
                        mode=mode,
                        eval_store=eval_store,
                        credential_rel=credential_rel,
                        provider_id=provider_id,
                        model_id=model_id,
                        model_config_hash=model_config_hash,
                        base_files=base_files,
                        pack=pack,
                    )
                    setter = getattr(decision, "set_pending", None)
                    if callable(setter):
                        setter(step.decision)
                    saved = _save_decision(
                        store=store,
                        batch_id=batch_id,
                        profile_store=profile_store,
                        pack=pack,
                        task=task,
                        step=step,
                        decision=decision,
                        provider_id=provider_id or "",
                        model_id=model_id or "",
                        model_config_hash=model_config_hash or "",
                        base_hash=run_base_hash,
                        lease=lease,
                        process_log=process_log,
                        now_value=now_value,
                    )
                    if saved["reused"]:
                        decisions_reused += 1
                    else:
                        drafts.append(saved)

                cursor, task_pause, lease_key = _handle_discovery(
                    store=store,
                    cursor=cursor,
                    step=step,
                    task=task,
                    owner=owner,
                    now_value=now_value,
                    on_read_ok=on_read_ok,
                )
            processed += 1
            if (
                task.kind == "unread"
                and last_kind == "job"
                and task_pause is None
                and lease_key is not None
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
            if task.kind == "job" and step.elapsed_seconds >= limits.job_seconds:
                overruns.append(
                    {
                        "kind": "job",
                        "elapsed_seconds": step.elapsed_seconds,
                        "limit_seconds": limits.job_seconds,
                    }
                )
                if task_pause is None:
                    task_pause = "precondition_failed"
            previous_check_mono = step.monotonic_ms
            last_kind = task.kind
            checkpoint = store.save_checkpoint(batch_id, cursor, now=now_value)
            if process_log is not None:
                process_log.record(
                    stage="run",
                    operation="checkpoint",
                    result="ok",
                    duration_ms=0,
                )
            del checkpoint
            if task_pause is not None:
                pause_reason = task_pause
                _pause(
                    store,
                    batch_id,
                    cursor,
                    pause_reason,
                    now=now_value,
                    process_log=process_log,
                )
                break
        store.record_runtime_event(
            batch_id,
            "run_finished",
            now=clock(),
            reason_code=pause_reason,
            payload={"processed": processed, "preemptions": len(preemptions)},
        )
        converged = pause_reason in {None, "budget_exhausted", "preemption_limit", "unread_unverified"}
        return {
            "run": {
                "mode": mode,
                "batch_id": batch_id,
                "processed": processed,
                "pause_reason": pause_reason,
                "drafts": drafts,
                "decisions_reused": decisions_reused,
                "preemptions": preemptions,
                "overruns": overruns,
                "remaining_budget": cursor.budget.to_json(),
                "preemptions_used": cursor.preemptions_used,
                "actions_performed": 0,
                "model_calls": 0 if mode != "draft_only" else len(drafts),
                "business_writes": processed,
            },
            "acceptance": acceptance,
            "limitations": [
                "fixture_stimulus_only",
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
            gate.release(now=now, converged=converged and close_error is None)
        except Exception:
            if close_error is not None:
                raise close_error
            raise


def _wall_ms(value: str) -> int:
    """把规范 UTC 时间换算为毫秒，供双时钟漂移比较。"""
    from app.domain.models import parse_utc

    return int(parse_utc(value).timestamp() * 1000)


def _batch_id(scene: RuntimeScenePort) -> str:
    """从场景端口读取批次身份；端口必须显式提供。"""
    batch_id = getattr(scene, "batch_id", None)
    if not isinstance(batch_id, str):
        raise PolicyError("invalid_input", [_issue("batch", "missing")])
    return batch_id


def _last_job_key(cursor: BatchCursor) -> str:
    """返回最近一次已处理的职位键；抢占事件必须绑定真实职位。"""
    if cursor.last_job_dedupe_key is None:
        raise PolicyError("invalid_input", [_issue("job", "missing")])
    return cursor.last_job_dedupe_key
