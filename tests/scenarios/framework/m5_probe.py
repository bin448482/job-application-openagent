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
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.domain.actions import (
    ActionAuthorization,
    ActionIdentity,
    ActionObservation,
    ActionOutcome,
    ApplyQuotaContract,
    ReconciliationObservation,
    action_key_for,
    derive_action_outcome,
    validate_evidence,
)
from app.domain.runtime import BudgetLimits
from app.domain.models import (
    Candidate,
    Evidence,
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
)
from app.domain.reading import message_key_from_stable, message_stable_identity_hash
from app.ports.store import StoreError
from app.process_log import ProcessLog
from app.usecases.actions import perform_action, reconcile_action

_BASELINE_POPEN = subprocess.Popen
_AUTHORIZATION_NONCES: list[str] = []
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
    process_log: ProcessLog | None = None,
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
        process_log=process_log,
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
    nonce = str(uuid.uuid4())
    _AUTHORIZATION_NONCES.append(nonce)
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
        nonce=nonce,
    )


@contextmanager
def _synthetic_device(device: _Device) -> Iterator[None]:
    """把 ADB 子进程与 Job 创建替换为合成边界；进程账本仍为真实实现。

    同时给原始 `subprocess.Popen.__init__` 加计数包装，实际测量边界窗口内是否
    有代码绕过替换去启动真实进程；计数在退出窗口时归入 `device`。
    """
    original_popen = subprocess.Popen
    original_init = original_popen.__init__
    real_attempts = [0]

    def counting_init(self: object, *args: object, **kwargs: object) -> None:
        """记录一次真实 Popen 构造后按原实现启动。"""
        real_attempts[0] += 1
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    original_create = ProcessFamilyBound.__dict__["create"]
    original_real = command_module._is_real_process
    original_now = actions_module._utc_now
    subprocess.Popen = device.popen  # type: ignore[assignment]
    original_popen.__init__ = counting_init  # type: ignore[method-assign]
    ProcessFamilyBound.create = classmethod(lambda cls: _Bound())  # type: ignore[assignment]
    command_module._is_real_process = lambda process: True  # type: ignore[assignment]
    actions_module._utc_now = lambda: NOW  # type: ignore[assignment]
    try:
        yield
    finally:
        subprocess.Popen = original_popen  # type: ignore[assignment]
        original_popen.__init__ = original_init  # type: ignore[method-assign]
        ProcessFamilyBound.create = original_create  # type: ignore[assignment]
        command_module._is_real_process = original_real  # type: ignore[assignment]
        actions_module._utc_now = original_now  # type: ignore[assignment]
        device.real_popen_attempts += real_attempts[0]


def _os_root(root: Path) -> Path:
    """固定合成 OS 根到被忽略的 data/evals 下，手工运行也不创建仓库根目录。"""
    path = root / "data" / "evals" / "m5-probe" / "os-gate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _process_log(root: Path, case: str) -> ProcessLog:
    """为单个案例创建独立本机过程日志，不与其他案例共享追加。"""
    path = root / "data" / "logs" / f"m5-{case}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return ProcessLog(path=path)


LOG_FIELDS = frozenset(
    {
        "schema_version",
        "ts",
        "correlation_id",
        "stage",
        "operation",
        "result",
        "reason_code",
        "error_category",
        "duration_ms",
        "command",
        "exit_code",
        "mode",
        "config_source",
        "device_count",
        "case_index",
    }
)


def _log_projection(
    log: ProcessLog, *, forbidden_values: tuple[str, ...] = ()
) -> dict[str, Any]:
    """从原始 JSONL 独立构造日志投影：字段白名单、敏感载荷、逐命令分类与闭合。"""
    try:
        raw = log.path.read_text(encoding="utf-8")
    except OSError:
        return {
            "events": [],
            "schema_violations": 0,
            "sensitive_hits": 0,
            "correlations": 0,
            "finish": {"started": 0, "ok": 0, "failed": 0, "unpaired": 0},
            "finish_by_command": {},
            "reasons": {},
            "reasons_by_command": {},
            "association_errors": 0,
        }
    forbidden = (*forbidden_values, BODY, "合成附件简历.pdf", "10,20", "30,40")
    sensitive_hits = sum(1 for item in forbidden if item and item in raw)
    events: list[dict[str, Any]] = []
    schema_violations = 0
    reasons: dict[str, list[str]] = {}
    reasons_by_command: dict[str, list[str]] = {}
    association_errors = 0
    perform: dict[str, Any] | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            schema_violations += 1
            continue
        if not isinstance(row, dict):
            schema_violations += 1
            continue
        if set(row) - LOG_FIELDS:
            schema_violations += 1
        command = row.get("command")
        event = {
            "stage": row.get("stage"),
            "operation": row.get("operation"),
            "result": row.get("result"),
            "reason_code": row.get("reason_code"),
            "error_category": row.get("error_category"),
            "correlation": row.get("correlation_id"),
            "command": command,
        }
        events.append(event)
        result = event["result"]
        operation = event["operation"]
        if result == "failed" and operation:
            reason = f"{event['reason_code']}:{event['error_category']}"
            reasons.setdefault(str(operation), []).append(reason)
            if command is not None:
                reasons_by_command.setdefault(str(command), []).append(reason)
        # 逐命令实例闭合：每个带 command 的 perform started 开启一个实例；有标签
        # finish 必须在该实例内 started→终态恰一组；perform ok 必须有且仅有一组，
        # perform failed/undetermined 允许 0 或 1 组。缺组、重复、乱序、跨实例错
        # 标签都计入 unpaired；无标签共享采集 finish 只保留独立聚合，不伪造归属。
        label = None if command is None else str(command)
        if operation == "perform" and label is not None:
            if result == "started":
                if perform is not None:
                    association_errors += 1
                perform = {
                    "command": label,
                    "groups": 0,
                    "finish_open": False,
                    "terminals": 0,
                }
            elif result in {"ok", "failed", "undetermined"}:
                if perform is None or perform["command"] != label:
                    association_errors += 1
                else:
                    perform["terminals"] += 1
                    if perform["finish_open"]:
                        association_errors += 1
                    if result == "ok" and perform["groups"] != 1:
                        association_errors += 1
                    if perform["groups"] > 1:
                        association_errors += 1
                    if perform["terminals"] != 1:
                        association_errors += 1
                    perform = None
        elif operation == "action_finish_command" and label is not None:
            if result == "started":
                if perform is None or perform["command"] != label:
                    association_errors += 1
                else:
                    if perform["finish_open"]:
                        association_errors += 1
                    perform["finish_open"] = True
                    perform["groups"] += 1
            elif result in {"ok", "failed", "undetermined"}:
                if (
                    perform is None
                    or perform["command"] != label
                    or not perform["finish_open"]
                ):
                    association_errors += 1
                else:
                    perform["finish_open"] = False
    if perform is not None:
        association_errors += 1
    finish_events = [
        item for item in events if item["operation"] == "action_finish_command"
    ]
    started = sum(1 for item in finish_events if item["result"] == "started")
    ok = sum(1 for item in finish_events if item["result"] == "ok")
    failed = sum(1 for item in finish_events if item["result"] == "failed")
    finish_by_command: dict[str, dict[str, int]] = {}
    for item in finish_events:
        label = "" if item["command"] is None else str(item["command"])
        bucket = finish_by_command.setdefault(
            label, {"started": 0, "ok": 0, "failed": 0}
        )
        result = item["result"]
        if result in bucket:
            bucket[result] += 1
    return {
        "events": events,
        "schema_violations": schema_violations,
        "sensitive_hits": sensitive_hits,
        "correlations": len(
            {str(item["correlation"]) for item in events if item["correlation"]}
        ),
        "finish": {
            "started": started,
            "ok": ok,
            "failed": failed,
            "unpaired": abs(started - ok - failed) + association_errors,
        },
        "finish_by_command": finish_by_command,
        "reasons": reasons,
        "reasons_by_command": reasons_by_command,
        "association_errors": association_errors,
    }


