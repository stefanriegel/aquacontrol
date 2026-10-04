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
from aquacontrol.config import AppConfig
from aquacontrol.device import Device
from aquacontrol.fake import FakeTransport
from aquacontrol.monitor import Monitor
from aquacontrol.schedule import Scheduler
from aquacontrol.sensors import ExternalStore
from aquacontrol.validate import make_check
from aquacontrol import web
from aquacontrol.web import App, make_server
from tests.fixtures import load

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
        self.app = App(self.device, self.monitor, self.scheduler, self.backups, self.config, ExternalStore(),
                       hash_password("pw", iterations=1000), {"llm-vm": hash_token("tok")}, STATIC)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.auth = "Basic " + base64.b64encode(b"admin:pw").decode()

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


if __name__ == "__main__":
    unittest.main()
