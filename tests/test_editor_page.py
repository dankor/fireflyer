"""Static guards on the editor page (`editor.html` + `static/editor.css` and
`static/editor.js`). Its interactive behavior is
JS/browser territory the snapshot suite can't reach, but a couple of regressions
are cheap to pin down from the rendered HTML/CSS — notably that the "stale"
preview overlay stays *interactive*: a `pointer-events: none` there once made
the greyed preview swallow clicks and broke the row/column resize handles."""

import re

from pathlib import Path

from fireflyer.web.app import DEFAULT_YAML, _theme_switch, render_editor_page
from fireflyer.web.assets import STATIC_DIR

EDITOR_CSS = (STATIC_DIR / "editor.css").read_text()
EDITOR_JS = (STATIC_DIR / "editor.js").read_text()


def _page() -> str:
    """The page as the browser ends up with it: the markup plus the stylesheet
    and script it links, which the guards below read as one text."""
    page = render_editor_page(DEFAULT_YAML, theme=_theme_switch())
    return page + EDITOR_CSS + EDITOR_JS


def test_stale_preview_is_greyed_but_still_interactive():
    page = _page()
    m = re.search(r"\.pane\.output\.stale \.pane-body \{([^}]*)\}", page)
    assert m, "the `.stale .pane-body` rule is missing"
    rule = m.group(1)
    assert "opacity" in rule  # greyed as a stale cue
    # ...but not disabled — `pointer-events: none` here broke vertical resize.
    assert "pointer-events" not in rule


def test_refresh_overlay_and_stale_wiring_present():
    page = _page()
    assert 'id="output-pane"' in page and 'class="pane output"' in page
    assert 'id="refresh"' in page and "ff-refresh" in page
    assert "function markStale" in page
    assert "addEventListener('input'" in page and "markStale()" in page


def test_run_button_and_status_removed():
    page = _page()
    assert 'id="run"' not in page
    assert 'id="status"' not in page


def test_row_resize_rewrite_is_yaml_style_agnostic():
    # Row-height drags rewrite the Nth `@height` token directly. An earlier
    # version scanned for the row's `[ ... ]` flow-style brackets and silently
    # no-op'd on block-style rows, so drags snapped back. Guard against a revert
    # to that bracket-only approach (verified for real via a browser drag).
    page = _page()
    assert "function setRowUnits" in page
    assert "rowBracketSpan" not in page
    assert "lastIndexOf('['" not in page


def test_default_dashboard_parses_and_every_chart_renders():
    """The starter dashboard doubles as the quick guide — it's the first thing a
    new user sees and the example the AI assistant is shown. A YAML typo or a
    stale calc key in it is a broken first impression, and nothing else would
    catch it: the seed swallows exceptions so startup can't crash.

    Rendered with **no dataset store at all**, which is the point: the starter
    dashboard carries its own data in an inline `datasets:` block, so a fresh
    checkout renders with nothing uploaded and nothing seeded.
    """
    import fireflyer as ff

    dashboard = ff.Dashboard.from_yaml(DEFAULT_YAML)
    assert dashboard.name
    assert dashboard.tabs, "the guide demonstrates tabs"

    for cid in dashboard.chart_configs:
        html = dashboard.render_cell(cid, cf_tokens=[])
        assert 'class="error"' not in html, f"{cid} failed to render"
        assert "<article" in html, cid


def test_default_dashboard_is_commented():
    """It's a guide, not just a dashboard — the comments are the content, and a
    reformat that drops them defeats the point."""
    comments = [l for l in DEFAULT_YAML.splitlines() if l.strip().startswith("#")]
    assert len(comments) > 40, len(comments)
    for key in ("calcs", "charts", "layout", "datasets"):
        assert f"{key}:" in DEFAULT_YAML
    # The data block goes last — the readable parts stay at the top.
    assert DEFAULT_YAML.index("\ndatasets:") > DEFAULT_YAML.index("\nlayout:")


# --- mode switch --------------------------------------------------------------


def test_the_mode_switch_sits_before_the_dashboard_name():
    """One segmented control, on the left, ahead of the name — where the eye
    starts. It replaced a row of separate toggles on the right."""
    page = _page()
    left = page[page.index('<div class="topbar-left">') : page.index('<div class="topbar-right">')]
    assert 'id="ff-modes"' in left, "the switch belongs in the left group"
    # The name slot is filled only in portal/paths mode; when it is, the switch
    # comes first.
    if "ff-dash-name" in left:
        assert left.index('id="ff-modes"') < left.index("ff-dash-name")


