from __future__ import annotations

import argparse
import json
import math
import struct
import threading
import webbrowser
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import camera_gphoto2


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_TMP_DIR = PROJECT_ROOT / "tmp"
LOCATION_TMP_DIR = PROJECT_TMP_DIR / "location-ui"
CONFIG_LOCATION_DIR = PROJECT_ROOT / "config" / "location"

DEFAULT_TARGET_MAX = 49152
DEFAULT_MAX_EXPOSURE_TRIALS = 3
DEFAULT_ISO = "100"
DEFAULT_APERTURE = "4"
DEFAULT_MIN_SHUTTER_SPEED = "1/8000"
DEFAULT_MAX_SHUTTER_SPEED = "30"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
DEFAULT_AUTO_DETECT_INSET_RATIO = 0.08
COLORCHECKER_CLASSIC_ROWS = 4
COLORCHECKER_CLASSIC_COLS = 6
COLORCHECKER_CLASSIC_COUNT = COLORCHECKER_CLASSIC_ROWS * COLORCHECKER_CLASSIC_COLS
SUPPORTED_COLORCHECKER_ORIENTATIONS = (0, 180)

COLORCHECKER_CLASSIC_24_PATCHES = (
    {"standard_index": 1, "name": "dark_skin", "label": "Skin D", "roles": ["skin", "memory_color"]},
    {"standard_index": 2, "name": "light_skin", "label": "Skin L", "roles": ["skin", "memory_color"]},
    {"standard_index": 3, "name": "blue_sky", "label": "Sky", "roles": ["memory_color"]},
    {"standard_index": 4, "name": "foliage", "label": "Foliage", "roles": ["memory_color"]},
    {"standard_index": 5, "name": "blue_flower", "label": "Flower", "roles": ["memory_color"]},
    {"standard_index": 6, "name": "bluish_green", "label": "BG", "roles": ["color"]},
    {"standard_index": 7, "name": "orange", "label": "Orange", "roles": ["color"]},
    {"standard_index": 8, "name": "purplish_blue", "label": "PB", "roles": ["color"]},
    {"standard_index": 9, "name": "moderate_red", "label": "MR", "roles": ["color"]},
    {"standard_index": 10, "name": "purple", "label": "Purple", "roles": ["color"]},
    {"standard_index": 11, "name": "yellow_green", "label": "YG", "roles": ["color"]},
    {"standard_index": 12, "name": "orange_yellow", "label": "OY", "roles": ["color"]},
    {"standard_index": 13, "name": "blue", "label": "B", "roles": ["rgbcmy", "hue_anchor", "blue"]},
    {"standard_index": 14, "name": "green", "label": "G", "roles": ["rgbcmy", "hue_anchor", "green"]},
    {"standard_index": 15, "name": "red", "label": "R", "roles": ["rgbcmy", "hue_anchor", "red"]},
    {"standard_index": 16, "name": "yellow", "label": "Y", "roles": ["rgbcmy", "hue_anchor", "yellow"]},
    {"standard_index": 17, "name": "magenta", "label": "M", "roles": ["rgbcmy", "hue_anchor", "magenta"]},
    {"standard_index": 18, "name": "cyan", "label": "C", "roles": ["rgbcmy", "hue_anchor", "cyan"]},
    {"standard_index": 19, "name": "white", "label": "W", "roles": ["neutral", "white", "exposure"]},
    {"standard_index": 20, "name": "neutral_8", "label": "N8", "roles": ["neutral", "gray"]},
    {"standard_index": 21, "name": "neutral_6_5", "label": "N6.5", "roles": ["neutral", "gray"]},
    {"standard_index": 22, "name": "neutral_5", "label": "N5", "roles": ["neutral", "gray"]},
    {"standard_index": 23, "name": "neutral_3_5", "label": "N3.5", "roles": ["neutral", "gray"]},
    {"standard_index": 24, "name": "black", "label": "K", "roles": ["neutral", "black", "ambient_check"]},
)

Point = tuple[float, float]
Quad = list[Point]


@dataclass(frozen=True)
class ProjectionSegment:
    start: int
    end: int
    score: float

    @property
    def center(self) -> float:
        return (self.start + self.end) / 2.0


@dataclass(frozen=True)
class AxisGridEstimate:
    luma: Any
    dark: Any
    chart_bbox: tuple[int, int, int, int]
    row_segments: list[ProjectionSegment]
    col_segments: list[ProjectionSegment]


def linear_rgb_to_preview_uint8(image: Any, *, white_point: int = DEFAULT_TARGET_MAX) -> Any:
    np = _import_numpy()
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"expected HxWx3 image, got shape {image.shape}")
    if white_point <= 0:
        raise ValueError("white_point must be positive")
    normalized = np.clip(image.astype(np.float32) / float(white_point), 0.0, 1.0)
    return (normalized * 255.0 + 0.5).astype(np.uint8)


def rgb_to_png_data(image: Any) -> bytes:
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"expected HxWx3 image, got shape {image.shape}")
    np = _import_numpy()
    if image.dtype != np.uint8:
        raise ValueError(f"expected uint8 image, got dtype {image.dtype}")
    height, width = image.shape[:2]
    raw_rows = b"".join(b"\x00" + image[row].tobytes() for row in range(height))
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw_rows, level=6)),
            _png_chunk(b"IEND", b""),
        ]
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def detect_color_checker_quads(
    image: Any,
    *,
    rows: int,
    cols: int,
    inset_ratio: float = DEFAULT_AUTO_DETECT_INSET_RATIO,
) -> list[Quad]:
    if rows <= 0 or cols <= 0:
        raise ValueError("rows and cols must be positive")
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"expected HxWx3 image, got shape {image.shape}")

    try:
        return _detect_color_checker_quads_mcc(image, rows=rows, cols=cols)
    except RuntimeError:
        pass

    estimate = _estimate_axis_grid(image, rows=rows, cols=cols)
    try:
        return _detect_color_checker_quads_opencv(estimate, rows=rows, cols=cols, inset_ratio=inset_ratio)
    except RuntimeError:
        return _detect_color_checker_quads_projection(estimate, rows=rows, cols=cols, inset_ratio=inset_ratio)


def _detect_color_checker_quads_mcc(image: Any, *, rows: int, cols: int) -> list[Quad]:
    if rows != 4 or cols != 6:
        raise RuntimeError("OpenCV mcc supports the 4x6 MCC24 chart")
    cv2 = _import_cv2()
    if not hasattr(cv2, "mcc"):
        raise RuntimeError("OpenCV mcc module is not available")
    np = _import_numpy()
    detector = cv2.mcc.CCheckerDetector_create()
    bgr_image = np.ascontiguousarray(image[..., ::-1])
    if not detector.process(bgr_image, cv2.mcc.MCC24, 1, False):
        raise RuntimeError("OpenCV mcc did not detect a Macbeth 24 chart")
    checker = detector.getBestColorChecker()
    if checker is None:
        checkers = detector.getListColorChecker()
        if not checkers:
            raise RuntimeError("OpenCV mcc returned no color checker")
        checker = checkers[0]

    points = np.asarray(checker.getColorCharts(), dtype=np.float64).reshape((-1, 4, 2))
    if points.shape[0] != rows * cols:
        raise RuntimeError(f"OpenCV mcc returned {points.shape[0]} cells, expected {rows * cols}")
    quads = [[(float(x), float(y)) for x, y in _order_quad_points(cell.tolist())] for cell in points]
    if not _mcc_quads_are_row_major(quads, rows=rows, cols=cols):
        raise RuntimeError("OpenCV mcc returned an unexpected chart orientation")
    return quads


def _mcc_quads_are_row_major(quads: list[Quad], *, rows: int, cols: int) -> bool:
    centers = [(sum(point[0] for point in quad) / 4.0, sum(point[1] for point in quad) / 4.0) for quad in quads]
    row_centers = [
        sum(centers[row * cols + col][1] for col in range(cols)) / cols
        for row in range(rows)
    ]
    if any(row_centers[index] >= row_centers[index + 1] for index in range(rows - 1)):
        return False
    for row in range(rows):
        xs = [centers[row * cols + col][0] for col in range(cols)]
        if any(xs[index] >= xs[index + 1] for index in range(cols - 1)):
            return False
    return True


def _estimate_axis_grid(image: Any, *, rows: int, cols: int) -> AxisGridEstimate:
    luma = _rgb_luma(image)
    dark_threshold = _chart_dark_threshold(luma)
    dark = luma <= dark_threshold
    chart_bbox = _mask_bbox(dark)
    if chart_bbox is None:
        raise ValueError("could not find dark chart grid pixels")

    x0, y0, x1, y1 = chart_bbox
    crop_luma = luma[y0 : y1 + 1, x0 : x1 + 1]
    content_threshold = _chart_content_threshold(luma, dark_threshold)
    content = crop_luma > content_threshold

    row_segments = _select_regular_segments(_projection_segments(content.mean(axis=1)), rows)
    col_segments = _select_regular_segments(_projection_segments(content.mean(axis=0)), cols)

    row_segments = [ProjectionSegment(segment.start + y0, segment.end + y0, segment.score) for segment in row_segments]
    col_segments = [ProjectionSegment(segment.start + x0, segment.end + x0, segment.score) for segment in col_segments]
    return AxisGridEstimate(
        luma=luma,
        dark=dark,
        chart_bbox=chart_bbox,
        row_segments=row_segments,
        col_segments=col_segments,
    )


