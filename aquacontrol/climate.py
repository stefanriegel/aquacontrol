"""Climate automation: switch the room air conditioner (via Home Assistant) on when the water cooling
runs at its limit for a while, and off again when the water is clearly cooler. See
docs/superpowers/specs/2026-10-04-aquacontrol-climate-design.md. Manual operation always wins: the
controller only ever switches off an AC it switched on itself and still "owns"."""
from __future__ import annotations

import copy
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .ha import HAClient, HAError

DEFAULT_CLIMATE: dict = {
    "enabled": False,
    "ha_url": "",
    "entity_id": "climate.panasonic_ac_panasonic_ac",
    "horizontal_select": "select.panasonic_ac_panasonic_ac_horizontal_swing_mode",
    "vertical_select": "select.panasonic_ac_panasonic_ac_vertical_swing_mode",
    "on": {"water_c": 40.0, "fan_percent": 85.0, "fan_channels": [2, 3], "minutes": 5},
    "off": {"water_c": 36.0, "minutes": 10},
    "min_on_minutes": 30,
    "min_off_minutes": 15,
    "max_switches_per_hour": 2,
    "manual_off_pause_minutes": 120,
    "ac": {"hvac_mode": "cool", "temperature": 20.0, "preset": "Quiet", "fan_mode": "Automatic",
           "horizontal": "left", "vertical": "down_center"},
}

HVAC_MODES = ("cool", "dry", "fan_only")
PRESETS = ("Normal", "Quiet", "Powerful")
FAN_MODES = ("Automatic", "1", "2", "3", "4", "5")
HORIZONTAL = ("auto", "left", "left_center", "center", "right_center", "right")
VERTICAL = ("swing", "auto", "up", "up_center", "center", "down_center", "down")
MIN_GAP_C = 2.0
MAX_SWITCHES_LIMIT = 20
MANUAL_OFF_PAUSE_RANGE = (10, 480)


class ClimateConfigError(ValueError):
    pass


@dataclass(frozen=True)
class OnConfig:
    water_c: float
    fan_percent: float
    fan_channels: tuple[int, ...]
    minutes: int


@dataclass(frozen=True)
class OffConfig:
    water_c: float
    minutes: int


@dataclass(frozen=True)
class ACConfig:
    hvac_mode: str
    temperature: float
    preset: str
    fan_mode: str
    horizontal: str
    vertical: str


@dataclass(frozen=True)
class ClimateConfig:
    enabled: bool
    ha_url: str
    entity_id: str
    horizontal_select: str
    vertical_select: str
    on: OnConfig
    off: OffConfig
    min_on_minutes: int
    min_off_minutes: int
    max_switches_per_hour: int
    manual_off_pause_minutes: int
    ac: ACConfig

    def to_json(self) -> dict:
        return {
            "enabled": self.enabled, "ha_url": self.ha_url, "entity_id": self.entity_id,
            "horizontal_select": self.horizontal_select, "vertical_select": self.vertical_select,
            "on": {"water_c": self.on.water_c, "fan_percent": self.on.fan_percent,
                   "fan_channels": list(self.on.fan_channels), "minutes": self.on.minutes},
            "off": {"water_c": self.off.water_c, "minutes": self.off.minutes},
            "min_on_minutes": self.min_on_minutes, "min_off_minutes": self.min_off_minutes,
            "max_switches_per_hour": self.max_switches_per_hour,
            "manual_off_pause_minutes": self.manual_off_pause_minutes,
            "ac": {"hvac_mode": self.ac.hvac_mode, "temperature": self.ac.temperature,
                   "preset": self.ac.preset, "fan_mode": self.ac.fan_mode,
                   "horizontal": self.ac.horizontal, "vertical": self.ac.vertical},
        }


