"""提供文件适配器共用的项目内路径、链接/reparse 与有界读取安全边界。"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath


def is_link_like(path: Path) -> bool:
    """判断路径是否为符号链接或 Windows reparse point/junction。"""
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            attributes = path.lstat().st_file_attributes
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                return True
    except OSError:
        return False
    return False


def normalize_project_relative(value: object) -> str:
    """把项目内相对路径规范为 POSIX 形式；非法输入抛 ValueError。"""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("path is invalid")
    normalized = value.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or candidate.anchor
        or normalized.startswith("~")
        or not candidate.parts
        or any(part in ("..", "") for part in candidate.parts)
    ):
        raise ValueError("path is invalid")
    return candidate.as_posix()


def resolve_project_relative(
    root: Path, value: object, *, missing_ok: bool = False
) -> tuple[Path, str]:
    """解析项目内相对路径，拒绝越界、链接/junction 与缺失；返回绝对/相对路径。"""
    posix = normalize_project_relative(value)
    candidate = PurePosixPath(posix)
    resolved = (root / Path(*candidate.parts)).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("path escapes")
    current = root
    for part in candidate.parts:
        current = current / part
        if current.exists() and is_link_like(current):
            raise ValueError("path link")
    if not missing_ok and not resolved.exists():
        raise FileNotFoundError(posix)
    return resolved, posix


def read_bounded_bytes(path: Path, max_bytes: int | None) -> bytes:
    """读取普通文件；指定上限时最多读 max+1 字节，超限抛 ValueError('size')。"""
    if max_bytes is None:
        return path.read_bytes()
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("size")
    return data
