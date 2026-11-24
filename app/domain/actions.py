"""定义 M5 五类不可逆动作的动作键、观察、三态证据、授权、人工卡与对账规则。"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import (
    APP,
    EVIDENCE_EFFECTS,
    EVIDENCE_NOT_PERFORMED_REASONS,
    EVIDENCE_UNKNOWN_REASONS,
    Evidence,
    is_conversation_key,
    is_hash,
    is_nonempty_text,
    is_uuid,
    parse_utc,
    validate_evidence,
)
from app.domain.policy import is_profile_id
from app.domain.reading import is_message_key

ACTION_KINDS = (
    "send_text",
    "send_resume",
    "change_wechat",
    "change_phone",
    "delete_conversation",
)
ACTION_KIND_SET = frozenset(ACTION_KINDS)
ACTION_SUFFIXES = {
    "send_resume": "resume",
    "change_wechat": "wechat",
    "change_phone": "phone",
    "delete_conversation": "delete",
}
ACTION_EFFECT_BY_KIND = {
    "send_text": "sent",
    "send_resume": "request_sent",
    "change_wechat": "request_sent",
    "change_phone": "request_sent",
    "delete_conversation": "deleted",
}
DELIVERED_EFFECT = "sent"
ACTION_STATES = frozenset(
    {"pending", "performed", "not_performed", "unknown", "cancelled"}
)
UNRESOLVED_ACTION_STATES = frozenset({"pending", "unknown", "cancelled"})
ALLOWED_RESOLUTIONS = (
    "confirm_performed",
    "confirm_not_performed",
    "abandon",
)
OBSERVATION_ERRORS = frozenset(
    {"device_disconnected", "executor_interrupted"}
)
CONTACT_SLOT_STATUSES = frozenset({"requested", "none"})
ACTION_REASON_CODES = frozenset(
    {
        "invalid_action",
        "invalid_observation",
        "invalid_authorization",
        "authorization_required",
        "authorization_expired",
        "action_conflict",
        "action_not_found",
        "action_not_ready",
        "precondition_failed",
        "quota_exceeded",
        "cooldown_active",
        "text_send_blocked",
        "scene_unsettled",
        "recovery_not_confirmed",
        "action_mismatch",
        "invalid_resolution",
        "policy_changed",
    }
)


class ActionError(Exception):
    """携带固定 M5 动作原因码；不携带正文、坐标、路径或原始异常。"""

    def __init__(self, reason_code: str) -> None:
        """校验并保存白名单原因码，异常文本只含该码。"""
        if reason_code not in ACTION_REASON_CODES:
            raise ValueError("action reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ActionObservation:
    """表示一次执行器调用后的可信前后观察，不含正文与坐标原文。"""

    action_key: str
    action_kind: str
    before_snapshot_id: str | None
    after_snapshot_id: str | None
    observed_at: str
    clicked: bool
    error: str | None
    scene_settled: bool
    command_quiet: bool
    confirmation_open: bool
    input_box_empty: bool
    ime_original: str | None
    ime_current: str | None
    body_readback_matches: bool
    echo_count_before: int
    echo_count_after: int
    attachment_name_matched: bool
    attachment_request_observed: bool
    delivery_observed: bool
    contact_slot_available: bool
    contact_slot_status: str | None
    identity_unique: bool
    target_present_before: bool
    target_present_after: bool
    anchor_present_after: bool


@dataclass(frozen=True)
class ActionOutcome:
    """表示由观察推导出的 Evidence 与现场收敛事实，供仓储一次提交。"""

    evidence: Evidence
    scene_settled: bool
    confirmation_open: bool
    ime_restored: bool
    command_quiet: bool
    pause: bool
    delivered: bool


@dataclass(frozen=True)
class ActionAuthorization:
    """表示本轮针对单一动作键的显式分动作授权及其摘要绑定。"""

    profile_id: str
    base_hash: str
    pack_hash: str
    acceptance_base_hash: str
    acceptance_pack_hash: str
    task_key: str
    action_key: str
    authorized_kinds: tuple[str, ...]
    expires_at: str
    confirmed: bool
    nonce: str


@dataclass(frozen=True)
class ReconciliationObservation:
    """表示人工对账时对原动作、原命令与现场收敛的显式观察。"""

    action_key: str
    resolution: str
    observed_at: str
    evidence: Evidence | None
    command_quiet: bool
    scene_settled: bool
    confirmation_open: bool
    ime_original: str | None
    ime_current: str | None
    matched_action_key: str
    confirmed: bool


def _require_bool(name: str, value: object) -> None:
    """要求字段为真正的布尔值，不接受整数或其它真值。"""
    if type(value) is not bool:
        raise ValueError(f"{name} is invalid")


def _require_int(name: str, value: object) -> None:
    """要求字段为非负整数且不接受 bool。"""
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} is invalid")


def _require_optional_text(name: str, value: object) -> None:
    """要求字段为 null 或可编码非空字符串。"""
    if value is not None and not is_nonempty_text(value):
        raise ValueError(f"{name} is invalid")


def action_key_for(
    action_kind: str,
    *,
    app: str,
    conversation_key: str | None = None,
    message_key: str | None = None,
) -> str:
    """按设计说明 §5.5 生成五类动作键；文本键额外绑定入站消息身份。"""
    if action_kind not in ACTION_KIND_SET:
        raise ActionError("invalid_action")
    if not isinstance(app, str) or app != APP:
        raise ActionError("invalid_action")
    if action_kind == "send_text":
        if not is_message_key(message_key):
            raise ActionError("invalid_action")
        return f"{app}:{message_key}:reply"
    if not is_conversation_key(app, conversation_key):
        raise ActionError("invalid_action")
    return f"{conversation_key}:{ACTION_SUFFIXES[action_kind]}"


def is_action_key(action_kind: object, value: object) -> bool:
    """判断动作键是否精确符合该类动作的文档规则。"""
    if action_kind not in ACTION_KIND_SET or not isinstance(value, str):
        return False
    if action_kind == "send_text":
        prefix = f"{APP}:"
        suffix = ":reply"
        if not value.startswith(prefix) or not value.endswith(suffix):
            return False
        message_key = value[len(prefix): -len(suffix)]
        return is_message_key(message_key) and value == f"{APP}:{message_key}:reply"
    suffix = ":" + ACTION_SUFFIXES[action_kind]
    if not value.endswith(suffix):
        return False
    conversation_key = value[: -len(suffix)]
    return is_conversation_key(APP, conversation_key)


def validate_action_identity(action_kind: object, action_key: object) -> None:
    """校验动作类型与动作键的一致形状，失败抛固定 invalid_action。"""
    if not is_action_key(action_kind, action_key):
        raise ActionError("invalid_action")


def expected_effect(action_kind: str, *, delivered: bool = False) -> str:
    """返回该动作在 performed 时唯一允许的效果；简历交付与请求分开。"""
    if action_kind not in ACTION_KIND_SET:
        raise ActionError("invalid_action")
    if action_kind == "send_resume" and delivered:
        return DELIVERED_EFFECT
    return ACTION_EFFECT_BY_KIND[action_kind]


def validate_observation(observation: object) -> None:
    """严格校验动作观察的全部字段类型、枚举与 UUID/时间形状。"""
    if not isinstance(observation, ActionObservation):
        raise ValueError("observation is invalid")
    if not is_action_key(observation.action_kind, observation.action_key):
        raise ValueError("observation action key is invalid")
    for name in ("before_snapshot_id", "after_snapshot_id"):
        value = getattr(observation, name)
        if value is not None and not is_uuid(value):
            raise ValueError(f"{name} is invalid")
    if (observation.before_snapshot_id is None) != (
        observation.after_snapshot_id is None
    ):
        raise ValueError("snapshot pair is invalid")
    parse_utc(observation.observed_at)
    for name in (
        "clicked",
        "scene_settled",
        "command_quiet",
        "confirmation_open",
        "input_box_empty",
        "body_readback_matches",
        "attachment_name_matched",
        "attachment_request_observed",
        "delivery_observed",
        "contact_slot_available",
        "identity_unique",
        "target_present_before",
        "target_present_after",
        "anchor_present_after",
    ):
        _require_bool(name, getattr(observation, name))
    if observation.error is not None and observation.error not in OBSERVATION_ERRORS:
        raise ValueError("observation error is invalid")
    for name in ("echo_count_before", "echo_count_after"):
        _require_int(name, getattr(observation, name))
    _require_optional_text("ime_original", observation.ime_original)
    _require_optional_text("ime_current", observation.ime_current)
    if (
        observation.contact_slot_status is not None
        and observation.contact_slot_status not in CONTACT_SLOT_STATUSES
    ):
        raise ValueError("contact slot status is invalid")


def _precondition_ok(observation: ActionObservation) -> bool:
    """按动作类型判断点击前的前置证据是否成立。"""
    kind = observation.action_kind
    if kind == "send_text":
        return observation.body_readback_matches
    if kind == "send_resume":
        return observation.attachment_name_matched
    if kind in {"change_wechat", "change_phone"}:
        return observation.contact_slot_available
    if kind == "delete_conversation":
        return observation.identity_unique and observation.target_present_before
    return False


def _performed_effect(observation: ActionObservation) -> str | None:
    """只在动作特有前后变化足够证明时返回效果，否则返回 None。"""
    before = observation.before_snapshot_id
    after = observation.after_snapshot_id
    if before is None or after is None or before == after:
        return None
    kind = observation.action_kind
    if kind == "send_text":
        if (
            observation.body_readback_matches
            and observation.echo_count_after > observation.echo_count_before
        ):
            return "sent"
        return None
    if kind == "send_resume":
        if not observation.attachment_name_matched:
            return None
        if observation.delivery_observed:
            return "sent"
        if observation.attachment_request_observed:
            return "request_sent"
        return None
    if kind in {"change_wechat", "change_phone"}:
        if observation.contact_slot_status == "requested":
            return "request_sent"
        return None
    if kind == "delete_conversation":
        if (
            observation.target_present_before
            and not observation.target_present_after
            and observation.anchor_present_after
        ):
            return "deleted"
        return None
    return None


def derive_action_outcome(observation: ActionObservation) -> ActionOutcome:
    """由可信观察推导 performed/not_performed/unknown 与现场是否收敛。"""
    validate_observation(observation)
    kind = observation.action_kind
    before = observation.before_snapshot_id
    after = observation.after_snapshot_id
    ime_restored = (
        observation.ime_original is None
        or observation.ime_current == observation.ime_original
    )

    if observation.error == "device_disconnected":
        if observation.clicked:
            evidence = _evidence(
                observation, "unknown", "none", "device_disconnected"
            )
        else:
            evidence = _evidence(
                observation, "not_performed", "none", "cancelled_before_action"
            )
    elif observation.error == "executor_interrupted":
        if observation.clicked:
            evidence = _evidence(
                observation, "unknown", "none", "executor_interrupted"
            )
        else:
            evidence = _evidence(
                observation, "not_performed", "none", "cancelled_before_action"
            )
    elif not _precondition_ok(observation):
        if observation.clicked:
            evidence = _evidence(
                observation, "unknown", "none", "result_unverified"
            )
        else:
            evidence = _evidence(
                observation, "not_performed", "none", "precondition_failed"
            )
    elif not observation.clicked:
        evidence = _evidence(
            observation, "not_performed", "none", "cancelled_before_action"
        )
    else:
        effect = _performed_effect(observation)
        if effect is None:
            evidence = _evidence(
                observation, "unknown", "none", "result_unverified"
            )
        else:
            evidence = _evidence(observation, "performed", effect, None)

    pause = (
        evidence.outcome != "performed"
        and (
            not observation.scene_settled
            or observation.confirmation_open
            or not observation.input_box_empty
            or not observation.command_quiet
            or not ime_restored
        )
    ) or (
        evidence.outcome == "performed"
        and (
            not observation.scene_settled
            or not observation.input_box_empty
            or not observation.command_quiet
            or not ime_restored
        )
    )
    return ActionOutcome(
        evidence=evidence,
        scene_settled=observation.scene_settled,
        confirmation_open=observation.confirmation_open,
        ime_restored=ime_restored,
        command_quiet=observation.command_quiet,
        pause=pause,
        delivered=(
            kind == "send_resume"
            and evidence.outcome == "performed"
            and evidence.effect == DELIVERED_EFFECT
        ),
    )


def _evidence(
    observation: ActionObservation,
    outcome: str,
    effect: str,
    reason_code: str | None,
) -> Evidence:
    """构造七字段 Evidence；文本只说 sent/unknown，从不使用旧术语。"""
    if effect not in EVIDENCE_EFFECTS:
        raise ActionError("invalid_observation")
    evidence = Evidence(
        action_key=observation.action_key,
        outcome=outcome,
        effect=effect,
        reason_code=reason_code,
        before_snapshot_id=observation.before_snapshot_id,
        after_snapshot_id=observation.after_snapshot_id,
        observed_at=observation.observed_at,
    )
    try:
        validate_evidence(evidence)
    except ValueError:
        raise ActionError("invalid_observation") from None
    return evidence


def validate_authorization(authorization: object) -> None:
    """严格校验本轮分动作授权的身份、摘要、动作键、期限与确认。"""
    if not isinstance(authorization, ActionAuthorization):
        raise ActionError("invalid_authorization")
    if not is_profile_id(authorization.profile_id):
        raise ActionError("invalid_authorization")
    for name in ("base_hash", "pack_hash", "acceptance_base_hash", "acceptance_pack_hash"):
        if not is_hash(getattr(authorization, name)):
            raise ActionError("invalid_authorization")
    if not isinstance(authorization.task_key, str) or not authorization.task_key.startswith(
        f"{APP}:discovery:"
    ):
        raise ActionError("invalid_authorization")
    if not is_action_key(
        _kind_from_key(authorization.action_key), authorization.action_key
    ):
        raise ActionError("invalid_authorization")
    if (
        not isinstance(authorization.authorized_kinds, tuple)
        or not authorization.authorized_kinds
        or any(kind not in ACTION_KIND_SET for kind in authorization.authorized_kinds)
        or len(set(authorization.authorized_kinds)) != len(
            authorization.authorized_kinds
        )
    ):
        raise ActionError("invalid_authorization")
    parse_utc(authorization.expires_at)
    if type(authorization.confirmed) is not bool:
        raise ActionError("invalid_authorization")
    if not is_uuid(authorization.nonce):
        raise ActionError("invalid_authorization")


def _kind_from_key(action_key: object) -> str | None:
    """从动作键找出唯一类型；未知返回 None 供授权校验拒绝。"""
    for kind in ACTION_KINDS:
        if is_action_key(kind, action_key):
            return kind
    return None


def authorize(
    authorization: ActionAuthorization,
    *,
    action_kind: str,
    action_key: str,
    task_key: str,
    profile_id: str,
    base_hash: str,
    pack_hash: str,
    now: str,
) -> None:
    """复核本轮授权是否恰好覆盖该动作、当前摘要与未过期期限。"""
    validate_authorization(authorization)
    now_dt = parse_utc(now)
    checks = (
        authorization.confirmed,
        action_kind in authorization.authorized_kinds,
        authorization.action_key == action_key,
        authorization.task_key == task_key,
        authorization.profile_id == profile_id,
        authorization.base_hash == base_hash,
        authorization.pack_hash == pack_hash,
        authorization.acceptance_base_hash == base_hash,
        authorization.acceptance_pack_hash == pack_hash,
        now_dt < parse_utc(authorization.expires_at),
    )
    if not all(checks):
        raise ActionError(
            "authorization_expired"
            if authorization.confirmed
            and authorization.action_key == action_key
            and now_dt >= parse_utc(authorization.expires_at)
            else "authorization_required"
        )


def human_card(record: dict[str, object]) -> dict[str, object]:
    """生成固定人工卡；只含无正文任务/动作、原因、快照与三项处置。"""
    action_key = record.get("action_key")
    task_key = record.get("task_key")
    reason_code = record.get("reason_code")
    if not is_nonempty_text(action_key) or not is_nonempty_text(task_key):
        raise ActionError("invalid_action")
    if reason_code is None:
        reason_code = "result_unverified"
    if reason_code not in EVIDENCE_UNKNOWN_REASONS:
        reason_code = "result_unverified"
    snapshots: list[str] = []
    for name in ("before_snapshot_id", "after_snapshot_id"):
        value = record.get(name)
        if isinstance(value, str) and is_uuid(value) and value not in snapshots:
            snapshots.append(value)
    return {
        "task_id": task_key,
        "action_key": action_key,
        "reason_code": reason_code,
        "snapshot_ids": snapshots,
        "allowed_resolutions": list(ALLOWED_RESOLUTIONS),
    }


def validate_reconciliation_observation(
    observation: object, *, action_kind: str
) -> None:
    """校验人工对账观察的类型、动作匹配与证据三态/效果组合。"""
    if not isinstance(observation, ReconciliationObservation):
        raise ActionError("invalid_observation")
    if not is_action_key(action_kind, observation.action_key):
        raise ActionError("invalid_observation")
    if observation.resolution not in ALLOWED_RESOLUTIONS:
        raise ActionError("invalid_resolution")
    if observation.matched_action_key != observation.action_key:
        raise ActionError("action_mismatch")
    parse_utc(observation.observed_at)
    for name in (
        "command_quiet",
        "scene_settled",
        "confirmation_open",
        "confirmed",
    ):
        if type(getattr(observation, name)) is not bool:
            raise ActionError("invalid_observation")
    _require_optional_text("ime_original", observation.ime_original)
    _require_optional_text("ime_current", observation.ime_current)
    if observation.resolution == "abandon":
        if observation.evidence is not None:
            raise ActionError("invalid_observation")
        return
    evidence = observation.evidence
    if not isinstance(evidence, Evidence):
        raise ActionError("invalid_observation")
    try:
        validate_evidence(evidence)
    except ValueError:
        raise ActionError("invalid_observation") from None
    if evidence.action_key != observation.action_key:
        raise ActionError("action_mismatch")
    if observation.resolution == "confirm_performed":
        if evidence.outcome != "performed":
            raise ActionError("invalid_observation")
        if evidence.effect not in {
            expected_effect(action_kind),
            expected_effect(action_kind, delivered=True),
        }:
            raise ActionError("invalid_observation")
        if action_kind != "send_resume" and evidence.effect != expected_effect(
            action_kind
        ):
            raise ActionError("invalid_observation")
    elif observation.resolution == "confirm_not_performed":
        if (
            evidence.outcome != "not_performed"
            or evidence.effect != "none"
            or evidence.reason_code not in EVIDENCE_NOT_PERFORMED_REASONS
        ):
            raise ActionError("invalid_observation")


def reconciliation_converged(observation: ReconciliationObservation) -> bool:
    """判断人工对账是否满足原命令停止、现场/IME 收敛且未留确认框。"""
    ime_restored = (
        observation.ime_original is None
        or observation.ime_current == observation.ime_original
    )
    return (
        observation.command_quiet
        and observation.scene_settled
        and not observation.confirmation_open
        and ime_restored
    )
