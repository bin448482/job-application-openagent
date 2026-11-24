"""验证 M5 动作原语参数数组、有界不重试与合成执行器的严格观察。"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from app.adapters.adb.actions import AdbActionExecutor
from app.adapters.simulated.actions import ScriptedActionExecutor
from app.domain.actions import action_key_for, derive_action_outcome
from app.domain.models import canonical_hash
from app.ports.actions import ActionExecutionError, ActionPlan
from tests import action_helpers as helpers

CONVERSATION = "boss:conv:" + canonical_hash(["合成联系人", "合成公司", "合成职位"])
MESSAGE = helpers.synthetic_text_key()
TEXT_KEY = f"boss:{MESSAGE}:reply"


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


class AdbActionPrimitiveTests(unittest.TestCase):
    """mock subprocess 验证真实适配器不连接设备。"""

    def _results(self, texts: list[str]) -> list[subprocess.CompletedProcess]:
        """把文本输出包装为成功 CompletedProcess 列表。"""
        return [
            subprocess.CompletedProcess(args=[], returncode=0, stdout=text, stderr="")
            for text in texts
        ]

    def test_send_text_command_order_parameters_and_single_attempt(self) -> None:
        """dump→cat→input text→tap→dump→cat，shell=false、5 秒、每步复核。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        before = '<hierarchy><node text="合成联系人" /></hierarchy>'
        after = '<hierarchy><node text="合成联系人" /><node text="您好" /></hierarchy>'
        results = self._results(["", before, "", "", "", after])
        with patch("subprocess.run", side_effect=results) as run:
            observation = executor.execute(plan())
        self.assertEqual(run.call_count, 6)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-3:], ["uiautomator", "dump", "/data/local/tmp/m5-action-ui.xml"])
        self.assertEqual(commands[1][-2:], ["cat", "/data/local/tmp/m5-action-ui.xml"])
        self.assertEqual(commands[2][-3:], ["input", "text", "您好"])
        self.assertEqual(commands[3][-4:], ["input", "tap", "10", "20"])
        for call in run.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertFalse(call.kwargs["check"])
        self.assertEqual(gate.verified, 6)
        self.assertTrue(observation.clicked)
        self.assertTrue(observation.body_readback_matches)
        self.assertEqual(observation.echo_count_after, 1)
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "performed")

    def test_failure_before_click_does_not_retry(self) -> None:
        """首条命令非零退出即停止，只调用一次并标记未点击。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with patch("subprocess.run", side_effect=[failed]) as run:
            with self.assertRaises(ActionExecutionError) as caught:
                executor.execute(plan())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(caught.exception.reason_code, "device_disconnected")
        self.assertFalse(caught.exception.clicked)

    def test_failure_after_irreversible_tap_keeps_clicked(self) -> None:
        """点击后读取失败为 clicked=True，调用方必须收敛 unknown。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        before = "<hierarchy />"
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        results = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=before, stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            failed,
        ]
        with patch("subprocess.run", side_effect=results) as run:
            with self.assertRaises(ActionExecutionError) as caught:
                executor.execute(plan())
        self.assertEqual(run.call_count, 5)
        self.assertTrue(caught.exception.clicked)

    def test_ime_restore_is_best_effort_and_reported(self) -> None:
        """IME 切换后恢复命令与最终读取使用同一门禁；恢复失败保留未知。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        before = "<hierarchy />"
        after = '<hierarchy><node class="android.widget.EditText" text="您好" /></hierarchy>'
        results = self._results(
            ["", before, "", "", "", after, "", "com.example.default/.Ime"]
        )
        with patch("subprocess.run", side_effect=results) as run:
            observation = executor.execute(plan(ime=True))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[-2][-3:], ["ime", "set", "com.example.default/.Ime"])
        self.assertEqual(
            commands[-1][-4:],
            ["settings", "get", "secure", "default_input_method"],
        )
        self.assertEqual(observation.ime_current, "com.example.default/.Ime")
        self.assertTrue(observation.ime_original.endswith(".Ime"))

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
