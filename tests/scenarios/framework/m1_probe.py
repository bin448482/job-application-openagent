"""在隔离根内用纯合成输入驱动 M1 真实 initialize_database/SqliteStore。

只构造领域输入、假时钟、固定 host/UUID/PID 与停止检查器；不复制业务实现，
不执行 ADB/网络/模型，不读真实库或私有事实。SQL 仅用于合成初态、损坏注入或
只读终态核验，不用 SQL 替代生产状态转换。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.schema import initialize_database
from app.domain.models import (
    Candidate,
    Evidence,
    Lease,
    Owner,
    ReadResult,
    UnreadRow,
    UnreadSignal,
    UnreadSnapshot,
    canonical_hash,
    conversation_key_for,
    discovery_key_for,
    fingerprint_for,
    identity_hash_for,
    parse_utc,
)
from app.ports.store import StoreError
from app.process_log import ProcessLog

HOST_LOCAL = canonical_hash("synthetic-m1-host-local")
HOST_OTHER = canonical_hash("synthetic-m1-host-other")
OWNER_ID = "00000000-0000-4000-8000-000000000011"
OWNER_ID_2 = "00000000-0000-4000-8000-000000000012"
SNAPSHOT_ID = "00000000-0000-4000-8000-000000000099"
BASE_TIME = "2026-11-24T00:00:00.000Z"


class ProbeError(RuntimeError):
    """表示脚本或合成输入不满足受控探针协议。"""


def _is_link_like(path: Path) -> bool:
    """识别 symlink 与 Windows junction。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _reject_links_on_path(path: Path, root: Path, label: str) -> None:
    """从原始路径到根逐项检查链接，再检查根的祖先；不做 resolve 前置。"""
    current = path
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 路径含链接或 junction")
        if current == root:
            break
        if current.parent == current:
            break
        current = current.parent
    current = root
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 根的祖先含链接或 junction")
        if current.parent == current:
            break
        current = current.parent


def _relative_path(value: str, root: Path, label: str, *, must_exist: bool) -> Path:
    """只接受隔离根内相对路径；先查原始链接再做 resolve/containment。"""
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{label} 必须是非空相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProbeError(f"{label} 不得是绝对路径或包含 ..")
    lexical = root / candidate
    _reject_links_on_path(lexical, root, label)
    resolved = lexical.resolve()
    if not _within(resolved, root):
        raise ProbeError(f"{label} 解析后越出隔离根")
    if must_exist and not resolved.is_file():
        raise ProbeError(f"{label} 不存在")
    return resolved


