"""编排 M4a 策略包初始化、校验、预览、列表、快照与恢复文件工作流。"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from app.domain.json_input import load_json_object
from app.domain.models import is_hash, is_nonempty_text, parse_utc
from app.domain.policy import (
    BASE_VERSION,
    HARD_FLOOR_A_KEYS,
    HARD_FLOOR_B_KEYS,
    HARD_FLOOR_C_KEYS,
    PACK_FILE_NAMES,
    POLICY_TIERS,
    PolicyIssue,
    PolicyPack,
    build_envelope,
    compute_base_hash,
    compute_pack_hash,
    effective_limits,
    is_profile_id,
    is_snapshot_id,
    normalize_active_pack_path,
    normalize_snapshot_path,
    pack_sources,
    parse_and_validate_pack,
    render_request,
    utc_now,
    validate_policy_guides,
    validate_preview_case,
    DecisionError,
)
from app.ports.policy import PolicyActivityPort, PolicyError, PolicyRepositoryPort
from app.process_log import ProcessLog, elapsed_ms

MANIFEST_KEYS = (
    "schema_version",
    "profile_id",
    "source_pack",
    "base_version",
    "base_hash",
    "pack_hash",
    "created_at",
)


class RefusingPolicyActivity:
    """M4a 生产受控实现：无可信活动/未决证据时返回 unknown，拒绝恢复。"""

    def assess_restore(self, *, profile_id: str, dest_pack: str) -> str:
        """不把未知状态当已停止；真实检查由 M4c 注入。"""
        del profile_id, dest_pack
        return "unknown"


@contextmanager
def _policy_stage(
    process_log: ProcessLog | None, operation: str
) -> Iterator[None]:
    """在实际执行边界记录 policy 阶段 started→ok/failed/undetermined 与耗时。"""
    if process_log is not None:
        process_log.record(stage="policy", operation=operation, result="started")
    started = time.perf_counter()
    try:
        yield
    except PolicyError as error:
        if process_log is not None:
            result = "undetermined" if error.activity_state == "unknown" else "failed"
            process_log.record(
                stage="policy",
                operation=operation,
                result=result,
                reason_code=error.reason_code,
                error_category="policy",
                duration_ms=elapsed_ms(started),
            )
        raise
    except Exception:
        if process_log is not None:
            process_log.record(
                stage="policy",
                operation=operation,
                result="failed",
                error_category="internal",
                duration_ms=elapsed_ms(started),
            )
        raise
    else:
        if process_log is not None:
            process_log.record(
                stage="policy",
                operation=operation,
                result="ok",
                duration_ms=elapsed_ms(started),
            )


def _issue(file: str | None, field: str | None, code: str) -> PolicyIssue:
    """构造用例层固定诊断，不携带原值。"""
    return PolicyIssue(file=file, field=field, line=None, code=code)


def _local_dest(value: object) -> tuple[str, str]:
    """校验 init/restore 目标为 prompts/local 直接 profile 子目录。"""
    try:
        normalized = normalize_active_pack_path(value)
    except ValueError:
        raise PolicyError("invalid_input", [_issue(None, None, "path")]) from None
    return normalized, normalized.split("/")[2]


def _snapshot_path(value: object) -> str:
    """校验快照路径为 prompts/local/history 直接快照目录。"""
    try:
        return normalize_snapshot_path(value)
    except ValueError:
        raise PolicyError("invalid_input", [_issue(None, None, "path")]) from None


def _active_pack_path(value: object) -> str:
    """校验策略包路径为 prompts/local 直接活动子目录。"""
    try:
        return normalize_active_pack_path(value)
    except ValueError:
        raise PolicyError("invalid_input", [_issue(None, None, "path")]) from None


def _prefix_issues(issues: list[PolicyIssue], prefix: str) -> list[PolicyIssue]:
    """把载荷文件名诊断补为项目相对路径，便于快照/恢复定位。"""
    return [
        PolicyIssue(
            file=f"{prefix}/{item.file}" if item.file else prefix,
            field=item.field,
            line=item.line,
            code=item.code,
        )
        for item in issues
    ]


def _check_guides(
    guides: dict[str, str],
    *,
    reason_code: str = "invalid_policy",
    prefix: str | None = None,
) -> None:
    """校验可选双指南内容，失败抛出带项目相对定位的固定策略错误。"""
    issues = validate_policy_guides(guides)
    if issues:
        if prefix is not None:
            issues = _prefix_issues(issues, prefix)
        raise PolicyError(reason_code, issues)


def _parse_pack(
    files: dict[str, str],
    guides: dict[str, str],
    pack_rel: str,
    *,
    reason_code: str = "invalid_policy",
) -> PolicyPack:
    """解析四载荷并校验指南；快照场景用 policy_changed 区分来源变化。"""
    pack, issues = parse_and_validate_pack(files)
    if issues:
        raise PolicyError(reason_code, _prefix_issues(issues, pack_rel))
    assert pack is not None
    _check_guides(guides, reason_code=reason_code, prefix=pack_rel)
    return pack


def _load_active_bundle(
    repo: PolicyRepositoryPort, pack_rel: str
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """读取 base、活动包四载荷与可选双指南，供 load 阶段使用。"""
    return repo.read_base(), repo.read_pack(pack_rel), repo.read_guides(pack_rel)


def _load_snapshot_bundle(
    repo: PolicyRepositoryPort, snapshot_rel: str, manifest_rel: str
) -> tuple[dict[str, object], dict[str, str], dict[str, str], dict[str, str]]:
    """读取清单、快照四载荷、快照指南与当前 base，供 restore load 阶段使用。"""
    return (
        repo.read_manifest(manifest_rel),
        repo.read_snapshot_pack(snapshot_rel),
        repo.read_snapshot_guides(snapshot_rel),
        repo.read_base(),
    )


def _verify_source_unchanged(
    repo: PolicyRepositoryPort,
    pack_rel: str,
    files: dict[str, str],
    guides: dict[str, str],
) -> None:
    """重读活动源四载荷与指南，任一差异或读取失败都受控拒绝快照。"""
    try:
        current_files = repo.read_pack(pack_rel)
        current_guides = repo.read_guides(pack_rel)
    except PolicyError:
        raise PolicyError(
            "policy_changed", [_issue(pack_rel, None, "conflict")]
        ) from None
    if current_files != files or current_guides != guides:
        raise PolicyError("policy_changed", [_issue(pack_rel, None, "conflict")])


def _read_snapshot_pack(
    repo: PolicyRepositoryPort, snapshot_rel: str
) -> PolicyPack:
    """读取并校验快照包，失败时按快照内容已变化拒绝恢复。"""
    files = repo.read_snapshot_pack(snapshot_rel)
    pack, issues = parse_and_validate_pack(files)
    if issues:
        raise PolicyError("policy_changed", _prefix_issues(issues, snapshot_rel))
    assert pack is not None
    return pack


def _replace_profile_id(text: str, profile_id: str) -> str:
    """把示例 pack.toml 的唯一 profile_id 行替换为目标目录身份。"""
    matches = list(re.finditer(r'(?m)^profile_id[ \t]*=[ \t]*"[^"]*"', text))
    if len(matches) != 1:
        raise PolicyError(
            "invalid_policy", [_issue("pack.toml", "profile_id", "format")]
        )
    match = matches[0]
    return text[: match.start()] + f'profile_id = "{profile_id}"' + text[match.end() :]


def _guide_texts(profile_id: str) -> dict[str, str]:
    """生成 init 固定双指南；只描述本机编辑与验证，不写入个人事实。"""
    agents = (
        f"# prompts/local/{profile_id} 本机策略包\n\n"
        "本目录是本机策略包，默认被 Git 忽略；不要写入个人事实、联系方式、真实聊天内容或凭据。\n\n"
        "只编辑 pack.toml、tone.md、phrase-bank.md、never-say.md。变更后运行 validate 与 preview；"
        "snapshot 会复制到 prompts/local/history，history 不是活动包。\n"
        "restore 只恢复到新的 prompts/local 直接子目录，并保留原 profile_id。\n"
    )
    return {"AGENTS.md": agents, "CLAUDE.md": "@AGENTS.md\n"}


def _hard_floor() -> dict[str, object]:
    """从领域唯一硬下限数据源返回 A/B/C 最小档位摘要。"""
    return {
        "A_allowed": sorted(HARD_FLOOR_A_KEYS),
        "B_minimum": sorted(HARD_FLOOR_B_KEYS),
        "C_only": sorted(HARD_FLOOR_C_KEYS),
    }


def _tier_groups(pack: PolicyPack) -> dict[str, list[str]]:
    """按 A/B/C 展开 45 个键，保持固定键顺序。"""
    groups: dict[str, list[str]] = {tier: [] for tier in POLICY_TIERS}
    for key in pack.tier_by_key:
        groups[pack.tier_by_key[key]].append(key)
    for values in groups.values():
        values.sort()
    return groups


def _validate_result(
    pack: PolicyPack, pack_rel: str, base_files: dict[str, str], guides: dict[str, str]
) -> dict[str, object]:
    """组装 validate/preview 共用的有效限制、硬下限、摘要与来源结果。"""
    limits = effective_limits(pack)
    return {
        "pack_path": pack_rel,
        "profile_id": pack.profile_id,
        "base_version": BASE_VERSION,
        "base_hash": compute_base_hash(base_files),
        "pack_hash": pack.pack_hash,
        "effective": limits,
        "tiers": _tier_groups(pack),
        "hard_floor": _hard_floor(),
        "sources": pack_sources(pack),
        "files": [f"{pack_rel}/{name}" for name in PACK_FILE_NAMES],
        "guides": [f"{pack_rel}/{name}" for name in sorted(guides)],
    }


def init_pack(
    repo: PolicyRepositoryPort,
    dest_rel: str,
    *,
    now: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """从公开示例生成仅 local 直接子目录的新包，失败不留可加载半包。"""
    del now
    dest, profile_id = _local_dest(dest_rel)
    if repo.path_exists(dest):
        raise PolicyError("policy_exists", [_issue(dest, None, "conflict")])
    with _policy_stage(process_log, "load"):
        example = repo.read_example_pack()
        base_files = repo.read_base()
    files = {name: example[name] for name in PACK_FILE_NAMES}
    guides = _guide_texts(profile_id)
    with _policy_stage(process_log, "validate"):
        files["pack.toml"] = _replace_profile_id(files["pack.toml"], profile_id)
        pack = _parse_pack(files, guides, dest)
    with _policy_stage(process_log, "publish"):
        staging = repo.make_staging_dir("prompts/local")
        published = False
        try:
            _write_payload(repo, staging, files, guides)
            _verify_staging(repo, staging, files, guides, pack.pack_hash)
            repo.promote(staging, dest)
            published = True
            final_files = repo.read_pack(dest)
            if compute_pack_hash(final_files) != pack.pack_hash:
                raise PolicyError("policy_io_error", [_issue(dest, None, "format")])
        except Exception:
            if published:
                _cleanup(repo, dest)
            _cleanup(repo, staging)
            raise
    return {
        "pack_path": dest,
        "profile_id": profile_id,
        "base_version": BASE_VERSION,
        "base_hash": compute_base_hash(base_files),
        "pack_hash": pack.pack_hash,
        "files": [f"{dest}/{name}" for name in PACK_FILE_NAMES],
        "guides": [f"{dest}/{name}" for name in sorted(guides)],
    }


def validate_pack(
    repo: PolicyRepositoryPort,
    pack_rel: str,
    *,
    now: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """读取 base 与活动包，返回 ok、有效限制、硬下限和摘要。"""
    del now
    normalized = _active_pack_path(pack_rel)
    with _policy_stage(process_log, "load"):
        base_files, files, guides = _load_active_bundle(repo, normalized)
    with _policy_stage(process_log, "validate"):
        pack = _parse_pack(files, guides, normalized)
    return _validate_result(pack, normalized, base_files, guides)


def preview_pack(
    repo: PolicyRepositoryPort,
    pack_rel: str,
    case_id: str,
    *,
    now: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """用独立合成输入纯渲染 system/developer/user 投影，不调用模型或读取答案。"""
    del now
    normalized = _active_pack_path(pack_rel)
    with _policy_stage(process_log, "load"):
        base_files, files, guides = _load_active_bundle(repo, normalized)
        case = repo.read_preview_case(case_id)
    with _policy_stage(process_log, "validate"):
        pack = _parse_pack(files, guides, normalized)
        try:
            validate_preview_case(case)
        except (ValueError, UnicodeError):
            raise PolicyError("unknown_case", [_issue(None, None, "format")]) from None
        if case.get("case_id") != case_id:
            raise PolicyError("unknown_case", [_issue(None, None, "format")])
    with _policy_stage(process_log, "render"):
        try:
            envelope = build_envelope(
                task_id=str(case["task_id"]),
                nonce=str(case["nonce"]),
                kind=str(case["kind"]),
                subject=case["subject"],
                inbound=str(case["inbound"]),
                context=list(case["context"]),
                allowed_facts=list(case["allowed_facts"]),
                base_files=base_files,
                pack=pack,
                now=str(case["now"]),
            )
            rendered = render_request(
                base_files, pack, envelope, now=str(case["now"])
            )
        except DecisionError as error:
            if error.reason_code == "input_too_large":
                raise PolicyError("invalid_policy", [_issue(None, None, "size")]) from None
            raise PolicyError("invalid_input", [_issue(None, None, "format")]) from None
    result = _validate_result(pack, normalized, base_files, guides)
    result["case_id"] = case_id
    result["now"] = str(case["now"])
    result["envelope"] = envelope
    result["rendered"] = rendered.to_dict()
    result["preview_only"] = True
    return result


def list_packs(
    repo: PolicyRepositoryPort,
    *,
    now: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """列出活动包与 history 快照的相对路径、身份、摘要与有效性。"""
    del now
    with _policy_stage(process_log, "load"):
        base_files = repo.read_base()
    base_hash = compute_base_hash(base_files)
    active: list[dict[str, object]] = []
    for rel in repo.list_pack_dirs():
        item: dict[str, object] = {
            "path": rel,
            "kind": "active",
            "profile_id": None,
            "base_hash": base_hash,
            "pack_hash": None,
            "valid": False,
        }
        try:
            with _policy_stage(process_log, "load"):
                files = repo.read_pack(rel)
                guides = repo.read_guides(rel)
            with _policy_stage(process_log, "validate"):
                pack = _parse_pack(files, guides, rel)
        except PolicyError:
            active.append(item)
            continue
        item["profile_id"] = pack.profile_id
        item["pack_hash"] = pack.pack_hash
        item["valid"] = True
        active.append(item)
    history: list[dict[str, object]] = []
    for rel in repo.list_history_dirs():
        manifest_rel = f"{rel}.local.json"
        item = {
            "path": rel,
            "kind": "history",
            "profile_id": None,
            "base_hash": None,
            "pack_hash": None,
            "valid": False,
        }
        try:
            with _policy_stage(process_log, "load"):
                manifest_data = repo.read_manifest(manifest_rel)
                files = repo.read_snapshot_pack(rel)
                guides = repo.read_snapshot_guides(rel)
            with _policy_stage(process_log, "validate"):
                manifest = _validate_manifest(manifest_data, manifest_rel)
                pack = _parse_pack(files, guides, rel, reason_code="policy_changed")
        except PolicyError:
            history.append(item)
            continue
        item["profile_id"] = manifest["profile_id"]
        item["base_hash"] = manifest["base_hash"]
        item["pack_hash"] = manifest["pack_hash"]
        item["valid"] = (
            compute_pack_hash(pack.files) == manifest["pack_hash"]
            and pack.profile_id == manifest["profile_id"]
        )
        history.append(item)
    return {"packs": active, "history": history}


def _validate_manifest(data: object, manifest_rel: str) -> dict[str, object]:
    """严格校验快照清单的固定键、身份、摘要、base 与创建时间。"""
    if not isinstance(data, dict) or set(data) != set(MANIFEST_KEYS):
        raise PolicyError("policy_changed", [_issue(manifest_rel, None, "format")])
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise PolicyError("policy_changed", [_issue(manifest_rel, "schema_version", "type")])
    if not is_profile_id(data["profile_id"]):
        raise PolicyError("policy_changed", [_issue(manifest_rel, "profile_id", "vocabulary")])
    source = data["source_pack"]
    if not isinstance(source, str):
        raise PolicyError("policy_changed", [_issue(manifest_rel, "source_pack", "type")])
    parts = source.replace("\\", "/").split("/")
    if (
        len(parts) != 3
        or parts[0] != "prompts"
        or parts[1] != "local"
        or parts[2] == "history"
        or parts[2].startswith(".")
        or not is_profile_id(parts[2])
    ):
        raise PolicyError("policy_changed", [_issue(manifest_rel, "source_pack", "path")])
    if not is_nonempty_text(data["base_version"]):
        raise PolicyError("policy_changed", [_issue(manifest_rel, "base_version", "type")])
    for key in ("base_hash", "pack_hash"):
        if not is_hash(data[key]):
            raise PolicyError("policy_changed", [_issue(manifest_rel, key, "type")])
    try:
        parse_utc(data["created_at"])
    except ValueError:
        raise PolicyError("policy_changed", [_issue(manifest_rel, "created_at", "type")]) from None
    return data


def _snapshot_identifier(now: str) -> str:
    """由毫秒 UTC 时间与随机后缀生成不覆盖的快照 ID。"""
    parsed = parse_utc(now)
    stamp = parsed.strftime("%Y%m%dT%H%M%S") + f"{parsed.microsecond // 1000:03d}Z"
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _write_payload(
    repo: PolicyRepositoryPort,
    staging: str,
    files: dict[str, str],
    guides: dict[str, str],
) -> None:
    """把四载荷与可选双指南按原字节写入暂存目录。"""
    for name in PACK_FILE_NAMES:
        repo.write_text_exact(f"{staging}/{name}", files[name])
    for name, text in guides.items():
        repo.write_text_exact(f"{staging}/{name}", text)


def _verify_staging(
    repo: PolicyRepositoryPort,
    staging: str,
    files: dict[str, str],
    guides: dict[str, str],
    expected_hash: str,
) -> None:
    """复核暂存目录清单与四个载荷原文，复制后摘要必须与来源一致。"""
    if set(repo.list_files(staging)) != set(PACK_FILE_NAMES) | set(guides):
        raise PolicyError("policy_io_error", [_issue(staging, None, "format")])
    staged: dict[str, str] = {}
    for name in PACK_FILE_NAMES:
        text = repo.read_text_exact(f"{staging}/{name}")
        if text != files[name]:
            raise PolicyError("policy_io_error", [_issue(staging, None, "format")])
        staged[name] = text
    if compute_pack_hash(staged) != expected_hash:
        raise PolicyError("policy_io_error", [_issue(staging, None, "format")])


def _cleanup(repo: PolicyRepositoryPort, *paths: str) -> None:
    """尽力清理本次操作自有路径；清理失败不遮蔽原始失败。"""
    for path in paths:
        try:
            repo.remove_owned(path)
        except PolicyError:
            pass


def snapshot_pack(
    repo: PolicyRepositoryPort,
    pack_rel: str,
    *,
    now: str | None = None,
    snapshot_id: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """把已校验活动包复制到 history 快照并写相邻清单，全部完成才发布成功。"""
    normalized = _active_pack_path(pack_rel)
    with _policy_stage(process_log, "load"):
        base_files, files, guides = _load_active_bundle(repo, normalized)
    with _policy_stage(process_log, "validate"):
        pack = _parse_pack(files, guides, normalized)
        if files != pack.files:
            raise PolicyError("invalid_policy", [_issue(normalized, None, "conflict")])
    created_at = now or utc_now()
    identifier = snapshot_id or _snapshot_identifier(created_at)
    if not is_snapshot_id(identifier):
        raise PolicyError("invalid_input", [_issue(None, None, "path")])
    snapshot_rel = f"prompts/local/history/{identifier}"
    manifest_rel = f"{snapshot_rel}.local.json"
    if repo.path_exists(snapshot_rel) or repo.path_exists(manifest_rel):
        raise PolicyError("policy_exists", [_issue(snapshot_rel, None, "conflict")])
    manifest = {
        "schema_version": 1,
        "profile_id": pack.profile_id,
        "source_pack": normalized,
        "base_version": BASE_VERSION,
        "base_hash": compute_base_hash(base_files),
        "pack_hash": pack.pack_hash,
        "created_at": created_at,
    }
    with _policy_stage(process_log, "publish"):
        staging = repo.make_staging_dir("prompts/local/history")
        staging_manifest = f"prompts/local/history/.staging-{uuid.uuid4().hex}.local.json"
        published_dir = False
        published_manifest = False
        try:
            _write_payload(repo, staging, pack.files, guides)
            _verify_staging(repo, staging, pack.files, guides, pack.pack_hash)
            _verify_source_unchanged(repo, normalized, pack.files, guides)
            repo.write_manifest(staging_manifest, manifest)
            repo.promote(staging, snapshot_rel)
            published_dir = True
            repo.promote(staging_manifest, manifest_rel)
            published_manifest = True
            _verify_source_unchanged(repo, normalized, pack.files, guides)
            final_pack = _read_snapshot_pack(repo, snapshot_rel)
            final_manifest = _validate_manifest(repo.read_manifest(manifest_rel), manifest_rel)
            if (
                final_pack.pack_hash != pack.pack_hash
                or final_manifest["pack_hash"] != pack.pack_hash
                or final_manifest["profile_id"] != pack.profile_id
            ):
                raise PolicyError("policy_changed", [_issue(snapshot_rel, None, "conflict")])
        except Exception:
            if published_manifest:
                _cleanup(repo, manifest_rel)
            if published_dir:
                _cleanup(repo, snapshot_rel)
            _cleanup(repo, staging_manifest, staging)
            raise
    return {
        "snapshot_path": snapshot_rel,
        "manifest_path": manifest_rel,
        "profile_id": pack.profile_id,
        "base_version": BASE_VERSION,
        "base_hash": compute_base_hash(base_files),
        "pack_hash": pack.pack_hash,
        "files": [f"{snapshot_rel}/{name}" for name in PACK_FILE_NAMES],
        "guides": [f"{snapshot_rel}/{name}" for name in sorted(guides)],
    }


def restore_pack(
    repo: PolicyRepositoryPort,
    snapshot_rel: str,
    dest_rel: str,
    activity: PolicyActivityPort,
    *,
    now: str | None = None,
    process_log: ProcessLog | None = None,
) -> dict[str, object]:
    """校验清单/快照/目标三值一致后恢复到新 local 子目录，不覆盖旧包。"""
    del now
    snapshot = _snapshot_path(snapshot_rel)
    dest, _ = _local_dest(dest_rel)
    manifest_rel = f"{snapshot}.local.json"
    with _policy_stage(process_log, "load"):
        manifest_data, snapshot_files, guides, base_files = _load_snapshot_bundle(
            repo, snapshot, manifest_rel
        )
    with _policy_stage(process_log, "validate"):
        manifest = _validate_manifest(manifest_data, manifest_rel)
        snapshot_pack = _parse_pack(
            snapshot_files, guides, snapshot, reason_code="policy_changed"
        )
        snapshot_hash = compute_pack_hash(snapshot_pack.files)
        if snapshot_hash != manifest["pack_hash"]:
            raise PolicyError("policy_changed", [_issue(snapshot, None, "conflict")])
        if snapshot_pack.profile_id != manifest["profile_id"]:
            raise PolicyError("policy_changed", [_issue(snapshot, "profile_id", "conflict")])
        base_hash = compute_base_hash(base_files)
        if manifest["base_hash"] != base_hash:
            raise PolicyError("policy_changed", [_issue(manifest_rel, "base_hash", "conflict")])
        if manifest["base_version"] != BASE_VERSION:
            raise PolicyError("policy_changed", [_issue(manifest_rel, "base_version", "conflict")])
        if repo.path_exists(dest):
            raise PolicyError("policy_exists", [_issue(dest, None, "conflict")])
        try:
            assessment = activity.assess_restore(
                profile_id=str(manifest["profile_id"]), dest_pack=dest
            )
        except Exception:
            assessment = "unknown"
        if assessment not in {"stopped", "active", "unknown"}:
            assessment = "unknown"
        if assessment != "stopped":
            raise PolicyError(
                "policy_changed",
                [_issue(dest, None, "conflict")],
                activity_state=assessment,
            )
    with _policy_stage(process_log, "publish"):
        staging = repo.make_staging_dir("prompts/local")
        published = False
        try:
            _write_payload(repo, staging, snapshot_pack.files, guides)
            _verify_staging(repo, staging, snapshot_pack.files, guides, snapshot_hash)
            repo.promote(staging, dest)
            published = True
            target_files = repo.read_pack(dest)
            target_pack, issues = parse_and_validate_pack(target_files)
            if issues:
                raise PolicyError("policy_changed", _prefix_issues(issues, dest))
            assert target_pack is not None
            target_hash = compute_pack_hash(target_pack.files)
            if not (
                target_hash == snapshot_hash == manifest["pack_hash"]
                and target_pack.profile_id == manifest["profile_id"]
            ):
                raise PolicyError("policy_changed", [_issue(dest, None, "conflict")])
        except Exception:
            if published:
                _cleanup(repo, dest)
            _cleanup(repo, staging)
            raise
    return {
        "pack_path": dest,
        "source_snapshot": snapshot,
        "profile_id": target_pack.profile_id,
        "base_version": BASE_VERSION,
        "base_hash": base_hash,
        "pack_hash": target_hash,
        "files": [f"{dest}/{name}" for name in PACK_FILE_NAMES],
        "guides": [f"{dest}/{name}" for name in sorted(guides)],
    }
