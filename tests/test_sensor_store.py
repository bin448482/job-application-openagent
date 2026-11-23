"""验证 M2 sense 的合成入库、重放幂等、停止原因、日志与安全错误出口。"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.adapters.adb.device import AdbDeviceProbe
from app.adapters.boss.list import load_and_parse_snapshot
from app.adapters.sqlite.repo import SqliteStore, default_local_host
from app.adapters.sqlite.schema import initialize_database
from app.domain.models import (
    Owner,
    canonical_hash,
    conversation_key_for,
    discovery_key_for,
    fingerprint_for,
    identity_hash_for,
)
from app.ports.store import StoreError
from app.process_log import ProcessLog
from app.cli.main import main

from tests.m1_helpers import (
    HOST_A,
    FakeClock,
    event_count,
    fetch_all,
    fetch_one,
    make_result,
)
from tests.test_sensor import (
    BASE_TIME,
    FIXTURE_DIR,
    base_profile,
    build_xml,
    make_fixture,
    positive_xml,
)

FIXTURE_RELATIVE = "tests/fixtures/sensor/positive.json"
DB_RELATIVE = "data/runtime/m2.sqlite3"
FORBIDDEN_STORES = ("claim", "start", "renew", "finish", "recover")


def _copy_fixture(root: Path) -> Path:
    """把传感器合成正例复制到临时项目根，保持 manifest 相对引用。"""
    target = root / "tests" / "fixtures" / "sensor"
    shutil.copytree(FIXTURE_DIR, target)
    return target / "positive.json"


class SenseStoreTests(unittest.TestCase):
    """覆盖 CLI 全链路、持久化重放、终态与仓储错误出口。"""

    def setUp(self) -> None:
        """创建临时项目根、日志目录并固定 CLI 项目根。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_root = self.root / "logs"
        self.log_paths: list[Path] = []
        self.patcher = patch("app.cli.main.PROJECT_ROOT", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.manifest = _copy_fixture(self.root)

    def _run(self, *args: str) -> tuple[int, str, str]:
        """在进程内运行 CLI，并断言 fixture 路径零设备/网络/任务租约调用。"""
        stdout = io.StringIO()
        stderr = io.StringIO()
        log = ProcessLog(path=self.log_root / "process.jsonl")
        self.log_paths.append(log.path)
        with patch(
            "subprocess.run", side_effect=AssertionError("subprocess is forbidden")
        ) as run_mock, patch(
            "socket.socket", side_effect=AssertionError("socket is forbidden")
        ) as socket_mock, patch(
            "socket.create_connection",
            side_effect=AssertionError("network is forbidden"),
        ) as connect_mock, patch.object(
            AdbDeviceProbe,
            "probe",
            side_effect=AssertionError("device probe is forbidden"),
        ) as probe_mock:
            store_mocks = [
                patch.object(
                    SqliteStore,
                    name,
                    side_effect=AssertionError(f"{name} is forbidden"),
                )
                for name in FORBIDDEN_STORES
            ]
            started = [mock.start() for mock in store_mocks]
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(list(args), process_log=log)
            finally:
                for mock in store_mocks:
                    mock.stop()
        run_mock.assert_not_called()
        socket_mock.assert_not_called()
        connect_mock.assert_not_called()
        probe_mock.assert_not_called()
        for mock in started:
            mock.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def _database(self) -> Path:
        """返回本次测试的默认业务库路径。"""
        return self.root / DB_RELATIVE

    def _all_events(self) -> list[dict[str, object]]:
        """读取本次测试所有独立运行日志。"""
        return [
            json.loads(line)
            for path in self.log_paths
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_positive_fixture_contract_and_replay_idempotence(self) -> None:
        """正例退出 0、唯一 unread 候选，重放发现仍为 1 且不追加事件。"""
        expected = json.loads(
            (FIXTURE_DIR / "expected.json").read_text(encoding="utf-8")
        )["positive.json"]
        code, stdout, stderr = self._run(
            "sense", "--fixture", FIXTURE_RELATIVE, "--db", DB_RELATIVE
        )
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        result = payload["result"]
        self.assertEqual(
            list(payload), ["schema_version", "result", "stop_reason"]
        )
        self.assertEqual(payload["stop_reason"], expected["stop_reason"])
        self.assertEqual(result["status"], expected["result_status"])
        self.assertEqual(
            result["value"]["unread_state"], expected["unread_state"]
        )
        self.assertEqual(
            [candidate["kind"] for candidate in result["value"]["candidates"]],
            expected["candidate_kinds"],
        )
        self.assertEqual(len(result["value"]["candidates"]), 1)
        self.assertTrue(result["snapshot_id"])

        path = self._database()
        discoveries = fetch_all(path, "SELECT * FROM hr_discoveries")
        self.assertEqual(len(discoveries), expected["discovery_count"])
        discovery = discoveries[0]
        self.assertEqual(discovery["state"], "queued")
        self.assertEqual(discovery["kind"], "unread")
        self.assertEqual(discovery["opened"], 0)
        self.assertIsNone(discovery["nonce"])
        conversation = fetch_one(path, "SELECT * FROM hr_conversations")
        self.assertIsNone(conversation["baseline_preview_hash"])
        self.assertIsNone(conversation["baseline_time_hint"])
        self.assertIsNone(conversation["verified_at"])

        expected_identity = identity_hash_for("合成联系人", "合成公司", "合成职位")
        expected_conversation = conversation_key_for(
            "boss", "合成联系人", "合成公司", "合成职位"
        )
        expected_preview = canonical_hash("合成预览正文")
        expected_fingerprint = fingerprint_for(expected_preview, "10:00", 1)
        self.assertEqual(conversation["identity_hash"], expected_identity)
        self.assertEqual(discovery["conversation_key"], expected_conversation)
        self.assertEqual(discovery["fingerprint"], expected_fingerprint)
        self.assertEqual(
            discovery["key"],
            discovery_key_for("boss", expected_conversation, expected_fingerprint),
        )

        events_before = event_count(path)
        code, stdout, stderr = self._run(
            "sense", "--fixture", FIXTURE_RELATIVE, "--db", DB_RELATIVE
        )
        self.assertEqual((code, stderr), (0, ""))
        replay = json.loads(stdout)["result"]
        self.assertEqual(replay["value"]["unread_state"], "present")
        self.assertEqual(len(replay["value"]["candidates"]), 1)
        self.assertEqual(event_count(path), events_before)
        self.assertEqual(len(fetch_all(path, "SELECT * FROM hr_discoveries")), 1)

    def test_sense_logs_share_correlation_and_store_operations(self) -> None:
        """同一 ProcessLog 关联 sense 解析与仓储 open/upsert/unresolved。"""
        code, stdout, stderr = self._run(
            "sense", "--fixture", FIXTURE_RELATIVE, "--db", DB_RELATIVE
        )
        self.assertEqual((code, stderr), (0, ""))
        events = self._all_events()
        self.assertEqual(len({event["correlation_id"] for event in events}), 1)
        stages = [event["stage"] for event in events]
        self.assertIn("sense", stages)
        store_operations = [
            (event["operation"], event["result"])
            for event in events
            if event["stage"] == "store"
        ]
        for operation in ("init", "open_store", "upsert_snapshot", "unresolved_conversations"):
            self.assertIn((operation, "started"), store_operations)
            self.assertIn((operation, "ok"), store_operations)
        log_text = "\n".join(
            path.read_text(encoding="utf-8") for path in self.log_paths
        )
        self.assertNotIn("tests/fixtures", log_text)
        self.assertNotIn("m2.sqlite3", log_text)
        self.assertNotIn("合成", log_text)
        self.assertNotIn("boss:conv:", log_text)
        self.assertNotIn(json.loads(stdout)["result"]["snapshot_id"], log_text)

    def test_terminal_same_fingerprint_is_not_reopened(self) -> None:
        """同指纹终态仍观察到正信号时只定向核对，不重开或新增入站。"""
        code, _, _ = self._run(
            "sense", "--fixture", FIXTURE_RELATIVE, "--db", DB_RELATIVE
        )
        self.assertEqual(code, 0)
        path = self._database()
        owner = Owner(str(uuid.uuid4()), 4242, default_local_host())
        result = load_and_parse_snapshot(
            self.manifest, "00000000-0000-4000-8000-000000000301"
        )
        store = SqliteStore(
            path, process_log=ProcessLog(path=self.log_root / "terminal.jsonl")
        )
        try:
            lease = store.claim("boss", "unread", owner, BASE_TIME)
            self.assertIsNotNone(lease)
            store.start(lease, BASE_TIME)
            store.finish(lease, BASE_TIME, "done", None, result)
        finally:
            store.close()
        before_events = event_count(path)
        before_rows = fetch_all(path, "SELECT * FROM hr_discoveries")

        code, stdout, stderr = self._run(
            "sense", "--fixture", FIXTURE_RELATIVE, "--db", DB_RELATIVE
        )
        self.assertEqual((code, stderr), (2, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["stop_reason"], "needs_targeted_verification")
        self.assertEqual(payload["result"]["status"], "ok")
        self.assertEqual(payload["result"]["value"]["unread_state"], "present")
        self.assertEqual(fetch_all(path, "SELECT * FROM hr_discoveries"), before_rows)
        self.assertEqual(event_count(path), before_events)

    def test_terminal_old_key_and_different_queued_new_key(self) -> None:
        """旧指纹终态保留，不同指纹新键可独立 queued 且不续租。"""
        path = self.root / "data/runtime/terminal-new.sqlite3"
        path.parent.mkdir(parents=True)
        clock = FakeClock(BASE_TIME)
        initialize_database(path)
        store = SqliteStore(
            path,
            local_host=HOST_A,
            monotonic=clock.monotonic,
            stop_checker=lambda pid: "stopped",
            process_log=ProcessLog(path=self.log_root / "store.jsonl"),
        )
        self.addCleanup(store.close)
        owner = Owner(str(uuid.uuid4()), 4242, HOST_A)
        old = make_result(
            observed_at=clock.now(),
            contact="合成甲",
            company="合成公司甲",
            position="合成职位甲",
            preview="旧预览",
            unread_count=1,
        )
        old_keys = store.upsert_snapshot(old)
        lease = store.claim("boss", "unread", owner, clock.now())
        self.assertIsNotNone(lease)
        store.start(lease, clock.now())
        store.finish(lease, clock.now(), "done", None, old)
        clock.advance(1)
        new = make_result(
            observed_at=clock.now(),
            contact="合成甲",
            company="合成公司甲",
            position="合成职位甲",
            preview="新预览",
            unread_count=1,
        )
        new_keys = store.upsert_snapshot(new)
        self.assertNotEqual(old_keys, new_keys)
        rows = fetch_all(path, "SELECT * FROM hr_discoveries ORDER BY updated_at")
        self.assertEqual([row["state"] for row in rows], ["done", "queued"])
        self.assertEqual(rows[0]["reason_code"], None)
        self.assertIsNone(rows[1]["nonce"])
        conversation = conversation_key_for(
            "boss", "合成甲", "合成公司甲", "合成职位甲"
        )
        self.assertEqual(
            store.unresolved_conversations("boss"), [conversation]
        )

    def test_stop_reasons_and_non_persistence_for_negative_cases(self) -> None:
        """全局正无行、全缺失、仅预览正与全零分别命中固定出口。"""
        cases = self._stop_reason_fixtures()
        for name, (manifest, expected_stop, expected_exit, expected_discoveries) in cases.items():
            with self.subTest(name=name):
                db = f"data/runtime/{name}.sqlite3"
                code, stdout, stderr = self._run(
                    "sense", "--fixture", str(manifest.relative_to(self.root)), "--db", db
                )
                self.assertEqual((code, stderr), (expected_exit, ""))
                payload = json.loads(stdout)
                self.assertEqual(payload["stop_reason"], expected_stop)
                self.assertEqual(payload["result"]["status"], "ok")
                path = self.root / db
                rows = fetch_all(path, "SELECT * FROM hr_discoveries")
                self.assertEqual(len(rows), expected_discoveries)

    def _stop_reason_fixtures(self) -> dict[str, tuple[Path, str | None, int, int]]:
        """构造四种停止原因合成夹具并返回期望出口。"""
        directory = self.root / "cases"
        profile = base_profile()

        global_positive = make_fixture(
            directory / "global-positive",
            "manifest",
            [
                build_xml(
                    [],
                    profile=profile,
                    global_labels=[
                        {"label": "新招呼", "count_suffix": "greeting_count", "text": "2"}
                    ],
                )
            ],
            profile=profile,
        )

        missing_sources = make_fixture(
            directory / "missing",
            "manifest",
            [
                build_xml(
                    [
                        {
                            "name": "甲",
                            "company": "公司甲",
                            "position": "职位甲",
                            "preview": "预览",
                        }
                    ]
                )
            ],
            profile=profile,
        )

        verified_profile = base_profile()
        verified_profile["new_greeting"]["semantics_verified"] = True
        verified_profile["tab_badge"]["global_zero_verified"] = True
        identity = identity_hash_for("甲", "公司甲", "职位甲")
        baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": canonical_hash("旧预览"),
                "time_hint": "10:00",
            }
        ]
        preview_only = make_fixture(
            directory / "preview-only",
            "manifest",
            [
                build_xml(
                    [
                        {
                            "name": "甲",
                            "company": "公司甲",
                            "position": "职位甲",
                            "preview": "新预览",
                            "time": "11:00",
                            "badges": [("0", "[900,40][960,90]")],
                        }
                    ],
                    profile=verified_profile,
                    global_labels=[
                        {"label": "新招呼", "count_suffix": "greeting_count", "text": "0"},
                        {"label": "消息", "count_suffix": "tab_count", "text": "0"},
                    ],
                )
            ],
            profile=verified_profile,
            baseline_rows=baseline,
            proof={"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
        )

        absent_baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": canonical_hash("相同预览"),
                "time_hint": "10:00",
            }
        ]
        absent = make_fixture(
            directory / "absent",
            "manifest",
            [
                build_xml(
                    [
                        {
                            "name": "甲",
                            "company": "公司甲",
                            "position": "职位甲",
                            "preview": "相同预览",
                            "time": "10:00",
                            "badges": [("0", "[900,40][960,90]")],
                        }
                    ],
                    profile=verified_profile,
                    global_labels=[
                        {"label": "新招呼", "count_suffix": "greeting_count", "text": "0"},
                        {"label": "消息", "count_suffix": "tab_count", "text": "0"},
                    ],
                )
            ],
            profile=verified_profile,
            baseline_rows=absent_baseline,
            proof={"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
        )
        return {
            "global-positive": (
                global_positive,
                "needs_targeted_verification",
                2,
                0,
            ),
            "missing": (missing_sources, "unread_unverified", 2, 0),
            "preview-only": (preview_only, None, 0, 1),
            "absent": (absent, None, 0, 0),
        }

    def test_invalid_fixture_and_uncalibrated_do_not_open_database(self) -> None:
        """坏 XML、未标定版本与非法 manifest 都不创建或改动业务库。"""
        directory = self.root / "failures"
        broken = make_fixture(
            directory / "broken",
            "manifest",
            ["<hierarchy>"],
        )
        mismatch = make_fixture(
            directory / "mismatch",
            "manifest",
            [positive_xml()],
            app_version="other-version",
        )
        unsafe = make_fixture(
            directory / "unsafe",
            "manifest",
            [positive_xml()],
        )
        unsafe_data = json.loads(unsafe.read_text(encoding="utf-8"))
        unsafe_data["xml_paths"] = ["../outside.xml"]
        unsafe.write_text(json.dumps(unsafe_data), encoding="utf-8")
        bad_baseline = make_fixture(
            directory / "bad-baseline",
            "manifest",
            [positive_xml()],
            baseline_rows=[
                {
                    "conversation_key": "boss:conv:"
                    + identity_hash_for("合成联系人", "合成公司", "合成职位"),
                    "preview_hash": "invalid-hash",
                    "time_hint": "10:00",
                }
            ],
        )
        huge_profile = base_profile()
        huge_profile["badge_region"] = [0, 0, 10**400, 1]
        huge_region = make_fixture(
            directory / "huge-region",
            "manifest",
            [positive_xml()],
            profile=huge_profile,
        )
        bad_unicode = make_fixture(
            directory / "bad-unicode",
            "manifest",
            [positive_xml()],
        )
        (directory / "bad-unicode" / "manifest-0.xml").write_bytes(
            b"\xff\xfe\x00\x00"
        )

        cases = {
            "broken": (broken, "failed", "invalid_xml", "invalid_xml"),
            "mismatch": (
                mismatch,
                "undetermined",
                "uncalibrated_profile",
                "uncalibrated_profile",
            ),
            "unsafe": (unsafe, "failed", "invalid_fixture", "invalid_fixture"),
            "bad-baseline": (
                bad_baseline,
                "failed",
                "invalid_fixture",
                "invalid_fixture",
            ),
            "huge-region": (
                huge_region,
                "failed",
                "invalid_fixture",
                "invalid_fixture",
            ),
            "bad-unicode": (
                bad_unicode,
                "failed",
                "invalid_xml",
                "invalid_xml",
            ),
        }
        for name, (manifest, status, reason, stop_reason) in cases.items():
            with self.subTest(name=name):
                db = f"data/runtime/fail-{name}.sqlite3"
                code, stdout, stderr = self._run(
                    "sense", "--fixture", str(manifest.relative_to(self.root)), "--db", db
                )
                self.assertEqual((code, stderr), (2, ""))
                payload = json.loads(stdout)
                self.assertEqual(payload["result"]["status"], status)
                self.assertEqual(payload["result"]["reason_code"], reason)
                self.assertEqual(payload["stop_reason"], stop_reason)
                self.assertIsNone(payload["result"]["value"])
                self.assertFalse((self.root / db).exists())

    def test_repository_errors_use_safe_failure_payload(self) -> None:
        """初始化或打开仓储失败不伪造 ReadResult，只输出固定安全错误。"""
        with patch(
            "app.cli.main.initialize_database",
            side_effect=StoreError("store_busy"),
        ):
            code, stdout, stderr = self._run(
                "sense", "--fixture", FIXTURE_RELATIVE, "--db", DB_RELATIVE
            )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(
            json.loads(stdout),
            {"schema_version": 1, "status": "failed", "reason_code": "store_busy"},
        )

        with patch(
            "app.cli.main.initialize_database", side_effect=TypeError("mock")
        ):
            code, stdout, stderr = self._run(
                "sense", "--fixture", FIXTURE_RELATIVE, "--db", DB_RELATIVE
            )
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(
            json.loads(stdout)["reason_code"], "store_io_error"
        )

    def test_sense_argument_shape_is_strict(self) -> None:
        """sense 必须 --fixture，拒绝设备、未知、重复与越界数据库参数。"""
        cases = (
            ("missing", ("sense",)),
            ("device", ("sense", "--fixture", FIXTURE_RELATIVE, "--device")),
            ("unknown", ("sense", "--fixture", FIXTURE_RELATIVE, "--force")),
            (
                "duplicate",
                ("sense", "--fixture", FIXTURE_RELATIVE, "--fixture", FIXTURE_RELATIVE),
            ),
            (
                "absolute-db",
                ("sense", "--fixture", FIXTURE_RELATIVE, "--db", str(self.root / "x.sqlite3")),
            ),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                code, stdout, stderr = self._run(*arguments)
                self.assertEqual((code, stderr), (2, ""))
                self.assertEqual(
                    json.loads(stdout),
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "reason_code": "invalid_input",
                    },
                )


if __name__ == "__main__":
    unittest.main()
