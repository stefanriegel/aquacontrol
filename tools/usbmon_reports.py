#!/usr/bin/env python3
"""Extract HID SET_REPORT writes to the QUADRO from a usbmon pcap and diff settings reports.

Capture on the Proxmox host while aquasuite (VM) talks to the device:
    modprobe usbmon; tcpdump -i usbmon3 -s 0 -w /root/aq.pcap
Then:
    python3 tools/usbmon_reports.py extract /root/aq.pcap out/      # writes 001_feature03.bin, ...
    python3 tools/usbmon_reports.py diff out/003_feature03.bin out/004_feature03.bin
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path

LINKTYPE_USB_LINUX_MMAPPED = 220
REPORT_TYPES = {1: "input", 2: "output", 3: "feature"}
LED_BASE_ABS = 397      # payload 396 + report id
LED_SIZE = 70


@dataclass(frozen=True)
class SetReport:
    seq: int
    ts: float
    report_type: str
    report_id: int
    data: bytes


def parse_pcap(path: str | Path, devnum: int | None = None) -> list[SetReport]:
    raw = Path(path).read_bytes()
    magic, = struct.unpack_from("<I", raw, 0)
    if magic != 0xA1B2C3D4:
        raise ValueError("expected a little-endian classic pcap file")
    linktype, = struct.unpack_from("<I", raw, 20)
    if linktype != LINKTYPE_USB_LINUX_MMAPPED:
        raise ValueError(f"linktype {linktype} is not usbmon (220)")
    off, out = 24, []
    while off + 16 <= len(raw):
        ts_sec, ts_usec, incl, _orig = struct.unpack_from("<IIII", raw, off)
        pkt = raw[off + 16: off + 16 + incl]
        off += 16 + incl
        if len(pkt) < 64:
            continue
        ev_type, xfer, epnum, dev = chr(pkt[8]), pkt[9], pkt[10], pkt[11]
        flag_setup = pkt[14]
        if ev_type != "S" or xfer != 2 or flag_setup != 0 or (devnum is not None and dev != devnum):
            continue
        bm_request_type, b_request, w_value, _w_index, _w_length = struct.unpack_from("<BBHHH", pkt, 40)
        if bm_request_type != 0x21 or b_request != 0x09:   # class/interface OUT, SET_REPORT
            continue
        rtype = REPORT_TYPES.get(w_value >> 8, f"type{w_value >> 8}")
        out.append(SetReport(len(out) + 1, ts_sec + ts_usec / 1e6, rtype, w_value & 0xFF, bytes(pkt[64:])))
    return out


def extract(pcap: str, outdir: str) -> None:
    dest = Path(outdir)
    dest.mkdir(parents=True, exist_ok=True)
    for r in parse_pcap(pcap):
        name = f"{r.seq:03d}_{r.report_type}{r.report_id:02x}.bin"
        (dest / name).write_bytes(r.data)
        print(f"{name}  {len(r.data):4d} bytes  t={r.ts:.3f}")


def describe(offset_abs: int) -> str:
    if LED_BASE_ABS <= offset_abs < LED_BASE_ABS + 8 * LED_SIZE:
        idx, rel = divmod(offset_abs - LED_BASE_ABS, LED_SIZE)
        return f"LED{idx + 1}+{rel}"
    if offset_abs in (959, 960):
        return "CRC"
    return f"payload {offset_abs - 1}"


def diff(a: bytes, b: bytes) -> list[tuple[int, int, int, str]]:
    return [(i, a[i], b[i], describe(i)) for i in range(min(len(a), len(b))) if a[i] != b[i]]


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "extract":
        extract(argv[1], argv[2])
        return 0
    if len(argv) == 3 and argv[0] == "diff":
        for i, x, y, where in diff(Path(argv[1]).read_bytes(), Path(argv[2]).read_bytes()):
            print(f"abs {i:4d}  {x:02x} -> {y:02x}  {where}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
