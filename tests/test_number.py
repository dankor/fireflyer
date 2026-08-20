import re

import pytest

import fireflyer as ff


def test_number_sum_amount(orders_parquet, snapshot):
    chart = ff.chart.number(
        dataset=orders_parquet,
        title="Revenue",
        calc={"agg": "sum", "formula": "amount"},
    )
    snapshot(chart.to_html())


def test_number_count_is_non_null(orders_parquet):
    """count = number of non-null values. Seed CSV has 7 rows, all with an id."""
    chart = ff.chart.number(
        dataset=orders_parquet, title="t", calc={"agg": "count", "formula": "id"}
    )
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="7">7</div>' in html


def test_number_sum_thousands_separated(orders_parquet):
    """amount sums to 42+15+99+30+75+12+60 = 333 (small here, but format check)."""
    chart = ff.chart.number(
        dataset=orders_parquet, title="t", calc={"agg": "sum", "formula": "amount"}
    )
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="333">333</div>' in html
    # No caption text is rendered anymore.
    assert "fireflyer-number-caption" not in html


def test_number_dcount_distinct_values(orders_parquet):
    """dcount = distinct values. status has paid, pending, cancelled → 3."""
    chart = ff.chart.number(
        dataset=orders_parquet,
        title="t",
        calc={"agg": "dcount", "formula": "status"},
    )
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="3">3</div>' in html


def test_number_max_and_min(orders_parquet):
    """max/min reduce the amount column (min 12, max 99)."""
    hi = ff.chart.number(
        dataset=orders_parquet, title="t", calc={"agg": "max", "formula": "amount"}
    )
    lo = ff.chart.number(
        dataset=orders_parquet, title="t", calc={"agg": "min", "formula": "amount"}
    )
    assert '<div class="fireflyer-number-value" title="99">99</div>' in hi.to_html()
    assert '<div class="fireflyer-number-value" title="12">12</div>' in lo.to_html()


def test_number_format_token_applies(orders_parquet):
    """The calc's `format` token drives display: a `$` suffix and 2 decimals."""
    chart = ff.chart.number(
        dataset=orders_parquet,
        title="t",
        calc={"agg": "sum", "formula": "amount", "format": "0.00$"},
    )
    assert ">333.00$<" in chart.to_html()


def test_number_filter_applies_before_aggregating(orders_parquet):
    """A declared chart filter narrows the rows before the reduction runs."""
    chart = ff.chart.number(
        dataset=orders_parquet,
        title="Paid revenue",
        calc={"agg": "sum", "formula": "amount"},
        filters=[{"column": "status", "op": "in", "values": ["paid"]}],
    )
    # Paid amounts: 42 + 99 + 75 + 60 = 276.
    assert '<div class="fireflyer-number-value" title="276">276</div>' in chart.to_html()


def test_number_derived_calc_ratio(orders_parquet):
    """A number can show a derived ratio calc — avg order value = 333 / 7."""
    yaml = """
name: Test dashboard
calcs:
  orders:
    revenue: {agg: sum, formula: amount}
    n: {agg: count}
    aov: {formula: revenue / n, format: '0.00'}
charts:
  kpi: {type: number, dataset: orders, title: AOV, calc: aov}
layout:
  - ["@20", "kpi:100"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
    assert ">47.57<" in dashboard.to_html()


def test_number_tooltip_shows_calc_description(orders_parquet):
    """When the calc has a description, the value gets a styled hover tooltip
    with the calc name, its description, and the exact figure."""
    yaml = """
name: T
calcs:
  orders:
    revenue: {name: Revenue, agg: sum, formula: amount, description: Total paid revenue}
charts:
  kpi: {type: number, dataset: orders, title: Rev, calc: revenue}
