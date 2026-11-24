"""验证 M6 生产组合：M3 读取门禁、唯一 decide、缓存、恢复、预算与 read_only。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from app.adapters.boss.read import load_and_read_fixture
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.domain.models import canonical_hash
from app.ports.runtime import RawScene
from app.usecases.runtime import _safe_scene
from tests import policy_helpers
from tests import runtime_helpers as helpers

ROOT = Path(__file__).resolve().parents[1]


def job_key(label: str) -> str:
    """生成合成职位键（作为场景刺激，不替代 M3 job list 页面）。"""
    return "boss:job:" + canonical_hash({"job": label})


class _FakeStore:
    """提供安全点证据的只读假仓储。"""

    def __init__(self, *, pending: list[dict[str, object]] | None = None, checkpoint: bool = True) -> None:
        """保存 pending 动作与检查点状态。"""
        self._pending = pending or []
        self._checkpoint = checkpoint

    def pending_actions(self, app: str = "boss") -> list[dict[str, object]]:
        """返回配置的 pending 动作。"""
        del app
        return self._pending

    def checkpoint_status(self, batch_id: str) -> dict[str, object]:
        """返回配置的检查点状态。"""
        del batch_id
        return {
            "last_checkpoint_id": "00000000-0000-4000-8000-0000000000ff"
            if self._checkpoint
            else None,
            "checkpoint_events": 1 if self._checkpoint else 0,
        }


class SafePointEvidenceTests(unittest.TestCase):
    """七条件必须从实际读取/仓储现场派生，fixture 不能直接宣告。"""

    def setUp(self) -> None:
        self.result = load_and_read_fixture(
            ROOT / "tests/fixtures/read/job_list.json",
            "00000000-0000-4000-8000-0000000000aa",
        )

    def _safe(self, **kwargs: object) -> tuple[object, ...]:
        """调用唯一安全点派生函数。"""
        raw = kwargs.pop(
            "raw",
            RawScene("", None, 0, "synthetic-ime-default"),
        )
        store = kwargs.pop("store", _FakeStore())
        return _safe_scene(
            store=store,
            batch_id=helpers.BATCH,
            task_key="",
            read_results=kwargs.pop("read_results", (self.result.value,)),
            raw=raw,
        )

    def test_each_condition_independently_false(self) -> None:
        """逐项改变真实读取/现场/仓储证据时对应条件为假。"""
        base = self._safe()
        self.assertTrue(all(base))
        cases = {
            "list_returned": {"read_results": ()},
            "input_empty": {"raw": RawScene("草稿", None, 0)},
            "confirmation_closed": {"raw": RawScene("", "确认发送", 0)},
            "no_inflight_command": {"raw": RawScene("", None, 1)},
            "no_pending_action": {
                "store": _FakeStore(pending=[{"state": "pending"}])
            },
            "action_state_committed": {
                "store": _FakeStore(pending=[{"state": "unknown"}])
            },
            "checkpoint_committed": {"store": _FakeStore(checkpoint=False)},
        }
        for index, name in enumerate(
            (
                "list_returned",
                "input_empty",
                "confirmation_closed",
                "no_inflight_command",
                "no_pending_action",
                "action_state_committed",
                "checkpoint_committed",
            )
        ):
            with self.subTest(name=name):
                value = self._safe(**cases[name])
                self.assertFalse(value[index])
                if name not in {"no_pending_action", "action_state_committed"}:
                    self.assertTrue(all(value[:index] + value[index + 1 :]))


class RuntimeExecuteTests(unittest.TestCase):
    """通过 CLI 组合根运行 M3/decide 生产链路，不连接真实设备/provider。"""

    def setUp(self) -> None:
        self.temp, self.root = helpers.runtime_project()
        self.os_root = self.root / "os-root"
        code, payload, stderr = helpers.prepare(self.root, self.os_root)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(stderr, "")
        self.credential, self.config_hash = helpers.credential(self.root)
        self.provider, self.model_id, _ = helpers.required_owner()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _execute(self, fixture_rel: str, *, mode: str = "draft_only", os_root: Path | None = None):
        """按当前凭证运行一次 execute。"""
        return helpers.execute(
            self.root,
            os_root or self.os_root,
            fixture_rel,
            mode=mode,
            credential_rel=self.credential,
            provider=self.provider,
            model_id=self.model_id,
            model_config_hash=self.config_hash,
        )

    def _counts(self) -> dict[str, int]:
        """读取 M6 业务表行数。"""
        return helpers.row_counts(self.root)

    def test_job_then_unread_preemption_and_draft(self) -> None:
        """职位后观察到未读：实际 M3/decide 处理并记录 stage_preempted。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key)
        steps = [
            helpers.step(jobs=(job_key("job-a"),), device_calls=1),
            helpers.step(unread=(key,), in_progress=True),
            helpers.step(
                unread=(key,),
                jobs=(job_key("job-b"),),
                device_calls=1,
                decision=helpers.decision_stimulus(),
            ),
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/preempt.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 2)
        self.assertEqual(len(run["preemptions"]), 1)
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(run["preemptions_used"], 1)
        self.assertEqual(run["remaining_budget"]["candidates"], 18)
        self.assertEqual(stderr, "")

    def test_unknown_unread_requires_one_verification_then_pauses(self) -> None:
        """未知未读先一次定向核对；仍未知暂停且不取职位。"""
        steps = [
            helpers.step(
                unread_unknown=True,
                verification="still_unknown",
                read_fixture="tests/fixtures/read/page.json",
            )
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/unknown.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["pause_reason"], "unread_unverified")

    def test_non_actionable_read_does_not_finish_discovery(self) -> None:
        """页面读取不可作为未读目标时不得把发现标 done。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key)
        steps = [
            helpers.step(
                unread=(key,),
                read_fixture="tests/fixtures/read/page.json",
                decision=helpers.decision_stimulus(),
            )
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/gate.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            state = connection.execute(
                "SELECT state FROM hr_discoveries WHERE conversation_key = ?", (key,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(state, "blocked")
        self.assertEqual(payload["result"]["run"]["processed"], 1)  # type: ignore[index]

    def test_invalid_decision_is_blocked_without_draft(self) -> None:
        """非法 intent 经唯一 decide 拒绝，不写草稿、不标 done。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key)
        invalid = helpers.decision_stimulus()
        invalid["intent"] = "synthetic_invalid_intent"
        steps = [helpers.step(unread=(key,), decision=invalid)]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/invalid.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["drafts"], [])
        self.assertEqual(run["pause_reason"], "semantic_block")
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM hr_discoveries WHERE conversation_key = ?", (key,)
                ).fetchone()[0],
                "blocked",
            )
        finally:
            connection.close()

    def test_needs_human_stays_blocked_with_review_draft(self) -> None:
        """needs_human/C 保存待审草稿但任务保持 blocked，不标 done。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key)
        human = helpers.decision_stimulus()
        human["intent"] = "needs_human"
        human["sub_intents"] = ["ambiguous"]
        human["policy_tier"] = "C"
        human["escalate"] = "human_review"
        steps = [helpers.step(unread=(key,), decision=human)]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/human.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["pause_reason"], "semantic_block")
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM hr_discoveries WHERE conversation_key = ?", (key,)
                ).fetchone()[0],
                "blocked",
            )
        finally:
            connection.close()

    def test_paused_batch_resumes_without_quarantine(self) -> None:
        """暂停批次重开后 resume 保留预算/游标并可继续职位。"""
        pause = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/pause.json",
            steps=[helpers.step(unread_unknown=True, verification="still_unknown",
                                read_fixture="tests/fixtures/read/page.json")],
        )
        code, payload, stderr = self._execute(pause)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["pause_reason"], "unread_unverified")  # type: ignore[index]
        resume = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/resume.json",
            steps=[helpers.step(
                unread_unknown=True,
                verification="cleared",
                read_fixture="tests/fixtures/read/job_list.json",
                jobs=(job_key("resume-job"),),
            )],
        )
        code, payload, stderr = self._execute(resume)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertIsNone(run["pause_reason"])
        # 后续同批次不能再因恢复失败隔离门禁。
        after = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/after.json",
            steps=[helpers.step(jobs=(job_key("after-job"),))],
        )
        code, payload, stderr = self._execute(after)
        self.assertEqual(code, 0, (payload, stderr))

    def test_decision_memory_reuse_and_fact_invalidation(self) -> None:
        """有效命中返回草稿关联；事实撤销后同一语义输入重新 decide。"""
        key = helpers.m3_unread_key()
        profile = SqliteProfileStore(self.root, "demo", create=False, now=helpers.NOW)
        profile.open()
        try:
            profile.set_fact(
                "work_location", "合成城市甲", None, now=helpers.NOW, replace=False
            )
        finally:
            profile.close()
        decision = helpers.decision_stimulus()
        for index in (1, 2):
            helpers.seed_discovery(self.root, key, unread_count=index)
            rel = helpers.write_scenario(
                self.root,
                f"tests/fixtures/runtime/memory-{index}.json",
                steps=[helpers.step(unread=(key,), decision=decision)],
            )
            code, payload, stderr = self._execute(rel)
            self.assertEqual(code, 0, (payload, stderr))
            run = payload["result"]["run"]  # type: ignore[index]
            if index == 1:
                self.assertEqual(len(run["drafts"]), 1)
            else:
                self.assertEqual(run["drafts"], [])
                self.assertEqual(run["decisions_reused"], 1)
        profile = SqliteProfileStore(self.root, "demo", create=False, now=helpers.NOW)
        profile.open()
        try:
            profile.revoke_fact("work_location", now=helpers.NOW)
        finally:
            profile.close()
        helpers.seed_discovery(self.root, key, unread_count=3)
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/memory-3.json",
            steps=[helpers.step(unread=(key,), decision=decision)],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(len(run["drafts"]), 1)
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runtime_decisions").fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_corrupted_cache_is_not_reused(self) -> None:
        """损坏的决策摘要/档位不得被静默当作缓存命中。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key)
        decision = helpers.decision_stimulus()
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/cache.json",
            steps=[helpers.step(unread=(key,), decision=decision)],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "UPDATE runtime_decisions SET intent = 'corrupted_intent', policy_tier = 'A'"
            )
            connection.commit()
        finally:
            connection.close()
        helpers.seed_discovery(self.root, key, unread_count=2)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["drafts"], [])
        self.assertEqual(run["decisions_reused"], 0)
        self.assertEqual(run["pause_reason"], "semantic_block")

    def test_device_call_budget_equality_stops_new_work(self) -> None:
        """设备调用达到上限后不再开始新任务。"""
        steps = [helpers.step(jobs=(job_key("budget"),), device_calls=200)]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/calls.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["pause_reason"], "budget_exhausted")
        self.assertEqual(run["remaining_budget"]["device_calls"], 0)

    def test_candidate_budget_equality_stops_21st_task(self) -> None:
        """20 条候选用完后的下一条不再开始。"""
        steps = [
            helpers.step(jobs=(job_key(f"candidate-{index}"),), read_fixture="tests/fixtures/read/page.json")
            for index in range(21)
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/candidates.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 20)
        self.assertEqual(run["pause_reason"], "budget_exhausted")
        self.assertEqual(run["remaining_budget"]["candidates"], 0)

    def test_restart_does_not_refill_budget(self) -> None:
        """同批次新进程恢复不补预算。"""
        first = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/restart-a.json",
            steps=[helpers.step(jobs=(job_key("restart-a"),), read_fixture="tests/fixtures/read/page.json")],
        )
        code, payload, stderr = self._execute(first)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["remaining_budget"]["candidates"], 19)  # type: ignore[index]
        second = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/restart-b.json",
            steps=[helpers.step(jobs=(job_key("restart-b"),), read_fixture="tests/fixtures/read/page.json")],
        )
        code, payload, stderr = self._execute(second)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["remaining_budget"]["candidates"], 18)  # type: ignore[index]

    def test_read_only_has_zero_business_writes(self) -> None:
        """read_only 只经 M3 读取可见观察，不写业务表/不导航/不调用模型。"""
        before = self._counts()
        key = helpers.m3_unread_key()
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/read-only.json",
            steps=[helpers.step(unread=(key,), decision=helpers.decision_stimulus())],
        )
        code, payload, stderr = self._execute(rel, mode="read_only")
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertTrue(run["read_only"])
        self.assertEqual(run["business_writes"], 0)
        self.assertEqual(run["navigation"], 0)
        self.assertEqual(run["model_calls"], 0)
        self.assertEqual(self._counts(), before)

    def test_missing_device_confirmation_blocks_before_external_work(self) -> None:
        """缺本轮确认时生产入口固定拒绝，零业务写/模型/门禁取得。"""
        before = self._counts()
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/no-confirm.json",
            steps=[helpers.step(unread=(helpers.m3_unread_key(),))],
        )
        code, payload, stderr = helpers.execute(
            self.root,
            self.os_root,
            rel,
            mode="read_only",
            confirm_device=False,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "authorization_required")  # type: ignore[index]
        self.assertEqual(stderr, "")
        self.assertEqual(self._counts(), before)

    def test_prepare_requires_shared_exclusion(self) -> None:
        """共享门禁占用期间显式 prepare 返回 device_busy 且不改 profile。"""
        from app.adapters.sqlite.device_gate import SharedDeviceGate
        from app.domain.models import Owner

        os_root = self.os_root / "prepare-blocked"
        path = profile_runtime_path(self.root, "demo")
        holder = Owner(
            id="22222222-2222-4222-8222-222222222222",
            pid=5151,
            host=canonical_hash({"host": "holder"}),
        )
        gate = SharedDeviceGate(profile_id="demo", profile_db_path=path, os_root=os_root)
        gate.acquire(profile_id="demo", profile_db_path=str(path), owner=holder, now=helpers.NOW)
        before = path.read_bytes()
        code, payload, _ = helpers.prepare(self.root, os_root)
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "device_busy")  # type: ignore[index]
        self.assertEqual(path.read_bytes(), before)
        gate.release(now=helpers.NOW, converged=True)

    def test_apply_job_cli_fails_closed_without_quota_contract(self) -> None:
        """缺少 apply 配额合同时 CLI 在任何 profile/门禁/设备写入前拒绝。"""
        before = self._counts()
        job = job_key("apply-cli")
        code, payload, _ = policy_helpers.run_cli(
            self.root,
            [
                "action", "perform", "--profile", "demo", "--pack", "prompts/local/demo",
                "--action-kind", "apply_job", "--target-key", job,
                "--contact", "合成联系人", "--company", "合成公司", "--position", "合成职位",
                "--fixture", "tests/fixtures/runtime/apply.json",
                "--credential", self.credential, "--provider", self.provider,
                "--model-id", self.model_id, "--model-config-hash", self.config_hash,
                "--mode", "draft_only", "--confirm-device", "--confirm-action",
                "--authorize", "apply_job",
            ],
            os_root=self.os_root / "apply",
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "apply_quota_contract_missing")  # type: ignore[index]
        self.assertEqual(self._counts(), before)

    def test_bad_fixture_and_paths_are_rejected(self) -> None:
        """坏 JSON/越界路径不进入运行，也不降级成功。"""
        bad = Path(self.root / "tests/fixtures/runtime/bad.json")
        bad.write_text("{not json", encoding="utf-8")
        code, payload, stderr = self._execute("tests/fixtures/runtime/bad.json")
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "invalid_input")  # type: ignore[index]
        self.assertEqual(stderr, "")
        code, payload, _ = helpers.execute(
            self.root,
            self.os_root,
            str(self.root / "absolute.json"),
            mode="read_only",
            credential_rel=self.credential,
            provider=self.provider,
            model_id=self.model_id,
            model_config_hash=self.config_hash,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "invalid_input")  # type: ignore[index]


class _Helpers:
    """为测试文件提供 policy_helpers 引用，避免顶层导入噪音。"""

    policy_helpers = helpers.policy_helpers


if __name__ == "__main__":
    unittest.main()
