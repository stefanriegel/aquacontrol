"""Raw settings-report backups on disk. Files named `pinned_*` are never pruned."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .protocol import check_settings_report

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+\.bin")


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
                 for f in self.dir.iterdir() if _NAME_RE.fullmatch(f.name)]
        # newest first; pinned and normal names both start with a sortable timestamp
        return sorted(items, key=lambda b: b.name.removeprefix("pinned_"), reverse=True)

    def load(self, name: str) -> bytes:
        if not _NAME_RE.fullmatch(name):
            raise BackupError(f"ungültiger Backup-Name {name!r}")
        path = self.dir / name
        if not path.is_file():
            raise BackupError(f"Backup {name} existiert nicht")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise BackupError(f"Backup {name} nicht lesbar: {e.strerror or e}") from e
        try:
            check_settings_report(data)
        except ValueError as e:
            raise BackupError(f"Backup {name} ist beschädigt: {e}") from e
        return data

    def _prune(self) -> None:
        normal = [b for b in self.list() if not b.pinned]
        for old in normal[self.keep:]:
            (self.dir / old.name).unlink(missing_ok=True)
