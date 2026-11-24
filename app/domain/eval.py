"""提供 M4b 评测套件、独立断言、留出冻结/访问/退役与无上传合同校验。"""

from __future__ import annotations

from typing import Mapping

from app.domain.models import canonical_hash, is_hash, is_nonempty_text, parse_utc
from app.domain.policy import (
    INTENTS,
    SUB_INTENTS,
    is_case_id,
    validate_preview_case,
)

SUITE_NAMES = ("public", "regression", "holdout")
SUITE_KEYS = ("schema_version", "suite", "version", "cases")
CASE_KEYS = ("input", "assertions")
ASSERTION_KEYS = (
    "intent_in",
    "sub_intents_required",
    "sub_intents_forbidden",
    "reply_nonempty",
    "reply_contains_any",
    "escalate_in",
)
CHECK_CODES = frozenset(
    {
        "intent",
        "sub_intents_missing",
        "sub_intents_forbidden",
        "reply_empty",
        "reply_contains",
        "escalate",
        "tier",
    }
)
SAMPLE_EXCLUDE_KEYS = ("case_id", "task_id", "nonce", "now")
REGISTRY_KEYS = ("schema_version", "batches")
BATCH_KEYS = (
    "batch_id",
    "suite_hash",
    "version",
    "sample_count",
    "sample_fingerprints",
    "annotator",
    "maintainer",
    "frozen_at",
    "candidate_release",
    "status",
    "viewed",
    "regression_eligible",
    "regression_ref",
    "access_rounds",
    "retired_at",
)
ACCESS_KEYS = (
    "attempt_id",
    "started_at",
    "completed_at",
    "phase",
    "outcome",
    "base_hash",
    "pack_hash",
    "provider_id",
    "model_id",
    "model_config_hash",
    "passed",
    "total",
    "repeat_access",
)
ATTESTATION_KEYS = (
    "schema_version",
    "scope",
    "provider_id",
    "model_id",
    "model_config_hash",
    "no_upload",
    "no_cache",
    "no_share",
    "no_telemetry",
    "cloud_fallback_disabled",
    "host_scope",
    "operator",
    "attested_at",
)


def empty_holdout_registry() -> dict[str, object]:
    """返回空的留出批次登记对象。"""
    return {"schema_version": 1, "batches": []}


def _validate_assertions(data: object) -> dict[str, object]:
    """校验单例断言只含固定键与合法词表/类型。"""
    if not isinstance(data, dict) or set(data) != set(ASSERTION_KEYS):
        raise ValueError("assertions keys are invalid")
    intent_in = data["intent_in"]
    if (
        not isinstance(intent_in, list)
        or not intent_in
        or any(not isinstance(item, str) or item not in INTENTS for item in intent_in)
    ):
        raise ValueError("assertions intent is invalid")
    for key in ("sub_intents_required", "sub_intents_forbidden"):
        items = data[key]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or item not in SUB_INTENTS for item in items
        ):
            raise ValueError("assertions sub intent is invalid")
    if not isinstance(data["reply_nonempty"], bool):
        raise ValueError("assertions reply flag is invalid")
    contains = data["reply_contains_any"]
    if not isinstance(contains, list) or any(
        not is_nonempty_text(item) for item in contains
    ):
        raise ValueError("assertions reply contains is invalid")
    escalate_in = data["escalate_in"]
    if (
        not isinstance(escalate_in, list)
        or not escalate_in
        or any(item not in (None, "human_review") for item in escalate_in)
    ):
        raise ValueError("assertions escalate is invalid")
    return data


def validate_suite(data: object, suite_name: str) -> dict[str, object]:
    """严格校验公开/回归/留出套件；返回原对象供调用方投影。"""
    if suite_name not in SUITE_NAMES:
        raise ValueError("suite name is invalid")
    if not isinstance(data, dict) or set(data) != set(SUITE_KEYS):
        raise ValueError("suite keys are invalid")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("suite version is invalid")
    if data["suite"] != suite_name:
        raise ValueError("suite identity is invalid")
    if not is_nonempty_text(data["version"]):
        raise ValueError("suite release is invalid")
    cases = data["cases"]
    if not isinstance(cases, list) or len(cases) > 100:
        raise ValueError("suite cases are invalid")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != set(CASE_KEYS):
            raise ValueError("suite case keys are invalid")
        try:
            preview = validate_preview_case(case["input"])
            _validate_assertions(case["assertions"])
        except (ValueError, UnicodeError):
            raise ValueError("suite case is invalid") from None
        case_id = preview["case_id"]
        if not is_case_id(case_id) or case_id in seen:
            raise ValueError("suite case id is invalid")
        seen.add(case_id)
    return data


def suite_hash(data: Mapping[str, object]) -> str:
    """计算包含输入与断言侧的套件摘要，供报告与冻结批次绑定。"""
    return canonical_hash(data)