def _detect_color_checker_quads_projection(
    estimate: AxisGridEstimate,
    *,
    rows: int,
    cols: int,
    inset_ratio: float,
) -> list[Quad]:
    source_rect = _axis_grid_source_rect(estimate.row_segments, estimate.col_segments)
    grid_quad = _fit_dark_grid_quad(estimate.dark, estimate.chart_bbox, source_rect)

    quads: list[Quad] = []
    for row_index in range(rows):
        row_segment = estimate.row_segments[row_index]
        top = row_segment.start
        bottom = row_segment.end
        for col_index in range(cols):
            col_segment = estimate.col_segments[col_index]
            left = col_segment.start
            right = col_segment.end
            inset_x = max(0.0, (right - left) * inset_ratio)
            inset_y = max(0.0, (bottom - top) * inset_ratio)
            quads.append(
                [
                    _map_rect_point_to_quad((left + inset_x, top + inset_y), source_rect, grid_quad),
                    _map_rect_point_to_quad((right - inset_x, top + inset_y), source_rect, grid_quad),
                    _map_rect_point_to_quad((right - inset_x, bottom - inset_y), source_rect, grid_quad),
                    _map_rect_point_to_quad((left + inset_x, bottom - inset_y), source_rect, grid_quad),
                ]
            )
    return quads


def _detect_color_checker_quads_opencv(
    estimate: AxisGridEstimate,
    *,
    rows: int,
    cols: int,
    inset_ratio: float,
) -> list[Quad]:
    cv2 = _import_cv2()
    horizontal_slope, vertical_slope = _opencv_grid_slopes(cv2, estimate)
    if abs(horizontal_slope) < 1e-9 and abs(vertical_slope) < 1e-9:
        raise RuntimeError("could not detect OpenCV grid angle")

    row_lines, col_lines = _opencv_cell_boundary_lines(
        cv2,
        estimate,
        rows=rows,
        cols=cols,
        horizontal_slope=horizontal_slope,
        vertical_slope=vertical_slope,
    )

    quads: list[Quad] = []
    for row_index in range(rows):
        top_line, bottom_line = row_lines[row_index]
        for col_index in range(cols):
            left_line, right_line = col_lines[col_index]
            quad = [
                _intersect_y_and_x_lines(top_line, left_line),
                _intersect_y_and_x_lines(top_line, right_line),
                _intersect_y_and_x_lines(bottom_line, right_line),
                _intersect_y_and_x_lines(bottom_line, left_line),
            ]
            if not _is_valid_cell_quad(quad, estimate.row_segments[row_index], estimate.col_segments[col_index]):
                raise RuntimeError("OpenCV grid produced invalid cell geometry")
            quads.append(_inset_quad(quad, inset_ratio))
    return quads


