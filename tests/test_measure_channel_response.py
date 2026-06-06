import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import camera_gphoto2
import measure_channel_response


def project_temp_dir():
    measure_channel_response.PROJECT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=measure_channel_response.PROJECT_TMP_DIR)


class MeasureChannelResponseParseTests(unittest.TestCase):
    def test_parse_code_values_accepts_commas_and_spaces(self):
        self.assertEqual(measure_channel_response.parse_code_values("1, 2 4"), (1, 2, 4))

    def test_parse_channels_rejects_unknown_channel(self):
        with self.assertRaisesRegex(Exception, "unsupported"):
            measure_channel_response.parse_channels("cw uv")

    def test_build_measurement_plan_uses_channel_order_and_duty(self):
        plan = measure_channel_response.build_measurement_plan(
            channels=("cw", "r"),
            codes=(0, 2048),
            max_code=4096,
        )

        self.assertEqual(plan[0]["command"], {"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0})
        self.assertEqual(plan[1]["command"], {"cw": 2048, "ww": 0, "r": 0, "g": 0, "b": 0})
        self.assertEqual(plan[2]["command"], {"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0})
        self.assertEqual(plan[3]["command"], {"cw": 0, "ww": 0, "r": 2048, "g": 0, "b": 0})
        self.assertAlmostEqual(plan[1]["duty"], 0.5)

    def test_validate_output_range_requires_explicit_high_output_for_real_run(self):
        plan = measure_channel_response.build_measurement_plan(channels=("cw",), codes=(2048,), max_code=4095)

        with self.assertRaisesRegex(ValueError, "allow-high-output"):
            measure_channel_response.validate_output_range(
                plan=plan,
                max_code=4095,
                safe_code_limit=1024,
                allow_high_output=False,
                dry_run=False,
            )

        measure_channel_response.validate_output_range(
            plan=plan,
            max_code=4095,
            safe_code_limit=1024,
            allow_high_output=False,
            dry_run=True,
        )

    def test_shutter_seconds_parses_fractional_speeds(self):
        self.assertAlmostEqual(measure_channel_response.shutter_seconds("1/125"), 0.008)
        self.assertAlmostEqual(measure_channel_response.shutter_seconds("2.5"), 2.5)

    def test_default_auto_exposure_metering_uses_saved_location_config(self):
        args = measure_channel_response.parse_args([])

        path = measure_channel_response.resolve_auto_exposure_metering_location_config(args)

        self.assertEqual(path, measure_channel_response.DEFAULT_AUTO_EXPOSURE_METERING_LOCATION_CONFIG)

    def test_measurement_location_config_is_default_metering_config(self):
        args = measure_channel_response.parse_args(["--location-config", "config/location/custom.json"])

        path = measure_channel_response.resolve_auto_exposure_metering_location_config(args)

        self.assertEqual(path, Path("config/location/custom.json"))

    def test_explicit_metering_location_config_takes_precedence(self):
        args = measure_channel_response.parse_args(
            [
                "--location-config",
                "config/location/measurement.json",
                "--auto-exposure-metering-location-config",
                "config/location/metering.json",
            ]
        )

        path = measure_channel_response.resolve_auto_exposure_metering_location_config(args)

        self.assertEqual(path, Path("config/location/metering.json"))

    def test_default_codes_start_at_full_scale(self):
        args = measure_channel_response.parse_args([])

        self.assertEqual(args.codes[0], 4095)
        self.assertEqual(args.codes[-1], 8)


class MeasureChannelResponseRegionTests(unittest.TestCase):
    def test_roi_measurement_normalizes_by_shutter_seconds_and_subtracts_ambient(self):
        image = np.array(
            [
                [[10, 20, 30], [20, 40, 60]],
                [[30, 60, 90], [40, 80, 120]],
            ],
            dtype=np.uint16,
        )
        ambient = {"mean": 10.0, "channel_mean": [2.0, 4.0, 6.0], "channel_median": [2.0, 4.0, 6.0]}

        measurements = measure_channel_response.measure_image_regions(
            image,
            [{"type": "roi", "name": "center", "x": 0, "y": 0, "width": 2, "height": 2}],
            shutter_seconds_value=0.5,
            ambient_regions={"center": ambient},
            ambient_shutter_seconds=1.0,
            numpy_module=np,
        )

        region = measurements[0]
        self.assertEqual(region["stats"]["pixel_count"], 4)
        self.assertEqual(region["normalized"]["channel_mean_per_second"], [50.0, 100.0, 150.0])
        self.assertEqual(region["ambient_subtracted"]["channel_mean_per_second"], [48.0, 96.0, 144.0])

    def test_location_config_regions_select_requested_blocks(self):
        config = {
            "blocks": [
                {"index": 1, "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]},
                {"index": 2, "points": [{"x": 2, "y": 0}, {"x": 3, "y": 0}, {"x": 3, "y": 1}, {"x": 2, "y": 1}]},
            ]
        }
        with project_temp_dir() as tmpdir:
            path = Path(tmpdir) / "locations.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            regions = measure_channel_response.load_location_regions(path, (2,))

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["name"], "block_02")
        self.assertEqual(regions[0]["points"][0], (2.0, 0.0))

    def test_ambient_stop_requires_longest_shutter_and_small_signal(self):
        regions = [
            {"ambient_subtracted": {"channel_mean_per_second": [0.0, 1.0, -2.0]}},
        ]

        self.assertTrue(
            measure_channel_response.should_stop_channel_at_ambient(
                shutter_speed="30",
                max_shutter_speed="30",
                region_measurements=regions,
                threshold_per_second=2.0,
            )
        )
        self.assertFalse(
            measure_channel_response.should_stop_channel_at_ambient(
                shutter_speed="1",
                max_shutter_speed="30",
                region_measurements=regions,
                threshold_per_second=2.0,
            )
        )
        self.assertFalse(
            measure_channel_response.should_stop_channel_at_ambient(
                shutter_speed="30",
                max_shutter_speed="30",
                region_measurements=[{"ambient_subtracted": {"channel_mean_per_second": [3.0]}}],
                threshold_per_second=2.0,
            )
        )


class MeasureChannelResponseRunTests(unittest.TestCase):
    def test_dry_run_writes_plan_without_hardware_calls(self):
        with project_temp_dir() as tmpdir:
            output = measure_channel_response.run_channel_response(
                output_root=Path(tmpdir),
                run_name="dry",
                channels=("cw",),
                codes=(1, 2),
                max_code=4095,
                safe_code_limit=1,
                allow_high_output=False,
                dry_run=True,
                include_ambient=True,
                settle_seconds=0.0,
                regions=[{"type": "full", "name": "full_image"}],
                target_max=49152,
                iso="100",
                aperture="4",
                image_format="RAW",
                min_shutter_speed="1/8000",
                max_shutter_speed="30",
                max_trials=5,
                max_captures=10,
                decode_formats=("npy",),
            )

            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(data["status"], "dry_run")
        self.assertEqual(len(data["plan"]), 2)
        self.assertEqual(data["plan"][1]["code"], 2)
        self.assertEqual(data["camera"]["auto_exposure_metering"]["mode"], camera_gphoto2.METERING_MODE_FULL)
        self.assertIsNone(data["camera"]["auto_exposure_metering"]["regions"])

    def test_run_writes_measurements_and_turns_light_off(self):
        light_calls = []

        def fake_light(cw, ww, r, g, b):
            payload = {"cw": int(cw), "ww": int(ww), "r": int(r), "g": int(g), "b": int(b)}
            light_calls.append(payload)
            return {
                "cold_white": payload["cw"],
                "warm_white": payload["ww"],
                "red": payload["r"],
                "green": payload["g"],
                "blue": payload["b"],
            }

        class FakeAutoExpose:
            def __init__(self):
                self.count = 0
                self.metering_regions = []
                self.initial_shutter_speeds = []

            def __call__(self, *, output_dir, filename_template, decode_output_dir, decode_formats, **kwargs):
                self.metering_regions.append(kwargs.get("metering_regions"))
                self.initial_shutter_speeds.append(kwargs.get("initial_shutter_speed"))
                output_path = Path(output_dir)
                decode_path = Path(decode_output_dir)
                output_path.mkdir(parents=True, exist_ok=True)
                decode_path.mkdir(parents=True, exist_ok=True)
                stem = filename_template.replace(".%C", "")
                raw_file = output_path / f"{stem}.cr3"
                raw_file.write_bytes(b"raw")
                npy_file = decode_path / f"{stem}.npy"
                value = 20 + self.count * 10
                np.save(npy_file, np.full((2, 2, 3), value, dtype=np.uint16))
                decoded = camera_gphoto2.DecodeResult(
                    source_file=raw_file,
                    output_files=(npy_file,),
                    metadata_file=decode_path / f"{stem}.json",
                    image_shape=(2, 2, 3),
                    image_dtype="uint16",
                    stats={"image": {"max": value, "channel_max": [value, value, value]}, "raw_visible": {"max": value}},
                )
                capture = camera_gphoto2.CaptureResult(
                    connection=camera_gphoto2.CameraConnection("test", "usb:test"),
                    settings=camera_gphoto2.CaptureSettings(shutter_speed="1/2"),
                    saved_file=raw_file,
                    stdout="",
                    decoded=decoded,
                )
                self.count += 1
                return camera_gphoto2.AutoExposureResult(target_max=49152, final_capture=capture, trials=())

        auto_expose = FakeAutoExpose()
        metering_regions = [{"type": "full", "name": "meter"}]

        def fake_capture(*, output_dir, filename_template, settings, **kwargs):
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            raw_file = output_path / filename_template.replace(".%C", ".cr3")
            raw_file.write_bytes(b"ambient")
            return camera_gphoto2.CaptureResult(
                connection=camera_gphoto2.CameraConnection("test", "usb:test"),
                settings=settings,
                saved_file=raw_file,
                stdout="",
            )

        def fake_decoder(raw_file, *, output_dir, formats, **kwargs):
            decode_path = Path(output_dir)
            decode_path.mkdir(parents=True, exist_ok=True)
            npy_file = decode_path / f"{Path(raw_file).stem}.npy"
            np.save(npy_file, np.full((2, 2, 3), 600, dtype=np.uint16))
            return camera_gphoto2.DecodeResult(
                source_file=Path(raw_file),
                output_files=(npy_file,),
                metadata_file=decode_path / f"{Path(raw_file).stem}.json",
                image_shape=(2, 2, 3),
                image_dtype="uint16",
                stats={"image": {"max": 600, "channel_max": [600, 600, 600]}, "raw_visible": {"max": 600}},
            )

        with project_temp_dir() as tmpdir:
            output = measure_channel_response.run_channel_response(
                output_root=Path(tmpdir),
                run_name="run",
                channels=("cw",),
                codes=(1,),
                max_code=4095,
                safe_code_limit=1024,
                allow_high_output=False,
                dry_run=False,
                include_ambient=True,
                settle_seconds=0.0,
                regions=[{"type": "full", "name": "full_image"}],
                target_max=49152,
                iso="100",
                aperture="4",
                image_format="RAW",
                min_shutter_speed="1/8000",
                max_shutter_speed="30",
                max_trials=5,
                max_captures=10,
                decode_formats=("npy",),
                auto_exposure_metering_regions=metering_regions,
                light_fn=fake_light,
                auto_expose_fn=auto_expose,
                capture_fn=fake_capture,
                decoder_fn=fake_decoder,
            )

            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(data["status"], "complete")
        self.assertIsNotNone(data["ambient"])
        self.assertEqual(len(data["measurements"]), 1)
        self.assertEqual(data["measurements"][0]["command"]["cw"], 1)
        self.assertEqual(data["camera"]["auto_exposure_metering"]["mode"], camera_gphoto2.METERING_MODE_LOCATION)
        self.assertEqual(data["measurements"][0]["regions"][0]["normalized"]["channel_mean_per_second"], [40.0, 40.0, 40.0])
        self.assertEqual(data["measurements"][0]["regions"][0]["ambient_subtracted"]["channel_mean_per_second"], [20.0, 20.0, 20.0])
        self.assertEqual(data["ambient"]["capture"]["iso"], "100")
        self.assertEqual(data["ambient"]["capture"]["shutter_speed"], "30")
        self.assertEqual(auto_expose.metering_regions, [metering_regions])
        self.assertEqual(auto_expose.initial_shutter_speeds, ["1/8000"])
        self.assertEqual(light_calls[0], {"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0})
        self.assertEqual(light_calls[-1], {"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0})

    def test_run_skips_lower_codes_when_channel_reaches_ambient_limit(self):
        light_calls = []

        def fake_light(cw, ww, r, g, b):
            payload = {"cw": int(cw), "ww": int(ww), "r": int(r), "g": int(g), "b": int(b)}
            light_calls.append(payload)
            return payload

        def fake_capture(*, output_dir, filename_template, settings, **kwargs):
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            raw_file = output_path / filename_template.replace(".%C", ".cr3")
            raw_file.write_bytes(b"ambient")
            return camera_gphoto2.CaptureResult(
                connection=camera_gphoto2.CameraConnection("test", "usb:test"),
                settings=settings,
                saved_file=raw_file,
                stdout="",
            )

        def fake_decoder(raw_file, *, output_dir, formats, **kwargs):
            decode_path = Path(output_dir)
            decode_path.mkdir(parents=True, exist_ok=True)
            npy_file = decode_path / f"{Path(raw_file).stem}.npy"
            np.save(npy_file, np.full((1, 1, 3), 300, dtype=np.uint16))
            return camera_gphoto2.DecodeResult(
                source_file=Path(raw_file),
                output_files=(npy_file,),
                metadata_file=decode_path / f"{Path(raw_file).stem}.json",
                image_shape=(1, 1, 3),
                image_dtype="uint16",
                stats={"image": {"max": 300, "channel_max": [300, 300, 300]}, "raw_visible": {"max": 300}},
            )

        class FakeAutoExpose:
            def __init__(self):
                self.calls = []
                self.initial_shutter_speeds = []

            def __call__(self, *, output_dir, filename_template, decode_output_dir, **kwargs):
                self.calls.append(filename_template)
                self.initial_shutter_speeds.append(kwargs.get("initial_shutter_speed"))
                output_path = Path(output_dir)
                decode_path = Path(decode_output_dir)
                output_path.mkdir(parents=True, exist_ok=True)
                decode_path.mkdir(parents=True, exist_ok=True)
                stem = filename_template.replace(".%C", "")
                raw_file = output_path / f"{stem}.cr3"
                raw_file.write_bytes(b"raw")
                npy_file = decode_path / f"{stem}.npy"
                np.save(npy_file, np.full((1, 1, 3), 301, dtype=np.uint16))
                decoded = camera_gphoto2.DecodeResult(
                    source_file=raw_file,
                    output_files=(npy_file,),
                    metadata_file=decode_path / f"{stem}.json",
                    image_shape=(1, 1, 3),
                    image_dtype="uint16",
                    stats={"image": {"max": 301, "channel_max": [301, 301, 301]}, "raw_visible": {"max": 301}},
                )
                capture = camera_gphoto2.CaptureResult(
                    connection=camera_gphoto2.CameraConnection("test", "usb:test"),
                    settings=camera_gphoto2.CaptureSettings(shutter_speed="30"),
                    saved_file=raw_file,
                    stdout="",
                    decoded=decoded,
                )
                return camera_gphoto2.AutoExposureResult(target_max=49152, final_capture=capture, trials=())

        auto_expose = FakeAutoExpose()
        with project_temp_dir() as tmpdir:
            output = measure_channel_response.run_channel_response(
                output_root=Path(tmpdir),
                run_name="skip",
                channels=("cw",),
                codes=(4, 2, 1),
                max_code=4095,
                safe_code_limit=1024,
                allow_high_output=False,
                dry_run=False,
                include_ambient=True,
                settle_seconds=0.0,
                regions=[{"type": "full", "name": "full_image"}],
                target_max=49152,
                iso="100",
                aperture="4",
                image_format="RAW",
                min_shutter_speed="1/8000",
                max_shutter_speed="30",
                max_trials=5,
                max_captures=10,
                decode_formats=("npy",),
                ambient_stop_threshold_per_second=2.0,
                light_fn=fake_light,
                auto_expose_fn=auto_expose,
                capture_fn=fake_capture,
                decoder_fn=fake_decoder,
            )

            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(data["status"], "complete")
        self.assertEqual([item["code"] for item in data["measurements"]], [4])
        self.assertEqual([item["code"] for item in data["skipped_measurements"]], [2, 1])
        self.assertEqual(data["measurements"][0]["stop_reason"]["kind"], "ambient_limited")
        self.assertEqual(len(auto_expose.calls), 1)
        self.assertEqual(auto_expose.initial_shutter_speeds, ["1/8000"])


if __name__ == "__main__":
    unittest.main()
