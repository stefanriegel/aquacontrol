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

    def test_duplicate_ids_get_device_suffix(self):
        root = Path(self.tmp.name, "dup")
        root.mkdir()
        devs = Path(self.tmp.name, "devs")
        for n, (idx, dev) in enumerate([(0, "1-0051"), (1, "1-0053")]):
            hwmon(root, idx, "spd5118", {"temp1": (None, "4%d000" % (n + 1))})
            (devs / dev).mkdir(parents=True)
            (root / f"hwmon{idx}" / "device").symlink_to(devs / dev)
        hwmon(root, 2, "nvme", {"temp1": ("Composite", "40000")})
        r = read_host_sensors({"spd5118@1-0053/temp1": "DIMM B"}, root)
        self.assertEqual([x.id for x in r], ["spd5118@1-0051/temp1", "spd5118@1-0053/temp1", "nvme/Composite"])
        self.assertEqual([x.label for x in r][:2], ["RAM 1", "DIMM B"])
        self.assertEqual([x.value for x in r][:2], [41.0, 42.0])
        self.assertEqual(read_host_sensors({"spd5118@1-0051/temp1": None}, root)[0].id, "spd5118@1-0053/temp1")

    def test_duplicate_without_device_link_uses_hwmon_name(self):
        root = Path(self.tmp.name, "dup")
        root.mkdir()
        hwmon(root, 3, "spd5118", {"temp1": (None, "41000")})
        hwmon(root, 7, "spd5118", {"temp1": (None, "42000")})
        self.assertEqual([x.id for x in read_host_sensors({}, root)],
                         ["spd5118@hwmon3/temp1", "spd5118@hwmon7/temp1"])

    def test_missing_root(self):
        self.assertEqual(read_host_sensors({}, "/nonexistent"), [])


class FriendlyLabelsTest(unittest.TestCase):
    """A fake hwmon tree like the real host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "hwmon")
        self.root.mkdir()
        devs = Path(self.tmp.name, "devs")
        hwmon(self.root, 0, "nvme", {"temp1": ("Composite", "40000"), "temp2": ("Sensor 1", "41000"),
                                     "temp3": ("Sensor 2", "42000")})
        hwmon(self.root, 1, "k10temp", {"temp1": ("Tctl", "51000"), "temp3": ("Tccd1", "39000"),
                                        "temp4": ("Tccd2", "38000")})
        for idx, dev in ((2, "2-0051"), (3, "2-0050")):  # hwmon numbering is the reverse of the bus addresses
            hwmon(self.root, idx, "spd5118", {"temp1": (None, "4%d000" % idx)})
            (devs / dev).mkdir(parents=True)
            (self.root / f"hwmon{idx}" / "device").symlink_to(devs / dev)
        hwmon(self.root, 5, "amdgpu", {"temp1": ("edge", "35000")})
        hwmon(self.root, 6, "acpitz", {"temp1": (None, "27000")})

    def tearDown(self):
        self.tmp.cleanup()

    def test_derived_labels(self):
        labels = {r.id: r.label for r in read_host_sensors({}, self.root)}
        self.assertEqual(labels, {
            "nvme/Composite": "NVMe", "nvme/Sensor 1": "NVMe Sensor 1", "nvme/Sensor 2": "NVMe Sensor 2",
            "k10temp/Tctl": "CPU", "k10temp/Tccd1": "CPU CCD1", "k10temp/Tccd2": "CPU CCD2",
            "spd5118@2-0050/temp1": "RAM 1", "spd5118@2-0051/temp1": "RAM 2",
            "amdgpu/edge": "iGPU", "acpitz/temp1": "acpitz/temp1"})

    def test_ram_numbering_follows_id_order_not_hwmon_order(self):
        r = {x.id: x for x in read_host_sensors({}, self.root)}
        self.assertEqual(r["spd5118@2-0050/temp1"].value, 43.0)  # hwmon3
        self.assertEqual(r["spd5118@2-0050/temp1"].label, "RAM 1")

    def test_config_label_wins_and_none_hides(self):
        r = {x.id: x.label for x in read_host_sensors(
            {"k10temp/Tctl": "Prozessor", "nvme/Composite": None, "spd5118@2-0050/temp1": "DIMM A"}, self.root)}
        self.assertEqual(r["k10temp/Tctl"], "Prozessor")
        self.assertNotIn("nvme/Composite", r)
        self.assertEqual(r["spd5118@2-0050/temp1"], "DIMM A")
        self.assertEqual(r["k10temp/Tccd1"], "CPU CCD1")

    def test_hidden_or_renamed_neighbours_keep_the_ram_numbers(self):
        r = {x.id: x.label for x in read_host_sensors({"spd5118@2-0050/temp1": None}, self.root)}
        self.assertEqual(r["spd5118@2-0051/temp1"], "RAM 2")

    def test_empty_config_label_falls_back_to_derived(self):
        r = {x.id: x.label for x in read_host_sensors({"k10temp/Tctl": ""}, self.root)}
        self.assertEqual(r["k10temp/Tctl"], "CPU")


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

    def test_rejects_huge_int_and_trailing_newline_id(self):
        for sid, value in (("g", 10**400), ("g\n", 1)):
            body = {"source": "llm-vm", "sensors": [{"id": sid, "value": value, "unit": "°C"}]}
            with self.subTest(sid=sid, value=str(value)[:8]), self.assertRaises(ExternalError):
                self.store.put("llm-vm", body)

    def test_rejects_labels_that_break_json_output(self):
        for label in ("\ud800", "a\x00b", "x\ny", "tab\there", "del\x7f", "c1\x85"):
            body = {"source": "llm-vm", "sensors": [{"id": "g", "label": label, "value": 1, "unit": "°C"}]}
            with self.subTest(label=repr(label)), self.assertRaises(ExternalError):
                self.store.put("llm-vm", body)
        self.assertEqual(self.store.current(), [])

    def test_accepts_umlauts_in_label(self):
        body = {"source": "llm-vm", "sensors": [{"id": "g", "label": "Grafikkarte Süd °C", "value": 1, "unit": "°C"}]}
        self.assertEqual(self.store.put("llm-vm", body), 1)

    def test_rejects_duplicate_ids_in_payload(self):
        s = {"id": "gpu0", "value": 1, "unit": "°C"}
        with self.assertRaises(ExternalError):
            self.store.put("llm-vm", {"source": "llm-vm", "sensors": [s, dict(s, value=2)]})


if __name__ == "__main__":
    unittest.main()