def _within(child: Path, parent: Path) -> bool:
    """判断解析后路径是否仍在父目录内。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _barrier_paths(root: Path) -> tuple[Path, str]:
    """校验父 runner 注入的 barrier 目录与名称；只允许根内绝对目录与 0/1。"""
    directory_value = os.environ.get("M1_BARRIER_DIR")
    name = os.environ.get("M1_BARRIER_NAME")
    if not directory_value or not name:
        raise ProbeError("barrier 环境缺失")
    if re.fullmatch(r"[01]", name) is None:
        raise ProbeError("barrier 名称必须为 0 或 1")
    raw_directory = Path(directory_value)
    if not raw_directory.is_absolute():
        raise ProbeError("barrier 目录必须为绝对路径")
    _reject_links_on_path(raw_directory, root, "barrier")
    directory = raw_directory.resolve()
    if not _within(directory, root) or not directory.is_dir():
        raise ProbeError("barrier 目录越界或不存在")
    return directory, name


class FakeClock:
    """同时推进墙钟与单调时钟，避免合成 now 被误判 clock_changed。"""

    def __init__(self, start: str = BASE_TIME) -> None:
        """保存初始墙钟与单调起点。"""
        self._wall = parse_utc(start)
        self._mono = 100.0

    def set_now(self, now: str, mono_delta: float | None = None) -> None:
        """推进墙钟；默认单调钟同步，显式 mono_delta 可制造双钟偏差。"""
        target = parse_utc(now)
        delta = (target - self._wall).total_seconds()
        if delta < 0:
            raise ProbeError("墙钟不得回退")
        if mono_delta is not None:
            if isinstance(mono_delta, bool) or not isinstance(mono_delta, (int, float)):
                raise ProbeError("mono_delta 必须是非负有限数字")
            if not math.isfinite(mono_delta) or mono_delta < 0:
                raise ProbeError("mono_delta 必须是非负有限数字")
        self._mono += delta if mono_delta is None else mono_delta
        self._wall = target

    def monotonic(self) -> float:
        """返回当前单调秒，供仓储比较墙钟增量。"""
        return self._mono


def _owner(owner_id: str = OWNER_ID, pid: object = 4242) -> Owner:
    """构造固定合成 owner；host 只使用本机合成摘要。"""
    return Owner(id=owner_id, pid=pid, host=HOST_LOCAL)


def _snapshot(now: str, kinds: list[str], count: int) -> ReadResult:
    """构造一个合法合成未读快照，候选数量与 kind 由脚本指定。"""
    signals = {
        "row_badge": UnreadSignal("present", count, None),
        "new_greeting": UnreadSignal("unknown", None, "missing_signal"),
        "tab_badge": UnreadSignal("unknown", None, "missing_signal"),
        "preview_delta": UnreadSignal("unknown", None, "no_baseline"),
    }
    rows: list[UnreadRow] = []
    candidates: list[Candidate] = []
    for index in range(count):
        contact = f"合成联系人{index + 1}"
        company = f"合成机构{index + 1}"
        position = f"合成岗位{index + 1}"
        identity = identity_hash_for(contact, company, position)
        conversation = conversation_key_for("boss", contact, company, position)
        preview_hash = canonical_hash(f"合成预览{index + 1}")
        time_hint = "10:00"
        unread_count = 1
        kind = kinds[index % len(kinds)]
        fingerprint = fingerprint_for(preview_hash, time_hint, unread_count)
        rows.append(
            UnreadRow(
                conversation_key=conversation,
                identity_hash=identity,
                preview_hash=preview_hash,
                time_hint=time_hint,
                unread_count=unread_count,
            )
        )
        candidates.append(
            Candidate(
                candidate_key=discovery_key_for("boss", conversation, fingerprint),
                conversation_key=conversation,
                kind=kind,
                source_names=("row_badge",),
                fingerprint=fingerprint,
            )
        )
    snapshot = UnreadSnapshot(
        app="boss",
        profile_id="synthetic-m1",
        coverage="viewport",
        signals=signals,
        rows=tuple(rows),
        candidates=tuple(candidates),
        unread_state="present",
    )
    return ReadResult(
        status="ok",
        value=snapshot,
        reason_code=None,
        snapshot_id=SNAPSHOT_ID,
        observed_at=now,
        source="fixture",
        confidence="high",
    )


def _read_result(now: str, status: str, reason: str | None) -> ReadResult:
    """构造读取三态 proof；ok 使用合法合成快照，其余 value 为空。"""
    if status == "ok":
        return _snapshot(now, ["unread"], 1)
    return ReadResult(
        status=status,
        value=None,
        reason_code=reason,
        snapshot_id=None,
        observed_at=now,
        source="fixture",
        confidence="low",
    )


def _evidence(now: str, outcome: str, reason: str | None) -> Evidence:
    """构造动作证据 proof；不触发真实设备。"""
    if outcome == "performed":
        return Evidence(
            action_key="conversation:synthetic:reply",
            outcome="performed",
            effect="sent",
            reason_code=None,
            before_snapshot_id="00000000-0000-4000-8000-0000000000a1",
            after_snapshot_id="00000000-0000-4000-8000-0000000000a2",
            observed_at=now,
        )
    return Evidence(
        action_key="conversation:synthetic:reply",
        outcome="unknown",
        effect="none",
        reason_code=reason or "result_unverified",
        before_snapshot_id=None,
        after_snapshot_id=None,
        observed_at=now,
    )


def _lease_json(lease: Lease) -> dict:
    """输出不含真实 nonce 值的租约关系摘要。"""
    return {
        "granted": True,
        "key": lease.key,
        "expires_at": lease.expires_at,
        "nonce_present": bool(lease.nonce),
        "owner_host_local": lease.owner.host == HOST_LOCAL,
        "state": "leased",
    }


def _read_connection(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开合成库，供固定白名单 inspect 使用。"""
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _inspect_raw(db_path: Path) -> dict:
    """只读返回版本与对象名，供初始化失败后的终态核验。"""
    connection = _read_connection(db_path)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "tables": tables,
        }
    finally:
        connection.close()


