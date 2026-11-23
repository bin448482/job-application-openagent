"""校验合成探针输入并生成 M0 doctor 的唯一 JSON 业务结果。"""

from __future__ import annotations

from app.ports.device import DeviceEntry, ProbeSnapshot

SCHEMA_VERSION = 1
DEVICE_STATES = frozenset({"device", "offline", "unauthorized"})
FOREGROUND_APPS = frozenset({"boss", "other", "unknown"})
_OUTPUT_KEYS = (
    "schema_version",
    "status",
    "reason_code",
    "device_count",
    "selected_alias",
    "unlocked",
    "foreground_app",
)


def parse_probe_input(data: object) -> ProbeSnapshot:
    """把合成 fixture 对象校验收敛为探针快照，拒绝额外键、重复别名与非法枚举。"""
    if not isinstance(data, dict):
        raise ValueError("fixture must be an object")
    if set(data) != {"devices", "selected_alias", "unlocked", "foreground_app"}:
        raise ValueError("fixture keys are invalid")

    raw_devices = data["devices"]
    if not isinstance(raw_devices, list):
        raise ValueError("devices must be a list")

    devices: list[DeviceEntry] = []
    aliases: set[str] = set()
    for raw_device in raw_devices:
        if not isinstance(raw_device, dict) or set(raw_device) != {"alias", "state"}:
            raise ValueError("device entries are invalid")
        alias = raw_device["alias"]
        state = raw_device["state"]
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("device alias is invalid")
        if alias in aliases:
            raise ValueError("device alias is duplicated")
        if not isinstance(state, str) or state not in DEVICE_STATES:
            raise ValueError("device state is invalid")
        aliases.add(alias)
        devices.append(DeviceEntry(alias=alias, state=state))

    selected_alias = data["selected_alias"]
    if selected_alias is not None:
        if not isinstance(selected_alias, str) or selected_alias not in aliases:
            raise ValueError("selected alias is invalid")

    unlocked = data["unlocked"]
    if unlocked is not None and not isinstance(unlocked, bool):
        raise ValueError("unlocked must be a boolean or null")

    foreground_app = data["foreground_app"]
    if not isinstance(foreground_app, str) or foreground_app not in FOREGROUND_APPS:
        raise ValueError("foreground app is invalid")

    return ProbeSnapshot(
        devices=tuple(devices),
        selected_alias=selected_alias,
        unlocked=unlocked,
        foreground_app=foreground_app,
    )


def run_doctor(snapshot: ProbeSnapshot) -> dict[str, object]:
    """按清单、选择、状态、解锁、前台的顺序判定，并返回固定字段的 doctor 结果。"""
    device_count = len(snapshot.devices)
    if device_count == 0:
        return _failure(snapshot, None, "device_disconnected")

    selected_alias = snapshot.selected_alias
    if selected_alias is None:
        if device_count != 1:
            return _failure(snapshot, None, "device_ambiguous")
        selected_alias = snapshot.devices[0].alias

    selected = next(
        (device for device in snapshot.devices if device.alias == selected_alias),
        None,
    )
    if selected is None:
        raise ValueError("selected device is missing")

    if selected.state == "offline":
        return _failure(snapshot, selected_alias, "device_offline")
    if selected.state == "unauthorized":
        return _failure(snapshot, selected_alias, "device_unauthorized")
    if selected.state != "device":
        return _failure(snapshot, selected_alias, "device_offline")

    if snapshot.unlocked is not True:
        return _failure(snapshot, selected_alias, "device_locked")
    if snapshot.foreground_app != "boss":
        return _failure(snapshot, selected_alias, "wrong_page")

    return _result(snapshot, selected_alias, "ok", None)


def _failure(
    snapshot: ProbeSnapshot,
    selected_alias: str | None,
    reason_code: str,
) -> dict[str, object]:
    """保留已选逻辑名与已知字段，生成失败业务结果。"""
    return _result(snapshot, selected_alias, "failed", reason_code)


def _result(
    snapshot: ProbeSnapshot,
    selected_alias: str | None,
    status: str,
    reason_code: str | None,
) -> dict[str, object]:
    """按白名单顺序组装唯一的 doctor JSON 业务对象。"""
    return {
        _OUTPUT_KEYS[0]: SCHEMA_VERSION,
        _OUTPUT_KEYS[1]: status,
        _OUTPUT_KEYS[2]: reason_code,
        _OUTPUT_KEYS[3]: len(snapshot.devices),
        _OUTPUT_KEYS[4]: selected_alias,
        _OUTPUT_KEYS[5]: snapshot.unlocked,
        _OUTPUT_KEYS[6]: snapshot.foreground_app,
    }