def _log_pairs(log: ProcessLog) -> dict[str, list[str]]:
    """把 action 阶段日志压缩为 operation→result 序列，供独立断言。"""
    pairs: dict[str, list[str]] = {}
    for event in _log_projection(log)["events"]:
        if event["stage"] != "action" or not event["operation"]:
            continue
        pairs.setdefault(str(event["operation"]), []).append(str(event["result"]))
    return pairs


def _log_correlations(log: ProcessLog) -> int:
    """返回本案例日志中的唯一 correlation 数。"""
    return int(_log_projection(log)["correlations"])


def _log_has_body(log: ProcessLog, body: str) -> bool:
    """兼容旧断言：投影敏感命中包含正文时返回 True。"""
    projection = _log_projection(log, forbidden_values=(body,))
    return projection["sensitive_hits"] > 0


def _log_summary(
    log: ProcessLog, *, forbidden_values: tuple[str, ...] = ()
) -> dict[str, Any]:
    """输出无敏感原值的日志投影与 action 阶段序列。"""
    projection = _log_projection(log, forbidden_values=forbidden_values)
    return {
        **projection,
        "pairs": _log_pairs(log),
    }


def _attach_log(result: dict[str, Any], log: ProcessLog) -> None:
    """把日志投影与兼容字段绑定到结果，避免各分支重复。"""
    summary = _log_summary(
        log, forbidden_values=tuple(_AUTHORIZATION_NONCES)
    )
    result["log_projection"] = summary
    result["log_pairs"] = summary["pairs"]
    result["log_correlations"] = summary["correlations"]
    result["log_has_body"] = summary["sensitive_hits"] > 0
    result["log_schema_violations"] = summary["schema_violations"]
    result["log_sensitive_hits"] = summary["sensitive_hits"]
    result["log_finish"] = summary["finish"]
    result["log_finish_by_command"] = summary["finish_by_command"]
    result["log_reasons"] = summary["reasons"]
    result["log_reasons_by_command"] = summary["reasons_by_command"]
    result["log_association_errors"] = summary["association_errors"]


def _gate_view(gate: SharedDeviceGate) -> dict[str, Any] | None:
    """只读返回门禁状态与路径摘要，不输出完整行、owner 或 nonce。"""
    row = gate.inspect()
    if row is None:
        return None
    return {
        "state": str(row.get("state")),
        "path_token": canonical_hash(
            str(Path(str(row.get("profile_db_path"))).resolve())
        ),
        "profile_id": row.get("profile_id"),
        "owner_pid": row.get("owner_pid"),
    }


def _task_row(db_path: Path) -> dict[str, Any] | None:
    """只读返回任务状态与租约是否存在，用于 FE/重放后的任务观察。"""
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute(
            "SELECT state, lease_owner, lease_expires_at FROM hr_discoveries "
            "WHERE state IN ('leased', 'running', 'blocked', 'done', 'not_sent') "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "state": row[0],
        "has_lease": row[1] is not None,
        "has_expiry": row[2] is not None,
    }


def _ledger_summary(directory: Path) -> dict[str, Any]:
    """只读统计真实命令账本文件/行/阶段，不返回 PID 或键值。

    真实 begin 只认固定 `{"begin":true}` 标记；PID 行不代替 begin。闭合要求
    单文件、has begin、真实 begin==complete、无 unresolved、每条 complete 都
    有先前 begin，且 pid 只出现在已开始的命令内。坏行/错序保持未闭合。
    这里的 `closed` 是本场景的严格证据完整性判断，不是生产
    `CommandIdentityStore.check` 的返回值；`check` 只做记录计数与进程停止证明，
    两者在 complete-before-begin 等边界上可以不同，不互相冒充。
    """
    files = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    begin = 0
    complete = 0
    unresolved = 0
    pids = 0
    order_errors = 0
    open_commands = 0
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                unresolved += 1
                continue
            if not isinstance(item, dict):
                unresolved += 1
                continue
            if item.get("unresolved") is True:
                unresolved += 1
                continue
            if item.get("begin") is True:
                begin += 1
                open_commands += 1
                continue
            if item.get("complete") is True:
                complete += 1
                if open_commands <= 0:
                    order_errors += 1
                else:
                    open_commands -= 1
                continue
            if "pid" in item:
                pid = item.get("pid")
                if type(pid) is not int or pid <= 0:
                    unresolved += 1
                    continue
                pids += 1
                if open_commands <= 0:
                    order_errors += 1
                continue
            unresolved += 1
    return {
        "files": len(files),
        "begin": begin,
        "complete": complete,
        "unresolved": unresolved,
        "pids": pids,
        "order_errors": order_errors,
        "closed": len(files) == 1
        and begin > 0
        and begin == complete
        and unresolved == 0
        and order_errors == 0
        and open_commands == 0
        and pids > 0,
    }


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
        self.served_xml: list[str] = []
        self.on_cat: Callable[[int, str], None] | None = None
        self.taps = 0
        self.irreversible = 0
        self.failed_after_irreversible = False
        self._next_pid = 7000
        self.real_popen_attempts = 0

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
            text = self.xml_queue.pop(0)
            index = len(self.served_xml)
            self.served_xml.append(text)
            if self.on_cat is not None:
                self.on_cat(index, text)
            return text, 0
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
    process_log: ProcessLog | None = None,
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
        process_log=process_log,
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


