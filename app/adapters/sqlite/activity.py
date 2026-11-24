"""实现 restore 前的真实 profile 活动、设备占用与 M1 未决状态检查。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.domain.policy import utc_now
from app.ports.device_gate import DeviceGateError
from app.ports.profile import ProfileError


class ProfilePolicyActivity:
    """只读检查共享设备占用与 profile 库现有任务状态，不解除占用或改库。

    M4c 只核对现有 M1 任务/设备行；M5 动作账本尚未实现，因此本检查不构成
    完整 MVP 恢复对账，也不得被表述为完整恢复入口。
    """

    def __init__(
        self,
        project_root: Path,
        *,
        os_root: Path | None = None,
        platform_name: str | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        """保存项目根、可注入测试 OS 根与时钟；构造阶段不访问文件。"""
        self._root = Path(project_root)
        self._os_root = os_root
        self._platform_name = platform_name
        self._clock = clock

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
        if not path.is_file():
            return "stopped"
        store = SqliteProfileStore(
            self._root, profile_id, create=False, now=now
        )
        try:
            store.open()
            pending = store.pending_activity()
        except (ProfileError, ValueError):
            return "unknown"
        finally:
            store.close()
        if pending["leased"] or pending["running"] or pending["device_lease"]:
            return "active"
        if pending["blocked"]:
            return "unknown"
        return "stopped"
