import re

import pytest

import fireflyer as ff


def _smart_yaml(orders_parquet: str) -> str:
    return f"""
name: Test dashboard

charts:
  orders_table:
    type: table
    dataset: {orders_parquet}
    title: Orders
    pagination: 5

  status_pie:
    type: pie
    dataset: {orders_parquet}
    title: Orders by Status
    column: status

  orders_detail:
    type: table
    dataset: {orders_parquet}
    title: All Orders
    pagination: 10

layout:
  - Overview
  - ["@40", "orders_table:60", "status_pie:40"]
  - "-"
  - Detail
  - ["@30", "orders_detail:100"]
"""


def test_dashboard_smart_example(orders_parquet, snapshot):
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    snapshot(dashboard.to_html())


def test_dashboard_crossfilter_narrows_other_charts(orders_parquet):
    """Crossfilter on `status` filters the table; source pie keeps full data."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
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


def test_dashboard_crossfilter_yaml_round_trips(orders_parquet):
    """YAML source is embedded so htmx clicks can replay it via /dashboard."""
    yaml_text = _smart_yaml(orders_parquet)
    dashboard = ff.Dashboard.from_yaml(yaml_text)
    html = dashboard.to_html()
    assert '<input type="hidden" name="yaml_text"' in html
    # The full YAML survives in the hidden input (escaped, but still present).
    assert "status_pie" in html


def test_dashboard_filter_indicator_always_present(orders_parquet):
    """Every cell carries the filter indicator — even with zero filters."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    html = dashboard.to_html()
    # Three cells in _smart_yaml (orders_table × 2 + status_pie × 1).
    assert html.count('class="fireflyer-filter-indicator') == 3
    # All show count 0.
    assert html.count('<span class="count">0</span>') == 3
    # No cell is highlighted (no `.has-filters` modifier yet).
    assert 'indicator has-filters"' not in html


def test_dashboard_filter_indicator_highlights_filtered_cells(orders_parquet):
    """When filters narrow a cell, its indicator switches to the active state."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
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


def test_dashboard_emitter_chart_indicator_is_red(orders_parquet):
    """The chart that produced the crossfilter shows the red is-emitter state."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    html = dashboard.to_html(cf_tokens=["status_pie|status=paid"])
    # The pie cell uses the is-emitter modifier; downstream tables use has-filters.
    assert 'class="fireflyer-filter-indicator is-emitter"' in html
    assert html.count('class="fireflyer-filter-indicator has-filters"') == 2
    # Emitter tooltip uses the "Filtering others by" label.
    assert ">Filtering others by<" in html


def test_render_skeleton_emits_cell_placeholders(orders_parquet):
    """Skeleton has no chart HTML — only placeholders that hx-trigger=load."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
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


def test_render_skeleton_includes_cf_tokens(orders_parquet):
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    html = dashboard.render_skeleton(cf_tokens=["status_pie|status=paid"])
    assert '<input type="hidden" name="cf" value="status_pie|status=paid">' in html


def test_render_cell_returns_indicator_plus_chart(orders_parquet):
    """render_cell produces the same content the synchronous path does, but
    scoped to a single chart cell."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    html = dashboard.render_cell("status_pie", col="2", row="1")
    # Wrapping cell + indicator + chart all present.
    assert 'class="fireflyer-dashboard-cell"' in html
    # Grid placement round-tripped from the skeleton.
    assert "grid-column: 2" in html
    assert "grid-row: 1" in html
    assert 'class="fireflyer-filter-indicator' in html
    assert 'class="fireflyer-chart fireflyer-pie' in html


def test_render_cell_emitter_state_passes_through(orders_parquet):
    """When the requested cell is the active emitter, its indicator goes red."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    html = dashboard.render_cell(
        "status_pie", cf_tokens=["status_pie|status=paid"]
    )
    assert 'class="fireflyer-filter-indicator is-emitter"' in html
    assert ">Filtering others by<" in html


def test_render_cell_unknown_id_errors(orders_parquet):
    import pytest
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    with pytest.raises(ff.DashboardError, match="unknown chart"):
        dashboard.render_cell("ghost")


# --- Vertical merge rule ------------------------------------------------------


def _merge_yaml(orders_parquet: str, dashboard_block: str) -> str:
    return f"""
