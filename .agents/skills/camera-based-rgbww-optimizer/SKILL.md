---
name: camera-based-rgbww-optimizer
description: Use when working on camera-based RGBWW calibration, RGBWW channel control, Canon camera capture, Ray120c reference matching, or color-chart workflows.
---

# Camera-Based RGBWW Optimizer Skill

## Project Facts

- Planned optimizer language: Python.
- Implementation package: `src/camera_based_rgbww_optimizer/`.
- Source layout: `control/` for device control, `optimization/` for measurement/calibration workflows, `utils/` for shared helpers and post-processing, and `interaction/` for local UI tools.
- Root-level compatibility scripts are intentionally not part of the current interface.
- ESPHome device name and default host: `bedroom-rgbww-strip` / `bedroom-rgbww-strip.local`.
- ESPHome firmware configs live under `firmware/esphome/bedroom-rgbww-strip*.yaml`.
- `.venv/bin/python -m pip install -e .` installs the package's Python dependencies; `gphoto2` remains a system dependency.
- Reference light: Aputure Amaran Ray120c at 100% brightness.
- Camera path: Canon EOS R6 Mark III over USB PTP using `gphoto2`.
- Measurement basis: fixed linear camera RAW RGB pipeline.
- Calibration scene: black card, white card, 18% gray card, 24-color chart, RGBWW output, and Ray120c output.

## Workflow

1. Check `docs/README.md` before changing calibration or device-control behavior.
2. Use package module commands, for example `.venv/bin/python -m camera_based_rgbww_optimizer.control.camera_gphoto2 ...`.
3. Keep all temporary project outputs under repository `tmp/`.
4. For channel tests, use low initial values and test one physical channel at a time.
5. For Canon R6 Mark III sessions, use `camera_based_rgbww_optimizer.control.camera_gphoto2`.
6. For Ray120c CCT/G-M, HSI, and RGB reference control, use `camera_based_rgbww_optimizer.control.ray120c`.
7. For RGBWW code/duty response measurement, use `camera_based_rgbww_optimizer.optimization.measure_channel_response`.
8. For color-block location annotation, use `camera_based_rgbww_optimizer.interaction.location_picker_ui`.

## Calibration Direction

- White mode should match Ray120c 100% brightness across `1800K..20000K` CCT and `-1.0..+1.0` G/M offset.
- Use the first-pass CCT/G-M LUT grid from `docs/README.md`.
- All optimizer logic should operate on gamma-calibrated channel values, not raw PWM values.
- Color mode should use model-first matching in camera-observed color space plus sparse residual correction where measurements justify it.
- If CCT/G-M calibration measurements reveal a better color-mode model or solver structure, update the strategy to follow the measured evidence.

## Safety

- Do not run broad high-output sweeps without an explicit bounded range.
- Confirm channel order, brightness scaling, and thermal behavior before increasing PWM levels.
- All scripts that can leave the RGBWW fixture on must explicitly turn it off before exiting, except dedicated light-on/control tools.