def _opencv_cell_boundary_lines(
    cv2: Any,
    estimate: AxisGridEstimate,
    *,
    rows: int,
    cols: int,
    horizontal_slope: float,
    vertical_slope: float,
) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], list[tuple[tuple[float, float], tuple[float, float]]]]:
    source_rect = _axis_grid_source_rect(estimate.row_segments, estimate.col_segments)
    center_x = _rect_center_x(source_rect)
    center_y = _rect_center_y(source_rect)
    row_lines = [
        [
            _shift_y_line((horizontal_slope, 0.0), x=center_x, y=float(segment.start)),
            _shift_y_line((horizontal_slope, 0.0), x=center_x, y=float(segment.end)),
        ]
        for segment in estimate.row_segments
    ]
    col_lines = [
        [
            _shift_x_line((vertical_slope, 0.0), x=float(segment.start), y=center_y),
            _shift_x_line((vertical_slope, 0.0), x=float(segment.end), y=center_y),
        ]
        for segment in estimate.col_segments
    ]

    visible_quads = _opencv_visible_cell_quads(cv2, estimate, rows=rows, cols=cols)
    visible_count = sum(1 for row in visible_quads for quad in row if quad is not None)
    if visible_count < max(4, (rows * cols) // 2):
        return ([(top, bottom) for top, bottom in row_lines], [(left, right) for left, right in col_lines])

    for row_index in range(rows):
        top_points: list[Point] = []
        bottom_points: list[Point] = []
        for col_index in range(cols):
            quad = visible_quads[row_index][col_index]
            if quad is None:
                continue
            top_points.extend([quad[0], quad[1]])
            bottom_points.extend([quad[3], quad[2]])
        if len(top_points) >= 4:
            row_lines[row_index][0] = _fit_y_line(top_points, float(estimate.row_segments[row_index].start))
        if len(bottom_points) >= 4:
            row_lines[row_index][1] = _fit_y_line(bottom_points, float(estimate.row_segments[row_index].end))

    for col_index in range(cols):
        left_points: list[Point] = []
        right_points: list[Point] = []
        for row_index in range(rows):
            quad = visible_quads[row_index][col_index]
            if quad is None:
                continue
            left_points.extend([quad[0], quad[3]])
            right_points.extend([quad[1], quad[2]])
        if len(left_points) >= 4:
            col_lines[col_index][0] = _fit_x_line(left_points, float(estimate.col_segments[col_index].start))
        if len(right_points) >= 4:
            col_lines[col_index][1] = _fit_x_line(right_points, float(estimate.col_segments[col_index].end))

    return ([(top, bottom) for top, bottom in row_lines], [(left, right) for left, right in col_lines])


def _opencv_visible_cell_quads(
    cv2: Any,
    estimate: AxisGridEstimate,
    *,
    rows: int,
    cols: int,
) -> list[list[Quad | None]]:
    np = _import_numpy()
    x0, y0, x1, y1 = estimate.chart_bbox
    gray = estimate.luma.astype(np.uint8)
    crop = gray[y0 : y1 + 1, x0 : x1 + 1]
    content_threshold = _chart_content_threshold(estimate.luma, _chart_dark_threshold(estimate.luma))
    mask = (crop > max(20.0, content_threshold)).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    median_cell_width = float(np.median([segment.end - segment.start + 1 for segment in estimate.col_segments]))
    median_cell_height = float(np.median([segment.end - segment.start + 1 for segment in estimate.row_segments]))
    expected_area = median_cell_width * median_cell_height
    cells: list[list[Quad | None]] = [[None for _ in range(cols)] for _ in range(rows)]
    scores: list[list[float]] = [[-1.0 for _ in range(cols)] for _ in range(rows)]

    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < expected_area * 0.25 or area > expected_area * 1.35:
            continue
        x, y, width, height = cv2.boundingRect(contour)
        if width < median_cell_width * 0.45 or width > median_cell_width * 1.35:
            continue
        if height < median_cell_height * 0.45 or height > median_cell_height * 1.35:
            continue

        center = (x0 + x + width / 2.0, y0 + y + height / 2.0)
        row_index = _nearest_segment_index(center[1], estimate.row_segments)
        col_index = _nearest_segment_index(center[0], estimate.col_segments)
        if row_index is None or col_index is None:
            continue

        shifted = contour + np.array([[[x0, y0]]], dtype=contour.dtype)
        quad = _order_quad_points(cv2.boxPoints(cv2.minAreaRect(shifted)).tolist())
        if not _is_valid_cell_quad(quad, estimate.row_segments[row_index], estimate.col_segments[col_index]):
            continue
        if area <= scores[row_index][col_index]:
            continue
        cells[row_index][col_index] = quad
        scores[row_index][col_index] = area
    return cells


def _nearest_segment_index(value: float, segments: list[ProjectionSegment]) -> int | None:
    distances = [abs(segment.center - value) for segment in segments]
    index = min(range(len(segments)), key=lambda candidate: distances[candidate])
    segment = segments[index]
    tolerance = max((segment.end - segment.start + 1) * 0.85, 20.0)
    if distances[index] > tolerance:
        return None
    return index


def _order_quad_points(points: list[list[float]]) -> Quad:
    candidates = [(float(x), float(y)) for x, y in points]
    top_left = min(candidates, key=lambda point: point[0] + point[1])
    bottom_right = max(candidates, key=lambda point: point[0] + point[1])
    top_right = max(candidates, key=lambda point: point[0] - point[1])
    bottom_left = min(candidates, key=lambda point: point[0] - point[1])
    return [top_left, top_right, bottom_right, bottom_left]


def _axis_grid_source_rect(
    row_segments: list[ProjectionSegment],
    col_segments: list[ProjectionSegment],
) -> tuple[float, float, float, float]:
    return (
        float(col_segments[0].start),
        float(row_segments[0].start),
        float(col_segments[-1].end),
        float(row_segments[-1].end),
    )


def _opencv_grid_slopes(cv2: Any, estimate: AxisGridEstimate) -> tuple[float, float]:
    np = _import_numpy()
    x0, y0, x1, y1 = estimate.chart_bbox
    gray = estimate.luma.astype(np.uint8)
    crop = gray[y0 : y1 + 1, x0 : x1 + 1]
    blur = cv2.GaussianBlur(crop, (5, 5), 0)
    edges = cv2.Canny(blur, 15, 45, apertureSize=3, L2gradient=True)
    min_length = max(120, min(crop.shape[:2]) // 8)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80, minLineLength=min_length, maxLineGap=25)
    if lines is None:
        return (0.0, 0.0)

    horizontal: list[tuple[float, float]] = []
    vertical: list[tuple[float, float]] = []
    for [[x_start, y_start, x_end, y_end]] in lines:
        dx = float(x_end - x_start)
        dy = float(y_end - y_start)
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        angle = math.degrees(math.atan2(dy, dx))
        if abs(angle) <= 20.0 and abs(dx) > 1e-6:
            horizontal.append((dy / dx, length))
        elif abs(abs(angle) - 90.0) <= 20.0 and abs(dy) > 1e-6:
            vertical.append((dx / dy, length))
    return (_weighted_median(horizontal), _weighted_median(vertical))


def _weighted_median(values_and_weights: list[tuple[float, float]]) -> float:
    if not values_and_weights:
        return 0.0
    values_and_weights = sorted(values_and_weights, key=lambda item: item[0])
    total = sum(weight for _value, weight in values_and_weights)
    midpoint = total / 2.0
    cumulative = 0.0
    for value, weight in values_and_weights:
        cumulative += weight
        if cumulative >= midpoint:
            return float(value)
    return float(values_and_weights[-1][0])


def _rect_center_x(rect: tuple[float, float, float, float]) -> float:
    return (rect[0] + rect[2]) / 2.0


def _rect_center_y(rect: tuple[float, float, float, float]) -> float:
    return (rect[1] + rect[3]) / 2.0


def _is_valid_cell_quad(quad: Quad, row_segment: ProjectionSegment, col_segment: ProjectionSegment) -> bool:
    expected_area = (row_segment.end - row_segment.start + 1) * (col_segment.end - col_segment.start + 1)
    area = abs(_polygon_signed_area(quad))
    if area < expected_area * 0.4 or area > expected_area * 1.8:
        return False
    center_x = sum(point[0] for point in quad) / 4.0
    center_y = sum(point[1] for point in quad) / 4.0
    return (
        col_segment.start - 30 <= center_x <= col_segment.end + 30
        and row_segment.start - 30 <= center_y <= row_segment.end + 30
    )


def _inset_quad(quad: Quad, ratio: float) -> Quad:
    if ratio <= 0:
        return quad
    center_x = sum(point[0] for point in quad) / len(quad)
    center_y = sum(point[1] for point in quad) / len(quad)
    scale = max(0.0, min(1.0, 1.0 - ratio * 2.0))
    return [
        (center_x + (point[0] - center_x) * scale, center_y + (point[1] - center_y) * scale)
        for point in quad
    ]


def _import_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for OpenCV block detection") from exc
    return cv2


def _rgb_luma(image: Any) -> Any:
    return (
        image[..., 0].astype("float32") * 0.2126
        + image[..., 1].astype("float32") * 0.7152
        + image[..., 2].astype("float32") * 0.0722
    )


def _chart_dark_threshold(luma: Any) -> float:
    np = _import_numpy()
    p05, p50 = np.percentile(luma, [5, 50])
    return min(40.0, max(8.0, float(p05 + (p50 - p05) * 0.2)))


def _chart_content_threshold(luma: Any, dark_threshold: float) -> float:
    np = _import_numpy()
    p50 = float(np.percentile(luma, 50))
    return min(35.0, dark_threshold + max(4.0, (p50 - dark_threshold) * 0.2))


def _mask_bbox(mask: Any) -> tuple[int, int, int, int] | None:
    np = _import_numpy()
    ys, xs = np.nonzero(mask)
    if xs.size == 0 or ys.size == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def _fit_dark_grid_quad(
    dark: Any,
    chart_bbox: tuple[int, int, int, int],
    source_rect: tuple[float, float, float, float],
) -> Quad:
    chart_left, chart_top, chart_right, chart_bottom = chart_bbox
    source_left, source_top, source_right, source_bottom = source_rect
    source_center_x = (source_left + source_right) / 2.0
    source_center_y = (source_top + source_bottom) / 2.0
    fallback_quad = [
        (source_left, source_top),
        (source_right, source_top),
        (source_right, source_bottom),
        (source_left, source_bottom),
    ]

    top_line = _shift_y_line(
        _fit_y_line(
            _dark_edge_points_y(
                dark,
                x_start=source_left,
                x_end=source_right,
                y_start=chart_top,
                y_end=source_top,
                use_min=True,
            ),
            fallback_y=source_top,
        ),
        x=source_center_x,
        y=source_top,
    )
    bottom_line = _shift_y_line(
        _fit_y_line(
            _dark_edge_points_y(
                dark,
                x_start=source_left,
                x_end=source_right,
                y_start=source_bottom,
                y_end=chart_bottom,
                use_min=False,
            ),
            fallback_y=source_bottom,
        ),
        x=source_center_x,
        y=source_bottom,
    )
    left_line = _shift_x_line(
        _fit_x_line(
            _dark_edge_points_x(
                dark,
                x_start=chart_left,
                x_end=source_left,
                y_start=source_top,
                y_end=source_bottom,
                use_min=True,
            ),
            fallback_x=source_left,
        ),
        x=source_left,
        y=source_center_y,
    )
    right_line = _shift_x_line(
        _fit_x_line(
            _dark_edge_points_x(
                dark,
                x_start=source_right,
                x_end=chart_right,
                y_start=source_top,
                y_end=source_bottom,
                use_min=False,
            ),
            fallback_x=source_right,
        ),
        x=source_right,
        y=source_center_y,
    )

    quad = [
        _intersect_y_and_x_lines(top_line, left_line),
        _intersect_y_and_x_lines(top_line, right_line),
        _intersect_y_and_x_lines(bottom_line, right_line),
        _intersect_y_and_x_lines(bottom_line, left_line),
    ]
    if not _is_reasonable_grid_quad(quad, source_rect):
        return fallback_quad
    return quad


def _dark_edge_points_y(
    dark: Any,
    *,
    x_start: float,
    x_end: float,
    y_start: float,
    y_end: float,
    use_min: bool,
) -> list[Point]:
    np = _import_numpy()
    left = max(0, int(math.floor(min(x_start, x_end))))
    right = min(dark.shape[1] - 1, int(math.ceil(max(x_start, x_end))))
    top = max(0, int(math.floor(min(y_start, y_end))))
    bottom = min(dark.shape[0] - 1, int(math.ceil(max(y_start, y_end))))
    if right < left or bottom < top:
        return []
    stride = max(1, (right - left + 1) // 500)
    points: list[Point] = []
    for x in range(left, right + 1, stride):
        ys = np.flatnonzero(dark[top : bottom + 1, x])
        if ys.size == 0:
            continue
        y = ys[0] if use_min else ys[-1]
        points.append((float(x), float(top + int(y))))
    return points


def _dark_edge_points_x(
    dark: Any,
    *,
    x_start: float,
    x_end: float,
    y_start: float,
    y_end: float,
    use_min: bool,
) -> list[Point]:
    np = _import_numpy()
    left = max(0, int(math.floor(min(x_start, x_end))))
    right = min(dark.shape[1] - 1, int(math.ceil(max(x_start, x_end))))
    top = max(0, int(math.floor(min(y_start, y_end))))
    bottom = min(dark.shape[0] - 1, int(math.ceil(max(y_start, y_end))))
    if right < left or bottom < top:
        return []
    stride = max(1, (bottom - top + 1) // 500)
    points: list[Point] = []
    for y in range(top, bottom + 1, stride):
        xs = np.flatnonzero(dark[y, left : right + 1])
        if xs.size == 0:
            continue
        x = xs[0] if use_min else xs[-1]
        points.append((float(left + int(x)), float(y)))
    return points


def _fit_y_line(points: list[Point], fallback_y: float) -> tuple[float, float]:
    np = _import_numpy()
    if len(points) < 2:
        return (0.0, float(fallback_y))
    xs = np.asarray([point[0] for point in points], dtype=np.float64)
    ys = np.asarray([point[1] for point in points], dtype=np.float64)
    slope, intercept = _robust_polyfit(xs, ys, fallback=(0.0, float(fallback_y)))
    return (float(slope), float(intercept))


def _fit_x_line(points: list[Point], fallback_x: float) -> tuple[float, float]:
    np = _import_numpy()
    if len(points) < 2:
        return (0.0, float(fallback_x))
    xs = np.asarray([point[0] for point in points], dtype=np.float64)
    ys = np.asarray([point[1] for point in points], dtype=np.float64)
    slope, intercept = _robust_polyfit(ys, xs, fallback=(0.0, float(fallback_x)))
    return (float(slope), float(intercept))


def _robust_polyfit(x_values: Any, y_values: Any, *, fallback: tuple[float, float]) -> tuple[float, float]:
    np = _import_numpy()
    keep = np.ones(x_values.shape, dtype=bool)
    slope, intercept = fallback
    for _ in range(3):
        if int(keep.sum()) < 2:
            return fallback
        slope, intercept = np.polyfit(x_values[keep], y_values[keep], 1)
        residuals = y_values - (slope * x_values + intercept)
        kept_residuals = residuals[keep]
        median = float(np.median(kept_residuals))
        mad = float(np.median(np.abs(kept_residuals - median)))
        tolerance = max(4.0, mad * 3.5)
        keep = np.abs(residuals - median) <= tolerance
    return (float(slope), float(intercept))


def _shift_y_line(line: tuple[float, float], *, x: float, y: float) -> tuple[float, float]:
    slope, _intercept = line
    return (slope, y - slope * x)


def _shift_x_line(line: tuple[float, float], *, x: float, y: float) -> tuple[float, float]:
    slope, _intercept = line
    return (slope, x - slope * y)


def _intersect_y_and_x_lines(y_line: tuple[float, float], x_line: tuple[float, float]) -> Point:
    y_slope, y_intercept = y_line
    x_slope, x_intercept = x_line
    denominator = 1.0 - x_slope * y_slope
    if abs(denominator) < 1e-6:
        y = y_slope * x_intercept + y_intercept
        return (float(x_intercept), float(y))
    x = (x_slope * y_intercept + x_intercept) / denominator
    y = y_slope * x + y_intercept
    return (float(x), float(y))


def _is_reasonable_grid_quad(quad: Quad, source_rect: tuple[float, float, float, float]) -> bool:
    source_left, source_top, source_right, source_bottom = source_rect
    source_width = source_right - source_left
    source_height = source_bottom - source_top
    if source_width <= 0 or source_height <= 0:
        return False
    if not all(math.isfinite(x) and math.isfinite(y) for x, y in quad):
        return False
    xs = [point[0] for point in quad]
    ys = [point[1] for point in quad]
    if max(xs) - min(xs) < source_width * 0.7 or max(ys) - min(ys) < source_height * 0.7:
        return False
    if max(xs) - min(xs) > source_width * 1.3 or max(ys) - min(ys) > source_height * 1.3:
        return False
    area = abs(_polygon_signed_area(quad))
    source_area = source_width * source_height
    return source_area * 0.6 <= area <= source_area * 1.4


def _polygon_signed_area(polygon: list[Point]) -> float:
    area = 0.0
    for index, point in enumerate(polygon):
        next_point = polygon[(index + 1) % len(polygon)]
        area += point[0] * next_point[1] - next_point[0] * point[1]
    return area / 2.0


def _map_rect_point_to_quad(point: Point, source_rect: tuple[float, float, float, float], quad: Quad) -> Point:
    source_left, source_top, source_right, source_bottom = source_rect
    width = source_right - source_left
    height = source_bottom - source_top
    if width <= 0 or height <= 0:
        raise ValueError("detected color-block grid has invalid dimensions")
    u = (point[0] - source_left) / width
    v = (point[1] - source_top) / height
    top_left, top_right, bottom_right, bottom_left = quad
    x = (
        (1 - u) * (1 - v) * top_left[0]
        + u * (1 - v) * top_right[0]
        + u * v * bottom_right[0]
        + (1 - u) * v * bottom_left[0]
    )
    y = (
        (1 - u) * (1 - v) * top_left[1]
        + u * (1 - v) * top_right[1]
        + u * v * bottom_right[1]
        + (1 - u) * v * bottom_left[1]
    )
    return (float(x), float(y))


def _select_regular_segments(segments: list[ProjectionSegment], count: int) -> list[ProjectionSegment]:
    if len(segments) < count:
        raise ValueError(f"expected at least {count} color-block bands, found {len(segments)}")

    best: tuple[float, float, list[ProjectionSegment]] | None = None
    for candidate in _regular_segment_candidates(segments, count):
        centers = [segment.center for segment in candidate]
        gaps = [centers[index + 1] - centers[index] for index in range(len(centers) - 1)]
        mean_gap = sum(gaps) / len(gaps)
        if mean_gap <= 0:
            continue
        variance = sum((gap - mean_gap) ** 2 for gap in gaps) / len(gaps)
        gap_cv = math.sqrt(variance) / mean_gap
        widths = [segment.end - segment.start + 1 for segment in candidate]
        mean_width = sum(widths) / len(widths)
        width_variance = sum((width - mean_width) ** 2 for width in widths) / len(widths)
        width_cv = math.sqrt(width_variance) / mean_width if mean_width > 0 else float("inf")
        mean_score = sum(segment.score for segment in candidate) / len(candidate)
        ranking = (gap_cv + width_cv * 0.6, -mean_score, candidate)
        if best is None or ranking[:2] < best[:2]:
            best = ranking

    if best is None:
        raise ValueError("could not select a regular color-block grid")
    return best[2]


def _regular_segment_candidates(segments: list[ProjectionSegment], count: int) -> list[list[ProjectionSegment]]:
    if len(segments) > 18:
        return [segments[start : start + count] for start in range(0, len(segments) - count + 1)]
    candidates: list[list[ProjectionSegment]] = []
    _collect_regular_segment_candidates(segments, count, 0, [], candidates)
    return candidates


def _collect_regular_segment_candidates(
    segments: list[ProjectionSegment],
    count: int,
    start_index: int,
    current: list[ProjectionSegment],
    candidates: list[list[ProjectionSegment]],
) -> None:
    if len(current) == count:
        candidates.append(list(current))
        return
    remaining = count - len(current)
    for index in range(start_index, len(segments) - remaining + 1):
        current.append(segments[index])
        _collect_regular_segment_candidates(segments, count, index + 1, current, candidates)
        current.pop()


def _projection_segments(projection: Any) -> list[ProjectionSegment]:
    np = _import_numpy()
    values = np.asarray(projection, dtype=np.float32)
    if values.size == 0:
        return []
    smooth_window = max(3, int(round(values.size / 500)))
    if smooth_window % 2 == 0:
        smooth_window += 1
    if smooth_window > 3:
        kernel = np.ones(smooth_window, dtype=np.float32) / smooth_window
        values = np.convolve(values, kernel, mode="same")

    threshold = max(0.05, float(values.max()) * 0.35)
    min_len = max(3, int(round(values.size * 0.01)))
    segments: list[ProjectionSegment] = []
    start: int | None = None
    for index, value in enumerate(values):
        if value >= threshold and start is None:
            start = index
        elif value < threshold and start is not None:
            _append_projection_segment(segments, values, start, index - 1, min_len)
            start = None
    if start is not None:
        _append_projection_segment(segments, values, start, values.size - 1, min_len)
    return segments


def _append_projection_segment(segments: list[ProjectionSegment], values: Any, start: int, end: int, min_len: int) -> None:
    if end - start + 1 < min_len:
        return
    score = float(values[start : end + 1].mean())
    segments.append(ProjectionSegment(start=start, end=end, score=score))


def point_in_polygon(point: Point, polygon: list[Point]) -> bool:
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def distance_to_segment(point: Point, start: Point, end: Point) -> float:
    px, py = point
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    if dx == 0 and dy == 0:
        return math.hypot(px - sx, py - sy)
    t = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest_x = sx + t * dx
    closest_y = sy + t * dy
    return math.hypot(px - closest_x, py - closest_y)


def clamp_point(point: Point, width: int, height: int) -> Point:
    x, y = point
    return (min(max(x, 0.0), float(width - 1)), min(max(y, 0.0), float(height - 1)))


def clamp_quad(quad: Quad, width: int, height: int) -> Quad:
    return [clamp_point(point, width, height) for point in quad]


def translate_quad(quad: Quad, dx: float, dy: float, width: int, height: int) -> Quad:
    min_x = min(point[0] for point in quad)
    max_x = max(point[0] for point in quad)
    min_y = min(point[1] for point in quad)
    max_y = max(point[1] for point in quad)
    if min_x + dx < 0:
        dx = -min_x
    if max_x + dx > width - 1:
        dx = width - 1 - max_x
    if min_y + dy < 0:
        dy = -min_y
    if max_y + dy > height - 1:
        dy = height - 1 - max_y
    return [(x + dx, y + dy) for x, y in quad]


def quad_to_json(quad: Quad) -> list[dict[str, float]]:
    return [{"x": round(float(x), 3), "y": round(float(y), 3)} for x, y in quad]


def colorchecker_standard_index(*, block_index: int, orientation_degrees: int) -> int:
    if not 1 <= block_index <= COLORCHECKER_CLASSIC_COUNT:
        raise ValueError("block_index must be in 1..24")
    if orientation_degrees == 0:
        return block_index
    if orientation_degrees == 180:
        return COLORCHECKER_CLASSIC_COUNT + 1 - block_index
    raise ValueError("only 0 and 180 degree ColorChecker orientations are supported")


def colorchecker_patch_metadata(*, block_index: int, orientation_degrees: int) -> dict[str, Any]:
    standard_index = colorchecker_standard_index(
        block_index=block_index,
        orientation_degrees=orientation_degrees,
    )
    patch = dict(COLORCHECKER_CLASSIC_24_PATCHES[standard_index - 1])
    patch["block_index"] = block_index
    return patch


def build_colorchecker_chart_metadata(
    *,
    rows: int,
    cols: int,
    block_count: int,
    orientation_degrees: int,
) -> dict[str, Any] | None:
    if rows != COLORCHECKER_CLASSIC_ROWS or cols != COLORCHECKER_CLASSIC_COLS:
        return None
    if block_count != COLORCHECKER_CLASSIC_COUNT:
        return None
    if orientation_degrees not in SUPPORTED_COLORCHECKER_ORIENTATIONS:
        raise ValueError("only 0 and 180 degree ColorChecker orientations are supported")
    return {
        "type": "colorchecker_classic_24",
        "rows": rows,
        "cols": cols,
        "detected_order": "image_row_major",
        "orientation_degrees": orientation_degrees,
        "standard_index_formula": "block_index" if orientation_degrees == 0 else "25 - block_index",
    }


def build_location_config(
    *,
    target_block_count: int,
    image_width: int,
    image_height: int,
    quads: list[Quad],
    capture: camera_gphoto2.CaptureResult | None,
    rows: int | None = None,
    cols: int | None = None,
    chart_orientation_degrees: int = 0,
    created_at: str | None = None,
) -> dict[str, Any]:
    if target_block_count != len(quads):
        raise ValueError("target_block_count must match the number of quadrilaterals")
    chart = (
        build_colorchecker_chart_metadata(
            rows=rows,
            cols=cols,
            block_count=target_block_count,
            orientation_degrees=chart_orientation_degrees,
        )
        if rows is not None and cols is not None
        else None
    )
    config = {
        "version": 1,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "target_block_count": target_block_count,
        "image": {
            "width": image_width,
            "height": image_height,
        },
        "capture": capture.to_jsonable() if capture is not None else None,
        "blocks": [
            {
                "index": index + 1,
                "points": quad_to_json(quad),
                **(
                    {"patch": colorchecker_patch_metadata(block_index=index + 1, orientation_degrees=chart_orientation_degrees)}
                    if chart is not None
                    else {}
                ),
            }
            for index, quad in enumerate(quads)
        ],
    }
    if chart is not None:
        config["chart"] = chart
    return config


def save_location_config(config: dict[str, Any], *, output_dir: Path | None = None) -> Path:
    target_dir = output_dir or CONFIG_LOCATION_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"locations-{timestamp}.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_preview_from_npy(path: Path, *, white_point: int) -> Any:
    np = _import_numpy()
    return linear_rgb_to_preview_uint8(np.load(path), white_point=white_point)


def find_npy_output(capture: camera_gphoto2.CaptureResult) -> Path:
    if capture.decoded is None:
        raise RuntimeError("auto exposure did not return decoded output")
    for output_file in capture.decoded.output_files:
        if output_file.suffix == ".npy":
            return output_file
    raise RuntimeError("auto exposure did not produce a .npy decoded output")


def _import_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required for the location picker UI") from exc
    return np


@dataclass
class LocationPickerState:
    args: argparse.Namespace
    lock: threading.RLock = field(default_factory=threading.RLock)
    preview_image: Any | None = None
    preview_png: bytes | None = None
    capture_result: camera_gphoto2.AutoExposureResult | None = None
    image_width: int = 0
    image_height: int = 0
    status: str = "等待自动曝光..."
    error: str | None = None
    saved_path: str | None = None
    loading: bool = False
    auto_detect_quads: list[Quad] | None = None
    auto_detect_error: str | None = None
    auto_detect_rows: int | None = None
    auto_detect_cols: int | None = None

    def start_auto_exposure(self) -> bool:
        with self.lock:
            if self.loading:
                return False
            self.preview_image = None
            self.preview_png = None
            self.capture_result = None
            self.image_width = 0
            self.image_height = 0
            self.status = "正在自动曝光..."
            self.error = None
            self.loading = True
            self.auto_detect_quads = None
            self.auto_detect_error = None
            self.auto_detect_rows = None
            self.auto_detect_cols = None
        thread = threading.Thread(target=self._auto_exposure_worker, daemon=True)
        thread.start()
        return True

    def _auto_exposure_worker(self) -> None:
        try:
            run_dir = LOCATION_TMP_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
            metering_regions = None
            if self.args.metering_mode == camera_gphoto2.METERING_MODE_LOCATION:
                if self.args.metering_location_config is None:
                    raise RuntimeError("--metering-location-config is required when --metering-mode=location")
                metering_regions = camera_gphoto2.load_metering_regions(self.args.metering_location_config)
            result = camera_gphoto2.auto_expose_capture(
                output_dir=run_dir / "camera",
                filename_template="%Y%m%d-%H%M%S-location.%C",
                target_max=self.args.target_max,
                iso=self.args.iso,
                aperture=self.args.aperture,
                image_format="RAW",
                min_shutter_speed=self.args.min_shutter_speed,
                max_shutter_speed=self.args.max_shutter_speed,
                max_trials=self.args.max_exposure_trials,
                decode_output_dir=run_dir / "decoded",
                decode_formats=("npy",),
                metering_regions=metering_regions,
                port=self.args.camera_port,
                expected_model=self.args.model,
                executable=self.args.gphoto2,
                timeout=self.args.timeout,
            )
            preview = load_preview_from_npy(find_npy_output(result.final_capture), white_point=self.args.target_max)
        except Exception as exc:
            with self.lock:
                self.status = f"自动曝光失败: {exc}"
                self.error = str(exc)
                self.loading = False
            return

        auto_detect_quads: list[Quad] | None = None
        auto_detect_error: str | None = None
        try:
            auto_detect_quads = detect_color_checker_quads(preview, rows=self.args.rows, cols=self.args.cols)
        except Exception as exc:
            auto_detect_error = str(exc)

        image_height, image_width = preview.shape[:2]
        image_max = result.final_capture.decoded.to_jsonable().get("image_max") if result.final_capture.decoded else None
        shutter = result.final_capture.settings.shutter_speed
        status = f"已加载: {image_width}x{image_height}, shutter={shutter}, max={image_max}"
        if auto_detect_quads is not None:
            status = f"{status}; 已自动识别 {self.args.rows}x{self.args.cols} 色块"
        elif auto_detect_error is not None:
            status = f"{status}; 自动识别失败: {auto_detect_error}"
        with self.lock:
            self.capture_result = result
            self.preview_image = preview
            self.preview_png = rgb_to_png_data(preview)
            self.image_width = int(image_width)
            self.image_height = int(image_height)
            self.status = status
            self.error = None
            self.loading = False
            self.auto_detect_quads = auto_detect_quads
            self.auto_detect_error = auto_detect_error
            self.auto_detect_rows = self.args.rows
            self.auto_detect_cols = self.args.cols

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            capture = self.capture_result.to_jsonable() if self.capture_result is not None else None
            auto_detect = {
                "rows": self.auto_detect_rows,
                "cols": self.auto_detect_cols,
                "target_block_count": len(self.auto_detect_quads) if self.auto_detect_quads is not None else None,
                "quads": _quads_to_jsonable(self.auto_detect_quads) if self.auto_detect_quads is not None else None,
                "error": self.auto_detect_error,
            }
            return {
                "status": self.status,
                "error": self.error,
                "saved_path": self.saved_path,
                "loading": self.loading,
                "has_image": self.preview_png is not None,
                "retry_available": not self.loading and self.preview_png is None,
                "auto_detect": auto_detect,
                "image": {
                    "width": self.image_width,
                    "height": self.image_height,
                },
                "defaults": {
                    "blocks": self.args.blocks,
                    "rows": self.args.rows,
                    "cols": self.args.cols,
                    "chart_orientation_degrees": self.args.chart_orientation,
                },
                "capture": capture,
            }

    def detect_quads(self, *, rows: int, cols: int) -> dict[str, Any]:
        if rows <= 0 or cols <= 0:
            raise ValueError("rows and cols must be positive")
        with self.lock:
            image = self.preview_image
        if image is None:
            raise RuntimeError("preview image is not ready")

        quads = detect_color_checker_quads(image, rows=rows, cols=cols)
        with self.lock:
            self.status = f"已自动识别 {rows}x{cols} 色块"
            self.auto_detect_quads = quads
            self.auto_detect_error = None
            self.auto_detect_rows = rows
            self.auto_detect_cols = cols
        return {
            "status": f"已自动识别 {rows}x{cols} 色块",
            "target_block_count": rows * cols,
            "quads": _quads_to_jsonable(quads),
        }

    def save_quads(
        self,
        *,
        target_block_count: int,
        quads_payload: Any,
        rows: int | None = None,
        cols: int | None = None,
        chart_orientation_degrees: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            capture = self.capture_result.final_capture if self.capture_result is not None else None
            image_width = self.image_width
            image_height = self.image_height
        if capture is None or image_width <= 0 or image_height <= 0:
            raise RuntimeError("preview image is not ready")

        quads = parse_quads_payload(quads_payload, width=image_width, height=image_height)
        rows = rows if rows is not None else self.args.rows
        cols = cols if cols is not None else self.args.cols
        chart_orientation_degrees = (
            chart_orientation_degrees
            if chart_orientation_degrees is not None
            else self.args.chart_orientation
        )
        config = build_location_config(
            target_block_count=target_block_count,
            image_width=image_width,
            image_height=image_height,
            quads=quads,
            capture=capture,
            rows=rows,
            cols=cols,
            chart_orientation_degrees=chart_orientation_degrees,
        )
        path = save_location_config(config)
        with self.lock:
            self.saved_path = str(path)
            self.status = f"已保存: {path}"
        return {"status": f"已保存: {path}", "path": str(path), "config": config}


def _quads_to_jsonable(quads: list[Quad]) -> list[list[list[float]]]:
    return [[[float(x), float(y)] for x, y in quad] for quad in quads]


def parse_quads_payload(raw_quads: Any, *, width: int, height: int) -> list[Quad]:
    if not isinstance(raw_quads, list):
        raise ValueError("quads must be a list")
    quads: list[Quad] = []
    for quad_index, raw_quad in enumerate(raw_quads):
        if not isinstance(raw_quad, list) or len(raw_quad) != 4:
            raise ValueError(f"quad {quad_index + 1} must contain four points")
        quad: Quad = []
        for point_index, raw_point in enumerate(raw_quad):
            x, y = _parse_payload_point(raw_point, quad_index=quad_index, point_index=point_index)
            if not (0.0 <= x <= width - 1 and 0.0 <= y <= height - 1):
                raise ValueError(f"quad {quad_index + 1} point {point_index + 1} is outside the image")
            quad.append((x, y))
        quads.append(quad)
    return quads


def _parse_payload_point(raw_point: Any, *, quad_index: int, point_index: int) -> Point:
    if isinstance(raw_point, dict):
        raw_x = raw_point.get("x")
        raw_y = raw_point.get("y")
    elif isinstance(raw_point, list) and len(raw_point) == 2:
        raw_x, raw_y = raw_point
    else:
        raise ValueError(f"quad {quad_index + 1} point {point_index + 1} must be [x, y] or {{x, y}}")
    try:
        x = float(raw_x)
        y = float(raw_y)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"quad {quad_index + 1} point {point_index + 1} is not numeric") from exc
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError(f"quad {quad_index + 1} point {point_index + 1} is not finite")
    return (x, y)


def make_location_picker_handler(state: LocationPickerState) -> type[BaseHTTPRequestHandler]:
    class LocationPickerRequestHandler(BaseHTTPRequestHandler):
        server_version = "WLEDRGBWWLocationPicker/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_bytes(LOCATION_PICKER_HTML.encode("utf-8"), content_type="text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                self._send_json(state.snapshot())
                return
            if parsed.path == "/preview.png":
                with state.lock:
                    preview_png = state.preview_png
                if preview_png is None:
                    self._send_json({"error": "preview image is not ready"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self._send_bytes(preview_png, content_type="image/png")
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                payload = self._read_json_body()
                if parsed.path == "/api/detect":
                    response = state.detect_quads(
                        rows=_required_int(payload, "rows"),
                        cols=_required_int(payload, "cols"),
                    )
                elif parsed.path == "/api/retry":
                    started = state.start_auto_exposure()
                    response = state.snapshot()
                    response["retry_started"] = started
                elif parsed.path == "/api/save":
                    response = state.save_quads(
                        target_block_count=_required_int(payload, "target_block_count"),
                        quads_payload=payload.get("quads"),
                        rows=_optional_int(payload, "rows"),
                        cols=_optional_int(payload, "cols"),
                        chart_orientation_degrees=_optional_int(payload, "chart_orientation_degrees"),
                    )
                else:
                    self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(response)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > 2_000_000:
                raise ValueError("request body is too large")
            raw_body = self.rfile.read(content_length)
            try:
                payload = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("request body must be JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                content_type="application/json; charset=utf-8",
                status=status,
            )

        def _send_bytes(
            self,
            data: bytes,
            *,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return LocationPickerRequestHandler


def _required_int(payload: dict[str, Any], name: str) -> int:
    if name not in payload:
        raise ValueError(f"{name} is required")
    try:
        return int(payload[name])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _optional_int(payload: dict[str, Any], name: str) -> int | None:
    if name not in payload or payload[name] is None:
        return None
    try:
        return int(payload[name])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def create_location_picker_server(
    *,
    host: str,
    port: int,
    state: LocationPickerState,
    port_search_limit: int = 20,
) -> ThreadingHTTPServer:
    handler = make_location_picker_handler(state)
    candidates = [0] if port == 0 else list(range(port, port + port_search_limit + 1))
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last_error = exc
    if last_error is None:
        raise RuntimeError("could not create location picker server")
    raise last_error


LOCATION_PICKER_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WLED-RGBWW Location Picker</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #18191b;
      --panel: #25282c;
      --panel-2: #30343a;
      --text: #f4f7fb;
      --muted: #b8c0ca;
      --line: #505965;
      --cyan: #42d9ff;
      --gold: #ffcf40;
      --danger: #ff6b6b;
      --ok: #69e0a3;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; background: var(--bg); color: var(--text); font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { overflow: hidden; }
    .app { height: 100%; display: grid; grid-template-rows: auto 1fr auto; }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 48px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      white-space: nowrap;
      overflow-x: auto;
    }
    label { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); }
    input {
      width: 68px;
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 8px;
      background: var(--panel-2);
      color: var(--text);
      font: inherit;
    }
    input.small { width: 54px; }
    button {
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: var(--panel-2);
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }
    button:hover:not(:disabled) { border-color: #7a8798; background: #383d44; }
    button:disabled { color: #7a828c; cursor: default; opacity: 0.7; }
    button.primary { border-color: #809a46; background: #3c4a2d; color: #f8ffe8; }
    button.danger { border-color: #8e4b4b; background: #4a3030; }
    .statusbar {
      min-height: 32px;
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 5px 10px;
      border-top: 1px solid var(--line);
      background: var(--panel);
      color: var(--muted);
    }
    .statusbar .strong { color: var(--text); }
    .statusbar .error { color: var(--danger); }
    .statusbar .saved { color: var(--ok); }
    .canvas-wrap { position: relative; min-height: 0; background: #101113; }
    canvas { display: block; width: 100%; height: 100%; cursor: crosshair; }
    .overlay {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      pointer-events: none;
      color: var(--muted);
      font-size: 15px;
    }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class="app">
    <div class="toolbar">
      <label>总色块数 <input id="blocks" type="number" min="1" max="1000" value="24"></label>
      <label>行 <input id="rows" class="small" type="number" min="1" max="100" value="4"></label>
      <label>列 <input id="cols" class="small" type="number" min="1" max="100" value="6"></label>
      <button id="retry">重试相机</button>
      <button id="detect">自动识别</button>
      <button id="rotateChart" title="切换标准24色色卡标注方向">色卡 0°</button>
      <button id="fit">适配窗口</button>
      <button id="delete" class="danger">删除选中</button>
      <button id="clear" class="danger">清空</button>
      <button id="save" class="primary" disabled>确认保存</button>
    </div>
    <div class="canvas-wrap">
      <canvas id="canvas"></canvas>
      <div id="overlay" class="overlay">正在自动曝光...</div>
    </div>
    <div class="statusbar">
      <span class="strong" id="count">0 / 24</span>
      <span id="status">正在自动曝光...</span>
      <span class="saved" id="saved"></span>
    </div>
  </div>
  <script>
    const HANDLE_RADIUS = 7;
    const EDGE_HIT_DISTANCE = 8;
    const MIN_CREATE_PIXELS = 6;

    const canvas = document.getElementById("canvas");
    const ctx = canvas.getContext("2d");
    const overlay = document.getElementById("overlay");
    const blocksInput = document.getElementById("blocks");
    const rowsInput = document.getElementById("rows");
    const colsInput = document.getElementById("cols");
    const retryButton = document.getElementById("retry");
    const detectButton = document.getElementById("detect");
    const rotateChartButton = document.getElementById("rotateChart");
    const fitButton = document.getElementById("fit");
    const deleteButton = document.getElementById("delete");
    const clearButton = document.getElementById("clear");
    const saveButton = document.getElementById("save");
    const statusEl = document.getElementById("status");
    const countEl = document.getElementById("count");
    const savedEl = document.getElementById("saved");

    const state = {
      hasImage: false,
      image: null,
      imageWidth: 0,
      imageHeight: 0,
      zoom: 1,
      offsetX: 0,
      offsetY: 0,
      quads: [],
      selectedQuad: -1,
      drag: null,
      createPreview: null,
      fittedOnce: false,
      loading: false,
      retryAvailable: false,
      chartOrientationDegrees: 0
    };

    const COLORCHECKER_CLASSIC_24 = [
      { index: 1, name: "dark_skin", label: "Skin D" },
      { index: 2, name: "light_skin", label: "Skin L" },
      { index: 3, name: "blue_sky", label: "Sky" },
      { index: 4, name: "foliage", label: "Foliage" },
      { index: 5, name: "blue_flower", label: "Flower" },
      { index: 6, name: "bluish_green", label: "BG" },
      { index: 7, name: "orange", label: "Orange" },
      { index: 8, name: "purplish_blue", label: "PB" },
      { index: 9, name: "moderate_red", label: "MR" },
      { index: 10, name: "purple", label: "Purple" },
      { index: 11, name: "yellow_green", label: "YG" },
      { index: 12, name: "orange_yellow", label: "OY" },
      { index: 13, name: "blue", label: "B" },
      { index: 14, name: "green", label: "G" },
      { index: 15, name: "red", label: "R" },
      { index: 16, name: "yellow", label: "Y" },
      { index: 17, name: "magenta", label: "M" },
      { index: 18, name: "cyan", label: "C" },
      { index: 19, name: "white", label: "W" },
      { index: 20, name: "neutral_8", label: "N8" },
      { index: 21, name: "neutral_6_5", label: "N6.5" },
      { index: 22, name: "neutral_5", label: "N5" },
      { index: 23, name: "neutral_3_5", label: "N3.5" },
      { index: 24, name: "black", label: "K" }
    ];

    function resizeCanvas() {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(rect.width * dpr));
      canvas.height = Math.max(1, Math.round(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function canvasSize() {
      const rect = canvas.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }

    async function refreshState() {
      const response = await fetch("/api/state");
      const data = await response.json();
      blocksInput.value = data.defaults.blocks;
      rowsInput.value = data.defaults.rows;
      colsInput.value = data.defaults.cols;
      state.chartOrientationDegrees = Number.isFinite(data.defaults.chart_orientation_degrees)
        ? data.defaults.chart_orientation_degrees
        : 0;
      state.loading = Boolean(data.loading);
      state.retryAvailable = Boolean(data.retry_available);
      updateStatus(data.status, data.error);
      if (data.saved_path) savedEl.textContent = data.saved_path;
      if (data.has_image && !state.hasImage) {
        state.imageWidth = data.image.width;
        state.imageHeight = data.image.height;
        await loadPreview();
        applyInitialAutoDetect(data.auto_detect);
      }
      updateControls();
      if (!state.hasImage && data.loading) {
        window.setTimeout(refreshState, 800);
      }
    }

    async function loadPreview() {
      const image = new Image();
      image.decoding = "async";
      image.src = "/preview.png?t=" + Date.now();
      await image.decode();
      state.image = image;
      state.hasImage = true;
      overlay.classList.add("hidden");
      fitToWindow();
    }

    function applyInitialAutoDetect(autoDetect) {
      if (!autoDetect || !Array.isArray(autoDetect.quads)) return;
      state.quads = autoDetect.quads;
      state.selectedQuad = -1;
      if (Number.isFinite(autoDetect.target_block_count)) {
        blocksInput.value = autoDetect.target_block_count;
      }
      if (Number.isFinite(autoDetect.rows)) rowsInput.value = autoDetect.rows;
      if (Number.isFinite(autoDetect.cols)) colsInput.value = autoDetect.cols;
      draw();
    }

    function fitToWindow() {
      if (!state.hasImage) return;
      const size = canvasSize();
      state.zoom = Math.max(0.02, Math.min(8, Math.min(size.width / state.imageWidth, size.height / state.imageHeight) * 0.96));
      state.offsetX = (size.width - state.imageWidth * state.zoom) / 2;
      state.offsetY = (size.height - state.imageHeight * state.zoom) / 2;
      state.fittedOnce = true;
      draw();
    }

    function draw() {
      const size = canvasSize();
      ctx.clearRect(0, 0, size.width, size.height);
      ctx.fillStyle = "#101113";
      ctx.fillRect(0, 0, size.width, size.height);
      if (!state.hasImage || !state.image) {
        updateControls();
        return;
      }
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(state.image, state.offsetX, state.offsetY, state.imageWidth * state.zoom, state.imageHeight * state.zoom);
      state.quads.forEach((quad, index) => drawQuad(quad, index === state.selectedQuad, false, index));
      if (state.createPreview) drawQuad(state.createPreview, false, true);
      updateControls();
    }

    function drawQuad(quad, selected, preview, quadIndex = -1) {
      const color = preview ? "#b9c0c8" : (selected ? "#ffcf40" : "#42d9ff");
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      quad.forEach((point, index) => {
        const canvasPoint = imageToCanvas(point);
        if (index === 0) ctx.moveTo(canvasPoint.x, canvasPoint.y);
        else ctx.lineTo(canvasPoint.x, canvasPoint.y);
      });
      ctx.closePath();
      ctx.stroke();
      quad.forEach((point) => {
        const canvasPoint = imageToCanvas(point);
        const radius = HANDLE_RADIUS + (selected ? 2 : 0);
        ctx.beginPath();
        ctx.arc(canvasPoint.x, canvasPoint.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = "#202327";
        ctx.fill();
        ctx.stroke();
      });
      if (!preview && shouldShowColorCheckerLabels()) {
        drawPatchLabel(quad, quadIndex);
      }
      ctx.restore();
    }

    function shouldShowColorCheckerLabels() {
      return parseInt(rowsInput.value, 10) === 4
        && parseInt(colsInput.value, 10) === 6
        && state.quads.length === 24;
    }

    function colorCheckerPatchForBlock(blockIndex) {
      const standardIndex = state.chartOrientationDegrees === 180 ? 25 - blockIndex : blockIndex;
      return COLORCHECKER_CLASSIC_24[standardIndex - 1] || null;
    }

    function drawPatchLabel(quad, quadIndex) {
      const patch = colorCheckerPatchForBlock(quadIndex + 1);
      if (!patch) return;
      const center = quad.reduce((acc, point) => [acc[0] + point[0] / 4, acc[1] + point[1] / 4], [0, 0]);
      const canvasPoint = imageToCanvas(center);
      const text = patch.label;
      ctx.save();
      ctx.font = "600 12px system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const metrics = ctx.measureText(text);
      const paddingX = 5;
      const width = metrics.width + paddingX * 2;
      const height = 18;
      ctx.fillStyle = "rgba(0,0,0,0.65)";
      ctx.fillRect(canvasPoint.x - width / 2, canvasPoint.y - height / 2, width, height);
      ctx.strokeStyle = "rgba(255,255,255,0.35)";
      ctx.strokeRect(canvasPoint.x - width / 2, canvasPoint.y - height / 2, width, height);
      ctx.fillStyle = "#f4f7fb";
      ctx.fillText(text, canvasPoint.x, canvasPoint.y);
      ctx.restore();
    }

    function imageToCanvas(point) {
      return { x: point[0] * state.zoom + state.offsetX, y: point[1] * state.zoom + state.offsetY };
    }

    function canvasToImage(point) {
      return [(point.x - state.offsetX) / state.zoom, (point.y - state.offsetY) / state.zoom];
    }

    function eventPoint(event) {
      const rect = canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function clampPoint(point) {
      return [
        Math.min(Math.max(point[0], 0), state.imageWidth - 1),
        Math.min(Math.max(point[1], 0), state.imageHeight - 1)
      ];
    }

    function rectToQuad(start, end) {
      const left = Math.min(start[0], end[0]);
      const right = Math.max(start[0], end[0]);
      const top = Math.min(start[1], end[1]);
      const bottom = Math.max(start[1], end[1]);
      return [[left, top], [right, top], [right, bottom], [left, bottom]];
    }

    function translateQuad(quad, dx, dy) {
      let minX = Math.min(...quad.map((point) => point[0]));
      let maxX = Math.max(...quad.map((point) => point[0]));
      let minY = Math.min(...quad.map((point) => point[1]));
      let maxY = Math.max(...quad.map((point) => point[1]));
      if (minX + dx < 0) dx = -minX;
      if (maxX + dx > state.imageWidth - 1) dx = state.imageWidth - 1 - maxX;
      if (minY + dy < 0) dy = -minY;
      if (maxY + dy > state.imageHeight - 1) dy = state.imageHeight - 1 - maxY;
      return quad.map((point) => [point[0] + dx, point[1] + dy]);
    }

    function pointInPolygon(point, polygon) {
      const x = point[0];
      const y = point[1];
      let inside = false;
      for (let index = 0; index < polygon.length; index++) {
        const current = polygon[index];
        const next = polygon[(index + 1) % polygon.length];
        if ((current[1] > y) !== (next[1] > y)) {
          const intersectionX = (next[0] - current[0]) * (y - current[1]) / (next[1] - current[1]) + current[0];
          if (x < intersectionX) inside = !inside;
        }
      }
      return inside;
    }

    function distanceToSegment(point, start, end) {
      const dx = end.x - start.x;
      const dy = end.y - start.y;
      if (dx === 0 && dy === 0) return Math.hypot(point.x - start.x, point.y - start.y);
      const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy)));
      return Math.hypot(point.x - (start.x + t * dx), point.y - (start.y + t * dy));
    }

    function hitTest(canvasPoint) {
      const imagePoint = canvasToImage(canvasPoint);
      for (let quadIndex = state.quads.length - 1; quadIndex >= 0; quadIndex--) {
        const quad = state.quads[quadIndex];
        for (let vertexIndex = 0; vertexIndex < 4; vertexIndex++) {
          const handle = imageToCanvas(quad[vertexIndex]);
          if (Math.hypot(canvasPoint.x - handle.x, canvasPoint.y - handle.y) <= HANDLE_RADIUS + 4) {
            return { mode: "vertex", quadIndex, subIndex: vertexIndex };
          }
        }
        for (let edgeIndex = 0; edgeIndex < 4; edgeIndex++) {
          const start = imageToCanvas(quad[edgeIndex]);
          const end = imageToCanvas(quad[(edgeIndex + 1) % 4]);
          if (distanceToSegment(canvasPoint, start, end) <= EDGE_HIT_DISTANCE) {
            return { mode: "edge", quadIndex, subIndex: edgeIndex };
          }
        }
        if (pointInPolygon(imagePoint, quad)) return { mode: "move", quadIndex, subIndex: null };
      }
      return null;
    }

    function zoomAt(canvasPoint, factor) {
      if (!state.hasImage) return;
      const before = canvasToImage(canvasPoint);
      state.zoom = Math.max(0.02, Math.min(8, state.zoom * factor));
      state.offsetX = canvasPoint.x - before[0] * state.zoom;
      state.offsetY = canvasPoint.y - before[1] * state.zoom;
      draw();
    }

    canvas.addEventListener("mousedown", (event) => {
      if (!state.hasImage) return;
      const point = eventPoint(event);
      if (event.button === 1 || event.button === 2) {
        state.drag = { mode: "pan", startCanvas: point };
        event.preventDefault();
        return;
      }
      if (event.button !== 0) return;
      const hit = hitTest(point);
      if (hit) {
        state.selectedQuad = hit.quadIndex;
        state.drag = {
          mode: hit.mode,
          startCanvas: point,
          startImage: canvasToImage(point),
          quadIndex: hit.quadIndex,
          subIndex: hit.subIndex,
          originalQuad: state.quads[hit.quadIndex].map((item) => [...item])
        };
      } else {
        const imagePoint = clampPoint(canvasToImage(point));
        state.selectedQuad = -1;
        state.drag = { mode: "create", startCanvas: point, startImage: imagePoint };
        state.createPreview = [imagePoint, imagePoint, imagePoint, imagePoint];
      }
      draw();
      event.preventDefault();
    });

    window.addEventListener("mousemove", (event) => {
      if (!state.drag || !state.hasImage) return;
      const point = eventPoint(event);
      if (state.drag.mode === "pan") {
        state.offsetX += point.x - state.drag.startCanvas.x;
        state.offsetY += point.y - state.drag.startCanvas.y;
        state.drag.startCanvas = point;
        draw();
        return;
      }
      const imagePoint = clampPoint(canvasToImage(point));
      if (state.drag.mode === "create") {
        state.createPreview = rectToQuad(state.drag.startImage, imagePoint);
      } else {
        const dx = imagePoint[0] - state.drag.startImage[0];
        const dy = imagePoint[1] - state.drag.startImage[1];
        const quad = state.drag.originalQuad.map((item) => [...item]);
        if (state.drag.mode === "move") {
          state.quads[state.drag.quadIndex] = translateQuad(quad, dx, dy);
        } else if (state.drag.mode === "vertex") {
          quad[state.drag.subIndex] = imagePoint;
          state.quads[state.drag.quadIndex] = quad.map(clampPoint);
        } else if (state.drag.mode === "edge") {
          const first = state.drag.subIndex;
          const second = (first + 1) % 4;
          quad[first] = clampPoint([quad[first][0] + dx, quad[first][1] + dy]);
          quad[second] = clampPoint([quad[second][0] + dx, quad[second][1] + dy]);
          state.quads[state.drag.quadIndex] = quad;
        }
      }
      draw();
    });

    window.addEventListener("mouseup", () => {
      if (state.drag && state.drag.mode === "create" && state.createPreview) {
        const width = Math.abs(state.createPreview[1][0] - state.createPreview[0][0]);
        const height = Math.abs(state.createPreview[2][1] - state.createPreview[1][1]);
        if (width >= MIN_CREATE_PIXELS && height >= MIN_CREATE_PIXELS) {
          state.quads.push(state.createPreview);
          state.selectedQuad = state.quads.length - 1;
        }
      }
      state.createPreview = null;
      state.drag = null;
      draw();
    });

    canvas.addEventListener("wheel", (event) => {
      zoomAt(eventPoint(event), event.deltaY < 0 ? 1.15 : 1 / 1.15);
      event.preventDefault();
    }, { passive: false });

    canvas.addEventListener("contextmenu", (event) => event.preventDefault());

    window.addEventListener("keydown", (event) => {
      if (event.key === "Delete" || event.key === "Backspace") {
        deleteSelected();
        event.preventDefault();
      }
    });

    function deleteSelected() {
      if (state.selectedQuad < 0) return;
      state.quads.splice(state.selectedQuad, 1);
      state.selectedQuad = -1;
      draw();
    }

    function clearQuads() {
      state.quads = [];
      state.selectedQuad = -1;
      draw();
    }

    async function postJson(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    async function autoDetect() {
      if (!state.hasImage) return;
      try {
        updateStatus("正在自动识别...", null);
        const rows = parseInt(rowsInput.value, 10);
        const cols = parseInt(colsInput.value, 10);
        const data = await postJson("/api/detect", { rows, cols });
        state.quads = data.quads;
        state.selectedQuad = -1;
        blocksInput.value = data.target_block_count;
        updateStatus(data.status, null);
        draw();
      } catch (error) {
        updateStatus("自动识别失败: " + error.message, error.message);
      }
    }

    function resetImageState() {
      state.hasImage = false;
      state.image = null;
      state.imageWidth = 0;
      state.imageHeight = 0;
      state.zoom = 1;
      state.offsetX = 0;
      state.offsetY = 0;
      state.quads = [];
      state.selectedQuad = -1;
      state.drag = null;
      state.createPreview = null;
      state.fittedOnce = false;
      state.chartOrientationDegrees = 0;
      savedEl.textContent = "";
      draw();
    }

    async function retryCamera() {
      try {
        resetImageState();
        state.loading = true;
        state.retryAvailable = false;
        updateStatus("正在自动曝光...", null);
        updateControls();
        const data = await postJson("/api/retry", {});
        state.loading = Boolean(data.loading);
        state.retryAvailable = Boolean(data.retry_available);
        updateStatus(data.status, data.error);
        updateControls();
        window.setTimeout(refreshState, 800);
      } catch (error) {
        state.loading = false;
        state.retryAvailable = true;
        updateStatus("重试失败: " + error.message, error.message);
        updateControls();
      }
    }

    async function saveConfig() {
      if (!state.hasImage) return;
      try {
        const target = parseInt(blocksInput.value, 10);
        const rows = parseInt(rowsInput.value, 10);
        const cols = parseInt(colsInput.value, 10);
        const data = await postJson("/api/save", {
          target_block_count: target,
          rows,
          cols,
          chart_orientation_degrees: state.chartOrientationDegrees,
          quads: state.quads
        });
        updateStatus(data.status, null);
        savedEl.textContent = data.path;
      } catch (error) {
        updateStatus("保存失败: " + error.message, error.message);
      }
    }

    function updateStatus(message, error) {
      statusEl.textContent = message || "";
      statusEl.className = error ? "error" : "";
      overlay.textContent = message || "";
      if (state.hasImage) overlay.classList.add("hidden");
      else overlay.classList.remove("hidden");
    }

    function updateControls() {
      const target = parseInt(blocksInput.value, 10);
      const ready = state.hasImage;
      const saveReady = ready && Number.isFinite(target) && target > 0 && state.quads.length === target;
      retryButton.disabled = state.loading || ready || !state.retryAvailable;
      detectButton.disabled = !ready;
      rotateChartButton.disabled = !ready || parseInt(rowsInput.value, 10) !== 4 || parseInt(colsInput.value, 10) !== 6 || state.quads.length !== 24;
      rotateChartButton.textContent = "色卡 " + state.chartOrientationDegrees + "°";
      fitButton.disabled = !ready;
      deleteButton.disabled = !ready || state.selectedQuad < 0;
      clearButton.disabled = !ready || state.quads.length === 0;
      saveButton.disabled = !saveReady;
      countEl.textContent = state.quads.length + " / " + (Number.isFinite(target) ? target : "-");
    }

    blocksInput.addEventListener("input", updateControls);
    retryButton.addEventListener("click", retryCamera);
    detectButton.addEventListener("click", autoDetect);
    rotateChartButton.addEventListener("click", () => {
      state.chartOrientationDegrees = state.chartOrientationDegrees === 180 ? 0 : 180;
      draw();
    });
    fitButton.addEventListener("click", fitToWindow);
    deleteButton.addEventListener("click", deleteSelected);
    clearButton.addEventListener("click", clearQuads);
    saveButton.addEventListener("click", saveConfig);
    window.addEventListener("resize", resizeCanvas);

    resizeCanvas();
    refreshState().catch((error) => updateStatus("状态加载失败: " + error.message, error.message));
  </script>
</body>
</html>
"""


def rect_to_quad(start: Point, end: Point) -> Quad:
    x1, y1 = start
    x2, y2 = end
    left = min(x1, x2)
    right = max(x1, x2)
    top = min(y1, y2)
    bottom = max(y1, y2)
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture a calibration image and mark color-block locations in a local Web UI.")
    parser.add_argument("--blocks", type=int, default=24, help="Expected total color-block count.")
    parser.add_argument("--rows", type=int, default=4, help="Grid row count for automatic block detection.")
    parser.add_argument("--cols", type=int, default=6, help="Grid column count for automatic block detection.")
    parser.add_argument(
        "--chart-orientation",
        type=int,
        choices=SUPPORTED_COLORCHECKER_ORIENTATIONS,
        default=0,
        help="Standard ColorChecker Classic 24 orientation for 4x6 patch metadata.",
    )
    parser.add_argument("--host", default=DEFAULT_UI_HOST, help="Local Web UI bind host.")
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT, help="Local Web UI bind port; uses the next free port if occupied.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the local Web UI in the default browser.")
    parser.add_argument("--target-max", type=int, default=DEFAULT_TARGET_MAX)
    parser.add_argument("--iso", default=DEFAULT_ISO)
    parser.add_argument("--aperture", default=DEFAULT_APERTURE)
    parser.add_argument("--min-shutter-speed", default=DEFAULT_MIN_SHUTTER_SPEED)
    parser.add_argument("--max-shutter-speed", default=DEFAULT_MAX_SHUTTER_SPEED)
    parser.add_argument("--max-exposure-trials", type=int, default=DEFAULT_MAX_EXPOSURE_TRIALS)
    parser.add_argument(
        "--metering-mode",
        choices=(camera_gphoto2.METERING_MODE_FULL, camera_gphoto2.METERING_MODE_LOCATION),
        default=camera_gphoto2.METERING_MODE_FULL,
        help="Use the full decoded image or a saved 24-block location config for auto-exposure metering.",
    )
    parser.add_argument("--metering-location-config", type=Path, help="Location picker JSON used by location metering.")
    parser.add_argument("--model", default=camera_gphoto2.DEFAULT_CAMERA_MODEL)
    parser.add_argument("--port", dest="camera_port", help="Camera USB port passed to gphoto2.")
    parser.add_argument("--camera-port", dest="camera_port", help="Camera USB port passed to gphoto2.")
    parser.add_argument("--gphoto2", default="gphoto2")
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = LocationPickerState(args=args)
    server = create_location_picker_server(host=args.host, port=args.ui_port, state=state)
    bound_host, bound_port = server.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"
    print(f"Location picker Web UI: {url}")
    print("Press Ctrl+C to stop.")
    state.start_auto_exposure()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping location picker Web UI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
