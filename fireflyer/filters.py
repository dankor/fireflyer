"""Filter primitives shared by every chart.

A filter is a small declarative shape: `{column, op, values}` where `op` is
`in`, `ni` (not-in) or `between`. Filters AND together; an empty list passes
every row. The same shape powers the Python `filters=...` argument, the YAML
`filters:` block, and dashboard crossfilters — see architecture.md, "Filters".

`between` takes exactly two values and is **half-open** — `lo <= v < hi`. It
exists because a bucketed axis is a *range*: a bar labelled `2026-06` covers
every date in June, which no list of exact values can express (see the bar
chart's spec). Half-open is what makes adjacent buckets tile without overlap.
"""

import base64
import json
import re
import zlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

import polars as pl

OPS = ("in", "ni", "between")
# `between` is a range, so it needs exactly a low and a high bound.
_BETWEEN_ARITY = 2


class FilterError(ValueError):
    """Raised when a filter declaration is malformed."""


@dataclass(frozen=True)
class Filter:
    column: str
    op: str
    values: tuple[Any, ...]

    def as_dict(self) -> dict:
        return {"column": self.column, "op": self.op, "values": list(self.values)}

    @property
    def values_text(self) -> str:
        """The values as the filter indicator shows them. A `between` reads as
        `low–high` with a midnight time trimmed off each bound: a bucket edge is
        midnight by construction, so `2026-02-01 00:00:00+00:00` says nothing the
        date doesn't, and two of them overflowed the tooltip.

        Whole days read as the days they cover — `2026-02-01–2026-02-28`, not
        the exclusive `…–2026-03-01` the model stores — matching the inclusive
        "to" of the date picker that sets them; a single day reads as itself.
        Display only — the stored values still round-trip exactly."""
        if self.op != "between":
            return ", ".join(str(v) for v in self.values)
        days = day_range(self.values)
        if days is None:
            return "–".join(_drop_midnight(v) for v in self.values)
        first, last = days
        return first if first == last else f"{first}–{last}"


# A time of exactly midnight, optionally UTC. A non-UTC offset is left alone:
# `2026-02-01 00:00:00+03:00` is not the same instant as `2026-02-01`.
_MIDNIGHT_RE = re.compile(r"[ T]00:00:00(\.0+)?(\+00:00|Z)?$")


def _drop_midnight(value) -> str:
    return _MIDNIGHT_RE.sub("", str(value))


# --- whole-day ranges ---------------------------------------------------------
#
# `between` is half-open, which is right for the model (adjacent ranges tile)
# and wrong for a person: "June" is 1–30 June, not 1 June up to 1 July. A date
# picker therefore takes an inclusive last day, and these convert at the edge.

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_range(values) -> tuple[str, str] | None:
    """Inclusive `(first_day, last_day)` for a half-open `between` whose bounds
    are whole days (a midnight time is allowed), or None when they aren't — a
    range with a real time in it has no day-picker form."""
    if len(values) != 2:
        return None
    low, high = (_drop_midnight(v) for v in values)
    if not (_DAY_RE.match(low) and _DAY_RE.match(high)):
        return None
    try:
        last = date.fromisoformat(high) - timedelta(days=1)
    except ValueError:
        return None
    return low, last.isoformat()


def day_bounds(first: str, last: str) -> list[str] | None:
    """The half-open `[first, last + 1 day)` bounds for an inclusive pair of
    picked days, or None when either isn't a date."""
    try:
        date.fromisoformat(first)
        end = date.fromisoformat(last) + timedelta(days=1)
    except ValueError:
        return None
    return [first, end.isoformat()]


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
        if op == "between" and len(values) != _BETWEEN_ARITY:
            raise FilterError(
                f"filter #{i}: `between` takes exactly two values "
                f"(low, high), got {len(values)}"
            )
        out.append(Filter(column=column, op=op, values=tuple(values)))
    return out


def apply(df: pl.DataFrame, filters: Iterable[Filter]) -> pl.DataFrame:
    """AND-apply each filter to `df`. Filters on absent columns are skipped.

    Skipping (rather than erroring) is required for crossfilters: a click in
    one chart emits a filter that other charts may not be able to apply if
    their dataset lacks that column. The architecture spec calls this out.
    """
    preds = predicates(filters, df.columns)
    return df.filter(*preds) if preds else df


def predicates(filters: Iterable[Filter], columns: Iterable[str]) -> list[pl.Expr]:
    """Polars filter expressions for the filters whose column exists in
    `columns` — the lazy-scan counterpart to `apply`, so charts can push
    predicates down into `scan_parquet`. Absent-column filters are skipped
    (crossfilter contract). Passing an empty list to `.filter(*[])` is a no-op.
    """
    have = set(columns)
    exprs: list[pl.Expr] = []
    for f in filters:
        if f.column not in have:
            continue
        # Stringify both sides — crossfilter values arrive as strings (URL form
        # data); declared filters can be any literal in YAML/Python.
        col = pl.col(f.column).cast(pl.String, strict=False)
        targets = [str(v) for v in f.values]
        if f.op == "between":
            exprs.append(_between(f.column, col, *targets))
        else:
            exprs.append(col.is_in(targets) if f.op == "in" else ~col.is_in(targets))
    return exprs


