"""创建并核验 profile 独立库：复用 M1 v1 五表并追加最小 profile 元数据与事实表。"""

from __future__ import annotations

import sqlite3
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

PROFILE_SCHEMA_VERSION = 1

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

PROFILE_EXPECTED_OBJECTS = {
    **{name: ("table", sql) for name, sql in TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in INDEX_SQL.items()},
    **{name: ("table", sql) for name, sql in PROFILE_TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in PROFILE_INDEX_SQL.items()},
}


def initialize_profile_database(path: Path, profile_id: str, *, now: str) -> None:
    """在单个写事务内创建或核验 profile 库；绝不导入旧合成库或覆盖已有行。"""
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
        _initialize_in_transaction(connection, profile_id, now)
    finally:
        connection.close()


def _initialize_in_transaction(
    connection: sqlite3.Connection, profile_id: str, now: str
) -> None:
    """在 BEGIN IMMEDIATE 内建表或核验绑定身份，任何失败整体回滚。"""
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        names = _user_object_names(connection)
    except sqlite3.OperationalError:
        rollback_quietly(connection)
        raise ProfileError("profile_io_error") from None
    except (sqlite3.Error, TypeError):
        rollback_quietly(connection)
        raise ProfileError("profile_io_error") from None

    try:
        if names or version != 0:
            verify_profile_schema(connection, profile_id)
            connection.execute("COMMIT")
            return
        statements = [TABLE_SQL[name] for name in TABLE_SQL] + [
            INDEX_SQL[name] for name in INDEX_SQL
        ] + [PROFILE_TABLE_SQL[name] for name in PROFILE_TABLE_SQL] + [
            PROFILE_INDEX_SQL[name] for name in PROFILE_INDEX_SQL
        ]
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO profile_meta(singleton, profile_id, schema_version, created_at) "
            "VALUES (1, ?, ?, ?)",
            (profile_id, PROFILE_SCHEMA_VERSION, now),
        )
        connection.execute(f"PRAGMA user_version = {PROFILE_SCHEMA_VERSION}")
        verify_profile_schema(connection, profile_id)
        connection.execute("COMMIT")
    except ProfileError:
        rollback_quietly(connection)
        raise
    except (sqlite3.Error, OSError, RuntimeError):
        rollback_quietly(connection)
        raise ProfileError("profile_io_error") from None


def connect_profile_database(path: Path, profile_id: str) -> sqlite3.Connection:
    """打开现有 profile 库并核验完整结构、版本与绑定 ID。"""
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
        verify_profile_schema(connection, profile_id)
    except Exception:
        connection.close()
        raise
    return connection


def verify_profile_schema(connection: sqlite3.Connection, profile_id: str) -> None:
    """逐项比较版本、对象集合、规范化 SQL 与唯一 profile 身份。"""
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
    except (sqlite3.Error, TypeError):
        raise ProfileError("profile_io_error") from None
    if version != PROFILE_SCHEMA_VERSION or set(objects) != set(PROFILE_EXPECTED_OBJECTS):
        raise ProfileError("profile_mismatch")
    for name, (expected_type, expected_sql) in PROFILE_EXPECTED_OBJECTS.items():
        actual_type, actual_sql = objects[name]
        if actual_type != expected_type or _normalize_sql(actual_sql) != _normalize_sql(
            expected_sql
        ):
            raise ProfileError("profile_mismatch")
    try:
        row = connection.execute(
            "SELECT profile_id, schema_version FROM profile_meta WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error:
        raise ProfileError("profile_mismatch") from None
    if row is None or row["profile_id"] != profile_id:
        raise ProfileError("profile_mismatch")
    if int(row["schema_version"]) != PROFILE_SCHEMA_VERSION:
        raise ProfileError("profile_mismatch")


def _user_object_names(connection: sqlite3.Connection) -> list[str]:
    """列出用户表/索引/视图/触发器名称，内部 sqlite_ 对象不计入。"""
    return [
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    ]
