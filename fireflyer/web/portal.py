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
from fireflyer.web import auth as auth_mod

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
# Editor chrome, not chart output, so the same escaped-f-string style as
# app.py's INDEX is fine here (the no-f-string rule is for chart HTML). User
# input — dashboard names — is escape()'d; ids are UUIDs and timestamps are
# machine-generated, so they're safe.

_GALLERY_CSS = """
  * { box-sizing: border-box; }
  :root { color-scheme: light; --bg:#f5f6f8; --panel:#fff; --border:#e0e0e0;
    --text:#20242b; --muted:#5e6975; --accent:#20a7c9; --accent-hover:#1a8aa6;
    --danger:#e04355; }
  @media (prefers-color-scheme: dark) { :root { color-scheme: dark;
    --bg:#0f1620; --panel:#1b2635; --border:#2c384a; --text:#e6e8ec;
    --muted:#a3adbd; --accent:#20a7c9; --accent-hover:#48c4e0; --danger:#e5646f; } }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font-family:-apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; }
  .topbar { height:54px; display:flex; align-items:center; padding:0 16px;
    background:var(--panel); border-bottom:1px solid var(--border); }
  .gallery { max-width:960px; margin:0 auto; padding:28px 20px; }
  /* Right-side topbar group: the detail action group + profile. */
  .topbar-right { margin-left:auto; display:inline-flex; align-items:center; gap:14px; }
  .detail-actions { display:inline-flex; gap:4px; }   /* icons close together */
  .detail-actions .act { margin-left:0; }
  /* Detail topbar lead: back button (+ switch/path dropdown in local mode) +
     the name block, evenly spaced. */
  .topbar-lead { display:inline-flex; align-items:center; gap:14px; }
  /* Detail context in the topbar: name + small description under it. */
  .topbar-title { display:flex; flex-direction:column; line-height:1.2; }
  .topbar-title .tname { font-weight:600; font-size:15px; }
  .topbar-title .tdesc { font-size:12px; color:var(--muted); }
  /* Column type icon: a small monospaced glyph badge. */
  .type-icon { display:inline-flex; align-items:center; justify-content:center;
    width:18px; height:18px; border-radius:4px; font-size:11px; font-weight:700;
    font-family:ui-monospace,Menlo,monospace; background:var(--bg);
    border:1px solid var(--border); color:var(--muted); margin-right:8px; }
  .preview-wrap { overflow:auto; border:1px solid var(--border); border-radius:8px; }
  table.preview { border-collapse:collapse; width:100%; font-size:13px; }
  .preview th, .preview td { padding:7px 12px; border-bottom:1px solid var(--border);
    text-align:left; white-space:nowrap; }
  .preview thead th { color:var(--muted); font-weight:600; background:var(--panel);
    position:sticky; top:0; }
  table.dash-table { width:100%; border-collapse:collapse; background:var(--panel);
    border:1px solid var(--border); border-radius:8px; overflow:hidden; }
  .dash-table th, .dash-table td { text-align:left; padding:11px 14px;
    border-bottom:1px solid var(--border); font-size:14px; }
  .dash-table thead th { font-size:12px; color:var(--muted); font-weight:600;
    text-transform:uppercase; letter-spacing:.03em; }
  .dash-table tbody tr:last-child td { border-bottom:0; }
  .dash-table tbody tr:hover { background:var(--bg); }
  .dash-table a.name { color:var(--text); font-weight:600; text-decoration:none; }
  .dash-table a.name:hover { color:var(--accent); }
  .dash-table td.muted { color:var(--muted); }
  td.actions { text-align:right; white-space:nowrap; }
  td.actions form { display:inline; margin:0; }
  /* Actions are icon buttons — minimal text; meaning lives in the title tooltip. */
  .act { display:inline-flex; align-items:center; justify-content:center; gap:4px;
    background:transparent; border:1px solid var(--border); color:var(--text);
    border-radius:4px; padding:6px 7px; font-size:12px; cursor:pointer;
    text-decoration:none; margin-left:6px; }
  .act svg { width:15px; height:15px; display:block; }
  .act:hover { border-color:var(--accent); color:var(--accent); }
  .act-danger:hover { border-color:var(--danger); color:var(--danger); }
  .act .badge { font-weight:600; }
  .link-list { list-style:none; padding:0; margin:0; }
  .link-list li { border-bottom:1px solid var(--border); }
  .link-list li:last-child { border-bottom:0; }
  .link-list a { display:block; padding:9px 4px; color:var(--text);
    text-decoration:none; font-size:14px; }
  .link-list a:hover { color:var(--accent); }
  .empty { color:var(--muted); text-align:center; padding:44px 0;
    background:var(--panel); border:1px solid var(--border); border-radius:8px; }
  dialog { border:1px solid var(--border); border-radius:10px; padding:0;
    background:var(--panel); color:var(--text); width:340px; }
  dialog::backdrop { background:rgba(0,0,0,.4); }
  dialog form { padding:22px; margin:0; }
  dialog h3 { margin:0 0 14px; font-size:16px; }
  dialog input, dialog textarea, dialog select { width:100%; padding:8px 10px;
    border:1px solid var(--border); border-radius:4px; background:var(--bg);
    color:var(--text); font-size:14px; font-family:inherit; }
  dialog.wide { width:420px; }
  dialog label { display:block; font-size:12px; color:var(--muted); margin:12px 0 4px; }
  dialog textarea { resize:vertical; min-height:52px; }
  .dialog-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:18px; }
  .dialog-actions .cancel { background:transparent; border:1px solid var(--border);
    color:var(--text); padding:7px 13px; }
  .dialog-actions .ok { background:var(--accent); border:0; color:#fff;
    padding:7px 15px; font-weight:500; }
"""

