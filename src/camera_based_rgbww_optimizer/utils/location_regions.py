from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence


def load_location_regions(path: Path, block_indices: str | tuple[int, ...] = "all") -> list[dict[str, Any]]:
    config = json.loads(path.read_text(encoding="utf-8"))
    wanted = None if block_indices == "all" else set(block_indices)
    regions: list[dict[str, Any]] = []
    for block in config.get("blocks", []):
        index = int(block["index"])
        if wanted is not None and index not in wanted:
            continue
        points = [(float(point["x"]), float(point["y"])) for point in block["points"]]
        regions.append({"type": "polygon", "name": f"block_{index:02d}", "index": index, "points": points})
    if not regions:
        raise ValueError(f"no location regions selected from {path}")
    return regions


def extract_region(np: Any, image: Any, region: dict[str, Any]) -> Any:
    if len(image.shape) != 3:
        raise ValueError("decoded image must be HxWxC")
    if region["type"] == "full":
        return image
    if region["type"] == "roi":
        x = int(region["x"])
        y = int(region["y"])
        width = int(region["width"])
        height = int(region["height"])
        if x + width > image.shape[1] or y + height > image.shape[0]:
            raise ValueError(f"ROI {region['name']} exceeds image bounds")
        return image[y : y + height, x : x + width]
    if region["type"] == "polygon":
        return extract_polygon(np, image, region["points"])
    raise ValueError(f"unsupported region type: {region['type']}")


def extract_polygon(np: Any, image: Any, points: Sequence[tuple[float, float]]) -> Any:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x = max(0, int(math.floor(min(xs))))
    max_x = min(image.shape[1] - 1, int(math.ceil(max(xs))))
    min_y = max(0, int(math.floor(min(ys))))
    max_y = min(image.shape[0] - 1, int(math.ceil(max(ys))))
    if max_x < min_x or max_y < min_y:
        raise ValueError("polygon is outside image bounds")
    yy, xx = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
    px = xx.astype("float64") + 0.5
    py = yy.astype("float64") + 0.5
    inside = np.zeros(px.shape, dtype=bool)
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        crosses = ((y1 > py) != (y2 > py)) & (px < (x2 - x1) * (py - y1) / ((y2 - y1) or 1e-12) + x1)
        inside ^= crosses
    pixels = image[min_y : max_y + 1, min_x : max_x + 1][inside]
    if pixels.size == 0:
        raise ValueError("polygon selected no pixels")
    return pixels


def jsonable_regions(regions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    jsonable = []
    for region in regions:
        item = dict(region)
        if "points" in item:
            item["points"] = [{"x": float(x), "y": float(y)} for x, y in item["points"]]
        jsonable.append(item)
    return jsonable
