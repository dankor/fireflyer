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

import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

import yaml

from fireflyer.dashboard import Dashboard
from fireflyer.datasets import Dataset
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


def _name_of(yaml_text: str) -> str:
    """The dashboard's display name, from the YAML's required top-level `name:`
    key. Also validates: `from_yaml` raises DashboardError on an invalid or
    nameless dashboard. Parsing only — never opens the CSV, so a missing dataset
    path is fine."""
    return Dashboard.from_yaml(yaml_text).name


class SqliteStore(_SqlVersions):
    """Local/test store. `:memory:` for tests, a file path for local portal dev."""

    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False: uvicorn serves requests off a threadpool.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_VERSIONS_SCHEMA)
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

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = ?", (id,))
        self._conn.execute("DELETE FROM dashboard_versions WHERE dashboard_id = ?", (id,))
        self._conn.commit()


class PostgresStore(_SqlVersions):
    """Runtime store. Same schema, `%s` placeholders; imports psycopg lazily so
    this module stays importable without the `.[portal]` extra installed."""

    def __init__(self, dsn: str):
        import psycopg  # optional dependency, only needed at portal runtime

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_VERSIONS_SCHEMA)

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

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = %s", (id,))
        self._conn.execute("DELETE FROM dashboard_versions WHERE dashboard_id = %s", (id,))


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
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.6v.1"/>',
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
}


def _icon(name: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>'
    )


def info_tip(text: str, el_id: str = "") -> str:
    """An ⓘ beside a name that shows `text` — a dashboard's or dataset's
    description — as a tooltip on hover or keyboard focus. Styled in nav.css,
    which the gallery and the editor both link. "" without text; with `el_id`
    it is rendered anyway, hidden, for the editor's script to fill in when the
    YAML gains a description."""
    if not text and not el_id:
        return ""
    attr_id = f' id="{escape(el_id, quote=True)}"' if el_id else ""
    tip = escape(text, quote=True)
    return (
        f'<span class="ff-info"{attr_id} tabindex="0" role="img" aria-label="{tip}"'
        f' data-tip="{tip}"{"" if text else " hidden"}>{_icon("info")}</span>'
    )


def _stamp(iso: str) -> str:
    """A stored UTC time as `<time>`: `2026-10-16 14:52:07 UTC` as written, which
    gallery.js turns into the viewer's local time in the same shape."""
    text = iso.replace("T", " ")[:19]
    return f'<time datetime="{escape(iso)}">{escape(text)} UTC</time>' if text else ""


def _row(row: DashboardRow) -> str:
    updated = _stamp(row.updated_at)
    author = escape(row.author) if row.author else "—"
    name = escape(row.name)
    return (
        "<tr>"
        f'<td><a class="name" href="/d/{row.id}">{name}</a>{info_tip(row.description)}</td>'
        f"<td>{author}</td>"
        f'<td class="muted">{updated}</td>'
        '<td class="actions">'
        f'<a class="act" href="/d/{row.id}" title="Edit">{_icon("pencil")}</a>'
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
) -> str:
    """The gallery page frame. Overview pages (dashboards/datasets lists) lead
    with a Dashboards | Datasets switch on the left; in local paths mode the path
    dropdown sits on the **right**. Detail pages (a selected dataset) lead with a
    back button (+ switch, local) in `topbar_left`. Then optional `topbar_right`,
    then `body`. No second nav bar."""
    nav = "" if detail else nav_switch(active)
    right = path_dropdown(paths, active_path) + topbar_right + user_menu
    right_html = f'<span class="topbar-right">{right}</span>' if right else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
{assets.favicons()}
{assets.stylesheets("gallery.css", "nav.css", "profile.css")}
{assets.script("gallery.js")}
</head>
<body>
<header class="topbar">{nav}{topbar_left}{right_html}</header>
<main class="gallery">{body}</main>
{extra}
</body>
</html>"""


def render_gallery(
    rows: list[DashboardRow], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None,
) -> str:
    if rows:
        body_table = (
            '<table class="dash-table"><thead><tr>'
            "<th>Name</th><th>Author</th><th>Last updated</th><th></th>"
            "</tr></thead><tbody>"
            + "".join(_row(r) for r in rows)
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
    name_row = (
        '<label>Name</label><input name="name" autocomplete="off" required>'
        if name_field else ""
    )
    return f"""
