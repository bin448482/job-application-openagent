"""提供 BOSS 合成读取夹具共用的严格 JSON/XML/路径与基础节点解析。"""

from __future__ import annotations

import math
import re
from pathlib import Path
from xml.etree import ElementTree

from app.domain.json_input import load_json_object as _load_strict_json
from app.domain.models import is_nonempty_text, parse_utc

EPOCH = "1970-01-01T00:00:00.000Z"
_BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")


class XmlShapeError(Exception):
    """表示受控 XML 形状错误，由调用方映射为 invalid_xml。"""


class FixtureEncodingError(ValueError):
    """表示夹具 XML 文件编码损坏，用于与 manifest/IO 错误区分。"""


def read_json_file(path: Path) -> object:
    """读取严格 UTF-8 JSON，并复用共享重复对象键校验。"""
    text = path.read_text(encoding="utf-8")
    return _load_strict_json(text)


def read_xml_text(path: Path) -> str:
    """以严格 UTF-8 读取 XML；编码损坏映射为 invalid_xml 专用错误。"""
    try:
        data = path.read_bytes()
    except OSError:
        raise ValueError("xml file is unreadable") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise FixtureEncodingError("xml encoding is invalid") from None


def resolve_fixture_reference(base: Path, value: str) -> Path:
    """把夹具引用限制在夹具目录内，拒绝绝对路径、.. 与链接。"""
    if not is_nonempty_text(value) or value.startswith("~"):
        raise ValueError("fixture reference is invalid")
    candidate = Path(value)
    if candidate.is_absolute() or candidate.drive or candidate.anchor:
        raise ValueError("fixture reference is absolute")
    if ".." in candidate.parts:
        raise ValueError("fixture reference escapes")
    current = base
    for part in candidate.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("fixture reference is a link")
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise ValueError("fixture reference is invalid")
    return resolved


def parse_xml_root(xml_text: str) -> ElementTree.Element:
    """拒绝损坏 Unicode、DTD/ENTITY、非 hierarchy 根和非 node 元素后解析 XML。"""
    try:
        xml_text.encode("utf-8")
    except UnicodeError:
        raise XmlShapeError from None
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise XmlShapeError
    try:
        root = ElementTree.fromstring(xml_text)
    except (ElementTree.ParseError, UnicodeError, ValueError):
        raise XmlShapeError from None
    if root.tag != "hierarchy":
        raise XmlShapeError
    for element in root.iter():
        if element is root:
            continue
        if element.tag != "node":
            raise XmlShapeError
    return root


def parent_map(
    root: ElementTree.Element,
) -> dict[ElementTree.Element, ElementTree.Element]:
    """构建元素到直接父节点的映射，用于容器与行内排除。"""
    return {child: parent for parent in root.iter() for child in list(parent)}


def parse_bounds(
    element: ElementTree.Element,
) -> tuple[int, int, int, int] | None:
    """严格解析四个非负整数且正尺寸的 bounds，否则返回 None。"""
    value = element.get("bounds")
    if not isinstance(value, str):
        return None
    match = _BOUNDS_RE.fullmatch(value)
    if match is None:
        return None
    left, top, right, bottom = (int(group) for group in match.groups())
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def rect_inside(
    inner: tuple[int, int, int, int], outer: tuple[float, float, float, float]
) -> bool:
    """判断节点矩形是否完全落在标定区域内（含边界）。"""
    left, top, right, bottom = inner
    outer_left, outer_top, outer_right, outer_bottom = outer
    return (
        left >= outer_left
        and top >= outer_top
        and right <= outer_right
        and bottom <= outer_bottom
    )


def rid_matches(node: ElementTree.Element, suffix: str) -> bool:
    """按 resource-id 的 :id/ 后缀边界匹配，避免子串误命中。"""
    rid = node.get("resource-id")
    if not isinstance(rid, str):
        return False
    return (
        rid == suffix
        or rid.endswith(":id/" + suffix)
        or rid.endswith("/" + suffix)
    )


def require_text(value: object) -> str:
    """要求非空且可 UTF-8 编码的字符串。"""
    if not is_nonempty_text(value):
        raise ValueError("text is invalid")
    return str(value)


def require_time(value: object) -> str:
    """要求规范毫秒 UTC 时间字符串。"""
    parse_utc(value)
    return str(value)


def safe_time(value: object) -> str:
    """把无效时间替换为固定纪元值，仅用于构造安全失败结果。"""
    try:
        parse_utc(value)
    except ValueError:
        return EPOCH
    return str(value)


def validate_ratio_region(value: object) -> list[float]:
    """校验相对多边形的 0–1 有限数值、正面积，拒绝 bool 与巨大整数溢出。"""
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("ratio region is invalid")
    numbers: list[float] = []
    for item in value:
        if type(item) is int:
            if item < 0 or item > 1:
                raise ValueError("ratio region is invalid")
            numbers.append(float(item))
        elif type(item) is float:
            if not math.isfinite(item) or item < 0 or item > 1:
                raise ValueError("ratio region is invalid")
            numbers.append(item)
        else:
            raise ValueError("ratio region is invalid")
    if numbers[0] >= numbers[2] or numbers[1] >= numbers[3]:
        raise ValueError("ratio region is invalid")
    return numbers
