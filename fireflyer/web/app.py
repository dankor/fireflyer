import dataclasses
import hmac
import json
import os
import pathlib
import re
import threading
import time
import traceback
from html import escape, unescape

import yaml
from dotenv import load_dotenv
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from fireflyer import config_edit
from fireflyer import date_range
from fireflyer import number_range
from fireflyer import yaml_assist
from fireflyer import filters as filters_mod
from fireflyer import calcs_edit
from fireflyer import column_map
from fireflyer import connections as connections_mod
from fireflyer import groups
from fireflyer.scan import NoAccess
from fireflyer.datasets import DatasetError, DatasetStore, SqlCatalog, fit_name
from fireflyer.params import filter_fields as builder_fields, parse_filters
from fireflyer.storage import make_object_store
from fireflyer.chart.map.chart import Map
from fireflyer.chart.table.chart import Table
from fastapi.responses import RedirectResponse
from fireflyer.dashboard import (
    Dashboard,
    DashboardError,
    filter_add_body,
    grain_state_html,
    qualify_refs,
    rename_column_refs,
    rename_dataset_ref,
    rename_group_ref,
    set_grain_token,
)
from fireflyer.web import assets
from fireflyer.web import auth as auth_mod
from fireflyer.web import chat as chat_mod
from fireflyer.web import portal as portal_mod
from fireflyer.web import paths as paths_mod
from fireflyer.web import usage as usage_mod

# Load .env (ANTHROPIC_API_KEY) before reading it. The AI assistant is enabled
# only when a key is present; otherwise the editor shows a setup notice.
load_dotenv()
CHAT_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))

# Portal mode (owner-approved exception to the no-persistence anti-goal, scoped
# to web/): when FIREFLYER_PORTAL is set, `/` becomes a gallery of dashboards
# stored in Postgres (`DATABASE_URL` — it won't start without one) and each
# opens in the existing editor. Off by default — `/` is the usual
# single-dashboard editor. Tests set `app.state.store` directly.
PORTAL_ENABLED = bool(os.environ.get("FIREFLYER_PORTAL"))
PORTAL_TITLE = os.environ.get("FIREFLYER_PORTAL_TITLE", "Fireflyer Portal")
_DATABASE_URL = portal_mod.database_url() if PORTAL_ENABLED else ""

app = FastAPI()
app.mount("/static", StaticFiles(directory=assets.STATIC_DIR), name="static")
app.state.store = portal_mod.SqlStore(_DATABASE_URL) if PORTAL_ENABLED else None
# Portal mode is gated behind a login. `authenticator` is the swappable
# credential check (default: admin/admin); None disables auth entirely (local
# mode). Tests set both `store` and `authenticator` directly. In portal mode the
# users with a password (Users tab) sign in too, after the env login.


class _PortalUsers:
    """The Users tab's people as an Authenticator: their name and password,
    checked against the stored hash (`RoleStore.verify`)."""

    def verify(self, username: str, password: str) -> str | None:
        return _role_store().verify(username, password)


app.state.authenticator = (
    auth_mod.AnyOf(auth_mod.default_authenticator(), _PortalUsers()) if PORTAL_ENABLED else None
)

# Datasets are named entities (unique name -> Parquet in object storage),
# referenced by name from dashboard YAML in both modes. With FIREFLYER_S3_ENDPOINT
# set (portal runtime) they live in Garage/S3; otherwise in a local folder
# (FIREFLYER_DATA, default ./data/datasets). Tests set `app.state.datasets`.
def _object_store_config() -> dict:
    endpoint = os.environ.get("FIREFLYER_S3_ENDPOINT")
    if endpoint:
        return {
            "endpoint_url": endpoint,
            "bucket": os.environ.get("FIREFLYER_S3_BUCKET", "fireflyer"),
            "access_key": os.environ.get("FIREFLYER_S3_ACCESS_KEY", ""),
            "secret_key": os.environ.get("FIREFLYER_S3_SECRET_KEY", ""),
            "region": os.environ.get("FIREFLYER_S3_REGION", "garage"),
        }
    return {"base": os.environ.get("FIREFLYER_DATA", "data/datasets")}


# Each dataset's YAML (details, versions, column mappings) lives in the portal's
# database in portal mode, and as a file beside its data locally.
app.state.datasets = DatasetStore(
    make_object_store(_object_store_config()),
    SqlCatalog(_DATABASE_URL) if PORTAL_ENABLED else None,
)




@app.middleware("http")
async def _require_login(request: Request, call_next):
    """When auth is on, every route except the login page requires a session;
    unauthenticated requests are redirected to /login. Static assets are open
    too — the login page needs its stylesheet and icon, and they hold no data —
    and so is `/auth/`: where another system's sign-in routes go (an SSO
    provider's redirect and callback), which run before there's a session and
    end in `sign_in`."""
    auth = app.state.authenticator
    if (
        auth is not None
        and request.url.path != "/login"
        and not request.url.path.startswith(("/static/", "/auth/"))
        and not _session_valid(auth_mod.session(request))
    ):
        resp = RedirectResponse("/login", status_code=303)
        auth_mod.clear_session(resp)
        return resp
    return await call_next(request)


@app.middleware("http")
async def _hsts(request: Request, call_next):
    """With a team's own certificate (`fireflyer.portal` sets FIREFLYER_HSTS),
    browsers are told to use HTTPS only, for a year. Never for the local
    self-signed one — HSTS on localhost would follow the browser to every other
    local app."""
    response = await call_next(request)
    if os.environ.get("FIREFLYER_HSTS") == "1" and request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


# --- activity log ------------------------------------------------------------------
# Portal mode keeps a row per action in its database (`portal.ActivityLog`):
# the security audit trail, and later a usage dashboard's data. A route says
# what it did with `_log`; `_activity` writes it once the response is made, so
# the row carries the real status code — a refusal or a crash is logged too.
# Local and paths modes log nothing. Tests set `app.state.activity`.
app.state.activity = (
    portal_mod.ActivityLog(
        _DATABASE_URL, keep_days=int(os.environ.get("FIREFLYER_ACTIVITY_DAYS", "365")),
    ) if PORTAL_ENABLED else None
)
# The editor's requests name their dashboard (editor.js): its routes take the
# YAML, not an id.
_DASHBOARD_HEADER = "X-FF-Dashboard"
# A dataset page's requests outside its address (its filter rows) name its group.
_GROUP_HEADER = "X-FF-Group"
# Form fields a row never keeps: secrets, and a dashboard's whole YAML (a save's
# version holds it).
_UNLOGGED = {"yaml_text", "password", "clear_password"}


def _log(request: Request, action: str, object_type: str = "", object_name: str = "",
         *, status_code: int | None = None, actor: str | None = None, group: str | None = None,
         **detail) -> None:
    """What this request did, for `_activity` to write. `status_code` overrides
    the response's — a JSON `{ok: false}` comes back 200 but failed; `actor` names
    who before there's a session (a login)."""
    if app.state.activity is None:
        return
    request.state.activity = (portal_mod.Activity(
        actor=(auth_mod.current_user(request) or "") if actor is None else actor,
        action=action, object_type=object_type, object_name=object_name,
        detail={k: v for k, v in detail.items() if v not in (None, "", [], {})},
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        group=_logged_group(request) if group is None else group,
    ), status_code)


def _logged_group(request: Request) -> str:
    """The group a logged request acted in: the URL's, or the one its header
    names — "" for what's global (roles, users, the login)."""
    if "group" in request.path_params:
        return groups.from_url(request.path_params["group"])
    sent = request.headers.get(_DASHBOARD_HEADER)
    if sent:
        return unquote(sent).partition("/")[0]
    return unquote(request.headers.get(_GROUP_HEADER, ""))


def _record(entry) -> None:
    """Never in the way: a log that can't be written loses its row, not the
    request."""
    try:
        app.state.activity.record(entry)
    except Exception:
        traceback.print_exc()


@app.middleware("http")
async def _activity(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as exc:
        _write_activity(request, 500, str(exc))
        raise
    error = ""
    if response.status_code >= 400 and getattr(request.state, "activity", None) is not None:
        # A refusal's answer is its reason ("Only an admin manages roles"):
        # read it to log it, then send the same bytes on.
        body = b"".join([chunk async for chunk in response.body_iterator])
        error = _answer_text(body, response.headers.get("content-type", ""))
        sent = response
        response = Response(body, sent.status_code)
        response.raw_headers = list(sent.raw_headers)    # every one, two cookies too
    await run_in_threadpool(_write_activity, request, response.status_code, error)
    return response


def _answer_text(body: bytes, content_type: str) -> str:
    """A failed answer's message, for the log: a JSON `error`, or a short text
    (`_error`'s `<pre>` read as its words) — never a whole page."""
    text = body.decode("utf-8", "replace").strip()
    if "json" in content_type:
        try:
            found = json.loads(text)
        except ValueError:
            return ""
        message = found.get("error") or found.get("detail") if isinstance(found, dict) else ""
        return message if isinstance(message, str) else ""
    if len(text) > 1000 or re.search(r"(?i)<(html|body|!doctype)\b", text):
        return ""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _write_activity(request: Request, status: int, error: str = "") -> None:
    logged = getattr(request.state, "activity", None)
    if logged is None or app.state.activity is None:
        return
    entry, status_code = logged
    entry.status_code = status_code if status_code is not None else status
    if error and entry.status_code >= 400 and "error" not in entry.detail:
        entry.detail["error"] = error
    _record(entry)


def _form_detail(form) -> dict:
    """A form's fields as a row's detail — a repeated one as its list."""
    return {k: (vs if len(vs) > 1 else vs[0])
            for k in dict.fromkeys(form.keys()) if k not in _UNLOGGED
            for vs in [form.getlist(k)]}


def _dash_of(request: Request, yaml_text: str = "") -> str:
    """Which dashboard an editor request is about: the one the page opened
    (`<group>/<name>`, sent by editor.js — the group is the row's own column),
    else the posted YAML's `name:`."""
    sent = request.headers.get(_DASHBOARD_HEADER)
    if sent:
        return unquote(sent).partition("/")[2]
    found = re.search(r"(?m)^name:[ \t]*(.*?)[ \t]*$", yaml_text)
    return found.group(1).strip("\"'") if found else ""


def _session_valid(signed: tuple[str, str] | None) -> bool:
    """A signed session still holds. A stored user's — local or from another
    system — carries their current key: setting or removing a password rotates
    it and deleting the user drops it, so their old sessions stop working.
    Only the env login's has no key."""
    if signed is None:
        return False
    identity, key = signed
    if not _users_on():
        return not key
    current = _role_store().session_key(identity)
    if current is not None:
        return bool(key) and hmac.compare_digest(key.encode(), current.encode())
    return not key and identity == auth_mod.admin_name()


# The starter dashboard is an ordinary dashboard file, not a string in here:
# 400+ lines of YAML that a reader edits like any other. docker-compose maps the
# same folder in as the `demo` path, so what you open in the browser is this
# file. Missing it is a broken checkout, so let the import fail and say so.
DEFAULT_YAML = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "demo/dashboards/orders-overview.yaml"
).read_text()

# Chat body differs by whether a key is configured: the live input, or a setup
# notice. Built as a plain string so its contents drop into the page verbatim.
if CHAT_ENABLED:
    CHAT_PANEL = """
      <div class="ff-docs-body chat-log" id="chat-log"></div>
      <form class="chat-input" id="chat-form">
        <button type="button" class="chat-new" id="chat-new" title="New conversation" aria-label="New conversation" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>
        <textarea id="chat-text" spellcheck="false" placeholder="Ask to add or change charts, add calcs, resize rows…"></textarea>
        <button type="submit" class="chat-send" id="chat-send">Send</button>
      </form>"""
else:
    CHAT_PANEL = """
      <div class="ff-docs-body"><div class="chat-notice">Set <code>ANTHROPIC_API_KEY</code> in <code>.env</code> and restart to enable the AI assistant.</div></div>"""

# Topbar pieces: the back button (to the dashboard's group), the name, and the
# save button/name-edit/theme, whose JS all lives in static/editor.js.


def _name_and_description(yaml_text: str) -> tuple[str, str]:
    dashboard = Dashboard.from_yaml(yaml_text)
    return dashboard.name, dashboard.description


def _dash_name_html(name: str, description: str = "") -> str:
    # Leads the left group (after the back button): the name, and under it the
    # description's first line, small and ellipsized. The two are one button —
    # a click opens a card beneath them (static/editor.js) with a field for
    # each, which writes the YAML's `name:` and `description:`; in view mode
    # the card only reads, and opens only when there's a description to read.
    # editor.js keeps all of it in step with the YAML.
    desc = escape(description, quote=True)
    hidden = "" if description else " hidden"
    return (
        '<span class="ff-dash-title">'
        '<span class="ff-dash-head" id="ff-dash-head" role="button" aria-haspopup="dialog">'
        f'<span class="ff-dash-name" id="ff-dash-name" data-description="{desc}">{escape(name)}</span>'
        f'<span class="ff-dash-sub" id="ff-dash-sub"{hidden}>{escape(description)}</span>'
        "</span>"
        '<span class="ff-dash-about" id="ff-dash-about" role="dialog" aria-label="Name and description" hidden>'
        '<input id="ff-dash-name-input" type="text" aria-label="Dashboard name" placeholder="Name"'
        ' spellcheck="false" autocomplete="off">'
        '<textarea id="ff-dash-about-text" rows="3" placeholder="Add a description"'
        ' aria-label="Dashboard description" spellcheck="true"></textarea>'
        "</span>"
        "</span>"
    )


# Discard (a red cross) and Save (a floppy disk), on the dashboard editor and a
# dataset's page; Save shows a check for a moment once saved.
_DISCARD_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>'
_SAVE_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 3h11l4 4v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M8 3v5h7V3"/><rect x="7" y="13" width="10" height="7" rx="1"/></svg>'


def _save_html(url: str) -> str:
    # Both hidden until there are unsaved changes (toggled by updateSaveState()):
    # Discard puts the last save back, Save keeps the changes.
    # One segmented group, styled like the mode switch's resting segments.
    return (
        '<span class="ff-savebar" role="group" aria-label="Unsaved changes">'
        '<button class="ff-discard" id="ff-discard" type="button" title="Discard unsaved changes"'
        f' aria-label="Discard unsaved changes" hidden>{_DISCARD_ICON}</button>'
        f'<button class="ff-save" id="ff-save" data-save-url="{escape(url, quote=True)}/save" title="Save (⌘/Ctrl+S)"'
        f' aria-label="Save" hidden>{_SAVE_ICON}</button>'
        "</span>"
    )


# Icon-only theme switch. Inline SVG (stroke=currentColor so it inherits the
# segment colour): "A" for auto, sun for light, moon for dark. No text labels —
# `title`/`aria-label` carry the meaning.
_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{}</svg>'


# The editor's mode switch: one segmented control where a row of separate
# toggles used to be. Exactly one mode is active, so the icon that is lit always
# says what the screen is showing. Icons only, per the project's UI style — the
# title/aria-label carry the meaning.
_MODE_LABELS = {
    "view": "View",
    "edit": "Edit — drag, add and rearrange charts",
    "code": "YAML",
    "diff": "Compare versions",
    "chat": "AI assistant",
    "docs": "Chart reference",
    "calcs": "Calcs",
}
# One <svg> shape, filled in per mode.
_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{}</svg>'

