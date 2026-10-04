import tempfile
import threading
import time
import unittest

from aquacontrol import protocol as p
from aquacontrol.backups import BackupStore
from aquacontrol.device import Device, DeviceError
from aquacontrol.fake import FakeTransport
from aquacontrol.validate import ValidationError, make_check
from tests.fixtures import load


class DeviceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = load("settings_live.bin")
        self.fake = FakeTransport(self.original, load("names.bin"))
        self.backups = BackupStore(self.tmp.name)
        self.dev = Device(self.fake, self.backups, make_check({0: 25.0}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_writes_commits_and_backs_up(self):
        result = self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0), reason="test")
        self.assertTrue(result.changed)
        self.assertEqual(self.fake.commits, 1)
        self.assertEqual(self.dev.read_settings().controllers[3].target_c, 38.0)
        self.assertEqual(self.backups.load(result.backup), self.original)

    def test_noop_does_not_write(self):
        result = self.dev.apply(lambda s: s)
        self.assertFalse(result.changed)
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.backups.list(), [])

    def test_validation_error_writes_nothing(self):
        with self.assertRaises(ValidationError):
            self.dev.apply(lambda s: p.with_fan(s, 0, min_percent=5.0))
        self.assertEqual(self.fake.writes, [])

    def test_bad_crc_from_device_aborts(self):
        corrupt = bytearray(self.original)
        corrupt[50] ^= 1
        self.fake.settings = bytes(corrupt)
        with self.assertRaises(DeviceError):
            self.dev.apply(lambda s: s)
        self.assertEqual(self.fake.writes, [])

    def test_verify_failure_rolls_back(self):
        self.fake.ignore_writes = True  # device keeps the old settings
        with self.assertRaises(DeviceError) as cm:
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertIn("wiederhergestellt", str(cm.exception))
        self.assertEqual(len(self.fake.writes), 2)
        self.assertEqual(self.fake.writes[1], self.original)  # rollback write

    def test_write_error_rolls_back(self):
        self.fake.fail_next_write = True
        with self.assertRaises(DeviceError):
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertEqual(self.fake.settings, self.original)

    def test_readback_oserror_rolls_back(self):
        self.fake.read_faults = [OSError("fake: read failed")] * 2  # the retry fails as well
        with self.assertRaises(DeviceError) as cm:
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertIn("wiederhergestellt", str(cm.exception))
        self.assertEqual(len(self.fake.writes), 2)
        self.assertEqual(self.fake.writes[1], self.original)
        self.assertEqual(self.fake.settings, self.original)

    def _count_settings_reads(self):
        reads = []
        real_get = self.fake.get_feature

        def get_feature(report_id, length):
            reads.append(report_id)
            return real_get(report_id, length)

        self.fake.get_feature = get_feature
        return reads

    def test_transient_readback_error_is_retried_without_rollback(self):
        for fault in (OSError("fake: read failed"), "corrupt"):
            with self.subTest(fault=fault):
                self.fake.settings, self.fake.writes, self.fake.commits = self.original, [], 0
                self.fake.read_faults = [fault]  # only the first read-back fails
                result = self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0), backup=False)
                self.assertTrue(result.changed)
                self.assertEqual(len(self.fake.writes), 1)  # no rollback write
                self.assertEqual(self.fake.commits, 1)
                self.assertEqual(self.fake.settings, self.fake.writes[0])

    def test_mismatch_rolls_back_without_retry(self):
        self.fake.ignore_writes = True  # read-back succeeds but differs: VerifyError
        reads = self._count_settings_reads()
        with self.assertRaises(DeviceError) as cm:
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0), backup=False)
        self.assertIn("wiederhergestellt", str(cm.exception))
        # initial read + one read-back + one rollback read-back: the mismatch is not read again
        self.assertEqual(len(reads), 3)
        self.assertEqual(len(self.fake.writes), 2)

    def test_readback_crc_corrupt_rolls_back(self):
        self.fake.read_faults = ["corrupt"] * 2
        with self.assertRaises(DeviceError) as cm:
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertIn("wiederhergestellt", str(cm.exception))
        self.assertEqual(len(self.fake.writes), 2)
        self.assertEqual(self.fake.settings, self.original)

    def test_rollback_verification_failure_is_reported(self):
        self.fake.read_faults = [OSError("fake: read failed")] * 2 + ["corrupt"] * 2
        with self.assertRaises(DeviceError) as cm:
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertIn("nicht bestätigt", str(cm.exception))
        self.assertNotIn("wiederhergestellt", str(cm.exception))
        self.assertEqual(len(self.fake.writes), 2)

    def test_rollback_write_failure_is_reported(self):
        self.fake.read_faults = [OSError("fake: read failed")] * 2
        real_set = self.fake.set_feature

        def set_feature(report):
            if self.fake.writes:  # first write applies, the rollback write fails
                raise OSError("fake: rollback write failed")
            real_set(report)

        self.fake.set_feature = set_feature
        with self.assertRaises(DeviceError) as cm:
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertIn("nicht bestätigt", str(cm.exception))

    def test_device_absent(self):
        self.fake.present = False
        with self.assertRaises(DeviceError):
            self.dev.read_settings()
        with self.assertRaises(DeviceError):
            self.dev.apply(lambda s: s)

    def test_no_backup_flag(self):
        result = self.dev.apply(lambda s: p.with_strip(s, enabled=False), backup=False)
        self.assertTrue(result.changed)
        self.assertIsNone(result.backup)
        self.assertEqual(self.backups.list(), [])

    def test_restore(self):
        self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        result = self.dev.restore(self.original)
        self.assertTrue(result.changed)
        self.assertEqual(self.fake.settings, self.original)

    def test_restore_rejects_corrupt_report(self):
        with self.assertRaises(ValueError):
            self.dev.restore(self.original[:-1])

    def test_concurrent_applies_are_serialised(self):
        self.fake.read_delay = 0.05  # without the lock both threads read the same old report
        barrier = threading.Barrier(2)
        errors = []

        def edit(idx, value):
            try:
                barrier.wait()
                self.dev.apply(lambda s: p.with_controller(s, idx, target_c=value), backup=False)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=edit, args=(1, 30.0)), threading.Thread(target=edit, args=(2, 31.0))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        s = self.dev.read_settings()
        self.assertEqual((s.controllers[1].target_c, s.controllers[2].target_c), (30.0, 31.0))

    def test_close_waits_for_in_flight_apply_and_then_refuses_writes(self):
        self.fake.read_delay = 0.1  # an apply takes ~0.3 s (read, write, read-back)
        started = threading.Event()
        result = []

        def edit():
            started.set()
            result.append(self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0), backup=False))

        t = threading.Thread(target=edit)
        t.start()
        started.wait()
        time.sleep(0.05)  # apply is now holding the lock
        self.dev.close()  # must block until the write is finished and verified
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].changed)
        self.assertEqual(self.fake.commits, 1)
        self.assertEqual(self.dev.read_settings().controllers[3].target_c, 38.0)
        t.join()
        writes = len(self.fake.writes)
        for call in (lambda: self.dev.apply(lambda s: p.with_strip(s, enabled=False)),
                     lambda: self.dev.restore(self.original),
                     self.dev.rewrite_current):
            with self.assertRaises(DeviceError) as cm:
                call()
            self.assertEqual(str(cm.exception), "Dienst wird beendet")
        self.assertEqual(len(self.fake.writes), writes)  # lock was released again: no deadlock, no write

    def test_read_names(self):
        self.assertEqual(self.dev.read_names().fans[0], "Pumpe")


if __name__ == "__main__":
    unittest.main()
