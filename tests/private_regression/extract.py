#!/usr/bin/env python3
"""从旧项目只读抽取 UI XML 与失败日志，生成脱敏合成派生样本与安全回执。

本工具只做两件事：把受控发现的 Android uiautomator XML 按白名单重构成纯合成结构，
以及把 `data/operations.log` 中固定白名单事件的相对时序压缩成待标注失败窗口。
原始正文、源文件名/路径、绝对时间、原始哈希和可关联源 ID 不进入派生正文、stdout
或安全回执；本机来源映射单独写入同批次的 `provenance.local.json`。

本模块不是通用 ETL，也不执行源项目代码、读取其数据库/配置/凭据或连接设备。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat as stat_module
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

TOOL_NAME = "private-regression-extractor"
TOOL_VERSION = "0.1.0"
SCHEMA_RECEIPT = "private-regression-safe-receipt/v1"
SCHEMA_VALIDATION = "private-regression-validation/v1"
SCHEMA_MANIFEST = "private-regression-manifest/v1"
SCHEMA_WINDOW = "private-regression-failure-window/v1"
SCHEMA_PROVENANCE = "private-regression-provenance/v1"

# 扫描与读取预算：命中任一上限即在回执中标记 truncated/partial，不静默继续。
MAX_FILES_SCANNED = 20_000
MAX_WALK_DEPTH = 10
MAX_TOTAL_READ_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_XML_DEPTH = 64
MAX_XML_NODES = 6_000
MAX_XML_SAMPLES = 48
MAX_LOG_BYTES = 128 * 1024 * 1024
MAX_LOG_LINES = 500_000
MAX_LOG_LINE_BYTES = 64 * 1024
MAX_FAILURE_WINDOWS = 12
MAX_WINDOW_EVENTS = 40
WINDOW_CLUSTER_GAP = 6
WINDOW_PAD = 4
MAX_COORDINATE = 20_000
MAX_BOUNDED_NUMBER = 1_000_000_000
MAX_JSON_BYTES = 1024 * 1024
MAX_VALIDATE_FILES = 256

XML_FAULTS = (
    "dtd_or_entity",
    "invalid_encoding",
    "malformed_xml",
    "oversize",
    "too_deep",
    "too_many_nodes",
    "io_error",
)
ATTRIBUTE_FAULTS = (
    "unknown_attr",
    "invalid_bounds",
    "invalid_boolean",
    "unknown_class",
    "unknown_tag",
)
FAILURE_CATEGORIES = (
    "transport_failure",
    "executor_error",
    "read_incomplete",
    "filter_unverified",
    "direction_unknown",
    "verification_insufficient",
    "cleanup_unverified",
    "unread_unknown",
    "action_before_failure",
    "pending_crash",
    "post_click_disconnect",
    "local_commit_failed",
)
ACTION_IDS = ("A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08")
UNCOVERED_REASONS = (
    "none",
    "no_xml_candidates",
    "log_missing",
    "no_failure_evidence",
    "truncated_before_evidence",
    "log_read_error",
)
ERROR_REASONS = (
    "usage_error",
    "source_missing",
    "path_escape",
    "symlink_rejected",
    "output_overlap",
    "output_not_ignored",
    "git_unavailable",
    "output_exists",
    "source_changed",
    "internal_error",
)

BOOL_ATTRS = (
    "checkable",
    "checked",
    "clickable",
    "enabled",
    "focusable",
    "focused",
    "scrollable",
    "long-clickable",
    "password",
    "selected",
)
KNOWN_ATTRS = frozenset(
    {"class", "bounds", "resource-id", "package", "text", "content-desc", *BOOL_ATTRS}
)

# 只保留能对应到 Android 通用控件语义的类名；其余统一退化为 android.view.View。
CLASS_WHITELIST = frozenset(
    {
        "android.view.View",
        "android.view.ViewGroup",
        "android.widget.TextView",
        "android.widget.Button",
        "android.widget.EditText",
        "android.widget.ImageView",
        "android.widget.ImageButton",
        "android.widget.CheckBox",
        "android.widget.RadioButton",
        "android.widget.Switch",
        "android.widget.ProgressBar",
        "android.widget.SeekBar",
        "android.widget.Spinner",
        "android.widget.ListView",
        "android.widget.GridView",
        "android.widget.ScrollView",
        "android.widget.HorizontalScrollView",
        "android.widget.LinearLayout",
        "android.widget.FrameLayout",
        "android.widget.RelativeLayout",
        "android.widget.GridLayout",
        "android.widget.TableLayout",
        "android.widget.TableRow",
        "android.widget.Toolbar",
        "android.webkit.WebView",
        "androidx.recyclerview.widget.RecyclerView",
        "androidx.viewpager.widget.ViewPager",
        "androidx.viewpager2.widget.ViewPager2",
    }
)
SAFE_TAGS = frozenset({"hierarchy", "node"})
SYNTHETIC_CLASS = "android.view.View"
SYNTHETIC_PACKAGE = "com.synthetic.app"
SYNTHETIC_TEXT = "TEXT"
SYNTHETIC_DESC = "DESC"
SYNTHETIC_ROTATION_MAX = 3
BOUNDS_RE = re.compile(r"^\[(\d{1,6}),(\d{1,6})\]\[(\d{1,6}),(\d{1,6})\]$")
DATE_RE = re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b")
NODE_TAG_RE = re.compile(r"<(/?)node\b[^>]*>")

# 日志字段白名单：只读取这些键，不把任意键或任意值带出。
EVENT_KEYS = ("event", "event_name", "name", "type")
REASON_KEYS = ("reason_code", "reason", "detail_code", "detail")
STATUS_KEYS = ("status", "result", "state")
ACTION_KEYS = ("action", "action_type", "kind")
TIME_KEYS = ("timestamp", "ts", "time", "created_at", "at", "event_time")
BOUNDED_NUMBER_KEYS = (
    "duration_ms",
    "elapsed_ms",
    "waited_ms",
    "latency_ms",
    "retry_count",
    "attempt",
    "count",
    "size_bytes",
)

EVENT_MAP = {
    "adb_start": "transport",
    "adb_finished": "transport",
    "adb_failed": "transport_failure",
    "executor_error": "executor_error",
    "read_incomplete": "read_incomplete",
    "followup_read_incomplete": "read_incomplete",
    "filter_unverified": "filter_unverified",
    "unread_filter_failed": "filter_unverified",
    "no_direction_evidence": "direction_unknown",
    "latest_message_not_incoming": "direction_unknown",
    "needs_probe": "verification_insufficient",
    "post_reply_followup_unknown": "verification_insufficient",
    "menu_unverified": "cleanup_unverified",
    "delete_unverified": "cleanup_unverified",
    "badge_unknown_rows": "unread_unknown",
    "stage_preempted": "scheduling",
    "task_claimed": "scheduling",
    "task_started": "scheduling",
    "task_finished": "scheduling",
    "lease_renewed": "scheduling",
    "lease_recovered": "scheduling",
    "unread_filter_applied": "filter_applied",
    "entry_absent": "filter_applied",
}
REASON_MAP = {
    "authorization_missing": "authorization_missing",
    "precondition_failed": "precondition_failed",
    "cancelled_before_action": "cancelled_before_action",
    "device_disconnected": "device_disconnected",
    "executor_interrupted": "executor_interrupted",
    "result_unverified": "result_unverified",
    "local_commit_failed": "local_commit_failed",
    "timeout": "timeout",
    "transport_error": "transport_error",
    "identity_unavailable": "identity_unavailable",
    "not_unique": "not_unique",
}
STATUS_MAP = {
    "performed": "performed",
    "not_performed": "not_performed",
    "request_sent": "request_sent",
    "sent": "sent",
    "unknown": "unknown",
    "blocked": "blocked",
    "deleted": "deleted",
    "applied": "applied",
    "failed": "failed",
    "ok": "ok",
    "success": "ok",
    "pending": "pending",
}
ACTION_MAP = {
    "text": "A01",
    "reply": "A01",
    "send_text": "A01",
    "resume": "A02",
    "resume_send": "A02",
    "wechat": "A03",
    "phone": "A04",
    "delete": "A05",
    "cleanup": "A05",
    "job_apply": "A06",
    "apply": "A06",
    "manual": "A07",
    "reconcile": "A07",
    "evidence": "A08",
}
FAILURE_EVENT_CATEGORIES = frozenset(
    {
        "transport_failure",
        "executor_error",
        "read_incomplete",
        "filter_unverified",
        "direction_unknown",
        "verification_insufficient",
        "cleanup_unverified",
        "unread_unknown",
    }
)
FAILURE_REASONS = frozenset(
    {
        "authorization_missing",
        "precondition_failed",
        "cancelled_before_action",
        "device_disconnected",
        "executor_interrupted",
        "result_unverified",
        "local_commit_failed",
        "timeout",
        "transport_error",
        "identity_unavailable",
        "not_unique",
    }
)
FAILURE_STATUSES = frozenset({"failed", "blocked"})
FORBIDDEN_OUTPUT_KEYS = frozenset(
    {
        "source",
        "source_path",
        "source_root",
        "path",
        "file",
        "filename",
        "file_path",
        "absolute_path",
        "sha256",
        "hash",
        "timestamp",
        "date",
        "time",
        "raw",
        "oracle",
        "pass",
    }
)


class ExtractionError(Exception):
    """携带固定错误码的拒绝，不保存原始异常文本或路径。"""

    def __init__(self, reason: str) -> None:
        """记录白名单中的错误原因码。"""
        if reason not in ERROR_REASONS:
            reason = "internal_error"
        self.reason = reason
        super().__init__(reason)


class _Anomaly(Exception):
    """XML 内容级异常，只携带固定故障类别。"""

    def __init__(self, fault: str) -> None:
        """记录白名单中的 XML 故障类别。"""
        self.fault = fault if fault in XML_FAULTS else "malformed_xml"
        super().__init__(self.fault)


class _BudgetExceeded(Exception):
    """读取预算耗尽，调用方按截断处理。"""


@dataclass
class DiscoveryStats:
    """受控发现阶段的计数与上限命中状态。"""

    files_scanned: int = 0
    dirs_scanned: int = 0
    roots_present: int = 0
    skipped_symlink: int = 0
    skipped_nonregular: int = 0
    skipped_oversize: int = 0
    file_cap_hit: bool = False
    depth_cap_hit: bool = False
    xml_candidates: list[Path] = field(default_factory=list)
    oversize_candidates: list[Path] = field(default_factory=list)
    log_path: Path | None = None
    log_present: bool = False


@dataclass
class SanitizeOutcome:
    """单份 XML 的白名单重构结果。"""

    xml_text: str
    node_count: int
    max_depth: int
    faults: dict[str, int]
    anomaly: str | None = None


@dataclass
class ReadRecord:
    """已读取源文件的本地完整性记录，只用于 provenance 与不变性校验。"""

    relative: str
    size: int
    mtime_ns: int
    sha256: str
    complete: bool = True


@dataclass
class ReadBudget:
    """受控读取预算；所有正文读取（含校验重读）都必须先扣减。"""

    limit: int
    used: int = 0
    exhausted: bool = False

    def remaining(self) -> int:
        """返回剩余可读取字节数。"""
        return max(0, self.limit - self.used)

    def consume(self, amount: int) -> None:
        """记录已读取字节；超过上限时标记耗尽。"""
        self.used += max(0, amount)
        if self.used >= self.limit:
            self.exhausted = True


@dataclass
class TimelineEvent:
    """一条已按白名单映射的日志事件，不保留任何原始字符串。"""

    event: str
    reason: str
    status: str
    action: str
    ts: datetime | None
    values: dict[str, int]
    failure: bool


@dataclass
class WindowCandidate:
    """未命名的失败窗口候选。"""

    signature: tuple[object, ...]
    events: list[TimelineEvent]
    first_index: int
    trimmed: bool = False


class AliasBook:
    """为 resource-id 生成进程内唯一的合成别名并保留重复关系。"""

    def __init__(self, token_factory: Callable[[], str] | None = None) -> None:
        """可注入确定性 token 工厂，生产默认使用随机合成 token。"""
        self._factory = token_factory or (lambda: "res_" + secrets.token_hex(4))
        self._map: dict[str, str] = {}
        self._used: set[str] = set()

    def alias(self, original: str) -> str:
        """同一原始 resource-id 始终得到同一别名，不同值得到不同别名。"""
        if original in self._map:
            return self._map[original]
        for _ in range(64):
            token = self._factory()
            if token not in self._used:
                self._used.add(token)
                self._map[original] = token
                return token
        raise ExtractionError("internal_error")


def _is_link_like(path: Path) -> bool:
    """识别符号链接、junction 及其他 Windows reparse point。"""
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(st, "st_file_attributes", 0) & reparse)


def _assert_no_reparse_ancestors(path: Path) -> None:
    """在规范化前检查原始词法路径每个已存在组件（含根/祖先/自身）。"""
    parts = path.parts
    if not parts:
        return
    current = Path(parts[0])
    if current.exists() and _is_link_like(current):
        raise ExtractionError("symlink_rejected")
    for part in parts[1:]:
        current = current / part
        if not current.exists():
            break
        if _is_link_like(current):
            raise ExtractionError("symlink_rejected")


def _assert_within(root: Path, candidate: Path) -> None:
    """在读取前确认原始根与候选路径没有链接祖先且在允许根内。"""
    _assert_no_reparse_ancestors(root)
    _assert_no_reparse_ancestors(candidate)
    root_resolved = Path(os.path.realpath(root))
    candidate_resolved = Path(os.path.realpath(candidate))
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ExtractionError("path_escape") from exc
    try:
        relative = candidate.resolve().relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        raise ExtractionError("path_escape") from exc
    current = root_resolved
    for part in relative.parts:
        current = current / part
        if _is_link_like(current):
            raise ExtractionError("symlink_rejected")


def _paths_overlap(left: Path, right: Path) -> bool:
    """判断两个目录是否存在包含关系或相同。"""
    a = os.path.normcase(os.path.realpath(left))
    b = os.path.normcase(os.path.realpath(right))
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _find_repo_root(start: Path) -> Path | None:
    """向上查找最近的 Git 工作区根，不读取工作区内容。"""
    current = start
    for _ in range(64):
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def _git_ignore_status(target: Path) -> tuple[bool, bool]:
    """返回 (是否被 Git 忽略, 是否已被跟踪)；Git 不可用时抛固定错误。"""
    repo = _find_repo_root(target if target.exists() else target.parent)
    if repo is None:
        raise ExtractionError("git_unavailable")
    try:
        rel = target.resolve().relative_to(repo.resolve())
    except (ValueError, OSError) as exc:
        raise ExtractionError("git_unavailable") from exc
    rel_text = rel.as_posix()
    try:
        ignored = (
            subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", "--", rel_text],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        tracked = (
            subprocess.run(
                ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel_text],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError as exc:
        raise ExtractionError("git_unavailable") from exc
    return ignored, tracked


def _assert_output_safe(output_root: Path) -> None:
    """在写入前确认输出根被 Git 忽略且未跟踪。"""
    ignored, tracked = _git_ignore_status(output_root)
    if tracked or not ignored:
        raise ExtractionError("output_not_ignored")


def _new_batch_id() -> str:
    """生成无日期、无源信息的唯一批次目录名。"""
    return "b-" + secrets.token_hex(6)


def _walk_xml(root: Path, stats: DiscoveryStats) -> None:
    """在单一受控根内有限深度、有限文件数地发现 XML 候选。"""
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        if stats.file_cap_hit:
            return
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            continue
        if depth > MAX_WALK_DEPTH:
            stats.depth_cap_hit = True
            dirnames[:] = []
            continue
        dirnames.sort()
        filenames.sort()
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            if _is_link_like(child):
                stats.skipped_symlink += 1
            else:
                kept.append(name)
        dirnames[:] = kept
        stats.dirs_scanned += 1
        for name in filenames:
            if stats.files_scanned >= MAX_FILES_SCANNED:
                stats.file_cap_hit = True
                return
            stats.files_scanned += 1
            candidate = current / name
            if _is_link_like(candidate):
                stats.skipped_symlink += 1
                continue
            if not candidate.is_file():
                stats.skipped_nonregular += 1
                continue
            if candidate.suffix.lower() != ".xml":
                continue
            try:
                _assert_within(root, candidate)
            except ExtractionError:
                stats.skipped_nonregular += 1
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                stats.skipped_nonregular += 1
                continue
            if size > MAX_XML_BYTES:
                stats.oversize_candidates.append(candidate)
                stats.skipped_oversize += 1
            else:
                stats.xml_candidates.append(candidate)


def discover_source(source_root: Path) -> DiscoveryStats:
    """只从 screenshots/ 与 data/ 发现 XML，并精确识别 data/operations.log。"""
    stats = DiscoveryStats()
    for name in ("screenshots", "data"):
        root = source_root / name
        if not root.exists():
            continue
        if _is_link_like(root):
            stats.skipped_symlink += 1
            continue
        stats.roots_present += 1
        _walk_xml(root, stats)
    log_path = source_root / "data" / "operations.log"
    if log_path.exists() and not _is_link_like(log_path) and log_path.is_file():
        try:
            _assert_within(source_root / "data", log_path)
            stats.log_path = log_path
            stats.log_present = True
        except ExtractionError:
            stats.log_path = None
    elif log_path.exists():
        stats.skipped_symlink += 1
    return stats


def _decode_xml(raw: bytes) -> str:
    """按 BOM 或严格 UTF-8 解码；拒绝其他非法编码。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be")
    return raw.decode("utf-8")


