import json
import os
import re
import traceback
from html import escape

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from fireflyer import config_edit
from fireflyer import filters as filters_mod
from fireflyer.chart.map.chart import Map
from fireflyer.chart.table.chart import Table
from fastapi.responses import RedirectResponse
from fireflyer.dashboard import Dashboard, DashboardError
from fireflyer.web import auth as auth_mod
from fireflyer.web import chat as chat_mod
from fireflyer.web import portal as portal_mod

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
app.state.store = (
    portal_mod.make_store(os.environ.get("DATABASE_URL")) if PORTAL_ENABLED else None
)
# Portal mode is gated behind a login. `authenticator` is the swappable
# credential check (default: admin/admin); None disables auth entirely (local
# mode). Tests set both `store` and `authenticator` directly.
app.state.authenticator = auth_mod.default_authenticator() if PORTAL_ENABLED else None


@app.middleware("http")
async def _require_login(request: Request, call_next):
    """When auth is on, every route except the login page requires a session;
    unauthenticated requests are redirected to /login."""
    auth = app.state.authenticator
    if (
        auth is not None
        and request.url.path != "/login"
        and auth_mod.current_user(request) is None
    ):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)

# Pinned htmx version. Loaded once on the editor page so charts embedded via
# innerHTML can use hx-* attributes without each chart shipping its own script.
HTMX_SRC = "https://unpkg.com/htmx.org@1.9.12"

# Starter — exercises every layout element: header, two-chart row, separator,
# single-chart row. Small enough to read at a glance.
DEFAULT_YAML = """name: Orders overview

datasets:
  orders:
    path: files/orders.csv

charts:
  total_orders:
    type: number
    dataset: orders
    title: Total orders
    column: id
    agg: count

  revenue:
    type: number
    dataset: orders
    title: Revenue (paid)
    column: amount
    agg: sum
    filters:
      - column: status
        op: in
        values: [paid]

  biggest_order:
    type: number
    dataset: orders
    title: Biggest order
    column: amount
    agg: max

  orders:
    type: table
    dataset: orders
    title: Orders

  status:
    type: pie
    dataset: orders
    title: Orders by Status
    column: status

  by_day:
    type: bar
    dataset: orders
    title: Orders by Day, stacked by status
    x: day
    y: status

  density:
    type: map
    dataset: orders
    title: Order density (Kyiv)
    lat: lat
    lng: lng
    grid_size: 16

  orders_long:
    type: table
    dataset: orders
    title: All Orders
    pagination: 1000

dashboard:
  Overview:
    - ["@22", "total_orders", "revenue", "biggest_order"]
    - ["@40", "orders:3", "status:2"]
    - ["@30", "by_day", "status"]
    - "-"
    - ["@100", "density"]
  All orders:
    - ["@50", "orders_long"]
"""

# Chat body differs by whether a key is configured: the live input, or a setup
# notice. Built as a plain string so its contents drop into INDEX verbatim.
if CHAT_ENABLED:
    CHAT_PANEL = """
      <div class="chat-log" id="chat-log"></div>
      <form class="chat-input" id="chat-form">
        <textarea id="chat-text" spellcheck="false" placeholder="Ask to add or change charts, resize rows, add filters…"></textarea>
        <button type="submit" class="chat-send" id="chat-send">Send</button>
      </form>"""
else:
    CHAT_PANEL = """
      <div class="chat-notice">Set <code>ANTHROPIC_API_KEY</code> in <code>.env</code> and restart to enable the AI assistant.</div>"""

