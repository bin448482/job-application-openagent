"""定义任务、读取、证据与租约领域模型，并按值类型分派 M2/M3 严格校验。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

APP = "boss"
APPS = frozenset({APP})

TERMINAL_TASK_STATES = frozenset({"done", "not_sent", "failed", "cancelled"})
LEASED_TASK_STATES = frozenset({"leased", "running"})

DISCOVERY_KINDS = frozenset({"unread", "new_message"})
SIGNAL_KEYS = ("row_badge", "new_greeting", "tab_badge", "preview_delta")
SIGNAL_STATES = frozenset({"present", "absent", "unknown", "not_applicable"})
SIGNAL_UNKNOWN_REASONS = frozenset(
    {
        "missing_signal",
        "unverified_semantics",
        "no_baseline",
        "ambiguous_signal",
        "uncalibrated_region",
        "incomplete_coverage",
    }
)

READ_STATUSES = frozenset({"ok", "failed", "undetermined"})
READ_SOURCES = frozenset({"hierarchy", "pixel", "vision", "human", "fixture"})
CONFIDENCES = frozenset({"low", "medium", "high"})
READ_FAILED_REASONS = frozenset(
    {
        "device_disconnected",
        "device_unauthorized",
        "device_offline",
        "wrong_page",
        "invalid_xml",
        "invalid_fixture",
    }
)
READ_UNDETERMINED_REASONS = frozenset(
    {
        "unread_unverified",
        "identity_ambiguous",
        "incomplete_snapshot",
        "stale_snapshot",
        "uncalibrated_profile",
    }
)

EVIDENCE_OUTCOMES = frozenset({"performed", "not_performed", "unknown"})
EVIDENCE_EFFECTS = frozenset({"sent", "request_sent", "deleted", "applied", "none"})
EVIDENCE_NOT_PERFORMED_REASONS = frozenset(
    {"authorization_missing", "precondition_failed", "cancelled_before_action"}
)
EVIDENCE_UNKNOWN_REASONS = frozenset(
    {"device_disconnected", "result_unverified", "executor_interrupted"}
)

TASK_REASON_CODES = (
    READ_FAILED_REASONS
    | READ_UNDETERMINED_REASONS
    | EVIDENCE_NOT_PERFORMED_REASONS
    | EVIDENCE_UNKNOWN_REASONS
    | {"candidate_superseded", "recovery_requires_reconciliation", "invalid_decision", "human_review_required", "stale_decision", "policy_changed", "input_too_large", "decision_timeout"}
)

LEASE_SECONDS = 60
CLOCK_TOLERANCE_SECONDS = 1.0

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class UnreadSignal:
    """表示未读四源中单个来源的结论、计数与未知原因。"""

    state: str
    count: int | None
    reason_code: str | None


@dataclass(frozen=True)
class UnreadRow:
    """表示列表中的一行合成身份摘要与未读计数，不保存正文。"""

    conversation_key: str | None
    identity_hash: str | None
    preview_hash: str | None
    time_hint: str | None
    unread_count: int | None


@dataclass(frozen=True)
class Candidate:
    """表示一个可定位的未读或新消息候选及其来源指纹。"""

    candidate_key: str
    conversation_key: str
    kind: str
    source_names: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class UnreadSnapshot:
    """表示一次结构完整读取得到的未读快照，不包含聊天正文。"""

    app: str
    profile_id: str
    coverage: str
    signals: dict[str, UnreadSignal]
    rows: tuple[UnreadRow, ...]
    candidates: tuple[Candidate, ...]
    unread_state: str


@dataclass(frozen=True)
class ReadResult:
    """表示读取三态结果；失败与未决时 value 必须为空且原因非空。"""

    status: str
    value: object | None
    reason_code: str | None
    snapshot_id: str | None
    observed_at: str
    source: str
    confidence: str


@dataclass(frozen=True)
class Evidence:
    """表示一次动作的三态证据；M1 只用合成对象验证仓储入口。"""

    action_key: str
    outcome: str
    effect: str
    reason_code: str | None
    before_snapshot_id: str | None
    after_snapshot_id: str | None
    observed_at: str


@dataclass(frozen=True)
class Owner:
    """表示本次进程的租约持有者身份；host 只在本机库内使用。"""

    id: str
    pid: int
    host: str


@dataclass(frozen=True)
class Lease:
    """表示一次任务租约；后续调用必须回传同一 nonce 与库中截止时间。"""

    app: str
    key: str
    owner: Owner
    nonce: str
    expires_at: str


def canonical_hash(value: object) -> str:
    """按 §5.1 的键排序、UTF-8 与紧凑分隔符计算 SHA-256 小写摘要。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identity_hash_for(contact: str, company: str, position: str) -> str:
    """按联系人、公司、职位顺序计算会话身份摘要。"""
    return canonical_hash([contact, company, position])


