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
from app.process_log import EVENT_KEYS, ProcessLog
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


def _read_log(path: Path) -> list[dict[str, object]]:
    """逐行解析过程日志，返回事件对象列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _event_stages(events: list[dict[str, object]]) -> list[str]:
    """提取事件阶段序列，便于断言固定流程。"""
    return [str(event["stage"]) for event in events]


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
                "duplicate-key": '{"schema_version":1,"mode":"read_only","mode":"read_only"}',
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
                    self.assertNotIn('"mode"', completed.stdout)
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
            duplicate_top = _write_text(
                directory,
                "duplicate-top.json",
                '{"devices":[],"devices":[],"selected_alias":null,'
                '"unlocked":true,"foreground_app":"boss"}',
            )
            duplicate_nested = _write_text(
                directory,
                "duplicate-nested.json",
                '{"devices":[{"alias":"primary","alias":"primary",'
                '"state":"device"}],"selected_alias":"primary",'
                '"unlocked":true,"foreground_app":"boss"}',
            )
            lone_alias = _write_text(
                directory,
                "lone-alias.json",
                '{"devices":[{"alias":"\\ud800","state":"device"}],'
                '"selected_alias":null,"unlocked":true,'
                '"foreground_app":"boss"}',
            )
            lone_selected = _write_text(
                directory,
                "lone-selected.json",
                '{"devices":[{"alias":"primary","state":"device"}],'
                '"selected_alias":"\\ud800","unlocked":true,'
                '"foreground_app":"boss"}',
            )
            missing = "does-not-exist-m0.json"
            absolute = str((directory / "absolute.json").resolve())
            escaping = "..\\outside-m0.json"
            for path in (
                invalid_json,
                duplicate_top,
                duplicate_nested,
                lone_alias,
                lone_selected,
                missing,
                absolute,
                escaping,
            ):
                with self.subTest(path=path):
                    completed = _run_cli("doctor", "--fixture", path)
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stderr, "")
                    self.assertEqual(completed.stdout.count("\n"), 1)
                    self.assertNotIn('"alias"', completed.stdout)
                    self.assertNotIn('"devices"', completed.stdout)
                    self.assertNotIn("\\ud800", completed.stdout)
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
        with tempfile.TemporaryDirectory() as tmp:
            log = ProcessLog(path=Path(tmp) / "m0-process.jsonl")
            with patch("subprocess.run") as run_mock:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        ["doctor", "--fixture", READY_FIXTURE],
                        process_log=log,
                    )
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

    def test_adb_failure_log_categories_are_distinct(self) -> None:
        """过程日志用固定类别区分 ADB 缺失、超时、其它 IO 错误与非零退出。"""
        cases = {
            "missing": (FileNotFoundError("missing"), "adb_missing"),
            "timeout": (subprocess.TimeoutExpired(["adb"], 5), "adb_timeout"),
            "oserror": (PermissionError("denied"), "adb_oserror"),
            "nonzero": (
                lambda *args, **kwargs: _completed(returncode=1),
                "adb_nonzero",
            ),
        }
        for name, (effect, category) in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "m0-process.jsonl"
                    log = ProcessLog(path=path, correlation_id=f"corr-{name}")
                    with patch("subprocess.run", side_effect=effect):
                        with self.assertRaises(DeviceProbeError) as caught:
                            AdbDeviceProbe(process_log=log).probe("synthetic-serial")
                    self.assertEqual(
                        caught.exception.reason_code,
                        "device_disconnected",
                    )
                    events = _read_log(path)
                    log_text = path.read_text(encoding="utf-8")
                self.assertEqual(len(events), 2)
                started, failed = events
                self.assertEqual(started["stage"], "adb_devices")
                self.assertEqual(started["result"], "started")
                self.assertEqual(started["command"], "adb_devices")
                self.assertTrue(
                    all(
                        event["correlation_id"] == f"corr-{name}"
                        for event in events
                    )
                )
                self.assertEqual(failed["stage"], "adb_devices")
                self.assertEqual(failed["result"], "failed")
                self.assertEqual(failed["command"], "adb_devices")
                self.assertEqual(failed["reason_code"], "device_disconnected")
                self.assertEqual(failed["error_category"], category)
                self.assertNotIn("synthetic-serial", log_text)

    def test_adb_started_event_survives_interruption(self) -> None:
        """命令在途异常未归一为失败时，日志仍保留 started 事件可定位中断。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            log = ProcessLog(path=path, correlation_id="corr-interrupt")
            with patch("subprocess.run", side_effect=RuntimeError("interrupt")):
                with self.assertRaises(RuntimeError):
                    AdbDeviceProbe(process_log=log).probe("synthetic-serial")
            events = _read_log(path)
            log_text = path.read_text(encoding="utf-8")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "adb_devices")
        self.assertEqual(events[0]["result"], "started")
        self.assertEqual(events[0]["command"], "adb_devices")
        self.assertEqual(events[0]["correlation_id"], "corr-interrupt")
        self.assertNotIn("synthetic-serial", log_text)

    def test_prompt_line_is_not_counted_as_device(self) -> None:
        """ADB 提示行不得被当成设备，serial 未命中时计数为 0。"""
        devices = "List of devices attached\nadb: no devices/emulators found\n"
        with patch("subprocess.run", return_value=_completed(devices)) as run_mock:
            with self.assertRaises(DeviceProbeError) as caught:
                AdbDeviceProbe().probe("synthetic-serial")
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(caught.exception.reason_code, "device_disconnected")
        self.assertEqual(caught.exception.device_count, 0)

    def test_window_command_failure_is_disconnected(self) -> None:
        """窗口命令失败报告断连，并保留已知清单数量与 started/failed 顺序。"""
        devices = "List of devices attached\nsynthetic-serial\tdevice\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            log = ProcessLog(path=path, correlation_id="corr-window-fail")
            with patch(
                "subprocess.run",
                side_effect=[_completed(devices), _completed(returncode=1)],
            ) as run_mock:
                with self.assertRaises(DeviceProbeError) as caught:
                    AdbDeviceProbe(process_log=log).probe("synthetic-serial")
            events = _read_log(path)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(caught.exception.reason_code, "device_disconnected")
        self.assertEqual(caught.exception.device_count, 1)
        self.assertEqual(caught.exception.selected_alias, "primary")
        self.assertEqual(
            [(event["stage"], event["result"], event["command"]) for event in events],
            [
                ("adb_devices", "started", "adb_devices"),
                ("adb_devices", "ok", "adb_devices"),
                ("adb_window", "started", "adb_window"),
                ("adb_window", "failed", "adb_window"),
            ],
        )
        self.assertEqual(events[-1]["error_category"], "adb_nonzero")

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

    def test_keyguard_malformed_tokens_never_unlock(self) -> None:
        """畸形布尔 token、缺值和合法/畸形混杂都不得推定解锁。"""
        cases = {
            "false123": ("isKeyguardShowing=false123", None),
            "false_extra": ("isKeyguardShowing=false_extra", None),
            "false-hyphen": ("isKeyguardShowing=false-extra", None),
            "false_": ("isKeyguardShowing=false_", None),
            "missing_value": ("isKeyguardShowing=", None),
            "missing_equals": ("isKeyguardShowing", None),
            "malformed_then_false": (
                "isKeyguardShowing=false123\nisKeyguardShowing=false",
                None,
            ),
            "same_segment_mixed": (
                "isKeyguardShowing=false123 isKeyguardShowing=false",
                None,
            ),
            "valid_mixed": (
                "isKeyguardShowing=false\nisKeyguardShowing=true",
                None,
            ),
            "repeat_false": (
                "isKeyguardShowing=false\nisKeyguardShowing=false",
                True,
            ),
            "repeat_true": (
                "isKeyguardShowing=true\nisKeyguardShowing=true",
                False,
            ),
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

    def test_current_focus_rejects_multiple_components_in_one_line(self) -> None:
        """同一 mCurrentFocus 行出现多个组件或畸形组件时不得取第一个。"""
        cases = {
            "two-different": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main} "
                "Window{b u0 com.example.other/.Main}",
                "unknown",
            ),
            "two-boss": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main} "
                "Window{b u0 com.hpbr.bosszhipin/.Other}",
                "unknown",
            ),
            "valid-plus-malformed": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main} "
                "Window{broken}",
                "unknown",
            ),
            "malformed-plus-valid": (
                "mCurrentFocus=Window{broken} "
                "Window{b u0 com.example.other/.Main}",
                "unknown",
            ),
            "two-components-inside-one-window": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main "
                "com.example.other/.Main}",
                "unknown",
            ),
            "two-boss-components-inside-one-window": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main "
                "com.hpbr.bosszhipin/.Other}",
                "unknown",
            ),
            "suffix-garbage": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main} garbage",
                "unknown",
            ),
            "prefix-garbage": (
                "mCurrentFocus=prefix Window{a u0 com.hpbr.bosszhipin/.Main}",
                "unknown",
            ),
            "suffix-component": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main} "
                "com.example.other/.Main",
                "unknown",
            ),
            "single-boss": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main}",
                "boss",
            ),
            "single-other": (
                "mCurrentFocus=Window{b u0 com.example.other/.Main}",
                "other",
            ),
            "single-boss-surrounding-whitespace": (
                "mCurrentFocus=  Window{a u0 com.hpbr.bosszhipin/.Main}  ",
                "boss",
            ),
            "single-boss-extra-whitespace": (
                "mCurrentFocus=Window{abc   u0\t"
                "com.hpbr.bosszhipin/.Main}",
                "boss",
            ),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_decide_foreground(text), expected)

    def test_current_focus_requires_field_boundary(self) -> None:
        """mCurrentFocus 字段名前后不得粘连畸形前缀/后缀，缩进字段保持可读。"""
        cases = {
            "not-prefix": (
                "notmCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main}",
                "unknown",
            ),
            "word-prefix": (
                "prefixmCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main}",
                "unknown",
            ),
            "field-suffix": (
                "mCurrentFocusExtra=Window{a u0 com.hpbr.bosszhipin/.Main}",
                "unknown",
            ),
            "indented": (
                "  mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main}",
                "boss",
            ),
            "multiple-fields": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main}\n"
                "  mCurrentFocus=Window{b u0 com.example.other/.Main}",
                "unknown",
            ),
            "malformed-plus-valid": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main}\n"
                "notmCurrentFocus=Window{b u0 com.hpbr.bosszhipin/.Main}",
                "unknown",
            ),
            "suffix-field": (
                "mCurrentFocus=Window{a u0 com.hpbr.bosszhipin/.Main} "
                "mCurrentFocusExtra=x",
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
            "adb: no devices/emulators found\n"
            "error:\tno devices/emulators found\n"
            "List of devices attached\n"
            "\n"
            "serial-a\tdevice\n"
            "serial-b\toffline extra\n"
        )
        self.assertEqual(
            _parse_device_list(text),
            [("serial-a", "device"), ("serial-b", "offline")],
        )


