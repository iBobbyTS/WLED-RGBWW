# WLED RGBWW Optimizer

## Project Purpose

This project will optimize a PWM RGBWW WLED strip against a camera-observed reference light.

Stable project facts:

- WLED host: `wled-bedroom.local`
- Controller: GLEDOPTO `GL-C-211WL` ESP32 WLED PWM controller running WLED firmware
- Fixture: five-channel PWM RGBWW strip
- Phase 1 target: prove independent adjustment for all five physical channels
- Phase 2 target: use a camera as the measurement device and match the strip to an Aputure Amaran Ray120c reference light at 100% brightness
- Calibration scene: black card, white card, 18% gray card, 24-color chart, WLED strip output, and Ray120c reference output

Read-only device facts observed from WLED:

- Firmware: WLED `0.15.1`
- Reported name: `WLED-Gledopto`
- Light capability: `info.leds.lc = 7`, meaning RGB + white channel + CCT
- Hardware bus: WLED analog type `45`, `TYPE_ANALOG_5CH` / RGB + warm white + cold white
- GPIO order reported by config: `[19, 18, 17, 16, 4]`
- PWM frequency reported by config: `19531 Hz`
- Physical recovery controls observed by the user: restart button and reset button
- Web OTA page verified: `http://wled-bedroom.local/update` serves WLED Software Update for installed version `0.15.1`
- WLED config reports button 0 as type `2` on `GPIO0` with pull-up enabled; this needs physical verification against the reset/restart buttons and the documented `IO33` DIY interface.
- WLED config reports IR on `GPIO13`, type `1`, selected.
- Current WLED gamma config reports brightness gamma enabled and color/value gamma `2.8`; the optimizer should either neutralize WLED built-in gamma or treat it as part of the measured control chain.

## Phase 1: Five-Channel WLED Control

The first implementation milestone is to verify a safe, repeatable path for controlling the RGBWW channels separately.

Expected workflow:

1. Confirm WLED API reachability at `http://wled-bedroom.local`.
2. Identify the exact WLED JSON/API payload that maps to the physical RGBWW channels.
3. Start with low PWM values and test one physical channel at a time.
4. Record channel order, minimum visible level, saturation/thermal concerns, and any WLED-specific behavior such as global brightness scaling.
5. Keep the control path deterministic enough that later calibration code can request raw channel vectors.

Do not assume WLED UI labels or JSON field order match the physical strip order until the mapping has been measured.

### WLED Manual Channel Control Notes

Prefer WLED's JSON API for calibration tooling. The HTTP request API can set RGBW values, but WLED itself recommends JSON for new integrations.

For this five-channel analog RGBCCT setup, stock WLED control is exposed as:

- RGB values: `seg[0].col[0][0..2]` or object keys `r`, `g`, `b`
- White intensity: `seg[0].col[0][3]` or object key `w`
- White split: `seg[0].cct`

This means WLED exposes RGB + W + CCT, not direct independent `WW` and `CW` JSON fields. With the current non-IC CCT setup, WLED computes warm-white and cold-white PWM from the requested `w` value plus `cct`.

Current config has CCT blend `cb=0`, so WLED's stock mapping is approximately:

- `WW = W * (255 - CCT) / 255`
- `CW = W * CCT / 255`

For desired independent white values where `WW + CW <= 255`, invert that mapping as:

- `W = WW + CW`
- `CCT = round(255 * CW / (WW + CW))`

This is sufficient for many five-channel mix commands, but it is still routed through WLED's `W + CCT` representation and quantized to 8-bit command values. It is not full arbitrary direct 5-channel PWM control, especially when the desired `WW + CW` exceeds `255` or when exact integer output matters.

Example desired vector `[R, G, B, WW, CW] = [12, 17, 6, 200, 40]`:

- `W = 200 + 40 = 240`
- `CCT = round(255 * 40 / 240) = 43`
- JSON color command: `col = [[12,17,6,240]]`, `cct = 43`

Safe low-value manual test examples, not to be run without confirming the strip is ready:

```bash
# Red only at low value
curl -X POST "http://wled-bedroom.local/json/state" \
  -H "Content-Type: application/json" \
  -d '{"on":true,"bri":255,"transition":0,"seg":[{"id":0,"fx":0,"bri":255,"cct":0,"col":[[20,0,0,0]]}]}'

# Warm white only at low value
curl -X POST "http://wled-bedroom.local/json/state" \
  -H "Content-Type: application/json" \
  -d '{"on":true,"bri":255,"transition":0,"seg":[{"id":0,"fx":0,"bri":255,"cct":0,"col":[[0,0,0,20]]}]}'

# Cold white only at low value
curl -X POST "http://wled-bedroom.local/json/state" \
  -H "Content-Type: application/json" \
  -d '{"on":true,"bri":255,"transition":0,"seg":[{"id":0,"fx":0,"bri":255,"cct":255,"col":[[0,0,0,20]]}]}'
```

