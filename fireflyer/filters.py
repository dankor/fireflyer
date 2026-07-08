"""Filter primitives shared by every chart.

A filter is a small declarative shape: `{column, op, values}` where `op` is
`in` or `ni`. Filters AND together; an empty list passes every row. The same
shape powers the Python `filters=...` argument, the YAML `filters:` block,
and dashboard crossfilters — see architecture.md, section "Filters".
"""

from dataclasses import dataclass
from typing import Any, Iterable

import polars as pl

OPS = ("in", "ni")


class FilterError(ValueError):
    """Raised when a filter declaration is malformed."""


@dataclass(frozen=True)
class Filter:
    column: str
    op: str
    values: tuple[Any, ...]

    def as_dict(self) -> dict:
        return {"column": self.column, "op": self.op, "values": list(self.values)}


def normalize(raw) -> list[Filter]:
    """Validate `[{column, op, values}, ...]` and return Filter objects."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FilterError(f"filters must be a list, got {type(raw).__name__}")
    out = []
    for i, f in enumerate(raw):
        if isinstance(f, Filter):
            out.append(f)
            continue
        if not isinstance(f, dict):
            raise FilterError(f"filter #{i}: must be a mapping")
        missing = [k for k in ("column", "op", "values") if k not in f]
        if missing:
            raise FilterError(f"filter #{i}: missing key(s) {missing}")
        column, op, values = f["column"], f["op"], f["values"]
        if not isinstance(column, str) or not column:
            raise FilterError(f"filter #{i}: column must be a non-empty string")
        if op not in OPS:
            raise FilterError(
                f"filter #{i}: op must be one of {list(OPS)}, got {op!r}"
            )
        if not isinstance(values, (list, tuple)) or not values:
            raise FilterError(f"filter #{i}: values must be a non-empty list")
        out.append(Filter(column=column, op=op, values=tuple(values)))
    return out


def apply(df: pl.DataFrame, filters: Iterable[Filter]) -> pl.DataFrame:
    """AND-apply each filter to `df`. Filters on absent columns are skipped.

    Skipping (rather than erroring) is required for crossfilters: a click in
    one chart emits a filter that other charts may not be able to apply if
    their dataset lacks that column. The architecture spec calls this out.
    """
    for f in filters:
        if f.column not in df.columns:
            continue
        # Stringifying both sides keeps comparison consistent — crossfilter
        # values always arrive as strings (URL form data), declared filters
        # can be any literal in YAML/Python.
        col = pl.col(f.column).cast(pl.String, strict=False)
        targets = [str(v) for v in f.values]
        if f.op == "in":
            df = df.filter(col.is_in(targets))
        else:  # "ni"
            df = df.filter(~col.is_in(targets))
    return df


# --- Crossfilter URL tokens ----------------------------------------------------
#
# Crossfilters travel between client and server as a flat list of
# `<emitter>|<column>=<value>` strings. The emitter is the dashboard id of the
# chart that produced the click — needed so the source chart can be exempted
# from its own crossfilter at render time. When decoding to
# Filter objects, tokens emitted by the same chart that's about to render are
# dropped; the rest group by column into single `in` filters with multiple
# values. `|` separates emitter from rest; the first `=` after that separates
# column from value, so values can themselves contain `=` safely.


def decode_tokens(tokens: Iterable[str], exclude_emitter: str | None = None) -> list[Filter]:
    """Tokens → Filter list. Drops tokens emitted by `exclude_emitter`."""
    by_column: dict[str, list[str]] = {}
    for tok in tokens:
        if "|" not in tok:
            continue
        emitter, rest = tok.split("|", 1)
        if exclude_emitter is not None and emitter == exclude_emitter:
            continue
        if "=" not in rest:
            continue
        col, _, val = rest.partition("=")
        if col:
            by_column.setdefault(col, []).append(val)
    return [Filter(column=c, op="in", values=tuple(vs)) for c, vs in by_column.items()]


def emitted_by(tokens: Iterable[str], emitter: str) -> list[Filter]:
    """Filters tagged with `emitter` — used to surface a chart's own selection
    as a "this chart is filtering others" indicator (red state in the UI)."""
    by_column: dict[str, list[str]] = {}
    for tok in tokens:
        if "|" not in tok:
            continue
        e, rest = tok.split("|", 1)
        if e != emitter or "=" not in rest:
            continue
        col, _, val = rest.partition("=")
        if col:
            by_column.setdefault(col, []).append(val)
    return [Filter(column=c, op="in", values=tuple(vs)) for c, vs in by_column.items()]


def active_values_for(
    tokens: Iterable[str], emitter: str, column: str
) -> set[str]:
    """Values currently selected by `emitter` on `column` — drives visual state."""
    out: set[str] = set()
    for tok in tokens:
        if "|" not in tok:
            continue
        e, rest = tok.split("|", 1)
        if e != emitter or "=" not in rest:
            continue
        col, _, val = rest.partition("=")
        if col == column:
            out.add(val)
    return out


def toggle_token(tokens: list[str], token: str) -> list[str]:
    """Add `token` if absent, remove if present. Click-to-toggle semantics."""
    if token in tokens:
        return [t for t in tokens if t != token]
    return [*tokens, token]
