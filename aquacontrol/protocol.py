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
    led_start: int
    led_count: int
    mode: int
    flags: int
    raw: bytes  # all 70 bytes; v1 never modifies them


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
        leds.append(LedController(p[b + 1], p[b + 2], p[b + 3], _u16(p, b + 4), bytes(p[b:b + _LED_SIZE])))
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

    Bytes that are not modelled (unknown areas, LED controllers) are copied from `base`.
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
    r[a + _STRIP_BRIGHTNESS] = settings.strip_brightness
    struct.pack_into(">H", r, a + _STRIP_FLAGS, settings.strip_flags)
    struct.pack_into(">H", r, _CRC_ABS, crc16_usb(bytes(r[1:_CRC_ABS])))
    return bytes(r)


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