_MODE_ICONS = {
    # Inline SVG, `stroke=currentColor` so an icon follows its segment colour.
    "view": _ICON.format('<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>'),
    "edit": _ICON.format('<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
    "code": _ICON.format('<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>'),
    "diff": _ICON.format('<rect x="3" y="4" width="7" height="16" rx="1.5"/><rect x="14" y="4" width="7" height="16" rx="1.5"/><path d="M6 9h1M6 13h1M17 11h1M17 15h1"/>'),
    "chat": _ICON.format(
        '<rect x="4" y="9" width="16" height="11" rx="2"/>'      # head
        '<path d="M12 4.5V9"/><circle cx="12" cy="3.5" r="1.2"/>'  # antenna
        '<path d="M9 13.5v1.5"/><path d="M15 13.5v1.5"/>'          # eyes
        '<path d="M2 13.5v2"/><path d="M22 13.5v2"/>'              # ears
    ),
    "docs": _ICON.format('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'),
    "calcs": _ICON.format('<path d="M18 5H6l6 7-6 7h12"/>'),
}


def _mode_switch(view_only: bool = False) -> str:
    """Every mode — or, for someone who may only look, just view."""
    modes = ("view",) if view_only else ("view", "edit", "code", "diff", "chat", "calcs", "docs")
    buttons = "".join(
        f'<button type="button" data-mode="{mode}" title="{_MODE_LABELS[mode]}"'
        f' aria-label="{_MODE_LABELS[mode]}">{_MODE_ICONS[mode]}</button>'
        for mode in modes
    )
    return (
        '<div class="ff-theme ff-modes" id="ff-modes" role="group" '
        f'aria-label="Editor mode">{buttons}</div>'
    )


# The Auto / Light / Dark switch, in every page's topbar (portal.theme_switch).
_theme_switch = portal_mod.theme_switch


# --- chart documentation (editor-only) --------------------------------------
# The docs overlay lists each chart type and renders its spec.md, so the chart
# reference is one click away from the editor. Read once at import.
_CHART_DOC_ORDER = ("table", "pie", "bar", "map", "number")


def _render_spec_md(text: str) -> str:
    """Tiny markdown → HTML for the chart spec docs — handles the constructs the
    spec.md files use: #/## headings, `- ` bullets (one nesting level), **bold**,
    `code`, and paragraphs. Not a general markdown parser."""
    def inline(s: str) -> str:
        s = escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)

    out: list[str] = []
    para: list[str] = []
    depth = 0

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def set_depth(d: int) -> None:
        nonlocal depth
        while depth < d:
            out.append("<ul>"); depth += 1
        while depth > d:
            out.append("</ul>"); depth -= 1

    for line in text.splitlines():
        stripped = line.strip()
        head = re.match(r"(#{1,6})\s+(.*)", stripped)
        bullet = re.match(r"(\s*)-\s+(.*)", line)
        if not stripped:
            flush_para(); set_depth(0)
        elif head:
            flush_para(); set_depth(0)
            tag = {1: "h4", 2: "h5"}.get(len(head.group(1)), "h6")
            out.append(f"<{tag}>{inline(head.group(2))}</{tag}>")
        elif bullet:
            flush_para()
            set_depth(2 if len(bullet.group(1)) >= 2 else 1)
            out.append(f"<li>{inline(bullet.group(2))}</li>")
        else:
            set_depth(0); para.append(stripped)
    flush_para(); set_depth(0)
    return "".join(out)


def _build_chart_docs() -> str:
    """Render every chart's spec.md into the docs overlay (a `<details>` each)."""
    import pathlib

    base = pathlib.Path(__file__).resolve().parent.parent / "chart"
    parts = []
    for name in _CHART_DOC_ORDER:
        spec = base / name / "spec.md"
        if spec.exists():
            parts.append(
                '<details class="ff-docs-chart">'
                f"<summary>{escape(name)}</summary>"
                f'<div class="ff-docs-md">{_render_spec_md(spec.read_text())}</div>'
                "</details>"
            )
    return "".join(parts)


_CHART_DOCS_HTML = _build_chart_docs()


_DIR = pathlib.Path(__file__).resolve().parent


def render_editor_page(
    yaml_text: str,
    *,
    nav: str = "",
    dash_name: str = "",
    group: str = "",
    save: str = "",
    theme: str = "",
    user_menu: str = "",
    dash_id: str = "",
    dash_url: str = "",
    view_only: bool = False,
    view_as: bool = True,
) -> str:
    """The editor page seeded with `yaml_text` and topbar pieces — left: `nav`
    (back button) + editable `dash_name`; right: its `group` (read-only),
    `save` (shown only when unsaved), `theme` switch,
    `user_menu` (profile). `dash_id` is `<group>/<name>`, `dash_url` its
    address; `view_only` (someone who may only look) leaves the YAML out and
    the mode switch at view; `view_as`, whether the view-as picker shows.
    `editor.html` carries the placeholders; both modes share it. Read per render rather than at import, like the CSS and JS it
    links: uvicorn's `--reload` ignores non-Python files, so an edit to any of
    them shows on the next page load without a restart."""
    page = (_DIR / "editor.html").read_text()
    # nav + profile are shared with the gallery, and come after the editor's own
    # rules — the order they were inlined in, so the cascade is unchanged.
    styles = assets.favicons() + "\n" + assets.stylesheets("editor.css", "filters.css", "code.css", "nav.css", "profile.css")
    return (
        page.replace("__FF_STYLES__", styles)
        .replace("__FF_SCRIPT__", assets.script("filter-builder.js") + "\n" + assets.script("list-builder.js")
                 + "\n" + assets.script("editor.js")
                 + "\n" + assets.module("yaml-editor.js"))
        .replace("__FF_CHAT__", CHAT_PANEL)
        # Which dashboard: its version history, and the key its unsaved draft
        # is kept under in the browser ("" for the plain single-dashboard editor).
        .replace("__FF_DASH_ID__", escape(dash_id, quote=True))
        .replace("__FF_DASH_URL__", escape(dash_url, quote=True))
        .replace("__FF_VIEW_ONLY__", "1" if view_only else "")
        .replace("__FF_IMPORTMAP__", assets.import_map("diff-view.js", "yaml-look.js"))
        .replace("__FF_NAV__", nav)
        .replace("__FF_MODES__", _mode_switch(view_only))
        .replace("__FF_DASH_NAME__", dash_name)
        .replace("__FF_ROLES__", portal_mod.role_picker(_role_store().roles(), _users()) if view_as else "")
        .replace("__FF_GROUP__", group)
        .replace("__FF_SAVE__", save)
        .replace("__FF_THEME__", theme)
        .replace("__FF_USER_MENU__", user_menu)
        .replace("__FF_DOCS__", _CHART_DOCS_HTML)
        # Last, so a dashboard whose YAML happens to contain a placeholder
        # can't have it filled in.
        .replace("__FF_YAML_CONTENT__", escape(yaml_text))
    )


# --- groups: where a request is, and what its viewer may read -----------------
# Every dashboard and dataset lives in a group, named in the URL:
# `/d/<group>/<name>`, `/datasets/<group>/<name>`. Portal mode's groups are each
# user's own (`_bob`) and the shared ones an admin makes (`RoleStore.groups`).
# Local *paths mode* (FIREFLYER_PATHS, a base dir) has a group per host folder
# mapped under it — its dashboards are files in <path>/dashboards, its datasets
# an isolated blob store — and the plain single-dashboard editor has one,
# `local`. Only portal mode has `group:dataset` references and access checks.
PATHS_BASE = os.environ.get("FIREFLYER_PATHS", "")
_DATA_BASE = os.environ.get("FIREFLYER_DATA", "data/datasets")
# The demo path: docker-compose maps the repo's demo/ folder in under it.
DEMO_PATH = "demo"
LOCAL_GROUP = "local"


def _paths_mode() -> bool:
    return app.state.store is None and bool(PATHS_BASE)


def _groups_on() -> bool:
    """Portal mode: groups with people in them, references across them."""
    return app.state.store is not None


def _group_names() -> list[str]:
    if _groups_on():
        names = [g.name for g in _role_store().groups()]
        # The admin's is always there: where a portal with no login works.
        return names if groups.ADMIN_GROUP in names else [groups.ADMIN_GROUP, *names]
    if _paths_mode():
        return paths_mod.list_paths(PATHS_BASE)
    return [LOCAL_GROUP]


def _own_group(request: Request) -> str:
    """Where a request starts: the signed-in user's own group — `_admin` for the
    env login — in portal mode; the first path in paths mode."""
    if _groups_on():
        identity = auth_mod.current_user(request) or ""
        if not identity or identity == auth_mod.admin_name():
            return groups.ADMIN_GROUP
        return groups.personal(identity)
    names = _group_names()
    return names[0] if names else LOCAL_GROUP


def _request_group(request: Request) -> str:
    """The group a request is in: the URL's, for a page; for the editor's
    stateless routes (they take YAML, not an address), the open dashboard's —
    `<group>/<name>` in its `X-FF-Dashboard` header — or a dataset page's
    (`X-FF-Group`). Else the viewer's own."""
    if "group" in request.path_params:
        return groups.from_url(request.path_params["group"])
    sent = request.headers.get(_DASHBOARD_HEADER)
    group = unquote(sent).partition("/")[0] if sent else unquote(request.headers.get(_GROUP_HEADER, ""))
    if group and group in _group_names():
        return group
    return _own_group(request)


def _dashboards_in(group: str):
    """Group `group`'s dashboard store: the portal DB's rows for it, or that
    path's folder store (None in plain local mode, or for no such path)."""
    if _groups_on():
        return app.state.store.group(group)
    if not _paths_mode() or group not in paths_mod.list_paths(PATHS_BASE):
        return None
    return paths_mod.PathDashboardStore(
        f"{PATHS_BASE}/{group}", history=f"{_DATA_BASE}/{group}/history",
        index=f"{_DATA_BASE}/{group}/uses.json",
    )


def _datasets_in(group: str) -> DatasetStore:
    if _groups_on():
        return app.state.datasets.group(group)
    if _paths_mode() and group in paths_mod.list_paths(PATHS_BASE):
        return DatasetStore(make_object_store({"base": f"{_DATA_BASE}/{group}"}))
    return app.state.datasets


def _dash_store(request: Request):
    """The dashboard store of the request's group."""
    return _dashboards_in(_request_group(request))


def _dataset_store(request: Request) -> DatasetStore:
    """The dataset store of the request's group."""
    return _datasets_in(_request_group(request))


def _known_group(request: Request) -> bool:
    return _request_group(request) in _group_names()


def _can_write(request: Request, group: str | None = None) -> bool:
    """Whether the signed-in user may change `group` (the request's): an admin
    any, anyone else only their own. Without a login, everyone everything."""
    if not _groups_on() or not _users_on():
        return True
    group = _request_group(request) if group is None else group
    return _is_admin(request) or group == _own_group(request)


def _read_only(request: Request) -> HTMLResponse | None:
    """The refusal for a change to a group the user may only read — or None."""
    if not _known_group(request):
        return HTMLResponse("No such group", status_code=404)
    if not _can_write(request):
        return HTMLResponse("Only its owner or an admin changes this group", status_code=403)
    return None


def _every_group_datasets() -> list[tuple[str, DatasetStore]]:
    return [(g, _datasets_in(g)) for g in _group_names()]


# What a viewer may read: (group, dataset) -> the rows, as filters ([] all of
# them). None: everything — an admin, or where there are no logins. A user
# reads all of their own group, and of another's what a role of theirs is
# granted there (one role per dataset, `role_clashes`).
Grants = dict[tuple[str, str], list] | None


def _role_grants(roles: list[str], own: str = "") -> dict[tuple[str, str], list]:
    out: dict[tuple[str, str], list] = {}
    for group, store in _every_group_datasets():
        for ds in store.list():
            if group == own:
                out[(group, ds.name)] = []
                continue
            try:
                perms = store.permissions(ds.name)
            except DatasetError:
                continue
            held = [r for r in roles if r in perms]
            if held:
                out[(group, ds.name)] = perms[held[0]]
    return out


def _grants_of(request: Request) -> Grants:
    """The signed-in viewer's own grants, worked out once per request."""
    if not _groups_on() or not _users_on() or _is_admin(request):
        return None
    if not hasattr(request.state, "grants"):
        identity = auth_mod.current_user(request) or ""
        held = _role_store().user(identity)
        request.state.grants = _role_grants(held.roles if held else [], _own_group(request))
    return request.state.grants


class _Reader:
    """The datasets a request reads, by reference from its group — `orders`
    there, `sales:orders` elsewhere — and only as much of them as `grants`
    allow: a dataset not in them can't be read (`NoAccess`), and a granted one
    comes with its rows' filters, which every scan applies first
    (`scan.scan`). What a dashboard is handed as `datasets` in portal mode."""

    def __init__(self, home: str, grants: Grants):
        self.home = home
        self._grants = grants

    def normalize(self, ref: str) -> str:
        return groups.normal(ref, self.home)

    def _store(self, group: str) -> DatasetStore:
        if group not in _group_names():
            raise DatasetError(f"there is no group {group!r}")
        return _datasets_in(group)

    def rows(self, ref: str) -> list | None:
        """What of `ref` may be read: its filters, [] for all — None for none."""
        key = groups.split(ref, self.home)
        return [] if self._grants is None else self._grants.get(key)

    def source(self, ref: str):
        group, name = groups.split(ref, self.home)
        rows = self.rows(ref)
        if rows is None:
            raise NoAccess(f"no access to {ref}")
        uri, options = self._store(group).source(name)
        return (uri, options, rows) if rows else (uri, options)

    def get(self, ref: str):
        group, name = groups.split(ref, self.home)
        return self._store(group).get(name)

    def list(self) -> list:
        """Every dataset it may read, named as a reference from its group:
        the group's own first, then the rest, `group:name`."""
        out = []
        for group, store in sorted(_every_group_datasets(), key=lambda gs: gs[0] != self.home):
            for ds in store.list():
                ref = groups.join(group, ds.name, self.home)
                if self.rows(ref) is not None:
                    out.append(dataclasses.replace(ds, name=ref))
        return out

    def grants_here(self) -> dict[str, list]:
        """Its grants keyed by reference from its group — what `as_role` takes."""
        return {groups.join(g, n, self.home): rows for (g, n), rows in (self._grants or {}).items()}


def _reader(request: Request, grants: Grants | bool = True):
    """What the editor's routes read datasets through: in portal mode a
    `_Reader` (references across groups, the viewer's grants — or `grants`, a
    view-as); elsewhere the group's own store."""
    if not _groups_on():
        return _dataset_store(request)
    return _Reader(_request_group(request), _grants_of(request) if grants is True else grants)


def _page_title() -> str:
    """Browser-tab title for the gallery pages."""
    return PORTAL_TITLE if app.state.store is not None else "Fireflyer"