def _parse_bounds(value: str) -> str | None:
    """解析并校验 Android bounds；非法或越界返回 None。"""
    match = BOUNDS_RE.match(value.strip())
    if not match:
        return None
    left, top, right, bottom = (int(part) for part in match.groups())
    if not (0 <= left <= right <= MAX_COORDINATE):
        return None
    if not (0 <= top <= bottom <= MAX_COORDINATE):
        return None
    return f"[{left},{top}][{right},{bottom}]"


def _normalize_bool(value: str) -> str | None:
    """把布尔属性规范为 true/false；其他值丢弃。"""
    normalized = value.strip().lower()
    return normalized if normalized in {"true", "false"} else None


def _scan_node_structure(text: str) -> tuple[int, int]:
    """在解析前粗略统计 node 数量与嵌套深度，避免超大树/深树进入解析器。"""
    count = 0
    depth = 0
    maximum = 0
    for match in NODE_TAG_RE.finditer(text):
        closing = match.group(1) == "/"
        self_closing = match.group(0).rstrip().endswith("/>")
        if closing:
            depth = max(0, depth - 1)
            continue
        count += 1
        if not self_closing:
            depth += 1
            maximum = max(maximum, depth)
    return count, maximum


def _representative_xml(fault: str) -> str:
    """生成只含固定合成结构的异常类别代表，不复制任何原始片段。"""
    safe_fault = fault if fault in XML_FAULTS else "malformed_xml"
    return (
        '<hierarchy derived_synthetic_representative="true" '
        f'fault="{safe_fault}" synthetic="true">'
        '<node class="android.view.View" bounds="[0,0][0,0]" text="TEXT"/>'
        "</hierarchy>"
    )


