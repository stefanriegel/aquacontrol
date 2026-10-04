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
import urllib.parse
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import protocol as p
from .auth import token_source, verify_password
from .backups import BackupError, BackupStore
from .colors import hex_to_hsv1536, hsv1536_to_hex
from .climate import (FAN_MODES, HORIZONTAL, HVAC_MODES, PRESETS, VERTICAL, ClimateConfigError, ClimateController,
                      _entity as parse_entity, merge_climate, parse_climate_config)
from .config import AppConfig, Secrets
from .device import Device, DeviceError
from .ha import HAError
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
FAIL_DELAY_S = 0.2          # pause before answering a failed login
MAX_VERIFY_CONCURRENCY = 2  # parallel PBKDF2 verifications
VERIFY_WAIT_S = 2.0         # how long a login waits for a verification slot before 503
FAIL_LIMIT = 10             # failed logins per FAIL_WINDOW_S before further attempts get 429
FAIL_WINDOW_S = 60.0
MAX_CONNECTIONS = 32
MODES_BY_NAME = {"fixed": p.MODE_FIXED, "target": p.MODE_TARGET, "curve": p.MODE_CURVE}
LED_MODE_NAMES = {p.LED_MODE_UNUSED: "unbenutzt", p.LED_MODE_STATIC: "statisch", p.LED_MODE_COLOR_SWITCH: "farbschalter"}
LED_EDITABLE_MODES = (p.LED_MODE_STATIC, p.LED_MODE_COLOR_SWITCH)
LED_FLAG_FIELDS = {"fade": p.LED_FLAG_FADE, "blink": p.LED_FLAG_BLINK, "brightness_by_source": p.LED_FLAG_BRIGHTNESS}
LED_BODY_KEYS = {"thresholds", "colors", "source", *LED_FLAG_FIELDS}


class BadRequest(ValueError):
    pass


class NotFound(Exception):
    """Feature not wired up in this App (-> 404)."""


class LoginThrottled(Exception):
    """Too many failed logins recently (-> 429)."""


class LoginBusy(Exception):
    """No password-verification slot became free in time (-> 503)."""


def _number(body: dict, key: str) -> float | None:
    if key not in body:
        return None
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadRequest(f"{key} muss eine Zahl sein")
    try:
        f = float(v)  # OverflowError for huge ints
    except (OverflowError, ValueError, TypeError):
        raise BadRequest(f"{key} muss eine Zahl sein") from None
    if not math.isfinite(f):
        raise BadRequest(f"{key} muss eine Zahl sein")
    return f


def _source_name(source: int, sensors: list[str]) -> str:
    """Display name of an LED data source index (see docs/led-layout.md)."""
    if 0 <= source < len(sensors):
        return sensors[source]
    if source == 4:
        return "Durchfluss"
    if 5 <= source < 5 + p.NUM_SOFT_SENSORS:
        return f"Software-Sensor {source - 4}"
    return "keine" if source == p.LED_SOURCE_NONE else f"Quelle {source}"


def _led_body(body: dict) -> dict:
    """Parse and type-check the body of PUT /api/settings/led/N (ranges are checked by validate.py)."""
    unknown = sorted(set(body) - LED_BODY_KEYS)
    if unknown:
        raise BadRequest(f"unbekanntes Feld: {', '.join(unknown)}")
    out: dict = {}
    for key in LED_FLAG_FIELDS:
        if key in body:
            if not isinstance(body[key], bool):
                raise BadRequest(f"{key} muss true oder false sein")
            out[key] = body[key]
    if "source" in body:
        if isinstance(body["source"], bool) or not isinstance(body["source"], int):
            raise BadRequest("source muss eine Ganzzahl sein")
        out["source"] = body["source"]
    if "thresholds" in body:
        t = body["thresholds"]
        if not isinstance(t, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in t):
            raise BadRequest("thresholds muss eine Liste ganzer Zahlen sein")
        if not 1 <= len(t) <= p.LED_MAX_THRESHOLDS:
            raise BadRequest(f"thresholds braucht 1 bis {p.LED_MAX_THRESHOLDS} Schwellen")
        out["thresholds"] = tuple(t)
    if "colors" in body:
        c = body["colors"]
        if not isinstance(c, list) or not 1 <= len(c) <= p.LED_PALETTE:
            raise BadRequest(f"colors muss 1 bis {p.LED_PALETTE} Farben als \"#rrggbb\" enthalten")
        try:
            out["colors"] = tuple((color, hex_to_hsv1536(color)) for color in c)
        except ValueError as e:
            raise BadRequest(str(e)) from None
    return out


