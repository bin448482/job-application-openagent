"""编排 M5 单次动作执行、文本资格与人工对账，不实现调度或真实设备入口。"""

from __future__ import annotations

import json
from pathlib import Path

from app.domain.actions import (
    ActionAuthorization,
    ActionError,
    ActionObservation,
    ActionOutcome,
    ReconciliationObservation,
    authorize,
    derive_action_outcome,
    human_card,
    is_action_key,
    reconciliation_converged,
    validate_reconciliation_observation,
)
from app.domain.models import Lease, Owner, parse_utc
from app.domain.policy import v1_policy_tier
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
    target_label: str | None,
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
) -> dict[str, object]:
    """在共享占用与运行中租约下原子 pending、执行一次并提交三态证据。"""
    authorize(
        authorization,
        action_kind=action_kind,
        action_key=action_key,
        task_key=lease.key,
        profile_id=authorization.profile_id,
        base_hash=current_base_hash,
        pack_hash=current_pack_hash,
        now=now,
    )
    if not is_action_key(action_kind, action_key):
        raise ActionError("invalid_action")
    if not _identity_matches(
        gate,
        profile_id=authorization.profile_id,
        profile_db_path=profile_db_path,
        owner=lease.owner,
        require_owner=True,
    ):
        raise ActionError("authorization_required")
    if store.current_lease_expiry(lease, now=now) is None:
        raise ActionError("authorization_required")

    ime_original: str | None = None
    if ime_component is not None:
        try:
            ime_original = executor.read_current_ime()
        except ActionExecutionError:
            raise ActionError("precondition_failed") from None
        if not isinstance(ime_original, str) or not ime_original:
            raise ActionError("precondition_failed")

    plan = ActionPlan(
        action_key=action_key,
        action_kind=action_kind,
        target_key=target_key,
        conversation_key=conversation_key,
        body_text=body_text,
        attachment_name=attachment_name,
        target_label=target_label,
        anchor_label=anchor_label,
        ime_component=ime_component,
        ime_original=ime_original,
        irreversible_tap_index=len(tap_points) - 1 if tap_points else 0,
        tap_points=tap_points,
    )
    # 唯一准入边界：额度/冷却/pending/键锁与本次 pending 同一事务提交。
    store.begin_action(
        lease,
        action_kind=action_kind,
        action_key=action_key,
        target_key=target_key,
        conversation_key=conversation_key,
        authorization_json=_authorization_projection(authorization),
        daily_sends=daily_sends,
        reply_cooldown_seconds=reply_cooldown_seconds,
        ime_original=ime_original,
        now=now,
    )

    observation: ActionObservation
    try:
        observation = executor.execute(plan)
        if (
            observation.action_key != action_key
            or observation.action_kind != action_kind
        ):
            observation = _failed_observation(
                plan=plan,
                reason_code="executor_interrupted",
                clicked=True,
                now=now,
                ime_original=ime_original,
            )
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
    except Exception:
        observation = _failed_observation(
            plan=plan,
            reason_code="executor_interrupted",
            clicked=True,
            now=now,
            ime_original=ime_original,
        )

    outcome = derive_action_outcome(observation)
    completed = store.complete_action(
        lease, action_key=action_key, outcome=outcome, now=now
    )
    record = store.get_action(action_key)
    card = None
    if record is not None and record["state"] in {"pending", "unknown"}:
        card = human_card(record)
    result = dict(completed)
    result["card"] = card
    result["evidence"] = completed["evidence"]
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
    eligible = authorization_valid and bool(usage["eligible"])
    return {
        "eligible": eligible,
        "reason": authorization_reason if not authorization_valid else usage["reason"],
        "authorization_valid": authorization_valid,
        "target_key": target_key,
        "conversation_key": conversation_key,
        "quota": usage,
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
    now: str,
) -> dict[str, object]:
    """经原 profile 共享占用与本轮显式确认收敛人工卡三项处置。"""
    if explicit_confirm is not True:
        raise ActionError("recovery_not_confirmed")
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
    try:
        validate_reconciliation_observation(observation, action_kind=action_kind)
    except ActionError:
        raise
    except ValueError:
        raise ActionError("invalid_observation") from None
    if observation.resolution != "abandon" and not reconciliation_converged(
        observation
    ):
        raise ActionError("scene_unsettled")
    if observation.evidence is not None:
        if parse_utc(observation.evidence.observed_at) != parse_utc(now):
            raise ActionError("invalid_observation")
    return store.reconcile_action(
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
        now=now,
    )
