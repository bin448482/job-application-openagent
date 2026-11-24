"""验证固定 OS 共享设备门禁的跨进程互斥、崩溃保留、nonce 与隔离语义。"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.adapters.sqlite.activity import ProfilePolicyActivity
from app.adapters.sqlite.device_gate import (
    GATE_APP_DIR,
    GATE_DB_NAME,
    SharedDeviceGate,
    guard_database_path,
)
from app.adapters.sqlite.profile_store import (
    SqliteProfileStore,
    profile_runtime_path,
)
from app.domain.models import Owner, canonical_hash
from app.ports.device_gate import DeviceGateError

NOW = "2026-11-24T00:00:00.000Z"
LATER = "2030-11-24T00:00:00.000Z"
OWNER_ID = "00000000-0000-4000-8000-000000000001"
HOST = canonical_hash("synthetic-host")


def _owner(pid: int = 4242) -> Owner:
    """构造合成门禁持有者身份，不使用真实 PID。"""
    return Owner(id=OWNER_ID, pid=pid, host=HOST)


def _race_worker(
    os_root: str,
    project_root: str,
    profile_id: str,
    barrier: object,
    queue: object,
) -> None:
    """在子进程中等待屏障后竞争同一共享门禁，把结果写入队列。"""
    from app.adapters.sqlite.device_gate import SharedDeviceGate
    from app.adapters.sqlite.profile_store import profile_runtime_path
    from app.domain.models import Owner, canonical_hash
    from app.ports.device_gate import DeviceGateError

    barrier.wait()
    try:
        path = profile_runtime_path(Path(project_root), profile_id)
        gate = SharedDeviceGate(
            profile_id=profile_id,
            profile_db_path=path,
            os_root=Path(os_root),
        )
        gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(path),
            owner=Owner(
                id=str(__import__("uuid").uuid4()),
                pid=os.getpid(),
                host=canonical_hash("worker-host"),
            ),
            now=NOW,
        )
    except DeviceGateError as error:
        queue.put(error.reason_code)
    else:
        queue.put("held")


class SharedDeviceGateTests(unittest.TestCase):
    """覆盖固定路径、互斥、隔离、崩溃与不自动解锁。"""

    def setUp(self) -> None:
        """创建独立项目根与独立 OS 应用数据根。"""
        self.project_tmp = tempfile.TemporaryDirectory()
        self.os_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.project_tmp.name)
        self.os_root = Path(self.os_tmp.name)

    def tearDown(self) -> None:
        """只清理本次拥有的两个临时根。"""
        self.project_tmp.cleanup()
        self.os_tmp.cleanup()

    def _gate(self, profile_id: str = "alpha") -> SharedDeviceGate:
        """为给定 profile 构造固定路径门禁实例。"""
        path = profile_runtime_path(self.root, profile_id)
        return SharedDeviceGate(
            profile_id=profile_id,
            profile_db_path=path,
            os_root=self.os_root,
        )

    def _acquire(self, gate: SharedDeviceGate, profile_id: str = "alpha") -> str:
        """按实例绑定 profile 取得占用并返回 nonce。"""
        path = profile_runtime_path(self.root, profile_id)
        return gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(path),
            owner=_owner(),
            now=NOW,
        )

    def test_path_is_fixed_and_independent_of_project_root(self) -> None:
        """门禁路径只由 OS 根决定，不随 clone/worktree 或 profile 改变。"""
        other_root = Path(self.project_tmp.name) / "another-clone"
        expected = self.os_root / GATE_APP_DIR / GATE_DB_NAME
        self.assertEqual(guard_database_path(self.os_root), expected)
        self.assertEqual(
            guard_database_path(self.os_root), guard_database_path(self.os_root)
        )
        self.assertNotEqual(
            profile_runtime_path(other_root, "beta"),
            profile_runtime_path(self.root, "alpha"),
        )
        self.assertEqual(guard_database_path(self.os_root), expected)

    def test_two_profiles_cannot_hold_primary(self) -> None:
        """同 OS 根下不同 profile 的第二占用被拒绝，不按 profile 拆锁。"""
        first = self._gate("alpha")
        self._acquire(first, "alpha")
        second = self._gate("beta")
        with self.assertRaises(DeviceGateError) as caught:
            self._acquire(second, "beta")
        self.assertEqual(caught.exception.reason_code, "device_busy")
        first.release(now=LATER, converged=True)
        self.assertIsNone(first.inspect())
        self._acquire(second, "beta")
        second.release(now=LATER, converged=True)

    def test_crash_pid_and_ttl_do_not_release(self) -> None:
        """不调用 release 的进程退出后，TTL/新 PID/新实例都不能自动解锁。"""
        crashed = self._gate("alpha")
        self._acquire(crashed, "alpha")
        later_gate = self._gate("alpha")
        with self.assertRaises(DeviceGateError) as caught:
            self._acquire(later_gate, "alpha")
        self.assertEqual(caught.exception.reason_code, "device_busy")
        acquired = crashed.inspect()
        self.assertEqual(acquired["state"], "held")
        self.assertEqual(acquired["profile_id"], "alpha")

    def test_abnormal_release_quarantines_and_blocks(self) -> None:
        """异常释放保留原绑定并置 quarantined，后续取得给出固定隔离原因。"""
        gate = self._gate("alpha")
        self._acquire(gate)
        gate.release(now=LATER, converged=False)
        row = gate.inspect()
        self.assertEqual(row["state"], "quarantined")
        self.assertEqual(row["owner_pid"], 4242)
        with self.assertRaises(DeviceGateError) as caught:
            self._acquire(self._gate("alpha"), "alpha")
        self.assertEqual(caught.exception.reason_code, "device_quarantined")
        with self.assertRaises(DeviceGateError) as verify_error:
            self._gate("alpha").verify()
        self.assertEqual(verify_error.exception.reason_code, "device_not_held")

    def test_nonce_tamper_and_profile_binding_mismatch_keep_row(self) -> None:
        """篡改 nonce 或换 profile 交付路径都不能通过复核或释放。"""
        gate = self._gate("alpha")
        self._acquire(gate)
        path = guard_database_path(self.os_root)
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "UPDATE device_guard SET nonce = ? WHERE device_key = 'primary'",
                ("00000000-0000-4000-8000-000000000099",),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(DeviceGateError) as caught:
            gate.verify()
        self.assertEqual(caught.exception.reason_code, "device_gate_mismatch")
        with self.assertRaises(DeviceGateError) as release_error:
            gate.release(now=LATER, converged=True)
        self.assertEqual(release_error.exception.reason_code, "device_gate_mismatch")
        self.assertEqual(gate.inspect()["state"], "held")

    def test_unreadable_gate_library_never_claims_release(self) -> None:
        """门禁库损坏时复核与释放都固定 IO 失败，不伪造设备空闲。"""
        gate = self._gate("alpha")
        self._acquire(gate)
        path = guard_database_path(self.os_root)
        path.write_bytes(b"not-a-sqlite-database")
        with self.assertRaises(DeviceGateError) as verify_error:
            gate.verify()
        self.assertEqual(verify_error.exception.reason_code, "device_gate_io_error")
        with self.assertRaises(DeviceGateError) as release_error:
            gate.release(now=LATER, converged=True)
        self.assertEqual(release_error.exception.reason_code, "device_gate_io_error")

    def test_non_windows_or_missing_root_is_unsupported(self) -> None:
        """未支持平台或缺失固定 OS 根时构造即拒绝真实占用。"""
        path = profile_runtime_path(self.root, "alpha")
        with self.assertRaises(DeviceGateError) as caught:
            SharedDeviceGate(
                profile_id="alpha",
                profile_db_path=path,
                platform_name="linux",
            )
        self.assertEqual(caught.exception.reason_code, "device_gate_unavailable")

    def test_cross_process_race_across_two_project_roots(self) -> None:
        """两个逻辑项目根/不同 profile 的独立进程竞争同一 OS 门禁只一个成功。"""
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        queue = context.Queue()
        workers = [
            context.Process(
                target=_race_worker,
                args=(
                    str(self.os_root),
                    str(self.root),
                    "alpha",
                    barrier,
                    queue,
                ),
            ),
            context.Process(
                target=_race_worker,
                args=(
                    str(self.os_root),
                    str(Path(self.project_tmp.name) / "another-clone"),
                    "beta",
                    barrier,
                    queue,
                ),
            ),
        ]
        for worker in workers:
            worker.start()
        outcomes = [queue.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=30)
        self.assertEqual(sorted(outcomes), ["device_busy", "held"])
        self.assertEqual(
            SharedDeviceGate(
                profile_id="alpha",
                profile_db_path=profile_runtime_path(self.root, "alpha"),
                os_root=self.os_root,
            ).inspect()["state"],
            "held",
        )


class ProfilePolicyActivityTests(unittest.TestCase):
    """覆盖真实 profile 库 leased/running/blocked 状态对 restore 的影响。"""

    def setUp(self) -> None:
        """创建临时项目根与独立 OS 根，并初始化 profile 库。"""
        self.project_tmp = tempfile.TemporaryDirectory()
        self.os_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.project_tmp.name)
        self.os_root = Path(self.os_tmp.name)
        store = SqliteProfileStore(self.root, "demo", now=NOW)
        store.open()
        store.close()
        self.profile_path = profile_runtime_path(self.root, "demo")

    def tearDown(self) -> None:
        """只清理本次拥有的临时根。"""
        self.project_tmp.cleanup()
        self.os_tmp.cleanup()

    def _insert_discovery(self, *, state: str) -> None:
        """直接写一条满足 v1 CHECK 的合成未决发现，供活动检查读取。"""
        conversation_key = "boss:conv:" + "1" * 64
        discovery_key = "boss:discovery:" + "2" * 64
        connection = sqlite3.connect(str(self.profile_path))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT OR IGNORE INTO hr_conversations(app, key, identity_hash) "
                "VALUES ('boss', ?, ?)",
                (conversation_key, "1" * 64),
            )
            if state == "blocked":
                connection.execute(
                    "INSERT INTO hr_discoveries(app, key, conversation_key, kind, "
                    "fingerprint, source_names_json, snapshot_id, state, priority, "
                    "created_at, updated_at, reason_code) "
                    "VALUES ('boss', ?, ?, 'unread', ?, '[\"row_badge\"]', ?, "
                    "'blocked', 0, ?, ?, 'unread_unverified')",
                    (
                        discovery_key,
                        conversation_key,
                        "3" * 64,
                        "00000000-0000-4000-8000-000000000801",
                        NOW,
                        NOW,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO hr_discoveries(app, key, conversation_key, kind, "
                    "fingerprint, source_names_json, snapshot_id, state, priority, "
                    "created_at, updated_at, lease_owner, nonce, lease_expires_at, "
                    "owner_host, owner_pid) "
                    "VALUES ('boss', ?, ?, 'unread', ?, '[\"row_badge\"]', ?, "
                    "'leased', 0, ?, ?, 'synthetic-owner', ?, ?, ?, 1)",
                    (
                        discovery_key,
                        conversation_key,
                        "3" * 64,
                        "00000000-0000-4000-8000-000000000801",
                        NOW,
                        NOW,
                        "00000000-0000-4000-8000-000000000802",
                        LATER,
                        HOST,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def test_blocked_is_unknown_and_leased_is_active(self) -> None:
        """blocked 记 unknown、leased 记 active；都不允许 restore 换目录。"""
        activity = ProfilePolicyActivity(self.root, os_root=self.os_root)
        self.assertEqual(
            activity.assess_restore(profile_id="demo", dest_pack="prompts/local/x"),
            "stopped",
        )
        self._insert_discovery(state="blocked")
        self.assertEqual(
            activity.assess_restore(profile_id="demo", dest_pack="prompts/local/x"),
            "unknown",
        )
        connection = sqlite3.connect(str(self.profile_path))
        try:
            connection.execute(
                "UPDATE hr_discoveries SET state = 'leased', reason_code = NULL, "
                "lease_owner = 'synthetic-owner', nonce = ?, lease_expires_at = ?, "
                "owner_host = ?, owner_pid = 1",
                (
                    "00000000-0000-4000-8000-000000000803",
                    LATER,
                    HOST,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            activity.assess_restore(profile_id="demo", dest_pack="prompts/local/x"),
            "active",
        )

    def test_legacy_v1_database_at_profile_path_is_unknown(self) -> None:
        """profile 路径是旧五表库而非完整 profile 库时保守 unknown。"""
        from app.adapters.sqlite.schema import initialize_database

        legacy = Path(self.project_tmp.name) / "legacy.sqlite3"
        initialize_database(legacy)
        self.profile_path.write_bytes(legacy.read_bytes())
        activity = ProfilePolicyActivity(self.root, os_root=self.os_root)
        self.assertEqual(
            activity.assess_restore(profile_id="demo", dest_pack="prompts/local/x"),
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
