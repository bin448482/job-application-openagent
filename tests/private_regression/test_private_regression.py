"""私有回归工具的纯合成自测，使用独立预期检查脱敏、边界与安全回执。

所有输入都是临时目录中的合成 XML/JSONL；测试不读取真实旧项目，不连接设备，
不调用网络或模型。断言以已知合成秘密串的反例为准，而不是复用实现自身的输出。
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract  # noqa: E402


def _alias_factory() -> callable:
    """返回稳定且符合生产别名格式的测试 token 工厂。"""
    counter = itertools.count(1)
    return lambda: f"res_{next(counter):08x}"


def _make_repo(root: Path, ignore: str = "/data/**\n") -> Path:
    """在临时目录初始化 Git 工作区并写入忽略规则。"""
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(ignore, encoding="utf-8")
    return repo


def _make_source(root: Path) -> Path:
    """创建只含 screenshots/ 与 data/ 的合成源目录。"""
    source = root / "source"
    (source / "screenshots").mkdir(parents=True)
    (source / "data").mkdir(parents=True)
    return source


def _xml(*nodes: str, rotation: str = "0") -> str:
    """拼装一个 Android hierarchy 合成文档。"""
    return f'<hierarchy rotation="{rotation}">{"".join(nodes)}</hierarchy>'


def _node(**attrs: str) -> str:
    """把属性字典渲染为 node 标签。"""
    body = " ".join(f'{key}="{value}"' for key, value in attrs.items())
    return f"<node {body}/>"


def _run_main(args: list[str]) -> tuple[int, str, str]:
    """运行 CLI 并捕获 stdout/stderr，供泄露检查使用。"""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = extract.main(args)
    return code, out.getvalue(), err.getvalue()


def _keys(payload: object) -> set[str]:
    """递归收集 JSON 键名。"""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key).lower())
            found |= _keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _keys(item)
    return found


def _read_json(path: Path) -> dict:
    """读取并解析 JSON 产物。"""
    return json.loads(path.read_text(encoding="utf-8"))


class PrivateRegressionTests(unittest.TestCase):
    """覆盖白名单、异常输入、路径/预算、时序、验证器与泄露边界。"""

    def setUp(self) -> None:
        """为每个测试创建独立临时目录。"""
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(self._temp.name)

    def _make_junction(self, link: Path, target: Path) -> None:
        """创建 Windows 目录 junction；失败则跳过当前测试。"""
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("junction creation unavailable")
        self.addCleanup(
            lambda: subprocess.run(
                ["cmd", "/c", "rmdir", str(link)],
                capture_output=True,
                check=False,
            )
        )

    # ---------- XML 白名单与拓扑 ----------

    def test_topology_recurses_and_drops_unknown_fields(self) -> None:
        """深层节点必须保留父子次序，未知属性/真实文本被替换。"""
        raw = _xml(
            _node(
                **{
                    "class": "android.widget.FrameLayout",
                    "bounds": "[0,0][100,100]",
                    "package": "com.secret.pkg",
                    "data-secret": "SYNTH_SECRET_ATTR",
                }
            ).replace("/>", ">")
            + _node(
                **{
                    "class": "android.widget.LinearLayout",
                    "bounds": "[1,1][90,90]",
                }
            ).replace("/>", ">")
            + _node(
                **{
                    "class": "android.widget.TextView",
                    "text": "SYNTH_SECRET_TEXT",
                    "content-desc": "SYNTH_SECRET_DESC",
                    "resource-id": "com.secret:id/row",
                    "clickable": "true",
                    "bounds": "[2,2][80,80]",
                }
            )
            + "</node></node>"
        )
        outcome = extract.sanitize_xml(raw.encode("utf-8"), extract.AliasBook(_alias_factory()))
        self.assertIsNone(outcome.anomaly)
        self.assertEqual(outcome.node_count, 3)
        self.assertEqual(outcome.max_depth, 3)
        self.assertNotIn("SYNTH_SECRET", outcome.xml_text)
        self.assertNotIn("com.secret", outcome.xml_text)
        self.assertNotIn("data-secret", outcome.xml_text)
        self.assertIn('package="com.synthetic.app"', outcome.xml_text)
        self.assertIn('text="TEXT"', outcome.xml_text)
        self.assertIn('content-desc="DESC"', outcome.xml_text)
        root = extract.ET.fromstring(outcome.xml_text)
        frame = list(root)[0]
        linear = list(frame)[0]
        self.assertEqual(linear.tag, "node")
        self.assertEqual(len(list(linear)), 1)
        self.assertEqual(outcome.faults["unknown_attr"], 1)

    def test_resource_alias_preserves_duplicates(self) -> None:
        """同一 resource-id 得到同一别名，不同值得到不同别名。"""
        raw = _xml(
            _node(**{"class": "android.widget.TextView", "resource-id": "com.secret:id/a"}),
            _node(**{"class": "android.widget.TextView", "resource-id": "com.secret:id/a"}),
            _node(**{"class": "android.widget.TextView", "resource-id": "com.secret:id/b"}),
        )
        outcome = extract.sanitize_xml(raw.encode("utf-8"), extract.AliasBook(_alias_factory()))
        root = extract.ET.fromstring(outcome.xml_text)
        aliases = [node.get("resource-id") for node in root]
        self.assertEqual(aliases[0], aliases[1])
        self.assertNotEqual(aliases[0], aliases[2])
        self.assertTrue(all(alias.startswith("res_") for alias in aliases))
        self.assertNotIn("com.secret", outcome.xml_text)

    def test_invalid_bounds_and_boolean_dropped(self) -> None:
        """非法 bounds/布尔值被丢弃并计数，合法值规范化保留。"""
        raw = _xml(
            _node(
                **{
                    "class": "android.widget.TextView",
                    "bounds": "[10,0][5,20]",
                    "clickable": "maybe",
                    "enabled": "TRUE",
                }
            ),
            _node(**{"class": "android.widget.TextView", "bounds": "[0,0][10,20]"}),
        )
        outcome = extract.sanitize_xml(raw.encode("utf-8"), extract.AliasBook(_alias_factory()))
        self.assertEqual(outcome.faults["invalid_bounds"], 1)
        self.assertEqual(outcome.faults["invalid_boolean"], 1)
        root = extract.ET.fromstring(outcome.xml_text)
        first, second = list(root)
        self.assertIsNone(first.get("bounds"))
        self.assertIsNone(first.get("clickable"))
        self.assertEqual(first.get("enabled"), "true")
        self.assertEqual(second.get("bounds"), "[0,0][10,20]")

    def test_unknown_class_and_tag_are_synthetic(self) -> None:
        """未知类名退化为固定 View，未知标签映射为 node 并计数。"""
        raw = (
            '<hierarchy rotation="0"><weird class="com.secret.Widget" '
            'text="SYNTH_SECRET"/></hierarchy>'
        )
        outcome = extract.sanitize_xml(raw.encode("utf-8"), extract.AliasBook(_alias_factory()))
        self.assertEqual(outcome.faults["unknown_class"], 1)
        self.assertEqual(outcome.faults["unknown_tag"], 1)
        self.assertIn('class="android.view.View"', outcome.xml_text)
        self.assertNotIn("com.secret", outcome.xml_text)
        self.assertNotIn("SYNTH_SECRET", outcome.xml_text)
        self.assertIn("<node", outcome.xml_text)

    def test_empty_hierarchy_is_valid_not_anomaly(self) -> None:
        """空 hierarchy 是合法空屏，不应被当作异常代表。"""
        outcome = extract.sanitize_xml(b"<hierarchy/>", extract.AliasBook(_alias_factory()))
        self.assertIsNone(outcome.anomaly)
        self.assertEqual(outcome.node_count, 0)
        self.assertEqual(outcome.xml_text, "<hierarchy/>")

    # ---------- 异常输入 ----------

    def test_dtd_and_entity_rejected(self) -> None:
        """DTD 与实体声明在解析前被拒绝，代表不含原文。"""
        for payload in (
            '<!DOCTYPE hierarchy [<!ENTITY x "SYNTH_SECRET">]><hierarchy><node text="&x;"/></hierarchy>',
            '<!ENTITY x "SYNTH_SECRET"><hierarchy/>',
        ):
            outcome = extract.sanitize_xml(payload.encode("utf-8"), extract.AliasBook(_alias_factory()))
            self.assertEqual(outcome.anomaly, "dtd_or_entity")
            self.assertIn('derived_synthetic_representative="true"', outcome.xml_text)
            self.assertIn('fault="dtd_or_entity"', outcome.xml_text)
            self.assertNotIn("SYNTH_SECRET", outcome.xml_text)
            self.assertNotIn("<!ENTITY", outcome.xml_text)
            extract.ET.fromstring(outcome.xml_text)

    def test_invalid_encoding_rejected(self) -> None:
        """非 UTF-8/UTF-16 字节在读取后、解析前被拒绝。"""
        outcome = extract.sanitize_xml(
            b'<hierarchy><node class="android.view.View"/>\xff</hierarchy>',
            extract.AliasBook(_alias_factory()),
        )
        self.assertEqual(outcome.anomaly, "invalid_encoding")
        self.assertNotIn("\xff", outcome.xml_text)

    def test_malformed_xml_rejected(self) -> None:
        """结构损坏的 XML 只生成固定合成代表。"""
        outcome = extract.sanitize_xml(
            b"<hierarchy><node></hierarchy>", extract.AliasBook(_alias_factory())
        )
        self.assertEqual(outcome.anomaly, "malformed_xml")
        self.assertIn('fault="malformed_xml"', outcome.xml_text)

    def test_too_deep_and_too_many_nodes(self) -> None:
        """深度/节点上限触发纯合成代表，不复制原始子树。"""
        deep = "<hierarchy>" + "<node>" * 5 + "</node>" * 5 + "</hierarchy>"
        with mock.patch.object(extract, "MAX_XML_DEPTH", 2):
            outcome = extract.sanitize_xml(deep.encode("utf-8"), extract.AliasBook(_alias_factory()))
        self.assertEqual(outcome.anomaly, "too_deep")
        wide = _xml(*[_node(**{"class": "android.view.View"}) for _ in range(6)])
        with mock.patch.object(extract, "MAX_XML_NODES", 3):
            outcome = extract.sanitize_xml(wide.encode("utf-8"), extract.AliasBook(_alias_factory()))
        self.assertEqual(outcome.anomaly, "too_many_nodes")
        self.assertIn('derived_synthetic_representative="true"', outcome.xml_text)

    # ---------- 路径、链接与输出边界 ----------

    def test_path_escape_rejected_before_read(self) -> None:
        """越出允许根的路径在读取前被拒绝。"""
        root = self.tmp / "source" / "screenshots"
        root.mkdir(parents=True)
        outside = self.tmp / "outside.xml"
        outside.write_text("<hierarchy/>", encoding="utf-8")
        with self.assertRaises(extract.ExtractionError) as raised:
            extract._assert_within(root, outside)
        self.assertEqual(raised.exception.reason, "path_escape")
        with mock.patch.object(extract, "open", create=True) as opened:
            with self.assertRaises(extract.ExtractionError):
                extract._assert_within(root, outside)
            opened.assert_not_called()

    def test_junction_ancestor_rejected_before_read(self) -> None:
        """源路径中存在的 junction 祖先在 realpath 之前被拒绝。"""
        real = self.tmp / "real"
        (real / "source" / "screenshots").mkdir(parents=True)
        link = self.tmp / "link"
        self._make_junction(link, real)
        repo = _make_repo(self.tmp)
        with self.assertRaises(extract.ExtractionError) as raised:
            extract.run_extraction(link / "source", repo / "data" / "private-regression")
        self.assertEqual(raised.exception.reason, "symlink_rejected")

    def test_junction_child_skipped_without_read(self) -> None:
        """screenshots 内的 junction 目录不会被遍历，也不读取其内容。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "x.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        self._make_junction(source / "screenshots" / "j", outside)
        with mock.patch.object(extract, "_read_xml", side_effect=AssertionError("must not read")):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["discovery"]["xml_candidates"], 0)
        self.assertGreaterEqual(receipt["discovery"]["skipped_symlink"], 1)
        self.assertEqual(receipt["status"], "no_candidates")

    def test_output_overlap_rejected(self) -> None:
        """输出位于源内或源位于输出内都拒绝，不写任何产物。"""
        source = _make_source(self.tmp)
        with self.assertRaises(extract.ExtractionError) as raised:
            extract.run_extraction(source, source / "out")
        self.assertEqual(raised.exception.reason, "output_overlap")

    def test_junction_output_ancestor_rejected(self) -> None:
        """输出根祖先中的 junction 在写入前被拒绝。"""
        real_repo = _make_repo(self.tmp)
        link = self.tmp / "linkrepo"
        self._make_junction(link, real_repo)
        source = _make_source(self.tmp)
        with self.assertRaises(extract.ExtractionError) as raised:
            extract.run_extraction(source, link / "data" / "private-regression")
        self.assertEqual(raised.exception.reason, "symlink_rejected")
        self.assertFalse((real_repo / "data").exists())

    def test_output_overwrite_rejected(self) -> None:
        """已存在的同名批次目录不被覆盖。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(), encoding="utf-8")
        output = repo / "data" / "private-regression"
        output.mkdir(parents=True)
        (output / "b-fixed").mkdir()
        with mock.patch.object(extract, "_new_batch_id", return_value="b-fixed"):
            with self.assertRaises(extract.ExtractionError) as raised:
                extract.run_extraction(source, output)
        self.assertEqual(raised.exception.reason, "output_exists")

    def test_output_not_ignored_rejected(self) -> None:
        """输出根未被 Git 忽略时在写入前拒绝。"""
        repo = _make_repo(self.tmp, ignore="")
        source = _make_source(self.tmp)
        output = repo / "out"
        with self.assertRaises(extract.ExtractionError) as raised:
            extract.run_extraction(source, output)
        self.assertEqual(raised.exception.reason, "output_not_ignored")
        self.assertFalse(output.exists())

    # ---------- 抽取、去重、抽样与 provenance ----------

    def test_source_unchanged_and_git_ignored(self) -> None:
        """抽取前后源字节不变，输出批次被忽略且未跟踪。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        xml_path = source / "screenshots" / "a.xml"
        xml_path.write_text(_xml(_node(**{"class": "android.widget.TextView", "text": "SYNTH_SECRET"})), encoding="utf-8")
        before = xml_path.read_bytes()
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(xml_path.read_bytes(), before)
        self.assertTrue(receipt["safety"]["source_unchanged"])
        self.assertTrue(receipt["safety"]["output_git_ignored"])
        self.assertTrue(receipt["safety"]["output_not_tracked"])
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        ignored = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", "--", str(batch.relative_to(repo))],
            check=False,
        ).returncode
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(batch.relative_to(repo))],
            capture_output=True,
            check=False,
        ).returncode
        self.assertEqual(ignored, 0)
        self.assertNotEqual(tracked, 0)

    def test_no_candidates_reports_missing(self) -> None:
        """空源准确报告无候选与日志缺失，不谎报覆盖。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "no_candidates")
        self.assertEqual(receipt["xml"]["uncovered_reason"], "no_xml_candidates")
        self.assertEqual(receipt["log"]["uncovered_reason"], "log_missing")
        self.assertEqual(receipt["log"]["missing_actions"], list(extract.ACTION_IDS))
        self.assertEqual(receipt["log"]["needs_label"], 0)
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        self.assertEqual(list((batch / "xml").glob("*.xml")), [])
        self.assertEqual(list((batch / "failure_windows").glob("*.json")), [])

    def test_duplicate_xml_dedup(self) -> None:
        """结构相同的 XML 去重，重复计数进入回执。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        body = _xml(_node(**{"class": "android.widget.TextView", "text": "SYNTH_A"}))
        (source / "screenshots" / "a.xml").write_text(body, encoding="utf-8")
        (source / "screenshots" / "b.xml").write_text(body, encoding="utf-8")
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["xml"]["selected"], 1)
        self.assertEqual(receipt["xml"]["duplicates_dropped"], 1)

    def test_provenance_maps_selected_cases_not_read_order(self) -> None:
        """provenance 按 case_id 映射实际选中源，而不是用读取顺序 zip。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        shots = source / "screenshots"
        first = _xml(_node(**{"class": "android.widget.TextView", "text": "SYNTH_A"}))
        (shots / "a.xml").write_text(first, encoding="utf-8")
        (shots / "b.xml").write_text(first, encoding="utf-8")
        (shots / "c.xml").write_text(
            _xml(_node(**{"class": "android.widget.Button", "text": "SYNTH_C"})),
            encoding="utf-8",
        )
        (shots / "d.xml").write_text(
            '<!DOCTYPE hierarchy [<!ENTITY x "y">]><hierarchy/>', encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        provenance = _read_json(batch / "provenance.local.json")
        mapping = {item["case_id"]: item["relative"] for item in provenance["files"]}
        self.assertEqual(mapping["XML-0001"], "screenshots/a.xml")
        self.assertEqual(mapping["XML-0002"], "screenshots/c.xml")
        self.assertNotIn("screenshots/b.xml", mapping.values())
        self.assertNotIn("screenshots/d.xml", mapping.values())

    def test_sampling_cap_and_partial_status(self) -> None:
        """候选超过样本上限时只保留上限份并标记 truncated/partial。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        for index in range(12):
            (source / "screenshots" / f"{index:02d}.xml").write_text(
                _xml(
                    _node(
                        **{
                            "class": "android.view.View",
                            "bounds": f"[0,0][{index + 1},10]",
                        }
                    )
                ),
                encoding="utf-8",
            )
        with mock.patch.object(extract, "MAX_XML_SAMPLES", 5):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["xml"]["selected"], 5)
        self.assertTrue(receipt["xml"]["truncated"])
        self.assertEqual(receipt["status"], "partial")

    def test_oversize_xml_becomes_representative(self) -> None:
        """超限 XML 不读取正文，只生成 oversize 合成代表。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "big.xml").write_text(
            _xml(_node(**{"class": "android.widget.TextView", "text": "SYNTH_BIG"})) * 10,
            encoding="utf-8",
        )
        with mock.patch.object(extract, "MAX_XML_BYTES", 80):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["xml"]["selected"], 0)
        self.assertEqual(receipt["xml"]["representatives"], 1)
        self.assertEqual(receipt["xml"]["faults"]["oversize"], 1)

    # ---------- 失败时序 ----------

    def _run_with_log(
        self,
        lines: list[dict],
        patches: dict | None = None,
        raw_text: str | None = None,
    ) -> tuple[dict, Path]:
        """在合成源中写入 JSONL 并执行抽取。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        if raw_text is None:
            raw_text = "\n".join(json.dumps(line) for line in lines) + "\n"
        (source / "data" / "operations.log").write_text(raw_text, encoding="utf-8")
        stack = contextlib.ExitStack()
        for name, value in (patches or {}).items():
            stack.enter_context(mock.patch.object(extract, name, value))
        with stack:
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        return receipt, repo / "data" / "private-regression" / receipt["batch_id"]

    def test_failure_window_uses_fixed_categories_and_needs_label(self) -> None:
        """已知失败事件映射为固定类别，样本初态一律 needs_label。"""
        receipt, batch = self._run_with_log(
            [
                {"timestamp": "2000-01-01T03:04:05+00:00", "event": "adb_start", "status": "ok"},
                {
                    "timestamp": "2000-01-01T03:04:06+00:00",
                    "event": "executor_error",
                    "reason_code": "device_disconnected",
                    "status": "unknown",
                    "action": "text",
                    "duration_ms": 120,
                },
            ]
        )
        self.assertEqual(receipt["log"]["failure_windows"], 1)
        self.assertEqual(receipt["log"]["needs_label"], 1)
        self.assertEqual(receipt["log"]["failure_categories"]["executor_error"], 1)
        self.assertIn("A01", receipt["log"]["covered_actions"])
        window = _read_json(next((batch / "failure_windows").glob("*.json")))
        self.assertEqual(window["expected"], "needs_label")
        self.assertEqual(window["events"][1]["event"], "executor_error")
        self.assertEqual(window["events"][1]["reason"], "device_disconnected")
        self.assertEqual(window["events"][1]["action"], "A01")
        self.assertEqual(window["events"][1]["values"], {"duration_ms": 120})
        self.assertEqual(window["events"][0]["rel_ms"], 0)

    def test_only_unknown_events_reports_uncovered(self) -> None:
        """只有未知事件时不发明失败窗口，明确报告无证据。"""
        receipt, batch = self._run_with_log(
            [
                {"timestamp": "2000-01-01T03:04:05+00:00", "event": "zzz", "status": "???", "reason": "???"},
                {"timestamp": "2000-01-01T03:04:06+00:00", "event": "yyy"},
            ]
        )
        self.assertEqual(receipt["log"]["failure_windows"], 0)
        self.assertEqual(receipt["log"]["needs_label"], 0)
        self.assertEqual(receipt["log"]["uncovered_reason"], "no_failure_evidence")
        self.assertEqual(receipt["log"]["missing_actions"], list(extract.ACTION_IDS))
        self.assertEqual(list(batch.glob("failure_windows/*.json")), [])

    def test_clock_missing_marked(self) -> None:
        """缺少时间戳时相对时间为 null 并标记 clock_missing。"""
        receipt, batch = self._run_with_log(
            [{"event": "executor_error", "reason_code": "timeout"}]
        )
        window = _read_json(next((batch / "failure_windows").glob("*.json")))
        self.assertTrue(window["clock_missing"])
        self.assertIsNone(window["events"][0]["rel_ms"])
        self.assertEqual(receipt["log"]["clock_missing_windows"], 1)

    def test_out_of_order_not_silently_fixed(self) -> None:
        """乱序时间保留负相对值并显式标记，不排序修复。"""
        receipt, batch = self._run_with_log(
            [
                {"timestamp": "2000-01-01T03:04:10+00:00", "event": "executor_error"},
                {"timestamp": "2000-01-01T03:04:05+00:00", "event": "read_incomplete"},
                {"timestamp": "2000-01-01T03:04:20+00:00", "event": "needs_probe"},
            ]
        )
        window = _read_json(next((batch / "failure_windows").glob("*.json")))
        relatives = [event["rel_ms"] for event in window["events"]]
        self.assertEqual(relatives, [0, -5000, 10000])
        self.assertTrue(window["out_of_order"])
        self.assertFalse(window["monotonic"])
        self.assertEqual(receipt["log"]["out_of_order_windows"], 1)

    def test_truncated_log_marked(self) -> None:
        """日志行数上限触发时窗口与回执都标记 truncated。"""
        lines = [
            {"timestamp": f"2000-01-01T03:04:{index:02d}+00:00", "event": "executor_error"}
            for index in range(6)
        ]
        receipt, batch = self._run_with_log(lines, {"MAX_LOG_LINES": 2})
        self.assertTrue(receipt["log"]["truncated"])
        self.assertEqual(receipt["status"], "partial")
        for path in (batch / "failure_windows").glob("*.json"):
            self.assertTrue(_read_json(path)["truncated"])

    def test_old_oracle_fields_not_trusted(self) -> None:
        """旧日志里的 result/expected/status=PASS 不被当作答案或状态。"""
        receipt, batch = self._run_with_log(
            [
                {
                    "timestamp": "2000-01-01T03:04:05+00:00",
                    "event": "executor_error",
                    "result": "PASS",
                    "expected": "PASS",
                    "status": "PASS",
                }
            ]
        )
        window = _read_json(next((batch / "failure_windows").glob("*.json")))
        self.assertEqual(window["expected"], "needs_label")
        self.assertEqual(window["events"][0]["status"], "unknown")
        self.assertNotIn("PASS", json.dumps(window))
        self.assertEqual(receipt["log"]["needs_label"], 1)

    def test_read_budget_is_bounded(self) -> None:
        """读取与校验都受预算限制，回执给出实际用量。"""
        long_line = {"event": "executor_error", "note": "SYNTH_" + "x" * 200}
        receipt, _ = self._run_with_log([long_line], {"MAX_LOG_LINE_BYTES": 50})
        self.assertGreaterEqual(receipt["log"]["invalid_lines"], 1)
        self.assertLessEqual(receipt["budget"]["xml_read_used"], receipt["budget"]["xml_read_limit"])
        self.assertLessEqual(receipt["budget"]["verify_xml_used"], receipt["budget"]["verify_xml_limit"])
        self.assertLessEqual(receipt["budget"]["log_read_used"], receipt["budget"]["log_read_limit"])
        self.assertLessEqual(receipt["budget"]["verify_log_used"], receipt["budget"]["verify_log_limit"])

    # ---------- 安全回执、泄露与错误路径 ----------

    def test_stdout_and_outputs_do_not_leak_synthetic_secrets(self) -> None:
        """stdout/派生正文不出现合成秘密、路径、绝对日期或 traceback。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        secret_xml = _xml(
            _node(
                **{
                    "class": "com.secret.Cls",
                    "text": "SYNTH_XML_TEXT",
                    "content-desc": "SYNTH_XML_DESC",
                    "package": "com.secret.pkg",
                    "resource-id": "com.secret:id/SYNTH_RES",
                    "bounds": "[0,0][10,10]",
                }
            )
        )
        (source / "screenshots" / "secret.xml").write_text(secret_xml, encoding="utf-8")
        (source / "data" / "operations.log").write_text(
            json.dumps(
                {
                    "timestamp": "2000-01-01T03:04:05+00:00",
                    "event": "executor_error",
                    "note": "SYNTH_LOG_VALUE",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        code, out, err = _run_main(
            ["--source", str(source), "--output", str(repo / "data" / "private-regression")]
        )
        self.assertEqual(code, 0)
        combined = out + err
        for secret in (
            "SYNTH_XML_TEXT",
            "SYNTH_XML_DESC",
            "SYNTH_RES",
            "com.secret",
            "SYNTH_LOG_VALUE",
            "2000-01-01",
            str(source),
            "Traceback",
        ):
            self.assertNotIn(secret, combined)
        batch = repo / "data" / "private-regression" / json.loads(out)["batch_id"]
        for path in batch.rglob("*"):
            if path.is_file() and path.name != "provenance.local.json":
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("SYNTH_XML_TEXT", text)
                self.assertNotIn("SYNTH_LOG_VALUE", text)
                self.assertNotIn("2000-01-01", text)

    def test_error_paths_do_not_leak(self) -> None:
        """参数错误与缺失源只输出固定错误码，不回显路径或 traceback。"""
        secret = "SYNTH_SECRET_PATH"
        code, out, err = _run_main(
            ["--source", str(self.tmp / secret / "missing"), "--output", str(self.tmp / "out")]
        )
        self.assertEqual(code, 2)
        combined = out + err
        self.assertIn('"error_reason": "source_missing"', combined)
        self.assertNotIn(secret, combined)
        self.assertNotIn("Traceback", combined)
        code, out, err = _run_main(["--bogus", secret])
        self.assertEqual(code, 2)
        self.assertIn('"error_reason": "usage_error"', out + err)
        self.assertNotIn(secret, out + err)

    def test_source_change_sets_error_and_nonzero_exit(self) -> None:
        """抽取期间源被改动必须导致 error 与非零退出码。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        xml_path = source / "screenshots" / "a.xml"
        xml_path.write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        original = extract.sanitize_xml

        def tamper(raw: bytes, aliases: extract.AliasBook) -> extract.SanitizeOutcome:
            """读取后改写源文件，模拟并发修改。"""
            xml_path.write_text("<hierarchy/>", encoding="utf-8")
            return original(raw, aliases)

        with mock.patch.object(extract, "sanitize_xml", side_effect=tamper):
            code, out, _ = _run_main(
                ["--source", str(source), "--output", str(repo / "data" / "private-regression")]
            )
        receipt = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_reason"], "source_changed")
        self.assertFalse(receipt["safety"]["source_unchanged"])

    def test_receipt_has_no_forbidden_keys(self) -> None:
        """安全回执与 manifest 不含路径/哈希/时间等禁字段。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        manifest = _read_json(batch / "manifest.json")
        self.assertEqual(_keys([receipt, manifest]) & extract.FORBIDDEN_OUTPUT_KEYS, set())

    def test_manifest_and_windows_all_needs_label(self) -> None:
        """所有派生样本与窗口的 expected 都是 needs_label。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        (source / "screenshots" / "bad.xml").write_text(
            '<!DOCTYPE hierarchy [<!ENTITY x "y">]><hierarchy/>', encoding="utf-8"
        )
        (source / "data" / "operations.log").write_text(
            json.dumps({"event": "executor_error"}) + "\n", encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        manifest = _read_json(batch / "manifest.json")
        self.assertTrue(manifest["samples"])
        self.assertTrue(all(sample["expected"] == "needs_label" for sample in manifest["samples"]))
        for path in (batch / "failure_windows").glob("*.json"):
            self.assertEqual(_read_json(path)["expected"], "needs_label")

    def test_window_dedup_distinguishes_time_order(self) -> None:
        """结构相同但时序不同的两个窗口不能被去重吞掉乱序证据。"""
        pattern = [
            "executor_error",
            "read_incomplete",
            "executor_error",
            "needs_probe",
            "executor_error",
            "read_incomplete",
            "executor_error",
            "needs_probe",
            "executor_error",
        ]
        base = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        lines: list[dict] = []

        def add(events: list[str], seconds: list[int]) -> None:
            """追加一组带绝对秒偏移的合成事件。"""
            for event, second in zip(events, seconds):
                lines.append(
                    {
                        "timestamp": (base + timedelta(seconds=second)).isoformat(),
                        "event": event,
                    }
                )

        add(["adb_start"] * 4, [0, 1, 2, 3])
        add(pattern, [4, 5, 6, 7, 8, 9, 10, 11, 12])
        add(["adb_start"] * 4, [13, 14, 15, 16])
        add(["adb_start"] * 4, [17, 18, 19, 20])
        add(pattern, [104, 105, 106, 107, 108, 107, 110, 111, 112])
        add(["adb_start"] * 4, [113, 114, 115, 116])
        receipt, batch = self._run_with_log(lines)
        windows = [_read_json(path) for path in (batch / "failure_windows").glob("*.json")]
        self.assertEqual(len(windows), 2)
        self.assertEqual(receipt["log"]["out_of_order_windows"], 1)
        self.assertEqual(sum(1 for window in windows if window["out_of_order"]), 1)
        self.assertNotEqual(windows[0]["events"], windows[1]["events"])

    def test_long_cluster_keeps_tail_category(self) -> None:
        """长失败簇分片后保留尾部新类别，并标记窗口裁切。"""
        lines = [
            {
                "timestamp": (datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat(),
                "event": "executor_error" if index < 49 else "delete_unverified",
            }
            for index in range(50)
        ]
        receipt, batch = self._run_with_log(lines)
        windows = [_read_json(path) for path in (batch / "failure_windows").glob("*.json")]
        self.assertEqual(receipt["log"]["failure_categories"]["cleanup_unverified"], 1)
        self.assertEqual(receipt["log"]["missing_window_categories"], [])
        self.assertTrue(receipt["log"]["window_truncated"])
        self.assertEqual(receipt["status"], "partial")
        self.assertIn(
            "cleanup_unverified",
            {event["event"] for window in windows for event in window["events"]},
        )

    def test_long_physical_line_counts_toward_line_cap(self) -> None:
        """超长物理行也计入行数上限，不能借无效行绕过。"""
        long_line = json.dumps({"event": "x", "note": "y" * 200})
        valid_line = json.dumps({"event": "executor_error"})
        receipt, _ = self._run_with_log(
            [],
            {"MAX_LOG_LINE_BYTES": 50, "MAX_LOG_LINES": 1},
            raw_text=long_line + "\n" + valid_line + "\n",
        )
        self.assertTrue(receipt["log"]["truncated"])
        self.assertEqual(receipt["log"]["lines"], 2)
        self.assertGreaterEqual(receipt["log"]["invalid_lines"], 1)
        self.assertEqual(receipt["log"]["failure_windows"], 0)
        self.assertEqual(receipt["log"]["uncovered_reason"], "truncated_before_evidence")

    def test_nonfinite_numbers_ignored(self) -> None:
        """NaN/Infinity 数值被丢弃，不会中断整批抽取。"""
        line = (
            '{"event": "executor_error", "duration_ms": NaN, '
            '"waited_ms": Infinity, "attempt": 1}'
        )
        receipt, batch = self._run_with_log([], {}, raw_text=line + "\n")
        self.assertNotEqual(receipt["status"], "error")
        window = _read_json(next((batch / "failure_windows").glob("*.json")))
        self.assertEqual(window["events"][0]["values"], {"attempt": 1})

    # ---------- 本机验证器 ----------

    def test_validator_accepts_valid_batch(self) -> None:
        """本机验证器对正常批次返回 valid。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "valid", result)
        self.assertTrue(all(result["checks"].values()))

    def test_validator_rejects_xml_character_data(self) -> None:
        """派生 XML 节点正文/tail 只允许空白，隐藏原文必须被拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        (batch / "xml" / "XML-0001.xml").write_text(
            '<hierarchy><node class="android.view.View">SYNTH_SECRET_CHARACTER_DATA</node></hierarchy>',
            encoding="utf-8",
        )
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["xml_safety_ok"])

    def test_validator_rejects_arbitrary_free_fields(self) -> None:
        """回执/窗口出现未知自由字段时验证器拒绝，而非只查少数禁用键。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        (source / "data" / "operations.log").write_text(
            json.dumps({"event": "executor_error"}) + "\n", encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        window_path = next((batch / "failure_windows").glob("*.json"))
        window = _read_json(window_path)
        window["arbitrary_note"] = "SYNTH_FREE_FIELD"
        window_path.write_text(json.dumps(window), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertFalse(result["checks"]["windows_schema_ok"])
        window.pop("arbitrary_note")
        window_path.write_text(json.dumps(window), encoding="utf-8")
        receipt_path = batch / "safe_receipt.json"
        payload = _read_json(receipt_path)
        payload["arbitrary_note"] = "SYNTH_FREE_FIELD"
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertFalse(result["checks"]["receipt_schema_ok"])

    def test_validator_rejects_junction_batch(self) -> None:
        """验证器在读取前拒绝 batch 路径中的 junction。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        link = self.tmp / "batch-link"
        self._make_junction(link, batch)
        result = extract.validate_batch(link)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["batch_path_safe"])

    def test_validator_rejects_tampered_window(self) -> None:
        """把窗口 expected 改为 PASS 后验证器必须拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "data" / "operations.log").write_text(
            json.dumps({"timestamp": "2000-01-01T03:04:05+00:00", "event": "executor_error"}) + "\n",
            encoding="utf-8",
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        window_path = next((batch / "failure_windows").glob("*.json"))
        window = _read_json(window_path)
        window["expected"] = "PASS"
        window_path.write_text(json.dumps(window), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["windows_schema_ok"])

    def test_validator_rejects_safety_false_and_bad_xml(self) -> None:
        """验证器读取 safety 与 XML 内容，拒绝 false 安全条件与非白名单类。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        (batch / "xml" / "XML-0001.xml").write_text(
            '<hierarchy><node class="com.secret.Cls" text="SECRET"/></hierarchy>', encoding="utf-8"
        )
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["xml_safety_ok"])
        receipt_path = batch / "safe_receipt.json"
        payload = _read_json(receipt_path)
        payload["safety"]["source_unchanged"] = False
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertFalse(result["checks"]["safety_all_true"])

    def test_validator_rejects_forbidden_receipt_key(self) -> None:
        """回执加入 source_path 之类禁字段时验证器拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(_xml(), encoding="utf-8")
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        receipt_path = batch / "safe_receipt.json"
        payload = _read_json(receipt_path)
        payload["source_path"] = "SYNTH_LEAK"
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertFalse(result["checks"]["no_forbidden_keys"])


if __name__ == "__main__":
    unittest.main()
