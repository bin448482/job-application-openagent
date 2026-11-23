"""实现 M3 页面/职位/未读/聊天层级解析、跨屏合并与合成 read 夹具入口。"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from xml.etree import ElementTree

from app.adapters.boss.pixel import parse_pixel_row_badge, validate_calibration
from app.adapters.boss.support import (
    EPOCH,
    FixtureEncodingError,
    XmlShapeError,
    parse_bounds,
    parse_xml_root,
    read_json_file,
    read_xml_text,
    rect_inside,
    require_text,
    require_time,
    resolve_fixture_reference,
    rid_matches,
    safe_time,
    validate_ratio_region,
)
from app.domain.ladder import (
    FieldReading,
    resolve_field,
    validate_field_resolution,
)
from app.domain.models import (
    APP,
    ReadResult,
    canonical_hash,
    conversation_key_from_identity,
    identity_hash_for,
    is_hash,
    is_nonempty_text,
    is_uuid,
    parse_utc,
    validate_read_result,
)
from app.domain.reading import (
    ChatMessage,
    Conversation,
    FieldEvidence,
    FIELD_REASON_PRIORITY,
    JobCard,
    JobDetail,
    JobList,
    PageSnapshot,
    UnreadRead,
    UnreadRowRead,
    conversation_inbound_state,
    job_identity_hash_for,
    job_key_from_identity,
    message_key_from_stable,
    message_stable_identity_hash,
    normalize_job_company,
    normalize_job_salary,
    normalize_job_title,
)

PROFILE_KEYS = frozenset({"profile_id", "app", "version", "calibrated", "selectors"})
SELECTOR_KEYS = frozenset(
    {
        "bottom_labels",
        "popup_markers",
        "login_markers",
        "recommend_markers",
        "chat_list_suffix",
        "chat_detail_suffixes",
        "job_description_suffix",
        "job_chat_suffixes",
        "job_title_suffix",
        "job_salary_suffix",
        "job_company_suffix",
        "job_recruiter_suffix",
        "job_location_suffix",
        "conversation_list_suffix",
        "conversation_title_suffix",
        "conversation_subtitle_suffix",
        "conversation_row_suffix",
        "conversation_text_suffix",
        "conversation_time_suffix",
        "conversation_system_suffix",
        "conversation_card_suffix",
        "conversation_avatar_suffix",
        "conversation_bubble_suffix",
        "conversation_status_suffix",
        "conversation_stable_id_suffix",
        "conversation_stable_id_verified",
        "unread_list_suffix",
        "unread_row_suffix",
        "unread_name_suffix",
        "unread_company_suffix",
        "unread_position_suffix",
        "unread_badge_region",
        "unread_badge_class",
    }
)
MANIFEST_KEYS = frozenset(
    {
        "profile_path",
        "app_version",
        "observed_at",
        "kind",
        "hierarchy_paths",
        "coverage_proof",
        "conversation_context",
        "pixel",
    }
)
CONTEXT_KEYS = frozenset({"contact", "company", "position", "source_snapshot_id"})
PIXEL_KEYS = frozenset({"calibration_path", "raw_path"})
COVERAGE_KEYS = frozenset({"kind", "top_proven", "bottom_proven"})
COVERAGE_KINDS = frozenset({"viewport", "full_traversal", "calibrated_global_zero"})
READ_KINDS = frozenset(
    {"page", "job_list", "job_detail", "unread", "conversation"}
)
SNAPSHOT_SECONDS = 5
_FIXTURE_SOURCE = "fixture"
_HR_ROLE_TERMS = ("招聘者", "猎头", "人事", "recruiter", "hr")
_ASCII_INT_RE = __import__("re").compile(r"^[0-9]+$")
_DIRECTION_NAMES = ("avatar", "bubble", "sent_status")


@dataclass(frozen=True)
class ReadFixture:
    """保存已校验的 M3 profile、XML、像素与上下文输入。"""

    profile: dict[str, object]
    app_version: str
    observed_at: str
    kind: str
    xml_texts: tuple[str, ...]
    coverage_proof: dict[str, object]
    conversation_context: dict[str, str] | None
    calibration: dict[str, object] | None
    raw_screen: bytes | None


def load_read_fixture(manifest_path: Path) -> ReadFixture:
    """读取并严格校验 M3 manifest、profile、XML 与可选像素文件。"""
    try:
        manifest = read_json_file(manifest_path)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("manifest is invalid") from None
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise ValueError("manifest keys are invalid")
    kind = manifest["kind"]
    if not isinstance(kind, str) or kind not in READ_KINDS:
        raise ValueError("read kind is invalid")
    app_version = require_text(manifest["app_version"])
    observed_at = require_time(manifest["observed_at"])
    coverage_proof = _validate_coverage_proof(manifest["coverage_proof"])
    hierarchy_values = manifest["hierarchy_paths"]
    if (
        not isinstance(hierarchy_values, list)
        or not hierarchy_values
        or any(not is_nonempty_text(value) for value in hierarchy_values)
        or len(set(hierarchy_values)) != len(hierarchy_values)
    ):
        raise ValueError("hierarchy paths are invalid")
    conversation_context = _validate_context(manifest["conversation_context"])
    pixel = manifest["pixel"]
    if kind == "conversation":
        if conversation_context is None:
            raise ValueError("conversation context is required")
    elif conversation_context is not None:
        raise ValueError("conversation context is not allowed")
    if kind != "conversation" and len(hierarchy_values) != 1:
        raise ValueError("read kind requires exactly one hierarchy")
    if kind != "unread" and pixel is not None:
        raise ValueError("pixel is only allowed for unread")

    fixture_dir = manifest_path.parent.resolve()
    profile_path = resolve_fixture_reference(
        fixture_dir, require_text(manifest["profile_path"])
    )
    profile = read_json_file(profile_path)
    validate_read_profile(profile)
    xml_texts = tuple(
        read_xml_text(resolve_fixture_reference(fixture_dir, value))
        for value in hierarchy_values
    )
    calibration: dict[str, object] | None = None
    raw_screen: bytes | None = None
    if pixel is not None:
        if not isinstance(pixel, dict) or set(pixel) != PIXEL_KEYS:
            raise ValueError("pixel reference is invalid")
        calibration_path = resolve_fixture_reference(
            fixture_dir, require_text(pixel["calibration_path"])
        )
        raw_path = resolve_fixture_reference(fixture_dir, require_text(pixel["raw_path"]))
        calibration = read_json_file(calibration_path)
        validate_calibration(calibration)
        try:
            raw_screen = raw_path.read_bytes()
        except OSError:
            raise ValueError("screen is unreadable") from None
    return ReadFixture(
        profile=profile,
        app_version=app_version,
        observed_at=observed_at,
        kind=kind,
        xml_texts=xml_texts,
        coverage_proof=coverage_proof,
        conversation_context=conversation_context,
        calibration=calibration,
        raw_screen=raw_screen,
    )


def load_and_read_fixture(
    fixture_path: Path, snapshot_id: str, now: str | None = None
) -> ReadResult:
    """加载 M3 合成夹具并分派到生产解析/阶梯/跨屏合并函数。"""
    if not is_uuid(snapshot_id):
        raise ValueError("snapshot id is invalid")
    try:
        fixture = load_read_fixture(fixture_path)
    except FixtureEncodingError:
        return _failed("invalid_xml", snapshot_id, EPOCH)
    except (OSError, UnicodeError, ValueError):
        return _failed("invalid_fixture", None, EPOCH)
    profile = fixture.profile
    if (
        profile["calibrated"] is not True
        or fixture.app_version != profile["version"]
    ):
        return _undetermined("uncalibrated_profile", snapshot_id, fixture.observed_at)
    try:
        if fixture.kind == "page":
            return parse_page(
                fixture.xml_texts[0], profile, fixture.observed_at, snapshot_id
            )
        if fixture.kind == "job_list":
            return parse_job_list(
                fixture.xml_texts[0], profile, fixture.observed_at, snapshot_id
            )
        if fixture.kind == "job_detail":
            return parse_job_detail(
                fixture.xml_texts[0], profile, fixture.observed_at, snapshot_id
            )
        if fixture.kind == "unread":
            effective_now = fixture.observed_at if now is None else now
            return parse_unread(
                fixture.xml_texts[0],
                profile,
                fixture.calibration,
                fixture.raw_screen,
                fixture.observed_at,
                snapshot_id,
                fixture.coverage_proof,
                effective_now,
            )
        pages = [
            parse_conversation_page(
                xml_text,
                profile,
                fixture.conversation_context,
                fixture.observed_at,
                snapshot_id,
            )
            for xml_text in fixture.xml_texts
        ]
        effective_now = fixture.observed_at if now is None else now
        return merge_conversations(
            pages, profile, fixture.coverage_proof, effective_now
        )
    except Exception:
        return _failed("invalid_fixture", snapshot_id, fixture.observed_at)


def validate_read_profile(profile: object) -> None:
    """严格校验 M3 读取 profile 的固定键、选择器类型与合成标定声明。"""
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
        raise ValueError("read profile keys are invalid")
    if not is_nonempty_text(profile["profile_id"]) or profile["app"] != APP:
        raise ValueError("read profile identity is invalid")
    if not is_nonempty_text(profile["version"]) or type(profile["calibrated"]) is not bool:
        raise ValueError("read profile version is invalid")
    selectors = profile["selectors"]
    if not isinstance(selectors, dict) or set(selectors) != SELECTOR_KEYS:
        raise ValueError("selectors are invalid")
    _require_text_list(selectors["bottom_labels"], minimum=2)
    for name in ("popup_markers", "login_markers", "recommend_markers"):
        _require_text_list(selectors[name], minimum=0)
    for name in (
        "chat_list_suffix",
        "job_description_suffix",
        "job_title_suffix",
        "job_salary_suffix",
        "job_company_suffix",
        "job_recruiter_suffix",
        "job_location_suffix",
        "conversation_list_suffix",
        "conversation_title_suffix",
        "conversation_subtitle_suffix",
        "conversation_row_suffix",
        "conversation_text_suffix",
        "conversation_time_suffix",
        "conversation_system_suffix",
        "conversation_card_suffix",
        "conversation_avatar_suffix",
        "conversation_bubble_suffix",
        "conversation_status_suffix",
        "unread_list_suffix",
        "unread_row_suffix",
        "unread_name_suffix",
        "unread_company_suffix",
        "unread_position_suffix",
        "unread_badge_class",
    ):
        if not is_nonempty_text(selectors[name]):
            raise ValueError("selector is invalid")
    for name in ("chat_detail_suffixes", "job_chat_suffixes"):
        _require_text_list(selectors[name], minimum=1)
    stable_suffix = selectors["conversation_stable_id_suffix"]
    if stable_suffix is not None and not is_nonempty_text(stable_suffix):
        raise ValueError("stable id suffix is invalid")
    if type(selectors["conversation_stable_id_verified"]) is not bool:
        raise ValueError("stable id verified is invalid")
    if selectors["conversation_stable_id_verified"] and stable_suffix is None:
        raise ValueError("verified stable id requires suffix")
    region = selectors["unread_badge_region"]
    if region is not None:
        validate_ratio_region(region)


def parse_page(
    xml_text: str, profile: dict, observed_at: str, snapshot_id: str
) -> ReadResult:
    """按 CORE-W14 优先级分类页面并输出唯一字段证据。"""
    safe_time_value = safe_time(observed_at)
    safe_snapshot = snapshot_id if is_uuid(snapshot_id) else None
    if not is_uuid(snapshot_id):
        return _failed("invalid_fixture", None, safe_time_value)
    try:
        validate_read_profile(profile)
        parse_utc(observed_at)
    except ValueError:
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    try:
        root = parse_xml_root(xml_text)
    except XmlShapeError:
        return _failed("invalid_xml", safe_snapshot, safe_time_value)
    try:
        text_set = _text_set(root)
        selectors = profile["selectors"]
        signals: list[str] = []
        page = "unknown"
        reason = "missing_signal"
        if _matches_any(text_set, selectors["popup_markers"]):
            page = "popup"
            signals.append("popup")
        elif _matches_any(text_set, selectors["login_markers"]):
            page = "login"
            signals.append("login")
        elif _has_any_suffix(root, selectors["chat_detail_suffixes"]):
            page = "chat_detail"
            signals.append("chat_detail")
        elif _nodes(root, str(selectors["job_description_suffix"])) and _has_any_suffix(
            root, selectors["job_chat_suffixes"]
        ):
            page = "job_detail"
            signals.append("job_detail")
        elif _matches_any(text_set, selectors["recommend_markers"]):
            page = "recommend_feed"
            signals.append("recommend_feed")
        else:
            list_nodes = _nodes(root, str(selectors["chat_list_suffix"]))
            if len(list_nodes) > 1:
                reason = "ambiguous_signal"
            elif len(list_nodes) == 1:
                page = "chat_list"
                signals.append("chat_list")
            elif len(text_set & set(selectors["bottom_labels"])) >= 2:
                page = "main"
                signals.append("bottom_tabs")
        evidence = FieldEvidence(
            field="page",
            source="hierarchy",
            confidence="high" if page != "unknown" else "low",
            state="ok" if page != "unknown" else "unknown",
            reason_code=None if page != "unknown" else reason,
        )
        snapshot = PageSnapshot(
            app=APP,
            profile_id=str(profile["profile_id"]),
            page=page,
            signals=tuple(signals),
            field_evidence=(evidence,),
        )
    except (TypeError, ValueError, AttributeError):
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    return _ok(snapshot, safe_snapshot, safe_time_value)


def parse_job_list(
    xml_text: str, profile: dict, observed_at: str, snapshot_id: str
) -> ReadResult:
    """提取当前视口内完整职位卡并重算去重后的职位键。"""
    safe_time_value = safe_time(observed_at)
    safe_snapshot = snapshot_id if is_uuid(snapshot_id) else None
    if not is_uuid(snapshot_id):
        return _failed("invalid_fixture", None, safe_time_value)
    try:
        validate_read_profile(profile)
        parse_utc(observed_at)
    except ValueError:
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    try:
        root = parse_xml_root(xml_text)
    except XmlShapeError:
        return _failed("invalid_xml", safe_snapshot, safe_time_value)
    try:
        selectors = profile["selectors"]
        cards: list[JobCard] = []
        for node in root.iter():
            if node.get("clickable") != "true":
                continue
            title = _unique_text(node, str(selectors["job_title_suffix"]))
            salary = _unique_text(node, str(selectors["job_salary_suffix"]))
            company = _unique_text(node, str(selectors["job_company_suffix"]))
            if title is None or salary is None or company is None:
                continue
            if not normalize_job_title(title):
                continue
            identity = job_identity_hash_for(title, company, salary)
            cards.append(
                JobCard(
                    job_key=job_key_from_identity(identity),
                    identity_hash=identity,
                    title_raw=title,
                    title=normalize_job_title(title),
                    company_raw=company,
                    company=normalize_job_company(company),
                    salary_raw=salary,
                    salary=normalize_job_salary(salary),
                )
            )
        if not cards:
            return _undetermined("incomplete_snapshot", safe_snapshot, safe_time_value)
        counts: dict[str, int] = {}
        for card in cards:
            assert card.identity_hash is not None
            counts[card.identity_hash] = counts.get(card.identity_hash, 0) + 1
        cards = [
            replace(card, job_key=None)
            if card.identity_hash is not None and counts[card.identity_hash] > 1
            else card
            for card in cards
        ]
        value = JobList(
            app=APP,
            profile_id=str(profile["profile_id"]),
            coverage="viewport",
            cards=tuple(cards),
            field_evidence=(
                FieldEvidence("job_cards", "hierarchy", "high", "ok", None),
            ),
        )
    except (TypeError, ValueError, AttributeError):
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    return _ok(value, safe_snapshot, safe_time_value)


def parse_job_detail(
    xml_text: str, profile: dict, observed_at: str, snapshot_id: str
) -> ReadResult:
    """解析职位详情正文、字段与完整性；截断正文不得返回部分值。"""
    safe_time_value = safe_time(observed_at)
    safe_snapshot = snapshot_id if is_uuid(snapshot_id) else None
    if not is_uuid(snapshot_id):
        return _failed("invalid_fixture", None, safe_time_value)
    try:
        validate_read_profile(profile)
        parse_utc(observed_at)
    except ValueError:
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    try:
        root = parse_xml_root(xml_text)
    except XmlShapeError:
        return _failed("invalid_xml", safe_snapshot, safe_time_value)
    try:
        selectors = profile["selectors"]
        description_nodes = _nodes(root, str(selectors["job_description_suffix"]))
        chat_nodes = _unique_nodes_any(root, selectors["job_chat_suffixes"])
        if len(description_nodes) != 1 or not chat_nodes:
            return _failed("wrong_page", safe_snapshot, safe_time_value)
        description = description_nodes[0].get("text")
        if not isinstance(description, str) or description == "":
            return _failed("wrong_page", safe_snapshot, safe_time_value)
        title = _unique_text(root, str(selectors["job_title_suffix"]))
        salary = _unique_text(root, str(selectors["job_salary_suffix"]))
        company = _unique_text(root, str(selectors["job_company_suffix"]))
        if title is None or salary is None or company is None:
            return _failed("wrong_page", safe_snapshot, safe_time_value)
        if _description_incomplete(description):
            return _undetermined("incomplete_snapshot", safe_snapshot, safe_time_value)
        recruiter = _unique_text(root, str(selectors["job_recruiter_suffix"]))
        location = _unique_text(root, str(selectors["job_location_suffix"]))
        missing: list[str] = []
        if recruiter is None:
            missing.append("recruiter")
        if location is None:
            missing.append("location")
        identity = job_identity_hash_for(title, company, salary)
        value = JobDetail(
            app=APP,
            profile_id=str(profile["profile_id"]),
            job_key=job_key_from_identity(identity),
            identity_hash=identity,
            title_raw=title,
            title=normalize_job_title(title),
            company_raw=company,
            company=normalize_job_company(company),
            salary_raw=salary,
            salary=normalize_job_salary(salary),
            recruiter_raw=recruiter,
            recruiter=None if recruiter is None else recruiter.strip(),
            location_raw=location,
            location=None if location is None else location.strip(),
            description=description,
            description_hash=canonical_hash(description),
            description_length=len(description),
            complete=True,
            missing_fields=tuple(missing),
            field_evidence=(
                FieldEvidence("job_detail", "hierarchy", "high", "ok", None),
            ),
        )
    except (TypeError, ValueError, AttributeError):
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    return _ok(value, safe_snapshot, safe_time_value)


def parse_unread(
    xml_text: str,
    profile: dict,
    calibration: dict[str, object] | None,
    raw_screen: bytes | None,
    observed_at: str,
    snapshot_id: str,
    coverage_proof: dict[str, object],
    now: str,
) -> ReadResult:
    """解析未读行并按字段阶梯合并层级/像素，必要时保留 unknown。"""
    safe_time_value = safe_time(observed_at)
    safe_snapshot = snapshot_id if is_uuid(snapshot_id) else None
    if not is_uuid(snapshot_id):
        return _failed("invalid_fixture", None, safe_time_value)
    try:
        validate_read_profile(profile)
        proof = _validate_coverage_proof(coverage_proof)
        parse_utc(observed_at)
        now_dt = parse_utc(now)
        delta = (now_dt - parse_utc(observed_at)).total_seconds()
    except ValueError:
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    if delta < 0 or delta > SNAPSHOT_SECONDS:
        return _undetermined("stale_snapshot", safe_snapshot, safe_time_value)
    try:
        root = parse_xml_root(xml_text)
    except XmlShapeError:
        return _failed("invalid_xml", safe_snapshot, safe_time_value)
    try:
        selectors = profile["selectors"]
        list_nodes = _nodes(root, str(selectors["unread_list_suffix"]))
        if not list_nodes:
            return _failed("wrong_page", safe_snapshot, safe_time_value)
        if len(list_nodes) > 1:
            return _undetermined("incomplete_snapshot", safe_snapshot, safe_time_value)
        row_nodes = _row_nodes(list_nodes[0], str(selectors["unread_row_suffix"]))
        if row_nodes is None or not row_nodes:
            return _undetermined("incomplete_snapshot", safe_snapshot, safe_time_value)
        context = _field_context(profile, snapshot_id, observed_at)
        rows: list[UnreadRowRead] = []
        for row_node in row_nodes:
            row = _parse_unread_row(
                row_node,
                profile,
                calibration,
                raw_screen,
                context,
            )
            if row is None:
                return _failed("invalid_fixture", safe_snapshot, safe_time_value)
            rows.append(row)
        counts: dict[str, int] = {}
        for row in rows:
            if row.identity_hash is not None:
                counts[row.identity_hash] = counts.get(row.identity_hash, 0) + 1
        normalized: list[UnreadRowRead] = []
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
        coverage = (
            "all"
            if proof["kind"] == "full_traversal"
            and proof["top_proven"] is True
            and proof["bottom_proven"] is True
            else "viewport"
        )
        unread_state = "unknown"
        if any(row.state == "present" for row in normalized):
            unread_state = "present"
        elif coverage == "all" and all(row.state == "absent" for row in normalized):
            unread_state = "absent"
        evidence = _unread_evidence(tuple(normalized))
        value = UnreadRead(
            app=APP,
            profile_id=str(profile["profile_id"]),
            coverage=coverage,
            rows=tuple(normalized),
            unread_state=unread_state,
            field_evidence=(evidence,),
        )
    except (TypeError, ValueError, AttributeError):
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    return _ok(value, safe_snapshot, safe_time_value)


def parse_conversation_page(
    xml_text: str,
    profile: dict,
    context: dict[str, str] | None,
    observed_at: str,
    snapshot_id: str,
) -> ReadResult:
    """解析单页会话消息、方向证据与应用稳定消息身份。"""
    safe_time_value = safe_time(observed_at)
    safe_snapshot = snapshot_id if is_uuid(snapshot_id) else None
    if not is_uuid(snapshot_id):
        return _failed("invalid_fixture", None, safe_time_value)
    try:
        validate_read_profile(profile)
        parse_utc(observed_at)
        if context is None:
            return _undetermined("identity_ambiguous", safe_snapshot, safe_time_value)
        contact_raw = require_text(context["contact"])
        company = require_text(context["company"])
        position = require_text(context["position"])
    except ValueError:
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    try:
        root = parse_xml_root(xml_text)
    except XmlShapeError:
        return _failed("invalid_xml", safe_snapshot, safe_time_value)
    try:
        selectors = profile["selectors"]
        list_nodes = _nodes(root, str(selectors["conversation_list_suffix"]))
        if not list_nodes:
            return _failed("wrong_page", safe_snapshot, safe_time_value)
        if len(list_nodes) > 1:
            return _undetermined("incomplete_snapshot", safe_snapshot, safe_time_value)
        list_node = list_nodes[0]
        contact = _unique_text(root, str(selectors["conversation_title_suffix"]))
        if contact is None:
            return _failed("wrong_page", safe_snapshot, safe_time_value)
        if contact != contact_raw:
            return _undetermined("identity_ambiguous", safe_snapshot, safe_time_value)
        identity = identity_hash_for(contact, company, position)
        conversation_key = conversation_key_from_identity(APP, identity)
        subtitle = _unique_text(root, str(selectors["conversation_subtitle_suffix"]))
        role = "hr" if subtitle and _is_hr_role(subtitle) else "unknown"
        row_nodes = _row_nodes(list_node, str(selectors["conversation_row_suffix"]))
        if row_nodes is None:
            return _undetermined("incomplete_snapshot", safe_snapshot, safe_time_value)
        message_identity_verified = bool(
            selectors["conversation_stable_id_verified"]
            and selectors["conversation_stable_id_suffix"] is not None
        )
        messages = tuple(
            _parse_chat_message(
                row_node,
                profile,
                conversation_key,
                message_identity_verified,
            )
            for row_node in row_nodes
        )
        messages = _clear_duplicate_stable_ids(messages)
        read_complete = list_node.get("scrollable") == "false"
        state, key, reason = conversation_inbound_state(
            messages, read_complete, True, message_identity_verified
        )
        inbound_evidence = FieldEvidence(
            field="conversation_inbound",
            source="hierarchy",
            confidence="high" if state != "unknown" else "low",
            state="ok" if state != "unknown" else "unknown",
            reason_code=None if state != "unknown" else reason,
        )
        value = Conversation(
            app=APP,
            profile_id=str(profile["profile_id"]),
            conversation_key=conversation_key,
            identity_hash=identity,
            contact=contact,
            contact_verified=True,
            role=role,
            message_identity_verified=message_identity_verified,
            read_complete=read_complete,
            messages=messages,
            inbound_state=state,
            latest_inbound_key=key,
            field_evidence=(
                FieldEvidence(
                    "conversation_identity", "hierarchy", "high", "ok", None
                ),
                inbound_evidence,
            ),
        )
    except (TypeError, ValueError, AttributeError):
        return _failed("invalid_fixture", safe_snapshot, safe_time_value)
    return _ok(value, safe_snapshot, safe_time_value)


def merge_conversations(
    pages: list[ReadResult],
    profile: dict,
    coverage_proof: dict[str, object],
    now: str,
) -> ReadResult:
    """合并同一次遍历的会话页，只裁剪有稳定身份的连续边界重叠。"""
    if not isinstance(pages, (list, tuple)) or not pages:
        return _failed("invalid_fixture", None, _safe_now(now))
    if any(not isinstance(page, ReadResult) for page in pages):
        return _failed("invalid_fixture", None, _safe_now(now))
    try:
        validate_read_profile(profile)
        proof = _validate_coverage_proof(coverage_proof)
        now_dt = parse_utc(now)
        for page in pages:
            validate_read_result(page)
    except ValueError:
        return _failed("invalid_fixture", None, _safe_now(now))
    for page in pages:
        if page.status != "ok":
            return page
    first = pages[0]
    assert isinstance(first.value, Conversation)
    snapshot_id = first.snapshot_id
    observed_at = first.observed_at
    delta = (now_dt - parse_utc(observed_at)).total_seconds()
    if delta < 0 or delta > SNAPSHOT_SECONDS:
        return _undetermined("stale_snapshot", snapshot_id, observed_at)
    for page in pages:
        value = page.value
        assert isinstance(value, Conversation)
        if (
            page.snapshot_id != snapshot_id
            or page.observed_at != observed_at
            or value.app != APP
            or value.profile_id != first.value.profile_id
            or value.conversation_key != first.value.conversation_key
            or value.contact != first.value.contact
        ):
            return _failed("invalid_fixture", snapshot_id, observed_at)
    merged = list(first.value.messages)
    for previous, current in zip(pages, pages[1:]):
        assert isinstance(previous.value, Conversation)
        assert isinstance(current.value, Conversation)
        overlap = _conversation_overlap(previous.value.messages, current.value.messages)
        merged.extend(current.value.messages[overlap:])
    messages = _clear_duplicate_stable_ids(tuple(merged))
    read_complete = bool(
        proof["kind"] == "full_traversal"
        and proof["top_proven"] is True
        and proof["bottom_proven"] is True
    ) or (len(pages) == 1 and first.value.read_complete)
    if not read_complete:
        return _undetermined("incomplete_snapshot", snapshot_id, observed_at)
    state, key, reason = conversation_inbound_state(
        messages,
        read_complete,
        first.value.contact_verified,
        first.value.message_identity_verified,
    )
    inbound_evidence = FieldEvidence(
        field="conversation_inbound",
        source="hierarchy",
        confidence="high" if state != "unknown" else "low",
        state="ok" if state != "unknown" else "unknown",
        reason_code=None if state != "unknown" else reason,
    )
    value = replace(
        first.value,
        messages=messages,
        read_complete=read_complete,
        inbound_state=state,
        latest_inbound_key=key,
        field_evidence=(
            FieldEvidence("conversation_identity", "hierarchy", "high", "ok", None),
            inbound_evidence,
        ),
    )
    return _ok(value, snapshot_id, observed_at)


def _parse_unread_row(
    row_node: ElementTree.Element,
    profile: dict,
    calibration: dict[str, object] | None,
    raw_screen: bytes | None,
    context: dict[str, str],
) -> UnreadRowRead | None:
    """解析一行未读角标并执行层级/像素字段阶梯；结构失败返回 None。"""
    selectors = profile["selectors"]
    name = _unique_text(row_node, str(selectors["unread_name_suffix"]))
    company = _unique_text(row_node, str(selectors["unread_company_suffix"]))
    position = _unique_text(row_node, str(selectors["unread_position_suffix"]))
    identity = (
        identity_hash_for(name, company, position)
        if name is not None and company is not None and position is not None
        else None
    )
    hierarchy = _hierarchy_badge_reading(row_node, profile, context)
    pixel = None
    if calibration is not None and raw_screen is not None:
        pixel = parse_pixel_row_badge(
            raw_screen,
            calibration,
            parse_bounds(row_node),
            app=context["app"],
            profile_id=context["profile_id"],
            profile_version=context["profile_version"],
            snapshot_id=context["snapshot_id"],
            observed_at=context["observed_at"],
            layout_context=context["layout_context"],
        )
    resolution = resolve_field(hierarchy=hierarchy, pixel=pixel)
    validate_field_resolution(resolution)
    if resolution.state == "failed":
        return None
    if resolution.state == "undetermined":
        state = "unknown"
        count = None
        source = "none"
        confidence = "low"
        reason = resolution.reason_code
    else:
        state = resolution.state
        count = resolution.count
        source = resolution.source or "none"
        confidence = resolution.confidence or "low"
        reason = None
    return UnreadRowRead(
        identity_hash=identity,
        conversation_key=None,
        state=state,
        count=count,
        source=source,
        confidence=confidence,
        reason_code=reason,
    )


def _hierarchy_badge_reading(
    row_node: ElementTree.Element, profile: dict, context: dict[str, str]
) -> FieldReading:
    """在标定行区域内读取唯一 TextView ASCII 整数，返回层级单级结论。"""
    selectors = profile["selectors"]
    region = selectors["unread_badge_region"]
    row_bounds = parse_bounds(row_node)

    def reading(state: str, count: int | None, reason: str | None) -> FieldReading:
        """用固定绑定上下文构造层级字段结论。"""
        return FieldReading(
            field="unread_badges",
            app=context["app"],
            profile_id=context["profile_id"],
            profile_version=context["profile_version"],
            snapshot_id=context["snapshot_id"],
            observed_at=context["observed_at"],
            layout_context=context["layout_context"],
            source="hierarchy",
            confidence="high" if state in {"present", "absent"} else "low",
            state=state,
            count=count,
            reason_code=reason,
        )

    if region is None:
        return reading("unknown", None, "uncalibrated_region")
    if row_bounds is None:
        return reading("unknown", None, "uncalibrated_region")
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
    except (OverflowError, ValueError):
        return reading("unknown", None, "uncalibrated_region")
    field_suffixes = {
        str(selectors["unread_name_suffix"]),
        str(selectors["unread_company_suffix"]),
        str(selectors["unread_position_suffix"]),
    }
    values: list[int] = []
    invalid_bounds = False
    for node in row_node.iter():
        if node is row_node or node.get("class") != selectors["unread_badge_class"]:
            continue
        if any(rid_matches(node, suffix) for suffix in field_suffixes):
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
        return reading("unknown", None, "uncalibrated_region")
    if len(values) > 1:
        return reading("unknown", None, "ambiguous_signal")
    if len(values) == 1:
        return reading("present", values[0], None)
    return reading("unknown", None, "missing_signal")


def _parse_chat_message(
    row_node: ElementTree.Element,
    profile: dict,
    conversation_key: str,
    message_identity_verified: bool,
) -> ChatMessage:
    """解析一条聊天行的类型、方向、截断与稳定身份，不靠内容生成身份。"""
    selectors = profile["selectors"]
    system_text = _unique_text(row_node, str(selectors["conversation_system_suffix"]))
    card = _unique_node(row_node, str(selectors["conversation_card_suffix"]))
    text = _unique_text(row_node, str(selectors["conversation_text_suffix"]))
    time_hint = _unique_text(row_node, str(selectors["conversation_time_suffix"]))
    if system_text is not None:
        kind = "system_tip"
        direction = "system"
        evidence: tuple[str, ...] = ()
        content = system_text
    elif card is not None and text is None:
        kind = "card"
        direction, evidence = _direction_for(row_node, profile)
        content = None
    elif text is not None:
        kind = "text"
        direction, evidence = _direction_for(row_node, profile)
        content = text
    elif time_hint is not None:
        kind = "time"
        direction = "system"
        evidence = ()
        content = None
    else:
        kind = "unknown"
        direction = "unknown"
        evidence = ()
        content = None
    truncated = row_node.get("truncated") == "true"
    if text is not None:
        text_nodes = _unique_nodes(row_node, str(selectors["conversation_text_suffix"]))
        if len(text_nodes) == 1 and text_nodes[0].get("truncated") == "true":
            truncated = True
    stable_hash = None
    stable_source = None
    if message_identity_verified:
        stable_suffix = selectors["conversation_stable_id_suffix"]
        assert isinstance(stable_suffix, str)
        stable_nodes = _nodes(row_node, stable_suffix)
        if len(stable_nodes) == 1:
            raw_id = stable_nodes[0].get("content-desc")
            if is_nonempty_text(raw_id):
                stable_hash = message_stable_identity_hash(raw_id)
                stable_source = "app_message_id"
    repeat_safe = stable_hash is not None
    message_key = (
        message_key_from_stable(conversation_key, stable_hash)
        if repeat_safe and stable_hash is not None
        else None
    )
    content_hash = canonical_hash(content) if kind in {"text", "system_tip"} else None
    return ChatMessage(
        kind=kind,
        direction=direction,
        text=content,
        content_hash=content_hash,
        time_hint=time_hint,
        stable_identity_hash=stable_hash,
        stable_identity_source=stable_source,
        repeat_safe=repeat_safe,
        truncated=truncated,
        direction_evidence=evidence,
        message_key=message_key,
    )


def _direction_for(
    row_node: ElementTree.Element, profile: dict
) -> tuple[str, tuple[str, ...]]:
    """按头像、气泡、我方发送状态三路证据判定方向，冲突或缺失为 unknown。"""
    selectors = profile["selectors"]
    row_bounds = parse_bounds(row_node)
    if row_bounds is None:
        return "unknown", ()
    row_left, _, row_right, _ = row_bounds
    center = (row_left + row_right) / 2
    evidence: list[str] = []
    directions: set[str] = set()
    for name, suffix, evidence_name in (
        ("avatar", str(selectors["conversation_avatar_suffix"]), "avatar"),
        ("bubble", str(selectors["conversation_bubble_suffix"]), "bubble"),
    ):
        node = _unique_node(row_node, suffix)
        if node is None:
            continue
        bounds = parse_bounds(node)
        if bounds is None:
            continue
        left, _, right, _ = bounds
        node_center = (left + right) / 2
        if node_center == center:
            continue
        evidence.append(evidence_name)
        directions.add("incoming" if node_center < center else "outgoing")
    status_node = _unique_node(row_node, str(selectors["conversation_status_suffix"]))
    if status_node is not None and is_nonempty_text(status_node.get("text")):
        evidence.append("sent_status")
        directions.add("outgoing")
    evidence_tuple = tuple(
        name for name in _DIRECTION_NAMES if name in evidence
    )
    if len(directions) == 1:
        return next(iter(directions)), evidence_tuple
    if len(directions) > 1:
        return "unknown", evidence_tuple
    return "unknown", evidence_tuple


def _clear_duplicate_stable_ids(
    messages: tuple[ChatMessage, ...]
) -> tuple[ChatMessage, ...]:
    """清除非边界重复的稳定身份，避免把不同消息合并成可发送键。"""
    counts: dict[str, int] = {}
    for message in messages:
        if message.stable_identity_hash is not None:
            counts[message.stable_identity_hash] = (
                counts.get(message.stable_identity_hash, 0) + 1
            )
    result: list[ChatMessage] = []
    for message in messages:
        if (
            message.stable_identity_hash is not None
            and counts[message.stable_identity_hash] > 1
        ):
            result.append(
                replace(
                    message,
                    stable_identity_hash=None,
                    stable_identity_source=None,
                    repeat_safe=False,
                    message_key=None,
                )
            )
        else:
            result.append(message)
    return tuple(result)


def _conversation_overlap(
    previous: tuple[ChatMessage, ...], current: tuple[ChatMessage, ...]
) -> int:
    """只取边界上稳定身份与上下文完全一致的连续最长重叠。"""
    limit = min(len(previous), len(current))
    for length in range(limit, 0, -1):
        if all(
            _messages_match(previous[-length + index], current[index])
            for index in range(length)
        ):
            return length
    return 0


def _messages_match(left: ChatMessage, right: ChatMessage) -> bool:
    """判断两行是否具有同一稳定身份且内容/方向/时间上下文一致。"""
    return (
        left.stable_identity_hash is not None
        and left.stable_identity_hash == right.stable_identity_hash
        and left.kind == right.kind
        and left.direction == right.direction
        and left.content_hash == right.content_hash
        and left.time_hint == right.time_hint
        and left.truncated == right.truncated
    )


def _unread_evidence(rows: tuple[UnreadRowRead, ...]) -> FieldEvidence:
    """按实际行结果重算未读字段证据，避免与行状态矛盾。"""
    unknown_reasons = [
        row.reason_code
        for row in rows
        if row.state == "unknown" and row.reason_code is not None
    ]
    used_pixel = any(row.source == "pixel" for row in rows)
    source = "pixel" if used_pixel else "hierarchy"
    confidence = "medium" if used_pixel else "high"
    if unknown_reasons:
        reason = next(
            (name for name in FIELD_REASON_PRIORITY if name in unknown_reasons),
            "ambiguous_signal",
        )
        return FieldEvidence(
            field="unread_badges",
            source=source,
            confidence="low",
            state="unknown",
            reason_code=reason,
        )
    return FieldEvidence("unread_badges", source, confidence, "ok", None)


def _field_context(
    profile: dict, snapshot_id: str, observed_at: str
) -> dict[str, str]:
    """构造层级/像素字段共享的 app/profile/版式绑定上下文。"""
    selectors = profile["selectors"]
    return {
        "app": APP,
        "profile_id": str(profile["profile_id"]),
        "profile_version": str(profile["version"]),
        "snapshot_id": snapshot_id,
        "observed_at": observed_at,
        "layout_context": canonical_hash(selectors),
    }


def _validate_coverage_proof(value: object) -> dict[str, object]:
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


def _validate_context(value: object) -> dict[str, str] | None:
    """校验合成会话上下文的固定键、文本与来源快照 UUID。"""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != CONTEXT_KEYS:
        raise ValueError("conversation context is invalid")
    for name in ("contact", "company", "position"):
        if not is_nonempty_text(value[name]):
            raise ValueError("conversation context text is invalid")
    if not is_uuid(value["source_snapshot_id"]):
        raise ValueError("conversation context snapshot is invalid")
    return {
        "contact": str(value["contact"]),
        "company": str(value["company"]),
        "position": str(value["position"]),
        "source_snapshot_id": str(value["source_snapshot_id"]),
    }


def _require_text_list(value: object, *, minimum: int) -> None:
    """要求去重非空字符串数组且达到最小长度。"""
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or any(not is_nonempty_text(item) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("text list is invalid")


def _text_set(root: ElementTree.Element) -> set[str]:
    """收集层级中所有非空文本，仅用于页面信号匹配。"""
    return {
        text
        for node in root.iter()
        if isinstance((text := node.get("text")), str) and text != ""
    }


def _matches_any(text_set: set[str], markers: list[str]) -> bool:
    """判断文本集合是否命中任一精确标记。"""
    return any(marker in text_set for marker in markers)


def _nodes(root: ElementTree.Element, suffix: str) -> list[ElementTree.Element]:
    """返回子树内匹配资源 ID 后缀的全部节点。"""
    return [node for node in root.iter() if rid_matches(node, suffix)]


def _unique_node(root: ElementTree.Element, suffix: str) -> ElementTree.Element | None:
    """要求匹配后缀的节点唯一，否则返回 None。"""
    nodes = _nodes(root, suffix)
    return nodes[0] if len(nodes) == 1 else None


def _unique_nodes(root: ElementTree.Element, suffix: str) -> list[ElementTree.Element]:
    """返回匹配后缀的节点列表，供截断属性判断。"""
    return _nodes(root, suffix)


def _unique_text(root: ElementTree.Element, suffix: str) -> str | None:
    """只接受唯一节点的原样非空 text，缺失或重复返回 None。"""
    node = _unique_node(root, suffix)
    if node is None:
        return None
    text = node.get("text")
    return text if isinstance(text, str) and text != "" else None


def _has_any_suffix(root: ElementTree.Element, suffixes: list[str]) -> bool:
    """判断子树中是否存在任一后缀节点。"""
    return any(_nodes(root, suffix) for suffix in suffixes)


def _unique_nodes_any(
    root: ElementTree.Element, suffixes: list[str]
) -> list[ElementTree.Element]:
    """返回任一后缀匹配的节点列表。"""
    found: list[ElementTree.Element] = []
    for suffix in suffixes:
        found.extend(_nodes(root, suffix))
    return found


def _row_nodes(
    list_node: ElementTree.Element, suffix: str
) -> list[ElementTree.Element] | None:
    """按文档顺序取直属行；嵌套行返回 None 表示不完整。"""
    nodes = [
        node
        for node in list_node.iter()
        if node is not list_node and rid_matches(node, suffix)
    ]
    matched = set(nodes)
    for node in nodes:
        parent = _parent_of(list_node, node)
        while parent is not None and parent is not list_node:
            if parent in matched:
                return None
            parent = _parent_of(list_node, parent)
    return nodes


def _parent_of(
    root: ElementTree.Element, target: ElementTree.Element
) -> ElementTree.Element | None:
    """在给定子树中查找 target 的直接父节点。"""
    for element in root.iter():
        for child in list(element):
            if child is target:
                return element
    return None


def _description_incomplete(text: str) -> bool:
    """按 JOBS-P06 判断正文截断，不把截断文本交给模型补全。"""
    return "查看更多" in text or text.endswith("…") or text.endswith("...")


def _is_hr_role(subtitle: str) -> bool:
    """按 tv_sub_title 词表判断招聘者角色，未知保持 unknown。"""
    lowered = subtitle.lower()
    return any(term in lowered for term in _HR_ROLE_TERMS)


def _ok(value: object, snapshot_id: str | None, observed_at: str) -> ReadResult:
    """构造结构确定的高置信成功结果。"""
    result = ReadResult("ok", value, None, snapshot_id, observed_at, _FIXTURE_SOURCE, "high")
    validate_read_result(result)
    return result


def _failed(reason_code: str, snapshot_id: str | None, observed_at: str) -> ReadResult:
    """构造字段完整的失败结果，不携带部分值。"""
    return ReadResult("failed", None, reason_code, snapshot_id, observed_at, _FIXTURE_SOURCE, "low")


def _undetermined(
    reason_code: str, snapshot_id: str | None, observed_at: str
) -> ReadResult:
    """构造字段完整的未决结果，不携带部分值。"""
    return ReadResult(
        "undetermined", None, reason_code, snapshot_id, observed_at, _FIXTURE_SOURCE, "low"
    )


def _safe_now(value: object) -> str:
    """把无效时间替换为固定纪元值，仅用于失败结果。"""
    try:
        parse_utc(value)
    except ValueError:
        return EPOCH
    return str(value)
