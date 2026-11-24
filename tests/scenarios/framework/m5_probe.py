"""在隔离根内用真实 M5 入口执行不可逆动作、故障窗口与人工对账。

只替换外部设备边界：`subprocess.Popen` 返回合成原始 XML/IME/returncode，
`ProcessFamilyBound` 用受控空族替身，`LOCALAPPDATA`/profile/账本/证据都在隔离
根内。假设备只记录远端命令与可能不可逆命令计数，不返回 performed/隔离/重试
等产品结论；产品独立推导，预期只由独立 expected 比较。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

ROOT = Path.cwd().resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.adb import actions as actions_module
from app.adapters.adb import command as command_module
from app.adapters.adb.actions import AdbActionExecutor
from app.adapters.adb.command import CommandIdentityStore, ProcessFamilyBound
from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import SqliteProfileStore
from app.adapters.sqlite.repo import SqliteStore
from app.domain.actions import (
    ActionAuthorization,
    ActionIdentity,
    ActionObservation,
    ReconciliationObservation,
    action_key_for,
    derive_action_outcome,
    validate_evidence,
)
from app.domain.models import (
    Candidate,
    Evidence,
    Owner,
    ReadResult,
    UnreadRow,
    UnreadSignal,
    UnreadSnapshot,
    conversation_key_for,
    discovery_key_for,
    fingerprint_for,
    identity_hash_for,
)
from app.domain.reading import message_key_from_stable, message_stable_identity_hash
from app.ports.store import StoreError
from app.usecases.actions import perform_action, reconcile_action

NOW = "2026-11-24T00:00:00.000Z"
EXPIRES = "2026-11-24T01:00:00.000Z"
BASE_HASH = "a" * 64
PACK_HASH = "b" * 64
OWNER_ID = "00000000-0000-4000-8000-0000000000a1"
OWNER_HOST = "c" * 64
CONTACT = "合成联系人甲"
COMPANY = "合成机构甲"
POSITION = "合成岗位甲"
ANCHOR = "合成邻居乙"
BODY = "合成回复正文"
STABLE = message_stable_identity_hash("synthetic-stable-message-1")
SIGNAL_KEYS = ("row_badge", "new_greeting", "tab_badge", "preview_delta")
ACTION_LAYOUT = {
    "A01": ("send_text", "text"),
    "A02": ("send_resume", "resume"),
    "A03": ("change_wechat", "wechat"),
    "A04": ("change_phone", "phone"),
    "A05": ("delete_conversation", "delete"),
}


class ProbeError(RuntimeError):
    """表示探针请求或隔离边界不满足受控协议。"""


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


def _relative(value: object, label: str, *, must_exist: bool = False) -> Path:
    """只接受隔离根内相对路径，先拒绝原始链接再 resolve。"""
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{label} 必须是非空相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProbeError(f"{label} 不得绝对或包含 ..")
    lexical = ROOT / candidate
    current = lexical
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 路径含链接或 junction")
        if current == ROOT or current.parent == current:
            break
        current = current.parent
    resolved = lexical.resolve()
    if not _within(resolved, ROOT):
        raise ProbeError(f"{label} 越出隔离根")
    if must_exist and not resolved.exists():
        raise ProbeError(f"{label} 不存在")
    return resolved


def _load_stimuli(path: Path) -> dict[str, Any]:
    """读取不含任何产品结论的纯合成原始刺激。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ProbeError("刺激文件不可读") from None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ProbeError("刺激 schema 非法")
    actions = value.get("actions")
    if not isinstance(actions, dict):
        raise ProbeError("刺激 actions 非法")
    return value


def _owner() -> Owner:
    """返回本探针进程的合成 owner。"""
    return Owner(OWNER_ID, 4242, OWNER_HOST)


def _signals() -> dict[str, UnreadSignal]:
    """构造全部未知信号，让 seed 快照不依赖真实未读。"""
    return {name: UnreadSignal("unknown", None, "missing_signal") for name in SIGNAL_KEYS}


def _seed_queued(store: SqliteStore, profile_id: str) -> None:
    """向真实 profile 库写入与动作身份一致的合成 queued 发现。"""
    conversation = conversation_key_for("boss", CONTACT, COMPANY, POSITION)
    fingerprint = fingerprint_for(None, "10:00", 1)
    row = UnreadRow(
        conversation_key=conversation,
        identity_hash=identity_hash_for(CONTACT, COMPANY, POSITION),
        preview_hash=None,
        time_hint="10:00",
        unread_count=1,
    )
    candidate = Candidate(
        candidate_key=discovery_key_for("boss", conversation, fingerprint),
        conversation_key=conversation,
        kind="unread",
        source_names=("row_badge",),
        fingerprint=fingerprint,
    )
    snapshot = UnreadSnapshot(
        app="boss",
        profile_id=profile_id,
        coverage="viewport",
        signals=_signals(),
        rows=(row,),
        candidates=(candidate,),
        unread_state="present",
    )
    store.upsert_snapshot(
        ReadResult(
            "ok",
            snapshot,
            None,
            str(uuid.uuid4()),
            NOW,
            "fixture",
            "high",
        )
    )


