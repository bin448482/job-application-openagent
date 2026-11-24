"""验证 profile 独立事实库、确认提案、冲突/有效期/撤销/遗忘与日志边界。"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.sqlite.device_gate import SharedDeviceGate, guard_database_path
from app.adapters.sqlite.profile_schema import PROFILE_V1_EXPECTED_OBJECTS
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.adapters.sqlite.schema import initialize_database
from app.cli.main import main
from app.domain.models import Owner, canonical_hash
from app.domain.policy import parse_and_validate_pack
from app.domain.profile import select_profile_facts
from app.process_log import ProcessLog
from tests import policy_helpers

NOW = "2026-11-24T00:00:00.000Z"
LATER = "2026-12-01T00:00:00.000Z"


def _pack_with_facts(*keys: str):
    """用公开示例四载荷构造只登记给定事实键的合法包。"""
    files = policy_helpers.real_example_files()
    allow = ", ".join(f'"{key}"' for key in keys)
    files["pack.toml"] = files["pack.toml"].replace(
        "allow_keys = []", f"allow_keys = [{allow}]"
    )
    pack, issues = parse_and_validate_pack(files)
    if issues or pack is None:
        raise AssertionError([item.to_dict() for item in issues])
    return pack


def _sha(path: Path) -> str:
    """返回文件字节摘要，用于证明旧库未被改动。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _guard_hold_worker(
    os_root: str,
    project_root: str,
    profile_id: str,
    ready: object,
    release: object,
) -> None:
    """在子进程中真实持有事实写门禁事务，直到父进程显式放行。"""
    from app.adapters.sqlite.device_gate import SharedDeviceGate as Gate
    from app.adapters.sqlite.profile_store import profile_runtime_path as runtime_path

    path = runtime_path(Path(project_root), profile_id)
    gate = Gate(
        profile_id=profile_id,
        profile_db_path=path,
        os_root=Path(os_root),
    )
    with gate.write_guard(create=True):
        ready.set()
        release.wait(30)


def _acquire_probe_worker(
    os_root: str,
    project_root: str,
    profile_id: str,
    queue: object,
) -> None:
    """在子进程中尝试取得共享占用，记录实际结果并在成功时释放。"""
    import uuid as uuid_module

    from app.adapters.sqlite.device_gate import SharedDeviceGate as Gate
    from app.adapters.sqlite.profile_store import profile_runtime_path as runtime_path
    from app.domain.models import Owner as OwnerModel, canonical_hash as host_hash
    from app.ports.device_gate import DeviceGateError as GateError

    path = runtime_path(Path(project_root), profile_id)
    gate = Gate(
        profile_id=profile_id,
        profile_db_path=path,
        os_root=Path(os_root),
    )
    try:
        gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(path),
            owner=OwnerModel(
                id=str(uuid_module.uuid4()),
                pid=os.getpid(),
                host=host_hash("probe-host"),
            ),
            now=NOW,
        )
    except GateError as error:
        queue.put(error.reason_code)
    else:
        queue.put("held")
        gate.release(now=NOW, converged=True)


