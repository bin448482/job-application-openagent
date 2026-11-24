"""实现 M0 显式真实设备探针，只顺序调用 adb devices 与 dumpsys window。"""

from __future__ import annotations

import re
import subprocess
import time

from app.ports.device import DeviceEntry, ProbeSnapshot
from app.ports.device_gate import DeviceGateError
from app.ports.screen import DeviceGate
from app.process_log import ProcessLog, elapsed_ms

ADB_TIMEOUT_SECONDS = 5
BOSS_PACKAGE = "com.hpbr.bosszhipin"

_KEYGUARD_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])isKeyguardShowing")
_KEYGUARD_VALUE_RE = re.compile(r"([^\s]*)")
_FOCUS_LINE_RE = re.compile(r"[ \t]*mCurrentFocus=([^\r\n]*)")
_WINDOW_CONTENT_RE = re.compile(r"Window\{([^}]*)\}")
_COMPONENT_TOKEN_RE = re.compile(r"([A-Za-z0-9_.]+)/[^\s}]+")


class DeviceProbeError(Exception):
    """携带安全原因码、已知清单数量和所选别名的真实探针失败。"""

    def __init__(
        self,
        reason_code: str,
        device_count: int = 0,
        selected_alias: str | None = None,
    ) -> None:
        """保存可输出到 JSON 的失败字段，不保留原始命令或序列号。"""
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.device_count = device_count
        self.selected_alias = selected_alias


class AdbDeviceProbe:
    """通过参数数组和 shell=false 执行一次有界的真实 ADB 只读探针。"""

    def __init__(
        self,
        adb: str = "adb",
        process_log: ProcessLog | None = None,
        gate: DeviceGate | None = None,
    ) -> None:
        """保存 adb 文件名、可选日志与共享门禁；构造阶段不启动任何子进程。"""
        self._adb = adb
        self._process_log = process_log
        self._gate = gate

    def probe(self, serial: str | None = None) -> ProbeSnapshot:
        """读取一次设备清单和窗口字段，返回不含真实序列号或窗口原文的快照。"""
        self._require_gate()
        if not isinstance(serial, str) or not serial:
            raise ValueError("serial is required")

        listing = self._run(
            [self._adb, "devices"],
            device_count=0,
            selected_alias=None,
            stage="adb_devices",
            command="adb_devices",
        )
        entries = _parse_device_list(listing)
        matched_index = next(
            (index for index, (entry_serial, _) in enumerate(entries) if entry_serial == serial),
            None,
        )
        if matched_index is None:
            raise DeviceProbeError("device_disconnected", len(entries), None)

        devices: list[DeviceEntry] = []
        for index, (_, state) in enumerate(entries):
            alias = "primary" if index == matched_index else f"device-{index + 1}"
            devices.append(DeviceEntry(alias=alias, state=_normalize_state(state)))

        if devices[matched_index].state != "device":
            return ProbeSnapshot(
                devices=tuple(devices),
                selected_alias="primary",
                unlocked=None,
                foreground_app="unknown",
            )

        window_dump = self._run(
            [self._adb, "-s", serial, "shell", "dumpsys window"],
            device_count=len(entries),
            selected_alias="primary",
            stage="adb_window",
            command="adb_window",
        )
        return ProbeSnapshot(
            devices=tuple(devices),
            selected_alias="primary",
            unlocked=_decide_unlocked(window_dump),
            foreground_app=_decide_foreground(window_dump),
        )

    def _run(
        self,
        args: list[str],
        *,
        device_count: int,
        selected_alias: str | None,
        stage: str,
        command: str,
    ) -> str:
        """每条 ADB 子命令前要求并复核共享门禁，再执行一条不重试的命令。"""
        self._require_gate()
        assert self._gate is not None
        self._gate.verify()
        started = time.perf_counter()
        self._record(
            stage=stage,
            command=command,
            result="started",
            duration_ms=0,
        )
        try:
            completed = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=ADB_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            self._record_failure(stage, command, started, "adb_missing")
            raise DeviceProbeError(
                "device_disconnected",
                device_count,
                selected_alias,
            ) from None
        except subprocess.TimeoutExpired:
            self._record_failure(stage, command, started, "adb_timeout")
            raise DeviceProbeError(
                "device_disconnected",
                device_count,
                selected_alias,
            ) from None
        except OSError:
            self._record_failure(stage, command, started, "adb_oserror")
            raise DeviceProbeError(
                "device_disconnected",
                device_count,
                selected_alias,
            ) from None
        if completed.returncode != 0:
            self._record_failure(stage, command, started, "adb_nonzero")
            raise DeviceProbeError(
                "device_disconnected",
                device_count,
                selected_alias,
            )
        self._record(
            stage=stage,
            command=command,
            result="ok",
            duration_ms=elapsed_ms(started),
        )
        return completed.stdout

    def _require_gate(self) -> None:
        """真实适配器缺失共享门禁时在零子进程处固定拒绝。"""
        if self._gate is None:
            raise DeviceGateError("device_not_held")

    def _record_failure(
        self,
        stage: str,
        command: str,
        started: float,
        error_category: str,
    ) -> None:
        """记录一条固定 ADB 失败类别，业务原因码仍为 device_disconnected。"""
        self._record(
            stage=stage,
            command=command,
            result="failed",
            duration_ms=elapsed_ms(started),
            reason_code="device_disconnected",
            error_category=error_category,
        )

    def _record(
        self,
        *,
        stage: str,
        command: str,
        result: str,
        duration_ms: int,
        reason_code: str | None = None,
        error_category: str | None = None,
    ) -> None:
        """向可选过程日志写固定命令事件；日志失败不得影响探针或泄露原值。"""
        if self._process_log is None:
            return
        try:
            self._process_log.record(
                stage=stage,
                result=result,
                reason_code=reason_code,
                error_category=error_category,
                duration_ms=duration_ms,
                command=command,
                mode="device",
            )
        except Exception:
            return


