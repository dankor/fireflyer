import polars as pl
import pytest

from fireflyer import calcs as M


# --- format_value -------------------------------------------------------------


@pytest.mark.parametrize(
    "value, token, expected",
    [
        (1234.0, "", "1,234"),          # default: thousands, whole number
        (1234.5, "", "1,234.5"),        # default: trimmed decimals
        (None, "", ""),                 # empty result -> blank
        (1234.4, "0", "1234"),          # integer, no grouping (no comma in pattern)
        (1234.5, "0.00", "1234.50"),    # 2 decimals, no grouping
        (1234.5, "0,0.00", "1,234.50"), # grouping + 2 decimals
        (1234.5, "0.00$", "1234.50$"),  # suffix
        (1234.4, "$0,0", "$1,234"),     # prefix + grouping
        (0.25, "0.0%", "0.2%"),         # `%` is a literal suffix — no auto-scaling
        (25.0, "0.0%", "25.0%"),        # scaling is done in the formula, not here
        (23400, "0a $", "23k $"),       # `a` abbreviates + literal suffix
        (23400, "0.0a $", "23.4k $"),   # abbreviation keeps the pattern's decimals
        (23400, "$0.0a", "$23.4k"),     # prefix + abbreviation
        (1500000, "0.0a", "1.5m"),      # millions
        (276, "0a $", "276 $"),         # under 1000 stays plain
        (1971, "0.0a", "1.9k"),         # truncates (not rounds) to 1 decimal
        (2000, "0.0a", "2k"),           # trailing .0 dropped
        (2000, "0a", "2k"),             # 0 decimals, whole
        (2300, "0.0a", "2.3k"),         # no binary-float truncation artifact
        (1999, "0.0a", "1.9k"),         # truncation, not rounding up
        (276, "0.0a $", "276 $"),       # trailing .0 dropped under 1000 too
    ],
)
def test_format_value(value, token, expected):
    assert M.format_value(value, token) == expected


# --- expression parser --------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (333.0, "333"),                          # whole -> integer, no ".0"
        (5400.0, "5,400"),                       # grouped: someone reads these digits
        (1234567.0, "1,234,567"),
        (276 / 333 * 100, "82.88288288288288"),  # full float precision, unrounded
        (1234.5678, "1,234.5678"),               # grouped *and* unrounded
        (None, ""),
        (0.0, "0"),
    ],
)
def test_exact_value(value, expected):
    assert M.exact_value(value) == expected


def test_compile_formula_names():
    _, names = M.compile_formula("a + b * 2 - (c / a)", pl.col)
    assert names == {"a", "b", "c"}


@pytest.mark.parametrize("bad", ["", "a +", "a + * b", "(a", "a )", "a @ b"])
def test_compile_formula_rejects_bad(bad):
    with pytest.raises(M.CalcError):
        M.compile_formula(bad, pl.col)


# --- CalcSet aggregation ---------------------------------------------------


@pytest.fixture
def lf():
    return pl.scan_parquet("tests/data/orders.parquet")


def test_aggregate_sum_by_group(lf):
    ms = M.CalcSet.from_defs({"revenue": {"agg": "sum", "formula": "amount"}})
    out = ms.aggregate(lf, ["status"], "revenue").sort("status")
    assert out["status"].to_list() == ["cancelled", "paid", "pending"]
    assert out[M.VALUE].to_list() == [30.0, 276.0, 27.0]


def test_aggregate_row_level_calculation(lf):
    """A row-level formula (`amount * 2`) is computed before the reduction."""
    ms = M.CalcSet.from_defs({"m": {"agg": "sum", "formula": "amount * 2"}})
    assert ms.scalar(lf, "m") == 666.0


def test_derived_ratio_per_group(lf):
    ms = M.CalcSet.from_defs({
        "revenue": {"agg": "sum", "formula": "amount"},
        "n": {"agg": "count"},
        "aov": {"formula": "revenue / n"},
    })
    out = ms.aggregate(lf, ["status"], "aov").sort("status")
    # cancelled: 30/1, paid: 276/4, pending: 27/2
    assert out[M.VALUE].to_list() == [30.0, 69.0, 13.5]


def test_conditional_aggregation_via_filters(lf):
    """A calc's own `filters` narrow the rows it sees — count of paid only."""
    ms = M.CalcSet.from_defs({
        "paid": {
            "agg": "count",
            "filters": [{"column": "status", "op": "in", "values": ["paid"]}],
        },
    })
    assert ms.scalar(lf, "paid") == 4.0


def test_divide_by_zero_is_null(lf):
    ms = M.CalcSet.from_defs({
        "z": {"agg": "sum", "formula": "amount", "filters": [
            {"column": "status", "op": "in", "values": ["nonexistent"]}]},
        "n": {"agg": "count"},
        "ratio": {"formula": "n / z"},   # z is 0/absent -> undefined
    })
    assert ms.scalar(lf, "ratio") is None