def _create_v1_profile(path: Path, *, profile_id: str = "demo") -> None:
    """按 v1 对象集与 meta/事实行创建严格旧 profile 库，供迁移拒绝测试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for _name, (_kind, sql) in PROFILE_V1_EXPECTED_OBJECTS.items():
            connection.execute(sql)
        connection.execute(
            "INSERT INTO profile_meta(singleton, profile_id, schema_version, created_at) "
            "VALUES (1, ?, 1, ?)",
            (profile_id, NOW),
        )
        connection.execute(
            "INSERT INTO profile_facts(fact_key, source_id, value, state, "
            "confirmed_at, expires_at, superseded_by, updated_at) "
            "VALUES ('work_location', '00000000-0000-4000-8000-000000000701', "
            "'合成城市甲', 'active', ?, NULL, NULL, ?)",
            (NOW, NOW),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def _insert_bad_gate_state(
    path: Path, *, profile_id: str, profile_db_path: str
) -> None:
    """绕过 CHECK 注入不可识别门禁状态行，模拟坏库/故障残留。"""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "INSERT INTO device_guard(device_key, profile_id, profile_db_path, "
            "owner_id, owner_pid, owner_host, nonce, state, acquired_at, updated_at) "
            "VALUES ('primary', ?, ?, ?, 4242, ?, ?, 'released', ?, ?)",
            (
                profile_id,
                profile_db_path,
                "00000000-0000-4000-8000-000000000001",
                canonical_hash("synthetic-host"),
                "00000000-0000-4000-8000-000000000077",
                NOW,
                NOW,
            ),
        )
        connection.commit()
    finally:
        connection.close()


class ProfileFactsDomainTests(unittest.TestCase):
    """覆盖唯一筛选器的缺失、过期、冲突与撤销/作废排除。"""

    def setUp(self) -> None:
        """创建临时项目根与独立 profile 库。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SqliteProfileStore(self.root, "demo", now=NOW)
        self.store.open()

    def tearDown(self) -> None:
        """关闭连接并清理本次临时根。"""
        self.store.close()
        self.tmp.cleanup()

    def _select(self, pack, now: str = NOW) -> dict[str, object]:
        """用当前库行运行唯一事实筛选器。"""
        return select_profile_facts(self.store.active_fact_rows(), pack, now=now)

    def test_missing_and_conflict_become_pending_without_values(self) -> None:
        """缺失与同键多来源冲突都只给待办，不按较新值自动择一。"""
        pack = _pack_with_facts("work_location")
        empty = self._select(pack)
        self.assertEqual(empty["allowed"], [])
        self.assertEqual(empty["pending"], [{"key": "work_location", "reason": "missing"}])

        self.store.set_fact(
            "work_location", "合成城市甲", None, now=NOW, replace=False
        )
        first = self._select(pack)
        self.assertEqual(first["allowed"][0]["value"], "合成城市甲")
        self.store.set_fact(
            "work_location", "合成城市乙", None, now=NOW, replace=False
        )
        conflicted = self._select(pack)
        self.assertEqual(conflicted["allowed"], [])
        self.assertEqual(
            conflicted["pending"], [{"key": "work_location", "reason": "conflict"}]
        )

        same_value_store = SqliteProfileStore(self.root, "same", now=NOW)
        same_value_store.open()
        try:
            same_value_store.set_fact(
                "work_location", "合成城市甲", None, now=NOW, replace=False
            )
            same_value_store.set_fact(
                "work_location", "合成城市甲", None, now=NOW, replace=False
            )
            same = select_profile_facts(
                same_value_store.active_fact_rows(), pack, now=NOW
            )
        finally:
            same_value_store.close()
        self.assertEqual(same["allowed"], [])
        self.assertEqual(
            same["pending"], [{"key": "work_location", "reason": "conflict"}]
        )

    def test_replace_supersedes_previous_active_sources(self) -> None:
        """显式 replace 才作废旧来源，并恢复唯一可注入事实。"""
        pack = _pack_with_facts("work_location")
        self.store.set_fact("work_location", "合成城市甲", None, now=NOW, replace=False)
        self.store.set_fact("work_location", "合成城市乙", None, now=NOW, replace=False)
        replaced = self.store.set_fact(
            "work_location", "合成城市丙", None, now=NOW, replace=True
        )
        self.assertFalse(replaced["conflict"])
        self.assertEqual(len(replaced["replaced_source_ids"]), 2)
        selected = self._select(pack)
        self.assertEqual([item["value"] for item in selected["allowed"]], ["合成城市丙"])
        self.assertEqual(selected["pending"], [])

    def test_expired_and_revoked_are_excluded(self) -> None:
        """到期与撤销行不进入 allowed，到期只记 expired 待办。"""
        pack = _pack_with_facts("work_location")
        self.store.set_fact(
            "work_location",
            "合成城市甲",
            "2026-11-25T00:00:00.000Z",
            now=NOW,
            replace=False,
        )
        expired = self._select(pack, now=LATER)
        self.assertEqual(expired["allowed"], [])
        self.assertEqual(
            expired["pending"], [{"key": "work_location", "reason": "expired"}]
        )
        self.store.revoke_fact("work_location", now=LATER)
        revoked = self._select(pack, now=NOW)
        self.assertEqual(revoked["allowed"], [])
        self.assertEqual(
            revoked["pending"], [{"key": "work_location", "reason": "missing"}]
        )

    def test_forget_deletes_values_and_keeps_value_free_audit(self) -> None:
        """遗忘删除事实行，只保留来源 ID/无值审计事件。"""
        self.store.set_fact("work_location", "合成城市甲", None, now=NOW, replace=False)
        result = self.store.forget_fact("work_location", now=LATER)
        self.assertEqual(result["forgotten_count"], 1)
        self.assertEqual(self.store.active_fact_rows(), [])
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            facts = connection.execute("SELECT * FROM profile_facts").fetchall()
            events = connection.execute(
                "SELECT fact_key, action, previous_source_ids_json FROM profile_fact_events"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(facts, [])
        self.assertTrue(events)
        self.assertEqual(events[-1][1], "forget")
        self.assertNotIn("合成城市甲", events[-1][2])


class ProfileFactsCliTests(unittest.TestCase):
    """覆盖本机 CLI 提案/确认/再显示、包白名单待办与日志无值。"""

    def setUp(self) -> None:
        """创建临时项目根与临时 OS 根，并复制公开策略资源。"""
        self.tmp, self.root = policy_helpers.temp_project()
        self.os_root = Path(self.tmp.name) / "os-root"
        self.os_root.mkdir()
        code, _payload, _stderr = policy_helpers.run_cli(
            self.root,
            ["policy", "init", "--dest", "prompts/local/demo"],
            os_root=self.os_root,
        )
        self.assertEqual(code, 0)
        pack_path = self.root / "prompts/local/demo/pack.toml"
        text = pack_path.read_bytes().decode("utf-8")
        pack_path.write_bytes(
            text.replace("allow_keys = []", 'allow_keys = ["work_location"]').encode(
                "utf-8"
            )
        )

    def tearDown(self) -> None:
        """清理本次临时项目根。"""
        self.tmp.cleanup()

    def _run(self, argv: list[str], log: ProcessLog | None = None):
        """在临时项目根运行 CLI 并返回退出码、JSON 与 stderr。"""
        return policy_helpers.run_cli(
            self.root, argv, process_log=log, os_root=self.os_root
        )

    def _hold(self, profile_id: str = "demo") -> SharedDeviceGate:
        """在临时 OS 根取得同 profile 的共享设备占用。"""
        path = profile_runtime_path(self.root, profile_id)
        gate = SharedDeviceGate(
            profile_id=profile_id,
            profile_db_path=path,
            os_root=self.os_root,
        )
        gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(path),
            owner=Owner(
                id="00000000-0000-4000-8000-000000000001",
                pid=4242,
                host=canonical_hash("synthetic-host"),
            ),
            now=NOW,
        )
        return gate

    def test_confirmed_write_is_blocked_during_same_profile_device_session(self) -> None:
        """设备会话期间拒绝确认写入；提案、只读与其它 profile 不受影响。"""
        confirm = [
            "facts",
            "set",
            "--profile",
            "demo",
            "--key",
            "work_location",
            "--value",
            "合成城市甲",
            "--confirm",
        ]
        gate = self._hold("demo")
        try:
            code, payload, stderr = self._run(confirm)
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["reason_code"], "device_busy")

            code, proposal, _ = self._run(confirm[:-1])
            self.assertEqual(code, 0)
            self.assertEqual(proposal["status"], "confirmation_required")

            code, shown, _ = self._run(["facts", "show", "--profile", "demo"])
            self.assertEqual(code, 0)
            self.assertEqual(shown["result"]["facts"], [])

            code, other, _ = self._run(
                [
                    "facts",
                    "set",
                    "--profile",
                    "alpha",
                    "--key",
                    "work_location",
                    "--value",
                    "合成城市乙",
                    "--confirm",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(other["result"]["result"]["source_id"])
        finally:
            gate.release(now=NOW, converged=True)

        code, payload, _ = self._run(confirm)
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["confirmed"])
        self.assertEqual(payload["result"]["display_value"], "合成城市甲")

    def test_quarantined_session_blocks_writes_with_fixed_reason(self) -> None:
        """异常隔离的占用同样拒绝写入，不自动放行或解锁。"""
        gate = self._hold("demo")
        gate.release(now=NOW, converged=False)
        code, payload, _ = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                "合成城市甲",
                "--confirm",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "device_quarantined")

    def test_blocked_write_logs_gate_category_without_value(self) -> None:
        """拒写日志使用 gate 类别与固定原因，不记录事实值。"""
        log = ProcessLog(
            self.root / "facts-blocked.jsonl", correlation_id="corr-facts-blocked"
        )
        gate = self._hold("demo")
        try:
            code, _payload, _ = self._run(
                [
                    "facts",
                    "set",
                    "--profile",
                    "demo",
                    "--key",
                    "work_location",
                    "--value",
                    "合成日志城市",
                    "--confirm",
                ],
                log=log,
            )
        finally:
            gate.release(now=NOW, converged=True)
        self.assertEqual(code, 2)
        failed = [
            event
            for event in _events(log)
            if event["stage"] == "facts" and event["result"] == "failed"
        ]
        self.assertEqual(failed[-1]["reason_code"], "device_busy")
        self.assertEqual(failed[-1]["error_category"], "gate")
        self.assertNotIn("合成日志城市", log.path.read_text(encoding="utf-8"))

    def test_set_requires_confirmation_and_redisplays_value(self) -> None:
        """未确认只给提案；确认后重读库并再显示同一值。"""
        proposal = [
            "facts",
            "set",
            "--profile",
            "demo",
            "--key",
            "work_location",
            "--value",
            "合成城市甲",
        ]
        code, payload, stderr = self._run(proposal)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["status"], "confirmation_required")
        self.assertFalse(payload["result"]["confirmed"])
        show_code, shown, _ = self._run(
            ["facts", "show", "--profile", "demo"]
        )
        self.assertEqual(show_code, 0)
        self.assertEqual(shown["result"]["facts"], [])

        code, payload, _ = self._run(proposal + ["--confirm"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["confirmed"])
        self.assertEqual(payload["result"]["display_value"], "合成城市甲")
        self.assertTrue(payload["result"]["result"]["source_id"])
        self.assertEqual(
            payload["result"]["result"]["source_id"],
            payload["result"]["result"]["source_id"].strip(),
        )

    def test_facts_open_profile_logs_success_and_failure(self) -> None:
        """facts 成功/坏库失败均有 profile/open_profile started 与唯一终态。"""
        value = "合成日志城市"
        success = ProcessLog(
            self.root / "facts-open-ok.jsonl", correlation_id="corr-facts-ok"
        )
        code, payload, _ = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                value,
                "--confirm",
            ],
            log=success,
        )
        self.assertEqual(code, 0)
        events = _events(success)
        opens = [
            event
            for event in events
            if event["stage"] == "profile"
            and event["operation"] == "open_profile"
        ]
        self.assertEqual([event["result"] for event in opens], ["started", "ok"])
        self.assertNotIn(value, success.path.read_text(encoding="utf-8"))

        path = profile_runtime_path(self.root, "demo")
        path.write_bytes(b"not-a-profile-database")
        failure = ProcessLog(
            self.root / "facts-open-fail.jsonl", correlation_id="corr-facts-fail"
        )
        code, payload, _ = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                value,
                "--confirm",
            ],
            log=failure,
        )
        self.assertEqual(code, 2)
        self.assertIn(payload["reason_code"], {"profile_mismatch", "profile_io_error"})
        events = _events(failure)
        opens = [
            event
            for event in events
            if event["stage"] == "profile"
            and event["operation"] == "open_profile"
        ]
        self.assertEqual(
            [event["result"] for event in opens], ["started", "failed"]
        )
        self.assertEqual(opens[-1]["reason_code"], payload["reason_code"])
        self.assertNotIn(value, failure.path.read_text(encoding="utf-8"))

    def test_pack_profile_must_match_selected_profile(self) -> None:
        """指定包时 profile 身份必须与 --profile 一致，不能交叉组合。"""
        for name in ("alpha", "beta"):
            code, _payload, _ = self._run(
                ["policy", "init", "--dest", f"prompts/local/{name}"]
            )
            self.assertEqual(code, 0)
        code, payload, stderr = self._run(
            [
                "facts",
                "set",
                "--profile",
                "alpha",
                "--key",
                "work_location",
                "--value",
                "合成城市甲",
                "--confirm",
            ]
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        code, payload, stderr = self._run(
            [
                "facts",
                "show",
                "--profile",
                "alpha",
                "--pack",
                "prompts/local/beta",
            ]
        )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "profile_mismatch")
        code, payload, stderr = self._run(
            [
                "facts",
                "set",
                "--profile",
                "alpha",
                "--pack",
                "prompts/local/beta",
                "--key",
                "salary_expectation",
                "--value",
                "15-20k",
                "--confirm",
            ]
        )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "profile_mismatch")
        code, payload, stderr = self._run(
            ["facts", "show", "--profile", "alpha"]
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertEqual(
            [item["key"] for item in payload["result"]["facts"]],
            ["work_location"],
        )
        code, payload, stderr = self._run(
            [
                "facts",
                "show",
                "--profile",
                "alpha",
                "--pack",
                "prompts/local/alpha",
            ]
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertTrue(payload["result"]["scoped_by_pack"])

    def test_values_never_enter_process_log(self) -> None:
        """事实值只在本机 stdout/库中，不写过程日志。"""
        log = ProcessLog(self.root / "facts.jsonl", correlation_id="corr-facts")
        value = "合成机密城市不应入日志"
        code, payload, _ = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                value,
                "--confirm",
            ],
            log=log,
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["display_value"], value)
        log_text = log.path.read_text(encoding="utf-8")
        self.assertNotIn(value, log_text)
        self.assertNotIn("demo", log_text)
        events = [
            json.loads(line) for line in log_text.splitlines() if line.strip()
        ]
        self.assertTrue(
            any(
                event["stage"] == "facts"
                and event["operation"] == "set"
                and event["result"] == "ok"
                for event in events
            )
        )

    def test_pack_scoped_pending_and_replace_resolution(self) -> None:
        """包白名单内冲突显示为待办，显式 replace 后恢复 allowed。"""
        base = [
            "facts",
            "set",
            "--profile",
            "demo",
            "--pack",
            "prompts/local/demo",
            "--key",
            "work_location",
            "--value",
        ]
        self._run(base + ["合成城市甲", "--confirm"])
        self._run(base + ["合成城市乙", "--confirm"])
        code, payload, _ = self._run(
            [
                "facts",
                "show",
                "--profile",
                "demo",
                "--pack",
                "prompts/local/demo",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["result"]["pending"],
            [{"key": "work_location", "reason": "conflict"}],
        )
        self.assertEqual(payload["result"]["allowed"], [])
        self._run(
            base
            + ["合成城市丙", "--replace", "--confirm"]
        )
        code, payload, _ = self._run(
            [
                "facts",
                "show",
                "--profile",
                "demo",
                "--pack",
                "prompts/local/demo",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["pending"], [])
        self.assertEqual(
            [item["value"] for item in payload["result"]["allowed"]],
            ["合成城市丙"],
        )

    def test_revoke_and_forget_require_confirmation(self) -> None:
        """撤销/遗忘未确认不落库；确认后撤销排除、遗忘只留无值审计。"""
        for argv in (
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                "合成城市甲",
                "--confirm",
            ],
        ):
            self._run(argv)
        code, payload, _ = self._run(
            ["facts", "revoke", "--profile", "demo", "--key", "work_location"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "confirmation_required")
        _code, shown, _ = self._run(["facts", "show", "--profile", "demo"])
        self.assertEqual(len(shown["result"]["facts"]), 1)
        code, payload, _ = self._run(
            [
                "facts",
                "revoke",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--confirm",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["result"]["revoked_count"], 1)
        code, payload, _ = self._run(
            [
                "facts",
                "forget",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--confirm",
            ]
        )
        self.assertEqual(code, 0)
        self.assertFalse(payload["result"]["secure_erase_claimed"])
        self.assertIn(
            "sqlite_wal_and_existing_local_backups_not_erased",
            payload["result"]["limitations"],
        )

    def test_existing_directory_profile_path_is_not_empty(self) -> None:
        """profile 固定路径存在但为目录时 facts show 不伪造空结果。"""
        path = profile_runtime_path(self.root, "demo")
        path.mkdir(parents=True)
        code, payload, stderr = self._run(["facts", "show", "--profile", "demo"])
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "profile_io_error")

    def test_permission_error_on_profile_path_is_not_missing(self) -> None:
        """精确 profile 路径 lstat 权限错误不伪造空结果。"""
        path = profile_runtime_path(self.root, "demo")
        real_lstat = os.lstat

        def denied(target: object) -> object:
            """只对精确 profile 路径注入 permission 错误，其它路径透传真实 lstat。"""
            if Path(target) == path:
                raise PermissionError("synthetic")
            return real_lstat(target)

        with patch("app.adapters.path_state.os.lstat", side_effect=denied):
            code, payload, stderr = self._run(
                ["facts", "show", "--profile", "demo"]
            )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "profile_io_error")

    def test_invalid_fact_value_is_rejected_without_echo(self) -> None:
        """非法事实值只返回 invalid_fact，不回显原值。"""
        value = "含换行\n的值"
        code, payload, stderr = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                value,
                "--confirm",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["reason_code"], "invalid_fact")
        self.assertNotIn(value.strip(), json.dumps(payload, ensure_ascii=False))

        contact = "13800138000"
        code, payload, _ = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                contact,
                "--confirm",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "invalid_fact")
        self.assertNotIn(contact, json.dumps(payload, ensure_ascii=False))


