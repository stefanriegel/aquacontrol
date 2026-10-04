import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from aquacontrol.backups import BackupError, BackupStore
from tests.fixtures import load


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 10, 4, 12, 0, 0)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class BackupStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BackupStore(self.tmp.name, keep=3, clock=FakeClock())
        self.report = load("settings_live.bin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load(self):
        name = self.store.save(self.report, "Kurve Pumpe")
        self.assertTrue(name.endswith("_kurve-pumpe.bin"))
        self.assertEqual(self.store.load(name), self.report)

    def test_list_newest_first_and_prune_keeps_pinned(self):
        pinned = self.store.save(self.report, "initial", pinned=True)
        names = [self.store.save(self.report, f"n{i}") for i in range(5)]
        listed = [b.name for b in self.store.list()]
        self.assertEqual(listed[:3], list(reversed(names[-3:])))
        self.assertIn(pinned, listed)
        self.assertEqual(len(listed), 4)  # 3 kept + pinned

    def test_rejects_invalid_report(self):
        with self.assertRaises(ValueError):
            self.store.save(self.report[:-1])

    def test_load_rejects_traversal_and_missing(self):
        for bad in ("../etc/passwd", "a/b.bin", "x.txt", ""):
            with self.subTest(bad=bad), self.assertRaises(BackupError):
                self.store.load(bad)
        with self.assertRaises(BackupError):
            self.store.load("missing.bin")

    def test_load_rejects_corrupted_file(self):
        Path(self.tmp.name, "broken.bin").write_bytes(b"\x03" + bytes(960))
        with self.assertRaises(BackupError) as cm:
            self.store.load("broken.bin")
        self.assertIn("beschädigt", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
