import polars as pl
import pytest

from fireflyer import filters as filters_mod


def test_normalize_accepts_well_formed_filter():
    out = filters_mod.normalize([
        {"column": "status", "op": "in", "values": ["paid", "pending"]}
    ])
    assert len(out) == 1
    assert out[0].column == "status"
    assert out[0].op == "in"
    assert out[0].values == ("paid", "pending")


def test_normalize_rejects_unknown_op():
    with pytest.raises(filters_mod.FilterError, match="op must be one of"):
        filters_mod.normalize([{"column": "x", "op": "eq", "values": [1]}])


def test_normalize_rejects_empty_values():
    with pytest.raises(filters_mod.FilterError, match="values must be a non-empty list"):
        filters_mod.normalize([{"column": "x", "op": "in", "values": []}])


def test_normalize_passes_through_filter_objects():
    f = filters_mod.Filter(column="x", op="ni", values=("a",))
    out = filters_mod.normalize([f])
    assert out == [f]


def test_apply_in_filters_rows():
    df = pl.DataFrame({"status": ["paid", "pending", "cancelled", "paid"]})
    fs = filters_mod.normalize(
        [{"column": "status", "op": "in", "values": ["paid"]}]
    )
    out = filters_mod.apply(df, fs)
    assert out["status"].to_list() == ["paid", "paid"]


def test_apply_ni_filters_rows():
    df = pl.DataFrame({"status": ["paid", "pending", "cancelled"]})
    fs = filters_mod.normalize(
        [{"column": "status", "op": "ni", "values": ["cancelled"]}]
    )
    out = filters_mod.apply(df, fs)
    assert out["status"].to_list() == ["paid", "pending"]


def test_apply_skips_unknown_column():
    """Required for crossfilters that don't apply to every chart's dataset."""
    df = pl.DataFrame({"status": ["paid", "pending"]})
    fs = filters_mod.normalize(
        [{"column": "country", "op": "in", "values": ["US"]}]
    )
    assert filters_mod.apply(df, fs).height == 2


def test_apply_stringifies_for_numeric_columns():
    df = pl.DataFrame({"id": [1, 2, 3]})
    fs = filters_mod.normalize(
        [{"column": "id", "op": "in", "values": ["1", "3"]}]
    )
    out = filters_mod.apply(df, fs)
    assert out["id"].to_list() == [1, 3]


def test_decode_tokens_groups_by_column():
    tokens = ["pie_a|status=paid", "pie_a|status=pending", "pie_b|region=eu"]
    decoded = filters_mod.decode_tokens(tokens)
    by_col = {f.column: f for f in decoded}
    assert by_col["status"].values == ("paid", "pending")
    assert by_col["region"].values == ("eu",)
    assert all(f.op == "in" for f in decoded)


def test_decode_tokens_excludes_emitter():
    """The source chart's own tokens drop out — it sees its full dataset."""
    tokens = ["pie_a|status=paid", "pie_b|region=eu"]
    decoded = filters_mod.decode_tokens(tokens, exclude_emitter="pie_a")
    assert [f.column for f in decoded] == ["region"]


def test_emitted_by_groups_only_emitter_tokens():
    tokens = [
        "pie_a|status=paid",
        "pie_a|status=pending",
        "pie_b|region=eu",
    ]
    a = filters_mod.emitted_by(tokens, "pie_a")
    assert len(a) == 1
    assert a[0].column == "status"
    assert a[0].values == ("paid", "pending")
    assert filters_mod.emitted_by(tokens, "pie_b") == [
        filters_mod.Filter(column="region", op="in", values=("eu",))
    ]
    assert filters_mod.emitted_by(tokens, "ghost") == []


def test_active_values_for_picks_emitter_and_column():
    tokens = [
        "pie_a|status=paid",
        "pie_a|status=pending",
        "pie_b|status=paid",   # different emitter — ignored
        "pie_a|region=eu",     # different column — ignored
    ]
    assert filters_mod.active_values_for(tokens, "pie_a", "status") == {
        "paid",
        "pending",
    }
    assert filters_mod.active_values_for(tokens, "pie_a", "region") == {"eu"}
    assert filters_mod.active_values_for(tokens, "pie_b", "status") == {"paid"}


