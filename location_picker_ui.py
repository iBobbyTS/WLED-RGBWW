from __future__ import annotations

import argparse
import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

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
DEFAULT_CANVAS_WIDTH = 1200
DEFAULT_CANVAS_HEIGHT = 800
DEFAULT_AUTO_DETECT_INSET_RATIO = 0.08
HANDLE_RADIUS = 7
EDGE_HIT_DISTANCE = 8
MIN_CREATE_PIXELS = 6

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


@dataclass
class DragState:
    mode: str
    start_canvas: Point
    start_image: Point | None = None
    quad_index: int | None = None
    vertex_index: int | None = None
    edge_index: int | None = None
    original_quad: Quad | None = None


def linear_rgb_to_preview_uint8(image: Any, *, white_point: int = DEFAULT_TARGET_MAX) -> Any:
    np = _import_numpy()
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"expected HxWx3 image, got shape {image.shape}")
    if white_point <= 0:
        raise ValueError("white_point must be positive")
    normalized = np.clip(image.astype(np.float32) / float(white_point), 0.0, 1.0)
    return (normalized * 255.0 + 0.5).astype(np.uint8)


def resize_nearest_rgb(image: Any, scale: float) -> Any:
    np = _import_numpy()
    if scale <= 0:
        raise ValueError("scale must be positive")
    source_h, source_w = image.shape[:2]
    target_w = max(1, int(round(source_w * scale)))
    target_h = max(1, int(round(source_h * scale)))
    row_index = np.minimum((np.arange(target_h) / scale).astype(np.int64), source_h - 1)
    col_index = np.minimum((np.arange(target_w) / scale).astype(np.int64), source_w - 1)
    return image[row_index[:, None], col_index[None, :]]


def rgb_to_ppm_photo_data(image: Any) -> bytes:
    height, width = image.shape[:2]
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + image.tobytes()


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


def build_location_config(
    *,
    target_block_count: int,
    image_width: int,
    image_height: int,
    quads: list[Quad],
    capture: camera_gphoto2.CaptureResult | None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if target_block_count != len(quads):
        raise ValueError("target_block_count must match the number of quadrilaterals")
    return {
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
            }
            for index, quad in enumerate(quads)
        ],
    }


