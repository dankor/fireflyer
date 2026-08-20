import re

import fireflyer as ff


def test_bar_orders_by_day_stacked(orders_parquet, snapshot):
    chart = ff.chart.bar(
        dataset=orders_parquet,
        title="Orders by Day, stacked by status",
        x="day",
        y="status",
    )
    snapshot(chart.to_html())


def test_bar_long_axis_grows_and_scrolls(csv_to_parquet):
    """Many categories grow the viewBox past the default so each bar keeps a
    minimum width; the SVG's min-width then lets the canvas scroll. Few
    categories keep the default width (fit-to-cell, no scroll)."""
    import re

    lines = ["cat,grp,val"] + [f"c{i:02d},A,{i + 1}" for i in range(20)]
    dataset = csv_to_parquet("\n".join(lines) + "\n", "many")
    html = ff.chart.bar(dataset=dataset, title="t", x="cat", y="grp").to_html()
    vb = float(re.search(r'viewBox="0 0 ([\d.]+) 260"', html).group(1))
    assert vb > 380  # viewBox widened → wider-than-canvas SVG → canvas scrolls
    assert html.count("<rect") == 20  # every category still rendered

    # Few categories: shown whole at the fixed aspect, no growth, no scroll.
    from fireflyer.chart.bar.chart import FIT_PLOT_W, PLOT_X, RIGHT_MARGIN

    fixed = PLOT_X + FIT_PLOT_W + RIGHT_MARGIN
    few = csv_to_parquet("cat,grp,val\na,A,1\nb,A,2\nc,A,3\n", "few")
    html2 = ff.chart.bar(dataset=few, title="t", x="cat", y="grp").to_html()
    assert f'viewBox="0 0 {fixed} 260"' in html2


def test_bar_segments_match_data(orders_parquet):
    """Fixture distribution by (day, status):
        2026-06-01 → paid:3                 (1 segment)
        2026-06-02 → pending:1, paid:1      (2 segments)
        2026-06-03 → cancelled:1            (1 segment)
        2026-06-04 → pending:1              (1 segment)
    """
    chart = ff.chart.bar(dataset=orders_parquet, title="t", x="day", y="status")
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
    # Legend lists every y value — swatch and label only, no numbers; the values
    # are already on the bars and in the hover tooltips.
    assert '<span class="label">paid</span>' in html
    assert 'class="meta"' not in html


def test_bar_segments_clickable_with_crossfilter(orders_parquet):
    """With a crossfilter ctx, segments carry hx-* attrs and emitter-prefixed tokens."""
    chart = ff.chart.bar(dataset=orders_parquet, title="t", x="day", y="status")
    # `active` drives the legend (a series); `selected` drives the segments
    # (whole tokens, so only the clicked (x, y) cell lights).
    crossfilter = {
        "endpoint": "/dashboard",
        "target": "#fireflyer-dashboard",
        "include": "#fireflyer-dashboard input[type=hidden]",
        "emitter": "by_day",
        "active": {"paid"},
        "selected": {"by_day|day=2026-06-01|status=paid"},
    }
    html = chart.to_html(crossfilter=crossfilter)
    # Every segment carries hx-post; its token names **both** dimensions, since
    # a segment is an (x, y) cell.
    assert "hx-post=\"/dashboard\"" in html
    assert '"toggle": "by_day|day=2026-06-01|status=paid"' in html
    # Only the clicked cell lights — not every bar of that series. `paid` also
    # appears on 06-02, which must stay unselected.
    assert html.count('data-active="1"') == 1
    # has-selection toggles the fade for non-selected segments. Check class
    # membership, not adjacency — other modifiers sit between them.
    classes = re.search(
        r'<article class="(fireflyer-chart fireflyer-bar[^"]*)"', html
    ).group(1).split()
    assert "has-selection" in classes
    # Legend's paid entry is marked active, and every row is clickable — it
    # carries the same toggle token its segments do.
    assert re.search(r'<li class="active"[^>]*hx-post=', html)
    # A legend row means "this series", so it stays one-dimensional.
    assert html.count('"toggle": "by_day|status=paid"') == 1


def test_bar_hover_tooltip_renders(orders_parquet):
    """Each segment gets a paired tooltip below the SVG, indexed by data-i."""
    chart = ff.chart.bar(dataset=orders_parquet, title="t", x="day", y="status")
    html = chart.to_html()
    # Fixture has 5 segments total.
    assert html.count('class="fireflyer-bar-tooltip"') == 5
    # Each card leads with the bucket, then names the series and its value.
    assert '<div class="fireflyer-bar-tooltip-head">2026-06-01</div>' in html
    assert '<span class="fireflyer-bar-tooltip-name">paid</span>' in html
    assert '<span class="fireflyer-bar-tooltip-val">3</span>' in html
    # Each card carries its own placement — a share of the viewBox — so it lands
    # beside its segment without depending on an anchor resolving.
    assert re.search(r'class="fireflyer-bar-tooltip" data-i="0" '
                     r'style="--ff-tip-x: [\d.]+%; --ff-tip-y: [\d.]+%; '
                     r'position-anchor: --ff-bar-s0"', html)


