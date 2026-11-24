"""验证 run check 的设备确认、验收绑定、A 档拒绝与跨库故障隔离。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.adapters.files.evals import FileEvalStore
from app.adapters.sqlite.device_gate import SharedDeviceGate, guard_database_path
from app.adapters.sqlite.profile_store import profile_runtime_path
from app.ports.decision import BridgeDescriptor
from app.usecases import policy_eval as eval_usecases
from tests import policy_helpers
from tests.test_policy_eval import SimulatedBridge, public_decisions

NOW = "2026-11-24T00:00:00.000Z"


class _AcceptanceBridge(SimulatedBridge):
    """把模拟 transport 报告为非 simulated，验证验收绑定路径。"""

    def descriptor(self) -> BridgeDescriptor:
        """返回 simulated=False 的固定摘要。"""
        return replace(super().descriptor(), simulated=False)


class RunGuardTests(unittest.TestCase):
    """覆盖只读确认、草稿验收、固定 C 与跨库故障。"""

    def setUp(self) -> None:
        """创建临时项目根、OS 根与带事实白名单的活动包。"""
        self.tmp, self.root = policy_helpers.temp_project()
        self.os_tmp = tempfile.TemporaryDirectory()
        self.os_root = Path(self.os_tmp.name)
        code, payload, stderr = policy_helpers.run_cli(
            self.root, ["policy", "init", "--dest", "prompts/local/demo"]
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        pack_path = self.root / "prompts/local/demo/pack.toml"
        pack_path.write_bytes(
            pack_path.read_bytes()
            .decode("utf-8")
            .replace(
                "allow_keys = []",
                'allow_keys = ["work_location", "salary_expectation"]',
            )
            .encode("utf-8")
        )

    def tearDown(self) -> None:
        """只清理本用例拥有的临时根。"""
        self.tmp.cleanup()
        self.os_tmp.cleanup()

    def _check(self, extra: list[str], *, confirm: bool = False):
        """运行 run check 并返回退出码、JSON 与 stderr。"""
        argv = [
            "run",
            "check",
            "--pack",
            "prompts/local/demo",
            "--mode",
            extra.pop(0),
            *extra,
        ]
        if confirm:
            argv.append("--confirm-device")
        return policy_helpers.run_cli(self.root, argv, os_root=self.os_root)

    def _gate(self) -> SharedDeviceGate:
        """返回 demo profile 的共享门禁只读实例。"""
        return SharedDeviceGate(
            profile_id="demo",
            profile_db_path=profile_runtime_path(self.root, "demo"),
            os_root=self.os_root,
        )

    def _credential(self) -> tuple[str, str]:
        """运行一次成功公开评测并返回凭证路径与 model_config_hash。"""
        bridge = _AcceptanceBridge(public_decisions())
        result = eval_usecases.eval_pack(
            policy_helpers.repository(self.root),
            FileEvalStore(self.root),
            "prompts/local/demo",
            "public",
            bridge,
            lambda plan: True,
            now=NOW,
        )
        return (
            str(result["acceptance_path"]),
            bridge.descriptor().model_config_hash,
        )

    def test_read_only_requires_current_round_device_confirmation(self) -> None:
        """未确认不取得占用也不启动子进程；确认后只做预检并释放。"""
        with patch("subprocess.run") as run:
            code, payload, stderr = self._check(["read_only"])
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "authorization_required")
        run.assert_not_called()
        self.assertIsNone(self._gate().inspect())

        with patch("subprocess.run") as run:
            code, payload, stderr = self._check(["read_only"], confirm=True)
        self.assertEqual((code, stderr), (0, ""), payload)
        run.assert_not_called()
        result = payload["result"]
        self.assertTrue(result["preflight_only"])
        self.assertEqual(result["actions_performed"], 0)
        self.assertEqual(result["gate"], "released")
        self.assertIn("m5_actions_not_implemented", result["limitations"])
        self.assertIn("m8_run_not_implemented", result["limitations"])
        self.assertIsNone(self._gate().inspect())

    def test_draft_requires_current_acceptance_and_rejects_after_change(self) -> None:
        """草稿模式缺凭证拒绝；摘要变化后旧凭证拒绝；匹配当前包才通过。"""
        code, payload, _ = self._check(["draft_only"], confirm=True)
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "authorization_required")
        credential, config_hash = self._credential()
        code, payload, stderr = self._check(
            [
                "draft_only",
                "--credential",
                credential,
                "--provider",
                "local-process",
                "--model-id",
                "synthetic-model",
                "--model-config-hash",
                config_hash,
            ],
            confirm=True,
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertEqual(
            payload["result"]["acceptance"]["scope"], "model_eval_only"
        )

        pack_path = self.root / "prompts/local/demo/tone.md"
        pack_path.write_bytes(pack_path.read_bytes() + "合成编辑".encode("utf-8"))
        code, payload, _ = self._check(
            [
                "draft_only",
                "--credential",
                credential,
                "--provider",
                "local-process",
                "--model-id",
                "synthetic-model",
                "--model-config-hash",
                config_hash,
            ],
            confirm=True,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "policy_changed")

    def test_a_mode_is_always_rejected_without_send_authorization(self) -> None:
        """v1 固定 C 且缺独立前置判据时 A 档预检始终拒绝。"""
        code, payload, stderr = self._check(["auto_send_tier_a"], confirm=True)
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "authorization_required")
        self.assertIsNone(self._gate().inspect())

    def test_pending_profile_activity_is_not_released_as_converged(self) -> None:
        """profile 库有 blocked 未决时预检拒绝，占用保留并隔离。"""
        from app.adapters.sqlite.profile_store import SqliteProfileStore

        store = SqliteProfileStore(self.root, "demo", now=NOW)
        store.open()
        store.close()
        path = profile_runtime_path(self.root, "demo")
        conversation_key = "boss:conv:" + "1" * 64
        discovery_key = "boss:discovery:" + "2" * 64
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO hr_conversations(app, key, identity_hash) "
                "VALUES ('boss', ?, ?)",
                (conversation_key, "1" * 64),
            )
            connection.execute(
                "INSERT INTO hr_discoveries(app, key, conversation_key, kind, "
                "fingerprint, source_names_json, snapshot_id, state, priority, "
                "created_at, updated_at, reason_code) "
                "VALUES ('boss', ?, ?, 'unread', ?, '[\"row_badge\"]', ?, "
                "'blocked', 0, ?, ?, 'unread_unverified')",
                (
                    discovery_key,
                    conversation_key,
                    "3" * 64,
                    "00000000-0000-4000-8000-000000000901",
                    NOW,
                    NOW,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        code, payload, stderr = self._check(["read_only"], confirm=True)
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "authorization_required")
        self.assertEqual(self._gate().inspect()["state"], "quarantined")

    def test_cross_database_failure_quarantines_shared_occupancy(self) -> None:
        """共享占用取得后 profile 库不可用，保留占用并隔离，不补偿为空闲。"""
        profile_dir = self.root / "data/runtime/profiles/demo"
        profile_dir.mkdir(parents=True)
        (profile_dir / "runtime.sqlite3").write_bytes(b"not-a-profile-database")
        code, payload, stderr = self._check(["read_only"], confirm=True)
        self.assertEqual((code, stderr), (2, ""))
        self.assertIn(
            payload["reason_code"], {"profile_mismatch", "profile_io_error"}
        )
        row = self._gate().inspect()
        self.assertEqual(row["state"], "quarantined")
        self.assertEqual(row["profile_id"], "demo")
        connection = sqlite3.connect(str(guard_database_path(self.os_root)))
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual(tables, {"device_guard"})


if __name__ == "__main__":
    unittest.main()
