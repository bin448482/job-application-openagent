"""验证 M3 页面/职位/未读/会话读取、字段阶梯、像素与跨屏身份契约。"""

from __future__ import annotations

import copy
import io
import json
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.adapters.boss.list import parse_snapshot
from app.adapters.boss.pixel import (
    parse_pixel_row_badge,
    parse_screen_header,
    validate_calibration,
)
from app.adapters.boss.read import (
    load_and_read_fixture,
    merge_conversations,
    parse_conversation_page,
    parse_job_detail,
    parse_job_list,
    parse_page,
    parse_unread,
    validate_read_profile,
)
from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.schema import initialize_database
from app.cli.main import main
from app.domain.ladder import (
    FieldReading,
    resolve_field,
    validate_field_reading,
    validate_field_resolution,
)
from app.domain.models import (
    APP,
    ReadResult,
    UnreadSignal,
    UnreadSnapshot,
    canonical_hash,
    conversation_key_for,
    identity_hash_for,
    is_uuid,
    validate_read_result,
)
from app.domain.reading import (
    ChatMessage,
    Conversation,
    FieldEvidence,
    JobCard,
    JobList,
    PageSnapshot,
    UnreadRead,
    UnreadRowRead,
    conversation_inbound_state,
    job_identity_hash_for,
    job_key_from_identity,
    message_key_from_stable,
    message_stable_identity_hash,
    new_message_keys,
    normalize_job_title,
    validate_read_value,
    validate_unread_row,
)
from app.domain.scheduler import stop_reason_for
from app.process_log import ProcessLog
from app.ports.store import StoreError
from app.usecases.read import (
    ReadNotActionable,
    build_read_payload,
    is_actionable,
    require_actionable,
    targeted_verification_outcome,
)
from app.usecases.sense import build_sense_payload

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "read"
SENSOR_DIR = Path(__file__).resolve().parent / "fixtures" / "sensor"
BASE_TIME = "2026-11-24T00:00:00.000Z"
SNAPSHOT_ID = "00000000-0000-4000-8000-000000000301"
TARGETED_SNAPSHOT_ID = "00000000-0000-4000-8000-000000000302"


def base_profile() -> dict:
    """读取合成 M3 profile 深拷贝，避免测试间污染。"""
    return copy.deepcopy(
        json.loads((FIXTURE_DIR / "profile.json").read_text(encoding="utf-8"))
    )


def fixture_text(name: str) -> str:
    """读取合成 XML 文本。"""
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _rid(suffix: str) -> str:
    """生成合成资源 ID。"""
    return f"com.hpbr.bosszhipin:id/{suffix}"


