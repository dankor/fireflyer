"""Surgical edits to a dashboard's top-level `calcs:` block.

The editor's calcs manager (a modal like the docs overlay) adds / edits /
deletes calcs from the UI. Like `config_edit`, these are pure functions over
YAML **text** so they unit-test without the web stack, and each result is
re-validated through `Dashboard.from_yaml` before it's returned.

Only the `calcs:` block is rewritten — everything else in the document
(charts, layout, comments, formatting) stays byte-for-byte. The calcs block
itself is re-emitted from parsed data, so comments *inside* it are not preserved
(a documented limitation, same as `config_edit`'s within-block behavior).
"""

from __future__ import annotations

from html import escape

import yaml

from fireflyer import calcs as calcs_mod
from fireflyer import filters as filters_mod
from fireflyer.params import FilterListParam, ParamContext


class CalcsEditError(ValueError):
    """Raised for an invalid calcs edit — message is shown to the user."""


def _load(text: str) -> dict:
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def list_for_dataset(text: str, dataset: str) -> dict:
    """The raw calc definitions declared for `dataset` (key -> def)."""
    block = _load(text).get("calcs") or {}
    got = block.get(dataset)
    return dict(got) if isinstance(got, dict) else {}


def chart_datasets(text: str) -> list[str]:
    """Dataset names referenced by the dashboard's charts, in first-seen order —
    the datasets the manager offers to attach a calc to."""
    charts = _load(text).get("charts") or {}
    seen: list[str] = []
    for cfg in charts.values():
        ds = cfg.get("dataset") if isinstance(cfg, dict) else None
        if ds and ds not in seen:
            seen.append(ds)
    return seen


def upsert_calc(
    text: str, dataset: str, key: str, definition: dict, original_key: str = ""
) -> str:
    """Add or replace calc `key` under `dataset`, creating the block/dataset
    as needed. `original_key` (when renaming) drops the old entry. Validates the
    whole document before returning."""
    key = (key or "").strip()
    if not key:
        raise CalcsEditError("a calc needs a key")
    if not dataset:
        raise CalcsEditError("a calc needs a dataset")

    config = _load(text)
    block = dict(config.get("calcs") or {})
    entries = dict(block.get(dataset) or {})
    if original_key and original_key != key:
        entries.pop(original_key, None)
    entries[key] = _clean(definition)
    block[dataset] = entries

    new_text = _splice(text, block)
    _validate(new_text)
    return new_text


def delete_calc(text: str, dataset: str, key: str) -> str:
    """Remove calc `key` from `dataset`. Refuses if a chart still references
    it (the referencing chart would fail validation). Drops an emptied dataset
    and an emptied block."""
    referrers = _referencing_charts(text, dataset, key)
    if referrers:
        raise CalcsEditError(
            f"calc {key!r} is used by chart(s) {', '.join(sorted(referrers))} — "
            "point them at another calc first"
        )
    config = _load(text)
    block = dict(config.get("calcs") or {})
    entries = dict(block.get(dataset) or {})
    entries.pop(key, None)
    if entries:
        block[dataset] = entries
    else:
        block.pop(dataset, None)

    new_text = _splice(text, block)
    _validate(new_text)
    return new_text


def _referencing_charts(text: str, dataset: str, key: str) -> set[str]:
    charts = _load(text).get("charts") or {}
    out = set()
    for cid, cfg in charts.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("dataset") == dataset and cfg.get("calc") == key:
            out.add(cid)
    return out


def _clean(definition: dict) -> dict:
    """Drop empty optional keys so the emitted YAML stays tidy. `agg`/`formula`
    are kept as given (telling the kinds apart is the caller's job)."""
    order = ("name", "description", "agg", "formula", "format", "filters")
    out = {}
    for k in order:
        v = definition.get(k)
        if v in (None, "", []):
            continue
        out[k] = v
    return out


def _validate(text: str) -> None:
    from fireflyer.dashboard import Dashboard, DashboardError

    try:
        Dashboard.from_yaml(text)
    except DashboardError as exc:
        raise CalcsEditError(str(exc)) from exc


# --- block splicing -----------------------------------------------------------