# Minimal vanilla JS — the portal gallery is editor chrome (dev tool), exempt
# from the no-JS rule. Native <dialog>; clone reuses one dialog, its form action
# and default name set from the clicked row's data-* attributes.
_GALLERY_JS = """
<script>
function openAdd(){ document.getElementById('add-dialog').showModal(); }
function openClone(btn){
  var d = document.getElementById('clone-dialog');
  document.getElementById('clone-form').action = '/d/' + btn.dataset.id + '/clone';
  var input = document.getElementById('clone-name');
  input.value = btn.dataset.name + ' (copy)';
  d.showModal(); input.select();
}
</script>"""


# Inline-SVG action icons (stroke=currentColor so they follow the button colour).
_ICONS = {
    "pencil": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
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


# Topbar nav CSS shared by the gallery `_shell` and the editor INDEX (which have
# separate <style> blocks — no shared base stylesheet), so the Dashboards |
# Datasets switch and the path dropdown look identical in both.
NAV_CSS = """
  /* Back button — one shared style for the dataset detail and the editor. */
  .ff-back { display:inline-flex; align-items:center; justify-content:center;
    background:transparent; border:1px solid var(--border); color:var(--text);
    border-radius:4px; padding:6px 7px; text-decoration:none; }
  .ff-back svg { width:15px; height:15px; display:block; }
  .ff-back:hover { border-color:var(--accent); color:var(--accent); }
  /* Left-nav group: switch + path dropdown share one gap. */
  /* Dashboards | Datasets segmented switch. */
  .ff-switch { display:inline-flex; border:1px solid var(--border);
    border-radius:7px; overflow:hidden; }
  .ff-switch-seg { padding:6px 14px; font-size:13px; color:var(--muted);
    text-decoration:none; }
  .ff-switch-seg + .ff-switch-seg { border-left:1px solid var(--border); }
  .ff-switch-seg:hover { background:var(--bg); color:var(--text); }
  .ff-switch-seg.active { background:var(--accent); color:#fff; }
  /* Path dropdown: a labelled <details> showing the active path. */
  .ff-pathdd { position:relative; }
  .ff-pathdd summary { list-style:none; cursor:pointer; display:inline-flex;
    align-items:center; gap:5px; padding:5px 10px; border:1px solid var(--border);
    border-radius:7px; font-size:13px; color:var(--text); }
  .ff-pathdd summary::-webkit-details-marker { display:none; }
  .ff-pathdd summary svg { width:11px; height:11px; opacity:.7; }
  .ff-pathdd summary:hover, .ff-pathdd[open] summary { background:var(--bg); }
  .ff-pathdd-menu { position:absolute; right:0; top:calc(100% + 6px);
    min-width:160px; background:var(--panel); border:1px solid var(--border);
    border-radius:6px; box-shadow:0 8px 24px rgba(0,0,0,.18); padding:6px; z-index:30; }
  .ff-pathdd-menu a { display:block; padding:8px 10px; border-radius:4px;
    color:var(--text); text-decoration:none; font-size:14px; }
  .ff-pathdd-menu a:hover { background:var(--bg); }
  .ff-pathdd-menu a.active { color:var(--accent); font-weight:600; }
"""

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
<style>{_GALLERY_CSS}{NAV_CSS}{auth_mod.PROFILE_CSS}</style>
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
        extra=add_dialog + clone_dialog + _GALLERY_JS,
        topbar_right=f'<button class="act" type="button" title="New dashboard"'
                     f' onclick="openAdd()">{_icon("plus")}</button>',
        paths=paths, active_path=active_path,
    )


# --- datasets gallery -------------------------------------------------------

_DATASETS_JS = """
<script>
function openUpload(){ document.getElementById('upload-dialog').showModal(); }
function openDsRename(btn){
  var f = document.getElementById('ds-rename-form');
  f.action = '/datasets/' + encodeURIComponent(btn.dataset.name) + '/rename';
  var i = document.getElementById('ds-rename-name');
  i.value = btn.dataset.name;
  document.getElementById('ds-rename-desc').value = btn.dataset.desc || '';
  document.getElementById('ds-rename-dialog').showModal(); i.select();
}
</script>"""


def _type_icon(dtype: str) -> str:
    """A small badge glyph for a Parquet column type."""
    d = dtype.lower()
    if any(x in d for x in ("int", "float", "decimal")):
        glyph, name = "#", "number"
    elif "bool" in d:
        glyph, name = "✓", "boolean"
    elif any(x in d for x in ("date", "time")):
        glyph, name = "◷", "date / time"
    elif "str" in d or "utf" in d:
        glyph, name = "T", "text"
    else:
        glyph, name = "?", dtype
    return f'<span class="type-icon" title="{escape(name)}">{glyph}</span>'


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
        + _DATASETS_JS
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
        extra=replace + _rename_dialog() + usage_dialog + _DATASETS_JS,
        topbar_left=topbar_left, topbar_right=topbar_right,
        paths=paths, active_path=active_path, detail=True,
    )
