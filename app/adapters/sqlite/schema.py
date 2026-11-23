"""创建并核验 M1 空库 v1 的五张 STRICT 表、约束与索引，失败时整体回滚。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from app.ports.store import StoreError

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

TABLE_SQL = {
    "hr_conversations": """CREATE TABLE hr_conversations (
    app TEXT NOT NULL CHECK (app = 'boss'),
    "key" TEXT NOT NULL,
    identity_hash TEXT NOT NULL CHECK (length(identity_hash) = 64),
    baseline_preview_hash TEXT,
    baseline_time_hint TEXT,
    verified_at TEXT,
    PRIMARY KEY (app, "key"),
    CHECK (substr("key", 1, length(app) + 1) = app || ':'),
    CHECK ((baseline_preview_hash IS NULL) = (verified_at IS NULL)),
    CHECK (verified_at IS NOT NULL OR baseline_time_hint IS NULL)
) STRICT""",
    "hr_discoveries": """CREATE TABLE hr_discoveries (
    app TEXT NOT NULL CHECK (app = 'boss'),
    "key" TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('unread', 'new_message')),
    fingerprint TEXT NOT NULL,
    source_names_json TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    opened INTEGER NOT NULL DEFAULT 0 CHECK (opened IN (0, 1)),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'leased', 'running', 'done', 'not_sent', 'failed', 'blocked', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_owner TEXT,
    nonce TEXT,
    lease_expires_at TEXT,
    owner_host TEXT,
    owner_pid INTEGER CHECK (owner_pid IS NULL OR owner_pid > 0),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    reason_code TEXT,
    PRIMARY KEY (app, "key"),
    FOREIGN KEY (app, conversation_key) REFERENCES hr_conversations(app, "key") ON DELETE RESTRICT,
    CHECK (substr("key", 1, length(app) + 1) = app || ':'),
    CHECK (
        (state IN ('leased', 'running')
            AND lease_owner IS NOT NULL AND nonce IS NOT NULL
            AND lease_expires_at IS NOT NULL AND owner_host IS NOT NULL
            AND owner_pid IS NOT NULL)
        OR
        (state NOT IN ('leased', 'running')
            AND lease_owner IS NULL AND nonce IS NULL
            AND lease_expires_at IS NULL AND owner_host IS NULL
            AND owner_pid IS NULL)
    ),
    CHECK (
        (state IN ('queued', 'leased', 'running', 'done') AND reason_code IS NULL)
        OR
        (state IN ('not_sent', 'failed', 'blocked', 'cancelled')
            AND reason_code IS NOT NULL AND length(reason_code) > 0)
    )
) STRICT""",
    "hr_messages": """CREATE TABLE hr_messages (
    app TEXT NOT NULL CHECK (app = 'boss'),
    "key" TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    inbound_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (app, "key"),
    FOREIGN KEY (app, conversation_key) REFERENCES hr_conversations(app, "key") ON DELETE RESTRICT,
    CHECK (substr("key", 1, length(app) + 1) = app || ':')
) STRICT""",
    "device_lease": """CREATE TABLE device_lease (
    device_key TEXT PRIMARY KEY CHECK (device_key = 'primary'),
    lease_owner TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_host TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    lease_expires_at TEXT NOT NULL,
    reservation_state TEXT NOT NULL CHECK (reservation_state IN ('held', 'quarantined'))
) STRICT""",
    "events": """CREATE TABLE events (
    event_id INTEGER PRIMARY KEY,
    app TEXT CHECK (app IS NULL OR app = 'boss'),
    subject_key TEXT,
    event TEXT NOT NULL CHECK (event IN ('snapshot_upserted', 'candidate_superseded', 'candidate_deferred', 'task_claimed', 'task_started', 'lease_renewed', 'task_finished', 'lease_recovered')),
    reason_code TEXT,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
) STRICT""",
}

INDEX_SQL = {
    "idx_hr_conversations_identity": (
        'CREATE UNIQUE INDEX idx_hr_conversations_identity '
        'ON hr_conversations(app, identity_hash)'
    ),
    "idx_hr_discoveries_conversation_active": (
        'CREATE UNIQUE INDEX idx_hr_discoveries_conversation_active '
        'ON hr_discoveries(app, conversation_key) '
        "WHERE state IN ('queued', 'leased', 'running', 'blocked')"
    ),
    "idx_hr_discoveries_nonce": (
        'CREATE UNIQUE INDEX idx_hr_discoveries_nonce '
        'ON hr_discoveries(nonce) WHERE nonce IS NOT NULL'
    ),
    "idx_hr_discoveries_claim": (
        'CREATE INDEX idx_hr_discoveries_claim '
        'ON hr_discoveries(app, kind, state, priority DESC, created_at, "key")'
    ),
    "idx_events_subject": (
        'CREATE INDEX idx_events_subject ON events(app, subject_key, event_id)'
    ),
}

EXPECTED_OBJECTS = {
    **{name: ("table", sql) for name, sql in TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in INDEX_SQL.items()},
}

_FAULT_HOOK = Callable[[int, sqlite3.Connection], None]


def connect_database(path: Path) -> sqlite3.Connection:
    """打开现有库并设置外键与忙等待；结构不是完整 v1 时拒绝使用。"""
    if not path.is_file():
        raise StoreError("store_io_error")
    try:
        connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except (sqlite3.Error, OSError):
        raise StoreError("store_io_error") from None
    try:
        verify_v1(connection)
    except Exception:
        connection.close()
        raise
    return connection


def initialize_database(
    path: Path, *, fault_hook: _FAULT_HOOK | None = None
) -> None:
    """幂等初始化或核验 v1 库；仅全新空库建表，绝不覆盖已有业务行。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except (sqlite3.Error, OSError):
        raise StoreError("store_io_error") from None

    try:
        _initialize_in_transaction(connection, fault_hook)
    finally:
        connection.close()


