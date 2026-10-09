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

import json
from dataclasses import dataclass, field
from typing import Callable, NamedTuple
from html import escape

from fireflyer import date_range
from fireflyer import filters as filters_mod


# Icon buttons, matching the portal gallery's vocabulary (`web/portal.py`
# `_icon`). Spelled out again rather than imported: nothing under `fireflyer/`
# imports from `fireflyer/web/`, and one small dict is cheaper than inverting
# that. Keep the shapes in step if the gallery's change.
ICONS = {
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "pencil": '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
             '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "close": '<path d="M18 6L6 18"/><path d="M6 6l12 12"/>',
}


def icon(name: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>'
    )


@dataclass
class ParamContext:
    """What a widget needs to render without reaching into chart code."""
    datasets: dict[str, str] = field(default_factory=dict)  # dataset id -> path
    dataset_id: str | None = None                           # this chart's dataset
    columns: list[str] = field(default_factory=list)        # its dataset's columns
    calcs: list[str] = field(default_factory=list)       # calc keys for its dataset
    # column -> its `ValueChoices`, or None when they can't be read; drives the
    # filter builder's value picker.
    column_values: Callable[[str], "ValueChoices | None"] | None = None
    # column -> its type (`column_type`), for the type glyph beside it and the
    # ops a filter on it offers. A column missing here is of unknown type.
    column_types: dict[str, str] = field(default_factory=dict)


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
    """The chart's dataset — its unique name. A plain text field for now; once
    the portal's dataset gallery is wired in, `ctx.datasets` can turn this back
    into a dropdown of the stored dataset names."""

    kind = "dataset"

    def render(self, value, ctx: ParamContext) -> str:
        val = "" if value is None else str(value)
        return self._wrap(
            f'<input class="ff-input" type="text" name="{escape(self.name, quote=True)}" '
            f'value="{escape(val, quote=True)}">'
        )

    def parse(self, form):
        return (form.get(self.name) or "").strip()


class ColumnParam(Param):
    """Dropdown of the chart dataset's columns. Keeps the current value even if
    the column list can't be read (e.g. missing CSV) so nothing is silently
    dropped. `optional` adds a blank choice, without which a column that has a
    meaning when unset (the bar's `y`) could be set but never cleared."""
    kind = "column"

    def __init__(self, name: str, label: str, optional: bool = False):
        super().__init__(name, label)
        self.optional = optional

    def render(self, value, ctx: ParamContext) -> str:
        choices = list(ctx.columns)
        if value not in choices and value not in (None, ""):
            choices = [str(value), *choices]
        if self.optional:
            choices = [("", "—"), *choices]
        return self._wrap(
            f'<select class="ff-input" name="{escape(self.name, quote=True)}">'
            f'{_options(choices, value)}</select>'
        )

    def parse(self, form):
        return (form.get(self.name) or "").strip()


class CalcParam(Param):
    """Dropdown of the chart dataset's calc keys (defined in the dashboard's
    `calcs:` block, managed in the calcs modal). Keeps the current value
    even when the calc list can't be read, so nothing is silently dropped."""
    kind = "calc"

    def render(self, value, ctx: ParamContext) -> str:
        choices = list(ctx.calcs)
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


class ListParam(Param):
    """Comma-separated list of names — the table's `columns`, `measures` and
    `sort`.

    A text field rather than a multi-select: all three are **order-sensitive**
    (column order, measure order, sort precedence), and `sort` entries carry a
    `+`/`-` prefix. A checkbox list expresses neither.
    """
    kind = "list"

    def __init__(self, name: str, label: str, placeholder: str = ""):
        super().__init__(name, label)
        self.placeholder = placeholder

    def render(self, value, ctx: ParamContext) -> str:
        val = ", ".join(str(v) for v in (value or []))
        placeholder = (
            f' placeholder="{escape(self.placeholder, quote=True)}"'
            if self.placeholder
            else ""
        )
        return self._wrap(
            f'<input class="ff-input" type="text" '
            f'name="{escape(self.name, quote=True)}" '
            f'value="{escape(val, quote=True)}"{placeholder}>'
        )

    def parse(self, form):
        raw = form.get(self.name) or ""
        return [part.strip() for part in raw.split(",") if part.strip()]

    def to_yaml(self, value):
        return value or []  # empty list is dropped by the emitter


# The filter builder's inputs, shared by two places: the chart builder's
# `FilterListParam` (filters saved into the chart's YAML) and the dashboard's
# filter panel, whose + adds a global quick filter for this viewer. One set of
# fields and one parser, so the two can't drift apart.

# `between` takes exactly two comma-separated values (low, high) and is
# half-open — the same op a bar segment emits for a bucketed axis.
FILTER_OPS = (("in", "in"), ("ni", "not in"), ("between", "between"))


