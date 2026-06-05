from __future__ import annotations

import argparse
import base64
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
DEFAULT_ISO = "100"
DEFAULT_APERTURE = "4"
DEFAULT_MIN_SHUTTER_SPEED = "1/8000"
DEFAULT_MAX_SHUTTER_SPEED = "30"
DEFAULT_CANVAS_WIDTH = 1200
DEFAULT_CANVAS_HEIGHT = 800
HANDLE_RADIUS = 7
EDGE_HIT_DISTANCE = 8
MIN_CREATE_PIXELS = 6

Point = tuple[float, float]
Quad = list[Point]


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


def rgb_to_ppm_photo_data(image: Any) -> str:
    height, width = image.shape[:2]
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return base64.b64encode(header + image.tobytes()).decode("ascii")


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
    parser.add_argument("--target-max", type=int, default=DEFAULT_TARGET_MAX)
    parser.add_argument("--iso", default=DEFAULT_ISO)
    parser.add_argument("--aperture", default=DEFAULT_APERTURE)
    parser.add_argument("--min-shutter-speed", default=DEFAULT_MIN_SHUTTER_SPEED)
    parser.add_argument("--max-shutter-speed", default=DEFAULT_MAX_SHUTTER_SPEED)
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
