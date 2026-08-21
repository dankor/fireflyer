import re
from html import escape
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2
import polars as pl
import yaml

from fireflyer import filters as filters_mod
from fireflyer import calcs as calcs_mod
from fireflyer import inline_data
from fireflyer.scan import scan
from fireflyer.chart.bar.chart import Bar
from fireflyer.chart.map.chart import Map
from fireflyer.chart.number.chart import Number
from fireflyer.chart.pie.chart import Pie
from fireflyer.chart.table.chart import Table

# 1 height unit in the layout DSL maps to this many CSS pixels.
HEIGHT_UNIT_PX = 8

# `type:` value in chart configs maps to the chart's Python class.
CHART_TYPES: dict[str, type] = {
    "table": Table, "pie": Pie, "bar": Bar, "map": Map, "number": Number,
}

# Endpoint and target used by chart-level crossfilter clicks (pie slices today).
# Kept here, not in chart code, so charts stay unaware of dashboard wiring.
CROSSFILTER_ENDPOINT = "/dashboard"
CROSSFILTER_TARGET = "#fireflyer-dashboard"
CROSSFILTER_INCLUDE = "#fireflyer-dashboard input[type=hidden]"

# A chart's own grain/window controls change only that chart, so they re-render
# just their cell instead of the whole dashboard. (Crossfilter still goes wide —
# it changes every chart's data.) `closest` lets the chart address its cell
# without knowing an id.
CELL_ENDPOINT = "/dashboard/cell"
CELL_TARGET = "closest .fireflyer-dashboard-cell"

# The page-level grain state, wrapped so a cell-scoped response can rewrite it
# out-of-band — see `grain_state_html`.
GRAIN_STATE_ID = "ff-grain-state"


def grain_state_html(tokens: list[str]) -> str:
    """The dashboard's grain hidden inputs as an **out-of-band** swap.

    A chart-local re-render replaces only its own cell, so the page-level state
    it just changed has to be written back separately — otherwise the next
    request would still carry the old token and undo the change."""
    inputs = "".join(
        f'<input type="hidden" name="grain" value="{escape(tok, quote=True)}">'
        for tok in tokens
    )
    return f'<span id="{GRAIN_STATE_ID}" hx-swap-oob="true">{inputs}</span>' 


def set_grain_token(tokens: list[str], token: str) -> list[str]:
    """Apply one `"<chart id>|<grain>[|<offset>]"` pick to the token list,
    replacing any previous pick for that chart. A bare `"by_day|"` drops the
    entry, putting the chart back on its automatic grain and default window.

    One token carries both parts because they're one idea — how *this viewer* is
    looking at this chart's time axis right now. It lives beside the crossfilter
    tokens (hidden inputs on the dashboard, same round-trip) rather than in the
    YAML, which is the dashboard's definition and shared by everyone."""
    cid, _, rest = token.partition("|")
    kept = [t for t in tokens if t.partition("|")[0] != cid]
    return kept + [token] if rest else kept


def view_for(tokens: list[str], cid: str) -> tuple[str, int | None]:
    """`(grain, window offset)` for a chart. An empty grain means automatic; a
    None offset means the default window (the most recent bars). Either part can
    be set without the other — moving the window keeps an automatic grain."""
    for token in tokens:
        owner, _, rest = token.partition("|")
        if owner == cid:
            grain, _, offset = rest.partition("|")
            return grain, int(offset) if offset.isdigit() else None
    return "", None


_DIR = Path(__file__).parent
_CSS = (_DIR / "dashboard.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "dashboard.html").read_text(),
    autoescape=True,
)
# The async serving path emits a skeleton (cell placeholders that hx-trigger
# on load) instead of pre-rendering every chart in one request. Each cell's
# placeholder POSTs to /dashboard/cell, which returns the per-cell template
# below. to_html() keeps using the full template for _repr_html_/tests.
_SKELETON_TEMPLATE = jinja2.Template(
    (_DIR / "skeleton.html").read_text(),
    autoescape=True,
)
_CELL_TEMPLATE = jinja2.Template(
    (_DIR / "cell.html").read_text(),
    autoescape=True,
)

_ROW_HEIGHT_RE = re.compile(r"^@(\d+(?:\.\d+)?)$")
# A widget token is `<chart_id>` or `<chart_id>:<width>`. The width is optional
# and defaults to 1. A bare id whose chart also sits directly above **inherits**
# that chart's size and spans down (see `_resolve_group`); any other cell fills
# the leftover space.
_WIDGET_RE = re.compile(r"^([A-Za-z_][\w-]*)(?::(\d+(?:\.\d+)?))?$")


