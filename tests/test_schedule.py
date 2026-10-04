import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

from aquacontrol import protocol as p
from aquacontrol.config import AppConfig
from aquacontrol.device import Device, DeviceError
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

    def test_rejects_trailing_newline_in_time(self):
        with self.assertRaises(ScheduleError):
            parse_rules([{"time": "09:00\n", "on": True}])


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

    def test_default_config_makes_no_write_at_night(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = Scheduler(self.dev, AppConfig(Path(tmp, "config.json")).rules, clock=lambda: self.now)
            self.assertEqual(self.now, at(5, 3, 0))
            self.assertIsNone(sched.tick())
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.fake.commits, 0)

    def test_override_applies_without_rules(self):
        sched = Scheduler(self.dev, lambda: [], clock=lambda: self.now)
        sched.set_override(False, 40)
        self.assertTrue(sched.tick().changed)
        s = self.dev.read_settings()
        self.assertEqual((s.strip_enabled, s.strip_brightness), (False, 40))

    def test_status(self):
        st = self.sched.status()
        self.assertEqual(st["desired"], {"on": False, "brightness": None})
        self.assertEqual(st["next_change"], "2026-10-05T09:00")


class BackoffTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransport(load("settings_live.bin"))
        self.dev = Device(self.fake, None, make_check({0: 25.0}))
        self.now = at(5, 3, 0)
        self.rules = NIGHT
        self.sched = Scheduler(self.dev, lambda: self.rules, clock=lambda: self.now)
        self.fake.ignore_writes = True  # every write attempt fails verification (and is rolled back)

    def attempt(self):
        """Tick once; returns how many writes reached the device (2 per failed attempt: write + rollback)."""
        before = len(self.fake.writes)
        try:
            self.sched.tick()
        except DeviceError:
            pass
        return len(self.fake.writes) - before

    def test_failure_pauses_writes_for_5_minutes(self):
        self.assertEqual(self.attempt(), 2)
        self.assertIn("Schreiben fehlgeschlagen", self.sched.last_error)
        self.now += timedelta(minutes=4, seconds=59)
        self.assertEqual(self.attempt(), 0)
        self.assertIsNone(self.sched.tick())
        self.assertIn("Schreiben fehlgeschlagen", self.sched.status()["last_error"])  # kept while backing off
        self.now += timedelta(seconds=1)
        self.assertEqual(self.attempt(), 2)

    def test_backoff_grows_5_15_60_and_stays_at_60(self):
        for minutes in (5, 15, 60, 60):
            self.assertEqual(self.attempt(), 2)
            self.now += timedelta(minutes=minutes) - timedelta(seconds=1)
            self.assertEqual(self.attempt(), 0, minutes)
            self.now += timedelta(seconds=1)

    def test_rules_change_resets_backoff(self):
        self.assertEqual(self.attempt(), 2)
        self.assertEqual(self.attempt(), 0)
        self.rules = parse_rules([{"time": "01:00", "target": "strip", "on": False, "brightness": 50}])
        self.assertEqual(self.attempt(), 2)

    def test_override_resets_backoff(self):
        self.assertEqual(self.attempt(), 2)
        self.assertEqual(self.attempt(), 0)
        self.sched.set_override(False)  # same state as the rule, but a manual change: tried right away
        self.assertEqual(self.attempt(), 2)

    def test_success_resets_failure_count(self):
        self.assertEqual(self.attempt(), 2)           # 1st failure -> 5 min
        self.now += timedelta(minutes=5)
        self.assertEqual(self.attempt(), 2)           # 2nd failure -> 15 min
        self.now += timedelta(minutes=15)
        self.fake.ignore_writes = False
        self.assertEqual(self.attempt(), 1)           # success
        self.assertIsNone(self.sched.last_error)
        self.now = at(5, 9, 0)
        self.fake.ignore_writes = True
        self.assertEqual(self.attempt(), 2)           # fails again: back to 5 min, not 60
        self.now += timedelta(minutes=5)
        self.assertEqual(self.attempt(), 2)

    def test_run_survives_failure_and_does_not_hammer(self):
        class Stop(threading.Event):  # wait() returns at once; stops after 3 rounds
            rounds = 0

            def wait(self, timeout=None):
                self.rounds += 1
                if self.rounds >= 3:
                    self.set()
                return self.is_set()

        with self.assertLogs("aquacontrol.schedule", "WARNING"):
            self.sched.run(Stop())
        self.assertEqual(len(self.fake.writes), 2)  # one failed attempt, then paused
        self.assertIn("Schreiben fehlgeschlagen", self.sched.last_error)


if __name__ == "__main__":
    unittest.main()
