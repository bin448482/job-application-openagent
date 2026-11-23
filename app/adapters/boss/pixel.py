"""实现 M3 合成 RGBA 截图头校验与区域角标像素纯解析。"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from app.adapters.boss.support import validate_ratio_region
from app.domain.ladder import FieldReading
from app.domain.models import APP, is_hash, is_nonempty_text, is_uuid, parse_utc

CALIBRATION_KEYS = frozenset(
    {
        "calibration_id",
        "app",
        "profile_id",
        "version",
        "calibrated",
        "absent_state_verified",
        "resolution",
        "badge_region",
        "target_color",
        "tolerance",
        "threshold_pixels",
        "source",
        "calibrated_on",
    }
)
HEADER_SIZE = 16
SUPPORTED_PIXEL_FORMATS = frozenset({1})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ScreenHeader:
    """保存严格解析后的合成 RGBA 头字段。"""

    width: int
    height: int
    pixel_format: int
    colorspace: int


def parse_screen_header(raw: bytes) -> ScreenHeader:
    """解析 16 字节小端头并拒绝不支持格式或尺寸/长度不一致。"""
    if not isinstance(raw, bytes) or len(raw) < HEADER_SIZE:
        raise ValueError("screen header is invalid")
    width, height, pixel_format, colorspace = struct.unpack("<IIII", raw[:HEADER_SIZE])
    if width <= 0 or height <= 0:
        raise ValueError("screen size is invalid")
    if pixel_format not in SUPPORTED_PIXEL_FORMATS:
        raise ValueError("screen format is unsupported")
    expected = width * height * 4
    if len(raw) - HEADER_SIZE != expected:
        raise ValueError("screen length is invalid")
    return ScreenHeader(width, height, pixel_format, colorspace)


def validate_calibration(calibration: object) -> dict[str, object]:
    """严格校验合成像素标定固定键、类型、分辨率、区域、颜色与日期。"""
    if not isinstance(calibration, dict) or set(calibration) != CALIBRATION_KEYS:
        raise ValueError("calibration keys are invalid")
    for name in ("calibration_id", "profile_id", "version", "source"):
        if not is_nonempty_text(calibration[name]):
            raise ValueError("calibration text is invalid")
    if calibration["app"] != APP:
        raise ValueError("calibration app is invalid")
    if type(calibration["calibrated"]) is not bool:
        raise ValueError("calibration flag is invalid")
    if type(calibration["absent_state_verified"]) is not bool:
        raise ValueError("calibration absent flag is invalid")
    resolution = calibration["resolution"]
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(type(value) is not int or value <= 0 for value in resolution)
    ):
        raise ValueError("calibration resolution is invalid")
    region = validate_ratio_region(calibration["badge_region"])
    color = calibration["target_color"]
    if (
        not isinstance(color, list)
        or len(color) != 3
        or any(type(value) is not int or value < 0 or value > 255 for value in color)
    ):
        raise ValueError("calibration color is invalid")
    tolerance = calibration["tolerance"]
    if type(tolerance) is not int or tolerance < 0 or tolerance > 255:
        raise ValueError("calibration tolerance is invalid")
    threshold = calibration["threshold_pixels"]
    if type(threshold) is not int or threshold <= 0:
        raise ValueError("calibration threshold is invalid")
    calibrated_on = calibration["calibrated_on"]
    if not isinstance(calibrated_on, str) or _DATE_RE.fullmatch(calibrated_on) is None:
        raise ValueError("calibration date is invalid")
    return {
        "calibration_id": str(calibration["calibration_id"]),
        "app": APP,
        "profile_id": str(calibration["profile_id"]),
        "version": str(calibration["version"]),
        "calibrated": calibration["calibrated"],
        "absent_state_verified": calibration["absent_state_verified"],
        "resolution": [int(resolution[0]), int(resolution[1])],
        "badge_region": region,
        "target_color": [int(value) for value in color],
        "tolerance": tolerance,
        "threshold_pixels": threshold,
        "source": str(calibration["source"]),
        "calibrated_on": calibrated_on,
    }


def parse_pixel_row_badge(
    raw: bytes,
    calibration: object,
    row_bounds: tuple[int, int, int, int] | None,
    *,
    app: str,
    profile_id: str,
    profile_version: str,
    snapshot_id: str,
    observed_at: str,
    layout_context: str,
) -> FieldReading:
    """在已标定行区域统计强像素，返回 present/absent/unknown/failed 单级结论。"""
    if (
        app != APP
        or not is_nonempty_text(profile_id)
        or not is_nonempty_text(profile_version)
        or not is_uuid(snapshot_id)
        or not is_hash(layout_context)
    ):
        raise ValueError("pixel context is invalid")
    parse_utc(observed_at)
    normalized = validate_calibration(calibration)

    def reading(state: str, count: int | None, reason: str | None) -> FieldReading:
        """用同一绑定上下文构造像素字段结论。"""
        return FieldReading(
            field="unread_badges",
            app=app,
            profile_id=profile_id,
            profile_version=profile_version,
            snapshot_id=snapshot_id,
            observed_at=observed_at,
            layout_context=layout_context,
            source="pixel",
            confidence="medium" if state in {"present", "absent"} else "low",
            state=state,
            count=count,
            reason_code=reason,
        )

    if not normalized["calibrated"]:
        return reading("unknown", None, "uncalibrated_region")
    if (
        normalized["profile_id"] != profile_id
        or normalized["version"] != profile_version
    ):
        return reading("unknown", None, "calibration_mismatch")
    try:
        header = parse_screen_header(raw)
    except ValueError:
        return reading("failed", None, "invalid_fixture")
    expected_width, expected_height = normalized["resolution"]
    if (header.width, header.height) != (expected_width, expected_height):
        return reading("unknown", None, "calibration_mismatch")
    if row_bounds is None:
        return reading("unknown", None, "uncalibrated_region")
    row_left, row_top, row_right, row_bottom = row_bounds
    row_width = row_right - row_left
    row_height = row_bottom - row_top
    if row_width <= 0 or row_height <= 0:
        return reading("unknown", None, "uncalibrated_region")
    region = normalized["badge_region"]
    try:
        left = row_left + float(region[0]) * row_width
        top = row_top + float(region[1]) * row_height
        right = row_left + float(region[2]) * row_width
        bottom = row_top + float(region[3]) * row_height
    except (OverflowError, ValueError):
        return reading("unknown", None, "uncalibrated_region")
    if left < 0 or top < 0 or right > header.width or bottom > header.height:
        return reading("unknown", None, "uncalibrated_region")
    target = normalized["target_color"]
    tolerance = normalized["tolerance"]
    threshold = normalized["threshold_pixels"]
    left_int = max(0, int(left))
    top_int = max(0, int(top))
    right_int = min(header.width, int(right + 0.999999))
    bottom_int = min(header.height, int(bottom + 0.999999))
    if right_int <= left_int or bottom_int <= top_int:
        return reading("unknown", None, "uncalibrated_region")

    pixels = raw[HEADER_SIZE:]
    strong = 0
    for y in range(top_int, bottom_int):
        row_start = y * header.width * 4
        for x in range(left_int, right_int):
            offset = row_start + x * 4
            red, green, blue = pixels[offset], pixels[offset + 1], pixels[offset + 2]
            if (
                abs(red - target[0]) <= tolerance
                and abs(green - target[1]) <= tolerance
                and abs(blue - target[2]) <= tolerance
            ):
                strong += 1
    if strong >= threshold and strong > 0:
        return reading("present", None, None)
    if normalized["absent_state_verified"] is True:
        return reading("absent", 0, None)
    return reading("unknown", None, "uncalibrated_region")
