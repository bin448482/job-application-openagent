"""验证 M3 ADB 采集适配器的参数、超时、不重试、证据保存与共享门禁。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.adb.screen import ADB_TIMEOUT_SECONDS, AdbScreenReader
from app.adapters.boss.read import parse_page
from app.ports.screen import (
    DeviceGate,
    DeviceGateUnavailable,
    HierarchyCapture,
    ScreenCapture,
    ScreenReadError,
)
from app.process_log import ProcessLog
from app.usecases.read import read_from_device

BASE_TIME = "2026-11-24T00:00:00.000Z"
SNAPSHOT_ID = "00000000-0000-4000-8000-000000000401"
SERIAL = "synthetic-serial"
PAGE_XML = '<hierarchy rotation="0"><node text="消息" bounds="[0,0][10,10]" /></hierarchy>'


class _Gate:
    """模拟共享设备门禁：可选拒绝，并记录检查次数。"""

    def __init__(self, unavailable: bool) -> None:
        """保存是否拒绝与检查计数。"""
        self.unavailable = unavailable
        self.checks = 0
        self.releases = 0

    def require_available(self) -> None:
        """计数后按配置拒绝或放行。"""
        self.checks += 1
        if self.unavailable:
            raise DeviceGateUnavailable()

    def verify(self) -> None:
        """每条设备命令前复核占用，复用同一拒绝开关。"""
        self.checks += 1
        if self.unavailable:
            raise DeviceGateUnavailable()

    def release(self) -> None:
        """记录释放次数。"""
        self.releases += 1


class _Screen:
    """模拟采集端口，记录是否被调用。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.dump_calls = 0
        self.capture_calls = 0

    def dump_hierarchy(self, serial: str) -> HierarchyCapture:
        """返回合成层级并记录调用。"""
        self.dump_calls += 1
        return HierarchyCapture(PAGE_XML, "hierarchy.xml", len(PAGE_XML))

    def capture_screen(self, serial: str) -> ScreenCapture:
        """返回合成截图并记录调用。"""
        self.capture_calls += 1
        return ScreenCapture(b"raw", "screen.rgba", 3)


