"""验证第六动作 apply_job 经唯一 perform_action/命令账本/执行器链路的三态。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from app.adapters.adb.command import CommandIdentityStore
from app.adapters.simulated.actions import (
    CommandLedgerActionExecutor,
    ScriptedActionExecutor,
)
from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.domain.actions import (
    ActionAuthorization,
    ActionIdentity,
    ApplyQuotaContract,
    action_key_for,
)
from app.domain.models import APP, Owner, add_seconds, canonical_hash
from app.domain.policy import utc_now
from app.usecases.actions import perform_action
from tests import action_helpers as helpers


class ApplyActionEndToEndTests(unittest.TestCase):
    """不启动真实 ADB；用真实临时账本与合成观察验证动作生命周期。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        profile = SqliteProfileStore(self.root, "demo", create=True, now=utc_now())
        profile.open()
        profile.close()
        self.path = profile_runtime_path(self.root, "demo")
        self.host = canonical_hash({"host": "synthetic-apply"})
        self.owner = Owner(str(uuid.uuid4()), 4242, self.host)
        self.store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.host,
            require_runtime=True,
        )
        self.batch = str(uuid.uuid4())
        self.job = "boss:job:" + canonical_hash({"job": "apply-e2e"})
        self.store.ensure_batch(self.batch, self._limits(), utc_now())
        self.store.enqueue_jobs(self.batch, (self.job,), utc_now())
        self.gate = SharedDeviceGate(
            profile_id="demo",
            profile_db_path=self.path,
            os_root=self.root / "os-root",
        )
        self.gate.acquire(
            profile_id="demo",
            profile_db_path=str(self.path),
            owner=self.owner,
            now=utc_now(),
        )

    def tearDown(self) -> None:
        try:
            self.gate.release(now=utc_now(), converged=True)
        except Exception:
            pass
        self.store.close()
        self._tmp.cleanup()

    def _limits(self):
        """返回默认开发预算。"""
        from app.domain.runtime import BudgetLimits

        return BudgetLimits()

    def _lease(self):
        """领取并开始职位任务。"""
        lease = self.store.claim_job(self.batch, self.owner, utc_now(), self.job)
        assert lease is not None
        return self.store.start_job(lease, utc_now())

    def _authorization(self, action_key: str, *, confirmed: bool = True, task: str) -> ActionAuthorization:
        """构造绑定当前动作/任务/profile/摘要的合成授权。"""
        digest = canonical_hash({"synthetic": "pack"})
        return ActionAuthorization(
            profile_id="demo",
            base_hash=digest,
            pack_hash=digest,
            acceptance_base_hash=digest,
            acceptance_pack_hash=digest,
            task_key=task,
            action_key=action_key,
            authorized_kinds=("apply_job",),
            expires_at=add_seconds(utc_now(), 300),
            confirmed=confirmed,
            nonce=str(uuid.uuid4()),
        )

    def _quota(self, action_key: str, *, task: str, allowance: int = 1) -> ApplyQuotaContract:
        """构造独立的 apply 配额合同，不使用 daily_sends。"""
        return ApplyQuotaContract(
            profile_id="demo",
            task_key=task,
            action_key=action_key,
            allowance=allowance,
            used=0,
            confirmed=True,
            nonce=str(uuid.uuid4()),
            expires_at=add_seconds(utc_now(), 300),
        )

    def _perform(self, *, observation: dict[str, object], confirmed: bool = True, allowance: int = 1):
        """调用唯一 perform_action，返回结果与命令记录。"""
        lease = self._lease()
        action_key = action_key_for("apply_job", app=APP, message_key=self.job)
        commands: list[str] = []
        ledger = CommandIdentityStore(self.root / "ledger")
        executor = CommandLedgerActionExecutor(
            ScriptedActionExecutor({"observation": observation}), ledger, commands=commands
        )
        result = perform_action(
            store=self.store,
            gate=self.gate,
            authorization=self._authorization(
                action_key, confirmed=confirmed, task=lease.key
            ),
            lease=lease,
            action_kind="apply_job",
            action_key=action_key,
            target_key=self.job,
            conversation_key=self.job,
            body_text=None,
            attachment_name=None,
            identity=None,
            anchor_label=None,
            ime_component=None,
            tap_points=((10, 10),),
            current_base_hash=canonical_hash({"synthetic": "pack"}),
            current_pack_hash=canonical_hash({"synthetic": "pack"}),
            daily_sends=1,
            reply_cooldown_seconds=0,
            profile_db_path=str(self.path),
            executor=executor,
            now=utc_now(),
            apply_quota=self._quota(action_key, task=lease.key, allowance=allowance),
        )
        return result, commands, action_key

    def test_performed_applied_uses_ledger_and_no_retry(self) -> None:
        """点击前授权/前置通过，一次账本命令后 applied；同任务不重放。"""
        observation = helpers.observation(
            apply_available_before=True, apply_completed_after=True
        )
        result, commands, action_key = self._perform(observation=observation)
        self.assertEqual(result["state"], "performed")
        self.assertEqual(result["evidence"]["effect"], "applied")  # type: ignore[index]
        self.assertEqual(len(commands), 1)
        stored = self.store.get_action(action_key)
        assert stored is not None
        self.assertEqual(stored["task_kind"], "job")
        self.assertEqual(stored["effect"], "applied")
        with self.assertRaises(Exception):
            self._perform(observation=observation)

    def test_authorization_rejected_before_any_command(self) -> None:
        """缺授权在 pending/命令前拒绝，命令数为 0。"""
        observation = helpers.observation(
            apply_available_before=True, apply_completed_after=True
        )
        lease = self._lease()
        action_key = action_key_for("apply_job", app=APP, message_key=self.job)
        commands: list[str] = []
        ledger = CommandIdentityStore(self.root / "ledger-reject")
        executor = CommandLedgerActionExecutor(
            ScriptedActionExecutor({"observation": observation}), ledger, commands=commands
        )
        with self.assertRaises(Exception):
            perform_action(
                store=self.store,
                gate=self.gate,
                authorization=self._authorization(
                    action_key, confirmed=False, task=lease.key
                ),
                lease=lease,
                action_kind="apply_job",
                action_key=action_key,
                target_key=self.job,
                conversation_key=self.job,
                body_text=None,
                attachment_name=None,
                identity=None,
                anchor_label=None,
                ime_component=None,
                tap_points=((10, 10),),
                current_base_hash=canonical_hash({"synthetic": "pack"}),
                current_pack_hash=canonical_hash({"synthetic": "pack"}),
                daily_sends=1,
                reply_cooldown_seconds=0,
                profile_db_path=str(self.path),
                executor=executor,
                now=utc_now(),
                apply_quota=self._quota(action_key, task=lease.key),
            )
        self.assertEqual(commands, [])
        self.assertIsNone(self.store.get_action(action_key))

    def test_after_click_disconnect_is_unknown_and_not_replayed(self) -> None:
        """点击后断连记 unknown、任务 blocked/设备隔离，不自动重放。"""
        leased = self._lease()
        action_key = action_key_for("apply_job", app=APP, message_key=self.job)
        commands: list[str] = []
        ledger = CommandIdentityStore(self.root / "ledger-unknown")
        executor = CommandLedgerActionExecutor(
            ScriptedActionExecutor(
                {"raise": {"reason_code": "device_disconnected", "clicked": True}}
            ),
            ledger,
            commands=commands,
        )
        result = perform_action(
            store=self.store,
            gate=self.gate,
            authorization=self._authorization(action_key, task=leased.key),
            lease=leased,
            action_kind="apply_job",
            action_key=action_key,
            target_key=self.job,
            conversation_key=self.job,
            body_text=None,
            attachment_name=None,
            identity=None,
            anchor_label=None,
            ime_component=None,
            tap_points=((10, 10),),
            current_base_hash=canonical_hash({"synthetic": "pack"}),
            current_pack_hash=canonical_hash({"synthetic": "pack"}),
            daily_sends=1,
            reply_cooldown_seconds=0,
            profile_db_path=str(self.path),
            executor=executor,
            now=utc_now(),
            apply_quota=self._quota(action_key, task=leased.key),
        )
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        self.assertIsNotNone(result["card"])
        self.assertEqual(len(commands), 1)
        with self.assertRaises(Exception):
            self.store.claim_job(self.batch, self.owner, utc_now())


    def test_local_complete_failure_keeps_pending_and_blocks_replay(self) -> None:
        """本地提交失败保留 pending；重开库后同任务不能重放。"""
        lease = self._lease()
        action_key = action_key_for("apply_job", app=APP, message_key=self.job)
        commands: list[str] = []
        ledger = CommandIdentityStore(self.root / "ledger-fail")
        executor = CommandLedgerActionExecutor(
            ScriptedActionExecutor(
                {
                    "observation": helpers.observation(
                        apply_available_before=True, apply_completed_after=True
                    )
                }
            ),
            ledger,
            commands=commands,
        )

        def fault(name: str) -> None:
            if name == "action_before_task_update":
                raise RuntimeError("synthetic commit fault")

        failing = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.host,
            require_runtime=True,
            fault_hook=lambda name, connection: fault(name),
        )
        try:
            with self.assertRaises(Exception):
                perform_action(
                    store=failing,
                    gate=self.gate,
                    authorization=self._authorization(action_key, task=lease.key),
                    lease=lease,
                    action_kind="apply_job",
                    action_key=action_key,
                    target_key=self.job,
                    conversation_key=self.job,
                    body_text=None,
                    attachment_name=None,
                    identity=None,
                    anchor_label=None,
                    ime_component=None,
                    tap_points=((10, 10),),
                    current_base_hash=canonical_hash({"synthetic": "pack"}),
                    current_pack_hash=canonical_hash({"synthetic": "pack"}),
                    daily_sends=1,
                    reply_cooldown_seconds=0,
                    profile_db_path=str(self.path),
                    executor=executor,
                    now=utc_now(),
                    apply_quota=self._quota(action_key, task=lease.key),
                )
        finally:
            failing.close()
        stored = self.store.get_action(action_key)
        assert stored is not None
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(len(commands), 1)
        with self.assertRaises(Exception):
            self.store.begin_action(
                lease,
                action_kind="apply_job",
                action_key=action_key,
                target_key=self.job,
                conversation_key=self.job,
                authorization_json="{}",
                daily_sends=1,
                reply_cooldown_seconds=0,
                ime_original=None,
                now=utc_now(),
                apply_allowance=1,
            )

    def test_perform_action_with_adb_executor_raw_xml(self) -> None:
        """控件与前后差分来自真实 XML/合成子进程/真实账本；caller tap 不参与。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor

        before = (
            '<hierarchy><node text="合成岗位甲"><node text="合成公司甲" />'
            '<node text="合成职位甲" />'
            '<node text="立即申请" clickable="true" bounds="[700,2100][1040,2300]" />'
            '</node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成岗位甲"><node text="合成公司甲" />'
            '<node text="合成职位甲" />'
            '<node text="已申请" clickable="false" bounds="[700,2100][1040,2300]" />'
            '</node></hierarchy>'
        )
        with tempfile.TemporaryDirectory(
            dir=helpers.ROOT, prefix=".m6-apply-xml-"
        ) as directory:
            root = Path(directory)
            for index, text in enumerate((before, after)):
                (root / f"phase-{index}.xml").write_text(text, encoding="utf-8")
            script = root / "synthetic_adb.py"
            script.write_text(
                "import pathlib, sys\n"
                "root = pathlib.Path(__file__).resolve().parent\n"
                "args = sys.argv[1:]\n"
                "if 'cat' in args:\n"
                "    counter = root / 'cat-count'\n"
                "    try:\n"
                "        index = int(counter.read_text('utf-8'))\n"
                "    except Exception:\n"
                "        index = 0\n"
                "    counter.write_text(str(index + 1), 'utf-8')\n"
                "    sys.stdout.write((root / ('phase-%d.xml' % min(index, 1)))"
                ".read_text('utf-8'))\n",
                encoding="utf-8",
            )
            ledger_dir = root / "ledger"
            lease = self._lease()
            action_key = action_key_for("apply_job", app=APP, message_key=self.job)
            executor = AdbActionExecutor(
                serial=str(script),
                gate=self.gate,
                adb=sys.executable,
                command_store=CommandIdentityStore(ledger_dir),
                child_scan=lambda pid: [],
            )
            with patch("app.adapters.adb.actions._utc_now", side_effect=utc_now):
                result = perform_action(
                    store=self.store,
                    gate=self.gate,
                    authorization=self._authorization(action_key, task=lease.key),
                    lease=lease,
                    action_kind="apply_job",
                    action_key=action_key,
                    target_key=self.job,
                    conversation_key=self.job,
                    body_text=None,
                    attachment_name=None,
                    identity=ActionIdentity("合成岗位甲", "合成公司甲", "合成职位甲"),
                    anchor_label=None,
                    ime_component=None,
                    tap_points=((1, 1),),
                    current_base_hash=canonical_hash({"synthetic": "pack"}),
                    current_pack_hash=canonical_hash({"synthetic": "pack"}),
                    daily_sends=1,
                    reply_cooldown_seconds=0,
                    profile_db_path=str(self.path),
                    executor=executor,
                    now=utc_now(),
                    apply_quota=self._quota(action_key, task=lease.key),
                )
            ledger = CommandIdentityStore(ledger_dir)
            self.assertNotEqual(ledger.identities(action_key), [])
        self.assertEqual(result["state"], "performed")
        self.assertEqual(result["evidence"]["effect"], "applied")  # type: ignore[index]
        self.assertIsNone(result["card"])
        stored = self.store.get_action(action_key)
        assert stored is not None
        self.assertEqual(stored["effect"], "applied")


if __name__ == "__main__":
    unittest.main()