def _authors() -> bool:
    """Whether lists show an Author column — only with a login: without one
    (local mode) there's nobody to name."""
    return app.state.authenticator is not None


def _inactive_groups(request: Request) -> set[str]:
    """The groups with nothing shared with the viewer — greyed in the picker,
    still open to them."""
    grants = _grants_of(request)
    if grants is None:
        return set()
    shared = {g for g, _ in grants}
    return {g for g in _group_names() if g not in shared and g != _own_group(request)}


def _picker(request: Request, group: str, kind: str = "d") -> str:
    """The topbar's group dropdown: portal mode's groups, or paths mode's
    paths; nothing in plain local mode. `kind`: the list a pick opens."""
    if _groups_on():
        return portal_mod.group_picker(_role_store().groups(), group, _inactive_groups(request), kind)
    if _paths_mode():
        return portal_mod.path_dropdown(paths_mod.list_paths(PATHS_BASE), group, kind)
    return ""


def _group_label(group: str) -> str:
    """An opened dashboard's or dataset's group (or path), where the lists have
    the picker — read-only: back is the way to another; nothing in plain local
    mode."""
    if _groups_on():
        return portal_mod.group_label(group, "Group")
    if _paths_mode():
        return portal_mod.group_label(group, "Path")
    return ""


def _writable_groups(request: Request) -> list[str]:
    """Where the user may put things — a clone, say: every group for an admin
    (or without a login), else their own."""
    names = _group_names()
    return [g for g in names if _can_write(request, g)]


def _readable(request: Request, home: str, refs: list[str]) -> bool:
    """Whether the viewer may read every dataset in `refs` (written in `home`)."""
    grants = _grants_of(request)
    return grants is None or all(groups.split(r, home) in grants for r in refs)


def _user_menu(request: Request, extra: str = "") -> str:
    """The profile dropdown (username + optional `extra` + logout), or empty when
    auth is off."""
    if app.state.authenticator is None:
        return ""
    return auth_mod.user_menu(auth_mod.current_user(request) or "", extra=extra)


@app.get("/login")
def login_form():
    if app.state.authenticator is None:  # auth off: nothing to log in to
        return RedirectResponse("/", status_code=303)
    return auth_mod.login_page(title=PORTAL_TITLE)


@app.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    found = auth_mod.identify(app.state.authenticator, username, password)
    resp = RedirectResponse("/", status_code=303)
    try:
        if not found:
            raise portal_mod.RoleError("no such user")
        sign_in(request, resp, *found)
    except portal_mod.RoleError:
        # The page asks again with 200; the row says what it was — 401.
        _log(request, "login", actor=username, status_code=401, error="Invalid username or password")
        return auth_mod.login_page("Invalid username or password.", PORTAL_TITLE)
    _log(request, "login", actor=found[0], source=found[1])
    return resp


def sign_in(request: Request, response, identity: str, source: str = portal_mod.LOCAL, **profile: str) -> None:
    """Start `identity`'s session on `response` — the one way in. The login
    form calls it; so does any other system that proves who someone is: an
    SSO/OAuth callback after the provider's check, or an authenticator with
    its own `source` (LDAP…). An outside identity is made a user the first
    time, with that source and no roles (`RoleStore.provision`; `profile`:
    first_name, last_name, email, refreshed each time). RoleError when the name
    belongs to a local user, the admin, or another provider."""
    if identity == auth_mod.admin_name() and source == portal_mod.LOCAL:
        key = ""                                      # the env login: no stored user
    elif source == portal_mod.LOCAL:
        key = _role_store().session_key(identity) or ""
    else:
        key = _role_store().provision(identity, source, **profile)
    # Secure over HTTPS — served so (`fireflyer.portal`), or behind a proxy that
    # says so (X-Forwarded-Proto, which uvicorn turns into the request's scheme).
    auth_mod.set_session(response, identity, key, secure=request.url.scheme == "https")


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    _log(request, "logout")
    resp = RedirectResponse("/login", status_code=303)
    auth_mod.clear_session(resp)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Portal and paths mode: the dashboards of your own group (the first
    path). Plain local: the single-dashboard editor."""
    if _groups_on() or _paths_mode():
        if not _group_names():
            return portal_mod.render_gallery([], _page_title(), _user_menu(request), can_write=False)
        return RedirectResponse(f"/d/{groups.url_name(_own_group(request))}", status_code=303)
    # Plain local mode: no gallery to go back to, no save/profile; just the
    # editable name and theme switch.
    return render_editor_page(
        DEFAULT_YAML,
        dash_name=_dash_name_html(*_name_and_description(DEFAULT_YAML)),
        theme=_theme_switch(),
    )


def _no_group() -> HTMLResponse:
    return HTMLResponse("No such group", status_code=404)


@app.get("/d/{group}", response_class=HTMLResponse)
def dashboards_gallery(group: str, request: Request):
    """A group's dashboards: the ones whose data is all shared with the viewer
    first, then — greyed, still open to them — the rest."""
    group = groups.from_url(group)
    _log(request, "open", "gallery", "dashboards")
    store = _dashboards_in(group)
    if group not in _group_names() or store is None:
        return _no_group()
    reads = _datasets_of(store)
    rows = store.list()
    inactive = {r.id for r in rows if not _readable(request, group, reads.get(r.id, []))}
    rows = [r for r in rows if r.id not in inactive] + [r for r in rows if r.id in inactive]
    return portal_mod.render_gallery(
        rows, _page_title(), _user_menu(request), picker=_picker(request, group),
        authors=_authors(), datasets=_known_refs(group, reads), uses=reads,
        users=_users_tab(request), group=group, inactive=inactive,
        connections=_may_connect(request),
        can_write=_can_write(request, group), clone_groups=_writable_groups(request),
    )


def _empty_yaml(name: str, description: str = "") -> str:
    """A blank but valid dashboard named `name` — no datasets, charts, or layout
    yet. The author fills it in the editor. json.dumps quotes the name (and the
    description, when given) safely — as written, not ASCII-escaped: YAML reads
    the `\\ud83e\\udd81` pair JSON would write for 🦁 as two lone surrogates."""
    about = f"description: {json.dumps(description, ensure_ascii=False)}\n" if description else ""
    return (
        f"name: {json.dumps(name, ensure_ascii=False)}\n{about}\n"
        "datasets: {}\n\ncharts: {}\n\nlayout: []\n"
    )


def _current_author(request: Request) -> str:
    """The logged-in user, recorded as a dashboard's author. Empty when auth is
    off (local dev)."""
    if app.state.authenticator is None:
        return ""
    return auth_mod.current_user(request) or ""


@app.post("/d/{group}/new")
async def portal_new(
    group: str, request: Request, name: str = Form(""), description: str = Form("")
):
    if refused := _read_only(request):
        return refused
    store = _dash_store(request)
    if store is None:
        return _no_group()
    wanted = name.strip() or _free_name(store, "Untitled dashboard")
    _log(request, "create", "dashboard", wanted, description=description.strip())
    try:
        new_id = store.create(_empty_yaml(wanted, description.strip()), _current_author(request))
    except DashboardError as exc:                  # the name is taken
        return _error(exc)
    return RedirectResponse(groups.dashboard_url(store.group_name, new_id), status_code=303)


def _free_name(store, name: str) -> str:
    """A default name no dashboard has yet."""
    return store.free_name(name)


def _dashboard_row(request: Request, dash: str):
    """The request's dashboard — `dash` its URL segment — or None."""
    store = _dash_store(request)
    return store.get(groups.from_url(dash)) if store is not None else None


@app.post("/d/{group}/{dash}/clone")
async def portal_clone(group: str, dash: str, request: Request, name: str = Form(""),
                       to: str = Form("", alias="group")):
    """A copy, under a new name, in a group the user may write — their own,
    for anyone who can open it. Into another group, its bare references are
    made `group:dataset`, so the copy reads the same data (`qualify_refs`)."""
    row = _dashboard_row(request, dash)
    source = _request_group(request)
    target = to.strip() or source
    if row is None:
        _log(request, "clone", "dashboard", name.strip(), source=groups.from_url(dash))
        return HTMLResponse("Dashboard not found", status_code=404)
    if target not in _group_names():
        return _no_group()
    if not _can_write(request, target):
        return HTMLResponse("You can't add to that group", status_code=403)
    store = _dashboards_in(target)
    new_name = name.strip() or _free_name(store, f"{row.name} (copy)")
    _log(request, "clone", "dashboard", new_name, source=row.name, source_group=source)
    text = portal_mod.set_yaml_name(row.yaml, new_name)
    if _groups_on() and target != source:
        text = qualify_refs(text, source, _inline_names(text))
    try:
        new_id = store.create(text, _current_author(request))
    except DashboardError as exc:
        return _error(exc)
    return RedirectResponse(groups.dashboard_url(target, new_id), status_code=303)


def _inline_names(text: str) -> set[str]:
    try:
        return set(Dashboard.from_yaml(text)._inline_names)
    except DashboardError:
        return set()


@app.get("/d/{group}/{dash}", response_class=HTMLResponse)
def portal_open(group: str, dash: str, request: Request) -> HTMLResponse:
    """The dashboard in the editor — for whoever may change its group. Anyone
    else gets it to look at: view mode only, rendered from what's stored, and
    its YAML never sent (`_yaml_for`)."""
    group = groups.from_url(group)
    _log(request, "open", "dashboard", groups.from_url(dash), **dict(request.query_params))
    row = _dashboard_row(request, dash)
    if row is None:
        return HTMLResponse("Dashboard not found", status_code=404)
    writer = _can_write(request, group)
    url = groups.dashboard_url(group, row.id)
    # A selected item (like the dataset detail): back button + the editable
    # name; no Dashboards|Datasets switch, and the group read-only (the switch
    # and the picker are for the lists, where back goes).
    page = render_editor_page(
        row.yaml if writer else "",
        nav=portal_mod.back_button(f"/d/{groups.url_name(group)}", "Back to dashboards"),
        dash_name=_dash_name_html(row.name, row.description),
        group=_group_label(group),
        save=_save_html(url) if writer else "",
        user_menu=_user_menu(request),
        theme=_theme_switch(),
        dash_id=f"{group}/{row.id}",
        dash_url=url,
        view_only=not writer,
        view_as=_may_view_as(request),
    )
    return HTMLResponse(page)


@app.post("/d/{group}/{dash}/save")
async def portal_save(group: str, dash: str, request: Request, yaml_text: str = Form("")) -> dict:
    """`id` and `url` come back: a changed `name:` renames a portal dashboard
    (its name is its key), and the editor moves to the new address."""
    if refused := _read_only(request):
        return JSONResponse({"ok": False, "error": refused.body.decode()}, status_code=refused.status_code)
    store = _dash_store(request)
    dash_id = groups.from_url(dash)
    try:
        saved = store.save(dash_id, yaml_text, author=_current_author(request)) or dash_id
    except DashboardError as exc:
        _log(request, "save", "dashboard", dash_id, status_code=400, error=str(exc))
        return {"ok": False, "error": str(exc)}
    versions = store.versions(saved)
    _log(request, "save", "dashboard", saved, renamed_from=dash_id if saved != dash_id else "",
         version=versions[0].version if versions else None)
    return {"ok": True, "id": f"{store.group_name}/{saved}", "url": groups.dashboard_url(store.group_name, saved)}


def _versions_refused(request: Request, dash: str):
    """A version holds the whole YAML: only for whoever may change the group."""
    if not _can_write(request) or _dashboard_row(request, dash) is None:
        return JSONResponse({"error": "no such dashboard"}, status_code=404)
    return None


@app.get("/d/{group}/{dash}/versions")
def dashboard_versions(group: str, dash: str, request: Request):
    """The dashboard's saved versions, newest first: number, Unix time, author.
    The compare view lists them; it fetches one's YAML when it's picked."""
    _log(request, "versions", "dashboard", groups.from_url(dash))
    if refused := _versions_refused(request, dash):
        return refused
    return {"versions": [
        {"version": v.version, "created_at": v.created_at, "author": v.author}
        for v in _dash_store(request).versions(groups.from_url(dash))
    ]}


@app.get("/d/{group}/{dash}/versions/{number}")
def dashboard_version(group: str, dash: str, number: int, request: Request):
    _log(request, "version", "dashboard", groups.from_url(dash), version=number)
    if refused := _versions_refused(request, dash):
        return refused
    found = _dash_store(request).version(groups.from_url(dash), number)
    if found is None:
        return JSONResponse({"error": "no such version"}, status_code=404)
    return {"version": found.version, "created_at": found.created_at,
            "author": found.author, "yaml": found.yaml}


@app.post("/d/{group}/{dash}/delete")
async def portal_delete(group: str, dash: str, request: Request):
    _log(request, "delete", "dashboard", groups.from_url(dash))
    if refused := _read_only(request):
        return refused
    _dash_store(request).delete(groups.from_url(dash))
    return RedirectResponse(f"/d/{group}", status_code=303)


# --- datasets (portal gallery tab) ------------------------------------------


def _datasets_of(store) -> dict[str, list[str]]:
    """Dashboard id -> the datasets it reads, each a reference from its group
    (`group:name` for another's) — the store's index, so the dashboards list
    is a lookup, not a parse of every dashboard."""
    out: dict[str, list[str]] = {}
    for u in store.uses() if store is not None else []:
        if not u.column:
            ref = groups.join(u.dataset_group or u.group, u.dataset, u.group)
            out.setdefault(u.dashboard_id, []).append(ref)
    return out


def _known_refs(group: str, reads: dict[str, list[str]]) -> set[str]:
    """Which of the references in `reads` (written in `group`) name a dataset
    that exists — the rest show as missing."""
    names: dict[str, set[str]] = {}
    known = set()
    for ref in {r for refs in reads.values() for r in refs}:
        where, name = groups.split(ref, group)
        if where not in names:
            names[where] = set(_datasets_in(where).names()) if where in _group_names() else set()
        if name in names[where]:
            known.add(ref)
    return known


def _readers(group: str, name: str = "") -> list:
    """Every dashboard — of any group — reading group `group`'s datasets (just
    `name`, given one): the `Use` rows of the stores' index."""
    store = _dashboards_in(group)
    if store is None:
        return []
    return [u for u in store.readers() if not name or u.dataset == name]


def _use_link(use, home: str) -> tuple[str, str]:
    """A dashboard reading a dataset of `home`: its address, and its name — with
    a `group:` prefix when it's another group's."""
    label = use.dashboard_name if use.group == home else f"{use.group}{groups.SEPARATOR}{use.dashboard_name}"
    return groups.dashboard_url(use.group, use.dashboard_id), label


def _dashboards_using(group: str, name: str) -> list[tuple[str, str]]:
    return list(dict.fromkeys(_use_link(u, group) for u in _readers(group, name)))


