"""加载并校验场景清单、独立预期与夹具，不执行任何业务实现。

本模块只负责测试资产的结构、覆盖口径和路径边界；它不解析未读、不判定动作结果，
也不把资产自检通过当作产品验收通过。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCENARIO_ROOT = Path(__file__).resolve().parent.parent

MILESTONE_SCENARIO_COUNTS = {
    "M0": 3,
    "M1": 7,
    "M2": 15,
    "M3": 14,
    "M4": 11,
    "M5": 8,
    "M6": 8,
    "M7": 4,
    "M8": 3,
}
MILLESTONE_ORDER = tuple(MILESTONE_SCENARIO_COUNTS)
LAYERS = ("E0", "F", "T", "O", "W")
OFFLINE_LAYER = "F"
ASSET_STATUSES = ("READY_FOR_APP", "ASSET_ONLY", "DEVICE_GATE")
ENTRY_STATES = ("frozen", "unfrozen", "device", "none")
DEVICE_GATE_LAYERS = ("T", "O", "W")
SCENARIO_ID_RE = re.compile(r"^SC-[A-Z]\d{2}$")
VARIANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]"),
    re.compile(r"\\\\[A-Za-z0-9_.-]+[\\/]"),
    re.compile(r"(?i)(?:^|[\\/])(?:users|home)[\\/]"),
)


class AssetError(ValueError):
    """表示测试资产自身不满足已登记的契约，而不是产品行为失败。"""


@dataclass(frozen=True)
class LoadedAssets:
    """一次加载得到的场景清单与独立预期，保持只读语义。"""

    catalogue: dict[str, Any]
    oracles: dict[str, Any]
    root: Path

    def scenario_index(self) -> dict[str, dict[str, Any]]:
        """按场景 ID 建立索引，供报告和定向执行使用。"""
        return {item["id"]: item for item in self.catalogue["scenarios"]}


def load_json(path: Path) -> Any:
    """读取 UTF-8 JSON；失败只保留相对文件名与异常类别，不带绝对路径。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise AssetError(f"无法读取资产 {path.name}: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise AssetError(f"无法解析资产 {path.name}: JSONDecodeError") from exc


def load_assets(root: Path = SCENARIO_ROOT) -> LoadedAssets:
    """加载场景清单和独立预期，不读取任何产品输入或运行库。"""
    catalogue = load_json(root / "catalogue.json")
    oracles = load_json(root / "expected" / "oracles.json")
    return LoadedAssets(catalogue=catalogue, oracles=oracles, root=root)


def iter_variants(catalogue: dict[str, Any]):
    """按清单顺序产出 (场景, 变体)，供校验和报告复用。"""
    for scenario in catalogue.get("scenarios", []):
        for variant in scenario.get("variants", []):
            yield scenario, variant


def _check_relative_path(value: str) -> list[str]:
    """确认资产路径是项目内相对路径，拒绝盘符、UNC 和用户目录。"""
    errors: list[str] = []
    if not value or Path(value).is_absolute():
        return [f"路径不是相对路径: {value!r}"]
    if ".." in Path(value).parts:
        errors.append(f"路径包含越界片段: {value!r}")
    for pattern in FORBIDDEN_PATH_PATTERNS:
        if pattern.search(value):
            errors.append(f"路径疑似本机绝对或用户目录: {value!r}")
    return errors


def _scan_strings(value: Any, trail: str = "") -> list[tuple[str, str]]:
    """展开资产中的字符串，用于敏感路径与答案串扰检查。"""
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((trail, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_scan_strings(item, f"{trail}.{key}" if trail else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_scan_strings(item, f"{trail}[{index}]"))
    return found


def _within(child: Path, parent: Path) -> bool:
    """判断解析后的路径是否仍在给定目录内。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_fixture_target(base_dir: Path, relative: str, label: str) -> tuple[Path | None, list[str]]:
    """先完整校验相对路径、symlink 与解析后边界，再允许任何读取。"""
    errors = _check_relative_path(relative)
    if errors:
        return None, [f"{label}: {item}" for item in errors]
    base_resolved = base_dir.resolve()
    target = (base_dir / relative).resolve()
    if not _within(target, base_resolved):
        return None, [f"{label}: 解析后越出夹具目录"]
    if not target.is_file():
        return None, [f"{label}: 文件不存在或不是普通文件"]
    return target, []


def _validate_doctor_fixture(path: Path) -> list[str]:
    """按 M0 冻结契约检查合成设备清单的键、类型和唯一别名。"""
    errors: list[str] = []
    try:
        value = load_json(path)
    except AssetError as exc:
        return [str(exc)]
    if not isinstance(value, dict):
        return [f"{path.name}: 顶层必须是 JSON 对象"]
    if path.name.startswith("invalid_"):
        return errors
    expected_keys = {"devices", "selected_alias", "unlocked", "foreground_app"}
    if set(value) != expected_keys:
        errors.append(f"{path.name}: 顶层键必须恰为 {sorted(expected_keys)}")
        return errors
    devices = value.get("devices")
    if not isinstance(devices, list):
        return [f"{path.name}: devices 必须是数组"]
    aliases: list[str] = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict) or set(device) != {"alias", "state"}:
            errors.append(f"{path.name}: devices[{index}] 必须恰含 alias/state")
            continue
        if not isinstance(device["alias"], str) or not device["alias"]:
            errors.append(f"{path.name}: devices[{index}].alias 必须非空")
            continue
        aliases.append(device["alias"])
        if device["state"] not in ("device", "offline", "unauthorized"):
            errors.append(f"{path.name}: devices[{index}].state 不在词表")
    if len(aliases) != len(set(aliases)):
        errors.append(f"{path.name}: alias 重复")
    selected = value.get("selected_alias")
    if selected is not None and selected not in aliases:
        errors.append(f"{path.name}: selected_alias 不在清单中")
    if not isinstance(value.get("unlocked"), (bool, type(None))):
        errors.append(f"{path.name}: unlocked 必须是 bool 或 null")
    if value.get("foreground_app") not in ("boss", "other", "unknown"):
        errors.append(f"{path.name}: foreground_app 不在词表")
    return errors


def _validate_sensor_manifest(path: Path) -> list[str]:
    """按 M2 冻结 manifest 检查键集合、页路径和 profile 相邻约束。

    所有路径先做相对、symlink 与解析后边界校验；任一非法立即返回，绝不继续
    读取夹具目录之外的文件。
    """
    errors: list[str] = []
    try:
        value = load_json(path)
    except AssetError as exc:
        return [str(exc)]
    if not isinstance(value, dict):
        return [f"{path.name}: manifest 必须是 JSON 对象"]
    if path.name.startswith("invalid_"):
        return errors
    keys = {"profile_path", "xml_paths", "app_version", "observed_at", "baseline_rows", "coverage_proof"}
    if set(value) != keys:
        return [f"{path.name}: manifest 键必须恰为 {sorted(keys)}"]

    base_dir = path.parent
    resolved_base = base_dir.resolve()
    if not _within(resolved_base, resolved_base):
        return [f"{path.name}: 夹具目录不可解析"]

    profile_path = value.get("profile_path")
    if not isinstance(profile_path, str) or not profile_path:
        errors.append(f"{path.name}: profile_path 必须是非空字符串")
        return errors
    profile_file, path_errors = _safe_fixture_target(base_dir, profile_path, f"{path.name}.profile_path")
    errors.extend(path_errors)

    xml_paths = value.get("xml_paths")
    if not isinstance(xml_paths, list) or not xml_paths:
        errors.append(f"{path.name}: xml_paths 必须是非空数组")
        return errors
    if not all(isinstance(relative, str) and relative for relative in xml_paths):
        errors.append(f"{path.name}: xml_paths 每项必须是非空字符串")
        return errors
    if len(set(xml_paths)) != len(xml_paths):
        errors.append(f"{path.name}: xml_paths 不得重复")
    xml_files: list[Path] = []
    for index, relative in enumerate(xml_paths):
        target, path_errors = _safe_fixture_target(base_dir, relative, f"{path.name}.xml_paths[{index}]")
        errors.extend(path_errors)
        if target is not None:
            xml_files.append(target)
    if errors:
        return errors

    proof = value.get("coverage_proof")
    if not isinstance(proof, dict) or set(proof) != {"kind", "top_proven", "bottom_proven"}:
        return [f"{path.name}: coverage_proof 形状不符"]
    if proof["kind"] not in ("viewport", "full_traversal", "calibrated_global_zero"):
        return [f"{path.name}: coverage_proof.kind 不在词表"]
    for key in ("top_proven", "bottom_proven"):
        if not isinstance(proof[key], bool):
            errors.append(f"{path.name}: coverage_proof.{key} 必须是 bool")
    if not isinstance(value.get("app_version"), str) or not value["app_version"]:
        errors.append(f"{path.name}: app_version 必须是非空字符串")
    if not isinstance(value.get("observed_at"), str) or not value["observed_at"]:
        errors.append(f"{path.name}: observed_at 必须是非空字符串")
    rows = value.get("baseline_rows")
    if not isinstance(rows, list):
        errors.append(f"{path.name}: baseline_rows 必须是数组")
    else:
        seen: list[str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {"conversation_key", "preview_hash", "time_hint"}:
                errors.append(f"{path.name}: baseline_rows[{index}] 形状不符")
                continue
            key = row["conversation_key"]
            if not isinstance(key, str) or not key.startswith("boss:conv:"):
                errors.append(f"{path.name}: baseline_rows[{index}] 会话键前缀不符")
            elif key in seen:
                errors.append(f"{path.name}: baseline_rows[{index}] 会话键重复")
            else:
                seen.append(key)
            if not isinstance(row["preview_hash"], (str, type(None))):
                errors.append(f"{path.name}: baseline_rows[{index}].preview_hash 类型不符")
            if not isinstance(row["time_hint"], (str, type(None))):
                errors.append(f"{path.name}: baseline_rows[{index}].time_hint 类型不符")

    for xml_file in xml_files:
        try:
            root = ElementTree.parse(xml_file).getroot()
        except ElementTree.ParseError:
            errors.append(f"{path.name}: XML 解析失败: {xml_file.name}")
            continue
        except OSError as exc:
            errors.append(f"{path.name}: XML 读取失败: {xml_file.name}: {type(exc).__name__}")
            continue
        if root.tag != "hierarchy":
            errors.append(f"{path.name}: XML 根必须是 hierarchy")
    if profile_file is not None:
        try:
            profile = load_json(profile_file)
        except AssetError as exc:
            errors.append(str(exc))
        else:
            profile_keys = {
                "profile_id",
                "app",
                "version",
                "calibrated",
                "list_id_suffix",
                "row_id_suffix",
                "row_fields",
                "badge_region",
                "new_greeting",
                "tab_badge",
                "not_applicable_sources",
            }
            if not isinstance(profile, dict) or set(profile) != profile_keys:
                errors.append(f"{profile_file.name}: profile 键必须恰为 M2 最小字段")
            elif profile.get("app") != "boss":
                errors.append(f"{profile_file.name}: app 仅允许 boss")
    return errors



def _safe_asset_target(root: Path, relative: str) -> tuple[Path | None, list[str]]:
    """确认资产路径位于测试目录内且不是符号链接，再允许读取。"""
    errors = _check_relative_path(relative)
    if errors:
        return None, errors
    resolved_root = root.resolve()
    candidate = root / relative
    if candidate.is_symlink():
        return None, [f"夹具路径不得是符号链接: {relative}"]
    resolved = candidate.resolve()
    if not _within(resolved, resolved_root):
        return None, [f"夹具路径解析后越界: {relative}"]
    return resolved, []


M6_PROHIBITED_KEYS = {
    "expected",
    "first_observed_at",
    "handling_started_at",
    "stage_preempted",
    "pause_preemption_limit",
    "preemption_limit",
}
M6_PROHIBITED_EVENTS = {
    "unread_handling_started",
    "pause_preemption_limit",
    "stage_preempted",
    "preemption_observed",
    "job_started",
}


def _walk_keys(value: Any, trail: str = "") -> list[tuple[str, str]]:
    """递归列出对象键路径与事件名，用于刺激/结果分离检查。"""
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((trail, str(key)))
            found.extend(_walk_keys(item, f"{trail}.{key}" if trail else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_keys(item, f"{trail}[{index}]"))
    elif isinstance(value, str):
        found.append((trail, value))
    return found


def _validate_m6_stimuli(path: Path) -> list[str]:
    """M6 events 只允许外部刺激，禁止把产品观察或暂停结果当输入。"""
    try:
        value = load_json(path)
    except AssetError as exc:
        return [str(exc)]
    if not isinstance(value, dict):
        return [f"{path.name}: M6 刺激必须是 JSON 对象"]
    errors: list[str] = []
    if value.get("role") != "driver_stimuli_only":
        errors.append(f"{path.name}: role 必须为 driver_stimuli_only")
    for trail, token in _walk_keys(value):
        if token in M6_PROHIBITED_KEYS:
            errors.append(f"{path.name}:{trail} 含产品结果字段 {token}")
        if trail.endswith(".event") and token in M6_PROHIBITED_EVENTS:
            errors.append(f"{path.name}:{trail} 含产品结果事件 {token}")
    return errors


def _validate_fixture_files(root: Path, fixture: str) -> list[str]:
    """检查夹具是否存在且只读取测试目录内的普通文件。"""
    path, errors = _safe_asset_target(root, fixture)
    if path is None:
        return errors
    if not path.exists():
        return [f"夹具不存在: {fixture}"]
    if path.is_dir():
        candidates = [path / "manifest.json", path / "fixture.json"]
        if not any(item.is_file() for item in candidates) and not any(path.glob("invalid_*.json")):
            errors.append(f"夹具目录缺少 manifest.json/fixture.json: {fixture}")
        for item in path.rglob("*.json"):
            if item.is_symlink() or not _within(item.resolve(), path):
                errors.append(f"{item.name}: 夹具内文件越界或为链接")
                continue
            try:
                value = load_json(item)
            except AssetError as exc:
                errors.append(str(exc))
                continue
            for trail, text in _scan_strings(value):
                if "expected/oracles.json" in text or "expected_ref" in text:
                    errors.append(f"{item.name}:{trail} 引用了独立答案路径")
        if (path / "manifest.json").is_file():
            errors.extend(_validate_sensor_manifest(path / "manifest.json"))
        return errors
    if path.suffix.lower() != ".json":
        return errors
    try:
        value = load_json(path)
    except AssetError as exc:
        return errors + [str(exc)]
    for trail, text in _scan_strings(value):
        if "expected/oracles.json" in text or "expected_ref" in text:
            errors.append(f"{fixture}:{trail} 引用了独立答案路径")
    if "m6" in path.parts and "events" in path.parts:
        errors.extend(_validate_m6_stimuli(path))
    if "doctor" in path.parts:
        errors.extend(_validate_doctor_fixture(path))
    elif path.name == "manifest.json":
        errors.extend(_validate_sensor_manifest(path))
    return errors



def validate_assets(assets: LoadedAssets) -> dict[str, Any]:
    """执行资产自检并返回结构化结果；不运行产品、不产生业务 PASS。"""
    errors: list[str] = []
    warnings: list[str] = []
    root = assets.root
    catalogue = assets.catalogue
    oracles = assets.oracles

    if catalogue.get("schema_version") != 1:
        errors.append("catalogue.schema_version 必须为 1")
    if oracles.get("schema_version") != 1:
        errors.append("oracles.schema_version 必须为 1")
    scenarios = catalogue.get("scenarios")
    if not isinstance(scenarios, list):
        return {"ok": False, "errors": ["catalogue.scenarios 必须是数组"], "warnings": []}

    ids = [item.get("id") for item in scenarios if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        errors.append("场景 ID 重复")
    expected_total = sum(MILESTONE_SCENARIO_COUNTS.values())
    if len(scenarios) != expected_total:
        errors.append(f"场景总数必须为 {expected_total}，实际 {len(scenarios)}")
    counts: dict[str, int] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            errors.append("场景项必须是对象")
            continue
        scenario_id = scenario.get("id", "")
        if not isinstance(scenario_id, str) or not SCENARIO_ID_RE.match(scenario_id):
            errors.append(f"非法场景 ID: {scenario_id!r}")
            continue
        milestone = scenario.get("milestone")
        if milestone not in MILESTONE_SCENARIO_COUNTS:
            errors.append(f"{scenario_id}: 未知里程碑 {milestone!r}")
        else:
            counts[milestone] = counts.get(milestone, 0) + 1
        for key in ("title", "source", "preconditions", "offline_subscenarios", "coverage_status", "variants"):
            if key not in scenario:
                errors.append(f"{scenario_id}: 缺少 {key}")
        if not scenario.get("variants"):
            errors.append(f"{scenario_id}: 至少需要一个变体")
        seen_variants: set[str] = set()
        for variant in scenario.get("variants", []):
            if not isinstance(variant, dict):
                errors.append(f"{scenario_id}: 变体必须是对象")
                continue
            variant_id = variant.get("id", "")
            if not isinstance(variant_id, str) or not VARIANT_ID_RE.match(variant_id):
                errors.append(f"{scenario_id}: 非法变体 ID {variant_id!r}")
                continue
            if variant_id in seen_variants:
                errors.append(f"{scenario_id}: 变体 ID 重复 {variant_id}")
            seen_variants.add(variant_id)
            ref = f"{scenario_id}/{variant_id}"
            layer = variant.get("layer")
            asset_status = variant.get("asset_status")
            entry_state = variant.get("entry_state")
            if layer not in LAYERS:
                errors.append(f"{ref}: layer 不在词表")
            if asset_status not in ASSET_STATUSES:
                errors.append(f"{ref}: asset_status 不在词表")
            if entry_state not in ENTRY_STATES:
                errors.append(f"{ref}: entry_state 不在词表")
            if layer in DEVICE_GATE_LAYERS:
                if not variant.get("device_gate"):
                    errors.append(f"{ref}: T/O/W 变体必须显式 device_gate=true")
                if asset_status != "DEVICE_GATE":
                    errors.append(f"{ref}: T/O/W 变体不得标为离线资产完成")
                if entry_state != "device":
                    errors.append(f"{ref}: T/O/W 变体 entry_state 必须为 device")
            elif variant.get("device_gate"):
                errors.append(f"{ref}: 非 T/O/W 变体不得标 device_gate")
            if layer == OFFLINE_LAYER and asset_status == "DEVICE_GATE":
                errors.append(f"{ref}: F 变体不得标 DEVICE_GATE")
            if asset_status == "READY_FOR_APP":
                if entry_state != "frozen":
                    errors.append(f"{ref}: READY_FOR_APP 必须绑定 frozen 入口")
                if not variant.get("steps"):
                    errors.append(f"{ref}: READY_FOR_APP 必须给出可执行步骤")
            if asset_status == "ASSET_ONLY":
                if not variant.get("required_api") and not variant.get("setup"):
                    warnings.append(f"{ref}: ASSET_ONLY 未说明缺失入口或前置")
            if variant.get("expected_ref") != ref:
                errors.append(f"{ref}: expected_ref 必须指向自身且独立存档")
            if ref not in oracles.get("variants", {}):
                errors.append(f"{ref}: 缺少独立预期")
            fixture = variant.get("fixture")
            if fixture:
                errors.extend(_validate_fixture_files(root, fixture))
            missing_inputs = variant.get("expected_missing_inputs", [])
            if not isinstance(missing_inputs, list):
                errors.append(f"{ref}: expected_missing_inputs 必须是数组")
            else:
                for relative in missing_inputs:
                    if not isinstance(relative, str) or not relative.startswith("tests/scenarios/fixtures/"):
                        errors.append(f"{ref}: expected_missing_inputs 路径必须位于 fixtures 下")
                        continue
                    path_errors = _check_relative_path(relative)
                    if path_errors:
                        errors.extend(f"{ref}: {item}" for item in path_errors)
                        continue
                    resolved = (root.parent.parent / relative).resolve()
                    if not _within(resolved, (root / "fixtures").resolve()):
                        errors.append(f"{ref}: expected_missing_inputs 解析后越界")
                    elif resolved.exists():
                        errors.append(f"{ref}: 登记为缺失的输入实际存在")

    for milestone, expected_count in MILESTONE_SCENARIO_COUNTS.items():
        if counts.get(milestone, 0) != expected_count:
            errors.append(f"{milestone} 场景数必须为 {expected_count}，实际 {counts.get(milestone, 0)}")
    if list(MILESTONE_SCENARIO_COUNTS) != list(MILLESTONE_ORDER):
        errors.append("里程碑顺序常量不一致")

    catalogue_refs = {f"{scenario['id']}/{variant['id']}" for scenario, variant in iter_variants(catalogue)}
    oracle_variants = oracles.get("variants", {})
    if not isinstance(oracle_variants, dict):
        errors.append("oracles.variants 必须是对象")
        oracle_variants = {}
    for ref in sorted(set(oracle_variants) - catalogue_refs):
        errors.append(f"{ref}: 独立预期没有对应场景变体")
    for ref in sorted(catalogue_refs - set(oracle_variants)):
        errors.append(f"{ref}: 场景变体没有独立预期")
    for ref, oracle in oracle_variants.items():
        if not isinstance(oracle, dict):
            errors.append(f"{ref}: 预期项必须是对象")
            continue
        for key in ("scenario_id", "variant_id", "layer", "expected_summary", "assertions", "acceptance", "forbidden", "source"):
            if key not in oracle:
                errors.append(f"{ref}: 预期缺少 {key}")
        if not oracle.get("assertions"):
            errors.append(f"{ref}: 至少需要一条独立判据")
        if oracle.get("acceptance") is not None and oracle.get("layer") != OFFLINE_LAYER:
            errors.append(f"{ref}: 仅 F 变体可声明可执行验收")
        for trail, text in _scan_strings(oracle):
            for pattern in FORBIDDEN_PATH_PATTERNS:
                if pattern.search(text):
                    errors.append(f"{ref}:{trail} 含本机绝对或用户路径")

    layer_counts: dict[str, int] = {layer: 0 for layer in LAYERS}
    status_counts: dict[str, int] = {status: 0 for status in ASSET_STATUSES}
    for _, variant in iter_variants(catalogue):
        layer_counts[variant.get("layer")] = layer_counts.get(variant.get("layer"), 0) + 1
        status_counts[variant.get("asset_status")] = status_counts.get(variant.get("asset_status"), 0) + 1
    ready = [
        f"{scenario['id']}/{variant['id']}"
        for scenario, variant in iter_variants(catalogue)
        if variant.get("asset_status") == "READY_FOR_APP"
    ]
    if not ready:
        warnings.append("没有绑定 frozen 入口的离线变体，严格验收必然 BLOCKED")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "scenario_count": len(scenarios),
        "variant_count": sum(len(item.get("variants", [])) for item in scenarios if isinstance(item, dict)),
        "milestone_counts": counts,
        "layer_counts": layer_counts,
        "status_counts": status_counts,
        "ready_for_app": len(ready),
    }