# The ops that compare against a set of exact values — the ones a value picker
# can serve.
PICKABLE_OPS = ("in", "ni")
# A column's type as the editor shows it: an icon and what it means. One table
# for every place a column is listed — the dataset page, the filter fields'
# column picker, the filter panel's rows. Drawn on the chart icons' 16px grid
# in `currentColor`, because text glyphs (`◷`, a boxed `T`) were unreadable at
# this size. `date` also decides the ops: it is the only type a range makes
# sense on. `between` exists for time buckets; on text it compares
# alphabetically, which nobody means.
DATE_KIND = "date"
COLUMN_TYPES = {
    # Digits — the same `123` as the number chart's icon: both say "a number".
    "number": ('<path d="M2 6 3.5 4.5V11.5"/><path d="M6 6a1.5 1.5 0 0 1 3 0c0 1.4-3 2.6-3 5.5h3"/>'
               '<path d="M11 4.6h2.6l-1.6 2.2a1.9 1.9 0 1 1-1.2 3.6"/>', "number"),
    # A toggle, switched on.
    "boolean": ('<rect x="1.5" y="4.5" width="13" height="7" rx="3.5"/>'
                '<circle cx="11" cy="8" r="1.8"/>', "boolean"),
    # A calendar page.
    DATE_KIND: ('<rect x="2.5" y="3.5" width="11" height="10" rx="1.5"/>'
                '<path d="M2.5 7h11M5.5 2v3M10.5 2v3"/>', "date / time"),
    # A letter beside lines of text.
    "text": ('<path d="M1.5 12.5 4.5 4l3 8.5M2.5 10h4"/>'
             '<path d="M10 6.5h4.5M10 9.5h4.5M10 12.5h3"/>', "text"),
    # A question mark in a circle.
    "other": ('<circle cx="8" cy="8" r="5.8"/>'
              '<path d="M6.3 6.4a1.8 1.8 0 1 1 2.4 1.7c-.5.2-.7.6-.7 1.1M8 11.2v.1"/>', "other"),
}


def column_type(dtype) -> str:
    """A `COLUMN_TYPES` key for a Polars dtype (or its string form). A duration
    is checked first: its name mentions a time unit, but it is no date."""
    d = str(dtype).lower()
    if "duration" in d:
        return "other"
    if any(x in d for x in ("int", "float", "decimal")):
        return "number"
    if "bool" in d:
        return "boolean"
    if "date" in d or "time" in d:
        return DATE_KIND
    if any(x in d for x in ("str", "utf", "categorical", "enum")):
        return "text"
    return "other"


def type_icon_svg(kind) -> str:
    """The bare SVG for a column type; "" for an unknown type."""
    if kind not in COLUMN_TYPES:
        return ""
    paths, _ = COLUMN_TYPES[kind]
    return (
        '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none"'
        ' stroke="currentColor" stroke-width="1.4" stroke-linecap="round"'
        f' stroke-linejoin="round">{paths}</svg>'
    )


def type_glyph(kind) -> str:
    """The type icon shown beside a column name, its meaning on hover; "" for
    an unknown type — never a wrong one."""
    if kind not in COLUMN_TYPES:
        return ""
    _, name = COLUMN_TYPES[kind]
    return f'<span class="ff-type-glyph" title="{escape(name)}">{type_icon_svg(kind)}</span>'

# Re-renders a filter row's fields when its column or op changes, so the values
# control can turn into (or out of) a picker. Shared by both callers.
FILTER_FIELDS_ENDPOINT = "/filter/fields"
# Re-renders just the value list for a search, so the search box keeps focus.
FILTER_VALUES_ENDPOINT = "/filter/values"
# A list this short is read at a glance; past it, a search box earns its place.
SEARCH_FROM = 10


class ValueChoices(NamedTuple):
    """A column's distinct values for a value picker: sorted, as text, at most
    a page of them (`more` says the column has others — the search narrows)."""
    values: list[str]
    more: bool = False


def filter_ops(kind, column="", op="") -> tuple:
    """The `(value, label)` ops a filter on a column of `kind` offers: `between`
    only on dates. With a column whose kind is unknown (its schema can't be
    read) nothing is withheld that might be right; with no column yet, only the
    ops every column takes. An `op` already set is always kept — opening a
    filter must never quietly change what it says."""
    if kind == DATE_KIND or (kind is None and column):
        ops = FILTER_OPS
    else:
        ops = tuple(o for o in FILTER_OPS if o[0] in PICKABLE_OPS)
    extra = tuple(o for o in FILTER_OPS if o[0] == op and o not in ops)
    return ops + extra


