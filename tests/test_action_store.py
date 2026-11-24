"""验证 M5 动作仓储的原子 pending、额度冷却、三态提交、对账与迁移。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from app.adapters.sqlite.profile_schema import (
    ACTION_INDEX_SQL,
    ACTION_TABLE_SQL,
    PROFILE_V1_EXPECTED_OBJECTS,
    initialize_profile_database,
)
from app.adapters.sqlite.profile_store import profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.domain.actions import (
    ActionObservation,
    derive_action_outcome,
    human_card,
)
from app.domain.models import Owner, canonical_hash
from app.ports.store import StoreError
from tests import action_helpers as helpers
from tests import m1_helpers
from app.process_log import ProcessLog

NOW = m1_helpers.BASE_TIME
OWNER = Owner(m1_helpers.OWNER_ID, 4242, m1_helpers.HOST_A)


def _message_key(tag: str) -> str:
    """构造形状合法的稳定消息键。"""
    return "boss:msg:" + canonical_hash({"synthetic-message": tag})


def _text_observation(
    action_key: str, observed_at: str, **overrides: object
) -> ActionObservation:
    """构造文本发送观察。"""
    return ActionObservation(
        action_key=action_key,
        action_kind="send_text",
        **helpers.observation(observed_at=observed_at, **overrides),
    )


class ActionStoreTests(unittest.TestCase):
    """覆盖唯一仓储边界内的准入、三态、锁、额度与正文不落库。"""

    def setUp(self) -> None:
        """创建临时项目根、v2 profile 库与唯一 SqliteStore。"""
        self.tmp = tempfile.TemporaryDirectory(dir=helpers.ROOT, prefix=".m5-store-")
        self.root = Path(self.tmp.name)
        from app.adapters.sqlite.profile_store import SqliteProfileStore

        profile = SqliteProfileStore(self.root, "demo", now=NOW)
        profile.open()
        profile.close()
        self.path = profile_runtime_path(self.root, "demo")
        self.clock = m1_helpers.FakeClock()
        self.faults: list[str] = []
        self.store = self._open()

    def tearDown(self) -> None:
        """关闭连接并只清理本用例临时根。"""
        self.store.close()
        self.tmp.cleanup()

    def _open(self, fault_hook=None) -> SqliteStore:
        """打开同一 profile 库的仓储连接。"""
        return SqliteStore(
            self.path,
            profile_id="demo",
            local_host=m1_helpers.HOST_A,
            monotonic=self.clock.monotonic,
            stop_checker=lambda pid: "stopped",
            fault_hook=fault_hook,
            process_log=ProcessLog(
                path=self.root / "action-store.jsonl"
            ),
        )

    def _run_task(self, preview: str = "合成预览", contact: str = "合成联系人") -> tuple[object, str]:
        """向 profile 库 upsert 合成候选并 claim/start，返回租约与会话键。"""
        result = m1_helpers.make_result(
            profile_id="demo",
            contact=contact,
            preview=preview,
            observed_at=self.clock.now(),
            snapshot_id=str(uuid.uuid4()),
        )
        self.store.upsert_snapshot(result)
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.assertIsNotNone(lease)
        lease = self.store.start(lease, self.clock.now())
        conversation = self.store.task_conversation(lease, now=self.clock.now())
        self.assertIsNotNone(conversation)
        return lease, str(conversation)

    def _begin(
        self,
        lease,
        conversation: str,
        tag: str,
        *,
        daily_sends: int = 10,
        cooldown: int = 0,
        ime_original: str | None = "synthetic-ime-default",
    ) -> str:
        """在给定租约上写文本 pending 并返回动作键。"""
        message = _message_key(tag)
        action_key = f"boss:{message}:reply"
        self.store.begin_action(
            lease,
            action_kind="send_text",
            action_key=action_key,
            target_key=message,
            conversation_key=conversation,
            authorization_json='{"synthetic":true}',
            daily_sends=daily_sends,
            reply_cooldown_seconds=cooldown,
            ime_original=ime_original,
            now=self.clock.now(),
        )
        return action_key

    def test_pending_is_written_before_execution_and_blocks_duplicate(self) -> None:
        """begin 立即落库 pending、IME 与事件；重复键与并发未决拒绝。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "one")
        record = self.store.get_action(action_key)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempt"], 1)
        events = m1_helpers.fetch_all(
            self.path,
            "SELECT event, to_state FROM action_events WHERE action_key = ?",
            (action_key,),
        )
        self.assertEqual(events, [{"event": "action_pending", "to_state": "pending"}])
        ime = self.store.load_ime_state()
        self.assertEqual(ime["original_ime"], "synthetic-ime-default")
        self.assertFalse(ime["restored"])
        with self.assertRaises(StoreError) as caught:
            self._begin(lease, conversation, "one")
        self.assertEqual(caught.exception.reason_code, "action_conflict")
        # 同任务另一动作也被未决锁拒绝。
        other_message = _message_key("other")
        with self.assertRaises(StoreError) as caught:
            self.store.begin_action(
                lease,
                action_kind="send_text",
                action_key=f"boss:{other_message}:reply",
                target_key=other_message,
                conversation_key=conversation,
                authorization_json="{}",
                daily_sends=10,
                reply_cooldown_seconds=0,
                ime_original=None,
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "action_conflict")

    def test_leased_without_start_has_zero_action_admission(self) -> None:
        """leased 但未 start 时仓储拒绝，执行器不会被调用。"""
        result = m1_helpers.make_result(
            profile_id="demo",
            observed_at=self.clock.now(),
            snapshot_id=str(uuid.uuid4()),
        )
        self.store.upsert_snapshot(result)
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        message = _message_key("leased")
        with self.assertRaises(StoreError):
            self.store.begin_action(
                lease,
                action_kind="send_text",
                action_key=f"boss:{message}:reply",
                target_key=message,
                conversation_key=f"boss:conv:{m1_helpers.identity_hash_for('合成联系人', '合成公司', '合成职位')}",
                authorization_json="{}",
                daily_sends=10,
                reply_cooldown_seconds=0,
                ime_original=None,
                now=self.clock.now(),
            )
        self.assertEqual(self.store.get_action(f"boss:{message}:reply"), None)
        self.assertEqual(m1_helpers.event_count(self.path, "task_started"), 0)

    def test_performed_commits_task_done_and_releases_device(self) -> None:
        """performed 同一事务完成任务、删除设备行并保留动作审计。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "performed")
        observation = _text_observation(action_key, self.clock.now())
        outcome = derive_action_outcome(observation)
        result = self.store.complete_action(
            lease, action_key=action_key, outcome=outcome, now=self.clock.now()
        )
        self.assertEqual(result["state"], "performed")
        self.assertTrue(result["gate_converged"])
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            )["state"],
            "done",
        )
        self.assertIsNone(
            m1_helpers.fetch_one(self.path, "SELECT * FROM device_lease")
        )
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT event FROM action_events WHERE action_key = ? ORDER BY event_id DESC LIMIT 1",
                (action_key,),
            )["event"],
            "action_completed",
        )
        # 正文与授权原值不进入动作事件。
        payloads = m1_helpers.fetch_all(
            self.path, "SELECT payload_json FROM action_events WHERE action_key = ?", (action_key,)
        )
        self.assertNotIn("合成回复正文", str(payloads))

    def test_unknown_keeps_lock_and_quarantines_device(self) -> None:
        """unknown 标 blocked、隔离设备、保留人工卡且无重试入口。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "unknown")
        observation = _text_observation(
            action_key, self.clock.now(), error="device_disconnected"
        )
        result = self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(observation),
            now=self.clock.now(),
        )
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        task = m1_helpers.fetch_one(
            self.path, 'SELECT state, reason_code FROM hr_discoveries WHERE "key" = ?', (lease.key,)
        )
        self.assertEqual(task, {"state": "blocked", "reason_code": "device_disconnected"})
        device = m1_helpers.fetch_one(self.path, "SELECT reservation_state FROM device_lease")
        self.assertEqual(device, {"reservation_state": "quarantined"})
        pending = self.store.pending_actions()
        self.assertEqual(len(pending), 1)
        card = human_card(pending[0])
        self.assertEqual(card["action_key"], action_key)
        # 未决文本继续阻断新发送，不把 unknown 当未发送。
        status = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=10,
            reply_cooldown_seconds=0,
            conversation_key=conversation,
        )
        self.assertEqual(status["reason"], "text_send_blocked")
        with self.assertRaises(StoreError) as caught:
            self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.assertEqual(caught.exception.reason_code, "lease_busy")

    def test_not_performed_unsettled_stays_pending_and_blocks_release(self) -> None:
        """已证明未点击但现场未收敛时保持 pending、blocked 与隔离。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "unsettled")
        observation = _text_observation(
            action_key,
            self.clock.now(),
            clicked=False,
            error="device_disconnected",
            confirmation_open=True,
        )
        result = self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(observation),
            now=self.clock.now(),
        )
        self.assertEqual(result["state"], "pending")
        self.assertFalse(result["gate_converged"])
        self.assertEqual(self.store.get_action(action_key)["state"], "pending")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            )["state"],
            "blocked",
        )

    def test_settled_not_performed_is_not_sent_and_releases(self) -> None:
        """现场收敛的 not_performed 正例收敛 not_sent 并允许后续任务。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "not-sent")
        observation = _text_observation(
            action_key,
            self.clock.now(),
            clicked=False,
            error="device_disconnected",
        )
        result = self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(observation),
            now=self.clock.now(),
        )
        self.assertEqual(result["state"], "not_performed")
        self.assertTrue(result["gate_converged"])
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            )["state"],
            "not_sent",
        )
        self.assertIsNone(m1_helpers.fetch_one(self.path, "SELECT * FROM device_lease"))

    def test_fault_in_action_commit_rolls_back_everything(self) -> None:
        """动作结果、任务、设备或审计任一写入失败时全部回滚。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "fault")

        def hook(name: str, connection: sqlite3.Connection) -> None:
            """在指定动作写入边界注入一次合成故障。"""
            self.faults.append(name)
            if name == "action_before_task_update":
                raise RuntimeError("synthetic fault")

        store = self._open(fault_hook=hook)
        try:
            observation = _text_observation(action_key, self.clock.now())
            with self.assertRaises(StoreError) as caught:
                store.complete_action(
                    lease,
                    action_key=action_key,
                    outcome=derive_action_outcome(observation),
                    now=self.clock.now(),
                )
            self.assertEqual(caught.exception.reason_code, "store_io_error")
            self.assertIn("action_before_task_update", self.faults)
        finally:
            store.close()
        self.assertEqual(self.store.get_action(action_key)["state"], "pending")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            )["state"],
            "running",
        )
        self.assertEqual(
            m1_helpers.fetch_one(self.path, "SELECT reservation_state FROM device_lease"),
            {"reservation_state": "held"},
        )
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT COUNT(*) AS total FROM action_events WHERE event = 'action_completed'"
            ),
            {"total": 0},
        )

    def test_crash_pending_survives_new_connection(self) -> None:
        """进程崩溃残留 pending 在新连接可读，视为未决而不重放。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "crash")
        self.store.close()
        self.store = self._open()
        record = self.store.get_action(action_key)
        self.assertEqual(record["state"], "pending")
        cards = [human_card(item) for item in self.store.pending_actions()]
        self.assertEqual(cards[0]["reason_code"], "result_unverified")

    def test_text_quota_and_cooldown_boundaries(self) -> None:
        """日额度跨任务累计；冷却截止等于 now 允许，刚过截止也允许。"""
        lease, conversation = self._run_task()
        first_key = self._begin(
            lease, conversation, "quota-1", daily_sends=2, cooldown=10
        )
        self.store.complete_action(
            lease,
            action_key=first_key,
            outcome=derive_action_outcome(
                _text_observation(first_key, self.clock.now())
            ),
            now=self.clock.now(),
        )
        # 冷却内拒绝。
        self.clock.advance(5)
        lease2, conversation2 = self._run_task(preview="合成预览二")
        self.assertEqual(conversation2, conversation)
        with self.assertRaises(StoreError) as caught:
            self._begin(
                lease2, conversation2, "quota-2", daily_sends=2, cooldown=10
            )
        self.assertEqual(caught.exception.reason_code, "cooldown_active")
        # 冷却截止等于 now 允许。
        self.clock.advance(5)
        lease2 = self.store.renew(lease2, self.clock.now())
        second_key = self._begin(
            lease2, conversation2, "quota-2", daily_sends=2, cooldown=10
        )
        self.store.complete_action(
            lease2,
            action_key=second_key,
            outcome=derive_action_outcome(
                _text_observation(second_key, self.clock.now())
            ),
            now=self.clock.now(),
        )
        # 达到日额度 2 后拒绝；未收敛的 not_performed 不消耗成功额度。
        self.clock.advance(20)
        lease3, conversation3 = self._run_task(preview="合成预览三")
        with self.assertRaises(StoreError) as caught:
            self._begin(
                lease3, conversation3, "quota-3", daily_sends=2, cooldown=10
            )
        self.assertEqual(caught.exception.reason_code, "quota_exceeded")

    def test_utc_date_rollover_and_lowered_limit_keep_usage(self) -> None:
        """UTC 跨日计数重算；调低上限按已用量扣减，换连接不清零。"""
        lease, conversation = self._run_task()
        first = self._begin(lease, conversation, "day-1", daily_sends=3, cooldown=0)
        self.store.complete_action(
            lease,
            action_key=first,
            outcome=derive_action_outcome(_text_observation(first, self.clock.now())),
            now=self.clock.now(),
        )
        status = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=3,
            reply_cooldown_seconds=0,
            conversation_key=conversation,
        )
        self.assertEqual(status["daily_count"], 1)
        # 重启连接不清零。
        self.store.close()
        self.store = self._open()
        status = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=3,
            reply_cooldown_seconds=0,
            conversation_key=conversation,
        )
        self.assertEqual(status["daily_count"], 1)
        # 跨 UTC 日重新计数。
        self.clock.advance(86400)
        lease2, conversation2 = self._run_task(preview="次日", contact="次日联系人")
        self.assertNotEqual(conversation2, conversation)
        status = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=3,
            reply_cooldown_seconds=0,
            conversation_key=conversation2,
        )
        self.assertEqual(status["daily_count"], 0)
        second = self._begin(
            lease2, conversation2, "day-2", daily_sends=3, cooldown=0
        )
        self.store.complete_action(
            lease2,
            action_key=second,
            outcome=derive_action_outcome(_text_observation(second, self.clock.now())),
            now=self.clock.now(),
        )
        # 调低为 1 后，同已有 1 条成功即停。
        self.clock.advance(1)
        lease3, conversation3 = self._run_task(preview="调低", contact="调低联系人")
        with self.assertRaises(StoreError) as caught:
            self._begin(lease3, conversation3, "day-2-low", daily_sends=1, cooldown=0)
        self.assertEqual(caught.exception.reason_code, "quota_exceeded")

    def test_profile_total_across_conversations_and_cooldown_just_past(self) -> None:
        """同日另一会话仍受 profile 总额约束；冷却刚过截止变为可发。"""
        lease, conversation = self._run_task()
        first = self._begin(lease, conversation, "conv-a", daily_sends=2, cooldown=10)
        self.store.complete_action(
            lease,
            action_key=first,
            outcome=derive_action_outcome(_text_observation(first, self.clock.now())),
            now=self.clock.now(),
        )
        self.clock.advance(11)
        lease2, conversation2 = self._run_task(preview="另一会话", contact="另一联系人")
        self.assertNotEqual(conversation2, conversation)
        second = self._begin(
            lease2, conversation2, "conv-b", daily_sends=2, cooldown=10
        )
        self.store.complete_action(
            lease2,
            action_key=second,
            outcome=derive_action_outcome(_text_observation(second, self.clock.now())),
            now=self.clock.now(),
        )
        self.clock.advance(11)
        lease3, conversation3 = self._run_task(preview="第三会话", contact="第三联系人")
        with self.assertRaises(StoreError) as caught:
            self._begin(lease3, conversation3, "conv-c", daily_sends=2, cooldown=10)
        self.assertEqual(caught.exception.reason_code, "quota_exceeded")

    def test_fault_before_action_event_also_rolls_back(self) -> None:
        """动作审计写入失败同样整体回滚任务、动作与设备。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "event-fault")

        def hook(name: str, connection: sqlite3.Connection) -> None:
            """在动作审计写入前注入一次合成故障。"""
            if name == "action_before_event":
                raise RuntimeError("synthetic event fault")

        store = self._open(fault_hook=hook)
        try:
            with self.assertRaises(StoreError):
                store.complete_action(
                    lease,
                    action_key=action_key,
                    outcome=derive_action_outcome(
                        _text_observation(action_key, self.clock.now())
                    ),
                    now=self.clock.now(),
                )
        finally:
            store.close()
        self.assertEqual(self.store.get_action(action_key)["state"], "pending")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            )["state"],
            "running",
        )
        self.assertEqual(
            m1_helpers.fetch_one(self.path, "SELECT reservation_state FROM device_lease"),
            {"reservation_state": "held"},
        )

    def test_expired_lease_cannot_begin_action(self) -> None:
        """租约到期后 begin 拒绝且零 pending/零动作事件。"""
        lease, conversation = self._run_task()
        self.clock.advance(60)
        with self.assertRaises(StoreError) as caught:
            self._begin(lease, conversation, "expired")
        self.assertEqual(caught.exception.reason_code, "stale_lease")
        self.assertEqual(self.store.get_action(f"boss:{_message_key('expired')}:reply"), None)
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path,
                "SELECT COUNT(*) AS total FROM action_events",
            ),
            {"total": 0},
        )

    def test_performed_but_unsettled_stays_unknown_and_does_not_count(self) -> None:
        """已观察到发送但 IME/现场未收敛时保留 unknown 锁，不提前计成功额度。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "performed-unsettled")
        observation = _text_observation(
            action_key, self.clock.now(), ime_current="other-ime"
        )
        outcome = derive_action_outcome(observation)
        self.assertEqual(outcome.evidence.outcome, "performed")
        self.assertTrue(outcome.pause)
        result = self.store.complete_action(
            lease, action_key=action_key, outcome=outcome, now=self.clock.now()
        )
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state, reason_code FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            ),
            {"state": "blocked", "reason_code": "result_unverified"},
        )
        status = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=10,
            reply_cooldown_seconds=0,
            conversation_key=conversation,
        )
        self.assertEqual(status["daily_count"], 0)
        self.assertEqual(status["reason"], "text_send_blocked")

    def test_cancelled_action_can_be_reconciled_later_without_reopening_task(self) -> None:
        """abandon 后仍可受控确认未执行，清锁/设备但任务保持 cancelled。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "abandon-then-confirm")
        self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(
                _text_observation(
                    action_key, self.clock.now(), error="device_disconnected"
                )
            ),
            now=self.clock.now(),
        )
        self.store.reconcile_action(
            action_key=action_key,
            resolution="abandon",
            evidence=None,
            command_quiet=False,
            scene_settled=False,
            confirmation_open=True,
            ime_restored=False,
            explicit_confirm=True,
            command_stopped=False,
            now=self.clock.now(),
        )
        evidence = m1_helpers.make_evidence(
            outcome="not_performed",
            effect="none",
            reason_code="cancelled_before_action",
            observed_at=self.clock.now(),
            action_key=action_key,
        )
        result = self.store.reconcile_action(
            action_key=action_key,
            resolution="confirm_not_performed",
            evidence=evidence,
            command_quiet=True,
            scene_settled=True,
            confirmation_open=False,
            ime_restored=True,
            explicit_confirm=True,
            command_stopped=True,
            now=self.clock.now(),
        )
        self.assertEqual(result["state"], "not_performed")
        self.assertTrue(result["gate_converged"])
        self.assertEqual(result["task_state"], "cancelled")
        self.assertEqual(self.store.pending_actions(), [])
        self.assertIsNone(
            m1_helpers.fetch_one(self.path, "SELECT * FROM device_lease")
        )
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            )["state"],
            "cancelled",
        )

    def test_old_observed_time_is_rejected_without_clearing_quota(self) -> None:
        """完成证据早于本次 pending 时拒绝，不能按错误日期清额度。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "old-time")
        observation = _text_observation(
            action_key, "2026-11-23T23:59:59.000Z"
        )
        with self.assertRaises(StoreError) as caught:
            self.store.complete_action(
                lease,
                action_key=action_key,
                outcome=derive_action_outcome(observation),
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "invalid_observation")
        self.assertEqual(self.store.get_action(action_key)["state"], "pending")
        status = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=10,
            reply_cooldown_seconds=0,
            conversation_key=conversation,
        )
        self.assertEqual(status["daily_count"], 0)

    def test_complete_updates_persisted_ime_restored_state(self) -> None:
        """IME 恢复后同一账本标记 restored，重启读取仍为已恢复。"""
        lease, conversation = self._run_task()
        action_key = self._begin(
            lease, conversation, "ime-restored", ime_original="original-ime"
        )
        observation = _text_observation(
            action_key,
            self.clock.now(),
            ime_original="original-ime",
            ime_current="original-ime",
        )
        self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(observation),
            now=self.clock.now(),
        )
        state = self.store.load_ime_state()
        self.assertTrue(state["restored"])
        self.assertEqual(state["current_ime"], "original-ime")
        self.store.close()
        self.store = self._open()
        self.assertTrue(self.store.load_ime_state()["restored"])

    def test_reconcile_rejects_other_host_owner(self) -> None:
        """不同本机 host 的 owner PID stopped 不能作为原命令停止证明。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "host-mismatch")
        self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(
                _text_observation(
                    action_key, self.clock.now(), error="device_disconnected"
                )
            ),
            now=self.clock.now(),
        )
        other_store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=m1_helpers.HOST_B,
            monotonic=self.clock.monotonic,
            stop_checker=lambda pid: "stopped",
            process_log=ProcessLog(path=self.root / "other-host.jsonl"),
        )
        try:
            evidence = m1_helpers.make_evidence(
                outcome="performed",
                effect="sent",
                before_snapshot_id=helpers.BEFORE,
                after_snapshot_id=helpers.AFTER,
                observed_at=self.clock.now(),
                action_key=action_key,
            )
            with self.assertRaises(StoreError) as caught:
                other_store.reconcile_action(
                    action_key=action_key,
                    resolution="confirm_performed",
                    evidence=evidence,
                    command_quiet=True,
                    scene_settled=True,
                    confirmation_open=False,
                    ime_restored=True,
                    explicit_confirm=True,
                    command_stopped=True,
                    now=self.clock.now(),
                )
            self.assertEqual(caught.exception.reason_code, "owner_not_stopped")
        finally:
            other_store.close()

    def test_confirm_performed_counts_once(self) -> None:
        """unknown 文本经人工 confirm_performed 只计一次成功额度。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "confirm")
        self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(
                _text_observation(
                    action_key, self.clock.now(), error="device_disconnected"
                )
            ),
            now=self.clock.now(),
        )
        before = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=10,
            reply_cooldown_seconds=0,
            conversation_key=conversation,
        )
        self.assertEqual(before["daily_count"], 0)
        evidence = m1_helpers.make_evidence(
            outcome="performed",
            effect="sent",
            before_snapshot_id=helpers.BEFORE,
            after_snapshot_id=helpers.AFTER,
            observed_at=self.clock.now(),
            action_key=action_key,
        )
        result = self.store.reconcile_action(
            action_key=action_key,
            resolution="confirm_performed",
            evidence=evidence,
            command_quiet=True,
            scene_settled=True,
            confirmation_open=False,
            ime_restored=True,
            explicit_confirm=True,
            command_stopped=True,
            now=self.clock.now(),
        )
        self.assertEqual(result["state"], "performed")
        self.assertTrue(result["gate_converged"])
        after = self.store.text_admission_status(
            now=self.clock.now(),
            daily_sends=10,
            reply_cooldown_seconds=0,
            conversation_key=conversation,
        )
        self.assertEqual(after["daily_count"], 1)
        with self.assertRaises(StoreError) as caught:
            self.store.reconcile_action(
                action_key=action_key,
                resolution="confirm_performed",
                evidence=evidence,
                command_quiet=True,
                scene_settled=True,
                confirmation_open=False,
                ime_restored=True,
                explicit_confirm=True,
                command_stopped=True,
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "action_not_ready")

    def test_abandon_keeps_lock_and_device_quarantine(self) -> None:
        """abandon 只关闭任务，动作锁与设备隔离保留。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "abandon")
        self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(
                _text_observation(
                    action_key, self.clock.now(), error="device_disconnected"
                )
            ),
            now=self.clock.now(),
        )
        result = self.store.reconcile_action(
            action_key=action_key,
            resolution="abandon",
            evidence=None,
            command_quiet=False,
            scene_settled=False,
            confirmation_open=True,
            ime_restored=False,
            explicit_confirm=True,
            command_stopped=True,
            now=self.clock.now(),
        )
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(result["gate_converged"])
        self.assertTrue(result["lock_retained"])
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, 'SELECT state, reason_code FROM hr_discoveries WHERE "key" = ?', (lease.key,)
            ),
            {"state": "cancelled", "reason_code": "cancelled_before_action"},
        )
        self.assertEqual(
            m1_helpers.fetch_one(self.path, "SELECT reservation_state FROM device_lease"),
            {"reservation_state": "quarantined"},
        )
        self.assertEqual(len(self.store.pending_actions()), 1)

    def test_reconcile_requires_scene_convergence_and_confirmation(self) -> None:
        """现场未收敛或未显式确认时不改变 pending/unknown。"""
        lease, conversation = self._run_task()
        action_key = self._begin(lease, conversation, "reconcile-guard")
        self.store.complete_action(
            lease,
            action_key=action_key,
            outcome=derive_action_outcome(
                _text_observation(
                    action_key, self.clock.now(), error="device_disconnected"
                )
            ),
            now=self.clock.now(),
        )
        evidence = m1_helpers.make_evidence(
            outcome="performed",
            effect="sent",
            before_snapshot_id=helpers.BEFORE,
            after_snapshot_id=helpers.AFTER,
            observed_at=self.clock.now(),
            action_key=action_key,
        )
        with self.assertRaises(StoreError) as caught:
            self.store.reconcile_action(
                action_key=action_key,
                resolution="confirm_performed",
                evidence=evidence,
                command_quiet=True,
                scene_settled=False,
                confirmation_open=False,
                ime_restored=True,
                explicit_confirm=True,
                command_stopped=True,
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "scene_unsettled")
        with self.assertRaises(StoreError) as caught:
            self.store.reconcile_action(
                action_key=action_key,
                resolution="confirm_performed",
                evidence=evidence,
                command_quiet=True,
                scene_settled=True,
                confirmation_open=False,
                ime_restored=True,
                explicit_confirm=False,
                command_stopped=True,
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "recovery_not_confirmed")
        self.assertEqual(self.store.get_action(action_key)["state"], "unknown")


class ProfileMigrationTests(unittest.TestCase):
    """v1 八表 profile 库可备份迁移；普通 M1 库与未知对象不宽放。"""

    def setUp(self) -> None:
        """创建临时目录。"""
        self.tmp = tempfile.TemporaryDirectory(dir=helpers.ROOT, prefix=".m5-migrate-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        """只清理本用例临时根。"""
        self.tmp.cleanup()

    def _create_v1(self, path: Path) -> None:
        """按 v1 对象集与 meta 行创建严格旧 profile 库并写入事实/任务。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            for name, (obj_type, sql) in PROFILE_V1_EXPECTED_OBJECTS.items():
                del obj_type
                connection.execute(sql)
            connection.execute(
                "INSERT INTO profile_meta(singleton, profile_id, schema_version, created_at) "
                "VALUES (1, 'demo', 1, ?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO profile_facts(fact_key, source_id, value, state, "
                "confirmed_at, expires_at, superseded_by, updated_at) "
                "VALUES ('work_location', ?, '合成城市甲', 'active', ?, NULL, NULL, ?)",
                (helpers.BEFORE, NOW, NOW),
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()

    def test_v1_profile_is_backed_up_then_adds_action_tables(self) -> None:
        """迁移先备份、后单事务加三表并保留事实；失败不污染原库。"""
        path = profile_runtime_path(self.root, "demo")
        self._create_v1(path)
        backup = initialize_profile_database(path, "demo", now=NOW)
        self.assertIsNotNone(backup)
        backups = list(path.parent.glob("runtime.sqlite3.v1-backup-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        connection = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0], 1
            )
        finally:
            connection.close()
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 2
            )
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertIn("action_records", names)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM profile_facts").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT schema_version FROM profile_meta").fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_plain_m1_or_unknown_profile_is_not_migrated(self) -> None:
        """普通 M1 五表库与额外对象库拒绝，不创建备份或动作表。"""
        from app.adapters.sqlite.schema import initialize_database

        path = profile_runtime_path(self.root, "demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        initialize_database(path)
        with self.assertRaises(Exception) as caught:
            initialize_profile_database(path, "demo", now=NOW)
        self.assertEqual(caught.exception.reason_code, "profile_mismatch")
        self.assertEqual(list(path.parent.glob("*.v1-backup-*.sqlite3")), [])

        other = profile_runtime_path(self.root, "other")
        self._create_v1(other)
        connection = sqlite3.connect(str(other))
        try:
            connection.execute("CREATE TABLE extra(value TEXT)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(Exception) as caught:
            initialize_profile_database(other, "other", now=NOW)
        self.assertEqual(caught.exception.reason_code, "profile_mismatch")
        connection = sqlite3.connect(str(other))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 1
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
