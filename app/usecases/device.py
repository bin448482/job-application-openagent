"""编排共享设备门禁的只读占用视图与同 profile 写入保护。"""

from __future__ import annotations

from app.ports.device_gate import DeviceGateError

GATE_OCCUPIED_STATES = ("held", "quarantined")


def occupancy_payload(row: dict[str, object] | None) -> dict[str, object]:
    """把唯一 primary 行映射为只读占用结果；明确无行表示确认无占用。"""
    if row is None:
        return {
            "inspect_only": True,
            "occupied": False,
            "state": None,
            "profile_id": None,
            "owner_pid": None,
            "acquired_at": None,
            "updated_at": None,
        }
    state = row["state"]
    if state not in GATE_OCCUPIED_STATES:
        raise DeviceGateError("device_gate_io_error")
    return {
        "inspect_only": True,
        "occupied": True,
        "state": state,
        "profile_id": row["profile_id"],
        "owner_pid": row["owner_pid"],
        "acquired_at": row["acquired_at"],
        "updated_at": row["updated_at"],
    }


def require_profile_writable(
    row: dict[str, object] | None, *, profile_id: str
) -> None:
    """先验证占用状态，再只对同 profile 的 held/quarantined 拒绝写入。

    占用行缺失表示已确认无设备会话；行在但状态无法识别时按未知处理，
    任何 profile 都不做写入放行，避免不同 profile 绕过坏行保护。
    调用方负责把门禁库不可读等读取失败按失败关闭处理。
    """
    if row is None:
        return
    state = row["state"]
    if state not in GATE_OCCUPIED_STATES:
        raise DeviceGateError("device_gate_io_error")
    if row["profile_id"] != profile_id:
        return
    if state == "held":
        raise DeviceGateError("device_busy")
    raise DeviceGateError("device_quarantined")
