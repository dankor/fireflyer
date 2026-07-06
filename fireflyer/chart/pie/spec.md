# Pie chart

## Purpose
Display category distribution as a donut.

## Behavior
- Reads the CSV.
- Applies the chart's `filters` (see architecture.md "Filters") before grouping.
- Groups by the configured column and counts rows per category.
- Renders an SVG donut, one slice per category, sorted by count descending.
- Each slice has a hover tooltip showing label, count, and percent.
- Each slice brightens slightly on hover.
- A single category renders as a full ring.
- Categories beyond the palette length recycle colors.
- Only count aggregation is supported.

## Parameters
- `dataset: str` — path to the CSV.
- `title: str` — chart title.
- `column: str` — column to group by.
- `filters: list = []` — declarative pre-filter applied before grouping. Each entry is `{column, op (in|ni), values}`.

## Crossfilter interaction
- Inside a dashboard, slices are clickable. A click emits a crossfilter `{column: self.column, op: in, values: [<clicked value>]}` and re-renders the whole dashboard. Clicking the same slice again clears it. See architecture.md "Filters → Crossfiltering".
- Slices whose value is currently selected stay at full opacity; unselected slices fade.
- Outside a dashboard (e.g. standalone `to_html()` call) slices are not clickable; rendering is identical to a chart without crossfilters.

## Editor params
Edit-modal schema (`Pie.PARAMS`): dataset (dropdown), title (text), column (column dropdown),
filters (filter builder). Widgets live in `fireflyer/params.py`.