INDEX = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fireflyer</title>
<script src="{HTMX_SRC}"></script>
<style>
  :root {{
    color-scheme: light;
    --bg: #f5f6f8;
    --panel: #ffffff;
    --border: #e0e0e0;
    --text: #20242b;
    --muted: #5e6975;
    --accent: #20a7c9;
    --accent-hover: #1a8aa6;
    --error: #e04355;
  }}
  /* Editor chrome dark palette. The topbar toggle sets `data-ff-theme` on
     <html>; that same attribute also themes the dashboard preview and every
     chart inside it (their CSS keys off any ancestor). "auto" (no attribute)
     follows the OS, but an explicit "light" opts back out of a dark OS. */
  @media (prefers-color-scheme: dark) {{
    :root:not([data-ff-theme="light"]) {{
      color-scheme: dark;
      --bg: #0f1620;
      --panel: #1b2635;
      --border: #2c384a;
      --text: #e6e8ec;
      --muted: #a3adbd;
      --accent: #20a7c9;
      --accent-hover: #48c4e0;
      --error: #e04355;
    }}
  }}
  :root[data-ff-theme="dark"] {{
    color-scheme: dark;
    --bg: #0f1620;
    --panel: #1b2635;
    --border: #2c384a;
    --text: #e6e8ec;
    --muted: #a3adbd;
    --accent: #20a7c9;
    --accent-hover: #48c4e0;
    --error: #e04355;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; height: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Helvetica, Arial, sans-serif;
    color: var(--text); background: var(--bg); }}
  .topbar {{
    position: relative; height: 44px; display: flex; align-items: center;
    justify-content: space-between;
    padding: 0 16px; background: var(--panel); border-bottom: 1px solid var(--border);
  }}
  .topbar-left, .topbar-right {{ display: flex; align-items: center; gap: 14px; }}
  .topbar .brand {{ font-weight: 600; font-size: 14px; letter-spacing: -0.01em;
    color: var(--text); text-decoration: none; }}
  a.brand:hover {{ color: var(--accent); }}
  .topbar .ff-nav {{ display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 30px; border-radius: 5px; color: var(--text);
    text-decoration: none; font-size: 16px; }}
  .topbar .ff-nav:hover {{ background: var(--bg); }}
  /* Dashboard title in the left group, after the logo (separated by a dot).
     Click-to-rename: editable in place; capped width with ellipsis so a long
     name doesn't push the right group (grows to full text while focused). */
  .topbar .ff-sep {{ color: var(--muted); }}
  .topbar .ff-dash-name {{ font-size: 14px; font-weight: 600; color: var(--text);
    white-space: nowrap; max-width: 340px; overflow: hidden; text-overflow: ellipsis;
    padding: 3px 8px; border-radius: 4px; cursor: text; outline: none; }}
  .topbar .ff-dash-name:hover {{ background: var(--bg); }}
  .topbar .ff-dash-name:focus {{ background: var(--bg); overflow: visible; max-width: none;
    box-shadow: 0 0 0 2px var(--accent); }}
  /* 3-segment icon theme switch: Auto (A) / Light (sun) / Dark (moon). */
  .topbar .ff-theme {{ display: inline-flex; border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden; }}
  .topbar .ff-theme button {{ background: var(--panel); color: var(--muted); border: 0;
    border-left: 1px solid var(--border); padding: 5px 8px; cursor: pointer;
    display: inline-flex; align-items: center; }}
  .topbar .ff-theme button:first-child {{ border-left: 0; }}
  .topbar .ff-theme button svg {{ width: 16px; height: 16px; display: block; }}
  .topbar .ff-theme button:hover {{ background: var(--bg); color: var(--text); }}
  .topbar .ff-theme button.active {{ background: var(--accent); color: #fff; }}
  .topbar .toggle {{
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    padding: 5px 12px; border-radius: 4px; font-size: 12px; cursor: pointer;
  }}
  .topbar .toggle:hover {{ background: var(--bg); }}
  .topbar .run {{
    background: var(--accent); color: #fff; border: 0; padding: 6px 14px;
    border-radius: 4px; font-size: 13px; font-weight: 500; cursor: pointer;
  }}
  .topbar .run:hover {{ background: var(--accent-hover); }}
  .topbar .run:disabled {{ opacity: 0.6; cursor: not-allowed; }}
{auth_mod.PROFILE_CSS}
  /* Output pane: refresh overlay shown when the YAML is edited but not re-run. */
  .pane.output {{ position: relative; }}
  /* Greyed as a "stale" cue, but still interactive — the resize/move/edit
     handlers read the live textarea and re-render on release, so acting on a
     stale preview stays consistent (and blocking it broke vertical resize). */
  .pane.output.stale .pane-body {{ opacity: 0.55; filter: grayscale(0.35);
    transition: opacity 0.12s; }}
  /* Centered in the output pane (both axes) and sized responsively via clamp,
     so it stays a big, obvious target at any pane width. */
  .ff-refresh {{ display: none; position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%); z-index: 6; align-items: center; gap: 10px;
    background: var(--accent); color: #fff; border: 0;
    padding: clamp(10px, 1.6vw, 18px) clamp(20px, 2.6vw, 34px);
    border-radius: 10px; font-size: clamp(15px, 1.4vw, 20px); font-weight: 600;
    cursor: pointer; white-space: nowrap; max-width: calc(100% - 32px);
    box-shadow: 0 8px 26px rgba(0,0,0,0.32); transition: background 0.12s, transform 0.08s; }}
  .ff-refresh:hover {{ background: var(--accent-hover); }}
  .ff-refresh:active {{ transform: translate(-50%, -50%) scale(0.97); }}
  .pane.output.stale .ff-refresh {{ display: inline-flex; }}
  /* Transient error toast (bottom-centre). */
  .ff-toast {{ position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%);
    background: var(--error); color: #fff; padding: 9px 16px; border-radius: 6px;
    font-size: 13px; z-index: 50; box-shadow: 0 6px 20px rgba(0,0,0,0.25); }}
  .layout {{
    display: grid; grid-template-columns: 1fr 5px 1fr;
    background: var(--border);
    height: calc(100vh - 44px);
  }}
  /* Draggable divider between the editor and output panes. */
  .pane-resizer {{ background: var(--border); cursor: col-resize; }}
  .pane-resizer:hover, .pane-resizer.dragging {{ background: var(--accent); }}
  /* Editor hidden: single column, left pane + divider removed from the grid. */
  .layout.editor-hidden {{ grid-template-columns: 1fr; }}
  .layout.editor-hidden .pane.editor,
  .layout.editor-hidden .pane-resizer {{ display: none; }}
  /* View-only mode: resizing edits the (now hidden) YAML, so suppress the
     handles entirely. The JS also bails, but hiding them removes the affordance. */
  .layout.editor-hidden .fireflyer-resize-handle,
  .layout.editor-hidden .fireflyer-resize-col-handle,
  .layout.editor-hidden .fireflyer-chart-tools,
  .layout.editor-hidden .fireflyer-add-row,
  .layout.editor-hidden .fireflyer-add-cell {{ display: none; }}
  .pane {{ display: flex; flex-direction: column; background: var(--panel); min-height: 0; }}
  .pane-body {{ flex: 1; overflow: auto; min-height: 0; }}
  #code {{
    width: 100%; height: 100%; border: 0; outline: 0; resize: none;
    padding: 14px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px; line-height: 1.5; color: var(--text); background: var(--panel);
    tab-size: 2;
  }}
  #output {{ padding: 16px; }}
  /* The output pane scrolls vertically only. Filter-indicator tooltips escape
     their cells (cells are overflow:visible) and, while hidden, still reserve
     layout — without this they'd extend the panel's scroll width and surface a
     spurious horizontal scrollbar. Clipping overflow-x is harmless: content-
     sized tooltips stay within their cells. overflow-y stays auto from the base
     rule, so tall dashboards still scroll. */
  .pane-body:has(#output) {{ overflow-x: hidden; }}
  pre.error {{ color: var(--error); white-space: pre-wrap; font-size: 12px; }}
  /* AI assistant — stacked under the YAML editor in the left pane. */
  .chat {{
    display: flex; flex-direction: column; height: 300px; flex: none;
    border-top: 1px solid var(--border); background: var(--panel);
  }}
  .chat.collapsed {{ height: 33px; }}
  .chat.collapsed .chat-log,
  .chat.collapsed .chat-input,
  .chat.collapsed .chat-notice {{ display: none; }}
  .chat-header {{
    height: 33px; flex: none; display: flex; align-items: center;
    padding: 0 14px; font-size: 11px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em;
    border-bottom: 1px solid var(--border);
  }}
  .chat-collapse {{
    margin-left: auto; background: transparent; border: 0; color: var(--muted);
    cursor: pointer; font-size: 16px; line-height: 1; padding: 0 4px;
  }}
  .chat-log {{
    flex: 1; overflow: auto; min-height: 0; padding: 12px 14px;
    display: flex; flex-direction: column; gap: 10px;
  }}
  .chat-msg {{
    font-size: 13px; line-height: 1.45; white-space: pre-wrap;
    padding: 8px 10px; border-radius: 6px; max-width: 90%;
  }}
  .chat-msg.user {{ background: var(--bg); align-self: flex-end; }}
  .chat-msg.assistant {{ background: rgba(32,167,201,0.12); align-self: flex-start; }}
  .chat-msg.error {{ background: rgba(224,67,85,0.12); color: var(--error); align-self: flex-start; }}
  .chat-input {{
    flex: none; display: flex; gap: 8px; padding: 10px 14px;
    border-top: 1px solid var(--border);
  }}
  #chat-text {{
    flex: 1; resize: none; height: 46px; border: 1px solid var(--border);
    border-radius: 4px; padding: 8px; font-family: inherit; font-size: 13px;
    outline: 0; background: var(--bg); color: var(--text);
  }}
  #chat-text:focus {{ border-color: var(--accent); }}
  .chat-send {{
    background: var(--accent); color: #fff; border: 0; padding: 0 14px;
    border-radius: 4px; font-size: 13px; font-weight: 500; cursor: pointer;
  }}
  .chat-send:disabled {{ opacity: 0.6; cursor: not-allowed; }}
  .chat-notice {{ padding: 12px 14px; font-size: 12px; color: var(--muted); }}
  .chat-notice code {{ background: var(--bg); padding: 1px 4px; border-radius: 3px; }}
  /* Edit-chart modal (editor-only). */
  .ff-modal-overlay {{
    position: fixed; inset: 0; z-index: 50; background: rgba(20,36,43,0.45);
    display: none; align-items: flex-start; justify-content: center;
    padding: 48px 16px; overflow: auto;
  }}
  .ff-modal-overlay.open {{ display: flex; }}
  .ff-modal {{
    background: var(--panel); border-radius: 8px; width: 440px; max-width: 100%;
    box-shadow: 0 12px 40px rgba(0,0,0,0.25); overflow: hidden;
  }}
  .ff-modal-head {{
    display: flex; align-items: center; gap: 8px;
    padding: 14px 16px; border-bottom: 1px solid var(--border);
  }}
  .ff-modal-title {{ font-size: 14px; }}
  .ff-modal-body {{
    padding: 14px 16px; display: flex; flex-direction: column; gap: 12px;
    max-height: 60vh; overflow: auto;
  }}
  .ff-field {{ display: flex; flex-direction: column; gap: 4px; }}
  .ff-field-label {{ font-size: 12px; font-weight: 600; color: var(--muted); }}
  .ff-input {{
    border: 1px solid var(--border); border-radius: 4px; padding: 7px 9px;
    font-size: 13px; font-family: inherit; outline: 0;
    background: var(--bg); color: var(--text);
  }}
  .ff-input:focus {{ border-color: var(--accent); }}
  .ff-check {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
  .ff-filters {{ display: flex; flex-direction: column; gap: 6px; }}
  .ff-filter-row {{
    display: grid; grid-template-columns: 1fr 84px 1.4fr auto; gap: 6px;
    align-items: center;
  }}
  .ff-filter-del {{
    border: 0; background: transparent; color: var(--muted); font-size: 18px;
    cursor: pointer; line-height: 1; padding: 0 4px;
  }}
  .ff-filter-del:hover {{ color: var(--error); }}
  .ff-filter-add {{
    align-self: flex-start; background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; padding: 4px 10px; font-size: 12px; cursor: pointer;
    color: var(--text);
  }}
  .ff-modal-error {{
    margin: 0 16px; padding: 8px 10px; background: rgba(224,67,85,0.12); color: var(--error);
    border-radius: 4px; font-size: 12px;
  }}
  .ff-modal-foot {{
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 12px 16px; border-top: 1px solid var(--border);
  }}
  .ff-btn {{
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    border-radius: 4px; padding: 7px 14px; font-size: 13px; cursor: pointer;
  }}
  .ff-btn.ff-primary {{ background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 500; }}
  .ff-btn.ff-danger {{ background: var(--error); border-color: var(--error); color: #fff; font-weight: 500; }}
  .ff-btn:hover {{ filter: brightness(0.97); }}
  .ff-confirm-text {{ font-size: 13px; line-height: 1.5; margin: 0; }}
  /* Popup for the gutter "+" (insert row): chart / header / separator. */
  .ff-addmenu {{
    position: fixed; z-index: 60; min-width: 132px; padding: 4px;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; box-shadow: 0 8px 24px rgba(0,0,0,0.18);
    display: flex; flex-direction: column;
  }}
  .ff-addmenu[hidden] {{ display: none; }}
  .ff-addmenu button {{
    text-align: left; background: transparent; border: 0; border-radius: 4px;
    padding: 7px 10px; font-size: 13px; color: var(--text); cursor: pointer;
  }}
  .ff-addmenu button:hover {{ background: var(--bg); color: var(--accent); }}
  /* Red cancel button overlaying the topbar above the output pane; its left edge
     tracks the output pane (set in JS) and it only shows in move mode. */
  .ff-move-discard {{
    position: absolute; top: 0; bottom: 0; left: 50%; right: 0; z-index: 30;
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    background: var(--error); color: #fff; border: 0; border-radius: 0;
    font-size: 14px; font-weight: 500; line-height: 1; cursor: pointer;
  }}
  .ff-move-discard[hidden] {{ display: none; }}
  .ff-move-discard:hover {{ filter: brightness(0.93); }}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-left">
    __FF_NAV__
    __FF_BRAND__
    __FF_DASH_NAME__
  </div>
  <div class="topbar-right">
    __FF_SAVE__
    <button class="toggle" id="toggle">Preview</button>
    __FF_THEME__
    __FF_USER_MENU__
  </div>
  <button type="button" class="ff-move-discard" id="ff-move-cancel" hidden title="Cancel move (Esc)" aria-label="Cancel move (Esc)">✕ (Esc)</button>
</header>
<div class="layout" id="layout">
  <section class="pane editor">
    <div class="pane-body">
      <textarea id="code" spellcheck="false" autocomplete="off">__FF_YAML_CONTENT__</textarea>
    </div>
    <div class="chat collapsed" id="chat">
      <div class="chat-header">AI editor
        <button class="chat-collapse" id="chat-collapse" title="Expand" aria-label="Expand chat">+</button>
      </div>{CHAT_PANEL}
    </div>
  </section>
  <div class="pane-resizer" id="pane-resizer" title="Drag to resize"></div>
  <section class="pane output" id="output-pane">
    <div class="pane-body"><div id="output"></div></div>
    <!-- Shown (over a greyed-out, stale preview) only after a manual YAML edit. -->
    <button type="button" class="ff-refresh" id="refresh" title="Refresh preview (⌘/Ctrl+Enter)">↻ Refresh</button>
  </section>
</div>
<div class="ff-modal-overlay" id="ff-modal-overlay">
  <div class="ff-modal" id="ff-modal"></div>
</div>
<div class="ff-addmenu" id="ff-addmenu" hidden>
  <button type="button" data-add-kind="chart">Chart…</button>
  <button type="button" data-add-kind="header">Header</button>
  <button type="button" data-add-kind="separator">Separator</button>
  <button type="button" data-add-kind="tab">Tab</button>
</div>
<!-- Transient error toast (replaces the old topbar status text). -->
<div class="ff-toast" id="ff-toast" hidden></div>
<script>
const codeEl = document.getElementById('code');
const outEl = document.getElementById('output');
const outPane = document.getElementById('output-pane');
const refreshBtn = document.getElementById('refresh');
const toastEl = document.getElementById('ff-toast');

// Transient error toast — replaces the old topbar status line for the rare
// config-edit failure messages. Auto-hides after a few seconds.
let toastTimer = null;
function flash(msg) {{
  toastEl.textContent = msg;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function() {{ toastEl.hidden = true; }}, 3000);
}}

// Theme switch — a 3-segment Auto / Light / Dark control. Sets `data-ff-theme`
// on <html>, which themes the editor chrome, the dashboard preview, and every
// chart inside it (their CSS keys off this attribute on any ancestor). "auto"
// leaves it off so the OS preference wins. The choice persists across reloads.
const themeSwitch = document.getElementById('theme-switch');
const THEME_MODES = ['auto', 'light', 'dark'];
let themeMode = localStorage.getItem('ffTheme') || 'auto';
if (THEME_MODES.indexOf(themeMode) < 0) themeMode = 'auto';
function applyTheme() {{
  if (themeMode === 'auto') delete document.documentElement.dataset.ffTheme;
  else document.documentElement.dataset.ffTheme = themeMode;
  themeSwitch.querySelectorAll('button').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.mode === themeMode);
  }});
  localStorage.setItem('ffTheme', themeMode);
}}
themeSwitch.querySelectorAll('button').forEach(function(b) {{
  b.addEventListener('click', function() {{ themeMode = b.dataset.mode; applyTheme(); }});
}});
applyTheme();

