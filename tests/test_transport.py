import tempfile
import unittest
from pathlib import Path

from aquacontrol import transport


class IoctlNumberTest(unittest.TestCase):
    def test_matches_linux_hidraw_h(self):
        # HIDIOCGFEATURE(len) = _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x07, len)
        self.assertEqual(transport.hidiocgfeature(1013), 0xC3F54807)
        self.assertEqual(transport.hidiocsfeature(961), 0xC3C14806)


class FindHidrawTest(unittest.TestCase):
    def make(self, root: Path, name: str, hid_id: str):
        d = root / name / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME=x\n")

    def test_finds_quadro(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root, "hidraw0", "0003:0000046D:0000C52B")
            self.make(root, "hidraw1", "0003:00000C70:0000F00D")
            self.assertEqual(transport.find_hidraw(root), "/dev/hidraw1")

    def test_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(transport.find_hidraw(tmp))
        self.assertIsNone(transport.find_hidraw("/nonexistent/hidraw"))

    def test_open_without_device_raises(self):
        t = transport.HidrawTransport(find=lambda: None)
        with self.assertRaises(transport.DeviceUnavailable):
            t.get_feature(3, 1013)


if __name__ == "__main__":
    unittest.main()
