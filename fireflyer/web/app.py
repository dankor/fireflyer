import hmac
import json
import os
import pathlib
import re
import threading
import traceback
from html import escape

from dotenv import load_dotenv
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

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
from fireflyer.datasets import DatasetError, DatasetStore, SqlCatalog
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
    rename_column_refs,
    rename_dataset_ref,
    set_grain_token,
)
from fireflyer.web import assets
from fireflyer.web import auth as auth_mod
from fireflyer.web import chat as chat_mod
from fireflyer.web import portal as portal_mod
from fireflyer.web import paths as paths_mod

# Load .env (ANTHROPIC_API_KEY) before reading it. The AI assistant is enabled
# only when a key is present; otherwise the editor shows a setup notice.
load_dotenv()
CHAT_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))

# Portal mode (owner-approved exception to the no-persistence anti-goal, scoped
# to web/): when FIREFLYER_PORTAL is set, `/` becomes a gallery of dashboards
# stored in a DB and each opens in the existing editor. A DATABASE_URL selects
# Postgres; otherwise a local sqlite file. Off by default — `/` is the usual
# single-dashboard editor. Tests set `app.state.store` directly.
PORTAL_ENABLED = bool(os.environ.get("FIREFLYER_PORTAL"))
PORTAL_TITLE = os.environ.get("FIREFLYER_PORTAL_TITLE", "Fireflyer Portal")

app = FastAPI()
app.mount("/static", StaticFiles(directory=assets.STATIC_DIR), name="static")
app.state.store = (
    portal_mod.make_store(os.environ.get("DATABASE_URL")) if PORTAL_ENABLED else None
)
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
    SqlCatalog(os.environ.get("DATABASE_URL") or "portal.db") if PORTAL_ENABLED else None,
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

# Topbar pieces. Nav is a hamburger link to the dashboards gallery; the brand is
# a link there too in portal mode (a plain span locally, where `/` is the editor
# itself). The save button/name-edit/theme JS all live in static/editor.js.
# Back arrow to the gallery — the opened-dashboard editor mirrors the dataset
# detail page (same shared `back_button` style, no brand text).
_BACK_HTML = portal_mod.back_button("/", "Back to dashboards")


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


