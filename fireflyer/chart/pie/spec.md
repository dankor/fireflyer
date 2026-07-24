# Pie chart

## Purpose
Display category distribution as a donut.

## Behavior
- Reads the CSV.
- Applies the chart's `filters` (see architecture.md "Filters") before grouping.
- Groups by `column` and sizes each slice by `agg`:
  - `count` (default) — number of rows per category.
  - `sum` — sum of `value` per category.
  - `dcount` — distinct (non-null) values of `value` per category.
- Only **additive, non-negative** aggregations are offered: a donut's angles are
  proportions of a total, so `max`/`min`/`avg` (which the number chart supports)
  would make the slices and percentages meaningless, and are deliberately absent.
- Renders an SVG donut, one slice per category, sorted by the aggregated value
  descending.
- Each slice has a hover tooltip showing label, value, and percent.
- Each slice brightens slightly on hover.
- A single category renders as a full ring.
- Categories beyond the palette length recycle colors.
- When `total` is on (the default), the **grand total of the shown slices** is
  displayed in the donut centre — a short/compact figure (`1.4k`, `3m`); the
  exact number is the hover title. It reflects the current filters/crossfilter.
  `total: false` hides it.

## Theming
- Card, text, legend, and tooltip colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.
- Slice **fills** are the fixed categorical palette — theme-independent, so a value keeps its color in either mode. Slice separators and the donut hole take the card color (`--ff-panel`) so slices stay distinct on any background.

## Parameters
- `dataset: str` — path to the CSV.
- `title: str` — chart title.
- `column: str` — the category column to group by.
- `value: str = ""` — the column to aggregate for slice size. Required when `agg`
  is `sum`/`dcount`; ignored for `count`.
- `agg: str = "count"` — `count` | `sum` | `dcount` (see Behavior).
- `total: bool = True` — show the grand total in the donut centre.
- `filters: list = []` — declarative pre-filter applied before grouping. Each entry is `{column, op (in|ni), values}`.

## Crossfilter interaction
- Inside a dashboard, slices are clickable. A click emits a crossfilter `{column: self.column, op: in, values: [<clicked value>]}` and re-renders the whole dashboard. Clicking the same slice again clears it. See architecture.md "Filters → Crossfiltering".
- Slices whose value is currently selected stay at full opacity; unselected slices fade.
- Outside a dashboard (e.g. standalone `to_html()` call) slices are not clickable; rendering is identical to a chart without crossfilters.

## Editor params
Edit-modal schema (`Pie.PARAMS`): dataset (dropdown), title (text), column (column
dropdown), value (column dropdown), agg (choice: count/sum/dcount), total
(checkbox), filters (filter builder). Widgets live in `fireflyer/params.py`.
