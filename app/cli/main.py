"""解析严格 CLI 参数、加载最小配置，并输出唯一的 UTF-8 JSON 结果。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from app.adapters.adb.device import AdbDeviceProbe, DeviceProbeError
from app.ports.device import ProbeSnapshot
from app.usecases.doctor import parse_probe_input, run_doctor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = {"schema_version": 1, "mode": "read_only"}


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


def _load_config(value: str | None) -> dict[str, object]:
    """读取并校验最小配置；省略或空对象都收敛为只读默认值。"""
    if value is None:
        return dict(DEFAULT_CONFIG)
    try:
        path = _resolve_project_path(value)
        data = json.loads(path.read_text(encoding="utf-8"))
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
    return {**DEFAULT_CONFIG, **data}


def _load_fixture(value: str) -> ProbeSnapshot:
    """读取并严格校验合成探针输入，不启动子进程也不写任何文件。"""
    try:
        path = _resolve_project_path(value)
        data = json.loads(path.read_text(encoding="utf-8"))
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


def _device_failure(error: DeviceProbeError) -> dict[str, object]:
    """把真实探针失败转成保留已知数量的安全 doctor 结果。"""
    return {
        "schema_version": 1,
        "status": "failed",
        "reason_code": error.reason_code,
        "device_count": max(0, error.device_count),
        "selected_alias": error.selected_alias,
        "unlocked": None,
        "foreground_app": "unknown",
    }


def _emit(payload: dict[str, object]) -> None:
    """把白名单 JSON 对象以单行 UTF-8 写入 stdout。"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def _emit_error(reason_code: str) -> None:
    """输出参数、配置或夹具错误的安全固定对象。"""
    _emit(
        {
            "schema_version": 1,
            "status": "failed",
            "reason_code": reason_code,
        }
    )


def main(argv: list[str] | None = None) -> int:
    """执行一次 doctor 调用并返回 0 或 2，不向 stderr 回显输入或异常。"""
    try:
        args = _build_parser().parse_args(argv)
    except _CliArgumentError:
        _emit_error("invalid_input")
        return 2

    try:
        _load_config(args.config)
    except _ConfigError:
        _emit_error("invalid_config")
        return 2
    except Exception:
        _emit_error("invalid_config")
        return 2

    if not _mode_is_valid(args):
        _emit_error("invalid_input")
        return 2

    snapshot: ProbeSnapshot
    try:
        if args.fixture is not None:
            snapshot = _load_fixture(args.fixture)
        else:
            snapshot = AdbDeviceProbe().probe(args.serial)
    except _FixtureError:
        _emit_error("invalid_fixture")
        return 2
    except DeviceProbeError as error:
        _emit(_device_failure(error))
        return 2
    except Exception:
        _emit_error("invalid_input")
        return 2

    try:
        result = run_doctor(snapshot)
    except Exception:
        _emit_error("invalid_input")
        return 2
    _emit(result)
    return 0 if result["status"] == "ok" else 2