name: Test dashboard
charts:
  orders: {{type: table, dataset: {orders_parquet}, title: Orders}}
  by_day: {{type: bar, dataset: {orders_parquet}, title: ByDay, x: day, y: status}}
  status: {{type: pie, dataset: {orders_parquet}, title: Status, column: status}}
  new: {{type: table, dataset: {orders_parquet}, title: New}}
  kpi: {{type: number, dataset: {orders_parquet}, title: KPI}}
layout:
{dashboard_block}
"""


def test_dashboard_merges_chart_across_consecutive_rows(orders_parquet):
    """A chart sized in one row and repeated **bare** below spans the rows: it
    collapses into one placement with a row-spanning CSS grid-row value, and its
    neighbour fills (and column-spans) the leftover width."""
    yaml = _merge_yaml(orders_parquet, """
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


def test_dashboard_leftover_fill_column_span(orders_parquet):
    """Whiteboard case 10: a lower row finer than the leftover splits it by its
    own proportions, so the union grid gains a boundary and the spanning chart's
    neighbour column-spans."""
    yaml = _merge_yaml(orders_parquet, """
  - ["@20", "orders", "status"]
  - ["@20", "by_day", "new", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    # orders occupies [0,1] over a union grid of {0, 0.5, 1}; by_day/new split its
    # half. So orders column-spans two fine columns, status spans two rows.
    assert "grid-template-columns: 0.5fr 0.5fr 1fr" in html
    assert "grid-column: 1 / span 2" in html   # orders over the two left columns
    assert "grid-row: 1 / span 2" in html       # status spans both rows


def test_dashboard_rejects_non_consecutive_duplicate(orders_parquet):
    """A chart split by a separator (span can't jump it) resolves to two
    placements — rejected."""
    yaml = _merge_yaml(orders_parquet, """
  - ["@40", "orders:60", "status:40"]
  - "-"
  - ["@40", "by_day:60", "status"]
""")
    import pytest
    with pytest.raises(ff.DashboardError, match="more than once"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_width_repeat(orders_parquet):
    """The old merge form — repeating a chart WITH a width below — is no longer a
    span (only a bare repeat inherits), so it leaves two placements and errors."""
    yaml = _merge_yaml(orders_parquet, """
  - ["@40", "orders:60", "status:40"]
  - ["@30", "by_day:60", "status:40"]
""")
    import pytest
    with pytest.raises(ff.DashboardError, match="more than once"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_same_chart_twice_in_row(orders_parquet):
    yaml = _merge_yaml(orders_parquet, """
  - ["@40", "status", "status"]
""")
    import pytest
    with pytest.raises(ff.DashboardError, match="twice in the same row"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_single_row_unchanged_placement(orders_parquet):
    """Non-merged cells still get explicit grid-column/grid-row placement."""
    yaml = _merge_yaml(orders_parquet, """
  - ["@40", "orders:60", "status:40"]
""")
    dashboard = ff.Dashboard.from_yaml(yaml)
    html = dashboard.to_html()
    # Two cells, both at row 1 with grid-column 1 and 2 respectively.
    assert "grid-column: 1; grid-row: 1" in html
    assert "grid-column: 2; grid-row: 1" in html
    assert "grid-template-rows: 320px" in html


def test_dashboard_indicator_skips_missing_columns(orders_parquet):
    """A declared filter on a column the dataset lacks doesn't count."""
    yaml = f"""
name: Test dashboard
charts:
  t:
    type: table
    dataset: {orders_parquet}
    title: T
    filters:
      - column: nonexistent
        op: in
        values: [x]
layout:
  - ["@20", "t:100"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml)
    html = dashboard.to_html()
    # Indicator is present but count is 0 — the bogus column was dropped.
    assert 'class="fireflyer-filter-indicator' in html
    assert 'indicator has-filters"' not in html
    assert '<span class="count">0</span>' in html


def test_dashboard_widths_are_proportions(orders_parquet):
    """Widths are proportions (fr tracks), so any positive values are valid and
    equal integers split the row evenly — no sum-to-100 requirement."""
    yaml = f"""
name: Test dashboard
charts:
  a: {{type: table, dataset: {orders_parquet}, title: A}}
  b: {{type: table, dataset: {orders_parquet}, title: B}}
  c: {{type: table, dataset: {orders_parquet}, title: C}}
layout:
  - ["@20", "a:1", "b:1", "c:1"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert "grid-template-columns: 1fr 1fr 1fr" in html


def test_dashboard_proportional_widths_equivalent(orders_parquet):
    """`1 4` and `20 80` describe the same split; both are accepted and render
    as their literal fr weights."""
    def cols(a, b):
        yaml = f"""
name: Test dashboard
charts:
  a: {{type: table, dataset: {orders_parquet}, title: A}}
  b: {{type: table, dataset: {orders_parquet}, title: B}}
layout:
  - ["@20", "a:{a}", "b:{b}"]
"""
        html = ff.Dashboard.from_yaml(yaml).to_html()
        import re
        return re.search(r"grid-template-columns: ([^;]+);", html).group(1)

    assert cols(1, 4) == "1fr 4fr"
    assert cols(20, 80) == "20fr 80fr"  # same 20/80 split, just a different scale


def test_dashboard_single_cell_fills_row(orders_parquet):
    """A lone cell fills the row regardless of its number — proportions, not %."""
    yaml = f"""
name: Test dashboard
charts:
  t: {{type: table, dataset: {orders_parquet}, title: T}}
layout:
  - ["@20", "t:60"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert "grid-template-columns: 60fr" in html


def test_dashboard_optional_width_defaults_to_one(orders_parquet):
    """A bare id is `id:1`, so three bare cells split the row into equal thirds."""
    yaml = _merge_yaml(orders_parquet, """
  - ["@20", "orders", "by_day", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert "grid-template-columns: 1fr 1fr 1fr" in html


def test_dashboard_bare_inherit_spans(orders_parquet):
    """Whiteboard case 2: bare cells everywhere still span — `status` repeated
    bare below inherits its column, `by_day` fills the two left columns."""
    yaml = _merge_yaml(orders_parquet, """
  - ["@20", "orders", "new", "status"]
  - ["@20", "by_day", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert html.count("fireflyer-chart fireflyer-pie") == 1
    assert "grid-row: 1 / span 2" in html
    assert "grid-column: 1 / span 2" in html   # by_day over both left columns


def test_dashboard_rejects_unknown_chart(orders_parquet):
    yaml = f"""
name: Test dashboard
charts:
  t: {{type: table, dataset: {orders_parquet}, title: T}}
layout:
  - ["@20", "nope:100"]
"""
    with pytest.raises(ff.DashboardError, match="unknown chart 'nope'"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_single_row_insert_keeps_span(orders_parquet):
    """Whiteboard case 11: a chart added to just the first row of a merge keeps
    the spanning chart aligned — the lower row's `by_day` fills and column-spans
    the leftover, no spacer needed."""
    yaml = _merge_yaml(orders_parquet, """
  - ["@20", "orders", "new", "status"]
  - ["@20", "by_day", "status"]
""")
    html = ff.Dashboard.from_yaml(yaml).to_html()
    # status spans both rows once; by_day spans the two left columns in row 2.
    assert html.count("grid-row: 1 / span 2") == 1
    assert "grid-column: 1 / span 2" in html
    assert "grid-template-columns: 1fr 1fr 1fr" in html


def test_dashboard_rejects_missing_dataset():
    # `datasets:` block is gone; a chart just needs a `dataset` *name*.
    yaml = """
name: Test dashboard
charts:
  t: {type: table, title: T}
layout: []
"""
    with pytest.raises(ff.DashboardError, match="missing `dataset`"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_unknown_chart_type(orders_parquet):
    yaml = f"""
name: Test dashboard
charts:
  t: {{type: histogram, dataset: {orders_parquet}, title: T}}
layout: []
"""
    with pytest.raises(ff.DashboardError, match="unknown type 'histogram'"):
        ff.Dashboard.from_yaml(yaml)


def test_dashboard_rejects_missing_top_level(orders_parquet):
    yaml = f"""
name: Test dashboard
charts:
  t: {{type: table, dataset: {orders_parquet}, title: T}}
"""
    with pytest.raises(ff.DashboardError, match="missing top-level key: 'layout'"):
        ff.Dashboard.from_yaml(yaml)


# --- dataset references (delete-guard / cascade-rename helpers) ----------------


def test_dataset_names_and_rename_ref():
    from fireflyer.dashboard import rename_dataset_ref

    yaml = """name: D
charts:
  a: {type: table, dataset: orders, title: A}
  b: {type: pie, dataset: sales, title: B, column: x}
layout:
  - ["@20", "a", "b"]
"""
    assert ff.Dashboard.dataset_names(yaml) == {"orders", "sales"}

    out = rename_dataset_ref(yaml, "orders", "orders_2026")
    assert "dataset: orders_2026" in out
    assert "dataset: sales" in out            # unrelated ref untouched
    assert ff.Dashboard.dataset_names(out) == {"orders_2026", "sales"}


def test_rename_ref_respects_word_boundary():
    from fireflyer.dashboard import rename_dataset_ref

    yaml = """name: D
charts:
  a: {type: table, dataset: orders, title: A}
  b: {type: table, dataset: orders_archive, title: B}
layout:
  - ["@20", "a", "b"]
"""
    out = rename_dataset_ref(yaml, "orders", "sales")
    assert "dataset: sales" in out
    assert "dataset: orders_archive" in out   # not renamed to sales_archive


def test_set_grain_token_replaces_per_chart():
    """One pick per chart: setting again replaces, and an empty grain clears it
    back to automatic without disturbing other charts."""
    from fireflyer.dashboard import set_grain_token, view_for

    tokens = set_grain_token([], "by_day|month")
    assert tokens == ["by_day|month"]
    tokens = set_grain_token(tokens, "by_day|year")
    assert tokens == ["by_day|year"]
    tokens = set_grain_token(tokens, "other|day")
    assert sorted(tokens) == ["by_day|year", "other|day"]

    tokens = set_grain_token(tokens, "by_day|")     # back to auto
    assert tokens == ["other|day"]
    assert view_for(tokens, "other") == ("day", None)
    assert view_for(tokens, "by_day") == ("", None)
    assert view_for(tokens, "absent") == ("", None)

    # The same token carries the window offset alongside the grain, and either
    # part can be set without the other.
    tokens = set_grain_token(tokens, "by_day|month|120")
    assert view_for(tokens, "by_day") == ("month", 120)
    tokens = set_grain_token(tokens, "by_day||60")      # move window, grain auto
    assert view_for(tokens, "by_day") == ("", 60)


def test_grain_state_rides_back_out_of_band():
    """A cell-scoped re-render doesn't touch the page-level hidden inputs, so the
    grain state it just changed is written back as an out-of-band swap —
    otherwise the next request would carry the old token and undo the change."""
    from fireflyer.dashboard import GRAIN_STATE_ID, grain_state_html

    html = grain_state_html(["by_day|month", "other|day|60"])
    assert f'id="{GRAIN_STATE_ID}"' in html
    assert 'hx-swap-oob="true"' in html
    assert html.count('name="grain"') == 2
    assert 'value="by_day|month"' in html

    # Quoting is escaped, not concatenated raw.
    assert "&quot;" in grain_state_html(['a|b"c'])


def test_between_filter_renders_readably_in_the_indicator(orders_parquet):
    """A `between` row is not `in`/`not in` — labelling it "not in" was actively
    wrong, and one nowrap line truncated away the end of the range."""
    yaml = f"""
name: Between
charts:
  t: {{type: table, dataset: {orders_parquet}, title: T,
      filters: [{{column: day, op: between, values: ['2026-06-01', '2026-06-03']}}]}}
layout:
  - ["@20", "t"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    row = re.search(r'<div class="fireflyer-filter-row">(.*?)</div>\s*</div>',
                    html, re.S).group(1)
    text = " ".join(re.sub(r"<[^>]+>", " ", row).split())
    assert text == "day between 2026-06-01\u20132026-06-03"
    assert "not in" not in text


def test_column_calc_filter_counts_as_applied(csv_to_parquet):
    """The indicator resolves columns against the scan *with* calcs attached: a
    filter naming a column calc really does narrow the chart, so reporting it as
    unapplied made the indicator contradict the data."""
    import fireflyer as ff_mod

    csv = "day,status\n2026-01-01,paid\n2026-02-01,paid\n"
    parquet = csv_to_parquet(csv, "calc_filter")
    yaml = f"""
name: Calc filter
calcs:
  {parquet}:
    order_day: {{formula: 'str2dt(day, YYYY-MM-DD)'}}
charts:
  t: {{type: table, dataset: {parquet}, title: T,
      filters: [{{column: order_day, op: between,
                 values: ['2026-01-01', '2026-02-01']}}]}}
layout:
  - ["@20", "t"]
"""
    html = ff_mod.Dashboard.from_yaml(yaml).to_html()
    assert '<span class="count">1</span>' in html      # not 0
    assert "order_day" in html


def test_indicator_counts_match_across_emitters_and_recipients(orders_parquet):
    """Every badge shows the same number for the same dashboard state, red or
    blue. A chart is exempt from its own crossfilter, so an emitter's applied
    list is short by exactly what it emits — counting one *or* the other made
    the red badges read lower than their blue neighbours."""
    yaml = f"""
name: Counts
charts:
  pie1: {{type: pie, dataset: {orders_parquet}, title: A, column: status}}
  pie2: {{type: pie, dataset: {orders_parquet}, title: B, column: day}}
  tbl:  {{type: table, dataset: {orders_parquet}, title: C}}
layout:
  - ["@30", "pie1", "pie2", "tbl"]
"""
    dash = ff.Dashboard.from_yaml(yaml)

    def counts(tokens):
        html = dash.to_html(cf_tokens=tokens)
        return re.findall(r'<span class="count">(\d+)</span>', html)

    assert counts([]) == ["0", "0", "0"]
    assert counts(["pie1|status=paid"]) == ["1", "1", "1"]          # one is red
    assert counts(["pie1|status=paid", "pie2|day=2026-06-01"]) == ["2", "2", "2"]


def test_emitter_tooltip_lists_both_its_own_and_incoming_filters(orders_parquet):
    """An emitter that's also downstream of another chart shows both sections,
    so the list adds up to the number on the badge."""
    yaml = f"""
name: Both
charts:
  pie1: {{type: pie, dataset: {orders_parquet}, title: A, column: status}}
  pie2: {{type: pie, dataset: {orders_parquet}, title: B, column: day}}
layout:
  - ["@30", "pie1", "pie2"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html(
        cf_tokens=["pie1|status=paid", "pie2|day=2026-06-01"]
    )
    # pie1's tooltip: emits status, receives day.
    tip = re.search(
        r'<div class="fireflyer-filter-tooltip" role="tooltip">(.*?)</div>\s*</div>',
        html, re.S,
    ).group(1)
    assert "Filtering others by" in tip
    assert "Active filters" in tip
    assert tip.count('class="fireflyer-filter-row"') == 2


def test_open_tooltip_is_lifted_above_other_badges(orders_parquet):
    """A tooltip lives inside its badge's stacking context, so it could only be
    ordered against that badge's siblings — another cell's badge at the same
    z-index drew over it. The badge is raised while the tooltip is open."""
    html = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet)).to_html()
    rule = re.search(
        r"\.fireflyer-filter-indicator:hover,\s*"
        r"\.fireflyer-filter-indicator:focus-within \{([^}]*)\}",
        html,
    )
    assert rule and "z-index" in rule.group(1)
    lifted = int(re.search(r"z-index:\s*(\d+)", rule.group(1)).group(1))
    base = int(re.search(
        r"\.fireflyer-filter-indicator \{[^}]*z-index:\s*(\d+)", html, re.S
    ).group(1))
    assert lifted > base
