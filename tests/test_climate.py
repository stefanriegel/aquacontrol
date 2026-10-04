import threading
import unittest

from types import SimpleNamespace

from aquacontrol.climate import (BACKOFF_S, DEFAULT_CLIMATE, INTERVAL_S, ClimateController, make_client_factory,
                                 merge_climate, parse_climate_config)
from aquacontrol.ha import HAClient, HAError

E = "climate.panasonic_ac_panasonic_ac"
H = "select.panasonic_ac_panasonic_ac_horizontal_swing_mode"
V = "select.panasonic_ac_panasonic_ac_vertical_swing_mode"
MIN = 60

ON_CALLS = [
    ("climate", "set_hvac_mode", {"entity_id": E, "hvac_mode": "cool"}),
    ("climate", "set_temperature", {"entity_id": E, "temperature": 20.0}),
    ("climate", "set_preset_mode", {"entity_id": E, "preset_mode": "Quiet"}),
    ("climate", "set_fan_mode", {"entity_id": E, "fan_mode": "Automatic"}),
    ("select", "select_option", {"entity_id": H, "option": "left"}),
    ("select", "select_option", {"entity_id": V, "option": "down_center"}),
]


class FakeClient:
    """Stands in for HAClient: records every call, simulates the AC, can fail on demand."""

    def __init__(self):
        self.log = []
        self.failing = set()      # "get", "domain.service", or "*"
        self.ignore_on = False    # the AC reports "off" although it was told to switch on (cloud lag)
        self.clock = lambda: 0.0
        self.stamps = []          # (what, time) of every call: "get", "domain.service"
        self.lag = 0              # state changes become visible only after this many get_state calls
        self._queued = []         # [remaining reads, change] pairs
        self.states = {
            E: {"state": "off", "attributes": {"temperature": 24.0, "preset_mode": "Normal", "fan_mode": "Automatic"}},
            H: {"state": "auto", "attributes": {}},
            V: {"state": "auto", "attributes": {}},
        }

    def _maybe_fail(self, key):
        if key in self.failing or "*" in self.failing:
            raise HAError("Home Assistant nicht erreichbar: Test")

    def get_state(self, entity_id):
        self.log.append(("get", entity_id))
        self.stamps.append(("get", self.clock()))
        self._maybe_fail("get")
        for item in list(self._queued):
            item[0] -= 1
            if item[0] < 0:
                self._queued.remove(item)
                item[1]()
        return {"entity_id": entity_id, **{k: (dict(v) if isinstance(v, dict) else v)
                                           for k, v in self.states[entity_id].items()}}

    def call(self, domain, service, data):
        self.log.append(("call", domain, service, dict(data)))
        self.stamps.append((f"{domain}.{service}", self.clock()))
        self._maybe_fail(f"{domain}.{service}")
        if self.lag:
            self._queued.append([self.lag, lambda: self._apply(service, data)])
        else:
            self._apply(service, data)

    def _apply(self, service, data):
        ent = self.states[data["entity_id"]]
        if service == "set_hvac_mode" and not self.ignore_on:
            ent["state"] = data["hvac_mode"]
        elif service == "set_temperature":
            ent["attributes"]["temperature"] = data["temperature"]
        elif service == "set_preset_mode":
            ent["attributes"]["preset_mode"] = data["preset_mode"]
        elif service == "set_fan_mode":
            ent["attributes"]["fan_mode"] = data["fan_mode"]
        elif service == "select_option":
            ent["state"] = data["option"]
        elif service == "turn_off":
            ent["state"] = "off"

    @property
    def calls(self):
        return [(d, s, data) for kind, *rest in self.log if kind == "call" for d, s, data in [rest]]

    @property
    def gets(self):
        return [e for kind, e, *_ in self.log if kind == "get"]

    def count(self, domain, service):
        return len([c for c in self.calls if c[:2] == (domain, service)])

    def ac(self, state=None, **attrs):
        if state is not None:
            self.states[E]["state"] = state
        self.states[E]["attributes"].update(attrs)


def snap(water=41.0, fans=(90.0, 90.0, 90.0, 90.0), online=True):
    if not online:
        return {"online": False, "updated": 1.0, "status": {"temps": [water, None, None, None],
                                                           "fans": [{"percent": p} for p in fans]}, "sensors": []}
    return {"online": True, "updated": 1.0, "status": {"temps": [water, None, None, None],
                                                       "fans": [{"percent": p} for p in fans]}, "sensors": []}


def hot(**kw):
    return snap(41.0, (30.0, 90.0, 90.0, 30.0), **kw)


