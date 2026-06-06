from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence

import camera_gphoto2
import esphome
import location_regions


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_TMP_DIR = PROJECT_ROOT / "tmp"
DEFAULT_OUTPUT_ROOT = PROJECT_TMP_DIR / "channel-response"
DEFAULT_AUTO_EXPOSURE_METERING_LOCATION_CONFIG = PROJECT_ROOT / "config" / "location" / "locations-20260605-225800.json"
DEFAULT_CODE_VALUES = (
    4095,
    3072,
    2048,
    1536,
    1024,
    768,
    512,
    384,
    256,
    192,
    128,
    96,
    64,
    48,
    32,
    24,
    16,
    12,
    8,
)
DEFAULT_CHANNELS = ("cw", "ww", "r", "g", "b")
DEFAULT_SAFE_CODE_LIMIT = 1024
DEFAULT_SETTLE_SECONDS = 0.5
DEFAULT_DECODE_FORMATS = ("npy",)
DEFAULT_AMBIENT_ISO = "100"
DEFAULT_AMBIENT_SHUTTER_SPEED = "30"
DEFAULT_AMBIENT_TIMEOUT = 120.0
DEFAULT_AMBIENT_STOP_THRESHOLD_PER_SECOND = 2.0

LightFn = Callable[..., dict[str, int]]
AutoExposeFn = Callable[..., camera_gphoto2.AutoExposureResult]
CaptureFn = Callable[..., camera_gphoto2.CaptureResult]
DecoderFn = Callable[..., camera_gphoto2.DecodeResult]


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
    return location_regions.load_location_regions(path, block_indices)


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


