import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from aquacontrol import __main__ as main_mod
from aquacontrol.auth import hash_token, verify_password
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


if __name__ == "__main__":
    unittest.main()
