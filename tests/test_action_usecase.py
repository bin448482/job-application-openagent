"""验证 M5 生产用例边界：授权精确匹配、unknown 单次调用、零重放与当前证据。"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
import uuid
import json
from dataclasses import replace
from pathlib import Path

from app.adapters.sqlite.profile_store import profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.domain.actions import (
    ActionAuthorization,
    ActionError,
    ActionIdentity,
    ActionObservation,
    ReconciliationObservation,
    action_key_for,
)
from app.domain.models import Evidence, Owner, canonical_hash
from app.ports.actions import ActionExecutionError
from app.ports.device_gate import DeviceSessionIdentity
from app.ports.store import StoreError
from app.process_log import ProcessLog
from app.usecases.actions import perform_action, reconcile_action
from tests import action_helpers as helpers
from tests import m1_helpers

NOW = m1_helpers.BASE_TIME
OWNER = Owner(m1_helpers.OWNER_ID, 4242, m1_helpers.HOST_A)
CONVERSATION = "boss:conv:" + canonical_hash(["合成联系人", "合成公司", "合成职位"])
IDENTITY = ActionIdentity("合成联系人", "合成公司", "合成职位")
MESSAGE = helpers.synthetic_text_key()
TEXT_KEY = f"boss:{MESSAGE}:reply"
DIGEST = canonical_hash({"synthetic": "pack"})


class FakeGate:
    """提供与当前租约一致的共享门禁身份，不读写真实门禁库。"""

    def __init__(self, path: Path) -> None:
        """保存 profile 库路径。"""
        self.path = path
        self.released: list[bool] = []
        self.override_path: Path | None = None
        self.override_owner: Owner | None = None

    def session_identity(self) -> DeviceSessionIdentity:
        """返回 demo/owner/held 的完整可信身份。"""
        return DeviceSessionIdentity(
            profile_id="demo",
            profile_db_path=str(self.override_path or self.path),
            owner=self.override_owner or OWNER,
            nonce=helpers.BEFORE,
            state="held",
        )

    def verify(self, *args: object, **kwargs: object) -> None:
        """始终视为已复核。"""

    def require_available(self) -> None:
        """兼容门禁端口。"""

    def release(self, *, now: str, converged: bool) -> None:
        """记录收敛状态。"""
        self.released.append(converged)


class CountingExecutor:
    """记录执行次数，并按脚本成功、失败或返回观察。"""

    def __init__(self, *, fail: bool = False, observation: dict | None = None) -> None:
        """保存失败标志与观察脚本。"""
        self.calls = 0
        self.fail = fail
        self.observation = observation or helpers.observation()
        self.observed_at = NOW

    def read_current_ime(self, *, before_command=None) -> str:
        """返回合成 IME。"""
        if before_command is not None:
            before_command()
        return "synthetic-ime-default"

    def execute(self, plan: object, *, before_command=None) -> ActionObservation:
        """每次调用计数后返回或抛出。"""
        if before_command is not None:
            before_command()
        self.calls += 1
        if self.fail:
            raise ActionExecutionError("device_disconnected", clicked=True)
        data = dict(self.observation)
        data["observed_at"] = self.observed_at
        data["ime_original"] = "synthetic-ime-default"
        data["ime_current"] = "synthetic-ime-default"
        return ActionObservation(
            action_key=TEXT_KEY, action_kind="send_text", **data
        )


class DriftExecutor(CountingExecutor):
    """在 executor 边界模拟条件漂移并触发每命令 guard。"""

    def __init__(self, snapshot: dict, *, after_return: bool = False) -> None:
        """保存可变快照与漂移时机。"""
        super().__init__()
        self.snapshot = snapshot
        self.after_return = after_return

    def execute(self, plan, *, before_command=None):
        """在执行中或执行后改变当前包摘要。"""
        self.calls += 1
        self.snapshot["pack_hash"] = canonical_hash({"drift": "pack"})
        if self.after_return:
            return ActionObservation(
                action_key=TEXT_KEY,
                action_kind="send_text",
                **helpers.observation(observed_at=helpers.NOW),
            )
        if before_command is not None:
            before_command()
        return ActionObservation(
            action_key=TEXT_KEY,
            action_kind="send_text",
            **helpers.observation(observed_at=helpers.NOW),
        )


class AdvancingExecutor(CountingExecutor):
    """在返回观察前推进可信时钟并触发每命令 guard。"""

    def __init__(self, clock: object, *, seconds: int = 61) -> None:
        """保存可控时钟与推进秒数。"""
        super().__init__()
        self.clock = clock
        self.seconds = seconds

    def execute(self, plan, *, before_command=None):
        """推进墙钟/单调钟后再返回观察。"""
        self.calls += 1
        self.clock.advance(self.seconds)
        if before_command is not None:
            before_command()
        return ActionObservation(
            action_key=TEXT_KEY,
            action_kind="send_text",
            **helpers.observation(observed_at=self.clock.now()),
        )


class ActionUseCaseTests(unittest.TestCase):
    """真实临时 profile 库 + 生产用例，执行器为合成替身。"""

    def setUp(self) -> None:
        """创建 v2 profile 库、仓储、合成候选与运行中租约。"""
        self.tmp = tempfile.TemporaryDirectory(dir=helpers.ROOT, prefix=".m5-usecase-")
        self.root = Path(self.tmp.name)
        from app.adapters.sqlite.profile_store import SqliteProfileStore

        profile = SqliteProfileStore(self.root, "demo", now=NOW)
        profile.open()
        profile.close()
        self.path = profile_runtime_path(self.root, "demo")
        self.clock = m1_helpers.FakeClock()
        self.store = self._open()
        self.gate = FakeGate(self.path)
        self.lease = self._run_task()

    def tearDown(self) -> None:
        """关闭连接并只清理本用例临时根。"""
        self.store.close()
        self.tmp.cleanup()

    def _open(self) -> SqliteStore:
        """打开唯一 profile 仓储。"""
        return SqliteStore(
            self.path,
            profile_id="demo",
            local_host=m1_helpers.HOST_A,
            monotonic=self.clock.monotonic,
            stop_checker=lambda pid: "stopped",
            process_log=m1_helpers.ProcessLog(path=self.root / "usecase.jsonl"),
        )

    def _run_task(self, preview: str = "合成预览"):
        """插入合成候选并 claim/start。"""
        result = m1_helpers.make_result(
            profile_id="demo",
            preview=preview,
            observed_at=self.clock.now(),
            snapshot_id=str(uuid.uuid4()),
        )
        self.store.upsert_snapshot(result)
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        return self.store.start(lease, self.clock.now())

    def _claim_only(self):
        """入队并 claim 但故意不 start，用于验证零设备调用。"""
        result = m1_helpers.make_result(
            profile_id="demo",
            preview="未start",
            observed_at=self.clock.now(),
            snapshot_id=str(uuid.uuid4()),
        )
        self.store.upsert_snapshot(result)
        return self.store.claim("boss", "unread", OWNER, self.clock.now())

    def _authorization(
        self,
        *,
        action_key: str = TEXT_KEY,
        kinds: tuple[str, ...] = ("send_text",),
        expires_at: str | None = None,
        confirmed: bool = True,
    ) -> ActionAuthorization:
        """构造与当前 lease/摘要绑定的本轮分动作授权。"""
        return ActionAuthorization(
            profile_id="demo",
            base_hash=DIGEST,
            pack_hash=DIGEST,
            acceptance_base_hash=DIGEST,
            acceptance_pack_hash=DIGEST,
            task_key=self.lease.key,
            action_key=action_key,
            authorized_kinds=kinds,
            expires_at=expires_at or helpers.authorization_expiry(self.clock.now()),
            confirmed=confirmed,
            nonce=helpers.AFTER,
        )

    def _perform(
        self,
        executor: CountingExecutor,
        authorization=None,
        *,
        action_key: str = TEXT_KEY,
        target_key: str = MESSAGE,
        reload_snapshot=None,
        ime_component: str | None = None,
        process_log: ProcessLog | None = None,
        identity: ActionIdentity | None = IDENTITY,
    ):
        """调用生产 perform 用例。"""
        return perform_action(
            store=self.store,
            gate=self.gate,
            authorization=authorization or self._authorization(),
            lease=self.lease,
            action_kind="send_text",
            action_key=action_key,
            target_key=target_key,
            conversation_key=CONVERSATION,
            body_text="合成回复正文",
            attachment_name=None,
            identity=identity,
            anchor_label=None,
            ime_component=ime_component,
            tap_points=((10, 20),),
            current_base_hash=DIGEST,
            current_pack_hash=DIGEST,
            daily_sends=10,
            reply_cooldown_seconds=0,
            profile_db_path=str(self.path),
            executor=executor,
            now=self.clock.now(),
            clock=lambda: self.clock.now(),
            reload_snapshot=reload_snapshot,
            process_log=process_log,
        )

    def test_action_key_target_and_actual_store_binding_are_enforced(self) -> None:
        """动作键与消息目标错配、或实际 store 路径不符时零调用。"""
        other_message = "boss:msg:" + canonical_hash({"other": "message"})
        executor = CountingExecutor()
        with self.assertRaises(ActionError):
            self._perform(
                executor,
                action_key=TEXT_KEY,
                target_key=other_message,
            )
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.store.pending_actions(), [])
        self.gate.override_path = self.path.with_name("other.sqlite3")
        with self.assertRaises(ActionError):
            self._perform(executor)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.store.pending_actions(), [])

    def test_missing_target_identity_is_rejected_before_pending(self) -> None:
        """公共入口缺少完整目标身份时零执行、不写 pending。"""
        executor = CountingExecutor()
        with self.assertRaises(ActionError) as caught:
            self._perform(executor, identity=None)
        self.assertEqual(caught.exception.reason_code, "precondition_failed")
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.store.pending_actions(), [])

    def test_conflicting_identity_is_rejected_before_pending(self) -> None:
        """完整但与持久任务会话冲突的职位不得准入或调用设备。"""
        from unittest.mock import patch

        executor = CountingExecutor()
        conflicting = ActionIdentity("合成联系人", "合成公司", "其他职位")
        with patch("subprocess.run") as run:
            with self.assertRaises(ActionError) as caught:
                self._perform(executor, identity=conflicting)
        self.assertEqual(caught.exception.reason_code, "precondition_failed")
        self.assertEqual(executor.calls, 0)
        run.assert_not_called()
        self.assertEqual(self.store.pending_actions(), [])

    def test_public_path_rejects_wrong_job_before_irreversible_tap(self) -> None:
        """公共 perform + 真实 ADB 适配器：目标 XML 职位与绑定身份不符时 tap0。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor

        wrong_job = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="其他职位" /><node text="消息" /></node></hierarchy>'
        )
        calls: list[list[str]] = []

        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            """记录命令并按 cat 返回合成层级。"""
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=wrong_job, stderr=""
            )

        executor = AdbActionExecutor(serial="<serial>", gate=self.gate)
        with (
            patch("subprocess.run", side_effect=run),
            patch("app.adapters.adb.actions._utc_now", return_value=self.clock.now()),
        ):
            result = perform_action(
                store=self.store,
                gate=self.gate,
                authorization=self._authorization(),
                lease=self.lease,
                action_kind="send_text",
                action_key=TEXT_KEY,
                target_key=MESSAGE,
                conversation_key=CONVERSATION,
                body_text="合成回复正文",
                attachment_name=None,
                identity=IDENTITY,
                anchor_label="消息",
                ime_component=None,
                tap_points=((10, 20),),
                current_base_hash=DIGEST,
                current_pack_hash=DIGEST,
                daily_sends=10,
                reply_cooldown_seconds=0,
                profile_db_path=str(self.path),
                executor=executor,
                now=self.clock.now(),
                clock=lambda: self.clock.now(),
            )
        self.assertEqual([c for c in calls if "input" in c or "tap" in c], [])
        self.assertIn(result["state"], {"not_performed", "unknown", "pending"})
        self.assertNotEqual(result["evidence"]["outcome"], "performed")

    def test_public_path_missing_identity_rejects_before_subprocess(self) -> None:
        """公共 perform 缺完整身份时不启动任何设备子进程。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor

        executor = AdbActionExecutor(serial="<serial>", gate=self.gate)
        with patch("subprocess.run") as run:
            with self.assertRaises(ActionError) as caught:
                perform_action(
                    store=self.store,
                    gate=self.gate,
                    authorization=self._authorization(),
                    lease=self.lease,
                    action_kind="send_text",
                    action_key=TEXT_KEY,
                    target_key=MESSAGE,
                    conversation_key=CONVERSATION,
                    body_text="合成回复正文",
                    attachment_name=None,
                    identity=None,
                    anchor_label="消息",
                    ime_component=None,
                    tap_points=((10, 20),),
                    current_base_hash=DIGEST,
                    current_pack_hash=DIGEST,
                    daily_sends=10,
                    reply_cooldown_seconds=0,
                    profile_db_path=str(self.path),
                    executor=executor,
                    now=self.clock.now(),
                    clock=lambda: self.clock.now(),
                )
        self.assertEqual(caught.exception.reason_code, "precondition_failed")
        run.assert_not_called()

    def test_public_path_full_identity_performs_through_real_adapter(self) -> None:
        """公共 perform + 真实 ADB 适配器：完整身份正常执行为 performed/tap1。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor

        before = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" /></node></hierarchy>'
        )
        readback = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" />'
            '<node class="android.widget.EditText" text="合成回复正文" />'
            '</node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" />'
            '<node text="合成回复正文" />'
            '<node class="android.widget.EditText" text="" /></node></hierarchy>'
        )
        state = {"cat": 0}
        calls: list[list[str]] = []

        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            """按命令类型返回合成层级，cat 依次给出 before/readback/after。"""
            calls.append(list(args))
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                out = (before, readback, after)[min(index, 2)]
            else:
                out = ""
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=out, stderr=""
            )

        executor = AdbActionExecutor(serial="<serial>", gate=self.gate)
        with (
            patch("subprocess.run", side_effect=run),
            patch("app.adapters.adb.actions._utc_now", return_value=self.clock.now()),
        ):
            result = perform_action(
                store=self.store,
                gate=self.gate,
                authorization=self._authorization(),
                lease=self.lease,
                action_kind="send_text",
                action_key=TEXT_KEY,
                target_key=MESSAGE,
                conversation_key=CONVERSATION,
                body_text="合成回复正文",
                attachment_name=None,
                identity=IDENTITY,
                anchor_label="消息",
                ime_component=None,
                tap_points=((10, 20),),
                current_base_hash=DIGEST,
                current_pack_hash=DIGEST,
                daily_sends=10,
                reply_cooldown_seconds=0,
                profile_db_path=str(self.path),
                executor=executor,
                now=self.clock.now(),
                clock=lambda: self.clock.now(),
            )
        self.assertEqual(result["state"], "performed")
        self.assertEqual(result["evidence"]["effect"], "sent")
        self.assertEqual(len([c for c in calls if "tap" in c]), 1)

    def test_identity_must_match_persisted_task_conversation(self) -> None:
        """身份与调用方会话键一致但持久任务不同，仍须在 pending 前拒绝。"""
        executor = CountingExecutor()
        other = ActionIdentity("另一人", "另一公司", "另一职位")
        with self.assertRaises(ActionError) as caught:
            perform_action(
                store=self.store,
                gate=self.gate,
                authorization=self._authorization(),
                lease=self.lease,
                action_kind="send_text",
                action_key=TEXT_KEY,
                target_key=MESSAGE,
                conversation_key=other.conversation_key(),
                body_text="合成回复正文",
                attachment_name=None,
                identity=other,
                anchor_label=None,
                ime_component=None,
                tap_points=((10, 20),),
                current_base_hash=DIGEST,
                current_pack_hash=DIGEST,
                daily_sends=10,
                reply_cooldown_seconds=0,
                profile_db_path=str(self.path),
                executor=executor,
                now=self.clock.now(),
                clock=lambda: self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "precondition_failed")
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.store.pending_actions(), [])

    def test_public_entry_binds_action_before_first_ime_command(self) -> None:
        """公开入口在首次读取原 IME 前绑定动作上下文并记录命令族。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor
        from app.adapters.adb.command import CommandIdentityStore

        ime = "com.example.default/.Ime"
        tree = {7000: [7999], 7999: []}

        class Proc:
            """最小 Popen 替身，返回固定 IME。"""

            def __init__(self, pid: int) -> None:
                """保存 PID。"""
                self.pid = pid
                self.returncode = 0

            def communicate(self, timeout: float | None = None):
                """返回 IME 文本。"""
                return ime, ""

            def kill(self) -> None:
                """无 kill。"""

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        with tempfile.TemporaryDirectory(
            dir=helpers.ROOT, prefix=".m5-ime-bind-"
        ) as directory:
            identity_store = CommandIdentityStore(Path(directory))
            executor = AdbActionExecutor(
                serial="<serial>",
                gate=self.gate,
                command_store=identity_store,
                child_scan=lambda pid: list(tree.get(pid, [])),
            )
            observation = ActionObservation(
                action_key=TEXT_KEY,
                action_kind="send_text",
                **helpers.observation(
                    observed_at=self.clock.now(),
                    ime_original=ime,
                    ime_current=ime,
                ),
            )
            with patch("subprocess.Popen", side_effect=lambda *a, **k: Proc(7000)), patch.object(
                executor, "execute", return_value=observation
            ):
                result = self._perform(
                    executor, ime_component="com.example/.Ime"
                )
            self.assertEqual(result["state"], "performed")
            self.assertIn(7000, identity_store.identities(TEXT_KEY))
            self.assertIn(7999, identity_store.identities(TEXT_KEY))
            self.assertNotEqual(
                identity_store.check(
                    TEXT_KEY,
                    self.lease.owner.pid,
                    lambda pid: "alive" if pid == 7999 else "stopped",
                ),
                "stopped",
            )

    def test_task_conversation_read_error_is_logged_and_safe(self) -> None:
        """真实任务身份 SELECT 读错误在 validate 阶段安全闭合日志且不降级。"""
        from unittest.mock import patch

        executor = CountingExecutor()
        log = ProcessLog(path=self.root / "validate-fault.jsonl")
        with patch.object(
            self.store,
            "task_conversation",
            side_effect=StoreError("store_io_error"),
        ):
            with self.assertRaises(StoreError) as caught:
                self._perform(executor, process_log=log)
        self.assertEqual(caught.exception.reason_code, "store_io_error")
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.store.pending_actions(), [])
        records = [
            json.loads(line)
            for line in log.path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        validate = [
            item for item in records if item["operation"] == "action_validate"
        ]
        self.assertEqual([item["result"] for item in validate], ["started", "failed"])
        self.assertEqual(validate[-1]["reason_code"], "store_io_error")
        self.assertEqual(validate[-1]["error_category"], "store")
        self.assertFalse(log.degraded)

    def test_command_family_unknown_keeps_public_scene_unconverged(self) -> None:
        """账本未证明命令全停时保留已证效果，但不报 gate_converged/不释放。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor
        from app.adapters.adb.command import CommandIdentityStore

        ime = "com.example.default/.Ime"
        body = "合成回复正文"
        before = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" /></node></hierarchy>'
        )
        readback = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" />'
            '<node class="android.widget.EditText" text="合成回复正文" />'
            '</node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" />'
            '<node text="合成回复正文" />'
            '<node class="android.widget.EditText" text="" /></node></hierarchy>'
        )
        state = {"cat": 0, "ime": ime}

        class Proc:
            """合成 Popen：按参数返回 IME 或 before/readback/after。"""

            def __init__(self, args: list[str]) -> None:
                """保存命令参数与成功返回码。"""
                self.args = args
                self.pid = 9000
                self.returncode = 0

            def communicate(self, timeout: float | None = None):
                """返回合成输出。"""
                if "ime" in self.args and "set" in self.args:
                    state["ime"] = self.args[-1]
                    return "", ""
                if "settings" in self.args:
                    return state["ime"], ""
                if "cat" in self.args:
                    index = state["cat"]
                    state["cat"] = index + 1
                    return (before, readback, after)[min(index, 2)], ""
                return "", ""

            def kill(self) -> None:
                """无 kill。"""

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        with tempfile.TemporaryDirectory(
            dir=helpers.ROOT, prefix=".m5-prop-"
        ) as directory:
            executor = AdbActionExecutor(
                serial="<serial>",
                gate=self.gate,
                command_store=CommandIdentityStore(Path(directory)),
                child_scan=lambda pid: [],
            )
            with (
                patch(
                    "subprocess.Popen",
                    side_effect=lambda args, **kwargs: Proc(args),
                ),
                patch(
                    "app.adapters.adb.actions._utc_now",
                    return_value=self.clock.now(),
                ),
            ):
                result = self._perform(
                    executor,
                    ime_component="com.example/.Ime",
                )
        self.assertEqual(result["evidence"]["outcome"], "performed")
        self.assertEqual(result["evidence"]["effect"], "sent")
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        self.assertIsNotNone(result["card"])

    def test_finish_write_failure_marks_family_unproven(self) -> None:
        """平台已证明但完成写入失败时必须让现场保持未收敛。"""
        from app.adapters.adb.actions import AdbActionExecutor
        from app.adapters.adb.command import CommandIdentityStore

        with tempfile.TemporaryDirectory(
            dir=helpers.ROOT, prefix=".m5-finish-fail-"
        ) as directory:
            store = CommandIdentityStore(Path(directory))
            executor = AdbActionExecutor(
                serial="<serial>", gate=self.gate, command_store=store
            )
            executor.bind_action(TEXT_KEY)
            store.finish_family = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
                OSError("synthetic")
            )
            with self.assertRaises(OSError):
                executor._record_command_end(9000, True, True)
            self.assertTrue(executor._family_unproven)

    def test_empty_ime_early_exit_keeps_scene_unconverged(self) -> None:
        """首次 IME 返回空且账本 unknown 时不得硬编码 quiet=True。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor
        from app.adapters.adb.command import CommandIdentityStore

        class Proc:
            """所有命令返回空输出。"""

            def __init__(self, args: list[str]) -> None:
                """保存参数与 PID。"""
                self.args = args
                self.pid = 8000
                self.returncode = 0

            def communicate(self, timeout: float | None = None):
                """返回空。"""
                return "", ""

            def kill(self) -> None:
                """无 kill。"""

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        with tempfile.TemporaryDirectory(
            dir=helpers.ROOT, prefix=".m5-empty-ime-"
        ) as directory:
            executor = AdbActionExecutor(
                serial="<serial>",
                gate=self.gate,
                command_store=CommandIdentityStore(Path(directory)),
                child_scan=lambda pid: [],
            )
            with patch(
                "subprocess.Popen",
                side_effect=lambda args, **kwargs: Proc(args),
            ):
                result = self._perform(
                    executor, ime_component="com.example/.Ime"
                )
        self.assertEqual(result["evidence"]["outcome"], "not_performed")
        self.assertEqual(result["state"], "pending")
        self.assertFalse(result["gate_converged"])
        self.assertIsNotNone(result["card"])

    def test_complete_write_failure_emits_safe_failed_log_and_isolates(self) -> None:
        """一次账本 complete 写失败留下固定 failed 终态且公开保持未收敛。"""
        from unittest.mock import patch

        from app.adapters.adb.actions import AdbActionExecutor
        from app.adapters.adb.command import CommandIdentityStore, ProcessFamilyBound

        ime = "com.example.default/.Ime"
        before = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" /></node></hierarchy>'
        )
        readback = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" />'
            '<node class="android.widget.EditText" text="合成回复正文" />'
            '</node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成联系人"><node text="合成公司" />'
            '<node text="合成职位" /><node text="消息" />'
            '<node text="合成回复正文" />'
            '<node class="android.widget.EditText" text="" /></node></hierarchy>'
        )
        state = {"cat": 0, "ime": ime}

        class Proc:
            """合成 Popen，按参数返回 IME 或 before/readback/after。"""

            def __init__(self, args: list[str]) -> None:
                """保存参数与 PID。"""
                self.args = args
                self.pid = 9100
                self.returncode = 0

            def communicate(self, timeout: float | None = None):
                """返回合成输出。"""
                if "ime" in self.args and "set" in self.args:
                    state["ime"] = self.args[-1]
                    return "", ""
                if "settings" in self.args:
                    return state["ime"], ""
                if "cat" in self.args:
                    index = state["cat"]
                    state["cat"] = index + 1
                    return (before, readback, after)[min(index, 2)], ""
                return "", ""

            def kill(self) -> None:
                """无 kill。"""

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        class Bound:
            """成功约束并证明空族的 Job 替身。"""

            def assign(self, pid: int) -> bool:
                """成功加入。"""
                return True

            def resume(self, pid: int) -> bool:
                """成功恢复。"""
                return True

            def terminate(self) -> bool:
                """成功终止。"""
                return True

            def active(self) -> int:
                """空族。"""
                return 0

            def close(self) -> None:
                """无关闭。"""

        with tempfile.TemporaryDirectory(
            dir=helpers.ROOT, prefix=".m5-logtrace-"
        ) as directory:
            store = CommandIdentityStore(Path(directory))
            original_append = store._append
            hits = {"count": 0}

            def flaky_append(action_key: str, item: dict[str, object]) -> None:
                """只在首次 complete 追加注入一次真实 OSError。"""
                if item.get("complete") is True and hits["count"] == 0:
                    hits["count"] += 1
                    raise OSError("synthetic ledger failure")
                original_append(action_key, item)

            store._append = flaky_append  # type: ignore[method-assign]
            log = ProcessLog(path=self.root / "finish-fault.jsonl")
            executor = AdbActionExecutor(
                serial="<serial>",
                gate=self.gate,
                process_log=log,
                command_store=store,
                child_scan=lambda pid: [],
            )
            with (
                patch.object(
                    ProcessFamilyBound, "create", return_value=Bound()
                ),
                patch(
                    "app.adapters.adb.command._is_real_process",
                    return_value=True,
                ),
                patch(
                    "subprocess.Popen",
                    side_effect=lambda args, **kwargs: Proc(args),
                ),
                patch(
                    "app.adapters.adb.actions._utc_now",
                    return_value=self.clock.now(),
                ),
            ):
                result = self._perform(
                    executor, ime_component="com.example/.Ime"
                )
        self.assertEqual(hits["count"], 1)
        self.assertEqual(result["evidence"]["outcome"], "performed")
        self.assertEqual(result["evidence"]["effect"], "sent")
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        self.assertIsNotNone(result["card"])
        self.assertEqual(self.gate.released, [])
        record = self.store.get_action(TEXT_KEY)
        self.assertIsNotNone(record)
        self.assertEqual(record["state"], "unknown")
        records = [
            json.loads(line)
            for line in log.path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        finish = [
            item for item in records if item["operation"] == "action_finish_command"
        ]
        results = [item["result"] for item in finish]
        self.assertEqual(results.count("failed"), 1)
        self.assertEqual(results.count("started"), results.count("ok") + 1)
        failed = [item for item in finish if item["result"] == "failed"][0]
        self.assertEqual(failed["reason_code"], "store_io_error")
        self.assertEqual(failed["error_category"], "store")
        self.assertEqual(
            len([item for item in records if item["result"] == "failed"]), 1
        )
        self.assertFalse(log.degraded)
        raw = log.path.read_text("utf-8")
        self.assertNotIn("synthetic ledger failure", raw)
        self.assertNotIn("OSError", raw)

    def test_leased_without_start_has_zero_device_calls(self) -> None:
        """leased 未 start 时，即使需要 IME 也不读设备、不写 pending。"""
        other_tmp = tempfile.TemporaryDirectory(dir=helpers.ROOT, prefix=".m5-leased-")
        other_root = Path(other_tmp.name)
        try:
            from app.adapters.sqlite.profile_store import SqliteProfileStore

            profile = SqliteProfileStore(other_root, "demo", now=NOW)
            profile.open()
            profile.close()
            other_path = profile_runtime_path(other_root, "demo")
            other_store = SqliteStore(
                other_path,
                profile_id="demo",
                local_host=m1_helpers.HOST_A,
                monotonic=self.clock.monotonic,
                stop_checker=lambda pid: "stopped",
                process_log=ProcessLog(path=other_root / "leased.jsonl"),
            )
            try:
                result = m1_helpers.make_result(
                    profile_id="demo",
                    observed_at=self.clock.now(),
                    snapshot_id=str(uuid.uuid4()),
                )
                other_store.upsert_snapshot(result)
                claimed = other_store.claim(
                    "boss", "unread", OWNER, self.clock.now()
                )
                executor = CountingExecutor()
                auth = ActionAuthorization(
                    profile_id="demo",
                    base_hash=DIGEST,
                    pack_hash=DIGEST,
                    acceptance_base_hash=DIGEST,
                    acceptance_pack_hash=DIGEST,
                    task_key=claimed.key,
                    action_key=TEXT_KEY,
                    authorized_kinds=("send_text",),
                    expires_at=helpers.authorization_expiry(self.clock.now()),
                    confirmed=True,
                    nonce=helpers.AFTER,
                )
                with self.assertRaises(ActionError):
                    perform_action(
                        store=other_store,
                        gate=FakeGate(other_path),
                        authorization=auth,
                        lease=claimed,
                        action_kind="send_text",
                        action_key=TEXT_KEY,
                        target_key=MESSAGE,
                        conversation_key=CONVERSATION,
                        body_text="合成回复正文",
                        attachment_name=None,
                        identity=IDENTITY,
                        anchor_label=None,
                        ime_component="com.example/.Ime",
                        tap_points=((10, 20),),
                        current_base_hash=DIGEST,
                        current_pack_hash=DIGEST,
                        daily_sends=10,
                        reply_cooldown_seconds=0,
                        profile_db_path=str(other_path),
                        executor=executor,
                        now=self.clock.now(),
                        clock=lambda: self.clock.now(),
                    )
                self.assertEqual(executor.calls, 0)
                self.assertIsNone(other_store.get_action(TEXT_KEY))
            finally:
                other_store.close()
        finally:
            other_tmp.cleanup()

    def test_execution_drift_after_start_becomes_unknown_without_retry(self) -> None:
        """执行中当前包变化时不再发命令，保留 unknown 未决且只一次尝试。"""
        snapshot = {
            "base_hash": DIGEST,
            "pack_hash": DIGEST,
            "acceptance_base_hash": DIGEST,
            "acceptance_pack_hash": DIGEST,
        }
        executor = DriftExecutor(snapshot)
        result = self._perform(
            executor, reload_snapshot=lambda: dict(snapshot)
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")

    def test_drift_after_execution_before_commit_is_unknown(self) -> None:
        """执行完成但提交前条件漂移时收敛 unknown，不提交 performed。"""
        snapshot = {
            "base_hash": DIGEST,
            "pack_hash": DIGEST,
            "acceptance_base_hash": DIGEST,
            "acceptance_pack_hash": DIGEST,
        }
        executor = DriftExecutor(snapshot, after_return=True)
        result = self._perform(
            executor, reload_snapshot=lambda: dict(snapshot)
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["evidence"]["outcome"], "unknown")
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")

    def test_result_evidence_has_exact_seven_fields(self) -> None:
        """公开结果中的 evidence 是完整七字段，不是六个顶层同名。"""
        result = self._perform(CountingExecutor())
        self.assertEqual(
            set(result["evidence"]),
            {
                "action_key",
                "outcome",
                "effect",
                "reason_code",
                "before_snapshot_id",
                "after_snapshot_id",
                "observed_at",
            },
        )

    def test_bad_observation_is_unknown_and_keeps_pending_lock(self) -> None:
        """坏观察（非 bool clicked）在执行后收敛 unknown，不抛未处理异常。"""
        class BadExecutor(CountingExecutor):
            """返回非法 clicked 的公开 typed 端口观察。"""

            def execute(self, plan, *, before_command=None):
                """返回 clicked=1 的坏观察。"""
                self.calls += 1
                data = helpers.observation(observed_at=helpers.NOW)
                data["clicked"] = 1
                return ActionObservation(
                    action_key=TEXT_KEY, action_kind="send_text", **data
                )

        executor = BadExecutor()
        result = self._perform(executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["evidence"]["reason_code"], "executor_interrupted")
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")
        self.assertIsNotNone(result["card"])

    def test_reconcile_requires_persisted_ime_and_stopped_commands(self) -> None:
        """恢复必须核持久化原 IME 与原父子命令停止，二者不足均拒绝。"""
        executor = CountingExecutor(fail=True)
        result = self._perform(
            executor, ime_component="com.example.synthetic/.Ime"
        )
        self.assertEqual(result["state"], "unknown")
        key = result["action_key"]
        state = self.store.load_ime_state()
        self.assertEqual(state["original_ime"], "synthetic-ime-default")
        evidence = Evidence(
            action_key=key,
            outcome="performed",
            effect="sent",
            reason_code=None,
            before_snapshot_id=helpers.BEFORE,
            after_snapshot_id=helpers.AFTER,
            observed_at=self.clock.now(),
        )
        observation = ReconciliationObservation(
            action_key=key,
            resolution="confirm_performed",
            observed_at=self.clock.now(),
            evidence=evidence,
            command_quiet=True,
            scene_settled=True,
            confirmation_open=False,
            ime_original="different-ime",
            ime_current="different-ime",
            matched_action_key=key,
            confirmed=True,
        )
        with self.assertRaises(ActionError) as caught:
            reconcile_action(
                store=self.store,
                gate=self.gate,
                observation=observation,
                profile_id="demo",
                profile_db_path=str(self.path),
                explicit_confirm=True,
                command_stop_checker=lambda pid: "stopped",
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "scene_unsettled")
        self.assertEqual(self.store.get_action(key)["state"], "unknown")

        good = replace(
            observation,
            ime_original="synthetic-ime-default",
            ime_current="synthetic-ime-default",
        )
        with self.assertRaises(ActionError) as caught:
            reconcile_action(
                store=self.store,
                gate=self.gate,
                observation=good,
                profile_id="demo",
                profile_db_path=str(self.path),
                explicit_confirm=True,
                command_stop_checker=lambda pid: "alive",
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "recovery_not_confirmed")
        self.assertEqual(self.store.get_action(key)["state"], "unknown")

    def test_usecase_logs_paired_safe_action_stages(self) -> None:
        """显式传入的 ProcessLog 记录 validate/admit/execute/derive/commit 阶段。"""
        log = ProcessLog(path=self.root / "usecase-action.jsonl")
        result = self._perform(CountingExecutor(), process_log=log)
        self.assertEqual(result["state"], "performed")
        records = [
            json.loads(line)
            for line in log.path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        action_records = [
            item for item in records if item["stage"] == "action"
        ]
        operations = {item["operation"] for item in action_records}
        self.assertTrue(
            {
                "action_validate",
                "action_admit",
                "action_execute",
                "action_derive",
                "action_commit",
            }
            <= operations
        )
        for operation in (
            "action_validate",
            "action_admit",
            "action_execute",
            "action_derive",
            "action_commit",
        ):
            results = [
                item["result"]
                for item in action_records
                if item["operation"] == operation
            ]
            self.assertEqual(results.count("started"), 1, operation)
            self.assertEqual(results[0], "started")
            self.assertEqual(results[-1], "ok")
            self.assertEqual(
                len([r for r in results if r in {"ok", "failed", "undetermined"}]),
                1,
                operation,
            )
        joined = log.path.read_text("utf-8")
        self.assertNotIn("合成回复正文", joined)
        self.assertNotIn(str(self.path), joined)

    def test_default_clock_is_dynamic_and_expires_before_next_command(self) -> None:
        """合法省略 clock 的公开调用使用真实当前时间，截止后下一命令为0。"""
        from unittest.mock import patch

        executor = AdvancingExecutor(self.clock)
        with patch("app.usecases.actions.utc_now", self.clock.now):
            result = perform_action(
                store=self.store,
                gate=self.gate,
                authorization=self._authorization(),
                lease=self.lease,
                action_kind="send_text",
                action_key=TEXT_KEY,
                target_key=MESSAGE,
                conversation_key=CONVERSATION,
                body_text="合成回复正文",
                attachment_name=None,
                identity=IDENTITY,
                anchor_label=None,
                ime_component=None,
                tap_points=((10, 20),),
                current_base_hash=DIGEST,
                current_pack_hash=DIGEST,
                daily_sends=10,
                reply_cooldown_seconds=0,
                profile_db_path=str(self.path),
                executor=executor,
                now=self.clock.now(),
            )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")
        task = m1_helpers.fetch_one(
            self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (self.lease.key,)
        )
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(
            m1_helpers.fetch_one(self.path, "SELECT reservation_state FROM device_lease"),
            {"reservation_state": "quarantined"},
        )

    def test_acceptance_drift_is_rejected_before_next_command(self) -> None:
        """只漂移当前验收摘要也必须拒绝，不拿包摘要代替验收。"""
        snapshot = {
            "base_hash": DIGEST,
            "pack_hash": DIGEST,
            "acceptance_base_hash": DIGEST,
            "acceptance_pack_hash": DIGEST,
        }

        class AcceptanceDrift(CountingExecutor):
            """在执行开始时只改变验收 pack 摘要。"""

            def execute(self, plan, *, before_command=None):
                """改变验收摘要后触发 guard。"""
                self.calls += 1
                snapshot["acceptance_pack_hash"] = canonical_hash(
                    {"drift": "acceptance"}
                )
                if before_command is not None:
                    before_command()
                return ActionObservation(
                    action_key=TEXT_KEY,
                    action_kind="send_text",
                    **helpers.observation(observed_at=helpers.NOW),
                )

        executor = AcceptanceDrift()
        result = self._perform(
            executor, reload_snapshot=lambda: dict(snapshot)
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")

    def test_shared_identity_drift_after_execution_freezes_unknown(self) -> None:
        """执行后共享 owner 变化统一冻结 unknown/blocked，不丢锁。"""
        executor = CountingExecutor()
        original_execute = executor.execute

        def execute(plan, *, before_command=None):
            """执行一次后改变门禁 owner。"""
            result = original_execute(plan, before_command=before_command)
            self.gate.override_owner = Owner(
                m1_helpers.OWNER_ID_2, 4243, m1_helpers.HOST_A
            )
            return result

        executor.execute = execute  # type: ignore[method-assign]
        result = self._perform(executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (self.lease.key,)
            )["state"],
            "blocked",
        )

    def test_completion_ime_binding_rejects_missing_or_wrong_original(self) -> None:
        """完成观察原 IME 缺失/不同值不收敛、不假写 restored。"""
        for ime_original, ime_current in ((None, None), ("other-ime", "other-ime")):
            with self.subTest(original=ime_original):
                if self.store.get_action(TEXT_KEY) is not None:
                    self.store.close()
                    self.tmp.cleanup()
                    self.setUp()
                class WrongIme(CountingExecutor):
                    """返回未绑定持久原值的 IME 观察。"""

                    def execute(self, plan, *, before_command=None):
                        """返回错误 IME 的 performed 观察。"""
                        self.calls += 1
                        data = helpers.observation(
                            observed_at=self_clock_now(),
                            ime_original=ime_original,
                            ime_current=ime_current,
                        )
                        return ActionObservation(
                            action_key=TEXT_KEY,
                            action_kind="send_text",
                            **data,
                        )

                def self_clock_now() -> str:
                    """返回当前测试假时钟时间。"""
                    return self.clock.now()

                executor = WrongIme()
                result = self._perform(
                    executor, ime_component="com.example.synthetic/.Ime"
                )
                self.assertEqual(result["state"], "unknown")
                state = self.store.load_ime_state()
                self.assertFalse(state["restored"])

    def test_matching_ime_completion_marks_restored(self) -> None:
        """正常完成的观察原 IME 与持久值一致时收敛 performed 并更新账本。"""
        executor = CountingExecutor()
        result = self._perform(
            executor, ime_component="com.example.synthetic/.Ime"
        )
        self.assertEqual(result["state"], "performed")
        self.assertTrue(self.store.load_ime_state()["restored"])

    def test_missing_acceptance_fields_are_rejected(self) -> None:
        """当前验收四字段缺失/None 不得用包摘要补成验收成功。"""
        snapshot = {
            "base_hash": DIGEST,
            "pack_hash": DIGEST,
            "acceptance_base_hash": DIGEST,
            "acceptance_pack_hash": DIGEST,
        }

        class MissingAcceptance(CountingExecutor):
            """执行开始时删除验收字段。"""

            def execute(self, plan, *, before_command=None):
                """删除验收 base/pack 后触发 guard。"""
                self.calls += 1
                snapshot.pop("acceptance_base_hash", None)
                snapshot.pop("acceptance_pack_hash", None)
                if before_command is not None:
                    before_command()
                return ActionObservation(
                    action_key=TEXT_KEY,
                    action_kind="send_text",
                    **helpers.observation(observed_at=helpers.NOW),
                )

        executor = MissingAcceptance()
        result = self._perform(
            executor, reload_snapshot=lambda: dict(snapshot)
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])

    def test_unknown_is_single_call_and_not_replayed(self) -> None:
        """执行失败只调用一次；再次同键在同任务上被 action_conflict 拒绝。"""
        executor = CountingExecutor(fail=True)
        result = self._perform(executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        with self.assertRaises((StoreError, ActionError)):
            self._perform(executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT COUNT(*) AS total FROM action_records"
            ),
            {"total": 1},
        )

    def test_authorization_exact_target_and_action(self) -> None:
        """错误动作键/种类或未确认授权在 executor 前拒绝。"""
        executor = CountingExecutor()
        other_key = f"{CONVERSATION}:resume"
        with self.assertRaises(ActionError):
            self._perform(
                executor,
                authorization=self._authorization(action_key=other_key),
            )
        with self.assertRaises(ActionError):
            self._perform(
                executor,
                authorization=self._authorization(kinds=("send_resume",)),
            )
        with self.assertRaises(ActionError):
            self._perform(
                executor,
                authorization=self._authorization(confirmed=False),
            )
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.store.pending_actions(), [])

    def test_success_does_not_touch_facts_and_blocks_replay(self) -> None:
        """performed 不改写事实，且同一动作键不允许新任务重放。"""
        executor = CountingExecutor()
        result = self._perform(executor)
        self.assertEqual(result["state"], "performed")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT COUNT(*) AS total FROM profile_facts"
            ),
            {"total": 0},
        )
        lease2 = self._run_task(preview="合成预览二")
        with self.assertRaises(StoreError) as caught:
            perform_action(
                store=self.store,
                gate=self.gate,
                authorization=ActionAuthorization(
                    profile_id="demo",
                    base_hash=DIGEST,
                    pack_hash=DIGEST,
                    acceptance_base_hash=DIGEST,
                    acceptance_pack_hash=DIGEST,
                    task_key=lease2.key,
                    action_key=TEXT_KEY,
                    authorized_kinds=("send_text",),
                    expires_at=helpers.authorization_expiry(self.clock.now()),
                    confirmed=True,
                    nonce=helpers.BEFORE,
                ),
                lease=lease2,
                action_kind="send_text",
                action_key=TEXT_KEY,
                target_key=MESSAGE,
                conversation_key=CONVERSATION,
                body_text="合成回复正文",
                attachment_name=None,
                identity=IDENTITY,
                anchor_label=None,
                ime_component=None,
                tap_points=((10, 20),),
                current_base_hash=DIGEST,
                current_pack_hash=DIGEST,
                daily_sends=10,
                reply_cooldown_seconds=0,
                profile_db_path=str(self.path),
                executor=CountingExecutor(),
                now=self.clock.now(),
                clock=lambda: self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "action_conflict")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT COUNT(*) AS total FROM action_records"
            ),
            {"total": 1},
        )

    def test_reconcile_requires_current_evidence(self) -> None:
        """人工确认使用历史观察时间不构成当前证据。"""
        executor = CountingExecutor(fail=True)
        result = self._perform(executor)
        self.assertEqual(result["state"], "unknown")
        old_evidence = Evidence(
            action_key=TEXT_KEY,
            outcome="performed",
            effect="sent",
            reason_code=None,
            before_snapshot_id=helpers.BEFORE,
            after_snapshot_id=helpers.AFTER,
            observed_at="2026-01-01T00:00:00.000Z",
        )
        observation = ReconciliationObservation(
            action_key=TEXT_KEY,
            resolution="confirm_performed",
            observed_at=self.clock.now(),
            evidence=old_evidence,
            command_quiet=True,
            scene_settled=True,
            confirmation_open=False,
            ime_original="synthetic-ime-default",
            ime_current="synthetic-ime-default",
            matched_action_key=TEXT_KEY,
            confirmed=True,
        )
        with self.assertRaises(ActionError) as caught:
            reconcile_action(
                store=self.store,
                gate=self.gate,
                observation=observation,
                profile_id="demo",
                profile_db_path=str(self.path),
                explicit_confirm=True,
                command_stop_checker=lambda pid: "stopped",
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "invalid_observation")
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
