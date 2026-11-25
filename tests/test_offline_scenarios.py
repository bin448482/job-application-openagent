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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from scenarios.framework import m0_protocol_probe, m1_probe, m2_probe, m3_probe, m4_probe, m4c_probe, m5_probe, m6_probe, model, replay, runner  # noqa: E402
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


def _make_m4_project(root: Path) -> None:
    """在临时根复制公开 base/示例/套件与合成包，供 M4 探针真实调用。"""
    repo_root = TESTS_DIR.parent
    for relative in ("tests/fixtures/policy-preview", "evals/policy-suite"):
        shutil.copytree(repo_root / relative, root / relative, dirs_exist_ok=True)
    shutil.copytree(
        SCENARIO_ROOT / "fixtures/m4/policy/base",
        root / "prompts/base",
        dirs_exist_ok=True,
    )
    for name in ("intent-and-reply.md", "job-evaluate.md"):
        base_path = root / "prompts" / "base" / name
        base_path.write_text(
            base_path.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    for name in ("valid", "eval"):
        shutil.copytree(
            SCENARIO_ROOT / "fixtures/m4/policy/packs" / name,
            root / "prompts/local" / name,
            dirs_exist_ok=True,
        )
        for file_name in ("pack.toml", "tone.md", "phrase-bank.md", "never-say.md"):
            pack_path = root / "prompts/local" / name / file_name
            pack_path.write_text(
                pack_path.read_text(encoding="utf-8"),
                encoding="utf-8",
                newline="\n",
            )
    shutil.copytree(
        SCENARIO_ROOT / "fixtures/m4/eval/holdout",
        root / "evals/private/holdout",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        SCENARIO_ROOT / "fixtures/m4/eval/regression",
        root / "evals/private/regression",
        dirs_exist_ok=True,
    )
    (root / "data/evals").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        SCENARIO_ROOT
        / "fixtures/m4/eval/attestation/holdout-attestation.synthetic.json",
        root / "data/evals/holdout-attestation.local.json",
    )


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

    def test_holdout_attestation_fixture_is_present_and_bound(self) -> None:
        """D11 合成无上传合同必须随资产交付，并与探针桥摘要一致。"""
        from app.domain.eval import validate_holdout_attestation

        path = (
            SCENARIO_ROOT
            / "fixtures/m4/eval/attestation/holdout-attestation.synthetic.json"
        )
        self.assertTrue(path.is_file(), "缺失合成无上传合同夹具")
        data = json.loads(path.read_text(encoding="utf-8"))
        validated = validate_holdout_attestation(
            data,
            provider_id="local-process",
            model_id="synthetic-model",
            model_config_hash="0" * 64,
        )
        self.assertIs(validated["no_upload"], True)
        self.assertEqual(validated["scope"], "holdout_local_no_upload")

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
                """仅把合成 evil.py 标记为链接，验证复制前拒绝分支。"""
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

    def test_fixture_files_maps_public_name_and_rejects_unsafe_paths(self) -> None:
        """单文件夹具可映射为消费方固定名，越界/答案/缺失来源必须拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            fixtures = target / "tests" / "scenarios" / "fixtures"
            source_root = fixtures / "m4" / "attestation"
            source_root.mkdir(parents=True)
            source = source_root / "holdout-attestation.synthetic.json"
            source.write_bytes(b'{"synthetic":true}\r\n')
            (source_root / "expected.json").write_text("{}", encoding="utf-8")
            variant = {
                "steps": [],
                "fixture_files": {
                    "data/evals/holdout-attestation.local.json":
                        "m4/attestation/holdout-attestation.synthetic.json",
                },
            }
            root = runner._prepare_isolation(target, "run-test", "SC-D11", "mapped", variant)
            destination = root / "data/evals/holdout-attestation.local.json"
            self.assertEqual(destination.read_bytes(), b'{"synthetic":true}\n')
            in_root = (
                target / "data/evals/scenario-runs/run-test/iso-SC-D11-mapped"
                / "data/evals/contract.json"
            )
            for bad in (
                {"../escape.json": "m4/attestation/holdout-attestation.synthetic.json"},
                {"data/evals/x.json": "../outside.json"},
                {"data/evals/x.json": "m4/attestation/expected.json"},
                {"data/evals/x.json": "m4/attestation/missing.json"},
                {"data/evals/x.json": str(source)},
                {str(in_root): "m4/attestation/holdout-attestation.synthetic.json"},
                {"data/evals/x.json": "m4/attestation/../attestation/holdout-attestation.synthetic.json"},
                {"data/evals/../contract.local.json": "m4/attestation/holdout-attestation.synthetic.json"},
                {"data/expected.json": "m4/attestation/holdout-attestation.synthetic.json"},
                {"data/oracles.json": "m4/attestation/holdout-attestation.synthetic.json"},
                {"data/evals/x.json": "C:/absolute/synthetic.json"},
                {"data/evals/x.json": "C:relative/synthetic.json"},
                {"data/evals/x.json": r"\\server\share\synthetic.json"},
                {r"\contract.json": "m4/attestation/holdout-attestation.synthetic.json"},
            ):
                with self.assertRaises(runner.IsolationError):
                    runner._prepare_isolation(
                        target, "run-test", "SC-D11", "mapped",
                        {"steps": [], "fixture_files": bad},
                    )

    def test_asset_validation_uses_same_mapping_path_rules(self) -> None:
        """资产自检与运行时共享同一原始路径规则，拒绝绝对/上跳/答案目标。"""
        assets = load_assets()
        cases = (
            {"data/evals/c.json": "C:/abs/synthetic.json"},
            {"data/evals/c.json": "m4/eval/../eval/attestation/holdout-attestation.synthetic.json"},
            {"data/evals/../contract.local.json": "m4/eval/attestation/holdout-attestation.synthetic.json"},
            {"data/expected.json": "m4/eval/attestation/holdout-attestation.synthetic.json"},
            {r"\contract.json": "m4/eval/attestation/holdout-attestation.synthetic.json"},
        )
        for mapping in cases:
            catalogue = copy.deepcopy(assets.catalogue)
            variant = next(
                item
                for scenario in catalogue["scenarios"]
                if scenario["id"] == "SC-D11"
                for item in scenario["variants"]
                if item["id"] == "holdout_freeze"
            )
            variant["fixture_files"] = mapping
            report = model.validate_assets(
                model.LoadedAssets(catalogue, assets.oracles, assets.root)
            )
            self.assertFalse(report["ok"], mapping)
            self.assertTrue(
                any("fixture_files" in error for error in report["errors"]),
                report["errors"],
            )

    def test_fixture_files_and_lf_inputs_share_one_byte_contract(self) -> None:
        """完整 prepare 中普通 CRLF 转换，BOM/NUL 自动与显式 LF 均原样。"""
        samples = {
            "plain": (b'{"synthetic":true}\r\n', b'{"synthetic":true}\n'),
            "bom": (b"\xef\xbb\xbfa\r\nb\r\n", b"\xef\xbb\xbfa\r\nb\r\n"),
            "nul": (b"a\r\n\x00b\r\n", b"a\r\n\x00b\r\n"),
            "bad": (b"\xff\r\n", b"\xff\r\n"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            fixtures = target / "tests" / "scenarios" / "fixtures" / "m4" / "bytes"
            fixtures.mkdir(parents=True)
            for name, (raw, _) in samples.items():
                (fixtures / f"{name}.json").write_bytes(raw)
            for name, (raw, expected) in samples.items():
                mapping = {f"data/evals/{name}.json": f"m4/bytes/{name}.json"}
                for declare in (False, True):
                    variant = {"steps": [], "fixture_files": mapping}
                    if declare:
                        variant["lf_inputs"] = list(mapping)
                    label = f"SC-D11-{name}-{'lf' if declare else 'auto'}"
                    try:
                        root = runner._prepare_isolation(
                            target, "run-bytes", "SC-D11", label, variant
                        )
                    except runner.IsolationError:
                        self.assertEqual((name, declare), ("bad", True))
                        continue
                    self.assertEqual(
                        (root / f"data/evals/{name}.json").read_bytes(),
                        expected if name != "bad" else raw,
                    )
                    if name == "bad":
                        self.assertFalse(declare)
                    if name == "plain":
                        self.assertNotEqual(expected, raw)
                    elif name in ("bom", "nul"):
                        self.assertEqual(expected, raw)

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

    def test_m0_probe_copy_rejects_ancestor_junction(self) -> None:
        """M0 边界替身复制入口必须复用父路径 junction 校验。"""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            fixtures = target / "tests" / "scenarios" / "fixtures"
            fixtures.mkdir(parents=True)
            real_framework = target / "real-framework"
            real_framework.mkdir()
            probe = real_framework / "m0_protocol_probe.py"
            probe.write_text("", encoding="utf-8")
            linked = target / "tests" / "scenarios" / "framework"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(real_framework)],
                capture_output=True,
                text=True,
                check=False,
            )
            variant = {
                "steps": ["python tests/scenarios/framework/m0_protocol_probe.py --case timeout"],
                "fixture": None,
            }
            if created.returncode == 0 and runner._is_link_like(linked):
                with self.assertRaises(runner.IsolationError):
                    runner._prepare_isolation(target, "run-test", "SC-C03", "probe", variant)
            else:
                real_is_junction = Path.is_junction

                def fake_is_junction(self: Path) -> bool:
                    """无 junction 权限时仅把合成 framework 目录标记为 junction。"""
                    return self.name == "framework" or real_is_junction(self)

                with mock.patch.object(Path, "is_junction", fake_is_junction):
                    with self.assertRaises(runner.IsolationError):
                        runner._prepare_isolation(target, "run-test", "SC-C03", "probe", variant)

    def test_m0_probe_symlink_is_rejected_before_copy(self) -> None:
        """M0 边界替身自身为链接时不得复制。"""
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "app").mkdir()
            (target / "app" / "__main__.py").write_text("", encoding="utf-8")
            fixtures = target / "tests" / "scenarios" / "fixtures"
            fixtures.mkdir(parents=True)
            framework = target / "tests" / "scenarios" / "framework"
            framework.mkdir()
            (framework / "m0_protocol_probe.py").write_text("", encoding="utf-8")
            variant = {
                "steps": ["python tests/scenarios/framework/m0_protocol_probe.py --case timeout"],
                "fixture": None,
            }
            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self: Path) -> bool:
                """仅把合成 M0 探针文件标记为链接，验证不复制链接自身。"""
                return self.name == "m0_protocol_probe.py" or real_is_symlink(self)

            with mock.patch.object(Path, "is_symlink", fake_is_symlink):
                with self.assertRaises(runner.IsolationError):
                    runner._prepare_isolation(target, "run-test", "SC-C03", "probe", variant)

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

    def test_milestone_gate_blocks_unimplemented_and_unfrozen(self) -> None:
        """M6 已分派；S01/S02 all_true/S08 已接线，其余变体仍只能 BLOCKED。"""
        assets = load_assets()
        self.assertEqual(
            runner.IMPLEMENTED_MILESTONES, {"M0", "M1", "M2", "M3", "M4", "M5", "M6"}
        )
        scenario = {"id": "SC-R99", "milestone": "M7"}
        variant = {
            "id": "synthetic",
            "layer": "F",
            "asset_status": "READY_FOR_APP",
            "entry_state": "frozen",
            "steps": ["python -m app doctor --fixture tests/scenarios/fixtures/m0/doctor/ready.json"],
            "expected_ref": "SC-R99/synthetic",
        }
        target = SCENARIO_ROOT.parent.parent
        self.assertEqual(
            runner._variant_status(scenario, variant, target),
            "BLOCKED_MILESTONE_UNIMPLEMENTED",
        )
        index = {
            f"{item['id']}/{entry['id']}": (item, entry)
            for item, entry in model.iter_variants(assets.catalogue)
        }
        for ref in (
            "SC-S01/safe_point_preempts",
            "SC-S01/arrival_after_commit",
            "SC-S02/all_true",
            "SC-S08/three_hooks",
        ):
            item, entry = index[ref]
            self.assertEqual(
                runner._variant_status(item, entry, target),
                "READY",
                ref,
            )
        for ref in (
            "SC-S02/single_false_blocks",
            "SC-S04/targeted_once",
            "SC-S04/count_no_target",
        ):
            item, entry = index[ref]
            self.assertEqual(
                runner._variant_status(item, entry, target),
                "BLOCKED_ENTRY_UNFROZEN",
                ref,
            )

    def test_m6_probe_rejects_unsafe_or_missing_stimuli(self) -> None:
        """M6 探针必须在读取前拒绝绝对/上跳/越界或不存在的刺激。"""
        for value in (
            "../outside-stimuli.json",
            "C:/outside-stimuli.json",
            "tests/scenarios/fixtures/m6/does-not-exist.stimuli.json",
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = m6_probe.main(["--case", "boundaries", "--stimuli", value])
            self.assertEqual(code, 2, value)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "probe_error")
            self.assertEqual(payload["reason"], "刺激路径非法" if ".." in value or value.startswith("C:") else "刺激文件不可读")

    def test_s03_timeline_consumes_original_events(self) -> None:
        """S03 原始 virtual_ms/arrival_at/安全点顺序必须实际进入解析轨迹。"""
        stimuli = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s03-preemption-limit.stimuli.json").read_text(
                encoding="utf-8"
            )
        )
        start, cycles, timeline = m6_probe._parse_s03_timeline(stimuli)
        self.assertEqual(start, 0)
        self.assertEqual(len(cycles), 6)
        self.assertEqual(len(timeline), 25)
        self.assertEqual(
            (cycles[0]["job_ms"], cycles[0]["arrival_at"], cycles[0]["snapshot_ms"],
             cycles[0]["not_reached_ms"], cycles[0]["safe_ms"]),
            (0, 1000, 1500, 1800, 2000),
        )
        self.assertEqual(
            (cycles[1]["job_ms"], cycles[1]["arrival_at"], cycles[1]["snapshot_ms"],
             cycles[1]["not_reached_ms"], cycles[1]["safe_ms"]),
            (2000, 2200, 2400, None, 2600),
        )
        changed = copy.deepcopy(stimuli)
        changed["events"][2]["virtual_ms"] = 1600
        _start, _cycles, changed_timeline = m6_probe._parse_s03_timeline(changed)
        self.assertNotEqual(changed_timeline[2]["virtual_ms"], 1500)
        arrival_only = copy.deepcopy(stimuli)
        arrival_only["events"][1]["arrival_at"] = 900
        _start, _cycles, arrival_timeline = m6_probe._parse_s03_timeline(arrival_only)
        self.assertEqual(arrival_timeline[1]["arrival_at"], 900)

    def _run_s03_temp(self, stimuli):
        """在临时合成项目根上运行真实 _Context/_run_s03，返回输出与上下文。"""
        import action_helpers

        tmp, root = action_helpers.temp_project()
        shutil.copytree(
            TESTS_DIR.parent / "tests" / "fixtures" / "read",
            root / "tests" / "fixtures" / "read",
        )
        (root / "tests" / "fixtures" / "runtime").mkdir(parents=True, exist_ok=True)
        old_root = m6_probe.ROOT
        try:
            m6_probe.ROOT = root
            ctx = m6_probe._Context()
            return m6_probe._run_s03(ctx, stimuli), ctx
        finally:
            m6_probe.ROOT = old_root
            tmp.cleanup()

    def test_s03_arrival_only_changes_execution_clock(self) -> None:
        """arrival-only 变异必须改变真实 execute_run 的 EnvironmentClock 轨迹。"""
        stimuli = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s03-preemption-limit.stimuli.json").read_text(
                encoding="utf-8"
            )
        )
        original, _ctx = self._run_s03_temp(stimuli)
        mutant = copy.deepcopy(stimuli)
        mutant["events"][1]["arrival_at"] = 900
        changed, _ctx2 = self._run_s03_temp(mutant)
        self.assertEqual(original["clock_trace"][1], 1000)
        self.assertEqual(changed["clock_trace"][1], 900)
        self.assertNotEqual(original["clock_trace"], changed["clock_trace"])
        self.assertNotEqual(original["timeline"], changed["timeline"])
        self.assertEqual(original["step_count"], 25)
        self.assertEqual(changed["step_count"], 25)

    def test_s03_unsafe_sampling_is_real_and_corruption_rejected(self) -> None:
        """unsafe 前后真实采样为 1 次观察/0 增量；破坏采样的输出被原断言拒绝。"""
        stimuli = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s03-preemption-limit.stimuli.json").read_text(
                encoding="utf-8"
            )
        )
        original, _ctx = self._run_s03_temp(stimuli)
        self.assertEqual(original["unsafe_observations"], 1)
        self.assertEqual(original["unsafe_sample_monotonic"], 1800)
        for key in (
            "unsafe_stage_before",
            "unsafe_stage_after",
            "unsafe_stage_delta",
            "unsafe_preemptions_before",
            "unsafe_preemptions_after",
            "unsafe_preemptions_delta",
        ):
            self.assertEqual(original[key], 0, key)
        self.assertFalse(original["unsafe_observation_incremented"])
        paths = [
            {"path": "unsafe_stage_delta", "equals": 0},
            {"path": "unsafe_preemptions_delta", "equals": 0},
            {"path": "unsafe_observation_incremented", "equals": False},
        ]
        self.assertEqual(compare_step(original, {"stdout_paths": paths}), [])
        corrupted = copy.deepcopy(original)
        corrupted["unsafe_stage_delta"] = 1
        corrupted["unsafe_observation_incremented"] = True
        self.assertNotEqual(compare_step(corrupted, {"stdout_paths": paths}), [])

    def test_s03_timeline_rejects_unknown_missing_duplicate_out_of_order(self) -> None:
        """S03 未知/缺失/重复/乱序/clock_start 不符必须拒绝。"""
        base = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s03-preemption-limit.stimuli.json").read_text(
                encoding="utf-8"
            )
        )

        def unknown(value):
            value["events"][2]["event"] = "ignored_marker"

        def missing_snapshot(value):
            value["events"].pop(2)

        def duplicate_seq(value):
            value["events"][1]["seq"] = value["events"][0]["seq"]

        def out_of_order(value):
            value["events"][3]["virtual_ms"] = 1400

        def clock_mismatch(value):
            value["clock_start_ms"] = 999999

        def missing_arrival(value):
            value["events"][1].pop("arrival_at")

        for mutate in (
            unknown,
            missing_snapshot,
            duplicate_seq,
            out_of_order,
            clock_mismatch,
            missing_arrival,
        ):
            value = copy.deepcopy(base)
            mutate(value)
            with self.assertRaises(m6_probe.ProbeError):
                m6_probe._parse_s03_timeline(value)

    def test_a06_control_binding_fact_is_consumed(self) -> None:
        """比较器必须消费逐控制真实绑定事实；错绑定被拒绝。"""
        step = {
            "stdout_paths": [
                {"path": "env.binding_ok", "equals": True},
                {"path": "env.profile_meta.profile_id", "equals": "m6-apply-preexisting"},
            ]
        }
        payload = {
            "env": {
                "binding_ok": True,
                "profile_meta": {"profile_id": "m6-apply-preexisting"},
            }
        }
        self.assertEqual(compare_step(payload, step), [])
        bad = copy.deepcopy(payload)
        bad["env"]["binding_ok"] = False
        self.assertNotEqual(compare_step(bad, step), [])

    def test_a06_control_log_fact_missing_is_rejected(self) -> None:
        """删除未断言的日志事实后比较器必须报缺失。"""
        step = {"stdout_paths": [{"path": "log_finish.unpaired", "equals": 0}]}
        self.assertEqual(compare_step({"log_finish": {"unpaired": 0}}, step), [])
        self.assertNotEqual(compare_step({}, step), [])

    def test_a06_control_nonzero_popen_is_rejected(self) -> None:
        """非零真实 Popen 尝试必须被比较器拒绝。"""
        step = {"stdout_paths": [{"path": "real_popen_attempts", "equals": 0}]}
        self.assertEqual(compare_step({"real_popen_attempts": 0}, step), [])
        self.assertNotEqual(compare_step({"real_popen_attempts": 1}, step), [])

    def test_deferred_and_module_evidence_are_not_pass(self) -> None:
        """DEFERRED 与 MODULE_EVIDENCE 不能被当 PASS 或普通入口阻塞。"""
        assets = load_assets()
        self.assertEqual(
            assets.oracles["variants"]["SC-L06/abandon_task"]["deferred_to"], "M5"
        )
        self.assertIn(
            "test_migrations",
            assets.oracles["variants"]["SC-L07/invalid_lease_state"]["module_evidence"],
        )
        m1 = next(item for item in assets.catalogue["scenarios"] if item["id"] == "SC-L06")
        deferred = next(item for item in m1["variants"] if item["id"] == "abandon_task")
        target = SCENARIO_ROOT.parent.parent
        self.assertEqual(runner._variant_status(m1, deferred, target), "DEFERRED")

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


class M6SevenVariantTests(unittest.TestCase):
    """M6 七个原变体的独立接线、缺口与变异负控；作者自检，不是产品验收。"""

    @classmethod
    def setUpClass(cls) -> None:
        """加载 oracle 供 comparator 复用。"""
        cls.assets = load_assets()

    def _run_case(self, name: str, stimuli_rel: str):
        """在临时合成项目根运行真实 M6 探针案例并返回输出。"""
        import action_helpers

        tmp, root = action_helpers.temp_project()
        try:
            shutil.copytree(
                TESTS_DIR.parent / "tests" / "fixtures" / "read",
                root / "tests" / "fixtures" / "read",
            )
            shutil.copytree(
                SCENARIO_ROOT / "fixtures" / "m3",
                root / "tests" / "scenarios" / "fixtures" / "m3",
            )
            stimuli = json.loads((SCENARIO_ROOT / stimuli_rel).read_text(encoding="utf-8"))
            old_root = m6_probe.ROOT
            try:
                m6_probe.ROOT = root
                ctx = m6_probe._Context()
                return {"status": "ok", "case": name, **m6_probe.CASES[name](ctx, stimuli)}
            finally:
                m6_probe.ROOT = old_root
        finally:
            tmp.cleanup()

    def _oracle_paths(self, ref: str) -> dict:
        """返回该变体独立 oracle 的 stdout path → equals 映射。"""
        step = self.assets.oracles["variants"][ref]["acceptance"]["steps"][0]
        return {item["path"]: item.get("equals") for item in step["stdout_paths"]}

    def test_s01_safe_point_uses_real_detection_and_clock(self) -> None:
        """检测切页是独立刺激；delay 改变实际 handling/waited，旧 oracle 拒绝。"""
        payload = self._run_case(
            "safe_point_preempts",
            "fixtures/m6/events/s01-preemption.stimuli.json",
        )
        self.assertEqual(payload["first_observed_ms"], 10000)
        self.assertEqual(payload["handling_started_ms"], 20200)
        self.assertEqual(payload["waited_ms"], 10200)
        self.assertEqual(payload["detection_ms"], 180)
        self.assertTrue(payload["arrival_not_first_observed"])
        self.assertTrue(payload["stage_preempted_after_safe_point"])
        self.assertFalse(payload["preempted_before_safe_point"])
        self.assertTrue(payload["checkpoint_before_stage"])
        self.assertEqual(
            payload["clock_trace"], [0, 8000, 10000, 20020, 20200]
        )
        oracle = self.assets.oracles["variants"]["SC-S01/safe_point_preempts"]["acceptance"]
        self.assertEqual(compare_step(payload, oracle["steps"][0]), [])
        stimuli = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s01-preemption.stimuli.json").read_text(
                encoding="utf-8"
            )
        )
        mutant = copy.deepcopy(stimuli)
        mutant["detect_after_ms"] = 300
        original_root = m6_probe.ROOT
        import action_helpers

        tmp, root = action_helpers.temp_project()
        try:
            shutil.copytree(
                TESTS_DIR.parent / "tests" / "fixtures" / "read",
                root / "tests" / "fixtures" / "read",
            )
            m6_probe.ROOT = root
            ctx = m6_probe._Context()
            changed = m6_probe._run_s01(ctx, mutant)
        finally:
            m6_probe.ROOT = original_root
            tmp.cleanup()
        self.assertEqual(changed["handling_started_ms"], 20320)
        self.assertEqual(changed["waited_ms"], 10320)
        self.assertEqual(changed["detection_ms"], 300)
        self.assertNotEqual(compare_step(changed, oracle["steps"][0]), [])
        arrival_only = copy.deepcopy(stimuli)
        arrival_only["events"][1]["arrival_at"] = 7000
        tmp, root = action_helpers.temp_project()
        try:
            shutil.copytree(
                TESTS_DIR.parent / "tests" / "fixtures" / "read",
                root / "tests" / "fixtures" / "read",
            )
            m6_probe.ROOT = root
            ctx = m6_probe._Context()
            arrival_payload = m6_probe._run_s01(ctx, arrival_only)
        finally:
            m6_probe.ROOT = original_root
            tmp.cleanup()
        self.assertEqual(arrival_payload["first_observed_ms"], 10000)
        self.assertEqual(arrival_payload["handling_started_ms"], 20200)
        self.assertEqual(arrival_payload["clock_trace"][1], 7000)

    def test_s01_arrival_after_commit_observed_only_at_normal_check(self) -> None:
        """到达不产生 first_observed；下一次正常检查才记录并切换。"""
        payload = self._run_case(
            "arrival_after_commit",
            "fixtures/m6/events/s01-arrival-after-commit.stimuli.json",
        )
        self.assertEqual(payload["arrival_at"], 500)
        self.assertEqual(payload["first_observed_ms"], 1000)
        self.assertEqual(payload["handling_started_ms"], 1000)
        self.assertEqual(payload["waited_ms"], 0)
        self.assertTrue(payload["arrival_not_first_observed"])
        self.assertTrue(payload["first_observed_at_normal_check"])
        self.assertTrue(payload["checkpoint_before_stage"])
        oracle = self.assets.oracles["variants"]["SC-S01/arrival_after_commit"]["acceptance"]
        self.assertEqual(compare_step(payload, oracle["steps"][0]), [])
        stimuli = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s01-arrival-after-commit.stimuli.json").read_text(
                encoding="utf-8"
            )
        )
        mutant = copy.deepcopy(stimuli)
        mutant["events"][2]["virtual_ms"] = 1200
        import action_helpers

        tmp, root = action_helpers.temp_project()
        original_root = m6_probe.ROOT
        try:
            shutil.copytree(
                TESTS_DIR.parent / "tests" / "fixtures" / "read",
                root / "tests" / "fixtures" / "read",
            )
            m6_probe.ROOT = root
            ctx = m6_probe._Context()
            changed = m6_probe._run_s01_arrival(ctx, mutant)
        finally:
            m6_probe.ROOT = original_root
            tmp.cleanup()
        self.assertEqual(changed["first_observed_ms"], 1200)
        self.assertNotEqual(compare_step(changed, oracle["steps"][0]), [])

    def test_s08_consumes_third_window_without_observe(self) -> None:
        """第三时窗确实被消费且零观察调用；刷新/到达变异改变真实轨迹。"""
        payload = self._run_case(
            "three_hooks",
            "fixtures/m6/events/s08-observation-boundaries.stimuli.json",
        )
        self.assertEqual(
            payload["hook_first_observed"], [0, 1000, None]
        )
        self.assertTrue(payload["third_window_consumed"])
        self.assertEqual(payload["third_window_observe_calls"], 0)
        self.assertFalse(payload["third_refresh_visible"])
        self.assertTrue(payload["third_arrival_event_consumed"])
        self.assertEqual(payload["third_arrival_event_consumed_at_ms"], 1900)
        self.assertTrue(payload["third_arrival_payload_present"])
        self.assertEqual(len(payload["third_arrival_payload_sha256"]), 64)
        self.assertEqual(
            payload["third_arrival_injection_boundary"],
            "driver_inbox_at_arrival_ms_refresh_selects_source",
        )
        self.assertTrue(payload["third_refresh_inbox_present"])
        self.assertFalse(payload["third_refresh_source_visible"])
        self.assertFalse(payload["third_refresh_consumed_payload"])
        self.assertEqual(
            payload["third_refresh_selected_fixture"],
            "tests/fixtures/read/page.json",
        )
        self.assertEqual(payload["third_arrival_product_state"], "absent")
        self.assertFalse(payload["third_arrival_claimed"])
        self.assertFalse(payload["third_arrival_observed"])
        self.assertIsNone(payload["third_window_first_observed_ms"])
        self.assertEqual(payload["consumed_step_count"], 11)
        self.assertEqual(payload["observe_call_ms"], [0, 100, 1000, 1100])
        self.assertTrue(payload["unobserved_not_fabricated"])
        self.assertTrue(payload["visible_at_refresh_preempts"])
        oracle = self.assets.oracles["variants"]["SC-S08/three_hooks"]["acceptance"]
        self.assertEqual(compare_step(payload, oracle["steps"][0]), [])
        stimuli = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s08-observation-boundaries.stimuli.json").read_text(
                encoding="utf-8"
            )
        )
        refresh_mutant = copy.deepcopy(stimuli)
        refresh_mutant["events"][1]["virtual_ms"] = 1100
        arrival_mutant = copy.deepcopy(stimuli)
        arrival_mutant["events"][1]["arrival_at"] = 800
        visible_mutant = copy.deepcopy(stimuli)
        visible_mutant["third_refresh_visible"] = True
        missing_mutant = copy.deepcopy(stimuli)
        missing_mutant["third_arrival"] = False
        missing_mutant["third_refresh_visible"] = True
        import action_helpers

        original_root = m6_probe.ROOT
        changed_refresh = changed_arrival = visible = missing = None
        for label, value in (
            ("refresh", refresh_mutant),
            ("arrival", arrival_mutant),
            ("visible", visible_mutant),
            ("missing", missing_mutant),
        ):
            tmp, root = action_helpers.temp_project()
            try:
                shutil.copytree(
                    TESTS_DIR.parent / "tests" / "fixtures" / "read",
                    root / "tests" / "fixtures" / "read",
                )
                m6_probe.ROOT = root
                ctx = m6_probe._Context()
                result = m6_probe._run_s08(ctx, value)
            finally:
                m6_probe.ROOT = original_root
                tmp.cleanup()
            if label == "refresh":
                changed_refresh = result
            elif label == "arrival":
                changed_arrival = result
            elif label == "visible":
                visible = result
            else:
                missing = result
        self.assertEqual(changed_refresh["second_hook_first_observed_at"], 1100)
        self.assertNotEqual(compare_step(changed_refresh, oracle["steps"][0]), [])
        self.assertEqual(changed_arrival["second_hook_first_observed_at"], 1000)
        self.assertNotEqual(compare_step(changed_arrival, oracle["steps"][0]), [])
        self.assertEqual(visible["third_window_first_observed_ms"], 2000)
        self.assertEqual(visible["third_window_observe_calls"], 1)
        self.assertFalse(visible["third_arrival_claimed"])
        self.assertTrue(visible["third_arrival_observed"])
        self.assertTrue(visible["third_refresh_inbox_present"])
        self.assertTrue(visible["third_refresh_source_visible"])
        self.assertTrue(visible["third_refresh_consumed_payload"])
        self.assertEqual(
            visible["third_refresh_selected_fixture"],
            "tests/fixtures/m6/read-s08-third-arrival/unread.json",
        )
        self.assertEqual(visible["third_arrival_product_state"], "absent")
        self.assertNotEqual(compare_step(visible, oracle["steps"][0]), [])
        self.assertFalse(missing["third_arrival_event_consumed"])
        self.assertFalse(missing["third_arrival_payload_present"])
        self.assertFalse(missing["third_refresh_inbox_present"])
        self.assertFalse(missing["third_refresh_source_visible"])
        self.assertFalse(missing["third_refresh_consumed_payload"])
        self.assertEqual(
            missing["third_refresh_selected_fixture"],
            "tests/fixtures/read/page.json",
        )
        self.assertIsNone(missing["third_window_first_observed_ms"])
        self.assertEqual(missing["third_window_observe_calls"], 0)
        self.assertEqual(missing["stage_preempted_count"], 2)
        self.assertNotEqual(compare_step(missing, oracle["steps"][0]), [])

    def test_s02_converged_retry_real_lifecycle_and_checkpoint_order(self) -> None:
        """pending/unknown 真实收敛后重跑；未决仍保锁并拒绝，检查点先于切换。"""
        payload = self._run_case(
            "safe_point_converged",
            "fixtures/m6/safe-point/converged-retry.stimuli.json",
        )
        self.assertEqual(payload["pending_action_state"], "pending")
        self.assertEqual(payload["unknown_action_state"], "unknown")
        self.assertEqual(payload["pending_reconciled_state"], "not_performed")
        self.assertEqual(payload["unknown_reconciled_state"], "not_performed")
        self.assertEqual(payload["pending_after_actions"], 0)
        self.assertEqual(payload["unknown_after_actions"], 0)
        self.assertEqual(payload["all_true_stage_preempted_count"], 1)
        self.assertEqual(payload["all_true_first_observed_ms"], 1000)
        self.assertEqual(payload["all_true_pre_pending_actions"], 0)
        self.assertEqual(payload["all_true_pre_last_state"], "not_performed")
        self.assertEqual(payload["all_true_read_status"], "ok")
        self.assertTrue(payload["all_true_read_actionable"])
        self.assertTrue(payload["all_true_input_empty"])
        self.assertTrue(payload["all_true_confirmation_closed"])
        self.assertTrue(payload["all_true_no_inflight"])
        self.assertLess(
            payload["all_true_checkpoint_event_id"],
            payload["all_true_stage_event_id"],
        )
        self.assertEqual(payload["all_true_checkpoint_stage_delta"], 3)
        self.assertTrue(payload["all_true_checkpoint_before_stage"])
        self.assertEqual(payload["refusal_reason"], "device_busy")
        self.assertEqual(payload["refusal_gate_state"], "held")
        self.assertTrue(payload["refusal_lock_retained"])
        self.assertEqual(payload["refusal_scene_reads"], 0)
        self.assertEqual(payload["refusal_action_state"], "pending")
        oracle = self.assets.oracles["variants"]["SC-S02/all_true"]["acceptance"]
        self.assertEqual(compare_step(payload, oracle["steps"][0]), [])
        corrupted = copy.deepcopy(payload)
        corrupted["all_true_checkpoint_before_stage"] = False
        self.assertNotEqual(compare_step(corrupted, oracle["steps"][0]), [])
        reversed_order = copy.deepcopy(payload)
        reversed_order["all_true_checkpoint_event_id"] = (
            payload["all_true_stage_event_id"] + 1
        )
        reversed_order["all_true_checkpoint_stage_delta"] = -1
        self.assertNotEqual(compare_step(reversed_order, oracle["steps"][0]), [])

    def test_s02_list_semantics_separates_unknown_and_recognized_pages(self) -> None:
        """真实 M3 页面：unknown 不算 list；会话/职位详情/主页面当前算 list。"""
        payload = self._run_case(
            "safe_point_list_semantics",
            "fixtures/m6/safe-point/converged-retry.stimuli.json",
        )
        cases = payload["cases"]
        self.assertEqual(cases["unknown_page"]["status"], "ok")
        self.assertEqual(cases["unknown_page"]["page"], "unknown")
        self.assertFalse(cases["unknown_page"]["page_ok"])
        self.assertEqual(cases["unknown_page"]["failed"], ["list_returned"])
        self.assertEqual(cases["chat_list_page"]["page"], "chat_list")
        self.assertTrue(cases["chat_list_page"]["page_ok"])
        self.assertEqual(cases["conversation_page"]["page"], "chat_detail")
        self.assertTrue(cases["conversation_page"]["page_ok"])
        self.assertEqual(cases["job_detail_page"]["page"], "job_detail")
        self.assertTrue(cases["job_detail_page"]["page_ok"])
        self.assertEqual(cases["main_page"]["page"], "main")
        self.assertTrue(cases["main_page"]["page_ok"])
        forged = copy.deepcopy(payload)
        forged["cases"]["unknown_page"]["page_ok"] = True
        self.assertNotEqual(
            compare_step(
                forged,
                {
                    "stdout_paths": [
                        {"path": "cases.unknown_page.page_ok", "equals": False}
                    ]
                },
            ),
            [],
        )

    def test_s04_gap_counts_actual_reads_only(self) -> None:
        """S04 只记真实初始读取；静态接口缺失单列，不升级业务 PASS。"""
        payload = self._run_case(
            "unread_verification_gap",
            "fixtures/m6/events/s04-verification-gap.stimuli.json",
        )
        self.assertEqual(payload["read_calls"], len(payload["read_requests"]))
        self.assertEqual(payload["read_calls"], 1)
        self.assertEqual(payload["unread_verified_events"], 1)
        self.assertEqual(payload["verification_payload"], "still_unknown")
        self.assertEqual(payload["pause_reason"], "unread_unverified")
        self.assertTrue(payload["diagnostic_only"])
        self.assertTrue(payload["not_business_pass"])
        self.assertNotIn("targeted_verification_api_calls", payload)
        self.assertIn(
            "targeted_verification_outcome", payload["source_audit"]["missing_call"]
        )
        stimuli = json.loads(
            (SCENARIO_ROOT / "fixtures/m6/events/s04-verification-gap.stimuli.json").read_text(
                encoding="utf-8"
            )
        )
        cleared = copy.deepcopy(stimuli)
        cleared["verification"] = "cleared"
        import action_helpers

        tmp, root = action_helpers.temp_project()
        original_root = m6_probe.ROOT
        try:
            shutil.copytree(
                TESTS_DIR.parent / "tests" / "fixtures" / "read",
                root / "tests" / "fixtures" / "read",
            )
            m6_probe.ROOT = root
            ctx = m6_probe._Context()
            accepted = m6_probe._run_s04_gap(ctx, cleared)
        finally:
            m6_probe.ROOT = original_root
            tmp.cleanup()
        self.assertEqual(accepted["read_calls"], 1)
        self.assertEqual(accepted["unread_verified_events"], 1)
        self.assertEqual(accepted["verification_payload"], "cleared")
        self.assertEqual(accepted["processed"], 0)
        self.assertTrue(accepted["scene_declaration_only"])

    def test_supplemental_observation_is_wired_to_oracle(self) -> None:
        """新增变体 expected 独立于原观察文件，并与 oracle 比较值一致。"""
        supplemental = json.loads(
            (SCENARIO_ROOT / "expected/m6-supplemental-observations.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            supplemental["role"], "expected_observation_only_not_sut_input"
        )
        observation = supplemental["cases"]["SC-S01/arrival_after_commit"][
            "observation"
        ]
        paths = self._oracle_paths("SC-S01/arrival_after_commit")
        mapping = {
            "arrival_at": "arrival_at",
            "first_observed_at": "first_observed_ms",
            "handling_started_at": "handling_started_ms",
            "waited_ms": "waited_ms",
            "arrival_not_first_observed": "arrival_not_first_observed",
        }
        for source, target in mapping.items():
            self.assertEqual(paths[target], observation[source], source)


class M1DriverBoundaryTests(unittest.TestCase):
    """验证 M1 探针路径边界、并发出错清理与关键判据 mutation。"""

    @classmethod
    def setUpClass(cls) -> None:
        """加载资产供 oracle 结构断言。"""
        cls.assets = load_assets()

    def test_m1_probe_rejects_parent_path_without_creating_file(self) -> None:
        """db/request 的 .. 路径必须在读取/写入前拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(Exception):
                m1_probe._relative_path("../escaped.sqlite3", root, "db", must_exist=False)
            self.assertFalse((root.parent / "escaped.sqlite3").exists())

    def test_fake_clock_rejects_backward_and_allows_independent_mono(self) -> None:
        """墙钟不得回退；mono_delta 可独立推进以制造 clock_changed 刺激。"""
        clock = m1_probe.FakeClock("2026-11-24T00:00:00.000Z")
        with self.assertRaises(Exception):
            clock.set_now("2026-11-23T23:59:59.000Z")
        before = clock.monotonic()
        clock.set_now("2026-11-24T00:00:20.000Z", mono_delta=0.0)
        self.assertEqual(clock.monotonic(), before)

    def test_concurrent_spawn_failure_cleans_started_child(self) -> None:
        """第二个 Popen 失败时必须终止/回收第一个子进程且不写 go。"""
        class FakeProcess:
            """最小 Popen 替身，记录终止与通信调用。"""

            def __init__(self) -> None:
                """初始化调用计数。"""
                self.terminate_calls = 0
                self.kill_calls = 0
                self.communicate_calls = 0
                self.returncode = None

            def poll(self):
                """模拟子进程仍在运行。"""
                return None

            def terminate(self) -> None:
                """记录 terminate。"""
                self.terminate_calls += 1

            def kill(self) -> None:
                """记录 kill。"""
                self.kill_calls += 1

            def communicate(self, timeout=None):
                """返回空管道，模拟已回收。"""
                self.communicate_calls += 1
                return "", ""

        fake = FakeProcess()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(runner.subprocess, "Popen", side_effect=[fake, OSError("spawn")]):
                results = runner._run_concurrent(
                    [
                        "python tests/scenarios/framework/m1_probe.py --request a --db x",
                        "python tests/scenarios/framework/m1_probe.py --request b --db x",
                    ],
                    root,
                    "run-test",
                    timeout=5,
                )
            self.assertTrue(all(item["error"] for item in results))
            self.assertTrue(fake.terminate_calls or fake.kill_calls)
            self.assertGreaterEqual(fake.communicate_calls, 1)
            self.assertFalse((root / "barrier" / "go").exists())

    def test_concurrent_early_exit_is_fail_without_go(self) -> None:
        """子进程提前退出且无 ready 文件时不得置 go/返回成功。"""
        class ExitedProcess:
            """模拟已退出且没有 ready 文件的 Popen。"""

            def __init__(self) -> None:
                """初始化通信计数。"""
                self._communicated = False
                self.returncode = 0

            def poll(self):
                """报告进程已退出。"""
                return 0

            def kill(self) -> None:
                """已退出时无需 kill。"""
                return None

            def communicate(self, timeout=None):
                """返回退出输出。"""
                if self._communicated:
                    return "", ""
                self._communicated = True
                return "", ""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processes = [ExitedProcess(), ExitedProcess()]
            with mock.patch.object(runner.subprocess, "Popen", side_effect=processes):
                results = runner._run_concurrent(
                    [
                        "python tests/scenarios/framework/m1_probe.py --request a --db x",
                        "python tests/scenarios/framework/m1_probe.py --request b --db x",
                    ],
                    root,
                    "run-test",
                    timeout=5,
                )
            self.assertTrue(all(item["error"] for item in results))
            self.assertFalse((root / "barrier" / "go").exists())

    def test_real_junction_in_relative_and_barrier_paths_is_rejected(self) -> None:
        """原始路径祖先为真实 Windows junction 时必须拒绝，不靠 mock。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(real)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0 or not runner._is_link_like(linked):
                self.skipTest("当前平台/账户不能创建 junction")
            with self.assertRaises(Exception):
                m1_probe._relative_path("linked/request.json", root, "request", must_exist=False)
            with mock.patch.dict(
                os.environ,
                {"M1_BARRIER_DIR": str(linked), "M1_BARRIER_NAME": "0"},
            ):
                with self.assertRaises(Exception):
                    m1_probe._barrier_paths(root)

    def test_clock_rejects_invalid_mono_delta_without_state_change(self) -> None:
        """mono_delta 必须为非 bool 有限非负数字，失败时不改两钟。"""
        clock = m1_probe.FakeClock("2026-11-24T00:00:00.000Z")
        before_wall = clock._wall
        before_mono = clock.monotonic()
        for invalid in (-1, True, "1", float("nan"), float("inf")):
            with self.assertRaises(Exception):
                clock.set_now("2026-11-24T00:00:20.000Z", mono_delta=invalid)
            self.assertEqual(clock._wall, before_wall)
            self.assertEqual(clock.monotonic(), before_mono)
        clock.set_now("2026-11-24T00:00:20.000Z", mono_delta=0.0)
        self.assertEqual(clock.monotonic(), before_mono)

    def test_invalid_concurrent_timeout_never_starts_children(self) -> None:
        """_execute_variant 不得 int() 强转 bool/字符串 timeout；非法时零启动。"""
        assets = load_assets()
        oracle = {
            "variants": {
                "SC-X/x": {
                    "scenario_id": "SC-X",
                    "variant_id": "x",
                    "layer": "F",
                    "expected_summary": "timeout",
                    "assertions": ["invalid timeout"],
                    "acceptance": {"steps": [{"children": [{}, {}]}]},
                    "forbidden": [],
                    "source": [],
                }
            }
        }
        variant = {
            "id": "x",
            "layer": "F",
            "asset_status": "READY_FOR_APP",
            "entry_state": "frozen",
            "steps": [{"concurrent": ["python a", "python b"], "timeout": True}],
            "expected_ref": "SC-X/x",
        }
        scenario = {"id": "SC-X", "milestone": "M1"}
        custom = model.LoadedAssets({"scenarios": []}, oracle, SCENARIO_ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(runner, "_prepare_isolation", return_value=root), mock.patch.object(
                runner, "_run_concurrent"
            ) as concurrent:
                result = runner._execute_variant(custom, scenario, variant, SCENARIO_ROOT.parent.parent, "run-test")
            self.assertEqual(result["result"], "FAIL")
            concurrent.assert_not_called()

    def test_m2_probe_real_junction_is_rejected(self) -> None:
        """M2 原始路径祖先为真实 junction 时必须拒绝，不先 resolve。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(real)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0 or not runner._is_link_like(linked):
                self.skipTest("当前平台/账户不能创建 junction")
            with mock.patch.object(m2_probe, "ROOT", root):
                with self.assertRaises(Exception):
                    m2_probe._relative("linked/request.json", "request", must_exist=False)

    def test_real_m2_serializer_mutant_is_caught_by_u01_oracle(self) -> None:
        """真实 sense serializer 篡改候选键/身份/指纹时 SC-U01 oracle 必须失败。"""
        from app.adapters.boss.list import load_and_parse_snapshot
        from app.usecases import sense

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-U01/unique_row_badge"]["acceptance"]["steps"][0]
        manifest = SCENARIO_ROOT / "fixtures/m2/sensor/positive/manifest.json"

        def bad_result_payload(result):
            """保持外层结构但篡改候选键、会话键与指纹，模拟 serializer 缺陷。"""
            snapshot = result.value
            value = {
                "app": snapshot.app,
                "profile_id": snapshot.profile_id,
                "coverage": snapshot.coverage,
                "signals": {
                    name: {
                        "state": snapshot.signals[name].state,
                        "count": snapshot.signals[name].count,
                        "reason_code": snapshot.signals[name].reason_code,
                    }
                    for name in snapshot.signals
                },
                "rows": [
                    {
                        "conversation_key": row.conversation_key,
                        "identity_hash": row.identity_hash,
                        "preview_hash": row.preview_hash,
                        "time_hint": row.time_hint,
                        "unread_count": row.unread_count,
                    }
                    for row in snapshot.rows
                ],
                "candidates": [
                    {
                        "candidate_key": "wrong",
                        "conversation_key": "wrong",
                        "kind": candidate.kind,
                        "source_names": list(candidate.source_names),
                        "fingerprint": "wrong",
                    }
                    for candidate in snapshot.candidates
                ],
                "unread_state": snapshot.unread_state,
            }
            return {
                "status": result.status,
                "value": value,
                "reason_code": result.reason_code,
                "snapshot_id": result.snapshot_id,
                "observed_at": result.observed_at,
                "source": result.source,
                "confidence": result.confidence,
            }

        result = load_and_parse_snapshot(
            manifest,
            "00000000-0000-4000-8000-0000000000c1",
            "2026-11-24T00:00:00.000Z",
        )
        with mock.patch.object(sense, "result_payload", side_effect=bad_result_payload):
            payload = sense.build_sense_payload(result, None)
        invocation = {
            "exit_code": 0,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(
            any("candidate_key" in item or "fingerprint" in item or "conversation_key" in item for item in differences),
            differences,
        )

    def test_real_m2_source_merge_mutant_is_caught_by_u07_db_oracle(self) -> None:
        """真实 CLI 持久化丢弃新来源时，SC-U07 的库内来源断言必须拒绝。"""
        from app.adapters.sqlite.repo import SqliteStore
        from app.cli.main import main as cli_main
        from app.process_log import ProcessLog

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-U07/same_fingerprint_cross_source"]["acceptance"]["steps"][3]
        project = SCENARIO_ROOT.parent.parent
        manifests = [
            "tests/scenarios/fixtures/m2/sensor/cross_badge_only/manifest.json",
            "tests/scenarios/fixtures/m2/sensor/cross_badge_preview/manifest.json",
        ]

        def drop_new_sources(self, stored_json, new_sources):
            """只返回库中旧来源，模拟持久化漏合并新来源的缺陷。"""
            return stored_json

        with tempfile.TemporaryDirectory(dir=project) as temporary:
            folder = Path(temporary)
            db = folder / "runtime.sqlite3"
            db_arg = f"{folder.name}/runtime.sqlite3"
            log = ProcessLog(path=folder / "mutant.jsonl")
            with mock.patch.object(SqliteStore, "_merged_sources", drop_new_sources), mock.patch(
                "app.cli.main.PROJECT_ROOT", project
            ):
                for manifest in manifests:
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = cli_main(
                            ["sense", "--fixture", manifest, "--db", db_arg],
                            process_log=log,
                        )
                    self.assertEqual(code, 0)
                    if manifest == manifests[0]:
                        m2_probe._record_baseline(db)
            actual = m2_probe._inspect(db)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": "ok", **actual},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("source_names" in item for item in differences), differences)

    def _run_u13_running_with_defer_mutant(self, mutate) -> list[str]:
        """用真实 seed/CLI/inspect 执行 SC-U13 running 三步并返回 oracle 差异。"""
        from app.adapters.sqlite.repo import SqliteStore
        from app.cli.main import main as cli_main
        from app.process_log import ProcessLog

        assets = load_assets()
        steps = assets.oracles["variants"]["SC-U13/running_blocked_new_fingerprint"]["acceptance"]["steps"]
        project = SCENARIO_ROOT.parent.parent
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m2/probe/seed_running.json").read_text(encoding="utf-8")
        )
        original = SqliteStore._defer_sources

        def mutant(store, *args, **kwargs):
            """先调用生产 defer，再注入合成缺陷。"""
            result = original(store, *args, **kwargs)
            mutate(store)
            return result

        with tempfile.TemporaryDirectory(dir=project) as temporary:
            folder = Path(temporary)
            db = folder / "runtime.sqlite3"
            db_arg = f"{folder.name}/runtime.sqlite3"
            with mock.patch.object(m2_probe, "ROOT", project):
                seeded = m2_probe._seed(request, db)
            log = ProcessLog(path=folder / "mutant.jsonl")
            with mock.patch.object(SqliteStore, "_defer_sources", mutant), mock.patch(
                "app.cli.main.PROJECT_ROOT", project
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli_main(
                        [
                            "sense",
                            "--fixture",
                            "tests/scenarios/fixtures/m2/sensor/new_fingerprint/manifest.json",
                            "--db",
                            db_arg,
                        ],
                        process_log=log,
                    )
            inspected = m2_probe._inspect(db)

        invocations = [
            {
                "exit_code": 0,
                "stdout_json": {"schema_version": 1, "status": "ok", **seeded},
                "stdout_json_ok": True,
                "stdout_text": "",
                "stderr": "",
            },
            {
                "exit_code": code,
                "stdout_json": json.loads(output.getvalue()),
                "stdout_json_ok": True,
                "stdout_text": "",
                "stderr": "",
            },
            {
                "exit_code": 0,
                "stdout_json": {"schema_version": 1, "status": "ok", **inspected},
                "stdout_json_ok": True,
                "stdout_text": "",
                "stderr": "",
            },
        ]
        differences: list[str] = []
        for index, (invocation, step) in enumerate(zip(invocations, steps)):
            differences.extend(f"第{index + 1}步: {item}" for item in compare_invocation(invocation, step))
        return differences

    def test_real_m2_u13_lease_nonce_mutant_is_caught(self) -> None:
        """真实 defer 后同步篡改任务/设备 nonce 时，SC-U13 租约断言必须拒绝。"""
        replacement = "00000000-0000-4000-8000-0000000000ff"

        def mutate(store):
            """同步改写 running 任务与 held 设备的 nonce，保持非空和关系。"""
            store._connection.execute(
                "UPDATE hr_discoveries SET nonce = ? WHERE state = 'running'", (replacement,)
            )
            store._connection.execute(
                "UPDATE device_lease SET nonce = ? WHERE device_key = 'primary'", (replacement,)
            )

        differences = self._run_u13_running_with_defer_mutant(mutate)
        self.assertTrue(
            any(
                "nonce" in item or "stable_unchanged" in item or "protected_task" in item
                for item in differences
            ),
            differences,
        )

    def test_real_m2_u13_payload_mutant_is_caught(self) -> None:
        """真实 defer 后篡改 kind/snapshot/opened 时，SC-U13 保护签名必须拒绝。"""
        replacement = "00000000-0000-4000-8000-0000000000ee"

        def mutate(store):
            """保持 key/fingerprint/owner，只改合同规定不得变化的负载字段。"""
            store._connection.execute(
                "UPDATE hr_discoveries SET kind = 'new_message', snapshot_id = ?, opened = 1 "
                "WHERE state = 'running'",
                (replacement,),
            )

        differences = self._run_u13_running_with_defer_mutant(mutate)
        self.assertTrue(
            any("protected_task" in item or "snapshot_id" in item or "kind" in item for item in differences),
            differences,
        )

    def test_real_m2_extra_message_row_changes_rollback_signature(self) -> None:
        """合成库新增合法 hr_messages 行时，SC-U14 回滚签名必须变为未保持。"""
        import sqlite3

        assets = load_assets()
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m2/probe/seed_backwards.json").read_text(encoding="utf-8")
        )
        oracle = assets.oracles["variants"]["SC-U14/observed_at_backwards"]["acceptance"]["steps"][2]
        project = SCENARIO_ROOT.parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "runtime.sqlite3"
            with mock.patch.object(m2_probe, "ROOT", project):
                m2_probe._seed(request, db)
            seeded = m2_probe._inspect(db)
            self.assertTrue(seeded["unchanged_since_seed"])
            conversation = seeded["conversation_keys"][0]
            connection = sqlite3.connect(db)
            try:
                connection.execute(
                    "INSERT INTO hr_messages (app,key,conversation_key,inbound_hash,observed_at) "
                    "VALUES ('boss','boss:message:mutant',?,?, '2026-11-24T00:00:00.000Z')",
                    (conversation, "b" * 64),
                )
                connection.commit()
            finally:
                connection.close()
            actual = m2_probe._inspect(db)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": "ok", **actual},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("unchanged_since_seed" in item for item in differences), differences)

    def test_m3_read_device_is_rejected_before_subprocess(self) -> None:
        """真实 read --device 必须在任何子进程前以 invalid_input 拒绝。"""
        from app.cli.main import main as cli_main
        from app.process_log import ProcessLog

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            log = ProcessLog(path=folder / "read-device.jsonl")
            output = io.StringIO()
            with mock.patch("subprocess.run") as run, redirect_stdout(output):
                code = cli_main(
                    ["read", "--device", "--serial", "synthetic"],
                    process_log=log,
                )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["reason_code"], "invalid_input")
        run.assert_not_called()

    def test_m3_probe_real_junction_is_rejected(self) -> None:
        """M3 原始路径祖先为真实 junction 时必须拒绝，不先 resolve。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(real)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0 or not runner._is_link_like(linked):
                self.skipTest("当前平台/账户不能创建 junction")
            with mock.patch.object(m3_probe, "ROOT", root):
                with self.assertRaises(Exception):
                    m3_probe._relative("linked/request.json", "request", must_exist=False)

    def test_real_m3_ladder_conflict_mutant_is_caught_by_r13_oracle(self) -> None:
        """真实 resolve_field 把冲突误判 present 时，SC-R13 实际 oracle 必须拒绝。"""
        from dataclasses import replace

        from app.domain import ladder

        assets = load_assets()
        request = json.loads(
            (
                SCENARIO_ROOT / "fixtures/m3/probe/ladder_conflict_request.json"
            ).read_text(encoding="utf-8")
        )
        oracle = assets.oracles["variants"]["SC-R13/cross_level_conflict"]["acceptance"]["steps"][4]
        original = ladder._resolution

        def wrong_conflict(*args, **kwargs):
            """把 undetermined/ambiguous_signal 篡改为可决策 present。"""
            resolution = original(*args, **kwargs)
            if resolution.state == "undetermined" and resolution.reason_code == "ambiguous_signal":
                return replace(
                    resolution,
                    state="present",
                    count=1,
                    reason_code=None,
                    requires_human=False,
                    source="hierarchy",
                    confidence="high",
                )
            return resolution

        with mock.patch.object(ladder, "_resolution", wrong_conflict), mock.patch.object(
            m3_probe, "ROOT", SCENARIO_ROOT.parent.parent
        ):
            actual = m3_probe._ladder(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": "ok", **actual},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(
            any("state" in item or "requires_human" in item for item in differences),
            differences,
        )

    def test_real_m3_events_pollution_is_caught_by_r05_full_signature(self) -> None:
        """真实 R05 末步必须用五表+事件完整签名拒绝合法事件污染。"""
        import sqlite3

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R05/list_only_identify"]["acceptance"]["steps"][3]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m2/probe/seed_queued.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "runtime.sqlite3"
            with mock.patch.object(m2_probe, "ROOT", SCENARIO_ROOT.parent.parent):
                m2_probe._seed(request, db)
            self.assertTrue(m2_probe._inspect(db)["unchanged_since_seed"])
            connection = sqlite3.connect(db)
            try:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE event_id = "
                    "(SELECT MIN(event_id) FROM events)",
                    ('{"synthetic":"polluted"}',),
                )
                connection.commit()
            finally:
                connection.close()
            actual = m2_probe._inspect(db)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": "ok", **actual},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("unchanged_since_seed" in item for item in differences), differences)

    def test_real_m3_same_snapshot_mutant_is_caught_by_r03_resolved_control(self) -> None:
        """真实定向核对忽略同 snapshot 条件时，R03 可解除对照必须拒绝。"""
        from app.usecases import read as read_usecase

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R03/targeted_api_resolved"]["acceptance"]["steps"][1]
        request = json.loads(
            (
                SCENARIO_ROOT / "fixtures/m3/probe/targeted_resolved_same_request.json"
            ).read_text(encoding="utf-8")
        )
        original = read_usecase.targeted_verification_outcome

        def ignore_same_snapshot(initial, targeted, queued_keys, *, now):
            """模拟误删同 snapshot 拒绝条件。"""
            result = original(initial, targeted, queued_keys, now=now)
            if targeted.snapshot_id == initial.snapshot_id:
                return None
            return result

        with mock.patch.object(
            read_usecase, "targeted_verification_outcome", ignore_same_snapshot
        ), mock.patch.object(m3_probe, "ROOT", SCENARIO_ROOT.parent.parent):
            actual = m3_probe._targeted(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": "ok", **actual},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("outcome" in item for item in differences), differences)

    def test_real_m3_serializer_mutant_is_caught_by_r05_shape_oracle(self) -> None:
        """真实 build_read_payload 缺 result.confidence/新增额外键时 R05 oracle 拒绝。"""
        from app.adapters.boss.read import load_and_read_fixture
        from app.usecases import read as read_usecase

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R05/list_only_identify"]["acceptance"]["steps"][2]
        result = load_and_read_fixture(
            SCENARIO_ROOT / "fixtures/m3/read/unique_unread/fixture.json",
            "00000000-0000-4000-8000-000000000501",
        )
        original = read_usecase.result_payload

        def bad_payload(item):
            """删除固定七键之一并加入额外合成键。"""
            payload = original(item)
            payload.pop("confidence")
            payload["extra"] = "synthetic"
            if isinstance(payload.get("value"), dict):
                payload["value"]["extra"] = "synthetic"
            return payload

        with mock.patch.object(read_usecase, "result_payload", side_effect=bad_payload):
            payload = read_usecase.build_read_payload(result)
        invocation = {
            "exit_code": 0,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(
            any("键集合" in item or "confidence" in item for item in differences), differences
        )

    def test_real_m3_boundary_identity_mutant_is_caught_by_r07_oracle(self) -> None:
        """真实跨屏比较忽略稳定 ID 时，相邻同文不同 ID 会被错误裁剪并被 R07 拒绝。"""
        from app.adapters.boss import read as boss_read
        from app.usecases import read as read_usecase

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R07/stable_identity_new_keys"]["acceptance"]["steps"][5]
        original = boss_read._messages_match

        def ignore_identity(left, right):
            """只比较内容/方向/时间，模拟误删稳定 ID 条件。"""
            return (
                left.kind == right.kind
                and left.direction == right.direction
                and left.content_hash == right.content_hash
                and left.time_hint == right.time_hint
                and left.truncated == right.truncated
            )

        with mock.patch.object(boss_read, "_messages_match", ignore_identity):
            result = boss_read.load_and_read_fixture(
                SCENARIO_ROOT / "fixtures/m3/read/conversation_boundary_diff_id/fixture.json",
                "00000000-0000-4000-8000-000000000502",
            )
        payload = read_usecase.build_read_payload(result)
        invocation = {
            "exit_code": 0,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("messages" in item or "长度" in item for item in differences), differences)

    def test_real_m3_page_identity_mutant_is_caught_by_r07_oracle(self) -> None:
        """真实 merge 仅用首屏身份时，后页重复 ID 的未知必须被 R07 拒绝。"""
        from dataclasses import replace

        from app.adapters.boss import read as boss_read
        from app.domain.reading import (
            Conversation,
            FieldEvidence,
            conversation_inbound_state,
        )
        from app.usecases import read as read_usecase

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R07/stable_identity_new_keys"]["acceptance"]["steps"][7]
        original = boss_read.merge_conversations

        def first_page_identity(pages, profile, proof, now, *, app_version=None):
            """模拟只采用首屏 message_identity_verified 并重算派生状态的回归。"""
            result = original(pages, profile, proof, now, app_version=app_version)
            if result.status == "ok" and isinstance(result.value, Conversation) and pages:
                first = pages[0].value
                if isinstance(first, Conversation):
                    value = replace(result.value, message_identity_verified=first.message_identity_verified)
                    state, key, reason = conversation_inbound_state(
                        value.messages,
                        value.read_complete,
                        value.contact_verified,
                        value.message_identity_verified,
                    )
                    inbound = FieldEvidence(
                        field="conversation_inbound",
                        source="hierarchy",
                        confidence="high" if state != "unknown" else "low",
                        state="ok" if state != "unknown" else "unknown",
                        reason_code=None if state != "unknown" else reason,
                    )
                    value = replace(
                        value,
                        inbound_state=state,
                        latest_inbound_key=key,
                        field_evidence=(value.field_evidence[0], inbound),
                    )
                    return replace(result, value=value)
            return result

        with mock.patch.object(boss_read, "merge_conversations", first_page_identity):
            result = boss_read.load_and_read_fixture(
                SCENARIO_ROOT / "fixtures/m3/read/conversation_late_duplicate/fixture.json",
                "00000000-0000-4000-8000-000000000503",
            )
        payload = read_usecase.build_read_payload(result)
        invocation = {
            "exit_code": 0,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(
            any("message_identity_verified" in item or "inbound_state" in item for item in differences),
            differences,
        )

    @staticmethod
    def _synthetic_log_event(
        stage: str,
        operation: str | None,
        result: str,
        *,
        reason_code: str | None = None,
        correlation: str = "00000000-0000-4000-8000-0000000000aa",
        **extra,
    ) -> dict:
        """构造字段完整、值合法的合成日志事件，供日志判据反例使用。"""
        event = {
            "schema_version": 1,
            "ts": "2026-11-24T00:00:00.000Z",
            "correlation_id": correlation,
            "stage": stage,
            "operation": operation,
            "result": result,
            "reason_code": reason_code,
            "error_category": None,
            "duration_ms": 0,
            "command": None,
            "exit_code": None,
            "mode": None,
            "config_source": None,
            "device_count": None,
            "case_index": None,
        }
        event.update(extra)
        return event

    def _write_synthetic_log(self, root: Path, events: list[dict], name: str) -> Path:
        """把合成事件写成单次调用的 JSON Lines 日志文件。"""
        log_dir = root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / name
        path.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
            encoding="utf-8",
        )
        return path

    def test_m3_log_check_rejects_order_reason_extra_key_and_path(self) -> None:
        """日志判据必须拒绝倒序、阶段/原因篡改、缺 started、额外键与路径值。"""
        events = [
            self._synthetic_log_event("start", None, "started"),
            self._synthetic_log_event("read", "load", "started"),
            self._synthetic_log_event("read", "unread", "started"),
            self._synthetic_log_event("read", "unread", "failed", reason_code="invalid_xml"),
            self._synthetic_log_event("read", "load", "failed", reason_code="invalid_xml"),
            self._synthetic_log_event("exit", None, "failed", reason_code="invalid_xml"),
        ]
        expected = {
            "step": 0,
            "events": [
                {"stage": "start", "operation": None, "result": "started", "reason_code": None},
                {"stage": "read", "operation": "load", "result": "started", "reason_code": None},
                {"stage": "read", "operation": "unread", "result": "started", "reason_code": None},
                {
                    "stage": "read",
                    "operation": "unread",
                    "result": "failed",
                    "reason_code": "invalid_xml",
                },
                {
                    "stage": "read",
                    "operation": "load",
                    "result": "failed",
                    "reason_code": "invalid_xml",
                },
                {
                    "stage": "exit",
                    "operation": None,
                    "result": "failed",
                    "reason_code": "invalid_xml",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._write_synthetic_log(root, events, "m0-process-synthetic.jsonl")
            self.assertIsNone(runner._log_check(root, expected, [path]))
            mutations = {
                "reversed": list(reversed(events)),
                "stage_rewritten": [
                    {**event, "stage": "decision"} for event in events
                ],
                "missing_started": [
                    event
                    for event in events
                    if not (
                        event["stage"] == "read"
                        and event["operation"] == "unread"
                        and event["result"] == "started"
                    )
                ],
                "extra_key": [{**event, "synthetic_path": "/synthetic/path"} for event in events],
                "path_value": [{**event, "command": "C:/synthetic/path"} for event in events],
            }
            for name, mutated in mutations.items():
                self._write_synthetic_log(root, mutated, "m0-process-synthetic.jsonl")
                self.assertIsNotNone(
                    runner._log_check(root, expected, [path]), name
                )
            self._write_synthetic_log(root, events, "m0-process-synthetic.jsonl")
            other = self._write_synthetic_log(
                root,
                [
                    self._synthetic_log_event(
                        "read",
                        "unread",
                        "ok",
                        correlation="00000000-0000-4000-8000-0000000000bb",
                    )
                ],
                "m0-process-second.jsonl",
            )
            self.assertIn("一个日志文件", runner._log_check(root, expected, [path, other]) or "")
            self._write_synthetic_log(
                root,
                [
                    self._synthetic_log_event("start", None, "started"),
                    self._synthetic_log_event(
                        "read",
                        "unread",
                        "ok",
                        correlation="00000000-0000-4000-8000-0000000000cc",
                    ),
                ],
                "m0-process-synthetic.jsonl",
            )
            self.assertIn(
                "correlation",
                runner._log_check(
                    root,
                    {
                        "step": 0,
                        "events": [
                            {"stage": "start", "operation": None, "result": "started", "reason_code": None},
                            {"stage": "read", "operation": "unread", "result": "ok", "reason_code": None},
                        ],
                    },
                    [path],
                )
                or "",
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write("合成消息甲\n")
            self.assertIn(
                "泄露",
                runner._log_check(root, {**expected, "forbidden": ["合成消息甲"]}, [path]) or "",
            )

    def test_m3_log_check_rejects_real_scenario_mutations_without_borrowing(self) -> None:
        """真实 SC-R13 日志的倒序/阶段/缺 started/额外路径必须被实际 oracle 拒绝。"""
        assets = load_assets()
        variant = variant_index(assets)["SC-R13/log_trace_no_pixel"]
        checks = assets.oracles["variants"]["SC-R13/log_trace_no_pixel"]["acceptance"]["log_checks"]
        target = TESTS_DIR.parent
        run_id = f"unittest-log-{uuid.uuid4().hex[:8]}"
        isolation = runner._prepare_isolation(
            target, run_id, "SC-R13", "log_trace_no_pixel", variant
        )
        try:
            step_logs: list[list[Path]] = []
            previous = set(runner._safe_log_files(isolation))
            for command in variant["steps"]:
                runner._run_step(command, isolation, run_id)
                current = set(runner._safe_log_files(isolation))
                step_logs.append(sorted(current - previous))
                previous = current
            self.assertEqual([len(item) for item in step_logs], [1, 1])
            self.assertIsNone(runner._log_check(isolation, checks[0], step_logs[0]))
            self.assertIsNone(runner._log_check(isolation, checks[1], step_logs[1]))
            self.assertIsNotNone(
                runner._log_check(isolation, checks[0], step_logs[1]),
                "不得跨调用借用另一调用的日志",
            )
            path = step_logs[1][0]
            original = path.read_text(encoding="utf-8")
            try:
                lines = original.splitlines()
                events = [json.loads(line) for line in lines]
                mutations = {
                    "reversed": list(reversed(lines)),
                    "stage_and_reason": [
                        json.dumps(
                            {
                                **event,
                                "stage": "decision",
                                "reason_code": "wrong_page" if event["reason_code"] else None,
                            },
                            ensure_ascii=False,
                        )
                        for event in events
                    ],
                    "missing_invalid_xml_started": [
                        line
                        for line, event in zip(lines, events)
                        if not (
                            event["stage"] == "read"
                            and event["operation"] == "unread"
                            and event["result"] == "started"
                        )
                    ],
                    "extra_synthetic_path": [
                        json.dumps(
                            {
                                **event,
                                "synthetic_path": str(
                                    (
                                        target
                                        / "tests/scenarios/fixtures/m3/read/invalid_xml_with_pixel/fixture.json"
                                    ).resolve()
                                ),
                            },
                            ensure_ascii=False,
                        )
                        for event in events
                    ],
                }
                for name, mutated in mutations.items():
                    path.write_text("\n".join(mutated) + "\n", encoding="utf-8")
                    self.assertIsNotNone(
                        runner._log_check(isolation, checks[1], step_logs[1]), name
                    )
            finally:
                path.write_text(original, encoding="utf-8")
            self.assertIsNone(runner._log_check(isolation, checks[1], step_logs[1]))
            self.assertIsNone(runner._log_check(isolation, checks[0], step_logs[0]))
        finally:
            shutil.rmtree(target / runner.RUN_ROOT / run_id, ignore_errors=True)

    def test_real_m3_log_check_rejects_junction_log_dir(self) -> None:
        """日志观察器必须先拒绝日志目录 junction，不能跟随到隔离根外。"""
        if os.name != "nt":
            self.skipTest("仅 Windows 验证 junction")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            log_dir = root / "data" / "logs"
            log_dir.parent.mkdir(parents=True)
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(log_dir), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0 or not runner._is_link_like(log_dir):
                self.skipTest("当前平台/账户不能创建 junction")
            with self.assertRaises(runner.IsolationError):
                runner._safe_log_files(root)

    def test_real_m3_non_first_rows_mutant_is_caught_by_r10_oracle(self) -> None:
        """真实未读输出第二/三行新增键、缺键或身份篡改必须被实际 oracle 拒绝。"""
        from app.adapters.boss.read import load_and_read_fixture
        from app.usecases.read import build_read_payload

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R10/incomplete_or_ambiguous"]["acceptance"]["steps"][9]
        result = load_and_read_fixture(
            SCENARIO_ROOT / "fixtures/m3/read/ambiguous_unread/fixture.json",
            "00000000-0000-4000-8000-0000000005a1",
        )
        payload = build_read_payload(result)
        invocation = {
            "exit_code": 0,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        self.assertEqual(compare_invocation(invocation, oracle), [])
        mutations = {
            "second_extra_key": lambda rows: rows[1].__setitem__("unexpected_synthetic_key", "x"),
            "second_missing_hash": lambda rows: rows[1].pop("identity_hash"),
            "second_hash_tampered": lambda rows: rows[1].__setitem__("identity_hash", "0" * 64),
            "third_hash_tampered": lambda rows: rows[2].__setitem__("identity_hash", "1" * 64),
        }
        for name, mutate in mutations.items():
            broken = copy.deepcopy(payload)
            mutate(broken["result"]["value"]["rows"])
            differences = compare_invocation({**invocation, "stdout_json": broken}, oracle)
            self.assertTrue(differences, name)

    def test_real_m3_non_first_cards_mutant_is_caught_by_r10_oracle(self) -> None:
        """真实职位列表非首卡新增键、缺键或身份篡改必须被实际 oracle 拒绝。"""
        from app.adapters.boss.read import load_and_read_fixture
        from app.usecases.read import build_read_payload

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R10/page_job_paths"]["acceptance"]["steps"][3]
        result = load_and_read_fixture(
            SCENARIO_ROOT / "fixtures/m3/read/job_list_ambiguous/fixture.json",
            "00000000-0000-4000-8000-0000000005a2",
        )
        payload = build_read_payload(result)
        invocation = {
            "exit_code": 0,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        self.assertEqual(compare_invocation(invocation, oracle), [])
        mutations = {
            "second_extra_key": lambda cards: cards[1].__setitem__("unexpected_synthetic_key", "x"),
            "second_missing_hash": lambda cards: cards[1].pop("identity_hash"),
            "second_hash_tampered": lambda cards: cards[1].__setitem__("identity_hash", "0" * 64),
            "third_hash_tampered": lambda cards: cards[2].__setitem__("identity_hash", "1" * 64),
        }
        for name, mutate in mutations.items():
            broken = copy.deepcopy(payload)
            mutate(broken["result"]["value"]["cards"])
            differences = compare_invocation({**invocation, "stdout_json": broken}, oracle)
            self.assertTrue(differences, name)

    def test_real_m3_non_first_message_and_evidence_mutants_are_caught(self) -> None:
        """真实会话第二条消息与第二项证据的键/摘要/状态篡改必须被实际 oracle 拒绝。"""
        from app.adapters.boss.read import load_and_read_fixture
        from app.usecases.read import build_read_payload

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-R10/incomplete_or_ambiguous"]["acceptance"]["steps"][3]
        result = load_and_read_fixture(
            SCENARIO_ROOT / "fixtures/m3/read/conversation_ambiguous/fixture.json",
            "00000000-0000-4000-8000-0000000005a3",
        )
        payload = build_read_payload(result)
        invocation = {
            "exit_code": 2,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        self.assertEqual(compare_invocation(invocation, oracle), [])
        mutations = {
            "second_message_extra_key": lambda value: value["messages"][1].__setitem__(
                "unexpected_synthetic_key", "x"
            ),
            "second_message_missing_hash": lambda value: value["messages"][1].pop("content_hash"),
            "second_message_hash_tampered": lambda value: value["messages"][1].__setitem__(
                "content_hash", "0" * 64
            ),
            "second_evidence_state_tampered": lambda value: value["field_evidence"][1].__setitem__(
                "state", "ok"
            ),
            "second_evidence_reason_tampered": lambda value: value["field_evidence"][1].__setitem__(
                "reason_code", None
            ),
        }
        for name, mutate in mutations.items():
            broken = copy.deepcopy(payload)
            mutate(broken["result"]["value"])
            differences = compare_invocation({**invocation, "stdout_json": broken}, oracle)
            self.assertTrue(differences, name)

    def test_real_m4_decision_timeout_and_retry_mutant_are_caught(self) -> None:
        """真实 decide 提交后超时只一次；若误允许重试，D03 实际 oracle 必须拒绝。"""
        from app.ports.decision import BridgeError

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D03/post_submit_timeout"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d03_post_submit_timeout.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                actual = m4_probe._decision_run_operation(request)
                normal = {
                    "exit_code": 0,
                    "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                        key: value for key, value in actual.items() if key != "status"
                    }},
                    "stdout_json_ok": True,
                    "stdout_text": "",
                    "stderr": "",
                }
                self.assertEqual(compare_invocation(normal, oracle), [])
                with mock.patch.object(
                    BridgeError,
                    "retryable",
                    new_callable=mock.PropertyMock,
                    return_value=True,
                ):
                    mutant = m4_probe._decision_run_operation(request)
            invocation = {
                "exit_code": 0,
                "stdout_json": {"schema_version": 1, "status": mutant["status"], **{
                    key: value for key, value in mutant.items() if key != "status"
                }},
                "stdout_json_ok": True,
                "stdout_text": "",
                "stderr": "",
            }
            differences = compare_invocation(invocation, oracle)
            self.assertTrue(any("attempts" in item or "bridge_calls" in item for item in differences), differences)

    def test_m2_probe_seed_uses_synthetic_host_without_platform_lookup(self) -> None:
        """M2 探针 seed 使用固定合成 host，不触发 platform.node/WMI 回退。"""
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m2/probe/seed_blocked.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "m2-host.sqlite3"
            with mock.patch("platform.node", side_effect=AssertionError("platform.node called")):
                result = m2_probe._seed(request, db)
        self.assertEqual(result["state"], "blocked")

    def test_real_m4_constant_hash_mutant_is_caught_by_d08_oracle(self) -> None:
        """真实摘要函数被常量替换时，D08 独立手算摘要 oracle 必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D08/hash_order"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d08_hash.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                normal = m4_probe._hash_operation(request)
                invocation = {
                    "exit_code": 0,
                    "stdout_json": {"schema_version": 1, "status": normal["status"], **{
                        key: value for key, value in normal.items() if key != "status"
                    }},
                    "stdout_json_ok": True,
                    "stdout_text": "",
                    "stderr": "",
                }
                self.assertEqual(compare_invocation(invocation, oracle), [])
                with mock.patch.object(m4_probe, "compute_base_hash", return_value="f" * 64):
                    mutant = m4_probe._hash_operation(request)
            broken = {
                "exit_code": 0,
                "stdout_json": {"schema_version": 1, "status": mutant["status"], **{
                    key: value for key, value in mutant.items() if key != "status"
                }},
                "stdout_json_ok": True,
                "stdout_text": "",
                "stderr": "",
            }
            differences = compare_invocation(broken, oracle)
            self.assertTrue(any("base_hash" in item for item in differences), differences)

    def test_real_m4_role_partition_mutant_is_caught_by_d07_oracle(self) -> None:
        """真实桥请求额外加入 tools 时，D07 角色/工具 oracle 必须拒绝。"""
        from app.adapters.llm import support as bridge_support

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D07/contract_role_partition"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d07_role.json").read_text(encoding="utf-8")
        )
        original = bridge_support.SubprocessBridge._request

        def with_tools(self, system_message, developer_message, user_message, echo):
            """在真实桥请求上追加一个合成工具，模拟分区泄露。"""
            text = original(self, system_message, developer_message, user_message, echo)
            data = json.loads(text.decode("utf-8"))
            data["tools"] = [{"name": "synthetic"}]
            return (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root), mock.patch.object(
                bridge_support.SubprocessBridge, "_request", with_tools
            ):
                actual = m4_probe._bridge_operation(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("tools" in item for item in differences), differences)

    def test_real_m4_holdout_failure_finish_mutant_is_caught_by_d11_audit(self) -> None:
        """删除失败访问的 finish 收尾时，D11 首次失败审计 oracle 必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/holdout_first_failure"]["acceptance"]["steps"][2]
        from app.usecases import policy_eval as eval_usecases

        requests = {
            name: json.loads(
                (SCENARIO_ROOT / f"fixtures/m4/probe/{name}.json").read_text(encoding="utf-8")
            )
            for name in ("d11_register", "d11_holdout_failed_access", "d11_holdout_audit")
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                m4_probe._eval_operation(requests["d11_register"])
                with mock.patch.object(
                    eval_usecases, "_finish_holdout", lambda *args, **kwargs: None
                ):
                    m4_probe._eval_operation(requests["d11_holdout_failed_access"])
                actual = m4_probe._eval_operation(requests["d11_holdout_audit"])
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("outcomes" in item or "phases" in item for item in differences), differences)

    def test_real_m4_holdout_attestation_gate_positive_and_missing_contract(self) -> None:
        """合成无上传合同随资产交付时真实访问；缺合同时零桥调用拒绝。"""
        requests = {
            name: json.loads(
                (SCENARIO_ROOT / f"fixtures/m4/probe/{name}.json").read_text(encoding="utf-8")
            )
            for name in ("d11_register", "d11_holdout_first")
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                m4_probe._eval_operation(requests["d11_register"])
                allowed = m4_probe._eval_operation(requests["d11_holdout_first"])
        self.assertEqual(allowed["status"], "ok", allowed)
        self.assertGreater(allowed["bridge_calls"], 0)
        self.assertTrue(allowed["authorize_viewed"])
        self.assertEqual(allowed["authorize_intent_count"], 1)
        self.assertIsNotNone(allowed["holdout"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            (root / "data/evals/holdout-attestation.local.json").unlink()
            with mock.patch.object(m4_probe, "ROOT", root):
                m4_probe._eval_operation(requests["d11_register"])
                blocked = m4_probe._eval_operation(requests["d11_holdout_first"])
        self.assertEqual(blocked["status"], "error")
        self.assertEqual(blocked["reason_code"], "authorization_required")
        self.assertEqual(blocked["bridge_calls"], 0)
        self.assertIsNone(blocked["authorize_intent_count"])

    def test_real_m4_regression_admission_tamper_is_caught_by_d11_oracle(self) -> None:
        """真实 regression 准入样本被篡改时，D11 独立指纹 oracle 必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/holdout_freeze"]["acceptance"]["steps"][-1]
        requests = {
            name: json.loads(
                (SCENARIO_ROOT / f"fixtures/m4/probe/{name}.json").read_text(encoding="utf-8")
            )
            for name in ("d11_register", "d11_retire", "d11_regression_check")
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                m4_probe._eval_operation(requests["d11_register"])
                m4_probe._eval_operation(requests["d11_retire"])
                admissions = sorted((root / "data/evals/regression-admissions").glob("*/suite.json"))
                self.assertTrue(admissions)
                data = json.loads(admissions[0].read_text(encoding="utf-8"))
                target = data["cases"][0]["input"]
                target["case_id"] = "tampered-synthetic"
                target["inbound"] = "被篡改的合成输入"
                admissions[0].write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                actual = m4_probe._eval_operation(requests["d11_regression_check"])
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(
            any("regression_case_ids" in item or "regression_case_digests" in item for item in differences),
            differences,
        )

    def test_real_m4_eval_role_leak_mutant_is_caught_by_d11_oracle(self) -> None:
        """实际评测桥 developer/system 泄漏合成 expected 标记时，D11 oracle 必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/public_contract"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d11_eval_public.json").read_text(encoding="utf-8")
        )
        original = m4_probe._EvalBridge.complete

        def leaking_complete(self, **kwargs):
            """在真实桥调用成功后向 developer 载荷注入真实合成 case_id 值。"""
            text = original(self, **kwargs)
            self.role_payloads[-1]["developer"] += " eval-greeting-location"
            return text

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root), mock.patch.object(
                m4_probe._EvalBridge, "complete", leaking_complete
            ):
                actual = m4_probe._eval_operation(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("transport_markers" in item for item in differences), differences)

    def test_real_m4_bridge_intent_clear_mutant_is_caught_by_d11_oracle(self) -> None:
        """桥窗口把本次 intent/viewed 清空时，D11 bridge 边界观察必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/holdout_freeze"]["acceptance"]["steps"][3]
        requests = {
            name: json.loads(
                (SCENARIO_ROOT / f"fixtures/m4/probe/{name}.json").read_text(encoding="utf-8")
            )
            for name in ("d11_register", "d11_holdout_first")
        }
        original = m4_probe._EvalBridge.complete

        def clearing_complete(self, **kwargs):
            """只在真实桥窗口把 registry 的 rounds/viewed 清空。"""
            original_read = self.store.read_holdout_registry
            self.store.read_holdout_registry = lambda: {"schema_version": 1, "batches": []}
            try:
                return original(self, **kwargs)
            finally:
                self.store.read_holdout_registry = original_read

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                m4_probe._eval_operation(requests["d11_register"])
                with mock.patch.object(
                    m4_probe._EvalBridge, "complete", clearing_complete
                ):
                    actual = m4_probe._eval_operation(requests["d11_holdout_first"])
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("bridge_intent" in item for item in differences), differences)

    def test_real_m4_sample_occupancy_mutant_is_caught_by_conflict_oracle(self) -> None:
        """删除真实样本占用检查后，D11 冲突 oracle 必须拒绝。"""
        from app.domain import eval as eval_domain

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/holdout_conflict_paths"]["acceptance"]["steps"][2]
        requests = {
            name: json.loads(
                (SCENARIO_ROOT / f"fixtures/m4/probe/{name}.json").read_text(encoding="utf-8")
            )
            for name in ("d11_register", "d11_register_duplicate")
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                m4_probe._eval_operation(requests["d11_register"])
                with mock.patch.object(
                    eval_domain, "occupied_sample_fingerprints", return_value=set()
                ):
                    actual = m4_probe._eval_operation(requests["d11_register_duplicate"])
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("detail" in item or "status" in item for item in differences), differences)

    def test_real_m4_wrong_attempt_id_mutant_is_caught_by_d11_oracle(self) -> None:
        """桥窗口把 intent attempt_id 换成另一合法 ID 时，D11 oracle 必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/holdout_freeze"]["acceptance"]["steps"][3]
        requests = {
            name: json.loads(
                (SCENARIO_ROOT / f"fixtures/m4/probe/{name}.json").read_text(encoding="utf-8")
            )
            for name in ("d11_register", "d11_holdout_first")
        }
        original = m4_probe._EvalBridge.complete

        def wrong_attempt(self, **kwargs):
            """只在真实桥窗口把 intent attempt_id 替换为另一合成 UUID。"""
            original_read = self.store.read_holdout_registry

            def changed():
                """返回把 intent attempt_id 替换后的登记视图。"""
                registry = original_read()
                for batch in registry.get("batches", []):
                    for entry in batch.get("access_rounds", []):
                        if entry.get("phase") == "intent":
                            entry["attempt_id"] = "00000000-0000-4000-8000-00000000ffff"
                return registry

            self.store.read_holdout_registry = changed
            try:
                return original(self, **kwargs)
            finally:
                self.store.read_holdout_registry = original_read

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root):
                m4_probe._eval_operation(requests["d11_register"])
                with mock.patch.object(
                    m4_probe._EvalBridge, "complete", wrong_attempt
                ):
                    actual = m4_probe._eval_operation(requests["d11_holdout_first"])
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("bridge_attempt" in item for item in differences), differences)

    def test_real_m4_expected_marker_leak_is_caught_by_terms_oracle(self) -> None:
        """评测 assertion 侧唯一 expected 标记进入 developer 时，terms oracle 必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/public_contract_terms"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d11_eval_terms.json").read_text(encoding="utf-8")
        )
        original = m4_probe._EvalBridge.complete

        def leaking_complete(self, **kwargs):
            """在真实桥入参中追加评测侧唯一 expected 标记。"""
            leaked = dict(kwargs)
            leaked["developer_message"] = (
                str(kwargs.get("developer_message", "")) + " M4-EXPECTED-MARKER-UNIQUE"
            )
            return original(self, **leaked)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            shutil.copytree(
                SCENARIO_ROOT / "fixtures/m4/eval/public_terms",
                root / "evals/policy-suite",
                dirs_exist_ok=True,
            )
            with mock.patch.object(m4_probe, "ROOT", root), mock.patch.object(
                m4_probe._EvalBridge, "complete", leaking_complete
            ):
                actual = m4_probe._eval_operation(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("transport_markers" in item for item in differences), differences)

    def test_real_m4_envelope_limit_mutant_is_caught_by_d08_oracle(self) -> None:
        """真实 envelope 上限放宽 1 字节时，D08 精确 2049 oracle 必须拒绝。"""
        from app.domain import policy as policy_module

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D08/size_limits"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d08_envelope_size.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root), mock.patch.object(
                policy_module, "ENVELOPE_MAX_BYTES", 2049
            ):
                actual = m4_probe._envelope_size_operation(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("over_accepted" in item for item in differences), differences)

    def test_real_m4_link_target_read_mutant_is_caught_by_d05_oracle(self) -> None:
        """真实仓库经链接目标读 pack.toml 时，D05 symlink oracle 必须拒绝。"""
        if os.name != "nt":
            self.skipTest("仅 Windows 验证 junction")
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D05/symlink_pack"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d05_symlink.json").read_text(encoding="utf-8")
        )
        original = m4_probe.FilePolicyRepository.read_pack

        def reading_link(self, pack_rel):
            """模拟先通过链接读取目标 pack.toml 再走原校验。"""
            if str(pack_rel).endswith("linked"):
                (Path(self.root) / "prompts/local/valid/pack.toml").read_text(encoding="utf-8")
            return original(self, pack_rel)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            (root / "prompts/local").mkdir(parents=True, exist_ok=True)
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(root / "prompts/local/linked"), str(root / "prompts/local/valid")],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest("当前平台/账户不能创建 junction")
            with mock.patch.object(m4_probe, "ROOT", root), mock.patch.object(
                m4_probe.FilePolicyRepository, "read_pack", reading_link
            ):
                actual = m4_probe._policy_validate_operation(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("target_reads" in item for item in differences), differences)

    def test_real_m4_simulated_acceptance_mutant_is_caught_by_d11_oracle(self) -> None:
        """模拟桥被伪装成非模拟并写验收凭证时，D11 public oracle 必须拒绝。"""
        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-D11/public_contract"]["acceptance"]["steps"][0]
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m4/probe/d11_eval_public.json").read_text(encoding="utf-8")
        )
        original = m4_probe._EvalBridge.descriptor

        def real_descriptor(self):
            """返回 simulated=false 的错误描述符。"""
            from app.ports.decision import BridgeDescriptor

            return BridgeDescriptor(
                provider_id="local-process",
                model_id="synthetic-model",
                model_config_hash="0" * 64,
                bridge="bridge_b",
                simulated=False,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            with mock.patch.object(m4_probe, "ROOT", root), mock.patch.object(
                m4_probe._EvalBridge, "descriptor", real_descriptor
            ):
                actual = m4_probe._eval_operation(request)
        invocation = {
            "exit_code": 0,
            "stdout_json": {"schema_version": 1, "status": actual["status"], **{
                key: value for key, value in actual.items() if key != "status"
            }},
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("acceptance" in item or "simulated" in item for item in differences), differences)

    def test_real_m2_sut_mutant_coverage_is_caught(self) -> None:
        """真实 M2 解析器把 coverage 降为 viewport 时，SC-U04 oracle 必须失败。"""
        from app.adapters.boss.list import load_and_parse_snapshot
        from app.usecases.sense import build_sense_payload

        assets = load_assets()
        oracle = assets.oracles["variants"]["SC-U04/full_all_absent"]["acceptance"]["steps"][0]
        manifest = SCENARIO_ROOT / "fixtures/m2/sensor/full_all_absent/manifest.json"
        with mock.patch("app.adapters.boss.list._coverage", return_value="viewport"):
            result = load_and_parse_snapshot(
                manifest,
                "00000000-0000-4000-8000-0000000000b1",
                "2026-11-24T00:00:00.000Z",
            )
        payload = build_sense_payload(result, None)
        invocation = {
            "exit_code": 0,
            "stdout_json": payload,
            "stdout_json_ok": True,
            "stdout_text": "",
            "stderr": "",
        }
        differences = compare_invocation(invocation, oracle)
        self.assertTrue(any("unread_state" in item or "stop_reason" in item for item in differences))

    def test_real_sut_mutant_skipping_task_finished_is_caught(self) -> None:
        """真实 SqliteStore 仅跳过 task_finished 时，实际场景 oracle 必须失败。"""
        from app.adapters.sqlite.repo import SqliteStore

        assets = load_assets()
        request = json.loads(
            (SCENARIO_ROOT / "fixtures/m1/l05_undetermined.json").read_text(encoding="utf-8")
        )
        oracle = assets.oracles["variants"]["SC-L05/finish_undetermined"]["acceptance"]["steps"][0]
        original = SqliteStore._insert_event

        def skip_task_finished(self, **kwargs):
            """只丢弃 task_finished，模拟漏事件缺陷。"""
            if kwargs.get("event") == "task_finished":
                return None
            return original(self, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "runtime.sqlite3"
            with mock.patch.object(SqliteStore, "_insert_event", skip_task_finished):
                actual = m1_probe.run_request(request, db, root)
            invocation = {
                "exit_code": 0,
                "stdout_json": actual,
                "stdout_json_ok": True,
                "stdout_text": "",
                "stderr": "",
            }
            differences = compare_invocation(invocation, oracle)
            self.assertTrue(any("task_finished" in item for item in differences), differences)

    def test_oracles_reject_missing_event_attempt_and_partial_lease(self) -> None:
        """关键 oracle 必须能抓住漏事件、attempt 误增与部分租约残留。"""
        l05 = self.assets.oracles["variants"]["SC-L05/finish_undetermined"]["acceptance"]
        l02 = self.assets.oracles["variants"]["SC-L02/same_task_claim"]["acceptance"]
        l05_text = json.dumps(l05, ensure_ascii=False)
        l02_text = json.dumps(l02, ensure_ascii=False)
        self.assertIn("lease_fields_all_null", l05_text)
        self.assertIn("task_finished", l05_text)
        self.assertIn("attempt_total", l02_text)
        step_expect = {
            "stdout_paths": [
                {"path": "steps.7.task.lease_fields_all_null", "equals": True},
                {"path": "steps.7.event_counts.task_finished", "equals": 1},
                {"path": "steps.1.attempt_total", "equals": 1},
            ]
        }
        actual = {
            "steps": [
                {},
                {"attempt_total": 1},
                {},
                {},
                {},
                {},
                {},
                {"task": {"lease_fields_all_null": True}, "event_counts": {"task_finished": 1}},
            ]
        }
        self.assertFalse(compare_step(actual, step_expect))
        for path, wrong in (
            ("steps.7.event_counts.task_finished", 0),
            ("steps.7.task.lease_fields_all_null", False),
            ("steps.1.attempt_total", 2),
        ):
            broken = copy.deepcopy(actual)
            parts = path.split(".")
            target = broken
            for part in parts[:-1]:
                target = target[int(part)] if part.isdigit() else target[part]
            target[parts[-1]] = wrong
            self.assertTrue(compare_step(broken, step_expect), path)


class P2M4CProbeBoundaryTests(unittest.TestCase):
    """验证 M4c 探针真正拒绝越界/链接请求，并能正常执行受控接口。"""

    def _temporary_root(self) -> tempfile.TemporaryDirectory:
        """在忽略的运行目录下创建探针临时根。"""
        base = TESTS_DIR.parent / "data" / "evals" / "probe-selftest"
        base.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=base)

    def _junction(self, root: Path, name: str, target: Path) -> Path:
        """在 Windows 合成隔离根内创建 junction；不可用时跳过。"""
        if sys.platform != "win32":
            self.skipTest("junction 自检只在 Windows 执行")
        junction = root / name
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0 or not junction.is_dir():
            self.skipTest("本机不允许创建合成 junction")
        return junction

    def test_relative_accepts_normal_request(self) -> None:
        """隔离根内普通请求路径被接受。"""
        with self._temporary_root() as temporary:
            root = Path(temporary).resolve()
            (root / "request.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(m4c_probe, "ROOT", root):
                resolved = m4c_probe._relative(
                    "request.json", "request", must_exist=True
                )
            self.assertEqual(resolved, root / "request.json")

    def test_relative_rejects_absolute_and_dotdot(self) -> None:
        """绝对路径与 .. 在读取前拒绝。"""
        with self._temporary_root() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(m4c_probe, "ROOT", root):
                with self.assertRaises(m4c_probe.ProbeError):
                    m4c_probe._relative(str(root / "request.json"), "request")
                with self.assertRaises(m4c_probe.ProbeError):
                    m4c_probe._relative("../outside.json", "request")

    def test_junction_leaf_and_barrier_are_rejected(self) -> None:
        """request/os_root/barrier 的 leaf/祖先 junction 在 resolve 前拒绝。"""
        with self._temporary_root() as temporary:
            root = Path(temporary).resolve()
            target = root / "junction-target"
            target.mkdir()
            plain = root / "plain" / "nested"
            plain.mkdir(parents=True)
            junction = self._junction(root, "junction", target)
            try:
                with mock.patch.object(m4c_probe, "ROOT", root):
                    with self.assertRaises(m4c_probe.ProbeError):
                        m4c_probe._relative("junction/request.json", "request")
                    with self.assertRaises(m4c_probe.ProbeError):
                        m4c_probe._relative("junction", "os_root")
                    with self.assertRaises(m4c_probe.ProbeError):
                        m4c_probe._relative("junction/child", "os_root")
                    self.assertEqual(
                        m4c_probe._relative("plain/nested", "os_root"),
                        plain,
                    )
                    env = {
                        "M1_BARRIER_DIR": str(junction),
                        "M1_BARRIER_NAME": "0",
                    }
                    with mock.patch.dict(os.environ, env, clear=False):
                        with self.assertRaises(m4c_probe.ProbeError):
                            m4c_probe._barrier()
                self.assertFalse((junction / "0.ready").exists())
            finally:
                junction.rmdir()

    def test_main_runs_normal_gate_lifecycle(self) -> None:
        """临时根内正常 gate_lifecycle 探针返回 ok 且释放。"""
        with self._temporary_root() as temporary:
            root = Path(temporary).resolve()
            request = {
                "operation": "gate_lifecycle",
                "profile_id": "synthetic-release",
                "os_root": "data/runtime/os-gate",
            }
            (root / "request.json").write_text(
                json.dumps(request), encoding="utf-8"
            )
            output = io.StringIO()
            with mock.patch.object(m4c_probe, "ROOT", root):
                with redirect_stdout(output):
                    code = m4c_probe.main(["--request", "request.json"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["released"], True)

    def test_main_rejects_outside_request(self) -> None:
        """越界请求由 main 返回固定错误，不读取根外文件。"""
        with self._temporary_root() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with mock.patch.object(m4c_probe, "ROOT", root):
                with redirect_stdout(output):
                    code = m4c_probe.main(["--request", "../outside.json"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["reason_code"], "ProbeError")

    def test_preflight_oracle_rejects_action_mutant(self) -> None:
        """预检 oracle 拒绝把 actions_performed 从 0 改成 1 的变异。"""
        assets = load_assets()
        expected = assets.oracles["variants"]["SC-D09/m4c_preflight"]["acceptance"]["steps"][0]
        payload = {
            "status": "ok",
            "preflight_only": True,
            "mode": "read_only",
            "actions_performed": 0,
            "decisions_performed": 0,
            "held_during_open": True,
            "state_during_open": "held",
            "gate_released": True,
        }
        self.assertEqual(compare_step(payload, expected), [])
        mutated = dict(payload, actions_performed=1)
        errors = compare_step(mutated, expected)
        self.assertTrue(any("actions_performed" in error for error in errors), errors)

    def test_facts_oracle_rejects_forget_residue(self) -> None:
        """事实 oracle 拒绝 forget 后仍残留事实行的变异。"""
        assets = load_assets()
        expected = assets.oracles["variants"]["SC-D04/m4c_facts_flow"]["acceptance"]["steps"][0]
        payload = {
            "status": "ok",
            "propose_action": "set",
            "propose_confirmed": False,
            "apply_action": "set",
            "apply_confirmed": True,
            "after_count": 1,
            "after_revoke_count": 0,
            "raw_fact_rows_after_forget": 0,
            "raw_fact_events_after_forget": 3,
            "value_visible_in_local_show": True,
        }
        self.assertEqual(compare_step(payload, expected), [])
        mutated = dict(payload, raw_fact_rows_after_forget=1)
        errors = compare_step(mutated, expected)
        self.assertTrue(any("raw_fact_rows_after_forget" in error for error in errors), errors)

    def test_shared_gate_oracle_rejects_identity_mutant(self) -> None:
        """共享门禁 oracle 拒绝篡改 owner 或把 active/unknown 当 stopped。"""
        assets = load_assets()
        expected = assets.oracles["variants"]["SC-D10/m4c_activity"]["acceptance"]["steps"][0]
        payload = {
            "status": "ok",
            "stopped_assessment": "stopped",
            "active_assessment": "active",
            "unknown_assessment": "unknown",
            "directory_profile_assessment": "unknown",
        }
        self.assertEqual(compare_step(payload, expected), [])
        mutated = dict(payload, active_assessment="unknown")
        errors = compare_step(mutated, expected)
        self.assertTrue(any("active_assessment" in error for error in errors), errors)

    def test_d06_facts_entry_oracle_rejects_forget_residue(self) -> None:
        """D06 事实入口 oracle 必须拒绝 forget 后仍残留真实行。"""
        assets = load_assets()
        expected = assets.oracles["variants"]["SC-D06/m4c_facts_entry"]["acceptance"]["steps"][0]
        payload = {
            "status": "ok",
            "before_count": 0,
            "propose_confirmed": False,
            "apply_confirmed": True,
            "after_revoke_count": 0,
            "raw_fact_rows_after_forget": 0,
            "raw_fact_events_after_forget": 3,
        }
        self.assertEqual(compare_step(payload, expected), [])
        mutated = dict(payload, raw_fact_rows_after_forget=1)
        errors = compare_step(mutated, expected)
        self.assertTrue(any("raw_fact_rows_after_forget" in error for error in errors), errors)

    def test_lf_inputs_normalizes_copy_and_rejects_invalid_paths(self) -> None:
        """隔离副本的 LF 规范化只改传入根，并在越界路径前拒绝。"""
        with self._temporary_root() as temporary:
            root = Path(temporary).resolve()
            target = root / "copy.txt"
            target.write_bytes(b"a\r\nb\r\n")
            runner._normalize_text_file(target)
            self.assertEqual(target.read_bytes(), b"a\nb\n")
            target.write_bytes(b"a\r\nb\r\n")
            runner._normalize_lf_inputs(root, ["copy.txt"])
            self.assertEqual(target.read_bytes(), b"a\nb\n")
            binary = root / "binary.bin"
            binary.write_bytes(b"\xff\r\n")
            runner._normalize_text_file(binary)
            self.assertEqual(binary.read_bytes(), b"\xff\r\n")
            bom = root / "bom.txt"
            bom.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
            runner._normalize_text_file(bom)
            self.assertEqual(bom.read_bytes(), b"\xef\xbb\xbfa\r\nb\r\n")
            nul = root / "nul.txt"
            nul.write_bytes(b"a\r\n\x00b\r\n")
            runner._normalize_text_file(nul)
            self.assertEqual(nul.read_bytes(), b"a\r\n\x00b\r\n")
            for invalid in ("../outside.txt", str(root / "copy.txt")):
                with self.assertRaises(runner.IsolationError):
                    runner._normalize_lf_inputs(root, [invalid])
            with self.assertRaises(runner.IsolationError):
                runner._normalize_lf_inputs(root, ["missing.txt"])

    def test_m4_probe_contract_error_has_safe_json(self) -> None:
        """真实 PolicyError 由探针输出固定 JSON，不回显 traceback 或绝对路径。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_m4_project(root)
            request = {"operation": "hash_mutations", "pack": "prompts/local/valid"}
            (root / "request.json").write_text(json.dumps(request), encoding="utf-8")
            output = io.StringIO()
            errors = io.StringIO()
            with mock.patch.object(m4_probe, "ROOT", root):
                with redirect_stdout(output), redirect_stderr(errors):
                    normal = m4_probe.main(["--request", "request.json"])
                (root / "prompts/local/valid/pack.toml").write_text(
                    "invalid =\n", encoding="utf-8"
                )
                output = io.StringIO()
                errors = io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    broken = m4_probe.main(["--request", "request.json"])
            self.assertEqual(normal, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(broken, 0)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["reason_code"], "invalid_policy")
            self.assertEqual(errors.getvalue(), "")
            self.assertNotIn("Traceback", output.getvalue())
            self.assertNotIn(str(root), output.getvalue())

    def _race_payloads(self) -> tuple[dict, dict]:
        """构造一份 winner/loser 合成 payload，仅用于比较器自检。"""
        winner = {
            "status": "ok",
            "outcome": "held",
            "owner_id": "00000000-0000-4000-8000-000000000081",
            "owner_pid": 1,
            "owner_host": "a" * 64,
            "profile_id": "synthetic-alpha",
            "profile_path_token": "1" * 64,
            "session_nonce": "00000000-0000-4000-8000-0000000000aa",
            "shared_owner_id": "00000000-0000-4000-8000-000000000081",
            "shared_owner_pid": 1,
            "shared_owner_host": "a" * 64,
            "shared_nonce": "00000000-0000-4000-8000-0000000000aa",
            "shared_profile_id": "synthetic-alpha",
            "shared_profile_path_token": "1" * 64,
            "shared_state": "held",
            "shared_owner_matches_self": True,
            "shared_nonce_matches_session": True,
            "shared_profile_matches_self": True,
        }
        loser = {
            "status": "ok",
            "outcome": "busy",
            "owner_id": "00000000-0000-4000-8000-000000000082",
            "owner_pid": 2,
            "owner_host": "a" * 64,
            "profile_id": "synthetic-beta",
            "profile_path_token": "2" * 64,
            "session_nonce": None,
            "shared_owner_id": "00000000-0000-4000-8000-000000000081",
            "shared_owner_pid": 1,
            "shared_owner_host": "a" * 64,
            "shared_nonce": "00000000-0000-4000-8000-0000000000aa",
            "shared_profile_id": "synthetic-alpha",
            "shared_profile_path_token": "1" * 64,
            "shared_state": "held",
            "shared_owner_matches_self": False,
            "shared_nonce_matches_session": False,
            "shared_profile_matches_self": False,
        }
        return winner, loser

    def test_children_relation_accepts_winner_row(self) -> None:
        """跨子进程关联接受 loser 读取的共享行等于唯一 winner。"""
        winner, loser = self._race_payloads()
        relation = {
            "winner_value": "held",
            "loser_value": "busy",
            "pairs": [
                {"winner": "owner_id", "loser": "shared_owner_id"},
                {"winner": "owner_pid", "loser": "shared_owner_pid"},
                {"winner": "owner_host", "loser": "shared_owner_host"},
                {"winner": "session_nonce", "loser": "shared_nonce"},
                {"winner": "profile_id", "loser": "shared_profile_id"},
                {"winner": "profile_path_token", "loser": "shared_profile_path_token"},
            ],
            "winner_paths": [
                {"path": "shared_owner_matches_self", "equals": True},
                {"path": "shared_nonce_matches_session", "equals": True},
                {"path": "shared_profile_matches_self", "equals": True},
            ],
            "loser_paths": [
                {"path": "shared_owner_matches_self", "equals": False},
                {"path": "shared_nonce_matches_session", "equals": False},
                {"path": "shared_profile_matches_self", "equals": False},
            ],
        }
        children = [
            {"stdout_json": winner},
            {"stdout_json": loser},
        ]
        self.assertEqual(runner._compare_children_relation(children, relation), [])
        wrong_owner = dict(loser, shared_owner_id="00000000-0000-4000-8000-000000000099")
        errors = runner._compare_children_relation(
            [{"stdout_json": winner}, {"stdout_json": wrong_owner}], relation
        )
        self.assertTrue(any("shared_owner_id" in error for error in errors), errors)
        for field in ("shared_nonce", "shared_profile_path_token"):
            wrong = dict(loser, **{field: "9" * 64})
            errors = runner._compare_children_relation(
                [{"stdout_json": winner}, {"stdout_json": wrong}], relation
            )
            self.assertTrue(any(field in error for error in errors), errors)
        two_winners = [
            {"stdout_json": winner},
            {"stdout_json": dict(loser, outcome="held")},
        ]
        errors = runner._compare_children_relation(two_winners, relation)
        self.assertTrue(any("恰一" in error for error in errors), errors)


class BuilderProvenanceTests(unittest.TestCase):
    """确认清单可追溯到作者脚本，且脚本存在的判据字段未被手改丢失。"""

    def test_builder_source_exists_and_catalogue_points_to_it(self) -> None:
        """catalogue.generated_from 必须指向实际存在的作者脚本。"""
        catalogue = json.loads((SCENARIO_ROOT / "catalogue.json").read_text(encoding="utf-8"))
        relative = catalogue["generated_from"]
        self.assertTrue((SCENARIO_ROOT / relative).is_file())
        spec = importlib.util.spec_from_file_location("build_assets", SCENARIO_ROOT / relative)
        self.assertIsNotNone(spec)


class FactsCliPersistenceTests(unittest.TestCase):
    """用真实 `python -m app facts` 与合成 LOCALAPPDATA 验证持久写入/拒写。"""

    def setUp(self) -> None:
        """建立独立临时项目根与合成 OS 根，不读真实配置或门禁。"""
        self.temporary = tempfile.TemporaryDirectory(
            dir=TESTS_DIR.parent, prefix=".m5-facts-"
        )
        self.root = Path(self.temporary.name)
        (self.root / "localappdata").mkdir()
        shutil.copytree(TESTS_DIR.parent / "app", self.root / "app")

    def tearDown(self) -> None:
        """只清理本测试自建临时根。"""
        self.temporary.cleanup()

    def _env(self) -> dict[str, str]:
        """构造最小环境：仅合成 LOCALAPPDATA/HOME 与项目 PYTHONPATH。"""
        env = {
            key: os.environ[key]
            for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")
            if key in os.environ
        }
        env.update(
            {
                "PYTHONPATH": str(self.root),
                "PYTHONNOUSERSITE": "1",
                "LOCALAPPDATA": str(self.root / "localappdata"),
                "APPDATA": str(self.root / "appdata"),
                "HOME": str(self.root / "home"),
                "USERPROFILE": str(self.root / "home"),
                "TEMP": str(self.root / "tmp"),
                "TMP": str(self.root / "tmp"),
                "PATH": "",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        for name in ("appdata", "home", "tmp"):
            (self.root / name).mkdir(exist_ok=True)
        return env

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        """运行真实 facts CLI，返回 stdout JSON 与退出码。"""
        return subprocess.run(
            [sys.executable, "-m", "app", "facts", *args],
            cwd=self.root,
            env=self._env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
            check=False,
        )

    def _profile_db(self, profile_id: str = "demo") -> Path:
        """返回隔离根内真实 profile 库路径。"""
        return (
            self.root
            / "data"
            / "runtime"
            / "profiles"
            / profile_id
            / "runtime.sqlite3"
        )

    def _fact_rows(self, profile_id: str = "demo") -> list[tuple]:
        """只读返回含合成值的事实与无值事件投影，用于前后比较。"""
        connection = sqlite3.connect(str(self._profile_db(profile_id)))
        try:
            facts = connection.execute(
                "SELECT fact_key, state, value, source_id, updated_at "
                "FROM profile_facts ORDER BY fact_key, source_id"
            ).fetchall()
            events = connection.execute(
                "SELECT fact_key, action, source_id, created_at "
                "FROM profile_fact_events ORDER BY fact_key, action, source_id"
            ).fetchall()
        finally:
            connection.close()
        return [tuple(row) for row in facts] + [tuple(row) for row in events]

    def _gate(self, profile_id: str):
        """建立指向隔离 OS 根的真实共享门禁对象。"""
        from app.adapters.sqlite.device_gate import SharedDeviceGate

        return SharedDeviceGate(
            profile_id=profile_id,
            profile_db_path=self._profile_db(profile_id),
            os_root=self.root / "localappdata",
        )

    def _acquire(self, profile_id: str):
        """以本进程合成 owner 真实取得唯一 primary 占用并保留持有对象。"""
        from app.domain.models import Owner

        gate = self._gate(profile_id)
        gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(self._profile_db(profile_id)),
            owner=Owner("00000000-0000-4000-8000-0000000000b1", 5150, "d" * 64),
            now="2026-11-24T00:00:00.000Z",
        )
        return gate

    def test_idle_set_revoke_forget_persists_without_values_in_audit(self) -> None:
        """空闲合法正例：真实 set/revoke/forget 持久化且遗忘不保留值。"""
        set_result = self._cli(
            "set", "--profile", "demo", "--key", "work_location",
            "--value", "合成地点甲", "--confirm",
        )
        self.assertEqual(set_result.returncode, 0, set_result.stdout)
        payload = json.loads(set_result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["result"]["display_value"], "合成地点甲")
        self.assertTrue(self._profile_db().is_file())
        rows = self._fact_rows()
        self.assertTrue(any(row[1] == "active" for row in rows if row[0] == "work_location"))
        revoke = self._cli(
            "revoke", "--profile", "demo", "--key", "work_location", "--confirm"
        )
        self.assertEqual(revoke.returncode, 0, revoke.stdout)
        self.assertFalse(
            any(row[1] == "active" for row in self._fact_rows() if row[0] == "work_location")
        )
        forget = self._cli(
            "forget", "--profile", "demo", "--key", "work_location", "--confirm"
        )
        self.assertEqual(forget.returncode, 0, forget.stdout)
        connection = sqlite3.connect(str(self._profile_db()))
        try:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM profile_facts WHERE value = ?",
                ("合成地点甲",),
            ).fetchone()[0]
            audit_columns = connection.execute(
                "SELECT COUNT(*) FROM profile_fact_events "
                "WHERE previous_source_ids_json LIKE ?",
                ("%合成地点甲%",),
            ).fetchone()[0]
        finally:
            connection.close()
        # 产品只承诺删除事实行与无值审计，不宣称擦除 WAL/空闲页；这里只核对表内值。
        self.assertEqual(remaining, 0)
        self.assertEqual(audit_columns, 0)

    def test_held_same_profile_refuses_all_writes_and_keeps_rows(self) -> None:
        """同 profile held 时 set/revoke/forget 均拒写，事实/事件/门禁行不变。"""
        self.assertEqual(
            self._cli(
                "set", "--profile", "demo", "--key", "work_location",
                "--value", "合成地点甲", "--confirm",
            ).returncode,
            0,
        )
        before_rows = self._fact_rows()
        self._acquire("demo")
        gate = self._gate("demo")
        before_gate = gate.inspect()
        attempts = (
            ("set", "--profile", "demo", "--key", "work_location",
             "--value", "合成地点乙", "--confirm"),
            ("revoke", "--profile", "demo", "--key", "work_location", "--confirm"),
            ("forget", "--profile", "demo", "--key", "work_location", "--confirm"),
        )
        for args in attempts:
            completed = self._cli(*args)
            self.assertNotEqual(completed.returncode, 0, args)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "failed", args)
            self.assertEqual(payload["reason_code"], "device_busy", args)
        self.assertEqual(self._fact_rows(), before_rows)
        self.assertEqual(gate.inspect(), before_gate)

    def test_quarantined_same_profile_refuses_without_creating_profile(self) -> None:
        """同 profile quarantined 时拒写且不创建目标 profile 库。"""
        gate = self._acquire("fresh")
        gate.release(now="2026-11-24T00:00:00.000Z", converged=False)
        before_gate = gate.inspect()
        completed = self._cli(
            "set", "--profile", "fresh", "--key", "work_location",
            "--value", "合成地点甲", "--confirm",
        )
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["reason_code"], "device_quarantined")
        self.assertFalse(self._profile_db("fresh").exists())
        self.assertEqual(gate.inspect(), before_gate)

    def test_unknown_gate_path_refuses_before_any_profile_write(self) -> None:
        """门禁路径不可判定时失败关闭，不建立新 profile。"""
        from app.adapters.sqlite.device_gate import guard_database_path

        database = guard_database_path(self.root / "localappdata")
        database.parent.mkdir(parents=True, exist_ok=True)
        database.mkdir()
        completed = self._cli(
            "set", "--profile", "fresh", "--key", "work_location",
            "--value", "合成地点甲", "--confirm",
        )
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertIn(
            payload["reason_code"], {"device_gate_io_error", "device_busy"}
        )
        self.assertFalse(self._profile_db("fresh").exists())

    def test_gate_exclusive_lock_refuses_without_partial_writes(self) -> None:
        """真实 SQLite 排他锁下 CLI 拒写且已有事实/门禁行不变。"""
        self.assertEqual(
            self._cli(
                "set", "--profile", "demo", "--key", "work_location",
                "--value", "合成地点甲", "--confirm",
            ).returncode,
            0,
        )
        self._acquire("other-gate")
        before_rows = self._fact_rows()
        gate = self._gate("other-gate")
        before_gate = gate.inspect()
        from app.adapters.sqlite.device_gate import guard_database_path

        lock = sqlite3.connect(str(guard_database_path(self.root / "localappdata")))
        try:
            lock.execute("BEGIN EXCLUSIVE")
            completed = self._cli(
                "set", "--profile", "demo", "--key", "salary_expectation",
                "--value", "合成薪资甲", "--confirm",
            )
        finally:
            lock.rollback()
            lock.close()
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["reason_code"], "device_busy")
        self.assertEqual(self._fact_rows(), before_rows)
        self.assertEqual(gate.inspect(), before_gate)

    def test_synthetic_localappdata_is_not_real_os_root(self) -> None:
        """全部路径都在临时根内，未触碰真实 LOCALAPPDATA。"""
        completed = self._cli(
            "set", "--profile", "demo", "--key", "work_location",
            "--value", "合成地点甲", "--confirm",
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        from app.adapters.sqlite.device_gate import guard_database_path

        database = guard_database_path(self.root / "localappdata")
        self.assertTrue(str(database).startswith(str(self.root)))
        self.assertTrue(self._profile_db().is_file())


class M5LogAndLedgerOracleTests(unittest.TestCase):
    """用原 A01_FE 判据证明原始 JSONL/账本变异会被拒绝，不引入宽松比较器。

    基线不是预计算字典：每个用例把合成但真实格式的原始行写入临时目录，再调用
    现有 `_log_projection`/`_ledger_summary` 重新投影，最后送同一原判据。
    """

    @classmethod
    def setUpClass(cls) -> None:
        """加载一次原独立预期，只取日志与账本路径。"""
        cls.assets = load_assets()
        acceptance = cls.assets.oracles["variants"]["SC-A01/A01_FE"]["acceptance"]
        cls.paths = acceptance["steps"][0]["stdout_paths"]
        cls.log_paths = [
            item for item in cls.paths if str(item["path"]).startswith("log_")
        ]
        cls.ledger_paths = [
            item for item in cls.paths if str(item["path"]).startswith("ledger.")
        ]

    def setUp(self) -> None:
        """为每个用例建立独立临时原始数据目录。"""
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    @staticmethod
    def _a01_fe_rows() -> list[dict]:
        """构造与固定 A01_FE 原始 action 序列同形的新合成 JSONL 行。

        序列来自固定 92ec 的合成 A01_FE 日志：7 个带标签 perform 实例 + 6 组
        共享采集无标签 finish = 13 个 finish 组，另有动作/仓储生命周期失败。
        这里只重建原始字段，不读取 expected，也不从产品结论反推输入。
        """
        correlation = "00000000-0000-4000-8000-0000000000fe"
        sequence = [
            ("action", "action_validate", "started", None, None, None),
            ("action", "action_validate", "ok", None, None, None),
            ("action", "action_admit", "started", None, None, None),
            ("action", "begin_action", "started", None, None, None),
            ("action", "begin_action", "ok", None, None, None),
            ("action", "action_admit", "ok", None, None, None),
            ("action", "action_execute", "started", None, None, None),
            ("action", "perform", "started", "adb_action_ime_get", None, None),
            ("action", "action_finish_command", "started", "adb_action_ime_get", None, None),
            ("action", "action_finish_command", "ok", "adb_action_ime_get", None, None),
            ("action", "perform", "ok", "adb_action_ime_get", None, None),
            ("action", "save_ime", "started", None, None, None),
            ("action", "save_ime", "ok", None, None, None),
            ("action", "get_action", "started", None, None, None),
            ("action", "get_action", "ok", None, None, None),
            ("action", "action_finish_command", "started", None, None, None),
            ("action", "action_finish_command", "ok", None, None, None),
            ("action", "action_finish_command", "started", None, None, None),
            ("action", "action_finish_command", "ok", None, None, None),
            ("action", "perform", "started", "adb_action_ime_set", None, None),
            ("action", "action_finish_command", "started", "adb_action_ime_set", None, None),
            ("action", "action_finish_command", "ok", "adb_action_ime_set", None, None),
            ("action", "perform", "ok", "adb_action_ime_set", None, None),
            ("action", "perform", "started", "adb_action_ime_get", None, None),
            ("action", "action_finish_command", "started", "adb_action_ime_get", None, None),
            ("action", "action_finish_command", "ok", "adb_action_ime_get", None, None),
            ("action", "perform", "ok", "adb_action_ime_get", None, None),
            ("action", "perform", "started", "adb_action_input", None, None),
            ("action", "action_finish_command", "started", "adb_action_input", None, None),
            ("action", "action_finish_command", "ok", "adb_action_input", None, None),
            ("action", "perform", "ok", "adb_action_input", None, None),
            ("action", "action_finish_command", "started", None, None, None),
            ("action", "action_finish_command", "ok", None, None, None),
            ("action", "action_finish_command", "started", None, None, None),
            ("action", "action_finish_command", "ok", None, None, None),
            ("action", "perform", "started", "adb_action_tap", None, None),
            ("action", "action_finish_command", "started", "adb_action_tap", None, None),
            ("action", "action_finish_command", "ok", "adb_action_tap", None, None),
            ("action", "perform", "ok", "adb_action_tap", None, None),
            ("action", "action_finish_command", "started", None, None, None),
            ("action", "action_finish_command", "ok", None, None, None),
            ("action", "action_finish_command", "started", None, None, None),
            ("action", "action_finish_command", "ok", None, None, None),
            ("action", "perform", "started", "adb_action_ime_set", None, None),
            ("action", "action_finish_command", "started", "adb_action_ime_set", None, None),
            ("action", "action_finish_command", "ok", "adb_action_ime_set", None, None),
            ("action", "perform", "ok", "adb_action_ime_set", None, None),
            ("action", "perform", "started", "adb_action_ime_get", None, None),
            ("action", "action_finish_command", "started", "adb_action_ime_get", None, None),
            ("action", "action_finish_command", "ok", "adb_action_ime_get", None, None),
            ("action", "perform", "ok", "adb_action_ime_get", None, None),
            ("action", "action_execute", "ok", None, None, None),
            ("action", "action_derive", "started", None, None, None),
            ("action", "action_derive", "ok", None, None, None),
            ("action", "action_commit", "started", None, None, None),
            ("action", "complete_action", "started", None, None, None),
            ("action", "complete_action", "failed", None, "store_io_error", "store"),
            ("action", "action_commit", "failed", None, "store_io_error", "action"),
            ("action", "action_validate", "started", None, None, None),
            ("action", "action_validate", "ok", None, None, None),
            ("action", "action_admit", "started", None, None, None),
            ("action", "begin_action", "started", None, None, None),
            ("action", "begin_action", "failed", None, "action_conflict", "store"),
            ("action", "action_admit", "failed", None, "action_conflict", "action"),
        ]
        rows = []
        for index, (stage, operation, result, command, reason, error) in enumerate(
            sequence
        ):
            rows.append(
                {
                    "schema_version": 1,
                    "ts": "2026-11-25T00:00:00+00:00",
                    "correlation_id": correlation,
                    "stage": stage,
                    "operation": operation,
                    "result": result,
                    "reason_code": reason,
                    "error_category": error,
                    "duration_ms": 0,
                    "command": command,
                    "case_index": index,
                }
            )
        return rows

    @staticmethod
    def _finish_indices(rows: list[dict], command: str) -> list[int]:
        """返回指定命令的 finish started/终态原始行下标。"""
        return [
            index
            for index, item in enumerate(rows)
            if item.get("operation") == "action_finish_command"
            and item.get("command") == command
        ]

    @staticmethod
    def _remove_group(rows: list[dict], command: str, *, instance: int = 0) -> list[dict]:
        """删除指定命令第 N 个实例的 finish 组，保留其余行原样。"""
        indices = M5LogAndLedgerOracleTests._finish_indices(rows, command)
        if len(indices) < 2 * (instance + 1):
            raise AssertionError("finish group missing")
        removed = set(indices[2 * instance : 2 * instance + 2])
        return [copy.deepcopy(item) for index, item in enumerate(rows) if index not in removed]

    @staticmethod
    def _duplicate_group(rows: list[dict], command: str, *, instance: int = 0) -> list[dict]:
        """把指定命令第 N 个实例的 finish 组复制到该组终态之后。"""
        indices = M5LogAndLedgerOracleTests._finish_indices(rows, command)
        terminal = indices[2 * instance + 1]
        group = [copy.deepcopy(rows[indices[2 * instance]]), copy.deepcopy(rows[terminal])]
        mutated: list[dict] = []
        for index, item in enumerate(rows):
            mutated.append(copy.deepcopy(item))
            if index == terminal:
                mutated.extend(copy.deepcopy(value) for value in group)
        return mutated

    @staticmethod
    def _move_group(
        rows: list[dict], source: str, target: str, *, target_instance: int = 0
    ) -> list[dict]:
        """把 source 的 finish 组移动到 target 已有 perform 窗口内。"""
        source_indices = M5LogAndLedgerOracleTests._finish_indices(rows, source)
        group = [copy.deepcopy(rows[source_indices[0]]), copy.deepcopy(rows[source_indices[1]])]
        target_indices = M5LogAndLedgerOracleTests._finish_indices(rows, target)
        anchor = target_indices[2 * target_instance + 1]
        mutated: list[dict] = []
        for index, item in enumerate(rows):
            if index in {source_indices[0], source_indices[1]}:
                continue
            mutated.append(copy.deepcopy(item))
            if index == anchor:
                mutated.extend(copy.deepcopy(value) for value in group)
        return mutated

    def test_input_finish_replaced_by_second_tap_finish_is_rejected(self) -> None:
        """固定 92 实际 FE 变异：删 input finish、重复 tap finish 必须被拒绝。"""
        rows = self._a01_fe_rows()
        input_indices = self._finish_indices(rows, "adb_action_input")
        tap_indices = self._finish_indices(rows, "adb_action_tap")
        self.assertEqual(len(input_indices), 2)
        self.assertEqual([rows[i]["result"] for i in tap_indices], ["started", "ok"])
        tap_group = [copy.deepcopy(rows[i]) for i in tap_indices]
        mutated: list[dict] = []
        for index, item in enumerate(rows):
            if index in set(input_indices):
                continue
            mutated.append(copy.deepcopy(item))
            if index == tap_indices[1]:
                mutated.extend(copy.deepcopy(value) for value in tap_group)
        result = self._project(mutated)
        self.assertEqual(result["log_finish"]["started"], 13)
        self.assertEqual(result["log_finish"]["ok"], 13)
        self.assertGreater(result["log_association_errors"], 0)
        self.assertGreater(result["log_finish"]["unpaired"], 0)
        self.assertNotIn("adb_action_input", result["log_finish_by_command"])
        self.assertEqual(result["log_finish_by_command"]["adb_action_tap"]["started"], 2)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_finish_moved_to_other_existing_command_is_rejected(self) -> None:
        """把 finish 移到另一条已有 started 的合法命令必须被拒绝。"""
        rows = self._move_group(
            self._a01_fe_rows(), "adb_action_input", "adb_action_ime_set"
        )
        result = self._project(rows)
        self.assertGreater(result["log_association_errors"], 0)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_duplicate_finish_group_is_rejected(self) -> None:
        """同一 perform 内重复一组 finish 必须被拒绝。"""
        rows = self._duplicate_group(self._a01_fe_rows(), "adb_action_input")
        result = self._project(rows)
        self.assertGreater(result["log_association_errors"], 0)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_misordered_finish_group_is_rejected(self) -> None:
        """finish 终态先于 started 的乱序必须被拒绝。"""
        rows = self._a01_fe_rows()
        indices = self._finish_indices(rows, "adb_action_input")
        swapped = [copy.deepcopy(item) for item in rows]
        swapped[indices[0]], swapped[indices[1]] = swapped[indices[1]], swapped[indices[0]]
        result = self._project(swapped)
        self.assertGreater(result["log_association_errors"], 0)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_same_label_instance_boundaries_are_enforced(self) -> None:
        """相同 label 的两个实例必须各自恰一组 finish，不能互相顶替。"""
        rows = self._a01_fe_rows()
        missing = self._remove_group(rows, "adb_action_ime_get", instance=0)
        missing_result = self._project(missing)
        self.assertGreater(missing_result["log_association_errors"], 0)
        self.assertTrue(
            compare_step(missing_result, {"stdout_paths": self.log_paths})
        )
        duplicated = self._duplicate_group(rows, "adb_action_ime_get", instance=1)
        duplicate_result = self._project(duplicated)
        self.assertGreater(duplicate_result["log_association_errors"], 0)
        self.assertTrue(
            compare_step(duplicate_result, {"stdout_paths": self.log_paths})
        )

    def test_unlabeled_shared_finishes_remain_independent(self) -> None:
        """共享采集无标签 finish 保持独立聚合，不伪造命令归属或 unpaired。"""
        result = self._project(self._a01_fe_rows())
        self.assertEqual(result["log_finish"]["unpaired"], 0)
        self.assertEqual(result["log_association_errors"], 0)
        self.assertEqual(
            result["log_finish_by_command"][""],
            {"started": 6, "ok": 6, "failed": 0},
        )
        self.assertEqual(result["log_reasons_by_command"], {})

    def _project(
        self, rows: list[dict], *, forbidden: tuple[str, ...] = (), root: Path | None = None
    ) -> dict:
        """把原始行写入独立日志文件，再调用现有投影输出结果形状。"""
        from app.process_log import ProcessLog

        log = ProcessLog(path=(root or self.root) / "m5-a01-fe.jsonl")
        text = "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n"
        log.path.write_text(text, encoding="utf-8", newline="\n")
        if forbidden:
            projection = m5_probe._log_projection(log, forbidden_values=forbidden)
            return {
                "log_schema_violations": projection["schema_violations"],
                "log_sensitive_hits": projection["sensitive_hits"],
                "log_finish": projection["finish"],
                "log_finish_by_command": projection["finish_by_command"],
                "log_reasons": projection["reasons"],
                "log_reasons_by_command": projection["reasons_by_command"],
                "log_association_errors": projection["association_errors"],
                "log_pairs": m5_probe._log_pairs(log),
                "log_correlations": m5_probe._log_correlations(log),
            }
        result: dict = {}
        m5_probe._attach_log(result, log)
        return result

    def _production_ledger(self, count: int = 13, root: Path | None = None):
        """用生产写入器生成真实格式账本，返回目录、动作键与生产 checker。"""
        from app.adapters.adb.command import CommandIdentityStore

        directory = (root or self.root) / "ledger"
        store = CommandIdentityStore(directory)
        key = "synthetic-a01-fe-action"
        for index in range(count):
            pid = 7100 + index
            store.mark_started(key)
            store.record(key, pid)
            store.finish_family(key, pid, lambda _pid: [], bounded=True, proven=True)
        return directory, key, store

    @staticmethod
    def _ledger_entries(directory: Path) -> tuple[Path, list[dict]]:
        """读取唯一账本文件的原始行，供变异后重写。"""
        path = next(directory.glob("*.jsonl"))
        return path, [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _rewrite_ledger(path: Path, entries: list[dict]) -> None:
        """把变异后的账本行按原始 JSONL 写回同一路径。"""
        path.write_text(
            "\n".join(json.dumps(item, separators=(",", ":")) for item in entries)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_raw_baseline_accepts_original_a01_fe_criteria(self) -> None:
        """原始行基线经现有投影后满足原 A01_FE 日志/账本判据。"""
        directory, key, store = self._production_ledger()
        summary = m5_probe._ledger_summary(directory)
        self.assertEqual(summary["files"], 1)
        self.assertEqual(summary["begin"], 13)
        self.assertEqual(summary["complete"], 13)
        self.assertEqual(summary["pids"], 13)
        self.assertEqual(summary["unresolved"], 0)
        self.assertTrue(summary["closed"])
        self.assertEqual(store.check(key, 1, lambda _pid: "stopped"), "stopped")

        result = self._project(self._a01_fe_rows())
        result["ledger"] = summary
        self.assertEqual(result["log_finish"]["started"], 13)
        self.assertEqual(result["log_finish"]["unpaired"], 0)
        self.assertEqual(
            sum(bucket["started"] for bucket in result["log_finish_by_command"].values()),
            13,
        )
        self.assertEqual(
            result["log_pairs"]["action_commit"], ["started", "failed"]
        )
        self.assertEqual(
            compare_step(result, {"stdout_paths": self.log_paths + self.ledger_paths}),
            [],
        )

    def test_missing_begin_is_rejected_by_ledger_rule(self) -> None:
        """删掉真实 begin 后统计不得把 pid 当 begin，生产 checker 也拒绝。"""
        directory, key, store = self._production_ledger()
        path, entries = self._ledger_entries(directory)
        self._rewrite_ledger(
            path, [item for item in entries if item.get("begin") is not True]
        )
        summary = m5_probe._ledger_summary(directory)
        self.assertEqual(summary["begin"], 0)
        self.assertEqual(summary["pids"], 13)
        self.assertFalse(summary["closed"])
        self.assertEqual(store.check(key, 1, lambda _pid: "stopped"), "unknown")
        mutated = self._project(self._a01_fe_rows())
        mutated["ledger"] = summary
        self.assertTrue(
            compare_step(mutated, {"stdout_paths": self.ledger_paths})
        )

    def test_missing_complete_multiple_files_and_bad_lines_are_rejected(self) -> None:
        """缺 complete、多文件、坏行都不得默认为闭合。"""
        for name in ("missing_complete", "multiple_files", "bad_line"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    case_root = Path(temporary)
                    directory, _key, _store = self._production_ledger(root=case_root)
                    path, entries = self._ledger_entries(directory)
                    if name == "missing_complete":
                        index = max(
                            i for i, item in enumerate(entries) if item.get("complete")
                        )
                        entries.pop(index)
                        self._rewrite_ledger(path, entries)
                    elif name == "multiple_files":
                        (directory / "action-second.jsonl").write_text(
                            path.read_text(encoding="utf-8"),
                            encoding="utf-8",
                            newline="\n",
                        )
                    else:
                        with path.open("a", encoding="utf-8", newline="\n") as handle:
                            handle.write("{not-json\n")
                    summary = m5_probe._ledger_summary(directory)
                    self.assertFalse(summary["closed"], name)
                    mutated = self._project(self._a01_fe_rows(), root=case_root)
                    mutated["ledger"] = summary
                    self.assertTrue(
                        compare_step(mutated, {"stdout_paths": self.ledger_paths}),
                        name,
                    )

    def test_out_of_order_ledger_is_rejected(self) -> None:
        """complete 先于 begin 时场景严格证据完整性拒绝，生产计数/停止检查接受。

        `_ledger_summary.closed` 是场景层严格证据完整性，不是生产
        `CommandIdentityStore.check` 的返回值；两者语义差异如实保留，不据此
        复用私有 `_load` 或扩大生产语义。
        """
        directory, key, store = self._production_ledger()
        path, entries = self._ledger_entries(directory)
        index = next(i for i, item in enumerate(entries) if item.get("complete"))
        entries.insert(0, entries.pop(index))
        self._rewrite_ledger(path, entries)
        summary = m5_probe._ledger_summary(directory)
        self.assertGreater(summary["order_errors"], 0)
        self.assertFalse(summary["closed"])
        self.assertEqual(store.check(key, 1, lambda _pid: "stopped"), "stopped")
        mutated = self._project(self._a01_fe_rows())
        mutated["ledger"] = summary
        self.assertTrue(compare_step(mutated, {"stdout_paths": self.ledger_paths}))

    def test_missing_finish_command_is_rejected(self) -> None:
        """删掉 action_finish_command 后原日志判据必须失败。"""
        rows = [
            item
            for item in self._a01_fe_rows()
            if item.get("operation") != "action_finish_command"
        ]
        result = self._project(rows)
        self.assertEqual(result["log_finish"]["started"], 0)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_wrong_failure_classification_is_rejected(self) -> None:
        """把完成事务失败改成别的分类必须被原判据拒绝。"""
        rows = copy.deepcopy(self._a01_fe_rows())
        for item in rows:
            if item.get("operation") == "action_commit" and item.get("result") == "failed":
                item["error_category"] = "device"
        result = self._project(rows)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_illegal_field_or_sensitive_payload_is_rejected(self) -> None:
        """非法字段和合成正文/附件名/坐标载荷均被原判据拒绝。"""
        illegal = self._a01_fe_rows() + [
            {
                "schema_version": 1,
                "stage": "action",
                "operation": "action_extra",
                "result": "ok",
                "extra_field": "x",
            }
        ]
        self.assertGreater(self._project(illegal)["log_schema_violations"], 0)
        self.assertTrue(
            compare_step(self._project(illegal), {"stdout_paths": self.log_paths})
        )
        for payload in ("合成回复正文", "合成附件简历.pdf", "10,20"):
            with self.subTest(payload=payload):
                rows = self._a01_fe_rows() + [
                    {
                        "schema_version": 1,
                        "stage": "store",
                        "operation": payload,
                        "result": "ok",
                    }
                ]
                self.assertEqual(self._project(rows)["log_sensitive_hits"], 1)
                self.assertTrue(
                    compare_step(self._project(rows), {"stdout_paths": self.log_paths})
                )

    def test_authorization_nonce_payload_is_rejected(self) -> None:
        """合成授权 nonce 出现在原始行时敏感命中非零并拒绝原判据。"""
        nonce = "00000000-0000-4000-8000-0000000000aa"
        rows = self._a01_fe_rows() + [
            {
                "schema_version": 1,
                "stage": "store",
                "operation": nonce,
                "result": "ok",
            }
        ]
        result = self._project(rows, forbidden=(nonce,))
        self.assertEqual(result["log_sensitive_hits"], 1)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_unpaired_finish_terminal_is_rejected(self) -> None:
        """只留 finish 终态而删除 started 时原闭合判据必须失败。"""
        rows = copy.deepcopy(self._a01_fe_rows())
        index = next(
            i
            for i, item in enumerate(rows)
            if item.get("operation") == "action_finish_command"
            and item.get("command") is not None
            and item.get("result") == "started"
        )
        rows.pop(index)
        result = self._project(rows)
        self.assertGreater(result["log_finish"]["unpaired"], 0)
        self.assertTrue(compare_step(result, {"stdout_paths": self.log_paths}))

    def test_lifecycle_failures_are_not_given_adb_command_attribution(self) -> None:
        """action_commit/complete_action/begin_action 失败保留 operation 责任，不伪造 ADB 归属。"""
        result = self._project(self._a01_fe_rows())
        self.assertEqual(result["log_reasons_by_command"], {})
        self.assertEqual(
            result["log_reasons"]["action_commit"], ["store_io_error:action"]
        )
        self.assertEqual(
            result["log_reasons"]["complete_action"], ["store_io_error:store"]
        )
        self.assertEqual(
            result["log_reasons"]["begin_action"], ["action_conflict:store"]
        )
        self.assertEqual(
            compare_step(result, {"stdout_paths": self.log_paths}), []
        )


class PopenCounterBoundaryTests(unittest.TestCase):
    """纯合成 FakePopen 验证边界窗口实测计数与恢复，不启动真实进程。"""

    class _FakeDevice:
        """只提供 _synthetic_device 需要的 popen 与计数器。"""

        def __init__(self) -> None:
            """初始化实测计数。"""
            self.real_popen_attempts = 0

        def popen(self, *_args, **_kwargs):
            """生产边界不应回落到合成 popen。"""
            raise AssertionError("synthetic popen must not be called")

    def _window(self, body, *, explode: bool = False):
        """在 FakePopen 类上进入 _synthetic_device，返回设备、调用计数与原 init。"""
        device = self._FakeDevice()
        calls: list[tuple] = []

        class FakePopen:
            """纯合成 Popen 类，只记录构造调用。"""

            def __init__(self, *args, **kwargs):
                """记录一次构造；explode 时模拟异常路径。"""
                calls.append(args)
                if explode:
                    raise RuntimeError("synthetic popen failure")

        original_init = FakePopen.__init__
        with mock.patch.object(subprocess, "Popen", FakePopen):
            try:
                with m5_probe._synthetic_device(device):
                    body(FakePopen)
            except RuntimeError:
                pass
            self.assertIs(subprocess.Popen, FakePopen)
            self.assertIs(FakePopen.__init__, original_init)
        return device, calls, original_init

    def test_one_real_popen_constructor_is_counted(self) -> None:
        """窗口内经保存的原始类构造一次真实 Popen 时实测计数为 1。"""
        device, calls, _original = self._window(
            lambda fake: fake(["synthetic"], shell=False)
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(device.real_popen_attempts, 1)

    def test_exception_path_is_counted_and_restored(self) -> None:
        """构造抛异常仍计数 1，退出后 Popen 与 __init__ 均恢复。"""
        device, calls, _original = self._window(
            lambda fake: fake(["synthetic"], shell=False), explode=True
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(device.real_popen_attempts, 1)

    def test_no_call_counts_zero(self) -> None:
        """窗口内未调用原始构造时实测计数为 0。"""
        device, calls, _original = self._window(lambda _fake: None)
        self.assertEqual(calls, [])
        self.assertEqual(device.real_popen_attempts, 0)


class GateObservationBoundaryTests(unittest.TestCase):
    """假 gate/假 sleep 验证有界观察轨迹与 D09 原强判据，不触真实门禁。"""

    class _Gate:
        """按序列返回行、None、固定 IO 错误或非预期错误。"""

        def __init__(self, sequence):
            """保存待消费序列。"""
            self.sequence = list(sequence)

        def inspect(self):
            """消费一个预设结果。"""
            value = self.sequence.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    def _observe(self, sequence, attempts=80):
        """用零耗时 sleep 调用真实有界观察函数。"""
        sleeps: list[float] = []
        result = m4c_probe.observe_gate_after_busy(
            self._Gate(sequence),
            attempts=attempts,
            interval=0.025,
            sleep=lambda value: sleeps.append(value),
        )
        return result, sleeps

    def test_sustained_none_is_exhausted_not_success(self) -> None:
        """持续 None 80 次仍无行，如实 exhausted，不产生 shared。"""
        result, sleeps = self._observe([None] * 80)
        self.assertEqual(result["trace"], ["none"] * 80)
        self.assertEqual(result["attempts"], 80)
        self.assertTrue(result["exhausted"])
        self.assertFalse(result["shared_found"])
        self.assertTrue(result["saw_none"])
        self.assertFalse(result["saw_io_error"])
        self.assertEqual(len(sleeps), 79)

    def test_sustained_io_error_is_exhausted_with_fixed_category(self) -> None:
        """持续 device_gate_io_error 只记录固定类别，不输出原异常。"""
        from app.ports.device_gate import DeviceGateError

        errors = [DeviceGateError("device_gate_io_error")] * 80
        result, _sleeps = self._observe(errors)
        self.assertEqual(result["trace"], ["io_error"] * 80)
        self.assertEqual(result["last_error"], "device_gate_io_error")
        self.assertTrue(result["exhausted"])
        self.assertTrue(result["saw_io_error"])

    def test_row_after_delay_is_returned(self) -> None:
        """正常延迟后在窗口内出现的行被返回。"""
        row = {"state": "held"}
        result, sleeps = self._observe([None, None, row])
        self.assertEqual(result["trace"], ["none", "none", "row"])
        self.assertEqual(result["attempts"], 3)
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["shared"], row)
        self.assertEqual(len(sleeps), 2)

    def test_row_at_81st_attempt_is_not_observed(self) -> None:
        """第 81 次才出现的行超出上限，必须保持 exhausted。"""
        result, _sleeps = self._observe([None] * 80 + [{"state": "held"}])
        self.assertEqual(result["attempts"], 80)
        self.assertTrue(result["exhausted"])
        self.assertFalse(result["shared_found"])

    def test_unexpected_gate_error_propagates(self) -> None:
        """非 device_gate_io_error 立即传播，不被等待吞掉。"""
        from app.ports.device_gate import DeviceGateError

        with self.assertRaises(DeviceGateError) as captured:
            self._observe([DeviceGateError("device_busy")])
        self.assertEqual(captured.exception.reason_code, "device_busy")

    def _relation_payloads(self):
        """构造符合 D09 原关系的 winner/loser 投影。"""
        nonce = "00000000-0000-4000-8000-000000000091"
        common = {
            "owner_id": "00000000-0000-4000-8000-000000000081",
            "owner_pid": 1,
            "owner_host": "a" * 64,
            "profile_path_token": "b" * 64,
            "profile_id": "synthetic-alpha",
            "session_nonce": nonce,
            "shared_owner_id": "00000000-0000-4000-8000-000000000081",
            "shared_owner_pid": 1,
            "shared_owner_host": "a" * 64,
            "shared_nonce": nonce,
            "shared_profile_id": "synthetic-alpha",
            "shared_profile_path_token": "b" * 64,
            "shared_state": "held",
        }
        winner = {
            **common,
            "outcome": "held",
            "shared_owner_matches_self": True,
            "shared_nonce_matches_session": True,
            "shared_profile_matches_self": True,
        }
        loser = {
            **common,
            "outcome": "busy",
            "shared_owner_matches_self": False,
            "shared_nonce_matches_session": False,
            "shared_profile_matches_self": False,
        }
        relation = self.assets.oracles["variants"]["SC-D09/m4c_single_primary"][
            "acceptance"
        ]["steps"][0]["children_relation"]
        return winner, loser, relation

    class _RaceStore:
        """提供 _gate_race 需要的 close，不创建真实 profile 库。"""

        def close(self) -> None:
            """无资源需要关闭。"""

    class _RaceGate:
        """只服务实际 _gate_race 包装：acquire 固定 busy，inspect 返回预设行。"""

        def __init__(self, row):
            """保存预设 inspect 行与 acquire 次数。"""
            self.row = row
            self.acquire_calls = 0

        def acquire(self, **_kwargs) -> None:
            """记录并固定抛出 device_busy，模拟另一进程先占用。"""
            from app.ports.device_gate import DeviceGateError

            self.acquire_calls += 1
            raise DeviceGateError("device_busy")

        def inspect(self):
            """返回预设共享行或 None。"""
            return self.row

    def _actual_gate_race_loser(self, shared):
        """调用真实 _gate_race 包装，只替换 profile/barrier/gate 边界。"""
        gate = self._RaceGate(shared)
        profile = Path("synthetic-profile.sqlite3")
        with (
            mock.patch.object(
                m4c_probe, "_profile", return_value=(self._RaceStore(), profile)
            ),
            mock.patch.object(m4c_probe, "_os_root", return_value=Path("synthetic-os")),
            mock.patch.object(m4c_probe, "_barrier"),
            mock.patch.object(
                m4c_probe, "SharedDeviceGate", return_value=gate
            ),
        ):
            payload = m4c_probe._gate_race({"profile_id": "synthetic-alpha"})
        self.assertEqual(gate.acquire_calls, 1)
        return payload

    def _winner_for(self, loser, nonce="00000000-0000-4000-8000-000000000093"):
        """按 loser 自身声明的真实身份构造唯一 winner，用于原关系判据。"""
        return {
            "outcome": "held",
            "owner_id": loser["owner_id"],
            "owner_pid": loser["owner_pid"],
            "owner_host": loser["owner_host"],
            "profile_id": loser["profile_id"],
            "profile_path_token": loser["profile_path_token"],
            "session_nonce": nonce,
            "shared_owner_id": loser["owner_id"],
            "shared_owner_pid": loser["owner_pid"],
            "shared_owner_host": loser["owner_host"],
            "shared_nonce": nonce,
            "shared_profile_id": loser["profile_id"],
            "shared_profile_path_token": loser["profile_path_token"],
            "shared_state": "held",
            "shared_owner_matches_self": True,
            "shared_nonce_matches_session": True,
            "shared_profile_matches_self": True,
        }

    def _d09_relation(self) -> dict:
        """返回原 D09 children_relation 判据。"""
        return self.assets.oracles["variants"]["SC-D09/m4c_single_primary"][
            "acceptance"
        ]["steps"][0]["children_relation"]

    def test_actual_gate_race_sustained_none_fails_d09_relation(self) -> None:
        """实际 _gate_race 持续 None 输出保留轨迹并产生 8 条原关系差异。"""
        loser = self._actual_gate_race_loser(None)
        self.assertEqual(loser["inspect_trace"], ["none"] * 80)
        self.assertEqual(loser["inspect_attempts"], 80)
        self.assertTrue(loser["inspect_exhausted"])
        self.assertIsNone(loser["inspect_last_error"])
        winner = self._winner_for(loser)
        errors = runner._compare_children_relation(
            [{"stdout_json": winner}, {"stdout_json": loser}], self._d09_relation()
        )
        self.assertEqual(len(errors), 8, errors)

    def test_actual_gate_race_wrong_identity_fails_d09_relation(self) -> None:
        """实际 _gate_race 返回错身份时保留行观察并产生 6 条原关系差异。"""
        shared = {
            "owner_id": "00000000-0000-4000-8000-000000000099",
            "owner_pid": 9,
            "owner_host": "c" * 64,
            "nonce": "00000000-0000-4000-8000-000000000092",
            "profile_id": "synthetic-beta",
            "profile_db_path": "other-profile.sqlite3",
            "state": "held",
        }
        loser = self._actual_gate_race_loser(shared)
        self.assertEqual(loser["inspect_trace"], ["row"])
        self.assertEqual(loser["inspect_attempts"], 1)
        self.assertFalse(loser["inspect_exhausted"])
        self.assertIsNone(loser["inspect_last_error"])
        winner = self._winner_for(loser)
        errors = runner._compare_children_relation(
            [{"stdout_json": winner}, {"stdout_json": loser}], self._d09_relation()
        )
        self.assertEqual(len(errors), 6, errors)

    def test_sustained_none_output_fails_original_d09_relation(self) -> None:
        """持续 None 的实际投影送入原 D09 children_relation 会失败。"""
        winner, loser, relation = self._relation_payloads()
        loser.update(
            {
                "shared_owner_id": None,
                "shared_owner_pid": None,
                "shared_owner_host": None,
                "shared_nonce": None,
                "shared_profile_id": None,
                "shared_profile_path_token": None,
                "shared_state": None,
            }
        )
        self.assertTrue(runner._compare_children_relation([{'stdout_json': winner}, {'stdout_json': loser}], relation))

    def test_seven_identity_tamper_fields_are_rejected(self) -> None:
        """7 个身份/状态字段逐一篡改都被原 D09 关系拒绝。"""
        winner, loser, relation = self._relation_payloads()
        self.assertEqual(runner._compare_children_relation([{'stdout_json': winner}, {'stdout_json': loser}], relation), [])
        tamper = {
            "shared_owner_id": "00000000-0000-4000-8000-000000000099",
            "shared_owner_pid": 9,
            "shared_owner_host": "c" * 64,
            "shared_nonce": "00000000-0000-4000-8000-000000000092",
            "shared_profile_id": "synthetic-beta",
            "shared_profile_path_token": "d" * 64,
            "shared_state": "quarantined",
        }
        for field, value in tamper.items():
            mutated = copy.deepcopy(loser)
            mutated[field] = value
            self.assertTrue(
                runner._compare_children_relation([{'stdout_json': winner}, {'stdout_json': mutated}], relation), field
            )

    @classmethod
    def setUpClass(cls) -> None:
        """加载 D09 原预期供关系负控使用。"""
        cls.assets = load_assets()


if __name__ == "__main__":
    unittest.main()
