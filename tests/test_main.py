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

        real_run = main_mod.Scheduler.run

        def run(sched, stop):
            real_run(sched, stop)
            events.append("scheduler-stopped")

        with socket.socket() as probe:  # a free port (the CLI treats 0 as "use the default")
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
        with mock.patch.object(Device, "close", close), mock.patch.object(main_mod.Scheduler, "run", run):
            rc = main_mod.main(["--daemon-config", str(self.daemon), "--app-config", str(self.app),
                                "run", "--fake", str(FIXTURES), "--no-tls", "--listen", "127.0.0.1",
                                "--port", str(port)])
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["scheduler-stopped", "close"])

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


if __name__ == "__main__":
    unittest.main()
