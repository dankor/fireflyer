import math
from dataclasses import dataclass, field
from pathlib import Path

import jinja2

from fireflyer import filters as filters_mod
from fireflyer import measures as measures_mod
from fireflyer.params import (
    BoolParam,
    ColumnParam,
    DatasetParam,
    FilterListParam,
    MeasureParam,
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
R_OUT, R_IN = 90, 54

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
        else:
            next_angle = angle + 2 * math.pi * value / total
            path = _wedge_path(angle, next_angle)
            angle = next_angle
        segments.append({
            "color": COLORS[i % len(COLORS)],
            "path": path,
            "label": label,
            "display": fmt(value),
            "percent": f"{value / total * 100:.1f}",
            "is_active": label in active,
            "click_token": f"{emitter}|{column}={label}" if emitter else "",
        })
    return segments


@dataclass
class Pie:
    dataset: str
    title: str
    column: str  # category to group by
    # A measure key resolved against the dashboard's `measures:` block, or — for
    # standalone use — an inline measure definition dict (None means row count).
    # Slices are sized by the measure per category; the centre total is the
    # measure re-aggregated over the whole dataset (so a dcount isn't summed).
    measure: object = None
    total: bool = True  # show the grand total in the donut centre
    filters: list = field(default_factory=list)

    _resolve = None   # name -> (uri, storage_options); not a dataclass field
    _measures = None  # MeasureSet for this chart's dataset; set by the dashboard

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("column", "Column"),
        MeasureParam("measure", "Measure"),
        BoolParam("total", "Show total in centre"),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)

    def _resolve_measure(self):
        """(MeasureSet, key). Inline dict / None builds a one-off set; a string
        key resolves against the dashboard-supplied set."""
        if isinstance(self.measure, dict):
            return measures_mod.single(self.measure)
        if self.measure in (None, ""):
            return measures_mod.single(None)
        if self._measures is None:
            raise ValueError(
                f"measure {self.measure!r} needs a dashboard `measures:` block"
            )
        return self._measures, self.measure

    def to_html(
        self, *, crossfilter: dict | None = None, theme: str | None = None
    ) -> str:
        """Render the chart.

        `theme` forces a palette (`"dark"`/`"light"`); omitted, the chart follows
        the viewer's OS preference. Inside a dashboard the theme is inherited from
        the dashboard root, so this is only needed for standalone rendering.

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
        # projection into the Parquet read, so only what the measure and the
        # category column need is scanned, not the whole file.
        lf = scan(self.dataset, self._resolve)
        preds = filters_mod.predicates(self.filters, lf.collect_schema().names())
        if preds:
            lf = lf.filter(*preds)

        measures, key = self._resolve_measure()
        # Slice size = the measure per category; drop empty/undefined groups.
        grouped = (
            measures.aggregate(lf, [self.column], key)
            .drop_nulls(measures_mod.VALUE)
            .sort(measures_mod.VALUE, descending=True)
        )
        labels = [str(v) if v is not None else "" for v in grouped[self.column].to_list()]
        values = [float(v) for v in grouped[measures_mod.VALUE].to_list()]
        # Proportions are of the shown slices; the centre total is the measure
        # re-aggregated over the whole dataset (additive-independent — a dcount
        # total is distinct-over-all, not the sum of per-slice dcounts).
        shown_total = sum(values) or 1
        grand_total = measures.scalar(lf, key)

        ctx = crossfilter or {}
        active = set(ctx.get("active") or ())
        emitter = ctx.get("emitter")
        segments = _build_segments(
            labels, values, shown_total, self.column, active, emitter,
            lambda v: measures.fmt(key, v),
        )

        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            segments=segments,
            cx=CX,
            cy=CY,
            r_out=R_OUT,
            r_in=R_IN,
            has_selection=bool(active),
            crossfilter=crossfilter,
            show_total=self.total,
            total_display=_compact(grand_total or 0),
            total_exact=measures.fmt(key, grand_total),
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