class ProcessLogTests(unittest.TestCase):
    """验证本机过程日志的固定字段、白名单与静默失败边界。"""

    def test_appends_whitelisted_json_lines(self) -> None:
        """合法事件逐行追加，字段集合和关联 ID 保持固定。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            log = ProcessLog(path=path, correlation_id="corr-whitelist")
            events = [
                {"stage": "start", "result": "started"},
                {"stage": "arguments", "result": "ok"},
                {
                    "stage": "config",
                    "result": "ok",
                    "config_source": "default",
                },
                {"stage": "decision", "result": "ok", "device_count": 1},
                {"stage": "output", "result": "ok", "exit_code": 0},
                {
                    "stage": "exit",
                    "result": "ok",
                    "exit_code": 0,
                    "duration_ms": 5,
                },
            ]
            for event in events:
                self.assertTrue(log.record(**event))
            parsed = _read_log(path)

        self.assertEqual(len(parsed), len(events))
        for event in parsed:
            self.assertEqual(list(event), list(EVENT_KEYS))
            self.assertEqual(event["schema_version"], 1)
            self.assertEqual(event["correlation_id"], "corr-whitelist")
        self.assertEqual(_event_stages(parsed), [event["stage"] for event in events])
        self.assertEqual(parsed[-1]["exit_code"], 0)

    def test_rejects_non_whitelisted_values(self) -> None:
        """非固定阶段、原因码、类别、命令或类型都拒绝写入。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            log = ProcessLog(path=path)
            cases = {
                "stage": {"stage": "raw", "result": "ok"},
                "result": {"stage": "start", "result": "done"},
                "reason": {
                    "stage": "start",
                    "result": "ok",
                    "reason_code": "secret",
                },
                "category": {
                    "stage": "start",
                    "result": "ok",
                    "error_category": "secret",
                },
                "duration-bool": {
                    "stage": "start",
                    "result": "ok",
                    "duration_ms": True,
                },
                "command": {
                    "stage": "start",
                    "result": "ok",
                    "command": "adb devices",
                },
                "exit-code": {
                    "stage": "start",
                    "result": "ok",
                    "exit_code": 1,
                },
                "mode": {"stage": "start", "result": "ok", "mode": "auto"},
                "config-source": {
                    "stage": "start",
                    "result": "ok",
                    "config_source": "path",
                },
                "count-bool": {
                    "stage": "start",
                    "result": "ok",
                    "device_count": True,
                },
                "count-negative": {
                    "stage": "start",
                    "result": "ok",
                    "device_count": -1,
                },
            }
            for name, event in cases.items():
                with self.subTest(name=name):
                    self.assertFalse(log.record(**event))
            self.assertTrue(log.degraded)
            self.assertFalse(path.exists())

    def test_append_failure_is_silent_and_returns_false(self) -> None:
        """日志 IO 失败必须返回 False，且不向 stdout/stderr 泄露异常原值。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            log = ProcessLog(path=path)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                ProcessLog,
                "_append",
                side_effect=OSError("secret /absolute/path"),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    recorded = log.record(stage="start", result="started")
            self.assertFalse(recorded)
            self.assertTrue(log.degraded)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertFalse(path.exists())


class DoctorLoggingTests(unittest.TestCase):
    """验证 doctor 全流程日志可追溯，且不影响既有 JSON 与安全出口。"""

    def test_fixture_run_logs_stages_and_correlation(self) -> None:
        """fixture 成功流程记录 start 到 exit，且不写夹具路径或业务值。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    ["doctor", "--fixture", READY_FIXTURE],
                    process_log=ProcessLog(
                        path=path,
                        correlation_id="corr-fixture",
                    ),
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(json.loads(stdout.getvalue()), READY_OUTPUT)
            events = _read_log(path)
            log_text = path.read_text(encoding="utf-8")

        self.assertEqual(
            _event_stages(events),
            [
                "start",
                "arguments",
                "config",
                "mode",
                "fixture",
                "fixture",
                "decision",
                "output",
                "exit",
            ],
        )
        self.assertTrue(
            all(event["correlation_id"] == "corr-fixture" for event in events)
        )
        self.assertEqual(events[-1]["exit_code"], 0)
        self.assertEqual(events[-1]["result"], "ok")
        self.assertEqual(events[-2]["stage"], "output")
        self.assertEqual(events[-2]["result"], "ok")
        self.assertEqual(events[-3]["stage"], "decision")
        self.assertEqual(events[-3]["device_count"], 1)
        self.assertNotIn("tests/fixtures", log_text)
        self.assertNotIn("primary", log_text)
        self.assertNotIn('"boss"', log_text)

    def test_device_run_logs_two_commands_without_raw_values(self) -> None:
        """显式设备流程记录两条固定命令事件，不写 serial 或窗口原文。"""
        devices = "List of devices attached\nsynthetic-serial\tdevice\n"
        window = (
            "mCurrentFocus=Window{abc u0 com.hpbr.bosszhipin/"
            "com.hpbr.bosszhipin.ui.MainActivity}\n"
            "isKeyguardShowing=false\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch(
                "subprocess.run",
                side_effect=[_completed(devices), _completed(window)],
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        ["doctor", "--device", "--serial", "synthetic-serial"],
                        process_log=ProcessLog(
                            path=path,
                            correlation_id="corr-device",
                        ),
                    )
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(json.loads(stdout.getvalue()), READY_OUTPUT)
            events = _read_log(path)
            log_text = path.read_text(encoding="utf-8")

        self.assertEqual(
            _event_stages(events),
            [
                "start",
                "arguments",
                "config",
                "mode",
                "probe",
                "adb_devices",
                "adb_devices",
                "adb_window",
                "adb_window",
                "probe",
                "decision",
                "output",
                "exit",
            ],
        )
        command_events = [
            (event["stage"], event["result"], event["command"])
            for event in events
            if event["command"]
        ]
        self.assertEqual(
            command_events,
            [
                ("adb_devices", "started", "adb_devices"),
                ("adb_devices", "ok", "adb_devices"),
                ("adb_window", "started", "adb_window"),
                ("adb_window", "ok", "adb_window"),
            ],
        )
        self.assertTrue(
            all(event["correlation_id"] == "corr-device" for event in events)
        )
        self.assertNotIn("synthetic-serial", log_text)
        self.assertNotIn("com.hpbr", log_text)
        self.assertNotIn("isKeyguardShowing", log_text)
        self.assertNotIn("mCurrentFocus", log_text)

    def test_invalid_fixture_logs_fixed_reason_category(self) -> None:
        """非法夹具记录固定 invalid_fixture/fixture，且不回显原内容。"""
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".m0-log-fixture-") as tmp:
            directory = Path(tmp)
            fixture_path = _write_text(directory, "bad.json", "{")
            log_path = Path(tmp) / "m0-process.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    ["doctor", "--fixture", fixture_path],
                    process_log=ProcessLog(
                        path=log_path,
                        correlation_id="corr-invalid",
                    ),
                )
            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "schema_version": 1,
                    "status": "failed",
                    "reason_code": "invalid_fixture",
                },
            )
            events = _read_log(log_path)
            log_text = log_path.read_text(encoding="utf-8")

        failed = [
            event
            for event in events
            if event["stage"] == "fixture" and event["result"] == "failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason_code"], "invalid_fixture")
        self.assertEqual(failed[0]["error_category"], "fixture")
        self.assertEqual(events[-2]["stage"], "output")
        self.assertEqual(events[-2]["result"], "ok")
        self.assertEqual(events[-1]["stage"], "exit")
        self.assertEqual(events[-1]["exit_code"], 2)
        self.assertEqual(events[-1]["reason_code"], "invalid_fixture")
        self.assertNotIn("bad.json", log_text)

    def test_log_io_failure_marks_degraded_without_changing_output(self) -> None:
        """日志写入失败时 stdout/退出码不变，但 stderr 有明显固定降级标记。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                ProcessLog,
                "_append",
                side_effect=OSError("secret /absolute/path"),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        ["doctor", "--fixture", READY_FIXTURE],
                        process_log=ProcessLog(path=path),
                    )
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "m0_process_log_degraded\n")
            self.assertEqual(json.loads(stdout.getvalue()), READY_OUTPUT)
            self.assertFalse(path.exists())

    def test_stdout_write_failures_exit_two_without_traceback(self) -> None:
        """stdout 写出 UnicodeError/OSError 时安全退出 2，不输出 traceback。"""
        errors = {
            "oserror": OSError("secret /absolute/path"),
            "unicode": UnicodeEncodeError(
                "utf-8",
                "\ud800",
                0,
                1,
                "surrogate",
            ),
        }

        class _FailingStdout:
            """模拟已损坏的标准输出通道。"""

            def __init__(self, error: Exception) -> None:
                """保存要抛出的输出错误。"""
                self._error = error

            def write(self, text: str) -> int:
                """模拟写入失败，不产生任何 stdout 内容。"""
                raise self._error

            def flush(self) -> None:
                """模拟刷新空操作。"""
                return None

        for name, error in errors.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "m0-process.jsonl"
                    stderr = io.StringIO()
                    with redirect_stdout(_FailingStdout(error)):
                        with redirect_stderr(stderr):
                            code = main(
                                ["doctor", "--fixture", READY_FIXTURE],
                                process_log=ProcessLog(path=path),
                            )
                    self.assertEqual(code, 2)
                    self.assertNotIn("Traceback", stderr.getvalue())
                    self.assertNotIn("secret", stderr.getvalue())
                    events = _read_log(path)
                output_events = [
                    event for event in events if event["stage"] == "output"
                ]
                self.assertEqual(len(output_events), 1)
                self.assertEqual(output_events[0]["result"], "failed")
                self.assertEqual(output_events[0]["error_category"], "internal")
                self.assertEqual(output_events[0]["exit_code"], 2)
                self.assertEqual(events[-1]["stage"], "exit")
                self.assertEqual(events[-1]["exit_code"], 2)
                self.assertEqual(events[-1]["result"], "failed")
                self.assertEqual(events[-1]["error_category"], "internal")

    def test_device_failure_maps_seven_keys_and_keeps_no_business_db(self) -> None:
        """CLI 设备失败复用唯一组装函数，输出七键且不新增业务库。"""
        before = _sqlite_files()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("subprocess.run", side_effect=FileNotFoundError("missing")):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        ["doctor", "--device", "--serial", "synthetic-serial"],
                        process_log=ProcessLog(
                            path=path,
                            correlation_id="corr-device-failure",
                        ),
                    )
            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue(), "")
            payload = json.loads(stdout.getvalue())
            self.assertEqual(list(payload), OUTPUT_KEYS)
            self.assertEqual(
                payload,
                _business_output(
                    "failed",
                    "device_disconnected",
                    0,
                    None,
                    None,
                    "unknown",
                ),
            )
            events = _read_log(path)
        self.assertEqual(before, _sqlite_files())
        device_events = [
            event for event in events if event["stage"] == "adb_devices"
        ]
        self.assertEqual(len(device_events), 2)
        self.assertEqual(device_events[0]["result"], "started")
        self.assertEqual(device_events[1]["result"], "failed")
        self.assertEqual(device_events[1]["error_category"], "adb_missing")
        self.assertEqual(
            device_events[1]["reason_code"],
            "device_disconnected",
        )
        self.assertEqual(events[-2]["stage"], "output")
        self.assertEqual(events[-2]["result"], "ok")
        self.assertEqual(events[-1]["exit_code"], 2)
        self.assertNotIn("synthetic-serial", stdout.getvalue())

    def test_help_logs_argparse_output_path(self) -> None:
        """--help 保持 argparse 标准行为，并记录 output/exit 而不伪造 JSON。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    main(
                        ["--help"],
                        process_log=ProcessLog(
                            path=path,
                            correlation_id="corr-help",
                        ),
                    )
            self.assertEqual(caught.exception.code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("usage:", stdout.getvalue())
            events = _read_log(path)
        self.assertEqual(
            _event_stages(events),
            ["start", "arguments", "output", "exit"],
        )
        self.assertEqual(events[2]["result"], "ok")
        self.assertEqual(events[-1]["exit_code"], 0)

    def test_whitelist_rejection_marks_cli_degraded(self) -> None:
        """白名单拒绝也置 degraded，CLI 仍输出固定降级提示且不写非法事件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m0-process.jsonl"
            log = ProcessLog(path=path, correlation_id="corr-invalid-log")
            self.assertFalse(log.record(stage="invalid-stage", result="ok"))
            self.assertTrue(log.degraded)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    ["doctor", "--fixture", READY_FIXTURE],
                    process_log=log,
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "m0_process_log_degraded\n")
            self.assertEqual(json.loads(stdout.getvalue()), READY_OUTPUT)
            events = _read_log(path)
            log_text = path.read_text(encoding="utf-8")
        self.assertEqual(
            _event_stages(events),
            [
                "start",
                "arguments",
                "config",
                "mode",
                "fixture",
                "fixture",
                "decision",
                "output",
                "exit",
            ],
        )
        self.assertNotIn("invalid-stage", log_text)


if __name__ == "__main__":
    unittest.main()
