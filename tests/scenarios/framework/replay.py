"""用虚拟时钟重放固定事件序列，并原样记录三个调度时刻。

这里只做测试驱动的时序展开：不在测试侧实现调度决策，也不从缺失事件补造
arrival、first_observed 或 handling_started。脚本起点、事件时间和时刻顺序
必须全部满足单调约束，否则拒绝重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

REQUIRED_EVENT_KEYS = {"seq", "virtual_ms", "event"}
OPTIONAL_EVENT_KEYS = {"arrival_at", "first_observed_at", "handling_started_at", "note"}
TIME_KEYS = ("arrival_at", "first_observed_at", "handling_started_at")


class ReplayError(ValueError):
    """表示事件脚本本身不满足确定性重放约束。"""


@dataclass
class VirtualClock:
    """单调递增的虚拟时钟，禁止回拨或依赖宿主真实时间。"""

    start_ms: int = 0
    _now_ms: int = field(init=False)

    def __post_init__(self) -> None:
        """校验起始值并初始化当前毫秒。"""
        _require_non_negative_int(self.start_ms, "start_ms")
        self._now_ms = self.start_ms

    @property
    def now_ms(self) -> int:
        """返回当前虚拟毫秒，供事件注入与耗时差值使用。"""
        return self._now_ms

    def advance_to(self, virtual_ms: int) -> int:
        """推进到指定虚拟时刻；相同值允许用于同刻多事件。"""
        _require_non_negative_int(virtual_ms, "virtual_ms")
        if virtual_ms < self._now_ms:
            raise ReplayError("虚拟时钟不得回拨")
        self._now_ms = virtual_ms
        return self._now_ms

    def now_iso(self) -> str:
        """把虚拟毫秒转为契约要求的 UTC 毫秒时间字符串。"""
        moment = datetime.fromtimestamp(self._now_ms / 1000, tz=timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self._now_ms % 1000:03d}Z"

    def elapsed_ms(self, since_ms: int) -> int:
        """返回自给定虚拟时刻起的单调时钟差值。"""
        if since_ms > self._now_ms:
            raise ReplayError("起算时刻晚于当前虚拟时刻")
        return self._now_ms - since_ms


def _require_non_negative_int(value: Any, label: str) -> None:
    """要求非 bool 的非负整数，避免 True 冒充 1。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayError(f"{label} 必须是非负整数且不得为 bool")


def validate_script(script: dict[str, Any], start_ms: int | None = None) -> list[str]:
    """检查脚本起点、序号、时间单调和时刻字段类型。"""
    errors: list[str] = []
    declared_start = script.get("clock_start_ms", 0)
    if isinstance(declared_start, bool) or not isinstance(declared_start, int) or declared_start < 0:
        errors.append("clock_start_ms 必须是非负整数且不得为 bool")
        declared_start = 0
    if start_ms is not None and start_ms != declared_start:
        errors.append("注入时钟起点与脚本 clock_start_ms 不一致")
    events = script.get("events")
    if not isinstance(events, list) or not events:
        return errors + ["events 必须是非空数组"]
    previous_seq = 0
    previous_ms = -1
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{index}] 必须是对象")
            continue
        keys = set(event)
        if not REQUIRED_EVENT_KEYS.issubset(keys):
            errors.append(f"events[{index}] 缺少 seq/virtual_ms/event")
            continue
        extra = keys - REQUIRED_EVENT_KEYS - OPTIONAL_EVENT_KEYS
        if extra:
            errors.append(f"events[{index}] 含未知键 {sorted(extra)}")
        seq = event.get("seq")
        virtual_ms = event.get("virtual_ms")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= previous_seq:
            errors.append(f"events[{index}].seq 必须严格递增")
        else:
            previous_seq = seq
        if isinstance(virtual_ms, bool) or not isinstance(virtual_ms, int) or virtual_ms < previous_ms:
            errors.append(f"events[{index}].virtual_ms 必须非负且不倒退")
        elif virtual_ms < declared_start:
            errors.append(f"events[{index}].virtual_ms 早于脚本起点")
        else:
            previous_ms = virtual_ms
        if not isinstance(event.get("event"), str) or not event["event"]:
            errors.append(f"events[{index}].event 必须非空")
        for key in TIME_KEYS:
            if key in event:
                value = event[key]
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    errors.append(f"events[{index}].{key} 必须是虚拟毫秒整数或 null")
                elif value is not None and value < declared_start:
                    errors.append(f"events[{index}].{key} 早于脚本起点")
        if "note" in event and not isinstance(event["note"], str):
            errors.append(f"events[{index}].note 必须是字符串")
    return errors


def replay_events(script: dict[str, Any], clock: VirtualClock | None = None) -> dict[str, Any]:
    """按脚本顺序重放事件，返回时钟轨迹与未补造的三个时刻。

    同一 virtual_ms 的多个事件按 seq 执行。输出只回显脚本显式给出的时刻，
    缺失值为 null，绝不从相邻事件推断。注入时钟必须从脚本声明的起点开始，
    且当前时刻不得晚于首个事件。
    """
    declared_start = script.get("clock_start_ms", 0)
    errors = validate_script(script)
    if errors:
        raise ReplayError("; ".join(errors))
    active_clock = clock or VirtualClock(declared_start)
    if active_clock.now_ms != declared_start:
        raise ReplayError("注入时钟当前时刻必须等于脚本起点")
    timeline: list[dict[str, Any]] = []
    for event in sorted(script["events"], key=lambda item: (item["virtual_ms"], item["seq"])):
        now_ms = active_clock.advance_to(event["virtual_ms"])
        record = {
            "seq": event["seq"],
            "virtual_ms": now_ms,
            "event": event["event"],
            "note": event.get("note"),
            "arrival_at": event.get("arrival_at"),
            "first_observed_at": event.get("first_observed_at"),
            "handling_started_at": event.get("handling_started_at"),
        }
        timeline.append(record)
    return {
        "script_id": script.get("script_id"),
        "clock_start_ms": declared_start,
        "clock_end_ms": active_clock.now_ms,
        "timeline": timeline,
    }


def timing_invariants(timeline: dict[str, Any]) -> dict[str, Any]:
    """从重放结果提取独立时间判据，不代替产品调度结论。"""
    records = timeline["timeline"]
    arrival = [item["arrival_at"] for item in records if item["arrival_at"] is not None]
    observed = [item["first_observed_at"] for item in records if item["first_observed_at"] is not None]
    handling = [item["handling_started_at"] for item in records if item["handling_started_at"] is not None]
    first_observed = min(observed) if observed else None
    first_handling = min(handling) if handling else None
    return {
        "arrival_present": bool(arrival),
        "first_observed_present": first_observed is not None,
        "handling_started_present": first_handling is not None,
        "first_arrival_at": min(arrival) if arrival else None,
        "first_observed_at": first_observed,
        "first_handling_started_at": first_handling,
        "waited_ms": (
            first_handling - first_observed
            if first_observed is not None and first_handling is not None
            else None
        ),
        "arrival_before_observation": (
            min(arrival) <= first_observed
            if arrival and first_observed is not None
            else None
        ),
    }
