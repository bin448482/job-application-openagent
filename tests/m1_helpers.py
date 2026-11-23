"""提供 M1 测试用的合成 ReadResult/Evidence、假时钟与临时库辅助，不连接设备。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.schema import initialize_database
from app.domain.models import (
    Candidate,
    Evidence,
    ReadResult,
    UnreadRow,
    UnreadSignal,
    UnreadSnapshot,
    canonical_hash,
    conversation_key_for,
    discovery_key_for,
    fingerprint_for,
    format_utc,
    identity_hash_for,
    parse_utc,
)

BASE_TIME = "2026-11-24T00:00:00.000Z"
SIGNAL_KEYS = ("row_badge", "new_greeting", "tab_badge", "preview_delta")


class FakeClock:
    """同时推进墙钟与单调时钟，用于隔离 TTL 与 clock_changed 测试。"""

    def __init__(self, start: str = BASE_TIME) -> None:
        """保存初始 UTC 时间与固定的单调起点。"""
        self._wall = parse_utc(start)
        self._mono = 1000.0

    def now(self) -> str:
        """返回当前墙钟的规范毫秒 UTC 字符串。"""
        return format_utc(self._wall)

    def monotonic(self) -> float:
        """返回当前单调秒数，供仓储比较墙钟增量。"""
        return self._mono

    def advance(self, seconds: float, mono_seconds: float | None = None) -> None:
        """默认同步推进墙钟与单调时钟；显式传 mono_seconds 可模拟时钟异常。"""
        self._wall += timedelta(seconds=seconds)
        self._mono += seconds if mono_seconds is None else mono_seconds


def open_store(
    directory: str | Path,
    *,
    clock: FakeClock | None = None,
    local_host: str = "host-a",
    stop_checker: object | None = None,
    fault_hook: object | None = None,
) -> tuple[Path, SqliteStore, FakeClock]:
    """在临时目录初始化 v1 库并返回路径、仓储与可控时钟。"""
    path = Path(directory) / "runtime.sqlite3"
    initialize_database(path)
    active_clock = clock if clock is not None else FakeClock()
    store = SqliteStore(
        path,
        local_host=local_host,
        monotonic=active_clock.monotonic,
        stop_checker=stop_checker if stop_checker is not None else lambda pid: "stopped",
        fault_hook=fault_hook,
    )
    return path, store, active_clock


def _signal(state: str = "unknown") -> UnreadSignal:
    """构造默认未知信号，避免测试夹具自身触发信号组合校验。"""
    if state == "unknown":
        return UnreadSignal("unknown", None, "missing_signal")
    raise ValueError("unsupported synthetic signal")


def make_result(
    *,
    observed_at: str = BASE_TIME,
    contact: str = "合成联系人",
    company: str = "合成公司",
    position: str = "合成职位",
    preview: str | None = "合成预览",
    time_hint: str | None = "10:00",
    unread_count: int | None = 1,
    kind: str = "unread",
    sources: tuple[str, ...] = ("row_badge",),
    snapshot_id: str | None = None,
    status: str = "ok",
    reason_code: str | None = None,
    app: str = "boss",
    profile_id: str = "profile-1",
    unread_state: str = "present",
    coverage: str = "viewport",
    confidence: str = "high",
    source: str = "fixture",
) -> ReadResult:
    """按合成身份构造三态读取结果；ok 时重算一致的键与指纹。"""
    if status != "ok":
        return ReadResult(
            status,
            None,
            reason_code
            if reason_code is not None
            else ("device_disconnected" if status == "failed" else "unread_unverified"),
            None,
            observed_at,
            source,
            "low",
        )
    identity_hash = identity_hash_for(contact, company, position)
    conversation_key = conversation_key_for(app, contact, company, position)
    preview_hash = canonical_hash(preview) if preview is not None else None
    row = UnreadRow(
        conversation_key=conversation_key,
        identity_hash=identity_hash,
        preview_hash=preview_hash,
        time_hint=time_hint,
        unread_count=unread_count,
    )
    fingerprint = fingerprint_for(preview_hash, time_hint, unread_count)
    candidate = Candidate(
        candidate_key=discovery_key_for(app, conversation_key, fingerprint),
        conversation_key=conversation_key,
        kind=kind,
        source_names=sources,
        fingerprint=fingerprint,
    )
    snapshot = UnreadSnapshot(
        app=app,
        profile_id=profile_id,
        coverage=coverage,
        signals={name: _signal() for name in SIGNAL_KEYS},
        rows=(row,),
        candidates=(candidate,),
        unread_state=unread_state,
    )
    return ReadResult(
        "ok",
        snapshot,
        None,
        snapshot_id if snapshot_id is not None else str(uuid.uuid4()),
        observed_at,
        source,
        confidence,
    )


def make_evidence(
    *,
    outcome: str = "unknown",
    reason_code: str | None = None,
    effect: str | None = None,
    before_snapshot_id: str | None = None,
    after_snapshot_id: str | None = None,
    observed_at: str = BASE_TIME,
    action_key: str = "boss:conv:合成:reply",
) -> Evidence:
    """按动作三态构造合法合成证据，非法组合由显式覆盖触发。"""
    if outcome == "performed":
        active_effect = effect if effect is not None else "sent"
        active_before = before_snapshot_id or str(uuid.uuid4())
        active_after = after_snapshot_id or str(uuid.uuid4())
        active_reason = reason_code
    elif outcome == "not_performed":
        active_effect = effect if effect is not None else "none"
        active_before = before_snapshot_id
        active_after = after_snapshot_id
        active_reason = (
            reason_code if reason_code is not None else "precondition_failed"
        )
    else:
        active_effect = effect if effect is not None else "none"
        active_before = before_snapshot_id
        active_after = after_snapshot_id
        active_reason = (
            reason_code if reason_code is not None else "result_unverified"
        )
    return Evidence(
        action_key=action_key,
        outcome=outcome,
        effect=active_effect,
        reason_code=active_reason,
        before_snapshot_id=active_before,
        after_snapshot_id=active_after,
        observed_at=observed_at,
    )


def fetch_all(path: Path, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    """用独立只读连接读取行，验证事务已提交或已回滚。"""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def fetch_one(path: Path, sql: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
    """用独立连接读取第一行；不存在返回 None。"""
    rows = fetch_all(path, sql, params)
    return rows[0] if rows else None


def event_count(path: Path, event: str | None = None) -> int:
    """统计事件总数或指定事件名数量。"""
    if event is None:
        rows = fetch_all(path, "SELECT COUNT(*) AS total FROM events")
    else:
        rows = fetch_all(path, "SELECT COUNT(*) AS total FROM events WHERE event = ?", (event,))
    return int(rows[0]["total"])


def database_dump(path: Path) -> dict[str, object]:
    """导出五表全部行与结构版本，用于幂等和回滚比较。"""
    tables = (
        "hr_conversations",
        "hr_discoveries",
        "hr_messages",
        "device_lease",
        "events",
    )
    return {
        table: fetch_all(path, f"SELECT * FROM {table} ORDER BY 1, 2")
        for table in tables
    }


def sources_json(sources: tuple[str, ...]) -> str:
    """把来源名列表序列化为期望的紧凑 JSON 文本。"""
    return json.dumps(list(sources), ensure_ascii=False, separators=(",", ":"))