For the optimizer, first determine whether the WLED RGB + W + CCT parameterization is sufficient to represent desired warm-white/cold-white pairs under the current CCT blend setting. If arbitrary independent WW/CW vectors are required beyond that mapping, plan for a custom WLED usermod/firmware endpoint or another controller stack.

WLED command values for brightness, RGB, W, and relative CCT are 8-bit style `0..255` values. On ESP32 PWM output, WLED 0.15.x can use higher hardware PWM resolution at the configured PWM frequency, but stock per-channel color data is still stored and commanded as 8-bit values. Treat `0..255` as the optimizer's command resolution unless a custom firmware/API path is introduced.

## Firmware Options And DIY Findings

Known options:

- WLED stock firmware: already installed, supports web OTA at `/update`, and is safest while staying within stock WLED behavior. It exposes RGB + W + CCT rather than direct arbitrary five-channel commands.
- ESPHome: a community report for the same `GL-C-211WL` says ESPHome was compiled from the command line and uploaded through the default WLED web interface, then updated OTA afterward. The shared example used `esp32dev`, ESP-IDF framework, and LEDC outputs on the documented PWM pins. ESPHome `rgbww` can expose five float outputs, including independent warm-white and cold-white channels. For calibration control, prefer five raw `ledc` outputs plus a user-defined native API action that accepts integer channel values and calls `set_level(value / 4095.0)`, rather than relying only on Home Assistant light UI semantics.
- Tasmota: supports RGBCCT lights and independent PWM channels via `SetOption68 1`; ESP32 Tasmota uses LEDC and can expose more PWM channels. No same-device GL-C-211WL Tasmota success case has been confirmed yet, and the command/UI model is less suitable for high-precision calibration than a purpose-built API.
- Custom ESP-IDF firmware: best fit for the optimizer if true `[R,G,B,WW,CW]` direct control and maximum usable PWM bit depth are required. Use LEDC directly, choose frequency/resolution explicitly, expose a small HTTP/WebSocket API, and keep all gamma/color logic in Python.

PWM resolution guidance for custom ESP-IDF/ESPHome LEDC:

- `19531 Hz`: about `12-bit`
- `9765 Hz`: about `13-bit`
- `4882 Hz`: about `14-bit`
- `2441 Hz`: about `15-bit`
- `1220 Hz`: about `16-bit` on original ESP32-class LEDC, but this may increase camera banding risk

For an exact 12-bit target on original ESP32 LEDC, use `19531 Hz` or lower. A rounded `20000 Hz` setting may drop below 12-bit because the LEDC frequency/resolution pair must fit the hardware clock divider.

Precision path for ESPHome calibration control:

- Treat `12-bit` as the PWM duty resolution at the LEDC output, not as the native API payload type.
- Python optimizer should send channel values as integers in `0..4095`.
- ESPHome user-defined native API actions support `int` variables as C++ `int` / `int32_t`; use those for channel commands.
- In ESPHome lambda code, clamp each integer and call `set_level(value / 4095.0f)`.
- ESPHome `float` variables and light command fields are protobuf `float` / C++ `float` values, i.e. 32-bit float on ESP32, not float16.
- Do not use float16 in the optimizer path. IEEE binary16 cannot exactly represent all 12-bit integer codes; it exactly represents every integer only up to `2048`, then skips odd integers in the `2048..4096` range.

Recommended experimental path:

1. Keep stock WLED until a physical serial/bootloader recovery path is confirmed.
2. If replacing firmware via WLED Web OTA, first use ESPHome rather than fully custom ESP-IDF because a community same-model success path exists.
3. For calibration-grade control, build toward a custom ESP-IDF firmware after confirming recovery access and PWM/camera banding constraints.

ESPHome Wi-Fi safety requirement:

- ESPHome does not provide a WLED-style factory AP unless the compiled YAML includes it.
- Include `wifi.ap` with a strong password and `captive_portal:` in any first ESPHome firmware for this controller.
- When normal Wi-Fi fails, ESPHome's captive portal fallback starts an AP after the configured timeout, commonly about one minute.
- The captive portal can accept new Wi-Fi credentials and can upload firmware, while the ESPHome firmware still boots.
- Do not rely on WLED's saved Wi-Fi settings carrying over to ESPHome; compile the intended SSID/password and fallback AP into the first ESPHome image.

