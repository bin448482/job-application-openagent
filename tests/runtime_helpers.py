"""提供 M6 运行测试共用的临时项目、合成 profile 发现、场景夹具与 CLI 辅助。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.adapters.sqlite.profile_store import profile_runtime_path
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
    conversation_key_from_identity,
    discovery_key_for,
    fingerprint_for,
    identity_hash_for,
)
from tests import action_helpers, policy_helpers

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-25T10:00:00.000Z"
BATCH = "00000000-0000-4000-8000-0000000000aa"


def runtime_project():
    """创建带公开策略、合成凭证与 M6 场景目录的临时项目根。"""
    tmp, root = action_helpers.temp_project()
    action_helpers.init_pack(root)
    (root / "tests" / "fixtures" / "runtime").mkdir(parents=True, exist_ok=True)
    return tmp, root


def required_owner() -> tuple[str, str, str]:
    """返回与验收桥一致的 provider/model/model_config_hash。"""
    return action_helpers.model_identity()


def prepare(root: Path, os_root: Path) -> tuple[int, dict[str, object] | None, str]:
    """显式准备 profile v3 库，返回 CLI 结果。"""
    return policy_helpers.run_cli(
        root,
        ["run", "prepare", "--pack", "prompts/local/demo"],
        os_root=os_root,
    )


def credential(root: Path) -> tuple[str, str]:
    """生成当前包验收凭证，返回相对路径与 model_config_hash。"""
    return action_helpers.credential(root)


def conversation_key(contact: str, company: str, position: str) -> str:
    """按既有身份算法生成合成会话键。"""
    return conversation_key_from_identity(
        APP, identity_hash_for(contact, company, position)
    )


def seed_discovery(
    root: Path,
    key: str,
    *,
    kind: str = "unread",
    profile_id: str = "demo",
    observed_at: str = NOW,
    unread_count: int = 1,
) -> None:
    """向已准备 profile 库插入一条可定位合成发现。"""
    path = profile_runtime_path(root, profile_id)
    identity_hash = key.rsplit(":", 1)[-1]
    fingerprint = fingerprint_for(None, None, unread_count)
    candidate_key = discovery_key_for(APP, key, fingerprint)
    snapshot = UnreadSnapshot(
        app=APP,
        profile_id=profile_id,
        coverage="viewport",
        signals={
            name: UnreadSignal("absent", 0, None)
            for name in ("row_badge", "new_greeting", "tab_badge", "preview_delta")
        },
        rows=(
            UnreadRow(
                conversation_key=key,
                identity_hash=identity_hash,
                preview_hash=None,
                time_hint=None,
                unread_count=unread_count,
            ),
        ),
        candidates=(
            Candidate(
                candidate_key=candidate_key,
                conversation_key=key,
                kind=kind,
                source_names=("row_badge",),
                fingerprint=fingerprint,
            ),
        ),
        unread_state="present",
    )
    result = ReadResult(
        status="ok",
        value=snapshot,
        reason_code=None,
        snapshot_id=str(uuid.uuid4()),
        observed_at=observed_at,
        source="fixture",
        confidence="high",
    )
    store = SqliteStore(path, profile_id=profile_id, require_runtime=True)
    try:
        store.upsert_snapshot(result)
    finally:
        store.close()


def safe_point(**overrides: object) -> dict[str, bool]:
    """构造七项全部成立的安全点刺激。"""
    values = {
        "list_returned": True,
        "input_empty": True,
        "confirmation_closed": True,
        "no_inflight_command": True,
        "no_pending_action": True,
        "action_state_committed": True,
        "checkpoint_committed": True,
    }
    values.update({name: bool(value) for name, value in overrides.items()})
    return values


def step(
    *,
    wall_now: str = "2026-11-25T10:00:01.000Z",
    monotonic_ms: int = 1000,
    elapsed_seconds: int = 1,
    device_calls: int = 1,
    unread: tuple[str, ...] = (),
    new_messages: tuple[str, ...] = (),
    jobs: tuple[str, ...] = (),
    cleanup: tuple[str, ...] = (),
    unread_unknown: bool = False,
    unread_signal_without_target: bool = False,
    safe: dict[str, bool] | None = None,
    outcome: str = "done",
    reason: str | None = None,
    verification: str = "none",
    decision: dict[str, object] | None = None,
    read_status: str = "ok",
    read_reason: str | None = None,
    in_progress: bool = False,
) -> dict[str, object]:
    """构造一条严格场景刺激；缺省为可成功完成的普通步骤。"""
    return {
        "wall_now": wall_now,
        "monotonic_ms": monotonic_ms,
        "elapsed_seconds": elapsed_seconds,
        "device_calls": device_calls,
        "unread": list(unread),
        "new_messages": list(new_messages),
        "jobs": list(jobs),
        "cleanup": list(cleanup),
        "unread_unknown": unread_unknown,
        "unread_signal_without_target": unread_signal_without_target,
        "safe_point": safe_point() if safe is None else safe,
        "task_outcome": outcome,
        "reason_code": reason,
        "unread_verification": verification,
        "decision": decision,
        "read_status": read_status,
        "read_reason": read_reason,
        "in_progress": in_progress,
    }


def decision_stimulus(
    *,
    reply: str = "合成草稿正文。",
    tier: str = "C",
    evidence_hash: str | None = None,
) -> dict[str, object]:
    """构造模型决策刺激，不是断言答案或授权。"""
    return {
        "intent": "interested",
        "sub_intents": ["greeting"],
        "policy_tier": tier,
        "reply": reply,
        "evidence_hash": evidence_hash or canonical_hash({"synthetic": "evidence"}),
    }


def write_scenario(
    root: Path,
    rel: str,
    *,
    batch_id: str = BATCH,
    steps: list[dict[str, object]],
    extra: dict[str, object] | None = None,
) -> str:
    """把合成场景写入临时项目根，返回相对路径。"""
    spec: dict[str, object] = {
        "schema_version": 1,
        "batch_id": batch_id,
        "steps": steps,
    }
    if extra:
        spec.update(extra)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return rel


def execute(
    root: Path,
    os_root: Path,
    fixture_rel: str,
    *,
    mode: str = "draft_only",
    credential_rel: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    model_config_hash: str | None = None,
    confirm_device: bool = True,
) -> tuple[int, dict[str, object] | None, str]:
    """运行 run execute CLI，返回退出码、JSON 与 stderr。"""
    argv = [
        "run",
        "execute",
        "--pack",
        "prompts/local/demo",
        "--mode",
        mode,
        "--fixture",
        fixture_rel,
    ]
    if confirm_device:
        argv.append("--confirm-device")
    for name, value in (
        ("--credential", credential_rel),
        ("--provider", provider),
        ("--model-id", model_id),
        ("--model-config-hash", model_config_hash),
    ):
        if value is not None:
            argv.extend([name, value])
    return policy_helpers.run_cli(root, argv, os_root=os_root)


def row_counts(root: Path, profile_id: str = "demo") -> dict[str, int]:
    """只读统计 M6 业务表行数，供 read_only 零写断言。"""
    import sqlite3

    path = profile_runtime_path(root, profile_id)
    connection = sqlite3.connect(str(path))
    try:
        names = (
            "jobs",
            "job_batches",
            "drafts",
            "runtime_decisions",
            "scheduler_events",
            "action_records",
        )
        return {
            name: int(
                connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            )
            for name in names
        }
    finally:
        connection.close()


def owner() -> Owner:
    """返回一个规范合成租约持有者。"""
    return Owner(
        id="11111111-1111-4111-8111-111111111111",
        pid=4242,
        host=canonical_hash({"host": "synthetic"}),
    )
