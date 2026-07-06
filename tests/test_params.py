"""Unit tests for the shared editor param widgets."""

from fireflyer.params import (
    BoolParam,
    ChoiceParam,
    ColumnParam,
    DatasetParam,
    FilterListParam,
    IntParam,
    ParamContext,
    TextParam,
)


class FakeForm:
    """Stand-in for Starlette's FormData in unit tests."""

    def __init__(self, single=None, multi=None):
        self._single = single or {}
        self._multi = multi or {}

    def get(self, key, default=None):
        return self._single.get(key, default)

    def getlist(self, key):
        return self._multi.get(key, [])


CTX = ParamContext(
    datasets={"orders": "o.csv", "events": "e.csv"},
    dataset_id="orders",
    columns=["id", "status", "amount"],
)


def test_text_param_render_and_parse():
    p = TextParam("title", "Title")
    html = p.render("Revenue", CTX)
    assert 'name="title"' in html and 'value="Revenue"' in html
    assert p.parse(FakeForm({"title": "  Sales "})) == "Sales"


def test_dataset_param_lists_datasets_and_selects_current():
    p = DatasetParam("dataset", "Dataset")
    html = p.render("orders", CTX)
    assert '<option value="orders" selected>' in html
    assert '<option value="events">' in html


def test_column_param_options_from_context():
    p = ColumnParam("column", "Column")
    html = p.render("status", CTX)
    assert '<option value="status" selected>' in html
    assert '<option value="amount">' in html


def test_column_param_keeps_unknown_current_value():
    """A column not present in the CSV header is still offered, so editing a
    chart never silently drops a hand-written column name."""
    p = ColumnParam("column", "Column")
    html = p.render("ghost", CTX)
    assert '<option value="ghost" selected>' in html


def test_choice_param_render_and_parse():
    p = ChoiceParam("agg", "Aggregation", ["count", "sum", "max"])
    html = p.render("sum", CTX)
    assert '<option value="sum" selected>' in html
    assert p.parse(FakeForm({"agg": "max"})) == "max"


def test_int_param_parses_int():
    p = IntParam("pagination", "Rows", minimum=0)
    assert p.parse(FakeForm({"pagination": "25"})) == 25


def test_int_param_nullable_blank_is_none():
    p = IntParam("zoom", "Zoom", nullable=True)
    assert p.parse(FakeForm({"zoom": ""})) is None
    # to_yaml passes None through; the emitter drops it.
    assert p.to_yaml(None) is None
    assert '<input class="ff-input" type="number"' in p.render(None, CTX)


def test_bool_param_present_is_true_absent_is_false():
    p = BoolParam("search", "Search box")
    assert p.parse(FakeForm({"search": "true"})) is True
    assert p.parse(FakeForm({})) is False
    assert "checkbox" in p.render(True, CTX) and "checked" in p.render(True, CTX)
    assert "checked" not in p.render(False, CTX)


def test_filter_list_renders_rows_and_template():
    p = FilterListParam("filters", "Filters")
    html = p.render([{"column": "status", "op": "in", "values": ["paid", "pending"]}], CTX)
    assert "ff-filter-add" in html and "ff-filter-tpl" in html
    assert 'value="paid, pending"' in html
    assert '<option value="status" selected>' in html


def test_filter_list_parse_zips_and_skips_empty():
    p = FilterListParam("filters", "Filters")
    form = FakeForm(multi={
        "filter_column": ["status", "", "amount"],   # middle row has no column
        "filter_op": ["in", "ni", "ni"],
        "filter_values": ["paid, pending", "x", ""],  # last row has no values
    })
    assert p.parse(form) == [
        {"column": "status", "op": "in", "values": ["paid", "pending"]},
    ]
