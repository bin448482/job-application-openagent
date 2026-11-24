"""声明 M4b 决策桥、当前包快照与可信租约的最小端口与固定诊断词表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

BRIDGE_TIMEOUT_SECONDS = 20
BRIDGE_MAX_OUTPUT_BYTES = 64 * 1024
BRIDGE_ROLES = ("system", "developer", "user")
BRIDGE_PROTOCOLS = {
    "external-agent": "m4b-agent-v1",
    "local-process": "m4b-local-v1",
}
BRIDGE_PROVIDERS = tuple(BRIDGE_PROTOCOLS)

BRIDGE_REASON_CODES = frozenset(
    {
        "invalid_provider",
        "authorization_required",
        "bridge_start_failed",
        "bridge_unavailable",
        "bridge_timeout",
        "bridge_protocol",
        "bridge_roles",
        "bridge_auth",
        "bridge_quota",
        "bridge_disconnected",
    }
)

# 只有传输层能直接观察到的“进程尚未启动”可重试；进程启动后一律视为可能已提交。
BRIDGE_RETRYABLE_REASON_CODES = frozenset({"bridge_start_failed"})


@dataclass(frozen=True)
class BridgeDescriptor:
    """描述桥的固定身份与报告摘要，不包含凭据、端点或 argv 原值。"""

    provider_id: str
    model_id: str
    model_config_hash: str
    bridge: str
    simulated: bool

    def to_dict(self) -> dict[str, object]:
        """转为报告使用的固定五键对象。"""
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_config_hash": self.model_config_hash,
            "bridge": self.bridge,
            "simulated": self.simulated,
        }


class BridgeError(Exception):
    """携带固定桥诊断、可信提交边界与是否允许重试；消息不含原异常。"""

    def __init__(self, reason_code: str, *, submitted: bool) -> None:
        """保存白名单原因码与传输层观察到的提交状态，异常文本只用原因码。"""
        if reason_code not in BRIDGE_REASON_CODES:
            raise ValueError("bridge reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.submitted = bool(submitted)

    @property
    def retryable(self) -> bool:
        """只允许可证明未启动的固定传输错误重试。"""
        return not self.submitted and self.reason_code in BRIDGE_RETRYABLE_REASON_CODES


class DecisionBridgePort(Protocol):
    """声明固定三消息分区的单次决策桥；不接收 role/tool/历史配置。"""

    def descriptor(self) -> BridgeDescriptor:
        """返回 provider/model/config 摘要与 simulated 标记。"""
        ...

    def complete(
        self,
        *,
        system_message: str,
        developer_message: str,
        user_message: str,
        echo: Mapping[str, str],
    ) -> str:
        """发送固定 system/developer/user 与独立回显对象，返回原始 Decision JSON 文本。"""
        ...


class DecisionSnapshotPort(Protocol):
    """声明桥调用前后重读当前 base 与活动包四载荷的能力。"""

    def reload(self) -> tuple[dict[str, str], dict[str, str]]:
        """返回当前 base 原文与四载荷原文；读取失败由适配器映射固定错误。"""
        ...


class DecisionLeasePort(Protocol):
    """声明读取可信租约截止时间的最小能力；M4c/M5 注入真实租约。"""

    def current_expiry(self) -> str | None:
        """返回规范 UTC 截止时间；无租约返回 None，由用例拒绝决策。"""
        ...


class DecisionClockPort(Protocol):
    """声明可信当前时间；每次调用都应重新读取，不得由模型或固定测试值替代。"""

    def now(self) -> str:
        """返回规范 UTC 当前时间，供租约、事实有效期与回显边界复验。"""
        ...
