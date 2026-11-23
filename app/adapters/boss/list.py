"""解析 BOSS 合成列表 XML、执行 M2 四源判定并合并连续分页快照。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from xml.etree import ElementTree

from app.adapters.boss.support import (
    EPOCH,
    FixtureEncodingError as _FixtureXmlError,
    XmlShapeError as _XmlError,
    collect_region_badge_values as _collect_region_badge_values,
    validate_coverage_proof as _validate_coverage_proof,
    parse_xml_root as _parse_xml_root,
    parent_map as _parent_map,
    read_json_file as _read_json_file,
    read_xml_text as _read_xml_text,
    require_text as _require_text,
    require_time as _require_time,
    resolve_fixture_reference as _resolve_fixture_reference,
    rid_matches as _rid_matches,
    row_nodes as _row_nodes,
    safe_time as _safe_time,
    validate_ratio_region,
)
from app.domain.models import (
    APP,
    Candidate,
    ReadResult,
    UnreadRow,
    UnreadSignal,
    UnreadSnapshot,
    canonical_hash,
    conversation_key_from_identity,
    discovery_key_for,
    fingerprint_for,
    identity_hash_for,
    is_conversation_key,
    is_hash,
    is_nonempty_text,
    is_uuid,
    parse_utc,
    validate_read_result,
)
from app.domain.scheduler import aggregate_unread

PROFILE_KEYS = frozenset(
    {
        "profile_id",
        "app",
        "version",
        "calibrated",
        "list_id_suffix",
        "row_id_suffix",
        "row_fields",
        "badge_region",
        "new_greeting",
        "tab_badge",
        "not_applicable_sources",
    }
)
ROW_FIELD_KEYS = ("name", "company", "position", "preview", "time")
IDENTITY_FIELD_KEYS = ("name", "company", "position")
MANIFEST_KEYS = frozenset(
    {
        "profile_path",
        "xml_paths",
        "app_version",
        "observed_at",
        "baseline_rows",
        "coverage_proof",
    }
)
BASELINE_KEYS = frozenset({"conversation_key", "preview_hash", "time_hint"})
SOURCE_ORDER = ("row_badge", "new_greeting", "tab_badge", "preview_delta")
UNKNOWN_REASON_PRIORITY = (
    "ambiguous_signal",
    "uncalibrated_region",
    "unverified_semantics",
    "missing_signal",
    "no_baseline",
    "incomplete_coverage",
)
SNAPSHOT_SECONDS = 5

_COUNT_RE = re.compile(r"^([0-9]+)(\+)?$")
_VIEWPORT = "viewport"
_ALL = "all"
_FIXTURE_SOURCE = "fixture"


@dataclass(frozen=True)
class SenseFixture:
    """保存已校验的合成夹具内容，供解析与合并阶段复用。"""

    profile: dict[str, object]
    baseline_rows: tuple[dict[str, object], ...]
    coverage_proof: dict[str, object]
    observed_at: str
    app_version: str
    xml_texts: tuple[str, ...]


def load_sense_fixture(manifest_path: Path) -> SenseFixture:
    """读取并严格校验 manifest、profile 与 XML 文件，拒绝越界或链接引用。"""
    try:
        manifest = _read_json_file(manifest_path)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("manifest is invalid") from None
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise ValueError("manifest keys are invalid")

    profile_path = _require_text(manifest["profile_path"])
    xml_values = manifest["xml_paths"]
    if (
        not isinstance(xml_values, list)
        or not xml_values
        or any(not is_nonempty_text(value) for value in xml_values)
        or len(set(xml_values)) != len(xml_values)
    ):
        raise ValueError("xml paths are invalid")
    app_version = _require_text(manifest["app_version"])
    observed_at = _require_time(manifest["observed_at"])
    baseline_rows = _validate_baseline_rows(manifest["baseline_rows"])
    coverage_proof = _validate_coverage_proof(manifest["coverage_proof"])
    if coverage_proof["kind"] == _VIEWPORT and len(xml_values) != 1:
        raise ValueError("viewport must contain exactly one page")

    fixture_dir = manifest_path.parent.resolve()
    profile_file = _resolve_fixture_reference(fixture_dir, profile_path)
    profile = _read_json_file(profile_file)
    validate_profile(profile)
    xml_files = [
        _resolve_fixture_reference(fixture_dir, value) for value in xml_values
    ]
    xml_texts: list[str] = []
    for path in xml_files:
        xml_texts.append(_read_xml_text(path))

    return SenseFixture(
        profile=profile,
        baseline_rows=baseline_rows,
        coverage_proof=coverage_proof,
        observed_at=observed_at,
        app_version=app_version,
        xml_texts=tuple(xml_texts),
    )


def load_and_parse_snapshot(
    fixture_path: Path, snapshot_id: str, now: str | None = None
) -> ReadResult:
    """读取整份合成夹具，逐页解析并合并为唯一读取结果；不读取 expected。"""
    if not is_uuid(snapshot_id):
        raise ValueError("snapshot id is invalid")
    try:
        fixture = load_sense_fixture(fixture_path)
    except _FixtureXmlError:
        return _failed("invalid_xml", snapshot_id, EPOCH)
    except (OSError, UnicodeError, ValueError):
        return _failed("invalid_fixture", snapshot_id, EPOCH)
    profile = fixture.profile
    if (
        profile["calibrated"] is not True
        or fixture.app_version != profile["version"]
    ):
        return _undetermined(
            "uncalibrated_profile", snapshot_id, fixture.observed_at
        )
    pages = [
        parse_snapshot(
            xml_text,
            profile,
            list(fixture.baseline_rows),
            fixture.observed_at,
            snapshot_id,
        )
        for xml_text in fixture.xml_texts
    ]
    effective_now = fixture.observed_at if now is None else now
    return merge_snapshots(
        pages,
        profile,
        list(fixture.baseline_rows),
        fixture.coverage_proof,
        effective_now,
    )


def parse_snapshot(
    xml_text: str,
    profile: dict,
    baseline_rows: list[dict],
    observed_at: str,
    snapshot_id: str,
) -> ReadResult:
    """解析单页层级 XML，按 profile 输出视口级四源结论、行与候选。"""
    safe_observed = _safe_time(observed_at)
    safe_snapshot = snapshot_id if is_uuid(snapshot_id) else None
    if not is_uuid(snapshot_id):
        return _failed("invalid_fixture", None, safe_observed)
    try:
        parse_utc(observed_at)
        validate_profile(profile)
        baselines = _validate_baseline_rows(baseline_rows)
    except ValueError:
        return _failed("invalid_fixture", safe_snapshot, safe_observed)
    if profile["calibrated"] is not True:
        return _undetermined("uncalibrated_profile", safe_snapshot, safe_observed)
    if not isinstance(xml_text, str):
        return _failed("invalid_xml", safe_snapshot, safe_observed)

    try:
        root = _parse_xml_root(xml_text)
        parents = _parent_map(root)
        list_nodes = [
            node for node in root.iter() if _rid_matches(node, _suffix(profile, "list"))
        ]
        if not list_nodes:
            return _failed("wrong_page", safe_snapshot, safe_observed)
        if len(list_nodes) > 1:
            return _undetermined(
                "incomplete_snapshot", safe_snapshot, safe_observed
            )
        list_node = list_nodes[0]
        row_nodes = _row_nodes(list_node, _suffix(profile, "row"))
        if row_nodes is None:
            return _undetermined(
                "incomplete_snapshot", safe_snapshot, safe_observed
            )
        parsed = [
            _parse_row(row_node, profile)
            for row_node in row_nodes
        ]
        unknown_reasons = [
            reason for _, reason in parsed if reason is not None
        ]
        rows = _normalize_identity_rows(tuple(row for row, _ in parsed))
        signals = {
            "row_badge": _row_badge_signal(
                rows,
                unknown_reasons,
                profile,
                full_traversal=False,
            ),
            "new_greeting": _global_signal(
                root,
                parents,
                row_nodes,
                profile,
                "new_greeting",
            ),
            "tab_badge": _global_signal(
                root,
                parents,
                row_nodes,
                profile,
                "tab_badge",
            ),
            "preview_delta": _preview_signal(
                rows,
                baselines,
                profile,
                full_traversal=False,
            ),
        }
        candidates = _build_candidates(rows, baselines, profile)
        snapshot = aggregate_unread(
            UnreadSnapshot(
                app=APP,
                profile_id=str(profile["profile_id"]),
                coverage=_VIEWPORT,
                signals=signals,
                rows=rows,
                candidates=candidates,
                unread_state="unknown",
            ),
            [],
        )
    except _XmlError:
        return _failed("invalid_xml", safe_snapshot, safe_observed)
    except (TypeError, ValueError, AttributeError):
        return _failed("invalid_fixture", safe_snapshot, safe_observed)
    return ReadResult(
        "ok",
        snapshot,
        None,
        safe_snapshot,
        safe_observed,
        _FIXTURE_SOURCE,
        "high",
    )


def merge_snapshots(
    pages: list[ReadResult],
    profile: dict,
    baseline_rows: list[dict],
    coverage_proof: dict,
    now: str,
) -> ReadResult:
    """合并同一遍历的连续视口页，重算信号、行身份、指纹与候选。"""
    if not isinstance(pages, (list, tuple)) or not pages:
        return _failed("invalid_fixture", None, _safe_time(now))
    for page in pages:
        if not isinstance(page, ReadResult):
            return _failed("invalid_fixture", None, _safe_time(now))
    for page in pages:
        try:
            _validate_page(page)
        except ValueError:
            return _failed("invalid_fixture", None, _safe_time(now))
    first = pages[0]
    try:
        validate_profile(profile)
        baselines = _validate_baseline_rows(baseline_rows)
        proof = _validate_coverage_proof(coverage_proof)
        now_dt = parse_utc(now)
    except ValueError:
        return _failed("invalid_fixture", first.snapshot_id, _safe_time(now))

    for page in pages:
        if page.status != "ok":
            return page

    snapshot_id = first.snapshot_id
    observed_at = first.observed_at
    profile_id = str(profile["profile_id"])
    if not is_uuid(snapshot_id):
        return _failed("invalid_fixture", None, observed_at)
    for page in pages:
        if (
            page.snapshot_id != snapshot_id
            or page.observed_at != observed_at
            or page.value is None
            or page.value.app != APP
            or page.value.profile_id != profile_id
        ):
            return _failed("invalid_fixture", snapshot_id, observed_at)
    if profile["calibrated"] is not True:
        return _undetermined("uncalibrated_profile", snapshot_id, observed_at)

    delta = (now_dt - parse_utc(observed_at)).total_seconds()
    if delta < 0 or delta > SNAPSHOT_SECONDS:
        return _undetermined("stale_snapshot", snapshot_id, observed_at)

    merged_rows: list[UnreadRow] = list(pages[0].value.rows)
    for previous, current in zip(pages, pages[1:]):
        assert previous.value is not None and current.value is not None
        overlap = _boundary_overlap(previous.value.rows, current.value.rows)
        merged_rows.extend(current.value.rows[overlap:])
    rows = _normalize_identity_rows(tuple(merged_rows))

    page_badge_reasons = [
        page.value.signals["row_badge"].reason_code
        for page in pages
        if page.value is not None
        and page.value.signals["row_badge"].state == "unknown"
    ]
    signals = {
        "row_badge": _row_badge_signal(
            rows,
            [reason for reason in page_badge_reasons if reason is not None],
            profile,
            full_traversal=False,
        ),
        "new_greeting": _merge_global_signals(
            [page.value.signals["new_greeting"] for page in pages if page.value]
        ),
        "tab_badge": _merge_global_signals(
            [page.value.signals["tab_badge"] for page in pages if page.value]
        ),
        "preview_delta": _preview_signal(
            rows, baselines, profile, full_traversal=False
        ),
    }
    coverage = _coverage(proof, profile, signals["tab_badge"])
    full_traversal = (
        coverage == _ALL
        and proof["kind"] == "full_traversal"
        and proof["top_proven"] is True
        and proof["bottom_proven"] is True
    )
    signals["row_badge"] = _row_badge_signal(
        rows,
        [reason for reason in page_badge_reasons if reason is not None],
        profile,
        full_traversal=full_traversal,
    )
    signals["preview_delta"] = _preview_signal(
        rows, baselines, profile, full_traversal=full_traversal
    )
    candidates = _build_candidates(rows, baselines, profile)
    snapshot = aggregate_unread(
        UnreadSnapshot(
            app=APP,
            profile_id=profile_id,
            coverage=coverage,
            signals=signals,
            rows=rows,
            candidates=candidates,
            unread_state="unknown",
        ),
        [],
    )
    return ReadResult(
        "ok",
        snapshot,
        None,
        snapshot_id,
        observed_at,
        _FIXTURE_SOURCE,
        "high",
    )


def validate_profile(profile: object) -> None:
    """严格校验 M2 最小 profile 的固定键、枚举、区域与全局信号配置。"""
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
        raise ValueError("profile keys are invalid")
    for name in (
        "profile_id",
        "version",
        "list_id_suffix",
        "row_id_suffix",
    ):
        _require_text(profile[name])
    if profile["app"] != APP:
        raise ValueError("profile app is invalid")
    if type(profile["calibrated"]) is not bool:
        raise ValueError("profile calibration is invalid")

    fields = profile["row_fields"]
    if not isinstance(fields, dict) or set(fields) != set(ROW_FIELD_KEYS):
        raise ValueError("row fields are invalid")
    for name in ROW_FIELD_KEYS:
        _require_text(fields[name])

    region = profile["badge_region"]
    if region is not None:
        validate_ratio_region(region)

    _validate_label_config(profile["new_greeting"], "semantics_verified")
    _validate_label_config(profile["tab_badge"], "global_zero_verified")

    sources = profile["not_applicable_sources"]
    if (
        not isinstance(sources, list)
        or any(
            not isinstance(value, str) or value not in SOURCE_ORDER
            for value in sources
        )
        or len(set(sources)) != len(sources)
    ):
        raise ValueError("not applicable sources are invalid")


def _validate_label_config(config: object, flag: str) -> None:
    """校验全局标签的固定键、文本与布尔证明位。"""
    if not isinstance(config, dict) or set(config) != {
        "label",
        "count_id_suffix",
        flag,
    }:
        raise ValueError("global signal config is invalid")
    _require_text(config["label"])
    _require_text(config["count_id_suffix"])
    if type(config[flag]) is not bool:
        raise ValueError("global signal proof is invalid")


def _validate_baseline_rows(value: object) -> tuple[dict[str, object], ...]:
    """校验 manifest 基线数组的固定键、会话键、摘要、时间与唯一性。"""
    if not isinstance(value, (list, tuple)):
        raise ValueError("baseline rows are invalid")
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != BASELINE_KEYS:
            raise ValueError("baseline row is invalid")
        key = item["conversation_key"]
        if not is_conversation_key(APP, key) or key in seen:
            raise ValueError("baseline conversation key is invalid")
        seen.add(str(key))
        preview_hash = item["preview_hash"]
        if preview_hash is not None and not is_hash(preview_hash):
            raise ValueError("baseline preview hash is invalid")
        time_hint = item["time_hint"]
        if time_hint is not None and not isinstance(time_hint, str):
            raise ValueError("baseline time is invalid")
        rows.append(
            {
                "conversation_key": str(key),
                "preview_hash": preview_hash,
                "time_hint": time_hint,
            }
        )
    return tuple(rows)


def _validate_page(page: ReadResult) -> None:
    """校验合并入口的页面为完整三态结果，拒绝非法原因或快照形状。"""
    validate_read_result(page)


def _parse_row(
    row_node: ElementTree.Element, profile: dict
) -> tuple[UnreadRow, str | None]:
    """解析一行身份、预览、时间与区域内角标，未知字段保持 null。"""
    fields = profile["row_fields"]
    texts = {
        name: _unique_field_text(row_node, str(fields[name])) for name in ROW_FIELD_KEYS
    }
    if all(texts[name] is not None for name in IDENTITY_FIELD_KEYS):
        identity_hash = identity_hash_for(
            texts["name"], texts["company"], texts["position"]
        )
        conversation_key = conversation_key_from_identity(APP, identity_hash)
    else:
        identity_hash = None
        conversation_key = None
    preview_hash = (
        canonical_hash(texts["preview"]) if texts["preview"] is not None else None
    )
    time_hint = texts["time"]
    not_applicable = profile["not_applicable_sources"]
    if "row_badge" in not_applicable:
        unread_count = None
        badge_reason = None
    else:
        unread_count, badge_reason = _row_badge(row_node, profile)
    return (
        UnreadRow(
            conversation_key=conversation_key,
            identity_hash=identity_hash,
            preview_hash=preview_hash,
            time_hint=time_hint,
            unread_count=unread_count,
        ),
        badge_reason,
    )


def _unique_field_text(
    row_node: ElementTree.Element, suffix: str
) -> str | None:
    """只接受行内唯一节点的原样非空 text，缺失或重复返回 None。"""
    matches = [
        node
        for node in row_node.iter()
        if node is not row_node and _rid_matches(node, suffix)
    ]
    if len(matches) != 1:
        return None
    text = matches[0].get("text")
    if not isinstance(text, str) or text == "":
        return None
    return text


def _row_badge(
    row_node: ElementTree.Element, profile: dict
) -> tuple[int | None, str | None]:
    """只在标定区域内读取唯一 TextView ASCII 整数，返回计数或未知原因。"""
    field_suffixes = {
        str(value) for value in profile["row_fields"].values()
    }
    values, reason = _collect_region_badge_values(
        row_node,
        profile["badge_region"],
        "android.widget.TextView",
        field_suffixes,
    )
    if reason is not None:
        return None, reason
    return values[0], None


def _global_signal(
    root: ElementTree.Element,
    parents: dict[ElementTree.Element, ElementTree.Element],
    row_nodes: list[ElementTree.Element],
    profile: dict,
    name: str,
) -> UnreadSignal:
    """按标签直接父容器解析新招呼或底部 tab 角标，不映射到任意行。"""
    if name in profile["not_applicable_sources"]:
        return UnreadSignal("not_applicable", None, "profile_not_applicable")
    config = profile[name]
    label = str(config["label"])
    labels = [
        node
        for node in root.iter()
        if node.tag == "node"
        and node.get("text") == label
        and not _inside_any(node, row_nodes, parents)
    ]
    if not labels:
        return UnreadSignal("unknown", None, "missing_signal")
    if len(labels) > 1:
        return UnreadSignal("unknown", None, "ambiguous_signal")
    container = parents.get(labels[0])
    if container is None:
        return UnreadSignal("unknown", None, "ambiguous_signal")
    counts = [
        node
        for node in container.iter()
        if _rid_matches(node, str(config["count_id_suffix"]))
    ]
    if not counts:
        return UnreadSignal("unknown", None, "missing_signal")
    if len(counts) > 1:
        return UnreadSignal("unknown", None, "ambiguous_signal")
    text = counts[0].get("text")
    if not isinstance(text, str):
        return UnreadSignal("unknown", None, "missing_signal")
    return _global_count_signal(text, name, config)


def _global_count_signal(
    text: str, name: str, config: dict
) -> UnreadSignal:
    """把全局计数文本转为 present/absent/unknown，不把下界当精确值。"""
    match = _COUNT_RE.fullmatch(text)
    if match is None:
        return UnreadSignal("unknown", None, "missing_signal")
    value = int(match.group(1))
    lower_bound = match.group(2) is not None
    if lower_bound:
        if value == 0:
            return UnreadSignal("unknown", None, "ambiguous_signal")
        return UnreadSignal("present", None, None)
    if value > 0:
        return UnreadSignal("present", value, None)
    if name == "new_greeting":
        if config["semantics_verified"] is True:
            return UnreadSignal("absent", 0, None)
        return UnreadSignal("unknown", None, "unverified_semantics")
    if config["global_zero_verified"] is True:
        return UnreadSignal("absent", 0, None)
    return UnreadSignal("unknown", None, "unverified_semantics")


def _inside_any(
    node: ElementTree.Element,
    row_nodes: list[ElementTree.Element],
    parents: dict[ElementTree.Element, ElementTree.Element],
) -> bool:
    """判断节点是否位于任一消息行子树内。"""
    current = node
    while current in parents:
        current = parents[current]
        if current in row_nodes:
            return True
    return False


def _row_badge_signal(
    rows: tuple[UnreadRow, ...],
    unknown_reasons: list[str],
    profile: dict,
    *,
    full_traversal: bool,
) -> UnreadSignal:
    """按行角标合并规则输出 present/absent/unknown 与优先级原因。"""
    if "row_badge" in profile["not_applicable_sources"]:
        return UnreadSignal("not_applicable", None, "profile_not_applicable")
    if not rows:
        return UnreadSignal("unknown", None, "missing_signal")
    counts = [row.unread_count for row in rows]
    positive = [count for count in counts if count is not None and count > 0]
    if positive:
        total = sum(count for count in counts if count is not None)
        exact = all(count is not None for count in counts)
        return UnreadSignal("present", total if exact else None, None)
    if all(count == 0 for count in counts):
        if full_traversal:
            return UnreadSignal("absent", 0, None)
        return UnreadSignal("unknown", None, "incomplete_coverage")
    return UnreadSignal("unknown", None, _reason(unknown_reasons))


def _preview_signal(
    rows: tuple[UnreadRow, ...],
    baseline_rows: tuple[dict[str, object], ...],
    profile: dict,
    *,
    full_traversal: bool,
) -> UnreadSignal:
    """按已验证基线逐行比较预览，输出已知变化行数或保守未知。"""
    if "preview_delta" in profile["not_applicable_sources"]:
        return UnreadSignal("not_applicable", None, "profile_not_applicable")
    if not rows:
        return UnreadSignal("unknown", None, "missing_signal")
    baselines = {str(item["conversation_key"]): item for item in baseline_rows}
    results = [_preview_change(row, baselines) for row in rows]
    changed = results.count("changed")
    if changed:
        return UnreadSignal("present", changed, None)
    if all(result == "same" for result in results):
        if full_traversal:
            return UnreadSignal("absent", 0, None)
        return UnreadSignal("unknown", None, "incomplete_coverage")
    return UnreadSignal("unknown", None, "no_baseline")


def _preview_change(
    row: UnreadRow, baselines: dict[str, dict[str, object]]
) -> str:
    """返回 changed/same/unknown；null 不证明无变化。"""
    if row.conversation_key is None or row.preview_hash is None:
        return "unknown"
    baseline = baselines.get(row.conversation_key)
    if baseline is None:
        return "unknown"
    preview_hash = baseline["preview_hash"]
    if not is_hash(preview_hash):
        return "unknown"
    if row.preview_hash != preview_hash:
        return "changed"
    row_time = row.time_hint
    baseline_time = baseline["time_hint"]
    if (
        isinstance(row_time, str)
        and row_time != ""
        and isinstance(baseline_time, str)
        and baseline_time != ""
    ):
        return "same" if row_time == baseline_time else "changed"
    return "unknown"


def _merge_global_signals(signals: list[UnreadSignal]) -> UnreadSignal:
    """合并各页全局信号；保留任一 present，冲突不求和。"""
    if all(signal == signals[0] for signal in signals):
        return signals[0]
    if any(signal.state == "present" for signal in signals):
        counts = [
            signal.count for signal in signals if signal.state == "present"
        ]
        if (
            len(counts) == len(signals)
            and all(count is not None for count in counts)
            and len(set(counts)) == 1
        ):
            return UnreadSignal("present", counts[0], None)
        return UnreadSignal("present", None, None)
    if all(signal.state == "absent" for signal in signals):
        return UnreadSignal("absent", 0, None)
    if all(signal.state == "not_applicable" for signal in signals):
        return UnreadSignal("not_applicable", None, "profile_not_applicable")
    return UnreadSignal("unknown", None, "ambiguous_signal")


def _boundary_overlap(
    previous: tuple[UnreadRow, ...], current: tuple[UnreadRow, ...]
) -> int:
    """只取相邻页之间最长连续整行完全一致的重叠长度。"""
    limit = min(len(previous), len(current))
    for length in range(limit, 0, -1):
        if tuple(previous[-length:]) == tuple(current[:length]):
            return length
    return 0


def _normalize_identity_rows(rows: tuple[UnreadRow, ...]) -> tuple[UnreadRow, ...]:
    """只保留全局唯一身份的会话键；缺失或重复身份不可定位。"""
    counts = Counter(
        row.identity_hash for row in rows if row.identity_hash is not None
    )
    normalized: list[UnreadRow] = []
    for row in rows:
        if row.identity_hash is not None and counts[row.identity_hash] == 1:
            normalized.append(
                replace(
                    row,
                    conversation_key=conversation_key_from_identity(
                        APP, row.identity_hash
                    ),
                )
            )
        else:
            normalized.append(replace(row, conversation_key=None))
    return tuple(normalized)


def _build_candidates(
    rows: tuple[UnreadRow, ...],
    baseline_rows: tuple[dict[str, object], ...],
    profile: dict,
) -> tuple[Candidate, ...]:
    """从合并后的唯一身份行重算 unread/new_message 候选与指纹。"""
    baselines = {str(item["conversation_key"]): item for item in baseline_rows}
    badge_applicable = "row_badge" not in profile["not_applicable_sources"]
    preview_applicable = "preview_delta" not in profile["not_applicable_sources"]
    candidates: list[Candidate] = []
    for row in rows:
        if row.conversation_key is None:
            continue
        changed = (
            preview_applicable
            and _preview_change(row, baselines) == "changed"
        )
        source_names: list[str] = []
        if badge_applicable and row.unread_count is not None and row.unread_count > 0:
            kind = "unread"
            source_names.append("row_badge")
        elif changed:
            kind = "new_message"
        else:
            continue
        if changed:
            source_names.append("preview_delta")
        fingerprint = fingerprint_for(
            row.preview_hash, row.time_hint, row.unread_count
        )
        candidates.append(
            Candidate(
                candidate_key=discovery_key_for(
                    APP, row.conversation_key, fingerprint
                ),
                conversation_key=row.conversation_key,
                kind=kind,
                source_names=tuple(source_names),
                fingerprint=fingerprint,
            )
        )
    return tuple(sorted(candidates, key=lambda candidate: candidate.candidate_key))


def _coverage(
    proof: dict[str, object], profile: dict, tab_signal: UnreadSignal
) -> str:
    """仅完整遍历或已验证全局零信号可声明 all，其余降为 viewport。"""
    if (
        proof["kind"] == "full_traversal"
        and proof["top_proven"] is True
        and proof["bottom_proven"] is True
    ):
        return _ALL
    if (
        proof["kind"] == "calibrated_global_zero"
        and profile["tab_badge"]["global_zero_verified"] is True
        and tab_signal.state == "absent"
        and tab_signal.count == 0
    ):
        return _ALL
    return _VIEWPORT


def _reason(reasons: list[str]) -> str:
    """按设计说明的原因优先级选择唯一未知原因，默认歧义。"""
    for reason in UNKNOWN_REASON_PRIORITY:
        if reason in reasons:
            return reason
    return "ambiguous_signal"


def _suffix(profile: dict, name: str) -> str:
    """返回列表或行的资源 ID 后缀。"""
    return str(profile["list_id_suffix"] if name == "list" else profile["row_id_suffix"])


def _failed(reason_code: str, snapshot_id: str | None, observed_at: str) -> ReadResult:
    """构造字段完整的失败读取结果，不携带部分快照。"""
    return ReadResult(
        "failed",
        None,
        reason_code,
        snapshot_id,
        observed_at,
        _FIXTURE_SOURCE,
        "low",
    )


def _undetermined(
    reason_code: str, snapshot_id: str | None, observed_at: str
) -> ReadResult:
    """构造字段完整的未决读取结果，不携带部分快照。"""
    return ReadResult(
        "undetermined",
        None,
        reason_code,
        snapshot_id,
        observed_at,
        _FIXTURE_SOURCE,
        "low",
    )
