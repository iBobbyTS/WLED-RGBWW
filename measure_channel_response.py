from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence

import camera_gphoto2
import esphome


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_TMP_DIR = PROJECT_ROOT / "tmp"
DEFAULT_OUTPUT_ROOT = PROJECT_TMP_DIR / "channel-response"
DEFAULT_CODE_VALUES = (
    1,
    2,
    3,
    4,
    6,
    8,
    12,
    16,
    24,
    32,
    48,
    64,
    96,
    128,
    192,
    256,
    384,
    512,
    768,
    1024,
    1536,
    2048,
    3072,
    4095,
)
DEFAULT_CHANNELS = ("cw", "ww", "r", "g", "b")
DEFAULT_SAFE_CODE_LIMIT = 1024
DEFAULT_SETTLE_SECONDS = 0.5
DEFAULT_DECODE_FORMATS = ("npy",)

LightFn = Callable[..., dict[str, int]]
AutoExposeFn = Callable[..., camera_gphoto2.AutoExposureResult]


def parse_code_values(value: str) -> tuple[int, ...]:
    tokens = [token.strip() for token in value.replace(",", " ").split()]
    if not tokens:
        raise argparse.ArgumentTypeError("at least one code value is required")
    codes: list[int] = []
    for token in tokens:
        try:
            code = int(token)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid code value: {token}") from exc
        if code < 0:
            raise argparse.ArgumentTypeError("code values must be non-negative")
        codes.append(code)
    return tuple(codes)


def parse_channels(value: str) -> tuple[str, ...]:
    channels = tuple(token.strip().lower() for token in value.replace(",", " ").split() if token.strip())
    if not channels:
        raise argparse.ArgumentTypeError("at least one channel is required")
    unsupported = [channel for channel in channels if channel not in DEFAULT_CHANNELS]
    if unsupported:
        raise argparse.ArgumentTypeError(f"unsupported channel(s): {', '.join(unsupported)}")
    return channels


