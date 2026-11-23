"""提供 CLI 与适配器共用的严格 JSON 输入解析，拒绝重复对象键。"""

from __future__ import annotations

import json

JsonPairs = list[tuple[str, object]]


def reject_duplicate_keys(pairs: JsonPairs) -> dict[str, object]:
    """把 JSON 对象键值对转为字典；任意层级出现重复键时抛出 ValueError。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def load_json_object(text: str) -> object:
    """解析严格 JSON 文本，固定使用共享重复键校验，不负责文件 IO。"""
    return json.loads(text, object_pairs_hook=reject_duplicate_keys)
