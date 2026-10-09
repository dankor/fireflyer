import html as html_mod
import json
import re
from pathlib import Path

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


def _badge_counts(html):
    """The numbers on the filter badges. The badge is a `<summary>` whose text
    *is* the count — there is no icon and no separate `.count` span."""
    return re.findall(
        r'<summary aria-label="[^"]*">(\d+)</summary>', html
    )

def test_dashboard_filter_indicator_always_present(orders_parquet):
    """Every cell carries the filter indicator — even with zero filters."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    html = dashboard.to_html()
    # Three cells in _smart_yaml (orders_table × 2 + status_pie × 1).
    assert html.count('class="fireflyer-filter-indicator') == 3
    # All show count 0.
    assert _badge_counts(html).count("0") == 3
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
    assert _badge_counts(html).count("1") == 3
    assert _badge_counts(html).count("0") == 0
    # Tooltip surfaces the filter detail.
    from fireflyer.params import type_glyph

    assert f'<td class="col" title="status">{type_glyph("text")}status</td>' in html
    assert '<td class="vals" title="paid">paid</td>' in html


def test_dashboard_emitter_chart_indicator_is_red(orders_parquet):
    """The chart that produced the crossfilter shows the red is-emitter state."""
    dashboard = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet))
    html = dashboard.to_html(cf_tokens=["status_pie|status=paid"])
    # The pie cell uses the is-emitter modifier; downstream tables use has-filters.
    assert 'class="fireflyer-filter-indicator is-emitter"' in html
    assert html.count('class="fireflyer-filter-indicator has-filters"') == 2
    # The section labels are gone: each row names its source instead, and the
    # emitting chart's own row is marked as the emitter.
    assert '<tr class="src-emitter">' in html
    assert '<tr class="src-incoming">' in html


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
    assert '<tr class="src-emitter">' in html


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
    assert "0" in _badge_counts(html)


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
        # The row's inline style — not a grid rule in the dashboard's stylesheet.
        return re.search(
            r'class="fireflyer-dashboard-row" style="grid-template-columns: ([^;]+);', html
        ).group(1)

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
    row = re.search(r'<tr class="src-\w+">(.*?)</tr>',
                    html, re.S).group(1)
    # Past the source cell — the row now leads with where the filter came from.
    row = re.sub(r'<span class="ff-type-glyph"[^>]*>[^<]*</span>', "", row)     # type glyph
    text = " ".join(re.sub(r"<[^>]+>", " ", row.split("</td>", 1)[1]).split())
    # Half-open [06-01, 06-03) covers the 1st and 2nd — shown as the days it covers.
    assert text == "day between 2026-06-01\u20132026-06-02"
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
    assert "1" in _badge_counts(html)      # not 0
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
        return _badge_counts(html)

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
        r'<div class="fireflyer-filter-panel">(.*?)</div>\s*</details>',
        html, re.S,
    ).group(1)
    # Both directions appear, told apart by their source rather than by a
    # section heading: what this chart emits, and what reaches it from another.
    assert '<tr class="src-emitter">' in tip
    assert '<tr class="src-incoming">' in tip
    assert tip.count('<tr class="src-') == 2


def test_open_tooltip_is_lifted_above_other_badges(orders_parquet):
    """A tooltip lives inside its badge's stacking context, so it could only be
    ordered against that badge's siblings — another cell's badge at the same
    z-index drew over it. The badge is raised while the tooltip is open."""
    html = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet)).to_html()
    rule = re.search(r"\.fireflyer-filter-indicator\[open\] \{([^}]*)\}", html)
    assert rule and "z-index" in rule.group(1)
    lifted = int(re.search(r"z-index:\s*(\d+)", rule.group(1)).group(1))
    base = int(re.search(
        r"\.fireflyer-filter-indicator \{[^}]*z-index:\s*(\d+)", html, re.S
    ).group(1))
    assert lifted > base


# --- the filter badge -----------------------------------------------------


def test_the_badge_is_a_number_you_click(orders_parquet):
    """It was a filter icon with a hover card. A card you have to keep the
    pointer inside is hard to read and unusable on a touch screen, so it is a
    `<details>` now — click to open, stays open. Native, so no JS ships in
    `to_html()` output."""
    html = ff.Dashboard.from_yaml(_smart_yaml(orders_parquet)).to_html()

    assert "<details class=\"fireflyer-filter-indicator" in html
    assert "<summary" in html
    # The number is the badge: no icon, no separate label beside it.
    assert 'class="count"' not in html
    assert "M1 3h14l-5 6.5V14" not in html, "the filter icon should be gone"
    assert _badge_counts(html), "the count still renders"

    # Opening is a state, not a hover.
    assert ".fireflyer-filter-indicator[open]" in html
    assert ".fireflyer-filter-indicator:hover .fireflyer-filter" not in html


def test_the_badge_names_the_dataset_and_when_it_changed():
    """Which data a chart is built on, and how stale it might be, are the two
    questions the badge is opened to answer."""
    import tempfile

    from fireflyer.datasets import DatasetStore
    from fireflyer.storage import make_object_store

    store = DatasetStore(make_object_store({"base": tempfile.mkdtemp()}))
    store.create("orders", b"region,amount\nnorth,10\n", description="d")
    yaml = (
        "name: T\ncharts:\n  a: {type: table, dataset: orders, title: A}\n"
        'layout:\n  - ["@30", "a"]\n'
    )
    html = ff.Dashboard.from_yaml(yaml, datasets=store).to_html()
    found = re.search(
        r'ds-name">([^<]*)</span>\s*<span class="ds-updated">([^<]*)<', html
    )
    assert found.group(1) == "orders"
    assert found.group(2).startswith("updated 20")


def test_inline_data_is_labelled_rather_than_dated():
    """Inline CSV has no update time of its own — it changes when the dashboard
    does — so a date would be invented."""
    yaml = (
        "name: T\ncharts:\n  a: {type: table, dataset: sales, title: A}\n"
        'layout:\n  - ["@30", "a"]\n'
        "datasets:\n  sales: |\n    region,amount\n    north,10\n"
    )
    html = ff.Dashboard.from_yaml(yaml).to_html()
    found = re.search(
        r'ds-name">([^<]*)</span>\s*<span class="ds-updated">([^<]*)<', html
    )
    assert found.group(1) == "sales"
    assert found.group(2) == "inline data"


def test_a_dataset_with_no_metadata_costs_the_date_not_the_render(orders_parquet):
    """Standalone and in tests the resolver is a bare callable with nothing to
    ask about update times."""
    yaml = (
        f"name: T\ncharts:\n  a: {{type: table, dataset: {orders_parquet}, title: A}}\n"
        'layout:\n  - ["@30", "a"]\n'
    )
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert 'class="ds-updated">—<' in html


def test_the_panel_says_which_chart_set_each_filter():
    """A narrowed chart is no use if you can't tell which chart to click to undo
    it — the old two-section list never said. Each row now leads with its
    source: the emitting chart, by icon and title."""
    yaml = (
        "name: T\ncharts:\n"
        "  a: {type: pie, dataset: sales, title: By region, column: region}\n"
        "  b: {type: table, dataset: sales, title: Rows,"
        " filters: [{column: region, op: ni, values: [east]}]}\n"
        'layout:\n  - ["@30", "a", "b"]\n'
        "datasets:\n  sales: |\n    region,amount\n    north,10\n    south,5\n"
    )
    dashboard = ff.Dashboard.from_yaml(yaml)

    def rows(cid):
        html = dashboard.render_cell(cid, cf_tokens=["a|region=north"])
        out = []
        html = re.sub(r'<span class="ff-type-glyph"[^>]*>[^<]*</span>', "", html)
        for m in re.finditer(r'<tr class="src-(\w+)">(.*?)</tr>', html, re.S):
            cells = [
                " ".join(re.sub(r"<[^>]+>", " ", c).split())
                for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)
            ][:4]                  # source, column, op, values — not the remove cell
            out.append((m.group(1), cells))
        return out

    # The emitting chart lists what it is doing to everyone else...
    assert rows("a") == [("emitter", ["By region", "region", "in", "north"])]
    # ...and the chart on the receiving end names the chart that narrowed it,
    # alongside the filter its own YAML declares.
    assert rows("b") == [
        ("declared", ["declared", "region", "not in", "east"]),
        ("incoming", ["By region", "region", "in", "north"]),
    ]


def test_the_source_carries_its_chart_type_icon():
    """Recognisable before it is read; and a declared filter comes from the
    definition, not a chart, so it gets no glyph."""
    yaml = (
        "name: T\ncharts:\n"
        "  a: {type: bar, dataset: sales, title: By region, x: region}\n"
        "  b: {type: table, dataset: sales, title: Rows,"
        " filters: [{column: region, op: ni, values: [east]}]}\n"
        'layout:\n  - ["@30", "a", "b"]\n'
        "datasets:\n  sales: |\n    region,amount\n    north,10\n"
    )
    html = ff.Dashboard.from_yaml(yaml).render_cell("b", cf_tokens=["a|region=north"])

    incoming = re.search(r'<tr class="src-incoming">(.*?)</tr>', html, re.S).group(1)
    assert "<svg" in incoming, "a chart source shows its type"
    assert 'd="M3 13V7M8 13V3M13 13v-4"' in incoming, "the bar glyph"

    declared = re.search(r'<tr class="src-declared">(.*?)</tr>', html, re.S).group(1)
    source = declared.split("</td>", 1)[0]             # past it: the column's type icon
    assert "<svg" not in source, "a declared filter has no chart to show"


def test_the_filter_panel_columns_add_up():
    """Auto table layout ignores `max-width` on a cell, so a long chart title
    took the width it wanted and the other three columns wrapped — `Order
    status` over two lines, `refunded` split mid-word. The table is fixed-layout
    with declared widths now, which only works if they actually fit: the values
    column absorbs the remainder, so the others must leave it enough for a date
    range. (The fifth is a global filter's remove button.)
    """
    css = (Path(__file__).resolve().parent.parent / "fireflyer" / "dashboard.css").read_text()

    assert "table-layout: fixed" in css, "auto layout ignores per-cell widths"
    panel_width = int(re.search(r"width: min\((\d+)px", css).group(1))
    widths = dict(
        (int(n), int(w))
        for n, w in re.findall(r"th:nth-child\((\d)\) \{ width: (\d+)px", css)
    )
    assert sorted(widths) == [1, 2, 3, 5], "the values column takes what is left"
    declared = [widths[n] for n in sorted(widths)]

    panel_padding, cell_gaps = 20, 32            # 8px 10px panel, 8px per gap
    remainder = panel_width - panel_padding - sum(declared) - cell_gaps
    # Roughly 6px per character at this font size.
    assert remainder >= len("2026-06-01–2026-06-08") * 6, remainder
    assert declared[1] >= len("Order status") * 6, "a column name should not wrap"


_GLOBAL_YAML = (
    "name: T\ncharts:\n"
    "  a: {type: pie, dataset: sales, title: By region, column: region}\n"
    "  b: {type: table, dataset: sales, title: Rows}\n"
    "  c: {type: table, dataset: other, title: Other}\n"
    'layout:\n  - ["@30", "a", "b", "c"]\n'
    "datasets:\n"
    "  sales: |\n    region,amount\n    north,10\n    south,5\n"
    "  other: |\n    city\n    Kyiv\n"
)


def test_a_global_filter_shows_as_global_with_a_remove_button():
    """It has no chart to click to undo it, so its row says Global and carries
    its own remove — a toggle of exactly its token."""
    from fireflyer import filters as filters_mod

    token = filters_mod.global_token("region", "ni", ["south"])
    html = ff.Dashboard.from_yaml(_GLOBAL_YAML).render_cell("b", cf_tokens=[token])
    row = re.search(r'<tr class="src-global">(.*?)</tr>', html, re.S).group(1)
    assert "<span>Global</span>" in row
    assert '<td class="op">not in</td>' in row
    remove = re.search(r'class="fireflyer-filter-remove"[^>]*hx-vals=\'([^\']*)\'', row).group(1)
    assert json.loads(html_mod.unescape(remove)) == {"remove": [token], "open_filter": "b"}
    # It narrows the chart: the badge is blue and counts it.
    assert 'class="fireflyer-filter-indicator has-filters"' in html


def test_a_global_filter_skips_a_chart_without_its_column():
    """The crossfilter contract: a chart whose data lacks the column ignores it,
    and its panel doesn't claim otherwise."""
    from fireflyer import filters as filters_mod

    token = filters_mod.global_token("region", "in", ["north"])
    html = ff.Dashboard.from_yaml(_GLOBAL_YAML).render_cell("c", cf_tokens=[token])
    assert 'class="src-global"' not in html
    assert 'class="fireflyer-filter-indicator"' in html


def test_every_panel_offers_the_builders_filter_fields_for_its_columns():
    """The + form is the chart builder's own fields, offering this chart's
    columns — so a filter typed here reads like one written in the builder."""
    from fireflyer.params import filter_fields

    html = ff.Dashboard.from_yaml(_GLOBAL_YAML).render_cell("b")
    form = re.search(r'<form class="fireflyer-filter-add"(.*?)</form>', html, re.S).group(0)
    types = {"region": "text", "amount": "number"}
    assert filter_fields(list(types), types=types, live="sales") in form
    assert 'hx-post="/dashboard"' in form
    # The full page (`to_html`) has the same panel, so both paths agree.
    page = ff.Dashboard.from_yaml(_GLOBAL_YAML).to_html()
    assert filter_fields(["city"], types={"city": "text"}, live="other") in page


def test_every_filter_but_a_declared_one_has_a_remove_button():
    """Declared filters are the chart's definition; the rest are viewer state
    and can be cleared from the panel, without finding the slice that set them."""
    from fireflyer import filters as filters_mod

    yaml = _GLOBAL_YAML.replace(
        "  b: {type: table, dataset: sales, title: Rows}\n",
        "  b: {type: table, dataset: sales, title: Rows,"
        " filters: [{column: amount, op: ni, values: ['0']}]}\n",
    )
    tokens = ["a|region=north", "a|region=south"]
    html = ff.Dashboard.from_yaml(yaml).render_cell("b", cf_tokens=tokens)
    rows = dict(re.findall(r'<tr class="src-(\w+)">(.*?)</tr>', html, re.S))
    assert "fireflyer-filter-remove" not in rows["declared"]
    remove = re.search(r"hx-vals='([^']*)'", rows["incoming"]).group(1)
    assert json.loads(html_mod.unescape(remove)) == {"remove": tokens, "open_filter": "b"}


def test_the_panel_just_used_comes_back_open():
    """A filter change re-renders the dashboard; the panel the viewer was using
    must not snap shut under their pointer. Only that one — and only for this
    response, not as state a later click would carry."""
    dash = ff.Dashboard.from_yaml(_GLOBAL_YAML)
    skeleton = dash.render_skeleton(open_filter="b")
    vals = re.findall(r"hx-vals='(\{\"cid\"[^']*)'", skeleton)
    assert [json.loads(v).get("open_filter") for v in vals] == [None, "1", None]
    assert 'name="open_filter"' not in skeleton          # not a hidden input

    assert re.search(r'<details [^>]*name="fireflyer-filter" open>', dash.render_cell("b", open_filter=True))
    assert not re.search(r'<details [^>]*\bopen>', dash.render_cell("b"))


def test_only_a_global_filter_can_be_edited():
    """A typed filter can be retyped; a click filter is undone by clicking."""
    from fireflyer import filters as filters_mod

    token = filters_mod.global_token("region", "in", ["north"])
    html = ff.Dashboard.from_yaml(_GLOBAL_YAML).render_cell(
        "b", cf_tokens=[token, "a|region=south"]
    )
    rows = dict(re.findall(r'<tr class="src-(\w+)">(.*?)</tr>', html, re.S))
    edit = re.search(r'class="fireflyer-filter-edit"[^>]*>', rows["global"]).group(0)
    assert 'hx-target="#ff-add-b"' in edit and 'hx-post="/filter/edit"' in edit
    assert json.loads(html_mod.unescape(re.search(r"hx-vals='([^']*)'", edit).group(1)))["token"] == token
    assert "fireflyer-filter-edit" not in rows["incoming"]
    assert '<form class="fireflyer-filter-add" id="ff-add-b"' in html
