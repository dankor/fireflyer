# Number chart

## Purpose
Display a single aggregated scalar — a "big number" KPI — from a measure.

## Behavior
- Reads the dataset.
- Applies the chart's `filters` (see architecture.md "Filters") before aggregating.
- Reduces the whole (ungrouped) dataset to one value using its **measure** (see
  architecture.md "Measures"): an aggregate (`count`/`sum`/`dcount`/`min`/`max`/
  `avg` over a row-level formula) or a derived ratio over other measures. With no
  `measure` the chart shows a plain row count.
- Formats the value using the **measure's own `format`** token
  (`<prefix><0,.pattern>[a]<suffix>`, e.g. `0.00$` → `333.00$`, or `0.0a $` →
  `23.4k $` where `a` abbreviates large numbers). With no token, the default is
  thousands-separated with trimmed decimals (`1,420`, `1,234.5`).
- An empty / undefined result (e.g. a divide-by-zero ratio) renders blank.
- Renders the value large and centered, in the chart's primary text color
  (`--ff-ink` — near-black in light mode, near-white in dark), with no caption
  beneath it. The value carries a `title` attribute with the full-precision
  figure, so hovering a shaped value can still reveal its exact one. No other
  interactivity — a scalar has nothing to click, so the chart is not a
  crossfilter source.

## Theming
Card and text colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.

## Parameters
- `dataset: str` — dataset name (or Parquet path standalone).
- `title: str` — chart title (its own key — **not** inherited from the measure's `name`).
- `measure` — a measure **key** resolved against the dashboard's `measures:`
  block, or — for standalone use — an inline measure definition dict. `None`
  (the default) means a plain row count. The measure supplies the value **and**
  its formatting.
- `filters: list = []` — declarative pre-filter applied before aggregating,
  intersected with the measure's own filters.

## Editor params
Edit-modal schema (`Number.PARAMS`): dataset (dropdown), title (text), measure
(measure dropdown), filters (filter builder). Widgets live in `fireflyer/params.py`.
