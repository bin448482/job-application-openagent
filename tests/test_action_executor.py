"""验证 M5 动作原语参数数组、有界不重试、IME 闭环与合成执行器严格观察。"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from app.adapters.adb.actions import AdbActionExecutor
from app.adapters.simulated.actions import ScriptedActionExecutor
from app.domain.actions import derive_action_outcome
from app.domain.models import canonical_hash
from app.ports.actions import ActionExecutionError, ActionPlan
from app.process_log import ProcessLog
from tests import action_helpers as helpers

CONVERSATION = "boss:conv:" + canonical_hash(["合成联系人", "合成公司", "合成职位"])
MESSAGE = helpers.synthetic_text_key()
TEXT_KEY = f"boss:{MESSAGE}:reply"
BEFORE_XML = (
    '<hierarchy><node text="合成联系人" /><node text="消息" /></hierarchy>'
)
READBACK_XML = (
    '<hierarchy><node class="android.widget.EditText" text="您好" />'
    '<node text="合成联系人" /><node text="消息" /></hierarchy>'
)
AFTER_XML = (
    '<hierarchy><node class="android.widget.EditText" text="" />'
    '<node text="合成联系人" /><node text="您好" /><node text="消息" /></hierarchy>'
)


class FakeGate:
    """记录 verify 次数的共享门禁替身；不读写真实门禁库。"""

    def __init__(self) -> None:
        """初始化复核计数。"""
        self.verified = 0

    def require_available(self) -> None:
        """兼容 ScreenPort 形状。"""
        self.verify()

    def verify(self, *args: object, **kwargs: object) -> None:
        """记录每条设备命令前的复核。"""
        self.verified += 1

    def release(self, *args: object, **kwargs: object) -> None:
        """释放不参与本测试。"""


def plan(*, ime: bool = False, taps: int = 1) -> ActionPlan:
    """构造文本发送计划；可选 IME 切换与点击数。"""
    return ActionPlan(
        action_key=TEXT_KEY,
        action_kind="send_text",
        target_key=MESSAGE,
        conversation_key=CONVERSATION,
        body_text="您好",
        attachment_name=None,
        target_label="合成联系人",
        anchor_label="消息",
        ime_component="com.example.synthetic/.Ime" if ime else None,
        ime_original="com.example.default/.Ime" if ime else None,
        irreversible_tap_index=max(0, taps - 1),
        tap_points=tuple((10 + index, 20) for index in range(taps)),
    )


def resume_plan() -> ActionPlan:
    """构造两击简历请求计划（打开槽位 + 确认）。"""
    return ActionPlan(
        action_key=f"{CONVERSATION}:resume",
        action_kind="send_resume",
        target_key=CONVERSATION,
        conversation_key=CONVERSATION,
        body_text=None,
        attachment_name="合成附件简历.pdf",
        target_label="合成联系人",
        anchor_label="消息",
        ime_component=None,
        ime_original=None,
        irreversible_tap_index=1,
        tap_points=((10, 20), (30, 40)),
    )


class AdbActionPrimitiveTests(unittest.TestCase):
    """mock subprocess 验证真实适配器不连接设备。"""

    def _runner(
        self,
        *,
        fail_at: int | None = None,
        current_ime: str = "com.example.default/.Ime",
        after_xml: str = AFTER_XML,
    ):
        """构造按命令类型返回合成 XML/IME 的单次执行器，绝不重试。"""
        calls: list[list[str]] = []
        state = {"cat": 0, "ime": current_ime}

        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            """按命令类型返回合成 XML/IME 输出。"""
            calls.append(list(args))
            if fail_at is not None and len(calls) == fail_at:
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr=""
                )
            if "uiautomator" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                text = (BEFORE_XML, READBACK_XML, after_xml)[min(index, 2)]
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=text, stderr="")
            if "ime" in args and "set" in args:
                state["ime"] = args[-1]
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "settings" in args and "get" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=state["ime"] + "\n", stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        return run, calls

    def test_send_text_command_order_parameters_and_single_attempt(self) -> None:
        """dump/cat、input、回读、tap、after 均 shell=false、5 秒、每步复核。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        runner, calls = self._runner()
        with patch("subprocess.run", side_effect=runner) as run:
            observation = executor.execute(plan())
        self.assertEqual(run.call_count, 8)
        self.assertEqual(calls[0][-3:], ["uiautomator", "dump", "/data/local/tmp/m5-action-ui.xml"])
        self.assertEqual(calls[1][-2:], ["cat", "/data/local/tmp/m5-action-ui.xml"])
        self.assertEqual(calls[2][-3:], ["input", "text", "您好"])
        self.assertEqual(calls[5][-4:], ["input", "tap", "10", "20"])
        for call in run.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertFalse(call.kwargs["check"])
        self.assertEqual(gate.verified, 8)
        self.assertTrue(observation.clicked)
        self.assertTrue(observation.body_readback_matches)
        self.assertTrue(observation.identity_unique)
        self.assertEqual(observation.echo_count_after, 1)
        self.assertTrue(observation.input_box_empty)
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "performed")

    def test_input_readback_mismatch_does_not_click(self) -> None:
        """输入回读不含正文时不发发送点击，观测可收敛 not_performed。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        runner, calls = self._runner(
            after_xml='<hierarchy><node class="android.widget.EditText" text="别的" />'
            '<node text="合成联系人" /><node text="消息" /></hierarchy>'
        )
        # readback 由 _runner 固定，另用只返回 before/误读/after 的脚本覆盖。
        state = {"cat": 0}
        texts = (
            BEFORE_XML,
            '<hierarchy><node class="android.widget.EditText" text="别的" />'
            '<node text="合成联系人" /><node text="消息" /></hierarchy>',
            AFTER_XML,
        )

        def readback_runner(args: list[str], **kwargs: object):
            """按阶段返回输入回读与后续层级文本。"""
            calls.append(list(args))
            if "uiautomator" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=texts[min(index, 2)], stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        del runner
        with patch("subprocess.run", side_effect=readback_runner):
            observation = executor.execute(plan())
        commands = [item for item in calls if "input" in item and "tap" in item]
        self.assertEqual(commands, [])
        outcome = derive_action_outcome(observation)
        self.assertEqual(outcome.evidence.outcome, "not_performed")
        self.assertTrue(outcome.pause)

    def test_duplicate_identity_does_not_tap(self) -> None:
        """目标标签不唯一时前置失败，任何点击均不发出。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        duplicate = (
            '<hierarchy><node text="合成联系人" /><node text="合成联系人" />'
            '<node text="消息" /></hierarchy>'
        )
        state = {"cat": 0}

        def runner(args: list[str], **kwargs: object):
            """按命令类型返回合成层级文本。"""
            if "uiautomator" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=duplicate, stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=runner) as run:
            observation = executor.execute(plan())
        taps = [call for call in run.call_args_list if "tap" in call.args[0]]
        self.assertEqual(taps, [])
        self.assertFalse(observation.clicked)
        self.assertFalse(observation.identity_unique)
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "not_performed")

    def test_resume_attachment_name_is_checked_before_confirm(self) -> None:
        """简历附件名不符时只做打开槽位，不点击确认。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node text="合成联系人" /><node text="消息" />'
            '<node text="发简历" /></hierarchy>'
        )
        dialog = '<hierarchy><node text="合成简历-其他.pdf" /></hierarchy>'
        state = {"cat": 0}
        texts = (before, dialog, before)

        def runner(args: list[str], **kwargs: object):
            """按命令类型返回合成层级文本。"""
            if "uiautomator" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=texts[min(index, 2)], stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=runner) as run:
            observation = executor.execute(resume_plan())
        taps = [call.args[0] for call in run.call_args_list if "tap" in call.args[0]]
        self.assertEqual(len(taps), 1)
        self.assertFalse(observation.clicked)
        self.assertFalse(observation.attachment_name_matched)
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "not_performed")

    def test_resume_and_contact_require_new_change_not_preexisting_marker(self) -> None:
        """既有“请求中”提示不证明本次效果；新增提示才可 request_sent。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node text="合成联系人" /><node text="消息" />'
            '<node text="附件简历请求已发送" /></hierarchy>'
        )
        dialog = (
            '<hierarchy><node text="合成联系人" />'
            '<node text="合成附件简历.pdf" /></hierarchy>'
        )
        after_same = before
        state = {"cat": 0}
        texts = (before, dialog, after_same)

        def runner(args: list[str], **kwargs: object):
            """按命令类型返回合成层级文本。"""
            if "uiautomator" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=texts[min(index, 2)], stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(resume_plan())
        self.assertTrue(observation.clicked)
        self.assertTrue(observation.attachment_name_matched)
        self.assertFalse(observation.attachment_request_observed)
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "unknown")

    def test_failure_before_click_does_not_retry(self) -> None:
        """首条命令非零退出即停止，只调用一次并标记未点击。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        runner, calls = self._runner(fail_at=1)
        with patch("subprocess.run", side_effect=runner) as run:
            with self.assertRaises(ActionExecutionError) as caught:
                executor.execute(plan())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(caught.exception.reason_code, "device_disconnected")
        self.assertFalse(caught.exception.clicked)
        self.assertTrue(caught.exception.scene_settled)
        self.assertEqual(len(calls), 1)

    def test_failure_after_irreversible_tap_keeps_clicked(self) -> None:
        """点击后读取失败为 clicked=True，调用方必须收敛 unknown。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        runner, calls = self._runner(fail_at=7)
        with patch("subprocess.run", side_effect=runner) as run:
            with self.assertRaises(ActionExecutionError) as caught:
                executor.execute(plan())
        self.assertEqual(run.call_count, 7)
        self.assertTrue(caught.exception.clicked)
        self.assertFalse(caught.exception.command_quiet)
        self.assertIn("input", calls[5])

    def test_ime_switch_and_restore_are_bounded_and_read_back(self) -> None:
        """输入前实际切到目标 IME，恢复后读回原值；失败保持未知。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        runner, calls = self._runner()
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(plan(ime=True))
        set_commands = [item for item in calls if "ime" in item and "set" in item]
        self.assertEqual(set_commands[0][-1], "com.example.synthetic/.Ime")
        self.assertEqual(set_commands[-1][-1], "com.example.default/.Ime")
        self.assertEqual(observation.ime_current, "com.example.default/.Ime")
        self.assertTrue(observation.ime_original.endswith(".Ime"))

    def test_adb_steps_use_fixed_command_labels(self) -> None:
        """动作 ADB 事件区分 dump/cat/input/tap/IME，不写参数或 serial。"""
        gate = FakeGate()
        log = ProcessLog(path=helpers.ROOT / "data" / "logs" / "m5-adb.jsonl")
        executor = AdbActionExecutor(
            serial="<serial>", gate=gate, process_log=log
        )
        runner, _calls = self._runner()
        with patch("subprocess.run", side_effect=runner):
            executor.execute(plan(ime=True))
        records = [
            __import__("json").loads(line)
            for line in log.path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        commands = {item["command"] for item in records}
        self.assertTrue(
            {
                "adb_action_dump",
                "adb_action_cat",
                "adb_action_input",
                "adb_action_tap",
                "adb_action_ime_set",
                "adb_action_ime_get",
            }
            <= commands
        )
        joined = log.path.read_text("utf-8")
        self.assertNotIn("<serial>", joined)
        self.assertNotIn("/data/local/tmp", joined)
        try:
            log.path.unlink()
        except OSError:
            pass

    def test_read_current_ime(self) -> None:
        """读取当前 IME 返回去空白值；失败为断连。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="com.example/.Ime\n", stderr=""
            ),
        ):
            self.assertEqual(executor.read_current_ime(), "com.example/.Ime")