def test_scalar_dcount_is_distinct_over_all(lf):
    ms = M.CalcSet.from_defs({"d": {"agg": "dcount", "formula": "day"}})
    assert ms.scalar(lf, "d") == 4.0


# --- validation ---------------------------------------------------------------


def test_rejects_unknown_agg():
    with pytest.raises(M.CalcError, match="agg"):
        M.CalcSet.from_defs({"m": {"agg": "median", "formula": "amount"}})


def test_rejects_non_count_agg_without_formula():
    with pytest.raises(M.CalcError, match="formula"):
        M.CalcSet.from_defs({"m": {"agg": "sum"}})


def test_rejects_calc_with_neither_agg_nor_formula():
    with pytest.raises(M.CalcError, match="agg.*formula|formula"):
        M.CalcSet.from_defs({"m": {"name": "x"}})


def test_name_that_is_not_a_calc_reads_as_a_column():
    """Without a `kind:` marker a typo'd calc reference is indistinguishable from
    a dataset column, so it becomes a column calc and fails at read time (when
    the schema is known) rather than at parse time."""
    cs = M.CalcSet.from_defs({"m": {"formula": "ghost / 2"}})
    assert cs.get("m").is_column


def test_rejects_derived_of_derived():
    with pytest.raises(M.CalcError, match="derived"):
        M.CalcSet.from_defs({
            "a": {"agg": "count"},
            "b": {"formula": "a * 2"},
            "c": {"formula": "b + 1"},   # references a derived calc
        })


def test_rejects_filters_on_derived():
    with pytest.raises(M.CalcError, match="filters"):
        M.CalcSet.from_defs({
            "a": {"agg": "count"},
            "b": {"formula": "a * 2", "filters": [
                {"column": "status", "op": "in", "values": ["paid"]}]},
        })


def test_parse_block_keyed_by_dataset():
    block = M.parse_block({"orders": {"n": {"agg": "count"}}})
    assert "orders" in block
    assert "n" in block["orders"]


def test_parse_block_rejects_non_mapping():
    with pytest.raises(M.CalcError):
        M.parse_block([1, 2, 3])


# --- str2dt() -------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern, raw, expected",
    [
        ("YYYYMMDD", "20240115", "2024-01-15"),
        ("YYYY-MM-DD", "2024-01-15", "2024-01-15"),
        ("DD/MM/YYYY", "15/01/2024", "2024-01-15"),
        ("YY-MM-DD", "24-01-15", "2024-01-15"),
    ],
)
def test_str2dt_parses_pattern(pattern, raw, expected):
    df = pl.DataFrame({"d": [raw]})
    expr, _ = M.compile_formula(f"str2dt(d, {pattern})", pl.col)
    assert str(df.select(expr.alias("out"))["out"][0]) == expected


def test_str2dt_on_integer_column():
    """A `YYYYMMDD` column stored as an integer parses too (cast to text first)."""
    df = pl.DataFrame({"d": [20240115]})
    expr, _ = M.compile_formula("str2dt(d, YYYYMMDD)", pl.col)
    assert str(df.select(expr.alias("out"))["out"][0]) == "2024-01-15"


def test_str2dt_with_time_yields_datetime():
    """A pattern with HH/mm/ss keeps the time — the result is a Datetime."""
    df = pl.DataFrame({"t": ["2024-01-15 10:30:05"]})
    expr, _ = M.compile_formula("str2dt(t, YYYY-MM-DD HH:mm:ss)", pl.col)
    out = df.select(expr.alias("out"))["out"]
    assert out.dtype == pl.Datetime
    assert str(out[0]) == "2024-01-15 10:30:05"


def test_str2dt_parses_a_utc_offset():
    """`Z` reads the offset (with or without the colon) and the result is
    normalized to UTC — 17:14:03+03:00 is 14:14:03Z."""
    df = pl.DataFrame({"t": ["2024-06-26 17:14:03+03:00", "2024-06-26 17:14:03+0300"]})
    expr, _ = M.compile_formula("str2dt(t, YYYY-MM-DD HH:mm:ssZ)", pl.col)
    out = df.select(expr.alias("out"))["out"]
    assert out.dtype == pl.Datetime(time_unit="us", time_zone="UTC")
    assert [str(v) for v in out.to_list()] == [
        "2024-06-26 14:14:03+00:00", "2024-06-26 14:14:03+00:00",
    ]


