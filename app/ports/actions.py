"""声明 M5 设备动作执行端口与受控执行错误，不暴露坐标、正文或路径。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.actions import ActionObservation, ACTION_KIND_SET

EXECUTION_ERROR_REASONS = frozenset(
    {"device_disconnected", "executor_interrupted"}
)


@dataclass(frozen=True)
class ActionPlan:
    """表示一次动作执行的只读参数；正文与坐标只在内存中传递。"""

    action_key: str
    action_kind: str
    target_key: str
    conversation_key: str
    body_text: str | None
    attachment_name: str | None
    target_label: str | None
    anchor_label: str | None
    ime_component: str | None
    ime_original: str | None
    irreversible_tap_index: int
    tap_points: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        """校验动作类型与可选文本形状，拒绝未知动作。"""
        if self.action_kind not in ACTION_KIND_SET:
            raise ValueError("action kind is invalid")
        for value in (
            self.action_key,
            self.target_key,
            self.conversation_key,
        ):
            if not isinstance(value, str) or not value:
                raise ValueError("action plan identity is invalid")
        for name in (
            "body_text",
            "attachment_name",
            "target_label",
            "anchor_label",
            "ime_component",
            "ime_original",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} is invalid")
        if type(self.irreversible_tap_index) is not int or self.irreversible_tap_index < 0:
            raise ValueError("irreversible tap index is invalid")
        if not isinstance(self.tap_points, tuple):
            raise ValueError("tap points are invalid")
        if self.tap_points and self.irreversible_tap_index >= len(self.tap_points):
            raise ValueError("tap index is out of range")
        for point in self.tap_points:
            if (
                not isinstance(point, tuple)
                or len(point) != 2
                or any(type(value) is not int or value < 0 for value in point)
            ):
                raise ValueError("tap point is invalid")


class ActionExecutionError(Exception):
    """表示执行器失败；除 click 事实外可携带已观察到的现场收敛状态。"""

    def __init__(
        self,
        reason_code: str,
        *,
        clicked: bool,
        scene_settled: bool = False,
        input_box_empty: bool = False,
        command_quiet: bool = False,
        ime_restored: bool = False,
    ) -> None:
        """保存固定原因、点击事实与保守/明确的收敛标志，不携带命令输出。"""
        if reason_code not in EXECUTION_ERROR_REASONS:
            raise ValueError("execution reason is invalid")
        for name, value in (
            ("clicked", clicked),
            ("scene_settled", scene_settled),
            ("input_box_empty", input_box_empty),
            ("command_quiet", command_quiet),
            ("ime_restored", ime_restored),
        ):
            if type(value) is not bool:
                raise ValueError(f"{name} is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.clicked = clicked
        self.scene_settled = scene_settled
        self.input_box_empty = input_box_empty
        self.command_quiet = command_quiet
        self.ime_restored = ime_restored


class DeviceActionPort(Protocol):
    """抽象只读当前 IME 与一次有界动作执行，不重试也不决定三态。"""

    def read_current_ime(self) -> str:
        """读取当前输入法标识；失败抛出 ActionExecutionError。"""
        ...

    def execute(self, plan: ActionPlan) -> ActionObservation:
        """执行一次动作并返回可信前后观察；不得自行宣告 performed。"""
        ...
