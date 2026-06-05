---
name: wled-rgbww
description: Use when working on the WLED RGBWW optimizer, WLED channel control, camera-based calibration, or Ray120c matching workflow.
---

# WLED RGBWW Project Skill

## Project Facts

- WLED host: `wled-bedroom.local`
- Controller: GLEDOPTO `GL-C-211WL` ESP32 WLED PWM controller running WLED firmware
- Fixture: PWM RGBWW strip with five physical channels
- Observed WLED config: WLED `0.15.1`, analog type `45` / RGB + warm white + cold white, GPIO order `[19, 18, 17, 16, 4]`, PWM frequency `19531 Hz`, light capability `lc=7`
- Physical controls observed by the user: restart button and reset button
- WLED web OTA page is available at `/update` while WLED can boot; this does not replace serial bootloader recovery.
- WLED config reports button 0 on `GPIO0` and IR on `GPIO13`; physically verify this against the reset/restart buttons and documented `IO33` DIY interface before relying on it.
- Planned optimizer language: Python
- Reference light: Aputure Amaran Ray120c at 100% brightness
- Ray120c CCT/G-M control uses Amaran Desktop OpenAPI v2 at `ws://127.0.0.1:33782` with node ID `40165-560387`; use the repo wrapper `ray120c.py` instead of v1 WebSocket or direct BLE for extended CCT work.
- Camera control path: Canon EOS R6 Mark III over USB PTP using Homebrew `gphoto2 2.5.32` with `libgphoto2 2.5.34`; use `camera_gphoto2.py` for repeatable detection, ISO/aperture/shutter writes, RAW `.cr3` capture, and immediate download.
- Measurement basis: the selected camera under the documented capture policy
- Calibration scene: black card, white card, 18% gray card, 24-color chart, WLED output, and Ray120c output

## Default Workflow

1. Check `docs/README.md` before changing calibration or device-control behavior.
2. For WLED work, first verify API reachability at `http://wled-bedroom.local`.
3. For channel tests, use low initial values and test one physical channel at a time.
4. Record confirmed channel order and WLED payload behavior in `docs/README.md`.
5. For optimizer work, follow the documented camera policy: ISO 100, aperture two stops down from maximum, automatic shutter targeting white at about 80% full-well capacity, and fixed white balance/focus/image processing.
6. Calibrate per-channel WLED gamma before running white-mode or color-mode optimization.
7. For Canon R6 Mark III sessions, prefer `python3 camera_gphoto2.py capture ...`; the script runs `gphoto2 --auto-detect` because the USB port can change, avoids capture while shutter speed is `bulb`, and uses `--capture-image-and-download` for the initial RAW transfer path.
8. For RAW decoding, use `python3 camera_gphoto2.py decode ...` or capture with `--decode-linear`; it uses rawpy/LibRaw black/white level handling, demosaics to 16-bit linear camera RGB, and deliberately disables color matrix output, camera/auto white balance, auto-brightening, and gamma correction.
9. For exposure selection, use `python3 camera_gphoto2.py auto-expose ... --target-max 49152`; it keeps trial captures under the project `tmp/` directory, deletes trial RAW/decoded outputs, and only saves the final accepted capture and decoded outputs.

## Calibration Direction

