"""提供 CLI 与适配器共用的严格 JSON 输入解析，拒绝重复对象键。"""

from __future__ import annotations

import json
from typing import Callable

JsonPairs = list[tuple[str, object]]


def reject_duplicate_keys(pairs: JsonPairs) -> dict[str, object]:
    """把 JSON 对象键值对转为字典；任意层级出现重复键时抛出 ValueError。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def load_json_object(
    text: str,
    *,
    object_pairs_hook: Callable[[JsonPairs], dict[str, object]] = reject_duplicate_keys,
) -> object:
    """解析严格 UTF-8 JSON 文本，由调用方保留文件 IO 与错误分类。"""
    return json.loads(text, object_pairs_hook=object_pairs_hook)
