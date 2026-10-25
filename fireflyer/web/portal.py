"""Portal mode: DB-backed dashboard persistence + a listing gallery.

Editor-only, like the rest of `web/`. This is an owner-approved exception to the
"no persistence / multi-user" anti-goal in architecture.md, kept deliberately
small: dashboards are stored as an opaque YAML text blob (validated by the
backend via `Dashboard.from_yaml`, never decomposed into tables), so every
existing stateless editor route keeps working byte-for-byte.

Two stores share one tiny schema. `SqliteStore` (stdlib, in-memory by default)
powers local dev and the test suite — no service, no driver. `PostgresStore`
powers `python -m fireflyer.portal` at runtime and imports `psycopg` lazily so
the core install and `pip install -e ".[test]"` never need the `.[portal]`
extra. Tests exercise this module directly (no web stack), matching how
params/config_edit logic is kept unit-testable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

import yaml

from fireflyer import column_map
from fireflyer.dashboard import Dashboard
from fireflyer.datasets import Dataset, Version
from fireflyer.params import COLUMN_TYPES, column_type, type_icon_svg
from fireflyer.web import assets

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboards ("
    " id TEXT PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " author TEXT NOT NULL DEFAULT '',"
    " yaml TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)"
)
# Every saved state of every dashboard, numbered 1, 2, 3… per dashboard, with
# who saved it and when (Unix seconds). What the editor's compare view diffs.
_VERSIONS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboard_versions ("
    " dashboard_id TEXT NOT NULL,"
    " version INTEGER NOT NULL,"
    " yaml TEXT NOT NULL,"
    " author TEXT NOT NULL DEFAULT '',"
    " created_at BIGINT NOT NULL,"
    " PRIMARY KEY (dashboard_id, version))"
)


# What each dashboard reads — `(dataset, column)` pairs from `Dashboard.uses`,
# written on every save — so the overviews (a dashboard's datasets, a dataset's
# dashboards, a column's) are a query, not a parse of every dashboard. Each
# indexed dashboard also has one `('', '')` row, so "indexed, uses nothing" is
# told apart from "not indexed yet" (a dashboard from before the index).
_USES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboard_uses ("
    " dashboard_id TEXT NOT NULL,"
    " dataset TEXT NOT NULL,"
    " column_name TEXT NOT NULL)"
)


@dataclass(frozen=True)
class Use:
    """One thing a dashboard reads: a dataset (`column` ""), one of its columns,
    or every column (`column` "*")."""
    dashboard_id: str
    dashboard_name: str
    dataset: str
    column: str


def _uses_of(yaml: str) -> list[tuple[str, str]]:
    try:
        return Dashboard.uses(yaml)
    except DashboardError:
        return []


@dataclass
class DashboardRow:
    id: str
    name: str
    author: str
    yaml: str
    updated_at: str

    @property
    def description(self) -> str:
        """The YAML's top-level `description:`, read on demand rather than kept
        as a column: like `name`, it's part of the definition, and every store
        already holds the YAML. "" when absent or the YAML doesn't parse."""
        try:
            raw = yaml.safe_load(self.yaml) or {}
        except yaml.YAMLError:
            return ""
        description = raw.get("description") if isinstance(raw, dict) else None
        return description.strip() if isinstance(description, str) else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DashboardVersion:
    """One saved state of a dashboard. `yaml` is "" in a listing — fetched on
    its own when a version is opened."""
    version: int
    created_at: int        # Unix seconds; the browser shows it in local time
    author: str
    yaml: str = ""


def _unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return int(time.time())


class _SqlVersions:
    """Version history over SQL, shared by both stores. A store supplies
    `_run(sql, params)` — `?` placeholders, the store adapts them — and these
    record a version on create and save, list them, and fetch one.

    A save that changes nothing records nothing. A dashboard saved before
    history existed gets its stored state recorded as version 1 on its first
    save, so even an old dashboard can be compared with its first edit."""

    def _latest(self, id: str):
        return self._run(
            "SELECT version, yaml FROM dashboard_versions WHERE dashboard_id = ?"
            " ORDER BY version DESC LIMIT 1", (id,),
        ).fetchone()

    def _record(self, id: str, yaml: str, author: str, at: int | None = None) -> int:
        latest = self._latest(id)
        if latest and latest[1] == yaml:
            return latest[0]
        number = latest[0] + 1 if latest else 1
        self._run(
            "INSERT INTO dashboard_versions (dashboard_id, version, yaml, author, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (id, number, yaml, author, at if at is not None else int(time.time())),
        )
        return number

    def _baseline(self, id: str) -> None:
        """Record what's stored now as version 1, if this dashboard has none."""
        if self._latest(id) is None:
            row = self.get(id)
            if row is not None:
                self._record(id, row.yaml, row.author, _unix(row.updated_at))

    def versions(self, id: str) -> list[DashboardVersion]:
        """Newest first, without their YAML."""
        rows = self._run(
            "SELECT version, created_at, author FROM dashboard_versions"
            " WHERE dashboard_id = ? ORDER BY version DESC", (id,),
        ).fetchall()
        return [DashboardVersion(int(v), int(at), a) for v, at, a in rows]

    def version(self, id: str, number: int) -> DashboardVersion | None:
        row = self._run(
            "SELECT version, created_at, author, yaml FROM dashboard_versions"
            " WHERE dashboard_id = ? AND version = ?", (id, number),
        ).fetchone()
        return DashboardVersion(int(row[0]), int(row[1]), row[2], row[3]) if row else None