def make_read_fixture(
    root: Path,
    name: str,
    kind: str,
    xml_texts: list[str],
    *,
    profile: dict | None = None,
    proof: dict | None = None,
    context: dict | None = None,
    pixel: tuple[dict, bytes] | None = None,
    app_version: str | None = None,
    observed_at: str = BASE_TIME,
) -> str:
    """在临时项目根写入 profile/XML/像素/manifest，返回相对 manifest 路径。"""
    directory = root / "fixtures" / name
    directory.mkdir(parents=True, exist_ok=True)
    active_profile = profile if profile is not None else base_profile()
    (directory / "profile.json").write_text(
        json.dumps(active_profile, ensure_ascii=False), encoding="utf-8"
    )
    paths: list[str] = []
    for index, text in enumerate(xml_texts):
        file_name = f"page-{index}.xml"
        (directory / file_name).write_text(text, encoding="utf-8")
        paths.append(file_name)
    pixel_ref = None
    if pixel is not None:
        calibration, raw = pixel
        (directory / "calibration.json").write_text(
            json.dumps(calibration, ensure_ascii=False), encoding="utf-8"
        )
        (directory / "screen.rgba").write_bytes(raw)
        pixel_ref = {"calibration_path": "calibration.json", "raw_path": "screen.rgba"}
    manifest = {
        "profile_path": "profile.json",
        "app_version": app_version or str(active_profile["version"]),
        "observed_at": observed_at,
        "kind": kind,
        "hierarchy_paths": paths,
        "coverage_proof": proof
        or {"kind": "viewport", "top_proven": False, "bottom_proven": False},
        "conversation_context": context,
        "pixel": pixel_ref,
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path.relative_to(root).as_posix()


def run_cli(root: Path, args: list[str], log_path: Path) -> tuple[int, dict, str]:
    """在进程内运行 CLI，返回退出码、JSON 与 stderr。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    log = ProcessLog(path=log_path)
    with patch("app.cli.main.PROJECT_ROOT", root):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args, process_log=log)
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def make_raw(width: int, height: int, red_pixels: list[tuple[int, int]]) -> bytes:
    """构造指定位置为红色的合成 RGBA 截图。"""
    pixels = bytearray(width * height * 4)
    for x, y in red_pixels:
        offset = (y * width + x) * 4
        pixels[offset] = 255
        pixels[offset + 3] = 255
    return struct.pack("<IIII", width, height, 1, 0) + bytes(pixels)


def calibration(
    *,
    resolution: list[int] | None = None,
    threshold: int = 1,
    absent_state_verified: bool = True,
    profile: dict | None = None,
) -> dict:
    """生成显式合成像素标定。"""
    active = profile if profile is not None else base_profile()
    return {
        "calibration_id": "synthetic-calibration",
        "app": "boss",
        "profile_id": str(active["profile_id"]),
        "version": str(active["version"]),
        "calibrated": True,
        "absent_state_verified": absent_state_verified,
        "resolution": resolution or [100, 100],
        "badge_region": [0.0, 0.0, 0.2, 1.0],
        "target_color": [255, 0, 0],
        "tolerance": 0,
        "threshold_pixels": threshold,
        "source": "synthetic",
        "calibrated_on": "2026-11-24",
    }


def unread_xml(badge: str | None, *, row_bounds: str = "[0,0][100,100]") -> str:
    """生成单行合成未读列表 XML，可选角标。"""
    badge_xml = (
        f'<node text="{badge}" class="android.widget.TextView" bounds="[1,1][19,99]" />'
        if badge is not None
        else ""
    )
    return (
        '<hierarchy rotation="0"><node class="android.widget.FrameLayout" '
        'package="com.hpbr.bosszhipin" bounds="[0,0][1080,2400]">'
        f'<node resource-id="{_rid("recyclerView")}" class="androidx.recyclerview.widget.RecyclerView" '
        'scrollable="true" bounds="[0,0][1080,2200]">'
        f'<node resource-id="{_rid("message_row")}" class="android.widget.LinearLayout" '
        f'bounds="{row_bounds}">'
        f'<node text="合成联系人甲" resource-id="{_rid("tv_name")}" class="android.widget.TextView" bounds="[22,10][90,30]" />'
        f'<node text="合成公司甲" resource-id="{_rid("tv_company")}" class="android.widget.TextView" bounds="[22,30][90,60]" />'
        f'<node text="合成职位甲" resource-id="{_rid("tv_position")}" class="android.widget.TextView" bounds="[22,60][90,90]" />'
        f"{badge_xml}</node></node></node></hierarchy>"
    )


def ladder_reading(
    *,
    state: str,
    source: str = "hierarchy",
    count: int | None = None,
    reason: str | None = None,
    layout: str | None = None,
) -> FieldReading:
    """构造单级字段结论，默认绑定同一合成上下文。"""
    return FieldReading(
        field="unread_badges",
        app=APP,
        profile_id="synthetic-boss-m3",
        profile_version="14.200-synthetic",
        snapshot_id=SNAPSHOT_ID,
        observed_at=BASE_TIME,
        layout_context=layout or canonical_hash(base_profile()["selectors"]),
        source=source,
        confidence="high" if source == "hierarchy" else "medium",
        state=state,
        count=count,
        reason_code=reason,
    )


class ReadFixtureCLITests(unittest.TestCase):
    """覆盖五个合成 read CLI fixture、门禁、零设备/库/子进程与参数边界。"""

    def setUp(self) -> None:
        """创建临时日志目录。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        """清理本次临时目录。"""
        self.temp.cleanup()

    def test_cli_fixtures_match_independent_expectations(self) -> None:
        """五个 fixture 均接生产解析并输出可决策结果，不写业务库。"""
        expected = json.loads((FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))
        project = Path(__file__).resolve().parents[1]
        for name in (
            "page.json",
            "job_list.json",
            "job_detail.json",
            "unread.json",
            "conversation.json",
        ):
            with self.subTest(name=name):
                with patch("subprocess.run") as run, patch(
                    "app.cli.main.initialize_database"
                ) as initialize:
                    code, payload, stderr = run_cli(
                        project,
                        ["read", "--fixture", f"tests/fixtures/read/{name}"],
                        self.root / f"{name}.jsonl",
                    )
                run.assert_not_called()
                initialize.assert_not_called()
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                result = payload["result"]
                item = expected[name]
                self.assertEqual(result["status"], item["status"])
                self.assertEqual(payload["actionable"], item["actionable"])
                self.assertEqual(payload["gate_reason"], item["gate_reason"])
                if "page" in item:
                    self.assertEqual(result["value"]["page"], item["page"])
                if "card_count" in item:
                    self.assertEqual(len(result["value"]["cards"]), item["card_count"])
                if "job_key_prefix" in item:
                    self.assertTrue(result["value"]["job_key"].startswith(item["job_key_prefix"]))
                if "unread_state" in item:
                    self.assertEqual(result["value"]["unread_state"], item["unread_state"])
                    self.assertEqual(result["value"]["rows"][0]["source"], item["row_source"])
                if "contact" in item:
                    value = result["value"]
                    self.assertEqual(value["contact"], item["contact"])
                    self.assertEqual(value["inbound_state"], item["inbound_state"])
                    self.assertEqual(
                        [message["kind"] for message in value["messages"]],
                        item["message_kinds"],
                    )
                    self.assertIn("avatar", value["messages"][0]["direction_evidence"])
        self.assertEqual(len(expected), 5)

    def test_cli_rejects_device_mode_and_missing_fixture(self) -> None:
        """read 不接受真实设备入口，缺 fixture 或未知选项均为 invalid_input。"""
        project = Path(__file__).resolve().parents[1]
        for args in (
            ["read", "--device", "--serial", "synthetic"],
            ["read"],
            ["read", "--fixture", "tests/fixtures/read/page.json", "--db", "data/runtime/x.sqlite3"],
        ):
            with self.subTest(args=args):
                code, payload, _ = run_cli(
                    project, args, self.root / "args.jsonl"
                )
                self.assertEqual(code, 2)
                self.assertEqual(payload["reason_code"], "invalid_input")

    def test_read_fixture_writes_only_read_log_and_no_subprocess(self) -> None:
        """read fixture 只写本次独立日志，零子进程、零 DB。"""
        project = Path(__file__).resolve().parents[1]
        log_path = self.root / "read.jsonl"
        with patch("subprocess.run") as run:
            code, payload, _ = run_cli(
                project,
                ["read", "--fixture", "tests/fixtures/read/page.json"],
                log_path,
            )
        run.assert_not_called()
        self.assertEqual(code, 0)
        files = list((self.root).glob("read-*.jsonl"))
        self.assertEqual(len(files), 1)
        events = [
            json.loads(line)
            for line in files[0].read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[0]["stage"], "start")
        read_events = [event for event in events if event["stage"] == "read"]
        for operation in ("load", "page", "gate"):
            states = [
                event["result"]
                for event in read_events
                if event["operation"] == operation
            ]
            self.assertIn("started", states)
            self.assertTrue(any(state != "started" for state in states))
        self.assertNotIn("tests/fixtures", files[0].read_text(encoding="utf-8"))

    def test_uncalibrated_result_logs_undetermined_not_ok(self) -> None:
        """F6：未标定结果日志区分 undetermined，不混成成功。"""
        profile = base_profile()
        profile["calibrated"] = False
        manifest = make_read_fixture(
            self.root, "uncalibrated", "page", [fixture_text("page.xml")], profile=profile
        )
        log_path = self.root / "uncalibrated.jsonl"
        code, payload, stderr = run_cli(
            self.root, ["read", "--fixture", manifest], log_path
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["result"]["status"], "undetermined")
        self.assertEqual(stderr, "")
        events = self._log_events(self.root, "uncalibrated-*.jsonl")
        load_states = [
            (event["result"], event["reason_code"])
            for event in events
            if event["stage"] == "read" and event["operation"] == "load"
        ]
        self.assertIn(("undetermined", "uncalibrated_profile"), load_states)
        self.assertNotIn(("ok", None), load_states)
        self.assertFalse(any(event["result"] == "ok" and event["stage"] == "read" and event["operation"] == "page" for event in events))

    def test_identity_ambiguous_is_logged_without_degradation(self) -> None:
        """F6：合法 identity_ambiguous 可追溯且不触日志降级。"""
        context = {
            "contact": "合成联系人乙",
            "company": "合成公司甲",
            "position": "合成职位甲",
            "source_snapshot_id": "00000000-0000-4000-8000-000000000201",
        }
        manifest = make_read_fixture(
            self.root,
            "identity",
            "conversation",
            [fixture_text("conversation-0.xml"), fixture_text("conversation-1.xml")],
            proof={"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            context=context,
        )
        log_path = self.root / "identity.jsonl"
        code, payload, stderr = run_cli(
            self.root, ["read", "--fixture", manifest], log_path
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["result"]["reason_code"], "identity_ambiguous")
        self.assertNotIn("m0_process_log_degraded", stderr)
        events = self._log_events(self.root, "identity-*.jsonl")
        reasons = [
            event["reason_code"]
            for event in events
            if event["reason_code"] is not None
        ]
        self.assertIn("identity_ambiguous", reasons)
        gate_states = [
            (event["result"], event["reason_code"])
            for event in events
            if event["stage"] == "read" and event["operation"] == "gate"
        ]
        self.assertIn(("failed", "not_ok"), gate_states)

    def test_later_page_duplicate_identity_survives_merge(self) -> None:
        """R1：后页 parser 清键后的身份歧义必须传播到真实 CLI/merge。"""
        page1 = fixture_text("conversation-1.xml").replace(
            'content-desc="msg-3"', 'content-desc="msg-2"'
        )
        manifest = make_read_fixture(
            self.root,
            "later-duplicate",
            "conversation",
            [fixture_text("conversation-0.xml"), page1],
            proof={"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            context={
                "contact": "合成联系人甲",
                "company": "合成公司甲",
                "position": "合成职位甲",
                "source_snapshot_id": "00000000-0000-4000-8000-000000000201",
            },
        )
        code, payload, _ = run_cli(
            self.root, ["read", "--fixture", manifest], self.root / "later.jsonl"
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["actionable"])
        value = payload["result"]["value"]
        self.assertEqual(value["inbound_state"], "unknown")
        self.assertIsNone(value["latest_inbound_key"])
        self.assertFalse(value["message_identity_verified"])

    def test_pixel_trace_requires_actual_execution(self) -> None:
        """R3：合法标定但 XML 错页时不得记录 pixel 开始/失败。"""
        wrong_page = '<hierarchy rotation="0"><node text="无列表" bounds="[0,0][10,10]" /></hierarchy>'
        manifest = make_read_fixture(
            self.root,
            "wrong-page",
            "unread",
            [wrong_page],
            pixel=(calibration(threshold=1), make_raw(100, 100, [])),
        )
        log_path = self.root / "wrong-page.jsonl"
        code, payload, _ = run_cli(
            self.root, ["read", "--fixture", manifest], log_path
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["result"]["reason_code"], "wrong_page")
        events = self._log_events(self.root, "wrong-page-*.jsonl")
        self.assertFalse(
            any(
                event["stage"] == "read" and event["operation"] == "pixel"
                for event in events
            )
        )

    def test_pixel_conflict_logged_as_field_undetermined(self) -> None:
        """F6：像素与层级冲突记 pixel undetermined/ambiguous_signal。"""
        manifest = make_read_fixture(
            self.root,
            "pixel-conflict",
            "unread",
            [unread_xml("1")],
            pixel=(calibration(threshold=1), make_raw(100, 100, [])),
        )
        log_path = self.root / "pixel-conflict.jsonl"
        code, payload, stderr = run_cli(
            self.root, ["read", "--fixture", manifest], log_path
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["actionable"])
        self.assertNotIn("m0_process_log_degraded", stderr)
        events = self._log_events(self.root, "pixel-conflict-*.jsonl")
        self.assertTrue(
            any(
                event["stage"] == "read"
                and event["operation"] == "pixel"
                and event["result"] == "undetermined"
                and event["reason_code"] == "ambiguous_signal"
                for event in events
            )
        )

    def _log_events(self, root: Path, pattern: str) -> list[dict]:
        """读取指定模式的本机过程日志事件。"""
        files = list(root.glob(pattern))
        self.assertEqual(len(files), 1)
        return [
            json.loads(line)
            for line in files[0].read_text(encoding="utf-8").splitlines()
        ]


class PageReadTests(unittest.TestCase):
    """覆盖页面分类、unknown 门禁、多列表歧义、返回重判与版本漂移。"""

    def test_chat_list_page_is_actionable(self) -> None:
        """聊天列表结构完整时页面可决策。"""
        result = parse_page(fixture_text("page.xml"), base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.page, "chat_list")
        self.assertTrue(is_actionable(result)[0])

    def test_unknown_page_is_ok_but_not_actionable(self) -> None:
        """可解析但无页面信号时结构 ok、字段 unknown、门禁拒绝。"""
        result = parse_page(
            '<hierarchy rotation="0"><node text="无信号" bounds="[0,0][10,10]" /></hierarchy>',
            base_profile(),
            BASE_TIME,
            SNAPSHOT_ID,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.page, "unknown")
        self.assertEqual(is_actionable(result), (False, "field_unknown"))
        with self.assertRaises(ReadNotActionable):
            require_actionable(result)

    def test_multiple_chat_lists_are_ambiguous(self) -> None:
        """多个候选列表不得按第一个猜测页面。"""
        profile = base_profile()
        xml = (
            f'<hierarchy rotation="0">'
            f'<node resource-id="{_rid("recyclerView")}" bounds="[0,0][100,100]" />'
            f'<node resource-id="{_rid("recyclerView")}" bounds="[0,100][100,200]" />'
            f"</hierarchy>"
        )
        result = parse_page(xml, profile, BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.page, "unknown")
        self.assertEqual(result.value.field_evidence[0].reason_code, "ambiguous_signal")

    def test_return_to_chat_page_is_not_assumed_list(self) -> None:
        """SC-R11：返回后仍为聊天页时重新分类，不假定已回列表。"""
        result = parse_page(
            fixture_text("conversation-1.xml"), base_profile(), BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.page, "chat_detail")
        self.assertNotEqual(result.value.page, "chat_list")

    def test_popup_has_priority(self) -> None:
        """弹窗标记优先于列表信号。"""
        xml = (
            '<hierarchy rotation="0">'
            '<node text="温馨提示" bounds="[0,0][100,100]" />'
            f'<node resource-id="{_rid("recyclerView")}" bounds="[0,0][100,100]" />'
            "</hierarchy>"
        )
        result = parse_page(xml, base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.value.page, "popup")

    def test_uncalibrated_profile_and_invalid_xml(self) -> None:
        """版本不符返回 uncalibrated_profile；坏 XML 返回 invalid_xml。"""
        profile = base_profile()
        profile["calibrated"] = False
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = make_read_fixture(
                root, "uncalibrated", "page", [fixture_text("page.xml")], profile=profile
            )
            result = load_and_read_fixture(root / manifest, SNAPSHOT_ID)
        self.assertEqual(result.status, "undetermined")
        self.assertEqual(result.reason_code, "uncalibrated_profile")
        broken = parse_page("<hierarchy><node></hierarchy>", base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(broken.status, "failed")
        self.assertEqual(broken.reason_code, "invalid_xml")


class JobReadTests(unittest.TestCase):
    """覆盖职位卡规范化/去重、详情正文完整性与错误边界。"""

    def test_job_list_cards_and_keys(self) -> None:
        """两张卡各自重算职位键，标题规范化只去首尾字符。"""
        result = parse_job_list(
            fixture_text("job_list.xml"), base_profile(), BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.value.cards), 2)
        for card in result.value.cards:
            self.assertEqual(card.title, normalize_job_title(card.title_raw))
            expected = job_identity_hash_for(card.title_raw, card.company_raw, card.salary_raw)
            self.assertEqual(card.identity_hash, expected)
            self.assertEqual(card.job_key, job_key_from_identity(expected))

    def test_duplicate_job_cards_lose_locatable_key(self) -> None:
        """同标题同公司同薪资重复卡保留摘要但不生成可定位键。"""
        xml = fixture_text("job_list.xml").replace("合成岗位乙", "合成岗位甲").replace(
            "合成公司乙", "合成公司甲"
        ).replace("15-20K", "20-30K")
        result = parse_job_list(xml, base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.status, "ok")
        self.assertTrue(all(card.job_key is None for card in result.value.cards))
        self.assertFalse(is_actionable(result)[0])

    def test_zero_cards_is_incomplete_not_empty_success(self) -> None:
        """无完整职位卡返回 incomplete_snapshot，不用空列表冒充成功。"""
        xml = '<hierarchy rotation="0"><node clickable="true" bounds="[0,0][10,10]" /></hierarchy>'
        result = parse_job_list(xml, base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.status, "undetermined")
        self.assertEqual(result.reason_code, "incomplete_snapshot")

    def test_job_detail_keeps_original_description_and_hash(self) -> None:
        """职位详情提供正文原文、摘要、长度与完整性，不只给摘要。"""
        result = parse_job_detail(
            fixture_text("job_detail.xml"), base_profile(), BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(result.status, "ok")
        value = result.value
        self.assertIn("合成职位正文", value.description)
        self.assertEqual(value.description_hash, canonical_hash(value.description))
        self.assertEqual(value.description_length, len(value.description))
        self.assertTrue(value.complete)
        self.assertEqual(value.missing_fields, ())

    def test_truncated_description_is_undetermined(self) -> None:
        """截断正文不得返回部分详情或交模型补全。"""
        xml = fixture_text("job_detail.xml").replace(
            "合成职位正文，包含职责与要求。", "合成职位正文，包含职责与要求…"
        )
        result = parse_job_detail(xml, base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.status, "undetermined")
        self.assertEqual(result.reason_code, "incomplete_snapshot")
        self.assertIsNone(result.value)

    def test_missing_chat_or_optional_fields(self) -> None:
        """缺沟通按钮为 wrong_page；缺招聘者/地点保留字段未知但不猜值。"""
        xml = fixture_text("job_detail.xml").replace('resource-id="com.hpbr.bosszhipin:id/btn_chat"', "")
        result = parse_job_detail(xml, base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "wrong_page")
        xml2 = fixture_text("job_detail.xml").replace(
            'resource-id="com.hpbr.bosszhipin:id/tv_recruiter"', 'resource-id="com.hpbr.bosszhipin:id/no_recruiter"'
        )
        result2 = parse_job_detail(xml2, base_profile(), BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result2.status, "ok")
        self.assertIn("recruiter", result2.value.missing_fields)
        self.assertIsNone(result2.value.recruiter)


class UnreadReadTests(unittest.TestCase):
    """覆盖未读行三态、身份收敛、像素后备与失败/未知边界。"""

    def test_hierarchy_badge_is_present_and_locatable(self) -> None:
        """层级区域角标明确为 1 时产生可定位行。"""
        result = parse_unread(
            fixture_text("unread.xml"),
            base_profile(),
            None,
            None,
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.unread_state, "present")
        row = result.value.rows[0]
        self.assertEqual(row.state, "present")
        self.assertEqual(row.count, 1)
        self.assertEqual(row.source, "hierarchy")
        self.assertTrue(row.conversation_key.startswith("boss:conv:"))

    def test_badge_outside_region_is_unknown_not_absent(self) -> None:
        """区域外数字不得当角标，也不得把未知降为 absent。"""
        xml = unread_xml("1").replace('bounds="[1,1][19,99]"', 'bounds="[50,1][80,99]"')
        result = parse_unread(
            xml,
            base_profile(),
            None,
            None,
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.rows[0].state, "unknown")
        self.assertEqual(result.value.unread_state, "unknown")
        self.assertFalse(is_actionable(result)[0])

    def test_explicit_zero_badge_is_absent(self) -> None:
        """F5：层级显式 0 与 M2 一致为 absent，不变成 failed。"""
        result = parse_unread(
            unread_xml("0"),
            base_profile(),
            None,
            None,
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        row = result.value.rows[0]
        self.assertEqual(row.state, "absent")
        self.assertEqual(row.count, 0)
        self.assertEqual(result.value.unread_state, "absent")

    def test_pixel_present_count_is_rejected(self) -> None:
        """F5：像素 present 不得携带精确计数。"""
        row = UnreadRowRead(
            identity_hash=None,
            conversation_key=None,
            state="present",
            count=7,
            source="pixel",
            confidence="medium",
            reason_code=None,
        )
        with self.assertRaises(ValueError):
            validate_unread_row(row)

    def test_badge_disappearance_in_viewport_is_not_absent(self) -> None:
        """SC-R08：角标消失/延迟刷新在仅视口时不得宣告无未读或关闭发现。"""
        result = parse_unread(
            unread_xml(None),
            base_profile(),
            None,
            None,
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.rows[0].state, "unknown")
        self.assertEqual(result.value.unread_state, "unknown")
        self.assertFalse(is_actionable(result)[0])

    def test_duplicate_identity_keeps_other_positive_target(self) -> None:
        """重复身份行不可定位，但不丢其它唯一正目标。"""
        row = (
            f'<node resource-id="{_rid("message_row")}" bounds="[0,0][100,100]">'
            f'<node text="合成联系人甲" resource-id="{_rid("tv_name")}" class="android.widget.TextView" bounds="[22,10][90,30]" />'
            f'<node text="合成公司甲" resource-id="{_rid("tv_company")}" class="android.widget.TextView" bounds="[22,30][90,60]" />'
            f'<node text="合成职位甲" resource-id="{_rid("tv_position")}" class="android.widget.TextView" bounds="[22,60][90,90]" />'
            f'<node text="1" class="android.widget.TextView" bounds="[1,1][19,99]" /></node>'
        )
        other = (
            f'<node resource-id="{_rid("message_row")}" bounds="[0,100][100,200]">'
            f'<node text="合成联系人乙" resource-id="{_rid("tv_name")}" class="android.widget.TextView" bounds="[22,110][90,130]" />'
            f'<node text="合成公司乙" resource-id="{_rid("tv_company")}" class="android.widget.TextView" bounds="[22,130][90,160]" />'
            f'<node text="合成职位乙" resource-id="{_rid("tv_position")}" class="android.widget.TextView" bounds="[22,160][90,190]" />'
            f'<node text="2" class="android.widget.TextView" bounds="[1,101][19,199]" /></node>'
        )
        xml = (
            f'<hierarchy rotation="0"><node resource-id="{_rid("recyclerView")}" bounds="[0,0][1080,2200]">'
            f"{row}{row}{other}</node></hierarchy>"
        )
        result = parse_unread(
            xml,
            base_profile(),
            None,
            None,
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.unread_state, "present")
        locatable = [row for row in result.value.rows if row.conversation_key is not None]
        self.assertEqual(len(locatable), 1)
        self.assertEqual(locatable[0].count, 2)
        self.assertTrue(is_actionable(result)[0])

    def test_pixel_fallback_present_without_exact_count(self) -> None:
        """层级角标缺失时，显式标定像素可证明 present 但不生成精确计数。"""
        raw = make_raw(100, 100, [(x, y) for y in range(100) for x in range(20)])
        calib = calibration(threshold=2000)
        result = parse_unread(
            unread_xml(None),
            base_profile(),
            calib,
            raw,
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        row = result.value.rows[0]
        self.assertEqual(row.state, "present")
        self.assertIsNone(row.count)
        self.assertEqual(row.source, "pixel")
        self.assertEqual(row.confidence, "medium")

    def test_read_fixture_loads_pixel_files_through_production_path(self) -> None:
        """完整 fixture 路径加载显式合成像素并接到生产阶梯。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = make_raw(100, 100, [(x, y) for y in range(100) for x in range(20)])
            manifest = make_read_fixture(
                root,
                "pixel",
                "unread",
                [unread_xml(None)],
                pixel=(calibration(threshold=2000), raw),
            )
            result = load_and_read_fixture(root / manifest, SNAPSHOT_ID)
        self.assertEqual(result.status, "ok")
        row = result.value.rows[0]
        self.assertEqual(row.state, "present")
        self.assertEqual(row.source, "pixel")
        self.assertIsNone(row.count)

    def test_pixel_threshold_boundary_and_absent_state(self) -> None:
        """达到阈值为 present，低于阈值仅在已标定空态时为 absent。"""
        red = [(x, y) for y in range(100) for x in range(20)]
        raw_exact = make_raw(100, 100, red[:10])
        exact = parse_pixel_row_badge(
            raw_exact,
            calibration(threshold=10),
            (0, 0, 100, 100),
            app=APP,
            profile_id="synthetic-boss-m3",
            profile_version="14.200-synthetic",
            snapshot_id=SNAPSHOT_ID,
            observed_at=BASE_TIME,
            layout_context=canonical_hash(base_profile()["selectors"]),
        )
        self.assertEqual(exact.state, "present")
        raw_below = make_raw(100, 100, red[:9])
        below = parse_pixel_row_badge(
            raw_below,
            calibration(threshold=10),
            (0, 0, 100, 100),
            app=APP,
            profile_id="synthetic-boss-m3",
            profile_version="14.200-synthetic",
            snapshot_id=SNAPSHOT_ID,
            observed_at=BASE_TIME,
            layout_context=canonical_hash(base_profile()["selectors"]),
        )
        self.assertEqual(below.state, "absent")
        unverified = parse_pixel_row_badge(
            raw_below,
            calibration(threshold=10, absent_state_verified=False),
            (0, 0, 100, 100),
            app=APP,
            profile_id="synthetic-boss-m3",
            profile_version="14.200-synthetic",
            snapshot_id=SNAPSHOT_ID,
            observed_at=BASE_TIME,
            layout_context=canonical_hash(base_profile()["selectors"]),
        )
        self.assertEqual(unverified.state, "unknown")
        self.assertEqual(unverified.reason_code, "uncalibrated_region")

    def test_pixel_resolution_mismatch_and_corrupt_input(self) -> None:
        """分辨率不符为 calibration_mismatch；损坏/未知格式为 failed，不静默 absent。"""
        raw = make_raw(100, 100, [(0, 0)])
        mismatch = parse_pixel_row_badge(
            raw,
            calibration(resolution=[101, 100]),
            (0, 0, 100, 100),
            app=APP,
            profile_id="synthetic-boss-m3",
            profile_version="14.200-synthetic",
            snapshot_id=SNAPSHOT_ID,
            observed_at=BASE_TIME,
            layout_context=canonical_hash(base_profile()["selectors"]),
        )
        self.assertEqual(mismatch.state, "unknown")
        self.assertEqual(mismatch.reason_code, "calibration_mismatch")
        broken = parse_pixel_row_badge(
            b"not-a-screen",
            calibration(),
            (0, 0, 100, 100),
            app=APP,
            profile_id="synthetic-boss-m3",
            profile_version="14.200-synthetic",
            snapshot_id=SNAPSHOT_ID,
            observed_at=BASE_TIME,
            layout_context=canonical_hash(base_profile()["selectors"]),
        )
        self.assertEqual(broken.state, "failed")
        unknown_format = struct.pack("<IIII", 100, 100, 2, 0) + bytes(100 * 100 * 4)
        unsupported = parse_pixel_row_badge(
            unknown_format,
            calibration(),
            (0, 0, 100, 100),
            app=APP,
            profile_id="synthetic-boss-m3",
            profile_version="14.200-synthetic",
            snapshot_id=SNAPSHOT_ID,
            observed_at=BASE_TIME,
            layout_context=canonical_hash(base_profile()["selectors"]),
        )
        self.assertEqual(unsupported.state, "failed")

    def test_corrupt_pixel_fails_whole_read_not_unknown(self) -> None:
        """像素失败不降级为 unknown/absent，读取整体 failed。"""
        result = parse_unread(
            unread_xml(None),
            base_profile(),
            calibration(),
            b"bad",
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "invalid_fixture")

    def test_invalid_calibration_is_rejected_before_pixel_trace(self) -> None:
        """窄收尾：不支持 source 在像素开始前拒绝，零 pixel 事件。"""
        bad = calibration()
        bad["source"] = "unsupported-synthetic"
        with tempfile.TemporaryDirectory() as temp:
            log = ProcessLog(path=Path(temp) / "process.jsonl")
            result = parse_unread(
                unread_xml(None),
                base_profile(),
                bad,
                make_raw(100, 100, []),
                BASE_TIME,
                SNAPSHOT_ID,
                {"kind": "viewport", "top_proven": False, "bottom_proven": False},
                BASE_TIME,
                process_log=log,
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason_code, "invalid_fixture")
            events = self._read_log(log)
        self.assertFalse(
            any(
                event["stage"] == "read" and event["operation"] == "pixel"
                for event in events
            )
        )
        self.assertFalse(log.degraded)

    def test_corrupt_pixel_logs_started_to_failed(self) -> None:
        """窄收尾：实际像素失败保留 started→failed 与固定原因/耗时。"""
        with tempfile.TemporaryDirectory() as temp:
            log = ProcessLog(path=Path(temp) / "process.jsonl")
            result = parse_unread(
                unread_xml(None),
                base_profile(),
                calibration(),
                b"bad",
                BASE_TIME,
                SNAPSHOT_ID,
                {"kind": "viewport", "top_proven": False, "bottom_proven": False},
                BASE_TIME,
                process_log=log,
            )
            events = self._read_log(log)
        self.assertEqual(result.status, "failed")
        pixel_events = [
            event
            for event in events
            if event["stage"] == "read" and event["operation"] == "pixel"
        ]
        self.assertEqual([event["result"] for event in pixel_events], ["started", "failed"])
        self.assertEqual(pixel_events[-1]["reason_code"], "invalid_fixture")
        self.assertGreaterEqual(pixel_events[-1]["duration_ms"], 0)
        self.assertEqual(len({event["correlation_id"] for event in events}), 1)

    @staticmethod
    def _read_log(log: ProcessLog) -> list[dict]:
        """读取本次日志器实际创建的唯一文件事件；无可读事件返回空。"""
        if not log.path.exists():
            return []
        return [
            json.loads(line)
            for line in log.path.read_text(encoding="utf-8").splitlines()
        ]

    def test_hierarchy_pixel_conflict_becomes_unknown(self) -> None:
        """层级 present 与像素 absent 冲突时转 undetermined/人工，不覆盖前级。"""
        result = parse_unread(
            unread_xml("1"),
            base_profile(),
            calibration(threshold=1),
            make_raw(100, 100, []),
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        row = result.value.rows[0]
        self.assertEqual(row.state, "unknown")
        self.assertEqual(row.source, "none")
        self.assertFalse(is_actionable(result)[0])


class LadderTests(unittest.TestCase):
    """覆盖字段阶梯前级优先、冲突、failed 保留、视觉默认关闭与人工出口。"""

    def test_hierarchy_confirmed_is_not_overridden(self) -> None:
        """层级 present 与像素 present 一致时采用层级高置信。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="present", count=1),
            pixel=ladder_reading(state="present", source="pixel"),
        )
        self.assertEqual(resolution.state, "present")
        self.assertEqual(resolution.source, "hierarchy")
        self.assertEqual(resolution.confidence, "high")
        self.assertFalse(resolution.requires_human)

    def test_hierarchy_pixel_conflict_requires_human(self) -> None:
        """跨级不同结论必须 undetermined，不能高置信覆盖结构证据。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="present", count=1),
            pixel=ladder_reading(state="absent", source="pixel", count=0),
        )
        self.assertEqual(resolution.state, "undetermined")
        self.assertTrue(resolution.requires_human)
        self.assertEqual(resolution.reason_code, "ambiguous_signal")

    def test_unknown_hierarchy_uses_pixel_medium(self) -> None:
        """层级未知时像素可补位，但置信度为 medium 且计数未知。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="unknown", reason="missing_signal"),
            pixel=ladder_reading(state="present", source="pixel"),
        )
        self.assertEqual(resolution.state, "present")
        self.assertEqual(resolution.source, "pixel")
        self.assertEqual(resolution.confidence, "medium")
        self.assertIsNone(resolution.count)

    def test_failed_is_not_masked_by_later_success(self) -> None:
        """层级 failed 后即使像素成功也保留失败原因。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="failed", reason="invalid_fixture"),
            pixel=ladder_reading(state="present", source="pixel"),
        )
        self.assertEqual(resolution.state, "failed")
        self.assertEqual(resolution.reason_code, "invalid_fixture")
        self.assertFalse(resolution.requires_human)
        validate_field_resolution(resolution)

    def test_pixel_failed_after_unknown_is_failed(self) -> None:
        """层级未知后像素 failed 仍为 failed，不降为 unknown 或 absent。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="unknown", reason="missing_signal"),
            pixel=ladder_reading(state="failed", source="pixel", reason="invalid_fixture"),
        )
        self.assertEqual(resolution.state, "failed")
        self.assertEqual(resolution.reason_code, "invalid_fixture")

    def test_lower_level_failure_does_not_override_confirmed_hierarchy(self) -> None:
        """前级已确认时，后级像素失败不覆盖该字段，也不产生冲突。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="present", count=1),
            pixel=ladder_reading(state="failed", source="pixel", reason="invalid_fixture"),
            human=ladder_reading(state="present", source="human", count=1),
        )
        self.assertEqual(resolution.state, "present")
        self.assertEqual(resolution.source, "hierarchy")
        self.assertEqual(resolution.confidence, "high")

    def test_human_cannot_mask_pixel_failure_after_unknown(self) -> None:
        """前级像素 failed 不被后级人工成功掩盖。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="unknown", reason="missing_signal"),
            pixel=ladder_reading(state="failed", source="pixel", reason="invalid_fixture"),
            human=ladder_reading(state="present", source="human", count=1),
        )
        self.assertEqual(resolution.state, "failed")
        self.assertEqual(resolution.reason_code, "invalid_fixture")

    def test_vision_default_off_and_no_cross_evidence(self) -> None:
        """视觉默认关闭；启用但无独立交叉证据仍转人工，不放行。"""
        vision = ladder_reading(state="present", source="vision")
        disabled = resolve_field(
            hierarchy=ladder_reading(state="unknown", reason="missing_signal"),
            vision=vision,
        )
        self.assertEqual(disabled.state, "undetermined")
        self.assertTrue(disabled.requires_human)
        enabled = resolve_field(
            hierarchy=ladder_reading(state="unknown", reason="missing_signal"),
            vision=vision,
            vision_enabled=True,
            independent_cross_evidence=False,
        )
        self.assertEqual(enabled.state, "undetermined")
        self.assertTrue(enabled.requires_human)

    def test_explicit_human_input_is_the_only_human_confirmation(self) -> None:
        """只有显式人工结论可作为 human 出口。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="unknown", reason="missing_signal"),
            pixel=ladder_reading(state="unknown", source="pixel", reason="missing_signal"),
            human=ladder_reading(state="present", source="human", count=1),
        )
        self.assertEqual(resolution.state, "present")
        self.assertEqual(resolution.source, "human")
        self.assertEqual(resolution.confidence, "high")

    def test_context_mismatch_is_undetermined(self) -> None:
        """不同版式/快照上下文的结果不得拼成 ok。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="present", count=1),
            pixel=ladder_reading(
                state="present",
                source="pixel",
                layout=canonical_hash({"different": True}),
            ),
        )
        self.assertEqual(resolution.state, "undetermined")
        self.assertTrue(resolution.requires_human)

    def test_field_reading_rejects_pixel_count(self) -> None:
        """像素单级结论不得携带精确计数。"""
        with self.assertRaises(ValueError):
            validate_field_reading(
                ladder_reading(state="present", source="pixel", count=1)
            )

    def test_source_must_match_call_slot(self) -> None:
        """伪装来源或把同源当独立证据必须拒绝，不能绕过视觉默认关闭。"""
        vision_as_hierarchy = ladder_reading(state="present", source="vision")
        with self.assertRaises(ValueError):
            resolve_field(hierarchy=vision_as_hierarchy)
        pixel_as_hierarchy = ladder_reading(state="present", source="pixel")
        pixel_as_pixel = ladder_reading(state="present", source="pixel")
        with self.assertRaises(ValueError):
            resolve_field(hierarchy=pixel_as_hierarchy, pixel=pixel_as_pixel)
        hierarchy_as_pixel = ladder_reading(state="present", count=1)
        with self.assertRaises(ValueError):
            resolve_field(hierarchy=hierarchy_as_pixel, pixel=hierarchy_as_pixel)

    def test_failed_survives_later_context_conflict(self) -> None:
        """后级不同上下文的成功/冲突不能改写前级明确 failed。"""
        failed = ladder_reading(state="failed", reason="invalid_xml")
        unrelated = ladder_reading(
            state="present",
            source="pixel",
            layout=canonical_hash({"different": True}),
        )
        resolution = resolve_field(hierarchy=failed, pixel=unrelated)
        self.assertEqual(resolution.state, "failed")
        self.assertEqual(resolution.reason_code, "invalid_xml")
        validate_field_resolution(resolution)
        self.assertEqual(
            {reading.layout_context for reading in resolution.evidence},
            {failed.layout_context},
        )

    def test_mixed_context_undetermined_is_self_validating(self) -> None:
        """跨上下文未决/人工出口只携带同上下文证据，自身可通过验证器。"""
        hierarchy = ladder_reading(state="present", count=1)
        pixel = ladder_reading(
            state="present",
            source="pixel",
            layout=canonical_hash({"different": True}),
        )
        resolution = resolve_field(hierarchy=hierarchy, pixel=pixel)
        self.assertEqual(resolution.state, "undetermined")
        self.assertTrue(resolution.requires_human)
        validate_field_resolution(resolution)
        self.assertEqual(
            {reading.layout_context for reading in resolution.evidence},
            {hierarchy.layout_context},
        )

    def test_bad_types_raise_value_error_not_type_error(self) -> None:
        """列表来源、坏置信度/状态/原因/flag 与 bool 计数统一 ValueError。"""
        base = ladder_reading(state="present", count=1)
        bad_sources = replace(base, source=[])
        bad_confidence = replace(base, confidence=[])
        bad_state = replace(base, state=[])
        bad_reason = replace(
            ladder_reading(state="unknown", reason="missing_signal"),
            reason_code=[],
        )
        bad_count = replace(base, count=True)
        for reading in (bad_sources, bad_confidence, bad_state, bad_reason, bad_count):
            with self.subTest(reading=reading):
                with self.assertRaises(ValueError):
                    resolve_field(hierarchy=reading)
        with self.assertRaises(ValueError):
            resolve_field(hierarchy=base, vision_enabled=1)
        with self.assertRaises(ValueError):
            resolve_field(hierarchy=base, independent_cross_evidence=1)

    def test_pixel_only_keeps_pixel_source_and_confidence(self) -> None:
        """仅像素确认不得冒充 hierarchy/high。"""
        pixel = ladder_reading(state="present", source="pixel")
        resolution = resolve_field(pixel=pixel)
        self.assertEqual(resolution.state, "present")
        self.assertEqual(resolution.source, "pixel")
        self.assertEqual(resolution.confidence, "medium")
        self.assertIsNone(resolution.count)
        validate_field_resolution(resolution)

    def test_human_only_keeps_human_source(self) -> None:
        """仅显式人工确认使用 human/high。"""
        human = ladder_reading(state="present", source="human", count=1)
        resolution = resolve_field(human=human)
        self.assertEqual(resolution.state, "present")
        self.assertEqual(resolution.source, "human")
        self.assertEqual(resolution.confidence, "high")
        validate_field_resolution(resolution)

    def test_human_conflict_with_pixel_is_undetermined(self) -> None:
        """后级人工不同值不得覆盖像素已确认值。"""
        resolution = resolve_field(
            pixel=ladder_reading(state="present", source="pixel"),
            human=ladder_reading(state="absent", source="human", count=0),
        )
        self.assertEqual(resolution.state, "undetermined")
        self.assertTrue(resolution.requires_human)
        validate_field_resolution(resolution)

    def test_resolution_tampering_is_rejected(self) -> None:
        """结论状态/计数必须与证据联动，不能只改派生值。"""
        resolution = resolve_field(
            hierarchy=ladder_reading(state="present", count=1)
        )
        validate_field_resolution(resolution)
        with self.assertRaises(ValueError):
            validate_field_resolution(
                replace(
                    resolution,
                    state="absent",
                    count=0,
                    source="hierarchy",
                    confidence="high",
                )
            )
        with self.assertRaises(ValueError):
            validate_field_resolution(
                replace(
                    resolution,
                    state="present",
                    count=2,
                    source="hierarchy",
                    confidence="high",
                )
            )

    def test_conflict_cannot_be_forged_resolved(self) -> None:
        """R2：保留冲突证据时不能把 undetermined 伪造成层级 present。"""
        hierarchy = ladder_reading(state="present", count=1)
        pixel = ladder_reading(state="absent", source="pixel", count=0)
        actual = resolve_field(hierarchy=hierarchy, pixel=pixel)
        self.assertEqual(actual.state, "undetermined")
        with self.assertRaises(ValueError):
            validate_field_resolution(
                replace(
                    actual,
                    state="present",
                    count=1,
                    reason_code=None,
                    source="hierarchy",
                    confidence="high",
                    requires_human=False,
                )
            )

    def test_failure_cannot_be_forged_resolved(self) -> None:
        """R2：保留 failed evidence 时不能把 failed 伪造成 pixel present。"""
        hierarchy = ladder_reading(state="failed", reason="invalid_xml")
        pixel = ladder_reading(state="present", source="pixel")
        actual = resolve_field(hierarchy=hierarchy, pixel=pixel)
        self.assertEqual(actual.state, "failed")
        with self.assertRaises(ValueError):
            validate_field_resolution(
                replace(
                    actual,
                    state="present",
                    count=None,
                    reason_code=None,
                    source="pixel",
                    confidence="medium",
                    requires_human=False,
                )
            )


