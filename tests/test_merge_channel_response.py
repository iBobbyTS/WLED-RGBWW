import json
import tempfile
import unittest
from pathlib import Path

from camera_based_rgbww_optimizer.utils import merge_channel_response


def make_run(path: Path, *, status: str, channel: str, codes: list[int]) -> Path:
    measurements = []
    for index, code in enumerate(codes):
        command = {"cw": 0, "ww": 0, "r": 0, "g": 0, "b": 0}
        command[channel] = code
        measurements.append(
            {
                "index": index,
                "channel": channel,
                "code": code,
                "duty": code / 4095,
                "command": command,
                "shutter_seconds": 0.5,
                "max_ambient_subtracted_mean_per_second": float(code),
                "auto_exposure": {
                    "capture_count": 2,
                    "final": {
                        "shutter_speed": "1/2",
                        "decoded": {"metering": {"image_max": 40000}},
                    },
                },
                "regions": [
                    {
                        "name": "block_01",
                        "index": 1,
                        "stats": {"pixel_count": 4},
                        "normalized": {
                            "channel_mean_per_second": [1.0, 2.0, 3.0],
                            "channel_median_per_second": [1.5, 2.5, 3.5],
                        },
                        "ambient_subtracted": {
                            "channel_mean_per_second": [0.5, 1.5, 2.5],
                            "mean_per_second": 1.5,
                        },
                    }
                ],
            }
        )
    payload = {
        "status": status,
        "created_at": "2026-06-06T00:00:00+00:00",
        "run_dir": str(path.parent),
        "measurement": {"max_code": 4095},
        "measurements": measurements,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class MergeChannelResponseTests(unittest.TestCase):
    def test_merge_remaps_white_channels_and_keeps_requested_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = make_run(root / "first" / "channel-response.json", status="running", channel="cw", codes=[4095, 8, 6])
            second = make_run(root / "second" / "channel-response.json", status="complete", channel="ww", codes=[4095, 8])

            merged = merge_channel_response.merge_channel_responses(
                [first, second],
                codes=(4095, 8),
                channel_maps={
                    str(first): {"cw": "ww"},
                    str(second): {"ww": "cw"},
                },
            )

        self.assertEqual(len(merged["measurements"]), 4)
        self.assertEqual([(m["channel"], m["code"]) for m in merged["measurements"]], [("cw", 4095), ("cw", 8), ("ww", 4095), ("ww", 8)])
        self.assertEqual(merged["measurements"][0]["original_channel"], "ww")
        self.assertEqual(merged["measurements"][0]["command"], {"cw": 4095, "ww": 0, "r": 0, "g": 0, "b": 0})
        self.assertEqual(merged["measurements"][2]["original_channel"], "cw")
        self.assertEqual(merged["measurements"][2]["command"], {"cw": 0, "ww": 4095, "r": 0, "g": 0, "b": 0})
        self.assertEqual(merged["coverage"]["cw"]["missing_codes"], [])
        self.assertEqual(merged["coverage"]["r"]["missing_codes"], [4095, 8])
        self.assertEqual(merged["measurements"][0]["regions"][0]["ambient_subtracted_channel_mean_per_second"], [0.5, 1.5, 2.5])


if __name__ == "__main__":
    unittest.main()
