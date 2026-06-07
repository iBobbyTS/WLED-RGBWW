from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from camera_based_rgbww_optimizer.paths import PROJECT_ROOT

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "tmp" / "channel-response" / "merged"
DEFAULT_CODES = (
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
DEFAULT_CHANNEL_MAP = {
    "tmp/channel-response/channel-response-20260606-001511/channel-response.json": {"cw": "ww"},
    "tmp/channel-response/channel-response-ww-rgb-20260606-004059/channel-response.json": {"ww": "cw"},
}
CHANNEL_ORDER = ("cw", "ww", "r", "g", "b")


def parse_codes(value: str) -> tuple[int, ...]:
    codes = tuple(int(token.strip()) for token in value.replace(",", " ").split() if token.strip())
    if not codes:
        raise argparse.ArgumentTypeError("at least one code is required")
    return codes


def load_channel_response(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def merge_channel_responses(
    paths: Sequence[Path],
    *,
    codes: Sequence[int] = DEFAULT_CODES,
    channel_maps: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    wanted_codes = set(int(code) for code in codes)
    merged: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "merged_camera_based_rgbww_optimizer_channel_code_response",
        "codes": [int(code) for code in codes],
        "channels": list(CHANNEL_ORDER),
        "channel_aliases": {},
        "source_files": [],
        "measurements": [],
        "notes": [
            "Only measurements useful for code-duty optimization are retained.",
            "White-channel labels are corrected from visual observation: the first run's cw output was warm white, and the second run's ww output was cold white.",
        ],
    }
    by_key: dict[tuple[str, int], dict[str, Any]] = {}
    channel_maps = channel_maps or {}

    for path in paths:
        source = load_channel_response(path)
        source_key = _relative_key(path)
        channel_map = channel_maps.get(source_key, {})
        if channel_map:
            merged["channel_aliases"][source_key] = channel_map
        merged["source_files"].append(
            {
                "path": str(path),
                "status": source.get("status"),
                "created_at": source.get("created_at"),
                "run_dir": source.get("run_dir"),
                "channel_map": channel_map,
            }
        )
        max_code = int(source.get("measurement", {}).get("max_code", 4095))
        for measurement in source.get("measurements", []):
            original_channel = str(measurement["channel"])
            channel = channel_map.get(original_channel, original_channel)
            code = int(measurement["code"])
            if code not in wanted_codes:
                continue
            if channel not in CHANNEL_ORDER:
                continue
            item = compact_measurement(
                measurement,
                source_path=path,
                channel=channel,
                original_channel=original_channel,
                max_code=max_code,
            )
            key = (channel, code)
            if key in by_key:
                raise ValueError(f"duplicate merged measurement for {channel} code {code}")
            by_key[key] = item

    for channel in CHANNEL_ORDER:
        for code in codes:
            item = by_key.get((channel, int(code)))
            if item is not None:
                merged["measurements"].append(item)

    merged["coverage"] = build_coverage(merged["measurements"], codes)
    return merged


def compact_measurement(
    measurement: dict[str, Any],
    *,
    source_path: Path,
    channel: str,
    original_channel: str,
    max_code: int,
) -> dict[str, Any]:
    auto_exposure = measurement.get("auto_exposure", {})
    final = auto_exposure.get("final", {})
    decoded = final.get("decoded", {})
    return {
        "channel": channel,
        "original_channel": original_channel,
        "code": int(measurement["code"]),
        "duty": float(measurement["code"]) / float(max_code),
        "source_file": str(source_path),
        "source_index": int(measurement["index"]),
        "command": remap_command(measurement.get("command", {}), channel=channel, original_channel=original_channel),
        "shutter_seconds": float(measurement["shutter_seconds"]),
        "capture_count": int(auto_exposure.get("capture_count", 0)),
        "final_shutter_speed": final.get("shutter_speed"),
        "decoded_metering_max": _decoded_max(decoded),
        "max_ambient_subtracted_mean_per_second": measurement.get("max_ambient_subtracted_mean_per_second"),
        "regions": [compact_region(region) for region in measurement.get("regions", [])],
    }


def remap_command(command: dict[str, Any], *, channel: str, original_channel: str) -> dict[str, int]:
    remapped = {name: 0 for name in CHANNEL_ORDER}
    for name in CHANNEL_ORDER:
        value = int(command.get(name, 0))
        if name == original_channel:
            remapped[channel] = value
        elif name != channel:
            remapped[name] = value
    return remapped


def compact_region(region: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": region.get("name"),
        "index": region.get("index"),
        "pixel_count": region.get("stats", {}).get("pixel_count"),
        "channel_mean_per_second": region.get("normalized", {}).get("channel_mean_per_second"),
        "channel_median_per_second": region.get("normalized", {}).get("channel_median_per_second"),
        "ambient_subtracted_channel_mean_per_second": region.get("ambient_subtracted", {}).get("channel_mean_per_second"),
        "ambient_subtracted_mean_per_second": region.get("ambient_subtracted", {}).get("mean_per_second"),
    }


def build_coverage(measurements: Sequence[dict[str, Any]], codes: Sequence[int]) -> dict[str, Any]:
    by_channel: dict[str, list[int]] = {channel: [] for channel in CHANNEL_ORDER}
    for measurement in measurements:
        by_channel[measurement["channel"]].append(int(measurement["code"]))
    return {
        channel: {
            "count": len(values),
            "codes": values,
            "missing_codes": [int(code) for code in codes if int(code) not in values],
        }
        for channel, values in by_channel.items()
    }


def write_outputs(merged: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "channel-code-duty-response-merged.json"
    summary_path = output_dir / "channel-code-duty-response-summary.json"
    json_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    summary = {
        "created_at": merged["created_at"],
        "kind": merged["kind"],
        "source_files": merged["source_files"],
        "channel_aliases": merged["channel_aliases"],
        "coverage": merged["coverage"],
        "measurement_count": len(merged["measurements"]),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return json_path, summary_path


def _decoded_max(decoded: dict[str, Any]) -> int | float | None:
    metering = decoded.get("metering")
    if isinstance(metering, dict) and metering.get("image_max") is not None:
        return metering.get("image_max")
    return decoded.get("image_max")


def _relative_key(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge channel response runs into compact code-duty optimization data.")
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        default=[
            PROJECT_ROOT / "tmp" / "channel-response" / "channel-response-20260606-001511" / "channel-response.json",
            PROJECT_ROOT / "tmp" / "channel-response" / "channel-response-ww-rgb-20260606-004059" / "channel-response.json",
        ],
    )
    parser.add_argument("--codes", type=parse_codes, default=DEFAULT_CODES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    merged = merge_channel_responses(args.inputs, codes=args.codes, channel_maps=DEFAULT_CHANNEL_MAP)
    json_path, summary_path = write_outputs(merged, args.output_dir)
    print(json_path)
    print(summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
