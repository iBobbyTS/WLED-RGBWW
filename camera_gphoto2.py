from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence


DEFAULT_CAMERA_MODEL = "Canon EOS R6 Mark III"
DEFAULT_ISO = "100"
DEFAULT_APERTURE = "4"
DEFAULT_SHUTTER_SPEED = "1/30"
DEFAULT_IMAGE_FORMAT = "RAW"
DEFAULT_FILENAME_TEMPLATE = "%Y%m%d-%H%M%S.%C"
DEFAULT_DECODE_STEM_SUFFIX = ".rawpy-linear-camera-rgb16"
DEFAULT_DECODE_FORMATS = ("npy", "tiff")
DEFAULT_AUTO_EXPOSURE_MAX = 49152
DEFAULT_MIN_SHUTTER_SPEED = "1/8000"
DEFAULT_MAX_SHUTTER_SPEED = "30"

CONFIG_ISO = "/main/imgsettings/iso"
CONFIG_APERTURE = "/main/capturesettings/aperture"
CONFIG_SHUTTER_SPEED = "/main/capturesettings/shutterspeed"
CONFIG_IMAGE_FORMAT = "/main/imgsettings/imageformat"
PROJECT_TMP_DIR = Path(__file__).resolve().parent / "tmp"


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
    decoded: "DecodeResult | None" = None

    def to_jsonable(self) -> dict[str, object]:
        data: dict[str, object] = {
            "model": self.connection.model,
            "port": self.connection.port,
            "iso": self.settings.iso,
            "aperture": self.settings.aperture,
            "shutter_speed": self.settings.shutter_speed,
            "image_format": self.settings.image_format,
            "saved_file": str(self.saved_file),
        }
        if self.decoded is not None:
            data["decoded"] = self.decoded.to_jsonable()
        return data


@dataclass(frozen=True)
class DecodeResult:
    source_file: Path
    output_files: tuple[Path, ...]
    metadata_file: Path
    image_shape: tuple[int, ...]
    image_dtype: str
    stats: dict[str, Any]

    def to_jsonable(self) -> dict[str, object]:
        return {
            "source_file": str(self.source_file),
            "output_files": [str(path) for path in self.output_files],
            "metadata_file": str(self.metadata_file),
            "image_shape": list(self.image_shape),
            "image_dtype": self.image_dtype,
            "image_max": _nested_stat(self.stats, "image", "max"),
            "raw_visible_max": _nested_stat(self.stats, "raw_visible", "max"),
            "black_level_per_channel": self.stats.get("black_level_per_channel"),
            "white_level": self.stats.get("white_level"),
            "camera_white_level_per_channel": self.stats.get("camera_white_level_per_channel"),
        }


