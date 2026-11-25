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

from app.adapters.sqlite.profile_schema import connect_profile_database
from app.adapters.sqlite.schema import (
    connect_database,
    map_locked_error,
    rollback_quietly,
)
from app.domain.actions import (
    ActionError,
    ActionOutcome,
    ACTION_KIND_SET,
    UNRESOLVED_ACTION_STATES,
    expected_effect,
    is_action_key,
    validate_action_target,
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
    is_discovery_key,
    is_job_task_key,
    is_task_key,
    is_uuid,
    is_conversation_key,
    is_nonempty_text,
    parse_utc,
    validate_event_reason,
    validate_evidence,
    validate_lease,
    validate_owner,
    validate_read_result,
    validate_task_lease,
)
from app.domain.runtime import BatchCursor, BudgetLimits, PAUSE_REASONS
from app.ports.store import StoreError
from app.process_log import ProcessLog, elapsed_ms
from app.domain.reading import is_message_key, is_job_key

StopChecker = Callable[[int], str]
FaultHook = Callable[[str, sqlite3.Connection], None]


def _store_operation(name: str, *, stage: str = "store"):
    """把公开仓储方法包在唯一 started/ok/failed 操作日志边界内。"""

    def decorate(method: Callable[..., object]) -> Callable[..., object]:
        """返回保留原 docstring 与签名的包装方法。"""

        @functools.wraps(method)
        def wrapped(self: "SqliteStore", *args: object, **kwargs: object) -> object:
            """在调用前后记录固定操作名、结果、原因码与耗时。"""
            with self._operation(name, stage=stage):
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


def default_command_stop_checker(pid: int) -> str:
    """检查原父进程及其直接子进程是否都已停止；无法证明时返回 unknown。"""
    state = default_stop_checker(pid)
    if state != "stopped":
        return state
    children = _child_processes(pid)
    if children is None:
        return "unknown"
    return "alive" if children else "stopped"


def _child_processes(pid: int) -> list[int] | None:
    """返回直接子进程 PID；无法枚举时返回 None。"""
    if os.name == "nt":
        return _windows_children(pid)
    try:
        children: list[int] = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                stat = Path(f"/proc/{name}/stat").read_text(encoding="utf-8")
            except OSError:
                continue
            parts = stat.rsplit(")", 1)
            if len(parts) != 2:
                continue
            fields = parts[1].split()
            if len(fields) > 1 and fields[1].isdigit() and int(fields[1]) == pid:
                children.append(int(name))
        return children
    except OSError:
        return None


