"""The number-range picker (`fireflyer/number_range.py`): from / to fields over
a two-handle slider spanning the column's values."""

import json
import re
from html import unescape

import pytest

from fireflyer import filters as filters_mod
from fireflyer import number_range
from fireflyer.params import NUMBER_KIND, filter_fields, filter_ops, parse_filters


def _fields(html):
    return (
        re.search(r'name="filter_low" value="([^"]*)"', html).group(1),
        re.search(r'name="filter_high" value="([^"]*)"', html).group(1),
    )


def _drag(handle, value, **state):
    params = {"filter_low": "", "filter_high": "", "slide_low": "6.07", "slide_high": "443.05",
              "min": "6.07", "max": "443.05", "step": "1", handle: value, **state}
    return _fields(number_range.respond(params, handle))


def test_numbers_get_a_range_op():
    assert [v for v, _ in filter_ops(NUMBER_KIND, "amount")] == ["in", "ni", "between"]
    assert [v for v, _ in filter_ops("text", "status")] == ["in", "ni"]


def test_a_number_range_includes_its_to():
    """A price from 10 to 20 means 20 too; dates stay half-open."""
    import polars as pl

    df = pl.DataFrame({"n": [9, 10, 20, 21]})
    f = filters_mod.normalize([{"column": "n", "op": "between", "values": ["10", "20"]}])
    assert df.filter(*filters_mod.predicates(f, df.columns))["n"].to_list() == [10, 20]


def test_the_picker_spans_the_columns_values():
    html = number_range.render("10", "40", (0, 100), integer=True)
    track = re.search(r'<div class="ff-numrange-track[^"]*" style="([^"]*)"', html).group(1)
    assert track == "--lo: 0.1; --hi: 0.4"
    assert 'min="0" max="100" step="1"' in html
    assert _fields(html) == ("10", "40")


@pytest.mark.parametrize("handle, value, expected", [
    ("slide_low", "109.086", ("109", "443.05")),   # on a notch, down; the other end kept
    ("slide_high", "200.4", ("6.07", "201")),      # a "to" snaps up, never cutting rows off
    ("slide_low", "6.07", ("6.07", "443.05")),     # resting on an end: the exact bound
    # A slider stops only on min + k·step: its last stop, 442.07, is the end.
    ("slide_high", "442.07", ("6.07", "443.05")),
])
def test_a_handle_lands_on_a_notch_without_cutting_rows_off(handle, value, expected):
    assert _drag(handle, value) == expected


def test_a_handle_dragged_past_the_other_takes_it_along():
    assert _drag("slide_low", "300", slide_high="200") == ("300", "300")
    assert _drag("slide_high", "100", slide_low="200") == ("100", "100")


def test_typed_ends_that_cross_are_put_in_order():
    params = {"filter_low": "50", "filter_high": "20", "min": "0", "max": "100", "step": "1"}
    html = number_range.respond(params, "filter_high")
    assert 'style="--lo: 0.2; --hi: 0.5"' in html


def test_a_typed_number_redraws_only_the_slider():
    """So the field being typed in keeps its focus and cursor."""
    params = {"filter_low": "10", "filter_high": "40", "min": "0", "max": "100", "step": "1"}
    html = number_range.respond(params, "filter_low")
    assert html.lstrip().startswith('<div class="ff-numrange-track') and "filter_low" not in html.split("hx-")[0]


def test_slider_step_is_a_round_notch():
    assert number_range.slider_step(6.07, 443.05, False) == 1
    assert number_range.slider_step(0, 5, False) == 0.01
    assert number_range.slider_step(1, 9, True) == 1


def test_each_input_lists_exactly_the_params_it_sends():
    """htmx sends a listed name it doesn't have as "undefined" (see the date
    range's equivalent test)."""
    html = number_range.render("", "", (0, 100), required=True)
    for el in re.findall(r"<input\b[^>]*hx-get[^>]*>", html):
        vals = set(json.loads(unescape(re.search(r'hx-vals="([^"]*)"', el).group(1))))
        listed = set(re.search(r'hx-params="([^"]*)"', el).group(1).split(","))
        assert listed == vals | {"filter_low", "filter_high", "slide_low", "slide_high"}, el[:80]


def test_the_slider_stays_out_of_the_tab_order_and_the_form():
    html = number_range.render("", "", (0, 100))
    for el in re.findall(r'<input type="range"[^>]*>', html):
        assert 'tabindex="-1"' in el and 'form="ff-none"' in el


def test_the_filter_fields_use_it_and_parse_it_back():
    html = filter_fields(["amount"], "amount", "between", ["10", "20"],
                         types={"amount": NUMBER_KIND}, extent=(0, 100, True))
    assert 'class="ff-numrange"' in html and _fields(html) == ("10", "20")

    class Form:
        def multi_items(self):
            return [("filter_column", "amount"), ("filter_op", "between"),
                    ("filter_low", "10"), ("filter_high", "20.5")]
    assert parse_filters(Form()) == [{"column": "amount", "op": "between", "values": ["10", "20.5"]}]


def test_without_an_extent_it_is_just_the_two_fields():
    html = number_range.render("1", "2", None)
    assert '<div class="ff-numrange-track' not in html and _fields(html) == ("1", "2")
    assert "hx-get" not in html            # nothing to keep in step, so nothing sent
