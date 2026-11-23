"""附加生成 M1 合成请求脚本；由 build_assets.py 调用。"""

from __future__ import annotations

from pathlib import Path
import json

ROOT = Path("tests/scenarios")
T0 = "2026-11-24T00:00:00.000Z"
T20 = "2026-11-24T00:00:20.000Z"
T21 = "2026-11-24T00:00:21.000Z"
T61 = "2026-11-24T00:01:01.000Z"
T120 = "2026-11-24T00:02:00.000Z"


def write_request(name: str, steps: list[dict], start_at: str = T0) -> str:
    """写出一个 M1 探针请求并返回相对夹具名。"""
    relative = f"fixtures/m1/{name}.json"
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"start_at": start_at, "steps": steps}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return name


def add_open(host: str = "local", stop: str = "stopped", fault: str | None = None) -> dict:
    """构造 open 步骤；host/stop/fault 均为合成枚举。"""
    step = {"op": "open", "store_host": host, "stop_mode": stop}
    if fault:
        step["fault"] = fault
    return step


def seed(count: int = 1, kinds: list[str] | None = None, now: str = T0) -> list[dict]:
    """构造 init/open/upsert/close 初态序列。"""
    return [
        {"op": "init"},
        add_open(),
        {"op": "upsert", "count": count, "kinds": kinds or ["unread"], "now": now},
        {"op": "close"},
    ]


