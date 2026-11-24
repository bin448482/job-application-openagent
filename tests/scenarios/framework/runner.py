"""场景资产自检与真实产品离线验收的分流入口。

`assets` 模式只证明清单、独立预期和夹具自洽；`acceptance` 模式先校验资产，再在
隔离运行根中复制真实交付的 `app/` 与合成夹具，用 tripwire 拒绝真实 ADB/网络，
缺少应用或入口未冻结时显式 BLOCKED，绝不以零项或全 skip 给出总体 PASS。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .compare import compare_invocation
from .model import (
    AssetError,
    LoadedAssets,
    SCENARIO_ROOT,
    iter_variants,
    load_assets,
    validate_assets,
)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 3
RUN_ROOT = Path("data") / "evals" / "scenario-runs"
# 只有已独立验收并进入稳定 main 的里程碑才允许执行；扩展前先更新指南。
IMPLEMENTED_MILESTONES = {"M0", "M1", "M2", "M3", "M4"}


def _utc_stamp() -> str:
    """生成报告目录使用的 UTC 时间戳，不使用本机时区。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _write_report(report: dict[str, Any], target: Path, run_id: str) -> Path:
    """把本机运行报告写入被忽略的 data/evals 子目录并返回路径。"""
    directory = target / RUN_ROOT / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _relative(path: Path, target: Path) -> str:
    """把项目内路径转为报告用相对路径，避免公开绝对路径。"""
    try:
        return path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return path.name


def _parse_stdout(raw: str) -> tuple[Any, bool]:
    """把 stdout 解析为单个 JSON 值，拒绝前后附加文本。"""
    stripped = raw.strip()
    if not stripped:
        return None, False
    try:
        return json.loads(stripped), True
    except json.JSONDecodeError:
        return None, False


SITECUSTOMIZE = '''"""隔离运行的边界 tripwire：记录并拒绝网络、外部进程和真实设备命令。"""
import os
import socket
import subprocess

LOG = os.environ["SCENARIO_TRIPWIRE_LOG"]


def _record(kind):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(kind + "\\n")


def _deny(kind):
    _record(kind)
    raise RuntimeError("scenario boundary blocked: " + kind)


def _blocked_connect(self, address):
    _deny("socket.connect")


def _blocked_create_connection(address, *args, **kwargs):
    _deny("socket.create_connection")


def _blocked_getaddrinfo(*args, **kwargs):
    _deny("socket.getaddrinfo")


def _blocked_popen(*args, **kwargs):
    _deny("subprocess.Popen")


def _blocked_system(*args, **kwargs):
    _deny("os.system")


socket.socket.connect = _blocked_connect
socket.create_connection = _blocked_create_connection
socket.getaddrinfo = _blocked_getaddrinfo
subprocess.Popen = _blocked_popen
os.system = _blocked_system
'''


class IsolationError(RuntimeError):
    """表示目标仓库的复制源含链接、重解析点或越界路径。"""


def _within(child: Path, parent: Path) -> bool:
    """用相对路径判断解析结果是否仍在父目录内，避免同名前缀绕过。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_like(path: Path) -> bool:
    """识别 symlink 与 Windows junction/reparse 点。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _reject_link_ancestors(source: Path, stop: Path, label: str) -> None:
    """拒绝源自身及其到目标根之间的 symlink/junction。"""
    stop_absolute = stop.absolute()
    current = source.absolute()
    while True:
        if _is_link_like(current):
            raise IsolationError(f"{label} 自身或父路径含链接/junction")
        if current == stop_absolute or current.parent == current:
            break
        current = current.parent


def _walk_source(current: Path, resolved_source: Path, label: str) -> None:
    """逐层扫描源树；链接或 junction 在继续下钻前拒绝。"""
    try:
        entries = list(os.scandir(current))
    except OSError as exc:
        raise IsolationError(f"{label} 无法读取目录: {type(exc).__name__}") from exc
    for entry in entries:
        item = Path(entry.path)
        if _is_link_like(item):
            raise IsolationError(f"{label} 含符号链接或 junction")
        if not _within(item.resolve(), resolved_source):
            raise IsolationError(f"{label} 含越界路径")
        if entry.is_dir(follow_symlinks=False):
            _walk_source(item, resolved_source, label)


def _validate_source_tree(source: Path, stop: Path, label: str) -> None:
    """复制前验证源自身/父路径与整棵源树，拒绝链接、junction 与越界。"""
    _reject_link_ancestors(source, stop, label)
    if not source.is_dir():
        raise IsolationError(f"{label} 目录不存在")
    resolved_source = source.resolve()
    if not _within(resolved_source, stop.resolve()):
        raise IsolationError(f"{label} 解析后越出目标根")
    _walk_source(source, resolved_source, label)


FIXTURE_TOKEN = re.compile(r"tests/scenarios/fixtures/[A-Za-z0-9_./-]+")
M0_PROBE_PATH = "tests/scenarios/framework/m0_protocol_probe.py"
M1_PROBE_PATH = "tests/scenarios/framework/m1_probe.py"
M2_PROBE_PATH = "tests/scenarios/framework/m2_probe.py"
M3_PROBE_PATH = "tests/scenarios/framework/m3_probe.py"
M4_PROBE_PATH = "tests/scenarios/framework/m4_probe.py"
M4C_PROBE_PATH = "tests/scenarios/framework/m4c_probe.py"
EXPECTED_FILE_NAMES = {"expected.json", "oracles.json"}


def _is_expected_name(path: Path) -> bool:
    """识别不得进入 SUT 输入树的答案文件。"""
    return path.name in EXPECTED_FILE_NAMES or path.name.startswith("expected.")


def _iter_command_strings(variant: dict[str, Any]):
    """展开普通步骤与 concurrent 步骤中的命令字符串。"""
    for step in variant.get("steps", []):
        if isinstance(step, str):
            yield step
        elif isinstance(step, dict):
            for command in step.get("concurrent", []):
                if isinstance(command, str):
                    yield command


