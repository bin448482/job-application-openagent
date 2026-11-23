"""解析严格 CLI 参数、加载最小配置，并输出唯一的 UTF-8 JSON 结果。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import NoReturn

from app.adapters.adb.device import AdbDeviceProbe, DeviceProbeError
from app.adapters.sqlite.schema import initialize_database
from app.ports.device import ProbeSnapshot
from app.ports.store import StoreError
from app.process_log import ProcessLog, elapsed_ms
from app.usecases.doctor import build_result, parse_probe_input, run_doctor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = "data/runtime/runtime.sqlite3"


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
    """构造只含全局 --config、doctor 与 store init 的严格解析器。"""
    parser = _StrictArgumentParser(
        prog="python -m app",
        description="M0 只读 doctor 探针与 M1 store init；默认不连接设备、不调用网络。",
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
    store = subparsers.add_parser(
        "store",
        allow_abbrev=False,
        help="维护 M1 本机状态库。",
        description="只支持 store init；不导入旧库、不覆盖已有业务行。",
    )
    store_commands = store.add_subparsers(
        dest="store_command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    store_init = store_commands.add_parser(
        "init",
        allow_abbrev=False,
        help="幂等创建或核验 v1 空库。",
        description="首次创建五表、约束与索引；已存在 v1 库只核验不改行。",
    )
    store_init.add_argument(
        "--db",
        action=_UniqueStore,
        metavar="<项目内相对文件>",
        help=f"本机 SQLite 路径；省略为 {DEFAULT_DB_PATH}。",
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
    log.emit_degraded_warning()
    return exit_code


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


def _error_payload(reason_code: str) -> dict[str, object]:
    """构造参数、配置或夹具错误的安全固定 JSON 对象。"""
    return {
        "schema_version": 1,
        "status": "failed",
        "reason_code": reason_code,
    }


def _store_success_payload() -> dict[str, object]:
    """返回 store init 成功时的唯一固定 JSON 对象。"""
    return {"schema_version": 1, "status": "ok", "reason_code": None}


def _run_store_init(log: ProcessLog, started: float, args: argparse.Namespace) -> int:
    """解析项目内 --db 并幂等初始化或核验，最多输出一个安全 JSON。"""
    db_value = args.db if args.db is not None else DEFAULT_DB_PATH
    stage_started = time.perf_counter()
    log.record(stage="store", operation="init", result="started")
    try:
        path = _resolve_project_path(db_value)
    except ValueError:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            reason_code="invalid_input",
            error_category="store",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log, started, _error_payload("invalid_input"), 2, "invalid_input", "store"
        )
    try:
        initialize_database(path)
    except StoreError as error:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            reason_code=error.reason_code,
            error_category="store",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "store",
        )
    except Exception:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log, started, _error_payload("store_io_error"), 2, "store_io_error", "store"
        )
    log.record(
        stage="store",
        operation="init",
        result="ok",
        duration_ms=elapsed_ms(stage_started),
    )
    return _finalize_output(log, started, _store_success_payload(), 0, None, None)


def _finish_output_failure(log: ProcessLog, started: float) -> int:
    """输出通道不可用时记录 output=failed，并安全完成 exit。"""
    log.record(
        stage="output",
        result="failed",
        error_category="internal",
        exit_code=2,
    )
    return _finish(log, started, 2, None, "internal")


def _finalize_output(
    log: ProcessLog,
    started: float,
    payload: dict[str, object],
    exit_code: int,
    reason_code: str | None,
    error_category: str | None,
) -> int:
    """唯一 JSON 输出收尾：安全写出、记录 output 事件并完成 exit。"""
    output_started = time.perf_counter()
    if _emit(payload):
        log.record(
            stage="output",
            result="ok",
            duration_ms=elapsed_ms(output_started),
            exit_code=exit_code,
        )
        return _finish(log, started, exit_code, reason_code, error_category)
    return _finish_output_failure(log, started)


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
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_input"),
            2,
            "invalid_input",
            "argument",
        )
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
        # argparse 已直接写出帮助/用法；这里只记录其输出路径，不伪造 doctor JSON。
        log.record(
            stage="output",
            result="ok" if code == 0 else "failed",
            reason_code=None,
            error_category=None if code == 0 else "internal",
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
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_config"),
            2,
            "invalid_config",
            "config",
        )
    except Exception:
        log.record(
            stage="config",
            result="failed",
            reason_code=None,
            error_category="internal",
            config_source=config_source,
        )
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_config"),
            2,
            "invalid_config",
            "internal",
        )
    log.record(stage="config", result="ok", config_source=config_source)

    if args.command == "store":
        return _run_store_init(log, started, args)

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
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_input"),
            2,
            "invalid_input",
            "argument",
        )
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
            return _finalize_output(
                log,
                started,
                _error_payload("invalid_fixture"),
                2,
                "invalid_fixture",
                "fixture",
            )
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
            return _finalize_output(
                log,
                started,
                _error_payload("invalid_input"),
                2,
                "invalid_input",
                "internal",
            )
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
            return _finalize_output(
                log,
                started,
                build_result(
                    status="failed",
                    reason_code=error.reason_code,
                    device_count=device_count,
                    selected_alias=error.selected_alias,
                    unlocked=None,
                    foreground_app="unknown",
                ),
                2,
                error.reason_code,
                "probe",
            )
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
            return _finalize_output(
                log,
                started,
                _error_payload("invalid_input"),
                2,
                "invalid_input",
                "internal",
            )
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
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_input"),
            2,
            "invalid_input",
            "internal",
        )

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
    return _finalize_output(
        log,
        started,
        result,
        exit_code,
        reason_code,
        error_category,
    )
