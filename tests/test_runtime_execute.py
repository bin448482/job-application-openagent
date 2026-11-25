"""验证 M6 生产组合：M3 读取门禁、唯一 decide、缓存、恢复、预算与 read_only。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
from pathlib import Path

from app.adapters.boss.read import load_and_read_fixture
from app.adapters.files.evals import FileEvalStore
from app.adapters.files.policy import FilePolicyRepository
from app.adapters.simulated.runtime import (
    FixtureM3Read,
    StimulusRuntimeScene,
    SyntheticDecisionBridge,
)
from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.domain.models import canonical_hash
from app.domain.policy import utc_now
from app.domain.runtime import BudgetLimits
from app.ports.runtime import RawScene
from app.usecases import policy as policy_usecases
from app.usecases.policy_eval import RepositorySnapshot
from app.usecases.runtime import derive_safe_point, execute_run
from tests import policy_helpers
from tests import runtime_helpers as helpers

ROOT = Path(__file__).resolve().parents[1]


def job_key(label: str) -> str:
    """生成合成职位键（只用于动作合同等非场景权威输入）。"""
    return "boss:job:" + canonical_hash({"job": label})


class _FakeStore:
    """提供安全点证据的只读假仓储。"""

    def __init__(
        self,
        *,
        pending: list[dict[str, object]] | None = None,
        last_state: str | None = None,
        checkpoint: bool = True,
    ) -> None:
        """保存 pending 动作、最近动作状态与检查点状态。"""
        self._pending = pending or []
        self._last_state = last_state
        self._checkpoint = checkpoint

    def pending_actions(self, app: str = "boss") -> list[dict[str, object]]:
        """返回配置的 pending 动作。"""
        del app
        return self._pending

    def last_action_state(self, task_key: str) -> str | None:
        """返回配置的最近动作终态。"""
        del task_key
        return self._last_state

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

    def _safe(self, **kwargs: object) -> object:
        """调用唯一安全点派生入口。"""
        raw = kwargs.pop(
            "raw",
            RawScene("", None, 0, "synthetic-ime-default"),
        )
        store = kwargs.pop("store", _FakeStore())
        read_results = kwargs.pop("read_results", (self.result,))
        task_key = kwargs.pop("task_key", None)
        if task_key is None and read_results:
            first = read_results[0]
            value = getattr(first, "value", None)
            cards = getattr(value, "cards", None)
            if cards:
                task_key = cards[0].job_key
        return derive_safe_point(
            store=store,
            batch_id=helpers.BATCH,
            task_key=task_key,
            read_results=read_results,
            raw=raw,
        )

    def test_each_condition_independently_false(self) -> None:
        """纯领域假仓储可逐项置假；真实仓储的第五/六项蕴含另测。"""
        base = self._safe()
        self.assertTrue(base.is_safe())  # type: ignore[attr-defined]
        cases = {
            "list_returned": {"read_results": ()},
            "input_empty": {"raw": RawScene("草稿", None, 0)},
            "confirmation_closed": {"raw": RawScene("", "确认发送", 0)},
            "no_inflight_command": {"raw": RawScene("", None, 1)},
            "no_pending_action": {"store": _FakeStore(pending=[{"state": "pending"}])},
            "action_state_committed": {"store": _FakeStore(last_state="unknown")},
            "checkpoint_committed": {"store": _FakeStore(checkpoint=False)},
        }
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
                value = self._safe(**cases[name])
                self.assertFalse(getattr(value, name))

    def test_pending_and_last_action_are_independent(self) -> None:
        """有 pending 但最近动作已提交时两条件可一假一真。"""
        value = self._safe(
            store=_FakeStore(pending=[{"state": "pending"}], last_state="performed")
        )
        self.assertFalse(value.no_pending_action)  # type: ignore[attr-defined]
        self.assertTrue(value.action_state_committed)  # type: ignore[attr-defined]

    def test_non_actionable_page_is_not_list_returned(self) -> None:
        """不可行动页面即使有结果也不算安全点第一项。"""
        from app.domain.models import ReadResult

        failed = ReadResult(
            status="failed",
            value=None,
            reason_code="wrong_page",
            snapshot_id=None,
            observed_at=helpers.NOW,
            source="fixture",
            confidence="low",
        )
        self.assertFalse(
            self._safe(read_results=(failed,), task_key=None).list_returned  # type: ignore[attr-defined]
        )


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

    def _execute(
        self, fixture_rel: str, *, mode: str = "draft_only", os_root: Path | None = None
    ):
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
        """职位后观察到实际未读：经 M3/decide 处理并记录 stage_preempted。"""
        job_manifest, _job_key = helpers.make_job_fixture(self.root, "preempt")
        unread_manifest, key = helpers.make_unread_fixture(self.root, "preempt")
        conversation = "tests/fixtures/runtime/read-preempt/conversation.json"
        helpers.seed_discovery(self.root, key)
        steps = [
            helpers.step(read_fixture=job_manifest, job_fixture=job_manifest),
            helpers.step(
                read_fixture=unread_manifest,
                conversation_fixture=conversation,
                in_progress=True,
            ),
            helpers.step(
                read_fixture=unread_manifest,
                conversation_fixture=conversation,
                decision=helpers.decision_stimulus(),
            ),
        ]
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/preempt.json", steps=steps
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 2)
        self.assertEqual(len(run["preemptions"]), 1)
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(run["preemptions_used"], 1)
        self.assertLess(run["preemptions"][0]["waited_ms"], 5000)
        self.assertGreaterEqual(run["preemptions"][0]["waited_ms"], 0)
        self.assertEqual(stderr, "")

    def test_actual_m2_new_message_precedes_job(self) -> None:
        """实际 M2 解析+upsert 的 queued new_message 在职位前处理。"""
        key = helpers.seed_new_message(self.root, "actual")
        job_manifest, _job_key = helpers.make_job_fixture(self.root, "newmsg-job")
        steps = [
            helpers.step(
                read_fixture="tests/fixtures/read/job_list.json",
                conversation_fixture="tests/fixtures/read/conversation.json",
                decision=helpers.decision_stimulus(),
            ),
            helpers.step(read_fixture=job_manifest, job_fixture=job_manifest),
        ]
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/new-message.json", steps=steps
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 2)
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(run["drafts"][0]["task_key"], key)
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM hr_discoveries WHERE kind = 'new_message'"
                ).fetchone()[0],
                "done",
            )
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state = 'done'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_new_message_hint_only_is_not_a_task(self) -> None:
        """没有实际 M2 发现时，兼容 new_messages 提示不产生任务/草稿。"""
        key = helpers.m3_unread_key()
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/new-message-hint.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/page.json",
                    new_messages=(key,),
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["drafts"], [])
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM hr_discoveries WHERE kind = 'new_message'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_unread_precedes_same_conversation_new_message(self) -> None:
        """同会话未读优先，且处理未读后不再把旧 queued new_message 当新任务。"""
        key = helpers.m3_unread_key()
        new_key = helpers.seed_new_message(self.root, "coexist")
        helpers.seed_discovery(self.root, key, unread_count=1)
        self.assertEqual(new_key, key)
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/unread-new-message.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/unread.json",
                    conversation_fixture="tests/fixtures/read/conversation.json",
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(run["drafts"][0]["task_key"], key)
        from app.adapters.sqlite.repo import SqliteStore

        store = SqliteStore(
            profile_runtime_path(self.root, "demo"),
            profile_id="demo",
            require_runtime=True,
        )
        try:
            self.assertEqual(store.queued_new_message_keys(), ())
        finally:
            store.close()

    def test_new_message_without_actual_conversation_is_blocked(self) -> None:
        """queued 记录不能替代同键实际 M3 会话证据。"""
        helpers.seed_new_message(self.root, "no-conversation")
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/new-message-no-conv.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/page.json",
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["drafts"], [])
        self.assertEqual(run["pause_reason"], "semantic_block")

    def test_fake_job_hint_is_not_authority(self) -> None:
        """兼容 jobs hint 不在真实读取中时不得产生职位候选或 done。"""
        fake = job_key("fake-hint")
        for index, job_fixture in enumerate(
            (None, "tests/fixtures/read/job_list.json")
        ):
            rel = helpers.write_scenario(
                self.root,
                f"tests/fixtures/runtime/fake-job-{index}.json",
                steps=[
                    helpers.step(
                        read_fixture="tests/fixtures/read/page.json",
                        jobs=(fake,),
                        job_fixture=job_fixture,
                    )
                ],
            )
            code, payload, stderr = self._execute(rel)
            self.assertEqual(code, 0, (payload, stderr))
            self.assertEqual(payload["result"]["run"]["processed"], 0)  # type: ignore[index]
            self.assertIsNone(payload["result"]["run"]["pause_reason"])  # type: ignore[index]
            connection = sqlite3.connect(
                str(profile_runtime_path(self.root, "demo"))
            )
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                )
            finally:
                connection.close()

    def test_cleanup_hint_is_ignored_without_isolation(self) -> None:
        """未实现 cleanup 的兼容提示不进入 claim、不隔离设备。"""
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/cleanup.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/page.json",
                    cleanup=("cleanup-hint",),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["processed"], 0)  # type: ignore[index]
        gate = SharedDeviceGate(
            profile_id="demo",
            profile_db_path=profile_runtime_path(self.root, "demo"),
            os_root=self.os_root,
        )
        self.assertIsNone(gate.inspect())

    def test_unread_unknown_hint_alone_does_not_pause(self) -> None:
        """完全相同 page 读取下，仅切换兼容 unknown 提示不改变结果。"""
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/unknown-hint.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/page.json",
                    unread_unknown=True,
                    verification="still_unknown",
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 0)
        self.assertIsNone(run["pause_reason"])

    def test_unknown_unread_requires_one_verification_then_pauses(self) -> None:
        """实际 M3 未读为 unknown 时先一次定向核对；仍未知暂停且不取职位。"""
        manifest, _key = helpers.make_unknown_unread_fixture(self.root, "unknown")
        steps = [
            helpers.step(
                read_fixture=manifest,
                verification="still_unknown",
            )
        ]
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/unknown.json", steps=steps
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["pause_reason"], "unread_unverified")

    def test_non_actionable_read_does_not_finish_discovery(self) -> None:
        """页面读取不可作为未读目标时不得认领或把发现标 done。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key)
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/gate.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/page.json",
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["processed"], 0)  # type: ignore[index]
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            state = connection.execute(
                "SELECT state FROM hr_discoveries WHERE conversation_key = ?", (key,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(state, "done")

    def test_invalid_decision_is_blocked_without_draft(self) -> None:
        """非法 intent 经唯一 decide 拒绝，不写草稿、不标 done。"""
        manifest, key = helpers.make_unread_fixture(self.root, "invalid")
        conversation = "tests/fixtures/runtime/read-invalid/conversation.json"
        helpers.seed_discovery(self.root, key)
        invalid = helpers.decision_stimulus()
        invalid["intent"] = "synthetic_invalid_intent"
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/invalid.json",
            steps=[
                helpers.step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=invalid,
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["drafts"], [])
        self.assertEqual(run["pause_reason"], "semantic_block")
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM hr_discoveries WHERE conversation_key = ?",
                    (key,),
                ).fetchone()[0],
                "blocked",
            )
        finally:
            connection.close()

    def test_needs_human_stays_blocked_with_review_draft(self) -> None:
        """needs_human/C 保存待审草稿但任务保持 blocked，不标 done。"""
        manifest, key = helpers.make_unread_fixture(self.root, "human")
        conversation = "tests/fixtures/runtime/read-human/conversation.json"
        helpers.seed_discovery(self.root, key)
        human = helpers.decision_stimulus()
        human.update(
            {
                "intent": "needs_human",
                "sub_intents": ["ambiguous"],
                "policy_tier": "C",
                "escalate": "human_review",
            }
        )
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/human.json",
            steps=[
                helpers.step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=human,
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["pause_reason"], "semantic_block")  # type: ignore[index]
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM hr_discoveries WHERE conversation_key = ?",
                    (key,),
                ).fetchone()[0],
                "blocked",
            )
        finally:
            connection.close()

    def test_paused_batch_resumes_without_quarantine(self) -> None:
        """实际 unknown 未读暂停后，恢复批次并处理实际 M3 职位。"""
        unknown_manifest, _ = helpers.make_unknown_unread_fixture(self.root, "pause")
        pause = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/pause.json",
            steps=[helpers.step(read_fixture=unknown_manifest, verification="still_unknown")],
        )
        code, payload, stderr = self._execute(pause)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["pause_reason"], "unread_unverified")  # type: ignore[index]
        real_job = helpers.m3_job_keys()[0]
        resume = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/resume.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/job_list.json",
                    jobs=(real_job,),
                )
            ],
        )
        code, payload, stderr = self._execute(resume)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertIsNone(run["pause_reason"])
        after = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/after.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/job_list.json",
                    jobs=(helpers.m3_job_keys()[1],),
                )
            ],
        )
        code, payload, stderr = self._execute(after)
        self.assertEqual(code, 0, (payload, stderr))

    def test_decision_memory_reuse_and_fact_invalidation(self) -> None:
        """有效命中返回草稿关联；事实撤销后同一语义输入重新 decide。"""
        manifest, key = helpers.make_unread_fixture(self.root, "memory")
        conversation = "tests/fixtures/runtime/read-memory/conversation.json"
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
                steps=[
                    helpers.step(
                        read_fixture=manifest,
                        conversation_fixture=conversation,
                        decision=decision,
                    )
                ],
            )
            code, payload, stderr = self._execute(rel)
            self.assertEqual(code, 0, (payload, stderr))
            run = payload["result"]["run"]  # type: ignore[index]
            if index == 1:
                self.assertEqual(len(run["drafts"]), 1)
            else:
                self.assertEqual(run["drafts"], [])
                self.assertEqual(run["decisions_reused"], 1)
                self.assertEqual(run["model_calls"], 0)
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
            steps=[
                helpers.step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=decision,
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(len(run["drafts"]), 1)
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runtime_decisions").fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_evidence_identity_change_is_not_reused(self) -> None:
        """正文相同但稳定消息身份/时间变化时必须重新调用模型。"""
        base_manifest, key = helpers.make_unread_fixture(self.root, "fresh")
        source = self.root / "tests/fixtures/runtime/read-fresh"
        for label, stable, time_hint in (
            ("a", "msg-3a", "10:03"),
            ("b", "msg-3b", "10:09"),
        ):
            target = self.root / "tests/fixtures/runtime" / f"fresh-{label}"
            shutil.copytree(source, target)
            path = target / "conversation-1.xml"
            text = path.read_text(encoding="utf-8")
            text = text.replace('content-desc="msg-3"', f'content-desc="{stable}"')
            text = text.replace('text="10:03"', f'text="{time_hint}"')
            path.write_text(text, encoding="utf-8")
        decision = helpers.decision_stimulus()
        for index, label in enumerate(("a", "b")):
            helpers.seed_discovery(self.root, key, unread_count=index + 1)
            manifest = f"tests/fixtures/runtime/fresh-{label}/conversation.json"
            rel = helpers.write_scenario(
                self.root,
                f"tests/fixtures/runtime/fresh-{label}.json",
                steps=[
                    helpers.step(
                        read_fixture=base_manifest,
                        conversation_fixture=manifest,
                        decision=decision,
                    )
                ],
            )
            code, payload, stderr = self._execute(rel)
            self.assertEqual(code, 0, (payload, stderr))
            run = payload["result"]["run"]  # type: ignore[index]
            if index == 0:
                self.assertEqual(len(run["drafts"]), 1)
            else:
                self.assertEqual(run["decisions_reused"], 0)
                self.assertEqual(len(run["drafts"]), 1)

    def test_corrupted_cache_is_not_reused(self) -> None:
        """损坏的决策摘要/档位不得被静默当作缓存命中。"""
        manifest, key = helpers.make_unread_fixture(self.root, "corrupt")
        conversation = "tests/fixtures/runtime/read-corrupt/conversation.json"
        helpers.seed_discovery(self.root, key)
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/cache.json",
            steps=[
                helpers.step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
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

    def test_consistent_corruption_is_not_reused(self) -> None:
        """摘要与草稿被一致改成非法 intent/A 时仍被当前 pack 语义拒绝。"""
        manifest, key = helpers.make_unread_fixture(self.root, "consistent")
        conversation = "tests/fixtures/runtime/read-consistent/conversation.json"
        helpers.seed_discovery(self.root, key)
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/consistent.json",
            steps=[
                helpers.step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            connection.execute(
                "UPDATE runtime_decisions SET intent='synthetic_invalid_intent', policy_tier='A'"
            )
            connection.execute(
                "UPDATE drafts SET intent='synthetic_invalid_intent', policy_tier='A'"
            )
            connection.commit()
        finally:
            connection.close()
        helpers.seed_discovery(self.root, key, unread_count=2)
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["decisions_reused"], 0)
        self.assertEqual(run["drafts"], [])
        self.assertEqual(run["pause_reason"], "semantic_block")

    def test_device_call_fixture_declaration_is_not_authority(self) -> None:
        """fixture 声明的 device_calls 不参与预算，实际读取次数才是计数。"""
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/declared-calls.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/job_list.json",
                    device_calls=200,
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["remaining_budget"]["device_calls"], 199)

    def test_negative_overrun_uses_actual_reads(self) -> None:
        """实际两次 M3 读取可越过小预算并保留负超支证据。"""
        manifest, key = helpers.make_unread_fixture(self.root, "overrun")
        conversation = "tests/fixtures/runtime/read-overrun/conversation.json"
        helpers.seed_discovery(self.root, key)
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/overrun.json",
            steps=[
                helpers.step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    device_calls=0,
                )
            ],
        )
        spec = json.loads((self.root / rel).read_text(encoding="utf-8"))
        pack_rel, base_files, _a, _b, pack = policy_usecases.load_active_pack(
            FilePolicyRepository(self.root), "prompts/local/demo"
        )
        path = profile_runtime_path(self.root, "demo")
        os_root = self.os_root / "overrun"
        gate = SharedDeviceGate(
            profile_id="demo", profile_db_path=path, os_root=os_root
        )
        profile_store = SqliteProfileStore(
            self.root, "demo", create=False, now=utc_now()
        )
        scene = StimulusRuntimeScene(spec, profile_id="demo")
        owner = helpers.owner()

        def open_store() -> SqliteStore:
            """按当前 profile 路径打开运行仓储。"""
            return SqliteStore(
                path, profile_id="demo", local_host=owner.host, require_runtime=True
            )

        result = execute_run(
            base_files=base_files,
            pack=pack,
            mode="draft_only",
            confirm_device=True,
            gate=gate,
            profile_store=profile_store,
            profile_db_path=str(path),
            owner=owner,
            now=utc_now(),
            scene=scene,
            read=FixtureM3Read(self.root),
            bridge_factory=lambda stimulus: SyntheticDecisionBridge(
                stimulus,
                provider_id=self.provider,
                model_id=self.model_id,
                model_config_hash=self.config_hash,
            ),
            snapshot=RepositorySnapshot(FilePolicyRepository(self.root), pack_rel),
            open_store=open_store,
            limits=BudgetLimits(device_calls=1),
            provider_id=self.provider,
            model_id=self.model_id,
            model_config_hash=self.config_hash,
            eval_store=FileEvalStore(self.root),
            credential_rel=self.credential,
        )
        run = result["run"]
        self.assertEqual(run["pause_reason"], "budget_exhausted")
        self.assertEqual(run["remaining_budget"]["device_calls"], -1)
        self.assertTrue(
            any(item["kind"] == "device_calls" for item in run["overruns"])
        )

    def test_candidate_budget_equality_stops_21st_task(self) -> None:
        """20 条实际职位读取候选用完后下一条不再开始。"""
        steps = []
        for index in range(21):
            manifest, _key = helpers.make_job_fixture(self.root, f"budget{index}")
            steps.append(helpers.step(read_fixture=manifest, job_fixture=manifest))
        rel = helpers.write_scenario(
            self.root, "tests/fixtures/runtime/candidates.json", steps=steps
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertEqual(run["processed"], 20)
        self.assertEqual(run["pause_reason"], "budget_exhausted")
        self.assertEqual(run["remaining_budget"]["candidates"], 0)

    def test_restart_does_not_refill_budget(self) -> None:
        """同批次新进程恢复不补预算。"""
        first_manifest, _ = helpers.make_job_fixture(self.root, "restartA")
        first = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/restart-a.json",
            steps=[helpers.step(read_fixture=first_manifest, job_fixture=first_manifest)],
        )
        code, payload, stderr = self._execute(first)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["remaining_budget"]["candidates"], 19)  # type: ignore[index]
        second_manifest, _ = helpers.make_job_fixture(self.root, "restartB")
        second = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/restart-b.json",
            steps=[helpers.step(read_fixture=second_manifest, job_fixture=second_manifest)],
        )
        code, payload, stderr = self._execute(second)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(payload["result"]["run"]["remaining_budget"]["candidates"], 18)  # type: ignore[index]

    def test_completed_batch_is_safe_for_restore(self) -> None:
        """正常安全结束后 batch completed；restore 活动检查为 stopped。"""
        manifest, _key = helpers.make_job_fixture(self.root, "complete")
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/complete.json",
            steps=[helpers.step(read_fixture=manifest, job_fixture=manifest)],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            self.assertEqual(
                connection.execute("SELECT state FROM job_batches").fetchone()[0],
                "completed",
            )
        finally:
            connection.close()
        from app.adapters.sqlite.activity import ProfilePolicyActivity

        activity = ProfilePolicyActivity(self.root, os_root=self.os_root / "restore")
        self.assertEqual(
            activity.assess_restore(profile_id="demo", dest_pack="prompts/local/x"),
            "stopped",
        )

    def test_unresolved_queued_job_blocks_restore(self) -> None:
        """完成后仍有 queued 职位时 restore 保守返回 unknown。"""
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/queued.json",
            steps=[
                helpers.step(
                    read_fixture="tests/fixtures/read/job_list.json",
                    jobs=(helpers.m3_job_keys()[0],),
                )
            ],
        )
        code, payload, stderr = self._execute(rel)
        self.assertEqual(code, 0, (payload, stderr))
        connection = sqlite3.connect(str(profile_runtime_path(self.root, "demo")))
        try:
            queued = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state = 'queued'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(queued, 0)
        from app.adapters.sqlite.activity import ProfilePolicyActivity

        activity = ProfilePolicyActivity(self.root, os_root=self.os_root / "queued")
        self.assertEqual(
            activity.assess_restore(profile_id="demo", dest_pack="prompts/local/x"),
            "unknown",
        )

    def test_read_only_has_zero_business_writes(self) -> None:
        """read_only 只经 M3 读取可见观察，不写业务表/不导航/不调用模型。"""
        before = self._counts()
        manifest, _key = helpers.make_unread_fixture(self.root, "readonly")
        conversation = "tests/fixtures/runtime/read-readonly/conversation.json"
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/read-only.json",
            steps=[
                helpers.step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        code, payload, stderr = self._execute(rel, mode="read_only")
        self.assertEqual(code, 0, (payload, stderr))
        run = payload["result"]["run"]  # type: ignore[index]
        self.assertTrue(run["read_only"])
        self.assertEqual(run["business_writes"], 0)
        self.assertEqual(run["navigation"], 0)
        self.assertEqual(run["model_calls"], 0)
        self.assertEqual(run["observed"]["unread"], 1)
        self.assertEqual(run["observed"]["jobs"], 0)
        self.assertEqual(self._counts(), before)

    def test_missing_device_confirmation_blocks_before_external_work(self) -> None:
        """缺本轮确认时生产入口固定拒绝，零业务写/模型/门禁取得。"""
        before = self._counts()
        rel = helpers.write_scenario(
            self.root,
            "tests/fixtures/runtime/no-confirm.json",
            steps=[helpers.step(read_fixture="tests/fixtures/read/page.json")],
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
        from app.domain.models import Owner

        os_root = self.os_root / "prepare-blocked"
        path = profile_runtime_path(self.root, "demo")
        holder = Owner(
            id="22222222-2222-4222-8222-222222222222",
            pid=5151,
            host=canonical_hash({"host": "holder"}),
        )
        gate = SharedDeviceGate(profile_id="demo", profile_db_path=path, os_root=os_root)
        gate.acquire(
            profile_id="demo", profile_db_path=str(path), owner=holder, now=helpers.NOW
        )
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
