# aquacontrol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Windows aquasuite VM with a Python daemon on the Proxmox host. It shows the QUADRO's live values plus host and GPU temperatures, edits the fan and LED settings stored in the device, and switches the LEDs on a schedule.

**Architecture:** Pure-stdlib Python package `aquacontrol`.
- A pure protocol layer decodes and encodes the HID reports.
- A hidraw transport talks to the device.
- `Device` serialises every write and does backup, write, commit, verify and rollback.
- A monitor thread reads the live input reports.
- A scheduler thread switches the LEDs.
- A ThreadingHTTPServer serves the JSON API and a vanilla-JS UI over TLS with Basic auth.

The device runs its curves and LED effects itself. The daemon only edits stored settings: on a user action, or about twice a day for the schedule.

**Tech Stack:** Python 3.13 standard library (host: Debian 13 / PVE 9.1, kernel 6.17), `unittest`, vanilla JS/SVG/CSS, systemd, udev.

**Spec:** `docs/superpowers/specs/2026-10-04-aquacontrol-design.md`

**How this plan was made:** Every code block below was written and run before the plan was saved, against
the real device reports in `tests/fixtures/`. The full suite was 107 tests and all passed with Python 3.14
on macOS. The real usbmon pcap parsed on the host with Python 3.13. Copy the blocks verbatim. If a test
fails, the transcription is wrong, not the design.

## Global Constraints

- Python standard library only: no pip packages on the hypervisor. Target is Python 3.13; the code must also run on 3.11+.
- **Never commit the device serial number** (raw bytes at status-report offset 3..6, or its text form). Status fixtures are scrubbed with `scrub_status_serial`. The repo is public.
- Strings that must never appear in the repo (the serial, internal IPs) are listed one per line in `~/.aquacontrol-never-commit`. That file lives outside the repo, and the operator creates it once. Check with `git grep -nF -f ~/.aquacontrol-never-commit`, which must print nothing.
- **Never commit internal IPs, tokens, password hashes or aquasuite account data.** Commands below use `$PVE` (SSH target of the Proxmox host) and `$LLMVM` (SSH target of VM 103). The operator knows their values; they are not stored in the repo.
- Byte order is big-endian. Temperatures and percentages are ×100.
- Settings report `0x03` is 961 bytes, with a CRC-16/USB over bytes 1..958 stored big-endian at 959..960.
- Commit report: `02 00 00 00 02 00 00 00 00 34 c6`.
- Channel 1 is the pump. Its device minimum and its fixed value must never go below `min_percent` (25 % by default).
- A device write happens only on explicit user action, a backup restore, or a schedule change. Never periodically, and never when nothing changed.
- UI text and error messages are in German. Code, comments and commit messages are in English.
- Test command: `python3 -m unittest discover -s tests -t .`, run from the repo root.
- Hardware tasks (14 onwards) change the real device. Each one starts only after the user has explicitly said yes in the chat.

## Review Focus

1. **QUADRO missing or held by VM 100.** The daemon stays up, the UI shows "offline", and writes return 502 with a clear message instead of a traceback. Pinned by `test_device_absent` (Task 5), `test_run_survives_missing_device` (Task 7) and `test_device_absent_is_502` (Task 10).
2. **UI save and scheduler tick at the same time.** Both are serialised and neither edit is lost. Pinned by `test_concurrent_applies_are_serialised` (Task 5).
3. **Daemon down at the switch time, or started just after midnight.** The LED state is caught up from the most recent rule, including rules from the previous day. Pinned by `test_catch_up_turns_off_and_writes_once` and `test_just_after_midnight_uses_previous_day` (Task 8).
4. **Malformed or hostile request bodies** (wrong types, NaN, 15 curve points, non-JSON, wrong Content-Type, oversized). These return 400 and nothing is written. Pinned by `test_validation_and_malformed_bodies_are_400` (Task 10).
5. **Restoring a corrupted or foreign backup, or a path-traversal name.** It is rejected before anything is written. Pinned by `test_load_rejects_traversal_and_missing` and `test_load_rejects_corrupted_file` (Task 3), and `test_backups_and_restore` (Task 10).

---

### Task 1: Project scaffold and protocol layer

The binary fixtures are already in the repo (`tests/fixtures/*.bin`, committed together with this plan). They are real device reports:
- `settings_live.bin`: report 0x03 read from the device on 2026-10-04.
- `settings_aquasuite_2025.bin`: the stale aquasuite profile.
- `names.bin`: report 0x08.
- `status.bin`: report 0x01 with the serial zeroed. At the moment of capture, hwmon read temp1 31720, fan1..4 3024/362/363/503 rpm, flow 1092 dL/h.

**Files:**
- Create: `aquacontrol/__init__.py` (empty), `tests/__init__.py` (empty), `tools/__init__.py` (empty), `tests/fixtures/__init__.py`, `aquacontrol/protocol.py`, `.gitignore`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces, in `aquacontrol.protocol`:
  - constants `SETTINGS_REPORT_ID=3`, `SETTINGS_REPORT_LEN=961`, `STATUS_REPORT_ID=1`, `STATUS_REPORT_LEN=220`, `NAMES_REPORT_ID=8`, `NAMES_REPORT_LEN=1013`, `FEATURE_READ_LEN=1013`, `COMMIT_REPORT`, `MODE_FIXED=0`, `MODE_TARGET=1`, `MODE_CURVE=2`, `MODE_FOLLOW=4`, `MODE_NAMES`, `STRIP_FLAG_DISABLED=0x0002`, `NUM_FANS=4`, `NUM_CURVE_POINTS=16`
  - dataclasses `FanConfig(flags, min_percent, max_percent, fallback_percent, max_rpm)`,
    `Controller(mode, fixed_percent, sensor, target_c, pid: tuple[int,...6], curve_start_c, curve: tuple[(temp_c, percent)]*16)`,
    `LedController(led_start, led_count, mode, flags, raw: bytes)`,
    `Settings(temp_offsets, fans, controllers, strip_brightness, strip_flags, leds, profile)` with the property `strip_enabled`,
    `FanStatus(percent, voltage, current_ma, power_w, rpm)`,
    `Status(firmware, temps, soft_sensors, vcc12, flow_lph, fans, profile)`,
    `Names(fans, leds, temps, flow, strip)`
  - functions `crc16_usb`, `settings_crc_ok`, `check_settings_report` (raises `ProtocolError`, a subclass of `ValueError`), `decode_settings`, `encode_settings(settings, base) -> bytes`, `with_strip(s, enabled=None, brightness=None)`, `with_controller(s, idx0, **changes)`, `with_fan(s, idx0, **changes)`, `decode_status`, `scrub_status_serial`, `decode_names`
  - `tests.fixtures.load(name) -> bytes`

- [ ] **Step 1: Scaffold**

```bash
mkdir -p aquacontrol tests tools static deploy/push-gpu
touch aquacontrol/__init__.py tests/__init__.py tools/__init__.py
printf '__pycache__/\n*.pyc\ndev/\n*.pcap\n' > .gitignore
```

`tests/fixtures/__init__.py`:

```python
from pathlib import Path

FIXTURES = Path(__file__).parent


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
```

- [ ] **Step 2: Write the failing test**

`tests/test_protocol.py`:

```python
import unittest
from dataclasses import replace

from aquacontrol import protocol as p
from tests.fixtures import load


class CrcTest(unittest.TestCase):
    def test_known_vector(self):
        # CRC-16/USB check value from the CRC catalogue
        self.assertEqual(p.crc16_usb(b"123456789"), 0xB4C8)

    def test_fixture_reports_have_valid_crc(self):
        for name in ("settings_live.bin", "settings_aquasuite_2025.bin"):
            with self.subTest(name=name):
                self.assertTrue(p.settings_crc_ok(load(name)))

    def test_names_report_crc(self):
        r = load("names.bin")
        self.assertEqual(p.crc16_usb(r[1:-2]), int.from_bytes(r[-2:], "big"))


class DecodeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.s = p.decode_settings(load("settings_live.bin"))

    def test_fan_configs(self):
        self.assertEqual(self.s.fans[0], p.FanConfig(3, 28.02, 90.3, 100.0, 4500))
        self.assertEqual(self.s.fans[1], p.FanConfig(3, 5.0, 100.0, 75.78, 2000))
        self.assertEqual(self.s.fans[3].min_percent, 9.69)

    def test_controller_modes_and_targets(self):
        modes = [c.mode for c in self.s.controllers]
        self.assertEqual(modes, [p.MODE_CURVE, p.MODE_TARGET, p.MODE_TARGET, p.MODE_TARGET])
        self.assertEqual([c.target_c for c in self.s.controllers], [36.0, 34.0, 35.0, 37.0])
        self.assertEqual(self.s.controllers[0].fixed_percent, 29.52)
        self.assertEqual(self.s.controllers[0].sensor, 0)
        self.assertEqual(self.s.controllers[0].pid, (1400, 1200, 0, 40, 20, 1))

    def test_pump_curve(self):
        curve = self.s.controllers[0].curve
        self.assertEqual(len(curve), 16)
        self.assertEqual(curve[0], (12.53, 0.0))
        self.assertEqual(curve[6], (30.05, 6.35))
        self.assertEqual(curve[15], (43.0, 90.0))
        self.assertEqual(self.s.controllers[0].curve_start_c, 32.45)

    def test_strip_and_leds(self):
        self.assertEqual(self.s.strip_brightness, 218)
        self.assertTrue(self.s.strip_enabled)
        self.assertEqual((self.s.leds[0].led_start, self.s.leds[0].led_count, self.s.leds[0].mode), (0, 30, 18))
        self.assertEqual((self.s.leds[1].led_start, self.s.leds[1].led_count), (30, 15))
        self.assertEqual((self.s.leds[6].led_start, self.s.leds[6].led_count, self.s.leds[6].flags), (45, 15, 6))
        self.assertEqual(len(self.s.leds[0].raw), 70)
        self.assertEqual(self.s.profile, 1)

    def test_rejects_bad_crc(self):
        r = bytearray(load("settings_live.bin"))
        r[100] ^= 0xFF
        with self.assertRaises(p.ProtocolError):
            p.decode_settings(bytes(r))

    def test_rejects_wrong_length_and_id(self):
        r = load("settings_live.bin")
        with self.assertRaises(p.ProtocolError):
            p.decode_settings(r[:-1])
        with self.assertRaises(p.ProtocolError):
            p.decode_settings(b"\x04" + r[1:])


class EncodeSettingsTest(unittest.TestCase):
    def test_roundtrip_is_byte_identical(self):
        for name in ("settings_live.bin", "settings_aquasuite_2025.bin"):
            with self.subTest(name=name):
                r = load(name)
                self.assertEqual(p.encode_settings(p.decode_settings(r), r), r)

    def test_change_touches_only_that_field_and_crc(self):
        r = load("settings_live.bin")
        s = p.with_controller(p.decode_settings(r), 3, target_c=38.5)
        out = p.encode_settings(s, r)
        diff = [i for i in range(len(r)) if r[i] != out[i]]
        # controller 4 base = payload 53 + 3*85 = 308; setpoint at +5 -> payload 313 -> absolute 314..315
        self.assertTrue(set(diff) <= {314, 315, 959, 960}, diff)
        self.assertTrue(p.settings_crc_ok(out))
        self.assertEqual(p.decode_settings(out).controllers[3].target_c, 38.5)

    def test_curve_and_fan_limits_roundtrip(self):
        r = load("settings_live.bin")
        new_curve = tuple((20.0 + i, 30.0 + i * 4) for i in range(16))
        s = p.decode_settings(r)
        s = p.with_controller(s, 2, mode=p.MODE_CURVE, curve=new_curve)
        s = p.with_fan(s, 2, min_percent=12.5, max_percent=95.0)
        d = p.decode_settings(p.encode_settings(s, r))
        self.assertEqual(d.controllers[2].curve, new_curve)
        self.assertEqual(d.controllers[2].mode, p.MODE_CURVE)
        self.assertEqual((d.fans[2].min_percent, d.fans[2].max_percent), (12.5, 95.0))

    def test_led_bytes_are_never_changed(self):
        r = load("settings_live.bin")
        s = p.decode_settings(r)
        fake_led = replace(s.leds[0], raw=bytes(70), mode=0)
        s = replace(s, leds=(fake_led,) + s.leds[1:])
        out = p.encode_settings(s, r)
        self.assertEqual(out[397:397 + 560], r[397:397 + 560])

    def test_with_strip(self):
        s = p.decode_settings(load("settings_live.bin"))
        off = p.with_strip(s, enabled=False)
        self.assertFalse(off.strip_enabled)
        self.assertEqual(off.strip_brightness, 218)
        on = p.with_strip(off, enabled=True, brightness=40)
        self.assertTrue(on.strip_enabled)
        self.assertEqual(on.strip_brightness, 40)
        self.assertEqual(on.strip_flags, s.strip_flags)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.st = p.decode_status(load("status.bin"))

    def test_values_match_hwmon(self):
        # hwmon at capture time: temp1 31720, fan1..4 3024/362/363/503, fan5 (flow) 1092 dL/h,
        # power2..4 380000/840000/220000 uW, in0 12000 mV, curr2 32 mA
        self.assertEqual(self.st.firmware, 1033)
        self.assertEqual(self.st.temps, (31.72, None, None, None))
        self.assertEqual(self.st.flow_lph, 109.2)
        self.assertEqual(self.st.vcc12, 12.0)
        self.assertEqual([f.rpm for f in self.st.fans], [3024, 362, 363, 503])
        self.assertEqual([f.power_w for f in self.st.fans], [0.0, 0.38, 0.84, 0.22])
        self.assertEqual(self.st.fans[1].current_ma, 32)
        self.assertEqual([f.percent for f in self.st.fans], [34.86, 5.0, 5.0, 9.69])
        self.assertEqual(self.st.soft_sensors, (None,) * 16)
        self.assertEqual(self.st.profile, 1)

    def test_rejects_non_status(self):
        with self.assertRaises(p.ProtocolError):
            p.decode_status(load("status.bin")[:100])

    def test_fixture_serial_is_scrubbed(self):
        self.assertEqual(load("status.bin")[3:7], b"\x00\x00\x00\x00")

    def test_scrub_status_serial(self):
        raw = bytearray(load("status.bin"))
        raw[3:7] = b"\x01\x02\x03\x04"
        self.assertEqual(p.scrub_status_serial(bytes(raw))[3:7], b"\x00" * 4)


class NamesTest(unittest.TestCase):
    def test_decode_names(self):
        n = p.decode_names(load("names.bin"))
        self.assertEqual(n.fans, ("Pumpe", "140mm Radiator", "420mm Radiator", "Fan 4"))
        self.assertEqual(n.leds[0], "LED Controller 1")
        self.assertEqual(n.temps[0], "Wasser Temp")
        self.assertEqual(n.flow, "Flow")
        self.assertEqual(n.strip, "Strip")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: ERROR `ModuleNotFoundError: No module named 'aquacontrol.protocol'`

- [ ] **Step 4: Write the implementation**

`aquacontrol/protocol.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: 19 tests OK

- [ ] **Step 6: Commit**

```bash
git add .gitignore aquacontrol/__init__.py aquacontrol/protocol.py tests/__init__.py tests/fixtures/__init__.py tests/test_protocol.py tools/__init__.py
git commit -m "feat: QUADRO report codec (settings, status, names) with real-device fixtures"
```

### Task 2: Safety rules (validate.py)

**Files:**
- Create: `aquacontrol/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `aquacontrol.protocol` (Task 1).
- Produces:
  - `ValidationError(ValueError)`
  - `check(old: Settings, new: Settings, min_percent: dict[int, float]) -> None`, where the dict key is a 0-based channel index
  - `make_check(min_percent) -> Callable[[Settings, Settings], None]`
  - Only fields that changed are validated, and all messages are in German.

- [ ] **Step 1: Write the failing test**

`tests/test_validate.py`:

```python
import unittest

from aquacontrol import protocol as p
from aquacontrol.validate import ValidationError, check
from tests.fixtures import load

PUMP_FLOOR = {0: 25.0}


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.old = p.decode_settings(load("settings_live.bin"))

    def ok(self, new):
        check(self.old, new, PUMP_FLOOR)

    def bad(self, new, fragment):
        with self.assertRaises(ValidationError) as cm:
            check(self.old, new, PUMP_FLOOR)
        self.assertIn(fragment, str(cm.exception))

    def test_unchanged_is_ok(self):
        self.ok(self.old)

    def test_target_temperature(self):
        self.ok(p.with_controller(self.old, 1, target_c=40.0))
        self.bad(p.with_controller(self.old, 1, target_c=70.0), "Zieltemperatur")
        self.bad(p.with_controller(self.old, 1, target_c=float("nan")), "ungültige Zahl")

    def test_modes(self):
        self.ok(p.with_controller(self.old, 3, mode=p.MODE_CURVE))
        self.bad(p.with_controller(self.old, 3, mode=p.MODE_FOLLOW), "Modus")

    def test_follow_mode_already_set_stays_allowed(self):
        old = p.with_controller(self.old, 2, mode=p.MODE_FOLLOW)
        check(old, p.with_controller(old, 2, target_c=36.0), PUMP_FLOOR)

    def test_curve_must_rise_strictly(self):
        good = tuple((20.0 + i, 10.0 + i) for i in range(16))
        self.ok(p.with_controller(self.old, 2, curve=good))
        flat = tuple((20.0 + (i if i != 5 else 4), 10.0) for i in range(16))
        self.bad(p.with_controller(self.old, 2, curve=flat), "streng steigen")

    def test_curve_percent_and_length(self):
        over = tuple((20.0 + i, 101.0 if i == 15 else 10.0) for i in range(16))
        self.bad(p.with_controller(self.old, 2, curve=over), "zwischen 0 und 100")
        short = tuple((20.0 + i, 10.0) for i in range(15))
        self.bad(p.with_controller(self.old, 2, curve=short), "16 Punkte")

    def test_pump_curve_points_below_floor_are_allowed(self):
        # the device minimum (28 %) clamps them; only the minimum itself is guarded
        curve = tuple((20.0 + i, 0.0) for i in range(16))
        self.ok(p.with_controller(self.old, 0, curve=curve))

    def test_pump_minimum_floor(self):
        self.ok(p.with_fan(self.old, 0, min_percent=25.0))
        self.bad(p.with_fan(self.old, 0, min_percent=10.0), "Minimum 10.0 % unter erlaubtem 25.0 %")

    def test_pump_fixed_floor(self):
        self.bad(p.with_controller(self.old, 0, mode=p.MODE_FIXED, fixed_percent=5.0), "Fest-Wert")
        self.ok(p.with_controller(self.old, 0, mode=p.MODE_FIXED, fixed_percent=60.0))

    def test_switching_pump_to_fixed_checks_stored_value(self):
        old = p.with_controller(self.old, 0, fixed_percent=5.0)
        with self.assertRaises(ValidationError):
            check(old, p.with_controller(old, 0, mode=p.MODE_FIXED), PUMP_FLOOR)

    def test_min_below_max(self):
        self.bad(p.with_fan(self.old, 1, min_percent=60.0, max_percent=50.0), "kleiner als Maximum")

    def test_sensor_choice(self):
        self.ok(p.with_controller(self.old, 1, sensor=2))
        self.bad(p.with_controller(self.old, 1, sensor=7), "Sensor")

    def test_strip(self):
        self.ok(p.with_strip(self.old, enabled=False, brightness=0))
        self.bad(p.with_strip(self.old, brightness=300), "Helligkeit")
        from dataclasses import replace
        self.bad(replace(self.old, strip_flags=0x0100), "An/Aus-Bit")

    def test_read_only_fields(self):
        from dataclasses import replace
        self.bad(p.with_controller(self.old, 1, pid=(0, 0, 0, 0, 0, 0)), "PID")
        self.bad(p.with_fan(self.old, 1, fallback_percent=50.0), "Fallback")
        self.bad(replace(self.old, profile=2), "Profilnummer")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_validate -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.validate'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/validate.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_validate -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/validate.py tests/test_validate.py
