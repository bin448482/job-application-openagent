"""只读复核 M4b 验收凭证是否绑定当前 base/pack/provider/model 摘要。"""

from __future__ import annotations

from app.domain.models import is_hash
from app.domain.policy import (
    BASE_VERSION,
    PolicyIssue,
    PolicyPack,
    compute_base_hash,
)
from app.ports.eval import EvalStorePort
from app.ports.policy import PolicyError

ACCEPTANCE_KEYS = (
    "schema_version",
    "suite",
    "suite_version",
    "suite_hash",
    "base_version",
    "base_hash",
    "pack_id",
    "pack_hash",
    "provider_id",
    "model_id",
    "model_config_hash",
    "bridge",
    "case_count",
    "created_at",
)


def _issue(field: str | None, code: str) -> PolicyIssue:
    """构造固定凭证诊断，不回显凭证原值。"""
    return PolicyIssue(file=None, field=field, line=None, code=code)


def check_acceptance(
    eval_store: EvalStorePort,
    credential_rel: str,
    *,
    base_files: dict[str, str],
    pack: PolicyPack,
    provider_id: str | None = None,
    model_id: str | None = None,
    model_config_hash: str | None = None,
) -> dict[str, object]:
    """读取本机凭证并比较当前摘要；任何摘要变化都拒绝旧验收。"""
    data = eval_store.read_acceptance(credential_rel)
    if data is None:
        raise PolicyError("authorization_required")
    if set(data) != set(ACCEPTANCE_KEYS):
        raise PolicyError("policy_changed", [_issue(None, "format")])
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise PolicyError("policy_changed", [_issue("schema_version", "type")])
    if data["suite"] != "public" or type(data["case_count"]) is not int:
        raise PolicyError("policy_changed", [_issue("suite", "vocabulary")])
    if data["case_count"] <= 0:
        raise PolicyError("policy_changed", [_issue("case_count", "range")])
    for field in ("suite_hash", "base_hash", "pack_hash", "model_config_hash"):
        if not is_hash(data[field]):
            raise PolicyError("policy_changed", [_issue(field, "type")])
    if data["base_version"] != BASE_VERSION:
        raise PolicyError("policy_changed", [_issue("base_version", "conflict")])
    current_base_hash = compute_base_hash(base_files)
    if data["base_hash"] != current_base_hash:
        raise PolicyError("policy_changed", [_issue("base_hash", "conflict")])
    if data["pack_id"] != pack.profile_id:
        raise PolicyError("policy_changed", [_issue("pack_id", "conflict")])
    if data["pack_hash"] != pack.pack_hash:
        raise PolicyError("policy_changed", [_issue("pack_hash", "conflict")])
    for field, expected in (
        ("provider_id", provider_id),
        ("model_id", model_id),
        ("model_config_hash", model_config_hash),
    ):
        if expected is not None and data[field] != expected:
            raise PolicyError("policy_changed", [_issue(field, "conflict")])
    return {
        "valid": True,
        "credential_path": credential_rel,
        "scope": "model_eval_only",
        "base_version": data["base_version"],
        "base_hash": data["base_hash"],
        "pack_id": data["pack_id"],
        "pack_hash": data["pack_hash"],
        "provider_id": data["provider_id"],
        "model_id": data["model_id"],
        "model_config_hash": data["model_config_hash"],
        "created_at": data["created_at"],
        "note": "model-eval acceptance only; not device or send authorization",
    }