// Which dashboard tab is showing. Threaded to /execute so an edit re-renders
// the same tab, and re-synced from the rendered hidden input after every swap
// (the server clamps it, and a dissolve drops back to a flat, tab-less render).
let activeTab = 0;
function syncActiveTab() {{
  const inp = outEl.querySelector('#fireflyer-dashboard input[name="active_tab"]');
  activeTab = inp ? (parseInt(inp.value, 10) || 0) : 0;
}}

async function run() {{
  outPane.classList.remove('stale');  // re-rendering now: clear the stale overlay
  refreshBtn.disabled = true;
  try {{
    const res = await fetch('/execute?active_tab=' + activeTab, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/yaml'}},
      body: codeEl.value,
    }});
    const data = await res.json();
    outEl.innerHTML = data.html;
    // htmx doesn't auto-wire nodes inserted via innerHTML; wire them now.
    if (window.htmx) window.htmx.process(outEl);
    syncActiveTab();
  }} catch (e) {{
    outEl.innerHTML = '<pre class="error">' + e + '</pre>';
  }} finally {{
    refreshBtn.disabled = false;
    updateSaveState();  // config-edit ops change the YAML then run() — refresh Save
  }}
}}

// --- unsaved-changes (Save) + editable title ------------------------------
// Two independent "dirty" notions: the preview is *stale* vs the last render
// (drives Refresh), and the YAML is *unsaved* vs what's stored (drives Save).
const saveBtn = document.getElementById('ff-save');       // portal only
const nameEl = document.getElementById('ff-dash-name');   // editable title
let savedYaml = codeEl.value;

function markStale() {{ outPane.classList.add('stale'); }}
function updateSaveState() {{ if (saveBtn) saveBtn.hidden = (codeEl.value === savedYaml); }}

// Title <-> YAML `name:` key (two-way). Read/rewrite the top-level line.
function yamlName(text) {{
  const m = text.match(/^name:[ \\t]*(.*)$/m);
  if (!m) return '';
  let v = m[1].trim();
  if (v.startsWith('"')) {{ try {{ return JSON.parse(v); }} catch (e) {{ return v.slice(1, -1); }} }}
  if (v.startsWith("'") && v.endsWith("'")) return v.slice(1, -1).replace(/''/g, "'");
  return v;
}}
function setYamlName(text, name) {{ return text.replace(/^name:.*$/m, 'name: ' + JSON.stringify(name)); }}
function syncNameFromYaml() {{
  if (nameEl && document.activeElement !== nameEl) nameEl.textContent = yamlName(codeEl.value);
}}

// A manual YAML edit greys out the (now stale) preview and reveals the refresh
// button; it may also change the name or dirty state. Programmatic edits (chat,
// config-edit) call run() directly, which refreshes Save on its own.
codeEl.addEventListener('input', function() {{ markStale(); updateSaveState(); syncNameFromYaml(); }});

async function doSave() {{
  if (!saveBtn || saveBtn.hidden) return;   // nothing to save
  const label = saveBtn.textContent;
  saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
  try {{
    const res = await fetch(saveBtn.dataset.saveUrl, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: new URLSearchParams({{yaml_text: codeEl.value}}),
    }});
    const data = await res.json();
    if (data.ok) {{ savedYaml = codeEl.value; saveBtn.textContent = 'Saved \\u2713'; }}
    else {{ saveBtn.textContent = 'Save failed'; console.warn('Save failed:', data.error || ''); }}
  }} catch (e) {{ saveBtn.textContent = 'Save failed'; }}
  finally {{
    saveBtn.disabled = false;
    setTimeout(function() {{ saveBtn.textContent = label; updateSaveState(); }}, 1200);
  }}
}}

if (saveBtn) {{
  saveBtn.addEventListener('click', doSave);
  window.addEventListener('keydown', function(e) {{
    if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) {{ e.preventDefault(); doSave(); }}
  }});
  // Guard against losing unsaved edits by navigating away (logo / ☰ / reload).
  window.addEventListener('beforeunload', function(e) {{
    if (!saveBtn.hidden) {{ e.preventDefault(); e.returnValue = ''; }}
  }});
}}

if (nameEl) {{
  nameEl.setAttribute('contenteditable', 'plaintext-only');
  nameEl.setAttribute('spellcheck', 'false');
  nameEl.setAttribute('title', 'Click to rename');
  nameEl.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{ e.preventDefault(); nameEl.blur(); }}
    else if (e.key === 'Escape') {{ e.preventDefault(); nameEl.textContent = yamlName(codeEl.value); nameEl.blur(); }}
  }});
  nameEl.addEventListener('blur', function() {{
    const nm = nameEl.textContent.trim();
    if (!nm || nm === yamlName(codeEl.value)) {{ nameEl.textContent = yamlName(codeEl.value); return; }}
    codeEl.value = setYamlName(codeEl.value, nm);   // rewrite the YAML `name:` key
    nameEl.textContent = nm;
    updateSaveState();   // renaming is an unsaved change (but not a preview change)
  }});
}}

// A crossfilter click or deployed tab button swaps #fireflyer-dashboard via
// htmx (not through run()); keep the JS tab state in sync afterwards. During a
// cross-tab chart move the destination tab's cells load one by one (each an
// htmx swap) and only carry data-cid once loaded, so rebuild the drop zones as
// they arrive.
outEl.addEventListener('htmx:afterSwap', () => {{
  syncActiveTab();
  const dash = dashboardEl();
  if (moveCid !== null && dash && dash.classList.contains('ff-move-mode')) {{
    const overlay = dash.querySelector('.ff-move-overlay');
    if (overlay) overlay.remove();
    buildMoveZones();
  }}
}});

// Render the default example immediately so the page isn't empty.
window.addEventListener('DOMContentLoaded', run);
refreshBtn.addEventListener('click', run);
codeEl.addEventListener('keydown', e => {{
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {{ e.preventDefault(); run(); }}
}});

const layoutEl = document.getElementById('layout');
const toggleBtn = document.getElementById('toggle');
// The editor:output split, kept so Preview can drop to one column and Edit can
// restore the ratio the user dragged to. Inline style wins over the CSS class,
// so we set it explicitly on each toggle.
const PANE_GUTTER = 5;
let paneSplit = '1fr ' + PANE_GUTTER + 'px 1fr';
toggleBtn.addEventListener('click', () => {{
  const hidden = layoutEl.classList.toggle('editor-hidden');
  toggleBtn.textContent = hidden ? 'Edit' : 'Preview';
  layoutEl.style.gridTemplateColumns = hidden ? '1fr' : paneSplit;
}});

// Drag the divider to change the editor/output proportion.
const paneResizer = document.getElementById('pane-resizer');
let paneDrag = null;
paneResizer.addEventListener('mousedown', e => {{
  e.preventDefault();
  paneDrag = layoutEl.getBoundingClientRect();
  paneResizer.classList.add('dragging');
  document.body.style.userSelect = 'none';
}});
window.addEventListener('mousemove', e => {{
  if (!paneDrag) return;
  const total = paneDrag.width - PANE_GUTTER;
  const left = Math.max(160, Math.min(total - 160, e.clientX - paneDrag.left));
  paneSplit = left + 'fr ' + PANE_GUTTER + 'px ' + (total - left) + 'fr';
  layoutEl.style.gridTemplateColumns = paneSplit;
}});
window.addEventListener('mouseup', () => {{
  if (!paneDrag) return;
  paneDrag = null;
  paneResizer.classList.remove('dragging');
  document.body.style.userSelect = '';
}});

// --- Resize -----------------------------------------------------------------
// Handles render in the output only while editing. Dragging one updates the
// grid live, then rewrites the YAML and re-runs. Suppressed in "Hide YAML"
// (view-only) mode — there's nothing to edit into.
//   Rows: drag the bottom edge to change height. 1 unit = 8px, matching
//   HEIGHT_UNIT_PX in dashboard.py.
//   Columns: drag an interior boundary of the (union) column grid to rebalance
//   the two adjacent fine columns; snaps to 10% steps. On release the server
//   (`config_edit.resize_columns`) recomputes each cell's width from the fine
//   columns it spans — so every row those columns belong to is updated (even a
//   drag started on an inherited/lower row) and spanning cells stay bare.
const HEIGHT_UNIT_PX = 8;
const COL_STEP = 10;   // column widths snap to 10% increments while dragging
let resize = null;

function gcd(a, b) {{ a = Math.abs(a); b = Math.abs(b); while (b) {{ [a, b] = [b, a % b]; }} return a || 1; }}
// Reduce a width vector to its smallest whole-number ratio: [80,20] -> [4,1].
function reduceRatio(nums) {{
  const g = nums.reduce((acc, n) => gcd(acc, n), 0) || 1;
  return nums.map(n => Math.round(n / g));
}}

const editingDisabled = () => layoutEl.classList.contains('editor-hidden');

