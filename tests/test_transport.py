import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aquacontrol import transport
from aquacontrol.protocol import COMMIT_REPORT


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


class CommitTest(unittest.TestCase):
    def send(self, **kwargs):
        t = transport.HidrawTransport(find=lambda: "/dev/hidraw9", **kwargs)
        with mock.patch.object(transport.os, "open", return_value=42) as op, \
                mock.patch.object(transport.os, "close") as cl, \
                mock.patch.object(transport.os, "write") as wr, \
                mock.patch.object(transport.fcntl, "ioctl") as io:
            t.send_commit(COMMIT_REPORT)
        op.assert_called_once()
        cl.assert_called_once_with(42)
        return wr, io

    def test_default_commit_is_a_feature_report_like_the_kernel_driver(self):
        wr, io = self.send()
        wr.assert_not_called()
        io.assert_called_once()
        fd, request, buf, _ = io.call_args.args
        self.assertEqual((fd, request), (42, transport.hidiocsfeature(len(COMMIT_REPORT))))
        self.assertEqual(bytes(buf), COMMIT_REPORT)

    def test_commit_as_output_uses_os_write(self):
        wr, io = self.send(commit_as="output")
        io.assert_not_called()
        wr.assert_called_once_with(42, COMMIT_REPORT)

    def test_invalid_commit_as(self):
        with self.assertRaises(ValueError):
            transport.HidrawTransport(commit_as="interrupt")


if __name__ == "__main__":
    unittest.main()
