"""A dataset's column mapping: the columns charts see, each a formula over the
uploaded ones.

An upload is read with every column as text — a CSV says nothing about types,
and guessing quietly turns `007` into `7`. The mapping then lists the columns
the dataset has, each with a formula over the uploaded columns, in the calc
grammar (`+ - * /`, `str2dt()`, `to_number()`). A column's type is whatever its
formula gives: text as uploaded, a number from `to_number()`, a date from
`str2dt()`. It's YAML, edited in a form or by hand:

    columns:
      id:
        formula: to_number(id)
      order_status:
        formula: status
      total:
        formula: to_number(unit_price) * to_number(qty)

A column's key is its name; `formula` defaults to the column of that name, and
`name: formula` is the short form. Columns can be renamed (a new key over the
old formula), dropped (left out) or added (a formula over several). Names are
any text, in any script.

`service: true` marks a **service column**: part of the dataset — on its page,
with its type and values — but never for dashboards. It's built, then left out
of the data charts read, so nothing can use it.

`permissions:` names the roles that may read the dataset — each `full`, or the
filters that narrow the rows it sees; a role not named may not. Part of the
mapping, so it's versioned with it: a change is the next minor, compared and
restored like the columns.

    permissions:
      viewer:
        - {column: order_status, op: in, values: [paid]}
      admin: full

Pure functions over Polars frames — the dataset store calls them, and they
test without any storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl
import yaml

from fireflyer import filters as filters_mod
from fireflyer.calcs import CalcError, compile_formula, formula_names

# Polars dtypes of an older, type-guessed dataset that were numbers.
_NUMERIC_DTYPES = ("Int", "UInt", "Float", "Decimal")
# The types a mapping could name before formulas decided them.
_OLD_TYPES = ("string", "numeric", "integer", "float", "boolean", "date", "datetime")
_OLD_NUMERIC = ("numeric", "integer", "float")
# A role's permission that narrows nothing.
FULL = "full"
_TO_NUMBER_ARG = re.compile(r"\bto_number\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")


class MappingError(ValueError):
    """A mapping that can't be read, or that doesn't fit the upload."""


@dataclass(frozen=True)
class Rule:
    name: str
    formula: str
    service: bool = False             # in the dataset, never in what charts read


# --- reading and writing ------------------------------------------------------


def default(sources: list[str], previous: list[Rule] | None = None,
            previous_sources: list[str] | None = None) -> list[Rule]:
    """The mapping for a fresh upload: every uploaded column as it is — or,
    re-uploading, the previous mapping carried forward: its columns whose
    formulas still find what they read, then the upload's new columns as they
    are (one dropped before stays dropped)."""
    if previous is None:
        return [Rule(s, s) for s in sources]
    have = set(sources)
    kept = [r for r in previous if formula_names(r.formula) <= have]
    seen = set(previous_sources or [])
    taken = {r.name for r in kept}
    return kept + [Rule(s, s) for s in sources if s not in seen and s not in taken]


def from_dtypes(columns: list[tuple[str, str]]) -> list[Rule]:
    """The mapping that keeps a type-guessed dataset's numbers numbers."""
    return [Rule(n, f"to_number({n})" if d.startswith(_NUMERIC_DTYPES) else n) for n, d in columns]


