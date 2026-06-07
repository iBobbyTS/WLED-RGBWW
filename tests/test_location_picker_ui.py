import json
import time
import tempfile
import unittest
import urllib.error
import urllib.request
import zlib
from argparse import Namespace
from pathlib import Path
from unittest import mock

import numpy as np

from camera_based_rgbww_optimizer.control import camera_gphoto2
from camera_based_rgbww_optimizer.interaction import location_picker_ui


class LocationArgsTests(unittest.TestCase):
    def test_parse_args_includes_exposure_trial_limit(self):
        default_args = location_picker_ui.parse_args([])
        custom_args = location_picker_ui.parse_args(
            [
                "--max-exposure-trials",
                "3",
                "--ui-port",
                "0",
                "--port",
                "usb:001,010",
                "--metering-mode",
                "location",
                "--metering-location-config",
                "config/location/example.json",
            ]
        )

        self.assertEqual(default_args.max_exposure_trials, location_picker_ui.DEFAULT_MAX_EXPOSURE_TRIALS)
        self.assertEqual(default_args.rows, 4)
        self.assertEqual(default_args.cols, 6)
        self.assertEqual(default_args.metering_mode, camera_gphoto2.METERING_MODE_FULL)
        self.assertEqual(custom_args.max_exposure_trials, 3)
        self.assertEqual(custom_args.ui_port, 0)
        self.assertEqual(custom_args.camera_port, "usb:001,010")
        self.assertEqual(custom_args.metering_mode, camera_gphoto2.METERING_MODE_LOCATION)
        self.assertEqual(custom_args.metering_location_config, Path("config/location/example.json"))


