"""Local (non-portal) *paths* mode: dashboards are YAML files in a folder you
Docker-map under a base dir, and each path gets its own isolated dataset blob
store.

You bind-mount host folders into the container's base dir (`FIREFLYER_PATHS`,
default `/paths`); **each mapped folder is a selectable "path"**. A path's
dashboards live in `<path>/dashboards/*.yaml` (files you own / commit); its
datasets live in the container's object store isolated per path
(`<FIREFLYER_DATA>/<path>/`), because they're Parquet you create via web upload
— never written into your path folder.

`PathDashboardStore` mirrors the portal `SqliteStore`/`PostgresStore` surface
(list/get/create/save/delete over `DashboardRow`, versions/version over
`DashboardVersion`), so the gallery + editor routes stay store-agnostic. The
dashboard's **id is its filename stem**.

A dashboard's **version history** lives beside the path's datasets, under
`<FIREFLYER_DATA>/<path>/history/<stem>/` — one JSON file per version — not in
the dashboards folder, which is yours (and may well be a git repo).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from fireflyer.dashboard import Dashboard, DashboardError
from fireflyer.web.portal import DashboardRow, DashboardVersion


def list_paths(base: str) -> list[str]:
    """Names of the folders mapped under the base dir — the path switcher list."""
    p = Path(base)
    if not p.is_dir():
        return []
    return sorted(
        d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")
    )


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "dashboard"


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
        timespec="seconds"
    )


class PathDashboardStore:
    """Dashboards as `<root>/dashboards/*.yaml`, keyed by filename stem; their
    versions under `history` (a folder; None keeps no history)."""

    def __init__(self, root: str, history: str | None = None):
        self._dir = Path(root) / "dashboards"
        self._history = Path(history) if history else None

    def _path(self, stem: str) -> Path:
        # Stems are filenames only — reject anything with path separators.
        if not stem or "/" in stem or "\\" in stem or stem.startswith("."):
            raise DashboardError(f"invalid dashboard id {stem!r}")
        return self._dir / f"{stem}.yaml"

    def _row(self, path: Path) -> DashboardRow:
        text = path.read_text()
        try:
            name = Dashboard.from_yaml(text).name
        except DashboardError:
            name = path.stem
        return DashboardRow(
            id=path.stem, name=name, author="", yaml=text, updated_at=_mtime(path)
        )

    def list(self) -> list[DashboardRow]:
        if not self._dir.is_dir():
            return []
        rows = [self._row(f) for f in self._dir.glob("*.yaml")]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    def get(self, stem: str) -> DashboardRow | None:
        try:
            path = self._path(stem)
        except DashboardError:
            return None
        return self._row(path) if path.exists() else None

    def create(self, yaml: str, author: str = "") -> str:
        name = Dashboard.from_yaml(yaml).name  # validates + reads name
        self._dir.mkdir(parents=True, exist_ok=True)
        stem = _slug(name)
        # Avoid clobbering an existing file — suffix until free.
        candidate, n = stem, 2
        while self._path(candidate).exists():
            candidate, n = f"{stem}-{n}", n + 1
        self._path(candidate).write_text(yaml)
        self._record(candidate, yaml, author)
        return candidate

    def save(self, stem: str, yaml: str, author: str = "") -> None:
        Dashboard.from_yaml(yaml)  # validate before writing
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(stem)
        # A dashboard from before history existed: what's on disk is version 1.
        if path.exists() and not self.versions(stem):
            self._record(stem, path.read_text(), "", int(path.stat().st_mtime))
        path.write_text(yaml)
        self._record(stem, yaml, author)

    def delete(self, stem: str) -> None:
        path = self._path(stem)
        if path.exists():
            path.unlink()
        folder = self._versions_dir(stem)
        if folder and folder.exists():
            shutil.rmtree(folder)

    # --- versions -------------------------------------------------------------

    def _versions_dir(self, stem: str) -> Path | None:
        self._path(stem)                     # the same guard against odd ids
        return self._history / stem if self._history else None

    def _read(self, file: Path) -> DashboardVersion:
        data = json.loads(file.read_text())
        return DashboardVersion(data["version"], data["created_at"], data["author"], data["yaml"])

    def _record(self, stem: str, yaml: str, author: str, at: int | None = None) -> None:
        """Keep `yaml` as the next version — unless it's the latest already."""
        folder = self._versions_dir(stem)
        if folder is None:
            return
        listed = self.versions(stem)
        if listed and self.version(stem, listed[0].version).yaml == yaml:
            return
        number = listed[0].version + 1 if listed else 1
        folder.mkdir(parents=True, exist_ok=True)
        record = {"version": number, "created_at": at if at is not None else int(time.time()),
                  "author": author, "yaml": yaml}
        (folder / f"{number}.json").write_text(json.dumps(record))

    def versions(self, stem: str) -> list[DashboardVersion]:
        """Newest first, without their YAML."""
        folder = self._versions_dir(stem)
        if not folder or not folder.is_dir():
            return []
        out = []
        for file in folder.glob("*.json"):
            v = self._read(file)
            out.append(DashboardVersion(v.version, v.created_at, v.author))
        return sorted(out, key=lambda v: v.version, reverse=True)

    def version(self, stem: str, number: int) -> DashboardVersion | None:
        folder = self._versions_dir(stem)
        file = folder / f"{int(number)}.json" if folder else None
        return self._read(file) if file and file.is_file() else None
