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

from fireflyer.dashboard import Dashboard
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
  .topbar { height:44px; display:flex; align-items:center; padding:0 16px;
    background:var(--panel); border-bottom:1px solid var(--border); }
  .topbar .brand { font-weight:600; font-size:14px; }
  .gallery { max-width:960px; margin:0 auto; padding:28px 20px; }
  .gallery-head { display:flex; align-items:center; margin-bottom:18px; }
  .gallery-head h2 { font-size:18px; margin:0; }
  .btn { cursor:pointer; font-size:14px; border-radius:4px; }
  .btn-primary { margin-left:auto; background:var(--accent); color:#fff; border:0;
    padding:8px 14px; font-weight:500; }
  .btn-primary:hover { background:var(--accent-hover); }
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
  .act { background:transparent; border:1px solid var(--border); color:var(--text);
    border-radius:4px; padding:4px 9px; font-size:13px; cursor:pointer;
    text-decoration:none; margin-left:6px; }
  .act:hover { border-color:var(--accent); color:var(--accent); }
  .act-danger:hover { border-color:var(--danger); color:var(--danger); }
  .empty { color:var(--muted); text-align:center; padding:44px 0;
    background:var(--panel); border:1px solid var(--border); border-radius:8px; }
  dialog { border:1px solid var(--border); border-radius:10px; padding:0;
    background:var(--panel); color:var(--text); width:340px; }
  dialog::backdrop { background:rgba(0,0,0,.4); }
  dialog form { padding:22px; margin:0; }
  dialog h3 { margin:0 0 14px; font-size:16px; }
  dialog input { width:100%; padding:8px 10px; border:1px solid var(--border);
    border-radius:4px; background:var(--bg); color:var(--text); font-size:14px; }
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
        f'<a class="act" href="/d/{row.id}">Edit</a>'
        f'<button class="act" type="button" data-id="{row.id}"'
        f' data-name="{escape(row.name, quote=True)}" onclick="openClone(this)">Clone</button>'
        f'<form method="post" action="/d/{row.id}/delete"'
        " onsubmit=\"return confirm('Remove this dashboard?')\">"
        '<button class="act act-danger" type="submit">Remove</button></form>'
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


def render_gallery(
    rows: list[DashboardRow], title: str = "Fireflyer Portal", user_menu: str = ""
) -> str:
    if rows:
        body = (
            '<table class="dash-table"><thead><tr>'
            "<th>Name</th><th>Author</th><th>Last updated</th><th></th>"
            "</tr></thead><tbody>"
            + "".join(_row(r) for r in rows)
            + "</tbody></table>"
        )
    else:
        body = '<div class="empty">No dashboards yet. Create one with + New dashboard.</div>'
    # `user_menu` (username + logout) is right-aligned in the flex topbar.
    menu = f'<span style="margin-left:auto">{user_menu}</span>' if user_menu else ""
    add_dialog = _dialog("add-dialog", "add-form", "/new", "New dashboard", "Create")
    # Clone dialog id must match the input id JS targets (clone-name); build it
    # with a fixed input id rather than the "{form_id}-name" convention.
    clone_dialog = _dialog("clone-dialog", "clone-form", "", "Clone dashboard", "Clone")
    clone_dialog = clone_dialog.replace('id="clone-form-name"', 'id="clone-name"')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>{_GALLERY_CSS}{auth_mod.PROFILE_CSS}</style>
</head>
<body>
<header class="topbar"><span class="brand">{escape(title)}</span>{menu}</header>
<main class="gallery">
  <div class="gallery-head">
    <h2>Dashboards</h2>
    <button class="btn btn-primary" onclick="openAdd()">+ New dashboard</button>
  </div>
  {body}
</main>
{add_dialog}
{clone_dialog}
{_GALLERY_JS}
</body>
</html>"""
