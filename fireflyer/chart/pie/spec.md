# Pie chart

## Purpose
Display category distribution as a donut.

## Behavior
- Reads the dataset.
- Applies the chart's `filters` (see architecture.md "Filters") before grouping.
- Groups by `column` and sizes each slice by its **calc** (see architecture.md
  "Calcs"). With no `calc` the slices count rows. Slice values are formatted
  by the calc's own `format` token.
- Slice proportions/percentages are of the shown slices' total. The calc can
  be any calc — a ratio slice is sized by its per-category value, though only
  an **additive** calc (count/sum) makes the proportions meaningful. Use a
  single additive calc to get a true "share of total" breakdown.
- Renders an SVG donut, one slice per category, sorted by the calc value
  descending; empty/undefined groups are dropped.
- A slice shows its **percent on the slice** (white, centred in the ring band)
  **only when the label actually fits** its wedge — checked against both the ring
  thickness and the slice's arc, so it never spills the ring or a neighbour.
  Slices too small/narrow get no label. Percents are shown to two decimals.
- Each slice has a hover tooltip: the category as a header, then a
  `calc · value · percent` row, and the calc's `description` (when set). The
  value is the **exact** figure — unrounded and thousands-separated — not the
  chart's formatted one, matching the bar and number KPI: a format token
  abbreviates because the chart is short of space, and a tooltip is where you go
  to find out what it stood for.
  The calc name is omitted for a plain/standalone count. The card sits **beside
  the donut on its slice's side, vertically level with that slice**, 8px clear of
  the donut's bounding box — not hugging the arc, which puts a card over the ring
  for slices near the top or bottom (the circle bulges back out past a card
  offset sideways from the arc point). Via CSS anchor positioning it escapes the
  chart/scroll frame and flips to the other side near a screen edge, so it never
  truncates (see SKILL.md "Tooltips"). Both placement paths — anchored and the
  plain-absolute fallback — read one gap value, so they can't drift apart.
- On hover a slice **pops out** — it scales up radially from the donut centre and
  brightens — and its percent label gets a highlight box.
- A single category renders as a full ring.
- Categories beyond the palette length recycle colors.
- A legend entry is a **colour swatch and a label** — no numbers. The values
  are already on the slices (percent) and in the hover tooltips, and dropping
  them keeps the row short enough to fit more entries per page.
- **The legend is one row above the donut and pages** — `◀ n/m ▶` — rather than
  a column beside it. The donut is a fixed 220px square, so width spent on a
  legend column is width the chart can't use, while a row costs one line of
  height. The **donut always shows every slice**, so paging only ever hides
  legend rows, never data.
  `LEGEND_PAGE_SIZE` is fixed, which is the honest limit of doing this on the
  server: how many entries *actually* fit depends on label lengths and the
  cell's pixel width, neither knowable at render time (Superset measures them in
  the browser).
  The pager posts to `/dashboard/cell` like the bar's grain controls, so only
  that chart re-renders. The page is **ephemeral** — it rides the request rather
  than the page-level hidden inputs, so a full re-render (a crossfilter click,
  say) returns to the first page. Which slice of the legend you're looking at
  isn't worth threading through every later request.
  Standalone there's no endpoint to post to, so the whole legend renders and
  there's no pager — the same rule the bar's controls follow.
- When `total` is on (the default), the donut centre shows the **calc
  re-aggregated over the whole (ungrouped) dataset** — *not* the sum of the
  shown slices. So a `dcount` total is distinct-over-all (not the sum of
  per-slice dcounts), and a derived/ratio calc is recomputed at the grand
  level. It's a short/compact figure (`1.4k`, `3m`) with the calc-formatted
  exact number as the hover title, and reflects the current filters/crossfilter.
  `total: false` hides it. When the calc has a `description`, hovering the
  centre shows a rich tooltip — the calc name, its description, and the exact
  total — anchored to the centre and, via CSS anchor positioning, kept outside
  the ring and on-screen (never overlapping or truncating; a transparent hit
  circle in the hole is the hover target). Otherwise a plain native `Total:`
  title is used.

## Theming
- Card, text, legend, and tooltip colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.
- Slice **fills** are the fixed categorical palette — theme-independent, so a value keeps its color in either mode. Slice separators and the donut hole take the card color (`--ff-panel`) so slices stay distinct on any background.
- Tooltips are **opaque** and sized to their text (shared across the charts): a
  translucent card picked up whatever slice sat behind it, so the same card read
  as a different shade depending on where it opened.

## Parameters
- `dataset: str` — dataset name (or Parquet path standalone).
- `title: str` — chart title.
- `column: str` — the category column to group by. May be a **column calc** key
  as well as a real dataset column.
- `calc` — a calc **key** resolved against the dashboard's `calcs:`
  block, or — for standalone use — an inline calc definition dict. `None`
  (the default) means a per-category row count.
- `total: bool = True` — show the grand total in the donut centre.
- `filters: list = []` — declarative pre-filter applied before grouping. Each entry is `{column, op (in|ni), values}`.

## Crossfilter interaction
- Inside a dashboard, both slices **and legend rows** are clickable. A click emits a crossfilter `{column: self.column, op: in, values: [<clicked value>]}` and re-renders the whole dashboard. Clicking the same slice/row again clears it. See architecture.md "Filters → Crossfiltering".
- Slices whose value is currently selected stay at full opacity; unselected slices and legend rows fade.
- Outside a dashboard (e.g. standalone `to_html()` call) slices are not clickable; rendering is identical to a chart without crossfilters.

## Editor params
Edit-modal schema (`Pie.PARAMS`): dataset (dropdown), title (text), column (column
dropdown), calc (calc dropdown), total (checkbox), filters (filter builder).
Widgets live in `fireflyer/params.py`.
