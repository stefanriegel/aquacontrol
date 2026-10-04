import copy
import unittest

from aquacontrol.climate import (DEFAULT_CLIMATE, ClimateConfig, ClimateConfigError, merge_climate,
                                 parse_climate_config)


class ParseClimateConfigTest(unittest.TestCase):
    def bad(self, patch, fragment=None):
        raw = merge_climate(DEFAULT_CLIMATE, patch)
        with self.assertRaises(ClimateConfigError) as cm:
            parse_climate_config(raw)
        if fragment:
            self.assertIn(fragment, str(cm.exception))

    def good(self, patch):
        return parse_climate_config(merge_climate(DEFAULT_CLIMATE, patch))

    def test_defaults_match_the_spec(self):
        cfg = parse_climate_config({})
        self.assertIsInstance(cfg, ClimateConfig)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.ha_url, "")
        self.assertEqual(cfg.entity_id, "climate.panasonic_ac_panasonic_ac")
        self.assertEqual((cfg.on.water_c, cfg.on.fan_percent, cfg.on.fan_channels, cfg.on.minutes),
                         (40.0, 85.0, (2, 3), 5))
        self.assertEqual((cfg.off.water_c, cfg.off.minutes), (36.0, 10))
        self.assertEqual((cfg.min_on_minutes, cfg.min_off_minutes, cfg.max_switches_per_hour), (30, 15, 2))
        self.assertEqual((cfg.ac.hvac_mode, cfg.ac.temperature, cfg.ac.preset, cfg.ac.fan_mode,
                          cfg.ac.horizontal, cfg.ac.vertical),
                         ("cool", 20.0, "Quiet", "Automatic", "left", "down_center"))

    def test_to_json_roundtrip(self):
        cfg = parse_climate_config(DEFAULT_CLIMATE)
        self.assertEqual(cfg.to_json(), DEFAULT_CLIMATE)
        self.assertEqual(parse_climate_config(cfg.to_json()), cfg)

    def test_default_does_not_mutate(self):
        before = copy.deepcopy(DEFAULT_CLIMATE)
        self.good({"enabled": True, "on": {"water_c": 45}})
        self.assertEqual(DEFAULT_CLIMATE, before)

    def test_merge_is_deep(self):
        merged = merge_climate(DEFAULT_CLIMATE, {"on": {"minutes": 7}, "ac": {"temperature": 22}})
        self.assertEqual(merged["on"]["minutes"], 7)
        self.assertEqual(merged["on"]["water_c"], 40.0)
        self.assertEqual(merged["ac"]["preset"], "Quiet")

    def test_valid_variations(self):
        cfg = self.good({"enabled": True, "ha_url": "https://ha.example:8123/", "on": {"water_c": 38},
                         "off": {"water_c": 36}, "ac": {"temperature": 16, "hvac_mode": "fan_only",
                                                         "fan_mode": "3", "preset": "Powerful",
                                                         "horizontal": "auto", "vertical": "swing"}})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.ha_url, "https://ha.example:8123")  # trailing slash removed
        self.assertEqual(cfg.ac.temperature, 16.0)
        self.assertEqual(self.good({"ac": {"temperature": 30}}).ac.temperature, 30.0)
        self.assertEqual(self.good({"ac": {"temperature": 21.5}}).ac.temperature, 21.5)
        self.assertEqual(self.good({"on": {"fan_channels": [3, 1, 2]}}).on.fan_channels, (1, 2, 3))

    def test_not_a_dict(self):
        for raw in (None, [], "x", 3):
            with self.subTest(raw=raw), self.assertRaises(ClimateConfigError):
                parse_climate_config(raw)

    def test_unknown_keys_rejected(self):
        self.bad({"bogus": 1}, "bogus")
        self.bad({"on": {"bogus": 1}}, "bogus")
        self.bad({"ac": {"bogus": 1}}, "bogus")

    def test_enabled_must_be_bool(self):
        for v in ("yes", 1, None):
            with self.subTest(v=v):
                self.bad({"enabled": v}, "enabled")

    def test_hysteresis_gap(self):
        self.bad({"off": {"water_c": 40.0}}, "mindestens 2")
        self.bad({"off": {"water_c": 41.0}})
        self.bad({"on": {"water_c": 38.0}, "off": {"water_c": 36.5}}, "mindestens 2")
        self.good({"on": {"water_c": 38.0}, "off": {"water_c": 36.0}})  # exactly 2 degrees is fine

    def test_minutes_range(self):
        for path in (("on", "minutes"), ("off", "minutes"), ("min_on_minutes",), ("min_off_minutes",)):
            for v in (0, 241, -1, 1.5, True, "5", None):
                with self.subTest(path=path, v=v):
                    patch = {path[0]: v} if len(path) == 1 else {path[0]: {path[1]: v}}
                    self.bad(patch)
            for v in (1, 240):
                patch = {path[0]: v} if len(path) == 1 else {path[0]: {path[1]: v}}
                self.good(patch)

    def test_max_switches_per_hour(self):
        for v in (0, -1, 1.5, True, "2"):
            with self.subTest(v=v):
                self.bad({"max_switches_per_hour": v})
        self.good({"max_switches_per_hour": 1})

    def test_percent_range(self):
        for v in (-1, 100.5, True, "x", float("nan")):
            with self.subTest(v=v):
                self.bad({"on": {"fan_percent": v}}, "Lüfterleistung")
        self.good({"on": {"fan_percent": 0}})
        self.good({"on": {"fan_percent": 100}})

    def test_channels(self):
        for v in ([], [0], [5], [2, 2], [True], ["2"], "2,3", None, [1.5]):
            with self.subTest(v=v):
                self.bad({"on": {"fan_channels": v}}, "Kanäle")

    def test_temperature_steps_and_range(self):
        for v in (15.5, 30.5, 20.3, True, "20", float("inf")):
            with self.subTest(v=v):
                self.bad({"ac": {"temperature": v}}, "Temperatur")

    def test_water_temperatures_must_be_numbers(self):
        for v in ("x", True, None, float("nan")):
            with self.subTest(v=v):
                self.bad({"on": {"water_c": v}})
                self.bad({"off": {"water_c": v}})

    def test_ac_choices(self):
        self.bad({"ac": {"hvac_mode": "heat"}}, "hvac_mode")
        self.bad({"ac": {"hvac_mode": "off"}})
        self.bad({"ac": {"preset": "Turbo"}}, "Preset")
        self.bad({"ac": {"fan_mode": "6"}}, "Lüfter")
        self.bad({"ac": {"fan_mode": 3}}, "Lüfter")
        self.bad({"ac": {"horizontal": "up"}}, "horizontal")
        self.bad({"ac": {"vertical": "left"}}, "vertical")

    def test_ha_url(self):
        for v in ("ftp://x", "ha", "http://", "javascript:alert(1)", "http://a b", 5, None):
            with self.subTest(v=v):
                self.bad({"ha_url": v}, "ha_url")
        self.good({"ha_url": ""})
        self.good({"ha_url": "http://ha.example:8123"})

    def test_entity_ids(self):
        for key, domain in (("entity_id", "climate"), ("horizontal_select", "select"), ("vertical_select", "select")):
            for v in ("switch.x", domain, f"{domain}.", f"{domain}.Foo", f"{domain}.a b", f"{domain}.a/b", "", None, 5):
                with self.subTest(key=key, v=v):
                    self.bad({key: v}, key)
            self.good({key: f"{domain}.my_thing_2"})

    def test_messages_are_german(self):
        self.bad({"on": {"minutes": 0}}, "Minuten")


if __name__ == "__main__":
    unittest.main()
