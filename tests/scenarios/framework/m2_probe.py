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
        else:
            connection.execute(
                "UPDATE hr_discoveries SET state=?, reason_code=?, updated_at=?",
                (state, reason, updated_at),
            )
        connection.commit()
        signature = _signature(connection)
    finally:
        connection.close()
    (db.parent / f"{db.name}.baseline.json").write_text(
        json.dumps({"signature": signature}, sort_keys=True), encoding="utf-8"
    )
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
    events = [
        list(row)
        for row in connection.execute(
            "SELECT app,subject_key,event,reason_code,created_at,payload_json FROM events ORDER BY event_id"
        ).fetchall()
    ]
    return canonical_hash([conversations, discoveries, devices, events])


def _inspect(db: Path) -> dict:
    """固定白名单只读返回发现状态与事件计数。"""
    connection = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            'SELECT state, reason_code, updated_at, attempt, "key", conversation_key, fingerprint '
            'FROM hr_discoveries ORDER BY "key"'
        ).fetchall()
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
        baseline_path = db.parent / f"{db.name}.baseline.json"
        unchanged = None
        if baseline_path.is_file():
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            unchanged = baseline.get("signature") == signature
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
            "event_counts": counts,
            "state_signature": signature,
            "unchanged_since_seed": unchanged,
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
