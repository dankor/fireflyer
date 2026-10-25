"""Column mappings: each column a formula over the uploaded ones — reading the
YAML, carrying it across uploads, and building it over an all-text upload, with
an error, not a silent null, for a value to_number() can't read."""

import polars as pl
import pytest

from fireflyer import column_map as cm

SOURCES = ["id", "price", "qty", "status"]


def test_the_long_and_short_forms_both_read():
    rules = cm.parse(
        "columns:\n  id:\n    formula: to_number(id)\n  state: status\n  qty:\n", SOURCES
    )
    assert rules == [
        cm.Rule("id", "to_number(id)"),
        cm.Rule("state", "status"),                 # renamed: a new key, the old column
        cm.Rule("qty", "qty"),                      # no formula: the column of that name
    ]


def test_dump_reads_back_one_formula_per_line():
    rules = [cm.Rule("id", "to_number(id)"), cm.Rule("unit price", "price"),
             cm.Rule("total", "to_number(price) * to_number(qty)")]
    text = cm.dump(rules)
    assert text == (
        "columns:\n  id:\n    formula: to_number(id)\n"
        "  unit price:\n    formula: price\n"
        "  total:\n    formula: to_number(price) * to_number(qty)\n"
    )
    assert cm.parse(text, SOURCES) == rules


@pytest.mark.parametrize("text, message", [
    ("columns:\n  x:\n    formula: nope\n", "column 'x': no column 'nope'"),
    ("columns:\n  id: {type: numeric}\n", "type comes from its formula now"),
    ("columns:\n  x:\n    formula: price +\n", "column 'x':"),
    ("columns:\n  x:\n    formula: lower(price)\n", "unknown function 'lower'"),
    ("id: numeric\n", "needs a `columns:` block"),
])
def test_a_mapping_that_does_not_fit_says_why(text, message):
    with pytest.raises(cm.MappingError, match=message):
        cm.parse(text, SOURCES)


def test_a_new_upload_carries_the_mapping_forward():
    """Kept: columns whose formulas still find their columns. Added: columns new
    in this upload. One dropped before stays dropped."""
    before = [cm.Rule("n", "to_number(id)"), cm.Rule("total", "to_number(price) * to_number(qty)")]
    after = cm.default(["id", "price", "status", "note"], before, ["id", "price", "qty", "status"])
    assert after == [cm.Rule("n", "to_number(id)"), cm.Rule("note", "note")]


def test_a_rename_is_the_same_formula_under_a_new_name():
    before = [cm.Rule("status", "status"), cm.Rule("qty", "qty")]
    after = [cm.Rule("state", "status"), cm.Rule("qty", "to_number(qty)")]
    assert cm.renames(before, after) == {"status": "state"}


def test_formulas_give_the_types():
    df = pl.DataFrame({"id": [" 1", "2", None], "price": ["1.5", "", "3"], "qty": ["2", "1", "4"],
                       "day": ["2026-10-16", "2026-01-01", None], "s": ["a", "b", "c"]})
    out = cm.apply(df, [
        cm.Rule("id", "to_number(id)"), cm.Rule("price", "to_number(price)"), cm.Rule("s", "s"),
        cm.Rule("total", "to_number(price) * to_number(qty)"), cm.Rule("day", "str2dt(day, YYYY-MM-DD)"),
    ])
    # `total` came out whole (3, 12), so it's integers too.
    assert out.schema == {"id": pl.Int64, "price": pl.Float64, "s": pl.String,
                          "total": pl.Int64, "day": pl.Date}
    assert out.row(0)[:4] == (1, 1.5, "a", 3)
    assert out["price"][1] is None                  # an empty cell is missing, not an error


def test_a_value_to_number_cannot_read_is_an_error_naming_it():
    df = pl.DataFrame({"n": ["1", "two", "3", "four"]})
    with pytest.raises(cm.MappingError, match=r"column 'n': 2 value\(s\) aren't numbers — e.g. 'two'"):
        cm.apply(df, [cm.Rule("n", "to_number(n)")])


def test_arithmetic_on_text_says_to_use_to_number():
    df = pl.DataFrame({"a": ["1"], "b": ["2"]})
    with pytest.raises(cm.MappingError, match="column 'product' \\(a \\* b\\).*to_number"):
        cm.apply(df, [cm.Rule("product", "a * b")])


def test_examples_are_the_first_distinct_values():
    df = pl.DataFrame({"x": ["b", "a", "b", "", None, "c", "d", "e", "f"], "n": [1, 2, 2, 3, 4, 5, 6, 7, 8]})
    assert cm.examples(df) == {"x": ["b", "a", "c", "d", "e"], "n": ["1", "2", "3", "4", "5"]}


def test_older_guessed_types_carry_over():
    assert cm.from_dtypes([("a", "Int64"), ("b", "Float64"), ("c", "String")]) == [
        cm.Rule("a", "to_number(a)"), cm.Rule("b", "to_number(b)"), cm.Rule("c", "c"),
    ]


def test_a_mapping_saved_before_formulas_reads_as_today():
    old = ("columns:\n  id: integer\n  amount: numeric\n  status:\n    name: state\n    type: string\n"
           "  day: {name: d, type: date, format: YYYY-MM-DD}\n  s: string\n")
    assert cm.upgrade(old) == (
        "columns:\n  id:\n    formula: to_number(id)\n  amount:\n    formula: to_number(amount)\n"
        "  state:\n    formula: status\n  d:\n    formula: day\n  s:\n    formula: s\n"
    )
    current = "columns:\n  id:\n    formula: to_number(id)\n"
    assert cm.upgrade(current) == current


def test_names_in_any_script_are_written_as_they_are():
    text = cm.dump([cm.Rule("Підрозділ", "organisation_name")])
    assert text == "columns:\n  Підрозділ:\n    formula: organisation_name\n"


def test_a_service_column_is_marked_in_the_yaml():
    rules = cm.parse("columns:\n  key:\n    formula: id\n    service: true\n  qty: to_number(qty)\n", SOURCES)
    assert rules == [cm.Rule("key", "id", True), cm.Rule("qty", "to_number(qty)")]
    assert cm.dump(rules) == (
        "columns:\n  key:\n    formula: id\n    service: true\n  qty:\n    formula: to_number(qty)\n"
    )
    with pytest.raises(cm.MappingError, match="`service` is true or false"):
        cm.parse("columns:\n  key:\n    service: yes please\n", ["key"])