def _between(column: str, as_text: pl.Expr, low: str, high: str) -> pl.Expr:
    """Half-open `low <= v < high`.

    Compared as **numbers** when both bounds are numeric, as text otherwise.
    Text works for the case this exists for — ISO-8601 dates sort
    chronologically as strings — but `"10" < "9"` lexicographically, so numeric
    bounds have to opt out of the stringify-both-sides rule the other ops use.
    """
    if _is_number(low) and _is_number(high):
        as_number = pl.col(column).cast(pl.Float64, strict=False)
        return (as_number >= float(low)) & (as_number < float(high))
    return (as_text >= low) & (as_text < high)


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


# --- Crossfilter URL tokens ----------------------------------------------------
#
# A crossfilter travels between client and server as a flat list of strings:
#
#     token := emitter "|" part ("|" part)*
#     part  := column "=" value              # an `in` filter
#            | column "~" low "~" high       # a half-open `between` filter
#
# The emitter is the dashboard id of the chart that produced the click — needed
# so the source chart can be exempted from its own crossfilter at render time.
#
# **One token is one selection.** A click that means "this bucket *and* this
# series" (a bar segment) carries both parts, so it toggles as a unit: clicking
# the same segment again clears both halves, never half of one. Parts of
# different tokens still group by column into a single `in` filter, so separate
# clicks accumulate as before.
#
# A part is an `in` filter when it contains `=`, since the `between` parts we
# generate are dates and never do. That order matters: a *value* may contain
# `~` (`status=a~b`), so `=` has to be tested first.
#
# **Global quick filters** ride the same channel under the reserved emitter `*`,
# which no chart id can be. Their part is the whole filter as JSON instead:
# they are typed, not clicked, so they need `ni` and arbitrary value text that
# the click syntax can't carry. A global filter is never exempted from any
# chart (no chart is its emitter), and it is never merged into another
# source's `in` filter — it ANDs with them, as a filter you typed should.

_IN_SEP = "="
_RANGE_SEP = "~"
GLOBAL = "*"


def _parts(token: str, emitter_filter=None) -> list[tuple[str, str, tuple]]:
    """`(column, op, values)` for each part of a token, or [] when the token is
    malformed or its emitter is filtered out. `emitter_filter(emitter)` returns
    True to keep."""
    if "|" not in token:
        return []
    emitter, _, rest = token.partition("|")
    if emitter_filter is not None and not emitter_filter(emitter):
        return []
    if emitter == GLOBAL:
        return _global_parts(rest)
    out = []
    for part in rest.split("|"):
        if _IN_SEP in part:
            column, _, value = part.partition(_IN_SEP)
            if column:
                out.append((column, "in", (value,)))
        elif _RANGE_SEP in part:
            column, _, bounds = part.partition(_RANGE_SEP)
            low, sep, high = bounds.partition(_RANGE_SEP)
            if column and sep:
                out.append((column, "between", (low, high)))
    return out


def _global_parts(payload: str) -> list[tuple[str, str, tuple]]:
    """The one filter a global token carries, or [] when it doesn't parse — a
    hand-edited URL must not take the dashboard down."""
    try:
        (f,) = normalize([json.loads(payload)])
    except (ValueError, FilterError):
        return []
    return [(f.column, f.op, f.values)]


def _collect(tokens: Iterable[str], emitter_filter) -> list[Filter]:
    """Parts → Filters. Clicked `in` parts on one column merge into a single
    multi-value filter (so separate clicks accumulate); `between` parts stay
    separate, since two ranges are an OR the model can't express and shouldn't
    silently fake. Global filters stay separate too (see the token notes)."""
    merged: dict[str, list[str]] = {}
    separate: list[Filter] = []
    for token in tokens:
        is_global = token.partition("|")[0] == GLOBAL
        for column, op, values in _parts(token, emitter_filter):
            if op == "in" and not is_global:
                merged.setdefault(column, []).extend(values)
            else:
                separate.append(Filter(column=column, op=op, values=values))
    return [
        Filter(column=c, op="in", values=tuple(vs)) for c, vs in merged.items()
    ] + separate


def decode_tokens(tokens: Iterable[str], exclude_emitter: str | None = None) -> list[Filter]:
    """Tokens → Filter list. Drops tokens emitted by `exclude_emitter`."""
    return _collect(tokens, lambda e: e != exclude_emitter)