def _collect_selected_inputs(target: Path, variant: dict[str, Any]) -> list[Path]:
    """收集命令与 manifest 实际引用的合成输入，排除 expected/答案文件。

    只有变体显式登记的缺失输入允许不存在；其余缺失一律阻断，避免把产品
    错误处理场景提前在隔离层吃掉。
    """
    fixtures_root = (target / "tests" / "scenarios" / "fixtures").resolve()
    missing_allowed = {
        (target / relative).resolve()
        for relative in variant.get("expected_missing_inputs", [])
        if isinstance(relative, str)
    }
    selected: list[Path] = []
    queue: list[Path] = []
    for command in _iter_command_strings(variant):
        for token in FIXTURE_TOKEN.findall(command):
            queue.append(target / token.rstrip("/"))
    fixture = variant.get("fixture")
    if fixture:
        queue.append(target / "tests" / "scenarios" / fixture)
    seen: set[Path] = set()
    while queue:
        candidate = queue.pop(0)
        if candidate.is_symlink():
            raise IsolationError("选定输入不得是符号链接")
        resolved = candidate.resolve()
        if not _within(resolved, fixtures_root):
            raise IsolationError("选定输入越出夹具根")
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            for item in sorted(resolved.rglob("*")):
                if item.is_file() and not _is_expected_name(item):
                    queue.append(item)
            continue
        if not resolved.is_file():
            if resolved in missing_allowed:
                continue
            raise IsolationError("选定输入不存在")
        if _is_expected_name(resolved):
            continue
        selected.append(resolved)
        if resolved.parent.name == "probe" and resolved.suffix == ".json":
            try:
                probe_request = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                probe_request = None
            if isinstance(probe_request, dict) and isinstance(probe_request.get("fixture"), str):
                queue.append(target / probe_request["fixture"])
            if isinstance(probe_request, dict) and isinstance(probe_request.get("extra_fixture"), str):
                queue.append(target / probe_request["extra_fixture"])
            for key in ("initial_fixture", "targeted_fixture", "previous_fixture", "current_fixture"):
                if isinstance(probe_request, dict) and isinstance(probe_request.get(key), str):
                    queue.append(target / probe_request[key])
        if resolved.name in ("manifest.json", "read.json", "fixture.json"):
            try:
                manifest = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict):
                for key in ("profile_path",):
                    relative = manifest.get(key)
                    if isinstance(relative, str):
                        queue.append(resolved.parent / relative)
                for relative in manifest.get("xml_paths") or []:
                    if isinstance(relative, str):
                        queue.append(resolved.parent / relative)
                for relative in manifest.get("hierarchy_paths") or []:
                    if isinstance(relative, str):
                        queue.append(resolved.parent / relative)
                pixel = manifest.get("pixel")
                if isinstance(pixel, dict):
                    for key in ("calibration_path", "raw_path"):
                        relative = pixel.get(key)
                        if isinstance(relative, str):
                            queue.append(resolved.parent / relative)
    return selected


