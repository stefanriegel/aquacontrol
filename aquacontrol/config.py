"""Two config files: daemon.json (admin-owned, read-only for the daemon) and
config.json (daemon-owned, rewritten atomically when the schedule changes)."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .climate import ClimateConfig, ClimateConfigError, merge_climate, parse_climate_config
from .schedule import Rule, parse_rules, rules_to_json

log = logging.getLogger(__name__)

DEFAULT_APP_CONFIG = {
    "fans": {"1": {"name": "Pumpe", "min_percent": 25}, "2": {"name": "140mm Radiator"},
             "3": {"name": "420mm Radiator"}, "4": {"name": "Gehäuselüfter"}},
    "sensors": {"1": "Wasser Temp"},
    "host_sensors": {"k10temp/Tctl": "CPU", "nvme/Composite": "NVMe"},
    "leds": {},
    "schedule": [],  # no rules by default: a fresh install never writes to the device on its own
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


def _atomic_write(path: Path, data: dict, mode: int = 0o644, force_mode: bool = False) -> None:
    """Write JSON via temp file + rename. An existing file keeps its mode and owner, unless force_mode is set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")  # created 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if force_mode:
            os.chmod(tmp, mode)
        elif path.exists():
            st = path.stat()
            os.chmod(tmp, st.st_mode & 0o777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        else:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Secrets:
    """secrets.json next to config.json (mode 0600). Holds the Home Assistant token; the token is never
    logged and never leaves this class except through get_ha_token()."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("%s is unreadable, treating it as empty", self.path)
            return {}
        return raw if isinstance(raw, dict) else {}

    def get_ha_token(self) -> str:
        with self._lock:
            token = self._load().get("ha_token", "")
        return token if isinstance(token, str) else ""

    @staticmethod
    def clean_token(token: object) -> str:
        """Strip surrounding whitespace and check the token (printable ASCII, no spaces: it ends up in an HTTP
        header). The empty string is valid and means "delete"."""
        if not isinstance(token, str):
            raise ClimateConfigError("Token muss ein Text sein")
        token = token.strip()
        if token and not re.fullmatch(r"[\x21-\x7e]{1,4096}", token):
            raise ClimateConfigError("Token enthält ungültige Zeichen oder ist zu lang")
        return token

    def set_ha_token(self, token: str) -> None:
        """Store the token; an empty string removes it."""
        token = self.clean_token(token)
        with self._lock:
            data = self._load()
            if token:
                data["ha_token"] = token
            else:
                data.pop("ha_token", None)
            _atomic_write(self.path, data, mode=0o600, force_mode=True)


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
        self.secrets = Secrets(self.path.with_name("secrets.json"))
        try:
            self._climate = parse_climate_config(self._raw.get("climate", {}))
        except ClimateConfigError as e:  # a broken section must not stop the daemon; automation stays off
            log.warning("%s: ungültiger climate-Abschnitt (%s), Klima-Automatik bleibt aus", self.path, e)
            self._climate = parse_climate_config({})

    def fan_name(self, index: int, fallback: str) -> str:
        return self._raw.get("fans", {}).get(str(index + 1), {}).get("name") or fallback

    def min_percent(self) -> dict[int, float]:
        # The pump floor (fan 1) always exists; other values count only if valid.
        out = {0: float(DEFAULT_APP_CONFIG["fans"]["1"]["min_percent"])}
        fans = self._raw.get("fans", {})
        for key, fan in (fans.items() if isinstance(fans, dict) else ()):
            if key not in ("1", "2", "3", "4") or not isinstance(fan, dict):
                continue
            value = fan.get("min_percent")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
                out[int(key) - 1] = float(value)
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

    # --- climate automation -------------------------------------------------------------------------
    def climate_config(self) -> ClimateConfig:
        with self._lock:
            return self._climate

    def climate_raw(self) -> dict:
        """The full climate section (defaults filled in) as a fresh dict. Never contains the token."""
        with self._lock:
            return self._climate.to_json()

    def set_climate(self, raw: object) -> ClimateConfig:
        cfg = parse_climate_config(raw)  # raises ClimateConfigError before anything is written
        with self._lock:
            self._store_climate(cfg)
        return cfg

    def patch_climate(self, patch: object) -> ClimateConfig:
        """Partial update: merge onto the current section under the lock, validate, persist."""
        if not isinstance(patch, dict):
            raise ClimateConfigError("Klima-Konfiguration muss ein Objekt sein")
        with self._lock:
            cfg = parse_climate_config(merge_climate(self._climate.to_json(), patch))
            self._store_climate(cfg)
        return cfg

    def _store_climate(self, cfg: ClimateConfig) -> None:
        raw = dict(self._raw)
        raw["climate"] = cfg.to_json()
        _atomic_write(self.path, raw)
        self._raw = raw
        self._climate = cfg
