"""回放纯合成 M6 场景/决策刺激；不读取期望答案，也不代表真实设备或模型。"""

from __future__ import annotations

import uuid

from app.domain.models import (
    APP,
    ReadResult,
    UnreadRow,
    UnreadSignal,
    UnreadSnapshot,
    is_hash,
    is_uuid,
    parse_utc,
)
from app.domain.runtime import SafePoint, SceneCandidates, TaskCandidate
from app.ports.runtime import DecisionStimulus, SceneStep

_FORBIDDEN_KEYS = {"expected", "oracle", "verdict", "answer"}
_STEP_KEYS = {
    "wall_now",
    "monotonic_ms",
    "elapsed_seconds",
    "device_calls",
    "unread",
    "new_messages",
    "jobs",
    "cleanup",
    "unread_unknown",
    "unread_signal_without_target",
    "safe_point",
    "task_outcome",
    "reason_code",
    "unread_verification",
    "decision",
    "read_status",
    "read_reason",
    "in_progress",
}
_SAFE_KEYS = {
    "list_returned",
    "input_empty",
    "confirmation_closed",
    "no_inflight_command",
    "no_pending_action",
    "action_state_committed",
    "checkpoint_committed",
}


class StimulusRuntimeScene:
    """按顺序回放合成场景步骤；每个步骤都是输入刺激，不含断言结果。"""

    def __init__(self, spec: dict[str, object], *, profile_id: str) -> None:
        """严格校验场景刺激 schema，拒绝 expected/oracle 等答案键。"""
        if not isinstance(spec, dict) or set(spec) != {
            "schema_version",
            "batch_id",
            "steps",
        }:
            raise ValueError("runtime fixture is invalid")
        if spec["schema_version"] != 1:
            raise ValueError("runtime fixture is invalid")
        if not is_uuid(spec["batch_id"]) or not isinstance(spec["steps"], list):
            raise ValueError("runtime fixture is invalid")
        self.batch_id = str(spec["batch_id"])
        self.profile_id = profile_id
        self._steps = [self._parse_step(value) for value in spec["steps"]]
        self._index = 0

    def _parse_step(self, value: object) -> SceneStep:
        """把一条刺激对象解析为严格场景步骤。"""
        if not isinstance(value, dict) or set(value) - _STEP_KEYS:
            raise ValueError("runtime step is invalid")
        if _FORBIDDEN_KEYS & set(value):
            raise ValueError("runtime step is invalid")
        if not _STEP_KEYS <= set(value):
            raise ValueError("runtime step is invalid")
        wall_now = value["wall_now"]
        parse_utc(wall_now)
        for name in ("monotonic_ms", "elapsed_seconds", "device_calls"):
            item = value[name]
            if type(item) is not int or item < 0:
                raise ValueError("runtime step is invalid")
        safe_raw = value["safe_point"]
        if not isinstance(safe_raw, dict) or set(safe_raw) != _SAFE_KEYS:
            raise ValueError("runtime safe point is invalid")
        safe = SafePoint(**safe_raw)  # type: ignore[arg-type]
        candidates = SceneCandidates(
            unread=tuple(
                TaskCandidate("unread", key) for key in value["unread"]  # type: ignore[union-attr]
            ),
            new_messages=tuple(
                TaskCandidate("new_message", key) for key in value["new_messages"]  # type: ignore[union-attr]
            ),
            jobs=tuple(
                TaskCandidate("job", key) for key in value["jobs"]  # type: ignore[union-attr]
            ),
            cleanup=tuple(
                TaskCandidate("cleanup", key) for key in value["cleanup"]  # type: ignore[union-attr]
            ),
            unread_unknown=value["unread_unknown"],  # type: ignore[arg-type]
            unread_signal_without_target=value["unread_signal_without_target"],  # type: ignore[arg-type]
        )
        outcome = value["task_outcome"]
        if outcome not in {"done", "failed", "blocked"}:
            raise ValueError("runtime task outcome is invalid")
        reason = value["reason_code"]
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise ValueError("runtime reason is invalid")
        verification = value["unread_verification"]
        if verification not in {"none", "cleared", "still_unknown"}:
            raise ValueError("runtime verification is invalid")
        in_progress = value["in_progress"]
        if type(in_progress) is not bool:
            raise ValueError("runtime in-progress is invalid")
        decision = self._decision(value["decision"])
        read_result = self._read_result(value, wall_now)
        return SceneStep(
            wall_now=wall_now,
            monotonic_ms=value["monotonic_ms"],  # type: ignore[arg-type]
            elapsed_seconds=value["elapsed_seconds"],  # type: ignore[arg-type]
            device_calls=value["device_calls"],  # type: ignore[arg-type]
            candidates=candidates,
            safe_point=safe,
            task_outcome=outcome,
            reason_code=reason,
            unread_verification=verification,
            decision=decision,
            read_result=read_result,
            in_progress=in_progress,
        )

    def _decision(self, value: object) -> DecisionStimulus | None:
        """解析可选决策刺激；只接受枚举/摘要/本地回复。"""
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {
            "intent",
            "sub_intents",
            "policy_tier",
            "reply",
            "evidence_hash",
        }:
            raise ValueError("runtime decision is invalid")
        if (
            not isinstance(value["intent"], str)
            or not value["intent"]
            or not isinstance(value["sub_intents"], list)
            or any(not isinstance(item, str) or not item for item in value["sub_intents"])
            or value["policy_tier"] not in {"A", "B", "C"}
            or not isinstance(value["reply"], str)
            or not value["reply"]
            or not is_hash(value["evidence_hash"])
        ):
            raise ValueError("runtime decision is invalid")
        return DecisionStimulus(
            intent=str(value["intent"]),
            sub_intents=tuple(value["sub_intents"]),
            policy_tier=str(value["policy_tier"]),
            reply=str(value["reply"]),
            evidence_hash=str(value["evidence_hash"]),
        )

    def _read_result(self, value: dict[str, object], wall_now: str) -> ReadResult:
        """把读取刺激映射为三态结果；ok 仍要求完整合成 UnreadSnapshot。"""
        status = value["read_status"]
        reason = value["read_reason"]
        if status not in {"ok", "failed", "undetermined"}:
            raise ValueError("runtime read status is invalid")
        if status == "ok":
            if reason is not None:
                raise ValueError("runtime read reason is invalid")
            unknown = UnreadSignal("unknown", None, "missing_signal")
            snapshot = UnreadSnapshot(
                app=APP,
                profile_id=self.profile_id,
                coverage="viewport",
                signals={name: unknown for name in ("row_badge", "new_greeting", "tab_badge", "preview_delta")},
                rows=(),
                candidates=(),
                unread_state="unknown",
            )
            return ReadResult(
                status="ok",
                value=snapshot,
                reason_code=None,
                snapshot_id=str(uuid.uuid4()),
                observed_at=str(wall_now),
                source="fixture",
                confidence="low",
            )
        if not isinstance(reason, str) or not reason:
            raise ValueError("runtime read reason is invalid")
        if status == "failed":
            if reason not in {
                "device_disconnected",
                "device_unauthorized",
                "device_offline",
                "wrong_page",
                "invalid_xml",
                "invalid_fixture",
            }:
                raise ValueError("runtime read reason is invalid")
        elif reason not in {
            "unread_unverified",
            "identity_ambiguous",
            "incomplete_snapshot",
            "stale_snapshot",
            "uncalibrated_profile",
        }:
            raise ValueError("runtime read reason is invalid")
        return ReadResult(
            status=status,
            value=None,
            reason_code=reason,
            snapshot_id=None,
            observed_at=str(wall_now),
            source="fixture",
            confidence="low",
        )

    def next_step(self) -> SceneStep | None:
        """返回下一步刺激；没有更多步骤返回 None。"""
        if self._index >= len(self._steps):
            return None
        value = self._steps[self._index]
        self._index += 1
        return value


class StimulusDecisionSource:
    """按场景顺序返回决策刺激，不伪造真实模型调用或授权。"""

    def __init__(self) -> None:
        """创建空决策序列；每次调用前由场景步骤提供刺激。"""
        self.calls: list[dict[str, str]] = []
        self._pending: DecisionStimulus | None = None

    def set_pending(self, stimulus: DecisionStimulus | None) -> None:
        """登记当前步骤的决策刺激，供唯一决策端口消费。"""
        self._pending = stimulus

    def decide(
        self, *, task: TaskCandidate, input_hash: str, facts_hash: str
    ) -> DecisionStimulus:
        """返回当前刺激并记录输入身份；不得被当作真实模型成功。"""
        if self._pending is None:
            raise ValueError("decision stimulus is missing")
        self.calls.append(
            {
                "task_key": task.key,
                "input_hash": input_hash,
                "facts_hash": facts_hash,
            }
        )
        value = self._pending
        self._pending = None
        return value
