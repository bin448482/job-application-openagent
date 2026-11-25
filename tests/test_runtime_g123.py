"""验证 M6 G1 安全点真实来源、G2 实际一次定向采集与 G3 开始/到达边界。

全部使用临时项目根、临时 OS 门禁根与真实 SQLite profile/仓储；外部 fake 只
替代 M3 原始输入引用与环境时钟，不直接返回 ReadResult、候选、安全点或终态。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from unittest.mock import patch

from app.adapters.adb.runtime_read import AdbRuntimeRead
from app.adapters.adb.screen import AdbScreenReader
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
from app.domain.models import APP, Lease, ReadResult, canonical_hash, parse_utc
from app.domain.reading import (
    FieldEvidence,
    PageSnapshot,
    job_identity_hash_for,
    job_key_from_identity,
)
from app.domain.runtime import BudgetLimits
from app.ports.runtime import RawScene
from app.ports.store import StoreError
from app.process_log import ProcessLog
from app.usecases import policy as policy_usecases
from app.usecases.policy_eval import RepositorySnapshot
from app.usecases.runtime import derive_safe_point, execute_run
from tests import runtime_helpers as helpers

ROOT = Path(__file__).resolve().parents[1]
BATCH = helpers.BATCH


def _format_ms(base_ms: int, offset_ms: int) -> str:
    """把固定基准毫秒加偏移格式化为规范 UTC 秒/毫秒字符串。"""
    value = datetime.fromtimestamp(
        (base_ms + offset_ms) / 1000, tz=timezone.utc
    )
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class _Clock:
    """测试专用可信双时钟；只能通过显式推进改变墙钟与单调读数。"""

    def __init__(self, wall_base: str = helpers.NOW, mono_ms: int = 0) -> None:
        """保存基准墙钟毫秒与当前单调毫秒。"""
        self._base_ms = int(parse_utc(wall_base).timestamp() * 1000)
        self._wall_offset = mono_ms
        self._mono = mono_ms

    def now(self) -> str:
        """返回当前规范 UTC 时间，只在显式墙钟推进时变化。"""
        return _format_ms(self._base_ms, self._wall_offset)

    def mono(self) -> int:
        """返回当前单调毫秒。"""
        return self._mono

    def wall_ms(self) -> int:
        """返回当前墙钟相对基准的毫秒偏移，供独立仓储时钟一致性使用。"""
        return self._wall_offset

    def advance(self, ms: int) -> None:
        """同步推进墙钟与单调时钟，模拟真实环境耗时。"""
        self._wall_offset += ms
        self._mono += ms

    def advance_mono_only(self, ms: int) -> None:
        """只推进单调时钟，用于构造真实双钟漂移负控。"""
        self._mono += ms

    def wall_at(self, mono_ms: int) -> str:
        """返回基准墙钟加指定毫秒偏移的时间。"""
        return _format_ms(self._base_ms, mono_ms)


class _AdvancingRead(FixtureM3Read):
    """在指定原始读取引用上推进可信时钟，模拟真实采集耗时。"""

    def __init__(self, project_root: Path, clock: _Clock, target: str, ms: int) -> None:
        """保存真实解析根、时钟与需要计费的定向引用。"""
        super().__init__(project_root)
        self._clock = clock
        self._target = target
        self._ms = ms

    def read(self, relative: str):
        """先推进环境时钟再走真实 M3 解析。"""
        if relative == self._target:
            self._clock.advance(self._ms)
        return super().read(relative)


class _SwitchRead(FixtureM3Read):
    """按外部可见性标志切换真实原始 manifest，仍走真实 M3 解析。"""

    def __init__(self, project_root: Path, hidden: str, visible: str) -> None:
        """保存隐藏/可见原始引用与初始不可见状态。"""
        super().__init__(project_root)
        self._hidden = hidden
        self._visible = visible
        self.state = {"visible": False}

    def read(self, relative: str):
        """选择当前真实外部可见的原始输入；不返回业务答案。"""
        if relative == "switch:unread":
            relative = self._visible if self.state["visible"] else self._hidden
        return super().read(relative)


class _CountingRead(FixtureM3Read):
    """统计每个原始引用实际被真实解析读取的次数。"""

    def __init__(self, project_root: Path) -> None:
        """初始化分路径计数。"""
        super().__init__(project_root)
        self.counts: dict[str, int] = {}

    def read(self, relative: str):
        """计数后走真实 M3 解析。"""
        self.counts[relative] = self.counts.get(relative, 0) + 1
        return super().read(relative)


class _AdvancingBridge:
    """包裹真实合成 bridge，只在原始 transport 调用期间推进可信时钟。"""

    def __init__(self, inner: object, clock: _Clock, ms: int) -> None:
        """保存真实 bridge 与需要计费的 transport 耗时。"""
        self._inner = inner
        self._clock = clock
        self._ms = ms

    def descriptor(self):
        """委托真实 bridge 描述符。"""
        return self._inner.descriptor()  # type: ignore[attr-defined]

    def complete(self, **kwargs: object) -> str:
        """先推进环境时钟，再委托真实原始响应。"""
        self._clock.advance(self._ms)
        return self._inner.complete(**kwargs)  # type: ignore[attr-defined]




class _RuntimeCase(unittest.TestCase):
    """提供临时 profile/门禁/真实仓储与 execute_run 组合根。"""

    def setUp(self) -> None:
        self.temp, self.root = helpers.runtime_project()
        self.os_root = self.root / "os-root"
        code, payload, stderr = helpers.prepare(self.root, self.os_root)
        self.assertEqual(code, 0, (payload, stderr))
        self.assertEqual(stderr, "")
        self.credential, self.config_hash = helpers.credential(self.root)
        self.provider, self.model_id, _ = helpers.required_owner()
        self.owner = helpers.owner()
        self.path = profile_runtime_path(self.root, "demo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    # ---- 真实 SQLite / 场景辅助 ----

    def _store(self) -> SqliteStore:
        """按当前 profile 路径打开真实运行仓储。"""
        return SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.owner.host,
            require_runtime=True,
        )

    def _scenario(
        self,
        name: str,
        steps: list[dict[str, object]],
        *,
        batch_id: str = BATCH,
    ) -> str:
        """写入合成场景刺激并返回相对路径。"""
        return helpers.write_scenario(
            self.root,
            f"tests/fixtures/runtime/{name}.json",
            steps=steps,
            batch_id=batch_id,
        )

    def _execute(
        self,
        rel: str,
        *,
        clock: _Clock | None = None,
        read: object | None = None,
        on_boundary: object | None = None,
        limits: BudgetLimits | None = None,
        process_log: ProcessLog | None = None,
        bridge_factory: object | None = None,
        store_hook: object | None = None,
        store_monotonic: object | None = None,
    ) -> dict[str, object]:
        """经真实 M3 解析、唯一 decide、真实 profile/gate/仓储运行一次。"""
        active = clock or _Clock()
        spec = json.loads((self.root / rel).read_text(encoding="utf-8"))
        _pack_rel, base_files, _files, _guides, pack = (
            policy_usecases.load_active_pack(
                FilePolicyRepository(self.root), "prompts/local/demo"
            )
        )
        gate = SharedDeviceGate(
            profile_id="demo", profile_db_path=self.path, os_root=self.os_root
        )
        profile_store = SqliteProfileStore(
            self.root, "demo", create=False, now=active.now()
        )
        scene = StimulusRuntimeScene(spec, profile_id="demo")
        owner = self.owner

        def default_bridge(stimulus: object) -> SyntheticDecisionBridge:
            """为当前步骤构造合成 bridge transport，由唯一 decide 校验。"""
            return SyntheticDecisionBridge(
                stimulus,
                provider_id=self.provider,
                model_id=self.model_id,
                model_config_hash=self.config_hash,
            )

        def open_store() -> SqliteStore:
            """在共享门禁已持有、profile 已打开后打开唯一运行仓储。"""
            monotonic = store_monotonic or (lambda: active.wall_ms() / 1000.0)
            store = SqliteStore(
                self.path,
                profile_id="demo",
                local_host=owner.host,
                monotonic=monotonic,
                require_runtime=True,
            )
            if store_hook is not None:
                return store_hook(store)  # type: ignore[operator,no-any-return]
            return store

        result = execute_run(
            base_files=base_files,
            pack=pack,
            mode="draft_only",
            confirm_device=True,
            gate=gate,
            profile_store=profile_store,
            profile_db_path=str(self.path),
            owner=owner,
            now=active.now(),
            scene=scene,
            read=read or FixtureM3Read(self.root),
            bridge_factory=bridge_factory or default_bridge,  # type: ignore[arg-type]
            snapshot=RepositorySnapshot(FilePolicyRepository(self.root), _pack_rel),
            open_store=open_store,
            limits=limits or BudgetLimits(),
            provider_id=self.provider,
            model_id=self.model_id,
            model_config_hash=self.config_hash,
            eval_store=FileEvalStore(self.root),
            credential_rel=self.credential,
            clock=active.now,
            monotonic_ms=active.mono,
            on_boundary=on_boundary,
            process_log=process_log,
        )
        return result["run"]  # type: ignore[return-value]

    def _step(self, *, targeted: str | None = None, **kwargs: object) -> dict[str, object]:
        """构造场景步骤并附加可选定向采集引用。"""
        item = helpers.step(**kwargs)
        item["targeted_read_fixture"] = targeted
        return item

    def _events(
        self, batch_id: str = BATCH
    ) -> list[tuple[str, dict[str, object], str | None]]:
        """只读返回指定批次的调度事件名/payload/原因。"""
        connection = sqlite3.connect(str(self.path))
        try:
            rows = connection.execute(
                "SELECT event, payload_json, reason_code FROM scheduler_events "
                "WHERE batch_id = ? ORDER BY event_id",
                (batch_id,),
            ).fetchall()
        finally:
            connection.close()
        return [
            (str(event), json.loads(payload), reason)
            for event, payload, reason in rows
        ]

    def _cursor(self, batch_id: str = BATCH) -> dict[str, object] | None:
        """只读返回指定批次持久游标。"""
        connection = sqlite3.connect(str(self.path))
        try:
            row = connection.execute(
                "SELECT cursor_json FROM job_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else json.loads(row[0])

    def _two_job_list(
        self, name: str, label_a: str, label_b: str
    ) -> tuple[str, str, str]:
        """生成含两张真实职位卡的单页 job_list，返回 manifest 与两个职位键。"""
        manifest_a, key_a = helpers.make_job_fixture(self.root, label_a)
        manifest_b, key_b = helpers.make_job_fixture(self.root, label_b)
        blocks: list[str] = []
        for manifest in (manifest_a, manifest_b):
            xml = (self.root / manifest).parent.joinpath("job_list.xml").read_text(
                encoding="utf-8"
            )
            start = xml.index('      <node clickable="true"')
            end = xml.index("      </node>", start) + len("      </node>")
            blocks.append(xml[start:end])
        public = (ROOT / "tests/fixtures/read/job_list.xml").read_text(encoding="utf-8")
        header = public[: public.index('      <node clickable="true"')]
        footer = public[public.index("    </node>\n  </node>\n</hierarchy>"):]
        target = self.root / "tests/fixtures/runtime" / name
        target.mkdir(parents=True, exist_ok=True)
        for item in ("profile.json", "job_list.json"):
            shutil.copyfile(ROOT / "tests/fixtures/read" / item, target / item)
        (target / "job_list.xml").write_text(
            header + blocks[0] + "\n" + blocks[1] + "\n" + footer,
            encoding="utf-8",
        )
        return f"tests/fixtures/runtime/{name}/job_list.json", key_a, key_b

    def _slow_copy(self, manifest_rel: str, name: str) -> str:
        """复制同一合成职位 manifest 到独立路径，供只在该次读取推进时钟。"""
        source = (self.root / manifest_rel).parent
        target = self.root / "tests/fixtures/runtime" / name
        shutil.copytree(source, target)
        return f"tests/fixtures/runtime/{name}/{Path(manifest_rel).name}"

    def _task_events(self) -> list[str]:
        """只读返回 M1 任务事件名，供续租/终态时序断言。"""
        connection = sqlite3.connect(str(self.path))
        try:
            rows = connection.execute(
                "SELECT event FROM events ORDER BY event_id"
            ).fetchall()
        finally:
            connection.close()
        return [str(row[0]) for row in rows]

    def _seed_discovery(self, label: str) -> tuple[str, str]:
        """通过真实 M3 取值与生产 upsert 写入同身份未读发现。"""
        manifest, key = helpers.make_unread_fixture(self.root, label)
        helpers.seed_discovery(self.root, key)
        return manifest, key

    def _write_unread_variant(
        self,
        label: str,
        *,
        badge: str | None = "1",
        proof: dict[str, object] | None = None,
        observed_at: str = helpers.NOW,
        strip_identity: bool = False,
    ) -> str:
        """复制公开未读夹具，按合成身份/角标/覆盖证明生成独立原始输入。"""
        source = ROOT / "tests/fixtures/read"
        target = self.root / "tests/fixtures/runtime" / f"targeted-{label}"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("profile.json", "unread.xml", "unread.json"):
            shutil.copyfile(source / name, target / name)
        contact = f"合成联系人{label}"
        company = f"合成公司{label}"
        position = f"合成职位{label}"
        xml = (target / "unread.xml").read_text(encoding="utf-8")
        xml = (
            xml.replace("合成联系人甲", contact)
            .replace("合成公司甲", company)
            .replace("合成职位甲", position)
        )
        if badge is None:
            xml = xml.replace(
                '        <node text="1" class="android.widget.TextView" '
                'bounds="[20,220][80,280]" />\n',
                "",
            )
        else:
            xml = xml.replace(
                'text="1" class="android.widget.TextView" '
                'bounds="[20,220][80,280]"',
                f'text="{badge}" class="android.widget.TextView" '
                'bounds="[20,220][80,280]"',
            )
        if strip_identity:
            for name in ("tv_name", "tv_company", "tv_position"):
                marker = f'com.hpbr.bosszhipin:id/{name}"'
                index = xml.find(marker)
                if index < 0:
                    raise AssertionError("identity node missing")
                start = xml.rfind("<node ", 0, index)
                end = xml.find("/>", index) + 2
                xml = xml[:start] + xml[end:]
        (target / "unread.xml").write_text(xml, encoding="utf-8")
        manifest = json.loads((target / "unread.json").read_text(encoding="utf-8"))
        manifest["observed_at"] = observed_at
        manifest["coverage_proof"] = proof or {
            "kind": "viewport",
            "top_proven": False,
            "bottom_proven": False,
        }
        (target / "unread.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return f"tests/fixtures/runtime/targeted-{label}/unread.json"

    def _insert_action(
        self,
        conversation_key: str,
        state: str,
        *,
        created_at: str,
        suffix: str,
    ) -> None:
        """向真实 profile 库写入一条满足约束的动作行，用于安全点来源负控。"""
        connection = sqlite3.connect(str(self.path))
        try:
            row = connection.execute(
                "SELECT \"key\" FROM hr_discoveries WHERE app = 'boss' "
                "AND conversation_key = ? ORDER BY created_at LIMIT 1",
                (conversation_key,),
            ).fetchone()
            self.assertIsNotNone(row)
            discovery_key = str(row[0])
            outcome = None
            effect = None
            reason = None
            before = None
            after = None
            observed = None
            if state == "performed":
                outcome, effect, observed = "performed", "sent", created_at
                before = str(uuid.uuid4())
                after = str(uuid.uuid4())
            elif state == "not_performed":
                outcome, effect = "not_performed", "none"
                reason, observed = "precondition_failed", created_at
            elif state == "unknown":
                outcome, effect = "unknown", "none"
                reason, observed = "result_unverified", created_at
            connection.execute(
                "INSERT INTO action_records(action_key, app, action_kind, task_kind, "
                "task_key, discovery_task_key, job_task_key, target_key, "
                "conversation_key, state, outcome, effect, reason_code, "
                "before_snapshot_id, after_snapshot_id, observed_at, "
                "authorization_json, lease_owner, owner_pid, owner_host, nonce, "
                "lease_expires_at, created_at, updated_at) VALUES "
                "(?, 'boss', 'send_text', 'discovery', ?, ?, NULL, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"boss:action:{suffix}",
                    discovery_key,
                    discovery_key,
                    conversation_key,
                    conversation_key,
                    state,
                    outcome,
                    effect,
                    reason,
                    before,
                    after,
                    observed,
                    self.owner.id,
                    self.owner.pid,
                    self.owner.host,
                    str(uuid.uuid4()),
                    helpers.NOW,
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()


class SafePointSourceTests(_RuntimeCase):
    """G1：七条件来自真实读取/持久 pending/动作/检查点与真实任务身份。"""

    def _read(self, manifest: str):
        """经真实解析器读取一个未读合成 manifest。"""
        result = load_and_read_fixture(
            self.root / manifest, "00000000-0000-4000-8000-0000000000aa"
        )
        self.assertEqual(result.status, "ok", result.reason_code)
        return result

    def test_empty_checkpoint_is_false_until_real_commit(self) -> None:
        """空 checkpoint 表必须由同一派生入口得到 false；真实提交后才为真。"""
        store = self._store()
        try:
            store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            job_result = load_and_read_fixture(
                ROOT / "tests/fixtures/read/job_list.json",
                "00000000-0000-4000-8000-0000000000aa",
            )
            self.assertEqual(job_result.status, "ok")
            job_key = job_result.value.cards[0].job_key
            safe = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=job_key,
                read_results=(job_result,),
                raw=RawScene("", None, 0),
            )
            self.assertFalse(safe.checkpoint_committed)
            self.assertFalse(safe.failed_conditions() != ("checkpoint_committed",))
            cursor = store.load_batch(BATCH)["cursor"]
            store.save_checkpoint(BATCH, cursor, now=helpers.NOW)
            safe = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=job_key,
                read_results=(job_result,),
                raw=RawScene("", None, 0),
            )
            self.assertTrue(safe.is_safe())
        finally:
            store.close()

    def test_action_state_scoped_to_real_task_identity(self) -> None:
        """A/B 最近动作不得互相洗白/阻断；错/空身份保守，已收敛历史不永久阻断。"""
        manifest_a, key_a = self._seed_discovery("g1a")
        manifest_b, key_b = self._seed_discovery("g1b")
        read_a = self._read(manifest_a)
        read_b = self._read(manifest_b)
        store = self._store()
        try:
            store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            # A 较早 unknown，B 较晚 performed：A 不被 B 洗白，B 自身已收敛。
            self._insert_action(key_a, "unknown", created_at="2026-11-25T09:00:01.000Z", suffix="a1")
            self._insert_action(key_b, "performed", created_at="2026-11-25T09:00:02.000Z", suffix="b1")
            safe_a = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=key_a,
                read_results=(read_a,),
                raw=RawScene("", None, 0),
            )
            safe_b = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=key_b,
                read_results=(read_b,),
                raw=RawScene("", None, 0),
            )
            self.assertFalse(safe_a.action_state_committed)
            self.assertFalse(safe_a.no_pending_action)
            self.assertTrue(safe_b.action_state_committed)
            # 真实不变量：一致仓储里第六项为假时，其未决动作必被第五项命中。
            for value in (safe_a, safe_b):
                if not value.action_state_committed:
                    self.assertFalse(value.no_pending_action)

            connection = sqlite3.connect(str(self.path))
            try:
                connection.execute("DELETE FROM action_records")
                connection.commit()
            finally:
                connection.close()
            # A 较早 performed，B 较晚 unknown：A 不被 B 阻断。
            self._insert_action(key_a, "performed", created_at="2026-11-25T09:00:03.000Z", suffix="a2")
            self._insert_action(key_b, "unknown", created_at="2026-11-25T09:00:04.000Z", suffix="b2")
            safe_a = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=key_a,
                read_results=(read_a,),
                raw=RawScene("", None, 0),
            )
            self.assertTrue(safe_a.action_state_committed)
            self.assertFalse(safe_a.no_pending_action)
            # 错/空身份与错误任务：不回退全局最近动作；None 仅表示本轮尚未 start。
            other_job = "boss:job:" + canonical_hash({"job": "not-started-this-round"})
            self.assertTrue(
                derive_safe_point(
                    store=store,
                    batch_id=BATCH,
                    task_key=other_job,
                    read_results=(read_a,),
                    raw=RawScene("", None, 0),
                ).action_state_committed
            )
            self.assertFalse(
                derive_safe_point(
                    store=store,
                    batch_id=BATCH,
                    task_key="boss:conv:not-a-hash",
                    read_results=(read_a,),
                    raw=RawScene("", None, 0),
                ).action_state_committed
            )
            no_prior = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=None,
                read_results=(read_a,),
                raw=RawScene("", None, 0),
            )
            self.assertTrue(no_prior.action_state_committed)
            self.assertFalse(no_prior.no_pending_action)
            page = load_and_read_fixture(
                ROOT / "tests/fixtures/read/page.json",
                "00000000-0000-4000-8000-0000000000ab",
            )
            self.assertTrue(
                derive_safe_point(
                    store=store,
                    batch_id=BATCH,
                    task_key=None,
                    read_results=(page,),
                    raw=RawScene("", None, 0),
                ).action_state_committed
            )
        finally:
            store.close()

    def test_no_pending_distinct_from_committed_when_unrelated_pending(self) -> None:
        """现场有另一任务 pending 时第五项为假，当前任务最近动作仍可已提交。"""
        _manifest_a, key_a = self._seed_discovery("g1c")
        _manifest_b, key_b = self._seed_discovery("g1d")
        read_a = self._read(_manifest_a)
        store = self._store()
        try:
            store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            self._insert_action(key_a, "performed", created_at="2026-11-25T09:00:01.000Z", suffix="c1")
            self._insert_action(key_b, "pending", created_at="2026-11-25T09:00:02.000Z", suffix="d1")
            safe = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=key_a,
                read_results=(read_a,),
                raw=RawScene("", None, 0),
            )
            self.assertFalse(safe.no_pending_action)
            self.assertTrue(safe.action_state_committed)
        finally:
            store.close()

    def test_execute_run_positive_and_raw_scene_negative(self) -> None:
        """真实执行链正向派生；hint 假不阻断，raw 现场未收敛仍拦下新任务。"""
        manifest, _key = helpers.make_job_fixture(self.root, "g1run")
        hint_false = helpers.safe_point(
            list_returned=False,
            input_empty=False,
            confirmation_closed=False,
            no_inflight_command=False,
            no_pending_action=False,
            action_state_committed=False,
            checkpoint_committed=False,
        )
        positive = self._scenario(
            "g1-positive",
            [
                self._step(
                    read_fixture=manifest,
                    job_fixture=manifest,
                    safe=hint_false,
                )
            ],
        )
        run = self._execute(positive)
        self.assertEqual(run["processed"], 1)
        self.assertIsNone(run["pause_reason"])

        manifest2, _key2 = helpers.make_job_fixture(self.root, "g1unsafe")
        unsafe = self._scenario(
            "g1-unsafe",
            [
                self._step(
                    read_fixture=manifest2,
                    job_fixture=manifest2,
                    raw_scene={
                        "input_text": "未发送草稿",
                        "confirmation_title": None,
                        "inflight_commands": 0,
                        "ime_current": "synthetic-ime-default",
                    },
                )
            ],
        )
        run = self._execute(unsafe)
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["pause_reason"], "precondition_failed")

    def test_complete_job_detail_is_not_returned_to_list(self) -> None:
        """完整职位详情即使 actionable 也不能冒充回列表安全点。"""
        detail = "tests/fixtures/read/job_detail.json"
        step = self._step(read_fixture=detail, job_fixture=detail)
        rel = self._scenario("g1-detail", [step])
        run = self._execute(rel)
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["pause_reason"], "precondition_failed")

    def test_unknown_page_is_not_returned_to_list(self) -> None:
        """unknown 页面即使 status=ok 也不是回列表证据。"""
        store = self._store()
        try:
            store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            unknown = ReadResult(
                status="ok",
                value=PageSnapshot(
                    app=APP,
                    profile_id="synthetic-boss-m3",
                    page="unknown",
                    signals=(),
                    field_evidence=(
                        FieldEvidence(
                            "page", "hierarchy", "low", "unknown", "missing_signal"
                        ),
                    ),
                ),
                reason_code=None,
                snapshot_id="00000000-0000-4000-8000-0000000000ac",
                observed_at=helpers.NOW,
                source="fixture",
                confidence="low",
            )
            safe = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=None,
                read_results=(unknown,),
                raw=RawScene("", None, 0),
            )
            self.assertFalse(safe.list_returned)
        finally:
            store.close()

    def test_checkpoint_event_failure_leaves_uncommitted(self) -> None:
        """检查点事件写入故障回滚后，同一派生入口必须仍报告未提交。"""
        def boom(name: str, connection: object) -> None:
            """只在 checkpoint 事件边界注入真实事务故障。"""
            del connection
            if name == "checkpoint_before_event":
                raise RuntimeError("synthetic checkpoint fault")

        store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.owner.host,
            fault_hook=boom,
            require_runtime=True,
        )
        try:
            store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            job_result = load_and_read_fixture(
                ROOT / "tests/fixtures/read/job_list.json",
                "00000000-0000-4000-8000-0000000000ad",
            )
            job_key = job_result.value.cards[0].job_key
            cursor = store.load_batch(BATCH)["cursor"]
            with self.assertRaises(StoreError):
                store.save_checkpoint(BATCH, cursor, now=helpers.NOW)
            status = store.checkpoint_status(BATCH)
            self.assertIsNone(status["last_checkpoint_id"])
            self.assertEqual(status["checkpoint_events"], 0)
            safe = derive_safe_point(
                store=store,
                batch_id=BATCH,
                task_key=job_key,
                read_results=(job_result,),
                raw=RawScene("", None, 0),
            )
            self.assertFalse(safe.checkpoint_committed)
        finally:
            store.close()

    def test_scoped_committed_history_does_not_block_new_round(self) -> None:
        """真实已收敛历史动作不因新轮次/恢复批次永久阻断新的安全任务。"""
        _manifest, key = self._seed_discovery("g1history")
        self._insert_action(
            key,
            "performed",
            created_at="2026-11-25T09:00:01.000Z",
            suffix="history",
        )
        manifest, _job = helpers.make_job_fixture(self.root, "g1history-job")
        rel = self._scenario(
            "g1-history",
            [self._step(read_fixture=manifest, job_fixture=manifest)],
        )
        run = self._execute(rel, clock=_Clock())
        self.assertEqual(run["processed"], 1)
        self.assertIsNone(run["pause_reason"])


class TargetedReadTests(_RuntimeCase):
    """G2：verify_unread 必须执行一次真实定向采集，并按真实结果派生候选。"""

    def _ok_scenario(
        self, label: str, *, targeted: str | None, verification: str = "none"
    ) -> tuple[str, str]:
        """构造 unknown 初始读取 + 可选真实定向引用的一次场景。"""
        unknown, key = helpers.make_unknown_unread_fixture(self.root, label)
        helpers.seed_discovery(self.root, key)
        conversation = f"tests/fixtures/runtime/read-{label}/conversation.json"
        rel = self._scenario(
            f"targeted-{label}",
            [
                self._step(
                    read_fixture=unknown,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                    verification=verification,
                    targeted=targeted,
                )
            ],
        )
        return rel, key

    def test_actual_targeted_read_clears_once_with_real_budget(self) -> None:
        """真实一次定向采集清除 unknown，恰一次事件且按实际读取扣预算。"""
        _unused, _key = helpers.make_unread_fixture(self.root, "g2ok")
        targeted = self._write_unread_variant("g2ok", observed_at=helpers.NOW)
        rel, key = self._ok_scenario("g2ok", targeted=targeted)
        clock = _Clock()
        run = self._execute(rel, clock=clock)
        self.assertEqual(run["pause_reason"], None)
        self.assertEqual(run["processed"], 1)
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(run["drafts"][0]["task_key"], key)  # type: ignore[index]
        # 初始未读 + 会话 + 一次定向 = 3 次 fixture 采集。
        self.assertEqual(run["remaining_budget"]["device_calls"], 197)  # type: ignore[index]
        verified = [item for item in self._events() if item[0] == "unread_verified"]
        self.assertEqual(len(verified), 1)
        payload = verified[0][1]
        self.assertEqual(payload["outcome"], "cleared")
        self.assertEqual(payload["device_calls"], 1)

    def test_hint_cleared_without_capability_or_real_clear_does_not_clear(self) -> None:
        """旧 hint 声明 cleared 但缺采集能力/类型不符时必须暂停，不从 hint 清除。"""
        for index, targeted in enumerate((None, "tests/fixtures/read/page.json")):
            label = f"g2hint{index}"
            helpers.make_unknown_unread_fixture(self.root, label)
            helpers.make_unread_fixture(self.root, label)
            rel, _key = self._ok_scenario(
                label, targeted=targeted, verification="cleared"
            )
            run = self._execute(rel, clock=_Clock())
            self.assertEqual(run["pause_reason"], "unread_unverified", label)
            self.assertEqual(run["processed"], 0, label)
            verified = [item for item in self._events() if item[0] == "unread_verified"]
            self.assertTrue(verified)
            self.assertNotEqual(verified[-1][1]["outcome"], "cleared")

    def test_stale_and_future_targeted_reads_are_not_cleared(self) -> None:
        """定向采集 observed_at 过旧或未来都按 stale_snapshot 保守暂停。"""
        for label, observed in (
            ("g2stale", "2026-11-23T23:59:59.000Z"),
            ("g2future", "2026-11-25T10:00:01.000Z"),
        ):
            _unused, _key = helpers.make_unread_fixture(self.root, label)
            targeted = self._write_unread_variant(label, observed_at=observed)
            rel, _key = self._ok_scenario(label, targeted=targeted)
            run = self._execute(rel, clock=_Clock())
            self.assertEqual(run["pause_reason"], "unread_unverified", label)
            event = [item for item in self._events() if item[0] == "unread_verified"][-1]
            self.assertEqual(event[1]["outcome"], "stale_snapshot", label)

    def test_full_coverage_absent_clears_without_candidate(self) -> None:
        """全覆盖 absent 是合法已核实无未读：不暂停也不生成会话候选。"""
        label = "g2absent"
        helpers.make_unread_fixture(self.root, label)
        helpers.make_unknown_unread_fixture(self.root, label)
        targeted = self._write_unread_variant(
            label,
            badge="0",
            proof={"kind": "full_traversal", "top_proven": True, "bottom_proven": True},
        )
        rel, _key = self._ok_scenario(label, targeted=targeted)
        run = self._execute(rel, clock=_Clock())
        self.assertIsNone(run["pause_reason"])
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["drafts"], [])
        event = [item for item in self._events() if item[0] == "unread_verified"][-1]
        self.assertEqual(event[1]["outcome"], "cleared")

    def test_viewport_absent_and_count_without_target_stay_unknown(self) -> None:
        """viewport 角标消失与有计数无定位都不能清除 unknown。"""
        cases = (
            ("g2viewport", "0", False),
            ("g2count", "1", True),
        )
        for label, badge, strip_identity in cases:
            helpers.make_unread_fixture(self.root, label)
            helpers.make_unknown_unread_fixture(self.root, label)
            targeted = self._write_unread_variant(
                label, badge=badge, strip_identity=strip_identity
            )
            rel, _key = self._ok_scenario(label, targeted=targeted)
            run = self._execute(rel, clock=_Clock())
            self.assertEqual(run["pause_reason"], "unread_unverified", label)
            event = [item for item in self._events() if item[0] == "unread_verified"][-1]
            self.assertEqual(event[1]["outcome"], "unread_unverified", label)

    def test_ordinary_read_budget_is_settled_before_targeted_read(self) -> None:
        """普通读取耗时先结算；耗尽预算时不得继续定向读取或开始业务任务。"""
        _unused, _key = helpers.make_unread_fixture(self.root, "g2pre")
        unknown, _ = helpers.make_unknown_unread_fixture(self.root, "g2pre")
        targeted = self._write_unread_variant("g2pre", observed_at=helpers.NOW)
        rel, _key = self._ok_scenario("g2pre", targeted=targeted)
        clock = _Clock()
        read = _AdvancingRead(self.root, clock, unknown, 1_500)
        run = self._execute(
            rel,
            clock=clock,
            read=read,
            limits=BudgetLimits(round_seconds=1),
        )
        self.assertEqual(run["pause_reason"], "budget_exhausted")
        self.assertEqual(run["processed"], 0)
        self.assertEqual(run["remaining_budget"]["seconds"], 0)  # type: ignore[index]
        self.assertFalse(
            [item for item in self._events() if item[0] == "unread_verified"]
        )

    def test_ordinary_read_seconds_are_not_lost_with_default_budget(self) -> None:
        """默认预算下普通读取的整秒也真实扣除，不因进入定向分支丢失。"""
        _unused, _key = helpers.make_unread_fixture(self.root, "g2pre2")
        unknown, _ = helpers.make_unknown_unread_fixture(self.root, "g2pre2")
        targeted = self._write_unread_variant("g2pre2", observed_at=helpers.NOW)
        rel, _key = self._ok_scenario("g2pre2", targeted=targeted)
        clock = _Clock()
        read = _AdvancingRead(self.root, clock, unknown, 1_500)
        run = self._execute(rel, clock=clock, read=read)
        self.assertEqual(run["pause_reason"], None)
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["remaining_budget"]["seconds"], 899)  # type: ignore[index]

    def test_first_observed_uses_targeted_completion_when_it_first_reveals_unread(self) -> None:
        """定向采集首次带来 present 未读时，首次观察取该次采集完成边界，而非旧 unknown tick。"""
        label = "g2first"
        _unread, key = helpers.make_unread_fixture(self.root, label)
        unknown, _ = helpers.make_unknown_unread_fixture(self.root, label)
        helpers.seed_discovery(self.root, key)
        conversation = f"tests/fixtures/runtime/read-{label}/conversation.json"
        clock = _Clock()
        targeted = self._write_unread_variant(
            label, observed_at=clock.wall_at(10_250)
        )
        job_manifest, _job = helpers.make_job_fixture(self.root, "g2first-job")
        rel = self._scenario(
            "targeted-first-observation",
            [
                self._step(read_fixture=job_manifest, job_fixture=job_manifest),
                self._step(
                    read_fixture=unknown,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                    targeted=targeted,
                ),
            ],
        )
        read = _AdvancingRead(self.root, clock, targeted, 250)
        seen = {"checkpoint": 0}

        def boundary(name: str) -> None:
            """job checkpoint 后推进到 10000；定向采集自身耗时 250ms。"""
            if name == "after_checkpoint" and seen["checkpoint"] == 0:
                seen["checkpoint"] = 1
                clock.advance(10_000)

        run = self._execute(rel, clock=clock, read=read, on_boundary=boundary)
        self.assertEqual(len(run["preemptions"]), 1)
        event = run["preemptions"][0]
        self.assertEqual(event["first_observed_at"], clock.wall_at(10_250))
        self.assertEqual(event["handling_started_at"], clock.wall_at(10_250))
        self.assertEqual(event["waited_ms"], 0)
        self.assertEqual(
            self._cursor()["first_observed_at"],  # type: ignore[index]
            clock.wall_at(10_250),
        )

    def test_targeted_read_duration_is_counted_from_trusted_monotonic(self) -> None:
        """定向采集真实耗时按可信单调钟计入同轮预算，不丢亚秒余量。"""
        _unused, _key = helpers.make_unread_fixture(self.root, "g2time")
        targeted = self._write_unread_variant("g2time", observed_at=helpers.NOW)
        rel, key = self._ok_scenario("g2time", targeted=targeted)
        clock = _Clock()
        read = _AdvancingRead(self.root, clock, targeted, 1500)
        run = self._execute(rel, clock=clock, read=read)
        self.assertEqual(run["pause_reason"], None)
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["remaining_budget"]["seconds"], 899)  # type: ignore[index]
        event = [item for item in self._events() if item[0] == "unread_verified"][-1]
        self.assertEqual(event[1]["elapsed_ms"], 1500)
        self.assertEqual(event[1]["device_calls"], 1)
        self.assertEqual(run["drafts"][0]["task_key"], key)  # type: ignore[index]

    def test_targeted_collection_logs_started_and_single_terminal_without_identity(self) -> None:
        """定向采集日志只有固定分类/耗时/计数，started→单一终态且不含会话身份。"""
        _unused, _key = helpers.make_unread_fixture(self.root, "g2log")
        targeted = self._write_unread_variant("g2log", observed_at=helpers.NOW)
        rel, key = self._ok_scenario("g2log", targeted=targeted)
        log = ProcessLog(path=self.root / "data/logs/g2-targeted.jsonl")
        self._execute(rel, clock=_Clock(), process_log=log)
        events = [
            json.loads(line)
            for line in log.path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        unread = [
            item
            for item in events
            if item["stage"] == "run" and item["operation"] == "unread"
        ]
        self.assertEqual([item["result"] for item in unread], ["started", "ok"])
        self.assertEqual(unread[-1]["device_count"], 1)
        self.assertGreaterEqual(unread[-1]["duration_ms"], 0)
        text = log.path.read_text(encoding="utf-8")
        self.assertNotIn(key, text)
        self.assertNotIn("unread.xml", text)


class AdbRuntimeReadCommandTests(_RuntimeCase):
    """S06：唯一 AdbRuntimeRead 经真实 AdbScreenReader/run_adb 统计实际创建命令。"""

    class _Gate:
        """满足 ScreenPort 的共享门禁替身。"""

        def __init__(self) -> None:
            """记录复核次数。"""
            self.verifies = 0

        def require_available(self) -> None:
            """始终可用。"""
            return

        def verify(self) -> None:
            """记录一次命令前复核。"""
            self.verifies += 1

        def release(self) -> None:
            """无占用需要释放。"""
            return

    class _FakePopen:
        """只模拟一次已创建命令，不启动真实进程。"""

        def __init__(self, output: str) -> None:
            """保存固定输出与模拟 PID。"""
            self.pid = 5000 + len(output)
            self.returncode = 0
            self._output = output
            self.killed = False

        def communicate(self, timeout=None):
            """返回固定输出。"""
            del timeout
            return (self._output, "")

        def kill(self) -> None:
            """记录清理。"""
            self.killed = True

    def test_adb_runtime_read_counts_actual_spawns_only(self) -> None:
        """单页真实 dump+cat 计 2 次实际创建，解析仍走真实 M3。"""
        page_xml = (ROOT / "tests/fixtures/read/page.xml").read_text(
            encoding="utf-8"
        )
        gate = self._Gate()
        adapter = AdbRuntimeRead(
            self.root,
            AdbScreenReader(None, gate=gate),
            "synthetic-serial",
        )
        with patch(
            "app.adapters.adb.command.subprocess.Popen",
            side_effect=[self._FakePopen("dumped"), self._FakePopen(page_xml)],
        ):
            result = adapter.read("tests/fixtures/read/page.json")
        self.assertEqual(result.status, "ok")
        self.assertEqual(adapter.take_device_calls(), 2)
        self.assertEqual(gate.verifies, 2)


class ArrivalAndStartBoundaryTests(_RuntimeCase):
    """G3：checkpoint→refresh→安全点→真实开始边界与 arrival/first_observed 分离。"""

    def _job_then_unread(
        self,
        name: str,
        *,
        seed: bool = True,
        batch_id: str = BATCH,
    ) -> tuple[str, str | None]:
        """构造 职位→in_progress 未读→可处理未读 三步场景。"""
        job_manifest, _job_key = helpers.make_job_fixture(self.root, f"{name}-job")
        if seed:
            _manifest, key = self._seed_discovery(f"{name}-unread")
        else:
            _manifest, key = helpers.make_unread_fixture(self.root, f"{name}-unread")
        conversation = f"tests/fixtures/runtime/read-{name}-unread/conversation.json"
        steps = [
            self._step(read_fixture=job_manifest, job_fixture=job_manifest),
            self._step(
                read_fixture=_manifest,
                conversation_fixture=conversation,
                in_progress=True,
            ),
            self._step(
                read_fixture=_manifest,
                conversation_fixture=conversation,
                decision=helpers.decision_stimulus(),
            ),
        ]
        return self._scenario(name, steps, batch_id=batch_id), key

    def test_first_observed_uses_real_read_completion_time(self) -> None:
        """首次观察取真实采集完成时刻；每次未读采集 250ms 时等待只从 10250 起算。"""
        job_manifest, _job = helpers.make_job_fixture(self.root, "obs-job")
        unread_manifest, _key = self._seed_discovery("obs-unread")
        conversation = "tests/fixtures/runtime/read-obs-unread/conversation.json"
        rel = self._scenario(
            "obs-timing",
            [
                self._step(read_fixture=job_manifest, job_fixture=job_manifest),
                self._step(
                    read_fixture=unread_manifest,
                    conversation_fixture=conversation,
                    in_progress=True,
                ),
                self._step(
                    read_fixture=unread_manifest,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                ),
            ],
        )
        clock = _Clock()
        read = _AdvancingRead(self.root, clock, unread_manifest, 250)
        seen = {"checkpoint": 0}

        def boundary(name: str) -> None:
            """job checkpoint 后推进到 10000；采集耗时由读取端口真实推进。"""
            if name == "after_checkpoint" and seen["checkpoint"] == 0:
                seen["checkpoint"] = 1
                clock.advance(10_000)

        run = self._execute(rel, clock=clock, read=read, on_boundary=boundary)
        self.assertEqual(len(run["preemptions"]), 1)
        event = run["preemptions"][0]
        self.assertEqual(event["first_observed_at"], clock.wall_at(10_250))
        self.assertEqual(event["handling_started_at"], clock.wall_at(10_500))
        self.assertEqual(event["waited_ms"], 250)
        self.assertEqual(event["detection_ms"], 250)
        self.assertEqual(
            self._cursor()["first_observed_at"],  # type: ignore[index]
            clock.wall_at(10_250),
        )

    def test_job_selection_consumes_persisted_cursor_and_skips_visited(self) -> None:
        """两卡列表连续刷新用真实游标选择未访问职位；重排后不重复已 done。"""
        manifest_ab, key_a, key_b = self._two_job_list(
            "cursor-ab", "cursor-a", "cursor-b"
        )
        manifest_ba, _swap_b, _swap_a = self._two_job_list(
            "cursor-ba", "cursor-b", "cursor-a"
        )
        rel = self._scenario(
            "cursor-order",
            [
                self._step(read_fixture=manifest_ab),
                self._step(read_fixture=manifest_ab),
                self._step(read_fixture=manifest_ba),
            ],
            batch_id="00000000-0000-4000-8000-0000000000c5",
        )
        run = self._execute(rel, clock=_Clock())
        self.assertEqual(run["pause_reason"], None)
        self.assertEqual(run["processed"], 2)
        self.assertEqual(self._job_state(key_a), "done")
        self.assertEqual(self._job_state(key_b), "done")
        cursor = self._cursor("00000000-0000-4000-8000-0000000000c5")
        self.assertEqual(
            set(cursor["visited_keys"]),  # type: ignore[index]
            {key_a, key_b},
        )

    def test_job_finish_uses_current_wall_after_slow_detail_read(self) -> None:
        """职位详情读取耗时 1001ms 后，finish 用当前墙钟收敛为 done，不报 clock_changed。"""
        job, key = helpers.make_job_fixture(self.root, "slowjob")
        slow = self._slow_copy(job, "slowjob-copy")
        rel = self._scenario(
            "slow-job",
            [self._step(read_fixture=job, job_fixture=slow)],
        )
        clock = _Clock()
        read = _AdvancingRead(self.root, clock, slow, 1_001)
        run = self._execute(
            rel,
            clock=clock,
            read=read,
            store_monotonic=lambda: clock.mono() / 1000.0,
        )
        self.assertEqual(run["pause_reason"], None)
        self.assertEqual(run["processed"], 1)
        self.assertEqual(self._job_state(key), "done")

    def test_job_renew_and_finish_use_current_wall_after_20s_read(self) -> None:
        """详情读取跨 20 秒时用当前墙钟 renew 并以最新 Lease finish，终态后零续租。"""
        for elapsed in (20_000, 21_000):
            label = f"renew{elapsed}"
            job, key = helpers.make_job_fixture(self.root, label)
            slow = self._slow_copy(job, f"{label}-copy")
            rel = self._scenario(
                f"renew-{label}",
                [self._step(read_fixture=job, job_fixture=slow)],
                batch_id=f"00000000-0000-4000-8000-{elapsed:012d}",
            )
            clock = _Clock()
            read = _AdvancingRead(self.root, clock, slow, elapsed)
            before = self._task_events().count("lease_renewed")
            run = self._execute(rel, clock=clock, read=read)
            self.assertEqual(run["pause_reason"], None, label)
            self.assertEqual(run["processed"], 1, label)
            self.assertEqual(self._job_state(key), "done", label)
            self.assertEqual(
                self._task_events().count("lease_renewed") - before, 1, label
            )

    def test_decision_finish_uses_current_wall_after_slow_bridge(self) -> None:
        """唯一 decide 的桥 transport 耗时 25 秒后，完成仍用当前墙钟收敛为 done。"""
        manifest, key = self._seed_discovery("slowdecide")
        conversation = "tests/fixtures/runtime/read-slowdecide/conversation.json"
        rel = self._scenario(
            "slow-decide",
            [
                self._step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
        )
        clock = _Clock()

        def factory(stimulus: object) -> _AdvancingBridge:
            """包裹真实合成 bridge，只在 transport 调用期间推进可信时钟。"""
            inner = SyntheticDecisionBridge(
                stimulus,  # type: ignore[arg-type]
                provider_id=self.provider,
                model_id=self.model_id,
                model_config_hash=self.config_hash,
            )
            return _AdvancingBridge(inner, clock, 25_000)

        run = self._execute(rel, clock=clock, bridge_factory=factory)
        self.assertEqual(run["pause_reason"], None)
        self.assertEqual(run["processed"], 1)
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(self._discovery_state(key), "done")

    def _job_state(self, key: str) -> str:
        """只读返回职位任务状态。"""
        connection = sqlite3.connect(str(self.path))
        try:
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        return str(row[0])

    def _discovery_state(self, key: str) -> str:
        """只读返回会话发现的终态。"""
        connection = sqlite3.connect(str(self.path))
        try:
            row = connection.execute(
                "SELECT state FROM hr_discoveries WHERE conversation_key = ?",
                (key,),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        return str(row[0])

    def test_nonce_change_between_start_and_next_command_blocks_read(self) -> None:
        """真实 start 后任务 nonce 被外部改变时，下一条详情采集命令零启动。"""
        job, key = helpers.make_job_fixture(self.root, "nonce")
        slow = self._slow_copy(job, "nonce-copy")
        rel = self._scenario(
            "nonce-command",
            [self._step(read_fixture=job, job_fixture=slow)],
        )
        read = _CountingRead(self.root)

        class NonceChangingStore:
            """委托真实 SqliteStore，仅在该测试的 start 后改变持久 nonce。"""

            def __init__(self, inner: object) -> None:
                """保存真实仓储。"""
                self._inner = inner

            def __getattr__(self, name: str):
                """其余能力完全委托真实仓储。"""
                return getattr(self._inner, name)

            def start_job(self, lease: Lease, now: str) -> Lease:
                """真实 start 成功后模拟另一进程改变任务 nonce。"""
                result = self._inner.start_job(lease, now)  # type: ignore[attr-defined]
                connection = self._inner._connection  # type: ignore[attr-defined]
                connection.execute(
                    "UPDATE jobs SET nonce = ? WHERE job_key = ?",
                    (str(uuid.uuid4()), lease.key),
                )
                connection.commit()
                return result

        run = self._execute(
            rel,
            clock=_Clock(),
            read=read,
            store_hook=lambda inner: NonceChangingStore(inner),
        )
        self.assertEqual(run["pause_reason"], "precondition_failed")
        self.assertEqual(read.counts.get(slow, 0), 0)
        self.assertEqual(self._job_state(key), "running")

    def test_after_checkpoint_observes_settled_elapsed_and_command_counts(self) -> None:
        """任务完成后先结算耗时/命令再提交检查点，after_checkpoint 读到已充电游标。"""
        job, _key = helpers.make_job_fixture(self.root, "settle")
        slow = self._slow_copy(job, "settle-copy")
        rel = self._scenario(
            "settle-checkpoint",
            [self._step(read_fixture=job, job_fixture=slow)],
        )
        clock = _Clock()
        read = _AdvancingRead(self.root, clock, slow, 21_000)
        seen: dict[str, object] = {}

        def boundary(name: str) -> None:
            """在真实 checkpoint 提交后经独立连接读取持久游标。"""
            if name != "after_checkpoint":
                return
            connection = sqlite3.connect(str(self.path))
            try:
                row = connection.execute(
                    "SELECT cursor_json FROM job_batches WHERE batch_id = ?",
                    (BATCH,),
                ).fetchone()
            finally:
                connection.close()
            seen.update(json.loads(row[0]))

        run = self._execute(
            rel,
            clock=clock,
            read=read,
            on_boundary=boundary,
            store_monotonic=lambda: clock.mono() / 1000.0,
        )
        self.assertIsNone(run["pause_reason"])
        self.assertEqual(seen["remaining_budget"]["seconds"], 879)  # type: ignore[index]
        self.assertEqual(seen["remaining_budget"]["device_calls"], 198)  # type: ignore[index]
        self.assertEqual(
            run["remaining_budget"]["seconds"], 879  # type: ignore[index]
        )
        self.assertEqual(
            run["remaining_budget"]["device_calls"], 198  # type: ignore[index]
        )

    def test_s01_actual_start_samples_after_detection_gap(self) -> None:
        """安全点 20020、真实开始 20200，wait 10200；安全点前零切换。"""
        rel, _key = self._job_then_unread("s01")
        clock = _Clock()
        trace: list[tuple[str, int]] = []
        seen = {"job_checkpoint": 0, "preempt_safe": 0, "advanced": False}

        def boundary(name: str) -> None:
            """只在固定生产边界推进可信时钟并记录原始轨迹。"""
            if name == "after_checkpoint" and not seen["advanced"]:
                seen["job_checkpoint"] += 1
                clock.advance(10_000)
                trace.append((name, clock.mono()))
            elif name == "after_refresh" and clock.mono() == 10_000:
                clock.advance(10_020)
                trace.append((name, clock.mono()))
            elif name == "before_safe_point":
                seen["preempt_safe"] += 1
                if clock.mono() == 20_020:
                    self.assertEqual(
                        len([e for e in self._events() if e[0] == "stage_preempted"]),
                        0,
                    )
                    trace.append((name, clock.mono()))
            elif name == "before_claim" and clock.mono() == 20_020:
                seen["advanced"] = True
                clock.advance(180)
                trace.append((name, clock.mono()))

        run = self._execute(rel, clock=clock, on_boundary=boundary)
        self.assertEqual(run["pause_reason"], None)
        self.assertEqual(run["processed"], 2)
        self.assertEqual(len(run["preemptions"]), 1)
        event = run["preemptions"][0]
        self.assertEqual(event["first_observed_at"], clock.wall_at(10_000))
        self.assertEqual(event["handling_started_at"], clock.wall_at(20_200))
        self.assertEqual(event["waited_ms"], 10_200)
        self.assertEqual(event["detection_ms"], 10_020)
        start = [item for item in trace if item[0] == "before_claim"]
        self.assertEqual(start, [("before_claim", 20_200)])
        safe = [item for item in trace if item[0] == "before_safe_point"]
        self.assertEqual(safe, [("before_safe_point", 20_020)])

    def test_no_start_means_no_preemption_event_or_count(self) -> None:
        """未 seed 目标导致 claim 良性失败时零 stage_preempted、零抢占扣减。"""
        rel, _key = self._job_then_unread("nostart", seed=False)
        clock = _Clock()

        def boundary(name: str) -> None:
            """与正例相同的检测/切页耗时，但不能伪造任务开始。"""
            if name == "after_checkpoint" and clock.mono() == 0:
                clock.advance(10_000)
            elif name == "after_refresh" and clock.mono() == 10_000:
                clock.advance(10_020)
            elif name == "before_claim" and clock.mono() == 20_020:
                clock.advance(180)

        run = self._execute(rel, clock=clock, on_boundary=boundary)
        self.assertEqual(run["preemptions"], [])
        self.assertEqual(run["preemptions_used"], 0)
        self.assertFalse(
            [item for item in self._events() if item[0] == "stage_preempted"]
        )
        self.assertEqual(run["processed"], 1)

    def test_clock_drift_at_before_claim_blocks_new_task(self) -> None:
        """安全点后只推进单调钟造成漂移时拒绝 claim 且零 stage_preempted。"""
        rel, _key = self._job_then_unread("drift")
        clock = _Clock()
        seen = {"advanced": False}

        def boundary(name: str) -> None:
            """按真实窗口推进；在安全点后仅推进单调钟制造漂移。"""
            if name == "after_checkpoint" and not seen["advanced"]:
                clock.advance(10_000)
            elif name == "after_refresh" and clock.mono() == 10_000:
                clock.advance(10_020)
            elif name == "before_claim" and clock.mono() == 20_020:
                seen["advanced"] = True
                clock.advance_mono_only(5_000)

        run = self._execute(
            rel,
            clock=clock,
            on_boundary=boundary,
            store_monotonic=lambda: clock.mono() / 1000.0,
        )
        self.assertEqual(run["pause_reason"], "clock_changed")
        self.assertEqual(run["preemptions"], [])
        self.assertFalse(
            [item for item in self._events() if item[0] == "stage_preempted"]
        )
        self.assertEqual(run["processed"], 1)
        connection = sqlite3.connect(str(self.path))
        try:
            state, reason, cursor_json = connection.execute(
                "SELECT state, pause_reason, cursor_json FROM job_batches "
                "WHERE batch_id = ?",
                (BATCH,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(state, "paused")
        self.assertEqual(reason, "clock_changed")
        persisted = json.loads(cursor_json)
        self.assertEqual(
            persisted["remaining_budget"]["seconds"],
            run["remaining_budget"]["seconds"],
        )

    def test_budget_exhaustion_at_before_claim_blocks_start(self) -> None:
        """安全点后新增耗时耗尽本轮预算时不再 claim，也不写抢占。"""
        first, _ = helpers.make_job_fixture(self.root, "bfirst")
        second, _ = helpers.make_job_fixture(self.root, "bsecond")
        rel = self._scenario(
            "budget-before-claim",
            [
                self._step(read_fixture=first, job_fixture=first),
                self._step(read_fixture=second, job_fixture=second),
            ],
        )
        clock = _Clock()
        seen = {"claims": 0}

        def boundary(name: str) -> None:
            """第二次 claim 前消耗 3 秒，令等号预算阻止开始。"""
            if name == "before_claim":
                seen["claims"] += 1
                if seen["claims"] == 2:
                    clock.advance(3_000)

        run = self._execute(
            rel, clock=clock, on_boundary=boundary, limits=BudgetLimits(round_seconds=3)
        )
        self.assertEqual(run["pause_reason"], "budget_exhausted")
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["remaining_budget"]["seconds"], 0)  # type: ignore[index]

    def test_s08_three_windows_separate_arrival_observation_and_start(self) -> None:
        """S08：安全点前已观察=0；checkpoint 后到达由下次真实读取记 1000；refresh 后到达不强造观察。"""
        # 窗口 1：安全点前已经观察，first_observed 为 0。
        batch1 = "00000000-0000-4000-8000-0000000000b1"
        rel1, _ = self._job_then_unread("s08a", batch_id=batch1)
        clock1 = _Clock()
        trace1 = {"after_checkpoint": 0, "claims": 0}

        def boundary1(name: str) -> None:
            """记录 checkpoint 后外部到达；只在第二次 claim 前推进开始耗时。"""
            if name == "after_checkpoint" and trace1["after_checkpoint"] == 0:
                trace1["after_checkpoint"] = 1
            elif name == "before_claim":
                trace1["claims"] += 1
                if trace1["claims"] == 2:
                    clock1.advance(5_000)

        run1 = self._execute(rel1, clock=clock1, on_boundary=boundary1)
        self.assertEqual(len(run1["preemptions"]), 1)
        event1 = run1["preemptions"][0]
        self.assertEqual(event1["first_observed_at"], clock1.wall_at(0))
        self.assertEqual(event1["handling_started_at"], clock1.wall_at(5_000))
        self.assertEqual(event1["waited_ms"], 5_000)
        self.assertEqual(self._cursor(batch1)["first_observed_at"], clock1.wall_at(0))  # type: ignore[index]

        # 窗口 2：checkpoint 后 arrival=900，下一次真实 refresh=1000 才观察。
        batch2 = "00000000-0000-4000-8000-0000000000b2"
        unread2, _key2 = self._seed_discovery("s08b-unread")
        conversation2 = "tests/fixtures/runtime/read-s08b-unread/conversation.json"
        job2, _ = helpers.make_job_fixture(self.root, "s08b-job")
        rel2 = self._scenario(
            "s08b",
            [
                self._step(read_fixture=job2, job_fixture=job2),
                self._step(read_fixture="switch:unread", in_progress=True),
                self._step(
                    read_fixture="switch:unread",
                    conversation_fixture=conversation2,
                    decision=helpers.decision_stimulus(),
                ),
            ],
            batch_id=batch2,
        )
        read2 = _SwitchRead(self.root, "tests/fixtures/read/page.json", unread2)
        clock2 = _Clock()
        trace2 = {"arrival": 900, "seen": 0}

        def boundary2(name: str) -> None:
            """checkpoint 后推进到 900；refresh 后到达才让原始未读可见并推进到 1000。"""
            if name == "after_checkpoint" and trace2["seen"] == 0:
                trace2["seen"] = 1
                self.assertLess(trace2["arrival"], 1_000)
                clock2.advance(900)
            elif name == "after_refresh" and clock2.mono() == 900:
                read2.state["visible"] = True
                clock2.advance(100)
            elif name == "before_claim" and clock2.mono() == 1_000:
                clock2.advance(200)

        run2 = self._execute(
            rel2, clock=clock2, read=read2, on_boundary=boundary2
        )
        self.assertEqual(len(run2["preemptions"]), 1)
        event2 = run2["preemptions"][0]
        self.assertEqual(event2["first_observed_at"], clock2.wall_at(1_000))
        self.assertEqual(event2["handling_started_at"], clock2.wall_at(1_200))
        self.assertEqual(event2["waited_ms"], 200)
        self.assertEqual(trace2["arrival"], 900)
        self.assertEqual(self._cursor(batch2)["first_observed_at"], clock2.wall_at(1_000))  # type: ignore[index]

        # 窗口 3：refresh 后 arrival=1900，本次 2000 读取未再观察，保持 null。
        batch3 = "00000000-0000-4000-8000-0000000000b3"
        unread3, _key3 = self._seed_discovery("s08c-unread")
        job3, _ = helpers.make_job_fixture(self.root, "s08c-job")
        read3 = _SwitchRead(self.root, "tests/fixtures/read/page.json", unread3)
        rel3 = self._scenario(
            "s08c",
            [
                self._step(read_fixture=job3, job_fixture=job3),
                self._step(read_fixture="switch:unread"),
            ],
            batch_id=batch3,
        )
        clock3 = _Clock()
        trace3: dict[str, int] = {"arrival": 0, "seen": 0}

        def boundary3(name: str) -> None:
            """checkpoint 后推进到 2000；refresh 结束后才让原始未读可见。"""
            if name == "after_checkpoint" and trace3["seen"] == 0:
                trace3["seen"] = 1
                clock3.advance(2_000)
            elif name == "after_refresh" and clock3.mono() == 2_000:
                read3.state["visible"] = True
                trace3["arrival"] = 1_900

        run3 = self._execute(
            rel3, clock=clock3, read=read3, on_boundary=boundary3
        )
        self.assertEqual(run3["preemptions"], [])
        self.assertFalse(
            [item for item in self._events(batch3) if item[0] == "stage_preempted"]
        )
        self.assertEqual(trace3["arrival"], 1_900)
        self.assertIsNone(self._cursor(batch3)["first_observed_at"])  # type: ignore[index]
        # 到达后原始外界确实已切换为可见未读；只是本次结束后没有再读取。
        self.assertTrue(read3.state["visible"])
        visible3 = read3.read("switch:unread")
        self.assertEqual(visible3.status, "ok")
        self.assertEqual(visible3.value.unread_state, "present")


if __name__ == "__main__":
    unittest.main()