class App:
    def __init__(self, device: Device, monitor: Monitor, scheduler: Scheduler, backups: BackupStore,
                 config: AppConfig, externals: ExternalStore, password_hash: str,
                 push_tokens: dict[str, str], static_dir: str | Path,
                 climate: ClimateController | None = None,
                 ha_client_factory: Callable[[], object | None] | None = None):
        self.device = device
        self.monitor = monitor
        self.scheduler = scheduler
        self.backups = backups
        self.config = config
        self.externals = externals
        self.password_hash = password_hash
        self.push_tokens = push_tokens
        self.static_dir = Path(static_dir)
        self.climate = climate
        self.ha_client_factory = ha_client_factory
        self._names: p.Names | None = None
        self._auth_cache: dict[str, float] = {}
        self._auth_lock = threading.Lock()
        self._verify_slots = threading.BoundedSemaphore(MAX_VERIFY_CONCURRENCY)
        self._failures: deque[float] = deque()  # monotonic times of failed Basic logins
        self._push_failures: deque[float] = deque()  # same for push tokens (separate: must not lock the UI)

    def _throttled(self, now: float, failures: deque[float] | None = None) -> bool:
        failures = self._failures if failures is None else failures
        with self._auth_lock:
            while failures and now - failures[0] > FAIL_WINDOW_S:
                failures.popleft()
            return len(failures) >= FAIL_LIMIT

    def push_throttled(self) -> bool:
        return self._throttled(time.monotonic(), self._push_failures)

    def note_push_failure(self) -> None:
        with self._auth_lock:
            self._push_failures.append(time.monotonic())

    # --- auth ---------------------------------------------------------------
    def check_basic(self, header: str | None) -> bool:
        """True if authenticated, False if not. Raises LoginThrottled / LoginBusy when the
        password would have to be verified but the server is protecting its CPU."""
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
        if self._throttled(now):
            raise LoginThrottled()
        if not self._verify_slots.acquire(timeout=VERIFY_WAIT_S):
            raise LoginBusy()
        try:
            if self._throttled(time.monotonic()):  # failures may have piled up while waiting
                raise LoginThrottled()
            ok = verify_password(password, self.password_hash)
        finally:
            self._verify_slots.release()
        if not ok:
            with self._auth_lock:
                self._failures.append(time.monotonic())
            return False
        now = time.monotonic()
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
        sensors = [self.config.sensor_name(i, names.temps[i] if names else f"Sensor {i + 1}") for i in range(4)]
        leds = [{"index": i + 1, "name": self.config.led_name(i, names.leds[i] if names else f"LED {i + 1}"),
                 "led_start": led.led_start, "led_count": led.led_count, "mode": led.mode,
                 "mode_name": LED_MODE_NAMES.get(led.mode, f"raw-{led.mode}"),
                 "editable": led.mode in LED_EDITABLE_MODES,
                 "source": led.source, "source_name": _source_name(led.source, sensors),
                 "range": [led.binding1[0], led.binding1[1]],
                 "thresholds": list(led.thresholds),
                 "colors": [hsv1536_to_hex(*c) for c in led.colors],
                 "flags": {name: bool(led.flags & bit) for name, bit in LED_FLAG_FIELDS.items()}}
                for i, led in enumerate(s.leds)]
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
            if not isinstance(body["mode"], str) or body["mode"] not in MODES_BY_NAME:
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
            for n, (t, pct) in enumerate(curve, 1):
                pair = []
                for value, what in ((t, "Temperatur"), (pct, "Prozent")):
                    try:
                        pair.append(_number({"v": value}, "v"))
                    except BadRequest:
                        raise BadRequest(f"Kurvenpunkt {n}: {what} muss eine Zahl sein") from None
                pts.append(tuple(pair))
            ctrl["curve"] = tuple(pts)

        def mutate(s: p.Settings) -> p.Settings:
            if ctrl:
                s = p.with_controller(s, i, **ctrl)
            if fan:
                s = p.with_fan(s, i, **fan)
            return s

        result = self.device.apply(mutate, reason=f"kanal-{index}")
        return {"ok": True, "changed": result.changed, "backup": result.backup}

    def update_led(self, index: int, body: dict) -> dict:
        if not 1 <= index <= p.NUM_LEDS:
            raise BadRequest("LED-Controller muss 1–8 sein")
        i = index - 1
        req = _led_body(body)

        def mutate(s: p.Settings) -> p.Settings:
            led = s.leds[i]
            if led.mode not in LED_EDITABLE_MODES:
                if req:
                    raise BadRequest(f"LED-Controller {index} ist nicht änderbar")
                return s
            if led.mode == p.LED_MODE_STATIC and ("thresholds" in req or "source" in req):
                raise BadRequest("eine statische Farbe hat weder Schwellen noch Datenquelle")
            if "thresholds" in req:
                s = p.with_led(s, i, thresholds=req["thresholds"])
            led = s.leds[i]
            if "colors" in req:
                expected = len(led.colors)
                if len(req["colors"]) != expected:
                    raise BadRequest(f"colors braucht genau {expected} Farben (eine mehr als Schwellen)")
                # A colour that is the hex form of what the device holds stays as it is: converting it
                # back may not give the stored hue again (low saturation/value, rounding), and an
                # untouched colour must never be rewritten. (Aquasuite's green 511 is "#01ff00", which
                # does round-trip; only "#00ff00" maps to 512.)
                keep = tuple(old if hsv1536_to_hex(*old) == color.lower() else new
                             for (color, new), old in zip(req["colors"], led.palette))
                s = p.with_led(s, i, colors=keep)
            flags = led.flags
            for name, bit in LED_FLAG_FIELDS.items():
                if name in req:
                    flags = flags | bit if req[name] else flags & ~bit
            changes = {"flags": flags}
            if "source" in req:
                changes["source"] = req["source"]
            return p.with_led(s, i, **changes)

        result = self.device.apply(mutate, reason=f"led-{index}")
        return {"ok": True, "changed": result.changed, "backup": result.backup}

    def update_strip(self, body: dict) -> dict:
        enabled = body.get("enabled")
        brightness = body.get("brightness")
        if enabled is not None and not isinstance(enabled, bool):
            raise BadRequest("enabled muss true oder false sein")
        if brightness is not None and (isinstance(brightness, bool) or not isinstance(brightness, int)):
            raise BadRequest("brightness muss eine Ganzzahl sein")
        if brightness is not None and not 0 <= brightness <= 255:
            raise BadRequest("brightness muss zwischen 0 und 255 liegen")  # never leave a bad override behind
        # Every manual change goes through the scheduler override, so the next tick does not revert it.
        # Schedule writes create no backup, by design.
        on = enabled if enabled is not None else self.device.read_settings().strip_enabled
        self.scheduler.set_override(on, brightness)
        result = self.scheduler.tick()
        return {"ok": True, "changed": bool(result and result.changed), "backup": None}

    def schedule_json(self) -> dict:
        return {"rules": rules_to_json(self.config.rules()), **self.scheduler.status()}

    def put_schedule(self, body: dict) -> dict:
        self.config.set_rules(body.get("rules"))
        self.scheduler.clear_override()  # saved rules take effect now, a manual change must not shadow them
        self.scheduler.tick()
        return self.schedule_json()

    def override(self, body: dict) -> dict:
        if not isinstance(body.get("on"), bool):
            raise BadRequest("on muss true oder false sein")
        self.scheduler.set_override(body["on"])
        self.scheduler.tick()
        return self.schedule_json()

    def climate_json(self) -> dict:
        if self.climate is None:
            raise NotFound()
        return {"config": self.config.climate_raw(), "token_set": bool(self.config.secrets.get_ha_token()),
                "status": self.climate.status()}

    def put_climate(self, body: dict) -> dict:
        if self.climate is None:
            raise NotFound()
        patch = dict(body)
        token = Secrets.clean_token(patch.pop("token")) if "token" in patch else None  # validate before any write
        notice = None
        if patch:
            current = self.config.climate_config()
            # normalised new address, validated before anything is written (ClimateConfigError -> 400)
            new_url = parse_climate_config(merge_climate(current.to_json(), patch)).ha_url
            if new_url != current.ha_url and self.config.secrets.get_ha_token():
                # The stored token must never be sent to another address than the one it was entered for, not even
                # for a moment: delete it BEFORE the new address is persisted.
                self.config.secrets.set_ha_token("")
                if token is None:
                    notice = "Adresse geändert: das gespeicherte Token wurde gelöscht, bitte neu eingeben"
            self.config.patch_climate(patch)
        if token is not None:
            self.config.secrets.set_ha_token(token)  # "" removes it
        out = self.climate_json()
        if notice:
            out["notice"] = notice
        return out

    def climate_test(self) -> dict:
        """Read the AC and both louvre selects from Home Assistant. Changes nothing."""
        if self.climate is None:
            raise NotFound()
        client = self.ha_client_factory() if self.ha_client_factory else None
        if client is None:
            raise BadRequest("Home Assistant ist nicht eingerichtet: URL und Token speichern, dann testen")
        cfg = self.config.climate_config()

        def read(entity_id: str) -> dict:
            try:
                return client.get_state(entity_id)
            except HAError as e:
                raise HAError(f"{entity_id}: {e}") from None

        ac = read(cfg.entity_id)
        attrs = ac.get("attributes") if isinstance(ac.get("attributes"), dict) else {}
        return {"ok": True, "state": ac.get("state"), "temperature": attrs.get("temperature"),
                "preset": attrs.get("preset_mode"), "fan_mode": attrs.get("fan_mode"),
                "horizontal": read(cfg.horizontal_select).get("state"),
                "vertical": read(cfg.vertical_select).get("state")}

    def climate_options(self, query: dict[str, list[str]] | None = None) -> dict:
        """What the Klima tab may offer in its drop-downs: the lists Home Assistant reports for the AC and the two
        louvre selects, plus all climate and (swing) select entities. Falls back to built-in lists, never fails
        because of Home Assistant: `source` says which one it is and `error` why."""
        if self.climate is None:
            raise NotFound()
        query = query or {}
        cfg = self.config.climate_config()

        def wanted(key: str, domain: str, default: str) -> str:
            return parse_entity(query[key][0], domain, key) if key in query else default

        entity = wanted("entity_id", "climate", cfg.entity_id)
        h_select = wanted("horizontal_select", "select", cfg.horizontal_select)
        v_select = wanted("vertical_select", "select", cfg.vertical_select)
        fallback = {"hvac_modes": list(HVAC_MODES), "preset_modes": list(PRESETS), "fan_modes": list(FAN_MODES),
                    "horizontal": list(HORIZONTAL), "vertical": list(VERTICAL)}
        out: dict = {"source": "fallback", "error": None, **{k: list(v) for k, v in fallback.items()},
                     "climate_entities": [], "select_entities": [], "climate_entity_names": [],
                     "select_entity_names": [], "fallback": fallback}
        client = self.ha_client_factory() if self.ha_client_factory else None
        if client is None:
            out["error"] = "Home Assistant ist nicht eingerichtet: URL und Token speichern, dann Auswahl neu laden"
            return out
        try:
            states = client.get_states()
        except HAError as e:
            out["error"] = str(e)
            return out
        by_id = {s["entity_id"]: s for s in states if isinstance(s.get("entity_id"), str)}
        out["source"] = "ha"
        out["climate_entities"] = sorted(e for e in by_id if e.startswith("climate."))
        selects = sorted(e for e in by_id if e.startswith("select."))
        out["select_entities"] = [e for e in selects if "swing" in e] or selects

        def named(ids: list[str]) -> list[dict]:  # {id, name}: the same entities with Home Assistant's friendly_name
            result = []
            for eid in ids:
                attrs = by_id[eid].get("attributes")
                name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
                result.append({"id": eid, "name": name if isinstance(name, str) and name.strip() else None})
            return result

        out["climate_entity_names"] = named(out["climate_entities"])
        out["select_entity_names"] = named(out["select_entities"])

        def listed(entity_id: str, attribute: str) -> list[str]:
            attrs = by_id.get(entity_id, {}).get("attributes")
            values = attrs.get(attribute) if isinstance(attrs, dict) else None
            return [v for v in values if isinstance(v, str) and v] if isinstance(values, list) else []

        if entity not in by_id:
            out["error"] = f"{entity} wurde in Home Assistant nicht gefunden"
        for key, source, attribute in (("hvac_modes", entity, "hvac_modes"), ("preset_modes", entity, "preset_modes"),
                                       ("fan_modes", entity, "fan_modes"), ("horizontal", h_select, "options"),
                                       ("vertical", v_select, "options")):
            values = listed(source, attribute)
            if key == "hvac_modes":  # only modes a cooling automation can use (not off, heat, auto)
                values = [v for v in values if v in HVAC_MODES]
            if values:
                out[key] = values
        return out

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
            except (ValueError, RecursionError):  # JSON/Unicode errors, int literals over the digit limit
                raise BadRequest("Body ist kein gültiges JSON") from None

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if method == "POST" and path == "/api/external":
                    if app.push_throttled():
                        log.debug("push from %s rejected: too many failures", self.client_address[0])
                        return self._send(429, {"ok": False, "error": "Zu viele Fehlversuche, bitte später erneut versuchen"},
                                          extra={"Retry-After": str(int(FAIL_WINDOW_S))})
                    source = app.push_source(self.headers.get("Authorization"))
                    if source is None:
                        app.note_push_failure()
                        log.warning("failed push-token auth from %s", self.client_address[0])
                        return self._error(401, "ungültiges Token")
                    return self._send(200, app.push(source, self._body()))
                auth = self.headers.get("Authorization")
                try:
                    authenticated = app.check_basic(auth)
                except LoginThrottled:
                    log.debug("login from %s rejected: too many failures", self.client_address[0])
                    return self._send(429, {"ok": False, "error": "Zu viele Fehlversuche, bitte später erneut versuchen"},
                                      extra={"Retry-After": str(int(FAIL_WINDOW_S))})
                except LoginBusy:
                    return self._send(503, {"ok": False, "error": "Server ausgelastet, bitte erneut versuchen"},
                                      extra={"Retry-After": "2"})
                if not authenticated:
                    if auth:
                        log.warning("failed login from %s", self.client_address[0])
                    time.sleep(FAIL_DELAY_S)
                    return self._send(401, {"ok": False, "error": "Anmeldung erforderlich"},
                                      extra={"WWW-Authenticate": 'Basic realm="aquacontrol", charset="UTF-8"'})
                self._route(method, path)
            except BadRequest as e:
                self._error(400, str(e))
            except (ValidationError, ScheduleError, ExternalError, BackupError, ClimateConfigError) as e:
                self._error(400, str(e))
            except NotFound:
                self._error(404, "nicht gefunden")
            except HAError as e:
                self._error(502, str(e))
            except DeviceError as e:
                self._error(502, str(e))
            except Exception:
                log.exception("unhandled error for %s %s", method, path)
                self._error(500, "interner Fehler")

        def _route(self, method: str, path: str) -> None:
            if method == "GET" and path in STATIC_FILES:
                fname, ctype = STATIC_FILES[path]
                try:
                    data = (app.static_dir / fname).read_bytes()
                except OSError:
                    return self._error(404, "nicht gefunden")
                return self._send(200, data, ctype)
            if method == "GET" and path == "/api/status":
                return self._send(200, app.status_json())
            if method == "GET" and path == "/api/history":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
                raw = query.get("minutes", ["60"])[0]
                if not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= 360:
                    raise BadRequest("minutes muss eine Zahl von 1 bis 360 sein")
                return self._send(200, app.monitor.history(int(raw)))
            if method == "GET" and path == "/api/settings":
                return self._send(200, app.settings_json())
            if method == "GET" and path == "/api/schedule":
                return self._send(200, app.schedule_json())
            if method == "GET" and path == "/api/climate":
                return self._send(200, app.climate_json())
            if method == "GET" and path == "/api/climate/options":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                return self._send(200, app.climate_options(query))
            if method == "GET" and path == "/api/backups":
                return self._send(200, app.backups_json())
            if method in ("PUT", "POST"):
                body = self._body()
                if not isinstance(body, dict):
                    raise BadRequest("Body muss ein JSON-Objekt sein")
                m = re.fullmatch(r"/api/settings/fan/(\d)", path)
                if method == "PUT" and m:
                    return self._send(200, app.update_fan(int(m.group(1)), body))
                m = re.fullmatch(r"/api/settings/led/(\d)", path)
                if method == "PUT" and m:
                    return self._send(200, app.update_led(int(m.group(1)), body))
                if method == "PUT" and path == "/api/settings/strip":
                    return self._send(200, app.update_strip(body))
                if method == "PUT" and path == "/api/schedule":
                    return self._send(200, app.put_schedule(body))
                if method == "POST" and path == "/api/schedule/override":
                    return self._send(200, app.override(body))
                if method == "PUT" and path == "/api/climate":
                    return self._send(200, app.put_climate(body))
                if method == "POST" and path == "/api/climate/test":
                    return self._send(200, app.climate_test())
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

    def __init__(self, *args, **kwargs):
        self._conn_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._conn_slots.acquire(blocking=False):  # too many open connections: drop this one
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:  # the thread did not start, so nobody else will release the slot
            self._conn_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_slots.release()  # exactly once per accepted connection, also after TLS failures

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