def test_the_switch_has_one_segment_per_mode_and_no_text():
    """Icons only, per the project's UI style; the title/aria-label carry the
    meaning."""
    import re

    page = _page()
    switch = page[page.index('id="ff-modes"') :]
    switch = switch[: switch.index("</div>")]
    assert re.findall(r'data-mode="(\w+)"', switch) == [
        "view", "edit", "code", "chat", "calcs", "docs"
    ]
    # No segment carries a text label; each carries a tooltip instead.
    assert not re.search(r">\s*[A-Za-z]+\s*</button>", switch)
    buttons = re.findall(r"<button[^>]*>", switch)
    assert len(buttons) == 6
    for button in buttons:                       # meaning lives in the tooltip
        assert "title=" in button and "aria-label=" in button


def test_the_buttons_the_switch_replaced_are_gone():
    page = _page()
    for gone in ('id="ff-chat-btn"', 'id="ff-calcs-btn"', 'id="ff-docs-btn"',
                 'id="toggle"', 'id="pane-resizer"'):
        assert gone not in page, gone


def test_the_dashboard_is_full_width_by_default():
    """View is the landing mode and the canvas is the page — no split, no
    half-screen preview."""
    page = _page()
    assert '<div class="layout mode-view"' in page
    assert "setMode('view');" in page
    # The layout is a single positioned container, not a two-column grid.
    start = page.index("\n.layout {")
    rule = page[start : page.index("}", start)]
    assert "grid-template-columns" not in rule
    assert "position: relative" in rule


def test_every_panel_starts_closed():
    """Chat, docs and calcs are panels over the canvas, opened by a mode."""
    import re

    page = _page()
    panels = re.findall(r'<aside class="ff-docs ff-panel[^"]*" id="([\w-]+)" hidden>', page)
    assert sorted(panels) == ["ff-calcs", "ff-chat", "ff-docs", "ff-yaml"]


def test_edit_mode_opens_no_panel():
    """Edit is for rearranging the dashboard, so nothing may cover it — the YAML
    has its own `code` mode instead."""
    page = _page()
    assert "edit: null" in page, "edit must map to no panel"
    assert "code: 'ff-yaml'" in page, "the YAML belongs to `code`, not `edit`"


def test_code_mode_shows_the_yaml_in_a_panel():
    """The source is editable by hand again, in the same side panel the other
    modes use."""
    page = _page()
    panel = page[page.index('id="ff-yaml"') :]
    panel = panel[: panel.index("</aside>")]
    # The panel starts closed; the textarea inside it must not be hidden too,
    # or opening the panel would show an empty box.
    assert panel.split(">")[0].endswith("hidden"), "panel starts closed"
    textarea = panel[panel.index("<textarea") :]
    assert "hidden" not in textarea[: textarea.index(">")]
    assert "if (mode === 'code') codeEl.focus();" in page


def test_on_canvas_editing_is_gated_on_edit_mode():
    """The server always renders the editor chrome, so the mode class is what
    decides whether it shows — that is why switching modes needs no re-render.
    Gate it wrongly and the handles rewrite YAML the viewer cannot see."""
    page = _page()
    for affordance in ("fireflyer-resize-handle", "fireflyer-chart-tools",
                       "fireflyer-add-row", "fireflyer-add-cell"):
        assert f".layout:not(.mode-edit) .{affordance}" in page, affordance
    assert "const editingDisabled = () => currentMode !== 'edit';" in page


def test_run_syncs_the_topbar_after_a_programmatic_yaml_change():
    """Setting `codeEl.value` fires no `input` event, and the textarea is hidden
    so a user can never fire one either. Every rewrite — chart modal, calcs
    manager, chat — goes through run(), so that is where Save state and the
    editable title have to catch up. Without it, renaming via chat leaves the
    old name in the topbar."""
    page = _page()
    finally_block = page[page.index("function run(") :]
    finally_block = finally_block[finally_block.index("finally") :][:400]
    assert "updateSaveState();" in finally_block
    assert "syncNameFromYaml();" in finally_block


def test_every_panel_sits_beside_the_dashboard():
    """A panel is something you use *while* looking at the dashboard — writing
    YAML, asking the assistant, reading a chart's options. None of them overlay
    it. The split is keyed off one `panel-open` class rather than each mode, so
    adding a mode cannot leave a stale column behind."""
    page = _page()

    start = page.index(".layout.panel-open {")
    rule = page[start : page.index("}", start)]
    assert "display: grid" in rule and "grid-template-columns" in rule

    start = page.index(".layout.panel-open .ff-panel:not([hidden]) {")
    panel = page[start : page.index("}", start)]
    assert "grid-area: 1 / 1" in panel, "pin the row too, not just the column"

    # Column 2 is the drag handle, so the dashboard is column 3.
    start = page.index(".layout.panel-open #output-pane {")
    assert "grid-area: 1 / 3" in page[start : page.index("}", start)]

    # No per-mode split rules left to fall out of step with the switch.
    for mode in ("code", "chat", "docs", "calcs"):
        assert f".layout.mode-{mode} #" not in page, mode


