"""编排 M4b `policy eval`：实际加载包、同一桥路径、独立断言与本机报告。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from app.domain.eval import (
    SUITE_NAMES,
    check_holdout_access,
    evaluate_assertions,
    frozen_holdout_batch,
    project_case,
    record_holdout_access,
    register_holdout_batch,
    retire_holdout_batch,
    suite_hash,
    validate_holdout_registry,
    validate_suite,
)
from app.domain.models import add_seconds
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
        """保存由评测程序生成、非模型输入决定的固定截止时间。"""
        self._expires_at = expires_at

    def current_expiry(self) -> str | None:
        """返回评测合成租约的规范截止时间。"""
        return self._expires_at


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


def _case_result(
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
    """组装单例结果，只留固定诊断与摘要，不保存回复正文。"""
    result: dict[str, object] = {
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

    suite_started = time.perf_counter()
    if process_log is not None:
        process_log.record(stage="eval", operation="suite", result="started")
    try:
        raw_suite = eval_store.read_suite(suite_name)
        if raw_suite is None:
            suite = None
        else:
            suite = validate_suite(raw_suite, suite_name)
    except ValueError:
        if process_log is not None:
            process_log.record(
                stage="eval",
                operation="suite",
                result="failed",
                reason_code="eval_failed",
                error_category="eval",
                duration_ms=elapsed_ms(suite_started),
            )
        raise PolicyError("eval_failed", [_issue(None, "format")]) from None
    if process_log is not None:
        process_log.record(
            stage="eval",
            operation="suite",
            result="ok",
            duration_ms=elapsed_ms(suite_started),
        )
    if suite is None and suite_name == "public":
        raise PolicyError("eval_failed", [_issue(None, "missing")])
    if suite is None:
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": created_at,
            "suite": {
                "name": suite_name,
                "version": None,
                "hash": None,
                "case_count": 0,
            },
            "pack": {
                "path": normalized,
                "profile_id": pack.profile_id,
                "base_version": BASE_VERSION,
                "base_hash": compute_base_hash(base_files),
                "pack_hash": pack.pack_hash,
            },
            "provider": descriptor.to_dict(),
            "authorization":{"confirmed": True, "scope": "model_eval_only"},
            "buckets": _buckets(suite_name, 0, 0, 0),
            "cases": [],
            "acceptance": None,
            "limitations": ["suite_not_present"],
        }
        report_path = eval_store.write_report(run_id, report)
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
    holdout_batch = None
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
        if holdout_batch["sample_count"] != len(cases):
            raise PolicyError("eval_failed", [_issue(None, "range")])

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
        "scope": "model_eval_only",
    }
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
            )
        raise PolicyError("authorization_required", [_issue(None, "conflict")])
    if process_log is not None:
        process_log.record(stage="eval", operation="authorize", result="ok")

    if holdout_batch is not None:
        try:
            check_holdout_access(
                holdout_batch,
                base_hash=compute_base_hash(base_files),
                pack_hash=pack.pack_hash,
                provider_id=descriptor.provider_id,
                model_id=descriptor.model_id,
                model_config_hash=descriptor.model_config_hash,
            )
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "conflict")]) from None

    results: list[dict[str, object]] = []
    passed = 0
    failed = 0
    base_hash = compute_base_hash(base_files)
    for case in cases:
        preview = project_case(case)
        case_id = str(preview["case_id"])
        case_started = time.perf_counter()
        if process_log is not None:
            process_log.record(stage="eval", operation="case", result="started")
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
            base_hash = rendered.base_hash
            outcome = decide(
                rendered=rendered,
                envelope=envelope,
                base_files=base_files,
                pack=pack,
                bridge=bridge,
                snapshot=RepositorySnapshot(repo, normalized),
                lease=EvaluationLease(add_seconds(case_now, 60)),
                now=case_now,
                process_log=process_log,
            )
        except DecisionError as error:
            failed += 1
            results.append(
                _case_result(case_id, "failed", reason_code=error.reason_code)
            )
            if process_log is not None:
                process_log.record(
                    stage="eval",
                    operation="case",
                    result="failed",
                    reason_code=error.reason_code,
                    error_category="decision",
                    duration_ms=elapsed_ms(case_started),
                )
            continue
        except DecisionRunError as error:
            failed += 1
            results.append(
                _case_result(
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
                )
            continue
        passed += 1
        results.append(
            _case_result(
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
            )

    repeat_access = None
    if holdout_batch is not None:
        try:
            registry = validate_holdout_registry(eval_store.read_holdout_registry())
            batch = frozen_holdout_batch(registry, digest)
            assert batch is not None
            entry = record_holdout_access(
                registry,
                batch,
                at=created_at,
                base_hash=str(base_hash),
                pack_hash=pack.pack_hash,
                provider_id=descriptor.provider_id,
                model_id=descriptor.model_id,
                model_config_hash=descriptor.model_config_hash,
                passed=passed,
                total=len(cases),
            )
            eval_store.write_holdout_registry(registry)
            repeat_access = bool(entry["repeat_access"])
        except ValueError:
            raise PolicyError("eval_failed", [_issue(None, "conflict")]) from None

    measured = len(cases) > 0
    all_passed = measured and failed == 0
    acceptance = None
    limitations: list[str] = []
    if not measured:
        limitations.append("zero_samples")
    if suite_name != "public":
        limitations.append("suite_not_public")
    if descriptor.simulated:
        limitations.append("simulated_transport")
    if measured and failed > 0:
        limitations.append("case_failures")
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
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": created_at,
        "suite": {
            "name": suite_name,
            "version": suite["version"],
            "hash": digest,
            "case_count": len(cases),
        },
        "pack": {
            "path": normalized,
            "profile_id": pack.profile_id,
            "base_version": BASE_VERSION,
            "base_hash": base_hash,
            "pack_hash": pack.pack_hash,
        },
        "provider": descriptor.to_dict(),
        "authorization": {"confirmed": True, "scope": "model_eval_only"},
        "buckets": _buckets(suite_name, len(cases), passed, failed),
        "cases": results,
        "acceptance": acceptance,
        "limitations": limitations,
    }
    if suite_name == "holdout":
        report["holdout"] = {
            "batch_id": holdout_batch["batch_id"],
            "repeat_access": repeat_access,
            "new_evidence": repeat_access is False,
        }
    report_path = eval_store.write_report(run_id, report)
    acceptance_path = None
    if acceptance is not None:
        acceptance_path = eval_store.write_acceptance(run_id, acceptance)
    if all_passed is False or not measured:
        raise PolicyError("eval_failed", [_issue(report_path, "format")])
    return {
        "run_id": run_id,
        "report_path": report_path,
        "acceptance_path": acceptance_path,
        "suite": report["suite"],
        "buckets": report["buckets"],
        "acceptance": acceptance,
        "status": "measured" if measured else "not_measured",
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
) -> dict[str, object]:
    """登记新的留出冻结批次，并要求维护者、标注者、数量与候选发布齐全。"""
    data = eval_store.read_suite("holdout")
    if data is None:
        raise PolicyError("eval_failed", [_issue(None, "missing")])
    try:
        suite = validate_suite(data, "holdout")
        registry = validate_holdout_registry(eval_store.read_holdout_registry())
        batch = register_holdout_batch(
            registry,
            batch_id=batch_id,
            suite_digest=suite_hash(suite),
            version=version,
            sample_count=sample_count,
            annotator=annotator,
            maintainer=maintainer,
            frozen_at=frozen_at,
            candidate_release=candidate_release,
        )
    except ValueError:
        raise PolicyError("eval_failed", [_issue(None, "format")]) from None
    path = eval_store.write_holdout_registry(registry)
    return {"registry_path": path, "batch": batch}


def retire_holdout(eval_store: EvalStorePort, *, batch_id: str, retired_at: str) -> dict[str, object]:
    """整批退役留出，之后必须为新候选发布注册新冻结批次。"""
    try:
        registry = validate_holdout_registry(eval_store.read_holdout_registry())
        retire_holdout_batch(registry, batch_id=batch_id, retired_at=retired_at)
    except ValueError:
        raise PolicyError("eval_failed", [_issue(None, "conflict")]) from None
    path = eval_store.write_holdout_registry(registry)
    return {"registry_path": path, "batch_id": batch_id, "status": "retired"}