def _save_html(dash_id: str) -> str:
    # Both hidden until there are unsaved changes (toggled by updateSaveState()):
    # Discard puts the last save back, Save keeps the changes.
    # One segmented group, styled like the mode switch's resting segments.
    return (
        '<span class="ff-savebar" role="group" aria-label="Unsaved changes">'
        '<button class="ff-discard" id="ff-discard" type="button" title="Discard unsaved changes"'
        f' aria-label="Discard unsaved changes" hidden>{_DISCARD_ICON}</button>'
        f'<button class="ff-save" id="ff-save" data-save-url="/d/{dash_id}/save" title="Save (⌘/Ctrl+S)"'
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


def _mode_switch() -> str:
    buttons = "".join(
        f'<button type="button" data-mode="{mode}" title="{_MODE_LABELS[mode]}"'
        f' aria-label="{_MODE_LABELS[mode]}">{_MODE_ICONS[mode]}</button>'
        for mode in ("view", "edit", "code", "diff", "chat", "calcs", "docs")
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
    path_dropdown: str = "",
    save: str = "",
    theme: str = "",
    user_menu: str = "",
    dash_id: str = "",
) -> str:
    """The editor page seeded with `yaml_text` and topbar pieces — left: `nav`
    (back button + switch) + editable `dash_name`; right: `path_dropdown` (local
    paths mode), `save` (shown only when unsaved), Preview, `theme` switch,
    `user_menu` (profile). `editor.html` carries the placeholders; both modes
    share it. Read per render rather than at import, like the CSS and JS it
    links: uvicorn's `--reload` ignores non-Python files, so an edit to any of
    them shows on the next page load without a restart."""
    page = (_DIR / "editor.html").read_text()
    # nav + profile are shared with the gallery, and come after the editor's own
    # rules — the order they were inlined in, so the cascade is unchanged.
    styles = assets.favicons() + "\n" + assets.stylesheets("editor.css", "filters.css", "code.css", "nav.css", "profile.css")
    return (
        page.replace("__FF_STYLES__", styles)
        .replace("__FF_SCRIPT__", assets.script("filter-builder.js") + "\n" + assets.script("editor.js")
                 + "\n" + assets.module("yaml-editor.js"))
        .replace("__FF_CHAT__", CHAT_PANEL)
        # Which dashboard: its version history, and the key its unsaved draft
        # is kept under in the browser ("" for the plain single-dashboard editor).
        .replace("__FF_DASH_ID__", escape(dash_id, quote=True))
        .replace("__FF_IMPORTMAP__", assets.import_map("diff-view.js", "yaml-look.js"))
        .replace("__FF_NAV__", nav)
        .replace("__FF_MODES__", _mode_switch())
        .replace("__FF_DASH_NAME__", dash_name)
        .replace("__FF_ROLES__", portal_mod.role_picker(_role_store().roles(), _users()))
        .replace("__FF_PATHDD__", path_dropdown)
        .replace("__FF_SAVE__", save)
        .replace("__FF_THEME__", theme)
        .replace("__FF_USER_MENU__", user_menu)
        .replace("__FF_DOCS__", _CHART_DOCS_HTML)
        # Last, so a dashboard whose YAML happens to contain a placeholder
        # can't have it filled in.
        .replace("__FF_YAML_CONTENT__", escape(yaml_text))
    )


# --- store selection (portal DB vs local paths) -----------------------------
# Local *paths mode* is on when FIREFLYER_PATHS is set (a base dir; each host
# folder mapped under it is a switchable "path"). A path's dashboards are files
# in <path>/dashboards; its datasets are an isolated blob store. The active path
# rides in a cookie.
PATHS_BASE = os.environ.get("FIREFLYER_PATHS", "")
_DATA_BASE = os.environ.get("FIREFLYER_DATA", "data/datasets")
_PATH_COOKIE = "ff_path"
# Seeded on first run so a fresh paths-mode checkout opens on a working example.
DEMO_PATH = "demo"


def _paths_mode() -> bool:
    return app.state.store is None and bool(PATHS_BASE)


def _active_path(request: Request) -> str | None:
    """The selected path (paths mode), validated against the real mapped-folder
    list so a stale/forged cookie can't point outside the base. Defaults to the
    first."""
    paths = paths_mod.list_paths(PATHS_BASE)
    want = request.cookies.get(_PATH_COOKIE)
    if want in paths:
        return want
    return paths[0] if paths else None


def _dash_store(request: Request):
    """Dashboard store for this request: portal DB, or the active path's folder
    store (None in paths mode when no path exists yet)."""
    if app.state.store is not None:
        return app.state.store
    path = _active_path(request)
    if not path:
        return None
    return paths_mod.PathDashboardStore(
        f"{PATHS_BASE}/{path}", history=f"{_DATA_BASE}/{path}/history",
        index=f"{_DATA_BASE}/{path}/uses.json",
    )


def _dataset_store(request: Request):
    """Dataset store for this request: portal singleton, or the active path's
    isolated blob store."""
    if not _paths_mode():
        return app.state.datasets
    path = _active_path(request)
    if path is None:
        return app.state.datasets
    return DatasetStore(make_object_store({"base": f"{_DATA_BASE}/{path}"}))


@app.get("/path/{name}")
def switch_path(name: str) -> RedirectResponse:
    """Switch the active path (paths mode) — sets the cookie, back to the gallery."""
    resp = RedirectResponse("/", status_code=303)
    if name in paths_mod.list_paths(PATHS_BASE):
        resp.set_cookie(_PATH_COOKIE, name, httponly=True, samesite="lax")
    return resp


def _page_title() -> str:
    """Browser-tab title for the gallery pages."""
    return PORTAL_TITLE if app.state.store is not None else "Fireflyer"


def _gallery_kwargs(request: Request) -> dict:
    """Mode args for the gallery/datasets pages: the path list + active path in
    paths mode, and whether to show an Author column — only with a login, since
    without one (local mode) there's nobody to name."""
    authors = {"authors": app.state.authenticator is not None}
    if not _paths_mode():
        return authors
    return {
        "paths": paths_mod.list_paths(PATHS_BASE),
        "active_path": _active_path(request),
        **authors,
    }




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
    if not found:
        return auth_mod.login_page("Invalid username or password.", PORTAL_TITLE)
    resp = RedirectResponse("/", status_code=303)
    try:
        sign_in(request, resp, *found)
    except portal_mod.RoleError:
        return auth_mod.login_page("Invalid username or password.", PORTAL_TITLE)
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
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    auth_mod.clear_session(resp)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    # Portal: DB gallery. Paths mode: gallery of the active path's dashboards
    # (with the path picker). Plain local: the single-dashboard editor.
    if app.state.store is not None:
        return portal_mod.render_gallery(
            app.state.store.list(), PORTAL_TITLE, _user_menu(request),
            datasets=_dataset_names(request), uses=_datasets_of(request), users=_users_tab(request),
        )
    if _paths_mode():
        store = _dash_store(request)
        return portal_mod.render_gallery(
            store.list() if store else [], _page_title(), "",
            datasets=_dataset_names(request), uses=_datasets_of(request),
            **_gallery_kwargs(request),
        )
    # Plain local mode: no gallery to go back to, no save/profile; just the
    # editable name and theme switch.
    return render_editor_page(
        DEFAULT_YAML,
        dash_name=_dash_name_html(*_name_and_description(DEFAULT_YAML)),
        theme=_theme_switch(),
    )


def _empty_yaml(name: str, description: str = "") -> str:
    """A blank but valid dashboard named `name` — no datasets, charts, or layout
    yet. The author fills it in the editor. json.dumps quotes the name (and the
    description, when given) safely."""
    about = f"description: {json.dumps(description)}\n" if description else ""
    return (
        f"name: {json.dumps(name)}\n{about}\n"
        "datasets: {}\n\ncharts: {}\n\nlayout: []\n"
    )


def _set_yaml_name(yaml_text: str, name: str) -> str:
    """Rewrite the top-level `name:` line (used when cloning). `name:` is a
    required column-0 key, so replacing the first such line suffices."""
    return re.sub(r"(?m)^name:.*$", f"name: {json.dumps(name)}", yaml_text, count=1)


def _current_author(request: Request) -> str:
    """The logged-in user, recorded as a dashboard's author. Empty when auth is
    off (local dev)."""
    if app.state.authenticator is None:
        return ""
    return auth_mod.current_user(request) or ""


@app.post("/new")
async def portal_new(
    request: Request, name: str = Form(""), description: str = Form("")
) -> RedirectResponse:
    yaml_text = _empty_yaml(name.strip() or "Untitled dashboard", description.strip())
    new_id = _dash_store(request).create(yaml_text, _current_author(request))
    return RedirectResponse(f"/d/{new_id}", status_code=303)


@app.post("/d/{dash_id}/clone")
async def portal_clone(dash_id: str, request: Request, name: str = Form("")):
    store = _dash_store(request)
    row = store.get(dash_id)
    if row is None:
        return HTMLResponse("Dashboard not found", status_code=404)
    new_name = name.strip() or f"{row.name} (copy)"
    new_id = store.create(
        _set_yaml_name(row.yaml, new_name), _current_author(request)
    )
    return RedirectResponse(f"/d/{new_id}", status_code=303)


@app.get("/d/{dash_id}", response_class=HTMLResponse)
def portal_open(dash_id: str, request: Request) -> HTMLResponse:
    row = _dash_store(request).get(dash_id)
    if row is None:
        return HTMLResponse("Dashboard not found", status_code=404)
    # The theme switch sits in the topbar always, beside the profile (portal).
    user_menu, theme = _user_menu(request), _theme_switch()
    # A selected item (like the dataset detail): back button + the editable name;
    # no Dashboards|Datasets switch (that's for the lists). In local paths mode the
    # path dropdown rides on the right.
    nav, path_dd = _BACK_HTML, ""
    if _paths_mode():
        kw = _gallery_kwargs(request)
        path_dd = portal_mod.path_dropdown(kw["paths"], kw["active_path"])
    page = render_editor_page(
        row.yaml,
        nav=nav,
        dash_name=_dash_name_html(row.name, row.description),
        path_dropdown=path_dd,
        save=_save_html(row.id),
        user_menu=user_menu,
        theme=theme,
        dash_id=row.id,
    )
    return HTMLResponse(page)


@app.post("/d/{dash_id}/save")
async def portal_save(
    dash_id: str, request: Request, yaml_text: str = Form("")
) -> dict:
    try:
        _dash_store(request).save(dash_id, yaml_text, author=_current_author(request))
        return {"ok": True}
    except DashboardError as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/d/{dash_id}/versions")
def dashboard_versions(dash_id: str, request: Request):
    """The dashboard's saved versions, newest first: number, Unix time, author.
    The compare view lists them; it fetches one's YAML when it's picked."""
    store = _dash_store(request)
    if store is None or store.get(dash_id) is None:
        return JSONResponse({"error": "no such dashboard"}, status_code=404)
    return {"versions": [
        {"version": v.version, "created_at": v.created_at, "author": v.author}
        for v in store.versions(dash_id)
    ]}


@app.get("/d/{dash_id}/versions/{number}")
def dashboard_version(dash_id: str, number: int, request: Request):
    store = _dash_store(request)
    found = store.version(dash_id, number) if store is not None else None
    if found is None:
        return JSONResponse({"error": "no such version"}, status_code=404)
    return {"version": found.version, "created_at": found.created_at,
            "author": found.author, "yaml": found.yaml}


@app.post("/d/{dash_id}/delete")
async def portal_delete(dash_id: str, request: Request) -> RedirectResponse:
    _dash_store(request).delete(dash_id)
    return RedirectResponse("/", status_code=303)


# --- datasets (portal gallery tab) ------------------------------------------


def _uses(request: Request) -> list:
    """What every dashboard reads — the store's index (`Use` rows, written on
    save), so the overviews below are lookups, not a parse of every dashboard.
    Empty with no dashboard store (plain local mode)."""
    store = _dash_store(request)
    return store.uses() if store is not None else []


def _dashboards_using(request: Request, name: str) -> list[tuple[str, str]]:
    """(id, name) of stored dashboards that reference dataset `name`."""
    return list(dict.fromkeys(
        (u.dashboard_id, u.dashboard_name) for u in _uses(request) if u.dataset == name
    ))


def _columns_used_in(request: Request, name: str, columns: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Each column of dataset `name` -> the (id, name) of every dashboard whose
    charts read it (a raw table, `*`, reads them all)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for u in _uses(request):
        if u.dataset != name or not u.column:
            continue
        for column in columns if u.column == "*" else [u.column]:
            if (u.dashboard_id, u.dashboard_name) not in out.setdefault(column, []):
                out[column].append((u.dashboard_id, u.dashboard_name))
    return out


def _dataset_descriptions(request: Request) -> dict[str, str]:
    """Name -> description of the uploaded datasets — what a chart form's
    dataset field offers as you type."""
    return {d.name: d.description for d in _dataset_store(request).list()}


def _dataset_names(request: Request) -> set[str]:
    """The uploaded datasets' names, for the dashboards list's links."""
    return {d.name for d in _dataset_store(request).list()}


def _datasets_of(request: Request) -> dict[str, list[str]]:
    """Dashboard id -> the datasets it reads, for the dashboards list."""
    out: dict[str, list[str]] = {}
    for u in _uses(request):
        if not u.column:
            out.setdefault(u.dashboard_id, []).append(u.dataset)
    return out


def _dataset_users(request: Request) -> dict[str, list[tuple[str, str]]] | None:
    """Dataset name -> (id, name) of each dashboard reading it, for the datasets
    list. None with no dashboard store (plain local mode)."""
    if _dash_store(request) is None:
        return None
    users: dict[str, list[tuple[str, str]]] = {}
    for u in _uses(request):
        if not u.column:
            users.setdefault(u.dataset, []).append((u.dashboard_id, u.dashboard_name))
    return users


@app.get("/datasets", response_class=HTMLResponse)
def datasets_gallery(request: Request) -> str:
    return portal_mod.render_datasets(
        _dataset_store(request).list(), _page_title(), _user_menu(request),
        used_by=_dataset_users(request), users=_users_tab(request), **_gallery_kwargs(request),
    )


# --- access (roles) -------------------------------------------------------------
# In a database in every mode: the portal's, or a local sqlite file (shared by
# every path) when there's no portal. Opened on first use, so importing the app
# — as the tests do — creates no file. Tests set `app.state.roles`.
app.state.roles = None


def _role_store():
    if app.state.roles is None:
        local = f"{_DATA_BASE}/fireflyer.db"
        dsn = (os.environ.get("DATABASE_URL") or "portal.db") if PORTAL_ENABLED else local
        # Portal mode has people: the login is the built-in admin user.
        app.state.roles = portal_mod.RoleStore(dsn, admin=auth_mod.admin_name() if PORTAL_ENABLED else "")
    return app.state.roles


def _every_dataset_store() -> list[DatasetStore]:
    """Every dataset store a role can be named in: the one, or each path's —
    roles are shared by every path."""
    if not _paths_mode():
        return [app.state.datasets]
    return [DatasetStore(make_object_store({"base": f"{_DATA_BASE}/{p}"}))
            for p in paths_mod.list_paths(PATHS_BASE)]


@app.get("/access", response_class=HTMLResponse)
def access_gallery(request: Request) -> str:
    kwargs = _gallery_kwargs(request)
    return portal_mod.render_access(
        _role_store().roles(), _page_title(), _user_menu(request),
        paths=kwargs.get("paths"), active_path=kwargs.get("active_path"),
        datasets=_datasets_by_role(request), users=_users_tab(request),
    )


def _datasets_by_role(request: Request) -> dict[str, list[str]]:
    """Role name -> the datasets whose (published) permissions name it — the
    Access list's Datasets column. This path's datasets, in paths mode."""
    store = _dataset_store(request)
    out: dict[str, list[str]] = {}
    for ds in store.list():
        try:
            roles = store.permissions(ds.name)
        except DatasetError:
            continue
        for role in roles:
            out.setdefault(role, []).append(ds.name)
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
    if _users_on() and not _is_admin(request):
        return HTMLResponse("Only an admin manages roles", status_code=403)
    try:
        _role_store().create_role(name, description)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/access", status_code=303)


def _role_named(role_id: str) -> str | None:
    return next((r.name for r in _role_store().roles() if r.id == role_id), None)


@app.post("/access/{role_id}/rename")
async def access_rename(role_id: str, request: Request, name: str = Form(""),
                        description: str | None = Form(None)):
    if _users_on() and not _is_admin(request):
        return HTMLResponse("Only an admin manages roles", status_code=403)
    old = _role_named(role_id)
    try:
        _role_store().rename_role(role_id, name, description)
    except portal_mod.RoleError as exc:
        return _error(exc)
    # Datasets name their roles in `permissions:`: they follow the new name.
    new = _role_named(role_id)
    if old and new != old:
        for datasets in _every_dataset_store():
            datasets.rename_role(old, new, author=_current_author(request))
    return RedirectResponse("/access", status_code=303)


@app.post("/access/{role_id}/delete")
async def access_delete(role_id: str, request: Request):
    if _users_on() and not _is_admin(request):
        return HTMLResponse("Only an admin manages roles", status_code=403)
    name = _role_named(role_id)
    try:
        _role_store().delete_role(role_id)
    except portal_mod.RoleError as exc:
        return _error(exc)
    if name:
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
    """Dataset -> its role -> filters, over every dataset store (roles and
    users are shared by every path) — what a user's roles are checked against."""
    out: dict[str, dict] = {}
    for store in _every_dataset_store():
        for ds in store.list():
            try:
                out[ds.name] = _with_admin(store.permissions(ds.name))
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


def _check_roles(user: str, role_ids: list[str]) -> None:
    """RoleError when the roles `role_ids` name would read a dataset twice."""
    names = [r.name for r in _role_store().roles() if r.id in role_ids]
    clashes = portal_mod.role_clashes(names, _every_permission())
    if clashes:
        raise portal_mod.clash_error(user.strip(), clashes)


def _check_permissions(dataset: str, mapping: str) -> None:
    """RoleError when `mapping`'s permissions would give a user two roles on
    `dataset` — the other side of `_check_roles`."""
    if not _users_on():
        return
    perms = {dataset: _with_admin(column_map.permissions(mapping))}
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
    """The list pages' Users segment: portal mode, and only for an admin."""
    return _users_on() and _is_admin(request)


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


def _apply_password(user_id: str, change: str | None) -> None:
    if change is None:
        return
    if change:
        _role_store().set_password(user_id, change)
    else:
        _role_store().clear_password(user_id)


@app.get("/users", response_class=HTMLResponse)
def users_gallery(request: Request):
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
    if refused := _admins_only(request):
        return refused
    try:
        change = _password_change(password, "")
        _check_roles(name, role)
        user_id = _role_store().create_user(name, role, first_name, last_name, email)
        _apply_password(user_id, change)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/edit")
async def users_edit(user_id: str, request: Request, name: str = Form(""),
                     role: list[str] = Form(default=[]), first_name: str = Form(""),
                     last_name: str = Form(""), email: str = Form(""),
                     password: str = Form(""), clear_password: str = Form("")):
    if refused := _admins_only(request):
        return refused
    try:
        change = _password_change(password, clear_password)
        _check_roles(name, role)
        _role_store().edit_user(user_id, name, role, first_name, last_name, email)
        _apply_password(user_id, change)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/delete")
async def users_delete(user_id: str, request: Request):
    if refused := _admins_only(request):
        return refused
    try:
        _role_store().delete_user(user_id)
    except portal_mod.RoleError as exc:
        return _error(exc)
    return RedirectResponse("/users", status_code=303)


# --- dataset builds --------------------------------------------------------------
# An upload or a new column mapping is a new version, computed in the
# background (`DatasetStore.build`): the request returns at once, the page shows
# "building", and charts keep reading the previous file until it's done. One
# build at a time per process; a version left waiting by a restart is picked up
# when its page asks for its status.
_build_lock = threading.Lock()
_building: set[str] = set()


def _build_key(request: Request, name: str) -> str:
    # Per path in paths mode: each path's datasets are their own.
    return f"{_active_path(request) if _paths_mode() else ''}:{name}"


def _build(key: str, datasets: DatasetStore, dashboards, name: str, author: str) -> None:
    with _build_lock:
        _building.add(key)
        try:
            renames = datasets.build(name)
        finally:
            _building.discard(key)
    # Renamed columns: rewrite the dashboards that use them, as a dataset
    # rename rewrites its name.
    if renames and dashboards is not None:
        for row in dashboards.list():
            text = rename_column_refs(row.yaml, name, renames)
            if text != row.yaml:
                dashboards.save(row.id, text, author=author)


def _build_later(background: BackgroundTasks, request: Request, name: str) -> None:
    background.add_task(
        _build, _build_key(request, name), _dataset_store(request), _dash_store(request),
        name, _current_author(request),
    )


def _error(exc: Exception, status: int = 400) -> HTMLResponse:
    return HTMLResponse(f'<pre class="error">{escape(str(exc))}</pre>', status_code=status)


@app.post("/datasets/new")
async def datasets_new(
    request: Request,
    background: BackgroundTasks,
    name: str = Form(""),
    description: str = Form(""),
    delimiter: str = Form(","),
    file: UploadFile = File(...),
):
    try:
        ds = _dataset_store(request).create(
            name,
            await file.read(),
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
    return RedirectResponse(f"/datasets/{quote(ds.name)}", status_code=303)


@app.get("/datasets/{name}", response_class=HTMLResponse)
def dataset_detail(name: str, request: Request):
    datasets = _dataset_store(request)
    ds = datasets.get(name)
    if ds is None:
        return HTMLResponse("Dataset not found", status_code=404)
    return HTMLResponse(
        portal_mod.render_dataset_detail(
            ds, _page_title(), _user_menu(request),
            used_by=_dashboards_using(request, name),
            mapping=datasets.mapping(name),
            versions=datasets.versions(name),
            examples=datasets.examples(name),
            used_in=_columns_used_in(request, name, [c.name for c in ds.columns]),
            **_gallery_kwargs(request),
        )
    )


@app.post("/datasets/{name}/replace")
async def dataset_replace(
    name: str,
    request: Request,
    background: BackgroundTasks,
    description: str = Form(""),
    delimiter: str = Form(","),
    file: UploadFile = File(...),
):
    try:
        _dataset_store(request).replace(
            name, await file.read(),
            description=description or None, delimiter=delimiter or ",",
            author=_current_author(request), build=False,
        )
    except DatasetError as exc:
        return _error(exc)
    _build_later(background, request, name)
    return RedirectResponse(f"/datasets/{quote(name)}", status_code=303)


@app.post("/datasets/{name}/mapping")
async def dataset_mapping(
    name: str, request: Request, background: BackgroundTasks, mapping: str = Form("")
) -> JSONResponse:
    """A new column mapping (the YAML) — the next minor, built in the background."""
    try:
        _check_permissions(name, mapping)
        ds = _dataset_store(request).remap(
            name, mapping, author=_current_author(request), build=False
        )
    except (DatasetError, column_map.MappingError, portal_mod.RoleError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    _build_later(background, request, name)
    return JSONResponse({"building": ds.building})


@app.post("/datasets/{name}/mapping/check")
async def dataset_mapping_check(name: str, request: Request, mapping: str = Form("")) -> JSONResponse:
    """Read a mapping's YAML without saving it — the page's form shows what the
    code editor says. 400 with the reason when it doesn't read or fit."""
    datasets = _dataset_store(request)
    try:
        sources = datasets.versions(name)[0].sources
        rules = column_map.parse(mapping, sources)
    except (DatasetError, column_map.MappingError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"rules": [
        {"name": r.name, "formula": r.formula, "service": r.service} for r in rules
    ]})


@app.get("/datasets/{name}/status")
def dataset_status(name: str, request: Request, background: BackgroundTasks) -> JSONResponse:
    """What the page polls while a version builds."""
    datasets = _dataset_store(request)
    ds = datasets.get(name)
    if ds is None:
        return JSONResponse({"error": "unknown dataset"}, status_code=404)
    if ds.building and _build_key(request, name) not in _building:
        _build_later(background, request, name)
    return JSONResponse({"version": ds.version, "building": ds.building, "error": ds.error})


@app.get("/datasets/{name}/versions")
def dataset_versions(name: str, request: Request) -> JSONResponse:
    try:
        versions = _dataset_store(request).versions(name)
    except DatasetError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"versions": [
        {"version": v.label, "created_at": v.created_at, "author": v.author,
         "status": v.status, "rows": v.rows}
        for v in versions
    ]})


@app.get("/datasets/{name}/versions/{label}")
def dataset_version(name: str, label: str, request: Request) -> JSONResponse:
    try:
        v = _dataset_store(request).version(name, label)
    except DatasetError:
        v = None
    if v is None:
        return JSONResponse({"error": "unknown version"}, status_code=404)
    return JSONResponse({"version": v.label, "created_at": v.created_at, "author": v.author,
                         "status": v.status, "rows": v.rows, "mapping": v.mapping})


@app.get("/datasets/{name}/versions/{label}/restore")
def dataset_version_restore(name: str, label: str, request: Request) -> JSONResponse:
    """A version's mapping fitted to the latest upload — for the compare view's
    restore, which loads it as an unsaved edit (saving builds it anew)."""
    try:
        return JSONResponse({"mapping": _dataset_store(request).restored(name, label)})
    except DatasetError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/datasets/{name}/versions/{major}/delete")
def dataset_delete_major(name: str, major: int, request: Request):
    try:
        _dataset_store(request).delete_major(name, major)
    except DatasetError as exc:
        return _error(exc)
    return RedirectResponse(f"/datasets/{quote(name)}", status_code=303)


@app.post("/datasets/{old_name}/rename")
async def dataset_rename(
    old_name: str, request: Request, name: str = Form(""), description: str = Form(""),
    next: str = Form(""),
):
    """Rename and/or re-describe a dataset. From the dataset's own page
    (`next=detail`, its title card) back to that page, at the new name;
    otherwise to the list."""
    new = name.strip()
    try:
        _dataset_store(request).rename(old_name, new, description=description)
    except DatasetError as exc:
        return HTMLResponse(f'<pre class="error">{escape(str(exc))}</pre>', status_code=400)
    # Cascade: rewrite every dashboard that referenced the old name.
    store = _dash_store(request)
    if store is not None and new != old_name:
        for row in store.list():
            if old_name in Dashboard.dataset_names(row.yaml):
                store.save(
                    row.id, rename_dataset_ref(row.yaml, old_name, new),
                    author=_current_author(request),
                )
    back = f"/datasets/{quote(new)}" if next == "detail" else "/datasets"
    return RedirectResponse(back, status_code=303)


@app.post("/datasets/{name}/access/form", response_class=HTMLResponse)
async def dataset_access(name: str, request: Request, mapping: str = Form("")):
    """The dataset page's permissions mode: the roles the posted mapping's
    `permissions:` name, as a form, with a + for the rest — built from the YAML
    being edited, so it shows exactly what Save would keep."""
    store = _dataset_store(request)
    try:
        access = column_map.permissions(mapping)
    except column_map.MappingError as exc:
        return _error(exc)
    context = config_edit.dataset_context(name, store.source)
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


@app.post("/datasets/{name}/access/rows")
async def dataset_access_rows(name: str, request: Request) -> JSONResponse:
    """How many rows a role with the posted filters would see — the Rows
    column, recounted as its filters change."""
    try:
        count = _dataset_store(request).rows_seen(name, parse_filters(await request.form()))
    except (filters_mod.FilterError, DatasetError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"rows": count})


