"""声明 profile 独立运行库、长期事实与活动检查的最小端口与安全错误。"""

from __future__ import annotations

from typing import Protocol

PROFILE_REASON_CODES = frozenset(
    {
        "invalid_profile",
        "invalid_fact",
        "fact_conflict",
        "fact_not_found",
        "profile_io_error",
        "profile_mismatch",
    }
)


class ProfileError(Exception):
    """携带固定 profile 原因码；不携带事实值、绝对路径或底层异常文本。"""

    def __init__(self, reason_code: str) -> None:
        """校验并保存白名单原因码，异常文本只含该码。"""
        if reason_code not in PROFILE_REASON_CODES:
            raise ValueError("profile reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


class ProfileStorePort(Protocol):
    """抽象按 profile 绑定的独立库、事实登记与未决活动只读检查。"""

    def open(self) -> dict[str, object]:
        """打开或创建本 profile 独立库并返回绑定元数据。"""
        ...

    def set_fact(
        self,
        key: str,
        value: str,
        expires_at: str | None,
        *,
        now: str,
        replace: bool,
    ) -> dict[str, object]:
        """新增一个程序生成来源的事实；replace 明确作废旧 active 行。"""
        ...

    def revoke_fact(self, key: str, *, now: str) -> dict[str, object]:
        """把该键当前 active 来源全部标记 revoked，不删除历史。"""
        ...

    def forget_fact(self, key: str, *, now: str) -> dict[str, object]:
        """删除该键当前事实行，仅保留不含值的来源审计事件。"""
        ...

    def active_fact_rows(self) -> list[dict[str, object]]:
        """返回全部 active 行，含已过期行，供唯一筛选器计算待办。"""
        ...

    def source_ids_for_key(self, key: str) -> list[str]:
        """返回该键全部历史/当前来源 ID，用于遗忘前的无值提案。"""
        ...

    def pending_activity(self) -> dict[str, object]:
        """只读返回现有 M1 任务的未决/活动计数，供 restore 前检查。"""
        ...

    def close(self) -> None:
        """关闭本机连接；重复关闭保持无害。"""
        ...