def _reopen_store(db_path: Path, profile_id: str, log: ProcessLog) -> SqliteStore:
    """关闭后重建同 profile 仓储连接，用于真正的重开重放。"""
    return SqliteStore(
        db_path,
        profile_id=profile_id,
        local_host=OWNER_HOST,
        monotonic=lambda: 1000.0,
        stop_checker=lambda pid: "stopped",
        process_log=log,
    )


def _job_state(db_path: Path) -> dict[str, Any] | None:
    """只读返回合成职位任务状态与租约存在性，不返回职位正文。"""
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute(
            "SELECT state, lease_owner, lease_expires_at FROM jobs "
            "WHERE state IN ('queued', 'leased', 'running', 'blocked', 'done', 'not_sent') "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "state": row[0],
        "has_lease": row[1] is not None,
        "has_expiry": row[2] is not None,
    }


def _job_profile_context(
    profile_id: str,
    os_root: Path,
    process_log: ProcessLog | None = None,
    fault_hook: Callable[[str, object], None] | None = None,
    *,
    project_root: Path = ROOT,
) -> tuple[SqliteStore, Any, SharedDeviceGate, Path, str, str]:
    """先取得共享门禁，再在显式项目根创建/打开 profile v3 并领取职位。"""
    db_path = profile_runtime_path(project_root, profile_id)
    owner = _owner()
    gate = SharedDeviceGate(
        profile_id=profile_id, profile_db_path=db_path, os_root=os_root
    )
    gate.acquire(
        profile_id=profile_id, profile_db_path=str(db_path), owner=owner, now=NOW
    )
    try:
        profile = SqliteProfileStore(project_root, profile_id, create=True, now=NOW)
        profile.open()
        profile_path = profile.path
        profile.close()
        if profile_path != db_path:
            raise ProbeError("profile 路径与门禁绑定不一致")
        store = SqliteStore(
            db_path,
            profile_id=profile_id,
            local_host=OWNER_HOST,
            monotonic=lambda: 1000.0,
            stop_checker=lambda pid: "stopped",
            fault_hook=fault_hook,
            process_log=process_log,
            require_runtime=True,
        )
        batch = str(uuid.uuid4())
        job = "boss:job:" + canonical_hash({"job": f"apply-{profile_id}"})
        store.ensure_batch(batch, BudgetLimits(), NOW)
        store.enqueue_jobs(batch, (job,), NOW)
        claimed = store.claim_job(batch, owner, NOW, job)
        if claimed is None:
            store.close()
            raise ProbeError("合成职位领取失败")
        lease = store.start_job(claimed, NOW)
    except Exception:
        try:
            gate.release(now=NOW, converged=False)
        except Exception:
            pass
        raise
    return store, lease, gate, db_path, job, batch


def _apply_quota(
    *,
    profile_id: str,
    task_key: str,
    action_key: str,
    allowance: int = 1,
    used: int = 0,
) -> ApplyQuotaContract:
    """构造绑定当前 job 任务与动作键的显式合成 apply 配额合同。"""
    return ApplyQuotaContract(
        profile_id=profile_id,
        task_key=task_key,
        action_key=action_key,
        allowance=allowance,
        used=used,
        confirmed=True,
        nonce=str(uuid.uuid4()),
        expires_at=EXPIRES,
    )


def _apply_stimulus_for_fault(
    base: Mapping[str, Any], fault: str
) -> dict[str, Any]:
    """按 apply 故障窗口变形原始 XML；只改外部刺激，不写产品结论。"""
    sequence = list(base["xml_sequence"])
    if fault == "FD":
        sequence[-1] = sequence[0]
    return {
        "ime": base["ime"],
        "target_ime": None,
        "xml_sequence": sequence,
        "taps": [],
        "irreversible_index": 0,
    }