# Explicit theme override for a render. `None`/`"auto"` emit no attribute, so the
# output follows the viewer's OS preference (prefers-color-scheme); `"dark"` or
# `"light"` stamp `data-ff-theme` on the dashboard root to force one palette.
_THEMES = ("dark", "light")


def _normalize_theme(theme: str | None) -> str:
    return theme if theme in _THEMES else ""


class DashboardError(ValueError):
    """Raised for any invalid dashboard YAML — message is shown to the user."""


@dataclass
class _Row:
    height: float
    # (chart_id, width) — width is None for a bare token (inherit-if-above, else 1).
    cells: list[tuple[str, float | None]]
    # 0-based position among row-arrays in the `layout:` list. The editor's
    # resize handles use this to find which `@<height>` token to rewrite.
    ordinal: int = -1


@dataclass
class _Header:
    text: str


@dataclass
class _Separator:
    pass


@dataclass
class _Tab:
    """One named tab: a section of the layout. A dashboard is either a flat list
    of items (`tabs is None`) or a list of these. A tab owns a run of layout
    items; its `items` are already grouped `_RowGroup`/`_Header`/`_Separator`."""
    name: str
    items: list


@dataclass
class _ChartConfig:
    """Validated chart config; instantiated per render so crossfilters can merge in."""
    cls: type
    kwargs: dict


@dataclass
class _Placement:
    """One chart slot inside a `_RowGroup` grid."""
    chart_id: str
    grid_column: str        # CSS grid-column, e.g. "1" or "1 / span 2"
    grid_row: str           # CSS grid-row value, e.g. "1" or "1 / span 2"


