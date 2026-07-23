import fireflyer as ff


def test_pie_orders_by_status(orders_parquet, snapshot):
    chart = ff.chart.pie(dataset=orders_parquet, title="Orders by Status", column="status")
    snapshot(chart.to_html())


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