@app.post("/datasets/{name}/access/yaml")
async def dataset_access_yaml(name: str, request: Request) -> JSONResponse:
    """The permissions form, written into the posted mapping's `permissions:` —
    the YAML Save then keeps as the next version. Only roles that exist, and
    never two that one user holds."""
    form = await request.form()
    known = {r.name for r in _assignable(_role_store().roles())}
    try:
        grants = portal_mod.parse_access_form(form)
        mapping = column_map.with_permissions(
            form.get("mapping", ""), {r: fs for r, fs in grants.items() if r in known},
        )
        _check_permissions(name, mapping)
    except (filters_mod.FilterError, column_map.MappingError, portal_mod.RoleError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"mapping": mapping})


@app.post("/datasets/{name}/access/filter", response_class=HTMLResponse)
async def dataset_access_filter(name: str, request: Request):
    """The filter built in a role's draft row, as the compact row its list
    shows — or why it isn't a filter yet."""
    rows = parse_filters(await request.form())
    try:
        filters_mod.normalize(rows[:1])
    except filters_mod.FilterError as exc:
        return _error(exc)
    if not rows:
        return _error(ValueError("pick a column and its values"))
    context = config_edit.dataset_context(name, _dataset_store(request).source)
    return portal_mod.access_filter_row(rows[0], context.column_types)