// Rewrite the Nth @<height> token in the `dashboard:` section. `@<n>` is the
// row-height indicator and rows carry exactly one each in order, so the Nth
// token is row `ordinal`'s height. Rewriting the token directly works for any
// YAML style (flow `["@30", ...]` or block `- "@30"`) — an earlier bracket-only
// version silently no-op'd on block-style rows, so drags snapped back.
function setRowUnits(ordinal, units) {{
  const text = codeEl.value;
  const dashIdx = text.search(/^dashboard:/m);
  const re = /@(\\d+(?:\\.\\d+)?)/g;
  re.lastIndex = dashIdx === -1 ? 0 : dashIdx;
  let m, n = 0;
  while ((m = re.exec(text)) !== null) {{
    if (n === ordinal) {{
      codeEl.value = text.slice(0, m.index) + '@' + units
        + text.slice(m.index + m[0].length);
      return;
    }}
    n++;
  }}
}}

outEl.addEventListener('mousedown', e => {{
  if (editingDisabled()) return;
  const rowHandle = e.target.closest('.fireflyer-resize-handle');
  const colHandle = e.target.closest('.fireflyer-resize-col-handle');
  const handle = rowHandle || colHandle;
  if (!handle) return;
  e.preventDefault();
  const row = handle.closest('.fireflyer-dashboard-row');

  if (rowHandle) {{
    const track = parseInt(handle.dataset.track, 10);          // 1-based grid row
    const tracks = row.style.gridTemplateRows.split(/\\s+/);    // e.g. ["320px","240px"]
    resize = {{
      axis: 'row', handle, row, tracks,
      index: track - 1,
      ordinal: parseInt(handle.dataset.rowOrdinal, 10),
      startY: e.clientY,
      startPx: parseFloat(tracks[track - 1]) || 0,
      px: parseFloat(tracks[track - 1]) || 0,
    }};
  }} else {{
    const left = parseInt(handle.dataset.leftCol, 10);          // 0-based
    const cols = row.style.gridTemplateColumns.split(/\\s+/);    // e.g. ["3fr","2fr"]
    // Work in row-relative percentages so the drag is independent of the units
    // stored in the YAML (which are arbitrary proportions like 3:2 or 60:40).
    const nums = cols.map(c => parseFloat(c) || 0);
    const total = nums.reduce((a, b) => a + b, 0) || 1;
    const pcts = nums.map(n => n / total * 100);
    resize = {{
      axis: 'col', handle, row, left,
      ordinals: handle.dataset.ordinals.split(',').map(Number),
      startX: e.clientX,
      rowWidth: row.clientWidth,
      pcts,
      leftStart: pcts[left],
      pairPct: pcts[left] + pcts[left + 1],
    }};
  }}
  handle.classList.add('is-dragging');
  document.body.classList.add('fireflyer-resizing-' + resize.axis);
}});

window.addEventListener('mousemove', e => {{
  if (!resize) return;
  if (resize.axis === 'row') {{
    resize.px = Math.max(HEIGHT_UNIT_PX, resize.startPx + (e.clientY - resize.startY));
    resize.tracks[resize.index] = resize.px + 'px';
    resize.row.style.gridTemplateRows = resize.tracks.join(' ');
  }} else {{
    const deltaPct = (e.clientX - resize.startX) / resize.rowWidth * 100;
    // Snap the boundary to COL_STEP increments, keeping the dragged pair's
    // combined share fixed so the other columns don't move.
    let lp = Math.round((resize.leftStart + deltaPct) / COL_STEP) * COL_STEP;
    lp = Math.max(COL_STEP, Math.min(resize.pairPct - COL_STEP, lp));
    resize.pcts[resize.left] = lp;
    resize.pcts[resize.left + 1] = resize.pairPct - lp;
    resize.row.style.gridTemplateColumns = resize.pcts.map(p => p + 'fr').join(' ');
  }}
}});

window.addEventListener('mouseup', () => {{
  if (!resize) return;
  const r = resize;
  r.handle.classList.remove('is-dragging');
  document.body.classList.remove('fireflyer-resizing-' + r.axis);
  resize = null;
  if (r.axis === 'row') {{
    setRowUnits(r.ordinal, Math.max(1, Math.round(r.px / HEIGHT_UNIT_PX)));
    run();  // re-render so the output reflects (and re-validates) the new YAML
  }} else {{
    // The dragged columns are the group's fine (union) grid, which may not map
    // 1:1 to YAML tokens (spans, bare cells). The server recomputes each cell's
    // width from the fine columns it covers, so every row the columns belong to
    // is updated and spans stay bare. Reduce to the smallest whole-number ratio
    // first, so a 50/50 drag becomes "1:1".
    commitColumnResize(r.ordinals, reduceRatio(r.pcts.map(p => Math.round(p))));
  }}
}});
async function commitColumnResize(ordinals, widths) {{
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('ordinals', ordinals.join(','));
  fd.append('widths', widths.join(','));
  const res = await fetch('/chart/config/resize-columns', {{ method: 'POST', body: fd }});
  const data = await res.json();
  if (data.ok) codeEl.value = data.yaml;
  run();
}}

// --- AI assistant -----------------------------------------------------------
// Sends the message + current YAML + prior turns to /chat. The reply is shown
// in the log; if the assistant returns new YAML, it replaces the editor and
// re-renders through the same run() path as a manual edit.
const chatEl = document.getElementById('chat');
const chatCollapse = document.getElementById('chat-collapse');
chatCollapse.addEventListener('click', () => {{
  const collapsed = chatEl.classList.toggle('collapsed');
  chatCollapse.textContent = collapsed ? '+' : '–';
  chatCollapse.title = collapsed ? 'Expand' : 'Collapse';
}});

const chatForm = document.getElementById('chat-form');
const chatLog = document.getElementById('chat-log');
const chatText = document.getElementById('chat-text');
const chatSend = document.getElementById('chat-send');
const chatHistory = [];  // plain {{role, content}} text turns sent each request

function addChatMsg(role, text) {{
  const el = document.createElement('div');
  el.className = 'chat-msg ' + role;
  el.textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}}

async function sendChat(message) {{
  addChatMsg('user', message);
  chatSend.disabled = true;
  const pending = addChatMsg('assistant', '…');
  try {{
    const res = await fetch('/chat', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{message, yaml: codeEl.value, history: chatHistory}}),
    }});
    const data = await res.json();
    pending.remove();
    if (!data.ok) {{
      addChatMsg('error', data.reply || 'Something went wrong.');
      return;
    }}
    addChatMsg('assistant', data.reply);
    chatHistory.push({{role: 'user', content: message}});
    chatHistory.push({{role: 'assistant', content: data.reply}});
    if (data.yaml) {{
      codeEl.value = data.yaml;
      run();  // reflect the assistant's change in the output panel
    }}
  }} catch (e) {{
    pending.remove();
    addChatMsg('error', String(e));
  }} finally {{
    chatSend.disabled = false;
  }}
}}

if (chatForm) {{
  chatForm.addEventListener('submit', e => {{
    e.preventDefault();
    const msg = chatText.value.trim();
    if (!msg) return;
    chatText.value = '';
    sendChat(msg);
  }});
  // Enter sends; Shift+Enter inserts a newline.
  chatText.addEventListener('keydown', e => {{
    if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); chatForm.requestSubmit(); }}
  }});
}}

// --- Edit-chart modal -------------------------------------------------------
// The pencil on each chart opens a form built server-side from that chart's
// PARAMS. Save posts the fields back; the server rewrites just that chart's
// YAML block and returns the whole document, which we swap in and re-run.
const modalOverlay = document.getElementById('ff-modal-overlay');
const modalBox = document.getElementById('ff-modal');
let editingCid = null;   // chart being edited, or null when adding a new one
let addTarget = null;    // {{mode, index}} placement while adding

function closeModal() {{
  modalOverlay.classList.remove('open');
  modalBox.innerHTML = '';
  editingCid = null;
  addTarget = null;
}}

async function openChartEditor(cid) {{
  editingCid = cid;
  addTarget = null;
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('cid', cid);
  const res = await fetch('/chart/config/form', {{ method: 'POST', body: fd }});
  modalBox.innerHTML = await res.text();
  modalOverlay.classList.add('open');
}}

// Open the same modal in "add" mode. `mode` is 'row' (new row) or 'cell' (add to
// an existing row); `index` is the insert-before ordinal / 'end', or the row
// ordinal for 'cell'.
async function openAddChart(mode, index) {{
  editingCid = null;
  addTarget = {{ mode, index }};
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('add_type', 'table');
  fd.append('add_mode', mode);
  fd.append('add_index', index);
  const res = await fetch('/chart/config/add-form', {{ method: 'POST', body: fd }});
  modalBox.innerHTML = await res.text();
  modalOverlay.classList.add('open');
}}

// Show a confirm dialog before deleting a chart.
function openDeleteConfirm(cid) {{
  editingCid = null;
  addTarget = null;
  modalBox.innerHTML =
    '<div class="ff-modal-head"><span class="ff-modal-title">Delete chart</span></div>' +
    '<div class="ff-modal-body"><p class="ff-confirm-text"></p></div>' +
    '<div class="ff-modal-error" hidden></div>' +
    '<div class="ff-modal-foot">' +
    '<button type="button" class="ff-btn ff-cancel">Cancel</button>' +
    '<button type="button" class="ff-btn ff-danger" data-delete-cid>Delete</button>' +
    '</div>';
  // textContent so the id can't inject markup.
  modalBox.querySelector('.ff-confirm-text').textContent =
    'Delete "' + cid + '"? It will be removed from the dashboard.';
  modalBox.querySelector('[data-delete-cid]').dataset.deleteCid = cid;
  modalOverlay.classList.add('open');
}}

// Same confirm dialog for a header/separator, keyed by its layout-item index.
function openItemDeleteConfirm(index, kind) {{
  editingCid = null;
  addTarget = null;
  modalBox.innerHTML =
    '<div class="ff-modal-head"><span class="ff-modal-title">Delete ' + kind + '</span></div>' +
    '<div class="ff-modal-body"><p class="ff-confirm-text"></p></div>' +
    '<div class="ff-modal-error" hidden></div>' +
    '<div class="ff-modal-foot">' +
    '<button type="button" class="ff-btn ff-cancel">Cancel</button>' +
    '<button type="button" class="ff-btn ff-danger" data-delete-item>Delete</button>' +
    '</div>';
  modalBox.querySelector('.ff-confirm-text').textContent =
    'Delete this ' + kind + '? It will be removed from the dashboard.';
  modalBox.querySelector('[data-delete-item]').dataset.deleteItem = index;
  modalOverlay.classList.add('open');
}}