def conversation_key_for(app: str, contact: str, company: str, position: str) -> str:
    """生成带应用前缀的会话键；只对身份字段齐全的合成行使用。"""
    return conversation_key_from_identity(
        app, identity_hash_for(contact, company, position)
    )


def conversation_key_from_identity(app: str, identity_hash: str) -> str:
    """由已验证身份摘要重建会话键，供分页合并后唯一身份收敛使用。"""
    if not isinstance(app, str) or app not in APPS:
        raise ValueError("app is invalid")
    if not is_hash(identity_hash):
        raise ValueError("identity hash is invalid")
    return f"{app}:conv:{identity_hash}"


def is_conversation_key(app: object, key: object) -> bool:
    """判断是否为指定应用的规范会话键，拒绝跨应用或非摘要后缀。"""
    if not isinstance(app, str) or app not in APPS or not isinstance(key, str):
        return False
    prefix = f"{app}:conv:"
    return key.startswith(prefix) and is_hash(key[len(prefix):])


def fingerprint_for(
    preview_hash: str | None, time_hint: str | None, unread_count: int | None
) -> str:
    """按预览摘要、时间提示与未读计数计算候选指纹。"""
    if unread_count is not None and not is_int(unread_count):
        raise ValueError("unread count is invalid")
    return canonical_hash([preview_hash, time_hint, unread_count])


def discovery_key_for(app: str, conversation_key: str, fingerprint: str) -> str:
    """生成带应用前缀的发现键；同一会话多来源仍由会话唯一索引收敛。"""
    if not isinstance(app, str) or app not in APPS:
        raise ValueError("app is invalid")
    if not is_nonempty_text(conversation_key) or not is_nonempty_text(fingerprint):
        raise ValueError("discovery key inputs are invalid")
    return f"{app}:discovery:{canonical_hash([conversation_key, fingerprint])}"


def canonical_source_names(values: object) -> tuple[str, ...]:
    """把来源名去重并按四源定义顺序规范化为元组。"""
    if not isinstance(values, (tuple, list)):
        raise ValueError("source names are invalid")
    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or value not in SIGNAL_KEYS:
            raise ValueError("source name is invalid")
        if value not in names:
            names.append(value)
    if not names:
        raise ValueError("source names are empty")
    return tuple(name for name in SIGNAL_KEYS if name in names)


