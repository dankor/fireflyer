import math
import re

import pytest

import fireflyer as ff
from fireflyer.chart.pie.chart import CX, CY, R_OUT


def test_pie_orders_by_status(orders_parquet, snapshot):
    chart = ff.chart.pie(dataset=orders_parquet, title="Orders by Status", column="status")
    snapshot(chart.to_html())


def test_pie_sum_of_amount(orders_parquet, snapshot):
    """Slices sized by summed `amount` per status, not row count."""
    chart = ff.chart.pie(
        dataset=orders_parquet,
        title="Revenue by Status",
        column="status",
        calc={"agg": "sum", "formula": "amount"},
    )
    snapshot(chart.to_html())


def test_pie_total_hidden(orders_parquet, snapshot):
    """`total: false` drops the donut-centre total."""
    chart = ff.chart.pie(
        dataset=orders_parquet, title="Orders by Status", column="status", total=False
    )
    snapshot(chart.to_html())


def test_pie_rejects_unknown_agg(orders_parquet):
    with pytest.raises(ValueError, match="agg"):
        ff.chart.pie(
            dataset=orders_parquet,
            title="x",
            column="status",
            calc={"agg": "bogus", "formula": "amount"},
        ).to_html()


def test_pie_dcount_total_is_distinct_over_all(orders_parquet):
    """The centre total is the calc re-aggregated over the whole dataset — a
    dcount total is distinct-over-all, not the sum of per-slice dcounts."""
    chart = ff.chart.pie(
        dataset=orders_parquet,
        title="Distinct days by status",
        column="status",
        calc={"agg": "dcount", "formula": "day"},
    )
    html = chart.to_html()
    # 4 distinct days overall; summing per-status dcounts (2+2+1) would over-count,
    # so this asserts the grand-total re-aggregation.
    assert "Total: 4" in html


def test_pie_centre_tooltip_shows_calc_description(orders_parquet):
    """The donut-centre total gets a rich tooltip (calc name, description, and
    the exact total) when the calc has a description."""
    yaml = """
name: T
calcs:
  orders:
    revenue: {name: Revenue, agg: sum, formula: amount, description: Revenue by status}
charts:
  p: {type: pie, dataset: orders, title: Revenue, column: status, calc: revenue}
layout:
  - ["@30", "p:1"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
    html = dashboard.to_html()
    assert 'class="fireflyer-pie-total-tip"' in html
    assert ">Revenue</span>" in html                             # calc name
    assert ">Revenue by status</span>" in html                   # description
    assert 'class="fireflyer-pie-total-tip-value">333</span>' in html  # exact total


def test_pie_centre_tooltip_plain_without_description(orders_parquet):
    """No description → the centre keeps its plain native `Total:` title."""
    chart = ff.chart.pie(
        dataset=orders_parquet, title="t", column="status",
        calc={"agg": "sum", "formula": "amount"},
    )
    html = chart.to_html()
    assert "<title>Total: 333</title>" in html
    assert '<div class="fireflyer-pie-total-tip"' not in html   # no tooltip element
    assert 'class="fireflyer-pie-total-hit"' not in html        # no hit circle


def test_pie_orders_declared_filter(orders_parquet, snapshot):
    chart = ff.chart.pie(
        dataset=orders_parquet,
        title="Open orders",
        column="status",
        filters=[{"column": "status", "op": "ni", "values": ["cancelled"]}],
    )
    snapshot(chart.to_html())


def test_pie_legend_is_clickable_under_crossfilter(orders_parquet):
    """Inside a dashboard the legend crossfilters like the slices: each row
    carries the same toggle token; standalone it's plain."""
    crossfilter = {
        "endpoint": "/dashboard",
        "target": "#fireflyer-dashboard",
        "include": "#fireflyer-dashboard input[type=hidden]",
        "emitter": "status_pie",
        "active": set(),
    }
    chart = ff.chart.pie(dataset=orders_parquet, title="t", column="status")
    html = chart.to_html(crossfilter=crossfilter)
    assert 'class="fireflyer-legend fireflyer-legend-clickable"' in html  # the <ul>
    # A legend <li> carries the crossfilter post + emitter-prefixed token.
    assert 'hx-post="/dashboard"' in html
    assert "status_pie|status=paid" in html
    # Standalone: legend is not clickable.
    plain = chart.to_html()
    assert 'class="fireflyer-legend fireflyer-legend-clickable"' not in plain
    assert 'class="fireflyer-legend"' in plain


