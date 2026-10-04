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
        mon.ingest(self.status)
        self.assertTrue(mon.snapshot()["online"])

    def test_run_survives_missing_device(self):
        calls = []
        stop = threading.Event()

        def opener():
            calls.append(1)
            stop.set()
            raise OSError("no device")

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
