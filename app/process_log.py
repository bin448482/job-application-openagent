"""追加 M0 本机白名单过程日志，只写固定字段且不影响业务输出。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "data" / "logs" / "m0-process.jsonl"

STAGES = frozenset(
    {
        "start",
        "arguments",
        "config",
        "mode",
        "fixture",
        "probe",
        "adb_devices",
        "adb_window",
        "decision",
        "output",
        "exit",
    }
)
RESULTS = frozenset({"started", "ok", "failed"})
REASON_CODES = frozenset(
    {
        "invalid_input",
        "invalid_config",
        "invalid_fixture",
        "device_disconnected",
        "device_ambiguous",
        "device_offline",
        "device_unauthorized",
        "device_locked",
        "wrong_page",
    }
)
ERROR_CATEGORIES = frozenset(
    {
        "argument",
        "config",
        "fixture",
        "probe",
        "business",
        "internal",
        "adb_missing",
        "adb_timeout",
        "adb_nonzero",
        "adb_oserror",
    }
)
COMMANDS = frozenset({"adb_devices", "adb_window"})
MODES = frozenset({"fixture", "device"})
CONFIG_SOURCES = frozenset({"default", "file"})
EVENT_KEYS = (
    "schema_version",
    "ts",
    "correlation_id",
    "stage",
    "result",
    "reason_code",
    "error_category",
    "duration_ms",
    "command",
    "exit_code",
    "mode",
    "config_source",
    "device_count",
)


def elapsed_ms(started: float) -> int:
    """把 `time.perf_counter()` 起点换算为非负整数毫秒。"""
    return max(0, int((time.perf_counter() - started) * 1000))


class ProcessLog:
    """尽力追加 JSON Lines 过程日志；非法值或写入失败都置 degraded 并返回 False。"""

    def __init__(self, path: Path | None = None, correlation_id: str | None = None) -> None:
        """保存日志路径、关联 ID 与降级标记，构造阶段不创建目录或打开文件。"""
        self.path = DEFAULT_LOG_PATH if path is None else path
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.degraded = False

    def record(
        self,
        *,
        stage: str,
        result: str,
        reason_code: str | None = None,
        error_category: str | None = None,
        duration_ms: int = 0,
        command: str | None = None,
        exit_code: int | None = None,
        mode: str | None = None,
        config_source: str | None = None,
        device_count: int | None = None,
    ) -> bool:
        """追加一条固定白名单事件；非法值或任何 IO 失败都置 degraded 并返回 False。"""
        try:
            if not _valid_event(
                stage=stage,
                result=result,
                reason_code=reason_code,
                error_category=error_category,
                duration_ms=duration_ms,
                command=command,
                exit_code=exit_code,
                mode=mode,
                config_source=config_source,
                device_count=device_count,
            ):
                self.degraded = True
                return False

            payload = {
                "schema_version": SCHEMA_VERSION,
                "ts": _utc_now(),
                "correlation_id": self.correlation_id,
                "stage": stage,
                "result": result,
                "reason_code": reason_code,
                "error_category": error_category,
                "duration_ms": duration_ms,
                "command": command,
                "exit_code": exit_code,
                "mode": mode,
                "config_source": config_source,
                "device_count": device_count,
            }
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            self._append(line)
        except Exception:
            self.degraded = True
            return False
        return True

    def _append(self, line: str) -> None:
        """追加一行 UTF-8 文本；由 `record` 统一处理失败，不自行打印。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()


def _valid_event(
    *,
    stage: str,
    result: str,
    reason_code: str | None,
    error_category: str | None,
    duration_ms: int,
    command: str | None,
    exit_code: int | None,
    mode: str | None,
    config_source: str | None,
    device_count: int | None,
) -> bool:
    """检查事件只含固定阶段、结果、原因码和可选元数据。"""
    if stage not in STAGES or result not in RESULTS:
        return False
    if reason_code is not None and reason_code not in REASON_CODES:
        return False
    if error_category is not None and error_category not in ERROR_CATEGORIES:
        return False
    if type(duration_ms) is not int or duration_ms < 0:
        return False
    if command is not None and command not in COMMANDS:
        return False
    if exit_code is not None and exit_code not in (0, 2):
        return False
    if mode is not None and mode not in MODES:
        return False
    if config_source is not None and config_source not in CONFIG_SOURCES:
        return False
    if device_count is not None and (type(device_count) is not int or device_count < 0):
        return False
    return True


def _utc_now() -> str:
    """返回毫秒精度的 UTC 时间字符串，便于本机按时间复核。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
