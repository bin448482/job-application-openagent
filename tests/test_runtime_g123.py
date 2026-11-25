"""验证 M6 G1 安全点真实来源、G2 实际一次定向采集与 G3 开始/到达边界。

全部使用临时项目根、临时 OS 门禁根与真实 SQLite profile/仓储；外部 fake 只
替代 M3 原始输入引用与环境时钟，不直接返回 ReadResult、候选、安全点或终态。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
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
from app.domain.runtime import BatchCursor, BudgetLimits
from app.ports.runtime import RawScene, RuntimeCommandControlError
from app.ports.store import StoreError
from app.process_log import ProcessLog
from app.usecases import policy as policy_usecases
from app.usecases.policy_eval import RepositorySnapshot
from app.usecases.runtime import (
    _LeaseCommandContext,
    _safe_scene,
    derive_safe_point,
    execute_run,
)
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


class _SecondConversationRead(FixtureM3Read):
    """第二次读取同一会话引用前改写真实原始 manifest/profile 的测试刺激。"""

    def __init__(self, project_root: Path, target: str, mutate) -> None:
        """保存真实解析根、目标会话引用与第二次读取前的原始输入改写。"""
        super().__init__(project_root)
        self._target = target
        self._mutate = mutate
        self._conversation_reads = 0

    def read(self, relative: str):
        """仅在 post-claim 会话重读前改写原始输入，仍走真实 M3 解析。"""
        if relative == self._target:
            self._conversation_reads += 1
            if self._conversation_reads == 2:
                self._mutate()
        return super().read(relative)


class _ReplayConversationRead(FixtureM3Read):
    """第二次读取同一会话时回放第一次真实结果，模拟重放快照反例。"""

    def __init__(self, project_root: Path, target: str) -> None:
        """保存真实解析根与目标会话引用。"""
        super().__init__(project_root)
        self._target = target
        self._cached = None

    def read(self, relative: str):
        """目标会话第二次读取直接回放第一次 ReadResult。"""
        if relative == self._target:
            if self._cached is not None:
                return self._cached
            self._cached = super().read(relative)
            return self._cached
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
        """为每个用例创建独立临时项目根并准备真实 profile/门禁/验收。"""
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
        """清理本用例独占的临时项目根，不触碰其它运行产物。"""
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
        on_started: object | None = None,
        gate: object | None = None,
    ) -> dict[str, object]:
        """经真实 M3 解析、唯一 decide、真实 profile/gate/仓储运行一次。"""
        active = clock or _Clock()
        spec = json.loads((self.root / rel).read_text(encoding="utf-8"))
        _pack_rel, base_files, _files, _guides, pack = (
            policy_usecases.load_active_pack(
                FilePolicyRepository(self.root), "prompts/local/demo"
            )
        )
        gate = gate or SharedDeviceGate(
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
            on_started=on_started,  # type: ignore[arg-type]
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

    def _job_list(
        self, name: str, labels: tuple[str, ...]
    ) -> tuple[str, list[str]]:
        """生成含多张真实职位卡的 job_list，返回 manifest 与各职位键。"""
        manifests: list[str] = []
        keys: list[str] = []
        for label in labels:
            manifest, key = helpers.make_job_fixture(self.root, label)
            manifests.append(manifest)
            keys.append(key)
        blocks: list[str] = []
        for manifest in manifests:
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
            header + "\n".join(blocks) + "\n" + footer,
            encoding="utf-8",
        )
        return f"tests/fixtures/runtime/{name}/job_list.json", keys

    def _two_job_list(
        self, name: str, label_a: str, label_b: str
    ) -> tuple[str, str, str]:
        """生成含两张真实职位卡的单页 job_list，返回 manifest 与两个职位键。"""
        manifest, keys = self._job_list(name, (label_a, label_b))
        return manifest, keys[0], keys[1]

    def _many_page_conversation(self, name: str, pages: int) -> str:
        """生成引用 pages 份真实会话页 XML 的 conversation manifest。"""
        target = self.root / "tests/fixtures/runtime" / name
        target.mkdir(parents=True, exist_ok=True)
        for item in ("profile.json", "conversation.json"):
            shutil.copyfile(ROOT / "tests/fixtures/read" / item, target / item)
        template = (ROOT / "tests/fixtures/read/conversation-0.xml").read_text(
            encoding="utf-8"
        )
        first = template.index(
            '      <node resource-id="com.hpbr.bosszhipin:id/chat_message_row"'
        )
        second = template.index(
            '      <node resource-id="com.hpbr.bosszhipin:id/chat_message_row"',
            first + 1,
        )
        suffix = template[template.index("    </node>\n  </node>\n</hierarchy>"):]
        prefix = template[:second]
        paths: list[str] = []
        for index in range(pages):
            page = (
                prefix.replace("合成消息甲", f"合成消息{index}")
                .replace("10:00", f"10:{index % 60:02d}")
                .replace("msg-1", f"msg-page-{index}")
                + suffix
            )
            file_name = f"conversation_{index:03d}.xml"
            (target / file_name).write_text(page, encoding="utf-8")
            paths.append(file_name)
        manifest = json.loads((target / "conversation.json").read_text(encoding="utf-8"))
        manifest["hierarchy_paths"] = paths
        manifest["coverage_proof"] = {
            "kind": "full_traversal",
            "top_proven": True,
            "bottom_proven": True,
        }
        (target / "conversation.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return f"tests/fixtures/runtime/{name}/conversation.json"

    def _page_outputs(self, manifest_rel: str) -> list[object]:
        """返回 conversation 每页的真实 XML 文本。"""
        manifest = json.loads((self.root / manifest_rel).read_text(encoding="utf-8"))
        base = (self.root / manifest_rel).parent
        return [
            (base / path).read_text(encoding="utf-8")
            for path in manifest["hierarchy_paths"]
        ]

    def _unread_pixel_manifest(self, name: str) -> tuple[str, bytes]:
        """生成带显式像素文件的未读 manifest，返回 manifest 与截图字节。"""
        rel = self._write_unread_variant(name, observed_at=helpers.NOW)
        target = (self.root / rel).parent
        profile = json.loads((target / "profile.json").read_text(encoding="utf-8"))
        calibration = {
            "calibration_id": "synthetic-calibration",
            "app": "boss",
            "profile_id": str(profile["profile_id"]),
            "version": str(profile["version"]),
            "calibrated": True,
            "absent_state_verified": True,
            "resolution": [100, 100],
            "badge_region": [0.0, 0.0, 0.2, 1.0],
            "target_color": [255, 0, 0],
            "tolerance": 0,
            "threshold_pixels": 1,
            "source": "synthetic",
            "calibrated_on": "2026-11-24",
        }
        (target / "calibration.json").write_text(
            json.dumps(calibration, ensure_ascii=False), encoding="utf-8"
        )
        pixels = bytearray(100 * 100 * 4)
        pixels[(5 * 100 + 5) * 4] = 255
        pixels[(5 * 100 + 5) * 4 + 3] = 255
        raw = struct.pack("<IIII", 100, 100, 1, 0) + bytes(pixels)
        (target / "screen.rgba").write_bytes(raw)
        manifest = json.loads((target / "unread.json").read_text(encoding="utf-8"))
        manifest["pixel"] = {
            "calibration_path": "calibration.json",
            "raw_path": "screen.rgba",
        }
        (target / "unread.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return rel, raw

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

    def test_complete_job_detail_counts_as_compatible_list(self) -> None:
        """冻结公开 M6 合同下，完整可行动职位详情按旧语义计入 list_returned。"""
        detail = "tests/fixtures/read/job_detail.json"
        step = self._step(read_fixture=detail, job_fixture=detail)
        rel = self._scenario("g1-detail", [step])
        run = self._execute(rel)
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["pause_reason"], None)

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

    def test_legacy_safe_scene_projects_detail_and_unknown_pages(self) -> None:
        """兼容七布尔入口：可行动 JobDetail 算 list，unknown 页面不算。"""
        store = self._store()
        try:
            batch = store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            store.save_checkpoint(BATCH, batch["cursor"], now=helpers.NOW)
            detail = load_and_read_fixture(
                ROOT / "tests/fixtures/read/job_detail.json",
                "00000000-0000-4000-8000-0000000000ad",
            )
            safe_detail = _safe_scene(
                store=store,
                batch_id=BATCH,
                task_key="",
                read_results=(detail,),
                raw=RawScene("", None, 0),
            )
            self.assertEqual(len(safe_detail), 7)
            self.assertTrue(safe_detail[0])
            self.assertTrue(all(safe_detail[1:]))
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
                snapshot_id="00000000-0000-4000-8000-0000000000ae",
                observed_at=helpers.NOW,
                source="fixture",
                confidence="low",
            )
            safe_unknown = _safe_scene(
                store=store,
                batch_id=BATCH,
                task_key="",
                read_results=(unknown,),
                raw=RawScene("", None, 0),
            )
            self.assertFalse(safe_unknown[0])
            self.assertTrue(all(safe_unknown[1:]))
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
        self.assertEqual(run["remaining_budget"]["device_calls"], 196)  # type: ignore[index]
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


class S06CommandBudgetMatrixTests(_RuntimeCase):
    """S06：真实 execute_run→AdbRuntimeRead→run_adb.on_spawn 的 199/200/201/250。"""

    class _Gate:
        """ScreenPort 共享门禁替身。"""

        def require_available(self) -> None:
            """始终可用。"""
            return

        def verify(self) -> None:
            """命令前复核无副作用。"""
            return

        def release(self) -> None:
            """无占用。"""
            return

    class _FakePopen:
        """模拟一次真实创建命令，不启动外部进程。"""

        def __init__(self, output: object) -> None:
            """保存固定输出。"""
            self.pid = 6000 + id(output) % 1000
            self.returncode = 0
            self._output = output
            self.killed = False

        def communicate(self, timeout=None):
            """返回固定输出。"""
            del timeout
            empty = b"" if isinstance(self._output, bytes) else ""
            return (self._output, empty)

        def kill(self) -> None:
            """记录清理。"""
            self.killed = True

    def _run_case(self, total: int, pages: int, *, unread: bool) -> dict[str, object]:
        """按 total 实际命令构造一次含 post-claim 重读的真实 execute_run。"""
        batch = f"00000000-0000-4000-8000-{total:012d}"
        conversation = self._many_page_conversation(f"s06-{total}", pages)
        conv_outputs = self._page_outputs(conversation)
        if unread:
            primary, raw = self._unread_pixel_manifest("甲")
            key = helpers.m3_unread_key()
            helpers.seed_discovery(self.root, key, unread_count=1)
            step = self._step(
                read_fixture=primary,
                conversation_fixture=conversation,
                decision=helpers.decision_stimulus(),
            )
            outputs: list[object] = ["dumped", (self.root / primary).parent.joinpath("unread.xml").read_text(encoding="utf-8"), raw]
        else:
            helpers.seed_new_message(self.root, f"s06-{total}")
            primary = "tests/fixtures/read/page.json"
            step = self._step(
                read_fixture=primary,
                conversation_fixture=conversation,
                decision=helpers.decision_stimulus(),
            )
            outputs = ["dumped", (ROOT / "tests/fixtures/read/page.xml").read_text(encoding="utf-8")]
        for xml in conv_outputs:
            outputs.extend(["dumped", xml])
        # 决策前当前会话重读：同一 conversation fixture 经同一真实采集/解析再走一次。
        for xml in conv_outputs:
            outputs.extend(["dumped", xml])
        rel = self._scenario(f"s06-{total}", [step], batch_id=batch)
        adapter = AdbRuntimeRead(
            self.root,
            AdbScreenReader(None, gate=self._Gate()),
            "synthetic-serial",
        )
        fake = [self._FakePopen(item) for item in outputs]
        with patch(
            "app.adapters.adb.command.subprocess.Popen", side_effect=fake
        ):
            return self._execute(
                rel,
                clock=_Clock(),
                read=adapter,
                store_monotonic=lambda: 0.0,
            )

    def test_199_commands_allow_one_task(self) -> None:
        """真实 199 次命令（含 post-claim 重读）后仍可开始一条任务，预算余 1。"""
        run = self._run_case(199, 49, unread=True)
        self.assertEqual(run["processed"], 1)
        self.assertIsNone(run["pause_reason"])
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(run["remaining_budget"]["device_calls"], 1)  # type: ignore[index]

    def test_200_201_250_commands_pause_and_preserve_budget(self) -> None:
        """200 等号、201 与 250 在途超支均暂停且保留真实剩余/负值。"""
        cases = ((200, 99, False, 0), (201, 99, True, -1), (250, 124, False, -50))
        for total, pages, unread, remaining in cases:
            run = self._run_case(total, pages, unread=unread)
            self.assertEqual(run["processed"], 0, total)
            self.assertEqual(run["pause_reason"], "budget_exhausted", total)
            self.assertEqual(
                run["remaining_budget"]["device_calls"], remaining, total  # type: ignore[index]
            )

class SafetyStopTests(_RuntimeCase):
    """双钟安全停止：真实 profile/batch 校验、预算保存与故障回滚。"""

    def test_safety_stop_validates_batch_cursor_and_reason(self) -> None:
        """错误 batch/cursor/原因拒绝，合法调用保存暂停与四维预算。"""
        store = self._store()
        try:
            store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            cursor = store.load_batch(BATCH)["cursor"]
            self.assertIsInstance(cursor, BatchCursor)
            cursor = replace(cursor, budget=replace(cursor.budget, remaining_seconds=875))
            with self.assertRaises(StoreError):
                store.safety_stop_after_clock_change(
                    "00000000-0000-4000-8000-000000000099",
                    cursor,
                    "clock_changed",
                    now=helpers.NOW,
                )
            with self.assertRaises(StoreError):
                store.safety_stop_after_clock_change(
                    BATCH, cursor, "budget_exhausted", now=helpers.NOW
                )
            store.safety_stop_after_clock_change(
                BATCH, cursor, "clock_changed", now=helpers.NOW
            )
            batch = store.load_batch(BATCH)
            self.assertEqual(batch["state"], "paused")
            self.assertEqual(batch["pause_reason"], "clock_changed")
            self.assertEqual(batch["cursor"].budget.remaining_seconds, 875)
        finally:
            store.close()

    def test_safety_stop_event_failure_rolls_back(self) -> None:
        """事件写入故障回滚暂停状态，不伪造已保存。"""
        def boom(name: str, connection: object) -> None:
            """只在安全停止事件边界注入故障。"""
            del connection
            if name == "safety_stop_before_event":
                raise RuntimeError("synthetic safety stop fault")

        store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.owner.host,
            fault_hook=boom,
            require_runtime=True,
        )
        try:
            store.ensure_batch(BATCH, BudgetLimits(), helpers.NOW)
            cursor = store.load_batch(BATCH)["cursor"]
            with self.assertRaises(StoreError):
                store.safety_stop_after_clock_change(
                    BATCH, cursor, "clock_changed", now=helpers.NOW
                )
            batch = store.load_batch(BATCH)
            self.assertEqual(batch["state"], "active")
            self.assertIsNone(batch["pause_reason"])
        finally:
            store.close()


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
        conversation = "tests/fixtures/runtime/read-s01-unread/conversation.json"
        clock = _Clock()
        trace: list[tuple[str, int]] = []
        seen = {"job_checkpoint": 0, "preempt_safe": 0, "advanced": False}

        def refresh_observation() -> None:
            """第二次真实解析前把会话原始 observed_at 更新为当前可信墙钟。"""
            path = self.root / conversation
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["observed_at"] = clock.now()
            path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

        read = _SecondConversationRead(self.root, conversation, refresh_observation)

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

        run = self._execute(rel, clock=clock, read=read, on_boundary=boundary)
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
        self.assertEqual(run["remaining_budget"]["seconds"], 875)  # type: ignore[index]
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


class _ToggleGate:
    """包裹真实共享门禁，允许测试在真实 start 后切换为失败，仍保持真实占用。"""

    def __init__(self, inner: object) -> None:
        """保存真实门禁并初始化可用状态。"""
        self._inner = inner
        self.fail = False

    def __getattr__(self, name: str):
        """其余能力完全委托真实门禁。"""
        return getattr(self._inner, name)

    def verify(self, now: str | None = None):
        """按开关拒绝命令前复核；否则委托真实门禁。"""
        if self.fail:
            raise RuntimeError("synthetic gate changed")
        if now is None:
            return self._inner.verify()  # type: ignore[attr-defined]
        return self._inner.verify(now=now)  # type: ignore[attr-defined]


class _FakeSpawn:
    """最低外部进程替身：不启动真实进程，可独立推进可信时钟。"""

    def __init__(self, output: object, clock=None, ms: int = 0) -> None:
        """保存固定输出、可选时钟与单条命令耗时。"""
        self.pid = 8000 + id(output) % 1000
        self.returncode = 0
        self._output = output
        self._clock = clock
        self._ms = ms
        self.killed = False

    def communicate(self, timeout=None):
        """返回固定输出；仅在显式注入时钟时推进命令耗时。"""
        del timeout
        if self._clock is not None and self._ms:
            self._clock.advance(self._ms)
        empty = b"" if isinstance(self._output, bytes) else ""
        return (self._output, empty)

    def kill(self) -> None:
        """记录清理。"""
        self.killed = True


class PostClaimConversationRejectionTests(_RuntimeCase):
    """A：post-claim 重读只用当前新鲜会话证据，异常原始输入 zero bridge。"""

    def _base_ms(self) -> int:
        """返回固定合成基准墙钟的毫秒值。"""
        return int(parse_utc(helpers.NOW).timestamp() * 1000)

    def _mutate_manifest(self, rel: str, **fields: object) -> None:
        """改写真实会话 manifest 的合成字段，保留其余原始输入。"""
        path = self.root / rel
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update(fields)
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def _mutate_profile(self, rel: str, profile_id: str) -> None:
        """只改会话 fixture 的 selector profile_id，模拟采集上下文变化。"""
        path = self.root / rel
        manifest = json.loads(path.read_text(encoding="utf-8"))
        profile_path = path.parent / str(manifest["profile_path"])
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["profile_id"] = profile_id
        profile_path.write_text(
            json.dumps(profile, ensure_ascii=False), encoding="utf-8"
        )

    def _mutate_identity(self, rel: str) -> None:
        """把会话原始标题与上下文改成另一合成身份，形成错会话键刺激。"""
        path = self.root / rel
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["conversation_context"]["contact"] = "合成联系人乙"
        for page in manifest["hierarchy_paths"]:
            page_path = path.parent / page
            text = page_path.read_text(encoding="utf-8").replace(
                "合成联系人甲", "合成联系人乙"
            )
            page_path.write_text(text, encoding="utf-8")
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def _mutate_stable_identity(self, rel: str) -> None:
        """关闭会话稳定消息身份验证，形成无稳定入站身份刺激。"""
        path = self.root / rel
        manifest = json.loads(path.read_text(encoding="utf-8"))
        profile_path = path.parent / str(manifest["profile_path"])
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["selectors"]["conversation_stable_id_verified"] = False
        profile_path.write_text(
            json.dumps(profile, ensure_ascii=False), encoding="utf-8"
        )

    def _run_rejected(
        self, name: str, mutate=None, *, replay: bool = False
    ) -> None:
        """第二次会话原始读取异常时必须安全阻塞且零模型桥。"""
        manifest, key = self._seed_discovery(name)
        conversation = f"tests/fixtures/runtime/read-{name}/conversation.json"
        rel = self._scenario(
            name,
            [
                self._step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
            batch_id=f"00000000-0000-4000-8000-{abs(hash(name)) % 10**12:012d}",
        )
        calls = {"bridge": 0}

        def factory(stimulus: object) -> SyntheticDecisionBridge:
            """记录任何被允许启动的合成桥调用。"""
            calls["bridge"] += 1
            return SyntheticDecisionBridge(
                stimulus,  # type: ignore[arg-type]
                provider_id=self.provider,
                model_id=self.model_id,
                model_config_hash=self.config_hash,
            )

        if replay:
            read = _ReplayConversationRead(self.root, conversation)
        else:
            read = _SecondConversationRead(self.root, conversation, mutate)
        run = self._execute(rel, read=read, bridge_factory=factory)
        self.assertEqual(run["pause_reason"], "unread_unverified", run)
        self.assertEqual(run["drafts"], [], run)
        self.assertEqual(calls["bridge"], 0, run)
        self.assertEqual(self._discovery_state(key), "blocked")

    def _post_close_state(
        self, key: str, batch_id: str
    ) -> dict[str, object]:
        """运行关闭后以新只读连接核对草稿/决策记忆/发现/预算持久结果。"""
        uri = self.path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            drafts = connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE profile_id = ?", ("demo",)
            ).fetchone()[0]
            decisions = connection.execute(
                "SELECT COUNT(*) FROM runtime_decisions WHERE profile_id = ?",
                ("demo",),
            ).fetchone()[0]
            row = connection.execute(
                "SELECT state FROM hr_discoveries WHERE conversation_key = ?",
                (key,),
            ).fetchone()
            cursor_row = connection.execute(
                "SELECT cursor_json FROM job_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        finally:
            connection.close()
        return {
            "drafts": int(drafts),
            "runtime_decisions": int(decisions),
            "discovery_state": None if row is None else str(row[0]),
            "budget": None
            if cursor_row is None
            else json.loads(cursor_row[0])["remaining_budget"],
        }

    def _run_same_observed(
        self, name: str, advance_ms: int, *, batch_id: str
    ) -> tuple[dict[str, object], str, int]:
        """第二次真实解析同 observed_at 的新快照，仅推进可信双时钟后运行。"""
        manifest, key = self._seed_discovery(name)
        conversation = f"tests/fixtures/runtime/read-{name}/conversation.json"
        rel = self._scenario(
            name,
            [
                self._step(
                    read_fixture=manifest,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
            batch_id=batch_id,
        )
        clock = _Clock()
        calls = {"bridge": 0}

        def factory(stimulus: object) -> SyntheticDecisionBridge:
            """记录任何被允许启动的合成桥调用。"""
            calls["bridge"] += 1
            return SyntheticDecisionBridge(
                stimulus,  # type: ignore[arg-type]
                provider_id=self.provider,
                model_id=self.model_id,
                model_config_hash=self.config_hash,
            )

        read = _SecondConversationRead(
            self.root, conversation, lambda: clock.advance(advance_ms)
        )
        run = self._execute(rel, clock=clock, read=read, bridge_factory=factory)
        return run, key, calls["bridge"]

    def test_same_observed_after_six_seconds_is_rejected_without_bridge(self) -> None:
        """同一 observed_at 的新快照在外部推进 6 秒后仍须按年龄阻塞且零桥。"""
        batch = "00000000-0000-4000-8000-0000000000c1"
        run, key, bridge = self._run_same_observed(
            "same6000", 6_000, batch_id=batch
        )
        self.assertEqual(run["pause_reason"], "unread_unverified", run)
        self.assertEqual(run["drafts"], [], run)
        self.assertEqual(run["model_calls"], 0, run)
        self.assertEqual(bridge, 0, run)
        state = self._post_close_state(key, batch)
        self.assertEqual(state["drafts"], 0, state)
        self.assertEqual(state["runtime_decisions"], 0, state)
        self.assertEqual(state["discovery_state"], "blocked", state)
        self.assertEqual(state["budget"]["seconds"], 894, state)  # type: ignore[index]
        self.assertEqual(
            run["remaining_budget"]["seconds"], 894, run  # type: ignore[index]
        )

    def test_same_observed_at_zero_offset_still_decides_once(self) -> None:
        """同 observed_at 的新快照在 0ms 偏移下仍满足年龄约束并完成一次决策。"""
        batch = "00000000-0000-4000-8000-0000000000c2"
        run, key, bridge = self._run_same_observed(
            "same0", 0, batch_id=batch
        )
        self.assertEqual(run["pause_reason"], None, run)
        self.assertEqual(len(run["drafts"]), 1, run)
        self.assertEqual(run["model_calls"], 1, run)
        self.assertEqual(bridge, 1, run)
        state = self._post_close_state(key, batch)
        self.assertEqual(state["drafts"], 1, state)
        self.assertEqual(state["runtime_decisions"], 1, state)
        self.assertEqual(state["discovery_state"], "done", state)
        self.assertEqual(state["budget"]["seconds"], 900, state)  # type: ignore[index]

    def test_same_observed_at_five_seconds_still_decides_once(self) -> None:
        """同 observed_at 的新快照在 5000ms 边界内仍允许决策，不扩大到 5001ms。"""
        batch = "00000000-0000-4000-8000-0000000000c3"
        run, key, bridge = self._run_same_observed(
            "same5000", 5_000, batch_id=batch
        )
        self.assertEqual(run["pause_reason"], None, run)
        self.assertEqual(len(run["drafts"]), 1, run)
        self.assertEqual(run["model_calls"], 1, run)
        self.assertEqual(bridge, 1, run)
        state = self._post_close_state(key, batch)
        self.assertEqual(state["drafts"], 1, state)
        self.assertEqual(state["runtime_decisions"], 1, state)
        self.assertEqual(state["discovery_state"], "done", state)
        self.assertEqual(state["budget"]["seconds"], 895, state)  # type: ignore[index]

    def test_same_observed_at_5001ms_is_rejected_without_bridge(self) -> None:
        """同 observed_at 超过 5 秒的 5001ms 偏移必须阻塞且零桥。"""
        batch = "00000000-0000-4000-8000-0000000000c4"
        run, key, bridge = self._run_same_observed(
            "same5001", 5_001, batch_id=batch
        )
        self.assertEqual(run["pause_reason"], "unread_unverified", run)
        self.assertEqual(run["drafts"], [], run)
        self.assertEqual(run["model_calls"], 0, run)
        self.assertEqual(bridge, 0, run)
        state = self._post_close_state(key, batch)
        self.assertEqual(state["drafts"], 0, state)
        self.assertEqual(state["runtime_decisions"], 0, state)
        self.assertEqual(state["discovery_state"], "blocked", state)
        self.assertEqual(state["budget"]["seconds"], 895, state)  # type: ignore[index]

    def test_stale_second_conversation_is_rejected_without_bridge(self) -> None:
        """第二次会话 observed_at 落后 30 秒时必须保守阻塞且零桥。"""
        self._run_rejected(
            "stalereread",
            lambda: self._mutate_manifest(
                "tests/fixtures/runtime/read-stalereread/conversation.json",
                observed_at=_format_ms(self._base_ms(), -30_000),
            ),
        )

    def test_future_second_conversation_is_rejected_without_bridge(self) -> None:
        """第二次会话 observed_at 超前 6 秒时不得当作当前证据或启动桥。"""
        self._run_rejected(
            "futureread",
            lambda: self._mutate_manifest(
                "tests/fixtures/runtime/read-futureread/conversation.json",
                observed_at=_format_ms(self._base_ms(), 6_000),
            ),
        )

    def test_changed_selector_profile_is_rejected_without_bridge(self) -> None:
        """第二次会话 selector profile 与初次采集上下文不一致时零桥。"""
        self._run_rejected(
            "profileread",
            lambda: self._mutate_profile(
                "tests/fixtures/runtime/read-profileread/conversation.json",
                "synthetic-boss-m3-alt",
            ),
        )

    def test_wrong_conversation_key_is_rejected_without_bridge(self) -> None:
        """第二次采集变成另一会话身份时不得用旧任务键启动桥。"""
        self._run_rejected(
            "keyread",
            lambda: self._mutate_identity(
                "tests/fixtures/runtime/read-keyread/conversation.json"
            ),
        )

    def test_replayed_snapshot_is_rejected_without_bridge(self) -> None:
        """第二次会话回放同一 ReadResult/snapshot 时不得当作新证据。"""
        self._run_rejected("replayread", replay=True)

    def test_incomplete_second_conversation_is_rejected_without_bridge(self) -> None:
        """第二次会话缺少完整遍历证明时安全阻塞且零桥。"""
        self._run_rejected(
            "incompleteread",
            lambda: self._mutate_manifest(
                "tests/fixtures/runtime/read-incompleteread/conversation.json",
                coverage_proof={
                    "kind": "full_traversal",
                    "top_proven": True,
                    "bottom_proven": False,
                },
            ),
        )

    def test_missing_stable_inbound_identity_is_rejected_without_bridge(self) -> None:
        """第二次会话没有稳定入站身份时不得构造可发送证据或启动桥。"""
        self._run_rejected(
            "stableread",
            lambda: self._mutate_stable_identity(
                "tests/fixtures/runtime/read-stableread/conversation.json"
            ),
        )


class PostClaimLongReadTests(_RuntimeCase):
    """S07：post-claim 多命令长读取按 20 秒续租并诚实处理新鲜度。"""

    class _Gate:
        """ScreenPort 门禁替身。"""

        def require_available(self) -> None:
            """始终可用。"""
            return

        def verify(self) -> None:
            """命令前复核无副作用。"""
            return

        def release(self) -> None:
            """无占用。"""
            return

    def _outputs(self, pages: int) -> list[object]:
        """构造未读 3 条 + pre-claim 2N + post-claim 2N 条真实原始输出。"""
        primary, raw = self._unread_pixel_manifest("甲")
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key, unread_count=1)
        conversation = self._many_page_conversation(f"longread{pages}", pages)
        page_xml = self._page_outputs(conversation)
        unread_xml = (self.root / primary).parent.joinpath("unread.xml").read_text(
            encoding="utf-8"
        )
        outputs: list[object] = ["dumped", unread_xml, raw]
        for xml in page_xml:
            outputs.extend(["dumped", xml])
        for xml in page_xml:
            outputs.extend(["dumped", xml])
        return outputs, key, conversation

    def _run(
        self, pages: int, ms: int, *, batch: str
    ) -> tuple[dict[str, object], int, dict[str, object], _Clock]:
        """经真实 execute_run→AdbRuntimeRead→AdbScreenReader→run_adb 运行。"""
        outputs, key, conversation = self._outputs(pages)
        rel = self._scenario(
            f"long-{pages}-{ms}",
            [
                self._step(
                    read_fixture=self._unread_pixel_manifest("甲")[0],
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
            batch_id=batch,
        )
        clock = _Clock()
        made: list[_FakeSpawn] = []

        def spawn(*args: object, **kwargs: object) -> _FakeSpawn:
            """按实际创建顺序返回最低进程替身并记录启动数。"""
            del args, kwargs
            index = len(made)
            if index >= len(outputs):
                raise AssertionError(f"unexpected spawn {index}")
            item = _FakeSpawn(outputs[index], clock, ms)
            made.append(item)
            return item

        seen: dict[str, object] = {}

        def started() -> None:
            """真实 claim/start 成功后记录 pre-claim 实际启动数。"""
            seen["after_start"] = len(made)

        adapter = AdbRuntimeRead(
            self.root,
            AdbScreenReader(None, gate=self._Gate()),
            "synthetic-serial",
            clock=clock.now,
        )
        calls = {"bridge": 0}
        finish: dict[str, object] = {}

        def factory(stimulus: object) -> SyntheticDecisionBridge:
            """记录任何桥调用；长读陈旧时必须为零。"""
            calls["bridge"] += 1
            return SyntheticDecisionBridge(
                stimulus,  # type: ignore[arg-type]
                provider_id=self.provider,
                model_id=self.model_id,
                model_config_hash=self.config_hash,
            )

        class RecordingStore:
            """委托真实仓储，仅记录语义阻塞实际消费的 Lease 截止时间。"""

            def __init__(self, inner: object) -> None:
                """保存真实仓储。"""
                self._inner = inner

            def __getattr__(self, name: str):
                """其余能力完全委托。"""
                return getattr(self._inner, name)

            def finish_semantic_block(self, lease: Lease, now: str, reason: str) -> str:
                """记录 finish 消费的最新 Lease 截止时间后委托真实写入。"""
                finish["expires_at"] = lease.expires_at
                finish["now"] = now
                return self._inner.finish_semantic_block(  # type: ignore[attr-defined]
                    lease, now, reason
                )

        with patch("app.adapters.adb.command.subprocess.Popen", side_effect=spawn):
            run = self._execute(
                rel,
                clock=clock,
                read=adapter,
                on_started=started,
                bridge_factory=factory,
                store_monotonic=lambda: clock.mono() / 1000.0,
                store_hook=lambda inner: RecordingStore(inner),
            )
        run["_spawned"] = len(made)
        run["_after_start"] = seen.get("after_start")
        run["_bridge"] = calls["bridge"]
        run["_finish"] = finish
        return run, len(outputs), {"key": key}, clock

    def test_43_command_long_read_renews_and_blocks_on_stale_freshness(self) -> None:
        """10 页 pre+post 共 43 条命令、80 秒 post 读取在 20/40/60/80 秒续租后按新鲜度阻塞。"""
        run, expected, meta, clock = self._run(
            10,
            4_000,
            batch="00000000-0000-4000-8000-0000000000aa",
        )
        self.assertEqual(run["_spawned"], expected)
        self.assertEqual(run["_spawned"], 43)
        self.assertEqual(run["pause_reason"], "unread_unverified")
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["drafts"], [])
        self.assertEqual(run["_bridge"], 0)
        self.assertEqual(self._discovery_state(meta["key"]), "blocked")  # type: ignore[arg-type]
        self.assertEqual(self._task_events().count("lease_renewed"), 4)
        connection = sqlite3.connect(str(self.path))
        try:
            renewed = [
                str(row[0])
                for row in connection.execute(
                    "SELECT created_at FROM events WHERE event = 'lease_renewed' "
                    "ORDER BY event_id"
                ).fetchall()
            ]
        finally:
            connection.close()
        self.assertEqual(
            renewed,
            [clock.wall_at(ms) for ms in (112_000, 132_000, 152_000, 172_000)],
        )
        self.assertEqual(run["_finish"]["now"], clock.wall_at(172_000))
        self.assertEqual(run["_finish"]["expires_at"], clock.wall_at(232_000))

    def test_short_trusted_clock_read_builds_decision_from_new_snapshot(self) -> None:
        """2 页、每条 500ms 的短读取仍使用新采集快照完成一次决策。"""
        run, expected, meta, _clock = self._run(
            2,
            500,
            batch="00000000-0000-4000-8000-0000000000ab",
        )
        self.assertEqual(run["_spawned"], expected)
        self.assertEqual(run["_spawned"], 11)
        self.assertEqual(run["pause_reason"], None, run)
        self.assertEqual(run["processed"], 1, run)
        self.assertEqual(len(run["drafts"]), 1)
        self.assertEqual(run["_bridge"], 1)
        self.assertEqual(self._discovery_state(meta["key"]), "done")  # type: ignore[arg-type]
        # 短读没有逐命令 20 秒续租；仅决策前一次性 Lease 复核会记一次续租。
        self.assertEqual(self._task_events().count("lease_renewed"), 1)


class PostClaimLeaseFailureTests(_RuntimeCase):
    """C：post-claim 逐命令门禁/Lease/nonce/时钟失败在后续 spawn 前停止。"""

    class _Gate:
        """ScreenPort 门禁替身，可被测试在真实 start 后切换为失败。"""

        def __init__(self) -> None:
            """初始化可用状态。"""
            self.fail = False

        def require_available(self) -> None:
            """始终可用。"""
            return

        def verify(self) -> None:
            """按测试开关拒绝命令前复核。"""
            if self.fail:
                raise RuntimeError("synthetic gate changed")

        def release(self) -> None:
            """无占用。"""
            return

    def _prepare(self, name: str) -> tuple[str, str, list[object], str]:
        """生成未读 2 页会话与 pre+post 原始输出，返回场景与键。"""
        primary, raw = self._unread_pixel_manifest("甲")
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key, unread_count=1)
        conversation = self._many_page_conversation(name, 2)
        page_xml = self._page_outputs(conversation)
        outputs: list[object] = [
            "dumped",
            (self.root / primary).parent.joinpath("unread.xml").read_text(
                encoding="utf-8"
            ),
            raw,
        ]
        for xml in page_xml + page_xml:
            outputs.extend(["dumped", xml])
        rel = self._scenario(
            name,
            [
                self._step(
                    read_fixture=primary,
                    conversation_fixture=conversation,
                    decision=helpers.decision_stimulus(),
                )
            ],
            batch_id=f"00000000-0000-4000-8000-{abs(hash(name)) % 10**12:012d}",
        )
        return rel, key, outputs, conversation

    def _execute_with(
        self, rel: str, outputs: list[object], *, gate=None, store_hook=None, on_started=None
    ) -> tuple[dict[str, object], list[_FakeSpawn], dict[str, object]]:
        """用最低进程替身运行一次并返回真实启动数与 start 边界计数。"""
        made: list[_FakeSpawn] = []
        clock = _Clock()
        seen: dict[str, object] = {}

        def spawn(*args: object, **kwargs: object) -> _FakeSpawn:
            """按实际创建顺序计数；超出原始响应即失败。"""
            del args, kwargs
            index = len(made)
            if index >= len(outputs):
                raise AssertionError(f"unexpected spawn {index}")
            item = _FakeSpawn(outputs[index], clock, 100)
            made.append(item)
            return item

        def started() -> None:
            """真实 start 后记录边界并可选切换外部状态。"""
            seen["after_start"] = len(made)
            if on_started is not None:
                on_started()

        adapter = AdbRuntimeRead(
            self.root,
            AdbScreenReader(None, gate=gate or self._Gate()),
            "synthetic-serial",
            clock=clock.now,
        )
        calls = {"bridge": 0}

        def factory(stimulus: object) -> SyntheticDecisionBridge:
            """记录桥调用。"""
            calls["bridge"] += 1
            return SyntheticDecisionBridge(
                stimulus,  # type: ignore[arg-type]
                provider_id=self.provider,
                model_id=self.model_id,
                model_config_hash=self.config_hash,
            )

        with patch("app.adapters.adb.command.subprocess.Popen", side_effect=spawn):
            run = self._execute(
                rel,
                clock=clock,
                read=adapter,
                on_started=started,
                bridge_factory=factory,
                store_hook=store_hook,
                store_monotonic=lambda: clock.mono() / 1000.0,
                gate=gate,
            )
        run["_spawned"] = len(made)
        run["_bridge"] = calls["bridge"]
        return run, made, seen

    def test_expired_lease_before_first_post_claim_command_starts_nothing(self) -> None:
        """真实 start 后墙钟越过 60 秒 Lease 时，post-claim 零命令且零桥。"""
        rel, key, outputs, _conversation = self._prepare("expiry")
        clock = _Clock()

        def advance() -> None:
            """模拟 start 与首条重读命令之间的外部长时间停顿。"""
            clock.advance(61_000)

        gate = self._Gate()
        made: list[_FakeSpawn] = []

        def spawn(*args: object, **kwargs: object) -> _FakeSpawn:
            """按序返回最低替身。"""
            del args, kwargs
            made.append(_FakeSpawn(outputs[len(made)], clock, 100))
            return made[-1]

        adapter = AdbRuntimeRead(
            self.root,
            AdbScreenReader(None, gate=gate),
            "synthetic-serial",
            clock=clock.now,
        )
        with patch("app.adapters.adb.command.subprocess.Popen", side_effect=spawn):
            run = self._execute(
                rel,
                clock=clock,
                read=adapter,
                on_started=advance,
                store_monotonic=lambda: clock.mono() / 1000.0,
            )
        self.assertEqual(run["pause_reason"], "precondition_failed")
        self.assertEqual(run["processed"], 1)
        self.assertEqual(run["drafts"], [])
        self.assertEqual(len(made), 7)  # 3 条初读 + 2 页 pre，start 后过期零重读命令
        self.assertEqual(self._discovery_state(key), "running")

    def test_gate_change_after_start_blocks_first_post_claim_command(self) -> None:
        """共享门禁在真实 start 后变化时首条重读命令不启动并零桥。"""
        rel, key, outputs, _conversation = self._prepare("gatechange")
        gate = _ToggleGate(
            SharedDeviceGate(
                profile_id="demo", profile_db_path=self.path, os_root=self.os_root
            )
        )

        def fail() -> None:
            """真实 start 后切换门禁为失败。"""
            gate.fail = True

        run, made, seen = self._execute_with(
            rel, outputs, gate=gate, on_started=fail
        )
        self.assertEqual(run["pause_reason"], "precondition_failed")
        self.assertEqual(run["_bridge"], 0)
        self.assertEqual(len(made), seen["after_start"])
        self.assertEqual(self._discovery_state(key), "running")

    def test_nonce_change_between_post_claim_commands_stops_subsequent_spawn(self) -> None:
        """首条 post-claim 命令后 nonce 改变时后续命令零启动并零桥。"""
        rel, key, outputs, _conversation = self._prepare("midnonce")
        state = {"calls": 0, "changed": False}

        class NonceChangingStore:
            """委托真实仓储，只在 post-claim 首条 require_running 后改 nonce。"""

            def __init__(self, inner: object) -> None:
                """保存真实仓储。"""
                self._inner = inner

            def __getattr__(self, name: str):
                """其余能力完全委托。"""
                return getattr(self._inner, name)

            def require_running(self, lease: Lease, *, now: str) -> bool:
                """返回真实结果后注入一次外部 nonce 变化。"""
                result = self._inner.require_running(lease, now=now)  # type: ignore[attr-defined]
                state["calls"] += 1
                if result and state["calls"] == 1:
                    connection = self._inner._connection  # type: ignore[attr-defined]
                    connection.execute(
                        "UPDATE hr_discoveries SET nonce = ? WHERE \"key\" = ?",
                        (str(uuid.uuid4()), lease.key),
                    )
                    connection.commit()
                    state["changed"] = True
                return result

        run, made, seen = self._execute_with(
            rel, outputs, store_hook=lambda inner: NonceChangingStore(inner)
        )
        self.assertTrue(state["changed"])
        self.assertEqual(run["pause_reason"], "precondition_failed")
        self.assertEqual(run["_bridge"], 0)
        self.assertEqual(len(made), seen["after_start"] + 1)
        self.assertEqual(self._discovery_state(key), "running")

    def test_context_rejects_expiry_gate_nonce_clock_and_renew_failure(self) -> None:
        """真实仓储下逐命令上下文对过期/门禁/nonce/时钟/renew 失败均结构化拒绝。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key, unread_count=1)
        batch = "00000000-0000-4000-8000-0000000000c1"
        clock = _Clock()
        store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.owner.host,
            monotonic=lambda: clock.mono() / 1000.0,
            require_runtime=True,
        )
        try:
            store.ensure_batch(batch, BudgetLimits(), helpers.NOW)
            lease = store.claim(APP, "unread", self.owner, helpers.NOW, conversation_key=key)
            self.assertIsNotNone(lease)
            lease = store.start(lease, helpers.NOW)  # type: ignore[arg-type]

            class Gate:
                """可控门禁。"""

                def __init__(self) -> None:
                    """初始化可用。"""
                    self.fail = False

                def verify(self, now: str) -> None:
                    """按开关拒绝。"""
                    del now
                    if self.fail:
                        raise RuntimeError("gate changed")

            gate = Gate()
            context = _LeaseCommandContext(
                store=store,
                gate=gate,  # type: ignore[arg-type]
                clock=clock.now,
                monotonic_ms=clock.mono,
                lease=lease,  # type: ignore[arg-type]
            )
            context.before()

            gate.fail = True
            with self.assertRaises(RuntimeCommandControlError) as raised:
                context.before()
            self.assertEqual(raised.exception.reason_code, "device_gate_mismatch")
            gate.fail = False

            connection = store._connection  # type: ignore[attr-defined]
            old_nonce = lease.nonce
            connection.execute(
                "UPDATE hr_discoveries SET nonce = ? WHERE \"key\" = ?",
                (str(uuid.uuid4()), lease.key),
            )
            connection.commit()
            with self.assertRaises(RuntimeCommandControlError) as raised:
                context.before()
            self.assertEqual(raised.exception.reason_code, "stale_lease")
            connection.execute(
                "UPDATE hr_discoveries SET nonce = ? WHERE \"key\" = ?",
                (old_nonce, lease.key),
            )
            connection.commit()

            old_lease = context.lease
            clock.advance(20_000)
            context.after(failed=False)
            self.assertGreater(
                parse_utc(context.lease.expires_at),
                parse_utc(old_lease.expires_at),
            )

            clock.advance(80_000)
            with self.assertRaises(StoreError) as stale:
                store.finish_semantic_block(
                    context.lease, clock.now(), "unread_unverified"
                )
            self.assertEqual(stale.exception.reason_code, "stale_lease")
        finally:
            store.close()

    def test_clock_change_on_renew_reports_clock_changed(self) -> None:
        """续租时墙钟领先内部单调钟超过容差必须结构化报 clock_changed。"""
        key = helpers.m3_unread_key()
        helpers.seed_discovery(self.root, key, unread_count=1)
        batch = "00000000-0000-4000-8000-0000000000c2"
        store = SqliteStore(
            self.path,
            profile_id="demo",
            local_host=self.owner.host,
            monotonic=lambda: 0.0,
            require_runtime=True,
        )
        try:
            store.ensure_batch(batch, BudgetLimits(), helpers.NOW)
            lease = store.claim(APP, "unread", self.owner, helpers.NOW, conversation_key=key)
            lease = store.start(lease, helpers.NOW)  # type: ignore[arg-type]
            clock = _Clock()

            class Gate:
                """始终可用。"""

                def verify(self, now: str) -> None:
                    """无副作用。"""
                    del now

            context = _LeaseCommandContext(
                store=store,
                gate=Gate(),  # type: ignore[arg-type]
                clock=clock.now,
                monotonic_ms=clock.mono,
                lease=lease,  # type: ignore[arg-type]
            )
            clock.advance(30_000)
            with self.assertRaises(RuntimeCommandControlError) as raised:
                context.after(failed=False)
            self.assertEqual(raised.exception.reason_code, "clock_changed")
        finally:
            store.close()

    def test_before_rejection_prevents_any_adapter_spawn(self) -> None:
        """绑定上下文在命令前拒绝时适配器零 Popen，不存在后续采集。"""
        manifest = "tests/fixtures/read/page.json"

        class RejectingContext:
            """首条命令前固定拒绝。"""

            lease = object()

            def before(self) -> None:
                """拒绝命令前复核。"""
                raise RuntimeCommandControlError("stale_lease")

            def after(self, *, failed: bool) -> None:
                """不应被调用。"""
                del failed

        adapter = AdbRuntimeRead(
            self.root,
            AdbScreenReader(None, gate=self._Gate()),
            "synthetic-serial",
        )
        adapter.bind_command_context(RejectingContext())
        made: list[object] = []

        def spawn(*args: object, **kwargs: object) -> object:
            """任何真实创建都记录为回归。"""
            del args, kwargs
            made.append(object())
            raise AssertionError("must not spawn")

        with patch("app.adapters.adb.command.subprocess.Popen", side_effect=spawn):
            with self.assertRaises(RuntimeCommandControlError):
                adapter.read(manifest)
        self.assertEqual(made, [])

    def test_renew_failure_stops_after_first_spawn(self) -> None:
        """命令后严格续租失败时传播固定原因且不再启动下一条命令。"""
        conversation = self._many_page_conversation("renewfail", 2)

        class FailingAfter:
            """首条命令后严格续租失败。"""

            lease = object()

            def before(self) -> None:
                """命令前放行。"""
                return

            def after(self, *, failed: bool) -> None:
                """命令后固定失败。"""
                del failed
                raise RuntimeCommandControlError("store_busy")

        adapter = AdbRuntimeRead(
            self.root,
            AdbScreenReader(None, gate=self._Gate()),
            "synthetic-serial",
        )
        adapter.bind_command_context(FailingAfter())
        made: list[_FakeSpawn] = []

        def spawn(*args: object, **kwargs: object) -> _FakeSpawn:
            """第一条命令真实创建后即应停止。"""
            del args, kwargs
            item = _FakeSpawn("dumped")
            made.append(item)
            return item

        with patch("app.adapters.adb.command.subprocess.Popen", side_effect=spawn):
            with self.assertRaises(RuntimeCommandControlError) as raised:
                adapter.read(conversation)
        self.assertEqual(raised.exception.reason_code, "store_busy")
        self.assertEqual(len(made), 1)


