# Number chart

## Purpose
Display a single aggregated scalar — a "big number" KPI — from one column.

## Behavior
- Reads the CSV.
- Applies the chart's `filters` (see architecture.md "Filters") before aggregating.
- Reduces `column` to one value using `agg`:
  - `count` — number of **non-null** values.
  - `sum` — sum of the values.
  - `dcount` — number of **distinct** (non-null) values.
  - `max` — largest value.
  - `min` — smallest value.
- Formats the number per `format`:
  - `compact` (default) — BI-standard abbreviation to ~3 significant figures
    with a lowercase `k`/`m`/`b`/`t` suffix: `1,420 → 1.42k`, `3,000,000 → 3m`,
    `12,300,000 → 12.3m`. Values below 1000 show plainly. This mirrors
    d3's `~s` format and keeps the KPI legible in a small cell.
  - `full` — every digit, thousands-separated: `1,420`, `12,300,000`.
  - Both drop trailing decimal zeros, so a whole value never shows `333.00`.
  - Strings/dates (from `max`/`min` on a text column) pass through unformatted.
- On empty data, `max`/`min` render an em dash (`—`); `count`/`dcount`/`sum`
  render `0`.
- Renders the value large and centered, in the chart's primary text color
  (`--ff-ink` — near-black in light mode, near-white in dark), with no caption
  beneath it. The value carries a `title` attribute with the un-abbreviated
  figure, so hovering a compact `1.42K` reveals `1,420`. No other interactivity —
  a scalar has nothing to click, so the chart is not a crossfilter source.

## Theming
Card and text colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.

## Parameters
- `dataset: str` — path to the CSV.
- `title: str` — chart title.
- `column: str` — the column to aggregate.
- `agg: str = "count"` — one of `count`, `sum`, `dcount`, `max`, `min`.
  An unknown value raises at construction time.
- `format: str = "compact"` — `compact` (abbreviated, the default) or `full`
  (all digits). An unknown value raises at construction time.
- `filters: list = []` — declarative pre-filter applied before aggregating.

## Editor params
Edit-modal schema (`Number.PARAMS`): dataset (dropdown), title (text), column (column dropdown),
agg (choice), format (choice), filters (filter builder). Widgets live in `fireflyer/params.py`.
