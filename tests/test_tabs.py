"""Tabs: a `dashboard:` mapping (tab name -> layout list). Covers parsing,
rendering (active tab only, tab bar, global numbering), and the surgical
config_edit tab gestures."""

import pytest

import fireflyer as ff
from fireflyer import config_edit as ce


def _tabbed(csv_path: str) -> str:
    return f"""name: Test dashboard
datasets:
  o: {{path: {csv_path}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: pie, dataset: o, title: B, column: status}}
  c: {{type: table, dataset: o, title: C}}
dashboard:
  Overview:
    - ["@22", "a", "b"]
    - "-"
  All orders:
    - ["@50", "c"]
"""


# --- parsing -----------------------------------------------------------------


def test_tabs_parse_names_and_shape(orders_csv):
    d = ff.Dashboard.from_yaml(_tabbed(orders_csv))
    assert [t.name for t in d.tabs] == ["Overview", "All orders"]


def test_flat_dashboard_has_no_tabs(orders_csv):
    yaml = f"""name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
dashboard:
  - ["@20", "a"]
"""
    assert ff.Dashboard.from_yaml(yaml).tabs is None


def test_empty_tab_rejected(orders_csv):
    yaml = f"""name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
dashboard:
  Empty:
  Full:
    - ["@20", "a"]
"""
    with pytest.raises(ff.DashboardError, match="at least one layout item"):
        ff.Dashboard.from_yaml(yaml)


def test_chart_in_two_tabs_rejected(orders_csv):
    """A chart resolves to one placement across the whole dashboard."""
    yaml = f"""name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
dashboard:
  One:
    - ["@20", "a"]
  Two:
    - ["@20", "a"]
"""
    with pytest.raises(ff.DashboardError, match="more than once"):
        ff.Dashboard.from_yaml(yaml)


def test_span_within_a_lower_tab(orders_csv):
    """A bare-inherit span still works inside a tab that isn't the first — proof
    that per-tab grouping and global ordinal numbering are correct."""
    yaml = f"""name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: pie, dataset: o, title: B, column: status}}
  c: {{type: bar, dataset: o, title: C, x: day, y: status}}
dashboard:
  First:
    - ["@20", "a"]
  Second:
    - ["@40", "c:60", "b:40"]
    - ["@30", "c", "b"]
"""
    d = ff.Dashboard.from_yaml(yaml)
    html = d.to_html(active_tab=1)
    # b (and c) span the two rows of the Second tab.
    assert "grid-row: 1 / span 2" in html


# --- rendering ---------------------------------------------------------------


def test_to_html_renders_tab_bar_and_active_tab_only(orders_csv):
    d = ff.Dashboard.from_yaml(_tabbed(orders_csv))
    html = d.to_html()
    assert '<div class="fireflyer-tabs"' in html
    assert html.count('class="fireflyer-tab') >= 2      # two tab buttons
    # Active tab 0 shows a + b, not c.
    assert "fireflyer-pie" in html                        # b is in tab 0
    assert html.count("fireflyer-chart fireflyer-table") == 1   # only a, not c


def test_to_html_active_tab_switches_content(orders_csv):
    d = ff.Dashboard.from_yaml(_tabbed(orders_csv))
    html = d.to_html(active_tab=1)
    # Tab 1 has c (a table) and no pie.
    assert "fireflyer-pie" not in html
    assert 'name="active_tab" value="1"' in html


def test_skeleton_tab_bar_and_lazy_cells(orders_csv):
    d = ff.Dashboard.from_yaml(_tabbed(orders_csv))
    html = d.render_skeleton(active_tab=0)
    assert '<div class="fireflyer-tabs"' in html
    # Only the active tab's cells become placeholders (a + b = 2), not c.
    assert html.count('hx-post="/dashboard/cell"') == 2
    assert '<input type="hidden" name="active_tab" value="0">' in html


def test_skeleton_editing_shows_tab_toolbar_not_add_first(orders_csv):
    d = ff.Dashboard.from_yaml(_tabbed(orders_csv))
    html = d.render_skeleton(editing=True)
    assert "fireflyer-tab-switch" in html
    assert 'data-tab-index="0"' in html
    # The top "+" (add-first-tab) button only shows when NOT tabbed.
    assert 'class="fireflyer-add-tab-first-btn"' not in html


