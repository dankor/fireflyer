from dataclasses import dataclass, field
from pathlib import Path

import jinja2
import polars as pl

from fireflyer import filters as filters_mod
from fireflyer import calcs as calcs_mod
from fireflyer.params import (
    CalcParam,
    ChoiceParam,
    ColumnParam,
    DatasetParam,
    FilterListParam,
    IntParam,
    TextParam,
)
from fireflyer.scan import scan

# Categorical palette mirroring the pie chart's so the same `y` value gets the
# same color across charts on a dashboard. Per the per-chart-CSS policy each
# chart owns its palette; duplication is intentional.
COLORS = [
    "#1FA8C9",
    "#454E7C",
    "#5AC189",
    "#FF7F44",
    "#666666",
    "#E04355",
    "#FCC700",
    "#A868B7",
    "#3CCCCB",
    "#A38F79",
]

# SVG plot geometry. CHART_W/PLOT_W are the *default* (few-category) sizes; with
# many categories the plot (and the viewBox) grow so bars keep a minimum width
# and the canvas scrolls horizontally. Must match the viewBox in chart.html.
CHART_W = 380
CHART_H = 260
PLOT_X = 32
PLOT_Y = 16
PLOT_W = 332
PLOT_H = 190
BAR_GAP = 10
# Right margin kept when the plot grows (viewBox width = PLOT_X + plot_w + this).
RIGHT_MARGIN = CHART_W - PLOT_X - PLOT_W
# A bar renders at least this wide (viewBox units ≈ px at the scroll threshold);
# once `n` bars need more than the default plot, it grows and the canvas scrolls.
MIN_BAR_W = 28
# Each bar owns a slot — its own width plus one gap. Geometry is derived from the
# slot rather than from a fixed gap, so the gap shrinks along with the bar once
# the plot hits its ceiling. (A fixed gap used to survive the cap and eat the
# whole plot: 400 bars needed 3990px of gaps inside a 2000px plot, which made
# every bar *negative* wide and rendered nothing.)
SLOT_W = MIN_BAR_W + BAR_GAP
# Share of a slot given to the gap once slots are squeezed below SLOT_W.
GAP_RATIO = BAR_GAP / SLOT_W
# Ceiling on the scrollable plot — roughly 400 bars at full width. Past this the
# bars thin out rather than the canvas growing without bound.
MAX_PLOT_W = 16000
# Plot width whenever the chart is shown whole rather than scrolled. **Fixed**,
# so the viewBox aspect (~2.5:1) is the same for two bars as for thirty: the
# drawing then scales identically whatever the bar count, and switching grain
# doesn't resize the chart or change its height. Growing it one slot per bar
# instead gave a 9:1 letterbox at 30 bars and a 1.5:1 box at 2, so every grain
# switch redrew at a different size.
FIT_PLOT_W = 600
# A bar never gets wider than this, so a two-bar year view spreads out instead of
# rendering two billboards across the whole plot.
MAX_BAR_W = 64

# How many bars the automatic grain aims to stay under — roughly what a wide
# cell shows without much scrolling. Deliberately independent of MAX_PLOT_W:
# that one is the ceiling for a *manually* picked grain, where scrolling through
# a long axis is the whole point.
MAX_BARS = 52

# Which axis the categories run along. `horizontal` is the default and is what
# the chart has always drawn: categories left-to-right, bars growing up.
DIRECTIONS = ("horizontal", "vertical")

# Time grains a temporal x-axis can bucket into, **finest first** (the order
# `_pick_grain` walks): the Polars `dt.truncate` unit, the strftime pattern for
# the axis label, and the picker's segment letter. Day is the floor — a bar per
# hour is a different chart, not a grain of this one. Quarters have no strftime
# code, so `_label_expr` composes them by hand.
GRAINS = (
    ("day", "1d", "%Y-%m-%d", "D"),
    ("week", "1w", "%Y-%m-%d", "W"),   # labelled by the week's Monday
    ("month", "1mo", "%Y-%m", "M"),
    ("quarter", "1q", "", "Q"),
    ("year", "1y", "%Y", "Y"),
)


