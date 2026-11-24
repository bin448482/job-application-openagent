"""验证 M1 空库初始化、结构核验、幂等、回滚与 store init CLI 契约。"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.adapters.sqlite import schema as schema_module
from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.schema import (
    EXPECTED_OBJECTS,
    INDEX_SQL,
    TABLE_SQL,
    connect_database,
    initialize_database,
)
from app.cli.main import main
from app.domain.models import canonical_hash
from app.ports.store import StoreError
from app.process_log import ProcessLog

from tests.m1_helpers import HOST_A, database_dump, make_result, open_store

SUCCESS_TEXT = '{"schema_version":1,"status":"ok","reason_code":null}\n'


def _remove_link(path: Path) -> None:
    """清理测试创建的目录链接或 junction，不跟随到目标目录。"""
    try:
        os.rmdir(path)
    except OSError:
        return


class MigrationTests(unittest.TestCase):
    """覆盖 v1 建库、失败回滚、篡改拒绝与重复初始化不改行。"""

    def setUp(self) -> None:
        """为每个测试创建独立临时目录。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

    def _path(self, name: str = "state.sqlite3") -> Path:
        """返回临时目录中的测试库路径。"""
        return self.directory / name

    def test_fresh_init_creates_strict_v1_objects(self) -> None:
        """新库得到五张 STRICT 表、五个索引与 user_version=1。"""
        path = self._path()
        initialize_database(path)
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            objects = {
                row["name"]: row["type"]
                for row in connection.execute(
                    "SELECT name, type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(set(objects), set(EXPECTED_OBJECTS))
            strict = {
                row["name"]: row["strict"]
                for row in connection.execute("PRAGMA table_list")
                if row["name"] in TABLE_SQL
            }
            self.assertEqual(set(strict), set(TABLE_SQL))
            self.assertTrue(all(value == 1 for value in strict.values()))
        finally:
            connection.close()

    def test_reinit_preserves_business_rows(self) -> None:
        """已存在完整 v1 库重复初始化只核验，不改业务行。"""
        path, store, clock = open_store(self.directory)
        store.upsert_snapshot(make_result(observed_at=clock.now()))
        store.close()
        before = database_dump(path)
        initialize_database(path)
        initialize_database(path)
        self.assertEqual(database_dump(path), before)
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            connection.close()

    def test_init_rolls_back_on_midway_failure(self) -> None:
        """建表中途失败时 user_version 仍为 0 且无残留表或索引。"""
        path = self._path()
        calls: list[int] = []

        def hook(index: int, connection: sqlite3.Connection) -> None:
            """在第三条 DDL 后注入故障，验证建表事务整体回滚。"""
            calls.append(index)
            if index == 2:
                raise RuntimeError("injected DDL failure")

        with self.assertRaises(StoreError) as context:
            initialize_database(path, fault_hook=hook)
        self.assertEqual(context.exception.reason_code, "migration_failed")
        self.assertEqual(calls, [0, 1, 2])
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_init_rolls_back_after_version_write(self) -> None:
        """全部 DDL 后、设置版本后的故障同样回滚为无表且版本 0。"""
        path = self._path()
        final_index = len(TABLE_SQL) + len(INDEX_SQL)

        def hook(index: int, connection: sqlite3.Connection) -> None:
            """在版本写入后的最后故障点抛错，验证版本与 DDL 一并回滚。"""
            if index == final_index:
                raise RuntimeError("injected version failure")

        with self.assertRaises(StoreError) as context:
            initialize_database(path, fault_hook=hook)
        self.assertEqual(context.exception.reason_code, "migration_failed")
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_init_maps_write_lock_to_store_busy(self) -> None:
        """另一连接持写锁时初始化报 store_busy；释放后幂等成功且数据不变。"""
        path = self._path()
        initialize_database(path)
        before = database_dump(path)
        holder = sqlite3.connect(str(path))
        holder.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(schema_module, "BUSY_TIMEOUT_MS", 5):
                with self.assertRaises(StoreError) as context:
                    initialize_database(path)
            self.assertEqual(context.exception.reason_code, "store_busy")
        finally:
            holder.rollback()
            holder.close()
        initialize_database(path)
        self.assertEqual(database_dump(path), before)

    def test_open_maps_exclusive_lock_to_store_busy(self) -> None:
        """打开/核验遇到独占写锁且等待耗尽时同样报 store_busy。"""
        path = self._path()
        initialize_database(path)
        holder = sqlite3.connect(str(path))
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with patch.object(schema_module, "BUSY_TIMEOUT_MS", 5):
                with self.assertRaises(StoreError) as context:
                    connect_database(path)
            self.assertEqual(context.exception.reason_code, "store_busy")
        finally:
            holder.rollback()
            holder.close()
        connection = connect_database(path)
        connection.close()

    def test_unknown_version_is_rejected(self) -> None:
        """user_version 不是 1 的库拒绝打开或初始化。"""
        path = self._path()
        initialize_database(path)
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()
        for operation in (
            lambda: initialize_database(path),
            lambda: connect_database(path),
            lambda: SqliteStore(
                path,
                local_host=HOST_A,
                process_log=ProcessLog(path=self._path("open-process.jsonl")),
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(StoreError) as context:
                    operation()
                self.assertEqual(
                    context.exception.reason_code, "db_version_unsupported"
                )

    def test_nonempty_unversioned_library_is_rejected(self) -> None:
        """已有用户表但 user_version=0 的库不能当作空库重建。"""
        path = self._path()
        initialize_database(path)
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()
        with self.assertRaises(StoreError) as context:
            initialize_database(path)
        self.assertEqual(context.exception.reason_code, "db_version_unsupported")

    def test_tampered_schema_is_rejected(self) -> None:
        """缺索引、多列、多表或替换表定义都拒绝为未知版本。"""
        tampers = {
            "drop-index": lambda connection: connection.execute(
                "DROP INDEX idx_events_subject"
            ),
            "drop-partial-index": lambda connection: connection.execute(
                "DROP INDEX idx_hr_discoveries_nonce"
            ),
            "add-column": lambda connection: connection.execute(
                "ALTER TABLE hr_discoveries ADD COLUMN extra TEXT"
            ),
            "add-table": lambda connection: connection.execute(
                "CREATE TABLE extra (value TEXT)"
            ),
            "replace-table": lambda connection: (
                connection.execute("DROP TABLE events"),
                connection.execute(
                    "CREATE TABLE events (event_id INTEGER PRIMARY KEY, app TEXT) STRICT"
                ),
            ),
        }
        for name, tamper in tampers.items():
            with self.subTest(name=name):
                path = self._path(f"{name}.sqlite3")
                initialize_database(path)
                connection = sqlite3.connect(str(path))
                tamper(connection)
                connection.commit()
                connection.close()
                with self.assertRaises(StoreError) as context:
                    initialize_database(path)
                self.assertEqual(
                    context.exception.reason_code, "db_version_unsupported"
                )
                with self.assertRaises(StoreError) as context:
                    connect_database(path)
                self.assertEqual(
                    context.exception.reason_code, "db_version_unsupported"
                )

    def test_non_sqlite_file_is_store_io_error(self) -> None:
        """非 SQLite 文件或目录不能伪造成功建库。"""
        path = self._path("corrupt.sqlite3")
        path.write_bytes(b"not a sqlite database")
        with self.assertRaises(StoreError) as context:
            initialize_database(path)
        self.assertEqual(context.exception.reason_code, "store_io_error")
        with self.assertRaises(StoreError) as context:
            connect_database(path)
        self.assertEqual(context.exception.reason_code, "store_io_error")
        with self.assertRaises(StoreError) as context:
            initialize_database(self.directory)
        self.assertEqual(context.exception.reason_code, "store_io_error")

    def test_schema_checks_app_lease_reason_and_foreign_keys(self) -> None:
        """直接 SQL 也受 app=boss、租约联动、原因、前缀与外键约束。"""
        path = self._path()
        initialize_database(path)
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA foreign_keys = ON")
        identity = canonical_hash("合成身份")
        conversation_key = f"boss:conv:{identity}"
        discovery_key = f"boss:discovery:{canonical_hash('合成发现')}"
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_conversations (app, \"key\", identity_hash) "
                    "VALUES ('liepin', ?, ?)",
                    (conversation_key, identity),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_conversations (app, \"key\", identity_hash) "
                    "VALUES ('boss', ?, ?)",
                    ("liepin:conv:" + identity, identity),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_conversations (app, \"key\", identity_hash, "
                    "baseline_preview_hash) VALUES ('boss', ?, ?, ?)",
                    (conversation_key, identity, canonical_hash("预览")),
                )
            connection.execute(
                "INSERT INTO hr_conversations (app, \"key\", identity_hash) "
                "VALUES ('boss', ?, ?)",
                (conversation_key, identity),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_discoveries (app, \"key\", conversation_key, kind, "
                    "fingerprint, source_names_json, snapshot_id, state, reason_code, "
                    "created_at, updated_at) VALUES ('boss', ?, ?, 'unread', ?, '[]', "
                    "'x', 'queued', 'bad', 't', 't')",
                    (discovery_key, conversation_key, identity),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_discoveries (app, \"key\", conversation_key, kind, "
                    "fingerprint, source_names_json, snapshot_id, state, created_at, "
                    "updated_at) VALUES ('boss', ?, ?, 'unread', ?, '[]', 'x', "
                    "'leased', 't', 't')",
                    (discovery_key, conversation_key, identity),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_discoveries (app, \"key\", conversation_key, kind, "
                    "fingerprint, source_names_json, snapshot_id, created_at, updated_at) "
                    "VALUES ('boss', ?, ?, 'unread', ?, '[]', 'x', 't', 't')",
                    (discovery_key, "boss:conv:" + "0" * 64, identity),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO device_lease (device_key, lease_owner, owner_pid, "
                    "owner_host, nonce, lease_expires_at, reservation_state) "
                    "VALUES ('primary', 'o', 0, 'h', 'n', 't', 'held')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO events (event, created_at, payload_json) "
                    "VALUES ('custom_event', 't', '{}')"
                )
        finally:
            connection.close()


    def test_lease_state_foreign_key_rollback_and_active_unique(self) -> None:
        """SQL CHECK/FK/RESTRICT/active 唯一索引的真实正负控与行保持。"""
        path = self.directory / "constraints.sqlite3"
        initialize_database(path)
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            conv = "boss:conv:" + canonical_hash(["a", "b", "c"])
            hash_value = conv.rsplit(":", 1)[-1]
            connection.execute(
                "INSERT INTO hr_conversations(app, key, identity_hash) VALUES ('boss', ?, ?)",
                (conv, hash_value),
            )
            connection.execute(
                "INSERT INTO hr_discoveries(app, key, conversation_key, kind, fingerprint, "
                "source_names_json, snapshot_id, state, created_at, updated_at) "
                "VALUES ('boss', 'boss:discovery:one', ?, 'unread', 'f', '[\"row_badge\"]', "
                "'00000000-0000-4000-8000-000000000001', 'queued', 'now', 'now')",
                (conv,),
            )
            connection.commit()
            before = connection.execute("SELECT COUNT(*) FROM hr_discoveries").fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_discoveries(app, key, conversation_key, kind, fingerprint, "
                    "source_names_json, snapshot_id, state, created_at, updated_at) "
                    "VALUES ('boss', 'boss:discovery:two', ?, 'unread', 'f2', '[\"row_badge\"]', "
                    "'00000000-0000-4000-8000-000000000002', 'queued', 'now2', 'now2')",
                    (conv,),
                )
            connection.rollback()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM hr_discoveries").fetchone()[0],
                before,
            )
            # 非法 leased 缺租约字段由 CHECK 拒绝；合法完整租约接受。
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO hr_discoveries(app, key, conversation_key, kind, fingerprint, "
                    "source_names_json, snapshot_id, state, created_at, updated_at) "
                    "VALUES ('boss', 'boss:discovery:bad', ?, 'unread', 'f3', '[\"row_badge\"]', "
                    "'00000000-0000-4000-8000-000000000003', 'leased', 'now', 'now')",
                    (conv,),
                )
            connection.rollback()
            conv2 = "boss:conv:" + canonical_hash(["d", "e", "f"])
            connection.execute(
                "INSERT INTO hr_conversations(app, key, identity_hash) VALUES ('boss', ?, ?)",
                (conv2, conv2.rsplit(":", 1)[-1]),
            )
            connection.execute(
                "INSERT INTO hr_discoveries(app, key, conversation_key, kind, fingerprint, "
                "source_names_json, snapshot_id, state, lease_owner, nonce, lease_expires_at, "
                "owner_host, owner_pid, attempt, created_at, updated_at) "
                "VALUES ('boss', 'boss:discovery:leased', ?, 'unread', 'f4', '[\"row_badge\"]', "
                "'00000000-0000-4000-8000-000000000004', 'leased', "
                "'11111111-1111-4111-8111-111111111111', "
                "'22222222-2222-4222-8222-222222222222', 'later', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 42, 1, 'now', 'now')",
                (conv2,),
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM hr_conversations WHERE key = ?", (conv,))
            connection.rollback()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM hr_conversations").fetchone()[0], 2
            )
            self.assertGreaterEqual(
                connection.execute("SELECT COUNT(*) FROM hr_discoveries").fetchone()[0], 2
            )
        finally:
            connection.close()


class StoreCliTests(unittest.TestCase):
    """覆盖 store init 的固定 JSON、幂等、路径边界与安全错误出口。"""

    def setUp(self) -> None:
        """创建临时项目根与过程日志路径。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_path = self.root / "process.jsonl"
        self.log_paths: list[Path] = []
        self.patcher = patch("app.cli.main.PROJECT_ROOT", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _run(self, *args: str) -> tuple[int, str, str]:
        """在进程内运行 CLI 并返回退出码、stdout 与 stderr。"""
        stdout = io.StringIO()
        stderr = io.StringIO()
        log = ProcessLog(path=self.log_path)
        self.log_paths.append(log.path)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                list(args),
                process_log=log,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def _all_events(self) -> list[dict[str, object]]:
        """按创建顺序读取本次测试所有独立运行日志的事件。"""
        return [
            json.loads(line)
            for path in self.log_paths
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_store_init_is_idempotent_with_fixed_json(self) -> None:
        """连续两次 init 都退出 0 且第二次不改业务行。"""
        relative = "data/runtime/m1.sqlite3"
        code, stdout, stderr = self._run("store", "init", "--db", relative)
        self.assertEqual((code, stdout, stderr), (0, SUCCESS_TEXT, ""))
        self.assertEqual(list(json.loads(stdout)), ["schema_version", "status", "reason_code"])
        path = self.root / relative
        connection = sqlite3.connect(str(path))
        identity = canonical_hash("幂等合成身份")
        connection.execute(
            "INSERT INTO hr_conversations (app, \"key\", identity_hash) VALUES ('boss', ?, ?)",
            (f"boss:conv:{identity}", identity),
        )
        connection.commit()
        connection.close()
        before = database_dump(path)
        code, stdout, stderr = self._run("store", "init", "--db", relative)
        self.assertEqual((code, stdout, stderr), (0, SUCCESS_TEXT, ""))
        self.assertEqual(database_dump(path), before)
        events = self._all_events()
        self.assertIn("store", [event["stage"] for event in events])
        self.assertEqual(
            [event["result"] for event in events if event["stage"] == "store"],
            ["started", "ok", "started", "ok"],
        )

    def test_store_init_default_database_path(self) -> None:
        """省略 --db 时写到项目内默认 data/runtime/runtime.sqlite3。"""
        code, stdout, stderr = self._run("store", "init")
        self.assertEqual((code, stdout, stderr), (0, SUCCESS_TEXT, ""))
        self.assertTrue((self.root / "data/runtime/runtime.sqlite3").is_file())

    def test_store_init_rejects_unsafe_paths(self) -> None:
        """绝对路径、~、越界 ..、空值与重复选项都 invalid_input。"""
        outside = (self.root.parent / "outside.sqlite3").resolve()
        cases = (
            ("absolute", ("store", "init", "--db", str(outside))),
            ("tilde", ("store", "init", "--db", "~/state.sqlite3")),
            ("escape", ("store", "init", "--db", "data/../../outside.sqlite3")),
            ("empty", ("store", "init", "--db", "")),
            ("duplicate", ("store", "init", "--db", "a.sqlite3", "--db", "b.sqlite3")),
            ("missing-subcommand", ("store",)),
            ("unknown-option", ("store", "init", "--db", "a.sqlite3", "--force")),
            ("missing-value", ("store", "init", "--db")),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                code, stdout, stderr = self._run(*arguments)
                self.assertEqual(code, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(
                    json.loads(stdout),
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "reason_code": "invalid_input",
                    },
                )

    def test_store_init_rejects_corrupt_tampered_and_unknown_libraries(self) -> None:
        """损坏文件、被篡改 v1 与未知版本分别输出固定仓储原因。"""
        runtime = self.root / "data/runtime"
        runtime.mkdir(parents=True)
        corrupt = runtime / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        code, stdout, _ = self._run("store", "init", "--db", "data/runtime/corrupt.sqlite3")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["reason_code"], "store_io_error")

        tampered = runtime / "tampered.sqlite3"
        initialize_database(tampered)
        connection = sqlite3.connect(str(tampered))
        connection.execute("DROP INDEX idx_events_subject")
        connection.commit()
        connection.close()
        code, stdout, _ = self._run("store", "init", "--db", "data/runtime/tampered.sqlite3")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["reason_code"], "db_version_unsupported")

        future = runtime / "future.sqlite3"
        initialize_database(future)
        connection = sqlite3.connect(str(future))
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()
        code, stdout, _ = self._run("store", "init", "--db", "data/runtime/future.sqlite3")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["reason_code"], "db_version_unsupported")

    def test_store_init_rejects_symlink_escape(self) -> None:
        """解析符号链接或 junction 后越出项目根的路径仍被拒绝。"""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        link = self.root / "linked"
        try:
            os.symlink(outside.name, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("symlink is unavailable on this host")
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), outside.name],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0 or not link.exists():
                self.skipTest("symlink and junction are unavailable on this host")
        self.addCleanup(_remove_link, link)
        code, stdout, stderr = self._run(
            "store", "init", "--db", "linked/escaped.sqlite3"
        )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(json.loads(stdout)["reason_code"], "invalid_input")

    def test_store_init_logs_failure_with_fixed_reason(self) -> None:
        """store 失败日志只写固定阶段、原因与 store 错误类别。"""
        code, _, _ = self._run("store", "init", "--db", "../escape.sqlite3")
        self.assertEqual(code, 2)
        events = self._all_events()
        store_events = [event for event in events if event["stage"] == "store"]
        self.assertEqual(store_events[0]["result"], "started")
        self.assertEqual(store_events[-1]["result"], "failed")
        self.assertEqual(store_events[-1]["reason_code"], "invalid_input")
        self.assertEqual(store_events[-1]["error_category"], "store")

        with patch("app.cli.main.initialize_database", side_effect=TypeError("mock")):
            code, stdout, stderr = self._run(
                "store", "init", "--db", "data/runtime/internal.sqlite3"
            )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(
            json.loads(stdout)["reason_code"], "store_io_error"
        )
        internal = [
            event
            for event in self._all_events()
            if event["stage"] == "store" and event["result"] == "failed"
        ][-1]
        self.assertEqual(internal["operation"], "init")
        self.assertIsNone(internal["reason_code"])
        self.assertEqual(internal["error_category"], "internal")
