"""验证 M6 批次/草稿持久化事务、typed action 引用、apply_job 三态与旧库迁移。"""

from __future__ import annotations

import json
import sqlite3
import unittest
import uuid

from app.adapters.sqlite.profile_schema import (
    PROFILE_SCHEMA_VERSION,
    PROFILE_V2_EXPECTED_OBJECTS,
    initialize_profile_database,
)
from app.adapters.sqlite.profile_store import profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.domain.actions import (
    ActionObservation,
    action_key_for,
    derive_action_outcome,
)
from app.domain.models import (
    APP,
    Owner,
    canonical_hash,
    conversation_key_for,
)
from app.domain.runtime import BatchCursor, BudgetLimits
from app.ports.store import StoreError
from app.domain.policy import utc_now as now_utc

ROOT_NOW = "2026-11-25T10:00:00.000Z"


def owner(host: str) -> Owner:
    """返回与指定本机摘要匹配的合成租约持有者。"""
    return Owner(
        id=str(uuid.uuid4()),
        pid=4242,
        host=host,
    )


def job(label: str) -> str:
    """构造规范职位键。"""
    return "boss:job:" + canonical_hash({"job": label})


def apply_observation(action_key: str, *, outcome: str = "done") -> ActionObservation:
    """构造一次 apply_job 观察；outcome 只决定刺激，不直接给 Evidence。"""
    before = str(uuid.uuid4())
    after = str(uuid.uuid4())
    performed = outcome == "done"
    return ActionObservation(
        action_key=action_key,
        action_kind="apply_job",
        before_snapshot_id=before,
        after_snapshot_id=after,
        observed_at=now_utc(),
        clicked=True,
        error=None if performed else "device_disconnected",
        scene_settled=True,
        command_quiet=True,
        confirmation_open=False,
        input_box_empty=True,
        ime_original=None,
        ime_current=None,
        body_readback_matches=False,
        echo_count_before=0,
        echo_count_after=0,
        attachment_name_matched=False,
        attachment_request_observed=False,
        delivery_observed=False,
        contact_slot_available=False,
        contact_slot_status=None,
        identity_unique=True,
        list_complete=False,
        protections_clear=False,
        target_present_before=True,
        target_present_after=not performed,
        anchor_present_after=False,
        apply_available_before=True,
        apply_completed_after=performed,
    )


