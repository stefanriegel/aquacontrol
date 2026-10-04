"""Time-based LED control. The desired state is always "what the most recent rule
says", so a missed switch (daemon down at 01:00) is caught up on the next tick."""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from .protocol import with_strip

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
ALL_DAYS = frozenset(range(7))  # Monday = 0, like datetime.weekday()
# Pause before the next device write after the 1st, 2nd, 3rd (and later) consecutive failure.
BACKOFF = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60))


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    at: time
    on: bool
    brightness: int | None = None
    days: frozenset[int] = ALL_DAYS
    target: str = "strip"


@dataclass(frozen=True)
class StripState:
    on: bool
    brightness: int | None = None  # None = keep the current brightness


@dataclass(frozen=True)
class Override:
    state: StripState
    set_at: datetime


def parse_rules(raw: object) -> list[Rule]:
    if not isinstance(raw, list) or len(raw) > 50:
        raise ScheduleError("Zeitplan muss eine Liste mit höchstens 50 Regeln sein")
    rules = []
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ScheduleError(f"Regel {n}: muss ein Objekt sein")
        m = _TIME_RE.fullmatch(str(item.get("time", "")))
        if not m:
            raise ScheduleError(f"Regel {n}: Uhrzeit muss HH:MM sein")
        target = item.get("target", "strip")
        if target != "strip":
            raise ScheduleError(f"Regel {n}: Ziel {target!r} wird noch nicht unterstützt (nur 'strip')")
        on = item.get("on")
        if not isinstance(on, bool):
            raise ScheduleError(f"Regel {n}: 'on' muss true oder false sein")
        brightness = item.get("brightness")
        if brightness is not None and (isinstance(brightness, bool) or not isinstance(brightness, int)
                                       or not 0 <= brightness <= 255):
            raise ScheduleError(f"Regel {n}: Helligkeit muss 0–255 sein")
        days = item.get("days", list(range(7)))
        if not isinstance(days, list) or not days or any(
                isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days):
            raise ScheduleError(f"Regel {n}: Tage müssen eine nicht leere Liste aus 0 (Mo) bis 6 (So) sein")
        rules.append(Rule(time(int(m.group(1)), int(m.group(2))), on, brightness, frozenset(days), target))
    return rules


def rules_to_json(rules: list[Rule]) -> list[dict]:
    out = []
    for r in rules:
        item = {"time": r.at.strftime("%H:%M"), "target": r.target, "on": r.on}
        if r.brightness is not None:
            item["brightness"] = r.brightness
        if r.days != ALL_DAYS:
            item["days"] = sorted(r.days)
        out.append(item)
    return out


def last_rule_before(rules: list[Rule], now: datetime) -> tuple[Rule, datetime] | None:
    for back in range(8):
        day = (now - timedelta(days=back)).date()
        hits = [(datetime.combine(day, r.at), r) for r in rules if day.weekday() in r.days]
        hits = [(ts, r) for ts, r in hits if ts <= now]
        if hits:
            ts, rule = max(hits, key=lambda h: h[0])
            return rule, ts
    return None


def next_change(rules: list[Rule], now: datetime) -> datetime | None:
    for ahead in range(8):
        day = (now + timedelta(days=ahead)).date()
        times = [datetime.combine(day, r.at) for r in rules if day.weekday() in r.days]
        times = [t for t in times if t > now]
        if times:
            return min(times)
    return None


def desired_state(rules: list[Rule], now: datetime, override: Override | None = None) -> StripState | None:
    last = last_rule_before(rules, now)
    if override is not None and (last is None or override.set_at >= last[1]):
        return override.state
    if last is None:
        return None
    rule = last[0]
    return StripState(rule.on, rule.brightness)


class Scheduler:
    def __init__(self, device, get_rules: Callable[[], list[Rule]],
                 clock: Callable[[], datetime] = datetime.now):
        self._device = device
        self._get_rules = get_rules
        self._clock = clock
        self._override: Override | None = None
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self._failures = 0                       # consecutive failed writes
        self._retry_at: datetime | None = None   # no device write before this time
        self._rules_used: list[Rule] | None = None

    def set_override(self, on: bool, brightness: int | None = None) -> None:
        with self._lock:
            self._override = Override(StripState(on, brightness), self._clock())
            self._failures, self._retry_at = 0, None  # a manual change is tried right away

    def tick(self):
        """Apply the desired state. tick() itself keeps the write backoff (so it is testable via the
        injected clock): while backing off it returns None and leaves last_error untouched."""
        now = self._clock()
        rules = list(self._get_rules())
        with self._lock:
            override = self._override
            if rules != self._rules_used:  # new rules are tried right away
                self._rules_used = rules
                self._failures, self._retry_at = 0, None
            if self._retry_at is not None and now < self._retry_at:
                return None
        state = desired_state(rules, now, override)
        if state is None:
            self.last_error = None
            return None
        try:
            result = self._device.apply(
                lambda s: with_strip(s, enabled=state.on, brightness=state.brightness),
                backup=False, reason="schedule")
        except Exception as e:
            with self._lock:
                self._failures += 1
                self._retry_at = now + BACKOFF[min(self._failures, len(BACKOFF)) - 1]
            self.last_error = str(e)
            raise
        with self._lock:
            self._failures, self._retry_at = 0, None
        self.last_error = None
        return result

    def status(self) -> dict:
        now = self._clock()
        rules = self._get_rules()
        with self._lock:
            override = self._override
        state = desired_state(rules, now, override)
        nxt = next_change(rules, now)
        last = last_rule_before(rules, now)
        overridden = override is not None and (last is None or override.set_at >= last[1])
        return {
            "desired": None if state is None else {"on": state.on, "brightness": state.brightness},
            "next_change": None if nxt is None else nxt.isoformat(timespec="minutes"),
            "override_active": overridden,
            "last_error": self.last_error,
        }

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep running; tick() recorded last_error and the backoff
                log.warning("schedule tick failed: %s", e)
            stop.wait(60 - self._clock().second + 1)
