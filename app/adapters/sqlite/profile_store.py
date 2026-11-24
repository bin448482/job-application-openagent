"""实现 profile 独立运行库的事实登记、撤销、遗忘与只读活动检查。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from app.adapters.sqlite.profile_schema import (
    PROFILE_SCHEMA_VERSION,
    connect_profile_database,
    initialize_profile_database,
)
from app.domain.models import parse_utc
from app.domain.policy import is_profile_id
from app.domain.profile import (
    validate_fact_expiry,
    validate_fact_key,
    validate_fact_value,
)
from app.ports.profile import ProfileError

PROFILE_RUNTIME_ROOT = ("data", "runtime", "profiles")
PENDING_TASK_STATES = ("leased", "running", "blocked")


def profile_runtime_path(project_root: Path, profile_id: str) -> Path:
    """返回固定 profile 库路径；调用方不能自选目录或跨 profile 复用库。"""
    if not is_profile_id(profile_id):
        raise ProfileError("invalid_profile")
    return (
        Path(project_root).resolve()
        / PROFILE_RUNTIME_ROOT[0]
        / PROFILE_RUNTIME_ROOT[1]
        / PROFILE_RUNTIME_ROOT[2]
        / profile_id
        / "runtime.sqlite3"
    )


class SqliteProfileStore:
    """以独立 SQLite 库按 profile 保存长期事实与未决状态，不读取旧合成库。"""

    def __init__(
        self,
        project_root: Path,
        profile_id: str,
        *,
        create: bool = True,
        now: str,
    ) -> None:
        """保存固定 profile 身份与路径；构造阶段不创建文件。"""
        if not is_profile_id(profile_id):
            raise ProfileError("invalid_profile")
        parse_utc(now)
        self.profile_id = profile_id
        self.path = profile_runtime_path(project_root, profile_id)
        self._create = create
        self._now = now
        self._connection: sqlite3.Connection | None = None

    def open(self) -> dict[str, object]:
        """打开或创建本 profile 独立库，并核验库元数据绑定。"""
        try:
            if self._create:
                initialize_profile_database(self.path, self.profile_id, now=self._now)
            elif not self.path.is_file():
                raise ProfileError("profile_io_error")
            self._connection = connect_profile_database(self.path, self.profile_id)
        except ProfileError:
            raise
        except (sqlite3.Error, OSError):
            raise ProfileError("profile_io_error") from None
        return {
            "profile_id": self.profile_id,
            "schema_version": PROFILE_SCHEMA_VERSION,
        }

    def close(self) -> None:
        """关闭本机连接；重复关闭保持无害。"""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _require_connection(self) -> sqlite3.Connection:
        """返回已打开连接；未打开时以固定 profile_io_error 拒绝。"""
        if self._connection is None:
            raise ProfileError("profile_io_error")
        return self._connection

    def set_fact(
        self,
        key: str,
        value: str,
        expires_at: str | None,
        *,
        now: str,
        replace: bool,
    ) -> dict[str, object]:
        """新增程序生成来源的事实；replace 时显式作废旧 active 行。"""
        connection = self._require_connection()
        validate_fact_key(key)
        validate_fact_value(value)
        validate_fact_expiry(expires_at, now=now)
        source_id = str(uuid.uuid4())
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = [
                str(row["source_id"])
                for row in connection.execute(
                    "SELECT source_id FROM profile_facts "
                    "WHERE fact_key = ? AND state = 'active' ORDER BY source_id",
                    (key,),
                )
            ]
            if replace and previous:
                connection.execute(
                    "UPDATE profile_facts SET state = 'superseded', superseded_by = ?, "
                    "updated_at = ? WHERE fact_key = ? AND state = 'active'",
                    (source_id, now, key),
                )
            connection.execute(
                "INSERT INTO profile_facts(fact_key, source_id, value, state, "
                "confirmed_at, expires_at, superseded_by, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, NULL, ?)",
                (key, source_id, value, now, expires_at, now),
            )
            self._insert_event(
                connection,
                key=key,
                action="set",
                source_id=source_id,
                previous_ids=previous,
                now=now,
            )
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM profile_facts "
                    "WHERE fact_key = ? AND state = 'active' "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (key, now),
                ).fetchone()[0]
            )
            connection.execute("COMMIT")
        except ProfileError:
            _rollback(connection)
            raise
        except (sqlite3.Error, OSError):
            _rollback(connection)
            raise ProfileError("profile_io_error") from None
        return {
            "key": key,
            "value": value,
            "source": "user_confirmed",
            "source_id": source_id,
            "state": "active",
            "confirmed_at": now,
            "expires_at": expires_at,
            "replaced_source_ids": previous if replace else [],
            "conflict": active_count > 1,
        }

    def revoke_fact(self, key: str, *, now: str) -> dict[str, object]:
        """把该键全部 active 来源标记 revoked；不删除历史行。"""
        connection = self._require_connection()
        validate_fact_key(key)
        parse_utc(now)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ids = [
                str(row["source_id"])
                for row in connection.execute(
                    "SELECT source_id FROM profile_facts "
                    "WHERE fact_key = ? AND state = 'active' ORDER BY source_id",
                    (key,),
                )
            ]
            if not ids:
                raise ProfileError("fact_not_found")
            connection.execute(
                "UPDATE profile_facts SET state = 'revoked', superseded_by = NULL, "
                "updated_at = ? WHERE fact_key = ? AND state = 'active'",
                (now, key),
            )
            self._insert_event(
                connection,
                key=key,
                action="revoke",
                source_id=None,
                previous_ids=ids,
                now=now,
            )
            connection.execute("COMMIT")
        except ProfileError:
            _rollback(connection)
            raise
        except (sqlite3.Error, OSError):
            _rollback(connection)
            raise ProfileError("profile_io_error") from None
        return {"key": key, "revoked_source_ids": ids, "revoked_count": len(ids)}

    def forget_fact(self, key: str, *, now: str) -> dict[str, object]:
        """删除该键全部事实行，只保留不含值的来源 ID 审计事件。"""
        connection = self._require_connection()
        validate_fact_key(key)
        parse_utc(now)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ids = [
                str(row["source_id"])
                for row in connection.execute(
                    "SELECT source_id FROM profile_facts "
                    "WHERE fact_key = ? ORDER BY source_id",
                    (key,),
                )
            ]
            if not ids:
                raise ProfileError("fact_not_found")
            connection.execute("DELETE FROM profile_facts WHERE fact_key = ?", (key,))
            self._insert_event(
                connection,
                key=key,
                action="forget",
                source_id=None,
                previous_ids=ids,
                now=now,
            )
            connection.execute("COMMIT")
        except ProfileError:
            _rollback(connection)
            raise
        except (sqlite3.Error, OSError):
            _rollback(connection)
            raise ProfileError("profile_io_error") from None
        return {
            "key": key,
            "forgotten_source_ids": ids,
            "forgotten_count": len(ids),
            "audit": "source_ids_without_value",
        }

    def active_fact_rows(self) -> list[dict[str, object]]:
        """返回全部 active 行（含已过期），供领域唯一筛选器计算待办。"""
        connection = self._require_connection()
        try:
            rows = connection.execute(
                "SELECT fact_key, value, source_id, state, confirmed_at, expires_at "
                "FROM profile_facts WHERE state = 'active' ORDER BY fact_key, source_id"
            ).fetchall()
        except sqlite3.Error:
            raise ProfileError("profile_io_error") from None
        result: list[dict[str, object]] = []
        for row in rows:
            result.append(
                {
                    "key": str(row["fact_key"]),
                    "value": str(row["value"]),
                    "source": "user_confirmed",
                    "expires_at": row["expires_at"],
                    "source_id": str(row["source_id"]),
                    "state": str(row["state"]),
                    "confirmed_at": str(row["confirmed_at"]),
                }
            )
        return result

    def source_ids_for_key(self, key: str) -> list[str]:
        """返回该键全部来源 ID（含已撤销/作废），不返回值。"""
        connection = self._require_connection()
        validate_fact_key(key)
        try:
            return [
                str(row["source_id"])
                for row in connection.execute(
                    "SELECT source_id FROM profile_facts "
                    "WHERE fact_key = ? ORDER BY source_id",
                    (key,),
                )
            ]
        except sqlite3.Error:
            raise ProfileError("profile_io_error") from None

    def pending_activity(self) -> dict[str, object]:
        """只读统计现有 M1 任务的 leased/running/blocked 与设备行数量。"""
        connection = self._require_connection()
        try:
            counts = {
                state: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM hr_discoveries WHERE state = ?",
                        (state,),
                    ).fetchone()[0]
                )
                for state in PENDING_TASK_STATES
            }
            device_rows = int(
                connection.execute("SELECT COUNT(*) FROM device_lease").fetchone()[0]
            )
        except sqlite3.Error:
            raise ProfileError("profile_io_error") from None
        return {
            "leased": counts["leased"],
            "running": counts["running"],
            "blocked": counts["blocked"],
            "device_lease": device_rows,
        }

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        key: str,
        action: str,
        source_id: str | None,
        previous_ids: list[str],
        now: str,
    ) -> None:
        """写入不含事实值的事实审计事件；只保留来源 ID。"""
        payload = json.dumps(
            {"previous_source_ids": sorted(previous_ids)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO profile_fact_events(fact_key, action, source_id, "
            "previous_source_ids_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, action, source_id, payload, now),
        )


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前事务，不覆盖原始安全错误。"""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        return
