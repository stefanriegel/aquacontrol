import unittest
from dataclasses import replace

from aquacontrol import protocol as p
from tests.fixtures import load


class CrcTest(unittest.TestCase):
    def test_known_vector(self):
        # CRC-16/USB check value from the CRC catalogue
        self.assertEqual(p.crc16_usb(b"123456789"), 0xB4C8)

    def test_fixture_reports_have_valid_crc(self):
        for name in ("settings_live.bin", "settings_aquasuite_2025.bin"):
            with self.subTest(name=name):
                self.assertTrue(p.settings_crc_ok(load(name)))

    def test_names_report_crc(self):
        r = load("names.bin")
        self.assertEqual(p.crc16_usb(r[1:-2]), int.from_bytes(r[-2:], "big"))


class DecodeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.s = p.decode_settings(load("settings_live.bin"))

    def test_fan_configs(self):
        self.assertEqual(self.s.fans[0], p.FanConfig(3, 28.02, 90.3, 100.0, 4500))
        self.assertEqual(self.s.fans[1], p.FanConfig(3, 5.0, 100.0, 75.78, 2000))
        self.assertEqual(self.s.fans[3].min_percent, 9.69)

    def test_controller_modes_and_targets(self):
        modes = [c.mode for c in self.s.controllers]
        self.assertEqual(modes, [p.MODE_CURVE, p.MODE_TARGET, p.MODE_TARGET, p.MODE_TARGET])
        self.assertEqual([c.target_c for c in self.s.controllers], [36.0, 34.0, 35.0, 37.0])
        self.assertEqual(self.s.controllers[0].fixed_percent, 29.52)
        self.assertEqual(self.s.controllers[0].sensor, 0)
        self.assertEqual(self.s.controllers[0].pid, (1400, 1200, 0, 40, 20, 1))

    def test_pump_curve(self):
        curve = self.s.controllers[0].curve
        self.assertEqual(len(curve), 16)
        self.assertEqual(curve[0], (12.53, 0.0))
        self.assertEqual(curve[6], (30.05, 6.35))
        self.assertEqual(curve[15], (43.0, 90.0))
        self.assertEqual(self.s.controllers[0].curve_start_c, 32.45)

    def test_strip_and_leds(self):
        self.assertEqual(self.s.strip_brightness, 218)
        self.assertTrue(self.s.strip_enabled)
        self.assertEqual((self.s.leds[0].led_start, self.s.leds[0].led_count, self.s.leds[0].mode), (0, 30, 18))
        self.assertEqual((self.s.leds[1].led_start, self.s.leds[1].led_count), (30, 15))
        self.assertEqual((self.s.leds[6].led_start, self.s.leds[6].led_count, self.s.leds[6].flags), (45, 15, 6))
        self.assertEqual(len(self.s.leds[0].raw), 70)
        self.assertEqual(self.s.profile, 1)

    def test_rejects_bad_crc(self):
        r = bytearray(load("settings_live.bin"))
        r[100] ^= 0xFF
        with self.assertRaises(p.ProtocolError):
            p.decode_settings(bytes(r))

    def test_rejects_wrong_length_and_id(self):
        r = load("settings_live.bin")
        with self.assertRaises(p.ProtocolError):
            p.decode_settings(r[:-1])
        with self.assertRaises(p.ProtocolError):
            p.decode_settings(b"\x04" + r[1:])


