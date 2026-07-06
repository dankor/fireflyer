import math
from dataclasses import dataclass, field
from pathlib import Path

import jinja2
import polars as pl

from fireflyer import filters as filters_mod
from fireflyer.params import ColumnParam, DatasetParam, FilterListParam, TextParam

# Categorical palette borrowed from Apache Superset's "Default" theme.
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


def _build_segments(
    labels: list[str],
    values: list[int],
    total: int,
    column: str,
    active: set[str],
    emitter: str | None,
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
            "count": value,
            "percent": f"{value / total * 100:.1f}",
            "is_active": label in active,
            "click_token": f"{emitter}|{column}={label}" if emitter else "",
        })
    return segments


@dataclass
class Pie:
    dataset: str
    title: str
    column: str
    filters: list = field(default_factory=list)

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("column", "Column"),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)

    def to_html(self, *, crossfilter: dict | None = None) -> str:
        """Render the chart.

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
        df = pl.read_csv(self.dataset)
        df = filters_mod.apply(df, self.filters)
        counts = (
            df.group_by(self.column)
            .agg(pl.len().alias("count"))
            .sort("count", descending=True)
        )
        labels = [str(v) if v is not None else "" for v in counts[self.column].to_list()]
        values = [int(v) for v in counts["count"].to_list()]
        # `or 1` keeps percentage math defined when the CSV is empty.
        total = sum(values) or 1

        ctx = crossfilter or {}
        active = set(ctx.get("active") or ())
        emitter = ctx.get("emitter")
        segments = _build_segments(labels, values, total, self.column, active, emitter)

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
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
