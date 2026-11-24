"""提供真实 profile 任务与共享门禁绑定的只读 DecisionLeasePort 适配。"""

from __future__ import annotations

from pathlib import Path

from app.adapters.sqlite.repo import SqliteStore
from app.domain.policy import utc_now
from app.ports.device_gate import SharedDeviceGatePort

PROFILE_LEASE_REASONS = frozenset(
    {"device_not_held", "device_gate_mismatch", "stale_lease"}
)


class ProfileSessionLease:
    """每次读取都重核 profile 库任务/设备租约与共享门禁身份；不缓存。"""

    def __init__(
        self,
        store: SqliteStore,
        nonce: str,
        *,
        profile_id: str,
        gate: SharedDeviceGatePort,
        profile_db_path: Path,
        clock: object = utc_now,
    ) -> None:
        """保存真实 profile 仓储、任务 nonce、共享门禁与可信时钟。"""
        self._store = store
        self._nonce = nonce
        self._profile_id = profile_id
        self._gate = gate
        self._profile_db_path = Path(profile_db_path).resolve()
        self._clock = clock

    def current_expiry(self) -> str | None:
        """返回当前真实租约截止时间；身份/绑定/到期任一不成立返回 None。"""
        try:
            if self._store.profile_id != self._profile_id:
                return None
            if self._store.path != self._profile_db_path:
                return None
            self._gate.verify()
            current = (
                self._clock.now()
                if hasattr(self._clock, "now")
                else self._clock()
            )
            return self._store.current_lease_expiry(self._nonce, now=current)
        except Exception:
            return None
