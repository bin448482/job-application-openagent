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

PROFILE_SCHEMA_VERSION = 3
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
    action_kind TEXT NOT NULL CHECK (action_kind IN ('send_text', 'send_resume', 'change_wechat', 'change_phone', 'delete_conversation', 'apply_job')),
    task_kind TEXT NOT NULL DEFAULT 'discovery' CHECK (task_kind IN ('discovery', 'job')),
    task_key TEXT NOT NULL,
    discovery_task_key TEXT,
    job_task_key TEXT,
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
    FOREIGN KEY (app, discovery_task_key) REFERENCES hr_discoveries(app, "key") ON DELETE RESTRICT,
    FOREIGN KEY (app, job_task_key) REFERENCES jobs(app, job_key) ON DELETE RESTRICT,
    CHECK (
        (task_kind = 'discovery' AND discovery_task_key IS NOT NULL
            AND discovery_task_key = task_key AND job_task_key IS NULL)
        OR
        (task_kind = 'job' AND job_task_key IS NOT NULL
            AND job_task_key = task_key AND discovery_task_key IS NULL)
    ),
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

M6_TABLE_SQL = {
    "jobs": """CREATE TABLE jobs (
    app TEXT NOT NULL CHECK (app = 'boss'),
    job_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    conversation_key TEXT,
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
    PRIMARY KEY (app, job_key),
    CHECK (dedupe_key = job_key),
    CHECK (substr(job_key, 1, length(app) + 5) = app || ':job:'),
    CHECK (conversation_key IS NULL OR substr(conversation_key, 1, length(app) + 5) = app || ':conv:'),
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
    "job_batches": """CREATE TABLE job_batches (
    batch_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL CHECK (length(profile_id) BETWEEN 1 AND 32),
    cursor_json TEXT NOT NULL CHECK (json_valid(cursor_json)),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'paused', 'completed')),
    pause_reason TEXT,
    last_checkpoint_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (state = 'paused' OR pause_reason IS NULL),
    CHECK (state <> 'paused' OR (pause_reason IS NOT NULL AND length(pause_reason) > 0))
) STRICT""",
    "drafts": """CREATE TABLE drafts (
    draft_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL CHECK (length(profile_id) BETWEEN 1 AND 32),
    task_kind TEXT NOT NULL CHECK (task_kind IN ('discovery', 'job')),
    task_key TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    base_hash TEXT NOT NULL CHECK (length(base_hash) = 64),
    pack_hash TEXT NOT NULL CHECK (length(pack_hash) = 64),
    model_id TEXT NOT NULL,
    model_config_hash TEXT NOT NULL CHECK (length(model_config_hash) = 64),
    facts_hash TEXT NOT NULL CHECK (length(facts_hash) = 64),
    intent TEXT NOT NULL,
    sub_intents_json TEXT NOT NULL CHECK (json_valid(sub_intents_json)),
    policy_tier TEXT NOT NULL CHECK (policy_tier IN ('A', 'B', 'C')),
    reply TEXT NOT NULL CHECK (length(reply) BETWEEN 1 AND 1200),
    state TEXT NOT NULL DEFAULT 'pending_review' CHECK (state IN ('pending_review', 'reviewed', 'superseded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT""",
    "runtime_decisions": """CREATE TABLE runtime_decisions (
    profile_id TEXT NOT NULL CHECK (length(profile_id) BETWEEN 1 AND 32),
    subject_key TEXT NOT NULL,
    task_kind TEXT NOT NULL CHECK (task_kind IN ('discovery', 'job')),
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    base_hash TEXT NOT NULL CHECK (length(base_hash) = 64),
    pack_hash TEXT NOT NULL CHECK (length(pack_hash) = 64),
    model_id TEXT NOT NULL,
    model_config_hash TEXT NOT NULL CHECK (length(model_config_hash) = 64),
    facts_hash TEXT NOT NULL CHECK (length(facts_hash) = 64),
    intent TEXT NOT NULL,
    sub_intents_json TEXT NOT NULL CHECK (json_valid(sub_intents_json)),
    policy_tier TEXT NOT NULL CHECK (policy_tier IN ('A', 'B', 'C')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, subject_key, input_hash, base_hash, pack_hash, model_id, model_config_hash)
) STRICT""",
    "scheduler_events": """CREATE TABLE scheduler_events (
    event_id INTEGER PRIMARY KEY,
    batch_id TEXT,
    task_key TEXT,
    event TEXT NOT NULL CHECK (event IN ('run_started', 'run_finished', 'checkpoint_committed', 'stage_preempted', 'round_paused', 'unread_verified', 'draft_saved', 'decision_reused', 'job_claimed', 'job_started', 'job_finished', 'batch_resumed')),
    reason_code TEXT,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
) STRICT""",
}

M6_INDEX_SQL = {
    "idx_jobs_batch_state": (
        "CREATE INDEX idx_jobs_batch_state "
        "ON jobs(app, batch_id, state, priority DESC, created_at, job_key)"
    ),
    "idx_jobs_nonce": (
        "CREATE UNIQUE INDEX idx_jobs_nonce "
        "ON jobs(nonce) WHERE nonce IS NOT NULL"
    ),
    "idx_drafts_task": (
        "CREATE INDEX idx_drafts_task "
        "ON drafts(profile_id, task_key, created_at)"
    ),
    "idx_scheduler_events_batch": (
        "CREATE INDEX idx_scheduler_events_batch "
        "ON scheduler_events(batch_id, event_id)"
    ),
}

# v2 的 action_records 仅用于严格核验旧库；v3 重建后才允许 apply_job/job 任务。
ACTION_V2_RECORDS_SQL = """CREATE TABLE action_records (
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
) STRICT"""

PROFILE_V1_EXPECTED_OBJECTS = {
    **{name: ("table", sql) for name, sql in TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in INDEX_SQL.items()},
    **{name: ("table", sql) for name, sql in PROFILE_TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in PROFILE_INDEX_SQL.items()},
}

PROFILE_V2_EXPECTED_OBJECTS = {
    **PROFILE_V1_EXPECTED_OBJECTS,
    "action_records": ("table", ACTION_V2_RECORDS_SQL),
    **{
        name: ("table", sql)
        for name, sql in ACTION_TABLE_SQL.items()
        if name != "action_records"
    },
    **{name: ("index", sql) for name, sql in ACTION_INDEX_SQL.items()},
}

PROFILE_EXPECTED_OBJECTS = {
    **PROFILE_V2_EXPECTED_OBJECTS,
    "action_records": ("table", ACTION_TABLE_SQL["action_records"]),
    **{name: ("table", sql) for name, sql in M6_TABLE_SQL.items()},
    **{name: ("index", sql) for name, sql in M6_INDEX_SQL.items()},
}


def profile_expected_objects(version: int) -> dict[str, tuple[str, str]]:
    """返回指定 profile 结构版本的对象集；未知版本拒绝。"""
    if version == 1:
        return PROFILE_V1_EXPECTED_OBJECTS
    if version == 2:
        return PROFILE_V2_EXPECTED_OBJECTS
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
    """先分类空库/v1/v2/v3；旧库先核验身份与完整 DDL，再本机备份并迁移。"""
    version, objects = _read_schema(connection)
    if not objects and version == 0:
        _create_v3(connection, profile_id, now)
        return None
    if version == PROFILE_SCHEMA_VERSION and set(objects) == set(
        PROFILE_EXPECTED_OBJECTS
    ):
        _verify_objects(connection, profile_id, PROFILE_SCHEMA_VERSION)
        return None
    if version == 2 and set(objects) == set(PROFILE_V2_EXPECTED_OBJECTS):
        # 备份前先核验完整 DDL 与 profile 身份，避免把异 profile 内容复制到
        # 目标 profile 目录，也避免为坏库或错身份库生成任何备份。
        _verify_objects(connection, profile_id, 2)
        backup = _backup_profile(connection, path, backup_root, 2)
        _migrate_v2_to_v3(connection, profile_id)
        return backup
    if version == 1 and set(objects) == set(PROFILE_V1_EXPECTED_OBJECTS):
        _verify_objects(connection, profile_id, 1)
        backup = _backup_profile(connection, path, backup_root, 1)
        _migrate_v1_to_v3(connection, profile_id)
        return backup
    raise ProfileError("profile_mismatch")


def _create_v3(
    connection: sqlite3.Connection, profile_id: str, now: str
) -> None:
    """在单事务内创建 v1 事实表、v3 动作表与 M6 运行表并写入绑定身份。"""
    statements = _v3_statements()
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


def _migrate_v1_to_v3(connection: sqlite3.Connection, profile_id: str) -> None:
    """在备份完成后仅追加 v3 动作表与 M6 运行表，并原子更新结构版本。"""
    try:
        connection.execute("BEGIN IMMEDIATE")
        version, objects = _read_schema(connection)
        if version != 1 or set(objects) != set(PROFILE_V1_EXPECTED_OBJECTS):
            raise ProfileError("profile_mismatch")
        _verify_objects(connection, profile_id, 1)
        for statement in _v3_extension_statements():
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


def _migrate_v2_to_v3(connection: sqlite3.Connection, profile_id: str) -> None:
    """备份后重建动作表以加入 apply_job/job 任务，再追加 M6 表。"""
    columns = (
        "action_key, app, action_kind, task_key, target_key, conversation_key, "
        "state, outcome, effect, reason_code, before_snapshot_id, "
        "after_snapshot_id, observed_at, scene_settled, confirmation_open, "
        "ime_restored, command_quiet, authorization_json, lease_owner, "
        "owner_pid, owner_host, nonce, lease_expires_at, created_at, updated_at, "
        "attempt"
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        version, objects = _read_schema(connection)
        if version != 2 or set(objects) != set(PROFILE_V2_EXPECTED_OBJECTS):
            raise ProfileError("profile_mismatch")
        _verify_objects(connection, profile_id, 2)
        connection.execute("DROP INDEX idx_action_records_task")
        connection.execute("DROP INDEX idx_action_records_state")
        connection.execute("ALTER TABLE action_records RENAME TO action_records_v2")
        connection.execute(M6_TABLE_SQL["jobs"])
        connection.execute(ACTION_TABLE_SQL["action_records"])
        connection.execute(
            f"INSERT INTO action_records ({columns}, task_kind, discovery_task_key) "
            f"SELECT {columns}, 'discovery', task_key FROM action_records_v2"
        )
        connection.execute("DROP TABLE action_records_v2")
        for statement in _v3_after_rebuild_statements():
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


def _v3_extension_statements() -> list[str]:
    """返回 v1→v3 需要追加的 jobs/动作表/索引与其余 M6 运行表。"""
    return (
        [M6_TABLE_SQL["jobs"]]
        + [ACTION_TABLE_SQL[name] for name in ACTION_TABLE_SQL]
        + [ACTION_INDEX_SQL[name] for name in ACTION_INDEX_SQL]
        + [M6_TABLE_SQL[name] for name in M6_TABLE_SQL if name != "jobs"]
        + [M6_INDEX_SQL[name] for name in M6_INDEX_SQL]
    )


def _v3_after_rebuild_statements() -> list[str]:
    """返回 v2→v3 重建动作表后需补建的动作索引与其余 M6 运行表。"""
    return (
        [
            ACTION_INDEX_SQL["idx_action_records_task"],
            ACTION_INDEX_SQL["idx_action_records_state"],
        ]
        + [M6_TABLE_SQL[name] for name in M6_TABLE_SQL if name != "jobs"]
        + [M6_INDEX_SQL[name] for name in M6_INDEX_SQL]
    )


def _v3_statements() -> list[str]:
    """返回空库创建 v3 的确定性顺序；jobs 必须先于动作表建立外键。"""
    return (
        [TABLE_SQL[name] for name in TABLE_SQL]
        + [INDEX_SQL[name] for name in INDEX_SQL]
        + [PROFILE_TABLE_SQL[name] for name in PROFILE_TABLE_SQL]
        + [PROFILE_INDEX_SQL[name] for name in PROFILE_INDEX_SQL]
        + [M6_TABLE_SQL["jobs"]]
        + [ACTION_TABLE_SQL[name] for name in ACTION_TABLE_SQL]
        + [ACTION_INDEX_SQL[name] for name in ACTION_INDEX_SQL]
        + [M6_TABLE_SQL[name] for name in M6_TABLE_SQL if name != "jobs"]
        + [M6_INDEX_SQL[name] for name in M6_INDEX_SQL]
    )


def _backup_profile(
    connection: sqlite3.Connection,
    path: Path,
    backup_root: Path | None,
    source_version: int,
) -> str:
    """用标准库 SQLite backup API 写不覆盖旧文件的本机备份，返回相对路径。"""
    root = Path(backup_root) if backup_root is not None else path.parent
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ProfileError("profile_io_error") from None
    target = root / f"{path.name}.v{source_version}-backup-{uuid.uuid4().hex}.sqlite3"
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
    path: Path,
    profile_id: str,
    *,
    require_actions: bool = False,
    require_runtime: bool = False,
) -> sqlite3.Connection:
    """打开现有 profile 库并核验 v1/v2/v3；动作写要求 v2+，M6 要求 v3。"""
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
        if require_actions and version < 2:
            raise ProfileError("profile_mismatch")
        if require_runtime and version != PROFILE_SCHEMA_VERSION:
            raise ProfileError("profile_mismatch")
    except Exception:
        connection.close()
        raise
    return connection


def verify_profile_schema(connection: sqlite3.Connection, profile_id: str) -> int:
    """逐项比较 v1/v2/v3 版本、对象集合、规范化 SQL 与唯一 profile 身份。"""
    return _verify_any(connection, profile_id)


def _verify_any(connection: sqlite3.Connection, profile_id: str) -> int:
    """核验 v1/v2/v3 并返回版本号；其它结构固定 profile_mismatch。"""
    version, objects = _read_schema(connection)
    expected = {
        1: PROFILE_V1_EXPECTED_OBJECTS,
        2: PROFILE_V2_EXPECTED_OBJECTS,
        PROFILE_SCHEMA_VERSION: PROFILE_EXPECTED_OBJECTS,
    }
    if version in expected and set(objects) == set(expected[version]):
        _verify_objects(connection, profile_id, version)
        return version
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