def project_case(case: Mapping[str, object]) -> dict[str, object]:
    """只投影单例允许输入，绝不把断言或其它案例送入桥。"""
    return dict(case["input"])  # type: ignore[arg-type]


def sample_fingerprint(case: Mapping[str, object]) -> str:
    """按语义输入排除 case/task/nonce/now 等运行身份，生成样本身份摘要。"""
    input_data = project_case(case)
    return canonical_hash(
        {key: value for key, value in input_data.items() if key not in SAMPLE_EXCLUDE_KEYS}
    )


def suite_fingerprints(suite: Mapping[str, object]) -> list[str]:
    """按套件顺序返回每例样本身份摘要；重复指纹由套件/登记校验拒绝。"""
    cases = suite["cases"]
    assert isinstance(cases, list)
    return [sample_fingerprint(case) for case in cases]


def evaluate_assertions(
    decision: Mapping[str, object],
    reply: str,
    resolved_tier: str,
    assertions: Mapping[str, object],
) -> list[str]:
    """独立检查意图、子意图、回复属性、escalate 与最终档位，返回失败码。"""
    failures: list[str] = []
    if decision.get("intent") not in assertions["intent_in"]:
        failures.append("intent")
    sub_intents = decision.get("sub_intents")
    subs = set(sub_intents) if isinstance(sub_intents, list) else set()
    if not set(assertions["sub_intents_required"]) <= subs:
        failures.append("sub_intents_missing")
    if set(assertions["sub_intents_forbidden"]) & subs:
        failures.append("sub_intents_forbidden")
    if assertions["reply_nonempty"] and not reply:
        failures.append("reply_empty")
    contains = assertions["reply_contains_any"]
    if contains and not any(item in reply for item in contains):
        failures.append("reply_contains")
    if decision.get("escalate") not in assertions["escalate_in"]:
        failures.append("escalate")
    if resolved_tier != "C":
        failures.append("tier")
    return failures


def _validate_hashes(item: Mapping[str, object], keys: tuple[str, ...]) -> None:
    """校验指定字段均为小写 SHA-256。"""
    for key in keys:
        if not is_hash(item[key]):
            raise ValueError("holdout hash is invalid")


def validate_holdout_registry(data: object) -> dict[str, object]:
    """严格校验登记样本指纹、冻结元数据、访问轮次与回归资格。"""
    if not isinstance(data, dict) or set(data) != set(REGISTRY_KEYS):
        raise ValueError("holdout registry keys are invalid")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("holdout registry version is invalid")
    batches = data["batches"]
    if not isinstance(batches, list):
        raise ValueError("holdout batches are invalid")
    seen: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict) or set(batch) != set(BATCH_KEYS):
            raise ValueError("holdout batch keys are invalid")
        if not is_nonempty_text(batch["batch_id"]) or batch["batch_id"] in seen:
            raise ValueError("holdout batch id is invalid")
        seen.add(batch["batch_id"])
        _validate_hashes(batch, ("suite_hash",))
        for key in ("version", "annotator", "maintainer", "candidate_release"):
            if not is_nonempty_text(batch[key]):
                raise ValueError("holdout batch identity is invalid")
        if type(batch["sample_count"]) is not int or batch["sample_count"] < 0:
            raise ValueError("holdout sample count is invalid")
        fingerprints = batch["sample_fingerprints"]
        if (
            not isinstance(fingerprints, list)
            or len(fingerprints) != batch["sample_count"]
            or any(not is_hash(item) for item in fingerprints)
            or len(set(fingerprints)) != len(fingerprints)
        ):
            raise ValueError("holdout sample fingerprints are invalid")
        if not isinstance(batch["viewed"], bool) or not isinstance(
            batch["regression_eligible"], bool
        ):
            raise ValueError("holdout flags are invalid")
        if batch["regression_ref"] is not None and not is_nonempty_text(
            batch["regression_ref"]
        ):
            raise ValueError("holdout regression ref is invalid")
        try:
            parse_utc(batch["frozen_at"])
            if batch["retired_at"] is not None:
                parse_utc(batch["retired_at"])
        except (ValueError, TypeError):
            raise ValueError("holdout time is invalid") from None
        if batch["status"] not in ("frozen", "retired"):
            raise ValueError("holdout status is invalid")
        rounds = batch["access_rounds"]
        if not isinstance(rounds, list):
            raise ValueError("holdout access rounds are invalid")
        for item in rounds:
            if not isinstance(item, dict) or set(item) != set(ACCESS_KEYS):
                raise ValueError("holdout access keys are invalid")
            if not is_nonempty_text(item["attempt_id"]):
                raise ValueError("holdout attempt is invalid")
            for key in ("started_at", "completed_at"):
                value = item[key]
                if value is not None:
                    try:
                        parse_utc(value)
                    except (ValueError, TypeError):
                        raise ValueError("holdout access time is invalid") from None
            if item["phase"] not in ("intent", "completed"):
                raise ValueError("holdout access phase is invalid")
            if item["outcome"] not in (
                "started",
                "completed",
                "failed",
                "unknown",
            ):
                raise ValueError("holdout access outcome is invalid")
            _validate_hashes(
                item, ("base_hash", "pack_hash", "model_config_hash")
            )
            for key in ("provider_id", "model_id"):
                if not is_nonempty_text(item[key]):
                    raise ValueError("holdout access identity is invalid")
            for key in ("passed", "total"):
                value = item[key]
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("holdout access counts are invalid")
            if (
                item["passed"] is not None
                and item["total"] is not None
                and item["passed"] > item["total"]
            ):
                raise ValueError("holdout access counts are invalid")
            if not isinstance(item["repeat_access"], bool):
                raise ValueError("holdout access repeat is invalid")
    return data