def filter_fields(
    columns, column="", op="in", values=(), choices=None, live=None, types=None,
    anchor="",
) -> str:
    """Column / op / values inputs for one filter, read back by
    `parse_filters`. A `column` the list doesn't have is kept as an option, so
    an edit never silently drops it. `types` (column -> `column_type`) puts a
    type glyph beside each column and decides the ops (`filter_ops`).

    The values control depends on what is known. For `in` / `not in` with the
    column's values in hand (`choices`, a `ValueChoices`), it is a checkbox list
    — the current ones ticked — so nobody has to remember or retype them, with
    a search box once the list is long (`value_picker`). For `between` on a
    date column it is a one-field range picker (`date_range`) — opening on
    `anchor`, the column's latest day, when no range is set yet.
    Otherwise (no column yet, or values that can't be read) it is the
    comma-separated text input.

    Until a column is picked the op and values are disabled — they mean nothing
    yet, and a column change re-renders them anyway. With `live` every field is
    also `required`, which is what lets the panel show its + only once the row
    is complete (pure CSS on `:invalid`); the chart builder can't use that,
    since a blank row would then block saving the chart.

    `live` (the dataset name) wires the column and op selects to re-fetch these
    fields on change via htmx — the dashboard's filter panel, which has no
    script of its own. The dataset rides in `hx-vals` rather than a hidden
    input, which every crossfilter click would otherwise carry along. The chart
    builder does the same re-fetch from the editor's JS instead, because its
    form holds many rows and htmx would send them all.
    """
    types = types or {}
    kind = types.get(column)
    need = " required" if live is not None else ""
    off = "" if column else " disabled"
    listed = [column, *columns] if column and column not in columns else list(columns)
    refetch = search = ""
    if live is not None:
        context = escape(json.dumps({"filter_dataset": live, "live": "1"}), quote=True)
        shared = (
            ' hx-include="#fireflyer-dashboard input[name=yaml_text]"'
            f' hx-vals="{context}"'
        )
        refetch = (
            f' hx-post="{FILTER_FIELDS_ENDPOINT}" hx-trigger="change"'
            ' hx-target="closest .ff-filter-fields" hx-swap="outerHTML"' + shared
        )
        # A pause in typing, or the search box's own clear (×). Only the list
        # is swapped, so the box keeps focus and the cursor stays put.
        search = (
            f' hx-post="{FILTER_VALUES_ENDPOINT}"'
            ' hx-trigger="input changed delay:250ms, search"'
            ' hx-target="next .ff-filter-choices" hx-swap="outerHTML"' + shared
        )
    return (
        '<span class="ff-filter-fields">'
        f'<select class="ff-input ff-column-select" name="filter_column" aria-label="Column"{need}{refetch}>'
        f'{_column_options(listed, column, types)}</select>'
        f'<select class="ff-input" name="filter_op" aria-label="Operator"{off}{refetch}>'
        f'{_options(filter_ops(kind, column, op), op)}</select>'
        f'{_values_control(op, values, choices, kind, need + off, search, anchor)}'
        '</span>'
    )


def _column_options(columns, current, types) -> str:
    """The column picker's options, each led by its type glyph. A customizable
    select (`appearance: base-select`) draws the glyph, and `<selectedcontent>`
    carries it into the closed control; a browser without one shows the
    options' text — the glyph character and the name, so the type still reads."""
    options = "".join(
        f'<option value="{escape(c, quote=True)}"{" selected" if c == current else ""}>'
        f'{type_glyph(types.get(c))} <span>{escape(c)}</span></option>'
        for c in columns
    )
    # The empty option doubles as the placeholder the closed picker shows.
    placeholder = '<option value=""><span class="ff-placeholder">Column</span></option>'
    return f'<button><selectedcontent></selectedcontent></button>{placeholder}{options}'


def _values_control(op, values, choices, kind=None, attrs="", search="", anchor="") -> str:
    """`attrs` (` required`, ` disabled`) go on the text and date inputs. Not on
    the checkboxes: `required` there would demand every box ticked — "at least
    one" is the panel's CSS's job."""
    values = [str(v) for v in values]
    if op == "between" and kind == DATE_KIND:
        picker = _day_range(values, "required" in attrs, anchor)
        if picker:
            return picker
    if op not in PICKABLE_OPS or choices is None:
        text = ", ".join(values)
        return (
            '<input class="ff-input" type="text" name="filter_values" aria-label="Values" '
            f'value="{escape(text, quote=True)}" placeholder="comma, separated"{attrs}>'
        )
    return value_picker(values, choices, search)