def cool(water=35.0):
    return snap(water, (30.0, 40.0, 40.0, 30.0))


class Rig:
    def __init__(self, **patch):
        self.now = 1_000_000.0
        self.client = FakeClient()
        self.client_available = True
        self.factory_calls = 0
        self.cfg = parse_climate_config(merge_climate(DEFAULT_CLIMATE,
                                                      {"enabled": True, "ha_url": "http://ha.example:8123", **patch}))
        self.snapshot = hot()
        self.client.clock = lambda: self.now
        self.sleeps = []
        self.ctrl = ClimateController(lambda: self.snapshot, self.factory, lambda: self.cfg, clock=lambda: self.now,
                                      sleep=self.sleeps.append)

    def attempts(self, what):
        """Minutes (relative to the first one) at which `what` was tried."""
        times = [t for w, t in self.client.stamps if w == what]
        return [round((t - times[0]) / MIN, 1) for t in times]

    def factory(self):
        self.factory_calls += 1
        return self.client if self.client_available else None

    def set_cfg(self, **patch):
        self.cfg = parse_climate_config(merge_climate(self.cfg.to_json(), patch))

    def tick(self):
        self.ctrl.tick()

    def set(self, snapshot):
        """Change the QUADRO reading and run a cycle right away."""
        self.snapshot = snapshot
        self.ctrl.tick()

    def advance(self, minutes):
        """Tick every 30 s until `minutes` have passed (the last tick lands exactly on the target)."""
        end = self.now + minutes * MIN
        while self.now < end - 1e-6:
            self.now = min(self.now + INTERVAL_S, end)
            self.ctrl.tick()

    def status(self):
        return self.ctrl.status()

    def state(self):
        return self.ctrl.status()["state"]

    def bring_on(self):
        self.snapshot = hot()
        self.tick()
        self.advance(5)
        assert self.state() == "owned", self.status()
        self.client.log.clear()