def global_token(column: str, op: str, values) -> str:
    """The token for a global quick filter typed into a filter panel."""
    payload = {"column": column, "op": op, "values": [str(v) for v in values]}
    return GLOBAL + "|" + json.dumps(payload, separators=(",", ":"))


def global_filters(tokens: Iterable[str]) -> list[tuple[str, Filter]]:
    """`(token, filter)` for each global quick filter. The token comes along
    because removing one means toggling exactly that token off."""
    out = []
    for token in tokens:
        if token.partition("|")[0] == GLOBAL:
            out += [(token, Filter(c, op, vs)) for c, op, vs in _parts(token)]
    return out


def by_emitter(tokens: Iterable[str]) -> list[tuple[str, Filter]]:
    """`(emitter, filter)` pairs — who set each filter, not just what it says.

    `decode_tokens` throws the emitter away, which is right for *applying* a
    filter (the chart doesn't care where it came from) and wrong for showing
    one: a viewer looking at a narrowed chart needs to know which chart to click
    to undo it. Grouped per emitter first, so one chart's repeated clicks on a
    column still merge into a single multi-value filter.
    """
    groups: dict[str, list[str]] = {}
    for token in tokens:
        groups.setdefault(token.partition("|")[0], []).append(token)
    return [
        (emitter, f)
        for emitter, group in groups.items()
        for f in _collect(group, lambda _: True)
    ]


def tokens_behind(tokens: Iterable[str], emitter: str, f: Filter) -> list[str]:
    """The tokens that make up `f` as `by_emitter` reported it — what removing
    that filter has to drop. Clicked `in` values on a column merge across
    clicks, so that is every token of `emitter` with an `in` part on the
    column; anything else (a range, a global filter) is one exact part. A
    multi-part token goes whole: it is one selection, the same unit a second
    click on it would clear."""
    def builds(column, op, values):
        if column != f.column or op != f.op:
            return False
        merged = op == "in" and emitter != GLOBAL
        return merged or values == f.values

    return [
        t for t in tokens
        if t.partition("|")[0] == emitter and any(builds(*p) for p in _parts(t))
    ]


def emitted_by(tokens: Iterable[str], emitter: str) -> list[Filter]:
    """Filters tagged with `emitter` — used to surface a chart's own selection
    as a "this chart is filtering others" indicator (red state in the UI)."""
    return _collect(tokens, lambda e: e == emitter)


def active_values_for(
    tokens: Iterable[str], emitter: str, column: str
) -> set[str]:
    """Values currently selected by `emitter` on `column` — drives visual state.
    A `between` part reports its low bound, which is the bucket a bar segment
    was clicked on, so the segment lights up again on re-render."""
    out: set[str] = set()
    for token in tokens:
        for col, _, values in _parts(token, lambda e: e == emitter):
            if col == column:
                out.add(values[0])
    return out


def tokens_for(tokens: Iterable[str], emitter: str) -> set[str]:
    """The raw tokens this chart emitted. A chart highlights its own selection by
    matching whole tokens, not values: a multi-part token means "this bucket AND
    this series", so comparing values alone would light every bucket of that
    series."""
    return {t for t in tokens if t.partition("|")[0] == emitter and "|" in t}


def toggle_token(tokens: list[str], token: str) -> list[str]:
    """Add `token` if absent, remove if present. Click-to-toggle semantics — and
    because a token holds a whole selection, a multi-part one toggles as a unit."""
    if token in tokens:
        return [t for t in tokens if t != token]
    return [*tokens, token]


def range_part(column: str, low, high) -> str:
    """A `between` token part. Kept here so charts don't hand-build token
    syntax."""
    return f"{column}{_RANGE_SEP}{low}{_RANGE_SEP}{high}"


def value_part(column: str, value) -> str:
    """An `in` token part."""
    return f"{column}{_IN_SEP}{value}"


# --- URL state -------------------------------------------------------------------
#
# Every token in play — crossfilter clicks and global filters alike — rides in the
# page URL as one `f` query parameter, so a reload or a shared link reopens the
# same view. A global filter's JSON quoted into a URL grows fast, and browsers and
# proxies cut long URLs off, so the list is always compressed: JSON → zlib →
# base64url. One format, reversible, no length threshold to get wrong.


def encode_state(tokens: Iterable[str]) -> str:
    """The `f` parameter for `tokens`; "" when there are none."""
    tokens = list(tokens)
    if not tokens:
        return ""
    packed = zlib.compress(json.dumps(tokens, separators=(",", ":")).encode(), 9)
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def decode_state(text: str) -> list[str]:
    """Tokens from an `f` parameter. Anything that doesn't decode is an empty
    state — a truncated or hand-edited link opens the dashboard unfiltered
    rather than not at all."""
    if not text:
        return []
    try:
        packed = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        tokens = json.loads(zlib.decompress(packed))
    except (ValueError, zlib.error):
        return []
    if not isinstance(tokens, list):
        return []
    return [t for t in tokens if isinstance(t, str)]