class _SqlUses:
    """The `dashboard_uses` index over SQL, shared by both stores (`_run` as in
    `_SqlVersions`). Written on create/save, dropped on delete; a dashboard not
    indexed yet is indexed the first time the index is read."""

    def _index(self, id: str, yaml: str) -> None:
        self._run("DELETE FROM dashboard_uses WHERE dashboard_id = ?", (id,))
        for dataset, column in [("", "")] + _uses_of(yaml):
            self._run(
                "INSERT INTO dashboard_uses (dashboard_id, dataset, column_name) VALUES (?, ?, ?)",
                (id, dataset, column),
            )

    def uses(self) -> list[Use]:
        for id, yaml in self._run(
            "SELECT id, yaml FROM dashboards"
            " WHERE id NOT IN (SELECT dashboard_id FROM dashboard_uses)"
        ).fetchall():
            self._index(str(id), yaml)
        rows = self._run(
            "SELECT u.dashboard_id, d.name, u.dataset, u.column_name FROM dashboard_uses u"
            " JOIN dashboards d ON d.id = u.dashboard_id WHERE u.dataset <> ''"
            " ORDER BY d.name, u.dataset, u.column_name"
        ).fetchall()
        return [Use(str(i), n, ds, c) for i, n, ds, c in rows]


def _name_of(yaml_text: str) -> str:
    """The dashboard's display name, from the YAML's required top-level `name:`
    key. Also validates: `from_yaml` raises DashboardError on an invalid or
    nameless dashboard. Parsing only — never opens the CSV, so a missing dataset
    path is fine."""
    return Dashboard.from_yaml(yaml_text).name


class SqliteStore(_SqlVersions, _SqlUses):
    """Local/test store. `:memory:` for tests, a file path for local portal dev."""

    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False: uvicorn serves requests off a threadpool.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_VERSIONS_SCHEMA)
        self._conn.execute(_USES_SCHEMA)
        self._conn.commit()

    def _run(self, sql: str, params=()):
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor

    def list(self) -> list[DashboardRow]:
        rows = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards"
            " ORDER BY updated_at DESC"
        ).fetchall()
        return [DashboardRow(*r) for r in rows]

    def get(self, id: str) -> DashboardRow | None:
        row = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards WHERE id = ?",
            (id,),
        ).fetchone()
        return DashboardRow(*row) if row else None

    def create(self, yaml: str, author: str = "") -> str:
        name = _name_of(yaml)  # validates + reads the top-level `name:` key
        new_id, now = str(uuid.uuid4()), _now()
        self._conn.execute(
            "INSERT INTO dashboards (id, name, author, yaml, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, name, author, yaml, now, now),
        )
        self._conn.commit()
        self._record(new_id, yaml, author)
        self._index(new_id, yaml)
        return new_id

    def save(self, id: str, yaml: str, author: str = "") -> None:
        # Name is re-derived from the YAML; author (the creator) is left as-is.
        # `author` is who saved — recorded on the version, not the dashboard.
        name = _name_of(yaml)
        self._baseline(id)
        self._conn.execute(
            "UPDATE dashboards SET name = ?, yaml = ?, updated_at = ? WHERE id = ?",
            (name, yaml, _now(), id),
        )
        self._conn.commit()
        self._record(id, yaml, author)
        self._index(id, yaml)

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = ?", (id,))
        self._conn.execute("DELETE FROM dashboard_versions WHERE dashboard_id = ?", (id,))
        self._conn.execute("DELETE FROM dashboard_uses WHERE dashboard_id = ?", (id,))
        self._conn.commit()


