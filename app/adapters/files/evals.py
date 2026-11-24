"""实现 M4b 套件读取、本机评测报告与留出登记的文件适配器。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from app.domain.json_input import load_json_object
from app.domain.policy import PolicyIssue
from app.ports.policy import PolicyError

SUITE_PATHS = {
    "public": "evals/policy-suite/public.json",
    "regression": "evals/private/regression/suite.json",
    "holdout": "evals/private/holdout/suite.json",
}
REPORT_ROOT = "data/evals"
REGISTRY_REL = "data/evals/holdout-registry.local.json"
SUITE_MAX_BYTES = 256 * 1024
REGISTRY_MAX_BYTES = 512 * 1024
REPORT_MAX_BYTES = 2 * 1024 * 1024


class FileEvalStore:
    """只在项目内读取合成套件，并把全部产物写入被忽略的 data/evals。"""

    def __init__(self, root: Path) -> None:
        """保存项目根，不读取环境变量、凭据或其它本机私有配置。"""
        self._root = root.resolve()

    def _resolve(self, rel_path: str) -> Path:
        """解析项目内 POSIX 相对路径，拒绝越界、链接组件与非普通文件。"""
        if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path:
            raise PolicyError("policy_io_error", [PolicyIssue(None, None, None, "path")])
        candidate = Path(rel_path)
        if candidate.is_absolute() or candidate.drive:
            raise PolicyError("policy_io_error", [PolicyIssue(None, None, None, "path")])
        current = self._root
        for part in candidate.parts:
            if part in ("", ".", ".."):
                raise PolicyError(
                    "policy_io_error", [PolicyIssue(None, None, None, "path")]
                )
            current = current / part
            if current.exists() and current.is_symlink():
                raise PolicyError(
                    "policy_io_error", [PolicyIssue(None, None, None, "path")]
                )
        resolved = current.resolve()
        if not resolved.is_relative_to(self._root):
            raise PolicyError("policy_io_error", [PolicyIssue(None, None, None, "path")])
        return resolved

    def _read_json(self, rel_path: str, max_bytes: int) -> object | None:
        """读取有界 UTF-8 JSON；不存在返回 None，坏编码/重复键/超限受控失败。"""
        path = self._resolve(rel_path)
        if not path.exists():
            return None
        try:
            if not path.is_file():
                raise OSError("not a file")
            raw = path.read_bytes()
        except OSError:
            raise PolicyError(
                "policy_io_error", [PolicyIssue(rel_path, None, None, "path")]
            ) from None
        if len(raw) > max_bytes:
            raise PolicyError("policy_io_error", [PolicyIssue(rel_path, None, None, "size")])
        if raw.startswith(b"\xef\xbb\xbf"):
            raise PolicyError(
                "policy_io_error", [PolicyIssue(rel_path, None, None, "encoding")]
            )
        try:
            return load_json_object(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise PolicyError(
                "policy_io_error", [PolicyIssue(rel_path, None, None, "format")]
            ) from None

    def read_suite(self, suite_name: str) -> dict[str, object] | None:
        """读取指定套件原文对象；不存在返回 None，不读取任何答案旁路文件。"""
        rel = SUITE_PATHS.get(suite_name)
        if rel is None:
            raise PolicyError("invalid_input", [PolicyIssue(None, None, None, "vocabulary")])
        data = self._read_json(rel, SUITE_MAX_BYTES)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise PolicyError("policy_io_error", [PolicyIssue(rel, None, None, "format")])
        return data

    def _write_json(self, rel_path: str, payload: dict[str, object], max_bytes: int) -> str:
        """把固定 JSON 写入项目内新文件；不覆盖已有文件、不跟随链接。"""
        path = self._resolve(rel_path)
        text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        raw = text.encode("utf-8")
        if len(raw) > max_bytes:
            raise PolicyError("policy_io_error", [PolicyIssue(REPORT_ROOT, None, None, "size")])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            raise PolicyError("policy_exists", [PolicyIssue(rel_path, None, None, "conflict")]) from None
        except OSError:
            raise PolicyError("policy_io_error", [PolicyIssue(rel_path, None, None, "path")]) from None
        return rel_path

    def _run_path(self, run_id: str, name: str) -> str:
        """校验运行 ID 并返回本机运行目录内的固定文件名。"""
        if (
            not isinstance(run_id, str)
            or len(run_id) != 32
            or any(char not in "0123456789abcdef" for char in run_id)
        ):
            raise PolicyError("policy_io_error", [PolicyIssue(REPORT_ROOT, None, None, "path")])
        return f"{REPORT_ROOT}/eval-{run_id}/{name}"

    def write_report(self, run_id: str, report: dict[str, object]) -> str:
        """写出本机评测报告；失败或已存在时不返回成功路径。"""
        return self._write_json(self._run_path(run_id, "report.json"), report, REPORT_MAX_BYTES)

    def write_acceptance(self, run_id: str, acceptance: dict[str, object]) -> str:
        """写出绑定摘要的本机验收凭证；仅由评测成功路径调用。"""
        return self._write_json(
            self._run_path(run_id, "acceptance.json"), acceptance, 64 * 1024
        )

    def read_holdout_registry(self) -> dict[str, object]:
        """读取本机留出登记；不存在时返回空登记，不创建文件。"""
        data = self._read_json(REGISTRY_REL, REGISTRY_MAX_BYTES)
        if data is None:
            return {"schema_version": 1, "batches": []}
        if not isinstance(data, dict):
            raise PolicyError("policy_io_error", [PolicyIssue(REGISTRY_REL, None, None, "format")])
        return data

    def write_holdout_registry(self, registry: dict[str, object]) -> str:
        """同卷临时文件加改名原子替换留出登记，失败不留下半文件。"""
        path = self._resolve(REGISTRY_REL)
        text = json.dumps(
            registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        raw = text.encode("utf-8")
        if len(raw) > REGISTRY_MAX_BYTES:
            raise PolicyError("policy_io_error", [PolicyIssue(REGISTRY_REL, None, None, "size")])
        temp = path.parent / f".{REGISTRY_REL.rsplit('/', 1)[-1]}.{uuid.uuid4().hex}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("xb") as handle:
                handle.write(raw)
            os.replace(temp, path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise PolicyError("policy_io_error", [PolicyIssue(REGISTRY_REL, None, None, "path")]) from None
        return REGISTRY_REL
