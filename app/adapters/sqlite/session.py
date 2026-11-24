"""提供真实 profile 任务与共享门禁绑定的只读 DecisionLeasePort 适配。"""

from __future__ import annotations

from pathlib import Path

from app.adapters.sqlite.repo import SqliteStore
from app.domain.models import Lease
from app.domain.policy import utc_now
from app.ports.device_gate import SharedDeviceGatePort


class ProfileSessionLease:
    """每次读取都重核 profile 库任务、取得时 Lease/Owner 与共享门禁身份。"""

    def __init__(
        self,
        store: SqliteStore,
        lease: Lease,
        *,
        profile_id: str,
        gate: SharedDeviceGatePort,
        profile_db_path: Path,
        clock: object = utc_now,
    ) -> None:
        """保存真实 profile 仓储、任务 Lease、共享门禁与可信时钟。"""
        self._store = store
        self._lease = lease
        self._profile_id = profile_id
        self._gate = gate
        self._profile_db_path = Path(profile_db_path).resolve()
        self._clock = clock

    def current_expiry(self) -> str | None:
        """返回当前真实租约截止时间；会话/绑定/owner/到期任一不成立返回 None。"""
        try:
            if self._store.profile_id != self._profile_id:
                return None
            if self._store.path != self._profile_db_path:
                return None
            identity = self._gate.session_identity()
            if (
                identity.profile_id != self._profile_id
                or Path(identity.profile_db_path).resolve()
                != self._profile_db_path
                or identity.owner != self._lease.owner
                or identity.state != "held"
            ):
                return None
            current = (
                self._clock.now()
                if hasattr(self._clock, "now")
                else self._clock()
            )
            return self._store.current_lease_expiry(self._lease, now=current)
        except Exception:
            return None
