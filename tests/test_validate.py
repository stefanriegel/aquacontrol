import unittest

from aquacontrol import protocol as p
from aquacontrol.validate import ValidationError, check
from tests.fixtures import load

PUMP_FLOOR = {0: 25.0}


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.old = p.decode_settings(load("settings_live.bin"))

    def ok(self, new):
        check(self.old, new, PUMP_FLOOR)

    def bad(self, new, fragment):
        with self.assertRaises(ValidationError) as cm:
            check(self.old, new, PUMP_FLOOR)
        self.assertIn(fragment, str(cm.exception))

    def test_unchanged_is_ok(self):
        self.ok(self.old)

    def test_target_temperature(self):
        self.ok(p.with_controller(self.old, 1, target_c=40.0))
        self.bad(p.with_controller(self.old, 1, target_c=70.0), "Zieltemperatur")
        self.bad(p.with_controller(self.old, 1, target_c=float("nan")), "ungültige Zahl")

    def test_modes(self):
        self.ok(p.with_controller(self.old, 3, mode=p.MODE_CURVE))
        self.bad(p.with_controller(self.old, 3, mode=p.MODE_FOLLOW), "Modus")

    def test_follow_mode_already_set_stays_allowed(self):
        old = p.with_controller(self.old, 2, mode=p.MODE_FOLLOW)
        check(old, p.with_controller(old, 2, target_c=36.0), PUMP_FLOOR)

    def test_curve_must_rise_strictly(self):
        good = tuple((20.0 + i, 10.0 + i) for i in range(16))
        self.ok(p.with_controller(self.old, 2, curve=good))
        flat = tuple((20.0 + (i if i != 5 else 4), 10.0) for i in range(16))
        self.bad(p.with_controller(self.old, 2, curve=flat), "streng steigen")

    def test_curve_percent_and_length(self):
        over = tuple((20.0 + i, 101.0 if i == 15 else 10.0) for i in range(16))
        self.bad(p.with_controller(self.old, 2, curve=over), "zwischen 0 und 100")
        short = tuple((20.0 + i, 10.0) for i in range(15))
        self.bad(p.with_controller(self.old, 2, curve=short), "16 Punkte")

    def test_pump_curve_points_below_floor_are_allowed(self):
        # the device minimum (28 %) clamps them; only the minimum itself is guarded
        curve = tuple((20.0 + i, 0.0) for i in range(16))
        self.ok(p.with_controller(self.old, 0, curve=curve))

    def test_pump_minimum_floor(self):
        self.ok(p.with_fan(self.old, 0, min_percent=25.0))
        self.bad(p.with_fan(self.old, 0, min_percent=10.0), "Minimum 10.0 % unter erlaubtem 25.0 %")

    def test_pump_fixed_floor(self):
        self.bad(p.with_controller(self.old, 0, mode=p.MODE_FIXED, fixed_percent=5.0), "Fest-Wert")
        self.ok(p.with_controller(self.old, 0, mode=p.MODE_FIXED, fixed_percent=60.0))

    def test_switching_pump_to_fixed_checks_stored_value(self):
        old = p.with_controller(self.old, 0, fixed_percent=5.0)
        with self.assertRaises(ValidationError):
            check(old, p.with_controller(old, 0, mode=p.MODE_FIXED), PUMP_FLOOR)

    def test_min_below_max(self):
        self.bad(p.with_fan(self.old, 1, min_percent=60.0, max_percent=50.0), "kleiner als Maximum")

    def test_sensor_choice(self):
        self.ok(p.with_controller(self.old, 1, sensor=2))
        self.bad(p.with_controller(self.old, 1, sensor=7), "Sensor")

    def test_strip(self):
        self.ok(p.with_strip(self.old, enabled=False, brightness=0))
        self.bad(p.with_strip(self.old, brightness=300), "Helligkeit")
        from dataclasses import replace
        self.bad(replace(self.old, strip_flags=0x0100), "An/Aus-Bit")

    def test_read_only_fields(self):
        from dataclasses import replace
        self.bad(p.with_controller(self.old, 1, pid=(0, 0, 0, 0, 0, 0)), "PID")
        self.bad(p.with_fan(self.old, 1, fallback_percent=50.0), "Fallback")
        self.bad(replace(self.old, profile=2), "Profilnummer")


if __name__ == "__main__":
    unittest.main()
