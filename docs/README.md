# Camera-Based RGBWW Optimizer

## Purpose

This project calibrates and optimizes a five-channel RGBWW light source using a
camera as the measurement device, an Aputure Amaran Ray120c as the high-color
reference, and a 24-patch color chart for camera-observed matching.

Core goals:

- Measure per-channel RGBWW brightness response in linear camera RAW space.
- Build code-duty correction curves for linear brightness control.
- Capture and annotate color-chart locations for repeatable patch statistics.
- Match white mode against Ray120c CCT/G-M references.
- Build model-first RGB/HSI matching workflows with sparse residual correction
  only where measurements justify it.

## Python Layout

Implementation lives in the installable package:

```text
src/camera_based_rgbww_optimizer/
  control/        # Canon/gphoto2, Ray120c, ESPHome RGBWW strip control
  optimization/   # Measurement and calibration workflows
  utils/          # Shared geometry/statistics and post-processing tools
  interaction/    # Local Web UI tools
  paths.py        # Project-root and tmp-path helpers
```

Install the project in editable mode before running package modules:

```bash
.venv/bin/python -m pip install -e .
```

This installs the package's Python runtime dependencies, including ESPHome
Native API control, Ray120c token generation, RAW decoding, NumPy processing,
TIFF export, and OpenCV color-chart detection. The `gphoto2` command-line tool
is a system dependency and must still be installed separately.

All commands below use `python -m ...`; root-level legacy script entry points are
not part of the current project interface.

## Device Facts

- Camera: Canon EOS R6 Mark III over USB PTP with `gphoto2`.
- Reference light: Aputure Amaran Ray120c controlled through Amaran Desktop
  OpenAPI v2 at `ws://127.0.0.1:33782`.
- RGBWW fixture: ESPHome Native API action `set_rgbww_12bit` with channel values
  `0..4095`.
- Default ESPHome device host: `bedroom-rgbww-strip.local`.
- Default ESPHome expected name: `bedroom-rgbww-strip`.
- The ESPHome host and expected name can be overridden with
  `CAMERA_BASED_RGBWW_OPTIMIZER_ESPHOME_HOST` and
  `CAMERA_BASED_RGBWW_OPTIMIZER_ESPHOME_EXPECTED_NAME`.
- ESPHome firmware files live under `firmware/esphome/` and use the same
  `bedroom-rgbww-strip` device name.

## Temporary Data

All project temporary data, captures, decoded images, probe outputs, and scratch
artifacts must live under the repository `tmp/` directory. Do not write project
temporary data to the system `/tmp`.

Scripts that can leave the RGBWW fixture on must turn it off before exiting,
except explicit light-on/control tools.

## RGBWW Control

Use the ESPHome control module:

```python
from camera_based_rgbww_optimizer.control.esphome import light

light(cw, ww, r, g, b)
```

Command-line use:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.control.esphome 0 0 2048 0 0
.venv/bin/python -m camera_based_rgbww_optimizer.control.esphome 0 0 0 0 0
.venv/bin/python -m camera_based_rgbww_optimizer.control.esphome 2048 0 0 0 0 --no-curve
```

If `config/channel/code-duty-curve.json` exists, input values are treated as
desired linear brightness codes and mapped through the measured code-duty curve
before sending PWM codes. Use `--no-curve` or `curve_path=None` for raw PWM
tests. If the curve file does not exist, values pass through directly. All-zero
output calls the ESPHome `all_off` action.

## Camera Control

Validated local setup on 2026-06-05:

- Host tool: Homebrew `gphoto2 2.5.32`
- Camera library: `libgphoto2 2.5.34`
- Detected camera: `Canon EOS R6 Mark III`
- Camera firmware/device version reported by gphoto2: `3-1.0.2`

Detect and capture:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.control.camera_gphoto2 detect
.venv/bin/python -m camera_based_rgbww_optimizer.control.camera_gphoto2 capture \
  --output-dir tmp/captures/camera \
  --iso 100 \
  --aperture 4 \
  --shutter-speed 1/30 \
  --image-format RAW
```

Decode a CR3 into linear 16-bit camera RGB:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.control.camera_gphoto2 decode tmp/captures/camera/example.cr3 \
  --output-dir tmp/captures/decoded \
  --format npy \
  --format tiff