def _pick_grain(lf: pl.LazyFrame, column: str, dtype):
    """The finest grain whose bucket count fits `MAX_BARS`, or None when the
    column isn't temporal (a categorical axis isn't bucketed at all).

    Counts the distinct buckets every candidate would produce in **one** pass
    rather than estimating from the calendar span, so sparse data — three days
    spread over two years — stays on `day` instead of being coarsened to
    quarters for a range that was never populated.
    """
    if dtype is None or not dtype.is_temporal():
        return None
    counts = lf.select(
        pl.col(column).dt.truncate(every).n_unique().alias(name)
        for name, every, _, _ in GRAINS
    ).collect()
    for grain in GRAINS:
        if counts[grain[0]][0] <= MAX_BARS:
            return grain
    return GRAINS[-1]  # even whole years overflow — nothing coarser to offer


def _grain_options(chosen, override: str, emitter) -> list[dict]:
    """Segments for the in-chart grain picker: an `Auto` segment then every
    grain, **coarsest first** (Y Q M W D) so the strip reads left-to-right from
    the widest view down. Empty for a non-temporal axis, or outside a dashboard
    (no emitter ⇒ nothing to post a choice to).

    `Auto` is active while the viewer hasn't overridden anything; its tooltip
    names whichever grain the pick landed on, so the strip still says what
    you're looking at."""
    if chosen is None or not emitter:
        return []
    options = [{
        "abbr": "A",
        "title": f"Auto — {chosen[0]}",
        "is_active": not override,
        "token": f"{emitter}|",
    }]
    for name, _, _, letter in reversed(GRAINS):
        options.append({
            "abbr": letter,
            "title": name.capitalize(),
            "is_active": name == override,
            "token": f"{emitter}|{name}",
        })
    return options


# A temporal bucket is null when the value didn't parse — almost always a
# `str2dt()` pattern that doesn't match every row. Naming it beats a blank bar
# at the end of the axis, which reads as a rendering glitch rather than a
# data problem.
NO_DATE_LABEL = "(no date)"

# Working column names for the intermediate frame — prefixed so they can't
# collide with a dataset column called `label` or `total`.
_X_LABEL = "__x_label__"
_Y_LABEL = "__y_label__"
_TOTAL = "__total__"
_BUCKET = "__bucket__"
_BUCKET_END = "__bucket_end__"


def _label_expr(column: str, grain) -> pl.Expr:
    """Axis label for each bucket, as a Polars expression so hundreds of them
    are formatted in one vectorized pass instead of a Python `strftime` per row:
    `2026-06-01`, `2026-06`, `2026 Q2`, `2026`. Quarters have no strftime code,
    so they're composed from the year and quarter parts. A non-temporal axis
    just stringifies. Nulls become NO_DATE_LABEL (temporal) or blank."""
    col = pl.col(column)
    if grain is None:
        return col.cast(pl.String).fill_null("")
    name, _, fmt, _ = grain
    if name == "quarter":
        label = (
            col.dt.year().cast(pl.String)
            + pl.lit(" Q")
            + col.dt.quarter().cast(pl.String)
        )
    else:
        label = col.dt.strftime(fmt)
    return label.fill_null(NO_DATE_LABEL)


# Legend entries shown at once, and the pager that reaches the rest. Duplicated
# from the pie rather than shared: each chart folder is the modularity boundary
# here, the same reason the colour palette is repeated (see COLORS above).
LEGEND_PAGE_SIZE = 6


