import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"


def js_chunks(*names: str) -> str:
    """Source of top-level functions and consts of app.js (to evaluate in node without a DOM)."""
    src = APP_JS.read_text()
    chunks = []
    for name in names:
        m = re.search(r"^function " + name + r"\(.*?^}\n", src, re.S | re.M)  # function ... up to the closing brace
        if not m:
            m = re.search(r"^const " + name + r" = \[.*?^\];\n", src, re.S | re.M)  # multi-line array
        if not m:
            m = re.search(r"^const " + name + r" = [^\n]*\n", src, re.M)  # one line
        assert m, f"{name} not found in app.js"
        chunks.append(m.group(0))
    return "\n".join(chunks)


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


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ClimateChoicesTest(unittest.TestCase):
    """Where only a fixed choice is valid, the Klima tab offers a drop-down filled from Home Assistant."""

    def test_options_keep_the_saved_value_selectable_and_marked(self):
        self.assertEqual(run_js("choiceOptions", [["a", "b"], "b"]), [["a", "a"], ["b", "b"]])
        self.assertEqual(run_js("choiceOptions", [["a", "b"], "x"]), [["x", "x (gespeichert)"], ["a", "a"], ["b", "b"]])
        self.assertEqual(run_js("choiceOptions", [["a", "b"], None]), [["a", "a"], ["b", "b"]])
        self.assertEqual(run_js("choiceOptions", [["a"], ""]), [["a", "a"]])
        self.assertEqual(run_js("choiceOptions", [[], "x"]), [["x", "x (gespeichert)"]])
        self.assertEqual(run_js("choiceOptions", [["a", "a", "b"], "a"]), [["a", "a"], ["b", "b"]])

    def test_options_can_have_nicer_labels(self):
        self.assertEqual(run_js("choiceOptions", [["cool", "dry"], "dry", {"cool": "Kühlen (cool)"}]),
                         [["cool", "Kühlen (cool)"], ["dry", "dry"]])
        self.assertEqual(run_js("choiceOptions", [["cool"], "fan_only", {"fan_only": "Nur Lüfter"}]),
                         [["fan_only", "Nur Lüfter (gespeichert)"], ["cool", "cool"]])

    def test_fan_channel_labels_use_the_fan_names(self):
        self.assertEqual(run_js("fanChannelLabel", [1, ["Pumpe", "140mm Radiator"]]), "2: 140mm Radiator")
        self.assertEqual(run_js("fanChannelLabel", [0, None]), "1")
        self.assertEqual(run_js("fanChannelLabel", [3, ["a", "b"]]), "4")

    def test_options_note_says_where_the_choices_come_from(self):
        ha = run_js("climateOptionsNote", [{"source": "ha", "error": None}])
        self.assertIn("Home Assistant", ha)
        self.assertNotIn("Eingebaute", ha)
        fallback = run_js("climateOptionsNote", [{"source": "fallback", "error": "Home Assistant: keine Antwort"}])
        self.assertIn("Eingebaute", fallback)
        self.assertIn("Home Assistant: keine Antwort", fallback)
        self.assertIn("Home Assistant: Entity fehlt", run_js("climateOptionsNote", [{"source": "ha", "error": "Home Assistant: Entity fehlt"}]))
        self.assertIn("nicht", run_js("climateOptionsNote", [None]))

    def test_saved_text_shows_the_notice_about_a_deleted_token(self):
        self.assertEqual(run_js("climateSavedText", [{"token_set": True}]), "Gespeichert.")
        text = run_js("climateSavedText", [{"token_set": False, "notice": "Adresse geändert: Token gelöscht"}])
        self.assertIn("Gespeichert.", text)
        self.assertIn("Token gelöscht", text)

    def test_manual_off_pause_and_unconfirmed_are_shown(self):
        status = {"arming_since": None, "owned_since": None, "off_condition_since": None, "cooldown_until": None,
                  "last_error": None, "switches_last_hour": 0, "manual_off_until": 2_000_000_000, "unconfirmed": False}
        lines = run_js("climateTimers", [status, 1_999_990_000], ("fmtDuration",))
        self.assertTrue(any(re.search(r"Automatik pausiert .* spätestens \d\d:\d\d", l) for l in lines), lines)
        lines = run_js("climateTimers", [{**status, "manual_off_until": None, "owned_since": 5, "unconfirmed": True}, 100],
                       ("fmtDuration",))
        self.assertTrue(any("unbestätigt" in l for l in lines), lines)

    def fields(self):
        script = js_chunks("HVAC_LABELS", "CLIMATE_FIELDS") + "\nconsole.log(JSON.stringify(CLIMATE_FIELDS));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [f for f in json.loads(out.stdout) if f.get("path") or f.get("kind") == "token"]

    def test_only_the_address_is_free_text(self):
        kinds = {f.get("path", "token"): f["kind"] for f in self.fields()}
        self.assertEqual([p for p, k in kinds.items() if k == "text"], ["ha_url"])
        for path in ("entity_id", "horizontal_select", "vertical_select", "ac.hvac_mode", "ac.preset", "ac.fan_mode",
                     "ac.horizontal", "ac.vertical"):
            self.assertEqual(kinds[path], "select", path)
        self.assertEqual(kinds["on.fan_channels"], "channels")
        self.assertEqual(kinds["token"], "token")
        for f in self.fields():
            if f["kind"] == "number":
                self.assertIn("min", f)
                self.assertIn("max", f)

    def test_every_drop_down_names_the_list_it_is_filled_from(self):
        lists = {f["path"]: f["list"] for f in self.fields() if f["kind"] == "select"}
        self.assertEqual(lists, {"entity_id": "climate_entities", "horizontal_select": "select_entities",
                                 "vertical_select": "select_entities", "ac.hvac_mode": "hvac_modes",
                                 "ac.preset": "preset_modes", "ac.fan_mode": "fan_modes",
                                 "ac.horizontal": "horizontal", "ac.vertical": "vertical"})

    def test_pause_field_is_present(self):
        self.assertIn("manual_off_pause_minutes", {f.get("path") for f in self.fields()})


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ArmedButtonTest(unittest.TestCase):
    """Dangerous buttons (delete token, restore backup) need a second click within a few seconds."""

    SCRIPT = """
const timers = { pending: [], set(fn, ms) { this.pending.push({ fn, ms }); return this.pending.length; },
                 clear(id) { if (id) this.pending[id - 1] = null; } };
const btn = { textContent: "Token löschen", className: "danger", handlers: [],
              addEventListener(ev, fn) { this.handlers.push(fn); } };
const log = [];
armedClick(btn, "Wirklich löschen?", () => log.push("done"), 5000, timers);
const click = () => btn.handlers[0]();
const out = [];
click(); out.push([btn.textContent, log.length, timers.pending[0].ms]);       // armed
timers.pending[0].fn();                                                        // the 5 s pass
out.push([btn.textContent, btn.className]);                                    // disarmed again
click(); click(); out.push([btn.textContent, log.length]);                     // arm, confirm
click(); out.push([btn.textContent, log.length]);                              // armed again after the action
console.log(JSON.stringify(out));
"""

    def test_second_click_only_counts_within_the_time(self):
        out = subprocess.run(["node", "-e", js_chunks("armedClick") + self.SCRIPT], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [["Wirklich löschen?", 0, 5000], ["Token löschen", "danger"],
                                                  ["Token löschen", 1], ["Wirklich löschen?", 1]])


class NoFreeTextTest(unittest.TestCase):
    def test_the_only_text_input_in_the_ui_is_the_home_assistant_address(self):
        src = APP_JS.read_text()
        self.assertEqual(len(re.findall(r'type:\s*"text"', src)), 1)  # the generic "text" kind of the Klima form
        html = (APP_JS.parent / "index.html").read_text()
        for tag in re.findall(r"<input\b[^>]*>", html):
            self.assertRegex(tag, r'type="(checkbox|range|number|time)"')
        for tag in re.findall(r"<input\b[^>]*>|<textarea\b", src):  # none built from literal markup
            self.fail(tag)


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
