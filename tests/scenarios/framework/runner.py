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
EXPECTED_FILE_NAMES = {"expected.json", "oracles.json"}


def _is_expected_name(path: Path) -> bool:
    """识别不得进入 SUT 输入树的答案文件。"""
    return path.name in EXPECTED_FILE_NAMES or path.name.startswith("expected.")


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
    for step in variant.get("steps", []):
        for token in FIXTURE_TOKEN.findall(str(step)):
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
        if resolved.name == "manifest.json":
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
    for source in selected:
        relative = source.relative_to((target / "tests" / "scenarios" / "fixtures").resolve())
        destination = root / "tests" / "scenarios" / "fixtures" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
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


def _isolation_env(root: Path) -> dict[str, str]:
    """构造最小隔离环境：只保留运行必需变量，本地数据与 tripwire 全在根内。"""
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
            "SCENARIO_TRIPWIRE_LOG": str(root / "tripwire.log"),
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


def _variant_status(variant: dict[str, Any], target: Path) -> str:
    """判定变体在当前目标仓库可达到的执行状态。"""
    if variant["layer"] in ("T", "O", "W"):
        return "DEVICE_GATE_UNVERIFIED"
    if variant["asset_status"] == "ASSET_ONLY" or variant["entry_state"] != "frozen":
        return "BLOCKED_ENTRY_UNFROZEN"
    if not (target / "app" / "__main__.py").is_file():
        return "BLOCKED_MISSING_APP"
    for step in variant.get("steps", []):
        if not isinstance(step, str) or not step.startswith("python -m app "):
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
    errors: list[str] = []
    steps = acceptance.get("steps")
    if not isinstance(steps, list) or len(steps) != len(variant.get("steps", [])):
        return {"ref": ref, "result": "FAIL", "errors": ["独立预期步骤数与清单不一致"]}
    for command, step_expectation in zip(variant["steps"], steps):
        expanded = command
        for token, value in substitutions.items():
            expanded = expanded.replace(token, value)
        result = _run_step(expanded, isolation, run_id)
        differences = compare_invocation(result, step_expectation)
        result["differences"] = differences
        step_results.append(result)
        errors.extend(f"第{len(step_results)}步: {item}" for item in differences)
    for check in acceptance.get("db_checks", []):
        resolved = dict(check)
        resolved["path"] = resolved["path"].replace("{db}", db_relative)
        problem = _db_check(isolation, resolved)
        if problem:
            errors.append(f"数据库: {problem}")
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
        "DEVICE_GATE_UNVERIFIED": 0,
        "SKIPPED_BY_FILTER": 0,
    }
    for scenario, variant in iter_variants(assets.catalogue):
        ref = f"{scenario['id']}/{variant['id']}"
        if only and not (ref.startswith(only) or scenario["id"] == only):
            counts["SKIPPED_BY_FILTER"] += 1
            results.append({"ref": ref, "result": "SKIPPED_BY_FILTER"})
            continue
        status = _variant_status(variant, target)
        if status == "READY":
            outcome = _execute_variant(assets, scenario, variant, target, run_id)
        else:
            outcome = {
                "ref": ref,
                "result": status,
                "errors": [] if status == "DEVICE_GATE_UNVERIFIED" else [variant.get("required_api") or "入口未冻结"],
            }
        counts[outcome["result"]] = counts.get(outcome["result"], 0) + 1
        results.append(outcome)

    offline_total = sum(
        1
        for scenario, variant in iter_variants(assets.catalogue)
        if variant["layer"] == "F"
        and (not only or f"{scenario['id']}/{variant['id']}".startswith(only) or scenario["id"] == only)
    )
    offline_blocked = counts["BLOCKED_MISSING_APP"] + counts["BLOCKED_ENTRY_UNFROZEN"]
    if counts["FAIL"]:
        overall = "FAIL"
    elif counts["PASS"] == 0 or offline_blocked:
        overall = "BLOCKED"
    else:
        overall = "OFFLINE_PASS"
    product = (
        "NOT_ACCEPTED_DEVICE_GATES_UNVERIFIED"
        if counts["DEVICE_GATE_UNVERIFIED"]
        else "NOT_ACCEPTED"
    )
    report = {
        "schema_version": 1,
        "mode": "acceptance",
        "run_id": run_id,
        "target_has_app": (target / "app" / "__main__.py").is_file(),
        "asset_check": asset_summary,
        "overall": overall,
        "product_acceptance": product,
        "layer_note": "只汇总 F 离线结果；T/O/W 保留未验收，离线 PASS 不等于产品通过。",
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
