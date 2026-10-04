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
