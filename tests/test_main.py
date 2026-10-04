import argparse
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from aquacontrol import __main__ as main_mod
from aquacontrol.auth import hash_password, hash_token, verify_password
from aquacontrol.device import Device
from tests.fixtures import FIXTURES


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.daemon = Path(self.tmp.name, "daemon.json")
        self.app = Path(self.tmp.name, "config.json")
        self.app.write_text(json.dumps({"backup_dir": str(Path(self.tmp.name, "b")), "schedule": []}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_fake(self):
        args = argparse.Namespace(app_config=str(self.app), fake=str(FIXTURES))
        device, monitor, scheduler, backups, config, externals = main_mod.build(args)
        self.assertEqual(device.read_settings().strip_brightness, 218)
        self.assertEqual(device.read_names().fans[0], "Pumpe")

    def test_build_climate_follows_the_config(self):
        args = argparse.Namespace(app_config=str(self.app), fake=str(FIXTURES))
        device, monitor, scheduler, backups, config, externals = main_mod.build(args)
        controller, factory = main_mod.build_climate(monitor, config)
        self.assertEqual(controller.status()["state"], "disabled")  # off by default
        self.assertIsNone(factory())
        config.patch_climate({"ha_url": "http://ha.example:8123"})
        config.secrets.set_ha_token("tok")
        self.assertIsNotNone(factory())

    def test_set_password(self):
        with mock.patch("getpass.getpass", side_effect=["langes-passwort", "langes-passwort"]), \
                redirect_stdout(io.StringIO()):
            rc = main_mod.main(["--daemon-config", str(self.daemon), "set-password"])
        self.assertEqual(rc, 0)
        self.assertTrue(verify_password("langes-passwort", json.loads(self.daemon.read_text())["password_hash"]))

    def test_set_password_too_short(self):
        with mock.patch("getpass.getpass", side_effect=["kurz", "kurz"]), redirect_stderr(io.StringIO()):
            self.assertEqual(main_mod.main(["--daemon-config", str(self.daemon), "set-password"]), 1)

    def test_add_push_token_prints_token_stores_hash(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            main_mod.main(["--daemon-config", str(self.daemon), "add-push-token", "llm-vm"])
        token = out.getvalue().strip()
        self.assertEqual(json.loads(self.daemon.read_text())["push_tokens"]["llm-vm"], hash_token(token))

    def test_run_refuses_without_password(self):
        self.daemon.write_text("{}")
        with self.assertLogs("aquacontrol", level="ERROR"):
            rc = main_mod.main(["--daemon-config", str(self.daemon), "--app-config", str(self.app),
                                "run", "--fake", str(FIXTURES), "--no-tls"])
        self.assertEqual(rc, 2)

    def test_run_joins_scheduler_and_closes_device_on_sigterm(self):
        self.daemon.write_text(json.dumps({"password_hash": hash_password("langes-passwort", iterations=1000)}))
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        events = []
        real_close = Device.close

        def close(dev):
            events.append("close")
            real_close(dev)

        real_monitor_run = main_mod.Monitor.run
        real_load = main_mod.Monitor.load_history
        real_save = main_mod.Monitor.save_history
        history_paths = []

        def monitor_run(mon, stop):
            real_monitor_run(mon, stop)
            events.append("monitor-stopped")

        def load_history(mon, path=None):
            history_paths.append(("load", Path(path)))
            events.append("history-loaded")
            real_load(mon, path)

        def save_history(mon, path=None):
            history_paths.append(("save", Path(path)))
            events.append("history-saved")
            real_save(mon, path)

        real_run = main_mod.Scheduler.run
        real_climate_run = main_mod.ClimateController.run

        def run(sched, stop):
            real_run(sched, stop)
            events.append("scheduler-stopped")

        def climate_run(ctrl, stop):
            real_climate_run(ctrl, stop)
            events.append("climate-stopped")

        with socket.socket() as probe:  # a free port (the CLI treats 0 as "use the default")
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        joins = []
        real_join = threading.Thread.join

        def join(thread, timeout=None):
            joins.append((thread.name, timeout))
            return real_join(thread, timeout)

        threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
        with mock.patch.object(Device, "close", close), mock.patch.object(main_mod.Scheduler, "run", run), \
                mock.patch.object(main_mod.ClimateController, "run", climate_run), \
                mock.patch.object(main_mod.Monitor, "run", monitor_run), \
                mock.patch.object(main_mod.Monitor, "load_history", load_history), \
                mock.patch.object(main_mod.Monitor, "save_history", save_history), \
                mock.patch.object(threading.Thread, "join", join):
            rc = main_mod.main(["--daemon-config", str(self.daemon), "--app-config", str(self.app),
                                "run", "--fake", str(FIXTURES), "--no-tls", "--listen", "127.0.0.1",
                                "--port", str(port)])
        self.assertEqual(rc, 0)
        # the history is loaded before the monitor thread starts and saved once after it (and the others) stopped,
        # right before the device goes away
        self.assertEqual(events[0], "history-loaded")
        self.assertEqual(sorted(events[1:-2]), ["climate-stopped", "monitor-stopped", "scheduler-stopped"])
        self.assertEqual(events[-2:], ["history-saved", "close"])
        history_file = self.app.parent / "history.json.gz"  # next to config.json
        self.assertEqual(history_paths, [("load", history_file), ("save", history_file)])
        self.assertTrue(history_file.exists())
        # a Home Assistant call can take 10 s per request and a switch-on is a sequence of them: wait up to the
        # 90 s systemd grants before giving up on the thread
        self.assertEqual(sorted(t for t in joins if t[0] in ("scheduler", "climate")),
                         [("climate", 90), ("scheduler", 90)])
        self.assertIn("monitor", [t[0] for t in joins])  # the monitor is stopped before the final save

    def test_a_failing_history_save_does_not_prevent_closing_the_device(self):
        self.daemon.write_text(json.dumps({"password_hash": hash_password("langes-passwort", iterations=1000)}))
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        closed = []
        real_close = Device.close
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
        with mock.patch.object(Device, "close", lambda dev: (closed.append(1), real_close(dev))), \
                mock.patch.object(main_mod.Monitor, "save_history", side_effect=OSError("disk full")), \
                self.assertLogs("aquacontrol", "WARNING") as logs:
            rc = main_mod.main(["--daemon-config", str(self.daemon), "--app-config", str(self.app),
                                "run", "--fake", str(FIXTURES), "--no-tls", "--listen", "127.0.0.1",
                                "--port", str(port)])
        self.assertEqual(rc, 0)
        self.assertEqual(closed, [1])
        self.assertTrue(any("Verlauf" in line for line in logs.output))

    def test_signal_during_startup_still_stops_the_daemon(self):
        """SIGTERM/SIGINT arriving before serve_forever() (here: right after the server socket exists)
        must lead to a clean exit. Runs in a subprocess: without early handlers the signal would
        kill (or interrupt) the test process itself."""
        self.daemon.write_text(json.dumps({"password_hash": hash_password("langes-passwort", iterations=1000)}))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        script = """
import os, signal, sys
from aquacontrol import __main__ as m
real = m.make_server
def make_server(*a, **k):
    server = real(*a, **k)
    os.kill(os.getpid(), getattr(signal, sys.argv[1]))
    return server
m.make_server = make_server
sys.exit(m.main(["--daemon-config", sys.argv[2], "--app-config", sys.argv[3], "run", "--fake", sys.argv[4],
                 "--no-tls", "--listen", "127.0.0.1", "--port", sys.argv[5]]))
"""
        root = Path(__file__).resolve().parent.parent
        for name in ("SIGTERM", "SIGINT"):
            with self.subTest(signal=name):
                proc = subprocess.run([sys.executable, "-c", script, name, str(self.daemon), str(self.app),
                                       str(FIXTURES), str(port)], cwd=root, capture_output=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())


class RunFailureTest(unittest.TestCase):
    """The daemon must not linger half-dead (no HTTP) and must release the device on every failure path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.daemon = Path(self.tmp.name, "daemon.json")
        self.daemon.write_text(json.dumps({"password_hash": hash_password("langes-passwort", iterations=1000)}))
        self.app = Path(self.tmp.name, "config.json")
        self.app.write_text(json.dumps({"backup_dir": str(Path(self.tmp.name, "b")), "schedule": []}))
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.closed = []
        real_close = Device.close

        def close(dev):
            self.closed.append(dev)
            real_close(dev)

        patcher = mock.patch.object(Device, "close", close)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_daemon(self, *extra):
        argv = ["--daemon-config", str(self.daemon), "--app-config", str(self.app), "run", "--fake", str(FIXTURES),
                "--listen", "127.0.0.1", "--port", "0", *extra]
        return main_mod.main(argv)

    def test_a_crashed_http_thread_stops_the_daemon_with_a_failure(self):
        real = main_mod.make_server

        def make_server(*a, **k):
            server = real(*a, **k)
            server.serve_forever = mock.Mock(side_effect=RuntimeError("boom"))
            return server

        timer = threading.Timer(20, os.kill, (os.getpid(), signal.SIGTERM))  # safety net: a hang ends as rc 0
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(main_mod, "make_server", make_server), \
                self.assertLogs("aquacontrol", level="ERROR") as logs:
            rc = self.run_daemon("--no-tls")
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.closed), 1)  # the normal shutdown ran
        self.assertIn("boom", "\n".join(logs.output))

    def test_device_is_closed_when_the_server_cannot_be_created(self):
        with mock.patch.object(main_mod, "make_server", side_effect=OSError("Address already in use")), \
                self.assertRaises(OSError):
            self.run_daemon("--no-tls")
        self.assertEqual(len(self.closed), 1)
        self.assertFalse(Path(self.tmp.name, "history.json.gz").exists())  # nothing loaded, so nothing saved

    def test_device_is_closed_when_build_fails_after_the_device_exists(self):
        with mock.patch.object(main_mod, "Scheduler", side_effect=ValueError("bad rules")), \
                self.assertRaises(ValueError):
            self.run_daemon("--no-tls")
        self.assertEqual(len(self.closed), 1)

    def test_device_is_closed_when_the_certificate_is_missing(self):
        with mock.patch.dict(os.environ, {}, clear=False), self.assertLogs("aquacontrol", level="ERROR"):
            os.environ.pop("CREDENTIALS_DIRECTORY", None)
            rc = self.run_daemon()
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.closed), 1)


if __name__ == "__main__":
    unittest.main()