def _columns_used_in(group: str, name: str, columns: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Each column of dataset `name` -> the dashboards whose charts read it (a
    raw table, `*`, reads them all)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for u in _readers(group, name):
        if not u.column:
            continue
        for column in columns if u.column == "*" else [u.column]:
            link = _use_link(u, group)
            if link not in out.setdefault(column, []):
                out[column].append(link)
    return out


def _dataset_descriptions(request: Request) -> dict[str, str]:
    """Reference -> description of the datasets a chart may read — what a
    chart form's dataset field offers as you type (portal mode: the request
    group's, then the others the viewer may read, `group:name`)."""
    return {d.name: d.description for d in _reader(request).list()}


@app.get("/datasets")
def datasets_home(request: Request):
    return RedirectResponse(f"/datasets/{groups.url_name(_own_group(request))}", status_code=303)


@app.get("/datasets/{group}", response_class=HTMLResponse)
def datasets_gallery(group: str, request: Request):
    group = groups.from_url(group)
    _log(request, "open", "gallery", "datasets")
    if group not in _group_names():
        return _no_group()
    datasets = _datasets_in(group).list()
    grants = _grants_of(request)
    inactive = set() if grants is None else {d.name for d in datasets if (group, d.name) not in grants}
    datasets = [d for d in datasets if d.name not in inactive] + [d for d in datasets if d.name in inactive]
    used_by = None
    if _dashboards_in(group) is not None:
        used_by = {}
        for u in _readers(group):
            if not u.column:
                link = _use_link(u, group)
                if link not in used_by.setdefault(u.dataset, []):
                    used_by[u.dataset].append(link)
    return portal_mod.render_datasets(
        datasets, _page_title(), _user_menu(request), picker=_picker(request, group, "datasets"),
        authors=_authors(), used_by=used_by, users=_users_tab(request), group=group,
        inactive=inactive, can_write=_can_write(request, group),
        connections=_may_connect(request),
        sources=_connections().names() if _may_connect(request) else None,
    )


# --- access (roles) -------------------------------------------------------------
# In a database in every mode: the portal's, or a local sqlite file (shared by
# every path) when there's no portal. Opened on first use, so importing the app
# — as the tests do — creates no file. Tests set `app.state.roles`.
# Portal mode opens it at once: a user whose name a group can't carry stops it
# here, at start-up, rather than on someone's first request.
app.state.roles = (
    portal_mod.RoleStore(_DATABASE_URL, admin=auth_mod.admin_name()) if PORTAL_ENABLED else None
)


def _role_store():
    if app.state.roles is None:
        local = f"{_DATA_BASE}/fireflyer.db"
        dsn = _DATABASE_URL if PORTAL_ENABLED else local
        # Portal mode has people: the login is the built-in admin user.
        app.state.roles = portal_mod.RoleStore(dsn, admin=auth_mod.admin_name() if PORTAL_ENABLED else "")
    return app.state.roles


def _every_dataset_store() -> list[DatasetStore]:
    """Every dataset store a role can be named in: each group's (each path's)
    — roles are shared by every group."""
    return [store for _, store in _every_group_datasets()]


def _dataset_key(group: str, name: str) -> str:
    """How a dataset is named where every group's are listed together — the
    Access list, a clash between two roles: `group:name`, or just the name
    where there's one group."""
    return name if len(_group_names()) < 2 else f"{group}{groups.SEPARATOR}{name}"


@app.get("/access", response_class=HTMLResponse)
def access_gallery(request: Request) -> str:
    _log(request, "open", "gallery", "access")
    return portal_mod.render_access(
        _role_store().roles(), _page_title(), _user_menu(request),
        datasets=_datasets_by_role(), users=_users_tab(request), connections=_may_connect(request),
    )


def _datasets_by_role() -> dict[str, list[tuple[str, str]]]:
    """Role name -> (href, label) of the datasets whose (published)
    permissions name it, in every group — the Access list's Datasets column."""
    out: dict[str, list[tuple[str, str]]] = {}
    for group, store in _every_group_datasets():
        for ds in store.list():
            try:
                roles = store.permissions(ds.name)
            except DatasetError:
                continue
            for role in roles:
                out.setdefault(role, []).append((groups.dataset_url(group, ds.name), _dataset_key(group, ds.name)))
    return out


def _with_admin(perms: dict) -> dict:
    """A dataset's permissions as they hold: with the built-in admin role
    (portal mode) reading all of it, though no dataset names it."""
    return {**perms, portal_mod.ADMIN: []} if _users_on() else perms


def _assignable(roles: list) -> list:
    """The roles a dataset's permissions may name: not the built-in admin,
    which reads every dataset anyway."""
    return [r for r in roles if not r.builtin]


@app.post("/access/new")
async def access_new(request: Request, name: str = Form(""), description: str = Form("")):
    _log(request, "create", "role", name.strip(), description=description.strip())
    if _users_on() and not _is_admin(request):
        return HTMLResponse("Only an admin manages roles", status_code=403)
    try:
        _role_store().create_role(name, description)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/access", status_code=303)


def _role_exists(name: str) -> bool:
    return any(r.name == name for r in _role_store().roles())


@app.post("/access/{old}/rename")
async def access_rename(old: str, request: Request, name: str = Form(""),
                        description: str | None = Form(None)):
    _log(request, "rename", "role", name.strip(), renamed_from=old if old != name.strip() else "",
         description=description)
    if _users_on() and not _is_admin(request):
        return HTMLResponse("Only an admin manages roles", status_code=403)
    if not _role_exists(old):
        return HTMLResponse("No such role", status_code=404)
    try:
        new = _role_store().rename_role(old, name, description)
    except portal_mod.RoleError as exc:
        return _error(exc)
    # Datasets name their roles in `permissions:`: they follow the new name.
    if new != old:
        for datasets in _every_dataset_store():
            datasets.rename_role(old, new, author=_current_author(request))
    return RedirectResponse("/access", status_code=303)


@app.post("/access/{name}/delete")
async def access_delete(name: str, request: Request):
    _log(request, "delete", "role", name)
    if _users_on() and not _is_admin(request):
        return HTMLResponse("Only an admin manages roles", status_code=403)
    if not _role_exists(name):
        return HTMLResponse("No such role", status_code=404)
    try:
        _role_store().delete_role(name)
    except portal_mod.RoleError as exc:
        return _error(exc)
    for datasets in _every_dataset_store():
        datasets.drop_role(name, author=_current_author(request))
    return RedirectResponse("/access", status_code=303)


# --- users ------------------------------------------------------------------------
# Portal mode only — there's a login, so there are people. Kept beside the roles
# (`RoleStore`), each holding at most one role per dataset: what a user sees of
# a dataset is then one role's filters, never two to choose between.


def _users_on() -> bool:
    return app.state.authenticator is not None


def _users() -> list:
    return _role_store().users() if _users_on() else []


def _every_permission() -> dict[str, dict]:
    """Dataset (`_dataset_key`) -> its role -> filters, over every group's
    datasets (roles and users are shared by every group) — what a user's roles
    are checked against."""
    out: dict[str, dict] = {}
    for group, store in _every_group_datasets():
        for ds in store.list():
            try:
                out[_dataset_key(group, ds.name)] = _with_admin(store.permissions(ds.name))
            except DatasetError:
                continue
    return out


def _role_reads() -> dict[str, list[str]]:
    """Role name -> the datasets it may read: the user dialog's clash hints."""
    out: dict[str, list[str]] = {}
    for dataset, perms in _every_permission().items():
        for role in perms:
            out.setdefault(role, []).append(dataset)
    return out


def _check_roles(user: str, roles: list[str]) -> None:
    """RoleError when `roles` would read a dataset twice."""
    clashes = portal_mod.role_clashes(roles, _every_permission())
    if clashes:
        raise portal_mod.clash_error(user.strip(), clashes)


def _check_permissions(group: str, dataset: str, mapping: str) -> None:
    """RoleError when `mapping`'s permissions would give a user two roles on
    group `group`'s `dataset` — the other side of `_check_roles`."""
    if not _users_on():
        return
    perms = {_dataset_key(group, dataset): _with_admin(column_map.permissions(mapping))}
    for user in _role_store().users():
        clashes = portal_mod.role_clashes(user.roles, perms)
        if clashes:
            raise portal_mod.clash_error(user.name, clashes)


def _is_admin(request: Request) -> bool:
    """Whether the signed-in user may manage users and roles: the env login,
    or a user holding the built-in `admin` role."""
    identity = auth_mod.current_user(request) or ""
    if identity == auth_mod.admin_name():
        return True
    held = _role_store().user(identity)
    return bool(held) and portal_mod.ADMIN in held.roles


def _users_tab(request: Request) -> bool:
    """The list pages' Users and Groups segments: portal mode, and only for an
    admin."""
    return _users_on() and _is_admin(request)


def _may_view_as(request: Request) -> bool:
    """Viewing a dashboard as a role or a user: an admin's preview — or
    anyone's, where there are no logins."""
    return not _users_on() or _is_admin(request)


def _admins_only(request: Request) -> HTMLResponse | None:
    """The refusal for a Users route — 404 outside portal mode, 403 for anyone
    but an admin (they'd set others' passwords) — or None to go on."""
    if not _users_on():
        return HTMLResponse("Users need portal mode", status_code=404)
    if not _is_admin(request):
        return HTMLResponse("Only an admin manages users", status_code=403)
    return None


def _password_change(password: str, clear: str) -> str | None:
    """What a user form asks of the password: a new one (checked against the
    policy now, before anything is saved), "" to remove it, None to keep it."""
    if clear:
        return ""
    if not password:
        return None
    try:
        return auth_mod.check_policy(password)
    except auth_mod.PasswordError as exc:
        raise portal_mod.RoleError(str(exc)) from exc


def _apply_password(user: str, change: str | None) -> None:
    if change is None:
        return
    if change:
        _role_store().set_password(user, change)
    else:
        _role_store().clear_password(user)


def _log_user(request: Request, action: str, name: str, roles: list[str] = (),
              password: str = "", clear: str = "", **detail) -> None:
    """A Users change: its roles, and whether the password was set or removed
    — never the password."""
    change = "cleared" if clear else "set" if password else ""
    _log(request, action, "user", name.strip(), roles=list(roles), password=change, **detail)


@app.get("/users", response_class=HTMLResponse)
def users_gallery(request: Request):
    _log(request, "open", "gallery", "users")
    if refused := _admins_only(request):
        return refused
    store = _role_store()
    return portal_mod.render_users(
        store.users(), store.roles(), _page_title(), _user_menu(request), reads=_role_reads(),
    )


# The forms post the password as typed (over the same origin, never echoed
# back or kept anywhere but as its hash).
@app.post("/users/new")
async def users_new(request: Request, name: str = Form(""), role: list[str] = Form(default=[]),
                    first_name: str = Form(""), last_name: str = Form(""), email: str = Form(""),
                    password: str = Form("")):
    _log_user(request, "create", name, role, password, first_name=first_name,
              last_name=last_name, email=email)
    if refused := _admins_only(request):
        return refused
    try:
        change = _password_change(password, "")
        _check_roles(name, role)
        created = _role_store().create_user(name, role, first_name, last_name, email)
        _apply_password(created, change)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{old}/edit")
async def users_edit(old: str, request: Request, name: str = Form(""),
                     role: list[str] = Form(default=[]), first_name: str = Form(""),
                     last_name: str = Form(""), email: str = Form(""),
                     password: str = Form(""), clear_password: str = Form("")):
    _log_user(request, "edit", name, role, password, clear_password, first_name=first_name,
              last_name=last_name, email=email, renamed_from=old if old != name.strip() else "")
    if refused := _admins_only(request):
        return refused
    try:
        change = _password_change(password, clear_password)
        _check_roles(name, role)
        edited = _role_store().edit_user(old, name, role, first_name, last_name, email)
        _apply_password(edited, change)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{name}/delete")
async def users_delete(name: str, request: Request):
    _log(request, "delete", "user", name)
    if refused := _admins_only(request):
        return refused
    try:
        _role_store().delete_user(name)
    except portal_mod.RoleError as exc:
        return _error(exc)
    # Their group goes with them: every dashboard and dataset in it. Charts
    # elsewhere that read those datasets lock.
    _drop_group(groups.personal(name))
    return RedirectResponse("/users", status_code=303)


def _drop_group(group: str) -> None:
    if _groups_on():
        app.state.store.drop_group(group)
        app.state.datasets.drop_group(group)


# --- groups -----------------------------------------------------------------------
# Portal mode, admins only (`_admins_only`). A shared group is added, renamed —
# every `old:<dataset>` reference in every group follows — and removed, with
# what's in it. A personal group comes and goes with its user.


@app.get("/groups", response_class=HTMLResponse)
def groups_gallery(request: Request):
    _log(request, "open", "gallery", "groups")
    if refused := _admins_only(request):
        return refused
    counts = {}
    for g in _role_store().groups():
        readers = {(u.group, u.dashboard_id) for u in _readers(g.name) if u.group != g.name}
        counts[g.name] = (len(_dashboards_in(g.name).list()), len(_datasets_in(g.name).names()), len(readers))
    return portal_mod.render_groups(_role_store().groups(), _page_title(), _user_menu(request), counts)


@app.post("/groups/new")
async def groups_new(request: Request, name: str = Form(""), description: str = Form("")):
    _log(request, "create", "group", name.strip(), description=description.strip())
    if refused := _admins_only(request):
        return refused
    try:
        _role_store().create_group(name, description)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group}/rename")
async def groups_rename(group: str, request: Request, name: str = Form(""),
                        description: str | None = Form(None)):
    old = groups.from_url(group)
    _log(request, "rename", "group", name.strip(), renamed_from=old if old != name.strip() else "",
         description=description)
    if refused := _admins_only(request):
        return refused
    try:
        new = _role_store().rename_group(old, name, description)
    except portal_mod.RoleError as exc:
        return _error(exc)
    if new != old:
        app.state.store.rename_group(old, new)
        app.state.datasets.rename_group(old, new)
        _cascade(lambda home, text: rename_group_ref(text, old, new), _current_author(request))
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group}/delete")
async def groups_delete(group: str, request: Request):
    name = groups.from_url(group)
    _log(request, "delete", "group", name)
    if refused := _admins_only(request):
        return refused
    try:
        _role_store().delete_group(name)
    except portal_mod.RoleError as exc:
        return _error(exc)
    _drop_group(name)
    return RedirectResponse("/groups", status_code=303)


def _cascade(change, author: str, group: str = "") -> None:
    """`change(group, yaml)` applied to every dashboard that can read group
    `group`'s datasets — in portal mode every group's; elsewhere (each path is
    isolated) just that group's — saved as a new version where it changed
    anything."""
    if _groups_on():
        rows = [(r, _dashboards_in(r.group)) for r in app.state.store.every()]
    else:
        store = _dashboards_in(group)
        rows = [(r, store) for r in (store.list() if store is not None else [])]
    for row, store in rows:
        text = change(row.group or group, row.yaml)
        if text != row.yaml:
            store.save(row.id, text, author=author)


# --- connections -----------------------------------------------------------------
# The databases a dataset's data can come from (`fireflyer/connections.py`):
# kept where roles are — the portal's database, or locally `fireflyer.db` — and
# managed by admins (anyone where there are no logins). A connection to the
# portal's own database gets `dataset_versions` (portal mode adds one,
# `internal`, at setup). Tests set `app.state.connections`.
app.state.connections = None


