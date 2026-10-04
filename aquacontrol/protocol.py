"""Byte layout of the Aquacomputer QUADRO HID reports (firmware 1033).

Pure functions only, no I/O. All multi-byte fields are big-endian; temperatures
and percentages are stored x100.

Offsets of the settings report (0x03) are payload-relative: payload = report[1:959],
so absolute offset = payload offset + 1. Offsets of the status report (0x01) are
absolute.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, replace

STATUS_REPORT_ID = 0x01
STATUS_REPORT_LEN = 220
SETTINGS_REPORT_ID = 0x03
SETTINGS_REPORT_LEN = 961
NAMES_REPORT_ID = 0x08
NAMES_REPORT_LEN = 1013
FEATURE_READ_LEN = 1013
COMMIT_REPORT = bytes.fromhex("02 00 00 00 02 00 00 00 00 34 c6")
MISSING_TEMP_RAW = 32767

MODE_FIXED = 0
MODE_TARGET = 1
MODE_CURVE = 2
MODE_FOLLOW = 4
MODE_NAMES = {MODE_FIXED: "fixed", MODE_TARGET: "target", MODE_CURVE: "curve", MODE_FOLLOW: "follow"}

STRIP_FLAG_DISABLED = 0x0002

LED_MODE_UNUSED = 0x00
LED_MODE_STATIC = 0x01
LED_MODE_COLOR_SWITCH = 0x12  # "Farbschalter": colour chosen by thresholds on a data source
LED_FLAG_FADE = 0x0001
LED_FLAG_BLINK = 0x0002
LED_FLAG_BRIGHTNESS = 0x4000  # "Helligkeit nach Datenquelle"
LED_EDITABLE_FLAGS = LED_FLAG_FADE | LED_FLAG_BLINK | LED_FLAG_BRIGHTNESS
LED_MAX_THRESHOLDS = 5
LED_UNUSED_THRESHOLD = 100  # what the device holds in threshold slots that are not in use
LED_VALUES = 12
LED_PALETTE = 6
LED_SOURCE_NONE = -1

NUM_FANS = 4
NUM_TEMPS = 4
NUM_SOFT_SENSORS = 16
NUM_CURVE_POINTS = 16
NUM_LEDS = 8

# settings payload offsets
_TEMP_OFFSETS = 9
_FAN_CONFIG = 17
_FAN_CONFIG_SIZE = 9
_CONTROLLER = 53
_CONTROLLER_SIZE = 85
_STRIP_BRIGHTNESS = 393
_STRIP_FLAGS = 394
_LED = 396
_LED_SIZE = 70
_PROFILE = 956
_CRC_ABS = 959  # absolute offset of the CRC in the settings report

# status report absolute offsets
_ST_FIRMWARE = 13
_ST_TEMPS = 52
_ST_SOFT = 60
_ST_VCC12 = 108
_ST_FLOW = 110
_ST_FAN = 112
_ST_FAN_SIZE = 13
_ST_PROFILE = 219

# names report
_NAMES_START = 3
_NAME_SLOT = 24
NAME_SLOTS_FANS = range(0, 4)
NAME_SLOTS_LEDS = range(8, 16)
NAME_SLOT_FLOW = 16
NAME_SLOTS_TEMPS = range(17, 21)
NAME_SLOT_STRIP = 23


class ProtocolError(ValueError):
    """A report has the wrong id, length or checksum."""


def crc16_usb(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from(">H", b, o)[0]


def _s16(b: bytes, o: int) -> int:
    return struct.unpack_from(">h", b, o)[0]


def _x100(raw: int) -> float:
    return raw / 100


def _raw(value: float) -> int:
    return int(round(value * 100))


@dataclass(frozen=True)
class FanConfig:
    flags: int
    min_percent: float
    max_percent: float
    fallback_percent: float
    max_rpm: int


@dataclass(frozen=True)
class Controller:
    mode: int
    fixed_percent: float
    sensor: int
    target_c: float
    pid: tuple[int, ...]  # raw PID block after the setpoint: 6 x s16, kept verbatim
    curve_start_c: float
    curve: tuple[tuple[float, float], ...]  # 16 x (temp_c, percent)


@dataclass(frozen=True)
class LedController:
    """One 70-byte LED entry. Only the fields the editor may change are written back (see encode_settings)."""
    led_start: int
    led_count: int
    mode: int
    flags: int
    source: int                                    # s16, -1 = none
    binding1: tuple[int, int, int, int]            # x1, x2 (source range), y1, y2
    values: tuple[int, ...]                        # 12 x s16; Farbschalter: [0] = n thresholds, [2..1+n] = thresholds
    palette: tuple[tuple[int, int, int], ...]      # 6 x (hue 0..1535, saturation, value)
    raw: bytes                                     # all 70 bytes as read; never written back

    @property
    def threshold_count(self) -> int:
        return self.values[0]

    @property
    def thresholds(self) -> tuple[int, ...]:
        if self.mode != LED_MODE_COLOR_SWITCH:
            return ()
        return self.values[2:2 + max(0, min(self.values[0], LED_VALUES - 2))]

    @property
    def colors(self) -> tuple[tuple[int, int, int], ...]:
        """The colours in use: one more than thresholds for a Farbschalter, one for a static colour."""
        if self.mode == LED_MODE_COLOR_SWITCH:
            return self.palette[:max(0, min(self.values[0] + 1, LED_PALETTE))]
        if self.mode == LED_MODE_STATIC:
            return self.palette[:1]
        return ()


@dataclass(frozen=True)
class Settings:
    temp_offsets: tuple[float, ...]
    fans: tuple[FanConfig, ...]
    controllers: tuple[Controller, ...]
    strip_brightness: int
    strip_flags: int
    leds: tuple[LedController, ...]
    profile: int

    @property
    def strip_enabled(self) -> bool:
        return not self.strip_flags & STRIP_FLAG_DISABLED


@dataclass(frozen=True)
class FanStatus:
    percent: float
    voltage: float
    current_ma: int
    power_w: float
    rpm: int


@dataclass(frozen=True)
class Status:
    firmware: int
    temps: tuple[float | None, ...]
    soft_sensors: tuple[float | None, ...]
    vcc12: float
    flow_lph: float
    fans: tuple[FanStatus, ...]
    profile: int


@dataclass(frozen=True)
class Names:
    fans: tuple[str, ...]
    leds: tuple[str, ...]
    temps: tuple[str, ...]
    flow: str
    strip: str


def settings_crc_ok(report: bytes) -> bool:
    return len(report) == SETTINGS_REPORT_LEN and crc16_usb(report[1:_CRC_ABS]) == _u16(report, _CRC_ABS)


def check_settings_report(report: bytes) -> None:
    if len(report) != SETTINGS_REPORT_LEN:
        raise ProtocolError(f"settings report has {len(report)} bytes, expected {SETTINGS_REPORT_LEN}")
    if report[0] != SETTINGS_REPORT_ID:
        raise ProtocolError(f"settings report id is {report[0]:#04x}, expected 0x03")
    if not settings_crc_ok(report):
        raise ProtocolError("settings report CRC mismatch")


def _decode_led(p: bytes, b: int) -> LedController:
    """Decode the 70-byte LED entry that starts at index `b` of `p` (offsets in the entry are relative to `b`)."""
    return LedController(
        led_start=p[b + 1], led_count=p[b + 2], mode=p[b + 3], flags=_u16(p, b + 4), source=_s16(p, b + 6),
        binding1=(_s16(p, b + 10), _s16(p, b + 12), p[b + 14], p[b + 15]),
        values=tuple(_s16(p, b + 22 + 2 * k) for k in range(LED_VALUES)),
        palette=tuple((_u16(p, b + 46 + 4 * k), p[b + 48 + 4 * k], p[b + 49 + 4 * k]) for k in range(LED_PALETTE)),
        raw=bytes(p[b:b + _LED_SIZE]))


def decode_settings(report: bytes) -> Settings:
    check_settings_report(report)
    p = report[1:]
    fans = []
    for i in range(NUM_FANS):
        b = _FAN_CONFIG + i * _FAN_CONFIG_SIZE
        fans.append(FanConfig(p[b], _x100(_s16(p, b + 1)), _x100(_s16(p, b + 3)),
                              _x100(_s16(p, b + 5)), _s16(p, b + 7)))
    controllers = []
    for i in range(NUM_FANS):
        b = _CONTROLLER + i * _CONTROLLER_SIZE
        pid_base = b + 5
        curve_base = b + 19
        curve = tuple(
            (_x100(_s16(p, curve_base + 2 + 2 * k)), _x100(_s16(p, curve_base + 34 + 2 * k)))
            for k in range(NUM_CURVE_POINTS)
        )
        controllers.append(Controller(
            mode=p[b],
            fixed_percent=_x100(_s16(p, b + 1)),
            sensor=_u16(p, b + 3),
            target_c=_x100(_s16(p, pid_base)),
            pid=tuple(_s16(p, pid_base + 2 + 2 * k) for k in range(6)),
            curve_start_c=_x100(_s16(p, curve_base)),
            curve=curve,
        ))
    leds = []
    for i in range(NUM_LEDS):
        b = _LED + i * _LED_SIZE
        leds.append(_decode_led(p, b))
    return Settings(
        temp_offsets=tuple(_x100(_s16(p, _TEMP_OFFSETS + 2 * i)) for i in range(NUM_TEMPS)),
        fans=tuple(fans),
        controllers=tuple(controllers),
        strip_brightness=p[_STRIP_BRIGHTNESS],
        strip_flags=_u16(p, _STRIP_FLAGS),
        leds=tuple(leds),
        profile=p[_PROFILE],
    )


def encode_settings(settings: Settings, base: bytes) -> bytes:
    """Patch the known fields of `settings` into a copy of `base` and fix the CRC.

    Bytes that are not modelled (unknown areas) are copied from `base`. LED entries are patched only where
    the entry differs from `base` and its mode (taken from `base`) is a Farbschalter or a static colour:
    flags 0x0001/0x0002/0x4000, source, values[0], the threshold slots and the colours in use (Farbschalter),
    resp. flags and colour 0 (static). Everything else in the 70 bytes comes from `base`.
    """
    check_settings_report(base)
    r = bytearray(base)
    a = 1  # payload offset -> absolute offset

    def put_s16(off: int, value: int) -> None:
        struct.pack_into(">h", r, a + off, value)

    for i, v in enumerate(settings.temp_offsets):
        put_s16(_TEMP_OFFSETS + 2 * i, _raw(v))
    for i, f in enumerate(settings.fans):
        b = _FAN_CONFIG + i * _FAN_CONFIG_SIZE
        r[a + b] = f.flags
        put_s16(b + 1, _raw(f.min_percent))
        put_s16(b + 3, _raw(f.max_percent))
        put_s16(b + 5, _raw(f.fallback_percent))
        put_s16(b + 7, f.max_rpm)
    for i, c in enumerate(settings.controllers):
        b = _CONTROLLER + i * _CONTROLLER_SIZE
        r[a + b] = c.mode
        put_s16(b + 1, _raw(c.fixed_percent))
        struct.pack_into(">H", r, a + b + 3, c.sensor)
        put_s16(b + 5, _raw(c.target_c))
        for k, v in enumerate(c.pid):
            put_s16(b + 7 + 2 * k, v)
        curve_base = b + 19
        put_s16(curve_base, _raw(c.curve_start_c))
        for k, (temp, pct) in enumerate(c.curve):
            put_s16(curve_base + 2 + 2 * k, _raw(temp))
            put_s16(curve_base + 34 + 2 * k, _raw(pct))
    for i, led in enumerate(settings.leds):
        _encode_led(r, a + _LED + i * _LED_SIZE, led)
    r[a + _STRIP_BRIGHTNESS] = settings.strip_brightness
    struct.pack_into(">H", r, a + _STRIP_FLAGS, settings.strip_flags)
    struct.pack_into(">H", r, _CRC_ABS, crc16_usb(bytes(r[1:_CRC_ABS])))
    return bytes(r)


def _encode_led(r: bytearray, b: int, led: LedController) -> None:
    """Patch `led` into the entry that starts at index `b` of `r`, which still holds the base report there."""
    old = _decode_led(r, b)
    if old.mode not in (LED_MODE_COLOR_SWITCH, LED_MODE_STATIC) or _same_led(old, led):
        return
    if old.flags != led.flags:
        struct.pack_into(">H", r, b + 4, (old.flags & ~LED_EDITABLE_FLAGS) | (led.flags & LED_EDITABLE_FLAGS))
    if old.mode == LED_MODE_STATIC:
        if old.palette[0] != led.palette[0]:
            _put_colour(r, b, 0, led.palette[0])
        return
    n = led.values[0]
    if not 0 <= n <= LED_MAX_THRESHOLDS:
        raise ProtocolError(f"LED controller: {n} thresholds, at most {LED_MAX_THRESHOLDS} supported")
    if old.source != led.source:
        struct.pack_into(">h", r, b + 6, led.source)
    old_n = min(max(old.values[0], 0), LED_MAX_THRESHOLDS)
    if old.values[0] != n:
        struct.pack_into(">h", r, b + 22, n)
    for k in range(2, 2 + n):  # threshold slots in use
        if old.values[k] != led.values[k]:
            struct.pack_into(">h", r, b + 22 + 2 * k, led.values[k])
    for k in range(2 + n, 2 + old_n):  # slots freed by shrinking get 100, like the unused slots on the device
        struct.pack_into(">h", r, b + 22 + 2 * k, LED_UNUSED_THRESHOLD)
    for k in range(n + 1):  # colours in use
        if old.palette[k] != led.palette[k]:
            _put_colour(r, b, k, led.palette[k])


def _same_led(a: LedController, b: LedController) -> bool:
    return (a.flags, a.source, a.values, a.palette) == (b.flags, b.source, b.values, b.palette)


def _put_colour(r: bytearray, b: int, k: int, colour: tuple[int, int, int]) -> None:
    h, sat, val = colour
    struct.pack_into(">HBB", r, b + 46 + 4 * k, h, sat, val)


def with_led(settings: Settings, index: int, *, thresholds: tuple[int, ...] | None = None,
             colors: tuple[tuple[int, int, int], ...] | None = None, **changes) -> Settings:
    """Copy of `settings` with LED controller `index` (0-based) changed.

    `changes` are LedController fields (flags, source, values, palette ...). `thresholds` sets values[0] and the
    threshold slots; when that grows the colour list, the new colours are copies of the last existing colour.
    `colors` then replaces the leading palette entries. Slots freed by shrinking are set to 100 only by
    encode_settings, so the model keeps what the device reported.
    """
    led = replace(settings.leds[index], **changes)
    if thresholds is not None:
        n = len(thresholds)
        if n > LED_VALUES - 2:
            raise ProtocolError(f"too many thresholds ({n})")
        values = list(led.values)
        values[0] = n
        values[2:2 + n] = thresholds
        palette = list(led.palette)
        have = max(0, min(led.values[0], LED_PALETTE - 1)) + 1  # colours in use before the change
        for k in range(have, min(n + 1, LED_PALETTE)):
            palette[k] = palette[have - 1]
        led = replace(led, values=tuple(values), palette=tuple(palette))
    if colors is not None:
        if len(colors) > LED_PALETTE:
            raise ProtocolError(f"too many colours ({len(colors)})")
        led = replace(led, palette=tuple(colors) + led.palette[len(colors):])
    leds = list(settings.leds)
    leds[index] = led
    return replace(settings, leds=tuple(leds))


def with_strip(settings: Settings, *, enabled: bool | None = None, brightness: int | None = None) -> Settings:
    flags = settings.strip_flags
    if enabled is not None:
        flags = flags & ~STRIP_FLAG_DISABLED if enabled else flags | STRIP_FLAG_DISABLED
    return replace(settings, strip_flags=flags,
                   strip_brightness=settings.strip_brightness if brightness is None else brightness)


def with_controller(settings: Settings, index: int, **changes) -> Settings:
    controllers = list(settings.controllers)
    controllers[index] = replace(controllers[index], **changes)
    return replace(settings, controllers=tuple(controllers))


def with_fan(settings: Settings, index: int, **changes) -> Settings:
    fans = list(settings.fans)
    fans[index] = replace(fans[index], **changes)
    return replace(settings, fans=tuple(fans))


def _temp(raw: int) -> float | None:
    return None if raw == MISSING_TEMP_RAW else _x100(raw)


def decode_status(report: bytes) -> Status:
    if len(report) != STATUS_REPORT_LEN or report[0] != STATUS_REPORT_ID:
        raise ProtocolError(f"not a status report (id {report[:1].hex()}, {len(report)} bytes)")
    fans = []
    for i in range(NUM_FANS):
        b = _ST_FAN + i * _ST_FAN_SIZE
        fans.append(FanStatus(_x100(_s16(report, b)), _x100(_s16(report, b + 2)), _s16(report, b + 4),
                              _x100(_s16(report, b + 6)), _s16(report, b + 8)))
    return Status(
        firmware=_u16(report, _ST_FIRMWARE),
        temps=tuple(_temp(_s16(report, _ST_TEMPS + 2 * i)) for i in range(NUM_TEMPS)),
        soft_sensors=tuple(_temp(_s16(report, _ST_SOFT + 2 * i)) for i in range(NUM_SOFT_SENSORS)),
        vcc12=_x100(_s16(report, _ST_VCC12)),
        flow_lph=_s16(report, _ST_FLOW) / 10,  # raw unit is dL/h
        fans=tuple(fans),
        profile=report[_ST_PROFILE],
    )


def scrub_status_serial(report: bytes) -> bytes:
    """Zero the raw device serial (bytes 3..6) so a status report can be shared."""
    r = bytearray(report)
    r[3:7] = b"\x00\x00\x00\x00"
    return bytes(r)


def decode_names(report: bytes) -> Names:
    if len(report) < NAMES_REPORT_LEN or report[0] != NAMES_REPORT_ID:
        raise ProtocolError("not a names report")

    def slot(i: int) -> str:
        start = _NAMES_START + i * _NAME_SLOT
        return report[start:start + _NAME_SLOT].split(b"\x00", 1)[0].decode("utf-8", "replace")

    return Names(
        fans=tuple(slot(i) for i in NAME_SLOTS_FANS),
        leds=tuple(slot(i) for i in NAME_SLOTS_LEDS),
        temps=tuple(slot(i) for i in NAME_SLOTS_TEMPS),
        flow=slot(NAME_SLOT_FLOW),
        strip=slot(NAME_SLOT_STRIP),
    )
