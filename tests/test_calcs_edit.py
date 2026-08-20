import pytest
import yaml

import fireflyer as ff
from fireflyer import calcs_edit as me


def _doc() -> str:
    return """name: Sales

calcs:
  orders:
    revenue: {agg: sum, formula: amount}

charts:
  kpi:
    type: number
    dataset: orders
    title: Revenue        # keep this comment
    calc: revenue
layout:
  - ["@20", "kpi:1"]
"""


class FakeForm:
    def __init__(self, single=None, multi=None):
        self._single = single or {}
        self._multi = multi or {}

    def get(self, key):
        return self._single.get(key)

    def getlist(self, key):
        return self._multi.get(key, [])


def test_list_for_dataset():
    got = me.list_for_dataset(_doc(), "orders")
    assert "revenue" in got and got["revenue"]["agg"] == "sum"


def test_chart_datasets():
    assert me.chart_datasets(_doc()) == ["orders"]


def test_upsert_adds_calc_preserves_siblings():
    out = me.upsert_calc(
        _doc(), "orders", "orders_count", {"agg": "count", "name": "Orders"}
    )
    block = yaml.safe_load(out)["calcs"]["orders"]
    assert "orders_count" in block and block["orders_count"]["agg"] == "count"
    assert "revenue" in block                      # existing calc kept
    assert "title: Revenue        # keep this comment" in out  # chart untouched
    ff.Dashboard.from_yaml(out, datasets=lambda n: ("tests/data/orders.parquet", None))


def test_upsert_creates_block_when_absent():
    text = """name: Sales
charts:
  kpi: {type: number, dataset: orders, title: K}
layout:
  - ["@20", "kpi:1"]
"""
    out = me.upsert_calc(text, "orders", "n", {"agg": "count"})
    assert yaml.safe_load(out)["calcs"]["orders"]["n"]["agg"] == "count"
    assert "charts:" in out and "layout:" in out


def test_upsert_rename_drops_old_key():
    # Rename an unreferenced calc so the doc still validates.
    text = me.upsert_calc(_doc(), "orders", "spare", {"agg": "count"})
    out = me.upsert_calc(
        text, "orders", "spare_renamed", {"agg": "count"}, original_key="spare"
    )
    block = yaml.safe_load(out)["calcs"]["orders"]
    assert "spare_renamed" in block and "spare" not in block
    assert "revenue" in block  # untouched


def test_upsert_rename_referenced_calc_surfaces_dangling_ref():
    # `revenue` is used by chart `kpi`; renaming it leaves a dangling reference.
    with pytest.raises(me.CalcsEditError, match="unknown calc"):
        me.upsert_calc(
            _doc(), "orders", "rev", {"agg": "sum", "formula": "amount"},
            original_key="revenue",
        )


def test_upsert_rejects_bad_definition():
    with pytest.raises(me.CalcsEditError, match="agg"):
        me.upsert_calc(_doc(), "orders", "bad", {"agg": "median", "formula": "amount"})


def test_delete_calc_guarded_by_chart_reference():
    with pytest.raises(me.CalcsEditError, match="used by chart"):
        me.delete_calc(_doc(), "orders", "revenue")


def test_delete_calc_removes_and_prunes_block():
    # Add a second, unreferenced calc, then delete it.
    text = me.upsert_calc(_doc(), "orders", "spare", {"agg": "count"})
    out = me.delete_calc(text, "orders", "spare")
    assert "spare" not in yaml.safe_load(out)["calcs"]["orders"]
    assert "revenue" in yaml.safe_load(out)["calcs"]["orders"]


def test_definition_from_form_aggregate():
    form = FakeForm(
        single={"kind": "aggregate", "agg": "sum", "formula": "amount",
                "name": "Revenue", "format": "0.00$"},
        multi={"filter_column": ["status"], "filter_op": ["in"],
               "filter_values": ["paid"]},
    )
    d = me.definition_from_form(form)
    assert d == {
        "name": "Revenue", "agg": "sum", "formula": "amount",
        "filters": [{"column": "status", "op": "in", "values": ["paid"]}],
        "format": "0.00$",
    }


def test_definition_from_form_derived():
    form = FakeForm(single={"kind": "formula", "formula": "a / b", "format": "0.0%"})
    d = me.definition_from_form(form)
    assert d == {"formula": "a / b", "format": "0.0%"}
    assert "agg" not in d


def test_definition_from_form_column():
    """A calculated column is just a formula — no `kind`, no agg, no filters
    (which the form still submits and the builder drops)."""
    form = FakeForm(
        single={"kind": "formula", "formula": "str2dt(day, YYYYMMDD)", "name": "Order day"},
        multi={"filter_column": ["status"], "filter_op": ["in"],
               "filter_values": ["paid"]},
    )
    d = me.definition_from_form(form)
    assert d == {"name": "Order day", "formula": "str2dt(day, YYYYMMDD)"}


def test_upsert_column_calc_round_trips():
    text = _doc()
    out = me.upsert_calc(
        text, "orders", "order_day", {"formula": "str2dt(day, YYYY-MM-DD)"},
    )
    assert yaml.safe_load(out)["calcs"]["orders"]["order_day"] == {
        "formula": "str2dt(day, YYYY-MM-DD)",
    }