@dataclass(frozen=True)
class AutoExposureTrial:
    index: int
    shutter_speed: str
    decoded_max: int
    raw_visible_max: int | None
    accepted: bool

    def to_jsonable(self) -> dict[str, object]:
        return {
            "index": self.index,
            "shutter_speed": self.shutter_speed,
            "decoded_max": self.decoded_max,
            "raw_visible_max": self.raw_visible_max,
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class AutoExposureResult:
    target_max: int
    final_capture: CaptureResult
    trials: tuple[AutoExposureTrial, ...]
    rejected_finals: tuple[AutoExposureTrial, ...] = ()

    def to_jsonable(self) -> dict[str, object]:
        return {
            "target_max": self.target_max,
            "final": self.final_capture.to_jsonable(),
            "trials": [trial.to_jsonable() for trial in self.trials],
            "rejected_finals": [trial.to_jsonable() for trial in self.rejected_finals],
        }


Runner = Callable[[Sequence[str]], CommandOutput]
Decoder = Callable[..., DecodeResult]


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


def read_config_choices(
    port: str,
    config_path: str,
    *,
    runner: Runner | None = None,
    executable: str = "gphoto2",
    timeout: float = 30.0,
) -> list[str]:
    output = _run(
        ["--port", port, "--get-config", config_path],
        runner=runner,
        executable=executable,
        timeout=timeout,
    )
    choices: list[str] = []
    for line in output.stdout.splitlines():
        match = re.match(r"^Choice:\s+\d+\s+(?P<value>.+)$", line.strip())
        if match:
            choices.append(match.group("value").strip())
    if not choices:
        raise GPhoto2Error(f"could not read choices for {config_path}")
    return choices


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


def decode_raw_image(
    raw_file: str | Path,
    *,
    output_dir: str | Path | None = None,
    output_stem: str | None = None,
    formats: Sequence[str] = DEFAULT_DECODE_FORMATS,
    demosaic_algorithm: str = "AHD",
    rawpy_module: Any | None = None,
    numpy_module: Any | None = None,
    tifffile_module: Any | None = None,
) -> DecodeResult:
    """Decode a RAW file to linear 16-bit camera-RGB outputs.

    The decode path intentionally keeps the image in camera RAW RGB space:
    no color matrix, no camera/auto white balance, no auto-brightening, and
    gamma=(1, 1). LibRaw/rawpy performs the RAW black/white level handling
    before demosaicing.
    """

    source_path = Path(raw_file)
    if not source_path.exists():
        raise GPhoto2Error(f"RAW file does not exist: {source_path}")

    normalized_formats = _normalize_decode_formats(formats)
    target_dir = Path(output_dir) if output_dir is not None else source_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem or f"{source_path.stem}{DEFAULT_DECODE_STEM_SUFFIX}"

    rawpy = rawpy_module or _import_required("rawpy", "Install rawpy to decode Canon CR3 files.")
    np = numpy_module or _import_required("numpy", "Install numpy to write decoded arrays.")
    tifffile = tifffile_module
    if "tiff" in normalized_formats and tifffile is None:
        tifffile = _import_required("tifffile", "Install tifffile to write 16-bit TIFF outputs.")

    algorithm = _get_rawpy_enum(rawpy.DemosaicAlgorithm, demosaic_algorithm, "demosaic algorithm")
    output_color = rawpy.ColorSpace.raw

    with rawpy.imread(str(source_path)) as raw:
        raw_visible = raw.raw_image_visible
        stats: dict[str, Any] = {
            "source_file": str(source_path),
            "rawpy_version": getattr(rawpy, "__version__", None),
            "libraw_version": getattr(rawpy, "libraw_version", None),
            "sizes": _rawpy_sizes_to_dict(raw.sizes),
            "num_colors": int(raw.num_colors),
            "color_desc": _decode_color_desc(raw.color_desc),
            "raw_pattern": raw.raw_pattern.tolist() if raw.raw_pattern is not None else None,
            "black_level_per_channel": [int(value) for value in raw.black_level_per_channel],
            "white_level": int(raw.white_level) if raw.white_level is not None else None,
            "camera_white_level_per_channel": (
                [int(value) for value in raw.camera_white_level_per_channel]
                if raw.camera_white_level_per_channel is not None
                else None
            ),
            "raw_visible": _array_stats(np, raw_visible),
            "postprocess_params": {
                "demosaic_algorithm": demosaic_algorithm,
                "output_color": "raw",
                "use_camera_wb": False,
                "use_auto_wb": False,
                "user_wb": [1.0, 1.0, 1.0, 1.0],
                "no_auto_bright": True,
                "bright": 1.0,
                "gamma": [1.0, 1.0],
                "output_bps": 16,
                "user_flip": 0,
            },
        }
        image = raw.postprocess(
            demosaic_algorithm=algorithm,
            output_color=output_color,
            use_camera_wb=False,
            use_auto_wb=False,
            user_wb=[1.0, 1.0, 1.0, 1.0],
            no_auto_bright=True,
            bright=1.0,
            gamma=(1.0, 1.0),
            output_bps=16,
            user_flip=0,
        )

    stats["image"] = _array_stats(np, image)
    output_files: list[Path] = []

    if "npy" in normalized_formats:
        npy_path = target_dir / f"{stem}.npy"
        np.save(npy_path, image)
        output_files.append(npy_path)

    if "tiff" in normalized_formats:
        tiff_path = target_dir / f"{stem}.tiff"
        tifffile.imwrite(
            tiff_path,
            image,
            photometric="rgb",
            metadata={
                "description": (
                    "rawpy output_color=raw gamma=(1,1) no_auto_bright=True "
                    "use_camera_wb=False use_auto_wb=False user_wb=[1,1,1,1] output_bps=16"
                )
            },
        )
        output_files.append(tiff_path)

    metadata_path = target_dir / f"{stem}.json"
    metadata_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    output_files.append(metadata_path)

    return DecodeResult(
        source_file=source_path,
        output_files=tuple(output_files),
        metadata_file=metadata_path,
        image_shape=tuple(int(value) for value in image.shape),
        image_dtype=str(image.dtype),
        stats=stats,
    )


def auto_expose_capture(
    *,
    output_dir: str | Path,
    filename_template: str = DEFAULT_FILENAME_TEMPLATE,
    target_max: int = DEFAULT_AUTO_EXPOSURE_MAX,
    iso: str = DEFAULT_ISO,
    aperture: str = DEFAULT_APERTURE,
    image_format: str = DEFAULT_IMAGE_FORMAT,
    min_shutter_speed: str = DEFAULT_MIN_SHUTTER_SPEED,
    max_shutter_speed: str = DEFAULT_MAX_SHUTTER_SPEED,
    shutter_speeds: Sequence[str] | None = None,
    decode_output_dir: str | Path | None = None,
    decode_formats: Sequence[str] = DEFAULT_DECODE_FORMATS,
    port: str | None = None,
    expected_model: str = DEFAULT_CAMERA_MODEL,
    allow_bulb: bool = False,
    keep_on_camera: bool = False,
    runner: Runner | None = None,
    executable: str = "gphoto2",
    timeout: float = 60.0,
    decoder: Decoder = decode_raw_image,
) -> AutoExposureResult:
    if target_max <= 0:
        raise GPhoto2Error("target_max must be positive")

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
    candidates = _select_shutter_candidates(
        connection.port,
        min_shutter_speed=min_shutter_speed,
        max_shutter_speed=max_shutter_speed,
        shutter_speeds=shutter_speeds,
        runner=runner,
        executable=executable,
        timeout=timeout,
    )

    trials: list[AutoExposureTrial] = []
    best_index: int | None = None
    low = 0
    high = len(candidates) - 1
    PROJECT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="auto-exposure-", dir=PROJECT_TMP_DIR) as tmpdir:
        trial_dir = Path(tmpdir) / "trials"
        trial_decode_dir = Path(tmpdir) / "decoded"
        while low <= high:
            mid = (low + high) // 2
            shutter_speed = candidates[mid]
            trial = _capture_decode_trial(
                index=len(trials),
                output_dir=trial_dir,
                filename_template=f"trial-{len(trials):03d}.cr3",
                settings=CaptureSettings(
                    iso=iso,
                    aperture=aperture,
                    shutter_speed=shutter_speed,
                    image_format=image_format,
                ),
                connection=connection,
                target_max=target_max,
                decode_output_dir=trial_decode_dir,
                decode_formats=("npy",),
                allow_bulb=allow_bulb,
                keep_on_camera=keep_on_camera,
                runner=runner,
                executable=executable,
                timeout=timeout,
                decoder=decoder,
                delete_after=True,
            )
            trials.append(trial)
            if trial.accepted:
                best_index = mid
                low = mid + 1
            else:
                high = mid - 1

    if best_index is None:
        trial_summary = ", ".join(f"{trial.shutter_speed}={trial.decoded_max}" for trial in trials)
        raise GPhoto2Error(f"no shutter speed met decoded max <= {target_max}; trials: {trial_summary}")

    rejected_finals: list[AutoExposureTrial] = []
    for candidate_index in range(best_index, -1, -1):
        shutter_speed = candidates[candidate_index]
        final_result = capture_image(
            output_dir=output_path,
            filename_template=filename_template,
            settings=CaptureSettings(
                iso=iso,
                aperture=aperture,
                shutter_speed=shutter_speed,
                image_format=image_format,
            ),
            port=connection.port,
            expected_model=connection.model,
            keep_on_camera=keep_on_camera,
            allow_bulb=allow_bulb,
            runner=runner,
            executable=executable,
            timeout=timeout,
        )
        decoded = decoder(
            final_result.saved_file,
            output_dir=decode_output_dir,
            formats=decode_formats,
        )
        final_result = CaptureResult(
            connection=final_result.connection,
            settings=final_result.settings,
            saved_file=final_result.saved_file,
            stdout=final_result.stdout,
            decoded=decoded,
        )
        decoded_max = _decoded_image_max(decoded)
        if decoded_max <= target_max:
            return AutoExposureResult(
                target_max=target_max,
                final_capture=final_result,
                trials=tuple(trials),
                rejected_finals=tuple(rejected_finals),
            )

        rejected_finals.append(
            AutoExposureTrial(
                index=len(rejected_finals),
                shutter_speed=shutter_speed,
                decoded_max=decoded_max,
                raw_visible_max=_decoded_raw_visible_max(decoded),
                accepted=False,
            )
        )
        _delete_capture_outputs(final_result)

    raise GPhoto2Error(f"final capture could not meet decoded max <= {target_max}")


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