def value_picker(selected, choices: ValueChoices, search="") -> str:
    """The checkbox list, with a search box above it once the column has more
    than `SEARCH_FROM` values. `search` is the box's htmx wiring (the panel);
    the editor's JS drives an unwired one.

    The box is outside the form (`form` names one that doesn't exist): Enter in
    it can't submit the panel's + by accident, and its text is never saved as
    part of a chart."""
    box = ""
    if choices.more or len(choices.values) > SEARCH_FROM:
        box = (
            '<input class="ff-input ff-filter-search" type="search" name="filter_q"'
            ' form="ff-none" placeholder="Search values" aria-label="Search values"'
            f' autocomplete="off"{search}>'
        )
    return f'<div class="ff-filter-picker">{box}{value_list(selected, choices)}</div>'


def value_list(selected, choices: ValueChoices, query="") -> str:
    """The checkboxes alone — what a search re-renders. Ticked values come
    first, matching or not: searching narrows what you can add, never what you
    already picked. That also keeps a value the data no longer has visible, so
    opening an old filter shows what it says instead of quietly losing it."""
    selected = [str(v) for v in selected]
    listed = selected + [v for v in choices.values if v not in selected]
    boxes = "".join(
        '<label class="ff-filter-choice">'
        f'<input type="checkbox" name="filter_value" value="{escape(v, quote=True)}"'
        f'{" checked" if v in selected else ""}><span>{escape(v)}</span></label>'
        for v in listed
    )
    if choices.more:
        note = "more values — keep typing" if query else "more values — search to narrow"
        boxes += f'<span class="ff-filter-choices-note">{note}</span>'
    elif not listed:
        note = "no matches" if query else "no values"
        boxes += f'<span class="ff-filter-choices-note">{note}</span>'
    return f'<div class="ff-filter-choices" role="group" aria-label="Values">{boxes}</div>'


def _day_range(values, required=False, anchor="") -> str:
    """The range picker for a `between` on a date column, prefilled from the
    stored half-open bounds — it shows the last day *in* the range. "" when the
    bounds carry a real time (an hourly bucket), which a day picker can't show;
    the text input keeps those intact instead."""
    first = last = ""
    if values:
        days = filters_mod.day_range(values)
        if days is None:
            return ""
        first, last = days
    return date_range.render(first, last, anchor=anchor, required=required)


def parse_filters(form) -> list[dict]:
    """`{column, op, values}` for each submitted `filter_fields` row. A row with
    no column or no values is skipped — that's an unfilled row, not an error.

    Walked in submission order, which is document order: a `filter_column`
    opens a row and the op and values after it belong to that row. Pairing the
    fields up by position can't work once a row's values are checkboxes — a
    row submits as many `filter_value`s as are ticked, or none.
    """
    rows = []
    for key, raw in form.multi_items():
        if key == "filter_column":
            rows.append({"column": (raw or "").strip(), "op": "in", "values": []})
        elif not rows:
            continue
        elif key == "filter_op":
            rows[-1]["op"] = raw if raw in filters_mod.OPS else "in"
        elif key == "filter_values":
            rows[-1]["values"] += [s.strip() for s in (raw or "").split(",") if s.strip()]
        elif key == "filter_value":                    # a ticked checkbox
            rows[-1]["values"].append(raw)
        elif key in ("filter_from", "filter_to"):     # a date picker
            rows[-1][key] = (raw or "").strip()
    for row in rows:
        first, last = row.pop("filter_from", ""), row.pop("filter_to", "")
        if first and last:
            row["values"] = filters_mod.day_bounds(first, last) or []
    return [r for r in rows if r["column"] and r["values"]]


class FilterListParam(Param):
    """The full filter builder: zero or more `{column, op, values}` rows. Rows
    are added/removed client-side; each row is `filter_fields` plus a remove
    button."""
    kind = "filters"

    def _row(self, columns, column="", op="in", values=(), choices=None, types=None) -> str:
        return (
            '<div class="ff-filter-row">'
            f'{filter_fields(columns, column, op, values, choices, types=types)}'
            '<button type="button" class="ff-filter-del" title="Remove filter"'
            f' aria-label="Remove filter">{icon("close")}</button>'
            '</div>'
        )

    def render(self, value, ctx: ParamContext) -> str:
        rows = []
        for f in (value or []):
            column = f.get("column", "")
            choices = ctx.column_values(column) if ctx.column_values and column else None
            rows.append(self._row(
                ctx.columns, column, f.get("op", "in"), f.get("values", []), choices,
                ctx.column_types,
            ))
        template = self._row(ctx.columns, types=ctx.column_types)  # cloned by the add button
        return self._wrap(
            f'<div class="ff-filters">'
            f'<div class="ff-filter-rows">{"".join(rows)}</div>'
            '<button type="button" class="ff-filter-add" title="Add a filter"'
            f' aria-label="Add a filter">{icon("plus")}</button>'
            f'<template class="ff-filter-tpl">{template}</template>'
            f'</div>'
        )

    def parse(self, form):
        return parse_filters(form)

    def to_yaml(self, value):
        return value or []  # empty list is dropped by the emitter
