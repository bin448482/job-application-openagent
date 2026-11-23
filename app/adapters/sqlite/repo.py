"""实现 M1 SQLite 仓储：候选入库、全局设备租约、任务三态与过期恢复。"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import platform
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from app.adapters.sqlite.schema import (
    connect_database,
    map_locked_error,
    rollback_quietly,
)
from app.domain.models import (
    APP,
    CLOCK_TOLERANCE_SECONDS,
    DISCOVERY_KINDS,
    LEASE_SECONDS,
    LEASED_TASK_STATES,
    TERMINAL_TASK_STATES,
    Evidence,
    Lease,
    Owner,
    ReadResult,
    UnreadRow,
    UnreadSnapshot,
    add_seconds,
    canonical_hash,
    canonical_source_names,
    discovery_key_for,
    fingerprint_for,
    format_utc,
    is_hash,
    is_int,
    is_nonempty_text,
    parse_utc,
    validate_event_reason,
    validate_evidence,
    validate_lease,
    validate_owner,
    validate_read_result,
)
from app.ports.store import StoreError
from app.process_log import ProcessLog, elapsed_ms

StopChecker = Callable[[int], str]
FaultHook = Callable[[str, sqlite3.Connection], None]


def _store_operation(name: str):
    """把公开仓储方法包在唯一 started/ok/failed 操作日志边界内。"""

    def decorate(method: Callable[..., object]) -> Callable[..., object]:
        """返回保留原 docstring 与签名的包装方法。"""

        @functools.wraps(method)
        def wrapped(self: "SqliteStore", *args: object, **kwargs: object) -> object:
            """在调用前后记录固定操作名、结果、原因码与耗时。"""
            with self._operation(name):
                return method(self, *args, **kwargs)

        return wrapped

    return decorate


def default_local_host() -> str:
    """按契约把本机节点名转换为只存本机库的摘要。"""
    return canonical_hash(platform.node())


def default_stop_checker(pid: int) -> str:
    """用标准库判断 PID 是否已停止；无法明确判断时返回 unknown。"""
    if os.name == "nt":
        return _windows_process_state(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stopped"
    except (PermissionError, OSError):
        return "unknown"
    return "alive"


def _windows_process_state(pid: int) -> str:
    """通过 OpenProcess 与 WaitForSingleObject 区分已停止、仍活动与未知。"""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_invalid_parameter = 87
    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize, False, int(pid)
    )
    if not handle:
        return (
            "stopped"
            if ctypes.get_last_error() == error_invalid_parameter
            else "unknown"
        )
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
    finally:
        kernel32.CloseHandle(handle)
    if result == wait_object_0:
        return "stopped"
    if result == wait_timeout:
        return "alive"
    return "unknown"


class SqliteStore:
    """以单连接、BEGIN IMMEDIATE 事务实现 v1 存储端口与跨表租约不变量。"""

    def __init__(
        self,
        path: Path,
        *,
        local_host: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        stop_checker: StopChecker | None = None,
        fault_hook: FaultHook | None = None,
        process_log: ProcessLog | None = None,
    ) -> None:
        """打开并核验 v1 库，保存本机主机摘要、时钟、停止检查器与可选日志器。"""
        self._monotonic = monotonic
        self._stop_checker = stop_checker if stop_checker is not None else default_stop_checker
        self._fault_hook = fault_hook
        self.process_log = process_log if process_log is not None else ProcessLog()
        self._clock_mark: tuple[float, datetime] | None = None
        with self._operation("open_store"):
            self._local_host = (
                local_host if local_host is not None else default_local_host()
            )
            if not is_hash(self._local_host):
                raise StoreError("invalid_input")
            self._connection = connect_database(path)

    def close(self) -> None:
        """关闭本机数据库连接；重复关闭保持无害。"""
        self._connection.close()

    def __enter__(self) -> "SqliteStore":
        """支持 with 语句返回自身，不新建额外连接。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """退出 with 时关闭连接。"""
        self.close()

    @_store_operation("upsert_snapshot")
    def upsert_snapshot(self, result: ReadResult) -> list[str]:
        """保存完整未读快照；未知行不落库，返回写后仍 queued 的发现键。"""
        observed_at, snapshot_id, records = _prepare_upsert(result)
        changed = False
        inserted = 0
        queued: set[str] = set()
        with self._write():
            for record in records:
                existing = self._select_discovery(record.key)
                if existing is not None:
                    if existing["state"] in TERMINAL_TASK_STATES:
                        continue
                    merged = self._merged_sources(existing["source_names_json"], record.sources)
                    if existing["state"] == "queued":
                        new_kind = _upgraded_kind(existing["kind"], record.kind)
                        if (
                            new_kind != existing["kind"]
                            or merged != existing["source_names_json"]
                        ):
                            _require_time_order(observed_at, existing["updated_at"])
                            self._connection.execute(
                                "UPDATE hr_discoveries SET kind = ?, source_names_json = ?, "
                                "updated_at = ? WHERE app = ? AND \"key\" = ?",
                                (new_kind, merged, observed_at, APP, record.key),
                            )
                            changed = True
                        queued.add(record.key)
                    else:
                        deferred = self._defer_sources(existing, merged, observed_at)
                        changed = changed or deferred
                    continue

                active = self._select_active_conversation(record.conversation_key)
                if active is not None:
                    if active["state"] == "queued":
                        _require_time_order(observed_at, active["updated_at"])
                        self._connection.execute(
                            "UPDATE hr_discoveries SET state = 'cancelled', "
                            "reason_code = 'candidate_superseded', updated_at = ? "
                            "WHERE app = ? AND \"key\" = ? AND state = 'queued'",
                            (observed_at, APP, active["key"]),
                        )
                        self._insert_event(
                            app=APP,
                            subject_key=active["key"],
                            event="candidate_superseded",
                            reason_code="candidate_superseded",
                            created_at=observed_at,
                            payload=_event_payload(
                                "queued", "cancelled", None, None
                            ),
                        )
                        changed = True
                    else:
                        merged = self._merged_sources(
                            active["source_names_json"], record.sources
                        )
                        deferred = self._defer_sources(active, merged, observed_at)
                        changed = changed or deferred
                        self._insert_event(
                            app=APP,
                            subject_key=active["key"],
                            event="candidate_deferred",
                            reason_code=None,
                            created_at=observed_at,
                            payload=_event_payload(
                                active["state"], active["state"], None, None
                            ),
                        )
                        continue

                self._ensure_conversation(record.conversation_key, record.identity_hash)
                self._insert_discovery(record, snapshot_id, observed_at)
                inserted += 1
                changed = True
                queued.add(record.key)

            if changed:
                self._insert_event(
                    app=APP,
                    subject_key=None,
                    event="snapshot_upserted",
                    reason_code=None,
                    created_at=observed_at,
                    payload=_event_payload(None, "queued", None, inserted),
                )
        return sorted(queued)

    @_store_operation("claim")
    def claim(
        self, app: str, kind: str, owner: Owner, now: str
    ) -> Lease | None:
        """在全局设备空闲时原子领取最高优先级 queued 发现。"""
        now_dt = self._prepare_now(now)
        try:
            if app != APP:
                raise ValueError("app is invalid")
            if not isinstance(kind, str) or kind not in DISCOVERY_KINDS:
                raise ValueError("kind is invalid")
            validate_owner(owner)
            if owner.host != self._local_host:
                raise ValueError("owner host is not local")
        except ValueError:
            raise StoreError("invalid_input") from None
        self._check_clock(now_dt)

        with self._write():
            if self._select_device() is not None:
                raise StoreError("lease_busy")
            row = self._connection.execute(
                "SELECT \"key\" FROM hr_discoveries "
                "WHERE app = ? AND kind = ? AND state = 'queued' "
                "ORDER BY priority DESC, created_at, \"key\" LIMIT 1",
                (app, kind),
            ).fetchone()
            if row is None:
                return None
            key = str(row["key"])
            nonce = str(uuid.uuid4())
            expires_at = add_seconds(now, LEASE_SECONDS)
            cursor = self._connection.execute(
                "UPDATE hr_discoveries SET state = 'leased', lease_owner = ?, "
                "owner_pid = ?, owner_host = ?, nonce = ?, lease_expires_at = ?, "
                "attempt = attempt + 1, updated_at = ? "
                "WHERE app = ? AND \"key\" = ? AND state = 'queued'",
                (owner.id, owner.pid, owner.host, nonce, expires_at, now, app, key),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            self._connection.execute(
                "INSERT INTO device_lease (device_key, lease_owner, owner_pid, "
                "owner_host, nonce, lease_expires_at, reservation_state) "
                "VALUES ('primary', ?, ?, ?, ?, ?, 'held')",
                (owner.id, owner.pid, owner.host, nonce, expires_at),
            )
            self._insert_event(
                app=app,
                subject_key=key,
                event="task_claimed",
                reason_code=None,
                created_at=now,
                payload=_event_payload("queued", "leased", nonce, None),
            )
        return Lease(app=app, key=key, owner=owner, nonce=nonce, expires_at=expires_at)

    @_store_operation("start")
    def start(self, lease: Lease, now: str) -> Lease:
        """提交 leased→running，且不延长任务或设备截止时间。"""
        now_dt = self._prepare_now(now)
        self._check_clock(now_dt)
        with self._write():
            row, _ = self._match_lease(lease, {"leased"}, now_dt, allow_expired=False)
            cursor = self._connection.execute(
                "UPDATE hr_discoveries SET state = 'running', updated_at = ? "
                "WHERE app = ? AND \"key\" = ? AND state = 'leased' AND nonce = ?",
                (now, lease.app, lease.key, lease.nonce),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            self._insert_event(
                app=lease.app,
                subject_key=lease.key,
                event="task_started",
                reason_code=None,
                created_at=now,
                payload=_event_payload(
                    "leased", "running", str(row["nonce"]), None
                ),
            )
        return lease

    @_store_operation("renew")
    def renew(self, lease: Lease, now: str) -> Lease:
        """把任务与设备截止时间一起延至 max(原值, now+60 秒)。"""
        now_dt = self._prepare_now(now)
        self._check_clock(now_dt)
        with self._write():
            row, _ = self._match_lease(
                lease, LEASED_TASK_STATES, now_dt, allow_expired=False
            )
            current = parse_utc(str(row["lease_expires_at"]))
            expires_at = format_utc(
                max(current, now_dt + timedelta(seconds=LEASE_SECONDS))
            )
            cursor = self._connection.execute(
                "UPDATE hr_discoveries SET lease_expires_at = ?, updated_at = ? "
                "WHERE app = ? AND \"key\" = ? AND nonce = ?",
                (expires_at, now, lease.app, lease.key, lease.nonce),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            cursor = self._connection.execute(
                "UPDATE device_lease SET lease_expires_at = ? "
                "WHERE device_key = 'primary' AND nonce = ? AND reservation_state = 'held'",
                (expires_at, lease.nonce),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            self._insert_event(
                app=lease.app,
                subject_key=lease.key,
                event="lease_renewed",
                reason_code=None,
                created_at=now,
                payload=_event_payload(
                    str(row["state"]), str(row["state"]), lease.nonce, None
                ),
            )
        return Lease(
            app=lease.app,
            key=lease.key,
            owner=lease.owner,
            nonce=lease.nonce,
            expires_at=expires_at,
        )

    @_store_operation("finish")
    def finish(
        self,
        lease: Lease,
        now: str,
        state: str,
        reason_code: str | None,
        proof: ReadResult | Evidence,
    ) -> str:
        """按读取或证据三态收敛 running 任务，并清租约或隔离设备行。"""
        now_dt = self._prepare_now(now)
        try:
            _validate_finish(state, reason_code, proof)
        except ValueError:
            raise StoreError("invalid_input") from None
        self._check_clock(now_dt)

        with self._write():
            row, _ = self._match_lease(
                lease, {"running"}, now_dt, allow_expired=False
            )
            self._run_fault_hook("finish_before_task_update")
            cursor = self._connection.execute(
                "UPDATE hr_discoveries SET state = ?, reason_code = ?, "
                "lease_owner = NULL, owner_pid = NULL, owner_host = NULL, "
                "nonce = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE app = ? AND \"key\" = ? AND state = 'running' "
                "AND lease_owner = ? AND owner_pid = ? AND owner_host = ? "
                "AND nonce = ? AND lease_expires_at = ?",
                (
                    state,
                    reason_code,
                    now,
                    lease.app,
                    lease.key,
                    lease.owner.id,
                    lease.owner.pid,
                    lease.owner.host,
                    lease.nonce,
                    lease.expires_at,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            self._run_fault_hook("finish_before_device_update")
            if state == "blocked":
                cursor = self._connection.execute(
                    "UPDATE device_lease SET reservation_state = 'quarantined' "
                    "WHERE device_key = 'primary' AND reservation_state = 'held' "
                    "AND lease_owner = ? AND owner_pid = ? AND owner_host = ? "
                    "AND nonce = ? AND lease_expires_at = ?",
                    (
                        lease.owner.id,
                        lease.owner.pid,
                        lease.owner.host,
                        lease.nonce,
                        lease.expires_at,
                    ),
                )
            else:
                cursor = self._connection.execute(
                    "DELETE FROM device_lease WHERE device_key = 'primary' "
                    "AND reservation_state = 'held' AND lease_owner = ? "
                    "AND owner_pid = ? AND owner_host = ? AND nonce = ? "
                    "AND lease_expires_at = ?",
                    (
                        lease.owner.id,
                        lease.owner.pid,
                        lease.owner.host,
                        lease.nonce,
                        lease.expires_at,
                    ),
                )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            self._run_fault_hook("finish_before_event")
            self._insert_event(
                app=lease.app,
                subject_key=lease.key,
                event="task_finished",
                reason_code=reason_code,
                created_at=now,
                payload=_event_payload(
                    str(row["state"]), state, lease.nonce, None
                ),
            )
        return "updated"

    @_store_operation("recover")
    def recover(
        self, lease: Lease, confirm_stopped: bool, now: str
    ) -> dict[str, str]:
        """仅在过期、确认停止且本机检查器判定停止后恢复旧租约。"""
        now_dt = self._prepare_now(now)
        if type(confirm_stopped) is not bool:
            raise StoreError("invalid_input")
        self._check_clock(now_dt)

        with self._write():
            row, device = self._match_lease(
                lease, LEASED_TASK_STATES, now_dt, allow_expired=True
            )
            if now_dt < parse_utc(str(row["lease_expires_at"])) or now_dt < parse_utc(
                str(device["lease_expires_at"])
            ):
                raise StoreError("lease_busy")
            if not confirm_stopped:
                raise StoreError("owner_not_stopped")
            if lease.owner.host != self._local_host:
                raise StoreError("owner_not_stopped")
            if self._stop_checker(lease.owner.pid) != "stopped":
                raise StoreError("owner_not_stopped")

            from_state = str(row["state"])
            if from_state == "leased":
                to_state = "queued"
                reason_code = None
            else:
                to_state = "blocked"
                reason_code = "recovery_requires_reconciliation"
            cursor = self._connection.execute(
                "UPDATE hr_discoveries SET state = ?, reason_code = ?, "
                "lease_owner = NULL, owner_pid = NULL, owner_host = NULL, "
                "nonce = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE app = ? AND \"key\" = ? AND state = ? AND nonce = ?",
                (to_state, reason_code, now, lease.app, lease.key, from_state, lease.nonce),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            if to_state == "queued":
                cursor = self._connection.execute(
                    "DELETE FROM device_lease WHERE device_key = 'primary' "
                    "AND reservation_state = 'held' AND nonce = ?",
                    (lease.nonce,),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE device_lease SET reservation_state = 'quarantined' "
                    "WHERE device_key = 'primary' AND reservation_state = 'held' "
                    "AND nonce = ?",
                    (lease.nonce,),
                )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            self._insert_event(
                app=lease.app,
                subject_key=lease.key,
                event="lease_recovered",
                reason_code=reason_code,
                created_at=now,
                payload=_event_payload(from_state, to_state, lease.nonce, None),
            )
        return {"status": "recovered", "task_state": to_state}

    @_store_operation("unresolved_conversations")
    def unresolved_conversations(self, app: str) -> list[str]:
        """只读返回 queued/leased/running/blocked 的去重会话键。"""
        if app != APP:
            raise StoreError("invalid_input")
        try:
            rows = self._connection.execute(
                "SELECT DISTINCT conversation_key FROM hr_discoveries "
                "WHERE app = ? AND state IN ('queued', 'leased', 'running', 'blocked') "
                "ORDER BY conversation_key",
                (app,),
            ).fetchall()
        except sqlite3.OperationalError as error:
            raise map_locked_error(error) from None
        except sqlite3.Error:
            raise StoreError("store_io_error") from None
        return [str(row["conversation_key"]) for row in rows]

    @contextmanager
    def _operation(self, name: str) -> Iterator[None]:
        """记录公开操作的 started/ok/failed；日志失败只降级，不影响业务结果。"""
        started = time.perf_counter()
        self.process_log.record(stage="store", operation=name, result="started")
        try:
            try:
                yield
            except StoreError as error:
                self.process_log.record(
                    stage="store",
                    operation=name,
                    result="failed",
                    reason_code=error.reason_code,
                    error_category="store",
                    duration_ms=elapsed_ms(started),
                )
                raise
            except Exception:
                self.process_log.record(
                    stage="store",
                    operation=name,
                    result="failed",
                    error_category="internal",
                    duration_ms=elapsed_ms(started),
                )
                raise
            else:
                self.process_log.record(
                    stage="store",
                    operation=name,
                    result="ok",
                    duration_ms=elapsed_ms(started),
                )
        finally:
            self.process_log.emit_degraded_warning()

    @contextmanager
    def _write(self) -> Iterator[None]:
        """开启写事务，统一映射锁等待、约束与 IO 错误并保证回滚。"""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            raise map_locked_error(error) from None
        except sqlite3.Error:
            raise StoreError("store_io_error") from None
        try:
            yield
            self._connection.execute("COMMIT")
        except StoreError:
            rollback_quietly(self._connection)
            raise
        except ValueError:
            rollback_quietly(self._connection)
            raise StoreError("invalid_input") from None
        except sqlite3.IntegrityError:
            rollback_quietly(self._connection)
            raise StoreError("invalid_input") from None
        except sqlite3.OperationalError as error:
            rollback_quietly(self._connection)
            raise map_locked_error(error) from None
        except (sqlite3.Error, OSError, RuntimeError):
            rollback_quietly(self._connection)
            raise StoreError("store_io_error") from None

    def _prepare_now(self, now: object) -> datetime:
        """解析入口 UTC 时间；非法类型或格式统一为 invalid_input。"""
        try:
            return parse_utc(now)
        except ValueError:
            raise StoreError("invalid_input") from None

    def _check_clock(self, now_dt: datetime) -> None:
        """比较墙钟与单调时钟增量，超过 1 秒即拒绝新动作。"""
        mono = float(self._monotonic())
        if self._clock_mark is None:
            self._clock_mark = (mono, now_dt)
            return
        previous_mono, previous_wall = self._clock_mark
        wall_delta = (now_dt - previous_wall).total_seconds()
        if abs(wall_delta - (mono - previous_mono)) > CLOCK_TOLERANCE_SECONDS:
            raise StoreError("clock_changed")
        self._clock_mark = (mono, now_dt)

    def _match_lease(
        self,
        lease: Lease,
        states: set[str] | frozenset[str],
        now_dt: datetime,
        *,
        allow_expired: bool,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        """核对任务与 held 设备行的持有者、nonce、截止时间；不匹配即 stale。"""
        try:
            validate_lease(lease)
        except ValueError:
            raise StoreError("invalid_input") from None
        row = self._connection.execute(
            "SELECT * FROM hr_discoveries WHERE app = ? AND \"key\" = ?",
            (lease.app, lease.key),
        ).fetchone()
        device = self._select_device()
        if row is None or device is None:
            raise StoreError("stale_lease")
        if (
            str(row["state"]) not in states
            or row["lease_owner"] != lease.owner.id
            or row["owner_pid"] != lease.owner.pid
            or row["owner_host"] != lease.owner.host
            or row["nonce"] != lease.nonce
            or row["lease_expires_at"] != lease.expires_at
            or str(device["reservation_state"]) != "held"
            or device["lease_owner"] != lease.owner.id
            or device["owner_pid"] != lease.owner.pid
            or device["owner_host"] != lease.owner.host
            or device["nonce"] != lease.nonce
            or device["lease_expires_at"] != lease.expires_at
        ):
            raise StoreError("stale_lease")
        if not allow_expired and (
            now_dt >= parse_utc(str(row["lease_expires_at"]))
            or now_dt >= parse_utc(str(device["lease_expires_at"]))
        ):
            raise StoreError("stale_lease")
        return row, device

    def _select_device(self) -> sqlite3.Row | None:
        """读取唯一 primary 设备行；不存在返回 None。"""
        return self._connection.execute(
            "SELECT * FROM device_lease WHERE device_key = 'primary'"
        ).fetchone()

    def _select_discovery(self, key: str) -> sqlite3.Row | None:
        """按主键读取一条发现，用于终态跳过与非终态合并。"""
        return self._connection.execute(
            "SELECT * FROM hr_discoveries WHERE app = ? AND \"key\" = ?",
            (APP, key),
        ).fetchone()

    def _select_active_conversation(self, conversation_key: str) -> sqlite3.Row | None:
        """读取同一会话当前唯一的非终态发现。"""
        return self._connection.execute(
            "SELECT * FROM hr_discoveries WHERE app = ? AND conversation_key = ? "
            "AND state IN ('queued', 'leased', 'running', 'blocked')",
            (APP, conversation_key),
        ).fetchone()

    def _merged_sources(self, stored_json: str, new_sources: tuple[str, ...]) -> str:
        """把库中来源与本次来源按四源顺序合并为规范 JSON。"""
        try:
            stored = json.loads(stored_json)
            merged = canonical_source_names(list(stored) + list(new_sources))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise StoreError("store_io_error") from None
        return _dump_sources(merged)

    def _defer_sources(
        self, existing: sqlite3.Row, merged: str, observed_at: str
    ) -> bool:
        """非 queued 活跃发现只合并不冲突的来源，不改负载或租约。"""
        if merged == existing["source_names_json"]:
            return False
        _require_time_order(observed_at, existing["updated_at"])
        self._connection.execute(
            "UPDATE hr_discoveries SET source_names_json = ?, updated_at = ? "
            "WHERE app = ? AND \"key\" = ?",
            (merged, observed_at, APP, existing["key"]),
        )
        return True

    def _ensure_conversation(self, key: str, identity_hash: str) -> None:
        """插入缺少的会话行；键绑定 identity_hash 后已有行直接复用。"""
        row = self._connection.execute(
            "SELECT identity_hash FROM hr_conversations WHERE app = ? AND \"key\" = ?",
            (APP, key),
        ).fetchone()
        if row is not None:
            return
        self._connection.execute(
            "INSERT INTO hr_conversations (app, \"key\", identity_hash) VALUES (?, ?, ?)",
            (APP, key, identity_hash),
        )

    def _insert_discovery(self, record: _CandidateRecord, snapshot_id: str, now: str) -> None:
        """插入新 queued 发现，保留合成观察时间作为创建与更新时间。"""
        self._connection.execute(
            "INSERT INTO hr_discoveries (app, \"key\", conversation_key, kind, "
            "fingerprint, source_names_json, snapshot_id, opened, state, priority, "
            "created_at, updated_at, attempt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'queued', 0, ?, ?, 0)",
            (
                APP,
                record.key,
                record.conversation_key,
                record.kind,
                record.fingerprint,
                _dump_sources(record.sources),
                snapshot_id,
                now,
                now,
            ),
        )

    def _insert_event(
        self,
        *,
        app: str | None,
        subject_key: str | None,
        event: str,
        reason_code: str | None,
        created_at: str,
        payload: dict[str, object],
    ) -> None:
        """追加一条白名单元数据事件，payload 固定四键且不含正文。"""
        validate_event_reason(reason_code)
        self._connection.execute(
            "INSERT INTO events (app, subject_key, event, reason_code, created_at, "
            "payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                app,
                subject_key,
                event,
                reason_code,
                created_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def _run_fault_hook(self, name: str) -> None:
        """仅在测试注入时执行故障点；生产入口不设置该回调。"""
        if self._fault_hook is not None:
            self._fault_hook(name, self._connection)


class _CandidateRecord:
    """保存已重算键与指纹的候选，供事务写入使用。"""

    __slots__ = ("key", "conversation_key", "kind", "fingerprint", "sources", "identity_hash")

    def __init__(
        self,
        key: str,
        conversation_key: str,
        kind: str,
        fingerprint: str,
        sources: tuple[str, ...],
        identity_hash: str,
    ) -> None:
        """保存单个候选的已校验字段。"""
        self.key = key
        self.conversation_key = conversation_key
        self.kind = kind
        self.fingerprint = fingerprint
        self.sources = sources
        self.identity_hash = identity_hash


def _prepare_upsert(
    result: ReadResult,
) -> tuple[str, str, list[_CandidateRecord]]:
    """校验完整 ReadResult 并重算候选键、指纹与会话身份。"""
    try:
        validate_read_result(result)
        if result.status != "ok" or result.value is None or result.snapshot_id is None:
            raise ValueError("snapshot is not persistable")
        if not isinstance(result.value, UnreadSnapshot):
            raise ValueError("snapshot value is not unread")
        snapshot = result.value
        rows: dict[str, UnreadRow] = {}
        for row in snapshot.rows:
            if row.conversation_key is None:
                continue
            if row.identity_hash is None:
                raise ValueError("row identity is missing")
            rows[row.conversation_key] = row
        records: list[_CandidateRecord] = []
        for candidate in snapshot.candidates:
            row = rows.get(candidate.conversation_key)
            if row is None:
                raise ValueError("candidate conversation is unknown")
            fingerprint = fingerprint_for(
                row.preview_hash, row.time_hint, row.unread_count
            )
            if fingerprint != candidate.fingerprint:
                raise ValueError("candidate fingerprint is stale")
            key = discovery_key_for(APP, candidate.conversation_key, fingerprint)
            if key != candidate.candidate_key:
                raise ValueError("candidate key is stale")
            records.append(
                _CandidateRecord(
                    key=key,
                    conversation_key=candidate.conversation_key,
                    kind=candidate.kind,
                    fingerprint=fingerprint,
                    sources=canonical_source_names(candidate.source_names),
                    identity_hash=row.identity_hash,
                )
            )
    except ValueError:
        raise StoreError("invalid_input") from None
    records.sort(key=lambda record: record.key)
    return result.observed_at, result.snapshot_id, records


def _validate_finish(state: str, reason_code: str | None, proof: object) -> None:
    """校验 finish 的状态、原因与 ReadResult/Evidence 三态组合。"""
    if not isinstance(state, str) or state not in {
        "done",
        "not_sent",
        "failed",
        "blocked",
    }:
        raise ValueError("finish state is invalid")
    if reason_code is not None and not is_nonempty_text(reason_code):
        raise ValueError("finish reason is invalid")
    if isinstance(proof, ReadResult):
        validate_read_result(proof)
        if state == "done":
            if proof.status != "ok" or reason_code is not None:
                raise ValueError("done proof is invalid")
            return
        if state == "failed":
            if proof.status != "failed" or proof.reason_code != reason_code:
                raise ValueError("failed proof is invalid")
            return
        if state == "blocked":
            if proof.status != "undetermined" or proof.reason_code != reason_code:
                raise ValueError("blocked proof is invalid")
            return
        raise ValueError("not sent requires evidence")
    if isinstance(proof, Evidence):
        validate_evidence(proof)
        if state == "done":
            if proof.outcome != "performed" or reason_code is not None:
                raise ValueError("done proof is invalid")
            return
        if state == "not_sent":
            if proof.outcome != "not_performed" or proof.reason_code != reason_code:
                raise ValueError("not sent proof is invalid")
            return
        if state == "blocked":
            if proof.outcome != "unknown" or proof.reason_code != reason_code:
                raise ValueError("blocked proof is invalid")
            return
        raise ValueError("failed requires read result")
    raise ValueError("proof is invalid")


def _upgraded_kind(existing: str, incoming: str) -> str:
    """queued 发现允许把 new_message 升级为更强的 unread。"""
    if existing == "new_message" and incoming == "unread":
        return "unread"
    return existing


def _dump_sources(sources: tuple[str, ...]) -> str:
    """把规范化来源名序列化为紧凑 JSON 数组文本。"""
    return json.dumps(list(sources), ensure_ascii=False, separators=(",", ":"))


def _event_payload(
    from_state: str | None,
    to_state: str | None,
    nonce: str | None,
    candidate_count: int | None,
) -> dict[str, object]:
    """构造固定四键事件负载，不含正文或参数原值。"""
    if candidate_count is not None and (
        not is_int(candidate_count) or candidate_count < 0
    ):
        raise ValueError("candidate count is invalid")
    return {
        "from_state": from_state,
        "to_state": to_state,
        "nonce": nonce,
        "candidate_count": candidate_count,
    }


def _require_time_order(observed_at: str, existing_updated_at: str) -> None:
    """拒绝用更早的观察时间倒写已有发现。"""
    if parse_utc(observed_at) < parse_utc(existing_updated_at):
        raise StoreError("invalid_input")
