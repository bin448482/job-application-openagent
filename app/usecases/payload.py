"""集中序列化唯一 ReadResult 外层与已登记的 M2/M3 读取值类型。"""

from __future__ import annotations

from app.domain.models import SIGNAL_KEYS, ReadResult, UnreadSnapshot, validate_read_result
from app.domain.reading import (
    ChatMessage,
    Conversation,
    FieldEvidence,
    JobCard,
    JobDetail,
    JobList,
    PageSnapshot,
    UnreadRead,
    UnreadRowRead,
)

SCHEMA_VERSION = 1


def result_payload(result: ReadResult) -> dict[str, object]:
    """序列化 ReadResult 固定七键与当前值类型全部固定键。"""
    validate_read_result(result)
    return {
        "status": result.status,
        "value": value_payload(result.value),
        "reason_code": result.reason_code,
        "snapshot_id": result.snapshot_id,
        "observed_at": result.observed_at,
        "source": result.source,
        "confidence": result.confidence,
    }


def value_payload(value: object) -> object:
    """按值类型投影固定白名单字段，不用 asdict 扩大 JSON 键。"""
    if value is None:
        return None
    if isinstance(value, UnreadSnapshot):
        return unread_snapshot_payload(value)
    if isinstance(value, PageSnapshot):
        return {
            "app": value.app,
            "profile_id": value.profile_id,
            "page": value.page,
            "signals": list(value.signals),
            "field_evidence": [_evidence_payload(item) for item in value.field_evidence],
        }
    if isinstance(value, JobList):
        return {
            "app": value.app,
            "profile_id": value.profile_id,
            "coverage": value.coverage,
            "cards": [_job_card_payload(card) for card in value.cards],
            "field_evidence": [_evidence_payload(item) for item in value.field_evidence],
        }
    if isinstance(value, JobDetail):
        return {
            "app": value.app,
            "profile_id": value.profile_id,
            "job_key": value.job_key,
            "identity_hash": value.identity_hash,
            "title_raw": value.title_raw,
            "title": value.title,
            "company_raw": value.company_raw,
            "company": value.company,
            "salary_raw": value.salary_raw,
            "salary": value.salary,
            "recruiter_raw": value.recruiter_raw,
            "recruiter": value.recruiter,
            "location_raw": value.location_raw,
            "location": value.location,
            "description": value.description,
            "description_hash": value.description_hash,
            "description_length": value.description_length,
            "complete": value.complete,
            "missing_fields": list(value.missing_fields),
            "field_evidence": [_evidence_payload(item) for item in value.field_evidence],
        }
    if isinstance(value, UnreadRead):
        return {
            "app": value.app,
            "profile_id": value.profile_id,
            "coverage": value.coverage,
            "rows": [_unread_row_payload(row) for row in value.rows],
            "unread_state": value.unread_state,
            "field_evidence": [_evidence_payload(item) for item in value.field_evidence],
        }
    if isinstance(value, Conversation):
        return {
            "app": value.app,
            "profile_id": value.profile_id,
            "conversation_key": value.conversation_key,
            "identity_hash": value.identity_hash,
            "contact": value.contact,
            "contact_verified": value.contact_verified,
            "role": value.role,
            "message_identity_verified": value.message_identity_verified,
            "read_complete": value.read_complete,
            "messages": [_chat_message_payload(message) for message in value.messages],
            "inbound_state": value.inbound_state,
            "latest_inbound_key": value.latest_inbound_key,
            "field_evidence": [_evidence_payload(item) for item in value.field_evidence],
        }
    raise ValueError("read value is not serializable")


def unread_snapshot_payload(value: UnreadSnapshot) -> dict[str, object]:
    """序列化 M2 未读快照，固定 SIGNAL_KEYS 顺序与全部既有字段。"""
    return {
        "app": value.app,
        "profile_id": value.profile_id,
        "coverage": value.coverage,
        "signals": {
            name: {
                "state": value.signals[name].state,
                "count": value.signals[name].count,
                "reason_code": value.signals[name].reason_code,
            }
            for name in SIGNAL_KEYS
        },
        "rows": [
            {
                "conversation_key": row.conversation_key,
                "identity_hash": row.identity_hash,
                "preview_hash": row.preview_hash,
                "time_hint": row.time_hint,
                "unread_count": row.unread_count,
            }
            for row in value.rows
        ],
        "candidates": [
            {
                "candidate_key": candidate.candidate_key,
                "conversation_key": candidate.conversation_key,
                "kind": candidate.kind,
                "source_names": list(candidate.source_names),
                "fingerprint": candidate.fingerprint,
            }
            for candidate in value.candidates
        ],
        "unread_state": value.unread_state,
    }


def _job_card_payload(card: JobCard) -> dict[str, object]:
    """序列化职位卡的原文、比较值与可选键。"""
    return {
        "job_key": card.job_key,
        "identity_hash": card.identity_hash,
        "title_raw": card.title_raw,
        "title": card.title,
        "company_raw": card.company_raw,
        "company": card.company,
        "salary_raw": card.salary_raw,
        "salary": card.salary,
    }


def _unread_row_payload(row: UnreadRowRead) -> dict[str, object]:
    """序列化单行未读结论。"""
    return {
        "identity_hash": row.identity_hash,
        "conversation_key": row.conversation_key,
        "state": row.state,
        "count": row.count,
        "source": row.source,
        "confidence": row.confidence,
        "reason_code": row.reason_code,
    }


def _chat_message_payload(message: ChatMessage) -> dict[str, object]:
    """序列化聊天行的类型、方向、内容指纹与稳定身份。"""
    return {
        "kind": message.kind,
        "direction": message.direction,
        "text": message.text,
        "content_hash": message.content_hash,
        "time_hint": message.time_hint,
        "stable_identity_hash": message.stable_identity_hash,
        "stable_identity_source": message.stable_identity_source,
        "repeat_safe": message.repeat_safe,
        "truncated": message.truncated,
        "direction_evidence": list(message.direction_evidence),
        "message_key": message.message_key,
    }


def _evidence_payload(evidence: FieldEvidence) -> dict[str, object]:
    """序列化字段证据。"""
    return {
        "field": evidence.field,
        "source": evidence.source,
        "confidence": evidence.confidence,
        "state": evidence.state,
        "reason_code": evidence.reason_code,
    }