// Pencil edits, trash deletes, gutter "+" adds. All ignored while the pane is hidden.
outEl.addEventListener('click', e => {{
  // Switching tabs is a view action (not an edit), so allow it even in Preview
  // where the editor pane is hidden. During a move the capture handler below
  // gets the click first, so this only fires when not moving.
  const tabSwitchNav = e.target.closest('.fireflyer-tab-switch');
  if (tabSwitchNav) {{ switchTab(parseInt(tabSwitchNav.dataset.tabIndex, 10)); return; }}
  if (editingDisabled()) return;
  // Charts carry data-cid; headers/separators carry data-item-index; tabs carry
  // data-tab-index. The move, edit and delete buttons are shared markup, so
  // branch on which identifier is present.
  const move = e.target.closest('.fireflyer-move-btn');
  if (move) {{
    if (move.dataset.cid) enterMove(move.dataset.cid);
    else if (move.dataset.tabIndex !== undefined) enterTabMove(move.dataset.tabIndex, move.closest('.fireflyer-tab-wrap'));
    else enterItemMove(move.dataset.itemIndex, move.closest('.fireflyer-dashboard-item'));
    return;
  }}
  const edit = e.target.closest('.fireflyer-edit-btn');
  if (edit) {{
    if (edit.dataset.cid) openChartEditor(edit.dataset.cid);
    else if (edit.dataset.tabIndex !== undefined) startTabEdit(edit.closest('.fireflyer-tab-wrap').querySelector('.fireflyer-tab-switch'));
    else startHeaderEdit(edit.closest('.fireflyer-dashboard-item').querySelector('.fireflyer-dashboard-header'));
    return;
  }}
  const del = e.target.closest('.fireflyer-delete-btn');
  if (del) {{
    if (del.dataset.cid) openDeleteConfirm(del.dataset.cid);
    else if (del.dataset.tabIndex !== undefined) openTabDeleteConfirm(del.dataset.tabIndex);
    else openItemDeleteConfirm(del.dataset.itemIndex, del.closest('.fireflyer-dashboard-item').dataset.kind);
    return;
  }}
  const addCell = e.target.closest('.fireflyer-add-cell');
  if (addCell) {{ openAddChart('cell', addCell.dataset.row); return; }}
  const addRow = e.target.closest('.fireflyer-add-row-btn');
  if (addRow) {{ showAddMenu(addRow, addRow.closest('.fireflyer-add-row').dataset.before); return; }}
}});

// Insert-row "+" opens a small menu: chart (modal), header, or separator.
const addMenu = document.getElementById('ff-addmenu');
let addMenuBefore = null;

function showAddMenu(btn, before) {{
  addMenuBefore = before;
  const r = btn.getBoundingClientRect();
  addMenu.style.left = (r.right + 6) + 'px';
  addMenu.style.top = r.top + 'px';
  addMenu.hidden = false;
}}
function hideAddMenu() {{ addMenu.hidden = true; addMenuBefore = null; }}

async function insertLayoutItem(kind, before) {{
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('kind', kind);
  fd.append('before', before);
  const res = await fetch('/chart/config/insert-item', {{ method: 'POST', body: fd }});
  const data = await res.json();
  if (!data.ok) {{ flash(data.error || 'Could not insert.'); return; }}
  codeEl.value = data.yaml;
  run();
}}

addMenu.addEventListener('click', e => {{
  const opt = e.target.closest('[data-add-kind]');
  if (!opt) return;
  const kind = opt.dataset.addKind;
  const before = addMenuBefore;
  hideAddMenu();
  if (kind === 'chart') openAddChart('row', before);
  else if (kind === 'tab') insertTab(before);
  else insertLayoutItem(kind, before);
}});

// Dismiss the menu on an outside click or Escape.
document.addEventListener('click', e => {{
  if (!addMenu.hidden && !addMenu.contains(e.target) && !e.target.closest('.fireflyer-add-row-btn')) hideAddMenu();
}});
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') hideAddMenu(); }});

// Double-click a header (edit mode) to rename it inline. Enter/blur saves,
// Escape cancels; the save rewrites the header line and re-renders.
outEl.addEventListener('dblclick', e => {{
  if (editingDisabled() || inMove()) return;
  const h = e.target.closest('.fireflyer-dashboard-header.fireflyer-editable');
  if (h) startHeaderEdit(h);
}});

// Editing a header borrows move mode's focus feel: the rest of the dashboard dims
// and its hover affordances are suppressed (.ff-focus-mode), and the same topbar
// cancel button appears. `currentHeaderFinish` lets that button / Esc cancel the
// active edit. Enter (or blur) saves; Esc / cancel button restores the original.
// Inline rename shared by headers and tabs: `el` becomes contentEditable, the
// dashboard enters focus mode (dims the rest, shows the topbar cancel), and
// `saveFn(text)` runs on Enter/blur. `sourceEl` stays lit while editing.
// `onCancel` (optional) runs instead of restoring the text when the edit is
// cancelled — used to undo a just-added tab.
let currentHeaderFinish = null;
function beginInlineEdit(el, sourceEl, saveFn, onCancel) {{
  const original = el.textContent;
  const dash = dashboardEl();
  el.contentEditable = 'true';
  el.classList.add('editing');
  if (dash) dash.classList.add('ff-focus-mode');
  if (sourceEl) sourceEl.classList.add('ff-edit-source');
  positionMoveDiscard();
  moveDiscard.hidden = false;
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  let done = false;
  function finish(save) {{
    if (done) return;
    done = true;
    currentHeaderFinish = null;
    el.contentEditable = 'false';
    el.classList.remove('editing');
    if (dash) dash.classList.remove('ff-focus-mode');
    if (sourceEl) sourceEl.classList.remove('ff-edit-source');
    moveDiscard.hidden = true;
    el.removeEventListener('keydown', onKey);
    el.removeEventListener('blur', onBlur);
    const text = el.textContent.trim();
    if (save && text && text !== original) saveFn(text);
    // With onCancel set (a just-added tab), anything short of a real new name —
    // Esc, blur, or keeping the default — undoes the add: name it or cancel.
    else if (onCancel) onCancel();
    else el.textContent = original;           // plain rename -> restore the text
  }}
  currentHeaderFinish = finish;
  function onKey(ev) {{
    if (ev.key === 'Enter') {{ ev.preventDefault(); finish(true); }}
    else if (ev.key === 'Escape') {{ ev.preventDefault(); finish(false); }}
  }}
  function onBlur() {{ finish(true); }}
  el.addEventListener('keydown', onKey);
  el.addEventListener('blur', onBlur);
}}

function startHeaderEdit(h) {{
  beginInlineEdit(h, h.closest('.fireflyer-dashboard-item'), text => saveHeader(h.dataset.headerIndex, text));
}}
function startTabEdit(el) {{
  if (!el) return;
  beginInlineEdit(el, el.closest('.fireflyer-tab-wrap'), text => saveTab(el.dataset.tabIndex, text));
}}
// A freshly added tab: force a name. `revertYaml` is the pre-add document, so
// cancelling (Esc/✕/blur/keeping the default) removes the just-added tab.
function startTabEditForNew(el, revertYaml) {{
  if (!el) {{ return; }}
  beginInlineEdit(
    el, el.closest('.fireflyer-tab-wrap'),
    text => saveTab(el.dataset.tabIndex, text),
    () => {{ codeEl.value = revertYaml; run(); }},
  );
}}

async function saveHeader(index, text) {{
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('index', index);
  fd.append('text', text);
  const res = await fetch('/chart/config/header', {{ method: 'POST', body: fd }});
  const data = await res.json();
  if (!data.ok) {{ flash(data.error || 'Could not rename.'); return; }}
  codeEl.value = data.yaml;
  run();
}}

// --- Tabs -------------------------------------------------------------------
// The tab bar's editor gestures. Switching re-runs on the chosen tab (only its
// charts load). Add/rename/move/delete post to the tab config routes, swap the
// returned YAML in, and re-run — same pattern as the header/chart gestures.
async function postTab(url, params) {{
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  for (const [k, v] of Object.entries(params)) fd.append(k, v);
  const res = await fetch(url, {{ method: 'POST', body: fd }});
  const data = await res.json();
  if (!data.ok) {{ flash(data.error || 'Tab action failed.'); return null; }}
  codeEl.value = data.yaml;
  return data;
}}

async function switchTab(index) {{
  // A chart move OR a tab move can span tabs — keep whichever is active alive so
  // the target row can be found in another tab.
  const chartMove = moveCid;
  const tabMove = moveTabIndex;
  activeTab = index;
  await run();
  if (chartMove !== null) enterMove(chartMove);
  else if (tabMove !== null) enterTabMove(tabMove, outEl.querySelector('.fireflyer-tab-wrap[data-tab-index="' + tabMove + '"]'));
}}

// "Tab" in the between-rows "+" menu. On a flat dashboard the first pick enables
// tabs by wrapping the whole layout in one tab; once tabbed, a pick splits the
// current tab at that gap. Either way the new tab opens for a forced rename —
// cancelling undoes the add (reverting to `prevYaml`).
async function insertTab(before) {{
  const prevYaml = codeEl.value;
  if (!outEl.querySelector('.fireflyer-tabs')) {{
    if (await postTab('/chart/config/tab-add-first', {{}})) {{
      activeTab = 0;
      await run();
      startTabEditForNew(outEl.querySelector('.fireflyer-tab-switch[data-tab-index="0"]'), prevYaml);
    }}
    return;
  }}
  if (await postTab('/chart/config/tab-insert', {{ before }})) {{
    await run();
    // insert_tab names the new tab "New tab"; the forced rename keeps at most one
    // such tab around, so this reliably finds the one just added.
    startTabEditForNew(outEl.querySelector('.fireflyer-tab-switch[data-tab-name="New tab"]'), prevYaml);
  }}
}}

function saveTab(index, name) {{
  postTab('/chart/config/tab-rename', {{ index, name }}).then(d => {{ if (d) run(); }});
}}

