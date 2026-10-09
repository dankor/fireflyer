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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

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


@dataclass
class DashboardRow:
    id: str
    name: str
    author: str
    yaml: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _name_of(yaml_text: str) -> str:
    """The dashboard's display name, from the YAML's required top-level `name:`
    key. Also validates: `from_yaml` raises DashboardError on an invalid or
    nameless dashboard. Parsing only — never opens the CSV, so a missing dataset
    path is fine."""
    return Dashboard.from_yaml(yaml_text).name


class SqliteStore:
    """Local/test store. `:memory:` for tests, a file path for local portal dev."""

    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False: uvicorn serves requests off a threadpool.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

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
        return new_id

    def save(self, id: str, yaml: str) -> None:
        # Name is re-derived from the YAML; author (the creator) is left as-is.
        name = _name_of(yaml)
        self._conn.execute(
            "UPDATE dashboards SET name = ?, yaml = ?, updated_at = ? WHERE id = ?",
            (name, yaml, _now(), id),
        )
        self._conn.commit()

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = ?", (id,))
        self._conn.commit()


class PostgresStore:
    """Runtime store. Same schema, `%s` placeholders; imports psycopg lazily so
    this module stays importable without the `.[portal]` extra installed."""

    def __init__(self, dsn: str):
        import psycopg  # optional dependency, only needed at portal runtime

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute(_SCHEMA)

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
        return new_id

    def save(self, id: str, yaml: str) -> None:
        # Name is re-derived from the YAML; author (the creator) is left as-is.
        name = _name_of(yaml)
        self._conn.execute(
            "UPDATE dashboards SET name = %s, yaml = %s, updated_at = %s WHERE id = %s",
            (name, yaml, _now(), id),
        )

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = %s", (id,))


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
}


def _icon(name: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>'
    )


def _row(row: DashboardRow) -> str:
    updated = escape(row.updated_at.replace("T", " ")[:16])
    author = escape(row.author) if row.author else "—"
    name = escape(row.name)
    return (
        "<tr>"
        f'<td><a class="name" href="/d/{row.id}">{name}</a></td>'
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


def _dialog(dialog_id: str, form_id: str, action: str, heading: str, ok_label: str) -> str:
    # `action` is empty for the clone dialog — set by JS from the clicked row.
    return f"""
<dialog id="{dialog_id}">
  <form id="{form_id}" method="post" action="{action}">
    <h3>{heading}</h3>
    <input name="name" id="{form_id}-name" placeholder="Dashboard name"
           autocomplete="off" required>
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
    add_dialog = _dialog("add-dialog", "add-form", "/new", "New dashboard", "Create")
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
    updated = escape(ds.updated_at.replace("T", " ")[:16])
    author = escape(ds.author) if ds.author else "—"
    return (
        "<tr>"
        f'<td><a class="name" href="{href}">{escape(ds.name)}</a></td>'
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

    # Header (back + name/description + actions) lives in the topbar.
    tname = f'<span class="tname">{escape(ds.name)}</span>'
    tdesc = f'<span class="tdesc">{escape(ds.description)}</span>' if ds.description else ""
    # A selected item leads with just the back button + name — no Dashboards |
    # Datasets switch (that's for the lists). The path dropdown still rides on the
    # right in local paths mode (added by `_shell`).
    topbar_left = (
        '<div class="topbar-lead">'
        + back_button("/datasets", "Back to datasets")
        + f'<div class="topbar-title">{tname}{tdesc}</div>'
        + "</div>"
    )
    topbar_right = (
        '<span class="detail-actions">'
        f'<button class="act" type="button" title="Edit" data-name="{escape(ds.name, quote=True)}"'
        f' data-desc="{escape(ds.description, quote=True)}"'
        f' onclick="openDsRename(this)">{_icon("pencil")}</button>'
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
        extra=replace + _rename_dialog() + usage_dialog,
        topbar_left=topbar_left, topbar_right=topbar_right,
        paths=paths, active_path=active_path, detail=True,
    )