class TurnOnTest(unittest.TestCase):
    def test_on_only_after_five_continuous_minutes(self):
        r = Rig()
        r.tick()
        self.assertEqual(r.state(), "arming")
        r.advance(4.5)
        self.assertEqual(r.client.calls, [])
        self.assertEqual(r.state(), "arming")
        r.advance(0.5)
        self.assertEqual(r.client.calls, ON_CALLS)
        self.assertEqual(r.state(), "owned")

    def test_interruption_resets_the_clock(self):
        r = Rig()
        r.tick()
        r.advance(4)
        r.snapshot = snap(39.0, (30.0, 90.0, 90.0, 30.0))  # below the on threshold for one cycle
        r.advance(0.5)
        self.assertIsNone(r.status()["arming_since"])
        r.set(hot())
        r.advance(4.5)
        self.assertEqual(r.client.calls, [])  # 4.5 min since the restart, not 9
        r.advance(0.5)
        self.assertEqual(r.client.calls, ON_CALLS)

    def test_threshold_is_inclusive(self):
        r = Rig()
        r.snapshot = snap(40.0, (0, 85.0, 85.0, 0))
        r.tick()
        r.advance(5)
        self.assertEqual(r.state(), "owned")

    def test_water_just_below_threshold_never_switches_on(self):
        r = Rig()
        r.snapshot = snap(39.9, (0, 100.0, 100.0, 0))
        r.tick()
        r.advance(60)
        self.assertEqual(r.client.log, [])
        self.assertEqual(r.state(), "idle")

    def test_every_fan_channel_must_reach_the_threshold(self):
        for fans in ((0, 84.9, 90, 0), (0, 90, 84.9, 0), (0, None, 90, 0), (0, 90, None, 0)):
            with self.subTest(fans=fans):
                r = Rig()
                r.snapshot = snap(45.0, fans)
                r.tick()
                r.advance(30)
                self.assertEqual(r.client.log, [])

    def test_fan_channels_are_configurable(self):
        r = Rig(on={"fan_channels": [1]})
        r.snapshot = snap(45.0, (90.0, 10.0, 10.0, 10.0))
        r.tick()
        r.advance(5)
        self.assertEqual(r.state(), "owned")

    def test_other_channels_are_ignored(self):
        r = Rig()
        r.snapshot = snap(45.0, (0.0, 90.0, 90.0, 0.0))  # pump and case fan idle: irrelevant
        r.tick()
        r.advance(5)
        self.assertEqual(r.state(), "owned")

    def test_no_ha_traffic_while_conditions_are_not_met(self):
        r = Rig()
        r.snapshot = cool(37.0)
        r.tick()
        r.advance(30)
        self.assertEqual(r.client.log, [])

    def test_no_ha_traffic_while_arming(self):
        r = Rig()
        r.tick()
        r.advance(4)
        self.assertEqual(r.client.log, [])

    def test_ha_state_is_read_before_switching_on(self):
        r = Rig()
        r.tick()
        r.advance(5)
        self.assertEqual(r.client.log[0], ("get", E))

    def test_manual_running_ac_is_left_alone(self):
        for running in ("cool", "heat", "fan_only", "dry", "heat_cool"):
            with self.subTest(running=running):
                r = Rig()
                r.client.ac(running)
                r.tick()
                r.advance(30)
                self.assertEqual(r.client.calls, [])
                self.assertEqual(r.state(), "idle")
                self.assertIn("Handbetrieb", r.status()["reason"])

    def test_ac_unavailable_in_ha_is_never_switched_on(self):
        for st in ("unavailable", "unknown"):
            with self.subTest(st=st):
                r = Rig()
                r.client.ac(st)
                r.tick()
                r.advance(10)
                self.assertEqual(r.client.calls, [])

    def test_switches_on_once_manual_ac_is_off_again(self):
        r = Rig()
        r.client.ac("cool")
        r.tick()
        r.advance(10)
        self.assertEqual(r.client.calls, [])
        r.client.ac("off")
        r.advance(5.5)  # a manually running AC is looked at again only every on.minutes
        self.assertEqual(r.client.calls, ON_CALLS)

    def test_manual_ac_is_not_polled_every_cycle(self):
        r = Rig()
        r.client.ac("cool")
        r.tick()
        r.advance(5)
        n = len(r.client.gets)
        r.advance(4.5)
        self.assertEqual(len(r.client.gets), n)
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), n + 1)

    def test_offline_quadro_never_switches_on(self):
        r = Rig()
        r.snapshot = hot(online=False)
        r.tick()
        r.advance(60)
        self.assertEqual(r.client.log, [])
        self.assertIn("offline", r.status()["reason"])

    def test_unknown_water_never_switches_on(self):
        r = Rig()
        r.snapshot = snap(None, (0, 90.0, 90.0, 0))
        r.tick()
        r.advance(60)
        self.assertEqual(r.client.log, [])
        r.snapshot = {"online": False, "updated": None, "status": None, "sensors": []}
        r.advance(10)
        self.assertEqual(r.client.log, [])

    def test_going_offline_during_arming_resets_the_clock(self):
        r = Rig()
        r.tick()
        r.advance(4)
        r.snapshot = hot(online=False)
        r.advance(0.5)
        r.snapshot = hot()
        r.advance(4.5)
        self.assertEqual(r.client.calls, [])

    def test_no_client_configured(self):
        r = Rig()
        r.client_available = False
        r.tick()
        r.advance(10)
        self.assertEqual(r.client.log, [])
        self.assertEqual(r.state(), "idle")
        self.assertIn("eingerichtet", r.status()["reason"])

    def test_on_values_come_from_the_config(self):
        r = Rig(ac={"hvac_mode": "dry", "temperature": 22.5, "preset": "Powerful", "fan_mode": "3",
                    "horizontal": "center", "vertical": "up"})
        r.tick()
        r.advance(5)
        self.assertEqual([c[2] for c in r.client.calls], [
            {"entity_id": E, "hvac_mode": "dry"}, {"entity_id": E, "temperature": 22.5},
            {"entity_id": E, "preset_mode": "Powerful"}, {"entity_id": E, "fan_mode": "3"},
            {"entity_id": H, "option": "center"}, {"entity_id": V, "option": "up"}])


