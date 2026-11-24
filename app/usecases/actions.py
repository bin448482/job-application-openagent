"""编排 M5 单次动作执行、文本资格与人工对账，不实现调度或真实设备入口。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

from app.domain.actions import (
    ActionAuthorization,
    ActionError,
    ActionIdentity,
    ActionObservation,
    ActionOutcome,
    ReconciliationObservation,
    authorize,
    derive_action_outcome,
    human_card,
    is_action_key,
    reconciliation_converged,
    validate_action_target,
    validate_reconciliation_observation,
)
from app.domain.models import Lease, Owner, is_hash, parse_utc
from app.domain.policy import utc_now, v1_policy_tier
from app.ports.actions import ActionExecutionError, ActionPlan, DeviceActionPort
from app.ports.device_gate import SharedDeviceGatePort
from app.ports.store import StorePort
from app.process_log import ProcessLog


def _authorization_projection(authorization: ActionAuthorization) -> str:
    """把本轮授权投影为不含凭据的固定 JSON，供本机库留痕。"""
    return json.dumps(
        {
            "profile_id": authorization.profile_id,
            "base_hash": authorization.base_hash,
            "pack_hash": authorization.pack_hash,
            "acceptance_base_hash": authorization.acceptance_base_hash,
            "acceptance_pack_hash": authorization.acceptance_pack_hash,
            "task_key": authorization.task_key,
            "action_key": authorization.action_key,
            "authorized_kinds": list(authorization.authorized_kinds),
            "expires_at": authorization.expires_at,
            "confirmed": authorization.confirmed,
            "nonce": authorization.nonce,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _log(
    process_log: ProcessLog | None,
    operation: str,
    result: str,
    *,
    reason_code: str | None = None,
    error_category: str | None = None,
) -> None:
    """写固定动作阶段事件；日志失败不影响业务结果。"""
    if process_log is None:
        return
    try:
        process_log.record(
            stage="action",
            operation=operation,
            result=result,
            reason_code=reason_code,
            error_category=error_category,
        )
    except Exception:
        return


def _store_binding_matches(
    store: StorePort, *, profile_id: str, profile_db_path: str
) -> bool:
    """核对实际仓储 profile 身份与固定库路径，不只比较调用参数字符串。"""
    actual_profile = getattr(store, "profile_id", None)
    actual_path = getattr(store, "path", None)
    if actual_profile != profile_id or actual_path is None:
        return False
    try:
        return Path(actual_path).resolve() == Path(profile_db_path).resolve()
    except (OSError, ValueError):
        return False


def _identity_matches(
    gate: SharedDeviceGatePort,
    *,
    profile_id: str,
    profile_db_path: str,
    owner: Owner,
    require_owner: bool,
) -> bool:
    """核对共享门禁的 profile/原库身份，必要时同时核对当前 owner。"""
    identity = gate.session_identity()
    if (
        identity.profile_id != profile_id
        or Path(identity.profile_db_path).resolve() != Path(profile_db_path).resolve()
        or identity.state != "held"
    ):
        return False
    if require_owner and identity.owner != owner:
        return False
    return True


def _failed_observation(
    *,
    plan: ActionPlan,
    reason_code: str,
    clicked: bool,
    now: str,
    ime_original: str | None,
    scene_settled: bool = False,
    input_box_empty: bool = False,
    command_quiet: bool = False,
    ime_restored: bool = False,
) -> ActionObservation:
    """把执行器失败收敛为观察；点击后断连绝不降级为未执行。"""
    return ActionObservation(
        action_key=plan.action_key,
        action_kind=plan.action_kind,
        before_snapshot_id=None,
        after_snapshot_id=None,
        observed_at=now,
        clicked=clicked,
        error=reason_code,
        scene_settled=scene_settled,
        command_quiet=command_quiet,
        confirmation_open=False,
        input_box_empty=input_box_empty,
        ime_original=ime_original,
        ime_current=ime_original if ime_restored else None,
        body_readback_matches=False,
        echo_count_before=0,
        echo_count_after=0,
        attachment_name_matched=False,
        attachment_request_observed=False,
        delivery_observed=False,
        contact_slot_available=False,
        contact_slot_status=None,
        identity_unique=False,
        list_complete=False,
        protections_clear=False,
        target_present_before=False,
        target_present_after=False,
        anchor_present_after=False,
    )


def perform_action(
    *,
    store: StorePort,
    gate: SharedDeviceGatePort,
    authorization: ActionAuthorization,
    lease: Lease,
    action_kind: str,
    action_key: str,
    target_key: str,
    conversation_key: str,
    body_text: str | None,
    attachment_name: str | None,
    identity: ActionIdentity,
    anchor_label: str | None,
    ime_component: str | None,
    tap_points: tuple[tuple[int, int], ...],
    current_base_hash: str,
    current_pack_hash: str,
    daily_sends: int,
    reply_cooldown_seconds: int,
    profile_db_path: str,
    executor: DeviceActionPort,
    now: str,
    process_log: ProcessLog | None = None,
    clock: Callable[[], str] | None = None,
    reload_snapshot: Callable[[], dict[str, str]] | None = None,
) -> dict[str, object]:
    """在共享占用与运行中租约下原子 pending、执行一次并提交三态证据。"""
    active_clock = clock if clock is not None else utc_now
    current = {
        "base_hash": current_base_hash,
        "pack_hash": current_pack_hash,
        "acceptance_base_hash": current_base_hash,
        "acceptance_pack_hash": current_pack_hash,
    }

    def guard() -> None:
        """在每个设备调用与提交前重核当前包/验收/仓储/共享/租约条件。"""
        current_now = active_clock()
        snapshot = reload_snapshot() if reload_snapshot is not None else current
        for field in (
            "base_hash",
            "pack_hash",
            "acceptance_base_hash",
            "acceptance_pack_hash",
        ):
            if field not in snapshot or not is_hash(snapshot[field]):
                raise ActionError("policy_changed")
        authorize(
            authorization,
            action_kind=action_kind,
            action_key=action_key,
            task_key=lease.key,
            profile_id=authorization.profile_id,
            base_hash=snapshot["base_hash"],
            pack_hash=snapshot["pack_hash"],
            now=current_now,
            acceptance_base_hash=snapshot.get(
                "acceptance_base_hash", snapshot["base_hash"]
            ),
            acceptance_pack_hash=snapshot.get(
                "acceptance_pack_hash", snapshot["pack_hash"]
            ),
        )
        if not _store_binding_matches(
            store,
            profile_id=authorization.profile_id,
            profile_db_path=profile_db_path,
        ):
            raise ActionError("authorization_required")
        if not store.require_running(lease, now=current_now):
            raise ActionError("authorization_required")
        gate.verify(now=current_now)
        if not _identity_matches(
            gate,
            profile_id=authorization.profile_id,
            profile_db_path=profile_db_path,
            owner=lease.owner,
            require_owner=True,
        ):
            raise ActionError("authorization_required")

    _log(process_log, "action_validate", "started")
    try:
        if not is_action_key(action_kind, action_key):
            raise ActionError("invalid_action")
        validate_action_target(action_kind, action_key, target_key)
        if (
            not isinstance(identity, ActionIdentity)
            or identity.conversation_key() != conversation_key
        ):
            raise ActionError("precondition_failed")
        persisted_conversation = store.task_conversation(lease, now=now)
        if (
            persisted_conversation is None
            or persisted_conversation != conversation_key
        ):
            raise ActionError("precondition_failed")
        if action_kind != "send_text" and target_key != conversation_key:
            raise ActionError("invalid_action")
        guard()
    except ActionError as error:
        _log(
            process_log,
            "action_validate",
            "failed",
            reason_code=error.reason_code,
            error_category="action",
        )
        raise
    _log(process_log, "action_validate", "ok")

    plan = ActionPlan(
        action_key=action_key,
        action_kind=action_kind,
        target_key=target_key,
        conversation_key=conversation_key,
        body_text=body_text,
        attachment_name=attachment_name,
        target_label=identity.contact,
        anchor_label=anchor_label,
        ime_component=ime_component,
        ime_original=None,
        irreversible_tap_index=len(tap_points) - 1 if tap_points else 0,
        tap_points=tap_points,
        identity_labels=identity.labels(),
    )

    _log(process_log, "action_admit", "started")
    try:
        # 唯一准入边界：额度/冷却/pending/键锁同一事务；此处先不读设备。
        store.begin_action(
            lease,
            action_kind=action_kind,
            action_key=action_key,
            target_key=target_key,
            conversation_key=conversation_key,
            authorization_json=_authorization_projection(authorization),
            daily_sends=daily_sends,
            reply_cooldown_seconds=reply_cooldown_seconds,
            ime_original=None,
            now=now,
        )
    except Exception as error:
        reason = getattr(error, "reason_code", "store_io_error")
        _log(
            process_log,
            "action_admit",
            "failed",
            reason_code=reason,
            error_category="action",
        )
        raise
    _log(process_log, "action_admit", "ok")

    _log(process_log, "action_execute", "started")
    bind = getattr(executor, "bind_action", None)
    if callable(bind):
        bind(action_key)
    ime_original: str | None = None
    if ime_component is not None:
        try:
            ime_original = executor.read_current_ime(before_command=guard)
            if not isinstance(ime_original, str) or not ime_original:
                raise ActionExecutionError(
                    "device_disconnected",
                    clicked=False,
                    scene_settled=True,
                    input_box_empty=True,
                    command_quiet=True,
                    ime_restored=True,
                )
            store.save_ime_state(action_key, ime_original, now=now)
            plan = replace(plan, ime_original=ime_original)
        except ActionExecutionError as error:
            observation = _failed_observation(
                plan=plan,
                reason_code=error.reason_code,
                clicked=False,
                now=now,
                ime_original=None,
                scene_settled=error.scene_settled,
                input_box_empty=error.input_box_empty,
                command_quiet=error.command_quiet,
                ime_restored=error.ime_restored,
            )
            _log(
                process_log,
                "action_execute",
                "failed",
                reason_code=error.reason_code,
                error_category="action",
            )
            return _commit(
                store=store,
                lease=lease,
                action_key=action_key,
                observation=observation,
                ime_original=ime_original,
                now=active_clock(),
                process_log=process_log,
            )
    try:
        guard()
        observation = executor.execute(plan, before_command=guard)
        if (
            observation.action_key != action_key
            or observation.action_kind != action_kind
        ):
            raise ValueError("observation identity mismatch")
        # 观察时间必须落在本次 pending 与可信当前时间之间。
        observed = parse_utc(observation.observed_at)
        current_now = parse_utc(active_clock())
        if observed > current_now or observed < parse_utc(now):
            raise ValueError("observation time is not trusted")
    except ActionExecutionError as error:
        observation = _failed_observation(
            plan=plan,
            reason_code=error.reason_code,
            clicked=error.clicked,
            now=now,
            ime_original=ime_original,
            scene_settled=error.scene_settled,
            input_box_empty=error.input_box_empty,
            command_quiet=error.command_quiet,
            ime_restored=error.ime_restored,
        )
        _log(
            process_log,
            "action_execute",
            "failed",
            reason_code=error.reason_code,
            error_category="action",
        )
    except ActionError as error:
        # 执行中条件漂移：不再发命令，按已尝试动作保留未决。
        observation = _failed_observation(
            plan=plan,
            reason_code="executor_interrupted",
            clicked=True,
            now=now,
            ime_original=ime_original,
        )
        _log(
            process_log,
            "action_execute",
            "failed",
            reason_code=error.reason_code,
            error_category="action",
        )
    except Exception:
        observation = _failed_observation(
            plan=plan,
            reason_code="executor_interrupted",
            clicked=True,
            now=now,
            ime_original=ime_original,
        )
        _log(
            process_log,
            "action_execute",
            "failed",
            reason_code="executor_interrupted",
            error_category="internal",
        )

    if observation.error is None:
        _log(process_log, "action_execute", "ok")
    try:
        guard()
    except Exception:
        observation = _failed_observation(
            plan=plan,
            reason_code="executor_interrupted",
            clicked=True,
            now=now,
            ime_original=ime_original,
        )
    return _commit(
        store=store,
        lease=lease,
        action_key=action_key,
        observation=observation,
        ime_original=ime_original,
        now=active_clock(),
        process_log=process_log,
    )


def _bind_ime(
    observation: ActionObservation, ime_original: str | None
) -> ActionObservation:
    """把完成观察绑定到已持久化原 IME；缺失/不同值不假称已恢复。"""
    if ime_original is None:
        return observation
    if (
        observation.ime_original == ime_original
        and observation.ime_current == ime_original
    ):
        return observation
    return replace(
        observation, ime_original=ime_original, ime_current=None
    )


def _commit(
    *,
    store: StorePort,
    lease: Lease,
    action_key: str,
    observation: ActionObservation,
    ime_original: str | None,
    now: str,
    process_log: ProcessLog | None,
) -> dict[str, object]:
    """由唯一纯域规则派生 Evidence 并原子提交；坏观察保持未知未决。"""
    observation = _bind_ime(observation, ime_original)
    _log(process_log, "action_derive", "started")
    try:
        outcome = derive_action_outcome(observation)
    except ValueError:
        outcome = derive_action_outcome(
            _failed_observation(
                plan=ActionPlan(
                    action_key=action_key,
                    action_kind=observation.action_kind,
                    target_key=observation.action_key,
                    conversation_key=observation.action_key,
                    body_text=None,
                    attachment_name=None,
                    target_label=None,
                    anchor_label=None,
                    ime_component=None,
                    ime_original=None,
                    irreversible_tap_index=0,
                    tap_points=(),
                ),
                reason_code="executor_interrupted",
                clicked=True,
                now=now,
                ime_original=None,
            )
        )
        _log(
            process_log,
            "action_derive",
            "failed",
            reason_code="invalid_observation",
            error_category="action",
        )
    else:
        _log(process_log, "action_derive", "ok")
    _log(process_log, "action_commit", "started")
    try:
        completed = store.complete_action(
            lease, action_key=action_key, outcome=outcome, now=now
        )
    except Exception as error:
        reason = getattr(error, "reason_code", "store_io_error")
        _log(
            process_log,
            "action_commit",
            "failed",
            reason_code=reason,
            error_category="action",
        )
        if reason in {"stale_lease", "action_not_ready", "invalid_observation"}:
            _log(process_log, "action_freeze", "started")
            try:
                frozen = store.freeze_attempted_action(
                    lease,
                    action_key=action_key,
                    reason_code="result_unverified",
                    now=now,
                )
            except Exception as freeze_error:
                _log(
                    process_log,
                    "action_freeze",
                    "failed",
                    reason_code=getattr(
                        freeze_error, "reason_code", "store_io_error"
                    ),
                    error_category="action",
                )
                raise
            _log(process_log, "action_freeze", "ok")
            record = store.get_action(action_key)
            frozen["card"] = (
                human_card(record)
                if record is not None
                and record["state"] in {"pending", "unknown", "cancelled"}
                else None
            )
            frozen["requested_effect"] = "none"
            return frozen
        raise
    _log(process_log, "action_commit", "ok")
    record = store.get_action(action_key)
    card = None
    if record is not None and record["state"] in {"pending", "unknown", "cancelled"}:
        card = human_card(record)
    result = dict(completed)
    result["card"] = card
    result["requested_effect"] = outcome.evidence.effect
    return result


def evaluate_text_eligibility(
    *,
    store: StorePort,
    authorization: ActionAuthorization,
    action_kind: str,
    action_key: str,
    target_key: str,
    conversation_key: str,
    task_key: str,
    current_base_hash: str,
    current_pack_hash: str,
    daily_sends: int,
    reply_cooldown_seconds: int,
    profile_db_path: str,
    now: str,
) -> dict[str, object]:
    """只计算文本拟执行资格；固定 C 与真实执行开关保持独立。"""
    authorization_valid = True
    authorization_reason: str | None = None
    if action_kind != "send_text":
        authorization_valid = False
        authorization_reason = "invalid_action"
    else:
        try:
            validate_action_target(action_kind, action_key, target_key)
            if not _store_binding_matches(
                store,
                profile_id=authorization.profile_id,
                profile_db_path=profile_db_path,
            ):
                raise ActionError("authorization_required")
            authorize(
                authorization,
                action_kind=action_kind,
                action_key=action_key,
                task_key=task_key,
                profile_id=authorization.profile_id,
                base_hash=current_base_hash,
                pack_hash=current_pack_hash,
                now=now,
            )
        except ActionError as error:
            authorization_valid = False
            authorization_reason = error.reason_code
    usage = store.text_admission_status(
        now=now,
        daily_sends=daily_sends,
        reply_cooldown_seconds=reply_cooldown_seconds,
        conversation_key=conversation_key,
    )
    history = store.get_action(action_key)
    history_state = None if history is None else str(history["state"])
    history_reason: str | None = None
    if history_state is not None:
        history_reason = (
            "text_send_blocked"
            if history_state in {"pending", "unknown", "cancelled"}
            else "action_conflict"
        )
    eligible = (
        authorization_valid
        and bool(usage["eligible"])
        and history_state is None
    )
    reason = authorization_reason if not authorization_valid else (
        history_reason if history_reason is not None else usage["reason"]
    )
    return {
        "eligible": eligible,
        "reason": reason,
        "authorization_valid": authorization_valid,
        "target_key": target_key,
        "conversation_key": conversation_key,
        "quota": usage,
        "history_state": history_state,
        "policy_tier": v1_policy_tier(),
        "production_auto_send": False,
        "actions_performed": 0,
    }


def reconcile_action(
    *,
    store: StorePort,
    gate: SharedDeviceGatePort,
    observation: ReconciliationObservation,
    profile_id: str,
    profile_db_path: str,
    explicit_confirm: bool,
    command_stop_checker: Callable[[int], str] | None,
    now: str,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """经原 profile 共享占用、持久化 IME 与本轮确认收敛人工处置。"""
    _log(process_log, "action_reconcile_validate", "started")
    try:
        if explicit_confirm is not True or observation.confirmed is not True:
            raise ActionError("recovery_not_confirmed")
        if not _store_binding_matches(
            store, profile_id=profile_id, profile_db_path=profile_db_path
        ):
            raise ActionError("authorization_required")
        if not _identity_matches(
            gate,
            profile_id=profile_id,
            profile_db_path=profile_db_path,
            owner=Owner(id="", pid=1, host=""),
            require_owner=False,
        ):
            raise ActionError("authorization_required")
        record = store.get_action(observation.action_key)
        if record is None:
            raise ActionError("action_not_found")
        action_kind = str(record["action_kind"])
        validate_reconciliation_observation(observation, action_kind=action_kind)
        ime_state = store.load_ime_state()
        authoritative_ime: str | None = None
        if (
            ime_state is not None
            and ime_state["action_key"] == observation.action_key
        ):
            authoritative_ime = str(ime_state["original_ime"])
            if (
                observation.ime_original != authoritative_ime
                or observation.ime_current != authoritative_ime
            ):
                raise ActionError("scene_unsettled")
        observation = replace(
            observation,
            ime_original=authoritative_ime,
            ime_current=authoritative_ime,
        )
        if observation.resolution != "abandon" and not reconciliation_converged(
            observation
        ):
            raise ActionError("scene_unsettled")
        if observation.evidence is not None:
            if parse_utc(observation.evidence.observed_at) != parse_utc(now):
                raise ActionError("invalid_observation")
        commands_stopped = False
        if observation.resolution != "abandon":
            owner_pid = int(record["owner_pid"])
            if command_stop_checker is None:
                raise ActionError("recovery_not_confirmed")
            commands_stopped = command_stop_checker(owner_pid) == "stopped"
            if not commands_stopped:
                raise ActionError("recovery_not_confirmed")
        _log(process_log, "action_reconcile_validate", "ok")
        _log(process_log, "action_reconcile_commit", "started")
        result = store.reconcile_action(
            action_key=observation.action_key,
            resolution=observation.resolution,
            evidence=observation.evidence,
            command_quiet=observation.command_quiet,
            scene_settled=observation.scene_settled,
            confirmation_open=observation.confirmation_open,
            ime_restored=(
                observation.ime_original is None
                or observation.ime_current == observation.ime_original
            ),
            explicit_confirm=explicit_confirm,
            command_stopped=commands_stopped,
            now=now,
        )
    except ActionError as error:
        _log(
            process_log,
            "action_reconcile_validate",
            "failed",
            reason_code=error.reason_code,
            error_category="action",
        )
        raise
    except Exception as error:
        _log(
            process_log,
            "action_reconcile_commit",
            "failed",
            reason_code=getattr(error, "reason_code", "store_io_error"),
            error_category="action",
        )
        raise
    _log(process_log, "action_reconcile_commit", "ok")
    return result
