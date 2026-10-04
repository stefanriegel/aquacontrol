import json
import tempfile
import unittest
from pathlib import Path

from aquacontrol.auth import hash_password, hash_token, token_source, verify_password
from aquacontrol.config import (AppConfig, ConfigError, load_daemon_config, update_daemon_config)
from aquacontrol.schedule import ScheduleError


class AuthTest(unittest.TestCase):
    def test_password_roundtrip(self):
        h = hash_password("geheim", iterations=1000)
        self.assertTrue(verify_password("geheim", h))
        self.assertFalse(verify_password("falsch", h))

    def test_malformed_hash(self):
        for bad in ("", "x", "md5$1$a$b", "pbkdf2_sha256$abc$$"):
            self.assertFalse(verify_password("x", bad))

    def test_token_source(self):
        tokens = {"llm-vm": hash_token("t0k3n")}
        self.assertEqual(token_source("t0k3n", tokens), "llm-vm")
        self.assertIsNone(token_source("other", tokens))


class DaemonConfigTest(unittest.TestCase):
    def test_load_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "daemon.json")
            path.write_text(json.dumps({"port": 9443}))
            update_daemon_config(path, password_hash="h")
            update_daemon_config(path, push_token=("llm-vm", "abc"))
            cfg = load_daemon_config(path)
            self.assertEqual((cfg.port, cfg.password_hash, cfg.push_tokens), (9443, "h", {"llm-vm": "abc"}))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)  # existing file mode kept

    def test_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "daemon.json")
            path.write_text("{nope")
            with self.assertRaises(ConfigError):
                load_daemon_config(path)
            path.write_text(json.dumps({"port": 0}))
            with self.assertRaises(ConfigError):
                load_daemon_config(path)


class AppConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_without_file(self):
        cfg = AppConfig(self.path)
        self.assertEqual(cfg.fan_name(0, "x"), "Pumpe")
        self.assertEqual(cfg.fan_name(3, "Fan 4"), "Gehäuselüfter")
        self.assertEqual(cfg.min_percent(), {0: 25.0})
        self.assertEqual(len(cfg.rules()), 2)
        self.assertEqual(cfg.led_name(0, "LED Controller 1"), "LED Controller 1")

    def _cfg(self, raw):
        self.path.write_text(json.dumps(raw))
        return AppConfig(self.path)

    def test_min_percent_partial_config_keeps_pump_floor(self):
        self.assertEqual(self._cfg({"schedule": []}).min_percent(), {0: 25.0})
        self.assertEqual(self._cfg({"fans": {"2": {"name": "x"}}}).min_percent(), {0: 25.0})

    def test_min_percent_valid_value_wins(self):
        cfg = self._cfg({"fans": {"1": {"min_percent": 30}, "3": {"min_percent": 12.5}}})
        self.assertEqual(cfg.min_percent(), {0: 30.0, 2: 12.5})

    def test_min_percent_ignores_invalid(self):
        for bad in (-5, 150, "x", True, None, float("nan")):
            with self.subTest(bad=bad):
                cfg = self._cfg({"fans": {"1": {"min_percent": bad}, "2": {"min_percent": bad}}})
                self.assertEqual(cfg.min_percent(), {0: 25.0})
        cfg = self._cfg({"fans": {"0": {"min_percent": 10}, "abc": {"min_percent": 10},
                                  "9": {"min_percent": 10}, "4": "kaputt"}})
        self.assertEqual(cfg.min_percent(), {0: 25.0})

    def test_set_rules_persists_atomically(self):
        cfg = AppConfig(self.path)
        cfg.set_rules([{"time": "22:00", "target": "strip", "on": False}])
        again = AppConfig(self.path)
        self.assertEqual(len(again.rules()), 1)
        self.assertEqual(json.loads(self.path.read_text())["schedule"][0]["time"], "22:00")

    def test_set_rules_invalid_keeps_old(self):
        cfg = AppConfig(self.path)
        with self.assertRaises(ScheduleError):
            cfg.set_rules([{"time": "99:00", "on": True}])
        self.assertEqual(len(cfg.rules()), 2)
        self.assertFalse(self.path.exists())

    def test_corrupt_file(self):
        self.path.write_text("{")
        with self.assertRaises(ConfigError):
            AppConfig(self.path)


if __name__ == "__main__":
    unittest.main()
