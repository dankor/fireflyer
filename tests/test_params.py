"""Unit tests for the shared editor param widgets."""

import re

import polars as pl
import pytest

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

    def multi_items(self):
        """Fields in document order, as a browser submits them: parallel lists
        interleave, so each filter row's column, op and values stay together."""
        items = list(self._single.items())
        longest = max((len(v) for v in self._multi.values()), default=0)
        for i in range(longest):
            items += [(k, v[i]) for k, v in self._multi.items() if i < len(v)]
        return items


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


def test_dataset_param_is_text_field_with_current_value():
    # Dataset is a name now (text field); a dropdown of stored datasets is
    # wired when the portal dataset gallery lands.
    p = DatasetParam("dataset", "Dataset")
    html = p.render("orders", CTX)
    assert 'type="text"' in html and 'name="dataset"' in html
    assert 'value="orders"' in html


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


# --- filter value picker ------------------------------------------------------

from fireflyer.params import ValueChoices, filter_fields, parse_filters, value_list


class OrderedForm:
    """A form as the browser submits it: an ordered list of (name, value)."""

    def __init__(self, items):
        self._items = items

    def multi_items(self):
        return list(self._items)


def test_in_and_not_in_list_the_columns_values_as_checkboxes():
    """With the values known, nobody has to remember or retype them."""
    for op in ("in", "ni"):
        html = filter_fields(["status"], "status", op, ["paid"],
                             choices=ValueChoices(["paid", "pending"]))
        assert 'type="text"' not in html
        assert '<input type="checkbox" name="filter_value" value="paid" checked>' in html
        assert '<input type="checkbox" name="filter_value" value="pending">' in html


def test_between_and_unknown_values_stay_a_text_input():
    """`between` takes two free bounds; and with no column (or too many values)
    there is nothing to list."""
    assert 'name="filter_values"' in filter_fields(["d"], "d", "between", ["a", "b"], choices=["a"])
    assert 'name="filter_values"' in filter_fields(["status"], "status", "in", ["x"], choices=None)
    assert 'value="x"' in filter_fields(["status"], "status", "in", ["x"], choices=None)


def test_a_chosen_value_the_data_no_longer_has_is_still_shown_ticked():
    """Opening an old filter shows what it says instead of quietly losing it."""
    html = filter_fields(["status"], "status", "in", ["gone"], choices=ValueChoices(["paid"]))
    assert html.index('value="gone" checked') < html.index('value="paid"')


def test_parse_keeps_each_rows_ticked_values_with_that_row():
    """Checkbox rows submit as many values as are ticked, or none, so rows are
    read in document order — not paired up by position."""
    form = OrderedForm([
        ("filter_column", "status"), ("filter_op", "in"),
        ("filter_value", "paid"), ("filter_value", "a, b"),
        ("filter_column", "region"), ("filter_op", "ni"),           # nothing ticked
        ("filter_column", "day"), ("filter_op", "between"),
        ("filter_values", "2026-01-01, 2026-02-01"),
    ])
    assert parse_filters(form) == [
        # A ticked value is taken whole — commas and all.
        {"column": "status", "op": "in", "values": ["paid", "a, b"]},
        {"column": "day", "op": "between", "values": ["2026-01-01", "2026-02-01"]},
    ]


# --- ops by column kind + date picker ---------------------------------------

from fireflyer.params import DATE_KIND, filter_ops


def _ops(kind, column="c", op=""):
    return [value for value, _ in filter_ops(kind, column, op)]


def test_between_is_offered_only_on_a_date_column():
    assert _ops(DATE_KIND) == ["in", "ni", "between"]
    assert _ops("other") == ["in", "ni"]
    assert _ops(None, column="") == ["in", "ni"]            # nothing picked yet
    assert _ops(None) == ["in", "ni", "between"]            # unreadable: withhold nothing


def test_an_existing_op_is_never_dropped_from_the_list():
    """A YAML `between` on a text column still shows as what it says."""
    assert _ops("other", op="between") == ["in", "ni", "between"]


