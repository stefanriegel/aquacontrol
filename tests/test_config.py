import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aquacontrol.auth import hash_password, hash_token, token_source, verify_password
from aquacontrol.climate import DEFAULT_CLIMATE, ClimateConfigError
from aquacontrol.config import (_atomic_write, AppConfig, ConfigError, Secrets, load_daemon_config, update_daemon_config)
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
        self.assertEqual(cfg.rules(), [])  # no schedule by default: a fresh install never writes on its own
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
        cfg.set_rules([{"time": "22:00", "target": "strip", "on": False}])
        with self.assertRaises(ScheduleError):
            cfg.set_rules([{"time": "99:00", "on": True}])
        self.assertEqual(len(cfg.rules()), 1)
        self.assertEqual(json.loads(self.path.read_text())["schedule"][0]["time"], "22:00")

    def test_corrupt_file(self):
        self.path.write_text("{")
        with self.assertRaises(ConfigError):
            AppConfig(self.path)


class AtomicWriteTest(unittest.TestCase):
    def test_data_is_flushed_to_disk_before_the_rename(self):
        """After a power loss a renamed but never synced file can be empty (secrets.json = silently no token)."""
        calls = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            calls.append("fsync")
            real_fsync(fd)

        def replace(src, dst):
            calls.append("replace")
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "x.json")
            with mock.patch.object(os, "fsync", fsync), mock.patch.object(os, "replace", replace):
                _atomic_write(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(calls[0], "fsync")
        self.assertLess(calls.index("fsync"), calls.index("replace"))


class SecretsTest(unittest.TestCase):
    TOKEN = "eyJhbGciOi.s3cr3t-token_value.sig"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "secrets.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_file_means_no_token(self):
        self.assertEqual(Secrets(self.path).get_ha_token(), "")
        self.assertFalse(self.path.exists())

    def test_roundtrip_and_mode_0600(self):
        old = os.umask(0)  # even a permissive umask must not leak the file
        self.addCleanup(os.umask, old)
        sec = Secrets(self.path)
        sec.set_ha_token(self.TOKEN)
        self.assertEqual(sec.get_ha_token(), self.TOKEN)
        self.assertEqual(Secrets(self.path).get_ha_token(), self.TOKEN)  # persisted
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {"ha_token": self.TOKEN})

    def test_existing_loose_mode_is_tightened(self):
        self.path.write_text("{}")
        os.chmod(self.path, 0o644)
        Secrets(self.path).set_ha_token(self.TOKEN)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_empty_string_clears(self):
        sec = Secrets(self.path)
        sec.set_ha_token(self.TOKEN)
        sec.set_ha_token("")
        self.assertEqual(sec.get_ha_token(), "")
        self.assertEqual(Secrets(self.path).get_ha_token(), "")
        self.assertNotIn(self.TOKEN, self.path.read_text())

    def test_other_keys_are_kept(self):
        self.path.write_text(json.dumps({"other": "x"}))
        Secrets(self.path).set_ha_token(self.TOKEN)
        self.assertEqual(json.loads(self.path.read_text()), {"other": "x", "ha_token": self.TOKEN})

    def test_write_is_atomic_no_temp_files_left(self):
        Secrets(self.path).set_ha_token(self.TOKEN)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["secrets.json"])

    def test_surrounding_whitespace_is_stripped_and_bad_tokens_rejected(self):
        sec = Secrets(self.path)
        sec.set_ha_token(f"  {self.TOKEN}\n")
        self.assertEqual(sec.get_ha_token(), self.TOKEN)
        for bad in ("a b", "x\ny", "tökən", "a" * 5000, 5, None):
            with self.subTest(bad=bad), self.assertRaises(ClimateConfigError):
                sec.set_ha_token(bad)
        self.assertEqual(sec.get_ha_token(), self.TOKEN)  # unchanged

    def test_corrupt_file_is_no_token_and_not_fatal(self):
        self.path.write_text("{nope")
        sec = Secrets(self.path)
        self.assertEqual(sec.get_ha_token(), "")
        sec.set_ha_token(self.TOKEN)
        self.assertEqual(sec.get_ha_token(), self.TOKEN)

    def test_stored_token_that_fails_validation_is_not_returned(self):
        # a hand-edited secrets.json must not be able to smuggle header-breaking characters into a request
        for bad in ("SECRET\nTOKEN", "SECRET\r\nX-Evil: 1", "SECRET has space", "SECRETtökən", "S" * 5000, 5, ["x"]):
            with self.subTest(bad=repr(bad)[:16]):
                self.path.write_text(json.dumps({"ha_token": bad}))
                sec = Secrets(self.path)
                with self.assertLogs("aquacontrol.config", level="WARNING") as logs:
                    self.assertEqual(sec.get_ha_token(), "")
                self.assertNotIn("SECRET", "\n".join(logs.output))
                with self.assertNoLogs("aquacontrol.config", level="WARNING"):  # warned once, not every cycle
                    self.assertEqual(sec.get_ha_token(), "")

    def test_stored_token_with_surrounding_whitespace_is_not_usable_either(self):
        self.path.write_text(json.dumps({"ha_token": f" {self.TOKEN}\n"}))
        with self.assertLogs("aquacontrol.config", level="WARNING"):
            self.assertEqual(Secrets(self.path).get_ha_token(), "")

    def test_token_is_never_logged(self):
        with self.assertNoLogs(level="DEBUG"):
            sec = Secrets(self.path)
            sec.set_ha_token(self.TOKEN)
            sec.get_ha_token()
            sec.set_ha_token("")


class ClimateSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_without_file(self):
        cfg = AppConfig(self.path)
        self.assertEqual(cfg.climate_raw(), DEFAULT_CLIMATE)
        self.assertFalse(cfg.climate_config().enabled)
        self.assertEqual(cfg.climate_raw()["ha_url"], "")

    def test_secrets_live_next_to_config(self):
        cfg = AppConfig(self.path)
        self.assertEqual(cfg.secrets.path, Path(self.tmp.name, "secrets.json"))

    def test_set_climate_persists_atomically_and_keeps_other_sections(self):
        self.path.write_text(json.dumps({"schedule": [{"time": "22:00", "target": "strip", "on": False}],
                                         "backup_dir": "/x"}))
        cfg = AppConfig(self.path)
        raw = {**DEFAULT_CLIMATE, "enabled": True, "ha_url": "http://ha.example:8123"}
        cfg.set_climate(raw)
        again = AppConfig(self.path)
        self.assertTrue(again.climate_config().enabled)
        self.assertEqual(again.climate_raw()["ha_url"], "http://ha.example:8123")
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk["backup_dir"], "/x")
        self.assertEqual(len(again.rules()), 1)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["config.json"])

    def test_set_climate_invalid_keeps_old_and_writes_nothing(self):
        cfg = AppConfig(self.path)
        cfg.set_climate({**DEFAULT_CLIMATE, "enabled": True})
        before = self.path.read_text()
        with self.assertRaises(ClimateConfigError):
            cfg.set_climate({**DEFAULT_CLIMATE, "on": {**DEFAULT_CLIMATE["on"], "minutes": 0}})
        self.assertEqual(self.path.read_text(), before)
        self.assertTrue(cfg.climate_config().enabled)

    def test_patch_climate_calls_on_url_change_under_the_lock_before_writing(self):
        cfg = AppConfig(self.path)
        cfg.patch_climate({"ha_url": "http://ha.example:8123"})
        seen = []
        real = cfg._store_climate
        cfg._store_climate = lambda c: (seen.append("store"), real(c))[1]
        hook = lambda: seen.append(("hook", cfg._lock.locked()))  # noqa: E731
        cfg.patch_climate({"enabled": True}, on_url_change=hook)               # same address: no call
        cfg.patch_climate({"ha_url": "http://ha.example:8123/"}, on_url_change=hook)  # equivalent: no call
        self.assertEqual(seen, ["store", "store"])
        seen.clear()
        cfg.patch_climate({"ha_url": "http://other.example:8123"}, on_url_change=hook)
        self.assertEqual(seen, [("hook", True), "store"])
        seen.clear()
        with self.assertRaises(ClimateConfigError):  # invalid: neither hook nor write
            cfg.patch_climate({"ha_url": "ftp://x"}, on_url_change=hook)
        self.assertEqual(seen, [])

    def test_patch_climate_merges_onto_current(self):
        cfg = AppConfig(self.path)
        cfg.set_climate({**DEFAULT_CLIMATE, "ha_url": "http://ha.example:8123"})
        cfg.patch_climate({"on": {"minutes": 7}, "enabled": True})
        raw = cfg.climate_raw()
        self.assertEqual(raw["ha_url"], "http://ha.example:8123")
        self.assertEqual(raw["on"], {**DEFAULT_CLIMATE["on"], "minutes": 7})
        self.assertTrue(raw["enabled"])
        with self.assertRaises(ClimateConfigError):
            cfg.patch_climate({"ac": {"hvac_mode": "heat"}})
        self.assertEqual(cfg.climate_raw()["ac"]["hvac_mode"], "cool")

    def test_climate_raw_is_a_copy(self):
        cfg = AppConfig(self.path)
        cfg.climate_raw()["on"]["minutes"] = 99
        self.assertEqual(cfg.climate_raw()["on"]["minutes"], 5)

    def test_invalid_stored_section_falls_back_to_defaults(self):
        self.path.write_text(json.dumps({"climate": {"enabled": True, "on": {"minutes": 0}}}))
        with self.assertLogs("aquacontrol.config", level="WARNING"):
            cfg = AppConfig(self.path)
        self.assertEqual(cfg.climate_raw(), DEFAULT_CLIMATE)  # disabled: the safe side

    def test_token_never_lands_in_config_json(self):
        cfg = AppConfig(self.path)
        cfg.secrets.set_ha_token("abc.def.ghi")
        cfg.set_climate(DEFAULT_CLIMATE)
        self.assertNotIn("abc.def.ghi", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
