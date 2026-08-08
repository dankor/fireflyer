import pytest
import yaml

import fireflyer as ff
from fireflyer import measures_edit as me


def _doc() -> str:
    return """name: Sales

measures:
  orders:
    revenue: {agg: sum, formula: amount}

charts:
  kpi:
    type: number
    dataset: orders
    title: Revenue        # keep this comment
    measure: revenue
dashboard:
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


def test_upsert_adds_measure_preserves_siblings():
    out = me.upsert_measure(
        _doc(), "orders", "orders_count", {"agg": "count", "name": "Orders"}
    )
    block = yaml.safe_load(out)["measures"]["orders"]
    assert "orders_count" in block and block["orders_count"]["agg"] == "count"
    assert "revenue" in block                      # existing measure kept
    assert "title: Revenue        # keep this comment" in out  # chart untouched
    ff.Dashboard.from_yaml(out, datasets=lambda n: ("tests/data/orders.parquet", None))


def test_upsert_creates_block_when_absent():
    text = """name: Sales
charts:
  kpi: {type: number, dataset: orders, title: K}
dashboard:
  - ["@20", "kpi:1"]
"""
    out = me.upsert_measure(text, "orders", "n", {"agg": "count"})
    assert yaml.safe_load(out)["measures"]["orders"]["n"]["agg"] == "count"
    assert "charts:" in out and "dashboard:" in out


def test_upsert_rename_drops_old_key():
    # Rename an unreferenced measure so the doc still validates.
    text = me.upsert_measure(_doc(), "orders", "spare", {"agg": "count"})
    out = me.upsert_measure(
        text, "orders", "spare_renamed", {"agg": "count"}, original_key="spare"
    )
    block = yaml.safe_load(out)["measures"]["orders"]
    assert "spare_renamed" in block and "spare" not in block
    assert "revenue" in block  # untouched


def test_upsert_rename_referenced_measure_surfaces_dangling_ref():
    # `revenue` is used by chart `kpi`; renaming it leaves a dangling reference.
    with pytest.raises(me.MeasuresEditError, match="unknown measure"):
        me.upsert_measure(
            _doc(), "orders", "rev", {"agg": "sum", "formula": "amount"},
            original_key="revenue",
        )


def test_upsert_rejects_bad_definition():
    with pytest.raises(me.MeasuresEditError, match="agg"):
        me.upsert_measure(_doc(), "orders", "bad", {"agg": "median", "formula": "amount"})


def test_delete_measure_guarded_by_chart_reference():
    with pytest.raises(me.MeasuresEditError, match="used by chart"):
        me.delete_measure(_doc(), "orders", "revenue")


def test_delete_measure_removes_and_prunes_block():
    # Add a second, unreferenced measure, then delete it.
    text = me.upsert_measure(_doc(), "orders", "spare", {"agg": "count"})
    out = me.delete_measure(text, "orders", "spare")
    assert "spare" not in yaml.safe_load(out)["measures"]["orders"]
    assert "revenue" in yaml.safe_load(out)["measures"]["orders"]


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
    form = FakeForm(single={"kind": "derived", "formula": "a / b", "format": "0.0%"})
    d = me.definition_from_form(form)
    assert d == {"formula": "a / b", "format": "0.0%"}
    assert "agg" not in d
