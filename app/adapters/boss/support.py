"""提供 BOSS 合成读取夹具共用的严格 JSON/XML/路径与基础节点解析。"""

from __future__ import annotations

import math
import re
from pathlib import Path
from xml.etree import ElementTree

from app.domain.json_input import load_json_object as _load_strict_json
from app.domain.models import is_nonempty_text, parse_utc

EPOCH = "1970-01-01T00:00:00.000Z"
COVERAGE_KEYS = frozenset({"kind", "top_proven", "bottom_proven"})
COVERAGE_KINDS = frozenset(
    {"viewport", "full_traversal", "calibrated_global_zero"}
)
_BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
_ASCII_INT_RE = re.compile(r"^[0-9]+$")


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


def validate_coverage_proof(value: object) -> dict[str, object]:
    """校验覆盖率证明固定三键、枚举与布尔证明位。"""
    if not isinstance(value, dict) or set(value) != COVERAGE_KEYS:
        raise ValueError("coverage proof is invalid")
    if not isinstance(value["kind"], str) or value["kind"] not in COVERAGE_KINDS:
        raise ValueError("coverage kind is invalid")
    if type(value["top_proven"]) is not bool or type(value["bottom_proven"]) is not bool:
        raise ValueError("coverage proof is invalid")
    return {
        "kind": value["kind"],
        "top_proven": value["top_proven"],
        "bottom_proven": value["bottom_proven"],
    }


def row_nodes(
    list_node: ElementTree.Element, suffix: str
) -> list[ElementTree.Element] | None:
    """按文档顺序取直属行；嵌套行返回 None 表示不完整。"""
    parents = parent_map(list_node)
    nodes = [
        node
        for node in list_node.iter()
        if node is not list_node and rid_matches(node, suffix)
    ]
    matched = set(nodes)
    for node in nodes:
        parent = parents.get(node)
        while parent is not None and parent is not list_node:
            if parent in matched:
                return None
            parent = parents.get(parent)
    return nodes


def collect_region_badge_values(
    row_node: ElementTree.Element,
    region: object,
    badge_class: str,
    excluded_suffixes: set[str],
) -> tuple[list[int], str | None]:
    """在行内标定区域读取唯一 ASCII 整数 TextView，返回计数或未知原因。"""
    if region is None:
        return [], "uncalibrated_region"
    row_bounds = parse_bounds(row_node)
    if row_bounds is None:
        return [], "uncalibrated_region"
    left, top, right, bottom = row_bounds
    width = right - left
    height = bottom - top
    try:
        region_rect = (
            left + float(region[0]) * width,
            top + float(region[1]) * height,
            left + float(region[2]) * width,
            top + float(region[3]) * height,
        )
    except (OverflowError, ValueError, TypeError):
        return [], "uncalibrated_region"
    values: list[int] = []
    invalid_bounds = False
    for node in row_node.iter():
        if node is row_node or node.get("class") != badge_class:
            continue
        if any(rid_matches(node, suffix) for suffix in excluded_suffixes):
            continue
        text = node.get("text")
        if not isinstance(text, str) or _ASCII_INT_RE.fullmatch(text) is None:
            continue
        bounds = parse_bounds(node)
        if bounds is None:
            invalid_bounds = True
            continue
        if not rect_inside(bounds, region_rect):
            continue
        values.append(int(text))
    if invalid_bounds:
        return [], "uncalibrated_region"
    if len(values) > 1:
        return [], "ambiguous_signal"
    if not values:
        return [], "missing_signal"
    return values, None


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