def _select_shutter_candidates(
    port: str,
    *,
    min_shutter_speed: str,
    max_shutter_speed: str,
    shutter_speeds: Sequence[str] | None,
    runner: Runner | None,
    executable: str,
    timeout: float,
) -> tuple[str, ...]:
    choices = (
        list(shutter_speeds)
        if shutter_speeds is not None
        else read_config_choices(
            port,
            CONFIG_SHUTTER_SPEED,
            runner=runner,
            executable=executable,
            timeout=timeout,
        )
    )
    min_seconds = _parse_shutter_seconds(min_shutter_speed)
    max_seconds = _parse_shutter_seconds(max_shutter_speed)
    if min_seconds > max_seconds:
        raise GPhoto2Error("min shutter speed must be shorter than or equal to max shutter speed")

    parsed: list[tuple[Fraction, str]] = []
    for choice in choices:
        try:
            seconds = _parse_shutter_seconds(choice)
        except GPhoto2Error:
            continue
        if min_seconds <= seconds <= max_seconds:
            parsed.append((seconds, choice))
    parsed.sort(key=lambda item: item[0])
    candidates: list[str] = []
    seen: set[str] = set()
    for _seconds, choice in parsed:
        if choice not in seen:
            candidates.append(choice)
            seen.add(choice)
    if not candidates:
        raise GPhoto2Error("no shutter speed candidates available in the requested range")
    return tuple(candidates)


