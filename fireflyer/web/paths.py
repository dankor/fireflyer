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
(list/get/create/save/delete over `DashboardRow`), so the gallery + editor routes
stay store-agnostic. The dashboard's **id is its filename stem**.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fireflyer.dashboard import Dashboard, DashboardError
from fireflyer.web.portal import DashboardRow


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
    """Dashboards as `<root>/dashboards/*.yaml`, keyed by filename stem."""

    def __init__(self, root: str):
        self._dir = Path(root) / "dashboards"

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
        return candidate

    def save(self, stem: str, yaml: str) -> None:
        Dashboard.from_yaml(yaml)  # validate before writing
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(stem).write_text(yaml)

    def delete(self, stem: str) -> None:
        path = self._path(stem)
        if path.exists():
            path.unlink()