git commit -m "feat: validation rules for settings changes (pump floor, curve shape, read-only fields)"
```

### Task 3: Backup store (backups.py)

**Files:**
- Create: `aquacontrol/backups.py`
- Test: `tests/test_backups.py`

**Interfaces:**
- Consumes: `check_settings_report` (Task 1).
- Produces `BackupError(ValueError)`, `BackupInfo(name, size, pinned)` and
  `BackupStore(directory, keep=50, clock=datetime.now)` with these methods:
  - `.save(report, reason='', pinned=False) -> str`
  - `.list() -> list[BackupInfo]`, newest first
  - `.load(name) -> bytes`, which rejects traversal and corrupt files
  - Files named `pinned_*` are never pruned.

- [ ] **Step 1: Write the failing test**

`tests/test_backups.py`:

```python
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from aquacontrol.backups import BackupError, BackupStore
from tests.fixtures import load


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 10, 4, 12, 0, 0)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class BackupStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BackupStore(self.tmp.name, keep=3, clock=FakeClock())
        self.report = load("settings_live.bin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load(self):
        name = self.store.save(self.report, "Kurve Pumpe")
        self.assertTrue(name.endswith("_kurve-pumpe.bin"))
        self.assertEqual(self.store.load(name), self.report)

    def test_list_newest_first_and_prune_keeps_pinned(self):
        pinned = self.store.save(self.report, "initial", pinned=True)
        names = [self.store.save(self.report, f"n{i}") for i in range(5)]
        listed = [b.name for b in self.store.list()]
        self.assertEqual(listed[:3], list(reversed(names[-3:])))
        self.assertIn(pinned, listed)
        self.assertEqual(len(listed), 4)  # 3 kept + pinned

    def test_rejects_invalid_report(self):
        with self.assertRaises(ValueError):
            self.store.save(self.report[:-1])

    def test_load_rejects_traversal_and_missing(self):
        for bad in ("../etc/passwd", "a/b.bin", "x.txt", ""):
            with self.subTest(bad=bad), self.assertRaises(BackupError):
                self.store.load(bad)
        with self.assertRaises(BackupError):
            self.store.load("missing.bin")

    def test_load_rejects_corrupted_file(self):
        Path(self.tmp.name, "broken.bin").write_bytes(b"\x03" + bytes(960))
        with self.assertRaises(BackupError) as cm:
            self.store.load("broken.bin")
        self.assertIn("beschädigt", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_backups -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.backups'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/backups.py`:

```python
"""Raw settings-report backups on disk. Files named `pinned_*` are never pruned."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .protocol import check_settings_report

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.bin$")


class BackupError(ValueError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    name: str
    size: int
    pinned: bool


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "backup"


class BackupStore:
    def __init__(self, directory: str | Path, keep: int = 50, clock=datetime.now):
        self.dir = Path(directory)
        self.keep = keep
        self._clock = clock

    def save(self, report: bytes, reason: str = "", pinned: bool = False) -> str:
        check_settings_report(report)
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = self._clock().strftime("%Y%m%d-%H%M%S-%f")
        name = f"{'pinned_' if pinned else ''}{stamp}_{_slug(reason)}.bin"
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(report)
        os.replace(tmp, self.dir / name)
        self._prune()
        return name

    def list(self) -> list[BackupInfo]:
        if not self.dir.exists():
            return []
        items = [BackupInfo(f.name, f.stat().st_size, f.name.startswith("pinned_"))
                 for f in self.dir.iterdir() if _NAME_RE.match(f.name)]
        # newest first; pinned and normal names both start with a sortable timestamp
        return sorted(items, key=lambda b: b.name.removeprefix("pinned_"), reverse=True)

    def load(self, name: str) -> bytes:
        if not _NAME_RE.match(name):
            raise BackupError(f"ungültiger Backup-Name {name!r}")
        path = self.dir / name
        if not path.is_file():
            raise BackupError(f"Backup {name} existiert nicht")
        data = path.read_bytes()
        try:
            check_settings_report(data)
        except ValueError as e:
            raise BackupError(f"Backup {name} ist beschädigt: {e}") from e
        return data

    def _prune(self) -> None:
        normal = [b for b in self.list() if not b.pinned]
        for old in normal[self.keep:]:
            (self.dir / old.name).unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_backups -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/backups.py tests/test_backups.py
git commit -m "feat: settings backup store with pruning and pinned backups"
```

### Task 4: hidraw transport and fake device (transport.py, fake.py)

**Files:**
- Create: `aquacontrol/transport.py`
- Create: `aquacontrol/fake.py`
- Test: `tests/test_transport.py`

**Interfaces:**
- Consumes: protocol constants (Task 1).
- Produces:
  - `DeviceUnavailable(OSError)` and `find_hidraw(sys_root='/sys/class/hidraw') -> str | None`
  - `HidrawTransport(find=find_hidraw)` with `.get_feature(report_id, length) -> bytes`, `.set_feature(report)` and `.write_output(report)`. Control operations are 200 ms apart.
  - `HidrawReader(find)` with `.read(timeout) -> bytes | None` and `.close()`
  - `hidiocgfeature(size)` and `hidiocsfeature(size)`
  - `aquacontrol.fake.FakeTransport(settings, names=None)` with the attributes `.settings`, `.present`, `.ignore_writes`, `.fail_next_write`, `.writes`, `.commits`
  - `aquacontrol.fake.FakeReader(status, interval=1.0)`
- The real ioctl path is only exercised on hardware (Task 14). The unit tests cover the ioctl numbers against `<linux/hidraw.h>` and the device discovery.

- [ ] **Step 1: Write the failing test**

`tests/test_transport.py`:

```python
import tempfile
import unittest
from pathlib import Path

from aquacontrol import transport


class IoctlNumberTest(unittest.TestCase):
    def test_matches_linux_hidraw_h(self):
        # HIDIOCGFEATURE(len) = _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x07, len)
        self.assertEqual(transport.hidiocgfeature(1013), 0xC3F54807)
        self.assertEqual(transport.hidiocsfeature(961), 0xC3C14806)


class FindHidrawTest(unittest.TestCase):
    def make(self, root: Path, name: str, hid_id: str):
        d = root / name / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME=x\n")

    def test_finds_quadro(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root, "hidraw0", "0003:0000046D:0000C52B")
            self.make(root, "hidraw1", "0003:00000C70:0000F00D")
            self.assertEqual(transport.find_hidraw(root), "/dev/hidraw1")

    def test_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(transport.find_hidraw(tmp))
        self.assertIsNone(transport.find_hidraw("/nonexistent/hidraw"))

    def test_open_without_device_raises(self):
        t = transport.HidrawTransport(find=lambda: None)
        with self.assertRaises(transport.DeviceUnavailable):
            t.get_feature(3, 1013)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_transport -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.transport'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/transport.py`:

```python
"""Linux hidraw access to the QUADRO. The device node is looked up on every
operation, so unplugging or handing the device to a VM never leaves a stale path."""
from __future__ import annotations

import fcntl
import os
import select
import time
from pathlib import Path
from typing import Callable, Protocol

HID_ID = "0003:00000C70:0000F00D"
MIN_CTRL_INTERVAL = 0.2  # seconds between control operations, as in the kernel driver

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


def hidiocsfeature(size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, 0x06, size)


def hidiocgfeature(size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, 0x07, size)


class DeviceUnavailable(OSError):
    """The QUADRO is not present (unplugged or passed through to a VM)."""


def find_hidraw(sys_root: str | Path = "/sys/class/hidraw") -> str | None:
    root = Path(sys_root)
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir()):
        try:
            uevent = (entry / "device" / "uevent").read_text()
        except OSError:
            continue
        if f"HID_ID={HID_ID}" in uevent.upper():
            return f"/dev/{entry.name}"
    return None


class Transport(Protocol):
    def get_feature(self, report_id: int, length: int) -> bytes: ...
    def set_feature(self, report: bytes) -> None: ...
    def write_output(self, report: bytes) -> None: ...


class HidrawTransport:
    def __init__(self, find: Callable[[], str | None] = find_hidraw):
        self._find = find
        self._last = 0.0

    def _open(self, flags: int) -> int:
        path = self._find()
        if path is None:
            raise DeviceUnavailable("QUADRO nicht gefunden (abgesteckt oder an eine VM durchgereicht?)")
        return os.open(path, flags)

    def _pace(self) -> None:
        wait = self._last + MIN_CTRL_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _done(self) -> None:
        self._last = time.monotonic()

    def get_feature(self, report_id: int, length: int) -> bytes:
        self._pace()
        fd = self._open(os.O_RDWR)
        try:
            buf = bytearray(length)
            buf[0] = report_id
            n = fcntl.ioctl(fd, hidiocgfeature(length), buf, True)
            return bytes(buf[:n]) if n > 0 else bytes(buf)
        finally:
            os.close(fd)
            self._done()

    def set_feature(self, report: bytes) -> None:
        self._pace()
        fd = self._open(os.O_RDWR)
        try:
            fcntl.ioctl(fd, hidiocsfeature(len(report)), bytearray(report), True)
        finally:
            os.close(fd)
            self._done()

    def write_output(self, report: bytes) -> None:
        self._pace()
        fd = self._open(os.O_RDWR)
        try:
            os.write(fd, report)
        finally:
            os.close(fd)
            self._done()


class HidrawReader:
    """Reads input reports (the device sends a status report about once a second)."""

    def __init__(self, find: Callable[[], str | None] = find_hidraw):
        path = find()
        if path is None:
            raise DeviceUnavailable("QUADRO nicht gefunden")
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def read(self, timeout: float) -> bytes | None:
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self._fd, 256)

    def close(self) -> None:
        os.close(self._fd)
```

`aquacontrol/fake.py`:

```python
"""In-memory QUADRO used by the tests and by `--fake` dev mode."""
from __future__ import annotations

import threading

from .protocol import (COMMIT_REPORT, NAMES_REPORT_ID, SETTINGS_REPORT_ID, SETTINGS_REPORT_LEN,
                       STATUS_REPORT_LEN)
from .transport import DeviceUnavailable


class FakeTransport:
    def __init__(self, settings: bytes, names: bytes | None = None):
        self.settings = settings
        self.names = names
        self.present = True
        self.ignore_writes = False      # simulate firmware that silently drops writes
        self.writes: list[bytes] = []   # every settings report written
        self.commits = 0
        self.fail_next_write = False
        self._lock = threading.Lock()

    def _check(self) -> None:
        if not self.present:
            raise DeviceUnavailable("fake device absent")

    def get_feature(self, report_id: int, length: int) -> bytes:
        self._check()
        if report_id == SETTINGS_REPORT_ID:
            return self.settings
        if report_id == NAMES_REPORT_ID and self.names is not None:
            return self.names
        raise OSError(f"fake: unsupported feature report {report_id:#04x}")

    def set_feature(self, report: bytes) -> None:
        self._check()
        with self._lock:
            if self.fail_next_write:
                self.fail_next_write = False
                raise OSError("fake: write failed")
            assert report[0] == SETTINGS_REPORT_ID and len(report) == SETTINGS_REPORT_LEN
            self.writes.append(report)
            if not self.ignore_writes:
                self.settings = report

    def write_output(self, report: bytes) -> None:
        self._check()
        assert report == COMMIT_REPORT
        self.commits += 1


class FakeReader:
    """Replays one status report forever, like the real device does once a second."""

    def __init__(self, status: bytes, interval: float = 1.0):
        assert len(status) == STATUS_REPORT_LEN
        self._status = status
        self._interval = interval
        self._stop = threading.Event()

    def read(self, timeout: float) -> bytes | None:
        if self._stop.wait(min(timeout, self._interval)):
            return None
        return self._status

    def close(self) -> None:
        self._stop.set()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_transport -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/transport.py aquacontrol/fake.py tests/test_transport.py
git commit -m "feat: hidraw transport with device discovery, plus in-memory fake device"
```

### Task 5: Device: verified writes with backup and rollback (device.py)

**Files:**
- Create: `aquacontrol/device.py`
- Test: `tests/test_device.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `DeviceError(Exception)`, `VerifyError(DeviceError)`, `ApplyResult(changed: bool, backup: str | None)`
  - `VOLATILE_OFFSETS: frozenset[int]`, empty for now; Task 14 may fill it.
  - `Device(transport, backups: BackupStore | None, check)` with these methods:
    - `.read_report() -> bytes`, `.read_settings() -> Settings`, `.read_names() -> Names`
    - `.apply(mutate, *, backup=True, reason='') -> ApplyResult`
    - `.restore(report, reason='restore') -> ApplyResult`
    - `.rewrite_current() -> None`
  - Every operation holds one `threading.Lock`. `ValidationError` from `check` propagates unchanged.

- [ ] **Step 1: Write the failing test**

`tests/test_device.py`:

```python
import tempfile
import threading
import unittest

from aquacontrol import protocol as p
from aquacontrol.backups import BackupStore
from aquacontrol.device import Device, DeviceError
from aquacontrol.fake import FakeTransport
from aquacontrol.validate import ValidationError, make_check
from tests.fixtures import load


class DeviceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = load("settings_live.bin")
        self.fake = FakeTransport(self.original, load("names.bin"))
        self.backups = BackupStore(self.tmp.name)
        self.dev = Device(self.fake, self.backups, make_check({0: 25.0}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_writes_commits_and_backs_up(self):
        result = self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0), reason="test")
        self.assertTrue(result.changed)
        self.assertEqual(self.fake.commits, 1)
        self.assertEqual(self.dev.read_settings().controllers[3].target_c, 38.0)
        self.assertEqual(self.backups.load(result.backup), self.original)

    def test_noop_does_not_write(self):
        result = self.dev.apply(lambda s: s)
        self.assertFalse(result.changed)
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.backups.list(), [])

    def test_validation_error_writes_nothing(self):
        with self.assertRaises(ValidationError):
            self.dev.apply(lambda s: p.with_fan(s, 0, min_percent=5.0))
        self.assertEqual(self.fake.writes, [])

    def test_bad_crc_from_device_aborts(self):
        corrupt = bytearray(self.original)
        corrupt[50] ^= 1
        self.fake.settings = bytes(corrupt)
        with self.assertRaises(DeviceError):
            self.dev.apply(lambda s: s)
        self.assertEqual(self.fake.writes, [])

    def test_verify_failure_rolls_back(self):
        self.fake.ignore_writes = True  # device keeps the old settings
        with self.assertRaises(DeviceError) as cm:
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertIn("wiederhergestellt", str(cm.exception))
        self.assertEqual(len(self.fake.writes), 2)
        self.assertEqual(self.fake.writes[1], self.original)  # rollback write

    def test_write_error_rolls_back(self):
        self.fake.fail_next_write = True
        with self.assertRaises(DeviceError):
            self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        self.assertEqual(self.fake.settings, self.original)

    def test_device_absent(self):
        self.fake.present = False
        with self.assertRaises(DeviceError):
            self.dev.read_settings()
        with self.assertRaises(DeviceError):
            self.dev.apply(lambda s: s)

    def test_no_backup_flag(self):
        result = self.dev.apply(lambda s: p.with_strip(s, enabled=False), backup=False)
        self.assertTrue(result.changed)
        self.assertIsNone(result.backup)
        self.assertEqual(self.backups.list(), [])

    def test_restore(self):
        self.dev.apply(lambda s: p.with_controller(s, 3, target_c=38.0))
        result = self.dev.restore(self.original)
        self.assertTrue(result.changed)
        self.assertEqual(self.fake.settings, self.original)

    def test_restore_rejects_corrupt_report(self):
        with self.assertRaises(ValueError):
            self.dev.restore(self.original[:-1])

    def test_concurrent_applies_are_serialised(self):
        barrier = threading.Barrier(2)
        errors = []

        def edit(idx, value):
            try:
                barrier.wait()
                self.dev.apply(lambda s: p.with_controller(s, idx, target_c=value), backup=False)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=edit, args=(1, 30.0)), threading.Thread(target=edit, args=(2, 31.0))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        s = self.dev.read_settings()
        self.assertEqual((s.controllers[1].target_c, s.controllers[2].target_c), (30.0, 31.0))

    def test_read_names(self):
        self.assertEqual(self.dev.read_names().fans[0], "Pumpe")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_device -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.device'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/device.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_device -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/device.py tests/test_device.py
git commit -m "feat: serialized device writes with backup, read-back verify and rollback"
```

### Task 6: Display-only sensors: host hwmon and pushed values (sensors.py)

**Files:**
- Create: `aquacontrol/sensors.py`
- Test: `tests/test_sensors.py`

**Interfaces:**
- Produces:
  - `Reading(id, label, value, unit)`
  - `read_host_sensors(labels: dict[str, str | None], sys_root='/sys/class/hwmon') -> list[Reading]`. It skips `quadro` and unreadable sensors. A label of `None` hides a sensor.
  - `ExternalError(ValueError)`
  - `ExternalStore(clock=time.monotonic)` with `.put(source, payload) -> int`, `.current() -> list[Reading]` (entries older than 30 s are dropped) and `.sources() -> dict[str, float age_s]`
  - Constants `EXTERNAL_MAX_SENSORS = 32`, `EXTERNAL_UNITS = ('°C', 'W', '%')`.

- [ ] **Step 1: Write the failing test**

`tests/test_sensors.py`:

```python
import tempfile
import unittest
from pathlib import Path

from aquacontrol.sensors import ExternalError, ExternalStore, read_host_sensors


def hwmon(root: Path, idx: int, name: str, temps: dict[str, tuple[str | None, str]]):
    d = root / f"hwmon{idx}"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for chan, (label, value) in temps.items():
        (d / f"{chan}_input").write_text(value + "\n")
        if label is not None:
            (d / f"{chan}_label").write_text(label + "\n")


class HostSensorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        hwmon(root, 0, "nvme", {"temp1": ("Composite", "44850")})
        hwmon(root, 1, "k10temp", {"temp1": ("Tctl", "51000"), "temp3": ("Tccd1", "39750")})
        hwmon(root, 2, "spd5118", {"temp1": (None, "42500")})
        hwmon(root, 4, "quadro", {"temp1": ("Coolant temp", "31720")})
        hwmon(root, 5, "amdgpu", {"temp1": ("edge", "garbage")})
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_all_but_quadro_and_broken(self):
        r = read_host_sensors({}, self.root)
        self.assertEqual([x.id for x in r], ["nvme/Composite", "k10temp/Tctl", "k10temp/Tccd1", "spd5118/temp1"])
        self.assertEqual(r[0].value, 44.85)
        self.assertEqual(r[0].unit, "°C")

    def test_rename_and_hide(self):
        r = read_host_sensors({"k10temp/Tctl": "CPU", "spd5118/temp1": None}, self.root)
        ids = {x.id: x.label for x in r}
        self.assertEqual(ids["k10temp/Tctl"], "CPU")
        self.assertNotIn("spd5118/temp1", ids)

    def test_missing_root(self):
        self.assertEqual(read_host_sensors({}, "/nonexistent"), [])


class ExternalStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = ExternalStore(clock=lambda: self.now)
        self.body = {"source": "llm-vm", "sensors": [
            {"id": "gpu0", "label": "GPU 0", "value": 45.0, "unit": "°C"},
            {"id": "gpu0_power", "label": "GPU 0 Leistung", "value": 250.5, "unit": "W"}]}

    def test_put_and_expire(self):
        self.assertEqual(self.store.put("llm-vm", self.body), 2)
        self.assertEqual([r.id for r in self.store.current()], ["llm-vm/gpu0", "llm-vm/gpu0_power"])
        self.now += 31
        self.assertEqual(self.store.current(), [])
        self.assertAlmostEqual(self.store.sources()["llm-vm"], 31)

    def test_source_must_match_token(self):
        with self.assertRaises(ExternalError):
            self.store.put("other", self.body)

    def test_rejects_bad_payloads(self):
        bad = [
            None, [], {"source": "llm-vm"}, {"source": "llm-vm", "sensors": []},
            {"source": "llm-vm", "sensors": [{"id": "GPU 0!", "value": 1, "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": "hot", "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": float("nan"), "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": True, "unit": "°C"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": 1, "unit": "K"}]},
            {"source": "llm-vm", "sensors": [{"id": "g", "value": 1, "unit": "°C"}] * 33},
        ]
        for body in bad:
            with self.subTest(body=str(body)[:60]), self.assertRaises(ExternalError):
                self.store.put("llm-vm", body)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_sensors -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.sensors'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/sensors.py`:

```python
"""Display-only temperatures: host hwmon sensors and values pushed by other machines."""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

EXTERNAL_MAX_SENSORS = 32
EXTERNAL_MAX_AGE_S = 30.0
EXTERNAL_UNITS = ("°C", "W", "%")
_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


@dataclass(frozen=True)
class Reading:
    id: str        # "k10temp/Tctl" or "llm-vm/gpu0"
    label: str
    value: float
    unit: str


def _natural(path: Path) -> tuple[str, int]:
    m = re.match(r"^(\D*)(\d*)$", path.name)
    return (m.group(1), int(m.group(2) or 0)) if m else (path.name, 0)


def read_host_sensors(labels: dict[str, str | None], sys_root: str | Path = "/sys/class/hwmon") -> list[Reading]:
    """All hwmon temperatures except the QUADRO's. `labels` renames (str) or hides (None)."""
    out: list[Reading] = []
    root = Path(sys_root)
    if not root.is_dir():
        return out
    for hw in sorted(root.iterdir(), key=_natural):
        try:
            name = (hw / "name").read_text().strip()
        except OSError:
            continue
        if name == "quadro":
            continue
        for inp in sorted(hw.glob("temp*_input"), key=lambda f: _natural(Path(f.name.removesuffix("_input")))):
            chan = inp.name.removesuffix("_input")
            try:
                label = (hw / f"{chan}_label").read_text().strip()
            except OSError:
                label = chan
            sid = f"{name}/{label}"
            if sid in labels and labels[sid] is None:
                continue
            try:
                value = int(inp.read_text().strip()) / 1000
            except (OSError, ValueError):
                continue  # e.g. ENODATA for an unconnected sensor
            out.append(Reading(sid, labels.get(sid) or sid, value, "°C"))
    return out


class ExternalError(ValueError):
    pass


class ExternalStore:
    """Latest values pushed per source; entries older than EXTERNAL_MAX_AGE_S count as missing."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, list[Reading]]] = {}

    def put(self, source: str, payload: object) -> int:
        if not isinstance(payload, dict) or payload.get("source") != source:
            raise ExternalError("source im Body passt nicht zum Token")
        sensors = payload.get("sensors")
        if not isinstance(sensors, list) or not 0 < len(sensors) <= EXTERNAL_MAX_SENSORS:
            raise ExternalError(f"sensors muss eine Liste mit 1–{EXTERNAL_MAX_SENSORS} Einträgen sein")
        readings = []
        for s in sensors:
            if not isinstance(s, dict):
                raise ExternalError("Sensor-Eintrag muss ein Objekt sein")
            sid, label, value, unit = s.get("id"), s.get("label", s.get("id")), s.get("value"), s.get("unit")
            if not isinstance(sid, str) or not _ID_RE.match(sid):
                raise ExternalError(f"ungültige Sensor-id {sid!r}")
            if not isinstance(label, str) or len(label) > 40:
                raise ExternalError(f"ungültiges label für {sid}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not -50 <= value <= 1000:
                raise ExternalError(f"ungültiger Wert für {sid}")
            if unit not in EXTERNAL_UNITS:
                raise ExternalError(f"Einheit für {sid} muss eine von {EXTERNAL_UNITS} sein")
            readings.append(Reading(f"{source}/{sid}", label, float(value), unit))
        with self._lock:
            self._data[source] = (self._clock(), readings)
        return len(readings)

    def current(self) -> list[Reading]:
        now = self._clock()
        with self._lock:
            return [r for ts, rs in self._data.values() if now - ts <= EXTERNAL_MAX_AGE_S for r in rs]

    def sources(self) -> dict[str, float]:
        """Age in seconds of the last push per source."""
        now = self._clock()
        with self._lock:
            return {src: now - ts for src, (ts, _) in self._data.items()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_sensors -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/sensors.py tests/test_sensors.py
git commit -m "feat: host hwmon temperatures and validated store for pushed sensors"
```

### Task 7: Monitor: live state and history (monitor.py)

**Files:**
- Create: `aquacontrol/monitor.py`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: `decode_status` (Task 1), `Reading` (Task 6), `FakeReader` (Task 4, tests only).
- Produces `Monitor(open_reader, extra=list, bucket_s=10, history_s=21600, clock=time.time)` with these methods:
  - `.ingest(report, now=None)`
  - `.snapshot() -> {online, updated, status (asdict), sensors (list of Reading dicts)}`
  - `.history(minutes) -> list[dict]`. The keys are `t`, `temp1..4`, `flow`, `fanN_rpm`, `fanN_percent` and extra °C sensor ids.
  - `.run(stop: threading.Event)`
- The monitor counts as offline when the last report is older than 5 s. The reader is retried every 5 s.

- [ ] **Step 1: Write the failing test**

`tests/test_monitor.py`:

```python
import threading
import unittest

from aquacontrol.monitor import Monitor
from aquacontrol.sensors import Reading
from tests.fixtures import load


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.extra = [Reading("k10temp/Tctl", "CPU", 51.0, "°C"), Reading("llm-vm/p", "P", 200.0, "W")]
        self.mon = Monitor(open_reader=lambda: None, extra=lambda: self.extra, clock=self.clock)
        self.status = load("status.bin")

    def test_offline_before_first_report(self):
        self.assertFalse(self.mon.snapshot()["online"])

    def test_snapshot_online_then_stale(self):
        self.mon.ingest(self.status)
        snap = self.mon.snapshot()
        self.assertTrue(snap["online"])
        self.assertEqual(snap["status"]["temps"][0], 31.72)
        self.assertEqual(snap["sensors"][0]["label"], "CPU")
        self.clock.t += 6
        self.assertFalse(self.mon.snapshot()["online"])

    def test_history_buckets_average(self):
        for i in range(25):  # 25 s of reports -> buckets at t0, t0+10 complete, t0+20 open
            self.mon.ingest(self.status, now=self.clock.t + i)
        self.clock.t += 25
        hist = self.mon.history(60)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["temp1"], 31.72)
        self.assertEqual(hist[0]["fan1_rpm"], 3024)
        self.assertEqual(hist[0]["k10temp/Tctl"], 51.0)
        self.assertNotIn("llm-vm/p", hist[0])  # only temperatures go into history
        self.assertNotIn("temp2", hist[0])     # missing sensor

    def test_extra_sensor_failure_does_not_break_ingest(self):
        def boom():
            raise OSError("hwmon gone")
        mon = Monitor(open_reader=lambda: None, extra=boom, clock=self.clock)
        mon.ingest(self.status)
        self.assertTrue(mon.snapshot()["online"])

    def test_run_survives_missing_device(self):
        calls = []
        stop = threading.Event()

        def opener():
            calls.append(1)
            stop.set()
            raise OSError("no device")

        Monitor(open_reader=opener, clock=self.clock).run(stop)
        self.assertEqual(calls, [1])

    def test_run_ingests_from_reader(self):
        from aquacontrol.fake import FakeReader
        stop = threading.Event()
        reader = FakeReader(self.status, interval=0.01)
        mon = Monitor(open_reader=lambda: reader, clock=self.clock)
        t = threading.Thread(target=mon.run, args=(stop,))
        t.start()
        for _ in range(100):
            if mon.snapshot()["online"]:
                break
            threading.Event().wait(0.01)
        stop.set()
        t.join(2)
        self.assertTrue(mon.snapshot()["online"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_monitor -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.monitor'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/monitor.py`:

```python
"""Live state and in-memory history. The QUADRO pushes a status report about once a
second; host and external sensors are sampled alongside it."""
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
        self._history: deque[dict] = deque(maxlen=history_s // bucket_s)
        self._bucket: tuple[int, dict[str, list[float]]] | None = None

    def ingest(self, report: bytes, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        status = decode_status(report)
        try:
            extra = self._extra()
        except Exception:  # sensor glitches must never stop the monitor
            log.exception("reading extra sensors failed")
            extra = []
        with self._lock:
            self._latest = (now, status)
            self._extra_latest = extra
            self._add_to_bucket(now, status, extra)

    def _add_to_bucket(self, now: float, status: Status, extra: list[Reading]) -> None:
        start = int(now // self._bucket_s * self._bucket_s)
        if self._bucket is not None and self._bucket[0] != start:
            self._finish_bucket()
        if self._bucket is None:
            self._bucket = (start, {})
        values = self._bucket[1]

        def add(key: str, v: float | None) -> None:
            if v is not None:
                values.setdefault(key, []).append(v)

        for i, t in enumerate(status.temps):
            add(f"temp{i + 1}", t)
        add("flow", status.flow_lph)
        for i, f in enumerate(status.fans):
            add(f"fan{i + 1}_rpm", f.rpm)
            add(f"fan{i + 1}_percent", f.percent)
        for r in extra:
            if r.unit == "°C":
                add(r.id, r.value)

    def _finish_bucket(self) -> None:
        start, values = self._bucket
        self._history.append({"t": start, **{k: round(sum(v) / len(v), 2) for k, v in values.items()}})
        self._bucket = None

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            if self._latest is None:
                return {"online": False, "updated": None, "status": None, "sensors": []}
            ts, status = self._latest
            return {
                "online": now - ts <= OFFLINE_AFTER_S,
                "updated": ts,
                "status": asdict(status),
                "sensors": [asdict(r) for r in self._extra_latest],
            }

    def history(self, minutes: int) -> list[dict]:
        cutoff = self._clock() - minutes * 60
        with self._lock:
            return [h for h in self._history if h["t"] >= cutoff]

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                reader = self._open_reader()
            except OSError as e:
                log.warning("status reader unavailable: %s", e)
                stop.wait(RETRY_S)
                continue
            try:
                while not stop.is_set():
                    data = reader.read(1.0)
                    if data and data[0] == STATUS_REPORT_ID and len(data) == STATUS_REPORT_LEN:
                        try:
                            self.ingest(data)
                        except ProtocolError as e:
                            log.warning("bad status report: %s", e)
            except OSError as e:
                log.warning("status reader lost: %s", e)
            finally:
                reader.close()
            stop.wait(RETRY_S)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_monitor -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/monitor.py tests/test_monitor.py
git commit -m "feat: status monitor with 6 h in-memory history"
```

### Task 8: LED schedule (schedule.py)

**Files:**
- Create: `aquacontrol/schedule.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: `with_strip` (Task 1), `Device.apply` (Task 5).
- Produces:
  - `ScheduleError(ValueError)`
  - `Rule(at: time, on: bool, brightness: int | None, days: frozenset[int], target='strip')`, `StripState(on, brightness=None)`, `Override(state, set_at)`
  - `parse_rules(raw) -> list[Rule]` and `rules_to_json(rules) -> list[dict]`. In v1 the only valid target is `strip`.
  - `last_rule_before(rules, now)`, `next_change(rules, now)`, `desired_state(rules, now, override=None) -> StripState | None`
  - `Scheduler(device, get_rules, clock=datetime.now)` with `.tick()`, `.set_override(on, brightness=None)`, `.status() -> dict(desired, next_change, override_active, last_error)` and `.run(stop)`
- Schedule writes use `backup=False`.

- [ ] **Step 1: Write the failing test**

`tests/test_schedule.py`:

```python
import unittest
from datetime import datetime, time

from aquacontrol import protocol as p
from aquacontrol.device import Device
from aquacontrol.fake import FakeTransport
from aquacontrol.schedule import (Override, Rule, ScheduleError, Scheduler, StripState, desired_state,
                                  next_change, parse_rules, rules_to_json)
from aquacontrol.validate import make_check
from tests.fixtures import load

NIGHT = parse_rules([
    {"time": "01:00", "target": "strip", "on": False},
    {"time": "09:00", "target": "strip", "on": True, "brightness": 218},
])


def at(day, hh, mm):  # 2026-10-05 is a Monday
    return datetime(2026, 10, day, hh, mm)


class ParseTest(unittest.TestCase):
    def test_parse_and_back(self):
        raw = [{"time": "01:00", "target": "strip", "on": False},
               {"time": "09:30", "target": "strip", "on": True, "brightness": 100, "days": [5, 6]}]
        rules = parse_rules(raw)
        self.assertEqual(rules[1], Rule(time(9, 30), True, 100, frozenset({5, 6})))
        self.assertEqual(rules_to_json(rules), raw)

    def test_rejects_invalid(self):
        bad = [
            "x", [{"time": "25:00", "on": True}], [{"time": "9:00", "on": True}],
            [{"time": "09:00", "on": "yes"}], [{"time": "09:00", "on": True, "brightness": 256}],
            [{"time": "09:00", "on": True, "days": []}], [{"time": "09:00", "on": True, "days": [7]}],
            [{"time": "09:00", "on": True, "target": "led:1"}], [[]],
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ScheduleError):
                parse_rules(raw)


class DesiredStateTest(unittest.TestCase):
    def test_daytime_on(self):
        self.assertEqual(desired_state(NIGHT, at(5, 12, 0)), StripState(True, 218))

    def test_night_off(self):
        self.assertEqual(desired_state(NIGHT, at(5, 3, 0)), StripState(False, None))

    def test_just_after_midnight_uses_previous_day(self):
        self.assertEqual(desired_state(NIGHT, at(5, 0, 30)), StripState(True, 218))

    def test_exact_rule_time_applies(self):
        self.assertEqual(desired_state(NIGHT, at(5, 1, 0)), StripState(False, None))

    def test_no_rules(self):
        self.assertIsNone(desired_state([], at(5, 12, 0)))

    def test_weekday_filter(self):
        rules = parse_rules([{"time": "08:00", "on": True, "days": [5, 6]},   # weekend
                             {"time": "07:00", "on": False, "days": [0]}])    # Monday
        # Monday 12:00: last matching rule is Monday 07:00 (off)
        self.assertEqual(desired_state(rules, at(5, 12, 0)).on, False)
        # Sunday 2026-10-11 09:00: weekend rule at 08:00 (on)
        self.assertEqual(desired_state(rules, datetime(2026, 10, 11, 9, 0)).on, True)

    def test_override_until_next_rule(self):
        ov = Override(StripState(True), at(5, 2, 0))  # turned on at night by hand
        self.assertEqual(desired_state(NIGHT, at(5, 3, 0), ov), StripState(True))
        self.assertEqual(desired_state(NIGHT, at(5, 9, 0), ov), StripState(True, 218))
        self.assertEqual(desired_state(NIGHT, at(6, 1, 30), ov), StripState(False, None))

    def test_next_change(self):
        self.assertEqual(next_change(NIGHT, at(5, 12, 0)), at(6, 1, 0))
        self.assertIsNone(next_change([], at(5, 12, 0)))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransport(load("settings_live.bin"))
        self.dev = Device(self.fake, None, make_check({0: 25.0}))
        self.now = at(5, 3, 0)
        self.sched = Scheduler(self.dev, lambda: NIGHT, clock=lambda: self.now)

    def test_catch_up_turns_off_and_writes_once(self):
        result = self.sched.tick()
        self.assertTrue(result.changed)
        self.assertFalse(self.dev.read_settings().strip_enabled)
        self.assertEqual(self.dev.read_settings().strip_brightness, 218)
        self.assertFalse(self.sched.tick().changed)  # second tick: nothing to do
        self.assertEqual(len(self.fake.writes), 1)

    def test_morning_turns_on_with_brightness(self):
        self.sched.tick()
        self.now = at(5, 9, 0)
        self.sched.tick()
        s = self.dev.read_settings()
        self.assertTrue(s.strip_enabled)
        self.assertEqual(s.strip_brightness, 218)

    def test_override(self):
        self.sched.tick()
        self.sched.set_override(True)
        self.sched.tick()
        self.assertTrue(self.dev.read_settings().strip_enabled)
        self.assertTrue(self.sched.status()["override_active"])

    def test_no_rules_no_writes(self):
        sched = Scheduler(self.dev, lambda: [], clock=lambda: self.now)
        self.assertIsNone(sched.tick())
        self.assertEqual(self.fake.writes, [])

    def test_status(self):
        st = self.sched.status()
        self.assertEqual(st["desired"], {"on": False, "brightness": None})
        self.assertEqual(st["next_change"], "2026-10-05T09:00")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_schedule -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.schedule'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/schedule.py`:

```python
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

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ALL_DAYS = frozenset(range(7))  # Monday = 0, like datetime.weekday()


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
        m = _TIME_RE.match(str(item.get("time", "")))
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

    def set_override(self, on: bool, brightness: int | None = None) -> None:
        with self._lock:
            self._override = Override(StripState(on, brightness), self._clock())

    def tick(self):
        now = self._clock()
        with self._lock:
            override = self._override
        state = desired_state(self._get_rules(), now, override)
        if state is None:
            return None
        return self._device.apply(
            lambda s: with_strip(s, enabled=state.on, brightness=state.brightness),
            backup=False, reason="schedule")

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
                self.last_error = None
            except Exception as e:  # keep running; the UI shows the error
                self.last_error = str(e)
                log.warning("schedule tick failed: %s", e)
            stop.wait(60 - self._clock().second + 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_schedule -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/schedule.py tests/test_schedule.py
git commit -m "feat: time-based LED strip schedule with catch-up and manual override"
```

### Task 9: Auth and configuration (auth.py, config.py)

**Files:**
- Create: `aquacontrol/auth.py`
- Create: `aquacontrol/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `parse_rules`, `rules_to_json` and `Rule` (Task 8).
- Produces, in `aquacontrol.auth`: `hash_password(pw, iterations=600000, salt=None) -> 'pbkdf2_sha256$iter$salt$hash'`, `verify_password`, `hash_token`, `new_token` and `token_source(token, push_tokens) -> source | None`.
- Produces, in `aquacontrol.config`:
  - `ConfigError`, `DEFAULT_APP_CONFIG`
  - `DaemonConfig(listen, port, password_hash, push_tokens)`, `load_daemon_config(path)`, `update_daemon_config(path, **changes)`; the key `push_token=(source, hash)` adds a token.
  - `AppConfig(path)` with `.fan_name(i0, fallback)`, `.min_percent() -> {i0: float}`, `.sensor_name`, `.led_name`, `.host_sensor_labels()`, `.backup_dir`, `.backup_keep`, `.rules()` and `.set_rules(raw)`. `set_rules` validates first, then writes atomically.

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

from aquacontrol.auth import hash_password, hash_token, token_source, verify_password
from aquacontrol.config import (AppConfig, ConfigError, load_daemon_config, update_daemon_config)
from aquacontrol.schedule import ScheduleError


class AuthTest(unittest.TestCase):
    def test_password_roundtrip(self):
        h = hash_password("geheim", iterations=1000)
        self.assertTrue(verify_password("geheim", h))
        self.assertFalse(verify_password("falsch", h))

    def test_malformed_hash(self):
        for bad in ("", "x", "md5$1$a$b", "pbkdf2_sha256$abc$$"):
            self.assertFalse(verify_password("x", bad))

    def test_token_source(self):
        tokens = {"llm-vm": hash_token("t0k3n")}
        self.assertEqual(token_source("t0k3n", tokens), "llm-vm")
        self.assertIsNone(token_source("other", tokens))


class DaemonConfigTest(unittest.TestCase):
    def test_load_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "daemon.json")
            path.write_text(json.dumps({"port": 9443}))
            update_daemon_config(path, password_hash="h")
            update_daemon_config(path, push_token=("llm-vm", "abc"))
            cfg = load_daemon_config(path)
            self.assertEqual((cfg.port, cfg.password_hash, cfg.push_tokens), (9443, "h", {"llm-vm": "abc"}))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)  # existing file mode kept

    def test_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "daemon.json")
            path.write_text("{nope")
            with self.assertRaises(ConfigError):
                load_daemon_config(path)
            path.write_text(json.dumps({"port": 0}))
            with self.assertRaises(ConfigError):
                load_daemon_config(path)


class AppConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_without_file(self):
        cfg = AppConfig(self.path)
        self.assertEqual(cfg.fan_name(0, "x"), "Pumpe")
        self.assertEqual(cfg.fan_name(3, "Fan 4"), "Gehäuselüfter")
        self.assertEqual(cfg.min_percent(), {0: 25.0})
        self.assertEqual(len(cfg.rules()), 2)
        self.assertEqual(cfg.led_name(0, "LED Controller 1"), "LED Controller 1")

    def test_set_rules_persists_atomically(self):
        cfg = AppConfig(self.path)
        cfg.set_rules([{"time": "22:00", "target": "strip", "on": False}])
        again = AppConfig(self.path)
        self.assertEqual(len(again.rules()), 1)
        self.assertEqual(json.loads(self.path.read_text())["schedule"][0]["time"], "22:00")

    def test_set_rules_invalid_keeps_old(self):
        cfg = AppConfig(self.path)
        with self.assertRaises(ScheduleError):
            cfg.set_rules([{"time": "99:00", "on": True}])
        self.assertEqual(len(cfg.rules()), 2)
        self.assertFalse(self.path.exists())

    def test_corrupt_file(self):
        self.path.write_text("{")
        with self.assertRaises(ConfigError):
            AppConfig(self.path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_config -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'aquacontrol.auth'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/auth.py`:

```python
"""Password hashing (PBKDF2-SHA256) and push-token hashing (SHA-256)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_source(token: str, push_tokens: dict[str, str]) -> str | None:
    """Return the source name whose stored hash matches `token`, else None."""
    h = hash_token(token)
    for source, stored in push_tokens.items():
        if hmac.compare_digest(h, stored):
            return source
    return None
```

`aquacontrol/config.py`:

```python
"""Two config files: daemon.json (admin-owned, read-only for the daemon) and
config.json (daemon-owned, rewritten atomically when the schedule changes)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .schedule import Rule, parse_rules, rules_to_json

DEFAULT_APP_CONFIG = {
    "fans": {"1": {"name": "Pumpe", "min_percent": 25}, "2": {"name": "140mm Radiator"},
             "3": {"name": "420mm Radiator"}, "4": {"name": "Gehäuselüfter"}},
    "sensors": {"1": "Wasser Temp"},
    "host_sensors": {"k10temp/Tctl": "CPU", "nvme/Composite": "NVMe"},
    "leds": {},
    "schedule": [
        {"time": "01:00", "target": "strip", "on": False},
        {"time": "09:00", "target": "strip", "on": True},
    ],
    "backup_dir": "/var/lib/aquacontrol/backups",
    "backup_keep": 50,
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DaemonConfig:
    listen: str = "0.0.0.0"
    port: int = 8443
    password_hash: str = ""
    push_tokens: dict[str, str] = field(default_factory=dict)


def load_daemon_config(path: str | Path) -> DaemonConfig:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    tokens = raw.get("push_tokens", {})
    if not isinstance(tokens, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tokens.items()):
        raise ConfigError(f"{path}: push_tokens muss ein Objekt aus Strings sein")
    port = raw.get("port", 8443)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"{path}: ungültiger port")
    return DaemonConfig(str(raw.get("listen", "0.0.0.0")), port, str(raw.get("password_hash", "")), tokens)


def update_daemon_config(path: str | Path, **changes) -> None:
    """Used by the CLI (runs as root) to set the password hash or a push token."""
    p = Path(path)
    raw = json.loads(p.read_text()) if p.exists() else {}
    for key, value in changes.items():
        if key == "push_token":
            source, token_hash = value
            raw.setdefault("push_tokens", {})[source] = token_hash
        else:
            raw[key] = value
    _atomic_write(p, raw, mode=0o640)


def _atomic_write(path: Path, data: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if path.exists():
        st = path.stat()
        os.chmod(tmp, st.st_mode & 0o777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    else:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


class AppConfig:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        if self.path.exists():
            try:
                self._raw = json.loads(self.path.read_text())
            except json.JSONDecodeError as e:
                raise ConfigError(f"{self.path}: {e}") from e
        else:
            self._raw = json.loads(json.dumps(DEFAULT_APP_CONFIG))
        self._rules = parse_rules(self._raw.get("schedule", []))

    def fan_name(self, index: int, fallback: str) -> str:
        return self._raw.get("fans", {}).get(str(index + 1), {}).get("name") or fallback

    def min_percent(self) -> dict[int, float]:
        out = {}
        for key, fan in self._raw.get("fans", {}).items():
            if isinstance(fan, dict) and isinstance(fan.get("min_percent"), (int, float)):
                out[int(key) - 1] = float(fan["min_percent"])
        return out

    def sensor_name(self, index: int, fallback: str) -> str:
        return self._raw.get("sensors", {}).get(str(index + 1)) or fallback

    def led_name(self, index: int, fallback: str) -> str:
        return self._raw.get("leds", {}).get(str(index + 1)) or fallback

    def host_sensor_labels(self) -> dict[str, str | None]:
        return dict(self._raw.get("host_sensors", {}))

    @property
    def backup_dir(self) -> str:
        return self._raw.get("backup_dir", DEFAULT_APP_CONFIG["backup_dir"])

    @property
    def backup_keep(self) -> int:
        return int(self._raw.get("backup_keep", 50))

    def rules(self) -> list[Rule]:
        with self._lock:
            return list(self._rules)

    def set_rules(self, raw_rules: object) -> list[Rule]:
        rules = parse_rules(raw_rules)  # raises ScheduleError before anything is written
        with self._lock:
            self._raw["schedule"] = rules_to_json(rules)
            _atomic_write(self.path, self._raw)
            self._rules = rules
        return rules
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_config -v` then `python3 -m unittest discover -s tests -t .`
Expected: all tests OK (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/auth.py aquacontrol/config.py tests/test_config.py
git commit -m "feat: password/token hashing and daemon/app configuration"
```

### Task 10: HTTP API (web.py) and page skeleton

**Files:**
- Create: `aquacontrol/web.py`, `static/index.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9.
- Produces:
  - `App(device, monitor, scheduler, backups, config, externals, password_hash, push_tokens, static_dir)`
  - `make_handler(app)`
  - `make_server(app, host, port, certfile=None, keyfile=None) -> ThreadingHTTPServer`. The TLS handshake runs in the per-connection thread.
- Endpoints:
  - `GET /`, `/app.js`, `/style.css`
  - `GET /api/status`, `/api/history?minutes=N` (at most 360), `/api/settings`, `/api/schedule`, `/api/backups`
  - `PUT /api/settings/fan/{1-4}` with the optional keys `mode` (fixed|target|curve), `fixed_percent`, `target_c`, `sensor`, `curve` (16 pairs), `min_percent`, `max_percent`
  - `PUT /api/settings/strip` with `{enabled, brightness}`
  - `PUT /api/schedule` with `{rules}`
  - `POST /api/schedule/override` with `{on}`
  - `POST /api/backups/<name>.bin/restore`
  - `POST /api/external` (Bearer token)
- Status codes:
  - 400: validation, schedule, external, backup and bad-request errors
  - 401: missing or wrong auth
  - 404: unknown route
  - 502: `DeviceError`
- Basic-auth successes are cached for 5 min, keyed by the SHA-256 of the header, because PBKDF2 at 600k iterations would cost about 0.3 s per request otherwise.
- Every PUT/POST requires `Content-Type: application/json` (CSRF guard) and a body of at most 64 KiB.

- [ ] **Step 1: Write the failing test**

`tests/test_web.py`:

```python
import base64
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from aquacontrol.auth import hash_password, hash_token
from aquacontrol.backups import BackupStore
from aquacontrol.config import AppConfig
from aquacontrol.device import Device
from aquacontrol.fake import FakeTransport
from aquacontrol.monitor import Monitor
from aquacontrol.schedule import Scheduler
from aquacontrol.sensors import ExternalStore
from aquacontrol.validate import make_check
from aquacontrol.web import App, make_server
from tests.fixtures import load

STATIC = Path(__file__).resolve().parent.parent / "static"


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeTransport(load("settings_live.bin"), load("names.bin"))
        self.backups = BackupStore(Path(self.tmp.name, "backups"))
        self.config = AppConfig(Path(self.tmp.name, "config.json"))
        self.device = Device(self.fake, self.backups, make_check(self.config.min_percent()))
        self.monitor = Monitor(open_reader=lambda: None)
        self.monitor.ingest(load("status.bin"))
        self.now = datetime(2026, 10, 5, 12, 0)
        self.scheduler = Scheduler(self.device, self.config.rules, clock=lambda: self.now)
        self.app = App(self.device, self.monitor, self.scheduler, self.backups, self.config, ExternalStore(),
                       hash_password("pw", iterations=1000), {"llm-vm": hash_token("tok")}, STATIC)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.auth = "Basic " + base64.b64encode(b"admin:pw").decode()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, auth=True, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        h = {"Authorization": self.auth} if auth else {}
        if body is not None or raw is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(payload) if ctype.startswith("application/json") else payload)

    def test_requires_auth(self):
        status, _ = self.req("GET", "/api/status", auth=False)
        self.assertEqual(status, 401)
        bad = "Basic " + base64.b64encode(b"admin:nope").decode()
        status, _ = self.req("GET", "/api/status", auth=False, headers={"Authorization": bad})
        self.assertEqual(status, 401)

    def test_status_and_settings(self):
        status, body = self.req("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"]["temps"][0], 31.72)
        self.assertEqual(body["fan_names"][3], "Gehäuselüfter")
        status, body = self.req("GET", "/api/settings")
        self.assertEqual(body["fans"][0]["mode"], "curve")
        self.assertEqual(body["fans"][1]["target_c"], 34.0)
        self.assertEqual(body["fans"][0]["floor_percent"], 25.0)
        self.assertEqual(body["strip"], {"enabled": True, "brightness": 218})
        self.assertEqual(body["sensors"][0], "Wasser Temp")

    def test_update_fan_target(self):
        status, body = self.req("PUT", "/api/settings/fan/4", {"target_c": 38.0})
        self.assertEqual((status, body["changed"]), (200, True))
        self.assertTrue(body["backup"].endswith(".bin"))
        self.assertEqual(self.device.read_settings().controllers[3].target_c, 38.0)

    def test_update_fan_curve(self):
        curve = [[20 + i, 10 + 5 * i] for i in range(16)]
        status, _ = self.req("PUT", "/api/settings/fan/3", {"mode": "curve", "curve": curve})
        self.assertEqual(status, 200)
        c = self.device.read_settings().controllers[2]
        self.assertEqual(c.curve[15], (35.0, 85.0))

    def test_validation_and_malformed_bodies_are_400(self):
        cases = [
            ("/api/settings/fan/1", {"min_percent": 5}),             # pump floor
            ("/api/settings/fan/2", {"target_c": "warm"}),
            ("/api/settings/fan/2", {"mode": "pid"}),
            ("/api/settings/fan/2", {"curve": [[1, 2]] * 15}),
            ("/api/settings/fan/2", {"curve": [["a", 2]] * 16}),
            ("/api/settings/fan/9", {"target_c": 30}),
            ("/api/settings/strip", {"brightness": "hell"}),
            ("/api/settings/strip", [1, 2]),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                status, resp = self.req("PUT", path, body)
                self.assertEqual(status, 400, resp)
                self.assertIn("error", resp)
        status, _ = self.req("PUT", "/api/settings/fan/2", raw=b"{not json")
        self.assertEqual(status, 400)
        status, _ = self.req("PUT", "/api/settings/fan/2", raw=b'{"target_c": NaN}')
        self.assertEqual(status, 400)
        status, _ = self.req("PUT", "/api/settings/fan/2", body={"target_c": 30},
                             headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        self.assertEqual(self.fake.writes, [])

    def test_device_absent_is_502(self):
        self.fake.present = False
        status, body = self.req("GET", "/api/settings")
        self.assertEqual(status, 502)
        status, _ = self.req("PUT", "/api/settings/fan/2", {"target_c": 30})
        self.assertEqual(status, 502)

    def test_strip_and_schedule(self):
        status, body = self.req("PUT", "/api/settings/strip", {"brightness": 100})
        self.assertEqual(status, 200)
        rules = [{"time": "01:00", "target": "strip", "on": False},
                 {"time": "09:00", "target": "strip", "on": True, "brightness": 150}]
        status, body = self.req("PUT", "/api/schedule", {"rules": rules})
        self.assertEqual(status, 200)
        self.assertEqual(body["desired"], {"on": True, "brightness": 150})
        self.assertEqual(self.device.read_settings().strip_brightness, 150)  # applied immediately
        status, body = self.req("POST", "/api/schedule/override", {"on": False})
        self.assertFalse(self.device.read_settings().strip_enabled)
        self.assertTrue(body["override_active"])
        status, _ = self.req("PUT", "/api/schedule", {"rules": [{"time": "7", "on": True}]})
        self.assertEqual(status, 400)

    def test_backups_and_restore(self):
        original = load("settings_live.bin")
        self.req("PUT", "/api/settings/fan/4", {"target_c": 38.0})
        status, items = self.req("GET", "/api/backups")
        self.assertEqual(len(items), 1)
        status, body = self.req("POST", f"/api/backups/{items[0]['name']}/restore", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.settings, original)
        status, _ = self.req("POST", "/api/backups/missing.bin/restore", {})
        self.assertEqual(status, 400)
        status, _ = self.req("POST", "/api/backups/..%2Fx.bin/restore", {})
        self.assertEqual(status, 404)

    def test_external_push(self):
        body = {"source": "llm-vm", "sensors": [{"id": "gpu0", "label": "GPU 0", "value": 45, "unit": "°C"}]}
        status, _ = self.req("POST", "/api/external", body, auth=False,
                             headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, resp = self.req("POST", "/api/external", body, auth=False,
                                headers={"Authorization": "Bearer tok"})
        self.assertEqual((status, resp["accepted"]), (200, 1))
        status, resp = self.req("POST", "/api/external", {"source": "other", "sensors": body["sensors"]},
                                auth=False, headers={"Authorization": "Bearer tok"})
        self.assertEqual(status, 400)
        self.assertEqual([r.id for r in self.app.externals.current()], ["llm-vm/gpu0"])

    def test_static_and_404(self):
        status, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>", body)
        status, _ = self.req("GET", "/etc/passwd")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_web -v`
Expected: ERROR `ModuleNotFoundError: No module named 'aquacontrol.web'`

- [ ] **Step 3: Write the implementation**

`aquacontrol/web.py`:

```python
"""HTTPS server: Basic-Auth protected JSON API + static UI, plus the token-protected
push endpoint for external sensors."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import ssl
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import protocol as p
from .auth import token_source, verify_password
from .backups import BackupError, BackupStore
from .config import AppConfig
from .device import Device, DeviceError
from .monitor import Monitor
from .schedule import ScheduleError, Scheduler, rules_to_json
from .sensors import ExternalError, ExternalStore
from .validate import ValidationError

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
AUTH_CACHE_S = 300
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8")}
MODES_BY_NAME = {"fixed": p.MODE_FIXED, "target": p.MODE_TARGET, "curve": p.MODE_CURVE}


class BadRequest(ValueError):
    pass


def _number(body: dict, key: str) -> float | None:
    if key not in body:
        return None
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise BadRequest(f"{key} muss eine Zahl sein")
    return float(v)


class App:
    def __init__(self, device: Device, monitor: Monitor, scheduler: Scheduler, backups: BackupStore,
                 config: AppConfig, externals: ExternalStore, password_hash: str,
                 push_tokens: dict[str, str], static_dir: str | Path):
        self.device = device
        self.monitor = monitor
        self.scheduler = scheduler
        self.backups = backups
        self.config = config
        self.externals = externals
        self.password_hash = password_hash
        self.push_tokens = push_tokens
        self.static_dir = Path(static_dir)
        self._names: p.Names | None = None
        self._auth_cache: dict[str, float] = {}
        self._auth_lock = threading.Lock()

    # --- auth ---------------------------------------------------------------
    def check_basic(self, header: str | None) -> bool:
        if not header or not header.startswith("Basic ") or not self.password_hash:
            return False
        key = hashlib.sha256(header.encode()).hexdigest()
        now = time.monotonic()
        with self._auth_lock:
            if self._auth_cache.get(key, 0) > now:
                return True
        try:
            _, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        if not verify_password(password, self.password_hash):
            return False
        with self._auth_lock:
            self._auth_cache = {k: v for k, v in self._auth_cache.items() if v > now}
            self._auth_cache[key] = now + AUTH_CACHE_S
        return True

    def push_source(self, header: str | None) -> str | None:
        if not header or not header.startswith("Bearer "):
            return None
        return token_source(header[7:].strip(), self.push_tokens)

    # --- read models --------------------------------------------------------
    def names(self) -> p.Names | None:
        if self._names is None:
            try:
                self._names = self.device.read_names()
            except DeviceError:
                return None
        return self._names

    def settings_json(self) -> dict:
        s = self.device.read_settings()
        names = self.names()
        fans = []
        for i, (c, f) in enumerate(zip(s.controllers, s.fans)):
            fans.append({
                "index": i + 1,
                "name": self.config.fan_name(i, names.fans[i] if names else f"Kanal {i + 1}"),
                "mode": p.MODE_NAMES.get(c.mode, f"raw-{c.mode}"),
                "fixed_percent": c.fixed_percent,
                "target_c": c.target_c,
                "sensor": c.sensor,
                "curve": [list(pt) for pt in c.curve],
                "min_percent": f.min_percent,
                "max_percent": f.max_percent,
                "fallback_percent": f.fallback_percent,
                "pid": list(c.pid),
                "floor_percent": self.config.min_percent().get(i),
            })
        leds = [{"index": i + 1, "name": self.config.led_name(i, names.leds[i] if names else f"LED {i + 1}"),
                 "led_start": led.led_start, "led_count": led.led_count, "mode": led.mode}
                for i, led in enumerate(s.leds)]
        sensors = [self.config.sensor_name(i, names.temps[i] if names else f"Sensor {i + 1}") for i in range(4)]
        return {"fans": fans, "leds": leds, "sensors": sensors,
                "strip": {"enabled": s.strip_enabled, "brightness": s.strip_brightness}}

    def status_json(self) -> dict:
        snap = self.monitor.snapshot()
        names = self.names()
        snap["fan_names"] = [self.config.fan_name(i, names.fans[i] if names else f"Kanal {i + 1}") for i in range(4)]
        snap["sensor_names"] = [self.config.sensor_name(i, names.temps[i] if names else f"Sensor {i + 1}")
                                for i in range(4)]
        snap["external_sources"] = self.externals.sources()
        return snap

    # --- mutations ----------------------------------------------------------
    def update_fan(self, index: int, body: dict) -> dict:
        if not 1 <= index <= p.NUM_FANS:
            raise BadRequest("Kanal muss 1–4 sein")
        i = index - 1
        ctrl: dict = {}
        fan: dict = {}
        if "mode" in body:
            if body["mode"] not in MODES_BY_NAME:
                raise BadRequest("mode muss fixed, target oder curve sein")
            ctrl["mode"] = MODES_BY_NAME[body["mode"]]
        for key, target in (("fixed_percent", ctrl), ("target_c", ctrl), ("min_percent", fan), ("max_percent", fan)):
            v = _number(body, key)
            if v is not None:
                target[key] = v
        if "sensor" in body:
            if isinstance(body["sensor"], bool) or not isinstance(body["sensor"], int):
                raise BadRequest("sensor muss eine Ganzzahl sein")
            ctrl["sensor"] = body["sensor"]
        if "curve" in body:
            curve = body["curve"]
            if not isinstance(curve, list) or len(curve) != p.NUM_CURVE_POINTS or not all(
                    isinstance(pt, list) and len(pt) == 2 for pt in curve):
                raise BadRequest("curve muss 16 Paare [Temperatur, Prozent] enthalten")
            pts = []
            for t, pct in curve:
                pts.append((_number({"v": t}, "v"), _number({"v": pct}, "v")))
            ctrl["curve"] = tuple(pts)

        def mutate(s: p.Settings) -> p.Settings:
            if ctrl:
                s = p.with_controller(s, i, **ctrl)
            if fan:
                s = p.with_fan(s, i, **fan)
            return s

        result = self.device.apply(mutate, reason=f"kanal-{index}")
        return {"ok": True, "changed": result.changed, "backup": result.backup}

    def update_strip(self, body: dict) -> dict:
        enabled = body.get("enabled")
        brightness = body.get("brightness")
        if enabled is not None and not isinstance(enabled, bool):
            raise BadRequest("enabled muss true oder false sein")
        if brightness is not None and (isinstance(brightness, bool) or not isinstance(brightness, int)):
            raise BadRequest("brightness muss eine Ganzzahl sein")
        result = self.device.apply(lambda s: p.with_strip(s, enabled=enabled, brightness=brightness),
                                   reason="strip")
        return {"ok": True, "changed": result.changed, "backup": result.backup}

    def schedule_json(self) -> dict:
        return {"rules": rules_to_json(self.config.rules()), **self.scheduler.status()}

    def put_schedule(self, body: dict) -> dict:
        self.config.set_rules(body.get("rules"))
        self.scheduler.tick()
        return self.schedule_json()

    def override(self, body: dict) -> dict:
        if not isinstance(body.get("on"), bool):
            raise BadRequest("on muss true oder false sein")
        self.scheduler.set_override(body["on"])
        self.scheduler.tick()
        return self.schedule_json()

    def backups_json(self) -> list[dict]:
        return [asdict(b) for b in self.backups.list()]

    def restore(self, name: str) -> dict:
        result = self.device.restore(self.backups.load(name), reason=f"restore-{name[:20]}")
        return {"ok": True, "changed": result.changed, "backup": result.backup}

    def push(self, source: str, body: object) -> dict:
        return {"ok": True, "accepted": self.externals.put(source, body)}


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "aquacontrol"
        sys_version = ""
        timeout = 30

        def log_message(self, fmt: str, *args) -> None:  # route to logging, never print bodies
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, payload: object, ctype: str = "application/json; charset=utf-8",
                  extra: dict | None = None) -> None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code: int, message: str) -> None:
            self._send(code, {"ok": False, "error": message})

        def _body(self) -> object:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise BadRequest("Content-Type muss application/json sein")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise BadRequest("ungültige Content-Length")
            if not 0 <= length <= MAX_BODY:
                raise BadRequest("Body zu groß")
            try:
                return json.loads(self.rfile.read(length) or b"null")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise BadRequest("Body ist kein gültiges JSON")

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if method == "POST" and path == "/api/external":
                    source = app.push_source(self.headers.get("Authorization"))
                    if source is None:
                        return self._error(401, "ungültiges Token")
                    return self._send(200, app.push(source, self._body()))
                if not app.check_basic(self.headers.get("Authorization")):
                    time.sleep(0.5)
                    return self._send(401, {"ok": False, "error": "Anmeldung erforderlich"},
                                      extra={"WWW-Authenticate": 'Basic realm="aquacontrol", charset="UTF-8"'})
                self._route(method, path)
            except BadRequest as e:
                self._error(400, str(e))
            except (ValidationError, ScheduleError, ExternalError, BackupError) as e:
                self._error(400, str(e))
            except DeviceError as e:
                self._error(502, str(e))
            except Exception:
                log.exception("unhandled error for %s %s", method, path)
                self._error(500, "interner Fehler")

        def _route(self, method: str, path: str) -> None:
            if method == "GET" and path in STATIC_FILES:
                fname, ctype = STATIC_FILES[path]
                return self._send(200, (app.static_dir / fname).read_bytes(), ctype)
            if method == "GET" and path == "/api/status":
                return self._send(200, app.status_json())
            if method == "GET" and path == "/api/history":
                m = re.search(r"minutes=(\d+)", self.path)
                return self._send(200, app.monitor.history(min(int(m.group(1)) if m else 60, 360)))
            if method == "GET" and path == "/api/settings":
                return self._send(200, app.settings_json())
            if method == "GET" and path == "/api/schedule":
                return self._send(200, app.schedule_json())
            if method == "GET" and path == "/api/backups":
                return self._send(200, app.backups_json())
            if method in ("PUT", "POST"):
                body = self._body()
                if not isinstance(body, dict):
                    raise BadRequest("Body muss ein JSON-Objekt sein")
                m = re.fullmatch(r"/api/settings/fan/(\d)", path)
                if method == "PUT" and m:
                    return self._send(200, app.update_fan(int(m.group(1)), body))
                if method == "PUT" and path == "/api/settings/strip":
                    return self._send(200, app.update_strip(body))
                if method == "PUT" and path == "/api/schedule":
                    return self._send(200, app.put_schedule(body))
                if method == "POST" and path == "/api/schedule/override":
                    return self._send(200, app.override(body))
                m = re.fullmatch(r"/api/backups/([A-Za-z0-9_.-]+\.bin)/restore", path)
                if method == "POST" and m:
                    return self._send(200, app.restore(m.group(1)))
            self._error(404, "nicht gefunden")

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    ssl_context: ssl.SSLContext | None = None

    def finish_request(self, request, client_address):
        # TLS handshake runs here, in the per-connection thread, so a slow client
        # cannot block accept() for everyone else.
        if self.ssl_context is not None:
            request.settimeout(10)
            request = self.ssl_context.wrap_socket(request, server_side=True)
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        log.debug("connection error from %s", client_address, exc_info=True)


def make_server(app: App, host: str, port: int, certfile: str | None = None,
                keyfile: str | None = None) -> ThreadingHTTPServer:
    server = _Server((host, port), make_handler(app))
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile, keyfile)
        server.ssl_context = ctx
    return server
```

`static/index.html` (the full page; `app.js` and `style.css` follow in Task 11):

```html
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aquacontrol</title>
<link rel="stylesheet" href="style.css">
<script src="app.js" defer></script>
</head>
<body>
<header>
  <h1>aquacontrol</h1>
  <span id="online" class="pill">…</span>
  <span id="water" class="big">–</span>
</header>
<nav>
  <button data-tab="overview" class="active">Übersicht</button>
  <button data-tab="fans">Lüfter</button>
  <button data-tab="leds">LEDs</button>
  <button data-tab="schedule">Zeitplan</button>
  <button data-tab="backups">Backups</button>
</nav>
<main>
  <section id="tab-overview">
    <div id="tiles" class="grid"></div>
    <h2>Temperaturen</h2>
    <div id="sensors" class="grid"></div>
    <h2>Verlauf <select id="hist-range"><option value="60">1 h</option><option value="180">3 h</option><option value="360">6 h</option></select></h2>
    <div id="series" class="series"></div>
    <svg id="chart" viewBox="0 0 800 260" preserveAspectRatio="none"></svg>
  </section>
  <section id="tab-fans" hidden><div id="fan-editors"></div></section>
  <section id="tab-leds" hidden>
    <div class="card">
      <h3>LED-Strip</h3>
      <label><input type="checkbox" id="strip-on"> eingeschaltet</label>
      <label>Helligkeit <input type="range" id="strip-bright" min="0" max="255"> <output id="strip-bright-val"></output></label>
      <button id="strip-save">Speichern</button> <span class="msg" id="strip-msg"></span>
    </div>
    <h2>LED-Controller</h2>
    <p class="hint">Farben und Schwellwerte je Gerät werden nach dem Reverse Engineering hier bearbeitbar.</p>
    <div id="led-list" class="grid"></div>
  </section>
  <section id="tab-schedule" hidden>
    <div class="card" id="sched-status"></div>
    <table id="rules"><thead><tr><th>Uhrzeit</th><th>Tage</th><th>Aktion</th><th>Helligkeit</th><th></th></tr></thead><tbody></tbody></table>
    <button id="rule-add">Regel hinzufügen</button>
    <button id="rules-save">Zeitplan speichern</button> <span class="msg" id="rules-msg"></span>
  </section>
  <section id="tab-backups" hidden>
    <p class="hint">Vor jeder Änderung wird das Geräteprofil gesichert. „pinned“-Backups werden nie automatisch gelöscht.</p>
    <table id="backup-list"><thead><tr><th>Name</th><th></th></tr></thead><tbody></tbody></table>
    <span class="msg" id="backup-msg"></span>
  </section>
</main>
</body>
</html>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_web -v` then `python3 -m unittest discover -s tests -t .`
Expected: all OK. `test_web` takes about 6 s because failed logins sleep 0.5 s.

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/web.py static/index.html tests/test_web.py
git commit -m "feat: JSON API with basic auth, token push endpoint and TLS server"
```

### Task 11: Entry point and CLI (__main__.py)

**Files:**
- Create: `aquacontrol/__main__.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything from Tasks 1–10.
- Produces the CLI `python3 -m aquacontrol [--daemon-config P] [--app-config P] COMMAND`:
  - `run [--static DIR] [--listen H] [--port N] [--no-tls] [--no-schedule] [--fake FIXTURE_DIR]`
  - `set-password`
  - `add-push-token SOURCE`: prints the token and stores only its hash
  - `backup [--reason R] [--pinned]`
  - `selftest-write`
- Also produces `build(args) -> (device, monitor, scheduler, backups, config, externals)`.
- `run` refuses to start without a password hash, and without TLS credentials (`$CREDENTIALS_DIRECTORY/cert|key` from systemd) unless `--no-tls` is given.

- [ ] **Step 1: Write the failing test**

`tests/test_main.py`:

```python
import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from aquacontrol import __main__ as main_mod
from aquacontrol.auth import hash_token, verify_password
from tests.fixtures import FIXTURES


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.daemon = Path(self.tmp.name, "daemon.json")
        self.app = Path(self.tmp.name, "config.json")
        self.app.write_text(json.dumps({"backup_dir": str(Path(self.tmp.name, "b")), "schedule": []}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_fake(self):
        args = argparse.Namespace(app_config=str(self.app), fake=str(FIXTURES))
        device, monitor, scheduler, backups, config, externals = main_mod.build(args)
        self.assertEqual(device.read_settings().strip_brightness, 218)
        self.assertEqual(device.read_names().fans[0], "Pumpe")

    def test_set_password(self):
        with mock.patch("getpass.getpass", side_effect=["langes-passwort", "langes-passwort"]), \
                redirect_stdout(io.StringIO()):
            rc = main_mod.main(["--daemon-config", str(self.daemon), "set-password"])
        self.assertEqual(rc, 0)
        self.assertTrue(verify_password("langes-passwort", json.loads(self.daemon.read_text())["password_hash"]))

    def test_set_password_too_short(self):
        with mock.patch("getpass.getpass", side_effect=["kurz", "kurz"]), redirect_stderr(io.StringIO()):
            self.assertEqual(main_mod.main(["--daemon-config", str(self.daemon), "set-password"]), 1)

    def test_add_push_token_prints_token_stores_hash(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            main_mod.main(["--daemon-config", str(self.daemon), "add-push-token", "llm-vm"])
        token = out.getvalue().strip()
        self.assertEqual(json.loads(self.daemon.read_text())["push_tokens"]["llm-vm"], hash_token(token))

    def test_run_refuses_without_password(self):
        self.daemon.write_text("{}")
        with self.assertLogs("aquacontrol", level="ERROR"):
            rc = main_mod.main(["--daemon-config", str(self.daemon), "--app-config", str(self.app),
                                "run", "--fake", str(FIXTURES), "--no-tls"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_main -v`
Expected: ERROR `ImportError` / `ModuleNotFoundError` for `aquacontrol.__main__`

- [ ] **Step 3: Write the implementation**

`aquacontrol/__main__.py`:

```python
"""Entry point: `python3 -m aquacontrol <command>`."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .auth import hash_password, hash_token, new_token
from .backups import BackupStore
from .config import AppConfig, ConfigError, load_daemon_config, update_daemon_config
from .device import Device
from .monitor import Monitor
from .schedule import Scheduler
from .sensors import ExternalStore, read_host_sensors
from .transport import HidrawReader, HidrawTransport
from .validate import make_check
from .web import App, make_server

log = logging.getLogger("aquacontrol")
DEFAULT_DAEMON = "/etc/aquacontrol/daemon.json"
DEFAULT_APP = "/var/lib/aquacontrol/config.json"
STATIC = Path(__file__).resolve().parent.parent / "static"


def build(args) -> tuple[Device, Monitor, Scheduler, BackupStore, AppConfig, ExternalStore]:
    config = AppConfig(args.app_config)
    backups = BackupStore(config.backup_dir, config.backup_keep)
    if args.fake:
        from .fake import FakeReader, FakeTransport
        fixtures = Path(args.fake)
        transport = FakeTransport((fixtures / "settings_live.bin").read_bytes(),
                                  (fixtures / "names.bin").read_bytes())
        open_reader = lambda: FakeReader((fixtures / "status.bin").read_bytes())  # noqa: E731
    else:
        transport = HidrawTransport()
        open_reader = HidrawReader
    device = Device(transport, backups, make_check(config.min_percent()))
    externals = ExternalStore()
    monitor = Monitor(open_reader, extra=lambda: read_host_sensors(config.host_sensor_labels()) + externals.current())
    scheduler = Scheduler(device, config.rules)
    return device, monitor, scheduler, backups, config, externals


def cmd_run(args) -> int:
    try:
        daemon = load_daemon_config(args.daemon_config)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    if not daemon.password_hash:
        log.error("kein Passwort gesetzt: python3 -m aquacontrol set-password")
        return 2
    device, monitor, scheduler, backups, config, externals = build(args)
    app = App(device, monitor, scheduler, backups, config, externals, daemon.password_hash,
              daemon.push_tokens, args.static)
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    cert = key = None
    if creds and not args.no_tls:
        cert, key = os.path.join(creds, "cert"), os.path.join(creds, "key")
    elif not args.no_tls:
        log.error("kein Zertifikat (CREDENTIALS_DIRECTORY fehlt); nur mit --no-tls lokal testen")
        return 2
    host = args.listen or daemon.listen
    port = args.port or daemon.port
    server = make_server(app, host, port, cert, key)
    stop = threading.Event()
    threads = [threading.Thread(target=monitor.run, args=(stop,), name="monitor", daemon=True)]
    if not args.no_schedule:
        threads.append(threading.Thread(target=scheduler.run, args=(stop,), name="scheduler", daemon=True))
    for t in threads:
        t.start()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("listening on %s://%s:%d", "https" if cert else "http", host, port)
    server.serve_forever()
    return 0


def cmd_set_password(args) -> int:
    pw = getpass.getpass("Neues Passwort: ")
    if len(pw) < 10:
        print("Mindestens 10 Zeichen.", file=sys.stderr)
        return 1
    if getpass.getpass("Wiederholen: ") != pw:
        print("Stimmt nicht überein.", file=sys.stderr)
        return 1
    update_daemon_config(args.daemon_config, password_hash=hash_password(pw))
    print(f"Passwort in {args.daemon_config} gesetzt. Dienst neu starten: systemctl restart aquacontrol")
    return 0


def cmd_add_push_token(args) -> int:
    token = new_token()
    update_daemon_config(args.daemon_config, push_token=(args.source, hash_token(token)))
    print(token)
    print(f"Token für Quelle '{args.source}' gespeichert (nur der Hash). Dienst neu starten.", file=sys.stderr)
    return 0


def cmd_backup(args) -> int:
    config = AppConfig(args.app_config)
    store = BackupStore(config.backup_dir, config.backup_keep)
    device = Device(HidrawTransport(), store, make_check({}))
    name = store.save(device.read_report(), args.reason, pinned=args.pinned)
    print(name)
    return 0


def cmd_selftest_write(args) -> int:
    config = AppConfig(args.app_config)
    store = BackupStore(config.backup_dir, config.backup_keep)
    device = Device(HidrawTransport(), store, make_check({}))
    print("Backup:", store.save(device.read_report(), "selftest", pinned=True))
    device.rewrite_current()
    print("Unverändertes Profil geschrieben, bestätigt und Byte für Byte verifiziert.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aquacontrol")
    parser.add_argument("--daemon-config", default=DEFAULT_DAEMON)
    parser.add_argument("--app-config", default=DEFAULT_APP)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Daemon starten")
    run.add_argument("--static", default=str(STATIC))
    run.add_argument("--listen")
    run.add_argument("--port", type=int)
    run.add_argument("--no-tls", action="store_true", help="nur für lokale Entwicklung")
    run.add_argument("--no-schedule", action="store_true")
    run.add_argument("--fake", metavar="FIXTURE_DIR", help="simuliertes Gerät aus Fixture-Dateien")
    run.set_defaults(func=cmd_run)
    sub.add_parser("set-password").set_defaults(func=cmd_set_password)
    tok = sub.add_parser("add-push-token")
    tok.add_argument("source")
    tok.set_defaults(func=cmd_add_push_token)
    bak = sub.add_parser("backup")
    bak.add_argument("--reason", default="manual")
    bak.add_argument("--pinned", action="store_true")
    bak.set_defaults(func=cmd_backup)
    sub.add_parser("selftest-write").set_defaults(func=cmd_selftest_write)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all OK

- [ ] **Step 5: Commit**

```bash
git add aquacontrol/__main__.py tests/test_main.py
git commit -m "feat: CLI entry point (run, set-password, add-push-token, backup, selftest-write)"
```

### Task 12: Web UI (app.js, style.css) with a browser check in fake mode

**Files:**
- Create: `static/app.js`, `static/style.css`

**Interfaces:**
- Consumes the API from Task 10. Talks only to `/api/*` via `fetch`, with no inline scripts or styles, so the CSP `default-src 'self'` holds.
- Tabs:
  - Übersicht: tiles, host/external sensors and the SVG history
  - Lüfter: per-channel mode, target, sensor and min/max, plus the curve editor with draggable points and a table
  - LEDs: strip on/off and brightness, and the LED controllers read-only
  - Zeitplan: rules table, status, and "jetzt an/aus"
  - Backups: list, and restore behind a two-step button (no `confirm()`)
- Light and dark mode follow `prefers-color-scheme`.

- [ ] **Step 1: Write `static/style.css`**

```css
:root {
  --bg: #f4f6f8; --card: #ffffff; --text: #17202a; --muted: #5d6d7e; --border: #d5dbe1;
  --accent: #0b7285; --ok: #2b8a3e; --warn: #c92a2a; --grid: #e3e8ed;
  --s1: #1c7ed6; --s2: #e8590c; --s3: #2b8a3e; --s4: #9c36b5; --s5: #c2255c; --s6: #5c940d;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #11161c; --card: #1a222b; --text: #e6edf3; --muted: #8b98a5; --border: #2c3742;
    --accent: #3bc9db; --ok: #69db7c; --warn: #ff8787; --grid: #26313c;
    --s1: #4dabf7; --s2: #ff922b; --s3: #69db7c; --s4: #da77f2; --s5: #f783ac; --s6: #a9e34b;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.45 system-ui, sans-serif; background: var(--bg); color: var(--text); }
header { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--border); }
header h1 { font-size: 18px; margin: 0; }
.big { margin-left: auto; font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.pill { padding: 2px 10px; border-radius: 999px; font-size: 13px; border: 1px solid var(--border); }
.pill.on { color: var(--ok); border-color: var(--ok); }
.pill.off { color: var(--warn); border-color: var(--warn); }
nav { display: flex; gap: 4px; padding: 8px 16px; overflow-x: auto; }
nav button { border: 0; background: none; color: var(--muted); padding: 6px 12px; border-radius: 6px; cursor: pointer; font: inherit; }
nav button.active { background: var(--card); color: var(--text); box-shadow: 0 0 0 1px var(--border); }
main { padding: 8px 16px 40px; max-width: 1100px; }
h2 { font-size: 16px; margin: 24px 0 8px; display: flex; gap: 8px; align-items: center; }
h3 { margin: 0 0 8px; font-size: 15px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.tile .label { color: var(--muted); font-size: 13px; }
.tile .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.stale .value { color: var(--muted); }
svg#chart { width: 100%; height: 260px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; }
svg text { fill: var(--muted); font-size: 11px; }
.series { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 13px; margin-bottom: 6px; }
.series label { display: flex; gap: 4px; align-items: center; }
.swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.fan-editor { display: grid; grid-template-columns: minmax(240px, 1fr) 2fr; gap: 16px; }
@media (max-width: 760px) { .fan-editor { grid-template-columns: 1fr; } }
.fields label { display: flex; justify-content: space-between; gap: 8px; margin: 6px 0; align-items: center; }
input, select, button { font: inherit; color: inherit; }
input[type=number], input[type=time], select { width: 110px; padding: 3px 6px; background: var(--bg); border: 1px solid var(--border); border-radius: 4px; }
button { padding: 5px 12px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg); cursor: pointer; }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
button.danger { border-color: var(--warn); color: var(--warn); }
svg.curve { width: 100%; height: 240px; background: var(--bg); border-radius: 6px; touch-action: none; }
svg.curve circle { fill: var(--accent); cursor: grab; }
svg.curve polyline { fill: none; stroke: var(--accent); stroke-width: 2; }
svg.curve .now { stroke: var(--warn); stroke-dasharray: 4 3; }
svg .gridline { stroke: var(--grid); stroke-width: 1; }
table { border-collapse: collapse; width: 100%; background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); font-size: 14px; }
td.days label { margin-right: 4px; font-size: 12px; }
.msg { font-size: 13px; }
.msg.ok { color: var(--ok); }
.msg.err { color: var(--warn); }
.hint { color: var(--muted); font-size: 13px; }
details summary { cursor: pointer; color: var(--muted); font-size: 13px; margin-top: 8px; }
.points { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; font-size: 12px; }
.points input { width: 100%; }
```

- [ ] **Step 2: Write `static/app.js`**

```javascript
"use strict";
// aquacontrol UI – vanilla JS, no build step. All device writes go through the JSON API.

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
};
const SVG = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}) => {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};
const fmt = (v, digits = 1, unit = "") => (v === null || v === undefined ? "–" : `${Number(v).toFixed(digits)}${unit}`);

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function showMsg(node, text, ok) {
  node.textContent = text;
  node.className = `msg ${ok ? "ok" : "err"}`;
}

function savedText(res) {
  if (!res.changed) return "Keine Änderung.";
  return res.backup ? `Gespeichert (Backup ${res.backup}).` : "Gespeichert.";
}

// ---------------------------------------------------------------- tabs
const loaders = {};
for (const btn of document.querySelectorAll("nav button")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll("nav button")) b.classList.toggle("active", b === btn);
    for (const s of document.querySelectorAll("main section")) s.hidden = s.id !== `tab-${btn.dataset.tab}`;
    if (loaders[btn.dataset.tab]) loaders[btn.dataset.tab]();
  });
}

// ---------------------------------------------------------------- overview
let lastStatus = null;

async function refreshStatus() {
  try {
    lastStatus = await api("GET", "/api/status");
  } catch (e) {
    $("#online").textContent = "keine Verbindung";
    $("#online").className = "pill off";
    return;
  }
  const s = lastStatus;
  $("#online").textContent = s.online ? "QUADRO online" : "QUADRO offline";
  $("#online").className = `pill ${s.online ? "on" : "off"}`;
  const st = s.status;
  $("#water").textContent = st ? fmt(st.temps[0], 1, " °C") : "–";
  const tiles = $("#tiles");
  tiles.replaceChildren();
  if (st) {
    st.temps.forEach((t, i) => {
      if (t !== null) tiles.append(tile(s.sensor_names[i], fmt(t, 2, " °C")));
    });
    tiles.append(tile("Durchfluss", fmt(st.flow_lph, 1, " l/h")));
    st.fans.forEach((f, i) => tiles.append(tile(s.fan_names[i], `${f.rpm} rpm`, `${fmt(f.percent, 1, " %")} · ${fmt(f.power_w, 2, " W")}`)));
  }
  const sensors = $("#sensors");
  sensors.replaceChildren();
  for (const r of s.sensors) sensors.append(tile(r.label, fmt(r.value, 1, ` ${r.unit}`)));
  for (const [src, age] of Object.entries(s.external_sources || {})) {
    if (age > 30) sensors.append(tile(src, "keine Daten", `letzter Push vor ${Math.round(age)} s`, true));
  }
  if (fanEditorsReady) updateOperatingPoints();
}

function tile(label, value, sub = "", stale = false) {
  return el("div", { class: `card tile${stale ? " stale" : ""}` },
    el("div", { class: "label" }, label), el("div", { class: "value" }, value),
    sub ? el("div", { class: "sub" }, sub) : "");
}

// history chart -------------------------------------------------------
const COLORS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];
const hiddenSeries = new Set(JSON.parse(localStorageGet("hiddenSeries") || "[]"));

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

function seriesLabel(key) {
  const s = lastStatus;
  if (!s) return key;
  let m = key.match(/^temp(\d)$/);
  if (m) return s.sensor_names[m[1] - 1];
  m = key.match(/^fan(\d)_(rpm|percent)$/);
  if (m) return `${s.fan_names[m[1] - 1]} ${m[2] === "rpm" ? "rpm" : "%"}`;
  if (key === "flow") return "Durchfluss l/h";
  const r = (s.sensors || []).find((x) => x.id === key);
  return r ? r.label : key;
}

async function refreshHistory() {
  let data;
  try {
    data = await api("GET", `/api/history?minutes=${$("#hist-range").value}`);
  } catch { return; }
  const keys = [...new Set(data.flatMap((d) => Object.keys(d)))]
    .filter((k) => k !== "t" && !k.endsWith("_rpm") && !k.endsWith("_percent") && k !== "flow");
  const series = $("#series");
  series.replaceChildren();
  keys.forEach((k, i) => {
    const color = `var(${COLORS[i % COLORS.length]})`;
    const cb = el("input", { type: "checkbox", checked: !hiddenSeries.has(k) });
    cb.addEventListener("change", () => {
      cb.checked ? hiddenSeries.delete(k) : hiddenSeries.add(k);
      localStorageSet("hiddenSeries", JSON.stringify([...hiddenSeries]));
      refreshHistory();
    });
    const sw = el("span", { class: "swatch" });
    sw.style.background = color;
    series.append(el("label", {}, cb, sw, seriesLabel(k)));
  });
  drawChart($("#chart"), data, keys.filter((k) => !hiddenSeries.has(k)), keys);
}

function drawChart(root, data, visible, allKeys) {
  root.replaceChildren();
  const W = 800, H = 260, L = 40, B = 20, T = 10;
  if (data.length < 2 || visible.length === 0) {
    root.append(Object.assign(svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }), { textContent: "Noch zu wenig Daten" }));
    return;
  }
  const vals = data.flatMap((d) => visible.map((k) => d[k]).filter((v) => v !== undefined));
  let lo = Math.floor(Math.min(...vals) - 1), hi = Math.ceil(Math.max(...vals) + 1);
  const t0 = data[0].t, t1 = data[data.length - 1].t;
  const x = (t) => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - 5);
  const y = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    root.append(svg("line", { x1: L, x2: W, y1: y(v), y2: y(v), class: "gridline" }));
    root.append(Object.assign(svg("text", { x: 4, y: y(v) + 4 }), { textContent: `${v.toFixed(0)}°` }));
  }
  for (const frac of [0, 0.5, 1]) {
    const t = t0 + (t1 - t0) * frac;
    const label = new Date(t * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    root.append(Object.assign(svg("text", { x: x(t), y: H - 4, "text-anchor": frac === 0 ? "start" : frac === 1 ? "end" : "middle" }), { textContent: label }));
  }
  for (const k of visible) {
    const idx = allKeys.indexOf(k);
    const pts = data.filter((d) => d[k] !== undefined).map((d) => `${x(d.t).toFixed(1)},${y(d[k]).toFixed(1)}`);
    const line = svg("polyline", { points: pts.join(" "), fill: "none", "stroke-width": 2 });
    line.style.stroke = `var(${COLORS[idx % COLORS.length]})`;
    root.append(line);
  }
}

$("#hist-range").addEventListener("change", refreshHistory);

// ---------------------------------------------------------------- fans
let fanEditorsReady = false;
const fanState = [];

loaders.fans = async () => {
  const box = $("#fan-editors");
  let settings;
  try {
    settings = await api("GET", "/api/settings");
  } catch (e) {
    box.replaceChildren(el("p", { class: "msg err" }, `Einstellungen nicht lesbar: ${e.message}`));
    return;
  }
  box.replaceChildren();
  fanState.length = 0;
  for (const fan of settings.fans) box.append(fanEditor(fan, settings.sensors));
  fanEditorsReady = true;
  updateOperatingPoints();
};

function fanEditor(fan, sensorNames) {
  const state = { fan, curve: fan.curve.map((p) => [...p]) };
  fanState.push(state);
  const msg = el("span", { class: "msg" });
  const mode = el("select", {},
    ...[["fixed", "Fest"], ["target", "Zieltemperatur"], ["curve", "Kurve"]].map(([v, t]) => el("option", { value: v, selected: fan.mode === v }, t)));
  if (!["fixed", "target", "curve"].includes(fan.mode)) mode.prepend(el("option", { value: fan.mode, selected: true, disabled: true }, `${fan.mode} (nur Anzeige)`));
  const fixed = el("input", { type: "number", min: 0, max: 100, step: 0.5, value: fan.fixed_percent });
  const target = el("input", { type: "number", min: 20, max: 60, step: 0.5, value: fan.target_c });
  const sensor = el("select", {}, ...sensorNames.map((n, i) => el("option", { value: i, selected: fan.sensor === i }, `${i + 1}: ${n}`)));
  const min = el("input", { type: "number", min: fan.floor_percent ?? 0, max: 100, step: 0.5, value: fan.min_percent });
  const max = el("input", { type: "number", min: 0, max: 100, step: 0.5, value: fan.max_percent });
  const chart = svg("svg", { class: "curve", viewBox: "0 0 400 240" });
  state.chart = chart;
  const points = el("div", { class: "points" });
  state.points = points;

  const rowFixed = el("label", {}, "Fest-Wert %", fixed);
  const rowTarget = el("label", {}, "Zieltemperatur °C", target);
  const curveBox = el("div", {}, chart, el("details", {}, el("summary", {}, "Punkte als Tabelle"), points));
  const sync = () => {
    rowFixed.hidden = mode.value !== "fixed";
    rowTarget.hidden = mode.value !== "target";
    curveBox.hidden = mode.value !== "curve";
  };
  mode.addEventListener("change", sync);

  const save = el("button", { class: "primary" }, "Speichern");
  save.addEventListener("click", async () => {
    const body = { mode: mode.value, sensor: Number(sensor.value), min_percent: Number(min.value), max_percent: Number(max.value) };
    if (mode.value === "fixed") body.fixed_percent = Number(fixed.value);
    if (mode.value === "target") body.target_c = Number(target.value);
    if (mode.value === "curve") body.curve = state.curve;
    if (!["fixed", "target", "curve"].includes(mode.value)) delete body.mode;
    save.disabled = true;
    try {
      showMsg(msg, savedText(await api("PUT", `/api/settings/fan/${fan.index}`, body)), true);
    } catch (e) {
      showMsg(msg, e.message, false);
    } finally {
      save.disabled = false;
    }
  });

  const floorHint = fan.floor_percent != null
    ? el("p", { class: "hint" }, `Minimum darf nicht unter ${fan.floor_percent} % liegen. Kurvenpunkte darunter werden vom Gerät auf das Minimum angehoben.`) : "";
  const card = el("div", { class: "card" },
    el("h3", {}, `${fan.index}: ${fan.name}`),
    el("div", { class: "fan-editor" },
      el("div", { class: "fields" },
        el("label", {}, "Modus", mode), rowFixed, rowTarget, el("label", {}, "Sensor", sensor),
        el("label", {}, "Minimum %", min), el("label", {}, "Maximum %", max),
        el("p", { class: "hint" }, `Fallback ${fan.fallback_percent} % · PID ${fan.pid.slice(0, 3).join("/")} (nur Anzeige)`),
        floorHint, save, " ", msg),
      curveBox));
  sync();
  drawCurve(state);
  return card;
}

const CX = (t) => 30 + ((t - 0) / 70) * 360;        // 0..70 °C
const CY = (p) => 10 + (1 - p / 100) * 210;          // 0..100 %
const invX = (x) => ((x - 30) / 360) * 70;
const invY = (y) => (1 - (y - 10) / 210) * 100;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const round1 = (v) => Math.round(v * 10) / 10;

function drawCurve(state) {
  const c = state.chart;
  c.replaceChildren();
  for (let t = 0; t <= 70; t += 10) {
    c.append(svg("line", { x1: CX(t), x2: CX(t), y1: 10, y2: 220, class: "gridline" }));
    c.append(Object.assign(svg("text", { x: CX(t), y: 236, "text-anchor": "middle" }), { textContent: `${t}°` }));
  }
  for (let p = 0; p <= 100; p += 25) {
    c.append(svg("line", { x1: 30, x2: 390, y1: CY(p), y2: CY(p), class: "gridline" }));
    c.append(Object.assign(svg("text", { x: 2, y: CY(p) + 4 }), { textContent: `${p}%` }));
  }
  c.append(svg("polyline", { points: state.curve.map(([t, p]) => `${CX(t)},${CY(p)}`).join(" ") }));
  if (state.now !== undefined) c.append(svg("line", { x1: CX(state.now), x2: CX(state.now), y1: 10, y2: 220, class: "now" }));
  state.curve.forEach(([t, p], i) => {
    const dot = svg("circle", { cx: CX(t), cy: CY(p), r: 6 });
    dot.addEventListener("pointerdown", (ev) => startDrag(ev, state, i));
    c.append(dot);
  });
  renderPointTable(state);
}

function startDrag(ev, state, i) {
  ev.preventDefault();
  const c = state.chart;
  c.setPointerCapture(ev.pointerId);
  const move = (e) => {
    const pt = c.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(c.getScreenCTM().inverse());
    const lo = i > 0 ? state.curve[i - 1][0] + 0.1 : 0;
    const hi = i < 15 ? state.curve[i + 1][0] - 0.1 : 100;
    state.curve[i] = [round1(clamp(invX(p.x), lo, hi)), round1(clamp(invY(p.y), 0, 100))];
    drawCurve(state);
  };
  const up = () => {
    c.removeEventListener("pointermove", move);
    c.removeEventListener("pointerup", up);
  };
  c.addEventListener("pointermove", move);
  c.addEventListener("pointerup", up);
}

function renderPointTable(state) {
  if (state.points.contains(document.activeElement)) return; // don't rebuild while typing
  state.points.replaceChildren();
  state.curve.forEach(([t, p], i) => {
    const ti = el("input", { type: "number", step: 0.1, value: t, "aria-label": `Punkt ${i + 1} °C` });
    const pi = el("input", { type: "number", step: 0.1, value: p, "aria-label": `Punkt ${i + 1} %` });
    const upd = () => { state.curve[i] = [Number(ti.value), Number(pi.value)]; drawCurve(state); };
    ti.addEventListener("change", upd);
    pi.addEventListener("change", upd);
    state.points.append(el("span", {}, `${i + 1}`), ti, pi, el("span", {}, ""));
  });
}

function updateOperatingPoints() {
  if (!lastStatus || !lastStatus.status) return;
  for (const state of fanState) {
    const temp = lastStatus.status.temps[state.fan.sensor];
    if (temp !== null && temp !== undefined && Math.abs((state.now ?? -1) - temp) > 0.05) {
      state.now = temp;
      drawCurve(state);
    }
  }
}

// ---------------------------------------------------------------- LEDs
loaders.leds = async () => {
  let settings;
  try {
    settings = await api("GET", "/api/settings");
  } catch (e) {
    showMsg($("#strip-msg"), e.message, false);
    return;
  }
  $("#strip-on").checked = settings.strip.enabled;
  $("#strip-bright").value = settings.strip.brightness;
  $("#strip-bright-val").textContent = settings.strip.brightness;
  const list = $("#led-list");
  list.replaceChildren();
  for (const led of settings.leds.filter((l) => l.led_count > 1)) {
    list.append(tile(`${led.index}: ${led.name}`, `LED ${led.led_start}–${led.led_start + led.led_count - 1}`, `Modus ${led.mode}`));
  }
};
$("#strip-bright").addEventListener("input", () => { $("#strip-bright-val").textContent = $("#strip-bright").value; });
$("#strip-save").addEventListener("click", async () => {
  try {
    const res = await api("PUT", "/api/settings/strip", { enabled: $("#strip-on").checked, brightness: Number($("#strip-bright").value) });
    showMsg($("#strip-msg"), savedText(res), true);
  } catch (e) {
    showMsg($("#strip-msg"), e.message, false);
  }
});

// ---------------------------------------------------------------- schedule
const DAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
let rules = [];

loaders.schedule = async () => {
  try {
    renderSchedule(await api("GET", "/api/schedule"));
  } catch (e) {
    showMsg($("#rules-msg"), e.message, false);
  }
};

function renderSchedule(data) {
  rules = data.rules.map((r) => ({ days: [0, 1, 2, 3, 4, 5, 6], ...r }));
  const d = data.desired;
  const status = $("#sched-status");
  const now = (on) => {
    const b = el("button", {}, on ? "LEDs jetzt an" : "LEDs jetzt aus");
    b.addEventListener("click", async () => {
      try { renderSchedule(await api("POST", "/api/schedule/override", { on })); } catch (e) { showMsg($("#rules-msg"), e.message, false); }
    });
    return b;
  };
  status.replaceChildren(
    el("div", {}, `Soll-Zustand: ${d ? (d.on ? "an" : "aus") + (d.brightness != null ? `, Helligkeit ${d.brightness}` : "") : "keine Regel"}`
      + (data.override_active ? " (manuell übersteuert bis zur nächsten Regel)" : "")),
    el("div", { class: "hint" }, `Nächster Wechsel: ${data.next_change ? new Date(data.next_change).toLocaleString("de-DE") : "–"}`),
    data.last_error ? el("div", { class: "msg err" }, `Letzter Fehler: ${data.last_error}`) : "",
    now(true), " ", now(false));
  renderRules();
}

function renderRules() {
  const tbody = $("#rules tbody");
  tbody.replaceChildren();
  rules.forEach((r, i) => {
    const time = el("input", { type: "time", value: r.time });
    time.addEventListener("change", () => { r.time = time.value; });
    const days = el("td", { class: "days" }, ...DAY_NAMES.map((n, d) => {
      const cb = el("input", { type: "checkbox", checked: r.days.includes(d) });
      cb.addEventListener("change", () => { r.days = DAY_NAMES.map((_, k) => k).filter((k) => (k === d ? cb.checked : r.days.includes(k))); });
      return el("label", {}, cb, n);
    }));
    const action = el("select", {}, el("option", { value: "on", selected: r.on }, "an"), el("option", { value: "off", selected: !r.on }, "aus"));
    action.addEventListener("change", () => { r.on = action.value === "on"; });
    const bright = el("input", { type: "number", min: 0, max: 255, placeholder: "unverändert", value: r.brightness ?? "" });
    bright.addEventListener("change", () => { r.brightness = bright.value === "" ? undefined : Number(bright.value); });
    const del = el("button", { class: "danger" }, "Löschen");
    del.addEventListener("click", () => { rules.splice(i, 1); renderRules(); });
    tbody.append(el("tr", {}, el("td", {}, time), days, el("td", {}, action), el("td", {}, bright), el("td", {}, del)));
  });
}

$("#rule-add").addEventListener("click", () => {
  rules.push({ time: "09:00", target: "strip", on: true, days: [0, 1, 2, 3, 4, 5, 6] });
  renderRules();
});
$("#rules-save").addEventListener("click", async () => {
  const payload = rules.map((r) => {
    const out = { time: r.time, target: "strip", on: r.on };
    if (r.brightness !== undefined && r.brightness !== null) out.brightness = r.brightness;
    if (r.days.length !== 7) out.days = r.days;
    return out;
  });
  try {
    renderSchedule(await api("PUT", "/api/schedule", { rules: payload }));
    showMsg($("#rules-msg"), "Zeitplan gespeichert.", true);
  } catch (e) {
    showMsg($("#rules-msg"), e.message, false);
  }
});

// ---------------------------------------------------------------- backups
loaders.backups = async () => {
  const tbody = $("#backup-list tbody");
  let items;
  try {
    items = await api("GET", "/api/backups");
  } catch (e) {
    showMsg($("#backup-msg"), e.message, false);
    return;
  }
  tbody.replaceChildren();
  for (const b of items) {
    const btn = el("button", {}, "Wiederherstellen");
    let armed = false;
    btn.addEventListener("click", async () => {
      if (!armed) { armed = true; btn.textContent = "Wirklich wiederherstellen?"; btn.className = "danger"; return; }
      try {
        showMsg($("#backup-msg"), savedText(await api("POST", `/api/backups/${encodeURIComponent(b.name)}/restore`, {})), true);
        loaders.backups();
      } catch (e) {
        showMsg($("#backup-msg"), e.message, false);
      }
    });
    tbody.append(el("tr", {}, el("td", {}, b.name), el("td", {}, btn)));
  }
};

// ---------------------------------------------------------------- start
refreshStatus();
refreshHistory();
setInterval(refreshStatus, 2000);
setInterval(refreshHistory, 30000);
```

- [ ] **Step 3: Syntax check**

Run: `node --check static/app.js && echo ok`
Expected: `ok`. If `node` is missing, skip this step; Step 5 covers it.

- [ ] **Step 4: Start the fake-mode server**

```bash
mkdir -p dev
python3 -c 'import json; from aquacontrol.auth import hash_password; json.dump({"password_hash": hash_password("devpassword1", iterations=1000)}, open("dev/daemon.json","w"))'
python3 -c 'import json; from aquacontrol.config import DEFAULT_APP_CONFIG as d; d=dict(d, backup_dir="dev/backups"); json.dump(d, open("dev/config.json","w"), ensure_ascii=False)'
python3 -m aquacontrol --daemon-config dev/daemon.json --app-config dev/config.json run --fake tests/fixtures --no-tls --listen 127.0.0.1 --port 18443
```

Run the last line in the background. Port 8099 is taken on the dev Mac; use 18443.

- [ ] **Step 5: Browser check**

Open `http://admin:devpassword1@127.0.0.1:18443/` in a browser. Use the Chrome/Scape browser tools if you are an agent.

Expected:
- The header shows "QUADRO online" and "31.7 °C".
- Übersicht shows Wasser Temp 31.72 °C, Durchfluss 109.2 l/h and four fan tiles: Pumpe 3024 rpm, 140mm Radiator 362, 420mm Radiator 363, Gehäuselüfter 503.
- Lüfter shows four cards. Pumpe shows mode "Kurve" and a curve with 16 points. Channels 2–4 show "Zieltemperatur" 34/35/37 °C.
- Dragging a point updates the line. "Speichern" on channel 4 with the target set to 38 shows "Gespeichert (Backup …)".
- Setting the Pumpe minimum to 10 shows the red error "Minimum 10.0 % unter erlaubtem 25.0 %".
- LEDs: the checkbox is on and the brightness is 218. The controllers listed are 1 (LED 0–29), 2 (30–44), 7 and 8 (45–59).
- Zeitplan: two rules (01:00 aus, 09:00 an) and the next switch time.
- Backups: after the save above, one entry.
- The browser console has no errors.

Take a screenshot of Übersicht and of Lüfter for the record, then stop the server.

- [ ] **Step 6: Commit**

```bash
git add static/app.js static/style.css
git commit -m "feat: web UI (overview, fan curve editor, LEDs, schedule, backups)"
```

### Task 13: Deployment files, tools and README

**Files:**
- Create:
  - `deploy/aquacontrol.service`, `deploy/70-aquacontrol.rules`, `deploy/install.sh`
  - `deploy/push-gpu/aquacontrol-push-gpu.sh`, `deploy/push-gpu/aquacontrol-push-gpu.service`, `deploy/push-gpu/aquacontrol-push-gpu.timer`
  - `tools/usbmon_reports.py`, `tools/make_fixture.py`
  - `README.md`
- Test: `tests/test_usbmon_reports.py`

**Interfaces:**
- `tools.usbmon_reports`:
  - `parse_pcap(path, devnum=None) -> list[SetReport(seq, ts, report_type, report_id, data)]`
  - `diff(a, b) -> list[(abs_offset, old, new, where)]`, where `where` is `LEDn+k`, `CRC` or `payload N`
  - CLI `extract PCAP OUTDIR` and `diff A B`
- `install.sh` is idempotent. It creates the user, the udev rule, `/etc/aquacontrol/daemon.json` (0640 root:aquacontrol), `/var/lib/aquacontrol/config.json` and the unit plus a cert drop-in, and restarts the service once a password exists.

- [ ] **Step 1: Write the failing test**

`tests/test_usbmon_reports.py`:

```python
import struct
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import load
from tools.usbmon_reports import diff, parse_pcap


def usbmon_packet(ev: str, xfer: int, setup: bytes | None, data: bytes, dev: int = 3) -> bytes:
    hdr = bytearray(64)
    hdr[8] = ord(ev)
    hdr[9] = xfer
    hdr[10] = 0
    hdr[11] = dev
    hdr[14] = 0 if setup is not None else 1
    if setup is not None:
        hdr[40:48] = setup
    struct.pack_into("<I", hdr, 36, len(data))
    return bytes(hdr) + data


def pcap(packets: list[bytes]) -> bytes:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 220)
    for i, p in enumerate(packets):
        out += struct.pack("<IIII", 1000 + i, 0, len(p), len(p)) + p
    return out


class UsbmonTest(unittest.TestCase):
    def test_extracts_feature_and_output_set_reports(self):
        report = load("settings_live.bin")
        commit = bytes.fromhex("02 00 00 00 02 00 00 00 00 34 c6")
        packets = [
            usbmon_packet("S", 2, struct.pack("<BBHHH", 0x21, 0x09, 0x0303, 1, 961), report),
            usbmon_packet("C", 2, None, b""),
            usbmon_packet("S", 2, struct.pack("<BBHHH", 0x21, 0x09, 0x0202, 1, 11), commit),
            usbmon_packet("S", 2, struct.pack("<BBHHH", 0xA1, 0x01, 0x0303, 1, 1013), b""),  # GET_REPORT
            usbmon_packet("C", 1, None, load("status.bin")),                                    # interrupt in
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "x.pcap")
            path.write_bytes(pcap(packets))
            reports = parse_pcap(path)
        self.assertEqual([(r.report_type, r.report_id) for r in reports], [("feature", 3), ("output", 2)])
        self.assertEqual(reports[0].data, report)
        self.assertEqual(reports[1].data, commit)

    def test_diff_labels_led_offsets(self):
        a = load("settings_live.bin")
        b = bytearray(a)
        b[397 + 70 + 5] ^= 0xFF  # LED controller 2, byte 5
        self.assertEqual(diff(a, bytes(b)), [(472, a[472], b[472], "LED2+5")])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_usbmon_reports -v`
Expected: ERROR `ModuleNotFoundError: No module named 'tools.usbmon_reports'`

- [ ] **Step 3: Write the tools**

`tools/usbmon_reports.py`:

```python
#!/usr/bin/env python3
"""Extract HID SET_REPORT writes to the QUADRO from a usbmon pcap and diff settings reports.

Capture on the Proxmox host while aquasuite (VM) talks to the device:
    modprobe usbmon; tcpdump -i usbmon3 -s 0 -w /root/aq.pcap
Then:
    python3 tools/usbmon_reports.py extract /root/aq.pcap out/      # writes 001_feature03.bin, ...
    python3 tools/usbmon_reports.py diff out/003_feature03.bin out/004_feature03.bin
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path

LINKTYPE_USB_LINUX_MMAPPED = 220
REPORT_TYPES = {1: "input", 2: "output", 3: "feature"}
LED_BASE_ABS = 397      # payload 396 + report id
LED_SIZE = 70


@dataclass(frozen=True)
class SetReport:
    seq: int
    ts: float
    report_type: str
    report_id: int
    data: bytes


def parse_pcap(path: str | Path, devnum: int | None = None) -> list[SetReport]:
    raw = Path(path).read_bytes()
    magic, = struct.unpack_from("<I", raw, 0)
    if magic != 0xA1B2C3D4:
        raise ValueError("expected a little-endian classic pcap file")
    linktype, = struct.unpack_from("<I", raw, 20)
    if linktype != LINKTYPE_USB_LINUX_MMAPPED:
        raise ValueError(f"linktype {linktype} is not usbmon (220)")
    off, out = 24, []
    while off + 16 <= len(raw):
        ts_sec, ts_usec, incl, _orig = struct.unpack_from("<IIII", raw, off)
        pkt = raw[off + 16: off + 16 + incl]
        off += 16 + incl
        if len(pkt) < 64:
            continue
        ev_type, xfer, epnum, dev = chr(pkt[8]), pkt[9], pkt[10], pkt[11]
        flag_setup = pkt[14]
        if ev_type != "S" or xfer != 2 or flag_setup != 0 or (devnum is not None and dev != devnum):
            continue
        bm_request_type, b_request, w_value, _w_index, _w_length = struct.unpack_from("<BBHHH", pkt, 40)
        if bm_request_type != 0x21 or b_request != 0x09:   # class/interface OUT, SET_REPORT
            continue
        rtype = REPORT_TYPES.get(w_value >> 8, f"type{w_value >> 8}")
        out.append(SetReport(len(out) + 1, ts_sec + ts_usec / 1e6, rtype, w_value & 0xFF, bytes(pkt[64:])))
    return out


def extract(pcap: str, outdir: str) -> None:
    dest = Path(outdir)
    dest.mkdir(parents=True, exist_ok=True)
    for r in parse_pcap(pcap):
        name = f"{r.seq:03d}_{r.report_type}{r.report_id:02x}.bin"
        (dest / name).write_bytes(r.data)
        print(f"{name}  {len(r.data):4d} bytes  t={r.ts:.3f}")


def describe(offset_abs: int) -> str:
    if LED_BASE_ABS <= offset_abs < LED_BASE_ABS + 8 * LED_SIZE:
        idx, rel = divmod(offset_abs - LED_BASE_ABS, LED_SIZE)
        return f"LED{idx + 1}+{rel}"
    if offset_abs in (959, 960):
        return "CRC"
    return f"payload {offset_abs - 1}"


def diff(a: bytes, b: bytes) -> list[tuple[int, int, int, str]]:
    return [(i, a[i], b[i], describe(i)) for i in range(min(len(a), len(b))) if a[i] != b[i]]


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "extract":
        extract(argv[1], argv[2])
        return 0
    if len(argv) == 3 and argv[0] == "diff":
        for i, x, y, where in diff(Path(argv[1]).read_bytes(), Path(argv[2]).read_bytes()):
            print(f"abs {i:4d}  {x:02x} -> {y:02x}  {where}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

`tools/make_fixture.py`:

```python
#!/usr/bin/env python3
"""Capture one status report from the QUADRO with the device serial zeroed.

Run on the host as root:  python3 tools/make_fixture.py status /tmp/status.bin
The settings (0x03) and names (0x08) reports contain no serial and can be saved
with `python3 -m aquacontrol backup` / copied as-is.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aquacontrol.protocol import STATUS_REPORT_ID, STATUS_REPORT_LEN, scrub_status_serial  # noqa: E402
from aquacontrol.transport import HidrawReader  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "status":
        print(__doc__)
        return 1
    reader = HidrawReader()
    try:
        for _ in range(10):
            data = reader.read(2.0)
            if data and data[0] == STATUS_REPORT_ID and len(data) == STATUS_REPORT_LEN:
                Path(argv[1]).write_bytes(scrub_status_serial(data))
                print(f"saved {argv[1]} (serial zeroed)")
                return 0
    finally:
        reader.close()
    print("no status report received", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Write the deployment files**

`deploy/aquacontrol.service`:

```ini
[Unit]
Description=aquacontrol - web control for the Aquacomputer QUADRO
After=network-online.target pve-cluster.service
Wants=network-online.target

[Service]
Type=simple
User=aquacontrol
Group=aquacontrol
WorkingDirectory=/opt/aquacontrol
ExecStart=/usr/bin/python3 -m aquacontrol run
Environment=PYTHONDONTWRITEBYTECODE=1
# cert/key are provided by the drop-in written by install.sh (LoadCredential=cert:..., key:...)
StateDirectory=aquacontrol
StateDirectoryMode=0750
Restart=on-failure
RestartSec=5

ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
DevicePolicy=closed
DeviceAllow=char-hidraw rw
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
LockPersonality=yes
RestrictRealtime=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
UMask=0027

[Install]
WantedBy=multi-user.target
```

`deploy/70-aquacontrol.rules`:

```text
# Give the aquacontrol service access to the Aquacomputer QUADRO hidraw node.
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0c70", ATTRS{idProduct}=="f00d", GROUP="aquacontrol", MODE="0660"
```

`deploy/install.sh` (then `chmod +x deploy/install.sh`):

```bash
#!/bin/bash
# Install or update aquacontrol on the Proxmox host. Run as root from the repo checkout.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"

id aquacontrol >/dev/null 2>&1 || \
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aquacontrol

install -d -m 0755 /opt/aquacontrol
rsync -a --delete "$SRC/aquacontrol" "$SRC/static" /opt/aquacontrol/

install -m 0644 "$SRC/deploy/70-aquacontrol.rules" /etc/udev/rules.d/70-aquacontrol.rules
udevadm control --reload
udevadm trigger --action=change --subsystem-match=hidraw

install -d -m 0750 -o root -g aquacontrol /etc/aquacontrol
if [ ! -f /etc/aquacontrol/daemon.json ]; then
  printf '{\n  "listen": "0.0.0.0",\n  "port": 8443,\n  "password_hash": "",\n  "push_tokens": {}\n}\n' \
    > /etc/aquacontrol/daemon.json
  chown root:aquacontrol /etc/aquacontrol/daemon.json
  chmod 0640 /etc/aquacontrol/daemon.json
fi

install -d -m 0750 -o aquacontrol -g aquacontrol /var/lib/aquacontrol /var/lib/aquacontrol/backups
if [ ! -f /var/lib/aquacontrol/config.json ]; then
  (cd /opt/aquacontrol && python3 -c 'import json; from aquacontrol.config import DEFAULT_APP_CONFIG; print(json.dumps(DEFAULT_APP_CONFIG, indent=2, ensure_ascii=False))') \
    > /var/lib/aquacontrol/config.json
  chown aquacontrol:aquacontrol /var/lib/aquacontrol/config.json
fi

# Prefer a custom/ACME certificate for the web UI if one is installed.
CERT=/etc/pve/local/pve-ssl.pem; KEY=/etc/pve/local/pve-ssl.key
if [ -f /etc/pve/local/pveproxy-ssl.pem ]; then
  CERT=/etc/pve/local/pveproxy-ssl.pem; KEY=/etc/pve/local/pveproxy-ssl.key
fi
install -m 0644 "$SRC/deploy/aquacontrol.service" /etc/systemd/system/aquacontrol.service
install -d /etc/systemd/system/aquacontrol.service.d
printf '[Service]\nLoadCredential=cert:%s\nLoadCredential=key:%s\n' "$CERT" "$KEY" \
  > /etc/systemd/system/aquacontrol.service.d/cert.conf

systemctl daemon-reload
systemctl enable aquacontrol >/dev/null
if grep -q '"password_hash": ""' /etc/aquacontrol/daemon.json; then
  echo "Noch kein Passwort: cd /opt/aquacontrol && python3 -m aquacontrol set-password && systemctl restart aquacontrol"
else
  systemctl restart aquacontrol
  systemctl --no-pager --lines=5 status aquacontrol || true
fi
```

`deploy/push-gpu/aquacontrol-push-gpu.sh` (then `chmod +x`):

```bash
#!/bin/bash
# Runs in VM 103: send GPU temperatures/power/utilisation to aquacontrol (display only).
# AQUACONTROL_URL, AQUACONTROL_TOKEN and AQUACONTROL_CA come from the unit's EnvironmentFile.
set -euo pipefail
sensors=$(nvidia-smi --query-gpu=index,temperature.gpu,power.draw,utilization.gpu \
                     --format=csv,noheader,nounits | python3 -c '
import json, sys
out = []
for line in sys.stdin:
    idx, temp, power, util = [v.strip() for v in line.split(",")]
    out.append({"id": f"gpu{idx}", "label": f"GPU {idx}", "value": float(temp), "unit": "°C"})
    out.append({"id": f"gpu{idx}_power", "label": f"GPU {idx} Leistung", "value": float(power), "unit": "W"})
    out.append({"id": f"gpu{idx}_util", "label": f"GPU {idx} Last", "value": float(util), "unit": "%"})
print(json.dumps({"source": "llm-vm", "sensors": out}))
')
curl -fsS --max-time 4 --cacert "$AQUACONTROL_CA" \
     -H "Authorization: Bearer $AQUACONTROL_TOKEN" -H "Content-Type: application/json" \
     -d "$sensors" "$AQUACONTROL_URL/api/external" >/dev/null
```

`deploy/push-gpu/aquacontrol-push-gpu.service`:

```ini
[Unit]
Description=Push GPU sensors to aquacontrol

[Service]
Type=oneshot
EnvironmentFile=/etc/aquacontrol-push.env
ExecStart=/usr/local/bin/aquacontrol-push-gpu.sh
DynamicUser=yes
SupplementaryGroups=video
```

`deploy/push-gpu/aquacontrol-push-gpu.timer`:

```ini
[Unit]
Description=Push GPU sensors to aquacontrol every 5 s

[Timer]
OnBootSec=30
OnUnitActiveSec=5
AccuracySec=1

[Install]
WantedBy=timers.target
```

`README.md`:

````markdown
# aquacontrol

Web control for an **Aquacomputer QUADRO** fan controller on a Linux/Proxmox host, replacing the
Windows-only aquasuite for day-to-day use.

The QUADRO runs its fan curves and LED effects on its own from settings stored in the device.
aquacontrol reads live values, edits those stored settings (fan mode, target temperature, curve,
min/max, LED strip on/off and brightness), switches the LEDs on a time schedule, and shows other
temperatures (host hwmon sensors, values pushed from other machines) for reference.

- Python 3.11+ standard library only, no dependencies
- Talks to the device via Linux `hidraw` (feature report 0x03 = settings, input report 0x01 = live data)
- Every write: fresh read → CRC check → backup → write → commit → read-back verify → rollback on mismatch

> Protocol knowledge builds on [TimSC/quadroctl](https://github.com/TimSC/quadroctl) and the
> [aquacomputer_d5next hwmon driver](https://github.com/aleksamagicka/aquacomputer_d5next-hwmon).
> Tested with QUADRO firmware 1033 only. Writing settings to hardware is at your own risk.

## Install (Proxmox host, as root)

```bash
rsync -a --exclude .git ./ root@pve:/root/aquacontrol-src/
ssh root@pve /root/aquacontrol-src/deploy/install.sh
ssh root@pve 'cd /opt/aquacontrol && python3 -m aquacontrol set-password && systemctl restart aquacontrol'
```

Web UI: `https://<host>:8443/` (user name is ignored, password as set).

Before the first change, store a pinned backup of the device settings:

```bash
runuser -u aquacontrol -- sh -c 'cd /opt/aquacontrol && python3 -m aquacontrol backup --pinned --reason initial'
```

## Pushing extra sensors (e.g. GPUs from a VM)

```bash
cd /opt/aquacontrol && python3 -m aquacontrol add-push-token llm-vm   # prints the token once
systemctl restart aquacontrol
```

On the sending machine install `deploy/push-gpu/` (script, service, timer) and create
`/etc/aquacontrol-push.env` (mode 0600):

```
AQUACONTROL_URL=https://<pve-host>:8443
AQUACONTROL_TOKEN=<token>
AQUACONTROL_CA=/etc/aquacontrol-ca.pem
```

## Development

```bash
python3 -m unittest discover -s tests -t .
# UI against a simulated device:
python3 -m aquacontrol --daemon-config dev/daemon.json --app-config dev/config.json \
    run --fake tests/fixtures --no-tls --listen 127.0.0.1 --port 18443
```

`dev/daemon.json` needs a `password_hash` (create one with
`python3 -c 'from aquacontrol.auth import hash_password; print(hash_password("devpassword1"))'`).

Test fixtures are real device reports. Status reports must have the device serial zeroed
(`tools/make_fixture.py` does that); the settings and names reports contain no serial.
````

- [ ] **Step 5: Verify**

Run:

```bash
python3 -m unittest discover -s tests -t .
bash -n deploy/install.sh
bash -n deploy/push-gpu/aquacontrol-push-gpu.sh
git grep -nF -f ~/.aquacontrol-never-commit || echo clean
```

Expected: all tests OK, no output from either `bash -n`, and `clean`.

- [ ] **Step 6: Commit**

```bash
git add deploy tools/usbmon_reports.py tools/make_fixture.py tests/test_usbmon_reports.py README.md
git commit -m "feat: systemd/udev deployment, GPU push timer, usbmon RE tools, README"
```

### Task 14: Hardware step 1 – deploy, read-only check, no-op write (USER GATE)

**Precondition:** The user said "yes" to this step in the chat. VM 100 is stopped. `lsusb -t` on the host shows the QUADRO with `Driver=usbhid` (not `usbfs`).

- [ ] **Step 1: Deploy**

```bash
rsync -a --delete --exclude .git --exclude dev ./ $PVE:/root/aquacontrol-src/
ssh $PVE /root/aquacontrol-src/deploy/install.sh
```

Expected: the script prints the "Noch kein Passwort" hint on the first run.

- [ ] **Step 2: Set the password (the user types it)**

Tell the user to run `! ssh -t $PVE 'cd /opt/aquacontrol && python3 -m aquacontrol set-password && systemctl restart aquacontrol'`.
Then run `ssh $PVE systemctl is-active aquacontrol`.
Expected: `active`

- [ ] **Step 3: Pinned initial backup, plus the 2026-10-04 snapshot**

```bash
ssh $PVE "runuser -u aquacontrol -- sh -c 'cd /opt/aquacontrol && python3 -m aquacontrol backup --pinned --reason initial'"
ssh $PVE 'install -o aquacontrol -g aquacontrol -m 0640 /root/quadro_ctrl_live.bin /var/lib/aquacontrol/backups/pinned_20261004-000000-000000_live-2026-10-04.bin'
```

Expected: a file name is printed. `GET /api/backups` lists both pinned backups.

- [ ] **Step 4: Read-only cross-check against hwmon**

```bash
ssh $PVE 'H=$(grep -l quadro /sys/class/hwmon/hwmon*/name | xargs dirname); cat $H/temp1_input $H/fan1_input $H/fan5_input'
curl -sk -u admin:"$PW" https://$PVE_HOST:8443/api/status | python3 -m json.tool | head -30
```

Expected:
- `temps[0]×1000` matches `temp1_input` within ±0.2 °C.
- `fans[0].rpm` matches `fan1_input` within ±3 %.
- `flow_lph×10` matches `fan5_input` within ±3 %.
- `GET /api/settings` shows the same modes and targets as Task 1's fixture, unless the user has changed the device since.

- [ ] **Step 5: No-op write self-test**

Tell the user first: the fans may twitch for a second.

```bash
ssh $PVE "systemctl stop aquacontrol; runuser -u aquacontrol -- sh -c 'cd /opt/aquacontrol && python3 -m aquacontrol selftest-write'; systemctl start aquacontrol"
```

Expected: `Unverändertes Profil geschrieben, bestätigt und Byte für Byte verifiziert.`

If it fails with `VerifyError` and the differing offsets are always the same, they are fields the device changes itself:
1. Add them to `VOLATILE_OFFSETS` in `aquacontrol/device.py`, plus `959` and `960` for the CRC.
2. Add a test in `tests/test_device.py` that checks a write with only those offsets differing passes.
3. Commit, redeploy, and repeat this step.

If the error is anything else, STOP and report to the user.

- [ ] **Step 6: Record the result**

Append a short "Hardware verification" section to `README.md` with:
- the firmware version
- the date
- the result of steps 4 and 5
- any `VOLATILE_OFFSETS`

Then commit:

```bash
git commit -am "docs: record hardware verification of read path and no-op write"
```

### Task 15: Hardware step 2 – real changes, restore, schedule (USER GATE)

**Precondition:** Task 14 passed. The user said "yes" in the chat and can watch the hardware: LEDs and fan noise or speed.

- [ ] **Step 1: LED brightness**

In the UI, under LEDs, set the brightness to 60 and save. Ask the user: "Sind die LEDs dunkler?"
Expected: yes. The response contains a backup name.

- [ ] **Step 2: LED off and on**

Untick "eingeschaltet" and save, then ask the user. Tick it again, set the brightness back to 218, and save.
Expected: the LEDs go off, then come back at the old brightness.

If "off" has no effect (the flag `0x0002` is wrong), STOP. Report to the user and propose brightness 0 as "off". That changes `with_strip` and the schedule semantics; plan an addendum.

- [ ] **Step 3: Fan change**

Under Lüfter → channel 4 (Gehäuselüfter), set the mode to "Fest" at 60 % and save. Watch `fans[3].rpm` in Übersicht for 15 s.
Expected: the rpm rises noticeably, from ~500 to well over 900.

Then restore the backup that this save created (Backups tab), check that channel 4 shows "Zieltemperatur 37 °C" again, and that the rpm falls back.

If the device accepts the write (verify passes) but nothing changes at the fan, this is the firmware-1033 issue #113. STOP: do Task 17 (aquasuite capture) first, compare the commit sequence aquasuite sends, and plan an addendum for `HidrawTransport.write_output` / `Device._write`.

- [ ] **Step 4: Persistence across a device power cycle**

Ask the user whether a QUADRO power cycle is acceptable now (unplug and replug USB/SATA power). If yes:
1. Set channel 4 to target 38 °C.
2. Power-cycle the QUADRO.
3. Check `GET /api/settings`: it still shows 38 °C.
4. Set it back to 37 °C.

If not, skip this step and note it in the README.

- [ ] **Step 5: Schedule**

Under Zeitplan, add two rules: (now + 2 min) "aus" and (now + 4 min) "an", then save. Watch the LEDs.
Expected: off at the first time and on at the second. `GET /api/schedule` shows `last_error: null`.
Restore the final rules: 01:00 aus, 09:00 an, brightness 218.

- [ ] **Step 6: Record the results**

Add the results to the README's hardware section, then commit:

```bash
git commit -am "docs: record hardware verification of writes, restore and schedule"
```

### Task 16: GPU temperatures from VM 103 (USER GATE for changes in VM 103)

**Precondition:** The user said "yes" to installing a timer in VM 103.

- [ ] **Step 1: Create a token on the host**

```bash
ssh $PVE 'cd /opt/aquacontrol && python3 -m aquacontrol add-push-token llm-vm && systemctl restart aquacontrol'
```

Copy the printed token into the next step. Don't log it anywhere else.

- [ ] **Step 2: Install in VM 103**

```bash
scp deploy/push-gpu/aquacontrol-push-gpu.sh deploy/push-gpu/aquacontrol-push-gpu.service deploy/push-gpu/aquacontrol-push-gpu.timer $LLMVM:/tmp/
ssh $PVE cat /etc/pve/pve-root-ca.pem | ssh $LLMVM 'sudo tee /etc/aquacontrol-ca.pem >/dev/null'
ssh $LLMVM 'sudo install -m 0755 /tmp/aquacontrol-push-gpu.sh /usr/local/bin/ && sudo install -m 0644 /tmp/aquacontrol-push-gpu.service /tmp/aquacontrol-push-gpu.timer /etc/systemd/system/'
ssh $LLMVM "sudo sh -c 'umask 077; printf \"AQUACONTROL_URL=https://%s:8443\nAQUACONTROL_TOKEN=%s\nAQUACONTROL_CA=/etc/aquacontrol-ca.pem\n\" $PVE_HOST <TOKEN> > /etc/aquacontrol-push.env'"
ssh $LLMVM 'sudo systemctl daemon-reload && sudo systemctl enable --now aquacontrol-push-gpu.timer && sleep 8 && systemctl status aquacontrol-push-gpu.service --no-pager | tail -5'
```

Expected: the last run reports `status=0/SUCCESS`.

If curl fails with a certificate name mismatch: the PVE certificate has no SAN for the IP. Fix it this way:
1. Use the node's hostname in `AQUACONTROL_URL`.
2. Add `--resolve <hostname>:8443:<ip>` to the curl line in the script.
3. Commit that change.

- [ ] **Step 3: Verify in the UI**

Expected:
- Übersicht → Temperaturen shows GPU 0 and GPU 1 (°C), plus power and utilisation tiles.
- The history gets two GPU series.
- After `ssh $LLMVM sudo systemctl stop aquacontrol-push-gpu.timer`, a "keine Daten" tile appears within 30 s.
- Restart the timer afterwards.

- [ ] **Step 4: Commit any script fix from Step 2** (skip if nothing changed).

### Task 17: LED reverse engineering with aquasuite (USER GATE, interactive)

**Goal:** Decode, inside `LedControllerConfig` (8 × 70 bytes at payload 396):
- which controller drives which device (radiator fans, case fans, pump)
- the fields for colour, temperature thresholds, source sensor, and per-controller brightness and on/off

**Output:** `docs/led-layout.md`, plus the go/no-go for a follow-up plan *"LED editor + `led:N` schedule targets"*. That code can't be specified before the layout is known, so it gets its own plan after this task.

- [ ] **Step 1: Prepare**

Tell the user that aquacontrol will be offline for the session, then run:

```bash
ssh $PVE 'systemctl stop aquacontrol && qm start 100'
```

VM 100 still has `usb0` and now has `vga: std`. Wait until the guest agent answers: `ssh $PVE qm agent 100 ping`.

- [ ] **Step 2: Start the capture**

```bash
ssh $PVE 'modprobe usbmon; B=$(for d in /sys/bus/usb/devices/*; do [ "$(cat $d/idProduct 2>/dev/null)" = f00d ] && cat $d/busnum; done | head -1); nohup tcpdump -i usbmon$B -s 0 -w /root/led-re.pcap >/dev/null 2>&1 & echo bus $B'
```

- [ ] **Step 3: One change at a time, guided**

Ask the user to open the VM console (Proxmox web UI → VM 100 → Console), open aquasuite → QUADRO → RGBpx/LED, and do exactly one change per step. After each change they click save and say "fertig". Keep a numbered log in `docs/led-layout.md`:
1. Controller 1 off → which device goes dark? Then on again. Repeat for controllers 2, 7 and 8.
2. Controller 1 brightness 100 % → 50 %.
3. Temperature colour: change the "yellow" threshold by +1 °C.
4. Change one colour, e.g. green → blue.
5. Change the source sensor of the colour effect, if offered.

- [ ] **Step 4: Extract and diff after each step**

```bash
scp tools/usbmon_reports.py $PVE:/root/
ssh $PVE 'python3 /root/usbmon_reports.py extract /root/led-re.pcap /root/led-re/'
ssh $PVE 'cd /root/led-re && ls'      # NNN_feature03.bin files in write order, plus output02/feature02 commits
ssh $PVE 'python3 /root/usbmon_reports.py diff /root/led-re/<prev>_feature03.bin /root/led-re/<new>_feature03.bin'
```

Record for each step:
- which `LEDn+k` bytes changed, with their old and new values
- how aquasuite commits: an `output02` or `feature02` report after the `feature03`. This also settles the firmware-1033 question from Task 15 Step 3.

- [ ] **Step 5: Restore and clean up**

1. Ask the user to restore the original LED settings in aquasuite. Diff the last written report against `tests/fixtures/settings_live.bin`; it should show no LED differences.
2. Run `ssh $PVE 'pkill tcpdump; qm shutdown 100'`, wait until VM 100 has stopped, then `ssh $PVE systemctl start aquacontrol`.
3. Don't commit any pcap or extracted report.

- [ ] **Step 6: Write and commit `docs/led-layout.md`**

Include:
- the field table (offset within the 70-byte entry, type, meaning, value examples)
- the controller → device mapping
- the commit sequence observed

Then commit:

```bash
git add docs/led-layout.md
git commit -m "docs: QUADRO LED controller layout from aquasuite captures"
```

Also add the device names to `/var/lib/aquacontrol/config.json` → `leds` on the host, e.g. `{"1": "Radiator 420", …}`, and restart the service.

### Task 18: Retire the Windows VM (USER GATE)

**Precondition:** Tasks 14 and 15 passed. The user confirms that VM 100 is no longer needed day to day.

- [ ] **Step 1: Detach the QUADRO from VM 100 permanently**

```bash
ssh $PVE 'qm set 100 --delete usb0 && qm config 100 | grep -E "^(usb|onboot)" ; qm status 100'
```

Expected: no `usb0` line, `onboot` absent or 0, `status: stopped`. The VM stays in place as a fallback.

- [ ] **Step 2: Document the way back**

Add a "Rollback to aquasuite" section to the README:
1. `systemctl stop aquacontrol`
2. `qm set 100 -usb0 host=0c70:f00d && qm start 100`
3. Afterwards: `qm shutdown 100`, `qm set 100 --delete usb0`, `systemctl start aquacontrol`.

- [ ] **Step 3: Restart test without rebooting the host**

```bash
ssh $PVE 'systemctl restart aquacontrol && sleep 5 && systemctl is-active aquacontrol && journalctl -u aquacontrol -n 5 --no-pager'
```

Expected: `active`. The log shows `listening on https://0.0.0.0:8443` and no tracebacks.

A full host reboot test happens only when the user plans a reboot anyway. Note it as an open item in the README.

- [ ] **Step 4: Commit and push**

```bash
git commit -am "docs: rollback procedure and retirement of the aquasuite VM"
git push
```
