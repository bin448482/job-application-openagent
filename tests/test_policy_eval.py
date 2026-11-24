"""验证 M4b policy eval 实包加载、报告、分桶、映射与留出生命周期。"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from app.adapters.files.evals import FileEvalStore
from app.adapters.llm.bridge_a import ExternalAgentBridge
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

    def _eval(self, bridge, suite: str = "public"):
        """以显式授权回调运行一次评测。"""
        return eval_usecases.eval_pack(
            self.repo,
            self.store,
            "prompts/local/demo",
            suite,
            bridge,
            lambda plan: True,
            now=self.now,
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

    def _install_holdout(self, *, empty: bool = False) -> dict[str, object]:
        """在临时项目复制公开套件为私有留出，并写冻结登记。"""
        suite = json.loads(
            (ROOT / "evals/policy-suite/public.json").read_text(encoding="utf-8")
        )
        suite["suite"] = "holdout"
        if empty:
            suite["cases"] = []
        target = self.root / "evals/private/holdout/suite.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
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
        """冻结批次记录访问轮次；重跑非新证据，摘要变化须退役并新批次。"""
        self._install_holdout()
        bridge = SimulatedBridge(public_decisions())
        first = self._eval(bridge, "holdout")
        report = self._report(first)
        self.assertEqual(report["buckets"]["holdout"]["passed"], 6)
        self.assertIsNone(report["acceptance"])
        self.assertFalse(report["holdout"]["repeat_access"])
        self.assertTrue(report["holdout"]["new_evidence"])
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
        eval_usecases.retire_holdout(self.store, batch_id="batch-1", retired_at=self.now)
        registry = self.store.read_holdout_registry()
        self.assertEqual(registry["batches"][0]["status"], "retired")
        eval_usecases.register_holdout(
            self.store,
            batch_id="batch-2",
            version="2026-11-24",
            sample_count=6,
            annotator="synthetic-annotator-2",
            maintainer="synthetic-maintainer",
            frozen_at=self.now,
            candidate_release="candidate-2",
        )
        third = self._eval(SimulatedBridge(public_decisions()), "holdout")
        self.assertEqual(self._report(third)["holdout"]["batch_id"], "batch-2")

    def test_holdout_rejects_external_provider(self) -> None:
        """留出只允许本机 provider，外部 agent 在调用前拒绝。"""
        self._install_holdout()
        bridge = SimulatedBridge(public_decisions(), provider_id="external-agent")
        with self.assertRaises(PolicyError) as caught:
            self._eval(bridge, "holdout")
        self.assertEqual(caught.exception.reason_code, "invalid_provider")
        self.assertEqual(bridge.requests, [])

    def test_zero_sample_holdout_is_not_measured(self) -> None:
        """零样本批次写未测报告且无验收凭证，不以 100% 冒充通过。"""
        self._install_holdout(empty=True)
        bridge = SimulatedBridge(public_decisions())
        with self.assertRaises(PolicyError) as caught:
            self._eval(bridge, "holdout")
        self.assertEqual(caught.exception.reason_code, "eval_failed")
        report_path = caught.exception.issues[0].file
        report = json.loads((self.root / str(report_path)).read_text(encoding="utf-8"))
        self.assertEqual(report["buckets"]["holdout"]["status"], "not_measured")
        self.assertEqual(report["buckets"]["holdout"]["total"], 0)
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
        self._install_holdout()
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
