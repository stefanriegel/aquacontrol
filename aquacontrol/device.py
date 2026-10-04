"""Settings read/write with backup, verification and rollback. All control
operations are serialised by one lock (UI requests and the scheduler share it)."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from .backups import BackupStore
from .protocol import (COMMIT_REPORT, FEATURE_READ_LEN, NAMES_REPORT_ID, NAMES_REPORT_LEN,
                       SETTINGS_REPORT_ID, SETTINGS_REPORT_LEN, Names, ProtocolError, Settings,
                       check_settings_report, decode_names, decode_settings, encode_settings)
from .transport import Transport

log = logging.getLogger(__name__)

# Absolute offsets the device may change on its own between write and read-back.
# Empty until the hardware test (plan task 15) shows otherwise.
VOLATILE_OFFSETS: frozenset[int] = frozenset()


class DeviceError(Exception):
    pass


class VerifyError(DeviceError):
    pass


@dataclass(frozen=True)
class ApplyResult:
    changed: bool
    backup: str | None = None


Check = Callable[[Settings, Settings], None]


class Device:
    def __init__(self, transport: Transport, backups: BackupStore | None, check: Check):
        self._t = transport
        self._backups = backups
        self._check = check
        self._lock = threading.Lock()

    def _read_report(self) -> bytes:
        try:
            report = self._t.get_feature(SETTINGS_REPORT_ID, FEATURE_READ_LEN)[:SETTINGS_REPORT_LEN]
            check_settings_report(report)
        except ProtocolError as e:
            raise DeviceError(f"Einstellungen ungültig gelesen: {e}") from e
        except OSError as e:
            raise DeviceError(f"Gerät nicht erreichbar: {e}") from e
        return report

    def read_report(self) -> bytes:
        with self._lock:
            return self._read_report()

    def read_settings(self) -> Settings:
        return decode_settings(self.read_report())

    def read_names(self) -> Names:
        with self._lock:
            try:
                return decode_names(self._t.get_feature(NAMES_REPORT_ID, NAMES_REPORT_LEN))
            except (OSError, ProtocolError) as e:
                raise DeviceError(f"Namen nicht lesbar: {e}") from e

    def apply(self, mutate: Callable[[Settings], Settings], *, backup: bool = True,
              reason: str = "") -> ApplyResult:
        with self._lock:
            old_report = self._read_report()
            old = decode_settings(old_report)
            new = mutate(old)
            self._check(old, new)
            new_report = encode_settings(new, old_report)
            if new_report == old_report:
                return ApplyResult(changed=False)
            return self._write_verified(old_report, new_report, backup, reason)

    def restore(self, report: bytes, reason: str = "restore") -> ApplyResult:
        check_settings_report(report)
        with self._lock:
            old_report = self._read_report()
            self._check(decode_settings(old_report), decode_settings(report))
            if report == old_report:
                return ApplyResult(changed=False)
            return self._write_verified(old_report, report, True, reason)

    def rewrite_current(self) -> None:
        """Write the current settings back unchanged (hardware self-test only)."""
        with self._lock:
            report = self._read_report()
            self._write(report)
            self._verify(report)

    def _write_verified(self, old_report: bytes, new_report: bytes, backup: bool,
                        reason: str) -> ApplyResult:
        name = self._backups.save(old_report, reason) if backup and self._backups else None
        try:
            self._write(new_report)
            self._verify(new_report)
        except (VerifyError, OSError) as e:
            log.error("write failed (%s), rolling back", e)
            try:
                self._write(old_report)
            except OSError as e2:
                raise DeviceError(f"Schreiben fehlgeschlagen ({e}) und Rücksetzen fehlgeschlagen ({e2})") from e2
            raise DeviceError(f"Schreiben fehlgeschlagen, alter Stand wiederhergestellt: {e}") from e
        log.info("settings written (%s), backup %s", reason or "change", name)
        return ApplyResult(changed=True, backup=name)

    def _write(self, report: bytes) -> None:
        self._t.set_feature(report)
        self._t.write_output(COMMIT_REPORT)

    def _verify(self, expected: bytes) -> None:
        got = self._read_report()
        diff = [i for i in range(SETTINGS_REPORT_LEN) if got[i] != expected[i] and i not in VOLATILE_OFFSETS]
        if diff:
            raise VerifyError(f"Gerät meldet abweichende Bytes an Offsets {diff[:12]}")
