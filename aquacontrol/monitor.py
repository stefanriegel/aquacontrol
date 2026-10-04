"""Live state and in-memory history. The QUADRO pushes a status report about once a
second; host and external sensors are sampled on their own cadence, so they keep
updating while the QUADRO is offline."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict
from typing import Callable, Protocol

from .protocol import STATUS_REPORT_ID, STATUS_REPORT_LEN, ProtocolError, Status, decode_status
from .sensors import Reading

log = logging.getLogger(__name__)

OFFLINE_AFTER_S = 5.0
RETRY_S = 5.0
EXTRA_INTERVAL_S = 2.0  # cadence of host/external sensor sampling


class Reader(Protocol):
    def read(self, timeout: float) -> bytes | None: ...
    def close(self) -> None: ...


class Monitor:
    def __init__(self, open_reader: Callable[[], Reader], extra: Callable[[], list[Reading]] = list,
                 bucket_s: int = 10, history_s: int = 6 * 3600, clock: Callable[[], float] = time.time):
        self._open_reader = open_reader
        self._extra = extra
        self._bucket_s = bucket_s
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: tuple[float, Status] | None = None
        self._extra_latest: list[Reading] = []
        self._extra_at: float | None = None
        self._history: deque[dict] = deque(maxlen=history_s // bucket_s)
        self._bucket: tuple[int, dict[str, list[float]]] | None = None

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

    def _idle(self, stop: threading.Event, seconds: float) -> None:
        """Wait up to `seconds`, sampling the extra sensors meanwhile."""
        end = time.monotonic() + seconds
        while True:
            self.poll_extra()
            remaining = end - time.monotonic()
            if remaining <= 0 or stop.wait(min(1.0, remaining)):
                return

    def run(self, stop: threading.Event) -> None:
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
            except OSError as e:
                log.warning("status reader lost: %s", e)
            finally:
                reader.close()
            self._idle(stop, RETRY_S)