def _profile_context(
    profile_id: str,
    os_root: Path,
    fault_hook: Callable[[str, object], None] | None = None,
) -> tuple[SqliteStore, Any, SharedDeviceGate, Path]:
    """创建真实 profile v2 库、seed、claim/start 并取得共享占用。"""
    profile = SqliteProfileStore(ROOT, profile_id, create=True, now=NOW)
    profile.open()
    db_path = profile.path
    profile.close()
    store = SqliteStore(
        db_path,
        profile_id=profile_id,
        local_host=OWNER_HOST,
        monotonic=lambda: 1000.0,
        stop_checker=lambda pid: "stopped",
        fault_hook=fault_hook,
    )
    _seed_queued(store, profile_id)
    owner = _owner()
    claimed = store.claim("boss", "unread", owner, NOW)
    if claimed is None:
        store.close()
        raise ProbeError("合成任务领取失败")
    lease = store.start(claimed, NOW)
    gate = SharedDeviceGate(
        profile_id=profile_id, profile_db_path=db_path, os_root=os_root
    )
    gate.acquire(
        profile_id=profile_id, profile_db_path=str(db_path), owner=owner, now=NOW
    )
    return store, lease, gate, db_path


def _identity() -> ActionIdentity:
    """返回与持久任务一致的合成结构化身份。"""
    return ActionIdentity(CONTACT, COMPANY, POSITION)


def _authorization(
    lease: Any,
    *,
    profile_id: str,
    action_kind: str,
    action_key: str,
    confirmed: bool = True,
) -> ActionAuthorization:
    """构造精确绑定当前动作、任务、四摘要与期限的分动作授权。"""
    return ActionAuthorization(
        profile_id=profile_id,
        base_hash=BASE_HASH,
        pack_hash=PACK_HASH,
        acceptance_base_hash=BASE_HASH,
        acceptance_pack_hash=PACK_HASH,
        task_key=lease.key,
        action_key=action_key,
        authorized_kinds=(action_kind,),
        expires_at=EXPIRES,
        confirmed=confirmed,
        nonce=str(uuid.uuid4()),
    )


@contextmanager
def _synthetic_device(device: _Device) -> Iterator[None]:
    """把 ADB 子进程与 Job 创建替换为合成边界；进程账本仍为真实实现。"""
    original_popen = subprocess.Popen
    original_create = ProcessFamilyBound.__dict__["create"]
    original_real = command_module._is_real_process
    original_now = actions_module._utc_now
    subprocess.Popen = device.popen  # type: ignore[assignment]
    ProcessFamilyBound.create = classmethod(lambda cls: _Bound())  # type: ignore[assignment]
    command_module._is_real_process = lambda process: True  # type: ignore[assignment]
    actions_module._utc_now = lambda: NOW  # type: ignore[assignment]
    try:
        yield
    finally:
        subprocess.Popen = original_popen  # type: ignore[assignment]
        ProcessFamilyBound.create = original_create  # type: ignore[assignment]
        command_module._is_real_process = original_real  # type: ignore[assignment]
        actions_module._utc_now = original_now  # type: ignore[assignment]


class _Bound:
    """合成 Job：立即加入/恢复并证明空族；不触真实平台 Job。"""

    def assign(self, pid: int) -> bool:
        """记录加入成功。"""
        return True

    def resume(self, pid: int) -> bool:
        """记录恢复成功。"""
        return True

    def terminate(self) -> bool:
        """记录终止成功。"""
        return True

    def active(self) -> int:
        """返回空族。"""
        return 0

    def close(self) -> None:
        """无资源需要关闭。"""