// First tab dissolves every tab back to a flat list; any other merges into the
// previous. Read the tab names from the bar so the first-tab confirm can list
// what will be removed.
function openTabDeleteConfirm(index) {{
  editingCid = null;
  addTarget = null;
  const names = [...outEl.querySelectorAll('.fireflyer-tab-switch')].map(t => t.textContent);
  const first = String(index) === '0';
  const msg = first
    ? 'Deleting the first tab removes all tabs and flattens the dashboard. Tabs removed: ' + names.join(', ') + '.'
    : 'Delete tab "' + (names[index] || '') + '"? Its charts merge into the previous tab.';
  modalBox.innerHTML =
    '<div class="ff-modal-head"><span class="ff-modal-title">Delete tab</span></div>' +
    '<div class="ff-modal-body"><p class="ff-confirm-text"></p></div>' +
    '<div class="ff-modal-error" hidden></div>' +
    '<div class="ff-modal-foot">' +
    '<button type="button" class="ff-btn ff-cancel">Cancel</button>' +
    '<button type="button" class="ff-btn ff-danger" data-delete-tab>Delete</button>' +
    '</div>';
  modalBox.querySelector('.ff-confirm-text').textContent = msg;
  modalBox.querySelector('[data-delete-tab]').dataset.deleteTab = index;
  modalOverlay.classList.add('open');
}}

// --- Move mode --------------------------------------------------------------
// Click a chart's "move" button to enter move mode: the picked chart stays lit,
// every other interaction (resize, edit, add, crossfilter) is turned off, and
// every valid spot lights up as a blue box — the column zones (place before/after
// a cell) plus the between-rows strips (into a new row). The hovered box goes
// solid as a placement preview. Click a box to commit (server rewrites the
// layout, we re-render); Esc/Cancel exits.
// A move is either a chart (moveCid set) or a header/separator (moveItemIndex
// set). A header/separator can only land between rows, so it uses just the
// between-row strips — no side/merge zones.
let moveCid = null;
let moveItemIndex = null;
let moveTabIndex = null;
const inMove = () => moveCid !== null || moveItemIndex !== null || moveTabIndex !== null;
const dashboardEl = () => outEl.querySelector('.fireflyer-dashboard');
const moveDiscard = document.getElementById('ff-move-cancel');
const outputPane = outEl.closest('.pane');

// Align the cancel button's left edge with the output pane so it covers exactly
// the right side, no matter where the pane split is dragged.
function positionMoveDiscard() {{
  moveDiscard.style.left = outputPane.getBoundingClientRect().left + 'px';
}}
new ResizeObserver(() => {{ if (inMove() || currentHeaderFinish) positionMoveDiscard(); }}).observe(outputPane);

async function postMove(url, params) {{
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  for (const [k, v] of Object.entries(params)) fd.append(k, v);
  const res = await fetch(url, {{ method: 'POST', body: fd }});
  const data = await res.json();
  if (!data.ok) {{ flash(data.error || 'Could not move.'); return; }}
  codeEl.value = data.yaml;
  run();
}}

// Build the drop zones for move mode, per the documented rules:
//  R1/R3/R4 — a SIDE zone on the left and right of every chart (except the
//    dragged one), at the chart's own height; a merged chart's cell is tall, so
//    its sides are full height and dropping there adopts its span.
//  R2 — common borders collapse to ONE drop (dedup), keeping the taller side.
//  R6 — no side zone on the dragged chart, nor on any border it shares.
//  R5 — a SINGLE merge bar, only for the dragged chart, only if a chart-row is
//    directly below it: a long bar down its centre into that row — drop to grow
//    it down one row (merge_down).
//  (between-rows: the whole-width add-row strips + internal-group gaps.)
function buildMoveZones() {{
  const dash = dashboardEl();
  if (!dash) return;
  const overlay = document.createElement('div');
  overlay.className = 'ff-move-overlay';
  const base = dash.getBoundingClientRect();
  const cells = [...dash.querySelectorAll('.fireflyer-dashboard-cell[data-cid]')];
  const breaks = [...dash.querySelectorAll('.fireflyer-dashboard-header, .fireflyer-dashboard-separator')]
    .map(el => {{ const r = el.getBoundingClientRect(); return (r.top + r.bottom) / 2; }});
  const srcCell = dash.querySelector('.fireflyer-dashboard-cell.ff-move-source');
  // A MERGED dragged chart keeps its shared borders (R3): each is a per-row
  // unmerge zone — dropping there puts it back single-row in that row. A plain
  // dragged chart suppresses them (R6).
  const srcMerged = srcCell && (srcCell.style.gridRow || '').includes('span');

  // Candidate side zones: both edges of every chart, grouped into visual rows.
  const bands = new Map();
  cells.forEach(c => {{
    const k = Math.round(c.getBoundingClientRect().top);
    if (!bands.has(k)) bands.set(k, []);
    bands.get(k).push(c);
  }});
  const cand = [];
  bands.forEach(band => {{
    const sorted = band.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
    sorted.forEach((cell, i) => {{
      if (cell.dataset.cid === moveCid) return;                        // R6: not the dragged chart itself
      const r = cell.getBoundingClientRect();
      const merged = (cell.style.gridRow || '').includes('span');
      const leftMoved = !srcMerged && i > 0 && sorted[i - 1].dataset.cid === moveCid;    // R6 (R3 keeps them)
      const rightMoved = !srcMerged && i < sorted.length - 1 && sorted[i + 1].dataset.cid === moveCid;
      if (!leftMoved) cand.push({{ x: r.left, top: r.top, bottom: r.bottom, h: r.height, dst: cell.dataset.cid, pos: 'before', merged }});
      if (!rightMoved) cand.push({{ x: r.right, top: r.top, bottom: r.bottom, h: r.height, dst: cell.dataset.cid, pos: 'after', merged }});
    }});
  }});
  dedupBorders(cand).forEach(z => addZone(overlay, base, z.x, z.top, z.h, z.dst, z.pos));

  // R5: the one merge bar, for the dragged chart, if a chart-row sits below it.
  if (srcCell) {{
    const r = srcCell.getBoundingClientRect();
    const belowBottom = rowBelow(cells, r, breaks);
    if (belowBottom !== null) addMergeZone(overlay, base, (r.left + r.right) / 2, r.top, belowBottom - r.top);
  }}

  dash.querySelectorAll('.fireflyer-dashboard-row').forEach(row => {{
    const rc = [...row.querySelectorAll('.fireflyer-dashboard-cell[data-cid]')];
    if (rc.length) addInternalRowZones(overlay, base, row, rc);
  }});
  dash.appendChild(overlay);
}}
// Collapse duplicate common borders (R2) — but only between NON-merged charts. A
// merged chart keeps its own full-height side zone (R5), so two zones on one
// border are dropped to one only when neither is merged; a merged chart's side
// always survives and sits next to the neighbour's.
function dedupBorders(zones) {{
  const kept = [];
  zones.forEach(z => {{
    const dup = !z.merged && kept.find(k =>
      !k.merged && Math.abs(k.x - z.x) < 14 && z.top < k.bottom - 4 && z.bottom > k.top + 4);
    if (!dup) kept.push(z);
  }});
  return kept;
}}
// Bottom y of the row directly below rect `r` — the nearest row of cells whose
// top sits below r, with no header/separator between. null if there's none.
function rowBelow(cells, r, breaks) {{
  let nextTop = Infinity;
  cells.forEach(c => {{
    const t = c.getBoundingClientRect().top;
    if (t > r.bottom - 4 && t < nextTop) nextTop = t;
  }});
  if (!isFinite(nextTop)) return null;
  if (breaks.some(y => y > r.bottom && y < nextTop)) return null;
  // The row's own height is its SHORTEST cell — a cell that itself spans further
  // down (e.g. a 2-row pie) must not stretch the merge bar past one row.
  let bottom = null;
  cells.forEach(c => {{
    const cr = c.getBoundingClientRect();
    if (Math.abs(cr.top - nextTop) < 4) bottom = Math.min(bottom === null ? cr.bottom : bottom, cr.bottom);
  }});
  return bottom;
}}
function addMergeZone(overlay, base, cx, top, height) {{
  const z = document.createElement('div');
  z.className = 'ff-move-zone ff-move-zone-span';
  z.style.left = (cx - base.left) + 'px';
  z.style.top = (top - base.top) + 'px';
  z.style.height = height + 'px';
  z.dataset.mergeDown = '1';
  overlay.appendChild(z);
}}
// A multi-row group renders as one grid; add a horizontal box at each boundary
// between its rows so a chart can be dropped into its own row between them.
function addInternalRowZones(overlay, base, row, cells) {{
  const ordinals = (row.dataset.ordinals || '').split(',').filter(Boolean);
  if (ordinals.length < 2) return;
  const rr = row.getBoundingClientRect();
  const tops = [...new Set(cells.map(c => Math.round(c.getBoundingClientRect().top)))].sort((a, b) => a - b);
  for (let i = 1; i < tops.length && i < ordinals.length; i++) {{
    const z = document.createElement('div');
    z.className = 'ff-move-zone-h';
    z.style.left = (rr.left - base.left) + 'px';
    z.style.top = (tops[i] - base.top) + 'px';
    z.style.width = rr.width + 'px';
    z.dataset.before = ordinals[i];   // new row before this YAML row
    overlay.appendChild(z);
  }}
}}
function addZone(overlay, base, x, top, height, dst, position) {{
  const z = document.createElement('div');
  z.className = 'ff-move-zone';
  z.style.left = (x - base.left) + 'px';
  z.style.top = (top - base.top) + 'px';
  z.style.height = height + 'px';
  z.dataset.dst = dst;
  z.dataset.position = position;
  overlay.appendChild(z);
}}