class ConversationReadTests(unittest.TestCase):
    """覆盖跨屏合并、稳定身份、方向、读全与入站门禁。"""

    def _pages(
        self,
        profile: dict | None = None,
        context: dict | None = None,
        observed_at: str = BASE_TIME,
    ) -> list[ReadResult]:
        """解析合成两页会话。"""
        active = profile if profile is not None else base_profile()
        context = context or {
            "contact": "合成联系人甲",
            "company": "合成公司甲",
            "position": "合成职位甲",
            "source_snapshot_id": "00000000-0000-4000-8000-000000000201",
        }
        return [
            parse_conversation_page(
                fixture_text("conversation-0.xml"), active, context, observed_at, SNAPSHOT_ID
            ),
            parse_conversation_page(
                fixture_text("conversation-1.xml"), active, context, observed_at, SNAPSHOT_ID
            ),
        ]

    def test_positive_merge_produces_known_inbound_key(self) -> None:
        """两页只裁剪稳定身份重叠，生成唯一可发送入站键。"""
        result = merge_conversations(
            self._pages(),
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        value = result.value
        self.assertTrue(value.read_complete)
        self.assertEqual([m.kind for m in value.messages], ["text", "text", "text", "system_tip"])
        self.assertEqual(value.inbound_state, "known")
        latest = value.messages[2]
        self.assertEqual(value.latest_inbound_key, latest.message_key)
        expected = message_key_from_stable(
            value.conversation_key, message_stable_identity_hash("msg-3")
        )
        self.assertEqual(value.latest_inbound_key, expected)
        self.assertTrue(is_actionable(result)[0])

    def test_incomplete_conversation_is_undetermined(self) -> None:
        """SC-R06：打开后未读全时 value=null，不生成入站身份或关闭发现。"""
        page = parse_conversation_page(
            fixture_text("conversation-0.xml"),
            base_profile(),
            {
                "contact": "合成联系人甲",
                "company": "合成公司甲",
                "position": "合成职位甲",
                "source_snapshot_id": "00000000-0000-4000-8000-000000000201",
            },
            BASE_TIME,
            SNAPSHOT_ID,
        )
        self.assertEqual(page.status, "ok")
        self.assertFalse(page.value.read_complete)
        result = merge_conversations(
            [page],
            base_profile(),
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(result.status, "undetermined")
        self.assertEqual(result.reason_code, "incomplete_snapshot")
        self.assertIsNone(result.value)

    def test_contact_mismatch_is_identity_ambiguous(self) -> None:
        """实际标题与上下文不一致时 undetermined，不虚构公司/职位。"""
        result = parse_conversation_page(
            fixture_text("conversation-0.xml"),
            base_profile(),
            {
                "contact": "合成联系人乙",
                "company": "合成公司甲",
                "position": "合成职位甲",
                "source_snapshot_id": "00000000-0000-4000-8000-000000000201",
            },
            BASE_TIME,
            SNAPSHOT_ID,
        )
        self.assertEqual(result.status, "undetermined")
        self.assertEqual(result.reason_code, "identity_ambiguous")

    def test_direction_conflict_makes_inbound_unknown(self) -> None:
        """SC-R10：头像与气泡方向冲突时消息方向 unknown，无可发送键。"""
        page0 = fixture_text("conversation-0.xml").replace(
            'bounds="[140,120][700,220]"', 'bounds="[600,120][980,220]"'
        )
        page1 = fixture_text("conversation-1.xml")
        pages = [
            parse_conversation_page(page0, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(page1, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        result = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.inbound_state, "unknown")
        self.assertIsNone(result.value.latest_inbound_key)
        self.assertFalse(is_actionable(result)[0])

    def test_truncated_latest_inbound_is_unknown(self) -> None:
        """截断正文不得生成可发送入站身份。"""
        page1 = fixture_text("conversation-1.xml").replace(
            'text="合成消息丙" resource-id="com.hpbr.bosszhipin:id/tv_content_text"',
            'text="合成消息丙" truncated="true" resource-id="com.hpbr.bosszhipin:id/tv_content_text"',
        )
        pages = [
            parse_conversation_page(fixture_text("conversation-0.xml"), base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(page1, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        result = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.value.inbound_state, "unknown")
        self.assertIsNone(result.value.latest_inbound_key)

    def test_duplicate_stable_id_clears_sendable_key(self) -> None:
        """同稳定 ID 重复时全部清空，不猜哪条是同一消息。"""
        page1 = fixture_text("conversation-1.xml").replace('content-desc="msg-3"', 'content-desc="msg-1"')
        pages = [
            parse_conversation_page(fixture_text("conversation-0.xml"), base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(page1, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        result = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.value.inbound_state, "unknown")
        self.assertIsNone(result.value.latest_inbound_key)
        self.assertTrue(
            all(
                message.message_key is None
                for message in result.value.messages
                if message.text in {"合成消息甲", "合成消息丙"}
            )
        )

    def test_earlier_duplicate_stable_identity_makes_conversation_unknown(self) -> None:
        """F1：较早重复稳定身份不能被最后一条唯一入站覆盖。"""
        page0 = fixture_text("conversation-0.xml").replace(
            'content-desc="msg-1"', 'content-desc="msg-2"'
        )
        pages = [
            parse_conversation_page(page0, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(fixture_text("conversation-1.xml"), base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        result = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.inbound_state, "unknown")
        self.assertIsNone(result.value.latest_inbound_key)
        self.assertFalse(result.value.message_identity_verified)
        self.assertFalse(is_actionable(result)[0])

    def test_duplicate_content_controls_make_latest_row_unknown(self) -> None:
        """F2：同正文控件多候选不得降级为 time/system 或 none。"""
        text_node = (
            '<node text="合成消息丙" resource-id="com.hpbr.bosszhipin:id/tv_content_text" '
            'class="android.widget.TextView" bounds="[160,630][680,710]" />'
        )
        duplicate = text_node.replace("合成消息丙", "合成消息丙二")
        page1 = fixture_text("conversation-1.xml").replace(text_node, text_node + duplicate)
        pages = [
            parse_conversation_page(fixture_text("conversation-0.xml"), base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(page1, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        result = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.inbound_state, "unknown")
        self.assertIsNone(result.value.latest_inbound_key)
        self.assertEqual(result.value.messages[-2].kind, "unknown")

    def test_duplicate_direction_controls_make_row_unknown(self) -> None:
        """F2：多头像/方向控件冲突不得被单一气泡证据掩盖。"""
        avatar = (
            '<node resource-id="com.hpbr.bosszhipin:id/iv_avatar" '
            'class="android.widget.ImageView" bounds="[20,620][120,720]" />'
        )
        duplicate = avatar.replace("[20,620][120,720]", "[880,620][980,720]")
        page1 = fixture_text("conversation-1.xml").replace(avatar, avatar + duplicate)
        pages = [
            parse_conversation_page(fixture_text("conversation-0.xml"), base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(page1, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        result = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.inbound_state, "unknown")
        self.assertIsNone(result.value.latest_inbound_key)

    def test_new_message_keys_rejects_cross_conversation(self) -> None:
        """新消息比较必须先核对同 app/profile/conversation。"""
        previous = merge_conversations(
            self._pages(),
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        other_context = {
            "contact": "合成联系人乙",
            "company": "合成公司甲",
            "position": "合成职位甲",
            "source_snapshot_id": "00000000-0000-4000-8000-000000000202",
        }
        page0 = fixture_text("conversation-0.xml").replace("合成联系人甲", "合成联系人乙")
        page1 = fixture_text("conversation-1.xml").replace("合成联系人甲", "合成联系人乙")
        other_pages = [
            parse_conversation_page(page0, base_profile(), other_context, BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(page1, base_profile(), other_context, BASE_TIME, SNAPSHOT_ID),
        ]
        other = merge_conversations(
            other_pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(other.status, "ok")
        with self.assertRaises(ValueError):
            new_message_keys(previous.value, other.value)

    def test_public_parsers_reject_uncalibrated_and_bad_context(self) -> None:
        """F3：公开解析入口自身执行标定/上下文校验，不依赖 fixture 包装。"""
        profile = base_profile()
        profile["calibrated"] = False
        page = parse_page(fixture_text("page.xml"), profile, BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(page.status, "undetermined")
        self.assertEqual(page.reason_code, "uncalibrated_profile")
        version = parse_page(
            fixture_text("page.xml"),
            base_profile(),
            BASE_TIME,
            SNAPSHOT_ID,
            app_version="other-version",
        )
        self.assertEqual(version.reason_code, "uncalibrated_profile")
        job = parse_job_list(fixture_text("job_list.xml"), profile, BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(job.reason_code, "uncalibrated_profile")
        detail = parse_job_detail(fixture_text("job_detail.xml"), profile, BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(detail.reason_code, "uncalibrated_profile")
        unread = parse_unread(
            fixture_text("unread.xml"),
            profile,
            None,
            None,
            BASE_TIME,
            SNAPSHOT_ID,
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(unread.reason_code, "uncalibrated_profile")
        conversation = parse_conversation_page(
            fixture_text("conversation-0.xml"), profile, self._context(), BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(conversation.reason_code, "uncalibrated_profile")
        missing = parse_conversation_page(
            fixture_text("conversation-0.xml"), base_profile(), {}, BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(missing.status, "failed")
        self.assertEqual(missing.reason_code, "invalid_fixture")
        extra = dict(self._context())
        extra["extra"] = "x"
        bad_extra = parse_conversation_page(
            fixture_text("conversation-0.xml"), base_profile(), extra, BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(bad_extra.reason_code, "invalid_fixture")
        page_none = parse_conversation_page(
            fixture_text("conversation-0.xml"), base_profile(), None, BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(page_none.reason_code, "identity_ambiguous")

    def test_public_merge_rejects_wrong_value_and_profile(self) -> None:
        """F3：公开合并入口对坏值类型/错 profile 受控失败，不泄漏 AssertionError。"""
        page = PageSnapshot(
            app=APP,
            profile_id="synthetic-boss-m3",
            page="chat_list",
            signals=("chat_list",),
            field_evidence=(FieldEvidence("page", "hierarchy", "high", "ok", None),),
        )
        wrong_value = ReadResult(
            "ok", page, None, SNAPSHOT_ID, BASE_TIME, "fixture", "high"
        )
        result = merge_conversations(
            [wrong_value],
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "invalid_fixture")
        pages = self._pages()
        other_profile = base_profile()
        other_profile["profile_id"] = "other-synthetic"
        mismatch = merge_conversations(
            pages,
            other_profile,
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(mismatch.status, "failed")
        self.assertEqual(mismatch.reason_code, "invalid_fixture")

    def test_huge_row_bounds_do_not_leak_overflow(self) -> None:
        """F3：巨大行坐标在方向中心计算中受控，不泄漏 OverflowError。"""
        huge = "9" * 400
        page1 = fixture_text("conversation-1.xml").replace(
            'bounds="[0,600][1000,800]"', f'bounds="[0,0][{huge},{huge}]"'
        )
        result = parse_conversation_page(
            page1, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID
        )
        self.assertIn(result.status, {"ok", "failed", "undetermined"})

    def test_unverified_stable_id_never_yields_key(self) -> None:
        """未标定稳定标识的真实版本即使方向/读全满足也不可发送。"""
        profile = base_profile()
        profile["selectors"]["conversation_stable_id_verified"] = False
        pages = [
            parse_conversation_page(fixture_text("conversation-0.xml"), profile, self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(fixture_text("conversation-1.xml"), profile, self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        result = merge_conversations(
            pages,
            profile,
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.inbound_state, "unknown")
        self.assertTrue(all(message.message_key is None for message in result.value.messages))
        self.assertFalse(is_actionable(result)[0])

    def test_same_text_and_second_without_stable_id_has_no_identity(self) -> None:
        """同正文同秒不是稳定身份，不能生成消息键。"""
        hash_a = message_stable_identity_hash("same-raw-id")
        hash_b = message_stable_identity_hash("same-raw-id")
        self.assertEqual(hash_a, hash_b)
        self.assertNotEqual(hash_a, message_stable_identity_hash("other-raw-id"))
        profile = base_profile()
        profile["selectors"]["conversation_stable_id_verified"] = False
        page = parse_conversation_page(
            fixture_text("conversation-1.xml"), profile, self._context(), BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(page.status, "ok")
        self.assertTrue(all(message.message_key is None for message in page.value.messages))

    def test_new_message_keys_only_reports_independent_new_keys(self) -> None:
        """SC-R07：跨快照只报告新稳定消息键，不合并同正文同秒。"""
        previous = merge_conversations(
            self._pages(),
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        extra = (
            f'<node resource-id="{_rid("chat_message_row")}" class="android.widget.LinearLayout" bounds="[0,900][1000,1100]">'
            f'<node resource-id="{_rid("iv_avatar")}" class="android.widget.ImageView" bounds="[20,920][120,1020]" />'
            f'<node resource-id="{_rid("bubble")}" class="android.widget.LinearLayout" bounds="[140,920][700,1020]" />'
            f'<node text="合成消息丁" resource-id="{_rid("tv_content_text")}" class="android.widget.TextView" bounds="[160,930][680,1010]" />'
            f'<node text="10:05" resource-id="{_rid("tv_time")}" class="android.widget.TextView" bounds="[720,960][820,1000]" />'
            f'<node content-desc="msg-4" resource-id="{_rid("msg_uid")}" class="android.widget.TextView" bounds="[0,0][1,1]" />'
            "</node>"
        )
        page1 = fixture_text("conversation-1.xml").replace(
            "    </node>\n  </node>\n</hierarchy>",
            extra + "    </node>\n  </node>\n</hierarchy>",
        )
        pages = [
            parse_conversation_page(fixture_text("conversation-0.xml"), base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
            parse_conversation_page(page1, base_profile(), self._context(), BASE_TIME, SNAPSHOT_ID),
        ]
        current = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(current.status, "ok")
        keys = new_message_keys(previous.value, current.value)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0], current.value.latest_inbound_key)

    def test_stale_and_future_conversation_rejected(self) -> None:
        """跨屏快照超过 5 秒或早于 now 为 stale_snapshot。"""
        pages = self._pages()
        stale = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            "2026-11-24T00:00:06.000Z",
        )
        self.assertEqual(stale.status, "undetermined")
        self.assertEqual(stale.reason_code, "stale_snapshot")
        future = merge_conversations(
            pages,
            base_profile(),
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            "2026-11-23T23:59:59.999Z",
        )
        self.assertEqual(future.reason_code, "stale_snapshot")

    @staticmethod
    def _context() -> dict:
        """返回合成会话上下文。"""
        return {
            "contact": "合成联系人甲",
            "company": "合成公司甲",
            "position": "合成职位甲",
            "source_snapshot_id": "00000000-0000-4000-8000-000000000201",
        }


class ReadValueAndStoreBoundaryTests(unittest.TestCase):
    """覆盖值类型分派、M1 仓储拒绝非未读值与定向核对接口。"""

    def test_read_result_accepts_known_values_and_rejects_unknown(self) -> None:
        """ReadResult 只接受登记值类型，未知类型仍拒绝。"""
        page = PageSnapshot(
            app=APP,
            profile_id="synthetic-boss-m3",
            page="chat_list",
            signals=("chat_list",),
            field_evidence=(FieldEvidence("page", "hierarchy", "high", "ok", None),),
        )
        validate_read_result(ReadResult("ok", page, None, SNAPSHOT_ID, BASE_TIME, "fixture", "high"))
        with self.assertRaises(ValueError):
            validate_read_value({"unexpected": True})
        with self.assertRaises(ValueError):
            validate_read_result(
                ReadResult("ok", {"unexpected": True}, None, SNAPSHOT_ID, BASE_TIME, "fixture", "high")
            )

    def test_store_rejects_non_unread_snapshot(self) -> None:
        """M1 upsert 不接收 M3 页面值，保持原信任边界。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "runtime.sqlite3"
            initialize_database(db)
            store = SqliteStore(db, process_log=ProcessLog(path=root / "store.jsonl"))
            page = PageSnapshot(
                app=APP,
                profile_id="synthetic-boss-m3",
                page="chat_list",
                signals=("chat_list",),
                field_evidence=(FieldEvidence("page", "hierarchy", "high", "ok", None),),
            )
            result = ReadResult("ok", page, None, SNAPSHOT_ID, BASE_TIME, "fixture", "high")
            with self.assertRaises(StoreError) as caught:
                store.upsert_snapshot(result)
            self.assertEqual(caught.exception.reason_code, "invalid_input")
            store.close()

    def test_targeted_verification_requires_locatable_target(self) -> None:
        """SC-R03/R04：一次定向核对仍无目标时返回 unread_unverified。"""
        profile = json.loads((SENSOR_DIR / "profile.json").read_text(encoding="utf-8"))
        xml = (SENSOR_DIR / "positive.xml").read_text(encoding="utf-8")
        initial = parse_snapshot(xml, profile, [], BASE_TIME, SNAPSHOT_ID)
        targeted = parse_snapshot(xml, profile, [], BASE_TIME, TARGETED_SNAPSHOT_ID)
        self.assertEqual(targeted.status, "ok")
        candidate = targeted.value.candidates[0]
        self.assertIsNone(
            targeted_verification_outcome(
                initial, targeted, [candidate.candidate_key], now=BASE_TIME
            )
        )
        self.assertEqual(
            targeted_verification_outcome(initial, targeted, [], now=BASE_TIME),
            "unread_unverified",
        )
        undetermined = ReadResult(
            "undetermined", None, "unread_unverified", SNAPSHOT_ID, BASE_TIME, "fixture", "low"
        )
        self.assertEqual(
            targeted_verification_outcome(
                undetermined, targeted, [], now=BASE_TIME
            ),
            "unread_unverified",
        )

    def test_targeted_verification_rejects_replay_and_bad_context(self) -> None:
        """定向核对拒绝同快照重放、旧/未来时间与错 profile，不解除未知。"""
        profile = json.loads((SENSOR_DIR / "profile.json").read_text(encoding="utf-8"))
        xml = (SENSOR_DIR / "positive.xml").read_text(encoding="utf-8")
        initial = parse_snapshot(xml, profile, [], BASE_TIME, SNAPSHOT_ID)
        targeted = parse_snapshot(xml, profile, [], BASE_TIME, TARGETED_SNAPSHOT_ID)
        self.assertEqual(
            targeted_verification_outcome(initial, initial, [], now=BASE_TIME),
            "unread_unverified",
        )
        older = replace(
            targeted, observed_at="2026-11-23T23:59:59.000Z"
        )
        self.assertEqual(
            targeted_verification_outcome(initial, older, [], now=BASE_TIME),
            "stale_snapshot",
        )
        future = replace(
            targeted, observed_at="2026-11-24T00:00:01.000Z"
        )
        self.assertEqual(
            targeted_verification_outcome(initial, future, [], now=BASE_TIME),
            "stale_snapshot",
        )
        wrong_profile = replace(
            targeted, value=replace(targeted.value, profile_id="other-synthetic")
        )
        self.assertEqual(
            targeted_verification_outcome(
                initial, wrong_profile, [], now=BASE_TIME
            ),
            "identity_ambiguous",
        )

    def test_m2_and_m3_share_one_result_serializer(self) -> None:
        """M2 sense 与 M3 read 复用同一序列化实现，sense 仍只接受 UnreadSnapshot。"""
        profile = json.loads((SENSOR_DIR / "profile.json").read_text(encoding="utf-8"))
        xml = (SENSOR_DIR / "positive.xml").read_text(encoding="utf-8")
        unread = parse_snapshot(xml, profile, [], BASE_TIME, SNAPSHOT_ID)
        payload = build_sense_payload(unread, None)
        self.assertEqual(
            payload["result"]["value"]["signals"]["row_badge"]["state"], "present"
        )
        self.assertEqual(list(payload["result"]["value"]["signals"]), [
            "row_badge", "new_greeting", "tab_badge", "preview_delta"
        ])
        page = PageSnapshot(
            app=APP,
            profile_id="synthetic-boss-m3",
            page="chat_list",
            signals=("chat_list",),
            field_evidence=(FieldEvidence("page", "hierarchy", "high", "ok", None),),
        )
        with self.assertRaises(ValueError):
            build_sense_payload(
                ReadResult("ok", page, None, SNAPSHOT_ID, BASE_TIME, "fixture", "high"),
                None,
            )

    def test_process_log_accepts_undetermined_and_identity_reason(self) -> None:
        """F6：日志白名单接受 undetermined 与 identity_ambiguous。"""
        with tempfile.TemporaryDirectory() as temp:
            log = ProcessLog(path=Path(temp) / "process.jsonl")
            self.assertTrue(
                log.record(
                    stage="read",
                    operation="conversation",
                    result="undetermined",
                    reason_code="identity_ambiguous",
                    error_category="read",
                )
            )
            self.assertFalse(log.degraded)

    def test_undetermined_does_not_enter_decision(self) -> None:
        """undetermined 永远不可决策，门禁抛固定错误。"""
        result = ReadResult(
            "undetermined", None, "unread_unverified", SNAPSHOT_ID, BASE_TIME, "fixture", "low"
        )
        self.assertEqual(is_actionable(result), (False, "not_ok"))
        with self.assertRaises(ReadNotActionable):
            require_actionable(result)
        payload = build_read_payload(result)
        self.assertFalse(payload["actionable"])
        self.assertEqual(payload["gate_reason"], "not_ok")


class PixelHeaderTests(unittest.TestCase):
    """覆盖 16 字节合成 RGBA 头的字段、字节序、格式与长度校验。"""

    def test_header_roundtrip_and_rejections(self) -> None:
        """小端头解析正确，未知格式/长度不符/零尺寸拒绝。"""
        raw = make_raw(100, 100, [])
        header = parse_screen_header(raw)
        self.assertEqual((header.width, header.height, header.pixel_format), (100, 100, 1))
        with self.assertRaises(ValueError):
            parse_screen_header(struct.pack("<IIII", 100, 100, 2, 0) + bytes(40000))
        with self.assertRaises(ValueError):
            parse_screen_header(struct.pack("<IIII", 100, 100, 1, 0) + bytes(39999))
        with self.assertRaises(ValueError):
            parse_screen_header(struct.pack("<IIII", 0, 100, 1, 0) + bytes(0))

    def test_header_colorspace_support_domain(self) -> None:
        """F5：合成头仅支持 colorspace=0，其它值明确拒绝。"""
        raw = make_raw(100, 100, [])
        self.assertEqual(parse_screen_header(raw).colorspace, 0)
        unsupported = struct.pack("<IIII", 100, 100, 1, 1) + bytes(40000)
        with self.assertRaises(ValueError):
            parse_screen_header(unsupported)

    def test_calibration_date_and_source_domain(self) -> None:
        """F5：真实日历日期与 synthetic 来源域严格校验。"""
        bad_date = calibration()
        bad_date["calibrated_on"] = "2026-02-30"
        with self.assertRaises(ValueError):
            validate_calibration(bad_date)
        bad_source = calibration()
        bad_source["source"] = "device_claimed"
        with self.assertRaises(ValueError):
            validate_calibration(bad_source)
        self.assertEqual(validate_calibration(calibration())["source"], "synthetic")


if __name__ == "__main__":
    unittest.main()
