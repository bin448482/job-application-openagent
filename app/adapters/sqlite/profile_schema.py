"""创建并核验 profile 独立库；v1 事实库可备份后最小迁移到 v2 动作表。"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from app.adapters.sqlite.schema import (
    BUSY_TIMEOUT_MS,
    INDEX_SQL,
    TABLE_SQL,
    _normalize_sql,
    rollback_quietly,
)
from app.domain.models import is_nonempty_text
from app.ports.profile import ProfileError

PROFILE_SCHEMA_VERSION = 2
PROFILE_META_SCHEMA_VERSION = 1

PROFILE_TABLE_SQL = {
    "profile_meta": """CREATE TABLE profile_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    profile_id TEXT NOT NULL UNIQUE CHECK (length(profile_id) BETWEEN 1 AND 32),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at TEXT NOT NULL
) STRICT""",
    "profile_facts": """CREATE TABLE profile_facts (
    fact_key TEXT NOT NULL CHECK (fact_key IN ('work_location', 'salary_expectation', 'years_experience')),
    source_id TEXT NOT NULL,
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 200),
    state TEXT NOT NULL CHECK (state IN ('active', 'superseded', 'revoked')),
    confirmed_at TEXT NOT NULL,
    expires_at TEXT,
    superseded_by TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (fact_key, source_id),
    CHECK ((state = 'superseded') = (superseded_by IS NOT NULL)),
    CHECK (expires_at IS NULL OR expires_at > confirmed_at)
) STRICT""",
    "profile_fact_events": """CREATE TABLE profile_fact_events (
    event_id INTEGER PRIMARY KEY,
    fact_key TEXT NOT NULL CHECK (fact_key IN ('work_location', 'salary_expectation', 'years_experience')),
    action TEXT NOT NULL CHECK (action IN ('set', 'revoke', 'forget')),
    source_id TEXT,
    previous_source_ids_json TEXT NOT NULL CHECK (json_valid(previous_source_ids_json)),
    created_at TEXT NOT NULL
) STRICT""",
}

PROFILE_INDEX_SQL = {
    "idx_profile_facts_key_state": (
        'CREATE INDEX idx_profile_facts_key_state ' 'ON profile_facts(fact_key, state)'
    ),
    "idx_profile_fact_events_key": (
        'CREATE INDEX idx_profile_fact_events_key '
        'ON profile_fact_events(fact_key, event_id)'
    ),
}

ACTION_TABLE_SQL = {
    "action_records": """CREATE TABLE action_records (
    action_key TEXT PRIMARY KEY,
    app TEXT NOT NULL CHECK (app = 'boss'),
    action_kind TEXT NOT NULL CHECK (action_kind IN ('send_text', 'send_resume', 'change_wechat', 'change_phone', 'delete_conversation')),
    task_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'performed', 'not_performed', 'unknown', 'cancelled')),
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ('performed', 'not_performed', 'unknown')),
    effect TEXT CHECK (effect IS NULL OR effect IN ('sent', 'request_sent', 'deleted', 'applied', 'none')),
    reason_code TEXT,
    before_snapshot_id TEXT,
    after_snapshot_id TEXT,
    observed_at TEXT,
    scene_settled INTEGER NOT NULL DEFAULT 0 CHECK (scene_settled IN (0, 1)),
    confirmation_open INTEGER NOT NULL DEFAULT 0 CHECK (confirmation_open IN (0, 1)),
    ime_restored INTEGER NOT NULL DEFAULT 0 CHECK (ime_restored IN (0, 1)),
    command_quiet INTEGER NOT NULL DEFAULT 0 CHECK (command_quiet IN (0, 1)),
    authorization_json TEXT NOT NULL CHECK (json_valid(authorization_json)),
    lease_owner TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_host TEXT NOT NULL,
    nonce TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt = 1),
    FOREIGN KEY (app, task_key) REFERENCES hr_discoveries(app, "key") ON DELETE RESTRICT,
    CHECK (substr(action_key, 1, length(app) + 1) = app || ':'),
    CHECK (substr(task_key, 1, length(app) + 1) = app || ':'),
    CHECK (substr(conversation_key, 1, length(app) + 1) = app || ':'),
    CHECK (
        (state = 'pending' AND outcome IS NULL AND effect IS NULL
            AND (reason_code IS NULL OR reason_code IN ('device_disconnected', 'result_unverified', 'executor_interrupted')))
        OR
        (state = 'performed' AND outcome = 'performed'
            AND effect IN ('sent', 'request_sent', 'deleted', 'applied')
            AND reason_code IS NULL
            AND before_snapshot_id IS NOT NULL AND after_snapshot_id IS NOT NULL
            AND before_snapshot_id <> after_snapshot_id
            AND observed_at IS NOT NULL)
        OR
        (state = 'not_performed' AND outcome = 'not_performed' AND effect = 'none'
            AND reason_code IN ('authorization_missing', 'precondition_failed', 'cancelled_before_action')
            AND observed_at IS NOT NULL)
        OR
        (state = 'unknown' AND outcome = 'unknown' AND effect = 'none'
            AND reason_code IN ('device_disconnected', 'result_unverified', 'executor_interrupted')
            AND observed_at IS NOT NULL)
        OR
        (state = 'cancelled' AND outcome IS NULL AND effect IS NULL AND reason_code IS NULL)
    )
) STRICT""",
    "action_events": """CREATE TABLE action_events (
    event_id INTEGER PRIMARY KEY,
    action_key TEXT NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('action_pending', 'action_completed', 'action_reconciled')),
    from_state TEXT CHECK (from_state IS NULL OR from_state IN ('pending', 'performed', 'not_performed', 'unknown', 'cancelled')),
    to_state TEXT NOT NULL CHECK (to_state IN ('pending', 'performed', 'not_performed', 'unknown', 'cancelled')),
    reason_code TEXT,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
) STRICT""",
    "action_ime_state": """CREATE TABLE action_ime_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    action_key TEXT NOT NULL,
    original_ime TEXT NOT NULL,
    current_ime TEXT,
    saved_at TEXT NOT NULL,
    restored INTEGER NOT NULL CHECK (restored IN (0, 1)),
    updated_at TEXT NOT NULL
) STRICT""",
}

ACTION_INDEX_SQL = {
    "idx_action_records_task": (
        "CREATE INDEX idx_action_records_task " "ON action_records(app, task_key)"
    ),
    "idx_action_records_state": (
        "CREATE INDEX idx_action_records_state "
        "ON action_records(state, action_kind, observed_at)"
    ),
    "idx_action_events_action": (
        "CREATE INDEX idx_action_events_action "
        "ON action_events(action_key, event_id)"
    ),
}

PROFILE_V1_EXPECTED_OBJECTS = {
    **{name: ("table", sql) for name, sql in TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in INDEX_SQL.items()},
    **{name: ("table", sql) for name, sql in PROFILE_TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in PROFILE_INDEX_SQL.items()},
}

PROFILE_EXPECTED_OBJECTS = {
    **PROFILE_V1_EXPECTED_OBJECTS,
    **{name: ("table", sql) for name, sql in ACTION_TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in ACTION_INDEX_SQL.items()},
}


def profile_expected_objects(version: int) -> dict[str, tuple[str, str]]:
    """返回指定 profile 结构版本的对象集；未知版本拒绝。"""
    if version == 1:
        return PROFILE_V1_EXPECTED_OBJECTS
    if version == PROFILE_SCHEMA_VERSION:
        return PROFILE_EXPECTED_OBJECTS
    raise ProfileError("profile_mismatch")


def initialize_profile_database(
    path: Path, profile_id: str, *, now: str, backup_root: Path | None = None
) -> str | None:
    """创建 v2 或从严格 v1 备份后最小迁移；返回迁移备份相对记录或 None。"""
    if not is_nonempty_text(profile_id) or len(profile_id) > 32:
        raise ProfileError("invalid_profile")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except sqlite3.OperationalError:
        raise ProfileError("profile_io_error") from None
    except (sqlite3.Error, OSError):
        raise ProfileError("profile_io_error") from None
    try:
        return _initialize_or_migrate(connection, path, profile_id, now, backup_root)
    finally:
        connection.close()


def _initialize_or_migrate(
    connection: sqlite3.Connection,
    path: Path,
    profile_id: str,
    now: str,
    backup_root: Path | None,
) -> str | None:
    """先分类空库/v1/v2；v1 先核验身份与完整 DDL，再本机备份并单事务迁移。"""
    version, objects = _read_schema(connection)
    if not objects and version == 0:
        _create_v2(connection, profile_id, now)
        return None
    if version == PROFILE_SCHEMA_VERSION and set(objects) == set(
        PROFILE_EXPECTED_OBJECTS
    ):
        _verify_objects(connection, profile_id, PROFILE_SCHEMA_VERSION)
        return None
    if version == 1 and set(objects) == set(PROFILE_V1_EXPECTED_OBJECTS):
        # 备份前先核验完整 DDL 与 profile 身份，避免把异 profile 内容复制到
        # 目标 profile 目录，也避免为坏库或错身份库生成任何备份。
        _verify_objects(connection, profile_id, 1)
        backup = _backup_profile(connection, path, backup_root)
        _migrate_v1_to_v2(connection, profile_id)
        return backup
    raise ProfileError("profile_mismatch")


def _create_v2(
    connection: sqlite3.Connection, profile_id: str, now: str
) -> None:
    """在单事务内创建 v1 事实表和 v2 动作表并写入绑定身份。"""
    statements = _v2_statements()
    try:
        connection.execute("BEGIN IMMEDIATE")
        version, objects = _read_schema(connection)
        if objects or version != 0:
            raise ProfileError("profile_mismatch")
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO profile_meta(singleton, profile_id, schema_version, created_at) "
            "VALUES (1, ?, ?, ?)",
            (profile_id, PROFILE_META_SCHEMA_VERSION, now),
        )
        connection.execute(f"PRAGMA user_version = {PROFILE_SCHEMA_VERSION}")
        _verify_objects(connection, profile_id, PROFILE_SCHEMA_VERSION)
        connection.execute("COMMIT")
    except ProfileError:
        rollback_quietly(connection)
        raise
    except (sqlite3.Error, OSError, RuntimeError):
        rollback_quietly(connection)
        raise ProfileError("profile_io_error") from None


def _migrate_v1_to_v2(connection: sqlite3.Connection, profile_id: str) -> None:
    """在备份完成后仅追加 v2 三表与索引，并原子更新结构版本。"""
    try:
        connection.execute("BEGIN IMMEDIATE")
        version, objects = _read_schema(connection)
        if version != 1 or set(objects) != set(PROFILE_V1_EXPECTED_OBJECTS):
            raise ProfileError("profile_mismatch")
        _verify_objects(connection, profile_id, 1)
        for statement in (
            [ACTION_TABLE_SQL[name] for name in ACTION_TABLE_SQL]
            + [ACTION_INDEX_SQL[name] for name in ACTION_INDEX_SQL]
        ):
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {PROFILE_SCHEMA_VERSION}")
        _verify_objects(connection, profile_id, PROFILE_SCHEMA_VERSION)
        connection.execute("COMMIT")
    except ProfileError:
        rollback_quietly(connection)
        raise
    except (sqlite3.Error, OSError, RuntimeError):
        rollback_quietly(connection)
        raise ProfileError("profile_io_error") from None


def _v2_statements() -> list[str]:
    """返回 v2 建库的确定性语句顺序，事实表在前、动作表在后。"""
    return (
        [TABLE_SQL[name] for name in TABLE_SQL]
        + [INDEX_SQL[name] for name in INDEX_SQL]
        + [PROFILE_TABLE_SQL[name] for name in PROFILE_TABLE_SQL]
        + [PROFILE_INDEX_SQL[name] for name in PROFILE_INDEX_SQL]
        + [ACTION_TABLE_SQL[name] for name in ACTION_TABLE_SQL]
        + [ACTION_INDEX_SQL[name] for name in ACTION_INDEX_SQL]
    )


def _backup_profile(
    connection: sqlite3.Connection, path: Path, backup_root: Path | None
) -> str:
    """用标准库 SQLite backup API 写不覆盖旧文件的本机备份，返回相对路径。"""
    root = Path(backup_root) if backup_root is not None else path.parent
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ProfileError("profile_io_error") from None
    target = root / f"{path.name}.v1-backup-{uuid.uuid4().hex}.sqlite3"
    if target.exists():
        raise ProfileError("profile_io_error")
    try:
        destination = sqlite3.connect(str(target))
        try:
            connection.backup(destination)
        finally:
            destination.close()
    except (sqlite3.Error, OSError):
        try:
            target.unlink()
        except OSError:
            pass
        raise ProfileError("profile_io_error") from None
    try:
        return str(target.relative_to(path.parent))
    except ValueError:
        return target.name


def connect_profile_database(
    path: Path, profile_id: str, *, require_actions: bool = False
) -> sqlite3.Connection:
    """打开现有 profile 库并核验 v1/v2 结构；动作写入要求 v2。"""
    if not path.is_file():
        raise ProfileError("profile_io_error")
    try:
        connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except sqlite3.OperationalError:
        raise ProfileError("profile_io_error") from None
    except (sqlite3.Error, OSError):
        raise ProfileError("profile_io_error") from None
    try:
        version = _verify_any(connection, profile_id)
        if require_actions and version != PROFILE_SCHEMA_VERSION:
            raise ProfileError("profile_mismatch")
    except Exception:
        connection.close()
        raise
    return connection


def verify_profile_schema(connection: sqlite3.Connection, profile_id: str) -> int:
    """逐项比较 v1/v2 版本、对象集合、规范化 SQL 与唯一 profile 身份。"""
    return _verify_any(connection, profile_id)


def _verify_any(connection: sqlite3.Connection, profile_id: str) -> int:
    """核验 v1 或 v2 并返回版本号；其它结构固定 profile_mismatch。"""
    version, objects = _read_schema(connection)
    if version == 1 and set(objects) == set(PROFILE_V1_EXPECTED_OBJECTS):
        _verify_objects(connection, profile_id, 1)
        return 1
    if version == PROFILE_SCHEMA_VERSION and set(objects) == set(
        PROFILE_EXPECTED_OBJECTS
    ):
        _verify_objects(connection, profile_id, PROFILE_SCHEMA_VERSION)
        return PROFILE_SCHEMA_VERSION
    raise ProfileError("profile_mismatch")


def _verify_objects(
    connection: sqlite3.Connection, profile_id: str, version: int
) -> None:
    """比较对象 DDL 后核验 profile_meta 绑定身份。"""
    expected = profile_expected_objects(version)
    actual_version, objects = _read_schema(connection)
    if actual_version != version or set(objects) != set(expected):
        raise ProfileError("profile_mismatch")
    for name, (expected_type, expected_sql) in expected.items():
        actual_type, actual_sql = objects[name]
        if actual_type != expected_type or _normalize_sql(actual_sql) != _normalize_sql(
            expected_sql
        ):
            raise ProfileError("profile_mismatch")
    _verify_identity(connection, profile_id)


def _verify_identity(connection: sqlite3.Connection, profile_id: str) -> None:
    """校验唯一 profile_meta 行绑定请求身份与元数据版本。"""
    try:
        row = connection.execute(
            "SELECT profile_id, schema_version FROM profile_meta WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error:
        raise ProfileError("profile_mismatch") from None
    if row is None or row["profile_id"] != profile_id:
        raise ProfileError("profile_mismatch")
    if int(row["schema_version"]) != PROFILE_META_SCHEMA_VERSION:
        raise ProfileError("profile_mismatch")


def _read_schema(
    connection: sqlite3.Connection,
) -> tuple[int, dict[str, tuple[str, str]]]:
    """读取版本与完整用户对象集；读取失败固定 profile IO 错误。"""
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        objects = {
            str(row["name"]): (str(row["type"]), str(row["sql"]))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    except sqlite3.OperationalError:
        raise ProfileError("profile_io_error") from None
    except (sqlite3.Error, TypeError, ValueError):
        raise ProfileError("profile_io_error") from None
    return version, objects


def _user_object_names(connection: sqlite3.Connection) -> list[str]:
    """列出用户表/索引/视图/触发器名称，内部 sqlite_ 对象不计入。"""
    return [
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    ]