def build_m1_fixtures() -> dict[str, str]:
    """生成 M1 全部脚本并返回名称索引。"""
    names: dict[str, str] = {}

    def add(name: str, steps: list[dict], start_at: str = T0):
        """登记一个脚本名并写出文件。"""
        names[name] = write_request(name, steps, start_at)

    add("l01_init_midway", [{"op": "init", "fault_at": 2}, {"op": "inspect_raw"}])
    add("l01_unknown_version", [{"op": "init"}, {"op": "prepare", "mode": "set_user_version", "value": 99}, {"op": "init"}, {"op": "inspect_raw"}])
    add("l01_nonempty_no_version", [{"op": "init"}, {"op": "prepare", "mode": "set_user_version", "value": 0}, {"op": "init"}, {"op": "inspect_raw"}])
    add("l01_missing_index", [{"op": "init"}, {"op": "prepare", "mode": "drop_index"}, {"op": "init"}])

    add("l02_setup_one", seed(1))
    add("l02_setup_two", seed(2, ["unread", "new_message"]))
    add("l02_claim_unread", [{"op": "barrier"}, add_open(), {"op": "claim", "kind": "unread", "owner_id": "00000000-0000-4000-8000-000000000021", "now": T0}])
    add("l02_claim_unread_2", [{"op": "barrier"}, add_open(), {"op": "claim", "kind": "unread", "owner_id": "00000000-0000-4000-8000-000000000029", "now": T0}])
    add("l02_claim_new", [{"op": "barrier"}, add_open(), {"op": "claim", "kind": "new_message", "owner_id": "00000000-0000-4000-8000-000000000022", "now": T0}])
    add("l02_verify", [add_open(), {"op": "inspect", "now": T0}])
    add("l02_ttl", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0},
        {"op": "claim", "kind": "unread", "owner_id": "00000000-0000-4000-8000-000000000023", "now": T61},
        {"op": "inspect", "now": T61},
    ])
    add("l02_device_row", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0},
        {"op": "claim", "kind": "unread", "owner_id": "00000000-0000-4000-8000-000000000024", "now": T0},
        {"op": "inspect", "now": T0},
    ])

    add("l03_renew", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "renew", "now": T20}, {"op": "inspect", "now": T20},
    ])
    add("l03_old_lease", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "renew", "now": T20},
        {"op": "finish", "lease_source": "previous", "state": "done", "proof": "read_ok", "now": T21},
        {"op": "inspect", "now": T21},
    ])
    add("l03_old_nonce", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "finish", "use_random_nonce": True, "state": "done", "proof": "read_ok", "now": T0},
        {"op": "inspect", "now": T0},
    ])
    add("l03_expired", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "finish", "state": "done", "proof": "read_ok", "now": T61},
        {"op": "inspect", "now": T61},
    ])
    add("l03_duplicate_start", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "start", "now": T0}, {"op": "inspect", "now": T0},
    ])
    add("l03_duplicate_finish", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "finish", "state": "done", "proof": "read_ok", "now": T0},
        {"op": "finish", "lease_source": "previous", "state": "done", "proof": "read_ok", "now": T0},
        {"op": "inspect", "now": T0},
    ])

    add("l04_leased_stopped", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "close"},
        add_open(stop="stopped"), {"op": "recover", "confirm_stopped": True, "now": T61},
        {"op": "inspect", "now": T61},
    ])
    add("l04_running_stopped", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"}, {"op": "close"}, add_open(stop="stopped"),
        {"op": "recover", "confirm_stopped": True, "now": T61}, {"op": "inspect", "now": T61},
    ])
    add("l04_alive", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"}, {"op": "close"}, add_open(stop="alive"),
        {"op": "recover", "confirm_stopped": True, "now": T61}, {"op": "inspect", "now": T61},
    ])
    add("l04_unknown", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"}, {"op": "close"}, add_open(stop="unknown"),
        {"op": "recover", "confirm_stopped": True, "now": T61}, {"op": "inspect", "now": T61},
    ])
    add("l04_permission", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"}, {"op": "close"}, add_open(stop="permission_denied"),
        {"op": "recover", "confirm_stopped": True, "now": T61}, {"op": "inspect", "now": T61},
    ])
    add("l04_different_host", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"}, {"op": "close"}, add_open(host="other", stop="stopped"),
        {"op": "recover", "confirm_stopped": True, "now": T61}, {"op": "inspect", "now": T61},
    ])
    add("l04_confirm_false", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "recover", "confirm_stopped": False, "now": T61}, {"op": "inspect", "now": T61},
    ])

    add("l05_undetermined", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"},
        {"op": "finish", "state": "blocked", "reason_code": "incomplete_snapshot", "proof": "read_undetermined", "now": T0},
        {"op": "inspect", "now": T0},
    ])
    add("l05_evidence_unknown", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"},
        {"op": "finish", "state": "blocked", "reason_code": "result_unverified", "proof": "evidence_unknown", "now": T0},
        {"op": "inspect", "now": T0},
    ])
    for fault, label in (
        ("finish_before_task_update", "task_update_miss"),
        ("finish_before_device_update", "device_update_miss"),
        ("finish_before_event", "event_write_fail"),
    ):
        add(f"l05_{label}", [
            {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
            {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
            {"op": "remember_device"}, {"op": "close"}, add_open(fault=fault),
            {"op": "finish", "state": "blocked", "reason_code": "incomplete_snapshot", "proof": "read_undetermined", "now": T0},
            {"op": "inspect", "now": T0},
        ])
    add("l05_rollback_claim_busy", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"}, {"op": "close"}, add_open(fault="finish_before_event"),
        {"op": "finish", "state": "blocked", "reason_code": "incomplete_snapshot", "proof": "read_undetermined", "now": T0},
        {"op": "claim", "kind": "unread", "owner_id": "00000000-0000-4000-8000-000000000025", "now": T0},
        {"op": "inspect", "now": T0},
    ])

    add("l06_parent_child_unknown", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "remember_device"}, {"op": "close"}, add_open(stop="stopped"),
        {"op": "recover", "confirm_stopped": True, "now": T61},
        {"op": "claim", "kind": "unread", "owner_id": "00000000-0000-4000-8000-000000000026", "now": T61},
        {"op": "inspect", "now": T61},
    ])
    add("l06_change_owner", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "recover", "owner_mode": "other", "confirm_stopped": True, "now": T61},
        {"op": "inspect", "now": T61},
    ])
    add("l06_ttl_expired", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "now": T0}, {"op": "start", "now": T0},
        {"op": "close"}, add_open(stop="stopped"),
        {"op": "recover", "confirm_stopped": True, "now": T61},
        {"op": "claim", "kind": "unread", "owner_id": "00000000-0000-4000-8000-000000000027", "now": T120},
        {"op": "inspect", "now": T120},
    ])

    add("l07_cross_app", [
        {"op": "init"}, add_open(), {"op": "claim_other_app", "now": T0}, {"op": "inspect", "now": T0},
    ])
    add("l07_bool_integer", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0},
        {"op": "claim", "kind": "unread", "owner_pid": True, "now": T0},
        {"op": "inspect", "now": T0},
    ])
    add("l07_busy_inline", [
        {"op": "init"}, add_open(), {"op": "upsert", "count": 1, "now": T0}, {"op": "close"},
        add_open(), {"op": "locked_claim", "now": T0}, {"op": "inspect", "now": T0},
    ])
    add("l07_io_error", [{"op": "init"}, {"op": "io_error_open"}])
    return names


if __name__ == "__main__":
    print(build_m1_fixtures())
