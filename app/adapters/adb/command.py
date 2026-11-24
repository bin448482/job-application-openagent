"""提供 ADB 子进程的共享有界执行边界：参数数组、门禁复核、超时与不重试。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

ADB_TIMEOUT_SECONDS = 5
_REAL_POPEN = subprocess.Popen


class ProcessFamilyBound:
    """用 Windows Job Object 对单条命令族做有限约束/核验；不可用时返回 None。"""

    def __init__(self, handle: int) -> None:
        """保存 Job 句柄；构造不启动或终止任何进程。"""
        self._handle = handle

    @classmethod
    def create(cls) -> "ProcessFamilyBound | None":
        """创建不限制退出行为的空 Job；非 Windows 或 API 不可用时返回 None。"""
        if os.name != "nt":
            return None
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            return cls(int(handle))
        except Exception:
            return None

    def assign(self, pid: int) -> bool:
        """把命令进程加入 Job；失败表示无法获得有限族约束。"""
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.AssignProcessToJobObject.argtypes = (
                wintypes.HANDLE,
                wintypes.HANDLE,
            )
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(
                0x0100 | 0x0001, False, int(pid)
            )  # SET_QUOTA | TERMINATE
            if not process:
                return False
            try:
                assigned = kernel32.AssignProcessToJobObject(
                    self._handle, process
                )
            finally:
                kernel32.CloseHandle(process)
            return bool(assigned)
        except Exception:
            return False

    def resume(self, pid: int) -> bool:
        """恢复由 CREATE_SUSPENDED 启动的进程主线程；失败返回 False。"""
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            class ThreadEntry(ctypes.Structure):
                """THREADENTRY32 固定字段，用于定位进程主线程。"""

                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", ctypes.c_long),
                    ("tpDeltaPri", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.argtypes = (
                wintypes.DWORD,
                wintypes.DWORD,
            )
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Thread32First.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(ThreadEntry),
            )
            kernel32.Thread32First.restype = wintypes.BOOL
            kernel32.Thread32Next.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(ThreadEntry),
            )
            kernel32.Thread32Next.restype = wintypes.BOOL
            kernel32.OpenThread.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenThread.restype = wintypes.HANDLE
            kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
            kernel32.ResumeThread.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
            if not snapshot or snapshot == -1:
                return False
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(ThreadEntry)
            resumed = False
            try:
                more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
                while more:
                    if entry.th32OwnerProcessID == int(pid):
                        thread = kernel32.OpenThread(
                            0x0002, False, entry.th32ThreadID
                        )
                        if thread:
                            try:
                                if (
                                    kernel32.ResumeThread(thread)
                                    != 0xFFFFFFFF
                                ):
                                    resumed = True
                            finally:
                                kernel32.CloseHandle(thread)
                    entry.dwSize = ctypes.sizeof(ThreadEntry)
                    more = kernel32.Thread32Next(
                        snapshot, ctypes.byref(entry)
                    )
            finally:
                kernel32.CloseHandle(snapshot)
            return resumed
        except Exception:
            return False

    def terminate(self) -> bool:
        """终止 Job 内剩余进程，使命令族可证明停止。"""
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = (
                ctypes.c_void_p,
                ctypes.c_uint,
            )
            kernel32.TerminateJobObject.restype = ctypes.c_int
            return bool(kernel32.TerminateJobObject(self._handle, 1))
        except Exception:
            return False

    def active(self) -> int | None:
        """返回 Job 内活动进程数；查询失败返回 None。"""
        try:
            import ctypes
            from ctypes import wintypes

            class Accounting(ctypes.Structure):
                """JOBOBJECT_BASIC_ACCOUNTING_INFORMATION 固定字段。"""

                _fields_ = [
                    ("TotalUserTime", ctypes.c_longlong),
                    ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                    ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD),
                    ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD),
                    ("TotalTerminatedProcesses", wintypes.DWORD),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.QueryInformationJobObject.argtypes = (
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_void_p,
            )
            kernel32.QueryInformationJobObject.restype = ctypes.c_int
            info = Accounting()
            ok = kernel32.QueryInformationJobObject(
                self._handle,
                1,  # JobObjectBasicAccountingInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
            return int(info.ActiveProcesses) if ok else None
        except Exception:
            return None

    def close(self) -> None:
        """关闭 Job 句柄；不保留常驻监控。"""
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(self._handle)
        except Exception:
            return


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

    def begin_family(
        self,
        action_key: str,
        pid: int,
        child_scan: Callable[[int], list[int] | None],
    ) -> None:
        """开始一段命令族：先写 begin，再记录当前可见后代；失败不写完成标记。"""
        if type(pid) is not int or pid <= 0:
            raise ValueError("pid is invalid")
        self._append(action_key, {"begin": True})
        complete = self._expand(action_key, [pid], child_scan)
        if not complete:
            self.record_unresolved(action_key)

    def finish_family(
        self,
        action_key: str,
        pid: int,
        child_scan: Callable[[int], list[int] | None],
        *,
        bounded: bool = False,
        proven: bool = False,
    ) -> None:
        """命令结束：无平台有界约束或未证明族停止时保持 unknown，不假证 stopped。"""
        if bounded:
            self._append(
                action_key, {"complete": True} if proven else {"unresolved": True}
            )
            return
        self._expand(action_key, [pid], child_scan)
        self.record_unresolved(action_key)

    def record_tree(
        self,
        action_key: str,
        pid: int,
        child_scan: Callable[[int], list[int] | None],
    ) -> None:
        """一次性记录命令 PID、可见后代并在完整时闭合；供单次采集/测试使用。"""
        self._append(action_key, {"begin": True})
        complete = self._expand(action_key, [pid], child_scan)
        self._append(
            action_key, {"complete": True} if complete else {"unresolved": True}
        )

    def _expand(
        self,
        action_key: str,
        roots: list[int],
        child_scan: Callable[[int], list[int] | None],
    ) -> bool:
        """从根展开并记录新后代；任何一次枚举失败都返回 False 并标记 unknown。"""
        seen: set[int] = set()
        pending = [pid for pid in roots if type(pid) is int and pid > 0]
        complete = True
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            self.record(action_key, current)
            try:
                children = child_scan(current)
            except Exception:
                children = None
            if children is None:
                complete = False
                self.record_unresolved(action_key)
                continue
            pending.extend(
                child for child in children if type(child) is int and child > 0
            )
        return complete

    def identities(self, action_key: str) -> list[int]:
        """只读返回动作已记录的 PID 去重列表；缺失/损坏返回空。"""
        return self._load(action_key)[0]

    def check(
        self,
        action_key: str,
        owner_pid: int,
        checker: Callable[[int], str],
    ) -> str:
        """核对 owner 与已记录命令族；无记录/损坏/未闭合或任一存活均拒绝。"""
        pids, unresolved, begins, completes = self._load(action_key)
        if unresolved or not pids or begins == 0 or begins != completes:
            return "unknown"
        for pid in [owner_pid, *pids]:
            state = checker(pid)
            if state != "stopped":
                return state
        return "stopped"

    def _load(self, action_key: str) -> tuple[list[int], bool, int, int]:
        """解析账本为 PID、不可证明标志与 begin/complete 计数；坏行按不可证明。"""
        path = self._path(action_key)
        if not path.is_file():
            return [], True, 0, 0
        pids: set[int] = set()
        unresolved = False
        begins = 0
        completes = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return [], True, 0, 0
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                return [], True, 0, 0
            if not isinstance(item, dict):
                return [], True, 0, 0
            if item.get("unresolved") is True:
                unresolved = True
            elif item.get("begin") is True:
                begins += 1
            elif item.get("complete") is True:
                completes += 1
            elif "pid" in item:
                pid = item["pid"]
                if type(pid) is not int or pid <= 0:
                    return [], True, 0, 0
                pids.add(pid)
            else:
                return [], True, 0, 0
        return sorted(pids), unresolved, begins, completes

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
    on_done: Callable[[int, bool, bool], None] | None = None,
    bound_factory: Callable[[], ProcessFamilyBound | None] | None = None,
    real_process_check: Callable[[object], bool] | None = None,
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
    bound = (bound_factory or ProcessFamilyBound.create)()
    creationflags = 0x00000004 if bound is not None else 0  # CREATE_SUSPENDED
    try:
        process = subprocess.Popen(
            args,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            creationflags=creationflags,
        )
    except FileNotFoundError:
        if bound is not None:
            bound.close()
        raise AdbCommandFailure("adb_missing") from None
    except OSError:
        if bound is not None:
            bound.close()
        raise AdbCommandFailure("adb_oserror") from None
    real_process = (real_process_check or _is_real_process)(process)
    if bound is not None:
        if not real_process:
            bound.close()
            bound = None
        else:
            assigned = bound.assign(process.pid)
            resumed = bound.resume(process.pid)
            if not resumed:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                bound.close()
                raise AdbCommandFailure("adb_oserror") from None
            if not assigned:
                bound.close()
                bound = None
    try:
        on_pid(process.pid)
    except Exception:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if bound is not None:
            bound.terminate()
            bound.close()
        raise AdbCommandFailure("adb_oserror") from None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.wait(timeout=1)
        _finalize(on_done, process.pid, bound)
        raise AdbCommandFailure("adb_timeout") from None
    _finalize(on_done, process.pid, bound)
    completed = subprocess.CompletedProcess(
        args=args, returncode=process.returncode, stdout=stdout, stderr=stderr
    )
    if completed.returncode != 0:
        raise AdbCommandFailure("adb_nonzero")
    return completed


def _is_real_process(process: object) -> bool:
    """只在真实 Windows Popen（含真实 _handle）上启用 Job 约束，避免 mock/fake PID。"""
    try:
        real_class = isinstance(process, _REAL_POPEN)
    except TypeError:
        return False
    return real_class and isinstance(getattr(process, "_handle", None), int)


def _finalize(
    on_done: Callable[[int, bool, bool], None] | None,
    pid: int,
    bound: ProcessFamilyBound | None,
) -> None:
    """命令结束后先约束/核验命令族，再闭合账本；失败只让账本保持 unknown。"""
    bounded = bound is not None
    proven = False
    if bound is not None:
        try:
            for _attempt in range(20):
                if not bound.terminate():
                    break
                active = bound.active()
                if active == 0:
                    proven = True
                    break
                if active is None:
                    break
                time.sleep(0.025)
        except Exception:
            proven = False
        finally:
            bound.close()
    if on_done is None:
        return
    try:
        on_done(pid, bounded, proven)
    except Exception:
        return


def save_evidence(directory: Path | None, name: str, payload: bytes) -> None:
    """把证据写到调用方指定本机目录；未指定目录时跳过，不做隐式落盘。"""
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(payload)