def _clean_node(
    source: ET.Element,
    aliases: AliasBook,
    faults: dict[str, int],
    counter: list[int],
    depth: int,
) -> ET.Element:
    """把单个源节点按属性白名单重构为合成节点。"""
    if depth > MAX_XML_DEPTH:
        raise _Anomaly("too_deep")
    counter[0] += 1
    if counter[0] > MAX_XML_NODES:
        raise _Anomaly("too_many_nodes")
    tag = source.tag if source.tag in SAFE_TAGS and source.tag != "hierarchy" else "node"
    if source.tag not in SAFE_TAGS:
        faults["unknown_tag"] += 1
    node = ET.Element(tag)
    class_name = source.get("class")
    if class_name in CLASS_WHITELIST:
        node.set("class", class_name)
    else:
        node.set("class", SYNTHETIC_CLASS)
        faults["unknown_class"] += 1
    for attr in BOOL_ATTRS:
        raw_value = source.get(attr)
        if raw_value is None:
            continue
        normalized = _normalize_bool(raw_value)
        if normalized is None:
            faults["invalid_boolean"] += 1
        else:
            node.set(attr, normalized)
    bounds = source.get("bounds")
    if bounds is not None:
        parsed = _parse_bounds(bounds)
        if parsed is None:
            faults["invalid_bounds"] += 1
        else:
            node.set("bounds", parsed)
    resource_id = source.get("resource-id")
    if resource_id:
        node.set("resource-id", aliases.alias(resource_id))
    if source.get("package"):
        node.set("package", SYNTHETIC_PACKAGE)
    text = source.get("text")
    if text and text.strip():
        node.set("text", SYNTHETIC_TEXT)
    content_desc = source.get("content-desc")
    if content_desc and content_desc.strip():
        node.set("content-desc", SYNTHETIC_DESC)
    for attr in source.attrib:
        if attr not in KNOWN_ATTRS:
            faults["unknown_attr"] += 1
    for child in source:
        node.append(_clean_node(child, aliases, faults, counter, depth + 1))
    return node


def _max_depth(element: ET.Element, depth: int = 0) -> int:
    """计算合成树的最大深度，用于manifest统计。"""
    best = depth
    for child in element:
        best = max(best, _max_depth(child, depth + 1))
    return best


def sanitize_xml(raw: bytes, aliases: AliasBook) -> SanitizeOutcome:
    """把原始 XML 重构为白名单合成树；异常输入返回纯合成代表。"""
    faults = {name: 0 for name in ATTRIBUTE_FAULTS}
    try:
        text = _decode_xml(raw)
    except UnicodeDecodeError:
        return SanitizeOutcome(_representative_xml("invalid_encoding"), 1, 1, faults, "invalid_encoding")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered or "<!element" in lowered or "<!attlist" in lowered:
        return SanitizeOutcome(_representative_xml("dtd_or_entity"), 1, 1, faults, "dtd_or_entity")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    node_count, node_depth = _scan_node_structure(text)
    if node_depth > MAX_XML_DEPTH:
        return SanitizeOutcome(_representative_xml("too_deep"), 1, 1, faults, "too_deep")
    if node_count > MAX_XML_NODES:
        return SanitizeOutcome(_representative_xml("too_many_nodes"), 1, 1, faults, "too_many_nodes")
    try:
        source_root = ET.fromstring(text)
    except ET.ParseError:
        return SanitizeOutcome(_representative_xml("malformed_xml"), 1, 1, faults, "malformed_xml")
    if source_root.tag != "hierarchy":
        return SanitizeOutcome(_representative_xml("malformed_xml"), 1, 1, faults, "malformed_xml")
    if len(source_root) == 0:
        return SanitizeOutcome("<hierarchy/>", 0, 0, faults, None)
    root = ET.Element("hierarchy")
    rotation = source_root.get("rotation")
    if rotation is not None and rotation.strip().isdigit() and int(rotation.strip()) <= SYNTHETIC_ROTATION_MAX:
        root.set("rotation", str(int(rotation.strip())))
    for attr in source_root.attrib:
        if attr != "rotation":
            faults["unknown_attr"] += 1
    counter = [0]
    try:
        for child in source_root:
            root.append(_clean_node(child, aliases, faults, counter, 1))
    except _Anomaly as exc:
        return SanitizeOutcome(_representative_xml(exc.fault), 1, 1, faults, exc.fault)
    return SanitizeOutcome(
        ET.tostring(root, encoding="unicode"),
        counter[0],
        _max_depth(root),
        faults,
        None,
    )


def _read_xml(path: Path, root: Path, budget: ReadBudget, size: int) -> tuple[bytes, ReadRecord]:
    """在路径安全检查与读取预算内读取一份 XML，并记录本地完整性信息。"""
    _assert_within(root, path)
    if _is_link_like(path):
        raise ExtractionError("symlink_rejected")
    if size > MAX_XML_BYTES:
        raise _Anomaly("oversize")
    if size > budget.remaining():
        budget.exhausted = True
        raise _BudgetExceeded
    with open(path, "rb") as handle:
        data = handle.read(size + 1)
    budget.consume(len(data))
    if len(data) > size:
        raise _Anomaly("oversize")
    if len(data) > MAX_XML_BYTES:
        raise _Anomaly("oversize")
    st = path.stat()
    return data, ReadRecord(
        relative=str(path.relative_to(root)).replace(os.sep, "/"),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _first_scalar(record: dict[str, object], keys: tuple[str, ...]) -> str | None:
    """从固定键集合中取第一个标量值；非标量一律忽略。"""
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int, float)):
            return str(value)
    return None


def _mapped(mapping: dict[str, str], value: str | None, default: str = "unknown") -> str:
    """把白名单值映射为固定类别；未知值返回固定 default。"""
    if value is None:
        return default
    return mapping.get(value.strip().lower(), default)


def _parse_timestamp(value: str | None) -> datetime | None:
    """解析 ISO 时间或数字秒/毫秒；无法识别返回 None，不猜日期。"""
    if value is None:
        return None
    text = value.strip()
    numeric = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if numeric:
        number = float(text)
        if number > 1e12:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _bounded_numbers(record: dict[str, object]) -> dict[str, int]:
    """只保留白名单数值键，并限制在非负有界范围；拒绝 NaN/Infinity。"""
    values: dict[str, int] = {}
    for key in BOUNDED_NUMBER_KEYS:
        raw = record.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        if isinstance(raw, float) and not math.isfinite(raw):
            continue
        number = int(raw)
        if 0 <= number <= MAX_BOUNDED_NUMBER:
            values[key] = number
    return values


def _to_event(record: dict[str, object]) -> TimelineEvent:
    """把一条 JSONL 记录压缩为固定类别事件，未知值统一 unknown。"""
    event = _mapped(EVENT_MAP, _first_scalar(record, EVENT_KEYS))
    reason = _mapped(REASON_MAP, _first_scalar(record, REASON_KEYS))
    status = _mapped(STATUS_MAP, _first_scalar(record, STATUS_KEYS))
    action = _mapped(ACTION_MAP, _first_scalar(record, ACTION_KEYS), default="unattributed")
    if action not in ACTION_IDS:
        action = "unattributed"
    ts = _parse_timestamp(_first_scalar(record, TIME_KEYS))
    failure = (
        event in FAILURE_EVENT_CATEGORIES
        or reason in FAILURE_REASONS
        or status in FAILURE_STATUSES
    )
    return TimelineEvent(event, reason, status, action, ts, _bounded_numbers(record), failure)


def _read_log_events(
    log_path: Path, budget: ReadBudget
) -> tuple[list[TimelineEvent], dict[str, object], ReadRecord]:
    """流式读取精确白名单日志，全程扣减预算并记录完整性语义。"""
    events: list[TimelineEvent] = []
    stats: dict[str, object] = {
        "lines": 0,
        "invalid_lines": 0,
        "truncated": False,
    }
    digest = hashlib.sha256()
    st = log_path.stat()
    with open(log_path, "rb") as handle:
        while True:
            if budget.remaining() <= 0:
                budget.exhausted = True
                stats["truncated"] = True
                break
            allowance = min(MAX_LOG_LINE_BYTES + 1, budget.remaining())
            line = handle.readline(allowance)
            if not line:
                break
            budget.consume(len(line))
            digest.update(line)
            stats["lines"] = int(stats["lines"]) + 1
            if int(stats["lines"]) > MAX_LOG_LINES:
                stats["truncated"] = True
                break
            if len(line) > MAX_LOG_LINE_BYTES:
                stats["invalid_lines"] = int(stats["invalid_lines"]) + 1
                while line and not line.endswith(b"\n"):
                    if budget.remaining() <= 0:
                        budget.exhausted = True
                        stats["truncated"] = True
                        break
                    chunk = handle.readline(min(MAX_LOG_LINE_BYTES, budget.remaining()))
                    if not chunk:
                        break
                    budget.consume(len(chunk))
                    digest.update(chunk)
                    line = chunk
                continue
            try:
                text = line.decode("utf-8").strip()
            except UnicodeDecodeError:
                stats["invalid_lines"] = int(stats["invalid_lines"]) + 1
                continue
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                stats["invalid_lines"] = int(stats["invalid_lines"]) + 1
                continue
            if not isinstance(record, dict):
                stats["invalid_lines"] = int(stats["invalid_lines"]) + 1
                continue
            events.append(_to_event(record))
    complete = not bool(stats["truncated"]) and not budget.exhausted
    record = ReadRecord(
        relative="data/operations.log",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        sha256=digest.hexdigest(),
        complete=complete,
    )
    return events, stats, record


