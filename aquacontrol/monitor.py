"""Live state and in-memory history. The QUADRO pushes a status report about once a
second; host and external sensors are sampled on their own cadence, so they keep
updating while the QUADRO is offline."""
from __future__ import annotations

import gzip
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Protocol

from .protocol import STATUS_REPORT_ID, STATUS_REPORT_LEN, ProtocolError, Status, decode_status
from .sensors import Reading

log = logging.getLogger(__name__)

OFFLINE_AFTER_S = 5.0
RETRY_S = 5.0
EXTRA_INTERVAL_S = 2.0  # cadence of host/external sensor sampling
HISTORY_SAVE_S = 300.0  # cadence of saving the history to disk
HISTORY_VERSION = 1
HISTORY_MODE = 0o640


def _number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class Reader(Protocol):
    def read(self, timeout: float) -> bytes | None: ...
    def close(self) -> None: ...


class Monitor:
    def __init__(self, open_reader: Callable[[], Reader], extra: Callable[[], list[Reading]] = list,
                 bucket_s: int = 10, history_s: int = 6 * 3600, clock: Callable[[], float] = time.time,
                 history_path: Path | str | None = None):
        self._open_reader = open_reader
        self._extra = extra
        self._bucket_s = bucket_s
        self._history_s = history_s
        self._history_path = Path(history_path) if history_path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: tuple[float, Status] | None = None
        self._extra_latest: list[Reading] = []
        self._extra_at: float | None = None
        self._history: deque[dict] = deque(maxlen=history_s // bucket_s)
        self._bucket: tuple[int, dict[str, list[float]]] | None = None
        self._last_save = clock()

    def ingest(self, report: bytes, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        status = decode_status(report)
        self.poll_extra(now)
        with self._lock:
            self._latest = (now, status)
            values = self._bucket_values(now)

            def add(key: str, v: float | None) -> None:
                if v is not None:
                    values.setdefault(key, []).append(v)

            for i, t in enumerate(status.temps):
                add(f"temp{i + 1}", t)
            add("flow", status.flow_lph)
            for i, f in enumerate(status.fans):
                add(f"fan{i + 1}_rpm", f.rpm)
                add(f"fan{i + 1}_percent", f.percent)

    def poll_extra(self, now: float | None = None) -> None:
        """Sample host/external sensors if EXTRA_INTERVAL_S have passed since the last sample.
        Independent of the QUADRO: °C values go into the history either way."""
        now = self._clock() if now is None else now
        with self._lock:
            if self._extra_at is not None and now - self._extra_at < EXTRA_INTERVAL_S:
                return
        try:
            extra = self._extra()
        except Exception:  # sensor glitches must never stop the monitor
            log.exception("reading extra sensors failed")
            extra = []
        with self._lock:
            self._extra_at = now
            self._extra_latest = extra
            values = self._bucket_values(now)
            for r in extra:
                if r.unit == "°C":
                    values.setdefault(r.id, []).append(r.value)

    def _bucket_values(self, now: float) -> dict[str, list[float]]:
        start = int(now // self._bucket_s * self._bucket_s)
        if self._bucket is not None and self._bucket[0] != start:
            self._finish_bucket()
        if self._bucket is None:
            self._bucket = (start, {})
        return self._bucket[1]

    def _finish_bucket(self) -> None:
        start, values = self._bucket
        self._history.append({"t": start, **{k: round(sum(v) / len(v), 2) for k, v in values.items()}})
        self._bucket = None

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            sensors = [asdict(r) for r in self._extra_latest]
            if self._latest is None:
                return {"online": False, "updated": None, "status": None, "sensors": sensors}
            ts, status = self._latest
            return {
                "online": now - ts <= OFFLINE_AFTER_S,
                "updated": ts,
                "status": asdict(status),
                "sensors": sensors,
            }

    def history(self, minutes: int) -> list[dict]:
        cutoff = self._clock() - minutes * 60
        with self._lock:
            return [h for h in self._history if h["t"] >= cutoff]

    def save_history(self, path: Path | str | None = None) -> None:
        """Write the finished buckets (not the open one) atomically to `path` (default: the history_path
        given to the constructor) as gzip-compressed JSON. Raises OSError on failure; an existing file stays intact."""
        target = Path(path if path is not None else self._history_path)
        with self._lock:  # snapshot under the lock, write outside of it
            entries = list(self._history)
        payload = {"version": HISTORY_VERSION, "bucket_s": self._bucket_s, "saved_at": self._clock(),
                   "history": entries}
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                    gz.write(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                raw.flush()
                os.fchmod(raw.fileno(), HISTORY_MODE)
                os.fsync(raw.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_history(self, path: Path | str | None = None) -> None:
        """Replace the history with what `save_history` wrote. A missing file is normal (first start); an
        unreadable or foreign one is logged and ignored. Entries outside the history window are dropped."""
        source = Path(path if path is not None else self._history_path)
        try:
            with gzip.open(source, "rt", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("history"), list):
                raise ValueError("unerwartetes Format")
        except FileNotFoundError:
            return
        except (OSError, EOFError, ValueError) as e:  # BadGzipFile is an OSError, JSON/Unicode errors ValueErrors
            log.warning("Verlauf in %s nicht lesbar, starte leer: %s", source, e)
            return
        if data.get("version") != HISTORY_VERSION or data.get("bucket_s") != self._bucket_s:
            log.info("Verlauf in %s hat ein anderes Format (Version %r, Raster %r s), wird ignoriert",
                        source, data.get("version"), data.get("bucket_s"))
            return
        cutoff = self._clock() - self._history_s
        entries = []
        for h in data["history"]:
            if not isinstance(h, dict) or not _number(h.get("t")) or h["t"] < cutoff:
                continue
            entries.append({k: v for k, v in h.items() if isinstance(k, str) and _number(v)})
        entries.sort(key=lambda h: h["t"])
        with self._lock:
            self._history.clear()
            self._history.extend(entries)  # the maxlen keeps the newest

    def _maybe_save(self) -> None:
        """Save if HISTORY_SAVE_S of (injected) clock time have passed since the last save; never raises."""
        if self._history_path is None:
            return
        now = self._clock()
        if now - self._last_save < HISTORY_SAVE_S:
            return
        self._last_save = now  # a failing disk is retried at the next interval, not on every loop
        try:
            self.save_history()
        except Exception as e:
            log.warning("Verlauf konnte nicht gespeichert werden: %s", e)

    def _idle(self, stop: threading.Event, seconds: float) -> None:
        """Wait up to `seconds`, sampling the extra sensors meanwhile."""
        end = time.monotonic() + seconds
        while True:
            self.poll_extra()
            self._maybe_save()
            remaining = end - time.monotonic()
            if remaining <= 0 or stop.wait(min(1.0, remaining)):
                return

    def run(self, stop: threading.Event) -> None:
        self._last_save = self._clock()
        while not stop.is_set():
            try:
                reader = self._open_reader()
            except OSError as e:
                log.warning("status reader unavailable: %s", e)
                self._idle(stop, RETRY_S)
                continue
            try:
                while not stop.is_set():
                    data = reader.read(1.0)
                    if data and data[0] == STATUS_REPORT_ID and len(data) == STATUS_REPORT_LEN:
                        try:
                            self.ingest(data)
                        except ProtocolError as e:
                            log.warning("bad status report: %s", e)
                    self.poll_extra()
                    self._maybe_save()
            except OSError as e:
                log.warning("status reader lost: %s", e)
            finally:
                reader.close()
            self._idle(stop, RETRY_S)
