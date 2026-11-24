"""在隔离根内调用真实 M4c profile 事实、共享门禁、活动检查与 run 预检接口。

只使用合成 profile 库、隔离 os_root 与固定时钟；不访问真实 LOCALAPPDATA、设备、
网络、模型或私有数据。输出只含固定状态/计数/无值事实键，不返回正文或路径。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path.cwd().resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.files.policy import FilePolicyRepository
from app.adapters.sqlite.activity import ProfilePolicyActivity
from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import SqliteProfileStore
from app.ports.device_gate import DeviceGateError
from app.ports.store import Owner
from app.usecases import facts as facts_usecases
from app.usecases import policy as policy_usecases
from app.usecases import run as run_usecases

NOW = "2026-11-24T00:00:00.000Z"
OWNER_ID = "00000000-0000-4000-8000-000000000081"
OWNER_HOST = "a" * 64


class ProbeError(RuntimeError):
    """表示 M4c 探针请求不满足受控协议。"""


def _within(child: Path, parent: Path) -> bool:
    """判断路径是否仍在父目录内。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _relative(value: object, label: str, *, must_exist: bool = False) -> Path:
    """只接受隔离根内相对路径，拒绝绝对路径与链接。"""
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{label} 必须是非空相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProbeError(f"{label} 不得绝对或包含 ..")
    resolved = (ROOT / candidate).resolve()
    if not _within(resolved, ROOT):
        raise ProbeError(f"{label} 越出隔离根")
    if must_exist and not resolved.exists():
        raise ProbeError(f"{label} 不存在")
    return resolved


def _barrier() -> None:
    """写 ready 并等待父 runner 的 go，用于两个受控进程同时竞争。"""
    directory_value = os.environ.get("M1_BARRIER_DIR")
    name = os.environ.get("M1_BARRIER_NAME")
    if not directory_value or name is None or re.fullmatch(r"[01]", name) is None:
        raise ProbeError("barrier 环境缺失")
    raw_directory = Path(directory_value)
    if not raw_directory.is_absolute():
        raise ProbeError("barrier 目录必须为绝对路径")
    directory = raw_directory.resolve()
    if not _within(directory, ROOT) or not directory.is_dir():
        raise ProbeError("barrier 越界或不存在")
    (directory / f"{name}.ready").write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (directory / "go").is_file():
            return
        time.sleep(0.02)
    raise ProbeError("barrier timeout")


def _profile(profile_id: str, *, db_relative: str | None = None) -> tuple[SqliteProfileStore, Path]:
    """创建/打开隔离 profile 库并返回 store 与绝对路径。"""
    store = SqliteProfileStore(ROOT, profile_id, create=True, now=NOW)
    store.open()
    return store, store.path


def _facts(request: Mapping[str, Any]) -> dict[str, Any]:
    """真实 propose/apply/show/revoke/forget 事实流程，返回无值摘要。"""
    profile_id = str(request.get("profile_id", "synthetic-facts"))
    store, _path = _profile(profile_id)
    try:
        key = str(request.get("key", "work_location"))
        value = str(request.get("value", "合成城市甲"))
        proposed = facts_usecases.propose_set(
            store,
            profile_id=profile_id,
            key=key,
            value=value,
            expires_at=None,
            replace=False,
            now=NOW,
        )
        before = facts_usecases.show_facts(
            store, profile_id=profile_id, pack=None, now=NOW
        )
        applied = facts_usecases.apply_set(
            store,
            profile_id=profile_id,
            key=key,
            value=value,
            expires_at=None,
            replace=False,
            now=NOW,
        )
        after = facts_usecases.show_facts(
            store, profile_id=profile_id, pack=None, now=NOW
        )
        revoked = facts_usecases.apply_revoke(
            store, profile_id=profile_id, key=key, now=NOW
        )
        after_revoke = facts_usecases.show_facts(
            store, profile_id=profile_id, pack=None, now=NOW
        )
        forgotten = facts_usecases.apply_forget(
            store, profile_id=profile_id, key=key, now=NOW
        )
        return {
            "status": "ok",
            "propose_action": proposed.get("action"),
            "propose_confirmed": proposed.get("confirmed"),
            "apply_action": applied.get("action"),
            "apply_confirmed": applied.get("confirmed"),
            "revoke_action": revoked.get("action"),
            "forget_action": forgotten.get("action"),
            "before_count": len(before.get("facts", [])),
            "after_count": len(after.get("facts", [])),
            "after_revoke_count": len(after_revoke.get("facts", [])),
            "value_visible_in_local_show": value in json.dumps(after, ensure_ascii=False),
        }
    finally:
        store.close()


