"""编排一次 M4b 决策：桥前重核、有界调用、桥后复验与固定诊断。"""

from __future__ import annotations

import time
from typing import Mapping

from app.domain.models import parse_utc
from app.domain.policy import (
    DECISION_REASON_CODES,
    DecisionError,
    PolicyPack,
    RenderedRequest,
    ReplyError,
    append_and_validate_reply,
    compute_base_hash,
    compute_pack_hash,
    effective_limits,
    parse_and_validate_pack,
    parse_decision,
    resolve_policy_tier,
    utc_now,
    v1_policy_tier,
    validate_decision,
    verify_rendered_request,
)
from app.ports.decision import (
    BRIDGE_REASON_CODES,
    BridgeError,
    DecisionBridgePort,
    DecisionLeasePort,
    DecisionSnapshotPort,
)
from app.process_log import ProcessLog, elapsed_ms

DECISION_RUN_REASON_CODES = frozenset(
    set(DECISION_REASON_CODES) | set(BRIDGE_REASON_CODES) | {"policy_changed"}
)
_BRIDGE_ERROR_TO_RUN = {"bridge_timeout": "decision_timeout"}


class DecisionRunError(Exception):
    """携带固定原因码、可信提交计数与可选字段诊断；不保存原异常或正文。"""

    def __init__(
        self,
        reason_code: str,
        *,
        submitted: bool,
        attempts: int,
        submissions: int,
        detail: str | None = None,
    ) -> None:
        """保存白名单原因与调用/提交计数；异常文本只用原因码。"""
        if reason_code not in DECISION_RUN_REASON_CODES:
            raise ValueError("decision run reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.submitted = bool(submitted)
        self.attempts = attempts
        self.submissions = submissions
        self.detail = detail


def _record_stage(
    process_log: ProcessLog | None,
    operation: str,
    result: str,
    *,
    started: float | None = None,
    reason_code: str | None = None,
    error_category: str | None = None,
) -> None:
    """通过唯一日志器记录 decision 阶段，不写正文、路径或异常原值。"""
    if process_log is None:
        return
    process_log.record(
        stage="decision",
        operation=operation,
        result=result,
        reason_code=reason_code,
        error_category=error_category,
        duration_ms=elapsed_ms(started) if started is not None else 0,
    )


def _lease_expiry(lease: DecisionLeasePort, now: str) -> str:
    """读取可信租约并检查 now 严格早于截止时间；无租约或到期即拒绝。"""
    try:
        value = lease.current_expiry()
    except Exception:
        raise DecisionRunError(
            "stale_decision", submitted=False, attempts=0, submissions=0
        ) from None
    if not isinstance(value, str):
        raise DecisionRunError(
            "stale_decision", submitted=False, attempts=0, submissions=0
        )
    try:
        if parse_utc(now) >= parse_utc(value):
            raise ValueError("expired")
    except ValueError:
        raise DecisionRunError(
            "stale_decision", submitted=False, attempts=0, submissions=0
        ) from None
    return value


def _reload_snapshot(snapshot: DecisionSnapshotPort) -> tuple[dict[str, str], dict[str, str]]:
    """重读当前 base 与四载荷；读取失败按包已变化受控拒绝。"""
    try:
        base_files, pack_files = snapshot.reload()
    except Exception:
        raise DecisionRunError(
            "policy_changed", submitted=False, attempts=0, submissions=0
        ) from None
    if not isinstance(base_files, dict) or not isinstance(pack_files, dict):
        raise DecisionRunError(
            "policy_changed", submitted=False, attempts=0, submissions=0
        )
    return base_files, pack_files


def _check_snapshot(
    rendered: RenderedRequest, base_files: Mapping[str, str], pack_files: Mapping[str, str]
) -> None:
    """按当前原文重算 base/pack 摘要，与渲染请求绑定值不同即拒绝。"""
    if (
        compute_base_hash(base_files) != rendered.base_hash
        or compute_pack_hash(pack_files) != rendered.pack_hash
    ):
        raise DecisionRunError(
            "policy_changed", submitted=False, attempts=0, submissions=0
        )


def decide(
    *,
    rendered: RenderedRequest,
    envelope: Mapping[str, object],
    base_files: Mapping[str, str],
    pack: PolicyPack,
    bridge: DecisionBridgePort,
    snapshot: DecisionSnapshotPort,
    lease: DecisionLeasePort,
    now: str,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """执行一次生产决策路径并返回不含正文的 Decision、复验回复与审计摘要。"""
    attempts = 0
    submissions = 0
    stage_started = time.perf_counter()
    _record_stage(process_log, "verify", "started")
    try:
        verify_rendered_request(rendered, base_files, pack, envelope, now=now)
        current_base, current_pack_files = _reload_snapshot(snapshot)
        _check_snapshot(rendered, current_base, current_pack_files)
        expiry_before = _lease_expiry(lease, now)
    except DecisionError as error:
        _record_stage(
            process_log,
            "verify",
            "failed",
            started=stage_started,
            reason_code=error.reason_code,
            error_category="decision",
        )
        raise DecisionRunError(
            error.reason_code, submitted=False, attempts=0, submissions=0
        ) from None
    except DecisionRunError as error:
        _record_stage(
            process_log,
            "verify",
            "failed",
            started=stage_started,
            reason_code=error.reason_code,
            error_category="decision" if error.reason_code != "policy_changed" else "policy",
        )
        raise
    _record_stage(process_log, "verify", "ok", started=stage_started)
    del expiry_before

    text: str | None = None
    while attempts < 2:
        attempts += 1
        call_started = time.perf_counter()
        _record_stage(process_log, "bridge", "started")
        try:
            text = bridge.complete(
                system_message=rendered.system_message,
                developer_message=rendered.developer_message,
                user_message=rendered.user_message,
                echo=rendered.protocol,
            )
        except BridgeError as error:
            submissions += 1 if error.submitted else 0
            reason = _BRIDGE_ERROR_TO_RUN.get(error.reason_code, error.reason_code)
            _record_stage(
                process_log,
                "bridge",
                "failed",
                started=call_started,
                reason_code=reason,
                error_category="bridge",
            )
            if error.retryable and attempts == 1:
                continue
            raise DecisionRunError(
                reason,
                submitted=error.submitted,
                attempts=attempts,
                submissions=submissions,
            ) from None
        submissions += 1
        _record_stage(process_log, "bridge", "ok", started=call_started)
        break
    assert text is not None

    validate_started = time.perf_counter()
    _record_stage(process_log, "validate", "started")
    try:
        after_base, after_pack_files = _reload_snapshot(snapshot)
        _check_snapshot(rendered, after_base, after_pack_files)
        expiry_after = _lease_expiry(lease, now)
        decision = parse_decision(text)
        validate_decision(
            decision,
            envelope,
            now=now,
            lease_expires_at=expiry_after,
        )
        canonical_pack, issues = parse_and_validate_pack(after_pack_files)
        if issues or canonical_pack is None:
            raise DecisionRunError(
                "policy_changed",
                submitted=True,
                attempts=attempts,
                submissions=submissions,
            )
        limits = effective_limits(canonical_pack)
        try:
            reply = append_and_validate_reply(
                decision["reply"],
                disclosure=str(limits["disclosure"]),
                max_chars=int(limits["max_chars"]),
                never_say=limits["never_say"],
                extra_topics=limits["extra_topics"],
                allowed_facts=envelope["allowed_facts"],
            )
        except ReplyError as error:
            raise DecisionRunError(
                "invalid_decision",
                submitted=True,
                attempts=attempts,
                submissions=submissions,
                detail=error.code,
            ) from None
        resolved_tier = resolve_policy_tier(
            canonical_pack,
            str(decision["intent"]),
            decision["sub_intents"],
            decision["escalate"],
            v1_policy_tier(),
        )
    except DecisionError as error:
        _record_stage(
            process_log,
            "validate",
            "failed",
            started=validate_started,
            reason_code=error.reason_code,
            error_category="decision",
        )
        raise DecisionRunError(
            error.reason_code,
            submitted=True,
            attempts=attempts,
            submissions=submissions,
        ) from None
    except DecisionRunError as error:
        if error.attempts == 0:
            error.attempts = attempts
            error.submissions = submissions
            error.submitted = True
        _record_stage(
            process_log,
            "validate",
            "failed",
            started=validate_started,
            reason_code=error.reason_code,
            error_category="policy" if error.reason_code == "policy_changed" else "decision",
        )
        raise
    _record_stage(process_log, "validate", "ok", started=validate_started)

    descriptor = bridge.descriptor()
    return {
        "decision": decision,
        "reply": reply,
        "resolved_tier": resolved_tier,
        "audit": {
            "base_version": rendered.base_version,
            "base_hash": rendered.base_hash,
            "pack_id": rendered.pack_id,
            "pack_hash": rendered.pack_hash,
            "prompt_hash": rendered.prompt_hash,
            "input_hash": rendered.input_hash,
            "rendered_at": utc_now(),
            "provider_id": descriptor.provider_id,
            "model_id": descriptor.model_id,
            "model_config_hash": descriptor.model_config_hash,
            "bridge": descriptor.bridge,
            "simulated": descriptor.simulated,
            "bridge_attempts": attempts,
            "bridge_submissions": submissions,
        },
    }