def test_str2dt_tolerates_optional_fractional_seconds():
    """`ss` maps to `%S%.f`, so one pattern covers both plain and fractional
    seconds — an export detail people rarely know about, and which otherwise
    silently nulls every row."""
    df = pl.DataFrame({"t": [
        "2024-06-26 17:14:03+03:00",
        "2024-06-26 17:14:03.123+03:00",
        "2024-06-26 17:14:03.123456+03:00",
    ]})
    expr, _ = M.compile_formula("str2dt(t, YYYY-MM-DD HH:mm:ssZ)", pl.col)
    assert df.select(expr.alias("out"))["out"].null_count() == 0

    # And with no offset in the pattern at all.
    plain = pl.DataFrame({"t": ["2024-06-26 17:14:03", "2024-06-26 17:14:03.5"]})
    expr2, _ = M.compile_formula("str2dt(t, YYYY-MM-DD HH:mm:ss)", pl.col)
    assert plain.select(expr2.alias("out"))["out"].null_count() == 0


def test_str2dt_accepts_a_bare_zulu_offset():
    """`Z` maps to `%#z`, which takes `+03:00`, `+0300` and a bare `Z` alike."""
    df = pl.DataFrame({"t": ["2024-06-26 17:14:03Z", "2024-06-26 17:14:03+0300"]})
    expr, _ = M.compile_formula("str2dt(t, YYYY-MM-DD HH:mm:ssZ)", pl.col)
    out = df.select(expr.alias("out"))["out"]
    assert out.null_count() == 0
    assert str(out[0]) == "2024-06-26 17:14:03+00:00"   # Zulu is already UTC


def test_str2dt_offset_with_iso_t_separator():
    """Everything that isn't a token is a literal separator, so an ISO `T`
    just works."""
    df = pl.DataFrame({"t": ["2024-06-26T17:14:03+03:00"]})
    expr, _ = M.compile_formula("str2dt(t, YYYY-MM-DDTHH:mm:ssZ)", pl.col)
    assert str(df.select(expr.alias("out"))["out"][0]) == "2024-06-26 14:14:03+00:00"


def test_str2dt_offset_counts_as_a_time_token():
    """`Z` is in _TIME_CODES, so an offset pattern parses to a Datetime rather
    than being sent down the to_date path."""
    assert "Z" in M._TIME_CODES


def test_str2dt_unparseable_row_is_null():
    """One malformed value becomes null instead of failing the whole render."""
    df = pl.DataFrame({"d": ["20240115", "not a date"]})
    expr, _ = M.compile_formula("str2dt(d, YYYYMMDD)", pl.col)
    assert df.select(expr.alias("out"))["out"].to_list()[1] is None


def test_str2dt_reports_its_column():
    _, names = M.compile_formula("str2dt(day, YYYYMMDD)", pl.col)
    assert names == {"day"}


@pytest.mark.parametrize(
    "bad, message",
    [
        ("str2dt(d)", "second argument"),
        ("str2dt(d,)", "needs a pattern"),
        ("str2dt(d, nope)", "no YYYY"),
        ("str2dt(1, YYYYMMDD)", "column name"),
        ("str2dt(d, YYYYMMDD", "missing"),
        ("month(d)", "unknown function"),
    ],
)
def test_str2dt_rejects_bad_calls(bad, message):
    with pytest.raises(M.CalcError, match=message):
        M.compile_formula(bad, pl.col)


# --- column calcs -------------------------------------------------------------


def test_column_calc_attaches_to_scan(lf):
    """A no-`agg` formula over dataset columns is a **column calc**: it's
    materialized onto the scan, so it can be grouped by like a real column."""
    cs = M.CalcSet.from_defs({
        "order_day": {"formula": "str2dt(day, YYYY-MM-DD)"},
        "revenue": {"agg": "sum", "formula": "amount"},
    })
    assert cs.get("order_day").is_column
    out = cs.aggregate(cs.attach(lf), ["order_day"], "revenue").sort("order_day")
    assert out["order_day"].dtype == pl.Date
    assert [str(d) for d in out["order_day"].to_list()] == [
        "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
    ]


def test_formula_over_calcs_is_derived_not_a_column():
    """The same shape of definition reads as derived when every name it
    references is another calc that produces a value."""
    cs = M.CalcSet.from_defs({
        "revenue": {"agg": "sum", "formula": "amount"},
        "n": {"agg": "count"},
        "aov": {"formula": "revenue / n"},
    })
    assert cs.get("aov").kind == "derived"
    assert cs.column_keys() == []


def test_column_calc_can_build_on_an_earlier_one(lf):
    """Column calcs attach in declaration order, so a later one may use an
    earlier one's key — and referencing a column calc keeps it row-level."""
    cs = M.CalcSet.from_defs({
        "doubled": {"formula": "amount * 2"},
        "quadrupled": {"formula": "doubled * 2"},
        "total": {"agg": "sum", "formula": "quadrupled"},
    })
    assert cs.column_keys() == ["doubled", "quadrupled"]
    assert cs.scalar(cs.attach(lf), "total") == 333.0 * 4


