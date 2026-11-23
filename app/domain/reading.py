"""定义 M3 纯读取值类型、严格校验、稳定消息/职位键与跨快照新增消息比较。"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import (
    APP,
    UnreadSnapshot,
    canonical_hash,
    conversation_key_from_identity,
    is_hash,
    is_nonempty_text,
    validate_unread_snapshot,
)

PAGE_KINDS = frozenset(
    {
        "main",
        "chat_list",
        "chat_detail",
        "job_detail",
        "popup",
        "login",
        "recommend_feed",
        "unknown",
    }
)
PAGE_SIGNALS = (
    "popup",
    "login",
    "chat_detail",
    "job_detail",
    "recommend_feed",
    "chat_list",
    "bottom_tabs",
)
FIELD_NAMES = frozenset(
    {
        "page",
        "job_cards",
        "job_detail",
        "unread_badges",
        "conversation_identity",
        "conversation_inbound",
    }
)
FIELD_SOURCES = frozenset({"hierarchy", "pixel", "human"})
FIELD_STATES = frozenset({"ok", "unknown", "failed"})
FIELD_UNKNOWN_REASONS = frozenset(
    {
        "missing_signal",
        "ambiguous_signal",
        "uncalibrated_region",
        "calibration_mismatch",
        "unverified_identity",
        "incomplete_coverage",
        "stable_identity_unverified",
        "unverified_semantics",
    }
)
FIELD_FAILED_REASONS = frozenset({"invalid_xml", "wrong_page", "invalid_fixture"})
FIELD_REASON_PRIORITY = (
    "ambiguous_signal",
    "calibration_mismatch",
    "uncalibrated_region",
    "unverified_identity",
    "stable_identity_unverified",
    "missing_signal",
    "incomplete_coverage",
    "unverified_semantics",
)
ROLES = frozenset({"hr", "unknown"})
DIRECTIONS = frozenset({"incoming", "outgoing", "system", "unknown"})
CHAT_KINDS = frozenset(
    {"text", "media", "resume_card", "card", "system_tip", "time", "unknown"}
)
INBOUND_STATES = frozenset({"known", "none", "non_text", "unknown"})
UNREAD_ROW_STATES = frozenset({"present", "absent", "unknown"})
UNREAD_ROW_SOURCES = frozenset({"hierarchy", "pixel", "human", "none"})
DIRECTION_EVIDENCE_NAMES = ("avatar", "bubble", "sent_status")
JOB_KEY_PREFIX = "boss:job:"
MESSAGE_KEY_PREFIX = "boss:msg:"
STABLE_ID_DOMAIN = ("boss", "message", "stable-id")


@dataclass(frozen=True)
class FieldEvidence:
    """表示一个读取字段的来源、置信度、结论与未知/失败原因。"""

    field: str
    source: str
    confidence: str
    state: str
    reason_code: str | None


@dataclass(frozen=True)
class PageSnapshot:
    """表示一次页面分类结果及命中的结构信号。"""

    app: str
    profile_id: str
    page: str
    signals: tuple[str, ...]
    field_evidence: tuple[FieldEvidence, ...]


@dataclass(frozen=True)
class JobCard:
    """表示一张可识别职位卡的原文、比较值与可选稳定职位键。"""

    job_key: str | None
    identity_hash: str | None
    title_raw: str
    title: str
    company_raw: str
    company: str
    salary_raw: str
    salary: str


@dataclass(frozen=True)
class JobList:
    """表示当前视口内的完整职位卡集合。"""

    app: str
    profile_id: str
    coverage: str
    cards: tuple[JobCard, ...]
    field_evidence: tuple[FieldEvidence, ...]


@dataclass(frozen=True)
class JobDetail:
    """表示职位详情页的正文、摘要、完整性及可选展示字段。"""

    app: str
    profile_id: str
    job_key: str
    identity_hash: str
    title_raw: str
    title: str
    company_raw: str
    company: str
    salary_raw: str
    salary: str
    recruiter_raw: str | None
    recruiter: str | None
    location_raw: str | None
    location: str | None
    description: str
    description_hash: str
    description_length: int
    complete: bool
    missing_fields: tuple[str, ...]
    field_evidence: tuple[FieldEvidence, ...]


@dataclass(frozen=True)
class UnreadRowRead:
    """表示一行保留身份摘要的未读角标读取结论。"""

    identity_hash: str | None
    conversation_key: str | None
    state: str
    count: int | None
    source: str
    confidence: str
    reason_code: str | None


@dataclass(frozen=True)
class UnreadRead:
    """表示当前视口的未读行读取结果，不替代 M2 四源快照入库。"""

    app: str
    profile_id: str
    coverage: str
    rows: tuple[UnreadRowRead, ...]
    unread_state: str
    field_evidence: tuple[FieldEvidence, ...]


@dataclass(frozen=True)
class ChatMessage:
    """表示一条聊天行的内容指纹、方向证据与可选应用稳定身份。"""

    kind: str
    direction: str
    text: str | None
    content_hash: str | None
    time_hint: str | None
    stable_identity_hash: str | None
    stable_identity_source: str | None
    repeat_safe: bool
    truncated: bool
    direction_evidence: tuple[str, ...]
    message_key: str | None


@dataclass(frozen=True)
class Conversation:
    """表示一次会话读取的身份、消息、方向门禁与可发送入站身份。"""

    app: str
    profile_id: str
    conversation_key: str
    identity_hash: str
    contact: str
    contact_verified: bool
    role: str
    message_identity_verified: bool
    read_complete: bool
    messages: tuple[ChatMessage, ...]
    inbound_state: str
    latest_inbound_key: str | None
    field_evidence: tuple[FieldEvidence, ...]


def normalize_job_title(value: str) -> str:
    """按 JOBS-P01 只去标题首尾的 @、%、& 与空格，不做内部改写。"""
    return value.strip(" @%&")


def normalize_job_company(value: str) -> str:
    """按 JOBS-P07 的比较口径只去公司名首尾空白。"""
    return value.strip()


def normalize_job_salary(value: str) -> str:
    """职位薪资只去首尾空白，不解析为数字或推断区间。"""
    return value.strip()


def job_identity_hash_for(title_raw: str, company_raw: str, salary_raw: str) -> str:
    """按规范化标题、公司、薪资计算职位身份摘要。"""
    return canonical_hash(
        [
            normalize_job_title(title_raw),
            normalize_job_company(company_raw),
            normalize_job_salary(salary_raw),
        ]
    )


def job_key_from_identity(identity_hash: str) -> str:
    """由已校验职位身份摘要生成带应用前缀的职位键。"""
    if not is_hash(identity_hash):
        raise ValueError("job identity hash is invalid")
    return f"{JOB_KEY_PREFIX}{identity_hash}"


def is_job_key(value: object) -> bool:
    """判断是否为规范 bjob 前缀加 SHA-256 摘要的职位键。"""
    return (
        isinstance(value, str)
        and value.startswith(JOB_KEY_PREFIX)
        and is_hash(value[len(JOB_KEY_PREFIX):])
    )


def message_stable_identity_hash(raw_stable_id: str) -> str:
    """对应用提供的消息标识原值按固定命名域计算稳定身份摘要。"""
    if not is_nonempty_text(raw_stable_id):
        raise ValueError("stable message id is invalid")
    return canonical_hash([*STABLE_ID_DOMAIN, raw_stable_id])


def message_key_from_stable(conversation_key: str, stable_identity_hash: str) -> str:
    """由已验证会话键与应用稳定消息身份生成可发送消息键。"""
    if not is_nonempty_text(conversation_key) or not is_hash(stable_identity_hash):
        raise ValueError("message key inputs are invalid")
    return f"{MESSAGE_KEY_PREFIX}{canonical_hash([conversation_key, stable_identity_hash])}"


def is_message_key(value: object) -> bool:
    """判断是否为规范 bmsg 前缀加 SHA-256 摘要的消息键。"""
    return (
        isinstance(value, str)
        and value.startswith(MESSAGE_KEY_PREFIX)
        and is_hash(value[len(MESSAGE_KEY_PREFIX):])
    )


def validate_read_value(value: object) -> None:
    """按具体值类型分派 M2/M3 严格校验，未知类型一律拒绝。"""
    if isinstance(value, UnreadSnapshot):
        validate_unread_snapshot(value)
        return
    if isinstance(value, PageSnapshot):
        validate_page_snapshot(value)
        return
    if isinstance(value, JobList):
        validate_job_list(value)
        return
    if isinstance(value, JobDetail):
        validate_job_detail(value)
        return
    if isinstance(value, UnreadRead):
        validate_unread_read(value)
        return
    if isinstance(value, Conversation):
        validate_conversation(value)
        return
    raise ValueError("read value type is invalid")


def validate_field_evidence(evidence: object) -> None:
    """校验字段名、来源组合、状态与原因词表。"""
    if not isinstance(evidence, FieldEvidence):
        raise ValueError("field evidence is invalid")
    if not _is_enum(evidence.field, FIELD_NAMES):
        raise ValueError("field name is invalid")
    if not _is_enum(evidence.source, FIELD_SOURCES):
        raise ValueError("field source is invalid")
    if evidence.field != "unread_badges" and evidence.source == "pixel":
        raise ValueError("pixel field source is invalid")
    if not _is_enum(evidence.confidence, {"low", "medium", "high"}):
        raise ValueError("field confidence is invalid")
    if not _is_enum(evidence.state, FIELD_STATES):
        raise ValueError("field state is invalid")
    if evidence.state == "ok":
        if evidence.reason_code is not None:
            raise ValueError("ok field reason is invalid")
        return
    if not isinstance(evidence.reason_code, str):
        raise ValueError("field reason is invalid")
    if evidence.state == "unknown":
        if evidence.reason_code not in FIELD_UNKNOWN_REASONS:
            raise ValueError("unknown field reason is invalid")
        return
    if evidence.reason_code not in FIELD_FAILED_REASONS:
        raise ValueError("failed field reason is invalid")


def validate_page_snapshot(snapshot: object) -> None:
    """校验页面分类、信号顺序与唯一的页面字段证据。"""
    if not isinstance(snapshot, PageSnapshot):
        raise ValueError("page snapshot is invalid")
    if snapshot.app != APP or not is_nonempty_text(snapshot.profile_id):
        raise ValueError("page snapshot identity is invalid")
    if not _is_enum(snapshot.page, PAGE_KINDS):
        raise ValueError("page kind is invalid")
    signals = _validate_signal_names(snapshot.signals)
    expected = tuple(name for name in PAGE_SIGNALS if name in set(signals))
    if signals != expected:
        raise ValueError("page signals are not canonical")
    evidence = _require_evidence(snapshot.field_evidence, ("page",))
    item = evidence[0]
    if item.source != "hierarchy":
        raise ValueError("page source is invalid")
    if item.confidence != ("high" if snapshot.page != "unknown" else "low"):
        raise ValueError("page confidence is invalid")
    if snapshot.page == "unknown":
        if item.state != "unknown" or item.reason_code is None:
            raise ValueError("unknown page evidence is invalid")
    elif item.state != "ok" or item.reason_code is not None:
        raise ValueError("page evidence is invalid")


def validate_job_card(card: object) -> None:
    """校验职位卡原文、规范化比较值与重算职位键。"""
    if not isinstance(card, JobCard):
        raise ValueError("job card is invalid")
    for name in ("title_raw", "company_raw", "salary_raw"):
        if not is_nonempty_text(getattr(card, name)):
            raise ValueError("job card raw field is invalid")
    if card.title != normalize_job_title(card.title_raw):
        raise ValueError("job title is not normalized")
    if card.company != normalize_job_company(card.company_raw):
        raise ValueError("job company is not normalized")
    if card.salary != normalize_job_salary(card.salary_raw):
        raise ValueError("job salary is not normalized")
    if not is_nonempty_text(card.title):
        raise ValueError("job title is empty")
    if not is_hash(card.identity_hash):
        raise ValueError("job identity hash is invalid")
    if card.identity_hash != job_identity_hash_for(
        card.title_raw, card.company_raw, card.salary_raw
    ):
        raise ValueError("job identity hash is stale")
    if card.job_key is not None:
        if not is_job_key(card.job_key):
            raise ValueError("job key is invalid")
        if card.job_key != job_key_from_identity(card.identity_hash):
            raise ValueError("job key is stale")


def validate_job_list(value: object) -> None:
    """校验职位列表的视口语义、卡集合与重复身份收敛。"""
    if not isinstance(value, JobList):
        raise ValueError("job list is invalid")
    if value.app != APP or not is_nonempty_text(value.profile_id):
        raise ValueError("job list identity is invalid")
    if value.coverage != "viewport":
        raise ValueError("job list coverage is invalid")
    if not isinstance(value.cards, (tuple, list)) or not value.cards:
        raise ValueError("job cards are empty")
    cards = tuple(value.cards)
    for card in cards:
        validate_job_card(card)
    counts: dict[str, int] = {}
    for card in cards:
        assert card.identity_hash is not None
        counts[card.identity_hash] = counts.get(card.identity_hash, 0) + 1
    for card in cards:
        assert card.identity_hash is not None
        if counts[card.identity_hash] > 1:
            if card.job_key is not None:
                raise ValueError("duplicate job key is not allowed")
        elif card.job_key != job_key_from_identity(card.identity_hash):
            raise ValueError("unique job key is required")
    evidence = _require_evidence(value.field_evidence, ("job_cards",))
    _require_ok_evidence(evidence[0])


def validate_job_detail(value: object) -> None:
    """校验职位详情正文、摘要、长度、完整性、缺失字段与职位键。"""
    if not isinstance(value, JobDetail):
        raise ValueError("job detail is invalid")
    if value.app != APP or not is_nonempty_text(value.profile_id):
        raise ValueError("job detail identity is invalid")
    for name in ("title_raw", "company_raw", "salary_raw"):
        if not is_nonempty_text(getattr(value, name)):
            raise ValueError("job detail raw field is invalid")
    if value.title != normalize_job_title(value.title_raw):
        raise ValueError("job detail title is not normalized")
    if value.company != normalize_job_company(value.company_raw):
        raise ValueError("job detail company is not normalized")
    if value.salary != normalize_job_salary(value.salary_raw):
        raise ValueError("job detail salary is not normalized")
    if not is_hash(value.identity_hash):
        raise ValueError("job detail identity hash is invalid")
    if value.identity_hash != job_identity_hash_for(
        value.title_raw, value.company_raw, value.salary_raw
    ):
        raise ValueError("job detail identity hash is stale")
    if value.job_key != job_key_from_identity(value.identity_hash):
        raise ValueError("job detail key is stale")
    if not is_nonempty_text(value.description):
        raise ValueError("job description is invalid")
    if value.description_hash != canonical_hash(value.description):
        raise ValueError("job description hash is stale")
    if not _is_nonnegative_int(value.description_length):
        raise ValueError("job description length is invalid")
    if value.description_length != len(value.description):
        raise ValueError("job description length is stale")
    if value.complete is not True:
        raise ValueError("job detail must be complete")
    for raw_name, name in (("recruiter_raw", "recruiter"), ("location_raw", "location")):
        raw = getattr(value, raw_name)
        normalized = getattr(value, name)
        if raw is None:
            if normalized is not None:
                raise ValueError("job detail optional field is invalid")
        else:
            if not is_nonempty_text(raw) or normalized != raw.strip():
                raise ValueError("job detail optional field is not normalized")
    missing = _validate_missing_fields(value.missing_fields)
    for name, raw_name in (("recruiter", "recruiter_raw"), ("location", "location_raw")):
        is_missing = getattr(value, raw_name) is None
        if is_missing != (name in missing):
            raise ValueError("job detail missing fields are stale")
    evidence = _require_evidence(value.field_evidence, ("job_detail",))
    _require_ok_evidence(evidence[0])


def validate_unread_row(row: object) -> None:
    """校验未读行身份、三态、计数、来源与原因组合。"""
    if not isinstance(row, UnreadRowRead):
        raise ValueError("unread row is invalid")
    if row.identity_hash is not None and not is_hash(row.identity_hash):
        raise ValueError("unread row identity is invalid")
    if row.conversation_key is not None:
        if row.identity_hash is None or not is_nonempty_text(row.conversation_key):
            raise ValueError("unread row conversation key is invalid")
        if row.conversation_key != conversation_key_from_identity(APP, row.identity_hash):
            raise ValueError("unread row conversation key is stale")
    if not _is_enum(row.state, UNREAD_ROW_STATES):
        raise ValueError("unread row state is invalid")
    if not _is_enum(row.source, UNREAD_ROW_SOURCES):
        raise ValueError("unread row source is invalid")
    if not _is_enum(row.confidence, {"low", "medium", "high"}):
        raise ValueError("unread row confidence is invalid")
    if row.count is not None and (not _is_nonnegative_int(row.count)):
        raise ValueError("unread row count is invalid")
    if row.state == "present":
        if row.source == "none" or row.reason_code is not None:
            raise ValueError("present unread row is invalid")
        if row.count is not None and row.count <= 0:
            raise ValueError("present unread row count is invalid")
        if row.source == "pixel" and row.count is not None:
            raise ValueError("pixel present count must be unknown")
        return
    if row.state == "absent":
        if row.count != 0 or row.reason_code is not None or row.source == "none":
            raise ValueError("absent unread row is invalid")
        return
    if (
        row.count is not None
        or not isinstance(row.reason_code, str)
        or row.reason_code not in FIELD_UNKNOWN_REASONS
    ):
        raise ValueError("unknown unread row is invalid")


def validate_unread_read(value: object) -> None:
    """校验未读读取的覆盖、行身份收敛、聚合状态与字段证据。"""
    if not isinstance(value, UnreadRead):
        raise ValueError("unread read is invalid")
    if value.app != APP or not is_nonempty_text(value.profile_id):
        raise ValueError("unread read identity is invalid")
    if not _is_enum(value.coverage, {"viewport", "all"}):
        raise ValueError("unread coverage is invalid")
    if not isinstance(value.rows, (tuple, list)) or not value.rows:
        raise ValueError("unread rows are empty")
    rows = tuple(value.rows)
    for row in rows:
        validate_unread_row(row)
    counts: dict[str, int] = {}
    for row in rows:
        if row.identity_hash is not None:
            counts[row.identity_hash] = counts.get(row.identity_hash, 0) + 1
    for row in rows:
        if row.identity_hash is None:
            if row.conversation_key is not None:
                raise ValueError("unknown identity row has a conversation key")
        elif counts[row.identity_hash] > 1 and row.conversation_key is not None:
            raise ValueError("duplicate unread identity is locatable")
    expected_state = "unknown"
    if any(row.state == "present" for row in rows):
        expected_state = "present"
    elif value.coverage == "all" and all(row.state == "absent" for row in rows):
        expected_state = "absent"
    if value.unread_state != expected_state:
        raise ValueError("unread state is stale")
    evidence = _require_evidence(value.field_evidence, ("unread_badges",))
    item = evidence[0]
    reasons = [
        row.reason_code
        for row in rows
        if row.state == "unknown" and row.reason_code is not None
    ]
    if reasons:
        if item.state != "unknown" or item.reason_code != _reason(reasons):
            raise ValueError("unread field evidence is stale")
    else:
        _require_ok_evidence(item)


def validate_chat_message(message: object) -> None:
    """校验聊天消息的类型、方向、内容指纹与稳定身份组合。"""
    if not isinstance(message, ChatMessage):
        raise ValueError("chat message is invalid")
    if not _is_enum(message.kind, CHAT_KINDS) or not _is_enum(
        message.direction, DIRECTIONS
    ):
        raise ValueError("chat message enum is invalid")
    if message.text is not None and not isinstance(message.text, str):
        raise ValueError("chat message text is invalid")
    if message.kind in {"text", "system_tip"}:
        if not is_nonempty_text(message.text) or not isinstance(message.text, str):
            raise ValueError("chat text is invalid")
        if message.content_hash != canonical_hash(message.text):
            raise ValueError("chat content hash is stale")
    elif message.content_hash is not None:
        raise ValueError("chat content hash is only for text")
    if message.time_hint is not None and not isinstance(message.time_hint, str):
        raise ValueError("chat time is invalid")
    if message.stable_identity_hash is not None:
        if not is_hash(message.stable_identity_hash):
            raise ValueError("chat stable identity is invalid")
        if message.stable_identity_source != "app_message_id":
            raise ValueError("chat stable identity source is invalid")
    elif message.stable_identity_source is not None:
        raise ValueError("chat stable identity source is orphaned")
    if type(message.repeat_safe) is not bool or message.repeat_safe != (
        message.stable_identity_hash is not None
    ):
        raise ValueError("chat repeat safe is stale")
    if type(message.truncated) is not bool:
        raise ValueError("chat truncated is invalid")
    if not isinstance(message.direction_evidence, (tuple, list)):
        raise ValueError("chat direction evidence is invalid")
    names = tuple(message.direction_evidence)
    if any(not isinstance(name, str) for name in names):
        raise ValueError("chat direction evidence name is invalid")
    if any(name not in DIRECTION_EVIDENCE_NAMES for name in names):
        raise ValueError("chat direction evidence name is invalid")
    if len(set(names)) != len(names):
        raise ValueError("chat direction evidence is duplicated")
    if names != tuple(name for name in DIRECTION_EVIDENCE_NAMES if name in set(names)):
        raise ValueError("chat direction evidence is not canonical")
    if message.direction == "system" and names:
        raise ValueError("system message has direction evidence")
    if message.direction in {"incoming", "outgoing"} and not names:
        raise ValueError("directed message lacks evidence")
    if message.message_key is not None:
        if not is_message_key(message.message_key) or not message.repeat_safe:
            raise ValueError("chat message key is invalid")


def validate_conversation(value: object) -> None:
    """校验会话身份、读全、消息身份收敛、入站三态与字段证据。"""
    if not isinstance(value, Conversation):
        raise ValueError("conversation is invalid")
    if value.app != APP or not is_nonempty_text(value.profile_id):
        raise ValueError("conversation identity is invalid")
    if not is_nonempty_text(value.conversation_key) or not is_hash(value.identity_hash):
        raise ValueError("conversation key is invalid")
    if value.conversation_key != conversation_key_from_identity(
        value.app, value.identity_hash
    ):
        raise ValueError("conversation key is stale")
    if not is_nonempty_text(value.contact) or value.contact_verified is not True:
        raise ValueError("conversation contact is unverified")
    if not _is_enum(value.role, ROLES):
        raise ValueError("conversation role is invalid")
    if type(value.message_identity_verified) is not bool:
        raise ValueError("conversation identity flag is invalid")
    if type(value.read_complete) is not bool:
        raise ValueError("conversation read complete is invalid")
    if not isinstance(value.messages, (tuple, list)):
        raise ValueError("conversation messages are invalid")
    messages = tuple(value.messages)
    for message in messages:
        validate_chat_message(message)
    stable_seen: set[str] = set()
    for message in messages:
        if message.stable_identity_hash is not None:
            if message.stable_identity_hash in stable_seen:
                raise ValueError("conversation stable identity is duplicated")
            stable_seen.add(message.stable_identity_hash)
        if message.message_key is not None:
            assert message.stable_identity_hash is not None
            expected_key = message_key_from_stable(
                value.conversation_key, message.stable_identity_hash
            )
            if message.message_key != expected_key:
                raise ValueError("conversation message key is stale")
    state, key, _reason_code = conversation_inbound_state(
        messages,
        value.read_complete,
        value.contact_verified,
        value.message_identity_verified,
    )
    if value.inbound_state != state:
        raise ValueError("conversation inbound state is stale")
    if value.latest_inbound_key != key:
        raise ValueError("conversation inbound key is stale")
    evidence = _require_evidence(
        value.field_evidence, ("conversation_identity", "conversation_inbound")
    )
    identity_evidence = evidence[0]
    if identity_evidence.state != "ok" or identity_evidence.source != "hierarchy":
        raise ValueError("conversation identity evidence is invalid")
    inbound_evidence = evidence[1]
    if inbound_evidence.source != "hierarchy":
        raise ValueError("conversation inbound evidence source is invalid")
    if state == "unknown":
        if (
            inbound_evidence.state != "unknown"
            or inbound_evidence.reason_code != _reason_code
        ):
            raise ValueError("conversation inbound evidence is stale")
    else:
        _require_ok_evidence(inbound_evidence)


def conversation_inbound_state(
    messages: tuple[ChatMessage, ...],
    read_complete: bool,
    contact_verified: bool,
    message_identity_verified: bool,
) -> tuple[str, str | None, str | None]:
    """按读全、身份、方向与稳定消息键计算入站三态、键与未知原因。"""
    if not read_complete:
        return "unknown", None, "incomplete_coverage"
    if not contact_verified:
        return "unknown", None, "unverified_identity"
    if not message_identity_verified:
        return "unknown", None, "stable_identity_unverified"
    if any(
        message.kind == "unknown" or message.direction == "unknown"
        for message in messages
    ):
        return "unknown", None, "ambiguous_signal"
    non_system = [
        message
        for message in messages
        if message.kind not in {"system_tip", "time"}
    ]
    if not non_system:
        return "none", None, None
    latest = non_system[-1]
    if latest.direction == "outgoing":
        return "none", None, None
    if latest.direction != "incoming":
        return "unknown", None, "ambiguous_signal"
    if latest.kind == "text":
        if latest.text is None or latest.truncated:
            return "unknown", None, "ambiguous_signal"
        if not latest.repeat_safe or latest.message_key is None:
            return "unknown", None, "stable_identity_unverified"
        return "known", latest.message_key, None
    if latest.kind in {"media", "resume_card", "card"}:
        return "non_text", None, None
    return "unknown", None, "ambiguous_signal"


def new_message_keys(previous: Conversation, current: Conversation) -> tuple[str, ...]:
    """比较同一会话的两个独立快照，只返回当前新出现的稳定消息键。"""
    validate_conversation(previous)
    validate_conversation(current)
    if (
        previous.app != current.app
        or previous.profile_id != current.profile_id
        or previous.conversation_key != current.conversation_key
        or previous.identity_hash != current.identity_hash
    ):
        raise ValueError("conversation identity mismatch")
    old = {
        message.message_key
        for message in previous.messages
        if message.message_key is not None
    }
    keys: list[str] = []
    for message in current.messages:
        if message.message_key is not None and message.message_key not in old:
            if message.message_key not in keys:
                keys.append(message.message_key)
    return tuple(keys)


def _validate_signal_names(values: object) -> tuple[str, ...]:
    """校验页面信号数组的非空、去重与固定词表。"""
    if not isinstance(values, (tuple, list)):
        raise ValueError("page signals are invalid")
    names = tuple(values)
    if any(not isinstance(name, str) for name in names):
        raise ValueError("page signal is invalid")
    if any(name not in PAGE_SIGNALS for name in names):
        raise ValueError("page signal is invalid")
    if len(set(names)) != len(names):
        raise ValueError("page signal is duplicated")
    return names


def _require_evidence(
    values: object, fields: tuple[str, ...]
) -> tuple[FieldEvidence, ...]:
    """要求字段证据数组长度与字段顺序完全匹配。"""
    if not isinstance(values, (tuple, list)) or len(values) != len(fields):
        raise ValueError("field evidence list is invalid")
    evidence = tuple(values)
    for item, name in zip(evidence, fields):
        validate_field_evidence(item)
        if item.field != name:
            raise ValueError("field evidence order is invalid")
    return evidence


def _require_ok_evidence(evidence: FieldEvidence) -> None:
    """要求给定字段证据为成功的层级或已标定像素结论。"""
    if evidence.state != "ok" or evidence.reason_code is not None:
        raise ValueError("field evidence is not ok")
    if evidence.source not in {"hierarchy", "pixel", "human"}:
        raise ValueError("field evidence source is invalid")


def _validate_missing_fields(values: object) -> tuple[str, ...]:
    """校验职位详情缺失字段去重且按固定顺序保存。"""
    if not isinstance(values, (tuple, list)):
        raise ValueError("missing fields are invalid")
    names = tuple(values)
    allowed = ("recruiter", "location")
    if any(not isinstance(name, str) for name in names):
        raise ValueError("missing field name is invalid")
    if any(name not in allowed for name in names) or len(set(names)) != len(names):
        raise ValueError("missing field name is invalid")
    if names != tuple(name for name in allowed if name in set(names)):
        raise ValueError("missing fields are not canonical")
    return names


def _is_enum(value: object, allowed: object) -> bool:
    """判断字符串枚举成员，避免列表等不可哈希类型泄漏 TypeError。"""
    return isinstance(value, str) and value in allowed


def _reason(reasons: list[str]) -> str:
    """按未知原因优先级选择唯一原因码。"""
    for name in FIELD_REASON_PRIORITY:
        if name in reasons:
            return name
    return "ambiguous_signal"


def _is_nonnegative_int(value: object) -> bool:
    """判断是否为非负整数且排除 bool。"""
    return type(value) is int and value >= 0
