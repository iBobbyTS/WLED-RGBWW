# ESPHome Firmware Preparation

This directory contains reproducible ESPHome firmware candidates for the
GLEDOPTO GL-C-211WL controller.

Current target:

- Board: `esp32dev`
- Framework: ESP-IDF
- PWM outputs: five independent ESP32 LEDC channels
- GPIO order, from observed WLED config: `R=19`, `G=18`, `B=17`, `WW=16`, `CW=4`
- PWM frequency: `19531 Hz`, matching the observed WLED setup and preserving about 12-bit duty resolution on the original ESP32 LEDC peripheral
- Optimizer API: `set_rgbww_12bit(red, green, blue, warm_white, cold_white)` with integer channel values from `0` to `4095`

## Config Files

- `wled-bedroom-rgbww-common.yaml`: shared hardware, Wi-Fi, API, OTA, LEDC, and
  boot-off behavior.
- `wled-bedroom-rgbww-no-webui.yaml`: no browser UI. This is the variant that
  has been flashed and verified to boot, join Wi-Fi, expose ESPHome Native API
  on port `6053`, and expose ESPHome OTA on port `3232`. Port `80` refusing
  connections is expected for this variant.
- `wled-bedroom-rgbww-webui.yaml`: adds Basic-Auth-protected `web_server` on
  port `80`. This variant has compiled successfully but has not yet been flashed
  and validated on the device.
- `wled-bedroom-rgbww.yaml`: default alias for the Web UI variant.

Both explicit variants keep the same device name, `wled-bedroom-rgbww`, but use
separate build directories so their generated binaries do not overwrite each
other.

## Local Compile

From the repository root:

```bash
uv venv .venv
uv pip install -r firmware/esphome/requirements.txt
```

No-WebUI build:

```bash
.venv/bin/esphome config firmware/esphome/wled-bedroom-rgbww-no-webui.yaml
.venv/bin/esphome compile firmware/esphome/wled-bedroom-rgbww-no-webui.yaml
```

WebUI build:

```bash
.venv/bin/esphome config firmware/esphome/wled-bedroom-rgbww-webui.yaml
.venv/bin/esphome compile firmware/esphome/wled-bedroom-rgbww-webui.yaml
```

## Build Outputs

No-WebUI variant:

- Status: compiled and verified on hardware for boot, Wi-Fi, Native API, and
  OTA reachability; browser access is intentionally unavailable.
- OTA binary:
  `firmware/esphome/.esphome/build/wled-bedroom-rgbww-no-webui/.pioenvs/wled-bedroom-rgbww/firmware.ota.bin`
  (`SHA256 f599a5544fdb0e76dbf121ac8bcc8978dc66e5c35dbc66f4bd48a3ff3b304da1`)
- Factory binary:
  `firmware/esphome/.esphome/build/wled-bedroom-rgbww-no-webui/.pioenvs/wled-bedroom-rgbww/firmware.factory.bin`
  (`SHA256 bb3bee426e58edc57fcc6173fcda99cae8a13532dd45ba4d26ad7aa971549d26`)

WebUI variant:

- Status: compiled successfully, but not yet flashed and not yet verified on
  hardware. Expected browser URL after OTA is `http://wled-bedroom-rgbww.local/`
  with Basic Auth credentials from ignored `secrets.yaml`.
- OTA binary:
  `firmware/esphome/.esphome/build/wled-bedroom-rgbww-webui/.pioenvs/wled-bedroom-rgbww/firmware.ota.bin`
  (`SHA256 2fc0b9690e7c646e8ff4d5d9e51898b9351a4b044ebb9f7ee8831a1648cf6813`)
- Factory binary:
  `firmware/esphome/.esphome/build/wled-bedroom-rgbww-webui/.pioenvs/wled-bedroom-rgbww/firmware.factory.bin`
  (`SHA256 526161ce202aba13163011030509ebecaed33ed79ff448c9947956d8ded23f75`)

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
.venv/bin/esphome upload firmware/esphome/wled-bedroom-rgbww-no-webui.yaml --device wled-bedroom-rgbww.local
```

WebUI OTA command:

```bash
.venv/bin/esphome upload firmware/esphome/wled-bedroom-rgbww-webui.yaml --device wled-bedroom-rgbww.local
```

## Do Not Flash Yet

Do not run `esphome run`, `esphome upload`, or upload the generated binary to
the WLED `/update` page until the physical recovery path is confirmed or the
user explicitly approves the flash step.

Before flashing a first ESPHome image, confirm:

- `secrets.yaml` contains real Wi-Fi credentials, a strong fallback AP password,
  a saved API encryption key, and an OTA password.
- The GPIO order is accepted as provisional until the strip is tested at low
  levels after flashing.
- WLED web OTA is only a booting-firmware update path, not brick recovery.
- There is a known way to enter the ESP32 ROM bootloader and access UART0
  `TX/RX`, or the user has accepted the soft-brick risk.