def test_str2dt_call_is_row_level_even_when_its_column_shadows_a_calc():
    """A `str2dt()` call always yields a date, so the formula is a column calc
    however its argument is named."""
    cs = M.CalcSet.from_defs({
        "day": {"agg": "count"},
        "order_day": {"formula": "str2dt(day, YYYY-MM-DD)"},
    })
    assert cs.get("order_day").is_column


def test_column_keys_lists_only_column_calcs():
    cs = M.CalcSet.from_defs({
        "n": {"agg": "count"},
        "day": {"formula": "str2dt(day, YYYY-MM-DD)"},
    })
    assert cs.column_keys() == ["day"]


def test_column_calc_is_not_a_value(lf):
    """Aggregating a column calc is a mistake — it's a dimension."""
    cs = M.CalcSet.from_defs({"d": {"formula": "amount * 2"}})
    with pytest.raises(M.CalcError, match="dimension"):
        cs.scalar(cs.attach(lf), "d")


def test_rejects_filters_on_a_formula_calc():
    with pytest.raises(M.CalcError, match="filters"):
        M.CalcSet.from_defs({"d": {
            "formula": "amount",
            "filters": [{"column": "status", "op": "in", "values": ["paid"]}],
        }})


def test_rejects_empty_formula():
    with pytest.raises(M.CalcError, match="`formula` is empty"):
        M.CalcSet.from_defs({"d": {"formula": "   "}})


def test_every_chart_tooltip_uses_the_same_exact_value(csv_to_parquet):
    """One rule across the charts: a tooltip shows the unrounded,
    thousands-separated figure, never the abbreviated one it labels the chart
    with. Pinned together because the pie drifted from it."""
    import re

    import fireflyer as ff

    parquet = csv_to_parquet("cat,grp,v\na,x,1234567\nb,y,987654\n", "exact_all")
    yaml = f"""
name: Exact
calcs:
  {parquet}:
    revenue: {{name: Revenue, description: Booked, agg: sum, formula: v,
               format: '0.0a $'}}
charts:
  b: {{type: bar, dataset: {parquet}, title: Bar, x: cat, y: grp, calc: revenue}}
  p: {{type: pie, dataset: {parquet}, title: Pie, column: cat, calc: revenue}}
  n: {{type: number, dataset: {parquet}, title: KPI, calc: revenue}}
layout:
  - ["@30", "b", "p", "n"]
"""
    html = ff.Dashboard.from_yaml(yaml).to_html()
    grand = M.exact_value(1234567 + 987654)

    assert re.search(r'bar-tooltip-val">1,234,567<', html)
    assert re.search(r'pie-tooltip-val">1,234,567<', html)
    assert re.search(rf'pie-total-tip-value">{grand}<', html)
    assert re.search(rf'number-tip-value">{grand}<', html)


# --- relabelling a column (overlay) -------------------------------------------


def test_a_column_calc_may_alias_its_own_column():
    """`status: {name: Order status, formula: status}` — the whole point of an
    overlay. A calc cannot reference itself, so the self-reference is the
    dataset column; without that rule the classifier read it as a *derived*
    calc referencing a derived calc and rejected the definition outright."""
    calcs = M.CalcSet.from_defs({
        "status": {"name": "Order status", "description": "Where it got to",
                   "formula": "status"},
    })
    calc = calcs.get("status")
    assert calc.is_column
    assert calcs.column_label("status") == "Order status"
    assert calcs.describe_column("status") == "Where it got to"


def test_an_overlay_reads_the_underlying_column():
    """The overlay computes from the raw column, so it can transform as well as
    relabel — and materializes under the same key."""
    calcs = M.CalcSet.from_defs({"amount": {"name": "Doubled", "formula": "amount * 2"}})
    frame = pl.LazyFrame({"amount": [1, 2, 3]})
    assert calcs.attach(frame).collect()["amount"].to_list() == [2, 4, 6]


def test_column_label_passes_through_a_plain_column():
    """Callers hand it any column name, calc or not."""
    calcs = M.CalcSet.from_defs({"x": {"name": "Ex", "formula": "x"}})
    assert calcs.column_label("nope") == "nope"
    assert calcs.describe_column("nope") == ""


def test_a_calc_key_still_shadows_a_column_for_other_formulas():
    """Only a calc's *own* formula treats the name as the raw column. Elsewhere
    a calc key still shadows a dataset column, which is what lets one column
    calc build on another."""
    calcs = M.CalcSet.from_defs({
        "amount": {"name": "Doubled", "formula": "amount * 2"},
        "quad": {"formula": "amount * 2"},          # refers to the calc above
    })
    frame = pl.LazyFrame({"amount": [1, 2, 3]})
    out = calcs.attach(frame).collect()
    assert out["amount"].to_list() == [2, 4, 6]
    assert out["quad"].to_list() == [4, 8, 12]      # 2x the *overlaid* amount
