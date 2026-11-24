"""实现 M5 恢复前的有界现场读取与当前 IME 核验，输出人工对账观察。"""

from __future__ import annotations

from app.adapters.adb.actions import _editor_texts, _node_texts
from app.domain.actions import ReconciliationObservation
from app.domain.models import Evidence
from app.ports.actions import DeviceActionPort
from app.ports.screen import DeviceGate, HierarchyCapture, ScreenPort


class AdbRecoveryObserver:
    """在共享门禁内一次读取层级与当前 IME，供生产恢复核对。"""

    def __init__(
        self,
        *,
        serial: str,
        gate: DeviceGate,
        screen_reader: ScreenPort,
        action_reader: DeviceActionPort,
    ) -> None:
        """保存 serial、共享门禁、既有采集器与 IME 读取原语。"""
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("serial is required")
        self._serial = serial
        self._gate = gate
        self._screen = screen_reader
        self._actions = action_reader

    def observe(
        self,
        *,
        action_key: str,
        action_kind: str,
        resolution: str,
        evidence: Evidence | None,
        ime_original: str | None,
        command_stopped: bool,
        confirmed: bool,
        observed_at: str,
    ) -> ReconciliationObservation:
        """一次有界采集后构造对账观察；命令未证明停止时 command_quiet=False。"""
        self._gate.verify()
        capture: HierarchyCapture = self._screen.dump_hierarchy(self._serial)
        texts = _node_texts(capture.xml_text)
        confirmation_open = "确认" in texts and "取消" in texts
        editors = _editor_texts(capture.xml_text)
        input_box_empty = (
            editors is not None and all(not value for value in editors)
            if action_kind == "send_text"
            else True
        )
        ime_current = self._actions.read_current_ime()
        return ReconciliationObservation(
            action_key=action_key,
            resolution=resolution,
            observed_at=observed_at,
            evidence=evidence,
            command_quiet=command_stopped,
            scene_settled=not confirmation_open and input_box_empty,
            confirmation_open=confirmation_open,
            ime_original=ime_original,
            ime_current=ime_current,
            matched_action_key=action_key,
            confirmed=confirmed,
        )
