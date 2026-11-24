"""实现 M5 真实 ADB 动作原语与一次有界执行，不重试且每条命令前复核共享门禁。"""

from __future__ import annotations

import subprocess
import time
import uuid
import re

from app.domain.actions import ActionObservation
from app.ports.actions import ActionExecutionError, ActionPlan
from app.ports.device_gate import DeviceGateError
from app.ports.screen import DeviceGate
from app.process_log import ProcessLog, elapsed_ms

ADB_ACTION_TIMEOUT_SECONDS = 5


class AdbActionExecutor:
    """通过参数数组、shell=false 与单次 attempt 执行动作原语并构造观察。"""

    def __init__(
        self,
        *,
        serial: str,
        gate: DeviceGate,
        adb: str = "adb",
        process_log: ProcessLog | None = None,
        timeout: float = ADB_ACTION_TIMEOUT_SECONDS,
    ) -> None:
        """保存 serial、共享门禁、adb 名、日志与固定超时；构造不启动子进程。"""
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("serial is required")
        self._serial = serial
        self._gate = gate
        self._adb = adb
        self._process_log = process_log
        self._timeout = timeout

    def read_current_ime(self) -> str:
        """读取当前输入法标识；失败不重试并归为断连。"""
        text = self._run(
            [self._adb, "-s", self._serial, "shell", "settings", "get", "secure", "default_input_method"],
            clicked=False,
        )
        value = text.strip()
        if not value:
            raise ActionExecutionError("device_disconnected", clicked=False, scene_settled=True, input_box_empty=True, command_quiet=True, ime_restored=True)
        return value

    def execute(self, plan: ActionPlan) -> ActionObservation:
        """顺序执行输入与点击一次，之后读取现场并尽力恢复原 IME。"""
        before_xml = self._read_hierarchy(clicked=False)
        clicked = False
        try:
            if plan.action_kind == "send_text":
                if plan.body_text is None:
                    raise ActionExecutionError("executor_interrupted", clicked=False)
                self._run(
                    [
                        self._adb,
                        "-s",
                        self._serial,
                        "shell",
                        "input",
                        "text",
                        _escape_input_text(plan.body_text),
                    ],
                    clicked=False,
                    phase="input",
                )
            for index, (x, y) in enumerate(plan.tap_points):
                is_irreversible = index >= plan.irreversible_tap_index
                clicked = clicked or is_irreversible
                self._run(
                    [self._adb, "-s", self._serial, "shell", "input", "tap", str(x), str(y)],
                    clicked=is_irreversible,
                    phase="clicked" if is_irreversible else "input",
                )
            after_xml = self._read_hierarchy(clicked=clicked)
        except DeviceGateError:
            raise ActionExecutionError(
                "device_disconnected",
                clicked=clicked,
                scene_settled=not clicked,
                input_box_empty=not clicked,
                command_quiet=False,
                ime_restored=not clicked,
            ) from None

        ime_current = plan.ime_original
        if plan.ime_component is not None and plan.ime_original is not None:
            try:
                self._run(
                    [
                        self._adb,
                        "-s",
                        self._serial,
                        "shell",
                        "ime",
                        "set",
                        plan.ime_original,
                    ],
                    clicked=clicked,
                    phase="clicked" if clicked else "idle",
                )
                ime_current = self.read_current_ime()
            except (ActionExecutionError, DeviceGateError):
                ime_current = None

        confirmation_open = "确认" in after_xml and "取消" in after_xml
        body = plan.body_text or ""
        target = plan.target_label or ""
        anchor = plan.anchor_label or ""
        return ActionObservation(
            action_key=plan.action_key,
            action_kind=plan.action_kind,
            before_snapshot_id=str(uuid.uuid4()),
            after_snapshot_id=str(uuid.uuid4()),
            observed_at=_utc_now(),
            clicked=clicked,
            error=None,
            scene_settled=not confirmation_open,
            command_quiet=True,
            confirmation_open=confirmation_open,
            input_box_empty=_edit_text_empty(after_xml),
            ime_original=plan.ime_original,
            ime_current=ime_current,
            body_readback_matches=bool(body) and body in after_xml,
            echo_count_before=before_xml.count(body) if body else 0,
            echo_count_after=after_xml.count(body) if body else 0,
            attachment_name_matched=bool(plan.attachment_name)
            and plan.attachment_name in after_xml,
            attachment_request_observed="附件简历请求已发送" in after_xml,
            delivery_observed="附件简历已发送" in after_xml,
            contact_slot_available=bool(target) and target in before_xml,
            contact_slot_status=(
                "requested" if "请求中" in after_xml else "none"
            )
            if plan.action_kind in {"change_wechat", "change_phone"}
            else None,
            identity_unique=bool(target) and before_xml.count(target) == 1,
            target_present_before=bool(target) and target in before_xml,
            target_present_after=bool(target) and target in after_xml,
            anchor_present_after=bool(anchor) and anchor in after_xml,
        )

    def _read_hierarchy(self, *, clicked: bool) -> str:
        """执行一次 dump 后 cat；两步都复核门禁且不重试。"""
        self._run(
            [
                self._adb,
                "-s",
                self._serial,
                "shell",
                "uiautomator",
                "dump",
                "/data/local/tmp/m5-action-ui.xml",
            ],
            clicked=clicked,
        )
        return self._run(
            [
                self._adb,
                "-s",
                self._serial,
                "shell",
                "cat",
                "/data/local/tmp/m5-action-ui.xml",
            ],
            clicked=clicked,
        )

    def _run(self, args: list[str], *, clicked: bool, phase: str = "idle") -> str:
        """每条子命令前复核共享门禁，单次有界执行；失败立即停止且不重发。"""
        self._gate.verify()
        started = time.perf_counter()
        self._record("started", 0)
        try:
            completed = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            self._record_failure(started, "adb_missing")
            raise ActionExecutionError(
                "device_disconnected", clicked=clicked, **_phase_flags(phase)
            ) from None
        except subprocess.TimeoutExpired:
            self._record_failure(started, "adb_timeout")
            raise ActionExecutionError(
                "device_disconnected", clicked=clicked, **_phase_flags(phase)
            ) from None
        except OSError:
            self._record_failure(started, "adb_oserror")
            raise ActionExecutionError(
                "device_disconnected", clicked=clicked, **_phase_flags(phase)
            ) from None
        if completed.returncode != 0:
            self._record_failure(started, "adb_nonzero")
            raise ActionExecutionError(
                "device_disconnected", clicked=clicked, **_phase_flags(phase)
            )
        self._record("ok", elapsed_ms(started))
        return completed.stdout

    def _record(self, result: str, duration_ms: int) -> None:
        """写固定 action/adb_action 事件，不写 serial、命令参数或现场原文。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage="action",
                result=result,
                operation="perform",
                duration_ms=duration_ms,
                command="adb_action",
            )
        except Exception:
            return

    def _record_failure(self, started: float, category: str) -> None:
        """记录固定 ADB 错误类别，业务原因仍由调用方收敛为断连。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage="action",
                result="failed",
                operation="perform",
                reason_code="device_disconnected",
                error_category=category,
                duration_ms=elapsed_ms(started),
                command="adb_action",
            )
        except Exception:
            return


def _phase_flags(phase: str) -> dict[str, bool]:
    """按命令阶段给出可证明的现场标志；无法证明的收敛保持 False。"""
    if phase not in {"idle", "input", "clicked"}:
        raise ValueError("phase is invalid")
    return {
        "scene_settled": phase == "idle",
        "input_box_empty": phase == "idle",
        "command_quiet": False,
        "ime_restored": phase == "idle",
    }


def _edit_text_empty(after_xml: str) -> bool:
    """从层级文本判断输入框是否为空；无法判断时保守为 False。"""
    nodes = re.findall(
        r'<node[^>]*class="android\.widget\.EditText"[^>]*>', after_xml
    )
    if not nodes:
        return True
    for node in nodes:
        match = re.search(r'text="([^"]*)"', node)
        if match is not None and match.group(1):
            return False
    return True


def _escape_input_text(value: str) -> str:
    """把空格编码为 adb input text 的空格转义；拒绝换行与控制字符。"""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ActionExecutionError("executor_interrupted", clicked=False)
    return value.replace(" ", "%s")


def _utc_now() -> str:
    """返回毫秒精度 UTC 字符串，供合成观察时间使用。"""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
