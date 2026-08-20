# Number chart

## Purpose
Display a single aggregated scalar — a "big number" KPI — from a calc.

## Behavior
- Reads the dataset.
- Applies the chart's `filters` (see architecture.md "Filters") before aggregating.
- Reduces the whole (ungrouped) dataset to one value using its **calc** (see
  architecture.md "Calcs"): an aggregate (`count`/`sum`/`dcount`/`min`/`max`/
  `avg` over a row-level formula) or a derived ratio over other calcs. With no
  `calc` the chart shows a plain row count.
- Formats the value using the **calc's own `format`** token
  (`<prefix><0,.pattern>[a]<suffix>`, e.g. `0.00$` → `333.00$`, or `0.0a $` →
  `23.4k $` where `a` abbreviates large numbers — decimals truncated with
  trailing zeros dropped, so `0.0a` gives 1971 → `1.9k`, 2000 → `2k`). With no
  token, the default is thousands-separated with trimmed decimals (`1,420`).
- An empty / undefined result (e.g. a divide-by-zero ratio) renders blank.
- Renders the value large and centered, in the chart's primary text color
  (`--ff-ink` — near-black in light mode, near-white in dark), with no caption
  beneath it. The value **auto-sizes to the cell** (container-query units, not the
  viewport) and never wraps, so a long figure shrinks to fit a short/narrow KPI
  cell instead of overflowing. The value carries a `title` attribute with the full-precision
  figure, so hovering a shaped value can still reveal its exact one. No other
  interactivity — a scalar has nothing to click, so the chart is not a
  crossfilter source.
- When the calc has a **`description`**, hovering the value shows the same card
  the bar and pie use: a **name / value row** with the full-precision figure
  pushed to the right, then the **description** under it — so a shaped KPI
  explains what it means and its precise number. Having no per-item identity, it
  drops the card's header row and leads with the name/value one. With no
  description, a plain `title` still reveals the same exact value. CSS-only (no JS); via CSS anchor positioning it
  escapes the card/scroll frame and flips to stay on-screen, so it never
  truncates (see SKILL.md "Tooltips").

## Theming
Card and text colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.

## Parameters
- `dataset: str` — dataset name (or Parquet path standalone).
- `title: str` — chart title (its own key — **not** inherited from the calc's `name`).
- `calc` — a calc **key** resolved against the dashboard's `calcs:`
  block, or — for standalone use — an inline calc definition dict. `None`
  (the default) means a plain row count. The calc supplies the value **and**
  its formatting.
- `filters: list = []` — declarative pre-filter applied before aggregating,
  intersected with the calc's own filters.

## Editor params
Edit-modal schema (`Number.PARAMS`): dataset (dropdown), title (text), calc
(calc dropdown), filters (filter builder). Widgets live in `fireflyer/params.py`.