def _parse_shutter_seconds(value: str) -> Fraction:
    normalized = value.strip().lower()
    if normalized == "bulb":
        raise GPhoto2Error("bulb is not a bounded shutter speed")
    try:
        return Fraction(normalized)
    except ValueError as exc:
        raise GPhoto2Error(f"unsupported shutter speed value: {value}") from exc


def _capture_decode_trial(
    *,
    index: int,
    output_dir: Path,
    filename_template: str,
    settings: CaptureSettings,
    connection: CameraConnection,
    target_max: int,
    decode_output_dir: Path,
    decode_formats: Sequence[str],
    allow_bulb: bool,
    keep_on_camera: bool,
    runner: Runner | None,
    executable: str,
    timeout: float,
    decoder: Decoder,
    delete_after: bool,
) -> AutoExposureTrial:
    capture: CaptureResult | None = None
    decoded: DecodeResult | None = None
    try:
        capture = capture_image(
            output_dir=output_dir,
            filename_template=filename_template,
            settings=settings,
            port=connection.port,
            expected_model=connection.model,
            keep_on_camera=keep_on_camera,
            allow_bulb=allow_bulb,
            runner=runner,
            executable=executable,
            timeout=timeout,
        )
        decoded = decoder(capture.saved_file, output_dir=decode_output_dir, formats=decode_formats)
        decoded_max = _decoded_image_max(decoded)
        return AutoExposureTrial(
            index=index,
            shutter_speed=settings.shutter_speed,
            decoded_max=decoded_max,
            raw_visible_max=_decoded_raw_visible_max(decoded),
            accepted=decoded_max <= target_max,
        )
    finally:
        if delete_after:
            if decoded is not None:
                _delete_decode_outputs(decoded)
            if capture is not None:
                _delete_path(capture.saved_file)