def max_ambient_subtracted_mean_per_second(region_measurements: Sequence[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for measurement in region_measurements:
        ambient_subtracted = measurement.get("ambient_subtracted")
        if not isinstance(ambient_subtracted, dict):
            continue
        channel_values = ambient_subtracted.get("channel_mean_per_second")
        if not isinstance(channel_values, (list, tuple)):
            continue
        values.extend(max(0.0, float(value)) for value in channel_values)
    if not values:
        return None
    return max(values)


def should_stop_channel_at_ambient(
    *,
    shutter_speed: str,
    max_shutter_speed: str,
    region_measurements: Sequence[dict[str, Any]],
    threshold_per_second: float,
) -> bool:
    if threshold_per_second < 0:
        return False
    try:
        at_max_shutter = shutter_seconds(shutter_speed) >= shutter_seconds(max_shutter_speed)
    except ValueError:
        return False
    if not at_max_shutter:
        return False
    signal = max_ambient_subtracted_mean_per_second(region_measurements)
    return signal is not None and signal <= threshold_per_second


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
    auto_exposure_metering_regions: Sequence[dict[str, Any]] | None = None,
    ambient_iso: str = DEFAULT_AMBIENT_ISO,
    ambient_shutter_speed: str = DEFAULT_AMBIENT_SHUTTER_SPEED,
    ambient_stop_threshold_per_second: float = DEFAULT_AMBIENT_STOP_THRESHOLD_PER_SECOND,
    output_json: Path | None = None,
    light_fn: LightFn = esphome.light,
    auto_expose_fn: AutoExposeFn = camera_gphoto2.auto_expose_capture,
    capture_fn: CaptureFn = camera_gphoto2.capture_image,
    decoder_fn: DecoderFn = camera_gphoto2.decode_raw_image,
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
            "ambient_stop_threshold_per_second": float(ambient_stop_threshold_per_second),
            "regions": _jsonable_regions(regions),
            "notes": [
                "Signal values are decoded linear camera RGB and normalized by shutter seconds.",
                "Ray120c is not part of this measurement; this records the WLED channel response only.",
                "Codes are measured from high to low; once a channel is ambient-limited at the longest shutter, lower codes are skipped.",
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
            "ambient": {
                "iso": str(ambient_iso),
                "shutter_speed": str(ambient_shutter_speed),
                "mode": "fixed",
            },
            "auto_exposure_metering": {
                "mode": camera_gphoto2.METERING_MODE_LOCATION if auto_exposure_metering_regions is not None else camera_gphoto2.METERING_MODE_FULL,
                "regions": _jsonable_regions(auto_exposure_metering_regions) if auto_exposure_metering_regions is not None else None,
            },
        },
        "plan": plan,
        "ambient": None,
        "measurements": [],
        "skipped_measurements": [],
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
            ambient_capture = _capture_fixed(
                capture_fn=capture_fn,
                decoder_fn=decoder_fn,
                camera_dir=camera_dir,
                decode_dir=decode_dir,
                filename_template="ambient.%C",
                iso=ambient_iso,
                aperture=aperture,
                image_format=image_format,
                shutter_speed=ambient_shutter_speed,
                decode_formats=decode_formats,
                metering_regions=auto_exposure_metering_regions,
            )
            ambient_shutter_seconds = shutter_seconds(ambient_capture.settings.shutter_speed)
            ambient_image = _load_npy(find_npy_output(ambient_capture))
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
                "capture": ambient_capture.to_jsonable(),
                "shutter_seconds": ambient_shutter_seconds,
                "regions": ambient_regions,
            }
            _write_json(output_json, result)

        stopped_channels: dict[str, dict[str, Any]] = {}
        started_channels: set[str] = set()
        for step in plan:
            if step["channel"] in stopped_channels:
                result["skipped_measurements"].append({**step, "skip_reason": stopped_channels[step["channel"]]})
                _write_json(output_json, result)
                continue
            first_channel_measurement = step["channel"] not in started_channels
            started_channels.add(step["channel"])
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
                metering_regions=auto_exposure_metering_regions,
                initial_shutter_speed=min_shutter_speed if first_channel_measurement else None,
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
            ambient_subtracted_signal = max_ambient_subtracted_mean_per_second(region_measurements)
            stop_at_ambient = should_stop_channel_at_ambient(
                shutter_speed=capture.final_capture.settings.shutter_speed,
                max_shutter_speed=max_shutter_speed,
                region_measurements=region_measurements,
                threshold_per_second=ambient_stop_threshold_per_second,
            )
            stop_reason = None
            if stop_at_ambient:
                stop_reason = {
                    "kind": "ambient_limited",
                    "at_code": int(step["code"]),
                    "max_shutter_speed": str(max_shutter_speed),
                    "threshold_per_second": float(ambient_stop_threshold_per_second),
                    "max_ambient_subtracted_mean_per_second": ambient_subtracted_signal,
                }
                stopped_channels[step["channel"]] = stop_reason
            result["measurements"].append(
                {
                    **step,
                    "auto_exposure": capture.to_jsonable(),
                    "shutter_seconds": seconds,
                    "max_ambient_subtracted_mean_per_second": ambient_subtracted_signal,
                    "stop_reason": stop_reason,
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
    parser.add_argument(
        "--auto-exposure-metering-mode",
        choices=(camera_gphoto2.METERING_MODE_FULL, camera_gphoto2.METERING_MODE_LOCATION),
        default=camera_gphoto2.METERING_MODE_LOCATION,
        help="Use the full decoded image or a saved 24-block location config for auto-exposure metering.",
    )
    parser.add_argument(
        "--auto-exposure-metering-location-config",
        type=Path,
        help="Location picker JSON used when --auto-exposure-metering-mode=location.",
    )
    parser.add_argument("--no-ambient", action="store_true", help="Skip the initial all-off ambient capture.")
    parser.add_argument("--target-max", type=int, default=camera_gphoto2.DEFAULT_AUTO_EXPOSURE_MAX)
    parser.add_argument("--iso", default=camera_gphoto2.DEFAULT_ISO)
    parser.add_argument("--aperture", default=camera_gphoto2.DEFAULT_APERTURE)
    parser.add_argument("--image-format", default=camera_gphoto2.DEFAULT_IMAGE_FORMAT)
    parser.add_argument("--ambient-iso", default=DEFAULT_AMBIENT_ISO)
    parser.add_argument("--ambient-shutter-speed", default=DEFAULT_AMBIENT_SHUTTER_SPEED)
    parser.add_argument(
        "--ambient-stop-threshold-per-second",
        type=float,
        default=DEFAULT_AMBIENT_STOP_THRESHOLD_PER_SECOND,
        help="Stop lower codes for a channel when the longest-exposure ambient-subtracted signal is at or below this value.",
    )
    parser.add_argument("--min-shutter-speed", default=camera_gphoto2.DEFAULT_MIN_SHUTTER_SPEED)
    parser.add_argument("--max-shutter-speed", default=camera_gphoto2.DEFAULT_MAX_SHUTTER_SPEED)
    parser.add_argument("--max-trials", type=int, default=camera_gphoto2.DEFAULT_AUTO_EXPOSURE_MAX_TRIALS)
    parser.add_argument("--max-captures", type=int, default=camera_gphoto2.DEFAULT_AUTO_EXPOSURE_MAX_CAPTURES)
    parser.add_argument("--decode-format", dest="decode_formats", action="append")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def resolve_auto_exposure_metering_location_config(args: argparse.Namespace) -> Path | None:
    if args.auto_exposure_metering_mode != camera_gphoto2.METERING_MODE_LOCATION:
        return None
    return (
        args.auto_exposure_metering_location_config
        or args.location_config
        or DEFAULT_AUTO_EXPOSURE_METERING_LOCATION_CONFIG
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    regions = build_region_specs(
        rois=args.roi,
        location_config=args.location_config,
        block_indices=args.block_indices,
    )
    auto_exposure_metering_regions = None
    auto_exposure_metering_location_config = resolve_auto_exposure_metering_location_config(args)
    if auto_exposure_metering_location_config is not None:
        auto_exposure_metering_regions = camera_gphoto2.load_metering_regions(auto_exposure_metering_location_config)
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
        auto_exposure_metering_regions=auto_exposure_metering_regions,
        ambient_iso=args.ambient_iso,
        ambient_shutter_speed=args.ambient_shutter_speed,
        ambient_stop_threshold_per_second=args.ambient_stop_threshold_per_second,
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
    metering_regions: Sequence[dict[str, Any]] | None = None,
    initial_shutter_speed: str | None = None,
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
        initial_shutter_speed=initial_shutter_speed,
        decode_output_dir=decode_dir,
        decode_formats=decode_formats,
        metering_regions=metering_regions,
    )


def _capture_fixed(
    *,
    capture_fn: CaptureFn,
    decoder_fn: DecoderFn,
    camera_dir: Path,
    decode_dir: Path,
    filename_template: str,
    iso: str,
    aperture: str,
    image_format: str,
    shutter_speed: str,
    decode_formats: Sequence[str],
    metering_regions: Sequence[dict[str, Any]] | None = None,
) -> camera_gphoto2.CaptureResult:
    capture = capture_fn(
        output_dir=camera_dir,
        filename_template=filename_template,
        settings=camera_gphoto2.CaptureSettings(
            iso=iso,
            aperture=aperture,
            shutter_speed=shutter_speed,
            image_format=image_format,
        ),
        timeout=DEFAULT_AMBIENT_TIMEOUT,
    )
    if metering_regions is None:
        decoded = decoder_fn(capture.saved_file, output_dir=decode_dir, formats=decode_formats)
    else:
        decoded = decoder_fn(capture.saved_file, output_dir=decode_dir, formats=decode_formats, metering_regions=metering_regions)
    return camera_gphoto2.CaptureResult(
        connection=capture.connection,
        settings=capture.settings,
        saved_file=capture.saved_file,
        stdout=capture.stdout,
        decoded=decoded,
    )


def _extract_region(np: Any, image: Any, region: dict[str, Any]) -> Any:
    return location_regions.extract_region(np, image, region)


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
    return location_regions.jsonable_regions(regions)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