class _Device:
    """按 argv 提供原始 XML/IME 并独立计数远端命令。"""

    def __init__(
        self, stimuli: Mapping[str, Any], *, fail_irreversible: bool = False
    ) -> None:
        """保存刺激、是否在不可逆点击处断连与初始计数。"""
        self._stimuli = stimuli
        self._fail_irreversible = fail_irreversible
        self._irreversible_index = int(stimuli["irreversible_index"])
        self.ime = str(stimuli["ime"])
        self.xml_queue = list(stimuli["xml_sequence"])
        self.calls: list[list[str]] = []
        self.taps = 0
        self.irreversible = 0
        self.failed_after_irreversible = False
        self._next_pid = 7000

    def popen(self, args: list[str], **kwargs: object) -> "_Proc":
        """返回一个按参数提供输出的合成进程。"""
        self.calls.append(list(args))
        self._next_pid += 1
        return _Proc(self, list(args), self._next_pid)

    def output(self, args: list[str]) -> tuple[str, int]:
        """按命令类型返回 stdout 与 returncode，并登记远端不可逆执行。"""
        if "settings" in args and "get" in args:
            return self.ime + "\n", 0
        if "ime" in args and "set" in args:
            self.ime = args[-1]
            return "", 0
        if "uiautomator" in args:
            return "", 0
        if "cat" in args:
            if not self.xml_queue:
                return "", 1
            return self.xml_queue.pop(0), 0
        if "input" in args and "tap" in args:
            index = self.taps
            self.taps += 1
            if index >= self._irreversible_index:
                self.irreversible += 1
                if self._fail_irreversible:
                    self.failed_after_irreversible = True
                    raise OSError("synthetic device lost")
            return "", 0
        return "", 0


class _Proc:
    """最小 Popen 替身：只提供 run_adb 使用的属性与有界回收。"""

    def __init__(self, device: _Device, args: list[str], pid: int) -> None:
        """保存设备、参数与固定 PID。"""
        self._device = device
        self.args = args
        self.pid = pid
        self.returncode = 0
        self._finished = False
        self._output: tuple[str, str] = ("", "")

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        """只执行一次合成命令；回收阶段重入返回既有结果，不重复远端动作。"""
        if self._finished:
            return self._output
        self._finished = True
        try:
            stdout, code = self._device.output(self.args)
        except OSError:
            self.returncode = 1
            raise
        self.returncode = code
        self._output = (stdout, "")
        return self._output

    def kill(self) -> None:
        """合成进程无需 kill。"""

    def wait(self, timeout: float | None = None) -> int:
        """立即回收。"""
        return self.returncode


class _ObservingExecutor(AdbActionExecutor):
    """在真实执行器边界记录 pending 时点，不改变业务判定。"""

    def __init__(self, *args: Any, store: SqliteStore, **kwargs: Any) -> None:
        """保存 store 供 pending 观察。"""
        super().__init__(*args, **kwargs)
        self._probe_store = store
        self.probe_pending_before_exec: str | None = None
        self.probe_observation: Any = None

    def execute(self, plan: Any, *, before_command: Callable[[], None] | None = None) -> Any:
        """执行前只读记录真实仓储状态，随后调用生产执行器。"""
        record = self._probe_store.get_action(plan.action_key)
        self.probe_pending_before_exec = None if record is None else str(record["state"])
        observation = super().execute(plan, before_command=before_command)
        self.probe_observation = observation
        return observation


class _CrashingExecutor(_ObservingExecutor):
    """在首次设备命令前用 BaseException 模拟进程死亡。"""

    def bind_action(self, action_key: str) -> None:
        """真实 pending 已提交后中断整个生命周期。"""
        raise SystemExit("synthetic process death after pending")


def _executor(
    *,
    gate: SharedDeviceGate,
    store: SqliteStore,
    root: Path,
    device: _Device,
    crash: bool = False,
) -> AdbActionExecutor:
    """构造真实执行器，账本/证据在隔离根，设备边界由合成进程提供。"""
    evidence = root / "data" / "evals" / "m5-probe" / "evidence"
    ledger = root / "data" / "evals" / "m5-probe" / "ledger"
    evidence.mkdir(parents=True, exist_ok=True)
    ledger.mkdir(parents=True, exist_ok=True)
    executor_class = _CrashingExecutor if crash else _ObservingExecutor
    return executor_class(
        serial="<synthetic-serial>",
        gate=gate,
        process_log=None,
        timeout=5,
        evidence_dir=evidence,
        command_store=CommandIdentityStore(ledger),
        child_scan=lambda pid: [],
        store=store,
        adb="adb",
    )


def _event_rows(db_path: Path, action_key: str) -> list[dict[str, Any]]:
    """只读返回动作事件状态序列，用于独立观察。"""
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT event, from_state, to_state, reason_code FROM action_events "
            "WHERE action_key = ? ORDER BY event_id",
            (action_key,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "event": str(row[0]),
            "from": row[1],
            "to": row[2],
            "reason": row[3],
        }
        for row in rows
    ]


