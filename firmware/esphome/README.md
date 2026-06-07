# ESPHome Firmware Preparation

This directory contains reproducible ESPHome firmware candidates for the
GLEDOPTO GL-C-211WL controller.

Current target:

- Board: `esp32dev`
- Framework: ESP-IDF
- PWM outputs: five independent ESP32 LEDC channels
- GPIO order, from the original controller config: `R=19`, `G=18`, `B=17`, `WW=16`, `CW=4`
- PWM frequency: `19531 Hz`, matching the original setup and preserving about 12-bit duty resolution on the original ESP32 LEDC peripheral
- Native API action: `set_rgbww_12bit(red, green, blue, warm_white, cold_white)` with integer channel values from `0` to `4095`
- ESPHome device name: `bedroom-rgbww-strip`

## Config Files

- `bedroom-rgbww-strip-common.yaml`: shared hardware, Wi-Fi, API, OTA, LEDC,
  and boot-off behavior.
- `bedroom-rgbww-strip-no-webui.yaml`: no browser UI. Port `80`
  refusing connections is expected for this variant.
- `bedroom-rgbww-strip-webui.yaml`: adds Basic-Auth-protected `web_server` on
  port `80`.
- `bedroom-rgbww-strip.yaml`: default alias for the Web UI variant.

Both explicit variants keep the same device name,
`bedroom-rgbww-strip`, but use separate build directories so their generated
binaries do not overwrite each other.

## Local Compile

From the repository root:

```bash
uv venv .venv
uv pip install -r firmware/esphome/requirements.txt
```

No-WebUI build:

```bash
.venv/bin/esphome config firmware/esphome/bedroom-rgbww-strip-no-webui.yaml
.venv/bin/esphome compile firmware/esphome/bedroom-rgbww-strip-no-webui.yaml
```

WebUI build:

```bash
.venv/bin/esphome config firmware/esphome/bedroom-rgbww-strip-webui.yaml
.venv/bin/esphome compile firmware/esphome/bedroom-rgbww-strip-webui.yaml
```

## Build Outputs

No-WebUI variant:

- Status: config-validated as `bedroom-rgbww-strip` on 2026-06-06 after the
  device rename. Compile and hardware validation still need to be rerun for
  this variant after the rename.
- Expected OTA binary after compile:
  `firmware/esphome/.esphome/build/bedroom-rgbww-strip-no-webui/.pioenvs/bedroom-rgbww-strip/firmware.ota.bin`
- Expected factory binary after compile:
  `firmware/esphome/.esphome/build/bedroom-rgbww-strip-no-webui/.pioenvs/bedroom-rgbww-strip/firmware.factory.bin`

WebUI variant:

- Status: compiled and OTA-validated on hardware as `bedroom-rgbww-strip` on
  2026-06-06.
- Hostname after OTA: `bedroom-rgbww-strip.local`, resolving to
  `192.168.31.21` during validation.
- Verified ports after OTA: Native API `6053`, ESPHome OTA `3232`, WebUI `80`.
- Native API device info returned name `bedroom-rgbww-strip` and user services
  `set_rgbww_12bit` and `all_off`.
- Unauthenticated WebUI returned HTTP `401`, matching Basic Auth from ignored
  `secrets.yaml`.
- Final validation sent `all_off` successfully:
  `{'red': 0, 'green': 0, 'blue': 0, 'warm_white': 0, 'cold_white': 0}`.
- OTA binary:
  `firmware/esphome/.esphome/build/bedroom-rgbww-strip-webui/.pioenvs/bedroom-rgbww-strip/firmware.ota.bin`
  (`885552 bytes`, `SHA256 57f37196bab1af3289f9a464ef83b334d2d73f32e6a59b92717240e03d0fcbb3`)
- Factory binary:
  `firmware/esphome/.esphome/build/bedroom-rgbww-strip-webui/.pioenvs/bedroom-rgbww-strip/firmware.factory.bin`
  (`951088 bytes`, `SHA256 23e0d601dc4bd2083ecd60a782c4ae6fb12c59df38d593fbf49b2c17e75975fb`)

ESPHome/PlatformIO stores toolchains under `~/.platformio/` and build outputs
under `firmware/esphome/.esphome/`.

`firmware/esphome/secrets.yaml` is intentionally ignored by git. The checked-in
`secrets.example.yaml` documents the required keys. The local ignored
`secrets.yaml` currently contains the `FireflyIoT` Wi-Fi credentials in
plaintext for this build.

## OTA Usage

Use the `firmware.ota.bin` from the selected variant for ESPHome OTA updates.
Use `firmware.factory.bin` only for serial/WebSerial full flashing from offset
`0x0`.

No-WebUI OTA command:

```bash
.venv/bin/esphome upload firmware/esphome/bedroom-rgbww-strip-no-webui.yaml --device bedroom-rgbww-strip.local
```

WebUI OTA command:

```bash
.venv/bin/esphome upload firmware/esphome/bedroom-rgbww-strip-webui.yaml --device bedroom-rgbww-strip.local
```

## Flash Safety

Do not run `esphome run`, `esphome upload`, or upload the generated binary to
the controller until the physical recovery path is confirmed or the user
explicitly approves the flash step.

Before flashing a new ESPHome image, confirm:

- `secrets.yaml` contains real Wi-Fi credentials, a strong fallback AP password,
  a saved API encryption key, and an OTA password.
- The GPIO order is accepted as provisional until the strip is tested at low
  levels after flashing.
- There is a known way to enter the ESP32 ROM bootloader and access UART0
  `TX/RX`, or the user has accepted the soft-brick risk.