def _legend_nav(count: int, page: int, emitter, refresh: dict | None) -> dict:
    """Pager for the legend row: `(current, total)` plus the neighbouring pages.

    Empty when everything fits, or outside a dashboard (no endpoint to post a
    page change to). The page is *ephemeral* — it rides the request rather than
    the page-level hidden inputs, so any full re-render resets it to the first
    page."""
    pages = max(1, -(-count // LEGEND_PAGE_SIZE))   # ceil
    if pages == 1 or not emitter or not refresh:
        return {}
    current = max(0, min(page, pages - 1))
    return {
        "page": current,
        "label": f"{current + 1}/{pages}",
        "prev": max(0, current - 1),
        "next": min(pages - 1, current + 1),
        "at_start": current == 0,
        "at_end": current == pages - 1,
    }


# The most bars drawn at once. Past this the axis is windowed: only this many
# buckets are rendered and the scale below moves the window, so the SVG, the DOM
# and the label count stay bounded however long the range is.
MAX_WINDOW_BARS = 30


def _window(n: int, offset: int | None) -> tuple[int, int] | None:
    """`(start, stop)` of the drawn slice, or None when the whole axis fits.

    `offset` is the viewer's position from the scale; None means the default —
    the **most recent** bars, which is the end of a chronological axis and the
    smallest tail of a size-ordered one. Clamped, so a stale offset from a
    filtered-down chart can't scroll past the end."""
    if n <= MAX_WINDOW_BARS:
        return None
    last_start = n - MAX_WINDOW_BARS
    start = last_start if offset is None else max(0, min(offset, last_start))
    return start, start + MAX_WINDOW_BARS


# Clickable slices of the range in the scale. Enough that moving across it feels
# continuous, few enough that each stays a comfortable target.
SCALE_SEGMENTS = 40


def _scale(
    n: int, window: tuple[int, int] | None, grain: str, emitter, labels
) -> dict:
    """The scale under a windowed axis: the whole range as hoverable segments,
    the current window lit. Each segment names the dates it would show, so
    moving across the scale reads them out as you go, and clicking jumps there.

    Segments rather than a drag-slider because the tooltip has to follow the
    pointer: a range input drags natively, but reading its *live* value to
    label the position needs JavaScript, which chart output forbids. Hovering
    discrete elements gets the same read-as-you-move feedback with CSS alone —
    the same `:hover` reveal the bar segments already use."""
    if not window or not emitter:
        return {}
    start, stop = window
    last_start = n - MAX_WINDOW_BARS
    count = min(SCALE_SEGMENTS, last_start + 1)
    offsets = [
        round(k * last_start / (count - 1)) if count > 1 else 0
        for k in range(count)
    ]
    # Light the segment nearest the window, so an arbitrary offset (a stale
    # token, say) still shows where it landed.
    current = min(range(count), key=lambda i: abs(offsets[i] - start))
    segments = [
        {
            "token": f"{emitter}|{grain}|{off}",
            "tip": f"{labels[off]} – {labels[min(off + MAX_WINDOW_BARS - 1, n - 1)]}",
            "is_current": i == current,
        }
        for i, off in enumerate(offsets)
    ]
    return {
        "segments": segments,
        "prev": f"{emitter}|{grain}|{max(0, start - MAX_WINDOW_BARS)}",
        "next": f"{emitter}|{grain}|{start + MAX_WINDOW_BARS}",
        "at_start": start == 0,
        "at_end": stop >= n,
    }


def _plot_width(n: int) -> float:
    """Plot width (viewBox units) for `n` categories: the default until bars
    would fall below MIN_BAR_W, then one slot per bar (capped) so they keep that
    width and the canvas scrolls instead."""
    if n <= 0:
        return PLOT_W
    return max(PLOT_W, min(n * SLOT_W, MAX_PLOT_W))


def _bar_geometry(n: int, plot_w: float) -> tuple[float, float]:
    """`(slot, bar_width)` for `n` bars across `plot_w`. The gap is a share of
    the slot, so a squeezed plot yields thin bars rather than negative ones, and
    the width is capped so a handful of bars stay bars rather than billboards.

    The slot is capped too, which packs the bars **left** rather than spreading
    them: dividing the whole plot between two bars left them marooned at the
    third-points with the chart's own emptiness between them. Leftover width now
    collects on the right, the way the sideways layout leaves its leftover
    height at the bottom."""
    slot = min(plot_w / n, MAX_BAR_W + BAR_GAP)
    gap = min(BAR_GAP, slot * GAP_RATIO)
    return slot, min(slot - gap, MAX_BAR_W)

_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "chart.html").read_text(),
    autoescape=True,
)


