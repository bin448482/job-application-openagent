"""验证 M1 候选入库、任务租约、三态收敛、恢复与事务回滚的完整契约。"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from app.adapters.sqlite import repo as repo_module
from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.schema import initialize_database
from app.domain.models import (
    Evidence,
    Lease,
    Owner,
    ReadResult,
    UnreadRow,
    canonical_hash,
    discovery_key_for,
)
from app.ports.store import StoreError

from tests.m1_helpers import (
    event_count,
    fetch_all,
    fetch_one,
    make_evidence,
    make_result,
    open_store,
    sources_json,
)

OWNER = Owner("owner-1", 4242, "host-a")


class StoreTests(unittest.TestCase):
    """覆盖合成快照入库、竞争、TTL、恢复与失败整体回滚。"""

    def setUp(self) -> None:
        """为每个测试创建独立临时 v1 库与可控时钟。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path, self.store, self.clock = open_store(self._tmp.name)
        self.addCleanup(self.store.close)

    def _seed(self, **kwargs: object) -> tuple[ReadResult, list[str]]:
        """以当前假时钟时间写入一条合成结果并返回结果与 queued 键。"""
        observed = kwargs.pop("observed_at", self.clock.now())
        result = make_result(observed_at=str(observed), **kwargs)
        keys = self.store.upsert_snapshot(result)
        return result, keys

    def _claim_and_start(self, *, owner: Owner = OWNER, kind: str = "unread") -> Lease:
        """领取并开始一条任务，返回 running 租约。"""
        now = self.clock.now()
        lease = self.store.claim("boss", kind, owner, now)
        self.assertIsNotNone(lease)
        return self.store.start(lease, now)

    def test_upsert_persists_conversation_discovery_and_event(self) -> None:
        """首次入库写会话、queued 发现与唯一 snapshot_upserted 事件。"""
        result, keys = self._seed()
        self.assertEqual(len(keys), 1)
        candidate = result.value.candidates[0]
        self.assertEqual(keys[0], candidate.candidate_key)
        conversation = fetch_one(
            self.path, "SELECT * FROM hr_conversations WHERE app = 'boss'"
        )
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation["identity_hash"], result.value.rows[0].identity_hash)
        self.assertIsNone(conversation["baseline_preview_hash"])
        self.assertIsNone(conversation["baseline_time_hint"])
        self.assertIsNone(conversation["verified_at"])
        row = fetch_one(self.path, "SELECT * FROM hr_discoveries")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["kind"], "unread")
        self.assertEqual(row["attempt"], 0)
        self.assertEqual(row["opened"], 0)
        self.assertEqual(row["source_names_json"], sources_json(("row_badge",)))
        self.assertEqual(row["created_at"], self.clock.now())
        self.assertEqual(row["updated_at"], self.clock.now())
        self.assertIsNone(row["nonce"])
        self.assertEqual(row["fingerprint"], candidate.fingerprint)
        self.assertEqual(event_count(self.path), 1)
        event = fetch_one(self.path, "SELECT * FROM events")
        self.assertEqual(event["event"], "snapshot_upserted")
        self.assertIsNone(event["reason_code"])
        self.assertEqual(event["subject_key"], None)
        self.assertEqual(
            event["payload_json"],
            '{"from_state":null,"to_state":"queued","nonce":null,"candidate_count":1}',
        )
        self.assertIsNone(fetch_one(self.path, "SELECT * FROM device_lease"))

    def test_upsert_same_fingerprint_replay_is_silent(self) -> None:
        """同指纹重放即使快照 ID 不同也不改行、不追加事件。"""
        result, first = self._seed()
        before = fetch_one(self.path, "SELECT * FROM hr_discoveries")
        seen = event_count(self.path)
        replay = replace(result, snapshot_id=str(uuid.uuid4()))
        self.assertEqual(self.store.upsert_snapshot(replay), first)
        self.assertEqual(fetch_one(self.path, "SELECT * FROM hr_discoveries"), before)
        self.assertEqual(event_count(self.path), seen)

    def test_upsert_ignores_rows_without_locatable_identity(self) -> None:
        """未知行不入库，但不妨碍同一快照中的明确候选落库。"""
        base = make_result(observed_at=self.clock.now())
        snapshot = base.value
        unknown = UnreadRow(
            conversation_key=None,
            identity_hash=None,
            preview_hash=canonical_hash("未知行预览"),
            time_hint="11:00",
            unread_count=None,
        )
        mixed = replace(snapshot, rows=snapshot.rows + (unknown,))
        result = replace(base, value=mixed)
        self.assertEqual(len(self.store.upsert_snapshot(result)), 1)
        self.assertEqual(len(fetch_all(self.path, "SELECT * FROM hr_conversations")), 1)
        self.assertEqual(len(fetch_all(self.path, "SELECT * FROM hr_discoveries")), 1)

        only_unknown = replace(base, value=replace(snapshot, rows=(unknown,), candidates=()))
        self.assertEqual(self.store.upsert_snapshot(only_unknown), [])
        self.assertEqual(len(fetch_all(self.path, "SELECT * FROM hr_conversations")), 1)
        self.assertEqual(event_count(self.path), 1)

    def test_upsert_rejects_non_ok_or_missing_snapshot(self) -> None:
        """failed/undetermined 或缺失 snapshot_id 的结果一律 invalid_input。"""
        failed = make_result(status="failed", reason_code="device_disconnected")
        undetermined = make_result(status="undetermined", reason_code="unread_unverified")
        ok_without_id = replace(make_result(), snapshot_id=None)
        for result in (failed, undetermined, ok_without_id):
            with self.subTest(status=result.status):
                with self.assertRaises(StoreError) as context:
                    self.store.upsert_snapshot(result)
                self.assertEqual(context.exception.reason_code, "invalid_input")
        self.assertEqual(fetch_all(self.path, "SELECT * FROM hr_discoveries"), [])

    def test_upsert_rejects_tampered_keys_sources_and_time_order(self) -> None:
        """篡改指纹/键、重复候选、非法来源、bool 计数与倒写时间都回滚。"""
        base = make_result(observed_at=self.clock.now())
        snapshot = base.value
        candidate = snapshot.candidates[0]
        row = snapshot.rows[0]
        cases = {
            "fingerprint": replace(base, value=replace(
                snapshot,
                candidates=(replace(candidate, fingerprint=canonical_hash("伪造")),),
            )),
            "candidate-key": replace(base, value=replace(
                snapshot,
                candidates=(replace(
                    candidate,
                    candidate_key=discovery_key_for(
                        "boss", candidate.conversation_key, canonical_hash("伪造")
                    ),
                ),),
            )),
            "missing-conversation": replace(base, value=replace(
                snapshot,
                rows=(replace(row, conversation_key="boss:conv:" + "0" * 64),),
            )),
            "duplicate-candidate": replace(base, value=replace(
                snapshot,
                candidates=(candidate, candidate),
            )),
            "bad-source": replace(base, value=replace(
                snapshot,
                candidates=(replace(candidate, source_names=("boss_only",)),),
            )),
            "bool-count": replace(base, value=replace(
                snapshot,
                rows=(replace(row, unread_count=True),),
            )),
            "bad-app": replace(base, value=replace(snapshot, app="liepin")),
        }
        for name, result in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as context:
                    self.store.upsert_snapshot(result)
                self.assertEqual(context.exception.reason_code, "invalid_input")
        self.assertEqual(fetch_all(self.path, "SELECT * FROM hr_discoveries"), [])

        self._seed(preview="第一版", observed_at=self.clock.now())
        self.clock.advance(-1)
        late_result = make_result(observed_at=self.clock.now(), preview="回写更早")
        with self.assertRaises(StoreError) as context:
            self.store.upsert_snapshot(late_result)
        self.assertEqual(context.exception.reason_code, "invalid_input")
        self.assertEqual(
            fetch_one(self.path, "SELECT state FROM hr_discoveries")["state"], "queued"
        )

    def test_new_fingerprint_supersedes_queued_discovery(self) -> None:
        """同会话新键在旧发现仍 queued 时取消旧行并插入新行。"""
        _, first = self._seed(preview="第一版")
        self.clock.advance(1)
        _, second = self._seed(preview="第二版")
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0], second[0])
        rows = fetch_all(self.path, "SELECT * FROM hr_discoveries ORDER BY state")
        self.assertEqual(len(rows), 2)
        old = fetch_one(
            self.path,
            "SELECT * FROM hr_discoveries WHERE \"key\" = ?",
            (first[0],),
        )
        new = fetch_one(
            self.path,
            "SELECT * FROM hr_discoveries WHERE \"key\" = ?",
            (second[0],),
        )
        self.assertEqual(old["state"], "cancelled")
        self.assertEqual(old["reason_code"], "candidate_superseded")
        self.assertEqual(new["state"], "queued")
        self.assertEqual(event_count(self.path, "candidate_superseded"), 1)
        superseded = fetch_one(
            self.path, "SELECT * FROM events WHERE event = 'candidate_superseded'"
        )
        self.assertEqual(superseded["subject_key"], first[0])
        self.assertEqual(
            superseded["payload_json"],
            '{"from_state":"queued","to_state":"cancelled","nonce":null,"candidate_count":null}',
        )
        self.assertEqual(event_count(self.path, "snapshot_upserted"), 2)

    def test_active_non_queued_blocks_new_key_and_merges_sources(self) -> None:
        """running 发现阻止新键落库，只合并来源且不改负载或租约。"""
        _, keys = self._seed(preview="第一版", sources=("row_badge",))
        self._claim_and_start()
        before = fetch_one(self.path, "SELECT * FROM hr_discoveries")
        self.clock.advance(1)
        result, returned = self._seed(
            preview="第二版", sources=("preview_delta",), contact="合成联系人"
        )
        self.assertEqual(returned, [])
        after = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        self.assertEqual(after["state"], "running")
        self.assertEqual(after["kind"], before["kind"])
        self.assertEqual(after["fingerprint"], before["fingerprint"])
        self.assertEqual(after["snapshot_id"], before["snapshot_id"])
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(
            after["source_names_json"], sources_json(("row_badge", "preview_delta"))
        )
        self.assertEqual(len(fetch_all(self.path, "SELECT * FROM hr_discoveries")), 1)
        self.assertEqual(event_count(self.path, "candidate_deferred"), 1)

    def test_terminal_discovery_key_is_skipped(self) -> None:
        """已有终态的同键重放跳过，不重开、不追加事件。"""
        result, keys = self._seed()
        lease = self._claim_and_start()
        self.store.finish(lease, self.clock.now(), "done", None, result)
        before = event_count(self.path)
        self.assertEqual(self.store.upsert_snapshot(result), [])
        self.assertEqual(event_count(self.path), before)
        row = fetch_one(self.path, "SELECT state FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(row["state"], "done")

    def test_claim_uses_priority_created_and_key_order(self) -> None:
        """claim 只选指定 kind 的 queued，并按优先级及时间键排序。"""
        _, first = self._seed(contact="合成联系人甲")
        self.clock.advance(1)
        _, second = self._seed(contact="合成联系人乙")
        self.assertEqual(self.store.claim("boss", "new_message", OWNER, self.clock.now()), None)
        self.store.close()
        connection = SqliteStore(self.path, local_host="host-a", monotonic=self.clock.monotonic)
        self.addCleanup(connection.close)
        connection._connection.execute(
            "UPDATE hr_discoveries SET priority = 5 WHERE \"key\" = ?", (second[0],)
        )
        connection._connection.commit()
        lease = connection.claim("boss", "unread", OWNER, self.clock.now())
        self.assertEqual(lease.key, second[0])
        self.assertEqual(lease.expires_at, "2026-11-24T00:01:01.000Z")
        device = fetch_one(self.path, "SELECT * FROM device_lease")
        self.assertEqual(device["nonce"], lease.nonce)
        self.assertEqual(device["reservation_state"], "held")
        self.assertEqual(device["lease_expires_at"], lease.expires_at)
        task = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (lease.key,)
        )
        self.assertEqual(task["state"], "leased")
        self.assertEqual(task["attempt"], 1)
        self.assertEqual(event_count(self.path, "task_claimed"), 1)
        started = connection.start(lease, self.clock.now())
        connection.finish(started, self.clock.now(), "done", None, make_result())
        next_lease = connection.claim("boss", "unread", OWNER, self.clock.now())
        self.assertEqual(next_lease.key, first[0])

    def test_claim_competition_and_empty_candidate(self) -> None:
        """第二个 owner 或第二次 claim 都 lease_busy；无候选返回 null 不写事件。"""
        empty_lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.assertIsNone(empty_lease)
        self.assertEqual(event_count(self.path), 0)
        _, keys = self._seed()
        first = self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.assertIsNotNone(first)
        device_before = fetch_one(self.path, "SELECT * FROM device_lease")
        for owner in (OWNER, Owner("owner-2", 5252, "host-a")):
            with self.subTest(owner=owner.id):
                with self.assertRaises(StoreError) as context:
                    self.store.claim("boss", "unread", owner, self.clock.now())
                self.assertEqual(context.exception.reason_code, "lease_busy")
        self.assertEqual(fetch_one(self.path, "SELECT * FROM device_lease"), device_before)
        self.assertEqual(
            fetch_one(self.path, "SELECT state FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))[
                "state"
            ],
            "leased",
        )

    def test_ttl_expiry_alone_does_not_release_device(self) -> None:
        """仅 TTL 到期不能释放设备占用或允许新 claim。"""
        _, keys = self._seed()
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        device_before = fetch_one(self.path, "SELECT * FROM device_lease")
        self.clock.advance(61)
        with self.assertRaises(StoreError) as context:
            self.store.claim("boss", "unread", Owner("owner-2", 9, "host-a"), self.clock.now())
        self.assertEqual(context.exception.reason_code, "lease_busy")
        self.assertEqual(fetch_one(self.path, "SELECT * FROM device_lease"), device_before)
        task = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        self.assertEqual(task["state"], "leased")
        self.assertEqual(task["attempt"], 1)
        self.assertEqual(task["nonce"], lease.nonce)

    def test_queued_new_message_can_upgrade_to_unread(self) -> None:
        """同指纹 queued 发现可把 new_message 升级为更强的 unread。"""
        _, keys = self._seed(kind="new_message", preview="同一预览")
        before = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        upgraded = make_result(
            observed_at=self.clock.now(), kind="unread", preview="同一预览"
        )
        self.assertEqual(self.store.upsert_snapshot(upgraded), keys)
        after = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        self.assertEqual(after["kind"], "unread")
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["fingerprint"], before["fingerprint"])
        self.assertGreaterEqual(event_count(self.path, "snapshot_upserted"), 2)

    def test_claim_rejects_invalid_app_kind_owner_and_time(self) -> None:
        """跨 app、未知 kind、bool/零 PID、异主机与坏时间都 invalid_input。"""
        self._seed()
        cases = (
            ("app", dict(app="liepin", kind="unread", owner=OWNER, now=self.clock.now())),
            ("kind", dict(app="boss", kind="job", owner=OWNER, now=self.clock.now())),
            (
                "host",
                dict(app="boss", kind="unread", owner=Owner("o", 1, "host-b"), now=self.clock.now()),
            ),
            (
                "bool-pid",
                dict(app="boss", kind="unread", owner=Owner("o", True, "host-a"), now=self.clock.now()),
            ),
            (
                "zero-pid",
                dict(app="boss", kind="unread", owner=Owner("o", 0, "host-a"), now=self.clock.now()),
            ),
            ("time", dict(app="boss", kind="unread", owner=OWNER, now="2026-11-24T00:00:00Z")),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as context:
                    self.store.claim(**arguments)
                self.assertEqual(context.exception.reason_code, "invalid_input")
        self.assertIsNone(fetch_one(self.path, "SELECT * FROM device_lease"))

    def test_start_moves_to_running_without_extending_and_rejects_repeat(self) -> None:
        """start 只允许 leased→running，不延长截止时间，重复或旧 nonce 都 stale。"""
        _, keys = self._seed()
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.clock.advance(1)
        started = self.store.start(lease, self.clock.now())
        self.assertEqual(started.expires_at, lease.expires_at)
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(task["state"], "running")
        device = fetch_one(self.path, "SELECT * FROM device_lease")
        self.assertEqual(device["lease_expires_at"], lease.expires_at)
        self.assertEqual(event_count(self.path, "task_started"), 1)
        with self.assertRaises(StoreError) as context:
            self.store.start(lease, self.clock.now())
        self.assertEqual(context.exception.reason_code, "stale_lease")
        with self.assertRaises(StoreError) as context:
            self.store.start(replace(lease, nonce=str(uuid.uuid4())), self.clock.now())
        self.assertEqual(context.exception.reason_code, "stale_lease")

    def test_renew_extends_both_rows_and_old_lease_is_stale(self) -> None:
        """renew 同步延长任务与设备；旧截止时间的 Lease 和过期续租都 stale。"""
        _, keys = self._seed()
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.clock.advance(30)
        renewed = self.store.renew(lease, self.clock.now())
        self.assertEqual(renewed.expires_at, "2026-11-24T00:01:30.000Z")
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        device = fetch_one(self.path, "SELECT * FROM device_lease")
        self.assertEqual(task["lease_expires_at"], renewed.expires_at)
        self.assertEqual(device["lease_expires_at"], renewed.expires_at)
        self.assertEqual(event_count(self.path, "lease_renewed"), 1)
        self.clock.advance(1)
        with self.assertRaises(StoreError) as context:
            self.store.renew(lease, self.clock.now())
        self.assertEqual(context.exception.reason_code, "stale_lease")
        self.clock.advance(100)
        with self.assertRaises(StoreError) as context:
            self.store.renew(renewed, self.clock.now())
        self.assertEqual(context.exception.reason_code, "stale_lease")
        self.assertEqual(
            fetch_one(self.path, "SELECT state FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))[
                "state"
            ],
            "leased",
        )

    def test_finish_done_clears_lease_and_removes_device(self) -> None:
        """ReadResult.ok 的 done 清空任务租约、删除设备行并追加事件。"""
        result, keys = self._seed()
        lease = self._claim_and_start()
        self.assertEqual(
            self.store.finish(lease, self.clock.now(), "done", None, result), "updated"
        )
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(task["state"], "done")
        self.assertIsNone(task["reason_code"])
        for field in ("lease_owner", "nonce", "lease_expires_at", "owner_host", "owner_pid"):
            self.assertIsNone(task[field])
        self.assertEqual(task["attempt"], 1)
        self.assertIsNone(fetch_one(self.path, "SELECT * FROM device_lease"))
        event = fetch_one(self.path, "SELECT * FROM events WHERE event = 'task_finished'")
        self.assertEqual(event["reason_code"], None)
        self.assertEqual(
            event["payload_json"],
            '{"from_state":"running","to_state":"done","nonce":"'
            + lease.nonce
            + '","candidate_count":null}',
        )

    def test_finish_done_with_performed_evidence(self) -> None:
        """Evidence.performed 可把任务收敛为 done，并要求两个不同快照。"""
        _, keys = self._seed()
        lease = self._claim_and_start()
        evidence = make_evidence(outcome="performed", effect="sent")
        self.store.finish(lease, self.clock.now(), "done", None, evidence)
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(task["state"], "done")
        self.assertIsNone(task["reason_code"])
        self.assertIsNone(fetch_one(self.path, "SELECT * FROM device_lease"))

    def test_finish_not_sent_and_failed_use_matching_proof(self) -> None:
        """not_sent 只接受 not_performed，failed 只接受 failed ReadResult。"""
        _, keys = self._seed(contact="合成联系人甲")
        lease = self._claim_and_start()
        evidence = make_evidence(outcome="not_performed", reason_code="precondition_failed")
        self.store.finish(lease, self.clock.now(), "not_sent", "precondition_failed", evidence)
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(task["state"], "not_sent")
        self.assertEqual(task["reason_code"], "precondition_failed")
        self.assertIsNone(fetch_one(self.path, "SELECT * FROM device_lease"))

        self.clock.advance(1)
        _, second = self._seed(contact="合成联系人乙")
        lease = self._claim_and_start()
        failed = make_result(status="failed", reason_code="device_disconnected")
        self.store.finish(lease, self.clock.now(), "failed", "device_disconnected", failed)
        task = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (second[0],)
        )
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["reason_code"], "device_disconnected")
        pending = fetch_one(self.path, "SELECT * FROM device_lease")
        self.assertIsNone(pending)

    def _assert_blocked_device_contract(
        self, result: ReadResult | Evidence, reason_code: str
    ) -> None:
        """逐字段断言 finish→blocked 的任务清租约与设备行原身份隔离。"""
        _, keys = self._seed()
        lease = self._claim_and_start()
        task_before = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        device_before = fetch_one(self.path, "SELECT * FROM device_lease")
        events_before = event_count(self.path)
        self.store.finish(lease, self.clock.now(), "blocked", reason_code, result)
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["reason_code"], reason_code)
        self.assertEqual(task["attempt"], task_before["attempt"])
        self.assertEqual(task["created_at"], task_before["created_at"])
        for field in ("lease_owner", "nonce", "lease_expires_at", "owner_host", "owner_pid"):
            self.assertIsNone(task[field])
        device = fetch_one(self.path, "SELECT * FROM device_lease")
        for field in (
            "device_key",
            "lease_owner",
            "owner_pid",
            "owner_host",
            "nonce",
            "lease_expires_at",
        ):
            self.assertEqual(device[field], device_before[field])
        self.assertEqual(device["reservation_state"], "quarantined")
        self.assertEqual(event_count(self.path), events_before + 1)
        finished = fetch_one(
            self.path, "SELECT * FROM events ORDER BY event_id DESC LIMIT 1"
        )
        self.assertEqual(finished["event"], "task_finished")
        self.assertEqual(finished["reason_code"], reason_code)
        with self.assertRaises(StoreError) as context:
            self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.assertEqual(context.exception.reason_code, "lease_busy")
        self.assertEqual(fetch_one(self.path, "SELECT * FROM device_lease"), device)

    def test_finish_blocked_by_undetermined_read_quarantines_device(self) -> None:
        """ReadResult.undetermined 使任务 blocked 且设备保持原身份隔离。"""
        self._assert_blocked_device_contract(
            make_result(status="undetermined", reason_code="unread_unverified"),
            "unread_unverified",
        )

    def test_finish_blocked_by_unknown_evidence_quarantines_device(self) -> None:
        """Evidence.unknown 使任务 blocked 且设备保持原身份隔离。"""
        self._assert_blocked_device_contract(
            make_evidence(outcome="unknown", reason_code="result_unverified"),
            "result_unverified",
        )

    def test_finish_rejects_invalid_proof_and_reason_combinations(self) -> None:
        """状态、原因与 proof 三态任一不匹配都 invalid_input 且保持 running。"""
        result, keys = self._seed()
        lease = self._claim_and_start()
        same_id = str(uuid.uuid4())
        cases = (
            ("done-failed", "done", None, make_result(status="failed", reason_code="device_disconnected")),
            ("done-unknown", "done", None, make_evidence(outcome="unknown")),
            ("not-sent-ok", "not_sent", "precondition_failed", result),
            ("failed-unknown", "failed", "device_disconnected", make_evidence(outcome="unknown")),
            ("blocked-failed", "blocked", "unread_unverified", make_result(status="failed")),
            ("done-reason", "done", "unread_unverified", result),
            ("bad-state", "cancelled", "candidate_superseded", result),
            (
                "bad-evidence",
                "done",
                None,
                make_evidence(
                    outcome="performed",
                    before_snapshot_id=same_id,
                    after_snapshot_id=same_id,
                ),
            ),
            (
                "reason-mismatch",
                "blocked",
                "identity_ambiguous",
                make_result(status="undetermined", reason_code="unread_unverified"),
            ),
        )
        for name, state, reason, proof in cases:
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as context:
                    self.store.finish(lease, self.clock.now(), state, reason, proof)
                self.assertEqual(context.exception.reason_code, "invalid_input")
        task = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["nonce"], lease.nonce)
        self.assertEqual(self.store.finish(lease, self.clock.now(), "done", None, result), "updated")

    def test_finish_rejects_stale_lease_deadline_and_repeat(self) -> None:
        """旧 nonce、错误 owner、过期截止时间与重复 finish 都 stale 且不改行。"""
        _, keys = self._seed()
        lease = self._claim_and_start()
        for name, altered in (
            ("nonce", replace(lease, nonce=str(uuid.uuid4()))),
            ("pid", replace(lease, owner=Owner("owner-1", 9999, "host-a"))),
            ("host", replace(lease, owner=Owner("owner-1", 4242, "host-b"))),
            ("expiry", replace(lease, expires_at="2026-11-24T00:00:01.000Z")),
        ):
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as context:
                    self.store.finish(altered, self.clock.now(), "done", None, make_result())
                self.assertIn(context.exception.reason_code, {"stale_lease", "invalid_input"})
                task = fetch_one(
                    self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
                )
                self.assertEqual(task["state"], "running")
                self.assertEqual(task["nonce"], lease.nonce)

        self.clock.advance(61)
        with self.assertRaises(StoreError) as context:
            self.store.finish(lease, self.clock.now(), "done", None, make_result())
        self.assertEqual(context.exception.reason_code, "stale_lease")

        self.clock.advance(-61)
        self.store.finish(lease, self.clock.now(), "done", None, make_result())
        with self.assertRaises(StoreError) as context:
            self.store.finish(lease, self.clock.now(), "done", None, make_result())
        self.assertEqual(context.exception.reason_code, "stale_lease")

    def test_recover_leased_requires_expiry_confirmation_host_and_stop(self) -> None:
        """leased 恢复逐项拒绝未过期、未确认、停止未知/活动与异主机。"""
        _, keys = self._seed()
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        with self.assertRaises(StoreError) as context:
            self.store.recover(lease, True, self.clock.now())
        self.assertEqual(context.exception.reason_code, "lease_busy")
        self.clock.advance(60)
        with self.assertRaises(StoreError) as context:
            self.store.recover(lease, False, self.clock.now())
        self.assertEqual(context.exception.reason_code, "owner_not_stopped")
        with self.assertRaises(StoreError) as context:
            self.store.recover(lease, 1, self.clock.now())
        self.assertEqual(context.exception.reason_code, "invalid_input")
        self.store.close()

        for stop_state in ("unknown", "alive"):
            with self.subTest(stop=stop_state):
                self.path, self.store, self.clock = open_store(
                    self._tmp.name,
                    clock=self.clock,
                    stop_checker=lambda pid, state=stop_state: state,
                )
                with self.assertRaises(StoreError) as context:
                    self.store.recover(lease, True, self.clock.now())
                self.assertEqual(context.exception.reason_code, "owner_not_stopped")
                self.store.close()

        self.path, self.store, self.clock = open_store(
            self._tmp.name, clock=self.clock, local_host="host-b"
        )
        with self.assertRaises(StoreError) as context:
            self.store.recover(lease, True, self.clock.now())
        self.assertEqual(context.exception.reason_code, "owner_not_stopped")
        self.store.close()

        self.path, self.store, self.clock = open_store(self._tmp.name, clock=self.clock)
        self.addCleanup(self.store.close)
        with self.assertRaises(StoreError) as context:
            self.store.recover(
                replace(lease, nonce=str(uuid.uuid4())), True, self.clock.now()
            )
        self.assertEqual(context.exception.reason_code, "stale_lease")
        self.assertIsNotNone(fetch_one(self.path, "SELECT * FROM device_lease"))
        self.assertEqual(
            fetch_one(self.path, "SELECT state FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))[
                "state"
            ],
            "leased",
        )

        self.assertEqual(
            self.store.recover(lease, True, self.clock.now()),
            {"status": "recovered", "task_state": "queued"},
        )
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(task["state"], "queued")
        self.assertIsNone(task["reason_code"])
        for field in ("lease_owner", "nonce", "lease_expires_at", "owner_host", "owner_pid"):
            self.assertIsNone(task[field])
        self.assertIsNone(fetch_one(self.path, "SELECT * FROM device_lease"))
        event = fetch_one(self.path, "SELECT * FROM events WHERE event = 'lease_recovered'")
        self.assertEqual(
            event["payload_json"],
            '{"from_state":"leased","to_state":"queued","nonce":"'
            + lease.nonce
            + '","candidate_count":null}',
        )
        with self.assertRaises(StoreError) as context:
            self.store.recover(lease, True, self.clock.now())
        self.assertEqual(context.exception.reason_code, "stale_lease")

    def test_recover_running_blocks_and_quarantines_device(self) -> None:
        """running 过期恢复为 blocked/recovery_requires_reconciliation 并隔离设备。"""
        _, keys = self._seed()
        lease = self._claim_and_start()
        device_before = fetch_one(self.path, "SELECT * FROM device_lease")
        self.clock.advance(60)
        self.assertEqual(
            self.store.recover(lease, True, self.clock.now()),
            {"status": "recovered", "task_state": "blocked"},
        )
        task = fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],))
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["reason_code"], "recovery_requires_reconciliation")
        for field in ("lease_owner", "nonce", "lease_expires_at", "owner_host", "owner_pid"):
            self.assertIsNone(task[field])
        device = fetch_one(self.path, "SELECT * FROM device_lease")
        for field in ("lease_owner", "owner_pid", "owner_host", "nonce", "lease_expires_at"):
            self.assertEqual(device[field], device_before[field])
        self.assertEqual(device["reservation_state"], "quarantined")
        with self.assertRaises(StoreError) as context:
            self.store.claim("boss", "unread", OWNER, self.clock.now())
        self.assertEqual(context.exception.reason_code, "lease_busy")
        with self.assertRaises(StoreError) as context:
            self.store.recover(lease, True, self.clock.now())
        self.assertEqual(context.exception.reason_code, "stale_lease")

    def test_clock_changed_rejects_renew_without_changing_rows(self) -> None:
        """墙钟与单调时钟增量相差超过 1 秒时拒绝新动作且不变更。"""
        _, keys = self._seed()
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        before_task = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        before_device = fetch_one(self.path, "SELECT * FROM device_lease")
        before_events = event_count(self.path)
        self.clock.advance(10, mono_seconds=1)
        with self.assertRaises(StoreError) as context:
            self.store.renew(lease, self.clock.now())
        self.assertEqual(context.exception.reason_code, "clock_changed")
        self.assertEqual(
            fetch_one(self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)),
            before_task,
        )
        self.assertEqual(fetch_one(self.path, "SELECT * FROM device_lease"), before_device)
        self.assertEqual(event_count(self.path), before_events)

    def _reopen_with_hook(self, hook: object) -> None:
        """关闭基础连接并以同一时钟和故障注入重开仓储。"""
        self.store.close()
        self.path, self.store, self.clock = open_store(
            self._tmp.name, clock=self.clock, fault_hook=hook
        )
        self.addCleanup(self.store.close)

    def _running_fixture(self) -> tuple[object, dict[str, object], dict[str, object]]:
        """创建 running 任务并返回租约、任务行与设备行快照。"""
        _, keys = self._seed()
        lease = self._claim_and_start()
        task = fetch_one(
            self.path, "SELECT * FROM hr_discoveries WHERE \"key\" = ?", (keys[0],)
        )
        device = fetch_one(self.path, "SELECT * FROM device_lease")
        return lease, task, device

    def test_finish_rolls_back_when_task_update_misses(self) -> None:
        """任务条件更新未命中时任务、设备与事件全部回滚。"""

        def hook(name: str, connection: object) -> None:
            if name == "finish_before_task_update":
                connection.execute(
                    "UPDATE hr_discoveries SET nonce = ? WHERE app = 'boss' AND \"key\" = ?",
                    (str(uuid.uuid4()), current_lease.key),
                )

        base = make_result()
        lease, task_before, device_before = self._running_fixture()
        current_lease = lease
        events_before = event_count(self.path)
        self._reopen_with_hook(hook)
        with self.assertRaises(StoreError) as context:
            self.store.finish(
                current_lease,
                self.clock.now(),
                "blocked",
                "unread_unverified",
                make_result(status="undetermined", reason_code="unread_unverified"),
            )
        self.assertEqual(context.exception.reason_code, "stale_lease")
        self.assertEqual(fetch_one(self.path, "SELECT * FROM hr_discoveries"), task_before)
        self.assertEqual(fetch_one(self.path, "SELECT * FROM device_lease"), device_before)
        self.assertEqual(event_count(self.path), events_before)

    def test_finish_rolls_back_when_device_update_misses(self) -> None:
        """设备条件更新未命中时已改任务与事件也全部回滚。"""

        def hook(name: str, connection: object) -> None:
            if name == "finish_before_device_update":
                connection.execute(
                    "UPDATE device_lease SET nonce = ? WHERE device_key = 'primary'",
                    (str(uuid.uuid4()),),
                )

        lease, task_before, device_before = self._running_fixture()
        events_before = event_count(self.path)
        self._reopen_with_hook(hook)
        with self.assertRaises(StoreError) as context:
            self.store.finish(
                lease,
                self.clock.now(),
                "blocked",
                "result_unverified",
                make_evidence(outcome="unknown", reason_code="result_unverified"),
            )
        self.assertEqual(context.exception.reason_code, "stale_lease")
        self.assertEqual(fetch_one(self.path, "SELECT * FROM hr_discoveries"), task_before)
        self.assertEqual(fetch_one(self.path, "SELECT * FROM device_lease"), device_before)
        self.assertEqual(event_count(self.path), events_before)

    def test_finish_rolls_back_when_event_insert_fails(self) -> None:
        """事件写入失败时任务与设备两表变更全部回滚。"""

        def hook(name: str, connection: object) -> None:
            if name == "finish_before_event":
                raise RuntimeError("injected event failure")

        lease, task_before, device_before = self._running_fixture()
        events_before = event_count(self.path)
        self._reopen_with_hook(hook)
        with self.assertRaises(StoreError) as context:
            self.store.finish(
                lease,
                self.clock.now(),
                "blocked",
                "unread_unverified",
                make_result(status="undetermined", reason_code="unread_unverified"),
            )
        self.assertEqual(context.exception.reason_code, "store_io_error")
        self.assertEqual(fetch_one(self.path, "SELECT * FROM hr_discoveries"), task_before)
        self.assertEqual(fetch_one(self.path, "SELECT * FROM device_lease"), device_before)
        self.assertEqual(event_count(self.path), events_before)

    def test_unresolved_conversations_returns_only_active_sorted(self) -> None:
        """只读查询返回 queued/leased/running/blocked 的去重会话键并排序。"""
        done_result, _ = self._seed(contact="合成联系人甲")
        done_lease = self._claim_and_start()
        self.store.finish(done_lease, self.clock.now(), "done", None, done_result)
        self.clock.advance(1)
        running_result, _ = self._seed(contact="合成联系人乙")
        self._claim_and_start()
        self.clock.advance(1)
        queued_result, _ = self._seed(contact="合成联系人丙")
        done_conversation = done_result.value.rows[0].conversation_key
        running_conversation = running_result.value.rows[0].conversation_key
        queued_conversation = queued_result.value.rows[0].conversation_key
        self.assertNotEqual(
            done_conversation,
            running_conversation,
        )
        self.assertEqual(
            self.store.unresolved_conversations("boss"),
            sorted([running_conversation, queued_conversation]),
        )
        self.assertNotIn(done_conversation, self.store.unresolved_conversations("boss"))
        with self.assertRaises(StoreError) as context:
            self.store.unresolved_conversations("liepin")
        self.assertEqual(context.exception.reason_code, "invalid_input")

    def test_cross_app_and_bool_snapshot_values_are_rejected(self) -> None:
        """M1 不放宽 app=boss，也不让 bool 冒充整数计数。"""
        valid = make_result(observed_at=self.clock.now())
        child = replace(valid, value=replace(valid.value, app="liepin"))
        with self.assertRaises(StoreError) as context:
            self.store.upsert_snapshot(child)
        self.assertEqual(context.exception.reason_code, "invalid_input")
        row = replace(valid.value.rows[0], unread_count=True)
        bool_count = replace(valid, value=replace(valid.value, rows=(row,)))
        with self.assertRaises(StoreError) as context:
            self.store.upsert_snapshot(bool_count)
        self.assertEqual(context.exception.reason_code, "invalid_input")
        self.assertEqual(fetch_all(self.path, "SELECT * FROM hr_discoveries"), [])


