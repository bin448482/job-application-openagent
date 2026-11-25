"""回放纯合成 M6 场景/决策刺激；不读取期望答案，也不代表真实设备或模型。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.adapters.boss.read import load_and_read_fixture
from app.domain.models import (
    APP,
    ReadResult,
    UnreadRow,
    UnreadSignal,
    UnreadSnapshot,
    canonical_hash,
    is_hash,
    is_uuid,
    parse_utc,
)
from app.domain.runtime import SafePoint, SceneCandidates, TaskCandidate
from app.ports.decision import BridgeDescriptor, DecisionBridgePort
from app.ports.runtime import DecisionStimulus, RawScene, SceneStep

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
    "read_fixture",
    "conversation_fixture",
    "job_fixture",
    "raw_scene",
    "bridge_response",
}
_RAW_SCENE_KEYS = {
    "input_text",
    "confirmation_title",
    "inflight_commands",
    "ime_current",
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
        decision = self._decision(value.get("decision")) or self._bridge_decision(
            value.get("bridge_response")
        )
        raw_scene = self._raw_scene(value.get("raw_scene"))
        read_fixture = value.get("read_fixture")
        if read_fixture is not None:
            if not isinstance(read_fixture, str) or not read_fixture:
                raise ValueError("runtime read fixture is invalid")
        else:
            if value["jobs"]:
                read_fixture = "tests/fixtures/read/job_list.json"
            elif value["unread"] or value["new_messages"]:
                read_fixture = "tests/fixtures/read/unread.json"
            else:
                read_fixture = "tests/fixtures/read/page.json"
        conversation_fixture = value.get("conversation_fixture")
        if conversation_fixture is None and value["unread"]:
            conversation_fixture = "tests/fixtures/read/conversation.json"
        job_fixture = value.get("job_fixture")
        for name, item in (
            ("conversation_fixture", conversation_fixture),
            ("job_fixture", job_fixture),
        ):
            if item is not None and (not isinstance(item, str) or not item):
                raise ValueError(f"{name} is invalid")
        read_result = self._read_result(value, wall_now)
        return SceneStep(
            read_fixture=read_fixture,
            conversation_fixture=conversation_fixture,
            job_fixture=job_fixture,
            raw_scene=raw_scene,
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

    def _raw_scene(self, value: object) -> RawScene:
        """解析原始现场刺激；缺省是空输入、无确认框、零在途命令。"""
        if value is None:
            return RawScene("", None, 0)
        if not isinstance(value, dict) or set(value) != _RAW_SCENE_KEYS:
            raise ValueError("runtime raw scene is invalid")
        return RawScene(
            input_text=value["input_text"],  # type: ignore[arg-type]
            confirmation_title=value["confirmation_title"],  # type: ignore[arg-type]
            inflight_commands=value["inflight_commands"],  # type: ignore[arg-type]
            ime_current=value["ime_current"],  # type: ignore[arg-type]
        )

    def _bridge_decision(self, value: object) -> DecisionStimulus | None:
        """解析合成 bridge 响应字段；任务/nonce/摘要由 echo 回填。"""
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {
            "intent",
            "sub_intents",
            "reply",
            "escalate",
        }:
            raise ValueError("runtime bridge response is invalid")
        if not {"intent", "sub_intents", "reply"} <= set(value):
            raise ValueError("runtime bridge response is invalid")
        sub_intents = value["sub_intents"]
        if not isinstance(sub_intents, list):
            raise ValueError("runtime bridge response is invalid")
        return DecisionStimulus(
            intent=str(value["intent"]),
            sub_intents=tuple(sub_intents),
            reply=str(value["reply"]),
            escalate=None if value.get("escalate") is None else str(value["escalate"]),
        )

    def _decision(self, value: object) -> DecisionStimulus | None:
        """解析旧版决策刺激；只当 bridge 响应字段，不当作已校验 Decision。"""
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {
            "intent",
            "sub_intents",
            "policy_tier",
            "reply",
            "evidence_hash",
            "escalate",
        }:
            raise ValueError("runtime decision is invalid")
        if not {
            "intent",
            "sub_intents",
            "policy_tier",
            "reply",
            "evidence_hash",
        } <= set(value):
            raise ValueError("runtime decision is invalid")
        if (
            not isinstance(value["intent"], str)
            or not isinstance(value["sub_intents"], list)
            or any(not isinstance(item, str) or not item for item in value["sub_intents"])
            or value["policy_tier"] not in {"A", "B", "C"}
            or not isinstance(value["reply"], str)
            or not is_hash(value["evidence_hash"])
        ):
            raise ValueError("runtime decision is invalid")
        return DecisionStimulus(
            intent=str(value["intent"]),
            sub_intents=tuple(value["sub_intents"]),
            reply=str(value["reply"]),
            escalate=None if value.get("escalate") is None else str(value["escalate"]),
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

class FixtureM3Read:
    """按项目内相对 M3 manifest 调用真实解析/门禁，供运行组合根注入。"""

    def __init__(self, project_root: Path) -> None:
        """保存项目根；路径解析拒绝绝对路径与越界。"""
        self._root = Path(project_root).resolve()
        self._calls = 0

    def read(self, relative: str) -> ReadResult:
        """读取并解析 M3 fixture；路径/格式错误映射固定失败。"""
        if not isinstance(relative, str) or not relative:
            raise ValueError("read fixture is invalid")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("read fixture is invalid")
        path = (self._root / candidate).resolve()
        if self._root not in path.parents and path != self._root:
            raise ValueError("read fixture is invalid")
        if not path.is_file():
            raise ValueError("read fixture is invalid")
        self._calls += 1
        return load_and_read_fixture(path, str(uuid.uuid4()))

    def take_device_calls(self) -> int:
        """返回实际执行的 M3 读取次数并清零。"""
        calls = self._calls
        self._calls = 0
        return calls


class SyntheticDecisionBridge:
    """返回一条合成 bridge transport 响应，由唯一 decide 完成全部校验。"""

    def __init__(
        self,
        stimulus: DecisionStimulus | None,
        *,
        provider_id: str,
        model_id: str,
        model_config_hash: str,
    ) -> None:
        """保存响应与固定描述符；不读取凭据或网络。"""
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("provider is invalid")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model is invalid")
        if not is_hash(model_config_hash):
            raise ValueError("model config is invalid")
        self._stimulus = stimulus
        self._descriptor = BridgeDescriptor(
            provider_id=provider_id,
            model_id=model_id,
            model_config_hash=model_config_hash,
            bridge="bridge_b",
            simulated=True,
        )

    def descriptor(self) -> BridgeDescriptor:
        """返回合成桥身份，供 decide 审计。"""
        return self._descriptor

    def complete(
        self,
        *,
        system_message: str,
        developer_message: str,
        user_message: str,
        echo: dict[str, object],
    ) -> str:
        """按固定 echo 回填 task/nonce/input_hash 并返回原始 JSON 文本。"""
        del system_message, developer_message
        if self._stimulus is None:
            raise ValueError("decision stimulus is missing")
        try:
            payload = json.loads(user_message)
        except (TypeError, ValueError):
            payload = {}
        inbound = payload.get("inbound") if isinstance(payload, dict) else None
        evidence = inbound[:2] if isinstance(inbound, str) and inbound else "ok"
        response = {
            "schema_version": 1,
            "task_id": echo["task_id"],
            "nonce": echo["nonce"],
            "input_hash": echo["input_hash"],
            "intent": self._stimulus.intent,
            "sub_intents": list(self._stimulus.sub_intents),
            "reply": self._stimulus.reply,
            "evidence": evidence,
            "escalate": self._stimulus.escalate,
        }
        return json.dumps(response, ensure_ascii=False)