def _action_record(db_path: Path, action_key: str) -> dict[str, Any] | None:
    """只读返回动作记录的无正文投影。"""
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute(
            "SELECT state, outcome, effect, reason_code, command_quiet, "
            "ime_restored, scene_settled FROM action_records WHERE action_key = ?",
            (action_key,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "state": row[0],
        "outcome": row[1],
        "effect": row[2],
        "reason": row[3],
        "command_quiet": bool(row[4]),
        "ime_restored": bool(row[5]),
        "scene_settled": bool(row[6]),
    }


def _completion_fault(name: str, _connection: object) -> None:
    """在真实提交事务的 action_before_event 点注入存储失败。"""
    if name == "action_before_event":
        raise StoreError("store_io_error")


def _stimulus_for_fault(
    base: Mapping[str, Any], fault: str, action_id: str
) -> dict[str, Any]:
    """把纯外部刺激按故障窗口变形；不写入任何产品结论。"""
    value = {
        "ime": base["ime"],
        "target_ime": base.get("target_ime"),
        "xml_sequence": list(base["xml_sequence"]),
        "taps": [list(item) for item in base["taps"]],
        "irreversible_index": int(base["irreversible_index"]),
    }
    if fault == "FD":
        if action_id == "A05":
            value["xml_sequence"][-1] = base["after_missing_anchor"]
        else:
            value["xml_sequence"][-1] = base["xml_sequence"][0]
    if action_id == "A02" and fault == "request_only":
        value["xml_sequence"][-1] = base["after_request_only"]
    if action_id == "A02" and fault == "delivered":
        value["xml_sequence"][-1] = base["after_delivered"]
    if action_id in {"A03", "A04"} and fault == "direct_send":
        value["taps"] = [list(value["taps"][-1])]
        value["irreversible_index"] = 0
        value["xml_sequence"] = [
            base["xml_sequence"][0],
            base["xml_sequence"][-1],
        ]
    return value


def _plan_for(
    action_id: str,
    action_kind: str,
    action_key: str,
    conversation: str,
    stimulus: Mapping[str, Any],
) -> dict[str, Any]:
    """构造 perform_action 的公开关键字；只有调用方计划，不是产品判定。"""
    message_key = message_key_from_stable(conversation, STABLE)
    body: str | None = BODY if action_kind == "send_text" else None
    attachment: str | None = "合成附件简历.pdf" if action_kind == "send_resume" else None
    target_key = message_key if action_kind == "send_text" else conversation
    anchor = ANCHOR if action_kind == "delete_conversation" else None
    ime_component = str(stimulus["target_ime"]) if action_kind == "send_text" else None
    return {
        "action_kind": action_kind,
        "action_key": action_key,
        "target_key": target_key,
        "conversation_key": conversation,
        "body_text": body,
        "attachment_name": attachment,
        "anchor_label": anchor,
        "ime_component": ime_component,
        "tap_points": tuple(tuple(item) for item in stimulus["taps"]),
    }


def _run_action_case(case: str, stimuli: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """用真实 perform_action 跑一个动作故障窗口，只输出可观察原始事实。"""
    action_id, _, fault = case.partition("_")
    kind, slug = ACTION_LAYOUT[action_id]
    base = stimuli["actions"][slug]
    stimulus = _stimulus_for_fault(base, fault, action_id)
    conversation = conversation_key_for("boss", CONTACT, COMPANY, POSITION)
    message_key = message_key_from_stable(conversation, STABLE)
    action_key = action_key_for(
        kind,
        app="boss",
        conversation_key=conversation,
        message_key=message_key if kind == "send_text" else None,
    )
    profile_id = "m5-action"
    os_root = root / "localappdata"
    store, lease, gate, db_path = _profile_context(
        profile_id, os_root, _completion_fault if fault == "FE" else None
    )
    plan = _plan_for(action_id, kind, action_key, conversation, stimulus)
    device = _Device(stimulus)
    result: dict[str, Any] = {
        "case": case,
        "action_kind": kind,
        "action_key_shape": action_key.split(":")[0] + ":" + action_key.split(":")[-1],
        "layer": "raw_device_usecase",
    }
    try:
        if fault == "FB":
            crashing = _executor(
                gate=gate, store=store, root=root, device=device, crash=True
            )
            try:
                perform_action(
                    store=store,
                    gate=gate,
                    authorization=_authorization(
                        lease,
                        profile_id=profile_id,
                        action_kind=kind,
                        action_key=action_key,
                    ),
                    lease=lease,
                    profile_db_path=str(db_path),
                    executor=crashing,
                    now=NOW,
                    clock=lambda: NOW,
                    current_base_hash=BASE_HASH,
                    current_pack_hash=PACK_HASH,
                    daily_sends=5,
                    reply_cooldown_seconds=0,
                    identity=_identity(),
                    reload_snapshot=lambda: {
                        "base_hash": BASE_HASH,
                        "pack_hash": PACK_HASH,
                        "acceptance_base_hash": BASE_HASH,
                        "acceptance_pack_hash": PACK_HASH,
                    },
                    **plan,
                )
            except SystemExit:
                result["interrupted"] = True
            record = _action_record(db_path, action_key)
            result["after_interrupt"] = record
            result["remote_after_interrupt"] = {
                "irreversible": device.irreversible,
                "all_commands": len(device.calls),
            }
            replay_device = _Device(stimulus)
            replay_executor = _executor(
                gate=gate, store=store, root=root, device=replay_device
            )
            replay = _perform(
                store=store,
                gate=gate,
                lease=lease,
                authorization=_authorization(
                    lease,
                    profile_id=profile_id,
                    action_kind=kind,
                    action_key=action_key,
                ),
                profile_db_path=str(db_path),
                executor=replay_executor,
                device=replay_device,
                plan=plan,
            )
            result["replay"] = {
                **replay,
                "irreversible": replay_device.irreversible,
                "all_commands": len(replay_device.calls),
            }
            return result

        if fault == "FA":
            authorization = _authorization(
                lease,
                profile_id=profile_id,
                action_kind=kind,
                action_key=action_key,
                confirmed=False,
            )
        else:
            authorization = _authorization(
                lease,
                profile_id=profile_id,
                action_kind=kind,
                action_key=action_key,
            )
        if fault == "FC":
            device = _Device(stimulus, fail_irreversible=True)
        executor = _executor(gate=gate, store=store, root=root, device=device)
        outcome = _perform(
            store=store,
            gate=gate,
            lease=lease,
            authorization=authorization,
            profile_db_path=str(db_path),
            executor=executor,
                device=device,
            plan=plan,
        )
        result["observed"] = outcome
        result["remote"] = {
            "irreversible": device.irreversible,
            "all_commands": len(device.calls),
            "failed_after_irreversible": device.failed_after_irreversible,
            "argv": [[item for item in call if item != "adb"] for call in device.calls],
        }
        result["pending_before_execute"] = getattr(
            executor, "probe_pending_before_exec", None
        )
        observation = getattr(executor, "probe_observation", None)
        if observation is not None:
            result["executor_observation"] = {
                name: getattr(observation, name)
                for name in (
                    "clicked",
                    "error",
                    "identity_unique",
                    "contact_slot_available",
                    "contact_slot_status",
                    "list_complete",
                    "protections_clear",
                    "target_present_before",
                    "target_present_after",
                    "anchor_present_after",
                    "body_readback_matches",
                    "attachment_name_matched",
                    "attachment_request_observed",
                    "delivery_observed",
                    "scene_settled",
                    "confirmation_open",
                    "command_quiet",
                )
            }
        record = _action_record(db_path, action_key)
        result["record"] = record
        result["events"] = _event_rows(db_path, action_key)
        converged = bool(outcome.get("gate_converged"))
        if fault != "FE":
            try:
                gate.release(now=NOW, converged=converged)
            except Exception:
                pass
        result["gate_after_release"] = gate.inspect()
        if fault in {"FC", "FE"}:
            replay_device = _Device(stimulus)
            replay_executor = _executor(
                gate=gate, store=store, root=root, device=replay_device
            )
            replay = _perform(
                store=store,
                gate=gate,
                lease=lease,
                authorization=_authorization(
                    lease,
                    profile_id=profile_id,
                    action_kind=kind,
                    action_key=action_key,
                ),
                profile_db_path=str(db_path),
                executor=replay_executor,
                device=replay_device,
                plan=plan,
            )
            result["replay"] = {
                **replay,
                "irreversible": replay_device.irreversible,
                "all_commands": len(replay_device.calls),
            }
        return result
    finally:
        try:
            store.close()
        except Exception:
            pass


def _perform(
    *,
    store: SqliteStore,
    gate: SharedDeviceGate,
    lease: Any,
    authorization: ActionAuthorization,
    profile_db_path: str,
    executor: AdbActionExecutor,
    device: _Device,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """调用真实 perform_action；异常只压缩为固定原因码。"""
    try:
        with _synthetic_device(device):
            value = perform_action(
                store=store,
                gate=gate,
                authorization=authorization,
                lease=lease,
                profile_db_path=profile_db_path,
                executor=executor,
                now=NOW,
                clock=lambda: NOW,
                current_base_hash=BASE_HASH,
                current_pack_hash=PACK_HASH,
                daily_sends=5,
                reply_cooldown_seconds=0,
                identity=_identity(),
                reload_snapshot=lambda: {
                    "base_hash": BASE_HASH,
                    "pack_hash": PACK_HASH,
                    "acceptance_base_hash": BASE_HASH,
                    "acceptance_pack_hash": PACK_HASH,
                },
                **plan,
            )
    except Exception as error:
        reason = getattr(error, "reason_code", type(error).__name__)
        return {"raised": str(reason), "state": None, "evidence": None}
    return {
        "raised": None,
        "state": value.get("state"),
        "evidence": value.get("evidence"),
        "gate_converged": value.get("gate_converged"),
        "card": value.get("card"),
        "delivered": value.get("delivered"),
    }


def _run_reconcile_case(case: str, stimuli: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """先用真实动作留下 unknown/pending，再走真实 reconcile_action。"""
    kind, slug = ACTION_LAYOUT["A01"]
    base = stimuli["actions"][slug]
    stimulus = _stimulus_for_fault(base, "FE", "A01")
    conversation = conversation_key_for("boss", CONTACT, COMPANY, POSITION)
    message_key = message_key_from_stable(conversation, STABLE)
    action_key = action_key_for(
        kind, app="boss", conversation_key=conversation, message_key=message_key
    )
    profile_id = "m5-action"
    os_root = root / "localappdata"
    store, lease, gate, db_path = _profile_context(
        profile_id,
        os_root,
        _completion_fault if case == "confirm_performed" else None,
    )
    plan = _plan_for("A01", kind, action_key, conversation, stimulus)
    device = _Device(stimulus)
    if case == "confirm_not_performed":
        device = _Device(stimulus, fail_irreversible=True)
    elif case == "abandon":
        device = _Device(stimulus, fail_irreversible=True)
    elif case == "reconnect":
        device = _Device(stimulus, fail_irreversible=True)
    try:
        executor = _executor(gate=gate, store=store, root=root, device=device)
        first = _perform(
            store=store,
            gate=gate,
            lease=lease,
            authorization=_authorization(
                lease,
                profile_id=profile_id,
                action_kind=kind,
                action_key=action_key,
            ),
            profile_db_path=str(db_path),
            executor=executor,
                device=device,
            plan=plan,
        )
        record = _action_record(db_path, action_key)
        result: dict[str, Any] = {
            "case": case,
            "layer": "raw_device_usecase",
            "first": first,
            "record_after_first": record,
            "remote_after_first": device.irreversible,
        }
        if case == "reconnect":
            replay_device = _Device(stimulus)
            replay = _perform(
                store=store,
                gate=gate,
                lease=lease,
                authorization=_authorization(
                    lease,
                    profile_id=profile_id,
                    action_kind=kind,
                    action_key=action_key,
                ),
                profile_db_path=str(db_path),
                executor=_executor(
                    gate=gate, store=store, root=root, device=replay_device
                ),
                device=replay_device,
                plan=plan,
            )
            result["replay"] = {
                **replay,
                "irreversible": replay_device.irreversible,
            }
            result["record_after_replay"] = _action_record(db_path, action_key)
            result["gate_still_occupied"] = gate.inspect() is not None
            return result
        if case == "abandon":
            observation = ReconciliationObservation(
                action_key=action_key,
                resolution="abandon",
                observed_at=NOW,
                evidence=None,
                command_quiet=False,
                scene_settled=False,
                confirmation_open=False,
                ime_original=str(stimulus["ime"]),
                ime_current=str(stimulus["ime"]),
                matched_action_key=action_key,
                confirmed=True,
            )
        elif case == "confirm_performed":
            observation = ReconciliationObservation(
                action_key=action_key,
                resolution="confirm_performed",
                observed_at=NOW,
                evidence=Evidence(
                    action_key=action_key,
                    outcome="performed",
                    effect="sent",
                    reason_code=None,
                    before_snapshot_id=str(uuid.uuid4()),
                    after_snapshot_id=str(uuid.uuid4()),
                    observed_at=NOW,
                ),
                command_quiet=True,
                scene_settled=True,
                confirmation_open=False,
                ime_original=str(stimulus["ime"]),
                ime_current=str(stimulus["ime"]),
                matched_action_key=action_key,
                confirmed=True,
            )
        else:
            observation = ReconciliationObservation(
                action_key=action_key,
                resolution="confirm_not_performed",
                observed_at=NOW,
                evidence=Evidence(
                    action_key=action_key,
                    outcome="not_performed",
                    effect="none",
                    reason_code="cancelled_before_action",
                    before_snapshot_id=str(uuid.uuid4()),
                    after_snapshot_id=None,
                    observed_at=NOW,
                ),
                command_quiet=True,
                scene_settled=True,
                confirmation_open=False,
                ime_original=str(stimulus["ime"]),
                ime_current=str(stimulus["ime"]),
                matched_action_key=action_key,
                confirmed=True,
            )
        try:
            reconciled = reconcile_action(
                store=store,
                gate=gate,
                observation=observation,
                profile_id=profile_id,
                profile_db_path=str(db_path),
                explicit_confirm=True,
                command_stop_checker=lambda pid: "stopped",
                now=NOW,
                process_log=None,
            )
            result["reconciled"] = {
                "resolution": reconciled.get("resolution"),
                "state": reconciled.get("state"),
                "task_state": reconciled.get("task_state"),
                "gate_converged": reconciled.get("gate_converged"),
                "lock_retained": reconciled.get("lock_retained"),
            }
        except Exception as error:
            result["reconciled"] = {
                "raised": str(getattr(error, "reason_code", type(error).__name__))
            }
        result["record_after_reconcile"] = _action_record(db_path, action_key)
        result["gate_after_reconcile"] = gate.inspect()
        return result
    finally:
        try:
            store.close()
        except Exception:
            pass


def _observation(**overrides: Any) -> ActionObservation:
    """构造合同层 ActionObservation 输入，字段完全由刺激决定。"""
    values: dict[str, Any] = {
        "action_key": "boss:boss:msg:" + "1" * 64 + ":reply",
        "action_kind": "send_text",
        "before_snapshot_id": str(uuid.uuid4()),
        "after_snapshot_id": str(uuid.uuid4()),
        "observed_at": NOW,
        "clicked": True,
        "error": None,
        "scene_settled": True,
        "command_quiet": True,
        "confirmation_open": False,
        "input_box_empty": True,
        "ime_original": None,
        "ime_current": None,
        "body_readback_matches": True,
        "echo_count_before": 0,
        "echo_count_after": 1,
        "attachment_name_matched": False,
        "attachment_request_observed": False,
        "delivery_observed": False,
        "contact_slot_available": False,
        "contact_slot_status": None,
        "identity_unique": True,
        "list_complete": True,
        "protections_clear": True,
        "target_present_before": True,
        "target_present_after": False,
        "anchor_present_after": False,
    }
    values.update(overrides)
    return ActionObservation(**values)


def _real_lock_probe(
    case: str, stimuli: Mapping[str, Any], root: Path
) -> dict[str, Any]:
    """真实仓储留下 pending，再验证动作键锁阻止第二次执行且零远端命令。"""
    kind, slug = ACTION_LAYOUT["A01"]
    base = stimuli["actions"][slug]
    stimulus = _stimulus_for_fault(base, "FB", "A01")
    conversation = conversation_key_for("boss", CONTACT, COMPANY, POSITION)
    message_key = message_stable_identity_hash("synthetic-stable-message-1")
    action_key = action_key_for(
        kind,
        app="boss",
        conversation_key=conversation,
        message_key=message_key_from_stable(conversation, message_key),
    )
    profile_id = "m5-evidence"
    os_root = root / "localappdata"
    store, lease, gate, db_path = _profile_context(
        profile_id,
        os_root,
        _completion_fault if case == "confirm_performed" else None,
    )
    plan = _plan_for("A01", kind, action_key, conversation, stimulus)
    device = _Device(stimulus)
    try:
        crashing = _executor(
            gate=gate, store=store, root=root, device=device, crash=True
        )
        try:
            _perform(
                store=store,
                gate=gate,
                lease=lease,
                authorization=_authorization(
                    lease,
                    profile_id=profile_id,
                    action_kind=kind,
                    action_key=action_key,
                ),
                profile_db_path=str(db_path),
                executor=crashing,
                device=device,
                plan=plan,
            )
        except SystemExit:
            pass
        pending = _action_record(db_path, action_key)
        replay_device = _Device(stimulus)
        replay = _perform(
            store=store,
            gate=gate,
            lease=lease,
            authorization=_authorization(
                lease,
                profile_id=profile_id,
                action_kind=kind,
                action_key=action_key,
            ),
            profile_db_path=str(db_path),
            executor=_executor(
                gate=gate, store=store, root=root, device=replay_device
            ),
            device=replay_device,
            plan=plan,
        )
        return {
            "pending_state": None if pending is None else pending["state"],
            "replay_raised": replay.get("raised"),
            "replay_irreversible": replay_device.irreversible,
            "gate_occupied": gate.inspect() is not None,
            "record_after_replay": _action_record(db_path, action_key),
        }
    finally:
        try:
            store.close()
        except Exception:
            pass


def _run_evidence_case(case: str, stimuli: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """合同层非法证据：实际调用领域派生/校验，并补真实仓储锁核验。"""
    result: dict[str, Any] = {
        "case": case,
        "layer": "contract_domain_plus_real_store_lock",
    }
    try:
        if case == "same_snapshot":
            before = str(uuid.uuid4())
            outcome = derive_action_outcome(
                _observation(before_snapshot_id=before, after_snapshot_id=before)
            )
            result["outcome"] = outcome.evidence.outcome
            result["effect"] = outcome.evidence.effect
            result["reason"] = outcome.evidence.reason_code
        elif case == "missing_snapshot":
            outcome = derive_action_outcome(
                _observation(before_snapshot_id=None, after_snapshot_id=None)
            )
            result["outcome"] = outcome.evidence.outcome
            result["effect"] = outcome.evidence.effect
        elif case == "performed_none":
            evidence = Evidence(
                action_key="boss:boss:msg:" + "1" * 64 + ":reply",
                outcome="performed",
                effect="none",
                reason_code=None,
                before_snapshot_id=str(uuid.uuid4()),
                after_snapshot_id=str(uuid.uuid4()),
                observed_at=NOW,
            )
            try:
                validate_evidence(evidence)
                result["rejected"] = False
            except ValueError:
                result["rejected"] = True
        elif case == "not_performed_sent":
            evidence = Evidence(
                action_key="boss:boss:msg:" + "1" * 64 + ":reply",
                outcome="not_performed",
                effect="sent",
                reason_code="cancelled_before_action",
                before_snapshot_id=None,
                after_snapshot_id=None,
                observed_at=NOW,
            )
            try:
                validate_evidence(evidence)
                result["rejected"] = False
            except ValueError:
                result["rejected"] = True
        elif case == "read_ok_no_action_delta":
            outcome = derive_action_outcome(
                _observation(clicked=False, echo_count_after=0)
            )
            result["outcome"] = outcome.evidence.outcome
            result["effect"] = outcome.evidence.effect
        elif case == "screenshot_only":
            before = str(uuid.uuid4())
            outcome = derive_action_outcome(
                _observation(
                    before_snapshot_id=before,
                    after_snapshot_id=before,
                    echo_count_after=0,
                )
            )
            result["outcome"] = outcome.evidence.outcome
            result["effect"] = outcome.evidence.effect
        else:
            raise ProbeError("未知证据案例")
        result["real_lock"] = _real_lock_probe(case, stimuli, root)
    except ProbeError:
        raise
    except Exception as error:
        result["raised"] = str(getattr(error, "reason_code", type(error).__name__))
    return result


def _dispatch(case: str, stimuli: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """按案例前缀分派到原始设备链路、人工对账或合同层。"""
    if case.startswith("A0") and "_" in case:
        action_id = case.split("_", 1)[0]
        if action_id in {"A01", "A02", "A03", "A04", "A05"}:
            fault = case.split("_", 1)[1]
            if action_id == "A01" and fault in {"input_method_shift", "no_send_control"}:
                return {
                    "case": case,
                    "layer": "blocked_requires_control_discovery",
                    "blocked": True,
                }
            if fault in {"base", "FA", "FB", "FC", "FD", "FE", "request_only", "delivered", "confirm_dialog", "direct_send"}:
                return _run_action_case(case, stimuli, root)
    if case in {
        "confirm_performed",
        "confirm_not_performed",
        "abandon",
        "reconnect",
    }:
        return _run_reconcile_case(case, stimuli, root)
    if case in {
        "same_snapshot",
        "missing_snapshot",
        "performed_none",
        "not_performed_sent",
        "read_ok_no_action_delta",
        "screenshot_only",
    }:
        return _run_evidence_case(case, stimuli, root)
    raise ProbeError("未知案例")


def main(argv: list[str] | None = None) -> int:
    """解析受控参数，在隔离根执行一个案例并输出单个 JSON。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--case", required=True)
    parser.add_argument("--stimuli", required=True)
    try:
        args = parser.parse_args(argv)
        stimuli_path = _relative(args.stimuli, "stimuli", must_exist=True)
        stimuli = _load_stimuli(stimuli_path)
        result = _dispatch(args.case, stimuli, ROOT)
        payload = {"status": "ok", **result}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except ProbeError as error:
        print(json.dumps({"status": "probe_error", "reason": str(error)}))
        return 2
    except Exception as error:  # noqa: BLE001 - 固定安全出口
        print(
            json.dumps(
                {
                    "status": "probe_error",
                    "reason": type(error).__name__,
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