def test_pie_orders_crossfilter_active(orders_parquet, snapshot):
    """With a click_action and active value, slices render hx-* attrs and fade."""
    chart = ff.chart.pie(dataset=orders_parquet, title="Orders by Status", column="status")
    crossfilter = {
        "endpoint": "/dashboard",
        "target": "#fireflyer-dashboard",
        "include": "#fireflyer-dashboard input[type=hidden]",
        "emitter": "status_pie",
        "active": {"paid"},
    }
    snapshot(chart.to_html(crossfilter=crossfilter))


def test_pie_legend_pages_when_it_does_not_fit(csv_to_parquet):
    """The legend is one row above the donut and **pages** rather than wrapping:
    a ◀ n/m ▶ control, like Superset. The donut always shows every slice, so
    paging never hides data."""
    from fireflyer.chart.pie.chart import LEGEND_PAGE_SIZE

    rows = "\n".join(f"c{i},{i + 1}" for i in range(LEGEND_PAGE_SIZE + 3))
    parquet = csv_to_parquet("cat,v\n" + rows + "\n", "pager")
    yaml = f"""
name: Pager
calcs:
  {parquet}:
    total: {{agg: sum, formula: v}}
charts:
  p: {{type: pie, dataset: {parquet}, title: T, column: cat, calc: total}}
layout:
  - ["@40", "p"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()

    # One page of entries in the legend, every slice still in the donut.
    # Count inside the donut's own svg — the dashboard chrome has <path> icons.
    start = html.index('viewBox="0 0 220 220"')
    donut = html[start:html.index("</svg>", start)]
    assert html.count('<span class="swatch"') == LEGEND_PAGE_SIZE
    assert donut.count("<path ") == LEGEND_PAGE_SIZE + 3

    pager = re.search(r'class="fireflyer-pie-legend-pager".*?</div>', html, re.S).group(0)
    assert ">1/2<" in pager
    assert re.search(r'aria-label="Previous legend page"[^>]*disabled', pager)
    assert '"legend_page": "1"' in pager


def test_pie_legend_has_no_pager_when_everything_fits(csv_to_parquet):
    parquet = csv_to_parquet("cat,v\na,1\nb,2\n", "nopager")
    yaml = f"""
name: NoPager
charts:
  p: {{type: pie, dataset: {parquet}, title: T, column: cat}}
layout:
  - ["@40", "p"]
"""
    assert 'class="fireflyer-pie-legend-pager"' not in (
        ff.Dashboard.from_yaml(yaml).to_html()
    )


def test_pie_legend_pager_is_dashboard_only(csv_to_parquet):
    """Standalone there's no endpoint to post a page change to, so the whole
    legend renders rather than a slice you couldn't page past."""
    from fireflyer.chart.pie.chart import LEGEND_PAGE_SIZE

    rows = "\n".join(f"c{i},{i + 1}" for i in range(LEGEND_PAGE_SIZE + 3))
    parquet = csv_to_parquet("cat,v\n" + rows + "\n", "solo_pager")
    html = ff.chart.pie(dataset=parquet, title="t", column="cat").to_html()
    assert 'class="fireflyer-pie-legend-pager"' not in html
    assert html.count('<span class="swatch"') == LEGEND_PAGE_SIZE + 3


def test_pie_legend_page_selects_the_right_entries(csv_to_parquet):
    """Paging forward shows the next slice of categories, and an out-of-range
    page clamps rather than emptying the legend."""
    from fireflyer.chart.pie.chart import LEGEND_PAGE_SIZE

    rows = "\n".join(f"c{i},{100 - i}" for i in range(LEGEND_PAGE_SIZE + 2))
    parquet = csv_to_parquet("cat,v\n" + rows + "\n", "page2")
    yaml = f"""
name: Page
calcs:
  {parquet}:
    total: {{agg: sum, formula: v}}
charts:
  p: {{type: pie, dataset: {parquet}, title: T, column: cat, calc: total}}
layout:
  - ["@40", "p"]
"""
    dash = ff.Dashboard.from_yaml(yaml)

    def labels(page):
        return re.findall(
            r'<span class="label">([^<]+)</span>', dash.render_cell("p", legend_page=page)
        )

    first, second = labels(0), labels(1)
    assert first == [f"c{i}" for i in range(LEGEND_PAGE_SIZE)]      # value order
    assert second == [f"c{LEGEND_PAGE_SIZE}", f"c{LEGEND_PAGE_SIZE + 1}"]
    assert not set(first) & set(second)
    assert labels(99) == second                                     # clamps