### ESPHome Compile Candidate

Prepared config:

- Shared config: `firmware/esphome/wled-bedroom-rgbww-common.yaml`
- No-WebUI entry: `firmware/esphome/wled-bedroom-rgbww-no-webui.yaml`
- WebUI entry: `firmware/esphome/wled-bedroom-rgbww-webui.yaml`
- Default alias: `firmware/esphome/wled-bedroom-rgbww.yaml` points to the WebUI variant
- ESPHome package pin: `firmware/esphome/requirements.txt`, currently `esphome==2026.5.2`
- Board/framework: `esp32dev` with ESP-IDF
- Flash size: `4MB`, matching observed WLED info
- Five LEDC outputs: `R=GPIO19`, `G=GPIO18`, `B=GPIO17`, `WW=GPIO16`, `CW=GPIO4`
- PWM frequency: `19531Hz`
- API action: `set_rgbww_12bit(red, green, blue, warm_white, cold_white)`, each argument integer `0..4095`
- Safety action: `all_off`
- Boot behavior: explicitly turns all five outputs off
- Wi-Fi safety: normal Wi-Fi from `secrets.yaml`, fallback AP, `captive_portal:`, OTA password, and API encryption key
- No-WebUI variant: no browser UI, Native API + OTA only
- WebUI variant: `web_server` on port `80` with Basic Auth credentials from ignored `secrets.yaml`

Local preparation commands:

```bash
uv venv .venv
uv pip install -r firmware/esphome/requirements.txt
.venv/bin/esphome config firmware/esphome/wled-bedroom-rgbww-no-webui.yaml
.venv/bin/esphome compile firmware/esphome/wled-bedroom-rgbww-no-webui.yaml
.venv/bin/esphome config firmware/esphome/wled-bedroom-rgbww-webui.yaml
.venv/bin/esphome compile firmware/esphome/wled-bedroom-rgbww-webui.yaml
```

Current status:

- No-WebUI variant is verified effective on hardware: it boots, joins Wi-Fi as `wled-bedroom-rgbww.local`, responds to ping, exposes Native API on port `6053`, and exposes ESPHome OTA on port `3232`. Port `80` refusing connections is expected because this variant has no browser UI.
- No-WebUI OTA binary: `firmware/esphome/.esphome/build/wled-bedroom-rgbww-no-webui/.pioenvs/wled-bedroom-rgbww/firmware.ota.bin`, `SHA256 f599a5544fdb0e76dbf121ac8bcc8978dc66e5c35dbc66f4bd48a3ff3b304da1`
- No-WebUI factory binary: `firmware/esphome/.esphome/build/wled-bedroom-rgbww-no-webui/.pioenvs/wled-bedroom-rgbww/firmware.factory.bin`, `SHA256 bb3bee426e58edc57fcc6173fcda99cae8a13532dd45ba4d26ad7aa971549d26`
- WebUI variant compiles successfully but has not yet been flashed or verified on hardware.
- WebUI OTA binary: `firmware/esphome/.esphome/build/wled-bedroom-rgbww-webui/.pioenvs/wled-bedroom-rgbww/firmware.ota.bin`, `SHA256 2fc0b9690e7c646e8ff4d5d9e51898b9351a4b044ebb9f7ee8831a1648cf6813`
- WebUI factory binary: `firmware/esphome/.esphome/build/wled-bedroom-rgbww-webui/.pioenvs/wled-bedroom-rgbww/firmware.factory.bin`, `SHA256 526161ce202aba13163011030509ebecaed33ed79ff448c9947956d8ded23f75`
- The local ignored `firmware/esphome/secrets.yaml` contains the `FireflyIoT` Wi-Fi credentials in plaintext for this build. Do not copy the password into tracked docs.
- If `wled-bedroom-rgbww.local` is pingable but the browser reports `ERR_CONNECTION_REFUSED`, the running firmware likely uses the verified No-WebUI variant. Native API on port `6053` and ESPHome OTA on port `3232` should still work.

Before any flash attempt, confirm the fallback AP password, API encryption key,
and OTA password in ignored `firmware/esphome/secrets.yaml` are the intended
final values.

### Native API Python Wrapper

`esphome.py` exposes a convenience function:

```python
from esphome import light

light(cw, ww, r, g, b)
```

The function maps call order `cw, ww, r, g, b` to ESPHome service arguments
`cold_white, warm_white, red, green, blue`. Each value is converted with
`int()` and is not range-clamped by the script. If all converted values are
zero, it calls the ESPHome `all_off` action instead of
`set_rgbww_12bit`.

Command-line use:

```bash
.venv/bin/python esphome.py 0 0 2048 0 0
.venv/bin/python esphome.py 0 0 0 0 0
```

Local validation:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile esphome.py tests/test_main.py
```

## Phase 2: Camera-Based Matching

The camera workflow should treat the camera as the observer that defines "matching" for this project. The goal is not abstract spectral accuracy; the goal is repeatable visual/colorimetric agreement as seen by the selected camera under controlled capture settings.

Camera capture policy:

- ISO: `100`
- Aperture: two stops down from maximum aperture
- Shutter speed: automatic, targeting white at about `80%` full-well capacity
- Record shutter metadata for each capture so brightness comparisons can be normalized when needed.
- Keep white balance, focus, and image processing settings fixed.
- Prefer RAW or another linearizable capture format. If only processed frames are available, document the camera pipeline and keep it fixed.
- Use the black/white/18% gray cards for exposure and neutrality checks.
- Use the 24-color chart to estimate camera-to-working-space correction and to detect unstable lighting or nonlinear camera behavior.
- Keep ambient light controlled and repeatable.

### Canon EOS R6 Mark III gphoto2 Probe

Use `gphoto2` as the first camera-control path for the Canon EOS R6 Mark III calibration workflow.

Validated local setup on 2026-06-05:

- Host tool: Homebrew `gphoto2 2.5.32`
- Camera library: `libgphoto2 2.5.34`
- Detected camera: `Canon EOS R6 Mark III`
- Observed USB port during the probe: `usb:001,010`
- Camera firmware/device version reported by gphoto2: `3-1.0.2`
- Lens reported during the probe: `RF35mm F1.8 MACRO IS STM`
- Storage reported during the probe: CFexpress and SD cards both visible

The project wrapper is `camera_gphoto2.py`. It auto-detects the camera port, sets bounded exposure parameters, verifies the shutter is not `bulb`, triggers capture, and reports the downloaded RAW path as JSON:

```bash
python3 camera_gphoto2.py detect
python3 camera_gphoto2.py capture \
  --output-dir captures/camera \
  --iso 100 \
  --aperture 4 \
  --shutter-speed 1/30 \
  --image-format RAW
```

The same wrapper can decode a captured CR3 into a linear 16-bit camera-RGB image for calibration:

```bash
python3 camera_gphoto2.py decode captures/camera/example.cr3 \
  --output-dir captures/decoded \
  --format npy \
  --format tiff
```

The decode path uses `rawpy`/LibRaw with:

- `output_color=rawpy.ColorSpace.raw`
- `use_camera_wb=False`
- `use_auto_wb=False`
- `user_wb=[1, 1, 1, 1]`
- `no_auto_bright=True`
- `gamma=(1, 1)`
- `output_bps=16`

This intentionally keeps the result in linear camera RAW RGB space. LibRaw handles the RAW black/white level correction before demosaicing; the project should not apply a second manual black-level subtraction to this decoded output. The command writes `.npy`, optional 16-bit `.tiff`, and a `.json` sidecar containing LibRaw sizes, black/white level metadata, and output statistics.

To capture and decode in one call:

```bash
python3 camera_gphoto2.py capture \
  --output-dir captures/camera \
  --iso 100 \
  --aperture 4 \
  --shutter-speed 1/30 \
  --image-format RAW \
  --decode-linear \
  --decode-output-dir captures/decoded
```

To automatically find an exposure whose decoded linear image max stays at or below `49152`, use:

```bash
python3 camera_gphoto2.py auto-expose \
  --output-dir captures/camera \
  --decode-output-dir captures/decoded \
  --target-max 49152 \
  --iso 100 \
  --aperture 4 \
  --min-shutter-speed 1/8000 \
  --max-shutter-speed 30 \
  --max-trials 5 \
  --max-captures 10