def save_location_config(config: dict[str, Any], *, output_dir: Path = CONFIG_LOCATION_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"locations-{timestamp}.json"
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


class LocationPickerApp:
    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.root.title("WLED-RGBWW Location Picker")

        self.preview_image: Any | None = None
        self.photo: tk.PhotoImage | None = None
        self.capture_result: camera_gphoto2.AutoExposureResult | None = None
        self.image_width = 0
        self.image_height = 0
        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.image_item: int | None = None
        self.quads: list[Quad] = []
        self.selected_quad: int | None = None
        self.drag: DragState | None = None
        self.create_preview: Quad | None = None

        self.block_count = tk.IntVar(value=args.blocks)
        self.grid_rows = tk.IntVar(value=args.rows)
        self.grid_cols = tk.IntVar(value=args.cols)
        self.status = tk.StringVar(value="正在自动曝光...")

        self._build_ui()
        self._bind_events()
        self.root.after(50, self._start_auto_exposure)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=6)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(toolbar, text="总色块数").pack(side=tk.LEFT)
        count_entry = ttk.Spinbox(toolbar, from_=1, to=1000, textvariable=self.block_count, width=6, command=self._update_confirm_state)
        count_entry.pack(side=tk.LEFT, padx=(4, 10))
        count_entry.bind("<KeyRelease>", lambda _event: self._update_confirm_state())

        ttk.Label(toolbar, text="行").pack(side=tk.LEFT)
        row_entry = ttk.Spinbox(toolbar, from_=1, to=100, textvariable=self.grid_rows, width=4)
        row_entry.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(toolbar, text="列").pack(side=tk.LEFT)
        col_entry = ttk.Spinbox(toolbar, from_=1, to=100, textvariable=self.grid_cols, width=4)
        col_entry.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(toolbar, text="自动识别", command=self.auto_detect_blocks).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(toolbar, text="适配窗口", command=self.fit_to_window).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="删除选中", command=self.delete_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="清空", command=self.clear_quads).pack(side=tk.LEFT, padx=(0, 12))

        self.confirm_button = ttk.Button(toolbar, text="确认保存", command=self.save_config, state=tk.DISABLED)
        self.confirm_button.pack(side=tk.LEFT)

        ttk.Label(toolbar, textvariable=self.status).pack(side=tk.LEFT, padx=(12, 0))

        self.canvas = tk.Canvas(self.root, width=DEFAULT_CANVAS_WIDTH, height=DEFAULT_CANVAS_HEIGHT, bg="#202020", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def _bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self.on_left_press)
        self.canvas.bind("<B1-Motion>", self.on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_release)
        self.canvas.bind("<ButtonPress-2>", self.on_pan_press)
        self.canvas.bind("<B2-Motion>", self.on_pan_drag)
        self.canvas.bind("<ButtonPress-3>", self.on_pan_press)
        self.canvas.bind("<B3-Motion>", self.on_pan_drag)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-4>", lambda event: self.zoom_at(event.x, event.y, 1.15))
        self.canvas.bind("<Button-5>", lambda event: self.zoom_at(event.x, event.y, 1 / 1.15))
        self.root.bind("<Delete>", lambda _event: self.delete_selected())
        self.root.bind("<BackSpace>", lambda _event: self.delete_selected())

    def _start_auto_exposure(self) -> None:
        thread = threading.Thread(target=self._auto_exposure_worker, daemon=True)
        thread.start()

    def _auto_exposure_worker(self) -> None:
        try:
            run_dir = LOCATION_TMP_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
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
                port=self.args.port,
                expected_model=self.args.model,
                executable=self.args.gphoto2,
                timeout=self.args.timeout,
            )
            preview = load_preview_from_npy(find_npy_output(result.final_capture), white_point=self.args.target_max)
        except Exception as exc:
            self.root.after(0, self._show_capture_error, exc)
            return
        self.root.after(0, self._set_image, result, preview)

    def _show_capture_error(self, exc: Exception) -> None:
        self.status.set(f"自动曝光失败: {exc}")
        messagebox.showerror("自动曝光失败", str(exc))

    def _set_image(self, result: camera_gphoto2.AutoExposureResult, preview: Any) -> None:
        self.capture_result = result
        self.preview_image = preview
        self.image_height, self.image_width = preview.shape[:2]
        image_max = result.final_capture.decoded.to_jsonable().get("image_max") if result.final_capture.decoded else None
        shutter = result.final_capture.settings.shutter_speed
        self.status.set(f"已加载: {self.image_width}x{self.image_height}, shutter={shutter}, max={image_max}")
        self.fit_to_window()

    def fit_to_window(self) -> None:
        if self.preview_image is None:
            return
        self.root.update_idletasks()
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        self.zoom = min(canvas_w / self.image_width, canvas_h / self.image_height) * 0.96
        self.zoom = max(0.02, min(8.0, self.zoom))
        self.offset_x = (canvas_w - self.image_width * self.zoom) / 2
        self.offset_y = (canvas_h - self.image_height * self.zoom) / 2
        self.redraw()

    def redraw(self) -> None:
        self.canvas.delete("all")
        if self.preview_image is None:
            self.canvas.create_text(30, 30, anchor="nw", fill="#f0f0f0", text=self.status.get())
            return

        scaled = resize_nearest_rgb(self.preview_image, self.zoom)
        self.photo = tk.PhotoImage(data=rgb_to_ppm_photo_data(scaled), format="PPM")
        self.image_item = self.canvas.create_image(self.offset_x, self.offset_y, anchor="nw", image=self.photo)

        for index, quad in enumerate(self.quads):
            self.draw_quad(quad, selected=index == self.selected_quad)
        if self.create_preview is not None:
            self.draw_quad(self.create_preview, selected=False, preview=True)
        self._update_confirm_state()

    def draw_quad(self, quad: Quad, *, selected: bool, preview: bool = False) -> None:
        coords: list[float] = []
        for point in quad:
            x, y = self.image_to_canvas(point)
            coords.extend([x, y])
        color = "#ffcf40" if selected else "#40d8ff"
        if preview:
            color = "#b0b0b0"
        self.canvas.create_polygon(coords, outline=color, fill="", width=2, tags=("quad",))
        for vertex_index, point in enumerate(quad):
            x, y = self.image_to_canvas(point)
            radius = HANDLE_RADIUS + (2 if selected else 0)
            self.canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                outline=color,
                fill="#202020",
                width=2,
                tags=("handle", f"v{vertex_index}"),
            )

    def image_to_canvas(self, point: Point) -> Point:
        x, y = point
        return (x * self.zoom + self.offset_x, y * self.zoom + self.offset_y)

    def canvas_to_image(self, point: Point) -> Point:
        x, y = point
        return ((x - self.offset_x) / self.zoom, (y - self.offset_y) / self.zoom)

    def on_left_press(self, event: tk.Event) -> None:
        if self.preview_image is None:
            return
        canvas_point = (float(event.x), float(event.y))
        hit = self.hit_test(canvas_point)
        if hit is not None:
            mode, quad_index, sub_index = hit
            self.selected_quad = quad_index
            self.drag = DragState(
                mode=mode,
                start_canvas=canvas_point,
                start_image=self.canvas_to_image(canvas_point),
                quad_index=quad_index,
                vertex_index=sub_index if mode == "vertex" else None,
                edge_index=sub_index if mode == "edge" else None,
                original_quad=list(self.quads[quad_index]),
            )
            self.redraw()
            return

        image_point = clamp_point(self.canvas_to_image(canvas_point), self.image_width, self.image_height)
        self.selected_quad = None
        self.drag = DragState(mode="create", start_canvas=canvas_point, start_image=image_point)
        self.create_preview = [image_point, image_point, image_point, image_point]
        self.redraw()

    def on_left_drag(self, event: tk.Event) -> None:
        if self.preview_image is None or self.drag is None:
            return
        image_point = clamp_point(self.canvas_to_image((float(event.x), float(event.y))), self.image_width, self.image_height)
        if self.drag.mode == "create" and self.drag.start_image is not None:
            self.create_preview = rect_to_quad(self.drag.start_image, image_point)
        elif self.drag.original_quad is not None and self.drag.start_image is not None and self.drag.quad_index is not None:
            dx = image_point[0] - self.drag.start_image[0]
            dy = image_point[1] - self.drag.start_image[1]
            quad = list(self.drag.original_quad)
            if self.drag.mode == "move":
                self.quads[self.drag.quad_index] = translate_quad(quad, dx, dy, self.image_width, self.image_height)
            elif self.drag.mode == "vertex" and self.drag.vertex_index is not None:
                quad[self.drag.vertex_index] = image_point
                self.quads[self.drag.quad_index] = clamp_quad(quad, self.image_width, self.image_height)
            elif self.drag.mode == "edge" and self.drag.edge_index is not None:
                first = self.drag.edge_index
                second = (first + 1) % 4
                quad[first] = clamp_point((quad[first][0] + dx, quad[first][1] + dy), self.image_width, self.image_height)
                quad[second] = clamp_point((quad[second][0] + dx, quad[second][1] + dy), self.image_width, self.image_height)
                self.quads[self.drag.quad_index] = quad
        self.redraw()

    def on_left_release(self, event: tk.Event) -> None:
        if self.drag is not None and self.drag.mode == "create" and self.create_preview is not None:
            width = abs(self.create_preview[1][0] - self.create_preview[0][0])
            height = abs(self.create_preview[2][1] - self.create_preview[1][1])
            if width >= MIN_CREATE_PIXELS and height >= MIN_CREATE_PIXELS:
                self.quads.append(self.create_preview)
                self.selected_quad = len(self.quads) - 1
        self.create_preview = None
        self.drag = None
        self.redraw()

    def on_pan_press(self, event: tk.Event) -> None:
        self.drag = DragState(mode="pan", start_canvas=(float(event.x), float(event.y)))

    def on_pan_drag(self, event: tk.Event) -> None:
        if self.drag is None or self.drag.mode != "pan":
            return
        x, y = float(event.x), float(event.y)
        self.offset_x += x - self.drag.start_canvas[0]
        self.offset_y += y - self.drag.start_canvas[1]
        self.drag.start_canvas = (x, y)
        self.redraw()

    def on_mouse_wheel(self, event: tk.Event) -> None:
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self.zoom_at(event.x, event.y, factor)

    def zoom_at(self, canvas_x: float, canvas_y: float, factor: float) -> None:
        if self.preview_image is None:
            return
        before = self.canvas_to_image((canvas_x, canvas_y))
        self.zoom = max(0.02, min(8.0, self.zoom * factor))
        self.offset_x = canvas_x - before[0] * self.zoom
        self.offset_y = canvas_y - before[1] * self.zoom
        self.redraw()

    def hit_test(self, canvas_point: Point) -> tuple[str, int, int | None] | None:
        image_point = self.canvas_to_image(canvas_point)
        for quad_index in range(len(self.quads) - 1, -1, -1):
            quad = self.quads[quad_index]
            for vertex_index, point in enumerate(quad):
                handle = self.image_to_canvas(point)
                if math.hypot(canvas_point[0] - handle[0], canvas_point[1] - handle[1]) <= HANDLE_RADIUS + 4:
                    return ("vertex", quad_index, vertex_index)
            for edge_index in range(4):
                start = self.image_to_canvas(quad[edge_index])
                end = self.image_to_canvas(quad[(edge_index + 1) % 4])
                if distance_to_segment(canvas_point, start, end) <= EDGE_HIT_DISTANCE:
                    return ("edge", quad_index, edge_index)
            if point_in_polygon(image_point, quad):
                return ("move", quad_index, None)
        return None

    def delete_selected(self) -> None:
        if self.selected_quad is None:
            return
        del self.quads[self.selected_quad]
        self.selected_quad = None
        self.redraw()

    def clear_quads(self) -> None:
        self.quads.clear()
        self.selected_quad = None
        self.redraw()

    def auto_detect_blocks(self) -> None:
        if self.preview_image is None:
            return
        try:
            rows = int(self.grid_rows.get())
            cols = int(self.grid_cols.get())
            quads = detect_color_checker_quads(self.preview_image, rows=rows, cols=cols)
        except Exception as exc:
            messagebox.showerror("自动识别失败", str(exc))
            return
        self.quads = quads
        self.selected_quad = None
        self.block_count.set(rows * cols)
        self.status.set(f"已自动识别 {rows}x{cols} 色块")
        self.redraw()

    def save_config(self) -> None:
        if self.capture_result is None or self.preview_image is None:
            return
        try:
            target_count = int(self.block_count.get())
            config = build_location_config(
                target_block_count=target_count,
                image_width=self.image_width,
                image_height=self.image_height,
                quads=self.quads,
                capture=self.capture_result.final_capture,
            )
            path = save_location_config(config)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.status.set(f"已保存: {path}")
        messagebox.showinfo("已保存", str(path))

    def _update_confirm_state(self) -> None:
        try:
            target = int(self.block_count.get())
        except (tk.TclError, ValueError):
            target = -1
        enabled = self.capture_result is not None and target > 0 and len(self.quads) == target
        self.confirm_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)