class LocationPreviewTests(unittest.TestCase):
    def test_linear_rgb_to_preview_uint8_scales_and_clips(self):
        image = np.array([[[0, 24576, 49152], [65535, 49152, 0]]], dtype=np.uint16)

        preview = location_picker_ui.linear_rgb_to_preview_uint8(image, white_point=49152)

        self.assertEqual(preview.dtype, np.uint8)
        self.assertEqual(preview.tolist(), [[[0, 128, 255], [255, 255, 0]]])

    def test_rgb_to_png_data_writes_valid_rgb_png(self):
        image = np.array(
            [
                [[255, 0, 0], [0, 255, 0]],
                [[0, 0, 255], [255, 255, 255]],
            ],
            dtype=np.uint8,
        )

        data = location_picker_ui.rgb_to_png_data(image)

        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(int.from_bytes(data[16:20], "big"), 2)
        self.assertEqual(int.from_bytes(data[20:24], "big"), 2)
        self.assertEqual(data[24], 8)
        self.assertEqual(data[25], 2)
        idat_start = data.index(b"IDAT") + 4
        idat_length = int.from_bytes(data[idat_start - 8:idat_start - 4], "big")
        raw = zlib.decompress(data[idat_start : idat_start + idat_length])
        self.assertEqual(raw, b"\x00\xff\x00\x00\x00\xff\x00\x00\x00\x00\xff\xff\xff\xff")

    def test_detect_color_checker_quads_from_grid_lines(self):
        image = np.full((240, 360, 3), 180, dtype=np.uint8)
        top = 20
        left = 30
        cell_w = 45
        cell_h = 40
        line = 6
        rows = 4
        cols = 6
        height = rows * cell_h + (rows + 1) * line
        width = cols * cell_w + (cols + 1) * line
        image[top : top + height, left : left + width] = 5
        for row in range(rows):
            for col in range(cols):
                y0 = top + line + row * (cell_h + line)
                x0 = left + line + col * (cell_w + line)
                image[y0 : y0 + cell_h, x0 : x0 + cell_w] = 80 + row * 20 + col

        quads = location_picker_ui.detect_color_checker_quads(image, rows=rows, cols=cols, inset_ratio=0.0)

        self.assertEqual(len(quads), 24)
        self.assertLess(quads[0][0][0], quads[1][0][0])
        self.assertLess(quads[0][0][1], quads[6][0][1])
        self.assertAlmostEqual(quads[0][0][0], left + line, delta=1.0)
        self.assertAlmostEqual(quads[0][0][1], top + line, delta=1.0)
        self.assertAlmostEqual(quads[-1][2][0], left + width - line - 1, delta=1.0)
        self.assertAlmostEqual(quads[-1][2][1], top + height - line - 1, delta=1.0)

    def test_detect_color_checker_quads_ignores_adjacent_ruler(self):
        image = np.full((280, 430, 3), 170, dtype=np.uint8)
        rows = 4
        cols = 6
        top = 30
        left = 90
        cell_w = 38
        cell_h = 34
        line = 7
        height = rows * cell_h + (rows + 1) * line
        width = cols * cell_w + (cols + 1) * line

        image[top : top + height, left : left + width] = 4
        for tick_y in range(top + 8, top + height - 8, 12):
            image[tick_y : tick_y + 2, 34:70] = 55
        image[top : top + height, 28:34] = 4
        image[top + height - 10 : top + height + 16, 0:left] = 35
        for row in range(rows):
            for col in range(cols):
                y0 = top + line + row * (cell_h + line)
                x0 = left + line + col * (cell_w + line)
                value = 6 if (row, col) == (0, 0) else 65 + row * 18 + col * 4
                image[y0 : y0 + cell_h, x0 : x0 + cell_w] = value

        quads = location_picker_ui.detect_color_checker_quads(image, rows=rows, cols=cols, inset_ratio=0.0)

        self.assertEqual(len(quads), rows * cols)
        self.assertAlmostEqual(quads[0][0][0], left + line, delta=2.0)
        self.assertAlmostEqual(quads[0][0][1], top + line, delta=2.0)
        self.assertAlmostEqual(quads[-1][2][0], left + width - line - 1, delta=2.0)
        self.assertAlmostEqual(quads[-1][2][1], top + height - line - 1, delta=2.0)

    def test_detect_color_checker_quads_defaults_to_inner_blocks(self):
        image = np.full((180, 260, 3), 170, dtype=np.uint8)
        rows = 2
        cols = 3
        top = 20
        left = 25
        cell_w = 50
        cell_h = 45
        line = 8
        height = rows * cell_h + (rows + 1) * line
        width = cols * cell_w + (cols + 1) * line
        image[top : top + height, left : left + width] = 3
        for row in range(rows):
            for col in range(cols):
                y0 = top + line + row * (cell_h + line)
                x0 = left + line + col * (cell_w + line)
                image[y0 : y0 + cell_h, x0 : x0 + cell_w] = 90 + row * 30 + col * 10

        quads = location_picker_ui.detect_color_checker_quads(image, rows=rows, cols=cols)

        inset_x = cell_w * location_picker_ui.DEFAULT_AUTO_DETECT_INSET_RATIO
        inset_y = cell_h * location_picker_ui.DEFAULT_AUTO_DETECT_INSET_RATIO
        self.assertEqual(len(quads), rows * cols)
        self.assertGreaterEqual(quads[0][0][0], left + line + inset_x - 1)
        self.assertGreaterEqual(quads[0][0][1], top + line + inset_y - 1)
        self.assertLessEqual(quads[0][1][0], left + line + cell_w - inset_x)
        self.assertLessEqual(quads[0][2][1], top + line + cell_h - inset_y)

    def test_detect_color_checker_quads_uses_tilted_quads_when_opencv_is_available(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV is not installed")

        rows = 4
        cols = 6
        top = 25
        left = 35
        cell_w = 36
        cell_h = 32
        line = 7
        height = rows * cell_h + (rows + 1) * line
        width = cols * cell_w + (cols + 1) * line
        image = np.full((260, 360, 3), 180, dtype=np.uint8)
        yy, xx = np.indices(image.shape[:2])
        u = xx - left - 0.01 * (yy - top)
        v = yy - top - 0.03 * (xx - left)
        image[(u >= 0) & (u < width) & (v >= 0) & (v < height)] = 4
        for row in range(rows):
            for col in range(cols):
                u0 = line + col * (cell_w + line)
                v0 = line + row * (cell_h + line)
                mask = (u >= u0) & (u < u0 + cell_w) & (v >= v0) & (v < v0 + cell_h)
                image[mask] = 70 + row * 25 + col * 3

        estimate = location_picker_ui._estimate_axis_grid(image, rows=rows, cols=cols)
        quads = location_picker_ui._detect_color_checker_quads_opencv(
            estimate,
            rows=rows,
            cols=cols,
            inset_ratio=location_picker_ui.DEFAULT_AUTO_DETECT_INSET_RATIO,
        )

        self.assertEqual(len(quads), rows * cols)
        self.assertGreater(abs(quads[0][1][1] - quads[0][0][1]), 0.5)
        self.assertGreater(abs(quads[0][3][0] - quads[0][0][0]), 0.1)

    def test_detect_color_checker_quads_prefers_mcc_patch_quads(self):
        image = np.full((300, 420, 3), 120, dtype=np.uint8)
        patch_quads = []
        for row in range(4):
            for col in range(6):
                x0 = 30 + col * 60
                y0 = 25 + row * 58
                patch_quads.extend(
                    [
                        [x0 + 10.0, y0 + 12.0],
                        [x0 + 45.0, y0 + 13.0],
                        [x0 + 44.0, y0 + 45.0],
                        [x0 + 9.0, y0 + 44.0],
                    ]
                )

        class FakeChecker:
            def getColorCharts(self):
                return np.array(patch_quads, dtype=np.float32)

        class FakeDetector:
            processed_image = None

            def process(self, *_args):
                self.processed_image = _args[0]
                return True

            def getBestColorChecker(self):
                return FakeChecker()

        fake_mcc = mock.Mock()
        fake_mcc.MCC24 = 0
        detector = FakeDetector()
        fake_mcc.CCheckerDetector_create.return_value = FakeDetector()
        fake_mcc.CCheckerDetector_create.return_value = detector
        fake_cv2 = mock.Mock()
        fake_cv2.mcc = fake_mcc
        with mock.patch.object(location_picker_ui, "_import_cv2", return_value=fake_cv2):
            quads = location_picker_ui.detect_color_checker_quads(image, rows=4, cols=6)

        self.assertEqual(len(quads), 24)
        self.assertEqual(quads[0], [(40.0, 37.0), (75.0, 38.0), (74.0, 70.0), (39.0, 69.0)])
        np.testing.assert_array_equal(detector.processed_image, image[..., ::-1])


class LocationGeometryTests(unittest.TestCase):
    def test_hit_helpers_handle_polygon_and_edges(self):
        quad = [(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)]

        self.assertTrue(location_picker_ui.point_in_polygon((20.0, 20.0), quad))
        self.assertFalse(location_picker_ui.point_in_polygon((5.0, 20.0), quad))
        self.assertEqual(location_picker_ui.distance_to_segment((20.0, 15.0), (10.0, 10.0), (30.0, 10.0)), 5.0)

    def test_translate_quad_clamps_to_image_bounds(self):
        quad = [(1.0, 1.0), (4.0, 1.0), (4.0, 4.0), (1.0, 4.0)]

        translated = location_picker_ui.translate_quad(quad, -10.0, 20.0, width=10, height=10)

        self.assertEqual(translated, [(0.0, 6.0), (3.0, 6.0), (3.0, 9.0), (0.0, 9.0)])


class LocationConfigTests(unittest.TestCase):
    def test_build_location_config_requires_matching_count_and_serializes_points(self):
        capture = camera_gphoto2.CaptureResult(
            connection=camera_gphoto2.CameraConnection("Canon EOS R6 Mark III", "usb:001,010"),
            settings=camera_gphoto2.CaptureSettings(iso="100", aperture="4", shutter_speed="1/30", image_format="RAW"),
            saved_file=Path("tmp/location-ui/camera/test.cr3"),
            stdout="",
        )

        config = location_picker_ui.build_location_config(
            target_block_count=1,
            image_width=100,
            image_height=50,
            quads=[[(1.23456, 2.0), (10.0, 2.0), (10.0, 20.0), (1.0, 20.0)]],
            capture=capture,
            created_at="2026-06-05T00:00:00+00:00",
        )

        self.assertEqual(config["target_block_count"], 1)
        self.assertEqual(config["image"], {"width": 100, "height": 50})
        self.assertEqual(config["blocks"][0]["points"][0], {"x": 1.235, "y": 2.0})
        self.assertEqual(config["capture"]["iso"], "100")

        with self.assertRaises(ValueError):
            location_picker_ui.build_location_config(
                target_block_count=2,
                image_width=100,
                image_height=50,
                quads=[[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]],
                capture=None,
            )

    def test_build_location_config_adds_standard_colorchecker_metadata(self):
        quads = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)] for _index in range(24)]

        config = location_picker_ui.build_location_config(
            target_block_count=24,
            image_width=100,
            image_height=50,
            quads=quads,
            capture=None,
            rows=4,
            cols=6,
            chart_orientation_degrees=0,
        )

        self.assertEqual(config["chart"]["type"], "colorchecker_classic_24")
        self.assertEqual(config["chart"]["orientation_degrees"], 0)
        self.assertEqual(config["blocks"][0]["patch"]["name"], "dark_skin")
        self.assertEqual(config["blocks"][12]["patch"]["name"], "blue")
        self.assertIn("hue_anchor", config["blocks"][12]["patch"]["roles"])
        self.assertEqual(config["blocks"][18]["patch"]["name"], "white")
        self.assertIn("exposure", config["blocks"][18]["patch"]["roles"])

    def test_build_location_config_supports_180_degree_colorchecker_metadata(self):
        quads = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)] for _index in range(24)]

        config = location_picker_ui.build_location_config(
            target_block_count=24,
            image_width=100,
            image_height=50,
            quads=quads,
            capture=None,
            rows=4,
            cols=6,
            chart_orientation_degrees=180,
        )

        self.assertEqual(config["chart"]["orientation_degrees"], 180)
        self.assertEqual(config["chart"]["standard_index_formula"], "25 - block_index")
        self.assertEqual(config["blocks"][0]["patch"]["name"], "black")
        self.assertEqual(config["blocks"][5]["patch"]["name"], "white")
        self.assertEqual(config["blocks"][6]["patch"]["name"], "cyan")
        self.assertEqual(config["blocks"][7]["patch"]["name"], "magenta")
        self.assertEqual(config["blocks"][8]["patch"]["name"], "yellow")
        self.assertEqual(config["blocks"][9]["patch"]["name"], "red")
        self.assertEqual(config["blocks"][10]["patch"]["name"], "green")
        self.assertEqual(config["blocks"][11]["patch"]["name"], "blue")

    def test_build_location_config_omits_colorchecker_metadata_for_other_grids(self):
        config = location_picker_ui.build_location_config(
            target_block_count=1,
            image_width=100,
            image_height=50,
            quads=[[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]],
            capture=None,
            rows=1,
            cols=1,
        )

        self.assertNotIn("chart", config)
        self.assertNotIn("patch", config["blocks"][0])

    def test_build_location_config_omits_colorchecker_metadata_when_block_count_is_not_24(self):
        config = location_picker_ui.build_location_config(
            target_block_count=1,
            image_width=100,
            image_height=50,
            quads=[[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]],
            capture=None,
            rows=4,
            cols=6,
            chart_orientation_degrees=180,
        )

        self.assertNotIn("chart", config)
        self.assertNotIn("patch", config["blocks"][0])

    def test_save_location_config_writes_under_requested_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "config" / "location"

            path = location_picker_ui.save_location_config({"version": 1}, output_dir=output_dir)

            self.assertEqual(path.parent, output_dir)
            self.assertTrue(path.exists())
            self.assertIn("locations-", path.name)