class OwnershipTest(unittest.TestCase):
    def test_owned_state_has_a_fingerprint_and_timers(self):
        r = Rig()
        r.tick()
        t0 = r.now
        r.advance(5)
        st = r.status()
        self.assertEqual(st["state"], "owned")
        self.assertEqual(st["owned_since"], r.now)
        self.assertEqual(st["arming_since"], None)
        self.assertIn("Klimaanlage eingeschaltet", st["events"][0]["message"])
        self.assertGreater(st["owned_since"], t0)

    def test_ha_is_polled_every_cycle_while_owned(self):
        r = Rig()
        r.bring_on()
        r.advance(2)
        self.assertEqual(len(r.client.gets), 4)
        self.assertEqual(r.client.calls, [])

    def test_external_off_hands_over(self):
        r = Rig()
        r.bring_on()
        r.client.ac("off")
        r.advance(0.5)
        self.assertEqual(r.state(), "cooldown")
        self.assertIn("Handbetrieb übernommen", r.status()["events"][0]["message"])
        self.assertEqual(r.client.calls, [])

    def test_external_changes_hand_over(self):
        for change in ({"state": "heat"}, {"temperature": 22.0}, {"preset_mode": "Powerful"}, {"state": "fan_only"}):
            with self.subTest(change=change):
                r = Rig()
                r.bring_on()
                r.client.ac(change.get("state"), **{k: v for k, v in change.items() if k != "state"})
                r.advance(0.5)
                self.assertIn("Handbetrieb übernommen", r.status()["events"][0]["message"])
                self.assertIsNone(r.status()["owned_since"])

    def test_after_hand_over_nothing_is_ever_switched_off(self):
        r = Rig()
        r.bring_on()
        r.client.ac("cool", temperature=23.0)
        r.snapshot = cool(30.0)
        r.advance(240)
        self.assertEqual(r.client.count("climate", "turn_off"), 0)
        self.assertEqual(r.client.calls, [])

    def test_louvre_selects_are_not_part_of_the_fingerprint(self):
        r = Rig()
        r.bring_on()
        r.client.states[H]["state"] = "right"
        r.client.states[V]["state"] = "auto"
        r.advance(1)
        self.assertEqual(r.state(), "owned")

    def test_fingerprint_compares_numbers_not_types(self):
        r = Rig()
        r.bring_on()
        r.client.ac(temperature=20)  # int instead of float
        r.advance(1)
        self.assertEqual(r.state(), "owned")

    def test_hand_over_blocks_switching_on_for_the_lockout(self):
        r = Rig(max_switches_per_hour=10)
        r.bring_on()
        r.client.ac("off")                      # somebody switched it off by hand while the water is still hot
        r.advance(0.5)
        r.client.log.clear()
        r.advance(14)
        self.assertEqual(r.client.calls, [])
        self.assertEqual(r.state(), "cooldown")
        r.advance(2)
        self.assertEqual(r.client.calls, ON_CALLS)

    def test_ha_unavailable_while_owned_keeps_ownership(self):
        r = Rig()
        r.bring_on()
        r.client.ac("unavailable")
        r.snapshot = cool()
        r.advance(60)
        self.assertEqual(r.client.calls, [])
        self.assertEqual(r.state(), "owned")
        r.set(hot())
        r.client.ac("cool")
        r.advance(1)
        self.assertEqual(r.state(), "owned")  # still the same fingerprint

    def test_restart_forgets_ownership(self):
        r = Rig()
        r.bring_on()
        fresh = ClimateController(lambda: cool(), r.factory, lambda: r.cfg, clock=lambda: r.now)
        for _ in range(240):
            r.now += INTERVAL_S
            fresh.tick()
        self.assertEqual(r.client.calls, [])
        self.assertEqual(fresh.status()["state"], "idle")

    def test_config_change_keeps_ownership_and_timers(self):
        r = Rig()
        r.bring_on()
        r.snapshot = cool()
        r.advance(5)
        since = r.status()["off_condition_since"]
        owned = r.status()["owned_since"]
        r.set_cfg(on={"water_c": 45.0}, ac={"temperature": 22.0})
        r.advance(0.5)
        self.assertEqual(r.state(), "owned")
        self.assertEqual(r.status()["off_condition_since"], since)
        self.assertEqual(r.status()["owned_since"], owned)


