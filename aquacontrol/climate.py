"""Climate automation: switch the room air conditioner (via Home Assistant) on when the water cooling
runs at its limit for a while, and off again when the water is clearly cooler. See
docs/superpowers/specs/2026-10-04-aquacontrol-climate-design.md. Manual operation always wins: the
controller only ever switches off an AC it switched on itself and still "owns"."""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass

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


def _entity(v: object, domain: str, what: str) -> str:
    if not isinstance(v, str) or not re.fullmatch(rf"{domain}\.[a-z0-9_]+", v):
        raise ClimateConfigError(f"{what} muss die Form {domain}.name haben (Kleinbuchstaben, Ziffern, _)")
    return v


def _url(v: object) -> str:
    if not isinstance(v, str):
        raise ClimateConfigError("ha_url muss ein Text sein")
    if v == "":
        return ""
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", v):
        raise ClimateConfigError("ha_url muss mit http:// oder https:// beginnen, z. B. http://homeassistant.local:8123")
    return v.rstrip("/")


def parse_climate_config(raw: object) -> ClimateConfig:
    """Validate a climate config dict (missing keys take the defaults) and return it as a ClimateConfig."""
    if not isinstance(raw, dict):
        raise ClimateConfigError("Klima-Konfiguration muss ein Objekt sein")
    top = {"enabled", "ha_url", "entity_id", "horizontal_select", "vertical_select", "on", "off",
           "min_on_minutes", "min_off_minutes", "max_switches_per_hour", "ac"}
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
                  _choice(get(ac_raw, "preset", d["ac"]), PRESETS, "Preset"),
                  _choice(get(ac_raw, "fan_mode", d["ac"]), FAN_MODES, "Lüfterstufe"),
                  _choice(get(ac_raw, "horizontal", d["ac"]), HORIZONTAL, "horizontal"),
                  _choice(get(ac_raw, "vertical", d["ac"]), VERTICAL, "vertical"))

    max_switches = get(raw, "max_switches_per_hour", d)
    if isinstance(max_switches, bool) or not isinstance(max_switches, int) or not 1 <= max_switches <= MAX_SWITCHES_LIMIT:
        raise ClimateConfigError(f"max_switches_per_hour muss eine ganze Zahl von 1 bis {MAX_SWITCHES_LIMIT} sein")
    return ClimateConfig(
        enabled=enabled,
        ha_url=_url(get(raw, "ha_url", d)),
        entity_id=_entity(get(raw, "entity_id", d), "climate", "entity_id"),
        horizontal_select=_entity(get(raw, "horizontal_select", d), "select", "horizontal_select"),
        vertical_select=_entity(get(raw, "vertical_select", d), "select", "vertical_select"),
        on=on, off=off,
        min_on_minutes=_minutes(get(raw, "min_on_minutes", d), "Mindestlaufzeit"),
        min_off_minutes=_minutes(get(raw, "min_off_minutes", d), "Sperrzeit"),
        max_switches_per_hour=max_switches, ac=ac)
