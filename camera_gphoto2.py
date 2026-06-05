from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_CAMERA_MODEL = "Canon EOS R6 Mark III"
DEFAULT_ISO = "100"
DEFAULT_APERTURE = "4"
DEFAULT_SHUTTER_SPEED = "1/30"
DEFAULT_IMAGE_FORMAT = "RAW"
DEFAULT_FILENAME_TEMPLATE = "%Y%m%d-%H%M%S.%C"

CONFIG_ISO = "/main/imgsettings/iso"
CONFIG_APERTURE = "/main/capturesettings/aperture"
CONFIG_SHUTTER_SPEED = "/main/capturesettings/shutterspeed"
CONFIG_IMAGE_FORMAT = "/main/imgsettings/imageformat"


class GPhoto2Error(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandOutput:
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class CameraConnection:
    model: str
    port: str


@dataclass(frozen=True)
class CaptureSettings:
    iso: str = DEFAULT_ISO
    aperture: str = DEFAULT_APERTURE
    shutter_speed: str = DEFAULT_SHUTTER_SPEED
    image_format: str = DEFAULT_IMAGE_FORMAT


@dataclass(frozen=True)
class CaptureResult:
    connection: CameraConnection
    settings: CaptureSettings
    saved_file: Path
    stdout: str

    def to_jsonable(self) -> dict[str, object]:
        return {
            "model": self.connection.model,
            "port": self.connection.port,
            "iso": self.settings.iso,
            "aperture": self.settings.aperture,
            "shutter_speed": self.settings.shutter_speed,
            "image_format": self.settings.image_format,
            "saved_file": str(self.saved_file),
        }


Runner = Callable[[Sequence[str]], CommandOutput]


def run_gphoto2(
    args: Sequence[str],
    *,
    executable: str = "gphoto2",
    timeout: float = 30.0,
) -> CommandOutput:
    command = [executable, *args]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise GPhoto2Error(f"{' '.join(command)} failed: {details}")
    return CommandOutput(stdout=completed.stdout, stderr=completed.stderr)


def parse_auto_detect(output: str) -> list[CameraConnection]:
    cameras: list[CameraConnection] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Model") or set(stripped) <= {"-"}:
            continue
        match = re.match(r"(?P<model>.+?)\s{2,}(?P<port>\S+)$", stripped)
        if match:
            cameras.append(CameraConnection(model=match.group("model").strip(), port=match.group("port")))
    return cameras


def auto_detect_camera(
    *,
    expected_model: str = DEFAULT_CAMERA_MODEL,
    runner: Runner | None = None,
    executable: str = "gphoto2",
    timeout: float = 30.0,
) -> CameraConnection:
    output = _run(["--auto-detect"], runner=runner, executable=executable, timeout=timeout)
    cameras = parse_auto_detect(output.stdout)
    for camera in cameras:
        if camera.model == expected_model:
            return camera
    available = ", ".join(f"{camera.model} ({camera.port})" for camera in cameras) or "none"
    raise GPhoto2Error(f"{expected_model} not detected; available cameras: {available}")


def set_capture_settings(
    port: str,
    settings: CaptureSettings,
    *,
    runner: Runner | None = None,
    executable: str = "gphoto2",
    timeout: float = 30.0,
) -> None:
    args = ["--port", port]
    args.extend(["--set-config", f"{CONFIG_ISO}={settings.iso}"])
    args.extend(["--set-config", f"{CONFIG_APERTURE}={settings.aperture}"])
    args.extend(["--set-config", f"{CONFIG_SHUTTER_SPEED}={settings.shutter_speed}"])
    args.extend(["--set-config", f"{CONFIG_IMAGE_FORMAT}={settings.image_format}"])
    _run(args, runner=runner, executable=executable, timeout=timeout)


def read_current_value(
    port: str,
    config_path: str,
    *,
    runner: Runner | None = None,
    executable: str = "gphoto2",
    timeout: float = 30.0,
) -> str:
    output = _run(
        ["--port", port, "--get-config", config_path],
        runner=runner,
        executable=executable,
        timeout=timeout,
    )
    for line in output.stdout.splitlines():
        if line.startswith("Current:"):
            return line.split(":", 1)[1].strip()
    raise GPhoto2Error(f"could not read current value for {config_path}")


def capture_image(
    *,
    output_dir: str | Path,
    filename_template: str = DEFAULT_FILENAME_TEMPLATE,
    settings: CaptureSettings = CaptureSettings(),
    port: str | None = None,
    expected_model: str = DEFAULT_CAMERA_MODEL,
    keep_on_camera: bool = False,
    allow_bulb: bool = False,
    verify_saved: bool = True,
    runner: Runner | None = None,
    executable: str = "gphoto2",
    timeout: float = 30.0,
) -> CaptureResult:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    connection = (
        CameraConnection(model=expected_model, port=port)
        if port is not None
        else auto_detect_camera(
            expected_model=expected_model,
            runner=runner,
            executable=executable,
            timeout=timeout,
        )
    )

    set_capture_settings(
        connection.port,
        settings,
        runner=runner,
        executable=executable,
        timeout=timeout,
    )

    current_shutter = read_current_value(
        connection.port,
        CONFIG_SHUTTER_SPEED,
        runner=runner,
        executable=executable,
        timeout=timeout,
    )
    if current_shutter.lower() == "bulb" and not allow_bulb:
        raise GPhoto2Error("refusing to capture with bulb shutter speed; set a bounded shutter speed first")

    filename = str(output_path / filename_template)
    args = [
        "--port",
        connection.port,
        "--capture-image-and-download",
        "--filename",
        filename,
    ]
    if keep_on_camera:
        args.append("--keep")
    output = _run(args, runner=runner, executable=executable, timeout=timeout)
    saved_file = _parse_saved_file(output.stdout)
    if verify_saved and not saved_file.exists():
        raise GPhoto2Error(f"gphoto2 reported saved file, but it does not exist: {saved_file}")

    return CaptureResult(
        connection=connection,
        settings=settings,
        saved_file=saved_file,
        stdout=output.stdout,
    )


def _parse_saved_file(output: str) -> Path:
    for line in output.splitlines():
        match = re.match(r"Saving file as (?P<path>.+)$", line.strip())
        if match:
            return Path(match.group("path"))
    raise GPhoto2Error("could not parse saved file path from gphoto2 output")


def _run(
    args: Sequence[str],
    *,
    runner: Runner | None,
    executable: str,
    timeout: float,
) -> CommandOutput:
    if runner is not None:
        return runner(args)
    return run_gphoto2(args, executable=executable, timeout=timeout)


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture Canon EOS R6 Mark III RAW files through gphoto2.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect_parser = subparsers.add_parser("detect", help="Auto-detect the configured camera.")
    detect_parser.add_argument("--model", default=DEFAULT_CAMERA_MODEL)
    detect_parser.add_argument("--gphoto2", default="gphoto2")
    detect_parser.add_argument("--timeout", type=float, default=30.0)

    capture_parser = subparsers.add_parser("capture", help="Set exposure parameters and capture a RAW file.")
    capture_parser.add_argument("--model", default=DEFAULT_CAMERA_MODEL)
    capture_parser.add_argument("--port", help="gphoto2 USB port. Defaults to auto-detect.")
    capture_parser.add_argument("--gphoto2", default="gphoto2")
    capture_parser.add_argument("--timeout", type=float, default=30.0)
    capture_parser.add_argument("--output-dir", default="captures/camera")
    capture_parser.add_argument("--filename-template", default=DEFAULT_FILENAME_TEMPLATE)
    capture_parser.add_argument("--iso", default=DEFAULT_ISO)
    capture_parser.add_argument("--aperture", default=DEFAULT_APERTURE)
    capture_parser.add_argument("--shutter-speed", default=DEFAULT_SHUTTER_SPEED)
    capture_parser.add_argument("--image-format", default=DEFAULT_IMAGE_FORMAT)
    capture_parser.add_argument("--keep", action="store_true", help="Keep the captured file on the camera.")
    capture_parser.add_argument("--allow-bulb", action="store_true", help="Allow capture when readback is bulb.")

    args = parser.parse_args(argv)

    try:
        if args.command == "detect":
            connection = auto_detect_camera(
                expected_model=args.model,
                executable=args.gphoto2,
                timeout=args.timeout,
            )
            _print_json({"model": connection.model, "port": connection.port})
        elif args.command == "capture":
            result = capture_image(
                output_dir=args.output_dir,
                filename_template=args.filename_template,
                settings=CaptureSettings(
                    iso=args.iso,
                    aperture=args.aperture,
                    shutter_speed=args.shutter_speed,
                    image_format=args.image_format,
                ),
                port=args.port,
                expected_model=args.model,
                keep_on_camera=args.keep,
                allow_bulb=args.allow_bulb,
                executable=args.gphoto2,
                timeout=args.timeout,
            )
            _print_json(result.to_jsonable())
        else:
            raise AssertionError(args.command)
    except Exception as exc:
        print(f"camera_gphoto2: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