@app.post("/datasets/{name}/delete")
async def dataset_delete(name: str, request: Request):
    used = _dashboards_using(request, name)
    if used:  # guard: don't orphan dashboards
        names = ", ".join(n for _, n in used)
        msg = f"Cannot remove {name!r}: still used by {len(used)} dashboard(s): {names}"
        return HTMLResponse(f'<pre class="error">{escape(msg)}</pre>', status_code=409)
    _dataset_store(request).delete(name)
    return RedirectResponse("/datasets", status_code=303)


@app.post("/execute")
async def execute(request: Request, active_tab: int = 0, f: str = "", role: str = "",
                  user: str = "") -> dict:
    """`f` is the page URL's filter state (see `filters.encode_state`): the
    editor passes it on every render, so a reload, a shared link, or a YAML edit
    all keep the filters in play."""
    body = (await request.body()).decode("utf-8")
    try:
        dashboard = _as_role(request, Dashboard.from_yaml(body, datasets=_dataset_store(request)), role, user)
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
        return {"ok": False, "html": f'<pre class="error">{escape(str(exc))}</pre>'}
    except Exception:
        return {
            "ok": False,
            "html": f'<pre class="error">{escape(traceback.format_exc())}</pre>',
        }


def _as_role(request: Request, dashboard: Dashboard, role: str, user: str = "") -> Dashboard:
    """`dashboard` as `role` (a name) sees it — the editor's view-as picker —
    from the `permissions:` of this path's datasets. A role no dataset names
    sees no managed dataset at all. Or as `user` sees it: each dataset through
    the one role of theirs that may read it (`role_clashes` keeps it one); a
    user with no such role — or one since removed — sees none of it."""
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
        return {ds.name: [] for ds in datasets.list()}
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
    if not CHAT_ENABLED:
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
        return {"ok": True, **result}
    except Exception as exc:  # surface SDK/auth/network errors as a chat reply
        return {"ok": False, "reply": f"Assistant error: {exc}"}


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
            for d in _dataset_store(request).list()
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
    chart._resolve = _dataset_store(request).source  # dataset name -> Parquet source
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
    chart._resolve = _dataset_store(request).source  # dataset name -> Parquet source
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
    try:
        dashboard = Dashboard.from_yaml(yaml_text, datasets=_dataset_store(request))
    except DashboardError as exc:
        return f'<pre class="error">{escape(str(exc))}</pre>'
    new_tokens = filters_mod.toggle_token(list(cf), toggle) if toggle else list(cf)
    # A filter panel row's ✕: every token behind that row.
    new_tokens = [t for t in new_tokens if t not in remove]
    # A filter panel's + form: the chart builder's own fields, parsed by its own
    # parser, become global quick filters. Added, never toggled — submitting the
    # same filter twice must not remove it; its row's ✕ does that. One the
    # model rejects (a `between` without exactly two bounds) is dropped rather
    # than kept as a token nothing would ever apply or show.
    for f in parse_filters(await request.form()):
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
    try:
        dashboard = _as_role(request, Dashboard.from_yaml(yaml_text, datasets=_dataset_store(request)), role, user)
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
        form.get("yaml_text", ""),
        form.get("filter_dataset", ""),
        column=(form.get("filter_column") or "").strip(),
        op=form.get("filter_op") or "in",
        values=row.get("values", []),
        resolve=_dataset_store(request).source,
        live=bool(form.get("live")),
    )


