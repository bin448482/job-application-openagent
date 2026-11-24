"""提供 M4a 测试共用的合成项目根、策略包与决策构造辅助。"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.adapters.files.policy import FilePolicyRepository
from app.cli.main import main
from app.domain.models import add_seconds
from app.domain.policy import (
    BASE_FILE_NAMES,
    PACK_FILE_NAMES,
    build_envelope,
    parse_and_validate_pack,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-24T00:00:00.000Z"


def read_exact(path: Path) -> str:
    """按 UTF-8 原始字节读取文件，不做换行转换。"""
    return path.read_bytes().decode("utf-8")


def real_base_files() -> dict[str, str]:
    """读取仓库公开 base 原文。"""
    return {
        name: read_exact(ROOT / "prompts" / "base" / name)
        for name in BASE_FILE_NAMES
    }


def real_example_files() -> dict[str, str]:
    """读取仓库公开示例四载荷原文。"""
    return {
        name: read_exact(ROOT / "prompts" / "examples" / "default" / name)
        for name in PACK_FILE_NAMES
    }


def real_example_pack():
    """解析并返回仓库公开示例包；断言其必须有效。"""
    pack, issues = parse_and_validate_pack(real_example_files())
    if pack is None or issues:
        raise AssertionError([item.to_dict() for item in issues])
    return pack


def real_preview_case(case_id: str) -> dict[str, object]:
    """读取仓库独立合成 preview 案例。"""
    path = ROOT / "tests" / "fixtures" / "policy-preview" / f"{case_id}.json"
    data = json.loads(read_exact(path))
    if not isinstance(data, dict):
        raise AssertionError("preview case is not an object")
    return data


def temp_project():
    """创建带 base、示例与 preview 的临时项目根，调用方负责清理。"""
    tmp = tempfile.TemporaryDirectory(dir=ROOT, prefix=".m4a-policy-")
    root = Path(tmp.name)
    for rel in (
        "prompts/base",
        "prompts/examples",
        "tests/fixtures/policy-preview",
        "evals/policy-suite",
    ):
        shutil.copytree(ROOT / rel, root / rel)
    return tmp, root


def write_pack(root: Path, rel: str, files: dict[str, str]) -> None:
    """把四载荷原字节写入临时项目根；用于构造无效包与编辑流程。"""
    target = root / rel
    target.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (target / name).write_bytes(text.encode("utf-8"))


def read_pack(root: Path, rel: str) -> dict[str, str]:
    """读取临时项目根内四载荷原文。"""
    target = root / rel
    return {name: read_exact(target / name) for name in PACK_FILE_NAMES}


def run_cli(
    root: Path,
    argv: list[str],
    process_log: object | None = None,
    os_root: Path | None = None,
) -> tuple[int, dict[str, object] | None, str]:
    """在临时项目根运行 CLI，可注入测试 OS 根；返回退出码、JSON 与 stderr。"""
    out = io.StringIO()
    err = io.StringIO()
    kwargs = {"process_log": process_log} if process_log is not None else {}
    if os_root is not None:
        kwargs["os_root"] = os_root
    with patch("app.cli.main.PROJECT_ROOT", root), redirect_stdout(out), redirect_stderr(err):
        code = main(argv, **kwargs)
    text = out.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, err.getvalue()


def valid_envelope(**overrides: object) -> dict[str, object]:
    """按公开 base/示例包与 policy-greeting 合成输入构造有效 envelope。"""
    case = real_preview_case("policy-greeting")
    params: dict[str, object] = {
        "task_id": case["task_id"],
        "nonce": case["nonce"],
        "kind": case["kind"],
        "subject": case["subject"],
        "inbound": case["inbound"],
        "context": case["context"],
        "allowed_facts": case["allowed_facts"],
        "base_files": real_base_files(),
        "pack": real_example_pack(),
        "now": case["now"],
    }
    params.update(overrides)
    return build_envelope(**params)  # type: ignore[arg-type]


def valid_decision(envelope: dict[str, object], **overrides: object) -> dict[str, object]:
    """构造与 envelope 逐字回显的有效 Decision。"""
    decision: dict[str, object] = {
        "schema_version": 1,
        "task_id": envelope["task_id"],
        "nonce": envelope["nonce"],
        "input_hash": envelope["input_hash"],
        "intent": "interested",
        "sub_intents": ["greeting"],
        "reply": "您好，感谢联系。",
        "evidence": str(envelope["inbound"])[:2],
        "escalate": None,
    }
    decision.update(overrides)
    return decision


def lease_expiry(now: str = NOW) -> str:
    """返回比 now 晚 60 秒的 M1 开发基线租约截止时间。"""
    return add_seconds(now, 60)


def repository(root: Path) -> FilePolicyRepository:
    """返回临时项目根的策略文件适配器。"""
    return FilePolicyRepository(root)