class PostgresStore(_SqlVersions, _SqlUses):
    """Runtime store. Same schema, `%s` placeholders; imports psycopg lazily so
    this module stays importable without the `.[portal]` extra installed."""

    def __init__(self, dsn: str):
        import psycopg  # optional dependency, only needed at portal runtime

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_VERSIONS_SCHEMA)
        self._conn.execute(_USES_SCHEMA)

    def _run(self, sql: str, params=()):
        return self._conn.execute(sql.replace("?", "%s"), params)

    def list(self) -> list[DashboardRow]:
        rows = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards"
            " ORDER BY updated_at DESC"
        ).fetchall()
        return [DashboardRow(str(r[0]), r[1], r[2], r[3], str(r[4])) for r in rows]

    def get(self, id: str) -> DashboardRow | None:
        row = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards WHERE id = %s",
            (id,),
        ).fetchone()
        if not row:
            return None
        return DashboardRow(str(row[0]), row[1], row[2], row[3], str(row[4]))

    def create(self, yaml: str, author: str = "") -> str:
        name = _name_of(yaml)  # validates + reads the top-level `name:` key
        new_id, now = str(uuid.uuid4()), _now()
        self._conn.execute(
            "INSERT INTO dashboards (id, name, author, yaml, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (new_id, name, author, yaml, now, now),
        )
        self._record(new_id, yaml, author)
        self._index(new_id, yaml)
        return new_id

    def save(self, id: str, yaml: str, author: str = "") -> None:
        # Name is re-derived from the YAML; author (the creator) is left as-is.
        # `author` is who saved — recorded on the version, not the dashboard.
        name = _name_of(yaml)
        self._baseline(id)
        self._conn.execute(
            "UPDATE dashboards SET name = %s, yaml = %s, updated_at = %s WHERE id = %s",
            (name, yaml, _now(), id),
        )
        self._record(id, yaml, author)
        self._index(id, yaml)

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = %s", (id,))
        self._conn.execute("DELETE FROM dashboard_versions WHERE dashboard_id = %s", (id,))
        self._conn.execute("DELETE FROM dashboard_uses WHERE dashboard_id = %s", (id,))


def make_store(dsn: str | None):
    """Postgres when a DSN is given (portal runtime); otherwise a local sqlite
    file so the portal can be tried without standing up a database."""
    if dsn:
        return PostgresStore(dsn)
    return SqliteStore("portal.db")


# --- gallery page -----------------------------------------------------------
# Editor chrome, not chart output, so f-strings are fine here (the no-f-string
# rule is for chart HTML). User input — dashboard names — is escape()'d; ids are
# UUIDs and timestamps are machine-generated, so they're safe. The styles and
# the dialog openers live in static/ (gallery.css, nav.css, gallery.js).

# Inline-SVG action icons (stroke=currentColor so they follow the button colour).
_ICONS = {
    "pencil": '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
             '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
              '<path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/>',
    "back": '<path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "code": '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
    "compare": '<rect x="3" y="4" width="7" height="16" rx="1.5"/><rect x="14" y="4" width="7" height="16" rx="1.5"/>'
               '<path d="M6 9h1M6 13h1M17 11h1M17 15h1"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    # An eye, crossed out: a service column — in the dataset, hidden from dashboards.
    "service": '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/>'
               '<circle cx="12" cy="12" r="2.8"/><path d="M4 4l16 16"/>',
    "cross": '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
    "save": '<path d="M5 3h11l4 4v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M8 3v5h7V3"/><rect x="7" y="13" width="10" height="7" rx="1"/>',
}


_THEME_ICONS = {
    "auto": '<path d="M5 20 12 4l7 16M7.5 14h9"/>',
    "light": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
             'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "dark": '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>',
}


def theme_switch() -> str:
    """Auto / Light / Dark — in the topbar of every page, the editor's and the
    gallery's alike (styled in nav.css, which both link). One choice for all:
    kept in the browser (`ffTheme`) and applied as `data-ff-theme` on <html>."""
    labels = {"auto": "Auto (follow OS)", "light": "Light", "dark": "Dark"}
    buttons = "".join(
        f'<button type="button" data-mode="{mode}" title="Theme: {labels[mode]}"'
        f' aria-label="Theme: {labels[mode]}">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{_THEME_ICONS[mode]}</svg></button>'
        for mode in ("auto", "light", "dark")
    )
    return f'<div class="ff-theme" id="theme-switch" role="group" aria-label="Theme">{buttons}</div>'


def _icon(name: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>'
    )


def name_cell(href: str, name: str, description: str = "") -> str:
    """A list row's name, linking to it, with its description as a small line
    under it — cut with an ellipsis, whole on hover — as a selected item's title
    shows it in the topbar."""
    sub = (
        f'<span class="sub" title="{escape(description, quote=True)}">{escape(description)}</span>'
        if description else ""
    )
    return f'<a class="name" href="{href}">{escape(name)}</a>{sub}'


def _stamp(iso: str) -> str:
    """A stored UTC time as `<time>`: `2026-10-16 14:52:07 UTC` as written, which
    gallery.js turns into the viewer's local time in the same shape."""
    text = iso.replace("T", " ")[:19]
    return f'<time datetime="{escape(iso)}">{escape(text)} UTC</time>' if text else ""


def _author_cell(author: str, authors: bool) -> str:
    """Who made it — a column only where there are people to name: with no
    login (local mode) every row would read "—"."""
    if not authors:
        return ""
    return f"<td>{escape(author) if author else '—'}</td>"