def _connections() -> connections_mod.ConnectionStore:
    if app.state.connections is None:
        dsn = _DATABASE_URL if PORTAL_ENABLED else f"{_DATA_BASE}/fireflyer.db"
        app.state.connections = connections_mod.ConnectionStore(
            dsn, auth_mod._secret(), own_url=_DATABASE_URL if PORTAL_ENABLED else "",
        )
    return app.state.connections


def _may_connect(request: Request) -> bool:
    """Connections, and a dataset's `source:`, are an admin's — anyone's where
    there's no login."""
    return not _users_on() or _is_admin(request)


def _admins_connect(request: Request) -> HTMLResponse | None:
    return None if _may_connect(request) else HTMLResponse("Only an admin manages connections", status_code=403)


def _datasets_reading(connection: str) -> list[tuple[str, str]]:
    """(href, label) of every dataset, in every group, whose latest mapping
    reads from `connection`."""
    out = []
    for group, store in _every_group_datasets():
        for name in store.names():
            try:
                source = store.query_source(name)
            except DatasetError:
                continue
            if source and source["connection"] == connection:
                out.append((groups.dataset_url(group, name), _dataset_key(group, name)))
    return out


@app.get("/connections", response_class=HTMLResponse)
def connections_gallery(request: Request):
    _log(request, "open", "gallery", "connections")
    if refused := _admins_connect(request):
        return refused
    store = _connections()
    return portal_mod.render_connections(
        store.list(), _page_title(), _user_menu(request),
        used={c.name: _datasets_reading(c.name) for c in store.list()}, users=_users_tab(request),
    )


def _connection_form(name: str, description: str, url: str, password: str, text: str) -> dict:
    """The dialog's fields — or its YAML, when that's what was posted."""
    if not text.strip():
        return {"name": name, "description": description, "url": url, "password": password}
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise connections_mod.ConnectionsError(f"not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) - {"name", "description", "url", "password"}:
        raise connections_mod.ConnectionsError("a connection takes name, description, url and password")
    return {k: str(doc.get(k) or "") for k in ("name", "description", "url", "password")}


@app.post("/connections/new")
async def connections_new(request: Request, name: str = Form(""), description: str = Form(""),
                          url: str = Form(""), password: str = Form(""), yaml_text: str = Form("", alias="yaml")):
    if refused := _admins_connect(request):
        return refused
    try:
        form = _connection_form(name, description, url, password, yaml_text)
        _log(request, "create", "connection", form["name"].strip(), password="set" if form["password"] else "")
        _connections().create(form["name"], form["url"], description=form["description"],
                              password=form["password"], author=_current_author(request))
    except connections_mod.ConnectionsError as exc:
        return _error(exc)
    return RedirectResponse("/connections", status_code=303)


@app.post("/connections/{old}/edit")
async def connections_edit(old: str, request: Request, name: str = Form(""), description: str = Form(""),
                           url: str = Form(""), password: str = Form(""),
                           yaml_text: str = Form("", alias="yaml")):
    if refused := _admins_connect(request):
        return refused
    try:
        form = _connection_form(name, description, url, password, yaml_text)
        new = form["name"].strip()
        _log(request, "edit", "connection", new, renamed_from=old if old != new else "",
             password="set" if form["password"] else "")
        users = _datasets_reading(old) if new != old else []
        _connections().update(old, new_name=new, url=form["url"], description=form["description"],
                              password=form["password"])
    except connections_mod.ConnectionsError as exc:
        return _error(exc)
    # Datasets name their connection in `source:`: they follow the new name.
    if users:
        for _, store in _every_group_datasets():
            for dataset in store.names():
                _rename_source(store, dataset, old, new, _current_author(request))
    return RedirectResponse("/connections", status_code=303)


def _rename_source(store: DatasetStore, name: str, old: str, new: str, author: str) -> None:
    try:
        mapping = store.mapping(name)
    except DatasetError:
        return
    source = column_map.source(mapping)
    if source and source["connection"] == old:
        text = column_map.dump(column_map.rules(mapping), column_map.permissions(mapping),
                               {**source, "connection": new}, column_map.history(mapping))
        store.remap(name, text, author=author)


@app.post("/connections/{name}/delete")
async def connections_delete(name: str, request: Request):
    _log(request, "delete", "connection", name)
    if refused := _admins_connect(request):
        return refused
    reading = _datasets_reading(name)
    if reading:
        return _error(f"Datasets read from {name!r}: " + ", ".join(label for _, label in reading), 409)
    try:
        _connections().delete(name)
    except connections_mod.ConnectionsError as exc:
        return _error(exc)
    return RedirectResponse("/connections", status_code=303)


@app.post("/connections/{name}/test")
def connections_test(name: str, request: Request) -> JSONResponse:
    if refused := _admins_connect(request):
        return _json_refused(refused)
    try:
        _connections().test(name)
    except connections_mod.ConnectionsError as exc:
        _log(request, "test", "connection", name, error=str(exc), status_code=400)
        return JSONResponse({"ok": False, "error": str(exc)})
    _log(request, "test", "connection", name)
    return JSONResponse({"ok": True})


def _run_query(source: dict):
    """A dataset's `source` run: its query on its connection — on Fireflyer's
    own database with `dataset_versions` beside the real tables."""
    tables = None
    if _connections().is_own(source["connection"]):
        tables = {connections_mod.DATASET_VERSIONS: app.state.datasets.every_version()}
    return _connections().read(source["connection"], source["query"], tables)


def _refresh(request: Request, name: str, mapping: str | None = None) -> str:
    """Run dataset `name`'s query — `mapping`'s, when a changed one comes with
    it — into its next version, built in the background by the caller. ""
    when done, else the database's (or the store's) error, also recorded on
    the dataset."""
    datasets = _dataset_store(request)
    source = column_map.source(mapping) if mapping is not None else datasets.query_source(name)
    if source is None:
        return "this dataset has no query: upload a file instead"
    start = time.monotonic()
    try:
        frame = _run_query(source)
        datasets.refresh(name, frame, mapping=mapping, author=_current_author(request), build=False)
    except (connections_mod.ConnectionsError, DatasetError) as exc:
        _log(request, "refresh", "dataset", name, connection=source["connection"], error=str(exc),
             status_code=400)
        return str(exc)
    _log(request, "refresh", "dataset", name, connection=source["connection"], rows=frame.height,
         seconds=round(time.monotonic() - start, 2))
    return ""


# --- dataset builds --------------------------------------------------------------
# An upload or a new column mapping is a new version, computed in the
# background (`DatasetStore.build`): the request returns at once, the page shows
# "building", and charts keep reading the previous file until it's done. One
# build at a time per process; a version left waiting by a restart is picked up
# when its page asks for its status.
_build_lock = threading.Lock()
_building: set[str] = set()


def _build_key(group: str, name: str) -> str:
    # Per group: each group's datasets are their own.
    return f"{group}:{name}"


def _build(group: str, datasets: DatasetStore, name: str, author: str) -> None:
    key = _build_key(group, name)
    with _build_lock:
        _building.add(key)
        try:
            renames = datasets.build(name)
        except Exception as exc:
            _log_build(author, name, error=str(exc))
            raise
        finally:
            _building.discard(key)
    built = datasets.get(name)
    _log_build(author, name, version=built.version if built else None, error=built.error if built else None)
    # Renamed columns: rewrite the dashboards that use them — in any group —
    # as a dataset rename rewrites its name.
    if renames:
        _cascade(lambda home, text: _rename_columns(text, home, group, name, renames), author, group)


def _spellings(home: str, group: str, name: str) -> list[str]:
    """How a dashboard in `home` may write group `group`'s dataset `name` —
    where there are no groups, just as its name."""
    if not _groups_on():
        return [name]
    qualified = groups.join(group, name, "")
    return [name, qualified] if home == group else [qualified]


def _rename_columns(text: str, home: str, group: str, name: str, renames: dict[str, str]) -> str:
    for ref in _spellings(home, group, name):
        text = rename_column_refs(text, ref, renames)
    return text


def _log_build(author: str, name: str, version=None, error=None) -> None:
    """A background build's outcome — no request, so written here, as who
    asked for it: 200 built, 500 failed (the previous version kept)."""
    if app.state.activity is None:
        return
    detail = {k: v for k, v in {"version": version, "error": error}.items() if v}
    _record(portal_mod.Activity(author, "build", "dataset", name, 500 if error else 200, detail))


def _build_later(background: BackgroundTasks, request: Request, name: str) -> None:
    background.add_task(
        _build, _request_group(request), _dataset_store(request), name, _current_author(request),
    )


def _error(exc: Exception, status: int = 400) -> HTMLResponse:
    return HTMLResponse(f'<pre class="error">{escape(str(exc))}</pre>', status_code=status)


def _json_refused(refused: HTMLResponse) -> JSONResponse:
    return JSONResponse({"error": refused.body.decode()}, status_code=refused.status_code)


def _ds_url(request: Request, name: str) -> str:
    return groups.dataset_url(_request_group(request), name)


@app.post("/datasets/{group}/new")
async def datasets_new(
    group: str,
    request: Request,
    background: BackgroundTasks,
    name: str = Form(""),
    description: str = Form(""),
    delimiter: str = Form(","),
    file: UploadFile = File(...),
):
    content = await file.read()
    _log(request, "upload", "dataset", name.strip(), file=file.filename, bytes=len(content),
         description=description, delimiter=delimiter)
    if refused := _read_only(request):
        return refused
    try:
        ds = _dataset_store(request).create(
            name,
            content,
            description=description,
            delimiter=delimiter or ",",
            author=_current_author(request),
            build=False,
        )
    except DatasetError as exc:
        return _error(exc)
    except Exception as exc:  # object-store errors (S3/Garage unreachable, bad creds)
        return _error(f"Storage error: {exc}", 502)
    _build_later(background, request, ds.name)
    return RedirectResponse(_ds_url(request, ds.name), status_code=303)


@app.post("/datasets/{group}/new-query")
def datasets_new_query(
    group: str, request: Request, background: BackgroundTasks, name: str = Form(""),
    description: str = Form(""), connection: str = Form(""), query: str = Form(""),
):
    """A new dataset from a query: run now — the database's error, if any, is
    the answer — and built in the background, as an upload is."""
    if refused := _read_only(request) or _admins_connect(request):
        return refused
    source = {"connection": connection.strip(), "query": query.strip()}
    start = time.monotonic()
    try:
        frame = _run_query(source)
        ds = _dataset_store(request).create_query(
            name, frame, source, description=description, author=_current_author(request), build=False,
        )
    except (connections_mod.ConnectionsError, DatasetError) as exc:
        _log(request, "create", "dataset", name.strip(), connection=connection, error=str(exc),
             status_code=400)
        return _error(exc)
    _log(request, "create", "dataset", ds.name, connection=connection, rows=frame.height,
         seconds=round(time.monotonic() - start, 2))
    _build_later(background, request, ds.name)
    return RedirectResponse(_ds_url(request, ds.name), status_code=303)


@app.post("/datasets/{group}/{ds}/refresh")
def dataset_refresh(group: str, ds: str, request: Request, background: BackgroundTasks):
    """Run the dataset's query again: the next version. A failure keeps the
    data and shows the database's error on the page."""
    name = groups.from_url(ds)
    if refused := _read_only(request) or _admins_connect(request):
        return refused
    error = _refresh(request, name)
    if error:
        try:
            _dataset_store(request).record_error(name, error)
        except DatasetError as exc:
            return _error(exc, 404)
    else:
        _build_later(background, request, name)
    return RedirectResponse(_ds_url(request, name), status_code=303)


@app.get("/datasets/{group}/{ds}", response_class=HTMLResponse)
def dataset_detail(group: str, ds: str, request: Request):
    name = groups.from_url(ds)
    group = groups.from_url(group)
    _log(request, "open", "dataset", name)
    if group not in _group_names():
        return _no_group()
    datasets = _dataset_store(request)
    found = datasets.get(name)
    if found is None:
        return HTMLResponse("Dataset not found", status_code=404)
    readers = _readers(group, name)
    writer = _can_write(request, group)
    grants = _grants_of(request)
    if writer or grants is None:
        examples = datasets.examples(name)
    else:
        # Someone else's: the values their role lets them see — none, unshared.
        seen = grants.get((group, name))
        examples = {} if seen is None else datasets.examples(name, rows=seen)
    return HTMLResponse(
        portal_mod.render_dataset_detail(
            found, _page_title(), _user_menu(request),
            used_by=list(dict.fromkeys(_use_link(u, group) for u in readers if u.group == group)),
            elsewhere=list(dict.fromkeys(_use_link(u, group) for u in readers if u.group != group)),
            mapping=datasets.mapping(name),
            versions=datasets.versions(name),
            examples=examples,
            used_in=_columns_used_in(group, name, [c.name for c in found.columns]),
            group_label=_group_label(group),
            authors=_authors(), group=group, readonly=not writer,
            ref=groups.join(group, name, "") if _groups_on() else name,
            refresh=writer and _may_connect(request) and datasets.query_source(name) is not None,
            sources=_connections().names() if writer and _may_connect(request) else None,
        )
    )


@app.post("/datasets/{group}/{ds}/replace")
async def dataset_replace(
    group: str,
    ds: str,
    request: Request,
    background: BackgroundTasks,
    description: str = Form(""),
    delimiter: str = Form(","),
    file: UploadFile = File(...),
):
    name = groups.from_url(ds)
    content = await file.read()
    _log(request, "replace", "dataset", name, file=file.filename, bytes=len(content),
         description=description, delimiter=delimiter)
    if refused := _read_only(request):
        return refused
    try:
        _dataset_store(request).replace(
            name, content,
            description=description or None, delimiter=delimiter or ",",
            author=_current_author(request), build=False,
        )
    except DatasetError as exc:
        return _error(exc)
    _build_later(background, request, name)
    return RedirectResponse(_ds_url(request, name), status_code=303)


@app.post("/datasets/{group}/{ds}/mapping")
async def dataset_mapping(
    group: str, ds: str, request: Request, background: BackgroundTasks, mapping: str = Form("")
) -> JSONResponse:
    """A new column mapping (the YAML) — the next minor, built in the background."""
    name = groups.from_url(ds)
    _log(request, "mapping", "dataset", name, mapping=mapping)
    if refused := _read_only(request):
        return _json_refused(refused)
    datasets = _dataset_store(request)
    try:
        _check_permissions(_request_group(request), name, mapping)
        new_source = column_map.source(mapping)
        if new_source != datasets.query_source(name):
            # A changed query: an admin's, and its result is the next version.
            if refused := _admins_connect(request):
                return _json_refused(refused)
            if new_source is not None:
                error = _refresh(request, name, mapping)
                if error:
                    return JSONResponse({"error": error}, status_code=400)
                _build_later(background, request, name)
                return JSONResponse({"building": datasets.get(name).building})
        found = datasets.remap(name, mapping, author=_current_author(request), build=False)
    except (DatasetError, column_map.MappingError, portal_mod.RoleError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    _build_later(background, request, name)
    return JSONResponse({"building": found.building})


@app.post("/datasets/{group}/{ds}/mapping/check")
async def dataset_mapping_check(group: str, ds: str, request: Request, mapping: str = Form("")) -> JSONResponse:
    """Read a mapping's YAML without saving it — the page's form shows what the
    code editor says. 400 with the reason when it doesn't read or fit."""
    if refused := _read_only(request):
        return _json_refused(refused)
    datasets = _dataset_store(request)
    name = groups.from_url(ds)
    try:
        source = column_map.source(mapping)
        if source is not None and source != datasets.query_source(name):
            # A new query: the database says whether it runs, and what it gives.
            sources = _run_query({**source, "query": f"select * from ({source['query'].rstrip(';')}) q limit 0"}).columns
            rules = column_map.rules(mapping)
        else:
            sources = datasets.versions(name)[0].sources
            rules = column_map.parse(mapping, sources)
    except (DatasetError, column_map.MappingError, connections_mod.ConnectionsError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"rules": [
        {"name": r.name, "formula": r.formula, "service": r.service} for r in rules
    ], "source": source, "history": column_map.history(mapping)})


@app.get("/datasets/{group}/{ds}/status")
def dataset_status(group: str, ds: str, request: Request, background: BackgroundTasks) -> JSONResponse:
    """What the page polls while a version builds."""
    name = groups.from_url(ds)
    found = _dataset_store(request).get(name) if _known_group(request) else None
    if found is None:
        return JSONResponse({"error": "unknown dataset"}, status_code=404)
    if found.building and _build_key(_request_group(request), name) not in _building:
        _build_later(background, request, name)
    return JSONResponse({"version": found.version, "building": found.building, "error": found.error})


@app.get("/datasets/{group}/{ds}/versions")
def dataset_versions(group: str, ds: str, request: Request) -> JSONResponse:
    name = groups.from_url(ds)
    _log(request, "versions", "dataset", name)
    if refused := _read_only(request):
        return _json_refused(refused)
    try:
        versions = _dataset_store(request).versions(name)
    except DatasetError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"versions": [
        {"version": v.label, "created_at": v.created_at, "author": v.author,
         "status": v.status, "rows": v.rows}
        for v in versions
    ]})