```

The auto-exposure routine keeps ISO and aperture fixed, starts from the current bounded shutter speed, and decodes each trial with the same linear camera-RGB path. It uses decoded metering max as the hard safety limit, but uses the strongest per-channel high-percentile contrast (`p99.9 - p10`) as the exposure feedback so saturated HSI/RGB hues are not misclassified as dark just because the green channel is weak. Very dark measurements jump by a bounded EV step, saturated measurements retreat by a bounded EV step, and mixed safe/overexposed brackets are refined with a geometric midpoint. The final saved image must have the selected metering pixels at or below `target_max`; rejected final captures are deleted and retried with a safer shutter. Temporary trial captures are capped by `--max-trials` (`5` by default), and total shutter releases including rejected finals and the saved final are capped by `--max-captures` (`10` by default). Trial RAW and decoded files are written under the project `tmp/` tree and deleted before the command returns. The final accepted RAW plus final decoded `.npy`/`.tiff`/`.json` sidecar are saved to the requested output directories. Final filenames are automatically numbered on retries so gphoto2 never prompts to overwrite an existing file. Auto exposure supports two metering modes without changing the exposure step algorithm: `--metering-mode full` keeps the original full-image max/contrast behavior, while `--metering-mode location --metering-location-config <json>` loads a saved location-picker JSON and computes max/contrast only from the 24 color-chart quadrilaterals. The full decoded image is still saved; the location mode only changes the statistics used to choose shutter speed. Location-metering decode sidecars include an additional `metering` section with the selected region count, pixel count, max values, and exposure contrast.

Example location-metered auto exposure:

```bash
.venv/bin/python camera_gphoto2.py auto-expose \
  --output-dir captures/camera \
  --decode-output-dir captures/decoded \
  --metering-mode location \
  --metering-location-config /Users/ibobby/Projects/WLED-RGBWW/config/location/locations-20260605-225800.json
```

Validation on 2026-06-05 used Ray120c CCT states `(0%, 0.1%, 1%, 10%, 50%, 100%) x (1800K, 5600K, 20000K)` plus HSI `100%` saturation at hues `0, 60, 120, 180, 240, 300`, from both underexposed (`1/8000`) and overexposed (`1s`) starts. The final report at `tmp/ae-ray120c-probe/20260605-184035/report.json` covered `48` auto-exposure runs with no errors, no final image/channel max above `49152`, maximum final channel value `48614`, and no run exceeding `10` shutter releases.

### WLED Channel Code Response Measurement

Use `measure_channel_response.py` to measure the WLED strip's per-channel code/duty to camera-linear response before Ray120c matching. Ray120c is not part of this measurement; the goal is to characterize the five WLED channels with the fixed Canon RAW linear pipeline.

The script turns on one channel at a time, runs the existing bounded auto-exposure capture for each code, decodes the RAW image to `.npy`, computes region statistics, normalizes signal by shutter seconds, and writes an incremental JSON report under `tmp/channel-response/`. It starts and ends with `all_off`, and can optionally capture an all-off ambient frame and subtract its normalized signal from each measurement.

First inspect the planned sweep without touching hardware:

```bash
.venv/bin/python measure_channel_response.py \
  --channels cw,ww,r,g,b \
  --codes "1,2,3,4,6,8,12,16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4095" \
  --dry-run
```

For a real high-output sweep, pass `--allow-high-output` only after confirming the strip and camera setup are safe:

```bash
.venv/bin/python measure_channel_response.py \
  --channels cw,ww,r,g,b \
  --codes "1,2,3,4,6,8,12,16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4095" \
  --location-config config/location/locations-YYYYMMDD-HHMMSS.json \
  --block-indices all \
  --allow-high-output