def _ref_links(links: list[tuple[str, str]], missing: set[str] = frozenset()) -> str:
    """What a list row is tied to — the datasets a dashboard reads, the
    dashboards a dataset feeds — as small links. A name in `missing` (a dataset
    a dashboard names that isn't uploaded) is shown, unlinked, so it stands out."""
    if not links:
        return '<span class="muted">—</span>'
    chips = "".join(
        f'<span class="ref is-missing" title="Not uploaded">{escape(label)}</span>' if label in missing
        else f'<a class="ref" href="{href}" title="{escape(label, quote=True)}">{escape(label)}</a>'
        for href, label in links
    )
    return f'<span class="refs">{chips}</span>'


def _dashboard_datasets(row: DashboardRow, known: set[str] | None,
                        uses: dict[str, list[str]] | None) -> str:
    """The uploaded datasets a dashboard reads (inline ones are part of it) —
    from the store's index when given, else worked out from its YAML."""
    names = sorted(uses.get(row.id, []) if uses is not None else Dashboard.dataset_names(row.yaml))
    missing = set(names) - known if known is not None else set()
    return _ref_links([(f"/datasets/{quote(n)}", n) for n in names], missing)


def _row(row: DashboardRow, authors: bool = True, known: set[str] | None = None,
         uses: dict[str, list[str]] | None = None) -> str:
    updated = _stamp(row.updated_at)
    return (
        "<tr>"
        f'<td>{name_cell(f"/d/{row.id}", row.name, row.description)}</td>'
        f"{_author_cell(row.author, authors)}"
        f'<td class="muted">{updated}</td>'
        f"<td>{_dashboard_datasets(row, known, uses)}</td>"
        # The name opens the dashboard, so the row needs no edit button.
        '<td class="actions">'
        f'<button class="act" type="button" title="Clone" data-id="{row.id}"'
        f' data-name="{escape(row.name, quote=True)}" onclick="openClone(this)">{_icon("copy")}</button>'
        f'<form method="post" action="/d/{row.id}/delete"'
        " onsubmit=\"return confirm('Remove this dashboard?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        "</td></tr>"
    )


def _dialog(
    dialog_id: str, form_id: str, action: str, heading: str, ok_label: str,
    description: bool = False,
) -> str:
    # `action` is empty for the clone dialog — set by JS from the clicked row.
    # Only a new dashboard asks for a description: a clone keeps its source's.
    about = (
        '\n    <textarea name="description" placeholder="Description (optional)"></textarea>'
        if description else ""
    )
    return f"""
<dialog id="{dialog_id}">
  <form id="{form_id}" method="post" action="{action}">
    <h3>{heading}</h3>
    <input name="name" id="{form_id}-name" placeholder="Dashboard name"
           autocomplete="off" required>{about}
    <div class="dialog-actions">
      <button type="button" class="cancel"
              onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">{ok_label}</button>
    </div>
  </form>
</dialog>"""



_CHEVRON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>'
)


def back_button(href: str, title: str) -> str:
    """The shared back button (dataset detail + dashboard editor use the same)."""
    t = escape(title, quote=True)
    return f'<a class="ff-back" href="{href}" title="{t}" aria-label="{t}">{_icon("back")}</a>'


def nav_switch(active: str) -> str:
    """The list pages' left-nav control: a segmented Dashboards | Datasets switch
    (`active` marks the current list). Not shown on selected-item pages."""
    def seg(href, label, key):
        cls = " active" if key == active else ""
        return f'<a class="ff-switch-seg{cls}" href="{href}">{label}</a>'
    return (
        '<nav class="ff-switch">'
        + seg("/", "Dashboards", "dashboards")
        + seg("/datasets", "Datasets", "datasets")
        + "</nav>"
    )


def path_dropdown(paths: list[str] | None, active_path: str | None) -> str:
    """A labelled dropdown showing the active path; picking one switches path.
    Lives on the **right** of the topbar. Empty with no paths (portal / plain)."""
    if not paths:
        return ""
    items = "".join(
        f'<a href="/path/{quote(p)}"'
        f'{" class=\"active\"" if p == active_path else ""}>{escape(p)}</a>'
        for p in paths
    )
    label = escape(active_path or "path")
    return (
        '<details class="ff-pathdd"><summary title="Switch path">'
        f"{label}{_CHEVRON}</summary>"
        f'<div class="ff-pathdd-menu">{items}</div></details>'
    )


