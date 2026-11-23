"""在隔离根内调用真实 M2 解析/合并/持久化接口，并做受限合成初态/只读观察。

只用于无法由 `app sense` CLI 注入的 now、终态/未关闭发现与时间倒退场景；不复制
业务逻辑，不读 expected，不连接设备/模型/网络。SQL 仅用于合成初态 seam 或只读
观察，生产状态转换仍由真实 `persist_and_aggregate`/CLI 完成。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path.cwd().resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.boss.list import load_and_parse_snapshot
from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.schema import initialize_database
from app.domain.models import canonical_hash
from app.process_log import ProcessLog
from app.usecases.sense import build_sense_payload, persist_and_aggregate


class ProbeError(RuntimeError):
    """表示 M2 探针请求或路径不满足受控协议。"""


def _within(child: Path, parent: Path) -> bool:
    """判断路径是否仍在父目录内。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_like(path: Path) -> bool:
    """识别 symlink 与 Windows junction。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _reject_links_on_path(path: Path, root: Path, label: str) -> None:
    """先检查原始 leaf/祖先，再检查根祖先；不得先 resolve。"""
    current = path
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 路径含链接或 junction")
        if current == root or current.parent == current:
            break
        current = current.parent
    current = root
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 根的祖先含链接或 junction")
        if current.parent == current:
            break
        current = current.parent


def _relative(value: object, label: str, *, must_exist: bool) -> Path:
    """只接受隔离根内相对路径，先查链接再 resolve/containment。"""
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{label} 必须是非空相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProbeError(f"{label} 不得绝对或包含 ..")
    lexical = ROOT / candidate
    _reject_links_on_path(lexical, ROOT, label)
    resolved = lexical.resolve()
    if not _within(resolved, ROOT):
        raise ProbeError(f"{label} 越出隔离根")
    if must_exist and not resolved.is_file():
        raise ProbeError(f"{label} 不存在")
    return resolved


def _baseline_path(db: Path) -> Path:
    """返回与合成库同目录的只读签名基线文件。"""
    return db.parent / f"{db.name}.baseline.json"


def _stable_signature(connection: sqlite3.Connection) -> str:
    """只对成功 sense 不得改变的基线、消息与设备做前后签名。"""
    conversations = [
        list(row)
        for row in connection.execute(
            "SELECT app,key,baseline_preview_hash,baseline_time_hint,verified_at "
            "FROM hr_conversations ORDER BY key"
        ).fetchall()
    ]
    messages = [
        list(row)
        for row in connection.execute(
            "SELECT app,key,conversation_key,inbound_hash,observed_at FROM hr_messages ORDER BY key"
        ).fetchall()
    ]
    devices = [
        list(row)
        for row in connection.execute(
            "SELECT device_key,lease_owner,owner_pid,owner_host,nonce,lease_expires_at,reservation_state "
            "FROM device_lease ORDER BY device_key"
        ).fetchall()
    ]
    return canonical_hash([conversations, messages, devices])


def _protected_task_signature(connection: sqlite3.Connection) -> str:
    """对成功或 defer 场景不得改变的任务负载与全部租约字段做前后签名。

    `source_names_json` 与 `updated_at` 是冻结合同允许合并变化的字段，故排除。
    """
    rows = [
        list(row)
        for row in connection.execute(
            "SELECT app,\"key\",conversation_key,kind,fingerprint,snapshot_id,opened,"
            "priority,created_at,attempt,reason_code,lease_owner,owner_pid,"
            "lease_expires_at,owner_host,nonce FROM hr_discoveries ORDER BY app,\"key\""
        ).fetchall()
    ]
    return canonical_hash(rows)


def _write_baseline(db: Path, signature: str, stable: str, protected: str) -> None:
    """把完整回滚签名、成功路径稳定签名与被保护任务签名写为比较基线。"""
    _baseline_path(db).write_text(
        json.dumps(
            {"signature": signature, "stable": stable, "protected": protected},
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _record_baseline(db: Path) -> dict:
    """只读记录当前库签名，供成功 sense 前后不推进断言。"""
    if not db.is_file():
        raise ProbeError("db 不存在")
    connection = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        signature = _signature(connection)
        stable = _stable_signature(connection)
        protected = _protected_task_signature(connection)
    finally:
        connection.close()
    _write_baseline(db, signature, stable, protected)
    return {
        "baseline_recorded": True,
        "state_signature": signature,
        "stable_signature": stable,
        "protected_task_signature": protected,
    }


def _parse(request: dict) -> dict:
    """调用真实 load_and_parse_snapshot；now 缺省时仍由生产契约使用 observed_at。"""
    fixture = _relative(request.get("fixture"), "fixture", must_exist=True)
    snapshot_id = request.get("snapshot_id") or str(uuid.uuid4())
    now = request.get("now")
    if now is not None and not isinstance(now, str):
        raise ProbeError("now 必须为字符串")
    result = load_and_parse_snapshot(fixture, snapshot_id, now)
    reason = result.reason_code if result.status != "ok" else None
    return {"payload": build_sense_payload(result, reason)}


def _seed(request: dict, db: Path) -> dict:
    """用生产解析+仓储创建合成发现，再用限定 SQL 设置初态。"""
    fixture = _relative(request.get("fixture"), "fixture", must_exist=True)
    now = request.get("now")
    if not isinstance(now, str):
        raise ProbeError("seed 需要规范 now")
    snapshot_id = request.get("snapshot_id") or str(uuid.uuid4())
    state = request.get("state")
    if state not in {"queued", "running", "blocked", "done"}:
        raise ProbeError("seed state 非法")
    reason = request.get("reason_code")
    if state in {"blocked"} and not isinstance(reason, str):
        raise ProbeError("blocked seed 需要 reason")
    if state in {"queued", "running", "done"} and reason is not None:
        raise ProbeError("非 blocked seed reason 必须为 null")
    initialize_database(db)
    fixtures = [fixture]
    extra = request.get("extra_fixture")
    if extra is not None:
        fixtures.append(_relative(extra, "extra_fixture", must_exist=True))
    log = ProcessLog(path=db.parent / f"m2-probe-{uuid.uuid4().hex}.jsonl")
    store = SqliteStore(db, process_log=log)
    try:
        for index, active_fixture in enumerate(fixtures):
            active_snapshot = snapshot_id if index == 0 else str(uuid.uuid4())
            result = load_and_parse_snapshot(active_fixture, active_snapshot, now)
            if result.status != "ok" or result.value is None:
                raise ProbeError("seed 解析结果非 ok")
            store.upsert_snapshot(result)
    finally:
        store.close()
    updated_at = request.get("updated_at", now)
    if not isinstance(updated_at, str):
        raise ProbeError("updated_at 非法")
    connection = sqlite3.connect(str(db))
    try:
        if state == "running":
            connection.execute(
                "UPDATE hr_discoveries SET state=?, reason_code=NULL, updated_at=?, "
                "lease_owner=?, owner_pid=?, owner_host=?, nonce=?, lease_expires_at=?",
                (
                    state,
                    updated_at,
                    "00000000-0000-4000-8000-000000000031",
                    1,
                    "a" * 64,
                    "00000000-0000-4000-8000-000000000032",
                    "2026-11-24T00:01:00.000Z",
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO device_lease "
                "(device_key, lease_owner, owner_pid, owner_host, nonce, lease_expires_at, reservation_state) "
                "VALUES ('primary', ?, ?, ?, ?, ?, 'held')",
                (
                    "00000000-0000-4000-8000-000000000031",
                    1,
                    "a" * 64,
                    "00000000-0000-4000-8000-000000000032",
                    "2026-11-24T00:01:00.000Z",
                ),
            )
        elif state == "blocked":
            connection.execute(
                "UPDATE hr_discoveries SET state=?, reason_code=?, updated_at=?, "
                "lease_owner=NULL, owner_pid=NULL, owner_host=NULL, nonce=NULL, lease_expires_at=NULL",
                (state, reason, updated_at),
            )
            connection.execute(
                "INSERT OR REPLACE INTO device_lease "
                "(device_key, lease_owner, owner_pid, owner_host, nonce, lease_expires_at, reservation_state) "
                "VALUES ('primary', ?, ?, ?, ?, ?, 'quarantined')",
                (
                    "00000000-0000-4000-8000-000000000031",
                    1,
                    "a" * 64,
                    "00000000-0000-4000-8000-000000000032",
                    "2026-11-24T00:01:00.000Z",
                ),
            )
        else:
            connection.execute(
                "UPDATE hr_discoveries SET state=?, reason_code=?, updated_at=?",
                (state, reason, updated_at),
            )
        connection.commit()
        signature = _signature(connection)
        stable = _stable_signature(connection)
        protected = _protected_task_signature(connection)
    finally:
        connection.close()
    _write_baseline(db, signature, stable, protected)
    return {
        "seeded": len(fixtures),
        "state": state,
        "reason_code": reason,
        "updated_at": updated_at,
        "state_signature": signature,
    }


def _signature(connection: sqlite3.Connection) -> str:
    """对四张合成表与事件做只读完整签名，用于整批回滚/基线不推进断言。"""
    conversations = [
        list(row)
        for row in connection.execute(
            "SELECT app,key,identity_hash,baseline_preview_hash,baseline_time_hint,verified_at "
            "FROM hr_conversations ORDER BY key"
        ).fetchall()
    ]
    discoveries = [
        list(row)
        for row in connection.execute(
            'SELECT app,key,conversation_key,kind,fingerprint,source_names_json,snapshot_id,'
            'opened,state,priority,created_at,updated_at,lease_owner,nonce,lease_expires_at,'
            'owner_host,owner_pid,attempt,reason_code FROM hr_discoveries ORDER BY "key"'
        ).fetchall()
    ]
    devices = [
        list(row)
        for row in connection.execute(
            "SELECT device_key,lease_owner,owner_pid,owner_host,nonce,lease_expires_at,reservation_state "
            "FROM device_lease ORDER BY device_key"
        ).fetchall()
    ]
    messages = [
        list(row)
        for row in connection.execute(
            "SELECT app,key,conversation_key,inbound_hash,observed_at FROM hr_messages ORDER BY key"
        ).fetchall()
    ]
    events = [
        list(row)
        for row in connection.execute(
            "SELECT event_id,app,subject_key,event,reason_code,created_at,payload_json "
            "FROM events ORDER BY event_id"
        ).fetchall()
    ]
    return canonical_hash([conversations, discoveries, devices, messages, events])


def _inspect(db: Path) -> dict:
    """固定白名单只读返回发现状态与事件计数。"""
    connection = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            'SELECT state, reason_code, updated_at, attempt, "key", conversation_key, fingerprint, '
            'source_names_json, lease_owner, owner_pid, owner_host, nonce, lease_expires_at '
            'FROM hr_discoveries ORDER BY "key"'
        ).fetchall()
        device = connection.execute(
            "SELECT reservation_state, lease_owner, owner_pid, owner_host, nonce, lease_expires_at "
            "FROM device_lease WHERE device_key='primary'"
        ).fetchone()
        device_count = connection.execute("SELECT COUNT(*) FROM device_lease").fetchone()[0]
        conversations = connection.execute(
            "SELECT baseline_preview_hash, baseline_time_hint, verified_at "
            "FROM hr_conversations ORDER BY key"
        ).fetchall()
        message_count = connection.execute("SELECT COUNT(*) FROM hr_messages").fetchone()[0]
        events = [
            str(row[0])
            for row in connection.execute("SELECT event FROM events ORDER BY event_id").fetchall()
        ]
        counts: dict[str, int] = {
            name: 0
            for name in (
                "snapshot_upserted",
                "candidate_superseded",
                "candidate_deferred",
                "task_claimed",
                "task_started",
                "lease_renewed",
                "task_finished",
                "lease_recovered",
            )
        }
        for event in events:
            counts[event] = counts.get(event, 0) + 1
        signature = _signature(connection)
        state_counts: dict[str, int] = {}
        for row in rows:
            state_counts[str(row[0])] = state_counts.get(str(row[0]), 0) + 1
        baseline_path = _baseline_path(db)
        unchanged = None
        stable_unchanged = None
        protected_unchanged = None
        if baseline_path.is_file():
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            unchanged = baseline.get("signature") == signature
            stable_unchanged = baseline.get("stable") == _stable_signature(connection)
            protected_unchanged = baseline.get("protected") == _protected_task_signature(connection)
        return {
            "task_count": len(rows),
            "states": [str(row[0]) for row in rows],
            "state_counts": state_counts,
            "reasons": [row[1] for row in rows],
            "updated_at": [str(row[2]) for row in rows],
            "attempt": [int(row[3]) for row in rows],
            "keys": [str(row[4]) for row in rows],
            "conversation_keys": [row[5] for row in rows],
            "fingerprints": [row[6] for row in rows],
            "source_names": [json.loads(row[7]) for row in rows],
            "lease_owner": [row[8] for row in rows],
            "owner_pid": [row[9] for row in rows],
            "owner_host": [row[10] for row in rows],
            "nonce_present": [row[11] is not None for row in rows],
            "nonces": [row[11] for row in rows],
            "lease_expires_at": [row[12] for row in rows],
            "device_present": device is not None,
            "device_count": int(device_count),
            "device_reservation_state": None if device is None else device[0],
            "device_lease_owner": None if device is None else device[1],
            "device_owner_pid": None if device is None else device[2],
            "device_owner_host": None if device is None else device[3],
            "device_nonce": None if device is None else device[4],
            "device_lease_expires_at": None if device is None else device[5],
            "conversation_count": len(conversations),
            "baseline_preview_hashes": [row[0] for row in conversations],
            "baseline_time_hints": [row[1] for row in conversations],
            "baseline_verified": [row[2] is not None for row in conversations],
            "message_count": int(message_count),
            "device_owner_matches_task": (
                device is not None
                and any(
                    row[8] == device[1]
                    and row[9] == device[2]
                    and row[10] == device[3]
                    and row[11] == device[4]
                    and row[12] == device[5]
                    for row in rows
                )
            ),
            "event_counts": counts,
            "state_signature": signature,
            "unchanged_since_seed": unchanged,
            "stable_unchanged_since_baseline": stable_unchanged,
            "protected_task_unchanged_since_baseline": protected_unchanged,
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    """读取 JSON 请求并输出单对象结果；不输出 traceback 或路径。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    try:
        request_path = _relative(args.request, "request", must_exist=True)
        db_path = _relative(args.db, "db", must_exist=False)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ProbeError("请求必须是对象")
        mode = request.get("mode")
        if mode == "parse":
            payload = _parse(request)
        elif mode == "seed":
            payload = _seed(request, db_path)
        elif mode == "baseline":
            payload = _record_baseline(db_path)
        elif mode == "inspect":
            payload = _inspect(db_path)
        else:
            raise ProbeError("未知 mode")
        output = {"schema_version": 1, "status": "ok", **payload}
        code = 0
    except Exception as exc:  # noqa: BLE001 - 探针边界不输出 traceback/路径
        output = {
            "schema_version": 1,
            "status": "failed",
            "outcome": type(exc).__name__,
        }
        code = 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
