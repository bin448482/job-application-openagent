"""编排 M3 读取门禁、定向核对结果、唯一 JSON 输出与真实 run 门禁。"""

from __future__ import annotations

from app.domain.models import ReadResult, UnreadSnapshot, parse_utc, validate_read_result
from app.domain.reading import (
    Conversation,
    JobDetail,
    JobList,
    PageSnapshot,
    UnreadRead,
)
from app.domain.scheduler import stop_reason_for
from app.ports.screen import DeviceGate, ScreenPort
from app.usecases.payload import SCHEMA_VERSION, result_payload

GATE_REASONS = frozenset(
    {"not_ok", "field_unknown", "identity_ambiguous", "incomplete", "no_target"}
)
TARGETED_MAX_AGE_SECONDS = 5


class ReadNotActionable(Exception):
    """表示读取结果未通过决策门禁，不得调用模型或动作。"""

    def __init__(self, reason_code: str) -> None:
        """保存固定门禁原因，不携带业务原文。"""
        if reason_code not in GATE_REASONS:
            raise ValueError("gate reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


def is_actionable(result: ReadResult) -> tuple[bool, str | None]:
    """按值类型判断读取是否能进入后续决策；结构 ok 不等于可决策。"""
    validate_read_result(result)
    if result.status != "ok" or result.value is None:
        return False, "not_ok"
    value = result.value
    if isinstance(value, PageSnapshot):
        if value.page == "unknown":
            return False, "field_unknown"
        return True, None
    if isinstance(value, JobList):
        if any(card.job_key is not None for card in value.cards):
            return True, None
        return False, "identity_ambiguous"
    if isinstance(value, JobDetail):
        if not value.complete:
            return False, "incomplete"
        if value.job_key is None:
            return False, "identity_ambiguous"
        return True, None
    if isinstance(value, UnreadRead):
        if any(
            row.state == "present" and row.conversation_key is not None
            for row in value.rows
        ):
            return True, None
        if value.unread_state == "unknown":
            return False, "field_unknown"
        if value.unread_state == "present":
            return False, "identity_ambiguous"
        return False, "no_target"
    if isinstance(value, Conversation):
        if value.inbound_state == "known" and value.latest_inbound_key is not None:
            return True, None
        if value.inbound_state == "unknown":
            by_reason = {
                item.reason_code
                for item in value.field_evidence
                if item.field == "conversation_inbound"
            }
            if by_reason & {
                "unverified_identity",
                "stable_identity_unverified",
                "ambiguous_signal",
            }:
                return False, "identity_ambiguous"
            return False, "field_unknown"
        return False, "no_target"
    return False, "not_ok"


def require_actionable(result: ReadResult) -> None:
    """读取不可决策时抛出固定门禁错误，阻止模型或动作入口。"""
    actionable, reason = is_actionable(result)
    if not actionable:
        raise ReadNotActionable(reason or "not_ok")


def build_read_payload(result: ReadResult) -> dict[str, object]:
    """组装唯一读取 JSON 外层，并显式暴露是否通过决策门禁。"""
    validate_read_result(result)
    actionable, gate_reason = is_actionable(result)
    return {
        "schema_version": SCHEMA_VERSION,
        "result": result_payload(result),
        "actionable": actionable,
        "gate_reason": gate_reason,
    }


def targeted_verification_outcome(
    initial: ReadResult,
    targeted: ReadResult,
    queued_keys: list[str],
    *,
    now: str,
) -> str | None:
    """核对一次真正新鲜、同 app/profile 的定向采集，再计算停止原因。"""
    validate_read_result(initial)
    validate_read_result(targeted)
    if initial.status != "ok" or initial.value is None:
        return initial.reason_code
    if not isinstance(initial.value, UnreadSnapshot):
        raise ValueError("targeted verification requires unread snapshots")
    if targeted.status != "ok" or targeted.value is None:
        return targeted.reason_code
    if not isinstance(targeted.value, UnreadSnapshot):
        raise ValueError("targeted verification requires unread snapshots")
    try:
        now_dt = parse_utc(now)
        observed_dt = parse_utc(targeted.observed_at)
        initial_dt = parse_utc(initial.observed_at)
    except ValueError:
        raise ValueError("targeted verification time is invalid") from None
    delta = (now_dt - observed_dt).total_seconds()
    if delta < 0 or delta > TARGETED_MAX_AGE_SECONDS:
        return "stale_snapshot"
    if observed_dt < initial_dt:
        return "stale_snapshot"
    if targeted.snapshot_id == initial.snapshot_id:
        return "unread_unverified"
    if (
        targeted.value.app != initial.value.app
        or targeted.value.profile_id != initial.value.profile_id
    ):
        return "identity_ambiguous"
    reason = stop_reason_for(targeted.value, queued_keys)
    if reason == "needs_targeted_verification":
        return "unread_unverified"
    return reason


def read_from_device(
    screen: ScreenPort,
    gate: DeviceGate,
    serial: str,
    parse_hierarchy: object,
) -> ReadResult:
    """任何设备调用前校验 parser 与共享门禁，再采集一次层级。"""
    if not callable(parse_hierarchy):
        raise ValueError("parser is invalid")
    gate.require_available()
    capture = screen.dump_hierarchy(serial)
    return parse_hierarchy(capture.xml_text)


