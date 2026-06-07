import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import numpy as np

from camera_based_rgbww_optimizer.control import camera_gphoto2


class CameraGPhoto2ParseTests(unittest.TestCase):
    def test_parse_auto_detect_finds_r6_mark_iii(self):
        output = """Model                          Port
----------------------------------------------------------
Canon EOS R6 Mark III          usb:001,010
"""

        self.assertEqual(
            camera_gphoto2.parse_auto_detect(output),
            [camera_gphoto2.CameraConnection("Canon EOS R6 Mark III", "usb:001,010")],
        )

    def test_read_config_current_and_choices_parses_single_gphoto2_output(self):
        def runner(args):
            self.assertEqual(list(args), ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED])
            return camera_gphoto2.CommandOutput(
                "Label: Shutter Speed\n"
                "Current: 1/125\n"
                "Choice: 0 bulb\n"
                "Choice: 1 1/125\n"
                "Choice: 2 1/60\n"
                "END\n"
            )

        current, choices = camera_gphoto2.read_config_current_and_choices(
            "usb:001,010",
            camera_gphoto2.CONFIG_SHUTTER_SPEED,
            runner=runner,
        )

        self.assertEqual(current, "1/125")
        self.assertEqual(choices, ["bulb", "1/125", "1/60"])

    def test_numbered_filename_template_keeps_first_name_and_numbers_retries(self):
        self.assertEqual(camera_gphoto2._numbered_filename_template("final.cr3", 0), "final.cr3")
        self.assertEqual(camera_gphoto2._numbered_filename_template("final.cr3", 1), "final-final001.cr3")
        self.assertEqual(
            camera_gphoto2._numbered_filename_template("%Y%m%d-%H%M%S.%C", 2),
            "%Y%m%d-%H%M%S-final002.%C",
        )


class CameraGPhoto2CaptureTests(unittest.TestCase):
    def test_capture_sets_parameters_and_returns_saved_file(self):
        calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            saved_file = Path(tmpdir) / "shot.cr3"

            def runner(args):
                calls.append(list(args))
                if args == ["--auto-detect"]:
                    return camera_gphoto2.CommandOutput(
                        "Model                          Port\n"
                        "----------------------------------------------------------\n"
                        "Canon EOS R6 Mark III          usb:001,010\n"
                    )
                if "--set-config" in args:
                    return camera_gphoto2.CommandOutput("")
                if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                    return camera_gphoto2.CommandOutput("Label: Shutter Speed\nCurrent: 1/30\nEND\n")
                if "--capture-image-and-download" in args:
                    saved_file.write_bytes(b"fake cr3")
                    return camera_gphoto2.CommandOutput(
                        f"New file is in location /capt0001.cr3 on the camera\nSaving file as {saved_file}\n"
                    )
                raise AssertionError(args)

            result = camera_gphoto2.capture_image(
                output_dir=tmpdir,
                filename_template="shot.cr3",
                runner=runner,
            )

        self.assertEqual(result.connection.port, "usb:001,010")
        self.assertEqual(result.saved_file, saved_file)
        self.assertEqual(
            calls[1],
            [
                "--port",
                "usb:001,010",
                "--set-config",
                f"{camera_gphoto2.CONFIG_ISO}=100",
                "--set-config",
                f"{camera_gphoto2.CONFIG_APERTURE}=4",
                "--set-config",
                f"{camera_gphoto2.CONFIG_SHUTTER_SPEED}=1/30",
                "--set-config",
                f"{camera_gphoto2.CONFIG_IMAGE_FORMAT}=RAW",
            ],
        )

    def test_capture_refuses_bulb_readback(self):
        calls = []

        def runner(args):
            calls.append(list(args))
            if args == ["--auto-detect"]:
                return camera_gphoto2.CommandOutput(
                    "Model                          Port\n"
                    "----------------------------------------------------------\n"
                    "Canon EOS R6 Mark III          usb:001,010\n"
                )
            if "--set-config" in args:
                return camera_gphoto2.CommandOutput("")
            if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                return camera_gphoto2.CommandOutput("Label: Shutter Speed\nCurrent: bulb\nEND\n")
            if "--capture-image-and-download" in args:
                raise AssertionError("capture should not run when shutter readback is bulb")
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(camera_gphoto2.GPhoto2Error, "bulb"):
                camera_gphoto2.capture_image(output_dir=tmpdir, runner=runner)

        self.assertFalse(any("--capture-image-and-download" in call for call in calls))

    def test_auto_detect_reports_available_cameras_when_model_is_missing(self):
        def runner(args):
            self.assertEqual(list(args), ["--auto-detect"])
            return camera_gphoto2.CommandOutput(
                "Model                          Port\n"
                "----------------------------------------------------------\n"
                "Some Other Camera              usb:000,001\n"
            )

        with self.assertRaisesRegex(camera_gphoto2.GPhoto2Error, "Some Other Camera"):
            camera_gphoto2.auto_detect_camera(runner=runner)


