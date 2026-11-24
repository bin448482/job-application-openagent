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
        self._append(action_key, {"pid": pid})

    def record_unresolved(self, action_key: str) -> None:
        """标记该动作命令族无法完整枚举；恢复时必须保持 unknown。"""
        self._append(action_key, {"unresolved": True})

    def record_tree(
        self,
        action_key: str,
        pid: int,
        child_scan: Callable[[int], list[int] | None],
    ) -> None:
        """记录命令 PID 及其当前可见后代；枚举失败写不可证明标记。"""
        seen: set[int] = set()
        pending = [pid]
        while pending:
            current = pending.pop()
            if current in seen or current <= 0:
                continue
            seen.add(current)
            self.record(action_key, current)
            try:
                children = child_scan(current)
            except Exception:
                children = None
            if children is None:
                self.record_unresolved(action_key)
                continue
            pending.extend(children)

    def identities(self, action_key: str) -> list[int]:
        """只读返回动作已记录的 PID 去重列表；缺失/损坏返回空。"""
        pids, _unresolved = self._load(action_key)
        return pids

    def check(
        self,
        action_key: str,
        owner_pid: int,
        checker: Callable[[int], str],
    ) -> str:
        """核对 owner 与已记录命令族；无记录/损坏/不可枚举或任一存活均拒绝。"""
        pids, unresolved = self._load(action_key)
        if unresolved or not pids:
            return "unknown"
        for pid in [owner_pid, *pids]:
            state = checker(pid)
            if state != "stopped":
                return state
        return "stopped"

    def _load(self, action_key: str) -> tuple[list[int], bool]:
        """解析账本为 PID 列表与不可证明标志；缺失/坏行按不可证明处理。"""
        path = self._path(action_key)
        if not path.is_file():
            return [], True
        pids: set[int] = set()
        unresolved = False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return [], True
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                return [], True
            if not isinstance(item, dict):
                return [], True
            if item.get("unresolved") is True:
                unresolved = True
            elif "pid" in item:
                pid = item["pid"]
                if type(pid) is not int or pid <= 0:
                    return [], True
                pids.add(pid)
            else:
                return [], True
        return sorted(pids), unresolved

    def _append(self, action_key: str, item: dict[str, object]) -> None:
        """追加一条固定形状记录；写入失败向上抛出，不伪造记录。"""
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._path(action_key).open(
            "a", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")

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
    try:
        on_pid(process.pid)
    except Exception:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise AdbCommandFailure("adb_oserror") from None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.wait(timeout=1)
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