class ScreenAdapterTests(unittest.TestCase):
    """覆盖 ADB 适配器成功、失败、无重试、证据与本机日志。"""

    def setUp(self) -> None:
        """创建临时证据与日志目录。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = ProcessLog(path=self.root / "logs" / "process.jsonl")

    def tearDown(self) -> None:
        """清理本次临时目录。"""
        self.temp.cleanup()

    def test_dump_hierarchy_uses_exact_args_timeout_and_saves_evidence(self) -> None:
        """dump/cat 使用参数数组、shell=false、5 秒超时且各执行一次。"""
        reader = AdbScreenReader(self.root / "evidence", process_log=self.log)
        completed = [
            subprocess.CompletedProcess([], 0, stdout="UI hierarchy dumped"),
            subprocess.CompletedProcess([], 0, stdout=PAGE_XML),
        ]
        with patch("subprocess.run", side_effect=completed) as run:
            capture = reader.dump_hierarchy(SERIAL)
        self.assertEqual(run.call_count, 2)
        first, second = run.call_args_list
        self.assertEqual(
            first.args[0],
            ["adb", "-s", SERIAL, "shell", "uiautomator", "dump", "/data/local/tmp/m3-ui.xml"],
        )
        self.assertEqual(
            second.args[0],
            ["adb", "-s", SERIAL, "shell", "cat", "/data/local/tmp/m3-ui.xml"],
        )
        for call in (first, second):
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], ADB_TIMEOUT_SECONDS)
        self.assertEqual(capture.xml_text, PAGE_XML)
        evidence = self.root / "evidence" / capture.evidence_name
        self.assertTrue(evidence.is_file())
        self.assertEqual(evidence.read_text(encoding="utf-8"), PAGE_XML)
        events = self._events()
        self.assertEqual(
            [event["command"] for event in events if event["command"]],
            [
                "adb_hierarchy_dump",
                "adb_hierarchy_dump",
                "adb_hierarchy_cat",
                "adb_hierarchy_cat",
            ],
        )
        self.assertEqual(
            [event["result"] for event in events if event["command"]],
            ["started", "ok", "started", "ok"],
        )
        self.assertNotIn(
            SERIAL,
            next((self.root / "logs").glob("process-*.jsonl")).read_text(encoding="utf-8"),
        )

    def test_capture_screen_is_binary_and_saved(self) -> None:
        """screencap 以二进制执行一次并保存原始证据。"""
        reader = AdbScreenReader(self.root / "evidence", process_log=self.log)
        raw = b"screen-bytes"
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=raw),
        ) as run:
            capture = reader.capture_screen(SERIAL)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args.args[0], ["adb", "-s", SERIAL, "exec-out", "screencap"]
        )
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], ADB_TIMEOUT_SECONDS)
        self.assertEqual(capture.raw, raw)
        self.assertEqual(
            (self.root / "evidence" / capture.evidence_name).read_bytes(), raw
        )

    def test_failures_are_single_attempt_device_disconnected(self) -> None:
        """缺失、超时、OSError、非零退出均为单次 attempt 的断连。"""
        reader = AdbScreenReader(self.root / "evidence", process_log=self.log)
        failures = [
            FileNotFoundError(),
            subprocess.TimeoutExpired(["adb"], ADB_TIMEOUT_SECONDS),
            OSError(),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch("subprocess.run", side_effect=failure) as run:
                    with self.assertRaises(ScreenReadError) as caught:
                        reader.dump_hierarchy(SERIAL)
                self.assertEqual(caught.exception.reason_code, "device_disconnected")
                self.assertEqual(run.call_count, 1)
        with patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 1, stdout="")
        ) as run:
            with self.assertRaises(ScreenReadError) as caught:
                reader.dump_hierarchy(SERIAL)
        self.assertEqual(caught.exception.reason_code, "device_disconnected")
        self.assertEqual(run.call_count, 1)

    def test_evidence_write_failure_does_not_fake_saved(self) -> None:
        """证据目录不可写时报 invalid_fixture，不声称已保存。"""
        evidence_file = self.root / "evidence-file"
        evidence_file.write_text("x", encoding="utf-8")
        reader = AdbScreenReader(evidence_file, process_log=self.log)
        with patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, stdout="dump"),
                subprocess.CompletedProcess([], 0, stdout=PAGE_XML),
            ],
        ):
            with self.assertRaises(ScreenReadError) as caught:
                reader.dump_hierarchy(SERIAL)
        self.assertEqual(caught.exception.reason_code, "invalid_fixture")
        events = self._events()
        self.assertTrue(
            any(
                event["stage"] == "adb_evidence"
                and event["result"] == "failed"
                and event["reason_code"] == "invalid_fixture"
                and event["error_category"] == "evidence"
                for event in events
            )
        )

    def test_reader_rechecks_gate_before_every_subprocess(self) -> None:
        """dump/cat 与 screencap 的每条子命令前都调用共享门禁复核。"""
        gate = _Gate(unavailable=False)
        reader = AdbScreenReader(self.root / "evidence", gate=gate)
        with patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, stdout="dumped"),
                subprocess.CompletedProcess([], 0, stdout=PAGE_XML),
            ],
        ):
            reader.dump_hierarchy(SERIAL)
        self.assertEqual(gate.checks, 2)
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=b"raw"),
        ):
            reader.capture_screen(SERIAL)
        self.assertEqual(gate.checks, 3)

    def test_serial_is_required(self) -> None:
        """空 serial 在子进程启动前拒绝。"""
        reader = AdbScreenReader(self.root / "evidence")
        with patch("subprocess.run") as run:
            with self.assertRaises(ValueError):
                reader.dump_hierarchy("")
        run.assert_not_called()

    def _events(self) -> list[dict]:
        """读取本次日志事件。"""
        files = list((self.root / "logs").glob("process-*.jsonl"))
        self.assertEqual(len(files), 1)
        return [
            __import__("json").loads(line)
            for line in files[0].read_text(encoding="utf-8").splitlines()
        ]


class DeviceGateTests(unittest.TestCase):
    """覆盖真实 read 在任何采集前检查共享设备门禁。"""

    def test_unavailable_gate_blocks_before_screen_call(self) -> None:
        """未交付门禁时不调用 dump、不启动子进程。"""
        gate = _Gate(unavailable=True)
        screen = _Screen()
        with patch("subprocess.run") as run:
            with self.assertRaises(DeviceGateUnavailable):
                read_from_device(
                    screen,
                    gate,
                    SERIAL,
                    lambda xml: parse_page(xml, _profile(), BASE_TIME, SNAPSHOT_ID),
                )
        self.assertEqual(gate.checks, 1)
        self.assertEqual(screen.dump_calls, 0)
        run.assert_not_called()

    def test_bad_parser_rejected_before_gate_or_screen(self) -> None:
        """不可调用 parser 在任何设备调用前拒绝，即使门禁可用。"""
        gate = _Gate(unavailable=False)
        screen = _Screen()
        with patch("subprocess.run") as run:
            with self.assertRaises(ValueError):
                read_from_device(screen, gate, SERIAL, "not-callable")
        self.assertEqual(gate.checks, 0)
        self.assertEqual(screen.dump_calls, 0)
        run.assert_not_called()

    def test_available_gate_uses_production_parser(self) -> None:
        """门禁通过后采集一次层级并交给生产解析函数。"""
        gate = _Gate(unavailable=False)
        screen = _Screen()
        result = read_from_device(
            screen,
            gate,
            SERIAL,
            lambda xml: parse_page(xml, _profile(), BASE_TIME, SNAPSHOT_ID),
        )
        self.assertEqual(gate.checks, 2)
        self.assertEqual(screen.dump_calls, 1)
        self.assertEqual(result.status, "ok")


def _profile() -> dict:
    """读取合成 M3 profile。"""
    import json

    path = Path(__file__).resolve().parent / "fixtures" / "read" / "profile.json"
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
