import tempfile
import tkinter as tk
import unittest
from pathlib import Path

import numpy as np

import camera_gphoto2
import location_picker_ui


class LocationArgsTests(unittest.TestCase):
    def test_parse_args_includes_exposure_trial_limit(self):
        default_args = location_picker_ui.parse_args([])
        custom_args = location_picker_ui.parse_args(["--max-exposure-trials", "3"])

        self.assertEqual(default_args.max_exposure_trials, location_picker_ui.DEFAULT_MAX_EXPOSURE_TRIALS)
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