def _initialize_in_transaction(
    connection: sqlite3.Connection, fault_hook: _FAULT_HOOK | None
) -> None:
    """在一个写事务内判断空库、建表或核验，任何失败都回滚。"""
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        names = _user_object_names(connection)
    except sqlite3.DatabaseError:
        _rollback(connection)
        raise StoreError("store_io_error") from None
    except sqlite3.Error:
        _rollback(connection)
        raise StoreError("store_io_error") from None

    try:
        if names or version != 0:
            if version != SCHEMA_VERSION:
                raise StoreError("db_version_unsupported")
            verify_v1(connection)
            connection.execute("COMMIT")
            return
        statements = [TABLE_SQL[name] for name in TABLE_SQL] + [
            INDEX_SQL[name] for name in INDEX_SQL
        ]
        for index, statement in enumerate(statements):
            connection.execute(statement)
            if fault_hook is not None:
                fault_hook(index, connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if fault_hook is not None:
            fault_hook(len(statements), connection)
        verify_v1(connection)
        connection.execute("COMMIT")
    except StoreError:
        _rollback(connection)
        raise
    except (sqlite3.Error, OSError, RuntimeError):
        _rollback(connection)
        raise StoreError("migration_failed") from None


def verify_v1(connection: sqlite3.Connection) -> None:
    """逐项比较 user_version、对象集合与规范化 SQL，拒绝缺失或篡改结构。"""
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        objects = {
            str(row["name"]): (str(row["type"]), str(row["sql"]))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    except (sqlite3.Error, TypeError):
        raise StoreError("store_io_error") from None
    if version != SCHEMA_VERSION or set(objects) != set(EXPECTED_OBJECTS):
        raise StoreError("db_version_unsupported")
    for name, (expected_type, expected_sql) in EXPECTED_OBJECTS.items():
        actual_type, actual_sql = objects[name]
        if actual_type != expected_type or _normalize_sql(actual_sql) != _normalize_sql(
            expected_sql
        ):
            raise StoreError("db_version_unsupported")


def _user_object_names(connection: sqlite3.Connection) -> list[str]:
    """列出用户表、索引、视图或触发器名称，内部 sqlite_ 对象不计入。"""
    return [
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    ]


def _normalize_sql(sql: str) -> str:
    """压缩空白并去除尾分号，用于比较本模块生成的确定性 DDL。"""
    return " ".join(sql.strip().rstrip(";").split())


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前事务；不回显底层异常。"""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        return