def _parse_device_list(text: str) -> list[tuple[str, str]]:
    """只从 tab 分隔的确定清单行提取序列号与状态，提示行不计入设备数。"""
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        serial = serial.strip()
        state_parts = state.split()
        if not serial or not state_parts:
            continue
        if serial.startswith("*") or serial.startswith("adb:") or serial.endswith(":"):
            continue
        entries.append((serial, state_parts[0]))
    return entries


def _normalize_state(state: str) -> str:
    """把清单状态收敛为 device、offline 或 unauthorized。"""
    if state == "device":
        return "device"
    if state == "unauthorized":
        return "unauthorized"
    return "offline"


def _decide_unlocked(text: str) -> bool | None:
    """仅当每个 isKeyguardShowing 都严格为 true/false 且全为 false 时判定解锁。"""
    tokens: list[str] = []
    for match in _KEYGUARD_NAME_RE.finditer(text):
        rest = text[match.end():]
        if not rest.startswith("="):
            return None
        token_match = _KEYGUARD_VALUE_RE.match(rest[1:])
        if token_match is None:
            return None
        tokens.append(token_match.group(1))
    if not tokens:
        return None
    normalized = {token.lower() for token in tokens}
    if not normalized <= {"true", "false"}:
        return None
    if normalized == {"false"}:
        return True
    if normalized == {"true"}:
        return False
    return None


def _decide_foreground(text: str) -> str:
    """仅当唯一合法字段行含唯一完整 Window 和唯一组件 token 时判定前台。"""
    values: list[str] = []
    for line in text.splitlines():
        if "mCurrentFocus" not in line:
            continue
        match = _FOCUS_LINE_RE.fullmatch(line)
        if match is None:
            return "unknown"
        values.append(match.group(1))
    if len(values) != 1:
        return "unknown"
    focus_line = values[0].strip()
    if focus_line.count("{") != 1 or focus_line.count("}") != 1:
        return "unknown"
    window = _WINDOW_CONTENT_RE.fullmatch(focus_line)
    if window is None:
        return "unknown"
    packages = _COMPONENT_TOKEN_RE.findall(window.group(1))
    if len(packages) != 1:
        return "unknown"
    if packages[0] == BOSS_PACKAGE:
        return "boss"
    return "other"