def _emit(block: dict) -> str:
    """Serialize `{calcs: block}` to text, or "" when the block is empty."""
    if not block:
        return ""
    dumped = yaml.safe_dump(
        {"calcs": block}, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return dumped.rstrip("\n") + "\n"


def _find_top_block(lines: list[str], key: str) -> tuple[int, int] | None:
    """(start, end) line indices of a top-level `key:` block — from its line to
    the line before the next column-0 key — or None if absent."""
    start = None
    for i, line in enumerate(lines):
        if line.startswith(key + ":"):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line and not line[0].isspace() and not line.startswith("#"):
            end = j
            break
    return start, end


def _splice(text: str, block: dict) -> str:
    """Replace (or insert / remove) the `calcs:` block in `text`."""
    lines = text.split("\n")
    found = _find_top_block(lines, "calcs")
    new_block = _emit(block)
    new_lines = new_block.split("\n")[:-1] if new_block else []

    if found:
        start, end = found
        # Keep a single blank separator line after the block if the original had
        # one (blocks in these docs are separated by a blank line).
        trailing = [""] if new_lines and end < len(lines) and lines[end - 1] == "" else []
        replacement = new_lines + trailing if new_lines else []
        lines[start:end] = replacement
        return "\n".join(lines)

    if not new_lines:
        return text  # nothing to insert

    # Insert before `charts:` (or after `name:` as a fallback).
    anchor = next((i for i, ln in enumerate(lines) if ln.startswith("charts:")), None)
    if anchor is None:
        anchor = next((i for i, ln in enumerate(lines) if ln.startswith("name:")), -1) + 1
    insert = new_lines + [""]
    lines[anchor:anchor] = insert
    return "\n".join(lines)


# --- form <-> definition ------------------------------------------------------


def definition_from_form(form) -> dict:
    """Build a calc definition dict from the manager form. An aggregate calc
    carries `agg` (+ optional row `formula`); a formula calc carries only
    `formula` — whether that reads as a derived value or a calculated column is
    inferred from what the formula references, not written down."""
    kind = (form.get("kind") or "aggregate").strip()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    fmt = (form.get("format") or "").strip()
    formula = (form.get("formula") or "").strip()
    definition: dict = {}
    if name:
        definition["name"] = name
    if description:
        definition["description"] = description
    if kind == "formula":
        definition["formula"] = formula
    else:
        definition["agg"] = (form.get("agg") or "count").strip()
        if formula:
            definition["formula"] = formula
        filters = _filters_from_form(form)
        if filters:
            definition["filters"] = filters
    if fmt:
        definition["format"] = fmt
    return definition


def _filters_from_form(form) -> list:
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
            "op": op if op in filters_mod.OPS else "in",
            "values": values,
        })
    return out


AGGS = calcs_mod.AGGS


# --- manager UI (server-rendered HTML for the editor overlay) ------------------
#
# `render_manager` lists calcs per dataset with edit/delete; `render_form`
# is the add/edit form. Both are pure HTML strings (autoescaped by hand) so they
# unit-test without the web stack, mirroring `config_edit.build_form`.


def _summary(definition: dict) -> str:
    """A one-line human description of a calc, e.g. `sum(amount)` or
    `= revenue / orders_count`."""
    if "agg" in definition:
        agg = definition.get("agg", "count")
        formula = str(definition.get("formula") or "").strip()
        body = f"{agg}({formula})" if formula else f"{agg}()"
    else:
        body = f"= {definition.get('formula', '')}"
    fmt = definition.get("format")
    return f"{body}  ·  {fmt}" if fmt else body


