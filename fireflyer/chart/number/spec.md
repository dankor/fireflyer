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
    Superset's SMART_NUMBER / d3 `~s` and keeps the KPI legible in a small cell.
  - `full` — every digit, thousands-separated: `1,420`, `12,300,000`.
  - Both drop trailing decimal zeros, so a whole value never shows `333.00`.
  - Strings/dates (from `max`/`min` on a text column) pass through unformatted.
- On empty data, `max`/`min` render an em dash (`—`); `count`/`dcount`/`sum`
  render `0`.
- Renders the value large and centered, in the chart's default (near-black) text
  color, with no caption beneath it. The value carries a `title` attribute with
  the un-abbreviated figure, so hovering a compact `1.42K` reveals `1,420`. No
  other interactivity — a scalar has nothing to click, so the chart is not a
  crossfilter source.

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
