import fireflyer as ff


def test_bar_orders_by_day_stacked(orders_csv, snapshot):
    chart = ff.chart.bar(
        dataset=orders_csv,
        title="Orders by Day, stacked by status",
        x="day",
        y="status",
    )
    snapshot(chart.to_html())


def test_bar_segments_match_data(orders_csv):
    """Fixture distribution by (day, status):
        2026-06-01 → paid:3                 (1 segment)
        2026-06-02 → pending:1, paid:1      (2 segments)
        2026-06-03 → cancelled:1            (1 segment)
        2026-06-04 → pending:1              (1 segment)
    """
    chart = ff.chart.bar(dataset=orders_csv, title="t", x="day", y="status")
    html = chart.to_html()
    # 5 stack segments total across 4 bars.
    assert html.count("<rect") == 5
    # Per-bar totals appear once each (3, 2, 1, 1).
    assert html.count('class="fireflyer-bar-value">3<') == 1
    assert html.count('class="fireflyer-bar-value">2<') == 1
    assert html.count('class="fireflyer-bar-value">1<') == 2
    # All four dates appear as x labels.
    for date in ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"):
        assert f">{date}<" in html
    # Legend lists every y value with its total count across all bars.
    # paid=4 (id 1,3,5,7), pending=2 (id 2,6), cancelled=1 (id 4).
    assert '<span class="label">paid</span>' in html
    assert '<span class="meta">4</span>' in html


def test_bar_segments_clickable_with_crossfilter(orders_csv):
    """With a crossfilter ctx, segments carry hx-* attrs and emitter-prefixed tokens."""
    chart = ff.chart.bar(dataset=orders_csv, title="t", x="day", y="status")
    crossfilter = {
        "endpoint": "/dashboard",
        "target": "#fireflyer-dashboard",
        "include": "#fireflyer-dashboard input[type=hidden]",
        "emitter": "by_day",
        "active": {"paid"},
    }
    html = chart.to_html(crossfilter=crossfilter)
    # Every segment carries hx-post; the toggle token includes emitter + y col.
    assert "hx-post=\"/dashboard\"" in html
    assert "by_day|status=paid" in html
    # The paid segments (all of them — paid spans multiple bars) are marked active.
    # Fixture: paid appears on 06-01 (×3) and 06-02 (×1) → 2 active segments.
    assert html.count('data-active="1"') == 2
    # has-selection toggles the fade for non-selected segments.
    assert "fireflyer-bar has-selection" in html
    # Legend's paid entry is marked active.
    assert '<li class="active">' in html


def test_bar_hover_tooltip_renders(orders_csv):
    """Each segment gets a paired tooltip below the SVG, indexed by data-i."""
    chart = ff.chart.bar(dataset=orders_csv, title="t", x="day", y="status")
    html = chart.to_html()
    # Fixture has 5 segments total.
    assert html.count('class="fireflyer-bar-tooltip"') == 5
    # Each tooltip shows the x · y label.
    assert "2026-06-01 · paid" in html
    # And carries the count.
    assert 'class="fireflyer-bar-tooltip-meta">3<' in html


def test_bar_filter_narrows_before_grouping(orders_csv):
    """Declared filter applies before the (x, y) group-by + count."""
    chart = ff.chart.bar(
        dataset=orders_csv,
        title="Paid only",
        x="day",
        y="status",
        filters=[{"column": "status", "op": "in", "values": ["paid"]}],
    )
    html = chart.to_html()
    # Paid rows are 1,3,5,7 → days 06-01 (×3), 06-02 (×1). Two bars, two segments.
    assert html.count("<rect") == 2
    assert ">2026-06-01<" in html
    assert ">2026-06-02<" in html
    assert ">2026-06-03<" not in html
