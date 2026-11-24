"""实现 M4b 套件读取、本机报告/凭证、留出登记事务与无上传合同。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from app.adapters.files.support import (
    is_link_like,
    read_bounded_bytes,
    resolve_project_relative,
)
from app.domain.eval import empty_holdout_registry
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
REGISTRY_LOCK_REL = "data/evals/.holdout-registry.lock"
ATTESTATION_REL = "data/evals/holdout-attestation.local.json"
REGRESSION_ADMISSION_ROOT = "data/evals/regression-admissions"
SUITE_MAX_BYTES = 256 * 1024
REGISTRY_MAX_BYTES = 512 * 1024
ATTESTATION_MAX_BYTES = 64 * 1024
REPORT_MAX_BYTES = 2 * 1024 * 1024
LOCK_WAIT_SECONDS = 5.0


class FileEvalStore:
    """只在项目内读取合成套件，并把全部产物写入被忽略的 data/evals。"""

    def __init__(self, root: Path) -> None:
        """保存项目根，不读取环境变量、凭据或其它本机私有配置。"""
        self._root = root.resolve()
        self._thread_lock = threading.Lock()

    def _resolve(self, rel_path: str) -> tuple[Path, str]:
        """解析项目内相对路径，拒绝越界、链接/junction 与非法形状。"""
        try:
            return resolve_project_relative(self._root, rel_path, missing_ok=True)
        except ValueError:
            raise PolicyError("policy_io_error", [PolicyIssue(rel_path, None, None, "path")]) from None

    def _read_json(self, rel_path: str, max_bytes: int) -> object | None:
        """有界读取 UTF-8 JSON；不存在返回 None，坏编码/重复键/超限受控失败。"""
        path, posix = self._resolve(rel_path)
        if not path.exists():
            return None
        if not path.is_file() or is_link_like(path):
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")])
        try:
            raw = read_bounded_bytes(path, max_bytes)
        except ValueError:
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "size")]) from None
        except OSError:
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")]) from None
        if raw.startswith(b"\xef\xbb\xbf"):
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "encoding")])
        try:
            return load_json_object(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "format")]) from None

    def _write_new_json(self, rel_path: str, payload: dict[str, object], max_bytes: int) -> str:
        """同卷临时完整写入后原子链接到目标；目标存在不覆盖，失败不留半文件。"""
        path, posix = self._resolve(rel_path)
        if path.exists():
            raise PolicyError("policy_exists", [PolicyIssue(posix, None, None, "conflict")])
        text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        raw = text.encode("utf-8")
        if len(raw) > max_bytes:
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "size")])
        temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temp, path)
        except FileExistsError:
            raise PolicyError("policy_exists", [PolicyIssue(posix, None, None, "conflict")]) from None
        except OSError:
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")]) from None
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return posix

    def read_suite(self, suite_name: str) -> dict[str, object] | None:
        """读取指定套件；regression 合并既有文件与已退役留出准入副本。"""
        rel = SUITE_PATHS.get(suite_name)
        if rel is None:
            raise PolicyError("invalid_input", [PolicyIssue(None, None, None, "vocabulary")])
        base = self._read_json(rel, SUITE_MAX_BYTES)
        if base is not None and not isinstance(base, dict):
            raise PolicyError("policy_io_error", [PolicyIssue(rel, None, None, "format")])
        if suite_name != "regression":
            return base
        registry = self._read_registry_unlocked()
        retired = {
            str(batch["batch_id"]): batch.get("regression_ref")
            for batch in registry.get("batches", [])
            if isinstance(batch, dict)
            and batch.get("status") == "retired"
            and batch.get("regression_eligible") is True
        }
        admissions = self._read_regression_admissions()
        if not admissions:
            return base
        cases: list[object] = []
        if base is not None:
            base_cases = base.get("cases")
            if not isinstance(base_cases, list):
                raise PolicyError("policy_io_error", [PolicyIssue(rel, None, None, "format")])
            cases.extend(base_cases)
        for name, item in admissions:
            if retired.get(name) != f"{REGRESSION_ADMISSION_ROOT}/{name}/suite.json":
                continue
            cases.extend(item)
        if not cases and base is None:
            return None
        return {
            "schema_version": 1,
            "suite": "regression",
            "version": "regression-admitted-v1",
            "cases": cases,
        }

    def _read_regression_admissions(self) -> list[tuple[str, list[object]]]:
        """读取本机 regression 准入副本；链接/坏文件 fail closed，不按 registry 过滤。"""
        root, posix = self._resolve(REGRESSION_ADMISSION_ROOT)
        if not root.exists():
            return []
        if not root.is_dir() or is_link_like(root):
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")])
        try:
            entries = sorted(os.scandir(root), key=lambda item: item.name)
        except OSError:
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")]) from None
        suites: list[tuple[str, list[object]]] = []
        for entry in entries:
            if is_link_like(Path(entry.path)) or not entry.is_dir(follow_symlinks=False):
                raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")])
            data = self._read_json(
                f"{REGRESSION_ADMISSION_ROOT}/{entry.name}/suite.json", SUITE_MAX_BYTES
            )
            if not isinstance(data, dict):
                raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "format")])
            cases = data.get("cases")
            if not isinstance(cases, list):
                raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "format")])
            suites.append((entry.name, cases))
        return suites

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
        return self._write_new_json(self._run_path(run_id, "report.json"), report, REPORT_MAX_BYTES)

    def write_acceptance(self, run_id: str, acceptance: dict[str, object]) -> str:
        """写出绑定摘要的本机验收凭证；仅由评测成功路径调用。"""
        return self._write_new_json(
            self._run_path(run_id, "acceptance.json"), acceptance, 64 * 1024
        )

    def read_holdout_registry(self) -> dict[str, object]:
        """读取本机留出登记；不存在时返回空登记，不创建文件。"""
        return self._read_registry_unlocked()

    def _read_registry_unlocked(self) -> dict[str, object]:
        """读取登记对象；缺失返回空登记，坏格式固定失败。"""
        data = self._read_json(REGISTRY_REL, REGISTRY_MAX_BYTES)
        if data is None:
            return empty_holdout_registry()
        if not isinstance(data, dict):
            raise PolicyError("policy_io_error", [PolicyIssue(REGISTRY_REL, None, None, "format")])
        return data

    @contextmanager
    def _registry_lock(self) -> Iterator[None]:
        """持项目内独占文件锁覆盖读-改-写最小原子区间，并串行同进程线程。"""
        with self._thread_lock:
            path, _posix = self._resolve(REGISTRY_LOCK_REL)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError:
                raise PolicyError("policy_io_error", [PolicyIssue(REGISTRY_REL, None, None, "path")]) from None
            try:
                if os.name == "nt":
                    import msvcrt

                    deadline = time.monotonic() + LOCK_WAIT_SECONDS
                    while True:
                        os.lseek(handle, 0, os.SEEK_SET)
                        try:
                            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise PolicyError(
                                    "policy_io_error",
                                    [PolicyIssue(REGISTRY_REL, None, None, "conflict")],
                                ) from None
                            time.sleep(0.05)
                    try:
                        yield
                    finally:
                        os.lseek(handle, 0, os.SEEK_SET)
                        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)

    def update_holdout_registry(
        self, mutator: Callable[[dict[str, object]], object]
    ) -> object:
        """在独占锁内读-改-原子写登记；mutator 或写失败不覆盖原登记。"""
        with self._registry_lock():
            registry = self._read_registry_unlocked()
            result = mutator(registry)
            self._write_registry_unlocked(registry)
            return result

    def _write_registry_unlocked(self, registry: dict[str, object]) -> str:
        """同卷临时文件原子替换登记；失败不留下可误认的部分登记。"""
        path, posix = self._resolve(REGISTRY_REL)
        text = json.dumps(
            registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        raw = text.encode("utf-8")
        if len(raw) > REGISTRY_MAX_BYTES:
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "size")])
        temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError:
            raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")]) from None
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return posix

    def write_holdout_registry(self, registry: dict[str, object]) -> str:
        """在独占锁内原子写入完整登记，供显式初始化或测试。"""
        with self._registry_lock():
            return self._write_registry_unlocked(registry)

    def read_holdout_attestation(self) -> dict[str, object] | None:
        """读取本机无上传合同；不存在返回 None，不读取环境或凭据。"""
        data = self._read_json(ATTESTATION_REL, ATTESTATION_MAX_BYTES)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise PolicyError("policy_io_error", [PolicyIssue(ATTESTATION_REL, None, None, "format")])
        return data

    def write_regression_admission(
        self, batch_id: str, suite: dict[str, object]
    ) -> str:
        """写退役留出整批回归准入副本；同内容重试幂等，不同内容不覆盖。"""
        rel = self._admission_rel(batch_id)
        payload = dict(suite)
        payload["suite"] = "regression"
        path, posix = self._resolve(rel)
        new_bytes = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if path.exists():
            if not path.is_file() or is_link_like(path):
                raise PolicyError("policy_io_error", [PolicyIssue(posix, None, None, "path")])
            try:
                existing = read_bounded_bytes(path, SUITE_MAX_BYTES)
            except ValueError:
                raise PolicyError(
                    "policy_io_error", [PolicyIssue(posix, None, None, "size")]
                ) from None
            except OSError:
                raise PolicyError(
                    "policy_io_error", [PolicyIssue(posix, None, None, "path")]
                ) from None
            if existing == new_bytes:
                return posix
            raise PolicyError("policy_exists", [PolicyIssue(posix, None, None, "conflict")])
        return self._write_new_json(rel, payload, SUITE_MAX_BYTES)

    def _admission_rel(self, batch_id: str) -> str:
        """校验批次 ID 字面并返回该批次唯一准入副本相对路径。"""
        if (
            not isinstance(batch_id, str)
            or not batch_id
            or len(batch_id) > 64
            or any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for char in batch_id
            )
        ):
            raise PolicyError(
                "policy_io_error", [PolicyIssue(REGRESSION_ADMISSION_ROOT, None, None, "path")]
            )
        return f"{REGRESSION_ADMISSION_ROOT}/{batch_id}/suite.json"

    def regression_admission_exists(self, batch_id: str) -> bool:
        """判断该批次准入副本是否为普通文件；链接/junction 由解析拒绝。"""
        path, _posix = self._resolve(self._admission_rel(batch_id))
        return path.is_file() and not is_link_like(path)

    def remove_regression_admission(self, batch_id: str) -> None:
        """仅删除该批次自己的准入文件与空目录；不递归、不 glob 清理。"""
        rel = self._admission_rel(batch_id)
        path, posix = self._resolve(rel)
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except FileNotFoundError:
            return
        except OSError:
            raise PolicyError(
                "policy_io_error", [PolicyIssue(posix, None, None, "path")]
            ) from None