@unittest.skipUnless(repo_module.os.name == "nt", "Windows-only ctypes contract")
class WindowsStopCheckerTests(unittest.TestCase):
    """用 mock 的 ctypes 句柄验证 Windows 停止判定，不检查真实 PID。"""

    class _Kernel32:
        """记录调用并返回预设句柄与等待结果。"""

        def __init__(self, handle: int, wait: int) -> None:
            """保存 OpenProcess 句柄值与 WaitForSingleObject 结果。"""
            self.handle = handle
            self.wait = wait
            self.closed = False

        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            """返回预设句柄，不访问真实进程。"""
            return self.handle

        def WaitForSingleObject(self, handle: int, timeout: int) -> int:
            """返回预设等待码。"""
            return self.wait

        def CloseHandle(self, handle: int) -> None:
            """记录句柄已关闭。"""
            self.closed = True

    def test_windows_state_mapping(self) -> None:
        """无效 PID、访问拒绝、已退出、仍活动与未知等待码分别映射。"""
        cases = (
            ("missing-pid", 0, 87, 0, "stopped"),
            ("access-denied", 0, 5, 0, "unknown"),
            ("exited", 1, 0, 0, "stopped"),
            ("alive", 1, 0, 258, "alive"),
            ("unknown-wait", 1, 0, 1, "unknown"),
        )
        for name, handle, error, wait, expected in cases:
            with self.subTest(name=name):
                kernel = self._Kernel32(handle, wait)
                with patch.object(
                    repo_module.ctypes, "WinDLL", return_value=kernel
                ), patch.object(
                    repo_module.ctypes, "get_last_error", return_value=error
                ):
                    self.assertEqual(repo_module._windows_process_state(1234), expected)
                if handle:
                    self.assertTrue(kernel.closed)
