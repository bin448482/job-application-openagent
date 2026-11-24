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


def _real_git_failure_command(repo: Path, fail_calls: set[int]) -> tuple[object, dict]:
    """包装真实 Git 调用，只对指定 ls-files 次数短暂破坏临时 index。"""
    real_run = extract.subprocess.run
    index = repo / ".git" / "index"
    state: dict[str, object] = {"ls_calls": 0, "codes": []}

    def command(arguments: list[str], *args: object, **kwargs: object) -> object:
        """在目标 ls-files 调用前写入真实坏 index，调用后恢复。"""
        if "ls-files" in arguments:
            state["ls_calls"] = int(state["ls_calls"]) + 1
            if int(state["ls_calls"]) in fail_calls:
                saved = index.read_bytes() if index.exists() else None
                index.write_bytes(b"SYNTHETIC_CORRUPT_INDEX")
                try:
                    result = real_run(arguments, *args, **kwargs)
                    state["codes"].append(result.returncode)
                    return result
                finally:
                    if saved is None:
                        index.unlink()
                    else:
                        index.write_bytes(saved)
        return real_run(arguments, *args, **kwargs)

    return command, state



class _CountingScandir:
    """包装真实 scandir，记录实际产出条目数与被关闭状态。"""

    def __init__(self, real: object) -> None:
        """保存底层迭代器并初始化计数。"""
        self._real = real
        self.yielded = 0
        self.closed = False

    def __iter__(self) -> "_CountingScandir":
        """返回自身作为迭代器。"""
        return self

    def __next__(self) -> object:
        """读取下一个条目并累计实际消费数。"""
        entry = next(self._real)
        self.yielded += 1
        return entry

    def close(self) -> None:
        """关闭底层 scandir 迭代器并记录。"""
        if not self.closed:
            self.closed = True
            self._real.close()


def _scandir_factory() -> tuple[object, list[tuple[Path, _CountingScandir]]]:
    """返回可替换 os.scandir 的计数工厂与探针列表。"""
    real = extract.os.scandir
    probes: list[tuple[Path, _CountingScandir]] = []

    def factory(path: Path) -> _CountingScandir:
        """为一次 scandir 调用创建计数包装。"""
        probe = _CountingScandir(real(path))
        probes.append((Path(path), probe))
        return probe

    return factory, probes


class _FakeDirEntry:
    """测试专用的最小 DirEntry，只用于把超深目录固定为首个条目。"""

    def __init__(self, path: Path) -> None:
        """保存真实路径。"""
        self.path = str(path)
        self.name = path.name

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        """把目标目录声明为目录。"""
        return True

    def is_file(self, follow_symlinks: bool = True) -> bool:
        """目标不是普通文件。"""
        return False