class AssistRequest(BaseModel):
    yaml: str = ""


@app.post("/yaml/assist")
async def yaml_assist_route(request: Request, req: AssistRequest) -> dict:
    """The YAML editor's question, asked after each pause in typing: what's
    wrong with this dashboard, and where (`problems`); and what can complete
    here (`schema`). One request for both, since both need the current text."""
    store = _dataset_store(request)
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
        form.get("yaml_text", ""), dataset, column=column, op=op, values=values,
        resolve=_dataset_store(request).source, live=True,
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
        form.get("yaml_text", ""),
        form.get("filter_dataset", ""),
        (form.get("filter_column") or "").strip(),
        query=(form.get("filter_q") or "").strip(),
        selected=form.getlist("filter_value"),
        resolve=_dataset_store(request).source,
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
            resolve=_dataset_store(request).source,
            datasets=_dataset_descriptions(request), dataset_override=dataset.strip(),
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
            dataset=dataset.strip(), title=title, resolve=_dataset_store(request).source,
            datasets=_dataset_descriptions(request),
        )
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return f'<div class="ff-modal-error" role="alert">{escape(str(exc))}</div>'


@app.post("/chart/config/create")
async def chart_config_create(request: Request) -> dict:
    """Create a chart from the add-form and place it in the layout. Returns
    {ok, yaml} on success, else {ok:false, error}."""
    form = await request.form()
    yaml_text = form.get("yaml_text", "")
    try:
        return {"ok": True, "yaml": config_edit.add_chart(yaml_text, form)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/insert-item")
async def chart_config_insert_item(
    yaml_text: str = Form(""),
    kind: str = Form(""),
    before: str = Form("end"),
) -> dict:
    """Insert a header or separator into the layout (no modal needed)."""
    try:
        return {"ok": True, "yaml": config_edit.insert_layout_item(yaml_text, kind, before)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/move-item")
async def chart_config_move_item(
    yaml_text: str = Form(""),
    index: int = Form(0),
    before: str = Form("end"),
) -> dict:
    """Move a header/separator (at layout-item `index`) to a new spot between rows."""
    try:
        return {"ok": True, "yaml": config_edit.move_layout_item(yaml_text, index, before)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/delete-item")
async def chart_config_delete_item(yaml_text: str = Form(""), index: int = Form(0)) -> dict:
    """Delete the header/separator at layout-item `index`."""
    try:
        return {"ok": True, "yaml": config_edit.delete_layout_item(yaml_text, index)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/move")
async def chart_config_move(
    yaml_text: str = Form(""),
    src: str = Form(""),
    dst: str = Form(""),
    position: str = Form("before"),
) -> dict:
    """Move chart `src` next to `dst` (drag-and-drop). Returns {ok, yaml}."""
    try:
        return {"ok": True, "yaml": config_edit.move_placement(yaml_text, src, dst, position)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/merge-down")
async def chart_config_merge_down(yaml_text: str = Form(""), cid: str = Form("")) -> dict:
    """Extend `cid`'s own span down by one row (merge current row + 1 below).
    Returns {ok, yaml}."""
    try:
        return {"ok": True, "yaml": config_edit.merge_down(yaml_text, cid)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/new-row")
async def chart_config_new_row(
    yaml_text: str = Form(""), src: str = Form(""), before: str = Form("end")
) -> dict:
    """Move chart `src` into its own new row at `before` (drag onto a row gap)."""
    try:
        return {"ok": True, "yaml": config_edit.move_to_new_row(yaml_text, src, before)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/resize-columns")
async def chart_config_resize_columns(
    yaml_text: str = Form(""), ordinals: str = Form(""), widths: str = Form("")
) -> dict:
    """Rewrite a merge group's column widths after a column-boundary drag.
    `ordinals` and `widths` are comma-separated. Returns {ok, yaml}."""
    try:
        ords = [int(o) for o in ordinals.split(",") if o != ""]
        ws = [float(w) for w in widths.split(",") if w != ""]
        return {"ok": True, "yaml": config_edit.resize_columns(yaml_text, ords, ws)}
    except (config_edit.ConfigEditError, DashboardError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/header")
async def chart_config_header(
    yaml_text: str = Form(""), index: int = Form(0), text: str = Form("")
) -> dict:
    """Rename the `index`-th layout header. Returns {ok, yaml} or error."""
    try:
        return {"ok": True, "yaml": config_edit.set_header_text(yaml_text, index, text)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/delete")
async def chart_config_delete(yaml_text: str = Form(""), cid: str = Form("")) -> dict:
    """Delete a chart (block + layout placements). Returns {ok, yaml} or error."""
    try:
        return {"ok": True, "yaml": config_edit.delete_chart(yaml_text, cid)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


# --- tabs --------------------------------------------------------------------
# The tab bar's editor gestures. Each is a thin wrapper over a config_edit tab
# function returning {ok, yaml}; the browser swaps the YAML in and re-runs.


@app.post("/chart/config/tab-add-first")
async def chart_config_tab_add_first(yaml_text: str = Form("")) -> dict:
    """Wrap a flat dashboard in its first tab. Returns {ok, yaml}."""
    try:
        return {"ok": True, "yaml": config_edit.add_first_tab(yaml_text)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/tab-insert")
async def chart_config_tab_insert(
    yaml_text: str = Form(""), before: str = Form("end")
) -> dict:
    """Add a tab that splits the current one at layout-item `before`."""
    try:
        return {"ok": True, "yaml": config_edit.insert_tab(yaml_text, before)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/tab-rename")
async def chart_config_tab_rename(
    yaml_text: str = Form(""), index: int = Form(0), name: str = Form("")
) -> dict:
    """Rename the `index`-th tab. Returns {ok, yaml} or error."""
    try:
        return {"ok": True, "yaml": config_edit.set_tab_text(yaml_text, index, name)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/tab-move")
async def chart_config_tab_move(
    yaml_text: str = Form(""), index: int = Form(0), before: str = Form("end")
) -> dict:
    """Move the `index`-th tab to layout-item gap `before`. Returns {ok, yaml}."""
    try:
        return {"ok": True, "yaml": config_edit.move_tab(yaml_text, index, before)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/tab-reorder")
async def chart_config_tab_reorder(
    yaml_text: str = Form(""), index: int = Form(0), to: int = Form(0)
) -> dict:
    """Move the `index`-th tab, rows and all, to before tab `to` (the tab count:
    after the last). Returns {ok, yaml}."""
    try:
        return {"ok": True, "yaml": config_edit.reorder_tab(yaml_text, index, to)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/tab-delete")
async def chart_config_tab_delete(
    yaml_text: str = Form(""), index: int = Form(0)
) -> dict:
    """Delete the `index`-th tab (first tab dissolves all). Returns {ok, yaml}."""
    try:
        return {"ok": True, "yaml": config_edit.delete_tab(yaml_text, index)}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/chart/config/save")
async def chart_config_save(request: Request) -> dict:
    """Apply the submitted modal form to the chart's YAML block.

    Returns {ok, yaml} on success (the browser swaps it into the editor and
    re-renders) or {ok:false, error} if the resulting YAML wouldn't parse."""
    form = await request.form()
    yaml_text = form.get("yaml_text", "")
    cid = form.get("cid", "")
    try:
        new_yaml = config_edit.apply_edit(yaml_text, cid, form)
        return {"ok": True, "yaml": new_yaml}
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return {"ok": False, "error": str(exc)}


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
    source = _dataset_store(request).source
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
    try:
        definition = calcs_edit.definition_from_form(form)
        new_yaml = calcs_edit.upsert_calc(
            yaml_text, dataset, key, definition, original_key=original_key
        )
        return {"ok": True, "yaml": new_yaml}
    except calcs_edit.CalcsEditError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/calcs/delete")
async def calcs_delete(request: Request) -> dict:
    """Delete a calc (guarded if a chart still references it)."""
    form = await request.form()
    try:
        new_yaml = calcs_edit.delete_calc(
            form.get("yaml_text", ""), form.get("dataset", ""), form.get("key", "")
        )
        return {"ok": True, "yaml": new_yaml}
    except calcs_edit.CalcsEditError as exc:
        return {"ok": False, "error": str(exc)}
