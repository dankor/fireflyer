from dataclasses import dataclass, field
from pathlib import Path

import jinja2
import polars as pl

from fireflyer import filters as filters_mod
from fireflyer.params import (
    ChoiceParam,
    ColumnParam,
    DatasetParam,
    FilterListParam,
    TextParam,
)
from fireflyer.scan import scan

# Supported aggregations — each a polars reduction over the target column.
# A plain tuple per project style — no registry abstraction.
#   count  → number of non-null values
#   sum    → sum of the values
#   dcount → number of distinct (non-null) values
#   max    → largest value
#   min    → smallest value
AGGREGATIONS = ("count", "sum", "dcount", "max", "min")

# How the scalar is rendered.
#   compact → BI-standard abbreviation: 1,420 → "1.42K", 3_000_000 → "3M".
#             Mirrors d3's `~s` format: ~3 significant figures,
#             K/M/B/T suffixes, trailing zeros dropped. The sensible default for
#             a big-number KPI that must fit a small cell.
#   full    → every digit, thousands-separated: "1,420". Also drops any ".00".
# Both trim trailing decimal zeros — a whole value never shows "333.00".
FORMATS = ("compact", "full")

# 10^3 boundaries, largest first, ending with a base (no suffix) for < 1000.
# Lowercase business notation (b for billion, not SI's G): 1.42k, 3m, 2.5b.
_COMPACT_STEPS = ((1e12, "t"), (1e9, "b"), (1e6, "m"), (1e3, "k"), (1.0, ""))

_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "chart.html").read_text(),
    autoescape=True,
)


def _reduce(lf: pl.LazyFrame, column: str, agg: str):
    """Apply one aggregation and return the scalar Python result. Takes a
    LazyFrame so Polars reads only `column` (+ any filter columns) from Parquet.

    polars' `count()` already excludes nulls, so `count` means "non-null
    values". `dcount` drops nulls first so it counts distinct *values*, not
    "value-or-missing". `max`/`min` on an empty frame return None.
    """
    col = pl.col(column)
    exprs = {
        "count": col.count(),
        "sum": col.sum(),
        "dcount": col.drop_nulls().n_unique(),
        "max": col.max(),
        "min": col.min(),
    }
    return lf.select(exprs[agg].alias("value")).collect().item()


def _trim(text: str) -> str:
    """Drop trailing decimal zeros and a bare point: '1.50'→'1.5', '3.00'→'3'."""
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sig3(scaled: float) -> str:
    """`scaled` to three significant figures: 1.42, 14.2, 142."""
    decimals = max(0, 3 - len(str(int(abs(scaled)))))
    return f"{scaled:.{decimals}f}"


def _compact(value: float) -> str:
    """Abbreviate a number to ~3 significant figures with a K/M/B/T suffix.

    Below 1000 the number is shown plainly (trimmed). At/above a step, the
    scaled value keeps three significant figures: 1.42K, 14.2K, 142K, 1.42M.
    """
    magnitude = abs(value)
    for i, (threshold, suffix) in enumerate(_COMPACT_STEPS):
        if magnitude < threshold:
            continue
        rendered = _sig3(value / threshold)
        # Rounding can lift e.g. 999.5K → "1000"; that reads better one step up
        # (→ 1M). Re-render against the larger suffix when a larger one exists.
        if abs(float(rendered)) >= 1000 and i > 0:
            threshold, suffix = _COMPACT_STEPS[i - 1]
            rendered = _sig3(value / threshold)
        if suffix == "":  # base step: plain integer / trimmed float
            return f"{value:,}" if isinstance(value, int) else _trim(f"{value:,.2f}")
        return _trim(rendered) + suffix
    return str(value)


def _format_value(value, fmt: str) -> str:
    """Human-readable scalar. Numbers use `fmt` (compact/full); strings and
    dates (from max/min on a text column) pass through; None (empty data on
    max/min) shows an em dash."""
    if value is None:
        return "—"
    if isinstance(value, bool):  # bool is an int subclass — keep it readable
        return str(value)
    if not isinstance(value, (int, float)):
        return str(value)
    if fmt == "compact":
        return _compact(value)
    # full: every digit, thousands-separated, no trailing ".0"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    return f"{value:,}"


@dataclass
class Number:
    dataset: str
    title: str
    column: str
    agg: str = "count"
    format: str = "compact"
    filters: list = field(default_factory=list)

    _resolve = None  # name -> (uri, storage_options); not a dataclass field

    # Editor modal schema — one Param per constructor kwarg, in display order.
    # See fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("column", "Column"),
        ChoiceParam("agg", "Aggregation", AGGREGATIONS),
        ChoiceParam("format", "Format", FORMATS),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)
        if self.agg not in AGGREGATIONS:
            raise ValueError(
                f"number chart: unknown agg {self.agg!r} "
                f"(expected one of {list(AGGREGATIONS)})"
            )
        if self.format not in FORMATS:
            raise ValueError(
                f"number chart: unknown format {self.format!r} "
                f"(expected one of {list(FORMATS)})"
            )

    def to_html(self, *, theme: str | None = None) -> str:
        """`theme` forces a palette (`"dark"`/`"light"`); omitted, the chart
        follows the viewer's OS preference (inherited from the dashboard root
        when nested)."""
        lf = scan(self.dataset, self._resolve)
        preds = filters_mod.predicates(self.filters, lf.collect_schema().names())
        if preds:
            lf = lf.filter(*preds)
        value = _reduce(lf, self.column, self.agg)

        # `exact` is the un-abbreviated value, surfaced as a native hover tooltip
        # so a compact "1.42K" can still reveal its precise figure on hover.
        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            value=_format_value(value, self.format),
            exact=_format_value(value, "full"),
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
