import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import jinja2
import polars as pl

from fireflyer import calcs as calcs_mod
from fireflyer import filters as filters_mod
from fireflyer.params import (
    BoolParam,
    DatasetParam,
    FilterListParam,
    IntParam,
    ListParam,
    TextParam,
)
from fireflyer.scan import scan

# Row cap. Without measures it bounds the rows *read* (pushed into the scan).
# With measures it bounds the **groups returned** instead — the aggregation
# still reads the whole dataset, because totalling the first 1000 rows of a
# larger file would report a number that is simply wrong.
MAX_ROWS = 1000

# Header length past which we assume the two-line clamp in chart.css bites and
# attach a tooltip carrying the full name. A heuristic — real glyph widths aren't
# knowable server-side, and the header is uppercased and letter-spaced, so it
# runs wider than `--ff-header-max: 16ch` suggests. Erring low only costs a
# redundant card on a header that happened to fit.
HEADER_CLAMP_CHARS = 24


def _sort_order(tokens: list[str]) -> tuple[list[str], list[bool]]:
    """Parse `['-revenue', '+status', 'day']` into names and descending flags.

    A leading `-` sorts descending, `+` or no prefix ascending. Order is
    precedence: the first entry is the primary sort.
    """
    names, descending = [], []
    for token in tokens:
        text = str(token).strip()
        if not text:
            continue
        if text[0] in "+-":
            names.append(text[1:].strip())
            descending.append(text[0] == "-")
        else:
            names.append(text)
            descending.append(False)
    return names, descending

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
    columns: list = field(default_factory=list)
    measures: list = field(default_factory=list)
    sort: list = field(default_factory=list)
    search: bool = True
    pagination: int = 5
    filters: list = field(default_factory=list)

    # Name -> (uri, storage_options) resolver, injected by the dashboard/route.
    # None: `dataset` is a Parquet path/URI (standalone). Not a dataclass field.
    _resolve = None
    # CalcSet for this chart's dataset; set by the dashboard. Column calcs show
    # up as columns, and `measures` keys resolve against it.
    _calcs = None

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ListParam("columns", "Columns", placeholder="all columns"),
        ListParam("measures", "Measures", placeholder="none — show raw rows"),
        ListParam("sort", "Sort", placeholder="-revenue, +status"),
        BoolParam("search", "Search box"),
        IntParam("pagination", "Rows per page", minimum=0),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)
        self.columns = [str(c) for c in (self.columns or [])]
        self.measures = [str(m) for m in (self.measures or [])]
        self.sort = [str(s) for s in (self.sort or [])]

    def _row_tokens(self, df: pl.DataFrame, dims: list[str], emitter: str) -> list[str]:
        """One crossfilter token per row, naming **every dimension in that row**.

        A row is a combination of its dimension values, so the whole combination
        is what a click means — one token with a part per column, toggling as a
        unit (same shape the bar uses for its two-dimension segments).

        The values come from Polars' own string cast, not Python's `str()`:
        `filters.predicates` compares `pl.col(c).cast(pl.String)` against the
        token text, and the two disagree for temporal columns — a datetime casts
        to `2026-06-01 00:00:00.000000` where `str()` gives `2026-06-01 00:00:00`,
        which would match nothing at all.
        """
        if not emitter or not dims:
            return [""] * df.height
        text = df.select([pl.col(d).cast(pl.String, strict=False) for d in dims])
        tokens = []
        for values in text.iter_rows():
            # A row with a null dimension isn't clickable. The filter model has
            # no "is null" op, and dropping just the null part would emit a
            # token that selects a *superset* of the row — clicking
            # `(name=null, team=x)` would filter on `team=x` alone and quietly
            # pull in every named row on that team. Offering no click beats
            # offering a wrong one.
            if any(value is None for value in values):
                tokens.append("")
                continue
            parts = [
                filters_mod.value_part(dim, value)
                for dim, value in zip(dims, values)
            ]
            tokens.append(f"{emitter}|{'|'.join(parts)}")
        return tokens

    def _cells(self, names, columns, measures, dims, row) -> list[dict]:
        """One rendered cell per column, with a hover card on the measure ones.

        The card is the same one the other charts show for an item: what the
        item *is* (here the row's dimension values), the calc's name with its
        **exact** value, then the description. A measure cell is formatted by the
        calc's `format` token, so `1.9k $` is exactly the case the exact line
        exists for (see SKILL.md, "Tooltips").

        Dimension cells get none: their text is the full, unformatted value
        already, so a card would only repeat what's on screen — and a table can
        render a thousand rows, where a card per cell is real page weight.
        """
        by_name = dict(zip(names, row))
        # The row's identity, e.g. "paid · 2026-06-01". Empty when the table has
        # no grouping columns, since a grand total isn't "about" any one thing.
        head = " · ".join(
            _format(by_name[d], False) for d in dims if by_name[d] is not None
        )
        cells = []
        for name, col, value in zip(names, columns, row):
            if name not in measures:
                cells.append({"text": _format(value, col["is_num"]), "is_num": col["is_num"]})
                continue
            cells.append({
                "text": self._calcs.fmt(name, value),
                "is_num": True,
                "tip": {
                    "head": head,
                    "name": self._calcs.label(name),
                    "exact": calcs_mod.exact_value(value) if value is not None else "",
                    "desc": col["desc"],
                },
            })
        return cells

    def _describe(self, column: str) -> str:
        """The calc description behind a column, or "" if there isn't one.

        Covers both kinds of calc a table can show: a `measures` key, and a
        **column calc** appearing as a plain dimension — both are defined in the
        dashboard's `calcs:` block and both may carry a `description`. A real
        dataset column has none.
        """
        return "" if self._calcs is None else self._calcs.describe_column(column)

    def _header(self, column: str, measures: list[str]) -> str:
        """What a viewer reads at the top of a column.

        A measure shows its calc's `name`; a dimension shows its calc's `name`
        too when the key is a column calc, so `{name: Order date, formula: ...}`
        relabels the column everywhere it appears without renaming anything the
        YAML refers to. A plain dataset column shows its own name.
        """
        if self._calcs is None:
            return column
        return (
            self._calcs.label(column) if column in measures
            else self._calcs.column_label(column)
        )

    def _rows(self) -> pl.DataFrame:
        """The table's rows — raw or grouped, then sorted."""
        lf = scan(self.dataset, self._resolve, self._calcs)
        df = self._aggregate(lf) if self.measures else self._raw(lf)
        names, descending = _sort_order(self.sort)
        # Sort by the keys the frame actually has. An unknown name is skipped
        # rather than raised on, matching how filters treat an absent column.
        keep = [(n, d) for n, d in zip(names, descending) if n in df.columns]
        if keep:
            df = df.sort([n for n, _ in keep], descending=[d for _, d in keep])
        return df

    def _raw(self, lf: pl.LazyFrame) -> pl.DataFrame:
        """Row-by-row, no aggregation. `head()` pushes down into the scan, so
        only ~MAX_ROWS are read from the Parquet, not the whole file."""
        df = lf.head(MAX_ROWS).collect()
        if self.columns:
            df = df.select([c for c in self.columns if c in df.columns])
        return filters_mod.apply(df, self.filters)

    def _aggregate(self, lf: pl.LazyFrame) -> pl.DataFrame:
        """Group by `columns` and compute one output column per measure.

        Filters apply *before* aggregating — the raw path filters a
        already-truncated slice, but a total over a filtered-after-the-fact
        sample would just be wrong. Each measure is computed by the shared calc
        engine (so a derived ratio and its own per-calc filters behave exactly
        as they do on the other charts) and the results are joined on the
        grouping columns.
        """
        if self._calcs is None:
            raise calcs_mod.CalcError(
                "table `measures` are calc keys, so they need a dashboard's "
                "`calcs:` block — a standalone table can only show raw rows"
            )
        schema = lf.collect_schema().names()
        preds = filters_mod.predicates(self.filters, schema)
        if preds:
            lf = lf.filter(*preds)

        dims = [c for c in self.columns if c in schema]
        frames = []
        for key in self.measures:
            frame = self._calcs.aggregate(lf, dims, key)
            # Select before renaming: an aggregate calc leaves a column named
            # after itself alongside `__value__`, so renaming first collides.
            frames.append(frame.select([*dims, pl.col(calcs_mod.VALUE).alias(key)]))

        out = frames[0]
        for other in frames[1:]:
            out = (
                # `nulls_equal`: a join follows SQL, where null never equals
                # null — so a group whose key is null failed to match itself and
                # each measure landed on its own row, one value per row down a
                # diagonal. `group_by` already treats all nulls as one group, so
                # this just makes the join agree with the grouping it's joining.
                out.join(other, on=dims, how="full", coalesce=True, nulls_equal=True)
                if dims
                else out.hstack(other)
            )
        return out.head(MAX_ROWS)

    def to_html(
        self,
        page: int = 1,
        query: str = "",
        *,
        theme: str | None = None,
        refresh: dict | None = None,
        crossfilter: dict | None = None,
    ) -> str:
        df = self._rows()
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

        # is_numeric drives right-alignment and thousands-separator formatting.
        # A measure column shows the calc's `name` as its header and is formatted
        # by the calc's own `format` token, so the table reads the same as a
        # number KPI on that calc. `desc` is the calc's description, shown as a
        # header tooltip — a header is a label, and this is where you find out
        # what it stands for (same role the description plays on the other
        # charts' tooltips).
        measures = [m for m in self.measures if m in df.columns]
        columns = []
        for name in df.columns:
            header = self._header(name, measures)
            desc = self._describe(name)
            columns.append({
                "header": header,
                "is_num": True if name in measures else df[name].dtype.is_numeric(),
                "desc": desc,
                # A card either explains the column or restores a name the
                # two-line clamp cut off. `desc` alone drives the underline —
                # an ellipsis already announces itself.
                "tip": bool(desc) or len(header) > HEADER_CLAMP_CHARS,
            })
        dims = [c for c in df.columns if c not in measures]
        emitter = (crossfilter or {}).get("emitter", "")
        selected = (crossfilter or {}).get("selected", set())
        tokens = self._row_tokens(df, dims, emitter)
        rows = [
            {
                "cells": self._cells(df.columns, columns, measures, dims, values),
                "token": token,
                "is_active": bool(token) and token in selected,
            }
            for values, token in zip(df.iter_rows(), tokens)
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
            refresh=refresh,
            crossfilter=crossfilter,
            has_selection=any(r["is_active"] for r in rows),
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
        # Appended only when set, so existing filter/measure-free snapshots keep
        # their hashes.
        for extra in (self.columns, self.measures, self.sort):
            if extra:
                parts.append(",".join(extra))
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
        for name, value in (
            ("columns", self.columns), ("measures", self.measures), ("sort", self.sort)
        ):
            if value:
                params[name] = ",".join(value)
        if self.filters:
            params["filters"] = _filters_json(self.filters)
        return urlencode(params)
