import unittest

import main


class LightPayloadTests(unittest.TestCase):
    def test_build_payload_uses_cw_ww_r_g_b_call_order(self):
        self.assertEqual(
            main._build_payload(cw=1, ww=2, r=3, g=4, b=5),
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
            main._build_payload(cw=-1, ww=4096.9, r="5000", g=True, b=0),
            {
                "red": 5000,
                "green": 1,
                "blue": 0,
                "warm_white": 4096,
                "cold_white": -1,
            },
        )

    def test_is_all_zero_after_int_conversion(self):
        self.assertTrue(main._is_all_zero(main._build_payload(0, 0, 0, 0, 0)))
        self.assertFalse(main._is_all_zero(main._build_payload(0, 0, 0, 0, 1)))


if __name__ == "__main__":
    unittest.main()
