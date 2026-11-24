"""验证 M6 实际运行：安全点、优先级抢占、预算、游标、双时钟与只读零写。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from app.adapters.sqlite.profile_store import profile_runtime_path
from app.domain.models import canonical_hash
from tests import policy_helpers
from tests import runtime_helpers as helpers


def job_key(label: str) -> str:
    """生成一个规范合成职位键。"""
    return "boss:job:" + canonical_hash({"job": label})


def conv_key(label: str) -> str:
    """生成一个规范合成会话键。"""
    return "boss:conv:" + canonical_hash({"conv": label})


class RuntimeExecuteTests(unittest.TestCase):
    """通过 CLI 组合根运行纯合成场景，不连接设备或 provider。"""

    def setUp(self) -> None:
        self.temp, self.root = helpers.runtime_project()
        self.os_root = self.root / "os-root"
        code, payload, stderr = helpers.prepare(self.root, self.os_root)
        self.assertEqual(code, 0, (payload, stderr))
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
        """读取当前 profile 的 M6 业务表行数。"""
        return helpers.row_counts(self.root)

    def test_candidate_budget_equality_stops_21st_task(self) -> None:
        """20 条候选用完后的下一条不再开始。"""
        steps = [
            helpers.step(
                jobs=(job_key(f"job-candidate-{index}"),),
                monotonic_ms=1000 + index * 1000,
                wall_now=f"2026-11-25T10:00:{index:02d}.000Z",
            )
            for index in range(21)
        ]
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/candidates.json", steps=steps
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 20)
        self.assertEqual(run["pause_reason"], "budget_exhausted")
        self.assertEqual(run["remaining_budget"]["candidates"], 0)

    def test_prepare_requires_shared_exclusion(self) -> None:
        """共享门禁占用期间显式 prepare 返回 device_busy 且不改 profile。"""
        from app.adapters.sqlite.device_gate import SharedDeviceGate
        from app.adapters.sqlite.profile_store import profile_runtime_path
        from app.domain.models import Owner, canonical_hash

        os_root = self.os_root / "prepare-blocked"
        path = profile_runtime_path(self.root, "demo")
        holder = Owner(
            id="22222222-2222-4222-8222-222222222222",
            pid=5151,
            host=canonical_hash({"host": "holder"}),
        )
        gate = SharedDeviceGate(
            profile_id="demo", profile_db_path=path, os_root=os_root
        )
        gate.acquire(
            profile_id="demo",
            profile_db_path=str(path),
            owner=holder,
            now=helpers.NOW,
        )
        before = path.read_bytes()
        code, payload, _ = helpers.prepare(self.root, os_root)
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "device_busy")  # type: ignore[index]
        self.assertEqual(path.read_bytes(), before)
        gate.release(now=helpers.NOW, converged=True)

    def test_job_overrun_is_reported_and_stops_next_work(self) -> None:
        """单职位达到 30 秒后完成在途结果、单列超支并暂停。"""
        steps = [
            helpers.step(
                jobs=(job_key("job-overrun"),), elapsed_seconds=30
            )
        ]
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/job-overrun.json", steps=steps
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["pause_reason"], "precondition_failed")
        self.assertEqual(run["overruns"][0]["kind"], "job")
        self.assertEqual(run["overruns"][0]["limit_seconds"], 30)

    def test_job_then_unread_preemption_and_draft(self) -> None:
        """职位后观察到未读：实际切换并开始处理后记录 stage_preempted 与草稿。"""
        key = conv_key("unread-a")
        helpers.seed_discovery(self.root, key)
        steps = [
            helpers.step(jobs=(job_key("job-a"),), elapsed_seconds=1, device_calls=1),
            helpers.step(
                unread=(key,),
                elapsed_seconds=1,
                device_calls=1,
                monotonic_ms=4000,
                wall_now="2026-11-25T10:00:04.000Z",
                in_progress=True,
            ),
            helpers.step(
                unread=(key,),
                jobs=(job_key("job-b"),),
                elapsed_seconds=2,
                device_calls=3,
                monotonic_ms=5000,
                wall_now="2026-11-25T10:00:05.000Z",
                decision=helpers.decision_stimulus(),
            ),
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/preempt.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        result = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(result["processed"], 2)
        self.assertEqual(len(result["preemptions"]), 1)
        event = result["preemptions"][0]
        self.assertEqual(event["from"], "job")
        self.assertEqual(event["to"], "unread")
        self.assertGreaterEqual(event["waited_ms"], 0)
        self.assertEqual(len(result["drafts"]), 1)
        self.assertEqual(result["preemptions_used"], 1)
        self.assertEqual(result["remaining_budget"]["preemptions"], 4)
        self.assertEqual(result["remaining_budget"]["candidates"], 18)
        self.assertEqual(result["actions_performed"], 0)

        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_decisions"
                ).fetchone()[0],
                1,
            )
            events = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM scheduler_events WHERE event = 'stage_preempted'"
                )
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["waited_ms"], 1000)
            # 草稿正文可在本机库内，但不得进入调度事件 payload。
            self.assertNotIn("合成草稿正文", json.dumps(events, ensure_ascii=False))
        finally:
            connection.close()

    def test_each_safe_point_conjunct_blocks_switch(self) -> None:
        """七个安全点条件逐个为假都阻止任务处理。"""
        for name in (
            "list_returned",
            "input_empty",
            "confirmation_closed",
            "no_inflight_command",
            "no_pending_action",
            "action_state_committed",
            "checkpoint_committed",
        ):
            with self.subTest(name=name):
                steps = [
                    helpers.step(
                        jobs=(job_key("job-" + name),), safe=helpers.safe_point(**{name: False})
                    )
                ]
                rel = helpers.write_scenario(
                    self.root, f"tests/fixtures/runtime/safe-{name}.json", steps=steps
                )
                code, payload, stderr = self._execute(
                    rel, os_root=self.os_root / name
                )
                self.assertEqual(code, 0, (payload, stderr))
                run = payload["result"]["run"]  # type: ignore[index]
                self.assertEqual(run["processed"], 0)
                self.assertEqual(run["pause_reason"], "precondition_failed")

    def test_unknown_unread_requires_one_verification_then_pauses(self) -> None:
        """未知未读先一次定向核对；仍未知即暂停且不取职位。"""
        steps = [
            helpers.step(
                unread_unknown=True,
                jobs=(job_key("job-unknown"),),
                verification="still_unknown",
            )
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/unknown.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["pause_reason"], "unread_unverified")

    def test_directed_verification_cleared_allows_next_priority(self) -> None:
        """一次定向核对明确无未读后可以处理职位。"""
        steps = [
            helpers.step(
                unread_unknown=True,
                jobs=(job_key("job-cleared"),),
                verification="cleared",
            )
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/cleared.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertIsNone(run["pause_reason"])

    def test_budget_equality_stops_new_work(self) -> None:
        """耗时/设备调用/候选任一等于零都在新工作前停止。"""
        cases = {
            "seconds": helpers.step(
                jobs=(job_key("job-budget-seconds"),), elapsed_seconds=900
            ),
            "device_calls": helpers.step(
                jobs=(job_key("job-budget-calls"),), device_calls=200
            ),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                rel = helpers.write_scenario(
                    self.root, f"tests/fixtures/runtime/budget-{name}.json", steps=[value]
                )
                code, payload, stderr = self._execute(rel)
                self.assertEqual(code, 0, (payload, stderr))
                run = payload["result"]["run"]  # type: ignore[index]
                self.assertEqual(run["processed"], 0)
                self.assertEqual(run["pause_reason"], "budget_exhausted")

    def test_sixth_preemption_pauses_without_falling_back(self) -> None:
        """五轮职位→未读切换后，第六次请求暂停且不继续职位。"""
        steps: list[dict[str, object]] = []
        for index in range(6):
            steps.append(
                helpers.step(
                    jobs=(job_key(f"job-{index}"),),
                    monotonic_ms=1000 + index * 2000,
                    wall_now=f"2026-11-25T10:00:{index * 2:02d}.000Z",
                )
            )
            key = conv_key(f"unread-{index}")
            helpers.seed_discovery(self.root, key)
            steps.append(
                helpers.step(
                    unread=(key,),
                    monotonic_ms=2000 + index * 2000,
                    wall_now=f"2026-11-25T10:00:{index * 2 + 1:02d}.000Z",
                    decision=helpers.decision_stimulus(),
                )
            )
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/preemption-limit.json", steps=steps
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 11)
        self.assertEqual(run["pause_reason"], "preemption_limit")
        self.assertEqual(run["preemptions_used"], 5)
        self.assertEqual(len(run["preemptions"]), 5)

    def test_restart_does_not_refill_budget_or_repeat_jobs(self) -> None:
        """同一批次用新进程恢复时保留游标、预算与已访问职位。"""
        first = [
            helpers.step(jobs=(job_key("job-restart-a"),)),
        ]
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/restart-a.json", steps=first
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["remaining_budget"]["candidates"], 19)  # type: ignore[index]
        second = [
            helpers.step(jobs=(job_key("job-restart-a"), job_key("job-restart-b"))),
        ]
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/restart-b.json", steps=second
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["remaining_budget"]["candidates"], 18)

    def test_clock_drift_pauses_before_work(self) -> None:
        """墙钟与单调时钟增量差超过 1 秒时停止。"""
        steps = [
            helpers.step(jobs=(job_key("job-clock-a"),), monotonic_ms=1000),
            helpers.step(
                jobs=(job_key("job-clock-b"),),
                monotonic_ms=1500,
                wall_now="2026-11-25T10:00:20.000Z",
            ),
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/clock.json", steps=steps)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["pause_reason"], "clock_changed")
        self.assertEqual(run["processed"], 1)

    def test_read_only_has_zero_business_writes_and_navigation(self) -> None:
        """read_only 只读可见观察，不导航、不消费未读、不写业务表。"""
        before = self._counts()
        steps = [
            helpers.step(
                unread=(conv_key("read-only"),),
                jobs=(job_key("job-read-only"),),
                decision=helpers.decision_stimulus(),
            )
        ]
        rel = helpers.write_scenario(self.root, "tests/fixtures/runtime/read-only.json", steps=steps)
        code, payload, stderr = self._execute(rel, mode="read_only")
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertTrue(run["read_only"])
        self.assertEqual(run["business_writes"], 0)
        self.assertEqual(run["navigation"], 0)
        self.assertEqual(run["model_calls"], 0)
        self.assertEqual(self._counts(), before)
        # profile 事实/任务审计也没有被读只运行隐式迁移或重写。
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 3
            )
        finally:
            connection.close()

    def test_decision_memory_is_invalidated_by_fact_change(self) -> None:
        """同输入命中记忆不重复调用；事实撤销后摘要变化并要求重审。"""
        key = conv_key("memory")
        evidence = helpers.decision_stimulus()["evidence_hash"]
        decision = helpers.decision_stimulus(evidence_hash=evidence)
        # 先写入一条允许事实，确保事实身份参与缓存键。
        from app.adapters.sqlite.profile_store import SqliteProfileStore

        profile = SqliteProfileStore(self.root, "demo", create=False, now=helpers.NOW)
        profile.open()
        try:
            profile.set_fact(
                "work_location", "合成城市甲", None, now=helpers.NOW, replace=False
            )
        finally:
            profile.close()
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
            self.assertEqual(run["processed"], 1)
            self.assertEqual(len(run["drafts"]), 1 if index == 1 else 0)
            if index == 2:
                self.assertEqual(run["decisions_reused"], 1)
        # 撤销事实后同输入重新决策并生成新草稿。
        profile = SqliteProfileStore(self.root, "demo", create=False, now=helpers.NOW)
        profile.open()
        try:
            profile.revoke_fact("work_location", now=helpers.NOW)
        finally:
            profile.close()
        helpers.seed_discovery(self.root, key, unread_count=3)
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/memory-3.json",
            steps=[helpers.step(unread=(key,), decision=decision)],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertFalse(run["drafts"][0]["reused"])
        path = profile_runtime_path(self.root, "demo")
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM drafts WHERE state = 'pending_review'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM drafts WHERE state = 'superseded'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runtime_decisions").fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_apply_job_cli_fails_closed_without_quota_contract(self) -> None:
        """缺少 apply 配额合同时 apply_job 在 profile/门禁/设备写入前拒绝。"""
        before = self._counts()
        job = job_key("apply-cli")
        code, payload, _ = policy_helpers.run_cli(
            self.root,
            [
                "action",
                "perform",
                "--profile",
                "demo",
                "--pack",
                "prompts/local/demo",
                "--action-kind",
                "apply_job",
                "--target-key",
                job,
                "--contact",
                "合成联系人",
                "--company",
                "合成公司",
                "--position",
                "合成职位",
                "--fixture",
                "tests/fixtures/runtime/apply.json",
                "--credential",
                self.credential,
                "--provider",
                self.provider,
                "--model-id",
                self.model_id,
                "--model-config-hash",
                self.config_hash,
                "--mode",
                "draft_only",
                "--confirm-device",
                "--confirm-action",
                "--authorize",
                "apply_job",
            ],
            os_root=self.os_root / "apply",
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "apply_quota_contract_missing")  # type: ignore[index]
        self.assertEqual(self._counts(), before)

    def test_bad_fixture_keys_and_malformed_json_are_rejected(self) -> None:
        """场景夹具缺键/额外键/答案键/坏 JSON 都失败关闭。"""
        bad = Path(self.root / "tests/fixtures/runtime/bad.json")
        bad.write_text("{not json", encoding="utf-8")
        code, payload, _ = self._execute("tests/fixtures/runtime/bad.json")
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "run_io_error")  # type: ignore[index]
        spec = {
            "schema_version": 1,
            "batch_id": helpers.BATCH,
            "steps": [dict(helpers.step(), expected={"exit": 0})],
        }
        bad.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        code, payload, _ = self._execute("tests/fixtures/runtime/bad.json")
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "run_io_error")  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
