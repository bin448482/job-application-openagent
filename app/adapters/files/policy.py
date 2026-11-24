"""实现 M4a 项目内策略包读取、暂存、同卷发布与受控清理的文件适配器。"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from app.domain.json_input import load_json_object
from app.domain.policy import (
    BASE_FILE_NAMES,
    GUIDE_FILE_NAMES,
    PACK_FILE_NAMES,
    PACK_MD_MAX_BYTES,
    PACK_TOML_MAX_BYTES,
    PACK_TOTAL_MAX_BYTES,
    PolicyIssue,
    is_case_id,
    is_snapshot_id,
    normalize_active_pack_path,
    normalize_snapshot_path,
)
from app.adapters.files.support import (
    is_link_like,
    normalize_project_relative,
    read_bounded_bytes,
    resolve_project_relative,
)
from app.ports.policy import PolicyError

BASE_MAX_BYTES = 64 * 1024
STAGING_PREFIX = ".staging-"
PREVIEW_ROOT = "tests/fixtures/policy-preview"


def _issue(file: str | None, field: str | None, code: str) -> PolicyIssue:
    """构造适配器层固定诊断，不携带原值或绝对路径。"""
    return PolicyIssue(file=file, field=field, line=None, code=code)


def _is_link_like(path: Path) -> bool:
    """判断路径是否为符号链接或 Windows reparse point/junction。"""
    return is_link_like(path)


class FilePolicyRepository:
    """仅在项目根内读写 prompts/base、prompts/local、examples 与 preview 案例。"""

    def __init__(self, root: Path) -> None:
        """保存项目根并准备本次操作自有路径集合，构造阶段不访问文件。"""
        self.root = Path(root).resolve()
        self._owned: set[str] = set()

    def _normalize_rel(self, value: str) -> str:
        """只做纯字符串项目内相对路径检查，不访问文件系统。"""
        try:
            return normalize_project_relative(value)
        except ValueError:
            raise PolicyError("invalid_input", [_issue(None, None, "path")]) from None

    def _resolve(self, value: str, *, missing_ok: bool = False) -> tuple[Path, str]:
        """把项目内相对路径规范为绝对路径与 POSIX 相对路径，拒绝越界与链接。"""
        posix = self._normalize_rel(value)
        try:
            return resolve_project_relative(self.root, posix, missing_ok=missing_ok)
        except ValueError:
            raise PolicyError("invalid_input", [_issue(posix, None, "path")]) from None
        except FileNotFoundError:
            raise PolicyError("policy_io_error", [_issue(posix, None, "missing")]) from None

    def _active_pack(self, value: str) -> str:
        """校验值恰为 local 直接活动包目录并返回规范相对路径。"""
        try:
            posix = normalize_active_pack_path(value)
        except ValueError:
            raise PolicyError("invalid_input", [_issue(None, None, "path")]) from None
        self._resolve(posix)
        return posix

    def _history_snapshot(self, value: str) -> str:
        """校验值恰为 history 直接快照目录并返回规范相对路径。"""
        try:
            posix = normalize_snapshot_path(value)
        except ValueError:
            raise PolicyError("invalid_input", [_issue(None, None, "path")]) from None
        self._resolve(posix)
        return posix

    def _read_bytes(self, rel_path: str, *, max_bytes: int | None = None) -> bytes:
        """读取普通文件原始字节；拒绝链接、BOM、NUL 与大小越界。"""
        path, posix = self._resolve(rel_path)
        if _is_link_like(path) or not path.is_file():
            raise PolicyError("invalid_policy", [_issue(posix, None, "path")])
        try:
            data = read_bounded_bytes(path, max_bytes)
        except ValueError:
            raise PolicyError("invalid_policy", [_issue(posix, None, "size")]) from None
        except OSError:
            raise PolicyError("policy_io_error", [_issue(posix, None, "format")]) from None
        if data.startswith(b"\xef\xbb\xbf"):
            raise PolicyError("invalid_policy", [_issue(posix, None, "encoding")])
        if b"\x00" in data:
            raise PolicyError("invalid_policy", [_issue(posix, None, "encoding")])
        return data

    def _read_text(self, rel_path: str, *, max_bytes: int | None = None) -> str:
        """按 UTF-8 严格解码且不做换行转换，保留原始 LF/CRLF 字节语义。"""
        data = self._read_bytes(rel_path, max_bytes=max_bytes)
        try:
            return data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            path, posix = self._resolve(rel_path)
            del path
            raise PolicyError("invalid_policy", [_issue(posix, None, "encoding")]) from None

    def _entries(self, rel_path: str) -> list[os.DirEntry[str]]:
        """列出目录直接项，拒绝链接与任何越界间接层。"""
        path, posix = self._resolve(rel_path)
        if _is_link_like(path) or not path.is_dir():
            raise PolicyError("invalid_policy", [_issue(posix, None, "path")])
        try:
            entries = list(os.scandir(path))
        except OSError:
            raise PolicyError("policy_io_error", [_issue(posix, None, "format")]) from None
        for entry in entries:
            if _is_link_like(Path(entry.path)):
                raise PolicyError("invalid_input", [_issue(posix, None, "path")])
        return entries

    def _read_payload_dir(
        self, rel_path: str, *, guides_required: bool
    ) -> dict[str, str]:
        """读取四载荷文件与可选双指南，拒绝子目录、链接和额外文件。"""
        entries = self._entries(rel_path)
        names = {entry.name for entry in entries}
        for required in PACK_FILE_NAMES:
            if required not in names:
                raise PolicyError("invalid_policy", [_issue(rel_path, None, "missing")])
        allowed = set(PACK_FILE_NAMES) | set(GUIDE_FILE_NAMES)
        if names - allowed:
            raise PolicyError("invalid_policy", [_issue(rel_path, None, "unknown_key")])
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                raise PolicyError("invalid_policy", [_issue(f"{rel_path}/{entry.name}", None, "path")])
        guides_present = set(GUIDE_FILE_NAMES) & names
        if guides_required and guides_present != set(GUIDE_FILE_NAMES):
            raise PolicyError("invalid_policy", [_issue(rel_path, None, "missing")])
        if guides_present and guides_present != set(GUIDE_FILE_NAMES):
            raise PolicyError("invalid_policy", [_issue(rel_path, None, "missing")])
        files: dict[str, str] = {}
        total = 0
        for name in PACK_FILE_NAMES:
            max_bytes = PACK_TOML_MAX_BYTES if name == "pack.toml" else PACK_MD_MAX_BYTES
            text = self._read_text(f"{rel_path}/{name}", max_bytes=max_bytes)
            total += len(text.encode("utf-8"))
            files[name] = text
        if total > PACK_TOTAL_MAX_BYTES:
            raise PolicyError("invalid_policy", [_issue(rel_path, None, "size")])
        return files

    def read_base(self) -> dict[str, str]:
        """读取两份公开 base 原文，按文件名返回并保留原始换行。"""
        base: dict[str, str] = {}
        for name in BASE_FILE_NAMES:
            base[name] = self._read_text(f"prompts/base/{name}", max_bytes=BASE_MAX_BYTES)
        return base

    def read_example_pack(self) -> dict[str, str]:
        """读取公开示例四载荷文件，不读取本机活动包或事实。"""
        return self._read_payload_dir("prompts/examples/default", guides_required=False)

    def read_pack(self, pack_rel: str) -> dict[str, str]:
        """读取 local 直接活动包四载荷文件，拒绝 history 与暂存目录。"""
        posix = self._active_pack(pack_rel)
        return self._read_payload_dir(posix, guides_required=False)

    def read_snapshot_pack(self, snapshot_rel: str) -> dict[str, str]:
        """读取 history 直接快照的四载荷文件，拒绝活动目录与暂存目录。"""
        posix = self._history_snapshot(snapshot_rel)
        return self._read_payload_dir(posix, guides_required=False)

    def _read_guides_dir(self, posix: str) -> dict[str, str]:
        """从给定包/快照目录读取可选双指南的共用正文核心。"""
        entries = self._entries(posix)
        names = {entry.name for entry in entries}
        present = set(GUIDE_FILE_NAMES) & names
        if not present:
            return {}
        if present != set(GUIDE_FILE_NAMES):
            raise PolicyError("invalid_policy", [_issue(posix, None, "missing")])
        guides: dict[str, str] = {}
        for name in GUIDE_FILE_NAMES:
            guides[name] = self._read_text(
                f"{posix}/{name}", max_bytes=PACK_MD_MAX_BYTES
            )
        return guides

    def read_snapshot_guides(self, snapshot_rel: str) -> dict[str, str]:
        """读取快照内可选双指南；两者都不存在时返回空映射。"""
        return self._read_guides_dir(self._history_snapshot(snapshot_rel))

    def read_guides(self, pack_rel: str) -> dict[str, str]:
        """读取包内可选双指南；两者都不存在时返回空映射。"""
        return self._read_guides_dir(self._active_pack(pack_rel))

    def read_preview_case(self, case_id: str) -> dict[str, object]:
        """读取固定 preview 目录内的合成案例，未知 ID 返回 unknown_case。"""
        if not is_case_id(case_id):
            raise PolicyError("unknown_case", [_issue(None, None, "path")])
        rel = f"{PREVIEW_ROOT}/{case_id}.json"
        try:
            text = self._read_text(rel, max_bytes=PACK_MD_MAX_BYTES)
        except PolicyError as error:
            if error.reason_code == "policy_io_error":
                raise PolicyError("unknown_case", [_issue(None, None, "path")]) from None
            raise
        try:
            data = load_json_object(text)
        except (ValueError, UnicodeError):
            raise PolicyError("unknown_case", [_issue(rel, None, "format")]) from None
        if not isinstance(data, dict):
            raise PolicyError("unknown_case", [_issue(rel, None, "format")])
        return data

    def list_pack_dirs(self) -> list[str]:
        """列出 local 下非 history、非暂存的活动包目录。"""
        base = self.root / "prompts" / "local"
        if not base.exists():
            return []
        if _is_link_like(base) or not base.is_dir():
            raise PolicyError("invalid_input", [_issue("prompts/local", None, "path")])
        try:
            entries = list(os.scandir(base))
        except OSError:
            raise PolicyError("policy_io_error", [_issue("prompts/local", None, "format")]) from None
        result: list[str] = []
        for entry in entries:
            if entry.name.startswith(".") or entry.name == "history":
                continue
            path = Path(entry.path)
            if _is_link_like(path) or path.is_dir():
                result.append(f"prompts/local/{entry.name}")
        return sorted(result)

    def list_history_dirs(self) -> list[str]:
        """列出 history 下符合快照 ID 词法的直接目录。"""
        base = self.root / "prompts" / "local" / "history"
        if not base.exists():
            return []
        if _is_link_like(base) or not base.is_dir():
            raise PolicyError("invalid_input", [_issue("prompts/local/history", None, "path")])
        try:
            entries = list(os.scandir(base))
        except OSError:
            raise PolicyError("policy_io_error", [_issue("prompts/local/history", None, "format")]) from None
        result: list[str] = []
        for entry in entries:
            if not is_snapshot_id(entry.name):
                continue
            path = Path(entry.path)
            if _is_link_like(path) or path.is_dir():
                result.append(f"prompts/local/history/{entry.name}")
        return sorted(result)

    def list_files(self, rel_path: str) -> list[str]:
        """列出目录直接项名称；不存在时返回空列表供完整性检查。"""
        path, posix = self._resolve(rel_path, missing_ok=True)
        if not path.exists():
            return []
        if _is_link_like(path) or not path.is_dir():
            raise PolicyError("invalid_policy", [_issue(posix, None, "path")])
        try:
            return sorted(item.name for item in os.scandir(path))
        except OSError:
            raise PolicyError("policy_io_error", [_issue(posix, None, "format")]) from None

    def read_manifest(self, manifest_rel: str) -> dict[str, object]:
        """读取 history 直接清单文件并拒绝重复 JSON 键。"""
        path, posix = self._resolve(manifest_rel)
        parts = posix.split("/")
        if (
            len(parts) != 4
            or parts[:3] != ["prompts", "local", "history"]
            or not parts[3].endswith(".local.json")
        ):
            raise PolicyError("invalid_input", [_issue(posix, None, "path")])
        snapshot_id = parts[3][: -len(".local.json")]
        if not is_snapshot_id(snapshot_id) or parts[3] != f"{snapshot_id}.local.json":
            raise PolicyError("invalid_input", [_issue(posix, None, "path")])
        text = self._read_text(posix, max_bytes=PACK_MD_MAX_BYTES)
        try:
            data = load_json_object(text)
        except (ValueError, UnicodeError):
            raise PolicyError("policy_changed", [_issue(posix, None, "format")]) from None
        if not isinstance(data, dict):
            raise PolicyError("policy_changed", [_issue(posix, None, "format")])
        return data

    def path_exists(self, rel_path: str) -> bool:
        """判断项目内相对路径是否存在，不访问链接目标之外的路径。"""
        path, _ = self._resolve(rel_path, missing_ok=True)
        return path.exists()

    def make_staging_dir(self, parent_rel: str) -> str:
        """在 local 或 history 下创建点前缀暂存目录，活动包发现器不会接纳。"""
        _, parent = self._resolve(parent_rel, missing_ok=True)
        if parent not in {"prompts/local", "prompts/local/history"}:
            raise PolicyError("invalid_input", [_issue(parent, None, "path")])
        parent_path, _ = self._resolve(parent_rel, missing_ok=True)
        try:
            parent_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise PolicyError("policy_io_error", [_issue(parent, None, "format")]) from None
        name = f"{STAGING_PREFIX}{uuid.uuid4().hex}"
        rel = f"{parent}/{name}"
        path, _ = self._resolve(rel, missing_ok=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            raise PolicyError("policy_io_error", [_issue(parent, None, "format")]) from None
        except OSError:
            raise PolicyError("policy_io_error", [_issue(parent, None, "format")]) from None
        self._owned.add(rel)
        return rel

    def write_text_exact(self, rel_path: str, text: str) -> None:
        """只向本次暂存路径按 UTF-8 原字节写入，不覆盖已有文件。"""
        if not isinstance(text, str):
            raise PolicyError("invalid_input", [_issue(None, None, "type")])
        path, posix = self._resolve(rel_path, missing_ok=True)
        if not any(part.startswith(STAGING_PREFIX) for part in posix.split("/")):
            raise PolicyError("invalid_input", [_issue(posix, None, "path")])
        try:
            with path.open("xb") as handle:
                handle.write(text.encode("utf-8"))
        except FileExistsError:
            raise PolicyError("policy_io_error", [_issue(posix, None, "format")]) from None
        except OSError:
            raise PolicyError("policy_io_error", [_issue(posix, None, "format")]) from None
        self._owned.add(posix)

    def read_text_exact(self, rel_path: str) -> str:
        """按 UTF-8 原字节读取文件，用于暂存副本复核。"""
        return self._read_text(rel_path, max_bytes=BASE_MAX_BYTES)

    def promote(self, staging_rel: str, dest_rel: str) -> None:
        """把暂存路径同卷改名为目标；目标存在时拒绝且不覆盖。"""
        staging_path, staging = self._resolve(staging_rel)
        dest_path, dest = self._resolve(dest_rel, missing_ok=True)
        if not any(part.startswith(STAGING_PREFIX) for part in staging.split("/")):
            raise PolicyError("invalid_input", [_issue(staging, None, "path")])
        if dest_path.exists():
            raise PolicyError("policy_exists", [_issue(dest, None, "conflict")])
        try:
            staging_path.rename(dest_path)
        except FileExistsError:
            raise PolicyError("policy_exists", [_issue(dest, None, "conflict")]) from None
        except OSError:
            raise PolicyError("policy_io_error", [_issue(dest, None, "format")]) from None
        self._owned.add(dest)

    def remove_owned(self, rel_path: str) -> None:
        """仅清理本次操作登记过的暂存或已发布路径，避免误删用户数据。"""
        _, posix = self._resolve(rel_path, missing_ok=True)
        if posix not in self._owned:
            raise PolicyError("invalid_input", [_issue(posix, None, "path")])
        path, _ = self._resolve(rel_path, missing_ok=True)
        try:
            if path.is_dir() and not _is_link_like(path):
                shutil.rmtree(path)
            elif path.exists() and not _is_link_like(path):
                path.unlink()
        except OSError:
            raise PolicyError("policy_io_error", [_issue(posix, None, "format")]) from None
        finally:
            self._owned.discard(posix)

    def write_manifest(self, staging_rel: str, manifest: dict[str, object]) -> None:
        """把清单按固定紧凑 JSON 写入暂存文件，再由调用方同卷发布。"""
        text = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.write_text_exact(staging_rel, text)


class PolicyFileSnapshot:
    """为 M4b/M5 决策边界提供每次重读 base 与活动四载荷的真实文件适配。"""

    def __init__(self, repository: FilePolicyRepository, pack_rel: str) -> None:
        """保存项目内策略仓储与显式活动包路径，构造阶段不读取文件。"""
        self._repository = repository
        self._pack_rel = pack_rel

    def reload(self) -> tuple[dict[str, str], dict[str, str]]:
        """重读当前 base 与活动包四载荷；读取失败由仓储抛固定策略错误。"""
        return (
            self._repository.read_base(),
            self._repository.read_pack(self._pack_rel),
        )
