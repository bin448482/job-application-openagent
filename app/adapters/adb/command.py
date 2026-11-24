"""提供 ADB 子进程的共享有界执行边界：参数数组、门禁复核、超时与不重试。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable

ADB_TIMEOUT_SECONDS = 5


class CommandIdentityStore:
    """按动作键保存本机 ADB 命令进程身份，供恢复时核对原命令族停止。"""

    def __init__(self, directory: Path) -> None:
        """保存本机忽略目录；构造不创建文件。"""
        self._directory = Path(directory)

    def record(self, action_key: str, pid: int) -> None:
        """追加一个命令 PID，供同一动作恢复时核对。"""
        if type(pid) is not int or pid <= 0:
            raise ValueError("pid is invalid")
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._path(action_key).open(
            "a", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(json.dumps({"pid": pid}, separators=(",", ":")) + "\n")

    def identities(self, action_key: str) -> list[int]:
        """只读返回动作已记录的 PID 去重列表；缺失返回空。"""
        path = self._path(action_key)
        if not path.is_file():
            return []
        try:
            pids = [
                int(json.loads(line)["pid"])
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return []
        return sorted(set(pid for pid in pids if pid > 0))

    def check(
        self,
        action_key: str,
        owner_pid: int,
        checker: Callable[[int], str],
    ) -> str:
        """对 owner 与已记录命令 PID 逐个检查；任一非 stopped 即返回该状态。"""
        for pid in [owner_pid, *self.identities(action_key)]:
            state = checker(pid)
            if state != "stopped":
                return state
        return "stopped"

    def _path(self, action_key: str) -> Path:
        """把动作键映射为固定文件名，不把原键写入文件名。"""
        digest = hashlib.sha256(action_key.encode("utf-8")).hexdigest()[:32]
        return self._directory / f"action-{digest}.jsonl"


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
    on_pid: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess:
    """在共享门禁复核后执行一次有界命令；可选记录进程身份，失败不重试。"""
    if before is not None:
        before()
    gate.verify()  # type: ignore[attr-defined]
    if on_pid is None:
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
    try:
        process = subprocess.Popen(
            args,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
        )
    except FileNotFoundError:
        raise AdbCommandFailure("adb_missing") from None
    except OSError:
        raise AdbCommandFailure("adb_oserror") from None
    on_pid(process.pid)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AdbCommandFailure("adb_timeout") from None
    completed = subprocess.CompletedProcess(
        args=args, returncode=process.returncode, stdout=stdout, stderr=stderr
    )
    if completed.returncode != 0:
        raise AdbCommandFailure("adb_nonzero")
    return completed


def save_evidence(directory: Path | None, name: str, payload: bytes) -> None:
    """把证据写到调用方指定本机目录；未指定目录时跳过，不做隐式落盘。"""
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(payload)
