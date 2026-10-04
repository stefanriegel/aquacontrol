import base64
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from aquacontrol.auth import hash_password, hash_token
from aquacontrol.backups import BackupStore
from aquacontrol.config import AppConfig
from aquacontrol.device import Device
from aquacontrol.fake import FakeTransport
from aquacontrol.monitor import Monitor
from aquacontrol.schedule import Scheduler
from aquacontrol.sensors import ExternalStore
from aquacontrol.validate import make_check
from aquacontrol.web import App, make_server
from tests.fixtures import load

STATIC = Path(__file__).resolve().parent.parent / "static"


class WebTest(unittest.TestCase):
    def setUp(self):
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
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
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
        self.assertEqual(body["desired"], {"on": True, "brightness": 150})
        self.assertEqual(self.device.read_settings().strip_brightness, 150)  # applied immediately
        status, body = self.req("POST", "/api/schedule/override", {"on": False})
        self.assertFalse(self.device.read_settings().strip_enabled)
        self.assertTrue(body["override_active"])
        status, _ = self.req("PUT", "/api/schedule", {"rules": [{"time": "7", "on": True}]})
        self.assertEqual(status, 400)

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


if __name__ == "__main__":
    unittest.main()
