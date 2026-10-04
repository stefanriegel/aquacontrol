import base64
import http.client
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from aquacontrol.auth import hash_password, hash_token
from aquacontrol.backups import BackupStore
from aquacontrol.climate import DEFAULT_CLIMATE, ClimateController
from aquacontrol.config import AppConfig
from aquacontrol.device import Device
from aquacontrol.fake import FakeTransport
from aquacontrol.monitor import Monitor
from aquacontrol.schedule import Scheduler
from aquacontrol.sensors import ExternalStore
from aquacontrol.ha import HAClient, HAError
from aquacontrol.validate import make_check
from aquacontrol import web
from aquacontrol.web import App, make_server
from tests.fixtures import load
from tests.test_ha import FakeHA
from tests.test_climate import E as CLIMATE_ENTITY, FakeClient, H as HSEL, V as VSEL

STATIC = Path(__file__).resolve().parent.parent / "static"


class WebTest(unittest.TestCase):
    def setUp(self):
        # failed logins are logged at warning level; keep the test output pristine
        handler = logging.NullHandler()
        logging.getLogger("aquacontrol.web").addHandler(handler)
        self.addCleanup(logging.getLogger("aquacontrol.web").removeHandler, handler)
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeTransport(load("settings_live.bin"), load("names.bin"))
        self.backups = BackupStore(Path(self.tmp.name, "backups"))
        self.config = AppConfig(Path(self.tmp.name, "config.json"))
        self.device = Device(self.fake, self.backups, make_check(self.config.min_percent()))
        self.monitor = Monitor(open_reader=lambda: None)
        self.monitor.ingest(load("status.bin"))
        self.now = datetime(2026, 10, 5, 12, 0)
        self.scheduler = Scheduler(self.device, self.config.rules, clock=lambda: self.now)
        self.ha = FakeClient()
        self.climate = ClimateController(self.monitor.snapshot, self.ha_client, self.config.climate_config)
        self.app = App(self.device, self.monitor, self.scheduler, self.backups, self.config, ExternalStore(),
                       hash_password("pw", iterations=1000), {"llm-vm": hash_token("tok")}, STATIC,
                       climate=self.climate, ha_client_factory=self.ha_client)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.auth = "Basic " + base64.b64encode(b"admin:pw").decode()

    def ha_client(self):
        """Like the real factory: a client only if URL and token are configured."""
        if self.config.climate_config().ha_url and self.config.secrets.get_ha_token():
            return self.ha
        return None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, auth=True, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        h = {"Authorization": self.auth} if auth else {}
        if body is not None or raw is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(payload) if ctype.startswith("application/json") else payload)

    def test_requires_auth(self):
        status, _ = self.req("GET", "/api/status", auth=False)
        self.assertEqual(status, 401)
        bad = "Basic " + base64.b64encode(b"admin:nope").decode()
        status, _ = self.req("GET", "/api/status", auth=False, headers={"Authorization": bad})
        self.assertEqual(status, 401)

    def test_status_and_settings(self):
        status, body = self.req("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"]["temps"][0], 31.72)
        self.assertEqual(body["fan_names"][3], "Gehäuselüfter")
        status, body = self.req("GET", "/api/settings")
        self.assertEqual(body["fans"][0]["mode"], "curve")
        self.assertEqual(body["fans"][1]["target_c"], 34.0)
        self.assertEqual(body["fans"][0]["floor_percent"], 25.0)
        self.assertEqual(body["strip"], {"enabled": True, "brightness": 218})
        self.assertEqual(body["sensors"][0], "Wasser Temp")

    def test_update_fan_target(self):
        status, body = self.req("PUT", "/api/settings/fan/4", {"target_c": 38.0})
        self.assertEqual((status, body["changed"]), (200, True))
        self.assertTrue(body["backup"].endswith(".bin"))
        self.assertEqual(self.device.read_settings().controllers[3].target_c, 38.0)

    def test_update_fan_curve(self):
        curve = [[20 + i, 10 + 5 * i] for i in range(16)]
        status, _ = self.req("PUT", "/api/settings/fan/3", {"mode": "curve", "curve": curve})
        self.assertEqual(status, 200)
        c = self.device.read_settings().controllers[2]
        self.assertEqual(c.curve[15], (35.0, 85.0))

    def test_validation_and_malformed_bodies_are_400(self):
        cases = [
            ("/api/settings/fan/1", {"min_percent": 5}),             # pump floor
            ("/api/settings/fan/2", {"target_c": "warm"}),
            ("/api/settings/fan/2", {"mode": "pid"}),
            ("/api/settings/fan/2", {"curve": [[1, 2]] * 15}),
            ("/api/settings/fan/2", {"curve": [["a", 2]] * 16}),
            ("/api/settings/fan/9", {"target_c": 30}),
            ("/api/settings/strip", {"brightness": "hell"}),
            ("/api/settings/strip", [1, 2]),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                status, resp = self.req("PUT", path, body)
                self.assertEqual(status, 400, resp)
                self.assertIn("error", resp)
        status, _ = self.req("PUT", "/api/settings/fan/2", raw=b"{not json")
        self.assertEqual(status, 400)
        status, _ = self.req("PUT", "/api/settings/fan/2", raw=b'{"target_c": NaN}')
        self.assertEqual(status, 400)
        status, _ = self.req("PUT", "/api/settings/fan/2", body={"target_c": 30},
                             headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        self.assertEqual(self.fake.writes, [])

    def test_bad_curve_point_names_the_point_and_field(self):
        curve = [[20 + i, 10 + 5 * i] for i in range(16)]
        for point, field, bad, expected in ((2, 0, "a", "Kurvenpunkt 3: Temperatur muss eine Zahl sein"),
                                            (4, 1, None, "Kurvenpunkt 5: Prozent muss eine Zahl sein"),
                                            (0, 0, True, "Kurvenpunkt 1: Temperatur muss eine Zahl sein")):
            with self.subTest(expected=expected):
                c = [list(pt) for pt in curve]
                c[point][field] = bad
                status, resp = self.req("PUT", "/api/settings/fan/3", {"mode": "curve", "curve": c})
                self.assertEqual(status, 400)
                self.assertEqual(resp["error"], expected)

    def test_device_absent_is_502(self):
        self.fake.present = False
        status, body = self.req("GET", "/api/settings")
        self.assertEqual(status, 502)
        status, _ = self.req("PUT", "/api/settings/fan/2", {"target_c": 30})
        self.assertEqual(status, 502)

    def test_strip_and_schedule(self):
        status, body = self.req("PUT", "/api/settings/strip", {"brightness": 100})
        self.assertEqual(status, 200)
        rules = [{"time": "01:00", "target": "strip", "on": False},
                 {"time": "09:00", "target": "strip", "on": True, "brightness": 150}]
        status, body = self.req("PUT", "/api/schedule", {"rules": rules})
        self.assertEqual(status, 200)
        # saving the schedule drops the manual override, so the 09:00 rule applies right away
        self.assertEqual(body["desired"], {"on": True, "brightness": 150})
        self.assertEqual(self.device.read_settings().strip_brightness, 150)
        self.now = datetime(2026, 10, 6, 9, 0)  # the next morning the rule still applies
        self.scheduler.tick()
        self.assertEqual(self.device.read_settings().strip_brightness, 150)
        status, body = self.req("POST", "/api/schedule/override", {"on": False})
        self.assertFalse(self.device.read_settings().strip_enabled)
        self.assertTrue(body["override_active"])
        status, _ = self.req("PUT", "/api/schedule", {"rules": [{"time": "7", "on": True}]})
        self.assertEqual(status, 400)

    def test_saving_schedule_clears_override_and_applies_rules_now(self):
        self.req("PUT", "/api/settings/strip", {"enabled": False})  # manual override, newer than any rule
        self.assertFalse(self.device.read_settings().strip_enabled)
        rules = [{"time": "01:00", "target": "strip", "on": False},
                 {"time": "09:00", "target": "strip", "on": True}]
        status, body = self.req("PUT", "/api/schedule", {"rules": rules})
        self.assertEqual(status, 200)
        self.assertTrue(self.device.read_settings().strip_enabled)
        self.assertFalse(body["override_active"])
        self.assertEqual(body["desired"], {"on": True, "brightness": None})

    def test_rejected_schedule_keeps_override(self):
        self.req("PUT", "/api/settings/strip", {"enabled": False})
        status, _ = self.req("PUT", "/api/schedule", {"rules": [{"time": "7", "on": True}]})
        self.assertEqual(status, 400)
        self.assertTrue(self.scheduler.status()["override_active"])

    def test_manual_strip_off_survives_schedule_tick(self):
        self.req("PUT", "/api/schedule", {"rules": [{"time": "09:00", "target": "strip", "on": True}]})
        status, body = self.req("PUT", "/api/settings/strip", {"enabled": False})
        self.assertEqual((status, body["changed"], body["backup"]), (200, True, None))
        self.assertFalse(self.device.read_settings().strip_enabled)
        writes = len(self.fake.writes)
        self.now = datetime(2026, 10, 5, 12, 1)  # the 09:00 "on" rule is earlier today
        self.scheduler.tick()
        self.assertFalse(self.device.read_settings().strip_enabled)
        self.assertEqual(len(self.fake.writes), writes)
        status, body = self.req("PUT", "/api/settings/strip", {"enabled": False})  # nothing new to write
        self.assertEqual((status, body["changed"]), (200, False))

    def test_manual_strip_brightness_persists_across_tick(self):
        self.req("PUT", "/api/schedule", {"rules": [{"time": "09:00", "target": "strip", "on": True,
                                                     "brightness": 150}]})
        self.assertEqual(self.device.read_settings().strip_brightness, 150)
        status, body = self.req("PUT", "/api/settings/strip", {"brightness": 60})
        self.assertEqual((status, body["changed"]), (200, True))
        self.now = datetime(2026, 10, 5, 12, 1)
        self.scheduler.tick()
        s = self.device.read_settings()
        self.assertEqual((s.strip_brightness, s.strip_enabled), (60, True))

    def test_manual_strip_change_works_without_rules(self):
        self.assertEqual(self.config.rules(), [])
        status, body = self.req("PUT", "/api/settings/strip", {"enabled": False, "brightness": 77})
        self.assertEqual((status, body["changed"]), (200, True))
        s = self.device.read_settings()
        self.assertEqual((s.strip_enabled, s.strip_brightness), (False, 77))
        status, _ = self.req("PUT", "/api/settings/strip", {"brightness": 300})
        self.assertEqual(status, 400)
        self.assertEqual(self.device.read_settings().strip_brightness, 77)

    def test_unreadable_backup_is_400(self):
        name = self.backups.save(load("settings_live.bin"), "x")
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "Permission denied")):
            status, body = self.req("POST", f"/api/backups/{name}/restore", {})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_backups_and_restore(self):
        original = load("settings_live.bin")
        self.req("PUT", "/api/settings/fan/4", {"target_c": 38.0})
        status, items = self.req("GET", "/api/backups")
        self.assertEqual(len(items), 1)
        status, body = self.req("POST", f"/api/backups/{items[0]['name']}/restore", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.settings, original)
        status, _ = self.req("POST", "/api/backups/missing.bin/restore", {})
        self.assertEqual(status, 400)
        status, _ = self.req("POST", "/api/backups/..%2Fx.bin/restore", {})
        self.assertEqual(status, 404)

    def test_external_push(self):
        body = {"source": "llm-vm", "sensors": [{"id": "gpu0", "label": "GPU 0", "value": 45, "unit": "°C"}]}
        status, _ = self.req("POST", "/api/external", body, auth=False,
                             headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, resp = self.req("POST", "/api/external", body, auth=False,
                                headers={"Authorization": "Bearer tok"})
        self.assertEqual((status, resp["accepted"]), (200, 1))
        status, resp = self.req("POST", "/api/external", {"source": "other", "sensors": body["sensors"]},
                                auth=False, headers={"Authorization": "Bearer tok"})
        self.assertEqual(status, 400)
        self.assertEqual([r.id for r in self.app.externals.current()], ["llm-vm/gpu0"])

    def test_static_and_404(self):
        status, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>", body)
        status, _ = self.req("GET", "/etc/passwd")
        self.assertEqual(status, 404)

    def test_client_input_never_causes_500(self):
        big = 10 ** 400  # float(big) overflows
        digits = b"1" * 5000  # beyond Python's int-string conversion limit
        cases = [
            ("unhashable mode", "/api/settings/fan/2", {"body": {"mode": []}}),
            ("unhashable mode (object)", "/api/settings/fan/2", {"body": {"mode": {}}}),
            ("huge target_c", "/api/settings/fan/2", {"raw": b'{"target_c": 1' + b"0" * 400 + b"}"}),
            ("huge curve value", "/api/settings/fan/2",
             {"raw": json.dumps({"curve": [[20 + i, big if i == 3 else 50] for i in range(16)]}).encode()}),
            ("int literal over the digit limit", "/api/settings/fan/2", {"raw": b'{"target_c": ' + digits + b"}"}),
            ("deeply nested JSON", "/api/settings/fan/2", {"raw": b"[" * 20000 + b"]" * 20000}),
        ]
        for name, path, kw in cases:
            with self.subTest(name):
                status, resp = self.req("PUT", path, **kw)
                self.assertEqual(status, 400, resp)
                self.assertIn("error", resp)
        self.assertEqual(self.fake.writes, [])

    def test_int_literal_over_digit_limit_in_push_body_is_400(self):
        status, _ = self.req("POST", "/api/external", auth=False, headers={"Authorization": "Bearer tok"},
                             raw=b'{"source": 1' + b"1" * 5000 + b"}")
        self.assertEqual(status, 400)

    def test_history_minutes(self):
        status, body = self.req("GET", "/api/history?minutes=30")
        self.assertEqual(status, 200)
        status, _ = self.req("GET", "/api/history")
        self.assertEqual(status, 200)
        for q in ("minutes=" + "9" * 5000, "minutes=0", "minutes=361", "minutes=abc", "minutes=-5", "minutes="):
            with self.subTest(q=q[:20]):
                status, resp = self.req("GET", "/api/history?" + q)
                self.assertEqual(status, 400, resp)
                self.assertIn("error", resp)

    def test_missing_static_file_is_404(self):
        self.app.static_dir = Path(self.tmp.name, "no-static")
        status, resp = self.req("GET", "/app.js")
        self.assertEqual(status, 404)
        self.assertIn("error", resp)

    def test_push_label_with_surrogate_or_control_char_is_400_and_status_survives(self):
        for label in ("\\ud800", "a\\u0000b", "x\\ny"):
            with self.subTest(label=label):
                raw = ('{"source": "llm-vm", "sensors": [{"id": "gpu0", "label": "%s", "value": 45, "unit": "\u00b0C"}]}'
                       % label).encode()
                status, _ = self.req("POST", "/api/external", auth=False,
                                     headers={"Authorization": "Bearer tok"}, raw=raw)
                self.assertEqual(status, 400)
        status, _ = self.req("GET", "/api/status")
        self.assertEqual(status, 200)

    def _login(self, password):
        return "Basic " + base64.b64encode(b"admin:" + password.encode()).decode()

    def test_failed_login_throttle_returns_429_without_pbkdf2(self):
        real = web.verify_password
        with mock.patch.object(web, "verify_password", side_effect=real) as verify, \
                mock.patch.object(web, "FAIL_DELAY_S", 0):
            self.assertEqual(self.req("GET", "/api/status")[0], 200)  # caches the good session
            self.assertEqual(verify.call_count, 1)
            for i in range(10):
                status, _ = self.req("GET", "/api/status", auth=False,
                                     headers={"Authorization": self._login(f"wrong{i}")})
                self.assertEqual(status, 401)
            self.assertEqual(verify.call_count, 11)
            status, resp = self.req("GET", "/api/status", auth=False,
                                    headers={"Authorization": self._login("wrong-again")})
            self.assertEqual(status, 429, resp)
            self.assertIn("error", resp)
            # even the correct password is not verified (no PBKDF2) while throttled ...
            self.assertEqual(self.req("GET", "/api/status", auth=False,
                                      headers={"Authorization": self._login("pw2")})[0], 429)
            self.assertEqual(verify.call_count, 11)
            # ... but the cached session keeps working
            self.assertEqual(self.req("GET", "/api/status")[0], 200)
            self.assertEqual(verify.call_count, 11)

    def test_failed_push_token_is_logged_with_ip_only(self):
        body = {"source": "llm-vm", "sensors": [{"id": "gpu0", "label": "GPU 0", "value": 45, "unit": "°C"}]}
        with self.assertLogs("aquacontrol.web", "WARNING") as cm:
            status, _ = self.req("POST", "/api/external", body, auth=False,
                                 headers={"Authorization": "Bearer geheimes-token"})
        self.assertEqual(status, 401)
        self.assertEqual(len(cm.output), 1)
        self.assertIn("127.0.0.1", cm.output[0])
        self.assertNotIn("geheimes-token", cm.output[0])

    def test_failed_push_token_throttle_returns_429_without_hashing(self):
        body = {"source": "llm-vm", "sensors": [{"id": "gpu0", "label": "GPU 0", "value": 45, "unit": "°C"}]}
        real = web.token_source
        with mock.patch.object(web, "token_source", side_effect=real) as check:
            for i in range(10):
                status, _ = self.req("POST", "/api/external", body, auth=False,
                                     headers={"Authorization": f"Bearer wrong{i}"})
                self.assertEqual(status, 401)
            self.assertEqual(check.call_count, 10)
            status, resp = self.req("POST", "/api/external", body, auth=False,
                                    headers={"Authorization": "Bearer tok"})  # even the right token waits
            self.assertEqual(status, 429, resp)
            self.assertIn("error", resp)
            self.assertEqual(check.call_count, 10)
        # push failures do not lock the UI login
        self.assertEqual(self.req("GET", "/api/status")[0], 200)
        self.assertEqual(self.req("GET", "/api/status", auth=False,
                                  headers={"Authorization": self.auth})[0], 200)
        self.app._push_failures.clear()
        status, _ = self.req("POST", "/api/external", body, auth=False, headers={"Authorization": "Bearer tok"})
        self.assertEqual(status, 200)

    def test_failed_login_throttle_expires(self):
        with mock.patch.object(web, "FAIL_DELAY_S", 0):
            for i in range(10):
                self.req("GET", "/api/status", auth=False, headers={"Authorization": self._login(f"wrong{i}")})
            self.assertEqual(self.req("GET", "/api/status")[0], 429)
            self.app._failures.clear()
            self.app._failures.extend([time.monotonic() - web.FAIL_WINDOW_S - 1] * 10)  # all outside the window
            self.assertEqual(self.req("GET", "/api/status")[0], 200)

    def test_failed_login_is_logged_without_password(self):
        with mock.patch.object(web, "FAIL_DELAY_S", 0), self.assertLogs("aquacontrol.web", "WARNING") as cm:
            self.req("GET", "/api/status", auth=False, headers={"Authorization": self._login("geheim123")})
        self.assertEqual(len(cm.output), 1)
        self.assertIn("127.0.0.1", cm.output[0])
        self.assertNotIn("geheim123", cm.output[0])

    def test_login_without_free_verification_slot_is_503(self):
        slots = [self.app._verify_slots.acquire() for _ in range(web.MAX_VERIFY_CONCURRENCY)]
        self.assertTrue(all(slots))
        try:
            with mock.patch.object(web, "VERIFY_WAIT_S", 0.05):
                status, resp = self.req("GET", "/api/status")
            self.assertEqual(status, 503, resp)
            self.assertIn("error", resp)
        finally:
            for _ in slots:
                self.app._verify_slots.release()
        self.assertEqual(self.req("GET", "/api/status")[0], 200)

    def test_connection_cap_drops_excess_and_releases_slots(self):
        self.server._conn_slots = threading.BoundedSemaphore(1)
        port = self.server.server_address[1]
        first = socket.create_connection(("127.0.0.1", port), timeout=5)  # idle, holds the only slot
        try:
            deadline = time.monotonic() + 5
            while self.server._conn_slots.acquire(blocking=False) and time.monotonic() < deadline:
                self.server._conn_slots.release()  # slot still free: handler thread not started yet
                time.sleep(0.01)
            second = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                self.assertEqual(second.recv(1), b"")  # closed immediately by the server
            finally:
                second.close()
        finally:
            first.close()
        deadline = time.monotonic() + 5
        while True:  # the slot comes back once the first connection's thread ends
            try:
                status, _ = self.req("GET", "/api/status")
                if status == 200:
                    break
            except OSError:
                pass
            self.assertLess(time.monotonic(), deadline, "slot never released")
            time.sleep(0.05)

    # --- climate API --------------------------------------------------------------------------------
    TOKEN = "eyJ.hunter2-very-secret.sig"

    def configure_ha(self):
        status, _ = self.req("PUT", "/api/climate", {"ha_url": "http://ha.example:8123", "token": self.TOKEN})
        self.assertEqual(status, 200)

    def test_climate_requires_auth(self):
        for method, path in (("GET", "/api/climate"), ("PUT", "/api/climate"), ("POST", "/api/climate/test")):
            with self.subTest(path=path, method=method):
                status, _ = self.req(method, path, {} if method != "GET" else None, auth=False)
                self.assertEqual(status, 401)

    def test_climate_defaults(self):
        status, body = self.req("GET", "/api/climate")
        self.assertEqual(status, 200)
        self.assertEqual(body["config"], DEFAULT_CLIMATE)
        self.assertIs(body["token_set"], False)
        self.assertEqual(body["status"]["state"], "disabled")
        self.assertEqual(body["status"]["events"], [])
        self.assertFalse(body["config"]["enabled"])

    def test_climate_partial_update_is_merged_and_persisted(self):
        status, body = self.req("PUT", "/api/climate", {"enabled": True, "ha_url": "http://ha.example:8123/",
                                                        "on": {"minutes": 7}, "ac": {"temperature": 22.5}})
        self.assertEqual(status, 200, body)
        cfg = body["config"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["ha_url"], "http://ha.example:8123")
        self.assertEqual(cfg["on"], {**DEFAULT_CLIMATE["on"], "minutes": 7})
        self.assertEqual(cfg["ac"]["preset"], "Quiet")  # untouched value survives
        self.assertEqual(cfg["ac"]["temperature"], 22.5)
        disk = json.loads(Path(self.tmp.name, "config.json").read_text())
        self.assertEqual(disk["climate"], cfg)
        status, again = self.req("GET", "/api/climate")
        self.assertEqual(again["config"], cfg)
        self.assertEqual(AppConfig(Path(self.tmp.name, "config.json")).climate_raw(), cfg)

    def test_token_is_stored_in_secrets_and_never_returned(self):
        status, body = self.req("PUT", "/api/climate", {"token": self.TOKEN})
        self.assertEqual(status, 200)
        self.assertIs(body["token_set"], True)
        self.assertEqual(self.config.secrets.get_ha_token(), self.TOKEN)
        self.assertEqual(Path(self.tmp.name, "secrets.json").stat().st_mode & 0o777, 0o600)
        self.req("PUT", "/api/climate", {"enabled": False})  # makes config.json exist
        self.assertNotIn(self.TOKEN, Path(self.tmp.name, "config.json").read_text())
        for method, path, payload in (("GET", "/api/climate", None), ("PUT", "/api/climate", {"enabled": False}),
                                      ("PUT", "/api/climate", {"token": self.TOKEN})):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
            headers = {"Authorization": self.auth, "Content-Type": "application/json"}
            conn.request(method, path, body=None if payload is None else json.dumps(payload).encode(), headers=headers)
            raw = conn.getresponse().read().decode()
            conn.close()
            with self.subTest(path=path, method=method):
                self.assertNotIn(self.TOKEN, raw)
                self.assertNotIn("hunter2", raw)
                self.assertIn('"token_set": true', raw)

    def test_empty_token_clears_and_missing_token_keeps(self):
        self.req("PUT", "/api/climate", {"token": self.TOKEN})
        status, body = self.req("PUT", "/api/climate", {"enabled": False})
        self.assertIs(body["token_set"], True)
        self.assertEqual(self.config.secrets.get_ha_token(), self.TOKEN)
        status, body = self.req("PUT", "/api/climate", {"token": ""})
        self.assertIs(body["token_set"], False)
        self.assertEqual(self.config.secrets.get_ha_token(), "")

    def test_changing_the_url_without_a_new_token_deletes_the_stored_token(self):
        self.configure_ha()
        status, body = self.req("PUT", "/api/climate", {"ha_url": "http://elsewhere.example:8123"})
        self.assertEqual(status, 200, body)
        self.assertIs(body["token_set"], False)
        self.assertIn("Token", body["notice"])
        self.assertIn("neu", body["notice"])
        self.assertEqual(self.config.secrets.get_ha_token(), "")
        self.assertEqual(body["config"]["ha_url"], "http://elsewhere.example:8123")
        self.assertNotIn("notice", self.req("GET", "/api/climate")[1])

    def test_unchanged_or_equivalent_url_keeps_the_token(self):
        self.configure_ha()
        for body in ({"ha_url": "http://ha.example:8123"}, {"ha_url": "http://ha.example:8123/"}, {"enabled": True},
                     {"ha_url": "http://ha.example:8123", "on": {"minutes": 6}}):
            with self.subTest(body=body):
                status, resp = self.req("PUT", "/api/climate", body)
                self.assertEqual(status, 200, resp)
                self.assertIs(resp["token_set"], True)
                self.assertNotIn("notice", resp)
        self.assertEqual(self.config.secrets.get_ha_token(), self.TOKEN)

    def test_changing_the_url_together_with_a_new_token_keeps_the_new_token(self):
        self.configure_ha()
        status, resp = self.req("PUT", "/api/climate", {"ha_url": "http://elsewhere.example:8123", "token": "new.token.value"})
        self.assertIs(resp["token_set"], True)
        self.assertNotIn("notice", resp)
        self.assertEqual(self.config.secrets.get_ha_token(), "new.token.value")

    def test_changing_the_url_without_a_stored_token_has_no_notice(self):
        status, resp = self.req("PUT", "/api/climate", {"ha_url": "http://ha.example:8123"})
        self.assertIs(resp["token_set"], False)
        self.assertNotIn("notice", resp)

    def test_failed_url_change_keeps_the_token(self):
        self.configure_ha()
        status, _ = self.req("PUT", "/api/climate", {"ha_url": "ftp://x"})
        self.assertEqual(status, 400)
        self.assertEqual(self.config.secrets.get_ha_token(), self.TOKEN)

    def test_climate_validation_errors_are_400_and_change_nothing(self):
        self.req("PUT", "/api/climate", {"token": self.TOKEN, "ha_url": "http://ha.example:8123"})
        before = self.config.climate_raw()
        cases = [
            {"on": {"minutes": 0}}, {"off": {"water_c": 40.0}}, {"ac": {"hvac_mode": "heat"}},
            {"ac": {"temperature": 20.3}}, {"ha_url": "ftp://x"}, {"entity_id": "switch.x"},
            {"enabled": "ja"}, {"bogus": 1}, {"on": "x"}, {"on": {"fan_channels": [5]}},
            {"max_switches_per_hour": 0},
            {"token": 5}, {"token": "has space"}, {"token": None},
            {"on": {"minutes": 0}, "token": "valid.token.value"},  # bad config: token must not be stored either
        ]
        for body in cases:
            with self.subTest(body=body):
                status, resp = self.req("PUT", "/api/climate", body)
                self.assertEqual(status, 400, resp)
                self.assertIn("error", resp)
                self.assertNotIn(self.TOKEN, json.dumps(resp))
        self.assertEqual(self.config.climate_raw(), before)
        self.assertEqual(self.config.secrets.get_ha_token(), self.TOKEN)
        status, _ = self.req("PUT", "/api/climate", raw=b"{nope")
        self.assertEqual(status, 400)
        status, _ = self.req("PUT", "/api/climate", [1])
        self.assertEqual(status, 400)

    def test_climate_error_messages_are_german(self):
        status, resp = self.req("PUT", "/api/climate", {"on": {"minutes": 0}})
        self.assertIn("Minuten", resp["error"])

    def test_climate_test_needs_setup(self):
        status, resp = self.req("POST", "/api/climate/test", {})
        self.assertEqual(status, 400)
        self.assertIn("eingerichtet", resp["error"])
        self.assertEqual(self.ha.log, [])

    def test_climate_test_reads_and_switches_nothing(self):
        self.configure_ha()
        self.ha.states[CLIMATE_ENTITY] = {"state": "cool", "attributes": {
            "temperature": 20.5, "preset_mode": "Quiet", "fan_mode": "Automatic"}}
        self.ha.states[HSEL]["state"] = "left"
        self.ha.states[VSEL]["state"] = "down_center"
        status, resp = self.req("POST", "/api/climate/test", {})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp, {"ok": True, "state": "cool", "temperature": 20.5, "preset": "Quiet",
                                "fan_mode": "Automatic", "horizontal": "left", "vertical": "down_center"})
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(sorted(self.ha.gets), sorted([CLIMATE_ENTITY, HSEL, VSEL]))
        self.assertNotIn(self.TOKEN, json.dumps(resp))

    def test_climate_test_ha_error_is_502_without_token(self):
        self.configure_ha()
        self.ha.failing = {"get"}
        status, resp = self.req("POST", "/api/climate/test", {})
        self.assertEqual(status, 502)
        self.assertIn("Home Assistant", resp["error"])
        self.assertNotIn(self.TOKEN, json.dumps(resp))
        self.assertEqual(self.ha.calls, [])

    def test_climate_test_names_the_failing_entity(self):
        self.configure_ha()
        orig = self.ha.get_state

        def get_state(entity_id):
            if entity_id == VSEL:
                raise HAError("Home Assistant: nicht gefunden")
            return orig(entity_id)
        self.ha.get_state = get_state
        status, resp = self.req("POST", "/api/climate/test", {})
        self.assertEqual(status, 502)
        self.assertIn(VSEL, resp["error"])

    # --- climate options (what the Klima tab offers in its drop-downs) ------------------------------------
    FALLBACK = {"hvac_modes": ["cool", "dry", "fan_only"], "preset_modes": ["Normal", "Quiet", "Powerful"],
                "fan_modes": ["Automatic", "1", "2", "3", "4", "5"],
                "horizontal": ["auto", "left", "left_center", "center", "right_center", "right"],
                "vertical": ["swing", "auto", "up", "up_center", "center", "down_center", "down"]}
    LISTS = ("hvac_modes", "preset_modes", "fan_modes", "horizontal", "vertical")

    def fill_ha(self):
        self.ha.states[CLIMATE_ENTITY]["attributes"].update({
            "hvac_modes": ["off", "heat_cool", "cool", "heat", "fan_only", "dry"],
            "preset_modes": ["Normal", "Powerful", "Quiet", "Eco"],
            "fan_modes": ["Automatic", "1", "2", "3", "4", "5"]})
        self.ha.states[HSEL]["attributes"] = {"options": ["auto", "left", "center", "right"]}
        self.ha.states[VSEL]["attributes"] = {"options": ["swing", "auto", "up", "down"]}
        self.ha.extra_states = {
            "climate.bedroom": {"state": "off", "attributes": {"hvac_modes": ["cool", "heat"], "fan_modes": ["low", "high"]}},
            "select.some_other_thing": {"state": "a", "attributes": {"options": ["a", "b"]}},
            "select.bedroom_swing_horizontal": {"state": "a", "attributes": {"options": ["a", "b"]}},
            "light.kitchen": {"state": "on", "attributes": {}},
            "sensor.water": {"state": "31", "attributes": {}}}

    def test_climate_options_require_auth(self):
        status, _ = self.req("GET", "/api/climate/options", auth=False)
        self.assertEqual(status, 401)

    def test_climate_options_fall_back_when_home_assistant_is_not_set_up(self):
        status, resp = self.req("GET", "/api/climate/options")
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["source"], "fallback")
        self.assertIn("eingerichtet", resp["error"])
        for key in self.LISTS:
            self.assertEqual(resp[key], self.FALLBACK[key])
        self.assertEqual(resp["fallback"], self.FALLBACK)
        self.assertEqual((resp["climate_entities"], resp["select_entities"]), ([], []))
        self.assertEqual(self.ha.log, [])

    def test_climate_options_come_from_home_assistant(self):
        self.configure_ha()
        self.fill_ha()
        status, resp = self.req("GET", "/api/climate/options")
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["source"], "ha")
        self.assertIsNone(resp["error"])
        self.assertEqual(resp["hvac_modes"], ["cool", "fan_only", "dry"])   # only what a cooling automation can use
        self.assertEqual(resp["preset_modes"], ["Normal", "Powerful", "Quiet", "Eco"])
        self.assertEqual(resp["fan_modes"], ["Automatic", "1", "2", "3", "4", "5"])
        self.assertEqual(resp["horizontal"], ["auto", "left", "center", "right"])
        self.assertEqual(resp["vertical"], ["swing", "auto", "up", "down"])
        self.assertEqual(resp["climate_entities"], ["climate.bedroom", CLIMATE_ENTITY])
        self.assertEqual(resp["select_entities"], ["select.bedroom_swing_horizontal", HSEL, VSEL])  # only "swing"
        self.assertEqual(resp["fallback"], self.FALLBACK)
        self.assertEqual(self.ha.calls, [])
        self.assertNotIn(self.TOKEN, json.dumps(resp))

    def test_climate_options_list_every_select_when_none_mentions_swing(self):
        self.configure_ha()
        self.fill_ha()
        for eid in (HSEL, VSEL, "select.bedroom_swing_horizontal"):
            self.ha.states.pop(eid, None)
            self.ha.extra_states.pop(eid, None)
        self.ha.states["select.louvre_h"] = {"state": "a", "attributes": {"options": ["a"]}}
        status, resp = self.req("GET", "/api/climate/options")
        self.assertEqual(resp["select_entities"], ["select.louvre_h", "select.some_other_thing"])

    def test_climate_options_fall_back_per_list_when_home_assistant_does_not_say(self):
        self.configure_ha()
        self.ha.states[CLIMATE_ENTITY]["attributes"] = {"preset_modes": [], "fan_modes": "nonsense",
                                                        "hvac_modes": ["heat", "off"]}
        self.ha.states[HSEL]["attributes"] = {"options": ["only", 5, None]}
        status, resp = self.req("GET", "/api/climate/options")
        self.assertEqual(resp["source"], "ha")
        self.assertEqual(resp["hvac_modes"], self.FALLBACK["hvac_modes"])   # nothing usable among heat/off
        self.assertEqual(resp["preset_modes"], self.FALLBACK["preset_modes"])
        self.assertEqual(resp["fan_modes"], self.FALLBACK["fan_modes"])
        self.assertEqual(resp["horizontal"], ["only"])
        self.assertEqual(resp["vertical"], self.FALLBACK["vertical"])

    def test_climate_options_when_home_assistant_fails(self):
        self.configure_ha()
        self.ha.failing = {"get"}
        status, resp = self.req("GET", "/api/climate/options")
        self.assertEqual(status, 200)
        self.assertEqual(resp["source"], "fallback")
        self.assertIn("Home Assistant", resp["error"])
        self.assertNotIn(self.TOKEN, json.dumps(resp))
        for key in self.LISTS:
            self.assertEqual(resp[key], self.FALLBACK[key])

    def test_climate_options_for_another_entity(self):
        self.configure_ha()
        self.fill_ha()
        status, resp = self.req("GET", "/api/climate/options?entity_id=climate.bedroom"
                                       "&horizontal_select=select.bedroom_swing_horizontal")
        self.assertEqual(resp["hvac_modes"], ["cool"])
        self.assertEqual(resp["fan_modes"], ["low", "high"])
        self.assertEqual(resp["preset_modes"], self.FALLBACK["preset_modes"])
        self.assertEqual(resp["horizontal"], ["a", "b"])
        for query in ("entity_id=switch.x", "horizontal_select=climate.x", "vertical_select=select.A%20b"):
            with self.subTest(query=query):
                status, resp = self.req("GET", "/api/climate/options?" + query)
                self.assertEqual(status, 400)

    def test_climate_options_unknown_entity_is_reported(self):
        self.configure_ha()
        self.fill_ha()
        self.req("PUT", "/api/climate", {"entity_id": "climate.gone"})
        status, resp = self.req("GET", "/api/climate/options")
        self.assertEqual(resp["source"], "ha")
        self.assertIn("climate.gone", resp["error"])
        self.assertEqual(resp["hvac_modes"], self.FALLBACK["hvac_modes"])
        self.assertIn("climate.bedroom", resp["climate_entities"])

    def test_climate_options_against_a_real_http_client(self):
        fake = FakeHA()
        self.addCleanup(fake.close)
        fake.reply = (200, json.dumps([
            {"entity_id": CLIMATE_ENTITY, "state": "off", "attributes": {"preset_modes": ["Normal", "Quiet"]}},
            {"entity_id": VSEL, "state": "auto", "attributes": {"options": ["auto", "down"]}},
            {"entity_id": "light.x", "state": "on", "attributes": {}}]).encode())
        self.configure_ha()
        self.app.ha_client_factory = lambda: HAClient(fake.url, self.TOKEN)
        status, resp = self.req("GET", "/api/climate/options")
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["preset_modes"], ["Normal", "Quiet"])
        self.assertEqual(resp["vertical"], ["auto", "down"])
        self.assertEqual(resp["horizontal"], self.FALLBACK["horizontal"])
        self.assertEqual(resp["climate_entities"], [CLIMATE_ENTITY])
        self.assertEqual(resp["select_entities"], [VSEL])
        self.assertEqual([(m, path, auth) for m, path, auth, *_ in fake.requests],
                         [("GET", "/api/states", f"Bearer {self.TOKEN}")])

    def test_climate_status_reflects_the_controller(self):
        self.req("PUT", "/api/climate", {"enabled": True})
        self.climate.tick()
        status, body = self.req("GET", "/api/climate")
        self.assertEqual(body["status"]["state"], "idle")  # water is 31.7 C: nothing to do
        self.assertTrue(body["status"]["enabled"])


if __name__ == "__main__":
    unittest.main()
