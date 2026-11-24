"""在隔离根内调用真实 M3 定向核对/消息新增键/字段阶梯接口，并输出受限观察。

只用于 CLI 无法表达的显式 now、跨快照消息比较与阶梯冲突；调用真实公开函数，
不复制业务逻辑，不读 expected，不连接设备、业务库、模型或网络。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path.cwd().resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.boss.list import load_and_parse_snapshot
from app.adapters.boss.read import load_and_read_fixture
from app.domain.ladder import FieldReading, resolve_field
from app.domain.reading import Conversation, new_message_keys
from app.usecases import read as read_usecase


class ProbeError(RuntimeError):
    """表示 M3 探针请求或路径不满足受控协议。"""


def _within(child: Path, parent: Path) -> bool:
    """判断路径是否仍在父目录内。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_like(path: Path) -> bool:
    """识别 symlink 与 Windows junction。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _reject_links_on_path(path: Path, root: Path, label: str) -> None:
    """先检查原始 leaf/祖先，再检查根祖先；不得先 resolve。"""
    current = path
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 路径含链接或 junction")
        if current == root or current.parent == current:
            break
        current = current.parent
    current = root
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 根的祖先含链接或 junction")
        if current.parent == current:
            break
        current = current.parent


def _relative(value: object, label: str, *, must_exist: bool) -> Path:
    """只接受隔离根内相对路径，先查链接再 resolve/containment。"""
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{label} 必须是非空相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProbeError(f"{label} 不得绝对或包含 ..")
    lexical = ROOT / candidate
    _reject_links_on_path(lexical, ROOT, label)
    resolved = lexical.resolve()
    if not _within(resolved, ROOT):
        raise ProbeError(f"{label} 越出隔离根")
    if must_exist and not resolved.is_file():
        raise ProbeError(f"{label} 不存在")
    return resolved


def _targeted(request: dict) -> dict:
    """调用真实 M2 解析与 M3 定向核对，返回固定安全观察字段。"""
    initial_path = _relative(request.get("initial_fixture"), "initial_fixture", must_exist=True)
    targeted_path = _relative(request.get("targeted_fixture"), "targeted_fixture", must_exist=True)
    now = request.get("now")
    if not isinstance(now, str):
        raise ProbeError("targeted 需要规范 now")
    initial_id = request.get("initial_snapshot_id") or str(uuid.uuid4())
    targeted_id = request.get("targeted_snapshot_id") or str(uuid.uuid4())
    if not isinstance(initial_id, str) or not isinstance(targeted_id, str):
        raise ProbeError("snapshot_id 必须是字符串")
    queued_keys = request.get("queued_keys", [])
    if not isinstance(queued_keys, list):
        raise ProbeError("queued_keys 必须是数组")
    initial = load_and_parse_snapshot(initial_path, initial_id)
    targeted = load_and_parse_snapshot(targeted_path, targeted_id)
    outcome = read_usecase.targeted_verification_outcome(initial, targeted, queued_keys, now=now)
    target_value = targeted.value
    return {
        "outcome": outcome,
        "initial_status": initial.status,
        "initial_reason_code": initial.reason_code,
        "initial_snapshot_id": initial.snapshot_id,
        "targeted_status": targeted.status,
        "targeted_reason_code": targeted.reason_code,
        "targeted_snapshot_id": targeted.snapshot_id,
        "targeted_unread_state": None if target_value is None else target_value.unread_state,
        "targeted_candidate_count": None if target_value is None else len(target_value.candidates),
    }


def _reading(value: object, slot: str) -> FieldReading | None:
    """把请求中的固定键对象构造为真实 FieldReading；null 表示未提供该槽位。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProbeError(f"{slot} reading 必须是对象或 null")
    keys = {
        "field",
        "app",
        "profile_id",
        "profile_version",
        "snapshot_id",
        "observed_at",
        "layout_context",
        "source",
        "confidence",
        "state",
        "count",
        "reason_code",
    }
    if set(value) != keys:
        raise ProbeError(f"{slot} reading 键形状不符")
    return FieldReading(**value)


def _ladder(request: dict) -> dict:
    """调用真实 resolve_field，并只输出校验后的合并结论。"""
    slots = {}
    for name in ("hierarchy", "pixel", "vision", "human"):
        slots[name] = _reading(request.get(name), name)
    vision_enabled = request.get("vision_enabled", False)
    cross = request.get("independent_cross_evidence", False)
    if type(vision_enabled) is not bool or type(cross) is not bool:
        raise ProbeError("视觉/交叉证据开关必须是 bool")
    resolution = resolve_field(
        hierarchy=slots["hierarchy"],
        pixel=slots["pixel"],
        vision=slots["vision"],
        human=slots["human"],
        vision_enabled=vision_enabled,
        independent_cross_evidence=cross,
    )
    return {
        "state": resolution.state,
        "count": resolution.count,
        "reason_code": resolution.reason_code,
        "source": resolution.source,
        "confidence": resolution.confidence,
        "requires_human": resolution.requires_human,
        "evidence_sources": [item.source for item in resolution.evidence],
        "evidence_states": [item.state for item in resolution.evidence],
    }


def _new_keys(request: dict) -> dict:
    """调用真实读取入口与 new_message_keys，跨会话错误映射为固定类别。"""
    previous_path = _relative(request.get("previous_fixture"), "previous_fixture", must_exist=True)
    current_path = _relative(request.get("current_fixture"), "current_fixture", must_exist=True)
    previous_id = request.get("previous_snapshot_id") or str(uuid.uuid4())
    current_id = request.get("current_snapshot_id") or str(uuid.uuid4())
    if not isinstance(previous_id, str) or not isinstance(current_id, str):
        raise ProbeError("snapshot_id 必须是字符串")
    previous = load_and_read_fixture(previous_path, previous_id)
    current = load_and_read_fixture(current_path, current_id)
    if previous.status != "ok" or not isinstance(previous.value, Conversation):
        raise ProbeError("previous 不是成功会话")
    if current.status != "ok" or not isinstance(current.value, Conversation):
        raise ProbeError("current 不是成功会话")
    try:
        keys = new_message_keys(previous.value, current.value)
        error = None
    except ValueError:
        keys = ()
        error = "identity_mismatch"
    return {
        "new_keys": list(keys),
        "error": error,
        "current_inbound_state": current.value.inbound_state,
        "current_latest_inbound_key": current.value.latest_inbound_key,
        "current_message_identity_verified": current.value.message_identity_verified,
        "current_message_keys": [
            message.message_key for message in current.value.messages
        ],
    }


def main(argv: list[str] | None = None) -> int:
    """读取 JSON 请求并输出单对象结果；不输出 traceback 或路径。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        request_path = _relative(args.request, "request", must_exist=True)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ProbeError("request 必须是对象")
        mode = request.get("mode")
        if mode == "targeted":
            payload = _targeted(request)
        elif mode == "ladder":
            payload = _ladder(request)
        elif mode == "new_keys":
            payload = _new_keys(request)
        else:
            raise ProbeError("未知 mode")
        output = {"schema_version": 1, "status": "ok", **payload}
        code = 0
    except Exception as exc:  # noqa: BLE001 - 探针边界不输出 traceback/路径
        output = {"schema_version": 1, "status": "failed", "outcome": type(exc).__name__}
        code = 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
