"""实现固定 OS 位置共享 SQLite 设备门禁：全会话唯一 primary、先占后用、异常隔离与只读占用查询。"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from app.adapters.path_state import (
    PATH_FILE,
    PATH_MISSING,
    PATH_UNKNOWN,
    probe_path,
)
from app.adapters.sqlite.schema import BUSY_TIMEOUT_MS, _normalize_sql
from app.domain.models import Owner, is_hash, is_uuid, parse_utc
from app.domain.policy import is_profile_id
from app.ports.device_gate import DeviceGateError, DeviceSessionIdentity
from app.process_log import ProcessLog, elapsed_ms

GATE_SCHEMA_VERSION = 1
GATE_APP_DIR = "job-application-openagent"
GATE_DB_NAME = "device-guard.sqlite3"
GATE_TABLE_SQL = """CREATE TABLE device_guard (
    device_key TEXT PRIMARY KEY CHECK (device_key = 'primary'),
    profile_id TEXT NOT NULL,
    profile_db_path TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_host TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('held', 'quarantined')),
    acquired_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT"""


class _GateBusy(Exception):
    """内部标记门禁库写锁竞争；只按调用路径映射，不暴露原始异常。"""


def _is_busy(error: BaseException) -> bool:
    """判断 SQLite 错误是否为忙/锁定，含扩展错误码，不读取异常原文。"""
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        for name in ("SQLITE_BUSY", "SQLITE_LOCKED"):
            base = getattr(sqlite3, name, None)
            if isinstance(base, int) and code & 0xFF == base & 0xFF:
                return True
    text = str(error).lower()
    return "locked" in text or "busy" in text



def guard_database_path(os_root: Path | None = None, *, platform_name: str | None = None) -> Path:
    """返回固定应用数据位置的门禁库路径；非 Windows 或缺失根时拒绝真实 run。"""
    if os_root is not None:
        root = Path(os_root)
    else:
        platform = platform_name if platform_name is not None else sys.platform
        if platform != "win32":
            raise DeviceGateError("device_gate_unavailable")
        value = os.environ.get("LOCALAPPDATA")
        if not value or not value.strip():
            raise DeviceGateError("device_gate_unavailable")
        root = Path(value)
    return root / GATE_APP_DIR / GATE_DB_NAME


class SharedDeviceGate:
    """持有本会话唯一 primary 占用；不做 TTL、PID 或重启自动释放。"""

    def __init__(
        self,
        *,
        profile_id: str,
        profile_db_path: Path,
        os_root: Path | None = None,
        platform_name: str | None = None,
        process_log: ProcessLog | None = None,
    ) -> None:
        """保存 profile 绑定、日志归属与固定路径；构造阶段不创建库或占用。"""
        if not is_profile_id(profile_id):
            raise DeviceGateError("device_gate_mismatch")
        if not isinstance(profile_db_path, Path) or not profile_db_path.is_absolute():
            raise ValueError("profile db path must be absolute")
        self._profile_id = profile_id
        self._profile_db_path = str(profile_db_path.resolve())
        self._path = guard_database_path(os_root, platform_name=platform_name)
        self._process_log = process_log
        self._nonce: str | None = None
        self._owner: Owner | None = None

    def acquire(
        self, *, profile_id: str, profile_db_path: str, owner: Owner, now: str
    ) -> str:
        """在共享库条件插入唯一 primary 行；存在任何状态即拒绝。"""
        operation = "acquire"
        started = time.perf_counter()
        self._record(operation, "started")
        try:
            if profile_id != self._profile_id:
                raise DeviceGateError("device_gate_mismatch")
            if Path(profile_db_path).resolve() != Path(self._profile_db_path):
                raise DeviceGateError("device_gate_mismatch")
            if (
                not is_uuid(owner.id)
                or not is_hash(owner.host)
                or type(owner.pid) is not int
                or owner.pid <= 0
            ):
                raise ValueError("owner is invalid")
            parse_utc(now)
            if self._nonce is not None:
                raise DeviceGateError("device_busy")
            nonce = str(uuid.uuid4())
            connection = self._connect(create=True, busy_timeout_ms=0)
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = _read_gate_row(connection)
                if existing is not None:
                    state = str(existing["state"])
                    connection.execute("ROLLBACK")
                    raise DeviceGateError(
                        "device_quarantined" if state == "quarantined" else "device_busy"
                    )
                connection.execute(
                    "INSERT INTO device_guard(device_key, profile_id, profile_db_path, "
                    "owner_id, owner_pid, owner_host, nonce, state, acquired_at, updated_at) "
                    "VALUES ('primary', ?, ?, ?, ?, ?, ?, 'held', ?, ?)",
                    (
                        self._profile_id,
                        self._profile_db_path,
                        owner.id,
                        owner.pid,
                        owner.host,
                        nonce,
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except DeviceGateError:
                raise
            except sqlite3.Error as error:
                _rollback(connection)
                if _is_busy(error):
                    raise DeviceGateError("device_busy") from None
                raise DeviceGateError("device_gate_io_error") from None
            except OSError:
                _rollback(connection)
                raise DeviceGateError("device_gate_io_error") from None
            finally:
                connection.close()
            self._nonce = nonce
            self._owner = owner
        except _GateBusy:
            self._record(operation, "failed", started, "device_busy")
            raise DeviceGateError("device_busy") from None
        except DeviceGateError as error:
            self._record(operation, "failed", started, error.reason_code)
            raise
        except Exception:
            self._record(operation, "failed", started)
            raise
        self._record(operation, "ok", started)
        return nonce

    def require_available(self) -> None:
        """要求本对象已取得占用并通过一次完整复核。"""
        self.verify()

    def verify(self, *, now: str | None = None) -> None:
        """复核完整 owner/nonce、profile 绑定与 held 状态；不自动续期或解锁。"""
        del now
        operation = "verify"
        started = time.perf_counter()
        self._record(operation, "started")
        try:
            self._read_validated_row()
        except DeviceGateError as error:
            self._record(operation, "failed", started, error.reason_code)
            raise
        except Exception:
            self._record(operation, "failed", started)
            raise
        self._record(operation, "ok", started)

    def release(self, *, now: str, converged: bool) -> None:
        """完整身份/绑定且已收敛才删除；否则保留原行并置 quarantined。"""
        operation = "release" if converged else "quarantine"
        started = time.perf_counter()
        self._record(operation, "started")
        try:
            parse_utc(now)
            if self._nonce is None or self._owner is None:
                raise DeviceGateError("device_not_held")
            connection = self._connect(create=False)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = _read_gate_row(connection)
                if row is None:
                    raise DeviceGateError("device_gate_mismatch")
                if row["state"] == "quarantined":
                    raise DeviceGateError("device_quarantined")
                if not self._matches_session(row):
                    raise DeviceGateError("device_gate_mismatch")
                if converged:
                    connection.execute(
                        "DELETE FROM device_guard "
                        "WHERE device_key = 'primary' AND nonce = ?",
                        (self._nonce,),
                    )
                else:
                    connection.execute(
                        "UPDATE device_guard SET state = 'quarantined', updated_at = ? "
                        "WHERE device_key = 'primary' AND nonce = ?",
                        (now, self._nonce),
                    )
                connection.execute("COMMIT")
            except DeviceGateError:
                _rollback(connection)
                raise
            except sqlite3.Error as error:
                _rollback(connection)
                if _is_busy(error):
                    raise DeviceGateError("device_busy") from None
                raise DeviceGateError("device_gate_io_error") from None
            except OSError:
                _rollback(connection)
                raise DeviceGateError("device_gate_io_error") from None
            finally:
                connection.close()
            self._nonce = None
            self._owner = None
        except DeviceGateError as error:
            self._record(operation, "failed", started, error.reason_code)
            raise
        except Exception:
            self._record(operation, "failed", started)
            raise
        self._record(operation, "ok", started)

    def recover(
        self,
        *,
        profile_id: str,
        profile_db_path: str,
        owner: Owner,
        confirm_stopped: bool,
        stop_checker: Callable[[int], str],
        now: str,
    ) -> str:
        """受控接管原 profile 的 held/quarantined 占用；需明确确认且原进程已停止。"""
        operation = "recover"
        started = time.perf_counter()
        self._record(operation, "started")
        try:
            if profile_id != self._profile_id:
                raise DeviceGateError("device_gate_mismatch")
            if Path(profile_db_path).resolve() != Path(self._profile_db_path):
                raise DeviceGateError("device_gate_mismatch")
            if (
                not is_uuid(owner.id)
                or not is_hash(owner.host)
                or type(owner.pid) is not int
                or owner.pid <= 0
            ):
                raise ValueError("owner is invalid")
            parse_utc(now)
            if confirm_stopped is not True:
                raise DeviceGateError("device_not_held")
            if self._nonce is not None:
                raise DeviceGateError("device_busy")
            connection = self._connect(create=False)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = _read_gate_row(connection)
                if row is None:
                    raise DeviceGateError("device_not_held")
                if (
                    row["profile_id"] != self._profile_id
                    or Path(str(row["profile_db_path"])).resolve()
                    != Path(self._profile_db_path)
                    or row["state"] not in {"held", "quarantined"}
                    or row["owner_host"] != owner.host
                ):
                    raise DeviceGateError("device_gate_mismatch")
                try:
                    pid = int(row["owner_pid"])
                except (TypeError, ValueError):
                    raise DeviceGateError("device_gate_io_error") from None
                if stop_checker(pid) != "stopped":
                    raise DeviceGateError("device_busy")
                nonce = str(uuid.uuid4())
                cursor = connection.execute(
                    "UPDATE device_guard SET owner_id = ?, owner_pid = ?, "
                    "owner_host = ?, nonce = ?, state = 'held', acquired_at = ?, "
                    "updated_at = ? WHERE device_key = 'primary' AND nonce = ?",
                    (
                        owner.id,
                        owner.pid,
                        owner.host,
                        nonce,
                        now,
                        now,
                        str(row["nonce"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise DeviceGateError("device_gate_mismatch")
                connection.execute("COMMIT")
            except DeviceGateError:
                _rollback(connection)
                raise
            except sqlite3.Error as error:
                _rollback(connection)
                if _is_busy(error):
                    raise DeviceGateError("device_busy") from None
                raise DeviceGateError("device_gate_io_error") from None
            except OSError:
                _rollback(connection)
                raise DeviceGateError("device_gate_io_error") from None
            finally:
                connection.close()
            self._nonce = nonce
            self._owner = owner
        except _GateBusy:
            self._record(operation, "failed", started, "device_busy")
            raise DeviceGateError("device_busy") from None
        except DeviceGateError as error:
            self._record(operation, "failed", started, error.reason_code)
            raise
        except Exception:
            self._record(operation, "failed", started)
            raise
        self._record(operation, "ok", started)
        return self._nonce

    def session_identity(self) -> DeviceSessionIdentity:
        """一次读取并完整核验同一行后返回 profile/原库/owner/nonce 身份。"""
        operation = "verify"
        started = time.perf_counter()
        self._record(operation, "started")
        try:
            row = self._read_validated_row()
            identity = DeviceSessionIdentity(
                profile_id=str(row["profile_id"]),
                profile_db_path=str(row["profile_db_path"]),
                owner=Owner(
                    id=str(row["owner_id"]),
                    pid=int(row["owner_pid"]),
                    host=str(row["owner_host"]),
                ),
                nonce=str(row["nonce"]),
                state=str(row["state"]),
            )
        except DeviceGateError as error:
            self._record(operation, "failed", started, error.reason_code)
            raise
        except Exception:
            self._record(operation, "failed", started)
            raise
        self._record(operation, "ok", started)
        return identity

    def _read_validated_row(self) -> sqlite3.Row:
        """读取一行并对同一行执行完整取得时身份/held 核验。"""
        if self._nonce is None or self._owner is None:
            raise DeviceGateError("device_not_held")
        connection = self._connect(create=False)
        try:
            row = _read_gate_row(connection)
        finally:
            connection.close()
        if row is None:
            raise DeviceGateError("device_gate_mismatch")
        if row["state"] == "quarantined":
            raise DeviceGateError("device_quarantined")
        if not self._matches_session(row):
            raise DeviceGateError("device_gate_mismatch")
        return row

    def inspect(self) -> dict[str, object] | None:
        """只读返回固定白名单占用行；明确不存在返回 None，其它异常状态拒绝。"""
        return _inspect_path(self._path)

    def write_guard(self, *, create: bool = True) -> "GateWriteGuard":
        """返回覆盖后续 profile 写的固定门禁排他事务守卫。"""
        return GateWriteGuard(self, create=create)

    def _matches_session(self, row: sqlite3.Row) -> bool:
        """比较取得时完整 owner 身份、nonce、profile 绑定与 held 状态。"""
        if self._nonce is None or self._owner is None:
            return False
        try:
            owner_pid = int(row["owner_pid"])
        except (TypeError, ValueError):
            return False
        return (
            row["nonce"] == self._nonce
            and row["owner_id"] == self._owner.id
            and owner_pid == self._owner.pid
            and row["owner_host"] == self._owner.host
            and row["profile_id"] == self._profile_id
            and Path(str(row["profile_db_path"])).resolve()
            == Path(self._profile_db_path)
            and row["state"] == "held"
        )

    def _record(
        self,
        operation: str,
        result: str,
        started: float | None = None,
        reason_code: str | None = None,
    ) -> None:
        """由门禁适配器唯一记录 acquire/verify/release/quarantine/facts_guard 日志。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage="device_gate",
                operation=operation,
                result=result,
                reason_code=reason_code,
                error_category="gate" if result == "failed" else None,
                duration_ms=elapsed_ms(started) if started is not None else 0,
            )
        except Exception:
            return

    def _connect(
        self, *, create: bool, busy_timeout_ms: int = BUSY_TIMEOUT_MS
    ) -> sqlite3.Connection:
        """打开固定门禁库；委托模块级连接函数并保留实例路径。"""
        return _connect_gate(
            self._path, create=create, busy_timeout_ms=busy_timeout_ms
        )


