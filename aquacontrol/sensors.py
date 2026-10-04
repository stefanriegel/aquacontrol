"""Display-only temperatures: host hwmon sensors and values pushed by other machines."""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

EXTERNAL_MAX_SENSORS = 32
EXTERNAL_MAX_AGE_S = 30.0
EXTERNAL_UNITS = ("°C", "W", "%")
_ID_RE = re.compile(r"[a-z0-9_-]{1,32}")


@dataclass(frozen=True)
class Reading:
    id: str        # "k10temp/Tctl" or "llm-vm/gpu0"
    label: str
    value: float
    unit: str


def _natural(path: Path) -> tuple[str, int]:
    m = re.match(r"^(\D*)(\d*)$", path.name)
    return (m.group(1), int(m.group(2) or 0)) if m else (path.name, 0)


def _device_id(hw: Path) -> str:
    """Basename of the resolved <hwmonN>/device link (e.g. "1-0051"), else the hwmon dir name."""
    link = hw / "device"
    return link.resolve().name if link.exists() else hw.name


def _friendly_label(chip: str, label: str, ram_rank: int | None) -> str | None:
    """German display label for well-known hwmon channels, None when there is no better name than the id."""
    if chip == "k10temp":
        if label == "Tctl":
            return "CPU"
        m = re.fullmatch(r"Tccd(\d+)", label)
        if m:
            return f"CPU CCD{m.group(1)}"
    elif chip == "spd5118" and ram_rank is not None:
        return f"RAM {ram_rank}"
    elif chip == "amdgpu" and label == "edge":
        return "iGPU"
    elif chip == "nvme":
        if label == "Composite":
            return "NVMe"
        if re.fullmatch(r"Sensor \d+", label):
            return f"NVMe {label}"
    return None


def read_host_sensors(labels: dict[str, str | None], sys_root: str | Path = "/sys/class/hwmon") -> list[Reading]:
    """All hwmon temperatures except the QUADRO's. `labels` renames (str) or hides (None); without a
    configured label a friendly German one is derived for well-known chips (see _friendly_label)."""
    out: list[Reading] = []
    root = Path(sys_root)
    if not root.is_dir():
        return out
    found = []  # (hwmon dir, chip name, label, input file)
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
            found.append((hw, name, label, inp))
    # Ids must be unique and stable across reboots (hwmonN numbering is not): when "<name>/<label>"
    # occurs more than once, every member becomes "<name>@<dev>/<label>" (dev = bus address of the device).
    counts = Counter(f"{name}/{label}" for _, name, label, _ in found)
    ids = []
    for hw, name, label, inp in found:
        sid = f"{name}/{label}"
        if counts[sid] > 1:
            sid = f"{name}@{_device_id(hw)}/{label}"
        ids.append(sid)
    # RAM modules are numbered in id order over all of them, hidden or renamed ones included, so a
    # module keeps its number when a neighbour is hidden
    ram_ids = sorted(sid for sid, (_, name, _, _) in zip(ids, found) if name == "spd5118")
    for sid, (hw, name, label, inp) in zip(ids, found):
        if sid in labels and labels[sid] is None:
            continue
        try:
            value = int(inp.read_text().strip()) / 1000
        except (OSError, ValueError):
            continue  # e.g. ENODATA for an unconnected sensor
        rank = ram_ids.index(sid) + 1 if name == "spd5118" else None
        out.append(Reading(sid, labels.get(sid) or _friendly_label(name, label, rank) or sid, value, "°C"))
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
        seen: set[str] = set()
        for s in sensors:
            if not isinstance(s, dict):
                raise ExternalError("Sensor-Eintrag muss ein Objekt sein")
            sid, label, value, unit = s.get("id"), s.get("label", s.get("id")), s.get("value"), s.get("unit")
            if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
                raise ExternalError(f"ungültige Sensor-id {sid!r}")
            if sid in seen:
                raise ExternalError(f"Sensor-id {sid!r} kommt mehrfach vor")
            seen.add(sid)
            if (not isinstance(label, str) or len(label) > 40
                    or any(unicodedata.category(ch) in ("Cc", "Cs") for ch in label)):
                # control characters and lone surrogates (JSON escape ud800) would break the JSON output
                raise ExternalError(f"ungültiges label für {sid}")
            # the range check also rejects nan/inf and huge ints (math.isfinite would overflow on 10**400)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not -50 <= value <= 1000:
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
