import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import camera_gphoto2
import location_picker_ui


class LocationArgsTests(unittest.TestCase):
    def test_parse_args_includes_exposure_trial_limit(self):
        default_args = location_picker_ui.parse_args([])
        custom_args = location_picker_ui.parse_args(["--max-exposure-trials", "3"])

        self.assertEqual(default_args.max_exposure_trials, location_picker_ui.DEFAULT_MAX_EXPOSURE_TRIALS)
        self.assertEqual(default_args.rows, 4)
        self.assertEqual(default_args.cols, 6)
        self.assertEqual(custom_args.max_exposure_trials, 3)


class LocationPreviewTests(unittest.TestCase):
    def test_linear_rgb_to_preview_uint8_scales_and_clips(self):
        image = np.array([[[0, 24576, 49152], [65535, 49152, 0]]], dtype=np.uint16)

        preview = location_picker_ui.linear_rgb_to_preview_uint8(image, white_point=49152)

        self.assertEqual(preview.dtype, np.uint8)
        self.assertEqual(preview.tolist(), [[[0, 128, 255], [255, 255, 0]]])

    def test_resize_nearest_rgb_preserves_expected_pixels(self):
        image = np.array(
            [
                [[1, 0, 0], [2, 0, 0]],
                [[3, 0, 0], [4, 0, 0]],
            ],
            dtype=np.uint8,
        )

        resized = location_picker_ui.resize_nearest_rgb(image, 2.0)

        self.assertEqual(resized.shape, (4, 4, 3))
        self.assertEqual(resized[0, 0, 0], 1)
        self.assertEqual(resized[0, 2, 0], 2)
        self.assertEqual(resized[2, 0, 0], 3)
        self.assertEqual(resized[2, 2, 0], 4)

    def test_ppm_photo_data_loads_in_tk(self):
        image = np.array(
            [
                [[255, 0, 0], [0, 255, 0]],
                [[0, 0, 255], [255, 255, 255]],
            ],
            dtype=np.uint8,
        )

        data = location_picker_ui.rgb_to_ppm_photo_data(image)

        self.assertTrue(data.startswith(b"P6\n2 2\n255\n"))
        root = tk.Tk()
        root.withdraw()
        try:
            photo = tk.PhotoImage(data=data, format="PPM")
            self.assertEqual(photo.width(), 2)
            self.assertEqual(photo.height(), 2)
        finally:
            root.destroy()

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

    def test_save_location_config_writes_under_requested_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "config" / "location"

            path = location_picker_ui.save_location_config({"version": 1}, output_dir=output_dir)

            self.assertEqual(path.parent, output_dir)
            self.assertTrue(path.exists())
            self.assertIn("locations-", path.name)


if __name__ == "__main__":
    unittest.main()
