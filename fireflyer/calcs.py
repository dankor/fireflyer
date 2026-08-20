"""Dashboard calcs — named aggregations over a dataset.

A dashboard declares a top-level `calcs:` block **keyed by dataset**; each
calc has a stable key, a definition, and display metadata. Charts no longer
carry an `agg`/`value` pair — they name a calc and supply the dimension to
break it down by. See architecture.md, "Calcs".

A calc is one of three kinds:

- **Aggregate** — `agg` + a row-level `formula` (an expression over *columns*),
  reduced per group. A bare column (`amount`) is the trivial case; `price * qty`
  is the pre-aggregate case. Optional `filters` pre-narrow the rows this calc
  sees (that's how conditional aggregation is expressed).
- **Derived** — a `formula` over *other calc keys*, no `agg`. Computed per
  group after the aggregates, so `revenue / orders_count` is a true per-group
  ratio. Its leaves must be aggregate calcs (no derived-of-derived).
- **Column** — a row-level `formula`, no `agg`. Not a value but a **dimension**:
  `attach` materializes it onto the scan, so it can be a chart's `x`/`column`
  (or feed a filter or another calc) exactly like a real dataset column. This is
  how `str2dt(order_date, YYYYMMDD)` puts a string date on an axis.

Nothing in the YAML names the kind. `agg` means aggregate; a bare formula is
sorted into derived vs column by **what it references** — sibling calcs make it
derived, dataset columns make it a column (see `_classify`). All three share the
same tiny expression grammar (`+ - * /`, parens, numeric literals, and the
`str2dt()` function); the only difference is what a bare name resolves to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from functools import reduce

import polars as pl

from fireflyer import filters as filters_mod

# The reductions a calc's `agg` may name. `count` needs no formula (row
# count); the rest reduce the calc's row-level formula. Deliberately small —
# no median/percentile (non-trivial, pushdown-hostile).
AGGS = ("count", "sum", "dcount", "min", "max", "avg")

# A calc's kind — never written in the YAML. `agg` means "aggregate"; a bare
# formula is sorted into "derived" vs "column" by what it references, which needs
# the whole set, so `_parse_calc` leaves it `_UNCLASSIFIED` for `_classify`.
_UNCLASSIFIED = "formula"

# Internal column name for the computed calc value in an aggregated frame.
VALUE = "__value__"


class CalcError(ValueError):
    """Raised for any malformed calc — message is shown to the user."""


# --- Expression grammar -------------------------------------------------------
#
# One recursive-descent parser turns a formula string into a polars expression.
# Identifiers are handed to a `resolve` callback: for a row-level formula they
# become `pl.col(<column>)`, for a derived formula `pl.col(<leaf calc>)` —
# same grammar, different leaf meaning. Numbers are literals; `+ - * /`, parens
# and unary minus compose them. No comparisons — aggregation is expressed by
# `agg:`, not inside the formula. There is exactly one function, `str2dt()`
# (see `_str2dt_expr`); anything else is rejected.

_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[A-Za-z_]\w*|\S")
_NAME_RE = re.compile(r"[A-Za-z_]\w*")

# Both ways a `str2dt()` call can arrive malformed are overwhelmingly the same
# mistake: written inline as `{formula: str2dt(day, YYYYMMDD)}`, YAML splits the
# flow mapping on str2dt()'s comma and the formula reaches us truncated to
# `str2dt(day`. Worth saying out loud — the raw parse error is baffling otherwise.
_FLOW_MAPPING_HINT = (
    " If the calc is written inline as `{...}`, quote the formula "
    "(`{formula: 'str2dt(day, YYYYMMDD)'}`) — the comma otherwise splits the "
    "YAML flow mapping."
)


def _tokenize(text: str) -> list[tuple[str, int]]:
    """`(token, offset)` pairs — the offset lets the parser slice a `str2dt()`
    pattern straight out of the source. Nothing is rejected here: a character
    the grammar doesn't accept is reported by the parser, which knows what it
    was expecting (a pattern's `-`, `:` and `+` are ordinary text inside
    `str2dt()` and a syntax error anywhere else)."""
    return [(m.group(), m.start()) for m in _TOKEN_RE.finditer(text)]


class _Parser:
    """expr := term (('+'|'-') term)* ; term := factor (('*'|'/') factor)* ;
    factor := number | call | name | '(' expr ')' | '-' factor ;
    call := 'str2dt' '(' name ',' <raw pattern> ')'."""

    def __init__(self, text: str, tokens: list[tuple[str, int]], resolve):
        self.text = text
        self.tokens = tokens
        self.pos = 0
        self.resolve = resolve
        self.names: set[str] = set()

    def _peek(self) -> str | None:
        return self.tokens[self.pos][0] if self.pos < len(self.tokens) else None

    def _eat(self) -> str:
        tok = self.tokens[self.pos][0]
        self.pos += 1
        return tok

    def _offset(self, index: int) -> int:
        """Where token `index` starts in the source (end of text if past it)."""
        return self.tokens[index][1] if index < len(self.tokens) else len(self.text)

    def parse(self) -> pl.Expr:
        if not self.tokens:
            raise CalcError("empty formula")
        expr = self._expr()
        if self.pos != len(self.tokens):
            raise CalcError(f"unexpected {self._peek()!r} in formula")
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
            raise CalcError("unexpected end of formula")
        if tok == "-":
            self._eat()
            return -self._factor()
        if tok == "(":
            self._eat()
            expr = self._expr()
            if self._peek() != ")":
                raise CalcError("missing ')' in formula")
            self._eat()
            return expr
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            self._eat()
            return pl.lit(float(tok))
        if _NAME_RE.fullmatch(tok):
            self._eat()
            if self._peek() == "(":
                return self._call(tok)
            self.names.add(tok)
            return self.resolve(tok)
        raise CalcError(f"unexpected {tok!r} in formula")

    def _call(self, name: str) -> pl.Expr:
        """`str2dt(<column>, <pattern>)` — the grammar's only function. The
        pattern is a literal like `YYYY-MM-DD HH:mm:ssZ`, whose `-`, `:`, `+`
        and spaces are not expression syntax, so it's taken as the **raw
        source** between the comma and the closing paren rather than re-joined
        from tokens."""
        if name != "str2dt":
            raise CalcError(
                f"unknown function {name!r} in formula — only str2dt() exists"
            )
        self._eat()  # '('
        column = self._peek()
        if column is None or not _NAME_RE.fullmatch(column):
            raise CalcError("str2dt() takes a column name as its first argument")
        self._eat()
        if self._peek() != ",":
            raise CalcError(
                "str2dt() takes a pattern as its second argument, "
                f"e.g. str2dt(order_date, YYYYMMDD).{_FLOW_MAPPING_HINT}"
            )
        self._eat()  # ','
        close = self._find_close()
        pattern = self.text[self._offset(self.pos) : self._offset(close)].strip()
        self.pos = close + 1
        self.names.add(column)
        return _str2dt_expr(column, pattern)

    def _find_close(self) -> int:
        """Index of the `)` token closing the current call."""
        for i in range(self.pos, len(self.tokens)):
            if self.tokens[i][0] == ")":
                return i
        raise CalcError(f"missing ')' in str2dt().{_FLOW_MAPPING_HINT}")


def compile_formula(text: str, resolve) -> tuple[pl.Expr, set[str]]:
    """Compile a formula to `(expr, referenced_names)`. `resolve(name)` maps a
    bare identifier to a polars expression."""
    parser = _Parser(text, _tokenize(text), resolve)
    return parser.parse(), parser.names


# `str2dt(<column>, <pattern>)` turns a string (or integer) column into a real
# Date/Datetime, so a `YYYYMMDD` column can be a chart's axis. The pattern is
# written the way people write dates; these tokens map to strptime codes and
# every other character passes through as a literal separator.
_PATTERN_CODES = {
    "YYYY": "%Y",
    "YY": "%y",
    "MM": "%m",   # month; lowercase `mm` is minutes
    "DD": "%d",
    "HH": "%H",
    "mm": "%M",
    # `%.f` makes fractional seconds **optional**, so one pattern covers both
    # `17:14:03` and `17:14:03.123456` — a distinction most people don't know
    # their export makes, and which otherwise silently nulls every row.
    "ss": "%S%.f",
    # `%#z` accepts `+03:00`, `+0300` and a bare `Z` (Zulu) alike.
    "Z": "%#z",
}
_PATTERN_CODE_RE = re.compile("|".join(sorted(_PATTERN_CODES, key=len, reverse=True)))
# Any of these makes the result a Datetime rather than a plain Date. `Z` counts:
# an offset is only meaningful on a value that carries a time.
_TIME_CODES = ("HH", "mm", "ss", "Z")


def _str2dt_expr(column: str, pattern: str) -> pl.Expr:
    """Parse `column` with `pattern`. Values that don't match become null
    (`strict=False`) — one malformed row shouldn't blow up a dashboard — and the
    column is cast to text first so an integer `20240115` parses too.

    A pattern ending in `Z` reads the UTC offset, and the result is **normalized
    to UTC**: `2024-06-26 17:14:03+03:00` buckets and labels as `14:14:03`."""
    if not pattern:
        raise CalcError("str2dt() needs a pattern, e.g. str2dt(order_date, YYYYMMDD)")
    if not _PATTERN_CODE_RE.search(pattern):
        raise CalcError(
            f"str2dt() pattern {pattern!r} has no YYYY/YY/MM/DD/HH/mm/ss/Z token"
        )
    fmt = _PATTERN_CODE_RE.sub(lambda m: _PATTERN_CODES[m.group()], pattern)
    text = pl.col(column).cast(pl.String, strict=False)
    if any(code in pattern for code in _TIME_CODES):
        return text.str.to_datetime(fmt, strict=False)
    return text.str.to_date(fmt, strict=False)


def formula_names(text: str) -> set[str]:
    """The bare identifiers a formula references, without building an expr."""
    _, names = compile_formula(text, pl.col)
    return names


# --- Calc model ------------------------------------------------------------


@dataclass(frozen=True)
class Calc:
    key: str
    name: str            # display label; falls back to the key
    description: str
    format: str          # `<prefix><0,.pattern><suffix>` token, or "" for default
    kind: str            # "aggregate" | "derived" | "column" (see `_classify`)
    agg: str | None      # one of AGGS; None unless the kind is "aggregate"
    formula: str         # row expr (aggregate/column) or calc expr (derived)
    filters: tuple       # tuple[Filter, ...] — only meaningful for aggregate calcs

    @property
    def is_aggregate(self) -> bool:
        return self.kind == "aggregate"

    @property
    def is_column(self) -> bool:
        return self.kind == "column"


def _parse_calc(key: str, raw) -> Calc:
    if not isinstance(raw, dict):
        raise CalcError(f"calc {key!r}: must be a mapping")
    name = str(raw.get("name") or key)
    description = str(raw.get("description") or "")
    fmt = str(raw.get("format") or "")

    if "agg" in raw:
        agg = raw["agg"]
        if agg not in AGGS:
            raise CalcError(
                f"calc {key!r}: agg must be one of {list(AGGS)}, got {agg!r}"
            )
        formula = str(raw.get("formula") or "")
        if agg != "count" and not formula.strip():
            raise CalcError(f"calc {key!r}: agg {agg!r} needs a `formula`")
        if formula.strip():
            compile_formula(formula, pl.col)  # syntax check
        filters = tuple(filters_mod.normalize(raw.get("filters")))
        return Calc(key, name, description, fmt, "aggregate", agg, formula, filters)

    if "formula" in raw:
        formula = str(raw["formula"]).strip()
        if not formula:
            raise CalcError(f"calc {key!r}: `formula` is empty")
        if raw.get("filters"):
            raise CalcError(
                f"calc {key!r}: a formula calc can't take `filters` — put them on "
                "the aggregate calcs it references"
            )
        compile_formula(formula, pl.col)  # syntax check
        return Calc(key, name, description, fmt, _UNCLASSIFIED, None, formula, ())

    raise CalcError(f"calc {key!r}: needs either `agg` or `formula`")


# A `str2dt()` call always yields a date, so a formula containing one is
# row-level no matter what its argument is named — checked by text since the name
# it references is a column, which `_classify` can't otherwise tell from a calc
# key.
_STR2DT_CALL_RE = re.compile(r"\bstr2dt\s*\(")


def _classify(calcs: dict[str, Calc]) -> dict[str, Calc]:
    """Decide whether each no-`agg` formula is **derived** (a value combining
    sibling calcs) or a **column** (a row-level expression over dataset
    columns) — the distinction the YAML no longer spells out.

    A formula is derived only when every name it references is another calc that
    itself produces a value. A name that isn't a calc key must be a dataset
    column, a `str2dt()` call is row-level by definition, and a reference to a
    column calc makes the formula row-level too — any of those make it a column.
    One pass in declaration order is enough because column calcs are attached in
    that order, so a chain can only build on an earlier link.

    A calc key therefore **shadows** a dataset column of the same name inside a
    no-`agg` formula — except in its *own* formula, where a self-reference can
    only mean the underlying column. That is what lets a column calc overlay a
    raw column to give it a display name and description.
    """
    out: dict[str, Calc] = {}
    columns: set[str] = set()
    for key, calc in calcs.items():
        if calc.kind != _UNCLASSIFIED:
            out[key] = calc
            continue
        names = formula_names(calc.formula)
        row_level = (
            not names
            or bool(_STR2DT_CALL_RE.search(calc.formula))
            # `n == key`: a calc cannot reference itself, so a self-reference is
            # the **dataset column** of that name. That's what makes an overlay
            # work — `status: {name: Order status, formula: status}` relabels the
            # raw column without renaming it.
            or any(n not in calcs or n == key or n in columns for n in names)
        )
        if row_level:
            columns.add(key)
        out[key] = replace(calc, kind="column" if row_level else "derived")
    return out


class CalcSet:
    """The calcs for a single dataset, keyed by name. Resolves a calc to
    Polars and to display strings; classifies and validates at construction."""

    def __init__(self, calcs: dict[str, Calc]):
        self.calcs = _classify(calcs)
        self._validate_refs()

    @classmethod
    def from_defs(cls, defs: dict) -> "CalcSet":
        if not isinstance(defs, dict):
            raise CalcError("calcs for a dataset must be a mapping of key -> calc")
        return cls({key: _parse_calc(key, raw) for key, raw in defs.items()})

    def __contains__(self, key: str) -> bool:
        return key in self.calcs

    def get(self, key: str) -> Calc:
        if key not in self.calcs:
            raise CalcError(f"unknown calc {key!r}")
        return self.calcs[key]

    def _validate_refs(self) -> None:
        for m in self.calcs.values():
            if m.kind != "derived":
                continue  # aggregate/column formulas name columns, not calcs
            for ref in formula_names(m.formula):
                if ref not in self.calcs:
                    raise CalcError(
                        f"calc {m.key!r}: formula references unknown calc {ref!r}"
                    )
                if not self.calcs[ref].is_aggregate:
                    raise CalcError(
                        f"calc {m.key!r}: formula may only reference aggregate "
                        f"calcs, but {ref!r} is {self.calcs[ref].kind}"
                    )

    def attach(self, lf: pl.LazyFrame) -> pl.LazyFrame:
        """Materialize this dataset's **column calcs** onto a scan, so a calc key
        works anywhere a column name does — a chart's `x`, a filter, another
        calc's formula. Added one at a time in declaration order, so a later
        column calc can build on an earlier one; Polars' projection pushdown
        prunes the ones a given chart doesn't touch."""
        for calc in self.calcs.values():
            if calc.is_column:
                expr, _ = compile_formula(calc.formula, pl.col)
                lf = lf.with_columns(expr.alias(calc.key))
        return lf

    def column_keys(self) -> list[str]:
        """The column-calc keys, in declaration order — the extra names the
        editor's column dropdowns offer alongside the dataset's real columns."""
        return [k for k, c in self.calcs.items() if c.is_column]

    def _leaves(self, key: str) -> list[str]:
        """The aggregate calcs a key needs computed. An aggregate is its own
        single leaf; a derived calc resolves to the aggregates it references."""
        m = self.get(key)
        if m.is_column:
            raise CalcError(
                f"calc {key!r} is a column calc — a dimension, not a value. Use it "
                "as a chart's `x`/`column`, not its `calc`"
            )
        if m.is_aggregate:
            return [key]
        return sorted(formula_names(m.formula))

    def _agg_expr(self, m: Calc) -> pl.Expr:
        """The polars reduction for one aggregate calc."""
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
        """The expression over leaf columns yielding the calc's value."""
        m = self.get(key)
        if m.is_aggregate:
            return pl.col(key)
        expr, _ = compile_formula(m.formula, pl.col)
        return expr

    def aggregate(self, lf: pl.LazyFrame, dims: list[str], key: str) -> pl.DataFrame:
        """Group `lf` by `dims` and compute calc `key` as a `__value__`
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
        """The calc over the whole (ungrouped) frame — a single Python value,
        or None when empty/undefined. Used for the pie's grand-total centre."""
        frame = self.aggregate(lf, [], key)
        if frame.height == 0:
            return None
        return frame[VALUE][0]

    # --- display ------------------------------------------------------------
    def label(self, key: str) -> str:
        return self.get(key).name

    def column_label(self, name: str) -> str:
        """Display name for something used as a **column**: a calc's `name` when
        the key is one, otherwise the name unchanged.

        This is how a column calc relabels a raw column without renaming it —

            order_day: {name: Order date, formula: str2dt(day, YYYY-MM-DD)}

        The key stays what the YAML refers to (`columns: [order_day]`, a filter,
        another formula); only what a viewer reads changes. Takes any string,
        including a plain dataset column that has no calc at all, so callers
        don't have to check first.
        """
        calc = self.calcs.get(name)
        return calc.name if calc is not None else name

    def describe_column(self, name: str) -> str:
        """A column's `description`, or "" — same lookup rules as
        `column_label`."""
        calc = self.calcs.get(name)
        return calc.description if calc is not None else ""

    def fmt(self, key: str, value) -> str:
        return format_value(value, self.get(key).format)