@dataclass
class _RowGroup:
    """One or more consecutive rows with identical column structure rendered
    as a single CSS grid. Merged charts (same id repeated at the same slot)
    become a single placement with `row_span > 1`."""
    columns_css: str        # e.g. "60fr 40fr"
    rows_css: str           # e.g. "320px 240px"
    placements: list[_Placement]
    # One entry per grid track, in order: (yaml_row_ordinal, height_units).
    # Drives the editor's resize handles — each track maps to one `@<height>`.
    tracks: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class Dashboard:
    chart_configs: dict[str, _ChartConfig]
    # Calcs, keyed by dataset name -> CalcSet. Parsed from the top-level
    # `calcs:` block; a chart resolves its `calc:` within its dataset's set.
    calc_sets: dict[str, Any] = field(default_factory=dict)
    items: list[Any] = field(default_factory=list)
    # None for a flat (list-form) dashboard; a list of `_Tab` when the
    # `layout:` section is a mapping of tab name -> layout list.
    tabs: list[_Tab] | None = None
    yaml_source: str = ""
    # Optional top-level `name:` — the dashboard's display name, part of the
    # definition (not portal metadata). Portal lists dashboards by it; local
    # mode just carries it. Empty when the key is absent.
    name: str = ""

    # Dataset resolver: name -> (uri, storage_options). Set by `from_yaml` from
    # the `datasets` argument (a DatasetStore or a callable); None means a
    # chart's `dataset` is a Parquet path/URI directly. Not a dataclass field.
    _resolve = None

    @classmethod
    def from_yaml(cls, text: str, datasets=None) -> "Dashboard":
        """Parse a dashboard. `datasets` resolves chart dataset *names* to their
        stored Parquet — a `DatasetStore` (uses `.source`) or a
        `name -> (uri, storage_options)` callable. Omitted (validation-only or
        standalone), a chart's `dataset` is taken as a Parquet path/URI."""
        try:
            config = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise DashboardError(f"invalid YAML: {exc}") from exc

        if not isinstance(config, dict):
            raise DashboardError(
                "top-level must be a mapping with keys: name, charts, layout"
            )
        for key in ("charts", "layout", "name"):
            if key not in config:
                raise DashboardError(f"missing top-level key: {key!r}")

        name = config["name"]
        if not isinstance(name, str) or not name.strip():
            raise DashboardError("top-level `name` must be a non-empty string")

        try:
            inline = inline_data.parse_block(config.get("datasets"))
        except inline_data.InlineDataError as exc:
            raise DashboardError(str(exc)) from exc
        resolve = _resolver_from(datasets, inline)
        chart_configs = _parse_charts(config["charts"])
        try:
            calc_sets = calcs_mod.parse_block(config.get("calcs"))
        except calcs_mod.CalcError as exc:
            raise DashboardError(str(exc)) from exc
        _validate_calc_refs(chart_configs, calc_sets)

        raw_layout = config["layout"]
        if isinstance(raw_layout, dict):
            tabs = _parse_tabs(raw_layout, chart_configs)
            # A chart resolves to exactly one placement across the whole
            # dashboard (span-aware), so the move machinery — which pulls a
            # chart from every row it's in — stays sound across tabs.
            _validate_unique_placements([g for t in tabs for g in t.items])
            dash = cls(chart_configs=chart_configs, calc_sets=calc_sets,
                       tabs=tabs, yaml_source=text, name=name)
        else:
            items = _parse_layout(raw_layout, chart_configs)
            items = _group_layout(items)
            _validate_unique_placements(items)
            dash = cls(chart_configs=chart_configs, calc_sets=calc_sets,
                       items=items, yaml_source=text, name=name)
        dash._resolve = resolve
        return dash

    @staticmethod
    def dataset_names(text: str) -> set[str]:
        """The **managed** dataset names a dashboard references (its charts'
        `dataset:` values). Best-effort — empty set if the YAML doesn't parse.
        Powers the portal's delete-guard and cascade-rename.

        Names defined by the dashboard's own inline `datasets:` block are
        excluded: those charts read data carried in the file, so they neither
        pin a stored dataset against deletion nor want renaming when one moves.
        """
        try:
            dash = Dashboard.from_yaml(text)
            inline = inline_data.parse_block(yaml.safe_load(text).get("datasets"))
        except (DashboardError, inline_data.InlineDataError, AttributeError,
                yaml.YAMLError):
            return set()
        return {
            cfg.kwargs.get("dataset")
            for cfg in dash.chart_configs.values()
            if cfg.kwargs.get("dataset") and cfg.kwargs["dataset"] not in inline
        }

    def _tab_context(self, active_tab: int):
        """(tabs_meta | None, clamped_active, active_items).

        `tabs_meta` is a list of `{name, index}` for the tab bar, or None for a
        flat dashboard. `active_items` are the grouped items to render — the
        active tab's, or the whole flat list."""
        if self.tabs is None:
            return None, 0, self.items
        active = active_tab if 0 <= active_tab < len(self.tabs) else 0
        meta = [{"name": t.name, "index": i} for i, t in enumerate(self.tabs)]
        return meta, active, self.tabs[active].items

    def to_html(
        self,
        cf_tokens: list[str] | None = None,
        active_tab: int = 0,
        theme: str | None = None,
        grain_tokens: list[str] | None = None,
    ) -> str:
        cf_tokens = list(cf_tokens or [])
        grain_tokens = list(grain_tokens or [])
        tabs_meta, active_tab, items = self._tab_context(active_tab)
        rendered = [self._render_item(item, cf_tokens, grain_tokens) for item in items]
        return _TEMPLATE.render(
            css=_CSS,
            items=rendered,
            tabs=tabs_meta,
            active_tab=active_tab,
            yaml_source=self.yaml_source,
            cf_tokens=cf_tokens,
            grain_tokens=grain_tokens,
            ff_theme=_normalize_theme(theme),
        )

    def render_skeleton(
        self,
        cf_tokens: list[str] | None = None,
        editing: bool = False,
        active_tab: int = 0,
        theme: str | None = None,
        grain_tokens: list[str] | None = None,
    ) -> str:
        """Layout-only render: cells are placeholders that hx-trigger on load.

        Used by /execute and /dashboard so the response is small and charts
        render in parallel via /dashboard/cell. The skeleton still embeds the
        YAML + current cf tokens as hidden inputs; cells include those via
        hx-include when they fetch themselves.

        `editing=True` (set by the web editor) adds a drag handle at the bottom
        of every row track so the user can resize rows; the deployable render
        leaves it off, so handles never ship outside the editor.

        When the dashboard is tabbed only the active tab's items are emitted
        (htmx lazy-loads a tab's charts on switch), but the header/item counters
        advance across **all** tabs so the emitted `before`/`index` values stay
        document-global — matching how `config_edit` scans the YAML.
        """
        cf_tokens = list(cf_tokens or [])
        grain_tokens = list(grain_tokens or [])
        tabs_meta, active_tab, _ = self._tab_context(active_tab)
        # Number headers in document order so the editor can double-click one to
        # rename it and the server can find the matching header line. `before` is
        # each item's index in the full layout-item list (rows + headers +
        # separators), so the add-row strips can target any gap — including around
        # a header/separator. A row group spans several yaml rows, so it advances
        # the counter by its track count.
        items = []
        header_i = 0
        item_i = 0
        end_before = "end"
        tab_iter = (
            [(0, self.items)] if self.tabs is None
            else [(i, t.items) for i, t in enumerate(self.tabs)]
        )
        for ti, tab_items in tab_iter:
            for item in tab_items:
                rendered = self._render_skeleton_item(item)
                rendered["before"] = item_i
                if rendered["kind"] == "header":
                    rendered["index"] = header_i
                    header_i += 1
                    item_i += 1
                elif rendered["kind"] == "separator":
                    item_i += 1
                else:  # row group — one yaml item per track
                    item_i += len(item.tracks)
                if self.tabs is None or ti == active_tab:
                    items.append(rendered)
            if self.tabs is not None and ti == active_tab:
                # The active tab's trailing "+" strip appends after its last
                # item; that's the next tab's first item index (or "end" when
                # this is the last tab) so the new row lands in the right tab.
                end_before = item_i if ti < len(self.tabs) - 1 else "end"
        return _SKELETON_TEMPLATE.render(
            css=_CSS,
            items=items,
            tabs=tabs_meta,
            active_tab=active_tab,
            end_before=end_before,
            yaml_source=self.yaml_source,
            cf_tokens=cf_tokens,
            grain_tokens=grain_tokens,
            editing=editing,
            ff_theme=_normalize_theme(theme),
        )

    def render_cell(
        self,
        cid: str,
        cf_tokens: list[str] | None = None,
        col: str = "1",
        row: str = "1",
        editing: bool = False,
        grain_tokens: list[str] | None = None,
        legend_page: int = 0,
        table_page: int = 1,
        table_query: str = "",
    ) -> str:
        """Render a single dashboard cell (indicator + chart). Used by
        /dashboard/cell — the response replaces the placeholder in place.

        `col` / `row` are CSS grid placement values threaded through from the
        skeleton's hx-vals so the rendered cell lands in the same grid slot
        (including merged spans like `"1 / span 2"`)."""
        cf_tokens = list(cf_tokens or [])
        if cid not in self.chart_configs:
            raise DashboardError(f"unknown chart {cid!r}")
        result = self._render_chart(
            cid, cf_tokens, list(grain_tokens or []),
            col=col, row=row, legend_page=legend_page,
            table_page=table_page, table_query=table_query,
        )
        return _CELL_TEMPLATE.render(
            chart_html=result["html"],
            applied=result["filters"],
            emitted=result["emitted"],
            col_labels=result["col_labels"],
            col=col,
            row=row,
            editing=editing,
            cid=cid,
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()

    def _render_item(self, item, cf_tokens: list[str], grain_tokens: list[str]) -> dict:
        if isinstance(item, _Header):
            return {"kind": "header", "text": item.text}
        if isinstance(item, _Separator):
            return {"kind": "separator"}
        # _RowGroup
        return {
            "kind": "row",
            "columns_css": item.columns_css,
            "rows_css": item.rows_css,
            "cells": [
                {
                    "grid_column": p.grid_column,
                    "grid_row": p.grid_row,
                    **self._render_chart(
                        p.chart_id, cf_tokens, grain_tokens,
                        col=p.grid_column, row=p.grid_row,
                    ),
                }
                for p in item.placements
            ],
        }

    def _render_skeleton_item(self, item) -> dict:
        if isinstance(item, _Header):
            return {"kind": "header", "text": item.text}
        if isinstance(item, _Separator):
            return {"kind": "separator"}
        return {
            "kind": "row",
            "columns_css": item.columns_css,
            "rows_css": item.rows_css,
            "cells": [
                {
                    "cid": p.chart_id,
                    "grid_column": p.grid_column,
                    "grid_row": p.grid_row,
                }
                for p in item.placements
            ],
            # One handle per grid track. `track` is the 1-based CSS grid row the
            # handle sits at the bottom of; `ordinal` is the YAML row it edits.
            "tracks": [
                {"track": i + 1, "ordinal": ordinal, "units": units}
                for i, (ordinal, units) in enumerate(item.tracks)
            ],
            # Column resize: a vertical handle sits on each interior column
            # boundary, spanning every track. `ordinals` are all YAML rows in
            # the group (they share one column structure, so a width drag must
            # rewrite each of them); `col_count` drives how many handles.
            "ordinals": ",".join(str(o) for o, _ in item.tracks),
            "col_count": len(item.columns_css.split()),
        }

    def _render_chart(
        self,
        cid: str,
        cf_tokens: list[str],
        grain_tokens: list[str],
        col: str = "1",
        row: str = "1",
        legend_page: int = 0,
        table_page: int = 1,
        table_query: str = "",
    ) -> dict:
        # Re-instantiate every render so merged filters reflect the current
        # crossfilter state. Cheap — chart classes are dataclasses, real work
        # happens in to_html(). Tokens from this chart itself are dropped so
        # the source chart stays unfiltered while showing its own selection.
        cfg = self.chart_configs[cid]
        kwargs = dict(cfg.kwargs)
        declared = filters_mod.normalize(kwargs.pop("filters", None))
        cross = filters_mod.decode_tokens(cf_tokens, exclude_emitter=cid)
        merged = declared + cross
        kwargs["filters"] = [f.as_dict() for f in merged]
        chart = cfg.cls(**kwargs)
        chart._resolve = self._resolve  # name -> Parquet source at read time
        # Calcs for this chart's dataset, so a `calc:` key resolves.
        chart._calcs = self.calc_sets.get(kwargs["dataset"])

        refresh = {
            "endpoint": CELL_ENDPOINT,
            "target": CELL_TARGET,
            "include": CROSSFILTER_INCLUDE,
            "cid": cid,
            "col": col,
            "row": row,
        }
        try:
            if isinstance(chart, Pie):
                active = filters_mod.active_values_for(cf_tokens, cid, chart.column)
                chart_html = chart.to_html(
                    crossfilter={
                        "endpoint": CROSSFILTER_ENDPOINT,
                        "target": CROSSFILTER_TARGET,
                        "include": CROSSFILTER_INCLUDE,
                        "emitter": cid,
                        "active": active,
                    },
                    legend_page=legend_page,
                    refresh=refresh,
                )
            elif isinstance(chart, Bar):
                active = filters_mod.active_values_for(cf_tokens, cid, chart.y)
                grain, offset = view_for(grain_tokens, cid)
                # Whole tokens, so a segment lights only for its own (x, y) cell.
                selected = filters_mod.tokens_for(cf_tokens, cid)
                chart_html = chart.to_html(
                    crossfilter={
                        "endpoint": CROSSFILTER_ENDPOINT,
                        "target": CROSSFILTER_TARGET,
                        "include": CROSSFILTER_INCLUDE,
                        "emitter": cid,
                        "active": active,
                        "selected": selected,
                    },
                    grain=grain,
                    offset=offset,
                    legend_page=legend_page,
                    refresh=refresh,
                )
            elif isinstance(chart, Table):
                # The table's own search/pagination re-render this cell rather than
                # calling /chart/table: only the dashboard holds the `calcs:` block,
                # so a chart rebuilt from URL params alone would lose its measures.
                # Whole tokens, so a row lights only for its own combination of
                # dimension values — the same rule the bar's segments follow.
                chart_html = chart.to_html(
                    page=table_page,
                    query=table_query,
                    refresh=refresh,
                    crossfilter={
                        "endpoint": CROSSFILTER_ENDPOINT,
                        "target": CROSSFILTER_TARGET,
                        "include": CROSSFILTER_INCLUDE,
                        "emitter": cid,
                        "selected": filters_mod.tokens_for(cf_tokens, cid),
                    },
                )
            else:
                chart_html = chart.to_html()
        except Exception as exc:
            # A chart that cannot read its data must not take the page down with
            # it. The cell arrives by htmx, which only swaps a 2xx response, so an
            # exception here left the placeholder spinning forever with the reason
            # visible only in the server log. The usual cause is a dashboard that
            # has outrun its dataset — a calc naming a column the data no longer
            # has — which is precisely the thing someone can fix once they can see
            # it. Every other cell still renders.
            chart_html = _chart_error_html(cid, exc)

        # Indicator state for the dashboard cell, computed here so chart code
        # stays unaware of it:
        #   - `filters`: filters from other charts narrowing this chart (blue)
        #   - `emitted`: filters this chart is emitting itself (red)
        # When both apply (chart is both source and downstream of others),
        # the template prefers the emitter state — the user typically wants
        # to know which chart is causing the cascade.
        applied = _applied_filters_for(
            merged, kwargs["dataset"], self._resolve, chart._calcs
        )
        emitted = filters_mod.emitted_by(cf_tokens, cid)
        # Display names for the columns those filters name, so a relabelled
        # column reads the same in the indicator as it does in the chart. Keyed
        # by the real column name, which is what a Filter carries.
        labels = {}
        if chart._calcs is not None:
            for f in [*applied, *emitted]:
                labels[f.column] = chart._calcs.column_label(f.column)
        return {
            "html": chart_html,
            "filters": applied,
            "emitted": emitted,
            "col_labels": labels,
        }


def _chart_error_html(cid: str, exc: Exception) -> str:
    """A chart that failed to render, as a card in its own cell.

    Only the first line of the message is kept: Polars appends the resolved
    query plan to its errors, which is pages long and says nothing a reader of
    the dashboard can act on. The first line is the part that does — e.g.
    `unable to find column "qty"; valid columns: [...]`.
    """
    first_line = str(exc).strip().splitlines()
    detail = first_line[0] if first_line else type(exc).__name__
    return (
        '<article class="fireflyer-chart-error" role="alert">'
        f'<div class="fireflyer-chart-error-head">{escape(cid)} failed to render</div>'
        f'<div class="fireflyer-chart-error-msg">{escape(detail)}</div>'
        "</article>"
    )


def _applied_filters_for(
    filters: list[filters_mod.Filter], dataset: str, resolve, calcs=None
) -> list[filters_mod.Filter]:
    """Filters that actually narrow this chart's data — column must exist.

    `calcs` matters: a filter can name a **column calc**, which only exists once
    the calc set is attached to the scan. Reading the bare Parquet schema would
    report such a filter as not applied even though the chart really is filtered
    by it — the indicator would then contradict the chart.
    """
    if not filters:
        return []
    # `scan(...).collect_schema()` reads only the Parquet footer/schema, not the
    # data. The chart's own to_html scans it too; a second schema read is cheap.
    columns = scan(dataset, resolve, calcs).collect_schema().names()
    return [f for f in filters if f.column in columns]


def _group_layout(items: list) -> list:
    """Group consecutive rows linked by a bare-inherit span into `_RowGroup`s.

    A lower row joins the group above it when it repeats a chart from the row
    directly above as a **bare** cell (no width) — that chart spans down. Rows
    with no such link render as independent single-row grids. Callers run
    `_validate_unique_placements` afterwards (across all tabs when tabbed).
    """
    output: list = []
    i = 0
    while i < len(items):
        item = items[i]
        if not isinstance(item, _Row):
            output.append(item)
            i += 1
            continue
        # Extend the group while each next row bare-inherits a chart from the
        # row directly above it (that is what makes a chart span the rows).
        j = i + 1
        while j < len(items):
            nxt = items[j]
            if not isinstance(nxt, _Row):
                break
            above = {cid for cid, _ in items[j - 1].cells}
            if not any(w is None and cid in above for cid, w in nxt.cells):
                break
            j += 1
        output.append(_resolve_group(items[i:j]))
        i = j

    return output


def _fill(fillers, lo, hi, ri, placements, new_active) -> None:
    """Lay `fillers` — each `(cid, width|None)` — contiguously across `[lo, hi]`,
    subdividing by width (a bare cell counts as 1). Appends new placements."""
    if not fillers:
        return
    if hi - lo <= 1e-9:
        raise DashboardError(
            f"chart {fillers[0][0]!r}: no room to place it — a spanning chart "
            "already fills that space"
        )
    widths = [(w if w is not None else 1.0) for _, w in fillers]
    span_total = sum(widths) or 1.0
    pos = lo
    for (cid, _), w in zip(fillers, widths):
        seg = (w / span_total) * (hi - lo)
        p = {"cid": cid, "start": pos, "end": pos + seg, "row": ri, "rowspan": 1}
        placements.append(p)
        new_active[cid] = p
        pos += seg


def _resolve_group(rows: list) -> _RowGroup:
    """Resolve a run of rows into placements. The first row's widths set the
    coordinate system `[0, total]`; a bare cell matching the chart directly above
    inherits its column and extends its row-span; every other cell fills the
    leftover space, subdividing it by its width."""
    total = sum((w if w is not None else 1.0) for _, w in rows[0].cells) or 1.0
    placements: list[dict] = []
    active: dict[str, dict] = {}   # cid -> placement from the row directly above

    for ri, row in enumerate(rows):
        above = active
        new_active: dict[str, dict] = {}
        seen: set[str] = set()
        cursor = 0.0
        pending: list[tuple[str, float | None]] = []
        for cid, w in row.cells:
            if cid in seen:
                raise DashboardError(
                    f"chart {cid!r}: used twice in the same row")
            seen.add(cid)
            if w is None and cid in above:            # inherited -> spans down
                p = above[cid]
                if p["start"] < cursor - 1e-9:
                    raise DashboardError(
                        f"chart {cid!r}: spanned column is out of order; keep "
                        "cells left-to-right")
                _fill(pending, cursor, p["start"], ri, placements, new_active)
                pending = []
                p["rowspan"] += 1
                new_active[cid] = p
                cursor = p["end"]
            else:                                      # a leftover filler
                pending.append((cid, w))
        _fill(pending, cursor, total, ri, placements, new_active)
        active = new_active

    return _finish_group(rows, placements, total)


def _finish_group(rows: list, placements: list[dict], total: float) -> _RowGroup:
    """Turn resolved [start,end] ranges into a `_RowGroup`: the fine column grid
    is the union of all boundaries; each cell spans the fine columns it covers."""
    edges = sorted(
        {round(p["start"], 6) for p in placements}
        | {round(p["end"], 6) for p in placements}
        | {0.0, round(total, 6)}
    )
    col_w = [edges[k + 1] - edges[k] for k in range(len(edges) - 1)]
    columns_css = " ".join(f"{w:g}fr" for w in col_w)
    rows_css = " ".join(f"{int(r.height * HEIGHT_UNIT_PX)}px" for r in rows)

    out: list[_Placement] = []
    for p in placements:
        c0 = edges.index(round(p["start"], 6))
        cspan = edges.index(round(p["end"], 6)) - c0
        grid_column = f"{c0 + 1}" if cspan <= 1 else f"{c0 + 1} / span {cspan}"
        rspan = p["rowspan"]
        grid_row = f"{p['row'] + 1}" if rspan <= 1 else f"{p['row'] + 1} / span {rspan}"
        out.append(_Placement(p["cid"], grid_column, grid_row))

    return _RowGroup(
        columns_css=columns_css,
        rows_css=rows_css,
        placements=out,
        tracks=[(r.ordinal, r.height) for r in rows],
    )


def _validate_unique_placements(items: list) -> None:
    """A chart id must resolve to exactly one placement (a span is one
    placement). Any other repeat — an explicit-width repeat, or a non-contiguous
    one — leaves two placements and errors here."""
    counts: dict[str, int] = {}
    for item in items:
        if isinstance(item, _RowGroup):
            for p in item.placements:
                counts[p.chart_id] = counts.get(p.chart_id, 0) + 1
    dups = sorted(cid for cid, n in counts.items() if n > 1)
    if dups:
        raise DashboardError(
            f"chart {dups[0]!r}: used more than once — a chart may appear again "
            "only as a bare cell in the row directly below (a contiguous span)"
        )


def rename_dataset_ref(text: str, old: str, new: str) -> str:
    """Rewrite chart `dataset: <old>` references to `<new>` in a dashboard's
    YAML text (cascade-rename). Word-boundary lookahead so `orders` doesn't
    match `orders_2`; leaves unrelated text (comments, layout) untouched."""
    return re.sub(
        r"(dataset:[ \t]*)" + re.escape(old) + r"(?=[\s,}])",
        r"\g<1>" + new,
        text,
    )


def _resolver_from(datasets, inline: dict[str, str] | None = None):
    """Turn the `from_yaml` `datasets` argument into a name -> (uri, options)
    callable, or None (chart `dataset` is a Parquet path/URI).

    A dashboard's own inline `datasets:` block is consulted **first**: it is
    part of the file being rendered, so it should win over a managed dataset
    that happens to share its name rather than silently reading someone else's
    data. Anything it doesn't define falls through to the store, and with no
    store at all the name is treated as a path, exactly as before.
    """
    base = None
    if datasets is not None:
        if hasattr(datasets, "source"):  # a DatasetStore
            base = datasets.source
        elif callable(datasets):
            base = datasets
        else:
            raise DashboardError(
                "datasets must be a DatasetStore or a resolver callable"
            )
    return inline_data.resolver(inline, base)


def _parse_charts(raw) -> dict[str, _ChartConfig]:
    if not isinstance(raw, dict):
        raise DashboardError("`charts` must be a mapping of id -> config")
    out = {}
    for cid, cfg in raw.items():
        if not isinstance(cfg, dict):
            raise DashboardError(f"chart {cid!r}: must be a mapping")
        params = dict(cfg)
        ctype = params.pop("type", None)
        if ctype not in CHART_TYPES:
            raise DashboardError(
                f"chart {cid!r}: unknown type {ctype!r} "
                f"(expected one of {sorted(CHART_TYPES)})"
            )
        # `dataset` is a dataset name (resolved to Parquet at render time), no
        # longer a ref into a top-level `datasets:` block.
        dataset_ref = params.pop("dataset", None)
        if not isinstance(dataset_ref, str) or not dataset_ref:
            raise DashboardError(f"chart {cid!r}: missing `dataset` (a dataset name)")
        kwargs = {"dataset": dataset_ref, **params}
        # Smoke-instantiate so YAML errors surface at parse time, not render time.
        try:
            CHART_TYPES[ctype](**kwargs)
        except (TypeError, ValueError) as exc:
            # ValueError covers FilterError plus per-chart param validation
            # (e.g. the number chart's unknown-`agg` guard).
            raise DashboardError(f"chart {cid!r}: {exc}") from exc
        out[cid] = _ChartConfig(cls=CHART_TYPES[ctype], kwargs=kwargs)
    return out


def _validate_calc_refs(
    chart_configs: dict[str, _ChartConfig], calc_sets: dict
) -> None:
    """Every chart `calc:` key must exist in its dataset's calc set, and name a
    value rather than a dimension. A chart with no `calc` (or an inline dict)
    needs no lookup — it defaults to a row count. Caught here so a typo'd key
    fails at parse time, not render."""
    for cid, cfg in chart_configs.items():
        dataset = cfg.kwargs["dataset"]
        # A chart's single `calc:`, plus the table's `measures:` list — same
        # rules for both: the key must exist and must name a value.
        keys = [cfg.kwargs.get("calc")]
        keys += list(cfg.kwargs.get("measures") or [])
        for calc in keys:
            if not isinstance(calc, str) or not calc:
                continue
            cset = calc_sets.get(dataset)
            if cset is None or calc not in cset:
                raise DashboardError(
                    f"chart {cid!r}: unknown calc {calc!r} for dataset "
                    f"{dataset!r} — define it under `calcs: {dataset}:`"
                )
            if cset.get(calc).is_column:
                raise DashboardError(
                    f"chart {cid!r}: {calc!r} is a column calc — a dimension, not a "
                    "value. Use it as the chart's `x`/`column` instead"
                )


def _parse_tabs(raw: dict, chart_configs: dict[str, _ChartConfig]) -> list[_Tab]:
    """Parse the mapping form of `layout:` (tab name -> layout list) into
    `_Tab`s. Row ordinals and — via the render pass — header/item indices run
    **globally** in document order across tabs, matching the way `config_edit`
    scans the whole `layout:` section by text."""
    if not raw:
        raise DashboardError("`dashboard` mapping must have at least one tab")
    tabs: list[_Tab] = []
    ordinal = 0
    for name, layout in raw.items():
        if not isinstance(layout, list) or not layout:
            raise DashboardError(
                f"tab {str(name)!r}: must contain at least one layout item"
            )
        items = _parse_layout(layout, chart_configs, start_ordinal=ordinal)
        ordinal += sum(1 for it in items if isinstance(it, _Row))
        tabs.append(_Tab(name=str(name), items=_group_layout(items)))
    return tabs


def _parse_layout(
    raw, chart_configs: dict[str, _ChartConfig], start_ordinal: int = 0
) -> list:
    if not isinstance(raw, list):
        raise DashboardError("`dashboard` must be a list of layout items")
    items = [_parse_item(item, i, chart_configs) for i, item in enumerate(raw)]
    # Number rows by document order so the editor can map a resize handle back
    # to the matching `@<height>` token (the Nth height token in the YAML).
    # `start_ordinal` continues the count across tabs so a lower tab's rows keep
    # unique, document-order ordinals.
    ordinal = start_ordinal
    for item in items:
        if isinstance(item, _Row):
            item.ordinal = ordinal
            ordinal += 1
    return items


def _parse_item(raw, index: int, chart_configs: dict[str, _ChartConfig]):
    if raw == "-":
        return _Separator()
    if isinstance(raw, str):
        return _Header(text=raw)
    if isinstance(raw, list):
        return _parse_row(raw, index, chart_configs)
    raise DashboardError(f"layout item #{index}: unsupported shape {raw!r}")


def _parse_row(raw: list, index: int, chart_configs: dict[str, _ChartConfig]) -> _Row:
    if not raw:
        raise DashboardError(f"row #{index}: empty row")
    head = raw[0]
    height_match = _ROW_HEIGHT_RE.match(head) if isinstance(head, str) else None
    if not height_match:
        raise DashboardError(
            f"row #{index}: first element must be '@<height>' (got {head!r})"
        )
    height = float(height_match.group(1))
    if height <= 0:
        raise DashboardError(f"row #{index}: height must be > 0")

    # Widths are proportions (relative weights, rendered as `fr` tracks) and are
    # **optional** — a bare `id` counts as `id:1`, and a bare id equal to the
    # chart directly above inherits its size and spans down. `a:1 b:4` is the
    # same 20/80 split as `a:20 b:80`; there is no sum requirement.
    cells: list[tuple[str, float | None]] = []
    for token in raw[1:]:
        if not isinstance(token, str):
            raise DashboardError(
                f"row #{index}: widget tokens must be strings, got {token!r}"
            )
        match = _WIDGET_RE.match(token)
        if not match:
            raise DashboardError(
                f"row #{index}: bad widget token {token!r} "
                "(expected '<id>' or '<id>:<width>')"
            )
        chart_id, raw_width = match.group(1), match.group(2)
        if chart_id not in chart_configs:
            raise DashboardError(f"row #{index}: unknown chart {chart_id!r}")
        width = float(raw_width) if raw_width is not None else None
        if width is not None and width <= 0:
            raise DashboardError(f"row #{index}: width for {chart_id!r} must be > 0")
        cells.append((chart_id, width))

    if not cells:
        raise DashboardError(f"row #{index}: must contain at least one widget")
    return _Row(height=height, cells=cells)