def test_leaving_a_panel_mode_clears_its_class():
    """`code` was added to the switch and missed in the hand-written removal
    list, so returning to view left `.mode-code` on the layout: the grid stayed,
    the dashboard stayed in column 2, and column 1 sat empty. Deriving the list
    from MODES makes that impossible."""
    page = _page()
    assert "for (const name of Object.keys(MODES)) layoutEl.classList.remove('mode-' + name);" in page
    assert "layoutEl.classList.toggle('panel-open', MODES[mode] !== null);" in page


def test_panels_have_no_close_button():
    """The switch is the only way in or out — a ✕ that does the same thing as
    clicking the lit segment is a second control for one job."""
    page = _page()
    assert "ff-docs-close" not in page
    assert "✕</button>" not in page.split('id="ff-move-cancel"')[0]


def test_the_panels_are_siblings_of_the_dashboard_pane():
    """`grid-area` only places an element in its *parent's* grid. The panels
    began life inside `#output-pane`, so the split rules did nothing at all: the
    left column stayed empty and the YAML rendered below the dashboard. Reading
    the CSS could not catch that — only the DOM shape can."""
    from html.parser import HTMLParser

    class Tree(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.parent_of = [], {}

        def handle_starttag(self, tag, attrs):
            found = dict(attrs)
            if found.get("id"):
                self.parent_of[found["id"]] = next(
                    (i for i in reversed(self.stack) if i), None
                )
            if tag not in ("input", "br", "img", "meta", "link", "path", "circle"):
                self.stack.append(found.get("id"))

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()

    tree = Tree()
    tree.feed(render_editor_page(DEFAULT_YAML, theme=_theme_switch()))
    for element in ("output-pane", "ff-split", "ff-yaml", "ff-chat", "ff-docs",
                    "ff-calcs"):
        assert tree.parent_of.get(element) == "layout", (
            f"{element} must be a direct child of .layout to be placed in its grid"
        )


def test_the_panel_is_resizable_and_the_width_is_remembered():
    """How much room you want for YAML or the assistant is a preference, not a
    per-visit decision — so the drag persists. It belongs in localStorage (per
    browser) rather than the dashboard, which is shared with everyone."""
    page = _page()

    assert 'id="ff-split"' in page
    start = page.index(".layout.panel-open .ff-split {")
    assert "cursor: col-resize" in page[start : page.index("}", start)]
    # The dragged width drives the grid track.
    assert "grid-template-columns: var(--panel-w, 38vw) 5px 1fr;" in page

    assert "localStorage.setItem(PANEL_WIDTH_KEY" in page
    assert "localStorage.getItem(PANEL_WIDTH_KEY)" in page
    # A blocked or absent store must not break the editor.
    assert page.count("catch (err) {}") >= 2

    # Neither pane can be dragged away entirely.
    start = page.index("function setPanelWidth(")
    body = page[start : page.index("\n}", start)]
    assert "Math.max(240" in body and "window.innerWidth - 320" in body


def test_the_editor_script_declares_the_state_it_uses():
    """Removing the Preview toggle took an adjacent block with it — the resize
    constants, `gcd`/`reduceRatio` and `let resize` — leaving handlers that
    referenced names nothing declared. Chart resize threw on every mousemove and
    shipped that way, because Python tests never execute this script.

    A crude check, but it catches exactly that: every module-level name the
    handlers lean on must still be declared somewhere in the page.
    """
    import re

    script = EDITOR_JS

    declared = set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", script))
    declared |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", script))

    # State and helpers that handlers reference; each was a real orphan risk.
    needed = {
        "resize", "HEIGHT_UNIT_PX", "COL_STEP", "gcd", "reduceRatio",
        "currentMode", "MODES", "setMode", "editingDisabled",
        "layoutEl", "outEl", "codeEl", "splitEl", "modeSwitch",
        "loadCalcs", "calcsBody", "run", "setPanelWidth", "setRowUnits",
    }
    missing = sorted(n for n in needed if n not in declared)
    assert not missing, f"used but never declared: {missing}"
