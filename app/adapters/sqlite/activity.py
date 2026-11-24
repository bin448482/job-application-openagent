"""实现 restore 前的真实 profile 活动、设备占用与 M1 未决状态检查。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.adapters.path_state import PATH_FILE, PATH_MISSING, probe_path
from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.domain.policy import utc_now
from app.ports.device_gate import DeviceGateError
from app.ports.profile import ProfileError
from app.process_log import ProcessLog
from app.profile_open import open_profile_store


class ProfilePolicyActivity:
    """只读检查共享设备占用、profile 任务与 M5 未决动作，不解除占用或改库。

    M5 动作账本已经实现；本检查把 pending/unknown/cancelled 动作视为未决，
    但仍不执行动作对账或自动解锁。
    """

    def __init__(
        self,
        project_root: Path,
        *,
        os_root: Path | None = None,
        platform_name: str | None = None,
        clock: Callable[[], str] = utc_now,
        process_log: ProcessLog | None = None,
    ) -> None:
        """保存项目根、可注入测试 OS 根、时钟与日志；构造阶段不访问文件。"""
        self._root = Path(project_root)
        self._os_root = os_root
        self._platform_name = platform_name
        self._clock = clock
        self._process_log = process_log

    def assess_restore(self, *, profile_id: str, dest_pack: str) -> str:
        """返回 stopped/active/unknown；只有 stopped 允许恢复新策略版本。"""
        del dest_pack
        now = self._clock()
        try:
            gate = SharedDeviceGate(
                profile_id=profile_id,
                profile_db_path=profile_runtime_path(self._root, profile_id),
                os_root=self._os_root,
                platform_name=self._platform_name,
            )
            occupied = gate.inspect()
        except (DeviceGateError, ProfileError, ValueError):
            return "unknown"
        if occupied is not None:
            return "active" if occupied["state"] == "held" else "unknown"

        path = profile_runtime_path(self._root, profile_id)
        state = probe_path(path)
        if state == PATH_MISSING:
            return "stopped"
        if state != PATH_FILE:
            return "unknown"
        store = SqliteProfileStore(
            self._root, profile_id, create=False, now=now
        )
        try:
            open_profile_store(store, self._process_log)
            pending = store.pending_activity()
        except (ProfileError, ValueError):
            return "unknown"
        finally:
            store.close()
        if (
            pending["leased"]
            or pending["running"]
            or pending["device_lease"]
            or pending.get("jobs")
        ):
            return "active"
        if (
            pending["blocked"]
            or pending.get("actions")
            or pending.get("batches")
        ):
            return "unknown"
        return "stopped"