```

If no `--roi` or `--location-config` is provided, the script measures the full decoded image. For patch-based measurements, prefer a saved location-picker config. Use `--roi x,y,width,height` for simple rectangular regions.

The measurement regions above control the final response statistics. Separately, the auto-exposure step can be told to meter only the saved 24 chart patches by adding:

```bash
--auto-exposure-metering-mode location \
--auto-exposure-metering-location-config config/location/locations-YYYYMMDD-HHMMSS.json
```

Each JSON measurement records:

- channel, code, duty, and the exact ESPHome command vector
- final auto-exposure result and shutter speed
- `shutter_seconds`
- per-region raw stats from decoded linear camera RGB
- per-region `channel_mean_per_second` and `channel_median_per_second`
- optional ambient-subtracted normalized channel means

For interactive color-block location picking, use:

```bash
python3 location_picker_ui.py --blocks 24 --rows 4 --cols 6
```

On startup the script serves a local Web UI on `127.0.0.1:8765` and opens it in the default browser. Use `--ui-port <port>` if that port is occupied, and use `--no-browser` if you want to open the URL manually. The `--port` and `--camera-port` options are aliases for the gphoto2 camera port. The Web UI runs the same bounded auto-exposure path, saves the final capture and decoded `.npy` under `tmp/location-ui/<timestamp>/camera` and `tmp/location-ui/<timestamp>/decoded`, then displays a PNG preview in a browser canvas. Trial captures are deleted by the auto-exposure function before the UI loads. After the final preview is decoded, the backend automatically runs the current row/column color-block detector and sends those quadrilaterals with the first loaded page state, so the browser fills the blocks immediately and the user normally only needs to inspect and fine-tune them. If the camera is offline or auto-exposure fails before an image loads, the toolbar `重试相机` button reruns the same capture flow after the camera is reconnected. The UI defaults to `--max-exposure-trials 3` because the location picker only needs a usable preview; raise it if a tighter exposure is needed. The `自动识别` button reruns detection with the entered row and column count when the user wants to replace the current quadrilaterals. For 4x6 charts, `opencv-contrib-python-headless` enables OpenCV's dedicated `mcc` Macbeth 24 ColorChecker detector and returns the patch quadrilaterals directly. If `mcc` is unavailable or fails, the UI falls back to OpenCV Canny/Hough grid-angle detection, then to the conservative projection detector. Fallback-detected blocks are inset by default so they stay inside color patches and do not include the black frame or grid lines. When `rows=4`, `cols=6`, and 24 quadrilaterals are present, the UI overlays standard ColorChecker Classic 24 labels and saved configs include `chart` metadata plus per-block `patch` metadata. Use the `色卡 0°` / `色卡 180°` toolbar button when the physical chart is rotated 180 degrees; manual patch-label editing is intentionally not supported yet. Use the mouse wheel to zoom, right-button or middle-button drag to pan, and left-button drag to create a rectangle. Existing quadrilaterals can be edited by dragging inside the shape to move it, dragging an edge to move that side, or dragging a corner handle to make an irregular quadrilateral. The confirm button is enabled only when the entered block count equals the number of quadrilaterals. Confirming writes a JSON configuration to `config/location/`.

Optional Python dependencies for decoding and automatic location detection:

```bash
python3 -m pip install rawpy numpy tifffile opencv-contrib-python-headless
```

For the repository-local `.venv`, bootstrap pip first if needed, then install the same dependencies:

```bash
.venv/bin/python3 -m ensurepip --upgrade
.venv/bin/python3 -m pip install --upgrade pip rawpy numpy tifffile opencv-contrib-python-headless
```

Detection and inspection commands:

```bash
gphoto2 --version
gphoto2 --auto-detect
gphoto2 --port usb:001,010 --summary
gphoto2 --port usb:001,010 --list-config
gphoto2 --port usb:001,010 \
  --get-config /main/imgsettings/iso \
  --get-config /main/capturesettings/aperture \
  --get-config /main/capturesettings/shutterspeed \
  --get-config /main/imgsettings/imageformat
```

Confirmed writable controls:

- ISO: `/main/imgsettings/iso`; validated at `100`
- Aperture: `/main/capturesettings/aperture`; validated at `4`
- Shutter speed: `/main/capturesettings/shutterspeed`; validated by changing from `bulb` to `1/30`
- Image format: `/main/imgsettings/imageformat`; current validated setting was `RAW`

Safe bounded capture smoke test:

```bash
mkdir -p /tmp/wled-rgbww-gphoto2-probe
gphoto2 --port usb:001,010 \
  --set-config /main/imgsettings/iso=100 \
  --set-config /main/capturesettings/aperture=4 \
  --set-config /main/capturesettings/shutterspeed=1/30 \
  --capture-image-and-download \
  --filename /tmp/wled-rgbww-gphoto2-probe/%Y%m%d-%H%M%S.%C
```

The smoke test downloaded `/tmp/wled-rgbww-gphoto2-probe/20260605-134136.cr3` and removed the temporary `/capt0001.cr3` from the camera. `exiftool` confirmed:

- Model: `Canon EOS R6 Mark III`
- File type: `CR3`
- Image size: `6960 x 4640`
- ISO: `100`
- Shutter speed: `1/30`
- Aperture/F-number: `4.0`

Silent/electronic shutter follow-up:

- After enabling silent shutter on the camera, `gphoto2 --capture-image-and-download` still succeeded.
- Downloaded file: `/tmp/wled-rgbww-gphoto2-probe/20260605-134503-silent.cr3`
- File size: about `19 MB`
- `exiftool` confirmed `ShutterMode: Electronic`, `DriveMode: Single-frame Shooting`, `ISO 100`, `1/30`, and `f/4.0`.

Operational notes:

- The port identifier can change after reconnecting; run `gphoto2 --auto-detect` before scripted sessions.
- Avoid triggering capture while shutter speed reads `bulb`; first set a bounded shutter speed.
- The command above uses gphoto2's capture-and-download flow and deletes the temporary camera-side file after transfer. If card retention is needed, add `--keep` deliberately.
- For optimizer automation, use `camera_gphoto2.py` first. It wraps gphoto2 via subprocess for reproducibility; consider Python libgphoto bindings only if subprocess overhead becomes a measured problem.

## Calibration Baselines

- The Ray120c is only the 100% brightness reference for calibrating the WLED strip.
- Calibrate gamma separately for each physical WLED channel before white-mode or color-mode optimization.
- All later optimization must operate on gamma-calibrated channel values, not raw PWM values.

## Ray120c Control Path

Use Amaran Desktop's local OpenAPI v2 WebSocket path for Ray120c CCT/G-M reference control:

- WebSocket URL: `ws://127.0.0.1:33782`
- Node ID: `40165-560387` (`amaran Ray 120c #1`)
- Action: `set_cct`
- OpenAPI version: `2`
- Token: per-request AES-256-GCM token generated from the OpenAPI secret, using `base64(iv + tag + ciphertext(timestamp_seconds))`

