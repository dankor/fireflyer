"""Dashboard measures — named aggregations over a dataset.

A dashboard declares a top-level `measures:` block **keyed by dataset**; each
measure has a stable key, a definition, and display metadata. Charts no longer
carry an `agg`/`value` pair — they name a measure and supply the dimension to
break it down by. See architecture.md, "Measures".

A measure is one of two kinds, told apart by whether `agg` is present:

- **Aggregate** — `agg` + a row-level `formula` (an expression over *columns*),
  reduced per group. A bare column (`amount`) is the trivial case; `price * qty`
  is the pre-aggregate ("calculated column") case. Optional `filters` pre-narrow
  the rows this measure sees (that's how conditional aggregation is expressed).
- **Derived** — a `formula` over *other measure keys*, no `agg`. Computed per
  group after the aggregates, so `revenue / orders_count` is a true per-group
  ratio. Its leaves must be aggregate measures (no derived-of-derived).

Both formula kinds allow `+ - * /`, parentheses and numeric literals only — the
same tiny expression grammar, the only difference being whether a bare name
resolves to a column (aggregate) or to a sibling measure (derived).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import reduce

import polars as pl

from fireflyer import filters as filters_mod

# The reductions a measure's `agg` may name. `count` needs no formula (row
# count); the rest reduce the measure's row-level formula. Deliberately small —
# no median/percentile (non-trivial, pushdown-hostile).
AGGS = ("count", "sum", "dcount", "min", "max", "avg")

# Internal column name for the computed measure value in an aggregated frame.
VALUE = "__value__"


class MeasureError(ValueError):
    """Raised for any malformed measure — message is shown to the user."""


# --- Expression grammar -------------------------------------------------------
#
# One recursive-descent parser turns a formula string into a polars expression.
# Identifiers are handed to a `resolve` callback: for a row-level formula they
# become `pl.col(<column>)`, for a derived formula `pl.col(<leaf measure>)` —
# same grammar, different leaf meaning. Numbers are literals; `+ - * /`, parens
# and unary minus compose them. No function calls, no comparisons — aggregation
# is expressed by `agg:`, not inside the formula.

_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[A-Za-z_]\w*|[-+*/()]|\S")


def _tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text)
    for tok in tokens:
        if not re.fullmatch(r"\d+(?:\.\d+)?|[A-Za-z_]\w*|[-+*/()]", tok):
            raise MeasureError(f"bad character {tok!r} in formula {text!r}")
    return tokens


class _Parser:
    """expr := term (('+'|'-') term)* ; term := factor (('*'|'/') factor)* ;
    factor := number | name | '(' expr ')' | '-' factor."""

    def __init__(self, tokens: list[str], resolve):
        self.tokens = tokens
        self.pos = 0
        self.resolve = resolve
        self.names: set[str] = set()

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _eat(self) -> str:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> pl.Expr:
        if not self.tokens:
            raise MeasureError("empty formula")
        expr = self._expr()
        if self.pos != len(self.tokens):
            raise MeasureError(f"unexpected {self._peek()!r} in formula")
        return expr

    def _expr(self) -> pl.Expr:
        expr = self._term()
        while self._peek() in ("+", "-"):
            op = self._eat()
            rhs = self._term()
            expr = expr + rhs if op == "+" else expr - rhs
        return expr

    def _term(self) -> pl.Expr:
        expr = self._factor()
        while self._peek() in ("*", "/"):
            op = self._eat()
            rhs = self._factor()
            expr = expr * rhs if op == "*" else expr / rhs
        return expr

    def _factor(self) -> pl.Expr:
        tok = self._peek()
        if tok is None:
            raise MeasureError("unexpected end of formula")
        if tok == "-":
            self._eat()
            return -self._factor()
        if tok == "(":
            self._eat()
            expr = self._expr()
            if self._peek() != ")":
                raise MeasureError("missing ')' in formula")
            self._eat()
            return expr
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            self._eat()
            return pl.lit(float(tok))
        if re.fullmatch(r"[A-Za-z_]\w*", tok):
            self._eat()
            self.names.add(tok)
            return self.resolve(tok)
        raise MeasureError(f"unexpected {tok!r} in formula")


def compile_formula(text: str, resolve) -> tuple[pl.Expr, set[str]]:
    """Compile a formula to `(expr, referenced_names)`. `resolve(name)` maps a
    bare identifier to a polars expression."""
    parser = _Parser(_tokenize(text), resolve)
    return parser.parse(), parser.names


def formula_names(text: str) -> set[str]:
    """The bare identifiers a formula references, without building an expr."""
    _, names = compile_formula(text, pl.col)
    return names


# --- Measure model ------------------------------------------------------------


@dataclass(frozen=True)
class Measure:
    key: str
    name: str            # display label; falls back to the key
    description: str
    format: str          # `<prefix><0,.pattern><suffix>` token, or "" for default
    agg: str | None      # one of AGGS, or None for a derived measure
    formula: str         # row expr (aggregate) or measure expr (derived); "" ok for count
    filters: tuple       # tuple[Filter, ...] — only meaningful for aggregate measures

    @property
    def is_aggregate(self) -> bool:
        return self.agg is not None


def _parse_measure(key: str, raw) -> Measure:
    if not isinstance(raw, dict):
        raise MeasureError(f"measure {key!r}: must be a mapping")
    name = str(raw.get("name") or key)
    description = str(raw.get("description") or "")
    fmt = str(raw.get("format") or "")

    if "agg" in raw:
        agg = raw["agg"]
        if agg not in AGGS:
            raise MeasureError(
                f"measure {key!r}: agg must be one of {list(AGGS)}, got {agg!r}"
            )
        formula = str(raw.get("formula") or "")
        if agg != "count" and not formula.strip():
            raise MeasureError(f"measure {key!r}: agg {agg!r} needs a `formula`")
        if formula.strip():
            compile_formula(formula, pl.col)  # syntax check
        filters = tuple(filters_mod.normalize(raw.get("filters")))
        return Measure(key, name, description, fmt, agg, formula, filters)

    if "formula" in raw:
        formula = str(raw["formula"]).strip()
        if not formula:
            raise MeasureError(f"measure {key!r}: `formula` is empty")
        if raw.get("filters"):
            raise MeasureError(
                f"measure {key!r}: a derived formula can't take `filters` — put "
                "them on the aggregate measures it references"
            )
        compile_formula(formula, pl.col)  # syntax check
        return Measure(key, name, description, fmt, None, formula, ())

    raise MeasureError(f"measure {key!r}: needs either `agg` or `formula`")


class MeasureSet:
    """The measures for a single dataset, keyed by name. Resolves a measure to
    Polars and to display strings; validates references at construction."""

    def __init__(self, measures: dict[str, Measure]):
        self.measures = measures
        self._validate_refs()

    @classmethod
    def from_defs(cls, defs: dict) -> "MeasureSet":
        if not isinstance(defs, dict):
            raise MeasureError("measures for a dataset must be a mapping of key -> measure")
        return cls({key: _parse_measure(key, raw) for key, raw in defs.items()})

    def __contains__(self, key: str) -> bool:
        return key in self.measures

    def get(self, key: str) -> Measure:
        if key not in self.measures:
            raise MeasureError(f"unknown measure {key!r}")
        return self.measures[key]

    def _validate_refs(self) -> None:
        for m in self.measures.values():
            if m.is_aggregate:
                continue
            for ref in formula_names(m.formula):
                if ref not in self.measures:
                    raise MeasureError(
                        f"measure {m.key!r}: formula references unknown measure {ref!r}"
                    )
                if not self.measures[ref].is_aggregate:
                    raise MeasureError(
                        f"measure {m.key!r}: formula may only reference aggregate "
                        f"measures, but {ref!r} is itself derived"
                    )

    def _leaves(self, key: str) -> list[str]:
        """The aggregate measures a key needs computed. An aggregate is its own
        single leaf; a derived measure resolves to the aggregates it references."""
        m = self.get(key)
        if m.is_aggregate:
            return [key]
        return sorted(formula_names(m.formula))

    def _agg_expr(self, m: Measure) -> pl.Expr:
        """The polars reduction for one aggregate measure."""
        if m.agg == "count" and not m.formula.strip():
            return pl.len().cast(pl.Float64)
        base, _ = compile_formula(m.formula, pl.col)
        reductions = {
            "count": base.count(),
            "sum": base.sum(),
            "dcount": base.drop_nulls().n_unique(),
            "min": base.min(),
            "max": base.max(),
            "avg": base.mean(),
        }
        return reductions[m.agg].cast(pl.Float64)

    def _value_expr(self, key: str) -> pl.Expr:
        """The expression over leaf columns yielding the measure's value."""
        m = self.get(key)
        if m.is_aggregate:
            return pl.col(key)
        expr, _ = compile_formula(m.formula, pl.col)
        return expr

    def aggregate(self, lf: pl.LazyFrame, dims: list[str], key: str) -> pl.DataFrame:
        """Group `lf` by `dims` and compute measure `key` as a `__value__`
        column. `dims=[]` reduces over the whole frame (one row). Each aggregate
        leaf is computed with its own `filters`, joined on `dims`, then the
        (derived or trivial) value expression is evaluated. Non-finite results —
        empty groups, divide-by-zero — become null so callers can drop them."""
        schema = lf.collect_schema().names()
        frames = []
        for leaf in self._leaves(key):
            lm = self.get(leaf)
            sub = lf
            if lm.filters:
                preds = filters_mod.predicates(lm.filters, schema)
                if preds:
                    sub = sub.filter(*preds)
            agg = self._agg_expr(lm).alias(leaf)
            frames.append(sub.group_by(dims).agg(agg) if dims else sub.select(agg))

        if dims:
            joined = reduce(
                lambda a, b: a.join(b, on=dims, how="full", coalesce=True), frames
            )
        else:
            joined = reduce(lambda a, b: a.join(b, how="cross"), frames)

        value = self._value_expr(key)
        finite = (
            pl.when(value.is_infinite() | value.is_nan())
            .then(None)
            .otherwise(value)
            .alias(VALUE)
        )
        return joined.with_columns(finite).collect()

    def scalar(self, lf: pl.LazyFrame, key: str):
        """The measure over the whole (ungrouped) frame — a single Python value,
        or None when empty/undefined. Used for the pie's grand-total centre."""
        frame = self.aggregate(lf, [], key)
        if frame.height == 0:
            return None
        return frame[VALUE][0]

    # --- display ------------------------------------------------------------
    def label(self, key: str) -> str:
        return self.get(key).name

    def fmt(self, key: str, value) -> str:
        return format_value(value, self.get(key).format)


