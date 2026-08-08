import pytest

import fireflyer as ff


def test_number_sum_amount(orders_parquet, snapshot):
    chart = ff.chart.number(
        dataset=orders_parquet,
        title="Revenue",
        measure={"agg": "sum", "formula": "amount"},
    )
    snapshot(chart.to_html())


def test_number_count_is_non_null(orders_parquet):
    """count = number of non-null values. Seed CSV has 7 rows, all with an id."""
    chart = ff.chart.number(
        dataset=orders_parquet, title="t", measure={"agg": "count", "formula": "id"}
    )
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="7">7</div>' in html


def test_number_sum_thousands_separated(orders_parquet):
    """amount sums to 42+15+99+30+75+12+60 = 333 (small here, but format check)."""
    chart = ff.chart.number(
        dataset=orders_parquet, title="t", measure={"agg": "sum", "formula": "amount"}
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
        measure={"agg": "dcount", "formula": "status"},
    )
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="3">3</div>' in html


def test_number_max_and_min(orders_parquet):
    """max/min reduce the amount column (min 12, max 99)."""
    hi = ff.chart.number(
        dataset=orders_parquet, title="t", measure={"agg": "max", "formula": "amount"}
    )
    lo = ff.chart.number(
        dataset=orders_parquet, title="t", measure={"agg": "min", "formula": "amount"}
    )
    assert '<div class="fireflyer-number-value" title="99">99</div>' in hi.to_html()
    assert '<div class="fireflyer-number-value" title="12">12</div>' in lo.to_html()


def test_number_format_token_applies(orders_parquet):
    """The measure's `format` token drives display: a `$` suffix and 2 decimals."""
    chart = ff.chart.number(
        dataset=orders_parquet,
        title="t",
        measure={"agg": "sum", "formula": "amount", "format": "0.00$"},
    )
    assert ">333.00$<" in chart.to_html()


def test_number_filter_applies_before_aggregating(orders_parquet):
    """A declared chart filter narrows the rows before the reduction runs."""
    chart = ff.chart.number(
        dataset=orders_parquet,
        title="Paid revenue",
        measure={"agg": "sum", "formula": "amount"},
        filters=[{"column": "status", "op": "in", "values": ["paid"]}],
    )
    # Paid amounts: 42 + 99 + 75 + 60 = 276.
    assert '<div class="fireflyer-number-value" title="276">276</div>' in chart.to_html()


def test_number_derived_measure_ratio(orders_parquet):
    """A number can show a derived ratio measure — avg order value = 333 / 7."""
    yaml = """
name: Test dashboard
measures:
  orders:
    revenue: {agg: sum, formula: amount}
    n: {agg: count}
    aov: {formula: revenue / n, format: '0.00'}
charts:
  kpi: {type: number, dataset: orders, title: AOV, measure: aov}
dashboard:
  - ["@20", "kpi:100"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
    assert ">47.57<" in dashboard.to_html()


def test_number_in_dashboard(orders_parquet):
    """The number type resolves in dashboard YAML and renders a KPI cell."""
    yaml = """
name: Test dashboard
measures:
  orders:
    revenue: {agg: sum, formula: amount}
charts:
  revenue: {type: number, dataset: orders, title: Revenue, measure: revenue}
dashboard:
  - ["@20", "revenue:100"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
    html = dashboard.to_html()
    assert 'class="fireflyer-chart fireflyer-number"' in html
    assert '<div class="fireflyer-number-value" title="333">333</div>' in html


def test_number_dashboard_rejects_bad_measure_ref(orders_parquet):
    yaml = """
name: Test dashboard
charts:
  bad: {type: number, dataset: orders, title: T, measure: missing}
dashboard:
  - ["@20", "bad:100"]
"""
    with pytest.raises(ff.DashboardError, match="unknown measure"):
        ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))


def test_number_dashboard_rejects_bad_agg(orders_parquet):
    yaml = """
name: Test dashboard
measures:
  orders:
    bad: {agg: median, formula: amount}
charts:
  bad: {type: number, dataset: orders, title: T, measure: bad}
dashboard:
  - ["@20", "bad:100"]
"""
    with pytest.raises(ff.DashboardError, match="agg"):
        ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))
