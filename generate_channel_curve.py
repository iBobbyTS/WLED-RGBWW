from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_PATH = PROJECT_ROOT / "tmp/channel-response/merged/channel-code-duty-response-merged.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "config/channel/code-duty-curve.json"
DEFAULT_BRIGHTNESS_KEY = "max_ambient_subtracted_mean_per_second"
CHANNEL_ORDER = ("cw", "ww", "r", "g", "b")


def load_merged_response(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_code_duty_curve(
    merged: dict[str, Any],
    *,
    brightness_key: str = DEFAULT_BRIGHTNESS_KEY,
    max_code: int | None = None,
) -> dict[str, Any]:
    max_code = int(max_code or max(int(code) for code in merged.get("codes", [4095])))
    channels = tuple(channel for channel in merged.get("channels", CHANNEL_ORDER) if channel in CHANNEL_ORDER)
    measurements = list(merged.get("measurements", []))

    curve: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "wled_rgbww_code_duty_curve",
        "source_kind": merged.get("kind"),
        "source_created_at": merged.get("created_at"),
        "source_files": merged.get("source_files", []),
        "brightness_key": brightness_key,
        "max_code": max_code,
        "input": "linear_brightness_code",
        "output": "pwm_code",
        "notes": [
            "Input values are desired linear brightness codes in 0..max_code.",
            "Output values are PWM codes interpolated from measured ambient-subtracted camera response.",
            "Non-monotonic low-end measurements are discarded before building inverse curves.",
        ],
        "channels": {},
    }

    for channel in channels:
        channel_measurements = [item for item in measurements if item.get("channel") == channel]
        curve["channels"][channel] = build_channel_curve(
            channel,
            channel_measurements,
            brightness_key=brightness_key,
            max_code=max_code,
        )

    return curve


def build_channel_curve(
    channel: str,
    measurements: Sequence[dict[str, Any]],
    *,
    brightness_key: str = DEFAULT_BRIGHTNESS_KEY,
    max_code: int = 4095,
) -> dict[str, Any]:
    raw_points = extract_raw_points(measurements, brightness_key=brightness_key, max_code=max_code)
    if not raw_points:
        raise ValueError(f"no usable measurements for channel {channel}")

    full_scale_point = find_full_scale_point(raw_points, max_code=max_code)
    full_scale_response = full_scale_point["response_per_second"]
    if full_scale_response <= 0:
        raise ValueError(f"full-scale response for channel {channel} must be positive")

    kept_points: list[dict[str, Any]] = []
    discarded_points: list[dict[str, Any]] = []
    inverse_points: list[dict[str, float | int]] = [{"target_code": 0.0, "pwm_code": 0}]
    last_response = 0.0

    for point in sorted(raw_points, key=lambda item: item["code"]):
        code = point["code"]
        response = point["response_per_second"]
        if response <= 0:
            discarded_points.append({**point, "reason": "non_positive_response"})
            continue
        if code != full_scale_point["code"] and response >= full_scale_response:
            discarded_points.append({**point, "reason": "exceeds_full_scale_response"})
            continue
        if response <= last_response:
            discarded_points.append({**point, "reason": "non_monotonic_response"})
            continue

        kept_points.append(point)
        inverse_points.append(
            {
                "target_code": round(response / full_scale_response * max_code, 6),
                "pwm_code": code,
            }
        )
        last_response = response

    if inverse_points[-1]["pwm_code"] != full_scale_point["code"]:
        inverse_points.append({"target_code": float(max_code), "pwm_code": full_scale_point["code"]})

    return {
        "max_code": max_code,
        "full_scale_code": full_scale_point["code"],
        "full_scale_response_per_second": full_scale_response,
        "points": inverse_points,
        "measured_points": kept_points,
        "discarded_points": discarded_points,
        "measurement_count": len(measurements),
        "usable_measurement_count": len(kept_points),
        "discarded_measurement_count": len(discarded_points),
    }


def extract_raw_points(measurements: Sequence[dict[str, Any]], *, brightness_key: str, max_code: int) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for measurement in measurements:
        if brightness_key not in measurement:
            continue
        code = int(measurement["code"])
        response = float(measurement[brightness_key])
        if not math.isfinite(response):
            continue
        points.append(
            {
                "code": code,
                "duty": float(measurement.get("duty", code / max_code)),
                "response_per_second": response,
            }
        )
    return points


def find_full_scale_point(points: Sequence[dict[str, Any]], *, max_code: int) -> dict[str, Any]:
    for point in points:
        if point["code"] == max_code:
            return point
    return max(points, key=lambda point: point["code"])


def write_curve(curve: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(curve, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate compact code-duty correction curves from merged channel response data.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--brightness-key", default=DEFAULT_BRIGHTNESS_KEY)
    parser.add_argument("--max-code", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    merged = load_merged_response(args.input)
    curve = build_code_duty_curve(merged, brightness_key=args.brightness_key, max_code=args.max_code)
    output = write_curve(curve, args.output)
    measurement_count = sum(channel["usable_measurement_count"] for channel in curve["channels"].values())
    discarded_count = sum(channel["discarded_measurement_count"] for channel in curve["channels"].values())
    print(f"wrote {output}")
    print(f"usable measurements: {measurement_count}, discarded measurements: {discarded_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
