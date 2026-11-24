"""验证 M5 实际 CLI 组合根的合成闭环、五动作正反例、资格、对账与恢复。"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.sqlite.activity import ProfilePolicyActivity
from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import (
    SqliteProfileStore,
    profile_runtime_path,
)
from app.domain.models import canonical_hash, conversation_key_for
from app.domain.policy import utc_now
from tests import action_helpers as helpers
from tests import policy_helpers

CONVERSATION = conversation_key_for("boss", *helpers.IDENTITY)
MESSAGE = helpers.synthetic_text_key()
TEXT_KEY = f"boss:{MESSAGE}:reply"
SUFFIX = {
    "send_resume": "resume",
    "change_wechat": "wechat",
    "change_phone": "phone",
    "delete_conversation": "delete",
}


def action_key(kind: str) -> str:
    """返回该动作类型在测试中的预期动作键。"""
    if kind == "send_text":
        return TEXT_KEY
    return f"{CONVERSATION}:{SUFFIX[kind]}"


def positive_observation(kind: str) -> dict[str, object]:
    """按动作类型构造可行的 performed 观察。"""
    if kind == "send_resume":
        return helpers.observation(
            body_readback_matches=False,
            attachment_name_matched=True,
            attachment_request_observed=True,
        )
    if kind in {"change_wechat", "change_phone"}:
        return helpers.observation(
            body_readback_matches=False,
            contact_slot_available=True,
            contact_slot_status="requested",
        )
    if kind == "delete_conversation":
        return helpers.observation(
            body_readback_matches=False,
            identity_unique=True,
            target_present_before=True,
            target_present_after=False,
            anchor_present_after=True,
        )
    return helpers.observation()


class ActionCliTests(unittest.TestCase):
    """通过真实临时 profile 库、生产 API 与 CLI 验证动作闭环。"""

    def setUp(self) -> None:
        """创建临时项目根、OS 根、策略包与当前验收凭证。"""
        self.tmp, self.root = helpers.temp_project()
        self.os_tmp = tempfile.TemporaryDirectory()
        self.os_root = Path(self.os_tmp.name)
        helpers.init_pack(self.root)
        self.credential, _ = helpers.credential(self.root)
        self.provider, self.model_id, self.model_config = helpers.model_identity()
        self.seed = helpers.seed_fixture(self.root)
        self.profile_path = profile_runtime_path(self.root, "demo")

    def tearDown(self) -> None:
        """只清理本用例拥有的临时项目根与 OS 根。"""
        self.tmp.cleanup()
        self.os_tmp.cleanup()

    def _perform_args(
        self,
        kind: str,
        fixture: str,
        *,
        mode: str = "draft_only",
        authorize: list[str] | None = None,
        confirm_action: bool = True,
        confirm_device: bool = True,
        seed: str | None = None,
        expires_at: str | None = None,
        extra: list[str] | None = None,
        target: str | None = None,
    ) -> list[str]:
        """构造 action perform 参数。"""
        if target is None:
            target = MESSAGE if kind == "send_text" else CONVERSATION
        args = [
            "action",
            "perform",
            "--profile",
            "demo",
            "--pack",
            "prompts/local/demo",
            "--action-kind",
            kind,
            "--target-key",
            target,
            "--fixture",
            fixture,
            "--credential",
            self.credential,
            "--provider",
            self.provider,
            "--model-id",
            self.model_id,
            "--model-config-hash",
            self.model_config,
            "--mode",
            mode,
        ]
        seed_rel = self.seed if seed is None else seed
        if seed_rel is not False:
            args += ["--seed-fixture", str(seed_rel)]
        for value in helpers.IDENTITY[1:]:
            args += ["--identity-label", value]
        for value in authorize if authorize is not None else [kind]:
            args += ["--authorize", value]
        if expires_at is not None:
            args += ["--authorization-expires-at", expires_at]
        if confirm_action:
            args += ["--confirm-action"]
        if confirm_device:
            args += ["--confirm-device"]
        if extra:
            args += extra
        return args

    def _run(self, argv: list[str], *, stop_checker: object | None = None):
        """用合成 OS 根运行 CLI，并在 seed 前刷新观察时间。"""
        seed_path = self.root / "tests/fixtures/actions/seed/manifest.json"
        if seed_path.is_file():
            import json

            manifest = json.loads(seed_path.read_text("utf-8"))
            manifest["observed_at"] = utc_now()
            seed_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
        if "--fixture" in argv:
            import json

            fixture_path = self.root / argv[argv.index("--fixture") + 1]
            if fixture_path.is_file() and "reconcile" not in argv:
                fixture = json.loads(fixture_path.read_text("utf-8"))
                if isinstance(fixture.get("observation"), dict):
                    fixture["observation"]["observed_at"] = utc_now()
                    fixture_path.write_text(
                        json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
                    )
        return helpers.run_action_cli(
            self.root,
            argv,
            os_root=self.os_root,
            stop_checker=stop_checker,
        )

    def _fixture(self, name: str, spec: dict[str, object]) -> str:
        """写入一份合成动作脚本并返回相对路径。"""
        return helpers.write_action_fixture(
            self.root, f"tests/fixtures/actions/{name}.json", spec
        )

    def _rows(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        """独立只读连接查询 profile 库。"""
        connection = sqlite3.connect(str(self.profile_path))
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql, params)]
        finally:
            connection.close()

    def _gate(self) -> SharedDeviceGate:
        """返回本 profile 的共享门禁只读实例。"""
        return SharedDeviceGate(
            profile_id="demo",
            profile_db_path=self.profile_path,
            os_root=self.os_root,
        )

    def test_five_actions_produce_action_specific_performed_evidence(self) -> None:
        """五动作分别经观察差分得到 sent/request_sent/deleted 且任务完成。"""
        expected = {
            "send_text": "sent",
            "send_resume": "request_sent",
            "change_wechat": "request_sent",
            "change_phone": "request_sent",
            "delete_conversation": "deleted",
        }
        for index, (kind, effect) in enumerate(expected.items()):
            with self.subTest(kind=kind):
                if index:
                    # 每个子例复用同一临时根前清掉上一动作的对象，保持独立候选。
                    self._reset_profile()
                    helpers.seed_fixture(self.root)
                fixture = self._fixture(
                    f"ok-{kind}",
                    {"observation": positive_observation(kind)},
                )
                code, payload, stderr = self._run(self._perform_args(kind, fixture))
                self.assertEqual((code, stderr), (0, ""), payload)
                result = payload["result"]
                self.assertEqual(result["state"], "performed")
                self.assertEqual(result["evidence"]["outcome"], "performed")
                self.assertEqual(result["evidence"]["effect"], effect)
                self.assertTrue(result["gate_converged"])
                rows = self._rows(
                    "SELECT state, effect FROM action_records WHERE action_key = ?",
                    (action_key(kind),),
                )
                self.assertEqual(rows, [{"state": "performed", "effect": effect}])
                self.assertEqual(
                    self._rows(
                        "SELECT state FROM hr_discoveries WHERE state != 'done'"
                    ),
                    [],
                )
                self.assertEqual(self._rows("SELECT * FROM device_lease"), [])
                self.assertIsNone(self._gate().inspect())

    def _reset_profile(self) -> None:
        """删除本用例拥有的 profile 库与共享门禁，供下一动作重新建库。"""
        if self.profile_path.exists():
            self.profile_path.unlink()
        if self.profile_path.parent.exists():
            for leftover in self.profile_path.parent.glob("runtime.sqlite3*"):
                leftover.unlink()
        guard = self.os_root / "job-application-openagent" / "device-guard.sqlite3"
        if guard.exists():
            guard.unlink()

    def test_read_only_or_missing_confirmation_performs_zero_actions(self) -> None:
        """read_only、未确认或未授权时不写 pending、不启动子进程。"""
        fixture = self._fixture("guard", {"observation": positive_observation("send_text")})
        with patch("subprocess.run") as run:
            code, payload, _ = self._run(
                self._perform_args("send_text", fixture, mode="read_only")
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "authorization_required")
        run.assert_not_called()
        with patch("subprocess.run") as run:
            code, payload, _ = self._run(
                self._perform_args("send_text", fixture, confirm_device=False)
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "authorization_required")
        run.assert_not_called()
        with patch("subprocess.run") as run:
            code, payload, _ = self._run(
                self._perform_args(
                    "send_text", fixture, authorize=["send_resume"]
                )
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "authorization_required")
        run.assert_not_called()
        if self.profile_path.exists():
            connection = sqlite3.connect(str(self.profile_path))
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM action_records"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_pack_change_after_acceptance_is_rejected(self) -> None:
        """当前包摘要变化后旧验收拒绝，不写 pending。"""
        pack = self.root / "prompts/local/demo/pack.toml"
        pack.write_bytes(
            pack.read_bytes()
            .decode("utf-8")
            .replace('tone = "tone.md"', 'tone = "tone.md"')
            .replace('max_chars = 300', 'max_chars = 299')
            .encode("utf-8")
        )
        fixture = self._fixture("drift", {"observation": positive_observation("send_text")})
        code, payload, _ = self._run(self._perform_args("send_text", fixture))
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertFalse(self.profile_path.exists())

    def test_click_before_failure_is_not_performed_positive(self) -> None:
        """执行器在点击前明确未点击且现场未进入输入时收敛 not_sent。"""
        fixture = self._fixture(
            "click-before",
            {"raise": {"reason_code": "device_disconnected", "clicked": False}},
        )
        code, payload, stderr = self._run(self._perform_args("send_text", fixture))
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertEqual(payload["result"]["state"], "not_performed")
        self.assertEqual(payload["result"]["evidence"]["reason_code"], "cancelled_before_action")
        self.assertTrue(payload["result"]["gate_converged"])
        self.assertEqual(
            self._rows("SELECT state FROM hr_discoveries WHERE state = 'not_sent'"),
            [{"state": "not_sent"}],
        )

    def test_not_performed_settled_releases_and_does_not_consume_quota(self) -> None:
        """已证明未点击且现场收敛时为 not_sent，不消耗成功文本额度。"""
        fixture = self._fixture(
            "not-performed",
            {
                "observation": helpers.observation(
                    clicked=False,
                    error="device_disconnected",
                )
            },
        )
        code, payload, stderr = self._run(self._perform_args("send_text", fixture))
        self.assertEqual((code, stderr), (0, ""), payload)
        result = payload["result"]
        self.assertEqual(result["state"], "not_performed")
        self.assertTrue(result["gate_converged"])
        self.assertEqual(
            self._rows("SELECT state FROM hr_discoveries WHERE state != 'not_sent'"),
            [],
        )
        self.assertIsNone(self._gate().inspect())
        connection = sqlite3.connect(str(self.profile_path))
        connection.row_factory = sqlite3.Row
        try:
            store_type = connection.execute(
                "SELECT COUNT(*) FROM action_records WHERE state = 'performed'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(store_type, 0)

    def test_unsettled_not_performed_keeps_pending_and_quarantines(self) -> None:
        """输入框/确认框/IME 未收敛时 pending、隔离且不释放共享占用。"""
        fixture = self._fixture(
            "unsettled",
            {
                "observation": helpers.observation(
                    clicked=False,
                    error="device_disconnected",
                    confirmation_open=True,
                    input_box_empty=False,
                )
            },
        )
        code, payload, _ = self._run(self._perform_args("send_text", fixture))
        self.assertEqual(code, 0, payload)
        result = payload["result"]
        self.assertEqual(result["state"], "pending")
        self.assertFalse(result["gate_converged"])
        self.assertIsNotNone(result["card"])
        self.assertEqual(
            self._rows(
                "SELECT state FROM hr_discoveries WHERE state = 'blocked'"
            ),
            [{"state": "blocked"}],
        )
        self.assertEqual(
            self._rows("SELECT reservation_state FROM device_lease"),
            [{"reservation_state": "quarantined"}],
        )
        occupant = self._gate().inspect()
        self.assertEqual(occupant["state"], "quarantined")

    def test_click_after_disconnect_unknown_no_replay(self) -> None:
        """点击后断连为 unknown；再次 perform 不重放同动作。"""
        fixture = self._fixture(
            "unknown",
            {
                "observation": helpers.observation(error="device_disconnected")
            },
        )
        code, payload, _ = self._run(self._perform_args("send_text", fixture))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["state"], "unknown")
        self.assertFalse(payload["result"]["gate_converged"])
        self.assertEqual(payload["result"]["card"]["reason_code"], "device_disconnected")
        before = self._rows("SELECT state FROM action_records")
        code2, payload2, _ = self._run(self._perform_args("send_text", fixture))
        self.assertEqual(code2, 2, payload2)
        self.assertIn(
            payload2["reason_code"],
            {"lease_busy", "device_quarantined", "action_conflict"},
        )
        self.assertEqual(self._rows("SELECT state FROM action_records"), before)

    def test_same_snapshot_and_already_absent_target_are_not_proof(self) -> None:
        """同快照不证明执行；删除目标早已不存在也不证明删除。"""
        same = self._fixture(
            "same-snapshot",
            {
                "observation": helpers.observation(
                    after_snapshot_id=helpers.BEFORE
                )
            },
        )
        code, payload, _ = self._run(self._perform_args("send_text", same))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["state"], "unknown")
        self._reset_profile()
        helpers.seed_fixture(self.root)
        absent = self._fixture(
            "delete-absent",
            {
                "observation": helpers.observation(
                    body_readback_matches=False,
                    clicked=False,
                    identity_unique=True,
                    target_present_before=False,
                    target_present_after=False,
                    anchor_present_after=True,
                )
            },
        )
        code, payload, _ = self._run(
            self._perform_args("delete_conversation", absent)
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["state"], "not_performed")
        self.assertEqual(payload["result"]["evidence"]["effect"], "none")

    def test_text_eligibility_positive_and_cooldown_negative(self) -> None:
        """合成有效授权且 not_sent 时资格为真；执行一次后冷却拒绝。"""
        message = "boss:msg:" + canonical_hash({"eligibility": "message"})
        task_key = "boss:discovery:" + canonical_hash({"eligibility": "task"})
        base_args = [
            "action",
            "eligibility",
            "--profile",
            "demo",
            "--pack",
            "prompts/local/demo",
            "--action-kind",
            "send_text",
            "--target-key",
            message,
            "--conversation-key",
            CONVERSATION,
            "--task-key",
            task_key,
            "--credential",
            self.credential,
            "--provider",
            self.provider,
            "--model-id",
            self.model_id,
            "--model-config-hash",
            self.model_config,
            "--authorize",
            "send_text",
            "--mode",
            "draft_only",
            "--confirm-action",
        ]
        with patch("subprocess.run") as run:
            code, payload, stderr = self._run(base_args)
        self.assertEqual((code, stderr), (0, ""), payload)
        result = payload["result"]
        self.assertTrue(result["eligible"])
        self.assertEqual(result["policy_tier"], "C")
        self.assertFalse(result["production_auto_send"])
        self.assertEqual(result["actions_performed"], 0)
        run.assert_not_called()

        expired = base_args + ["--authorization-expires-at", "2020-01-01T00:00:00.000Z"]
        code, payload, _ = self._run(expired)
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["result"]["eligible"])
        self.assertEqual(payload["result"]["reason"], "authorization_expired")

        # 执行一次真实文本动作后，同会话冷却使资格为假。
        fixture = self._fixture(
            "eligibility-send", {"observation": helpers.observation()}
        )
        code, payload, _ = self._run(
            self._perform_args(
                "send_text",
                fixture,
                target=message,
            )
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["state"], "performed")
        code, payload, _ = self._run(base_args)
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["result"]["eligible"])
        self.assertEqual(payload["result"]["reason"], "action_conflict")
        self.assertEqual(payload["result"]["history_state"], "performed")
        self.assertEqual(payload["result"]["quota"]["daily_count"], 1)
        other_message = "boss:msg:" + canonical_hash({"eligibility": "other"})
        other_args = list(base_args)
        other_args[other_args.index("--target-key") + 1] = other_message
        code, payload, _ = self._run(other_args)
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["result"]["eligible"])
        self.assertEqual(payload["result"]["reason"], "cooldown_active")

    def test_reconcile_confirm_performed_and_not_performed(self) -> None:
        """未决动作经本轮确认与真实证据收敛；核对不足不解锁。"""
        fixture = self._fixture(
            "reconcile-unknown",
            {"observation": helpers.observation(error="device_disconnected")},
        )
        code, payload, _ = self._run(self._perform_args("send_text", fixture))
        self.assertEqual(code, 0, payload)
        key = payload["result"]["action_key"]
        self.assertEqual(self._gate().inspect()["state"], "quarantined")

        unconverged = self._fixture(
            "reconcile-unconverged",
            {
                "observation": {
                    "evidence": {
                        "outcome": "performed",
                        "effect": "sent",
                        "reason_code": None,
                        "before_snapshot_id": helpers.BEFORE,
                        "after_snapshot_id": helpers.AFTER,
                        "observed_at": helpers.NOW,
                    },
                    "command_quiet": True,
                    "scene_settled": False,
                    "confirmation_open": False,
                    "ime_original": "synthetic-ime-default",
                    "ime_current": "synthetic-ime-default",
                    "matched_action_key": key,
                    "confirmed": True,
                }
            },
        )
        args = [
            "action",
            "reconcile",
            "--profile",
            "demo",
            "--pack",
            "prompts/local/demo",
            "--action-key",
            key,
            "--resolution",
            "confirm_performed",
            "--fixture",
            unconverged,
            "--mode",
            "draft_only",
            "--confirm-action",
            "--confirm-device",
            "--confirm-recovery",
        ]
        with patch("subprocess.run") as run:
            code, payload, _ = self._run(args, stop_checker=lambda pid: "stopped")
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "scene_unsettled")
        run.assert_not_called()
        self.assertEqual(
            self._rows("SELECT state FROM action_records WHERE action_key = ?", (key,)),
            [{"state": "unknown"}],
        )

        performed = self._fixture(
            "reconcile-performed",
            {
                "observation": {
                    "evidence": {
                        "outcome": "performed",
                        "effect": "sent",
                        "reason_code": None,
                        "before_snapshot_id": helpers.BEFORE,
                        "after_snapshot_id": helpers.AFTER,
                        "observed_at": helpers.NOW,
                    },
                    "command_quiet": True,
                    "scene_settled": True,
                    "confirmation_open": False,
                    "ime_original": "synthetic-ime-default",
                    "ime_current": "synthetic-ime-default",
                    "matched_action_key": key,
                    "confirmed": True,
                }
            },
        )
        args[args.index(unconverged)] = performed
        code, payload, stderr = self._run(args, stop_checker=lambda pid: "stopped")
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertEqual(payload["result"]["state"], "performed")
        self.assertTrue(payload["result"]["gate_converged"])
        self.assertIsNone(self._gate().inspect())
        self.assertEqual(self._rows("SELECT * FROM device_lease"), [])

    def test_reconcile_device_observer_reads_scene_once_and_rejects_bad_xml(self) -> None:
        """设备对账用现有观察器一次读取现场；坏 XML 收敛失败后再正例成功。"""
        fixture = self._fixture(
            "device-unknown",
            {"observation": helpers.observation(error="device_disconnected")},
        )
        code, payload, _ = self._run(self._perform_args("send_text", fixture))
        self.assertEqual(code, 0, payload)
        key = payload["result"]["action_key"]
        performed = self._fixture(
            "device-performed",
            {
                "observation": {
                    "evidence": {
                        "outcome": "performed",
                        "effect": "sent",
                        "reason_code": None,
                        "before_snapshot_id": helpers.BEFORE,
                        "after_snapshot_id": helpers.AFTER,
                        "observed_at": helpers.NOW,
                    },
                    "command_quiet": True,
                    "scene_settled": True,
                    "confirmation_open": False,
                    "ime_original": None,
                    "ime_current": "synthetic-ime-default",
                    "matched_action_key": key,
                    "confirmed": True,
                }
            },
        )
        args = [
            "action",
            "reconcile",
            "--profile",
            "demo",
            "--pack",
            "prompts/local/demo",
            "--action-key",
            key,
            "--resolution",
            "confirm_performed",
            "--fixture",
            performed,
            "--device",
            "--serial",
            "<serial>",
            "--mode",
            "draft_only",
            "--confirm-action",
            "--confirm-device",
            "--confirm-recovery",
        ]

        def make_runner(xml: str):
            """按 ADB 命令返回合成层级或 IME，不启动真实进程。"""
            calls: list[list[str]] = []

            def run(cmd: list[str], **kwargs: object):
                """返回符合命令形状的合成输出。"""
                calls.append(list(cmd))
                if "settings" in cmd:
                    out = "synthetic-ime-default\n"
                elif "cat" in cmd:
                    out = xml
                else:
                    out = ""
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=out, stderr=""
                )

            return run, calls

        bad_runner, bad_calls = make_runner("not-xml")
        with patch("subprocess.run", side_effect=bad_runner):
            code, payload, _ = self._run(args, stop_checker=lambda pid: "stopped")
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "scene_unsettled")
        self.assertTrue(bad_calls)

        good_runner, good_calls = make_runner(
            '<hierarchy><node text="消息" />'
            '<node class="android.widget.EditText" text="" /></hierarchy>'
        )
        with patch("subprocess.run", side_effect=good_runner):
            code, payload, stderr = self._run(args, stop_checker=lambda pid: "stopped")
        self.assertEqual((code, stderr), (0, ""), payload)
        self.assertEqual(payload["result"]["state"], "performed")
        self.assertTrue(payload["result"]["gate_converged"])
        self.assertTrue(any("uiautomator" in cmd for cmd in good_calls))

    def test_abandon_keeps_lock_device_isolation_and_restore_unknown(self) -> None:
        """abandon 只关任务；卡、锁、设备隔离保留，restore 仍 unknown。"""
        fixture = self._fixture(
            "abandon-unknown",
            {"observation": helpers.observation(error="device_disconnected")},
        )
        code, payload, _ = self._run(self._perform_args("send_text", fixture))
        key = payload["result"]["action_key"]
        abandon_fixture = self._fixture(
            "abandon",
            {
                "observation": {
                    "evidence": None,
                    "command_quiet": False,
                    "scene_settled": False,
                    "confirmation_open": True,
                    "ime_original": "synthetic-ime-default",
                    "ime_current": "other-ime",
                    "matched_action_key": key,
                    "confirmed": True,
                }
            },
        )
        args = [
            "action",
            "reconcile",
            "--profile",
            "demo",
            "--pack",
            "prompts/local/demo",
            "--action-key",
            key,
            "--resolution",
            "abandon",
            "--fixture",
            abandon_fixture,
            "--mode",
            "draft_only",
            "--confirm-action",
            "--confirm-device",
            "--confirm-recovery",
        ]
        code, payload, _ = self._run(args, stop_checker=lambda pid: "stopped")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["state"], "cancelled")
        self.assertFalse(payload["result"]["gate_converged"])
        self.assertTrue(payload["result"]["lock_retained"])
        self.assertEqual(
            self._rows("SELECT state FROM hr_discoveries WHERE state = 'cancelled'"),
            [{"state": "cancelled"}],
        )
        self.assertEqual(
            self._rows("SELECT reservation_state FROM device_lease"),
            [{"reservation_state": "quarantined"}],
        )
        self.assertEqual(self._gate().inspect()["state"], "quarantined")
        self.assertEqual(
            ProfilePolicyActivity(self.root, os_root=self.os_root).assess_restore(
                profile_id="demo", dest_pack="prompts/local/x"
            ),
            "unknown",
        )
        code, payload, _ = self._run(["action", "card", "--profile", "demo"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["count"], 1)
        self.assertEqual(
            payload["result"]["cards"][0]["allowed_resolutions"],
            ["confirm_performed", "confirm_not_performed", "abandon"],
        )

    def test_reconcile_requires_explicit_confirmation(self) -> None:
        """未本轮确认的人工处置不改变状态。"""
        fixture = self._fixture(
            "confirm-unknown",
            {"observation": helpers.observation(error="device_disconnected")},
        )
        code, payload, _ = self._run(self._perform_args("send_text", fixture))
        key = payload["result"]["action_key"]
        abandon = self._fixture(
            "abandon-unconfirmed",
            {
                "observation": {
                    "evidence": None,
                    "command_quiet": True,
                    "scene_settled": True,
                    "confirmation_open": False,
                    "ime_original": "synthetic-ime-default",
                    "ime_current": "synthetic-ime-default",
                    "matched_action_key": key,
                    "confirmed": True,
                }
            },
        )
        args = [
            "action",
            "reconcile",
            "--profile",
            "demo",
            "--pack",
            "prompts/local/demo",
            "--action-key",
            key,
            "--resolution",
            "abandon",
            "--fixture",
            abandon,
            "--mode",
            "draft_only",
            "--confirm-device",
            "--confirm-recovery",
        ]
        code, payload, _ = self._run(args, stop_checker=lambda pid: "stopped")
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "recovery_not_confirmed")
        self.assertEqual(
            self._rows("SELECT state FROM action_records WHERE action_key = ?", (key,)),
            [{"state": "unknown"}],
        )


if __name__ == "__main__":
    unittest.main()
