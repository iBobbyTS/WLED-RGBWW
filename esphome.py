from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path
from typing import Any


DEFAULT_HOST = "wled-bedroom-rgbww.local"
DEFAULT_PORT = 6053
DEFAULT_EXPECTED_NAME = "wled-bedroom-rgbww"
DEFAULT_SECRETS_PATH = Path(__file__).resolve().parent / "firmware/esphome/secrets.yaml"

SET_SERVICE = "set_rgbww_12bit"
OFF_SERVICE = "all_off"


def light(
    cw: Any,
    ww: Any,
    r: Any,
    g: Any,
    b: Any,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    secrets_path: str | Path = DEFAULT_SECRETS_PATH,
) -> dict[str, int]:
    """Set raw RGBWW channel codes through ESPHome Native API.

    Channel argument order is `cw, ww, r, g, b` for calibration convenience.
    Values are converted with `int()` and are not range-clamped in this script.
    If all converted values are zero, the ESPHome `all_off` action is called.
    """

    return asyncio.run(async_light(cw, ww, r, g, b, host=host, port=port, secrets_path=secrets_path))


async def async_light(
    cw: Any,
    ww: Any,
    r: Any,
    g: Any,
    b: Any,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    secrets_path: str | Path = DEFAULT_SECRETS_PATH,
) -> dict[str, int]:
    import aioesphomeapi

    payload = _build_payload(cw, ww, r, g, b)
    api_key = _load_api_encryption_key(secrets_path)

    client = aioesphomeapi.APIClient(
        host,
        port,
        None,
        noise_psk=api_key,
        expected_name=DEFAULT_EXPECTED_NAME,
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


def _build_payload(cw: Any, ww: Any, r: Any, g: Any, b: Any) -> dict[str, int]:
    return {
        "red": int(r),
        "green": int(g),
        "blue": int(b),
        "warm_white": int(ww),
        "cold_white": int(cw),
    }


def _is_all_zero(payload: dict[str, int]) -> bool:
    return all(value == 0 for value in payload.values())


def _load_api_encryption_key(secrets_path: str | Path) -> str:
    text = Path(secrets_path).read_text(encoding="utf-8")
    match = re.search(r"^api_encryption_key:\s*[\"']?([^\"'\n]+)[\"']?\s*$", text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"api_encryption_key not found in {secrets_path}")
    return match.group(1)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Set WLED RGBWW ESPHome channel codes.")
    parser.add_argument("cw", type=int)
    parser.add_argument("ww", type=int)
    parser.add_argument("r", type=int)
    parser.add_argument("g", type=int)
    parser.add_argument("b", type=int)
    args = parser.parse_args()

    payload = light(args.cw, args.ww, args.r, args.g, args.b)
    action = OFF_SERVICE if _is_all_zero(payload) else SET_SERVICE
    print(f"{action}: {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