def _prepare_isolation(
    target: Path,
    run_id: str,
    scenario_id: str,
    variant_id: str,
    variant: dict[str, Any],
) -> Path:
    """校验复制源后只把选定合成输入与交付 app 复制到隔离根。"""
    source_app = target / "app"
    source_fixtures = target / "tests" / "scenarios" / "fixtures"
    _validate_source_tree(source_app, target, "app")
    _validate_source_tree(source_fixtures, target, "fixtures")
    selected = _collect_selected_inputs(target, variant)
    root = target / RUN_ROOT / run_id / f"iso-{scenario_id}-{variant_id}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copytree(source_app, root / "app", symlinks=False)
    if any(M1_PROBE_PATH in command for command in _iter_command_strings(variant)):
        source_m1 = target / M1_PROBE_PATH
        _reject_link_ancestors(source_m1, target, "m1_probe")
        if source_m1.is_symlink():
            raise IsolationError("M1 探针不得是符号链接")
        resolved_m1 = source_m1.resolve()
        if not _within(resolved_m1, target.resolve()) or not resolved_m1.is_file():
            raise IsolationError("M1 探针路径越界或不存在")
        destination_m1 = root / M1_PROBE_PATH
        destination_m1.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved_m1, destination_m1)
    if any(M2_PROBE_PATH in command for command in _iter_command_strings(variant)):
        source_m2 = target / M2_PROBE_PATH
        _reject_link_ancestors(source_m2, target, "m2_probe")
        if source_m2.is_symlink():
            raise IsolationError("M2 探针不得是符号链接")
        resolved_m2 = source_m2.resolve()
        if not _within(resolved_m2, target.resolve()) or not resolved_m2.is_file():
            raise IsolationError("M2 探针路径越界或不存在")
        destination_m2 = root / M2_PROBE_PATH
        destination_m2.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved_m2, destination_m2)
    if any(M3_PROBE_PATH in command for command in _iter_command_strings(variant)):
        source_m3 = target / M3_PROBE_PATH
        _reject_link_ancestors(source_m3, target, "m3_probe")
        if source_m3.is_symlink():
            raise IsolationError("M3 探针不得是符号链接")
        resolved_m3 = source_m3.resolve()
        if not _within(resolved_m3, target.resolve()) or not resolved_m3.is_file():
            raise IsolationError("M3 探针路径越界或不存在")
        destination_m3 = root / M3_PROBE_PATH
        destination_m3.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved_m3, destination_m3)
    if any(M4_PROBE_PATH in command for command in _iter_command_strings(variant)):
        source_m4 = target / M4_PROBE_PATH
        _reject_link_ancestors(source_m4, target, "m4_probe")
        if source_m4.is_symlink():
            raise IsolationError("M4 探针不得是符号链接")
        resolved_m4 = source_m4.resolve()
        if not _within(resolved_m4, target.resolve()) or not resolved_m4.is_file():
            raise IsolationError("M4 探针路径越界或不存在")
        destination_m4 = root / M4_PROBE_PATH
        destination_m4.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved_m4, destination_m4)
    if any(M4C_PROBE_PATH in command for command in _iter_command_strings(variant)):
        source_m4c = target / M4C_PROBE_PATH
        _reject_link_ancestors(source_m4c, target, "m4c_probe")
        if source_m4c.is_symlink():
            raise IsolationError("M4c 探针不得是符号链接")
        resolved_m4c = source_m4c.resolve()
        if not _within(resolved_m4c, target.resolve()) or not resolved_m4c.is_file():
            raise IsolationError("M4c 探针路径越界或不存在")
        destination_m4c = root / M4C_PROBE_PATH
        destination_m4c.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved_m4c, destination_m4c)
    if any(M0_PROBE_PATH in command for command in _iter_command_strings(variant)):
        source_probe = target / M0_PROBE_PATH
        _reject_link_ancestors(source_probe, target, "m0_protocol_probe")
        if source_probe.is_symlink():
            raise IsolationError("M0 探针边界替身不得是符号链接")
        resolved_probe = source_probe.resolve()
        if not _within(resolved_probe, target.resolve()):
            raise IsolationError("M0 探针边界替身解析后越出目标根")
        if not resolved_probe.is_file():
            raise IsolationError("M0 探针边界替身不存在")
        destination = root / M0_PROBE_PATH
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved_probe, destination)
    for source in selected:
        relative = source.relative_to((target / "tests" / "scenarios" / "fixtures").resolve())
        destination = root / "tests" / "scenarios" / "fixtures" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    shared_inputs = variant.get("shared_inputs") or []
    if not isinstance(shared_inputs, list) or any(
        not isinstance(item, str) for item in shared_inputs
    ):
        raise IsolationError("共享输入必须是相对路径数组")
    for relative in shared_inputs:
        source_root = target / relative
        _validate_source_tree(source_root, target, "共享输入")
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or _is_expected_name(source):
                continue
            if _is_link_like(source):
                raise IsolationError("共享输入含符号链接或 junction")
            destination = root / relative / source.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    fixture_copies = variant.get("fixture_copies") or {}
    if not isinstance(fixture_copies, dict):
        raise IsolationError("fixture_copies 必须是对象")
    for destination_rel, relative in fixture_copies.items():
        if not isinstance(destination_rel, str) or not isinstance(relative, str):
            raise IsolationError("fixture_copies 必须是相对路径对象")
        destination_root = (root / destination_rel).resolve()
        if not _within(destination_root, root.resolve()):
            raise IsolationError("fixture_copies 目标越界")
        source_root = target / "tests" / "scenarios" / "fixtures" / relative
        _validate_source_tree(source_root, target, "复制夹具")
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or _is_expected_name(source):
                continue
            if source.name in {"AGENTS.md", "CLAUDE.md"}:
                continue
            if _is_link_like(source):
                raise IsolationError("复制夹具含符号链接或 junction")
            target_file = destination_root / source.relative_to(source_root)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target_file)
    local_packs = variant.get("local_packs") or {}
    if not isinstance(local_packs, dict):
        raise IsolationError("local_packs 必须是对象")
    for name, relative in local_packs.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise IsolationError("local 包名非法")
        source_root = target / "tests" / "scenarios" / "fixtures" / relative
        _validate_source_tree(source_root, target, "local 包")
        destination = root / "prompts" / "local" / name
        if destination.exists():
            raise IsolationError("local 包目标重复")
        destination.mkdir(parents=True)
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or _is_expected_name(source):
                continue
            if source.name in {"AGENTS.md", "CLAUDE.md"}:
                continue
            if _is_link_like(source):
                raise IsolationError("local 包含符号链接或 junction")
            target_file = destination / source.relative_to(source_root)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target_file)
    junctions = variant.get("junctions") or {}
    if not isinstance(junctions, dict):
        raise IsolationError("junctions 必须是对象")
    for link_rel, target_rel in junctions.items():
        if not isinstance(link_rel, str) or not isinstance(target_rel, str):
            raise IsolationError("junction 路径必须是字符串")
        link = (root / link_rel).resolve(strict=False)
        target_path = (root / target_rel).resolve()
        if not _within(link, root.resolve()) or not _within(target_path, root.resolve()):
            raise IsolationError("junction 路径越界")
        if link.exists() or _is_link_like(link):
            raise IsolationError("junction 目标已存在")
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0 or not _is_link_like(link):
                raise IsolationError("无法创建合成 junction")
        else:
            link.symlink_to(target_path, target_is_directory=True)
    (root / "data" / "evals" / "scenario-runs" / run_id).mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir()
    (root / "tmp").mkdir()
    (root / "localappdata").mkdir()
    (root / "appdata").mkdir()
    (root / "home").mkdir()
    (root / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    return root


ENV_ALLOWLIST = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)


def _isolation_env(root: Path, suffix: str = "") -> dict[str, str]:
    """构造最小隔离环境：并发子进程各自使用独立 tripwire 日志。"""
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(
        {
            "PYTHONPATH": str(root),
            "PYTHONNOUSERSITE": "1",
            "LOCALAPPDATA": str(root / "localappdata"),
            "APPDATA": str(root / "appdata"),
            "HOME": str(root / "home"),
            "USERPROFILE": str(root / "home"),
            "TEMP": str(root / "tmp"),
            "TMP": str(root / "tmp"),
            "PATH": str(root / "bin"),
            "SCENARIO_TRIPWIRE_LOG": str(root / f"tripwire{suffix}.log"),
        }
    )
    return env