def _device_digest(db_path: Path) -> str | None:
    """读取设备行的固定身份摘要；不存在返回 null。"""
    connection = _read_connection(db_path)
    try:
        row = connection.execute(
            "SELECT lease_owner, owner_pid, owner_host, nonce, lease_expires_at "
            "FROM device_lease WHERE device_key='primary'"
        ).fetchone()
        if row is None:
            return None
        return canonical_hash([row[0], row[1], row[2], row[3], row[4]])
    finally:
        connection.close()


def _capture_state(db_path: Path) -> dict:
    """只读捕获任务/设备/事件的完整签名，供失败回滚与拒绝调用比较。"""
    connection = _read_connection(db_path)
    try:
        tasks = connection.execute(
            "SELECT state, attempt, reason_code, lease_owner, owner_pid, owner_host, "
            'nonce, lease_expires_at, conversation_key FROM hr_discoveries ORDER BY "key"'
        ).fetchall()
        device = connection.execute(
            "SELECT reservation_state, lease_owner, owner_pid, owner_host, nonce, lease_expires_at "
            "FROM device_lease WHERE device_key='primary'"
        ).fetchone()
        events = [
            [str(row[0]), row[1]]
            for row in connection.execute(
                "SELECT event, reason_code FROM events ORDER BY event_id"
            ).fetchall()
        ]
        task_rows = [list(row) for row in tasks]
        device_row = list(device) if device is not None else None
        return {
            "task_signature": canonical_hash(task_rows),
            "task_count": len(task_rows),
            "device_signature": None if device_row is None else canonical_hash(device_row),
            "device_digest": (
                None
                if device_row is None
                else canonical_hash([device_row[1], device_row[2], device_row[3], device_row[4], device_row[5]])
            ),
            "event_signature": canonical_hash(events),
            "event_count": len(events),
        }
    finally:
        connection.close()


def _inspect(
    store: SqliteStore,
    db_path: Path,
    remembered_digest: str | None,
    remembered_state: dict | None,
) -> dict:
    """按固定只读白名单返回任务/设备/事件摘要，不接受任意 SQL。"""
    connection = _read_connection(db_path)
    try:
        tasks = connection.execute(
            "SELECT state, attempt, reason_code, lease_owner, owner_pid, owner_host, "
            'nonce, lease_expires_at, conversation_key FROM hr_discoveries ORDER BY "key"'
        ).fetchall()
        device = connection.execute(
            "SELECT reservation_state, lease_owner, owner_pid, owner_host, nonce, lease_expires_at "
            "FROM device_lease WHERE device_key='primary'"
        ).fetchone()
        events = [
            {"event": str(row[0]), "reason_code": row[1]}
            for row in connection.execute(
                "SELECT event, reason_code FROM events ORDER BY event_id"
            ).fetchall()
        ]
        event_counts: dict[str, int] = {
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
            event_counts[event["event"]] = event_counts.get(event["event"], 0) + 1
        task_views = []
        for task in tasks:
            lease_fields = {
                "lease_owner": task[3] is not None,
                "owner_pid": task[4] is not None,
                "owner_host": task[5] is not None,
                "nonce": task[6] is not None,
                "lease_expires_at": task[7] is not None,
            }
            task_views.append(
                {
                    "state": task[0],
                    "attempt": task[1],
                    "reason_code": task[2],
                    "lease_fields": lease_fields,
                    "lease_fields_present": all(lease_fields.values()),
                    "lease_fields_all_null": not any(lease_fields.values()),
                    "lease_expires_at": task[7],
                    "conversation_key_present": task[8] is not None,
                }
            )
        device_view: dict = {"present": False}
        if device is not None:
            device_fields = [device[1], device[2], device[3], device[4], device[5]]
            digest = canonical_hash(device_fields)
            owner_matches_task = any(
                task[3] == device[1]
                and task[4] == device[2]
                and task[5] == device[3]
                and task[6] == device[4]
                and task[7] == device[5]
                for task in tasks
            )
            device_view = {
                "present": True,
                "reservation_state": device[0],
                "identity_digest": digest,
                "identity_fields_present": all(value is not None for value in device_fields),
                "identity_unchanged": (
                    None if remembered_digest is None else digest == remembered_digest
                ),
                "owner_matches_task": owner_matches_task,
                "deadline_matches_task": any(
                    task[6] is not None and str(task[7]) == str(device[5])
                    for task in tasks
                ),
                "lease_expires_at": device[5],
            }
        task_rows = [list(row) for row in tasks]
        task_signature = canonical_hash(task_rows)
        event_signature = canonical_hash(
            [[event["event"], event["reason_code"]] for event in events]
        )
        current_state = {
            "task_signature": task_signature,
            "device_signature": None if device is None else canonical_hash(list(device)),
            "event_signature": event_signature,
            "event_count": len(events),
        }
        comparisons = {
            "task_unchanged_since_remember": None,
            "device_unchanged_since_remember": None,
            "events_unchanged_since_remember": None,
        }
        if remembered_state is not None:
            comparisons = {
                "task_unchanged_since_remember": task_signature == remembered_state["task_signature"],
                "device_unchanged_since_remember": current_state["device_signature"]
                == remembered_state["device_signature"],
                "events_unchanged_since_remember": event_signature == remembered_state["event_signature"],
            }
        unresolved = store.unresolved_conversations("boss")
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "task_count": len(task_views),
            "queued_count": sum(1 for task in task_views if task["state"] == "queued"),
            "leased_count": sum(1 for task in task_views if task["state"] == "leased"),
            "attempt_total": sum(int(task["attempt"]) for task in task_views),
            "task": task_views[0] if task_views else None,
            "tasks": task_views,
            "device": device_view,
            "events": events,
            "event_counts": event_counts,
            "event_count": len(events),
            "unresolved_count": len(unresolved),
            **comparisons,
        }
    finally:
        connection.close()


