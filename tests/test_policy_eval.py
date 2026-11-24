"""验证 M4b policy eval 实包加载、报告、分桶、映射与留出生命周期。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.adapters.files.evals import FileEvalStore
from app.adapters.llm.bridge_a import ExternalAgentBridge
from app.domain.eval import begin_holdout_access, evaluate_assertions
from app.domain.models import canonical_hash
from app.ports.decision import BridgeDescriptor
from app.ports.policy import PolicyError
from app.process_log import ProcessLog
from app.usecases import policy as policy_usecases
from app.usecases import policy_eval as eval_usecases
from tests import policy_helpers

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "fixtures" / "bridge" / "bridge_stub.py"


def public_decisions() -> dict[str, dict[str, object]]:
    """构造与公开套件属性一致、但不读取断言侧的合成决策表。"""
    return {
        "eval-task-greeting-location": {
            "intent": "interested",
            "sub_intents": ["greeting"],
            "reply": "您好，我目前接受苏州的工作地点。",
            "escalate": None,
        },
        "eval-task-greeting-salary": {
            "intent": "interested",
            "sub_intents": ["salary"],
            "reply": "您好，我的期望薪资是15-20k。",
            "escalate": None,
        },
        "eval-task-missing-salary": {
            "intent": "needs_human",
            "sub_intents": ["salary"],
            "reply": "薪资信息需要本人确认，我先转人工核实。",
            "escalate": "human_review",
        },
        "eval-task-injection-role": {
            "intent": "needs_human",
            "sub_intents": ["sensitive"],
            "reply": "这条消息包含异常指令，已转人工处理。",
            "escalate": "human_review",
        },
        "eval-task-job-question": {
            "intent": "interested",
            "sub_intents": ["job_question"],
            "reply": "这个岗位主要负责后端服务与接口开发，想先了解团队情况。",
            "escalate": None,
        },
        "eval-task-job-card": {
            "intent": "interested",
            "sub_intents": [],
            "reply": "这个岗位可以考虑，想先了解团队情况。",
            "escalate": None,
        },
    }


class SimulatedBridge:
    """测试用模拟 transport；descriptor 标记 simulated，永不产生验收凭证。"""

    def __init__(
        self,
        decisions: dict[str, dict[str, object]],
        *,
        provider_id: str = "local-process",
    ) -> None:
        """保存合成决策与 provider 标签，并初始化请求捕获。"""
        self.decisions = decisions
        self.provider_id = provider_id
        self.requests: list[dict[str, object]] = []

    def descriptor(self) -> BridgeDescriptor:
        """返回 simulated=True 的固定桥摘要。"""
        return BridgeDescriptor(
            provider_id=self.provider_id,
            model_id="synthetic-model",
            model_config_hash=canonical_hash({"simulated": self.provider_id}),
            bridge="bridge_b" if self.provider_id == "local-process" else "bridge_a",
            simulated=True,
        )

    def complete(
        self,
        *,
        system_message: str,
        developer_message: str,
        user_message: str,
        echo: dict[str, str],
    ) -> str:
        """只根据 echo 与 user JSON 构造 Decision，不读取任何断言/答案。"""
        self.requests.append(
            {
                "system": system_message,
                "developer": developer_message,
                "user": json.loads(user_message),
                "echo": dict(echo),
            }
        )
        spec = self.decisions[echo["task_id"]]
        data = json.loads(user_message)
        decision = {
            "schema_version": 1,
            "task_id": echo["task_id"],
            "nonce": echo["nonce"],
            "input_hash": echo["input_hash"],
            "intent": spec["intent"],
            "sub_intents": spec["sub_intents"],
            "reply": spec["reply"],
            "evidence": str(data["inbound"])[:2],
            "escalate": spec["escalate"],
        }
        return json.dumps(decision, ensure_ascii=False)


class SimulatedRealAdapter:
    """委托真实 A/B 适配器但把报告标记为 simulated，避免误发验收凭证。"""

    def __init__(self, inner: object) -> None:
        """保存真实适配器。"""
        self.inner = inner

    def descriptor(self) -> BridgeDescriptor:
        """返回真实摘要，但强制 simulated=True。"""
        return replace(self.inner.descriptor(), simulated=True)

    def complete(self, **kwargs: object) -> str:
        """委托真实子进程桥。"""
        return self.inner.complete(**kwargs)


class PolicyEvalTests(unittest.TestCase):
    """覆盖报告绑定、模拟标记、分桶、编辑重算与留出整批退役。"""

    def setUp(self) -> None:
        """创建临时项目根、活动包与公开套件副本。"""
        self.tmp, self.root = policy_helpers.temp_project()
        self.repo = policy_helpers.repository(self.root)
        policy_usecases.init_pack(self.repo, "prompts/local/demo")
        pack_path = self.root / "prompts/local/demo/pack.toml"
        text = pack_path.read_bytes().decode("utf-8")
        pack_path.write_bytes(
            text.replace(
                "allow_keys = []",
                'allow_keys = ["work_location", "salary_expectation"]',
            ).encode("utf-8")
        )
        self.store = FileEvalStore(self.root)
        self.now = policy_helpers.NOW

    def tearDown(self) -> None:
        """只清理本用例临时目录。"""
        self.tmp.cleanup()

    def _eval(self, bridge, suite: str = "public", *, attest: bool = True):
        """以显式授权回调运行一次评测；留出默认写合成无上传合同。"""
        if suite == "holdout" and attest:
            self._write_attestation(bridge)
        return eval_usecases.eval_pack(
            self.repo,
            self.store,
            "prompts/local/demo",
            suite,
            bridge,
            lambda plan: True,
            now=self.now,
        )

    def _write_attestation(self, bridge) -> None:
        """写入与当前桥摘要绑定的合成 operator/host 无上传合同。"""
        descriptor = bridge.descriptor()
        target = self.root / "data/evals/holdout-attestation.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": "holdout_local_no_upload",
                    "provider_id": descriptor.provider_id,
                    "model_id": descriptor.model_id,
                    "model_config_hash": descriptor.model_config_hash,
                    "no_upload": True,
                    "no_cache": True,
                    "no_share": True,
                    "no_telemetry": True,
                    "cloud_fallback_disabled": True,
                    "host_scope": "local",
                    "operator": "synthetic-operator",
                    "attested_at": self.now,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _report(self, result: dict[str, object]) -> dict[str, object]:
        """读取本机报告 JSON。"""
        return json.loads((self.root / str(result["report_path"])).read_text(encoding="utf-8"))

    def test_public_simulated_run_binds_digests_without_acceptance(self) -> None:
        """模拟公共集可跑通并绑定摘要，但 simulated 不产生验收凭证。"""
        bridge = SimulatedBridge(public_decisions())
        result = self._eval(bridge)
        self.assertEqual(result["status"], "measured")
        self.assertIsNone(result["acceptance"])
        self.assertIsNone(result["acceptance_path"])
        report = self._report(result)
        self.assertEqual(report["buckets"]["public"]["passed"], 6)
        self.assertEqual(report["buckets"]["public"]["failed"], 0)
        self.assertEqual(report["buckets"]["regression"]["status"], "not_measured")
        self.assertEqual(report["buckets"]["holdout"]["status"], "not_measured")
        self.assertEqual(report["provider"]["simulated"], True)
        self.assertIsNone(report["acceptance"])
        self.assertIn("simulated_transport", report["limitations"])
        self.assertEqual(len(report["pack"]["pack_hash"]), 64)
        for case in report["cases"]:
            self.assertNotIn("reply", case)
            self.assertEqual(len(case["prompt_hash"]), 64)
        self.assertFalse((self.root / "data/evals" / f"eval-{result['run_id']}" / "acceptance.json").exists())

    def test_real_adapter_process_path_is_marked_simulated(self) -> None:
        """真实 A/B 子进程路径跑公开集，报告仍标记 simulated 且无凭证。"""
        decisions = self.root / "decisions.json"
        capture = self.root / "requests.jsonl"
        decisions.write_text(json.dumps(public_decisions(), ensure_ascii=False), encoding="utf-8")
        argv = [sys.executable, str(STUB), "ok", str(decisions), str(capture)]
        inner = ExternalAgentBridge(argv, model_id="synthetic-model")
        result = self._eval(SimulatedRealAdapter(inner))
        report = self._report(result)
        self.assertEqual(report["buckets"]["public"]["passed"], 6)
        self.assertIsNone(report["acceptance"])
        self.assertTrue(report["provider"]["simulated"])
        lines = capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 6)
        requests = [json.loads(line) for line in lines]
        suite = json.loads(
            (ROOT / "evals/policy-suite/public.json").read_text(encoding="utf-8")
        )
        inbound_to_case = {
            case["input"]["inbound"]: case["input"]["case_id"]
            for case in suite["cases"]
        }
        for request in requests:
            self.assertEqual(len(request["messages"]), 3)
            self.assertNotIn("assertions", request)
            self.assertNotIn("expected", request)
            user_data = json.loads(request["messages"][2]["content"])
            self.assertIn(user_data["inbound"], inbound_to_case)
            current = user_data["inbound"]
            raw = request["messages"][2]["content"]
            for other in inbound_to_case:
                if other != current:
                    self.assertNotIn(other, raw)
        self.assertEqual(requests[0]["messages"][0]["content"], self.repo.read_base()["intent-and-reply.md"])
        self.assertIn("profile_id", requests[0]["messages"][1]["content"])

    def test_success_with_real_provider_generates_bound_acceptance(self) -> None:
        """非 simulated 的全通过公开集写出绑定 base/pack/provider/model/config 的凭证。"""
        class ProductionLikeBridge(SimulatedBridge):
            """报告为真实 transport，以便验证成功凭证路径。"""

            def descriptor(self) -> BridgeDescriptor:
                """返回 simulated=False 的固定摘要。"""
                return replace(super().descriptor(), simulated=False)

        bridge = ProductionLikeBridge(public_decisions())
        result = self._eval(bridge)
        self.assertIsNotNone(result["acceptance"])
        self.assertIsNotNone(result["acceptance_path"])
        report = self._report(result)
        acceptance = json.loads(
            (self.root / str(result["acceptance_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(acceptance["base_hash"], report["pack"]["base_hash"])
        self.assertEqual(acceptance["pack_hash"], report["pack"]["pack_hash"])
        self.assertEqual(acceptance["provider_id"], report["provider"]["provider_id"])
        self.assertEqual(acceptance["model_id"], report["provider"]["model_id"])
        self.assertEqual(
            acceptance["model_config_hash"], report["provider"]["model_config_hash"]
        )
        self.assertEqual(acceptance["suite_hash"], report["suite"]["hash"])

    def test_public_suite_missing_is_configuration_failure(self) -> None:
        """公开套件是部署资源；缺失时固定 eval_failed，不冒充未测通过。"""
        (self.root / "evals/policy-suite/public.json").unlink()
        bridge = SimulatedBridge(public_decisions())
        with self.assertRaises(PolicyError) as caught:
            self._eval(bridge)
        self.assertEqual(caught.exception.reason_code, "eval_failed")
        self.assertEqual(bridge.requests, [])

    def test_unsupported_provider_is_rejected_before_authorization(self) -> None:
        """未知/echo provider 在授权回调前固定 invalid_provider。"""
        class EchoBridge(SimulatedBridge):
            """把 provider 标记为 echo 的非法桥。"""

            def descriptor(self) -> BridgeDescriptor:
                """返回 echo provider。"""
                return replace(super().descriptor(), provider_id="echo")

        bridge = EchoBridge(public_decisions())
        called: list[object] = []
        with self.assertRaises(PolicyError) as caught:
            eval_usecases.eval_pack(
                self.repo,
                self.store,
                "prompts/local/demo",
                "public",
                bridge,
                lambda plan: called.append(plan) or True,
                now=self.now,
            )
        self.assertEqual(caught.exception.reason_code, "invalid_provider")
        self.assertEqual(called, [])
        self.assertEqual(bridge.requests, [])

    def test_authorization_is_checked_before_any_bridge_call(self) -> None:
        """授权回调拒绝时不启动桥、不写报告。"""
        bridge = SimulatedBridge(public_decisions())
        with self.assertRaises(PolicyError) as caught:
            eval_usecases.eval_pack(
                self.repo,
                self.store,
                "prompts/local/demo",
                "public",
                bridge,
                lambda plan: False,
                now=self.now,
            )
        self.assertEqual(caught.exception.reason_code, "authorization_required")
        self.assertEqual(bridge.requests, [])
        directories = list((self.root / "data/evals").glob("eval-*"))
        self.assertEqual(directories, [])

    def test_case_failure_writes_report_without_acceptance(self) -> None:
        """断言失败写逐例报告但无验收凭证，并返回固定 eval_failed。"""
        decisions = public_decisions()
        decisions["eval-task-greeting-location"]["reply"] = "您好。"
        bridge = SimulatedBridge(decisions)
        with self.assertRaises(PolicyError) as caught:
            self._eval(bridge)
        self.assertEqual(caught.exception.reason_code, "eval_failed")
        self.assertEqual(len(caught.exception.issues), 1)
        report_path = caught.exception.issues[0].file
        self.assertIsNotNone(report_path)
        report = json.loads((self.root / str(report_path)).read_text(encoding="utf-8"))
        self.assertEqual(report["buckets"]["public"]["failed"], 1)
        self.assertIsNone(report["acceptance"])
        self.assertIn("case_failures", report["limitations"])
        self.assertFalse((self.root / "data/evals" / f"eval-{report['run_id']}" / "acceptance.json").exists())

    def test_pack_edit_changes_actual_request_and_report_digest(self) -> None:
        """编辑当前包同时改变实际 developer 内容、prompt_hash 与报告 pack_hash。"""
        first_bridge = SimulatedBridge(public_decisions())
        first = self._eval(first_bridge)
        first_report = self._report(first)
        tone = self.root / "prompts/local/demo/tone.md"
        tone.write_bytes(tone.read_bytes() + "\n更简洁。".encode("utf-8"))
        second_bridge = SimulatedBridge(public_decisions())
        second = self._eval(second_bridge)
        second_report = self._report(second)
        self.assertNotEqual(
            first_report["pack"]["pack_hash"], second_report["pack"]["pack_hash"]
        )
        self.assertNotEqual(
            first_report["cases"][0]["prompt_hash"],
            second_report["cases"][0]["prompt_hash"],
        )
        self.assertNotEqual(
            first_bridge.requests[0]["developer"],
            second_bridge.requests[0]["developer"],
        )
        self.assertEqual(
            first_bridge.requests[0]["system"], second_bridge.requests[0]["system"]
        )

    def test_mapping_covers_all_original_cases_without_claiming_pass(self) -> None:
        """24 行映射完整、派生 ID 属于公开套件、旧 24 案例文件未被改写。"""
        mapping = json.loads(
            (ROOT / "evals/policy-suite/mapping.json").read_text(encoding="utf-8")
        )
        rows = mapping["rows"]
        self.assertEqual([row["original_id"] for row in rows], [f"M{index:02d}" for index in range(1, 25)])
        suite = json.loads(
            (ROOT / "evals/policy-suite/public.json").read_text(encoding="utf-8")
        )
        case_ids = {case["input"]["case_id"] for case in suite["cases"]}
        for row in rows:
            self.assertTrue(row["owner"])
            self.assertTrue(row["not_integrated_reason"])
            self.assertTrue(row["later_verification"])
            for derived in row["derived_ids"]:
                self.assertIn(derived, case_ids)
        for source_id in ("M01", "M04", "M14", "M24"):
            row = next(item for item in rows if item["original_id"] == source_id)
            self.assertTrue(row["derived_ids"])
        legacy = json.loads((ROOT / "evals/cases.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["vars"]["input"]["case_id"] for item in legacy],
            [f"M{index:02d}" for index in range(1, 25)],
        )

    def _install_holdout(self, *, empty: bool = False, register: bool = True) -> dict[str, object] | None:
        """在临时项目复制公开套件为私有留出，并按需写冻结登记。"""
        suite = json.loads(
            (ROOT / "evals/policy-suite/public.json").read_text(encoding="utf-8")
        )
        suite["suite"] = "holdout"
        if empty:
            suite["cases"] = []
        target = self.root / "evals/private/holdout/suite.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
        if not register:
            return None
        count = 0 if empty else len(suite["cases"])
        return eval_usecases.register_holdout(
            self.store,
            batch_id="batch-1",
            version="2026-11-24",
            sample_count=count,
            annotator="synthetic-annotator",
            maintainer="synthetic-maintainer",
            frozen_at=self.now,
            candidate_release="candidate-1",
        )

    def test_holdout_freeze_access_repeat_retire_and_new_batch(self) -> None:
        """冻结批次记录访问；重跑非新证据，退役后旧样本不可换名重冻结。"""
        self._install_holdout()
        bridge = SimulatedBridge(public_decisions())
        first = self._eval(bridge, "holdout")
        report = self._report(first)
        self.assertEqual(report["buckets"]["holdout"]["passed"], 6)
        self.assertIsNone(report["acceptance"])
        self.assertFalse(report["holdout"]["repeat_access"])
        self.assertTrue(report["holdout"]["new_evidence"])
        self.assertEqual(report["authorization"]["scope"], "holdout_no_upload_attested")
        second = self._eval(SimulatedBridge(public_decisions()), "holdout")
        second_report = self._report(second)
        self.assertTrue(second_report["holdout"]["repeat_access"])
        self.assertFalse(second_report["holdout"]["new_evidence"])
        tone = self.root / "prompts/local/demo/tone.md"
        tone.write_bytes(tone.read_bytes() + "\n更稳健。".encode("utf-8"))
        changed_bridge = SimulatedBridge(public_decisions())
        with self.assertRaises(PolicyError) as caught:
            self._eval(changed_bridge, "holdout")
        self.assertEqual(caught.exception.reason_code, "eval_failed")
        self.assertEqual(changed_bridge.requests, [])
        retired = eval_usecases.retire_holdout(
            self.store, batch_id="batch-1", retired_at=self.now
        )
        self.assertTrue(retired["regression_ref"])
        registry = self.store.read_holdout_registry()
        self.assertEqual(registry["batches"][0]["status"], "retired")
        self.assertTrue(registry["batches"][0]["regression_eligible"])
        with self.assertRaises(PolicyError):
            eval_usecases.register_holdout(
                self.store,
                batch_id="batch-same-samples",
                version="2026-11-24",
                sample_count=6,
                annotator="synthetic-annotator-2",
                maintainer="synthetic-maintainer",
                frozen_at=self.now,
                candidate_release="candidate-2",
            )
        # 新候选必须使用全新样本；旧样本换 batch/候选/套件均被占用指纹拒绝。
        suite = json.loads(
            (self.root / "evals/private/holdout/suite.json").read_text(encoding="utf-8")
        )
        for case in suite["cases"]:
            case["input"]["inbound"] = case["input"]["inbound"] + "（新样本）"
        (self.root / "evals/private/holdout/suite.json").write_text(
            json.dumps(suite, ensure_ascii=False), encoding="utf-8"
        )
        eval_usecases.register_holdout(
            self.store,
            batch_id="batch-2",
            version="2026-11-25",
            sample_count=6,
            annotator="synthetic-annotator-2",
            maintainer="synthetic-maintainer",
            frozen_at=self.now,
            candidate_release="candidate-2",
        )
        third = self._eval(SimulatedBridge(public_decisions()), "holdout")
        self.assertEqual(self._report(third)["holdout"]["batch_id"], "batch-2")

    def test_holdout_subset_split_is_rejected(self) -> None:
        """已看整批退役后，取子集作为新留出仍被样本身份占用拒绝。"""
        self._install_holdout()
        self._eval(SimulatedBridge(public_decisions()), "holdout")
        eval_usecases.retire_holdout(self.store, batch_id="batch-1", retired_at=self.now)
        suite = json.loads(
            (self.root / "evals/private/holdout/suite.json").read_text(encoding="utf-8")
        )
        suite["cases"] = suite["cases"][:2]
        for case in suite["cases"]:
            case["input"]["case_id"] = case["input"]["case_id"] + "-subset"
            case["input"]["task_id"] = case["input"]["task_id"] + "-subset"
            case["input"]["nonce"] = "00000000-0000-4000-8000-000000000999"
            case["input"]["now"] = "2026-12-01T00:00:00.000Z"
        (self.root / "evals/private/holdout/suite.json").write_text(
            json.dumps(suite, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(PolicyError):
            eval_usecases.register_holdout(
                self.store,
                batch_id="batch-subset",
                version="2026-12-01",
                sample_count=2,
                annotator="synthetic-annotator-3",
                maintainer="synthetic-maintainer",
                frozen_at=self.now,
                candidate_release="candidate-3",
            )

    def test_register_requires_actual_sample_count(self) -> None:
        """登记声明 7 但实际 1 时必须即时拒绝，不等 eval 才发现。"""
        self._install_holdout(register=False)
        suite = json.loads(
            (self.root / "evals/private/holdout/suite.json").read_text(encoding="utf-8")
        )
        suite["cases"] = suite["cases"][:1]
        (self.root / "evals/private/holdout/suite.json").write_text(
            json.dumps(suite, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(PolicyError):
            eval_usecases.register_holdout(
                self.store,
                batch_id="batch-count",
                version="2026-11-24",
                sample_count=7,
                annotator="synthetic-annotator",
                maintainer="synthetic-maintainer",
                frozen_at=self.now,
                candidate_release="candidate-count",
            )

    def test_missing_holdout_suite_is_not_measured_without_authorization(self) -> None:
        """缺留出套件返回未测、authorization 未确认且零桥调用。"""
        self._install_holdout(register=False)
        (self.root / "evals/private/holdout/suite.json").unlink()
        bridge = SimulatedBridge(public_decisions())
        result = self._eval(bridge, "holdout")
        self.assertEqual(result["status"], "not_measured")
        report = self._report(result)
        self.assertFalse(report["authorization"]["confirmed"])
        self.assertEqual(report["authorization"]["scope"], "not_applicable")
        self.assertIn("suite_not_present", report["limitations"])
        self.assertEqual(bridge.requests, [])
        self.assertFalse((self.root / "data/evals/holdout-registry.local.json").exists())

    def test_legacy_registry_without_fingerprints_fails_closed(self) -> None:
        """旧登记无样本身份时不得补空继续当新证据，零调用拒绝。"""
        self._install_holdout(register=False)
        registry = self.root / "data/evals/holdout-registry.local.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "batches": [
                        {
                            "batch_id": "legacy",
                            "suite_hash": "a" * 64,
                            "version": "old",
                            "sample_count": 1,
                            "annotator": "old",
                            "maintainer": "old",
                            "frozen_at": self.now,
                            "candidate_release": "old",
                            "status": "frozen",
                            "access_rounds": [],
                            "retired_at": None,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bridge = SimulatedBridge(public_decisions())
        with self.assertRaises(PolicyError) as caught:
            self._eval(bridge, "holdout")
        self.assertEqual(caught.exception.reason_code, "eval_failed")
        self.assertEqual(bridge.requests, [])

    def test_unrelated_bad_regression_admission_does_not_break_public_or_holdout(self) -> None:
        """public/holdout 不读取 regression 准入；只有 regression 自身坏输入失败。"""
        self._install_holdout(register=False)
        bad = self.root / "data/evals/regression-admissions/bad/suite.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{bad", encoding="utf-8")
        self.assertIsNotNone(self.store.read_suite("public"))
        self.assertIsNotNone(self.store.read_suite("holdout"))
        with self.assertRaises(PolicyError) as caught:
            self.store.read_suite("regression")
        self.assertEqual(caught.exception.reason_code, "policy_io_error")

    def test_holdout_concurrent_first_access_is_not_double_counted(self) -> None:
        """并发登记事务串行：只有一次首次访问，另一条标记重复，记录不丢失。"""
        self._install_holdout()
        results: list[bool] = []

        def begin(index: int) -> None:
            """在线程内提交一次访问意向并记录 repeat 标记。"""
            def mutate(registry):
                """在锁内提交一次访问意向并返回 repeat 标记。"""
                batch = registry["batches"][0]
                entry = begin_holdout_access(
                    registry,
                    batch,
                    attempt_id=f"attempt-{index}",
                    at=self.now,
                    base_hash="a" * 64,
                    pack_hash="b" * 64,
                    provider_id="local-process",
                    model_id="synthetic-model",
                    model_config_hash="c" * 64,
                )
                return bool(entry["repeat_access"])

            results.append(self.store.update_holdout_registry(mutate))  # type: ignore[arg-type]

        threads = [
            threading.Thread(target=begin, args=(index,), name=f"holdout-{index}")
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(results), [False, True])
        rounds = self.store.read_holdout_registry()["batches"][0]["access_rounds"]
        self.assertEqual(len(rounds), 2)

    def test_holdout_interruption_consumes_access_and_repeat_is_not_new_evidence(self) -> None:
        """已提交后中断不恢复未看：访问意向/未知结果持久化，重跑只算重复访问。"""
        self._install_holdout()
        bridge = SimulatedBridge(public_decisions())

        class ExitBridge(SimulatedBridge):
            """模拟调用后进程被 SystemExit 中断。"""

            def complete(self, **kwargs: object) -> str:
                """记录一次提交式调用后抛出进程中断。"""
                self.requests.append({"interrupted": True, **kwargs})
                raise SystemExit(7)

        interrupted = ExitBridge(public_decisions())
        with self.assertRaises(SystemExit):
            self._eval(interrupted, "holdout")
        registry = self.store.read_holdout_registry()
        rounds = registry["batches"][0]["access_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["phase"], "completed")
        self.assertEqual(rounds[0]["outcome"], "unknown")
        rerun = self._eval(bridge, "holdout")
        report = self._report(rerun)
        self.assertTrue(report["holdout"]["repeat_access"])
        self.assertFalse(report["holdout"]["new_evidence"])
        self.assertEqual(report["holdout"]["outcome"], "completed")

    def test_holdout_finish_write_failure_keeps_intent_and_repeat_is_not_new_evidence(self) -> None:
        """调用后收尾写失败保留 intent；重跑只能算重复访问，不恢复未看证据。"""
        self._install_holdout()

        class FailingFinishStore(FileEvalStore):
            """第一次登记成功，第二次收尾模拟写失败。"""

            def __init__(self, root: Path) -> None:
                """保存根并初始化调用计数。"""
                super().__init__(root)
                self.calls = 0

            def update_holdout_registry(self, mutator):
                """第二次登记事务固定失败。"""
                self.calls += 1
                if self.calls == 2:
                    raise PolicyError("policy_io_error", [])
                return super().update_holdout_registry(mutator)

        bridge = SimulatedBridge(public_decisions())
        self._write_attestation(bridge)
        with self.assertRaises(PolicyError) as caught:
            eval_usecases.eval_pack(
                self.repo,
                FailingFinishStore(self.root),
                "prompts/local/demo",
                "holdout",
                bridge,
                lambda plan: True,
                now=self.now,
            )
        self.assertEqual(caught.exception.reason_code, "policy_io_error")
        rounds = self.store.read_holdout_registry()["batches"][0]["access_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["phase"], "intent")
        self.assertEqual(rounds[0]["outcome"], "started")
        rerun = self._eval(SimulatedBridge(public_decisions()), "holdout")
        report = self._report(rerun)
        self.assertTrue(report["holdout"]["repeat_access"])
        self.assertFalse(report["holdout"]["new_evidence"])

    def test_holdout_intent_write_failure_makes_no_bridge_call(self) -> None:
        """调用前访问意向持久化失败时零桥调用，原登记保持不变。"""
        self._install_holdout()

        class FailingBeginStore(FileEvalStore):
            """第一次登记事务模拟本机写失败。"""

            def __init__(self, root: Path) -> None:
                """保存根并初始化失败计数。"""
                super().__init__(root)
                self.calls = 0

            def update_holdout_registry(self, mutator):
                """第一次直接固定失败，之后委托真实实现。"""
                self.calls += 1
                if self.calls == 1:
                    raise PolicyError("policy_io_error", [])
                return super().update_holdout_registry(mutator)

        bridge = SimulatedBridge(public_decisions())
        self._write_attestation(bridge)
        store = FailingBeginStore(self.root)
        before = self.store.read_holdout_registry()
        with self.assertRaises(PolicyError) as caught:
            eval_usecases.eval_pack(
                self.repo,
                store,
                "prompts/local/demo",
                "holdout",
                bridge,
                lambda plan: True,
                now=self.now,
            )
        self.assertEqual(caught.exception.reason_code, "policy_io_error")
        self.assertEqual(bridge.requests, [])
        self.assertEqual(self.store.read_holdout_registry(), before)

    def test_holdout_missing_or_mismatched_attestation_makes_no_call(self) -> None:
        """缺无上传合同或合同与当前 model/config 不符时零调用。"""
        self._install_holdout()
        missing = SimulatedBridge(public_decisions())
        with self.assertRaises(PolicyError) as caught:
            self._eval(missing, "holdout", attest=False)
        self.assertEqual(caught.exception.reason_code, "authorization_required")
        self.assertEqual(missing.requests, [])
        mismatched = SimulatedBridge(public_decisions())
        self._write_attestation(mismatched)
        target = self.root / "data/evals/holdout-attestation.local.json"
        data = json.loads(target.read_text(encoding="utf-8"))
        data["model_config_hash"] = "0" * 64
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(PolicyError) as caught:
            self._eval(mismatched, "holdout", attest=False)
        self.assertEqual(caught.exception.reason_code, "authorization_required")
        self.assertEqual(mismatched.requests, [])

    def test_retire_registry_failure_removes_admission_and_keeps_registry(self) -> None:
        """退役登记写失败时清理本次准入副本，regression 不可运行且原登记不变。"""
        self._install_holdout()
        before = self.store.read_holdout_registry()

        class FailingRetireStore(FileEvalStore):
            """退役登记写入固定失败。"""

            def update_holdout_registry(self, mutator):
                """不执行 mutator，模拟锁内写失败。"""
                raise PolicyError("policy_io_error", [])

        with self.assertRaises(PolicyError) as caught:
            eval_usecases.retire_holdout(
                FailingRetireStore(self.root),
                batch_id="batch-1",
                retired_at=self.now,
            )
        self.assertEqual(caught.exception.reason_code, "eval_failed")
        self.assertEqual(self.store.read_holdout_registry(), before)
        self.assertIsNone(self.store.read_suite("regression"))
        self.assertFalse(
            (self.root / "data/evals/regression-admissions/batch-1/suite.json").exists()
        )

    def test_holdout_rejects_external_provider(self) -> None:
        """留出只允许本机 provider，外部 agent 在调用前拒绝。"""
        self._install_holdout()
        bridge = SimulatedBridge(public_decisions(), provider_id="external-agent")
        with self.assertRaises(PolicyError) as caught:
            self._eval(bridge, "holdout")
        self.assertEqual(caught.exception.reason_code, "invalid_provider")
        self.assertEqual(bridge.requests, [])

    def test_zero_sample_holdout_is_not_measured(self) -> None:
        """零样本批次写未测报告、零授权零调用且无验收凭证。"""
        self._install_holdout(empty=True)
        bridge = SimulatedBridge(public_decisions())
        result = self._eval(bridge, "holdout")
        self.assertEqual(result["status"], "not_measured")
        report = self._report(result)
        self.assertEqual(report["buckets"]["holdout"]["status"], "not_measured")
        self.assertEqual(report["buckets"]["holdout"]["total"], 0)
        self.assertFalse(report["authorization"]["confirmed"])
        self.assertEqual(report["authorization"]["scope"], "not_applicable")
        self.assertIsNone(report["acceptance"])
        self.assertIn("zero_samples", report["limitations"])
        self.assertEqual(bridge.requests, [])

    def test_cli_eval_rejects_echo_reference_and_missing_confirmation(self) -> None:
        """CLI 拒绝 echo/参考回放，且未确认时在桥启动前 authorization_required。"""
        code, payload, _ = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "eval",
                "--pack",
                "prompts/local/demo",
                "--suite",
                "public",
                "--provider",
                "echo",
                "--bridge-command",
                "echo",
                "--model-id",
                "m",
                "--confirm-external-send",
            ],
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "invalid_provider")
        code, payload, _ = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "eval",
                "--pack",
                "prompts/local/demo",
                "--suite",
                "public",
                "--provider",
                "external-agent",
                "--bridge-command",
                sys.executable,
                "--arg",
                str(STUB),
                "--model-id",
                "m",
            ],
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "authorization_required")
        self.assertEqual(list((self.root / "data/evals").glob("eval-*")), [])

    def test_cli_holdout_register_and_retire(self) -> None:
        """CLI 最小登记/退役入口维护维护者、标注者、数量与候选关联。"""
        self._install_holdout(register=False)
        code, payload, _ = policy_helpers.run_cli(
            self.root,
            [
                "policy",
                "holdout",
                "register",
                "--batch-id",
                "batch-cli",
                "--version",
                "2026-11-24",
                "--sample-count",
                "6",
                "--annotator",
                "synthetic-annotator-cli",
                "--maintainer",
                "synthetic-maintainer",
                "--candidate-release",
                "candidate-cli",
            ],
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["result"]["batch"]["batch_id"], "batch-cli")
        code, payload, _ = policy_helpers.run_cli(
            self.root,
            ["policy", "holdout", "retire", "--batch-id", "batch-cli"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["status"], "retired")
        self.assertTrue(payload["result"]["regression_ref"])
        regression = self.store.read_suite("regression")
        self.assertIsNotNone(regression)
        self.assertEqual(len(regression["cases"]), 6)
        regression_result = self._eval(SimulatedBridge(public_decisions()), "regression")
        self.assertEqual(regression_result["status"], "measured")
        regression_report = self._report(regression_result)
        self.assertEqual(regression_report["buckets"]["regression"]["passed"], 6)
        self.assertIsNone(regression_report["acceptance"])

    def test_bounded_suite_read_rejects_oversize_before_full_decode(self) -> None:
        """2MiB 套件在 max+1 读取边界拒绝，不完整读入内存。"""
        target = self.root / "evals/private/holdout/suite.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"{" + b"x" * (2 * 1024 * 1024))
        with self.assertRaises(PolicyError) as caught:
            self.store.read_suite("holdout")
        self.assertEqual(caught.exception.reason_code, "policy_io_error")
        self.assertEqual(caught.exception.issues[0].code, "size")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_data_evals_junction_is_rejected_without_touching_target(self) -> None:
        """data/evals 为 junction 时写报告拒绝，目标目录内容不变。"""
        target = self.root / "data" / "evals-target"
        target.mkdir(parents=True)
        sentinel = target / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        link = self.root / "data" / "evals"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            shell=False,
        )
        try:
            with self.assertRaises(PolicyError) as caught:
                self.store.write_report("a" * 32, {"x": 1})
            self.assertEqual(caught.exception.reason_code, "policy_io_error")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse((target / ("eval-" + "a" * 32)).exists())
        finally:
            os.rmdir(link)

    def test_report_write_failure_leaves_no_partial_file(self) -> None:
        """原子发布失败清理临时文件，不留下可误认的报告/凭证。"""
        run_id = "b" * 32
        with patch("app.adapters.files.evals.os.link", side_effect=OSError("boom")):
            with self.assertRaises(PolicyError):
                self.store.write_report(run_id, {"x": 1})
        directory = self.root / "data/evals" / f"eval-{run_id}"
        self.assertFalse((directory / "report.json").exists())
        self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_job_card_requires_useful_classification(self) -> None:
        """job_card 正控制要求 interested 且无 human_review；反例被击穿。"""
        suite = json.loads(
            (ROOT / "evals/policy-suite/public.json").read_text(encoding="utf-8")
        )
        case = next(
            item
            for item in suite["cases"]
            if item["input"]["case_id"] == "eval-job-card"
        )
        positive = {
            "intent": "interested",
            "sub_intents": [],
            "escalate": None,
        }
        self.assertEqual(
            evaluate_assertions(positive, "", "C", case["assertions"]), []
        )
        negative = {
            "intent": "uncertain",
            "sub_intents": ["ambiguous"],
            "escalate": "human_review",
        }
        failures = evaluate_assertions(negative, "", "C", case["assertions"])
        self.assertIn("intent", failures)
        self.assertIn("escalate", failures)

    def test_eval_logs_real_stages_and_correlation(self) -> None:
        """eval/report/case 与 decision 阶段实际成对记录，报告关联同一 correlation。"""
        log = ProcessLog(self.root / "logs" / "eval.jsonl")
        result = eval_usecases.eval_pack(
            self.repo,
            self.store,
            "prompts/local/demo",
            "public",
            SimulatedBridge(public_decisions()),
            lambda plan: True,
            now=self.now,
            process_log=log,
        )
        report = self._report(result)
        self.assertEqual(report["correlation_id"], log.correlation_id)
        events = [
            json.loads(line)
            for line in log.path.read_text(encoding="utf-8").splitlines()
        ]
        operations = {(event["stage"], event.get("operation")) for event in events}
        self.assertIn(("eval", "suite"), operations)
        self.assertIn(("eval", "authorize"), operations)
        self.assertIn(("eval", "case"), operations)
        self.assertIn(("eval", "report"), operations)
        self.assertIn(("decision", "verify"), operations)
        self.assertIn(("decision", "bridge"), operations)
        self.assertIn(("decision", "validate"), operations)
        self.assertTrue(
            any(
                event["stage"] == "eval"
                and event.get("operation") == "case"
                and isinstance(event.get("case_index"), int)
                for event in events
            )
        )

    def test_missing_public_suite_logs_started_failed_only(self) -> None:
        """缺 public suite 时 suite 阶段只有 started→failed，不先记 ok。"""
        (self.root / "evals/policy-suite/public.json").unlink()
        log = ProcessLog(self.root / "logs" / "missing.jsonl")
        with self.assertRaises(PolicyError):
            eval_usecases.eval_pack(
                self.repo,
                self.store,
                "prompts/local/demo",
                "public",
                SimulatedBridge(public_decisions()),
                lambda plan: True,
                now=self.now,
                process_log=log,
            )
        events = [
            json.loads(line)
            for line in log.path.read_text(encoding="utf-8").splitlines()
        ]
        suite_results = [
            event["result"]
            for event in events
            if event["stage"] == "eval" and event.get("operation") == "suite"
        ]
        self.assertEqual(suite_results, ["started", "failed"])

    def test_authorization_denied_logs_started_failed_only(self) -> None:
        """authorize 返回 false 时同一阶段只有 started→failed，无 ok。"""
        log = ProcessLog(self.root / "logs" / "denied.jsonl")
        bridge = SimulatedBridge(public_decisions())
        with self.assertRaises(PolicyError) as caught:
            eval_usecases.eval_pack(
                self.repo,
                self.store,
                "prompts/local/demo",
                "public",
                bridge,
                lambda plan: False,
                now=self.now,
                process_log=log,
            )
        self.assertEqual(caught.exception.reason_code, "authorization_required")
        events = [
            json.loads(line)
            for line in log.path.read_text(encoding="utf-8").splitlines()
        ]
        results = [
            event["result"]
            for event in events
            if event["stage"] == "eval" and event.get("operation") == "authorize"
        ]
        self.assertEqual(results, ["started", "failed"])
        self.assertEqual(bridge.requests, [])

    def test_unknown_bridge_exception_is_logged_and_fixed(self) -> None:
        """未知桥异常映射 bridge_protocol，日志有终态且不写原异常。"""
        class BoomBridge(SimulatedBridge):
            """每次调用抛普通 RuntimeError。"""

            def complete(self, **kwargs: object) -> str:
                """模拟未知桥实现错误。"""
                raise RuntimeError("secret boom")

        log = ProcessLog(self.root / "logs" / "boom.jsonl")
        bridge = BoomBridge(public_decisions())
        with self.assertRaises(PolicyError):
            eval_usecases.eval_pack(
                self.repo,
                self.store,
                "prompts/local/demo",
                "public",
                bridge,
                lambda plan: True,
                now=self.now,
                process_log=log,
            )
        text = log.path.read_text(encoding="utf-8")
        events = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(
            any(
                event["stage"] == "decision"
                and event.get("operation") == "bridge"
                and event["result"] == "failed"
                and event.get("reason_code") == "bridge_protocol"
                for event in events
            )
        )
        self.assertNotIn("secret boom", text)

    def test_cli_eval_help_lists_providers_auth_and_limits(self) -> None:
        """eval --help 明确 provider 协议、认证边界、本机要求与固定诊断。"""
        out = io.StringIO()
        err = io.StringIO()
        log = ProcessLog(self.root / "logs" / "help.jsonl")
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                from app.cli.main import main

                main(["policy", "eval", "--help"], process_log=log)
        self.assertEqual(caught.exception.code, 0)
        text = out.getvalue()
        for expected in (
            "external-agent",
            "local-process",
            "m4b-agent-v1",
            "m4b-local-v1",
            "认证",
            "本机要求",
            "data/evals",
            "confirm-external-send",
        ):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