@app.get("/datasets/{group}/{ds}/versions/{label}")
def dataset_version(group: str, ds: str, label: str, request: Request) -> JSONResponse:
    name = groups.from_url(ds)
    _log(request, "version", "dataset", name, version=label)
    if refused := _read_only(request):
        return _json_refused(refused)
    try:
        v = _dataset_store(request).version(name, label)
    except DatasetError:
        v = None
    if v is None:
        return JSONResponse({"error": "unknown version"}, status_code=404)
    return JSONResponse({"version": v.label, "created_at": v.created_at, "author": v.author,
                         "status": v.status, "rows": v.rows, "mapping": v.mapping})


@app.get("/datasets/{group}/{ds}/versions/{label}/restore")
def dataset_version_restore(group: str, ds: str, label: str, request: Request) -> JSONResponse:
    """A version's mapping fitted to the latest upload — for the compare view's
    restore, which loads it as an unsaved edit (saving builds it anew)."""
    name = groups.from_url(ds)
    _log(request, "restore", "dataset", name, version=label)
    if refused := _read_only(request):
        return _json_refused(refused)
    try:
        return JSONResponse({"mapping": _dataset_store(request).restored(name, label)})
    except DatasetError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/datasets/{group}/{ds}/versions/{major}/delete")
def dataset_delete_major(group: str, ds: str, major: int, request: Request):
    name = groups.from_url(ds)
    _log(request, "delete_version", "dataset", name, version=major)
    if refused := _read_only(request):
        return refused
    try:
        _dataset_store(request).delete_major(name, major)
    except DatasetError as exc:
        return _error(exc)
    return RedirectResponse(_ds_url(request, name), status_code=303)


@app.post("/datasets/{group}/{ds}/rename")
async def dataset_rename(
    group: str, ds: str, request: Request, name: str = Form(""), description: str = Form(""),
    next: str = Form(""),
):
    """Rename and/or re-describe a dataset. From the dataset's own page
    (`next=detail`, its title card) back to that page, at the new name;
    otherwise to the list. A new name reaches every dashboard reading it, in
    every group."""
    old_name = groups.from_url(ds)
    new = name.strip()
    _log(request, "rename", "dataset", new, renamed_from=old_name if new != old_name else "",
         description=description)
    if refused := _read_only(request):
        return refused
    try:
        _dataset_store(request).rename(old_name, new, description=description)
    except DatasetError as exc:
        return HTMLResponse(f'<pre class="error">{escape(str(exc))}</pre>', status_code=400)
    home = _request_group(request)
    if new != old_name:
        _cascade(lambda where, text: _rename_dataset(text, where, home, old_name, new),
                 _current_author(request), home)
    back = _ds_url(request, new) if next == "detail" else f"/datasets/{groups.url_name(home)}"
    return RedirectResponse(back, status_code=303)


def _rename_dataset(text: str, where: str, group: str, old: str, new: str) -> str:
    """A dashboard in `where`, with group `group`'s dataset `old` renamed `new`."""
    for before, after in zip(_spellings(where, group, old), _spellings(where, group, new)):
        text = rename_dataset_ref(text, before, after)
    return text


def _filter_context(request: Request, name: str):
    """The columns of the dataset a page's filter rows are about."""
    return config_edit.dataset_context(name, _dataset_store(request).source)


@app.post("/datasets/{group}/{ds}/access/form", response_class=HTMLResponse)
async def dataset_access(group: str, ds: str, request: Request, mapping: str = Form("")):
    """The dataset page's permissions mode: the roles the posted mapping's
    `permissions:` name, as a form, with a + for the rest — built from the YAML
    being edited, so it shows exactly what Save would keep."""
    name = groups.from_url(ds)
    if refused := _read_only(request):
        return refused
    store = _dataset_store(request)
    try:
        access = column_map.permissions(mapping)
    except column_map.MappingError as exc:
        return _error(exc)
    context = _filter_context(request, name)
    draft = portal_mod.access_draft(builder_fields(context.columns, types=context.column_types))
    counts = {}
    for role, filters in access.items():
        try:
            counts[role] = store.rows_seen(name, filters)
        except DatasetError:
            counts[role] = None
    return portal_mod.render_access_form(
        _assignable(_role_store().roles()), access,
        lambda f: portal_mod.access_filter_row(f, context.column_types), draft, counts,
    )


@app.post("/datasets/{group}/{ds}/access/rows")
async def dataset_access_rows(group: str, ds: str, request: Request) -> JSONResponse:
    """How many rows a role with the posted filters would see — the Rows
    column, recounted as its filters change."""
    if refused := _read_only(request):
        return _json_refused(refused)
    try:
        count = _dataset_store(request).rows_seen(groups.from_url(ds), parse_filters(await request.form()))
    except (filters_mod.FilterError, DatasetError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"rows": count})


@app.post("/datasets/{group}/{ds}/access/yaml")
async def dataset_access_yaml(group: str, ds: str, request: Request) -> JSONResponse:
    """The permissions form, written into the posted mapping's `permissions:` —
    the YAML Save then keeps as the next version. Only roles that exist, and
    never two that one user holds."""
    name = groups.from_url(ds)
    form = await request.form()
    _log(request, "permissions", "dataset", name, **_form_detail(form))
    if refused := _read_only(request):
        return _json_refused(refused)
    known = {r.name for r in _assignable(_role_store().roles())}
    try:
        grants = portal_mod.parse_access_form(form)
        mapping = column_map.with_permissions(
            form.get("mapping", ""), {r: fs for r, fs in grants.items() if r in known},
        )
        _check_permissions(_request_group(request), name, mapping)
    except (filters_mod.FilterError, column_map.MappingError, portal_mod.RoleError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"mapping": mapping})


@app.post("/datasets/{group}/{ds}/access/filter", response_class=HTMLResponse)
async def dataset_access_filter(group: str, ds: str, request: Request):
    """The filter built in a role's draft row, as the compact row its list
    shows — or why it isn't a filter yet."""
    if refused := _read_only(request):
        return refused
    rows = parse_filters(await request.form())
    try:
        filters_mod.normalize(rows[:1])
    except filters_mod.FilterError as exc:
        return _error(exc)
    if not rows:
        return _error(ValueError("pick a column and its values"))
    context = _filter_context(request, groups.from_url(ds))
    return portal_mod.access_filter_row(rows[0], context.column_types)


@app.post("/datasets/{group}/{ds}/delete")
async def dataset_delete(group: str, ds: str, request: Request):
    """Guarded while a dashboard of its own group reads it. Other groups'
    dashboards don't hold it: their charts on it lock (the page warned)."""
    name = groups.from_url(ds)
    _log(request, "delete", "dataset", name)
    if refused := _read_only(request):
        return refused
    home = _request_group(request)
    used = list(dict.fromkeys(u.dashboard_name for u in _readers(home, name) if u.group == home))
    if used:  # guard: don't orphan dashboards
        names = ", ".join(used)
        msg = f"Cannot remove {name!r}: still used by {len(used)} dashboard(s): {names}"
        return HTMLResponse(f'<pre class="error">{escape(msg)}</pre>', status_code=409)
    _dataset_store(request).delete(name)
    return RedirectResponse(f"/datasets/{groups.url_name(home)}", status_code=303)


# What the editor does without a request of its own — put the last save back,
# switch mode, pick a role or user to view as — it says here (a beacon).
_CLIENT_ACTIONS = {"discard", "mode", "view_as"}


@app.post("/activity")
async def client_activity(request: Request) -> Response:
    form = await request.form()
    action = form.get("action", "")
    if action not in _CLIENT_ACTIONS:
        return Response(status_code=400)
    detail = {k: v for k, v in _form_detail(form).items() if k not in ("action", "dashboard")}
    # A beacon carries no headers: the dashboard, `<group>/<name>`, is a field.
    group, _, name = form.get("dashboard", "").partition("/")
    _log(request, action, "dashboard", name, group=group, **detail)
    return Response(status_code=204)


@app.post("/execute")
async def execute(request: Request, active_tab: int = 0, f: str = "", role: str = "",
                  user: str = "") -> dict:
    """`f` is the page URL's filter state (see `filters.encode_state`): the
    editor passes it on every render, so a reload, a shared link, or a YAML edit
    all keep the filters in play."""
    posted = (await request.body()).decode("utf-8")
    body = _yaml_for(request, posted)
    # Every render: opening, ↻, an edit applied, a role picked, a tab clicked.
    seen = dict(tab=active_tab or None, filters=filters_mod.decode_state(f), role=role, user=user)
    _log(request, "refresh", "dashboard", _dash_of(request, body), **seen)
    try:
        dashboard, role, user = _dashboard_for(request, body, role, user)
        if body is not posted:
            dashboard.yaml_source = ""          # a view-only page never holds the YAML
        # The response is a skeleton — each cell fetches itself via htmx so
        # charts render in parallel and slow charts don't block fast ones.
        # editing=True adds the editor-only row resize handles. `active_tab`
        # (a query param) keeps the editor on the current tab after an edit.
        return {
            "ok": True,
            "html": dashboard.render_skeleton(
                cf_tokens=filters_mod.decode_state(f),
                editing=True,
                active_tab=active_tab,
                role=role,
                user=user,
            ),
            # For the toolbar's ⓘ, which follows the YAML like the name does.
            "description": dashboard.description,
        }
    except DashboardError as exc:
        _log(request, "refresh", "dashboard", _dash_of(request, body), status_code=400, error=str(exc), **seen)
        return {"ok": False, "html": f'<pre class="error">{escape(str(exc))}</pre>'}
    except Exception as exc:
        _log(request, "refresh", "dashboard", _dash_of(request, body), status_code=500, error=str(exc), **seen)
        return {
            "ok": False,
            "html": f'<pre class="error">{escape(traceback.format_exc())}</pre>',
        }


def _yaml_for(request: Request, posted: str) -> str:
    """The YAML a render is of: what the editor posted — or, from a view-only
    page (which holds none), the stored dashboard its `X-FF-Dashboard` header
    names."""
    sent = request.headers.get(_DASHBOARD_HEADER)
    if posted.strip() or not sent:
        return posted
    group, _, name = unquote(sent).partition("/")
    store = _dashboards_in(group) if group in _group_names() else None
    row = store.get(name) if store is not None else None
    return row.yaml if row is not None else posted


def _dashboard_for(request: Request, yaml_text: str, role: str = "", user: str = ""):
    """`yaml_text` parsed for a render, and the role and user it's viewed as.
    In portal mode it reads through its viewer's grants — or, for an admin
    viewing as one, a role's or a user's (`_may_view_as`; anyone else's `role`
    and `user` are dropped). Locally, view-as is the only narrowing."""
    if not _may_view_as(request):
        role = user = ""
    if not _groups_on():
        dashboard = Dashboard.from_yaml(yaml_text, datasets=_dataset_store(request))
        return _as_role(request, dashboard, role, user), role, user
    if user:
        grants, name = _user_grants(user), user
    elif role:
        grants, name = (None if role == portal_mod.ADMIN else _role_grants([role])), role
    else:
        grants, name = _grants_of(request), auth_mod.current_user(request) or ""
    reader = _reader(request, grants)
    dashboard = Dashboard.from_yaml(yaml_text, datasets=reader)
    if grants is not None:
        dashboard.as_role(name, reader.grants_here())
    return dashboard, role, user


def _user_grants(user: str) -> Grants:
    """What `user` reads: all of their own group, and their roles' grants."""
    held = _role_store().user(user)
    if held is None:
        return {}
    if held.builtin or portal_mod.ADMIN in held.roles:
        return None
    return _role_grants(held.roles, groups.personal(user))


def _as_role(request: Request, dashboard: Dashboard, role: str, user: str = "") -> Dashboard:
    """Locally (no groups): `dashboard` as `role` (a name) sees it — the
    editor's view-as picker — from the `permissions:` of this path's datasets.
    A role no dataset names sees no managed dataset at all. Or as `user` sees
    it: each dataset through the one role of theirs that may read it
    (`role_clashes` keeps it one); a user with no such role — or one since
    removed — sees none of it."""
    datasets = _dataset_store(request)
    if user:
        held = _role_store().user(user) if _users_on() else None
        grants = {}
        for name in (held.roles if held else []):
            grants.update(_grants(datasets, name))
        return dashboard.as_role(user, grants)
    if not role:
        return dashboard
    return dashboard.as_role(role, _grants(datasets, role))


def _grants(datasets: DatasetStore, role: str) -> dict[str, list]:
    """Dataset -> `role`'s filters on it — every dataset, in full, for the
    built-in admin role."""
    if _users_on() and role == portal_mod.ADMIN:
        return {name: [] for name in datasets.names()}
    return datasets.grants(role)


class ChatRequest(BaseModel):
    message: str
    yaml: str = ""
    history: list[dict] = []


