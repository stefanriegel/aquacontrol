import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"


def run_js(function_name: str, args: object, deps: tuple[str, ...] = ()) -> object:
    """Evaluate one top-level function of static/app.js in node (no DOM needed). `deps` are further
    top-level functions it calls."""
    src = APP_JS.read_text()
    chunks = []
    for name in (*deps, function_name):
        m = re.search(rf"^function {name}\(.*?^}}\n", src, re.S | re.M)
        assert m, f"{name} not found in app.js"
        chunks.append(m.group(0))
    script = "\n".join(chunks) + f"\nconsole.log(JSON.stringify({function_name}(...JSON.parse(process.argv[1]))));"
    out = subprocess.run(["node", "-e", script, json.dumps(args)], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FanSaveBodyTest(unittest.TestCase):
    BASE = {"mode": "curve", "sensor": 0, "sensorChanged": False, "min": 20, "max": 90,
            "fixed": 50, "target": 35, "curve": [[1, 2]]}

    def body(self, **over):
        return run_js("fanSaveBody", [{**self.BASE, **over}])

    def test_sensor_is_omitted_unless_the_user_changed_it(self):
        self.assertNotIn("sensor", self.body())
        self.assertEqual(self.body(sensorChanged=True, sensor=2)["sensor"], 2)

    def test_mode_specific_fields(self):
        self.assertEqual(self.body(mode="curve"), {"mode": "curve", "min_percent": 20, "max_percent": 90,
                                                   "curve": [[1, 2]]})
        self.assertEqual(self.body(mode="fixed")["fixed_percent"], 50)
        self.assertEqual(self.body(mode="target")["target_c"], 35)
        self.assertNotIn("mode", self.body(mode="raw-7"))  # display-only modes are never sent


FIELDS = [
    {"group": "Home Assistant"},
    {"path": "enabled", "kind": "bool"},
    {"path": "ha_url", "kind": "text"},
    {"kind": "token"},
    {"path": "on.water_c", "kind": "number"},
    {"path": "on.fan_channels", "kind": "channels"},
    {"path": "ac.hvac_mode", "kind": "select"},
    {"path": "ac.temperature", "kind": "number"},
]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ClimateUiTest(unittest.TestCase):
    def body(self, values, token=""):
        return run_js("climateBody", [FIELDS, values, token])

    VALUES = {"enabled": True, "ha_url": "http://ha.example:8123", "on.water_c": "41.5",
              "on.fan_channels": ["2", 3], "ac.hvac_mode": "dry", "ac.temperature": "21.5"}

    def test_body_is_nested_and_typed(self):
        self.assertEqual(self.body(self.VALUES), {
            "enabled": True, "ha_url": "http://ha.example:8123", "on": {"water_c": 41.5, "fan_channels": [2, 3]},
            "ac": {"hvac_mode": "dry", "temperature": 21.5}})

    def test_token_only_sent_when_typed(self):
        self.assertNotIn("token", self.body(self.VALUES))
        self.assertEqual(self.body(self.VALUES, "abc.def")["token"], "abc.def")

    def test_empty_number_is_sent_as_null_so_the_server_rejects_it(self):
        self.assertIsNone(self.body({**self.VALUES, "on.water_c": ""})["on"]["water_c"])

    def test_duration_text(self):
        for seconds, text in ((0, "0 s"), (45, "45 s"), (60, "1 min"), (300, "5 min"), (3660, "1 h 01 min"),
                              (7200, "2 h 00 min")):
            with self.subTest(seconds=seconds):
                self.assertEqual(run_js("fmtDuration", [seconds]), text)

    def test_timer_lines(self):
        status = {"arming_since": 940, "owned_since": None, "off_condition_since": None, "cooldown_until": None,
                  "last_error": None, "switches_last_hour": 1}
        lines = run_js("climateTimers", [status, 1000], ("fmtDuration",))
        self.assertIn("Einschalt-Bedingung erfüllt seit 1 min", lines)
        self.assertIn("Eigene Schaltvorgänge in der letzten Stunde: 1", lines)
        owned = {**status, "arming_since": None, "owned_since": 100, "off_condition_since": 700}
        lines = run_js("climateTimers", [owned, 1000], ("fmtDuration",))
        self.assertIn("Läuft seit 15 min", lines)
        self.assertIn("Ausschalt-Bedingung erfüllt seit 5 min", lines)
        err = {**status, "last_error": "Home Assistant: kaputt"}
        self.assertIn("Letzter Fehler: Home Assistant: kaputt", run_js("climateTimers", [err, 1000], ("fmtDuration",)))

    def test_test_result_text(self):
        text = run_js("climateTestText", [{"ok": True, "state": "cool", "temperature": 20.5, "preset": "Quiet",
                                           "fan_mode": "Automatic", "horizontal": "left", "vertical": "down_center"}])
        self.assertEqual(text, "Verbunden. Zustand: cool, Soll 20.5 °C, Preset Quiet, Lüfter Automatic, "
                               "Lamellen horizontal left / vertikal down_center")
        self.assertIn("–", run_js("climateTestText", [{"ok": True, "state": "off", "temperature": None}]))


class CspTest(unittest.TestCase):
    """The server sends `default-src 'self'`: no inline scripts, handlers or style attributes."""

    def test_html_has_no_inline_code_or_styles(self):
        html = (APP_JS.parent / "index.html").read_text()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertNotRegex(html, r"\sstyle\s*=")
        self.assertNotIn("javascript:", html)

    def test_js_never_uses_inner_html_or_style_attributes(self):
        src = APP_JS.read_text()
        for needle in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(needle, src)
        self.assertNotRegex(src, r'["\']style["\']\s*:')
        self.assertNotRegex(src, r'setAttribute\(\s*["\']style["\']')

    def test_climate_tab_exists(self):
        html = (APP_JS.parent / "index.html").read_text()
        self.assertIn('data-tab="climate"', html)
        self.assertIn('id="tab-climate"', html)
        self.assertIn("Klima", html)
        self.assertIn("Verbindung testen", html)


if __name__ == "__main__":
    unittest.main()