class CameraGPhoto2DecodeTests(unittest.TestCase):
    def test_decode_raw_image_writes_linear_npy_and_metadata(self):
        opened_raws = []

        class FakeSizes:
            raw_height = 4
            raw_width = 4
            height = 2
            width = 2
            top_margin = 1
            left_margin = 1
            iheight = 2
            iwidth = 2

        class FakeRaw:
            sizes = FakeSizes()
            num_colors = 3
            color_desc = b"RGBG"
            raw_pattern = np.array([[0, 1], [3, 2]])
            raw_image_visible = np.array([[10, 20], [30, 40]], dtype=np.uint16)
            black_level_per_channel = [0, 32, 96, 64]
            white_level = 16383
            camera_white_level_per_channel = [144, 144, 144, 144]

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return None

            def postprocess(self, **kwargs):
                self.postprocess_kwargs = kwargs
                return np.array(
                    [
                        [[100, 200, 300], [110, 210, 310]],
                        [[120, 220, 320], [130, 230, 330]],
                    ],
                    dtype=np.uint16,
                )

        class FakeDemosaicAlgorithm:
            AHD = object()

        class FakeColorSpace:
            raw = object()

        class FakeRawpy:
            __version__ = "test-rawpy"
            libraw_version = (0, 0, 0)
            DemosaicAlgorithm = FakeDemosaicAlgorithm
            ColorSpace = FakeColorSpace

            def imread(self, path):
                self.path = path
                raw = FakeRaw()
                opened_raws.append(raw)
                return raw

        with tempfile.TemporaryDirectory() as tmpdir:
            raw_file = Path(tmpdir) / "input.cr3"
            raw_file.write_bytes(b"fake raw")

            result = camera_gphoto2.decode_raw_image(
                raw_file,
                output_dir=tmpdir,
                output_stem="decoded",
                formats=("npy",),
                rawpy_module=FakeRawpy(),
                numpy_module=np,
            )

            decoded = np.load(Path(tmpdir) / "decoded.npy")

        self.assertEqual(decoded.shape, (2, 2, 3))
        self.assertEqual(decoded.dtype, np.uint16)
        self.assertEqual(result.image_shape, (2, 2, 3))
        self.assertEqual(result.image_dtype, "uint16")
        self.assertIn(Path(tmpdir) / "decoded.json", result.output_files)
        self.assertEqual(result.stats["black_level_per_channel"], [0, 32, 96, 64])
        self.assertEqual(result.stats["white_level"], 16383)
        self.assertEqual(result.to_jsonable()["image_max"], 330)
        self.assertEqual(result.to_jsonable()["raw_visible_max"], 40)
        self.assertEqual(result.to_jsonable()["image_channel_max"], [130, 230, 330])
        self.assertGreater(result.to_jsonable()["exposure_metric"], 0)

        kwargs = opened_raws[0].postprocess_kwargs
        self.assertIs(kwargs["demosaic_algorithm"], FakeDemosaicAlgorithm.AHD)
        self.assertIs(kwargs["output_color"], FakeColorSpace.raw)
        self.assertFalse(kwargs["use_camera_wb"])
        self.assertFalse(kwargs["use_auto_wb"])
        self.assertEqual(kwargs["user_wb"], [1.0, 1.0, 1.0, 1.0])
        self.assertTrue(kwargs["no_auto_bright"])
        self.assertEqual(kwargs["bright"], 1.0)
        self.assertEqual(kwargs["gamma"], (1.0, 1.0))
        self.assertEqual(kwargs["output_bps"], 16)

    def test_decode_raw_image_writes_location_metering_stats(self):
        class FakeSizes:
            raw_height = 4
            raw_width = 4
            height = 2
            width = 3
            top_margin = 1
            left_margin = 1
            iheight = 2
            iwidth = 3

        class FakeRaw:
            sizes = FakeSizes()
            num_colors = 3
            color_desc = b"RGBG"
            raw_pattern = np.array([[0, 1], [3, 2]])
            raw_image_visible = np.array([[10, 20], [30, 40]], dtype=np.uint16)
            black_level_per_channel = [0, 32, 96, 64]
            white_level = 16383
            camera_white_level_per_channel = [144, 144, 144, 144]

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return None

            def postprocess(self, **_kwargs):
                return np.array(
                    [
                        [[100, 200, 300], [110, 210, 310], [50000, 50000, 50000]],
                        [[120, 220, 320], [130, 230, 330], [65535, 65535, 65535]],
                    ],
                    dtype=np.uint16,
                )

        class FakeDemosaicAlgorithm:
            AHD = object()

        class FakeColorSpace:
            raw = object()

        class FakeRawpy:
            __version__ = "test-rawpy"
            libraw_version = (0, 0, 0)
            DemosaicAlgorithm = FakeDemosaicAlgorithm
            ColorSpace = FakeColorSpace

            def imread(self, _path):
                return FakeRaw()

        with tempfile.TemporaryDirectory() as tmpdir:
            raw_file = Path(tmpdir) / "input.cr3"
            raw_file.write_bytes(b"fake raw")

            result = camera_gphoto2.decode_raw_image(
                raw_file,
                output_dir=tmpdir,
                output_stem="decoded",
                formats=("npy",),
                rawpy_module=FakeRawpy(),
                numpy_module=np,
                metering_regions=[
                    {
                        "type": "polygon",
                        "name": "block_01",
                        "index": 1,
                        "points": [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)],
                    }
                ],
            )

        self.assertEqual(result.stats["image"]["max"], 65535)
        self.assertEqual(result.stats["metering"]["mode"], camera_gphoto2.METERING_MODE_LOCATION)
        self.assertEqual(result.stats["metering"]["region_count"], 1)
        self.assertEqual(result.stats["metering"]["image"]["max"], 330)
        self.assertEqual(result.to_jsonable()["image_max"], 65535)
        self.assertEqual(result.to_jsonable()["metering"]["image_max"], 330)

    def test_decode_raw_image_rejects_unknown_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_file = Path(tmpdir) / "input.cr3"
            raw_file.write_bytes(b"fake raw")

            with self.assertRaisesRegex(camera_gphoto2.GPhoto2Error, "unsupported decode format"):
                camera_gphoto2.decode_raw_image(raw_file, formats=("jpeg",))