class GateWriteGuard:
    """在固定门禁库持有排他写事务，覆盖同 profile 校验与后续 profile 写。"""

    _OPERATION = "facts_guard"

    def __init__(self, gate: SharedDeviceGate, *, create: bool) -> None:
        """保存门禁实例与是否允许首次初始化空门禁库。"""
        self._gate = gate
        self._create = create
        self._connection: sqlite3.Connection | None = None
        self._started: float | None = None

    def __enter__(self) -> "GateWriteGuard":
        """取得门禁 `BEGIN IMMEDIATE` 并校验同 profile 占用；不插入或改写行。"""
        started = time.perf_counter()
        self._started = started
        self._gate._record(self._OPERATION, "started")
        try:
            connection = self._gate._connect(create=self._create)
            self._connection = connection
            connection.execute("BEGIN IMMEDIATE")
            row = _read_gate_row(connection)
            if row is not None:
                state = str(row["state"])
                if state not in {"held", "quarantined"}:
                    raise DeviceGateError("device_gate_io_error")
                if row["profile_id"] == self._gate._profile_id:
                    if state == "held":
                        raise DeviceGateError("device_busy")
                    raise DeviceGateError("device_quarantined")
        except _GateBusy:
            self._rollback_and_close()
            self._gate._record(self._OPERATION, "failed", started, "device_busy")
            raise DeviceGateError("device_busy") from None
        except DeviceGateError as error:
            self._rollback_and_close()
            self._gate._record(self._OPERATION, "failed", started, error.reason_code)
            raise
        except sqlite3.Error as error:
            self._rollback_and_close()
            reason = "device_busy" if _is_busy(error) else "device_gate_io_error"
            self._gate._record(self._OPERATION, "failed", started, reason)
            raise DeviceGateError(reason) from None
        except OSError:
            self._rollback_and_close()
            self._gate._record(
                self._OPERATION, "failed", started, "device_gate_io_error"
            )
            raise DeviceGateError("device_gate_io_error") from None
        except Exception:
            self._rollback_and_close()
            self._gate._record(self._OPERATION, "failed", started)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        """结束门禁写事务并关闭连接；body 异常不吞掉，仍回滚释放锁。

        事务本身无论成功或异常都回滚（守卫不写占用行）；日志终态反映被守卫的
        写区间是否无异常完成，body 失败不会记成完整成功。
        """
        del exc, traceback
        self._rollback_and_close()
        if exc_type is not None:
            self._gate._record(self._OPERATION, "failed", self._started)
        else:
            self._gate._record(self._OPERATION, "ok", self._started)
        return False

    def _rollback_and_close(self) -> None:
        """尽力回滚并关闭本地连接；进程死亡时由 SQLite 释放事务锁。"""
        if self._connection is not None:
            _rollback(self._connection)
            self._connection.close()
            self._connection = None