def _segment_token(
    emitter: str | None, x_column: str, xv: str, y_column: str, yv: str, spans: dict
) -> str:
    """The crossfilter token for one segment: **both** of its dimensions, since a
    segment *is* an (x, y) cell. One token, so the pair toggles as a unit.

    A bucketed axis contributes a half-open range (`2026-06` is all of June);
    a categorical one contributes the exact value. With no `y` there is only one
    dimension to filter on, so the token carries the bucket alone."""
    if not emitter:
        return ""
    span = spans.get(xv)
    x_part = (
        filters_mod.range_part(x_column, *span)
        if span
        else filters_mod.value_part(x_column, xv)
    )
    if not y_column:
        return f"{emitter}|{x_part}"
    return f"{emitter}|{x_part}|{filters_mod.value_part(y_column, yv)}"


# Sideways layout (`direction: vertical`): categories run top-to-bottom and the
# bars grow rightward, so the gutters swap — a left one wide enough for category
# labels written horizontally (their whole reason for existing), a right one for
# the value. The band height is fixed rather than divided out of a fixed canvas,
# so ten categories and fifty both stay readable and the canvas scrolls instead.
VERT_CHART_W = 648
VERT_VALUE_W = 46
VERT_SLOT_H = 26
VERT_GAP_RATIO = 0.28
# The label gutter is measured from the labels rather than fixed, so short ones
# (an ISO date) don't reserve room for long ones that aren't there. Approximate
# — SVG text can't be measured server-side — so it's padded and clamped.
VERT_LABEL_CHAR_W = 5.4
VERT_LABEL_MIN = 46
VERT_LABEL_MAX = 240


def _label_gutter(labels: list[str]) -> float:
    """Width to reserve on the left for category labels."""
    widest = max((len(label) for label in labels), default=0)
    return min(max(widest * VERT_LABEL_CHAR_W + 14, VERT_LABEL_MIN), VERT_LABEL_MAX)


def _build_bands(
    x_vals: list[str],
    y_vals: list[str],
    data: dict[str, dict[str, float]],
    max_total: float,
    selected: set[str],
    x_column: str,
    y_column: str,
    emitter: str | None,
    fmt,
    spans: dict,
    label_w: float,
) -> list[dict]:
    """Geometry for sideways bars — one horizontal band per category, segments
    growing rightward. Mirrors `_build_stacks`; the fields it emits are the same,
    so the template draws either layout without branching."""
    n = len(x_vals)
    if n == 0 or max_total <= 0:
        return []
    plot_len = VERT_CHART_W - label_w - VERT_VALUE_W
    bar_h = VERT_SLOT_H * (1 - VERT_GAP_RATIO)
    bars = []
    flat_i = 0
    for i, xv in enumerate(x_vals):
        top = PLOT_Y + i * VERT_SLOT_H + (VERT_SLOT_H - bar_h) / 2
        counts = data.get(xv, {})
        total = sum(counts.values())
        segments = []
        cursor = label_w
        for j, yv in enumerate(y_vals):
            count = counts.get(yv, 0)
            if count == 0:
                continue
            length = (count / max_total) * plot_len
            token = _segment_token(emitter, x_column, xv, y_column, yv, spans)
            segments.append({
                "i": flat_i,
                "x": cursor,
                "y": top,
                "width": length,
                "height": bar_h,
                "color": COLORS[j % len(COLORS)],
                "label": yv,
                "x_label": xv,
                "count": fmt(count),
                # The tooltip shows the unrounded figure: an abbreviated display
                # (`5.4k`) is for the axis, where space is short — a tooltip is
                # where you go to find out what it actually was.
                "exact": calcs_mod.exact_value(count),
                "is_active": bool(token) and token in selected,
                "click_token": token,
            })
            cursor += length
            flat_i += 1
        middle = top + bar_h / 2
        bars.append({
            "label": xv,
            "total": fmt(total),
            # Value sits just past the bar's end; category label in the gutter.
            "value_x": cursor + 5,
            "value_y": middle + 3,
            "value_anchor": "start",
            "label_x": label_w - 7,
            "label_y": middle + 3,
            "label_anchor": "end",
            "label_rotate": 0,
            "segments": segments,
        })
    return bars