class CameraGPhoto2AutoExposureTests(unittest.TestCase):
    def test_select_shutter_candidates_sorts_and_filters_bounded_values(self):
        candidates = camera_gphoto2._select_shutter_candidates(
            "usb:001,010",
            min_shutter_speed="1/100",
            max_shutter_speed="1",
            shutter_speeds=("bulb", "1", "1/10", "1/100", "1/1000"),
            runner=None,
            executable="gphoto2",
            timeout=30,
        )

        self.assertEqual(candidates, ("1/100", "1/10", "1"))

    def test_load_metering_regions_requires_24_blocks(self):
        config = {"blocks": []}
        for index in range(1, 25):
            config["blocks"].append(
                {
                    "index": index,
                    "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}],
                }
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "locations.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            regions = camera_gphoto2.load_metering_regions(path)

        self.assertEqual(len(regions), 24)
        self.assertEqual(regions[0]["name"], "block_01")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "locations.json"
            path.write_text(json.dumps({"blocks": config["blocks"][:23]}), encoding="utf-8")

            with self.assertRaisesRegex(camera_gphoto2.GPhoto2Error, "exactly 24"):
                camera_gphoto2.load_metering_regions(path)

    def test_auto_expose_uses_metering_stats_when_available(self):
        current_shutter = {"value": "1/100"}
        trial_shutters = []
        metering_max_by_shutter = {
            "1/100": 10000,
            "1/50": 48000,
            "1/25": 52000,
        }

        def runner(args):
            if args == ["--auto-detect"]:
                return camera_gphoto2.CommandOutput(
                    "Model                          Port\n"
                    "----------------------------------------------------------\n"
                    "Canon EOS R6 Mark III          usb:001,010\n"
                )
            if "--set-config" in args:
                for value in args:
                    if value.startswith(f"{camera_gphoto2.CONFIG_SHUTTER_SPEED}="):
                        current_shutter["value"] = value.split("=", 1)[1]
                return camera_gphoto2.CommandOutput("")
            if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                return camera_gphoto2.CommandOutput(f"Label: Shutter Speed\nCurrent: {current_shutter['value']}\nEND\n")
            if "--capture-image-and-download" in args:
                filename = Path(args[args.index("--filename") + 1])
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text(current_shutter["value"], encoding="utf-8")
                if filename.name.startswith("trial-"):
                    trial_shutters.append(current_shutter["value"])
                return camera_gphoto2.CommandOutput(f"Saving file as {filename}\n")
            raise AssertionError(args)

        def decoder(raw_file, *, output_dir=None, formats=("npy",), metering_regions=None):
            raw_path = Path(raw_file)
            shutter = raw_path.read_text(encoding="utf-8")
            target_dir = Path(output_dir) if output_dir is not None else raw_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            output_file = target_dir / f"{raw_path.stem}.json"
            output_file.write_text("{}", encoding="utf-8")
            metering_max = metering_max_by_shutter[shutter]
            self.assertIsNotNone(metering_regions)
            return camera_gphoto2.DecodeResult(
                source_file=raw_path,
                output_files=(output_file,),
                metadata_file=output_file,
                image_shape=(2, 2, 3),
                image_dtype="uint16",
                stats={
                    "image": {"max": 65535, "channel_max": [65535, 65535, 65535]},
                    "raw_visible": {"max": 16000},
                    "exposure": {"channel_contrast_max": 60000.0},
                    "metering": {
                        "mode": camera_gphoto2.METERING_MODE_LOCATION,
                        "image": {"max": metering_max, "channel_max": [100, metering_max, 90]},
                        "exposure": {"channel_contrast_max": 30000.0},
                    },
                },
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = camera_gphoto2.auto_expose_capture(
                output_dir=Path(tmpdir) / "final",
                filename_template="final.cr3",
                target_max=49152,
                shutter_speeds=("1/100", "1/50", "1/25"),
                decode_output_dir=Path(tmpdir) / "decoded",
                decode_formats=("npy",),
                metering_regions=[{"type": "full", "name": "test_meter"}],
                runner=runner,
                decoder=decoder,
            )

        self.assertEqual(trial_shutters, ["1/100", "1/25", "1/50"])
        self.assertEqual(result.final_capture.settings.shutter_speed, "1/50")
        self.assertEqual(result.final_capture.decoded.stats["image"]["max"], 65535)
        self.assertEqual(result.final_capture.decoded.stats["metering"]["image"]["max"], 48000)
        self.assertEqual(result.trials[-1].decoded_max, 48000)

    def test_auto_expose_deletes_trials_and_saves_only_final_capture(self):
        current_shutter = {"value": "1/100"}
        trial_shutters = []
        trial_raw_paths = []
        trial_decode_paths = []
        trial_workspace_roots = set()
        max_by_shutter = {
            "1/100": 10000,
            "1/50": 48000,
            "1/25": 52000,
        }

        def runner(args):
            if args == ["--auto-detect"]:
                return camera_gphoto2.CommandOutput(
                    "Model                          Port\n"
                    "----------------------------------------------------------\n"
                    "Canon EOS R6 Mark III          usb:001,010\n"
                )
            if "--set-config" in args:
                for value in args:
                    if value.startswith(f"{camera_gphoto2.CONFIG_SHUTTER_SPEED}="):
                        current_shutter["value"] = value.split("=", 1)[1]
                return camera_gphoto2.CommandOutput("")
            if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                return camera_gphoto2.CommandOutput(f"Label: Shutter Speed\nCurrent: {current_shutter['value']}\nEND\n")
            if "--capture-image-and-download" in args:
                filename = Path(args[args.index("--filename") + 1])
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text(str(current_shutter["value"]), encoding="utf-8")
                if filename.name.startswith("trial-"):
                    trial_shutters.append(current_shutter["value"])
                    trial_raw_paths.append(filename)
                    trial_workspace_roots.add(filename.parent.parent)
                return camera_gphoto2.CommandOutput(
                    f"New file is in location /capt0001.cr3 on the camera\nSaving file as {filename}\n"
                )
            raise AssertionError(args)

        def decoder(raw_file, *, output_dir=None, formats=("npy",)):
            raw_path = Path(raw_file)
            decoded_max = max_by_shutter[raw_path.read_text(encoding="utf-8")]
            target_dir = Path(output_dir) if output_dir is not None else raw_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            output_file = target_dir / f"{raw_path.stem}.json"
            output_file.write_text("{}", encoding="utf-8")
            if raw_path.name.startswith("trial-"):
                trial_decode_paths.append(output_file)
                trial_workspace_roots.add(output_file.parent.parent)
            return camera_gphoto2.DecodeResult(
                source_file=raw_path,
                output_files=(output_file,),
                metadata_file=output_file,
                image_shape=(2, 2, 3),
                image_dtype="uint16",
                stats={
                    "image": {"max": decoded_max},
                    "raw_visible": {"max": decoded_max // 4},
                },
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "final"
            decode_dir = Path(tmpdir) / "decoded"
            result = camera_gphoto2.auto_expose_capture(
                output_dir=output_dir,
                filename_template="final.cr3",
                target_max=49152,
                shutter_speeds=("1/100", "1/50", "1/25"),
                decode_output_dir=decode_dir,
                decode_formats=("npy",),
                runner=runner,
                decoder=decoder,
            )

            final_file = output_dir / "final.cr3"
            final_decode_file = decode_dir / "final.json"
            self.assertTrue(final_file.exists())
            self.assertTrue(final_decode_file.exists())
            self.assertEqual(trial_shutters, ["1/100", "1/25", "1/50"])
            self.assertEqual(result.final_capture.settings.shutter_speed, "1/50")
            self.assertEqual(result.final_capture.decoded.stats["image"]["max"], 48000)
            self.assertLessEqual(result.final_capture.decoded.stats["image"]["max"], 49152)

        self.assertGreaterEqual(len(trial_raw_paths), 1)
        self.assertGreaterEqual(len(trial_decode_paths), 1)
        self.assertFalse(any(path.exists() for path in trial_raw_paths))
        self.assertFalse(any(path.exists() for path in trial_decode_paths))
        self.assertFalse(any(path.exists() for path in trial_workspace_roots))

    def test_auto_expose_limits_trial_count_and_saves_best_accepted_capture(self):
        current_shutter = {"value": "1/100"}
        trial_shutters = []
        max_by_shutter = {
            "1/100": 29000,
            "1/60": 32000,
            "1/40": 30000,
            "1/25": 40000,
        }

        def runner(args):
            if args == ["--auto-detect"]:
                return camera_gphoto2.CommandOutput(
                    "Model                          Port\n"
                    "----------------------------------------------------------\n"
                    "Canon EOS R6 Mark III          usb:001,010\n"
                )
            if "--set-config" in args:
                for value in args:
                    if value.startswith(f"{camera_gphoto2.CONFIG_SHUTTER_SPEED}="):
                        current_shutter["value"] = value.split("=", 1)[1]
                return camera_gphoto2.CommandOutput("")
            if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                return camera_gphoto2.CommandOutput(f"Label: Shutter Speed\nCurrent: {current_shutter['value']}\nEND\n")
            if "--capture-image-and-download" in args:
                filename = Path(args[args.index("--filename") + 1])
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text(current_shutter["value"], encoding="utf-8")
                if filename.name.startswith("trial-"):
                    trial_shutters.append(current_shutter["value"])
                return camera_gphoto2.CommandOutput(
                    f"New file is in location /capt0001.cr3 on the camera\nSaving file as {filename}\n"
                )
            raise AssertionError(args)

        def decoder(raw_file, *, output_dir=None, formats=("npy",)):
            raw_path = Path(raw_file)
            decoded_max = max_by_shutter[raw_path.read_text(encoding="utf-8")]
            target_dir = Path(output_dir) if output_dir is not None else raw_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            output_file = target_dir / f"{raw_path.stem}.json"
            output_file.write_text("{}", encoding="utf-8")
            return camera_gphoto2.DecodeResult(
                source_file=raw_path,
                output_files=(output_file,),
                metadata_file=output_file,
                image_shape=(2, 2, 3),
                image_dtype="uint16",
                stats={"image": {"max": decoded_max}, "raw_visible": {"max": decoded_max // 4}},
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = camera_gphoto2.auto_expose_capture(
                output_dir=Path(tmpdir) / "final",
                filename_template="final.cr3",
                target_max=49152,
                max_trials=2,
                shutter_speeds=("1/100", "1/60", "1/40", "1/25"),
                decode_output_dir=Path(tmpdir) / "decoded",
                decode_formats=("npy",),
                runner=runner,
                decoder=decoder,
            )

        self.assertEqual(trial_shutters, ["1/100", "1/60"])
        self.assertEqual(len(result.trials), 2)
        self.assertEqual(result.final_capture.settings.shutter_speed, "1/25")
        self.assertEqual(result.final_capture.decoded.stats["image"]["max"], 40000)

    def test_auto_expose_uses_dark_metric_to_boost_from_underexposure(self):
        current_shutter = {"value": "1/8000"}
        trial_shutters = []
        max_by_shutter = {
            "1/8000": 2300,
            "1/80": 2400,
            "1.6": 30000,
            "2.5": 45000,
        }
        exposure_by_shutter = {
            "1/8000": 170.0,
            "1/80": 330.0,
            "1.6": 26000.0,
            "2.5": 40000.0,
        }

        def runner(args):
            if args == ["--auto-detect"]:
                return camera_gphoto2.CommandOutput(
                    "Model                          Port\n"
                    "----------------------------------------------------------\n"
                    "Canon EOS R6 Mark III          usb:001,010\n"
                )
            if "--set-config" in args:
                for value in args:
                    if value.startswith(f"{camera_gphoto2.CONFIG_SHUTTER_SPEED}="):
                        current_shutter["value"] = value.split("=", 1)[1]
                return camera_gphoto2.CommandOutput("")
            if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                return camera_gphoto2.CommandOutput(f"Label: Shutter Speed\nCurrent: {current_shutter['value']}\nEND\n")
            if "--capture-image-and-download" in args:
                filename = Path(args[args.index("--filename") + 1])
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text(current_shutter["value"], encoding="utf-8")
                if filename.name.startswith("trial-"):
                    trial_shutters.append(current_shutter["value"])
                return camera_gphoto2.CommandOutput(f"Saving file as {filename}\n")
            raise AssertionError(args)

        def decoder(raw_file, *, output_dir=None, formats=("npy",)):
            raw_path = Path(raw_file)
            shutter = raw_path.read_text(encoding="utf-8")
            target_dir = Path(output_dir) if output_dir is not None else raw_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            output_file = target_dir / f"{raw_path.stem}.json"
            output_file.write_text("{}", encoding="utf-8")
            return camera_gphoto2.DecodeResult(
                source_file=raw_path,
                output_files=(output_file,),
                metadata_file=output_file,
                image_shape=(2, 2, 3),
                image_dtype="uint16",
                stats={
                    "image": {"max": max_by_shutter[shutter], "channel_max": [100, max_by_shutter[shutter], 90]},
                    "raw_visible": {"max": max_by_shutter[shutter] // 4},
                    "exposure": {"channel_contrast_max": exposure_by_shutter[shutter]},
                },
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = camera_gphoto2.auto_expose_capture(
                output_dir=Path(tmpdir) / "final",
                filename_template="final.cr3",
                target_max=49152,
                max_trials=5,
                shutter_speeds=("1/8000", "1/80", "1.6", "2.5"),
                decode_output_dir=Path(tmpdir) / "decoded",
                decode_formats=("npy",),
                runner=runner,
                decoder=decoder,
            )

        self.assertEqual(trial_shutters, ["1/8000", "1/80", "1.6", "2.5"])
        self.assertEqual(result.final_capture.settings.shutter_speed, "2.5")
        self.assertEqual(result.trials[0].decision, "dark_boost")
        self.assertLessEqual(result.final_capture.decoded.stats["image"]["max"], 49152)

    def test_auto_expose_can_start_from_explicit_initial_shutter(self):
        current_shutter = {"value": "30"}
        trial_shutters = []
        max_by_shutter = {
            "1/8000": 1000,
            "1/1000": 8000,
            "1/125": 48000,
            "30": 65000,
        }

        def runner(args):
            if args == ["--auto-detect"]:
                return camera_gphoto2.CommandOutput(
                    "Model                          Port\n"
                    "----------------------------------------------------------\n"
                    "Canon EOS R6 Mark III          usb:001,010\n"
                )
            if "--set-config" in args:
                for value in args:
                    if value.startswith(f"{camera_gphoto2.CONFIG_SHUTTER_SPEED}="):
                        current_shutter["value"] = value.split("=", 1)[1]
                return camera_gphoto2.CommandOutput("")
            if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                return camera_gphoto2.CommandOutput(f"Label: Shutter Speed\nCurrent: {current_shutter['value']}\nEND\n")
            if "--capture-image-and-download" in args:
                filename = Path(args[args.index("--filename") + 1])
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text(current_shutter["value"], encoding="utf-8")
                if filename.name.startswith("trial-"):
                    trial_shutters.append(current_shutter["value"])
                return camera_gphoto2.CommandOutput(f"Saving file as {filename}\n")
            raise AssertionError(args)

        def decoder(raw_file, *, output_dir=None, formats=("npy",)):
            raw_path = Path(raw_file)
            shutter = raw_path.read_text(encoding="utf-8")
            target_dir = Path(output_dir) if output_dir is not None else raw_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            output_file = target_dir / f"{raw_path.stem}.json"
            output_file.write_text("{}", encoding="utf-8")
            return camera_gphoto2.DecodeResult(
                source_file=raw_path,
                output_files=(output_file,),
                metadata_file=output_file,
                image_shape=(2, 2, 3),
                image_dtype="uint16",
                stats={"image": {"max": max_by_shutter[shutter]}, "raw_visible": {"max": max_by_shutter[shutter]}},
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            camera_gphoto2.auto_expose_capture(
                output_dir=Path(tmpdir) / "final",
                filename_template="final.cr3",
                target_max=49152,
                max_trials=2,
                shutter_speeds=("1/8000", "1/1000", "1/125", "30"),
                initial_shutter_speed="1/8000",
                decode_output_dir=Path(tmpdir) / "decoded",
                decode_formats=("npy",),
                runner=runner,
                decoder=decoder,
            )

        self.assertEqual(trial_shutters[0], "1/8000")

    def test_auto_expose_uses_saturated_backoff_from_overexposure(self):
        current_shutter = {"value": "1"}
        trial_shutters = []
        max_by_shutter = {
            "1": 65535,
            "1/160": 65535,
            "1/8000": 11000,
            "1/2000": 35000,
            "1/1600": 43000,
        }

        def runner(args):
            if args == ["--auto-detect"]:
                return camera_gphoto2.CommandOutput(
                    "Model                          Port\n"
                    "----------------------------------------------------------\n"
                    "Canon EOS R6 Mark III          usb:001,010\n"
                )
            if "--set-config" in args:
                for value in args:
                    if value.startswith(f"{camera_gphoto2.CONFIG_SHUTTER_SPEED}="):
                        current_shutter["value"] = value.split("=", 1)[1]
                return camera_gphoto2.CommandOutput("")
            if args == ["--port", "usb:001,010", "--get-config", camera_gphoto2.CONFIG_SHUTTER_SPEED]:
                return camera_gphoto2.CommandOutput(f"Label: Shutter Speed\nCurrent: {current_shutter['value']}\nEND\n")
            if "--capture-image-and-download" in args:
                filename = Path(args[args.index("--filename") + 1])
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_text(current_shutter["value"], encoding="utf-8")
                if filename.name.startswith("trial-"):
                    trial_shutters.append(current_shutter["value"])
                return camera_gphoto2.CommandOutput(f"Saving file as {filename}\n")
            raise AssertionError(args)

        def decoder(raw_file, *, output_dir=None, formats=("npy",)):
            raw_path = Path(raw_file)
            shutter = raw_path.read_text(encoding="utf-8")
            decoded_max = max_by_shutter[shutter]
            target_dir = Path(output_dir) if output_dir is not None else raw_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            output_file = target_dir / f"{raw_path.stem}.json"
            output_file.write_text("{}", encoding="utf-8")
            return camera_gphoto2.DecodeResult(
                source_file=raw_path,
                output_files=(output_file,),
                metadata_file=output_file,
                image_shape=(2, 2, 3),
                image_dtype="uint16",
                stats={
                    "image": {"max": decoded_max, "channel_max": [decoded_max, decoded_max, decoded_max]},
                    "raw_visible": {"max": decoded_max // 4},
                    "exposure": {"channel_contrast_max": 50000.0},
                },
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = camera_gphoto2.auto_expose_capture(
                output_dir=Path(tmpdir) / "final",
                filename_template="final.cr3",
                target_max=49152,
                max_trials=6,
                shutter_speeds=("1/8000", "1/2000", "1/1600", "1/160", "1"),
                decode_output_dir=Path(tmpdir) / "decoded",
                decode_formats=("npy",),
                runner=runner,
                decoder=decoder,
            )

        self.assertEqual(trial_shutters, ["1", "1/160", "1/8000", "1/1600"])
        self.assertEqual(result.trials[0].decision, "saturated_backoff")
        self.assertEqual(result.final_capture.settings.shutter_speed, "1/1600")
        self.assertLessEqual(result.final_capture.decoded.stats["image"]["max"], 49152)

    def test_bracket_midpoint_uses_geometric_exposure_midpoint(self):
        candidates = tuple(Fraction(value) for value in ("1/8000", "1/2000", "1/1600", "1/800", "1/160"))

        self.assertEqual(
            camera_gphoto2._bracket_midpoint_index(candidates, low_index=0, high_index=4),
            2,
        )

    def test_auto_expose_uses_brightest_channel_metric_for_saturated_hues(self):
        candidates = tuple(Fraction(value) for value in ("1/8000", "1/2000", "1/500", "1/125", "1/80"))

        next_index = camera_gphoto2._next_shutter_index_from_measurement(
            candidates,
            current_index=0,
            decoded_max=4400,
            target_max=49152,
            exposure_metric=2500.0,
            target_metric=43254.0,
            accept_min=41779,
        )

        self.assertEqual(next_index, 1)

    def test_next_shutter_index_uses_linear_exposure_ratio(self):
        candidates = tuple(Fraction(value) for value in ("1/100", "1/50", "1/25", "1/10"))

        self.assertEqual(
            camera_gphoto2._next_shutter_index_from_measurement(
                candidates,
                current_index=0,
                decoded_max=10000,
                target_max=49152,
            ),
            2,
        )
        self.assertEqual(
            camera_gphoto2._next_shutter_index_from_measurement(
                candidates,
                current_index=2,
                decoded_max=52000,
                target_max=49152,
            ),
            1,
        )
        self.assertEqual(
            camera_gphoto2._next_shutter_index_from_measurement(
                candidates,
                current_index=1,
                decoded_max=48000,
                target_max=49152,
            ),
            1,
        )
        self.assertEqual(
            camera_gphoto2._next_shutter_index_from_measurement(
                candidates,
                current_index=0,
                decoded_max=0,
                target_max=49152,
            ),
            3,
        )


if __name__ == "__main__":
    unittest.main()
