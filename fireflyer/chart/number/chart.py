from dataclasses import dataclass, field
from pathlib import Path

import jinja2

from fireflyer import filters as filters_mod
from fireflyer import calcs as calcs_mod
from fireflyer.params import DatasetParam, FilterListParam, CalcParam, TextParam
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
    # A calc key resolved against the dashboard's `calcs:` block, or — for
    # standalone use — an inline calc definition dict (None means row count).
    # The scalar it reduces to fills the card; its `format` drives display.
    calc: object = None
    filters: list = field(default_factory=list)

    _resolve = None   # name -> (uri, storage_options); not a dataclass field
    _calcs = None  # CalcSet for this chart's dataset; set by the dashboard

    # Editor modal schema — one Param per constructor kwarg, in display order.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        CalcParam("calc", "Calc"),
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

    def to_html(self, *, theme: str | None = None) -> str:
        """`theme` forces a palette (`"dark"`/`"light"`); omitted, the chart
        follows the viewer's OS preference (inherited from the dashboard root
        when nested)."""
        lf = scan(self.dataset, self._resolve, self._calcs)
        preds = filters_mod.predicates(self.filters, lf.collect_schema().names())
        if preds:
            lf = lf.filter(*preds)
        calcs, key = self._resolve_calc()
        value = calcs.scalar(lf, key)
        calc = calcs.get(key)

        # Hover tooltip: when the calc has a `description`, a small card shows
        # the calc name, its description, and the exact (unformatted) value —
        # what the number means + its precise figure. Without one, a plain
        # `title` keeps the exact-value hover.
        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            value=calcs.fmt(key, value),
            exact=calcs_mod.exact_value(value),
            calc_name=calc.name,
            description=calc.description,
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()
