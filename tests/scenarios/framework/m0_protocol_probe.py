"""在隔离根内用假设备边界验证 M0 ADB 探针的超时、非零与不重试协议。

只替换 subprocess 设备边界，导入并调用已交付 app 的 `AdbDeviceProbe`；不启动
真实 ADB、不读取设备、不写业务库，也不复制模块单元测试的解析细节。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from types import SimpleNamespace


def _run_case(case: str) -> dict:
    """执行一个合成边界用例并返回固定 JSON 结果；不输出异常原文。"""
    from app.adapters.adb.device import AdbDeviceProbe, DeviceProbeError

    calls: list[tuple] = []

    def fake_run(*args, **kwargs):
        """记录设备边界调用并返回合成超时/非零结果，不启动进程。"""
        calls.append((args, kwargs))
        if case == "timeout":
            raise subprocess.TimeoutExpired(cmd=["adb"], timeout=5)
        if case == "nonzero":
            return SimpleNamespace(returncode=9, stdout="")
        raise AssertionError("未知合成用例")

    errors: list[str] = []
    reason_code = None
    original_run = subprocess.run
    try:
        subprocess.run = fake_run
        AdbDeviceProbe().probe("synthetic-serial")
    except DeviceProbeError as exc:
        reason_code = exc.reason_code
    except Exception:
        errors.append("DeviceProbeError 未按契约抛出")
    finally:
        subprocess.run = original_run
    if reason_code != "device_disconnected":
        errors.append("reason_code 必须为 device_disconnected")
    if len(calls) != 1:
        errors.append("设备命令必须只调用一次，不重试")
    if calls:
        args, kwargs = calls[0]
        if not args or not isinstance(args[0], list):
            errors.append("命令必须以参数数组传入")
        elif args[0] != ["adb", "devices"]:
            errors.append("首条命令必须为 adb devices")
        if kwargs.get("shell") is not False:
            errors.append("shell 必须显式为 false")
        if kwargs.get("timeout") != 5:
            errors.append("超时必须为 5 秒")
    return {
        "schema_version": 1,
        "case": case,
        "status": "ok" if not errors else "failed",
        "reason_code": reason_code,
        "call_count": len(calls),
        "shell": False if not calls else calls[0][1].get("shell"),
        "timeout_seconds": None if not calls else calls[0][1].get("timeout"),
        "retried": len(calls) > 1,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    """解析单一合成用例并输出 JSON；失败退出 2，不打印 traceback。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("timeout", "nonzero"), required=True)
    args = parser.parse_args(argv)
    try:
        result = _run_case(args.case)
    except Exception:
        result = {
            "schema_version": 1,
            "case": args.case,
            "status": "failed",
            "reason_code": None,
            "call_count": 0,
            "shell": None,
            "timeout_seconds": None,
            "retried": False,
            "errors": ["探针用例异常"],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
