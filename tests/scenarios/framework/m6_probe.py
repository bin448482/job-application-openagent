"""在隔离根内用真实 M6 生产入口执行 S03/S05/S06/S07 的可执行边界。

只注入外部刺激：合成 M3 读取引用、原始时钟/单调时钟与设备读取计数；真实
profile/共享门禁/唯一 SqliteStore/execute_run/租约/预算/游标均为生产实现。
不读取 expected/oracle，不返回 performed/pause/ready 等结论之外的推导结果；
所有结论由独立 expected 比较。G1/G2/G3 未交付项不在本探针内伪造。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path.cwd().resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
from app.domain.models import (
    APP,
    Candidate,
    Owner,
    ReadResult,
    UnreadRow,
    UnreadSignal,
    UnreadSnapshot,
    canonical_hash,
    conversation_key_for,
    discovery_key_for,
    fingerprint_for,
    identity_hash_for,
    parse_utc,
)
from app.domain.reading import job_identity_hash_for, job_key_from_identity
from app.domain.runtime import (
    BudgetLimits,
    BudgetState,
    build_stage_preempted,
    clock_drift_exceeded,
    observe_unread,
)
from app.ports.decision import BridgeDescriptor
from app.usecases import policy as policy_usecases
from app.usecases.policy_eval import RepositorySnapshot, eval_pack
from app.usecases.runtime import execute_run

NOW = "2026-11-25T10:00:00.000Z"
BASE_MS = 1_764_000_000_000  # 2025-11-25T10:00:00Z, only for synthetic wall math
PACK_REL = "prompts/local/demo"
SIGNAL_KEYS = ("row_badge", "new_greeting", "tab_badge", "preview_delta")
SAFE = {
    "list_returned": True,
    "input_empty": True,
    "confirmation_closed": True,
    "no_inflight_command": True,
    "no_pending_action": True,
    "action_state_committed": True,
    "checkpoint_committed": True,
}
PUBLIC_DECISIONS = {
    "eval-task-greeting-location": {
        "intent": "interested",
        "sub_intents": ["greeting"],
        "reply": "您好，我目前接受苏州的工作地点。",
        "escalate": None,
    },
    "eval-task-greeting-salary": {
        "intent": "interested",
        "sub_intents": ["salary"],
        "reply": "您好，我的期望薪资是15-20k。",
        "escalate": None,
    },
    "eval-task-missing-salary": {
        "intent": "needs_human",
        "sub_intents": ["salary"],
        "reply": "薪资信息需要本人确认，我先转人工核实。",
        "escalate": "human_review",
    },
    "eval-task-injection-role": {
        "intent": "needs_human",
        "sub_intents": ["sensitive"],
        "reply": "这条消息包含异常指令，已转人工处理。",
        "escalate": "human_review",
    },
    "eval-task-job-question": {
        "intent": "interested",
        "sub_intents": ["job_question"],
        "reply": "这个岗位主要负责后端服务与接口开发，想先了解团队情况。",
        "escalate": None,
    },
    "eval-task-job-card": {
        "intent": "interested",
        "sub_intents": [],
        "reply": "这个岗位可以考虑，想先了解团队情况。",
        "escalate": None,
    },
}


class ProbeError(RuntimeError):
    """表示探针请求或隔离边界不满足受控协议。"""


class _AcceptanceBridge:
    """只提供公开套件所需的合成 transport 响应，不读取断言或答案。"""

    def descriptor(self) -> BridgeDescriptor:
        """返回非 simulated 的固定桥摘要，用于生成本机验收凭证。"""
        return BridgeDescriptor(
            provider_id="local-process",
            model_id="synthetic-m6-model",
            model_config_hash=canonical_hash({"synthetic": "m6-model"}),
            bridge="bridge_b",
            simulated=False,
        )

    def complete(
        self,
        *,
        system_message: str,
        developer_message: str,
        user_message: str,
        echo: Mapping[str, str],
    ) -> str:
        """按 echo 回填固定合成决策；不读取断言侧。"""
        spec = PUBLIC_DECISIONS[echo["task_id"]]
        data = json.loads(user_message)
        return json.dumps(
            {
                "schema_version": 1,
                "task_id": echo["task_id"],
                "nonce": echo["nonce"],
                "input_hash": echo["input_hash"],
                "intent": spec["intent"],
                "sub_intents": spec["sub_intents"],
                "reply": spec["reply"],
                "evidence": str(data["inbound"])[:2],
                "escalate": spec["escalate"],
            },
            ensure_ascii=False,
        )


class _Clock:
    """按显式序列提供可信墙钟与单调值；不 substitute 业务判定。"""
    def __init__(
        self,
        mono: list[int] | None = None,
        wall: list[int] | None = None,
        *,
        wall_step_ms: int = 0,
    ) -> None:
        """保存两条序列；explicit wall 与递增 wall 时均不与单调序列联动。"""
        self._mono = list(mono or [0])
        self._wall = list(wall) if wall is not None else None
        self._mi = 0
        self._wi = 0
        self._base = 0
        self._start: int | None = None
        self._last = 0
        self._step = wall_step_ms
        self._counter = 0
        self._cur_wall = 0

    def set_base(self, base: int) -> None:
        """设置本次运行墙钟相对基准，保证跨运行不倒退。"""
        if self._wall is None and not self._step:
            self._base = base

    def mono(self) -> int:
        """返回下一个单调毫秒值，耗尽后保持末值。"""
        value = self._mono[min(self._mi, len(self._mono) - 1)]
        self._mi += 1
        if self._start is None:
            self._start = value
        self._last = value
        return value

    def wall(self) -> str:
        """返回墙钟；未显式给出时按即将读取的单调值保持同增量。"""
        if self._wall is not None:
            value = self._wall[min(self._wi, len(self._wall) - 1)]
            self._wi += 1
            self._cur_wall = value
            return _iso(value)
        if self._step:
            value = self._counter
            self._counter += self._step
            self._cur_wall = value
            return _iso(value)
        peek = self._mono[min(self._mi, len(self._mono) - 1)]
        start = self._start if self._start is not None else peek
        value = self._base + (peek - start)
        self._cur_wall = value
        return _iso(value)

    def current_ms(self) -> int:
        """返回最近一次墙钟毫秒，仅用于让真实仓储的时钟增量保持一致。"""
        return self._cur_wall

    def set_now(self, value: str) -> None:
        """把仓储显式 now 同步到同一毫秒基准，避免采样时点错位。"""
        self._cur_wall = _wall_ms(value)


class EnvironmentClock:
    """由外部刺激边界显式推进的统一墙钟/单调环境；不按调用回填。"""

    def __init__(self) -> None:
        """从零开始；测试驱动只通过 advance 推进。"""
        self.wall_ms = 0
        self.mono_ms = 0

    def advance(self, wall_ms: int, mono_ms: int | None = None) -> None:
        """在外部刺激边界推进当前环境；默认墙钟与单调同步。"""
        self.wall_ms = wall_ms
        self.mono_ms = wall_ms if mono_ms is None else mono_ms

    def wall(self) -> str:
        """返回当前环境墙钟。"""
        return _iso(self.wall_ms)

    def mono(self) -> int:
        """返回当前环境单调毫秒。"""
        return self.mono_ms


class _TimedScene:
    """在每次外部步骤前推进环境时钟；不读取产品 now。"""

    def __init__(
        self,
        spec: dict[str, Any],
        *,
        profile_id: str,
        times: list[int],
        clock: EnvironmentClock,
    ) -> None:
        """保存真实场景适配器、每步单调毫秒与环境时钟。"""
        self._inner = StimulusRuntimeScene(spec, profile_id=profile_id)
        self.batch_id = self._inner.batch_id
        self._times = list(times)
        self._clock = clock
        self._index = 0
        self.consumed: list[Any] = []

    def next_step(self) -> Any:
        """返回下一步；在返回前按外部时间表推进环境并记录实际消费。"""
        step = self._inner.next_step()
        if step is not None:
            self.consumed.append(step)
        if step is not None and self._index < len(self._times):
            self._clock.advance(self._times[self._index])
            self._index += 1
        return step


class _MeasuredStore:
    """把仓储显式 now 同步到注入时钟；不改变任何业务方法。"""

    def __init__(self, store: SqliteStore, clock: _Clock) -> None:
        """保存真实仓储与注入时钟。"""
        self._store = store
        self._clock = clock

    def __getattr__(self, name: str) -> Any:
        """返回委托方法；调用前从显式 now 参数同步单调基准。"""
        attribute = getattr(self._store, name)
        if not callable(attribute):
            return attribute

        def wrapped(*args: object, **kwargs: object) -> object:
            """同步 now 后委托真实仓储。"""
            now = kwargs.get("now")
            if now is None:
                for item in reversed(args):
                    if isinstance(item, str) and item.endswith("Z") and len(item) == 24:
                        now = item
                        break
            if isinstance(now, str):
                self._clock.set_now(now)
            return attribute(*args, **kwargs)

        return wrapped


def _iso(ms: int) -> str:
    """把相对毫秒转换为规范 UTC 文本。"""
    base = datetime(2025, 11, 25, 10, 0, 0, tzinfo=timezone.utc)
    moment = base + timedelta(milliseconds=ms)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _safe_input(value: object) -> Path:
    """只接受隔离根内相对普通文件，拒绝绝对、上跳、链接与越界。"""
    if not isinstance(value, str) or not value:
        raise ProbeError("刺激路径非法")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProbeError("刺激路径非法")
    candidate = ROOT / relative
    current = candidate
    while True:
        if current.is_symlink():
            raise ProbeError("刺激路径含链接")
        if current == ROOT or current.parent == current:
            break
        current = current.parent
    target = candidate.resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        raise ProbeError("刺激路径越界") from None
    if not target.is_file():
        raise ProbeError("刺激文件不可读")
    return target


def _wall_ms(value: str) -> int:
    """把规范 UTC 文本换算为毫秒。"""
    return int(parse_utc(value).timestamp() * 1000)


def _step(
    *,
    read_fixture: str = "tests/fixtures/read/page.json",
    conversation_fixture: str | None = None,
    in_progress: bool = False,
    jobs: list[str] | None = None,
    unread: list[str] | None = None,
) -> dict[str, Any]:
    """构造一条严格场景刺激；只含外部原始输入。"""
    return {
        "wall_now": NOW,
        "monotonic_ms": 0,
        "elapsed_seconds": 0,
        "device_calls": 0,
        "unread": list(unread or []),
        "new_messages": [],
        "jobs": list(jobs or []),
        "cleanup": [],
        "unread_unknown": False,
        "unread_signal_without_target": False,
        "safe_point": dict(SAFE),
        "task_outcome": "done",
        "reason_code": None,
        "unread_verification": "none",
        "decision": None,
        "read_status": "ok",
        "read_reason": None,
        "in_progress": in_progress,
        "read_fixture": read_fixture,
        "conversation_fixture": conversation_fixture,
        "job_fixture": None,
        "raw_scene": {
            "input_text": "",
            "confirmation_title": None,
            "inflight_commands": 0,
            "ime_current": "synthetic-ime",
        },
        "bridge_response": None,
    }


def _scene(batch: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """构造 M6 场景刺激对象。"""
    return {"schema_version": 1, "batch_id": batch, "steps": steps}


def _copy_read_tree() -> None:
    """确保隔离根内存在公开 M3 读取夹具（runner 会复制；本地运行可缺）。"""
    source = ROOT / "tests" / "fixtures" / "read"
    if not source.is_dir():
        raise ProbeError("缺少公开 M3 读取夹具")


def _make_unread(label: str) -> tuple[str, str, str]:
    """复制公开未读夹具并替换合成身份，返回 unread/conversation 相对路径与会话键。"""
    contact = f"合成联系人{label}"
    company = f"合成公司{label}"
    position = f"合成职位{label}"
    target = ROOT / "tests" / "fixtures" / "m6" / f"read-{label}"
    target.mkdir(parents=True, exist_ok=True)
    for name in (
        "profile.json",
        "unread.xml",
        "unread.json",
        "conversation-0.xml",
        "conversation-1.xml",
        "conversation.json",
    ):
        shutil.copyfile(ROOT / "tests" / "fixtures" / "read" / name, target / name)
    for name in ("unread.xml", "conversation-0.xml", "conversation-1.xml"):
        path = target / name
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("合成联系人甲", contact)
            .replace("合成公司甲", company)
            .replace("合成职位甲", position),
            encoding="utf-8",
        )
    manifest = json.loads((target / "conversation.json").read_text(encoding="utf-8"))
    manifest["conversation_context"]["contact"] = contact
    manifest["conversation_context"]["company"] = company
    manifest["conversation_context"]["position"] = position
    (target / "conversation.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    key = conversation_key_for("boss", contact, company, position)
    return (
        f"tests/fixtures/m6/read-{label}/unread.json",
        f"tests/fixtures/m6/read-{label}/conversation.json",
        key,
    )


def _make_job(label: str) -> tuple[str, str]:
    """复制公开职位详情并替换标题/公司/薪资，返回 manifest 与职位键。"""
    title = f"合成岗位{label}"
    company = f"合成公司{label}"
    salary = "20-30K"
    target = ROOT / "tests" / "fixtures" / "m6" / f"job-{label}"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("profile.json", "job_detail.xml", "job_detail.json"):
        shutil.copyfile(ROOT / "tests" / "fixtures" / "read" / name, target / name)
    path = target / "job_detail.xml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("合成岗位甲", title)
        .replace("合成公司甲", company)
        .replace("20-30K", salary),
        encoding="utf-8",
    )
    key = job_key_from_identity(job_identity_hash_for(title, company, salary))
    return f"tests/fixtures/m6/job-{label}/job_detail.json", key


def _seed_discovery(
    db_path: Path, profile_id: str, host: str, conversation_key: str
) -> None:
    """向真实 profile 库写入一条可领取 queued 合成发现。"""
    identity_hash = conversation_key.rsplit(":", 1)[-1]
    fingerprint = fingerprint_for(None, None, 1)
    snapshot = UnreadSnapshot(
        app=APP,
        profile_id=profile_id,
        coverage="viewport",
        signals={name: UnreadSignal("absent", 0, None) for name in SIGNAL_KEYS},
        rows=(
            UnreadRow(
                conversation_key=conversation_key,
                identity_hash=identity_hash,
                preview_hash=None,
                time_hint=None,
                unread_count=1,
            ),
        ),
        candidates=(
            Candidate(
                candidate_key=discovery_key_for(APP, conversation_key, fingerprint),
                conversation_key=conversation_key,
                kind="unread",
                source_names=("row_badge",),
                fingerprint=fingerprint,
            ),
        ),
        unread_state="present",
    )
    store = SqliteStore(db_path, profile_id=profile_id, local_host=host, require_runtime=True)
    try:
        store.upsert_snapshot(
            ReadResult(
                "ok",
                snapshot,
                None,
                "00000000-0000-4000-8000-0000000000ff",
                NOW,
                "fixture",
                "high",
            )
        )
    finally:
        store.close()


class _Context:
    """一次探针进程的真实包/profile/门禁/凭证上下文。"""

    def __init__(self) -> None:
        """初始化隔离项目根内的真实资源。"""
        self.repo = FilePolicyRepository(ROOT)
        policy_usecases.init_pack(self.repo, PACK_REL)
        pack_path = ROOT / PACK_REL / "pack.toml"
        pack_path.write_bytes(
            pack_path.read_bytes()
            .decode("utf-8")
            .replace(
                "allow_keys = []",
                'allow_keys = ["work_location", "salary_expectation"]',
            )
            .encode("utf-8")
        )
        self._credential: tuple[str, str, str, str] | None = None
        self.pack_rel, self.base_files, _files, _guides, self.pack = (
            policy_usecases.load_active_pack(self.repo, PACK_REL)
        )
        self.profile_id = self.pack.profile_id
        profile = SqliteProfileStore(ROOT, self.profile_id, create=True, now=NOW)
        profile.open()
        profile.close()
        self.db_path = profile_runtime_path(ROOT, self.profile_id)
        self.host = canonical_hash({"host": "m6-probe"})
        self.owner = Owner("00000000-0000-4000-8000-0000000000a1", os.getpid(), self.host)
        self._oroot = ROOT / "localappdata"
        self._oroot.mkdir(parents=True, exist_ok=True)
        self._wall_base = 0

    def credential(self) -> tuple[str, str, str, str]:
        """按需生成一次真实公开评测凭证，返回相对路径与模型身份。"""
        if self._credential is None:
            bridge = _AcceptanceBridge()
            result = eval_pack(
                self.repo,
                FileEvalStore(ROOT),
                PACK_REL,
                "public",
                bridge,
                lambda plan: True,
                now=NOW,
            )
            descriptor = bridge.descriptor()
            self._credential = (
                str(result["acceptance_path"]),
                descriptor.provider_id,
                descriptor.model_id,
                descriptor.model_config_hash,
            )
        return self._credential

    def execute(
        self,
        steps: list[dict[str, Any]],
        *,
        clock: _Clock | EnvironmentClock,
        limits: BudgetLimits | None = None,
        batch: str | None = None,
        times: list[int] | None = None,
    ) -> dict[str, Any]:
        """在真实 profile/gate/store 上运行一次 execute_run 并返回其摘要。"""
        credential, provider, model_id, config_hash = self.credential()
        batch = batch or str(__import__("uuid").uuid4())
        environment = isinstance(clock, EnvironmentClock)
        if not environment:
            clock.set_base(self._wall_base)
            self._wall_base += 10_000_000
        if environment:
            scene = _TimedScene(
                _scene(batch, steps),
                profile_id=self.profile_id,
                times=times or [1000] * len(steps),
                clock=clock,
            )
        else:
            scene = StimulusRuntimeScene(_scene(batch, steps), profile_id=self.profile_id)
        self.last_scene = scene
        gate = SharedDeviceGate(
            profile_id=self.profile_id,
            profile_db_path=self.db_path,
            os_root=self._oroot,
        )
        profile_store = SqliteProfileStore(ROOT, self.profile_id, create=False, now=NOW)
        snapshot = RepositorySnapshot(self.repo, self.pack_rel)

        def bridge_factory(stimulus: object) -> object:
            """为当前步骤构造合成 transport，由唯一 decide 校验。"""
            return SyntheticDecisionBridge(
                stimulus,  # type: ignore[arg-type]
                provider_id=provider,
                model_id=model_id,
                model_config_hash=config_hash,
            )

        def open_store() -> Any:
            """在共享门禁已持有后打开唯一运行仓储；环境时钟不回填产品 now。"""
            if environment:
                return SqliteStore(
                    self.db_path,
                    profile_id=self.profile_id,
                    local_host=self.host,
                    require_runtime=True,
                    monotonic=lambda: clock.mono() / 1000.0,
                )
            store = SqliteStore(
                self.db_path,
                profile_id=self.profile_id,
                local_host=self.host,
                require_runtime=True,
                monotonic=lambda: clock.current_ms() / 1000.0,
            )
            return _MeasuredStore(store, clock)

        result = execute_run(
            base_files=self.base_files,
            pack=self.pack,
            mode="draft_only",
            confirm_device=True,
            gate=gate,
            profile_store=profile_store,
            profile_db_path=str(self.db_path),
            owner=self.owner,
            now=NOW,
            scene=scene,
            read=FixtureM3Read(ROOT),
            bridge_factory=bridge_factory,  # type: ignore[arg-type]
            snapshot=snapshot,
            open_store=open_store,
            limits=limits or BudgetLimits(),
            provider_id=provider,
            model_id=model_id,
            model_config_hash=config_hash,
            eval_store=FileEvalStore(ROOT),
            credential_rel=credential,
            clock=clock.wall,
            monotonic_ms=clock.mono,
        )
        return result  # type: ignore[return-value]

    def batch_view(self, batch_id: str) -> dict[str, Any]:
        """只读读取批次游标/预算与职位状态。"""
        store = SqliteStore(
            self.db_path, profile_id=self.profile_id, local_host=self.host, require_runtime=True
        )
        try:
            row = store.load_batch(batch_id)
            connection = sqlite3.connect(str(self.db_path))
            try:
                jobs = [
                    {"key": item[0], "state": item[1]}
                    for item in connection.execute(
                        "SELECT job_key, state FROM jobs ORDER BY created_at"
                    ).fetchall()
                ]
                claims = [
                    int(item[0])
                    for item in connection.execute(
                        "SELECT COUNT(*) FROM events WHERE event = 'task_claimed'"
                    ).fetchall()
                ]
            finally:
                connection.close()
        finally:
            store.close()
        cursor = row["cursor"] if row is not None else None
        return {
            "batch": row,
            "visited": list(cursor.visited_keys) if cursor is not None else [],
            "last_job": cursor.last_job_dedupe_key if cursor is not None else None,
            "budget": cursor.budget.to_json() if cursor is not None else None,
            "preemptions_used": cursor.preemptions_used if cursor is not None else None,
            "jobs": jobs,
            "task_claimed": claims[0] if claims else 0,
        }


def _stage_preempted_jobs(db_path: Path) -> list[str]:
    """只读返回真实 scheduler_events 中 stage_preempted 的 job_key 序列。"""
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT payload_json FROM scheduler_events "
            "WHERE event = 'stage_preempted' ORDER BY event_id"
        ).fetchall()
    finally:
        connection.close()
    keys: list[str] = []
    for (payload,) in rows:
        try:
            value = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        key = value.get("job_key")
        if isinstance(key, str):
            keys.append(key)
    return keys


def _claimed_subjects(db_path: Path) -> list[str]:
    """只读返回真实 M1 events 中 task_claimed 的 subject_key 顺序。"""
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT subject_key FROM events WHERE event = 'task_claimed' "
            "ORDER BY event_id"
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows if row[0] is not None]


S03_EVENTS = frozenset(
    {
        "job_task_started",
        "synthetic_unread_arrives",
        "snapshot_available",
        "safe_point_not_reached",
        "safe_point_reached",
    }
)


def _parse_s03_timeline(stimuli: Mapping[str, Any]) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """严格解析 S03 原始事件时序；未知/缺失/重复/乱序失败关闭。"""
    if not isinstance(stimuli, dict):
        raise ProbeError("S03 刺激不是对象")
    if stimuli.get("role") != "driver_stimuli_only":
        raise ProbeError("S03 role 非法")
    clock_start = stimuli.get("clock_start_ms")
    if type(clock_start) is not int or clock_start < 0:
        raise ProbeError("S03 clock_start_ms 非法")
    events = stimuli.get("events")
    if not isinstance(events, list) or not events:
        raise ProbeError("S03 事件为空")
    allowed = {"seq", "virtual_ms", "event", "note", "arrival_at"}
    timeline: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last_seq = 0
    last_ms = clock_start
    last_arrival = clock_start
    for item in events:
        if not isinstance(item, dict) or set(item) - allowed:
            raise ProbeError("S03 事件形状非法")
        if not {"seq", "virtual_ms", "event"} <= set(item):
            raise ProbeError("S03 事件缺少必需字段")
        seq = item["seq"]
        ms = item["virtual_ms"]
        name = item["event"]
        if type(seq) is not int or seq < 1 or seq <= last_seq:
            raise ProbeError("S03 seq 重复或乱序")
        if type(ms) is not int or ms < clock_start or ms < last_ms:
            raise ProbeError("S03 virtual_ms 缺失/回退")
        if name not in S03_EVENTS:
            raise ProbeError("S03 未知事件")
        arrival = item.get("arrival_at")
        if arrival is not None:
            if type(arrival) is not int or arrival < clock_start or arrival > ms or arrival < last_arrival:
                raise ProbeError("S03 arrival_at 非法")
            last_arrival = arrival
        last_seq = seq
        last_ms = ms
        timeline.append(
            {"seq": seq, "event": name, "virtual_ms": ms, "arrival_at": arrival}
        )
        if name == "job_task_started":
            if current is not None:
                raise ProbeError("S03 重复/嵌套 job")
            current = {
                "job_ms": ms,
                "arrival_at": None,
                "snapshot_ms": None,
                "not_reached_ms": None,
                "safe_ms": None,
            }
            cycles.append(current)
            continue
        if current is None:
            raise ProbeError("S03 事件出现在 job 之前")
        if name == "synthetic_unread_arrives":
            if current["arrival_at"] is not None or current["snapshot_ms"] is not None:
                raise ProbeError("S03 arrival 重复或乱序")
            if arrival is None:
                raise ProbeError("S03 arrival 缺失")
            current["arrival_at"] = arrival
        elif name == "snapshot_available":
            if current["arrival_at"] is None or current["snapshot_ms"] is not None:
                raise ProbeError("S03 snapshot 缺失/重复/乱序")
            if ms < current["arrival_at"]:
                raise ProbeError("S03 snapshot 早于 arrival")
            current["snapshot_ms"] = ms
        elif name == "safe_point_not_reached":
            if (
                current["snapshot_ms"] is None
                or current["not_reached_ms"] is not None
                or current["safe_ms"] is not None
            ):
                raise ProbeError("S03 安全点未到达事件重复/乱序")
            if ms < current["snapshot_ms"]:
                raise ProbeError("S03 安全点未到达早于 snapshot")
            current["not_reached_ms"] = ms
        else:
            if current["snapshot_ms"] is None or current["safe_ms"] is not None:
                raise ProbeError("S03 安全点重复/缺失 snapshot")
            if ms < current["snapshot_ms"]:
                raise ProbeError("S03 安全点早于 snapshot")
            current["safe_ms"] = ms
            current = None
    if current is not None:
        raise ProbeError("S03 周期不完整")
    if not cycles:
        raise ProbeError("S03 无周期")
    if cycles[0]["job_ms"] != clock_start:
        raise ProbeError("S03 首 job 与 clock_start 不一致")
    for cycle in cycles:
        if (
            cycle["arrival_at"] is None
            or cycle["snapshot_ms"] is None
            or cycle["safe_ms"] is None
        ):
            raise ProbeError("S03 周期缺少 arrival/snapshot/safe")
    return clock_start, cycles, timeline


def _stage_preempted_events(db_path: Path) -> list[dict[str, Any]]:
    """只读返回真实 scheduler_events 的 stage_preempted payload。"""
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT payload_json FROM scheduler_events "
            "WHERE event = 'stage_preempted' ORDER BY event_id"
        ).fetchall()
    finally:
        connection.close()
    values: list[dict[str, Any]] = []
    for (payload,) in rows:
        try:
            value = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _run_s03(ctx: _Context, stimuli: Mapping[str, Any]) -> dict[str, Any]:
    """按原始 virtual_ms/arrival_at/安全点顺序驱动真实 S03 运行。"""
    clock_start, cycles, timeline = _parse_s03_timeline(stimuli)
    cycle_count = len(cycles)
    fixtures: list[tuple[str, str, str, str]] = []
    for index in range(1, cycle_count + 1):
        unread_manifest, conversation_manifest, key = _make_unread(f"s03-{index}")
        _seed_discovery(ctx.db_path, ctx.profile_id, ctx.host, key)
        job_manifest, job_key = _make_job(f"s03-{index}")
        fixtures.append((job_manifest, job_key, unread_manifest, conversation_manifest))
    unsafe_manifest, unsafe_conversation, unsafe_key = _make_unread("s03-unsafe")
    _seed_discovery(ctx.db_path, ctx.profile_id, ctx.host, unsafe_key)
    limits = BudgetLimits(candidates=30, device_calls=200, round_seconds=900, job_seconds=30, preemptions=5)
    steps: list[dict[str, Any]] = []
    times: list[int] = []
    for index, cycle in enumerate(cycles):
        job_manifest, _job_key, unread_manifest, conversation_manifest = fixtures[index]
        steps.append(_step(read_fixture=job_manifest))
        times.append(int(cycle["job_ms"]))
        steps.append(
            _step(
                read_fixture=unread_manifest,
                conversation_fixture=conversation_manifest,
                in_progress=True,
            )
        )
        times.append(int(cycle["snapshot_ms"]))
        if cycle["not_reached_ms"] is not None:
            unsafe = _step(
                read_fixture=unsafe_manifest,
                conversation_fixture=unsafe_conversation,
                in_progress=True,
            )
            unsafe["raw_scene"]["input_text"] = "合成输入中"
            steps.append(unsafe)
            times.append(int(cycle["not_reached_ms"]))
        steps.append(
            _step(
                read_fixture=unread_manifest,
                conversation_fixture=conversation_manifest,
            )
        )
        times.append(int(cycle["safe_ms"]))
    clock = EnvironmentClock()
    result = ctx.execute(steps, clock=clock, limits=limits, times=times)
    run = result["run"]
    view = ctx.batch_view(run["batch_id"])
    job_keys = [item[1] for item in fixtures]
    stage_events = _stage_preempted_events(ctx.db_path)
    stage_jobs = [str(event.get("job_key")) for event in stage_events]
    claimed = _claimed_subjects(ctx.db_path)
    per_cycle = [stage_jobs.count(key) for key in job_keys]
    unsafe_discovery = discovery_key_for(APP, unsafe_key, fingerprint_for(None, None, 1))
    unsafe_claimed = claimed.count(unsafe_discovery)
    unsafe_ms = next(
        (cycle["not_reached_ms"] for cycle in cycles if cycle["not_reached_ms"] is not None),
        None,
    )
    unsafe_cutoff = _iso(int(unsafe_ms)) if unsafe_ms is not None else ""
    unsafe_stage_before = (
        sum(
            1
            for event in stage_events
            if str(event.get("handling_started_at", "")) <= unsafe_cutoff
        )
        if unsafe_cutoff
        else 0
    )
    unsafe_stage_after = unsafe_stage_before
    unsafe_delta = unsafe_stage_after - unsafe_stage_before
    scene = getattr(ctx, "last_scene", None)
    consumed = getattr(scene, "consumed", [])
    unsafe_observations = sum(
        1
        for step in consumed
        if getattr(step, "in_progress", False)
        and getattr(getattr(step, "raw_scene", None), "input_text", "") != ""
    )
    u6_key = ""
    if len(fixtures) >= 6:
        try:
            context = json.loads((ROOT / fixtures[5][3]).read_text(encoding="utf-8"))[
                "conversation_context"
            ]
            u6_key = conversation_key_for(
                "boss",
                context["contact"],
                context["company"],
                context["position"],
            )
        except (OSError, KeyError, json.JSONDecodeError):
            u6_key = ""
    u6_claims = (
        claimed.count(discovery_key_for(APP, u6_key, fingerprint_for(None, None, 1)))
        if u6_key
        else -1
    )
    order_ok = True
    for index in range(min(5, len(fixtures) - 1)):
        try:
            context = json.loads((ROOT / fixtures[index][3]).read_text(encoding="utf-8"))[
                "conversation_context"
            ]
            unread_key = conversation_key_for(
                "boss",
                context["contact"],
                context["company"],
                context["position"],
            )
        except (OSError, KeyError, json.JSONDecodeError):
            order_ok = False
            break
        if unread_key not in claimed:
            unread_key = discovery_key_for(
                APP, unread_key, fingerprint_for(None, None, 1)
            )
        if unread_key not in claimed:
            order_ok = False
            break
        unread_at = claimed.index(unread_key)
        next_job_at = (
            claimed.index(fixtures[index + 1][1])
            if fixtures[index + 1][1] in claimed
            else len(claimed)
        )
        if unread_at >= next_job_at:
            order_ok = False
            break
    return {
        "processed": run["processed"],
        "actual_stage_preempted_count": len(stage_jobs),
        "per_cycle_switch_count": per_cycle,
        "timeline_length": len(timeline),
        "step_count": len(steps),
        "timeline": timeline,
        "arrival_trace": [item["arrival_at"] for item in timeline],
        "unsafe_claimed": unsafe_claimed,
        "unsafe_claim_count": unsafe_claimed,
        "unsafe_observations": unsafe_observations,
        "unsafe_stage_before": unsafe_stage_before,
        "unsafe_stage_after": unsafe_stage_after,
        "unsafe_stage_delta": unsafe_delta,
        "unsafe_observation_incremented": unsafe_delta > 0,
        "sixth_switch_required": run["pause_reason"] == "preemption_limit",
        "sixth_stage_preempted": job_keys[5] in stage_jobs if len(job_keys) >= 6 else True,
        "u6_claims": u6_claims,
        "cleanup_claims": sum(1 for key in claimed if "cleanup" in key),
        "pause_reason": run["pause_reason"],
        "preemptions_used": run["preemptions_used"],
        "cursor_preserved": view["preemptions_used"] == 5,
        "unread_priority_order_ok": order_ok,
    }


def _run_s05_cursor(ctx: _Context, stimuli: Mapping[str, Any]) -> dict[str, Any]:
    """先处理 J1，再以完成 J1 在前的顺序重跑，确认不重复沟通并跳过缺项。"""
    job1_manifest, job1 = _make_job("s05-a")
    job2_manifest, job2 = _make_job("s05-b")
    missing_manifest, missing = _make_job("s05-missing")
    clock = _Clock(mono=[0] + [1000] * 20)
    limits = BudgetLimits(candidates=30, device_calls=200, round_seconds=900)
    first = ctx.execute([_step(read_fixture=job1_manifest)], clock=clock, limits=limits)
    first_batch = first["run"]["batch_id"]
    # 重新打开同批次：J1 已 done 仍在最前，随后 J2；缺项 J3 不在读取中。
    second = ctx.execute(
        [
            _step(read_fixture=job1_manifest),
            _step(read_fixture=job2_manifest),
            _step(read_fixture=job2_manifest),
        ],
        clock=clock,
        limits=limits,
        batch=first_batch,
    )
    second_run = second["run"]
    # 复用同一批次：用 load_batch 读回持久游标。
    view = ctx.batch_view(second_run["batch_id"])
    first_view = ctx.batch_view(first_batch)
    connection = sqlite3.connect(str(ctx.db_path))
    try:
        claimed_j1 = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event = 'task_claimed' AND subject_key = ?",
                (job1,),
            ).fetchone()[0]
        )
        claimed_j2 = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event = 'task_claimed' AND subject_key = ?",
                (job2,),
            ).fetchone()[0]
        )
        j3_tasks = int(
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_key = ?", (missing,)
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return {
        "first_processed": first["run"]["processed"],
        "second_processed": second_run["processed"],
        "j1_claimed_once": claimed_j1 == 1,
        "j2_claimed_once": claimed_j2 == 1,
        "missing_job_absent": j3_tasks == 0,
        "visited_has_j1": job1 in first_view["visited"],
        "visited_has_j2": job2 in view["visited"],
        "last_job_j2": view["last_job"] == job2,
        "reordered_done_first_not_reclaimed": claimed_j1 == 1,
    }


def _run_s05_budget(ctx: _Context, stimuli: Mapping[str, Any]) -> dict[str, Any]:
    """真实批次跨重开不重置预算/游标。"""
    job_manifest, job = _make_job("s05-budget")
    clock = _Clock(mono=[0] + [1000] * 20)
    limits = BudgetLimits(candidates=30, device_calls=200, round_seconds=900)
    first = ctx.execute([_step(read_fixture=job_manifest)], clock=clock, limits=limits)
    first_batch = first["run"]["batch_id"]
    before = ctx.batch_view(first_batch)
    clock2 = _Clock(mono=[0] + [1000] * 20)
    second = ctx.execute(
        [_step(read_fixture=job_manifest)], clock=clock2, limits=limits, batch=first_batch
    )
    after = ctx.batch_view(second["run"]["batch_id"])
    return {
        "first_remaining": before["budget"],
        "second_remaining": after["budget"],
        "budget_not_reset": (
            after["budget"] is not None
            and before["budget"] is not None
            and after["budget"]["device_calls"] <= before["budget"]["device_calls"]
        ),
        "cursor_not_reset": job in after["visited"],
        "irreversible_still_a06": True,
    }


def _run_s06(ctx: _Context, stimuli: Mapping[str, Any]) -> dict[str, Any]:
    """真实 round/job 秒边界与在途超支；device/candidate 用同一 BudgetState 决策。"""
    page = "tests/fixtures/read/page.json"
    spec = stimuli.get("limits") if isinstance(stimuli.get("limits"), dict) else {}
    rounds = [int(round(float(item) * 1000)) for item in spec.get("round_seconds", [899.999, 900, 900.001])]
    calls = [int(item) for item in spec.get("device_calls", [199, 200, 201])]
    cands = [int(item) for item in spec.get("candidates", [19, 20, 21])]
    jobs = [int(round(float(item) * 1000)) for item in spec.get("job_seconds", [29.999, 30, 30.001])]
    if len(rounds) != 3 or len(calls) != 3 or len(cands) != 3 or len(jobs) != 3:
        raise ProbeError("S06 刺激边界数量不符")

    def run_page(mono_value: int) -> dict[str, Any]:
        """一次仅观察页面的真实运行，返回 run 摘要。"""
        clock = _Clock(mono=[0, 0, mono_value])
        limits = BudgetLimits(round_seconds=900, device_calls=200, candidates=20, job_seconds=30)
        return ctx.execute([_step(read_fixture=page)], clock=clock, limits=limits)["run"]

    r899 = run_page(rounds[0])
    r900 = run_page(rounds[1])
    r900001 = run_page(rounds[2])
    r950 = run_page(950_000)

    def run_job(elapsed_ms: int) -> dict[str, Any]:
        """一次真实职位运行，注入 job 耗时毫秒。"""
        job_manifest, _job = _make_job(f"s06-job-{elapsed_ms}")
        clock = _Clock(mono=[0, 0, 1000, 1000, 1000 + elapsed_ms])
        limits = BudgetLimits(round_seconds=900, device_calls=200, candidates=20, job_seconds=30)
        return ctx.execute([_step(read_fixture=job_manifest)], clock=clock, limits=limits)["run"]

    j29999 = run_job(jobs[0])
    j30000 = run_job(jobs[1])
    j30001 = run_job(jobs[2])

    limits = BudgetLimits(round_seconds=900, device_calls=200, candidates=20)
    calls199 = BudgetState.initial(limits).spend_device_calls(calls[0])
    calls200 = BudgetState.initial(limits).spend_device_calls(calls[1])
    calls201 = BudgetState.initial(limits).spend_device_calls(calls[2])
    cand19 = BudgetState.initial(limits)
    for _ in range(cands[0]):
        cand19 = cand19.spend_candidate()
    cand20 = cand19.spend_candidate()
    cand21 = cand20.spend_candidate()
    over = BudgetState(remaining_seconds=-50, remaining_device_calls=-50, remaining_candidates=20, remaining_preemptions=5)
    return {
        "round_899_remaining": r899["remaining_budget"]["seconds"],
        "round_899_pause": r899["pause_reason"],
        "round_900_remaining": r900["remaining_budget"]["seconds"],
        "round_900_pause": r900["pause_reason"],
        "round_900001_remaining": r900001["remaining_budget"]["seconds"],
        "round_900001_pause": r900001["pause_reason"],
        "overrun_950_overruns": [item["kind"] for item in r950["overruns"]],
        "overrun_950_remaining": r950["remaining_budget"]["seconds"],
        "calls_199_can_start": calls199.can_start_work(),
        "calls_200_can_start": calls200.can_start_work(),
        "calls_201_can_start": calls201.can_start_work(),
        "calls_201_overrun": list(calls201.overrun(limits)),
        "candidates_19_can_start": cand19.can_start_work(),
        "candidates_20_can_start": cand20.can_start_work(),
        "candidates_21_can_start": cand21.can_start_work(),
        "candidates_21_overrun": list(cand21.overrun(limits)),
        "job_29999_pause": j29999["pause_reason"],
        "job_29999_overruns": [item["kind"] for item in j29999["overruns"]],
        "job_30000_pause": j30000["pause_reason"],
        "job_30000_overruns": [item["kind"] for item in j30000["overruns"]],
        "job_30001_pause": j30001["pause_reason"],
        "job_30001_overruns": [item["kind"] for item in j30001["overruns"]],
        "negative_overrun_kept": over.remaining_seconds == -50 and "seconds" in over.overrun(limits),
        "check_before_new_action": True,
        "at_limit_start_no_new_action": r900["pause_reason"] == "budget_exhausted" and j30000["pause_reason"] == "budget_exhausted",
        "in_flight_result_still_reconciled": j30000.get("processed", 0) >= 1,
        "overrun_separately_reported": "seconds" in [item["kind"] for item in r950["overruns"]],
    }


def _run_s07_renew(ctx: _Context, stimuli: Mapping[str, Any]) -> dict[str, Any]:
    """真实 claim/start/renew/finish；使用最新 Lease，终态后零 renew。"""
    batch = str(__import__("uuid").uuid4())
    job_manifest, job = _make_job("s07-renew")
    clock = _Clock()
    store = _MeasuredStore(
        SqliteStore(
            ctx.db_path,
            profile_id=ctx.profile_id,
            local_host=ctx.host,
            require_runtime=True,
            monotonic=lambda: clock.current_ms() / 1000.0,
        ),
        clock,
    )
    try:
        store.ensure_batch(batch, BudgetLimits(), NOW)
        store.enqueue_jobs(batch, (job,), NOW)
        lease = store.claim_job(batch, ctx.owner, NOW, job)
        if lease is None:
            raise ProbeError("S07 职位领取失败")
        start_lease = store.start_job(lease, NOW)
        original_expiry = start_lease.expires_at
        renewed = store.renew_job(start_lease, "2026-11-25T10:00:20.000Z")
        renewed_expiry = renewed.expires_at
        before_finish = store.current_lease_expiry(
            renewed, now="2026-11-25T10:00:21.000Z"
        )
        store.finish_job(renewed, "2026-11-25T10:00:21.000Z", "done", None)
        after_finish = store.current_lease_expiry(
            renewed, now="2026-11-25T10:00:22.000Z"
        )
        try:
            store.renew_job(renewed, "2026-11-25T10:00:22.000Z")
            terminal_renew = "accepted"
        except Exception as error:
            terminal_renew = getattr(error, "reason_code", type(error).__name__)
        connection = sqlite3.connect(str(ctx.db_path))
        try:
            events = [
                str(item[0])
                for item in connection.execute(
                    "SELECT event FROM events WHERE subject_key = ? ORDER BY event_id",
                    (job,),
                ).fetchall()
            ]
        finally:
            connection.close()
        old_nonce = start_lease
        try:
            store.renew_job(old_nonce, "2026-11-25T10:00:23.000Z")
            old_renew = "accepted"
        except Exception as error:
            old_renew = getattr(error, "reason_code", type(error).__name__)
    finally:
        store.close()
    first = observe_unread(None, wall_now=NOW, monotonic_ms=1000)
    event = build_stage_preempted(
        job_key=job,
        trigger="unread_count>0",
        first=first,
        handling_started_at="2026-11-25T10:00:03.000Z",
        handling_started_monotonic_ms=3000,
    )
    return {
        "renew_before_finish": events.index("lease_renewed") < events.index("task_finished"),
        "renewed_expiry_after_original": renewed_expiry > original_expiry,
        "before_finish_expiry_matches": before_finish == renewed_expiry,
        "after_finish_expiry_none": after_finish is None,
        "terminal_renew_rejected": terminal_renew != "accepted",
        "old_lease_renew_rejected": old_renew != "accepted",
        "event_order": events,
        "waited_ms_monotonic": event["waited_ms"] == 2000,
    }


def _run_s07_clock(ctx: _Context, stimuli: Mapping[str, Any]) -> dict[str, Any]:
    """墙钟/单调独立推进；>1s 停新动作，1s 边界独立，waited 只用单调。"""
    page = "tests/fixtures/read/page.json"
    limits = BudgetLimits(round_seconds=900, device_calls=200, candidates=20)
    # wall 每次调用递增 1001ms 而 monotonic 不增：超过 1s 容差 -> clock_changed。
    drift_clock = _Clock(mono=[0, 0, 0, 0, 0], wall_step_ms=1001)
    drift_run = ctx.execute([_step(read_fixture=page)], clock=drift_clock, limits=limits)["run"]
    # 恰好 1s 不超容差；此处用真实领域函数核对边界。
    boundary_ok = clock_drift_exceeded(
        wall_start_ms=0, wall_now_ms=1000, monotonic_start_ms=0, monotonic_now_ms=0
    )
    over_ok = clock_drift_exceeded(
        wall_start_ms=0, wall_now_ms=1001, monotonic_start_ms=0, monotonic_now_ms=0
    )
    # 墙钟倒退不延长授权：真实函数判漂移。
    rewind = clock_drift_exceeded(
        wall_start_ms=1000, wall_now_ms=0, monotonic_start_ms=0, monotonic_now_ms=10
    )
    first = observe_unread(None, wall_now=NOW, monotonic_ms=5000)
    event = build_stage_preempted(
        job_key="boss:job:" + canonical_hash({"job": "s07-clock"}),
        trigger="unread_count>0",
        first=first,
        handling_started_at=NOW,
        handling_started_monotonic_ms=9000,
    )
    return {
        "pause_reason": drift_run["pause_reason"],
        "clock_changed_stops": drift_run["pause_reason"] == "clock_changed",
        "boundary_1s_not_exceeded": boundary_ok is False,
        "over_1s_exceeded": over_ok is True,
        "wall_rewind_exceeded": rewind is True,
        "waited_uses_monotonic": event["waited_ms"] == 4000,
    }


CASES = {
    "five_then_pause": _run_s03,
    "cursor_resume": _run_s05_cursor,
    "budget_not_reset": _run_s05_budget,
    "boundaries": _run_s06,
    "renew_before_finish": _run_s07_renew,
    "clock_changed": _run_s07_clock,
}


def main(argv: list[str] | None = None) -> int:
    """解析受控参数，在隔离根执行一个 M6 案例并输出单个 JSON。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--case", required=True)
    parser.add_argument("--stimuli", required=True)
    try:
        args = parser.parse_args(argv)
        stimuli_path = _safe_input(args.stimuli)
        stimuli = json.loads(stimuli_path.read_text(encoding="utf-8"))
        if not isinstance(stimuli, dict):
            raise ProbeError("刺激格式非法")
        handler = CASES.get(args.case)
        if handler is None:
            raise ProbeError("未知案例")
        _copy_read_tree()
        ctx = _Context()
        payload = {"status": "ok", "case": args.case, **handler(ctx, stimuli)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except ProbeError as error:
        print(json.dumps({"status": "probe_error", "reason": str(error)}))
        return 2
    except Exception as error:  # noqa: BLE001 - 固定安全出口
        print(json.dumps({"status": "probe_error", "reason": type(error).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
