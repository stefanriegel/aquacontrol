import gzip
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aquacontrol.monitor import Monitor
from aquacontrol.sensors import Reading
from tests.fixtures import load


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.extra = [Reading("k10temp/Tctl", "CPU", 51.0, "°C"), Reading("llm-vm/p", "P", 200.0, "W")]
        self.mon = Monitor(open_reader=lambda: None, extra=lambda: self.extra, clock=self.clock)
        self.status = load("status.bin")

    def test_offline_before_first_report(self):
        self.assertFalse(self.mon.snapshot()["online"])

    def test_snapshot_online_then_stale(self):
        self.mon.ingest(self.status)
        snap = self.mon.snapshot()
        self.assertTrue(snap["online"])
        self.assertEqual(snap["status"]["temps"][0], 31.72)
        self.assertEqual(snap["sensors"][0]["label"], "CPU")
        self.clock.t += 6
        self.assertFalse(self.mon.snapshot()["online"])

    def test_history_buckets_average(self):
        for i in range(25):  # 25 s of reports -> buckets at t0, t0+10 complete, t0+20 open
            self.mon.ingest(self.status, now=self.clock.t + i)
        self.clock.t += 25
        hist = self.mon.history(60)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["temp1"], 31.72)
        self.assertEqual(hist[0]["fan1_rpm"], 3024)
        self.assertEqual(hist[0]["k10temp/Tctl"], 51.0)
        self.assertNotIn("llm-vm/p", hist[0])  # only temperatures go into history
        self.assertNotIn("temp2", hist[0])     # missing sensor

    def test_extra_sensor_failure_does_not_break_ingest(self):
        def boom():
            raise OSError("hwmon gone")
        mon = Monitor(open_reader=lambda: None, extra=boom, clock=self.clock)
        with self.assertLogs("aquacontrol.monitor", "ERROR"):
            mon.ingest(self.status)
        self.assertTrue(mon.snapshot()["online"])

    def test_extra_sensors_refresh_without_quadro(self):
        snap = self.mon.snapshot()
        self.assertFalse(snap["online"])
        self.assertEqual(snap["sensors"], [])
        self.mon.poll_extra()
        snap = self.mon.snapshot()
        self.assertFalse(snap["online"])  # online refers to the QUADRO only
        self.assertIsNone(snap["status"])
        self.assertEqual(snap["sensors"][0]["value"], 51.0)
        self.extra = [Reading("k10temp/Tctl", "CPU", 55.0, "°C")]
        self.clock.t += 1
        self.mon.poll_extra()  # not due yet (2 s cadence)
        self.assertEqual(self.mon.snapshot()["sensors"][0]["value"], 51.0)
        self.clock.t += 1
        self.mon.poll_extra()
        self.assertEqual(self.mon.snapshot()["sensors"][0]["value"], 55.0)

    def test_extra_sensors_keep_filling_history_while_quadro_is_offline(self):
        self.mon.ingest(self.status)
        for _ in range(30):
            self.clock.t += 1
            self.mon.poll_extra()
        self.assertFalse(self.mon.snapshot()["online"])
        hist = self.mon.history(60)
        self.assertGreaterEqual(len(hist), 2)
        self.assertEqual(hist[-1]["k10temp/Tctl"], 51.0)
        self.assertNotIn("temp1", hist[-1])  # the QUADRO values stopped

    def test_ingest_does_not_double_count_extras(self):
        self.mon.ingest(self.status, now=self.clock.t)
        self.extra = [Reading("k10temp/Tctl", "CPU", 99.0, "°C")]
        self.mon.ingest(self.status, now=self.clock.t + 1)  # extras not due: cached value stays
        self.assertEqual(self.mon.snapshot()["sensors"][0]["value"], 51.0)

    def test_run_polls_extras_while_reader_times_out_and_when_device_is_missing(self):
        calls = []
        stop = threading.Event()

        class Idle:
            def read(self, timeout):
                stop.wait(0.01)
                return None

            def close(self):
                pass

        def extra():
            calls.append(1)
            if len(calls) >= 3:
                stop.set()
            return [Reading("k10temp/Tctl", "CPU", 51.0, "°C")]

        ticks = iter(range(0, 1000, 3))  # every call of the clock advances 3 s: always due
        mon = Monitor(open_reader=Idle, extra=extra, clock=lambda: float(next(ticks)))
        mon.run(stop)
        self.assertGreaterEqual(len(calls), 3)
        self.assertFalse(mon.snapshot()["online"])
        self.assertEqual(mon.snapshot()["sensors"][0]["label"], "CPU")

        calls.clear()
        stop2 = threading.Event()

        def opener():
            raise OSError("no device")

        def extra2():
            calls.append(1)
            stop2.set()
            return []

        with self.assertLogs("aquacontrol.monitor", "WARNING"):
            Monitor(open_reader=opener, extra=extra2, clock=self.clock).run(stop2)
        self.assertEqual(calls, [1])

    def test_run_survives_missing_device(self):
        calls = []
        stop = threading.Event()

        def opener():
            calls.append(1)
            stop.set()
            raise OSError("no device")

        with self.assertLogs("aquacontrol.monitor", "WARNING"):
            Monitor(open_reader=opener, clock=self.clock).run(stop)
        self.assertEqual(calls, [1])

    def test_run_ingests_from_reader(self):
        from aquacontrol.fake import FakeReader
        stop = threading.Event()
        reader = FakeReader(self.status, interval=0.01)
        mon = Monitor(open_reader=lambda: reader, clock=self.clock)
        t = threading.Thread(target=mon.run, args=(stop,))
        t.start()
        for _ in range(100):
            if mon.snapshot()["online"]:
                break
            threading.Event().wait(0.01)
        stop.set()
        t.join(2)
        self.assertTrue(mon.snapshot()["online"])


class HistoryPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "history.json.gz")
        self.clock = Clock()
        self.status = load("status.bin")
        self.mon = Monitor(open_reader=lambda: None, clock=self.clock)

    def write(self, payload):
        with gzip.open(self.path, "wt", encoding="utf-8") as f:
            json.dump(payload, f)

    def fresh(self, **kw):
        return Monitor(open_reader=lambda: None, clock=self.clock, **kw)

    def fill(self, mon, seconds=25):
        for i in range(seconds):  # buckets at t0, t0+10 finished, t0+20 open
            mon.ingest(self.status, now=self.clock.t + i)
        self.clock.t += seconds

    def test_round_trip(self):
        self.fill(self.mon)
        self.mon.save_history(self.path)
        other = self.fresh()
        other.load_history(self.path)
        self.assertEqual(len(other.history(60)), 2)
        self.assertEqual(other.history(60), self.mon.history(60))
        data = json.loads(gzip.decompress(self.path.read_bytes()))
        self.assertEqual((data["version"], data["bucket_s"]), (1, 10))
        self.assertIsInstance(data["saved_at"], (int, float))
        self.assertEqual(len(data["history"]), 2)  # the open bucket is not saved

    def test_loaded_history_continues_with_new_buckets(self):
        self.fill(self.mon)
        self.mon.save_history(self.path)
        other = self.fresh()
        other.load_history(self.path)
        other.ingest(self.status, now=self.clock.t + 30)
        other.ingest(self.status, now=self.clock.t + 41)
        self.assertEqual([h["t"] for h in other.history(60)][-1], int((self.clock.t + 30) // 10 * 10))
        self.assertEqual(len(other.history(60)), 3)

    def test_old_and_malformed_entries_are_dropped_on_load(self):
        now = int(self.clock.t)
        self.write({"version": 1, "bucket_s": 10, "saved_at": now, "history": [
            {"t": now - 6 * 3600 - 10, "temp1": 1.0},  # older than the window
            {"t": "x", "temp1": 2.0},                  # non-numeric t
            {"t": True, "temp1": 2.5},                 # bool is not a timestamp
            {"temp1": 3.0},                            # no t
            "junk",
            {"t": now - 60, "temp1": 4.0, "bad": "x"},
            {"t": now - 50, "temp1": 5.0},
        ]})
        self.mon.load_history(self.path)
        self.assertEqual(self.mon.history(6 * 60), [{"t": now - 60, "temp1": 4.0}, {"t": now - 50, "temp1": 5.0}])

    def test_other_version_or_bucket_size_is_ignored(self):
        now = int(self.clock.t)
        for version, bucket in ((2, 10), (1, 5)):
            with self.subTest(version=version, bucket=bucket):
                self.write({"version": version, "bucket_s": bucket, "saved_at": now,
                            "history": [{"t": now - 10, "temp1": 1.0}]})
                mon = self.fresh()
                with self.assertLogs("aquacontrol.monitor", "INFO"):
                    mon.load_history(self.path)
                self.assertEqual(mon.history(60), [])

    def test_load_keeps_the_maxlen(self):
        now = int(self.clock.t) // 10 * 10
        mon = self.fresh(history_s=60)  # 6 buckets
        self.write({"version": 1, "bucket_s": 10, "saved_at": now,
                    "history": [{"t": now - 10 * i, "temp1": 1.0} for i in range(12, 0, -1)]})
        mon.load_history(self.path)
        self.assertEqual([h["t"] for h in mon.history(60)], [now - 10 * i for i in range(6, 0, -1)])
        self.assertEqual(mon._history.maxlen, 6)

    def test_corrupt_file_warns_and_starts_empty(self):
        good = gzip.compress(json.dumps({"version": 1, "bucket_s": 10, "saved_at": 1, "history": []}).encode())
        cases = {"no gzip": b"das ist kein gzip", "truncated": good[:-6], "no json": gzip.compress(b"{nope"),
                 "wrong shape": gzip.compress(b"[1, 2]"),
                 "history not a list": gzip.compress(b'{"version": 1, "bucket_s": 10, "history": 5}'),
                 "empty": b""}
        for name, raw in cases.items():
            with self.subTest(name):
                self.path.write_bytes(raw)
                with self.assertLogs("aquacontrol.monitor", "WARNING") as logs:
                    self.mon.load_history(self.path)
                self.assertIn("Verlauf", logs.output[0])
                self.assertEqual(self.mon.history(60), [])

    def test_missing_file_is_silent(self):
        with self.assertNoLogs("aquacontrol.monitor", "WARNING"):
            self.mon.load_history(self.path)
        self.assertEqual(self.mon.history(60), [])

    def test_unreadable_path_warns_and_starts_empty(self):
        self.path.mkdir()  # a directory where the file should be
        with self.assertLogs("aquacontrol.monitor", "WARNING"):
            self.mon.load_history(self.path)
        self.assertEqual(self.mon.history(60), [])

    def test_saved_file_has_mode_0640_and_no_temp_files_remain(self):
        self.fill(self.mon)
        self.mon.save_history(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(os.listdir(self.tmp.name), ["history.json.gz"])

    def test_failed_save_leaves_the_old_file_intact_and_no_partial_file(self):
        self.fill(self.mon)
        self.mon.save_history(self.path)
        before = self.path.read_bytes()
        self.fill(self.mon)
        with mock.patch("aquacontrol.monitor.os.fsync", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            self.mon.save_history(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(os.listdir(self.tmp.name), ["history.json.gz"])

    def test_failed_first_save_creates_no_file(self):
        self.fill(self.mon)
        with mock.patch("aquacontrol.monitor.os.replace", side_effect=OSError("boom")), \
                self.assertRaises(OSError):
            self.mon.save_history(self.path)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_save_does_not_hold_the_lock_while_writing(self):
        self.fill(self.mon)
        seen = []
        real = os.fsync

        def fsync(fd):
            seen.append(self.mon._lock.acquire(blocking=False))  # would fail if save held the lock
            if seen[-1]:
                self.mon._lock.release()
            real(fd)

        with mock.patch("aquacontrol.monitor.os.fsync", fsync):
            self.mon.save_history(self.path)
        self.assertEqual(seen, [True])


class PeriodicSaveTest(unittest.TestCase):
    def test_run_saves_every_five_minutes_of_clock_time(self):
        clock = Clock()
        stop = threading.Event()
        reads = []

        class Reader:
            def read(self, timeout):
                reads.append(1)
                clock.t += 100  # fake time: no real sleeping
                if len(reads) >= 14:
                    stop.set()
                return None

            def close(self):
                pass

        mon = Monitor(open_reader=Reader, clock=clock, history_path=Path("/nonexistent/history.json.gz"))
        with mock.patch.object(mon, "save_history") as save:
            mon.run(stop)
        # 14 reads x 100 s = 1400 s: saves after 300, 600, 900 and 1200 s
        self.assertEqual(save.call_count, 4)

    def test_no_save_without_a_path(self):
        clock = Clock()
        stop = threading.Event()

        class Reader:
            def read(self, timeout):
                clock.t += 400
                stop.set()
                return None

            def close(self):
                pass

        mon = Monitor(open_reader=Reader, clock=clock)
        with mock.patch.object(Monitor, "save_history") as save:
            mon.run(stop)
        save.assert_not_called()

    def test_failing_periodic_save_is_logged_and_retried_only_at_the_next_interval(self):
        clock = Clock()
        stop = threading.Event()
        reads = []

        class Reader:
            def read(self, timeout):
                reads.append(1)
                clock.t += 100
                if len(reads) >= 4:
                    stop.set()
                return None

            def close(self):
                pass

        mon = Monitor(open_reader=Reader, clock=clock, history_path=Path("/nonexistent/h.gz"))
        with mock.patch.object(mon, "save_history", side_effect=OSError("disk full")) as save, \
                self.assertLogs("aquacontrol.monitor", "WARNING") as logs:
            mon.run(stop)
        self.assertEqual(save.call_count, 1)
        self.assertIn("Verlauf", logs.output[0])


if __name__ == "__main__":
    unittest.main()