class TurnOffTest(unittest.TestCase):
    def test_off_needs_ten_minutes_and_thirty_minutes_runtime(self):
        r = Rig()
        r.bring_on()
        r.set(cool(35.0))                     # right after switching on
        r.advance(29.5)
        self.assertEqual(r.client.calls, [])  # off timer long satisfied, min runtime not
        self.assertEqual(r.state(), "owned")
        r.advance(0.5)
        self.assertEqual(r.client.calls, [("climate", "turn_off", {"entity_id": E})])
        self.assertEqual(r.state(), "cooldown")

    def test_off_waits_for_the_ten_minutes_after_long_runtime(self):
        r = Rig()
        r.bring_on()
        r.advance(60)                          # still hot
        r.set(cool(36.0))                      # inclusive threshold
        r.advance(9.5)
        self.assertEqual(r.client.calls, [])
        self.assertIsNotNone(r.status()["off_condition_since"])
        r.advance(0.5)
        self.assertEqual(r.client.calls, [("climate", "turn_off", {"entity_id": E})])

    def test_warming_up_resets_the_off_timer(self):
        r = Rig()
        r.bring_on()
        r.advance(60)
        r.set(cool(35.0))
        r.advance(9)
        r.set(snap(36.5, (30, 40, 40, 30)))
        self.assertIsNone(r.status()["off_condition_since"])
        r.set(cool(35.0))
        r.advance(9.5)
        self.assertEqual(r.client.calls, [])
        r.advance(0.5)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)

    def test_between_thresholds_nothing_happens(self):
        r = Rig()
        r.bring_on()
        r.snapshot = snap(38.0, (30, 60, 60, 30))
        r.advance(240)
        self.assertEqual(r.client.calls, [])
        self.assertEqual(r.state(), "owned")

    def test_offline_keeps_ac_on_and_resets_the_off_timer(self):
        r = Rig()
        r.bring_on()
        r.advance(60)
        r.set(cool(35.0))
        r.advance(8)
        r.set(cool(35.0) | {"online": False})
        self.assertIsNone(r.status()["off_condition_since"])
        r.set(cool(35.0))
        r.advance(9.5)
        self.assertEqual(r.client.calls, [])       # the 10 minutes start over after the gap
        r.advance(0.5)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)

    def test_long_offline_never_switches_off(self):
        r = Rig()
        r.bring_on()
        r.snapshot = {"online": False, "updated": None, "status": None, "sensors": []}
        r.advance(300)
        self.assertEqual(r.client.calls, [])
        self.assertEqual(r.state(), "owned")

    def test_unknown_water_resets_the_off_timer(self):
        r = Rig()
        r.bring_on()
        r.advance(60)
        r.set(cool(35.0))
        r.advance(5)
        r.set(snap(None, (30, 40, 40, 30)))
        self.assertIsNone(r.status()["off_condition_since"])

    def test_off_ends_ownership(self):
        r = Rig()
        r.bring_on()
        r.advance(30)
        r.set(cool())
        r.advance(10)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)
        self.assertIsNone(r.status()["owned_since"])
        r.advance(5)  # AC is off now; nothing more happens
        self.assertEqual(r.client.count("climate", "turn_off"), 1)


class LockoutAndLimitTest(unittest.TestCase):
    def cycle_off(self, r):
        r.advance(30)
        r.set(cool())
        r.advance(10)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)

    def test_lockout_after_own_off(self):
        r = Rig(max_switches_per_hour=10)
        r.bring_on()
        self.cycle_off(r)
        r.client.log.clear()
        r.set(hot())
        r.advance(14.5)
        self.assertEqual(r.client.log, [])
        self.assertEqual(r.state(), "cooldown")
        self.assertIsNotNone(r.status()["cooldown_until"])
        r.advance(0.5)
        self.assertEqual(r.client.calls, ON_CALLS)

    def test_lockout_is_configurable(self):
        r = Rig(max_switches_per_hour=10, min_off_minutes=20)
        r.bring_on()
        self.cycle_off(r)
        r.client.log.clear()
        r.set(hot())
        r.advance(19.5)
        self.assertEqual(r.client.log, [])
        r.advance(0.5)
        self.assertEqual(r.client.calls, ON_CALLS)

    def test_switch_limit_blocks_switching_on(self):
        r = Rig()  # two switches per hour
        r.bring_on()                               # switch 1 (t0 + 5 min)
        t_on = r.status()["owned_since"]
        self.cycle_off(r)                          # switch 2 at ~ +40 min
        r.client.log.clear()
        r.set(hot())
        r.advance(15)                              # lockout (15 min) is over, limit is not
        self.assertEqual(r.client.log, [])
        self.assertEqual(r.state(), "idle")
        self.assertIn("Schaltlimit", r.status()["reason"])
        r.now = t_on + 3600 - 1
        r.tick()
        self.assertEqual(r.client.log, [])
        r.now = t_on + 3600 + 1
        r.tick()
        self.assertEqual(r.client.calls, ON_CALLS)

    def test_off_is_never_blocked_by_the_limit(self):
        r = Rig(max_switches_per_hour=1)
        r.bring_on()                               # limit already reached by this switch
        r.advance(30)
        r.set(cool())
        r.advance(10)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)

    def test_off_counts_towards_the_limit(self):
        # short timers so that four switches fit into one hour
        r = Rig(max_switches_per_hour=3, on={"minutes": 1}, off={"minutes": 1}, min_on_minutes=1, min_off_minutes=1)
        for _ in range(2):
            r.set(hot())
            r.advance(2)
            r.set(cool())
            r.advance(2)
        self.assertEqual(r.client.count("climate", "set_hvac_mode"), 2)
        self.assertEqual(r.client.count("climate", "turn_off"), 2)   # the 4th switch is an off: never blocked
        self.assertEqual(r.status()["switches_last_hour"], 4)
        r.set(hot())
        r.advance(20)                                                # lockout over, but the limit is reached
        self.assertEqual(r.client.count("climate", "set_hvac_mode"), 2)
        self.assertIn("Schaltlimit", r.status()["reason"])

    def test_hand_over_does_not_count_as_a_switch(self):
        r = Rig(max_switches_per_hour=2, min_off_minutes=1)
        r.bring_on()                               # switch 1
        r.client.ac("off")
        r.advance(1.5)                             # hand over
        r.client.log.clear()
        r.advance(5)
        self.assertEqual(r.client.calls, ON_CALLS)  # switch 2 is still allowed


