"""Static guards on the editor page (`INDEX`). Its interactive behavior is
JS/browser territory the snapshot suite can't reach, but a couple of regressions
are cheap to pin down from the rendered HTML/CSS — notably that the "stale"
preview overlay stays *interactive*: a `pointer-events: none` there once made
the greyed preview swallow clicks and broke the row/column resize handles."""

import re

from fireflyer.web.app import DEFAULT_YAML, _theme_switch, render_editor_page


def _page() -> str:
    return render_editor_page(DEFAULT_YAML, theme=_theme_switch())


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
