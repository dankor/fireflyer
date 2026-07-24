import pytest

import fireflyer as ff


def test_pie_orders_by_status(orders_parquet, snapshot):
    chart = ff.chart.pie(dataset=orders_parquet, title="Orders by Status", column="status")
    snapshot(chart.to_html())


def test_pie_sum_of_amount(orders_parquet, snapshot):
    """Slices sized by summed `amount` per status, not row count."""
    chart = ff.chart.pie(
        dataset=orders_parquet,
        title="Revenue by Status",
        column="status",
        agg="sum",
        value="amount",
    )
    snapshot(chart.to_html())


def test_pie_total_hidden(orders_parquet, snapshot):
    """`total: false` drops the donut-centre total."""
    chart = ff.chart.pie(
        dataset=orders_parquet, title="Orders by Status", column="status", total=False
    )
    snapshot(chart.to_html())


def test_pie_sum_requires_value_column(orders_parquet):
    with pytest.raises(ValueError, match="value"):
        ff.chart.pie(dataset=orders_parquet, title="x", column="status", agg="sum")


def test_pie_rejects_unknown_agg(orders_parquet):
    with pytest.raises(ValueError, match="agg"):
        ff.chart.pie(
            dataset=orders_parquet, title="x", column="status", agg="avg", value="amount"
        )


def test_pie_orders_declared_filter(orders_parquet, snapshot):
    chart = ff.chart.pie(
        dataset=orders_parquet,
        title="Open orders",
        column="status",
        filters=[{"column": "status", "op": "ni", "values": ["cancelled"]}],
    )
    snapshot(chart.to_html())


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