def _relative_times(
    events: list[TimelineEvent],
) -> tuple[list[int | None], bool, bool]:
    """返回窗口内相对毫秒、缺时钟标志与乱序标志；不排序修复。"""
    base: datetime | None = None
    for event in events:
        if event.ts is not None:
            base = event.ts
            break
    clock_missing = base is None or any(event.ts is None for event in events)
    relatives: list[int | None] = []
    out_of_order = False
    previous: int | None = None
    for event in events:
        relative: int | None = None
        if base is not None and event.ts is not None:
            relative = round((event.ts - base).total_seconds() * 1000)
            if previous is not None and relative < previous:
                out_of_order = True
            previous = relative
        relatives.append(relative)
    return relatives, clock_missing, out_of_order


def _cluster_failures(events: list[TimelineEvent]) -> list[WindowCandidate]:
    """按行距聚类失败证据；大簇有界分片，保留尾部新类别。"""
    indices = [index for index, event in enumerate(events) if event.failure]
    if not indices:
        return []
    clusters: list[list[int]] = [[indices[0]]]
    for index in indices[1:]:
        if index - clusters[-1][-1] <= WINDOW_CLUSTER_GAP:
            clusters[-1].append(index)
        else:
            clusters.append([index])
    chunk_size = max(1, MAX_WINDOW_EVENTS - 2 * WINDOW_PAD)
    candidates: list[WindowCandidate] = []
    for cluster in clusters:
        chunks = [
            cluster[offset : offset + chunk_size]
            for offset in range(0, len(cluster), chunk_size)
        ]
        for chunk in chunks:
            start = max(0, chunk[0] - WINDOW_PAD)
            end = min(len(events), chunk[-1] + WINDOW_PAD + 1)
            trimmed = len(cluster) > chunk_size
            if end - start > MAX_WINDOW_EVENTS:
                end = min(len(events), start + MAX_WINDOW_EVENTS)
                trimmed = True
            window = events[start:end]
            relatives, clock_missing, out_of_order = _relative_times(window)
            signature = (
                tuple((item.event, item.reason, item.status, item.action) for item in window),
                clock_missing,
                out_of_order,
                tuple(relatives),
            )
            candidates.append(WindowCandidate(signature, window, start, trimmed))
    return candidates


def _select_windows(candidates: list[WindowCandidate]) -> list[WindowCandidate]:
    """优先保留能覆盖新失败类别的不同结构/时序窗口，再按签名去重补齐。"""
    selected: list[WindowCandidate] = []
    seen: set[tuple[object, ...]] = set()
    covered: set[str] = set()
    for candidate in candidates:
        categories = {event.event for event in candidate.events if event.event in FAILURE_EVENT_CATEGORIES}
        if categories - covered:
            selected.append(candidate)
            seen.add(candidate.signature)
            covered |= categories
        if len(selected) >= MAX_FAILURE_WINDOWS:
            break
    for candidate in candidates:
        if len(selected) >= MAX_FAILURE_WINDOWS:
            break
        if candidate.signature in seen:
            continue
        selected.append(candidate)
        seen.add(candidate.signature)
    return sorted(selected, key=lambda item: item.first_index)


def _window_payload(case_id: str, candidate: WindowCandidate, truncated: bool) -> dict[str, object]:
    """生成单个失败窗口的安全JSON，时间只保留相对毫秒。"""
    relatives, clock_missing, out_of_order = _relative_times(candidate.events)
    payload_events: list[dict[str, object]] = []
    for event, relative in zip(candidate.events, relatives):
        payload_events.append(
            {
                "case_seq": len(payload_events) + 1,
                "rel_ms": relative,
                "event": event.event,
                "reason": event.reason,
                "status": event.status,
                "action": event.action,
                "values": event.values,
            }
        )
    return {
        "schema": SCHEMA_WINDOW,
        "case_id": case_id,
        "kind": "failure_window",
        "expected": "needs_label",
        "clock_missing": clock_missing,
        "monotonic": not out_of_order,
        "out_of_order": out_of_order,
        "truncated": bool(truncated or candidate.trimmed),
        "events": payload_events,
    }


