"""The one-field date-range picker (`fireflyer/date_range.py`)."""

import json
import re
from html import unescape

import pytest

from fireflyer import date_range


@pytest.mark.parametrize("first, last, day, expected", [
    ("", "", "2026-06-05", ("2026-06-05", "")),                     # starts a range
    ("2026-06-05", "", "2026-06-09", ("2026-06-05", "2026-06-09")),  # ends it
    ("2026-06-05", "", "2026-06-05", ("2026-06-05", "2026-06-05")),  # one day
    ("2026-06-05", "", "2026-06-01", ("2026-06-01", "")),           # earlier: moves start
    ("2026-06-05", "2026-06-09", "2026-06-20", ("2026-06-20", "")),  # complete: starts over
])
def test_pick(first, last, day, expected):
    assert date_range.pick(first, last, day) == expected


def _days(html):
    return {
        int(n): cls.split()
        for cls, n in re.findall(r'class="ff-cal-day ?([^"]*)"[^>]*>(\d+)</button>', html)
    }


def test_the_range_is_marked_and_labelled():
    html = date_range.render("2026-06-03", "2026-06-05")
    june = _days(html.split('class="ff-cal-month"')[1])
    assert "is-start" in june[3] and "in-range" in june[4] and "is-end" in june[5]
    assert "in-range" not in june[2] and "in-range" not in june[6]
    assert '<span class="ff-range-label">Jun 3 – Jun 5, 2026</span>' in html
    assert "is-complete" in html


def test_it_shows_two_months_from_the_start_or_the_anchor():
    titles = lambda h: re.findall(r'class="ff-cal-title">([^<]+)<', h)
    assert titles(date_range.render("2026-06-03")) == ["June 2026", "July 2026"]
    assert titles(date_range.render(anchor="2025-12-31")) == ["December 2025", "January 2026"]


def test_every_click_carries_the_whole_state_and_nothing_else():
    """A GET that brings its own state: it never drags the panel's form — or
    the dashboard's hidden inputs — into the URL."""
    html = date_range.render("2026-06-03", month="2026-06", required=True)
    button = re.search(r'<button type="button" class="ff-cal-day[^"]*"[^>]*>', html).group(0)
    vals = json.loads(unescape(re.search(r"hx-vals='([^']*)'", button).group(1)))
    assert vals == {"from": "2026-06-03", "to": "", "month": "2026-06",
                    "pick": "2026-06-01", "required": "1"}
    assert 'hx-get="/filter/range"' in button and 'hx-include="this"' in button
    # What it may send is pinned below: `test_each_element_lists_exactly_the_params_it_sends`.


def test_it_stays_open_while_picking_and_closes_when_complete():
    started = date_range.respond(day="2026-06-03", month="2026-06")
    assert re.search(r'<details class="ff-range is-picking" open>', started)
    done = date_range.respond("2026-06-03", "", "2026-06", "2026-06-07")
    assert re.search(r'<details class="ff-range is-complete">', done)
    paged = date_range.respond("2026-06-03", "", "2026-07")          # a month arrow
    assert " open>" in paged and "July 2026" in paged


def test_required_survives_the_re_render():
    """The panel's + waits for both ends; a click mustn't drop that."""
    html = date_range.respond(day="2026-06-03", month="2026-06", required=True)
    assert '<input name="filter_from" value="2026-06-03" hidden required>' in html
    assert '<input name="filter_to" value="" hidden required>' in html


@pytest.mark.parametrize("first, last, end, text, expected", [
    ("", "", "from", "2026-06-05", ("2026-06-05", "")),
    ("2026-06-05", "", "to", "2026-06-09", ("2026-06-05", "2026-06-09")),
    ("2026-06-05", "", "to", "2026/06/09", ("2026-06-05", "2026-06-09")),   # `/` too
    ("2026-06-05", "2026-06-09", "from", "2026.06.01", ("2026-06-01", "2026-06-09")),
    ("2026-06-05", "", "to", "2026-06-01", ("2026-06-01", "2026-06-05")),   # swapped
    ("2026-06-05", "2026-06-09", "from", "2026-06-20", ("2026-06-20", "")),  # past End
    ("", "", "to", "2026-06-09", ("2026-06-09", "")),                      # End alone
    ("2026-06-05", "2026-06-09", "from", "next week", ("2026-06-05", "2026-06-09")),
    ("2026-06-05", "2026-06-09", "to", "2026-02-30", ("2026-06-05", "2026-06-09")),
    ("2026-06-05", "2026-06-09", "from", " ", ("", "")),                   # cleared Start
    ("2026-06-05", "2026-06-09", "to", "", ("2026-06-05", "")),            # cleared End
])
def test_type_day(first, last, end, text, expected):
    assert date_range.type_day(first, last, end, text) == expected


def test_the_typed_fields_show_the_range_and_stay_outside_the_form():
    """Enter in them must re-render the picker, not submit the panel's +."""
    html = date_range.render("2026-06-03", "2026-06-05")
    for name, value in (("typed_from", "2026-06-03"), ("typed_to", "2026-06-05")):
        box = re.search(rf'<input class="ff-input" name="{name}"[^>]*>', html).group(0)
        assert f'value="{value}"' in box and 'form="ff-none"' in box
        assert "hx-trigger=\"change, keyup[key=='Enter']\"" in box


def test_typing_keeps_it_open_on_the_typed_days_month():
    html = date_range.respond("2026-06-03", "", "2026-06", typed=("to", "2026-08-20"))
    assert re.search(r'<details class="ff-range is-complete" open>', html)
    assert re.findall(r'class="ff-cal-title">([^<]+)<', html)[0] == "August 2026"
    assert '<input name="filter_to" value="2026-08-20" hidden>' in html


def test_each_element_lists_exactly_the_params_it_sends():
    """htmx sends a name listed in `hx-params` that the element doesn't have as
    the string "undefined". Listing the typed-date names on every button made
    each calendar click arrive as a garbage typed date — and get ignored."""
    html = date_range.render("2026-06-03", month="2026-06", required=True)
    elements = re.findall(r"<(?:button|input)\b[^>]*hx-get[^>]*>", html)
    assert elements
    for el in elements:
        sent = set(json.loads(unescape(re.search(r"hx-vals='([^']*)'", el).group(1))))
        own = re.search(r'\bname="([^"]+)"', el)
        if own:
            sent.add(own.group(1))
        listed = set(re.search(r'hx-params="([^"]*)"', el).group(1).split(","))
        assert listed == sent, el[:90]