- White mode should match Ray120c 100% brightness across `1800K` to `20000K` CCT and `-1.0` to `+1.0` G/M offset.
- Ray120c is only the 100% brightness calibration reference for the WLED strip.
- For Ray120c OpenAPI v2 `set_cct`, use raw `intensity` `0..1000` (`10` is `1%`) and raw `gm` `0..200` (`0` max magenta, `100` neutral, `200` max green). The v2 request requires a fresh AES-256-GCM token per request.
- For Ray120c HSL-like color work, use OpenAPI v2 `set_hsi` / `get_hsi`; the repo wrapper exposes both `set_hsi` and `set_hsl` aliases. Validated low-intensity cases read back exact `hue/sat/intensity` for hues `0`, `120`, `240`, `360`, plus `hue=30,sat=50`.
- For Ray120c RGB color work, use OpenAPI v2 `set_rgb` / `get_rgb`; validated low-intensity red/green/blue and mixed RGB values read back exact `r/g/b/intensity`. If `set_rgb` omits intensity, the current intensity is preserved.
- `get_node_config` reports `advanced_hsi_support=false`; `cct/gm` fields sent with `set_hsi` are ignored on this Ray120c, so use `set_cct` for CCT+G/M control.
- Direct BLE and Amaran Desktop v1 WebSocket paths are not the calibration control path: direct BLE did not reliably set extended CCT ranges, v1 `set_cct` with `gm` returned errors, and v1 `set_hsi` was not stable for CCT+G/M.
- All optimizer logic should operate on gamma-calibrated channel values, not raw PWM values.
- Stock WLED control for the five-channel RGBCCT bus is RGB + W + CCT. It does not directly expose separate JSON fields for arbitrary independent WW/CW values.
- With current CCT blend `cb=0`, map desired `[R,G,B,WW,CW]` to stock WLED as `W=WW+CW` and `CCT=round(255*CW/(WW+CW))` when `WW+CW <= 255`.
- Treat WLED command values as `0..255` unless a custom firmware/API path is introduced, even though ESP32 PWM output may use higher hardware PWM resolution internally.
- If replacing firmware, known viable directions are ESPHome via WLED Web OTA, Tasmota with independent PWM channels, or custom ESP-IDF LEDC firmware. Prefer custom ESP-IDF only after confirming serial/bootloader recovery.
- ESPHome can provide five independent LEDC outputs. For 12-bit PWM, use `19531 Hz` or lower rather than rounded `20000 Hz`; for optimizer control, prefer a user-defined native API action with `int`/`int32_t` variables in `0..4095`, then map each channel to output `set_level(value / 4095.0f)`. Do not use float16 for 12-bit channel commands.
- ESPHome configs are split into `firmware/esphome/wled-bedroom-rgbww-common.yaml`, `wled-bedroom-rgbww-no-webui.yaml`, and `wled-bedroom-rgbww-webui.yaml`; `wled-bedroom-rgbww.yaml` is a WebUI alias. Both variants use `esp32dev`, ESP-IDF, five LEDC outputs on `GPIO19/18/17/16/4`, `19531Hz`, API action `set_rgbww_12bit(...)`, and `all_off`. The No-WebUI variant has been verified on hardware for boot, Wi-Fi, Native API `6053`, and OTA `3232`; browser port `80` refusing is expected. The WebUI variant compiles and adds Basic Auth `web_server` on port `80`, but has not yet been flashed/verified on hardware. The ignored local `secrets.yaml` contains the `FireflyIoT` Wi-Fi credentials in plaintext.
- `main.py` exposes `light(cw, ww, r, g, b)`, mapping that call order to ESPHome `set_rgbww_12bit(red, green, blue, warm_white, cold_white)`. It converts arguments with `int()`, does not clamp values in Python, and calls `all_off` when all converted values are zero.
- Any first ESPHome firmware for this controller must include normal Wi-Fi credentials, `wifi.ap` with a strong fallback password, and `captive_portal:`; ESPHome fallback AP is opt-in and only exists if compiled into the YAML.
- Color mode should use model-first matching in camera-observed color space plus a sparse LUT/residual correction for Ray120c HSL/RGB command behavior.
- Do not treat Ray120c RGB mode as simple additive RGB unless measurements prove that assumption.

## Safety Notes

- Do not run broad high-output sweeps without an explicit bounded range.
- Confirm channel order, brightness scaling, and thermal behavior before increasing PWM levels.
- Before custom firmware flashing, confirm whether reset can enter ESP32 ROM bootloader via `GPIO0` and whether UART0 `TX/RX` pads are accessible; WLED web OTA and restart/reset buttons alone are not enough for serial recovery unless bootloader entry and UART are accessible.
- Preserve local user work and avoid committing unless the user explicitly asks.