def _barrier_wait(root: Path) -> None:
    """写 ready 文件并等待父 runner 的 go 文件，超时则失败。"""
    directory, name = _barrier_paths(root)
    (directory / f"{name}.ready").write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (directory / "go").is_file():
            return
        time.sleep(0.02)
    raise ProbeError("barrier timeout")


def _hold_lock(db_path: Path, root: Path) -> dict:
    """直接持合成库写锁，供 busy_timeout 场景使用；不改业务行。"""
    directory, _ = _barrier_paths(root)
    connection = sqlite3.connect(str(db_path), timeout=5)
    try:
        ready_peer = directory / "1.ready"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready_peer.is_file():
            time.sleep(0.02)
        connection.execute("BEGIN IMMEDIATE")
        _barrier_wait(root)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (directory / "release").is_file():
                break
            time.sleep(0.02)
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    return {"lock": "held_then_released"}


def _locked_claim(store: SqliteStore, db_path: Path, now: str) -> dict:
    """用第二条连接持写锁后调用真实 claim，验证 store_busy 出口。"""
    holder = sqlite3.connect(str(db_path), timeout=5)
    try:
        holder.execute("BEGIN IMMEDIATE")
        try:
            lease = store.claim("boss", "unread", _owner("00000000-0000-4000-8000-000000000028"), now)
        except StoreError as error:
            return {"claimed": False, "error": error.reason_code}
        return {"claimed": True, "lease": _lease_json(lease) if lease else None}
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _prepare(db_path: Path, mode: str, value: object) -> dict:
    """执行限定枚举的合成初态/损坏注入，不接受任意 SQL。"""
    connection = sqlite3.connect(str(db_path))
    try:
        if mode == "set_user_version":
            if type(value) is not int:
                raise ProbeError("version must be integer")
            connection.execute(f"PRAGMA user_version = {value}")
        elif mode == "add_user_table":
            connection.execute("CREATE TABLE synthetic_extra (id INTEGER)")
        elif mode == "drop_index":
            row = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL "
                "ORDER BY name LIMIT 1"
            ).fetchone()
            if row is None:
                raise ProbeError("no index to drop")
            connection.execute(f'DROP INDEX "{row[0]}"')
        else:
            raise ProbeError("unknown prepare mode")
        connection.commit()
    finally:
        connection.close()
    return {"prepared": mode}


