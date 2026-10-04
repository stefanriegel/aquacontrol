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