def parse_roi(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height")
    try:
        x, y, width, height = (int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ROI: {value}") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("ROI width and height must be positive")
    if x < 0 or y < 0:
        raise argparse.ArgumentTypeError("ROI x and y must be non-negative")
    return {"type": "roi", "x": x, "y": y, "width": width, "height": height}


def parse_block_indices(value: str) -> str | tuple[int, ...]:
    normalized = value.strip().lower()
    if normalized == "all":
        return "all"
    indices: list[int] = []
    for token in normalized.replace(",", " ").split():
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start <= 0 or end < start:
                raise argparse.ArgumentTypeError(f"invalid block range: {token}")
            indices.extend(range(start, end + 1))
        else:
            index = int(token)
            if index <= 0:
                raise argparse.ArgumentTypeError("block indices are 1-based")
            indices.append(index)
    if not indices:
        raise argparse.ArgumentTypeError("at least one block index is required")
    return tuple(dict.fromkeys(indices))


def build_channel_command(channel: str, code: int) -> dict[str, int]:
    command = {"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0}
    command[channel] = int(code)
    return command


def light_args(command: dict[str, int]) -> tuple[int, int, int, int, int]:
    return (command["cw"], command["ww"], command["r"], command["g"], command["b"])


def build_measurement_plan(
    *,
    channels: Sequence[str],
    codes: Sequence[int],
    max_code: int,
) -> list[dict[str, Any]]:
    if max_code <= 0:
        raise ValueError("max_code must be positive")
    plan: list[dict[str, Any]] = []
    for channel in channels:
        for code in codes:
            command = build_channel_command(channel, code)
            plan.append(
                {
                    "index": len(plan),
                    "channel": channel,
                    "code": int(code),
                    "duty": float(code) / float(max_code),
                    "command": command,
                }
            )
    return plan


def validate_output_range(
    *,
    plan: Sequence[dict[str, Any]],
    max_code: int,
    safe_code_limit: int,
    allow_high_output: bool,
    dry_run: bool,
) -> None:
    if max_code <= 0:
        raise ValueError("max_code must be positive")
    for step in plan:
        code = int(step["code"])
        if code > max_code:
            raise ValueError(f"code {code} exceeds max-code {max_code}")
        if code > safe_code_limit and not allow_high_output and not dry_run:
            raise ValueError(
                f"code {code} exceeds safe-code-limit {safe_code_limit}; "
                "pass --allow-high-output after confirming the sweep is safe"
            )


def shutter_seconds(shutter_speed: str) -> float:
    value = shutter_speed.strip().lower()
    if value == "bulb":
        raise ValueError("bulb shutter speed cannot be normalized")
    return float(Fraction(value))


def find_npy_output(capture: camera_gphoto2.CaptureResult) -> Path:
    if capture.decoded is None:
        raise RuntimeError("auto exposure did not return decoded output")
    for output_file in capture.decoded.output_files:
        if output_file.suffix == ".npy":
            return output_file
    raise RuntimeError("auto exposure did not produce a .npy decoded output")


def load_location_regions(path: Path, block_indices: str | tuple[int, ...]) -> list[dict[str, Any]]:
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


def build_region_specs(
    *,
    rois: Sequence[dict[str, Any]] | None,
    location_config: Path | None,
    block_indices: str | tuple[int, ...],
) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for index, roi in enumerate(rois or (), start=1):
        regions.append({"name": f"roi_{index}", **roi})
    if location_config is not None:
        regions.extend(load_location_regions(location_config, block_indices))
    if not regions:
        regions.append({"type": "full", "name": "full_image"})
    return regions


def summarize_region(array: Any) -> dict[str, Any]:
    if array.size == 0:
        raise ValueError("empty measurement region")
    flat = array.reshape(-1, array.shape[-1]).astype("float64")
    channel_mean = flat.mean(axis=0)
    channel_median = _percentile(flat, 50)
    channel_p10 = _percentile(flat, 10)
    channel_p90 = _percentile(flat, 90)
    channel_min = flat.min(axis=0)
    channel_max = flat.max(axis=0)
    channel_std = flat.std(axis=0)
    return {
        "pixel_count": int(flat.shape[0]),
        "mean": float(flat.mean()),
        "median": float(_scalar_percentile(flat, 50)),
        "min": float(flat.min()),
        "max": float(flat.max()),
        "std": float(flat.std()),
        "channel_mean": _float_list(channel_mean),
        "channel_median": _float_list(channel_median),
        "channel_p10": _float_list(channel_p10),
        "channel_p90": _float_list(channel_p90),
        "channel_min": _float_list(channel_min),
        "channel_max": _float_list(channel_max),
        "channel_std": _float_list(channel_std),
    }


def measure_image_regions(
    image: Any,
    regions: Sequence[dict[str, Any]],
    *,
    shutter_seconds_value: float,
    ambient_regions: dict[str, dict[str, Any]] | None = None,
    ambient_shutter_seconds: float | None = None,
    numpy_module: Any | None = None,
) -> list[dict[str, Any]]:
    np = numpy_module or _import_numpy()
    measurements: list[dict[str, Any]] = []
    for region in regions:
        pixels = _extract_region(np, image, region)
        stats = summarize_region(pixels)
        normalized = _normalize_stats(stats, shutter_seconds_value)
        ambient_subtracted = None
        if ambient_regions is not None and ambient_shutter_seconds is not None:
            ambient = ambient_regions.get(region["name"])
            if ambient is not None:
                ambient_norm = _normalize_stats(ambient, ambient_shutter_seconds)
                ambient_subtracted = {
                    "channel_mean_per_second": _subtract_lists(
                        normalized["channel_mean_per_second"],
                        ambient_norm["channel_mean_per_second"],
                    ),
                    "mean_per_second": normalized["mean_per_second"] - ambient_norm["mean_per_second"],
                }
        measurement = {
            "name": region["name"],
            "type": region["type"],
            "stats": stats,
            "normalized": normalized,
        }
        if "index" in region:
            measurement["index"] = region["index"]
        if ambient_subtracted is not None:
            measurement["ambient_subtracted"] = ambient_subtracted
        measurements.append(measurement)
    return measurements


def run_channel_response(
    *,
    output_root: Path,
    run_name: str | None,
    channels: Sequence[str],
    codes: Sequence[int],
    max_code: int,
    safe_code_limit: int,
    allow_high_output: bool,
    dry_run: bool,
    include_ambient: bool,
    settle_seconds: float,
    regions: Sequence[dict[str, Any]],
    target_max: int,
    iso: str,
    aperture: str,
    image_format: str,
    min_shutter_speed: str,
    max_shutter_speed: str,
    max_trials: int,
    max_captures: int,
    decode_formats: Sequence[str],
    output_json: Path | None = None,
    light_fn: LightFn = esphome.light,
    auto_expose_fn: AutoExposeFn = camera_gphoto2.auto_expose_capture,
) -> Path:
    plan = build_measurement_plan(channels=channels, codes=codes, max_code=max_code)
    validate_output_range(
        plan=plan,
        max_code=max_code,
        safe_code_limit=safe_code_limit,
        allow_high_output=allow_high_output,
        dry_run=dry_run,
    )
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = output_root / (run_name or timestamp)
    camera_dir = run_dir / "camera"
    decode_dir = run_dir / "decoded"
    if output_json is None:
        output_json = run_dir / "channel-response.json"
    else:
        output_json = Path(output_json)
        run_dir = output_json.parent
        camera_dir = run_dir / "camera"
        decode_dir = run_dir / "decoded"
    run_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "measurement": {
            "kind": "wled_rgbww_channel_code_response",
            "channels": list(channels),
            "codes": [int(code) for code in codes],
            "max_code": int(max_code),
            "safe_code_limit": int(safe_code_limit),
            "allow_high_output": bool(allow_high_output),
            "settle_seconds": float(settle_seconds),
            "include_ambient": bool(include_ambient),
            "regions": _jsonable_regions(regions),
            "notes": [
                "Signal values are decoded linear camera RGB and normalized by shutter seconds.",
                "Ray120c is not part of this measurement; this records the WLED channel response only.",
            ],
        },
        "camera": {
            "target_max": int(target_max),
            "iso": str(iso),
            "aperture": str(aperture),
            "image_format": str(image_format),
            "min_shutter_speed": str(min_shutter_speed),
            "max_shutter_speed": str(max_shutter_speed),
            "max_trials": int(max_trials),
            "max_captures": int(max_captures),
            "decode_formats": list(decode_formats),
        },
        "plan": plan,
        "ambient": None,
        "measurements": [],
        "status": "planned" if dry_run else "running",
    }
    _write_json(output_json, result)
    if dry_run:
        result["status"] = "dry_run"
        _write_json(output_json, result)
        return output_json

    ambient_stats_by_region: dict[str, dict[str, Any]] | None = None
    ambient_shutter_seconds: float | None = None
    try:
        light_fn(*light_args({"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0}))
        time.sleep(max(0.0, settle_seconds))
        if include_ambient:
            ambient_capture = _capture_auto_exposed(
                auto_expose_fn=auto_expose_fn,
                camera_dir=camera_dir,
                decode_dir=decode_dir,
                filename_template="ambient.%C",
                target_max=target_max,
                iso=iso,
                aperture=aperture,
                image_format=image_format,
                min_shutter_speed=min_shutter_speed,
                max_shutter_speed=max_shutter_speed,
                max_trials=max_trials,
                max_captures=max_captures,
                decode_formats=decode_formats,
            )
            ambient_shutter_seconds = shutter_seconds(ambient_capture.final_capture.settings.shutter_speed)
            ambient_image = _load_npy(find_npy_output(ambient_capture.final_capture))
            ambient_regions = measure_image_regions(
                ambient_image,
                regions,
                shutter_seconds_value=ambient_shutter_seconds,
            )
            ambient_stats_by_region = {
                region["name"]: region["stats"]
                for region in ambient_regions
            }
            result["ambient"] = {
                "auto_exposure": ambient_capture.to_jsonable(),
                "shutter_seconds": ambient_shutter_seconds,
                "regions": ambient_regions,
            }
            _write_json(output_json, result)

        for step in plan:
            command = step["command"]
            light_fn(*light_args(command))
            time.sleep(max(0.0, settle_seconds))
            capture = _capture_auto_exposed(
                auto_expose_fn=auto_expose_fn,
                camera_dir=camera_dir,
                decode_dir=decode_dir,
                filename_template=f"{step['index']:04d}-{step['channel']}-{step['code']}.%C",
                target_max=target_max,
                iso=iso,
                aperture=aperture,
                image_format=image_format,
                min_shutter_speed=min_shutter_speed,
                max_shutter_speed=max_shutter_speed,
                max_trials=max_trials,
                max_captures=max_captures,
                decode_formats=decode_formats,
            )
            seconds = shutter_seconds(capture.final_capture.settings.shutter_speed)
            image = _load_npy(find_npy_output(capture.final_capture))
            region_measurements = measure_image_regions(
                image,
                regions,
                shutter_seconds_value=seconds,
                ambient_regions=ambient_stats_by_region,
                ambient_shutter_seconds=ambient_shutter_seconds,
            )
            result["measurements"].append(
                {
                    **step,
                    "auto_exposure": capture.to_jsonable(),
                    "shutter_seconds": seconds,
                    "regions": region_measurements,
                }
            )
            _write_json(output_json, result)

        result["status"] = "complete"
        _write_json(output_json, result)
        return output_json
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        _write_json(output_json, result)
        raise
    finally:
        try:
            light_fn(*light_args({"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0}))
        except Exception:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure WLED RGBWW per-channel code/duty response with auto exposure.")
    parser.add_argument("--channels", type=parse_channels, default=DEFAULT_CHANNELS)
    parser.add_argument(
        "--codes",
        type=parse_code_values,
        default=DEFAULT_CODE_VALUES,
        help="Comma or space separated channel code values.",
    )
    parser.add_argument("--max-code", type=int, default=4095)
    parser.add_argument("--safe-code-limit", type=int, default=DEFAULT_SAFE_CODE_LIMIT)
    parser.add_argument("--allow-high-output", action="store_true")
    parser.add_argument("--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-name")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--roi", type=parse_roi, action="append", help="Measurement ROI as x,y,width,height. Repeatable.")
    parser.add_argument("--location-config", type=Path, help="Location picker JSON with color-block quadrilaterals.")
    parser.add_argument("--block-indices", type=parse_block_indices, default="all")
    parser.add_argument("--no-ambient", action="store_true", help="Skip the initial all-off ambient capture.")
    parser.add_argument("--target-max", type=int, default=camera_gphoto2.DEFAULT_AUTO_EXPOSURE_MAX)
    parser.add_argument("--iso", default=camera_gphoto2.DEFAULT_ISO)
    parser.add_argument("--aperture", default=camera_gphoto2.DEFAULT_APERTURE)
    parser.add_argument("--image-format", default=camera_gphoto2.DEFAULT_IMAGE_FORMAT)
    parser.add_argument("--min-shutter-speed", default=camera_gphoto2.DEFAULT_MIN_SHUTTER_SPEED)
    parser.add_argument("--max-shutter-speed", default=camera_gphoto2.DEFAULT_MAX_SHUTTER_SPEED)
    parser.add_argument("--max-trials", type=int, default=camera_gphoto2.DEFAULT_AUTO_EXPOSURE_MAX_TRIALS)
    parser.add_argument("--max-captures", type=int, default=camera_gphoto2.DEFAULT_AUTO_EXPOSURE_MAX_CAPTURES)
    parser.add_argument("--decode-format", dest="decode_formats", action="append")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    regions = build_region_specs(
        rois=args.roi,
        location_config=args.location_config,
        block_indices=args.block_indices,
    )
    output_json = run_channel_response(
        output_root=args.output_dir,
        run_name=args.run_name,
        channels=args.channels,
        codes=args.codes,
        max_code=args.max_code,
        safe_code_limit=args.safe_code_limit,
        allow_high_output=args.allow_high_output,
        dry_run=args.dry_run,
        include_ambient=not args.no_ambient,
        settle_seconds=args.settle_seconds,
        regions=regions,
        target_max=args.target_max,
        iso=args.iso,
        aperture=args.aperture,
        image_format=args.image_format,
        min_shutter_speed=args.min_shutter_speed,
        max_shutter_speed=args.max_shutter_speed,
        max_trials=args.max_trials,
        max_captures=args.max_captures,
        decode_formats=tuple(args.decode_formats or DEFAULT_DECODE_FORMATS),
        output_json=args.output_json,
    )
    print(output_json)
    return 0


def _capture_auto_exposed(
    *,
    auto_expose_fn: AutoExposeFn,
    camera_dir: Path,
    decode_dir: Path,
    filename_template: str,
    target_max: int,
    iso: str,
    aperture: str,
    image_format: str,
    min_shutter_speed: str,
    max_shutter_speed: str,
    max_trials: int,
    max_captures: int,
    decode_formats: Sequence[str],
) -> camera_gphoto2.AutoExposureResult:
    return auto_expose_fn(
        output_dir=camera_dir,
        filename_template=filename_template,
        target_max=target_max,
        iso=iso,
        aperture=aperture,
        image_format=image_format,
        min_shutter_speed=min_shutter_speed,
        max_shutter_speed=max_shutter_speed,
        max_trials=max_trials,
        max_captures=max_captures,
        decode_output_dir=decode_dir,
        decode_formats=decode_formats,
    )


def _extract_region(np: Any, image: Any, region: dict[str, Any]) -> Any:
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
        return _extract_polygon(np, image, region["points"])
    raise ValueError(f"unsupported region type: {region['type']}")


def _extract_polygon(np: Any, image: Any, points: Sequence[tuple[float, float]]) -> Any:
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


def _normalize_stats(stats: dict[str, Any], seconds: float) -> dict[str, Any]:
    if seconds <= 0:
        raise ValueError("shutter seconds must be positive")
    return {
        "shutter_seconds": float(seconds),
        "mean_per_second": float(stats["mean"]) / seconds,
        "channel_mean_per_second": [float(value) / seconds for value in stats["channel_mean"]],
        "channel_median_per_second": [float(value) / seconds for value in stats["channel_median"]],
    }


def _subtract_lists(left: Sequence[float], right: Sequence[float]) -> list[float]:
    return [float(a) - float(b) for a, b in zip(left, right)]


def _percentile(flat: Any, percentile: float) -> list[float]:
    np = _import_numpy()
    return _float_list(np.percentile(flat, percentile, axis=0))


def _scalar_percentile(flat: Any, percentile: float) -> float:
    np = _import_numpy()
    return float(np.percentile(flat, percentile))


def _float_list(values: Any) -> list[float]:
    return [float(value) for value in values]


def _load_npy(path: Path) -> Any:
    np = _import_numpy()
    return np.load(path)


def _import_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required for channel response measurement") from exc
    return np


def _jsonable_regions(regions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    jsonable = []
    for region in regions:
        item = dict(region)
        if "points" in item:
            item["points"] = [{"x": float(x), "y": float(y)} for x, y in item["points"]]
        jsonable.append(item)
    return jsonable


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