def merge_climate(current: dict, patch: dict) -> dict:
    """Deep-merge a partial update onto a config dict (new dict; neither argument is changed)."""
    out = copy.deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_climate(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _section(raw: dict, key: str, allowed: set[str]) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ClimateConfigError(f"{key} muss ein Objekt sein")
    for k in value:
        if k not in allowed:
            raise ClimateConfigError(f"Unbekannte Einstellung: {key}.{k}")
    return value


def _is_number(v: object) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def _number(v: object, what: str) -> float:
    if not _is_number(v):
        raise ClimateConfigError(f"{what} muss eine Zahl sein")
    return float(v)


def _percent(v: object, what: str) -> float:
    f = _number(v, what)
    if not 0 <= f <= 100:
        raise ClimateConfigError(f"{what}: Prozent müssen zwischen 0 und 100 liegen")
    return f


def _minutes(v: object, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 240:
        raise ClimateConfigError(f"{what}: Minuten müssen eine ganze Zahl von 1 bis 240 sein")
    return v


def _choice(v: object, allowed: tuple[str, ...], what: str) -> str:
    if not isinstance(v, str) or v not in allowed:
        raise ClimateConfigError(f"{what} muss eines von {', '.join(allowed)} sein")
    return v


def _option(v: object, what: str) -> str:
    """A name the AC reports as one of its options (preset, fan mode, louvre position). The valid set differs per
    device and comes from Home Assistant, so only the form is checked here."""
    if not isinstance(v, str) or not re.fullmatch(r"[^\x00-\x1f\x7f\s](?:[^\x00-\x1f\x7f]{0,62}[^\x00-\x1f\x7f\s])?", v):
        raise ClimateConfigError(f"{what} muss ein Name aus der Auswahl der Klimaanlage sein (1 bis 64 Zeichen)")
    return v


def _entity(v: object, domain: str, what: str) -> str:
    if not isinstance(v, str) or not re.fullmatch(rf"{domain}\.[a-z0-9_]+", v):
        raise ClimateConfigError(f"{what} muss die Form {domain}.name haben (Kleinbuchstaben, Ziffern, _)")
    return v


def _url(v: object) -> str:
    if not isinstance(v, str):
        raise ClimateConfigError("ha_url muss ein Text sein")
    if v == "":
        return ""
    if not re.fullmatch(r"https?://[^\s/?#@]+(/[^\s?#]*)?", v):
        raise ClimateConfigError("ha_url muss mit http:// oder https:// beginnen, z. B. http://homeassistant.local:8123 "
                                 "(ohne Benutzer und Passwort)")
    return v.rstrip("/")


def parse_climate_config(raw: object) -> ClimateConfig:
    """Validate a climate config dict (missing keys take the defaults) and return it as a ClimateConfig."""
    if not isinstance(raw, dict):
        raise ClimateConfigError("Klima-Konfiguration muss ein Objekt sein")
    top = {"enabled", "ha_url", "entity_id", "horizontal_select", "vertical_select", "on", "off",
           "min_on_minutes", "min_off_minutes", "max_switches_per_hour", "manual_off_pause_minutes", "ac"}
    for k in raw:
        if k not in top:
            raise ClimateConfigError(f"Unbekannte Einstellung: {k}")
    d = DEFAULT_CLIMATE

    def get(src: dict, key: str, default_src: dict):
        return src.get(key, default_src[key])

    enabled = get(raw, "enabled", d)
    if not isinstance(enabled, bool):
        raise ClimateConfigError("enabled muss true oder false sein")
    on_raw = _section(raw, "on", {"water_c", "fan_percent", "fan_channels", "minutes"})
    off_raw = _section(raw, "off", {"water_c", "minutes"})
    ac_raw = _section(raw, "ac", {"hvac_mode", "temperature", "preset", "fan_mode", "horizontal", "vertical"})

    channels = get(on_raw, "fan_channels", d["on"])
    if (not isinstance(channels, list) or not channels or len(set(channels)) != len(channels)
            or any(isinstance(c, bool) or not isinstance(c, int) or not 1 <= c <= 4 for c in channels)):
        raise ClimateConfigError("Kanäle müssen eine Liste verschiedener Zahlen von 1 bis 4 sein")
    on = OnConfig(_number(get(on_raw, "water_c", d["on"]), "Einschalt-Wassertemperatur"),
                  _percent(get(on_raw, "fan_percent", d["on"]), "Einschalt-Lüfterleistung"),
                  tuple(sorted(channels)), _minutes(get(on_raw, "minutes", d["on"]), "Einschalt-Dauer"))
    off = OffConfig(_number(get(off_raw, "water_c", d["off"]), "Ausschalt-Wassertemperatur"),
                    _minutes(get(off_raw, "minutes", d["off"]), "Ausschalt-Dauer"))
    if off.water_c > on.water_c - MIN_GAP_C:
        raise ClimateConfigError("Ausschalt-Temperatur muss mindestens 2 °C unter der Einschalt-Temperatur liegen")

    temperature = get(ac_raw, "temperature", d["ac"])
    if not _is_number(temperature) or not 16 <= temperature <= 30 or (temperature * 2) % 1 != 0:
        raise ClimateConfigError("Temperatur der Klimaanlage muss 16 bis 30 °C in 0,5er-Schritten sein")
    ac = ACConfig(_choice(get(ac_raw, "hvac_mode", d["ac"]), HVAC_MODES, "hvac_mode"),
                  float(temperature),
                  _option(get(ac_raw, "preset", d["ac"]), "Preset"),
                  _option(get(ac_raw, "fan_mode", d["ac"]), "Lüfterstufe"),
                  _option(get(ac_raw, "horizontal", d["ac"]), "horizontal"),
                  _option(get(ac_raw, "vertical", d["ac"]), "vertical"))

    max_switches = get(raw, "max_switches_per_hour", d)
    if isinstance(max_switches, bool) or not isinstance(max_switches, int) or not 1 <= max_switches <= MAX_SWITCHES_LIMIT:
        raise ClimateConfigError(f"max_switches_per_hour muss eine ganze Zahl von 1 bis {MAX_SWITCHES_LIMIT} sein")
    pause = get(raw, "manual_off_pause_minutes", d)
    low, high = MANUAL_OFF_PAUSE_RANGE
    if isinstance(pause, bool) or not isinstance(pause, int) or not low <= pause <= high:
        raise ClimateConfigError(f"manual_off_pause_minutes muss eine ganze Zahl von {low} bis {high} sein")
    return ClimateConfig(
        enabled=enabled,
        ha_url=_url(get(raw, "ha_url", d)),
        entity_id=_entity(get(raw, "entity_id", d), "climate", "entity_id"),
        horizontal_select=_entity(get(raw, "horizontal_select", d), "select", "horizontal_select"),
        vertical_select=_entity(get(raw, "vertical_select", d), "select", "vertical_select"),
        on=on, off=off,
        min_on_minutes=_minutes(get(raw, "min_on_minutes", d), "Mindestlaufzeit"),
        min_off_minutes=_minutes(get(raw, "min_off_minutes", d), "Sperrzeit"),
        max_switches_per_hour=max_switches, manual_off_pause_minutes=pause, ac=ac)


# --------------------------------------------------------------------------------------------------
# State machine

log = logging.getLogger(__name__)

INTERVAL_S = 30
BACKOFF_S = (60, 300, 900)  # pause after the 1st, 2nd, 3rd (and later) consecutive Home Assistant error
EVENT_LIMIT = 20
HOUR_S = 3600
UNAVAILABLE = ("unavailable", "unknown")
READBACK_POLL_S = 0.5    # after switching on, HA shows the new state after about half a second (cloud integration)
READBACK_MAX_S = 10.0
CONFIRM_WINDOW_S = 600   # how long an unconfirmed switch-on is re-checked on the following cycles
TEMP_TOLERANCE_C = 0.25
MISMATCHES_TO_HAND_OVER = 2


def _fmt_c(v: float) -> str:
    return f"{v:.1f} °C"


def _minutes_left(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


def make_client_factory(config) -> Callable[[], HAClient | None]:
    """A factory that follows the live config: an HAClient once URL and token are set, else None.
    `config` is an AppConfig (climate_config() and secrets.get_ha_token())."""
    def factory() -> HAClient | None:
        url, token = config.climate_config().ha_url, config.secrets.get_ha_token()
        return HAClient(url, token) if url and token else None
    return factory


class ClimateController:
    """Decides every INTERVAL_S seconds whether to switch the AC on or off. All times are epoch seconds from the
    injected clock. State lives in memory only: after a daemon restart a running AC counts as foreign.

    tick() holds the lock for its whole run (including Home Assistant calls) and publishes an immutable status
    dict at the end, so status() never waits for a hanging Home Assistant."""

    def __init__(self, get_snapshot: Callable[[], dict], client_factory: Callable[[], object | None],
                 get_config: Callable[[], ClimateConfig], clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], object] = time.sleep):
        self._get_snapshot = get_snapshot
        self._client_factory = client_factory
        self._get_config = get_config
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._owned_since: float | None = None
        self._fingerprint: tuple | None = None
        self._arming_since: float | None = None
        self._off_since: float | None = None
        self._cooldown_until = 0.0
        self._last_seen: str | None = None      # HA state at the last look (None: nothing trustworthy)
        self._manual_off_until: float | None = None  # set: a person switched the AC off, no automatic on until then
        self._mismatches = 0           # consecutive reads that differ from the fingerprint while owned
        self._unconfirmed = False      # owned, but HA has not shown the configured temperature/preset yet
        self._confirm_until: float | None = None  # switched on, HA still says off: keep looking until then
        self._off_pending = False      # our turn_off failed: a later "off" is still our own doing
        self._next_poll = 0.0          # a foreign (manually running) AC is looked at again only from here on
        self._probe_arming = False     # arming just started: look at the AC once right away (see _tick_unowned)
        self._switches: list[float] = []
        self._failures = 0
        self._ha_ok = self._ha_failed = False  # what Home Assistant did during the current tick
        self._retry_at: float | None = None
        self._last_error: str | None = None
        self._events: deque[tuple[float, str]] = deque(maxlen=EVENT_LIMIT)
        self._state = "idle"
        self._reason = "Noch kein Durchlauf"
        self._published = self._build_status(self._clock())

    # --- public -------------------------------------------------------------------------------------
    def status(self) -> dict:
        out = copy.deepcopy(self._published)
        enabled = self._get_config().enabled
        out["enabled"] = enabled
        if not enabled:
            out["state"], out["reason"] = "disabled", "Automatik ist deaktiviert"
        elif out["state"] == "disabled":
            out["state"], out["reason"] = "idle", "Aktiviert, der erste Durchlauf folgt"
        return out

    def tick(self) -> None:
        with self._lock:
            try:
                self._tick(self._clock())
            finally:
                self._published = self._build_status(self._clock())

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # keep running; the next cycle starts from the same in-memory state
                log.exception("climate tick failed")
            stop.wait(INTERVAL_S)

    # --- bookkeeping --------------------------------------------------------------------------------
    def _build_status(self, now: float) -> dict:
        try:
            enabled = self._get_config().enabled
        except Exception:
            enabled = False
        return {
            "enabled": enabled,
            "state": self._state,
            "reason": self._reason,
            "arming_since": self._arming_since if self._owned_since is None else None,
            "owned_since": self._owned_since,
            "cooldown_until": self._cooldown_until if self._owned_since is None and now < self._cooldown_until else None,
            "off_condition_since": self._off_since,
            "manual_off_until": self._manual_off_until,
            "unconfirmed": self._unconfirmed and self._owned_since is not None,
            "retry_at": self._retry_at,
            "last_error": self._last_error,
            "switches_last_hour": self._switch_count(now),
            "events": [{"t": t, "message": m} for t, m in reversed(self._events)],
        }

    def _event(self, now: float, message: str) -> None:
        self._events.append((now, message))
        log.info("climate: %s", message)

    def _switch_count(self, now: float) -> int:
        return len([t for t in self._switches if now - t < HOUR_S])

    def _record_switch(self, now: float) -> None:
        self._switches = [t for t in self._switches if now - t < HOUR_S] + [now]

    def _ok(self) -> None:
        self._failures, self._retry_at, self._last_error = 0, None, None

    def _fail(self, now: float, what: str, error: Exception) -> None:
        self._ha_failed = True
        self._failures += 1
        wait = BACKOFF_S[min(self._failures, len(BACKOFF_S)) - 1]
        self._retry_at = now + wait
        self._last_error = f"{what}: {error}"
        self._reason = f"{error} – nächster Versuch in {_minutes_left(wait)} min"
        self._event(now, f"Fehler ({what}): {error}")

    def _backing_off(self, now: float) -> bool:
        if self._retry_at is None or now >= self._retry_at:
            return False
        self._reason = f"Home Assistant gestört, nächster Versuch in {_minutes_left(self._retry_at - now)} min"
        return True

    @staticmethod
    def _fingerprint_of(st: dict) -> tuple:
        attrs = st.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        temp = attrs.get("temperature")
        temp = float(temp) if _is_number(temp) else None
        return (st.get("state"), temp, attrs.get("preset_mode"))

    @staticmethod
    def _temp_close(a: float | None, b: float | None) -> bool:
        return a is None and b is None or (a is not None and b is not None and abs(a - b) <= TEMP_TOLERANCE_C)

    @staticmethod
    def _preset_equal(a: object, b: object) -> bool:
        return (a.casefold() if isinstance(a, str) else a) == (b.casefold() if isinstance(b, str) else b)

    def _same(self, a: tuple, b: tuple) -> bool:
        """Fingerprints agree: same state, temperature within the tolerance, preset ignoring case."""
        return a[0] == b[0] and self._temp_close(a[1], b[1]) and self._preset_equal(a[2], b[2])

    def _reflects(self, st: dict, cfg: ClimateConfig) -> bool:
        """HA shows the AC running in the configured mode with the configured temperature and preset."""
        state, temp, preset = self._fingerprint_of(st)
        return (state == cfg.ac.hvac_mode and self._temp_close(temp, cfg.ac.temperature)
                and self._preset_equal(preset, cfg.ac.preset))

    def _take_ownership(self, now: float, st: dict, confirmed: bool) -> None:
        self._owned_since, self._fingerprint = now, self._fingerprint_of(st)
        self._arming_since = self._off_since = self._confirm_until = None
        self._unconfirmed, self._mismatches, self._off_pending = not confirmed, 0, False
        self._last_seen = st.get("state")

    def _read(self, now: float, client, entity_id: str) -> dict | None:
        try:
            st = client.get_state(entity_id)
        except HAError as e:
            self._fail(now, "Status abfragen", e)
            return None
        self._ha_ok = True  # not _ok(): a failing service call right after must still escalate the backoff
        return st

    # --- the state machine --------------------------------------------------------------------------
    def _tick(self, now: float) -> None:
        self._ha_ok = self._ha_failed = False
        self._decide(now)
        if self._ha_ok and not self._ha_failed:  # Home Assistant answered and the whole decision went through
            self._ok()

    def _decide(self, now: float) -> None:
        cfg = self._get_config()
        if not cfg.enabled:
            if self._owned_since is not None:
                self._event(now, "Automatik deaktiviert, Besitz abgegeben (die Klimaanlage bleibt unverändert)")
            self._owned_since = self._fingerprint = self._arming_since = self._off_since = None
            self._last_seen = self._manual_off_until = self._confirm_until = None
            self._off_pending = self._unconfirmed = self._probe_arming = False
            self._mismatches = 0
            self._failures, self._retry_at = 0, None
            self._state, self._reason = "disabled", "Automatik ist deaktiviert"
            return

        water, fans_ok, why_not = self._inputs(self._get_snapshot(), cfg)
        on_met = water is not None and water >= cfg.on.water_c and fans_ok
        owned = self._owned_since is not None
        if owned:
            self._arming_since = None
            met = water is not None and water <= cfg.off.water_c
            self._off_since = (self._off_since if self._off_since is not None else now) if met else None
        else:
            self._off_since = None
            if on_met:
                if self._arming_since is None:
                    self._arming_since, self._probe_arming = now, True
            else:  # the on-condition is interrupted: what was seen of the AC is outdated
                self._arming_since, self._next_poll, self._last_seen = None, 0.0, None
            if (self._manual_off_until is not None and water is not None and water <= cfg.off.water_c):
                # the pause waits for cool water, not for any interruption: the water must be cool again
                self._manual_off_until = None
                self._event(now, f"Wasser ist wieder kühl (≤ {_fmt_c(cfg.off.water_c)}): "
                                 "die Pause nach dem Ausschalten von Hand ist beendet")

        client = self._client_factory()
        if client is None:
            self._probe_arming = False  # nobody to ask: a stale flag would cause a read at some later tick
            self._state = "owned" if owned else "idle"
            self._reason = "Home Assistant ist nicht eingerichtet (URL oder Token fehlt)"
            return
        if owned:
            self._tick_owned(now, cfg, client, water)
        else:
            self._tick_unowned(now, cfg, client, water, why_not)

    @staticmethod
    def _inputs(snap: dict, cfg: ClimateConfig) -> tuple[float | None, bool, str]:
        """(water temperature or None, all fan conditions met, why the on-condition is not met)."""
        status = snap.get("status") if snap.get("online") else None
        if not status:
            return None, False, "QUADRO ist offline, es wird nicht eingeschaltet"
        temps = status.get("temps") or []
        water = temps[0] if temps else None
        if not _is_number(water):
            return None, False, "Wassertemperatur unbekannt, es wird nicht eingeschaltet"
        fans = status.get("fans") or []
        low = []
        for ch in cfg.on.fan_channels:
            pct = fans[ch - 1].get("percent") if ch - 1 < len(fans) else None
            if not _is_number(pct) or pct < cfg.on.fan_percent:
                low.append(ch)
        if water < cfg.on.water_c:
            why = f"Wasser {_fmt_c(water)}, Einschalten ab {_fmt_c(cfg.on.water_c)}"
        elif low:
            why = f"Lüfter Kanal {', '.join(map(str, low))} unter {cfg.on.fan_percent:g} %"
        else:
            why = ""
        return float(water), not low, why

    def _tick_owned(self, now: float, cfg: ClimateConfig, client, water: float | None) -> None:
        self._state = "owned"
        if self._backing_off(now):
            return
        st = self._read(now, client, cfg.entity_id)
        if st is None:
            return
        if st.get("state") in UNAVAILABLE:
            self._mismatches = 0  # not a reading of the AC: it breaks a streak of mismatches
            self._reason = "Klimaanlage ist in Home Assistant gerade nicht verfügbar, es wird nichts geschaltet"
            return
        self._last_seen = st.get("state")
        fp = self._fingerprint_of(st)
        if self._unconfirmed and self._reflects(st, cfg):  # the configured values arrived late: now it is certain
            self._fingerprint, self._unconfirmed, self._mismatches = fp, False, 0
            self._event(now, "Einschalten bestätigt: Home Assistant zeigt jetzt die eingestellten Werte")
        elif self._same(fp, self._fingerprint):
            self._mismatches = 0
        elif fp[0] == "off" and self._off_pending:  # our turn_off timed out, but HA did execute it
            self._finish_off(now, cfg, "Klimaanlage ist aus: das Ausschalten war als Fehler gemeldet worden, "
                                       "gilt als eigenes Ausschalten")
            return
        else:
            self._mismatches += 1
            if self._mismatches < MISMATCHES_TO_HAND_OVER:  # a stale or glitchy read must not take the AC away
                self._reason = "Klimaanlage weicht von den eingestellten Werten ab, die nächste Prüfung entscheidet"
                return
            self._hand_over(now, cfg, st)
            return
        off_at = None if self._off_since is None else self._off_since + cfg.off.minutes * 60
        run_at = self._owned_since + cfg.min_on_minutes * 60
        if off_at is not None and now >= off_at and now >= run_at:
            self._turn_off(now, cfg, client, water)
        elif off_at is None:
            self._reason = ("Klimaanlage läuft (von aquacontrol eingeschaltet)" if water is not None else
                            "Klimaanlage läuft, QUADRO offline oder Wasser unbekannt: bleibt an")
            if self._unconfirmed:
                self._reason += " – unbestätigt: eingestellte Werte noch nicht in Home Assistant sichtbar"
        else:
            self._reason = (f"Wasser kühl genug, Ausschalten in frühestens "
                            f"{_minutes_left(max(off_at, run_at) - now)} min")

    def _hand_over(self, now: float, cfg: ClimateConfig, st: dict) -> None:
        state = st.get("state")
        self._owned_since = self._fingerprint = self._arming_since = self._off_since = None
        self._off_pending = self._unconfirmed = False
        self._mismatches = 0
        self._last_seen = state
        self._cooldown_until = now + cfg.min_off_minutes * 60  # never fight a manual change straight away
        if state == "off":
            self._manual_off(now, cfg, f"Handbetrieb übernommen: Klimaanlage wurde von Hand ausgeschaltet")
            return
        self._event(now, f"Handbetrieb übernommen: Klimaanlage steht jetzt auf {state}, aquacontrol schaltet nichts mehr")
        self._state = "cooldown"
        self._reason = f"Handbetrieb übernommen, Sperrzeit noch {cfg.min_off_minutes} min"

    def _manual_off(self, now: float, cfg: ClimateConfig, what: str) -> None:
        """A person switched the AC off: do not switch it on again until the water was cool again (<= off.water_c on a
        tick with valid data, see _decide), or at the latest after the configured pause."""
        self._manual_off_until = now + cfg.manual_off_pause_minutes * 60
        self._arming_since = None
        self._event(now, f"{what} – Automatik pausiert bis das Wasser wieder kühl ist "
                         f"(spätestens {self._clock_text(self._manual_off_until)})")
        self._manual_off_status(cfg)

    def _manual_off_status(self, cfg: ClimateConfig) -> None:
        self._state = "cooldown"
        self._reason = ("Von Hand ausgeschaltet – Automatik pausiert bis das Wasser wieder kühl ist "
                        f"(≤ {_fmt_c(cfg.off.water_c)}, spätestens {self._clock_text(self._manual_off_until)})")

    @staticmethod
    def _clock_text(epoch: float) -> str:
        return time.strftime("%H:%M", time.localtime(epoch))

    def _turn_off(self, now: float, cfg: ClimateConfig, client, water: float | None) -> None:
        try:
            client.call("climate", "turn_off", {"entity_id": cfg.entity_id})
        except HAError as e:
            self._off_pending = True  # HA may have executed it anyway: a later "off" is still ours
            self._fail(now, "Ausschalten", e)
            return
        self._ha_ok = True
        self._finish_off(now, cfg, f"Klimaanlage ausgeschaltet (Wasser {_fmt_c(water)} ≤ {_fmt_c(cfg.off.water_c)} "
                                   f"seit {cfg.off.minutes} min)")

    def _finish_off(self, now: float, cfg: ClimateConfig, message: str) -> None:
        self._record_switch(now)  # counts as a switch, but is never blocked by the limit
        self._owned_since = self._fingerprint = self._off_since = None
        self._off_pending = self._unconfirmed = False
        self._mismatches = 0
        self._last_seen = "off"
        self._cooldown_until = now + cfg.min_off_minutes * 60
        self._event(now, message)
        self._state = "cooldown"
        self._reason = f"Sperrzeit nach dem Ausschalten: noch {cfg.min_off_minutes} min"

    def _tick_unowned(self, now: float, cfg: ClimateConfig, client, water: float | None, why_not: str) -> None:
        probe, self._probe_arming = self._probe_arming, False
        if self._manual_off_until is not None:
            if now < self._manual_off_until:
                self._probe_arming = False
                self._manual_off_status(cfg)
                return
            self._manual_off_until = None
            self._event(now, f"Pause nach dem Ausschalten von Hand abgelaufen ({cfg.manual_off_pause_minutes} min)")
        self._recheck_confirmation(now, cfg, client)
        if self._owned_since is not None:  # it showed the configured values after all
            return
        if probe and self._probe(now, cfg, client):
            return
        if now < self._cooldown_until:
            self._state = "cooldown"
            self._reason = f"Sperrzeit: Einschalten frühestens in {_minutes_left(self._cooldown_until - now)} min"
            return
        if self._arming_since is None:
            self._state = "idle"
            self._reason = why_not or "Bedingungen nicht erfüllt"
            return
        waited = now - self._arming_since
        if waited < cfg.on.minutes * 60:
            self._state = "arming"
            self._reason = (f"Bedingung erfüllt seit {int(waited // 60)} min, "
                            f"Einschalten nach {cfg.on.minutes} min")
            return
        recent = sorted(t for t in self._switches if now - t < HOUR_S)
        if len(recent) >= cfg.max_switches_per_hour:
            free_at = recent[len(recent) - cfg.max_switches_per_hour] + HOUR_S
            self._state = "idle"
            self._reason = (f"Schaltlimit erreicht ({len(recent)} Schaltvorgänge in der letzten Stunde), "
                            f"Einschalten frühestens in {_minutes_left(free_at - now)} min")
            return
        if now < self._next_poll:  # a manually running AC was seen recently: do not ask every cycle
            self._state = "idle"
            return
        self._state = "arming"
        if self._backing_off(now):
            return
        st = self._read(now, client, cfg.entity_id)
        if st is None:
            return
        state = st.get("state")
        seen, self._last_seen = self._last_seen, state
        if state == "off" and seen is not None and seen != "off" and seen not in UNAVAILABLE:
            self._manual_off(now, cfg, "Klimaanlage wurde von Hand ausgeschaltet")  # it was running before
            return
        if state != "off":
            self._next_poll = now + cfg.on.minutes * 60
            self._state = "idle"
            self._reason = ("Klimaanlage ist in Home Assistant nicht verfügbar, es wird nichts geschaltet"
                            if state in UNAVAILABLE else
                            f"Klimaanlage läuft bereits (Handbetrieb, Zustand {state}), aquacontrol schaltet nichts")
            return
        self._turn_on(now, cfg, client, water)

    def _probe(self, now: float, cfg: ClimateConfig, client) -> bool:
        """One look at the AC when arming starts, so that a manually running AC is known before the arming time is
        over: a person who switches it off during arming, the lockout or the switch limit is then noticed as a manual
        OFF. Returns True when that was the case (the pause has started)."""
        if self._backing_off(now):
            return False
        st = self._read(now, client, cfg.entity_id)
        if st is None:
            return False
        state = st.get("state")
        seen, self._last_seen = self._last_seen, state
        if state == "off" and seen is not None and seen != "off" and seen not in UNAVAILABLE:
            self._manual_off(now, cfg, "Klimaanlage wurde von Hand ausgeschaltet")
            return True
        if state != "off":
            self._next_poll = now + cfg.on.minutes * 60  # the regular look at the end of arming is not skipped
        return False

    def _turn_on(self, now: float, cfg: ClimateConfig, client, water: float | None) -> None:
        ac, e = cfg.ac, cfg.entity_id
        steps = [
            ("climate", "set_hvac_mode", {"entity_id": e, "hvac_mode": ac.hvac_mode}),
            ("climate", "set_temperature", {"entity_id": e, "temperature": ac.temperature}),
            ("climate", "set_preset_mode", {"entity_id": e, "preset_mode": ac.preset}),
            ("climate", "set_fan_mode", {"entity_id": e, "fan_mode": ac.fan_mode}),
            ("select", "select_option", {"entity_id": cfg.horizontal_select, "option": ac.horizontal}),
            ("select", "select_option", {"entity_id": cfg.vertical_select, "option": ac.vertical}),
        ]
        done, error = 0, None
        for domain, service, data in steps:
            try:
                client.call(domain, service, data)
            except HAError as ex:
                error = ex
                break
            done += 1
            self._ha_ok = True
        if done:
            self._record_switch(now)  # a half-done switch-on counts as well
        st, read_error = None, None
        if done:
            polls = int(READBACK_MAX_S / READBACK_POLL_S)
            for i in range(polls + 1):  # HA shows the result of a service call after a moment
                try:
                    st = client.get_state(e)
                except HAError as ex:
                    read_error = ex
                    break
                if self._reflects(st, cfg):
                    break
                if i < polls:
                    self._sleep(READBACK_POLL_S)
        if error is not None:
            self._fail(now, "Einschalten", error)
        elif read_error is not None:
            self._fail(now, "Status nach dem Einschalten", read_error)
        if st is not None:
            self._last_seen = st.get("state")
        if st is not None and st.get("state") not in ("off", *UNAVAILABLE):
            confirmed = self._reflects(st, cfg)
            self._take_ownership(now, st, confirmed)
            part = "" if error is None else f" (unvollständig: {done} von {len(steps)} Schritten)"
            part += "" if confirmed else ", unbestätigt: Home Assistant zeigt die eingestellten Werte noch nicht"
            self._event(now, f"Klimaanlage eingeschaltet{part} (Wasser {_fmt_c(water)}, "
                             f"Lüfter ≥ {cfg.on.fan_percent:g} %)")
            self._state = "owned"
            if error is None:
                self._reason = ("Klimaanlage eingeschaltet, aquacontrol hat den Besitz" if confirmed else
                                "Klimaanlage eingeschaltet, unbestätigt: eingestellte Werte noch nicht in Home "
                                "Assistant sichtbar")
        elif done:
            # not confirmed: start the on-time over instead of hammering the cloud, but keep looking: once the AC
            # shows the configured values on a later cycle, it is ours
            self._arming_since = now
            self._confirm_until = now + CONFIRM_WINDOW_S
            self._event(now, "Einschalten nicht bestätigt: Status nicht lesbar" if st is None else
                             "Einschalten nicht bestätigt: Klimaanlage meldet weiterhin keinen Betrieb")
            self._state = "arming"

    def _recheck_confirmation(self, now: float, cfg: ClimateConfig, client) -> None:
        """After a switch-on that HA did not show yet: take ownership as soon as the AC shows the configured values."""
        if self._confirm_until is None:
            return
        if now >= self._confirm_until or self._retry_at is not None and now < self._retry_at:
            if now >= self._confirm_until:
                self._confirm_until = None
            return
        st = self._read(now, client, cfg.entity_id)
        if st is None:
            return
        self._last_seen = st.get("state")
        if self._reflects(st, cfg):
            self._take_ownership(now, st, confirmed=True)
            self._event(now, "Klimaanlage eingeschaltet (von Home Assistant verzögert bestätigt)")
            self._state, self._reason = "owned", "Klimaanlage läuft (von aquacontrol eingeschaltet)"
        elif st.get("state") not in ("off", *UNAVAILABLE):  # running with other settings: somebody else's doing
            self._confirm_until = None