def _shell(
    title: str,
    user_menu: str,
    active: str,
    body: str,
    extra: str = "",
    topbar_left: str = "",
    topbar_right: str = "",
    paths: list[str] | None = None,
    active_path: str | None = None,
    detail: bool = False,
    head: str = "",
) -> str:
    """The gallery page frame. Overview pages (dashboards/datasets lists) lead
    with a Dashboards | Datasets switch on the left; in local paths mode the path
    dropdown sits on the **right**. Detail pages (a selected dataset) lead with a
    back button (+ switch, local) in `topbar_left`. Then optional `topbar_right`,
    then `body`. No second nav bar."""
    nav = "" if detail else nav_switch(active)
    right = path_dropdown(paths, active_path) + topbar_right + theme_switch() + user_menu
    right_html = f'<span class="topbar-right">{right}</span>' if right else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
{assets.favicons()}
{assets.stylesheets("gallery.css", "nav.css", "profile.css")}
{assets.script("gallery.js")}
{head}
</head>
<body>
<header class="topbar">{nav}{topbar_left}{right_html}</header>
<main class="gallery">{body}</main>
{extra}
</body>
</html>"""


def render_gallery(
    rows: list[DashboardRow], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None, authors: bool = True,
    datasets: set[str] | None = None, uses: dict[str, list[str]] | None = None,
) -> str:
    """`datasets`: the names uploaded, so a dashboard naming one that isn't can
    say so; None links every name. `uses`: dashboard id -> the datasets it
    reads, from the store's index; None works it out from each YAML."""
    if rows:
        body_table = (
            '<table class="dash-table"><thead><tr>'
            f"<th>Name</th>{'<th>Author</th>' if authors else ''}<th>Last updated</th><th>Datasets</th><th></th>"
            "</tr></thead><tbody>"
            + "".join(_row(r, authors, datasets, uses) for r in rows)
            + "</tbody></table>"
        )
    else:
        body_table = '<div class="empty">No dashboards yet. Use + to create one.</div>'
    add_dialog = _dialog("add-dialog", "add-form", "/new", "New dashboard", "Create", description=True)
    clone_dialog = _dialog("clone-dialog", "clone-form", "", "Clone dashboard", "Clone")
    clone_dialog = clone_dialog.replace('id="clone-form-name"', 'id="clone-name"')
    return _shell(
        title, user_menu, "dashboards", body_table,
        extra=add_dialog + clone_dialog,
        topbar_right=f'<button class="act" type="button" title="New dashboard"'
                     f' onclick="openAdd()">{_icon("plus")}</button>',
        paths=paths, active_path=active_path,
    )


# --- datasets gallery -------------------------------------------------------



def _type_icon(dtype: str) -> str:
    """A small badge glyph for a Parquet column type — the same classification
    the filter fields show (`params.column_type`)."""
    kind = column_type(dtype)
    _, name = COLUMN_TYPES[kind]
    if kind == "other":
        name = dtype
    return f'<span class="type-icon" title="{escape(name)}">{type_icon_svg(kind)}</span>'


def _upload_dialog(action: str, heading: str, ok_label: str, dialog_id: str, name_field: bool) -> str:
    # Name and description belong to a new dataset; a new version of one only
    # brings a file (its description is edited from the dataset's title).
    name_rows = (
        '<label>Name</label><input name="name" autocomplete="off" required>'
        '<label>Description</label><textarea name="description"></textarea>'
        if name_field else ""
    )
    return f"""
<dialog class="wide" id="{dialog_id}">
  <form method="post" action="{action}" enctype="multipart/form-data">
    <h3>{heading}</h3>
    {name_rows}
    <label>CSV file</label>
    <input type="file" name="file" accept=".csv,text/csv" required>
    <label>Delimiter</label>
    <select name="delimiter">
      <option value=",">Comma ( , )</option>
      <option value=";">Semicolon ( ; )</option>
      <option value="\t">Tab</option>
      <option value="|">Pipe ( | )</option>
    </select>
    <div class="dialog-actions">
      <button type="button" class="cancel" onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">{ok_label}</button>
    </div>
  </form>
</dialog>"""


def _version_cell(ds: Dataset) -> str:
    """`2.1`, or `2.1 → 2.2` while the next one builds."""
    if ds.building:
        return f"{ds.version} → {ds.building}" if ds.version else f"→ {ds.building}"
    return ds.version


def _dataset_row(ds: Dataset, authors: bool = True,
                 used_by: dict[str, list[tuple[str, str]]] | None = None) -> str:
    href = f"/datasets/{quote(ds.name)}"
    updated = _stamp(ds.updated_at)
    dashboards = "" if used_by is None else (
        "<td>" + _ref_links([(f"/d/{did}", name) for did, name in used_by.get(ds.name, [])]) + "</td>"
    )
    return (
        "<tr>"
        f'<td>{name_cell(href, ds.name, ds.description)}</td>'
        f'<td class="muted mono">{_version_cell(ds)}</td>'
        f'<td class="muted">{len(ds.columns) + len(ds.service)}</td>'
        f'<td class="muted">{ds.rows}</td>'
        f'<td class="muted">{updated}</td>'
        f"{_author_cell(ds.author, authors)}"
        f"{dashboards}"
        # The name opens the dataset — where it's renamed and described — so
        # the row needs no view or edit button of its own.
        '<td class="actions">'
        f'<form method="post" action="/datasets/{quote(ds.name)}/delete"'
        " onsubmit=\"return confirm('Remove this dataset?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        "</td></tr>"
    )


