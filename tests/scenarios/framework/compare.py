"""比较真实产品输出与独立预期，不修改被测结果。

比较器对 JSON 类型做严格比较（bool 不等于整数），并要求普通业务 CLI 的 stdout
是单个 JSON 对象；帮助等非业务输出只能通过显式 allow_non_json_stdout 走独立契约。
"""

from __future__ import annotations

from typing import Any

MISSING = object()
JSON_TYPES = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def resolve_path(value: Any, path: str) -> Any:
    """按点分路径读取 JSON 值，数组用数字下标；不存在返回 MISSING。"""
    if path == "":
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return MISSING
            if index < 0 or index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def strict_equal(actual: Any, expected: Any) -> bool:
    """按 JSON 类型严格比较，bool 与整数、null 与缺失互不相等。"""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and actual == expected
    if expected is None or actual is None:
        return expected is None and actual is None
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            strict_equal(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            strict_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def _type_ok(actual: Any, name: str) -> bool:
    """检查声明类型；整数值为 bool 时明确拒绝。"""
    target = JSON_TYPES[name]
    if name == "integer":
        return isinstance(actual, int) and not isinstance(actual, bool)
    if name == "boolean":
        return isinstance(actual, bool)
    return isinstance(actual, target)


def compare_step(stdout: Any, step: dict[str, Any]) -> list[str]:
    """按一条步骤预期比较 stdout，返回按路径定位的差异。"""
    errors: list[str] = []
    if "stdout_json_equals" in step:
        if not strict_equal(stdout, step["stdout_json_equals"]):
            errors.append("stdout 与独立预期不完全一致（含 JSON 类型严格比较）")
    for assertion in step.get("stdout_paths", []):
        path = assertion.get("path", "")
        if not isinstance(path, str):
            errors.append("stdout_paths 路径必须是字符串")
            continue
        actual = resolve_path(stdout, path)
        if actual is MISSING:
            errors.append(f"缺少输出路径 {path or '<root>'}")
            continue
        if "equals" in assertion and not strict_equal(actual, assertion["equals"]):
            errors.append(f"{path or '<root>'} 期望 {assertion['equals']!r}，实际 {actual!r}")
        if "in" in assertion and not any(strict_equal(actual, item) for item in assertion["in"]):
            errors.append(f"{path or '<root>'} 实际 {actual!r} 不在允许集合")
        if "type" in assertion:
            expected_type = assertion["type"]
            if expected_type not in JSON_TYPES:
                errors.append(f"{path or '<root>'} 声明了未知类型 {expected_type!r}")
            elif not _type_ok(actual, expected_type):
                errors.append(f"{path or '<root>'} 类型应为 {expected_type}")
        if "length_equals" in assertion:
            if not isinstance(actual, list) or len(actual) != assertion["length_equals"]:
                errors.append(f"{path or '<root>'} 长度应为 {assertion['length_equals']}")
        if "keys" in assertion:
            expected_keys = assertion["keys"]
            if not isinstance(actual, dict):
                errors.append(f"{path or '<root>'} 不是对象，无法检查键集合")
            elif set(actual) != set(expected_keys):
                errors.append(f"{path or '<root>'} 键集合必须恰为 {sorted(expected_keys)}")
    return errors


def compare_invocation(
    result: dict[str, Any],
    acceptance: dict[str, Any],
) -> list[str]:
    """比较一次子进程调用的退出码、JSON 类型与 stdout 断言。"""
    errors: list[str] = []
    exit_expectation = acceptance.get("exit_code")
    if exit_expectation is not None and result.get("exit_code") != exit_expectation:
        errors.append(f"退出码期望 {exit_expectation}，实际 {result.get('exit_code')}")
    if acceptance.get("allow_non_json_stdout"):
        if acceptance.get("require_stdout_nonempty") and not str(result.get("stdout_text", "")).strip():
            errors.append("帮助/非 JSON 输出不得为空")
    else:
        if not result.get("stdout_json_ok"):
            errors.append("普通业务 CLI 的 stdout 不是单个 JSON 对象")
        elif not isinstance(result.get("stdout_json"), dict):
            errors.append("普通业务 CLI 的 stdout 顶层必须是 JSON 对象")
        else:
            errors.extend(compare_step(result["stdout_json"], acceptance))
            required = acceptance.get("required_stdout_keys")
            if required and set(result["stdout_json"]) != set(required):
                errors.append(f"stdout 顶层键必须恰为 {sorted(required)}")
    if acceptance.get("stderr_empty") and result.get("stderr"):
        errors.append("stderr 非空")
    for needle in acceptance.get("stderr_forbidden", []):
        if needle and needle in result.get("stderr", ""):
            errors.append("stderr 回显了不应出现的原文或路径")
    return errors