def occupied_sample_fingerprints(registry: Mapping[str, object]) -> set[str]:
    """返回所有历史批次占用过的样本身份，退役/冻结均不可重新作为留出。"""
    batches = registry["batches"]
    assert isinstance(batches, list)
    occupied: set[str] = set()
    for batch in batches:
        occupied.update(str(item) for item in batch["sample_fingerprints"])
    return occupied


def register_holdout_batch(
    registry: dict[str, object],
    *,
    batch_id: str,
    suite_digest: str,
    version: str,
    sample_count: int,
    sample_fingerprints: list[str],
    annotator: str,
    maintainer: str,
    frozen_at: str,
    candidate_release: str,
) -> dict[str, object]:
    """注册冻结留出；缺维护者/标注者、数量不符或已看样本复用均拒绝。"""
    validate_holdout_registry(registry)
    if not is_nonempty_text(batch_id):
        raise ValueError("holdout batch id is invalid")
    if (
        type(sample_count) is not int
        or sample_count < 0
        or not isinstance(sample_fingerprints, list)
        or len(sample_fingerprints) != sample_count
        or any(not is_hash(item) for item in sample_fingerprints)
        or len(set(sample_fingerprints)) != len(sample_fingerprints)
    ):
        raise ValueError("holdout samples are invalid")
    batches = registry["batches"]
    assert isinstance(batches, list)
    if any(batch["batch_id"] == batch_id for batch in batches):
        raise ValueError("holdout batch exists")
    if occupied_sample_fingerprints(registry) & set(sample_fingerprints):
        raise ValueError("holdout samples already occupied")
    if any(
        batch["status"] == "frozen"
        and batch["candidate_release"] == candidate_release
        for batch in batches
    ):
        raise ValueError("holdout candidate already frozen")
    batch = {
        "batch_id": batch_id,
        "suite_hash": suite_digest,
        "version": version,
        "sample_count": sample_count,
        "sample_fingerprints": list(sample_fingerprints),
        "annotator": annotator,
        "maintainer": maintainer,
        "frozen_at": frozen_at,
        "candidate_release": candidate_release,
        "status": "frozen",
        "viewed": False,
        "regression_eligible": False,
        "regression_ref": None,
        "access_rounds": [],
        "retired_at": None,
    }
    batches.append(batch)
    validate_holdout_registry(registry)
    return batch


def frozen_holdout_batch(
    registry: Mapping[str, object], suite_digest: str
) -> dict[str, object] | None:
    """返回与套件摘要匹配的唯一冻结批次；多个匹配视为登记不明确。"""
    batches = registry["batches"]
    assert isinstance(batches, list)
    matches = [
        batch
        for batch in batches
        if batch["status"] == "frozen" and batch["suite_hash"] == suite_digest
    ]
    if len(matches) > 1:
        raise ValueError("holdout batch is ambiguous")
    return matches[0] if matches else None


def _check_digest(
    batch: Mapping[str, object],
    *,
    base_hash: str,
    pack_hash: str,
    provider_id: str,
    model_id: str,
    model_config_hash: str,
) -> None:
    """同一冻结批次只允许同一 base/pack/provider/model/config 摘要。"""
    rounds = batch["access_rounds"]
    assert isinstance(rounds, list)
    if not rounds:
        return
    previous = rounds[-1]
    if (
        previous["base_hash"],
        previous["pack_hash"],
        previous["provider_id"],
        previous["model_id"],
        previous["model_config_hash"],
    ) != (
        base_hash,
        pack_hash,
        provider_id,
        model_id,
        model_config_hash,
    ):
        raise ValueError("holdout batch digest changed")


