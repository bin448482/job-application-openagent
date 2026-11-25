"""验证 M3 ADB 采集适配器的参数、超时、不重试、证据保存与共享门禁。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.adb.command import run_adb
from app.adapters.adb.screen import ADB_TIMEOUT_SECONDS, AdbScreenReader
from app.adapters.boss.read import parse_page
from app.ports.device_gate import DeviceGateError
from app.ports.runtime import RuntimeCommandControlError
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
        reader = AdbScreenReader(
            self.root / "evidence",
            process_log=self.log,
            gate=_Gate(unavailable=False),
        )
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
        reader = AdbScreenReader(
            self.root / "evidence",
            process_log=self.log,
            gate=_Gate(unavailable=False),
        )
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
        reader = AdbScreenReader(
            self.root / "evidence",
            process_log=self.log,
            gate=_Gate(unavailable=False),
        )
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
        reader = AdbScreenReader(
            evidence_file,
            process_log=self.log,
            gate=_Gate(unavailable=False),
        )
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

    def test_guard_rejection_logs_started_and_single_terminal(self) -> None:
        """before guard 拒绝时记录固定失败终态并按原语义传播。"""
        class GuardError(Exception):
            """带固定原因码的 guard 拒绝替身。"""

            reason_code = "authorization_required"

        reader = AdbScreenReader(
            self.root / "evidence",
            process_log=self.log,
            gate=_Gate(unavailable=False),
        )

        def guard() -> None:
            """在任何子进程前拒绝。"""
            raise GuardError()

        with patch("subprocess.run") as run:
            with self.assertRaises(GuardError):
                reader.dump_hierarchy(SERIAL, before_command=guard)
        run.assert_not_called()
        events = [
            event
            for event in self._events()
            if event["stage"] == "adb_hierarchy"
            and event["command"] == "adb_hierarchy_dump"
        ]
        self.assertEqual([e["result"] for e in events], ["started", "failed"])
        self.assertEqual(events[-1]["reason_code"], "authorization_required")
        self.assertEqual(events[-1]["error_category"], "action_guard")

    def test_evidence_save_logs_started_and_single_terminal(self) -> None:
        """证据保存成功/失败都只记一次 started 和一个终态。"""
        reader = AdbScreenReader(
            self.root / "evidence",
            process_log=self.log,
            gate=_Gate(unavailable=False),
        )
        with patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, stdout="dumped"),
                subprocess.CompletedProcess([], 0, stdout=PAGE_XML),
            ],
        ):
            reader.dump_hierarchy(SERIAL)
        events = [
            event for event in self._events() if event["stage"] == "adb_evidence"
        ]
        self.assertEqual([e["result"] for e in events], ["started", "ok"])

        failing = AdbScreenReader(
            self.root / "evidence-file",
            process_log=ProcessLog(path=self.root / "logs" / "second.jsonl"),
            gate=_Gate(unavailable=False),
        )
        (self.root / "evidence-file").write_text("x", encoding="utf-8")
        with patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, stdout="dumped"),
                subprocess.CompletedProcess([], 0, stdout=PAGE_XML),
            ],
        ):
            with self.assertRaises(ScreenReadError):
                failing.dump_hierarchy(SERIAL)
        files = list((self.root / "logs").glob("second-*.jsonl"))
        self.assertEqual(len(files), 1)
        events = [
            __import__("json").loads(line)
            for line in files[0].read_text(encoding="utf-8").splitlines()
        ]
        evidence = [e for e in events if e["stage"] == "adb_evidence"]
        self.assertEqual([e["result"] for e in evidence], ["started", "failed"])

    def test_reader_without_gate_rejects_before_subprocess(self) -> None:
        """缺共享门禁时 M3 采集在零子进程处固定拒绝。"""
        reader = AdbScreenReader(self.root / "evidence")
        for call in (reader.dump_hierarchy, reader.capture_screen):
            with self.subTest(call=call.__name__):
                with patch("subprocess.run") as run:
                    with self.assertRaises(DeviceGateError) as caught:
                        call(SERIAL)
                self.assertEqual(caught.exception.reason_code, "device_not_held")
                run.assert_not_called()

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
        reader = AdbScreenReader(self.root / "evidence", gate=_Gate(unavailable=False))
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


class RunAdbSpawnCallbackTests(unittest.TestCase):
    """验证唯一 run_adb 的实际创建事件、严格回调传播与有界清理。"""

    class _FakePopen:
        """只模拟 Popen 生命周期，不创建真实进程。"""

        next_pid = 4242

        def __init__(self, *, returncode: int = 0, timeout: bool = False) -> None:
            """分配模拟 PID、返回码与超时行为。"""
            self.pid = RunAdbSpawnCallbackTests._FakePopen.next_pid
            RunAdbSpawnCallbackTests._FakePopen.next_pid += 1
            self.returncode = returncode
            self.killed = False
            self._timeout = timeout

        def communicate(self, timeout=None):
            """返回固定输出；首次按配置模拟超时。"""
            del timeout
            if self._timeout:
                self._timeout = False
                raise subprocess.TimeoutExpired(["adb"], 5)
            return ("ok", "")

        def kill(self) -> None:
            """记录有界清理调用。"""
            self.killed = True

    def test_spawn_pid_done_fire_once_on_created_process(self) -> None:
        """Popen 成功后 on_spawn/on_pid/on_done 各一次。"""
        fake = self._FakePopen()
        events: list[tuple[str, int | None]] = []
        with patch(
            "app.adapters.adb.command.subprocess.Popen", return_value=fake
        ):
            completed = run_adb(
                ["adb", "version"],
                gate=_Gate(False),
                on_spawn=lambda pid: events.append(("spawn", pid)),
                on_pid=lambda pid: events.append(("pid", pid)),
                on_done=lambda pid, bounded, proven: events.append(("done", pid)),
                bound_factory=lambda: None,
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            events,
            [("spawn", fake.pid), ("pid", fake.pid), ("done", fake.pid)],
        )

    def test_popen_missing_does_not_fire_spawn(self) -> None:
        """Popen 未创建时 on_spawn 为 0，仍保留固定 adb_missing。"""
        events: list[int] = []
        with patch(
            "app.adapters.adb.command.subprocess.Popen",
            side_effect=FileNotFoundError,
        ):
            with self.assertRaises(Exception) as caught:
                run_adb(
                    ["adb", "version"],
                    gate=_Gate(False),
                    on_spawn=events.append,
                    bound_factory=lambda: None,
                )
        self.assertEqual(events, [])
        self.assertEqual(caught.exception.category, "adb_missing")

    def test_strict_on_done_error_propagates_after_command(self) -> None:
        """严格 on_done 错误在命令结束后向上传播，不被静默吞掉。"""
        fake = self._FakePopen()

        def boom(pid: int, bounded: bool, proven: bool) -> None:
            """模拟续租控制失败。"""
            del pid, bounded, proven
            raise RuntimeError("strict callback")

        with patch(
            "app.adapters.adb.command.subprocess.Popen", return_value=fake
        ):
            with self.assertRaises(RuntimeCommandControlError) as caught:
                run_adb(
                    ["adb", "version"],
                    gate=_Gate(False),
                    on_spawn=lambda pid: None,
                    on_done=boom,
                    strict_callbacks=True,
                    bound_factory=lambda: None,
                )
        self.assertIsNone(caught.exception.command_category)

    def test_strict_callback_keeps_nonzero_and_timeout_category(self) -> None:
        """命令非零/超时与严格回调同时失败时，异常仍携带原命令固定类别。"""
        for category, fake in (
            ("adb_nonzero", self._FakePopen(returncode=1)),
            ("adb_timeout", self._FakePopen(timeout=True)),
        ):
            with self.subTest(category=category):
                def boom(pid: int, bounded: bool, proven: bool) -> None:
                    """模拟续租控制失败。"""
                    del pid, bounded, proven
                    raise RuntimeError("strict callback")

                with patch(
                    "app.adapters.adb.command.subprocess.Popen", return_value=fake
                ):
                    with self.assertRaises(RuntimeCommandControlError) as caught:
                        run_adb(
                            ["adb", "version"],
                            gate=_Gate(False),
                            on_spawn=lambda pid: None,
                            on_done=boom,
                            strict_callbacks=True,
                            bound_factory=lambda: None,
                        )
                self.assertEqual(caught.exception.command_category, category)

    def test_on_spawn_error_kills_created_process_and_propagates(self) -> None:
        """on_spawn 自身失败时先有界回收已创建进程再向上抛出。"""
        fake = self._FakePopen()

        def boom(pid: int) -> None:
            """模拟首次创建观测失败。"""
            del pid
            raise RuntimeError("spawn observer failed")

        with patch(
            "app.adapters.adb.command.subprocess.Popen", return_value=fake
        ):
            with self.assertRaises(RuntimeError):
                run_adb(
                    ["adb", "version"],
                    gate=_Gate(False),
                    on_spawn=boom,
                    bound_factory=lambda: None,
                )
        self.assertTrue(fake.killed)


if __name__ == "__main__":
    unittest.main()
