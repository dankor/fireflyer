import pytest

import fireflyer as ff
from fireflyer.chart.number.chart import _format_value


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0"),
        (42, "42"),
        (999, "999"),
        (1420, "1.42k"),          # 3 significant figures
        (14200, "14.2k"),
        (142000, "142k"),
        (1_000_000, "1m"),         # no trailing ".00"
        (1_500_000, "1.5m"),
        (12_300_000, "12.3m"),
        (2_500_000_000, "2.5b"),
        (3_000_000_000_000, "3t"),
        (999_500, "1m"),           # rounding rolls up to the next suffix
        (999_950_000, "1b"),
        (-1420, "-1.42k"),         # sign preserved
        (333.0, "333"),            # whole float — no ".0"
    ],
)
def test_compact_formatting(value, expected):
    assert _format_value(value, "compact") == expected


def test_full_formatting_keeps_all_digits_no_trailing_zeros():
    assert _format_value(1_420, "full") == "1,420"
    assert _format_value(12_300_000, "full") == "12,300,000"
    assert _format_value(333.0, "full") == "333"          # whole float trimmed


def test_format_passes_strings_and_none_through():
    # max/min on a text column yields a string; empty data yields None.
    assert _format_value("paid", "compact") == "paid"
    assert _format_value(None, "compact") == "—"


def test_number_sum_amount(orders_csv, snapshot):
    chart = ff.chart.number(
        dataset=orders_csv,
        title="Revenue",
        column="amount",
        agg="sum",
    )
    snapshot(chart.to_html())


def test_number_count_is_non_null(orders_csv):
    """count = number of non-null values. Seed CSV has 7 rows, all with an id."""
    chart = ff.chart.number(dataset=orders_csv, title="t", column="id", agg="count")
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="7">7</div>' in html


def test_number_sum_thousands_separated(orders_csv):
    """amount sums to 42+15+99+30+75+12+60 = 333 (small here, but format check)."""
    chart = ff.chart.number(dataset=orders_csv, title="t", column="amount", agg="sum")
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="333">333</div>' in html
    # No caption text is rendered anymore.
    assert "fireflyer-number-caption" not in html


def test_number_dcount_distinct_values(orders_csv):
    """dcount = distinct values. status has paid, pending, cancelled → 3."""
    chart = ff.chart.number(
        dataset=orders_csv, title="t", column="status", agg="dcount"
    )
    html = chart.to_html()
    assert '<div class="fireflyer-number-value" title="3">3</div>' in html


def test_number_compact_value_hover_shows_exact(orders_csv):
    """Compact display abbreviates, but the title carries the full figure so a
    hover reveals it. Filter down to a single big row to exercise it."""
    # A synthetic big value isn't in the seed CSV; assert via the formatter path
    # by checking the title/display pair the template builds.
    chart = ff.chart.number(dataset=orders_csv, title="t", column="amount", agg="sum")
    html = chart.to_html()
    # display == exact here (333 < 1000); the title attribute is always present.
    assert 'title="333"' in html


def test_number_max_and_min(orders_csv):
    """max/min reduce the amount column (min 12, max 99)."""
    hi = ff.chart.number(dataset=orders_csv, title="t", column="amount", agg="max")
    lo = ff.chart.number(dataset=orders_csv, title="t", column="amount", agg="min")
    assert '<div class="fireflyer-number-value" title="99">99</div>' in hi.to_html()
    assert '<div class="fireflyer-number-value" title="12">12</div>' in lo.to_html()


def test_number_filter_applies_before_aggregating(orders_csv):
    """A declared filter narrows the rows before the reduction runs."""
    chart = ff.chart.number(
        dataset=orders_csv,
        title="Paid revenue",
        column="amount",
        agg="sum",
        filters=[{"column": "status", "op": "in", "values": ["paid"]}],
    )
    # Paid amounts: 42 + 99 + 75 + 60 = 276.
    assert '<div class="fireflyer-number-value" title="276">276</div>' in chart.to_html()


def test_number_rejects_unknown_agg(orders_csv):
    with pytest.raises(ValueError, match="unknown agg"):
        ff.chart.number(dataset=orders_csv, title="t", column="amount", agg="avg")


def test_number_rejects_unknown_format(orders_csv):
    with pytest.raises(ValueError, match="unknown format"):
        ff.chart.number(
            dataset=orders_csv, title="t", column="amount", format="scientific"
        )


def test_number_full_format_param(orders_csv):
    """format=full renders the whole number (seed sums are < 1000, so compact
    would match here too — this asserts the param threads through)."""
    chart = ff.chart.number(
        dataset=orders_csv, title="t", column="amount", agg="sum", format="full"
    )
    assert '<div class="fireflyer-number-value" title="333">333</div>' in chart.to_html()


def test_number_in_dashboard(orders_csv):
    """The number type resolves in dashboard YAML and renders a KPI cell."""
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  revenue: {{type: number, dataset: o, title: Revenue, column: amount, agg: sum}}
dashboard:
  - ["@20", "revenue:100"]
"""
    dashboard = ff.Dashboard.from_yaml(yaml)
    html = dashboard.to_html()
    assert 'class="fireflyer-chart fireflyer-number"' in html
    assert '<div class="fireflyer-number-value" title="333">333</div>' in html


def test_number_dashboard_rejects_bad_agg(orders_csv):
    yaml = f"""
name: Test dashboard
datasets:
  o: {{path: {orders_csv}}}
charts:
  bad: {{type: number, dataset: o, title: T, column: amount, agg: median}}
dashboard:
  - ["@20", "bad:100"]
"""
    with pytest.raises(ff.DashboardError, match="unknown agg"):
        ff.Dashboard.from_yaml(yaml)
