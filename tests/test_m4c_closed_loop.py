"""验证 M4a→M4b 模拟 eval→snapshot→restore→validate/preview→eval 闭环与凭证绑定。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.adapters.files.evals import FileEvalStore
from app.adapters.files.policy import FilePolicyRepository
from app.adapters.sqlite.profile_store import (
    SqliteProfileStore,
    profile_runtime_path,
)
from app.adapters.sqlite.repo import SqliteStore
from app.process_log import ProcessLog
from app.domain.models import canonical_hash
from app.domain.policy import build_envelope, render_request
from app.ports.decision import BridgeDescriptor
from app.usecases import policy as policy_usecases
from app.usecases import policy_eval as eval_usecases
from app.usecases.policy_eval import RepositorySnapshot
from app.usecases.decision import DecisionRunError, FixedClock, decide
from tests import m1_helpers, policy_helpers
from tests.test_policy_eval import STUB, SimulatedBridge, public_decisions

NOW = "2026-11-24T00:00:00.000Z"
PROFILE_ID = "demo"


class _PreviewBridge:
    """按固定 echo 返回合法 Decision 的模拟桥，记录调用次数。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.calls = 0

    def descriptor(self) -> BridgeDescriptor:
        """返回固定 simulated 桥摘要。"""
        return BridgeDescriptor(
            provider_id="local-process",
            model_id="synthetic-model",
            model_config_hash=canonical_hash({"simulated": "preview"}),
            bridge="bridge_b",
            simulated=True,
        )

    def complete(self, *, system_message, developer_message, user_message, echo):
        """不读取正文语义，只按协议回显构造合成决策。"""
        del system_message, developer_message, user_message
        self.calls += 1
        return json.dumps(
            {
                "schema_version": 1,
                "task_id": echo["task_id"],
                "nonce": echo["nonce"],
                "input_hash": echo["input_hash"],
                "intent": "interested",
                "sub_intents": ["greeting"],
                "reply": "您好，感谢联系。",
                "evidence": "您好",
                "escalate": None,
            },
            ensure_ascii=False,
        )


class _Lease:
    """返回固定未来截止时间的合成可信租约。"""

    def current_expiry(self) -> str:
        """返回晚于合成 now 的截止时间。"""
        return "2026-11-24T00:01:00.000Z"


class _AcceptanceBridge(SimulatedBridge):
    """把模拟 transport 标记为非 simulated，仅用于验证凭证绑定路径。"""

    def descriptor(self) -> BridgeDescriptor:
        """返回相同摘要但 simulated=False 的固定桥描述。"""
        return replace(super().descriptor(), simulated=False)


