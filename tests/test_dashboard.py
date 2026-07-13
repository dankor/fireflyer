import pytest

import fireflyer as ff


def _smart_yaml(csv_path: str) -> str:
    return f"""
name: Test dashboard
datasets:
  orders:
    path: {csv_path}

charts:
  orders_table:
    type: table
    dataset: orders
    title: Orders
    pagination: 5

  status_pie:
    type: pie
    dataset: orders
    title: Orders by Status
    column: status

  orders_detail:
    type: table
    dataset: orders
    title: All Orders
    pagination: 10

dashboard:
  - Overview
  - ["@40", "orders_table:60", "status_pie:40"]
  - "-"
  - Detail
  - ["@30", "orders_detail:100"]
"""


def test_dashboard_smart_example(orders_csv, snapshot):
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    snapshot(dashboard.to_html())


def test_dashboard_crossfilter_narrows_other_charts(orders_csv):
    """Crossfilter on `status` filters the table; source pie keeps full data."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    # Click "paid" on status_pie — token is emitter-prefixed.
    html = dashboard.to_html(cf_tokens=["status_pie|status=paid"])

    # Non-paid amounts from orders.csv must NOT appear in any <td> — proves
    # the table was filtered. Numeric cells render as `>N<` (15, 30, 12 are
    # the only non-paid amounts in the seed CSV).
    assert ">15<" not in html
    assert ">30<" not in html
    assert ">12<" not in html
    # A paid amount must still appear.
    assert ">42<" in html

    # Source pie is exempt — still shows all 3 slices, with "paid" highlighted.
    assert html.count('data-active="1"') == 1
    assert 'data-i="0"' in html and 'data-i="1"' in html and 'data-i="2"' in html

    # Hidden cf token round-trips with its emitter prefix.
    assert 'name="cf" value="status_pie|status=paid"' in html


def test_dashboard_crossfilter_yaml_round_trips(orders_csv):
    """YAML source is embedded so htmx clicks can replay it via /dashboard."""
    yaml_text = _smart_yaml(orders_csv)
    dashboard = ff.Dashboard.from_yaml(yaml_text)
    html = dashboard.to_html()
    assert '<input type="hidden" name="yaml_text"' in html
    # The full YAML survives in the hidden input (escaped, but still present).
    assert "status_pie" in html


def test_dashboard_filter_indicator_always_present(orders_csv):
    """Every cell carries the filter indicator — even with zero filters."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    html = dashboard.to_html()
    # Three cells in _smart_yaml (orders_table × 2 + status_pie × 1).
    assert html.count('class="fireflyer-filter-indicator') == 3
    # All show count 0.
    assert html.count('<span class="count">0</span>') == 3
    # No cell is highlighted (no `.has-filters` modifier yet).
    assert 'indicator has-filters"' not in html


def test_dashboard_filter_indicator_highlights_filtered_cells(orders_csv):
    """When filters narrow a cell, its indicator switches to the active state."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    html = dashboard.to_html(cf_tokens=["status_pie|status=paid"])

    # 3 cells total: 2 downstream (blue), 1 emitter (red). All show count 1.
    assert html.count('class="fireflyer-filter-indicator') == 3
    assert html.count('class="fireflyer-filter-indicator has-filters"') == 2
    assert html.count('class="fireflyer-filter-indicator is-emitter"') == 1
    assert html.count('<span class="count">1</span>') == 3
    assert html.count('<span class="count">0</span>') == 0
    # Tooltip surfaces the filter detail.
    assert '<span class="col">status</span>' in html
    assert '<span class="vals">paid</span>' in html


def test_dashboard_emitter_chart_indicator_is_red(orders_csv):
    """The chart that produced the crossfilter shows the red is-emitter state."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    html = dashboard.to_html(cf_tokens=["status_pie|status=paid"])
    # The pie cell uses the is-emitter modifier; downstream tables use has-filters.
    assert 'class="fireflyer-filter-indicator is-emitter"' in html
    assert html.count('class="fireflyer-filter-indicator has-filters"') == 2
    # Emitter tooltip uses the "Filtering others by" label.
    assert ">Filtering others by<" in html


