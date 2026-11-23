"""编排 M2 未读快照的持久化、领域聚合、有序停止原因与唯一 JSON 结果。"""

from __future__ import annotations

from dataclasses import replace

from app.domain.models import (
    SIGNAL_KEYS,
    ReadResult,
    UnreadSnapshot,
    validate_read_result,
)
from app.domain.scheduler import aggregate_unread, stop_reason_for
from app.ports.store import StorePort

SCHEMA_VERSION = 1


def persist_and_aggregate(
    store: StorePort, result: ReadResult
) -> tuple[ReadResult, str | None]:
    """先 upsert 快照并查询未关闭发现，再纯聚合与判定 stop_reason。"""
    if result.status != "ok" or result.value is None:
        raise ValueError("only ok snapshots are persistable")
    queued_keys = store.upsert_snapshot(result)
    unresolved_keys = store.unresolved_conversations(result.value.app)
    snapshot = aggregate_unread(result.value, unresolved_keys)
    aggregated = replace(result, value=snapshot)
    validate_read_result(aggregated)
    return aggregated, stop_reason_for(snapshot, queued_keys)


def build_sense_payload(
    result: ReadResult, stop_reason: str | None
) -> dict[str, object]:
    """把三态读取结果与停止原因组装为固定外层 JSON 对象。"""
    validate_read_result(result)
    return {
        "schema_version": SCHEMA_VERSION,
        "result": result_payload(result),
        "stop_reason": stop_reason,
    }


def result_payload(result: ReadResult) -> dict[str, object]:
    """序列化读取结果的全部固定键，不新增审计或原文字段。"""
    validate_read_result(result)
    return {
        "status": result.status,
        "value": _snapshot_payload(result.value) if result.value is not None else None,
        "reason_code": result.reason_code,
        "snapshot_id": result.snapshot_id,
        "observed_at": result.observed_at,
        "source": result.source,
        "confidence": result.confidence,
    }


def _snapshot_payload(snapshot: UnreadSnapshot) -> dict[str, object]:
    """序列化未读快照的四源、行与候选，不包含聊天正文。"""
    return {
        "app": snapshot.app,
        "profile_id": snapshot.profile_id,
        "coverage": snapshot.coverage,
        "signals": {
            name: {
                "state": snapshot.signals[name].state,
                "count": snapshot.signals[name].count,
                "reason_code": snapshot.signals[name].reason_code,
            }
            for name in SIGNAL_KEYS
        },
        "rows": [
            {
                "conversation_key": row.conversation_key,
                "identity_hash": row.identity_hash,
                "preview_hash": row.preview_hash,
                "time_hint": row.time_hint,
                "unread_count": row.unread_count,
            }
            for row in snapshot.rows
        ],
        "candidates": [
            {
                "candidate_key": candidate.candidate_key,
                "conversation_key": candidate.conversation_key,
                "kind": candidate.kind,
                "source_names": list(candidate.source_names),
                "fingerprint": candidate.fingerprint,
            }
            for candidate in snapshot.candidates
        ],
        "unread_state": snapshot.unread_state,
    }
