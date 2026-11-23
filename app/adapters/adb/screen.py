"""实现 M3 显式 ADB 层级/截图采集，只做一次有界命令并保存本机证据。"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

from app.ports.screen import (
    HierarchyCapture,
    ScreenCapture,
    ScreenReadError,
)
from app.process_log import ProcessLog, elapsed_ms

ADB_TIMEOUT_SECONDS = 5
DEVICE_TMP_XML = "/data/local/tmp/m3-ui.xml"


class AdbScreenReader:
    """通过参数数组和 shell=false 执行一次层级 dump/cat 与原始截图。"""

    def __init__(
        self,
        evidence_dir: Path,
        adb: str = "adb",
        process_log: ProcessLog | None = None,
    ) -> None:
        """保存 adb 文件名、证据目录与可选日志，构造阶段不启动子进程。"""
        self._adb = adb
        self._evidence_dir = Path(evidence_dir)
        self._process_log = process_log

    def dump_hierarchy(self, serial: str) -> HierarchyCapture:
        """依次执行 dump 与 cat，保存 XML 证据并返回内存文本。"""
        _require_serial(serial)
        self._run_text(
            [self._adb, "-s", serial, "shell", "uiautomator", "dump", DEVICE_TMP_XML],
            stage="adb_hierarchy",
            command="adb_hierarchy_dump",
        )
        xml_text = self._run_text(
            [self._adb, "-s", serial, "shell", "cat", DEVICE_TMP_XML],
            stage="adb_hierarchy",
            command="adb_hierarchy_cat",
        )
        name = f"hierarchy-{uuid.uuid4()}.xml"
        payload = xml_text.encode("utf-8")
        self._save(name, payload)
        return HierarchyCapture(
            xml_text=xml_text,
            evidence_name=name,
            byte_size=len(payload),
        )

    def capture_screen(self, serial: str) -> ScreenCapture:
        """执行一次 exec-out screencap，保存原始字节并返回内存截图。"""
        _require_serial(serial)
        raw = self._run_bytes(
            [self._adb, "-s", serial, "exec-out", "screencap"],
            stage="adb_screen",
            command="adb_screencap",
        )
        name = f"screen-{uuid.uuid4()}.rgba"
        self._save(name, raw)
        return ScreenCapture(raw=raw, evidence_name=name, byte_size=len(raw))

    def _run_text(self, args: list[str], *, stage: str, command: str) -> str:
        """执行一条不重试的文本 ADB 命令，失败统一为断连。"""
        completed = self._run(args, stage=stage, command=command, binary=False)
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
    ) -> subprocess.CompletedProcess:
        """以 shell=false、5 秒超时执行一次子进程，不重试也不输出原值。"""
        started = time.perf_counter()
        self._record(stage, command, "started", 0)
        try:
            completed = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
                timeout=ADB_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            self._record_failure(stage, command, started, "adb_missing")
            raise ScreenReadError("device_disconnected") from None
        except subprocess.TimeoutExpired:
            self._record_failure(stage, command, started, "adb_timeout")
            raise ScreenReadError("device_disconnected") from None
        except OSError:
            self._record_failure(stage, command, started, "adb_oserror")
            raise ScreenReadError("device_disconnected") from None
        if completed.returncode != 0:
            self._record_failure(stage, command, started, "adb_nonzero")
            raise ScreenReadError("device_disconnected")
        self._record(stage, command, "ok", elapsed_ms(started))
        return completed

    def _save(self, name: str, payload: bytes) -> None:
        """把证据写到调用方指定的本机目录；失败不伪造已保存。"""
        started = time.perf_counter()
        try:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            (self._evidence_dir / name).write_bytes(payload)
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