def rect_to_quad(start: Point, end: Point) -> Quad:
    x1, y1 = start
    x2, y2 = end
    left = min(x1, x2)
    right = max(x1, x2)
    top = min(y1, y2)
    bottom = max(y1, y2)
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture a calibration image and mark color-block locations.")
    parser.add_argument("--blocks", type=int, default=24, help="Expected total color-block count.")
    parser.add_argument("--rows", type=int, default=4, help="Grid row count for automatic block detection.")
    parser.add_argument("--cols", type=int, default=6, help="Grid column count for automatic block detection.")
    parser.add_argument("--target-max", type=int, default=DEFAULT_TARGET_MAX)
    parser.add_argument("--iso", default=DEFAULT_ISO)
    parser.add_argument("--aperture", default=DEFAULT_APERTURE)
    parser.add_argument("--min-shutter-speed", default=DEFAULT_MIN_SHUTTER_SPEED)
    parser.add_argument("--max-shutter-speed", default=DEFAULT_MAX_SHUTTER_SPEED)
    parser.add_argument("--max-exposure-trials", type=int, default=DEFAULT_MAX_EXPOSURE_TRIALS)
    parser.add_argument("--model", default=camera_gphoto2.DEFAULT_CAMERA_MODEL)
    parser.add_argument("--port")
    parser.add_argument("--gphoto2", default="gphoto2")
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = tk.Tk()
    LocationPickerApp(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
