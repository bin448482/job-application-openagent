"""验证 M5 生产用例边界：授权精确匹配、unknown 单次调用、零重放与当前证据。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from app.adapters.sqlite.profile_store import profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.domain.actions import (
    ActionAuthorization,
    ActionError,
    ActionObservation,
    ReconciliationObservation,
    action_key_for,
)
from app.domain.models import Evidence, Owner, canonical_hash
from app.ports.actions import ActionExecutionError
from app.ports.device_gate import DeviceSessionIdentity
from app.ports.store import StoreError
from app.usecases.actions import perform_action, reconcile_action
from tests import action_helpers as helpers
from tests import m1_helpers

NOW = m1_helpers.BASE_TIME
OWNER = Owner(m1_helpers.OWNER_ID, 4242, m1_helpers.HOST_A)
CONVERSATION = "boss:conv:" + canonical_hash(["合成联系人", "合成公司", "合成职位"])
MESSAGE = helpers.synthetic_text_key()
TEXT_KEY = f"boss:{MESSAGE}:reply"
DIGEST = canonical_hash({"synthetic": "pack"})


class FakeGate:
    """提供与当前租约一致的共享门禁身份，不读写真实门禁库。"""

    def __init__(self, path: Path) -> None:
        """保存 profile 库路径。"""
        self.path = path
        self.released: list[bool] = []

    def session_identity(self) -> DeviceSessionIdentity:
        """返回 demo/owner/held 的完整可信身份。"""
        return DeviceSessionIdentity(
            profile_id="demo",
            profile_db_path=str(self.path),
            owner=OWNER,
            nonce=helpers.BEFORE,
            state="held",
        )

    def verify(self, *args: object, **kwargs: object) -> None:
        """始终视为已复核。"""

    def require_available(self) -> None:
        """兼容门禁端口。"""

    def release(self, *, now: str, converged: bool) -> None:
        """记录收敛状态。"""
        self.released.append(converged)


class CountingExecutor:
    """记录执行次数，并按脚本成功、失败或返回观察。"""

    def __init__(self, *, fail: bool = False, observation: dict | None = None) -> None:
        """保存失败标志与观察脚本。"""
        self.calls = 0
        self.fail = fail
        self.observation = observation or helpers.observation()

    def read_current_ime(self) -> str:
        """返回合成 IME。"""
        return "synthetic-ime-default"

    def execute(self, plan: object) -> ActionObservation:
        """每次调用计数后返回或抛出。"""
        self.calls += 1
        if self.fail:
            raise ActionExecutionError("device_disconnected", clicked=True)
        data = dict(self.observation)
        data["ime_original"] = "synthetic-ime-default"
        data["ime_current"] = "synthetic-ime-default"
        return ActionObservation(
            action_key=TEXT_KEY, action_kind="send_text", **data
        )


class ActionUseCaseTests(unittest.TestCase):
    """真实临时 profile 库 + 生产用例，执行器为合成替身。"""

    def setUp(self) -> None:
        """创建 v2 profile 库、仓储、合成候选与运行中租约。"""
        self.tmp = tempfile.TemporaryDirectory(dir=helpers.ROOT, prefix=".m5-usecase-")
        self.root = Path(self.tmp.name)
        from app.adapters.sqlite.profile_store import SqliteProfileStore

        profile = SqliteProfileStore(self.root, "demo", now=NOW)
        profile.open()
        profile.close()
        self.path = profile_runtime_path(self.root, "demo")
        self.clock = m1_helpers.FakeClock()
        self.store = self._open()
        self.gate = FakeGate(self.path)
        self.lease = self._run_task()

    def tearDown(self) -> None:
        """关闭连接并只清理本用例临时根。"""
        self.store.close()
        self.tmp.cleanup()

    def _open(self) -> SqliteStore:
        """打开唯一 profile 仓储。"""
        return SqliteStore(
            self.path,
            profile_id="demo",
            local_host=m1_helpers.HOST_A,
            monotonic=self.clock.monotonic,
            stop_checker=lambda pid: "stopped",
            process_log=m1_helpers.ProcessLog(path=self.root / "usecase.jsonl"),
        )

    def _run_task(self, preview: str = "合成预览"):
        """插入合成候选并 claim/start。"""
        result = m1_helpers.make_result(
            profile_id="demo",
            preview=preview,
            observed_at=self.clock.now(),
            snapshot_id=str(uuid.uuid4()),
        )
        self.store.upsert_snapshot(result)
        lease = self.store.claim("boss", "unread", OWNER, self.clock.now())
        return self.store.start(lease, self.clock.now())

    def _authorization(
        self,
        *,
        action_key: str = TEXT_KEY,
        kinds: tuple[str, ...] = ("send_text",),
        expires_at: str | None = None,
        confirmed: bool = True,
    ) -> ActionAuthorization:
        """构造与当前 lease/摘要绑定的本轮分动作授权。"""
        return ActionAuthorization(
            profile_id="demo",
            base_hash=DIGEST,
            pack_hash=DIGEST,
            acceptance_base_hash=DIGEST,
            acceptance_pack_hash=DIGEST,
            task_key=self.lease.key,
            action_key=action_key,
            authorized_kinds=kinds,
            expires_at=expires_at or helpers.authorization_expiry(self.clock.now()),
            confirmed=confirmed,
            nonce=helpers.AFTER,
        )

    def _perform(self, executor: CountingExecutor, authorization=None):
        """调用生产 perform 用例。"""
        return perform_action(
            store=self.store,
            gate=self.gate,
            authorization=authorization or self._authorization(),
            lease=self.lease,
            action_kind="send_text",
            action_key=TEXT_KEY,
            target_key=MESSAGE,
            conversation_key=CONVERSATION,
            body_text="合成回复正文",
            attachment_name=None,
            target_label=None,
            anchor_label=None,
            ime_component=None,
            tap_points=((10, 20),),
            current_base_hash=DIGEST,
            current_pack_hash=DIGEST,
            daily_sends=10,
            reply_cooldown_seconds=0,
            profile_db_path=str(self.path),
            executor=executor,
            now=self.clock.now(),
        )

    def test_unknown_is_single_call_and_not_replayed(self) -> None:
        """执行失败只调用一次；再次同键在同任务上被 action_conflict 拒绝。"""
        executor = CountingExecutor(fail=True)
        result = self._perform(executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["gate_converged"])
        with self.assertRaises((StoreError, ActionError)):
            self._perform(executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT COUNT(*) AS total FROM action_records"
            ),
            {"total": 1},
        )

    def test_authorization_exact_target_and_action(self) -> None:
        """错误动作键/种类或未确认授权在 executor 前拒绝。"""
        executor = CountingExecutor()
        other_key = f"{CONVERSATION}:resume"
        with self.assertRaises(ActionError):
            self._perform(
                executor,
                authorization=self._authorization(action_key=other_key),
            )
        with self.assertRaises(ActionError):
            self._perform(
                executor,
                authorization=self._authorization(kinds=("send_resume",)),
            )
        with self.assertRaises(ActionError):
            self._perform(
                executor,
                authorization=self._authorization(confirmed=False),
            )
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.store.pending_actions(), [])

    def test_success_does_not_touch_facts_and_blocks_replay(self) -> None:
        """performed 不改写事实，且同一动作键不允许新任务重放。"""
        executor = CountingExecutor()
        result = self._perform(executor)
        self.assertEqual(result["state"], "performed")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT COUNT(*) AS total FROM profile_facts"
            ),
            {"total": 0},
        )
        lease2 = self._run_task(preview="合成预览二")
        with self.assertRaises(StoreError) as caught:
            perform_action(
                store=self.store,
                gate=self.gate,
                authorization=ActionAuthorization(
                    profile_id="demo",
                    base_hash=DIGEST,
                    pack_hash=DIGEST,
                    acceptance_base_hash=DIGEST,
                    acceptance_pack_hash=DIGEST,
                    task_key=lease2.key,
                    action_key=TEXT_KEY,
                    authorized_kinds=("send_text",),
                    expires_at=helpers.authorization_expiry(self.clock.now()),
                    confirmed=True,
                    nonce=helpers.BEFORE,
                ),
                lease=lease2,
                action_kind="send_text",
                action_key=TEXT_KEY,
                target_key=MESSAGE,
                conversation_key=CONVERSATION,
                body_text="合成回复正文",
                attachment_name=None,
                target_label=None,
                anchor_label=None,
                ime_component=None,
                tap_points=((10, 20),),
                current_base_hash=DIGEST,
                current_pack_hash=DIGEST,
                daily_sends=10,
                reply_cooldown_seconds=0,
                profile_db_path=str(self.path),
                executor=CountingExecutor(),
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "action_conflict")
        self.assertEqual(
            m1_helpers.fetch_one(
                self.path, "SELECT COUNT(*) AS total FROM action_records"
            ),
            {"total": 1},
        )

    def test_reconcile_requires_current_evidence(self) -> None:
        """人工确认使用历史观察时间不构成当前证据。"""
        executor = CountingExecutor(fail=True)
        result = self._perform(executor)
        self.assertEqual(result["state"], "unknown")
        old_evidence = Evidence(
            action_key=TEXT_KEY,
            outcome="performed",
            effect="sent",
            reason_code=None,
            before_snapshot_id=helpers.BEFORE,
            after_snapshot_id=helpers.AFTER,
            observed_at="2026-01-01T00:00:00.000Z",
        )
        observation = ReconciliationObservation(
            action_key=TEXT_KEY,
            resolution="confirm_performed",
            observed_at=self.clock.now(),
            evidence=old_evidence,
            command_quiet=True,
            scene_settled=True,
            confirmation_open=False,
            ime_original="synthetic-ime-default",
            ime_current="synthetic-ime-default",
            matched_action_key=TEXT_KEY,
            confirmed=True,
        )
        with self.assertRaises(ActionError) as caught:
            reconcile_action(
                store=self.store,
                gate=self.gate,
                observation=observation,
                profile_id="demo",
                profile_db_path=str(self.path),
                explicit_confirm=True,
                now=self.clock.now(),
            )
        self.assertEqual(caught.exception.reason_code, "invalid_observation")
        self.assertEqual(self.store.get_action(TEXT_KEY)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
