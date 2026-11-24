"""编排 M4b `policy eval`：实包、同一桥路径、独立断言、留出事务与本机报告。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from app.domain.eval import (
    SUITE_NAMES,
    begin_holdout_access,
    evaluate_assertions,
    finish_holdout_access,
    frozen_holdout_batch,
    project_case,
    register_holdout_batch,
    retire_holdout_batch,
    sample_fingerprint,
    suite_fingerprints,
    suite_hash,
    validate_holdout_attestation,
    validate_holdout_registry,
    validate_suite,
)
from app.domain.models import add_seconds, canonical_hash, format_utc, parse_utc
from app.domain.policy import (
    BASE_VERSION,
    DecisionError,
    PolicyIssue,
    build_envelope,
    compute_base_hash,
    render_request,
    utc_now,
)
from app.ports.decision import BRIDGE_PROVIDERS, BridgeDescriptor, DecisionBridgePort
from app.ports.eval import EvalStorePort
from app.ports.policy import PolicyError, PolicyRepositoryPort
from app.process_log import ProcessLog, elapsed_ms
from app.usecases.decision import DecisionRunError, decide
from app.usecases.policy import load_active_pack


def _issue(file: str | None, code: str) -> PolicyIssue:
    """构造评测用例层固定诊断，不回显输入或模型输出。"""
    return PolicyIssue(file=file, field=None, line=None, code=code)


class RepositorySnapshot:
    """每次重读当前 base 与活动包，供桥前/桥后摘要复核。"""

    def __init__(self, repo: PolicyRepositoryPort, pack_rel: str) -> None:
        """保存仓库端口与已规范化的活动包路径。"""
        self._repo = repo
        self._pack_rel = pack_rel

    def reload(self) -> tuple[dict[str, str], dict[str, str]]:
        """读取当前 base 与四载荷原文；读取失败由决策用例按 policy_changed 处理。"""
        return self._repo.read_base(), self._repo.read_pack(self._pack_rel)


class EvaluationLease:
    """离线评测专用的程序侧合成租约；不证明 M1/M4c/M5 的真实租约或授权。"""

    def __init__(self, expires_at: str) -> None:
        """保存由评测程序生成的固定截止时间。"""
        self._expires_at = expires_at

    def current_expiry(self) -> str | None:
        """返回评测合成租约的规范截止时间。"""
        return self._expires_at


class LogicalAdvancingClock:
    """以 case_now 为逻辑起点并叠加真实单调耗时，供离线 eval 复验流逝。"""

    def __init__(self, base: str) -> None:
        """保存规范逻辑起点与实际启动的单调时钟。"""
        self._base = parse_utc(base)
        self._started = time.monotonic()

    def now(self) -> str:
        """返回逻辑起点加真实单调耗时后的毫秒 UTC 时间。"""
        elapsed = timedelta(seconds=time.monotonic() - self._started)
        return format_utc(self._base + elapsed)


def _descriptor(bridge: DecisionBridgePort) -> BridgeDescriptor:
    """校验桥描述符的 provider/model/config 形状，不读取凭据。"""
    descriptor = bridge.descriptor()
    if descriptor.provider_id not in BRIDGE_PROVIDERS:
        raise PolicyError("invalid_provider", [_issue(None, "vocabulary")])
    if not isinstance(descriptor.model_id, str) or not descriptor.model_id:
        raise PolicyError("invalid_provider", [_issue(None, "format")])
    if (
        not isinstance(descriptor.model_config_hash, str)
        or len(descriptor.model_config_hash) != 64
        or any(char not in "0123456789abcdef" for char in descriptor.model_config_hash)
    ):
        raise PolicyError("invalid_provider", [_issue(None, "format")])
    if descriptor.bridge not in ("bridge_a", "bridge_b"):
        raise PolicyError("invalid_provider", [_issue(None, "vocabulary")])
    return descriptor


def _buckets(suite_name: str, total: int, passed: int, failed: int) -> dict[str, object]:
    """按公开/回归/留出独立分桶；未运行分桶明确为未测而非 100%。"""
    buckets: dict[str, object] = {
        name: {
            "status": "not_measured",
            "total": None,
            "passed": 0,
            "failed": 0,
        }
        for name in SUITE_NAMES
    }
    measured = total > 0
    buckets[suite_name] = {
        "status": "measured" if measured else "not_measured",
        "total": total,
        "passed": passed,
        "failed": failed,
    }
    return buckets


@contextmanager
def _eval_stage(
    process_log: ProcessLog | None, operation: str
) -> Iterator[None]:
    """在 eval 实际边界记录 started→ok/failed，未知异常不泄露原文。"""
    if process_log is not None:
        process_log.record(stage="eval", operation=operation, result="started")
    started = time.perf_counter()
    try:
        yield
    except PolicyError as error:
        if process_log is not None:
            process_log.record(
                stage="eval",
                operation=operation,
                result="failed",
                reason_code=error.reason_code,
                error_category="eval",
                duration_ms=elapsed_ms(started),
            )
        raise
    except Exception:
        if process_log is not None:
            process_log.record(
                stage="eval",
                operation=operation,
                result="failed",
                error_category="internal",
                duration_ms=elapsed_ms(started),
            )
        raise
    else:
        if process_log is not None:
            process_log.record(
                stage="eval",
                operation=operation,
                result="ok",
                duration_ms=elapsed_ms(started),
            )


def _case_result(
    case_index: int,
    case_id: str,
    status: str,
    *,
    reason_code: str | None = None,
    checks: list[str] | None = None,
    detail: str | None = None,
    input_hash: str | None = None,
    prompt_hash: str | None = None,
    attempts: int = 0,
    submissions: int = 0,
) -> dict[str, object]:
    """组装单例结果，只留安全序号、摘要与固定诊断，不保存回复正文。"""
    result: dict[str, object] = {
        "case_index": case_index,
        "case_id": case_id,
        "status": status,
        "reason_code": reason_code,
        "checks": list(checks or []),
        "input_hash": input_hash,
        "prompt_hash": prompt_hash,
        "bridge_attempts": attempts,
        "bridge_submissions": submissions,
    }
    if detail is not None:
        result["detail"] = detail
    return result


def _base_report(
    *,
    run_id: str,
    created_at: str,
    correlation_id: str | None,
    suite_name: str,
    suite_version: str | None,
    digest: str | None,
    case_count: int,
    normalized: str,
    pack_id: str,
    base_hash: str | None,
    pack_hash: str,
    descriptor: BridgeDescriptor,
) -> dict[str, object]:
    """构造报告固定骨架；授权与验收由实际执行路径填写。"""
    return {
        "schema_version": 1,
        "run_id": run_id,
        "correlation_id": correlation_id,
        "created_at": created_at,
        "suite": {
            "name": suite_name,
            "version": suite_version,
            "hash": digest,
            "case_count": case_count,
        },
        "pack": {
            "path": normalized,
            "profile_id": pack_id,
            "base_version": BASE_VERSION,
            "base_hash": base_hash,
            "pack_hash": pack_hash,
        },
        "provider": descriptor.to_dict(),
        "buckets": _buckets(suite_name, case_count, 0, 0),
        "cases": [],
        "acceptance": None,
        "authorization": {
            "confirmed": False,
            "scope": "not_applicable",
            "reason": "not_measured",
        },
        "limitations": [],
    }


def _write_report(
    eval_store: EvalStorePort,
    run_id: str,
    report: dict[str, object],
    process_log: ProcessLog | None,
) -> str:
    """在实际写报告边界记录 started→ok/failed，返回本机相对路径。"""
    with _eval_stage(process_log, "report"):
        return eval_store.write_report(run_id, report)


def _attestation_digest(attestation: dict[str, object]) -> str:
    """计算无上传合同的规范摘要，只用于本机报告绑定。"""
    return canonical_hash(attestation)


def _finish_holdout(
    eval_store: EvalStorePort,
    digest: str,
    attempt_id: str,
    *,
    at: str,
    outcome: str,
    passed: int | None,
    total: int | None,
    process_log: ProcessLog | None,
) -> dict[str, object]:
    """在锁内收尾同一访问意向；返回完成记录。"""
    def mutate(registry: dict[str, object]) -> object:
        """在锁内完成同一访问意向的失败收尾。"""
        validate_holdout_registry(registry)
        batch = frozen_holdout_batch(registry, digest)
        if batch is None:
            raise ValueError("holdout batch is missing")
        return finish_holdout_access(
            registry,
            batch,
            attempt_id=attempt_id,
            at=at,
            outcome=outcome,
            passed=passed,
            total=total,
        )

    with _eval_stage(process_log, "holdout"):
        try:
            return eval_store.update_holdout_registry(mutate)  # type: ignore[return-value]
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "conflict")]) from None


def eval_pack(
    repo: PolicyRepositoryPort,
    eval_store: EvalStorePort,
    pack_rel: str,
    suite_name: str,
    bridge: DecisionBridgePort,
    authorize: Callable[[dict[str, object]], bool],
    *,
    now: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """加载当前包与套件，经同一决策桥逐例运行并写本机报告。"""
    if suite_name not in SUITE_NAMES:
        raise PolicyError("invalid_input", [_issue(None, "vocabulary")])
    packet = load_active_pack(repo, pack_rel, process_log=process_log)
    normalized, base_files, _files, _guides, pack = packet
    descriptor = _descriptor(bridge)
    run_id = uuid.uuid4().hex
    created_at = now or utc_now()
    correlation_id = process_log.correlation_id if process_log is not None else None
    base_hash = compute_base_hash(base_files)

    with _eval_stage(process_log, "suite"):
        try:
            raw_suite = eval_store.read_suite(suite_name)
            if raw_suite is None:
                if suite_name == "public":
                    raise PolicyError("eval_failed", [_issue(None, "missing")])
                suite = None
            else:
                suite = validate_suite(raw_suite, suite_name)
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "format")]) from None

    if suite is None:
        report = _base_report(
            run_id=run_id,
            created_at=created_at,
            correlation_id=correlation_id,
            suite_name=suite_name,
            suite_version=None,
            digest=None,
            case_count=0,
            normalized=normalized,
            pack_id=pack.profile_id,
            base_hash=base_hash,
            pack_hash=pack.pack_hash,
            descriptor=descriptor,
        )
        report["limitations"] = ["suite_not_present", "zero_samples"]
        report_path = _write_report(eval_store, run_id, report, process_log)
        return {
            "run_id": run_id,
            "report_path": report_path,
            "acceptance_path": None,
            "suite": report["suite"],
            "buckets": report["buckets"],
            "acceptance": None,
            "status": "not_measured",
        }

    cases = suite["cases"]
    assert isinstance(cases, list)
    digest = suite_hash(suite)
    if not cases:
        report = _base_report(
            run_id=run_id,
            created_at=created_at,
            correlation_id=correlation_id,
            suite_name=suite_name,
            suite_version=str(suite["version"]),
            digest=digest,
            case_count=0,
            normalized=normalized,
            pack_id=pack.profile_id,
            base_hash=base_hash,
            pack_hash=pack.pack_hash,
            descriptor=descriptor,
        )
        report["limitations"] = ["zero_samples"]
        report_path = _write_report(eval_store, run_id, report, process_log)
        return {
            "run_id": run_id,
            "report_path": report_path,
            "acceptance_path": None,
            "suite": report["suite"],
            "buckets": report["buckets"],
            "acceptance": None,
            "status": "not_measured",
        }

    holdout_batch = None
    attestation = None
    if suite_name == "holdout":
        if descriptor.provider_id != "local-process":
            raise PolicyError("invalid_provider", [_issue(None, "vocabulary")])
        try:
            registry = validate_holdout_registry(eval_store.read_holdout_registry())
            holdout_batch = frozen_holdout_batch(registry, digest)
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "format")]) from None
        if holdout_batch is None:
            raise PolicyError("eval_failed", [_issue(None, "missing")])
        if (
            holdout_batch["sample_count"] != len(cases)
            or holdout_batch["sample_fingerprints"]
            != [sample_fingerprint(case) for case in cases]
        ):
            raise PolicyError("eval_failed", [_issue(None, "range")])
        try:
            raw_attestation = eval_store.read_holdout_attestation()
            attestation = validate_holdout_attestation(
                raw_attestation,
                provider_id=descriptor.provider_id,
                model_id=descriptor.model_id,
                model_config_hash=descriptor.model_config_hash,
            )
        except (ValueError, PolicyError):
            raise PolicyError("authorization_required", [_issue(None, "conflict")]) from None

    plan = {
        "suite": suite_name,
        "suite_version": suite["version"],
        "suite_hash": digest,
        "case_count": len(cases),
        "profile_id": pack.profile_id,
        "pack_path": normalized,
        "pack_hash": pack.pack_hash,
        "provider": descriptor.to_dict(),
        "external_send": descriptor.provider_id == "external-agent",
        "scope": "holdout_local_no_upload" if suite_name == "holdout" else "model_eval_only",
    }
    if attestation is not None:
        plan["attestation_hash"] = _attestation_digest(attestation)

    attempt_entry: dict[str, object] | None = None
    if suite_name == "holdout":
        assert holdout_batch is not None
        attempt_id = uuid.uuid4().hex

        def begin_mutation(registry: dict[str, object]) -> object:
            """在锁内持久化首次/重复访问意向。"""
            validate_holdout_registry(registry)
            batch = frozen_holdout_batch(registry, digest)
            if batch is None:
                raise ValueError("holdout batch is missing")
            return begin_holdout_access(
                registry,
                batch,
                attempt_id=attempt_id,
                at=created_at,
                base_hash=base_hash,
                pack_hash=pack.pack_hash,
                provider_id=descriptor.provider_id,
                model_id=descriptor.model_id,
                model_config_hash=descriptor.model_config_hash,
            )

        with _eval_stage(process_log, "holdout"):
            try:
                attempt_entry = eval_store.update_holdout_registry(begin_mutation)  # type: ignore[assignment]
            except ValueError:
                raise PolicyError("eval_failed", [_issue(None, "conflict")]) from None

    authorize_started = time.perf_counter()
    if process_log is not None:
        process_log.record(stage="eval", operation="authorize", result="started")
    try:
        confirmed = bool(authorize(plan))
    except Exception:
        confirmed = False
    if not confirmed:
        if process_log is not None:
            process_log.record(
                stage="eval",
                operation="authorize",
                result="failed",
                reason_code="authorization_required",
                error_category="eval",
                duration_ms=elapsed_ms(authorize_started),
            )
        if attempt_entry is not None:
            try:
                _finish_holdout(
                    eval_store,
                    digest,
                    str(attempt_entry["attempt_id"]),
                    at=utc_now(),
                    outcome="failed",
                    passed=0,
                    total=len(cases),
                    process_log=process_log,
                )
            except PolicyError:
                pass
        raise PolicyError("authorization_required", [_issue(None, "conflict")])
    if process_log is not None:
        process_log.record(
            stage="eval",
            operation="authorize",
            result="ok",
            duration_ms=elapsed_ms(authorize_started),
        )

    results: list[dict[str, object]] = []
    passed = 0
    failed = 0
    interruption: BaseException | None = None
    case_failure_error: Exception | None = None
    try:
        for index, case in enumerate(cases, start=1):
            preview = project_case(case)
            case_id = str(preview["case_id"])
            case_started = time.perf_counter()
            if process_log is not None:
                process_log.record(stage="eval", operation="case", result="started", case_index=index)
            case_now = str(preview["now"])
            try:
                envelope = build_envelope(
                    task_id=str(preview["task_id"]),
                    nonce=str(preview["nonce"]),
                    kind=str(preview["kind"]),
                    subject=preview["subject"],
                    inbound=str(preview["inbound"]),
                    context=list(preview["context"]),
                    allowed_facts=list(preview["allowed_facts"]),
                    base_files=base_files,
                    pack=pack,
                    now=case_now,
                )
                rendered = render_request(base_files, pack, envelope, now=case_now)
                outcome = decide(
                    rendered=rendered,
                    envelope=envelope,
                    base_files=base_files,
                    pack=pack,
                    bridge=bridge,
                    snapshot=RepositorySnapshot(repo, normalized),
                    lease=EvaluationLease(add_seconds(case_now, 60)),
                    clock=LogicalAdvancingClock(case_now),
                    process_log=process_log,
                )
            except DecisionError as error:
                failed += 1
                results.append(
                    _case_result(index, case_id, "failed", reason_code=error.reason_code)
                )
                if process_log is not None:
                    process_log.record(
                        stage="eval",
                        operation="case",
                        result="failed",
                        reason_code=error.reason_code,
                        error_category="decision",
                        duration_ms=elapsed_ms(case_started),
                        case_index=index,
                    )
                continue
            except DecisionRunError as error:
                failed += 1
                results.append(
                    _case_result(
                        index,
                        case_id,
                        "failed",
                        reason_code=error.reason_code,
                        detail=error.detail,
                        attempts=error.attempts,
                        submissions=error.submissions,
                    )
                )
                if process_log is not None:
                    process_log.record(
                        stage="eval",
                        operation="case",
                        result="failed",
                        reason_code=error.reason_code,
                        error_category="decision",
                        duration_ms=elapsed_ms(case_started),
                        case_index=index,
                    )
                continue
            except Exception:
                failed += 1
                results.append(
                    _case_result(index, case_id, "failed", reason_code="eval_failed")
                )
                if process_log is not None:
                    process_log.record(
                        stage="eval",
                        operation="case",
                        result="failed",
                        reason_code="eval_failed",
                        error_category="eval",
                        duration_ms=elapsed_ms(case_started),
                        case_index=index,
                    )
                continue
            checks = evaluate_assertions(
                outcome["decision"],
                outcome["reply"],
                outcome["resolved_tier"],
                case["assertions"],
            )
            audit = outcome["audit"]
            if checks:
                failed += 1
                results.append(
                    _case_result(
                        index,
                        case_id,
                        "failed",
                        reason_code="eval_failed",
                        checks=checks,
                        input_hash=str(audit["input_hash"]),
                        prompt_hash=str(audit["prompt_hash"]),
                        attempts=int(audit["bridge_attempts"]),
                        submissions=int(audit["bridge_submissions"]),
                    )
                )
                if process_log is not None:
                    process_log.record(
                        stage="eval",
                        operation="case",
                        result="failed",
                        reason_code="eval_failed",
                        error_category="eval",
                        duration_ms=elapsed_ms(case_started),
                        case_index=index,
                    )
                continue
            passed += 1
            results.append(
                _case_result(
                    index,
                    case_id,
                    "passed",
                    input_hash=str(audit["input_hash"]),
                    prompt_hash=str(audit["prompt_hash"]),
                    attempts=int(audit["bridge_attempts"]),
                    submissions=int(audit["bridge_submissions"]),
                )
            )
            if process_log is not None:
                process_log.record(
                    stage="eval",
                    operation="case",
                    result="ok",
                    duration_ms=elapsed_ms(case_started),
                    case_index=index,
                )
        if failed:
            case_failure_error = PolicyError("eval_failed", [_issue(None, "format")])
    except BaseException as error:
        interruption = error

    outcome_name = (
        "unknown" if interruption is not None else "failed" if failed else "completed"
    )
    if attempt_entry is not None:
        try:
            _finish_holdout(
                eval_store,
                digest,
                str(attempt_entry["attempt_id"]),
                at=utc_now(),
                outcome=outcome_name,
                passed=passed,
                total=len(cases),
                process_log=process_log,
            )
        except PolicyError:
            if interruption is None:
                raise
    if interruption is not None:
        raise interruption

    acceptance = None
    limitations: list[str] = []
    if suite_name != "public":
        limitations.append("suite_not_public")
    if descriptor.simulated:
        limitations.append("simulated_transport")
    if failed:
        limitations.append("case_failures")
    confirmed_scope = "model_eval_only"
    if suite_name == "holdout":
        confirmed_scope = "holdout_no_upload_attested"
    authorization = {
        "confirmed": True,
        "scope": confirmed_scope,
    }
    if attestation is not None:
        authorization["attestation_hash"] = _attestation_digest(attestation)
    if not limitations:
        acceptance = {
            "schema_version": 1,
            "suite": "public",
            "suite_version": suite["version"],
            "suite_hash": digest,
            "base_version": BASE_VERSION,
            "base_hash": base_hash,
            "pack_id": pack.profile_id,
            "pack_hash": pack.pack_hash,
            "provider_id": descriptor.provider_id,
            "model_id": descriptor.model_id,
            "model_config_hash": descriptor.model_config_hash,
            "bridge": descriptor.bridge,
            "case_count": len(cases),
            "created_at": created_at,
        }
    report = _base_report(
        run_id=run_id,
        created_at=created_at,
        correlation_id=correlation_id,
        suite_name=suite_name,
        suite_version=str(suite["version"]),
        digest=digest,
        case_count=len(cases),
        normalized=normalized,
        pack_id=pack.profile_id,
        base_hash=base_hash,
        pack_hash=pack.pack_hash,
        descriptor=descriptor,
    )
    report["authorization"] = authorization
    report["buckets"] = _buckets(suite_name, len(cases), passed, failed)
    report["cases"] = results
    report["acceptance"] = acceptance
    report["limitations"] = limitations
    if attempt_entry is not None:
        report["holdout"] = {
            "batch_id": str(holdout_batch["batch_id"]),
            "attempt_id": str(attempt_entry["attempt_id"]),
            "outcome": outcome_name,
            "repeat_access": bool(attempt_entry["repeat_access"]),
            "new_evidence": bool(
                outcome_name == "completed" and not attempt_entry["repeat_access"]
            ),
        }
    report_path = _write_report(eval_store, run_id, report, process_log)
    acceptance_path = None
    if acceptance is not None:
        with _eval_stage(process_log, "acceptance"):
            acceptance_path = eval_store.write_acceptance(run_id, acceptance)
    if case_failure_error is not None:
        raise PolicyError(
            "eval_failed",
            [_issue(report_path, "format")],
        )
    return {
        "run_id": run_id,
        "report_path": report_path,
        "acceptance_path": acceptance_path,
        "suite": report["suite"],
        "buckets": report["buckets"],
        "acceptance": acceptance,
        "status": "measured",
    }


def register_holdout(
    eval_store: EvalStorePort,
    *,
    batch_id: str,
    version: str,
    sample_count: int,
    annotator: str,
    maintainer: str,
    frozen_at: str,
    candidate_release: str,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """登记新冻结批次；样本数/指纹必须与当前套件一致，占用样本一律拒绝。"""
    with _eval_stage(process_log, "holdout"):
        data = eval_store.read_suite("holdout")
        if data is None:
            raise PolicyError("eval_failed", [_issue(None, "missing")])
        try:
            suite = validate_suite(data, "holdout")
            cases = suite["cases"]
            assert isinstance(cases, list)
            fingerprints = suite_fingerprints(suite)
            if type(sample_count) is not int or sample_count != len(cases):
                raise ValueError("sample count mismatch")
            registry = eval_store.read_holdout_registry()
            validate_holdout_registry(registry)
            result = eval_store.update_holdout_registry(
                lambda current: register_holdout_batch(
                    current,
                    batch_id=batch_id,
                    suite_digest=suite_hash(suite),
                    version=version,
                    sample_count=sample_count,
                    sample_fingerprints=fingerprints,
                    annotator=annotator,
                    maintainer=maintainer,
                    frozen_at=frozen_at,
                    candidate_release=candidate_release,
                )
            )
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "format")]) from None
    return {"batch": result}


def retire_holdout(
    eval_store: EvalStorePort,
    *,
    batch_id: str,
    retired_at: str,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """整批退役并写实际 regression 准入副本；失败不改变原登记。"""
    with _eval_stage(process_log, "holdout"):
        data = eval_store.read_suite("holdout")
        if data is None:
            raise PolicyError("eval_failed", [_issue(None, "missing")])
        try:
            suite = validate_suite(data, "holdout")
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "format")]) from None
        try:
            registry = validate_holdout_registry(eval_store.read_holdout_registry())
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "format")]) from None
        batch = next(
            (item for item in registry["batches"] if item["batch_id"] == batch_id),
            None,
        )
        if batch is None:
            raise PolicyError("eval_failed", [_issue(None, "missing")])
        if (
            batch["suite_hash"] != suite_hash(suite)
            or batch["sample_fingerprints"] != suite_fingerprints(suite)
        ):
            raise PolicyError("eval_failed", [_issue(None, "conflict")])
        admission_existed = eval_store.regression_admission_exists(batch_id)
        regression_ref = eval_store.write_regression_admission(batch_id, suite)

        def mutate(current: dict[str, object]) -> object:
            """在锁内把批次整批退役并写 regression 引用。"""
            validate_holdout_registry(current)
            retire_holdout_batch(
                current,
                batch_id=batch_id,
                retired_at=retired_at,
                regression_ref=regression_ref,
            )
            return {"batch_id": batch_id, "status": "retired"}

        try:
            result = eval_store.update_holdout_registry(mutate)
        except (PolicyError, ValueError):
            if not admission_existed:
                try:
                    eval_store.remove_regression_admission(batch_id)
                except PolicyError:
                    pass
            raise PolicyError("eval_failed", [_issue(None, "conflict")]) from None
    return {"regression_ref": regression_ref, **result}  # type: ignore[dict-item]
