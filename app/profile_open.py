"""提供 facts/run/restore 共用的 profile 打开过程日志边界。"""

from __future__ import annotations

import time

from app.process_log import ProcessLog, elapsed_ms


def open_profile_store(store: object, process_log: ProcessLog | None) -> object:
    """调用 store.open，并记录唯一 profile/open_profile started 与终态。

    失败原因只取异常对象的固定 reason_code，不写路径、事实值或异常原文。
    """
    started = time.perf_counter()
    _record(process_log, "started")
    try:
        result = store.open()  # type: ignore[attr-defined]
    except Exception as error:
        reason = getattr(error, "reason_code", None)
        _record(
            process_log,
            "failed",
            started=started,
            reason_code=reason if isinstance(reason, str) else None,
        )
        raise
    _record(process_log, "ok", started=started)
    return result


def _record(
    process_log: ProcessLog | None,
    result: str,
    *,
    started: float | None = None,
    reason_code: str | None = None,
) -> None:
    """写唯一 profile 打开日志事件；日志失败不影响业务。"""
    if process_log is None:
        return
    try:
        process_log.record(
            stage="profile",
            operation="open_profile",
            result=result,
            reason_code=reason_code,
            error_category="profile" if result == "failed" else None,
            duration_ms=elapsed_ms(started) if started is not None else 0,
        )
    except Exception:
        return