def _run_apply_case(case: str, stimuli: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """用真实 perform_action 跑 apply_job 六个窗口；只输出可观察原始事实。"""
    _, _, fault = case.partition("_")
    base = stimuli["actions"]["apply"]
    stimulus = _apply_stimulus_for_fault(base, fault)
    controls: dict[str, Any] = {}
    if fault == "base":
        done = base["xml_sequence"][1]
        controls["preexisting_done"] = _apply_once(
            root, "preexisting", {**stimulus, "xml_sequence": [done, done]}
        )
        unrelated = base["xml_sequence"][0].replace(
            "</hierarchy>",
            '<node text="合成邻居乙" class="android.widget.TextView" bounds="[0,1800][1080,1900]"/></hierarchy>',
        )
        controls["unrelated_change"] = _apply_once(
            root,
            "unrelated",
            {**stimulus, "xml_sequence": [base["xml_sequence"][0], unrelated]},
        )
        external_done = done.replace(
            "</hierarchy>",
            '<node text="合成邻居乙" class="android.widget.TextView" bounds="[0,1800][1080,1900]"/></hierarchy>',
        )
        external_done_changed = external_done.replace(
            'text="合成邻居乙"', 'text="合成邻居乙更新"'
        )
        controls["preexisting_done_unrelated_change"] = _apply_once(
            root,
            "preexisting_unrelated",
            {**stimulus, "xml_sequence": [external_done, external_done_changed]},
        )
    if fault == "FD":
        first = base["xml_sequence"][0]
        controls["toast"] = _apply_once(
            root,
            "toast",
            {
                **stimulus,
                "xml_sequence": [
                    first,
                    first.replace(
                        "</hierarchy>",
                        '<node class="android.widget.Toast" text="已投递" bounds="[0,1700][1080,1800]"/></hierarchy>',
                    ),
                ],
            },
        )
        controls["dialog"] = _apply_once(
            root,
            "dialog",
            {
                **stimulus,
                "xml_sequence": [
                    first,
                    first.replace(
                        "</hierarchy>",
                        '<node class="android.app.Dialog" text="确认框" bounds="[0,1500][1080,1750]"><node text="确认" clickable="true" bounds="[0,1600][500,1700]"/><node text="取消" clickable="true" bounds="[520,1600][1000,1700]"/></node></hierarchy>',
                    ),
                ],
            },
        )
        controls["empty"] = _apply_once(
            root,
            "empty",
            {**stimulus, "xml_sequence": [first, '<hierarchy rotation="0"></hierarchy>']},
        )
    profile_id = "m6-apply"
    log = _process_log(root, case)
    store, lease, gate, db_path, job, batch = _job_profile_context(
        profile_id,
        _os_root(root),
        log,
        _completion_fault if fault == "FE" else None,
    )
    action_key = action_key_for("apply_job", app="boss", message_key=job)
    result: dict[str, Any] = {
        "case": case,
        "action_kind": "apply_job",
        "layer": "raw_device_usecase",
        "job_key_shape": job.split(":")[0] + ":" + job.split(":")[1],
        "batch_present": isinstance(batch, str) and bool(batch),
        "controls": controls,
    }
    reopened: SqliteStore | None = None

    def authorization(confirmed: bool = True) -> ActionAuthorization:
        """构造绑定当前 job 任务的精确合成授权。"""
        return _authorization(
            lease,
            profile_id=profile_id,
            action_kind="apply_job",
            action_key=action_key,
            confirmed=confirmed,
        )

    def admit(dev: _Device, store_: SqliteStore, executor: AdbActionExecutor, *, confirmed: bool = True) -> dict[str, Any]:
        """调用真实 perform_action；异常只压缩为固定原因码。"""
        try:
            with _synthetic_device(dev):
                value = perform_action(
                    store=store_,
                    gate=gate,
                    authorization=authorization(confirmed),
                    lease=lease,
                    action_kind="apply_job",
                    action_key=action_key,
                    target_key=job,
                    conversation_key=job,
                    body_text=None,
                    attachment_name=None,
                    identity=_identity(),
                    anchor_label=None,
                    ime_component=None,
                    tap_points=(),
                    current_base_hash=BASE_HASH,
                    current_pack_hash=PACK_HASH,
                    daily_sends=5,
                    reply_cooldown_seconds=0,
                    profile_db_path=str(db_path),
                    executor=executor,
                    now=NOW,
                    apply_quota=_apply_quota(
                        profile_id=profile_id,
                        task_key=lease.key,
                        action_key=action_key,
                    ),
                    process_log=log,
                    clock=lambda: NOW,
                    reload_snapshot=lambda: {
                        "base_hash": BASE_HASH,
                        "pack_hash": PACK_HASH,
                        "acceptance_base_hash": BASE_HASH,
                        "acceptance_pack_hash": PACK_HASH,
                    },
                )
        except Exception as error:
            return {
                "raised": str(getattr(error, "reason_code", type(error).__name__)),
                "state": None,
                "evidence": None,
            }
        return {
            "raised": None,
            "state": value.get("state"),
            "evidence": value.get("evidence"),
            "gate_converged": value.get("gate_converged"),
            "card": value.get("card"),
            "requested_effect": value.get("requested_effect"),
        }

    try:
        if fault == "FB":
            fb_device = _Device(stimulus)
            crashing = _executor(
                gate=gate,
                store=store,
                root=root,
                device=fb_device,
                process_log=log,
                crash=True,
            )
            try:
                admit(
                    fb_device,
                    store,
                    crashing,
                )
            except SystemExit:
                result["interrupted"] = True
            result["after_interrupt"] = _action_record(db_path, action_key)
            result["job_after_interrupt"] = _job_state(db_path)
            same_device = _Device(stimulus)
            result["same_connection_replay"] = {
                **admit(
                    same_device,
                    store,
                    _executor(
                        gate=gate,
                        store=store,
                        root=root,
                        device=same_device,
                        process_log=log,
                    ),
                ),
                "irreversible": same_device.irreversible,
                "reopened": False,
            }
            store.close()
            reopened = _reopen_store(db_path, profile_id, log)
            replay_device = _Device(stimulus)
            result["replay"] = {
                **admit(
                    replay_device,
                    reopened,
                    _executor(
                        gate=gate,
                        store=reopened,
                        root=root,
                        device=replay_device,
                        process_log=log,
                    ),
                ),
                "irreversible": replay_device.irreversible,
                "all_commands": len(replay_device.calls),
                "reopened": True,
            }
            result["record_after_replay"] = _action_record(db_path, action_key)
            result["job_after_replay"] = _job_state(db_path)
            result["ledger"] = _ledger_summary(
                root / "data" / "evals" / "m5-probe" / "ledger"
            )
            result["real_popen_attempts"] = fb_device.real_popen_attempts
            _attach_log(result, log)
            return result

        if fault == "FC":
            device = _Device(stimulus, fail_irreversible=True)
        else:
            device = _Device(stimulus)
        executor = _executor(
            gate=gate,
            store=store,
            root=root,
            device=device,
            process_log=log,
        )
        outcome = admit(
            device,
            store,
            executor,
            confirmed=fault != "FA",
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
        result["record"] = _action_record(db_path, action_key)
        result["events"] = _event_rows(db_path, action_key)
        result["job"] = _job_state(db_path)
        if fault in {"base", "FC", "FE"}:
            store.close()
            reopened = _reopen_store(db_path, profile_id, log)
            replay_device = _Device(stimulus)
            replay = admit(
                replay_device,
                reopened,
                _executor(
                    gate=gate,
                    store=reopened,
                    root=root,
                    device=replay_device,
                    process_log=log,
                ),
            )
            result["replay"] = {
                **replay,
                "irreversible": replay_device.irreversible,
                "all_commands": len(replay_device.calls),
                "reopened": True,
                "events": _event_rows(db_path, action_key),
                "job": _job_state(db_path),
            }
        result["ledger"] = _ledger_summary(
            root / "data" / "evals" / "m5-probe" / "ledger"
        )
        result["real_popen_attempts"] = device.real_popen_attempts
        result["gate_before_release"] = _gate_view(gate)
        converged = bool((outcome.get("gate_converged") if outcome else None))
        try:
            gate.release(now=NOW, converged=converged)
        except Exception:
            pass
        result["gate_after_release"] = _gate_view(gate)
        _attach_log(result, log)
        return result
    finally:
        for connection in (reopened, store):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def _xml_facts(before: str, after: str, target_text: str) -> dict[str, Any]:
    """独立解析原始 XML：目标子树 canonical/相等、目标外变化与 Toast/Dialog 结构。"""
    import xml.etree.ElementTree as ET

    def canonical(node: ET.Element) -> dict[str, Any]:
        """递归投影节点结构，不包含产品判定。"""
        return {
            "tag": node.tag,
            "attrs": dict(sorted(node.attrib.items())),
            "children": [canonical(child) for child in list(node)],
        }

    def target_canonical(xml_text: str) -> dict[str, Any] | None:
        """返回唯一目标节点的 canonical 投影；缺失/重复/坏 XML 为 None。"""
        try:
            root = ET.fromstring(xml_text)
        except (ET.ParseError, ValueError):
            return None
        matches = [node for node in root.iter("node") if node.get("text") == target_text]
        if len(matches) != 1:
            return None
        return canonical(matches[0])

    def outside_texts(xml_text: str) -> list[str]:
        """返回目标节点之外的文本集合，用于目标外变化比较。"""
        try:
            root = ET.fromstring(xml_text)
        except (ET.ParseError, ValueError):
            return []
        matches = [node for node in root.iter("node") if node.get("text") == target_text]
        if len(matches) != 1:
            return [node.get("text") or "" for node in root.iter("node")]
        target = matches[0]
        target_ids = {id(node) for node in target.iter("node")}
        return [
            node.get("text") or ""
            for node in root.iter("node")
            if id(node) not in target_ids
        ]

    def has_class(root: ET.Element, token: str) -> bool:
        """判断任意节点 class 是否包含固定 token。"""
        return any(token in (node.get("class") or "") for node in root.iter("node"))

    def dialog_nodes(xml_text: str) -> tuple[bool, bool]:
        """返回是否存在独立 Dialog 容器与确认/取消孩子。"""
        try:
            root = ET.fromstring(xml_text)
        except (ET.ParseError, ValueError):
            return False, False
        container = False
        controls = False
        for node in root.iter("node"):
            if "Dialog" in (node.get("class") or "") or "dialog" in (node.get("class") or ""):
                texts = {child.get("text") for child in node.iter("node")}
                if {"确认", "取消"} <= texts:
                    container = True
                    controls = True
        return container, controls

    target_before = target_canonical(before)
    target_after = target_canonical(after)
    outside_before = outside_texts(before)
    outside_after = outside_texts(after)
    try:
        after_root = ET.fromstring(after)
    except (ET.ParseError, ValueError):
        after_root = None
    try:
        before_root = ET.fromstring(before)
    except (ET.ParseError, ValueError):
        before_root = None
    toast_present = bool(
        after_root is not None
        and has_class(after_root, "Toast")
        and any(
            node.get("text") == "已投递" and "Toast" in (node.get("class") or "")
            for node in after_root.iter("node")
        )
    )
    dialog_present, dialog_controls = (
        dialog_nodes(after) if after_root is not None else (False, False)
    )
    target_done = bool(
        target_after is not None
        and any(
            (node.get("text") or "") in {"已申请", "继续沟通"}
            for node in _iter_canonical_texts(target_after)
        )
    )
    return {
        "target_before_sha": canonical_hash(target_before),
        "target_after_sha": canonical_hash(target_after),
        "target_unchanged": target_before is not None and target_before == target_after,
        "target_after_done": target_done,
        "outside_before": outside_before,
        "outside_after": outside_after,
        "external_changed": sorted(outside_before) != sorted(outside_after),
        "toast_present": toast_present,
        "dialog_container_present": dialog_present,
        "dialog_controls_present": dialog_controls,
    }


def _iter_canonical_texts(value: dict[str, Any]):
    """遍历 canonical 投影中的文本，供独立目标状态检查。"""
    text = value.get("attrs", {}).get("text")
    if text is not None:
        yield {"text": text}
    for child in value.get("children", []):
        yield from _iter_canonical_texts(child)


def _profile_meta(db_path: Path) -> dict[str, Any] | None:
    """只读返回真实 profile_meta 身份行。"""
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute(
            "SELECT profile_id, schema_version FROM profile_meta LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {"profile_id": row[0], "schema_version": row[1]}


def _relative_token(path: Path, root: Path) -> str:
    """返回隔离根内相对 token；无法相对时只留文件名。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _rel_or_none(path: Path, base: Path) -> str | None:
    """返回相对 token；不在 base 内返回 None。"""
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return None


def _binding_facts(
    root: Path,
    control_root: Path,
    control_os: Path,
    label: str,
    profile_id: str,
    db_path: Path,
    profile_meta: dict[str, Any] | None,
    gate_view: dict[str, Any] | None,
) -> dict[str, Any]:
    """由真实路径/DB 行/门禁行独立计算控制环境绑定一致性。"""
    project_ok = _rel_or_none(control_root, root) == f"controls/{label}"
    os_ok = _rel_or_none(control_os, root) == f"controls/{label}/os"
    db_ok = (
        _rel_or_none(db_path, control_root)
        == f"data/runtime/profiles/{profile_id}/runtime.sqlite3"
    )
    profile_ok = bool(profile_meta and profile_meta.get("profile_id") == profile_id)
    gate_profile_ok = bool(gate_view and gate_view.get("profile_id") == profile_id)
    gate_path_ok = bool(
        gate_view
        and gate_view.get("path_token") == canonical_hash(str(db_path.resolve()))
    )
    gate_state_ok = bool(gate_view and gate_view.get("state") == "held")
    ok = all(
        [
            project_ok,
            os_ok,
            db_ok,
            profile_ok,
            gate_profile_ok,
            gate_path_ok,
            gate_state_ok,
        ]
    )
    return {
        "project_root_ok": project_ok,
        "os_root_ok": os_ok,
        "db_path_ok": db_ok,
        "profile_meta_ok": profile_ok,
        "gate_profile_ok": gate_profile_ok,
        "gate_path_ok": gate_path_ok,
        "gate_state_ok": gate_state_ok,
        "ok": ok,
    }


def _apply_once(
    root: Path,
    label: str,
    stimulus: Mapping[str, Any],
    *,
    confirmed: bool = True,
) -> dict[str, Any]:
    """在独立合成 job/profile 上执行一次 apply 观察；只返回真实原始事实。"""
    profile_id = f"m6-apply-{label}"
    control_root = root / "controls" / label
    control_root.mkdir(parents=True, exist_ok=True)
    control_os = control_root / "os"
    control_os.mkdir(parents=True, exist_ok=True)
    log = _process_log(control_root, f"apply-{label}")
    store, lease, gate, db_path, job, _batch = _job_profile_context(
        profile_id,
        control_os,
        log,
        project_root=control_root,
    )
    action_key = action_key_for("apply_job", app="boss", message_key=job)
    device = _Device(stimulus)
    executor = _executor(
        gate=gate, store=store, root=control_root, device=device, process_log=log
    )
    try:
        try:
            with _synthetic_device(device):
                value = perform_action(
                    store=store,
                    gate=gate,
                    authorization=_authorization(
                        lease,
                        profile_id=profile_id,
                        action_kind="apply_job",
                        action_key=action_key,
                        confirmed=confirmed,
                    ),
                    lease=lease,
                    action_kind="apply_job",
                    action_key=action_key,
                    target_key=job,
                    conversation_key=job,
                    body_text=None,
                    attachment_name=None,
                    identity=_identity(),
                    anchor_label=None,
                    ime_component=None,
                    tap_points=(),
                    current_base_hash=BASE_HASH,
                    current_pack_hash=PACK_HASH,
                    daily_sends=5,
                    reply_cooldown_seconds=0,
                    profile_db_path=str(db_path),
                    executor=executor,
                    now=NOW,
                    apply_quota=_apply_quota(
                        profile_id=profile_id,
                        task_key=lease.key,
                        action_key=action_key,
                    ),
                    process_log=log,
                    clock=lambda: NOW,
                    reload_snapshot=lambda: {
                        "base_hash": BASE_HASH,
                        "pack_hash": PACK_HASH,
                        "acceptance_base_hash": BASE_HASH,
                        "acceptance_pack_hash": PACK_HASH,
                    },
                )
        except Exception as error:
            observed = {
                "raised": str(getattr(error, "reason_code", type(error).__name__)),
                "state": None,
                "evidence": None,
                "gate_converged": False,
            }
        else:
            observed = {
                "raised": None,
                "state": value.get("state"),
                "evidence": value.get("evidence"),
                "gate_converged": value.get("gate_converged"),
            }
        record = _action_record(db_path, action_key)
        facts = _xml_facts(
            str(stimulus["xml_sequence"][0]),
            str(stimulus["xml_sequence"][1]),
            _identity().contact,
        )
        result = {
            "observed": observed,
            "irreversible": device.irreversible,
            "record": record,
            "ledger": _ledger_summary(control_root / "data" / "evals" / "m5-probe" / "ledger"),
            "env": {
                "project_root": f"controls/{label}",
                "os_root": f"controls/{label}/os",
                "db_path_token": _relative_token(db_path, root),
                "profile_meta": _profile_meta(db_path),
                "gate": _gate_view(gate),
                "binding": _binding_facts(
                    root,
                    control_root,
                    control_os,
                    label,
                    profile_id,
                    db_path,
                    _profile_meta(db_path),
                    _gate_view(gate),
                ),
                "binding_ok": _binding_facts(
                    root,
                    control_root,
                    control_os,
                    label,
                    profile_id,
                    db_path,
                    _profile_meta(db_path),
                    _gate_view(gate),
                )["ok"],
            },
            "facts": facts,
            "after_consumed": len(device.served_xml) > 1,
            "served_xml": len(device.served_xml),
            "real_popen_attempts": device.real_popen_attempts,
        }
        _attach_log(result, log)
    finally:
        try:
            gate.release(
                now=NOW,
                converged=bool(observed.get("gate_converged")) if "observed" in locals() else False,
            )
        except Exception:
            pass
        store.close()
    return result


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
    log = _process_log(root, case)
    store, lease, gate, db_path = _profile_context(
        profile_id,
        _os_root(root),
        log,
        _completion_fault if fault == "FE" else None,
    )
    plan = _plan_for(action_id, kind, action_key, conversation, stimulus)
    device = _Device(stimulus)
    dialog_checks: list[dict[str, Any]] = []

    def on_cat(index: int, text: str) -> None:
        """在原始 dialog cat 时独立读取真实仓储与门禁，不代替产品判定。"""
        if index != 1:
            return
        record = store.get_action(action_key)
        dialog_checks.append(
            {
                "phase": "dialog",
                "action_state": None if record is None else str(record["state"]),
                "gate_state": (gate.inspect() or {}).get("state"),
                "has_confirm": "确认" in text and "取消" in text,
            }
        )

    if action_id in {"A03", "A04", "A05"}:
        device.on_cat = on_cat
    result: dict[str, Any] = {
        "case": case,
        "action_kind": kind,
        "action_key_shape": action_key.split(":")[0] + ":" + action_key.split(":")[-1],
        "layer": "raw_device_usecase",
        "dialog_distinct": (
            len(stimulus["xml_sequence"]) > 2
            and stimulus["xml_sequence"][1] != stimulus["xml_sequence"][0]
        ),
    }
    reopened: SqliteStore | None = None
    boundary_store: SqliteStore | None = None
    boundary_gate: SharedDeviceGate | None = None
    try:
        if fault == "FB":
            crashing = _executor(
                gate=gate,
                store=store,
                root=root,
                device=device,
                process_log=log,
                crash=True,
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
                    process_log=log,
                )
            except SystemExit:
                result["interrupted"] = True
            result["after_interrupt"] = _action_record(db_path, action_key)
            result["remote_after_interrupt"] = {
                "irreversible": device.irreversible,
                "all_commands": len(device.calls),
            }
            result["task_after_interrupt"] = _task_row(db_path)
            same_device = _Device(stimulus)
            result["same_connection_replay"] = {
                **_perform(
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
                        gate=gate,
                        store=store,
                        root=root,
                        device=same_device,
                        process_log=log,
                    ),
                    device=same_device,
                    plan=plan,
                    process_log=log,
                ),
                "irreversible": same_device.irreversible,
                "reopened": False,
            }
            store.close()
            reopened = _reopen_store(db_path, profile_id, log)
            replay_device = _Device(stimulus)
            result["replay"] = {
                **_perform(
                    store=reopened,
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
                        gate=gate,
                        store=reopened,
                        root=root,
                        device=replay_device,
                        process_log=log,
                    ),
                    device=replay_device,
                    plan=plan,
                    process_log=log,
                ),
                "irreversible": replay_device.irreversible,
                "all_commands": len(replay_device.calls),
                "reopened": True,
            }
            # 变异控制：移除崩溃点后，正常执行仍必须只触达合成 Popen。
            boundary_root = _os_root(root) / "boundary"
            boundary_root.mkdir(parents=True, exist_ok=True)
            boundary_log = _process_log(root, f"{case}-boundary")
            boundary_store, boundary_lease, boundary_gate, boundary_db = _profile_context(
                "m5-boundary", boundary_root, boundary_log
            )
            boundary_device = _Device(stimulus)
            boundary = _perform(
                store=boundary_store,
                gate=boundary_gate,
                lease=boundary_lease,
                authorization=_authorization(
                    boundary_lease,
                    profile_id="m5-boundary",
                    action_kind=kind,
                    action_key=action_key,
                ),
                profile_db_path=str(boundary_db),
                executor=_executor(
                    gate=boundary_gate,
                    store=boundary_store,
                    root=root,
                    device=boundary_device,
                    process_log=boundary_log,
                ),
                device=boundary_device,
                plan=plan,
                process_log=boundary_log,
            )
            result["boundary_control"] = {
                "all_commands": len(boundary_device.calls),
                "served_xml": len(boundary_device.served_xml),
                "raised": boundary.get("raised"),
                "real_popen_restored": subprocess.Popen is _BASELINE_POPEN,
                "real_popen_attempts": boundary_device.real_popen_attempts,
            }
            _attach_log(result, log)
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
        if action_id == "A04" and fault in {"base", "confirm_dialog"}:
            cross_log = _process_log(root, f"{case}-cross-kind")
            cross_device = _Device(stimulus)
            cross = _perform(
                store=store,
                gate=gate,
                lease=lease,
                authorization=_authorization(
                    lease,
                    profile_id=profile_id,
                    action_kind="change_wechat",
                    action_key=action_key,
                ),
                profile_db_path=str(db_path),
                executor=_executor(
                    gate=gate,
                    store=store,
                    root=root,
                    device=cross_device,
                    process_log=cross_log,
                ),
                device=cross_device,
                plan=plan,
                process_log=cross_log,
            )
            result["cross_kind"] = {
                "raised": cross.get("raised"),
                "irreversible": cross_device.irreversible,
                "record": _action_record(db_path, action_key),
            }
        if fault == "FC":
            device = _Device(stimulus, fail_irreversible=True)
            if action_id in {"A03", "A04", "A05"}:
                device.on_cat = on_cat
        executor = _executor(
            gate=gate,
            store=store,
            root=root,
            device=device,
            process_log=log,
        )
        outcome = _perform(
            store=store,
            gate=gate,
            lease=lease,
            authorization=authorization,
            profile_db_path=str(db_path),
            executor=executor,
            device=device,
            plan=plan,
            process_log=log,
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
        result["dialog_checks"] = dialog_checks
        result["record"] = _action_record(db_path, action_key)
        result["events"] = _event_rows(db_path, action_key)
        result["task"] = _task_row(db_path)
        if fault in {"base", "FC", "FE", "request_only", "delivered", "confirm_dialog"}:
            store.close()
            reopened = _reopen_store(db_path, profile_id, log)
            replay_device = _Device(stimulus)
            replay = _perform(
                store=reopened,
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
                    gate=gate,
                    store=reopened,
                    root=root,
                    device=replay_device,
                    process_log=log,
                ),
                device=replay_device,
                plan=plan,
                process_log=log,
            )
            key = "replay" if fault in {"FC", "FE"} else "base_replay"
            result[key] = {
                **replay,
                "irreversible": replay_device.irreversible,
                "all_commands": len(replay_device.calls),
                "reopened": True,
                "events": _event_rows(db_path, action_key),
                "task": _task_row(db_path),
            }
        if fault == "FE":
            result["ledger"] = _ledger_summary(
                root / "data" / "evals" / "m5-probe" / "ledger"
            )
            result["events_after_replay"] = _event_rows(db_path, action_key)
        result["gate_before_release"] = _gate_view(gate)
        converged = bool(outcome.get("gate_converged"))
        try:
            gate.release(now=NOW, converged=converged)
        except Exception:
            pass
        result["gate_after_release"] = _gate_view(gate)
        _attach_log(result, log)
        return result
    finally:
        for connection in (reopened, boundary_store, store):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if boundary_gate is not None:
            try:
                boundary_gate.release(now=NOW, converged=False)
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
    process_log: ProcessLog | None = None,
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
                process_log=process_log,
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
    profile_id = "m5-reconcile"
    log = _process_log(root, f"reconcile-{case}")
    store, lease, gate, db_path = _profile_context(
        profile_id,
        _os_root(root),
        log,
        fault_hook=_completion_fault if case == "confirm_performed" else None,
    )
    plan = _plan_for("A01", kind, action_key, conversation, stimulus)
    device = _Device(stimulus)
    if case == "confirm_not_performed":
        device = _Device(stimulus, fail_irreversible=True)
    elif case == "abandon":
        device = _Device(stimulus, fail_irreversible=True)
    reopened: SqliteStore | None = None
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
                process_log=log,
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
        result["gate_after_reconcile"] = _gate_view(gate)
        _attach_log(result, log)
        if case == "confirm_not_performed":
            store.close()
            reopened = _reopen_store(db_path, profile_id, log)
            replay_device = _Device(stimulus)
            replay = _perform(
                store=reopened,
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
                    gate=gate,
                    store=reopened,
                    root=root,
                    device=replay_device,
                    process_log=log,
                ),
                device=replay_device,
                plan=plan,
                process_log=log,
            )
            result["post_reconcile_replay"] = {
                **replay,
                "irreversible": replay_device.irreversible,
                "reopened": True,
            }
            result["record_after_post_replay"] = _action_record(
                db_path, action_key
            )
        return result
    finally:
        for connection in (reopened, store):
            if connection is not None:
                try:
                    connection.close()
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


def _run_evidence_case(case: str, stimuli: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """同一非法/原始证据进入真实派生与 complete_action，再重开核验锁。"""
    kind, slug = ACTION_LAYOUT["A01"]
    conversation = conversation_key_for("boss", CONTACT, COMPANY, POSITION)
    message_key = message_key_from_stable(conversation, STABLE)
    action_key = action_key_for(
        kind, app="boss", conversation_key=conversation, message_key=message_key
    )
    profile_id = "m5-evidence"
    log = _process_log(root, f"evidence-{case}")
    store, lease, gate, db_path = _profile_context(
        profile_id, _os_root(root), log
    )
    base = stimuli["actions"][slug]
    stimulus = _stimulus_for_fault(base, "FD", "A01")
    plan = _plan_for("A01", kind, action_key, conversation, stimulus)
    result: dict[str, Any] = {
        "case": case,
        "layer": "contract_domain_plus_complete_action",
        "remote": {"irreversible": 0, "all_commands": 0},
    }
    reopened: SqliteStore | None = None
    try:
        store.begin_action(
            lease,
            action_kind=kind,
            action_key=action_key,
            target_key=plan["target_key"],
            conversation_key=conversation,
            authorization_json=json.dumps({"synthetic": True}),
            daily_sends=5,
            reply_cooldown_seconds=0,
            ime_original=None,
            now=NOW,
        )
        if case == "performed_none":
            evidence = Evidence(
                action_key=action_key,
                outcome="performed",
                effect="none",
                reason_code=None,
                before_snapshot_id=str(uuid.uuid4()),
                after_snapshot_id=str(uuid.uuid4()),
                observed_at=NOW,
            )
            outcome = ActionOutcome(
                evidence=evidence,
                scene_settled=True,
                confirmation_open=False,
                ime_restored=True,
                command_quiet=True,
                pause=False,
                delivered=False,
            )
            result["derivation"] = {"constructed": "performed_none"}
        elif case == "not_performed_sent":
            evidence = Evidence(
                action_key=action_key,
                outcome="not_performed",
                effect="sent",
                reason_code="cancelled_before_action",
                before_snapshot_id=None,
                after_snapshot_id=None,
                observed_at=NOW,
            )
            outcome = ActionOutcome(
                evidence=evidence,
                scene_settled=True,
                confirmation_open=False,
                ime_restored=True,
                command_quiet=True,
                pause=False,
                delivered=False,
            )
            result["derivation"] = {"constructed": "not_performed_sent"}
        else:
            if case == "same_snapshot":
                before = str(uuid.uuid4())
                observation = _observation(
                    action_key=action_key,
                    before_snapshot_id=before,
                    after_snapshot_id=before,
                )
            elif case == "missing_snapshot":
                observation = _observation(
                    action_key=action_key,
                    before_snapshot_id=None,
                    after_snapshot_id=None,
                )
            elif case == "read_ok_no_action_delta":
                observation = _observation(
                    action_key=action_key,
                    clicked=False,
                    echo_count_after=0,
                )
            else:
                before = str(uuid.uuid4())
                observation = _observation(
                    action_key=action_key,
                    before_snapshot_id=before,
                    after_snapshot_id=before,
                    echo_count_after=0,
                )
            outcome = derive_action_outcome(observation)
            result["derivation"] = {
                "outcome": outcome.evidence.outcome,
                "effect": outcome.evidence.effect,
                "reason": outcome.evidence.reason_code,
            }
        try:
            completed = store.complete_action(
                lease, action_key=action_key, outcome=outcome, now=NOW
            )
            result["complete"] = {
                "raised": None,
                "state": completed.get("state"),
                "gate_converged": completed.get("gate_converged"),
            }
        except Exception as error:
            result["complete"] = {
                "raised": str(getattr(error, "reason_code", type(error).__name__)),
                "state": None,
                "gate_converged": False,
            }
        result["record"] = _action_record(db_path, action_key)
        result["events"] = _event_rows(db_path, action_key)
        result["task"] = _task_row(db_path)
        gate_view = _gate_view(gate)
        result["gate_state"] = None if gate_view is None else gate_view["state"]
        store.close()
        reopened = _reopen_store(db_path, profile_id, log)
        try:
            reopened.begin_action(
                lease,
                action_kind=kind,
                action_key=action_key,
                target_key=plan["target_key"],
                conversation_key=conversation,
                authorization_json=json.dumps({"synthetic": True}),
                daily_sends=5,
                reply_cooldown_seconds=0,
                ime_original=None,
                now=NOW,
            )
            result["reopen_replay"] = {"raised": None}
        except Exception as error:
            result["reopen_replay"] = {
                "raised": str(getattr(error, "reason_code", type(error).__name__))
            }
        result["reopen_record"] = _action_record(db_path, action_key)
        result["reopen_events"] = _event_rows(db_path, action_key)
        _attach_log(result, log)
        return result
    finally:
        for connection in (reopened, store):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def _dispatch(case: str, stimuli: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """按案例前缀分派到原始设备链路、人工对账或合同层。"""
    if case.startswith("A06_"):
        return _run_apply_case(case, stimuli, root)
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
    if case == "reconnect":
        return {
            "case": case,
            "layer": "blocked_requires_recovery_identity",
            "blocked": True,
            "reason": "M5 无键屏/独立会话身份恢复入口",
        }
    if case in {"confirm_performed", "confirm_not_performed", "abandon"}:
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
        _AUTHORIZATION_NONCES.clear()
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
