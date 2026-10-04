import tempfile
import unittest
from pathlib import Path

from aquacontrol.sensors import ExternalError, ExternalStore, read_host_sensors


def hwmon(root: Path, idx: int, name: str, temps: dict[str, tuple[str | None, str]]):
    d = root / f"hwmon{idx}"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for chan, (label, value) in temps.items():
        (d / f"{chan}_input").write_text(value + "\n")
        if label is not None:
            (d / f"{chan}_label").write_text(label + "\n")


class HostSensorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        hwmon(root, 0, "nvme", {"temp1": ("Composite", "44850")})
        hwmon(root, 1, "k10temp", {"temp1": ("Tctl", "51000"), "temp3": ("Tccd1", "39750")})
        hwmon(root, 2, "spd5118", {"temp1": (None, "42500")})
        hwmon(root, 4, "quadro", {"temp1": ("Coolant temp", "31720")})
        hwmon(root, 5, "amdgpu", {"temp1": ("edge", "garbage")})
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_all_but_quadro_and_broken(self):
        r = read_host_sensors({}, self.root)
        self.assertEqual([x.id for x in r], ["nvme/Composite", "k10temp/Tctl", "k10temp/Tccd1", "spd5118/temp1"])
        self.assertEqual(r[0].value, 44.85)
        self.assertEqual(r[0].unit, "°C")

    def test_rename_and_hide(self):
        r = read_host_sensors({"k10temp/Tctl": "CPU", "spd5118/temp1": None}, self.root)
        ids = {x.id: x.label for x in r}
        self.assertEqual(ids["k10temp/Tctl"], "CPU")
        self.assertNotIn("spd5118/temp1", ids)

    def test_missing_root(self):
        self.assertEqual(read_host_sensors({}, "/nonexistent"), [])


class ExternalStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = ExternalStore(clock=lambda: self.now)
        self.body = {"source": "llm-vm", "sensors": [
            {"id": "gpu0", "label": "GPU 0", "value": 45.0, "unit": "°C"},
            {"id": "gpu0_power", "label": "GPU 0 Leistung", "value": 250.5, "unit": "W"}]}

    def test_put_and_expire(self):
        self.assertEqual(self.store.put("llm-vm", self.body), 2)
        self.assertEqual([r.id for r in self.store.current()], ["llm-vm/gpu0", "llm-vm/gpu0_power"])
        self.now += 31
        self.assertEqual(self.store.current(), [])
        self.assertAlmostEqual(self.store.sources()["llm-vm"], 31)

    def test_source_must_match_token(self):
        with self.assertRaises(ExternalError):
            self.store.put("other", self.body)

    def test_rejects_bad_payloads(self):
        bad = [
            None, [], {"source": "llm-vm"}, {"source": "llm-vm", "sensors": []},
            {"source": "llm-vm", "sensors": [{"id": "GPU 0!", "value": 1, "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": "hot", "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": float("nan"), "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": True, "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": 1, "unit": "K"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": 1, "unit": "°C"}] * 33},
        ]
        for body in bad:
            with self.subTest(body=str(body)[:60]), self.assertRaises(ExternalError):
                self.store.put("llm-vm", body)


if __name__ == "__main__":
    unittest.main()