def inspect_gate_occupancy(
    *, os_root: Path | None = None, platform_name: str | None = None
) -> dict[str, object] | None:
    """只读返回固定门禁库唯一 primary 行；不创建、不改写、不释放。"""
    return _inspect_path(guard_database_path(os_root, platform_name=platform_name))


def _inspect_path(path: Path) -> dict[str, object] | None:
    """按固定门禁路径只读读取占用行；明确缺失返回 None，其余异常拒绝。"""
    state = probe_path(path)
    if state == PATH_MISSING:
        return None
    if state != PATH_FILE:
        raise DeviceGateError("device_gate_io_error")
    connection = _connect_gate(path, create=False)
    try:
        row = _read_gate_row(connection)
    finally:
        connection.close()
    if row is None:
        return None
    return _gate_row_payload(row)


def _gate_row_payload(row: sqlite3.Row) -> dict[str, object]:
    """把唯一 primary 行投影为固定白名单字段，不追加其它列。"""
    return {
        "profile_id": str(row["profile_id"]),
        "profile_db_path": str(row["profile_db_path"]),
        "owner_id": str(row["owner_id"]),
        "owner_pid": int(row["owner_pid"]),
        "owner_host": str(row["owner_host"]),
        "nonce": str(row["nonce"]),
        "state": str(row["state"]),
        "acquired_at": str(row["acquired_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _connect_gate(
    path: Path, *, create: bool, busy_timeout_ms: int = BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """打开固定门禁库；首次创建允许少量文件/SQLite IO 重试，忙不重试。"""
    attempts = 3 if create else 1
    last: DeviceGateError | None = None
    for attempt in range(attempts):
        try:
            return _connect_gate_once(
                path, create=create, busy_timeout_ms=busy_timeout_ms
            )
        except DeviceGateError as error:
            last = error
            if error.reason_code != "device_gate_io_error" or attempt + 1 >= attempts:
                raise
            time.sleep(0.02)
    assert last is not None
    raise last


def _connect_gate_once(
    path: Path, *, create: bool, busy_timeout_ms: int = BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """实际执行一次打开与建表/核验，失败关闭连接并映射固定错误。"""
    state = probe_path(path)
    if state == PATH_UNKNOWN:
        raise DeviceGateError("device_gate_io_error")
    if not create and state != PATH_FILE:
        raise DeviceGateError("device_gate_io_error")
    if create and state not in (PATH_MISSING, PATH_FILE):
        raise DeviceGateError("device_gate_io_error")
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    except sqlite3.Error as error:
        if _is_busy(error):
            raise _GateBusy() from None
        raise DeviceGateError("device_gate_io_error") from None
    except OSError:
        raise DeviceGateError("device_gate_io_error") from None
    try:
        _ensure_gate_schema(connection, create=create)
    except Exception:
        connection.close()
        raise
    return connection


def _ensure_gate_schema(connection: sqlite3.Connection, *, create: bool) -> None:
    """只允许明确新空库初始化；已有库核验完整对象集与 DDL，不改写版本。"""
    version, objects = _read_gate_schema(connection)
    if objects:
        _verify_gate_schema(version, objects)
        return
    if version != 0:
        raise DeviceGateError("device_gate_io_error")
    if not create:
        raise DeviceGateError("device_gate_io_error")
    try:
        connection.execute("BEGIN IMMEDIATE")
        version, objects = _read_gate_schema(connection)
        if objects:
            _verify_gate_schema(version, objects)
        else:
            if version != 0:
                raise DeviceGateError("device_gate_io_error")
            connection.execute(GATE_TABLE_SQL)
            connection.execute(f"PRAGMA user_version = {GATE_SCHEMA_VERSION}")
            version, objects = _read_gate_schema(connection)
            _verify_gate_schema(version, objects)
        connection.execute("COMMIT")
    except DeviceGateError:
        _rollback(connection)
        raise
    except sqlite3.Error as error:
        _rollback(connection)
        if _is_busy(error):
            raise _GateBusy() from None
        raise DeviceGateError("device_gate_io_error") from None
    except OSError:
        _rollback(connection)
        raise DeviceGateError("device_gate_io_error") from None


def _read_gate_schema(
    connection: sqlite3.Connection,
) -> tuple[int, dict[str, tuple[str, str]]]:
    """读取版本与完整用户对象集；忙单独标记，其它读取失败为门禁 IO 错误。"""
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        objects = {
            str(row["name"]): (str(row["type"]), str(row["sql"]))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    except sqlite3.Error as error:
        if _is_busy(error):
            raise _GateBusy() from None
        raise DeviceGateError("device_gate_io_error") from None
    except (TypeError, ValueError):
        raise DeviceGateError("device_gate_io_error") from None
    return version, objects


def _verify_gate_schema(version: int, objects: dict[str, tuple[str, str]]) -> None:
    """拒绝未知版本、额外/缺失对象或非本门禁 DDL。"""
    if version != GATE_SCHEMA_VERSION or set(objects) != {"device_guard"}:
        raise DeviceGateError("device_gate_io_error")
    object_type, sql = objects["device_guard"]
    if (
        object_type != "table"
        or _normalize_sql(sql) != _normalize_sql(GATE_TABLE_SQL)
    ):
        raise DeviceGateError("device_gate_io_error")


def _read_gate_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    """读取唯一 primary 行；读取失败固定为门禁 IO 错误。"""
    try:
        return connection.execute(
            "SELECT * FROM device_guard WHERE device_key = 'primary'"
        ).fetchone()
    except sqlite3.Error:
        raise DeviceGateError("device_gate_io_error") from None


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚门禁事务，不覆盖原始安全错误。"""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        return
