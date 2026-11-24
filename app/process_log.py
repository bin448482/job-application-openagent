"""按运行隔离地追加本机白名单过程日志，只写固定字段且不影响业务输出。"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "data" / "logs"
DEFAULT_LOG_NAME = "m0-process.jsonl"
DEGRADED_MARKER = "m0_process_log_degraded"

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
        "store",
        "sense",
        "read",
        "policy",
        "eval",
        "adb_hierarchy",
        "adb_screen",
        "adb_evidence",
        "output",
        "exit",
    }
)
RESULTS = frozenset({"started", "ok", "undetermined", "failed"})
REASON_CODES = frozenset(
    {
        "invalid_input",
        "invalid_config",
        "invalid_fixture",
        "invalid_policy",
        "invalid_decision",
        "stale_decision",
        "stale_evidence",
        "decision_timeout",
        "input_too_large",
        "invalid_provider",
        "eval_failed",
        "authorization_required",
        "bridge_start_failed",
        "bridge_unavailable",
        "bridge_timeout",
        "bridge_protocol",
        "bridge_roles",
        "bridge_auth",
        "bridge_quota",
        "bridge_disconnected",
        "policy_exists",
        "policy_io_error",
        "policy_changed",
        "unknown_case",
        "device_disconnected",
        "device_ambiguous",
        "device_offline",
        "device_unauthorized",
        "device_locked",
        "wrong_page",
        "invalid_xml",
        "incomplete_snapshot",
        "stale_snapshot",
        "uncalibrated_profile",
        "identity_ambiguous",
        "unread_unverified",
        "needs_targeted_verification",
        "ambiguous_signal",
        "uncalibrated_region",
        "calibration_mismatch",
        "missing_signal",
        "stable_identity_unverified",
        "unverified_identity",
        "not_ok",
        "field_unknown",
        "incomplete",
        "no_target",
        "device_gate_unavailable",
        "lease_busy",
        "stale_lease",
        "owner_not_stopped",
        "clock_changed",
        "store_busy",
        "store_io_error",
        "db_version_unsupported",
        "migration_failed",
    }
)
ERROR_CATEGORIES = frozenset(
    {
        "argument",
        "config",
        "fixture",
        "probe",
        "business",
        "store",
        "sense",
        "read",
        "policy",
        "eval",
        "decision",
        "bridge",
        "pixel",
        "gate",
        "evidence",
        "internal",
        "adb_missing",
        "adb_timeout",
        "adb_nonzero",
        "adb_oserror",
    }
)
COMMANDS = frozenset(
    {
        "adb_devices",
        "adb_window",
        "adb_hierarchy_dump",
        "adb_hierarchy_cat",
        "adb_screencap",
    }
)
MODES = frozenset({"fixture", "device"})
CONFIG_SOURCES = frozenset({"default", "file"})
OPERATIONS = frozenset(
    {
        "init",
        "parse",
        "aggregate",
        "stop_reason",
        "open_store",
        "upsert_snapshot",
        "claim",
        "start",
        "renew",
        "finish",
        "recover",
        "unresolved_conversations",
        "load",
        "page",
        "job_list",
        "job_detail",
        "unread",
        "conversation",
        "pixel",
        "merge",
        "gate",
        "init",
        "validate",
        "preview",
        "render",
        "publish",
        "list",
        "snapshot",
        "restore",
        "eval",
        "verify",
        "bridge",
        "validate",
        "suite",
        "authorize",
        "case",
        "report",
        "holdout",
    }
)
EVENT_KEYS = (
    "schema_version",
    "ts",
    "correlation_id",
    "stage",
    "operation",
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
    """每次运行写独立 JSON Lines 文件；非法值或写入失败都置 degraded 并返回 False。"""

    def __init__(self, path: Path | None = None, correlation_id: str | None = None) -> None:
        """确定本次运行唯一文件与关联 ID，构造阶段不创建目录或打开文件。"""
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.path = _run_log_path(path)
        self.degraded = False
        self._created = False
        self._warning_emitted = False

    def record(
        self,
        *,
        stage: str,
        result: str,
        operation: str | None = None,
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
                operation=operation,
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
                "operation": operation,
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
        """向本次运行唯一文件追加一行；首次用独占创建避免覆盖同名文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if self._created else "x"
        with self.path.open(mode, encoding="utf-8", newline="\n") as handle:
            self._created = True
            handle.write(line)
            handle.flush()

    def emit_degraded_warning(self) -> bool:
        """日志已降级时向 stderr 写一次固定标记；写失败不抛出、不遮蔽业务结果。"""
        if not self.degraded or self._warning_emitted:
            return False
        self._warning_emitted = True
        try:
            sys.stderr.write(DEGRADED_MARKER + "\n")
            sys.stderr.flush()
        except Exception:
            return False
        return True


def _run_log_path(path: Path | None) -> Path:
    """把默认或显式基础路径规范为本次运行唯一的文件路径。

    显式参数可以是文件基础路径，也可以是目录；无论哪种都只作为命名基础，
    实际文件固定为同目录的 `<基础名>-<运行 UUID>.<后缀>`，不追加到共享文件。
    """
    if path is None:
        base = DEFAULT_LOG_DIR / DEFAULT_LOG_NAME
    else:
        candidate = Path(path)
        if candidate.exists() and candidate.is_dir():
            base = candidate / DEFAULT_LOG_NAME
        elif candidate.exists() or candidate.suffix:
            base = candidate
        else:
            base = candidate / DEFAULT_LOG_NAME
    suffix = base.suffix or ".jsonl"
    return base.with_name(f"{base.stem}-{uuid.uuid4()}{suffix}")


def _valid_event(
    *,
    stage: str,
    result: str,
    operation: str | None,
    reason_code: str | None,
    error_category: str | None,
    duration_ms: int,
    command: str | None,
    exit_code: int | None,
    mode: str | None,
    config_source: str | None,
    device_count: int | None,
) -> bool:
    """检查事件只含固定阶段、操作名、结果、原因码和可选元数据。"""
    if stage not in STAGES or result not in RESULTS:
        return False
    if operation is not None and (
        not isinstance(operation, str) or operation not in OPERATIONS
    ):
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