@app.post("/chat")
async def chat(request: Request, req: ChatRequest) -> dict:
    """One AI-assistant turn. Returns {ok, reply, yaml?}. `yaml` is present only
    when the assistant proposed a change that parsed cleanly; the browser then
    swaps it into the editor and re-renders. The available datasets' schemas
    (columns + types, no data) are handed to the model so it builds charts and
    calcs from real columns."""
    asked = dict(prompt=req.message, turns=len(req.history))
    if not CHAT_ENABLED:
        _log(request, "chat", "dashboard", _dash_of(request, req.yaml), status_code=503, **asked)
        return {
            "ok": False,
            "reply": "AI assistant is disabled. Set ANTHROPIC_API_KEY in .env and restart.",
        }
    try:
        # A thread: the assistant can take a minute, and waiting on it in the
        # event loop froze every other request — other pages, chart cells.
        result = await run_in_threadpool(
            chat_mod.run_chat, req.message, req.yaml, req.history,
            datasets=_dataset_schemas(request),
        )
    except Exception as exc:  # surface SDK/auth/network errors as a chat reply
        _log(request, "chat", "dashboard", _dash_of(request, req.yaml), status_code=502, error=str(exc), **asked)
        return {"ok": False, "reply": f"Assistant error: {exc}"}
    _log(request, "chat", "dashboard", _dash_of(request, req.yaml), changed="yaml" in result,
         reply=result.get("reply"), **asked)
    return {"ok": True, **result}


def _dataset_schemas(request: Request) -> list[dict]:
    """Schema-only view of the available datasets for the AI assistant: each
    dataset's name + columns (name/type), never any row data. Best-effort — an
    unreadable store yields an empty list rather than failing the chat turn."""
    try:
        return [
            {
                "name": d.name,
                "columns": [{"name": c.name, "dtype": c.dtype} for c in d.columns],
            }
            for d in _reader(request).list()
        ]
    except Exception:
        return []


@app.get("/chart/map", response_class=HTMLResponse)
def chart_map(
    request: Request,
    dataset: str,
    title: str,
    lat: str,
    lng: str,
    grid_size: int = 20,
    zoom: int | None = None,
    filters: str = "",
) -> str:
    parsed = json.loads(filters) if filters else []
    chart = Map(
        dataset=dataset,
        title=title,
        lat=lat,
        lng=lng,
        grid_size=grid_size,
        zoom=zoom,
        filters=parsed,
    )
    chart._resolve = _reader(request).source  # dataset name -> Parquet source
    return chart.to_html()


@app.get("/chart/table", response_class=HTMLResponse)
def chart_table(
    request: Request,
    dataset: str,
    title: str,
    search: int = 1,
    pagination: int = 5,
    page: int = 1,
    q: str = "",
    filters: str = "",
    columns: str = "",
    sort: str = "",
) -> str:
    # `filters` is JSON-encoded by Table._base_params so the chart's own htmx
    # round-trips (search, pagination) preserve declared + merged crossfilters.
    # `measures` is deliberately absent: resolving a measure needs the
    # dashboard's `calcs:` block, which this standalone route has no access to,
    # so an embedded table posts to /dashboard/cell instead (see chart.html).
    parsed = json.loads(filters) if filters else []
    split = lambda text: [p.strip() for p in text.split(",") if p.strip()]
    chart = Table(
        dataset=dataset,
        title=title,
        columns=split(columns),
        sort=split(sort),
        search=bool(search),
        pagination=pagination,
        filters=parsed,
    )
    chart._resolve = _reader(request).source  # dataset name -> Parquet source
    return chart.to_html(page=page, query=q)


def _url_with_state(url: str, tokens: list[str]) -> str:
    """`url`'s path and query with `f` set to `tokens` (or dropped when there
    are none); every other parameter is kept. Path-relative, since htmx only
    replaces within the page's own origin."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "f"]
    state = filters_mod.encode_state(tokens)
    if state:
        query.append(("f", state))
    return urlunsplit(("", "", parts.path or "/", urlencode(query), ""))


def _log_dashboard(request: Request, yaml_text: str, cf: list[str], toggle: str, remove: list[str],
                   replace: str, added: list[dict], set_grain: str, active_tab: int, **seen) -> None:
    """What a viewer did to make `/dashboard` re-render — clicked a chart
    (crossfilter), added or edited a quick filter (filter), took one off
    (unfilter), picked a time grain, or switched tab — with the values: an
    audit trail says what was looked at."""
    if toggle:
        action = "crossfilter"
    elif added:
        action = "filter"
    elif remove:
        action = "unfilter"
    elif set_grain:
        action = "grain"
    else:
        action = "tab"
    _log(request, action, "dashboard", _dash_of(request, yaml_text), toggle=toggle, added=added,
         replaced=replace, removed=remove, grain=set_grain, tab=active_tab,
         filters=cf, **seen)


@app.post("/dashboard", response_class=HTMLResponse)
async def dashboard_render(
    request: Request,
    response: Response,
    yaml_text: str = Form(""),
    cf: list[str] = Form(default=[]),
    grain: list[str] = Form(default=[]),
    toggle: str = Form(""),
    remove: list[str] = Form(default=[]),
    replace: str = Form(""),
    set_grain: str = Form(""),
    editing: str = Form(""),
    active_tab: int = Form(0),
    open_filter: str = Form(""),
    role: str = Form(""),
    user: str = Form(""),
) -> str:
    """Re-render the dashboard with an updated crossfilter set.

    Triggered by an htmx click on a crossfilter-enabled chart element (e.g.
    a pie slice). The current `cf` tokens + the `toggle` token combine into
    a new set, which is decoded into Filter objects and threaded through
    Dashboard.to_html. The whole dashboard fragment is swapped in place.

    `editing` round-trips (as a hidden input) so the editor keeps its resize
    handles and per-chart edit buttons after a crossfilter click.
    """
    added = parse_filters(await request.form())
    posted, yaml_text = yaml_text, _yaml_for(request, yaml_text)
    _log_dashboard(request, yaml_text, list(cf), toggle, remove, replace, added, set_grain, active_tab,
                   role=role, user=user)
    try:
        dashboard, role, user = _dashboard_for(request, yaml_text, role, user)
    except DashboardError as exc:
        return f'<pre class="error">{escape(str(exc))}</pre>'
    if yaml_text is not posted:
        dashboard.yaml_source = ""
    new_tokens = filters_mod.toggle_token(list(cf), toggle) if toggle else list(cf)
    # A filter panel row's ✕: every token behind that row.
    new_tokens = [t for t in new_tokens if t not in remove]
    # A filter panel's + form: the chart builder's own fields, parsed by its own
    # parser, become global quick filters. Added, never toggled — submitting the
    # same filter twice must not remove it; its row's ✕ does that. One the
    # model rejects (a `between` without exactly two bounds) is dropped rather
    # than kept as a token nothing would ever apply or show.
    for f in added:
        try:
            filters_mod.normalize([f])
        except filters_mod.FilterError:
            continue
        token = filters_mod.global_token(f["column"], f["op"], f["values"])
        if replace in new_tokens:
            # An edit: the new filter takes the old one's place in the list —
            # once, should it match another filter already there.
            new_tokens = [token if t == replace else t for t in new_tokens]
            new_tokens = list(dict.fromkeys(new_tokens))
        elif token not in new_tokens:
            new_tokens.append(token)
    # `set_grain` is a bar chart's time-grain picker: "<cid>|<grain>", or
    # "<cid>|" to go back to automatic. Rides the same hidden-input round-trip
    # as `cf` so a grain choice survives crossfilter clicks and tab switches.
    grains = set_grain_token(list(grain), set_grain) if set_grain else list(grain)
    # The filters belong in the address bar, so reloading or sharing the page
    # keeps them. htmx applies this header itself — no editor JS — and replaces
    # the history entry rather than pushing one, so Back leaves the dashboard
    # instead of stepping through every click.
    current = request.headers.get("HX-Current-URL")
    if current:
        response.headers["HX-Replace-Url"] = _url_with_state(current, new_tokens)
    # Returning a skeleton means every cell re-fetches with the new cf state.
    # The dashboard div is swapped (outerHTML) and the fresh placeholders fire
    # hx-trigger="load" — same async path as the initial /execute response.
    # `active_tab` rides along (hidden input) so a crossfilter click or a tab
    # switch keeps the right tab showing.
    return dashboard.render_skeleton(
        cf_tokens=new_tokens,
        editing=bool(editing),
        active_tab=active_tab,
        grain_tokens=grains,
        open_filter=open_filter,
        role=role,
        user=user,
    )


@app.post("/dashboard/cell", response_class=HTMLResponse)
async def dashboard_cell(
    request: Request,
    yaml_text: str = Form(""),
    cid: str = Form(""),
    cf: list[str] = Form(default=[]),
    grain: list[str] = Form(default=[]),
    set_grain: str = Form(""),
    legend_page: int = Form(0),
    table_page: int = Form(1),
    table_q: str = Form(""),
    col: str = Form("1"),
    row: str = Form("1"),
    editing: str = Form(""),
    open_filter: str = Form(""),
    role: str = Form(""),
    user: str = Form(""),
) -> str:
    """Render a single dashboard cell. Triggered by the skeleton's per-cell
    hx-post; the response replaces the placeholder via outerHTML swap.

    `col` / `row` round-trip the CSS grid placement from the skeleton so the
    rendered cell lands in the right slot (and preserves any merged span).
    `editing` adds the per-chart edit (pencil) button.

    `set_grain` arrives when a chart's own grain/scale control fired: those
    change nothing for the other charts, so they re-render this cell alone
    rather than the whole dashboard. The page-level grain state rides back as an
    out-of-band swap, since a cell response doesn't otherwise touch it."""
    grains = set_grain_token(list(grain), set_grain) if set_grain else list(grain)
    yaml_text = _yaml_for(request, yaml_text)
    # A render fans out into a cell request per chart — logged as its one
    # `refresh`. A cell's own row is a viewer paging, searching or re-graining it.
    if set_grain or legend_page or table_page > 1 or table_q:
        _log(request, "search" if table_q else "grain" if set_grain else "page", "dashboard",
             _dash_of(request, yaml_text), chart=cid, q=table_q, page=table_page if table_page > 1 else None,
             legend_page=legend_page, grain=set_grain, filters=list(cf), role=role, user=user)
    try:
        dashboard, role, user = _dashboard_for(request, yaml_text, role, user)
        html = dashboard.render_cell(
            cid,
            cf_tokens=list(cf),
            col=col,
            row=row,
            editing=bool(editing),
            grain_tokens=grains,
            legend_page=legend_page,
            table_page=table_page,
            table_query=table_q,
            open_filter=bool(open_filter),
        )
        return html + grain_state_html(grains) if set_grain else html
    except DashboardError as exc:
        return f'<pre class="error">{escape(str(exc))}</pre>'


@app.post("/filter/fields", response_class=HTMLResponse)
async def filter_fields(request: Request) -> str:
    """A filter row's fields, re-rendered after its column or op changed, so the
    values control becomes a picker of that column's values (or text again).
    Posted by the dashboard's filter panel (htmx, `live` set: the response must
    keep re-fetching) and by the chart builder / calcs manager (editor JS)."""
    form = await request.form()
    rows = parse_filters(form) or [{}]
    row = rows[0]
    return config_edit.filter_fields_for(
        _yaml_for(request, form.get("yaml_text", "")),
        form.get("filter_dataset", ""),
        column=(form.get("filter_column") or "").strip(),
        op=form.get("filter_op") or "in",
        values=row.get("values", []),
        resolve=_reader(request).source,
        live=bool(form.get("live")),
    )


class AssistRequest(BaseModel):
    yaml: str = ""


@app.post("/yaml/assist")
async def yaml_assist_route(request: Request, req: AssistRequest) -> dict:
    """The YAML editor's question, asked after each pause in typing: what's
    wrong with this dashboard, and where (`problems`); and what can complete
    here (`schema`). One request for both, since both need the current text."""
    store = _reader(request)
    return {
        "problems": yaml_assist.lint(req.yaml, datasets=store),
        "schema": yaml_assist.schema(req.yaml, store),
    }


@app.post("/filter/edit", response_class=HTMLResponse)
async def filter_edit(request: Request) -> str:
    """A filter panel's bottom row, loaded with the global filter `token` for
    editing — or blank again, with no token (the ✕ that cancels an edit)."""
    form = await request.form()
    token = form.get("token", "")
    dataset = form.get("filter_dataset", "")
    found = filters_mod.global_filters([token]) if token else []
    column, op, values = ("", "in", [])
    if found:
        _, f = found[0]
        column, op, values = f.column, f.op, list(f.values)
    fields = config_edit.filter_fields_for(
        _yaml_for(request, form.get("yaml_text", "")), dataset, column=column, op=op, values=values,
        resolve=_reader(request).source, live=True,
    )
    return filter_add_body(
        form.get("cid", ""), dataset, fields, replace=token if found else ""
    )


@app.post("/filter/values", response_class=HTMLResponse)
async def filter_values(request: Request) -> str:
    """A filter row's value list, narrowed by its search box (`filter_q`). The
    ticked values ride along so they stay in the list, ticked. Posted by the
    dashboard's filter panel (htmx) and the chart builder / calcs manager
    (editor JS) alike."""
    form = await request.form()
    return config_edit.filter_values_for(
        _yaml_for(request, form.get("yaml_text", "")),
        form.get("filter_dataset", ""),
        (form.get("filter_column") or "").strip(),
        query=(form.get("filter_q") or "").strip(),
        selected=form.getlist("filter_value"),
        resolve=_reader(request).source,
    )


@app.get("/filter/range", response_class=HTMLResponse)
def filter_range(
    request: Request,
    month: str = "",
    pick: str = "",
    required: str = "",
) -> str:
    """The date-range picker after a click on a day (`pick`), a month arrow, or
    a date typed into its Start / End (`typed_from` / `typed_to`). Every request
    carries the picker's whole state, so this needs nothing else —
    no YAML, no dataset. `from`/`to` are read off the query by hand: `from` is
    a Python keyword, so it can't be a parameter name."""
    query = request.query_params
    typed = next(
        ((end, query[f"typed_{end}"]) for end in ("from", "to") if f"typed_{end}" in query),
        None,
    )
    return date_range.respond(
        query.get("from", ""), query.get("to", ""), month, pick,
        required=bool(required), typed=typed,
    )


@app.get("/filter/number", response_class=HTMLResponse)
def filter_number(request: Request) -> str:
    """The number-range picker after a handle was let go or a number typed
    (`changed` names which). Every request carries the picker's values and the
    column's extent, so this needs nothing else — no YAML, no dataset."""
    params = request.query_params
    return number_range.respond(params, params.get("changed", ""))


@app.post("/chart/config/form", response_class=HTMLResponse)
async def chart_config_form(
    request: Request,
    yaml_text: str = Form(""),
    cid: str = Form(""),
    type_override: str = Form(""),
    dataset: str = Form(""),
) -> str:
    """Build the edit-modal form for one chart from the current YAML.

    `type_override` re-renders the fields for a different chart type when the
    user changes the type dropdown; `dataset`, for a dataset picked there."""
    try:
        return config_edit.build_form(
            yaml_text, cid, type_override=type_override,
            resolve=_reader(request).source,
            datasets=_dataset_descriptions(request), dataset_override=dataset.strip(),
            group=_request_group(request) if _groups_on() else "",
        )
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return f'<div class="ff-modal-error" role="alert">{escape(str(exc))}</div>'