def _decoded_image_max(decoded: DecodeResult) -> int:
    try:
        return int(decoded.stats["image"]["max"])
    except (KeyError, TypeError) as exc:
        raise GPhoto2Error("decoded result did not include image max statistics") from exc


def _decoded_raw_visible_max(decoded: DecodeResult) -> int | None:
    try:
        return int(decoded.stats["raw_visible"]["max"])
    except (KeyError, TypeError):
        return None


def _nested_stat(stats: dict[str, Any], section: str, key: str) -> int | float | None:
    try:
        value = stats[section][key]
    except (KeyError, TypeError):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _delete_capture_outputs(capture: CaptureResult) -> None:
    if capture.decoded is not None:
        _delete_decode_outputs(capture.decoded)
    _delete_path(capture.saved_file)


def _delete_decode_outputs(decoded: DecodeResult) -> None:
    for output_file in decoded.output_files:
        _delete_path(output_file)
    _delete_path(decoded.metadata_file)


def _delete_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except IsADirectoryError:
        pass


def _normalize_decode_formats(formats: Sequence[str]) -> tuple[str, ...]:
    if not formats:
        raise GPhoto2Error("at least one decode format is required")
    normalized: list[str] = []
    for value in formats:
        lower = value.lower()
        if lower not in {"npy", "tiff"}:
            raise GPhoto2Error(f"unsupported decode format: {value}")
        if lower not in normalized:
            normalized.append(lower)
    return tuple(normalized)


