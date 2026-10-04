import unittest
from pathlib import Path

UNIT = Path(__file__).resolve().parent.parent / "deploy" / "aquacontrol.service"


def service_options() -> dict[str, list[str]]:
    section, out = None, {}
    for line in UNIT.read_text().splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Service" and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


class ServiceUnitTest(unittest.TestCase):
    def test_hardening_options(self):
        opts = service_options()
        expected = {
            "ProtectProc": "invisible", "ProcSubset": "pid", "ProtectClock": "yes", "ProtectHostname": "yes",
            "ProtectKernelLogs": "yes", "RestrictSUIDSGID": "yes", "PrivateIPC": "yes", "RemoveIPC": "yes",
            "MemoryDenyWriteExecute": "yes", "SystemCallFilter": "@system-service", "SystemCallErrorNumber": "EPERM",
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(opts.get(key), [value])

    def test_hidraw_stays_reachable(self):
        opts = service_options()
        # PrivateDevices would hide /dev/hidraw*; access is granted through the device policy instead
        self.assertNotIn("PrivateDevices", opts)
        self.assertEqual(opts["DevicePolicy"], ["closed"])
        self.assertEqual(opts["DeviceAllow"], ["char-hidraw rw"])


if __name__ == "__main__":
    unittest.main()
