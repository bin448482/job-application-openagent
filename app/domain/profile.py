"""定义 profile 长期事实的纯校验、状态筛选与待办计算，不做任何 IO。"""

from __future__ import annotations

from app.domain.models import is_nonempty_text, parse_utc
from app.domain.policy import (
    FACT_KEYS,
    PolicyPack,
    has_contact,
    select_allowed_facts_with_reasons,
)

FACT_STATES = frozenset({"active", "superseded", "revoked"})
FACT_EVENT_ACTIONS = frozenset({"set", "revoke", "forget"})
FACT_PENDING_REASONS = frozenset({"missing", "expired", "conflict"})
FACT_VALUE_MAX_CHARS = 200
FACT_ROW_KEYS = (
    "key",
    "value",
    "source",
    "expires_at",
    "source_id",
    "state",
    "confirmed_at",
)


class FactInputError(ValueError):
    """表示事实输入不可用；只携带固定字段名，不携带原值。"""

    def __init__(self, field: str, code: str) -> None:
        """保存固定字段与诊断码，异常文本只含诊断码。"""
        super().__init__(code)
        self.field = field
        self.code = code


def _has_disallowed_char(value: str) -> bool:
    """拒绝换行、其它控制字符和孤立代理字符，保持单行本机文本。"""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def validate_fact_key(key: object) -> str:
    """只接受注册表中的三个长期事实键。"""
    if not isinstance(key, str) or key not in FACT_KEYS:
        raise FactInputError("key", "vocabulary")
    return key


def validate_fact_value(value: object) -> str:
    """接受 1–200 个可编码单行字符，不规范化或改写使用者原值。"""
    if not is_nonempty_text(value):
        raise FactInputError("value", "type")
    assert isinstance(value, str)
    if len(value) > FACT_VALUE_MAX_CHARS or _has_disallowed_char(value):
        raise FactInputError("value", "range")
    if has_contact(value):
        raise FactInputError("value", "sensitive")
    return value


def validate_fact_expiry(expires_at: object, *, now: str) -> str | None:
    """有效期只能为空或晚于当前时间的规范 UTC 时间。"""
    if expires_at is None:
        return None
    if not isinstance(expires_at, str):
        raise FactInputError("expires_at", "type")
    try:
        now_dt = parse_utc(now)
        expires_dt = parse_utc(expires_at)
    except ValueError:
        raise FactInputError("expires_at", "format") from None
    if expires_dt <= now_dt:
        raise FactInputError("expires_at", "range")
    return expires_at


def validate_fact_rows(rows: object) -> list[dict[str, object]]:
    """校验仓储返回的事实行形状；不可用行直接拒绝，不猜测其语义。"""
    if not isinstance(rows, list):
        raise ValueError("fact rows are invalid")
    normalized: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FACT_ROW_KEYS):
            raise ValueError("fact row is invalid")
        validate_fact_key(row["key"])
        if not is_nonempty_text(row["value"]):
            raise ValueError("fact value is invalid")
        if row["source"] != "user_confirmed":
            raise ValueError("fact source is invalid")
        if not isinstance(row["source_id"], str) or not row["source_id"]:
            raise ValueError("fact source id is invalid")
        if row["state"] not in FACT_STATES:
            raise ValueError("fact state is invalid")
        try:
            parse_utc(row["confirmed_at"])
            if row["expires_at"] is not None:
                parse_utc(row["expires_at"])
        except ValueError:
            raise ValueError("fact time is invalid") from None
        normalized.append(
            {
                "key": str(row["key"]),
                "value": str(row["value"]),
                "source": "user_confirmed",
                "expires_at": row["expires_at"],
                "source_id": str(row["source_id"]),
                "state": str(row["state"]),
                "confirmed_at": str(row["confirmed_at"]),
            }
        )
    return normalized


def select_profile_facts(
    rows: list[dict[str, object]], pack: PolicyPack, *, now: str
) -> dict[str, object]:
    """把 profile active 行交给唯一事实规则，并按结果组织无值待办。

    active/superseded/revoked 是持久化状态转换，由 profile 侧先排除非 active；
    包白名单、来源、有效期、重复/冲突与 missing 原因全部由唯一
    `select_allowed_facts_with_reasons` 产出，不复制第二套选择语义。
    """
    active_rows = [
        row for row in validate_fact_rows(rows) if row["state"] == "active"
    ]
    projected = [
        {
            "key": row["key"],
            "value": row["value"],
            "source": row["source"],
            "expires_at": row["expires_at"],
        }
        for row in active_rows
    ]
    return select_allowed_facts_with_reasons(projected, pack, now=now)