The confirmed Ray120c ranges from `get_node_config` are:

- CCT extension enabled: `1800K` to `20000K`
- Intensity: raw `0..1000`, where `10` is `1%` and `1000` is `100%`
- G/M: raw `0..200`, where `0` is maximum magenta, `100` is neutral, and `200` is maximum green

The project wrapper is `ray120c.py`. It is importable from Python and also exposes a CLI:

```bash
python3 ray120c.py set-cct 1800 --intensity-percent 1 --gm 200
python3 ray120c.py set-cct 1800 --intensity-percent 1 --gm 0
python3 ray120c.py get-cct
python3 ray120c.py set-hsl 60 50 --intensity-percent 1
python3 ray120c.py get-hsl
python3 ray120c.py set-rgb 12 34 56 --intensity-percent 1
python3 ray120c.py get-rgb
python3 ray120c.py node-config
```

Environment overrides:

- `AMARAN_WS_URL`
- `AMARAN_NODE_ID`
- `AMARAN_CLIENT_ID`
- `AMARAN_OPENAPI_SECRET`
- `AMARAN_TIMEOUT`

The local wrapper defaults to the public OpenAPI demo secret documented by Sidus/Amaran because that key worked against the local Amaran Desktop v2 server during validation. Override it with `AMARAN_OPENAPI_SECRET` if Amaran Desktop starts requiring a per-user key.

Rejected or limited paths:

- Direct BLE via `wesbos/amaran-BLE-control` can adjust standard CCT/G-M values, but it did not reliably set the extended `1800..2299K` and `10001..20000K` ranges.
- Amaran Desktop v1 WebSocket `set_cct` can set extended CCT without G/M, but `set_cct` with `gm` returned errors for tested G/M values.
- v1 `set_hsi` with CCT/G-M-like fields can briefly affect output, but it returns to an HSI/white path and is not a stable CCT+G/M control method.

### HSI/HSL and RGB Validation

Amaran Desktop's OpenAPI names the HSL-like mode `HSI`. The wrapper exposes both `set_hsi`/`get_hsi` and `set_hsl`/`get_hsl` aliases. Validated ranges and scales:

- HSI/HSL: `hue` `0..360`, `sat` `0..100`, raw `intensity` `0..1000`
- RGB: `r/g/b` `0..255`, optional raw `intensity` `0..1000`
- CLI `--intensity-percent 1` maps to raw `intensity=10`

Validated acceptance evidence through OpenAPI v2 on the Ray120c:

- `set_hsi(0, 100, intensity_percent=1)`, `set_hsi(120, 100, intensity_percent=1)`, and `set_hsi(240, 100, intensity_percent=1)` each returned `code=0`, then `get_hsi` read back the exact `hue/sat/intensity`.
- `set_hsi(360, 100, intensity_percent=1)` and `set_hsi(30, 50, intensity_percent=1)` also returned `code=0`, then `get_hsi` read back the exact values.
- `set_rgb(255, 0, 0, intensity_percent=1)`, `set_rgb(0, 255, 0, intensity_percent=1)`, `set_rgb(0, 0, 255, intensity_percent=1)`, and `set_rgb(128, 64, 32, intensity_percent=1)` each returned `code=0`, then `get_rgb` read back the exact `r/g/b/intensity`.
- `set_rgb(12, 34, 56)` without an intensity field preserved the current raw `intensity=10` and `get_rgb` read back `r=12,g=34,b=56,intensity=10`.
- Sequential CLI validation passed for `set-hsl 60 50 --intensity-percent 1` followed by `get-hsl`, and `set-rgb 12 34 56 --intensity-percent 1` followed by `get-rgb`.

Boundary note: `get_node_config` reports `advanced_hsi_support=false`. A `set_hsi` request including optional `cct=1800` and `gm=200` returned `code=0`, but the response and `get_hsi` only contained `hue/sat/intensity`; `get_cct` stayed at the prior CCT/G-M state. Treat HSI/HSL `cct/gm` fields as ignored on this Ray120c and use `set_cct` for CCT+G/M reference control.