class ScriptedExecutorTests(unittest.TestCase):
    """合成执行器只回放观察，不接受任意 Evidence 字段。"""

    def test_positive_observation_derives_performed(self) -> None:
        """合成观察经纯域差分得到 performed+sent。"""
        executor = ScriptedActionExecutor(
            {
                "current_ime": "synthetic-ime-default",
                "observation": helpers.observation(),
            }
        )
        result = derive_action_outcome(executor.execute(plan()))
        self.assertEqual(result.evidence.outcome, "performed")
        self.assertEqual(result.evidence.effect, "sent")

    def test_raise_directive_keeps_click_fact(self) -> None:
        """脚本化失败不产生 Evidence；clicked 事实由执行错误保留。"""
        executor = ScriptedActionExecutor(
            {
                "raise": {"reason_code": "device_disconnected", "clicked": True}
            }
        )
        with self.assertRaises(ActionExecutionError) as caught:
            executor.execute(plan())
        self.assertTrue(caught.exception.clicked)

    def test_unknown_fields_and_evidence_are_rejected(self) -> None:
        """fixture 不能注入额外键或直接给 outcome/effect。"""
        data = helpers.observation()
        data["outcome"] = "performed"
        with self.assertRaises(ValueError):
            ScriptedActionExecutor({"observation": data}).execute(plan())
        with self.assertRaises(ValueError):
            ScriptedActionExecutor({"observation": helpers.observation(), "extra": 1})


if __name__ == "__main__":
    unittest.main()
