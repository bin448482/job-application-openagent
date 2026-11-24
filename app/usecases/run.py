"""执行真实运行前的 mode/设备确认/验收/共享占用与 profile 库绑定预检。"""

from __future__ import annotations

from app.domain.models import Owner
from app.domain.policy import PolicyIssue, PolicyPack, compute_base_hash
from app.ports.device_gate import SharedDeviceGatePort
from app.ports.eval import EvalStorePort
from app.ports.policy import PolicyError
from app.ports.profile import ProfileStorePort
from app.process_log import ProcessLog
from app.profile_open import open_profile_store
from app.usecases.acceptance import check_acceptance

RUN_MODES = ("read_only", "draft_only", "auto_send_tier_a")


def _issue(field: str | None, code: str) -> PolicyIssue:
    """构造固定运行预检诊断，不携带事实值或路径。"""
    return PolicyIssue(file=None, field=field, line=None, code=code)


def check_preflight(
    *,
    base_files: dict[str, str],
    pack: PolicyPack,
    mode: str,
    confirm_device: bool,
    gate: SharedDeviceGatePort,
    profile_store: ProfileStorePort,
    owner: Owner,
    profile_db_path: str,
    now: str,
    eval_store: EvalStorePort | None = None,
    credential_rel: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    model_config_hash: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """只做预检，不调用设备、模型或动作；失败时保留共享占用并隔离。

    read_only 只要求本轮设备确认；draft_only 另要求当前验收；A 档在 v1 固定 C
    且缺独立前置判据时始终拒绝，不把预检成功当作发送授权。
    """
    if mode not in RUN_MODES:
        raise PolicyError("invalid_input", [_issue("mode", "vocabulary")])
    if mode == "auto_send_tier_a":
        raise PolicyError(
            "authorization_required", [_issue("mode", "safety_floor")]
        )
    if not confirm_device:
        raise PolicyError(
            "authorization_required", [_issue("device", "confirmation")]
        )
    acceptance = None
    if mode == "draft_only":
        if (
            eval_store is None
            or not credential_rel
            or not provider_id
            or not model_id
            or not model_config_hash
        ):
            raise PolicyError(
                "authorization_required", [_issue("acceptance", "missing")]
            )
        acceptance = check_acceptance(
            eval_store,
            credential_rel,
            base_files=base_files,
            pack=pack,
            provider_id=provider_id,
            model_id=model_id,
            model_config_hash=model_config_hash,
        )

    gate.acquire(
        profile_id=pack.profile_id,
        profile_db_path=profile_db_path,
        owner=owner,
        now=now,
    )
    try:
        gate.verify(now=now)
        open_profile_store(profile_store, process_log)
        pending = profile_store.pending_activity()
    except Exception:
        try:
            gate.release(now=now, converged=False)
        except Exception:
            pass
        raise
    profile_store.close()
    if (
        pending["leased"]
        or pending["running"]
        or pending["blocked"]
        or pending["device_lease"]
        or pending.get("actions")
    ):
        try:
            gate.release(now=now, converged=False)
        except Exception:
            pass
        raise PolicyError(
            "authorization_required", [_issue("activity", "unresolved")]
        )
    gate.release(now=now, converged=True)
    return {
        "preflight_only": True,
        "mode": mode,
        "profile_id": pack.profile_id,
        "base_hash": compute_base_hash(base_files),
        "pack_hash": pack.pack_hash,
        "device_confirmed": True,
        "gate": "released",
        "profile_db": "bound",
        "actions_performed": 0,
        "decisions": 0,
        "acceptance": acceptance,
        "limitations": [
            "m5_actions_not_implemented",
            "m8_run_not_implemented",
            "device_probe_not_run",
        ],
    }
