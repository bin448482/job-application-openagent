"""提供 M5 离线合成的脚本化设备动作执行器，不启动子进程或连接真实设备。"""

from __future__ import annotations

from app.domain.actions import ActionObservation, is_action_key
from app.domain.models import is_uuid, parse_utc
from app.ports.actions import ActionExecutionError, ActionPlan, DeviceActionPort

_OBSERVATION_FIELDS = (
    "before_snapshot_id",
    "after_snapshot_id",
    "observed_at",
    "clicked",
    "error",
    "scene_settled",
    "command_quiet",
    "confirmation_open",
    "input_box_empty",
    "ime_original",
    "ime_current",
    "body_readback_matches",
    "echo_count_before",
    "echo_count_after",
    "attachment_name_matched",
    "attachment_request_observed",
    "delivery_observed",
    "contact_slot_available",
    "contact_slot_status",
    "identity_unique",
    "list_complete",
    "protections_clear",
    "target_present_before",
    "target_present_after",
    "anchor_present_after",
)


class ScriptedActionExecutor:
    """按显式合成脚本回放观察；不是 Evidence 回存器，也不代表真实设备。"""

    def __init__(self, spec: dict[str, object]) -> None:
        """保存合成本机 IME 与观察脚本；构造阶段不执行任何设备命令。"""
        if not isinstance(spec, dict):
            raise ValueError("action fixture is invalid")
        allowed = {"current_ime", "observation", "raise"}
        if set(spec) - allowed:
            raise ValueError("action fixture has unknown keys")
        current_ime = spec.get("current_ime", "synthetic-ime-default")
        if not isinstance(current_ime, str) or not current_ime:
            raise ValueError("current ime is invalid")
        self._current_ime = current_ime
        self._raise = spec.get("raise")
        observation = spec.get("observation")
        if observation is not None and not isinstance(observation, dict):
            raise ValueError("observation is invalid")
        self._observation = observation
        if self._raise is not None:
            allowed_raise = {
                "reason_code",
                "clicked",
                "scene_settled",
                "input_box_empty",
                "command_quiet",
                "ime_restored",
            }
            if (
                not isinstance(self._raise, dict)
                or not {"reason_code", "clicked"} <= set(self._raise)
                or set(self._raise) - allowed_raise
                or self._raise.get("reason_code")
                not in {"device_disconnected", "executor_interrupted"}
                or type(self._raise.get("clicked")) is not bool
            ):
                raise ValueError("raise directive is invalid")
            for name in (
                "scene_settled",
                "input_box_empty",
                "command_quiet",
                "ime_restored",
            ):
                if name in self._raise and type(self._raise[name]) is not bool:
                    raise ValueError("raise directive is invalid")

    def read_current_ime(self, *, before_command=None) -> str:
        """返回合成当前 IME；不执行设备命令。"""
        if before_command is not None:
            try:
                before_command()
            except Exception:
                raise ActionExecutionError("executor_interrupted", clicked=True) from None
        return self._current_ime

    def execute(self, plan: ActionPlan, *, before_command=None) -> ActionObservation:
        """按脚本返回一次观察；脚本声明失败时抛出固定执行错误。"""
        if before_command is not None:
            try:
                before_command()
            except Exception:
                raise ActionExecutionError("executor_interrupted", clicked=True) from None
        if self._raise is not None:
            clicked = bool(self._raise["clicked"])
            if clicked:
                defaults = {
                    "scene_settled": False,
                    "input_box_empty": False,
                    "command_quiet": False,
                    "ime_restored": False,
                }
            else:
                defaults = {
                    "scene_settled": True,
                    "input_box_empty": True,
                    "command_quiet": True,
                    "ime_restored": True,
                }
            flags = {
                name: bool(self._raise.get(name, defaults[name]))
                for name in defaults
            }
            raise ActionExecutionError(
                str(self._raise["reason_code"]), clicked=clicked, **flags
            )
        if self._observation is None:
            raise ActionExecutionError("executor_interrupted", clicked=True)
        if not is_action_key(plan.action_kind, plan.action_key):
            raise ActionExecutionError("executor_interrupted", clicked=False)
        data = dict(self._observation)
        expected_fields = set(_OBSERVATION_FIELDS)
        if plan.action_kind == "apply_job":
            expected_fields |= {"apply_available_before", "apply_completed_after"}
        if set(data) != expected_fields:
            raise ValueError("observation fields are invalid")
        for name in ("before_snapshot_id", "after_snapshot_id"):
            value = data[name]
            if value is not None and not is_uuid(value):
                raise ValueError(f"{name} is invalid")
        parse_utc(data["observed_at"])
        data["observed_at"] = _utc_now()
        if data["ime_original"] is None:
            data["ime_original"] = self._current_ime
        payload = {name: data[name] for name in _OBSERVATION_FIELDS}
        if plan.action_kind == "apply_job":
            payload["apply_available_before"] = data["apply_available_before"]
            payload["apply_completed_after"] = data["apply_completed_after"]
        return ActionObservation(
            action_key=plan.action_key,
            action_kind=plan.action_kind,
            **payload,
        )


def _utc_now() -> str:
    """返回本次合成观察的毫秒 UTC 时间。"""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
class CommandLedgerActionExecutor:
    """用真实 CommandIdentityStore 包裹合成执行器，验证动作命令账本边界。"""

    def __init__(
        self,
        inner: ScriptedActionExecutor,
        command_store: object,
        *,
        commands: list[str] | None = None,
    ) -> None:
        """保存合成执行器、真实账本与可选命令记录。"""
        self._inner = inner
        self._store = command_store
        self._walked = commands if commands is not None else []
        self._bound: str | None = None
        self._pid = __import__("os").getpid()

    def bind_action(self, action_key: str) -> None:
        """在首个设备命令前绑定动作键。"""
        if not isinstance(action_key, str) or not action_key:
            raise ValueError("action key is invalid")
        self._bound = action_key

    def read_current_ime(self, *, before_command=None) -> str:
        """返回合成 IME；动作绑定后必须已有账本。"""
        if before_command is not None:
            before_command()
        if self._bound is None or self._store is None:
            raise __import__("app.ports.actions", fromlist=["ActionExecutionError"]).ActionExecutionError(
                "executor_interrupted", clicked=False
            )
        return self._inner.read_current_ime()

    def execute(self, plan, *, before_command=None):
        """实际记录 begin→PID→complete 后回放合成观察。"""
        from app.ports.actions import ActionExecutionError

        if self._bound != plan.action_key or self._store is None:
            raise ActionExecutionError("executor_interrupted", clicked=False)
        if before_command is not None:
            try:
                before_command()
            except Exception:
                raise ActionExecutionError("executor_interrupted", clicked=False) from None
        self._store.mark_started(plan.action_key)
        self._store.record(plan.action_key, self._pid)
        self._walked.append(plan.action_key)
        self._store.finish_family(
            plan.action_key,
            self._pid,
            lambda _pid: [],
            bounded=True,
            proven=True,
        )
        return self._inner.execute(plan, before_command=before_command)
