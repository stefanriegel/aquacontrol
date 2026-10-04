"""In-memory QUADRO used by the tests and by `--fake` dev mode."""
from __future__ import annotations

import threading
import time

from .protocol import (COMMIT_REPORT, NAMES_REPORT_ID, SETTINGS_REPORT_ID, SETTINGS_REPORT_LEN,
                       STATUS_REPORT_LEN)
from .transport import DeviceUnavailable


class FakeTransport:
    def __init__(self, settings: bytes, names: bytes | None = None, read_delay: float = 0.0):
        self.settings = settings
        self.names = names
        self.present = True
        self.ignore_writes = False      # simulate firmware that silently drops writes
        self.writes: list[bytes] = []   # every settings report written
        self.commits = 0
        self.fail_next_write = False
        self.read_delay = read_delay    # seconds each settings read takes (widens race windows)
        # Faults for settings reads made after the first write, consumed in order:
        # an Exception instance is raised, "corrupt" returns a CRC-broken report.
        self.read_faults: list[Exception | str] = []
        self._lock = threading.Lock()

    def _check(self) -> None:
        if not self.present:
            raise DeviceUnavailable("fake device absent")

    def get_feature(self, report_id: int, length: int) -> bytes:
        self._check()
        if report_id == SETTINGS_REPORT_ID:
            report = self.settings
            if self.read_delay:
                time.sleep(self.read_delay)
            if self.writes and self.read_faults:
                fault = self.read_faults.pop(0)
                if isinstance(fault, Exception):
                    raise fault
                corrupt = bytearray(report)
                corrupt[50] ^= 1
                return bytes(corrupt)
            return report
        if report_id == NAMES_REPORT_ID and self.names is not None:
            return self.names
        raise OSError(f"fake: unsupported feature report {report_id:#04x}")

    def set_feature(self, report: bytes) -> None:
        self._check()
        with self._lock:
            if self.fail_next_write:
                self.fail_next_write = False
                raise OSError("fake: write failed")
            assert report[0] == SETTINGS_REPORT_ID and len(report) == SETTINGS_REPORT_LEN
            self.writes.append(report)
            if not self.ignore_writes:
                self.settings = report

    def write_output(self, report: bytes) -> None:
        self._check()
        assert report == COMMIT_REPORT
        self.commits += 1


class FakeReader:
    """Replays one status report forever, like the real device does once a second."""

    def __init__(self, status: bytes, interval: float = 1.0):
        assert len(status) == STATUS_REPORT_LEN
        self._status = status
        self._interval = interval
        self._stop = threading.Event()

    def read(self, timeout: float) -> bytes | None:
        if self._stop.wait(min(timeout, self._interval)):
            return None
        return self._status

    def close(self) -> None:
        self._stop.set()
