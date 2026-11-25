"""验证 M5 动作原语参数数组、有界不重试、IME 闭环与合成执行器严格观察。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.adapters.adb.actions import AdbActionExecutor
from app.adapters.adb.recovery import AdbRecoveryObserver
from app.adapters.simulated.actions import ScriptedActionExecutor
from app.domain.actions import ActionObservation, derive_action_outcome
from app.domain.models import canonical_hash
from app.ports.actions import ActionExecutionError, ActionPlan
from app.ports.screen import HierarchyCapture
from app.process_log import ProcessLog
from tests import action_helpers as helpers

CONVERSATION = "boss:conv:" + canonical_hash(["合成联系人", "合成公司", "合成职位"])
MESSAGE = helpers.synthetic_text_key()
TEXT_KEY = f"boss:{MESSAGE}:reply"
BEFORE_XML = (
    '<hierarchy><node text="合成联系人"><node text="消息" /></node></hierarchy>'
)
READBACK_XML = (
    '<hierarchy><node text="合成联系人"><node text="消息" />'
    '<node class="android.widget.EditText" text="您好" /></node></hierarchy>'
)
AFTER_XML = (
    '<hierarchy><node text="合成联系人"><node text="消息" />'
    '<node text="您好" /><node class="android.widget.EditText" text="" />'
    '</node></hierarchy>'
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


class _NoOpCommandStore:
    """观察逻辑测试替身：只满足执行边界，不写账本也不证明进程族。

    真实 `CommandIdentityStore` 与 Job 证明由 `test_action_usecase` 及
    `test_platform_bound_*` 等专门用例覆盖；本替身不支撑任何 performed 证据。
    """

    def mark_started(self, action_key: str) -> None:
        """观察用例不应触达真实账本。"""
        raise AssertionError("observation stub must not record")

    def record(self, action_key: str, pid: int) -> None:
        """观察用例不应触达真实账本。"""
        raise AssertionError("observation stub must not record")

    def record_unresolved(self, action_key: str) -> None:
        """观察用例不应触达真实账本。"""
        raise AssertionError("observation stub must not record")

    def finish_family(self, *args: object, **kwargs: object) -> None:
        """观察用例不应触达真实账本。"""
        raise AssertionError("observation stub must not record")


class _ObservationExecutor(AdbActionExecutor):
    """只验证观察/前置逻辑：按计划绑定动作并关闭真实命令账本回调。

    mock 的 `subprocess.run` 成功不构成进程族停止证明；真实账本/Job 路径必须
    由使用 `CommandIdentityStore` 的专门用例断言。
    """

    def __init__(self, **kwargs: object) -> None:
        """注入不写盘账本替身，保持执行边界非空。"""
        kwargs.setdefault("command_store", _NoOpCommandStore())
        super().__init__(**kwargs)

    def execute(self, plan, *, before_command=None):
        """绑定当前计划动作键后用 mock 观察输入执行一次。"""
        self.bind_action(plan.action_key)
        self._command_hooks = lambda: (None, None, None, None)  # type: ignore[method-assign]
        return super().execute(plan, before_command=before_command)


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

    def _custom_run(
        self, texts: tuple[str, str, str], *, current_ime: str = "com.example.default/.Ime"
    ):
        """按三个 cat 阶段返回指定 XML 的执行器，供目标差分反例使用。"""
        calls: list[list[str]] = []
        state = {"cat": 0, "ime": current_ime}

        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            """按命令类型返回指定阶段 XML。"""
            calls.append(list(args))
            if "uiautomator" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=texts[min(index, 2)], stderr=""
                )
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
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        runner, calls = self._runner()
        with patch("subprocess.run", side_effect=runner) as run:
            observation = executor.execute(plan())
        self.assertEqual(run.call_count, 8)
        self.assertEqual(calls[0][-3:], ["uiautomator", "dump", "/data/local/tmp/m3-ui.xml"])
        self.assertEqual(calls[1][-2:], ["cat", "/data/local/tmp/m3-ui.xml"])
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
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
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
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
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
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
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
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
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

    def test_text_readback_wrong_identity_or_multiple_editors_does_not_click(self) -> None:
        """readback 换人或两个 editor 任一含正文都不发点击。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        wrong_person = (
            '<hierarchy><node text="另一联系人"><node text="消息" /></node>'
            '<node class="android.widget.EditText" text="您好" /></hierarchy>'
        )
        runner, calls = self._custom_run((BEFORE_XML, wrong_person, AFTER_XML))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(plan())
        self.assertEqual([c for c in calls if "tap" in c], [])
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "not_performed")

        two_editors = (
            '<hierarchy><node text="合成联系人"><node text="消息" /></node>'
            '<node class="android.widget.EditText" text="别的" />'
            '<node class="android.widget.EditText" text="您好" /></hierarchy>'
        )
        runner, calls = self._custom_run((BEFORE_XML, two_editors, AFTER_XML))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(plan())
        self.assertEqual([c for c in calls if "tap" in c], [])
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "not_performed")

    def test_text_unrelated_subtree_echo_is_not_performed(self) -> None:
        """正文只增加在另一会话子树时不得 performed。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        after_other = (
            '<hierarchy><node text="合成联系人"><node text="消息" /></node>'
            '<node text="另一联系人"><node text="您好" /></node>'
            '<node class="android.widget.EditText" text="" /></hierarchy>'
        )
        runner, calls = self._custom_run((BEFORE_XML, READBACK_XML, after_other))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(plan())
        self.assertTrue([c for c in calls if "tap" in c])
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "unknown")

    def test_contact_handled_or_disabled_slot_does_not_tap(self) -> None:
        """联系方式槽位已处理/不可点击时零 tap。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        handled = (
            '<hierarchy><node text="合成联系人"><node text="微信" clickable="true" />'
            '<node text="请求中" /></node></hierarchy>'
        )
        contact = ActionPlan(
            action_key=f"{CONVERSATION}:wechat",
            action_kind="change_wechat",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=1,
            tap_points=((10, 20), (30, 40)),
        )
        runner, calls = self._custom_run((handled, handled, handled))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(contact)
        self.assertEqual([c for c in calls if "tap" in c], [])
        self.assertEqual(derive_action_outcome(observation).evidence.outcome, "not_performed")

    def test_contact_effect_is_type_specific(self) -> None:
        """另一类型新增请求不算本类型效果，点击后保持 unknown。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node text="合成联系人">'
            '<node text="微信" clickable="true" enabled="true" />'
            '<node text="电话" clickable="true" enabled="true" />'
            '<node text="消息" /></node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成联系人">'
            '<node text="微信" clickable="true" enabled="true">'
            '<node text="请求中" /></node>'
            '<node text="电话" clickable="true" enabled="true" />'
            '<node text="消息" /></node></hierarchy>'
        )
        phone = ActionPlan(
            action_key=f"{CONVERSATION}:phone",
            action_kind="change_phone",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=1,
            tap_points=((10, 20), (30, 40)),
        )
        runner, calls = self._custom_run((before, before, after))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(phone)
        self.assertEqual(len([c for c in calls if "tap" in c]), 2)
        self.assertNotEqual(
            derive_action_outcome(observation).evidence.outcome, "performed"
        )

    def test_nested_slot_container_and_button_count_as_one_slot(self) -> None:
        """标签容器→交换按钮→状态视为同一槽位，不因两层含类型词而拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node text="合成联系人">'
            '<node><node text="微信" /><node text="交换微信" clickable="true"'
            ' enabled="true" /></node>'
            '<node text="消息" /></node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成联系人">'
            '<node><node text="微信" /><node text="交换微信" clickable="true"'
            ' enabled="true" /><node text="请求中" /></node>'
            '<node text="消息" /></node></hierarchy>'
        )
        wechat = ActionPlan(
            action_key=f"{CONVERSATION}:wechat",
            action_kind="change_wechat",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=1,
            tap_points=((10, 20), (30, 40)),
        )
        runner, calls = self._custom_run((before, before, after))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(wechat)
        self.assertEqual(len([c for c in calls if "tap" in c]), 2)
        outcome = derive_action_outcome(observation)
        self.assertEqual(outcome.evidence.outcome, "performed")
        self.assertEqual(outcome.evidence.effect, "request_sent")

    def test_other_type_handled_slot_does_not_block_current_type(self) -> None:
        """另一类型已处理不污染本类型未处理槽位的准入与效果。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node text="合成联系人">'
            '<node><node text="微信" /><node text="已处理" /></node>'
            '<node><node text="电话" /><node text="交换电话" clickable="true"'
            ' enabled="true" /></node>'
            '<node text="消息" /></node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成联系人">'
            '<node><node text="微信" /><node text="已处理" /></node>'
            '<node><node text="电话" /><node text="交换电话" clickable="true"'
            ' enabled="true" /><node text="请求中" /></node>'
            '<node text="消息" /></node></hierarchy>'
        )
        phone = ActionPlan(
            action_key=f"{CONVERSATION}:phone",
            action_kind="change_phone",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=1,
            tap_points=((10, 20), (30, 40)),
        )
        runner, calls = self._custom_run((before, before, after))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(phone)
        self.assertEqual(len([c for c in calls if "tap" in c]), 2)
        outcome = derive_action_outcome(observation)
        self.assertEqual(outcome.evidence.outcome, "performed")
        self.assertEqual(outcome.evidence.effect, "request_sent")

    def test_dialog_disabled_slot_is_not_confirmed(self) -> None:
        """打开槽位后 dialog 同类型槽位变 disabled 时不再确认点击。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node text="合成联系人">'
            '<node text="电话" clickable="true" enabled="true" />'
            '<node text="消息" /></node></hierarchy>'
        )
        dialog = (
            '<hierarchy><node text="合成联系人">'
            '<node text="电话" clickable="true" enabled="false" />'
            '<node text="消息" /></node></hierarchy>'
        )
        phone = ActionPlan(
            action_key=f"{CONVERSATION}:phone",
            action_kind="change_phone",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=1,
            tap_points=((10, 20), (30, 40)),
        )
        runner, calls = self._custom_run((before, dialog, dialog))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(phone)
        self.assertEqual(len([c for c in calls if "tap" in c]), 1)
        self.assertFalse(observation.clicked)
        self.assertEqual(
            derive_action_outcome(observation).evidence.outcome, "not_performed"
        )

    def test_delete_row_or_anchor_false_is_not_list_coverage(self) -> None:
        """行/锚点自身 false 或外层列表无覆盖事实都不构成完整列表。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        row_false = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' bounds="[0,0][1000,1800]">'
            '<node class="android.widget.LinearLayout" scrollable="false">'
            '<node text="合成联系人"><node text="消息" /></node></node>'
            '</node></hierarchy>'
        )
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )
        runner, calls = self._custom_run((row_false, row_false, row_false))
        with patch("subprocess.run", side_effect=runner):
            executor.execute(delete)
        self.assertEqual([c for c in calls if "tap" in c], [])

        complete = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,1800]">'
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node></node></hierarchy>'
        )
        anchor_false = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' bounds="[0,0][1000,1800]">'
            '<node class="android.widget.Button" scrollable="false"'
            ' text="消息" bounds="[10,10][500,60]" />'
            '</node></hierarchy>'
        )
        runner, calls = self._custom_run((complete, complete, anchor_false))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(delete)
        self.assertEqual(len([c for c in calls if "tap" in c]), 3)
        self.assertNotEqual(
            derive_action_outcome(observation).evidence.outcome, "performed"
        )

    def test_delete_incomplete_after_viewport_is_not_performed(self) -> None:
        """删除后只看到可滚动不完整视口时不得判为已删除。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,1800]">'
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node></node></hierarchy>'
        )
        after = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="true" bounds="[0,0][1000,1800]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node></hierarchy>'
        )
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )
        runner, calls = self._custom_run((before, before, after))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(delete)
        self.assertEqual(len([c for c in calls if "tap" in c]), 3)
        self.assertNotEqual(
            derive_action_outcome(observation).evidence.outcome, "performed"
        )

    def test_delete_other_row_or_external_clip_is_not_complete(self) -> None:
        """其它行被列表容器裁剪或整个列表超出外层可见区域都不算完整。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )
        target_row = (
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node>'
        )
        other_row = '<node text="另一行" bounds="[0,1700][1000,2200]" />'
        clipped_before = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,1800]">'
            + target_row
            + other_row
            + '</node></hierarchy>'
        )
        runner, calls = self._custom_run(
            (clipped_before, clipped_before, clipped_before)
        )
        with patch("subprocess.run", side_effect=runner):
            executor.execute(delete)
        self.assertEqual([c for c in calls if "tap" in c], [])

        complete = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,1800]">'
            + target_row
            + '</node></hierarchy>'
        )
        clipped_after = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,1800]">'
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node text="另一行" bounds="[0,1700][1000,2200]" />'
            '</node></hierarchy>'
        )
        runner, calls = self._custom_run((complete, complete, clipped_after))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(delete)
        self.assertEqual(len([c for c in calls if "tap" in c]), 3)
        self.assertNotEqual(
            derive_action_outcome(observation).evidence.outcome, "performed"
        )

        external = (
            '<hierarchy bounds="[0,0][1000,1800]">'
            '<node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,2000]">'
            + target_row
            + '</node></hierarchy>'
        )
        runner, calls = self._custom_run((external, external, external))
        with patch("subprocess.run", side_effect=runner):
            executor.execute(delete)
        self.assertEqual([c for c in calls if "tap" in c], [])

    def test_delete_missing_bad_or_zero_bounds_is_not_complete(self) -> None:
        """缺失、坏格式、零面积与自相矛盾 bounds 在不可逆点击前拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )
        cases = {
            "missing-target": (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[0,0][1000,1800]">'
                '<node text="合成联系人"><node text="消息" /></node>'
                '</node></hierarchy>'
            ),
            "bad-container": (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="bad">'
                '<node text="合成联系人" bounds="[0,0][1000,200]">'
                '<node text="消息" bounds="[10,10][500,60]" /></node>'
                '</node></hierarchy>'
            ),
            "zero-area": (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[0,0][0,0]">'
                '<node text="合成联系人" bounds="[0,0][1000,200]">'
                '<node text="消息" bounds="[10,10][500,60]" /></node>'
                '</node></hierarchy>'
            ),
            "self-contradictory": (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[100,100][50,50]">'
                '<node text="合成联系人" bounds="[0,0][1000,200]">'
                '<node text="消息" bounds="[10,10][500,60]" /></node>'
                '</node></hierarchy>'
            ),
        }
        for name, xml in cases.items():
            with self.subTest(case=name):
                runner, calls = self._custom_run((xml, xml, xml))
                with patch("subprocess.run", side_effect=runner):
                    executor.execute(delete)
                self.assertEqual([c for c in calls if "tap" in c], [])

    def test_two_same_type_slots_are_ambiguous_and_zero_tap(self) -> None:
        """同一分组两个独立同类型槽位不得上溯合并，两个类型都零点击。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node text="合成联系人">'
            '<node><node text="微信" /><node text="交换微信"'
            ' clickable="true" enabled="true" /></node>'
            '<node><node text="微信" /><node text="交换微信"'
            ' clickable="true" enabled="true" /></node>'
            '<node text="消息" /></node></hierarchy>'
        )
        for kind, suffix in (("change_wechat", "wechat"), ("change_phone", "phone")):
            contact = ActionPlan(
                action_key=f"{CONVERSATION}:{suffix}",
                action_kind=kind,
                target_key=CONVERSATION,
                conversation_key=CONVERSATION,
                body_text=None,
                attachment_name=None,
                target_label="合成联系人",
                anchor_label="消息",
                ime_component=None,
                ime_original=None,
                irreversible_tap_index=1,
                tap_points=((10, 20), (30, 40)),
            )
            runner, calls = self._custom_run((before, before, before))
            with patch("subprocess.run", side_effect=runner):
                executor.execute(contact)
            self.assertEqual([c for c in calls if "tap" in c], [])

    def test_disabled_after_button_still_proves_new_type_status(self) -> None:
        """动作后按钮禁用但本类型精确新状态仍在同一槽位时证明 request_sent。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        for kind, suffix, marker, button in (
            ("change_wechat", "wechat", "微信", "交换微信"),
            ("change_phone", "phone", "电话", "交换电话"),
        ):
            before = (
                '<hierarchy><node text="合成联系人">'
                f'<node><node text="{marker}" /><node text="{button}"'
                ' clickable="true" enabled="true" /></node>'
                '<node text="消息" /></node></hierarchy>'
            )
            after = (
                '<hierarchy><node text="合成联系人">'
                f'<node><node text="{marker}" /><node text="{button}"'
                ' clickable="true" enabled="false" /><node text="请求中" /></node>'
                '<node text="消息" /></node></hierarchy>'
            )
            contact = ActionPlan(
                action_key=f"{CONVERSATION}:{suffix}",
                action_kind=kind,
                target_key=CONVERSATION,
                conversation_key=CONVERSATION,
                body_text=None,
                attachment_name=None,
                target_label="合成联系人",
                anchor_label="消息",
                ime_component=None,
                ime_original=None,
                irreversible_tap_index=1,
                tap_points=((10, 20), (30, 40)),
            )
            runner, calls = self._custom_run((before, before, after))
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(contact)
            self.assertEqual(len([c for c in calls if "tap" in c]), 2)
            outcome = derive_action_outcome(observation)
            self.assertEqual(outcome.evidence.outcome, "performed")
            self.assertEqual(outcome.evidence.effect, "request_sent")

    def test_delete_visible_leaf_bad_bounds_blocks_each_phase(self) -> None:
        """行内可见叶节点缺失/坏/零面积 bounds 在三阶段分别拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )

        def page(row: str) -> str:
            """把行放入合法 RecyclerView 容器。"""
            return (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[0,0][1000,1800]">'
                + row
                + '</node></hierarchy>'
            )

        leaf_variants = {
            "missing": "<node text=\"叶\" />",
            "bad": '<node text="叶" bounds="bad" />',
            "zero": '<node text="叶" bounds="[10,10][10,10]" />',
        }
        for name, leaf in leaf_variants.items():
            with self.subTest(phase="before", leaf=name):
                row = (
                    '<node text="合成联系人" bounds="[0,0][1000,200]">'
                    '<node text="消息" bounds="[10,10][500,60]" />'
                    + leaf
                    + '</node>'
                )
                invalid = page(row)
                runner, calls = self._custom_run(
                    (invalid, invalid, invalid)
                )
                with patch("subprocess.run", side_effect=runner):
                    executor.execute(delete)
                self.assertEqual([c for c in calls if "tap" in c], [])

            with self.subTest(phase="dialog", leaf=name):
                complete = page(
                    '<node text="合成联系人" bounds="[0,0][1000,200]">'
                    '<node text="消息" bounds="[10,10][500,60]" />'
                    '<node text="叶" bounds="[10,60][200,100]" /></node>'
                )
                dialog = page(
                    '<node text="合成联系人" bounds="[0,0][1000,200]">'
                    '<node text="消息" bounds="[10,10][500,60]" />'
                    + leaf
                    + '</node>'
                )
                runner, calls = self._custom_run(
                    (complete, dialog, complete)
                )
                with patch("subprocess.run", side_effect=runner):
                    observation = executor.execute(delete)
                self.assertEqual(len([c for c in calls if "tap" in c]), 2)
                self.assertNotEqual(
                    derive_action_outcome(observation).evidence.outcome,
                    "performed",
                )

            with self.subTest(phase="after", leaf=name):
                complete = page(
                    '<node text="合成联系人" bounds="[0,0][1000,200]">'
                    '<node text="消息" bounds="[10,10][500,60]" />'
                    '<node text="叶" bounds="[10,60][200,100]" /></node>'
                )
                after = page(
                    '<node text="消息" bounds="[10,10][500,60]" />'
                    + leaf
                )
                runner, calls = self._custom_run(
                    (complete, complete, after)
                )
                with patch("subprocess.run", side_effect=runner):
                    observation = executor.execute(delete)
                self.assertEqual(len([c for c in calls if "tap" in c]), 3)
                self.assertNotEqual(
                    derive_action_outcome(observation).evidence.outcome,
                    "performed",
                )

    def test_delete_outer_clip_or_scroll_blocks_each_phase(self) -> None:
        """更外层祖先裁剪或 scrollable=true 在三阶段分别拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )

        def page(row: str, outer: str = "") -> str:
            """把行放入合法容器，可选更外层包裹。"""
            inner = (
                '<node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[0,0][1000,1800]">'
                + row
                + '</node>'
            )
            return (
                '<hierarchy>'
                + (outer.replace("INNER", inner) if outer else inner)
                + '</hierarchy>'
            )

        row = (
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node>'
        )
        after_row = '<node text="消息" bounds="[10,10][500,60]" />'
        clip = (
            '<node class="android.widget.FrameLayout" bounds="[0,0][500,1800]">'
            'INNER</node>'
        )
        scroll = (
            '<node class="android.widget.ScrollView" scrollable="true"'
            ' bounds="[0,0][1000,2000]">INNER</node>'
        )
        complete = page(row)
        for name, outer in (("clip", clip), ("scroll", scroll)):
            with self.subTest(phase="before", case=name):
                invalid = page(row, outer)
                runner, calls = self._custom_run(
                    (invalid, invalid, invalid)
                )
                with patch("subprocess.run", side_effect=runner):
                    executor.execute(delete)
                self.assertEqual([c for c in calls if "tap" in c], [])

            with self.subTest(phase="dialog", case=name):
                dialog = page(row, outer)
                runner, calls = self._custom_run(
                    (complete, dialog, complete)
                )
                with patch("subprocess.run", side_effect=runner):
                    observation = executor.execute(delete)
                self.assertEqual(len([c for c in calls if "tap" in c]), 2)
                self.assertNotEqual(
                    derive_action_outcome(observation).evidence.outcome,
                    "performed",
                )

            with self.subTest(phase="after", case=name):
                after = page(after_row, outer)
                runner, calls = self._custom_run(
                    (complete, complete, after)
                )
                with patch("subprocess.run", side_effect=runner):
                    observation = executor.execute(delete)
                self.assertEqual(len([c for c in calls if "tap" in c]), 3)
                self.assertNotEqual(
                    derive_action_outcome(observation).evidence.outcome,
                    "performed",
                )

    def test_delete_intermediate_inner_clip_blocks_each_phase(self) -> None:
        """列表内中间祖先裁剪在有意义节点链上被三阶段拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )

        def page(row: str, clip: bool) -> str:
            """把行放入列表容器，可选中间 FrameLayout 裁剪。"""
            if clip:
                inner = (
                    '<node class="android.widget.FrameLayout"'
                    ' bounds="[0,0][500,200]">' + row + '</node>'
                )
            else:
                inner = row
            return (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[0,0][1000,1800]">'
                + inner
                + '</node></hierarchy>'
            )

        row = (
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node>'
        )
        after_row = (
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node text="另一行" bounds="[0,1700][1000,2200]" />'
        )
        complete = page(row, False)
        with self.subTest(phase="before"):
            invalid = page(row, True)
            runner, calls = self._custom_run((invalid, invalid, invalid))
            with patch("subprocess.run", side_effect=runner):
                executor.execute(delete)
            self.assertEqual([c for c in calls if "tap" in c], [])
        with self.subTest(phase="dialog"):
            dialog = page(row, True)
            runner, calls = self._custom_run((complete, dialog, complete))
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(delete)
            self.assertEqual(len([c for c in calls if "tap" in c]), 2)
            self.assertNotEqual(
                derive_action_outcome(observation).evidence.outcome, "performed"
            )
        with self.subTest(phase="after"):
            after = page(after_row, True)
            runner, calls = self._custom_run((complete, complete, after))
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(delete)
            self.assertEqual(len([c for c in calls if "tap" in c]), 3)
            self.assertNotEqual(
                derive_action_outcome(observation).evidence.outcome, "performed"
            )

    def test_delete_content_desc_leaf_missing_bounds_blocks_each_phase(self) -> None:
        """仅有 content-desc 的可见叶节点缺 bounds 在三阶段拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )

        def page(row: str) -> str:
            """放入合法列表容器。"""
            return (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[0,0][1000,1800]">'
                + row
                + '</node></hierarchy>'
            )

        bad_row = (
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node content-desc="描述叶" /></node>'
        )
        good_row = (
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node content-desc="描述叶" bounds="[10,60][200,100]" /></node>'
        )
        good_after = (
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node content-desc="描述叶" bounds="[10,60][200,100]" />'
        )
        bad_after = (
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node content-desc="描述叶" />'
        )
        complete = page(good_row)
        with self.subTest(phase="before"):
            invalid = page(bad_row)
            runner, calls = self._custom_run((invalid, invalid, invalid))
            with patch("subprocess.run", side_effect=runner):
                executor.execute(delete)
            self.assertEqual([c for c in calls if "tap" in c], [])
        with self.subTest(phase="dialog"):
            runner, calls = self._custom_run(
                (complete, page(bad_row), complete)
            )
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(delete)
            self.assertEqual(len([c for c in calls if "tap" in c]), 2)
            self.assertNotEqual(
                derive_action_outcome(observation).evidence.outcome, "performed"
            )
        with self.subTest(phase="after"):
            runner, calls = self._custom_run(
                (complete, complete, page(bad_after))
            )
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(delete)
            self.assertEqual(len([c for c in calls if "tap" in c]), 3)
            self.assertNotEqual(
                derive_action_outcome(observation).evidence.outcome, "performed"
            )
        with self.subTest(phase="after-good"):
            runner, calls = self._custom_run(
                (complete, complete, page(good_after))
            )
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(delete)
            self.assertEqual(
                derive_action_outcome(observation).evidence.outcome, "performed"
            )

    def test_delete_non_target_leaf_inner_scroll_blocks_each_phase(self) -> None:
        """非目标可见叶节点内部中间 ScrollView scrollable=true 三阶段拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )

        def page(row: str) -> str:
            """放入合法列表容器。"""
            return (
                '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
                ' scrollable="false" bounds="[0,0][1000,1800]">'
                + row
                + '</node></hierarchy>'
            )

        clean_row = (
            '<node text="合成联系人" bounds="[0,0][1000,400]">'
            '<node text="消息" bounds="[10,10][500,60]" />'
            '</node>'
        )
        inner_row = (
            '<node text="合成联系人" bounds="[0,0][1000,400]">'
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node class="android.widget.ScrollView" scrollable="true"'
            ' bounds="[0,60][1000,400]">'
            '<node text="非目标叶" bounds="[10,70][500,120]" />'
            '</node></node>'
        )
        after_inner = (
            '<node text="消息" bounds="[10,10][500,60]" />'
            '<node class="android.widget.ScrollView" scrollable="true"'
            ' bounds="[0,60][1000,400]">'
            '<node text="非目标叶" bounds="[10,70][500,120]" />'
            '</node>'
        )
        clean = page(clean_row)
        invalid_before = page(inner_row)
        with self.subTest(phase="before"):
            runner, calls = self._custom_run(
                (invalid_before, invalid_before, invalid_before)
            )
            with patch("subprocess.run", side_effect=runner):
                executor.execute(delete)
            self.assertEqual([c for c in calls if "tap" in c], [])
        with self.subTest(phase="dialog"):
            runner, calls = self._custom_run(
                (clean, page(inner_row), clean)
            )
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(delete)
            self.assertEqual(len([c for c in calls if "tap" in c]), 2)
            self.assertNotEqual(
                derive_action_outcome(observation).evidence.outcome, "performed"
            )
        with self.subTest(phase="after"):
            after = page(after_inner)
            runner, calls = self._custom_run((clean, clean, after))
            with patch("subprocess.run", side_effect=runner):
                observation = executor.execute(delete)
            self.assertEqual(len([c for c in calls if "tap" in c]), 3)
            self.assertNotEqual(
                derive_action_outcome(observation).evidence.outcome, "performed"
            )

    def test_delete_complete_list_removal_is_performed(self) -> None:
        """完整列表内目标消失且锚点仍在时为 deleted 正例。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        before = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,1800]">'
            '<node text="合成联系人" bounds="[0,0][1000,200]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node></node></hierarchy>'
        )
        after = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="false" bounds="[0,0][1000,1800]">'
            '<node text="消息" bounds="[10,10][500,60]" /></node></hierarchy>'
        )
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )
        runner, calls = self._custom_run((before, before, after))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(delete)
        self.assertEqual(len([c for c in calls if "tap" in c]), 3)
        outcome = derive_action_outcome(observation)
        self.assertEqual(outcome.evidence.outcome, "performed")
        self.assertEqual(outcome.evidence.effect, "deleted")

    def test_delete_scrollable_viewport_and_single_tap_resume_do_not_tap(self) -> None:
        """可滚动不完整列表与单 tap 简历均在不可逆点击前拒绝。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        scrollable = (
            '<hierarchy><node class="androidx.recyclerview.widget.RecyclerView"'
            ' scrollable="true" bounds="[0,0][1000,1800]"><node text="合成联系人">'
            '<node text="消息" /></node></node></hierarchy>'
        )
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )
        runner, calls = self._custom_run((scrollable, scrollable, scrollable))
        with patch("subprocess.run", side_effect=runner):
            executor.execute(delete)
        self.assertEqual([c for c in calls if "tap" in c], [])

        single = replace(resume_plan(), irreversible_tap_index=0, tap_points=((10, 20),))
        runner, calls = self._custom_run((BEFORE_XML, BEFORE_XML, BEFORE_XML))
        with patch("subprocess.run", side_effect=runner):
            executor.execute(single)
        self.assertEqual([c for c in calls if "tap" in c], [])

    def test_identity_failure_does_not_switch_ime(self) -> None:
        """无需改变设备的前置失败不触发 IME 切换。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        duplicate = (
            '<hierarchy><node text="合成联系人"><node text="消息" /></node>'
            '<node text="合成联系人" /></hierarchy>'
        )
        runner, calls = self._custom_run((duplicate, duplicate, duplicate))
        with patch("subprocess.run", side_effect=runner):
            executor.execute(plan(ime=True))
        self.assertEqual(
            [c for c in calls if "ime" in c and "set" in c], []
        )

    def test_failure_after_ime_switch_restores_original(self) -> None:
        """切换后出现可恢复早退时执行有界原 IME 恢复并读回。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        wrong = (
            '<hierarchy><node text="合成联系人"><node text="消息" /></node>'
            '<node class="android.widget.EditText" text="别的" /></hierarchy>'
        )
        runner, calls = self._custom_run((BEFORE_XML, wrong, AFTER_XML))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(plan(ime=True))
        set_commands = [c for c in calls if "ime" in c and "set" in c]
        self.assertEqual(set_commands[0][-1], "com.example.synthetic/.Ime")
        self.assertEqual(set_commands[-1][-1], "com.example.default/.Ime")
        self.assertEqual(observation.ime_current, "com.example.default/.Ime")

    def test_identity_labels_reject_wrong_job(self) -> None:
        """同名但缺少目标职位标签时前置失败，不点击。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        labeled = replace(plan(), identity_labels=("错误职位",))
        runner, calls = self._custom_run((BEFORE_XML, BEFORE_XML, BEFORE_XML))
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(labeled)
        self.assertEqual([c for c in calls if "tap" in c], [])
        self.assertFalse(observation.clicked)

    def test_delete_requires_target_list_coverage_not_unrelated_button(self) -> None:
        """任意节点 scrollable=false 不能替代目标列表覆盖。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        incomplete = (
            '<hierarchy><node text="无关按钮" scrollable="false" />'
            '<node text="合成联系人"><node text="消息" /></node></hierarchy>'
        )
        delete = ActionPlan(
            action_key=f"{CONVERSATION}:delete",
            action_kind="delete_conversation",
            target_key=CONVERSATION,
            conversation_key=CONVERSATION,
            body_text=None,
            attachment_name=None,
            target_label="合成联系人",
            anchor_label="消息",
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=2,
            tap_points=((1, 2), (3, 4), (5, 6)),
        )
        runner, calls = self._custom_run((incomplete, incomplete, incomplete))
        with patch("subprocess.run", side_effect=runner):
            executor.execute(delete)
        self.assertEqual([c for c in calls if "tap" in c], [])

    def test_failure_before_click_does_not_retry(self) -> None:
        """首条命令非零退出即停止，只调用一次并标记未点击。"""
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
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
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
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
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
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
        executor = _ObservationExecutor(
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
                "adb_hierarchy_dump",
                "adb_hierarchy_cat",
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

    def test_gate_failure_logs_whitelisted_terminal(self) -> None:
        """门禁失败使用白名单类别并写 started→failed，不降级。"""
        class FailingGate(FakeGate):
            """verify 抛出固定门禁错误。"""

            def verify(self, *args: object, **kwargs: object) -> None:
                """不执行命令，直接失败。"""
                from app.ports.device_gate import DeviceGateError

                raise DeviceGateError("device_not_held")

        log = ProcessLog(path=helpers.ROOT / "data" / "logs" / "m5-gate-fail.jsonl")
        executor = _ObservationExecutor(
            serial="<serial>", gate=FailingGate(), process_log=log
        )
        with self.assertRaises(ActionExecutionError):
            executor.execute(plan())
        self.assertFalse(log.degraded)
        records = [
            __import__("json").loads(line)
            for line in log.path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(records[0]["result"], "started")
        self.assertEqual(records[-1]["result"], "failed")
        self.assertEqual(records[-1]["error_category"], "adb_gate")
        try:
            log.path.unlink()
        except OSError:
            pass

    def test_evidence_directory_saves_xml_and_index(self) -> None:
        """真实证据目录保存原始 XML 与 UUID/动作索引，且不含正文。"""
        gate = FakeGate()
        with tempfile.TemporaryDirectory() as directory:
            log = ProcessLog(path=Path(directory) / "action-evidence.jsonl")
            executor = _ObservationExecutor(
                serial="<serial>",
                gate=gate,
                evidence_dir=Path(directory),
                process_log=log,
            )
            runner, _calls = self._runner()
            with patch("subprocess.run", side_effect=runner):
                executor.execute(plan())
            files = list(Path(directory).glob("action-*.xml"))
            indexes = list(Path(directory).glob("action-*.index.jsonl"))
            self.assertEqual(len(files), 3)
            self.assertEqual(len(indexes), 1)
            index_text = indexes[0].read_text("utf-8")
            self.assertIn('"phase":"before"', index_text)
            self.assertIn('"phase":"after"', index_text)
            self.assertNotIn("您好", index_text)
            records = [
                __import__("json").loads(line)
                for line in log.path.read_text("utf-8").splitlines()
                if line.strip()
            ]
            evidence = [
                item for item in records if item["stage"] == "adb_evidence"
            ]
            self.assertEqual(
                [item["result"] for item in evidence],
                ["started", "ok", "started", "ok", "started", "ok"],
            )

    def test_platform_bound_rejects_unproven_family_and_accepts_stopped(self) -> None:
        """平台 Job 有界时以核验结果为准：残留活进程 unknown，空族才 stopped。"""
        import tempfile
        from pathlib import Path

        from app.adapters.adb.command import CommandIdentityStore, run_adb

        class Proc:
            """最小 Popen 替身。"""

            def __init__(self) -> None:
                """固定 PID 与成功返回。"""
                self.pid = 4100
                self.returncode = 0

            def communicate(self, timeout: float | None = None):
                """返回成功输出。"""
                return "ok", ""

            def kill(self) -> None:
                """无 kill。"""

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        class Bound:
            """记录终止/关闭并返回固定活动进程数的 Job 替身。"""

            def __init__(self, active: int, assign: bool = True) -> None:
                """保存活动数与加入结果。"""
                self._active = active
                self._assign = assign
                self.terminated = False
                self.closed = False

            def assign(self, pid: int) -> bool:
                """记录加入结果。"""
                return self._assign

            def resume(self, pid: int) -> bool:
                """替身进程无需真实恢复。"""
                return True

            def terminate(self) -> bool:
                """记录终止。"""
                self.terminated = True
                return True

            def active(self) -> int | None:
                """返回固定活动进程数。"""
                return self._active

            def close(self) -> None:
                """记录关闭。"""
                self.closed = True

        def run_case(active: int):
            """用给定平台活动数跑一次真实 run_adb + 真实账本。"""
            with tempfile.TemporaryDirectory() as directory:
                store = CommandIdentityStore(Path(directory))
                key = "boss:conv:test:resume"
                bound = Bound(active)

                def on_pid(pid: int) -> None:
                    """启动时枚举不到后代。"""
                    store.begin_family(key, pid, lambda p: [])

                def on_done(pid: int, bounded: bool, proven: bool) -> None:
                    """把平台有界/证明结果交给唯一账本闭合。"""
                    store.finish_family(
                        key, pid, lambda p: [], bounded=bounded, proven=proven
                    )

                with patch("subprocess.Popen", return_value=Proc()):
                    run_adb(
                        ["adb"],
                        gate=FakeGate(),
                        on_pid=on_pid,
                        on_done=on_done,
                        bound_factory=lambda: bound,
                        real_process_check=lambda process: True,
                    )
                state = store.check(key, 4100, lambda pid: "stopped")
                return state, bound

        state, bound = run_case(1)
        self.assertTrue(bound.terminated)
        self.assertTrue(bound.closed)
        self.assertEqual(state, "unknown")

        state, _bound = run_case(0)
        self.assertEqual(state, "stopped")

    def test_platform_bound_unavailable_stays_unknown_even_if_scan_sees_adult(self) -> None:
        """无平台有界约束时即使枚举到后代也不得闭合为 stopped。"""
        import tempfile
        from pathlib import Path

        from app.adapters.adb.command import CommandIdentityStore, run_adb

        class Proc:
            """最小 Popen 替身，记录是否被执行。"""

            def __init__(self) -> None:
                """固定 PID 与未执行标志。"""
                self.pid = 4100
                self.returncode = 0
                self.communicated = False
                self.killed = False

            def communicate(self, timeout: float | None = None):
                """正常执行才会用默认预算；收尾回收使用 1 秒。"""
                if timeout != 1:
                    self.communicated = True
                return "ok", ""

            def kill(self) -> None:
                """记录 kill。"""
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        class Bound:
            """加入 Job 失败。"""

            def assign(self, pid: int) -> bool:
                """返回失败。"""
                return False

            def resume(self, pid: int) -> bool:
                """不应被调用。"""
                raise AssertionError

            def close(self) -> None:
                """无关闭。"""

        from app.adapters.adb.command import AdbCommandFailure

        with tempfile.TemporaryDirectory() as directory:
            store = CommandIdentityStore(Path(directory))
            key = "boss:conv:test:resume"
            process = Proc()
            with patch("subprocess.Popen", return_value=process):
                with self.assertRaises(AdbCommandFailure) as caught:
                    run_adb(
                        ["adb"],
                        gate=FakeGate(),
                        on_start=lambda: store.mark_started(key),
                        on_pid=lambda pid: store.record(key, pid),
                        on_fail=lambda: store.record_unresolved(key),
                        bound_factory=lambda: Bound(),
                        real_process_check=lambda value: True,
                    )
            self.assertEqual(caught.exception.category, "adb_oserror")
            self.assertFalse(process.communicated)
            self.assertTrue(process.killed)
            self.assertEqual(
                store.check(key, 4100, lambda pid: "stopped"), "unknown"
            )

    def test_real_windows_job_bounds_synthetic_child(self) -> None:
        """真实本机 Job 正常约束自有合成 Python 子孙并在有界收尾后全停。"""
        if os.name != "nt":
            self.skipTest("Windows Job Object only")
        from app.adapters.adb.command import ProcessFamilyBound, run_adb
        from app.adapters.sqlite.repo import default_stop_checker

        probe = ProcessFamilyBound.create()
        if probe is None:
            self.skipTest("native Job unavailable")
        probe.close()
        script = (
            "import subprocess,sys\n"
            "c=subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(30)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "print(c.pid, flush=True)\n"
        )
        results: list[tuple[bool, bool]] = []
        completed = run_adb(
            [sys.executable, "-c", script],
            gate=FakeGate(),
            on_pid=lambda pid: None,
            on_done=lambda pid, bounded, proven: results.append(
                (bounded, proven)
            ),
        )
        self.assertEqual(results, [(True, True)])
        child = int(completed.stdout.strip().splitlines()[0])
        state = "unknown"
        for _ in range(40):
            state = default_stop_checker(child)
            if state == "stopped":
                break
            import time

            time.sleep(0.05)
        if state != "stopped":
            try:
                os.kill(child, signal.SIGTERM)
            except OSError:
                pass
        self.assertEqual(state, "stopped")

    def test_run_adb_unclosed_fact_survives_execution_start_interruption(self) -> None:
        """执行起点前写入未闭合事实；中断后旧完成命令不能冒充 stopped。"""
        import tempfile
        from pathlib import Path

        from app.adapters.adb.command import (
            AdbCommandFailure,
            CommandIdentityStore,
            run_adb,
        )

        class Bound:
            """成功 assign、记录终止/关闭；resume 失败即断言不应恢复执行。"""

            def __init__(self) -> None:
                """初始化标志。"""
                self.terminated = False
                self.closed = False

            def assign(self, pid: int) -> bool:
                """成功加入。"""
                return True

            def resume(self, pid: int) -> bool:
                """执行前中断不应恢复。"""
                raise AssertionError("resume must not run")

            def terminate(self) -> bool:
                """记录终止。"""
                self.terminated = True
                return True

            def close(self) -> None:
                """记录关闭。"""
                self.closed = True

        class Proc:
            """最小 Popen 替身。"""

            def __init__(self) -> None:
                """固定 PID。"""
                self.pid = 7301
                self.returncode = 0
                self.killed = False

            def communicate(self, timeout: float | None = None):
                """不应被调用。"""
                raise AssertionError

            def kill(self) -> None:
                """记录 kill。"""
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        with tempfile.TemporaryDirectory() as directory:
            store = CommandIdentityStore(Path(directory))
            key = "boss:conv:test:resume"
            store.mark_started(key)
            store.record(key, 7300)
            store.finish_family(key, 7300, lambda pid: [], bounded=True, proven=True)
            bound = Bound()
            process = Proc()

            def interrupted_start() -> None:
                """先写未闭合事实，再模拟 owner 级中断。"""
                store.mark_started(key)
                raise BaseException("owner_exit")

            with patch("subprocess.Popen", return_value=process):
                with self.assertRaises(AdbCommandFailure) as caught:
                    run_adb(
                        ["adb"],
                        gate=FakeGate(),
                        on_start=interrupted_start,
                        on_pid=lambda pid: None,
                        bound_factory=lambda: bound,
                        real_process_check=lambda value: True,
                    )
            self.assertEqual(caught.exception.category, "adb_oserror")
            self.assertFalse(process.killed)
            self.assertTrue(bound.closed)
            self.assertEqual(
                store.check(key, 7300, lambda pid: "stopped"), "unknown"
            )

            interrupted_pid = {"called": False}

            def interrupted_on_pid(pid: int) -> None:
                """assign 成功、恢复前中断。"""
                interrupted_pid["called"] = True
                store.record(key, pid)
                raise BaseException("owner_exit")

            bound2 = Bound()
            process2 = Proc()
            with patch("subprocess.Popen", return_value=process2):
                with self.assertRaises(AdbCommandFailure):
                    run_adb(
                        ["adb"],
                        gate=FakeGate(),
                        on_start=lambda: store.mark_started(key),
                        on_pid=interrupted_on_pid,
                        on_fail=lambda: None,
                        bound_factory=lambda: bound2,
                        real_process_check=lambda value: True,
                    )
            self.assertTrue(interrupted_pid["called"])
            self.assertTrue(process2.killed)
            self.assertTrue(bound2.closed)
            self.assertEqual(
                store.check(key, 7300, lambda pid: "stopped"), "unknown"
            )

    def test_run_adb_resume_failure_does_not_execute_and_closes(self) -> None:
        """resume 失败时不 communicate、不重发，未闭合事实保持 unknown。"""
        import tempfile
        from pathlib import Path

        from app.adapters.adb.command import (
            AdbCommandFailure,
            CommandIdentityStore,
            run_adb,
        )

        class Bound:
            """assign 成功、resume 失败。"""

            def __init__(self) -> None:
                """初始化标志。"""
                self.terminated = False
                self.closed = False

            def assign(self, pid: int) -> bool:
                """成功加入。"""
                return True

            def resume(self, pid: int) -> bool:
                """返回失败。"""
                return False

            def terminate(self) -> bool:
                """记录终止。"""
                self.terminated = True
                return True

            def close(self) -> None:
                """记录关闭。"""
                self.closed = True

        class Proc:
            """最小 Popen 替身。"""

            def __init__(self) -> None:
                """固定 PID。"""
                self.pid = 7400
                self.returncode = 0
                self.killed = False
                self.executed = False

            def communicate(self, timeout: float | None = None):
                """正常预算表示执行，回收预算表示收尾。"""
                if timeout != 1:
                    self.executed = True
                return "", ""

            def kill(self) -> None:
                """记录 kill。"""
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        with tempfile.TemporaryDirectory() as directory:
            store = CommandIdentityStore(Path(directory))
            key = "boss:conv:test:resume"
            bound = Bound()
            process = Proc()
            with patch("subprocess.Popen", return_value=process):
                with self.assertRaises(AdbCommandFailure):
                    run_adb(
                        ["adb"],
                        gate=FakeGate(),
                        on_start=lambda: store.mark_started(key),
                        on_pid=lambda pid: store.record(key, pid),
                        on_fail=lambda: store.record_unresolved(key),
                        bound_factory=lambda: bound,
                        real_process_check=lambda value: True,
                    )
            self.assertFalse(process.executed)
            self.assertTrue(process.killed)
            self.assertTrue(bound.terminated)
            self.assertTrue(bound.closed)
            self.assertEqual(
                store.check(key, 7400, lambda pid: "stopped"), "unknown"
            )

    def test_run_adb_double_timeout_and_oserror_always_finalize(self) -> None:
        """二次 communicate 超时、wait 再超时与 communicate OSError 都执行收尾。"""
        import tempfile
        from pathlib import Path

        from app.adapters.adb.command import (
            AdbCommandFailure,
            CommandIdentityStore,
            run_adb,
        )

        class Bound:
            """记录终止/关闭并证明空族。"""

            def __init__(self) -> None:
                """初始化标志。"""
                self.terminated = False
                self.closed = False

            def assign(self, pid: int) -> bool:
                """成功加入。"""
                return True

            def resume(self, pid: int) -> bool:
                """成功恢复。"""
                return True

            def terminate(self) -> bool:
                """记录终止。"""
                self.terminated = True
                return True

            def active(self) -> int:
                """证明空族。"""
                return 0

            def close(self) -> None:
                """记录关闭。"""
                self.closed = True

        class Proc:
            """按次数抛出超时或 OSError。"""

            def __init__(self, mode: str) -> None:
                """保存模式。"""
                self.mode = mode
                self.pid = 7500
                self.returncode = 0
                self.calls = 0
                self.killed = False

            def communicate(self, timeout: float | None = None):
                """模拟超时/OSError。"""
                self.calls += 1
                if timeout == 1:
                    raise subprocess.TimeoutExpired(["adb"], 1)
                if self.mode == "timeout":
                    raise subprocess.TimeoutExpired(["adb"], 5)
                raise OSError("synthetic")

            def kill(self) -> None:
                """记录 kill。"""
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                """模拟 wait 再超时。"""
                raise subprocess.TimeoutExpired(["adb"], 1)

        with tempfile.TemporaryDirectory() as directory:
            store = CommandIdentityStore(Path(directory))
            key = "boss:conv:test:resume"
            for mode, expected in (("timeout", "adb_timeout"), ("oserror", "adb_oserror")):
                with self.subTest(mode=mode):
                    bound = Bound()
                    process = Proc(mode)
                    done: list[tuple[bool, bool]] = []
                    with patch("subprocess.Popen", return_value=process):
                        with self.assertRaises(AdbCommandFailure) as caught:
                            run_adb(
                                ["adb"],
                                gate=FakeGate(),
                                on_start=lambda: store.mark_started(key),
                                on_pid=lambda pid: store.record(key, pid),
                                on_done=lambda pid, bounded, proven: done.append(
                                    (bounded, proven)
                                ),
                                on_fail=lambda: store.record_unresolved(key),
                                bound_factory=lambda: bound,
                                real_process_check=lambda value: True,
                            )
                    self.assertEqual(caught.exception.category, expected)
                    self.assertTrue(process.killed)
                    self.assertTrue(bound.terminated)
                    self.assertTrue(bound.closed)
                    self.assertTrue(done)

    def test_run_adb_records_descendant_tree_through_pid_callback(self) -> None:
        """实际 Popen 回调记录命令族子孙，末端口活时恢复检查拒绝。"""
        import tempfile
        from pathlib import Path

        from app.adapters.adb.command import CommandIdentityStore, run_adb

        class Proc:
            """最小 Popen 替身，只提供 run_adb 使用的属性。"""

            def __init__(self) -> None:
                """固定 PID 与成功返回。"""
                self.pid = 4100
                self.returncode = 0

            def communicate(self, timeout: float | None = None):
                """返回成功输出。"""
                return "ok", ""

            def kill(self) -> None:
                """无 kill。"""

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        with tempfile.TemporaryDirectory() as directory:
            store = CommandIdentityStore(Path(directory))
            tree = {4100: [4200], 4200: [4300], 4300: [4400], 4400: []}

            def on_pid(pid: int) -> None:
                """按生产回调记录命令族。"""
                store.record_tree(
                    "boss:conv:test:resume", pid, lambda p: list(tree.get(p, []))
                )

            with patch("subprocess.Popen", return_value=Proc()):
                run_adb(["adb"], gate=FakeGate(), on_pid=on_pid)
            self.assertEqual(
                store.identities("boss:conv:test:resume"),
                [4100, 4200, 4300, 4400],
            )
            states = {4100: "stopped", 4200: "stopped", 4300: "stopped", 4400: "alive"}
            self.assertEqual(
                store.check(
                    "boss:conv:test:resume", 4100, lambda pid: states.get(pid, "unknown")
                ),
                "alive",
            )

    def test_run_adb_pid_record_failure_is_bounded(self) -> None:
        """Popen 成功后身份持久化失败时有界收尾并返回固定错误。"""
        from app.adapters.adb.command import AdbCommandFailure, run_adb

        class Proc:
            """记录 kill 与等待预算。"""

            def __init__(self) -> None:
                """初始化成功 PID 与预算。"""
                self.pid = 4100
                self.returncode = 0
                self.killed = False
                self.wait_budget: float | None = None

            def communicate(self, timeout: float | None = None):
                """不应被调用。"""
                raise AssertionError

            def kill(self) -> None:
                """记录 kill。"""
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                """记录有界等待。"""
                self.wait_budget = timeout
                return 0

        process = Proc()

        def fail(pid: int) -> None:
            """模拟身份写入失败。"""
            raise OSError("synthetic")

        with patch("subprocess.Popen", return_value=process):
            with self.assertRaises(AdbCommandFailure) as caught:
                run_adb(["adb"], gate=FakeGate(), on_pid=fail)
        self.assertEqual(caught.exception.category, "adb_oserror")
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_budget, 1)

    def test_run_adb_timeout_kill_and_reap_are_bounded(self) -> None:
        """超时后 kill 与回收都带预算，不使用无限等待。"""
        from app.adapters.adb.command import AdbCommandFailure, run_adb

        class Proc:
            """首次 communicate 超时，后续立即返回。"""

            def __init__(self) -> None:
                """初始化预算记录。"""
                self.pid = 4100
                self.returncode = 0
                self.budgets: list[float | None] = []
                self.killed = False

            def communicate(self, timeout: float | None = None):
                """记录每次等待预算并模拟首次超时。"""
                self.budgets.append(timeout)
                if len(self.budgets) == 1:
                    raise subprocess.TimeoutExpired(["adb"], timeout or 0)
                return "", ""

            def kill(self) -> None:
                """记录 kill。"""
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                """立即回收。"""
                return 0

        process = Proc()
        with patch("subprocess.Popen", return_value=process):
            with self.assertRaises(AdbCommandFailure) as caught:
                run_adb(["adb"], gate=FakeGate(), on_pid=lambda pid: None)
        self.assertEqual(caught.exception.category, "adb_timeout")
        self.assertTrue(process.killed)
        self.assertEqual(process.budgets, [5, 1])

    def test_recovery_observer_rejects_malformed_xml(self) -> None:
        """坏层级不能得到 scene_settled 收敛。"""
        class BadScreen:
            """返回非 XML。"""

            def dump_hierarchy(self, serial: str) -> HierarchyCapture:
                """返回坏文本。"""
                return HierarchyCapture("not-xml", "bad.xml", 7)

            def capture_screen(self, serial: str) -> object:
                """未使用。"""
                raise AssertionError

        class ActionReader:
            """返回固定 IME。"""

            def read_current_ime(self, *, before_command=None) -> str:
                """返回原 IME。"""
                return "ime"

            def execute(self, plan, *, before_command=None):
                """未使用。"""
                raise AssertionError

        observer = AdbRecoveryObserver(
            serial="<serial>",
            gate=FakeGate(),
            screen_reader=BadScreen(),
            action_reader=ActionReader(),
        )
        observation = observer.observe(
            action_key=f"{CONVERSATION}:resume",
            action_kind="send_resume",
            resolution="confirm_not_performed",
            evidence=None,
            ime_original="ime",
            command_stopped=True,
            confirmed=True,
            observed_at=helpers.NOW,
        )
        self.assertFalse(observation.scene_settled)

    def test_recovery_observer_reads_scene_and_ime(self) -> None:
        """生产恢复观察一次读取层级与 IME，命令未停止时不标 command_quiet。"""
        class Screen:
            """返回固定层级。"""

            def dump_hierarchy(self, serial: str) -> HierarchyCapture:
                """返回含 IME 空编辑框的层级。"""
                return HierarchyCapture(
                    xml_text=(
                        '<hierarchy><node class="android.widget.EditText" text="" />'
                        '<node text="消息" /></hierarchy>'
                    ),
                    evidence_name="synthetic.xml",
                    byte_size=1,
                )

            def capture_screen(self, serial: str) -> object:
                """未使用。"""
                raise AssertionError

        class ActionReader:
            """返回固定当前 IME。"""

            def read_current_ime(self, *, before_command=None) -> str:
                """返回原 IME。"""
                return "com.example.default/.Ime"

            def execute(self, plan, *, before_command=None):
                """未使用。"""
                raise AssertionError

        observer = AdbRecoveryObserver(
            serial="<serial>",
            gate=FakeGate(),
            screen_reader=Screen(),
            action_reader=ActionReader(),
        )
        observation = observer.observe(
            action_key=f"{CONVERSATION}:resume",
            action_kind="send_resume",
            resolution="confirm_not_performed",
            evidence=None,
            ime_original="com.example.default/.Ime",
            command_stopped=False,
            confirmed=True,
            observed_at=helpers.NOW,
        )
        self.assertFalse(observation.command_quiet)
        self.assertTrue(observation.scene_settled)
        self.assertEqual(observation.ime_current, "com.example.default/.Ime")

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


class LedgerBoundaryTests(unittest.TestCase):
    """执行边界必须在任何 ADB 命令/IME/点击前拒绝缺失或未绑定的命令账本。"""

    def _assert_refused_before_commands(self, executor) -> ActionExecutionError:
        """断言拒绝发生在零 subprocess/零门禁复核，且不虚构收敛事实。"""
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            with self.assertRaises(ActionExecutionError) as caught:
                executor.execute(plan())
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(caught.exception.reason_code, "executor_interrupted")
        self.assertFalse(caught.exception.clicked)
        self.assertFalse(caught.exception.scene_settled)
        self.assertFalse(caught.exception.input_box_empty)
        self.assertFalse(caught.exception.command_quiet)
        self.assertFalse(caught.exception.ime_restored)
        return caught.exception

    def test_missing_command_store_rejects_before_any_command(self) -> None:
        """缺少 command_store 时即使已绑定动作也在零命令处拒绝。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        executor.bind_action(TEXT_KEY)
        self._assert_refused_before_commands(executor)
        self.assertEqual(gate.verified, 0)

    def test_missing_action_binding_rejects_even_with_store(self) -> None:
        """有账本但从未 bind_action 时同样在零命令处拒绝。"""
        from app.adapters.adb.command import CommandIdentityStore

        gate = FakeGate()
        with tempfile.TemporaryDirectory() as directory:
            executor = AdbActionExecutor(
                serial="<serial>",
                gate=gate,
                command_store=CommandIdentityStore(Path(directory)),
            )
            self._assert_refused_before_commands(executor)
        self.assertEqual(gate.verified, 0)

    def test_mismatched_action_binding_rejects_before_any_command(self) -> None:
        """绑定到别的动作键时不得把命令记入错误动作族。"""
        from app.adapters.adb.command import CommandIdentityStore

        gate = FakeGate()
        with tempfile.TemporaryDirectory() as directory:
            executor = AdbActionExecutor(
                serial="<serial>",
                gate=gate,
                command_store=CommandIdentityStore(Path(directory)),
            )
            executor.bind_action(f"{CONVERSATION}:resume")
            self._assert_refused_before_commands(executor)
            self.assertEqual(
                CommandIdentityStore(Path(directory)).identities(
                    TEXT_KEY
                ),
                [],
            )
        self.assertEqual(gate.verified, 0)

    def test_bound_action_ime_read_requires_ledger(self) -> None:
        """已绑定动作的 IME 预读不得借 reconcile 独立读绕过账本。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        executor.bind_action(TEXT_KEY)
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            with self.assertRaises(ActionExecutionError) as caught:
                executor.read_current_ime()
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(caught.exception.reason_code, "executor_interrupted")
        self.assertFalse(caught.exception.clicked)
        self.assertFalse(caught.exception.command_quiet)
        self.assertEqual(gate.verified, 0)

    def test_unbound_reconcile_ime_read_stays_allowed(self) -> None:
        """未绑定动作的 reconcile 维护读取仍可不带账本执行一次。"""
        gate = FakeGate()
        executor = AdbActionExecutor(serial="<serial>", gate=gate)
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="com.example/.Ime\n", stderr=""
            ),
        ) as run:
            self.assertEqual(executor.read_current_ime(), "com.example/.Ime")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(gate.verified, 1)

    def test_before_index_failure_keeps_clicked_false_and_no_tap(self) -> None:
        """before 证据索引真实失败时不得固定 clicked=True，也不虚构命令已停。"""
        import hashlib
        import tempfile as temp

        gate = FakeGate()
        with temp.TemporaryDirectory() as directory:
            root = Path(directory)
            index_name = (
                "action-"
                + hashlib.sha256(TEXT_KEY.encode("utf-8")).hexdigest()[:16]
                + ".index.jsonl"
            )
            (root / index_name).mkdir()
            executor = _ObservationExecutor(
                serial="<serial>",
                gate=gate,
                evidence_dir=root,
            )
            runner, calls = AdbActionPrimitiveTests()._runner()
            with patch("subprocess.run", side_effect=runner):
                with self.assertRaises(ActionExecutionError) as caught:
                    executor.execute(plan())
        self.assertFalse(caught.exception.clicked)
        self.assertFalse(caught.exception.scene_settled)
        self.assertFalse(caught.exception.input_box_empty)
        self.assertFalse(caught.exception.command_quiet)
        self.assertFalse(caught.exception.ime_restored)
        self.assertEqual([c for c in calls if "tap" in c], [])
        observation = ActionObservation(
            action_key=TEXT_KEY,
            action_kind="send_text",
            **helpers.observation(
                clicked=False,
                error="executor_interrupted",
                command_quiet=False,
                scene_settled=False,
                input_box_empty=False,
            ),
        )
        outcome = derive_action_outcome(observation)
        self.assertEqual(outcome.evidence.outcome, "not_performed")
        self.assertTrue(outcome.pause)

    def test_after_index_failure_keeps_clicked_true_and_tap_count(self) -> None:
        """after 证据索引真实失败时保留一次点击事实，调用方必须收敛 unknown。"""
        import hashlib
        import tempfile as temp

        gate = FakeGate()
        with temp.TemporaryDirectory() as directory:
            root = Path(directory)
            index_name = (
                "action-"
                + hashlib.sha256(TEXT_KEY.encode("utf-8")).hexdigest()[:16]
                + ".index.jsonl"
            )
            index_path = root / index_name
            runner, calls = AdbActionPrimitiveTests()._runner()

            def run(args: list[str], **kwargs: object):
                """在 tap 后把索引文件替换为目录，制造 after 阶段真实冲突。"""
                if "tap" in args:
                    if index_path.exists():
                        index_path.unlink()
                    index_path.mkdir()
                return runner(args, **kwargs)

            executor = _ObservationExecutor(
                serial="<serial>",
                gate=gate,
                evidence_dir=root,
            )
            with patch("subprocess.run", side_effect=run):
                with self.assertRaises(ActionExecutionError) as caught:
                    executor.execute(plan())
            self.assertTrue(index_path.is_dir())
        self.assertTrue(caught.exception.clicked)
        self.assertFalse(caught.exception.command_quiet)
        self.assertEqual(len([c for c in calls if "tap" in c]), 1)


    def test_readback_index_failure_keeps_input_and_quiet_unset(self) -> None:
        """readback 索引失败保留已输入事实，不按 idle 推断输入为空或命令已停。"""
        import hashlib
        import tempfile as temp

        gate = FakeGate()
        with temp.TemporaryDirectory() as directory:
            root = Path(directory)
            index_name = (
                "action-"
                + hashlib.sha256(TEXT_KEY.encode("utf-8")).hexdigest()[:16]
                + ".index.jsonl"
            )
            index_path = root / index_name
            runner, calls = AdbActionPrimitiveTests()._runner()

            def run(args: list[str], **kwargs: object):
                """在 input text 后把索引文件替换为目录，制造 readback 真实冲突。"""
                if "input" in args and "text" in args:
                    if index_path.exists():
                        index_path.unlink()
                    index_path.mkdir()
                return runner(args, **kwargs)

            executor = _ObservationExecutor(
                serial="<serial>",
                gate=gate,
                evidence_dir=root,
            )
            with patch("subprocess.run", side_effect=run):
                with self.assertRaises(ActionExecutionError) as caught:
                    executor.execute(plan())
            self.assertTrue(index_path.is_dir())
        self.assertFalse(caught.exception.clicked)
        self.assertFalse(caught.exception.scene_settled)
        self.assertFalse(caught.exception.input_box_empty)
        self.assertFalse(caught.exception.command_quiet)
        self.assertEqual(len([c for c in calls if "text" in c]), 1)
        self.assertEqual([c for c in calls if "tap" in c], [])


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

class ApplyExecutorRawXmlTests(unittest.TestCase):
    """真实 AdbActionExecutor 经原始 XML 发现 apply 控件与前后差分。"""

    def _run(self, texts, calls):
        """按 cat 阶段返回原始 XML 的合成 adb runner。"""
        state = {"cat": 0}

        def runner(args, **kwargs):
            """返回指定阶段 XML，不连接真实设备。"""
            calls.append(list(args))
            if "uiautomator" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if "cat" in args:
                index = state["cat"]
                state["cat"] = index + 1
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=texts[min(index, 1)], stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        return runner

    def test_raw_xml_apply_control_and_before_after_change(self) -> None:
        """控件证据来自 XML；caller tap 坐标不参与，after 已申请判 applied。"""
        before = (
            '<hierarchy><node text="合成岗位甲"><node text="合成公司甲" />'
            '<node text="立即申请" clickable="true" bounds="[700,2100][1040,2300]" />'
            '</node></hierarchy>'
        )
        after = (
            '<hierarchy><node text="合成岗位甲"><node text="合成公司甲" />'
            '<node text="已申请" clickable="false" bounds="[700,2100][1040,2300]" />'
            '</node></hierarchy>'
        )
        calls: list[list[str]] = []
        gate = FakeGate()
        executor = _ObservationExecutor(serial="<serial>", gate=gate)
        action_key = "boss:boss:job:" + canonical_hash({"job": "raw"}) + ":apply"
        plan_value = ActionPlan(
            action_key=action_key,
            action_kind="apply_job",
            target_key="boss:job:" + canonical_hash({"job": "raw"}),
            conversation_key="boss:job:" + canonical_hash({"job": "raw"}),
            body_text=None,
            attachment_name=None,
            target_label="合成岗位甲",
            anchor_label=None,
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=0,
            tap_points=((1, 1),),
            identity_labels=("合成公司甲",),
        )
        runner = self._run((before, after), calls)
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(plan_value)
        self.assertTrue(observation.apply_available_before)
        self.assertTrue(observation.apply_completed_after)
        self.assertEqual(derive_action_outcome(observation).evidence.effect, "applied")
        tap_calls = [call for call in calls if "tap" in call]
        self.assertEqual(tap_calls[0][-2:], ["870", "2200"])

    def test_raw_xml_missing_apply_control_has_zero_taps(self) -> None:
        """before 子树没有显式申请控件时零点击，caller tap_points 不得替代。"""
        before = (
            '<hierarchy><node text="合成岗位甲"><node text="合成公司甲" />'
            '<node text="职位详情" bounds="[700,2100][1040,2300]" />'
            '</node></hierarchy>'
        )
        calls: list[list[str]] = []
        executor = _ObservationExecutor(serial="<serial>", gate=FakeGate())
        plan_value = ActionPlan(
            action_key="boss:boss:job:" + canonical_hash({"job": "raw-none"}) + ":apply",
            action_kind="apply_job",
            target_key="boss:job:" + canonical_hash({"job": "raw-none"}),
            conversation_key="boss:job:" + canonical_hash({"job": "raw-none"}),
            body_text=None,
            attachment_name=None,
            target_label="合成岗位甲",
            anchor_label=None,
            ime_component=None,
            ime_original=None,
            irreversible_tap_index=0,
            tap_points=((1, 1),),
            identity_labels=("合成公司甲",),
        )
        runner = self._run((before, before), calls)
        with patch("subprocess.run", side_effect=runner):
            observation = executor.execute(plan_value)
        self.assertFalse(observation.apply_available_before)
        self.assertFalse(observation.clicked)
        outcome = derive_action_outcome(observation)
        self.assertEqual(outcome.evidence.outcome, "not_performed")
        self.assertEqual(outcome.evidence.reason_code, "precondition_failed")
        self.assertFalse(any("tap" in call for call in calls))
