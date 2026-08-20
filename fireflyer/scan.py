"""How a chart reads its data — a lazy Parquet scan.

`dataset` is a dataset *name* in the dashboard/portal flow; a `resolve` callable
(set by the dashboard or a chart route) maps it to the stored Parquet's
`(uri, storage_options)`. Without a resolver, `dataset` is taken as a Parquet
path/URI directly (standalone use and tests). Scanning is lazy so Polars can
push projection + predicates down and read only the columns/row-groups a chart
needs — never the whole file.

`calcs` (the dataset's `CalcSet`, set on the chart by the dashboard) adds its
**column calcs** to the frame here, so a chart sees them as ordinary columns.
"""

from __future__ import annotations

import polars as pl


def scan(dataset: str, resolve=None, calcs=None) -> pl.LazyFrame:
    uri, storage_options = resolve(dataset) if resolve else (dataset, None)
    lf = pl.scan_parquet(uri, storage_options=storage_options)
    return calcs.attach(lf) if calcs is not None else lf