def format_utc(value: datetime) -> str:
    """把带时区时间格式化为毫秒精度 UTC 字符串。"""
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.") + f"{normalized.microsecond // 1000:03d}Z"


def parse_utc(value: object) -> datetime:
    """严格解析毫秒 UTC 字符串，拒绝非字符串、错误精度与不可往返格式。"""
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError("time is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise ValueError("time is invalid") from None
    if format_utc(parsed) != value:
        raise ValueError("time is invalid")
    return parsed


def add_seconds(value: str, seconds: int) -> str:
    """在规范 UTC 时间上增加整秒并以同一格式返回。"""
    return format_utc(parse_utc(value) + timedelta(seconds=seconds))


def is_int(value: object) -> bool:
    """判断是否为整数且排除 bool 冒充。"""
    return type(value) is int


def is_nonempty_text(value: object) -> bool:
    """判断是否为非空且可 UTF-8 编码的字符串。"""
    if not isinstance(value, str) or not value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def is_hash(value: object) -> bool:
    """判断是否为 SHA-256 小写十六进制摘要。"""
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def is_uuid(value: object) -> bool:
    """判断是否为带连字符的规范 UUID 字符串。"""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return str(parsed) == value


def is_optional_text(value: object) -> bool:
    """判断是否为 null 或非空可编码字符串。"""
    return value is None or is_nonempty_text(value)


def is_discovery_key(app: object, key: object) -> bool:
    """判断是否为指定应用的规范发现键，拒绝跨应用或非摘要后缀。"""
    if not isinstance(app, str) or app not in APPS or not isinstance(key, str):
        return False
    prefix = f"{app}:discovery:"
    return key.startswith(prefix) and is_hash(key[len(prefix):])


def is_job_task_key(key: object) -> bool:
    """判断是否为规范职位任务键；不导入 reading 以保持领域模型无环依赖。"""
    if not isinstance(key, str):
        return False
    prefix = f"{APP}:job:"
    return key.startswith(prefix) and is_hash(key[len(prefix):])


def is_task_key(key: object) -> bool:
    """判断是否为发现的会话任务键或职位任务键。"""
    return is_discovery_key(APP, key) or is_job_task_key(key)


def validate_owner(owner: object) -> None:
    """校验租约持有者的 UUID、正整数 PID 与本机摘要 host。"""
    if not isinstance(owner, Owner):
        raise ValueError("owner is invalid")
    if not is_uuid(owner.id):
        raise ValueError("owner id is invalid")
    if (
        not is_int(owner.pid)
        or owner.pid <= 0
        or owner.pid > 9223372036854775807
    ):
        raise ValueError("owner pid is invalid")
    if not is_hash(owner.host):
        raise ValueError("owner host is invalid")


def validate_lease(lease: object) -> None:
    """校验租约的应用、发现键、持有者、nonce 与截止时间形状。"""
    if not isinstance(lease, Lease):
        raise ValueError("lease is invalid")
    if not isinstance(lease.app, str) or lease.app not in APPS:
        raise ValueError("lease app is invalid")
    if not is_discovery_key(lease.app, lease.key):
        raise ValueError("lease key is invalid")
    validate_owner(lease.owner)
    if not is_uuid(lease.nonce):
        raise ValueError("lease nonce is invalid")
    parse_utc(lease.expires_at)


def validate_task_lease(lease: object) -> None:
    """校验发现或职位任务的租约；职位不得伪装为发现键。"""
    if not isinstance(lease, Lease):
        raise ValueError("lease is invalid")
    if not isinstance(lease.app, str) or lease.app not in APPS:
        raise ValueError("lease app is invalid")
    if not is_task_key(lease.key):
        raise ValueError("lease key is invalid")
    validate_owner(lease.owner)
    if not is_uuid(lease.nonce):
        raise ValueError("lease nonce is invalid")
    parse_utc(lease.expires_at)


def validate_unread_snapshot(snapshot: object) -> None:
    """校验未读快照的固定键、四源结论、行与候选，不做 IO。"""
    if not isinstance(snapshot, UnreadSnapshot):
        raise ValueError("snapshot is invalid")
    if not isinstance(snapshot.app, str) or snapshot.app not in APPS:
        raise ValueError("snapshot app is invalid")
    if not is_nonempty_text(snapshot.profile_id):
        raise ValueError("profile is invalid")
    if not isinstance(snapshot.coverage, str) or snapshot.coverage not in {
        "viewport",
        "all",
    }:
        raise ValueError("coverage is invalid")
    if not isinstance(snapshot.unread_state, str) or snapshot.unread_state not in {
        "present",
        "absent",
        "unknown",
    }:
        raise ValueError("unread state is invalid")
    if not isinstance(snapshot.signals, dict) or set(snapshot.signals) != set(SIGNAL_KEYS):
        raise ValueError("signals are invalid")
    for name in SIGNAL_KEYS:
        _validate_signal(snapshot.signals[name])
    if not isinstance(snapshot.rows, (tuple, list)):
        raise ValueError("rows are invalid")
    for row in snapshot.rows:
        _validate_row(row)
        if row.conversation_key is not None:
            if (
                row.identity_hash is None
                or row.conversation_key != f"{snapshot.app}:conv:{row.identity_hash}"
            ):
                raise ValueError("row conversation key is invalid")
    if not isinstance(snapshot.candidates, (tuple, list)):
        raise ValueError("candidates are invalid")
    seen: set[str] = set()
    for candidate in snapshot.candidates:
        _validate_candidate(candidate)
        if candidate.candidate_key in seen:
            raise ValueError("candidate key is duplicated")
        seen.add(candidate.candidate_key)


def _validate_signal(signal: object) -> None:
    """校验单个未读源的状态、计数与原因组合。"""
    if not isinstance(signal, UnreadSignal):
        raise ValueError("signal is invalid")
    if not isinstance(signal.state, str) or signal.state not in SIGNAL_STATES:
        raise ValueError("signal state is invalid")
    if signal.count is not None and (not is_int(signal.count) or signal.count < 0):
        raise ValueError("signal count is invalid")
    if signal.state in {"present", "absent"}:
        if signal.reason_code is not None:
            raise ValueError("signal reason is invalid")
        if signal.state == "present" and signal.count is not None and signal.count <= 0:
            raise ValueError("signal count is invalid")
        if signal.state == "absent" and signal.count != 0:
            raise ValueError("signal count is invalid")
        return
    if signal.count is not None:
        raise ValueError("signal count is invalid")
    if signal.state == "unknown":
        if not isinstance(signal.reason_code, str) or signal.reason_code not in SIGNAL_UNKNOWN_REASONS:
            raise ValueError("signal reason is invalid")
        return
    if signal.reason_code != "profile_not_applicable":
        raise ValueError("signal reason is invalid")


def _validate_row(row: object) -> None:
    """校验快照行的可空身份、摘要、时间与计数类型。"""
    if not isinstance(row, UnreadRow):
        raise ValueError("row is invalid")
    if not is_optional_text(row.conversation_key):
        raise ValueError("row conversation key is invalid")
    if row.identity_hash is not None and not is_hash(row.identity_hash):
        raise ValueError("row identity hash is invalid")
    if row.preview_hash is not None and not is_hash(row.preview_hash):
        raise ValueError("row preview hash is invalid")
    if row.time_hint is not None and not isinstance(row.time_hint, str):
        raise ValueError("row time is invalid")
    if row.unread_count is not None and (
        not is_int(row.unread_count) or row.unread_count < 0
    ):
        raise ValueError("row unread count is invalid")


def _validate_candidate(candidate: object) -> None:
    """校验候选键、会话键、类型、来源去重与指纹形状。"""
    if not isinstance(candidate, Candidate):
        raise ValueError("candidate is invalid")
    if not is_nonempty_text(candidate.candidate_key):
        raise ValueError("candidate key is invalid")
    if not is_nonempty_text(candidate.conversation_key):
        raise ValueError("candidate conversation key is invalid")
    if not isinstance(candidate.kind, str) or candidate.kind not in DISCOVERY_KINDS:
        raise ValueError("candidate kind is invalid")
    canonical_source_names(candidate.source_names)
    if not is_nonempty_text(candidate.fingerprint):
        raise ValueError("candidate fingerprint is invalid")


def validate_read_result(result: object) -> None:
    """校验读取结果的三态、来源、置信度、快照 ID 与 UTC 时间及原因组合。"""
    if not isinstance(result, ReadResult):
        raise ValueError("read result is invalid")
    if not isinstance(result.status, str) or result.status not in READ_STATUSES:
        raise ValueError("read status is invalid")
    if (
        not isinstance(result.source, str)
        or result.source not in READ_SOURCES
        or not isinstance(result.confidence, str)
        or result.confidence not in CONFIDENCES
    ):
        raise ValueError("read metadata is invalid")
    parse_utc(result.observed_at)
    if result.snapshot_id is not None and not is_uuid(result.snapshot_id):
        raise ValueError("snapshot id is invalid")
    if result.status == "ok":
        if result.value is None or result.reason_code is not None:
            raise ValueError("ok result is invalid")
        from app.domain.reading import validate_read_value

        validate_read_value(result.value)
        return
    if result.value is not None:
        raise ValueError("failed result must not carry a value")
    if result.status == "failed":
        if (
            not isinstance(result.reason_code, str)
            or result.reason_code not in READ_FAILED_REASONS
        ):
            raise ValueError("failed reason is invalid")
        return
    if (
        not isinstance(result.reason_code, str)
        or result.reason_code not in READ_UNDETERMINED_REASONS
    ):
        raise ValueError("undetermined reason is invalid")


def validate_evidence(evidence: object) -> None:
    """校验动作证据的三态、效果、快照与原因组合。"""
    if not isinstance(evidence, Evidence):
        raise ValueError("evidence is invalid")
    if not is_nonempty_text(evidence.action_key):
        raise ValueError("action key is invalid")
    if not isinstance(evidence.outcome, str) or evidence.outcome not in EVIDENCE_OUTCOMES:
        raise ValueError("evidence outcome is invalid")
    if not isinstance(evidence.effect, str) or evidence.effect not in EVIDENCE_EFFECTS:
        raise ValueError("evidence effect is invalid")
    parse_utc(evidence.observed_at)
    for snapshot_id in (evidence.before_snapshot_id, evidence.after_snapshot_id):
        if snapshot_id is not None and not is_uuid(snapshot_id):
            raise ValueError("evidence snapshot is invalid")
    if evidence.outcome == "performed":
        if (
            evidence.effect == "none"
            or evidence.reason_code is not None
            or evidence.before_snapshot_id is None
            or evidence.after_snapshot_id is None
            or evidence.before_snapshot_id == evidence.after_snapshot_id
        ):
            raise ValueError("performed evidence is invalid")
        return
    if evidence.effect != "none":
        raise ValueError("non-performed effect is invalid")
    if evidence.outcome == "not_performed":
        if (
            not isinstance(evidence.reason_code, str)
            or evidence.reason_code not in EVIDENCE_NOT_PERFORMED_REASONS
        ):
            raise ValueError("not performed reason is invalid")
        return
    if (
        not isinstance(evidence.reason_code, str)
        or evidence.reason_code not in EVIDENCE_UNKNOWN_REASONS
    ):
        raise ValueError("unknown reason is invalid")


def validate_event_reason(reason_code: object) -> None:
    """校验事件原因只取任务、证据或仓储固定词表或 null。"""
    if reason_code is not None and (
        not isinstance(reason_code, str) or reason_code not in TASK_REASON_CODES
    ):
        raise ValueError("event reason is invalid")
