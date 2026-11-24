"""提供 M4b 评测套件、独立断言与留出冻结/退役的纯领域校验。"""

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
REGISTRY_KEYS = ("schema_version", "batches")
BATCH_KEYS = (
    "batch_id",
    "suite_hash",
    "version",
    "sample_count",
    "annotator",
    "maintainer",
    "frozen_at",
    "candidate_release",
    "status",
    "access_rounds",
    "retired_at",
)
ACCESS_KEYS = (
    "at",
    "base_hash",
    "pack_hash",
    "provider_id",
    "model_id",
    "model_config_hash",
    "passed",
    "total",
    "repeat_access",
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


def validate_holdout_registry(data: object) -> dict[str, object]:
    """严格校验留出登记：维护者、独立标注者、版本、数量、时间与候选关联。"""
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
        if not is_hash(batch["suite_hash"]):
            raise ValueError("holdout suite hash is invalid")
        for key in (
            "version",
            "annotator",
            "maintainer",
            "candidate_release",
        ):
            if not is_nonempty_text(batch[key]):
                raise ValueError("holdout batch identity is invalid")
        if type(batch["sample_count"]) is not int or batch["sample_count"] < 0:
            raise ValueError("holdout sample count is invalid")
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
            try:
                parse_utc(item["at"])
            except (ValueError, TypeError):
                raise ValueError("holdout access time is invalid") from None
            for key in (
                "base_hash",
                "pack_hash",
                "model_config_hash",
            ):
                if not is_hash(item[key]):
                    raise ValueError("holdout access hash is invalid")
            for key in ("provider_id", "model_id"):
                if not is_nonempty_text(item[key]):
                    raise ValueError("holdout access identity is invalid")
            if (
                type(item["passed"]) is not int
                or type(item["total"]) is not int
                or item["passed"] < 0
                or item["total"] < 0
                or item["passed"] > item["total"]
                or not isinstance(item["repeat_access"], bool)
            ):
                raise ValueError("holdout access counts are invalid")
    return data


def register_holdout_batch(
    registry: dict[str, object],
    *,
    batch_id: str,
    suite_digest: str,
    version: str,
    sample_count: int,
    annotator: str,
    maintainer: str,
    frozen_at: str,
    candidate_release: str,
) -> dict[str, object]:
    """注册新的冻结留出批次；新候选发布不得复用旧批次或相同发布名。"""
    validate_holdout_registry(registry)
    if not is_nonempty_text(batch_id):
        raise ValueError("holdout batch id is invalid")
    batches = registry["batches"]
    assert isinstance(batches, list)
    if any(batch["batch_id"] == batch_id for batch in batches):
        raise ValueError("holdout batch exists")
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
        "annotator": annotator,
        "maintainer": maintainer,
        "frozen_at": frozen_at,
        "candidate_release": candidate_release,
        "status": "frozen",
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
    rounds = batch["access_rounds"]
    assert isinstance(rounds, list)
    if not rounds:
        return False
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
    return True


def record_holdout_access(
    registry: dict[str, object],
    batch: dict[str, object],
    *,
    at: str,
    base_hash: str,
    pack_hash: str,
    provider_id: str,
    model_id: str,
    model_config_hash: str,
    passed: int,
    total: int,
) -> dict[str, object]:
    """记录访问轮次；同摘要重跑标记为重复访问，摘要变化须先整批退役。"""
    validate_holdout_registry(registry)
    repeat = check_holdout_access(
        batch,
        base_hash=base_hash,
        pack_hash=pack_hash,
        provider_id=provider_id,
        model_id=model_id,
        model_config_hash=model_config_hash,
    )
    rounds = batch["access_rounds"]
    assert isinstance(rounds, list)
    entry = {
        "at": at,
        "base_hash": base_hash,
        "pack_hash": pack_hash,
        "provider_id": provider_id,
        "model_id": model_id,
        "model_config_hash": model_config_hash,
        "passed": passed,
        "total": total,
        "repeat_access": repeat,
    }
    rounds.append(entry)
    validate_holdout_registry(registry)
    return entry


def retire_holdout_batch(
    registry: dict[str, object], *, batch_id: str, retired_at: str
) -> None:
    """整批退役转回归；退役后不能继续作为留出证据使用。"""
    validate_holdout_registry(registry)
    for batch in registry["batches"]:
        if batch["batch_id"] == batch_id:
            if batch["status"] != "frozen":
                raise ValueError("holdout batch is retired")
            batch["status"] = "retired"
            batch["retired_at"] = retired_at
            validate_holdout_registry(registry)
            return
    raise ValueError("holdout batch is missing")
