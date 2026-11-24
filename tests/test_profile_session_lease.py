"""验证合法 profile 库接入唯一 SqliteStore 与真实任务/共享门禁租约适配。"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import (
    SqliteProfileStore,
    profile_runtime_path,
)
from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.session import ProfileSessionLease
from app.domain.models import Owner
from app.domain.policy import build_envelope, render_request
from app.ports.profile import ProfileError
from app.ports.store import StoreError
from app.usecases import policy as policy_usecases
from app.usecases.decision import DecisionRunError, FixedClock, decide
from app.usecases.policy_eval import RepositorySnapshot
from tests import m1_helpers, policy_helpers
from tests.test_m4c_closed_loop import _PreviewBridge

NOW = m1_helpers.BASE_TIME
LATER = "2026-11-24T00:10:00.000Z"
OWNER_ID = m1_helpers.OWNER_ID
OWNER_ID_2 = m1_helpers.OWNER_ID_2


class ProfileSessionLeaseTests(unittest.TestCase):
    """覆盖真实 profile 领取/续租/失效与 decide 桥前后拒绝。"""

    def setUp(self) -> None:
        """创建临时项目、合法 profile 库与已领取/启动的真实任务。"""
        self.tmp, self.root = policy_helpers.temp_project()
        self.repo = policy_helpers.repository(self.root)
        code, payload, stderr = policy_helpers.run_cli(
            self.root, ["policy", "init", "--dest", "prompts/local/demo"]
        )
        self.assertEqual((code, stderr), (0, ""), payload)
        profile_store = SqliteProfileStore(self.root, "demo", now=NOW)
        profile_store.open()
        profile_store.close()
        self.path = profile_runtime_path(self.root, "demo")
        self.clock = m1_helpers.FakeClock()
        self.owner = Owner(id=OWNER_ID, pid=4242, host=m1_helpers.HOST_A)
        self.store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=m1_helpers.HOST_A,
            monotonic=self.clock.monotonic,
            stop_checker=lambda pid: "stopped",
        )
        self.store.upsert_snapshot(
            m1_helpers.make_result(
                profile_id="demo",
                snapshot_id="00000000-0000-4000-8000-000000000b01",
            )
        )
        claimed = self.store.claim("boss", "unread", self.owner, NOW)
        assert claimed is not None
        self.lease = self.store.start(claimed, NOW)
        self.gate = SharedDeviceGate(
            profile_id="demo",
            profile_db_path=self.path,
            os_root=Path(self.root) / "appdata",
        )
        self.gate.acquire(
            profile_id="demo",
            profile_db_path=str(self.path),
            owner=self.owner,
            now=NOW,
        )

    def tearDown(self) -> None:
        """关闭仓储并清理本次临时项目根。"""
        self.store.close()
        self.tmp.cleanup()

    def _decide(
        self,
        lease: ProfileSessionLease,
        bridge: object,
        now: str,
        clock: object | None = None,
    ):
        """用当前临时包构造一次真实 decide，可注入同一真实租约时钟。"""
        _rel, base_files, _files, _guides, pack = policy_usecases.load_active_pack(
            self.repo, "prompts/local/demo"
        )
        case = policy_helpers.real_preview_case("policy-greeting")
        envelope = build_envelope(
            task_id=str(case["task_id"]),
            nonce=str(case["nonce"]),
            kind=str(case["kind"]),
            subject=case["subject"],
            inbound=str(case["inbound"]),
            context=list(case["context"]),
            allowed_facts=list(case["allowed_facts"]),
            base_files=base_files,
            pack=pack,
            now=now,
        )
        rendered = render_request(base_files, pack, envelope, now=now)
        return decide(
            rendered=rendered,
            envelope=envelope,
            base_files=base_files,
            pack=pack,
            bridge=bridge,
            snapshot=RepositorySnapshot(self.repo, "prompts/local/demo"),
            lease=lease,
            clock=clock if clock is not None else FixedClock(now),
        )

    def test_real_profile_lease_allows_decision_then_expires_and_detaches(self) -> None:
        """真实租约允许一次决策；过期或共享门禁失配后桥前拒绝且不重发。"""
        lease = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        self.assertEqual(lease.current_expiry(), self.lease.expires_at)
        bridge = _PreviewBridge()
        result = self._decide(lease, bridge, NOW)
        self.assertEqual(result["resolved_tier"], "C")
        self.assertEqual(bridge.calls, 1)

        expired = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock(LATER),
        )
        self.assertIsNone(expired.current_expiry())
        with self.assertRaises(DecisionRunError) as caught:
            self._decide(expired, bridge, LATER)
        self.assertEqual(caught.exception.reason_code, "stale_decision")
        self.assertEqual(bridge.calls, 1)

        self.gate.release(now=LATER, converged=True)
        detached = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        self.assertIsNone(detached.current_expiry())
        with self.assertRaises(DecisionRunError):
            self._decide(detached, bridge, NOW)
        self.assertEqual(bridge.calls, 1)

    def _mutate_task_owner_pair(self) -> None:
        """把任务与设备两行 owner 同步改成另一合法且彼此相等的 owner。"""
        connection = sqlite3.connect(str(self.path))
        try:
            for table in ("hr_discoveries", "device_lease"):
                connection.execute(
                    f"UPDATE {table} SET lease_owner = ?, owner_pid = ?, "
                    "owner_host = ? WHERE nonce = ?",
                    (
                        OWNER_ID_2,
                        5151,
                        m1_helpers.HOST_B,
                        self.lease.nonce,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def test_cross_gate_profile_library_and_owner_are_rejected_before_bridge(self) -> None:
        """门禁 profile/原库/owner 与取得时任务不一致时桥前零调用拒绝。"""
        self.gate.release(now=NOW, converged=True)
        other_path = profile_runtime_path(self.root, "other")
        other_gate = SharedDeviceGate(
            profile_id="other",
            profile_db_path=other_path,
            os_root=Path(self.root) / "appdata",
        )
        other_gate.acquire(
            profile_id="other",
            profile_db_path=str(other_path),
            owner=self.owner,
            now=NOW,
        )
        bridge = _PreviewBridge()
        port = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=other_gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        self.assertIsNone(port.current_expiry())
        with self.assertRaises(DecisionRunError):
            self._decide(port, bridge, NOW)
        self.assertEqual(bridge.calls, 0)
        other_gate.release(now=NOW, converged=True)

        clone_path = (self.root / "other-clone" / "runtime.sqlite3").resolve()
        clone_gate = SharedDeviceGate(
            profile_id="demo",
            profile_db_path=clone_path,
            os_root=Path(self.root) / "appdata",
        )
        clone_gate.acquire(
            profile_id="demo",
            profile_db_path=str(clone_path),
            owner=self.owner,
            now=NOW,
        )
        port = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=clone_gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        self.assertIsNone(port.current_expiry())
        with self.assertRaises(DecisionRunError):
            self._decide(port, bridge, NOW)
        self.assertEqual(bridge.calls, 0)
        clone_gate.release(now=NOW, converged=True)

        owner_gate = SharedDeviceGate(
            profile_id="demo",
            profile_db_path=self.path,
            os_root=Path(self.root) / "appdata",
        )
        owner_gate.acquire(
            profile_id="demo",
            profile_db_path=str(self.path),
            owner=Owner(id=OWNER_ID_2, pid=5151, host=m1_helpers.HOST_B),
            now=NOW,
        )
        port = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=owner_gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        self.assertIsNone(port.current_expiry())
        with self.assertRaises(DecisionRunError):
            self._decide(port, bridge, NOW)
        self.assertEqual(bridge.calls, 0)
        owner_gate.release(now=NOW, converged=True)

    def test_copied_store_with_copied_path_still_requires_original_gate(self) -> None:
        """复制 profile 库且适配器参数也指向副本时，原门禁仍拒绝。"""
        copy_path = self.root / "copied-profile.sqlite3"
        shutil.copy2(self.path, copy_path)
        copy_store = SqliteStore(
            copy_path,
            profile_id="demo",
            local_host=m1_helpers.HOST_A,
            monotonic=self.clock.monotonic,
            stop_checker=lambda pid: "stopped",
        )
        try:
            port = ProfileSessionLease(
                copy_store,
                self.lease,
                profile_id="demo",
                gate=self.gate,
                profile_db_path=copy_path,
                clock=FixedClock(NOW),
            )
            bridge = _PreviewBridge()
            self.assertIsNone(port.current_expiry())
            with self.assertRaises(DecisionRunError):
                self._decide(port, bridge, NOW)
            self.assertEqual(bridge.calls, 0)
        finally:
            copy_store.close()

    def test_task_owner_pair_change_is_rejected_even_if_rows_match(self) -> None:
        """任务/设备 owner 同步改成另一合法值时，仍不等于取得时 Lease。"""
        self._mutate_task_owner_pair()
        port = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        bridge = _PreviewBridge()
        self.assertIsNone(port.current_expiry())
        with self.assertRaises(DecisionRunError):
            self._decide(port, bridge, NOW)
        self.assertEqual(bridge.calls, 0)

    def test_bridge_period_owner_change_is_rejected_after_submission(self) -> None:
        """桥调用期间同步更换两行 owner 后，桥后拒绝且不重发。"""

        class _MutatingBridge(_PreviewBridge):
            """在返回决策前同步改两行 owner。"""

            def __init__(self, mutate: object) -> None:
                """保存变异回调。"""
                super().__init__()
                self.mutate = mutate

            def complete(self, **kwargs: object) -> str:
                """先产生决策，再修改任务/设备 owner。"""
                text = super().complete(**kwargs)
                self.mutate()
                return text

        port = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        bridge = _MutatingBridge(self._mutate_task_owner_pair)
        with self.assertRaises(DecisionRunError) as caught:
            self._decide(port, bridge, NOW)
        self.assertEqual(caught.exception.reason_code, "stale_decision")
        self.assertTrue(caught.exception.submitted)
        self.assertEqual(bridge.calls, 1)

    def test_bridge_crossing_expiry_is_rejected_after_submission(self) -> None:
        """桥调用期间真实租约跨截止时桥后拒绝，不重发。"""

        class _MutableClock:
            """可推进的可信时钟桩。"""

            def __init__(self, value: str) -> None:
                """保存当前 UTC 值。"""
                self.value = value

            def now(self) -> str:
                """返回当前值。"""
                return self.value

            def set(self, value: str) -> None:
                """推进到指定值。"""
                self.value = value

        class _AdvancingBridge(_PreviewBridge):
            """在返回决策前推进时钟，模拟桥期间租约到期。"""

            def __init__(self, clock: _MutableClock) -> None:
                """保存可推进时钟。"""
                super().__init__()
                self.clock = clock

            def complete(self, **kwargs: object) -> str:
                """先产生合法决策，再推进时钟。"""
                text = super().complete(**kwargs)
                self.clock.set(LATER)
                return text

        clock = _MutableClock(NOW)
        lease = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=clock,
        )
        bridge = _AdvancingBridge(clock)
        with self.assertRaises(DecisionRunError) as caught:
            self._decide(lease, bridge, NOW, clock)
        self.assertEqual(caught.exception.reason_code, "stale_decision")
        self.assertTrue(caught.exception.submitted)
        self.assertEqual(bridge.calls, 1)

    def test_renewed_lease_is_read_from_real_profile_store(self) -> None:
        """真实续租后适配器读取新截止时间，超过新截止即拒绝。"""
        self.clock.advance(30)
        renewed = self.store.renew(self.lease, self.clock.now())
        lease = ProfileSessionLease(
            self.store,
            renewed,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock("2026-11-24T00:01:10.000Z"),
        )
        self.assertEqual(lease.current_expiry(), renewed.expires_at)
        after = ProfileSessionLease(
            self.store,
            renewed,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock("2026-11-24T00:01:31.000Z"),
        )
        self.assertIsNone(after.current_expiry())

    def test_profile_store_rejects_other_profile_snapshot_before_write(self) -> None:
        """profile=demo 的仓储拒绝 profile=other 的合法快照且表/事件不变。"""
        connection = sqlite3.connect(str(self.path))
        try:
            before_events = int(
                connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            )
            before_discoveries = int(
                connection.execute(
                    "SELECT COUNT(*) FROM hr_discoveries"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        with self.assertRaises(StoreError) as caught:
            self.store.upsert_snapshot(
                m1_helpers.make_result(
                    profile_id="other",
                    contact="另一合成联系人",
                    snapshot_id="00000000-0000-4000-8000-000000000c02",
                )
            )
        self.assertEqual(caught.exception.reason_code, "invalid_input")
        connection = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(
                int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
                before_events,
            )
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM hr_discoveries"
                    ).fetchone()[0]
                ),
                before_discoveries,
            )
        finally:
            connection.close()

    def test_profile_store_rejects_other_profile_and_tampered_task_owner(self) -> None:
        """合法 profile 库不能以另一 ID 打开；任务 owner 篡改后租约拒绝。"""
        with self.assertRaises(ProfileError) as caught:
            SqliteStore(
                self.path,
                profile_id="other",
                local_host=m1_helpers.HOST_A,
                monotonic=self.clock.monotonic,
                stop_checker=lambda pid: "stopped",
            )
        self.assertEqual(caught.exception.reason_code, "profile_mismatch")
        lease = ProfileSessionLease(
            self.store,
            self.lease,
            profile_id="demo",
            gate=self.gate,
            profile_db_path=self.path,
            clock=FixedClock(NOW),
        )
        connection = sqlite3.connect(str(self.path))
        try:
            connection.execute(
                "UPDATE hr_discoveries SET lease_owner = 'tampered' WHERE nonce = ?",
                (self.lease.nonce,),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(lease.current_expiry())

        copy_path = self.root / "copied-profile.sqlite3"
        shutil.copy2(self.path, copy_path)
        copy_store = SqliteStore(
            copy_path,
            profile_id="demo",
            local_host=m1_helpers.HOST_A,
            monotonic=self.clock.monotonic,
            stop_checker=lambda pid: "stopped",
        )
        try:
            copied = ProfileSessionLease(
                copy_store,
                self.lease,
                profile_id="demo",
                gate=self.gate,
                profile_db_path=self.path,
                clock=FixedClock(NOW),
            )
            self.assertIsNone(copied.current_expiry())
        finally:
            copy_store.close()


if __name__ == "__main__":
    unittest.main()
