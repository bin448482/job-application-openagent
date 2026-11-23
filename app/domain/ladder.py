"""实现 M3 字段级层级/像素/视觉/人工读取阶梯的纯合并规则。"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import APP, is_hash, is_nonempty_text, is_uuid, parse_utc
from app.domain.reading import (
    FIELD_FAILED_REASONS,
    FIELD_REASON_PRIORITY,
    FIELD_UNKNOWN_REASONS,
)

FIELD_READING_STATES = frozenset({"present", "absent", "unknown", "failed"})
FIELD_RESOLUTION_STATES = frozenset({"present", "absent", "undetermined", "failed"})
LADDER_FIELD = "unread_badges"
LADDER_SOURCES = frozenset({"hierarchy", "pixel", "vision", "human"})


@dataclass(frozen=True)
class FieldReading:
    """表示单个读取级别对同一字段的原始结论与绑定上下文。"""

    field: str
    app: str
    profile_id: str
    profile_version: str
    snapshot_id: str
    observed_at: str
    layout_context: str
    source: str
    confidence: str
    state: str
    count: int | None
    reason_code: str | None


@dataclass(frozen=True)
class FieldResolution:
    """表示字段级合并后的四态结论、证据链与人工出口标记。"""

    field: str
    app: str
    profile_id: str
    profile_version: str
    snapshot_id: str
    observed_at: str
    layout_context: str
    state: str
    count: int | None
    reason_code: str | None
    source: str | None
    confidence: str | None
    requires_human: bool
    evidence: tuple[FieldReading, ...]


def validate_field_reading(reading: object) -> None:
    """校验单级字段结论的绑定上下文、状态、计数与原因组合。"""
    if not isinstance(reading, FieldReading):
        raise ValueError("field reading is invalid")
    _validate_context(
        reading.field,
        reading.app,
        reading.profile_id,
        reading.profile_version,
        reading.snapshot_id,
        reading.observed_at,
        reading.layout_context,
    )
    if reading.source not in LADDER_SOURCES:
        raise ValueError("field reading source is invalid")
    if reading.confidence not in {"low", "medium", "high"}:
        raise ValueError("field reading confidence is invalid")
    if reading.state not in FIELD_READING_STATES:
        raise ValueError("field reading state is invalid")
    _validate_count_reason(reading.state, reading.count, reading.reason_code, reading.source)
    if reading.source == "pixel" and reading.state == "present" and reading.count is not None:
        raise ValueError("pixel present count must be unknown")


def validate_field_resolution(resolution: object) -> None:
    """校验合并结论、上下文、人工出口与证据链一致性。"""
    if not isinstance(resolution, FieldResolution):
        raise ValueError("field resolution is invalid")
    _validate_context(
        resolution.field,
        resolution.app,
        resolution.profile_id,
        resolution.profile_version,
        resolution.snapshot_id,
        resolution.observed_at,
        resolution.layout_context,
    )
    if resolution.state not in FIELD_RESOLUTION_STATES:
        raise ValueError("field resolution state is invalid")
    if type(resolution.requires_human) is not bool:
        raise ValueError("field resolution human flag is invalid")
    if not isinstance(resolution.evidence, (tuple, list)) or not resolution.evidence:
        raise ValueError("field resolution evidence is empty")
    for reading in resolution.evidence:
        validate_field_reading(reading)
        if (
            reading.field != resolution.field
            or reading.app != resolution.app
            or reading.profile_id != resolution.profile_id
            or reading.profile_version != resolution.profile_version
            or reading.snapshot_id != resolution.snapshot_id
            or reading.observed_at != resolution.observed_at
            or reading.layout_context != resolution.layout_context
        ):
            raise ValueError("field resolution context is mixed")
    if resolution.state == "undetermined":
        if (
            resolution.requires_human is not True
            or resolution.source is not None
            or resolution.confidence is not None
            or resolution.count is not None
            or resolution.reason_code not in FIELD_UNKNOWN_REASONS
        ):
            raise ValueError("undetermined field resolution is invalid")
        return
    if resolution.requires_human is not False or resolution.source is None:
        raise ValueError("resolved field resolution is invalid")
    if resolution.confidence not in {"low", "medium", "high"}:
        raise ValueError("field resolution confidence is invalid")
    _validate_count_reason(
        resolution.state,
        resolution.count,
        resolution.reason_code,
        resolution.source,
    )
    if resolution.state == "failed":
        if resolution.confidence != "low":
            raise ValueError("failed field resolution confidence is invalid")
        return
    if resolution.state == "absent" and resolution.count != 0:
        raise ValueError("absent field resolution count is invalid")


def resolve_field(
    *,
    hierarchy: FieldReading | None = None,
    pixel: FieldReading | None = None,
    vision: FieldReading | None = None,
    human: FieldReading | None = None,
    vision_enabled: bool = False,
    independent_cross_evidence: bool = False,
) -> FieldResolution:
    """按层级→像素→视觉→人工顺序合并字段，保留失败并拒绝冲突覆盖。"""
    readings = [
        reading
        for reading in (hierarchy, pixel, vision, human)
        if reading is not None
    ]
    if not readings:
        raise ValueError("field readings are empty")
    for reading in readings:
        validate_field_reading(reading)
    first = readings[0]
    if any(reading.field != first.field for reading in readings):
        raise ValueError("field readings use different fields")
    if any(
        (
            reading.app,
            reading.profile_id,
            reading.profile_version,
            reading.snapshot_id,
            reading.observed_at,
            reading.layout_context,
        )
        != (
            first.app,
            first.profile_id,
            first.profile_version,
            first.snapshot_id,
            first.observed_at,
            first.layout_context,
        )
        for reading in readings
    ):
        return _resolution(
            first,
            "undetermined",
            None,
            "ambiguous_signal",
            None,
            None,
            True,
            tuple(readings),
        )

    ordered = tuple(
        reading
        for reading in (hierarchy, pixel, vision, human)
        if reading is not None
    )
    if hierarchy is not None and hierarchy.state == "failed":
        return _resolution(
            first,
            "failed",
            None,
            hierarchy.reason_code,
            hierarchy.source,
            "low",
            False,
            ordered,
        )

    primary = hierarchy
    if primary is not None and primary.state in {"present", "absent"}:
        conflict = any(
            reading.state in {"present", "absent"} and reading.state != primary.state
            for reading in (pixel, vision)
            if reading is not None
            and (reading is not vision or vision_enabled)
        )
        if conflict:
            return _resolution(
                first,
                "undetermined",
                None,
                "ambiguous_signal",
                None,
                None,
                True,
                ordered,
            )
        return _resolution(
            first,
            primary.state,
            primary.count,
            None,
            "hierarchy",
            "high",
            False,
            ordered,
        )

    if pixel is not None and pixel.state == "failed":
        return _resolution(
            first,
            "failed",
            None,
            pixel.reason_code,
            pixel.source,
            "low",
            False,
            ordered,
        )

    if human is not None:
        if human.state in {"present", "absent"}:
            return _resolution(
                first,
                human.state,
                human.count,
                None,
                "human",
                "high",
                False,
                ordered,
            )
        return _resolution(
            first,
            "undetermined",
            None,
            human.reason_code or "ambiguous_signal",
            None,
            None,
            True,
            ordered,
        )

    if pixel is not None and pixel.state in {"present", "absent"}:
        conflict = (
            vision is not None
            and vision_enabled
            and independent_cross_evidence
            and vision.state in {"present", "absent"}
            and vision.state != pixel.state
        )
        if conflict:
            return _resolution(
                first,
                "undetermined",
                None,
                "ambiguous_signal",
                None,
                None,
                True,
                ordered,
            )
        return _resolution(
            first,
            pixel.state,
            pixel.count,
            None,
            "pixel",
            "medium",
            False,
            ordered,
        )

    if vision is not None and vision.state in {"present", "absent"}:
        # 视觉不是可独立放行的证据；启用但无独立交叉证据时仍转人工。
        return _resolution(
            first,
            "undetermined",
            None,
            "ambiguous_signal",
            None,
            None,
            True,
            ordered,
        )

    reasons = [
        reading.reason_code
        for reading in ordered
        if reading.state == "unknown" and reading.reason_code is not None
    ]
    return _resolution(
        first,
        "undetermined",
        None,
        _reason(reasons),
        None,
        None,
        True,
        ordered,
    )


def _resolution(
    first: FieldReading,
    state: str,
    count: int | None,
    reason_code: str | None,
    source: str | None,
    confidence: str | None,
    requires_human: bool,
    evidence: tuple[FieldReading, ...],
) -> FieldResolution:
    """以首个 reading 的绑定上下文构造字段级合并结论。"""
    return FieldResolution(
        field=first.field,
        app=first.app,
        profile_id=first.profile_id,
        profile_version=first.profile_version,
        snapshot_id=first.snapshot_id,
        observed_at=first.observed_at,
        layout_context=first.layout_context,
        state=state,
        count=count,
        reason_code=reason_code,
        source=source,
        confidence=confidence,
        requires_human=requires_human,
        evidence=evidence,
    )


def _validate_context(
    field: str,
    app: str,
    profile_id: str,
    profile_version: str,
    snapshot_id: str,
    observed_at: str,
    layout_context: str,
) -> None:
    """校验字段名与 app/profile/snapshot/时间/版式绑定上下文。"""
    if field != LADDER_FIELD:
        raise ValueError("ladder field is invalid")
    if app != APP or not is_nonempty_text(profile_id) or not is_nonempty_text(
        profile_version
    ):
        raise ValueError("ladder profile is invalid")
    if not is_uuid(snapshot_id) or not is_hash(layout_context):
        raise ValueError("ladder snapshot is invalid")
    parse_utc(observed_at)


def _validate_count_reason(
    state: str, count: int | None, reason_code: str | None, source: str
) -> None:
    """校验状态、计数、来源与原因的联动，禁止各自合法但互相矛盾。"""
    if count is not None and (type(count) is not int or count < 0):
        raise ValueError("field count is invalid")
    if state == "present":
        if reason_code is not None or count is not None and count <= 0:
            raise ValueError("present field is invalid")
        if source == "pixel" and count is not None:
            raise ValueError("pixel present count must be null")
        return
    if state == "absent":
        if count != 0 or reason_code is not None:
            raise ValueError("absent field is invalid")
        return
    if count is not None:
        raise ValueError("unresolved field count must be null")
    if state == "unknown":
        if reason_code not in FIELD_UNKNOWN_REASONS:
            raise ValueError("unknown field reason is invalid")
        return
    if state == "failed":
        if reason_code not in FIELD_FAILED_REASONS:
            raise ValueError("failed field reason is invalid")
        return
    raise ValueError("field state is invalid")


def _reason(reasons: list[str]) -> str:
    """按未知原因优先级选择唯一原因码。"""
    for name in FIELD_REASON_PRIORITY:
        if name in reasons:
            return name
    return "ambiguous_signal"
