"""实现 M0 显式真实设备探针，只顺序调用 adb devices 与 dumpsys window。"""

from __future__ import annotations

import re
import subprocess

from app.ports.device import DeviceEntry, ProbeSnapshot

ADB_TIMEOUT_SECONDS = 5
BOSS_PACKAGE = "com.hpbr.bosszhipin"

_KEYGUARD_RE = re.compile(r"isKeyguardShowing=([A-Za-z]+)")
_FOCUS_RE = re.compile(r"mCurrentFocus=([^\r\n]*)")
_COMPONENT_RE = re.compile(r"Window\{[^}]*?\s([A-Za-z0-9_.]+)/[^\s}]+")


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

    def __init__(self, adb: str = "adb") -> None:
        """保存 adb 可执行文件名，构造阶段不启动任何子进程。"""
        self._adb = adb

    def probe(self, serial: str | None = None) -> ProbeSnapshot:
        """读取一次设备清单和窗口字段，返回不含真实序列号或窗口原文的快照。"""
        if not isinstance(serial, str) or not serial:
            raise ValueError("serial is required")

        listing = self._run(
            [self._adb, "devices"],
            device_count=0,
            selected_alias=None,
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
    ) -> str:
        """执行一条不重试的 ADB 命令，并把缺失、超时或非零退出归一为断连。"""
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
        except (OSError, subprocess.TimeoutExpired):
            raise DeviceProbeError(
                "device_disconnected",
                device_count,
                selected_alias,
            ) from None
        if completed.returncode != 0:
            raise DeviceProbeError(
                "device_disconnected",
                device_count,
                selected_alias,
            )
        return completed.stdout


def _parse_device_list(text: str) -> list[tuple[str, str]]:
    """从 adb devices 输出提取序列号与状态，忽略标题和守护进程提示。"""
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("List of devices attached"):
            continue
        if stripped.startswith("*") or stripped.startswith("adb server"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        entries.append((parts[0], parts[1]))
    return entries


def _normalize_state(state: str) -> str:
    """把清单状态收敛为 device、offline 或 unauthorized。"""
    if state == "device":
        return "device"
    if state == "unauthorized":
        return "unauthorized"
    return "offline"


def _decide_unlocked(text: str) -> bool | None:
    """只在全部 isKeyguardShowing 均为 false 时判定解锁，矛盾或缺失返回未知。"""
    values = _KEYGUARD_RE.findall(text)
    if not values:
        return None
    normalized = {value.lower() for value in values}
    if not normalized <= {"true", "false"}:
        return None
    if normalized == {"false"}:
        return True
    if normalized == {"true"}:
        return False
    return None


def _decide_foreground(text: str) -> str:
    """仅当 mCurrentFocus 唯一且组件可解析时按包名判定前台应用。"""
    matches = _FOCUS_RE.findall(text)
    if len(matches) != 1:
        return "unknown"
    component = _COMPONENT_RE.search(matches[0])
    if component is None:
        return "unknown"
    if component.group(1) == BOSS_PACKAGE:
        return "boss"
    return "other"
