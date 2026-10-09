"""A one-field date-range picker: the field shows the range, a click opens a
two-month calendar, the first day clicked starts the range and the second ends
it. The filter fields use it for `between` on a date column — the dashboard's
filter panel and the editor's chart builder alike.

No JavaScript: the popover is a `<details>`, and every click on a day or a month
arrow is an htmx GET to `/filter/range` carrying the picker's whole state — the
range so far and the month on show — which the server applies (`pick`) and
answers with the picker re-rendered. Being a GET that carries its own state, it
never drags a surrounding form along, which is what lets one control serve a
panel with one filter row and a builder with many.

The range it submits is inclusive — `filter_from` / `filter_to`, the first and
last day — and `params.parse_filters` turns that into the model's half-open
bounds.
"""

import calendar
from datetime import date
from pathlib import Path

import jinja2

RANGE_ENDPOINT = "/filter/range"

_TEMPLATE = jinja2.Template(
    (Path(__file__).parent / "date_range.html").read_text(), autoescape=True
)
# Weeks start on Monday (ISO 8601).
_WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_CALENDAR = calendar.Calendar(firstweekday=0)


def _day(text) -> date | None:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        return None


def _month(text) -> date | None:
    """The first of the month for `YYYY-MM`, or None."""
    return _day(f"{text}-01") if text else None


def _shift(month: date, by: int) -> date:
    index = month.year * 12 + month.month - 1 + by
    return date(index // 12, index % 12 + 1, 1)


def pick(first: str, last: str, day: str) -> tuple[str, str]:
    """The range after a click on `day`. With nothing started — or a range
    already complete — the click starts a new one. With a start, a later (or
    the same) day ends it, and an earlier one moves the start there instead."""
    if not first or last or day < first:
        return day, ""
    return first, day


def _label(first: date | None, last: date | None) -> str:
    """`Jun 1 – Jun 30, 2026`; the year once when both ends share it."""
    if first is None:
        return ""
    if last is None:
        return f"{first:%b} {first.day}, {first.year} –"
    if first == last:
        return f"{first:%b} {first.day}, {first.year}"
    if first.year == last.year:
        return f"{first:%b} {first.day} – {last:%b} {last.day}, {last.year}"
    return f"{first:%b} {first.day}, {first.year} – {last:%b} {last.day}, {last.year}"


def _month_grid(month: date, first, last, state: dict) -> dict:
    today = date.today()
    days = []
    for day in _CALENDAR.itermonthdates(month.year, month.month):
        if day.month != month.month:
            days.append(None)                  # padding before the 1st / after the end
            continue
        classes = [
            name for name, on in (
                ("is-start", day == first),
                ("is-end", day == last),
                ("in-range", first and last and first < day < last),
                ("is-today", day == today),
            ) if on
        ]
        days.append({
            "num": day.day,
            "label": f"{day:%A}, {day:%B} {day.day}, {day.year}",
            "classes": " ".join(classes),
            "vals": {**state, "pick": day.isoformat()},
        })
    return {"title": f"{month:%B %Y}", "days": days}


def render(first="", last="", month="", anchor="", is_open=False, required=False) -> str:
    """The picker. `first` / `last` are inclusive ISO days ("" when unset).
    It shows `month` (`YYYY-MM`) and the one after; without one it opens on the
    range's start, else on `anchor` — the latest day in the data, so a picker
    over last year's orders doesn't open on an empty today. `required` (the
    panel, whose + waits for both ends) is part of the state every click carries,
    so it survives the re-render."""
    first_day, last_day = _day(first), _day(last)
    shown = (
        _month(month)
        or (first_day and first_day.replace(day=1))
        or (_day(anchor) and _day(anchor).replace(day=1))
        or date.today().replace(day=1)
    )
    state = {
        "from": first_day.isoformat() if first_day else "",
        "to": last_day.isoformat() if last_day else "",
        "month": f"{shown:%Y-%m}",
        "required": "1" if required else "",
    }
    if first_day is None:
        hint = "Select the first day"
    elif last_day is None:
        hint = "Select the last day"
    else:
        hint = _label(first_day, last_day)
    return _TEMPLATE.render(
        first=state["from"],
        last=state["to"],
        label=_label(first_day, last_day),
        hint=hint,
        complete=bool(first_day and last_day),
        picking=bool(first_day and not last_day),
        is_open=is_open,
        required=required,
        weekdays=_WEEKDAYS,
        months=[_month_grid(m, first_day, last_day, state) for m in (shown, _shift(shown, 1))],
        state=state,
        prev={**state, "month": f"{_shift(shown, -1):%Y-%m}"},
        next={**state, "month": f"{_shift(shown, 1):%Y-%m}"},
        endpoint=RANGE_ENDPOINT,
    )


def _typed(text: str) -> date | None:
    """A typed day: ISO `2026-06-01`, or the same with `/` or `.` between."""
    return _day(text.strip().replace("/", "-").replace(".", "-"))


def type_day(first: str, last: str, end: str, text: str) -> tuple[str, str]:
    """The range after typing `text` into its Start (`end="from"`) or End
    (`end="to"`) field. Text that isn't a day changes nothing — the field shows
    the range again. A cleared Start clears the range, a cleared End reopens
    it. An End before the Start swaps them, so the range always runs forward."""
    if not text.strip():
        return ("", "") if end == "from" else (first, "")
    typed = _typed(text)
    if typed is None:
        return first, last
    typed = typed.isoformat()
    if end == "from":
        return (typed, last) if not last or typed <= last else (typed, "")
    if not first:
        return typed, ""
    return (first, typed) if typed >= first else (typed, first)


def respond(first="", last="", month="", day="", required=False, typed=None) -> str:
    """The `/filter/range` answer: the picker after a click on `day`, a month
    arrow (no `day`), or a date typed into Start or End (`typed`: which end,
    and the text). A click closes it once the range is complete — the field
    then shows it, and a click reopens it to adjust. Typing leaves it open, on
    the typed day's month: the other end usually comes next."""
    if typed is not None:
        end, text = typed
        first, last = type_day(first, last, end, text)
        shown = _day(last if end == "to" and last else first)
        month = f"{shown:%Y-%m}" if shown else month
        return render(first, last, month, is_open=True, required=required)
    if _day(day):
        first, last = pick(first, last, day)
        return render(first, last, month, is_open=not last, required=required)
    return render(first, last, month, is_open=True, required=required)