def _summarize_timeline(
    events: list[TimelineEvent],
    stats: dict[str, object],
    uncovered_reason: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """把事件压缩为有界窗口与固定类别覆盖统计。"""
    candidates = _select_windows(_cluster_failures(events))
    truncated = bool(stats["truncated"])
    windows = [
        _window_payload(f"FW-{index:04d}", candidate, truncated)
        for index, candidate in enumerate(candidates, start=1)
    ]
    counts = {category: 0 for category in FAILURE_CATEGORIES}
    unknown_event = 0
    unknown_reason = 0
    unknown_status = 0
    unattributed = 0
    nonfailure_events = 0
    for event in events:
        if event.event in counts:
            counts[event.event] += 1
        elif event.event == "unknown":
            unknown_event += 1
        else:
            nonfailure_events += 1
        if event.reason == "unknown":
            unknown_reason += 1
        if event.status == "unknown":
            unknown_status += 1
        if event.action == "unattributed":
            unattributed += 1
    covered_actions = sorted(
        {
            event["action"]
            for window in windows
            for event in window["events"]
            if event["action"] in ACTION_IDS
        }
    )
    missing_actions = [action for action in ACTION_IDS if action not in covered_actions]
    observed_failure_categories = {
        event.event for event in events if event.event in FAILURE_EVENT_CATEGORIES
    }
    window_failure_categories = {
        event["event"]
        for window in windows
        for event in window["events"]
        if event["event"] in FAILURE_EVENT_CATEGORIES
    }
    missing_window_categories = sorted(
        observed_failure_categories - window_failure_categories
    )
    if not windows and uncovered_reason == "none":
        uncovered_reason = "truncated_before_evidence" if truncated else "no_failure_evidence"
    clock_missing_windows = sum(1 for window in windows if window["clock_missing"])
    out_of_order_windows = sum(1 for window in windows if window["out_of_order"])
    window_truncated = any(window["truncated"] for window in windows)
    summary = {
        "events_total": len(events),
        "failure_windows": len(windows),
        "needs_label": len(windows),
        "unknown_event": unknown_event,
        "unknown_reason": unknown_reason,
        "unknown_status": unknown_status,
        "unattributed_action": unattributed,
        "nonfailure_events": nonfailure_events,
        "clock_missing_windows": clock_missing_windows,
        "out_of_order_windows": out_of_order_windows,
        "window_truncated": window_truncated,
        "covered_actions": covered_actions,
        "missing_actions": missing_actions,
        "failure_categories": counts,
        "missing_window_categories": missing_window_categories,
        "uncovered_reason": uncovered_reason,
    }
    return windows, summary


def _zero_faults() -> dict[str, int]:
    """返回固定故障类别到 0 的计数表。"""
    return {name: 0 for name in XML_FAULTS}


def _safe_receipt(
    batch_id: str,
    status: str,
    discovery: DiscoveryStats,
    xml_summary: dict[str, object],
    log_summary: dict[str, object],
    safety: dict[str, bool],
    budget: dict[str, object],
    error_reason: str | None = None,
) -> dict[str, object]:
    """构造只含固定字段、计数与类别的安全回执。"""
    receipt: dict[str, object] = {
        "schema": SCHEMA_RECEIPT,
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "batch_id": batch_id,
        "status": status,
        "discovery": {
            "roots": discovery.roots_present,
            "files_scanned": discovery.files_scanned,
            "dirs_scanned": discovery.dirs_scanned,
            "xml_candidates": len(discovery.xml_candidates),
            "oversize_candidates": len(discovery.oversize_candidates),
            "skipped_symlink": discovery.skipped_symlink,
            "skipped_nonregular": discovery.skipped_nonregular,
            "file_cap_hit": discovery.file_cap_hit,
            "depth_cap_hit": discovery.depth_cap_hit,
            "log_present": discovery.log_present,
        },
        "xml": xml_summary,
        "log": log_summary,
        "budget": budget,
        "safety": safety,
    }
    if error_reason is not None:
        receipt["error_reason"] = error_reason if error_reason in ERROR_REASONS else "internal_error"
    return receipt


def _error_receipt(reason: str) -> dict[str, object]:
    """构造固定错误回执，绝不包含路径、原始异常或自由文本。"""
    return {
        "schema": SCHEMA_RECEIPT,
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "batch_id": None,
        "status": "error",
        "error_reason": reason if reason in ERROR_REASONS else "internal_error",
    }


def _json_text(payload: object) -> str:
    """以稳定键序和 UTF-8 友好格式序列化。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, payload: object) -> None:
    """写入新的 JSON 产物；调用方保证目录是新建批次。"""
    path.write_text(_json_text(payload), encoding="utf-8", newline="\n")


def _hash_file_bounded(path: Path, budget: ReadBudget, expected_size: int) -> str | None:
    """在预算内流式计算完整文件哈希；预算不足或长度不符返回 None。"""
    if expected_size > budget.remaining():
        budget.exhausted = True
        return None
    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as handle:
            while True:
                allowance = min(64 * 1024, budget.remaining())
                if allowance <= 0:
                    budget.exhausted = True
                    return None
                chunk = handle.read(allowance)
                if not chunk:
                    break
                budget.consume(len(chunk))
                digest.update(chunk)
                total += len(chunk)
                if total > expected_size:
                    return None
    except OSError:
        return None
    if total != expected_size:
        return None
    return digest.hexdigest()


def _verify_source_unchanged(
    source_root: Path,
    xml_records: list[ReadRecord],
    log_record: ReadRecord | None,
    xml_budget: ReadBudget,
    log_budget: ReadBudget,
) -> tuple[bool, bool]:
    """有界校验源未变；返回 (是否未变, 日志是否仅前缀时间校验)。"""
    for record in xml_records:
        path = source_root / record.relative
        try:
            _assert_within(source_root, path)
            st = path.stat()
        except (ExtractionError, OSError):
            return False, False
        if st.st_size != record.size or st.st_mtime_ns != record.mtime_ns:
            return False, False
        if _hash_file_bounded(path, xml_budget, record.size) != record.sha256:
            return False, False
    if log_record is None:
        return True, False
    path = source_root / log_record.relative
    try:
        _assert_within(source_root / "data", path)
        st = path.stat()
    except (ExtractionError, OSError):
        return False, False
    if st.st_size != log_record.size or st.st_mtime_ns != log_record.mtime_ns:
        return False, False
    if not log_record.complete:
        return True, True
    if _hash_file_bounded(path, log_budget, log_record.size) != log_record.sha256:
        return False, False
    return True, False


def run_extraction(
    source_root: Path,
    output_root: Path,
    token_factory: Callable[[], str] | None = None,
) -> dict[str, object]:
    """执行一次受控只读抽取，返回安全回执并把脱敏产物写入新批次目录。"""
    raw_source = Path(os.path.abspath(source_root))
    raw_output = Path(os.path.abspath(output_root))
    _assert_no_reparse_ancestors(raw_source)
    _assert_no_reparse_ancestors(raw_output)
    source = Path(os.path.realpath(raw_source))
    output = Path(os.path.realpath(raw_output))
    if not source.is_dir() or _is_link_like(source):
        raise ExtractionError("source_missing")
    if _paths_overlap(source, output):
        raise ExtractionError("output_overlap")
    _assert_output_safe(output)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExtractionError("internal_error") from exc
    discovery = discover_source(source)
    aliases = AliasBook(token_factory)
    batch_id = _new_batch_id()
    batch_dir = output / batch_id
    try:
        batch_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise ExtractionError("output_exists") from exc
    (batch_dir / "xml" / "representatives").mkdir(parents=True, exist_ok=True)
    (batch_dir / "failure_windows").mkdir(parents=True, exist_ok=True)

    read_budget = ReadBudget(MAX_TOTAL_READ_BYTES)
    log_read_budget = ReadBudget(MAX_LOG_BYTES)
    verify_xml_budget = ReadBudget(MAX_TOTAL_READ_BYTES)
    verify_log_budget = ReadBudget(MAX_LOG_BYTES)
    xml_read_records: list[ReadRecord] = []
    sample_records: list[tuple[str, ReadRecord]] = []
    samples: list[dict[str, object]] = []
    representations: dict[str, str] = {}
    seen_signatures: set[str] = set()
    attr_totals = {name: 0 for name in ATTRIBUTE_FAULTS}
    fault_totals = _zero_faults()
    duplicates = 0
    cap_hit = False
    nodes_total = 0
    depth_max = 0

    for fault in XML_FAULTS:
        if fault == "oversize" and discovery.oversize_candidates:
            representations[fault] = _representative_xml(fault)
            fault_totals[fault] = len(discovery.oversize_candidates)

    candidates = sorted(discovery.xml_candidates, key=lambda item: str(item).lower())
    for path in candidates:
        if len(samples) >= MAX_XML_SAMPLES:
            cap_hit = True
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        try:
            raw, record = _read_xml(path, source, read_budget, size)
        except _BudgetExceeded:
            cap_hit = True
            break
        except _Anomaly as exc:
            representations.setdefault(exc.fault, _representative_xml(exc.fault))
            fault_totals[exc.fault] += 1
            continue
        except ExtractionError:
            continue
        xml_read_records.append(record)
        outcome = sanitize_xml(raw, aliases)
        for name, count in outcome.faults.items():
            attr_totals[name] += count
        if outcome.anomaly is not None:
            representations.setdefault(outcome.anomaly, outcome.xml_text)
            fault_totals[outcome.anomaly] += 1
            continue
        signature = hashlib.sha256(outcome.xml_text.encode("utf-8")).hexdigest()
        if signature in seen_signatures:
            duplicates += 1
            continue
        seen_signatures.add(signature)
        case_id = f"XML-{len(samples) + 1:04d}"
        (batch_dir / "xml" / f"{case_id}.xml").write_text(
            outcome.xml_text + "\n", encoding="utf-8", newline="\n"
        )
        samples.append(
            {
                "case_id": case_id,
                "kind": "sanitized_xml",
                "expected": "needs_label",
                "node_count": outcome.node_count,
                "max_depth": outcome.max_depth,
                "attribute_faults": dict(outcome.faults),
            }
        )
        sample_records.append((case_id, record))
        nodes_total += outcome.node_count
        depth_max = max(depth_max, outcome.max_depth)

    representative_entries: list[dict[str, object]] = []
    for index, fault in enumerate(XML_FAULTS, start=1):
        if fault not in representations:
            continue
        case_id = f"XREP-{index:04d}"
        (batch_dir / "xml" / "representatives" / f"{fault}.xml").write_text(
            representations[fault] + "\n", encoding="utf-8", newline="\n"
        )
        representative_entries.append(
            {
                "case_id": case_id,
                "kind": "derived_synthetic_representative",
                "fault": fault,
                "expected": "needs_label",
            }
        )
    samples.extend(representative_entries)

    log_summary: dict[str, object] = {
        "present": discovery.log_present,
        "lines": 0,
        "invalid_lines": 0,
        "truncated": False,
        "events_total": 0,
        "failure_windows": 0,
        "needs_label": 0,
        "unknown_event": 0,
        "unknown_reason": 0,
        "unknown_status": 0,
        "unattributed_action": 0,
        "nonfailure_events": 0,
        "clock_missing_windows": 0,
        "out_of_order_windows": 0,
        "window_truncated": False,
        "covered_actions": [],
        "missing_actions": list(ACTION_IDS),
        "failure_categories": {category: 0 for category in FAILURE_CATEGORIES},
        "missing_window_categories": [],
        "uncovered_reason": "log_missing",
    }
    windows: list[dict[str, object]] = []
    log_record: ReadRecord | None = None
    if discovery.log_path is not None:
        try:
            events, log_stats, log_record = _read_log_events(discovery.log_path, log_read_budget)
        except OSError:
            log_summary["present"] = True
            log_summary["uncovered_reason"] = "log_read_error"
        else:
            windows, summary = _summarize_timeline(events, log_stats, "none")
            log_summary.update(summary)
            log_summary.update(
                {
                    "present": True,
                    "lines": log_stats["lines"],
                    "invalid_lines": log_stats["invalid_lines"],
                    "truncated": log_stats["truncated"],
                }
            )
            for window in windows:
                _write_json(batch_dir / "failure_windows" / f"{window['case_id']}.json", window)

    source_unchanged, log_prefix_only = _verify_source_unchanged(
        source, xml_read_records, log_record, verify_xml_budget, verify_log_budget
    )
    ignored, tracked = _git_ignore_status(batch_dir)
    truncated = (
        cap_hit
        or discovery.file_cap_hit
        or discovery.depth_cap_hit
        or bool(log_summary["truncated"])
        or bool(log_summary["window_truncated"])
        or log_summary["uncovered_reason"] == "log_read_error"
        or read_budget.exhausted
        or log_read_budget.exhausted
    )
    has_output = bool(samples) or bool(windows)
    if not source_unchanged or not ignored or tracked:
        status = "error"
        error_reason = "source_changed" if not source_unchanged else "internal_error"
    elif not has_output:
        status = "no_candidates"
        error_reason = None
    elif truncated:
        status = "partial"
        error_reason = None
    else:
        status = "ok"
        error_reason = None
    xml_uncovered = "none" if (samples or representations) else "no_xml_candidates"
    xml_summary = {
        "selected": len(samples) - len(representative_entries),
        "representatives": len(representative_entries),
        "duplicates_dropped": duplicates,
        "nodes_total": nodes_total,
        "depth_max": depth_max,
        "attribute_faults": attr_totals,
        "faults": fault_totals,
        "truncated": bool(cap_hit or discovery.file_cap_hit or discovery.depth_cap_hit or read_budget.exhausted),
        "uncovered_reason": xml_uncovered,
    }
    safety = {
        "source_unchanged": bool(source_unchanged),
        "output_new_dir": True,
        "output_git_ignored": bool(ignored),
        "output_not_tracked": not bool(tracked),
        "no_overlap": True,
        "no_symlink_read": True,
        "no_dtd_entity": True,
        "no_absolute_dates": True,
        "stdout_sanitized": True,
        "no_source_paths_in_receipt": True,
    }
    budget_summary = {
        "xml_read_limit": read_budget.limit,
        "xml_read_used": read_budget.used,
        "xml_read_exhausted": bool(read_budget.exhausted),
        "log_read_limit": log_read_budget.limit,
        "log_read_used": log_read_budget.used,
        "log_read_exhausted": bool(log_read_budget.exhausted),
        "verify_xml_limit": verify_xml_budget.limit,
        "verify_xml_used": verify_xml_budget.used,
        "verify_xml_exhausted": bool(verify_xml_budget.exhausted),
        "verify_log_limit": verify_log_budget.limit,
        "verify_log_used": verify_log_budget.used,
        "verify_log_exhausted": bool(verify_log_budget.exhausted),
        "log_integrity_scope": "prefix" if log_prefix_only else "full",
    }
    receipt = _safe_receipt(
        batch_id,
        status,
        discovery,
        xml_summary,
        log_summary,
        safety,
        budget_summary,
        error_reason,
    )
    manifest = {
        "schema": SCHEMA_MANIFEST,
        "batch_id": batch_id,
        "samples": samples,
    }
    provenance = {
        "schema": SCHEMA_PROVENANCE,
        "not_for_model": True,
        "batch_id": batch_id,
        "source_root": str(source),
        "files": [
            {
                "case_id": case_id,
                "relative": record.relative,
                "size": record.size,
                "sha256": record.sha256,
            }
            for case_id, record in sample_records
        ],
        "log": (
            {
                "present": True,
                "relative": "data/operations.log",
                "size": log_record.size if log_record is not None else None,
                "sha256": log_record.sha256 if log_record is not None else None,
                "complete": bool(log_record.complete) if log_record is not None else False,
                "sha256_scope": "full"
                if log_record is not None and log_record.complete
                else "read_prefix",
            }
            if discovery.log_present
            else {"present": False}
        ),
    }
    _write_json(batch_dir / "manifest.json", manifest)
    _write_json(batch_dir / "safe_receipt.json", receipt)
    _write_json(batch_dir / "provenance.local.json", provenance)
    return receipt


class ExtractErrorSize(Exception):
    """校验读取超出单文件或总预算。"""


def _read_text_bounded(path: Path, root: Path, budget: ReadBudget, max_bytes: int) -> str:
    """在路径安全与读取预算内读取一个明确批次文件。"""
    _assert_within(root, path)
    if _is_link_like(path):
        raise ExtractionError("symlink_rejected")
    st = path.stat()
    if st.st_size > max_bytes:
        raise ExtractErrorSize()
    if st.st_size > budget.remaining():
        budget.exhausted = True
        raise ExtractErrorSize()
    with open(path, "rb") as handle:
        data = handle.read(st.st_size + 1)
    budget.consume(len(data))
    if len(data) != st.st_size:
        raise ExtractErrorSize()
    return data.decode("utf-8")


def _iter_keys(payload: object) -> Iterator[str]:
    """递归遍历 JSON 对象的所有键名。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from _iter_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_keys(item)


ALIAS_RE = re.compile(r"^res_[0-9a-f]{8}$")
WINDOW_EVENT_VALUES = frozenset(FAILURE_CATEGORIES) | {
    "transport",
    "scheduling",
    "filter_applied",
    "unknown",
}
WINDOW_REASON_VALUES = frozenset(REASON_MAP.values()) | {"unknown"}
WINDOW_STATUS_VALUES = frozenset(STATUS_MAP.values()) | {"unknown"}
WINDOW_ACTION_VALUES = frozenset(ACTION_IDS) | {"unattributed"}
REPRESENTATIVE_ATTRS = frozenset(
    {"derived_synthetic_representative", "fault", "synthetic"}
)


def _xml_shape(root: ET.Element) -> tuple[int, int]:
    """统计合成树中除根以外的节点数与最大深度。"""
    count = 0
    depth = 0
    stack = [(child, 1) for child in root]
    while stack:
        node, level = stack.pop()
        count += 1
        depth = max(depth, level)
        stack.extend((child, level + 1) for child in node)
    return count, depth


def _blank_character_data(element: ET.Element) -> bool:
    """只允许元素正文与 tail 为空白，防止隐藏原文。"""
    return not (element.text or "").strip() and not (element.tail or "").strip()


def _check_xml_file(text: str, kind: str, fault: str | None) -> tuple[bool, int, int]:
    """按固定属性/文本白名单与节点关系检查单份派生 XML。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False, 0, 0
    if root.tag != "hierarchy" or not _blank_character_data(root):
        return False, 0, 0
    root_allowed = {"rotation"} | (set(REPRESENTATIVE_ATTRS) if kind == "representative" else set())
    if set(root.attrib) - root_allowed:
        return False, 0, 0
    if root.get("rotation") not in (None, "0", "1", "2", "3"):
        return False, 0, 0
    if kind == "representative":
        if root.get("derived_synthetic_representative") != "true":
            return False, 0, 0
        if root.get("synthetic") != "true" or root.get("fault") != fault:
            return False, 0, 0
    elif REPRESENTATIVE_ATTRS & set(root.attrib):
        return False, 0, 0
    count = 0
    for node in root.iter():
        if node is root:
            continue
        if node.tag != "node" or not _blank_character_data(node):
            return False, 0, 0
        count += 1
        if set(node.attrib) - KNOWN_ATTRS:
            return False, 0, 0
        if node.get("class") not in CLASS_WHITELIST:
            return False, 0, 0
        for attr in BOOL_ATTRS:
            if attr in node.attrib and node.attrib[attr] not in {"true", "false"}:
                return False, 0, 0
        if "bounds" in node.attrib and _parse_bounds(node.attrib["bounds"]) is None:
            return False, 0, 0
        if "package" in node.attrib and node.attrib["package"] != SYNTHETIC_PACKAGE:
            return False, 0, 0
        if "text" in node.attrib and node.attrib["text"] != SYNTHETIC_TEXT:
            return False, 0, 0
        if "content-desc" in node.attrib and node.attrib["content-desc"] != SYNTHETIC_DESC:
            return False, 0, 0
        if "resource-id" in node.attrib and not ALIAS_RE.match(node.attrib["resource-id"]):
            return False, 0, 0
    count, depth = _xml_shape(root)
    if count > MAX_XML_NODES or depth > MAX_XML_DEPTH:
        return False, count, depth
    if kind == "representative" and count != 1:
        return False, count, depth
    return True, count, depth


def _check_window_payload(payload: object) -> bool:
    """检查失败窗口的固定字段、值白名单与相对时间标志一致性。"""
    if not isinstance(payload, dict):
        return False
    if set(payload) != {
        "schema",
        "case_id",
        "kind",
        "expected",
        "clock_missing",
        "monotonic",
        "out_of_order",
        "truncated",
        "events",
    }:
        return False
    if payload.get("schema") != SCHEMA_WINDOW:
        return False
    if not re.fullmatch(r"FW-\d{4}", str(payload.get("case_id"))):
        return False
    if payload.get("expected") != "needs_label" or payload.get("kind") != "failure_window":
        return False
    if not all(
        isinstance(payload.get(key), bool)
        for key in ("clock_missing", "monotonic", "out_of_order", "truncated")
    ):
        return False
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return False
    relatives: list[int | None] = []
    for expected_seq, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return False
        if set(event) != {"case_seq", "rel_ms", "event", "reason", "status", "action", "values"}:
            return False
        if event.get("case_seq") != expected_seq:
            return False
        if isinstance(event.get("case_seq"), bool) or not isinstance(event.get("case_seq"), int):
            return False
        if event.get("event") not in WINDOW_EVENT_VALUES:
            return False
        if event.get("reason") not in WINDOW_REASON_VALUES:
            return False
        if event.get("status") not in WINDOW_STATUS_VALUES:
            return False
        if event.get("action") not in WINDOW_ACTION_VALUES:
            return False
        values = event.get("values")
        if not isinstance(values, dict) or set(values) - set(BOUNDED_NUMBER_KEYS):
            return False
        for value in values.values():
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            if not 0 <= value <= MAX_BOUNDED_NUMBER:
                return False
        relative = event.get("rel_ms")
        if relative is not None and (isinstance(relative, bool) or not isinstance(relative, int)):
            return False
        relatives.append(relative)
    computed_missing = any(value is None for value in relatives)
    previous: int | None = None
    computed_out_of_order = False
    for value in relatives:
        if value is None:
            continue
        if previous is not None and value < previous:
            computed_out_of_order = True
        previous = value
    if payload.get("clock_missing") != computed_missing:
        return False
    if payload.get("out_of_order") != computed_out_of_order:
        return False
    if payload.get("monotonic") != (not computed_out_of_order):
        return False
    if not isinstance(payload.get("truncated"), bool):
        return False
    return True


def _is_nonneg_int(value: object) -> bool:
    """判断非负整数（排除布尔）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_str_list(value: object, allowed: frozenset[str] | tuple[str, ...]) -> bool:
    """判断字符串列表且元素都在固定集合内。"""
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item in allowed for item in value)
        and len(set(value)) == len(value)
    )


