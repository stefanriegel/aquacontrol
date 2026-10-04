"""Safety rules for settings changes. Only fields that changed are checked, so
odd values already stored in the device never block an unrelated edit."""
from __future__ import annotations

import math
from typing import Callable

from .protocol import (LED_EDITABLE_FLAGS, LED_FLAG_BRIGHTNESS, LED_MAX_THRESHOLDS, LED_MODE_COLOR_SWITCH,
                       LED_MODE_STATIC, MODE_CURVE, MODE_FIXED, MODE_TARGET, NUM_TEMPS, STRIP_FLAG_DISABLED,
                       LedController, Settings)

EDITABLE_MODES = (MODE_FIXED, MODE_TARGET, MODE_CURVE)
CURVE_SENSORS = (0, 1, 2, 3)
TARGET_RANGE_C = (20.0, 60.0)
CURVE_TEMP_RANGE_C = (0.0, 100.0)
LED_SOURCES = tuple(range(NUM_TEMPS))  # temperature sensors 1-4; same unit as the stored binding range
MAX_HUE = 1535


class ValidationError(ValueError):
    pass


def _finite(value: float, what: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValidationError(f"{what}: ungültige Zahl {value!r}")


def _percent(value: float, what: str) -> None:
    _finite(value, what)
    if not 0 <= value <= 100:
        raise ValidationError(f"{what}: {value} % liegt nicht zwischen 0 und 100")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_colour(name: str, colour: tuple) -> None:
    if len(colour) != 3:
        raise ValidationError(f"{name}: Farbe braucht Farbton, Sättigung und Helligkeit")
    h, s, v = colour
    if not _is_int(h) or not 0 <= h <= MAX_HUE:
        raise ValidationError(f"{name}: Farbton {h!r} nicht in 0–{MAX_HUE}")
    if not _is_int(s) or not 0 <= s <= 255:
        raise ValidationError(f"{name}: Sättigung {s!r} nicht in 0–255")
    if not _is_int(v) or not 0 <= v <= 255:
        raise ValidationError(f"{name}: Helligkeit {v!r} nicht in 0–255")


def _check_led(number: int, old: LedController, new: LedController) -> None:
    """Only a Farbschalter (thresholds, colours, flags, source) and a static colour (colour, flags) may change."""
    if (old.flags, old.source, old.values, old.palette) == (new.flags, new.source, new.values, new.palette) and \
            (old.led_start, old.led_count, old.mode, old.binding1) == (new.led_start, new.led_count, new.mode, new.binding1):
        return
    name = f"LED-Controller {number}"
    if old.mode not in (LED_MODE_COLOR_SWITCH, LED_MODE_STATIC):
        raise ValidationError(f"{name}: nicht änderbar (nur Farbschalter und statische Farbe)")
    if (new.led_start, new.led_count, new.mode, new.binding1) != (old.led_start, old.led_count, old.mode, old.binding1):
        raise ValidationError(f"{name}: LED-Bereich, Modus und Wertebereich sind nicht änderbar")
    if (new.flags ^ old.flags) & ~LED_EDITABLE_FLAGS:
        raise ValidationError(f"{name}: nur die Schalter Überblenden, Blinken und Helligkeit nach Datenquelle sind änderbar")
    if new.source != old.source:
        if old.mode == LED_MODE_STATIC:
            raise ValidationError(f"{name}: eine statische Farbe hat keine Datenquelle")
        if old.source not in LED_SOURCES:
            raise ValidationError(f"{name}: Datenquelle nur in der Aquasuite änderbar (aktuelle Quelle ist kein Temperatursensor)")
        if not _is_int(new.source) or new.source not in LED_SOURCES:
            raise ValidationError(f"{name}: Datenquelle {new.source!r} ist nicht wählbar (nur Temperatursensor 1–4)")
    if len(new.values) != len(old.values) or len(new.palette) != len(old.palette):
        raise ValidationError(f"{name}: Effektwerte und Palette haben eine feste Länge")
    if old.mode == LED_MODE_STATIC:
        if new.flags & LED_FLAG_BRIGHTNESS and not old.flags & LED_FLAG_BRIGHTNESS:
            raise ValidationError(f"{name}: Helligkeit nach Datenquelle gibt es bei einer statischen Farbe nicht")
        if new.values != old.values:
            raise ValidationError(f"{name}: eine statische Farbe hat keine Schwellen")
        if new.palette[1:] != old.palette[1:]:
            raise ValidationError(f"{name}: Palette: nur die Farbe ist änderbar")
        if new.palette[0] != old.palette[0]:
            _check_colour(name, new.palette[0])
        return
    n = new.values[0]
    if not _is_int(n) or not 1 <= n <= LED_MAX_THRESHOLDS:
        raise ValidationError(f"{name}: Anzahl der Schwellen {n!r} nicht in 1 bis {LED_MAX_THRESHOLDS}")
    old_n = old.values[0]
    free = range(2 + n, len(new.values))
    if new.values[1] != old.values[1] or any(new.values[k] != old.values[k] for k in free):
        raise ValidationError(f"{name}: andere Effektwerte sind nicht änderbar")
    if new.values[:2 + n] != old.values[:2 + n]:
        thresholds = new.values[2:2 + n]
        if not all(_is_int(t) for t in thresholds):
            raise ValidationError(f"{name}: Schwellen müssen ganze Zahlen sein")
        if any(b <= a for a, b in zip(thresholds, thresholds[1:])):
            raise ValidationError(f"{name}: Schwellen müssen streng steigen")
        x1, x2 = old.binding1[:2]
        if any(not x1 <= t <= x2 for t in thresholds):
            raise ValidationError(f"{name}: Schwellen müssen zwischen {x1} und {x2} liegen")
    used = n + 1
    if new.palette[used:] != old.palette[used:]:
        raise ValidationError(f"{name}: Palette: nur die verwendeten Farben sind änderbar")
    grown = range(max(old_n, 0) + 1, used)  # colours added by growing are copies of the last one
    for k in range(used):
        if new.palette[k] != old.palette[k] or k in grown:
            _check_colour(name, new.palette[k])


def check(old: Settings, new: Settings, min_percent: dict[int, float]) -> None:
    """Raise ValidationError if `new` is not an acceptable successor of `old`.

    `min_percent` maps a 0-based channel index to the lowest allowed device minimum
    and fixed power (used for the pump).
    """
    if new.temp_offsets != old.temp_offsets:
        raise ValidationError("Sensor-Offsets sind nicht änderbar")
    if len(new.leds) != len(old.leds):
        raise ValidationError("Anzahl der LED-Controller ist nicht änderbar")
    for i, (ol, nl) in enumerate(zip(old.leds, new.leds)):
        _check_led(i + 1, ol, nl)
    if new.profile != old.profile:
        raise ValidationError("Profilnummer ist nicht änderbar")

    for i, (oc, nc) in enumerate(zip(old.controllers, new.controllers)):
        name = f"Kanal {i + 1}"
        floor = min_percent.get(i)
        if nc.mode != oc.mode and nc.mode not in EDITABLE_MODES:
            raise ValidationError(f"{name}: Modus {nc.mode} ist nicht wählbar")
        if nc.sensor != oc.sensor and nc.sensor not in CURVE_SENSORS:
            raise ValidationError(f"{name}: Sensor {nc.sensor} ist nicht wählbar (nur 1–4)")
        if nc.fixed_percent != oc.fixed_percent or (nc.mode == MODE_FIXED and oc.mode != MODE_FIXED):
            _percent(nc.fixed_percent, f"{name} Fest-Wert")
            if floor is not None and nc.fixed_percent < floor:
                raise ValidationError(f"{name}: Fest-Wert {nc.fixed_percent} % unter Minimum {floor} %")
        if nc.target_c != oc.target_c:
            _finite(nc.target_c, f"{name} Zieltemperatur")
            lo, hi = TARGET_RANGE_C
            if not lo <= nc.target_c <= hi:
                raise ValidationError(f"{name}: Zieltemperatur {nc.target_c} °C nicht in {lo}–{hi} °C")
        if nc.pid != oc.pid or nc.curve_start_c != oc.curve_start_c:
            raise ValidationError(f"{name}: PID-Parameter und Kurvenstart sind nicht änderbar")
        if nc.curve != oc.curve:
            if len(nc.curve) != 16:
                raise ValidationError(f"{name}: Kurve braucht genau 16 Punkte")
            temps = [t for t, _ in nc.curve]
            for t, pct in nc.curve:
                _finite(t, f"{name} Kurventemperatur")
                lo, hi = CURVE_TEMP_RANGE_C
                if not lo <= t <= hi:
                    raise ValidationError(f"{name}: Kurventemperatur {t} °C nicht in {lo}–{hi} °C")
                _percent(pct, f"{name} Kurvenwert")
            if any(b <= a for a, b in zip(temps, temps[1:])):
                raise ValidationError(f"{name}: Kurventemperaturen müssen streng steigen")

    for i, (of, nf) in enumerate(zip(old.fans, new.fans)):
        name = f"Kanal {i + 1}"
        floor = min_percent.get(i)
        if nf.flags != of.flags or nf.fallback_percent != of.fallback_percent or nf.max_rpm != of.max_rpm:
            raise ValidationError(f"{name}: Flags, Fallback und Max-RPM sind nicht änderbar")
        if (nf.min_percent, nf.max_percent) != (of.min_percent, of.max_percent):
            _percent(nf.min_percent, f"{name} Minimum")
            _percent(nf.max_percent, f"{name} Maximum")
            if nf.min_percent >= nf.max_percent:
                raise ValidationError(f"{name}: Minimum muss kleiner als Maximum sein")
            if floor is not None and nf.min_percent < floor:
                raise ValidationError(f"{name}: Minimum {nf.min_percent} % unter erlaubtem {floor} %")

    if (new.strip_flags ^ old.strip_flags) & ~STRIP_FLAG_DISABLED:
        raise ValidationError("Nur das An/Aus-Bit des Strips ist änderbar")
    if new.strip_brightness != old.strip_brightness:
        b = new.strip_brightness
        if not isinstance(b, int) or isinstance(b, bool) or not 0 <= b <= 255:
            raise ValidationError(f"Helligkeit {b!r} nicht in 0–255")


def make_check(min_percent: dict[int, float]) -> Callable[[Settings, Settings], None]:
    return lambda old, new: check(old, new, min_percent)
