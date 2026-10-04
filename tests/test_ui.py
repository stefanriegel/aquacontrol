import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"


def run_js(function_name: str, args: object) -> object:
    """Evaluate one top-level function of static/app.js in node (no DOM needed)."""
    src = APP_JS.read_text()
    m = re.search(rf"^function {function_name}\(.*?^}}\n", src, re.S | re.M)
    assert m, f"{function_name} not found in app.js"
    script = f"{m.group(0)}\nconsole.log(JSON.stringify({function_name}(...JSON.parse(process.argv[1]))));"
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


if __name__ == "__main__":
    unittest.main()