class _FirstEntryScandir:
    """先产出指定目录条目，其余真实条目仅在继续迭代时消费。"""

    def __init__(self, forced: Path, remaining: object) -> None:
        """保存超深目录与真实剩余迭代器。"""
        self._forced = forced
        self._remaining = remaining
        self.yielded = 0
        self.remaining_yielded = 0
        self.closed = False

    def __iter__(self) -> "_FirstEntryScandir":
        """返回自身。"""
        return self

    def __next__(self) -> object:
        """首发强制条目，之后才消费真实目录。"""
        if self.yielded == 0:
            self.yielded = 1
            return _FakeDirEntry(self._forced)
        entry = next(self._remaining)
        self.remaining_yielded += 1
        self.yielded += 1
        return entry

    def close(self) -> None:
        """关闭真实剩余迭代器并记录。"""
        if not self.closed:
            self.closed = True
            self._remaining.close()




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

    # ---------- 独立审核 B-1：预算命中必须 truncated/partial ----------

    def test_oversize_budget_marks_partial_and_validates(self) -> None:
        """单文件超限时 xml.truncated=true、status=partial，且代表计数真实。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "big.xml").write_text(
            _xml(_node(**{"class": "android.widget.TextView", "text": "SYNTH_BIG"})) * 10,
            encoding="utf-8",
        )
        with mock.patch.object(extract, "MAX_XML_BYTES", 80):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["xml"]["truncated"])
        self.assertEqual(receipt["xml"]["faults"]["oversize"], 1)
        self.assertEqual(receipt["xml"]["representatives"], 1)
        self.assertTrue(receipt["safety"]["source_unchanged"])
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        self.assertEqual(extract.validate_batch(batch)["status"], "valid")

    def test_too_deep_budget_marks_partial(self) -> None:
        """深度超限时回执必须 partial，不能只记代表计数。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        deep = "<hierarchy>" + "<node>" * 5 + "</node>" * 5 + "</hierarchy>"
        (source / "screenshots" / "deep.xml").write_text(deep, encoding="utf-8")
        with mock.patch.object(extract, "MAX_XML_DEPTH", 2):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["xml"]["truncated"])
        self.assertEqual(receipt["xml"]["faults"]["too_deep"], 1)

    def test_too_many_nodes_budget_marks_partial(self) -> None:
        """节点数超限时回执必须 partial，并保留真实故障计数。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        wide = _xml(*[_node(**{"class": "android.view.View"}) for _ in range(5)])
        (source / "screenshots" / "wide.xml").write_text(wide, encoding="utf-8")
        with mock.patch.object(extract, "MAX_XML_NODES", 2):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["xml"]["truncated"])
        self.assertEqual(receipt["xml"]["faults"]["too_many_nodes"], 1)

    def test_budget_boundary_negative_stays_ok(self) -> None:
        """未命中预算的普通批次仍为 ok 且 truncated=false。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        with mock.patch.object(extract, "MAX_XML_BYTES", 4096):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "ok")
        self.assertFalse(receipt["xml"]["truncated"])

    # ---------- 独立审核 B-2：读取期间源变化必须 error/退出 2 ----------

    def test_growth_after_stat_sets_source_changed_error(self) -> None:
        """stat 后增长不能被降级为 oversize，必须 source_changed 且退出 2。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        target = source / "screenshots" / "grow.xml"
        target.write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        original = extract._read_xml

        def growing(path: Path, root: Path, budget: extract.ReadBudget, size: int):
            """在 stat 与读取之间向目标追加合成字节。"""
            if path == target:
                with path.open("ab") as handle:
                    handle.write(b"<!-- SYNTH_GROWTH -->")
            return original(path, root, budget, size)

        with mock.patch.object(extract, "_read_xml", side_effect=growing):
            code, out, _ = _run_main(
                ["--source", str(source), "--output", str(repo / "data" / "private-regression")]
            )
        receipt = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_reason"], "source_changed")
        self.assertFalse(receipt["safety"]["source_unchanged"])
        self.assertEqual(receipt["xml"]["faults"]["oversize"], 0)

    def test_shrink_during_read_sets_source_changed(self) -> None:
        """读取期间缩短同样必须按源改变失败，而不是静默跳过。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        target = source / "screenshots" / "shrink.xml"
        target.write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        original = extract._read_xml

        def shrinking(path: Path, root: Path, budget: extract.ReadBudget, size: int):
            """在读取前清空目标，模拟并发截断。"""
            if path == target:
                path.write_bytes(b"")
            return original(path, root, budget, size)

        with mock.patch.object(extract, "_read_xml", side_effect=shrinking):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_reason"], "source_changed")
        self.assertFalse(receipt["safety"]["source_unchanged"])

    def test_replacement_after_discovery_sets_source_changed(self) -> None:
        """发现后同大小替换也必须在结束复核中判为源改变。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        target = source / "screenshots" / "replace.xml"
        target.write_text(_xml(_node(**{"class": "android.view.View"})), encoding="utf-8")
        original = extract._read_xml

        def replacing(path: Path, root: Path, budget: extract.ReadBudget, size: int):
            """写入等长但不同内容，并强制不同 mtime。"""
            if path == target:
                path.write_text(
                    _xml(_node(**{"class": "android.view.VieW"})), encoding="utf-8"
                )
                stamp = target.stat().st_mtime_ns + 1_000_000_000
                os.utime(target, ns=(stamp, stamp))
            return original(path, root, budget, size)

        with mock.patch.object(extract, "_read_xml", side_effect=replacing):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_reason"], "source_changed")
        self.assertFalse(receipt["safety"]["source_unchanged"])

    def test_discovery_oversize_is_not_source_change(self) -> None:
        """发现阶段已知超限只算预算截断，不误报源改变。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "big.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})) * 10, encoding="utf-8"
        )
        with mock.patch.object(extract, "MAX_XML_BYTES", 50):
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["safety"]["source_unchanged"])

    # ---------- 独立审核 B-3：manifest 唯一性与 XML 自由文本通道 ----------

    def test_validator_rejects_duplicate_manifest_case_id(self) -> None:
        """重复 sanitized case_id 必须被拒绝，不能靠集合折叠掩盖文件数不足。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        manifest = _read_json(batch / "manifest.json")
        manifest["samples"].append(json.loads(json.dumps(manifest["samples"][0])))
        (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        receipt_payload = _read_json(batch / "safe_receipt.json")
        receipt_payload["xml"]["selected"] = 2
        (batch / "safe_receipt.json").write_text(json.dumps(receipt_payload), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["manifest_schema_ok"])
        self.assertFalse(result["checks"]["manifest_entries_ok"])

    def test_validator_rejects_duplicate_representative_fault(self) -> None:
        """同一故障类别出现两个代表条目必须被拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "bad.xml").write_text(
            '<!DOCTYPE hierarchy [<!ENTITY x "y">]><hierarchy/>', encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        manifest = _read_json(batch / "manifest.json")
        duplicate = next(
            sample for sample in manifest["samples"] if sample["kind"].startswith("derived")
        )
        manifest["samples"].append(json.loads(json.dumps(duplicate)))
        (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["manifest_schema_ok"])

    def test_validator_rejects_xml_comment_channel(self) -> None:
        """派生 XML 注释/处理指令/CDATA 中的自由文本必须在解析前被拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        payloads = (
            '<hierarchy><!-- SYNTH_SECRET_COMMENT --><node class="android.view.View"/></hierarchy>',
            '<hierarchy><?target SYNTH_SECRET_PI?><node class="android.view.View"/></hierarchy>',
            '<hierarchy><![CDATA[SYNTH_SECRET_CDATA]]><node class="android.view.View"/></hierarchy>',
        )
        for payload in payloads:
            (batch / "xml" / "XML-0001.xml").write_text(payload, encoding="utf-8")
            result = extract.validate_batch(batch)
            self.assertEqual(result["status"], "invalid")
            self.assertFalse(result["checks"]["xml_safety_ok"])

    def test_validator_rejects_xml_doctype_and_entity(self) -> None:
        """派生 XML 的 DTD/实体必须在解析前被拒绝，不能靠默认解析器展开。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        (batch / "xml" / "XML-0001.xml").write_text(
            '<!DOCTYPE hierarchy [<!ENTITY t "TEXT">]>'
            '<hierarchy><node class="android.view.View" text="&t;"/></hierarchy>',
            encoding="utf-8",
        )
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["xml_safety_ok"])

    def test_sanitizer_rejects_comment_and_processing_instruction(self) -> None:
        """抽取器同样在解析前拒绝注释/处理指令，代表不含原文。"""
        for payload, fault in (
            ('<hierarchy><!-- SYNTH_SECRET --><node/></hierarchy>', "unsafe_markup"),
            ('<?target SYNTH_SECRET?><hierarchy><node/></hierarchy>', "unsafe_markup"),
        ):
            outcome = extract.sanitize_xml(
                payload.encode("utf-8"), extract.AliasBook(_alias_factory())
            )
            self.assertEqual(outcome.anomaly, fault)
            self.assertNotIn("SYNTH_SECRET", outcome.xml_text)

    # ---------- 独立审核 N-1：状态与输出/安全语义自洽 ----------

    def test_validator_rejects_error_with_all_safety_true(self) -> None:
        """status=error 但安全布尔全 true 的矛盾回执必须被拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        payload = _read_json(batch / "safe_receipt.json")
        payload["status"] = "error"
        payload["error_reason"] = "source_changed"
        (batch / "safe_receipt.json").write_text(json.dumps(payload), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["status_consistent"])

    def test_validator_rejects_ok_without_output(self) -> None:
        """没有任何样本/窗口却声明 status=ok 必须被拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        payload = _read_json(batch / "safe_receipt.json")
        payload["status"] = "ok"
        (batch / "safe_receipt.json").write_text(json.dumps(payload), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["status_consistent"])

    def test_validator_rejects_partial_without_truncation(self) -> None:
        """没有截断/未覆盖原因却声明 partial 必须被拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        payload = _read_json(batch / "safe_receipt.json")
        payload["status"] = "partial"
        (batch / "safe_receipt.json").write_text(json.dumps(payload), encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["status_consistent"])

    # ---------- 独立审核 N-2：批次目录边界与总文件预算 ----------

    def test_validator_rejects_extra_nested_and_nonjson_files(self) -> None:
        """未声明的嵌套 XML/自由文本文件即使不被 glob 读取也必须拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        (batch / "xml" / "extra").mkdir()
        (batch / "xml" / "extra" / "raw.xml").write_text("<hierarchy/>", encoding="utf-8")
        (batch / "failure_windows" / "notes.txt").write_text(
            "SYNTH_SECRET_FREE_TEXT", encoding="utf-8"
        )
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["batch_layout_ok"])

    def test_validator_rejects_unknown_top_level_file(self) -> None:
        """批次根目录出现未声明文件时验证器拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        (batch / "notes.txt").write_text("SYNTH_SECRET", encoding="utf-8")
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["batch_layout_ok"])

    def test_validator_file_budget_is_total_not_per_category(self) -> None:
        """文件数上限按批次总量计算，而不是 XML/窗口各自放宽。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        (source / "data" / "operations.log").write_text(
            json.dumps({"event": "executor_error"}) + "\n", encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        with mock.patch.object(extract, "MAX_VALIDATE_FILES", 4):
            limited = extract.validate_batch(batch)
        self.assertEqual(limited["status"], "invalid")
        with mock.patch.object(extract, "MAX_VALIDATE_FILES", 5):
            exact = extract.validate_batch(batch)
        self.assertEqual(exact["status"], "valid", exact)

    def test_validator_rejects_link_inside_batch(self) -> None:
        """批次内新增 junction 必须在枚举时拒绝，不能跟随到源外。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        outside = self.tmp / "outside-windows"
        outside.mkdir()
        self._make_junction(batch / "failure_windows" / "linked", outside)
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["batch_layout_ok"])

    # ---------- 独立审核 N-3/N-4：日志竞态与未知标签预扫 ----------

    def test_log_read_rechecks_boundary_and_link(self) -> None:
        """发现后把日志替换为 junction 时读取前拒绝，且不打开源外文件。"""
        source = _make_source(self.tmp)
        log = source / "data" / "operations.log"
        log.write_text(json.dumps({"event": "executor_error"}) + "\n", encoding="utf-8")
        discovery = extract.discover_source(source)
        self.assertTrue(discovery.log_present)
        outside = self.tmp / "outside-log"
        outside.mkdir()
        (outside / "operations.log").write_text(
            json.dumps({"event": "executor_error", "note": "SYNTH_SECRET"}) + "\n",
            encoding="utf-8",
        )
        log.unlink()
        self._make_junction(log, outside)
        with mock.patch.object(
            extract, "open", create=True, side_effect=AssertionError("must not open")
        ):
            with self.assertRaises(extract.ExtractionError) as raised:
                extract._read_log_events(
                    discovery.log_path,
                    source / "data",
                    extract.ReadBudget(extract.MAX_LOG_BYTES),
                )
        self.assertEqual(raised.exception.reason, "symlink_rejected")

    def test_prescan_blocks_deep_unknown_tags_before_parsing(self) -> None:
        """未知标签的深树在解析前触发 too_deep，不进入 ElementTree。"""
        deep = "<hierarchy>" + "<weird>" * 5 + "</weird>" * 5 + "</hierarchy>"
        with mock.patch.object(extract, "MAX_XML_DEPTH", 2), mock.patch.object(
            extract.ET, "fromstring", side_effect=AssertionError("must not parse")
        ):
            outcome = extract.sanitize_xml(
                deep.encode("utf-8"), extract.AliasBook(_alias_factory())
            )
        self.assertEqual(outcome.anomaly, "too_deep")

    def test_prescan_blocks_wide_unknown_tags_before_parsing(self) -> None:
        """未知标签的宽树在解析前触发 too_many_nodes，不进入 ElementTree。"""
        wide = "<hierarchy>" + "<weird/>" * 5 + "</hierarchy>"
        with mock.patch.object(extract, "MAX_XML_NODES", 2), mock.patch.object(
            extract.ET, "fromstring", side_effect=AssertionError("must not parse")
        ):
            outcome = extract.sanitize_xml(
                wide.encode("utf-8"), extract.AliasBook(_alias_factory())
            )
        self.assertEqual(outcome.anomaly, "too_many_nodes")

    # ---------- 独立审核 N-5/N-6：provenance 与安全布尔语义 ----------

    def test_provenance_is_local_absolute_and_not_for_model(self) -> None:
        """provenance 只在批次目录保存绝对 source_root，且 not_for_model=true。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        provenance = _read_json(batch / "provenance.local.json")
        self.assertIs(provenance["not_for_model"], True)
        self.assertEqual(provenance["source_root"], str(source.resolve()))
        self.assertNotIn("source_root", receipt)
        self.assertNotIn(str(source), json.dumps(receipt))
        self.assertNotIn(str(source), (batch / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(extract.validate_batch(batch)["status"], "valid")

    # ---------- 复审阻塞：log-only 生成/验证互斥与状态组合 ----------

    def test_log_only_window_is_partial_and_both_cli_entries_agree(self) -> None:
        """只有日志失败窗口时抽取 partial、验证 valid，两个 CLI 入口一致。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "data" / "operations.log").write_text(
            json.dumps({"event": "executor_error"}) + "\n", encoding="utf-8"
        )
        output = repo / "data" / "private-regression"
        code, out, _ = _run_main(["--source", str(source), "--output", str(output)])
        receipt = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["xml"]["uncovered_reason"], "no_xml_candidates")
        batch = output / receipt["batch_id"]
        windows = list((batch / "failure_windows").glob("*.json"))
        self.assertEqual(len(windows), 1)
        self.assertEqual(_read_json(windows[0])["expected"], "needs_label")
        validate_code, validate_out, _ = _run_main(["--validate", str(batch)])
        validation = json.loads(validate_out)
        self.assertEqual(validate_code, 0)
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(validation["checks"]["status_consistent"])

    def test_status_combinations_follow_documented_coverage_rules(self) -> None:
        """XML-only/无失败日志为软原因 ok；无输出 no_candidates；预算截断 partial。"""
        cases = (
            ("xml_only", {"screenshots/a.xml": _xml(_node(**{"class": "android.view.View"}))}, None, "ok", "log_missing", "valid"),
            (
                "xml_no_failure",
                {
                    "screenshots/a.xml": _xml(_node(**{"class": "android.view.View"})),
                    "data/operations.log": json.dumps({"event": "task_started"}) + "\n",
                },
                None,
                "ok",
                "no_failure_evidence",
                "valid",
            ),
            ("empty", {}, None, "no_candidates", "log_missing", "valid"),
            (
                "log_truncated_no_window",
                {"data/operations.log": json.dumps({"event": "executor_error"}) + "\n"},
                {"MAX_LOG_LINES": 0},
                "no_candidates",
                "truncated_before_evidence",
                "valid",
            ),
            (
                "xml_budget_truncated",
                {"screenshots/a.xml": _xml(_node(**{"class": "android.view.View"})) * 10},
                {"MAX_XML_BYTES": 50},
                "partial",
                "log_missing",
                "valid",
            ),
        )
        for name, files, patches, expected_status, expected_log_uncovered, expected_validation in cases:
            with self.subTest(case=name):
                root = self.tmp / name
                root.mkdir()
                repo = _make_repo(root)
                source = _make_source(root)
                for relative, content in files.items():
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                stack = contextlib.ExitStack()
                for key, value in (patches or {}).items():
                    stack.enter_context(mock.patch.object(extract, key, value))
                with stack:
                    receipt = extract.run_extraction(
                        source, repo / "data" / "private-regression"
                    )
                self.assertEqual(receipt["status"], expected_status)
                self.assertEqual(
                    receipt["log"]["uncovered_reason"], expected_log_uncovered
                )
                batch = repo / "data" / "private-regression" / receipt["batch_id"]
                self.assertEqual(
                    extract.validate_batch(batch)["status"], expected_validation
                )

    # ---------- 复审 B-2：日志发现期 stat 与 pre-open/结束复核 ----------

    def test_log_content_change_after_discovery_sets_error_and_exit_two(self) -> None:
        """日志在发现后增长/缩短/替换必须 source_changed、safety=false、退出 2。"""
        mutations = {
            "growth": lambda path: path.open("ab").write(
                (json.dumps({"event": "read_incomplete"}) + "\n").encode("utf-8")
            ),
            "shrink": lambda path: path.write_bytes(b""),
            "replacement": lambda path: (
                path.write_text(
                    json.dumps({"event": "executor_error", "reason": "timeout"}) + "\n",
                    encoding="utf-8",
                ),
                os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2),
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                root = self.tmp / ("log-change-" + name)
                root.mkdir()
                repo = _make_repo(root)
                source = _make_source(root)
                log = source / "data" / "operations.log"
                log.write_text(json.dumps({"event": "executor_error"}) + "\n", encoding="utf-8")
                original = extract._read_log_events

                def wrapper(path: Path, root_dir: Path, budget: extract.ReadBudget, mutate=mutate, original=original):
                    """在读取窗口内改动日志，模拟发现后变化。"""
                    if path == log:
                        mutate(path)
                    return original(path, root_dir, budget)

                with mock.patch.object(extract, "_read_log_events", side_effect=wrapper):
                    code, out, _ = _run_main(
                        ["--source", str(source), "--output", str(repo / "data" / "private-regression")]
                    )
                receipt = json.loads(out)
                self.assertEqual(code, 2)
                self.assertEqual(receipt["status"], "error")
                self.assertEqual(receipt["error_reason"], "source_changed")
                self.assertFalse(receipt["safety"]["source_unchanged"])

    def test_log_change_after_discovery_blocks_preopen_read(self) -> None:
        """发现后 pre-open stat 不一致时不得再打开或读取日志。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        log = source / "data" / "operations.log"
        log.write_text(json.dumps({"event": "executor_error"}) + "\n", encoding="utf-8")
        original_discover = extract.discover_source

        def discovering(source_root: Path) -> extract.DiscoveryStats:
            """在发现返回后、pre-open 复核前改动日志。"""
            stats = original_discover(source_root)
            log.write_text(
                json.dumps({"event": "executor_error"}) + "\n"
                + json.dumps({"event": "read_incomplete"}) + "\n",
                encoding="utf-8",
            )
            return stats

        with mock.patch.object(extract, "discover_source", side_effect=discovering):
            with mock.patch.object(
                extract, "_read_log_events", side_effect=AssertionError("must not read")
            ):
                receipt = extract.run_extraction(
                    source, repo / "data" / "private-regression"
                )
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_reason"], "source_changed")
        self.assertFalse(receipt["safety"]["source_unchanged"])

    # ---------- 复审 N-4：Unicode 元素名解析前预算 ----------

    def test_prescan_blocks_unicode_deep_and_wide_before_parsing(self) -> None:
        """Unicode 元素名深/宽树也必须在 ET.fromstring 前触发预算。"""
        cases = (
            ("unicode_deep", "<hierarchy>" + "<é>" * 5 + "</é>" * 5 + "</hierarchy>", "too_deep"),
            ("unicode_wide", "<hierarchy>" + "<é/>" * 5 + "</hierarchy>", "too_many_nodes"),
        )
        for name, body, expected in cases:
            with self.subTest(case=name):
                calls: list[int] = []

                def failing_parse(*args: object, **kwargs: object) -> None:
                    """记录任何解析调用并立即失败。"""
                    calls.append(1)
                    raise AssertionError("must not parse before budget")

                with mock.patch.object(extract, "MAX_XML_DEPTH", 2), mock.patch.object(
                    extract, "MAX_XML_NODES", 2
                ), mock.patch.object(extract.ET, "fromstring", side_effect=failing_parse):
                    outcome = extract.sanitize_xml(
                        body.encode("utf-8"), extract.AliasBook(_alias_factory())
                    )
                self.assertEqual(outcome.anomaly, expected)
                self.assertEqual(calls, [])

    def test_unicode_attribute_values_and_tags_still_sanitize(self) -> None:
        """普通 node/hierarchy 的 Unicode 属性值仍按白名单合成，不整体拒绝。"""
        raw = (
            '<hierarchy rotation="0"><node class="android.widget.TextView" '
            'text="你好" content-desc="说明" bounds="[0,0][10,10]"/></hierarchy>'
        )
        outcome = extract.sanitize_xml(raw.encode("utf-8"), extract.AliasBook(_alias_factory()))
        self.assertIsNone(outcome.anomaly)
        self.assertIn('text="TEXT"', outcome.xml_text)
        self.assertIn('content-desc="DESC"', outcome.xml_text)
        self.assertNotIn("你好", outcome.xml_text)
        unknown = '<hierarchy><é class="android.view.View" text="你好"/></hierarchy>'
        outcome = extract.sanitize_xml(
            unknown.encode("utf-8"), extract.AliasBook(_alias_factory())
        )
        self.assertIsNone(outcome.anomaly)
        self.assertEqual(outcome.faults["unknown_tag"], 1)
        self.assertIn("<node", outcome.xml_text)
        self.assertNotIn("你好", outcome.xml_text)

    def test_unicode_deep_budget_sets_partial_fault_without_parsing(self) -> None:
        """Unicode 深树在抽取回执中记 too_deep/partial，且不先完整解析。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        deep = "<hierarchy>" + "<é>" * 5 + "</é>" * 5 + "</hierarchy>"
        (source / "screenshots" / "unicode-deep.xml").write_text(deep, encoding="utf-8")
        with mock.patch.object(extract, "MAX_XML_DEPTH", 2), mock.patch.object(
            extract.ET, "fromstring", side_effect=AssertionError("must not parse")
        ):
            receipt = extract.run_extraction(
                source, repo / "data" / "private-regression"
            )
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["xml"]["truncated"])
        self.assertEqual(receipt["xml"]["faults"]["too_deep"], 1)

    # ---------- 复审 N-2：未知空目录与目录总量边界 ----------

    def test_validator_rejects_unknown_empty_directory(self) -> None:
        """合法批次出现未知空目录也必须拒绝，不能以空目录绕过布局校验。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        (batch / "empty-dir").mkdir()
        result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["batch_layout_ok"])

    def test_validator_directory_budget_is_total(self) -> None:
        """目录数量有限总量；合法 3 个目录通过，超限或额外目录拒绝。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        with mock.patch.object(extract, "MAX_VALIDATE_DIRS", 3):
            self.assertEqual(extract.validate_batch(batch)["status"], "valid")
        with mock.patch.object(extract, "MAX_VALIDATE_DIRS", 2):
            self.assertEqual(extract.validate_batch(batch)["status"], "invalid")
        (batch / "wide-a").mkdir()
        (batch / "wide-b").mkdir()
        with mock.patch.object(extract, "MAX_VALIDATE_DIRS", 4):
            result = extract.validate_batch(batch)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["batch_layout_ok"])

    # ---------- f211f83 复审阻塞：流式枚举、立即停止与关闭 ----------

    def test_file_cap_stops_streaming_at_production_threshold(self) -> None:
        """单目录 300 额外文件时在 256 文件上限处停止，不消费全部条目。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        for index in range(300):
            (batch / f"extra-{index:03d}.txt").write_text("x", encoding="utf-8")
        total_entries = sum(1 for _ in extract.os.scandir(batch))
        self.assertGreater(total_entries, extract.MAX_VALIDATE_FILES)
        factory, probes = _scandir_factory()
        with mock.patch.object(extract.os, "scandir", side_effect=factory):
            result = extract.validate_batch(batch)
        yielded = sum(probe.yielded for _, probe in probes)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["checks"]["batch_layout_ok"])
        self.assertLess(yielded, total_entries)
        self.assertLessEqual(
            yielded, extract.MAX_VALIDATE_FILES + extract.MAX_VALIDATE_DIRS + 1
        )
        self.assertTrue(all(probe.closed for _, probe in probes))

    def test_directory_cap_stops_streaming_immediately(self) -> None:
        """目录上限命中后立即停止，不扫完大量额外空目录。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        for index in range(100):
            (batch / f"extra-dir-{index:03d}").mkdir()
        total_entries = sum(1 for _ in extract.os.scandir(batch))
        factory, probes = _scandir_factory()
        with mock.patch.object(extract, "MAX_VALIDATE_DIRS", 4):
            with mock.patch.object(extract.os, "scandir", side_effect=factory):
                result = extract.validate_batch(batch)
        yielded = sum(probe.yielded for _, probe in probes)
        self.assertEqual(result["status"], "invalid")
        self.assertLess(yielded, total_entries)
        self.assertLessEqual(yielded, 5 + 3)
        self.assertTrue(all(probe.closed for _, probe in probes))

    def test_depth_cap_does_not_scan_deeper_directory(self) -> None:
        """深度 4 允许扫描自身，深度 5 在上限检查处停止且不扫描更深目录。"""
        def build(name: str, levels: int) -> Path:
            """创建一个只含 XML 与指定深度目录链的批次。"""
            root = self.tmp / name
            root.mkdir()
            repo = _make_repo(root)
            source = _make_source(root)
            (source / "screenshots" / "a.xml").write_text(
                _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
            )
            receipt = extract.run_extraction(source, repo / "data" / "private-regression")
            batch = repo / "data" / "private-regression" / receipt["batch_id"]
            current = batch
            for index in range(levels):
                current = current / f"d{index}"
                current.mkdir()
            return batch

        depth4 = build("depth-four", 4)
        factory, probes = _scandir_factory()
        with mock.patch.object(extract.os, "scandir", side_effect=factory):
            result4 = extract.validate_batch(depth4)
        scanned4 = {path for path, _ in probes}
        self.assertEqual(result4["status"], "invalid")
        self.assertIn(depth4 / "d0" / "d1" / "d2" / "d3", scanned4)
        self.assertTrue(all(probe.closed for _, probe in probes))

        depth5 = build("depth-five", 5)
        factory, probes = _scandir_factory()
        with mock.patch.object(extract.os, "scandir", side_effect=factory):
            result5 = extract.validate_batch(depth5)
        scanned5 = {path for path, _ in probes}
        self.assertEqual(result5["status"], "invalid")
        self.assertNotIn(depth5 / "d0" / "d1" / "d2" / "d3" / "d4", scanned5)
        self.assertTrue(all(probe.closed for _, probe in probes))

    def test_depth_cap_stops_at_first_sibling_and_never_consumes_rest(self) -> None:
        """发现超深目录的当下停止，当前目录其余 100 兄弟与更深 scandir 零消费。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        current = batch
        chain = []
        for name in ("d0", "d1", "d2", "d3"):
            current = current / name
            current.mkdir()
            chain.append(current)
        d3 = chain[-1]
        d4 = d3 / "d4"
        d4.mkdir()
        for index in range(100):
            (d3 / f"f-{index:03d}.txt").write_text("x", encoding="utf-8")

        real = extract.os.scandir
        probes: list[tuple[Path, object]] = []
        d3_probe: list[_FirstEntryScandir] = []

        def factory(path: Path) -> object:
            """让 d3 的首个条目固定为超深 d4。"""
            if Path(path) == d3:
                probe = _FirstEntryScandir(d4, real(path))
                d3_probe.append(probe)
            else:
                probe = _CountingScandir(real(path))
            probes.append((Path(path), probe))
            return probe

        with mock.patch.object(extract.os, "scandir", side_effect=factory):
            result = extract.validate_batch(batch)
        scanned = {path for path, _ in probes}
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(len(d3_probe), 1)
        self.assertEqual(d3_probe[0].yielded, 1)
        self.assertEqual(d3_probe[0].remaining_yielded, 0)
        self.assertTrue(d3_probe[0].closed)
        self.assertNotIn(d4, scanned)
        self.assertTrue(all(probe.closed for _, probe in probes))

        small_root = self.tmp / "small-depth"
        small_root.mkdir()
        small_repo = _make_repo(small_root)
        small_source = _make_source(small_root)
        (small_source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        small_receipt = extract.run_extraction(
            small_source, small_repo / "data" / "private-regression"
        )
        small_batch = (
            small_repo / "data" / "private-regression" / small_receipt["batch_id"]
        )
        small_current = small_batch
        small_chain = []
        for name in ("s0", "s1", "s2"):
            small_current = small_current / name
            small_current.mkdir()
            small_chain.append(small_current)
        factory, small_probes = _scandir_factory()
        with mock.patch.object(extract, "MAX_VALIDATE_DEPTH", 2):
            with mock.patch.object(extract.os, "scandir", side_effect=factory):
                small_result = extract.validate_batch(small_batch)
        small_scanned = {path for path, _ in small_probes}
        self.assertEqual(small_result["status"], "invalid")
        self.assertNotIn(small_chain[2], small_scanned)
        self.assertTrue(all(probe.closed for _, probe in small_probes))

    # ---------- Git 查询运行故障：真实坏索引、阶段隔离与返回码矩阵 ----------

    def test_git_index_failure_at_initial_check_rejects_without_output(self) -> None:
        """初检 ls-files 真实返回 128 时固定 git_unavailable，且不创建输出。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        output = repo / "data" / "private-regression"
        command, state = _real_git_failure_command(repo, {1})
        with mock.patch.object(extract.subprocess, "run", side_effect=command):
            code, out, err = _run_main(["--source", str(source), "--output", str(output)])
        receipt = json.loads(out)
        self.assertEqual(state["codes"], [128])
        self.assertEqual(code, 2)
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_reason"], "git_unavailable")
        self.assertNotIn("safety", receipt)
        self.assertFalse(output.exists())
        self.assertNotIn("fatal", (out + err).lower())
        self.assertNotIn("index", (out + err).lower())

    def test_git_index_failure_at_final_check_reports_error_not_success(self) -> None:
        """结束复核 ls-files 真实返回 128 时 CLI 非零，不写成功安全回执。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        output = repo / "data" / "private-regression"
        command, state = _real_git_failure_command(repo, {2})
        with mock.patch.object(extract.subprocess, "run", side_effect=command):
            code, out, err = _run_main(["--source", str(source), "--output", str(output)])
        receipt = json.loads(out)
        self.assertEqual(state["codes"], [128])
        self.assertEqual(code, 2)
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["error_reason"], "git_unavailable")
        self.assertNotIn("safety", receipt)
        batches = list(output.glob("b-*"))
        self.assertEqual(len(batches), 1)
        self.assertTrue((batches[0] / "xml").is_dir())
        self.assertTrue(list((batches[0] / "xml").glob("*.xml")))
        self.assertFalse((batches[0] / "safe_receipt.json").exists())
        self.assertFalse((batches[0] / "manifest.json").exists())
        self.assertNotIn("fatal", (out + err).lower())
        self.assertNotIn("index", (out + err).lower())

    def test_git_index_failure_in_validator_returns_invalid(self) -> None:
        """独立 validate 遇到真实坏索引时 invalid/退出 2，不把查询失败当安全通过。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        receipt = extract.run_extraction(source, repo / "data" / "private-regression")
        batch = repo / "data" / "private-regression" / receipt["batch_id"]
        index = repo / ".git" / "index"
        saved = index.read_bytes() if index.exists() else None
        index.write_bytes(b"SYNTHETIC_CORRUPT_INDEX")
        try:
            result = extract.validate_batch(batch)
            code, out, err = _run_main(["--validate", str(batch)])
        finally:
            if saved is None:
                index.unlink()
            else:
                index.write_bytes(saved)
        cli_result = json.loads(out)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["error_reason"], "git_unavailable")
        self.assertFalse(result["checks"]["output_git_ignored"])
        self.assertFalse(result["checks"]["output_not_tracked"])
        self.assertEqual(code, 2)
        self.assertEqual(cli_result["status"], "invalid")
        self.assertEqual(cli_result["error_reason"], "git_unavailable")
        self.assertNotIn("fatal", (out + err).lower())
        self.assertNotIn("index", (out + err).lower())

    def test_git_positive_control_still_reports_ignored_untracked(self) -> None:
        """同一真实临时仓库无故障时仍为 ignored/untracked 正控。"""
        repo = _make_repo(self.tmp)
        source = _make_source(self.tmp)
        (source / "screenshots" / "a.xml").write_text(
            _xml(_node(**{"class": "android.view.View"})), encoding="utf-8"
        )
        output = repo / "data" / "private-regression"
        code, out, _ = _run_main(["--source", str(source), "--output", str(output)])
        receipt = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "ok")
        self.assertTrue(receipt["safety"]["output_git_ignored"])
        self.assertTrue(receipt["safety"]["output_not_tracked"])
        validate_code, validate_out, _ = _run_main(
            ["--validate", str(output / receipt["batch_id"])]
        )
        self.assertEqual(validate_code, 0)
        self.assertEqual(json.loads(validate_out)["status"], "valid")

    def test_git_query_returncode_matrix_and_oserror(self) -> None:
        """check-ignore/ls-files 仅接受 0/1；2/128/负码与 OSError 均固定拒绝。"""
        repo = _make_repo(self.tmp)
        target = repo / "data" / "private-regression"
        normal_cases = (
            (0, 0, True, True),
            (0, 1, True, False),
            (1, 0, False, True),
            (1, 1, False, False),
        )
        for check_code, tracked_code, expected_ignored, expected_tracked in normal_cases:
            with self.subTest(check=check_code, tracked=tracked_code):
                responses = [
                    subprocess.CompletedProcess([], check_code),
                    subprocess.CompletedProcess([], tracked_code),
                ]
                with mock.patch.object(extract.subprocess, "run", side_effect=responses):
                    ignored, tracked = extract._git_ignore_status(target)
                self.assertEqual(ignored, expected_ignored)
                self.assertEqual(tracked, expected_tracked)
        failure_cases = (
            (2, 1),
            (0, 2),
            (128, 1),
            (0, 128),
            (-9, 1),
            (0, -9),
        )
        for check_code, tracked_code in failure_cases:
            with self.subTest(check=check_code, tracked=tracked_code):
                responses = [
                    subprocess.CompletedProcess([], check_code),
                    subprocess.CompletedProcess([], tracked_code),
                ]
                with mock.patch.object(extract.subprocess, "run", side_effect=responses):
                    with self.assertRaises(extract.ExtractionError) as raised:
                        extract._git_ignore_status(target)
                self.assertEqual(raised.exception.reason, "git_unavailable")
        with mock.patch.object(extract.subprocess, "run", side_effect=OSError("synthetic")):
            with self.assertRaises(extract.ExtractionError) as raised:
                extract._git_ignore_status(target)
        self.assertEqual(raised.exception.reason, "git_unavailable")


if __name__ == "__main__":
    unittest.main()
