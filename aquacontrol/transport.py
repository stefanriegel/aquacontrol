"""Linux hidraw access to the QUADRO. The device node is looked up on every
operation, so unplugging or handing the device to a VM never leaves a stale path."""
from __future__ import annotations

import fcntl
import os
import select
import time
from pathlib import Path
from typing import Callable, Protocol

HID_ID = "0003:00000C70:0000F00D"
MIN_CTRL_INTERVAL = 0.2  # seconds between control operations, as in the kernel driver

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


def hidiocsfeature(size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, 0x06, size)


def hidiocgfeature(size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, 0x07, size)


class DeviceUnavailable(OSError):
    """The QUADRO is not present (unplugged or passed through to a VM)."""


def find_hidraw(sys_root: str | Path = "/sys/class/hidraw") -> str | None:
    root = Path(sys_root)
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir()):
        try:
            uevent = (entry / "device" / "uevent").read_text()
        except OSError:
            continue
        if f"HID_ID={HID_ID}" in uevent.upper():
            return f"/dev/{entry.name}"
    return None


class Transport(Protocol):
    def get_feature(self, report_id: int, length: int) -> bytes: ...
    def set_feature(self, report: bytes) -> None: ...
    def send_commit(self, report: bytes) -> None: ...


class HidrawTransport:
    def __init__(self, find: Callable[[], str | None] = find_hidraw, commit_as: str = "feature"):
        if commit_as not in ("feature", "output"):
            raise ValueError(f"commit_as muss 'feature' oder 'output' sein, nicht {commit_as!r}")
        self._find = find
        self._commit_as = commit_as
        self._last = 0.0

    def _open(self, flags: int) -> int:
        path = self._find()
        if path is None:
            raise DeviceUnavailable("QUADRO nicht gefunden (abgesteckt oder an eine VM durchgereicht?)")
        return os.open(path, flags)

    def _pace(self) -> None:
        wait = self._last + MIN_CTRL_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _done(self) -> None:
        self._last = time.monotonic()

    def get_feature(self, report_id: int, length: int) -> bytes:
        self._pace()
        fd = self._open(os.O_RDWR)
        try:
            buf = bytearray(length)
            buf[0] = report_id
            n = fcntl.ioctl(fd, hidiocgfeature(length), buf, True)
            return bytes(buf[:n]) if n > 0 else bytes(buf)
        finally:
            os.close(fd)
            self._done()

    def set_feature(self, report: bytes) -> None:
        self._pace()
        fd = self._open(os.O_RDWR)
        try:
            fcntl.ioctl(fd, hidiocsfeature(len(report)), bytearray(report), True)
        finally:
            os.close(fd)
            self._done()

    def send_commit(self, report: bytes) -> None:
        """Send the commit report. Like the kernel driver (aqc_send_ctrl_data) this is a Feature report
        (SET_REPORT, type feature) by default; commit_as="output" sends it as an Output report instead."""
        if self._commit_as == "feature":
            self.set_feature(report)
            return
        self._pace()
        fd = self._open(os.O_RDWR)
        try:
            os.write(fd, report)
        finally:
            os.close(fd)
            self._done()


class HidrawReader:
    """Reads input reports (the device sends a status report about once a second)."""

    def __init__(self, find: Callable[[], str | None] = find_hidraw):
        path = find()
        if path is None:
            raise DeviceUnavailable("QUADRO nicht gefunden")
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def read(self, timeout: float) -> bytes | None:
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self._fd, 256)

    def close(self) -> None:
        os.close(self._fd)
