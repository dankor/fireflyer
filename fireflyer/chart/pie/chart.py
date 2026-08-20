import math
from dataclasses import dataclass, field
from pathlib import Path

import jinja2

from fireflyer import filters as filters_mod
from fireflyer import calcs as calcs_mod
from fireflyer.params import (
    BoolParam,
    ColumnParam,
    DatasetParam,
    FilterListParam,
    CalcParam,
    TextParam,
)
from fireflyer.scan import scan

# Fixed categorical palette for slices — theme-independent, so a value keeps
# its color across light and dark.
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

# SVG canvas geometry. Must match the viewBox in pie.html (0 0 220 220).
# CX/CY: canvas center. R_OUT/R_IN: outer and inner donut radii.
CX, CY = 110, 110
# A thicker ring (smaller hole) leaves room for the on-slice percent labels to
# sit within the band; R_LABEL centres a label radially in that band.
R_OUT, R_IN = 90, 44
R_LABEL = (R_OUT + R_IN) / 2
# Percent-label text metrics (px in the 220 viewBox) for the fit test below:
# ~char width at the label font size, and the label box height.
PCT_CHAR_W = 6.8
PCT_TEXT_H = 13.0


def _label_fits(mid: float, span: float, width: float) -> bool:
    """True if a horizontal label of `width` × PCT_TEXT_H, centred at the ring
    mid-radius on a slice of angular `span` at mid-angle `mid`, fits inside that
    slice's wedge — radially within the ring band and angularly within the arc.
    Uses the box's projected half-extents along the radial/tangential axes, so it
    adapts to orientation (wide labels need a wide arc on top/bottom, a thick
    ring on the sides). Slices where it doesn't fit get no label."""
    a, b = width / 2, PCT_TEXT_H / 2
    ct, st = abs(math.cos(mid)), abs(math.sin(mid))
    radial_extent = a * ct + b * st        # half-size along the radius
    tangential_extent = a * st + b * ct    # half-size along the arc
    radial_ok = radial_extent <= (R_OUT - R_IN) / 2
    angular_ok = tangential_extent <= R_LABEL * span / 2
    return radial_ok and angular_ok

# Legend entries shown at once. The legend is a single row above the donut, so
# it pages rather than wrapping or scrolling. A fixed size is the honest limit
# of doing this server-side: how many *actually* fit depends on label lengths
# and the cell's pixel width, neither of which is knowable here (Superset
# measures them in the browser). Six fits a narrow cell now that an entry is
# just a swatch and a label, and a page that still overflows scrolls sideways
# rather than clipping.
LEGEND_PAGE_SIZE = 6


def _legend_nav(count: int, page: int, emitter, refresh: dict | None) -> dict:
    """Pager for the legend row: `(current, total)` plus the neighbouring pages.

    Empty when everything fits, or outside a dashboard (no endpoint to post a
    page change to). The page is *ephemeral* — it rides the request rather than
    the page-level hidden inputs, so any full re-render resets it to the first
    page. That's a deliberate trade: which slice of the legend you're looking at
    isn't state worth threading through every subsequent request."""
    pages = max(1, -(-count // LEGEND_PAGE_SIZE))   # ceil
    if pages == 1 or not emitter or not refresh:
        return {}
    current = max(0, min(page, pages - 1))
    return {
        "label": f"{current + 1}/{pages}",
        "prev": max(0, current - 1),
        "next": min(pages - 1, current + 1),
        "at_start": current == 0,
        "at_end": current == pages - 1,
    }


_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "chart.html").read_text(),
    autoescape=True,
)


def _wedge_path(start: float, end: float) -> str:
    """SVG path string for one donut slice between two angles in radians.

    Traces: outer arc clockwise, line inward, inner arc counter-clockwise, close.
    """
    x1o, y1o = CX + R_OUT * math.cos(start), CY + R_OUT * math.sin(start)
    x2o, y2o = CX + R_OUT * math.cos(end), CY + R_OUT * math.sin(end)
    x2i, y2i = CX + R_IN * math.cos(end), CY + R_IN * math.sin(end)
    x1i, y1i = CX + R_IN * math.cos(start), CY + R_IN * math.sin(start)
    # SVG large-arc-flag: 1 when the slice spans more than 180°.
    large = 1 if (end - start) > math.pi else 0
    return (
        f"M {x1o:.2f} {y1o:.2f} "
        f"A {R_OUT} {R_OUT} 0 {large} 1 {x2o:.2f} {y2o:.2f} "
        f"L {x2i:.2f} {y2i:.2f} "
        f"A {R_IN} {R_IN} 0 {large} 0 {x1i:.2f} {y1i:.2f} Z"
    )