def parse_block(raw) -> dict[str, MeasureSet]:
    """Parse the top-level `measures:` block (dataset -> {key -> measure}) into a
    MeasureSet per dataset. Absent block -> empty mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MeasureError("`measures` must be a mapping of dataset name -> measures")
    return {str(ds): MeasureSet.from_defs(defs) for ds, defs in raw.items()}


def single(measure=None) -> tuple[MeasureSet, str]:
    """A one-measure set for standalone chart use (no dashboard). `measure` is an
    inline definition dict, or None for a plain row count. Returns the set plus
    the synthetic key charts pass as `measure`."""
    if measure is None:
        measure = {"agg": "count"}
    return MeasureSet({"_": _parse_measure("_", measure)}), "_"


# --- Formatting ---------------------------------------------------------------
#
# A format token is `<prefix><number-pattern>[a]<suffix>`: the number-pattern is
# the contiguous run of `0 , .` (decimals from the digits after `.`, thousands if
# a `,` sits in the integer part); text before/after is literal. An optional `a`
# straight after the pattern **abbreviates** large numbers to ~compact form with
# a lowercase `k`/`m`/`b`/`t` unit (`0a $` -> `23k $`, `0.0a` -> `1.4k`); numbers
# under 1000 stay plain. No auto-scaling of percentages — `%` is a plain suffix,
# so a 0.25 ratio shown as 25% multiplies in the formula.

_PATTERN_RE = re.compile(r"[0,.]+")

# 10^3 steps, largest first, ending with the base (no unit) for < 1000.
_ABBR_STEPS = ((1e12, "t"), (1e9, "b"), (1e6, "m"), (1e3, "k"), (1.0, ""))


def format_value(value, token: str = "") -> str:
    """Render a measure value. Empty/None -> "" (empty results are dropped)."""
    if value is None:
        return ""
    number = float(value)
    if not token:
        return _default_format(number)
    match = _PATTERN_RE.search(token)
    if not match:  # no digits in the token — treat the whole thing as literal-only
        return token
    prefix, pattern, suffix = token[: match.start()], match.group(), token[match.end() :]
    integer, _, frac = pattern.partition(".")
    decimals = len(frac)
    grouping = "," in integer
    # An `a` immediately after the pattern turns on compact abbreviation.
    if suffix[:1] in ("a", "A"):
        body = _abbreviate(number, decimals, grouping)
        suffix = suffix[1:]
    else:
        body = f"{number:,.{decimals}f}" if grouping else f"{number:.{decimals}f}"
    return f"{prefix}{body}{suffix}"


def _abbreviate(number: float, decimals: int, grouping: bool) -> str:
    """Scale to the largest 10^3 step and append its unit: `23400 -> 23.4k`
    (with `decimals` decimals). Values under 1000 render plainly."""
    magnitude = abs(number)
    for step, unit in _ABBR_STEPS:
        if magnitude >= step:
            scaled = number / step
            body = f"{scaled:,.{decimals}f}" if grouping else f"{scaled:.{decimals}f}"
            return body + unit
    return f"{number:,.{decimals}f}" if grouping else f"{number:.{decimals}f}"


def _default_format(number: float) -> str:
    """No token given: thousands-separated, whole numbers without decimals, else
    up to two trimmed decimals (`1200 -> 1,200`, `1234.5 -> 1,234.5`)."""
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")
