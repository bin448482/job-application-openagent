"""验证 profile 独立事实库、确认提案、冲突/有效期/撤销/遗忘与日志边界。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.adapters.sqlite.schema import initialize_database
from app.cli.main import main
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
        """创建临时项目根并复制公开策略资源。"""
        self.tmp, self.root = policy_helpers.temp_project()
        code, _payload, _stderr = policy_helpers.run_cli(
            self.root, ["policy", "init", "--dest", "prompts/local/demo"]
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
        return policy_helpers.run_cli(self.root, argv, process_log=log)

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


if __name__ == "__main__":
    unittest.main()