def _run_step(
    command: str,
    root: Path,
    run_id: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """在隔离根内用参数数组运行生产 CLI；禁止 shell 与真实边界调用。"""
    tokens = shlex.split(command, posix=True)
    if not tokens or tokens[0] != "python":
        return {
            "command": command,
            "exit_code": None,
            "stdout_json": None,
            "stdout_json_ok": False,
            "stdout_text": "",
            "stderr": "",
            "error": "仅允许以 python 入口的显式步骤",
        }
    argv = [sys.executable] + tokens[1:]
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            env=_isolation_env(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": command,
            "exit_code": None,
            "stdout_json": None,
            "stdout_json_ok": False,
            "stdout_text": "",
            "stderr": "",
            "error": type(exc).__name__,
        }
    stdout_json, ok = _parse_stdout(completed.stdout)
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout_json": stdout_json,
        "stdout_json_ok": ok,
        "stdout_text": completed.stdout,
        "stderr": completed.stderr,
        "error": None,
        "run_id": run_id,
    }


LOG_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "ts",
        "correlation_id",
        "stage",
        "operation",
        "result",
        "reason_code",
        "error_category",
        "duration_ms",
        "command",
        "exit_code",
        "mode",
        "config_source",
        "device_count",
        "case_index",
    }
)
LOG_STAGES = frozenset(
    {
        "start",
        "arguments",
        "config",
        "mode",
        "fixture",
        "probe",
        "adb_devices",
        "adb_window",
        "decision",
        "store",
        "sense",
        "read",
        "adb_hierarchy",
        "adb_screen",
        "adb_evidence",
        "policy",
        "eval",
        "output",
        "exit",
    }
)
LOG_RESULTS = frozenset({"started", "ok", "undetermined", "failed"})
LOG_OPERATIONS = frozenset(
    {
        "init",
        "parse",
        "aggregate",
        "stop_reason",
        "open_store",
        "upsert_snapshot",
        "claim",
        "start",
        "renew",
        "finish",
        "recover",
        "unresolved_conversations",
        "load",
        "page",
        "job_list",
        "job_detail",
        "unread",
        "conversation",
        "pixel",
        "merge",
        "gate",
        "acceptance",
        "authorize",
        "bridge",
        "case",
        "eval",
        "holdout",
        "list",
        "preview",
        "publish",
        "render",
        "report",
        "restore",
        "snapshot",
        "suite",
        "validate",
        "verify",
    }
)
LOG_REASON_CODES = frozenset(
    {
        "invalid_input",
        "invalid_config",
        "invalid_fixture",
        "device_disconnected",
        "device_ambiguous",
        "device_offline",
        "device_unauthorized",
        "device_locked",
        "wrong_page",
        "invalid_xml",
        "incomplete_snapshot",
        "stale_snapshot",
        "uncalibrated_profile",
        "identity_ambiguous",
        "unread_unverified",
        "needs_targeted_verification",
        "ambiguous_signal",
        "uncalibrated_region",
        "calibration_mismatch",
        "missing_signal",
        "stable_identity_unverified",
        "unverified_identity",
        "not_ok",
        "field_unknown",
        "incomplete",
        "no_target",
        "device_gate_unavailable",
        "lease_busy",
        "stale_lease",
        "owner_not_stopped",
        "clock_changed",
        "store_busy",
        "store_io_error",
        "db_version_unsupported",
        "migration_failed",
        "authorization_required",
        "bridge_auth",
        "bridge_disconnected",
        "bridge_protocol",
        "bridge_quota",
        "bridge_roles",
        "bridge_start_failed",
        "bridge_timeout",
        "bridge_unavailable",
        "decision_timeout",
        "eval_failed",
        "input_too_large",
        "invalid_decision",
        "invalid_policy",
        "invalid_provider",
        "policy_changed",
        "policy_exists",
        "policy_io_error",
        "stale_decision",
        "stale_evidence",
        "unknown_case",
    }
)
LOG_ERROR_CATEGORIES = frozenset(
    {
        "argument",
        "config",
        "fixture",
        "probe",
        "business",
        "store",
        "sense",
        "read",
        "pixel",
        "gate",
        "evidence",
        "internal",
        "bridge",
        "decision",
        "eval",
        "policy",
        "adb_missing",
        "adb_timeout",
        "adb_nonzero",
        "adb_oserror",
    }
)
LOG_COMMANDS = frozenset(
    {
        "adb_devices",
        "adb_window",
        "adb_hierarchy_dump",
        "adb_hierarchy_cat",
        "adb_screencap",
    }
)
LOG_MODES = frozenset({"fixture", "device"})
LOG_CONFIG_SOURCES = frozenset({"default", "file"})
LOG_TS_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
LOG_CORRELATION_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _safe_log_files(root: Path) -> list[Path]:
    """先拒绝日志目录自身/祖先链接再解析，返回隔离根内的日志文件。"""
    logs_dir = root / "data" / "logs"
    _reject_link_ancestors(logs_dir, root, "过程日志目录")
    if not _within(logs_dir.resolve(), root.resolve()):
        raise IsolationError("过程日志目录越出隔离根")
    if not logs_dir.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(logs_dir.glob("m0-process-*.jsonl")):
        if _is_link_like(path):
            raise IsolationError("过程日志不得是符号链接或 junction")
        resolved = path.resolve()
        if not _within(resolved, root.resolve()):
            raise IsolationError("过程日志越出隔离根")
        if resolved.is_file():
            files.append(resolved)
    return files


