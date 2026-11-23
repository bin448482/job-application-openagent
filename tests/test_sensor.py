"""验证 M2 XML 解析、四源聚合、分页合并、profile/manifest 与停止原因契约。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.adapters.boss.list import (
    load_and_parse_snapshot,
    merge_snapshots,
    parse_snapshot,
)
from app.domain.models import (
    canonical_hash,
    conversation_key_for,
    discovery_key_for,
    fingerprint_for,
    identity_hash_for,
    is_uuid,
)
from app.domain.scheduler import aggregate_unread, stop_reason_for

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "sensor"
BASE_TIME = "2026-11-24T00:00:00.000Z"
SNAPSHOT_ID = "00000000-0000-4000-8000-000000000101"
SNAPSHOT_ID_2 = "00000000-0000-4000-8000-000000000102"


def base_profile() -> dict:
    """读取合成正例 profile 的深拷贝，避免测试间互相污染。"""
    return copy.deepcopy(
        json.loads((FIXTURE_DIR / "profile.json").read_text(encoding="utf-8"))
    )


def positive_xml() -> str:
    """读取合成正例 XML，作为独立键算法断言的输入。"""
    return (FIXTURE_DIR / "positive.xml").read_text(encoding="utf-8")


def _rid(suffix: str) -> str:
    """生成合成资源 ID，覆盖 :id/ 后缀匹配形态。"""
    return f"com.example.synthetic:id/{suffix}"


def _field_node(suffix: str, text: str, bounds: str) -> str:
    """生成一个带原样文本与合法 bounds 的合成节点。"""
    return (
        f'<node text="{text}" resource-id="{_rid(suffix)}" '
        f'class="android.widget.TextView" bounds="{bounds}" />'
    )


def build_xml(
    rows: list[dict],
    *,
    profile: dict | None = None,
    list_nodes: int = 1,
    global_labels: list[dict] | None = None,
    extra_list_xml: str = "",
) -> str:
    """按行字典生成合成列表 XML，可选重复列表或外部全局标签。"""
    active = profile if profile is not None else base_profile()
    fields = active["row_fields"]
    row_suffix = active["row_id_suffix"]
    list_suffix = active["list_id_suffix"]
    row_parts: list[str] = []
    for index, row in enumerate(rows):
        row_bounds = row.get("row_bounds", "[0,0][1000,200]")
        pieces = [f'<node resource-id="{_rid(row_suffix)}" bounds="{row_bounds}">']
        for name in ("name", "company", "position", "preview", "time"):
            value = row.get(name)
            if value is None:
                continue
            pieces.append(
                _field_node(
                    fields[name],
                    str(value),
                    row.get(f"{name}_bounds", "[20,10][300,60]"),
                )
            )
        for badge, bounds in row.get("badges", []):
            pieces.append(
                f'<node text="{badge}" class="android.widget.TextView" '
                f'bounds="{bounds}" />'
            )
        pieces.append(row.get("extra_xml", ""))
        pieces.append("</node>")
        row_parts.append("".join(pieces))
    row_xml = "".join(row_parts)
    list_xml = (
        f'<node resource-id="{_rid(list_suffix)}" bounds="[0,0][1000,1800]">'
        f"{row_xml}{extra_list_xml}</node>"
    )
    pieces = ['<hierarchy rotation="0">', list_xml * list_nodes]
    for label in global_labels or []:
        suffix = str(label["count_suffix"])
        pieces.append(
            '<node class="android.widget.LinearLayout" bounds="[0,1800][1000,1900]">'
            f'<node text="{label["label"]}" class="android.widget.TextView" '
            'bounds="[0,1800][200,1900]" />'
            f'<node text="{label["text"]}" resource-id="{_rid(suffix)}" '
            'class="android.widget.TextView" bounds="[200,1800][300,1900]" />'
            "</node>"
        )
    pieces.append("</hierarchy>")
    return "".join(pieces)


def make_fixture(
    directory: Path,
    name: str,
    xml_texts: list[str],
    *,
    profile: dict | None = None,
    baseline_rows: list[dict] | None = None,
    proof: dict | None = None,
    app_version: str | None = None,
    manifest_extra: dict | None = None,
) -> Path:
    """在临时目录写入 profile、XML 与 manifest，返回 manifest 路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    active_profile = profile if profile is not None else base_profile()
    (directory / "profile.json").write_text(
        json.dumps(active_profile, ensure_ascii=False), encoding="utf-8"
    )
    paths: list[str] = []
    for index, text in enumerate(xml_texts):
        file_name = f"{name}-{index}.xml"
        (directory / file_name).write_text(text, encoding="utf-8")
        paths.append(file_name)
    manifest = {
        "profile_path": "profile.json",
        "xml_paths": paths,
        "app_version": app_version or str(active_profile["version"]),
        "observed_at": BASE_TIME,
        "baseline_rows": baseline_rows or [],
        "coverage_proof": proof
        or {"kind": "viewport", "top_proven": False, "bottom_proven": False},
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


class SensorParseTests(unittest.TestCase):
    """覆盖单页四源判定、身份、边界、XML 安全与独立键算法。"""

    def _parse(
        self,
        xml: str,
        *,
        profile: dict | None = None,
        baselines: list[dict] | None = None,
        observed_at: str = BASE_TIME,
        snapshot_id: str = SNAPSHOT_ID,
    ):
        """调用单页解析并返回读取结果。"""
        return parse_snapshot(
            xml,
            profile if profile is not None else base_profile(),
            baselines or [],
            observed_at,
            snapshot_id,
        )

    def _merged(self, pages: list, *, profile: dict | None = None, proof=None, now=BASE_TIME):
        """调用分页合并并返回读取结果。"""
        return merge_snapshots(
            pages,
            profile if profile is not None else base_profile(),
            [],
            proof
            or {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            now,
        )

    def test_positive_row_badge_and_independent_keys(self) -> None:
        """有效行角标产生唯一 unread 候选，身份、指纹与发现键独立重算。"""
        result = self._parse(positive_xml())
        self.assertEqual(result.status, "ok")
        self.assertTrue(is_uuid(result.snapshot_id))
        snapshot = result.value
        self.assertEqual(snapshot.unread_state, "present")
        self.assertEqual(snapshot.signals["row_badge"].state, "present")
        self.assertEqual(snapshot.signals["row_badge"].count, 1)
        self.assertEqual(len(snapshot.candidates), 1)
        candidate = snapshot.candidates[0]
        self.assertEqual(candidate.kind, "unread")
        self.assertEqual(candidate.source_names, ("row_badge",))
        row = snapshot.rows[0]
        expected_identity = identity_hash_for("合成联系人", "合成公司", "合成职位")
        expected_conversation = conversation_key_for(
            "boss", "合成联系人", "合成公司", "合成职位"
        )
        expected_preview = canonical_hash("合成预览正文")
        expected_fingerprint = fingerprint_for(expected_preview, "10:00", 1)
        self.assertEqual(row.identity_hash, expected_identity)
        self.assertEqual(row.conversation_key, expected_conversation)
        self.assertEqual(row.preview_hash, expected_preview)
        self.assertEqual(candidate.fingerprint, expected_fingerprint)
        self.assertEqual(
            candidate.candidate_key,
            discovery_key_for("boss", expected_conversation, expected_fingerprint),
        )

    def test_preview_and_time_digits_are_not_row_badges(self) -> None:
        """正文与时间中的数字即使为 TextView 也不在角标区域时不算未读。"""
        xml = build_xml(
            [
                {
                    "name": "合成联系人",
                    "company": "合成公司",
                    "position": "合成职位",
                    "preview": "2026",
                    "time": "12",
                    "time_bounds": "[950,70][990,90]",
                    "preview_bounds": "[900,40][945,65]",
                }
            ]
        )
        result = self._parse(xml)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.signals["row_badge"].state, "unknown")
        self.assertEqual(result.value.signals["row_badge"].reason_code, "missing_signal")
        self.assertEqual(result.value.candidates, ())

    def test_explicit_zero_is_absent_only_with_full_traversal(self) -> None:
        """仅视口零角标是 unknown/incomplete，完整遍历且其它源为零才 absent。"""
        profile = base_profile()
        profile["new_greeting"]["semantics_verified"] = True
        profile["tab_badge"]["global_zero_verified"] = True
        preview = "相同预览"
        identity = identity_hash_for("合成联系人", "合成公司", "合成职位")
        baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": canonical_hash(preview),
                "time_hint": "10:00",
            }
        ]
        xml = build_xml(
            [
                {
                    "name": "合成联系人",
                    "company": "合成公司",
                    "position": "合成职位",
                    "preview": preview,
                    "time": "10:00",
                    "badges": [("0", "[900,40][960,90]")],
                }
            ],
            profile=profile,
            global_labels=[
                {"label": "新招呼", "count_suffix": "greeting_count", "text": "0"},
                {"label": "消息", "count_suffix": "tab_count", "text": "0"},
            ],
        )
        page = parse_snapshot(xml, profile, baseline, BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(page.status, "ok")
        self.assertEqual(page.value.signals["row_badge"].state, "unknown")
        self.assertEqual(
            page.value.signals["row_badge"].reason_code, "incomplete_coverage"
        )
        self.assertEqual(page.value.unread_state, "unknown")

        merged = merge_snapshots(
            [page],
            profile,
            baseline,
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(merged.status, "ok")
        self.assertEqual(merged.value.coverage, "all")
        for name in ("row_badge", "new_greeting", "tab_badge", "preview_delta"):
            self.assertEqual(merged.value.signals[name].state, "absent", name)
        self.assertEqual(merged.value.unread_state, "absent")
        self.assertEqual(merged.value.candidates, ())

    def test_viewport_zero_does_not_become_absent(self) -> None:
        """仅视口明确为零仍为 unknown，不能宣告全局无未读。"""
        profile = base_profile()
        profile["new_greeting"]["semantics_verified"] = True
        profile["tab_badge"]["global_zero_verified"] = True
        xml = build_xml(
            [
                {
                    "name": "合成联系人",
                    "company": "合成公司",
                    "position": "合成职位",
                    "preview": "相同预览",
                    "time": "10:00",
                    "badges": [("0", "[900,40][960,90]")],
                }
            ],
            profile=profile,
            global_labels=[
                {"label": "新招呼", "count_suffix": "greeting_count", "text": "0"},
                {"label": "消息", "count_suffix": "tab_count", "text": "0"},
            ],
        )
        result = self._parse(xml, profile=profile)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.coverage, "viewport")
        self.assertEqual(result.value.signals["row_badge"].state, "unknown")
        self.assertEqual(result.value.unread_state, "unknown")
        self.assertNotEqual(result.value.unread_state, "absent")

    def test_all_sources_missing_and_empty_list_are_unknown(self) -> None:
        """全部源缺失和空列表均不得把 unknown 冒充为 absent。"""
        missing = self._parse(build_xml([]))
        self.assertEqual(missing.status, "ok")
        self.assertEqual(missing.value.unread_state, "unknown")
        self.assertEqual(
            missing.value.signals["row_badge"].reason_code, "missing_signal"
        )
        self.assertEqual(
            missing.value.signals["preview_delta"].reason_code, "missing_signal"
        )
        merged = merge_snapshots(
            [missing],
            base_profile(),
            [],
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(merged.status, "ok")
        self.assertEqual(merged.value.coverage, "all")
        self.assertEqual(merged.value.unread_state, "unknown")

    def test_partial_zero_and_unknown_stays_unknown(self) -> None:
        """部分源明确为零而另一适用源缺失时仍为 unknown。"""
        xml = build_xml(
            [
                {
                    "name": "合成联系人",
                    "company": "合成公司",
                    "position": "合成职位",
                    "preview": "预览",
                    "time": "10:00",
                    "badges": [("0", "[900,40][960,90]")],
                }
            ]
        )
        result = self._parse(xml)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value.signals["new_greeting"].state, "unknown")
        self.assertEqual(result.value.unread_state, "unknown")

    def test_unverified_new_greeting_semantics(self) -> None:
        """未验证语义的新招呼 0 为 unknown，正值仍保守 present 且不虚构行。"""
        profile = base_profile()
        profile["new_greeting"]["semantics_verified"] = False
        zero_xml = build_xml(
            [],
            profile=profile,
            global_labels=[
                {"label": "新招呼", "count_suffix": "greeting_count", "text": "0"}
            ],
        )
        zero = self._parse(zero_xml, profile=profile)
        self.assertEqual(zero.status, "ok")
        self.assertEqual(zero.value.signals["new_greeting"].state, "unknown")
        self.assertEqual(
            zero.value.signals["new_greeting"].reason_code, "unverified_semantics"
        )
        self.assertNotEqual(zero.value.unread_state, "absent")

        positive_xml_text = build_xml(
            [],
            profile=profile,
            global_labels=[
                {"label": "新招呼", "count_suffix": "greeting_count", "text": "1"}
            ],
        )
        positive = self._parse(positive_xml_text, profile=profile)
        self.assertEqual(positive.value.signals["new_greeting"].state, "present")
        self.assertEqual(positive.value.unread_state, "present")
        self.assertEqual(positive.value.candidates, ())

    def test_preview_change_requires_comparable_baseline(self) -> None:
        """无基线不能产生 new_message；变化基线才产生可定位预览候选。"""
        xml = build_xml(
            [
                {
                    "name": "合成联系人",
                    "company": "合成公司",
                    "position": "合成职位",
                    "preview": "新预览",
                    "time": "11:00",
                }
            ]
        )
        no_baseline = self._parse(xml)
        self.assertEqual(no_baseline.value.signals["preview_delta"].state, "unknown")
        self.assertEqual(
            no_baseline.value.signals["preview_delta"].reason_code, "no_baseline"
        )
        self.assertEqual(no_baseline.value.candidates, ())

        identity = identity_hash_for("合成联系人", "合成公司", "合成职位")
        baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": canonical_hash("旧预览"),
                "time_hint": "10:00",
            }
        ]
        changed = self._parse(xml, baselines=baseline)
        self.assertEqual(changed.value.signals["preview_delta"].state, "present")
        self.assertEqual(len(changed.value.candidates), 1)
        self.assertEqual(changed.value.candidates[0].kind, "new_message")
        self.assertEqual(
            changed.value.candidates[0].source_names, ("preview_delta",)
        )

    def test_same_conversation_multiple_sources_merges_to_unread(self) -> None:
        """同一行未读与预览变化只产生一个 unread，并保留两种来源。"""
        profile = base_profile()
        xml = build_xml(
            [
                {
                    "name": "合成联系人",
                    "company": "合成公司",
                    "position": "合成职位",
                    "preview": "新预览",
                    "time": "11:00",
                    "badges": [("2", "[900,40][960,90]")],
                }
            ],
            profile=profile,
        )
        identity = identity_hash_for("合成联系人", "合成公司", "合成职位")
        baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": canonical_hash("旧预览"),
                "time_hint": "10:00",
            }
        ]
        result = self._parse(xml, profile=profile, baselines=baseline)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.value.candidates), 1)
        candidate = result.value.candidates[0]
        self.assertEqual(candidate.kind, "unread")
        self.assertEqual(candidate.source_names, ("row_badge", "preview_delta"))

    def test_missing_and_duplicate_identity_rows_are_not_locatable(self) -> None:
        """身份缺失或重复的行保留行结构但不产生可定位候选。"""
        missing = self._parse(
            build_xml(
                [
                    {
                        "name": "合成联系人",
                        "position": "合成职位",
                        "preview": "预览",
                        "badges": [("1", "[900,40][960,90]")],
                    }
                ]
            )
        )
        self.assertEqual(missing.status, "ok")
        self.assertIsNone(missing.value.rows[0].conversation_key)
        self.assertIsNone(missing.value.rows[0].identity_hash)
        self.assertEqual(missing.value.signals["row_badge"].state, "present")
        self.assertEqual(missing.value.candidates, ())

        duplicate = self._parse(
            build_xml(
                [
                    {
                        "name": "合成联系人",
                        "company": "合成公司",
                        "position": "合成职位",
                        "preview": "预览",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                    {
                        "name": "合成联系人",
                        "company": "合成公司",
                        "position": "合成职位",
                        "preview": "预览",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                ]
            )
        )
        self.assertEqual(duplicate.status, "ok")
        self.assertEqual(len(duplicate.value.rows), 2)
        self.assertTrue(
            all(row.conversation_key is None for row in duplicate.value.rows)
        )
        self.assertEqual(duplicate.value.candidates, ())

    def test_pagination_overlap_merges_and_rebuilds_unique_keys(self) -> None:
        """连续整行重叠只合并一次，合并后唯一身份可重建会话键。"""
        first = self._parse(
            build_xml(
                [
                    {
                        "name": "甲",
                        "company": "公司甲",
                        "position": "职位甲",
                        "preview": "预览甲",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                    {
                        "name": "乙",
                        "company": "公司乙",
                        "position": "职位乙",
                        "preview": "预览乙",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                ]
            ),
            snapshot_id=SNAPSHOT_ID,
        )
        second = self._parse(
            build_xml(
                [
                    {
                        "name": "乙",
                        "company": "公司乙",
                        "position": "职位乙",
                        "preview": "预览乙",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                    {
                        "name": "丙",
                        "company": "公司丙",
                        "position": "职位丙",
                        "preview": "预览丙",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                ]
            ),
            snapshot_id=SNAPSHOT_ID,
        )
        merged = self._merged([first, second])
        self.assertEqual(merged.status, "ok")
        names = [row.identity_hash for row in merged.value.rows]
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(row.conversation_key is not None for row in merged.value.rows))
        self.assertEqual(len(merged.value.candidates), 3)

    def test_non_boundary_duplicate_identity_stays_unlocatable(self) -> None:
        """非边界重复身份即使合并后仍置空会话键，不产生候选。"""
        first = self._parse(
            build_xml(
                [
                    {
                        "name": "甲",
                        "company": "公司甲",
                        "position": "职位甲",
                        "preview": "预览甲",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                    {
                        "name": "乙",
                        "company": "公司乙",
                        "position": "职位乙",
                        "preview": "预览乙",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                ]
            )
        )
        second = self._parse(
            build_xml(
                [
                    {
                        "name": "甲",
                        "company": "公司甲",
                        "position": "职位甲",
                        "preview": "预览甲",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                    {
                        "name": "丁",
                        "company": "公司丁",
                        "position": "职位丁",
                        "preview": "预览丁",
                        "badges": [("1", "[900,40][960,90]")],
                    },
                ]
            )
        )
        merged = self._merged([first, second])
        self.assertEqual(merged.status, "ok")
        duplicate_hash = identity_hash_for("甲", "公司甲", "职位甲")
        duplicates = [
            row for row in merged.value.rows if row.identity_hash == duplicate_hash
        ]
        self.assertEqual(len(duplicates), 2)
        self.assertTrue(all(row.conversation_key is None for row in duplicates))
        self.assertEqual(len(merged.value.candidates), 2)

    def test_global_signal_conflicts_are_conservative(self) -> None:
        """分页全局信号不一致不求和；任一 present 保留 present 且计数为空。"""
        profile = base_profile()
        page_one = self._parse(
            build_xml(
                [],
                profile=profile,
                global_labels=[
                    {"label": "消息", "count_suffix": "tab_count", "text": "3"}
                ],
            ),
            profile=profile,
        )
        page_two = self._parse(
            build_xml(
                [],
                profile=profile,
                global_labels=[
                    {"label": "消息", "count_suffix": "tab_count", "text": "0"}
                ],
            ),
            profile=profile,
        )
        merged = self._merged([page_one, page_two], profile=profile)
        self.assertEqual(merged.status, "ok")
        signal = merged.value.signals["tab_badge"]
        self.assertEqual(signal.state, "present")
        self.assertIsNone(signal.count)
        self.assertEqual(merged.value.unread_state, "present")

    def test_stale_and_future_snapshots_are_rejected(self) -> None:
        """超过 5 秒或早于观察时间的快照均为 stale_snapshot。"""
        page = self._parse(positive_xml())
        stale = merge_snapshots(
            [page],
            base_profile(),
            [],
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            "2026-11-24T00:00:06.000Z",
        )
        self.assertEqual(stale.status, "undetermined")
        self.assertEqual(stale.reason_code, "stale_snapshot")
        self.assertIsNone(stale.value)
        future = merge_snapshots(
            [page],
            base_profile(),
            [],
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            "2026-11-23T23:59:59.000Z",
        )
        self.assertEqual(future.status, "undetermined")
        self.assertEqual(future.reason_code, "stale_snapshot")

    def test_uncalibrated_profile_and_version_mismatch(self) -> None:
        """未标定或版本不符整体 undetermined，不解析出部分候选。"""
        profile = base_profile()
        profile["calibrated"] = False
        direct = self._parse(positive_xml(), profile=profile)
        self.assertEqual(direct.status, "undetermined")
        self.assertEqual(direct.reason_code, "uncalibrated_profile")
        self.assertIsNone(direct.value)

        with tempfile.TemporaryDirectory() as tmp:
            manifest = make_fixture(
                Path(tmp),
                "mismatch",
                [positive_xml()],
                app_version="other-version",
            )
            result = load_and_parse_snapshot(manifest, SNAPSHOT_ID)
        self.assertEqual(result.status, "undetermined")
        self.assertEqual(result.reason_code, "uncalibrated_profile")

    def test_invalid_xml_and_page_structure(self) -> None:
        """损坏、DTD、ENTITY、其它元素、错页与嵌套行均按固定原因失败。"""
        cases = {
            "malformed": ("<hierarchy>", "invalid_xml"),
            "doctype": (
                '<!DOCTYPE hierarchy [<!ENTITY x "y">]><hierarchy />',
                "invalid_xml",
            ),
            "entity": ("<hierarchy>&synthetic;</hierarchy>", "invalid_xml"),
            "other-element": (
                "<hierarchy><linear /></hierarchy>",
                "invalid_xml",
            ),
            "nested-hierarchy": (
                "<hierarchy><node><hierarchy /></node></hierarchy>",
                "invalid_xml",
            ),
            "wrong-root": ("<linear />", "invalid_xml"),
            "wrong-page": ("<hierarchy />", "wrong_page"),
        }
        for name, (xml, reason) in cases.items():
            with self.subTest(name=name):
                result = self._parse(xml)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason_code, reason)
                self.assertIsNone(result.value)

        two_lists = self._parse(build_xml([], list_nodes=2))
        self.assertEqual(two_lists.status, "undetermined")
        self.assertEqual(two_lists.reason_code, "incomplete_snapshot")

        nested = (
            '<hierarchy><node resource-id="com.example.synthetic:id/message_list" '
            'bounds="[0,0][1000,1000]">'
            '<node resource-id="com.example.synthetic:id/conversation_row" '
            'bounds="[0,0][1000,200]">'
            '<node resource-id="com.example.synthetic:id/conversation_row" '
            'bounds="[0,0][1000,100]" /></node></node></hierarchy>'
        )
        nested_result = self._parse(nested)
        self.assertEqual(nested_result.status, "undetermined")
        self.assertEqual(nested_result.reason_code, "incomplete_snapshot")

    def test_invalid_snapshot_id_and_baseline_hash_fail_closed(self) -> None:
        """非法 snapshot UUID 或非摘要基线必须在解析前失败，不能洗成成功。"""
        invalid_id = parse_snapshot(
            positive_xml(), base_profile(), [], BASE_TIME, "invalid"
        )
        self.assertEqual(invalid_id.status, "failed")
        self.assertEqual(invalid_id.reason_code, "invalid_fixture")
        self.assertIsNone(invalid_id.snapshot_id)
        self.assertIsNone(invalid_id.value)

        identity = identity_hash_for("合成联系人", "合成公司", "合成职位")
        invalid_baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": "invalid-hash",
                "time_hint": "10:00",
            }
        ]
        direct = parse_snapshot(
            positive_xml(), base_profile(), invalid_baseline, BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(direct.status, "failed")
        self.assertEqual(direct.reason_code, "invalid_fixture")
        self.assertIsNone(direct.value)

        with tempfile.TemporaryDirectory() as tmp:
            manifest = make_fixture(
                Path(tmp),
                "invalid-baseline",
                [positive_xml()],
                baseline_rows=invalid_baseline,
            )
            loaded = load_and_parse_snapshot(manifest, SNAPSHOT_ID)
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.reason_code, "invalid_fixture")

    def test_badge_region_bounds_and_ambiguity(self) -> None:
        """空区域、非法行 bounds、多个整数节点与区域外数字按固定原因处理。"""
        profile = base_profile()
        profile["badge_region"] = None
        no_region = self._parse(
            build_xml(
                [
                    {
                        "name": "甲",
                        "company": "公司甲",
                        "position": "职位甲",
                        "preview": "预览",
                        "badges": [("1", "[900,40][960,90]")],
                    }
                ],
                profile=profile,
            ),
            profile=profile,
        )
        self.assertEqual(
            no_region.value.signals["row_badge"].reason_code, "uncalibrated_region"
        )

        bad_bounds = self._parse(
            build_xml(
                [
                    {
                        "name": "甲",
                        "company": "公司甲",
                        "position": "职位甲",
                        "preview": "预览",
                        "row_bounds": "[0,0][0,200]",
                        "badges": [("1", "[900,40][960,90]")],
                    }
                ]
            )
        )
        self.assertEqual(
            bad_bounds.value.signals["row_badge"].reason_code, "uncalibrated_region"
        )

        ambiguous = self._parse(
            build_xml(
                [
                    {
                        "name": "甲",
                        "company": "公司甲",
                        "position": "职位甲",
                        "preview": "预览",
                        "badges": [
                            ("1", "[900,40][960,90]"),
                            ("2", "[905,45][965,95]"),
                        ],
                    }
                ]
            )
        )
        self.assertEqual(
            ambiguous.value.signals["row_badge"].reason_code, "ambiguous_signal"
        )

        outside_only = self._parse(
            build_xml(
                [
                    {
                        "name": "甲",
                        "company": "公司甲",
                        "position": "职位甲",
                        "preview": "预览",
                        "badges": [("7", "[100,100][200,150]")],
                    }
                ]
            )
        )
        self.assertEqual(
            outside_only.value.signals["row_badge"].reason_code, "missing_signal"
        )

    def test_not_applicable_sources_do_not_claim_absent(self) -> None:
        """全部源显式不适用仍为 unknown，不能被当作无未读。"""
        profile = base_profile()
        profile["not_applicable_sources"] = [
            "row_badge",
            "new_greeting",
            "tab_badge",
            "preview_delta",
        ]
        xml = build_xml(
            [
                {
                    "name": "甲",
                    "company": "公司甲",
                    "position": "职位甲",
                    "preview": "预览",
                }
            ],
            profile=profile,
        )
        page = self._parse(xml, profile=profile)
        self.assertEqual(page.status, "ok")
        for name in ("row_badge", "new_greeting", "tab_badge", "preview_delta"):
            self.assertEqual(page.value.signals[name].state, "not_applicable")
        merged = merge_snapshots(
            [page],
            profile,
            [],
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(merged.value.unread_state, "unknown")

    def test_global_lower_bound_and_zero_plus(self) -> None:
        """正下界保留 present 且计数为空，0+ 为未知歧义。"""
        profile = base_profile()
        lower = self._parse(
            build_xml(
                [],
                profile=profile,
                global_labels=[
                    {"label": "新招呼", "count_suffix": "greeting_count", "text": "3+"}
                ],
            ),
            profile=profile,
        )
        signal = lower.value.signals["new_greeting"]
        self.assertEqual(signal.state, "present")
        self.assertIsNone(signal.count)
        zero_plus = self._parse(
            build_xml(
                [],
                profile=profile,
                global_labels=[
                    {"label": "新招呼", "count_suffix": "greeting_count", "text": "0+"}
                ],
            ),
            profile=profile,
        )
        self.assertEqual(zero_plus.value.signals["new_greeting"].state, "unknown")
        self.assertEqual(
            zero_plus.value.signals["new_greeting"].reason_code, "ambiguous_signal"
        )

    def test_manifest_rejects_extra_keys_and_unsafe_references(self) -> None:
        """manifest 额外键、越界路径、绝对路径与缺失文件均为 invalid_fixture。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = make_fixture(root, "good", [positive_xml()])
            extra = make_fixture(root, "extra", [positive_xml()])
            extra_data = json.loads(extra.read_text(encoding="utf-8"))
            extra_data["unexpected"] = True
            extra.write_text(json.dumps(extra_data), encoding="utf-8")

            escape = make_fixture(root, "escape", [positive_xml()])
            escape_data = json.loads(escape.read_text(encoding="utf-8"))
            escape_data["xml_paths"] = ["../outside.xml"]
            escape.write_text(json.dumps(escape_data), encoding="utf-8")

            absolute = make_fixture(root, "absolute", [positive_xml()])
            absolute_data = json.loads(absolute.read_text(encoding="utf-8"))
            absolute_data["xml_paths"] = [str((root / "good-0.xml").resolve())]
            absolute.write_text(json.dumps(absolute_data), encoding="utf-8")

            missing = make_fixture(root, "missing", [positive_xml()])
            (root / "missing-0.xml").unlink()

            symlink = make_fixture(root, "symlink", [positive_xml()])
            link = root / "linked.xml"
            try:
                os.symlink(root / "good-0.xml", link)
            except (OSError, NotImplementedError):
                link = None
            symlink_case: Path | None = None
            if link is not None:
                symlink_data = json.loads(symlink.read_text(encoding="utf-8"))
                symlink_data["xml_paths"] = ["linked.xml"]
                symlink.write_text(json.dumps(symlink_data), encoding="utf-8")
                symlink_case = symlink

            cases = [good, extra, escape, absolute, missing]
            if symlink_case is not None:
                cases.append(symlink_case)
            for path in cases:
                with self.subTest(name=path.stem):
                    result = load_and_parse_snapshot(path, SNAPSHOT_ID)
                    if path.stem == "good":
                        self.assertEqual(result.status, "ok")
                    else:
                        self.assertEqual(result.status, "failed")
                        self.assertEqual(result.reason_code, "invalid_fixture")
                        self.assertIsNone(result.value)

    def test_merge_rejects_mismatched_ids_and_invalid_proof(self) -> None:
        """不同 snapshot ID、重复空页与非法覆盖率证明均 invalid_fixture。"""
        first = self._parse(positive_xml(), snapshot_id=SNAPSHOT_ID)
        second = self._parse(positive_xml(), snapshot_id=SNAPSHOT_ID_2)
        mismatched = self._merged([first, second])
        self.assertEqual(mismatched.status, "failed")
        self.assertEqual(mismatched.reason_code, "invalid_fixture")
        empty = merge_snapshots(
            [],
            base_profile(),
            [],
            {"kind": "viewport", "top_proven": False, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(empty.status, "failed")
        self.assertEqual(empty.reason_code, "invalid_fixture")
        bad_proof = merge_snapshots(
            [first],
            base_profile(),
            [],
            {"kind": "unsupported", "top_proven": True, "bottom_proven": False},
            BASE_TIME,
        )
        self.assertEqual(bad_proof.status, "failed")
        self.assertEqual(bad_proof.reason_code, "invalid_fixture")


class SensorAggregationTests(unittest.TestCase):
    """覆盖领域聚合与 stop_reason 的有序判据。"""

    def _positive(self):
        """解析正例并写入声称可领取的 unread 键。"""
        result = parse_snapshot(
            positive_xml(), base_profile(), [], BASE_TIME, SNAPSHOT_ID
        )
        self.assertEqual(result.status, "ok")
        return result

    def test_claimable_unread_returns_no_stop(self) -> None:
        """存在可领取 unread 时 stop_reason=null。"""
        result = self._positive()
        queued = [result.value.candidates[0].candidate_key]
        self.assertIsNone(stop_reason_for(result.value, queued))

    def test_positive_without_claimable_candidate_needs_targeting(self) -> None:
        """行角标为正但没有可领取候选时必须定向核对。"""
        result = self._positive()
        self.assertEqual(
            stop_reason_for(result.value, []), "needs_targeted_verification"
        )

    def test_missing_sources_are_unread_unverified(self) -> None:
        """前三源未知且无正信号时 stop_reason=unread_unverified。"""
        profile = base_profile()
        xml = build_xml(
            [
                {
                    "name": "甲",
                    "company": "公司甲",
                    "position": "职位甲",
                    "preview": "预览",
                }
            ],
            profile=profile,
        )
        result = parse_snapshot(xml, profile, [], BASE_TIME, SNAPSHOT_ID)
        self.assertEqual(result.value.unread_state, "unknown")
        self.assertEqual(
            stop_reason_for(result.value, []), "unread_unverified"
        )

    def test_preview_only_new_message_can_continue(self) -> None:
        """其它源明确 absent 且预览候选可领取时 stop_reason=null。"""
        profile = base_profile()
        profile["new_greeting"]["semantics_verified"] = True
        profile["tab_badge"]["global_zero_verified"] = True
        identity = identity_hash_for("甲", "公司甲", "职位甲")
        baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": canonical_hash("旧预览"),
                "time_hint": "10:00",
            }
        ]
        xml = build_xml(
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
            profile=profile,
            global_labels=[
                {"label": "新招呼", "count_suffix": "greeting_count", "text": "0"},
                {"label": "消息", "count_suffix": "tab_count", "text": "0"},
            ],
        )
        page = parse_snapshot(xml, profile, baseline, BASE_TIME, SNAPSHOT_ID)
        merged = merge_snapshots(
            [page],
            profile,
            baseline,
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(merged.status, "ok")
        self.assertEqual(merged.value.signals["preview_delta"].state, "present")
        queued = [merged.value.candidates[0].candidate_key]
        self.assertIsNone(stop_reason_for(merged.value, queued))

    def test_absent_snapshot_returns_no_stop(self) -> None:
        """完整遍历下所有适用源 absent 且无未关闭发现时 stop_reason=null。"""
        profile = base_profile()
        profile["new_greeting"]["semantics_verified"] = True
        profile["tab_badge"]["global_zero_verified"] = True
        identity = identity_hash_for("甲", "公司甲", "职位甲")
        baseline = [
            {
                "conversation_key": f"boss:conv:{identity}",
                "preview_hash": canonical_hash("相同"),
                "time_hint": "10:00",
            }
        ]
        xml = build_xml(
            [
                {
                    "name": "甲",
                    "company": "公司甲",
                    "position": "职位甲",
                    "preview": "相同",
                    "time": "10:00",
                    "badges": [("0", "[900,40][960,90]")],
                }
            ],
            profile=profile,
            global_labels=[
                {"label": "新招呼", "count_suffix": "greeting_count", "text": "0"},
                {"label": "消息", "count_suffix": "tab_count", "text": "0"},
            ],
        )
        page = parse_snapshot(xml, profile, baseline, BASE_TIME, SNAPSHOT_ID)
        merged = merge_snapshots(
            [page],
            profile,
            baseline,
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(merged.value.unread_state, "absent")
        self.assertIsNone(stop_reason_for(merged.value, []))

    def test_unresolved_conversation_blocks_absent(self) -> None:
        """任一未关闭发现使 absent 降级为 unknown，并由 stop_reason 保守出口。"""
        profile = base_profile()
        profile["not_applicable_sources"] = [
            "row_badge",
            "new_greeting",
            "preview_delta",
        ]
        profile["tab_badge"]["global_zero_verified"] = True
        xml = build_xml(
            [],
            profile=profile,
            global_labels=[
                {"label": "消息", "count_suffix": "tab_count", "text": "0"}
            ],
        )
        page = parse_snapshot(xml, profile, [], BASE_TIME, SNAPSHOT_ID)
        merged = merge_snapshots(
            [page],
            profile,
            [],
            {"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
            BASE_TIME,
        )
        self.assertEqual(merged.value.unread_state, "absent")
        unresolved_key = f"boss:conv:{identity_hash_for('甲', '公司甲', '职位甲')}"
        aggregated = aggregate_unread(merged.value, [unresolved_key])
        self.assertEqual(aggregated.unread_state, "unknown")
        self.assertEqual(
            stop_reason_for(aggregated, []), "unread_unverified"
        )


if __name__ == "__main__":
    unittest.main()
