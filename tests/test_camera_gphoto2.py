import tempfile
import unittest
from pathlib import Path

import numpy as np

import camera_gphoto2


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

    def test_auto_expose_deletes_trials_and_saves_only_final_capture(self):
        current_shutter = {"value": None}
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
            self.assertEqual(result.final_capture.settings.shutter_speed, "1/50")
            self.assertEqual(result.final_capture.decoded.stats["image"]["max"], 48000)
            self.assertLessEqual(result.final_capture.decoded.stats["image"]["max"], 49152)

        self.assertGreaterEqual(len(trial_raw_paths), 1)
        self.assertGreaterEqual(len(trial_decode_paths), 1)
        self.assertFalse(any(path.exists() for path in trial_raw_paths))
        self.assertFalse(any(path.exists() for path in trial_decode_paths))
        self.assertFalse(any(path.exists() for path in trial_workspace_roots))


if __name__ == "__main__":
    unittest.main()