def _build_stacks(
    x_vals: list[str],
    y_vals: list[str],
    data: dict[str, dict[str, float]],
    max_total: float,
    selected: set[str],
    x_column: str,
    y_column: str,
    emitter: str | None,
    fmt,
    plot_w: float,
    spans: dict,
) -> list[dict]:
    """Geometry for each stacked bar. Segment heights are the calc value per
    (x, y); `fmt` renders the value strings. `plot_w` is the plot width in
    viewBox units — it grows past the default when there are many categories, so
    each bar keeps a minimum width and the canvas scrolls (see `to_html`).

    Each segment carries a `click_token` (when an emitter is set) so the
    template can wire htmx hx-vals without knowing about the dashboard layer.
    `spans` maps a bucket label to its `(start, end)` for a temporal axis.
    Segments are flat-indexed via `i` so per-segment hover tooltips below the
    SVG can pair by `data-i`.
    """
    n = len(x_vals)
    if n == 0 or max_total <= 0:
        return []
    slot, bar_w = _bar_geometry(n, plot_w)
    baseline = PLOT_Y + PLOT_H
    bars = []
    flat_i = 0
    for i, xv in enumerate(x_vals):
        # Centred in its slot, so a capped-width bar sits mid-slot instead of
        # hugging the left edge with all the slack on one side.
        x = PLOT_X + i * slot + (slot - bar_w) / 2
        counts = data.get(xv, {})
        total = sum(counts.values())
        segments = []
        cursor = baseline
        for j, yv in enumerate(y_vals):
            count = counts.get(yv, 0)
            if count == 0:
                continue
            height = (count / max_total) * PLOT_H
            cursor -= height
            token = _segment_token(
                emitter, x_column, xv, y_column, yv, spans
            )
            segments.append({
                "i": flat_i,
                "x": x,
                "y": cursor,
                "width": bar_w,
                "height": height,
                "color": COLORS[j % len(COLORS)],
                "label": yv,
                "x_label": xv,
                "count": fmt(count),
                # The tooltip shows the unrounded figure: an abbreviated display
                # (`5.4k`) is for the axis, where space is short — a tooltip is
                # where you go to find out what it actually was.
                "exact": calcs_mod.exact_value(count),
                "is_active": bool(token) and token in selected,
                "click_token": token,
            })
            flat_i += 1
        total_h = (total / max_total) * PLOT_H if max_total else 0
        centre = x + bar_w / 2
        bars.append({
            "label": xv,
            "total": fmt(total),
            "value_x": centre,
            "value_y": baseline - total_h - 4,
            "value_anchor": "middle",
            "label_x": centre,
            "label_y": baseline + 14,
            "label_anchor": "end",
            # Long labels (ISO dates) would collide written flat.
            "label_rotate": -30,
            "segments": segments,
        })
    return bars


