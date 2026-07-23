from dataclasses import dataclass, field
from pathlib import Path

import jinja2
import polars as pl

from fireflyer import filters as filters_mod
from fireflyer.params import ColumnParam, DatasetParam, FilterListParam, TextParam
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

# SVG plot geometry. Must match the viewBox in chart.html.
CHART_W = 380
CHART_H = 260
PLOT_X = 32
PLOT_Y = 16
PLOT_W = 332
PLOT_H = 190
BAR_GAP = 10

_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "chart.html").read_text(),
    autoescape=True,
)


def _build_stacks(
    x_vals: list[str],
    y_vals: list[str],
    data: dict[str, dict[str, int]],
    max_total: int,
    active: set[str],
    y_column: str,
    emitter: str | None,
) -> list[dict]:
    """Geometry for each stacked bar.

    Each segment carries a `click_token` (when an emitter is set) so the
    template can wire htmx hx-vals without knowing about the dashboard layer.
    Segments are flat-indexed via `i` so per-segment hover tooltips below the
    SVG can pair by `data-i`.
    """
    n = len(x_vals)
    if n == 0 or max_total <= 0:
        return []
    bar_w = (PLOT_W - BAR_GAP * (n - 1)) / n if n > 1 else PLOT_W
    baseline = PLOT_Y + PLOT_H
    bars = []
    flat_i = 0
    for i, xv in enumerate(x_vals):
        x = PLOT_X + i * (bar_w + BAR_GAP)
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
            segments.append({
                "i": flat_i,
                "x": x,
                "y": cursor,
                "width": bar_w,
                "height": height,
                "color": COLORS[j % len(COLORS)],
                "label": yv,
                "x_label": xv,
                "count": count,
                "is_active": yv in active,
                "click_token": (
                    f"{emitter}|{y_column}={yv}" if emitter else ""
                ),
            })
            flat_i += 1
        total_h = (total / max_total) * PLOT_H if max_total else 0
        bars.append({
            "label": xv,
            "total": total,
            "center_x": x + bar_w / 2,
            "value_y": baseline - total_h - 4,
            "label_y": baseline + 14,
            "segments": segments,
        })
    return bars


@dataclass
class Bar:
    dataset: str
    title: str
    x: str
    y: str
    filters: list = field(default_factory=list)

    _resolve = None  # name -> (uri, storage_options); not a dataclass field

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("x", "X (bar groups)"),
        ColumnParam("y", "Y (stack / breakdown)"),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)

    def to_html(
        self, *, crossfilter: dict | None = None, theme: str | None = None
    ) -> str:
        """Render the chart.

        `theme` forces a palette (`"dark"`/`"light"`); omitted, the chart follows
        the viewer's OS preference (inherited from the dashboard root when nested).

        `crossfilter`, when provided, makes segments clickable. Same shape as
        the pie chart's: `endpoint`, `target`, `include`, `emitter`, `active`.
        Outside a dashboard the argument is omitted and segments render with
        no click attrs and no fade — identical to the standalone form.
        """
        # Lazy scan with predicate + projection pushdown: only x, y (+ filter
        # columns) are read from the Parquet.
        lf = scan(self.dataset, self._resolve)
        preds = filters_mod.predicates(self.filters, lf.collect_schema().names())
        if preds:
            lf = lf.filter(*preds)
        counts = lf.group_by([self.x, self.y]).agg(pl.len().alias("count")).collect()

        # Stable tie-breaker (lexicographic value) keeps the rendered HTML
        # deterministic — polars' group_by has no order guarantee otherwise.
        x_totals = (
            counts.group_by(self.x)
            .agg(pl.col("count").sum().alias("total"))
            .sort(["total", self.x], descending=[True, False])
        )
        x_vals = [str(v) if v is not None else "" for v in x_totals[self.x].to_list()]

        y_totals = (
            counts.group_by(self.y)
            .agg(pl.col("count").sum().alias("total"))
            .sort(["total", self.y], descending=[True, False])
        )
        y_vals = [str(v) if v is not None else "" for v in y_totals[self.y].to_list()]
        y_total_counts = [int(v) for v in y_totals["total"].to_list()]

        data: dict[str, dict[str, int]] = {}
        for row in counts.iter_rows(named=True):
            xv = str(row[self.x]) if row[self.x] is not None else ""
            yv = str(row[self.y]) if row[self.y] is not None else ""
            data.setdefault(xv, {})[yv] = int(row["count"])

        max_total = max(
            (sum(data.get(xv, {}).values()) for xv in x_vals), default=1
        ) or 1

        ctx = crossfilter or {}
        active = set(ctx.get("active") or ())
        emitter = ctx.get("emitter")

        bars = _build_stacks(x_vals, y_vals, data, max_total, active, self.y, emitter)
        # Flat list of all segments — drives per-segment hover tooltip CSS.
        all_segments = [s for b in bars for s in b["segments"]]
        legend = [
            {
                "label": yv,
                "color": COLORS[i % len(COLORS)],
                "total": tot,
                "is_active": yv in active,
            }
            for i, (yv, tot) in enumerate(zip(y_vals, y_total_counts))
        ]

        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            bars=bars,
            all_segments=all_segments,
            legend=legend,
            chart_w=CHART_W,
            chart_h=CHART_H,
            baseline_y=PLOT_Y + PLOT_H,
            plot_x=PLOT_X,
            plot_right=PLOT_X + PLOT_W,
            has_selection=bool(active),
            crossfilter=crossfilter,
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
