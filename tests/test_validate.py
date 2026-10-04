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


class ValidateLedTest(unittest.TestCase):
    """LED rules (spec 2/3): C1 is a Farbschalter on source 0 (range 20-70, thresholds 35/45), C2 a static colour."""

    def setUp(self):
        self.old = p.decode_settings(load("settings_live.bin"))

    def ok(self, new, old=None):
        check(old or self.old, new, PUMP_FLOOR)

    def bad(self, new, fragment, old=None):
        with self.assertRaises(ValidationError) as cm:
            check(old or self.old, new, PUMP_FLOOR)
        self.assertIn(fragment, str(cm.exception))

    def led(self, index=0, **changes):
        return p.with_led(self.old, index, **changes)

    def test_thresholds(self):
        self.ok(self.led(thresholds=(38, 45)))
        self.ok(self.led(thresholds=(20, 70)))  # the range limits themselves are fine
        self.bad(self.led(thresholds=(45, 45)), "streng steigen")
        self.bad(self.led(thresholds=(46, 45)), "streng steigen")
        self.bad(self.led(thresholds=(19, 45)), "zwischen 20 und 70")
        self.bad(self.led(thresholds=(35, 71)), "zwischen 20 und 70")
        self.bad(self.led(thresholds=(35.5, 45)), "ganze Zahl")
        self.bad(self.led(thresholds=(True, 45)), "ganze Zahl")

    def test_threshold_count_is_1_to_5(self):
        self.ok(self.led(thresholds=(40,)))
        self.ok(self.led(thresholds=(25, 30, 40, 50, 60)))
        self.bad(self.led(thresholds=()), "1 bis 5")
        self.bad(self.led(thresholds=(21, 22, 23, 24, 25, 26)), "1 bis 5")

    def test_growing_without_new_colour_copies_the_last_one(self):
        self.ok(self.led(thresholds=(35, 45, 55)))

    def test_colours(self):
        self.ok(self.led(colors=((0x03ff, 255, 255), (255, 255, 255), (0, 255, 255))))
        self.ok(self.led(colors=((1535, 0, 0), (255, 255, 255), (0, 255, 255))))
        self.bad(self.led(colors=((1536, 255, 255), (255, 255, 255), (0, 255, 255))), "Farbton")
        self.bad(self.led(colors=((-1, 255, 255), (255, 255, 255), (0, 255, 255))), "Farbton")
        self.bad(self.led(colors=((0, 256, 255), (255, 255, 255), (0, 255, 255))), "Sättigung")
        self.bad(self.led(colors=((0, 255, 300), (255, 255, 255), (0, 255, 255))), "Helligkeit")
        self.bad(self.led(colors=((0.5, 255, 255), (255, 255, 255), (0, 255, 255))), "Farbton")

    def test_colours_beyond_the_used_ones_are_not_changeable(self):
        palette = self.old.leds[0].palette[:3] + ((1, 2, 3),) + self.old.leds[0].palette[4:]
        self.bad(self.led(palette=palette), "Palette")

    def test_flags(self):
        self.ok(self.led(flags=0x0001))
        self.ok(self.led(flags=0x0003 | 0x4000))
        self.bad(self.led(flags=0x0004), "Schalter")
        self.bad(self.led(flags=0x8000), "Schalter")

    def test_source(self):
        self.ok(self.led(source=3))
        self.bad(self.led(source=4), "Datenquelle")  # flow
        self.bad(self.led(source=-1), "Datenquelle")
        self.bad(self.led(source=True), "Datenquelle")

    def test_display_only_source_stays_as_it_is(self):
        old = p.with_led(self.old, 0, source=4)
        self.ok(p.with_led(old, 0, thresholds=(38, 45)), old)
        self.bad(p.with_led(old, 0, source=7), "Datenquelle", old)

    def test_source_can_only_change_when_the_current_one_is_a_temperature_sensor(self):
        for old_source in (4, 5, -1):
            with self.subTest(old_source=old_source):
                old = p.with_led(self.old, 0, source=old_source)
                self.bad(p.with_led(old, 0, source=0), "nur in der Aquasuite", old)
                self.bad(p.with_led(old, 0, source=3), "nur in der Aquasuite", old)
                self.ok(p.with_led(old, 0, source=old_source), old)  # unchanged is fine
        self.ok(self.led(source=2))  # temperature -> other temperature

    def test_static_colour_may_not_gain_brightness_by_source(self):
        self.bad(self.led(1, flags=self.old.leds[1].flags | p.LED_FLAG_BRIGHTNESS), "Helligkeit nach Datenquelle")
        old = p.with_led(self.old, 1, flags=self.old.leds[1].flags | p.LED_FLAG_BRIGHTNESS)
        self.ok(p.with_led(old, 1, flags=old.leds[1].flags & ~p.LED_FLAG_BRIGHTNESS), old)  # can be switched off
        self.ok(p.with_led(old, 1, colors=((1, 2, 3),)), old)  # stays set
        self.ok(p.with_led(self.old, 1, flags=self.old.leds[1].flags | p.LED_FLAG_BLINK))

    def test_static_colour(self):
        self.ok(self.led(1, colors=((0x03ff, 255, 255),)))
        self.ok(self.led(1, flags=0x0002))
        self.bad(self.led(1, colors=((2000, 255, 255),)), "Farbton")
        self.bad(self.led(1, thresholds=(30,)), "statische")
        self.bad(self.led(1, source=2), "statische")
        self.bad(self.led(1, palette=self.old.leds[1].palette[:1] + ((9, 9, 9),) + self.old.leds[1].palette[2:]),
                 "Palette")

    def test_nothing_else_may_change(self):
        for field, value in (("led_start", 3), ("led_count", 1), ("mode", 1), ("binding1", (0, 100, 0, 255))):
            with self.subTest(field=field):
                self.bad(self.led(**{field: value}), "nicht änderbar")
        self.bad(self.led(values=(2, 5, 35, 45, 100, 100, 100, 70, 0, 0, 0, 0)), "nicht änderbar")  # values[1]
        self.bad(self.led(values=(2, 0, 35, 45, 100, 100, 100, 71, 0, 0, 0, 0)), "nicht änderbar")  # beyond n

    def test_unused_controllers_are_immutable(self):
        for i in range(2, 8):
            with self.subTest(controller=i + 1):
                self.bad(self.led(i, flags=0x0001), "nicht änderbar")
                self.bad(self.led(i, colors=((0, 255, 255),)), "nicht änderbar")
                self.bad(self.led(i, source=1), "nicht änderbar")

    def test_unknown_modes_are_immutable(self):
        old = p.with_led(self.old, 4, mode=0x20)
        self.bad(p.with_led(old, 4, flags=1), "nicht änderbar", old)

    def test_existing_odd_values_do_not_block_other_edits(self):
        old = p.with_led(self.old, 0, values=(2, 0, 10, 99, 100, 100, 100, 70, 0, 0, 0, 0))
        self.ok(p.with_led(old, 0, flags=1), old)  # thresholds outside the range stay as they are
        self.bad(p.with_led(old, 0, thresholds=(10, 60)), "zwischen 20 und 70", old)


if __name__ == "__main__":
    unittest.main()