@dataclass
class Bar:
    dataset: str
    title: str
    x: str
    # Optional: omit it for a plain one-bar-per-category chart. With a `y` the
    # bars stack, get a legend and a per-series palette; without one there's a
    # single series, so none of that applies.
    y: str = ""
    # A calc key resolved against the dashboard's `calcs:` block, or — for
    # standalone use — an inline calc definition dict (None means row count).
    # Each (x, y) segment is sized by the calc; a stack's total sums them, so
    # an additive calc (count/sum) is what makes a stacked bar meaningful.
    calc: object = None
    # Keep only the `top` biggest bars (0 = every bar). "Biggest" is by calc
    # value, not by position, so a date axis keeps its N busiest buckets and
    # still draws them oldest-first — a timeline that skipped to the largest
    # would stop being a timeline.
    top: int = 0
    # `horizontal` (the default) runs the categories left-to-right with the bars
    # growing upward; `vertical` runs them top-to-bottom with the bars growing
    # rightward. Note this names the axis the categories travel along, which is
    # the opposite of the convention some chart libraries use.
    direction: str = "horizontal"
    filters: list = field(default_factory=list)

    _resolve = None   # name -> (uri, storage_options); not a dataclass field
    _calcs = None  # CalcSet for this chart's dataset; set by the dashboard

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("x", "X (bar groups)"),
        ColumnParam("y", "Y (stack / breakdown)", optional=True),
        CalcParam("calc", "Calc"),
        IntParam("top", "Top N bars (0 = all)", minimum=0),
        ChoiceParam("direction", "Direction", DIRECTIONS),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)

    def _resolve_calc(self):
        """(CalcSet, key). Inline dict / None builds a one-off set; a string
        key resolves against the dashboard-supplied set."""
        if isinstance(self.calc, dict):
            return calcs_mod.single(self.calc)
        if self.calc in (None, ""):
            return calcs_mod.single(None)
        if self._calcs is None:
            raise ValueError(
                f"calc {self.calc!r} needs a dashboard `calcs:` block"
            )
        return self._calcs, self.calc

    def to_html(
        self,
        *,
        crossfilter: dict | None = None,
        grain: str = "",
        offset: int | None = None,
        legend_page: int = 0,
        refresh: dict | None = None,
        theme: str | None = None,
    ) -> str:
        """Render the chart.

        `theme` forces a palette (`"dark"`/`"light"`); omitted, the chart follows
        the viewer's OS preference (inherited from the dashboard root when nested).

        `crossfilter`, when provided, makes segments clickable. Same shape as
        the pie chart's: `endpoint`, `target`, `include`, `emitter`, `active`.
        Outside a dashboard the argument is omitted and segments render with
        no click attrs and no fade — identical to the standalone form. It also
        carries the endpoint the grain picker posts to, so the picker shows
        only inside a dashboard.

        `grain` names a time grain (see GRAINS) chosen by the viewer, replacing
        the automatic pick; empty means auto. `offset` is the viewer's position
        on the scale when a long axis is windowed; None means the default window
        (the most recent bars).

        `refresh` is where the grain and scale controls post: a chart's own view
        of its axis changes nothing for its neighbours, so they re-render just
        this cell rather than the whole dashboard. Crossfilter still goes wide.
        """
        ctx = crossfilter or {}
        # Lazy scan with predicate + projection pushdown: only x, y (+ calc /
        # filter columns) are read from the Parquet. Each (x, y) segment is
        # sized by the calc; drop empty/undefined groups.
        lf = scan(self.dataset, self._resolve, self._calcs)
        schema = lf.collect_schema()
        preds = filters_mod.predicates(self.filters, schema.names())
        if preds:
            lf = lf.filter(*preds)

        # A temporal x is bucketed to a time grain picked so the bars stay
        # readable — a year of daily rows is 365 hairlines otherwise. Chosen from
        # the *filtered* frame so the count reflects what's actually drawn, and
        # applied before aggregating so each calc reduces once, at the right
        # grain (rolling day-level values up in Python would break `avg`/`dcount`).
        # `grain` overrides that pick — the viewer clicked the in-chart picker.
        dtype = schema.get(self.x)
        chosen = _pick_grain(lf, self.x, dtype)
        if chosen and grain:
            chosen = next((g for g in GRAINS if g[0] == grain), chosen)
        if chosen:
            lf = lf.with_columns(pl.col(self.x).dt.truncate(chosen[1]))
        grain_options = _grain_options(chosen, grain, ctx.get("emitter"))

        calcs, key = self._resolve_calc()
        # `"_"` is the synthetic key for an inline/standalone calc with no real
        # name — blank it so the tooltip shows the value, not "_".
        calc = calcs.get(key)
        calc_name = "" if key == "_" else calc.name
        # Everything up to the geometry stays in Polars — label, drop, order and
        # total in vectorized passes — so a long axis costs a handful of frame
        # operations rather than a Python loop over every (x, y) pair. Only the
        # final SVG build below walks rows, and by then they're already ordered.
        #
        # A zero draws nothing, so it earns no bar, no legend row and no slot on
        # the axis — an all-zero group would otherwise eat axis width for an
        # invisible bar labelled `0`.
        counts = (
            calcs.aggregate(lf, [self.x, self.y] if self.y else [self.x], key)
            .drop_nulls(calcs_mod.VALUE)
            .filter(pl.col(calcs_mod.VALUE) != 0)
            .with_columns(
                _label_expr(self.x, chosen).alias(_X_LABEL),
                # One synthetic, unnamed series when there's no `y`, so the
                # stacking machinery below runs unchanged with a single layer.
                (
                    pl.col(self.y).cast(pl.String).fill_null("")
                    if self.y
                    else pl.lit("")
                ).alias(_Y_LABEL),
                pl.col(calcs_mod.VALUE).cast(pl.Float64),
            )
        )

        # Bar order: by stack total for a categorical axis, chronological for a
        # temporal one (by the bucket's date, not its label — `2026 Q2` doesn't
        # sort). Unparsed dates have no bucket, so they land last either way.
        by_x = counts.group_by(_X_LABEL).agg(
            pl.col(calcs_mod.VALUE).sum().alias(_TOTAL),
            pl.col(self.x).first().alias(_BUCKET),
        )
        # A bucket is a *range* — `2026-06` covers all of June — so a click on it
        # has to filter `>= start, < end`, not `== start`. The end is one grain
        # on from the (already truncated) start; Polars does the calendar
        # arithmetic, including quarters.
        if chosen:
            by_x = by_x.with_columns(
                pl.col(_BUCKET).dt.offset_by(chosen[1]).alias(_BUCKET_END)
            )
        if self.top > 0 and by_x.height > self.top:
            by_x = by_x.sort([_TOTAL, _X_LABEL], descending=[True, False]).head(self.top)
            # Narrow the rows too, so legend totals and the y-scale describe the
            # bars that are actually drawn.
            counts = counts.filter(
                pl.col(_X_LABEL).is_in(by_x[_X_LABEL].to_list())
            )
        by_x = (
            by_x.sort(_BUCKET, nulls_last=True)
            if chosen
            else by_x.sort([_TOTAL, _X_LABEL], descending=[True, False])
        )
        all_x = by_x[_X_LABEL].to_list()

        # Past MAX_WINDOW_BARS the axis is **windowed** rather than drawn whole:
        # only that many buckets are rendered, defaulting to the most recent, and
        # the scale below moves the window. Keeps the SVG, the DOM and the label
        # count bounded however long the range is — the alternative is thousands
        # of hairline bars nobody can read. Only inside a dashboard, where there's
        # an endpoint to move the window with; standalone there'd be no way back
        # to the hidden buckets, so the whole axis is drawn and scrolls.
        emitter = ctx.get("emitter")
        window = _window(len(all_x), offset) if emitter else None
        x_vals = all_x[window[0] : window[1]] if window else all_x
        # label -> (start, end) for the click token; empty for a categorical axis,
        # which filters on the exact value instead.
        spans = (
            dict(zip(by_x[_X_LABEL], zip(by_x[_BUCKET], by_x[_BUCKET_END])))
            if chosen
            else {}
        )
        if window:
            counts = counts.filter(pl.col(_X_LABEL).is_in(x_vals))

        # Legend order: largest series first, ties broken by label so the HTML
        # stays deterministic (polars' group_by has no order guarantee). Totals
        # cover the drawn window, not the whole range — they describe the bars
        # you can actually see.
        by_y = counts.group_by(_Y_LABEL).agg(
            pl.col(calcs_mod.VALUE).sum().alias(_TOTAL)
        ).sort([_TOTAL, _Y_LABEL], descending=[True, False])
        y_vals = by_y[_Y_LABEL].to_list()
        y_totals = dict(zip(y_vals, by_y[_TOTAL].to_list()))

        data: dict[str, dict[str, float]] = {}
        for xv, yv, value in counts.select(
            _X_LABEL, _Y_LABEL, calcs_mod.VALUE
        ).iter_rows():
            data.setdefault(xv, {})[yv] = value

        max_total = max(by_x[_TOTAL].to_list(), default=1) or 1

        active = set(ctx.get("active") or ())
        # Whole tokens, not values: a segment's token names both its dimensions,
        # so matching on the y value alone would light that series in *every*
        # bucket rather than the one cell that was clicked.
        selected = set(ctx.get("selected") or ())

        fmt = lambda v: calcs.fmt(key, v)
        sideways = self.direction == "vertical"
        if sideways:
            # Bands are a fixed height, so the canvas grows downward with the
            # category count and scrolls vertically rather than squeezing.
            scrolls = False
            plot_w = VERT_CHART_W
            chart_w = VERT_CHART_W
            chart_h = max(CHART_H, PLOT_Y * 2 + len(x_vals) * VERT_SLOT_H)
            label_w = _label_gutter(x_vals)
            axis = {"x1": label_w, "y1": PLOT_Y,
                    "x2": label_w, "y2": chart_h - PLOT_Y}
            bars = _build_bands(
                x_vals, y_vals, data, max_total, selected, self.x, self.y,
                emitter, fmt, spans, label_w,
            )
        else:
            # Scroll only when the whole axis is drawn *and* is too long to fit —
            # standalone, where there's no scale to move a window with. Everything
            # else is shown whole at the fixed FIT_PLOT_W aspect, so the drawing
            # scales the same way at any bar count and a grain switch keeps the
            # chart exactly the same size.
            scrolls = not window and _plot_width(len(x_vals)) > FIT_PLOT_W
            plot_w = _plot_width(len(x_vals)) if scrolls else FIT_PLOT_W
            chart_w = PLOT_X + plot_w + RIGHT_MARGIN
            chart_h = CHART_H
            # The baseline stops where the bars do. Running it the full plot
            # width past a left-packed handful of bars made the alignment read
            # as a chart that had failed to fill rather than one deliberately
            # aligned left.
            used = min(len(x_vals) * _bar_geometry(len(x_vals), plot_w)[0], plot_w) \
                if x_vals else plot_w
            axis = {"x1": PLOT_X, "y1": PLOT_Y + PLOT_H,
                    "x2": PLOT_X + used, "y2": PLOT_Y + PLOT_H}
            bars = _build_stacks(
                x_vals, y_vals, data, max_total, selected, self.x, self.y,
                emitter, fmt, plot_w, spans,
            )
        # Flat list of all segments — drives per-segment hover tooltip CSS.
        all_segments = [s for b in bars for s in b["segments"]]
        # Each tooltip carries its own placement, as a share of the viewBox, so
        # it lands next to its segment without depending on an anchor resolving
        # (see the note in chart.css). `y` is measured from the bottom so the
        # card hangs off the segment's top edge.
        for seg in all_segments:
            seg["tip_x"] = f"{(seg['x'] + seg['width'] / 2) / chart_w * 100:.3f}"
            seg["tip_y"] = f"{(chart_h - seg['y']) / chart_h * 100:.3f}"
        # Swatch + label only: the legend says which colour is which series, and
        # the values are already on the bars and in the hover tooltips. Each row
        # carries the same crossfilter token its segments do, so clicking the
        # legend toggles that series exactly like clicking a bar segment.
        # No `y` means one unnamed series, so there's nothing for a legend to
        # tell apart.
        legend = [] if not self.y else [
            {
                "label": yv,
                "color": COLORS[i % len(COLORS)],
                "is_active": yv in active,
                "click_token": f"{emitter}|{self.y}={yv}" if emitter else "",
            }
            for i, yv in enumerate(y_vals)
        ]
        # More series than fit a row are reached with a pager rather than a
        # scrollbar; every series is still drawn in the bars, so paging only
        # hides legend rows.
        legend_nav = _legend_nav(len(legend), legend_page, emitter, refresh)
        if legend_nav:
            start = legend_nav["page"] * LEGEND_PAGE_SIZE
            legend = legend[start : start + LEGEND_PAGE_SIZE]

        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            bars=bars,
            all_segments=all_segments,
            legend=legend,
            legend_nav=legend_nav,
            calc_name=calc_name,
            calc_desc=calc.description,
            chart_w=chart_w,
            chart_h=chart_h,
            axis=axis,
            sideways=sideways,
            scrolls=scrolls,
            scale=_scale(len(all_x), window, grain, emitter, all_x),
            grain_options=grain_options,
            refresh=refresh,
            has_selection=bool(selected),
            crossfilter=crossfilter,
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