def render_datasets(
    datasets: list[Dataset], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None, authors: bool = True,
    used_by: dict[str, list[tuple[str, str]]] | None = None,
) -> str:
    """`used_by`: dataset name -> the (id, name) of each dashboard reading it;
    None (no dashboards to look at, plain local mode) leaves the column out."""
    if datasets:
        table = (
            '<table class="dash-table"><thead><tr>'
            "<th>Name</th><th>Version</th><th>Columns</th><th>Rows</th><th>Updated</th>"
            f"{'<th>Author</th>' if authors else ''}"
            f"{'<th>Dashboards</th>' if used_by is not None else ''}<th></th>"
            "</tr></thead><tbody>"
            + "".join(_dataset_row(d, authors, used_by) for d in datasets)
            + "</tbody></table>"
        )
    else:
        table = '<div class="empty">No datasets yet. Use + to upload a CSV.</div>'
    extra = (
        _upload_dialog("/datasets/new", "New dataset", "Upload", "upload-dialog", True)
    )
    return _shell(
        title, user_menu, "datasets", table,
        extra=extra,
        topbar_right=f'<button class="act" type="button" title="New dataset"'
                     f' onclick="openUpload()">{_icon("plus")}</button>',
        paths=paths, active_path=active_path,
    )


def render_dataset_detail(
    ds: Dataset,
    title: str = "Fireflyer Portal",
    user_menu: str = "",
    used_by: list[tuple[str, str]] | None = None,
    paths: list[str] | None = None,
    active_path: str | None = None,
    mapping: str = "",
    versions: list[Version] | None = None,
    examples: dict[str, list[str]] | None = None,
    authors: bool = True,
    used_in: dict[str, list[tuple[str, str]]] | None = None,
) -> str:
    # The trash button: when the dataset is in use it shows a count badge and
    # opens the list of dashboards (open in a new tab) instead of deleting; when
    # nothing uses it, it deletes.
    used_by = used_by or []
    if used_by:
        trash = (
            f'<button class="act act-danger" type="button" title="Used by {len(used_by)} dashboard(s)"'
            ' onclick="document.getElementById(\'usage-dialog\').showModal()">'
            f'{_icon("trash")}<span class="badge">{len(used_by)}</span></button>'
        )
        links = "".join(
            f'<li><a href="/d/{did}" target="_blank" rel="noopener">{escape(name)}</a></li>'
            for did, name in used_by
        )
        usage_dialog = f"""
<dialog id="usage-dialog">
  <form method="dialog" style="padding:22px">
    <h3>Used by {len(used_by)} dashboard(s)</h3>
    <ul class="link-list">{links}</ul>
    <div class="dialog-actions"><button class="ok">Close</button></div>
  </form>
</dialog>"""
    else:
        trash = (
            f'<form method="post" action="/datasets/{quote(ds.name)}/delete" style="display:inline"'
            " onsubmit=\"return confirm('Remove this dataset?')\">"
            f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        )
        usage_dialog = ""

    # Header (back + name/description + actions) lives in the topbar, as a
    # dashboard's does in the editor: the name with the description's first
    # line under it, together one button opening a card with a field for each
    # (gallery.js `dsTitle`), so the toolbar needs neither an edit button nor
    # an ⓘ.
    desc = escape(ds.description, quote=True)
    tname = (
        '<span class="thead" id="ds-title-head" role="button" tabindex="0" aria-haspopup="dialog"'
        ' aria-label="Rename or describe the dataset">'
        f'<span class="tname" id="ds-title-name" data-name="{escape(ds.name, quote=True)}"'
        f' data-description="{desc}" data-action="/datasets/{quote(ds.name)}/rename">'
        f'{escape(ds.name)}</span>'
        f'<span class="tsub" id="ds-title-sub"{"" if ds.description else " hidden"}>'
        f"{escape(ds.description)}</span>"
        "</span>"
    )
    tabout = (
        '<span class="ff-title-about" id="ds-title-about" role="dialog" aria-label="Name and description" hidden>'
        '<input id="ds-title-name-input" type="text" aria-label="Dataset name" placeholder="Name"'
        ' spellcheck="false" autocomplete="off">'
        '<textarea id="ds-title-about-text" rows="3" placeholder="Add a description"'
        ' aria-label="Dataset description"></textarea>'
        '<span class="ff-title-error" id="ds-title-error" hidden></span></span>'
    )
    versions = versions or []
    # A selected item leads with the back button, the columns' mode switch and
    # the name, then the version — which opens the history. No Dashboards |
    # Datasets switch (that's for the lists); the path dropdown still rides on
    # the right in local paths mode (added by `_shell`).
    topbar_left = (
        '<div class="topbar-lead">'
        + back_button("/datasets", "Back to datasets")
        + _column_modes()
        + f'<div class="topbar-title" id="ds-title">{tname}{tabout}</div>'
        + _history_menu(ds, versions, authors)
        + "</div>"
    )
    topbar_right = (
        '<span class="ff-savebar" role="group" aria-label="Unsaved changes">'
        '<button class="ff-discard" id="ds-discard" type="button" title="Discard unsaved changes"'
        f' aria-label="Discard unsaved changes" hidden>{_icon("cross")}</button>'
        '<button class="ff-save" id="ds-save" type="button" title="Save (⌘/Ctrl+S)" aria-label="Save"'
        f' hidden>{_icon("save")}</button></span>'
        '<span class="detail-actions">'
        '<button class="act" title="Upload a new file (the next version)"'
        f" onclick=\"document.getElementById('replace-dialog').showModal()\">{_icon('upload')}</button>"
        f"{trash}</span>"
    )
    unreadable = _mapping_error(mapping, versions)
    status = (f'<div class="ds-status is-error" id="ds-status">{escape(unreadable)}</div>'
              if unreadable else _build_status(ds))
    body = f"""
  {status}
  <section class="ds-columns" id="ds-columns" data-mode="view">{_columns_view(ds, mapping, versions, examples or {}, used_in or {})}</section>
  <div class="ds-toast" id="ds-toast" role="status" hidden></div>
  <script type="application/json" id="ds-data">{_page_data(ds, mapping, versions, examples or {}, used_in or {})}</script>"""
    replace = _upload_dialog(
        f"/datasets/{quote(ds.name)}/replace", "Upload a new version", "Upload", "replace-dialog", False
    )
    page_head = (
        assets.stylesheets("code.css")
        + assets.import_map("yaml-look.js", "diff-view.js")
        + assets.module("dataset-columns.js")
    )
    return _shell(
        title, user_menu, "datasets", body,
        extra=replace + usage_dialog,
        topbar_left=topbar_left, topbar_right=topbar_right,
        paths=paths, active_path=active_path, detail=True, head=page_head,
    )


