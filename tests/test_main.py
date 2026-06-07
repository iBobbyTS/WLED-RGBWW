import unittest

from camera_based_rgbww_optimizer.control import esphome


class LightPayloadTests(unittest.TestCase):
    def test_default_device_identity_uses_physical_fixture_name(self):
        self.assertEqual(esphome.DEFAULT_HOST, "bedroom-rgbww-strip.local")
        self.assertEqual(esphome.DEFAULT_EXPECTED_NAME, "bedroom-rgbww-strip")

    def test_build_payload_uses_cw_ww_r_g_b_call_order(self):
        self.assertEqual(
            esphome._build_payload(cw=1, ww=2, r=3, g=4, b=5),
            {
                "red": 3,
                "green": 4,
                "blue": 5,
                "warm_white": 2,
                "cold_white": 1,
            },
        )

    def test_build_payload_converts_with_int_without_clamping(self):
        self.assertEqual(
            esphome._build_payload(cw=-1, ww=4096.9, r="5000", g=True, b=0),
            {
                "red": 5000,
                "green": 1,
                "blue": 0,
                "warm_white": 4096,
                "cold_white": -1,
            },
        )

    def test_is_all_zero_after_int_conversion(self):
        self.assertTrue(esphome._is_all_zero(esphome._build_payload(0, 0, 0, 0, 0)))
        self.assertFalse(esphome._is_all_zero(esphome._build_payload(0, 0, 0, 0, 1)))

    def test_build_payload_applies_code_duty_curve_when_provided(self):
        curve = {
            "max_code": 4095,
            "channels": {
                "cw": {
                    "points": [
                        {"target_code": 0, "pwm_code": 0},
                        {"target_code": 2048, "pwm_code": 1024},
                        {"target_code": 4095, "pwm_code": 4095},
                    ]
                },
                "ww": {
                    "points": [
                        {"target_code": 0, "pwm_code": 0},
                        {"target_code": 4095, "pwm_code": 4095},
                    ]
                },
            },
        }

        self.assertEqual(
            esphome._build_payload(cw=2048, ww=4095, r=123, g=0, b=0, curve=curve),
            {
                "red": 123,
                "green": 0,
                "blue": 0,
                "warm_white": 4095,
                "cold_white": 1024,
            },
        )

    def test_missing_code_duty_curve_keeps_direct_mode(self):
        self.assertIsNone(esphome._load_code_duty_curve_if_present("tmp/does-not-exist-code-duty-curve.json"))


if __name__ == "__main__":
    unittest.main()
