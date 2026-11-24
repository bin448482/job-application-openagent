"""实现 M5 真实 ADB 动作原语：前置验证、一次有界执行、动作特有差分与 IME 闭环。"""

from __future__ import annotations

import re
import time
import uuid
import json
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from app.adapters.adb.command import (
    ADB_TIMEOUT_SECONDS,
    AdbCommandFailure,
    CommandIdentityStore,
    run_adb,
)
from app.adapters.adb.screen import AdbScreenReader
from app.domain.actions import ActionObservation
from app.ports.actions import ActionExecutionError, ActionPlan
from app.ports.device_gate import DeviceGateError
from app.ports.screen import DeviceGate, ScreenReadError
from app.process_log import ProcessLog, elapsed_ms

DEVICE_TMP_XML = "/data/local/tmp/m5-action-ui.xml"
PROTECTION_MARKERS = ("草稿", "未读", "待处理", "待发送")
RESUME_REQUEST_MARKER = "附件简历请求已发送"
RESUME_DELIVERED_MARKER = "附件简历已发送"
CONTACT_REQUEST_MARKER = "请求中"


class AdbActionExecutor:
    """通过共享 ADB 边界执行动作原语，前置不成立时不发不可逆命令。"""

    def __init__(
        self,
        *,
        serial: str,
        gate: DeviceGate,
        adb: str = "adb",
        process_log: ProcessLog | None = None,
        timeout: float = ADB_TIMEOUT_SECONDS,
        evidence_dir: Path | None = None,
        screen_reader: AdbScreenReader | None = None,
        command_store: CommandIdentityStore | None = None,
    ) -> None:
        """保存 serial、共享门禁、adb 名、日志、超时、证据目录、采集器与命令账本。"""
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("serial is required")
        self._serial = serial
        self._gate = gate
        self._adb = adb
        self._process_log = process_log
        self._timeout = timeout
        self._evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
        self._command_store = command_store
        self._current_action_key: str | None = None
        self._screen = screen_reader or AdbScreenReader(
            evidence_dir=self._evidence_dir,
            adb=adb,
            process_log=process_log,
            gate=gate,
        )

    def read_current_ime(
        self, *, before_command: Callable[[], None] | None = None
    ) -> str:
        """读取当前输入法标识；失败不重试并归为断连。"""
        text = self._run(
            [
                self._adb,
                "-s",
                self._serial,
                "shell",
                "settings",
                "get",
                "secure",
                "default_input_method",
            ],
            clicked=False,
            phase="idle",
            command="adb_action_ime_get",
            before_command=before_command,
        )
        value = text.strip()
        if not value:
            raise ActionExecutionError(
                "device_disconnected",
                clicked=False,
                scene_settled=True,
                input_box_empty=True,
                command_quiet=True,
                ime_restored=True,
            )
        return value

    def execute(
        self,
        plan: ActionPlan,
        *,
        before_command: Callable[[], None] | None = None,
    ) -> ActionObservation:
        """先验证动作前置，再执行一次；只接受动作目标对应的前后新增变化。"""
        self._current_action_key = plan.action_key
        before_xml = self._read_hierarchy(
            plan.action_key,
            phase="before",
            clicked=False,
            before_command=before_command,
        )
        before_texts = _node_texts(before_xml)
        target = plan.target_label or ""
        anchor = plan.anchor_label or ""
        identity_unique = bool(target) and before_texts.count(target) == 1
        target_present_before = bool(target) and target in before_texts
        labels = plan.identity_labels
        if labels and _target_node(before_xml, target, labels) is None:
            identity_unique = False
        protections_clear = not any(
            marker in text
            for text in _target_scope_texts(before_xml, target, labels)
            for marker in PROTECTION_MARKERS
        )
        list_complete = _list_complete(before_xml)
        contact_slot_available = _slot_available(
            before_xml, target, plan.action_kind, labels
        )
        if plan.action_kind != "send_text" and plan.irreversible_tap_index == 0:
            return self._preflight_failure(
                plan, before_xml, ime_current=plan.ime_original
            )

        ime_current = plan.ime_original

        if plan.action_kind == "send_text":
            text_readback_verified = False
            if plan.body_text is None:
                raise ActionExecutionError("executor_interrupted", clicked=False)
            if not identity_unique:
                return self._preflight_failure(
                    plan, before_xml, ime_current=ime_current
                )
            if plan.ime_component is not None:
                switched = self._switch_ime(
                    plan.ime_component,
                    plan.ime_original,
                    before_command=before_command,
                )
                if switched is None:
                    return self._preflight_failure(
                        plan,
                        before_xml,
                        ime_current=None,
                        input_box_empty=True,
                    )
                ime_current = switched
            try:
                readback_xml = self._input_and_readback(
                    plan, before_command=before_command
                )
            except (ActionExecutionError, DeviceGateError) as error:
                restored = self._restore_ime(
                    plan.ime_original, before_command=before_command
                )
                reason_code = getattr(error, "reason_code", "device_disconnected")
                raise ActionExecutionError(
                    reason_code,
                    clicked=False,
                    scene_settled=True,
                    input_box_empty=False,
                    command_quiet=True,
                    ime_restored=(
                        plan.ime_original is None
                        or restored == plan.ime_original
                    ),
                ) from None
            readback_texts = _node_texts(readback_xml)
            body = plan.body_text
            editors = _editor_texts_in_target(
                readback_xml, target, plan.identity_labels
            )
            body_readback = (
                editors is not None
                and len(editors) == 1
                and editors[0] == body
                and readback_texts.count(target) == 1
            )
            text_readback_verified = body_readback
            if not body_readback:
                return self._observation(
                    plan,
                    clicked=False,
                    before_texts=before_texts,
                    after_texts=readback_texts,
                    after_xml=readback_xml,
                    ime_current=self._restore_ime(
                        plan.ime_original, before_command=before_command
                    ),
                    body_readback_matches=False,
                    identity_unique=identity_unique,
                    list_complete=list_complete,
                    protections_clear=protections_clear,
                    target_present_before=target_present_before,
                    target_present_after=target_present_before,
                    attachment_name_matched=False,
                    contact_slot_available=contact_slot_available,
                )
        else:
            if not identity_unique:
                return self._preflight_failure(
                    plan, before_xml, ime_current=ime_current
                )
            if plan.action_kind == "delete_conversation" and not (
                list_complete and protections_clear and target_present_before
            ):
                return self._preflight_failure(
                    plan, before_xml, ime_current=ime_current
                )
            if plan.action_kind in {"change_wechat", "change_phone"} and not (
                contact_slot_available
            ):
                return self._preflight_failure(
                    plan, before_xml, ime_current=ime_current
                )
            if plan.action_kind == "send_resume" and not plan.attachment_name:
                raise ActionExecutionError("executor_interrupted", clicked=False)

        clicked = False
        attachment_verified = False
        try:
            if plan.action_kind == "send_text":
                self._tap(plan, 0, before_command=before_command)
                clicked = True
            else:
                for index in range(len(plan.tap_points)):
                    is_irreversible = index >= plan.irreversible_tap_index
                    self._tap(
                        plan,
                        index,
                        before_command=before_command,
                        clicked_before=clicked,
                    )
                    if is_irreversible:
                        clicked = True
                        break
                    if index == plan.irreversible_tap_index - 1:
                        dialog_xml = self._read_hierarchy(
                            plan.action_key,
                            phase="dialog",
                            clicked=False,
                            before_command=before_command,
                        )
                        dialog_texts = _node_texts(dialog_xml)
                        dialog_identity = (
                            _target_node(
                                dialog_xml, target, plan.identity_labels
                            )
                            is not None
                        )
                        if plan.action_kind == "send_resume":
                            if (
                                not dialog_identity
                                or not plan.attachment_name
                                or plan.attachment_name not in dialog_texts
                            ):
                                return self._observation(
                                    plan,
                                    clicked=False,
                                    before_texts=before_texts,
                                    after_texts=dialog_texts,
                                    after_xml=dialog_xml,
                                    ime_current=ime_current,
                                    body_readback_matches=False,
                                    identity_unique=identity_unique,
                                    list_complete=list_complete,
                                    protections_clear=protections_clear,
                                    target_present_before=target_present_before,
                                    target_present_after=target_present_before,
                                    attachment_name_matched=False,
                                    contact_slot_available=contact_slot_available,
                                    force_input_box_empty=True,
                                )
                            attachment_verified = True
                        elif plan.action_kind == "delete_conversation":
                            dialog_protections_clear = not any(
                                marker in text
                                for text in _target_scope_texts(
                                    dialog_xml, target, plan.identity_labels
                                )
                                for marker in PROTECTION_MARKERS
                            )
                            if not (
                                dialog_identity
                                and list_complete
                                and protections_clear
                                and dialog_protections_clear
                            ):
                                return self._observation(
                                    plan,
                                    clicked=False,
                                    before_texts=before_texts,
                                    after_texts=dialog_texts,
                                    after_xml=dialog_xml,
                                    ime_current=ime_current,
                                    body_readback_matches=False,
                                    identity_unique=identity_unique,
                                    list_complete=list_complete,
                                    protections_clear=protections_clear,
                                    target_present_before=target_present_before,
                                    target_present_after=target_present_before,
                                    attachment_name_matched=False,
                                    contact_slot_available=contact_slot_available,
                                    force_input_box_empty=True,
                                )
                        elif plan.action_kind in {
                            "change_wechat",
                            "change_phone",
                        } and not dialog_identity:
                            return self._observation(
                                plan,
                                clicked=False,
                                before_texts=before_texts,
                                after_texts=dialog_texts,
                                after_xml=dialog_xml,
                                ime_current=ime_current,
                                body_readback_matches=False,
                                identity_unique=identity_unique,
                                list_complete=list_complete,
                                protections_clear=protections_clear,
                                target_present_before=target_present_before,
                                target_present_after=target_present_before,
                                attachment_name_matched=False,
                                contact_slot_available=contact_slot_available,
                                force_input_box_empty=True,
                            )
            after_xml = self._read_hierarchy(
                plan.action_key,
                phase="after",
                clicked=clicked,
                before_command=before_command,
            )
        except DeviceGateError:
            raise ActionExecutionError(
                "device_disconnected",
                clicked=clicked,
                scene_settled=not clicked,
                input_box_empty=not clicked,
                command_quiet=False,
                ime_restored=not clicked,
            ) from None
        except ActionExecutionError as error:
            restored = self._restore_ime(
                plan.ime_original, before_command=before_command
            )
            raise ActionExecutionError(
                error.reason_code,
                clicked=error.clicked,
                scene_settled=error.scene_settled,
                input_box_empty=error.input_box_empty,
                command_quiet=error.command_quiet,
                ime_restored=(
                    plan.ime_original is None
                    or restored == plan.ime_original
                ),
            ) from None

        after_texts = _node_texts(after_xml)
        if plan.action_kind == "delete_conversation":
            list_complete = list_complete and _list_complete(after_xml)
            protections_clear = protections_clear and not any(
                marker in text
                for text in _target_scope_texts(
                    after_xml, target, plan.identity_labels
                )
                for marker in PROTECTION_MARKERS
            )
        if plan.action_kind != "delete_conversation":
            identity_unique = identity_unique and after_texts.count(target) == 1
        if plan.action_kind == "send_resume":
            attachment_name_matched = attachment_verified
        else:
            attachment_name_matched = False
        return self._observation(
            plan,
            clicked=clicked,
            before_texts=before_texts,
            after_texts=after_texts,
            after_xml=after_xml,
            ime_current=self._restore_ime(
                plan.ime_original, before_command=before_command
            ),
            before_xml=before_xml,
            body_readback_matches=(
                plan.action_kind == "send_text" and text_readback_verified
            ),
            identity_unique=identity_unique,
            list_complete=list_complete,
            protections_clear=protections_clear,
            target_present_before=target_present_before,
            target_present_after=bool(target) and target in after_texts,
            attachment_name_matched=attachment_name_matched,
            contact_slot_available=contact_slot_available,
        )

    def _preflight_failure(
        self,
        plan: ActionPlan,
        before_xml: str,
        *,
        ime_current: str | None,
        input_box_empty: bool | None = None,
    ) -> ActionObservation:
        """前置不成立时不发不可逆命令，保留真实 before 现场。"""
        before_texts = _node_texts(before_xml)
        return self._observation(
            plan,
            clicked=False,
            before_texts=before_texts,
            after_texts=before_texts,
            after_xml=before_xml,
            ime_current=ime_current,
            body_readback_matches=False,
            identity_unique=False,
            list_complete=False,
            protections_clear=False,
            target_present_before=False,
            target_present_after=False,
            attachment_name_matched=False,
            contact_slot_available=False,
            force_input_box_empty=input_box_empty,
        )

    def _input_and_readback(
        self, plan: ActionPlan, *, before_command: Callable[[], None] | None
    ) -> str:
        """输入正文并一次回读，保存原始 XML 后返回；任何失败由调用方恢复 IME。"""
        self._run(
            [
                self._adb,
                "-s",
                self._serial,
                "shell",
                "input",
                "text",
                _escape_input_text(plan.body_text or ""),
            ],
            clicked=False,
            phase="input",
            command="adb_action_input",
            before_command=before_command,
        )
        readback_xml = self._read_hierarchy(
            plan.action_key,
            phase="readback",
            clicked=False,
            before_command=before_command,
        )
        return readback_xml

    def _observation(
        self,
        plan: ActionPlan,
        *,
        clicked: bool,
        before_texts: list[str],
        after_texts: list[str],
        after_xml: str,
        ime_current: str | None,
        before_xml: str | None = None,
        body_readback_matches: bool,
        identity_unique: bool,
        list_complete: bool,
        protections_clear: bool,
        target_present_before: bool,
        target_present_after: bool,
        attachment_name_matched: bool,
        contact_slot_available: bool,
        force_input_box_empty: bool | None = None,
    ) -> ActionObservation:
        """构造只含动作目标差分事实的观察，不使用全局提示当本次效果。"""
        confirmation_open = "确认" in after_texts and "取消" in after_texts
        body = plan.body_text or ""
        target = plan.target_label or ""
        before_scope = (
            _target_scope_texts(before_xml, target, plan.identity_labels)
            if before_xml is not None
            else before_texts
        )
        after_scope = _target_scope_texts(after_xml, target, plan.identity_labels)
        request_before = before_scope.count(RESUME_REQUEST_MARKER)
        request_after = after_scope.count(RESUME_REQUEST_MARKER)
        delivered_before = before_scope.count(RESUME_DELIVERED_MARKER)
        delivered_after = after_scope.count(RESUME_DELIVERED_MARKER)
        contact_before = before_scope.count(CONTACT_REQUEST_MARKER)
        contact_after = after_scope.count(CONTACT_REQUEST_MARKER)
        if force_input_box_empty is not None:
            input_box_empty = force_input_box_empty
        elif plan.action_kind == "send_text":
            editors = _editor_texts_in_target(
                after_xml, target, plan.identity_labels
            )
            input_box_empty = editors is not None and all(
                not value for value in editors
            )
        else:
            input_box_empty = True
        return ActionObservation(
            action_key=plan.action_key,
            action_kind=plan.action_kind,
            before_snapshot_id=(
                _snapshot_id(before_xml)
                if before_xml is not None
                else str(uuid.uuid4())
            ),
            after_snapshot_id=_snapshot_id(after_xml),
            observed_at=_utc_now(),
            clicked=clicked,
            error=None,
            scene_settled=not confirmation_open and input_box_empty,
            command_quiet=True,
            confirmation_open=confirmation_open,
            input_box_empty=input_box_empty,
            ime_original=plan.ime_original,
            ime_current=ime_current,
            body_readback_matches=body_readback_matches,
            echo_count_before=before_scope.count(body) if body else 0,
            echo_count_after=after_scope.count(body) if body else 0,
            attachment_name_matched=attachment_name_matched,
            attachment_request_observed=request_after > request_before,
            delivery_observed=delivered_after > delivered_before,
            contact_slot_available=contact_slot_available,
            contact_slot_status=(
                "requested" if contact_after > contact_before else "none"
            )
            if plan.action_kind in {"change_wechat", "change_phone"}
            else None,
            identity_unique=identity_unique,
            list_complete=list_complete,
            protections_clear=protections_clear,
            target_present_before=target_present_before,
            target_present_after=target_present_after,
            anchor_present_after=bool(plan.anchor_label)
            and plan.anchor_label in after_texts,
        )

    def _switch_ime(
        self,
        component: str,
        original: str | None,
        *,
        before_command: Callable[[], None] | None,
    ) -> str | None:
        """在输入前实际切到目标 IME 并读回确认；失败尝试恢复原 IME。"""
        try:
            self._run(
                [self._adb, "-s", self._serial, "shell", "ime", "set", component],
                clicked=False,
                phase="idle",
                command="adb_action_ime_set",
                before_command=before_command,
            )
            current = self.read_current_ime(before_command=before_command)
        except (ActionExecutionError, DeviceGateError):
            current = None
        if current == component:
            return current
        self._restore_ime(original, before_command=before_command)
        return None

    def _restore_ime(
        self, original: str | None, *, before_command: Callable[[], None] | None
    ) -> str | None:
        """动作后恢复原 IME 并读回；任何失败保持未知而不是假称恢复。"""
        if original is None:
            return None
        try:
            self._run(
                [self._adb, "-s", self._serial, "shell", "ime", "set", original],
                clicked=True,
                phase="clicked",
                command="adb_action_ime_set",
                before_command=before_command,
            )
            current = self.read_current_ime(before_command=before_command)
        except (ActionExecutionError, DeviceGateError):
            return None
        return current if current == original else None

    def _tap(
        self,
        plan: ActionPlan,
        index: int,
        *,
        before_command: Callable[[], None] | None,
        clicked_before: bool = False,
    ) -> None:
        """执行一个点击原语；不可逆序号由计划固定。"""
        x, y = plan.tap_points[index]
        is_irreversible = index >= plan.irreversible_tap_index
        self._run(
            [self._adb, "-s", self._serial, "shell", "input", "tap", str(x), str(y)],
            clicked=clicked_before or is_irreversible,
            phase="clicked" if is_irreversible else "input",
            command="adb_action_tap",
            before_command=before_command,
        )

    def _read_hierarchy(
        self,
        action_key: str,
        *,
        phase: str,
        clicked: bool,
        before_command: Callable[[], None] | None,
    ) -> str:
        """复用唯一采集器的 dump/cat 与证据保存，只补动作相位索引。"""
        active_phase = "clicked" if clicked else "idle"
        prefix = (
            "action-"
            + hashlib.sha256(action_key.encode("utf-8")).hexdigest()[:16]
            + "-"
            + phase
        )
        try:
            capture = self._screen.dump_hierarchy(
                self._serial,
                before_command=before_command,
                prefix=prefix,
            )
        except ScreenReadError as error:
            reason = (
                "executor_interrupted"
                if error.reason_code == "invalid_fixture"
                else "device_disconnected"
            )
            raise ActionExecutionError(
                reason, clicked=clicked, **_phase_flags(active_phase)
            ) from None
        except Exception:
            raise ActionExecutionError(
                "executor_interrupted",
                clicked=clicked,
                **_phase_flags(active_phase),
            ) from None
        self._index_evidence(
            action_key,
            phase,
            capture.xml_text,
            capture.evidence_name,
            capture.byte_size,
        )
        return capture.xml_text

    def _run(
        self,
        args: list[str],
        *,
        clicked: bool,
        phase: str,
        command: str,
        before_command: Callable[[], None] | None,
    ) -> str:
        """每条命令前重核条件，单次有界执行；失败立即停止且不重发。"""
        started = time.perf_counter()
        self._record(command, "started", 0)
        try:
            completed = run_adb(
                args,
                gate=self._gate,
                timeout=self._timeout,
                before=before_command,
                on_pid=(
                    self._record_command_pid
                    if self._command_store is not None
                    else None
                ),
            )
        except AdbCommandFailure as error:
            self._record_failure(command, started, error.category)
            raise ActionExecutionError(
                "device_disconnected", clicked=clicked, **_phase_flags(phase)
            ) from None
        except DeviceGateError:
            self._record_failure(command, started, "adb_gate")
            raise ActionExecutionError(
                "device_disconnected", clicked=clicked, **_phase_flags(phase)
            ) from None
        except Exception:
            self._record_failure(command, started, "action_guard")
            raise ActionExecutionError(
                "executor_interrupted", clicked=clicked, **_phase_flags(phase)
            ) from None
        self._record(command, "ok", elapsed_ms(started))
        return completed.stdout

    def _record_command_pid(self, pid: int) -> None:
        """把本动作命令 PID 写入本机命令身份账本；不写命令参数。"""
        if self._command_store is None or self._current_action_key is None:
            return
        try:
            self._command_store.record(self._current_action_key, pid)
        except (OSError, ValueError):
            return

    def _index_evidence(
        self,
        action_key: str,
        phase: str,
        xml_text: str,
        evidence_name: str,
        byte_size: int,
    ) -> None:
        """只追加动作相位到内容 UUID/采集证据名的本机索引，不重复保存 XML。"""
        if self._evidence_dir is None:
            return
        index_name = (
            "action-"
            + hashlib.sha256(action_key.encode("utf-8")).hexdigest()[:16]
            + ".index.jsonl"
        )
        try:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            with (self._evidence_dir / index_name).open(
                "a", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(
                    json.dumps(
                        {
                            "action_key": action_key,
                            "phase": phase,
                            "snapshot_id": _snapshot_id(xml_text),
                            "evidence": evidence_name,
                            "bytes": byte_size,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        except OSError:
            raise ActionExecutionError(
                "executor_interrupted", clicked=True
            ) from None

    def _record(self, command: str, result: str, duration_ms: int) -> None:
        """写固定 action 事件与有限步骤标签，不写 serial/参数/现场原文。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage="action",
                result=result,
                operation="perform",
                duration_ms=duration_ms,
                command=command,
            )
        except Exception:
            return

    def _record_failure(self, command: str, started: float, category: str) -> None:
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
                command=command,
            )
        except Exception:
            return


def _snapshot_id(xml_text: str) -> str:
    """由层级原文内容派生稳定 UUID；相同内容不能伪装成不同前后快照。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "m5-action:" + xml_text))


def _parse_texts(xml_text: str) -> list[str]:
    """用标准库解析层级全部节点文本；畸形 XML 返回空列表。"""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return []
    return [
        node.get("text") or ""
        for node in root.iter("node")
        if node.get("text")
    ]


def _node_texts(xml_text: str) -> list[str]:
    """返回可读节点文本列表，供唯一身份与动作差分使用。"""
    return _parse_texts(xml_text)


def _target_node(
    xml_text: str, target: str, identity_labels: tuple[str, ...] = ()
) -> ET.Element | None:
    """返回唯一文本等于目标且含全部身份标签的节点；缺失/重复返回 None。"""
    if not target:
        return None
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return None
    matches = [node for node in root.iter("node") if node.get("text") == target]
    if len(matches) != 1:
        return None
    node = matches[0]
    texts = {
        child.get("text") or "" for child in node.iter("node") if child.get("text")
    }
    if any(label not in texts for label in identity_labels):
        return None
    return node


def _target_scope_texts(
    xml_text: str, target: str, identity_labels: tuple[str, ...] = ()
) -> list[str]:
    """返回唯一目标节点子树内的文本，用于目标局部差分。"""
    node = _target_node(xml_text, target, identity_labels)
    if node is None:
        return []
    return [
        child.get("text") or ""
        for child in node.iter("node")
        if child.get("text")
    ]


def _list_complete(xml_text: str) -> bool:
    """要求显式非滚动列表且无任何可滚动容器，不能以属性缺失猜完整。"""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return False
    nodes = list(root.iter("node"))
    if any(node.get("scrollable") == "true" for node in nodes):
        return False
    return any(node.get("scrollable") == "false" for node in nodes)


def _slot_available(
    xml_text: str,
    target: str,
    action_kind: str,
    identity_labels: tuple[str, ...] = (),
) -> bool:
    """要求目标子树存在对应类型、enabled 且可点击的未处理槽位。"""
    node = _target_node(xml_text, target, identity_labels)
    if node is None:
        return False
    marker = "微信" if action_kind == "change_wechat" else "电话"
    if action_kind not in {"change_wechat", "change_phone"}:
        return True
    texts = _target_scope_texts(xml_text, target, identity_labels)
    if any(
        item in text
        for text in texts
        for item in (CONTACT_REQUEST_MARKER, "已处理", "已交换")
    ):
        return False
    for child in node.iter("node"):
        text = child.get("text") or ""
        if (
            marker in text
            and child.get("clickable") == "true"
            and child.get("enabled") == "true"
        ):
            return True
    return False


def _editor_texts_in_target(
    xml_text: str, target: str, identity_labels: tuple[str, ...] = ()
) -> list[str] | None:
    """返回目标子树内 EditText 文本；无目标/无编辑器/畸形返回 None。"""
    node = _target_node(xml_text, target, identity_labels)
    if node is None:
        return None
    result: list[str] = []
    found = False
    for child in node.iter("node"):
        if child.get("class") != "android.widget.EditText":
            continue
        found = True
        result.append(child.get("text") or "")
    return result if found else None


def _editor_texts(xml_text: str) -> list[str] | None:
    """返回 EditText 节点文本；无编辑器或畸形时返回 None，不证明输入框为空。"""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return None
    result: list[str] = []
    found = False
    for node in root.iter("node"):
        if node.get("class") != "android.widget.EditText":
            continue
        found = True
        result.append(node.get("text") or "")
    return result if found else None


def _phase_flags(phase: str) -> dict[str, bool]:
    """按命令阶段给出可证明的现场标志；无法证明的收敛保持 False。"""
    if phase not in {"idle", "input", "dialog", "clicked"}:
        raise ValueError("phase is invalid")
    if phase == "dialog":
        return {
            "scene_settled": False,
            "input_box_empty": True,
            "command_quiet": False,
            "ime_restored": True,
        }
    return {
        "scene_settled": phase == "idle",
        "input_box_empty": phase == "idle",
        "command_quiet": False,
        "ime_restored": phase == "idle",
    }


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
