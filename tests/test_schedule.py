import unittest
from datetime import datetime, time

from aquacontrol import protocol as p
from aquacontrol.device import Device
from aquacontrol.fake import FakeTransport
from aquacontrol.schedule import (Override, Rule, ScheduleError, Scheduler, StripState, desired_state,
                                  next_change, parse_rules, rules_to_json)
from aquacontrol.validate import make_check
from tests.fixtures import load

NIGHT = parse_rules([
    {"time": "01:00", "target": "strip", "on": False},
    {"time": "09:00", "target": "strip", "on": True, "brightness": 218},
])


def at(day, hh, mm):  # 2026-10-05 is a Monday
    return datetime(2026, 10, day, hh, mm)


class ParseTest(unittest.TestCase):
    def test_parse_and_back(self):
        raw = [{"time": "01:00", "target": "strip", "on": False},
               {"time": "09:30", "target": "strip", "on": True, "brightness": 100, "days": [5, 6]}]
        rules = parse_rules(raw)
        self.assertEqual(rules[1], Rule(time(9, 30), True, 100, frozenset({5, 6})))
        self.assertEqual(rules_to_json(rules), raw)

    def test_rejects_invalid(self):
        bad = [
            "x", [{"time": "25:00", "on": True}], [{"time": "9:00", "on": True}],
            [{"time": "09:00", "on": "yes"}], [{"time": "09:00", "on": True, "brightness": 256}],
            [{"time": "09:00", "on": True, "days": []}], [{"time": "09:00", "on": True, "days": [7]}],
            [{"time": "09:00", "on": True, "target": "led:1"}], [[]],
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ScheduleError):
                parse_rules(raw)


class DesiredStateTest(unittest.TestCase):
    def test_daytime_on(self):
        self.assertEqual(desired_state(NIGHT, at(5, 12, 0)), StripState(True, 218))

    def test_night_off(self):
        self.assertEqual(desired_state(NIGHT, at(5, 3, 0)), StripState(False, None))

    def test_just_after_midnight_uses_previous_day(self):
        self.assertEqual(desired_state(NIGHT, at(5, 0, 30)), StripState(True, 218))

    def test_exact_rule_time_applies(self):
        self.assertEqual(desired_state(NIGHT, at(5, 1, 0)), StripState(False, None))

    def test_no_rules(self):
        self.assertIsNone(desired_state([], at(5, 12, 0)))

    def test_weekday_filter(self):
        rules = parse_rules([{"time": "08:00", "on": True, "days": [5, 6]},   # weekend
                             {"time": "07:00", "on": False, "days": [0]}])    # Monday
        # Monday 12:00: last matching rule is Monday 07:00 (off)
        self.assertEqual(desired_state(rules, at(5, 12, 0)).on, False)
        # Sunday 2026-10-11 09:00: weekend rule at 08:00 (on)
        self.assertEqual(desired_state(rules, datetime(2026, 10, 11, 9, 0)).on, True)

    def test_override_until_next_rule(self):
        ov = Override(StripState(True), at(5, 2, 0))  # turned on at night by hand
        self.assertEqual(desired_state(NIGHT, at(5, 3, 0), ov), StripState(True))
        self.assertEqual(desired_state(NIGHT, at(5, 9, 0), ov), StripState(True, 218))
        self.assertEqual(desired_state(NIGHT, at(6, 1, 30), ov), StripState(False, None))

    def test_next_change(self):
        self.assertEqual(next_change(NIGHT, at(5, 12, 0)), at(6, 1, 0))
        self.assertIsNone(next_change([], at(5, 12, 0)))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransport(load("settings_live.bin"))
        self.dev = Device(self.fake, None, make_check({0: 25.0}))
        self.now = at(5, 3, 0)
        self.sched = Scheduler(self.dev, lambda: NIGHT, clock=lambda: self.now)

    def test_catch_up_turns_off_and_writes_once(self):
        result = self.sched.tick()
        self.assertTrue(result.changed)
        self.assertFalse(self.dev.read_settings().strip_enabled)
        self.assertEqual(self.dev.read_settings().strip_brightness, 218)
        self.assertFalse(self.sched.tick().changed)  # second tick: nothing to do
        self.assertEqual(len(self.fake.writes), 1)

    def test_morning_turns_on_with_brightness(self):
        self.sched.tick()
        self.now = at(5, 9, 0)
        self.sched.tick()
        s = self.dev.read_settings()
        self.assertTrue(s.strip_enabled)
        self.assertEqual(s.strip_brightness, 218)

    def test_override(self):
        self.sched.tick()
        self.sched.set_override(True)
        self.sched.tick()
        self.assertTrue(self.dev.read_settings().strip_enabled)
        self.assertTrue(self.sched.status()["override_active"])

    def test_no_rules_no_writes(self):
        sched = Scheduler(self.dev, lambda: [], clock=lambda: self.now)
        self.assertIsNone(sched.tick())
        self.assertEqual(self.fake.writes, [])

    def test_status(self):
        st = self.sched.status()
        self.assertEqual(st["desired"], {"on": False, "brightness": None})
        self.assertEqual(st["next_change"], "2026-10-05T09:00")


if __name__ == "__main__":
    unittest.main()