def _valid_log_event(event: dict[str, Any]) -> str | None:
    """按固定字段白名单与类型校验一条日志事件，拒绝额外键与路径值。"""
    if set(event) != LOG_EVENT_KEYS:
        return "过程日志事件键集合不符"
    if type(event["schema_version"]) is not int or event["schema_version"] != 1:
        return "过程日志 schema_version 非法"
    if not isinstance(event["ts"], str) or LOG_TS_PATTERN.fullmatch(event["ts"]) is None:
        return "过程日志时间戳非法"
    if (
        not isinstance(event["correlation_id"], str)
        or LOG_CORRELATION_PATTERN.fullmatch(event["correlation_id"]) is None
    ):
        return "过程日志 correlation 非法"
    if event["stage"] not in LOG_STAGES:
        return "过程日志阶段非法"
    if event["operation"] is not None and event["operation"] not in LOG_OPERATIONS:
        return "过程日志操作非法"
    if event["result"] not in LOG_RESULTS:
        return "过程日志结果非法"
    if event["reason_code"] is not None and event["reason_code"] not in LOG_REASON_CODES:
        return "过程日志原因码非法"
    if (
        event["error_category"] is not None
        and event["error_category"] not in LOG_ERROR_CATEGORIES
    ):
        return "过程日志错误类别非法"
    if type(event["duration_ms"]) is not int or event["duration_ms"] < 0:
        return "过程日志耗时非法"
    if event["command"] is not None and event["command"] not in LOG_COMMANDS:
        return "过程日志命令非法"
    if event["exit_code"] is not None and (
        type(event["exit_code"]) is not int or event["exit_code"] not in (0, 2)
    ):
        return "过程日志退出码非法"
    if event["mode"] is not None and event["mode"] not in LOG_MODES:
        return "过程日志模式非法"
    if (
        event["config_source"] is not None
        and event["config_source"] not in LOG_CONFIG_SOURCES
    ):
        return "过程日志配置来源非法"
    if event["device_count"] is not None and (
        type(event["device_count"]) is not int or event["device_count"] < 0
    ):
        return "过程日志设备数非法"
    if event["case_index"] is not None and (
        type(event["case_index"]) is not int or event["case_index"] < 1
    ):
        return "过程日志用例序号非法"
    for value in event.values():
        if isinstance(value, str) and ("/" in value or "\\" in value or ".." in value):
            return "过程日志字段值含路径或越界标记"
    return None


def _log_check(root: Path, check: dict[str, Any], files: list[Path] | None) -> str | None:
    """只检查单次 CLI 调用对应的日志：固定事件序列、成对次数与字段边界。"""
    if not isinstance(check, dict):
        return "日志检查不是对象"
    expected_events = check.get("events")
    if not isinstance(expected_events, list) or not expected_events:
        return "日志检查缺少期望事件序列"
    normalised: list[tuple[Any, Any, Any, Any]] = []
    for item in expected_events:
        if not isinstance(item, dict) or set(item) - {"stage", "operation", "result", "reason_code"}:
            return "日志期望事件形状非法"
        if not {"stage", "result"} <= set(item):
            return "日志期望事件缺少 stage/result"
        operation = item.get("operation")
        reason_code = item.get("reason_code")
        if item["stage"] not in LOG_STAGES or item["result"] not in LOG_RESULTS:
            return "日志期望事件阶段/结果非法"
        if operation is not None and operation not in LOG_OPERATIONS:
            return "日志期望事件操作非法"
        if reason_code is not None and reason_code not in LOG_REASON_CODES:
            return "日志期望事件原因码非法"
        normalised.append((item["stage"], operation, item["result"], reason_code))
    if files is None:
        return "过程日志路径安全校验失败"
    if len(files) != 1:
        return f"单次调用必须恰好一个日志文件，实际 {len(files)}"
    try:
        text = files[0].read_text(encoding="utf-8")
    except OSError:
        return "过程日志不可读"
    for needle in check.get("forbidden", []):
        if needle and str(needle) in text:
            return "过程日志泄露了正文/原始标识/路径"
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return "过程日志不是有效 JSON Lines"
        if not isinstance(item, dict):
            return "过程日志事件不是对象"
        problem = _valid_log_event(item)
        if problem:
            return problem
        events.append(item)
    if not events:
        return "过程日志为空"
    if len({item["correlation_id"] for item in events}) != 1:
        return "单次调用日志混入多个 correlation"
    actual = [
        (item["stage"], item["operation"], item["result"], item["reason_code"])
        for item in events
    ]
    if actual != normalised:
        first = next(
            (
                index
                for index, (left, right) in enumerate(zip(actual, normalised))
                if left != right
            ),
            min(len(actual), len(normalised)),
        )
        expected_item = normalised[first] if first < len(normalised) else None
        actual_item = actual[first] if first < len(actual) else None
        return (
            f"日志事件序列不符，第{first + 1}条期望 {expected_item} 实际 {actual_item}"
        )
    active: dict[tuple[str, str], bool] = {}
    for index, item in enumerate(events):
        operation = item["operation"]
        if operation is None:
            continue
        key = (item["stage"], operation)
        if item["result"] == "started":
            if active.get(key):
                return f"第{index + 1}条 started 重复"
            active[key] = True
        else:
            if not active.get(key):
                return f"第{index + 1}条缺少对应 started"
            active[key] = False
    if any(active.values()):
        return "存在未完成的 started"
    for operation in check.get("forbid_operations", []):
        if any(item["operation"] == str(operation) for item in events):
            return f"日志出现禁止操作 {operation}"
    return None


def _db_check(root: Path, check: dict[str, Any]) -> str | None:
    """在隔离的临时合成库上做只读断言；越界在 exists/open 前拒绝。"""
    root_resolved = root.resolve()
    candidate = root / check["path"]
    if candidate.is_symlink():
        return "数据库断言路径不得是符号链接"
    path = candidate.resolve()
    if not _within(path, root_resolved):
        return "数据库断言路径越出隔离根"
    if check.get("kind") == "file_absent":
        return None if not path.exists() else f"{check['path']} 不应存在"
    if not path.is_file():
        return f"{check['path']} 不存在"
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            cursor = connection.cursor()
            if check.get("kind") == "user_version":
                cursor.execute("PRAGMA user_version")
                actual = cursor.fetchone()[0]
                if actual != check.get("equals"):
                    return f"user_version 期望 {check.get('equals')}，实际 {actual}"
            elif check.get("kind") == "table_count":
                table = check["table"]
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                actual = cursor.fetchone()[0]
                if actual != check.get("equals"):
                    return f"{table} 行数期望 {check.get('equals')}，实际 {actual}"
            elif check.get("kind") == "table_exists":
                cursor.execute(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name=?",
                    (check["table"],),
                )
                actual = cursor.fetchone()[0]
                if bool(actual) != bool(check.get("equals")):
                    return f"{check['table']} 存在性不符"
            else:
                return f"未知数据库断言 {check.get('kind')!r}"
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return f"数据库断言失败: {type(exc).__name__}"
    return None


