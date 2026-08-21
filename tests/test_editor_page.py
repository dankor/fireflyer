"""Static guards on the editor page (`INDEX`). Its interactive behavior is
JS/browser territory the snapshot suite can't reach, but a couple of regressions
are cheap to pin down from the rendered HTML/CSS — notably that the "stale"
preview overlay stays *interactive*: a `pointer-events: none` there once made
the greyed preview swallow clicks and broke the row/column resize handles."""

import re

from pathlib import Path

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