def test_pie_tooltips_show_the_exact_value(csv_to_parquet):
    """Both of the pie's tooltips show the unrounded, thousands-separated figure
    — the slice card used to show the *formatted* value, so an abbreviated chart
    had no way to reveal what it stood for, and the centre's plain-title
    fallback disagreed with its own rich card."""
    parquet = csv_to_parquet("cat,v\na,1234567\nb,987654\n", "exact_tips")
    yaml = f"""
name: Exact
calcs:
  {parquet}:
    revenue: {{name: Revenue, description: Booked, agg: sum, formula: v,
               format: '0.0a $'}}
charts:
  p: {{type: pie, dataset: {parquet}, title: T, column: cat, calc: revenue}}
layout:
  - ["@30", "p"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert '<span class="fireflyer-pie-tooltip-val">1,234,567</span>' in html
    assert '<span class="fireflyer-pie-total-tip-value">2,222,221</span>' in html
    # The abbreviated form still labels the chart itself, just not the tooltip.
    assert "1.2m $" in html


def test_pie_centre_title_fallback_is_exact_too(csv_to_parquet):
    """With no description the centre falls back to a native title; it showed the
    formatted total while the rich card showed the exact one."""
    parquet = csv_to_parquet("cat,v\na,1234567\nb,987654\n", "exact_title")
    yaml = f"""
name: Exact
calcs:
  {parquet}:
    revenue: {{name: Revenue, agg: sum, formula: v, format: '0.0a $'}}
charts:
  p: {{type: pie, dataset: {parquet}, title: T, column: cat, calc: revenue}}