class ErrorTest(unittest.TestCase):
    def test_backoff_one_five_fifteen_minutes(self):
        r = Rig()
        r.tick()
        r.advance(4.5)
        r.client.failing = {"get"}
        r.advance(0.5)                                  # first attempt, fails
        self.assertEqual(len(r.client.gets), 1)
        self.assertIsNotNone(r.status()["last_error"])
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), 1)         # no new attempt within the first minute
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), 2)         # retry after 1 min, fails
        r.advance(4.5)
        self.assertEqual(len(r.client.gets), 2)         # now 5 minutes
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), 3)
        r.advance(14.5)
        self.assertEqual(len(r.client.gets), 3)         # then 15 minutes
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), 4)
        r.advance(14.5)
        self.assertEqual(len(r.client.gets), 4)         # stays at 15
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), 5)

    def test_success_resets_the_backoff(self):
        r = Rig()
        r.tick()
        r.advance(4.5)
        r.client.failing = {"get"}
        r.advance(0.5)
        r.advance(1)                                    # second failure, backoff now 5 min
        r.client.failing = set()
        r.advance(5)
        self.assertEqual(r.state(), "owned")
        self.assertIsNone(r.status()["last_error"])
        r.client.failing = {"get"}
        r.advance(0.5)                                  # the next failure starts again at 1 minute
        n = len(r.client.gets)
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), n)
        r.advance(0.5)
        self.assertEqual(len(r.client.gets), n + 1)

    def test_errors_are_logged_as_events_without_spam(self):
        r = Rig()
        r.client.failing = {"get"}
        r.tick()
        r.advance(5)
        r.advance(10)
        errors = [e for e in r.status()["events"] if "Fehler" in e["message"]]
        self.assertGreaterEqual(len(errors), 3)
        self.assertLessEqual(len(errors), 6)            # 1 + 5 + 15 minute backoff, not every 30 s

    def test_error_while_owned_keeps_ownership(self):
        r = Rig()
        r.bring_on()
        r.client.failing = {"*"}
        r.snapshot = cool()
        r.advance(60)
        self.assertEqual(r.state(), "owned")
        self.assertEqual(r.client.count("climate", "turn_off"), 0)
        self.assertIsNotNone(r.status()["last_error"])
        r.client.failing = set()
        r.advance(16)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)

    def test_turn_off_failure_keeps_ownership_and_backs_off(self):
        r = Rig()
        r.bring_on()
        r.advance(30)
        r.client.failing = {"climate.turn_off"}
        r.set(cool())
        r.advance(10)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)
        self.assertEqual(r.state(), "owned")
        r.advance(0.5)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)
        r.client.failing = set()
        r.advance(1)
        self.assertEqual(r.client.count("climate", "turn_off"), 2)
        self.assertEqual(r.state(), "cooldown")

    def test_half_executed_switch_on_counts_and_takes_ownership(self):
        r = Rig()
        r.client.failing = {"climate.set_temperature"}
        r.tick()
        r.advance(5)
        self.assertEqual([c[1] for c in r.client.calls], ["set_hvac_mode", "set_temperature"])  # stops at the error
        self.assertEqual(r.state(), "owned")           # AC is running now (state != off)
        self.assertIn("Einschalten", r.status()["last_error"])
        r.client.failing = set()
        r.snapshot = cool()
        r.advance(40)
        self.assertEqual(r.client.count("climate", "turn_off"), 1)   # it is ours, so it gets switched off

    def test_half_executed_switch_on_counts_towards_the_limit(self):
        r = Rig(max_switches_per_hour=1)
        r.client.failing = {"climate.set_preset_mode"}
        r.tick()
        r.advance(5)
        r.client.ac("off")                              # AC gets switched off by hand afterwards
        r.client.failing = set()
        r.advance(40)
        self.assertEqual(r.client.count("climate", "set_hvac_mode"), 1)   # one attempt, limit holds

    def test_first_call_failing_is_not_a_switch(self):
        r = Rig(max_switches_per_hour=1)
        r.client.failing = {"climate.set_hvac_mode"}
        r.tick()
        r.advance(5)
        self.assertEqual(r.state(), "arming")
        self.assertEqual(r.status()["switches_last_hour"], 0)
        r.client.failing = set()
        r.advance(2)
        self.assertEqual(r.client.count("climate", "set_temperature"), 1)
        self.assertEqual(r.state(), "owned")

    def test_ac_still_off_after_switch_on_is_not_owned(self):
        r = Rig()
        r.client.ignore_on = True
        r.tick()
        r.advance(5)
        self.assertEqual(r.client.calls, ON_CALLS)
        self.assertEqual(r.state(), "arming")
        self.assertIsNone(r.status()["owned_since"])
        r.advance(0.5)
        self.assertEqual(len(r.client.calls), len(ON_CALLS))   # waits for the on-time again, no hammering

    def test_read_back_failure_means_no_ownership(self):
        r = Rig()
        r.tick()
        r.advance(4.5)
        orig = r.client.get_state
        seen = []

        def get_state(eid):
            seen.append(eid)
            if len(seen) == 2:  # the first read decides "AC is off", the read-back after switching on fails
                raise HAError("Home Assistant nicht erreichbar: Test")
            return orig(eid)
        r.client.get_state = get_state
        r.advance(0.5)
        self.assertEqual(r.client.calls, ON_CALLS)
        self.assertNotEqual(r.state(), "owned")
        self.assertEqual(r.status()["switches_last_hour"], 1)


