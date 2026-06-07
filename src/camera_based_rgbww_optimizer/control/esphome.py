from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from camera_based_rgbww_optimizer.paths import PROJECT_ROOT

DEFAULT_HOST = os.environ.get("CAMERA_BASED_RGBWW_OPTIMIZER_ESPHOME_HOST", "bedroom-rgbww-strip.local")
DEFAULT_PORT = 6053
DEFAULT_EXPECTED_NAME = os.environ.get("CAMERA_BASED_RGBWW_OPTIMIZER_ESPHOME_EXPECTED_NAME", "bedroom-rgbww-strip")
DEFAULT_SECRETS_PATH = PROJECT_ROOT / "firmware/esphome/secrets.yaml"
DEFAULT_CURVE_PATH = PROJECT_ROOT / "config/channel/code-duty-curve.json"

SET_SERVICE = "set_rgbww_12bit"
OFF_SERVICE = "all_off"
CHANNEL_ORDER = ("cw", "ww", "r", "g", "b")


def light(
    cw: Any,
    ww: Any,
    r: Any,
    g: Any,
    b: Any,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    expected_name: str = DEFAULT_EXPECTED_NAME,
    secrets_path: str | Path = DEFAULT_SECRETS_PATH,
    curve_path: str | Path | None = DEFAULT_CURVE_PATH,
) -> dict[str, int]:
    """Set raw RGBWW channel codes through ESPHome Native API.

    Channel argument order is `cw, ww, r, g, b` for calibration convenience.
    If a code-duty curve file exists, values are treated as desired linear
    brightness codes and mapped to calibrated PWM codes. Without a curve file,
    values are converted with `int()` and are not range-clamped in this script.
    If all converted values are zero, the ESPHome `all_off` action is called.
    """

    return asyncio.run(
        async_light(cw, ww, r, g, b, host=host, port=port, expected_name=expected_name, secrets_path=secrets_path, curve_path=curve_path)
    )


async def async_light(
    cw: Any,
    ww: Any,
    r: Any,
    g: Any,
    b: Any,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    expected_name: str = DEFAULT_EXPECTED_NAME,
    secrets_path: str | Path = DEFAULT_SECRETS_PATH,
    curve_path: str | Path | None = DEFAULT_CURVE_PATH,
) -> dict[str, int]:
    import aioesphomeapi

    curve = _load_code_duty_curve_if_present(curve_path)
    payload = _build_payload(cw, ww, r, g, b, curve=curve)
    api_key = _load_api_encryption_key(secrets_path)

    client = aioesphomeapi.APIClient(
        host,
        port,
        None,
        noise_psk=api_key,
        expected_name=expected_name,
    )
    await client.connect(login=True)
    try:
        _, _, services = await client.device_info_and_list_entities()
        by_name = {service.name: service for service in services}
        if _is_all_zero(payload):
            await client.execute_service(by_name[OFF_SERVICE], {})
        else:
            await client.execute_service(by_name[SET_SERVICE], payload)
        return payload
    finally:
        await client.disconnect()


def _build_payload(cw: Any, ww: Any, r: Any, g: Any, b: Any, *, curve: dict[str, Any] | None = None) -> dict[str, int]:
    values = {"cw": int(cw), "ww": int(ww), "r": int(r), "g": int(g), "b": int(b)}
    if curve is not None:
        values = _apply_code_duty_curve(values, curve)
    return {
        "red": values["r"],
        "green": values["g"],
        "blue": values["b"],
        "warm_white": values["ww"],
        "cold_white": values["cw"],
    }


def _load_code_duty_curve_if_present(curve_path: str | Path | None) -> dict[str, Any] | None:
    if curve_path is None:
        return None
    path = Path(curve_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_code_duty_curve(values: dict[str, int], curve: dict[str, Any]) -> dict[str, int]:
    return {channel: _map_code_duty_value(channel, values[channel], curve) for channel in CHANNEL_ORDER}


def _map_code_duty_value(channel: str, value: int, curve: dict[str, Any]) -> int:
    channel_curve = curve.get("channels", {}).get(channel)
    if not channel_curve:
        return int(value)

    max_code = int(channel_curve.get("max_code", curve.get("max_code", 4095)))
    target_code = min(max(int(value), 0), max_code)
    points = _extract_curve_points(channel_curve)
    if not points:
        return target_code
    pwm_code = _interpolate_curve(points, target_code)
    return min(max(int(round(pwm_code)), 0), max_code)


def _extract_curve_points(channel_curve: dict[str, Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for point in channel_curve.get("points", []):
        points.append((float(point["target_code"]), float(point["pwm_code"])))
    return sorted(points)


def _interpolate_curve(points: list[tuple[float, float]], target_code: float) -> float:
    if target_code <= points[0][0]:
        return points[0][1]
    for index in range(1, len(points)):
        left_x, left_y = points[index - 1]
        right_x, right_y = points[index]
        if target_code <= right_x:
            if right_x == left_x:
                return right_y
            ratio = (target_code - left_x) / (right_x - left_x)
            return left_y + ratio * (right_y - left_y)
    return points[-1][1]


def _is_all_zero(payload: dict[str, int]) -> bool:
    return all(value == 0 for value in payload.values())


def _load_api_encryption_key(secrets_path: str | Path) -> str:
    text = Path(secrets_path).read_text(encoding="utf-8")
    match = re.search(r"^api_encryption_key:\s*[\"']?([^\"'\n]+)[\"']?\s*$", text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"api_encryption_key not found in {secrets_path}")
    return match.group(1)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Set RGBWW ESPHome channel codes.")
    parser.add_argument("cw", type=int)
    parser.add_argument("ww", type=int)
    parser.add_argument("r", type=int)
    parser.add_argument("g", type=int)
    parser.add_argument("b", type=int)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--expected-name", default=DEFAULT_EXPECTED_NAME)
    parser.add_argument("--curve", type=Path, default=DEFAULT_CURVE_PATH, help="Optional code-duty curve JSON path.")
    parser.add_argument("--no-curve", action="store_true", help="Disable code-duty curve correction.")
    args = parser.parse_args()

    curve_path = None if args.no_curve else args.curve
    payload = light(
        args.cw,
        args.ww,
        args.r,
        args.g,
        args.b,
        host=args.host,
        port=args.port,
        expected_name=args.expected_name,
        curve_path=curve_path,
    )
    action = OFF_SERVICE if _is_all_zero(payload) else SET_SERVICE
    print(f"{action}: {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