# --- a dataset's columns: view / edit / code / compare -----------------------------
# The page's columns panel switches between four modes, as the dashboard editor
# does: the mapping read (view), edited in a form (edit) or as YAML (code), and
# two versions of it side by side (compare). The view is rendered here; the
# rest is static/dataset-columns.js, from the JSON the page carries.

_COLUMN_MODES = (
    ("view", "eye", "Columns"),
    ("edit", "pencil", "Edit names and types"),
    ("code", "code", "Edit as YAML"),
    ("compare", "compare", "Compare versions"),
)


def _column_modes() -> str:
    buttons = "".join(
        f'<button type="button" data-mode="{mode}" title="{label}" aria-label="{label}"'
        f'{" class=active" if mode == "view" else ""}>{_icon(icon)}</button>'
        for mode, icon, label in _COLUMN_MODES
    )
    return f'<div class="ds-modes" id="ds-modes" role="group" aria-label="Columns">{buttons}</div>'


def _mapping_rules(ds: Dataset, mapping: str, versions: list[Version]) -> list[column_map.Rule]:
    latest = versions[0] if versions else None
    try:
        return column_map.parse(mapping, latest.sources if latest else [])
    except column_map.MappingError:
        return []


def _mapping_error(mapping: str, versions: list[Version]) -> str:
    """Why the mapping can't be read, if it can't — said, not an empty table."""
    try:
        column_map.parse(mapping, versions[0].sources if versions else [])
    except column_map.MappingError as exc:
        return str(exc)
    return ""


SERVICE_TITLE = "Service column — in the dataset, never in dashboards"


def _service_mark() -> str:
    return (f'<span class="col-service" title="{SERVICE_TITLE}" aria-label="{SERVICE_TITLE}">'
            f'{_icon("service")}</span>')


def _kinds(ds: Dataset) -> dict[str, tuple[str, str]]:
    """Column name -> (icon, kind) from the built data: a column's type is what
    its formula gave (text, a number from to_number(), a date from str2dt())."""
    out = {}
    for c in ds.columns + ds.service:
        kind = column_type(c.dtype)
        out[c.name] = (_type_icon(c.dtype), COLUMN_TYPES[kind][1] if kind in COLUMN_TYPES else c.dtype)
    return out


