# Pie chart

## Purpose
Display category distribution as a donut.

## Behavior
- Reads the dataset.
- Applies the chart's `filters` (see architecture.md "Filters") before grouping.
- Groups by `column` and sizes each slice by its **measure** (see architecture.md
  "Measures"). With no `measure` the slices count rows. Slice values are formatted
  by the measure's own `format` token.
- Slice proportions/percentages are of the shown slices' total. The measure can
  be any measure — a ratio slice is sized by its per-category value, though only
  an **additive** measure (count/sum) makes the proportions meaningful. Use a
  single additive measure to get a true "share of total" breakdown.
- Renders an SVG donut, one slice per category, sorted by the measure value
  descending; empty/undefined groups are dropped.
- Each slice has a hover tooltip showing label, value, and percent.
- Each slice brightens slightly on hover.
- A single category renders as a full ring.
- Categories beyond the palette length recycle colors.
- When `total` is on (the default), the donut centre shows the **measure
  re-aggregated over the whole (ungrouped) dataset** — *not* the sum of the
  shown slices. So a `dcount` total is distinct-over-all (not the sum of
  per-slice dcounts), and a derived/ratio measure is recomputed at the grand
  level. It's a short/compact figure (`1.4k`, `3m`) with the measure-formatted
  exact number as the hover title, and reflects the current filters/crossfilter.
  `total: false` hides it.

## Theming
- Card, text, legend, and tooltip colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.
- Slice **fills** are the fixed categorical palette — theme-independent, so a value keeps its color in either mode. Slice separators and the donut hole take the card color (`--ff-panel`) so slices stay distinct on any background.

## Parameters
- `dataset: str` — dataset name (or Parquet path standalone).
- `title: str` — chart title.
- `column: str` — the category column to group by.
- `measure` — a measure **key** resolved against the dashboard's `measures:`
  block, or — for standalone use — an inline measure definition dict. `None`
  (the default) means a per-category row count.
- `total: bool = True` — show the grand total in the donut centre.
- `filters: list = []` — declarative pre-filter applied before grouping. Each entry is `{column, op (in|ni), values}`.

## Crossfilter interaction
- Inside a dashboard, slices are clickable. A click emits a crossfilter `{column: self.column, op: in, values: [<clicked value>]}` and re-renders the whole dashboard. Clicking the same slice again clears it. See architecture.md "Filters → Crossfiltering".
- Slices whose value is currently selected stay at full opacity; unselected slices fade.
- Outside a dashboard (e.g. standalone `to_html()` call) slices are not clickable; rendering is identical to a chart without crossfilters.

## Editor params
Edit-modal schema (`Pie.PARAMS`): dataset (dropdown), title (text), column (column
dropdown), measure (measure dropdown), total (checkbox), filters (filter builder).
Widgets live in `fireflyer/params.py`.