def _check_receipt_structure(receipt: object) -> bool:
    """校验安全回执的固定字段、类型与值域；拒绝任意自由字段。"""
    if not isinstance(receipt, dict):
        return False
    allowed = {
        "schema",
        "tool_name",
        "tool_version",
        "batch_id",
        "status",
        "discovery",
        "xml",
        "log",
        "budget",
        "safety",
        "error_reason",
    }
    if set(receipt) not in (allowed, allowed - {"error_reason"}):
        return False
    if (
        receipt.get("schema") != SCHEMA_RECEIPT
        or receipt.get("tool_name") != TOOL_NAME
        or receipt.get("tool_version") != TOOL_VERSION
    ):
        return False
    if not re.fullmatch(r"b-[0-9a-f]{12}", str(receipt.get("batch_id"))):
        return False
    if receipt.get("status") not in {"ok", "partial", "no_candidates", "error"}:
        return False
    if "error_reason" in receipt and receipt["error_reason"] not in ERROR_REASONS:
        return False
    discovery = receipt.get("discovery")
    discovery_keys = {
        "roots",
        "files_scanned",
        "dirs_scanned",
        "xml_candidates",
        "oversize_candidates",
        "skipped_symlink",
        "skipped_nonregular",
        "file_cap_hit",
        "depth_cap_hit",
        "log_present",
    }
    if not isinstance(discovery, dict) or set(discovery) != discovery_keys:
        return False
    if not all(
        _is_nonneg_int(discovery.get(key))
        for key in discovery_keys - {"file_cap_hit", "depth_cap_hit", "log_present"}
    ):
        return False
    if not all(
        isinstance(discovery.get(key), bool)
        for key in ("file_cap_hit", "depth_cap_hit", "log_present")
    ):
        return False
    xml_summary = receipt.get("xml")
    xml_keys = {
        "selected",
        "representatives",
        "duplicates_dropped",
        "nodes_total",
        "depth_max",
        "attribute_faults",
        "faults",
        "truncated",
        "uncovered_reason",
    }
    if not isinstance(xml_summary, dict) or set(xml_summary) != xml_keys:
        return False
    if not all(
        _is_nonneg_int(xml_summary.get(key))
        for key in ("selected", "representatives", "duplicates_dropped", "nodes_total", "depth_max")
    ):
        return False
    if not isinstance(xml_summary.get("truncated"), bool):
        return False
    if xml_summary.get("uncovered_reason") not in UNCOVERED_REASONS:
        return False
    for key, allowed_names in (
        ("attribute_faults", ATTRIBUTE_FAULTS),
        ("faults", XML_FAULTS),
    ):
        faults = xml_summary.get(key)
        if not isinstance(faults, dict) or set(faults) != set(allowed_names):
            return False
        if not all(_is_nonneg_int(value) for value in faults.values()):
            return False
    log_summary = receipt.get("log")
    log_keys = {
        "present",
        "lines",
        "invalid_lines",
        "truncated",
        "events_total",
        "failure_windows",
        "needs_label",
        "unknown_event",
        "unknown_reason",
        "unknown_status",
        "unattributed_action",
        "nonfailure_events",
        "clock_missing_windows",
        "out_of_order_windows",
        "window_truncated",
        "covered_actions",
        "missing_actions",
        "failure_categories",
        "missing_window_categories",
        "uncovered_reason",
    }
    if not isinstance(log_summary, dict) or set(log_summary) != log_keys:
        return False
    if not all(
        _is_nonneg_int(log_summary.get(key))
        for key in (
            "lines",
            "invalid_lines",
            "events_total",
            "failure_windows",
            "needs_label",
            "unknown_event",
            "unknown_reason",
            "unknown_status",
            "unattributed_action",
            "nonfailure_events",
            "clock_missing_windows",
            "out_of_order_windows",
        )
    ):
        return False
    if not all(
        isinstance(log_summary.get(key), bool)
        for key in ("present", "truncated", "window_truncated")
    ):
        return False
    if not _is_str_list(log_summary.get("covered_actions"), ACTION_IDS):
        return False
    if not _is_str_list(log_summary.get("missing_actions"), ACTION_IDS):
        return False
    if not _is_str_list(log_summary.get("missing_window_categories"), FAILURE_CATEGORIES):
        return False
    if log_summary.get("uncovered_reason") not in UNCOVERED_REASONS:
        return False
    categories = log_summary.get("failure_categories")
    if not isinstance(categories, dict) or set(categories) != set(FAILURE_CATEGORIES):
        return False
    if not all(_is_nonneg_int(value) for value in categories.values()):
        return False
    budget = receipt.get("budget")
    budget_keys = {
        "xml_read_limit",
        "xml_read_used",
        "xml_read_exhausted",
        "log_read_limit",
        "log_read_used",
        "log_read_exhausted",
        "verify_xml_limit",
        "verify_xml_used",
        "verify_xml_exhausted",
        "verify_log_limit",
        "verify_log_used",
        "verify_log_exhausted",
        "log_integrity_scope",
    }
    if not isinstance(budget, dict) or set(budget) != budget_keys:
        return False
    if not all(
        _is_nonneg_int(budget.get(key))
        for key in (
            "xml_read_limit",
            "xml_read_used",
            "log_read_limit",
            "log_read_used",
            "verify_xml_limit",
            "verify_xml_used",
            "verify_log_limit",
            "verify_log_used",
        )
    ):
        return False
    if not all(
        isinstance(budget.get(key), bool)
        for key in (
            "xml_read_exhausted",
            "log_read_exhausted",
            "verify_xml_exhausted",
            "verify_log_exhausted",
        )
    ):
        return False
    if budget.get("log_integrity_scope") not in {"full", "prefix"}:
        return False
    safety = receipt.get("safety")
    safety_keys = {
        "source_unchanged",
        "output_new_dir",
        "output_git_ignored",
        "output_not_tracked",
        "no_overlap",
        "no_symlink_read",
        "no_dtd_entity",
        "no_absolute_dates",
        "stdout_sanitized",
        "no_source_paths_in_receipt",
    }
    if not isinstance(safety, dict) or set(safety) != safety_keys:
        return False
    if not all(isinstance(value, bool) for value in safety.values()):
        return False
    return True