def test_render_skeleton_emits_cell_placeholders(orders_csv):
    """Skeleton has no chart HTML — only placeholders that hx-trigger=load."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    html = dashboard.render_skeleton()
    # Three cells in the smart example → three placeholders.
    assert html.count('class="fireflyer-dashboard-cell fireflyer-cell-loading"') == 3
    assert html.count('hx-post="/dashboard/cell"') == 3
    assert html.count('hx-trigger="load"') == 3
    # YAML + cf state still embedded so cells include them on fetch.
    assert '<input type="hidden" name="yaml_text"' in html
    # No chart content yet (filter indicators come from cells, not the skeleton).
    # Match the class attribute, not the CSS selector — the CSS rules live in
    # the embedded stylesheet regardless.
    assert 'class="fireflyer-filter-indicator' not in html
    assert 'class="fireflyer-chart' not in html


def test_render_skeleton_includes_cf_tokens(orders_csv):
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    html = dashboard.render_skeleton(cf_tokens=["status_pie|status=paid"])
    assert '<input type="hidden" name="cf" value="status_pie|status=paid">' in html


def test_render_cell_returns_indicator_plus_chart(orders_csv):
    """render_cell produces the same content the synchronous path does, but
    scoped to a single chart cell."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    html = dashboard.render_cell("status_pie", col="2", row="1")
    # Wrapping cell + indicator + chart all present.
    assert 'class="fireflyer-dashboard-cell"' in html
    # Grid placement round-tripped from the skeleton.
    assert "grid-column: 2" in html
    assert "grid-row: 1" in html
    assert 'class="fireflyer-filter-indicator' in html
    assert 'class="fireflyer-chart fireflyer-pie' in html


def test_render_cell_emitter_state_passes_through(orders_csv):
    """When the requested cell is the active emitter, its indicator goes red."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    html = dashboard.render_cell(
        "status_pie", cf_tokens=["status_pie|status=paid"]
    )
    assert 'class="fireflyer-filter-indicator is-emitter"' in html
    assert ">Filtering others by<" in html


def test_render_cell_unknown_id_errors(orders_csv):
    import pytest
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_csv))
    with pytest.raises(ff.DashboardError, match="unknown chart"):
        dashboard.render_cell("ghost")


# --- Vertical merge rule ------------------------------------------------------


def _merge_yaml(csv_path: str, dashboard_block: str) -> str:
    return f"""
name: Test dashboard
datasets:
  o: {{path: {csv_path}}}
charts:
  orders: {{type: table, dataset: o, title: Orders}}
  by_day: {{type: bar, dataset: o, title: ByDay, x: day, y: status}}
  status: {{type: pie, dataset: o, title: Status, column: status}}
  new: {{type: table, dataset: o, title: New}}
  kpi: {{type: number, dataset: o, title: KPI, column: amount, agg: sum}}
dashboard:
{dashboard_block}
"""


def test_dashboard_merges_chart_across_consecutive_rows(orders_csv):
    """A chart sized in one row and repeated **bare** below spans the rows: it
    collapses into one placement with a row-spanning CSS grid-row value, and its
    neighbour fills (and column-spans) the leftover width."""
    yaml = _merge_yaml(orders_csv, """
  - ["@40", "orders:60", "status:40"]
  - ["@30", "by_day", "status"]
