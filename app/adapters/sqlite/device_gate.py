"""实现固定 OS 位置共享 SQLite 设备门禁：全会话唯一 primary、先占后用、异常隔离。"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from app.adapters.sqlite.schema import BUSY_TIMEOUT_MS, _normalize_sql
from app.domain.models import Owner, is_hash, is_uuid, parse_utc
from app.domain.policy import is_profile_id
from app.ports.device_gate import DeviceGateError

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
        process_log: object | None = None,
    ) -> None:
        """保存 profile 绑定与固定路径选择；构造阶段不创建库或占用。"""
        if not is_profile_id(profile_id):
            raise DeviceGateError("device_gate_mismatch")
        if not isinstance(profile_db_path, Path) or not profile_db_path.is_absolute():
            raise ValueError("profile db path must be absolute")
        self._profile_id = profile_id
        self._profile_db_path = str(profile_db_path.resolve())
        self._path = guard_database_path(os_root, platform_name=platform_name)
        self._process_log = process_log
        self._nonce: str | None = None

    def acquire(
        self, *, profile_id: str, profile_db_path: str, owner: Owner, now: str
    ) -> str:
        """在共享库条件插入唯一 primary 行；存在任何状态即拒绝。"""
        if profile_id != self._profile_id:
            raise DeviceGateError("device_gate_mismatch")
        if Path(profile_db_path).resolve() != Path(self._profile_db_path):
            raise DeviceGateError("device_gate_mismatch")
        if not is_uuid(owner.id) or not is_hash(owner.host) or type(owner.pid) is not int:
            raise ValueError("owner is invalid")
        if owner.pid <= 0:
            raise ValueError("owner is invalid")
        parse_utc(now)
        if self._nonce is not None:
            raise DeviceGateError("device_busy")
        nonce = str(uuid.uuid4())
        connection = self._connect(create=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._read_row(connection)
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
        except (sqlite3.Error, OSError):
            _rollback(connection)
            raise DeviceGateError("device_gate_io_error") from None
        finally:
            connection.close()
        self._nonce = nonce
        return nonce

    def require_available(self) -> None:
        """要求本对象已取得占用并通过一次完整复核。"""
        self.verify()

    def verify(self, *, now: str | None = None) -> None:
        """复核 held 状态、owner nonce 与 profile 库绑定；不自动续期或解锁。"""
        del now
        if self._nonce is None:
            raise DeviceGateError("device_not_held")
        connection = self._connect(create=False)
        try:
            row = self._read_row(connection)
        finally:
            connection.close()
        if row is None:
            raise DeviceGateError("device_gate_mismatch")
        if row["state"] == "quarantined":
            raise DeviceGateError("device_quarantined")
        if (
            row["nonce"] != self._nonce
            or row["profile_id"] != self._profile_id
            or Path(str(row["profile_db_path"])).resolve()
            != Path(self._profile_db_path)
            or row["state"] != "held"
        ):
            raise DeviceGateError("device_gate_mismatch")

    def release(self, *, now: str, converged: bool) -> None:
        """本次 owner/nonce 且已收敛才删除；否则保留原行并置 quarantined。"""
        parse_utc(now)
        if self._nonce is None:
            raise DeviceGateError("device_not_held")
        connection = self._connect(create=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read_row(connection)
            if row is None or row["nonce"] != self._nonce:
                raise DeviceGateError("device_gate_mismatch")
            if row["state"] == "quarantined":
                raise DeviceGateError("device_quarantined")
            if converged:
                connection.execute(
                    "DELETE FROM device_guard WHERE device_key = 'primary' AND nonce = ?",
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
        except (sqlite3.Error, OSError):
            _rollback(connection)
            raise DeviceGateError("device_gate_io_error") from None
        finally:
            connection.close()
        self._nonce = None

    def inspect(self) -> dict[str, object] | None:
        """只读返回固定白名单占用行；无行返回 None，不创建库或自动释放。"""
        if not self._path.is_file():
            return None
        connection = self._connect(create=False)
        try:
            row = self._read_row(connection)
        finally:
            connection.close()
        if row is None:
            return None
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

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        """打开固定门禁库；首次创建允许少量本地文件/SQLite 竞争重试。"""
        attempts = 3 if create else 1
        last: DeviceGateError | None = None
        for attempt in range(attempts):
            try:
                return self._connect_once(create=create)
            except DeviceGateError as error:
                last = error
                if (
                    error.reason_code != "device_gate_io_error"
                    or attempt + 1 >= attempts
                ):
                    raise
                time.sleep(0.02)
        assert last is not None
        raise last

    def _connect_once(self, *, create: bool) -> sqlite3.Connection:
        """实际执行一次打开与建表/核验，失败关闭连接并映射固定错误。"""
        if not create and not self._path.is_file():
            raise DeviceGateError("device_gate_io_error")
        try:
            if create:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self._path), timeout=BUSY_TIMEOUT_MS / 1000)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except sqlite3.OperationalError:
            raise DeviceGateError("device_gate_io_error") from None
        except (sqlite3.Error, OSError):
            raise DeviceGateError("device_gate_io_error") from None
        try:
            self._ensure_schema(connection, create=create)
        except Exception:
            connection.close()
            raise
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection, *, create: bool) -> None:
        """创建或核验唯一门禁表；已有其它结构时拒绝复用。"""
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            row = connection.execute(
                "SELECT type, sql FROM sqlite_schema WHERE name = 'device_guard'"
            ).fetchone()
        except sqlite3.Error:
            raise DeviceGateError("device_gate_io_error") from None
        if row is None:
            if not create:
                raise DeviceGateError("device_gate_io_error")
            try:
                connection.execute("BEGIN IMMEDIATE")
                # 取得写锁后重新检查，避免两个首次启动进程同时建表。
                raced = connection.execute(
                    "SELECT type, sql FROM sqlite_schema WHERE name = 'device_guard'"
                ).fetchone()
                if raced is None:
                    connection.execute(GATE_TABLE_SQL)
                    connection.execute(f"PRAGMA user_version = {GATE_SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except sqlite3.Error:
                _rollback(connection)
                raise DeviceGateError("device_gate_io_error") from None
            if raced is None:
                return
            row = raced
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if (
            str(row["type"]) != "table"
            or _normalize_sql(str(row["sql"])) != _normalize_sql(GATE_TABLE_SQL)
            or version != GATE_SCHEMA_VERSION
        ):
            raise DeviceGateError("device_gate_io_error")

    def _read_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
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
