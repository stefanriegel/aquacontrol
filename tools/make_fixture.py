#!/usr/bin/env python3
"""Capture one status report from the QUADRO with the device serial zeroed.

Run on the host as root:  python3 tools/make_fixture.py status /tmp/status.bin
The settings (0x03) and names (0x08) reports contain no serial and can be saved
with `python3 -m aquacontrol backup` / copied as-is.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aquacontrol.protocol import STATUS_REPORT_ID, STATUS_REPORT_LEN, scrub_status_serial  # noqa: E402
from aquacontrol.transport import HidrawReader  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "status":
        print(__doc__)
        return 1
    reader = HidrawReader()
    try:
        for _ in range(10):
            data = reader.read(2.0)
            if data and data[0] == STATUS_REPORT_ID and len(data) == STATUS_REPORT_LEN:
                Path(argv[1]).write_bytes(scrub_status_serial(data))
                print(f"saved {argv[1]} (serial zeroed)")
                return 0
    finally:
        reader.close()
    print("no status report received", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