layout:
  - ["@20", "kpi:1"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
    html = dashboard.to_html()
    assert 'class="fireflyer-number-tip"' in html
    assert ">Revenue</span>" in html                       # calc name
    assert ">Total paid revenue</div>" in html             # description
    assert 'class="fireflyer-number-tip-value">333</span>' in html  # exact value only


def test_number_without_description_keeps_plain_title(orders_parquet):
    """No description → no tooltip card, just the exact value as a plain title."""
    chart = ff.chart.number(
        dataset=orders_parquet, title="t", calc={"agg": "sum", "formula": "amount"}
    )
    html = chart.to_html()
    assert 'title="333"' in html
    assert '<div class="fireflyer-number-tip"' not in html  # no tooltip element


def test_number_in_dashboard(orders_parquet):
    """The number type resolves in dashboard YAML and renders a KPI cell."""
    yaml = """
name: Test dashboard
calcs:
  orders:
    revenue: {agg: sum, formula: amount}
charts:
  revenue: {type: number, dataset: orders, title: Revenue, calc: revenue}
layout:
  - ["@20", "revenue:100"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
    html = dashboard.to_html()
    assert 'class="fireflyer-chart fireflyer-number"' in html
    assert '<div class="fireflyer-number-value" title="333">333</div>' in html


def test_number_dashboard_rejects_bad_calc_ref(orders_parquet):
    yaml = """
name: Test dashboard
charts:
  bad: {type: number, dataset: orders, title: T, calc: missing}
layout:
  - ["@20", "bad:100"]
"""
    with pytest.raises(ff.DashboardError, match="unknown calc"):
        ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))


def test_dashboard_rejects_column_calc_as_a_charts_value(orders_parquet):
    """A column calc is a dimension — pointing a chart's `calc:` at one is caught
    at parse time, with a nudge toward `x`/`column`."""
    yaml = """
name: Test dashboard
calcs:
  orders:
    order_day: {formula: 'str2dt(day, YYYY-MM-DD)'}
charts:
  bad: {type: number, dataset: orders, title: T, calc: order_day}
layout:
  - ["@20", "bad:100"]
"""
    with pytest.raises(ff.DashboardError, match="column calc"):
        ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))


def test_number_dashboard_rejects_bad_agg(orders_parquet):
    yaml = """
name: Test dashboard
calcs:
  orders:
    bad: {agg: median, formula: amount}
charts:
  bad: {type: number, dataset: orders, title: T, calc: bad}
layout:
  - ["@20", "bad:100"]
"""
    with pytest.raises(ff.DashboardError, match="agg"):
        ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))


def test_number_tooltip_has_fallbacks_on_both_axes(orders_parquet):
    """A card centred on its item hangs off the edge of the screen when the item
    is in the first or last column. A vertical flip never notices — the overflow
    isn't vertical — so the inline axis needs its own fallbacks."""
    html = ff.chart.number(dataset=orders_parquet, title="t").to_html()
    block = re.search(r"@supports \(anchor-name: --a\) \{(.*?)\n\}", html, re.S).group(1)

    for name in ("start", "end", "above", "above-start", "above-end"):
        assert f"--ff-number-tip-{name}" in block, name
    # Sideways overflow is the common case, so those are tried first.
    order = re.search(r"position-try-fallbacks:(.*?);", block, re.S).group(1)
    assert order.index("tip-start") < order.index("tip-above")


def test_tooltip_anchor_rules_avoid_important(orders_parquet):
    """`!important` can stop a `@position-try` block applying, and the `@supports`
    block already wins on source order — it was never needed."""
    html = ff.chart.number(dataset=orders_parquet, title="t").to_html()
    block = re.search(r"@supports \(anchor-name: --a\) \{(.*?)\n\}", html, re.S).group(1)
    assert "!important" not in block


def test_number_tooltip_uses_the_shared_card_layout(orders_parquet):
    """The KPI's card is the same shape as the bar's and pie's: a name/value row
    with the value pushed right, then the description. The value used to sit on
    its own line under a rule, leaving the row's right-hand side empty."""
    html = ff.chart.number(
        dataset=orders_parquet, title="T",
        calc={"agg": "sum", "formula": "amount", "name": "Revenue",
              "description": "Booked revenue", "format": "0.0a"},
    ).to_html()
    card = re.search(
        r'<div class="fireflyer-number-tip".*?</div>\s*</div>', html, re.S
    ).group(0)

    row = re.search(
        r'<div class="fireflyer-number-tip-row">(.*?)</div>', card, re.S
    ).group(1)
    assert '<span class="fireflyer-number-tip-name">Revenue</span>' in row
    assert '<span class="fireflyer-number-tip-value">333</span>' in row   # same row
    # Description follows the row, not between the name and the value.
    assert card.index("tip-row") < card.index("tip-desc")

    # The value is pushed to the right of its row, as in the other charts.
    start = html.index(".fireflyer-number-tip-value {")
    assert "margin-left: auto" in html[start:html.index("}", start)]