def run_request(request: dict, db_path: Path, root: Path) -> dict:
    """按脚本顺序执行受控操作；预期 StoreError 作为数据返回。"""
    steps = request.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ProbeError("steps must be non-empty")
    log_value = request.get("log_path")
    if log_value is None:
        log_value = f"{db_path.relative_to(root).as_posix()}.probe-{uuid.uuid4().hex}.jsonl"
    log_path = _relative_path(str(log_value), root, "log_path", must_exist=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    clock = FakeClock(request.get("start_at", BASE_TIME))
    store: SqliteStore | None = None
    lease: Lease | None = None
    previous_lease: Lease | None = None
    remembered_digest: str | None = None
    remembered_state: dict | None = None
    stop_mode = "stopped"
    store_fault = None
    results: list[dict] = []
    last_outcome = "script_done"

    def open_store(store_host: str) -> SqliteStore:
        """按脚本 host 选项打开真实 SqliteStore，使用独立 ProcessLog。"""
        local_host = HOST_LOCAL if store_host == "local" else HOST_OTHER
        return SqliteStore(
            db_path,
            local_host=local_host,
            monotonic=clock.monotonic,
            stop_checker=lambda pid: stop_mode,
            fault_hook=store_fault,
            process_log=ProcessLog(path=log_path),
        )

    for step in steps:
        if not isinstance(step, dict) or "op" not in step:
            raise ProbeError("step must be an object with op")
        op = step["op"]
        now = step.get("now")
        if isinstance(now, str):
            clock.set_now(now, step.get("mono_delta"))
        try:
            if op == "barrier":
                _barrier_wait(root)
                result = {"barrier": "passed"}
                last_outcome = "barrier_passed"
            elif op == "release":
                directory, _ = _barrier_paths(root)
                (directory / "release").write_text("release", encoding="utf-8")
                result = {"released": True}
                last_outcome = "released"
            elif op == "init":
                fault = step.get("fault_at")
                hook = None
                if isinstance(fault, int) and not isinstance(fault, bool):
                    def hook(index: int, connection: sqlite3.Connection) -> None:
                        """在合成建表序号达到阈值时注入失败。"""
                        if index >= fault:
                            raise RuntimeError("synthetic init failure")
                initialize_database(db_path, fault_hook=hook)
                result = {"initialized": True}
                last_outcome = "initialized"
            elif op == "prepare":
                result = _prepare(db_path, str(step.get("mode")), step.get("value"))
                last_outcome = "prepared"
            elif op == "hold_lock":
                result = _hold_lock(db_path, root)
                last_outcome = "lock_held"
            elif op == "open":
                stop_mode = str(step.get("stop_mode", "stopped"))
                if stop_mode == "permission_denied":
                    stop_mode = "unknown"
                store_fault = None
                fault_name = step.get("fault")
                if isinstance(fault_name, str):
                    def hook(name: str, connection: sqlite3.Connection) -> None:
                        """按脚本指定故障点篡改或抛错；只用于合成终态测试。"""
                        if name != fault_name:
                            return
                        if name == "finish_before_task_update":
                            connection.execute(
                                'UPDATE hr_discoveries SET nonce = ? WHERE app="boss"',
                                (str(uuid.uuid4()),),
                            )
                        elif name == "finish_before_device_update":
                            connection.execute(
                                "UPDATE device_lease SET nonce = ? WHERE device_key='primary'",
                                (str(uuid.uuid4()),),
                            )
                        else:
                            raise RuntimeError("synthetic finish failure")
                    store_fault = hook
                if store is not None:
                    store.close()
                store = open_store(str(step.get("store_host", "local")))
                result = {"opened": True}
                last_outcome = "opened"
            elif op == "io_error_open":
                original = sqlite3.connect

                def fail_connect(*args, **kwargs):
                    """在标准库 sqlite 依赖边界注入 OSError。"""
                    raise OSError("synthetic io")

                sqlite3.connect = fail_connect
                try:
                    try:
                        open_store("local")
                    except StoreError as error:
                        result = {"opened": False, "error": error.reason_code}
                        last_outcome = error.reason_code
                    else:
                        result = {"opened": True}
                        last_outcome = "opened"
                finally:
                    sqlite3.connect = original
            elif op == "upsert":
                if store is None:
                    raise ProbeError("store not open")
                count = step.get("count", 1)
                if type(count) is not int:
                    raise ProbeError("count must be integer")
                result = {"keys": store.upsert_snapshot(
                    _snapshot(now or BASE_TIME, list(step.get("kinds", ["unread"])), count)
                )}
                last_outcome = "upserted"
            elif op == "claim":
                if store is None:
                    raise ProbeError("store not open")
                owner = _owner(str(step.get("owner_id", OWNER_ID)), step.get("owner_pid", 4242))
                lease = store.claim("boss", str(step.get("kind", "unread")), owner, now or BASE_TIME)
                result = {"lease": _lease_json(lease) if lease else None}
                last_outcome = "claimed" if lease else "no_candidate"
            elif op == "locked_claim":
                if store is None:
                    raise ProbeError("store not open")
                result = _locked_claim(store, db_path, now or BASE_TIME)
                last_outcome = result.get("error", "claimed")
            elif op == "claim_other_app":
                if store is None:
                    raise ProbeError("store not open")
                store.claim("other", "unread", _owner(), now or BASE_TIME)
                result = {"claimed": True}
                last_outcome = "claimed"
            elif op == "start":
                if store is None:
                    raise ProbeError("lease not held")
                active = previous_lease if step.get("lease_source") == "previous" else lease
                if active is None:
                    raise ProbeError("lease missing")
                lease = store.start(active, now or BASE_TIME)
                result = {"state": "running"}
                last_outcome = "started"
            elif op == "renew":
                if store is None or lease is None:
                    raise ProbeError("lease not held")
                old = lease.expires_at
                previous_lease = lease
                lease = store.renew(lease, now or BASE_TIME)
                result = {"expires_at": lease.expires_at, "advanced": lease.expires_at != old}
                last_outcome = "renewed"
            elif op == "finish":
                if store is None or (lease is None and previous_lease is None):
                    raise ProbeError("lease not held")
                active = previous_lease if step.get("lease_source") == "previous" else lease
                if active is None:
                    raise ProbeError("previous lease missing")
                if step.get("use_random_nonce"):
                    active = Lease(
                        app=active.app,
                        key=active.key,
                        owner=active.owner,
                        nonce=str(uuid.uuid4()),
                        expires_at=active.expires_at,
                    )
                state = str(step["state"])
                reason = step.get("reason_code")
                proof_name = str(step.get("proof"))
                if proof_name == "read_undetermined":
                    proof = _read_result(now or BASE_TIME, "undetermined", reason)
                elif proof_name == "read_ok":
                    proof = _read_result(now or BASE_TIME, "ok", None)
                elif proof_name == "evidence_unknown":
                    proof = _evidence(now or BASE_TIME, "unknown", reason)
                else:
                    raise ProbeError("unknown proof")
                previous_lease = active
                result = {"result": store.finish(active, now or BASE_TIME, state, reason, proof)}
                lease = None
                last_outcome = "finished"
            elif op == "recover":
                if store is None or lease is None:
                    raise ProbeError("lease not held")
                active = previous_lease if step.get("lease_source") == "previous" else lease
                if active is None:
                    raise ProbeError("previous lease missing")
                if step.get("owner_mode") == "other":
                    active = Lease(
                        app=active.app,
                        key=active.key,
                        owner=Owner(id=OWNER_ID_2, pid=active.owner.pid, host=active.owner.host),
                        nonce=active.nonce,
                        expires_at=active.expires_at,
                    )
                result = store.recover(active, step.get("confirm_stopped"), now or BASE_TIME)
                last_outcome = result.get("task_state", "recovered")
                lease = None
            elif op == "inspect_raw":
                result = _inspect_raw(db_path)
                last_outcome = "inspected_raw"
            elif op == "remember_device":
                remembered_digest = _device_digest(db_path)
                result = {"remembered": remembered_digest is not None}
                last_outcome = "device_remembered"
            elif op == "remember_state":
                remembered_state = _capture_state(db_path)
                remembered_digest = remembered_state["device_digest"]
                result = {"remembered": True}
                last_outcome = "state_remembered"
            elif op == "inspect":
                if store is None:
                    raise ProbeError("store not open")
                result = _inspect(store, db_path, remembered_digest, remembered_state)
                last_outcome = "inspected"
            elif op == "close":
                if store is not None:
                    store.close()
                    store = None
                result = {"closed": True}
                last_outcome = "closed"
            else:
                raise ProbeError("unknown op")
        except StoreError as error:
            result = {"op": op, "error": error.reason_code}
            last_outcome = error.reason_code
        results.append({"op": op, **result})
    if store is not None:
        store.close()
    return {
        "schema_version": 1,
        "status": "ok",
        "outcome": last_outcome,
        "steps": results,
    }


def main(argv: list[str] | None = None) -> int:
    """读取 JSON 请求并输出单个 JSON 结果；不输出异常原文。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    try:
        root = Path.cwd().resolve()
        request_path = _relative_path(args.request, root, "request", must_exist=True)
        db_path = _relative_path(args.db, root, "db", must_exist=False)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result = run_request(request, db_path, root)
        code = 0
    except Exception as exc:  # noqa: BLE001 - 探针边界不输出 traceback/路径
        result = {
            "schema_version": 1,
            "status": "failed",
            "outcome": type(exc).__name__,
            "steps": [],
        }
        code = 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