class EncodeSettingsTest(unittest.TestCase):
    def test_roundtrip_is_byte_identical(self):
        for name in ("settings_live.bin", "settings_aquasuite_2025.bin"):
            with self.subTest(name=name):
                r = load(name)
                self.assertEqual(p.encode_settings(p.decode_settings(r), r), r)

    def test_change_touches_only_that_field_and_crc(self):
        r = load("settings_live.bin")
        s = p.with_controller(p.decode_settings(r), 3, target_c=38.5)
        out = p.encode_settings(s, r)
        diff = [i for i in range(len(r)) if r[i] != out[i]]
        # controller 4 base = payload 53 + 3*85 = 308; setpoint at +5 -> payload 313 -> absolute 314..315
        self.assertTrue(set(diff) <= {314, 315, 959, 960}, diff)
        self.assertTrue(p.settings_crc_ok(out))
        self.assertEqual(p.decode_settings(out).controllers[3].target_c, 38.5)

    def test_curve_and_fan_limits_roundtrip(self):
        r = load("settings_live.bin")
        new_curve = tuple((20.0 + i, 30.0 + i * 4) for i in range(16))
        s = p.decode_settings(r)
        s = p.with_controller(s, 2, mode=p.MODE_CURVE, curve=new_curve)
        s = p.with_fan(s, 2, min_percent=12.5, max_percent=95.0)
        d = p.decode_settings(p.encode_settings(s, r))
        self.assertEqual(d.controllers[2].curve, new_curve)
        self.assertEqual(d.controllers[2].mode, p.MODE_CURVE)
        self.assertEqual((d.fans[2].min_percent, d.fans[2].max_percent), (12.5, 95.0))

    def test_led_bytes_are_never_changed(self):
        r = load("settings_live.bin")
        s = p.decode_settings(r)
        fake_led = replace(s.leds[0], raw=bytes(70), mode=0)
        s = replace(s, leds=(fake_led,) + s.leds[1:])
        out = p.encode_settings(s, r)
        self.assertEqual(out[397:397 + 560], r[397:397 + 560])

    def test_with_strip(self):
        s = p.decode_settings(load("settings_live.bin"))
        off = p.with_strip(s, enabled=False)
        self.assertFalse(off.strip_enabled)
        self.assertEqual(off.strip_brightness, 218)
        on = p.with_strip(off, enabled=True, brightness=40)
        self.assertTrue(on.strip_enabled)
        self.assertEqual(on.strip_brightness, 40)
        self.assertEqual(on.strip_flags, s.strip_flags)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.st = p.decode_status(load("status.bin"))

    def test_values_match_hwmon(self):
        # hwmon at capture time: temp1 31720, fan1..4 3024/362/363/503, fan5 (flow) 1092 dL/h,
        # power2..4 380000/840000/220000 uW, in0 12000 mV, curr2 32 mA
        self.assertEqual(self.st.firmware, 1033)
        self.assertEqual(self.st.temps, (31.72, None, None, None))
        self.assertEqual(self.st.flow_lph, 109.2)
        self.assertEqual(self.st.vcc12, 12.0)
        self.assertEqual([f.rpm for f in self.st.fans], [3024, 362, 363, 503])
        self.assertEqual([f.power_w for f in self.st.fans], [0.0, 0.38, 0.84, 0.22])
        self.assertEqual(self.st.fans[1].current_ma, 32)
        self.assertEqual([f.percent for f in self.st.fans], [34.86, 5.0, 5.0, 9.69])
        self.assertEqual(self.st.soft_sensors, (None,) * 16)
        self.assertEqual(self.st.profile, 1)

    def test_rejects_non_status(self):
        with self.assertRaises(p.ProtocolError):
            p.decode_status(load("status.bin")[:100])

    def test_fixture_serial_is_scrubbed(self):
        self.assertEqual(load("status.bin")[3:7], b"\x00\x00\x00\x00")

    def test_scrub_status_serial(self):
        raw = bytearray(load("status.bin"))
        raw[3:7] = b"\x01\x02\x03\x04"
        self.assertEqual(p.scrub_status_serial(bytes(raw))[3:7], b"\x00" * 4)


class NamesTest(unittest.TestCase):
    def test_decode_names(self):
        n = p.decode_names(load("names.bin"))
        self.assertEqual(n.fans, ("Pumpe", "140mm Radiator", "420mm Radiator", "Fan 4"))
        self.assertEqual(n.leds[0], "LED Controller 1")
        self.assertEqual(n.temps[0], "Wasser Temp")
        self.assertEqual(n.flow, "Flow")
        self.assertEqual(n.strip, "Strip")


if __name__ == "__main__":
    unittest.main()
