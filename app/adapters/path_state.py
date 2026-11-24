"""提供区分明确缺失、普通文件与不可判定路径的最小本机 lstat 边界。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

PATH_MISSING = "missing"
PATH_FILE = "file"
PATH_OTHER = "other"
PATH_UNKNOWN = "unknown"


def probe_path(path: Path) -> str:
    """只把 FileNotFoundError 视为明确缺失；其它 OSError 一律不可判定。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return PATH_MISSING
    except OSError:
        return PATH_UNKNOWN
    if stat.S_ISREG(info.st_mode):
        return PATH_FILE
    return PATH_OTHER
