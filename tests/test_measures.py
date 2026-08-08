import polars as pl
import pytest

from fireflyer import measures as M


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
    ],
)
def test_format_value(value, token, expected):
    assert M.format_value(value, token) == expected


# --- expression parser --------------------------------------------------------


def test_compile_formula_names():
    _, names = M.compile_formula("a + b * 2 - (c / a)", pl.col)
    assert names == {"a", "b", "c"}


@pytest.mark.parametrize("bad", ["", "a +", "a + * b", "(a", "a )", "a @ b"])
def test_compile_formula_rejects_bad(bad):
    with pytest.raises(M.MeasureError):
        M.compile_formula(bad, pl.col)


# --- MeasureSet aggregation ---------------------------------------------------


@pytest.fixture
def lf():
    return pl.scan_parquet("tests/data/orders.parquet")


def test_aggregate_sum_by_group(lf):
    ms = M.MeasureSet.from_defs({"revenue": {"agg": "sum", "formula": "amount"}})
    out = ms.aggregate(lf, ["status"], "revenue").sort("status")
    assert out["status"].to_list() == ["cancelled", "paid", "pending"]
    assert out[M.VALUE].to_list() == [30.0, 276.0, 27.0]


def test_aggregate_row_level_calculation(lf):
    """A row-level formula (`amount * 2`) is computed before the reduction."""
    ms = M.MeasureSet.from_defs({"m": {"agg": "sum", "formula": "amount * 2"}})
    assert ms.scalar(lf, "m") == 666.0


def test_derived_ratio_per_group(lf):
    ms = M.MeasureSet.from_defs({
        "revenue": {"agg": "sum", "formula": "amount"},
        "n": {"agg": "count"},
        "aov": {"formula": "revenue / n"},
    })
    out = ms.aggregate(lf, ["status"], "aov").sort("status")
    # cancelled: 30/1, paid: 276/4, pending: 27/2
    assert out[M.VALUE].to_list() == [30.0, 69.0, 13.5]


def test_conditional_aggregation_via_filters(lf):
    """A measure's own `filters` narrow the rows it sees — count of paid only."""
    ms = M.MeasureSet.from_defs({
        "paid": {
            "agg": "count",
            "filters": [{"column": "status", "op": "in", "values": ["paid"]}],
        },
    })
    assert ms.scalar(lf, "paid") == 4.0


def test_divide_by_zero_is_null(lf):
    ms = M.MeasureSet.from_defs({
        "z": {"agg": "sum", "formula": "amount", "filters": [
            {"column": "status", "op": "in", "values": ["nonexistent"]}]},
        "n": {"agg": "count"},
        "ratio": {"formula": "n / z"},   # z is 0/absent -> undefined
    })
    assert ms.scalar(lf, "ratio") is None


def test_scalar_dcount_is_distinct_over_all(lf):
    ms = M.MeasureSet.from_defs({"d": {"agg": "dcount", "formula": "day"}})
    assert ms.scalar(lf, "d") == 4.0


# --- validation ---------------------------------------------------------------


def test_rejects_unknown_agg():
    with pytest.raises(M.MeasureError, match="agg"):
        M.MeasureSet.from_defs({"m": {"agg": "median", "formula": "amount"}})


def test_rejects_non_count_agg_without_formula():
    with pytest.raises(M.MeasureError, match="formula"):
        M.MeasureSet.from_defs({"m": {"agg": "sum"}})


def test_rejects_measure_with_neither_agg_nor_formula():
    with pytest.raises(M.MeasureError, match="agg.*formula|formula"):
        M.MeasureSet.from_defs({"m": {"name": "x"}})


def test_rejects_derived_referencing_unknown_measure():
    with pytest.raises(M.MeasureError, match="unknown measure"):
        M.MeasureSet.from_defs({"m": {"formula": "ghost / 2"}})


def test_rejects_derived_of_derived():
    with pytest.raises(M.MeasureError, match="derived"):
        M.MeasureSet.from_defs({
            "a": {"agg": "count"},
            "b": {"formula": "a * 2"},
            "c": {"formula": "b + 1"},   # references a derived measure
        })


def test_rejects_filters_on_derived():
    with pytest.raises(M.MeasureError, match="filters"):
        M.MeasureSet.from_defs({
            "a": {"agg": "count"},
            "b": {"formula": "a * 2", "filters": [
                {"column": "status", "op": "in", "values": ["paid"]}]},
        })


def test_parse_block_keyed_by_dataset():
    block = M.parse_block({"orders": {"n": {"agg": "count"}}})
    assert "orders" in block
    assert "n" in block["orders"]


def test_parse_block_rejects_non_mapping():
    with pytest.raises(M.MeasureError):
        M.parse_block([1, 2, 3])
