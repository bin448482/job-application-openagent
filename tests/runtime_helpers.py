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
NOW = "2026-11-24T00:00:00.000Z"
BATCH = "00000000-0000-4000-8000-0000000000aa"


def runtime_project():
    """创建带公开策略、合成凭证与 M6 场景目录的临时项目根。"""
    tmp, root = action_helpers.temp_project()
    action_helpers.init_pack(root)
    import shutil
    shutil.copytree(ROOT / "tests" / "fixtures" / "read", root / "tests" / "fixtures" / "read")
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
    read_fixture: str | None = None,
    conversation_fixture: str | None = None,
    job_fixture: str | None = None,
    raw_scene: dict[str, object] | None = None,
    bridge_response: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造一条严格场景刺激；缺省为可成功完成的普通步骤。"""
    if read_fixture is None:
        if unread or new_messages:
            read_fixture = "tests/fixtures/read/unread.json"
        elif jobs:
            read_fixture = "tests/fixtures/read/job_list.json"
        else:
            read_fixture = "tests/fixtures/read/page.json"
    if conversation_fixture is None and unread:
        conversation_fixture = "tests/fixtures/read/conversation.json"
    if raw_scene is None:
        raw_scene = {
            "input_text": "",
            "confirmation_title": None,
            "inflight_commands": 0,
            "ime_current": "synthetic-ime-default",
        }
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
        "read_fixture": read_fixture,
        "conversation_fixture": conversation_fixture,
        "job_fixture": job_fixture,
        "raw_scene": raw_scene,
        "bridge_response": bridge_response,
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


def _refresh_conversation_observations(root: Path, fixture_rel: str) -> None:
    """在 CLI 合成运行前把场景引用的会话 manifest 采样时间更新为当前墙钟。

    坏 JSON/不可读路径不在这里报错，原样留给 CLI 夹具校验拒绝。
    """
    from datetime import datetime, timezone

    from app.domain.models import format_utc

    path = (root / fixture_rel).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        return
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    steps = spec.get("steps") if isinstance(spec, dict) else None
    if not isinstance(steps, list):
        return
    observed_at = format_utc(datetime.now(timezone.utc))
    for step in steps:
        if not isinstance(step, dict):
            continue
        rel = step.get("conversation_fixture")
        if not isinstance(rel, str):
            continue
        candidate = (root / rel).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            continue
        try:
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or "observed_at" not in manifest:
            continue
        manifest["observed_at"] = observed_at
        try:
            candidate.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            continue


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
    refresh_conversations: bool = False,
) -> tuple[int, dict[str, object] | None, str]:
    """运行 run execute CLI，返回退出码、JSON 与 stderr。

    默认不改任何原始时间输入；只有显式 `refresh_conversations=True` 的阳性
    采集用例才在运行前把会话采样时间更新为当前墙钟。
    """
    if refresh_conversations:
        _refresh_conversation_observations(root, fixture_rel)
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

def m3_unread_key() -> str:
    """返回公开 M3 unread fixture 的会话键。"""
    from app.adapters.boss.read import load_and_read_fixture

    result = load_and_read_fixture(
        ROOT / "tests/fixtures/read/unread.json",
        "00000000-0000-4000-8000-0000000000aa",
    )
    if result.status != "ok":
        raise AssertionError(result.reason_code)
    rows = [row.conversation_key for row in result.value.rows if row.conversation_key]
    if not rows:
        raise AssertionError("unread fixture has no key")
    return rows[0]


def m3_job_keys() -> tuple[str, ...]:
    """返回公开 M3 job_list fixture 的全部职位键。"""
    from app.adapters.boss.read import load_and_read_fixture

    result = load_and_read_fixture(
        ROOT / "tests/fixtures/read/job_list.json",
        "00000000-0000-4000-8000-0000000000aa",
    )
    if result.status != "ok":
        raise AssertionError(result.reason_code)
    return tuple(card.job_key for card in result.value.cards if card.job_key)


def make_unread_fixture(root: Path, label: str) -> tuple[str, str]:
    """复制 M3 unread/conversation fixture 并替换合成身份，返回 manifest 与会话键。"""
    import shutil

    contact = f"合成联系人{label}"
    company = f"合成公司{label}"
    position = f"合成职位{label}"
    target = root / "tests" / "fixtures" / "runtime" / f"read-{label}"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("profile.json", "unread.xml", "unread.json",
                 "conversation-0.xml", "conversation-1.xml", "conversation.json"):
        shutil.copyfile(ROOT / "tests/fixtures/read" / name, target / name)
    for name in ("unread.xml", "conversation-0.xml", "conversation-1.xml"):
        path = target / name
        text = path.read_text(encoding="utf-8")
        text = (
            text.replace("合成联系人甲", contact)
            .replace("合成公司甲", company)
            .replace("合成职位甲", position)
        )
        path.write_text(text, encoding="utf-8")
    manifest = json.loads((target / "conversation.json").read_text(encoding="utf-8"))
    manifest["conversation_context"]["contact"] = contact
    manifest["conversation_context"]["company"] = company
    manifest["conversation_context"]["position"] = position
    (target / "conversation.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return (
        f"tests/fixtures/runtime/read-{label}/unread.json",
        conversation_key(contact, company, position),
    )


def make_unknown_unread_fixture(root: Path, label: str) -> tuple[str, str]:
    """复制 M3 unread fixture 并移除角标，使实际读取得到 unknown 未读状态。"""
    import shutil

    contact = f"合成联系人{label}"
    company = f"合成公司{label}"
    position = f"合成职位{label}"
    target = root / "tests" / "fixtures" / "runtime" / f"unknown-{label}"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("profile.json", "unread.xml", "unread.json"):
        shutil.copyfile(ROOT / "tests" / "fixtures" / "read" / name, target / name)
    path = target / "unread.xml"
    text = path.read_text(encoding="utf-8")
    text = (
        text.replace("合成联系人甲", contact)
        .replace("合成公司甲", company)
        .replace("合成职位甲", position)
        .replace(
            '        <node text="1" class="android.widget.TextView" '
            'bounds="[20,220][80,280]" />\n',
            "",
        )
    )
    path.write_text(text, encoding="utf-8")
    return (
        f"tests/fixtures/runtime/unknown-{label}/unread.json",
        conversation_key(contact, company, position),
    )


def make_new_message_fixture(
    root: Path, label: str, *, profile_id: str = "demo"
) -> tuple[str, str]:
    """复制 M2 合成传感器夹具，形成实际解析可产生的 new_message 候选。"""
    import shutil

    target = root / "tests" / "fixtures" / "sensor" / f"newmsg-{label}"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("profile.json", "positive.xml"):
        shutil.copyfile(ROOT / "tests" / "fixtures" / "sensor" / name, target / name)
    profile_path = target / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["profile_id"] = profile_id
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    xml_path = target / "positive.xml"
    text = xml_path.read_text(encoding="utf-8")
    text = (
        text.replace("合成联系人", "合成联系人甲")
        .replace("合成公司", "合成公司甲")
        .replace("合成职位", "合成职位甲")
        .replace(
            '      <node text="1" class="android.widget.TextView" '
            'bounds="[900,40][960,90]" />\n',
            "",
        )
    )
    xml_path.write_text(text, encoding="utf-8")
    key = conversation_key("合成联系人甲", "合成公司甲", "合成职位甲")
    manifest = {
        "profile_path": "profile.json",
        "xml_paths": ["positive.xml"],
        "app_version": "14.200.0-synthetic",
        "observed_at": "2026-11-24T00:00:00.000Z",
        "baseline_rows": [
            {
                "conversation_key": key,
                "preview_hash": canonical_hash({"old": "preview"}),
                "time_hint": "09:00",
            }
        ],
        "coverage_proof": {
            "kind": "viewport",
            "top_proven": False,
            "bottom_proven": False,
        },
    }
    manifest_path = target / "new_message.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return f"tests/fixtures/sensor/newmsg-{label}/new_message.json", key


def seed_new_message(
    root: Path, label: str, *, profile_id: str = "demo"
) -> str:
    """通过实际 M2 解析与生产 upsert 写入 queued new_message，返回会话键。"""
    import uuid

    from app.adapters.boss.list import load_and_parse_snapshot
    from app.adapters.sqlite.repo import SqliteStore
    from app.usecases.sense import persist_and_aggregate

    manifest, key = make_new_message_fixture(root, label, profile_id=profile_id)
    result = load_and_parse_snapshot(root / manifest, str(uuid.uuid4()))
    if result.status != "ok":
        raise AssertionError(result.reason_code)
    store = SqliteStore(
        profile_runtime_path(root, profile_id),
        profile_id=profile_id,
        require_runtime=True,
    )
    try:
        persist_and_aggregate(store, result)
    finally:
        store.close()
    return key


def make_job_fixture(root: Path, label: str) -> tuple[str, str]:
    """复制 job_list fixture 并替换标题/公司/薪资，返回单卡列表 manifest 与职位键。

    安全点要求真实回到列表，因此职位场景的主读取使用列表页；同一 manifest
    也可作为 `job_fixture` 复核目标身份，不再用完整职位详情冒充列表。
    """
    import shutil
    from app.domain.models import canonical_hash
    from app.domain.reading import job_identity_hash_for, job_key_from_identity

    title = f"合成岗位{label}"
    company = f"合成公司{label}"
    salary = "20-30K"
    target = root / "tests" / "fixtures" / "runtime" / f"job-{label}"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("profile.json", "job_list.xml", "job_list.json"):
        shutil.copyfile(ROOT / "tests/fixtures/read" / name, target / name)
    path = target / "job_list.xml"
    text = path.read_text(encoding="utf-8")
    second = (
        '      <node clickable="true" class="android.widget.LinearLayout" '
        'bounds="[0,500][1080,800]">\n'
        '        <node text="合成岗位乙" resource-id="com.hpbr.bosszhipin:id/tv_position_name" '
        'class="android.widget.TextView" bounds="[40,520][600,580]" />\n'
        '        <node text="15-20K" resource-id="com.hpbr.bosszhipin:id/tv_salary_statue" '
        'class="android.widget.TextView" bounds="[620,520][900,580]" />\n'
        '        <node text="合成公司乙" resource-id="com.hpbr.bosszhipin:id/tv_company_name" '
        'class="android.widget.TextView" bounds="[40,590][600,640]" />\n'
        "      </node>\n"
    )
    if second not in text:
        raise AssertionError("job list fixture shape changed")
    text = text.replace(second, "")
    text = (
        text.replace("合成岗位甲", title)
        .replace("合成公司甲", company)
        .replace("20-30K", salary)
    )
    path.write_text(text, encoding="utf-8")
    key = job_key_from_identity(job_identity_hash_for(title, company, salary))
    return f"tests/fixtures/runtime/job-{label}/job_list.json", key
