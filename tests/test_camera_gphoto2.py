import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
