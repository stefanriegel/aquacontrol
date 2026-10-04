"""Safety rules for settings changes. Only fields that changed are checked, so
odd values already stored in the device never block an unrelated edit."""
from __future__ import annotations

import math
from typing import Callable

from .protocol import MODE_CURVE, MODE_FIXED, MODE_TARGET, STRIP_FLAG_DISABLED, Settings

EDITABLE_MODES = (MODE_FIXED, MODE_TARGET, MODE_CURVE)
CURVE_SENSORS = (0, 1, 2, 3)
TARGET_RANGE_C = (20.0, 60.0)
CURVE_TEMP_RANGE_C = (0.0, 100.0)


class ValidationError(ValueError):
    pass


def _finite(value: float, what: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValidationError(f"{what}: ungültige Zahl {value!r}")


def _percent(value: float, what: str) -> None:
    _finite(value, what)
    if not 0 <= value <= 100:
        raise ValidationError(f"{what}: {value} % liegt nicht zwischen 0 und 100")


def check(old: Settings, new: Settings, min_percent: dict[int, float]) -> None:
    """Raise ValidationError if `new` is not an acceptable successor of `old`.

    `min_percent` maps a 0-based channel index to the lowest allowed device minimum
    and fixed power (used for the pump).
    """
    if new.temp_offsets != old.temp_offsets:
        raise ValidationError("Sensor-Offsets sind nicht änderbar")
    if new.leds != old.leds:
        raise ValidationError("LED-Controller sind in dieser Version nicht änderbar")
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
