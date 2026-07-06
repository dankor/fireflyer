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
