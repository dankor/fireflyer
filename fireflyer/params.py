"""Editor parameter widgets — shared across every chart.

A chart's config is a set of parameters. In the browser editor's "edit chart"
modal each parameter is represented by a `Param`: a small object that knows how
to (1) **render** its input as autoescaped HTML, (2) **parse** the submitted form
value back to a Python value, and (3) **emit** that value into YAML.

Param classes live here, outside chart code, so every chart composes its editor
from the same building blocks — a chart just lists which params it has (its
`PARAMS`), and a new widget type is implemented once and reused. This is a
deliberate abstraction that supports the editor; see architecture.md,
"Chart params & editor modal", for why it's an intentional exception to the
repo's otherwise anti-abstraction stance.

Nothing here imports chart or dashboard code, so it's a standalone leaf module.
Rendering context (available datasets/columns) is passed in via `ParamContext`.
"""

from dataclasses import dataclass, field
from html import escape


@dataclass
class ParamContext:
    """What a widget needs to render without reaching into chart code."""
    datasets: dict[str, str] = field(default_factory=dict)  # dataset id -> path
    dataset_id: str | None = None                           # this chart's dataset
    columns: list[str] = field(default_factory=list)        # its dataset's columns


class Param:
    """Base widget. Subclasses override render/parse; to_yaml defaults to the
    value unchanged (fine for scalars)."""

    # Surfaced as a data-attribute so the editor JS can special-case a widget
    # (e.g. the filter builder) without parsing the label.
    kind = "text"

    def __init__(self, name: str, label: str):
        self.name = name
        self.label = label

    # --- to override ---------------------------------------------------------
    def render(self, value, ctx: ParamContext) -> str:
        raise NotImplementedError

    def parse(self, form):
        """`form` is any object with `.get(name)` (and `.getlist(name)` for the
        multi-value widgets) — Starlette's FormData in the app, a small fake in
        tests."""
        raise NotImplementedError

    def to_yaml(self, value):
        return value

    # --- shared helpers ------------------------------------------------------
    def _wrap(self, inner: str) -> str:
        return (
            f'<div class="ff-field" data-param="{escape(self.name, quote=True)}" '
            f'data-kind="{escape(self.kind, quote=True)}">'
            f'<label class="ff-field-label">{escape(self.label)}</label>'
            f'{inner}</div>'
        )


def _options(values, current) -> str:
    """<option> list with `current` pre-selected. `values` are (value, label)
    pairs or bare strings."""
    out = []
    for v in values:
        val, label = v if isinstance(v, tuple) else (v, v)
        sel = " selected" if str(val) == str(current) else ""
        out.append(
            f'<option value="{escape(str(val), quote=True)}"{sel}>'
            f'{escape(str(label))}</option>'
        )
    return "".join(out)


class TextParam(Param):
    kind = "text"

    def render(self, value, ctx: ParamContext) -> str:
        val = "" if value is None else str(value)
        return self._wrap(
            f'<input class="ff-input" type="text" name="{escape(self.name, quote=True)}" '
            f'value="{escape(val, quote=True)}">'
        )

    def parse(self, form):
        return (form.get(self.name) or "").strip()


class DatasetParam(Param):
    kind = "dataset"

    def render(self, value, ctx: ParamContext) -> str:
        return self._wrap(
            f'<select class="ff-input" name="{escape(self.name, quote=True)}">'
            f'{_options(list(ctx.datasets.keys()), value)}</select>'
        )

    def parse(self, form):
        return (form.get(self.name) or "").strip()


class ColumnParam(Param):
    """Dropdown of the chart dataset's columns. Keeps the current value even if
    the column list can't be read (e.g. missing CSV) so nothing is silently
    dropped."""
    kind = "column"

    def render(self, value, ctx: ParamContext) -> str:
        choices = list(ctx.columns)
        if value not in choices and value not in (None, ""):
            choices = [str(value), *choices]
        return self._wrap(
            f'<select class="ff-input" name="{escape(self.name, quote=True)}">'
            f'{_options(choices, value)}</select>'
        )

    def parse(self, form):
        return (form.get(self.name) or "").strip()