class M4cClosedLoopTests(unittest.TestCase):
    """串行运行编辑、评测、快照、真实活动恢复与再次评测。"""

    def setUp(self) -> None:
        """创建临时项目根、独立 OS 根与活动包。"""
        self.tmp, self.root = policy_helpers.temp_project()
        self.os_tmp = tempfile.TemporaryDirectory()
        self.os_root = Path(self.os_tmp.name)
        self.repo = policy_helpers.repository(self.root)
        self.store = FileEvalStore(self.root)
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
        self.profile = SqliteProfileStore(self.root, PROFILE_ID, now=NOW)
        self.profile.open()
        self.profile.set_fact(
            "work_location", "合成城市甲", None, now=NOW, replace=False
        )
        self.profile.close()

    def tearDown(self) -> None:
        """只清理本用例拥有的临时根。"""
        self.tmp.cleanup()
        self.os_tmp.cleanup()

    def _eval(self, bridge: SimulatedBridge, pack_rel: str = "prompts/local/demo"):
        """对指定活动包运行一次真实 usecase 评测路径。"""
        return eval_usecases.eval_pack(
            self.repo,
            self.store,
            pack_rel,
            "public",
            bridge,
            lambda plan: True,
            now=NOW,
        )

    def test_serial_closed_loop_keeps_three_hashes_and_profile_facts(self) -> None:
        """串行闭环三值一致，恢复不创建新身份或清空同 profile 事实。"""
        first_eval = self._eval(_AcceptanceBridge(public_decisions()))
        self.assertEqual(first_eval["status"], "measured")
        self.assertIsNotNone(first_eval["acceptance_path"])
        credential = str(first_eval["acceptance_path"])
        code, payload, stderr = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "acceptance",
                "check",
                "--pack",
                "prompts/local/demo",
                "--credential",
                credential,
                "--provider",
                "local-process",
                "--model-id",
                "synthetic-model",
            ],
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertTrue(payload["result"]["valid"])

        code, snapshot, stderr = policy_helpers.run_cli(
            self.root, ["policy", "snapshot", "--pack", "prompts/local/demo"]
        )
        self.assertEqual((code, stderr), (0, ""), snapshot)
        profile_before = self._profile_facts()
        profile_db = profile_runtime_path(self.root, PROFILE_ID)
        profile_bytes_before = profile_db.read_bytes()
        code, restored, stderr = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "restore",
                "--snapshot",
                snapshot["result"]["snapshot_path"],
                "--dest",
                "prompts/local/rollback",
            ],
            os_root=self.os_root,
        )
        self.assertEqual((code, stderr), (0, ""), restored)
        self.assertEqual(
            restored["result"]["pack_hash"], snapshot["result"]["pack_hash"]
        )

        code, validated, stderr = policy_helpers.run_cli(
            self.root, ["policy", "validate", "--pack", "prompts/local/rollback"]
        )
        self.assertEqual((code, stderr), (0, ""), validated)
        self.assertEqual(
            validated["result"]["pack_hash"], snapshot["result"]["pack_hash"]
        )
        code, preview, stderr = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "preview",
                "--pack",
                "prompts/local/rollback",
                "--case",
                "policy-greeting",
            ],
        )
        self.assertEqual((code, stderr), (0, ""), preview)
        self.assertTrue(preview["result"]["preview_only"])

        second_eval = self._eval(
            SimulatedBridge(public_decisions()), "prompts/local/rollback"
        )
        report = json.loads(
            (self.root / str(second_eval["report_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(
            report["pack"]["pack_hash"], snapshot["result"]["pack_hash"]
        )
        self.assertEqual(second_eval["status"], "measured")

        self.assertEqual(self._profile_facts(), profile_before)
        self.assertEqual(profile_db.read_bytes(), profile_bytes_before)
        self.assertNotIn("合成城市甲", json.dumps(second_eval, ensure_ascii=False))
        self.assertNotIn("合成城市甲", stderr)

    def test_actual_cli_eval_snapshot_restore_preview_eval_loop(self) -> None:
        """实际 CLI eval→snapshot→restore→validate/preview→eval 串行闭环。"""
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
        profile_db = profile_runtime_path(self.root, PROFILE_ID)
        store = SqliteStore(
            profile_db,
            profile_id=PROFILE_ID,
            local_host=m1_helpers.HOST_A,
            monotonic=m1_helpers.FakeClock().monotonic,
            stop_checker=lambda pid: "stopped",
            process_log=ProcessLog(self.root / "store-loop.jsonl"),
        )
        try:
            store.upsert_snapshot(
                m1_helpers.make_result(
                    profile_id=PROFILE_ID,
                    snapshot_id="00000000-0000-4000-8000-000000000c01",
                )
            )
        finally:
            store.close()
        profile_before = _profile_audit(profile_db)
        decisions = self.root / "decisions.json"
        decisions.write_text(
            json.dumps(public_decisions(), ensure_ascii=False), encoding="utf-8"
        )
        capture = self.root / "bridge-capture.jsonl"
        eval_argv = [
            "policy",
            "eval",
            "--pack",
            "prompts/local/demo",
            "--suite",
            "public",
            "--provider",
            "local-process",
            "--bridge-command",
            sys.executable,
            "--arg",
            str(STUB),
            "--arg",
            "ok",
            "--arg",
            str(decisions),
            "--arg",
            str(capture),
            "--model-id",
            "synthetic-model",
            "--confirm-external-send",
        ]
        code, first_eval, stderr = policy_helpers.run_cli(self.root, eval_argv)
        self.assertEqual((code, stderr), (0, ""), first_eval)
        self.assertIsNotNone(first_eval["result"]["acceptance_path"])
        code, acceptance, stderr = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "acceptance",
                "check",
                "--pack",
                "prompts/local/demo",
                "--credential",
                str(first_eval["result"]["acceptance_path"]),
                "--provider",
                "local-process",
                "--model-id",
                "synthetic-model",
            ],
        )
        self.assertEqual((code, stderr), (0, ""), acceptance)
        code, snapshot, stderr = policy_helpers.run_cli(
            self.root, ["policy", "snapshot", "--pack", "prompts/local/demo"]
        )
        self.assertEqual((code, stderr), (0, ""), snapshot)
        code, restored, stderr = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "restore",
                "--snapshot",
                snapshot["result"]["snapshot_path"],
                "--dest",
                "prompts/local/rollback",
            ],
            os_root=self.os_root,
        )
        self.assertEqual((code, stderr), (0, ""), restored)
        code, validated, stderr = policy_helpers.run_cli(
            self.root, ["policy", "validate", "--pack", "prompts/local/rollback"]
        )
        self.assertEqual((code, stderr), (0, ""), validated)
        code, preview, stderr = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "preview",
                "--pack",
                "prompts/local/rollback",
                "--case",
                "policy-greeting",
            ],
        )
        self.assertEqual((code, stderr), (0, ""), preview)
        second_argv = list(eval_argv)
        second_argv[second_argv.index("prompts/local/demo")] = (
            "prompts/local/rollback"
        )
        code, second_eval, stderr = policy_helpers.run_cli(self.root, second_argv)
        self.assertEqual((code, stderr), (0, ""), second_eval)
        first_report = json.loads(
            (self.root / str(first_eval["result"]["report_path"])).read_text(
                encoding="utf-8"
            )
        )
        second_report = json.loads(
            (self.root / str(second_eval["result"]["report_path"])).read_text(
                encoding="utf-8"
            )
        )
        hashes = {
            snapshot["result"]["pack_hash"],
            restored["result"]["pack_hash"],
            validated["result"]["pack_hash"],
            first_report["pack"]["pack_hash"],
            second_report["pack"]["pack_hash"],
        }
        self.assertEqual(len(hashes), 1)
        self.assertEqual(_profile_audit(profile_db), profile_before)
        self.assertEqual((self.root / str(second_eval["result"]["acceptance_path"])).is_file(), True)

    def test_existing_directory_paths_never_publish_restore(self) -> None:
        """门禁库或 profile 库路径是目录时实际 CLI restore 必须不发布。"""
        code, snapshot, stderr = policy_helpers.run_cli(
            self.root, ["policy", "snapshot", "--pack", "prompts/local/demo"]
        )
        self.assertEqual((code, stderr), (0, ""), snapshot)
        argv = [
            "policy",
            "restore",
            "--snapshot",
            snapshot["result"]["snapshot_path"],
            "--dest",
            "prompts/local/rollback",
        ]
        gate_path = (
            self.os_root / "job-application-openagent" / "device-guard.sqlite3"
        )
        gate_path.mkdir(parents=True)
        code, payload, stderr = policy_helpers.run_cli(
            self.root, argv, os_root=self.os_root
        )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertFalse((self.root / "prompts/local/rollback").exists())
        shutil.rmtree(gate_path)
        self.assertFalse(gate_path.exists())

        profile_db = profile_runtime_path(self.root, PROFILE_ID)
        profile_db.unlink()
        profile_db.mkdir()
        code, payload, stderr = policy_helpers.run_cli(
            self.root, argv, os_root=self.os_root
        )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertFalse((self.root / "prompts/local/rollback").exists())

    def test_permission_error_paths_never_publish_restore(self) -> None:
        """精确 gate/profile 路径 lstat 权限错误按 unknown 拒绝发布。"""
        code, snapshot, stderr = policy_helpers.run_cli(
            self.root, ["policy", "snapshot", "--pack", "prompts/local/demo"]
        )
        self.assertEqual((code, stderr), (0, ""), snapshot)
        argv = [
            "policy",
            "restore",
            "--snapshot",
            snapshot["result"]["snapshot_path"],
            "--dest",
            "prompts/local/rollback",
        ]
        gate_path = (
            self.os_root / "job-application-openagent" / "device-guard.sqlite3"
        )
        real_lstat = os.lstat

        def gate_lstat(path: object) -> object:
            """只对精确门禁路径注入 permission 错误，其它路径透传真实 lstat。"""
            if Path(path) == gate_path:
                raise PermissionError("synthetic")
            return real_lstat(path)

        with patch("app.adapters.path_state.os.lstat", side_effect=gate_lstat):
            code, payload, stderr = policy_helpers.run_cli(
                self.root, argv, os_root=self.os_root
            )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertFalse((self.root / "prompts/local/rollback").exists())

        profile_db = profile_runtime_path(self.root, PROFILE_ID)

        def profile_lstat(path: object) -> object:
            """只对精确 profile 库路径注入 permission 错误，其它路径透传真实 lstat。"""
            if Path(path) == profile_db:
                raise PermissionError("synthetic")
            return real_lstat(path)

        with patch("app.adapters.path_state.os.lstat", side_effect=profile_lstat):
            code, payload, stderr = policy_helpers.run_cli(
                self.root, argv, os_root=self.os_root
            )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertFalse((self.root / "prompts/local/rollback").exists())

    def test_credential_rejects_changed_pack(self) -> None:
        """编辑策略包后旧验收凭证因 pack_hash 变化被拒绝。"""
        result = self._eval(_AcceptanceBridge(public_decisions()))
        credential = str(result["acceptance_path"])
        pack_path = self.root / "prompts/local/demo/pack.toml"
        original = pack_path.read_bytes()
        pack_path.write_bytes(
            original.decode("utf-8").replace(
                "daily_sends = 10", "daily_sends = 9"
            ).encode("utf-8")
        )
        code, payload, stderr = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "acceptance",
                "check",
                "--pack",
                "prompts/local/demo",
                "--credential",
                credential,
            ],
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertEqual(payload["errors"][0]["field"], "pack_hash")
        self.assertNotIn("daily_sends", json.dumps(payload, ensure_ascii=False))

    def test_active_shared_gate_blocks_restore_until_converged(self) -> None:
        """设备占用为 held 时 restore 拒绝且不换目录解锁；显式释放后才恢复。"""
        code, snapshot, stderr = policy_helpers.run_cli(
            self.root, ["policy", "snapshot", "--pack", "prompts/local/demo"]
        )
        self.assertEqual((code, stderr), (0, ""), snapshot)
        from app.adapters.sqlite.device_gate import SharedDeviceGate
        from app.domain.models import Owner, canonical_hash

        profile_path = profile_runtime_path(self.root, PROFILE_ID)
        gate = SharedDeviceGate(
            profile_id=PROFILE_ID,
            profile_db_path=profile_path,
            os_root=self.os_root,
        )
        gate.acquire(
            profile_id=PROFILE_ID,
            profile_db_path=str(profile_path),
            owner=Owner(
                id="00000000-0000-4000-8000-000000000777",
                pid=777,
                host=canonical_hash("synthetic-gate-host"),
            ),
            now=NOW,
        )
        argv = [
            "policy",
            "restore",
            "--snapshot",
            snapshot["result"]["snapshot_path"],
            "--dest",
            "prompts/local/rollback",
        ]
        code, payload, stderr = policy_helpers.run_cli(
            self.root, argv, os_root=self.os_root
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertFalse((self.root / "prompts/local/rollback").exists())
        self.assertEqual(gate.inspect()["state"], "held")
        self.assertTrue(self._profile_facts())

        gate.release(now="2026-11-24T00:01:00.000Z", converged=True)
        code, restored, stderr = policy_helpers.run_cli(
            self.root, argv, os_root=self.os_root
        )
        self.assertEqual((code, stderr), (0, ""), restored)

    def test_decision_reloads_pack_and_stops_after_change(self) -> None:
        """真实文件快照在桥前后重读；包变更后新决策 policy_changed 且不重发。"""
        repo = FilePolicyRepository(self.root)
        _rel, base_files, _files, _guides, pack = policy_usecases.load_active_pack(
            repo, "prompts/local/demo"
        )
        case = policy_helpers.real_preview_case("policy-greeting")
        envelope = build_envelope(
            task_id=str(case["task_id"]),
            nonce=str(case["nonce"]),
            kind=str(case["kind"]),
            subject=case["subject"],
            inbound=str(case["inbound"]),
            context=list(case["context"]),
            allowed_facts=list(case["allowed_facts"]),
            base_files=base_files,
            pack=pack,
            now=NOW,
        )
        rendered = render_request(base_files, pack, envelope, now=NOW)
        bridge = _PreviewBridge()
        snapshot = RepositorySnapshot(repo, "prompts/local/demo")
        result = decide(
            rendered=rendered,
            envelope=envelope,
            base_files=base_files,
            pack=pack,
            bridge=bridge,
            snapshot=snapshot,
            lease=_Lease(),
            clock=FixedClock(NOW),
        )
        self.assertEqual(result["resolved_tier"], "C")
        self.assertEqual(bridge.calls, 1)
        tone = self.root / "prompts/local/demo/tone.md"
        tone.write_bytes(tone.read_bytes() + "合成编辑".encode("utf-8"))
        with self.assertRaises(DecisionRunError) as caught:
            decide(
                rendered=rendered,
                envelope=envelope,
                base_files=base_files,
                pack=pack,
                bridge=bridge,
                snapshot=snapshot,
                lease=_Lease(),
                clock=FixedClock(NOW),
            )
        self.assertEqual(caught.exception.reason_code, "policy_changed")
        self.assertEqual(bridge.calls, 1)

    def _profile_facts(self) -> list[dict[str, object]]:
        """读取同 profile 的 active 事实，确认恢复前后未重置或复制。"""
        store = SqliteProfileStore(
            self.root, PROFILE_ID, create=False, now=NOW
        )
        store.open()
        try:
            return store.active_fact_rows()
        finally:
            store.close()


def _profile_audit(path: Path) -> dict[str, object]:
    """返回 profile 事实/任务审计快照，用于恢复前后不变比较。"""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return {
            "meta": [
                dict(row)
                for row in connection.execute(
                    "SELECT profile_id, schema_version, created_at FROM profile_meta"
                )
            ],
            "facts": [
                dict(row)
                for row in connection.execute(
                    "SELECT fact_key, source_id, state, confirmed_at, expires_at "
                    "FROM profile_facts ORDER BY fact_key, source_id"
                )
            ],
            "fact_events": [
                dict(row)
                for row in connection.execute(
                    "SELECT fact_key, action, source_id, created_at "
                    "FROM profile_fact_events ORDER BY event_id"
                )
            ],
            "task_events": [
                dict(row)
                for row in connection.execute(
                    "SELECT event, reason_code, created_at, payload_json "
                    "FROM events ORDER BY event_id"
                )
            ],
        }
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
