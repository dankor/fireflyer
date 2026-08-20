# Table chart

## Purpose
Display the contents of a CSV as a tabular HTML view.

## Behavior
- Reads the CSV.
- **Two modes, chosen by whether `measures` is set.**
  - **No measures — raw rows.** Row by row, no aggregation, at most the first
    1000 rows. `columns` picks which columns to show and in what order; with no
    `columns`, every column shows, including the dataset's **column calcs** (they're
    materialized onto the scan, so a table on that dataset lists them too).
  - **With measures — grouped.** `columns` become the grouping keys and each
    measure is one output column. A measure is a **calc key** from the
    dashboard's `calcs:` block, computed by the same engine the other charts
    use — so per-calc `filters` and derived ratios behave identically here.
    `measures` with no `columns` gives a single row of grand totals.
- A measure column is headed by the calc's **`name`** and formatted with the
  calc's own **`format`** token, so a figure reads the same as it would in a
  number KPI on that calc. Measure columns are always right-aligned.
- **The 1000-row cap means different things per mode.** Raw: the rows *read*
  (pushed into the scan). Grouped: the **groups returned** — the aggregation
  reads the whole dataset, because totalling the first 1000 rows of a larger
  file would report a number that is simply wrong.
- **A column calc relabels the column it overlays.** A dimension column headed
  by a column calc shows that calc's **`name`** and its `description` in the
  header card — so `status: {name: Order status, formula: status}` retitles the
  column without renaming it; `columns:`, filters and crossfilter tokens all keep
  using the key. A plain dataset column shows its own name (see architecture.md,
  "Calcs").
- **A null grouping key is one group.** Polars' `group_by` already treats every
  null as a single group; the join that brings the measures together is told to
  as well (`nulls_equal`), because a join otherwise follows SQL, where null never
  equals null — the null group failed to match itself and each measure landed on
  its own row, one value per row down a diagonal. The cell renders blank, not
  `None`.
- **Filters** apply before aggregating in grouped mode (a total over a
  filtered-after-the-fact sample is meaningless). In raw mode they keep their
  existing behaviour — applied after the 1000-row read.
- **`sort`** is a list of keys, most significant first, each optionally prefixed
  `-` for descending or `+` for ascending (bare = ascending): `['-revenue',
  '+status']`. It can name a grouping column or a measure. A key the frame
  doesn't have is skipped rather than raised on, matching how a filter treats an
  absent column.
- Right-aligns numeric columns; left-aligns everything else.
- Formats numeric cells with thousands separators (e.g. `1,234,567`).
- **Measure cell tooltip.** Every measure cell has a hover card, the same one the
  other charts show for an item: the row's dimension values as the header
  (`paid · 2026-06-01`), then the calc's name with its **exact** value —
  unrounded and thousands-separated — then the calc's `description` when it has
  one. The cell itself is formatted by the calc's `format` token, so `1.9m $` on
  screen and `1,943,458` in the card is precisely what the exact value is for
  (see SKILL.md "Tooltips"). With no grouping columns the row isn't *about*
  anything, so the card drops the header and leads with the name/value row.
  **Dimension and raw-row cells get no card** — their text is already the full
  value, so one would only repeat what's on screen, and a table can render a
  thousand rows.
- **Header labels wrap to two lines, then ellipse.** A header is uppercased and
  was `nowrap`, so a long calc name reserved a whole line of column width —
  headers, not data, were what made columns wide. The label now wraps within
  `--ff-header-max` (16ch) to at most two lines and ellipses past that; the
  tooltip carries the full name, so nothing is lost. The clamp lives on an inner
  span because changing a header cell's `display` would drop it out of the table
  layout, and `overflow-wrap: anywhere` lets a single long token break rather
  than set the column width.