<dialog class="wide" id="{dialog_id}">
  <form method="post" action="{action}" enctype="multipart/form-data">
    <h3>{heading}</h3>
    {name_row}
    <label>Description</label><textarea name="description"></textarea>
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


def _dataset_row(ds: Dataset) -> str:
    href = f"/datasets/{quote(ds.name)}"
    updated = _stamp(ds.updated_at)
    author = escape(ds.author) if ds.author else "—"
    return (
        "<tr>"
        f'<td><a class="name" href="{href}">{escape(ds.name)}</a>{info_tip(ds.description)}</td>'
        f'<td class="muted">{len(ds.columns)}</td>'
        f'<td class="muted">{ds.rows}</td>'
        f'<td class="muted">{updated}</td>'
        f"<td>{author}</td>"
        '<td class="actions">'
        f'<a class="act" href="{href}" title="View">{_icon("eye")}</a>'
        f'<button class="act" type="button" title="Edit" data-name="{escape(ds.name, quote=True)}"'
        f' data-desc="{escape(ds.description, quote=True)}"'
        f' onclick="openDsRename(this)">{_icon("pencil")}</button>'
        f'<form method="post" action="/datasets/{quote(ds.name)}/delete"'
        " onsubmit=\"return confirm('Remove this dataset?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        "</td></tr>"
    )


def _rename_dialog() -> str:
    return """
<dialog id="ds-rename-dialog">
  <form id="ds-rename-form" method="post">
    <h3>Edit dataset</h3>
    <label>Name</label>
    <input name="name" id="ds-rename-name" autocomplete="off" required>
    <label>Description</label>
    <textarea name="description" id="ds-rename-desc"></textarea>
    <div class="dialog-actions">
      <button type="button" class="cancel" onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">Save</button>
    </div>
  </form>
</dialog>"""


def render_datasets(
    datasets: list[Dataset], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None,
) -> str:
    if datasets:
        table = (
            '<table class="dash-table"><thead><tr>'
            "<th>Name</th><th>Columns</th><th>Rows</th><th>Updated</th><th>Author</th><th></th>"
            "</tr></thead><tbody>"
            + "".join(_dataset_row(d) for d in datasets)
            + "</tbody></table>"
        )
    else:
        table = '<div class="empty">No datasets yet. Use + to upload a CSV.</div>'
    extra = (
        _upload_dialog("/datasets/new", "New dataset", "Upload", "upload-dialog", True)
        + _rename_dialog()
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
    preview_cols: list[str],
    preview_rows: list[list],
    title: str = "Fireflyer Portal",
    user_menu: str = "",
    used_by: list[tuple[str, str]] | None = None,
    paths: list[str] | None = None,
    active_path: str | None = None,
) -> str:
    # Preview header carries each column's type icon and its exact dtype as a
    # tooltip — so there's no need for a separate column-list card.
    dtypes = {c.name: c.dtype for c in ds.columns}
    head = "".join(
        f'<th title="{escape(dtypes.get(c, ""), quote=True)}">'
        f"{_type_icon(dtypes.get(c, ''))}{escape(c)}</th>"
        for c in preview_cols
    )
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{escape('' if v is None else str(v))}</td>" for v in r) + "</tr>"
        for r in preview_rows
    )
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
    # A selected item leads with just the back button + name — no Dashboards |
    # Datasets switch (that's for the lists). The path dropdown still rides on the
    # right in local paths mode (added by `_shell`).
    topbar_left = (
        '<div class="topbar-lead">'
        + back_button("/datasets", "Back to datasets")
        + f'<div class="topbar-title" id="ds-title">{tname}{tabout}</div>'
        + "</div>"
    )
    topbar_right = (
        '<span class="detail-actions">'
        '<button class="act" title="Replace data"'
        f" onclick=\"document.getElementById('replace-dialog').showModal()\">{_icon('upload')}</button>"
        f"{trash}</span>"
    )
    body = f"""
  <div class="preview-wrap"><table class="preview"><thead><tr>{head}</tr></thead>
  <tbody>{body_rows}</tbody></table></div>"""
    replace = _upload_dialog(
        f"/datasets/{quote(ds.name)}/replace", "Replace data", "Upload", "replace-dialog", False
    )
    return _shell(
        title, user_menu, "datasets", body,
        extra=replace + usage_dialog,
        topbar_left=topbar_left, topbar_right=topbar_right,
        paths=paths, active_path=active_path, detail=True,
    )