def _windows_children(pid: int) -> list[int] | None:
    """用 Toolhelp32 快照枚举直接子进程；失败返回 None。"""
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        """Toolhelp PROCESSENTRY32W 的固定字段。"""

        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot:
        return None
    invalid_handle = wintypes.HANDLE(-1).value
    if snapshot == invalid_handle:
        return None
    result: list[int] = []
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return result
        while True:
            if int(entry.th32ParentProcessID) == pid:
                result.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return result
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
        profile_id: str | None = None,
        require_runtime: bool = False,
    ) -> None:
        """打开并核验 v1 库或已校验 profile 库，保存本机身份/时钟/日志。

        `profile_id` 仅由 M4c 在完整 profile 库校验后传入；普通 M1 库仍走
        精确五表 `connect_database`，不放宽旧库验证。
        """
        self._monotonic = monotonic
        self._stop_checker = stop_checker if stop_checker is not None else default_stop_checker
        self._fault_hook = fault_hook
        self.process_log = process_log if process_log is not None else ProcessLog()
        self._clock_mark: tuple[float, datetime] | None = None
        self.profile_id = profile_id
        self.path = Path(path).resolve()
        with self._operation("open_store"):
            self._local_host = (
                local_host if local_host is not None else default_local_host()
            )
            if not is_hash(self._local_host):
                raise StoreError("invalid_input")
            if profile_id is None:
                self._connection = connect_database(path)
            else:
                self._connection = connect_profile_database(
                    Path(path),
                    profile_id,
                    require_actions=True,
                    require_runtime=require_runtime,
                )

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
        """保存完整未读快照；profile 模式先核对快照 profile_id 与本库绑定。"""
        observed_at, snapshot_id, records = _prepare_upsert(result)
        if self.profile_id is not None:
            if (
                not isinstance(result.value, UnreadSnapshot)
                or result.value.profile_id != self.profile_id
            ):
                raise StoreError("invalid_input")
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
        self,
        app: str,
        kind: str,
        owner: Owner,
        now: str,
        key: str | None = None,
        conversation_key: str | None = None,
    ) -> Lease | None:
        """在全局设备空闲时原子领取 queued 发现（可指定任务或会话身份）。"""
        now_dt = self._prepare_now(now)
        try:
            if app != APP:
                raise ValueError("app is invalid")
            if not isinstance(kind, str) or kind not in DISCOVERY_KINDS:
                raise ValueError("kind is invalid")
            if key is not None and conversation_key is not None:
                raise ValueError("claim target is ambiguous")
            if key is not None and not is_discovery_key(app, key):
                raise ValueError("key is invalid")
            if conversation_key is not None and not is_conversation_key(
                app, conversation_key
            ):
                raise ValueError("conversation key is invalid")
            validate_owner(owner)
            if owner.host != self._local_host:
                raise ValueError("owner host is not local")
        except ValueError:
            raise StoreError("invalid_input") from None
        self._check_clock(now_dt)

        with self._write():
            if self._select_device() is not None:
                raise StoreError("lease_busy")
            if key is not None:
                row = self._connection.execute(
                    "SELECT \"key\" FROM hr_discoveries "
                    "WHERE app = ? AND kind = ? AND state = 'queued' AND \"key\" = ?",
                    (app, kind, key),
                ).fetchone()
            elif conversation_key is not None:
                row = self._connection.execute(
                    "SELECT \"key\" FROM hr_discoveries "
                    "WHERE app = ? AND kind = ? AND state = 'queued' "
                    "AND conversation_key = ? "
                    "ORDER BY priority DESC, created_at, \"key\" LIMIT 1",
                    (app, kind, conversation_key),
                ).fetchone()
            else:
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
            row, _ = self._match_current_lease(
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

    @_store_operation("current_lease_expiry")
    def current_lease_expiry(self, lease: Lease, *, now: str) -> str | None:
        """只读返回当前真实截止；身份/nonce 匹配且未过期，否则 None。"""
        if not isinstance(lease, Lease):
            raise StoreError("invalid_input")
        try:
            now_dt = parse_utc(now)
        except ValueError:
            raise StoreError("invalid_input") from None
        try:
            row, _device = self._match_current_lease(
                lease, {"leased", "running"}, now_dt, allow_expired=False
            )
        except StoreError as error:
            if error.reason_code in {"stale_lease", "invalid_input"}:
                return None
            raise
        return str(row["lease_expires_at"])

    @_store_operation("require_running")
    def require_running(self, lease: Lease, *, now: str) -> bool:
        """只读确认任务已 start 为 running 且当前任务/设备租约完整有效。"""
        if not isinstance(lease, Lease):
            raise StoreError("invalid_input")
        try:
            now_dt = parse_utc(now)
        except ValueError:
            raise StoreError("invalid_input") from None
        try:
            self._match_current_lease(
                lease, {"running"}, now_dt, allow_expired=False
            )
        except StoreError as error:
            if error.reason_code in {"stale_lease", "invalid_input"}:
                return False
            raise
        return True

    @_store_operation("task_conversation")
    def task_conversation(self, lease: Lease, *, now: str) -> str | None:
        """只读返回 running 任务绑定会话键；租约任何不符返回 None。"""
        if not isinstance(lease, Lease):
            raise StoreError("invalid_input")
        try:
            validate_task_lease(lease)
            now_dt = parse_utc(now)
        except ValueError:
            raise StoreError("invalid_input") from None
        if self.current_lease_expiry(lease, now=now) is None:
            return None
        try:
            row = self._select_task_row(lease.key)
        except sqlite3.OperationalError as error:
            raise map_locked_error(error) from None
        except sqlite3.Error:
            raise StoreError("store_io_error") from None
        if (
            row is None
            or str(row["state"]) != "running"
            or str(row["nonce"]) != lease.nonce
        ):
            return None
        del now_dt
        if is_job_task_key(lease.key):
            conversation = row["conversation_key"]
            return str(conversation) if conversation is not None else lease.key
        return str(row["conversation_key"])

    @_store_operation("ensure_batch")
    def ensure_batch(
        self, batch_id: str, limits: BudgetLimits, now: str
    ) -> dict[str, object]:
        """幂等创建批次游标；已存在时只读返回，不重置预算或历史。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None or not is_uuid(batch_id):
            raise StoreError("invalid_input")
        if not isinstance(limits, BudgetLimits):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        initial = BatchCursor.initial(batch_id, limits)
        with self._write():
            self._connection.execute(
                "INSERT INTO job_batches(batch_id, profile_id, cursor_json, state, "
                "pause_reason, last_checkpoint_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', NULL, NULL, ?, ?) "
                "ON CONFLICT(batch_id) DO NOTHING",
                (
                    batch_id,
                    self.profile_id,
                    json.dumps(initial.to_json(), sort_keys=True),
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM job_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return self._batch_payload(row)

    @_store_operation("load_batch")
    def load_batch(self, batch_id: str) -> dict[str, object] | None:
        """只读返回批次游标与状态；不存在返回 None。"""
        if not is_uuid(batch_id):
            raise StoreError("invalid_input")
        row = self._connection.execute(
            "SELECT * FROM job_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return None if row is None else self._batch_payload(row)

    @_store_operation("resume_batch")
    def resume_batch(self, batch_id: str, now: str) -> dict[str, object]:
        """把 paused 批次原子转回 active，保留游标/预算/访问历史。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None or not is_uuid(batch_id):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            row = self._connection.execute(
                "SELECT * FROM job_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None or str(row["profile_id"]) != self.profile_id:
                raise StoreError("invalid_input")
            if str(row["state"]) in {"paused", "completed"}:
                self._connection.execute(
                    "UPDATE job_batches SET state = 'active', pause_reason = NULL, "
                    "updated_at = ? WHERE batch_id = ? AND state IN ('paused', 'completed')",
                    (now, batch_id),
                )
                self._insert_scheduler_event(
                    batch_id=batch_id,
                    task_key=None,
                    event="batch_resumed",
                    reason_code=None,
                    created_at=now,
                    payload={"pause_reason": row["pause_reason"]},
                )
                row = self._connection.execute(
                    "SELECT * FROM job_batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
        return self._batch_payload(row)

    @_store_operation("complete_batch")
    def complete_batch(
        self, batch_id: str, cursor: BatchCursor, now: str
    ) -> dict[str, object]:
        """正常安全结束：保存游标并把批次置 completed，不重置预算/历史。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None or not isinstance(cursor, BatchCursor):
            raise StoreError("invalid_input")
        if cursor.batch_id != batch_id:
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            row = self._connection.execute(
                "SELECT profile_id FROM job_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None or str(row["profile_id"]) != self.profile_id:
                raise StoreError("invalid_input")
            self._connection.execute(
                "UPDATE job_batches SET cursor_json = ?, state = 'completed', "
                "pause_reason = NULL, updated_at = ? WHERE batch_id = ?",
                (json.dumps(cursor.to_json(), sort_keys=True), now, batch_id),
            )
        return {"batch_id": batch_id, "state": "completed"}

    @_store_operation("checkpoint_status")
    def checkpoint_status(self, batch_id: str) -> dict[str, object]:
        """只读返回最后检查点与已提交事件计数，供安全点证据派生。"""
        if not is_uuid(batch_id):
            raise StoreError("invalid_input")
        row = self._connection.execute(
            "SELECT last_checkpoint_id FROM job_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise StoreError("invalid_input")
        count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE batch_id = ? "
                "AND event = 'checkpoint_committed'",
                (batch_id,),
            ).fetchone()[0]
        )
        return {
            "last_checkpoint_id": row["last_checkpoint_id"],
            "checkpoint_events": count,
        }

    def _batch_payload(self, row: sqlite3.Row) -> dict[str, object]:
        """把批次行投影为固定对象；游标重新严格解析后再输出。"""
        cursor = BatchCursor.from_json(json.loads(str(row["cursor_json"])))
        if (
            self.profile_id is not None
            and str(row["profile_id"]) != self.profile_id
        ):
            raise StoreError("invalid_input")
        return {
            "batch_id": str(row["batch_id"]),
            "profile_id": str(row["profile_id"]),
            "state": str(row["state"]),
            "pause_reason": row["pause_reason"],
            "last_checkpoint_id": row["last_checkpoint_id"],
            "cursor": cursor,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @_store_operation("enqueue_jobs")
    def enqueue_jobs(
        self, batch_id: str, job_keys: tuple[str, ...], now: str
    ) -> int:
        """把当前可见职位身份加入批次队列；已存在的身份不改状态或预算。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None or not is_uuid(batch_id):
            raise StoreError("invalid_input")
        if not isinstance(job_keys, tuple) or any(
            not is_job_key(key) for key in job_keys
        ):
            raise StoreError("invalid_input")
        if len(set(job_keys)) != len(job_keys):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        inserted = 0
        with self._write():
            batch = self._connection.execute(
                "SELECT * FROM job_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None or (
                self.profile_id is not None
                and str(batch["profile_id"]) != self.profile_id
            ):
                raise StoreError("invalid_input")
            for key in job_keys:
                cursor = self._connection.execute(
                    "INSERT INTO jobs(app, job_key, dedupe_key, batch_id, "
                    "conversation_key, state, priority, created_at, updated_at, "
                    "attempt) VALUES (?, ?, ?, ?, NULL, 'queued', 0, ?, ?, 0) "
                    "ON CONFLICT(app, job_key) DO NOTHING",
                    (APP, key, key, batch_id, now, now),
                )
                inserted += cursor.rowcount
        return inserted

    @_store_operation("claim_job")
    def claim_job(
        self,
        batch_id: str,
        owner: Owner,
        now: str,
        key: str | None = None,
    ) -> Lease | None:
        """在全局设备空闲时领取本批次 queued 职位（可选指定身份）。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None or not is_uuid(batch_id):
            raise StoreError("invalid_input")
        if key is not None and not is_job_key(key):
            raise StoreError("invalid_input")
        try:
            validate_owner(owner)
            if owner.host != self._local_host:
                raise ValueError("owner host is not local")
        except ValueError:
            raise StoreError("invalid_input") from None
        self._check_clock(now_dt)
        with self._write():
            batch = self._connection.execute(
                "SELECT * FROM job_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None or (
                self.profile_id is not None
                and str(batch["profile_id"]) != self.profile_id
            ):
                raise StoreError("invalid_input")
            if str(batch["state"]) != "active":
                return None
            if self._select_device() is not None:
                raise StoreError("lease_busy")
            row = self._connection.execute(
                "SELECT job_key FROM jobs WHERE app = ? AND batch_id = ? "
                "AND state = 'queued' "
                + ("AND job_key = ? " if key is not None else "")
                + "ORDER BY priority DESC, created_at, job_key LIMIT 1",
                ((APP, batch_id, key) if key is not None else (APP, batch_id)),
            ).fetchone()
            if row is None:
                return None
            key = str(row["job_key"])
            nonce = str(uuid.uuid4())
            expires_at = add_seconds(now, LEASE_SECONDS)
            cursor = self._connection.execute(
                "UPDATE jobs SET state = 'leased', lease_owner = ?, "
                "owner_pid = ?, owner_host = ?, nonce = ?, lease_expires_at = ?, "
                "attempt = attempt + 1, updated_at = ? "
                "WHERE app = ? AND job_key = ? AND state = 'queued'",
                (owner.id, owner.pid, owner.host, nonce, expires_at, now, APP, key),
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
                app=APP,
                subject_key=key,
                event="task_claimed",
                reason_code=None,
                created_at=now,
                payload=_event_payload("queued", "leased", nonce, None),
            )
        return Lease(app=APP, key=key, owner=owner, nonce=nonce, expires_at=expires_at)

    @_store_operation("start_job")
    def start_job(self, lease: Lease, now: str) -> Lease:
        """提交职位任务 leased→running；复用同一设备租约与截止时间。"""
        now_dt = self._prepare_now(now)
        if not is_job_task_key(lease.key):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            row, _ = self._match_lease(
                lease, {"leased"}, now_dt, allow_expired=False
            )
            cursor = self._connection.execute(
                "UPDATE jobs SET state = 'running', updated_at = ? "
                "WHERE app = ? AND job_key = ? AND state = 'leased' AND nonce = ?",
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

    @_store_operation("renew_job")
    def renew_job(self, lease: Lease, now: str) -> Lease:
        """同时延长职位任务与设备截止时间，返回新的 Lease。"""
        now_dt = self._prepare_now(now)
        if not is_job_task_key(lease.key):
            raise StoreError("invalid_input")
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
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE app = ? AND job_key = ? AND nonce = ?",
                (expires_at, now, lease.app, lease.key, lease.nonce),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            cursor = self._connection.execute(
                "UPDATE device_lease SET lease_expires_at = ? "
                "WHERE device_key = 'primary' AND nonce = ? "
                "AND reservation_state = 'held'",
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

    @_store_operation("finish_job")
    def finish_job(
        self,
        lease: Lease,
        now: str,
        state: str,
        reason_code: str | None,
    ) -> str:
        """收敛 running 职位任务；blocked 保留设备隔离，其余删除设备行。"""
        now_dt = self._prepare_now(now)
        if not is_job_task_key(lease.key):
            raise StoreError("invalid_input")
        if state not in {"done", "not_sent", "failed", "blocked", "cancelled"}:
            raise StoreError("invalid_input")
        if (state == "done" and reason_code is not None) or (
            state != "done" and not is_nonempty_text(reason_code)
        ):
            raise StoreError("invalid_input")
        if reason_code is not None:
            try:
                validate_event_reason(reason_code)
            except ValueError:
                raise StoreError("invalid_input") from None
        self._check_clock(now_dt)
        with self._write():
            task_row, _ = self._match_current_lease(
                lease, {"running"}, now_dt, allow_expired=False
            )
            self._apply_task_terminal(
                task_row,
                lease,
                now=now,
                task_state=state,
                reason_code=reason_code,
                from_state="running",
                lease_expires_at=str(task_row["lease_expires_at"]),
                keep_device=(state == "blocked"),
            )
        return "updated"

    @_store_operation("save_checkpoint")
    def save_checkpoint(
        self,
        batch_id: str,
        cursor: BatchCursor,
        *,
        now: str,
    ) -> dict[str, object]:
        """原子提交游标、预算与 checkpoint_committed；失败整体回滚。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("invalid_input")
        if not isinstance(cursor, BatchCursor) or cursor.batch_id != batch_id:
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        checkpoint_id = str(uuid.uuid4())
        with self._write():
            row = self._connection.execute(
                "SELECT profile_id FROM job_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None or str(row["profile_id"]) != self.profile_id:
                raise StoreError("invalid_input")
            cursor_json = json.dumps(cursor.to_json(), sort_keys=True)
            self._connection.execute(
                "UPDATE job_batches SET cursor_json = ?, state = 'active', "
                "pause_reason = NULL, last_checkpoint_id = ?, updated_at = ? "
                "WHERE batch_id = ?",
                (cursor_json, checkpoint_id, now, batch_id),
            )
            self._run_fault_hook("checkpoint_before_event")
            self._insert_scheduler_event(
                batch_id=batch_id,
                task_key=None,
                event="checkpoint_committed",
                reason_code=None,
                created_at=now,
                payload={
                    "checkpoint_id": checkpoint_id,
                    "preemptions_used": cursor.preemptions_used,
                    "remaining_budget": cursor.budget.to_json(),
                },
            )
        return {"checkpoint_id": checkpoint_id, "cursor": cursor, "updated_at": now}

    @_store_operation("pause_batch")
    def pause_batch(
        self,
        batch_id: str,
        cursor: BatchCursor,
        reason_code: str,
        *,
        now: str,
    ) -> dict[str, object]:
        """原子保存游标并暂停批次；原因只取固定词表。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("invalid_input")
        if (
            not isinstance(cursor, BatchCursor)
            or cursor.batch_id != batch_id
            or reason_code not in PAUSE_REASONS
        ):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            row = self._connection.execute(
                "SELECT profile_id FROM job_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None or str(row["profile_id"]) != self.profile_id:
                raise StoreError("invalid_input")
            self._connection.execute(
                "UPDATE job_batches SET cursor_json = ?, state = 'paused', "
                "pause_reason = ?, updated_at = ? WHERE batch_id = ?",
                (
                    json.dumps(cursor.to_json(), sort_keys=True),
                    reason_code,
                    now,
                    batch_id,
                ),
            )
            self._insert_scheduler_event(
                batch_id=batch_id,
                task_key=None,
                event="round_paused",
                reason_code=reason_code,
                created_at=now,
                payload={
                    "preemptions_used": cursor.preemptions_used,
                    "remaining_budget": cursor.budget.to_json(),
                },
            )
        return {"batch_id": batch_id, "state": "paused", "reason_code": reason_code}

    @_store_operation("pause_batch")
    def safety_stop_after_clock_change(
        self,
        batch_id: str,
        cursor: BatchCursor,
        reason_code: str,
        *,
        now: str,
    ) -> dict[str, object]:
        """时钟异常后的专用安全停止：保存最后可靠游标/预算，不续租/不改时钟锚点。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("invalid_input")
        if (
            not isinstance(cursor, BatchCursor)
            or cursor.batch_id != batch_id
            or reason_code != "clock_changed"
        ):
            raise StoreError("invalid_input")
        del now_dt
        with self._write():
            row = self._connection.execute(
                "SELECT profile_id FROM job_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None or str(row["profile_id"]) != self.profile_id:
                raise StoreError("invalid_input")
            self._connection.execute(
                "UPDATE job_batches SET cursor_json = ?, state = 'paused', "
                "pause_reason = ?, updated_at = ? WHERE batch_id = ?",
                (
                    json.dumps(cursor.to_json(), sort_keys=True),
                    reason_code,
                    now,
                    batch_id,
                ),
            )
            self._run_fault_hook("safety_stop_before_event")
            self._insert_scheduler_event(
                batch_id=batch_id,
                task_key=None,
                event="round_paused",
                reason_code=reason_code,
                created_at=now,
                payload={
                    "preemptions_used": cursor.preemptions_used,
                    "remaining_budget": cursor.budget.to_json(),
                    "safety_stop": True,
                },
            )
        return {"batch_id": batch_id, "state": "paused", "reason_code": reason_code}

    @_store_operation("record_runtime_event")
    def record_runtime_event(
        self,
        batch_id: str,
        event: str,
        *,
        now: str,
        task_key: str | None = None,
        reason_code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        """追加白名单调度事件；payload 不含正文、坐标或原始键值。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None or not is_uuid(batch_id):
            raise StoreError("invalid_input")
        if task_key is not None and not (
            is_job_task_key(task_key) or _is_discovery_task_key(task_key)
        ):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            row = self._connection.execute(
                "SELECT profile_id FROM job_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None or str(row["profile_id"]) != self.profile_id:
                raise StoreError("invalid_input")
            self._insert_scheduler_event(
                batch_id=batch_id,
                task_key=task_key,
                event=event,
                reason_code=reason_code,
                created_at=now,
                payload=payload or {},
            )

    @_store_operation("last_action_state")
    def last_action_state(self, task_key: str) -> str | None:
        """只读返回任务最近动作状态；会话键按真实发现关联，不用全局最近动作。

        职位键直接匹配持久 `task_key`；会话键经由 `hr_discoveries` 的
        `conversation_key` 关联到实际发现任务键，避免把另一任务的最近动作
        当作本任务证据。空键仅保留“是否存在任何动作”的保守探测。
        """
        if task_key:
            if is_job_task_key(task_key):
                row = self._connection.execute(
                    "SELECT state FROM action_records WHERE app = ? AND task_key = ? "
                    "ORDER BY created_at DESC, action_key DESC LIMIT 1",
                    (APP, task_key),
                ).fetchone()
            elif is_conversation_key(APP, task_key):
                row = self._connection.execute(
                    "SELECT ar.state FROM action_records ar "
                    "JOIN hr_discoveries d ON d.app = ar.app "
                    "AND d.\"key\" = ar.discovery_task_key "
                    "WHERE ar.app = ? AND d.conversation_key = ? "
                    "ORDER BY ar.created_at DESC, ar.action_key DESC LIMIT 1",
                    (APP, task_key),
                ).fetchone()
            else:
                return None
        else:
            row = self._connection.execute(
                "SELECT state FROM action_records WHERE app = ? "
                "ORDER BY created_at DESC, action_key DESC LIMIT 1",
                (APP,),
            ).fetchone()
        return None if row is None else str(row["state"])

    @_store_operation("queued_new_message_keys")
    def queued_new_message_keys(self) -> tuple[str, ...]:
        """只读返回同 profile 中仍 queued 的 M2 新消息会话键，按会话去重。"""
        if self.profile_id is None:
            return ()
        rows = self._connection.execute(
            "SELECT nm.conversation_key FROM hr_discoveries nm "
            "WHERE nm.app = ? AND nm.kind = 'new_message' AND nm.state = 'queued' "
            "AND nm.conversation_key IS NOT NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM hr_discoveries u "
            "WHERE u.app = nm.app AND u.conversation_key = nm.conversation_key "
            "AND u.kind = 'unread' "
            "AND u.state IN ('queued', 'leased', 'running', 'done') "
            "AND u.updated_at >= nm.created_at"
            ") "
            "ORDER BY nm.created_at, nm.conversation_key",
            (APP,),
        ).fetchall()
        keys: list[str] = []
        for row in rows:
            key = str(row["conversation_key"])
            if is_conversation_key(APP, key) and key not in keys:
                keys.append(key)
        return tuple(keys)

    @_store_operation("save_draft")
    def save_draft(
        self,
        *,
        draft_id: str,
        task_kind: str,
        task_key: str,
        subject_key: str,
        input_hash: str,
        base_hash: str,
        pack_hash: str,
        model_id: str,
        model_config_hash: str,
        facts_hash: str,
        intent: str,
        sub_intents: tuple[str, ...],
        policy_tier: str,
        reply: str,
        now: str,
    ) -> dict[str, object]:
        """保存本机草稿审阅件；不携带授权、租约或旧 nonce。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("invalid_input")
        if not is_uuid(draft_id):
            raise StoreError("invalid_input")
        if task_kind not in {"discovery", "job"}:
            raise StoreError("invalid_input")
        if not is_nonempty_text(task_key) or not is_nonempty_text(subject_key):
            raise StoreError("invalid_input")
        for name, value in (
            ("input_hash", input_hash),
            ("base_hash", base_hash),
            ("pack_hash", pack_hash),
            ("model_config_hash", model_config_hash),
            ("facts_hash", facts_hash),
        ):
            if not is_hash(value):
                raise StoreError("invalid_input")
        if not is_nonempty_text(model_id) or not is_nonempty_text(intent):
            raise StoreError("invalid_input")
        if not isinstance(sub_intents, tuple) or any(
            not is_nonempty_text(item) for item in sub_intents
        ):
            raise StoreError("invalid_input")
        if policy_tier not in {"A", "B", "C"} or not is_nonempty_text(reply):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            self._connection.execute(
                "UPDATE drafts SET state = 'superseded', updated_at = ? "
                "WHERE profile_id = ? AND task_key = ? AND state = 'pending_review'",
                (now, self.profile_id, task_key),
            )
            self._connection.execute(
                "INSERT INTO drafts (draft_id, profile_id, task_kind, task_key, "
                "subject_key, input_hash, base_hash, pack_hash, model_id, "
                "model_config_hash, facts_hash, intent, sub_intents_json, "
                "policy_tier, reply, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'pending_review', ?, ?)",
                (
                    draft_id,
                    self.profile_id,
                    task_kind,
                    task_key,
                    subject_key,
                    input_hash,
                    base_hash,
                    pack_hash,
                    model_id,
                    model_config_hash,
                    facts_hash,
                    intent,
                    json.dumps(list(sub_intents), sort_keys=True),
                    policy_tier,
                    reply,
                    now,
                    now,
                ),
            )
            self._insert_scheduler_event(
                batch_id=None,
                task_key=task_key,
                event="draft_saved",
                reason_code=None,
                created_at=now,
                payload={
                    "draft_id": draft_id,
                    "input_hash": input_hash,
                    "base_hash": base_hash,
                    "pack_hash": pack_hash,
                },
            )
        return self.get_draft(draft_id) or {}

    @_store_operation("get_draft")
    def get_draft(self, draft_id: str) -> dict[str, object] | None:
        """只读返回一条草稿；不存在返回 None，正文只在本机边界内。"""
        if not is_uuid(draft_id):
            raise StoreError("invalid_input")
        row = self._connection.execute(
            "SELECT * FROM drafts WHERE draft_id = ? AND profile_id = ?",
            (draft_id, self.profile_id),
        ).fetchone()
        return None if row is None else _draft_payload(row)

    @_store_operation("find_draft_by_identity")
    def find_draft_by_identity(
        self,
        *,
        task_key: str,
        subject_key: str,
        input_hash: str,
        base_hash: str,
        pack_hash: str,
        model_id: str,
        model_config_hash: str,
        facts_hash: str,
    ) -> dict[str, object] | None:
        """按完整当前身份查找唯一 pending_review 草稿；不跨身份复用。"""
        if self.profile_id is None:
            raise StoreError("invalid_input")
        for value in (
            input_hash,
            base_hash,
            pack_hash,
            model_config_hash,
            facts_hash,
        ):
            if not is_hash(value):
                raise StoreError("invalid_input")
        row = self._connection.execute(
            "SELECT * FROM drafts WHERE profile_id = ? AND task_key = ? "
            "AND subject_key = ? AND input_hash = ? AND base_hash = ? "
            "AND pack_hash = ? AND model_id = ? AND model_config_hash = ? "
            "AND facts_hash = ? AND state = 'pending_review' "
            "ORDER BY created_at DESC LIMIT 1",
            (
                self.profile_id,
                task_key,
                subject_key,
                input_hash,
                base_hash,
                pack_hash,
                model_id,
                model_config_hash,
                facts_hash,
            ),
        ).fetchone()
        return None if row is None else _draft_payload(row)

    @_store_operation("save_runtime_decision")
    def save_runtime_decision(
        self,
        *,
        subject_key: str,
        task_kind: str,
        input_hash: str,
        base_hash: str,
        pack_hash: str,
        model_id: str,
        model_config_hash: str,
        facts_hash: str,
        intent: str,
        sub_intents: tuple[str, ...],
        policy_tier: str,
        now: str,
    ) -> None:
        """保存最小运行时决策记忆：仅摘要/枚举，不含正文、旧 nonce 或授权。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("invalid_input")
        if task_kind not in {"discovery", "job"} or not is_nonempty_text(subject_key):
            raise StoreError("invalid_input")
        for name, value in (
            ("input_hash", input_hash),
            ("base_hash", base_hash),
            ("pack_hash", pack_hash),
            ("model_config_hash", model_config_hash),
            ("facts_hash", facts_hash),
        ):
            if not is_hash(value):
                raise StoreError("invalid_input")
        if not is_nonempty_text(model_id) or not is_nonempty_text(intent):
            raise StoreError("invalid_input")
        if not isinstance(sub_intents, tuple) or any(
            not is_nonempty_text(item) for item in sub_intents
        ):
            raise StoreError("invalid_input")
        if policy_tier not in {"A", "B", "C"}:
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            self._connection.execute(
                "INSERT INTO runtime_decisions (profile_id, subject_key, task_kind, "
                "input_hash, base_hash, pack_hash, model_id, model_config_hash, "
                "facts_hash, intent, sub_intents_json, policy_tier, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, subject_key, input_hash, base_hash, "
                "pack_hash, model_id, model_config_hash) DO UPDATE SET "
                "facts_hash = excluded.facts_hash, intent = excluded.intent, "
                "sub_intents_json = excluded.sub_intents_json, "
                "policy_tier = excluded.policy_tier, created_at = excluded.created_at",
                (
                    self.profile_id,
                    subject_key,
                    task_kind,
                    input_hash,
                    base_hash,
                    pack_hash,
                    model_id,
                    model_config_hash,
                    facts_hash,
                    intent,
                    json.dumps(list(sub_intents), sort_keys=True),
                    policy_tier,
                    now,
                ),
            )

    @_store_operation("find_runtime_decision")
    def find_runtime_decision(
        self,
        *,
        subject_key: str,
        task_kind: str,
        input_hash: str,
        base_hash: str,
        pack_hash: str,
        model_id: str,
        model_config_hash: str,
        facts_hash: str,
    ) -> dict[str, object] | None:
        """只在全部当前身份（含事实摘要）一致时返回记忆，否则视为失效。"""
        if self.profile_id is None:
            raise StoreError("invalid_input")
        if task_kind not in {"discovery", "job"} or not is_nonempty_text(subject_key):
            raise StoreError("invalid_input")
        for value in (
            input_hash,
            base_hash,
            pack_hash,
            model_config_hash,
            facts_hash,
        ):
            if not is_hash(value):
                raise StoreError("invalid_input")
        row = self._connection.execute(
            "SELECT * FROM runtime_decisions WHERE profile_id = ? AND "
            "subject_key = ? AND task_kind = ? AND input_hash = ? AND "
            "base_hash = ? AND pack_hash = ? AND model_id = ? AND "
            "model_config_hash = ? AND facts_hash = ?",
            (
                self.profile_id,
                subject_key,
                task_kind,
                input_hash,
                base_hash,
                pack_hash,
                model_id,
                model_config_hash,
                facts_hash,
            ),
        ).fetchone()
        if row is None:
            return None
        return {
            "subject_key": str(row["subject_key"]),
            "task_kind": str(row["task_kind"]),
            "input_hash": str(row["input_hash"]),
            "base_hash": str(row["base_hash"]),
            "pack_hash": str(row["pack_hash"]),
            "model_id": str(row["model_id"]),
            "model_config_hash": str(row["model_config_hash"]),
            "facts_hash": str(row["facts_hash"]),
            "intent": str(row["intent"]),
            "sub_intents": tuple(json.loads(str(row["sub_intents_json"]))),
            "policy_tier": str(row["policy_tier"]),
            "created_at": str(row["created_at"]),
        }

    def _insert_scheduler_event(
        self,
        *,
        batch_id: str | None,
        task_key: str | None,
        event: str,
        reason_code: str | None,
        created_at: str,
        payload: dict[str, object],
    ) -> None:
        """在同一事务追加调度事件；调用方已持有写事务。"""
        if event not in {
            "run_started",
            "run_finished",
            "checkpoint_committed",
            "stage_preempted",
            "round_paused",
            "unread_verified",
            "draft_saved",
            "decision_reused",
            "job_claimed",
            "job_started",
            "job_finished",
            "batch_resumed",
        }:
            raise StoreError("invalid_input")
        if reason_code is not None and reason_code not in PAUSE_REASONS:
            raise StoreError("invalid_input")
        if not isinstance(payload, dict):
            raise StoreError("invalid_input")
        self._connection.execute(
            "INSERT INTO scheduler_events(batch_id, task_key, event, reason_code, "
            "created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                task_key,
                event,
                reason_code,
                created_at,
                json.dumps(payload, sort_keys=True),
            ),
        )

    @_store_operation("finish_semantic_block")
    def finish_semantic_block(
        self, lease: Lease, now: str, reason_code: str
    ) -> str:
        """仅语义未决时阻塞任务并释放安全设备行；不做设备隔离。"""
        now_dt = self._prepare_now(now)
        if reason_code not in {"human_review_required", "invalid_decision", "unread_unverified", "identity_ambiguous", "incomplete_snapshot", "stale_snapshot", "stale_decision", "policy_changed", "input_too_large", "decision_timeout"}:
            raise StoreError("invalid_input")
        if not is_task_key(lease.key):
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            row, _device = self._match_current_lease(
                lease, {"running"}, now_dt, allow_expired=False
            )
            table = "jobs" if is_job_task_key(lease.key) else "hr_discoveries"
            key_column = "job_key" if is_job_task_key(lease.key) else "\"key\""
            cursor = self._connection.execute(
                f"UPDATE {table} SET state = 'blocked', reason_code = ?, "
                "lease_owner = NULL, owner_pid = NULL, owner_host = NULL, "
                "nonce = NULL, lease_expires_at = NULL, updated_at = ? "
                f"WHERE app = ? AND {key_column} = ? AND state = 'running' "
                "AND lease_owner = ? AND owner_pid = ? AND owner_host = ? "
                "AND nonce = ? AND lease_expires_at = ?",
                (
                    reason_code,
                    now,
                    APP,
                    lease.key,
                    lease.owner.id,
                    lease.owner.pid,
                    lease.owner.host,
                    lease.nonce,
                    str(row["lease_expires_at"]),
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
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
                    str(row["lease_expires_at"]),
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("stale_lease")
            self._insert_event(
                app=APP,
                subject_key=lease.key,
                event="task_finished",
                reason_code=reason_code,
                created_at=now,
                payload=_event_payload("running", "blocked", lease.nonce, None),
            )
        return "updated"

    @_store_operation("begin_action", stage="action")
    def begin_action(
        self,
        lease: Lease,
        *,
        action_kind: str,
        action_key: str,
        target_key: str,
        conversation_key: str,
        authorization_json: str,
        daily_sends: int,
        reply_cooldown_seconds: int,
        ime_original: str | None,
        now: str,
        apply_allowance: int | None = None,
    ) -> dict[str, object]:
        """原子准入并写 pending 与唯一动作键锁；额度冷却不通过则不调用执行器。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("action_not_ready")
        task_kind = "job" if is_job_task_key(lease.key) else "discovery"
        try:
            if action_kind not in ACTION_KIND_SET or not is_action_key(
                action_kind, action_key
            ):
                raise StoreError("invalid_action_key")
            validate_action_target(action_kind, action_key, target_key)
            if action_kind == "apply_job":
                if task_kind != "job" or target_key != lease.key:
                    raise StoreError("invalid_action_key")
                if type(apply_allowance) is not int or apply_allowance < 1:
                    raise StoreError("quota_exceeded")
            else:
                if task_kind != "discovery":
                    raise StoreError("invalid_action_key")
                if apply_allowance is not None:
                    raise StoreError("invalid_input")
                _validate_conversation_key(conversation_key)
                if action_kind != "send_text" and target_key != conversation_key:
                    raise StoreError("invalid_action_key")
            if (
                type(daily_sends) is not int
                or daily_sends < 1
                or type(reply_cooldown_seconds) is not int
                or reply_cooldown_seconds < 0
            ):
                raise StoreError("invalid_input")
            if not isinstance(authorization_json, str):
                raise StoreError("invalid_input")
            payload = json.loads(authorization_json)
            if not isinstance(payload, dict):
                raise StoreError("invalid_input")
            if ime_original is not None and (
                not isinstance(ime_original, str) or not ime_original.strip()
            ):
                raise StoreError("invalid_input")
        except StoreError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError):
            raise StoreError("invalid_input") from None
        self._check_clock(now_dt)

        with self._write():
            row, _ = self._match_current_lease(
                lease, {"running"}, now_dt, allow_expired=False
            )
            if action_kind == "apply_job":
                expected_conversation = (
                    row["conversation_key"]
                    if row["conversation_key"] is not None
                    else lease.key
                )
                if conversation_key != expected_conversation:
                    raise StoreError("invalid_action_key")
            elif str(row["conversation_key"]) != conversation_key:
                raise StoreError("invalid_action_key")
            existing = self._connection.execute(
                "SELECT state FROM action_records WHERE action_key = ?",
                (action_key,),
            ).fetchone()
            if existing is not None:
                raise StoreError("action_conflict")
            active = self._connection.execute(
                "SELECT 1 FROM action_records WHERE app = ? AND task_key = ? "
                "AND state IN ('pending', 'unknown', 'cancelled') LIMIT 1",
                (APP, lease.key),
            ).fetchone()
            if active is not None:
                raise StoreError("action_conflict")
            if action_kind == "send_text":
                self._require_text_admission(
                    now_dt,
                    daily_sends=daily_sends,
                    reply_cooldown_seconds=reply_cooldown_seconds,
                    conversation_key=conversation_key,
                )
            if action_kind == "apply_job":
                used = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM action_records WHERE app = ? "
                        "AND task_key = ? AND action_kind = 'apply_job' "
                        "AND outcome = 'performed'",
                        (APP, lease.key),
                    ).fetchone()[0]
                )
                if used >= int(apply_allowance):  # type: ignore[arg-type]
                    raise StoreError("quota_exceeded")
            self._run_fault_hook("action_before_insert")
            self._connection.execute(
                "INSERT INTO action_records (action_key, app, action_kind, task_kind, "
                "task_key, discovery_task_key, job_task_key, target_key, "
                "conversation_key, state, authorization_json, "
                "lease_owner, owner_pid, owner_host, nonce, lease_expires_at, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_key,
                    APP,
                    action_kind,
                    task_kind,
                    lease.key,
                    lease.key if task_kind == "discovery" else None,
                    lease.key if task_kind == "job" else None,
                    target_key,
                    conversation_key,
                    authorization_json,
                    lease.owner.id,
                    lease.owner.pid,
                    lease.owner.host,
                    lease.nonce,
                    str(row["lease_expires_at"]),
                    now,
                    now,
                ),
            )
            if ime_original is not None:
                self._connection.execute(
                    "INSERT INTO action_ime_state (singleton, action_key, original_ime, "
                    "current_ime, saved_at, restored, updated_at) "
                    "VALUES (1, ?, ?, ?, ?, 0, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET action_key = excluded.action_key, "
                    "original_ime = excluded.original_ime, "
                    "current_ime = excluded.current_ime, saved_at = excluded.saved_at, "
                    "restored = 0, updated_at = excluded.updated_at",
                    (action_key, ime_original, ime_original, now, now),
                )
            self._insert_action_event(
                action_key=action_key,
                event="action_pending",
                from_state=None,
                to_state="pending",
                reason_code=None,
                created_at=now,
                payload={"action_kind": action_kind, "nonce": lease.nonce},
            )
        return {
            "action_key": action_key,
            "action_kind": action_kind,
            "task_key": lease.key,
            "target_key": target_key,
            "conversation_key": conversation_key,
            "state": "pending",
            "created_at": now,
        }

    @_store_operation("complete_action", stage="action")
    def complete_action(
        self,
        lease: Lease,
        *,
        action_key: str,
        outcome: ActionOutcome,
        now: str,
    ) -> dict[str, object]:
        """同一事务提交动作证据、任务终态、设备处置与两类审计事件。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("action_not_ready")
        if not isinstance(outcome, ActionOutcome):
            raise StoreError("invalid_observation")
        try:
            validate_evidence(outcome.evidence)
        except ValueError:
            raise StoreError("invalid_observation") from None
        self._check_clock(now_dt)

        with self._write():
            task_row, _ = self._match_current_lease(
                lease, {"running"}, now_dt, allow_expired=False
            )
            record = self._connection.execute(
                "SELECT * FROM action_records WHERE action_key = ?",
                (action_key,),
            ).fetchone()
            if record is None:
                raise StoreError("action_not_found")
            if (
                str(record["state"]) != "pending"
                or str(record["task_key"]) != lease.key
                or not is_action_key(str(record["action_kind"]), action_key)
            ):
                raise StoreError("action_not_ready")
            action_kind = str(record["action_kind"])
            evidence = outcome.evidence
            if evidence.action_key != action_key:
                raise StoreError("invalid_observation")
            try:
                created_at = parse_utc(str(record["created_at"]))
                observed_at = parse_utc(evidence.observed_at)
            except ValueError:
                raise StoreError("invalid_observation") from None
            if observed_at < created_at or observed_at > now_dt:
                raise StoreError("invalid_observation")
            if evidence.outcome == "performed":
                allowed = {expected_effect(action_kind)}
                allowed.add(expected_effect(action_kind, delivered=True))
                if evidence.effect not in allowed:
                    raise StoreError("invalid_observation")
            elif evidence.effect != "none":
                raise StoreError("invalid_observation")

            if evidence.outcome == "performed" and not outcome.pause:
                record_state = "performed"
                record_outcome: str | None = "performed"
                record_effect: str | None = evidence.effect
                record_reason: str | None = None
                task_state = "done"
                task_reason: str | None = None
            elif evidence.outcome == "not_performed" and not outcome.pause:
                record_state = "not_performed"
                record_outcome = "not_performed"
                record_effect = "none"
                record_reason = evidence.reason_code
                task_state = "not_sent"
                task_reason = evidence.reason_code
            elif evidence.outcome == "not_performed":
                record_state = "pending"
                record_outcome = None
                record_effect = None
                record_reason = "result_unverified"
                task_state = "blocked"
                task_reason = "result_unverified"
            elif evidence.outcome == "performed":
                record_state = "unknown"
                record_outcome = "unknown"
                record_effect = "none"
                record_reason = "result_unverified"
                task_state = "blocked"
                task_reason = "result_unverified"
            else:
                record_state = "unknown"
                record_outcome = "unknown"
                record_effect = "none"
                record_reason = evidence.reason_code
                task_state = "blocked"
                task_reason = evidence.reason_code

            self._run_fault_hook("action_before_record_update")
            cursor = self._connection.execute(
                "UPDATE action_records SET state = ?, outcome = ?, effect = ?, "
                "reason_code = ?, before_snapshot_id = ?, after_snapshot_id = ?, "
                "observed_at = ?, scene_settled = ?, confirmation_open = ?, "
                "ime_restored = ?, command_quiet = ?, updated_at = ? "
                "WHERE action_key = ? AND state = 'pending'",
                (
                    record_state,
                    record_outcome,
                    record_effect,
                    record_reason,
                    evidence.before_snapshot_id,
                    evidence.after_snapshot_id,
                    evidence.observed_at,
                    1 if outcome.scene_settled else 0,
                    1 if outcome.confirmation_open else 0,
                    1 if outcome.ime_restored else 0,
                    1 if outcome.command_quiet else 0,
                    now,
                    action_key,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("action_not_ready")
            if outcome.ime_restored:
                self._connection.execute(
                    "UPDATE action_ime_state SET restored = 1, "
                    "current_ime = original_ime, updated_at = ? "
                    "WHERE singleton = 1 AND action_key = ?",
                    (now, action_key),
                )
            self._run_fault_hook("action_before_task_update")
            self._apply_task_terminal(
                task_row,
                lease,
                now=now,
                task_state=task_state,
                reason_code=task_reason,
                from_state="running",
                lease_expires_at=str(task_row["lease_expires_at"]),
                keep_device=(task_state == "blocked"),
            )
            self._run_fault_hook("action_before_event")
            self._insert_action_event(
                action_key=action_key,
                event="action_completed",
                from_state="pending",
                to_state=record_state,
                reason_code=record_reason,
                created_at=now,
                payload={"action_kind": action_kind, "evidence_outcome": evidence.outcome},
            )
        return {
            "action_key": action_key,
            "action_kind": action_kind,
            "task_key": lease.key,
            "state": record_state,
            "evidence": {
                "action_key": action_key,
                "outcome": evidence.outcome,
                "effect": evidence.effect,
                "reason_code": evidence.reason_code,
                "before_snapshot_id": evidence.before_snapshot_id,
                "after_snapshot_id": evidence.after_snapshot_id,
                "observed_at": evidence.observed_at,
            },
            "delivered": bool(outcome.delivered and record_state == "performed"),
            "gate_converged": record_state in {"performed", "not_performed"}
            and not outcome.pause,
        }

    @_store_operation("freeze_action", stage="action")
    def freeze_attempted_action(
        self,
        lease: Lease,
        *,
        action_key: str,
        reason_code: str,
        now: str,
    ) -> dict[str, object]:
        """已尝试动作在失效租约/共享异常下按原身份冻结为 unknown/blocked。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("action_not_ready")
        if not isinstance(reason_code, str) or not reason_code:
            raise StoreError("invalid_input")
        self._check_clock(now_dt)
        with self._write():
            task_row, _device = self._match_current_lease(
                lease, {"leased", "running"}, now_dt, allow_expired=True
            )
            record = self._connection.execute(
                "SELECT * FROM action_records WHERE action_key = ?",
                (action_key,),
            ).fetchone()
            if (
                record is None
                or str(record["state"]) != "pending"
                or str(record["task_key"]) != lease.key
                or str(record["nonce"]) != lease.nonce
                or str(record["lease_owner"]) != lease.owner.id
                or int(record["owner_pid"]) != lease.owner.pid
            ):
                raise StoreError("action_not_ready")
            cursor = self._connection.execute(
                "UPDATE action_records SET state = 'unknown', outcome = 'unknown', "
                "effect = 'none', reason_code = ?, observed_at = ?, updated_at = ? "
                "WHERE action_key = ? AND state = 'pending'",
                (reason_code, now, now, action_key),
            )
            if cursor.rowcount != 1:
                raise StoreError("action_not_ready")
            self._apply_task_terminal(
                task_row,
                lease,
                now=now,
                task_state="blocked",
                reason_code=reason_code,
                from_state=str(task_row["state"]),
                lease_expires_at=str(task_row["lease_expires_at"]),
                keep_device=True,
            )
            self._insert_action_event(
                action_key=action_key,
                event="action_completed",
                from_state="pending",
                to_state="unknown",
                reason_code=reason_code,
                created_at=now,
                payload={"action_kind": str(record["action_kind"]), "freeze": True},
            )
        return {
            "action_key": action_key,
            "action_kind": str(record["action_kind"]),
            "task_key": lease.key,
            "state": "unknown",
            "evidence": {
                "action_key": action_key,
                "outcome": "unknown",
                "effect": "none",
                "reason_code": reason_code,
                "before_snapshot_id": None,
                "after_snapshot_id": None,
                "observed_at": now,
            },
            "delivered": False,
            "gate_converged": False,
            "frozen": True,
        }

    @_store_operation("reconcile_action", stage="action")
    def reconcile_action(
        self,
        *,
        action_key: str,
        resolution: str,
        evidence: Evidence | None,
        command_quiet: bool,
        scene_settled: bool,
        confirmation_open: bool,
        ime_restored: bool,
        explicit_confirm: bool,
        command_stopped: bool,
        now: str,
    ) -> dict[str, object]:
        """人工确认/放弃固定三项；abandon 只关任务，未决锁与设备隔离保留。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("action_not_ready")
        if resolution not in {
            "confirm_performed",
            "confirm_not_performed",
            "abandon",
        }:
            raise StoreError("invalid_input")
        if explicit_confirm is not True:
            raise StoreError("recovery_not_confirmed")
        if type(command_stopped) is not bool:
            raise StoreError("invalid_input")
        for name, value in (
            ("command_quiet", command_quiet),
            ("scene_settled", scene_settled),
            ("confirmation_open", confirmation_open),
            ("ime_restored", ime_restored),
        ):
            if type(value) is not bool:
                raise StoreError("invalid_input")
        if resolution != "abandon":
            if evidence is None:
                raise StoreError("invalid_observation")
            try:
                validate_evidence(evidence)
            except ValueError:
                raise StoreError("invalid_observation") from None
            if (
                not command_quiet
                or not scene_settled
                or confirmation_open
                or not ime_restored
            ):
                raise StoreError("scene_unsettled")
        self._check_clock(now_dt)

        with self._write():
            record = self._connection.execute(
                "SELECT * FROM action_records WHERE action_key = ?",
                (action_key,),
            ).fetchone()
            if record is None:
                raise StoreError("action_not_found")
            from_state = str(record["state"])
            if from_state not in {"pending", "unknown", "cancelled"}:
                raise StoreError("action_not_ready")
            if from_state == "cancelled" and resolution == "abandon":
                raise StoreError("action_not_ready")
            if str(record["owner_host"]) != self._local_host:
                raise StoreError("owner_not_stopped")
            action_kind = str(record["action_kind"])
            task = self._select_task_row(str(record["task_key"]))
            if task is None:
                raise StoreError("action_not_ready")
            allowed_task_states = {"blocked", "cancelled"} if from_state == "cancelled" else {
                "leased",
                "running",
                "blocked",
            }
            if str(task["state"]) not in allowed_task_states:
                raise StoreError("action_not_ready")
            if str(task["state"]) in {"leased", "running"} and (
                task["nonce"] != record["nonce"]
                or task["lease_owner"] != record["lease_owner"]
                or task["owner_pid"] != record["owner_pid"]
                or task["owner_host"] != record["owner_host"]
            ):
                raise StoreError("stale_lease")
            if resolution != "abandon":
                if evidence is None or evidence.action_key != action_key:
                    raise StoreError("invalid_observation")
                if resolution == "confirm_performed":
                    if evidence.outcome != "performed":
                        raise StoreError("invalid_observation")
                    if evidence.effect not in {
                        expected_effect(action_kind),
                        expected_effect(action_kind, delivered=True),
                    }:
                        raise StoreError("invalid_observation")
                elif (
                    evidence.outcome != "not_performed"
                    or evidence.effect != "none"
                ):
                    raise StoreError("invalid_observation")
                if self._stop_checker(int(record["owner_pid"])) != "stopped":
                    raise StoreError("owner_not_stopped")
                if not command_stopped:
                    raise StoreError("owner_not_stopped")

            if resolution == "confirm_performed":
                new_record_state = "performed"
                record_outcome: str | None = "performed"
                record_effect: str | None = None if evidence is None else evidence.effect
                record_reason: str | None = None
                task_state = "cancelled" if from_state == "cancelled" else "done"
                task_reason: str | None = (
                    "cancelled_before_action" if from_state == "cancelled" else None
                )
                keep_device = False
            elif resolution == "confirm_not_performed":
                new_record_state = "not_performed"
                record_outcome = "not_performed"
                record_effect = "none"
                record_reason = None if evidence is None else evidence.reason_code
                task_state = "cancelled" if from_state == "cancelled" else "not_sent"
                task_reason = (
                    "cancelled_before_action"
                    if from_state == "cancelled"
                    else record_reason
                )
                keep_device = False
            else:
                new_record_state = "cancelled"
                record_outcome = None
                record_effect = None
                record_reason = None
                task_state = "cancelled"
                task_reason = "cancelled_before_action"
                keep_device = True

            self._run_fault_hook("reconcile_before_record")
            cursor = self._connection.execute(
                "UPDATE action_records SET state = ?, outcome = ?, effect = ?, "
                "reason_code = ?, before_snapshot_id = CASE WHEN ? IS NULL THEN before_snapshot_id ELSE ? END, "
                "after_snapshot_id = CASE WHEN ? IS NULL THEN after_snapshot_id ELSE ? END, "
                "observed_at = CASE WHEN ? IS NULL THEN observed_at ELSE ? END, "
                "scene_settled = ?, confirmation_open = ?, ime_restored = ?, "
                "command_quiet = ?, updated_at = ? "
                "WHERE action_key = ? AND state = ?",
                (
                    new_record_state,
                    record_outcome,
                    record_effect,
                    record_reason,
                    None if evidence is None else "x",
                    None if evidence is None else evidence.before_snapshot_id,
                    None if evidence is None else "x",
                    None if evidence is None else evidence.after_snapshot_id,
                    None if evidence is None else "x",
                    None if evidence is None else evidence.observed_at,
                    1 if scene_settled else 0,
                    1 if confirmation_open else 0,
                    1 if ime_restored else 0,
                    1 if command_quiet else 0,
                    now,
                    action_key,
                    from_state,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("action_not_ready")
            if ime_restored:
                self._connection.execute(
                    "UPDATE action_ime_state SET restored = 1, "
                    "current_ime = original_ime, updated_at = ? "
                    "WHERE singleton = 1 AND action_key = ?",
                    (now, action_key),
                )
            self._run_fault_hook("reconcile_before_task")
            if from_state == "cancelled":
                self._connection.execute(
                    "DELETE FROM device_lease WHERE device_key = 'primary'"
                )
            else:
                self._apply_reconciled_task(
                    task,
                    now=now,
                    task_state=task_state,
                    reason_code=task_reason,
                    keep_device=keep_device,
                )
            self._run_fault_hook("reconcile_before_event")
            self._insert_action_event(
                action_key=action_key,
                event="action_reconciled",
                from_state=from_state,
                to_state=new_record_state,
                reason_code=task_reason,
                created_at=now,
                payload={"resolution": resolution},
            )
        return {
            "action_key": action_key,
            "action_kind": action_kind,
            "task_key": str(record["task_key"]),
            "resolution": resolution,
            "state": new_record_state,
            "task_state": task_state,
            "gate_converged": resolution != "abandon",
            "lock_retained": resolution == "abandon",
        }

    @_store_operation("get_action", stage="action")
    def get_action(self, action_key: str) -> dict[str, object] | None:
        """只读返回一条动作记录，含固定标志位与证据摘要。"""
        if not isinstance(action_key, str) or not action_key:
            raise StoreError("invalid_input")
        try:
            row = self._connection.execute(
                "SELECT * FROM action_records WHERE action_key = ?",
                (action_key,),
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise map_locked_error(error) from None
        except sqlite3.Error:
            raise StoreError("store_io_error") from None
        return None if row is None else _action_row_payload(row)

    @_store_operation("pending_actions", stage="action")
    def pending_actions(self, app: str = APP) -> list[dict[str, object]]:
        """只读返回 pending/unknown/cancelled 动作，按创建时间与键排序。"""
        if app != APP:
            raise StoreError("invalid_input")
        try:
            rows = self._connection.execute(
                "SELECT * FROM action_records WHERE app = ? "
                "AND state IN ('pending', 'unknown', 'cancelled') "
                "ORDER BY created_at, action_key",
                (app,),
            ).fetchall()
        except sqlite3.OperationalError as error:
            raise map_locked_error(error) from None
        except sqlite3.Error:
            raise StoreError("store_io_error") from None
        return [_action_row_payload(row) for row in rows]

    @_store_operation("save_ime", stage="action")
    def save_ime_state(
        self, action_key: str, original_ime: str, *, now: str
    ) -> None:
        """在切换 IME 前持久化原值；只允许当前 pending 动作写入。"""
        if self.profile_id is None:
            raise StoreError("action_not_ready")
        if not isinstance(action_key, str) or not isinstance(original_ime, str):
            raise StoreError("invalid_input")
        if not original_ime.strip():
            raise StoreError("invalid_input")
        self._prepare_now(now)
        with self._write():
            record = self._connection.execute(
                "SELECT state FROM action_records WHERE action_key = ?",
                (action_key,),
            ).fetchone()
            if record is None or str(record["state"]) != "pending":
                raise StoreError("action_not_ready")
            self._connection.execute(
                "INSERT INTO action_ime_state (singleton, action_key, original_ime, "
                "current_ime, saved_at, restored, updated_at) "
                "VALUES (1, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET action_key = excluded.action_key, "
                "original_ime = excluded.original_ime, "
                "current_ime = excluded.current_ime, saved_at = excluded.saved_at, "
                "restored = 0, updated_at = excluded.updated_at",
                (action_key, original_ime, original_ime, now, now),
            )

    @_store_operation("text_admission", stage="action")
    def text_admission_status(
        self,
        *,
        now: str,
        daily_sends: int,
        reply_cooldown_seconds: int,
        conversation_key: str,
    ) -> dict[str, object]:
        """只读计算文本资格；库不可读时报错而不是返回默认零已用。"""
        now_dt = self._prepare_now(now)
        if self.profile_id is None:
            raise StoreError("action_not_ready")
        try:
            _validate_conversation_key(conversation_key)
            if (
                type(daily_sends) is not int
                or daily_sends < 1
                or type(reply_cooldown_seconds) is not int
                or reply_cooldown_seconds < 0
            ):
                raise ValueError("limits are invalid")
        except ValueError:
            raise StoreError("invalid_input") from None
        try:
            status = self._text_admission(
                now_dt,
                daily_sends=daily_sends,
                reply_cooldown_seconds=reply_cooldown_seconds,
                conversation_key=conversation_key,
            )
        except sqlite3.OperationalError as error:
            raise map_locked_error(error) from None
        except sqlite3.Error:
            raise StoreError("store_io_error") from None
        return status

    @_store_operation("load_ime", stage="action")
    def load_ime_state(self) -> dict[str, object] | None:
        """只读返回持久化的原 IME 与恢复状态，供重启后继续收敛。"""
        if self.profile_id is None:
            raise StoreError("action_not_ready")
        try:
            row = self._connection.execute(
                "SELECT * FROM action_ime_state WHERE singleton = 1"
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise map_locked_error(error) from None
        except sqlite3.Error:
            raise StoreError("store_io_error") from None
        if row is None:
            return None
        return {
            "action_key": str(row["action_key"]),
            "original_ime": str(row["original_ime"]),
            "current_ime": None if row["current_ime"] is None else str(row["current_ime"]),
            "saved_at": str(row["saved_at"]),
            "restored": bool(row["restored"]),
            "updated_at": str(row["updated_at"]),
        }

    def _require_text_admission(
        self,
        now_dt: datetime,
        *,
        daily_sends: int,
        reply_cooldown_seconds: int,
        conversation_key: str,
    ) -> None:
        """在 pending 插入同一事务内拒绝额度、冷却或未决文本发送。"""
        status = self._text_admission(
            now_dt,
            daily_sends=daily_sends,
            reply_cooldown_seconds=reply_cooldown_seconds,
            conversation_key=conversation_key,
        )
        if status["reason"] is not None:
            raise StoreError(str(status["reason"]))

    def _text_admission(
        self,
        now_dt: datetime,
        *,
        daily_sends: int,
        reply_cooldown_seconds: int,
        conversation_key: str,
    ) -> dict[str, object]:
        """按 UTC 日期、跨 app 文本成功与按会话冷却计算唯一文本资格。"""
        now_text = format_utc(now_dt)
        day = now_text[:10]
        daily_count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM action_records "
                "WHERE action_kind = 'send_text' AND state = 'performed' "
                "AND effect = 'sent' AND substr(observed_at, 1, 10) = ?",
                (day,),
            ).fetchone()[0]
        )
        pending_text = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM action_records "
                "WHERE action_kind = 'send_text' AND state IN ('pending', 'unknown')"
            ).fetchone()[0]
        )
        last_row = self._connection.execute(
            "SELECT MAX(observed_at) AS last_at FROM action_records "
            "WHERE action_kind = 'send_text' AND state = 'performed' "
            "AND effect = 'sent' AND conversation_key = ?",
            (conversation_key,),
        ).fetchone()
        last_at = None if last_row is None else last_row["last_at"]
        cooldown_until: str | None = None
        cooldown_active = False
        if last_at is not None:
            cooldown_until = add_seconds(str(last_at), reply_cooldown_seconds)
            cooldown_active = now_dt < parse_utc(cooldown_until)
        if pending_text:
            reason: str | None = "text_send_blocked"
        elif daily_count >= daily_sends:
            reason = "quota_exceeded"
        elif cooldown_active:
            reason = "cooldown_active"
        else:
            reason = None
        return {
            "eligible": reason is None,
            "reason": reason,
            "daily_count": daily_count,
            "daily_limit": daily_sends,
            "pending_text": pending_text,
            "cooldown_seconds": reply_cooldown_seconds,
            "last_sent_at": None if last_at is None else str(last_at),
            "cooldown_until": cooldown_until,
        }

    def _apply_task_terminal(
        self,
        task_row: sqlite3.Row,
        lease: Lease,
        *,
        now: str,
        task_state: str,
        reason_code: str | None,
        from_state: str,
        lease_expires_at: str,
        keep_device: bool,
    ) -> None:
        """在同一事务清任务租约并按 blocked/终态处理设备行与任务事件。"""
        if is_job_task_key(lease.key):
            table = "jobs"
            key_column = "job_key"
        else:
            table = "hr_discoveries"
            key_column = "\"key\""
        cursor = self._connection.execute(
            f"UPDATE {table} SET state = ?, reason_code = ?, "
            "lease_owner = NULL, owner_pid = NULL, owner_host = NULL, "
            "nonce = NULL, lease_expires_at = NULL, updated_at = ? "
            f"WHERE app = ? AND {key_column} = ? AND state = ? "
            "AND lease_owner = ? AND owner_pid = ? AND owner_host = ? "
            "AND nonce = ? AND lease_expires_at = ?",
            (
                task_state,
                reason_code,
                now,
                lease.app,
                lease.key,
                from_state,
                lease.owner.id,
                lease.owner.pid,
                lease.owner.host,
                lease.nonce,
                lease_expires_at,
            ),
        )
        if cursor.rowcount != 1:
            raise StoreError("stale_lease")
        if keep_device:
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
                    lease_expires_at,
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
                    lease_expires_at,
                ),
            )
        if cursor.rowcount != 1:
            raise StoreError("stale_lease")
        self._insert_event(
            app=lease.app,
            subject_key=lease.key,
            event="task_finished",
            reason_code=reason_code,
            created_at=now,
            payload=_event_payload(from_state, task_state, lease.nonce, None),
        )

    def _apply_reconciled_task(
        self,
        task: sqlite3.Row,
        *,
        now: str,
        task_state: str,
        reason_code: str | None,
        keep_device: bool,
    ) -> None:
        """人工对账后清理旧任务租约；abandon 保留隔离设备行。"""
        task_key = str(task["job_key"]) if "job_key" in task.keys() else str(task["key"])
        if is_job_task_key(task_key):
            table = "jobs"
            key_column = "job_key"
        else:
            table = "hr_discoveries"
            key_column = "\"key\""
        cursor = self._connection.execute(
            f"UPDATE {table} SET state = ?, reason_code = ?, "
            "lease_owner = NULL, owner_pid = NULL, owner_host = NULL, "
            "nonce = NULL, lease_expires_at = NULL, updated_at = ? "
            f"WHERE app = ? AND {key_column} = ? AND state = ?",
            (
                task_state,
                reason_code,
                now,
                APP,
                task_key,
                str(task["state"]),
            ),
        )
        if cursor.rowcount != 1:
            raise StoreError("stale_lease")
        if keep_device:
            self._connection.execute(
                "UPDATE device_lease SET reservation_state = 'quarantined' "
                "WHERE device_key = 'primary' AND reservation_state = 'held'"
            )
        else:
            self._connection.execute(
                "DELETE FROM device_lease WHERE device_key = 'primary'"
            )
        self._insert_event(
            app=APP,
            subject_key=task_key,
            event="task_finished",
            reason_code=reason_code,
            created_at=now,
            payload=_event_payload(str(task["state"]), task_state, None, None),
        )

    def _insert_action_event(
        self,
        *,
        action_key: str,
        event: str,
        from_state: str | None,
        to_state: str,
        reason_code: str | None,
        created_at: str,
        payload: dict[str, object],
    ) -> None:
        """追加不含正文/坐标的动作审计事件。"""
        validate_event_reason(reason_code)
        self._connection.execute(
            "INSERT INTO action_events (action_key, event, from_state, to_state, "
            "reason_code, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                action_key,
                event,
                from_state,
                to_state,
                reason_code,
                created_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    @contextmanager
    def _operation(self, name: str, *, stage: str = "store") -> Iterator[None]:
        """记录公开操作的 started/ok/failed；日志失败只降级，不影响业务结果。"""
        started = time.perf_counter()
        self.process_log.record(stage=stage, operation=name, result="started")
        try:
            try:
                yield
            except StoreError as error:
                self.process_log.record(
                    stage=stage,
                    operation=name,
                    result="failed",
                    reason_code=error.reason_code,
                    error_category="store",
                    duration_ms=elapsed_ms(started),
                )
                raise
            except Exception:
                self.process_log.record(
                    stage=stage,
                    operation=name,
                    result="failed",
                    error_category="internal",
                    duration_ms=elapsed_ms(started),
                )
                raise
            else:
                self.process_log.record(
                    stage=stage,
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

    def _match_current_lease(
        self,
        lease: Lease,
        states: set[str] | frozenset[str],
        now_dt: datetime,
        *,
        allow_expired: bool,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        """按 app/key/owner/pid/host/nonce 匹配当前行，使用库中真实截止时间。"""
        try:
            validate_task_lease(lease)
        except ValueError:
            raise StoreError("invalid_input") from None
        row = self._select_task_row(lease.key)
        device = self._select_device()
        if row is None or device is None:
            raise StoreError("stale_lease")
        if (
            str(row["state"]) not in states
            or row["lease_owner"] != lease.owner.id
            or row["owner_pid"] != lease.owner.pid
            or row["owner_host"] != lease.owner.host
            or row["nonce"] != lease.nonce
            or str(device["reservation_state"]) != "held"
            or device["lease_owner"] != lease.owner.id
            or device["owner_pid"] != lease.owner.pid
            or device["owner_host"] != lease.owner.host
            or device["nonce"] != lease.nonce
            or str(row["lease_expires_at"]) != str(device["lease_expires_at"])
        ):
            raise StoreError("stale_lease")
        if not allow_expired and (
            now_dt >= parse_utc(str(row["lease_expires_at"]))
            or now_dt >= parse_utc(str(device["lease_expires_at"]))
        ):
            raise StoreError("stale_lease")
        return row, device

    def _match_lease(
        self,
        lease: Lease,
        states: set[str] | frozenset[str],
        now_dt: datetime,
        *,
        allow_expired: bool,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        """核对任务与 held 设备行的持有者、nonce 与取得时截止；不匹配即 stale。"""
        row, device = self._match_current_lease(
            lease, states, now_dt, allow_expired=allow_expired
        )
        if (
            str(row["lease_expires_at"]) != lease.expires_at
            or str(device["lease_expires_at"]) != lease.expires_at
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

    def _select_task_row(self, key: str) -> sqlite3.Row | None:
        """按任务键前缀读取发现行或职位行，避免跨类型误取。"""
        if is_job_task_key(key):
            if self.profile_id is None:
                raise StoreError("invalid_input")
            return self._connection.execute(
                "SELECT * FROM jobs WHERE app = ? AND job_key = ?",
                (APP, key),
            ).fetchone()
        return self._select_discovery(key)

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


def _validate_conversation_key(conversation_key: object) -> None:
    """要求目标会话键是当前 app 的规范会话键。"""
    if not is_conversation_key(APP, conversation_key):
        raise ValueError("conversation key is invalid")


def _validate_target_key(action_kind: str, target_key: object) -> None:
    """文本目标必须是稳定消息键，申请目标是职位键，其它是规范会话键。"""
    if action_kind == "send_text":
        if not is_message_key(target_key):
            raise ValueError("target key is invalid")
        return
    if action_kind == "apply_job":
        if not is_job_key(target_key):
            raise ValueError("target key is invalid")
        return
    _validate_conversation_key(target_key)


def _is_discovery_task_key(value: object) -> bool:
    """判断是否为规范发现任务键（不依赖 reading 模块）。"""
    if not isinstance(value, str):
        return False
    prefix = f"{APP}:discovery:"
    return value.startswith(prefix) and is_hash(value[len(prefix) :])


def _draft_payload(row: sqlite3.Row) -> dict[str, object]:
    """把草稿行投影为固定对象；正文只在本机调用边界内返回。"""
    return {
        "draft_id": str(row["draft_id"]),
        "profile_id": str(row["profile_id"]),
        "task_kind": str(row["task_kind"]),
        "task_key": str(row["task_key"]),
        "subject_key": str(row["subject_key"]),
        "input_hash": str(row["input_hash"]),
        "base_hash": str(row["base_hash"]),
        "pack_hash": str(row["pack_hash"]),
        "model_id": str(row["model_id"]),
        "model_config_hash": str(row["model_config_hash"]),
        "facts_hash": str(row["facts_hash"]),
        "intent": str(row["intent"]),
        "sub_intents": tuple(json.loads(str(row["sub_intents_json"]))),
        "policy_tier": str(row["policy_tier"]),
        "reply": str(row["reply"]),
        "state": str(row["state"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _action_row_payload(row: sqlite3.Row) -> dict[str, object]:
    """把动作行投影为不含正文/坐标/授权的固定结果对象。"""
    return {
        "action_key": str(row["action_key"]),
        "app": str(row["app"]),
        "action_kind": str(row["action_kind"]),
        "task_kind": (
            str(row["task_kind"]) if "task_kind" in row.keys() else "discovery"
        ),
        "task_key": str(row["task_key"]),
        "target_key": str(row["target_key"]),
        "conversation_key": str(row["conversation_key"]),
        "state": str(row["state"]),
        "outcome": None if row["outcome"] is None else str(row["outcome"]),
        "effect": None if row["effect"] is None else str(row["effect"]),
        "reason_code": None if row["reason_code"] is None else str(row["reason_code"]),
        "before_snapshot_id": (
            None if row["before_snapshot_id"] is None else str(row["before_snapshot_id"])
        ),
        "after_snapshot_id": (
            None if row["after_snapshot_id"] is None else str(row["after_snapshot_id"])
        ),
        "observed_at": None if row["observed_at"] is None else str(row["observed_at"]),
        "scene_settled": bool(row["scene_settled"]),
        "confirmation_open": bool(row["confirmation_open"]),
        "ime_restored": bool(row["ime_restored"]),
        "command_quiet": bool(row["command_quiet"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "attempt": int(row["attempt"]),
        "owner_pid": int(row["owner_pid"]),
        "owner_host": str(row["owner_host"]),
    }
