"""Every chart declares the columns it reads, and reads only those.

A chart's params say what each value reads (`Param.reads`); `columns_read` turns
a chart's values into column names, and `scan_chart` hands the chart only those.
So a column a chart didn't declare isn't there to use — and what the dataset page
reports per column is what charts actually read. These tests hold every chart,
present and future, to it."""

import re
from pathlib import Path

import polars as pl
import pytest

import fireflyer as ff
from fireflyer import calcs as calcs_mod
from fireflyer.dashboard import CHART_TYPES
from fireflyer.params import READS, columns_read
from fireflyer.scan import scan_chart

CHART_DIR = Path(ff.__file__).parent / "chart"


@pytest.mark.parametrize("name, cls", sorted(CHART_TYPES.items()))
def test_every_param_says_what_it_reads(name, cls):
    for p in cls.PARAMS:
        assert getattr(p, "reads", None) in READS, f"{name}.{p.name}: say what it reads (Param.reads)"


@pytest.mark.parametrize("path", sorted(CHART_DIR.glob("*/chart.py")), ids=lambda p: p.parent.name)
def test_charts_read_data_only_through_the_gate(path):
    """Not `scan(...)` nor Parquet directly: `scan_chart(self)` is what limits a
    chart to its declared columns."""
    source = path.read_text()
    assert "scan_chart(self)" in source
    assert not re.search(r"(?<!_)\bscan\(|scan_parquet|read_parquet", source), path


def _pie(orders_parquet, **kw):
    chart = ff.chart.pie(dataset=orders_parquet, title="P", column="status", **kw)
    chart._calcs = calcs_mod.CalcSet.from_defs({
        "revenue": {"agg": "sum", "formula": "amount", "filters": [{"column": "day", "op": "ni", "values": ["x"]}]},
    })
    return chart


def test_a_chart_is_handed_only_what_it_declares(orders_parquet):
    chart = _pie(orders_parquet, calc="revenue")
    # The pie's column, its calc's formula and the calc's own filter — no more.
    assert set(scan_chart(chart).collect_schema().names()) == {"status", "amount", "day"}


def test_a_column_not_declared_is_not_there_to_read(orders_parquet):
    lf = scan_chart(_pie(orders_parquet, calc="revenue"))
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        lf.select("lat").collect()


def test_a_raw_table_without_columns_reads_every_column():
    from fireflyer.chart.table.chart import Table

    assert columns_read(Table.PARAMS, {"columns": [], "measures": []}) is None
    assert columns_read(Table.PARAMS, {"columns": [], "measures": ["n"]},
                        calcs_mod.CalcSet.from_defs({"n": {"agg": "count"}})) == set()


def test_what_a_dashboard_uses_is_traced_to_the_dataset_columns():
    """Column calcs count as the columns behind them; an overlay as its own."""
    yaml = """name: T
calcs:
  orders:
    status: {name: Order status, formula: status}
    order_day: {formula: 'str2dt(day, YYYY-MM-DD)'}
    revenue: {agg: sum, formula: unit_price * qty}
charts:
  p: {type: pie, dataset: orders, title: P, column: status, calc: revenue}
  b: {type: bar, dataset: orders, title: B, x: order_day, filters: [{column: channel, op: in, values: [web]}]}
  o: {type: number, dataset: other, title: O}
layout:
  - ["@30", "p", "b", "o"]
"""
    assert ff.Dashboard.columns_used(yaml, "orders") == {"status", "unit_price", "qty", "day", "channel"}
    assert ff.Dashboard.columns_used(yaml, "nope") == set()
    raw = yaml.replace("  o: {type: number,", "  t: {type: table, dataset: orders, title: T}\n  o: {type: number,") \
        .replace('"p", "b", "o"', '"p", "b", "t", "o"')
    assert ff.Dashboard.columns_used(raw, "orders") is None        # a raw table reads them all