layout:
  - ["@30", "p"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert "<title>Total: 2,222,221</title>" in html


def test_pie_tooltip_flips_sides_near_a_screen_edge(orders_parquet):
    """The card hangs to one side of the donut, so overflow is sideways and the
    fallback that matters is the inline flip."""
    html = ff.chart.pie(dataset=orders_parquet, title="t", column="status").to_html()
    block = re.search(r"@supports \(anchor-name: --a\) \{(.*?)\n\}", html, re.S).group(1)
    assert "position-try-fallbacks: flip-inline;" in block
    assert "!important" not in block


def test_pie_tooltip_placement_agrees_across_both_paths(orders_parquet):
    """The anchored and non-anchored placements must describe the same spot —
    only clipping behaviour should differ between them. Both read one gap
    variable and offset from the same anchor point."""
    html = ff.chart.pie(dataset=orders_parquet, title="t", column="status").to_html()
    assert "--ff-tip-gap: 8px;" in html

    start = html.index(".fireflyer-pie-tooltip--r {")
    transform_path = html[start : html.index("}", start)]
    assert "translate(var(--ff-tip-gap), -50%)" in transform_path

    block = re.search(r"@supports \(anchor-name: --a\) \{(.*?)\n\}", html, re.S).group(1)
    assert "left: calc(anchor(right) + var(--ff-tip-gap));" in block
    assert "right: calc(anchor(left) + var(--ff-tip-gap));" in block
    assert "transform: translateY(-50%);" in block


def test_pie_tooltip_anchors_to_a_real_box(orders_parquet):
    """Same rule as the bar: the anchor is a marker box inside the SVG, never an
    `anchor-name` on a <path>/<circle>. A shape isn't a CSS box, so the anchor
    silently fails and the fixed card falls back to its static position."""
    html = ff.chart.pie(dataset=orders_parquet, title="t", column="status").to_html()
    body = html[html.index('<article class="fireflyer-chart') :]
    svg = body[body.index("<svg ") : body.index("</svg>")]

    markers = {
        name: (float(x), float(y))
        for x, y, name in re.findall(
            r'<foreignObject x="([\d.]+)" y="([\d.]+)".*?anchor-name: (--ff-pie-s\d+)',
            svg,
        )
    }
    cards = re.findall(
        r"--ff-tip-x: ([\d.]+)px; --ff-tip-y: ([\d.]+)px; "
        r"position-anchor: (--ff-pie-s\d+)",
        body,
    )
    assert len(markers) == len(cards) == 3

    # Each marker sits on the donut's bounding-box edge, level with its slice —
    # the same point the non-anchored path offsets the card from.
    for x, y, name in cards:
        assert markers[name] == (float(x), float(y))
        mx, my = markers[name]
        assert mx in (CX - R_OUT, CX + R_OUT)
        assert CY - R_OUT <= my <= CY + R_OUT

    assert not re.search(r"<(path|circle)[^>]*anchor-name", svg)


def test_pie_tooltips_never_cover_the_donut(orders_parquet):
    """A card offset sideways from a point *on the arc* still cuts across the
    circle, because the circle bulges back out below it — which is how a tooltip
    ended up sitting over the donut. Anchoring to the bounding-box edge instead
    means clearing the anchor clears the chart, whatever the slice's angle."""
    html = ff.chart.pie(dataset=orders_parquet, title="t", column="status").to_html()
    body = html[html.index('<article class="fireflyer-chart') :]
    placements = re.findall(
        r'fireflyer-pie-tooltip--(\w)"[^>]*--ff-tip-x: ([-\d.]+)px; '
        r"--ff-tip-y: ([-\d.]+)px",
        body,
    )
    assert len(placements) == 3

    gap = 8
    for width, height in ((230, 60), (240, 90)):   # a typical card, and a tall one
        for side, x, y in placements:
            px, py = float(x), float(y)
            x0 = px + gap if side == "r" else px - gap - width
            y0 = py - height / 2                    # translateY(-50%)
            # Closest point of the card's box to the donut centre.
            near_x = min(max(CX, x0), x0 + width)
            near_y = min(max(CY, y0), y0 + height)
            clearance = math.hypot(near_x - CX, near_y - CY) - R_OUT
            assert clearance >= gap - 0.01, (side, width, height, clearance)


def test_pie_tooltip_placement_survives_the_anchored_override(orders_parquet):
    """Regression: the per-slice point must arrive as a custom property, not as
    an inline `left`/`top`. Inline beats any stylesheet rule, so inline coords
    outranked the anchored block's `inset: auto` while its `position: fixed`
    still applied — canvas coordinates resolved against the viewport and the
    card shot into the page corner."""
    html = ff.chart.pie(dataset=orders_parquet, title="t", column="status").to_html()
    body = html[html.index('<article class="fireflyer-chart') :]

    # `[ "]` so the class match stops at the card itself and skips its
    # `-head`/`-row` children, which share the prefix.
    for card in re.findall(r'<div class="fireflyer-pie-tooltip[ "][^>]*>', body):
        assert "--ff-tip-x" in card
        assert "left:" not in card and "top:" not in card

    start = html.index(".fireflyer-pie-tooltip {")
    base = html[start : html.index("}", start)]
    assert "left: var(--ff-tip-x" in base and "top: var(--ff-tip-y" in base


def test_pie_total_tooltip_anchors_to_a_real_box(orders_parquet):
    """The donut-centre card anchors to a marker at the centre, not to the
    invisible <circle> hit area."""
    yaml = """
name: T
calcs:
  orders:
    revenue: {name: Revenue, agg: sum, formula: amount, description: By status}
charts:
  p: {type: pie, dataset: orders, title: Revenue, column: status, calc: revenue}
layout:
  - ["@30", "p:1"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
    html = dashboard.to_html()
    # From the chart, not the page: the cell's filter-indicator icon is an <svg>
    # of its own and comes first.
    body = html[html.index('<article class="fireflyer-chart') :]
    svg = body[body.index("<svg ") : body.index("</svg>")]

    # `[^>]*><div[^>]*` rather than `.*?`: a lazy dot still crosses element
    # boundaries, so it happily pairs the *first* foreignObject's coordinates
    # with a later element's anchor-name.
    marker = re.search(
        r'<foreignObject x="([\d.]+)" y="([\d.]+)"[^>]*><div[^>]*'
        r"anchor-name: --ff-pie-total",
        svg,
    )
    # Bottom of the ring, not its centre — the card hangs below its anchor, so
    # anchoring at the centre would drop it over the donut.
    assert marker and (float(marker.group(1)), float(marker.group(2))) == (CX, CY + R_OUT)
    assert "anchor-name" not in re.search(
        r'<circle[^>]*class="fireflyer-pie-total-hit"[^>]*>', svg
    ).group(0)


def test_pie_tooltips_are_opaque_and_sized_to_their_text(orders_parquet):
    """A translucent card picked up whatever slice sat behind it, and a min-width
    padded a two-word row into a panel. Both cards match the bar's."""
    html = ff.chart.pie(dataset=orders_parquet, title="t", column="status").to_html()
    for rule in (".fireflyer-pie-tooltip {", ".fireflyer-pie-total-tip {"):
        start = html.index(rule)
        block = html[start : html.index("}", start)]
        assert "background: var(--ff-tooltip-bg);" in block, rule
        assert "backdrop-filter" not in block, rule
        assert "min-width" not in block, rule
        assert "width: max-content;" in block, rule