def _compact(value) -> str:
    """A short total for the donut centre: `1,420 → 1.4k`, `3,000,000 → 3m`.
    Lowercase business suffixes; ~2 significant figures so it fits the hole."""
    n = float(value)
    for step, suffix in ((1e12, "t"), (1e9, "b"), (1e6, "m"), (1e3, "k")):
        if abs(n) >= step:
            return f"{n / step:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{int(n):,}" if n.is_integer() else f"{n:g}"


def _build_segments(
    labels: list[str],
    values: list[float],
    total: float,
    column: str,
    active: set[str],
    emitter: str | None,
    fmt,
) -> list[dict]:
    """One segment per category, used for both the SVG and the legend.

    `path` is None for a single-category pie — those render as concentric
    circles because a 360° wedge has coincident endpoints and is undefined
    in SVG. `click_token` is the `<emitter>|<column>=<value>` string sent on
    slice click when the chart renders inside a dashboard; the emitter prefix
    lets the dashboard exempt the source chart from its own crossfilter.
    """
    n = len(values)
    # Start at 12 o'clock. SVG y grows downward, so angles increase clockwise.
    angle = -math.pi / 2
    segments = []
    for i, (label, value) in enumerate(zip(labels, values)):
        if n == 1:
            path = None
            mid = -math.pi / 2  # a full ring has no wedge; anchor its tooltip at top
            span = 2 * math.pi
        else:
            start = angle
            next_angle = angle + 2 * math.pi * value / total
            path = _wedge_path(start, next_angle)
            mid = (start + next_angle) / 2
            span = next_angle - start
            angle = next_angle
        # Anchor the slice's tooltip beside the donut, level with that slice:
        # `tip_y` follows the slice's mid-angle, but `tip_x` is pinned to the
        # donut's bounding-box edge on the slice's side rather than to the arc
        # itself. Hugging the arc looks closer but a card offset horizontally
        # from a point near the top of the circle still cuts across it, because
        # the circle bulges back out below — which is how a tooltip ended up
        # over the donut. The bounding box is the outermost the donut ever gets,
        # so clearing it clears the chart. Coords are canvas px (viewBox 1:1).
        side = "r" if math.cos(mid) >= 0 else "l"
        tip_x = CX + (R_OUT if side == "r" else -R_OUT)
        tip_y = CY + R_OUT * math.sin(mid)
        # On-slice percent label, centred in the ring band, shown only when it
        # fits its slice. `pct_w` is the highlight-box width (text + padding).
        percent = f"{value / total * 100:.2f}"
        pct_x = CX + R_LABEL * math.cos(mid)
        pct_y = CY + R_LABEL * math.sin(mid)
        text_w = len(percent + "%") * PCT_CHAR_W
        pct_w = text_w + 12
        segments.append({
            "color": COLORS[i % len(COLORS)],
            "path": path,
            "label": label,
            "display": fmt(value),
            # The unrounded, thousands-separated figure for the tooltip. Every
            # chart's tooltip shows this rather than the formatted value: a
            # format token abbreviates because the *chart* is short of space,
            # and a tooltip is where you go to find out what it stood for.
            "exact": calcs_mod.exact_value(value),
            "percent": percent,
            "is_active": label in active,
            "click_token": f"{emitter}|{column}={label}" if emitter else "",
            "tip_x": f"{tip_x:.1f}",
            "tip_y": f"{tip_y:.1f}",
            "tip_side": side,
            # On-slice percent label — only when the text actually fits the slice.
            "show_pct": _label_fits(mid, span, text_w),
            "pct_x": f"{pct_x:.1f}",
            "pct_y": f"{pct_y:.1f}",
            "pct_bg_x": f"{pct_x - pct_w / 2:.1f}",
            "pct_bg_y": f"{pct_y - 8:.1f}",
            "pct_bg_w": f"{pct_w:.1f}",
        })
    return segments


