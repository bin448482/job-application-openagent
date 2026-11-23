"""离线场景资产自检、判据反例与隔离边界的最小可运行检查。

本文件只验证测试资产与框架能否抓住错误，不实现业务解析或调度，也不把参考
回放当作产品通过。
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from scenarios.framework import model, m0_protocol_probe, replay, runner  # noqa: E402
from scenarios.framework.compare import compare_invocation, compare_step, strict_equal  # noqa: E402

SCENARIO_ROOT = TESTS_DIR / "scenarios"
EXPECTED_IDS = {
    *(f"SC-C0{index}" for index in range(1, 4)),
    *(f"SC-L0{index}" for index in range(1, 8)),
    *(f"SC-U{index:02d}" for index in range(1, 16)),
    *(f"SC-R{index:02d}" for index in range(1, 15)),
    *(f"SC-D{index:02d}" for index in range(1, 12)),
    *(f"SC-A0{index}" for index in range(1, 9)),
    *(f"SC-S0{index}" for index in range(1, 9)),
    *(f"SC-G0{index}" for index in range(1, 5)),
    *(f"SC-Z0{index}" for index in range(1, 4)),
}


def load_assets() -> model.LoadedAssets:
    """加载已提交的场景清单与独立预期。"""
    return model.load_assets(SCENARIO_ROOT)


def variant_index(assets: model.LoadedAssets) -> dict[str, dict]:
    """按完整引用建立变体索引，便于断言特定变体。"""
    result = {}
    for scenario, variant in model.iter_variants(assets.catalogue):
        result[f"{scenario['id']}/{variant['id']}"] = variant
    return result


class AssetCompletenessTests(unittest.TestCase):
    """验证 73 例、变体和独立预期没有静默缺项。"""

    @classmethod
    def setUpClass(cls) -> None:
        """加载一次资产，避免重复读盘。"""
        cls.assets = load_assets()

    def test_asset_self_check_passes_without_product_pass_claim(self) -> None:
        """资产自检应通过，但报告明确不是产品验收。"""
        summary = runner.run_assets(self.assets)
        self.assertTrue(summary["ok"], summary["errors"])
        self.assertEqual(summary["scenario_count"], 73)
        self.assertEqual(summary["milestone_counts"]["M8"], 3)
        self.assertIn("不代表产品", summary["layer_note"])

    def test_all_73_scenario_ids_are_registered_once(self) -> None:
        """场景 ID 集合必须与方案 73 例完全一致且无重复。"""
        ids = [item["id"] for item in self.assets.catalogue["scenarios"]]
        self.assertEqual(set(ids), EXPECTED_IDS)
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_variant_has_independent_oracle(self) -> None:
        """每个变体都应有独立 expected，且引用只指向自身。"""
        refs = set()
        for scenario, variant in model.iter_variants(self.assets.catalogue):
            ref = f"{scenario['id']}/{variant['id']}"
            refs.add(ref)
            self.assertEqual(variant["expected_ref"], ref)
        self.assertEqual(refs, set(self.assets.oracles["variants"]))

    def test_device_gates_are_not_counted_as_offline(self) -> None:
        """T/O/W 必须显式保留未验收，不能伪装成离线通过。"""
        for _, variant in model.iter_variants(self.assets.catalogue):
            if variant["layer"] in ("T", "O", "W"):
                self.assertTrue(variant["device_gate"])
                self.assertEqual(variant["asset_status"], "DEVICE_GATE")
                self.assertIsNone(self.assets.oracles["variants"][variant["expected_ref"]]["acceptance"])
        self.assertGreater(model.validate_assets(self.assets)["status_counts"]["DEVICE_GATE"], 0)

    def test_action_fault_matrix_has_all_six_by_five_cells(self) -> None:
        """A01–A06 每个动作都登记 base 与 F-A..F-E，不靠文字推论。"""
        for action in range(1, 7):
            scenario_id = f"SC-A0{action}"
            variants = self.assets.scenario_index()[scenario_id]["variants"]
            variant_ids = {item["id"] for item in variants}
            self.assertGreaterEqual(len(variants), 6)
            self.assertIn(f"A0{action}_base", variant_ids)
            for fault in ("FA", "FB", "FC", "FD", "FE"):
                self.assertIn(f"A0{action}_{fault}", variant_ids)

    def test_expected_is_not_referenced_by_sensor_inputs(self) -> None:
        """sensor manifest 只含输入键，绝不引用独立答案文件。"""
        for _, variant in model.iter_variants(self.assets.catalogue):
            fixture = variant.get("fixture")
            if not fixture or not fixture.endswith(".json"):
                continue
            path = SCENARIO_ROOT / fixture
            if path.name == "manifest.json":
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("expected", value)
                self.assertNotIn("expected_ref", value)


class StrictComparisonTests(unittest.TestCase):
    """证明独立判据能抓住 bool 冒充整数、无效 stdout 与类型漂移。"""

    def test_bool_does_not_equal_integer_in_exact_object(self) -> None:
        """coordinator 复现的 True 不等于 1 必须被拒绝。"""
        errors = compare_step({"schema_version": True}, {"stdout_json_equals": {"schema_version": 1}})
        self.assertTrue(errors)
        self.assertFalse(strict_equal(True, 1))
        self.assertFalse(strict_equal(1, True))

    def test_bool_does_not_equal_integer_on_path_assertions(self) -> None:
        """路径 equals 与 in 同样不能接受 bool 冒充整数。"""
        self.assertTrue(compare_step({"value": True}, {"stdout_paths": [{"path": "value", "equals": 1}]}))
        self.assertTrue(compare_step({"value": 1}, {"stdout_paths": [{"path": "value", "in": [True]}]}))
        self.assertFalse(compare_step({"value": 1}, {"stdout_paths": [{"path": "value", "equals": 1}]}))

    def test_null_requires_key_presence_not_missing(self) -> None:
        """null 预期必须有键存在；缺失不能与 null 混同。"""
        self.assertFalse(compare_step({"value": None}, {"stdout_paths": [{"path": "value", "equals": None}]}))
        self.assertTrue(compare_step({}, {"stdout_paths": [{"path": "value", "equals": None}]}))

    def test_invalid_or_non_object_stdout_is_rejected(self) -> None:
        """普通业务 CLI 必须是单个 JSON 对象，失败或缺失 stdout 不能通过。"""
        result = {"exit_code": 0, "stdout_json": None, "stdout_json_ok": False, "stdout_text": "", "stderr": ""}
        self.assertTrue(compare_invocation(result, {"exit_code": 0, "stderr_empty": True}))
        result = {"exit_code": 0, "stdout_json": [], "stdout_json_ok": True, "stdout_text": "[]", "stderr": ""}
        self.assertTrue(compare_invocation(result, {"exit_code": 0}))

    def test_help_output_requires_explicit_non_json_contract(self) -> None:
        """帮助可非 JSON，但必须显式允许且非空，不能放宽普通业务输出。"""
        result = {"exit_code": 0, "stdout_json": None, "stdout_json_ok": False, "stdout_text": "usage", "stderr": ""}
        self.assertFalse(compare_invocation(result, {"exit_code": 0, "allow_non_json_stdout": True}))
        self.assertTrue(
            compare_invocation(
                {"exit_code": 0, "stdout_json": None, "stdout_json_ok": False, "stdout_text": "", "stderr": ""},
                {"exit_code": 0, "allow_non_json_stdout": True, "require_stdout_nonempty": True},
            )
        )

    def test_key_set_and_length_assertions(self) -> None:
        """额外/缺失键与长度都必须显式失败。"""
        self.assertFalse(compare_step({"a": 1}, {"stdout_paths": [{"path": "", "keys": ["a"]}]}))
        self.assertTrue(compare_step({"a": 1, "b": 2}, {"stdout_paths": [{"path": "", "keys": ["a"]}]}))
        self.assertFalse(compare_step({"a": [1]}, {"stdout_paths": [{"path": "a", "length_equals": 1}]}))
        self.assertTrue(compare_step({"a": [1]}, {"stdout_paths": [{"path": "a", "length_equals": 2}]}))


class ReplayTests(unittest.TestCase):
    """验证虚拟时钟不会静默接受回拨、错误起点或补造时刻。"""

    def test_event_before_declared_start_is_rejected(self) -> None:
        """coordinator 复现的 clock_start=5000/event=1000 必须拒绝。"""
        script = {"clock_start_ms": 5000, "events": [{"seq": 1, "virtual_ms": 1000, "event": "synthetic"}]}
        self.assertTrue(replay.validate_script(script))
        with self.assertRaises(replay.ReplayError):
            replay.replay_events(script)

    def test_injected_clock_must_match_script_start(self) -> None:
        """注入时钟当前值必须等于脚本起点，避免起点被忽略。"""
        script = {"clock_start_ms": 5000, "events": [{"seq": 1, "virtual_ms": 5000, "event": "synthetic"}]}
        with self.assertRaises(replay.ReplayError):
            replay.replay_events(script, replay.VirtualClock(0))

    def test_bool_and_negative_times_are_rejected(self) -> None:
        """bool 与负数都不是合法虚拟毫秒。"""
        with self.assertRaises(replay.ReplayError):
            replay.VirtualClock(True)
        with self.assertRaises(replay.ReplayError):
            replay.VirtualClock(-1)
        script = {"clock_start_ms": 0, "events": [{"seq": 1, "virtual_ms": True, "event": "x"}]}
        self.assertTrue(replay.validate_script(script))
        script = {"clock_start_ms": 0, "events": [{"seq": 1, "virtual_ms": -1, "event": "x"}]}
        self.assertTrue(replay.validate_script(script))

    def test_stimulus_timeline_does_not_fabricate_product_observations(self) -> None:
        """外部刺激只给 arrival；first_observed/handling 只能来自独立 expected。"""
        script = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s01-preemption.stimuli.json").read_text(encoding="utf-8")
        )
        invariants = replay.timing_invariants(replay.replay_events(script))
        self.assertEqual(invariants["first_arrival_at"], 8000)
        self.assertIsNone(invariants["first_observed_at"])
        self.assertIsNone(invariants["first_handling_started_at"])
        self.assertIsNone(invariants["waited_ms"])
        observations = json.loads(
            (SCENARIO_ROOT / "expected/m6-observations.json").read_text(encoding="utf-8")
        )
        case = observations["cases"]["SC-S01/safe_point_preempts"]["observation"]
        self.assertEqual(case["first_observed_at"], 10000)
        self.assertEqual(case["handling_started_at"], 20200)
        self.assertEqual(case["waited_ms"], 10200)

    def test_m6_stimuli_do_not_embed_product_outcomes(self) -> None:
        """S01/S03/S06/S08 刺激不得含 first_observed、pause、expected 等结果。"""
        event_dir = SCENARIO_ROOT / "fixtures/m6/events"
        for path in sorted(event_dir.glob("*.json")):
            self.assertEqual(model._validate_m6_stimuli(path), [], path.name)
        sample = json.loads((event_dir / "s01-preemption.stimuli.json").read_text(encoding="utf-8"))
        sample["events"].append({"seq": 99, "virtual_ms": 99, "event": "pause_preemption_limit"})
        sample["expected"] = {"handling_started_at": 1}
        sample["first_observed_at"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text(json.dumps(sample), encoding="utf-8")
            errors = model._validate_m6_stimuli(path)
        self.assertTrue(any("pause_preemption_limit" in item or "产品结果" in item for item in errors))

    def test_s03_expected_counts_only_actual_switches(self) -> None:
        """6 轮刺激只在 expected 中得到 5 次实际切换，第 6 次暂停且游标保留。"""
        observations = json.loads(
            (SCENARIO_ROOT / "expected/m6-observations.json").read_text(encoding="utf-8")
        )
        case = observations["cases"]["SC-S03/five_then_pause"]["observation"]
        self.assertEqual(case["actual_stage_preempted_count"], 5)
        self.assertEqual(case["per_cycle_switch_count"], [1, 1, 1, 1, 1, 0])
        self.assertFalse(case["unsafe_observation_incremented"])
        self.assertTrue(case["sixth_switch_required"])
        self.assertFalse(case["sixth_stage_preempted"])
        self.assertEqual(case["pause_reason"], "preemption_limit")
        self.assertTrue(case["cursor_preserved"])

    def test_missing_handling_is_not_fabricated(self) -> None:
        """未开始处理时 handling/waited 必须为 null，不能由观察时间补造。"""
        script = {
            "clock_start_ms": 0,
            "events": [{"seq": 1, "virtual_ms": 8000, "event": "arrival", "arrival_at": 8000}],
        }
        invariants = replay.timing_invariants(replay.replay_events(script))
        self.assertIsNone(invariants["first_handling_started_at"])
        self.assertIsNone(invariants["waited_ms"])


class AssetSafetyTests(unittest.TestCase):
    """验证非法路径在任何读取前被拒绝，且错误不带绝对路径。"""

    def test_sensor_manifest_outside_paths_are_not_read(self) -> None:
        """coordinator 复现的越界 profile/XML 读取次数必须为 0。"""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture_dir = base / "fixture"
            fixture_dir.mkdir()
            outside_xml = base / "outside.xml"
            outside_xml.write_text("<hierarchy></hierarchy>", encoding="utf-8")
            outside_profile = base / "outside-profile.json"
            outside_profile.write_text("{}", encoding="utf-8")
            manifest = fixture_dir / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "profile_path": "../outside-profile.json",
                        "xml_paths": ["../outside.xml"],
                        "app_version": "x",
                        "observed_at": "2026-11-24T00:00:00.000Z",
                        "baseline_rows": [],
                        "coverage_proof": {"kind": "viewport", "top_proven": False, "bottom_proven": False},
                    }
                ),
                encoding="utf-8",
            )
            real_load = model.load_json
            read_paths: list[str] = []

            def spy_load(path: Path):
                read_paths.append(path.name)
                return real_load(path)

            with mock.patch.object(model, "load_json", side_effect=spy_load), mock.patch.object(
                model.ElementTree, "parse", side_effect=AssertionError("不得解析越界 XML")
            ):
                errors = model._validate_sensor_manifest(manifest)
            self.assertTrue(errors)
            self.assertEqual(read_paths, ["manifest.json"])

    def test_malformed_manifest_shapes_are_rejected_without_crash(self) -> None:
        """顶层/列表/嵌套形状反例必须受控拒绝，不能 TypeError 崩溃。"""
        base_manifest = {
            "profile_path": "profile.json",
            "xml_paths": ["list.xml"],
            "app_version": "synthetic-1",
            "observed_at": "2026-11-24T00:00:00.000Z",
            "baseline_rows": [],
            "coverage_proof": {"kind": "viewport", "top_proven": False, "bottom_proven": False},
        }
        mutations = [
            {"xml_paths": [{}]},
            {"xml_paths": [None]},
            {"xml_paths": "list.xml"},
            {"profile_path": 1},
            {"baseline_rows": [1]},
            {"coverage_proof": []},
            {"observed_at": 1},
            {"app_version": True},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            fixture_dir = Path(temporary)
            (fixture_dir / "profile.json").write_text("{}", encoding="utf-8")
            (fixture_dir / "list.xml").write_text("<hierarchy></hierarchy>", encoding="utf-8")
            for mutation in mutations:
                manifest = dict(base_manifest)
                manifest.update(mutation)
                path = fixture_dir / "manifest.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                errors = model._validate_sensor_manifest(path)
                self.assertTrue(errors, mutation)

    def test_fixture_path_traversal_returns_before_read(self) -> None:
        """非法 fixture 路径应在存在性/读取之前返回。"""
        with mock.patch.object(model, "load_json", side_effect=AssertionError("不得读取")):
            errors = model._validate_fixture_files(SCENARIO_ROOT, "../outside-fixture.json")
        self.assertTrue(errors)

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持 symlink")
    def test_symlink_escape_is_rejected_without_reading(self) -> None:
        """夹具内指向外部的 symlink 必须拒绝且不读取。"""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixtures = base / "fixtures"
            fixtures.mkdir()
            outside = base / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            link = fixtures / "link.json"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                # 无 symlink 权限时仍验证判定分支，不跳过安全检查。
                with mock.patch.object(Path, "is_symlink", return_value=True), mock.patch.object(
                    model, "load_json", side_effect=AssertionError("不得读取")
                ):
                    target, errors = model._safe_asset_target(base, "fixtures/link.json")
                self.assertIsNone(target)
                self.assertTrue(errors)
                return
            with mock.patch.object(model, "load_json", side_effect=AssertionError("不得读取")):
                errors = model._validate_fixture_files(base, "fixtures/link.json")
            self.assertTrue(errors)

    def test_load_json_error_does_not_embed_absolute_path(self) -> None:
        """读取失败只保留文件名与异常类别。"""
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            with self.assertRaises(model.AssetError) as captured:
                model.load_json(missing)
            message = str(captured.exception)
            self.assertNotIn(str(missing.parent), message)
            self.assertIn("missing.json", message)


class RejectionAndCoverageTests(unittest.TestCase):
    """证明资产校验会拒绝缺项、伪 READY 和伪设备通过。"""

    @classmethod
    def setUpClass(cls) -> None:
        """加载一次基准资产。"""
        cls.assets = load_assets()

    def _validate_mutated(self, mutate) -> list[str]:
        """对清单做深拷贝突变后运行校验并返回错误。"""
        catalogue = copy.deepcopy(self.assets.catalogue)
        oracles = copy.deepcopy(self.assets.oracles)
        mutate(catalogue, oracles)
        return model.validate_assets(model.LoadedAssets(catalogue, oracles, SCENARIO_ROOT))["errors"]

    def test_missing_scenario_is_rejected(self) -> None:
        """删除任一场景必须被总数和里程碑计数抓住。"""
        errors = self._validate_mutated(lambda catalogue, oracles: catalogue["scenarios"].pop())
        self.assertTrue(any("场景总数" in item or "场景数" in item for item in errors))

    def test_duplicate_scenario_id_is_rejected(self) -> None:
        """重复场景 ID 必须拒绝。"""
        errors = self._validate_mutated(
            lambda catalogue, oracles: catalogue["scenarios"].append(copy.deepcopy(catalogue["scenarios"][0]))
        )
        self.assertTrue(any("重复" in item for item in errors))

    def test_missing_oracle_is_rejected(self) -> None:
        """删除独立预期必须拒绝。"""
        errors = self._validate_mutated(lambda catalogue, oracles: oracles["variants"].pop(next(iter(oracles["variants"]))))
        self.assertTrue(any("没有独立预期" in item for item in errors))

    def test_ready_without_frozen_steps_is_rejected(self) -> None:
        """没有 frozen 步骤的变体不得标 READY_FOR_APP。"""
        def mutate(catalogue, oracles):
            variant = catalogue["scenarios"][0]["variants"][0]
            variant["asset_status"] = "READY_FOR_APP"
            variant["entry_state"] = "frozen"
            variant["steps"] = []

        errors = self._validate_mutated(mutate)
        self.assertTrue(any("可执行步骤" in item for item in errors))

    def test_device_variant_without_gate_is_rejected(self) -> None:
        """T/O/W 变体缺少 device_gate 必须拒绝。"""
        def mutate(catalogue, oracles):
            for scenario in catalogue["scenarios"]:
                for variant in scenario["variants"]:
                    if variant["layer"] == "T":
                        variant["device_gate"] = False
                        return

        errors = self._validate_mutated(mutate)
        self.assertTrue(any("device_gate" in item for item in errors))


class AcceptanceBoundaryTests(unittest.TestCase):
    """验证缺 app、资产非法与零执行都不会给出总体 PASS。"""

    @classmethod
    def setUpClass(cls) -> None:
        """加载资产供验收分流测试使用。"""
        cls.assets = load_assets()

    def test_missing_app_is_blocked_not_pass(self) -> None:
        """没有 app 时严格验收必须 BLOCKED，且 PASS 计数为 0。"""
        with tempfile.TemporaryDirectory() as temporary:
            report = runner.run_acceptance(self.assets, Path(temporary), only="SC-C01")
            self.assertEqual(report["overall"], "BLOCKED")
            self.assertNotEqual(report["product_acceptance"], "PASS")
            self.assertEqual(report["counts"]["PASS"], 0)
            self.assertGreater(report["counts"]["BLOCKED_MISSING_APP"], 0)
            self.assertNotEqual(runner._exit_code(report), 0)

    def test_invalid_assets_refuse_execution(self) -> None:
        """资产自检失败时拒绝运行任何产品场景。"""
        mutated = model.LoadedAssets(
            {"schema_version": 1, "scenarios": []},
            {"schema_version": 1, "variants": {}},
            SCENARIO_ROOT,
        )
        with tempfile.TemporaryDirectory() as temporary:
            report = runner.run_acceptance(mutated, Path(temporary))
            self.assertEqual(report["overall"], "BLOCKED_ASSETS")
            self.assertEqual(report["results"], [])
            self.assertNotEqual(runner._exit_code(report), 0)

    def test_db_path_cannot_escape_isolation(self) -> None:
        """数据库断言路径越出隔离根时拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            problem = runner._db_check(root, {"kind": "file_absent", "path": "../outside.sqlite3"})
            self.assertIsNotNone(problem)

    def test_same_prefix_sibling_is_rejected_before_connect(self) -> None:
        """同名前缀兄弟目录不得靠 startswith 绕过，connect 必须为 0 次。"""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "iso"
            sibling = base / "iso-escape"
            root.mkdir()
            sibling.mkdir()
            (sibling / "synthetic.sqlite3").write_bytes(b"")
            with mock.patch.object(runner.sqlite3, "connect", side_effect=AssertionError("不得连接")):
                problem = runner._db_check(
                    root,
                    {"kind": "table_count", "table": "x", "equals": 0, "path": "../iso-escape/synthetic.sqlite3"},
                )
            self.assertIsNotNone(problem)

    def test_isolation_env_drops_credentials(self) -> None:
        """隔离环境不得继承模型认证或服务凭据类变量。"""
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SCENARIO_FAKE_API_KEY": "secret", "OPENAI_API_KEY": "secret"}):
                env = runner._isolation_env(Path(temporary))
            self.assertNotIn("SCENARIO_FAKE_API_KEY", env)
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_source_tree_symlink_is_rejected_before_copy(self) -> None:
        """app/fixtures 源树含链接时必须在 copytree 前拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            (target / "app" / "evil.py").write_text("", encoding="utf-8")
            (target / "tests" / "scenarios" / "fixtures").mkdir(parents=True)
            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self: Path) -> bool:
                return self.name == "evil.py" or real_is_symlink(self)

            with mock.patch.object(Path, "is_symlink", fake_is_symlink):
                with self.assertRaises(runner.IsolationError):
                    runner._prepare_isolation(target, "run-test", "SC-C01", "ready", {})
            self.assertFalse((target / runner.RUN_ROOT / "run-test" / "iso-SC-C01-ready").exists())

    def test_prepare_isolation_copies_only_synthetic_inputs(self) -> None:
        """复制后的隔离根含交付 app、夹具与 tripwire，且不含宿主凭据。"""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            fixtures = target / "tests" / "scenarios" / "fixtures"
            fixtures.mkdir(parents=True)
            (fixtures / "synthetic.json").write_text("{}", encoding="utf-8")
            (fixtures / "expected.json").write_text("{}", encoding="utf-8")
            variant = {
                "steps": ["python -m app doctor --fixture tests/scenarios/fixtures/synthetic.json"],
                "fixture": "fixtures/synthetic.json",
            }
            root = runner._prepare_isolation(target, "run-test", "SC-C01", "ready", variant)
            self.assertTrue((root / "app" / "__main__.py").is_file())
            self.assertTrue((root / "tests" / "scenarios" / "fixtures" / "synthetic.json").is_file())
            self.assertFalse((root / "tests" / "scenarios" / "fixtures" / "expected.json").exists())
            self.assertTrue((root / "sitecustomize.py").is_file())
            self.assertTrue((root / "tripwire.log").parent.is_dir())

    def test_expected_and_unselected_fixtures_are_not_copied(self) -> None:
        """只复制选定合成输入；expected.json 与未引用案例不得进入 SUT 树。"""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            case = target / "tests" / "scenarios" / "fixtures" / "sensor" / "case"
            case.mkdir(parents=True)
            (case / "manifest.json").write_text(
                json.dumps(
                    {
                        "profile_path": "profile.json",
                        "xml_paths": ["list.xml"],
                        "app_version": "synthetic-1",
                        "observed_at": "2026-11-24T00:00:00.000Z",
                        "baseline_rows": [],
                        "coverage_proof": {"kind": "viewport", "top_proven": False, "bottom_proven": False},
                    }
                ),
                encoding="utf-8",
            )
            (case / "profile.json").write_text("{}", encoding="utf-8")
            (case / "list.xml").write_text("<hierarchy></hierarchy>", encoding="utf-8")
            (case / "expected.json").write_text('{"unread_state":"present"}', encoding="utf-8")
            other = target / "tests" / "scenarios" / "fixtures" / "other"
            other.mkdir()
            (other / "input.json").write_text("{}", encoding="utf-8")
            variant = {
                "steps": ["python -m app sense --fixture tests/scenarios/fixtures/sensor/case/manifest.json"],
                "fixture": "fixtures/sensor/case",
            }
            root = runner._prepare_isolation(target, "run-test", "SC-U01", "unique", variant)
            copied = sorted(
                path.relative_to(root).as_posix()
                for path in (root / "tests").rglob("*")
                if path.is_file()
            )
            self.assertIn("tests/scenarios/fixtures/sensor/case/manifest.json", copied)
            self.assertIn("tests/scenarios/fixtures/sensor/case/profile.json", copied)
            self.assertIn("tests/scenarios/fixtures/sensor/case/list.xml", copied)
            self.assertFalse(any(path.endswith("expected.json") for path in copied))
            self.assertFalse(any("other" in path for path in copied))

    def test_declared_missing_input_is_not_blocked_but_unexpected_missing_is(self) -> None:
        """只有显式登记的缺失输入可交给真实 CLI 返回 invalid_config。"""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            (target / "tests" / "scenarios" / "fixtures" / "m0" / "config").mkdir(parents=True)
            declared = {
                "steps": [
                    "python -m app --config tests/scenarios/fixtures/m0/config/missing.json doctor --fixture tests/scenarios/fixtures/m0/config/missing.json"
                ],
                "fixture": None,
                "expected_missing_inputs": ["tests/scenarios/fixtures/m0/config/missing.json"],
            }
            root = runner._prepare_isolation(target, "run-test", "SC-C02", "config_missing_file", declared)
            self.assertFalse((root / "tests" / "scenarios" / "fixtures" / "m0" / "config" / "missing.json").exists())
            unexpected = {
                "steps": [
                    "python -m app --config tests/scenarios/fixtures/m0/config/missing.json doctor --fixture tests/scenarios/fixtures/m0/config/missing.json"
                ],
                "fixture": None,
            }
            with self.assertRaises(runner.IsolationError):
                runner._prepare_isolation(target, "run-test", "SC-C02", "unexpected", unexpected)

    def test_root_or_ancestor_junction_is_rejected(self) -> None:
        """app 自身或其父路径为 junction 时，必须在遍历/复制前拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real = base / "real"
            (real / "app").mkdir(parents=True)
            (real / "app" / "__main__.py").write_text("", encoding="utf-8")
            link = base / "linked"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(real)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode == 0 and runner._is_link_like(link):
                with self.assertRaises(runner.IsolationError):
                    runner._validate_source_tree(link / "app", base, "app-ancestor")
                with self.assertRaises(runner.IsolationError):
                    runner._validate_source_tree(link / "app", link, "app-root")
            else:
                # 无 junction 权限时仍验证自身/父路径判定分支。
                with mock.patch.object(Path, "is_junction", return_value=True):
                    with self.assertRaises(runner.IsolationError):
                        runner._validate_source_tree(real / "app", base, "app-ancestor")

    def test_asset_mode_exit_code(self) -> None:
        """资产模式通过退出 0；断言失败为 1，不碰产品。"""
        summary = runner.run_assets(self.assets)
        self.assertTrue(summary["ok"])
        self.assertEqual(runner._exit_code(summary), 0)

    def test_cli_load_failure_has_no_traceback_or_absolute_path(self) -> None:
        """根目录不存在时 CLI 必须输出安全 JSON，不泄露 traceback/绝对路径。"""
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "not-there"
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = runner.main(["--mode", "assets", "--root", str(missing)])
            self.assertNotEqual(code, 0)
            self.assertEqual(err.getvalue(), "")
            self.assertNotIn("Traceback", out.getvalue())
            self.assertNotIn(str(missing), out.getvalue())
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["overall"], "ASSET_CHECK_FAILED")