def check_holdout_access(
    batch: Mapping[str, object],
    *,
    base_hash: str,
    pack_hash: str,
    provider_id: str,
    model_id: str,
    model_config_hash: str,
) -> bool:
    """检查批次是否仍可访问；返回是否为同摘要重复访问，摘要变化即拒绝。"""
    if batch["status"] != "frozen":
        raise ValueError("holdout batch is retired")
    _check_digest(
        batch,
        base_hash=base_hash,
        pack_hash=pack_hash,
        provider_id=provider_id,
        model_id=model_id,
        model_config_hash=model_config_hash,
    )
    return bool(batch["access_rounds"])


def begin_holdout_access(
    registry: dict[str, object],
    batch: dict[str, object],
    *,
    attempt_id: str,
    at: str,
    base_hash: str,
    pack_hash: str,
    provider_id: str,
    model_id: str,
    model_config_hash: str,
) -> dict[str, object]:
    """在任何调用前持久化访问意向；返回含首次/重复标记的访问记录。"""
    validate_holdout_registry(registry)
    repeat = check_holdout_access(
        batch,
        base_hash=base_hash,
        pack_hash=pack_hash,
        provider_id=provider_id,
        model_id=model_id,
        model_config_hash=model_config_hash,
    )
    if not is_nonempty_text(attempt_id):
        raise ValueError("holdout attempt is invalid")
    entry = {
        "attempt_id": attempt_id,
        "started_at": at,
        "completed_at": None,
        "phase": "intent",
        "outcome": "started",
        "base_hash": base_hash,
        "pack_hash": pack_hash,
        "provider_id": provider_id,
        "model_id": model_id,
        "model_config_hash": model_config_hash,
        "passed": None,
        "total": None,
        "repeat_access": repeat,
    }
    rounds = batch["access_rounds"]
    assert isinstance(rounds, list)
    rounds.append(entry)
    batch["viewed"] = True
    validate_holdout_registry(registry)
    return entry


def finish_holdout_access(
    registry: dict[str, object],
    batch: dict[str, object],
    *,
    attempt_id: str,
    at: str,
    outcome: str,
    passed: int | None,
    total: int | None,
) -> dict[str, object]:
    """完成/失败/未知收尾同一意向；不删除已消耗访问，不覆盖其它轮次。"""
    validate_holdout_registry(registry)
    if outcome not in ("completed", "failed", "unknown"):
        raise ValueError("holdout outcome is invalid")
    rounds = batch["access_rounds"]
    assert isinstance(rounds, list)
    for entry in rounds:
        if entry["attempt_id"] != attempt_id:
            continue
        if entry["phase"] != "intent":
            raise ValueError("holdout attempt is already completed")
        entry["phase"] = "completed"
        entry["outcome"] = outcome
        entry["completed_at"] = at
        entry["passed"] = passed
        entry["total"] = total
        validate_holdout_registry(registry)
        return entry
    raise ValueError("holdout attempt is missing")


def retire_holdout_batch(
    registry: dict[str, object],
    *,
    batch_id: str,
    retired_at: str,
    regression_ref: str,
) -> None:
    """整批退役并记录实际 regression 准入副本引用，不保留可重冻结留出。"""
    validate_holdout_registry(registry)
    if not is_nonempty_text(regression_ref):
        raise ValueError("holdout regression ref is invalid")
    for batch in registry["batches"]:
        if batch["batch_id"] == batch_id:
            if batch["status"] != "frozen":
                raise ValueError("holdout batch is retired")
            batch["status"] = "retired"
            batch["retired_at"] = retired_at
            batch["regression_eligible"] = True
            batch["regression_ref"] = regression_ref
            validate_holdout_registry(registry)
            return
    raise ValueError("holdout batch is missing")


def validate_holdout_attestation(
    data: object,
    *,
    provider_id: str,
    model_id: str,
    model_config_hash: str,
) -> dict[str, object]:
    """校验本机无上传合同与当前桥摘要绑定；缺项/非真/坏时间均拒绝。"""
    if not isinstance(data, dict) or set(data) != set(ATTESTATION_KEYS):
        raise ValueError("holdout attestation keys are invalid")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("holdout attestation version is invalid")
    if data["scope"] != "holdout_local_no_upload":
        raise ValueError("holdout attestation scope is invalid")
    if (
        data["provider_id"] != "local-process"
        or data["provider_id"] != provider_id
        or data["model_id"] != model_id
        or data["model_config_hash"] != model_config_hash
    ):
        raise ValueError("holdout attestation binding is invalid")
    for key in (
        "no_upload",
        "no_cache",
        "no_share",
        "no_telemetry",
        "cloud_fallback_disabled",
    ):
        if data[key] is not True:
            raise ValueError("holdout attestation flag is invalid")
    if data["host_scope"] != "local" or not is_nonempty_text(data["operator"]):
        raise ValueError("holdout attestation operator is invalid")
    try:
        parse_utc(data["attested_at"])
    except (ValueError, TypeError):
        raise ValueError("holdout attestation time is invalid") from None
    return data