@dataclass
class Pie:
    dataset: str
    title: str
    column: str  # category to group by
    # A calc key resolved against the dashboard's `calcs:` block, or — for
    # standalone use — an inline calc definition dict (None means row count).
    # Slices are sized by the calc per category; the centre total is the
    # calc re-aggregated over the whole dataset (so a dcount isn't summed).
    calc: object = None
    total: bool = True  # show the grand total in the donut centre
    filters: list = field(default_factory=list)

    _resolve = None   # name -> (uri, storage_options); not a dataclass field
    _calcs = None  # CalcSet for this chart's dataset; set by the dashboard

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("column", "Column"),
        CalcParam("calc", "Calc"),
        BoolParam("total", "Show total in centre"),
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
        legend_page: int = 0,
        refresh: dict | None = None,
        theme: str | None = None,
    ) -> str:
        """Render the chart.

        `theme` forces a palette (`"dark"`/`"light"`); omitted, the chart follows
        the viewer's OS preference. Inside a dashboard the theme is inherited from
        the dashboard root, so this is only needed for standalone rendering.

        `legend_page` selects which page of legend entries to show when there
        are more than fit on one row; `refresh` is where the pager posts (see
        `_legend_nav`).

        `crossfilter`, when provided, makes slices clickable. It is a small
        config dict supplied by the dashboard:
          - `endpoint`: URL to POST on click (e.g. "/dashboard")
          - `target`: htmx target selector (e.g. "#fireflyer-dashboard")
          - `include`: hx-include selector that gathers the hidden YAML +
            current crossfilter inputs from the surrounding dashboard
          - `emitter`: this chart's dashboard id, embedded in each slice's
            click token so the dashboard can exempt the source from its own
            crossfilter at render time
          - `active`: iterable of values currently selected on `self.column`
        Outside a dashboard (e.g. standalone Python use, snapshot tests) the
        argument is omitted and slices render exactly as before.
        """
        # Lazy scan: Polars pushes the filter predicates and the column
        # projection into the Parquet read, so only what the calc and the
        # category column need is scanned, not the whole file.
        lf = scan(self.dataset, self._resolve, self._calcs)
        preds = filters_mod.predicates(self.filters, lf.collect_schema().names())
        if preds:
            lf = lf.filter(*preds)

        calcs, key = self._resolve_calc()
        # Slice size = the calc per category; drop empty/undefined groups.
        grouped = (
            calcs.aggregate(lf, [self.column], key)
            .drop_nulls(calcs_mod.VALUE)
            .sort(calcs_mod.VALUE, descending=True)
        )
        labels = [str(v) if v is not None else "" for v in grouped[self.column].to_list()]
        values = [float(v) for v in grouped[calcs_mod.VALUE].to_list()]
        # Proportions are of the shown slices; the centre total is the calc
        # re-aggregated over the whole dataset (additive-independent — a dcount
        # total is distinct-over-all, not the sum of per-slice dcounts).
        shown_total = sum(values) or 1
        grand_total = calcs.scalar(lf, key)

        ctx = crossfilter or {}
        active = set(ctx.get("active") or ())
        emitter = ctx.get("emitter")
        segments = _build_segments(
            labels, values, shown_total, self.column, active, emitter,
            lambda v: calcs.fmt(key, v),
        )

        # `"_"` is the synthetic key for an inline/standalone calc with no real
        # name — blank it so slice tooltips show just value + percent, not "_".
        calc = calcs.get(key)
        calc_name = "" if key == "_" else calc.name

        # The legend shows one page at a time (see `_legend_nav`); the donut
        # always shows every slice, so paging never hides data.
        nav = _legend_nav(len(segments), legend_page, ctx.get("emitter"), refresh)
        if nav:
            start = (int(nav["label"].split("/")[0]) - 1) * LEGEND_PAGE_SIZE
            legend = segments[start : start + LEGEND_PAGE_SIZE]
        else:
            legend = segments

        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            segments=segments,
            legend=legend,
            legend_nav=nav,
            refresh=refresh,
            cx=CX,
            cy=CY,
            r_out=R_OUT,
            r_in=R_IN,
            has_selection=bool(active),
            crossfilter=crossfilter,
            show_total=self.total,
            total_display=_compact(grand_total or 0),
            total_raw=calcs_mod.exact_value(grand_total),
            calc_name=calc_name,
            calc_desc=calc.description,
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