class ChoiceParam(Param):
    """Dropdown over a fixed set of options (e.g. an aggregation)."""
    kind = "choice"

    def __init__(self, name: str, label: str, choices):
        super().__init__(name, label)
        self.choices = list(choices)

    def render(self, value, ctx: ParamContext) -> str:
        return self._wrap(
            f'<select class="ff-input" name="{escape(self.name, quote=True)}">'
            f'{_options(self.choices, value)}</select>'
        )

    def parse(self, form):
        return (form.get(self.name) or "").strip()


class IntParam(Param):
    """Number input. `nullable` allows an empty value meaning "unset" (e.g. the
    map's auto-fit zoom), which emits nothing into YAML."""
    kind = "int"

    def __init__(self, name, label, *, minimum=None, maximum=None, step=1, nullable=False):
        super().__init__(name, label)
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.nullable = nullable

    def render(self, value, ctx: ParamContext) -> str:
        attrs = [f'name="{escape(self.name, quote=True)}"', f'step="{self.step}"']
        if self.minimum is not None:
            attrs.append(f'min="{self.minimum}"')
        if self.maximum is not None:
            attrs.append(f'max="{self.maximum}"')
        val = "" if value is None else str(value)
        placeholder = ' placeholder="auto"' if self.nullable else ""
        return self._wrap(
            f'<input class="ff-input" type="number" {" ".join(attrs)}'
            f' value="{escape(val, quote=True)}"{placeholder}>'
        )

    def parse(self, form):
        raw = (form.get(self.name) or "").strip()
        if raw == "":
            if self.nullable:
                return None
            raw = "0"
        return int(float(raw))

    def to_yaml(self, value):
        return value  # None is dropped by the emitter


class BoolParam(Param):
    kind = "bool"

    def render(self, value, ctx: ParamContext) -> str:
        checked = " checked" if value else ""
        return self._wrap(
            f'<label class="ff-check"><input type="checkbox" '
            f'name="{escape(self.name, quote=True)}" value="true"{checked}>'
            f'<span>{escape(self.label)}</span></label>'
        )

    def parse(self, form):
        # Unchecked checkboxes are absent from the submitted form.
        return bool(form.get(self.name))


class FilterListParam(Param):
    """The full filter builder: zero or more `{column, op, values}` rows. Rows
    are added/removed client-side; each row submits three parallel fields
    (`filter_column`, `filter_op`, `filter_values`), zipped back on parse."""
    kind = "filters"

    OPS = (("in", "in"), ("ni", "not in"))

    def _row(self, columns, column="", op="in", values_text="") -> str:
        col_opts = _options(
            [column, *columns] if column and column not in columns else columns,
            column,
        )
        return (
            '<div class="ff-filter-row">'
            f'<select class="ff-input" name="filter_column"><option value=""></option>'
            f'{col_opts}</select>'
            f'<select class="ff-input" name="filter_op">{_options(self.OPS, op)}</select>'
            f'<input class="ff-input" type="text" name="filter_values" '
            f'value="{escape(values_text, quote=True)}" placeholder="comma, separated">'
            '<button type="button" class="ff-filter-del" aria-label="Remove filter">'
            '&times;</button>'
            '</div>'
        )

    def render(self, value, ctx: ParamContext) -> str:
        rows = []
        for f in (value or []):
            vals = ", ".join(str(v) for v in f.get("values", []))
            rows.append(self._row(ctx.columns, f.get("column", ""), f.get("op", "in"), vals))
        template = self._row(ctx.columns)  # blank row cloned by the add button
        return self._wrap(
            f'<div class="ff-filters">'
            f'<div class="ff-filter-rows">{"".join(rows)}</div>'
            f'<button type="button" class="ff-filter-add">+ add filter</button>'
            f'<template class="ff-filter-tpl">{template}</template>'
            f'</div>'
        )

    def parse(self, form):
        cols = form.getlist("filter_column")
        ops = form.getlist("filter_op")
        vals = form.getlist("filter_values")
        out = []
        for col, op, raw in zip(cols, ops, vals):
            col = (col or "").strip()
            if not col:
                continue
            values = [s.strip() for s in (raw or "").split(",") if s.strip()]
            if not values:
                continue
            out.append({
                "column": col,
                "op": op if op in ("in", "ni") else "in",
                "values": values,
            })
        return out

    def to_yaml(self, value):
        return value or []  # empty list is dropped by the emitter
