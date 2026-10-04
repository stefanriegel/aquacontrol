import threading
import unittest

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


if __name__ == "__main__":
    unittest.main()
