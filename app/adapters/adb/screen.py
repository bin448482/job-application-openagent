"""实现 M3 显式 ADB 层级/截图采集，只做一次有界命令并保存本机证据。"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from app.adapters.adb.command import (
    ADB_TIMEOUT_SECONDS,
    AdbCommandFailure,
    run_adb,
    save_evidence,
)
from app.ports.device_gate import DeviceGateError
from app.ports.screen import (
    DeviceGate,
    HierarchyCapture,
    ScreenCapture,
    ScreenReadError,
)
from app.process_log import REASON_CODES, ProcessLog, elapsed_ms

DEVICE_TMP_XML = "/data/local/tmp/m3-ui.xml"


class AdbScreenReader:
    """通过参数数组和 shell=false 执行一次层级 dump/cat 与原始截图。"""

    def __init__(
        self,
        evidence_dir: Path | None,
        adb: str = "adb",
        process_log: ProcessLog | None = None,
        gate: DeviceGate | None = None,
    ) -> None:
        """保存 adb 文件名、可选证据目录、日志与共享门禁；构造不启动子进程。"""
        self._adb = adb
        self._evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
        self._process_log = process_log
        self._gate = gate

    def dump_hierarchy(
        self,
        serial: str,
        *,
        before_command: object | None = None,
        prefix: str = "hierarchy",
        on_start: Callable[[], None] | None = None,
        on_pid: Callable[[int], None] | None = None,
        on_done: Callable[[int, bool, bool], None] | None = None,
        on_fail: Callable[[], None] | None = None,
    ) -> HierarchyCapture:
        """依次执行 dump 与 cat，保存 XML 证据并返回内存文本。"""
        self._require_gate()
        _require_serial(serial)
        self._run_text(
            [self._adb, "-s", serial, "shell", "uiautomator", "dump", DEVICE_TMP_XML],
            stage="adb_hierarchy",
            command="adb_hierarchy_dump",
            before_command=before_command,
            on_start=on_start,
            on_pid=on_pid,
            on_done=on_done,
            on_fail=on_fail,
        )
        xml_text = self._run_text(
            [self._adb, "-s", serial, "shell", "cat", DEVICE_TMP_XML],
            stage="adb_hierarchy",
            command="adb_hierarchy_cat",
            before_command=before_command,
            on_start=on_start,
            on_pid=on_pid,
            on_done=on_done,
            on_fail=on_fail,
        )
        name = f"{prefix}-{uuid.uuid4()}.xml"
        payload = xml_text.encode("utf-8")
        self._save(name, payload)
        return HierarchyCapture(
            xml_text=xml_text,
            evidence_name=name,
            byte_size=len(payload),
        )

    def capture_screen(self, serial: str) -> ScreenCapture:
        """执行一次 exec-out screencap，保存原始字节并返回内存截图。"""
        self._require_gate()
        _require_serial(serial)
        raw = self._run_bytes(
            [self._adb, "-s", serial, "exec-out", "screencap"],
            stage="adb_screen",
            command="adb_screencap",
        )
        name = f"screen-{uuid.uuid4()}.rgba"
        self._save(name, raw)
        return ScreenCapture(raw=raw, evidence_name=name, byte_size=len(raw))

    def _require_gate(self) -> None:
        """真实适配器缺失共享门禁时在零子进程处固定拒绝。"""
        if self._gate is None:
            raise DeviceGateError("device_not_held")

    def _run_text(
        self,
        args: list[str],
        *,
        stage: str,
        command: str,
        before_command: object | None = None,
        on_start: Callable[[], None] | None = None,
        on_pid: Callable[[int], None] | None = None,
        on_done: Callable[[int, bool, bool], None] | None = None,
        on_fail: Callable[[], None] | None = None,
    ) -> str:
        """执行一条不重试的文本 ADB 命令，失败统一为断连。"""
        completed = self._run(
            args,
            stage=stage,
            command=command,
            binary=False,
            before_command=before_command,
            on_start=on_start,
            on_pid=on_pid,
            on_done=on_done,
            on_fail=on_fail,
        )
        return completed.stdout

    def _run_bytes(self, args: list[str], *, stage: str, command: str) -> bytes:
        """执行一条不重试的二进制 ADB 命令，失败统一为断连。"""
        completed = self._run(args, stage=stage, command=command, binary=True)
        return completed.stdout

    def _run(
        self,
        args: list[str],
        *,
        stage: str,
        command: str,
        binary: bool,
        before_command: object | None = None,
        on_start: Callable[[], None] | None = None,
        on_pid: Callable[[int], None] | None = None,
        on_done: Callable[[int, bool, bool], None] | None = None,
        on_fail: Callable[[], None] | None = None,
    ) -> subprocess.CompletedProcess:
        """复用共享 ADB 边界；每条命令前复核门禁，单次 attempt 不重试。"""
        self._require_gate()
        assert self._gate is not None
        started = time.perf_counter()
        self._record(stage, command, "started", 0)
        try:
            completed = run_adb(
                args,
                gate=self._gate,
                timeout=ADB_TIMEOUT_SECONDS,
                binary=binary,
                before=before_command,  # type: ignore[arg-type]
                on_start=on_start,
                on_pid=on_pid,
                on_done=on_done,
                on_fail=on_fail,
            )
        except AdbCommandFailure as error:
            self._record_failure(stage, command, started, error.category)
            raise ScreenReadError("device_disconnected") from None
        except DeviceGateError:
            self._record_failure(stage, command, started, "adb_gate")
            raise ScreenReadError("device_disconnected") from None
        except Exception as error:
            reason = getattr(error, "reason_code", "invalid_input")
            if not isinstance(reason, str) or reason not in REASON_CODES:
                reason = "invalid_input"
            self._record_guard_failure(stage, command, started, reason)
            raise
        self._record(stage, command, "ok", elapsed_ms(started))
        return completed

    def _save(self, name: str, payload: bytes) -> None:
        """把证据写到调用方指定的本机目录；失败不伪造已保存。"""
        started = time.perf_counter()
        self._record_evidence("started", 0)
        try:
            if self._evidence_dir is not None:
                save_evidence(self._evidence_dir, name, payload)
        except OSError:
            self._record_evidence_failure(started)
            raise ScreenReadError("invalid_fixture") from None
        self._record_evidence("ok", elapsed_ms(started))

    def _record_evidence_failure(self, started: float) -> None:
        """记录证据保存失败；不写路径或异常。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage="adb_evidence",
                result="failed",
                reason_code="invalid_fixture",
                error_category="evidence",
                duration_ms=elapsed_ms(started),
                mode="device",
            )
        except Exception:
            return

    def _record_evidence(self, result: str, duration_ms: int) -> None:
        """记录证据保存成功；不写路径或内容。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage="adb_evidence",
                result=result,
                duration_ms=duration_ms,
                mode="device",
            )
        except Exception:
            return

    def _record_failure(
        self, stage: str, command: str, started: float, error_category: str
    ) -> None:
        """记录固定失败类别，业务原因仍为 device_disconnected。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage=stage,
                result="failed",
                reason_code="device_disconnected",
                error_category=error_category,
                duration_ms=elapsed_ms(started),
                command=command,
                mode="device",
            )
        except Exception:
            return

    def _record_guard_failure(
        self, stage: str, command: str, started: float, reason_code: str
    ) -> None:
        """记录调用方 guard 的固定拒绝类别，再按原语义传播，不写异常原文。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage=stage,
                result="failed",
                reason_code=reason_code,
                error_category="action_guard",
                duration_ms=elapsed_ms(started),
                command=command,
                mode="device",
            )
        except Exception:
            return

    def _record(
        self, stage: str, command: str, result: str, duration_ms: int
    ) -> None:
        """写固定命令事件；日志失败不影响采集或泄露原值。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage=stage,
                result=result,
                duration_ms=duration_ms,
                command=command,
                mode="device",
            )
        except Exception:
            return


def _require_serial(serial: str) -> None:
    """要求非空 serial；真实 serial 只在内存使用。"""
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError("serial is required")