def parse(text: str, sources: list[str]) -> list[Rule]:
    """The mapping in `text`, checked against the upload's `sources`."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise MappingError(f"not valid YAML: {exc}") from exc
    columns = doc.get("columns") if isinstance(doc, dict) else None
    if not isinstance(columns, dict) or not columns:
        raise MappingError("the mapping needs a `columns:` block, one entry per column")
    rules = [_rule(str(name), spec) for name, spec in columns.items()]
    _permissions(doc.get("permissions"))          # refuse a bad one here, where it's typed
    have = set(sources)
    for r in rules:
        missing = sorted(formula_names(r.formula) - have)
        if missing:
            raise MappingError(
                f"column {r.name!r}: no column {missing[0]!r} in the upload (it has: {', '.join(sources)})"
            )
    return rules


def dump(rules: list[Rule], permissions: dict[str, list[dict]] | None = None) -> str:
    """`rules` as the YAML `parse` reads: each column's formula on its own line
    — then `permissions` (role -> filters, `[]` for full), each filter on one."""
    lines = ["columns:"]
    for r in rules:
        lines += [f"  {_scalar(r.name)}:", f"    formula: {_scalar(r.formula)}"]
        if r.service:
            lines.append("    service: true")
    if permissions:
        lines.append("permissions:")
        for role, filters in permissions.items():
            if not filters:
                lines.append(f"  {_scalar(role)}: {FULL}")
                continue
            lines.append(f"  {_scalar(role)}:")
            lines += [f"    - {_flow(f)}" for f in filters]
    return "\n".join(lines) + "\n"


def permissions(text: str) -> dict[str, list[dict]]:
    """The mapping's `permissions:` — role name -> its filters (`[]`: full).
    Empty when there are none, or the YAML doesn't read."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    return _permissions(doc.get("permissions")) if isinstance(doc, dict) else {}


def with_permissions(text: str, grants: dict[str, list[dict]]) -> str:
    """`text` with its `permissions:` replaced by `grants` — its columns as
    they are."""
    doc = yaml.safe_load(text) or {}
    columns = doc.get("columns") if isinstance(doc, dict) else None
    rules = [_rule(str(n), spec) for n, spec in (columns or {}).items()]
    return dump(rules, _permissions(grants))


def _permissions(raw) -> dict[str, list[dict]]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise MappingError("`permissions:` maps each role to `full` or a list of filters")
    out = {}
    for role, spec in raw.items():
        if spec in (FULL, [], None):
            out[str(role)] = []
            continue
        try:
            out[str(role)] = [f.as_dict() for f in filters_mod.normalize(spec)]
        except filters_mod.FilterError as exc:
            raise MappingError(f"permissions of {role!r}: {exc}") from exc
    return out


def _flow(f: dict) -> str:
    """A filter on one line — `{column: status, op: in, values: [paid]}` — in
    its own script."""
    return yaml.safe_dump(f, default_flow_style=True, width=10_000, allow_unicode=True,
                          sort_keys=False).strip()


def renames(before: list[Rule], after: list[Rule]) -> dict[str, str]:
    """Old name -> new name, for each column now called something else — the
    same formula under a new key — which the dashboards using it need rewriting."""
    now = {r.name for r in after}
    by_formula = {r.formula: r.name for r in after}
    return {
        r.name: by_formula[r.formula]
        for r in before
        if r.name not in now and r.formula in by_formula
    }