""")
    dashboard = ff.Dashboard.from_yaml(yaml)
    html = dashboard.to_html()
    # status renders once, with grid-row: 1 / span 2 — orders and by_day each
    # take one row in the left column.
    assert html.count("fireflyer-chart fireflyer-pie") == 1
    assert "grid-row: 1 / span 2" in html
    assert "grid-template-columns: 60fr 40fr" in html
    # Row group's grid-template-rows includes both row heights (40 → 320px,
    # 30 → 240px).
    assert "grid-template-rows: 320px 240px" in html


def test_dashboard_leftover_fill_column_span(orders_csv):
    """Whiteboard case 10: a lower row finer than the leftover splits it by its
    own proportions, so the union grid gains a boundary and the spanning chart's
    neighbour column-spans."""
    yaml = _merge_yaml(orders_csv, """
  - ["@20", "orders", "status"]
  - ["@20", "by_day", "new", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    # orders occupies [0,1] over a union grid of {0, 0.5, 1}; by_day/new split its
    # half. So orders column-spans two fine columns, status spans two rows.
    assert "grid-template-columns: 0.5fr 0.5fr 1fr" in html
    assert "grid-column: 1 / span 2" in html   # orders over the two left columns
    assert "grid-row: 1 / span 2" in html       # status spans both rows


def test_dashboard_rejects_non_consecutive_duplicate(orders_csv):
    """A chart split by a separator (span can't jump it) resolves to two
    placements — rejected."""
    yaml = _merge_yaml(orders_csv, """
  - ["@40", "orders:60", "status:40"]
  - "-"
  - ["@40", "by_day:60", "status"]
""")
    import pytest
    with pytest.raises(ff.DashboardError, match="more than once"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_width_repeat(orders_csv):
    """The old merge form — repeating a chart WITH a width below — is no longer a
    span (only a bare repeat inherits), so it leaves two placements and errors."""
    yaml = _merge_yaml(orders_csv, """
  - ["@40", "orders:60", "status:40"]
  - ["@30", "by_day:60", "status:40"]
""")
    import pytest
    with pytest.raises(ff.DashboardError, match="more than once"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_same_chart_twice_in_row(orders_csv):
    yaml = _merge_yaml(orders_csv, """
  - ["@40", "status", "status"]
""")
    import pytest
    with pytest.raises(ff.DashboardError, match="twice in the same row"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_single_row_unchanged_placement(orders_csv):
    """Non-merged cells still get explicit grid-column/grid-row placement."""
    yaml = _merge_yaml(orders_csv, """
  - ["@40", "orders:60", "status:40"]
""")
    dashboard = ff.Dashboard.from_yaml(yaml)
    html = dashboard.to_html()
    # Two cells, both at row 1 with grid-column 1 and 2 respectively.
    assert "grid-column: 1; grid-row: 1" in html
    assert "grid-column: 2; grid-row: 1" in html
    assert "grid-template-rows: 320px" in html


def test_dashboard_indicator_skips_missing_columns(orders_csv):
    """A declared filter on a column the dataset lacks doesn't count."""
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  t:
    type: table
    dataset: o
    title: T
    filters:
      - column: nonexistent
        op: in
        values: [x]
dashboard:
  - ["@20", "t:100"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml)
    html = dashboard.to_html()
    # Indicator is present but count is 0 — the bogus column was dropped.
    assert 'class="fireflyer-filter-indicator' in html
    assert 'indicator has-filters"' not in html
    assert '<span class="count">0</span>' in html


def test_dashboard_widths_are_proportions(orders_csv):
    """Widths are proportions (fr tracks), so any positive values are valid and
    equal integers split the row evenly — no sum-to-100 requirement."""
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: table, dataset: o, title: B}}
  c: {{type: table, dataset: o, title: C}}
dashboard:
  - ["@20", "a:1", "b:1", "c:1"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert "grid-template-columns: 1fr 1fr 1fr" in html


def test_dashboard_proportional_widths_equivalent(orders_csv):
    """`1 4` and `20 80` describe the same split; both are accepted and render
    as their literal fr weights."""
    def cols(a, b):
        yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: table, dataset: o, title: B}}
dashboard:
  - ["@20", "a:{a}", "b:{b}"]
"""
        html = ff.Dashboard.from_yaml(yaml).to_html()
        import re
        return re.search(r"grid-template-columns: ([^;]+);", html).group(1)

    assert cols(1, 4) == "1fr 4fr"
    assert cols(20, 80) == "20fr 80fr"  # same 20/80 split, just a different scale


def test_dashboard_single_cell_fills_row(orders_csv):
    """A lone cell fills the row regardless of its number — proportions, not %."""
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  t: {{type: table, dataset: o, title: T}}
dashboard:
  - ["@20", "t:60"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert "grid-template-columns: 60fr" in html


def test_dashboard_optional_width_defaults_to_one(orders_csv):
    """A bare id is `id:1`, so three bare cells split the row into equal thirds."""
    yaml = _merge_yaml(orders_csv, """
  - ["@20", "orders", "by_day", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert "grid-template-columns: 1fr 1fr 1fr" in html


def test_dashboard_bare_inherit_spans(orders_csv):
    """Whiteboard case 2: bare cells everywhere still span — `status` repeated
    bare below inherits its column, `by_day` fills the two left columns."""
    yaml = _merge_yaml(orders_csv, """
  - ["@20", "orders", "new", "status"]
  - ["@20", "by_day", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert html.count("fireflyer-chart fireflyer-pie") == 1
    assert "grid-row: 1 / span 2" in html
    assert "grid-column: 1 / span 2" in html   # by_day over both left columns


def test_dashboard_rejects_unknown_chart(orders_csv):
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  t: {{type: table, dataset: o, title: T}}
dashboard:
  - ["@20", "nope:100"]
"""
    with pytest.raises(ff.DashboardError, match="unknown chart 'nope'"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_single_row_insert_keeps_span(orders_csv):
    """Whiteboard case 11: a chart added to just the first row of a merge keeps
    the spanning chart aligned — the lower row's `by_day` fills and column-spans
    the leftover, no spacer needed."""
    yaml = _merge_yaml(orders_csv, """
  - ["@20", "orders", "new", "status"]
  - ["@20", "by_day", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    # status spans both rows once; by_day spans the two left columns in row 2.
    assert html.count("grid-row: 1 / span 2") == 1
    assert "grid-column: 1 / span 2" in html
    assert "grid-template-columns: 1fr 1fr 1fr" in html


def test_dashboard_rejects_unknown_dataset():
    yaml = """
name: Test dashboard
datasets: {}
charts:
  t: {type: table, dataset: missing, title: T}
dashboard: []
"""
    with pytest.raises(ff.DashboardError, match="unknown dataset 'missing'"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_unknown_chart_type(orders_csv):
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  t: {{type: histogram, dataset: o, title: T}}
dashboard: []
"""
    with pytest.raises(ff.DashboardError, match="unknown type 'histogram'"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_missing_top_level(orders_csv):
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  t: {{type: table, dataset: o, title: T}}
"""
    with pytest.raises(ff.DashboardError, match="missing top-level key: 'dashboard'"):
        ff.Dashboard.from_yaml(yaml)
