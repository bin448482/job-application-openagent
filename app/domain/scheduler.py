"""实现 M2 未读快照的纯聚合与有序停止原因判定，不做任何 IO。"""

from __future__ import annotations

from dataclasses import replace

from app.domain.models import (
    APP,
    SIGNAL_KEYS,
    UnreadSnapshot,
    is_discovery_key,
    is_conversation_key,
    validate_unread_snapshot,
)

STOP_REASONS = frozenset(
    {
        "unread_unverified",
        "needs_targeted_verification",
    }
)
_LOCAL_SIGNAL_KEYS = ("row_badge", "new_greeting", "tab_badge")


def aggregate_unread(
    snapshot: UnreadSnapshot, unresolved_keys: list[str]
) -> UnreadSnapshot:
    """按四源结论与未关闭发现重算 unread_state，不改信号、行或候选。"""
    validate_unread_snapshot(snapshot)
    if not isinstance(unresolved_keys, (list, tuple)):
        raise ValueError("unresolved keys are invalid")
    seen: set[str] = set()
    for key in unresolved_keys:
        if not is_conversation_key(APP, key) or key in seen:
            raise ValueError("unresolved key is invalid")
        seen.add(key)

    applicable = [
        signal
        for signal in snapshot.signals.values()
        if signal.state != "not_applicable"
    ]
    if any(signal.state == "present" for signal in applicable):
        state = "present"
    elif (
        snapshot.coverage == "all"
        and applicable
        and all(signal.state == "absent" for signal in applicable)
        and all(row.conversation_key is not None for row in snapshot.rows)
        and not seen
    ):
        state = "absent"
    else:
        state = "unknown"
    return replace(snapshot, unread_state=state)


def stop_reason_for(
    snapshot: UnreadSnapshot, queued_keys: list[str]
) -> str | None:
    """按实施计划 M2 的顺序判定可调度停止原因；null 表示本轮可继续。"""
    validate_unread_snapshot(snapshot)
    if not isinstance(queued_keys, (list, tuple)):
        raise ValueError("queued keys are invalid")
    queued: set[str] = set()
    for key in queued_keys:
        if not is_discovery_key(APP, key) or key in queued:
            raise ValueError("queued key is invalid")
        queued.add(key)

    unread_candidates = [
        candidate for candidate in snapshot.candidates if candidate.kind == "unread"
    ]
    if any(candidate.candidate_key in queued for candidate in unread_candidates):
        return None

    local_signals = [snapshot.signals[name] for name in _LOCAL_SIGNAL_KEYS]
    if any(signal.state == "present" for signal in local_signals) or unread_candidates:
        return "needs_targeted_verification"

    if any(
        signal.state == "unknown" and signal.state != "not_applicable"
        for signal in local_signals
    ):
        return "unread_unverified"
    if snapshot.unread_state == "unknown":
        return "unread_unverified"

    preview = snapshot.signals["preview_delta"]
    if (
        preview.state == "present"
        and all(
            signal.state in {"absent", "not_applicable"}
            for signal in local_signals
        )
        and any(
            candidate.kind == "new_message" and candidate.candidate_key in queued
            for candidate in snapshot.candidates
        )
    ):
        return None

    if snapshot.unread_state == "absent":
        return None
    return "needs_targeted_verification"