def _check_manifest_structure(manifest: object) -> bool:
    """校验 manifest 的固定字段、case 类型与值域，拒绝自由字段。"""
    if not isinstance(manifest, dict):
        return False
    if set(manifest) != {"schema", "batch_id", "samples"}:
        return False
    if manifest.get("schema") != SCHEMA_MANIFEST:
        return False
    if not re.fullmatch(r"b-[0-9a-f]{12}", str(manifest.get("batch_id"))):
        return False
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        return False
    for sample in samples:
        if not isinstance(sample, dict):
            return False
        kind = sample.get("kind")
        if kind == "sanitized_xml":
            if set(sample) != {
                "case_id",
                "kind",
                "expected",
                "node_count",
                "max_depth",
                "attribute_faults",
            }:
                return False
            if not re.fullmatch(r"XML-\d{4}", str(sample.get("case_id"))):
                return False
            if (
                sample.get("expected") != "needs_label"
                or not _is_nonneg_int(sample.get("node_count"))
                or not _is_nonneg_int(sample.get("max_depth"))
            ):
                return False
            faults = sample.get("attribute_faults")
            if not isinstance(faults, dict) or set(faults) != set(ATTRIBUTE_FAULTS):
                return False
            if not all(_is_nonneg_int(value) for value in faults.values()):
                return False
        elif kind == "derived_synthetic_representative":
            if set(sample) != {"case_id", "kind", "fault", "expected"}:
                return False
            if not re.fullmatch(r"XREP-\d{4}", str(sample.get("case_id"))):
                return False
            if sample.get("expected") != "needs_label" or sample.get("fault") not in XML_FAULTS:
                return False
        else:
            return False
    return True