class BackoffEscalationTest(unittest.TestCase):
    """The backoff must also escalate when only the service call fails and the status read works."""

    def test_persistent_switch_on_failure_escalates(self):
        r = Rig()
        r.client.failing = {"climate.set_hvac_mode"}
        r.tick()
        r.advance(5 + 37)
        self.assertEqual(r.attempts("climate.set_hvac_mode"), [0.0, 1.0, 6.0, 21.0, 36.0])

    def test_persistent_turn_off_failure_escalates(self):
        r = Rig()
        r.bring_on()
        r.advance(30)
        r.client.failing = {"climate.turn_off"}
        r.client.stamps.clear()
        r.set(cool())
        r.advance(10 + 37)
        self.assertEqual(r.attempts("climate.turn_off"), [0.0, 1.0, 6.0, 21.0, 36.0])
        self.assertEqual(r.state(), "owned")


class DisabledTest(unittest.TestCase):
    def test_disabled_does_nothing(self):
        r = Rig(enabled=False)
        r.tick()
        r.advance(60)
        self.assertEqual(r.client.log, [])
        self.assertEqual(r.state(), "disabled")

    def test_disabled_never_even_builds_a_client(self):
        r = Rig(enabled=False)
        r.tick()
        r.advance(5)
        self.assertEqual(r.factory_calls, 0)

    def test_status_reflects_disabling_immediately(self):
        r = Rig()
        r.tick()
        self.assertEqual(r.state(), "arming")
        r.set_cfg(enabled=False)
        self.assertEqual(r.state(), "disabled")

    def test_disabling_releases_ownership_without_switching_off(self):
        r = Rig()
        r.bring_on()
        r.set_cfg(enabled=False)
        r.snapshot = cool()
        r.advance(60)
        self.assertEqual(r.client.log, [])
        self.assertEqual(r.state(), "disabled")
        self.assertIsNone(r.status()["owned_since"])
        r.set_cfg(enabled=True)
        r.advance(120)
        self.assertEqual(r.client.count("climate", "turn_off"), 0)  # the running AC is foreign now

    def test_disabling_resets_the_arming_clock(self):
        r = Rig()
        r.tick()
        r.advance(4)
        r.set_cfg(enabled=False)
        r.advance(1)
        r.set_cfg(enabled=True)
        r.advance(4.5)
        self.assertEqual(r.client.calls, [])