function enterMove(cid) {{
  moveCid = cid;
  const dash = dashboardEl();
  if (dash) {{
    dash.classList.add('ff-move-mode');
    const src = dash.querySelector('.fireflyer-dashboard-cell[data-cid="' + cid + '"]');
    if (src) src.classList.add('ff-move-source');
    buildMoveZones();
  }}
  positionMoveDiscard();
  moveDiscard.hidden = false;
}}
// Header/separator move: no drop-zone overlay — the between-row strips (already
// lit by ff-move-mode) are the only valid targets.
function enterItemMove(index, itemEl) {{
  moveItemIndex = index;
  const dash = dashboardEl();
  if (dash) {{
    dash.classList.add('ff-move-mode');
    if (itemEl) {{
      itemEl.classList.add('ff-move-source');
      // The strips right above and below the item are no-ops (they'd drop it back
      // where it already is), so hide them — only real relocations stay lit.
      [itemEl.previousElementSibling, itemEl.nextElementSibling].forEach(el => {{
        if (el && el.classList.contains('fireflyer-add-row')) el.classList.add('ff-move-hidden-strip');
      }});
    }}
  }}
  positionMoveDiscard();
  moveDiscard.hidden = false;
}}
// Tab move: like a header/separator move, its only drop targets are the
// between-row strips (already lit by ff-move-mode). Dropping moves the tab's
// key line — reordering it and reassigning the rows that fall under it.
function enterTabMove(index, wrapEl) {{
  moveTabIndex = index;
  const dash = dashboardEl();
  if (dash) {{
    dash.classList.add('ff-move-mode');
    if (wrapEl) wrapEl.classList.add('ff-move-source');
  }}
  positionMoveDiscard();
  moveDiscard.hidden = false;
}}
function exitMove() {{
  const dash = dashboardEl();
  if (dash) {{
    dash.classList.remove('ff-move-mode');
    dash.querySelectorAll('.ff-move-source').forEach(el => el.classList.remove('ff-move-source'));
    dash.querySelectorAll('.ff-move-hidden-strip').forEach(el => el.classList.remove('ff-move-hidden-strip'));
    const overlay = dash.querySelector('.ff-move-overlay');
    if (overlay) overlay.remove();
  }}
  moveDiscard.hidden = true;
  moveCid = null;
  moveItemIndex = null;
  moveTabIndex = null;
}}

// While moving, capture clicks: a blue box commits, anything else cancels — and
// the event is swallowed so nothing else (crossfilter, chart controls) fires.
outEl.addEventListener('click', e => {{
  if (!inMove()) return;
  e.preventDefault();
  e.stopPropagation();
  // Switching tabs mid-move lets a chart or a tab boundary be dropped into
  // another tab: the move stays alive across the re-render (switchTab re-enters
  // it), so the target row can be found there. (A header/separator move stays in
  // the current view.)
  const tabSwitch = e.target.closest('.fireflyer-tab-switch');
  if (tabSwitch) {{
    if (moveCid !== null || moveTabIndex !== null) switchTab(parseInt(tabSwitch.dataset.tabIndex, 10));
    return;
  }}
  const strip = e.target.closest('.fireflyer-add-row');
  // Tab move: only a between-row strip is valid — it repositions the tab's
  // boundary there (reordering the tab and reassigning the rows below it).
  if (moveTabIndex !== null) {{
    if (strip) {{
      const index = moveTabIndex;
      exitMove();
      postMove('/chart/config/tab-move', {{ index, before: strip.dataset.before }});
    }}
    return;
  }}
  // Header/separator: only a between-row strip is a valid drop; anything else
  // is a miss (move mode stays active until Esc/Cancel or an outside click).
  if (moveItemIndex !== null) {{
    if (strip) {{
      const index = moveItemIndex;
      exitMove();
      postMove('/chart/config/move-item', {{ index, before: strip.dataset.before }});
    }}
    return;
  }}
  const zone = e.target.closest('.ff-move-zone, .ff-move-zone-h');
  const src = moveCid;
  if (zone && zone.dataset.mergeDown) {{
    exitMove();
    postMove('/chart/config/merge-down', {{ cid: src }});
  }} else if (zone && zone.dataset.dst) {{
    exitMove();
    postMove('/chart/config/move', {{ src, dst: zone.dataset.dst, position: zone.dataset.position }});
  }} else if (zone && zone.dataset.before !== undefined) {{
    exitMove();
    postMove('/chart/config/new-row', {{ src, before: zone.dataset.before }});
  }} else if (strip) {{
    exitMove();
    postMove('/chart/config/new-row', {{ src, before: strip.dataset.before }});
  }}
  // A miss inside the pane (a chart or blank space) does nothing — the click is
  // already swallowed, so move mode stays active. Only Esc/Cancel or a click
  // outside the pane exits.
}}, true);

// Block every other pointer interaction (resize starts on mousedown) while moving.
outEl.addEventListener('mousedown', e => {{ if (inMove()) e.stopPropagation(); }}, true);

// During a header edit, cancel on mousedown and preventDefault so the header
// keeps focus (a plain click would blur it first and save). Move mode uses click.
moveDiscard.addEventListener('mousedown', e => {{
  if (currentHeaderFinish) {{ e.preventDefault(); currentHeaderFinish(false); }}
}});
moveDiscard.addEventListener('click', () => {{ if (inMove()) exitMove(); }});
document.addEventListener('keydown', e => {{ if (e.key === 'Escape' && inMove()) exitMove(); }});
// A click anywhere outside the output pane (editor, topbar, blank space) cancels
// the move — clicks inside the pane are handled above (a zone commits, a miss
// cancels), and are stopped from reaching here.
document.addEventListener('click', e => {{
  if (inMove() && !outEl.contains(e.target)) exitMove();
}});

// Confirm-delete button inside the modal.
modalBox.addEventListener('click', async e => {{
  const chartBtn = e.target.closest('[data-delete-cid]');
  const itemBtn = e.target.closest('[data-delete-item]');
  const tabBtn = e.target.closest('[data-delete-tab]');
  if (!chartBtn && !itemBtn && !tabBtn) return;
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  let url;
  if (chartBtn) {{ fd.append('cid', chartBtn.dataset.deleteCid); url = '/chart/config/delete'; }}
  else if (itemBtn) {{ fd.append('index', itemBtn.dataset.deleteItem); url = '/chart/config/delete-item'; }}
  else {{ fd.append('index', tabBtn.dataset.deleteTab); url = '/chart/config/tab-delete'; }}
  const res = await fetch(url, {{ method: 'POST', body: fd }});
  const data = await res.json();
  if (!data.ok) {{
    const err = modalBox.querySelector('.ff-modal-error');
    err.textContent = data.error || 'Could not delete.';
    err.hidden = false;
    return;
  }}
  codeEl.value = data.yaml;
  closeModal();
  run();
}});

// Backdrop / Cancel / Escape close.
modalOverlay.addEventListener('click', e => {{
  if (e.target === modalOverlay || e.target.closest('.ff-cancel')) closeModal();
}});
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape' && modalOverlay.classList.contains('open')) closeModal();
}});

// Swapping the chart type re-fetches the form so its fields match the new
// type's params (overlapping values carry over from the saved config).
modalBox.addEventListener('change', async e => {{
  const sel = e.target.closest('[data-type-select]');
  if (!sel) return;
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  let endpoint;
  if (editingCid === null) {{           // add mode: rebuild the create form
    fd.append('add_type', sel.value);
    fd.append('add_mode', addTarget.mode);
    fd.append('add_index', addTarget.index);
    endpoint = '/chart/config/add-form';
  }} else {{                            // edit mode: rebuild for the new type
    fd.append('cid', editingCid);
    fd.append('type_override', sel.value);
    endpoint = '/chart/config/form';
  }}
  const res = await fetch(endpoint, {{ method: 'POST', body: fd }});
  modalBox.innerHTML = await res.text();
}});

// Filter builder: add clones the blank-row template; the × removes a row.
modalBox.addEventListener('click', e => {{
  const add = e.target.closest('.ff-filter-add');
  if (add) {{
    const wrap = add.closest('.ff-filters');
    const tpl = wrap.querySelector('.ff-filter-tpl');
    wrap.querySelector('.ff-filter-rows').appendChild(tpl.content.cloneNode(true));
    return;
  }}
  const del = e.target.closest('.ff-filter-del');
  if (del) del.closest('.ff-filter-row').remove();
}});