@app.post("/chart/config/add-form", response_class=HTMLResponse)
async def chart_config_add_form(
    request: Request,
    yaml_text: str = Form(""),
    add_type: str = Form("table"),
    add_mode: str = Form("row"),
    add_index: str = Form("end"),
    dataset: str = Form(""),
    title: str = Form(""),
) -> str:
    """Build the create-chart modal (defaults for a fresh chart of `add_type`).
    Placement (`add_mode`/`add_index`) rides along as hidden inputs; with a
    `dataset` its calc and column fields list that dataset's."""
    try:
        return config_edit.build_add_form(
            yaml_text, add_type, add_mode, add_index,
            dataset=dataset.strip(), title=title, resolve=_reader(request).source,
            datasets=_dataset_descriptions(request), group=_request_group(request) if _groups_on() else "",
        )
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return f'<div class="ff-modal-error" role="alert">{escape(str(exc))}</div>'


def _edited(request: Request, action: str, yaml_text: str, edit, **detail) -> dict:
    """An editor gesture: `edit()` makes the new YAML — `{ok, yaml}` for the
    browser to swap in — or says why it can't (`{ok: false, error}`). Logged
    either way, a refusal as 400."""
    try:
        result = {"ok": True, "yaml": edit()}
    except (config_edit.ConfigEditError, DashboardError, calcs_edit.CalcsEditError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    _log(request, action, "dashboard", _dash_of(request, yaml_text),
         status_code=None if result["ok"] else 400, error=result.get("error"), **detail)
    return result


@app.post("/chart/config/create")
async def chart_config_create(request: Request) -> dict:
    """Create a chart from the add-form and place it in the layout. Returns
    {ok, yaml} on success, else {ok:false, error}."""
    form = await request.form()
    yaml_text = form.get("yaml_text", "")
    return _edited(request, "add_chart", yaml_text, lambda: config_edit.add_chart(yaml_text, form),
                   **_form_detail(form))


@app.post("/chart/config/insert-item")
async def chart_config_insert_item(
    request: Request,
    yaml_text: str = Form(""),
    kind: str = Form(""),
    before: str = Form("end"),
) -> dict:
    """Insert a header or separator into the layout (no modal needed)."""
    return _edited(request, "insert", yaml_text, lambda: config_edit.insert_layout_item(yaml_text, kind, before), kind=kind, before=before)


@app.post("/chart/config/move-item")
async def chart_config_move_item(
    request: Request,
    yaml_text: str = Form(""),
    index: int = Form(0),
    before: str = Form("end"),
) -> dict:
    """Move a header/separator (at layout-item `index`) to a new spot between rows."""
    return _edited(request, "move_item", yaml_text, lambda: config_edit.move_layout_item(yaml_text, index, before), index=index, before=before)


@app.post("/chart/config/delete-item")
async def chart_config_delete_item(request: Request, yaml_text: str = Form(""), index: int = Form(0)) -> dict:
    """Delete the header/separator at layout-item `index`."""
    return _edited(request, "delete_item", yaml_text, lambda: config_edit.delete_layout_item(yaml_text, index), index=index)


@app.post("/chart/config/move")
async def chart_config_move(
    request: Request,
    yaml_text: str = Form(""),
    src: str = Form(""),
    dst: str = Form(""),
    position: str = Form("before"),
) -> dict:
    """Move chart `src` next to `dst` (drag-and-drop). Returns {ok, yaml}."""
    return _edited(request, "move", yaml_text, lambda: config_edit.move_placement(yaml_text, src, dst, position), src=src, dst=dst, position=position)


@app.post("/chart/config/merge-down")
async def chart_config_merge_down(request: Request, yaml_text: str = Form(""), cid: str = Form("")) -> dict:
    """Extend `cid`'s own span down by one row (merge current row + 1 below).
    Returns {ok, yaml}."""
    return _edited(request, "merge", yaml_text, lambda: config_edit.merge_down(yaml_text, cid), cid=cid)


@app.post("/chart/config/new-row")
async def chart_config_new_row(
    request: Request,
    yaml_text: str = Form(""), src: str = Form(""), before: str = Form("end")
) -> dict:
    """Move chart `src` into its own new row at `before` (drag onto a row gap)."""
    return _edited(request, "new_row", yaml_text, lambda: config_edit.move_to_new_row(yaml_text, src, before), src=src, before=before)


@app.post("/chart/config/resize-columns")
async def chart_config_resize_columns(
    request: Request,
    yaml_text: str = Form(""), ordinals: str = Form(""), widths: str = Form("")
) -> dict:
    """Rewrite a merge group's column widths after a column-boundary drag.
    `ordinals` and `widths` are comma-separated. Returns {ok, yaml}."""
    def resize() -> str:
        ords = [int(o) for o in ordinals.split(",") if o != ""]
        ws = [float(w) for w in widths.split(",") if w != ""]
        return config_edit.resize_columns(yaml_text, ords, ws)
    return _edited(request, "resize", yaml_text, resize, ordinals=ordinals, widths=widths)


@app.post("/chart/config/header")
async def chart_config_header(
    request: Request,
    yaml_text: str = Form(""), index: int = Form(0), text: str = Form("")
) -> dict:
    """Rename the `index`-th layout header. Returns {ok, yaml} or error."""
    return _edited(request, "header", yaml_text, lambda: config_edit.set_header_text(yaml_text, index, text), index=index, text=text)


@app.post("/chart/config/delete")
async def chart_config_delete(request: Request, yaml_text: str = Form(""), cid: str = Form("")) -> dict:
    """Delete a chart (block + layout placements). Returns {ok, yaml} or error."""
    return _edited(request, "delete_chart", yaml_text, lambda: config_edit.delete_chart(yaml_text, cid), cid=cid)


# --- tabs --------------------------------------------------------------------
# The tab bar's editor gestures. Each is a thin wrapper over a config_edit tab
# function returning {ok, yaml}; the browser swaps the YAML in and re-runs.


@app.post("/chart/config/tab-add-first")
async def chart_config_tab_add_first(request: Request, yaml_text: str = Form("")) -> dict:
    """Wrap a flat dashboard in its first tab. Returns {ok, yaml}."""
    return _edited(request, "tab_add", yaml_text, lambda: config_edit.add_first_tab(yaml_text))


@app.post("/chart/config/tab-insert")
async def chart_config_tab_insert(
    request: Request,
    yaml_text: str = Form(""), before: str = Form("end")
) -> dict:
    """Add a tab that splits the current one at layout-item `before`."""
    return _edited(request, "tab_insert", yaml_text, lambda: config_edit.insert_tab(yaml_text, before), before=before)


@app.post("/chart/config/tab-rename")
async def chart_config_tab_rename(
    request: Request,
    yaml_text: str = Form(""), index: int = Form(0), name: str = Form("")
) -> dict:
    """Rename the `index`-th tab. Returns {ok, yaml} or error."""
    return _edited(request, "tab_rename", yaml_text, lambda: config_edit.set_tab_text(yaml_text, index, name), index=index, name=name)


@app.post("/chart/config/tab-move")
async def chart_config_tab_move(
    request: Request,
    yaml_text: str = Form(""), index: int = Form(0), before: str = Form("end")
) -> dict:
    """Move the `index`-th tab to layout-item gap `before`. Returns {ok, yaml}."""
    return _edited(request, "tab_move", yaml_text, lambda: config_edit.move_tab(yaml_text, index, before), index=index, before=before)


@app.post("/chart/config/tab-reorder")
async def chart_config_tab_reorder(
    request: Request,
    yaml_text: str = Form(""), index: int = Form(0), to: int = Form(0)
) -> dict:
    """Move the `index`-th tab, rows and all, to before tab `to` (the tab count:
    after the last). Returns {ok, yaml}."""
    return _edited(request, "tab_reorder", yaml_text, lambda: config_edit.reorder_tab(yaml_text, index, to), index=index, to=to)


@app.post("/chart/config/tab-delete")
async def chart_config_tab_delete(
    request: Request,
    yaml_text: str = Form(""), index: int = Form(0)
) -> dict:
    """Delete the `index`-th tab (first tab dissolves all). Returns {ok, yaml}."""
    return _edited(request, "tab_delete", yaml_text, lambda: config_edit.delete_tab(yaml_text, index), index=index)


@app.post("/chart/config/save")
async def chart_config_save(request: Request) -> dict:
    """Apply the submitted modal form to the chart's YAML block.

    Returns {ok, yaml} on success (the browser swaps it into the editor and
    re-renders) or {ok:false, error} if the resulting YAML wouldn't parse."""
    form = await request.form()
    yaml_text = form.get("yaml_text", "")
    cid = form.get("cid", "")
    return _edited(request, "edit_chart", yaml_text, lambda: config_edit.apply_edit(yaml_text, cid, form),
                   **_form_detail(form))


@app.post("/chart/config/preview", response_class=HTMLResponse)
async def chart_config_preview(request: Request) -> str:
    """The chart as the modal's fields would make it, drawn beside the form as
    they change: the form Save (`cid`) or Add (none) would post. Nothing is
    saved, or logged — a keystroke isn't a gesture. Read as the dashboard is,
    through the viewer's access.

    The preview's own controls — a table's pages and search, a legend's pages,
    a bar's time grain — come back here (editor.js) with what they'd send
    `/dashboard/cell`, and `part=cell` for just the cell they swap. A grain is
    the preview's alone: no out-of-band state for the page, as a cell's has."""
    form = await request.form()
    grains = form.getlist("grain")
    if form.get("set_grain"):
        grains = set_grain_token(grains, form["set_grain"])
    try:
        text, cid = config_edit.edited_chart(form.get("yaml_text", ""), form, form.get("cid", ""))
        dashboard, _, _ = _dashboard_for(request, text, "", "")
        cell = dashboard.render_cell(
            cid, grain_tokens=grains,
            legend_page=int(form.get("legend_page") or 0),
            table_page=int(form.get("table_page") or 1),
            table_query=form.get("table_q", ""),
        )
    except (config_edit.ConfigEditError, DashboardError, ValueError) as exc:
        return f'<div class="ff-preview-error" role="alert">{escape(str(exc))}</div>'
    if form.get("part") == "cell":
        return cell
    # The cell in a one-cell dashboard row, so the dashboard's look (and its
    # theme's colours) apply and the chart fills the pane as it fills its slot.
    return ('<div class="fireflyer-dashboard ff-preview-board">'
            '<div class="fireflyer-dashboard-row" style="grid-template-columns: 1fr; grid-template-rows: 100%;">'
            f"{cell}</div></div>")


# --- Calcs manager ---------------------------------------------------------
#
# A modal-like overlay (toggled from the topbar, styled like the docs overlay)
# lists a dashboard's calcs per dataset and adds / edits / deletes them.
# Pure logic lives in `calcs_edit`; these routes just thread the current YAML
# and the dataset's columns (for the filter builder) through it.


@app.post("/calcs/manager", response_class=HTMLResponse)
async def calcs_manager(yaml_text: str = Form("")) -> str:
    """The overlay body: calcs grouped by dataset with edit/delete controls."""
    return calcs_edit.render_manager(yaml_text)


@app.post("/calcs/form", response_class=HTMLResponse)
async def calcs_form(
    request: Request,
    yaml_text: str = Form(""),
    dataset: str = Form(""),
    key: str = Form(""),
) -> str:
    """The add/edit calc form (prefilled when `key` names an existing one)."""
    source = _reader(request).source
    columns = config_edit._columns(dataset, source)
    return calcs_edit.render_form(
        yaml_text, dataset, key, columns=columns,
        column_values=lambda column: config_edit.column_values(
            yaml_text, dataset, column, source
        ),
        column_types=config_edit.column_types(yaml_text, dataset, source),
        column_extent=lambda column: config_edit.column_extent(yaml_text, dataset, column, source),
    )


@app.post("/calcs/save")
async def calcs_save(request: Request) -> dict:
    """Add or replace a calc. Returns {ok, yaml} for the browser to swap into
    the editor, else {ok:false, error}."""
    form = await request.form()
    yaml_text = form.get("yaml_text", "")
    dataset = form.get("dataset", "")
    key = form.get("key", "")
    original_key = form.get("original_key", "")
    return _edited(request, "calc_save", yaml_text, lambda: calcs_edit.upsert_calc(
        yaml_text, dataset, key, calcs_edit.definition_from_form(form), original_key=original_key,
    ), **_form_detail(form))


@app.post("/calcs/delete")
async def calcs_delete(request: Request) -> dict:
    """Delete a calc (guarded if a chart still references it)."""
    form = await request.form()
    yaml_text = form.get("yaml_text", "")
    return _edited(request, "calc_delete", yaml_text, lambda: calcs_edit.delete_calc(
        yaml_text, form.get("dataset", ""), form.get("key", ""),
    ), **_form_detail(form))


# --- moving into groups ---------------------------------------------------------------
# Portal mode, at start-up: what's from before groups goes into the admin's —
# the dashboards' and datasets' rows did as their stores opened; the datasets'
# files move here — and a dataset whose name the rule refuses gets the nearest
# one it allows, every dashboard reading it following.


def _move_into_groups() -> None:
    app.state.datasets.move_into_group(groups.ADMIN_GROUP)
    store = app.state.datasets.group(groups.ADMIN_GROUP)
    taken = set(store.names())
    for name in sorted(taken):
        try:
            groups.check_name(name, "dataset")
            continue
        except groups.GroupError:
            pass
        new, n = fit_name(name), 2
        while new in taken:
            new, n = f"{fit_name(name)}-{n}", n + 1
        store.rename(name, new)
        taken = (taken - {name}) | {new}
        _cascade(lambda where, text: _rename_dataset(text, where, groups.ADMIN_GROUP, name, new),
                 auth_mod.admin_name())                  # no one asked: the admin login did


if PORTAL_ENABLED:
    _move_into_groups()


# --- usage analytics -----------------------------------------------------------------
# Shipped with the portal (`web/usage.py`): a connection `internal` to the
# portal's own database, datasets on it and the Fireflyer Usage dashboard, in
# the admin's group. Made once — when that group has none of the datasets — and
# never again: after that they're the admin's to change or remove.


def _set_up_usage() -> None:
    datasets = app.state.datasets.group(groups.ADMIN_GROUP)
    if set(datasets.names()) & set(usage_mod.DATASETS):
        return
    admin = auth_mod.admin_name()
    connections = _connections()
    if connections.get(usage_mod.CONNECTION) is None:
        connections.create(usage_mod.CONNECTION, usage_mod.connection_url(_DATABASE_URL), description=usage_mod.CONNECTION_DESCRIPTION,
                           author=admin)
    for name, (description, sql) in usage_mod.DATASETS.items():
        source = {"connection": usage_mod.CONNECTION, "query": sql}
        try:
            datasets.create_query(name, _run_query(source), source, description=description, author=admin)
        except (connections_mod.ConnectionsError, DatasetError) as exc:
            print(f"usage analytics: {name}: {exc}", flush=True)
    dashboards = _dashboards_in(groups.ADMIN_GROUP)
    if dashboards is not None and dashboards.get(usage_mod.DASHBOARD_NAME) is None:
        dashboards.create(usage_mod.DASHBOARD, author=admin)


if PORTAL_ENABLED:
    try:
        _set_up_usage()
    except Exception:                    # never in the way of starting
        traceback.print_exc()