def upgrade(text: str) -> str:
    """A stored mapping in today's terms and layout. Before formulas a mapping
    keyed the *uploaded* column and gave it a `type` (`numeric`…) and maybe a
    `name`; that becomes the column `name` with formula `<uploaded>` — wrapped
    in `to_number()` when it was numeric. Unreadable text comes back as is."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    columns = doc.get("columns") if isinstance(doc, dict) else None
    if not isinstance(columns, dict):
        return text
    rules = []
    for key, spec in columns.items():
        key = str(key)
        if isinstance(spec, str) and spec in _OLD_TYPES:
            spec = {"type": spec}
        if isinstance(spec, dict) and "formula" not in spec and set(spec) & {"type", "format", "name"}:
            numeric = str(spec.get("type") or "") in _OLD_NUMERIC
            rules.append(Rule(str(spec.get("name") or key), f"to_number({key})" if numeric else key))
            continue
        try:
            rules.append(_rule(key, spec))
        except MappingError:
            return text
    try:
        return dump(rules, _permissions(doc.get("permissions")))
    except MappingError:
        return text


def _rule(name: str, spec) -> Rule:
    if spec is None:
        return Rule(name, name)
    if isinstance(spec, str):
        spec = {"formula": spec}
    if not isinstance(spec, dict):
        raise MappingError(f"column {name!r}: expected a formula, or {{formula: …}}")
    extra = set(spec) - {"formula", "service"}
    if extra:
        hint = (" — a column's type comes from its formula now: to_number(), str2dt()"
                if extra & {"type", "format"} else "")
        raise MappingError(f"column {name!r}: unknown key {sorted(extra)[0]!r} (takes formula, service){hint}")
    service = spec.get("service", False)
    if not isinstance(service, bool):
        raise MappingError(f"column {name!r}: `service` is true or false")
    formula = str(spec.get("formula") or name).strip()
    try:
        compile_formula(formula, pl.col)
    except CalcError as exc:
        raise MappingError(f"column {name!r}: {exc}") from exc
    return Rule(name, formula, service)


def _scalar(text: str) -> str:
    """`text` as a YAML scalar — quoted only when it has to be, and in its own
    script (`Підрозділ`, not `\\u041F…`)."""
    dumped = yaml.safe_dump(text, default_flow_style=True, width=10_000, allow_unicode=True).strip()
    return dumped.removesuffix("\n...").removesuffix("...").strip()


# --- applying -----------------------------------------------------------------


def as_text(df: pl.DataFrame) -> pl.DataFrame:
    """Every column as text — how a mapping sees an upload, whatever its file."""
    return df.with_columns(pl.all().cast(pl.String))


def examples(df: pl.DataFrame, n: int = 5) -> dict[str, list[str]]:
    """Up to `n` distinct values per column, in the order they first appear —
    what the page shows beside each column."""
    out = {}
    for column in df.columns:
        values = as_text(df.select(column)).to_series().drop_nulls()
        out[column] = values.filter(values.str.strip_chars() != "").unique(maintain_order=True).head(n).to_list()
    return out


def apply(df: pl.DataFrame, rules: list[Rule]) -> pl.DataFrame:
    """`df` (all text) read through `rules`: each column its formula's result.
    A value `to_number()` can't read is an error, not a quiet null — the message
    names the column, how many values and one of them — and so is a formula
    that can't work on what it's given (arithmetic on text)."""
    problems = _unreadable_numbers(df, rules)
    if problems:
        raise MappingError("; ".join(problems[:3]))
    columns = []
    for r in rules:
        expr, _ = compile_formula(r.formula, pl.col)
        try:
            column = df.select(expr.alias(r.name)).to_series()
        except pl.exceptions.PolarsError as exc:
            hint = " — wrap a column in to_number() to do arithmetic on it" if "str" in str(exc).lower() else ""
            raise MappingError(f"column {r.name!r} ({r.formula}): {str(exc).splitlines()[0]}{hint}") from exc
        columns.append(_narrow(column) if column.dtype == pl.Float64 else column)
    return pl.DataFrame(columns)


def _unreadable_numbers(df: pl.DataFrame, rules: list[Rule]) -> list[str]:
    """For each column `to_number()` reads, the values it can't."""
    out = []
    for source in sorted({m for r in rules for m in _TO_NUMBER_ARG.findall(r.formula)}):
        if source not in df.columns:
            continue
        text = df.get_column(source).cast(pl.String).str.strip_chars()
        bad = text.is_not_null() & (text != "") & text.cast(pl.Float64, strict=False).is_null()
        if bad.any():
            sample = df.get_column(source).filter(bad).head(1).item()
            out.append(f"column {source!r}: {int(bad.sum())} value(s) aren't numbers — e.g. {sample!r}")
    return out


def _narrow(number: pl.Series) -> pl.Series:
    """Whole numbers as integers — an id stays `7`, not `7.0`."""
    values = number.drop_nulls()
    if values.is_empty():
        return number.cast(pl.Int64)
    # Past 2**53 a float no longer holds every whole number exactly.
    whole = bool(values.is_finite().all()) and bool((values == values.round(0)).all())
    return number.cast(pl.Int64) if whole and values.abs().max() < 2**53 else number