def test_bar_filter_narrows_before_grouping(orders_parquet):
    """Declared filter applies before the (x, y) group-by + count."""
    chart = ff.chart.bar(
        dataset=orders_parquet,
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


def _date_axis_yaml(parquet: str, x: str = "order_day") -> str:
    return f"""
name: Dates on the axis
calcs:
  {parquet}:
    order_day:
      formula: str2dt(day, YYYY-MM-DD)
    revenue: {{agg: sum, formula: amount}}
charts:
  by_day:
    type: bar
    dataset: {parquet}
    title: Revenue by day
    x: {x}
    y: status
    calc: revenue
layout:
  - ["@40", "by_day"]
"""


def test_bar_x_can_be_a_column_calc(orders_parquet):
    """A `str2dt()` column calc is a dimension like any dataset column."""
    html = ff.Dashboard.from_yaml(_date_axis_yaml(orders_parquet)).to_html()
    assert ">2026-06-01<" in html
    assert ">2026-06-04<" in html


def test_bar_orders_a_temporal_axis_chronologically(orders_parquet):
    """Categorical bars sort by total; a date axis is a timeline, so it sorts by
    date even though 06-01 is the biggest bar and 06-04 the smallest."""
    html = ff.Dashboard.from_yaml(_date_axis_yaml(orders_parquet)).to_html()
    days = re.findall(r'class="fireflyer-bar-label"[^>]*>(2026-06-\d\d)<', html)
    assert days == sorted(days)


# --- automatic time grain -----------------------------------------------------


def _dated(csv_to_parquet, days, step=1, name="g"):
    """A parquet of one row per `step` days over `days` days, plus a dashboard
    whose bar x is a `str2dt()` column calc over it. Returns the axis labels."""
    import datetime

    start = datetime.date(2026, 1, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,{i + 1}"
        for i in range(0, days, step)
    ]
    return _labels(csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", name))


def _labels(parquet, x="order_day"):
    yaml = f"""
name: Grain
calcs:
  {parquet}:
    order_day:
      formula: str2dt(day, YYYY-MM-DD)
    revenue:
      agg: sum
      formula: amount
charts:
  b:
    type: bar
    dataset: {parquet}
    title: T
    x: {x}
    y: status
    calc: revenue
layout:
  - ["@40", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    return re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html)


def test_grain_stays_on_days_when_they_fit(csv_to_parquet):
    labels = _dated(csv_to_parquet, 10, name="short")
    assert len(labels) == 10
    assert labels[0] == "2026-01-01"


def test_grain_widens_to_weeks_then_months(csv_to_parquet):
    """Past the bar cap the axis buckets up a grain at a time, and the labels
    change shape with it (`2026-01-05` week start vs `2026-01` month)."""
    weekly = _dated(csv_to_parquet, 120, name="mid")
    assert len(weekly) <= 52 and all(re.fullmatch(r"\d{4}-\d\d-\d\d", s) for s in weekly)
    assert len(weekly) < 120                      # actually bucketed, not one per day

    monthly = _dated(csv_to_parquet, 400, name="long")
    assert monthly[:2] == ["2026-01", "2026-02"]  # month labels drop the day


def test_grain_reaches_quarters_and_years(csv_to_parquet):
    from fireflyer.chart.bar.chart import MAX_WINDOW_BARS

    # 4000 days is ~44 quarters, past the window, so the default view is the
    # most recent MAX_WINDOW_BARS of them — ending at the last quarter of all.
    quarters = _dated(csv_to_parquet, 4000, name="decade")
    assert len(quarters) == MAX_WINDOW_BARS
    assert quarters[-1] == "2036 Q4"
    assert all(re.fullmatch(r"\d{4} Q[1-4]", q) for q in quarters)

    # ~110 years of buckets exceeds the window, so the default view is the most
    # recent MAX_WINDOW_BARS of them (see the windowing tests below).
    years = _dated(csv_to_parquet, 40000, name="century")
    assert len(years) == MAX_WINDOW_BARS
    assert years[-1] == "2135"


def test_grain_never_exceeds_the_bar_cap_until_years(csv_to_parquet):
    """Every grain but the coarsest keeps the bar count under the cap; years are
    the floor, so a long enough range overflows and scrolls instead."""
    from fireflyer.chart.bar.chart import MAX_BARS

    for days, name in ((120, "c1"), (400, "c2"), (4000, "c3")):
        assert len(_dated(csv_to_parquet, days, name=name)) <= MAX_BARS


def test_grain_follows_the_data_not_the_calendar_span(csv_to_parquet):
    """Three rows spread over two years stay on `day` — the picker counts the
    distinct buckets each grain would produce, so a sparse range isn't coarsened
    for dates that were never populated."""
    csv = "day,status,amount\n2026-01-01,paid,1\n2027-01-01,paid,1\n2028-01-01,paid,1\n"
    labels = _labels(csv_to_parquet(csv, "sparse"))
    assert labels == ["2026-01-01", "2027-01-01", "2028-01-01"]


def test_grain_buckets_before_aggregating(csv_to_parquet):
    """Bucketing happens before the calc reduces, so a month bar is the calc over
    that month's rows — not a roll-up of per-day results (which would break avg)."""
    import datetime

    start = datetime.date(2026, 1, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,{i + 1}"
        for i in range(400)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", "avg")
    yaml = f"""
name: Grain
calcs:
  {parquet}:
    order_day:
      formula: str2dt(day, YYYY-MM-DD)
    avg_amount:
      agg: avg
      formula: amount
charts:
  b:
    type: bar
    dataset: {parquet}
    title: T
    x: order_day
    y: status
    calc: avg_amount
layout:
  - ["@40", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    # January is amounts 1..31, so a true monthly average is 16 — a mean of daily
    # means over unequal months would drift off it.
    assert 'class="fireflyer-bar-value">16<' in html


def test_non_temporal_axis_is_untouched(orders_parquet):
    """A categorical x keeps size ordering and raw labels — no bucketing. `day`
    here is a plain string column, never parsed with str2dt()."""
    html = ff.chart.bar(
        dataset=orders_parquet, title="t", x="status", y="day"
    ).to_html()
    labels = re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html)
    # paid (4 rows) outranks pending (2) and cancelled (1) — size order, not
    # alphabetical, which is what a non-temporal axis has always done.
    assert labels == ["paid", "pending", "cancelled"]


# --- in-chart grain picker ----------------------------------------------------


def _segments(html):
    """(abbr, is_active) for each grain segment in the picker."""
    return [
        (m.group(2), "is-active" in m.group(1))
        for m in re.finditer(
            r'<button type="button" class="fireflyer-bar-grain-seg([^"]*)"[^>]*>([^<]+)<',
            html,
        )
    ]


def test_grain_picker_shows_in_a_dashboard(csv_to_parquet):
    """A temporal axis gets an Auto segment plus every grain the column
    supports. Auto is active until the viewer picks something."""
    import datetime

    start = datetime.date(2026, 1, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,{i + 1}"
        for i in range(400)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", "picker")
    dash = ff.Dashboard.from_yaml(_date_axis_yaml(parquet))

    segs = _segments(dash.to_html())
    # Auto first, then the grains coarsest → finest.
    assert [abbr for abbr, _ in segs] == ["A", "Y", "Q", "M", "W", "D"]
    assert [abbr for abbr, on in segs if on] == ["A"]

    # Picking one re-renders at that grain and moves the highlight.
    yearly = dash.to_html(grain_tokens=["by_day|year"])
    assert [abbr for abbr, on in _segments(yearly) if on] == ["Y"]
    assert re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', yearly) == [
        "2026", "2027",
    ]


def test_grain_picker_is_dashboard_only(orders_parquet):
    """Standalone there's no endpoint to post a choice to, so no picker —
    the same rule crossfilter follows. Match the markup, not the class name:
    the CSS rules ship in the embedded stylesheet either way."""
    html = ff.chart.bar(
        dataset=orders_parquet, title="t", x="day", y="status"
    ).to_html()
    assert 'class="fireflyer-bar-grain"' not in html


def test_grain_picker_absent_for_a_categorical_axis(orders_parquet):
    yaml = f"""
name: Cat
charts:
  b: {{type: bar, dataset: {orders_parquet}, title: T, x: status, y: day}}
layout:
  - ["@40", "b"]
"""
    assert 'class="fireflyer-bar-grain"' not in ff.Dashboard.from_yaml(yaml).to_html()


def test_grain_tokens_round_trip_as_hidden_inputs(csv_to_parquet):
    """The pick rides the dashboard's hidden inputs next to the crossfilter
    tokens, so it survives a crossfilter click or a tab switch."""
    parquet = csv_to_parquet(
        "day,status,amount\n2026-01-01,paid,1\n2026-02-01,paid,2\n", "hidden"
    )
    html = ff.Dashboard.from_yaml(_date_axis_yaml(parquet)).to_html(
        grain_tokens=["by_day|month"]
    )
    assert '<input type="hidden" name="grain" value="by_day|month">' in html


def test_unparsed_dates_get_a_named_bucket(csv_to_parquet):
    """A row whose timestamp doesn't match the str2dt pattern becomes null, and
    nulls collect into one clearly-labelled bar at the end of the axis — a blank
    bar there reads as a rendering glitch rather than a data problem."""
    from fireflyer.chart.bar.chart import NO_DATE_LABEL

    csv = (
        "day,status,amount\n"
        "2026-01-01,paid,1\n"
        "2026-02-01,paid,1\n"
        "not a date,paid,1\n"
        "also broken,paid,1\n"
    )
    labels = _labels(csv_to_parquet(csv, "unparsed"))
    assert labels[-1] == NO_DATE_LABEL          # nulls sort last, after real dates
    assert labels[:2] == ["2026-01-01", "2026-02-01"]


def test_zero_values_are_not_drawn(csv_to_parquet):
    """A zero draws nothing, so it gets no segment, no legend row and no slot on
    the axis — an all-zero group would otherwise eat axis width for an invisible
    bar labelled `0`."""
    csv = (
        "day,grp,v\n"
        "2026-01-01,a,5\n"
        "2026-01-01,b,0\n"     # zero segment inside a real bar
        "2026-01-02,a,3\n"
        "2026-01-03,a,0\n"     # whole group is zero
        "2026-01-03,b,0\n"
    )
    html = ff.chart.bar(
        dataset=csv_to_parquet(csv, "zeros"), title="t", x="day", y="grp",
        calc={"agg": "sum", "formula": "v"},
    ).to_html()

    labels = re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html)
    assert labels == ["2026-01-01", "2026-01-02"]       # the all-zero day is gone
    assert re.findall(r'class="fireflyer-bar-value">([^<]+)<', html) == ["5", "3"]
    assert html.count("<rect") == 2                     # no zero-height segment
    # `b` is zero everywhere, so it never reaches the legend.
    assert re.findall(r'<span class="label">([^<]+)</span>', html) == ["a"]


def test_bar_width_stays_positive_at_every_scale():
    """Regression: the plot width was capped while the gap between bars was not,
    so past ~200 bars the gaps alone exceeded the whole plot and every bar came
    out *negative* wide — the SVG rendered nothing. Geometry now derives the gap
    from each bar's slot, so widths stay positive however squeezed."""
    from fireflyer.chart.bar.chart import _bar_geometry, _plot_width

    for n in (1, 2, 3, 10, 52, 200, 400, 1_000, 10_000):
        slot, bar_w = _bar_geometry(n, _plot_width(n))
        assert bar_w > 0, f"{n} bars gave a bar width of {bar_w}"
        assert bar_w < slot, f"{n} bars left no gap"


def test_long_axis_grows_to_full_width_then_caps(csv_to_parquet):
    """Standalone there's no endpoint to move a window with, so the whole axis is
    drawn: it scrolls at full bar width rather than being squeezed into the cell,
    and past the ceiling bars thin out instead of the canvas growing unbounded."""
    from fireflyer.chart.bar.chart import MAX_PLOT_W, MIN_BAR_W

    import datetime

    start = datetime.date(2020, 1, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,1"
        for i in range(365)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", "year")
    html = ff.chart.bar(dataset=parquet, title="t", x="day", y="status").to_html()

    assert len(re.findall(r'class="fireflyer-bar-label"', html)) == 365
    widths = [
        float(w)
        for w in re.findall(r'<rect x="[\d.-]+" y="[\d.-]+" width="([\d.-]+)"', html)
    ]
    assert min(widths) == MIN_BAR_W          # full width, not squeezed
    view_w = float(re.search(r'viewBox="0 0 ([\d.]+) 260"', html).group(1))
    assert 365 * MIN_BAR_W < view_w <= MAX_PLOT_W + 100   # wide enough to scroll
    assert "fireflyer-bar-scroll" in html


def _long_axis_html(csv_to_parquet, days, name, grain="day"):
    import datetime

    start = datetime.date(2025, 11, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,1"
        for i in range(days)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", name)
    return ff.Dashboard.from_yaml(_date_axis_yaml(parquet)).to_html(
        grain_tokens=[f"by_day|{grain}"] if grain else []
    )


def _scale_segments(html):
    """(is_current, tooltip) per scale segment. Anchored on the markup, not the
    CSS rules of the same name that ship in the embedded stylesheet."""
    return [
        ("is-current" in m.group(1), m.group(2))
        for m in re.finditer(
            r'<button type="button" class="fireflyer-bar-scale-seg([^"]*)"[^>]*>'
            r'<span class="fireflyer-bar-scale-tip">([^<]+)</span>',
            html,
        )
    ]


def test_long_axis_is_windowed_to_the_most_recent_bars(csv_to_parquet):
    """Past MAX_WINDOW_BARS buckets the axis is windowed rather than drawn whole,
    defaulting to the end of the range — the newest data, and a bounded DOM."""
    from fireflyer.chart.bar.chart import MAX_WINDOW_BARS

    html = _long_axis_html(csv_to_parquet, 400, "win")
    labels = re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html)
    assert len(labels) == MAX_WINDOW_BARS
    assert labels[-1] == "2026-12-05"                       # last bucket of 400


def test_scale_segments_name_their_dates(csv_to_parquet):
    """Every segment carries the range it would show, revealed on hover — so
    moving across the scale reads the timeline out as you go."""
    from fireflyer.chart.bar.chart import MAX_WINDOW_BARS, SCALE_SEGMENTS

    segs = _scale_segments(_long_axis_html(csv_to_parquet, 400, "win_segs"))
    assert len(segs) == SCALE_SEGMENTS
    assert segs[0][1] == f"2025-11-01 – 2025-11-{MAX_WINDOW_BARS:02d}"
    assert segs[-1][1].endswith("2026-12-05")               # ends at the last bucket
    assert all(" – " in tip for _, tip in segs)


def test_scale_lights_exactly_the_current_window(csv_to_parquet):
    """One lit segment — the lens — and by default it's the end of the range."""
    segs = _scale_segments(_long_axis_html(csv_to_parquet, 400, "win_cur"))
    assert [on for on, _ in segs].count(True) == 1
    assert segs[-1][0] is True


def test_scale_segment_moves_the_window(csv_to_parquet):
    """Clicking a segment re-renders at that offset — a plain htmx post, no JS."""
    from fireflyer.chart.bar.chart import MAX_WINDOW_BARS

    import datetime

    start = datetime.date(2025, 11, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,1"
        for i in range(400)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", "win_move")
    dash = ff.Dashboard.from_yaml(_date_axis_yaml(parquet))

    html = dash.to_html(grain_tokens=["by_day|day"])
    assert '"set_grain": "by_day|day|0"' in html            # jump-to-start segment

    at_start = dash.to_html(grain_tokens=["by_day|day|0"])
    labels = re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', at_start)
    assert labels[0] == "2025-11-01"
    assert len(labels) == MAX_WINDOW_BARS
    assert _scale_segments(at_start)[0][0] is True          # lens moved to the start

    # An offset past the end clamps to the last full window.
    clamped = dash.to_html(grain_tokens=["by_day|day|9999"])
    assert re.findall(
        r'class="fireflyer-bar-label"[^>]*>([^<]+)<', clamped
    )[-1] == "2026-12-05"


def test_changing_grain_resets_the_window(csv_to_parquet):
    """The grain buttons post `<cid>|<grain>` with no offset, so switching grain
    lands on the default window rather than a stale offset."""
    from fireflyer.dashboard import view_for

    assert view_for(["by_day|day|120"], "by_day") == ("day", 120)
    assert view_for(["by_day|week"], "by_day") == ("week", None)


def test_standalone_is_not_windowed(csv_to_parquet):
    """No endpoint means no way back to the hidden buckets, so the whole axis is
    drawn and scrolls instead."""
    import datetime

    start = datetime.date(2025, 11, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,1"
        for i in range(400)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", "solo")
    html = ff.chart.bar(dataset=parquet, title="t", x="day", y="status").to_html()
    assert len(re.findall(r'class="fireflyer-bar-label"', html)) == 400
    assert 'class="fireflyer-bar-scale"' not in html


def test_short_axis_has_no_scale(csv_to_parquet):
    """An axis that fits needs no window and no lens."""
    html = _long_axis_html(csv_to_parquet, 400, "short_scale", grain="month")
    assert _scale_segments(html) == []
    assert 'class="fireflyer-bar-scale"' not in html


def test_grain_and_scale_refresh_only_their_own_cell(csv_to_parquet):
    """A chart's view of its own axis changes nothing for its neighbours, so the
    grain and scale controls re-render just their cell. Crossfilter still goes
    wide — it changes every chart's data."""
    html = _long_axis_html(csv_to_parquet, 400, "cell_scope")

    for cls in ("fireflyer-bar-grain-seg", "fireflyer-bar-scale-seg"):
        m = re.search(
            rf'class="{cls}[^"]*"[^>]*hx-post="([^"]+)"[^>]*hx-target="([^"]+)"', html
        )
        assert m, cls
        assert m.group(1) == "/dashboard/cell"
        assert m.group(2) == "closest .fireflyer-dashboard-cell"

    # The bar segments (crossfilter) still replace the whole dashboard.
    rect = re.search(r'<rect [^>]*hx-post="([^"]+)"[^>]*hx-target="([^"]+)"', html)
    assert rect.group(1) == "/dashboard"
    assert rect.group(2) == "#fireflyer-dashboard"

    # The cell route needs the grid placement back to land in the same slot.
    vals = re.search(r'fireflyer-bar-grain-seg[^>]*hx-vals=\'([^\']+)\'', html).group(1)
    for key in ('"cid"', '"col"', '"row"', '"set_grain"'):
        assert key in vals


def test_grain_switch_keeps_the_same_chart_size(csv_to_parquet):
    """Switching grain must not resize the chart. Every view that's shown whole
    uses the same fixed plot width, so the viewBox — and therefore the rendered
    height in a given cell — is identical at two bars and at thirty. It used to
    grow one slot per bar, so each grain redrew at a different aspect and the
    tall ones overflowed their cell into a scrollbar."""
    import datetime

    start = datetime.date(2024, 4, 4)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,1"
        for i in range(428)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", "aspect")
    dash = ff.Dashboard.from_yaml(_date_axis_yaml(parquet))

    boxes = {}
    for grain in ("year", "quarter", "month", "week", "day"):
        html = dash.to_html(grain_tokens=[f"by_day|{grain}"])
        chart = html[html.index('class="fireflyer-bar-canvas"'):]
        boxes[grain] = re.search(r'viewBox="([^"]+)"', chart).group(1)
        # Shown whole: never the scrolling layout, so no inner scrollbar either.
        article = re.search(
            r'<article class="(fireflyer-chart fireflyer-bar[^"]*)"', html
        ).group(1)
        assert "fireflyer-bar-scroll" not in article, grain

    assert len(set(boxes.values())) == 1, boxes


def test_few_bars_do_not_become_billboards(csv_to_parquet):
    """A capped bar width keeps a two-bar view looking like a bar chart rather
    than two slabs filling the plot."""
    from fireflyer.chart.bar.chart import MAX_BAR_W

    csv = "cat,grp,v\na,A,5\nb,A,3\n"
    html = ff.chart.bar(
        dataset=csv_to_parquet(csv, "billboard"), title="t", x="cat", y="grp",
        calc={"agg": "sum", "formula": "v"},
    ).to_html()
    widths = [
        float(w)
        for w in re.findall(r'<rect x="[\d.-]+" y="[\d.-]+" width="([\d.-]+)"', html)
    ]
    assert widths and max(widths) <= MAX_BAR_W


def test_legend_placement_adapts_to_the_cell(csv_to_parquet):
    """The legend sits above by default and only moves to a right-hand column in
    an extremely wide, short cell — decided in CSS by a container query, since
    the cell's size isn't knowable server-side.

    The geometry: the SVG letterboxes to a fixed aspect, so the drawing is
    `min(width, aspect × height)` across. A right-hand legend costs ~150px of
    width, one above costs ~26px of height, and width is what a bar chart is
    short of — so above wins until the cell is past roughly 3:1.
    """
    csv = "day,grp,v\n2026-01-01,a,5\n2026-01-02,b,3\n"
    parquet = csv_to_parquet(csv, "legend")
    yaml = f"""
name: Legend
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: day, y: grp}}
layout:
  - ["@40", "b"]
"""
    embedded = ff.Dashboard.from_yaml(yaml).to_html()
    classes = re.search(
        r'<article class="(fireflyer-chart fireflyer-bar[^"]*)"', embedded
    ).group(1).split()
    # Only a dashboard cell has the definite size a size container needs.
    assert "fireflyer-bar-embedded" in classes
    assert "@container ffbar (min-aspect-ratio: 3 / 1)" in embedded

    # The legend is ordered above rather than moved in the DOM, so it still
    # reads after the chart it describes. Search the markup, not the stylesheet
    # that ships above it and mentions both class names.
    markup = embedded[embedded.index('<article class="fireflyer-chart fireflyer-bar'):]
    assert markup.index('class="fireflyer-bar-canvas"') < markup.index(
        'class="fireflyer-legend'
    )

    standalone = ff.chart.bar(dataset=parquet, title="t", x="day", y="grp").to_html()
    solo_classes = re.search(
        r'<article class="(fireflyer-chart fireflyer-bar[^"]*)"', standalone
    ).group(1).split()
    assert "fireflyer-bar-embedded" not in solo_classes


def test_bar_legend_rows_crossfilter_like_the_segments(csv_to_parquet):
    """Clicking a legend row toggles that series exactly as clicking one of its
    bar segments does — same token, same endpoint, so the two are interchangeable
    and a click from either updates the whole dashboard."""
    csv = (
        "day,status,v\n"
        "2026-01-01,paid,5\n"
        "2026-01-01,pending,3\n"
        "2026-01-02,paid,2\n"
    )
    parquet = csv_to_parquet(csv, "legend_click")
    yaml = f"""
name: Legend click
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: day, y: status,
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@40", "b"]
"""
    dash = ff.Dashboard.from_yaml(yaml)
    html = dash.to_html()

    ul = re.search(r'<ul class="([^"]*fireflyer-legend[^"]*)"', html).group(1)
    assert "fireflyer-legend-clickable" in ul

    rows = re.findall(r"<li\b[^>]*>", html)
    assert rows and all("hx-post=" in row for row in rows)
    # Crossfilter goes wide — it changes every chart's data, unlike the grain
    # controls which re-render their own cell.
    assert all('hx-post="/dashboard"' in row for row in rows)
    assert all('role="button"' in row and 'tabindex="0"' in row for row in rows)

    # The row's token matches the one its segments emit.
    assert '"toggle": "b|status=paid"' in html

    # Clicking it marks that series active in the legend and fades the rest.
    picked = dash.to_html(cf_tokens=["b|status=paid"])
    active_row = re.search(r'<li class="active".*?</li>', picked, re.S).group(0)
    assert '<span class="label">paid</span>' in active_row
    assert "has-selection" in re.search(
        r'<article class="([^"]+)"', picked
    ).group(1)


def test_bar_legend_not_clickable_standalone(csv_to_parquet):
    """No dashboard, no crossfilter endpoint — so no click affordance, matching
    the segments and the pie."""
    csv = "day,status,v\n2026-01-01,paid,5\n"
    html = ff.chart.bar(
        dataset=csv_to_parquet(csv, "legend_solo"), title="t", x="day", y="status"
    ).to_html()
    ul = re.search(r'<ul class="([^"]*fireflyer-legend[^"]*)"', html).group(1)
    assert "fireflyer-legend-clickable" not in ul
    assert not re.search(r"<li\b[^>]*hx-post=", html)


def _legend_labels(html):
    ul = html[html.index('<ul class="fireflyer-legend'):]
    return re.findall(r'<span class="label">([^<]+)</span>', ul[:ul.index("</ul>")])


def test_bar_legend_pages_instead_of_scrolling(csv_to_parquet):
    """More series than fit are reached with a pager, not a scrollbar. Every
    series is still drawn in the bars, so paging only hides legend rows."""
    from fireflyer.chart.bar.chart import LEGEND_PAGE_SIZE

    n = LEGEND_PAGE_SIZE + 2
    rows = "\n".join(f"d1,s{i:02d},{n - i}" for i in range(n))
    parquet = csv_to_parquet("day,grp,v\n" + rows + "\n", "bar_pager")
    yaml = f"""
name: Bar pager
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: day, y: grp,
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@40", "b"]
"""
    dash = ff.Dashboard.from_yaml(yaml)
    html = dash.to_html()

    assert len(_legend_labels(html)) == LEGEND_PAGE_SIZE
    assert html.count("<rect") == n                 # every series still drawn
    pager = re.search(r'class="fireflyer-bar-legend-pager".*?</div>', html, re.S).group(0)
    assert ">1/2<" in pager
    assert '"legend_page": "1"' in pager

    # Page two shows the remainder, and an out-of-range page clamps to it.
    second = _legend_labels(dash.render_cell("b", legend_page=1))
    assert len(second) == 2
    assert not set(_legend_labels(html)) & set(second)
    assert _legend_labels(dash.render_cell("b", legend_page=9)) == second


def test_bar_legend_never_scrolls(csv_to_parquet):
    """No scrollbar on the legend in either orientation — the pager replaced it."""
    csv = "day,grp,v\n2026-01-01,a,5\n2026-01-01,b,3\n"
    html = ff.chart.bar(
        dataset=csv_to_parquet(csv, "noscroll"), title="t", x="day", y="grp",
        calc={"agg": "sum", "formula": "v"},
    ).to_html()
    start = html.index(".fireflyer-bar .fireflyer-legend {")
    legend_css = html[start:html.index("}", start)]
    assert "overflow" not in legend_css
    assert "max-height" not in legend_css


def test_segment_click_filters_both_dimensions(csv_to_parquet):
    """A segment is an (x, y) cell, so clicking it filters both — as one token,
    so the pair toggles together rather than half of it lingering."""
    csv = (
        "day,status,v\n"
        "2026-01-01,paid,5\n"
        "2026-01-01,pending,3\n"
        "2026-01-02,paid,2\n"
    )
    parquet = csv_to_parquet(csv, "twodim")
    yaml = f"""
name: Two dims
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: day, y: status,
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@40", "b"]
"""
    dash = ff.Dashboard.from_yaml(yaml)
    token = "b|day=2026-01-01|status=paid"
    assert f'"toggle": "{token}"' in dash.to_html()

    # Both halves decode, and the pair clears in one click.
    from fireflyer import filters as filters_mod

    decoded = {f.column: f for f in filters_mod.decode_tokens([token])}
    assert decoded["day"].values == ("2026-01-01",)
    assert decoded["status"].values == ("paid",)
    assert filters_mod.toggle_token([token], token) == []


def test_bucketed_axis_click_filters_a_date_range(csv_to_parquet):
    """A bucket is a range — `2026-01` is all of January — so the click has to
    emit `>= start, < end`. An exact-value filter would match only the 1st."""
    import datetime

    from fireflyer import filters as filters_mod

    start = datetime.date(2026, 1, 1)
    rows = [
        f"{(start + datetime.timedelta(days=i)).isoformat()},paid,1"
        for i in range(120)
    ]
    parquet = csv_to_parquet("day,status,amount\n" + "\n".join(rows) + "\n", "bucketed")
    html = ff.Dashboard.from_yaml(_date_axis_yaml(parquet)).to_html(
        grain_tokens=["by_day|month"]
    )

    token = next(
        t for t in re.findall(r'"toggle": "([^"]+)"', html) if "2026-01-01" in t
    )
    assert token == "by_day|order_day~2026-01-01~2026-02-01|status=paid"

    span = next(
        f for f in filters_mod.decode_tokens([token]) if f.op == "between"
    )
    assert span.values == ("2026-01-01", "2026-02-01")   # half-open, tiles cleanly

    # And it selects exactly January — 31 days, not one.
    import polars as pl

    frame = pl.read_parquet(parquet).with_columns(
        pl.col("day").str.to_date("%Y-%m-%d").alias("order_day")
    )
    kept = frame.filter(*filters_mod.predicates([span], frame.columns))
    assert kept.height == 31


def test_only_the_clicked_cell_highlights(csv_to_parquet):
    """A segment's token names both dimensions, so exactly one cell lights.
    Matching on the series value alone lit that colour in *every* bucket."""
    csv = (
        "day,status,v\n"
        "2026-01-01,paid,5\n"
        "2026-01-02,paid,4\n"
        "2026-01-02,pending,3\n"
    )
    parquet = csv_to_parquet(csv, "onecell")
    yaml = f"""
name: One cell
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: day, y: status,
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@40", "b"]
"""
    dash = ff.Dashboard.from_yaml(yaml)
    html = dash.to_html(cf_tokens=["b|day=2026-01-01|status=paid"])

    # `paid` exists on both days; only the clicked day is active. Count inside
    # the SVG — the stylesheet above it has `[data-active="0"]` selectors.
    start = html.index('<svg viewBox="0 0 648')      # the bar's own svg
    svg = html[start:html.index("</svg>", start)]
    assert svg.count('data-active="1"') == 1
    assert svg.count('data-active="0"') == 2
    # The legend still marks the series — that's what a legend row means.
    assert re.search(r'<li class="active".*?<span class="label">paid</span>',
                     html, re.S)


# --- one-dimensional bars -----------------------------------------------------


def _one_axis(csv_to_parquet, name="oneaxis"):
    parquet = csv_to_parquet("cat,v\na,5\nb,3\nc,8\na,2\n", name)
    yaml = f"""
name: One axis
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat,
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@30", "b"]
"""
    return ff.Dashboard.from_yaml(yaml)


def test_bar_works_without_a_y(csv_to_parquet):
    """`y` is optional: one bar per category, no stacking. The bars are still
    ordered by size and carry their totals."""
    html = _one_axis(csv_to_parquet).to_html()
    assert re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html) == [
        "c", "a", "b",
    ]
    assert re.findall(r'class="fireflyer-bar-value">([^<]+)<', html) == ["8", "7", "3"]
    assert html.count("<rect") == 3          # one segment per bar, not stacked


def test_no_legend_without_a_y(csv_to_parquet):
    """One unnamed series — there's nothing for a legend to tell apart."""
    html = _one_axis(csv_to_parquet, "nolegend").to_html()
    assert 'class="fireflyer-bar-legend-bar"' not in html
    assert 'class="fireflyer-legend' not in html


def test_one_axis_click_filters_one_dimension(csv_to_parquet):
    """With no `y` there's only one dimension to filter on, so the token carries
    the bucket alone rather than a dangling empty half."""
    dash = _one_axis(csv_to_parquet, "onefilter")
    html = dash.to_html()
    assert sorted(set(re.findall(r'"toggle": "([^"]+)"', html))) == [
        "b|cat=a", "b|cat=b", "b|cat=c",
    ]

    picked = dash.to_html(cf_tokens=["b|cat=a"])
    start = picked.index('<svg viewBox="0 0 648')
    svg = picked[start:picked.index("</svg>", start)]
    assert svg.count('data-active="1"') == 1


def test_one_axis_tooltip_omits_the_series(csv_to_parquet):
    """`x · y` with no y left a trailing separator."""
    html = _one_axis(csv_to_parquet, "onetip").to_html()
    heads = re.findall(r'class="fireflyer-bar-tooltip-head">([^<]+)<', html)
    assert heads == ["c", "a", "b"]
    # No series to name, and no percent — one series is always 100% of its bar.
    assert 'class="fireflyer-bar-tooltip-name"' not in html
    assert 'class="fireflyer-bar-tooltip-pct"' not in html


def test_two_axis_bars_are_unchanged(csv_to_parquet):
    """The stacked path still stacks, still has a legend, still filters both."""
    parquet = csv_to_parquet("cat,grp,v\na,x,5\na,y,3\nb,x,2\n", "twoaxis")
    yaml = f"""
name: Two axis
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat, y: grp,
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@30", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert html.count("<rect") == 3                       # 2 stacked + 1
    assert 'class="fireflyer-bar-legend-bar"' in html
    assert '"toggle": "b|cat=a|grp=x"' in html


def test_editor_can_clear_the_y_column(csv_to_parquet):
    """Without a blank choice a `y` could be set but never unset in the modal."""
    from fireflyer import config_edit as ce

    parquet = csv_to_parquet("cat,grp,v\na,x,5\n", "clear_y")
    doc = f"""name: Clear
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat, y: grp}}
layout:
  - ["@30", "b:1"]
"""
    field = ce.build_form(doc, "b").split('data-param="y"')[1].split("</div>")[0]
    assert '<option value=""' in field


# --- top N --------------------------------------------------------------------


def _topn(csv_to_parquet, top, name, extra=""):
    parquet = csv_to_parquet("cat,v\na,5\nb,3\nc,8\nd,1\ne,9\n", name)
    yaml = f"""
name: Top
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat, top: {top}{extra},
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@30", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    return (
        re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html),
        re.findall(r'class="fireflyer-bar-value">([^<]+)<', html),
    )


def test_top_keeps_the_largest_bars(csv_to_parquet):
    """`top: N` drops the smallest, not the last."""
    assert _topn(csv_to_parquet, 0, "all")[0] == ["e", "c", "a", "b", "d"]
    labels, values = _topn(csv_to_parquet, 3, "top3")
    assert labels == ["e", "c", "a"]
    assert values == ["9", "8", "5"]


def test_top_above_the_bar_count_is_a_no_op(csv_to_parquet):
    assert _topn(csv_to_parquet, 99, "top99")[0] == ["e", "c", "a", "b", "d"]


def test_top_rescales_and_narrows_the_legend(csv_to_parquet):
    """Dropped rows leave the frame, so the y-scale and legend totals describe
    the bars actually drawn rather than the ones that were cut."""
    csv = "cat,grp,v\na,big,100\nb,small,5\nb,other,3\n"
    parquet = csv_to_parquet(csv, "top_scale")
    yaml = f"""
name: Top scale
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat, y: grp, top: 1,
      calc: {{agg: sum, formula: v}}}}
layout:
  - ["@30", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    assert re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html) == ["a"]
    # `small`/`other` belonged only to the dropped bar, so they leave the legend.
    ul = html[html.index('<ul class="fireflyer-legend'):]
    assert re.findall(r'<span class="label">([^<]+)</span>', ul[:ul.index("</ul>")]) == [
        "big"
    ]


def test_top_on_a_date_axis_keeps_the_biggest_but_stays_chronological(csv_to_parquet):
    """A timeline that jumped to the largest bucket would stop being a timeline:
    `top` selects by size, the axis still draws oldest-first."""
    csv = (
        "day,status,amount\n"
        "2026-01-01,paid,1\n"
        "2026-02-01,paid,50\n"
        "2026-03-01,paid,5\n"
        "2026-04-01,paid,30\n"
    )
    parquet = csv_to_parquet(csv, "top_dates")
    yaml = f"""
name: Top dates
calcs:
  {parquet}:
    order_day: {{formula: 'str2dt(day, YYYY-MM-DD)'}}
    total: {{agg: sum, formula: amount}}
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: order_day, y: status,
      top: 2, calc: total}}