def test_skeleton_flat_editing_has_no_tab_bar(orders_csv):
    """A flat dashboard renders no tab bar; tabs are created from the between-rows
    "+" menu (which lives in the editor page, not the skeleton)."""
    yaml = f"""name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
dashboard:
  - ["@20", "a"]
"""
    html = ff.Dashboard.from_yaml(yaml).render_skeleton(editing=True)
    assert '<div class="fireflyer-tabs"' not in html


def test_tabbed_dashboard_snapshot(orders_csv, snapshot):
    d = ff.Dashboard.from_yaml(_tabbed(orders_csv))
    snapshot(d.to_html())


# --- config_edit: tab gestures ----------------------------------------------


def _flat(csv_path: str) -> str:
    return f"""name: Test dashboard
datasets:
  o: {{path: {csv_path}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: pie, dataset: o, title: B, column: status}}
dashboard:
  - ["@22", "a"]
  - ["@30", "b"]
"""


def test_add_first_tab_wraps_flat(orders_csv):
    out = ce.add_first_tab(_flat(orders_csv))
    d = ff.Dashboard.from_yaml(out)
    assert [t.name for t in d.tabs] == ["New tab"]
    assert "New tab:" in out


def test_add_first_tab_rejects_already_tabbed(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="already has tabs"):
        ce.add_first_tab(_tabbed(orders_csv))


def test_insert_tab_splits(orders_csv):
    # Split before item index 1 (the separator) -> All orders gets sep + c row.
    out = ce.insert_tab(_tabbed(orders_csv), 1)
    assert ce.tab_names(out) == ["Overview", "New tab", "All orders"]


def test_set_tab_text_renames(orders_csv):
    out = ce.set_tab_text(_tabbed(orders_csv), 1, "Everything")
    assert ce.tab_names(out) == ["Overview", "Everything"]


def test_set_tab_text_empty_rejected(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="cannot be empty"):
        ce.set_tab_text(_tabbed(orders_csv), 0, "   ")


def test_move_tab_repositions_boundary(orders_csv):
    """Move repositions the tab's start boundary (delimiter model): moving a
    tab's key line earlier hands it the rows it now sits above."""
    yaml = f"""name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  x: {{type: table, dataset: o, title: X}}
  y: {{type: table, dataset: o, title: Y}}
  z: {{type: table, dataset: o, title: Z}}
dashboard:
  A:
    - ["@20", "x"]
    - ["@20", "y"]
  B:
    - ["@20", "z"]
"""
    # Move B (index 1) to before item 1 (the y row): A keeps x, B gains y + z.
    out = ce.move_tab(yaml, 1, 1)
    d = ff.Dashboard.from_yaml(out)
    assert [t.name for t in d.tabs] == ["A", "B"]

    def chart_ids(tab):
        return [p.chart_id for g in tab.items if hasattr(g, "placements") for p in g.placements]

    assert chart_ids(d.tabs[0]) == ["x"]
    assert sorted(chart_ids(d.tabs[1])) == ["y", "z"]


def test_move_tab_onto_first_row_rejected(orders_csv):
    """Moving a tab's boundary above the first tab's only content would empty
    that tab — rejected, not silently applied."""
    with pytest.raises(ff.DashboardError):
        ce.move_tab(_tabbed(orders_csv), 1, 0)


def test_first_tab_cannot_be_moved(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="first tab can't be moved"):
        ce.move_tab(_tabbed(orders_csv), 0, 2)


def test_delete_first_tab_dissolves_all(orders_csv):
    out = ce.delete_tab(_tabbed(orders_csv), 0)
    assert ce.tab_names(out) == []                     # back to flat
    assert ff.Dashboard.from_yaml(out).tabs is None


def test_delete_non_first_tab_merges_into_previous(orders_csv):
    out = ce.delete_tab(_tabbed(orders_csv), 1)
    d = ff.Dashboard.from_yaml(out)
    assert [t.name for t in d.tabs] == ["Overview"]
    # c merged into Overview.
    assert any(
        "c" in [p.chart_id for p in g.placements]
        for g in d.tabs[0].items if hasattr(g, "placements")
    )


def test_cross_tab_move_dissolves_emptied_tab(orders_csv):
    """Moving the only chart out of a tab dissolves that (now empty) tab."""
    out = ce.move_placement(_tabbed(orders_csv), "c", "a", "before")
    assert ce.tab_names(out) == ["Overview"]           # All orders is gone


def test_delete_chart_dissolves_emptied_tab(orders_csv):
    out = ce.delete_chart(_tabbed(orders_csv), "c")
    assert ce.tab_names(out) == ["Overview"]