class IsolationTripwireTests(unittest.TestCase):
    """验证隔离运行会捕获并拒绝真实网络与外部进程尝试。"""

    def _run_probe(self, code: str) -> subprocess.CompletedProcess:
        """在带 sitecustomize 的隔离环境中运行一段探测代码。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sitecustomize.py").write_text(runner.SITECUSTOMIZE, encoding="utf-8")
            env = runner._isolation_env(root)
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                shell=False,
                check=False,
            )
            log = (root / "tripwire.log").read_text(encoding="utf-8") if (root / "tripwire.log").is_file() else ""
            completed.tripwire_log = log  # type: ignore[attr-defined]
            return completed

    def test_socket_attempt_is_blocked_and_recorded(self) -> None:
        """即使 tripwire 失效也不能真实联网：receiver 为 None。"""
        completed = self._run_probe("import socket; socket.socket.connect(None, ('127.0.0.1', 9))")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("RuntimeError", completed.stderr)
        self.assertIn("scenario boundary blocked: socket.connect", completed.stderr)
        self.assertIn("socket.connect", completed.tripwire_log)

    def test_subprocess_attempt_is_blocked_and_recorded(self) -> None:
        """即使 tripwire 失效也不能启动真实进程：argv 为空。"""
        completed = self._run_probe("import subprocess; subprocess.Popen([])")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("RuntimeError", completed.stderr)
        self.assertIn("scenario boundary blocked: subprocess.Popen", completed.stderr)
        self.assertIn("subprocess.Popen", completed.tripwire_log)


class P2M0BindingTests(unittest.TestCase):
    """M0 稳定 app 接入后的里程碑门禁与设备边界替身检查。"""

    def test_milestone_gate_blocks_unimplemented_m1(self) -> None:
        """M1 虽 READY，但未进入稳定 main 前只能 BLOCKED，不得被当 FAIL/PASS。"""
        assets = load_assets()
        self.assertEqual(runner.IMPLEMENTED_MILESTONES, {"M0"})
        m1_scenario = next(item for item in assets.catalogue["scenarios"] if item["milestone"] == "M1")
        m1_ready = next(
            item for item in m1_scenario["variants"] if item["asset_status"] == "READY_FOR_APP"
        )
        target = SCENARIO_ROOT.parent.parent
        self.assertEqual(
            runner._variant_status(m1_scenario, m1_ready, target),
            "BLOCKED_MILESTONE_UNIMPLEMENTED",
        )

    def test_m0_protocol_timeout_uses_device_boundary_only(self) -> None:
        """超时用例只替换 subprocess.run，返回同一原因码且不重试。"""
        result = m0_protocol_probe._run_case("timeout")
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertEqual(result["reason_code"], "device_disconnected")
        self.assertEqual(result["call_count"], 1)
        self.assertFalse(result["retried"])

    def test_m0_protocol_nonzero_does_not_retry(self) -> None:
        """非零退出同样只调用一次并归 device_disconnected。"""
        result = m0_protocol_probe._run_case("nonzero")
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertEqual(result["reason_code"], "device_disconnected")
        self.assertEqual(result["call_count"], 1)
        self.assertFalse(result["retried"])


class BuilderProvenanceTests(unittest.TestCase):
    """确认清单可追溯到作者脚本，且脚本存在的判据字段未被手改丢失。"""

    def test_builder_source_exists_and_catalogue_points_to_it(self) -> None:
        """catalogue.generated_from 必须指向实际存在的作者脚本。"""
        catalogue = json.loads((SCENARIO_ROOT / "catalogue.json").read_text(encoding="utf-8"))
        relative = catalogue["generated_from"]
        self.assertTrue((SCENARIO_ROOT / relative).is_file())
        spec = importlib.util.spec_from_file_location("build_assets", SCENARIO_ROOT / relative)
        self.assertIsNotNone(spec)


if __name__ == "__main__":
    unittest.main()
