"""提供 ADB 子进程的共享有界执行边界：参数数组、门禁复核、超时与不重试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

ADB_TIMEOUT_SECONDS = 5


class AdbCommandFailure(Exception):
    """携带固定本地错误类别，不包含命令参数、serial 或原始异常。"""

    def __init__(self, category: str) -> None:
        """保存 `adb_missing/adb_timeout/adb_oserror/adb_nonzero` 之一。"""
        if category not in {
            "adb_missing",
            "adb_timeout",
            "adb_oserror",
            "adb_nonzero",
        }:
            raise ValueError("adb category is invalid")
        super().__init__(category)
        self.category = category


def run_adb(
    args: list[str],
    *,
    gate: object,
    timeout: float = ADB_TIMEOUT_SECONDS,
    before: Callable[[], None] | None = None,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    """在共享门禁复核后执行一次有界命令；失败不重试，由调用方映射业务原因。"""
    if before is not None:
        before()
    gate.verify()  # type: ignore[attr-defined]
    try:
        completed = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise AdbCommandFailure("adb_missing") from None
    except subprocess.TimeoutExpired:
        raise AdbCommandFailure("adb_timeout") from None
    except OSError:
        raise AdbCommandFailure("adb_oserror") from None
    if completed.returncode != 0:
        raise AdbCommandFailure("adb_nonzero")
    return completed


def save_evidence(directory: Path | None, name: str, payload: bytes) -> None:
    """把证据写到调用方指定本机目录；未指定目录时跳过，不做隐式落盘。"""
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(payload)