def _columns_view(ds: Dataset, mapping: str, versions: list[Version], examples: dict[str, list[str]],
                  used_in: dict[str, list[tuple[str, str]]]) -> str:
    kinds = _kinds(ds)
    rows = "".join(
        "<tr>"
        f"<td>{kinds.get(r.name, ('', ''))[0]}<span class=\"col-name\">{escape(r.name)}</span>"
        + (_service_mark() if r.service else "") + "</td>"
        f'<td class="muted mono">{escape(r.formula)}</td>'
        f'<td class="ex">{_examples(examples.get(r.name, []))}</td>'
        f"<td>{_ref_links([(f'/d/{did}', n) for did, n in used_in.get(r.name, [])])}</td></tr>"
        for r in _mapping_rules(ds, mapping, versions)
    )
    return (
        # The type is the icon before the name — what the column's formula gave,
        # read from the built data. "Dashboards": the ones whose charts read it.
        '<table class="ds-cols"><thead><tr><th>Column</th><th>Formula</th><th>Examples</th><th>Dashboards</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _examples(values: list[str]) -> str:
    """A column's first distinct values as it was uploaded, each in a label."""
    return '<span class="col-examples">' + "".join(
        f'<span class="col-example" style="--n:{len(v)}" title="{escape(v, quote=True)}"'
        f' data-value="{escape(v, quote=True)}">{escape(v)}</span>'
        for v in values
    ) + "</span>"


def _history_menu(ds: Dataset, versions: list[Version], authors: bool = True) -> str:
    """The version chip — `v3.4` — opening every version, grouped by upload.
    A version opens the comparison with the one in use; an upload that isn't
    the one in use can be deleted, with its versions."""
    in_use = {ds.version.split(".")[0], ds.building.split(".")[0]} - {""}
    groups = []
    for major in sorted({v.major for v in versions}, reverse=True):
        mine = [v for v in versions if v.major == major]
        delete = "" if str(major) in in_use else (
            f'<form class="ds-major-delete" method="post" action="/datasets/{quote(ds.name)}/versions/{major}/delete"'
            f' onsubmit="return confirm(\'Delete upload {major} and its {len(mine)} version(s)?\')">'
            f'<button class="act act-danger" type="submit" title="Delete upload {major} and its versions"'
            f' aria-label="Delete upload {major}">{_icon("trash")}</button></form>'
        )
        items = "".join(
            f'<button type="button" class="ds-ver" data-version="{v.label}">'
            f'<span class="ds-ver-label">{v.label}</span>'
            f'<span class="muted">{_stamp(datetime.fromtimestamp(v.created_at, timezone.utc).isoformat())}</span>'
            + (f'<span class="muted">{escape(v.author or "")}</span>' if authors else "")
            + (f'<span class="ds-mark" title="In use" aria-label="In use">{_icon("check")}</span>'
               if v.label == ds.version else "")
            + ('<span class="ds-mark" title="Building" aria-label="Building"><span class="ds-spin"></span></span>'
               if v.status == "building" else "")
            + "</button>"
            for v in mine
        )
        # One group per upload, set apart by a rule — no heading (named for
        # screen readers only); an upload that can go shows just a trash icon,
        # in its own strip beside the group's rows.
        groups.append(f'<div class="ds-major" role="group" aria-label="Upload {major}">{delete}{items}</div>')
    label = f"v{ds.version}" if ds.version else '<span class="ds-spin" title="Building"></span>'
    return (
        '<details class="ds-history" id="ds-history">'
        f'<summary title="Versions">{label}</summary>'
        f'<div class="ds-history-menu">{"".join(groups)}</div></details>'
    )


def _build_status(ds: Dataset) -> str:
    """One line while a version builds, or when the last one failed."""
    if ds.building:
        return (f'<div class="ds-status is-building" id="ds-status" title="Building {ds.building}">'
                f'<span class="ds-spin"></span>{ds.building}</div>')
    if ds.error:
        return f'<div class="ds-status is-error" id="ds-status">{escape(ds.error)}</div>'
    return '<div class="ds-status" id="ds-status" hidden></div>'


def _page_data(ds: Dataset, mapping: str, versions: list[Version], examples: dict[str, list[str]],
               used_in: dict[str, list[tuple[str, str]]]) -> str:
    """What dataset-columns.js works from. `</` is escaped so no value can end
    the <script> it sits in."""
    rules = _mapping_rules(ds, mapping, versions)
    data = {
        "name": ds.name,
        "mapping": mapping,
        "rules": [{"name": r.name, "formula": r.formula, "service": r.service} for r in rules],
        "serviceIcon": _icon("service"),
        "examples": examples,
        "usedIn": used_in,
        # Column name -> [icon, kind] from the built data (a column not built
        # yet — an unsaved edit — has none).
        "kinds": _kinds(ds),
        "version": ds.version,
        "building": ds.building,
        "versions": [
            {"version": v.label, "created_at": v.created_at, "author": v.author, "status": v.status}
            for v in versions
        ],
    }
    return json.dumps(data).replace("</", "<\\/")