```

The decode path uses `rawpy`/LibRaw with raw color output, no camera/auto white
balance, no auto-brightening, linear gamma, and 16-bit output. LibRaw handles RAW
black/white level correction before demosaicing; do not apply a second manual
black-level subtraction to the decoded output.

Auto-exposure target:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.control.camera_gphoto2 auto-expose \
  --output-dir tmp/captures/camera \
  --decode-output-dir tmp/captures/decoded \
  --target-max 49152 \
  --iso 100 \
  --aperture 4 \
  --min-shutter-speed 1/8000 \
  --max-shutter-speed 30 \
  --max-trials 5 \
  --max-captures 10
```

The auto-exposure routine keeps ISO and aperture fixed, starts from the current
bounded shutter speed, meters decoded linear RGB, caps total shutter releases,
deletes trial files, and saves only the accepted final capture.

## Location Picker

Use the local Web UI to capture a preview and mark color-chart patches:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.interaction.location_picker_ui \
  --blocks 24 \
  --rows 4 \
  --cols 6
```

The UI serves on `127.0.0.1:8765` by default. It auto-exposes, decodes a preview,
runs automatic block detection, then lets the user edit quadrilaterals. Confirmed
locations are saved under `config/location/`.

The Python packages used by this path are installed by `pip install -e .`.

## Channel Response Measurement

Measure per-channel code/duty response:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.optimization.measure_channel_response \
  --channels cw,ww,r,g,b \
  --dry-run
```

Run a real high-output sweep only after confirming the strip and camera setup:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.optimization.measure_channel_response \
  --channels cw,ww,r,g,b \
  --location-config config/location/locations-20260605-225800.json \
  --block-indices all \
  --allow-high-output
```

The measurement script turns the light off before and after the run, captures a
fixed all-off ambient frame at `ISO 100` / `30s`, measures codes from high to low
down to `8`, normalizes by shutter seconds, subtracts ambient, and writes an
incremental JSON report under `tmp/channel-response/`.

## Response Merge And Curve Generation

Merge compact measurement data:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.utils.merge_channel_response
```

Generate the compact code-duty correction curve:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.utils.generate_channel_curve \
  --input tmp/channel-response/merged/channel-code-duty-response-merged.json \
  --output config/channel/code-duty-curve.json
```

The full merged response JSON is the measurement archive for later color
modeling. The code-duty correction path only needs the small curve file.

## Ray120c Control

Use Amaran Desktop OpenAPI v2 for Ray120c reference control:

```bash
.venv/bin/python -m camera_based_rgbww_optimizer.control.ray120c set-cct 1800 --intensity-percent 1 --gm 200
.venv/bin/python -m camera_based_rgbww_optimizer.control.ray120c get-cct
.venv/bin/python -m camera_based_rgbww_optimizer.control.ray120c set-hsl 60 50 --intensity-percent 1
.venv/bin/python -m camera_based_rgbww_optimizer.control.ray120c get-hsl
.venv/bin/python -m camera_based_rgbww_optimizer.control.ray120c set-rgb 12 34 56 --intensity-percent 1
.venv/bin/python -m camera_based_rgbww_optimizer.control.ray120c get-rgb
```

Confirmed Ray120c ranges:

- CCT: `1800K..20000K`
- Intensity raw: `0..1000`, where `10` is `1%`
- G/M raw: `0..200`, where `100` is neutral
- HSI/HSL hue: `0..360`
- HSI/HSL saturation: `0..100`
- RGB: `0..255` per channel

## Calibration Direction

White mode should match Ray120c 100% brightness across:

- CCT: `1800K..20000K`
- G/M offset: `-1.0..+1.0`

Initial CCT/G-M LUT grid:

- CCT points: `1800, 2000, 2300, 2700, 3200, 3800, 4500, 5000, 5600, 6500, 8000, 10000, 14000, 20000`
- G/M raw points: `0, 70, 100, 130, 200`
- Total initial grid: `14 x 5 = 70` Ray120c target points

Use model-first matching in camera-observed color space, then add sparse
LUT/residual correction only where real captures show systematic error. If CCT
calibration measurements point to a better color calibration method, update the
strategy to follow the measured evidence.

## Validation

Run:

```bash
.venv/bin/python -m pip install -e .
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```