def test_between_on_a_date_column_is_one_range_field():
    """Stored half-open; the field shows — and submits — the days it covers."""
    html = filter_fields(["d"], "d", "between", ["2026-06-01", "2026-07-01"], types={"d": DATE_KIND})
    assert '<span class="ff-range-label">Jun 1 – Jun 30, 2026</span>' in html
    assert '<input name="filter_from" value="2026-06-01" hidden>' in html
    assert '<input name="filter_to" value="2026-06-30" hidden>' in html
    assert 'name="filter_values"' not in html and 'type="date"' not in html


def test_a_range_with_a_time_in_it_stays_text():
    """An hourly bucket has no day-picker form; text keeps it intact."""
    html = filter_fields(["d"], "d", "between", ["2026-06-01 10:00:00", "2026-06-01 11:00:00"],
                         types={"d": DATE_KIND})
    assert 'type="date"' not in html and 'name="filter_values"' in html


def test_picked_days_parse_back_half_open():
    form = OrderedForm([
        ("filter_column", "d"), ("filter_op", "between"),
        ("filter_from", "2026-06-01"), ("filter_to", "2026-06-30"),
        ("filter_column", "e"), ("filter_op", "between"),
        ("filter_from", "2026-06-01"), ("filter_to", ""),          # unfinished
    ])
    assert parse_filters(form) == [
        {"column": "d", "op": "between", "values": ["2026-06-01", "2026-07-01"]},
    ]


# --- column type glyphs -------------------------------------------------------

from fireflyer.params import COLUMN_TYPES, column_type, type_glyph


@pytest.mark.parametrize("dtype, kind", [
    (pl.Int64, "number"), (pl.Float32, "number"), (pl.Decimal(10, 2), "number"),
    (pl.Boolean, "boolean"),
    (pl.Date, "date"), (pl.Datetime("us", "UTC"), "date"),
    (pl.String, "text"), (pl.Categorical, "text"),
    # A duration's name mentions a time unit; it is still no date.
    (pl.Duration("us"), "other"),
])
def test_column_type(dtype, kind):
    assert column_type(dtype) == kind
    assert column_type(str(dtype)) == kind          # the dataset page has strings


def test_each_column_option_is_led_by_its_type_glyph():
    html = filter_fields(["status", "amount"], types={"status": "text", "amount": "number"})
    assert f'<option value="status">{type_glyph("text")} <span>status</span></option>' in html
    assert f'{type_glyph("number")} <span>amount</span>' in html
    assert "<button><selectedcontent></selectedcontent></button>" in html
    # Unknown type: no icon, never a wrong one.
    assert type_glyph(None) == "" and 'value="x"> <span>x</span>' in filter_fields(["x"])


def test_each_type_icon_is_drawn_and_says_what_it_means():
    """Drawn, not a text glyph — `◷` and a boxed `T` were unreadable at 14px."""
    for kind, (_, name) in COLUMN_TYPES.items():
        glyph = type_glyph(kind)
        assert '<svg viewBox="0 0 16 16"' in glyph and 'stroke="currentColor"' in glyph
        assert f'title="{name}"' in glyph


# --- value search -------------------------------------------------------------

from fireflyer.params import SEARCH_FROM


def test_a_short_list_has_no_search_box():
    short = ValueChoices([str(i) for i in range(SEARCH_FROM)])
    assert 'name="filter_q"' not in filter_fields(["c"], "c", "in", choices=short)
    long = ValueChoices([str(i) for i in range(SEARCH_FROM + 1)])
    assert 'name="filter_q"' in filter_fields(["c"], "c", "in", choices=long)
    # "More than a page" always earns one, however few are shown.
    assert 'name="filter_q"' in filter_fields(["c"], "c", "in", choices=ValueChoices(["a"], more=True))


def test_the_search_box_is_outside_the_form():
    """Enter in it must not submit the panel's +, and its text is never saved."""
    many = ValueChoices([str(i) for i in range(20)])
    html = filter_fields(["c"], "c", "in", choices=many)
    box = re.search(r'<input[^>]*name="filter_q"[^>]*>', html).group(0)
    assert 'form="ff-none"' in box and 'type="search"' in box
    assert "hx-post" not in box                                  # builder: editor JS
    live = filter_fields(["c"], "c", "in", choices=many, live="orders")
    box = re.search(r'<input[^>]*name="filter_q"[^>]*>', live).group(0)
    assert 'hx-post="/filter/values"' in box and 'hx-target="next .ff-filter-choices"' in box
