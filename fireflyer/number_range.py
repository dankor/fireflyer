"""A number-range picker: a "from" and a "to" field over a long two-handle
slider spanning the column's values, as a shop filters on price. The filter
fields use it for `between` on a number column — the dashboard's filter panel
and the editor's chart builder alike. Both ends are inclusive (see
`filters._between`).

No JavaScript. The two number fields are what's submitted (`filter_low`,
`filter_high`); the slider moves them. Each change is an htmx GET to
`/filter/number` carrying the picker's values and the column's extent, and the
server answers with the part that has to catch up: a handle let go redraws the
whole picker, fields included; a number typed redraws only the slider, so the
field being typed in keeps its focus. A GET that brings its own state never
drags a surrounding form along, which is what lets one control serve a panel
with one filter row and a builder with many.
"""

import json
import math
from html import escape
from pathlib import Path

import jinja2

NUMBER_ENDPOINT = "/filter/number"

_TEMPLATE = jinja2.Template(
    (Path(__file__).parent / "number_range.html").read_text(), autoescape=True
)


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value) -> str:
    """A bound as a person writes it: `10`, not `10.0`; `2.5` as is."""
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _clamp(value, low, high):
    return min(max(value, low), high)


def slider_step(mn: float, mx: float, integer: bool) -> float:
    """How far one notch of the slider moves: 1 for whole numbers; otherwise a
    round power of ten about a hundredth of the span — 1 over 6–443, 0.01 over
    0–5 — so a dragged handle lands on 109, not 109.086300952381. A typed number
    keeps whatever precision it was typed with."""
    if integer:
        return 1.0
    span = mx - mn
    return 10.0 ** math.floor(math.log10(span / 100)) if span > 0 else 1.0


def _snap(value: float, step: float, bound: float, up: bool) -> float:
    """`value` on a notch — down for a "from", up for a "to", so a range set by
    the handles never cuts off the rows beneath them (both ends are inclusive).
    A handle within a notch of the track's end takes the column's exact `bound`:
    a slider stops only on `min + k·step`, so over 6.07–443.05 its last stop is
    442.07 — the end it's resting at has to mean 443.05, or the largest rows go."""
    if (value > bound - step) if up else (value < bound + step):
        return bound
    places = max(0, -math.floor(math.log10(step)))
    notches = math.ceil(value / step - 1e-9) if up else math.floor(value / step + 1e-9)
    return round(notches * step, places)


def _wiring(state: dict, changed: str, sent: list[str], target: str) -> str:
    """The htmx attributes on one input: it reports itself as `changed`, sends
    the picker's inputs (`hx-include`) and its state (`hx-vals`) — and, in
    `hx-params`, exactly those names: htmx sends a listed name it doesn't have
    as the string "undefined" (see date_range.html)."""
    vals = {**state, "changed": changed}
    params = ",".join(sent + list(vals))
    return (
        f'hx-get="{NUMBER_ENDPOINT}" hx-trigger="change" hx-include="closest .ff-numrange"'
        f' hx-vals="{escape(json.dumps(vals), quote=True)}" hx-params="{params}"'
        f' hx-target="{target}" hx-swap="outerHTML"'
    )


def render(low="", high="", extent=None, integer=False, required=False, part="all") -> str:
    """The picker. `low` / `high` are the chosen bounds ("" while unset).
    `extent` is the column's `(min, max)`; without it (an unreadable column)
    there's no slider, just the two fields. `integer` steps the slider (and the
    fields) by whole numbers. `required` (the panel, whose + waits for both
    ends) rides in every request, so it survives the re-render. `part="track"`
    renders the slider alone — what a typed number redraws."""
    lo, hi = _number(low), _number(high)
    state = {"required": "1" if required else "", "integer": "1" if integer else ""}
    track = None
    if extent is not None and extent[0] is not None and extent[1] is not None:
        mn, mx = float(extent[0]), float(extent[1])
        span = (mx - mn) or 1.0
        at_lo = _clamp(lo if lo is not None else mn, mn, mx)
        at_hi = _clamp(hi if hi is not None else mx, mn, mx)
        notch = slider_step(mn, mx, integer)
        state.update(min=fmt(mn), max=fmt(mx), step=fmt(notch))
        track = {
            "step": fmt(notch),
            "min": fmt(mn),
            "max": fmt(mx),
            "low": fmt(at_lo),
            "high": fmt(at_hi),
            # Where the shaded stretch starts and ends, 0..1 along the track.
            "lo_at": round((at_lo - mn) / span, 4),
            "hi_at": round((at_hi - mn) / span, 4),
            # Both handles on one spot at the top: the low one has to be the
            # one on top, or it can never be dragged back off the end.
            "low_on_top": at_lo >= mx - span * 0.02,
        }
    # Without a slider there's nothing for the fields to keep in step: they're
    # plain inputs then, sending nothing until the row is submitted.
    sent = ["filter_low", "filter_high", "slide_low", "slide_high"]
    wiring = {
        name: _wiring(state, name, sent, target) if track else ""
        for name, target in (
            ("filter_low", "next .ff-numrange-track"),
            ("filter_high", "next .ff-numrange-track"),
            ("slide_low", "closest .ff-numrange"),
            ("slide_high", "closest .ff-numrange"),
        )
    }
    return _TEMPLATE.render(
        part=part,
        low=fmt(lo),
        high=fmt(hi),
        track=track,
        step="1" if integer else "any",
        required=required,
        wiring=wiring,
    )


def respond(params, changed: str) -> str:
    """The `/filter/number` answer. `changed` is the input that moved:

    - a handle (`slide_low` / `slide_high`): its value becomes that end, the
      other end takes its own handle's value (so one drag sets both), and the
      whole picker is redrawn, fields included;
    - a field (`filter_low` / `filter_high`): only the slider is redrawn, so the
      field keeps its focus and cursor.

    A handle dragged past the other takes the other along; typed ends that
    cross are put back in order."""
    integer = bool(params.get("integer"))
    extent = (_number(params.get("min")), _number(params.get("max")))
    if None in extent:
        extent = None
    low, high = params.get("filter_low", ""), params.get("filter_high", "")
    if changed in ("slide_low", "slide_high"):
        low = params.get("slide_low", low)
        high = params.get("slide_high", high)
    lo, hi = _number(low), _number(high)
    notch = _number(params.get("step"))
    if changed in ("slide_low", "slide_high") and notch and extent:
        # A handle lands on a notch; a typed number is left as typed.
        lo = _snap(lo, notch, extent[0], up=False) if lo is not None else None
        hi = _snap(hi, notch, extent[1], up=True) if hi is not None else None
    if integer:
        lo = round(lo) if lo is not None else None
        hi = round(hi) if hi is not None else None
    if lo is not None and hi is not None and lo > hi:
        if changed == "slide_low":
            hi = lo
        elif changed == "slide_high":
            lo = hi
        else:
            lo, hi = hi, lo
    part = "track" if changed in ("filter_low", "filter_high") else "all"
    return render(
        fmt(lo), fmt(hi), extent, integer=integer,
        required=bool(params.get("required")), part=part,
    )