def _gate_race(request: Mapping[str, Any]) -> dict[str, Any]:
    """两个受控进程同时竞争同一隔离 os_root 的唯一 primary。"""
    profile_id = str(request.get("profile_id", "synthetic-profile"))
    store, db_path = _profile(profile_id)
    store.close()
    gate = SharedDeviceGate(
        profile_id=profile_id,
        profile_db_path=db_path,
        os_root=_relative(str(request.get("os_root", "data/runtime/os-gate")), "os_root"),
    )
    owner = Owner(OWNER_ID, 1, OWNER_HOST)
    _barrier()
    try:
        nonce = gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(db_path),
            owner=owner,
            now=NOW,
        )
    except DeviceGateError as error:
        return {
            "status": "ok",
            "outcome": "busy" if error.reason_code == "device_busy" else error.reason_code,
            "held": False,
        }
    identity = gate.session_identity()
    return {
        "status": "ok",
        "outcome": "held",
        "held": True,
        "profile_id": identity.profile_id,
        "owner_id": identity.owner.id,
        "owner_pid": identity.owner.pid,
        "owner_host": identity.owner.host,
        "nonce_present": bool(identity.nonce),
        "profile_path_match": identity.profile_db_path == str(db_path),
    }


def _gate_lifecycle(request: Mapping[str, Any]) -> dict[str, Any]:
    """真实 acquire/verify/release 收敛子证据。"""
    profile_id = str(request.get("profile_id", "synthetic-profile"))
    store, db_path = _profile(profile_id)
    store.close()
    gate = SharedDeviceGate(
        profile_id=profile_id,
        profile_db_path=db_path,
        os_root=_relative(str(request.get("os_root", "data/runtime/os-gate")), "os_root"),
    )
    owner = Owner(OWNER_ID, 1, OWNER_HOST)
    gate.acquire(
        profile_id=profile_id, profile_db_path=str(db_path), owner=owner, now=NOW
    )
    gate.verify(now=NOW)
    gate.release(now=NOW, converged=True)
    return {
        "status": "ok",
        "released": gate.inspect() is None,
    }


def _activity(request: Mapping[str, Any]) -> dict[str, Any]:
    """真实 ProfilePolicyActivity 评估 stopped/active。"""
    profile_id = str(request.get("profile_id", "synthetic-activity"))
    store, db_path = _profile(profile_id)
    store.close()
    os_root = _relative(str(request.get("os_root", "data/runtime/os-gate")), "os_root")
    activity = ProfilePolicyActivity(ROOT, os_root=os_root, clock=lambda: NOW)
    stopped = activity.assess_restore(profile_id=profile_id, dest_pack="prompts/local/demo")
    gate = SharedDeviceGate(
        profile_id=profile_id, profile_db_path=db_path, os_root=os_root
    )
    owner = Owner(OWNER_ID, 1, OWNER_HOST)
    gate.acquire(
        profile_id=profile_id, profile_db_path=str(db_path), owner=owner, now=NOW
    )
    active = activity.assess_restore(profile_id=profile_id, dest_pack="prompts/local/demo")
    return {
        "status": "ok",
        "stopped_assessment": stopped,
        "active_assessment": active,
    }


def _preflight(request: Mapping[str, Any]) -> dict[str, Any]:
    """真实 run check 预检 read_only 子证据。"""
    repo = FilePolicyRepository(ROOT)
    _normalized, base_files, _files, _guides, pack = policy_usecases.load_active_pack(
        repo, str(request.get("pack", "prompts/local/valid"))
    )
    profile_id = pack.profile_id
    store, db_path = _profile(profile_id)
    gate = SharedDeviceGate(
        profile_id=profile_id,
        profile_db_path=db_path,
        os_root=_relative(str(request.get("os_root", "data/runtime/os-gate")), "os_root"),
    )
    try:
        result = run_usecases.check_preflight(
            base_files=base_files,
            pack=pack,
            mode="read_only",
            confirm_device=True,
            gate=gate,
            profile_store=store,
            owner=Owner(OWNER_ID, 1, OWNER_HOST),
            profile_db_path=str(db_path),
            now=NOW,
        )
    finally:
        store.close()
    return {
        "status": "ok",
        "preflight_only": result.get("preflight_only"),
        "mode": result.get("mode"),
        "actions": result.get("actions"),
        "decisions": result.get("decisions"),
        "gate_released": gate.inspect() is None,
    }


def run_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """按 operation 分派真实 M4c 接口。"""
    operation = request.get("operation")
    if operation == "facts":
        return _facts(request)
    if operation == "gate_race":
        return _gate_race(request)
    if operation == "gate_lifecycle":
        return _gate_lifecycle(request)
    if operation == "activity":
        return _activity(request)
    if operation == "preflight":
        return _preflight(request)
    raise ProbeError("未知 operation")


def main(argv: list[str] | None = None) -> int:
    """读取受控请求并输出单个 JSON 摘要。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps({"schema_version": 1, "status": "error", "reason_code": "invalid_input"}))
        return 2
    try:
        request_path = _relative(args.request, "request", must_exist=True)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ProbeError("request 必须是对象")
        result = run_request(request)
    except (ProbeError, OSError, ValueError, UnicodeError) as error:
        result = {"status": "error", "reason_code": type(error).__name__}
    payload = {"schema_version": 1, "status": result.pop("status", "error"), **result}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
