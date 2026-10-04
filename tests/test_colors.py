import unittest

from aquacontrol.colors import hex_to_hsv1536, hsv1536_to_hex


class HexToHsvTest(unittest.TestCase):
    def assertHsv(self, hex_color, h, s, v, tol=1):
        got = hex_to_hsv1536(hex_color)
        self.assertLessEqual(abs(got[0] - h), tol, got)
        self.assertEqual(got[1:], (s, v))

    def test_primary_colours(self):
        self.assertHsv("#00ff00", 512, 255, 255)
        self.assertHsv("#ff0000", 0, 255, 255)
        self.assertHsv("#0000ff", 1024, 255, 255)
        self.assertHsv("#ffff00", 256, 255, 255)

    def test_exact_hue_scale(self):
        self.assertEqual(hex_to_hsv1536("#00ff00"), (512, 255, 255))
        self.assertEqual(hex_to_hsv1536("#00ffff"), (768, 255, 255))
        self.assertEqual(hex_to_hsv1536("#ff00ff"), (1280, 255, 255))

    def test_white_and_black(self):
        h, s, v = hex_to_hsv1536("#ffffff")
        self.assertEqual((s, v), (0, 255))
        self.assertEqual(hex_to_hsv1536("#000000")[2], 0)

    def test_just_below_red_is_the_last_hue_step(self):
        self.assertEqual(hex_to_hsv1536("#ff0001")[0], 1535)

    def test_case_and_format(self):
        self.assertEqual(hex_to_hsv1536("#00FF00"), hex_to_hsv1536("#00ff00"))
        for bad in ("00ff00", "#0f0", "#gg0000", "#00ff000", "", None, 5, "#00ff0é"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hex_to_hsv1536(bad)

    def test_back_to_hex(self):
        self.assertEqual(hsv1536_to_hex(512, 255, 255), "#00ff00")
        self.assertEqual(hsv1536_to_hex(0, 255, 255), "#ff0000")
        self.assertEqual(hsv1536_to_hex(1024, 255, 255), "#0000ff")
        self.assertEqual(hsv1536_to_hex(256, 255, 255), "#ffff00")
        self.assertEqual(hsv1536_to_hex(0, 0, 255), "#ffffff")
        self.assertEqual(hsv1536_to_hex(700, 90, 0), "#000000")

    def test_device_colours(self):
        # aquasuite writes green as h=511 (just below 120 degrees)
        self.assertRegex(hsv1536_to_hex(511, 255, 255), r"^#0[01]ff0[01]$")
        self.assertEqual(hsv1536_to_hex(255, 255, 255), "#fffe00")

    def test_hex_round_trip_stays_close(self):
        worst = 0
        for r in range(0, 256, 15):
            for g in range(0, 256, 15):
                for b in range(0, 256, 15):
                    h, s, v = hex_to_hsv1536(f"#{r:02x}{g:02x}{b:02x}")
                    self.assertTrue(0 <= h < 1536 and 0 <= s <= 255 and 0 <= v <= 255)
                    back = hsv1536_to_hex(h, s, v)
                    worst = max(worst, *(abs(int(back[i:i + 2], 16) - c) for i, c in ((1, r), (3, g), (5, b))))
        self.assertLessEqual(worst, 3)


if __name__ == "__main__":
    unittest.main()
