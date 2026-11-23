"""解析严格 CLI 参数、加载最小配置，并输出唯一的 UTF-8 JSON 结果。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import NoReturn

from app.adapters.adb.device import AdbDeviceProbe, DeviceProbeError
from app.ports.device import ProbeSnapshot
from app.process_log import ProcessLog, elapsed_ms
from app.usecases.doctor import build_result, parse_probe_input, run_doctor

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _CliArgumentError(Exception):
    """表示参数形状错误，统一映射为 invalid_input。"""


class _ConfigError(Exception):
    """表示配置读取或内容错误，统一映射为 invalid_config。"""


class _FixtureError(Exception):
    """表示夹具读取或形状错误，统一映射为 invalid_fixture。"""


class _StrictArgumentParser(argparse.ArgumentParser):
    """禁用自动缩写，并把参数错误转成可安全输出的异常。"""

    def error(self, message: str) -> NoReturn:
        """阻止 argparse 向 stderr 打印用法或原始输入。"""
        raise _CliArgumentError(message) from None


class _UniqueStore(argparse.Action):
    """拒绝同一选项重复出现，避免后值静默覆盖前值。"""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        """在目标属性已赋值时抛出参数错误。"""
        if getattr(namespace, self.dest, None) is not None:
            raise _CliArgumentError("duplicate option") from None
        setattr(namespace, self.dest, values)


class _UniqueStoreTrue(argparse.Action):
    """拒绝重复的布尔开关，保持参数校验严格。"""

    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        default: object = False,
        help: str | None = None,
    ) -> None:
        """按零参数开关初始化动作。"""
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            nargs=0,
            default=default,
            help=help,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        """在开关已打开时抛出参数错误。"""
        if getattr(namespace, self.dest, False):
            raise _CliArgumentError("duplicate option") from None
        setattr(namespace, self.dest, True)


def _build_parser() -> argparse.ArgumentParser:
    """构造只含全局 --config 与 doctor 子命令的严格解析器。"""
    parser = _StrictArgumentParser(
        prog="python -m app",
        description="M0 只读 doctor 探针；默认不连接设备、不调用网络。",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--config",
        action=_UniqueStore,
        metavar="<相对文件>",
        help="可选的最小只读配置；省略时使用内置默认值。",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    doctor = subparsers.add_parser(
        "doctor",
        allow_abbrev=False,
        help="运行一次只读设备探针。",
        description="在合成 fixture 或显式真实设备模式中选择其一。",
    )
    modes = doctor.add_mutually_exclusive_group()
    modes.add_argument(
        "--fixture",
        action=_UniqueStore,
        metavar="<相对文件>",
        help="读取合成探针输入，不启动子进程。",
    )
    modes.add_argument(
        "--device",
        action=_UniqueStoreTrue,
        default=False,
        help="显式启用一次真实 ADB 探针，必须同时给出 --serial。",
    )
    doctor.add_argument(
        "--serial",
        action=_UniqueStore,
        metavar="<serial>",
        help="仅与 --device 同用；只在内存匹配，不写入输出。",
    )
    return parser


def _resolve_project_path(value: str) -> Path:
    """把相对输入解析到项目根内，拒绝绝对路径、盘符和越界链接。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path is empty")
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or candidate.drive
        or candidate.anchor
        or value.startswith("~")
    ):
        raise ValueError("path is not project-relative")
    resolved = (PROJECT_ROOT / candidate).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError("path escapes project root")
    return resolved


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """把 JSON 对象转为字典，并在任意层级遇到重复键时拒绝。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _load_json_object(text: str) -> object:
    """在共享输入边界解析 JSON，并拒绝顶层或嵌套对象的重复键。"""
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def _validate_config(value: str | None) -> None:
    """只校验最小只读配置；省略或空对象保持固定 read_only 默认契约。"""
    if value is None:
        return
    try:
        path = _resolve_project_path(value)
        data = _load_json_object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _ConfigError from None

    if not isinstance(data, dict):
        raise _ConfigError
    if not set(data) <= {"schema_version", "mode"}:
        raise _ConfigError
    if "schema_version" in data:
        version = data["schema_version"]
        if type(version) is not int or version != 1:
            raise _ConfigError
    if "mode" in data:
        mode = data["mode"]
        if not isinstance(mode, str) or mode != "read_only":
            raise _ConfigError


def _load_fixture(value: str) -> ProbeSnapshot:
    """读取并严格校验合成探针输入，不启动子进程也不写业务库。"""
    try:
        path = _resolve_project_path(value)
        data = _load_json_object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _FixtureError from None
    try:
        return parse_probe_input(data)
    except ValueError:
        raise _FixtureError from None


def _mode_is_valid(args: argparse.Namespace) -> bool:
    """检查 fixture/device 互斥、serial 归属以及缺参数情形。"""
    if args.command != "doctor":
        return False
    if args.fixture is not None and not args.fixture:
        return False
    if args.device:
        return bool(args.serial and args.serial.strip())
    return args.fixture is not None and args.serial is None


def _requested_mode(args: argparse.Namespace) -> str | None:
    """返回参数请求的 fixture/device 模式；冲突或缺失时为 null。"""
    if args.fixture is not None and not args.device:
        return "fixture"
    if args.device and args.fixture is None:
        return "device"
    return None


def _finish(
    log: ProcessLog,
    started: float,
    exit_code: int,
    reason_code: str | None,
    error_category: str | None,
) -> int:
    """追加 exit 事件并返回退出码；日志降级时用固定 stderr 提示不完整。"""
    log.record(
        stage="exit",
        result="ok" if exit_code == 0 else "failed",
        reason_code=reason_code,
        error_category=error_category,
        duration_ms=elapsed_ms(started),
        exit_code=exit_code,
    )
    if getattr(log, "degraded", False):
        _emit_log_degraded()
    return exit_code


def _emit_log_degraded() -> None:
    """在不改变 stdout JSON 与退出码的前提下，提示本机日志不完整。"""
    try:
        sys.stderr.write("m0_process_log_degraded\n")
        sys.stderr.flush()
    except Exception:
        return


def _emit(payload: dict[str, object]) -> bool:
    """把白名单 JSON 对象以单行 UTF-8 写入 stdout；写出失败返回 False。"""
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
        else:
            sys.stdout.write(text)
            sys.stdout.flush()
    except Exception:
        return False
    return True


def _emit_error(reason_code: str) -> bool:
    """输出参数、配置或夹具错误的安全固定对象，并返回是否写出成功。"""
    return _emit(
        {
            "schema_version": 1,
            "status": "failed",
            "reason_code": reason_code,
        }
    )


def main(
    argv: list[str] | None = None,
    *,
    process_log: ProcessLog | None = None,
) -> int:
    """执行一次 doctor 调用并返回 0 或 2，不向 stderr 回显输入或异常。"""
    log = process_log if process_log is not None else ProcessLog()
    started = time.perf_counter()
    log.record(stage="start", result="started")

    try:
        args = _build_parser().parse_args(argv)
    except _CliArgumentError:
        log.record(
            stage="arguments",
            result="failed",
            reason_code="invalid_input",
            error_category="argument",
        )
        if not _emit_error("invalid_input"):
            return _finish(log, started, 2, None, "internal")
        return _finish(log, started, 2, "invalid_input", "argument")
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 2
        reason_code = None if code == 0 else "invalid_input"
        error_category = None if code == 0 else "argument"
        log.record(
            stage="arguments",
            result="ok" if code == 0 else "failed",
            reason_code=reason_code,
            error_category=error_category,
            exit_code=code,
        )
        _finish(log, started, code, reason_code, error_category)
        raise

    log.record(stage="arguments", result="ok")
    config_source = "file" if args.config is not None else "default"
    try:
        _validate_config(args.config)
    except _ConfigError:
        log.record(
            stage="config",
            result="failed",
            reason_code="invalid_config",
            error_category="config",
            config_source=config_source,
        )
        if not _emit_error("invalid_config"):
            return _finish(log, started, 2, None, "internal")
        return _finish(log, started, 2, "invalid_config", "config")
    except Exception:
        log.record(
            stage="config",
            result="failed",
            reason_code=None,
            error_category="internal",
            config_source=config_source,
        )
        if not _emit_error("invalid_config"):
            return _finish(log, started, 2, None, "internal")
        return _finish(log, started, 2, "invalid_config", "internal")
    log.record(stage="config", result="ok", config_source=config_source)

    mode = _requested_mode(args)
    if not _mode_is_valid(args):
        log.record(
            stage="mode",
            result="failed",
            reason_code="invalid_input",
            error_category="argument",
            mode=mode,
            config_source=config_source,
        )
        if not _emit_error("invalid_input"):
            return _finish(log, started, 2, None, "internal")
        return _finish(log, started, 2, "invalid_input", "argument")
    log.record(stage="mode", result="ok", mode=mode, config_source=config_source)

    snapshot: ProbeSnapshot
    if args.fixture is not None:
        stage_started = time.perf_counter()
        log.record(
            stage="fixture",
            result="started",
            mode=mode,
            config_source=config_source,
        )
        try:
            snapshot = _load_fixture(args.fixture)
        except _FixtureError:
            log.record(
                stage="fixture",
                result="failed",
                reason_code="invalid_fixture",
                error_category="fixture",
                duration_ms=elapsed_ms(stage_started),
                mode=mode,
                config_source=config_source,
            )
            if not _emit_error("invalid_fixture"):
                return _finish(log, started, 2, None, "internal")
            return _finish(log, started, 2, "invalid_fixture", "fixture")
        except Exception:
            log.record(
                stage="fixture",
                result="failed",
                reason_code=None,
                error_category="internal",
                duration_ms=elapsed_ms(stage_started),
                mode=mode,
                config_source=config_source,
            )
            if not _emit_error("invalid_input"):
                return _finish(log, started, 2, None, "internal")
            return _finish(log, started, 2, "invalid_input", "internal")
        log.record(
            stage="fixture",
            result="ok",
            duration_ms=elapsed_ms(stage_started),
            mode=mode,
            config_source=config_source,
        )
    else:
        stage_started = time.perf_counter()
        log.record(
            stage="probe",
            result="started",
            mode=mode,
            config_source=config_source,
        )
        try:
            snapshot = AdbDeviceProbe(process_log=log).probe(args.serial)
        except DeviceProbeError as error:
            device_count = max(0, error.device_count)
            log.record(
                stage="probe",
                result="failed",
                reason_code=error.reason_code,
                error_category="probe",
                duration_ms=elapsed_ms(stage_started),
                mode=mode,
                config_source=config_source,
            )
            log.record(
                stage="decision",
                result="failed",
                reason_code=error.reason_code,
                error_category="probe",
                mode=mode,
                config_source=config_source,
                device_count=device_count,
            )
            if not _emit(
                build_result(
                    status="failed",
                    reason_code=error.reason_code,
                    device_count=device_count,
                    selected_alias=error.selected_alias,
                    unlocked=None,
                    foreground_app="unknown",
                )
            ):
                return _finish(log, started, 2, None, "internal")
            return _finish(log, started, 2, error.reason_code, "probe")
        except Exception:
            log.record(
                stage="probe",
                result="failed",
                reason_code=None,
                error_category="internal",
                duration_ms=elapsed_ms(stage_started),
                mode=mode,
                config_source=config_source,
            )
            if not _emit_error("invalid_input"):
                return _finish(log, started, 2, None, "internal")
            return _finish(log, started, 2, "invalid_input", "internal")
        log.record(
            stage="probe",
            result="ok",
            duration_ms=elapsed_ms(stage_started),
            mode=mode,
            config_source=config_source,
        )

    try:
        result = run_doctor(snapshot)
    except Exception:
        log.record(
            stage="decision",
            result="failed",
            reason_code=None,
            error_category="internal",
            mode=mode,
            config_source=config_source,
        )
        if not _emit_error("invalid_input"):
            return _finish(log, started, 2, None, "internal")
        return _finish(log, started, 2, "invalid_input", "internal")

    status = result["status"]
    exit_code = 0 if status == "ok" else 2
    reason_code = result["reason_code"]
    error_category = None if status == "ok" else "business"
    log.record(
        stage="decision",
        result=status,
        reason_code=reason_code,
        error_category=error_category,
        mode=mode,
        config_source=config_source,
        device_count=result["device_count"],
    )
    if not _emit(result):
        return _finish(log, started, 2, None, "internal")
    return _finish(log, started, exit_code, reason_code, error_category)