def test_toggle_token_adds_then_removes():
    tokens = ["pie|status=paid"]
    tokens = filters_mod.toggle_token(tokens, "pie|status=pending")
    assert tokens == ["pie|status=paid", "pie|status=pending"]
    tokens = filters_mod.toggle_token(tokens, "pie|status=paid")
    assert tokens == ["pie|status=pending"]


# --- between ------------------------------------------------------------------


def test_between_is_half_open():
    """`lo <= v < hi`, so adjacent buckets tile without double-counting a row."""
    import datetime

    import polars as pl

    df = pl.DataFrame({"d": [
        datetime.date(2026, 5, 31), datetime.date(2026, 6, 1),
        datetime.date(2026, 6, 30), datetime.date(2026, 7, 1),
    ]})
    june = filters_mod.normalize(
        [{"column": "d", "op": "between", "values": ["2026-06-01", "2026-07-01"]}]
    )
    kept = df.filter(*filters_mod.predicates(june, df.columns))["d"].to_list()
    assert kept == [datetime.date(2026, 6, 1), datetime.date(2026, 6, 30)]


def test_between_compares_numbers_numerically():
    """Numeric bounds opt out of the stringify-both-sides rule the other ops
    use — lexicographically "10" < "9", which would silently mis-filter."""
    import polars as pl

    df = pl.DataFrame({"n": [8, 9, 10, 11]})
    f = filters_mod.normalize([{"column": "n", "op": "between", "values": [9, 11]}])
    assert df.filter(*filters_mod.predicates(f, df.columns))["n"].to_list() == [9, 10]


def test_between_requires_exactly_two_values():
    with pytest.raises(filters_mod.FilterError, match="exactly two values"):
        filters_mod.normalize([{"column": "d", "op": "between", "values": ["a"]}])


def test_compound_token_is_one_selection():
    """A bar segment's token carries both dimensions and toggles as a unit."""
    token = (
        "b|"
        + filters_mod.range_part("day", "2026-06-01", "2026-07-01")
        + "|"
        + filters_mod.value_part("status", "paid")
    )
    by_col = {f.column: f for f in filters_mod.decode_tokens([token])}
    assert by_col["day"].op == "between"
    assert by_col["status"].values == ("paid",)
    assert filters_mod.toggle_token([token], token) == []      # clears both halves
    # The emitter is still exempt from its own selection.
    assert filters_mod.decode_tokens([token], exclude_emitter="b") == []


def test_value_containing_a_range_separator_still_parses():
    """`=` is tested before `~`, so a value with a tilde isn't mistaken for a
    range."""
    assert filters_mod.decode_tokens(["p|status=a~b"])[0].values == ("a~b",)


@pytest.mark.parametrize(
    "low, high, expected",
    [
        # Midnight on a bucket edge says nothing the date doesn't.
        ("2026-02-01 00:00:00+00:00", "2026-03-01 00:00:00+00:00",
         "2026-02-01–2026-03-01"),
        ("2026-02-01 00:00:00", "2026-03-01 00:00:00", "2026-02-01–2026-03-01"),
        ("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z", "2026-02-01–2026-03-01"),
        ("2026-02-01", "2026-03-01", "2026-02-01–2026-03-01"),
        # A real time is information — keep it.
        ("2026-02-01 10:30:00+00:00", "2026-03-01 10:30:00+00:00",
         "2026-02-01 10:30:00+00:00–2026-03-01 10:30:00+00:00"),
        # A non-UTC midnight is a different instant from the bare date, so
        # trimming it would change what the filter says.
        ("2026-02-01 00:00:00+03:00", "2026-03-01 00:00:00+03:00",
         "2026-02-01 00:00:00+03:00–2026-03-01 00:00:00+03:00"),
    ],
)
def test_between_values_text_trims_midnight(low, high, expected):
    assert filters_mod.Filter("d", "between", (low, high)).values_text == expected


def test_values_text_leaves_other_ops_alone():
    assert filters_mod.Filter("s", "in", ("a", "b")).values_text == "a, b"
    # Trimming is display only — the stored values still round-trip exactly.
    f = filters_mod.Filter("d", "between", ("2026-02-01 00:00:00+00:00", "x"))
    assert f.values == ("2026-02-01 00:00:00+00:00", "x")