layout:
  - ["@30", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html(grain_tokens=["b|month"])
    # The two biggest months (Feb=50, Apr=30) — in date order, not size order.
    assert re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html) == [
        "2026-02", "2026-04",
    ]


# --- direction ----------------------------------------------------------------


def _directional(csv_to_parquet, direction, name):
    parquet = csv_to_parquet("cat,grp,v\nalpha,x,5\nbeta,y,3\ngamma,x,8\n", name)
    yaml = f"""
name: Dir
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat, y: grp,
      direction: {direction}, calc: {{agg: sum, formula: v}}}}
layout:
  - ["@30", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    start = html.index('class="fireflyer-bar-canvas"')
    return html, html[start:html.index("</svg>", start)]


def test_horizontal_is_the_default_and_unchanged(csv_to_parquet):
    """Categories left-to-right, bars growing up, labels tilted so long ones
    don't collide — exactly what the chart has always drawn."""
    html, svg = _directional(csv_to_parquet, "horizontal", "dir_h")
    axis = re.search(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', svg)
    x1, y1, x2, y2 = axis.groups()
    assert y1 == y2 and x1 != x2                    # a horizontal baseline
    assert "rotate(-30" in svg
    assert "fireflyer-bar-sideways" not in re.search(
        r'<article class="([^"]+)"', html
    ).group(1).split()


def test_vertical_lays_the_bars_sideways(csv_to_parquet):
    """Categories top-to-bottom, bars growing rightward from a left-hand axis,
    labels written flat in the gutter."""
    from fireflyer.chart.bar.chart import _label_gutter

    html, svg = _directional(csv_to_parquet, "vertical", "dir_v")
    gutter = _label_gutter(["alpha", "beta", "gamma"])
    axis = re.search(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', svg)
    x1, y1, x2, y2 = axis.groups()
    assert float(x1) == float(x2) == gutter and y1 != y2   # a vertical axis
    assert "rotate(" not in svg                        # labels sit flat

    # Every bar starts at the axis and grows rightward in its own band.
    rects = re.findall(
        r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg
    )
    assert rects and all(float(w) > 0 and float(h) > 0 for _, _, w, h in rects)
    assert min(float(x) for x, _, _, _ in rects) == gutter
    assert "fireflyer-bar-sideways" in re.search(
        r'<article class="([^"]+)"', html
    ).group(1).split()


def test_vertical_canvas_grows_with_the_category_count(csv_to_parquet):
    """Bands are a fixed height, so the viewBox grows downward and the canvas
    scrolls rather than squeezing fifty categories into one screen."""
    from fireflyer.chart.bar.chart import CHART_H, VERT_SLOT_H

    rows = "\n".join(f"c{i:02d},x,{i + 1}" for i in range(40))
    parquet = csv_to_parquet("cat,grp,v\n" + rows + "\n", "dir_tall")
    yaml = f"""
name: Tall
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat, y: grp,
      direction: vertical, calc: {{agg: sum, formula: v}}}}
layout:
  - ["@30", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    start = html.index('class="fireflyer-bar-canvas"')
    height = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', html[start:]).group(1))
    assert height > CHART_H
    assert height >= 30 * VERT_SLOT_H          # windowed to 30 bars


def test_bar_tooltip_matches_the_pie_and_number_cards(csv_to_parquet):
    """The segment tooltip: the bucket as a header, the series and its
    **unrounded** value, and the calc's description. No percent, and no second
    copy of the value — an abbreviation is for the axis, where space is short."""
    parquet = csv_to_parquet("cat,grp,v\na,x,5400\na,y,3000\nb,x,2000\n", "richtip")
    yaml = f"""
name: Rich
calcs:
  {parquet}:
    revenue:
      name: Revenue
      description: Total booked revenue
      agg: sum
      formula: v
      format: 0.0a
charts:
  b: {{type: bar, dataset: {parquet}, title: T, x: cat, y: grp, calc: revenue}}
layout:
  - ["@30", "b"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    card = re.search(
        r'<div class="fireflyer-bar-tooltip".*?</div>\s*</div>', html, re.S
    ).group(0)

    assert '<div class="fireflyer-bar-tooltip-head">a</div>' in card
    assert '<span class="fireflyer-bar-tooltip-name">x</span>' in card
    # The exact figure, thousands-separated — not the axis's abbreviated `5.4k`.
    assert '<span class="fireflyer-bar-tooltip-val">5,400</span>' in card
    assert "Total booked revenue" in card
    assert "5.4k" not in card
    # No percent element — a share of its own bar is a second thing to read past
    # on the way to the number. (The `%` in the card's inline placement doesn't
    # count, hence matching the element rather than the character.)
    assert 'class="fireflyer-bar-tooltip-pct"' not in card
    assert 'class="fireflyer-bar-tooltip-exact"' not in card


def test_bar_tooltip_placement_tracks_its_segment(csv_to_parquet):
    """Each card's placement comes from its own segment's geometry, so the left
    bar gets a card on the left and a taller segment pushes its card higher."""
    parquet = csv_to_parquet("cat,v\na,5\nb,3\n", "abovetip")
    html = ff.chart.bar(
        dataset=parquet, title="t", x="cat", calc={"agg": "sum", "formula": "v"}
    ).to_html()
    tips = [
        (float(x), float(y))
        for x, y in re.findall(
            r'class="fireflyer-bar-tooltip"[^>]*--ff-tip-x: ([\d.]+)%; '
            r'--ff-tip-y: ([\d.]+)%', html
        )
    ]
    assert len(tips) == 2
    assert tips[0][0] < tips[1][0]          # left bar, left card
    assert tips[0][1] > tips[1][1]          # taller bar (a=5), higher card


def test_bar_tooltip_anchors_to_a_real_box(csv_to_parquet):
    """The anchor is a zero-size marker element, not the SVG shape. A shape isn't
    a CSS box, so `anchor-name` on one isn't reliable — and a failed anchor drops
    a `position: fixed` card at its *static* position, which is how cards ended
    up beside the wrong bar. A real box can't fail that way."""
    parquet = csv_to_parquet("cat,v\na,5\nb,3\n", "esctip")
    html = ff.chart.bar(
        dataset=parquet, title="t", x="cat", calc={"agg": "sum", "formula": "v"}
    ).to_html()

    # One marker per segment, inside the SVG at that segment's top-centre — in
    # the drawing's coordinate space, so letterboxing can't shift it.
    start = html.index('<svg viewBox="0 0 ')
    svg = html[start:html.index("</svg>", start)]
    segments = {
        int(d): (float(x) + float(w) / 2, float(y))
        for x, y, w, d in re.findall(
            r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)"[^>]*data-i="(\d+)"', svg
        )
    }
    markers = re.findall(
        r'<foreignObject x="([\d.]+)" y="([\d.]+)".*?anchor-name: --ff-bar-s(\d+)', svg
    )
    assert len(markers) == len(segments) == 2
    for x, y, i in markers:
        assert (float(x), float(y)) == segments[int(i)]
    assert "anchor-name" not in re.search(r"<rect [^>]*>", svg).group(0)

    # Base placement still stands where anchoring isn't supported...
    start = html.index(".fireflyer-bar-tooltip {")
    base = html[start:html.index("}", start)]
    assert "position: absolute" in base
    assert "clamp(" in base                       # clamped inside the canvas

    # ...and where it is, the card goes back to escaping every clipping ancestor.
    block = re.search(r"@supports \(anchor-name: --a\) \{(.*?)\n\}", html, re.S).group(1)
    assert "position: fixed;" in block
    for name in ("start", "end", "below", "below-start", "below-end"):
        assert f"--ff-bar-tip-{name}" in block
    assert "anchor-scope: all" in block


def test_horizontal_bars_pack_left(csv_to_parquet):
    """A handful of bars start at the axis instead of being spread across the
    whole plot. Dividing the full width between two bars marooned them at the
    third-points with the chart's own emptiness between them; leftover width now
    collects on the right, the way the sideways layout leaves leftover height at
    the bottom."""
    from fireflyer.chart.bar.chart import BAR_GAP, MAX_BAR_W, PLOT_X

    def positions(n, name):
        parquet = csv_to_parquet(
            "cat,v\n" + "\n".join(f"c{i},{n - i}" for i in range(n)) + "\n", name
        )
        html = ff.chart.bar(
            dataset=parquet, title="t", x="cat", calc={"agg": "sum", "formula": "v"}
        ).to_html()
        start = html.index('<svg viewBox="0 0 ')
        svg = html[start:html.index("</svg>", start)]
        return [
            (float(x), float(w))
            for x, w in re.findall(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg)
        ], svg

    two, _ = positions(2, "packl2")
    six, _ = positions(6, "packl6")
    # Same starting position and same slot pitch whatever the count.
    assert two[0][0] == six[0][0]
    assert round(two[1][0] - two[0][0], 2) == round(six[1][0] - six[0][0], 2)
    # The pitch is the capped slot, not the plot divided by the bar count.
    assert round(two[1][0] - two[0][0], 2) == MAX_BAR_W + BAR_GAP
    assert two[0][0] >= PLOT_X


def test_baseline_stops_where_the_bars_do(csv_to_parquet):
    """Running the axis the full plot width past a left-packed handful made the
    alignment look like a chart that failed to fill."""
    parquet = csv_to_parquet("cat,v\na,5\nb,3\n", "axisstop")
    html = ff.chart.bar(
        dataset=parquet, title="t", x="cat", calc={"agg": "sum", "formula": "v"}
    ).to_html()
    start = html.index('<svg viewBox="0 0 ')
    svg = html[start:html.index("</svg>", start)]
    rects = re.findall(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg)
    last_bar_end = max(float(x) + float(w) for x, w in rects)
    axis_end = float(re.search(r'<line x1="[\d.]+"[^>]*x2="([\d.]+)"', svg).group(1))
    view_w = float(re.search(r'viewBox="0 0 ([\d.]+)', svg).group(1))

    from fireflyer.chart.bar.chart import BAR_GAP

    assert last_bar_end <= axis_end < view_w        # ends past the bars, not the plot
    assert axis_end - last_bar_end <= BAR_GAP       # and only just past them


def test_vertical_label_gutter_fits_the_labels(csv_to_parquet):
    """The left gutter is measured from the labels, not fixed: short ones (an
    ISO date) shouldn't reserve room for long ones that aren't there, which left
    a block of empty chart before the bars even started."""
    from fireflyer.chart.bar.chart import (
        VERT_LABEL_MAX, VERT_LABEL_MIN, _label_gutter,
    )

    def gutter_of(labels, name):
        rows = "\n".join(f"{label},x,{i + 1}" for i, label in enumerate(labels))
        parquet = csv_to_parquet("cat,grp,v\n" + rows + "\n", name)
        html = ff.chart.bar(
            dataset=parquet, title="t", x="cat", y="grp", direction="vertical",
            calc={"agg": "sum", "formula": "v"},
        ).to_html()
        start = html.index('<svg viewBox="0 0 ')
        svg = html[start:html.index("</svg>", start)]
        return float(re.search(r'<line x1="([\d.]+)"', svg).group(1))

    short = gutter_of(["a", "b"], "gut_short")
    dates = gutter_of(["2026-06-01", "2026-06-02"], "gut_dates")
    long = gutter_of(["a very long category name indeed" * 3], "gut_long")

    assert short < dates < long                 # scales with the longest label
    assert short == VERT_LABEL_MIN              # floors, so labels never touch
    assert long == VERT_LABEL_MAX               # and caps, so bars keep room
    assert dates == _label_gutter(["2026-06-01"])


def test_only_sideways_bars_pin_to_the_left_edge(csv_to_parquet):
    """The sideways layout reads left-to-right off a label gutter, so dead space
    on its left is wasted. Upright bars are better centred — pinning those left
    just moved the gap to the right."""
    parquet = csv_to_parquet("cat,v\na,5\nb,3\n", "leftpin")

    def aspect(direction):
        html = ff.chart.bar(
            dataset=parquet, title="t", x="cat", direction=direction,
            calc={"agg": "sum", "formula": "v"},
        ).to_html()
        start = html.index('<svg viewBox="0 0 ')
        return re.search(r'preserveAspectRatio="([^"]+)"', html[start:]).group(1)

    assert aspect("horizontal") == "xMidYMid meet"
    assert aspect("vertical") == "xMinYMid meet"
