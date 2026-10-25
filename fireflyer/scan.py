"""How a chart reads its data — a lazy Parquet scan.

`dataset` is a dataset *name* in the dashboard/portal flow; a `resolve` callable
(set by the dashboard or a chart route) maps it to the stored Parquet's
`(uri, storage_options)`. Without a resolver, `dataset` is taken as a Parquet
path/URI directly (standalone use and tests). Scanning is lazy so Polars can
push projection + predicates down and read only the columns/row-groups a chart
needs — never the whole file.

`calcs` (the dataset's `CalcSet`, set on the chart by the dashboard) adds its
**column calcs** to the frame here, so a chart sees them as ordinary columns.

A chart reads through `scan_chart`, never `scan` itself: it hands the chart only
the columns its params declare (`params.columns_read`), so using one it didn't
declare fails at once — and the dataset page's per-column Dashboards can trust the
declarations. (tests/test_chart_columns.py holds every chart to it.)
"""

from __future__ import annotations

import polars as pl


def scan(dataset: str, resolve=None, calcs=None) -> pl.LazyFrame:
    uri, storage_options = resolve(dataset) if resolve else (dataset, None)
    lf = pl.scan_parquet(uri, storage_options=storage_options)
    return calcs.attach(lf) if calcs is not None else lf


def scan_chart(chart) -> pl.LazyFrame:
    """`chart`'s dataset, cut to the columns its params say it reads. A
    declared name the dataset lacks is left for the chart's own message."""
    from fireflyer.params import columns_read

    lf = scan(chart.dataset, chart._resolve, chart._calcs)
    values = {p.name: getattr(chart, p.name, None) for p in chart.PARAMS}
    used = columns_read(chart.PARAMS, values, chart._calcs)
    if used is None:
        return lf
    return lf.select([c for c in lf.collect_schema().names() if c in used])