- **Column header tooltip.** A header whose column resolves to a calc with a
  `description` shows a hover card: the header, then the description. This covers
  a **measure** column (headed by the calc's `name`) and a **column calc** shown
  as a dimension — both are labels, and the description is where you find out
  what the label stands for, the same role it plays in the pie/bar/number cards.
  A plain dataset column has no description and gets no card — **unless its name
  is long enough for the two-line clamp to cut it**, in which case the card
  carries the full name (`HEADER_CLAMP_CHARS`, a heuristic: real glyph widths
  aren't knowable server-side, so it errs toward showing). Described headers
  carry a dotted underline and a `help` cursor, without which the explanation
  would be undiscoverable; a header that is merely clamped gets no underline,
  since the ellipsis already announces itself. CSS-only; the header cell is itself the anchor (a real CSS
  box, no marker element needed), and via anchor positioning the card escapes the
  scrolling table body and the dashboard cell, flipping on either axis to stay
  on-screen (see SKILL.md "Tooltips").
- **Row click crossfilters.** Inside a dashboard, clicking a row emits a filter
  naming **every dimension in that row** — one token with a part per column,
  toggling as a unit (click again to clear the whole combination), the same shape
  the bar uses for its two-dimension segments. Grouped, the dimensions are the
  `columns:` the table groups by; raw, they're every column shown, so a click
  drills to that one record. Measures are values, never filter terms. A row with **any**
  null dimension isn't clickable: the filter model has no "is null" op, and
  dropping just the null part would emit a token selecting a *superset* of the
  row — clicking `(name=null, team=x)` would filter on `team=x` alone and quietly
  pull in every named row on that team. Token
  values come from Polars' string cast — the same cast `filters.predicates`
  compares against, which matters for a temporal column, where Python's `str()`
  would produce text that matches nothing.
  The emitting table is **exempt from its own filter** (it keeps showing every
  row), with the selected row tinted and edge-marked and the rest dimmed — the
  same treatment the pie and bar give a selection. Standalone there's no
  dashboard to emit into, so rows aren't clickable.
- Alternating row backgrounds; hover highlights the row under the cursor.
- Empty cells render blank, not as the string `None`.

## Theming
Card, header, row stripes, hover highlight, borders, search input, and pagination colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart's `.fireflyer-chart-root`, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.

## Parameters
- `dataset: str` — path to the CSV.
- `title: str` — chart title.
- `columns: list = []` — columns to show, in order. With `measures` set these
  are the **grouping keys**. Empty means every column (raw mode) or no grouping
  at all — one totals row (grouped mode). A column calc's key works here, since
  it acts as a dimension.
- `measures: list = []` — calc **keys** from the dashboard's `calcs:` block,
  each rendered as one aggregated column. Empty means raw rows, no aggregation.
  A key must name a value (an aggregate or derived calc); a column calc is a
  dimension and is rejected at parse time, as it is for a chart's `calc:`.
- `sort: list = []` — ordering keys, most significant first, each optionally
  prefixed `-` (descending) or `+` (ascending; also the bare default), e.g.
  `['-revenue', '+status']`. Names a grouping column or a measure.
- `search: bool = True` — render a search input above the table that filters rows by case-insensitive substring match across all columns.
- `pagination: int = 5` — rows per page. `0` disables pagination (show everything in the 1000-row cap).
- `filters: list = []` — declarative pre-filter applied after the 1000-row read and before search/pagination. Each entry is `{column, op (in|ni), values}`. Filters whose `column` is absent from the CSV are silently skipped. See architecture.md "Filters".

## Search + pagination
- **Standalone**, controls hit `/chart/table` with `?q=...&page=N` and swap the
  chart fragment in place. **Inside a dashboard** they post to `/dashboard/cell`
  instead, re-rendering that one cell (the same endpoint the bar's grain buttons
  use). The reason is that only the dashboard holds the `calcs:` block: a chart
  rebuilt from URL params alone loses its `measures` — and its column calcs, which
  is a bug the old wiring already had. No other JavaScript.
- The chart's outer container has a stable id; htmx swaps the whole container so search and pagination state both re-render together.
- Search input fires on keyup (debounced) and resets to page 1.
- Search is applied before pagination — page count reflects filtered row count.
- Pagination footer shows prev / numbered pages / next. Hidden when only one page after filtering.
- When `to_html()` is invoked outside the Fireflyer web app, the controls render but htmx is absent, so they are inert. Acceptable for MVP.

## Editor params
Edit-modal schema (`Table.PARAMS`): dataset (dropdown), title (text), columns
(comma-separated list), measures (comma-separated list), sort (comma-separated
list), search (checkbox), pagination (number), filters (filter builder). The three
lists use `ListParam` — a text field rather than a multi-select, because all three
are order-sensitive and `sort` entries carry a `+`/`-` prefix. Widgets live in
`fireflyer/params.py`.