class S05ReopenControlTests(_RuntimeCase):
    """S05：真实 paused→关闭→新进程重开后的预算/游标/列表控制。"""

    def _prime(
        self, batch: str, key_a: str, *, pending: tuple[str, ...] = ()
    ) -> None:
        """父进程真实处理首卡并持久化暂停游标/四维预算，可选保留待办卡。"""
        store = self._store()
        try:
            store.ensure_batch(batch, BudgetLimits(), helpers.NOW)
            store.enqueue_jobs(batch, (key_a,) + pending, helpers.NOW)
            lease = store.claim_job(batch, self.owner, helpers.NOW, key_a)
            self.assertIsNotNone(lease)
            lease = store.start_job(lease, helpers.NOW)  # type: ignore[arg-type]
            store.finish_job(lease, helpers.NOW, "done", None)  # type: ignore[arg-type]
            batch_row = store.load_batch(batch)
            self.assertIsNotNone(batch_row)
            cursor = batch_row["cursor"]  # type: ignore[index]
            cursor = cursor.visit_job(key_a)  # type: ignore[union-attr]
            cursor = replace(
                cursor,
                budget=replace(
                    cursor.budget,
                    remaining_seconds=800,
                    remaining_device_calls=150,
                    remaining_candidates=10,
                    remaining_preemptions=3,
                ),
            )
            store.pause_batch(batch, cursor, "budget_exhausted", now=helpers.NOW)
        finally:
            store.close()

    def _child(
        self, name: str, batch: str, steps: list[dict[str, object]]
    ) -> dict[str, object]:
        """在真正独立 OS 进程中重开同一 profile/batch 并返回 run 结果。"""
        rel = self._scenario(name, steps, batch_id=batch)
        script = (
            "import sys\n"
            "import os\n"
            "from pathlib import Path\n"
            "import app.cli.main as m\n"
            "m.PROJECT_ROOT = Path(sys.argv[1])\n"
            "Path(sys.argv[1], 'child.pid').write_text(str(os.getpid()))\n"
            "os_root = Path(sys.argv[2])\n"
            "argv = sys.argv[3:]\n"
            "raise SystemExit(m.main(argv, os_root=os_root))\n"
        )
        argv = [
            "run",
            "execute",
            "--pack",
            "prompts/local/demo",
            "--mode",
            "draft_only",
            "--fixture",
            rel,
            "--confirm-device",
            "--credential",
            self.credential,
            "--provider",
            self.provider,
            "--model-id",
            self.model_id,
            "--model-config-hash",
            self.config_hash,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", script, str(self.root), str(self.os_root)] + argv,
            cwd=str(self.root),
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr))
        self.assertEqual(proc.stderr, "")
        child_pid = int((self.root / "child.pid").read_text(encoding="utf-8"))
        self.assertNotEqual(child_pid, os.getpid())
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(payload["status"], "ok")
        return payload["result"]["run"]  # type: ignore[index]

    def test_reopen_preserves_budget_and_skips_visited_first_card(self) -> None:
        """列表不变时新进程保留预算、跳过首卡 visited 并处理下一卡。"""
        batch = "00000000-0000-4000-8000-0000000000d1"
        manifest, keys = self._job_list("s05-same", ("s05a", "s05b"))
        self._prime(batch, keys[0])
        detail_b = helpers.make_job_fixture(self.root, "s05b")[0]
        run = self._child(
            "s05-same-child",
            batch,
            [self._step(read_fixture=manifest, job_fixture=detail_b)],
        )
        self.assertEqual(run["processed"], 1)
        self.assertIsNone(run["pause_reason"])
        self.assertEqual(run["remaining_budget"]["seconds"], 800)  # type: ignore[index]
        self.assertEqual(run["remaining_budget"]["device_calls"], 148)  # type: ignore[index]
        self.assertEqual(run["remaining_budget"]["candidates"], 9)  # type: ignore[index]
        self.assertEqual(run["remaining_budget"]["preemptions"], 3)  # type: ignore[index]
        self.assertEqual(self._job_state(keys[0]), "done")
        self.assertEqual(self._job_state(keys[1]), "done")

    def test_reopen_handles_reordered_list_by_identity(self) -> None:
        """重排后仍按真实 visited 身份选下一卡，不按旧下标重复。"""
        batch = "00000000-0000-4000-8000-0000000000d2"
        _ab, keys = self._job_list("s05-ab", ("s05c", "s05d"))
        reordered, _other = self._job_list("s05-ba", ("s05d", "s05c"))
        self._prime(batch, keys[0])
        run = self._child(
            "s05-ba-child",
            batch,
            [
                self._step(
                    read_fixture=reordered,
                    job_fixture=helpers.make_job_fixture(self.root, "s05d")[0],
                )
            ],
        )
        self.assertEqual(run["processed"], 1)
        self.assertEqual(self._job_state(keys[0]), "done")
        self.assertEqual(self._job_state(keys[1]), "done")

    def test_reopen_handles_inserted_card_without_replaying_visited(self) -> None:
        """插入新卡后按身份继续其后的未访问卡，不重放已 visited 首卡。"""
        batch = "00000000-0000-4000-8000-0000000000d3"
        _ab, keys = self._job_list("s05-ab2", ("s05e", "s05f"))
        inserted, _keys3 = self._job_list(
            "s05-insert", ("s05g", "s05e", "s05f")
        )
        self._prime(batch, keys[0])
        run = self._child(
            "s05-insert-child",
            batch,
            [
                self._step(
                    read_fixture=inserted,
                    job_fixture=helpers.make_job_fixture(self.root, "s05f")[0],
                ),
                self._step(
                    read_fixture=inserted,
                    job_fixture=helpers.make_job_fixture(self.root, "s05g")[0],
                ),
            ],
        )
        self.assertEqual(run["processed"], 2)
        self.assertEqual(self._job_state(keys[0]), "done")
        self.assertEqual(self._job_state(keys[1]), "done")
        inserted_key = helpers.make_job_fixture(self.root, "s05g")[1]
        self.assertEqual(self._job_state(inserted_key), "done")

    def test_reopen_missing_unvisited_card_is_not_claimed(self) -> None:
        """原先未访问的卡在新进程中消失时不得凭旧列表/下标领取。"""
        batch = "00000000-0000-4000-8000-0000000000d4"
        _ab, keys = self._job_list("s05-ab3", ("s05h", "s05i"))
        only_a, _only = self._job_list("s05-only", ("s05h",))
        self._prime(batch, keys[0], pending=(keys[1],))
        run = self._child(
            "s05-only-child",
            batch,
            [self._step(read_fixture=only_a)],
        )
        self.assertEqual(run["processed"], 0)
        self.assertEqual(self._job_state(keys[1]), "queued")


if __name__ == "__main__":
    unittest.main()