class LocationWebApiTests(unittest.TestCase):
    def make_state(self) -> location_picker_ui.LocationPickerState:
        args = Namespace(
            blocks=1,
            rows=1,
            cols=1,
            chart_orientation=0,
            target_max=location_picker_ui.DEFAULT_TARGET_MAX,
            iso="100",
            aperture="4",
            min_shutter_speed="1/8000",
            max_shutter_speed="30",
            max_exposure_trials=1,
            metering_mode=camera_gphoto2.METERING_MODE_FULL,
            metering_location_config=None,
            model=camera_gphoto2.DEFAULT_CAMERA_MODEL,
            camera_port=None,
            gphoto2="gphoto2",
            timeout=1.0,
        )
        state = location_picker_ui.LocationPickerState(args=args)
        capture = camera_gphoto2.CaptureResult(
            connection=camera_gphoto2.CameraConnection("Canon EOS R6 Mark III", "usb:001,010"),
            settings=camera_gphoto2.CaptureSettings(iso="100", aperture="4", shutter_speed="1/30", image_format="RAW"),
            saved_file=Path("tmp/location-ui/camera/test.cr3"),
            stdout="",
        )
        state.preview_image = np.array([[[255, 0, 0], [0, 255, 0]]], dtype=np.uint8)
        state.preview_png = location_picker_ui.rgb_to_png_data(state.preview_image)
        state.image_width = 2
        state.image_height = 1
        state.capture_result = camera_gphoto2.AutoExposureResult(target_max=49152, final_capture=capture, trials=())
        state.status = "已加载"
        state.loading = False
        return state

    def make_colorchecker_state(self) -> location_picker_ui.LocationPickerState:
        state = self.make_state()
        state.args.blocks = 24
        state.args.rows = 4
        state.args.cols = 6
        state.args.chart_orientation = 0
        state.image_width = 10
        state.image_height = 10
        return state

    def start_server(self, state: location_picker_ui.LocationPickerState):
        server = location_picker_ui.create_location_picker_server(host="127.0.0.1", port=0, state=state)
        thread = location_picker_ui.threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_state_and_preview_endpoints(self):
        state = self.make_state()
        server, base_url = self.start_server(state)
        try:
            with urllib.request.urlopen(base_url + "/api/state", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(base_url + "/preview.png", timeout=5) as response:
                preview = response.read()

            self.assertTrue(payload["has_image"])
            self.assertEqual(payload["image"], {"width": 2, "height": 1})
            self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))
        finally:
            server.shutdown()
            server.server_close()

    def test_save_endpoint_validates_and_writes_config(self):
        state = self.make_colorchecker_state()
        server, base_url = self.start_server(state)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "locations"
            quads = [
                [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
                for _index in range(24)
            ]
            try:
                request = urllib.request.Request(
                    base_url + "/api/save",
                    data=json.dumps(
                        {
                            "target_block_count": 24,
                            "rows": 4,
                            "cols": 6,
                            "chart_orientation_degrees": 180,
                            "quads": quads,
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with mock.patch.object(location_picker_ui, "CONFIG_LOCATION_DIR", output_dir):
                    with urllib.request.urlopen(request, timeout=5) as response:
                        payload = json.loads(response.read().decode("utf-8"))

                saved_path = Path(payload["path"])
                self.assertEqual(saved_path.parent, output_dir)
                self.assertTrue(saved_path.exists())
                self.assertEqual(payload["config"]["target_block_count"], 24)
                self.assertEqual(payload["config"]["chart"]["orientation_degrees"], 180)
                self.assertEqual(payload["config"]["blocks"][0]["patch"]["name"], "black")
                self.assertEqual(payload["config"]["blocks"][5]["patch"]["name"], "white")
            finally:
                server.shutdown()
                server.server_close()

    def test_detect_endpoint_rejects_missing_rows(self):
        state = self.make_state()
        server, base_url = self.start_server(state)
        try:
            request = urllib.request.Request(
                base_url + "/api/detect",
                data=json.dumps({"cols": 1}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)

            self.assertEqual(raised.exception.code, 400)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(payload["error"], "rows is required")
        finally:
            server.shutdown()
            server.server_close()

    def test_retry_endpoint_restarts_capture_after_failure(self):
        args = Namespace(
            blocks=1,
            rows=1,
            cols=1,
            chart_orientation=0,
            target_max=location_picker_ui.DEFAULT_TARGET_MAX,
            iso="100",
            aperture="4",
            min_shutter_speed="1/8000",
            max_shutter_speed="30",
            max_exposure_trials=1,
            metering_mode=camera_gphoto2.METERING_MODE_FULL,
            metering_location_config=None,
            model=camera_gphoto2.DEFAULT_CAMERA_MODEL,
            camera_port=None,
            gphoto2="gphoto2",
            timeout=1.0,
        )
        state = location_picker_ui.LocationPickerState(args=args)
        capture = camera_gphoto2.CaptureResult(
            connection=camera_gphoto2.CameraConnection("Canon EOS R6 Mark III", "usb:001,010"),
            settings=camera_gphoto2.CaptureSettings(iso="100", aperture="4", shutter_speed="1/30", image_format="RAW"),
            saved_file=Path("tmp/location-ui/camera/test.cr3"),
            stdout="",
        )
        auto_result = camera_gphoto2.AutoExposureResult(target_max=49152, final_capture=capture, trials=())
        preview = np.array([[[10, 20, 30]]], dtype=np.uint8)

        with mock.patch.object(location_picker_ui.camera_gphoto2, "auto_expose_capture", side_effect=[RuntimeError("camera offline"), auto_result]):
            with mock.patch.object(location_picker_ui, "find_npy_output", return_value=Path("unused.npy")):
                with mock.patch.object(location_picker_ui, "load_preview_from_npy", return_value=preview):
                    self.assertTrue(state.start_auto_exposure())
                    self.wait_for_state(state, lambda snapshot: not snapshot["loading"])
                    failed = state.snapshot()
                    self.assertFalse(failed["has_image"])
                    self.assertEqual(failed["error"], "camera offline")
                    self.assertTrue(failed["retry_available"])

                    server, base_url = self.start_server(state)
                    try:
                        request = urllib.request.Request(
                            base_url + "/api/retry",
                            data=b"{}",
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with urllib.request.urlopen(request, timeout=5) as response:
                            retry_payload = json.loads(response.read().decode("utf-8"))

                        self.assertTrue(retry_payload["retry_started"])
                        self.wait_for_state(state, lambda snapshot: snapshot["has_image"])
                        loaded = state.snapshot()
                        self.assertTrue(loaded["has_image"])
                        self.assertIsNone(loaded["error"])
                        self.assertFalse(loaded["retry_available"])
                    finally:
                        server.shutdown()
                        server.server_close()

    def test_capture_worker_passes_location_metering_regions(self):
        args = Namespace(
            blocks=24,
            rows=4,
            cols=6,
            chart_orientation=0,
            target_max=location_picker_ui.DEFAULT_TARGET_MAX,
            iso="100",
            aperture="4",
            min_shutter_speed="1/8000",
            max_shutter_speed="30",
            max_exposure_trials=1,
            metering_mode=camera_gphoto2.METERING_MODE_LOCATION,
            metering_location_config=Path("config/location/test.json"),
            model=camera_gphoto2.DEFAULT_CAMERA_MODEL,
            camera_port=None,
            gphoto2="gphoto2",
            timeout=1.0,
        )
        state = location_picker_ui.LocationPickerState(args=args)
        capture = camera_gphoto2.CaptureResult(
            connection=camera_gphoto2.CameraConnection("Canon EOS R6 Mark III", "usb:001,010"),
            settings=camera_gphoto2.CaptureSettings(iso="100", aperture="4", shutter_speed="1/30", image_format="RAW"),
            saved_file=Path("tmp/location-ui/camera/test.cr3"),
            stdout="",
            decoded=camera_gphoto2.DecodeResult(
                source_file=Path("tmp/location-ui/camera/test.cr3"),
                output_files=(Path("tmp/location-ui/decoded/test.npy"),),
                metadata_file=Path("tmp/location-ui/decoded/test.json"),
                image_shape=(1, 1, 3),
                image_dtype="uint8",
                stats={"image": {"max": 100}, "raw_visible": {"max": 25}},
            ),
        )
        auto_result = camera_gphoto2.AutoExposureResult(target_max=49152, final_capture=capture, trials=())
        preview = np.array([[[10, 20, 30]]], dtype=np.uint8)
        regions = [{"type": "full", "name": "meter"}]

        with mock.patch.object(location_picker_ui.camera_gphoto2, "load_metering_regions", return_value=regions) as load_regions:
            with mock.patch.object(location_picker_ui.camera_gphoto2, "auto_expose_capture", return_value=auto_result) as auto_expose:
                with mock.patch.object(location_picker_ui, "find_npy_output", return_value=Path("unused.npy")):
                    with mock.patch.object(location_picker_ui, "load_preview_from_npy", return_value=preview):
                        with mock.patch.object(location_picker_ui, "detect_color_checker_quads", side_effect=RuntimeError("not found")):
                            self.assertTrue(state.start_auto_exposure())
                            self.wait_for_state(state, lambda snapshot: snapshot["has_image"])

        load_regions.assert_called_once_with(Path("config/location/test.json"))
        self.assertIs(auto_expose.call_args.kwargs["metering_regions"], regions)

    def test_capture_success_exposes_initial_auto_detect_quads(self):
        args = Namespace(
            blocks=24,
            rows=4,
            cols=6,
            chart_orientation=0,
            target_max=location_picker_ui.DEFAULT_TARGET_MAX,
            iso="100",
            aperture="4",
            min_shutter_speed="1/8000",
            max_shutter_speed="30",
            max_exposure_trials=1,
            metering_mode=camera_gphoto2.METERING_MODE_FULL,
            metering_location_config=None,
            model=camera_gphoto2.DEFAULT_CAMERA_MODEL,
            camera_port=None,
            gphoto2="gphoto2",
            timeout=1.0,
        )
        state = location_picker_ui.LocationPickerState(args=args)
        capture = camera_gphoto2.CaptureResult(
            connection=camera_gphoto2.CameraConnection("Canon EOS R6 Mark III", "usb:001,010"),
            settings=camera_gphoto2.CaptureSettings(iso="100", aperture="4", shutter_speed="1/30", image_format="RAW"),
            saved_file=Path("tmp/location-ui/camera/test.cr3"),
            stdout="",
        )
        auto_result = camera_gphoto2.AutoExposureResult(target_max=49152, final_capture=capture, trials=())
        preview = np.array([[[10, 20, 30], [40, 50, 60]]], dtype=np.uint8)
        quads = [[(0.0, 0.0), (1.0, 0.0), (1.0, 0.0), (0.0, 0.0)] for _index in range(24)]

        with mock.patch.object(location_picker_ui.camera_gphoto2, "auto_expose_capture", return_value=auto_result):
            with mock.patch.object(location_picker_ui, "find_npy_output", return_value=Path("unused.npy")):
                with mock.patch.object(location_picker_ui, "load_preview_from_npy", return_value=preview):
                    with mock.patch.object(location_picker_ui, "detect_color_checker_quads", return_value=quads) as detect:
                        self.assertTrue(state.start_auto_exposure())
                        self.wait_for_state(state, lambda snapshot: snapshot["has_image"])

        snapshot = state.snapshot()
        detect.assert_called_once_with(preview, rows=4, cols=6)
        self.assertIn("已自动识别 4x6 色块", snapshot["status"])
        self.assertEqual(snapshot["auto_detect"]["target_block_count"], 24)
        self.assertEqual(len(snapshot["auto_detect"]["quads"]), 24)
        self.assertIsNone(snapshot["auto_detect"]["error"])

    def test_capture_success_keeps_preview_when_initial_auto_detect_fails(self):
        args = Namespace(
            blocks=24,
            rows=4,
            cols=6,
            chart_orientation=0,
            target_max=location_picker_ui.DEFAULT_TARGET_MAX,
            iso="100",
            aperture="4",
            min_shutter_speed="1/8000",
            max_shutter_speed="30",
            max_exposure_trials=1,
            metering_mode=camera_gphoto2.METERING_MODE_FULL,
            metering_location_config=None,
            model=camera_gphoto2.DEFAULT_CAMERA_MODEL,
            camera_port=None,
            gphoto2="gphoto2",
            timeout=1.0,
        )
        state = location_picker_ui.LocationPickerState(args=args)
        capture = camera_gphoto2.CaptureResult(
            connection=camera_gphoto2.CameraConnection("Canon EOS R6 Mark III", "usb:001,010"),
            settings=camera_gphoto2.CaptureSettings(iso="100", aperture="4", shutter_speed="1/30", image_format="RAW"),
            saved_file=Path("tmp/location-ui/camera/test.cr3"),
            stdout="",
        )
        auto_result = camera_gphoto2.AutoExposureResult(target_max=49152, final_capture=capture, trials=())
        preview = np.array([[[10, 20, 30], [40, 50, 60]]], dtype=np.uint8)

        with mock.patch.object(location_picker_ui.camera_gphoto2, "auto_expose_capture", return_value=auto_result):
            with mock.patch.object(location_picker_ui, "find_npy_output", return_value=Path("unused.npy")):
                with mock.patch.object(location_picker_ui, "load_preview_from_npy", return_value=preview):
                    with mock.patch.object(location_picker_ui, "detect_color_checker_quads", side_effect=RuntimeError("not found")):
                        self.assertTrue(state.start_auto_exposure())
                        self.wait_for_state(state, lambda snapshot: snapshot["has_image"])

        snapshot = state.snapshot()
        self.assertTrue(snapshot["has_image"])
        self.assertIn("自动识别失败: not found", snapshot["status"])
        self.assertIsNone(snapshot["auto_detect"]["quads"])
        self.assertEqual(snapshot["auto_detect"]["error"], "not found")

    def wait_for_state(self, state: location_picker_ui.LocationPickerState, predicate) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate(state.snapshot()):
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for state; last snapshot: {state.snapshot()}")


if __name__ == "__main__":
    unittest.main()
