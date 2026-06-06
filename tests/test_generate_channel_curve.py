import unittest

import generate_channel_curve


def measurement(channel, code, response):
    return {
        "channel": channel,
        "code": code,
        "duty": code / 4095,
        "max_ambient_subtracted_mean_per_second": response,
    }


class GenerateChannelCurveTests(unittest.TestCase):
    def test_build_channel_curve_filters_non_monotonic_points(self):
        merged = {
            "kind": "merged_wled_rgbww_channel_code_response",
            "codes": [4095, 16, 12, 8],
            "channels": ["cw"],
            "measurements": [
                measurement("cw", 4095, 1000.0),
                measurement("cw", 16, 100.0),
                measurement("cw", 12, 90.0),
                measurement("cw", 8, 95.0),
            ],
        }

        curve = generate_channel_curve.build_code_duty_curve(merged)
        channel_curve = curve["channels"]["cw"]

        self.assertEqual([point["pwm_code"] for point in channel_curve["points"]], [0, 8, 16, 4095])
        self.assertEqual(channel_curve["discarded_points"][0]["code"], 12)
        self.assertEqual(channel_curve["discarded_points"][0]["reason"], "non_monotonic_response")

    def test_build_channel_curve_uses_full_scale_response_for_target_codes(self):
        merged = {
            "codes": [4095, 2048],
            "channels": ["cw"],
            "measurements": [
                measurement("cw", 4095, 1000.0),
                measurement("cw", 2048, 250.0),
            ],
        }

        curve = generate_channel_curve.build_code_duty_curve(merged)
        points = curve["channels"]["cw"]["points"]

        self.assertEqual(points[1], {"target_code": 1023.75, "pwm_code": 2048})


if __name__ == "__main__":
    unittest.main()
