from dataclasses import dataclass, field
from pathlib import Path

import jinja2

from fireflyer import filters as filters_mod
from fireflyer import measures as measures_mod
from fireflyer.params import DatasetParam, FilterListParam, MeasureParam, TextParam
from fireflyer.scan import scan

_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "chart.html").read_text(),
    autoescape=True,
)


@dataclass
class Number:
    dataset: str
    title: str
    # A measure key resolved against the dashboard's `measures:` block, or — for
    # standalone use — an inline measure definition dict (None means row count).
    # The scalar it reduces to fills the card; its `format` drives display.
    measure: object = None
    filters: list = field(default_factory=list)

    _resolve = None   # name -> (uri, storage_options); not a dataclass field
    _measures = None  # MeasureSet for this chart's dataset; set by the dashboard

    # Editor modal schema — one Param per constructor kwarg, in display order.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        MeasureParam("measure", "Measure"),
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

    def to_html(self, *, theme: str | None = None) -> str:
        """`theme` forces a palette (`"dark"`/`"light"`); omitted, the chart
        follows the viewer's OS preference (inherited from the dashboard root
        when nested)."""
        lf = scan(self.dataset, self._resolve)
        preds = filters_mod.predicates(self.filters, lf.collect_schema().names())
        if preds:
            lf = lf.filter(*preds)
        measures, key = self._resolve_measure()
        value = measures.scalar(lf, key)

        # `exact` is the un-formatted, full-precision figure, surfaced as a native
        # hover tooltip so a shaped display value can still reveal its exact one.
        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            value=measures.fmt(key, value),
            exact=measures_mod.format_value(value),
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