class ProfileFactsGateAtomicityTests(unittest.TestCase):
    """覆盖确认事实写的 gate→profile 原子保护、跨进程与首次初始化。"""

    def setUp(self) -> None:
        """创建独立项目根与独立 OS 应用数据根。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.os_root = Path(self.tmp.name) / "os-root"
        self.os_root.mkdir()

    def tearDown(self) -> None:
        """只清理本次拥有的临时根。"""
        self.tmp.cleanup()

    def _run(self, argv: list[str], log: ProcessLog | None = None):
        """在临时项目根运行 CLI 并注入临时 OS 根。"""
        return policy_helpers.run_cli(
            self.root, argv, process_log=log, os_root=self.os_root
        )

    def _gate(self, profile_id: str = "demo") -> SharedDeviceGate:
        """为临时项目根构造固定路径门禁实例。"""
        return SharedDeviceGate(
            profile_id=profile_id,
            profile_db_path=profile_runtime_path(self.root, profile_id),
            os_root=self.os_root,
        )

    def _hold(self, profile_id: str = "demo") -> SharedDeviceGate:
        """取得同 profile 的合成设备占用并返回实例。"""
        gate = self._gate(profile_id)
        gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(profile_runtime_path(self.root, profile_id)),
            owner=Owner(
                id="00000000-0000-4000-8000-000000000001",
                pid=4242,
                host=canonical_hash("synthetic-host"),
            ),
            now=NOW,
        )
        return gate

    def _fact_commands(self, profile_id: str = "demo") -> dict[str, list[str]]:
        """返回 set/revoke/forget 的确认写入命令。"""
        return {
            "set": [
                "facts",
                "set",
                "--profile",
                profile_id,
                "--key",
                "work_location",
                "--value",
                "合成城市乙",
                "--confirm",
            ],
            "revoke": [
                "facts",
                "revoke",
                "--profile",
                profile_id,
                "--key",
                "work_location",
                "--confirm",
            ],
            "forget": [
                "facts",
                "forget",
                "--profile",
                profile_id,
                "--key",
                "work_location",
                "--confirm",
            ],
        }

    def test_held_gate_blocks_set_revoke_forget_without_any_profile_write(self) -> None:
        """同 profile 占用期间三种确认写都拒绝，事实与占用行完全不变。"""
        code, payload, _ = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                "合成城市甲",
                "--confirm",
            ]
        )
        self.assertEqual(code, 0)
        original_source = payload["result"]["result"]["source_id"]
        gate = self._hold("demo")
        before = gate.inspect()
        try:
            for operation, argv in self._fact_commands().items():
                with self.subTest(operation=operation):
                    code, payload, stderr = self._run(argv)
                    self.assertEqual((code, stderr), (2, ""))
                    self.assertEqual(payload["reason_code"], "device_busy")
                    self.assertEqual(gate.inspect(), before)
                    code, shown, _ = self._run(
                        ["facts", "show", "--profile", "demo"]
                    )
                    self.assertEqual(code, 0)
                    facts = shown["result"]["facts"]
                    self.assertEqual(len(facts), 1)
                    self.assertEqual(facts[0]["source_id"], original_source)
                    self.assertEqual(facts[0]["value"], "合成城市甲")
        finally:
            gate.release(now=NOW, converged=True)
        code, payload, _ = self._run(self._fact_commands()["revoke"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["result"]["revoked_count"], 1)

    def test_held_gate_blocks_first_profile_and_v1_migration(self) -> None:
        """占用期间不创建新 profile、不迁移 v1、不写备份或事实行。"""
        fresh_path = profile_runtime_path(self.root, "fresh")
        fresh_gate = self._hold("fresh")
        try:
            code, payload, _ = self._run(
                [
                    "facts",
                    "set",
                    "--profile",
                    "fresh",
                    "--key",
                    "work_location",
                    "--value",
                    "合成城市甲",
                    "--confirm",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["reason_code"], "device_busy")
            self.assertFalse(fresh_path.exists())
            self.assertEqual(list(fresh_path.parent.glob("*.v1-backup-*")), [])
        finally:
            fresh_gate.release(now=NOW, converged=True)

        demo_path = profile_runtime_path(self.root, "demo")
        _create_v1_profile(demo_path)
        before_bytes = _sha(demo_path)
        demo_gate = self._hold("demo")
        try:
            code, payload, _ = self._run(self._fact_commands()["set"])
            self.assertEqual(code, 2)
            self.assertEqual(payload["reason_code"], "device_busy")
        finally:
            demo_gate.release(now=NOW, converged=True)
        self.assertEqual(_sha(demo_path), before_bytes)
        self.assertEqual(list(demo_path.parent.glob("*.v1-backup-*")), [])
        connection = sqlite3.connect(str(demo_path))
        try:
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]), 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_first_confirmed_write_initializes_empty_gate_and_logs_guard(self) -> None:
        """Windows 首次确认写初始化严格空门禁库，日志记 facts_guard 且无事实值。"""
        gate_path = guard_database_path(self.os_root)
        self.assertFalse(gate_path.exists())
        log = ProcessLog(self.root / "facts-first.jsonl", correlation_id="corr-first")
        value = "合成首次城市"
        code, payload, stderr = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                value,
                "--confirm",
            ],
            log=log,
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertEqual(payload["result"]["display_value"], value)
        self.assertTrue(gate_path.is_file())
        self.assertIsNone(self._gate("demo").inspect())
        connection = sqlite3.connect(str(gate_path))
        try:
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]), 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall(),
                [("device_guard",)],
            )
            self.assertIsNone(
                connection.execute("SELECT * FROM device_guard").fetchone()
            )
        finally:
            connection.close()
        events = [
            event
            for event in _events(log)
            if event["stage"] == "device_gate"
            and event["operation"] == "facts_guard"
        ]
        self.assertEqual([event["result"] for event in events], ["started", "ok"])
        self.assertNotIn(value, log.path.read_text(encoding="utf-8"))

    def test_unknown_gate_schema_blocks_cli_before_profile_creation(self) -> None:
        """坏门禁结构在事实写前失败关闭，不创建 profile。"""
        gate_path = guard_database_path(self.os_root)
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(gate_path))
        try:
            connection.execute("CREATE TABLE legacy(value TEXT)")
            connection.execute("PRAGMA user_version = 9")
            connection.commit()
        finally:
            connection.close()
        before = gate_path.read_bytes()
        code, payload, _ = self._run(
            [
                "facts",
                "set",
                "--profile",
                "demo",
                "--key",
                "work_location",
                "--value",
                "合成城市甲",
                "--confirm",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "device_gate_io_error")
        self.assertFalse(profile_runtime_path(self.root, "demo").exists())
        self.assertEqual(gate_path.read_bytes(), before)

    def test_unknown_gate_state_blocks_other_profile_cli_write(self) -> None:
        """既存未知状态行让不同 profile 的事实写也失败关闭，不创建目标 profile。"""
        gate_path = guard_database_path(self.os_root)
        with self._gate("alpha").write_guard(create=True):
            pass
        _insert_bad_gate_state(
            gate_path,
            profile_id="alpha",
            profile_db_path=str(profile_runtime_path(self.root, "alpha")),
        )
        before = gate_path.read_bytes()
        for profile_id in ("demo", "alpha"):
            with self.subTest(profile=profile_id):
                code, payload, stderr = self._run(
                    [
                        "facts",
                        "set",
                        "--profile",
                        profile_id,
                        "--key",
                        "work_location",
                        "--value",
                        "合成城市甲",
                        "--confirm",
                    ]
                )
                self.assertEqual((code, stderr), (2, ""))
                self.assertEqual(payload["reason_code"], "device_gate_io_error")
                self.assertFalse(
                    profile_runtime_path(self.root, profile_id).exists()
                )
                self.assertEqual(gate_path.read_bytes(), before)

    def test_facts_guard_blocks_acquire_immediately_then_releases(self) -> None:
        """子进程事实写事务持有时 acquire 零等待拒绝；退出后正常取得。"""
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        worker = context.Process(
            target=_guard_hold_worker,
            args=(str(self.os_root), str(self.root), "alpha", ready, release),
        )
        worker.start()
        try:
            self.assertTrue(ready.wait(30))
            self.assertIsNone(self._gate("alpha").inspect())
            started = time.perf_counter()
            with self.assertRaises(Exception) as busy:
                self._gate("alpha").acquire(
                    profile_id="alpha",
                    profile_db_path=str(profile_runtime_path(self.root, "alpha")),
                    owner=Owner(
                        id="00000000-0000-4000-8000-000000000002",
                        pid=os.getpid(),
                        host=canonical_hash("parent-host"),
                    ),
                    now=NOW,
                )
            elapsed = time.perf_counter() - started
            self.assertEqual(busy.exception.reason_code, "device_busy")
            self.assertLess(elapsed, 2.0)
            self.assertIsNone(self._gate("alpha").inspect())
        finally:
            release.set()
            worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.exitcode, 0)
        acquired = self._gate("alpha")
        acquired.acquire(
            profile_id="alpha",
            profile_db_path=str(profile_runtime_path(self.root, "alpha")),
            owner=Owner(
                id="00000000-0000-4000-8000-000000000003",
                pid=os.getpid(),
                host=canonical_hash("parent-host"),
            ),
            now=NOW,
        )
        acquired.release(now=NOW, converged=True)

    def test_different_profile_guard_keeps_release_then_acquire_switch(self) -> None:
        """其它 profile 事实写可短暂串行化；原行不变且释放后可切换新 profile。"""
        alpha = self._hold("alpha")
        before = alpha.inspect()
        beta = self._gate("beta")
        with beta.write_guard(create=True):
            self.assertEqual(alpha.inspect(), before)
        self.assertEqual(alpha.inspect(), before)
        alpha.release(now=NOW, converged=True)
        beta.acquire(
            profile_id="beta",
            profile_db_path=str(profile_runtime_path(self.root, "beta")),
            owner=Owner(
                id="00000000-0000-4000-8000-000000000004",
                pid=os.getpid(),
                host=canonical_hash("beta-host"),
            ),
            now=NOW,
        )
        self.assertEqual(beta.inspect()["profile_id"], "beta")
        beta.release(now=NOW, converged=True)
        self.assertIsNone(beta.inspect())

    def test_profile_close_failure_still_releases_gate_for_other_process(self) -> None:
        """profile close 抛错也不跳过 guard 清理；新进程可立即取得占用。"""
        log = ProcessLog(
            self.root / "facts-close-fail.jsonl", correlation_id="corr-close-fail"
        )
        real_close = SqliteProfileStore.close

        def failing_close(store_self: SqliteProfileStore) -> None:
            """先执行真实 close，再注入失败，模拟关闭阶段的故障。"""
            real_close(store_self)
            raise OSError("synthetic close failure")

        with patch.object(SqliteProfileStore, "close", failing_close):
            code, payload, stderr = self._run(
                [
                    "facts",
                    "set",
                    "--profile",
                    "demo",
                    "--key",
                    "work_location",
                    "--value",
                    "合成关闭失败城市",
                    "--confirm",
                ],
                log=log,
            )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "profile_io_error")
        self.assertIsNone(self._gate("demo").inspect())
        guard_events = [
            event
            for event in _events(log)
            if event["stage"] == "device_gate"
            and event["operation"] == "facts_guard"
        ]
        self.assertEqual(
            [event["result"] for event in guard_events], ["started", "failed"]
        )
        facts_events = [
            event for event in _events(log) if event["stage"] == "facts"
        ]
        self.assertEqual(facts_events[-1]["result"], "failed")
        self.assertNotIn(
            "合成关闭失败城市", log.path.read_text(encoding="utf-8")
        )

        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        worker = context.Process(
            target=_acquire_probe_worker,
            args=(str(self.os_root), str(self.root), "demo", queue),
        )
        worker.start()
        try:
            outcome = queue.get(timeout=30)
        finally:
            worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(outcome, "held")
        self.assertIsNone(self._gate("demo").inspect())


class ProfileStoreBindingTests(unittest.TestCase):
    """覆盖固定路径、旧合成库不导入与 profile 元数据绑定。"""

    def setUp(self) -> None:
        """创建临时项目根。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        """清理本次临时根。"""
        self.tmp.cleanup()

    def test_legacy_synthetic_library_is_not_imported(self) -> None:
        """旧 data/runtime/runtime.sqlite3 不被读取、复制或重置。"""
        legacy = self.root / "data/runtime/runtime.sqlite3"
        initialize_database(legacy)
        before = _sha(legacy)
        store = SqliteProfileStore(self.root, "demo", now=NOW)
        store.open()
        store.set_fact("work_location", "合成城市甲", None, now=NOW, replace=False)
        store.close()
        self.assertEqual(_sha(legacy), before)
        self.assertTrue(profile_runtime_path(self.root, "demo").is_file())
        self.assertNotEqual(
            profile_runtime_path(self.root, "demo"), legacy
        )

    def test_profile_metadata_mismatch_is_rejected(self) -> None:
        """把库内 profile_id 改成另一个身份后拒绝打开，不自动迁移。"""
        store = SqliteProfileStore(self.root, "demo", now=NOW)
        store.open()
        store.close()
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("UPDATE profile_meta SET profile_id = 'other'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(Exception) as caught:
            SqliteProfileStore(self.root, "demo", now=NOW, create=False).open()
        self.assertEqual(caught.exception.reason_code, "profile_mismatch")

    def test_profiles_do_not_share_database_or_facts(self) -> None:
        """不同 profile 使用不同固定库，事实不跨 profile 复制。"""
        alpha = SqliteProfileStore(self.root, "alpha", now=NOW)
        alpha.open()
        alpha.set_fact("work_location", "合成城市甲", None, now=NOW, replace=False)
        beta = SqliteProfileStore(self.root, "beta", now=NOW)
        beta.open()
        self.assertEqual(beta.active_fact_rows(), [])
        alpha.close()
        beta.close()
        self.assertNotEqual(
            profile_runtime_path(self.root, "alpha"),
            profile_runtime_path(self.root, "beta"),
        )


def _events(log: ProcessLog) -> list[dict[str, object]]:
    """读取本次运行唯一日志文件的事件。"""
    return [
        json.loads(line)
        for line in log.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


if __name__ == "__main__":
    unittest.main()
