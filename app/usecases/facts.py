"""编排 profile 长期事实的确认提案、写入、再显示、撤销与遗忘。"""

from __future__ import annotations

from app.domain.policy import FACT_KEYS, PolicyPack
from app.domain.profile import (
    FactInputError,
    select_profile_facts,
    validate_fact_expiry,
    validate_fact_key,
    validate_fact_value,
)
from app.ports.profile import ProfileError, ProfileStorePort


def _invalid_fact(error: FactInputError) -> ProfileError:
    """把领域字段错误映射为固定 invalid_fact，不保留原值。"""
    del error
    return ProfileError("invalid_fact")


def _validated(
    key: object, value: object, expires_at: object, *, now: str
) -> tuple[str, str, str | None]:
    """校验 set 输入；错误只暴露固定 invalid_fact。"""
    try:
        return (
            validate_fact_key(key),
            validate_fact_value(value),
            validate_fact_expiry(expires_at, now=now),
        )
    except FactInputError as error:
        raise _invalid_fact(error) from None


def _pending(store: ProfileStorePort, pack: PolicyPack | None, *, now: str) -> list[dict[str, str]]:
    """按可选包白名单计算不含值的 missing/expired/conflict 待办。"""
    if pack is None:
        return []
    return list(select_profile_facts(store.active_fact_rows(), pack, now=now)["pending"])


def propose_set(
    store: ProfileStorePort,
    *,
    profile_id: str,
    key: object,
    value: object,
    expires_at: object,
    replace: bool,
    now: str,
) -> dict[str, object]:
    """构造写入前提案；不落库、不生成来源 ID，供本机再次确认。"""
    checked_key, checked_value, checked_expiry = _validated(
        key, value, expires_at, now=now
    )
    active = [
        row
        for row in store.active_fact_rows()
        if row["key"] == checked_key and row["state"] == "active"
    ]
    return {
        "action": "set",
        "profile_id": profile_id,
        "confirmed": False,
        "proposal": {
            "key": checked_key,
            "value": checked_value,
            "expires_at": checked_expiry,
            "replace": bool(replace),
            "active_source_ids": [str(row["source_id"]) for row in active],
            "would_conflict": bool(active) and not replace,
        },
        "note": "confirm again with --confirm to persist; values stay local",
    }


def apply_set(
    store: ProfileStorePort,
    *,
    profile_id: str,
    key: object,
    value: object,
    expires_at: object,
    replace: bool,
    now: str,
    pack: PolicyPack | None = None,
) -> dict[str, object]:
    """写入事实并从库中重读确认结果；值只出现在本机命令输出。"""
    checked_key, checked_value, checked_expiry = _validated(
        key, value, expires_at, now=now
    )
    record = store.set_fact(
        checked_key,
        checked_value,
        checked_expiry,
        now=now,
        replace=replace,
    )
    reread = [
        row
        for row in store.active_fact_rows()
        if row["source_id"] == record["source_id"]
    ]
    if len(reread) != 1:
        raise ProfileError("profile_io_error")
    confirmed = reread[0]
    return {
        "action": "set",
        "profile_id": profile_id,
        "confirmed": True,
        "result": {
            "key": confirmed["key"],
            "value": confirmed["value"],
            "source": confirmed["source"],
            "source_id": confirmed["source_id"],
            "expires_at": confirmed["expires_at"],
            "confirmed_at": confirmed["confirmed_at"],
            "replaced_source_ids": record["replaced_source_ids"],
            "conflict": record["conflict"],
        },
        "display_value": confirmed["value"],
        "pending": _pending(store, pack, now=now),
    }


def propose_revoke(
    store: ProfileStorePort, *, profile_id: str, key: object
) -> dict[str, object]:
    """构造撤销提案，列出现有 active 来源 ID但不写入。"""
    try:
        checked_key = validate_fact_key(key)
    except FactInputError as error:
        raise _invalid_fact(error) from None
    active = [
        str(row["source_id"])
        for row in store.active_fact_rows()
        if row["key"] == checked_key and row["state"] == "active"
    ]
    return {
        "action": "revoke",
        "profile_id": profile_id,
        "confirmed": False,
        "proposal": {"key": checked_key, "active_source_ids": active},
        "note": "confirm again with --confirm to revoke",
    }


def apply_revoke(
    store: ProfileStorePort,
    *,
    profile_id: str,
    key: object,
    now: str,
    pack: PolicyPack | None = None,
) -> dict[str, object]:
    """执行撤销并返回来源 ID，不返回事实值。"""
    try:
        checked_key = validate_fact_key(key)
    except FactInputError as error:
        raise _invalid_fact(error) from None
    result = store.revoke_fact(checked_key, now=now)
    return {
        "action": "revoke",
        "profile_id": profile_id,
        "confirmed": True,
        "result": result,
        "pending": _pending(store, pack, now=now),
    }


def propose_forget(
    store: ProfileStorePort, *, profile_id: str, key: object
) -> dict[str, object]:
    """构造遗忘提案；只列来源 ID，不显示即将删除的值。"""
    try:
        checked_key = validate_fact_key(key)
    except FactInputError as error:
        raise _invalid_fact(error) from None
    sources = store.source_ids_for_key(checked_key)
    return {
        "action": "forget",
        "profile_id": profile_id,
        "confirmed": False,
        "proposal": {"key": checked_key, "source_ids": sources},
        "note": (
            "forget deletes the current profile fact row and keeps only source-ID "
            "audit; it cannot erase SQLite WAL pages or pre-existing local backups"
        ),
    }


def apply_forget(
    store: ProfileStorePort,
    *,
    profile_id: str,
    key: object,
    now: str,
    pack: PolicyPack | None = None,
) -> dict[str, object]:
    """删除该键当前事实行，保留无值审计并明确不宣称安全擦除。"""
    try:
        checked_key = validate_fact_key(key)
    except FactInputError as error:
        raise _invalid_fact(error) from None
    result = store.forget_fact(checked_key, now=now)
    return {
        "action": "forget",
        "profile_id": profile_id,
        "confirmed": True,
        "result": result,
        "secure_erase_claimed": False,
        "limitations": ["sqlite_wal_and_existing_local_backups_not_erased"],
        "pending": _pending(store, pack, now=now),
    }


def show_facts(
    store: ProfileStorePort,
    *,
    profile_id: str,
    pack: PolicyPack | None,
    now: str,
) -> dict[str, object]:
    """显示本机事实值、来源、有效期与按包白名单计算的待办。"""
    rows = store.active_fact_rows()
    scoped = select_profile_facts(rows, pack, now=now) if pack is not None else None
    return {
        "profile_id": profile_id,
        "facts": rows,
        "active_keys": sorted({str(row["key"]) for row in rows}),
        "registered_keys": sorted(FACT_KEYS),
        "pending": list(scoped["pending"]) if scoped is not None else [],
        "allowed": list(scoped["allowed"]) if scoped is not None else [],
        "scoped_by_pack": pack is not None,
    }