def validate_batch(batch_dir: Path) -> dict[str, object]:
    """本机校验器：核对字段/值白名单、节点关系、计数、安全条件与 Git 边界。"""
    directory = Path(os.path.abspath(batch_dir))
    checks: dict[str, bool] = {
        "batch_path_safe": False,
        "receipt_schema_ok": False,
        "manifest_schema_ok": False,
        "batch_id_consistent": False,
        "manifest_entries_ok": False,
        "case_files_exact": False,
        "xml_safety_ok": False,
        "xml_manifest_shape_ok": False,
        "windows_schema_ok": False,
        "windows_expected_needs_label": False,
        "window_files_match": False,
        "counts_consistent": False,
        "status_consistent": False,
        "safety_all_true": False,
        "budget_consistent": False,
        "output_git_ignored": False,
        "output_not_tracked": False,
        "provenance_present": False,
        "no_forbidden_keys": False,
        "no_absolute_dates": False,
    }
    error_reason = "internal_error"
    try:
        _assert_no_reparse_ancestors(directory)
        if not directory.is_dir() or _is_link_like(directory):
            raise ExtractionError("symlink_rejected")
        checks["batch_path_safe"] = True
        read_budget = ReadBudget(MAX_TOTAL_READ_BYTES)
        receipt = json.loads(
            _read_text_bounded(
                directory / "safe_receipt.json", directory, read_budget, MAX_JSON_BYTES
            )
        )
        manifest = json.loads(
            _read_text_bounded(
                directory / "manifest.json", directory, read_budget, MAX_JSON_BYTES
            )
        )
        checks["receipt_schema_ok"] = _check_receipt_structure(receipt)
        checks["manifest_schema_ok"] = _check_manifest_structure(manifest)
        provenance_path = directory / "provenance.local.json"
        checks["provenance_present"] = (
            provenance_path.is_file() and not _is_link_like(provenance_path)
        )
        if not isinstance(receipt, dict) or not isinstance(manifest, dict):
            raise ValueError
        log_summary = receipt.get("log")
        xml_summary = receipt.get("xml")
        discovery = receipt.get("discovery")
        budget = receipt.get("budget")
        safety = receipt.get("safety")
        if not all(
            isinstance(part, dict)
            for part in (log_summary, xml_summary, discovery, budget, safety)
        ):
            raise ValueError
        batch_id = receipt.get("batch_id") if isinstance(receipt, dict) else None
        checks["batch_id_consistent"] = (
            isinstance(batch_id, str)
            and batch_id == manifest.get("batch_id")
            and batch_id == directory.name
        )
        samples = manifest.get("samples", []) if isinstance(manifest, dict) else []
        sanitized: list[dict[str, object]] = []
        representatives: list[dict[str, object]] = []
        manifest_ok = isinstance(samples, list)
        if manifest_ok:
            for sample in samples:
                if not isinstance(sample, dict):
                    manifest_ok = False
                    break
                kind = sample.get("kind")
                case_id = sample.get("case_id")
                if sample.get("expected") != "needs_label" or not isinstance(case_id, str):
                    manifest_ok = False
                    break
                if kind == "sanitized_xml":
                    if not re.fullmatch(r"XML-\d{4}", case_id):
                        manifest_ok = False
                        break
                    if (
                        not isinstance(sample.get("node_count"), int)
                        or not isinstance(sample.get("max_depth"), int)
                        or sample["node_count"] < 0
                        or sample["max_depth"] < 0
                        or not isinstance(sample.get("attribute_faults"), dict)
                        or set(sample["attribute_faults"]) - set(ATTRIBUTE_FAULTS)
                    ):
                        manifest_ok = False
                        break
                    sanitized.append(sample)
                elif kind == "derived_synthetic_representative":
                    if not re.fullmatch(r"XREP-\d{4}", case_id) or sample.get("fault") not in XML_FAULTS:
                        manifest_ok = False
                        break
                    representatives.append(sample)
                else:
                    manifest_ok = False
                    break
        checks["manifest_entries_ok"] = manifest_ok
        xml_dir = directory / "xml"
        xml_files = sorted(xml_dir.glob("*.xml"))
        rep_files = sorted((xml_dir / "representatives").glob("*.xml"))
        if len(xml_files) + len(rep_files) > MAX_VALIDATE_FILES:
            raise ExtractErrorSize()
        expected_xml = {xml_dir / f"{sample['case_id']}.xml" for sample in sanitized}
        expected_rep = {
            xml_dir / "representatives" / f"{sample['fault']}.xml"
            for sample in representatives
        }
        checks["case_files_exact"] = (
            set(xml_files) == expected_xml and set(rep_files) == expected_rep
        )
        xml_safe = True
        shape_ok = True
        shape_by_name: dict[str, tuple[int, int]] = {}
        for path in xml_files:
            text = _read_text_bounded(path, directory, read_budget, MAX_XML_BYTES)
            ok, count, depth = _check_xml_file(text, "sanitized", None)
            xml_safe = xml_safe and ok
            shape_by_name[path.stem] = (count, depth)
        for path in rep_files:
            text = _read_text_bounded(path, directory, read_budget, MAX_XML_BYTES)
            ok, count, depth = _check_xml_file(text, "representative", path.stem)
            xml_safe = xml_safe and ok
            shape_by_name[path.stem] = (count, depth)
        checks["xml_safety_ok"] = xml_safe
        for sample in sanitized:
            if shape_by_name.get(sample["case_id"]) != (sample["node_count"], sample["max_depth"]):
                shape_ok = False
                break
        checks["xml_manifest_shape_ok"] = shape_ok
        window_files = sorted((directory / "failure_windows").glob("*.json"))
        if len(window_files) > MAX_VALIDATE_FILES:
            raise ExtractErrorSize()
        payloads: list[dict[str, object]] = []
        windows_ok = True
        for path in window_files:
            try:
                payload = json.loads(
                    _read_text_bounded(path, directory, read_budget, MAX_JSON_BYTES)
                )
            except json.JSONDecodeError:
                windows_ok = False
                break
            if not _check_window_payload(payload):
                windows_ok = False
                break
            payloads.append(payload)
        checks["windows_schema_ok"] = windows_ok
        checks["windows_expected_needs_label"] = windows_ok and all(
            payload.get("expected") == "needs_label" for payload in payloads
        )
        checks["window_files_match"] = {
            f"{payload.get('case_id')}.json" for payload in payloads
        } == {path.name for path in window_files}
        log_summary = receipt.get("log", {}) if isinstance(receipt, dict) else {}
        xml_summary = receipt.get("xml", {}) if isinstance(receipt, dict) else {}
        discovery = receipt.get("discovery", {}) if isinstance(receipt, dict) else {}
        counts_ok = (
            isinstance(xml_summary, dict)
            and isinstance(log_summary, dict)
            and isinstance(discovery, dict)
            and xml_summary.get("selected") == len(sanitized)
            and xml_summary.get("representatives") == len(representatives)
            and log_summary.get("failure_windows") == len(payloads)
            and log_summary.get("needs_label") == len(payloads)
            and len(samples) == len(sanitized) + len(representatives)
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (
                    discovery.get("files_scanned"),
                    discovery.get("dirs_scanned"),
                    discovery.get("xml_candidates"),
                    discovery.get("oversize_candidates"),
                    discovery.get("skipped_symlink"),
                    discovery.get("skipped_nonregular"),
                )
            )
        )
        if counts_ok and payloads:
            if log_summary.get("truncated"):
                counts_ok = all(payload.get("truncated") for payload in payloads)
            counts_ok = counts_ok and log_summary.get("window_truncated") == any(
                payload.get("truncated") for payload in payloads
            )
        checks["counts_consistent"] = bool(counts_ok)
        safety = receipt.get("safety", {}) if isinstance(receipt, dict) else {}
        checks["safety_all_true"] = (
            isinstance(safety, dict)
            and set(safety)
            == {
                "source_unchanged",
                "output_new_dir",
                "output_git_ignored",
                "output_not_tracked",
                "no_overlap",
                "no_symlink_read",
                "no_dtd_entity",
                "no_absolute_dates",
                "stdout_sanitized",
                "no_source_paths_in_receipt",
            }
            and all(value is True for value in safety.values())
        )
        status = receipt.get("status") if isinstance(receipt, dict) else None
        budget = receipt.get("budget", {}) if isinstance(receipt, dict) else {}
        budget_keys = (
            "xml_read_limit",
            "xml_read_used",
            "xml_read_exhausted",
            "log_read_limit",
            "log_read_used",
            "log_read_exhausted",
            "verify_xml_limit",
            "verify_xml_used",
            "verify_xml_exhausted",
            "verify_log_limit",
            "verify_log_used",
            "verify_log_exhausted",
        )
        checks["budget_consistent"] = (
            isinstance(budget, dict)
            and all(key in budget for key in budget_keys)
            and budget.get("log_integrity_scope") in {"full", "prefix"}
            and all(
                isinstance(budget.get(key), int)
                and not isinstance(budget.get(key), bool)
                and budget.get(key) >= 0
                for key in (
                    "xml_read_limit",
                    "xml_read_used",
                    "log_read_limit",
                    "log_read_used",
                    "verify_xml_limit",
                    "verify_xml_used",
                    "verify_log_limit",
                    "verify_log_used",
                )
            )
            and all(
                budget[f"{prefix}_read_used"] <= budget[f"{prefix}_read_limit"]
                for prefix in ("xml", "log")
            )
            and budget["verify_xml_used"] <= budget["verify_xml_limit"]
            and budget["verify_log_used"] <= budget["verify_log_limit"]
            and all(
                isinstance(budget.get(key), bool)
                for key in (
                    "xml_read_exhausted",
                    "log_read_exhausted",
                    "verify_xml_exhausted",
                    "verify_log_exhausted",
                )
            )
        )
        status_ok = status in {"ok", "partial", "no_candidates", "error"}
        if status == "error":
            status_ok = status_ok and receipt.get("error_reason") in ERROR_REASONS
        else:
            status_ok = status_ok and "error_reason" not in receipt
        if status == "ok":
            status_ok = (
                status_ok
                and xml_summary.get("truncated") is False
                and log_summary.get("truncated") is False
                and log_summary.get("window_truncated") is False
                and budget.get("xml_read_exhausted") is False
                and budget.get("log_read_exhausted") is False
            )
        if status == "no_candidates":
            status_ok = status_ok and not samples and not payloads
        checks["status_consistent"] = bool(status_ok)
        ignored, tracked = _git_ignore_status(directory)
        checks["output_git_ignored"] = ignored
        checks["output_not_tracked"] = not tracked
        checks["no_forbidden_keys"] = not any(
            key.lower() in FORBIDDEN_OUTPUT_KEYS
            for key in _iter_keys([receipt, manifest, payloads])
        )
        combined = _json_text(receipt) + _json_text(manifest) + _json_text(payloads)
        checks["no_absolute_dates"] = DATE_RE.search(combined) is None
        error_reason = "none"
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        ExtractionError,
        ExtractErrorSize,
        ET.ParseError,
    ):
        error_reason = "internal_error"
    status = "valid" if all(checks.values()) else "invalid"
    return {
        "schema": SCHEMA_VALIDATION,
        "status": status,
        "checks": checks,
        "error_reason": error_reason,
    }


class _Parser(argparse.ArgumentParser):
    """不把原始参数写入错误输出的参数解析器。"""

    def error(self, message: str) -> None:  # noqa: D401 - argparse contract
        """把解析错误压缩为固定 usage_error，避免回显路径或参数值。"""
        raise ExtractionError("usage_error")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        """帮助正常退出；其余退出统一转为固定错误。"""
        if status:
            raise ExtractionError("usage_error")
        raise SystemExit(0)


def _build_parser() -> argparse.ArgumentParser:
    """定义一次抽取或一次本机校验的固定入口。"""
    parser = _Parser(prog="extract.py")
    parser.add_argument("--source", help="只读源项目根")
    parser.add_argument("--output", help="忽略目录中的输出根")
    parser.add_argument("--validate", help="校验一个已生成的批次目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行 CLI，stdout 只输出固定 schema 的安全回执或校验结果。"""
    try:
        args = _build_parser().parse_args(argv)
        if args.validate:
            result = validate_batch(Path(args.validate))
            print(_json_text(result), end="")
            return 0 if result["status"] == "valid" else 2
        if not args.source or not args.output:
            raise ExtractionError("usage_error")
        receipt = run_extraction(Path(args.source), Path(args.output))
        print(_json_text(receipt), end="")
        safety_ok = all(
            value is True for value in receipt.get("safety", {}).values()
        )
        return (
            0
            if receipt["status"] in {"ok", "partial", "no_candidates"} and safety_ok
            else 2
        )
    except ExtractionError as exc:
        print(_json_text(_error_receipt(exc.reason)), end="")
        return 2
    except Exception:  # noqa: BLE001 - 固定错误，绝不输出 traceback
        print(_json_text(_error_receipt("internal_error")), end="")
        return 2


if __name__ == "__main__":
    sys.exit(main())