## White Mode Target

The white-mode target is to match the Ray120c at 100% brightness across:

- CCT: `1800K` to `20000K`
- G/M offset: `-1.0` to `+1.0`

Recommended initial CCT/G-M LUT grid:

- CCT points: `1800, 2000, 2300, 2700, 3200, 3800, 4500, 5000, 5600, 6500, 8000, 10000, 14000, 20000`
- G/M raw points: `0, 70, 100, 130, 200`
- Total initial grid: `14 x 5 = 70` Ray120c target points

Use this as the first calibration grid, then add points only where interpolation or WLED matching error is high. CCT spacing should roughly follow mired spacing (`1e6 / K`) rather than equal Kelvin spacing, while preserving common photography points such as `5000K`, `5600K`, and `6500K`. Do not start with the full `19 x 7` grid unless the first pass shows that the extra G/M points (`40`, `160`) or dense high-CCT points materially reduce error.

Recommended direction:

- Measure Ray120c target outputs on a CCT/G-M grid.
- Measure WLED RGBWW channel response in the same camera-observed space.
- Solve constrained RGBWW channel vectors that minimize perceptual/colorimetric error while respecting brightness, clipping, and channel limits.

## Color Mode Strategy Decision

Use a model-first approach as the main path, then add a sparse LUT/residual correction layer for Ray120c HSL/RGB modes where the reference light's internal algorithm behaves nonlinearly or discontinuously. Treat Ray120c RGB as a black-box command mode, not as simple three-channel additive RGB.

The initial color-mode direction is provisional. If the CCT/G-M calibration work shows a better camera-observed model, channel basis model, solver structure, or residual-correction method, replace this color-mode plan with the method supported by those measurements.

Recommended structure:

1. Build a Ray120c target model: map Ray120c commands to measured 24-patch camera-RGB responses.
2. Build a WLED inverse model: map WLED RGBWW channel codes to predicted 24-patch camera-RGB responses, using the measured channel response curves and channel basis responses.
3. Solve WLED RGBWW channel vectors that best match each Ray120c target while respecting channel limits, clipping, brightness, smoothness, and optional white-channel/RGB-channel usage penalties.
4. Add a sparse residual LUT only after real WLED captures show systematic model error in specific RGB or HSI regions.

For Ray120c RGB mode, treat `(r, g, b, intensity)` as a black-box 3D/4D target command space. A first target grid should include the gray axis, RGB/CMY edges, and a sparse RGB cube such as `0, 64, 128, 192, 255` per channel at fixed intensity. Start with `100%` intensity; add intensity levels such as `10%, 25%, 50%, 100%` only after the RGB color model is stable.

For Ray120c HSI/HSL mode, model the command space with circular hue features such as `sin(hue)` and `cos(hue)`, plus saturation and intensity. A first target grid should use hue every `30 degrees`, saturation `0, 10, 25, 50, 75, 100`, and fixed `100%` intensity. Because `sat=0` should ideally ignore hue, measure a few hue values at `sat=0` first; if Ray120c output is stable there, store one neutral sample rather than duplicating all hues. Add local hue or saturation points only where measured residuals justify them.

The WLED side should not start as a direct Ray command to WLED code LUT. The dimensionality is too high and interpolation near gamut boundaries will be fragile. Prefer a shared inverse solver for CCT/G-M, RGB, and HSI targets, then use residual LUTs as localized corrections rather than the primary representation.

Reasoning:

- Pure LUT matching is straightforward and black-box friendly, but it needs many measurements, interpolates poorly near gamut boundaries, and is harder to adapt when brightness, exposure, or device behavior changes.
- Pure xy-only modeling is too weak because it can ignore luminance, camera nonlinearity, and metameric differences.
- A camera-observed color model gives a continuous target space for optimization, while a residual LUT can absorb Ray120c-specific behavior in HSL/RGB command space.

## Open Decisions

- Python environment and package manager: not selected yet.
- Test runner and validation commands: not selected yet.
- Ray120c control method: selected for CCT/G-M reference work via `ray120c.py` using Amaran Desktop OpenAPI v2 on `ws://127.0.0.1:33782`.
- Color-mode measurement grid: not selected yet.
- Firmware replacement safety: WLED web OTA is available while WLED can boot, but it is not a brick-recovery path. Confirm whether the physical reset button, WLED-configured `GPIO0` button, and documented `IO33` DIY interface are separate or connected; also confirm whether UART0 `TX/RX` test pads are accessible before flashing custom firmware.
- Maintainability audit reminder cadence: not configured.