class StatusTest(unittest.TestCase):
    def test_status_fields(self):
        r = Rig()
        r.tick()
        st = r.status()
        for key in ("state", "reason", "enabled", "arming_since", "owned_since", "cooldown_until",
                    "off_condition_since", "last_error", "events", "switches_last_hour"):
            self.assertIn(key, st)
        self.assertEqual(st["state"], "arming")
        self.assertEqual(st["arming_since"], r.now)
        self.assertTrue(st["reason"])
        self.assertTrue(st["enabled"])

    def test_events_are_capped_at_twenty_newest_first(self):
        r = Rig()
        r.client.failing = {"*"}
        r.snapshot = hot()
        r.tick()
        r.advance(5)
        for i in range(30):
            r.advance(15)
        events = r.status()["events"]
        self.assertEqual(len(events), 20)
        self.assertGreaterEqual(events[0]["t"], events[-1]["t"])
        self.assertTrue(all(set(e) == {"t", "message"} for e in events))

    def test_events_carry_timestamps(self):
        r = Rig()
        r.tick()
        r.advance(5)
        event = r.status()["events"][0]
        self.assertEqual(event["t"], r.now)

    def test_status_is_a_copy(self):
        r = Rig()
        r.tick()
        st = r.status()
        st["events"].append("x")
        st["state"] = "kaputt"
        self.assertEqual(r.status()["state"], "arming")

    def test_status_before_first_tick(self):
        r = Rig()
        self.assertIn(r.state(), ("idle", "arming"))
        self.assertEqual(Rig(enabled=False).state(), "disabled")

    def test_status_does_not_block_while_ha_hangs(self):
        r = Rig()
        r.tick()
        r.advance(4.5)
        entered, release = threading.Event(), threading.Event()
        orig = r.client.get_state

        def slow(eid):
            entered.set()
            release.wait(5)
            return orig(eid)
        r.client.get_state = slow
        t = threading.Thread(target=r.advance, args=(0.5,))
        t.start()
        self.assertTrue(entered.wait(5))
        done = []
        s = threading.Thread(target=lambda: done.append(r.status()))
        s.start()
        s.join(1)
        self.assertEqual(len(done), 1)       # status() answered while tick() sits in the HA call
        release.set()
        t.join(5)
        self.assertFalse(t.is_alive())


class ClientFactoryTest(unittest.TestCase):
    def config(self, url, token):
        cfg = parse_climate_config({"ha_url": url})
        return SimpleNamespace(climate_config=lambda: cfg, secrets=SimpleNamespace(get_ha_token=lambda: token))

    def test_none_without_url_or_token(self):
        self.assertIsNone(make_client_factory(self.config("", "tok"))())
        self.assertIsNone(make_client_factory(self.config("http://ha.example:8123", ""))())

    def test_client_with_both(self):
        client = make_client_factory(self.config("http://ha.example:8123", "tok"))()
        self.assertIsInstance(client, HAClient)
        self.assertEqual(client.base_url, "http://ha.example:8123")
        self.assertNotIn("tok", repr(client))

    def test_follows_config_changes(self):
        state = {"url": "", "token": ""}
        cfg = SimpleNamespace(climate_config=lambda: parse_climate_config({"ha_url": state["url"]}),
                              secrets=SimpleNamespace(get_ha_token=lambda: state["token"]))
        factory = make_client_factory(cfg)
        self.assertIsNone(factory())
        state.update(url="http://ha.example:8123", token="tok")
        self.assertIsNotNone(factory())


class RunLoopTest(unittest.TestCase):
    def test_run_ticks_every_thirty_seconds_until_stopped(self):
        r = Rig()
        waits = []

        class Stop:
            def is_set(self):
                return len(waits) >= 3

            def wait(self, timeout):
                waits.append(timeout)

        r.ctrl.run(Stop())
        self.assertEqual(waits, [INTERVAL_S] * 3)
        self.assertEqual(INTERVAL_S, 30)

    def test_run_survives_a_failing_tick(self):
        def boom():
            raise RuntimeError("kaputt")

        ctrl = ClimateController(boom, lambda: None, lambda: parse_climate_config({"enabled": True}), clock=lambda: 1.0)
        stop = threading.Event()
        calls = []
        orig_wait = stop.wait

        def wait(timeout):
            calls.append(timeout)
            if len(calls) >= 2:
                stop.set()
            return orig_wait(0)
        stop.wait = wait
        with self.assertLogs("aquacontrol.climate", level="ERROR"):
            ctrl.run(stop)
        self.assertEqual(len(calls), 2)

    def test_backoff_constants(self):
        self.assertEqual(BACKOFF_S, (60, 300, 900))


if __name__ == "__main__":
    unittest.main()
