"""验证 M0 doctor 的 CLI 契约、合成夹具、安全错误出口与真实探针解析。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.adapters.adb.device import (
    AdbDeviceProbe,
    DeviceProbeError,
    _decide_foreground,
    _decide_unlocked,
    _parse_device_list,
)
from app.cli.main import main
from app.ports.device import DeviceEntry, ProbeSnapshot
from app.usecases.doctor import run_doctor

ROOT = Path(__file__).resolve().parents[1]
READY_FIXTURE = "tests/fixtures/doctor/ready.json"
OUTPUT_KEYS = [
    "schema_version",
    "status",
    "reason_code",
    "device_count",
    "selected_alias",
    "unlocked",
    "foreground_app",
]
READY_OUTPUT = {
    "schema_version": 1,
    "status": "ok",
    "reason_code": None,
    "device_count": 1,
    "selected_alias": "primary",
    "unlocked": True,
    "foreground_app": "boss",
}


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """在项目根以 UTF-8 运行一次 python -m app 并捕获标准输出与错误。"""
    return subprocess.run(
        [sys.executable, "-m", "app", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _write_text(directory: Path, name: str, content: str) -> str:
    """在临时项目目录写入文本文件并返回可传给 CLI 的相对路径。"""
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return os.path.relpath(path, ROOT)


def _write_json(directory: Path, name: str, payload: object) -> str:
    """把对象序列化为 UTF-8 JSON 文件并返回相对路径。"""
    return _write_text(directory, name, json.dumps(payload, ensure_ascii=False))


def _business_output(
    status: str,
    reason_code: str | None,
    device_count: int,
    selected_alias: str | None,
    unlocked: bool | None,
    foreground_app: str,
) -> dict[str, object]:
    """构造 doctor 业务结果的完整预期对象。"""
    return {
        "schema_version": 1,
        "status": status,
        "reason_code": reason_code,
        "device_count": device_count,
        "selected_alias": selected_alias,
        "unlocked": unlocked,
        "foreground_app": foreground_app,
    }


def _sqlite_files() -> set[Path]:
    """列出项目内现有 SQLite 文件，用于确认 fixture 模式不写业务库。"""
    return set(ROOT.rglob("*.sqlite3"))


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """构造不依赖真实进程的 subprocess 结果对象。"""
    return subprocess.CompletedProcess(
        args=["adb"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


class DoctorCliTests(unittest.TestCase):
    """覆盖验收命令、配置与参数负例、业务失败和设备状态映射。"""

    def test_ready_fixture_contract(self) -> None:
        """ready 夹具必须退出 0、只输出一个完整 JSON，且不新增业务库。"""
        before = _sqlite_files()
        completed = _run_cli("doctor", "--fixture", READY_FIXTURE)
        after = _sqlite_files()
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload, READY_OUTPUT)
        self.assertEqual(list(payload), OUTPUT_KEYS)
        self.assertEqual(before, after)

    def test_help_exits_zero_without_stderr(self) -> None:
        """帮助路径退出 0，且不向 stderr 写内容。"""
        for args in (("--help",), ("doctor", "--help")):
            with self.subTest(args=args):
                completed = _run_cli(*args)
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(completed.stderr, "")
                self.assertIn("usage:", completed.stdout)

    def test_config_default_and_partial_values(self) -> None:
        """省略配置、空对象和只含一个合法键的配置都取只读默认。"""
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".m0-config-") as tmp:
            cases = {
                "empty": "{}",
                "version-only": '{"schema_version":1}',
                "mode-only": '{"mode":"read_only"}',
                "complete": '{"schema_version":1,"mode":"read_only"}',
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = _write_text(Path(tmp), f"{name}.json", content)
                    completed = _run_cli(
                        "--config",
                        path,
                        "doctor",
                        "--fixture",
                        READY_FIXTURE,
                    )
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(completed.stderr, "")
                    self.assertEqual(json.loads(completed.stdout), READY_OUTPUT)

    def test_negative_configs_are_invalid_config(self) -> None:
        """配置额外键、错误类型、绝对路径、越界或读取失败统一输出 invalid_config。"""
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".m0-config-") as tmp:
            directory = Path(tmp)
            cases = {
                "extra-key": '{"schema_version":1,"mode":"read_only","policy_pack":"x"}',
                "version-null": '{"schema_version":null}',
                "version-bool": '{"schema_version":true}',
                "version-two": '{"schema_version":2}',
                "version-string": '{"schema_version":"1"}',
                "mode-wrong": '{"mode":"draft_only"}',
                "mode-null": '{"mode":null}',
                "not-object": "[]",
                "invalid-json": "{",
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = _write_text(directory, f"{name}.json", content)
                    completed = _run_cli(
                        "--config",
                        path,
                        "doctor",
                        "--fixture",
                        READY_FIXTURE,
                    )
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stderr, "")
                    self.assertEqual(
                        json.loads(completed.stdout),
                        {
                            "schema_version": 1,
                            "status": "failed",
                            "reason_code": "invalid_config",
                        },
                    )

            missing = _run_cli(
                "--config",
                "does-not-exist-m0.json",
                "doctor",
                "--fixture",
                READY_FIXTURE,
            )
            absolute = _run_cli(
                "--config",
                str((directory / "absolute.json").resolve()),
                "doctor",
                "--fixture",
                READY_FIXTURE,
            )
            escaping = _run_cli(
                "--config",
                "..\\outside-m0.json",
                "doctor",
                "--fixture",
                READY_FIXTURE,
            )
            directory_path = _run_cli(
                "--config",
                "tests",
                "doctor",
                "--fixture",
                READY_FIXTURE,
            )
            for completed in (missing, absolute, escaping, directory_path):
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stderr, "")
                self.assertEqual(
                    json.loads(completed.stdout)["reason_code"],
                    "invalid_config",
                )

    def test_negative_modes_and_arguments_are_invalid_input(self) -> None:
        """缺模式、模式冲突、重复/未知选项和 serial 误用统一输出 invalid_input。"""
        cases = {
            "no-command": (),
            "unknown-command": ("sense",),
            "doctor-no-mode": ("doctor",),
            "both-modes": (
                "doctor",
                "--fixture",
                READY_FIXTURE,
                "--device",
                "--serial",
                "synthetic-serial",
            ),
            "device-without-serial": ("doctor", "--device"),
            "fixture-with-serial": (
                "doctor",
                "--fixture",
                READY_FIXTURE,
                "--serial",
                "synthetic-serial",
            ),
            "blank-serial": ("doctor", "--device", "--serial", "   "),
            "empty-fixture": ("doctor", "--fixture="),
            "duplicate-fixture": (
                "doctor",
                "--fixture",
                READY_FIXTURE,
                "--fixture",
                READY_FIXTURE,
            ),
            "duplicate-device": ("doctor", "--device", "--device", "--serial", "x"),
            "duplicate-serial": (
                "doctor",
                "--device",
                "--serial",
                "a",
                "--serial",
                "b",
            ),
            "duplicate-config": (
                "--config",
                "a.json",
                "--config",
                "b.json",
                "doctor",
                "--fixture",
                READY_FIXTURE,
            ),
            "unknown-option": ("doctor", "--unknown"),
            "abbreviated-option": ("doctor", "--fixt", READY_FIXTURE),
            "missing-fixture-value": ("doctor", "--fixture"),
            "missing-config-value": ("--config",),
            "missing-serial-value": ("doctor", "--device", "--serial"),
            "extra-positional": ("doctor", "extra"),
            "config-after-subcommand": (
                "doctor",
                "--config",
                "a.json",
                "--fixture",
                READY_FIXTURE,
            ),
        }
        for name, args in cases.items():
            with self.subTest(name=name):
                completed = _run_cli(*args)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stderr, "")
                self.assertEqual(
                    json.loads(completed.stdout),
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "reason_code": "invalid_input",
                    },
                )

    def test_negative_fixtures_are_invalid_fixture(self) -> None:
        """非法形状、重复别名、非法枚举、错误类型和读取失败输出 invalid_fixture。"""
        valid = {
            "devices": [{"alias": "primary", "state": "device"}],
            "selected_alias": "primary",
            "unlocked": True,
            "foreground_app": "boss",
        }
        cases = {
            "not-object": [],
            "extra-key": {**valid, "extra": 1},
            "missing-key": {
                "devices": valid["devices"],
                "selected_alias": "primary",
                "unlocked": True,
            },
            "devices-not-list": {**valid, "devices": {}},
            "device-not-object": {**valid, "devices": ["primary"]},
            "device-extra-key": {
                **valid,
                "devices": [{"alias": "primary", "state": "device", "extra": 1}],
            },
            "device-missing-state": {**valid, "devices": [{"alias": "primary"}]},
            "empty-alias": {
                **valid,
                "devices": [{"alias": "", "state": "device"}],
                "selected_alias": None,
            },
            "blank-alias": {
                **valid,
                "devices": [{"alias": "   ", "state": "device"}],
                "selected_alias": None,
            },
            "duplicate-alias": {
                **valid,
                "devices": [
                    {"alias": "primary", "state": "device"},
                    {"alias": "primary", "state": "offline"},
                ],
            },
            "invalid-state": {
                **valid,
                "devices": [{"alias": "primary", "state": "bootloader"}],
            },
            "missing-selection": {**valid, "selected_alias": "other"},
            "selection-wrong-type": {**valid, "selected_alias": 1},
            "unlocked-int": {**valid, "unlocked": 1},
            "unlocked-string": {**valid, "unlocked": "true"},
            "foreground-wrong-case": {**valid, "foreground_app": "Boss"},
            "foreground-null": {**valid, "foreground_app": None},
        }
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".m0-fixture-") as tmp:
            directory = Path(tmp)
            for name, payload in cases.items():
                with self.subTest(name=name):
                    path = _write_json(directory, f"{name}.json", payload)
                    completed = _run_cli("doctor", "--fixture", path)
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stderr, "")
                    self.assertEqual(
                        json.loads(completed.stdout),
                        {
                            "schema_version": 1,
                            "status": "failed",
                            "reason_code": "invalid_fixture",
                        },
                    )

            invalid_json = _write_text(directory, "invalid.json", "{")
            missing = "does-not-exist-m0.json"
            absolute = str((directory / "absolute.json").resolve())
            escaping = "..\\outside-m0.json"
            for path in (invalid_json, missing, absolute, escaping):
                with self.subTest(path=path):
                    completed = _run_cli("doctor", "--fixture", path)
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stderr, "")
                    self.assertEqual(
                        json.loads(completed.stdout)["reason_code"],
                        "invalid_fixture",
                    )

    def test_business_failures_use_full_output(self) -> None:
        """设备清单、状态、解锁和前台失败按顺序返回完整业务对象。"""
        cases = {
            "zero-devices": (
                {
                    "devices": [],
                    "selected_alias": None,
                    "unlocked": True,
                    "foreground_app": "boss",
                },
                _business_output(
                    "failed", "device_disconnected", 0, None, True, "boss"
                ),
            ),
            "multiple-unselected": (
                {
                    "devices": [
                        {"alias": "primary", "state": "device"},
                        {"alias": "second", "state": "device"},
                    ],
                    "selected_alias": None,
                    "unlocked": True,
                    "foreground_app": "boss",
                },
                _business_output(
                    "failed", "device_ambiguous", 2, None, True, "boss"
                ),
            ),
            "multiple-selected-offline": (
                {
                    "devices": [
                        {"alias": "primary", "state": "device"},
                        {"alias": "second", "state": "offline"},
                    ],
                    "selected_alias": "second",
                    "unlocked": True,
                    "foreground_app": "boss",
                },
                _business_output(
                    "failed", "device_offline", 2, "second", True, "boss"
                ),
            ),
            "offline": (
                {
                    "devices": [{"alias": "primary", "state": "offline"}],
                    "selected_alias": None,
                    "unlocked": True,
                    "foreground_app": "boss",
                },
                _business_output(
                    "failed", "device_offline", 1, "primary", True, "boss"
                ),
            ),
            "unauthorized": (
                {
                    "devices": [{"alias": "primary", "state": "unauthorized"}],
                    "selected_alias": "primary",
                    "unlocked": True,
                    "foreground_app": "boss",
                },
                _business_output(
                    "failed", "device_unauthorized", 1, "primary", True, "boss"
                ),
            ),
            "unlock-unknown": (
                {
                    "devices": [{"alias": "primary", "state": "device"}],
                    "selected_alias": "primary",
                    "unlocked": None,
                    "foreground_app": "boss",
                },
                _business_output(
                    "failed", "device_locked", 1, "primary", None, "boss"
                ),
            ),
            "locked": (
                {
                    "devices": [{"alias": "primary", "state": "device"}],
                    "selected_alias": "primary",
                    "unlocked": False,
                    "foreground_app": "boss",
                },
                _business_output(
                    "failed", "device_locked", 1, "primary", False, "boss"
                ),
            ),
            "foreground-other": (
                {
                    "devices": [{"alias": "primary", "state": "device"}],
                    "selected_alias": "primary",
                    "unlocked": True,
                    "foreground_app": "other",
                },
                _business_output(
                    "failed", "wrong_page", 1, "primary", True, "other"
                ),
            ),
            "foreground-unknown": (
                {
                    "devices": [{"alias": "primary", "state": "device"}],
                    "selected_alias": "primary",
                    "unlocked": True,
                    "foreground_app": "unknown",
                },
                _business_output(
                    "failed", "wrong_page", 1, "primary", True, "unknown"
                ),
            ),
            "single-auto-selected": (
                {
                    "devices": [{"alias": "only", "state": "device"}],
                    "selected_alias": None,
                    "unlocked": True,
                    "foreground_app": "boss",
                },
                _business_output("ok", None, 1, "only", True, "boss"),
            ),
            "multiple-selected-ready": (
                {
                    "devices": [
                        {"alias": "primary", "state": "device"},
                        {"alias": "second", "state": "offline"},
                    ],
                    "selected_alias": "primary",
                    "unlocked": True,
                    "foreground_app": "boss",
                },
                _business_output("ok", None, 2, "primary", True, "boss"),
            ),
        }
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".m0-business-") as tmp:
            directory = Path(tmp)
            for name, (payload, expected) in cases.items():
                with self.subTest(name=name):
                    path = _write_json(directory, f"{name}.json", payload)
                    completed = _run_cli("doctor", "--fixture", path)
                    expected_code = 0 if expected["status"] == "ok" else 2
                    self.assertEqual(completed.returncode, expected_code)
                    self.assertEqual(completed.stderr, "")
                    self.assertEqual(json.loads(completed.stdout), expected)

    def test_fixture_mode_never_starts_subprocess(self) -> None:
        """fixture 路径在进程内执行时也不得调用任何 subprocess.run。"""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("subprocess.run") as run_mock:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["doctor", "--fixture", READY_FIXTURE])
        run_mock.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), READY_OUTPUT)

    def test_run_doctor_unknown_selected_is_internal_error(self) -> None:
        """快照内部选择与清单不一致时显式失败，而不是伪造业务结论。"""
        snapshot = ProbeSnapshot(
            devices=(DeviceEntry(alias="primary", state="device"),),
            selected_alias="missing",
            unlocked=True,
            foreground_app="boss",
        )
        with self.assertRaises(ValueError):
            run_doctor(snapshot)


class AdbProbeTests(unittest.TestCase):
    """用 mock 的 subprocess 输出验证真实探针，不执行任何真实 ADB。"""

    def test_probe_success_parses_fields_and_call_contract(self) -> None:
        """成功路径解析解锁与前台，并按参数数组、shell=false、5 秒超时执行。"""
        devices = "List of devices attached\nsynthetic-serial\tdevice\n"
        window = (
            "mCurrentFocus=Window{abc123 u0 com.hpbr.bosszhipin/"
            "com.hpbr.bosszhipin.ui.MainActivity}\n"
            "isKeyguardShowing=false\n"
            "isKeyguardShowing=false\n"
        )
        with patch(
            "subprocess.run",
            side_effect=[_completed(devices), _completed(window)],
        ) as run_mock:
            snapshot = AdbDeviceProbe().probe("synthetic-serial")

        self.assertEqual(snapshot.selected_alias, "primary")
        self.assertEqual(snapshot.unlocked, True)
        self.assertEqual(snapshot.foreground_app, "boss")
        self.assertEqual(
            [entry.alias for entry in snapshot.devices],
            ["primary"],
        )
        self.assertEqual(run_doctor(snapshot), READY_OUTPUT)

        calls = run_mock.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0], ["adb", "devices"])
        self.assertEqual(
            calls[1].args[0],
            ["adb", "-s", "synthetic-serial", "shell", "dumpsys window"],
        )
        for call in calls:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertFalse(call.kwargs["check"])

        output = json.dumps(run_doctor(snapshot), ensure_ascii=False)
        self.assertNotIn("synthetic-serial", output)
        self.assertNotIn("synthetic-serial", repr(snapshot))

    def test_offline_unauthorized_and_unknown_states(self) -> None:
        """清单中的 offline、unauthorized 与未知状态分别保守映射。"""
        cases = {
            "offline": ("offline", "device_offline"),
            "unauthorized": ("unauthorized", "device_unauthorized"),
            "unknown": ("bootloader", "device_offline"),
        }
        for name, (state, reason) in cases.items():
            with self.subTest(name=name):
                devices = f"List of devices attached\nsynthetic-serial\t{state}\n"
                with patch("subprocess.run", return_value=_completed(devices)) as run_mock:
                    snapshot = AdbDeviceProbe().probe("synthetic-serial")
                self.assertEqual(run_mock.call_count, 1)
                result = run_doctor(snapshot)
                self.assertEqual(result["reason_code"], reason)
                self.assertEqual(result["selected_alias"], "primary")
                self.assertIsNone(result["unlocked"])
                self.assertEqual(result["foreground_app"], "unknown")

    def test_serial_not_found_is_disconnected(self) -> None:
        """显式 serial 不在清单时报告断连，且只在内存保留精确匹配。"""
        devices = "List of devices attached\nother-serial\tdevice\n"
        with patch("subprocess.run", return_value=_completed(devices)) as run_mock:
            with self.assertRaises(DeviceProbeError) as caught:
                AdbDeviceProbe().probe("synthetic-serial")
        self.assertEqual(run_mock.call_count, 1)
        error = caught.exception
        self.assertEqual(error.reason_code, "device_disconnected")
        self.assertEqual(error.device_count, 1)
        self.assertIsNone(error.selected_alias)
        self.assertNotIn("other-serial", str(error))

    def test_adb_failures_are_disconnected(self) -> None:
        """ADB 缺失、超时或非零退出都归一为断连且不重试。"""
        cases = {
            "oserror": OSError("missing"),
            "timeout": subprocess.TimeoutExpired(["adb"], 5),
            "nonzero": lambda *args, **kwargs: _completed(returncode=1),
        }
        for name, effect in cases.items():
            with self.subTest(name=name):
                with patch("subprocess.run", side_effect=effect) as run_mock:
                    with self.assertRaises(DeviceProbeError) as caught:
                        AdbDeviceProbe().probe("synthetic-serial")
                self.assertEqual(run_mock.call_count, 1)
                self.assertEqual(caught.exception.reason_code, "device_disconnected")

    def test_window_command_failure_is_disconnected(self) -> None:
        """窗口命令失败报告断连，并保留已知清单数量。"""
        devices = "List of devices attached\nsynthetic-serial\tdevice\n"
        with patch(
            "subprocess.run",
            side_effect=[_completed(devices), _completed(returncode=1)],
        ) as run_mock:
            with self.assertRaises(DeviceProbeError) as caught:
                AdbDeviceProbe().probe("synthetic-serial")
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(caught.exception.reason_code, "device_disconnected")
        self.assertEqual(caught.exception.device_count, 1)
        self.assertEqual(caught.exception.selected_alias, "primary")

    def test_keyguard_parsing(self) -> None:
        """全部 false 才解锁，全部 true 为锁屏，缺失或矛盾为未知。"""
        cases = {
            "all-false": ("isKeyguardShowing=false", True),
            "all-true": ("isKeyguardShowing=true", False),
            "mixed": ("isKeyguardShowing=true\nisKeyguardShowing=false", None),
            "missing": ("", None),
            "unexpected": ("isKeyguardShowing=maybe", None),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_decide_unlocked(text), expected)

    def test_current_focus_parsing(self) -> None:
        """仅唯一可解析 mCurrentFocus 才判定 boss 或 other，其余为未知。"""
        cases = {
            "boss": (
                "mCurrentFocus=Window{abc u0 com.hpbr.bosszhipin/"
                "com.hpbr.bosszhipin.ui.MainActivity}",
                "boss",
            ),
            "other": (
                "mCurrentFocus=Window{abc u0 com.example.other/.Main}",
                "other",
            ),
            "missing": ("", "unknown"),
            "null": ("mCurrentFocus=null", "unknown"),
            "invalid": ("mCurrentFocus=Window{invalid}", "unknown"),
            "multiple": (
                "mCurrentFocus=Window{abc u0 com.hpbr.bosszhipin/.Main}\n"
                "mCurrentFocus=Window{def u0 com.example.other/.Main}",
                "unknown",
            ),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_decide_foreground(text), expected)

    def test_device_list_parsing_ignores_non_device_lines(self) -> None:
        """清单解析忽略标题、守护进程提示和空行。"""
        text = (
            "* daemon not running; starting now at tcp:5037\n"
            "adb server version (41) doesn't match this client (39)\n"
            "List of devices attached\n"
            "\n"
            "serial-a\tdevice\n"
            "serial-b\toffline extra\n"
        )
        self.assertEqual(
            _parse_device_list(text),
            [("serial-a", "device"), ("serial-b", "offline")],
        )


if __name__ == "__main__":
    unittest.main()