// Save/Add: submit fields + current YAML; on success swap the editor and re-run.
// Edit posts to /save (needs cid); add posts to /create (placement is in the
// form's hidden inputs).
modalBox.addEventListener('submit', async e => {{
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);
  fd.append('yaml_text', codeEl.value);
  let endpoint;
  if (editingCid === null) {{
    endpoint = '/chart/config/create';
  }} else {{
    fd.append('cid', editingCid);
    endpoint = '/chart/config/save';
  }}
  const res = await fetch(endpoint, {{ method: 'POST', body: fd }});
  const data = await res.json();
  if (!data.ok) {{
    const err = form.querySelector('.ff-modal-error');
    err.textContent = data.error || 'Could not save.';
    err.hidden = false;
    return;
  }}
  codeEl.value = data.yaml;
  closeModal();
  run();
}});
</script>
</body>
</html>
"""


# Topbar pieces. Nav is a hamburger link to the dashboards gallery; the brand is
# a link there too in portal mode (a plain span locally, where `/` is the editor
# itself). The save button/name-edit/theme JS all live in the main INDEX script.
_NAV_HTML = '<a class="ff-nav" href="/" title="Dashboards" aria-label="Dashboards">☰</a>'


def _brand_html(link: bool) -> str:
    if link:
        return '<a class="brand" href="/" title="Dashboards">Fireflyer</a>'
    return '<span class="brand">Fireflyer</span>'


def _dash_name_html(name: str) -> str:
    # Sits after the logo, set off by a dot. Editable in place (wired by the
    # INDEX script); two-way with the YAML `name:`.
    return (
        '<span class="ff-sep" aria-hidden="true">·</span>'
        f'<span class="ff-dash-name" id="ff-dash-name">{escape(name)}</span>'
    )


def _save_html(dash_id: str) -> str:
    # Hidden until there are unsaved changes (toggled by updateSaveState()).
    return f'<button class="run" id="ff-save" data-save-url="/d/{dash_id}/save" hidden>Save</button>'


# Icon-only theme switch. Inline SVG (stroke=currentColor so it inherits the
# segment colour): "A" for auto, sun for light, moon for dark. No text labels —
# `title`/`aria-label` carry the meaning.
_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{}</svg>'
_THEME_ICONS = {
    "auto": _ICON_SVG.format('<path d="M5 20 12 4l7 16M7.5 14h9"/>'),
    "light": _ICON_SVG.format(
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
        'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
    ),
    "dark": _ICON_SVG.format('<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>'),
}


def _theme_switch() -> str:
    labels = {"auto": "Auto (follow OS)", "light": "Light", "dark": "Dark"}
    buttons = "".join(
        f'<button type="button" data-mode="{mode}" title="Theme: {labels[mode]}"'
        f' aria-label="Theme: {labels[mode]}">{_THEME_ICONS[mode]}</button>'
        for mode in ("auto", "light", "dark")
    )
    return f'<div class="ff-theme" id="theme-switch" role="group" aria-label="Theme">{buttons}</div>'


def render_editor_page(
    yaml_text: str,
    *,
    nav: str = "",
    brand: str = "",
    dash_name: str = "",
    save: str = "",
    theme: str = "",
    user_menu: str = "",
) -> str:
    """The editor page seeded with `yaml_text` and topbar pieces — left: `nav`
    (Dashboards link) + `brand` (logo); centered editable `dash_name`; right:
    `save` (shown only when unsaved), Preview, `theme` switch, `user_menu`
    (profile). INDEX carries the placeholders; both modes share it."""
    return (
        INDEX.replace("__FF_YAML_CONTENT__", escape(yaml_text))
        .replace("__FF_NAV__", nav)
        .replace("__FF_BRAND__", brand)
        .replace("__FF_DASH_NAME__", dash_name)
        .replace("__FF_SAVE__", save)
        .replace("__FF_THEME__", theme)
        .replace("__FF_USER_MENU__", user_menu)
    )


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
async def login_submit(username: str = Form(""), password: str = Form("")):
    identity = app.state.authenticator.verify(username, password)
    if not identity:
        return auth_mod.login_page("Invalid username or password.", PORTAL_TITLE)
    resp = RedirectResponse("/", status_code=303)
    auth_mod.set_session(resp, identity)
    return resp


@app.post("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    auth_mod.clear_session(resp)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    # Portal on: gallery of stored dashboards. Off: the single-dashboard editor.
    if app.state.store is not None:
        return portal_mod.render_gallery(
            app.state.store.list(), PORTAL_TITLE, _user_menu(request)
        )
    # Local mode: no nav/save/profile; just logo, editable name, theme switch.
    return render_editor_page(
        DEFAULT_YAML,
        brand=_brand_html(link=False),
        dash_name=_dash_name_html(Dashboard.from_yaml(DEFAULT_YAML).name),
        theme=_theme_switch(),
    )


def _empty_yaml(name: str) -> str:
    """A blank but valid dashboard named `name` — no datasets, charts, or layout
    yet. The author fills it in the editor. json.dumps quotes the name safely."""
    return f"name: {json.dumps(name)}\n\ndatasets: {{}}\n\ncharts: {{}}\n\ndashboard: []\n"


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
async def portal_new(request: Request, name: str = Form("")) -> RedirectResponse:
    yaml_text = _empty_yaml(name.strip() or "Untitled dashboard")
    new_id = app.state.store.create(yaml_text, _current_author(request))
    return RedirectResponse(f"/d/{new_id}", status_code=303)


@app.post("/d/{dash_id}/clone")
async def portal_clone(dash_id: str, request: Request, name: str = Form("")):
    row = app.state.store.get(dash_id)
    if row is None:
        return HTMLResponse("Dashboard not found", status_code=404)
    new_name = name.strip() or f"{row.name} (copy)"
    new_id = app.state.store.create(
        _set_yaml_name(row.yaml, new_name), _current_author(request)
    )
    return RedirectResponse(f"/d/{new_id}", status_code=303)


@app.get("/d/{dash_id}", response_class=HTMLResponse)
def portal_open(dash_id: str, request: Request) -> HTMLResponse:
    row = app.state.store.get(dash_id)
    if row is None:
        return HTMLResponse("Dashboard not found", status_code=404)
    # Theme switch lives centered inside the profile menu (topbar slot empty).
    theme_in_menu = f'<div class="ff-profile-theme">{_theme_switch()}</div>'
    page = render_editor_page(
        row.yaml,
        nav=_NAV_HTML,
        brand=_brand_html(link=True),
        dash_name=_dash_name_html(row.name),
        save=_save_html(row.id),
        user_menu=_user_menu(request, extra=theme_in_menu),
    )
    return HTMLResponse(page)


@app.post("/d/{dash_id}/save")
async def portal_save(dash_id: str, yaml_text: str = Form("")) -> dict:
    try:
        app.state.store.save(dash_id, yaml_text)
        return {"ok": True}
    except DashboardError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/d/{dash_id}/delete")
async def portal_delete(dash_id: str) -> RedirectResponse:
    app.state.store.delete(dash_id)
    return RedirectResponse("/", status_code=303)


@app.post("/execute")
async def execute(request: Request, active_tab: int = 0) -> dict:
    body = (await request.body()).decode("utf-8")
    try:
        dashboard = Dashboard.from_yaml(body)
        # The response is a skeleton — each cell fetches itself via htmx so
        # charts render in parallel and slow charts don't block fast ones.
        # editing=True adds the editor-only row resize handles. `active_tab`
        # (a query param) keeps the editor on the current tab after an edit.
        return {
            "ok": True,
            "html": dashboard.render_skeleton(editing=True, active_tab=active_tab),
        }
    except DashboardError as exc:
        return {"ok": False, "html": f'<pre class="error">{escape(str(exc))}</pre>'}
    except Exception:
        return {
            "ok": False,
            "html": f'<pre class="error">{escape(traceback.format_exc())}</pre>',
        }


class ChatRequest(BaseModel):
    message: str
    yaml: str = ""
    history: list[dict] = []


@app.post("/chat")
async def chat(req: ChatRequest) -> dict:
    """One AI-assistant turn. Returns {ok, reply, yaml?}. `yaml` is present only
    when the assistant proposed a change that parsed cleanly; the browser then
    swaps it into the editor and re-renders."""
    if not CHAT_ENABLED:
        return {
            "ok": False,
            "reply": "AI assistant is disabled. Set ANTHROPIC_API_KEY in .env and restart.",
        }
    try:
        result = chat_mod.run_chat(req.message, req.yaml, req.history)
        return {"ok": True, **result}
    except Exception as exc:  # surface SDK/auth/network errors as a chat reply
        return {"ok": False, "reply": f"Assistant error: {exc}"}


@app.get("/chart/map", response_class=HTMLResponse)
def chart_map(
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
    return chart.to_html()


@app.get("/chart/table", response_class=HTMLResponse)
def chart_table(
    dataset: str,
    title: str,
    search: int = 1,
    pagination: int = 5,
    page: int = 1,
    q: str = "",
    filters: str = "",
) -> str:
    # `filters` is JSON-encoded by Table._base_params so the chart's own htmx
    # round-trips (search, pagination) preserve declared + merged crossfilters.
    parsed = json.loads(filters) if filters else []
    chart = Table(
        dataset=dataset,
        title=title,
        search=bool(search),
        pagination=pagination,
        filters=parsed,
    )
    return chart.to_html(page=page, query=q)


@app.post("/dashboard", response_class=HTMLResponse)
async def dashboard_render(
    yaml_text: str = Form(""),
    cf: list[str] = Form(default=[]),
    toggle: str = Form(""),
    editing: str = Form(""),
    active_tab: int = Form(0),
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
        dashboard = Dashboard.from_yaml(yaml_text)
    except DashboardError as exc:
        return f'<pre class="error">{escape(str(exc))}</pre>'
    new_tokens = filters_mod.toggle_token(list(cf), toggle) if toggle else list(cf)
    # Returning a skeleton means every cell re-fetches with the new cf state.
    # The dashboard div is swapped (outerHTML) and the fresh placeholders fire
    # hx-trigger="load" — same async path as the initial /execute response.
    # `active_tab` rides along (hidden input) so a crossfilter click or a tab
    # switch keeps the right tab showing.
    return dashboard.render_skeleton(
        cf_tokens=new_tokens, editing=bool(editing), active_tab=active_tab
    )


@app.post("/dashboard/cell", response_class=HTMLResponse)
async def dashboard_cell(
    yaml_text: str = Form(""),
    cid: str = Form(""),
    cf: list[str] = Form(default=[]),
    col: str = Form("1"),
    row: str = Form("1"),
    editing: str = Form(""),
) -> str:
    """Render a single dashboard cell. Triggered by the skeleton's per-cell
    hx-post; the response replaces the placeholder via outerHTML swap.

    `col` / `row` round-trip the CSS grid placement from the skeleton so the
    rendered cell lands in the right slot (and preserves any merged span).
    `editing` adds the per-chart edit (pencil) button."""
    try:
        dashboard = Dashboard.from_yaml(yaml_text)
        return dashboard.render_cell(
            cid, cf_tokens=list(cf), col=col, row=row, editing=bool(editing)
        )
    except DashboardError as exc:
        return f'<pre class="error">{escape(str(exc))}</pre>'


@app.post("/chart/config/form", response_class=HTMLResponse)
async def chart_config_form(
    yaml_text: str = Form(""),
    cid: str = Form(""),
    type_override: str = Form(""),
) -> str:
    """Build the edit-modal form for one chart from the current YAML.

    `type_override` re-renders the fields for a different chart type when the
    user changes the type dropdown."""
    try:
        return config_edit.build_form(yaml_text, cid, type_override=type_override)
    except (config_edit.ConfigEditError, DashboardError) as exc:
        return f'<div class="ff-modal-error" role="alert">{escape(str(exc))}</div>'


@app.post("/chart/config/add-form", response_class=HTMLResponse)
async def chart_config_add_form(
    yaml_text: str = Form(""),
    add_type: str = Form("table"),
    add_mode: str = Form("row"),
    add_index: str = Form("end"),
) -> str:
    """Build the create-chart modal (defaults for a fresh chart of `add_type`).
    Placement (`add_mode`/`add_index`) rides along as hidden inputs."""
    try:
        return config_edit.build_add_form(yaml_text, add_type, add_mode, add_index)
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