def _import_required(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise GPhoto2Error(f"missing optional dependency {module_name!r}. {install_hint}") from exc


def _get_rawpy_enum(enum_container: Any, name: str, label: str) -> Any:
    try:
        return getattr(enum_container, name)
    except AttributeError as exc:
        available = ", ".join(item for item in dir(enum_container) if item.isupper())
        raise GPhoto2Error(f"unsupported {label}: {name}; available: {available}") from exc


def _rawpy_sizes_to_dict(sizes: Any) -> dict[str, int]:
    names = ("raw_height", "raw_width", "height", "width", "top_margin", "left_margin", "iheight", "iwidth")
    return {name: int(getattr(sizes, name)) for name in names}


def _decode_color_desc(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _array_stats(np: Any, array: Any) -> dict[str, Any]:
    return {
        "shape": [int(value) for value in array.shape],
        "dtype": str(array.dtype),
        "min": int(array.min()),
        "max": int(array.max()),
        "channel_min": _channel_stat(array, "min"),
        "channel_max": _channel_stat(array, "max"),
        "channel_mean": _channel_stat(array, "mean"),
        "percentiles": [float(value) for value in np.percentile(array, [0, 0.01, 0.1, 1, 50, 99, 99.9, 99.99, 100])],
    }


def _channel_stat(array: Any, stat: str) -> list[int | float] | None:
    if len(array.shape) != 3:
        return None
    values: list[int | float] = []
    for channel in range(array.shape[2]):
        channel_array = array[..., channel]
        if stat == "min":
            values.append(int(channel_array.min()))
        elif stat == "max":
            values.append(int(channel_array.max()))
        elif stat == "mean":
            values.append(float(channel_array.mean()))
        else:
            raise AssertionError(stat)
    return values


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
    capture_parser.add_argument(
        "--decode-linear",
        action="store_true",
        help="Decode the captured RAW to linear 16-bit camera-RGB outputs with rawpy.",
    )
    capture_parser.add_argument("--decode-output-dir", help="Directory for decoded outputs. Defaults to RAW file directory.")
    capture_parser.add_argument(
        "--decode-format",
        action="append",
        choices=("npy", "tiff"),
        dest="decode_formats",
        help="Decoded output format. Repeat for multiple formats. Defaults to npy and tiff.",
    )

    decode_parser = subparsers.add_parser("decode", help="Decode an existing RAW file to linear camera-RGB outputs.")
    decode_parser.add_argument("raw_file")
    decode_parser.add_argument("--output-dir", help="Directory for decoded outputs. Defaults to RAW file directory.")
    decode_parser.add_argument("--output-stem", help="Output basename without extension.")
    decode_parser.add_argument(
        "--format",
        action="append",
        choices=("npy", "tiff"),
        dest="formats",
        help="Decoded output format. Repeat for multiple formats. Defaults to npy and tiff.",
    )
    decode_parser.add_argument("--demosaic", default="AHD", help="rawpy demosaic algorithm name. Defaults to AHD.")

    auto_parser = subparsers.add_parser(
        "auto-expose",
        help="Find a bounded shutter speed whose decoded linear image max is below the target.",
    )
    auto_parser.add_argument("--model", default=DEFAULT_CAMERA_MODEL)
    auto_parser.add_argument("--port", help="gphoto2 USB port. Defaults to auto-detect.")
    auto_parser.add_argument("--gphoto2", default="gphoto2")
    auto_parser.add_argument("--timeout", type=float, default=60.0)
    auto_parser.add_argument("--output-dir", default="captures/camera")
    auto_parser.add_argument("--filename-template", default=DEFAULT_FILENAME_TEMPLATE)
    auto_parser.add_argument("--target-max", type=int, default=DEFAULT_AUTO_EXPOSURE_MAX)
    auto_parser.add_argument("--iso", default=DEFAULT_ISO)
    auto_parser.add_argument("--aperture", default=DEFAULT_APERTURE)
    auto_parser.add_argument("--image-format", default=DEFAULT_IMAGE_FORMAT)
    auto_parser.add_argument("--min-shutter-speed", default=DEFAULT_MIN_SHUTTER_SPEED)
    auto_parser.add_argument("--max-shutter-speed", default=DEFAULT_MAX_SHUTTER_SPEED)
    auto_parser.add_argument(
        "--shutter-speed",
        action="append",
        dest="shutter_speeds",
        help="Explicit shutter candidate. Repeat to provide a custom search list.",
    )
    auto_parser.add_argument("--decode-output-dir", help="Directory for final decoded outputs. Defaults to RAW file directory.")
    auto_parser.add_argument(
        "--decode-format",
        action="append",
        choices=("npy", "tiff"),
        dest="decode_formats",
        help="Final decoded output format. Repeat for multiple formats. Defaults to npy and tiff.",
    )
    auto_parser.add_argument("--keep", action="store_true", help="Keep the final captured file on the camera.")

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
            if args.decode_linear:
                decoded = decode_raw_image(
                    result.saved_file,
                    output_dir=args.decode_output_dir,
                    formats=args.decode_formats or DEFAULT_DECODE_FORMATS,
                )
                result = CaptureResult(
                    connection=result.connection,
                    settings=result.settings,
                    saved_file=result.saved_file,
                    stdout=result.stdout,
                    decoded=decoded,
                )
            _print_json(result.to_jsonable())
        elif args.command == "decode":
            result = decode_raw_image(
                args.raw_file,
                output_dir=args.output_dir,
                output_stem=args.output_stem,
                formats=args.formats or DEFAULT_DECODE_FORMATS,
                demosaic_algorithm=args.demosaic,
            )
            _print_json(result.to_jsonable())
        elif args.command == "auto-expose":
            result = auto_expose_capture(
                output_dir=args.output_dir,
                filename_template=args.filename_template,
                target_max=args.target_max,
                iso=args.iso,
                aperture=args.aperture,
                image_format=args.image_format,
                min_shutter_speed=args.min_shutter_speed,
                max_shutter_speed=args.max_shutter_speed,
                shutter_speeds=args.shutter_speeds,
                decode_output_dir=args.decode_output_dir,
                decode_formats=args.decode_formats or DEFAULT_DECODE_FORMATS,
                port=args.port,
                expected_model=args.model,
                allow_bulb=False,
                keep_on_camera=args.keep,
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