def _substitute(command: str, substitutions: dict[str, str]) -> str:
    """替换命令中的合成路径占位符，不执行 shell。"""
    expanded = command
    for token, value in substitutions.items():
        expanded = expanded.replace(token, value)
    return expanded


def _run_concurrent(
    commands: list[str],
    root: Path,
    run_id: str,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """父 runner 只并发启动两个隔离探针；任何失败都必须显式 FAIL 并清理。"""
    if len(commands) != 2:
        raise IsolationError("并发组只允许恰好两个子进程")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise IsolationError("并发超时必须为 1–120 秒整数")
    for command in commands:
        if not (
            command.startswith("python -m app ")
            or command.startswith(f"python {M0_PROBE_PATH} ")
            or command.startswith(f"python {M1_PROBE_PATH} ")
            or command.startswith(f"python {M2_PROBE_PATH} ")
            or command.startswith(f"python {M3_PROBE_PATH} ")
            or command.startswith(f"python {M4_PROBE_PATH} ")
            or command.startswith(f"python {M4C_PROBE_PATH} ")
        ):
            raise IsolationError("并发子进程命令不在白名单")
    barrier = root / "barrier"
    if barrier.exists():
        shutil.rmtree(barrier)
    barrier.mkdir()
    processes: list[subprocess.Popen] = []
    results: list[dict[str, Any]] = []
    failure: str | None = None
    try:
        for index, command in enumerate(commands):
            tokens = shlex.split(command, posix=True)
            env = _isolation_env(root, f"-c{index}")
            env.update({"M1_BARRIER_DIR": str(barrier), "M1_BARRIER_NAME": str(index)})
            try:
                processes.append(
                    subprocess.Popen(
                        [sys.executable] + tokens[1:],
                        cwd=root,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        shell=False,
                    )
                )
            except OSError as error:
                failure = f"spawn_failed:{type(error).__name__}"
                break
        if failure is None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if all((barrier / f"{index}.ready").is_file() for index in range(2)):
                    break
                if any(process.poll() is not None for process in processes):
                    failure = "barrier_incomplete"
                    break
                time.sleep(0.02)
            else:
                failure = "barrier_timeout"
            if len(processes) != 2:
                failure = failure or "spawn_failed"
            if failure is not None:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
            else:
                (barrier / "go").write_text("go", encoding="utf-8")
        for index, command in enumerate(commands):
            if index >= len(processes):
                results.append(
                    {
                        "command": command,
                        "exit_code": None,
                        "stdout_json": None,
                        "stdout_json_ok": False,
                        "stdout_text": "",
                        "stderr": "",
                        "error": failure or "spawn_failed",
                    }
                )
                continue
            process = processes[index]
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                results.append(
                    {
                        "command": command,
                        "exit_code": None,
                        "stdout_json": None,
                        "stdout_json_ok": False,
                        "stdout_text": stdout,
                        "stderr": stderr,
                        "error": "TimeoutExpired",
                    }
                )
                continue
            payload, ok = _parse_stdout(stdout)
            results.append(
                {
                    "command": command,
                    "exit_code": process.returncode,
                    "stdout_json": payload,
                    "stdout_json_ok": ok,
                    "stdout_text": stdout,
                    "stderr": stderr,
                    "error": failure if failure is not None else None,
                }
            )
        for index in range(len(processes)):
            tripwire = root / f"tripwire-c{index}.log"
            if tripwire.is_file() and tripwire.read_text(encoding="utf-8").strip():
                kinds = sorted(
                    {line.strip() for line in tripwire.read_text(encoding="utf-8").splitlines() if line.strip()}
                )
                if index < len(results):
                    results[index]["error"] = f"意外边界调用: {', '.join(kinds)}"
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.communicate(timeout=2)
            except Exception:
                pass


def _variant_status(scenario: dict[str, Any], variant: dict[str, Any], target: Path) -> str:
    """判定变体在当前目标仓库可达到的执行状态。"""
    if variant["layer"] in ("T", "O", "W"):
        return "DEVICE_GATE_UNVERIFIED"
    if variant["asset_status"] == "DEFERRED":
        return "DEFERRED"
    if variant["asset_status"] == "MODULE_EVIDENCE":
        return "NOT_COVERED_MODULE_ONLY"
    if variant["asset_status"] == "ASSET_ONLY" or variant["entry_state"] != "frozen":
        return "BLOCKED_ENTRY_UNFROZEN"
    if scenario["milestone"] not in IMPLEMENTED_MILESTONES:
        return "BLOCKED_MILESTONE_UNIMPLEMENTED"
    if not (target / "app" / "__main__.py").is_file():
        return "BLOCKED_MISSING_APP"
    commands = list(_iter_command_strings(variant))
    if not commands:
        return "BLOCKED_ENTRY_UNFROZEN"
    for command in commands:
        if not (
            command.startswith("python -m app ")
            or command.startswith(f"python {M0_PROBE_PATH} ")
            or command.startswith(f"python {M1_PROBE_PATH} ")
            or command.startswith(f"python {M2_PROBE_PATH} ")
            or command.startswith(f"python {M3_PROBE_PATH} ")
            or command.startswith(f"python {M4_PROBE_PATH} ")
            or command.startswith(f"python {M4C_PROBE_PATH} ")
        ):
            return "BLOCKED_ENTRY_UNFROZEN"
    return "READY"


def _execute_variant(
    assets: LoadedAssets,
    scenario: dict[str, Any],
    variant: dict[str, Any],
    target: Path,
    run_id: str,
) -> dict[str, Any]:
    """在隔离根按独立预期运行一个已冻结离线变体并返回逐步骤结果。"""
    ref = f"{scenario['id']}/{variant['id']}"
    oracle = assets.oracles["variants"][ref]
    acceptance = oracle.get("acceptance")
    if not isinstance(acceptance, dict):
        return {"ref": ref, "result": "BLOCKED_ENTRY_UNFROZEN", "errors": ["缺少可执行验收预期"]}
    try:
        isolation = _prepare_isolation(target, run_id, scenario["id"], variant["id"], variant)
    except IsolationError as exc:
        return {"ref": ref, "result": "FAIL", "errors": [str(exc)]}
    db_relative = f"data/evals/scenario-runs/{run_id}/{scenario['id']}_{variant['id']}.sqlite3"
    substitutions = {"{db}": db_relative, "{run_dir}": f"data/evals/scenario-runs/{run_id}"}
    step_results: list[dict[str, Any]] = []
    step_logs: list[list[Path] | None] = []
    errors: list[str] = []
    steps = acceptance.get("steps")
    if not isinstance(steps, list) or len(steps) != len(variant.get("steps", [])):
        return {"ref": ref, "result": "FAIL", "errors": ["独立预期步骤数与清单不一致"]}
    for variant_step, step_expectation in zip(variant["steps"], steps):
        if isinstance(variant_step, str):
            try:
                before = set(_safe_log_files(isolation))
            except IsolationError as exc:
                errors.append(f"第{len(step_results) + 1}步日志路径: {exc}")
                before = None
            expanded = _substitute(variant_step, substitutions)
            result = _run_step(expanded, isolation, run_id)
            try:
                after = set(_safe_log_files(isolation))
            except IsolationError as exc:
                after = None
                errors.append(f"第{len(step_results) + 1}步日志路径: {exc}")
            if before is None or after is None:
                step_logs.append(None)
            else:
                step_logs.append(sorted(after - before))
            differences = compare_invocation(result, step_expectation)
            result["differences"] = differences
            step_results.append(result)
            errors.extend(f"第{len(step_results)}步: {item}" for item in differences)
            continue
        if not isinstance(variant_step, dict) or not isinstance(variant_step.get("concurrent"), list):
            errors.append(f"第{len(step_results) + 1}步: 未知步骤形状")
            continue
        commands = [_substitute(command, substitutions) for command in variant_step["concurrent"]]
        timeout = variant_step.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
            errors.append("并发超时必须为 1–120 秒整数，未启动子进程")
            continue
        child_results = _run_concurrent(commands, isolation, run_id, timeout)
        child_expectations = step_expectation.get("children", [])
        if len(child_expectations) != len(child_results):
            errors.append("并发子进程预期数量不一致")
        for index, (child, child_expectation) in enumerate(zip(child_results, child_expectations)):
            differences = compare_invocation(child, child_expectation)
            child["differences"] = differences
            errors.extend(f"并发子{index + 1}: {item}" for item in differences)
        counts: dict[str, int] = {}
        for child in child_results:
            payload = child.get("stdout_json")
            outcome = payload.get("outcome") if isinstance(payload, dict) else None
            if isinstance(outcome, str):
                counts[outcome] = counts.get(outcome, 0) + 1
        expected_counts = step_expectation.get("outcome_counts")
        if expected_counts is not None and counts != expected_counts:
            errors.append(f"并发 outcome 计数不符: {counts} != {expected_counts}")
        step_results.append({"concurrent": child_results, "outcomes": counts})
        step_logs.append(None)
    for check in acceptance.get("db_checks", []):
        resolved = dict(check)
        resolved["path"] = resolved["path"].replace("{db}", db_relative)
        problem = _db_check(isolation, resolved)
        if problem:
            errors.append(f"数据库: {problem}")
    for check in acceptance.get("log_checks", []):
        if not isinstance(check, dict) or type(check.get("step")) is not int:
            errors.append("日志: 检查缺少整数 step")
            continue
        index = check["step"]
        if index < 0 or index >= len(step_logs):
            errors.append(f"日志: step {index} 不在本变体步骤范围")
            continue
        problem = _log_check(isolation, check, step_logs[index])
        if problem:
            errors.append(f"日志: {problem}")
    tripwire = isolation / "tripwire.log"
    if tripwire.is_file() and tripwire.read_text(encoding="utf-8").strip():
        kinds = sorted({line.strip() for line in tripwire.read_text(encoding="utf-8").splitlines() if line.strip()})
        errors.append(f"检测到意外边界调用: {', '.join(kinds)}")
    return {
        "ref": ref,
        "result": "PASS" if not errors else "FAIL",
        "errors": errors,
        "isolation": _relative(isolation, target),
        "steps": step_results,
    }


def run_acceptance(
    assets: LoadedAssets,
    target: Path,
    only: str | None = None,
) -> dict[str, Any]:
    """先校验资产，再汇总离线验收与实机门槛；零项或阻塞不返回 PASS。"""
    run_id = f"accept-{_utc_stamp()}-{uuid.uuid4().hex[:8]}"
    asset_summary = validate_assets(assets)
    if not asset_summary.get("ok"):
        report = {
            "schema_version": 1,
            "mode": "acceptance",
            "run_id": run_id,
            "overall": "BLOCKED_ASSETS",
            "product_acceptance": "NOT_ACCEPTED",
            "asset_check": asset_summary,
            "counts": {"PASS": 0, "FAIL": 0, "BLOCKED_ASSETS": 1},
            "results": [],
            "layer_note": "资产自检失败，拒绝运行任何产品场景。",
        }
        report_path = _write_report(report, target, run_id)
        report["report_path"] = _relative(report_path, target)
        return report

    results: list[dict[str, Any]] = []
    counts = {
        "PASS": 0,
        "FAIL": 0,
        "BLOCKED_MISSING_APP": 0,
        "BLOCKED_ENTRY_UNFROZEN": 0,
        "BLOCKED_MILESTONE_UNIMPLEMENTED": 0,
        "DEFERRED": 0,
        "NOT_COVERED_MODULE_ONLY": 0,
        "DEVICE_GATE_UNVERIFIED": 0,
        "SKIPPED_BY_FILTER": 0,
    }
    for scenario, variant in iter_variants(assets.catalogue):
        ref = f"{scenario['id']}/{variant['id']}"
        if only and not (ref.startswith(only) or scenario["id"] == only):
            counts["SKIPPED_BY_FILTER"] += 1
            results.append({"ref": ref, "result": "SKIPPED_BY_FILTER"})
            continue
        status = _variant_status(scenario, variant, target)
        if status == "READY":
            outcome = _execute_variant(assets, scenario, variant, target, run_id)
        else:
            outcome = {
                "ref": ref,
                "result": status,
                "errors": (
                    []
                    if status in ("DEVICE_GATE_UNVERIFIED", "DEFERRED", "NOT_COVERED_MODULE_ONLY")
                    else [variant.get("required_api") or "入口未冻结"]
                ),
                "deferred_to": variant.get("deferred_to") if status == "DEFERRED" else None,
                "module_evidence": variant.get("module_evidence") if status == "NOT_COVERED_MODULE_ONLY" else None,
            }
        counts[outcome["result"]] = counts.get(outcome["result"], 0) + 1
        results.append(outcome)

    offline_total = sum(
        1
        for scenario, variant in iter_variants(assets.catalogue)
        if variant["layer"] == "F"
        and (not only or f"{scenario['id']}/{variant['id']}".startswith(only) or scenario["id"] == only)
    )
    offline_blocked = (
        counts["BLOCKED_MISSING_APP"]
        + counts["BLOCKED_ENTRY_UNFROZEN"]
        + counts["BLOCKED_MILESTONE_UNIMPLEMENTED"]
        + counts["DEFERRED"]
        + counts["NOT_COVERED_MODULE_ONLY"]
    )
    if counts["FAIL"]:
        overall = "FAIL"
    elif counts["PASS"] == 0 or offline_blocked:
        overall = "BLOCKED"
    else:
        overall = "OFFLINE_PASS"
    if counts["DEVICE_GATE_UNVERIFIED"]:
        product = "NOT_ACCEPTED_DEVICE_GATES_UNVERIFIED"
    elif counts["DEFERRED"] or counts["NOT_COVERED_MODULE_ONLY"]:
        product = "NOT_ACCEPTED_DEFERRED_OR_MODULE_GAPS"
    else:
        product = "NOT_ACCEPTED"
    report = {
        "schema_version": 1,
        "mode": "acceptance",
        "run_id": run_id,
        "target_has_app": (target / "app" / "__main__.py").is_file(),
        "asset_check": asset_summary,
        "overall": overall,
        "product_acceptance": product,
        "layer_note": "只汇总已实现里程碑的 F 结果；未实现里程碑与 T/O/W 保留未验收，离线 PASS 不等于产品通过。",
        "implemented_milestones": sorted(IMPLEMENTED_MILESTONES),
        "counts": counts,
        "offline_variants": offline_total,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    report_path = _write_report(report, target, run_id)
    report["report_path"] = _relative(report_path, target)
    return report


def run_assets(assets: LoadedAssets) -> dict[str, Any]:
    """运行资产自检并返回汇总，不触碰目标应用。"""
    summary = validate_assets(assets)
    summary["mode"] = "assets"
    summary["layer_note"] = "资产自检通过只说明清单/预期/夹具自洽，不代表产品行为通过。"
    return summary


def _exit_code(report: dict[str, Any]) -> int:
    """把报告总体状态映射为固定退出码。"""
    if report["mode"] == "assets":
        return EXIT_PASS if report.get("ok") else EXIT_FAIL
    if report.get("overall") == "OFFLINE_PASS":
        return EXIT_PASS
    if report.get("overall") == "FAIL":
        return EXIT_FAIL
    return EXIT_BLOCKED


def _fatal_report(mode: str, message: str) -> dict[str, Any]:
    """构造不包含原始异常链与本机路径的安全失败报告。"""
    return {
        "schema_version": 1,
        "mode": mode,
        "ok": False,
        "overall": "BLOCKED_ASSETS" if mode == "acceptance" else "ASSET_CHECK_FAILED",
        "errors": [message],
        "layer_note": "资产加载失败，未运行任何产品场景。",
    }


def main(argv: list[str] | None = None) -> int:
    """解析命令行并输出单个 JSON 汇总；任何异常都转为安全失败对象。"""
    parser = argparse.ArgumentParser(description="BOSS 离线场景资产自检与产品验收分流")
    parser.add_argument("--mode", choices=("assets", "acceptance"), required=True)
    parser.add_argument("--target", default=str(SCENARIO_ROOT.parent.parent))
    parser.add_argument("--only", default=None, help="只运行指定场景 ID 或其子集前缀")
    parser.add_argument("--root", default=str(SCENARIO_ROOT))
    args = parser.parse_args(argv)
    try:
        assets = load_assets(Path(args.root))
    except Exception as exc:  # noqa: BLE001 - CLI 边界必须禁止 traceback/绝对路径外泄
        message = str(exc) if isinstance(exc, AssetError) else type(exc).__name__
        report = _fatal_report(args.mode, message)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return EXIT_FAIL if args.mode == "assets" else EXIT_BLOCKED
    try:
        if args.mode == "assets":
            report = run_assets(assets)
        else:
            report = run_acceptance(assets, Path(args.target).resolve(), args.only)
    except Exception as exc:  # noqa: BLE001 - 同上，异常不得带出路径或堆栈
        report = _fatal_report(args.mode, f"内部错误: {type(exc).__name__}")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return EXIT_FAIL if args.mode == "assets" else EXIT_BLOCKED
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return _exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