def parse_block(raw) -> dict[str, CalcSet]:
    """Parse the top-level `calcs:` block (dataset -> {key -> calc}) into a
    CalcSet per dataset. Absent block -> empty mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CalcError("`calcs` must be a mapping of dataset name -> calcs")
    return {str(ds): CalcSet.from_defs(defs) for ds, defs in raw.items()}


def single(calc=None) -> tuple[CalcSet, str]:
    """A one-calc set for standalone chart use (no dashboard). `calc` is an
    inline definition dict, or None for a plain row count. Returns the set plus
    the synthetic key charts pass as `calc`."""
    if calc is None:
        calc = {"agg": "count"}
    return CalcSet({"_": _parse_calc("_", calc)}), "_"


# --- Formatting ---------------------------------------------------------------
#
# A format token is `<prefix><number-pattern>[a]<suffix>`: the number-pattern is
# the contiguous run of `0 , .` (decimals from the digits after `.`, thousands if
# a `,` sits in the integer part); text before/after is literal. An optional `a`
# straight after the pattern **abbreviates** large numbers to ~compact form with
# a lowercase `k`/`m`/`b`/`t` unit (`0a $` -> `23k $`, `0.0a` -> `1.4k`); numbers
# under 1000 stay plain. The pattern's decimals are the **maximum** shown — the
# abbreviated value is truncated to them and trailing zeros dropped, so `0.0a`
# renders 1971 as `1.9k` and 2000 as `2k`. No auto-scaling of percentages — `%`
# is a plain suffix, so a 0.25 ratio shown as 25% multiplies in the formula.

_PATTERN_RE = re.compile(r"[0,.]+")

# 10^3 steps, largest first, ending with the base (no unit) for < 1000.
_ABBR_STEPS = ((1e12, "t"), (1e9, "b"), (1e6, "m"), (1e3, "k"), (1.0, ""))


def format_value(value, token: str = "") -> str:
    """Render a calc value. Empty/None -> "" (empty results are dropped)."""
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
    """Scale to the largest 10^3 step and append its unit: `23400 -> 23.4k`.
    `decimals` is the max shown — the value is truncated to it and trailing zeros
    dropped (`1971 -> 1.9k`, `2000 -> 2k`). Values under 1000 render plainly."""
    magnitude = abs(number)
    for step, unit in _ABBR_STEPS:
        if magnitude >= step:
            return _truncated(number / step, decimals, grouping) + unit
    return _truncated(number, decimals, grouping)


def _truncated(value: float, decimals: int, grouping: bool) -> str:
    """Truncate (toward zero, not round) to at most `decimals` places and drop
    trailing zeros. Decimal(str(...)) avoids binary-float artifacts like
    2.3*10 = 22.999… so 2300 doesn't truncate to 2.2k."""
    quantum = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    fixed = Decimal(str(value)).quantize(quantum, rounding=ROUND_DOWN)
    body = f"{fixed:,.{decimals}f}" if grouping else f"{fixed:.{decimals}f}"
    return body.rstrip("0").rstrip(".") if "." in body else body


def exact_value(value) -> str:
    """The calc value at **full precision** for the exact-value line in tooltips:
    no rounding and no format token, but thousands-separated, because the whole
    point of showing it is that someone is reading the digits. Whole numbers
    render as integers (`1,234,567`); others keep every float digit
    (`59.51428571428571`). Empty/None -> ""."""
    if value is None:
        return ""
    number = float(value)
    return f"{int(number):,}" if number.is_integer() else f"{number:,}"


def _default_format(number: float) -> str:
    """No token given: thousands-separated, whole numbers without decimals, else
    up to two trimmed decimals (`1200 -> 1,200`, `1234.5 -> 1,234.5`)."""
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")