class RuntimeStoreTests(unittest.TestCase):
    """直接验证唯一 SqliteStore 的 M6 事务边界。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        from app.adapters.sqlite.profile_store import SqliteProfileStore

        profile = SqliteProfileStore(self.root, "demo", create=True, now=now_utc())
        profile.open()
        profile.close()
        self.path = profile_runtime_path(self.root, "demo")
        self.owner = owner(canonical_hash({"host": "synthetic"}))
        self.store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.owner.host,
            require_runtime=True,
        )
        self.batch = str(uuid.uuid4())

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_checkpoint_failure_rolls_back_cursor_and_event(self) -> None:
        """检查点事件写失败时游标更新与事件一起回滚。"""
        self.store.ensure_batch(self.batch, BudgetLimits(), now_utc())

        def fail(name: str) -> None:
            if name == "checkpoint_before_event":
                raise RuntimeError("synthetic")

        with SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.owner.host,
            require_runtime=True,
            fault_hook=lambda name, connection: fail(name),
        ) as store:
            cursor = BatchCursor.initial(self.batch, BudgetLimits())
            with self.assertRaises(StoreError) as caught:
                store.save_checkpoint(self.batch, cursor, now=now_utc())
            self.assertEqual(caught.exception.reason_code, "store_io_error")
        batch = self.store.load_batch(self.batch)
        assert batch is not None
        self.assertEqual(batch["cursor"], BatchCursor.initial(self.batch, BudgetLimits()))
        connection = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM scheduler_events WHERE event = 'checkpoint_committed'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_apply_job_performed_and_no_replay(self) -> None:
        """apply_job 复用 pending/授权/证据路径，performed/applied 后不重放。"""
        key = job("apply-a")
        self.store.ensure_batch(self.batch, BudgetLimits(), now_utc())
        self.store.enqueue_jobs(self.batch, (key,), now_utc())
        lease = self.store.claim_job(self.batch, self.owner, now_utc(), key)
        assert lease is not None
        lease = self.store.start_job(lease, now_utc())
        action_key = action_key_for("apply_job", app=APP, message_key=key)
        self.store.begin_action(
            lease,
            action_kind="apply_job",
            action_key=action_key,
            target_key=key,
            conversation_key=key,
            authorization_json=json.dumps({"synthetic": True}),
            daily_sends=1,
            reply_cooldown_seconds=0,
            ime_original=None,
            now=now_utc(),
        )
        outcome = derive_action_outcome(apply_observation(action_key))
        self.assertEqual(outcome.evidence.outcome, "performed")
        self.assertEqual(outcome.evidence.effect, "applied")
        record = self.store.complete_action(lease, action_key=action_key, outcome=outcome, now=now_utc())
        self.assertEqual(record["state"], "performed")
        stored = self.store.get_action(action_key)
        assert stored is not None
        self.assertEqual(stored["task_kind"], "job")
        self.assertEqual(stored["effect"], "applied")
        # 任务已 done、设备行已删除；同动作键不能再次 pending。
        self.assertIsNone(self.store.claim_job(self.batch, self.owner, now_utc(), key))
        # typed 引用是数据库约束，不能只靠仓储口头校验。
        connection = sqlite3.connect(str(self.path))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE action_records SET job_task_key = NULL WHERE action_key = ?",
                    (action_key,),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_apply_job_unknown_keeps_device_quarantined(self) -> None:
        """点击后断连记 unknown，任务 blocked，设备隔离且不能自动重放。"""
        key = job("apply-unknown")
        self.store.ensure_batch(self.batch, BudgetLimits(), now_utc())
        self.store.enqueue_jobs(self.batch, (key,), now_utc())
        lease = self.store.claim_job(self.batch, self.owner, now_utc(), key)
        assert lease is not None
        lease = self.store.start_job(lease, now_utc())
        action_key = action_key_for("apply_job", app=APP, message_key=key)
        self.store.begin_action(
            lease,
            action_kind="apply_job",
            action_key=action_key,
            target_key=key,
            conversation_key=key,
            authorization_json=json.dumps({"synthetic": True}),
            daily_sends=1,
            reply_cooldown_seconds=0,
            ime_original=None,
            now=now_utc(),
        )
        outcome = derive_action_outcome(apply_observation(action_key, outcome="unknown"))
        self.assertEqual(outcome.evidence.outcome, "unknown")
        self.store.complete_action(lease, action_key=action_key, outcome=outcome, now=now_utc())
        self.assertEqual(self.store.get_action(action_key)["state"], "unknown")  # type: ignore[index]
        with self.assertRaises(StoreError) as caught:
            self.store.claim_job(self.batch, self.owner, now_utc())
        self.assertEqual(caught.exception.reason_code, "lease_busy")

    def test_v2_to_v3_preserves_action_rows_and_adds_m6_tables(self) -> None:
        """v2 动作行在备份后重建为 discovery 类型并保留，M6 表追加。"""
        path = self.root / "v2-profile.sqlite3"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            for name, (obj_type, sql) in PROFILE_V2_EXPECTED_OBJECTS.items():
                del obj_type
                connection.execute(sql)
            connection.execute(
                "INSERT INTO profile_meta(singleton, profile_id, schema_version, created_at) "
                "VALUES (1, 'demo', 1, ?)",
                (ROOT_NOW,),
            )
            conv = conversation_key_for(APP, "合成联系人", "合成公司", "合成职位")
            connection.execute(
                "INSERT INTO hr_conversations(app, key, identity_hash, baseline_preview_hash, "
                "baseline_time_hint, verified_at) VALUES (?, ?, ?, NULL, NULL, NULL)",
                (APP, conv, conv.rsplit(":", 1)[-1]),
            )
            discovery = "boss:discovery:" + canonical_hash({"d": "v2"})
            connection.execute(
                "INSERT INTO hr_discoveries(app, key, conversation_key, kind, fingerprint, "
                "source_names_json, snapshot_id, opened, state, priority, created_at, "
                "updated_at, attempt) VALUES (?, ?, ?, 'unread', ?, ?, ?, 0, 'queued', 0, ?, ?, 0)",
                (
                    APP,
                    discovery,
                    conv,
                    canonical_hash({"f": 1}),
                    '["row_badge"]',
                    str(uuid.uuid4()),
                    ROOT_NOW,
                    ROOT_NOW,
                ),
            )
            action_key = "boss:" + "boss:msg:" + canonical_hash({"m": 1}) + ":reply"
            connection.execute(
                "INSERT INTO action_records(action_key, app, action_kind, task_key, "
                "target_key, conversation_key, state, authorization_json, lease_owner, "
                "owner_pid, owner_host, nonce, lease_expires_at, created_at, updated_at) "
                "VALUES (?, 'boss', 'send_text', ?, ?, ?, 'pending', '{}', ?, 1, ?, ?, ?, ?, ?)",
                (
                    action_key,
                    discovery,
                    "boss:msg:" + canonical_hash({"m": 1}),
                    conv,
                    str(uuid.uuid4()),
                    canonical_hash({"host": "v2"}),
                    str(uuid.uuid4()),
                    ROOT_NOW,
                    ROOT_NOW,
                    ROOT_NOW,
                ),
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()
        backup = initialize_profile_database(path, "demo", now=now_utc())
        self.assertIsNotNone(backup)
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                PROFILE_SCHEMA_VERSION,
            )
            row = connection.execute(
                "SELECT task_kind, discovery_task_key, job_task_key FROM action_records"
            ).fetchone()
            self.assertEqual(row, ("discovery", discovery, None))
            names = {
                item[0]
                for item in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertTrue({"jobs", "job_batches", "drafts", "runtime_decisions", "scheduler_events"} <= names)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()
        self.assertIn(".v2-backup-", str(backup))


if __name__ == "__main__":
    unittest.main()
