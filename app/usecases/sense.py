"""编排 M2 未读快照的持久化、领域聚合、有序停止原因与唯一 JSON 结果。"""

from __future__ import annotations

import time
from dataclasses import replace

from app.domain.models import ReadResult, UnreadSnapshot, validate_read_result
from app.domain.scheduler import aggregate_unread, stop_reason_for
from app.ports.store import StorePort
from app.process_log import ProcessLog, elapsed_ms
from app.usecases.payload import SCHEMA_VERSION, result_payload


def persist_and_aggregate(
    store: StorePort,
    result: ReadResult,
    process_log: ProcessLog | None = None,
) -> tuple[ReadResult, str | None]:
    """先 upsert 快照并查询未关闭发现，再纯聚合与判定 stop_reason。"""
    if result.status != "ok" or result.value is None:
        raise ValueError("only ok snapshots are persistable")
    if not isinstance(result.value, UnreadSnapshot):
        raise ValueError("only unread snapshots are persistable")
    queued_keys = store.upsert_snapshot(result)
    unresolved_keys = store.unresolved_conversations(result.value.app)

    aggregate_started = time.perf_counter()
    if process_log is not None:
        process_log.record(stage="sense", operation="aggregate", result="started")
    try:
        snapshot = aggregate_unread(result.value, unresolved_keys)
    except Exception:
        if process_log is not None:
            process_log.record(
                stage="sense",
                operation="aggregate",
                result="failed",
                error_category="internal",
                duration_ms=elapsed_ms(aggregate_started),
            )
        raise
    else:
        if process_log is not None:
            process_log.record(
                stage="sense",
                operation="aggregate",
                result="ok",
                duration_ms=elapsed_ms(aggregate_started),
            )
    aggregated = replace(result, value=snapshot)
    validate_read_result(aggregated)

    stop_started = time.perf_counter()
    if process_log is not None:
        process_log.record(stage="sense", operation="stop_reason", result="started")
    try:
        reason = stop_reason_for(snapshot, queued_keys)
    except Exception:
        if process_log is not None:
            process_log.record(
                stage="sense",
                operation="stop_reason",
                result="failed",
                error_category="internal",
                duration_ms=elapsed_ms(stop_started),
            )
        raise
    else:
        if process_log is not None:
            process_log.record(
                stage="sense",
                operation="stop_reason",
                result="ok",
                duration_ms=elapsed_ms(stop_started),
            )
    return aggregated, reason


def build_sense_payload(
    result: ReadResult, stop_reason: str | None
) -> dict[str, object]:
    """把三态读取结果与停止原因组装为固定外层 JSON 对象。"""
    validate_read_result(result)
    if result.value is not None and not isinstance(result.value, UnreadSnapshot):
        raise ValueError("sense requires an unread snapshot")
    return {
        "schema_version": SCHEMA_VERSION,
        "result": result_payload(result),
        "stop_reason": stop_reason,
    }
