"""生成 M2 探针请求：now 注入、合成初态 seed 与只读 inspect。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path("tests/scenarios")
T0 = "2026-11-24T00:00:00.000Z"
T5 = "2026-11-24T00:00:05.000Z"
T120 = "2026-11-24T00:02:00.000Z"


def write_request(name: str, value: dict) -> str:
    """写出一个 M2 探针请求并返回相对路径名。"""
    relative = f"fixtures/m2/probe/{name}.json"
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return name


def build_m2_requests() -> dict[str, str]:
    """生成 now 注入、seed 与 inspect 请求。"""
    names: dict[str, str] = {}

    def add(name: str, value: dict) -> None:
        """登记并写请求。"""
        names[name] = write_request(name, value)

    add(
        "parse_stale",
        {
            "mode": "parse",
            "fixture": "tests/scenarios/fixtures/m2/sensor/stale_snapshot/manifest.json",
            "now": T0,
        },
    )
    add(
        "parse_future",
        {
            "mode": "parse",
            "fixture": "tests/scenarios/fixtures/m2/sensor/future_snapshot/manifest.json",
            "now": T0,
        },
    )
    add(
        "parse_exactly_five",
        {
            "mode": "parse",
            "fixture": "tests/scenarios/fixtures/m2/sensor/positive/manifest.json",
            "now": T5,
        },
    )
    add(
        "parse_over_five",
        {
            "mode": "parse",
            "fixture": "tests/scenarios/fixtures/m2/sensor/positive/manifest.json",
            "now": "2026-11-24T00:00:05.001Z",
        },
    )
    positive = "tests/scenarios/fixtures/m2/sensor/positive/manifest.json"
    for name, state, reason, updated in (
        ("seed_queued", "queued", None, T0),
        ("seed_running", "running", None, T0),
        ("seed_blocked", "blocked", "recovery_requires_reconciliation", T0),
        ("seed_terminal", "done", None, T0),
        ("seed_backwards", "queued", None, T120),
    ):
        add(
            name,
            {
                "mode": "seed",
                "fixture": positive,
                "now": T0,
                "state": state,
                "reason_code": reason,
                "updated_at": updated,
            },
        )
    add("inspect", {"mode": "inspect"})
    add("baseline", {"mode": "baseline"})
    add(
        "seed_queued_extra",
        {
            "mode": "seed",
            "fixture": positive,
            "extra_fixture": "tests/scenarios/fixtures/m2/sensor/positive_other/manifest.json",
            "now": T0,
            "state": "queued",
            "reason_code": None,
            "updated_at": T0,
        },
    )
    return names


if __name__ == "__main__":
    print(build_m2_requests())
