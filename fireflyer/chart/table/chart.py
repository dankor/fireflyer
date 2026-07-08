import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import jinja2
import polars as pl

from fireflyer import filters as filters_mod
from fireflyer.params import BoolParam, DatasetParam, FilterListParam, IntParam, TextParam

# Per the spec, the table chart reads at most the first 1000 rows.
MAX_ROWS = 1000

# Endpoint the chart's htmx controls call back into.
ENDPOINT = "/chart/table"

_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "chart.html").read_text(),
    autoescape=True,
)


def _format(value, is_num: bool) -> str:
    """Render a single cell.

    Numeric columns get thousands separators (1,234,567). Booleans look numeric
    to `isinstance(int)` but their column dtype is non-numeric, so `is_num`
    keeps them out of this branch.
    """
    if value is None:
        return ""
    if is_num and isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


def _search(df: pl.DataFrame, query: str) -> pl.DataFrame:
    """Case-insensitive substring match across all columns, stringified."""
    needle = query.casefold()
    columns = [pl.col(c).cast(pl.String, strict=False).fill_null("") for c in df.columns]
    haystack = pl.concat_str(columns, separator="\t").str.to_lowercase()
    return df.filter(haystack.str.contains(needle, literal=True))


# Compact paginator window — pages shown on either side of the current page.
# The usual shape: first/last always present, ± a small window around the
# current, ellipses for the gaps.
_PAGER_WINDOW = 2


def _page_links(current: int, total: int) -> list[int | None]:
    """Pages to render in the pagination strip.

    Returns ints for clickable pages and `None` for ellipsis positions, e.g.
    `[1, None, 8, 9, 10, 11, 12, None, 200]` for current=10, total=200. The
    first/last pages are always present; pages within `_PAGER_WINDOW` of the
    current page are shown; gaps wider than 1 collapse to a single `None`.
    """
    if total <= 1:
        return [1] if total == 1 else []
    around = set(
        range(max(1, current - _PAGER_WINDOW), min(total, current + _PAGER_WINDOW) + 1)
    )
    keep = sorted(around | {1, total})
    out: list[int | None] = []
    prev = 0
    for p in keep:
        if p - prev > 1:
            out.append(None)
        out.append(p)
        prev = p
    return out


def _filters_json(filters: list[filters_mod.Filter]) -> str:
    """Stable JSON for filters — used in chart_id hash and htmx URL params."""
    return json.dumps([f.as_dict() for f in filters], separators=(",", ":"))


@dataclass
class Table:
    dataset: str
    title: str
    search: bool = True
    pagination: int = 5
    filters: list = field(default_factory=list)

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        BoolParam("search", "Search box"),
        IntParam("pagination", "Rows per page", minimum=0),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)

    def to_html(
        self, page: int = 1, query: str = "", *, theme: str | None = None
    ) -> str:
        df = pl.read_csv(self.dataset).head(MAX_ROWS)
        df = filters_mod.apply(df, self.filters)
        if query:
            df = _search(df, query)

        total_rows = df.height
        if self.pagination > 0:
            total_pages = max(1, math.ceil(total_rows / self.pagination))
            page = max(1, min(page, total_pages))
            start = (page - 1) * self.pagination
            df = df.slice(start, self.pagination)
        else:
            total_pages = 1
            page = 1

        # Each column is (name, is_numeric). is_numeric drives right-alignment
        # and thousands-separator formatting.
        columns = [(name, df[name].dtype.is_numeric()) for name in df.columns]
        rows = [
            [(_format(v, is_num), is_num) for (_, is_num), v in zip(columns, row)]
            for row in df.iter_rows()
        ]

        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            chart_id=self._chart_id(),
            endpoint=ENDPOINT,
            base_params=self._base_params(),
            search=self.search,
            query=query,
            pagination=self.pagination,
            page=page,
            total_pages=total_pages,
            page_links=_page_links(page, total_pages),
            columns=columns,
            rows=rows,
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()

    def _chart_id(self) -> str:
        # Stable per chart identity so multiple tables on one page don't collide
        # and so snapshot tests stay deterministic. Filters join the key only
        # when present so existing filter-free snapshots keep their hashes.
        parts = [self.dataset, self.title, str(self.search), str(self.pagination)]
        if self.filters:
            parts.append(_filters_json(self.filters))
        digest = hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
        return f"fireflyer-table-{digest}"

    def _base_params(self) -> str:
        # Identifying params replayed on every htmx call; q and page get appended.
        params = {
            "dataset": self.dataset,
            "title": self.title,
            "search": int(self.search),
            "pagination": self.pagination,
        }
        if self.filters:
            params["filters"] = _filters_json(self.filters)
        return urlencode(params)