def render_manager(text: str) -> str:
    """The calcs overlay body: each of the dashboard's datasets, its calcs,
    and add/edit/delete controls."""
    block = _load(text).get("calcs") or {}
    datasets = chart_datasets(text)
    # Include any dataset that has calcs but no chart (edge case).
    for ds in block:
        if ds not in datasets:
            datasets.append(ds)

    if not datasets:
        return (
            '<p class="ff-calcs-empty">Add a chart with a dataset first, then '
            'define calcs for it here.</p>'
        )

    out = ['<div class="ff-calcs-list">']
    for ds in datasets:
        entries = block.get(ds) or {}
        ds_attr = escape(ds, quote=True)
        out.append('<section class="ff-calcs-ds">')
        out.append(
            '<div class="ff-calcs-ds-head">'
            f'<span class="ff-calcs-ds-name">{escape(ds)}</span>'
            f'<button type="button" class="ff-calcs-add" data-dataset="{ds_attr}">'
            '+ calc</button></div>'
        )
        if not entries:
            out.append('<p class="ff-calcs-empty">No calcs yet.</p>')
        for key, definition in entries.items():
            k_attr = escape(str(key), quote=True)
            out.append(
                '<div class="ff-calcs-row">'
                f'<span class="ff-calcs-key">{escape(str(key))}</span>'
                f'<span class="ff-calcs-sum">{escape(_summary(definition or {}))}</span>'
                '<span class="ff-calcs-acts">'
                f'<button type="button" class="ff-calcs-edit" '
                f'data-dataset="{ds_attr}" data-key="{k_attr}">Edit</button>'
                f'<button type="button" class="ff-calcs-del" '
                f'data-dataset="{ds_attr}" data-key="{k_attr}">Delete</button>'
                '</span></div>'
            )
        out.append('</section>')
    out.append('</div>')
    return "".join(out)


def _text_field(name: str, label: str, value: str, placeholder: str = "") -> str:
    ph = f' placeholder="{escape(placeholder, quote=True)}"' if placeholder else ""
    return (
        f'<div class="ff-field"><label class="ff-field-label">{escape(label)}</label>'
        f'<input class="ff-input" type="text" name="{escape(name, quote=True)}" '
        f'value="{escape(value, quote=True)}"{ph}></div>'
    )


def _select_field(name: str, label: str, options, current: str) -> str:
    opts = "".join(
        f'<option value="{escape(o, quote=True)}"'
        f'{" selected" if o == current else ""}>{escape(o)}</option>'
        for o in options
    )
    return (
        f'<div class="ff-field"><label class="ff-field-label">{escape(label)}</label>'
        f'<select class="ff-input" name="{escape(name, quote=True)}">{opts}</select></div>'
    )


def render_form(text: str, dataset: str, key: str = "", columns=None) -> str:
    """The add/edit calc form. Prefilled from the existing calc when
    `key` names one. `columns` populates the filter builder's column dropdowns."""
    definition = list_for_dataset(text, dataset).get(key, {}) if key else {}
    kind = "formula" if "agg" not in definition and "formula" in definition else "aggregate"
    datasets = chart_datasets(text)
    if dataset and dataset not in datasets:
        datasets = [dataset, *datasets]

    filters_widget = FilterListParam("filters", "Filters").render(
        definition.get("filters"), ParamContext(columns=list(columns or []))
    )

    heading = "Edit calc" if key else "New calc"
    # `data-param-hide` fields (agg + filters) are shown only for aggregate
    # calcs — a bare formula has neither; the kind dropdown toggles `data-kind`
    # on the form (see editor JS).
    fields = [
        f'<input type="hidden" name="original_key" value="{escape(key, quote=True)}">',
        _select_field("dataset", "Dataset", datasets, dataset),
        _text_field("key", "Key", key, "e.g. revenue"),
        _select_field("kind", "Kind", ["aggregate", "formula"], kind),
        f'<div data-param-hide>{_select_field("agg", "Aggregation", list(AGGS), definition.get("agg", "count"))}</div>',
        _text_field(
            "formula", "Formula", str(definition.get("formula") or ""),
            "aggregate: amount or price * qty · over calcs: revenue / orders_count · "
            "over columns: str2dt(order_date, YYYYMMDD)",
        ),
        _text_field("name", "Name", str(definition.get("name") or "")),
        _text_field("description", "Description", str(definition.get("description") or "")),
        _text_field("format", "Format", str(definition.get("format") or ""), "e.g. 0.00$ · 0.0a $ (23.4k) · 0.0%"),
        f'<div data-param-hide>{filters_widget}</div>',
    ]
    return (
        f'<form class="ff-calcs-form" data-kind="{kind}">'
        f'<div class="ff-calcs-form-head">{escape(heading)}</div>'
        + "".join(fields)
        + '<div class="ff-modal-error" hidden></div>'
        '<div class="ff-calcs-form-foot">'
        '<button type="button" class="ff-calcs-cancel">Cancel</button>'
        '<button type="submit" class="run">Save</button>'
        '</div></form>'
    )
