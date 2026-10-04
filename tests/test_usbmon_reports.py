import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import load
from tools.usbmon_reports import diff, parse_pcap


def usbmon_packet(ev: str, xfer: int, setup: bytes | None, data: bytes, dev: int = 3) -> bytes:
    hdr = bytearray(64)
    hdr[8] = ord(ev)
    hdr[9] = xfer
    hdr[10] = 0
    hdr[11] = dev
    hdr[14] = 0 if setup is not None else 1
    if setup is not None:
        hdr[40:48] = setup
    struct.pack_into("<I", hdr, 36, len(data))
    return bytes(hdr) + data


def pcap(packets: list[bytes]) -> bytes:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 220)
    for i, p in enumerate(packets):
        out += struct.pack("<IIII", 1000 + i, 0, len(p), len(p)) + p
    return out


class UsbmonTest(unittest.TestCase):
    def test_extracts_feature_and_output_set_reports(self):
        report = load("settings_live.bin")
        commit = bytes.fromhex("02 00 00 00 02 00 00 00 00 34 c6")
        packets = [
            usbmon_packet("S", 2, struct.pack("<BBHHH", 0x21, 0x09, 0x0303, 1, 961), report),
            usbmon_packet("C", 2, None, b""),
            usbmon_packet("S", 2, struct.pack("<BBHHH", 0x21, 0x09, 0x0202, 1, 11), commit),
            usbmon_packet("S", 2, struct.pack("<BBHHH", 0xA1, 0x01, 0x0303, 1, 1013), b""),  # GET_REPORT
            usbmon_packet("C", 1, None, load("status.bin")),                                    # interrupt in
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "x.pcap")
            path.write_bytes(pcap(packets))
            reports = parse_pcap(path)
        self.assertEqual([(r.report_type, r.report_id) for r in reports], [("feature", 3), ("output", 2)])
        self.assertEqual(reports[0].data, report)
        self.assertEqual(reports[1].data, commit)

    def _parse(self, raw: bytes, devnum=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "x.pcap")
            path.write_bytes(raw)
            return parse_pcap(path, devnum)

    def test_truncated_file_is_not_a_pcap(self):
        with self.assertRaisesRegex(ValueError, "not a pcap"):
            self._parse(b"\xd4\xc3\xb2")
        with self.assertRaisesRegex(ValueError, "not a pcap"):
            self._parse(b"")

    def test_devnum_filter_keeps_only_chosen_device(self):
        setup = struct.pack("<BBHHH", 0x21, 0x09, 0x0202, 1, 2)
        raw = pcap([usbmon_packet("S", 2, setup, b"\x01\x02", dev=3),
                    usbmon_packet("S", 2, setup, b"\x03\x04", dev=7)])
        self.assertEqual([r.data for r in self._parse(raw)], [b"\x01\x02", b"\x03\x04"])
        self.assertEqual([r.data for r in self._parse(raw, devnum=7)], [b"\x03\x04"])

    def test_wlength_mismatch_is_skipped_with_warning(self):
        setup = struct.pack("<BBHHH", 0x21, 0x09, 0x0303, 1, 961)
        raw = pcap([usbmon_packet("S", 2, setup, b"\x00" * 100)])   # snaplen-truncated
        with contextlib.redirect_stderr(io.StringIO()) as err:
            reports = self._parse(raw)
        self.assertEqual(reports, [])
        self.assertIn("skipping", err.getvalue())

    def test_diff_labels_led_offsets(self):
        a = load("settings_live.bin")
        b = bytearray(a)
        b[397 + 70 + 5] ^= 0xFF  # LED controller 2, byte 5
        self.assertEqual(diff(a, bytes(b)), [(472, a[472], b[472], "LED2+5")])


if __name__ == "__main__":
    unittest.main()
