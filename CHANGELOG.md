# Changelog

All notable changes to Fireflyer are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.0] - 2026-08-21

### Added

- **Inline datasets — CSV carried in the dashboard YAML itself.** An optional
  top-level `datasets:` block maps a name to CSV text (first line the header),
  and a chart references it exactly as it would an uploaded one:

  ```yaml
  datasets:
    order_data: |
      id,status,amount
      1,paid,42
  ```

  The point is prototyping without leaving the editor — invent a table, chart
  it, iterate — and a dashboard that carries its own data is a single file you
  can paste to someone. The assistant's prompt now tells it to write such a
  block when asked to mock up or demo something, so "make me some sample sales
  data and chart it" is one turn.

  Each block is converted to Parquet once and cached **by content checksum** in
  the temp dir: an unchanged block costs a `stat`, an edited one lands on a
  fresh path, and two dashboards with the same sample data share a file. The
  write is a rename, so concurrent renders can't read a half-written file. A
  name defined inline **shadows** a managed one — the block is part of the file
  being rendered, so reading someone else's stored data would be a surprise —
  and anything not defined inline falls through to the store as before.
  `Dashboard.dataset_names()` excludes inline names, so a chart reading inline
  data doesn't hold a stored dataset against deletion in the portal's guard.
  Managed datasets are unchanged and remain the home for real or large data.

  The block holds **CSV and nothing else** — a path or a header-only line is
  rejected with a message saying so, because either would otherwise parse as a
  valid one-column, zero-row table and fail much later complaining about a
  column the chart names.

### Changed

- **The starter dashboard carries its own data**, so a fresh checkout renders
  with nothing uploaded and nothing seeded. `files/orders.parquet` is gone (a
  derived binary in git), and the `orders` dataset is no longer seeded at
  startup in either local or paths mode. **`files/` is gone entirely** — the
  sample data now lives in the dashboard that uses it, so a CSV on disk, a
  generator to produce it, the Dockerfile `COPY` and the compose bind-mounts
  were all machinery around a file nothing read any more.
- **The route tests carry their own data too.** They had leaned on that seeded
  dataset — which meant they were passing off a leftover file on the
  developer's disk and would have failed on a clean checkout. The suite now
  runs with no dataset store present at all.


## [0.10.0] - 2026-08-21

### Changed

- **BREAKING — `measures:` is now `calcs:` and `dashboard:` is now `layout:`.**
  The two top-level YAML keys are renamed, and a chart's `measure:` key is now
  `calc:`. The old names are **not** accepted — an existing dashboard must be
  updated (rename the two block keys and every chart's `measure:`). The rename
  goes all the way through the code too: `fireflyer/measures.py` →
  `calcs.py`, `measures_edit.py` → `calcs_edit.py`, `MeasureSet` → `CalcSet`,
  `MeasureError` → `CalcError`, `MeasureParam` → `CalcParam`. The editor's
  measures manager is now the **calcs** manager (`/calcs/*` routes,
  `.ff-calcs-*` classes). Bundled sample dashboards and the seeded `demo` path
  were migrated.
- **The bar legend pages instead of scrolling** — `◀ n/m ▶`, the same control
  the pie legend uses. Neither legend scrolls now; a scrollbar under a legend
  read as chart furniture rather than a control.
- **Fixed: a segment click lit every bucket of that series.** The chart marked
  a segment active by comparing the series *value*, so a two-dimensional
  selection highlighted the whole row of it. Segments now match on the whole
  token, so exactly the clicked (x, y) cell lights; the legend still marks the
  series, which is what a legend row means.
- **Sideways bars align left in their cell** rather than being centred by
  letterboxing (upright bars stay centred — pinning those left only moves the
  gap to the right), and the sideways layout's label gutter is now measured from the
  labels instead of fixed — an ISO date reserved room for a name twice its
  length, leaving a block of empty chart before the bars started. Together those
  cut the dead space on the left roughly in half for date labels.
- **Horizontal bars align left.** A handful of bars now start at the axis at a
  fixed slot pitch rather than being spread across the whole plot, with leftover
  width on the right and the baseline stopping where the bars do — matching the
  sideways layout, which already packs from the top. Two bars used to sit
  marooned at the third-points of an otherwise empty plot.
- **Fixed: bar tooltips could appear beside the wrong bar.** They anchored to
  their SVG `<rect>`, which isn't a reliable CSS anchor — and when an anchor
  doesn't resolve, a `position: fixed` card with `inset: auto` falls back to its
  *static* position, landing wherever it sat in the tooltip list. Each card now
  carries its own placement as a share of the viewBox and positions itself
  absolutely, clamped so it can't leave the canvas — and anchors to a zero-size
  marker element carried inside the SVG rather than to the shape, so it still
  escapes the chart card instead of truncating — and being in the drawing's own
  coordinate space, letterboxing can't shift it off its segment. The card is
  opaque and sized to its text, rather than translucent with a minimum width. (The pie was never affected:
  its canvas is a fixed 220×220 with a 1:1 viewBox, so its inline px placement is
  correct with or without the anchor.)
- **The number KPI's tooltip uses the shared card layout.** Its exact value now
  sits at the right of the name row, as in the bar and pie, instead of on a line
  of its own under a rule — which left the row's right-hand side empty and read
  as a different kind of card.
- **Fixed: tooltips could hang off the side of the screen.** Every tooltip is
  centred on the item it describes, so one in the first or last column
  overflowed sideways — and the only `@position-try` fallback flipped it
  *vertically*, which never notices horizontal overflow. All three charts now
  carry fallbacks on both axes (near-edge alignment, tried before the vertical
  flip, plus the combinations). The `!important` on the pie's and bar's anchor
  rules is gone too: it can stop a `@position-try` block applying, and source
  order in the `@supports` block already wins.
- **Fixed: the pie's tooltips didn't show the exact value.** The slice card
  showed the *formatted* figure, so an abbreviated chart had no way to reveal
  what it stood for, and the centre total's plain-title fallback disagreed with
  its own rich card. Every chart tooltip now shows the same thing — the
  unrounded, thousands-separated value — and a test pins the four of them
  together.
- **Exact values are thousands-separated.** The unrounded figure in a tooltip
  (`exact_value`, shared by the bar, pie and number KPI) now groups its digits —
  `1,234,567` rather than `1234567`. It's shown precisely because someone is
  reading the digits. Precision is unchanged: whole numbers stay integers and
  floats keep every digit (`1,234.5678`). The helper was named `raw_value` while
  its contract was "no formatting at all"; grouping made that name wrong.
- **The bar's segment tooltip is now a proper card**: the bucket as a header,
  the series and its unrounded value, and the calc's description. It's anchored
  to its own segment — hanging off the top edge, where the eye already is —
  rather than centred over the canvas, and escapes the chart card via CSS anchor
  positioning instead of being clipped.
- **New bar chart `top`** — keep only the N biggest bars (0 = all). Selection
  is by calc value, not position, so a date axis keeps its N busiest buckets and
  still draws them oldest-first. Dropped rows leave the frame, so the value
  scale and the legend describe what's drawn.
- **New bar chart `direction`** — `horizontal` (the default, unchanged:
  categories left-to-right, bars growing up) or `vertical` (categories
  top-to-bottom, bars growing rightward from a left-hand axis, labels flat in a
  gutter rather than tilted). Sideways bands are a fixed height, so the canvas
  grows downward and scrolls instead of squeezing.
- **A bar chart's `y` is now optional.** Omit it for plain one-bar-per-category
  bars: a single series, no stacking, no legend, and a click filters the one
  dimension there is. The stacked path is unchanged. The editor's column
  dropdown gained a blank choice, without which a `y` could be set but never
  cleared.
- **Fixed: an emitting chart's badge counted differently.** A red badge showed
  only what that chart emits and a blue one only what narrows it, so the same
  dashboard state read as different numbers across the row. A chart is exempt
  from its own crossfilter, so its applied list is short by exactly what it
  emits — the badge now shows the sum (the two lists are disjoint), and an
  emitter that's also downstream lists both groups in its tooltip.
- **Fixed: another chart's badge drew over an open filter tooltip.** The tooltip
  sits inside its badge's stacking context, so its own `z-index` could only
  order it against that badge's siblings; against a *different* cell's badge at
  the same level, later-in-DOM won. The badge is now lifted while its tooltip is
  open, which takes the tooltip with it.
- **A `between` filter reads as `low–high` in the indicator**, with a midnight
  time trimmed off each bound — a bucket edge is midnight by construction, so
  `2026-02-01 00:00:00+00:00` said nothing the date didn't and two of them
  overflowed the tooltip. Display only; the stored bounds still round-trip
  exactly in the crossfilter token, and a non-UTC midnight is left alone since
  it isn't the same instant as the bare date.
- **Fixed: a `between` filter rendered as "not in" and truncated.** The filter
  indicator assumed every op was `in` or its negation, so a range was labelled
  as its own opposite; and a single nowrap line cut off the end of the range —
  the half that says where it stops. It now reads `from <low> to <high>` and
  wraps.
- **Fixed: a filter on a column calc was reported as not applied.** The
  indicator resolved column names against the bare Parquet schema, which has no
  column calcs, so a `date()`-derived filter showed as inactive even though the
  chart really was filtered by it — the indicator contradicted the data.
- **A bar segment click filters both dimensions.** A segment is an (x, y) cell,
  so clicking one now crossfilters the bucket *and* the series. Both ride in a
  single token and toggle as a unit — clicking again clears both halves, never
  one. Legend rows still filter the series alone, since that's what a row means.
- **New `between` filter op** (owner-approved addition to the in/not-in model).
  Half-open, `low <= v < high`, exactly two values. It exists because a bucketed
  axis is a *range*: a bar labelled `2026-06` covers all of June, which no list
  of exact values can express — an exact filter would have matched only the 1st.
  Half-open is what makes adjacent buckets tile without double-counting. It's
  available in YAML `filters:` and the editor's filter builder too. Bounds
  compare numerically when both parse as numbers, textually otherwise (ISO dates
  sort chronologically as text; `"10" < "9"` does not).
- **The bar legend crossfilters.** Clicking a legend row toggles that series
  exactly as clicking one of its bar segments does — matching the pie, whose
  legend has worked this way since 0.9.1.
- **Legends show a colour and a label only.** The per-entry values are gone from
  both the pie and bar legends — they're already on the slices/bars and in the
  hover tooltips, and the shorter rows fit more entries per legend page.
- **The pie legend moved above the donut and pages** — `◀ n/m ▶`, like Superset
  — instead of being a column beside it that made the whole chart scroll. The
  donut still shows every slice, so paging hides legend rows and never data. The
  pager posts to `/dashboard/cell`, so only that chart re-renders; the page is
  ephemeral and resets on a full re-render. Page size is fixed, since how many
  entries actually fit depends on label lengths and pixel width, neither
  knowable server-side. The bar legend is capped and scrolls, so many series
  can't eat the plot's height.
- **The bar legend places itself.** It now sits in a horizontal row above the
  plot by default, moving to a right-hand column only in an extremely wide,
  short cell. The drawing is `min(width, aspect × height)` across, so a
  right-hand legend costs ~150px of width against ~26px of height for one above
  — and width is what a bar chart is short of, so above wins in every cell that
  isn't past roughly 3:1 (a typical dashboard cell is ~2:1). Decided by a
  container query, since the cell's size isn't knowable server-side. No setting.
- **Grain and window changes re-render only their own chart.** They change
  nothing for the other charts on the dashboard, so they post to
  `/dashboard/cell` and swap that cell alone instead of replacing the whole
  dashboard and re-fetching every cell. The page-level grain state rides back as
  an htmx out-of-band swap, since a cell response doesn't otherwise touch it.
  Crossfilter still goes wide — it really does change every chart's data.
- **A long bar axis is windowed, with a draggable scale to move it.** Past 30
  buckets the chart renders only that many — the most recent by default — and a
  scale underneath moves the window: the whole range as 40 hoverable segments
  with the current window lit, plus ‹ / › to step one window. Each segment names
  the dates it would show in a tooltip, so moving across the scale reads the
  timeline out as you go, and clicking jumps there. **No JavaScript** — a
  drag-slider would need it to label the live position, which chart output
  forbids. The SVG, the DOM and the label count stay bounded however long the
  range is. Windowing needs an
  endpoint to post to, so it applies inside a dashboard only; standalone the
  whole axis is drawn and scrolls as before.
- **Fixed: a bar chart resized when you switched grain.** The viewBox grew one
  slot per bar, so every grain drew at a different aspect — a 60-bar window
  became a 9:1 letterbox unreadable at any size, while a two-bar year view was
  tall enough to overflow its cell and push out a vertical scrollbar. Any view
  shown whole now uses one fixed plot width, so the viewBox is identical at two
  bars and at thirty, and the SVG is absolutely positioned to fill its cell
  (which is what makes `height: 100%` resolve instead of falling back to the
  intrinsic aspect). Bar width is capped too, so a two-bar view spreads out
  rather than rendering two billboards.
- **Timeline lens + ruler on a long bar axis.** The scroll canvas's horizontal
  scrollbar is styled into a grabbable lens — drag to scroll, click the track to
  jump — because the platform default is invisible until you already scroll,
  which made a long axis look broken rather than scrollable. Beneath it, an axis
  longer than the automatic grain's target gets a ruler of five evenly spaced
  marks naming the whole range. *(The ruler and the position readout were
  dropped again in favour of the scale's own per-segment tooltips.)*
- **Fixed: a bar chart with many categories rendered nothing.** The plot width
  was capped while the gap between bars was not, so past ~200 bars the gaps
  alone exceeded the whole plot and every bar came out *negative* wide. Each bar
  now owns a slot and the gap is a share of it, so widths stay positive at any
  scale; the ceiling rose to about 400 bars at full width, so a manually picked
  fine grain scrolls properly instead of being squeezed into the cell.
- **Bar chart data prep moved into Polars.** Labelling, zero-dropping, ordering
  and totals are vectorized frame operations rather than a Python loop over
  every (x, y) pair — about 5× faster on a 2000-bucket axis. Only the final SVG
  geometry still walks rows.
- **Zero values are no longer drawn** on a bar chart. A zero gets no segment,
  no legend row and no slot on the axis, so an all-zero group stops eating axis
  width for an invisible bar labelled `0`.
- **A bar chart with a temporal `x` is ordered chronologically** instead of by
  stack total — a date axis reads as a timeline, not a ranking. Non-temporal
  axes are unchanged (still largest bar first).
- **A temporal bar axis picks its own time grain.** No setting and no YAML key:
  the chart counts how many buckets each candidate grain (day → week → month →
  quarter → year) would produce and takes the finest that fits the readable bar
  cap, so a year of daily rows draws ~12 month bars instead of 365 hairlines. It
  counts real buckets rather than estimating from the calendar span, so three
  rows spread over two years stay on `day`. Day is the floor — sub-daily
  timestamps land in their day's bucket — and year the ceiling, so a long enough
  range overflows and scrolls as before. Bucketing happens **before** the calc
  aggregates, so a month's bar is the calc over that month's rows rather than a
  roll-up of daily results (which would break `avg`/`dcount`). Labels follow the
  grain: `2026-06`, `2026 Q2`, `2026`.
- **Section headers sit just off the edge** (`--ff-header-indent`, 4px) instead
  of flush against it. A test keeps the value between the two things that looked
  wrong — zero, which read as touching, and the card's own text inset, which read
  as detached from the cards below.
- **Chart tooltips are opaque and sized to their text** across pie, bar and
  number — translucency let the chart show through, so one card read as several
  different shades, and a `min-width` padded a two-word row into a panel. The
  dashboard's filter-indicator tooltip is separate chrome and is unchanged.

### Added

- **In-chart grain picker.** Inside a dashboard a temporal bar axis carries a
  segmented control **directly beneath the plot**, on the axis it governs — `A`
  (auto) then `Y Q M W D`, coarsest to finest — that re-renders the chart at the
  chosen grain. It sits outside the scroll container, so it stays put when a
  long axis scrolls. The choice is per **viewer**, not per dashboard: it rides the page's hidden inputs beside the
  crossfilter tokens, so it survives crossfilter clicks and tab switches but is
  never written to the YAML, and `A` restores the automatic pick. Standalone
  charts have no endpoint to post to, so they get no picker — the same rule
  crossfilter follows.
- **Calculated columns.** A third kind of calc: a row-level formula that
  produces a **dimension**, not a value. It's materialized onto the scan, so its
  key works anywhere a column name does — a chart's `x`, `y` or `column`, a
  filter, or another calc's formula — and it shows up in the editor's column
  dropdowns (values still come from the calc dropdown). Column calcs apply in
  declaration order, so a later one can build on an earlier one. Pointing a
  chart's `calc:` at one is rejected at parse time.

  **Nothing in the YAML names the kind.** `agg` still means aggregate, and a
  bare `formula` is sorted into derived vs column by what it references:
  sibling calcs make it a derived value, dataset columns (or a `str2dt()` call, or
  another column calc) make it a calculated column. One consequence: a typo'd
  calc reference is now indistinguishable from a column name, so it surfaces at
  read time instead of parse time.
- **`str2dt(<column>, <pattern>)` in calc formulas** — the expression grammar's
  one function. It turns a text (or integer) column into a real Date/Datetime,
  which is what lets a `YYYYMMDD` string column go on an axis:

  ```yaml
  calcs:
    orders:
      order_day: {formula: 'str2dt(order_date, YYYYMMDD)'}
  charts:
    by_day: {type: bar, dataset: orders, x: order_day, y: status, calc: revenue}
  ```

  Patterns are written the way people write dates — `YYYY`, `YY`, `MM` (month),
  `DD`, `HH`, `mm` (minute), `ss`, `Z` (UTC offset) — with everything else a
  literal separator (`YYYY-MM-DD HH:mm:ss`, `DD/MM/YYYY`,
  `YYYY-MM-DDTHH:mm:ssZ`). A pattern with a time part — `Z` counts — yields a
  Datetime, otherwise a Date. Rows that don't match the pattern become null
  rather than failing the render.

  `Z` handles offset timestamps like `2024-06-26 17:14:03+03:00`, with or
  without the colon (`+0300`), and a bare `Z` (Zulu); the result is
  **normalized to UTC**, so that value buckets and labels as `14:14:03`. `ss`
  accepts optional fractional seconds, so `17:14:03` and `17:14:03.123456` need
  the same pattern — the alternative silently nulls every row over an export
  detail nobody knows they have.
- **Unparsed dates get a named bucket.** Rows whose timestamp doesn't match the
  pattern collect into one `(no date)` bar at the end of a temporal axis instead
  of a blank one, so a partial-parse problem is visible rather than looking like
  a rendering glitch.
- **Table: `columns`, `measures` and `sort`.** The table can now aggregate.
  `measures` is a list of calc keys, each rendered as one column; when it's set,
  `columns` become the **grouping keys** (and with no `columns` you get a single
  grand-total row). With no `measures` nothing changes — raw rows, with `columns`
  picking which to show. `sort` is a list of keys, most significant first, each
  optionally prefixed `-` for descending or `+`/bare for ascending
  (`sort: ['-revenue', '+status']`); it can name a grouping column or a measure.
  A measure column is headed by the calc's `name` and formatted with its
  `format` token, and aggregation goes through the shared calc engine, so
  per-calc filters and derived ratios behave exactly as they do on the other
  charts. An unknown measure key fails at parse time like a bad `calc:` does; a
  column calc is rejected as a measure, being a dimension.
  The **1000-row cap now means different things per mode**: raw, it caps the rows
  *read*; grouped, it caps the **groups returned**, because totalling the first
  1000 rows of a larger file would report a wrong number.
- **The bundled `orders` sample is regenerated** (`files/make_orders.py`, fixed
  seed, 120 rows) with the shape the guide needs: 10 weeks of dates in one year
  so the bar's grain picker has D/W/M to switch between, **8 channels** so a
  legend actually pages, a **nullable `segment`** so null grouping is visible in
  the demo rather than a surprise later, and `qty` + `unit_price` beside
  `amount` so a calc can show a row-level formula (`unit_price * qty`) that
  agrees with the stored column. The generator is checked in so the sample is
  reproducible rather than a mystery binary.
- **The starter dashboard is now a commented quick guide.** "Orders overview"
  was a plain example; it's rewritten as a worked tour with ~75 lines of
  comments explaining each key as you scroll — what a calc's three kinds are and
  why you never write the kind down, what `columns` vs `measures` does to a
  table, what `top`/`direction` do to a bar, and how `layout` rows, widths,
  headers, separators and tabs fit together. Three tabs (Overview / Tables /
  More charts) so each feature has somewhere to be shown rather than crammed
  into one screen. Two tests guard it: every chart in it must render, and the
  comments must still be there.
- **A column calc can relabel a column it overlays.** Give one a `name` and
  `description` and every viewer-facing use picks them up — the table's column
  header, its header tooltip, and the filter indicator — while `columns:`,
  filters, crossfilter tokens and other formulas keep using the key. Nothing is
  renamed; the label just overlays it. This needed a rule change: a
  **self-reference in a formula now means the dataset column**, since a calc
  cannot reference itself. Without it `status: {formula: status}` was classified
  as a derived calc referencing a derived calc and rejected outright, so the
  obvious way to write an overlay didn't parse. `CalcSet.column_label` /
  `describe_column` are the shared lookup and pass a plain column through
  unchanged.
- **Table: clicking a row crossfilters the dashboard.** The row emits one token
  naming every dimension in it, toggling as a unit — grouped, the `columns:` it
  groups by; raw, every column shown, so a click drills to that record. Measures
  are never filter terms. The emitting table stays unfiltered with the selected
  row tinted and the rest dimmed, matching the pie and bar. Token values use
  Polars' string cast, the same one `filters.predicates` compares against — with
  Python's `str()` a temporal dimension would build a token that matches no rows.
- **Table: hover tooltips on measure cells.** The same card the other charts
  show for an item — the row's dimension values as the header, then the calc's
  name with its **exact** value (unrounded, thousands-separated), then the
  description. A measure cell is formatted by the calc's `format` token, so
  `1.9m $` on screen against `1,943,458` in the card is exactly the case that
  rule exists for. A grand-total row (no grouping columns) drops the header.
  Dimension and raw-row cells get none: their text is already the full value, and
  a card per cell across a thousand rows is real page weight.
- **Table: narrower columns.** Header labels were `nowrap`, so a long calc name
  reserved a whole line of column width — headers, not data, were what made
  columns wide. A label now wraps to at most two lines within `--ff-header-max`
  (16ch) and ellipses past that, with the tooltip carrying the full name. A
  header long enough to be cut gets that card even with no description, so the
  name is always recoverable.
- **Table: column header tooltips.** A header whose column resolves to a calc
  with a `description` shows a hover card — the header, then the description.
  Covers a **measure** column (headed by the calc's `name`) and a **column calc**
  shown as a dimension; a plain dataset column has none and gets no card.
  Described headers get a dotted underline and a `help` cursor, since a column
  heading doesn't otherwise invite a hover. CSS-only, and the header cell is
  itself the anchor — a real CSS box, so no marker element is needed, unlike the
  SVG charts. Via anchor positioning it escapes the scrolling table body and the
  dashboard cell, flipping on either axis to stay on-screen. A table with no
  described columns emits byte-identical markup to before.
- `ListParam` (`params.py`) — comma-separated list widget backing the three new
  keys in the edit modal. A text field rather than a multi-select: all three are
  order-sensitive and `sort` carries `+`/`-` prefixes.

### Fixed

- **A chart that cannot render now shows an error card instead of spinning
  forever.** Cells arrive by htmx, which only swaps a 2xx response, so any
  exception below `render_cell` left the placeholder spinner turning with the
  reason visible only in the server log. A failed chart becomes a card naming
  the chart and the problem — Polars' resolved query plan is trimmed off, since
  it is pages long and says nothing a reader can act on. The cell wrapper
  survives so the grid doesn't collapse, and the other cells are unaffected.
  The usual trigger is a dashboard that has outrun its data: a calc naming a
  column the dataset no longer has.
- **A grouped table split a null grouping key across one row per measure.** The
  measures are joined on the grouping columns, and a join follows SQL — null
  never equals null — so the null group failed to match itself and its values
  landed on a diagonal, one per row. The join now treats nulls as equal
  (`nulls_equal`), matching what `group_by` already does. Relatedly, a row with
  any null dimension is no longer clickable: dropping the null part from its
  crossfilter token would have selected a superset of the row.
- **A header's or separator's move/edit/delete badge ignored every click.** The
  badge reuses `.fireflyer-chart-tools`, which parks itself `pointer-events:
  none` while hidden (opacity:0 alone does not stop clicks, so an invisible
  toolbar would otherwise eat hovers meant for the resize handle under it). The
  cell's reveal rule hands pointer events back; the header/separator one never
  did, so the badge appeared on hover and then did nothing. A test now asserts
  every reveal rule restores them — it fails against the old CSS.
- **A table inside a dashboard lost its calcs when searched or paged.** Its
  controls called `/chart/table`, which rebuilds the chart from URL params and
  has no access to the dashboard's `calcs:` block — so the rebuilt table dropped
  its column calcs (a pre-existing bug: a column-calc column vanished on page 2)
  and would have dropped `measures` entirely. Embedded tables now post to
  `/dashboard/cell`, the same cell-scoped endpoint the bar's grain buttons use;
  standalone tables still use `/chart/table`. Covered by route-level tests.

- **Pie tooltips opened in the corner of the page.** The per-slice point was
  emitted as an inline `left`/`top`, which outranks any stylesheet rule: the
  anchored block's `inset: auto` lost while its `position: fixed` still applied,
  so canvas coordinates resolved against the **viewport**. The placement now
  travels as `--ff-tip-x`/`--ff-tip-y` custom properties, leaving the override an
  ordinary cascade. The bug was latent until the block's `!important` was removed
  — that removal is what exposed it.
- **Pie tooltips sat on top of the donut, or well away from their slice.** The
  anchored placement dropped the card centred *below* its anchor, discarding the
  outward offset the non-anchored path applies — so the two paths disagreed and
  the card covered the ring for every slice on the left. Both now use one
  geometry: the card hangs beside the donut on its slice's side, level with the
  slice, 8px clear of the donut's **bounding box**. Anchoring to the arc point
  instead (the previous attempt) still overlapped, because a circle bulges back
  out past a card offset sideways from its upper or lower arc — clearing a point
  on a curve doesn't clear the curve. The gap is one custom property both paths
  read, and the four quadrant rules collapse to two sides.
- **Pie tooltips anchored to nothing.** `anchor-name` sat on the slice `<path>`
  and on the centre `<circle>`, and an SVG shape isn't a CSS box, so the anchor
  silently failed. Both now anchor to a 1px marker `<div>` in a `<foreignObject>`
  inside the SVG — the same fix the bar chart got.
- **A stale duplicate in `bar/chart.css` was overriding the rebuilt tooltip.**
  The stylesheet had accumulated a 225-line duplicated region plus a pre-rebuild
  `.fireflyer-bar-tooltip` rule *after* the current one, so the bar tooltip
  rendered with its old design — translucent, `white-space: nowrap`, pinned to
  the canvas top rather than to its segment — while the new rule sat inert
  earlier in the file. Also removed an orphaned copy of the anchored rule sitting
  **outside** its `@supports` guard, which pinned cards at their static position
  wherever anchor positioning isn't supported. Dead `-tooltip-label` /
  `-tooltip-meta` / `.legend .meta` rules dropped with it.
- **Chart CSS comments may no longer write a bare tag their own template emits.**
  A stylesheet is inlined into the chart's output, so a comment mentioning
  `<rect>` or a header tag lands in the HTML and gets matched by any test that
  scrapes markup — it cost real debugging three times. The guard derives each
  chart's emitted tags from its `chart.html`, so `<html>` in a theme comment
  stays fine while a tag the chart actually renders does not.
- **`tests/test_chart_css_is_coherent.py`** guards the class of bug that hid all
  of the above: it fails on a duplicated top-level selector, unbalanced braces, an
  anchored rule outside its `@supports` guard, or a translucent tooltip. It
  caught a second duplicate (`.fireflyer-number-body`) on its first run.

## [0.9.1] - 2026-08-09

### Added

- **Calc `description` in hover tooltips.** When a calc has a
  `description`, both the number KPI's value and the pie's donut-centre total
  show a **styled tooltip card** on hover — calc name, description, and the
  exact figure — so a chart explains what its number means, not just the raw
  value. CSS-only (no JS), themed like the existing tooltips; charts without a
  description are unchanged. The number card's tooltip escapes the cell (dropped
  below the value) so it isn't clipped in short KPI rows.
- **Richer pie slice tooltips.** A slice's hover tooltip now leads with the
  category as a header, then a `calc · value · percent` row and the calc's
  `description` (when set); tooltip value lines show the exact, unformatted
  figure, and is anchored **next to the hovered slice** (on the outer edge at the
  slice's mid-angle, offset outward and hanging into the canvas so it stays
  on-screen). Slices wide enough now show their **percent on the slice** (small
  ones skipped), and percents are shown to two decimals. A label is drawn only
  when it **fits** its slice (checked against the ring thickness and the arc), so
  it never spills the ring or a neighbour.
- **Semi-transparent tooltips.** Chart tooltips (number, pie, bar, and the
  dashboard filter indicator) now use a translucent background with a light
  backdrop blur so they read over content without fully hiding it.
- **Tooltips never truncate.** The number and pie tooltips use CSS anchor
  positioning (`position: fixed` + `@position-try`) as a progressive enhancement,
  so they escape the chart card and the scroll pane and flip to stay on-screen
  instead of being clipped. Documented in `fireflyer/chart/SKILL.md` → "Tooltips".
- **Clickable pie legend.** Inside a dashboard the pie legend rows crossfilter
  like the slices — click a category (or slice) to toggle it.
- **Pie slice pop-out on hover.** A hovered slice scales up radially from the
  donut centre (and its percent label gets a highlight box), so the active slice
  stands out. The centre-total tooltip now hangs **below the donut** instead of
  over it, so it no longer overlaps the ring.

### Changed

- **Bar chart scrolls a long axis.** With few categories bars fill the cell as
  before; with many, the plot grows so each bar keeps a minimum width and the
  canvas scrolls horizontally (title + legend stay put) instead of squeezing
  bars into hairlines. CSS-only (native scroll); an extreme category count is
  capped.
- **Number KPI value auto-sizes to its cell.** The big figure now scales with the
  cell (CSS container-query units) instead of the viewport, and no longer wraps —
  so a long number shrinks to fit a short/narrow KPI cell instead of overflowing
  the card.
- **Abbreviated number format (`…a`) drops trailing zeros.** The pattern's
  decimals are now the *maximum* shown: the abbreviated value is truncated (not
  rounded) to them and trailing zeros dropped, so `0.0a` renders 1971 as `1.9k`
  and 2000 as `2k` (was `2.0k`). Uses `Decimal` to avoid binary-float artifacts
  (2300 → `2.3k`, not `2.2k`).

## [0.9.0] - 2026-08-09

### Changed

- **AI assistant is a left-pane overlay** toggled from the topbar (`#ff-chat-btn`),
  mirroring the docs/measures overlays — it replaces the old bottom collapsible
  panel. The input is taller and vertically resizable.
- **The assistant now gets the datasets' schemas** (each column's name + type,
  **no data**) alongside the YAML, so it builds charts and measures from real
  columns and won't invent datasets/columns. The DSL system prompt is corrected
  to the current model — there is no `datasets:` block; datasets are managed
  separately and referenced by name.

## [0.8.0] - 2026-08-07

### Added

- **Dashboard measures.** Aggregation moves out of charts into a top-level
  `measures:` block, **keyed by dataset**. A measure is an **aggregate** (`agg`
  ∈ `count`/`sum`/`dcount`/`min`/`max`/`avg` over a row-level `formula` such as
  `amount` or `price * qty`, with optional `filters` for conditional
  aggregation) or a **derived** ratio (`formula` over other measure keys, e.g.
  `revenue / orders_count`). Measures carry `name`, `description`, and a
  `<prefix><0,.pattern>[a]<suffix>` `format` token (`0.00$`, `0.0%`; an `a`
  after the pattern abbreviates big numbers → `0.0a $` = `23.4k $`; no
  percentage auto-scaling). Empty/undefined results are dropped. New engine in
  `fireflyer/measures.py` (tiny `+ - * /` expression parser, Polars translator,
  format renderer).
- **Measures manager** in the editor: a topbar overlay (styled like the docs
  reference) lists measures per dataset with add / edit / delete, backed by
  `fireflyer/measures_edit.py` (surgical `measures:` block edits, re-validated
  through `Dashboard.from_yaml`). Charts pick a measure via a new `MeasureParam`
  dropdown in the edit modal.

### Changed

- **Charts reference a measure instead of aggregating themselves.** `number`,
  `pie`, `bar`, and `map` drop `agg`/`value`/`column`(number)/`format`(number)
  in favour of a single `measure:` key (default: row count). `pie`'s centre
  total is now the measure re-aggregated over the whole dataset (so a `dcount`
  total is distinct-over-all, not summed slices); `map` accepts a `count`/`sum`
  measure as the per-hex weight; `number` keeps its own `title` and inherits the
  measure's format.

### Removed

- The per-chart `agg` / `value` / number `format` fields. **Breaking** — update
  existing dashboards to define measures and reference them by key (no
  auto-migration).

## [0.7.0] - 2026-07-24

### Added

- **Pie aggregation + centre total.** A pie can now size its slices by an
  aggregation of a value column, not just row count: `agg: count | sum | dcount`
  with a `value` column (only additive measures — a donut's angles are
  proportions, so no max/min/avg). The donut **centre shows the grand total** of
  the shown slices by default (compact, filter/crossfilter aware); `total: false`
  hides it. Backward compatible — existing pies (no `agg`) still count rows.

## [0.6.0] - 2026-07-24

### Added

- **Chart documentation in the editor.** A **documentation** button (book icon)
  in the editor topbar toggles a **chart reference** overlay over the output
  pane: each chart type (table / pie / bar / map / number) as a collapsible
  section showing its `spec.md`, rendered by a tiny in-house markdown→HTML
  (headings, bullets, `**bold**`, `` `code` ``). Built once at import from the
  chart folders; Esc or ✕ closes it.

## [0.5.0] - 2026-07-14

### Added

- **Managed datasets.** Datasets are first-class named entities, not inline CSV
  paths: upload a CSV in the new **Datasets** tab → **Parquet** in object storage,
  referenced by **name** (`dataset: orders`); metadata (schema, rows, description,
  delimiter, author) is a YAML sidecar. `datasets.py` (`DatasetStore`) over
  `storage.py` (`ObjectStore`: a local folder, or **Garage/S3** via the
  `.[portal]` extra, chosen by `FIREFLYER_S3_ENDPOINT`).
- **Datasets tab** — list + upload / replace / rename / remove, and a detail view
  (per-column type icons + 20-row preview). **Delete-guard** (can't remove a
  dataset a dashboard uses) and **cascade-rename** (rewrites `dataset:` refs).
- **Local paths mode** (`FIREFLYER_PATHS`, `web/paths.py`) — a non-portal,
  no-DB/no-login way to manage many dashboards + datasets on your filesystem. Each
  host folder you Docker-map is a switchable **path** (dashboards as
  `<path>/dashboards/*.yaml`, datasets in an isolated per-path blob store); a
  **`demo` path** is seeded on first run. Data-free dashboard YAML is meant to be
  kept in git and deployed by a coming CLI/API — a **GitOps** workflow; datasets
  stay server-side.

### Changed

- **Dashboard YAML dropped its `datasets:` block.** `dataset:` is now a name
  resolved to Parquet at render (`Dashboard.from_yaml(text, datasets=<store>)`),
  or a path/URI directly when no store is given. Required top-level keys: `name`,
  `charts`, `dashboard`.
- **Charts read Parquet efficiently** — lazy `scan_parquet` with projection +
  predicate pushdown, so only the needed columns/row-groups are read.
- **Gallery / editor navigation.** List pages lead with a **Dashboards | Datasets**
  switch on the left and (local paths mode) a **path dropdown** on the right;
  selected items (a dataset detail, the dashboard editor) lead with a **back
  button** + name and keep the path dropdown, no switch. The Fireflyer brand text
  is gone from the editor.

## [0.4.0] - 2026-07-10

### Added

- **Portal mode** (`FIREFLYER_PORTAL`, `python -m fireflyer.portal`, compose
  `portal` profile) — an opt-in, DB-backed way to store and browse many
  dashboards, reusing the editor unchanged. `/` becomes a gallery table (name,
  author, last updated) with per-row **Edit / Clone / Remove** and **+ New**; each
  opens in the editor with a **Save** button. Dashboards are stored as an opaque
  YAML blob (validated on save, never decomposed), so every stateless editor route
  keeps working. Two backends in `web/portal.py`: stdlib **sqlite** (dev/tests) and
  **Postgres** (the optional `.[portal]` extra). An owner-approved exception to the
  no-persistence anti-goal, scoped to `web/`.
- **Portal login** (`web/auth.py`) — a simple auth gate, default **admin/admin**,
  with a topbar profile dropdown + **Log out**. A deliberately small, swappable
  backbone — an `Authenticator` protocol and an HMAC session cookie are
  independent, so an SSO/OAuth callback just reuses `set_session` (recipe in
  `architecture.md`); none implemented. Local mode has no login.
- **Required top-level `name:` key** — the dashboard's display name, part of the
  definition (same in local and portal); `Dashboard.from_yaml` rejects an empty
  name, and portal re-derives its listing name from the YAML on every save.
- **Editor topbar + refresh-on-edit preview.** The Run button and status text are
  gone; editing the YAML greys the (still-interactive) preview and shows a **↻
  Refresh** overlay. **Save** appears only when there are unsaved changes (⌘/Ctrl+S,
  with a navigate-away guard), and a **3-segment Auto / Light / Dark** icon theme
  switch replaces the old toggle. Fixed two resize snap-back bugs — block-style
  row-height drags, and column drags on tabbed dashboards.

## [0.3.1] - 2026-07-09

### Added

- **CI + release automation** (`.github/workflows/`). On every PR into `main`,
  `ci.yml` runs the pytest suite and checks that `pyproject.toml`'s version was
  bumped versus `main`; both are intended to be required status checks so a PR
  can't merge until tests pass and the version is updated. On merge to `main`,
  `tag-on-merge.yml` reads the version and pushes the matching `vX.Y.Z` tag.
  Snapshot tests were made checkout-path independent (the absolute dataset path
  and the SHA-1 chart ids derived from it are normalized in the `snapshot`
  fixture) so the suite passes on CI, not just the author's machine.

## [0.3.0] - 2026-07-08

### Added

- **Dark theme.** Dashboards and every chart now ship a light *and* a dark
  palette as CSS custom properties. Selection is automatic —
  it follows the viewer's OS preference (`prefers-color-scheme`) — and can be
  forced with a `data-ff-theme="light|dark"` attribute: `Dashboard.to_html(...)`
  and each chart's `to_html(...)` take a `theme="dark"|"light"` argument, and an
  explicit choice overrides the OS preference. The browser editor gains a topbar
  **Theme: Auto / Light / Dark** toggle (persisted in `localStorage`) that themes
  the editor chrome, the dashboard preview, and all charts at once. Tokens are
  mirrored across `dashboard.css` and each chart's `chart.css` (no shared
  stylesheet, by design); the map's basemap tiles and hex overlay stay fixed
  since the tiles are always a light raster.
- **Contributor guides tracked in-repo.** The chart- and param-authoring guides
  now live in the repository as `fireflyer/chart/SKILL.md` and
  `fireflyer/PARAM_SKILL.md` (previously only local, gitignored Claude Code
  skills). `CLAUDE.md` links both, and the skills point at these files as the
  single source of truth.

## [0.2.0] - 2026-07-08

### Added

- **Dashboard tabs.** A dashboard's `dashboard:` section can now be a mapping of
  tab name → layout list, splitting the page into tabs; a flat list stays the
  no-tabs form, unchanged. Only the active tab's charts load — switching is lazy
  via htmx — the tab bar is sticky, and crossfilters stay global across tabs. In
  the browser editor: add a tab from the row **"+"** menu (with a forced rename;
  cancelling undoes the add), rename a tab inline, move a tab's boundary between
  rows (switching tabs to reach a target row), and delete a tab — a non-first tab
  merges into the previous, and the first dissolves all tabs back to a flat list.
  Charts can be moved across tabs in move mode.

## [0.1.0] - 2026-07-07

First public release — an early MVP. Under heavy development and not yet
production-ready.

### Added

- **Charts from CSV.** `table`, `pie`, `bar`, `map`, and `number` chart types.
  Each takes a `dataset` and `title` plus a couple of chart-specific fields, and
  renders to standalone HTML (server-rendered, htmx-only, no build step).
- **YAML dashboards.** Declare datasets, charts, and page layout in a single
  YAML file via a compact layout DSL (`["@<height>", "<chart>:<width>", ...]`,
  headers, and separators). Render with `Dashboard.from_yaml(text).to_html()`.
- **Crossfiltering.** Click a chart value and every other chart narrows to
  match, with no page reload. Fixed `filters` can also be declared per chart.
- **Browser editor.** Two-pane editor: write YAML on the left, live preview on
  the right. Add charts from a menu with a form, drag rows and columns to
  resize, move charts, and edit each chart through a modal. Every edit is
  written back as clean YAML so the visual and code views stay in sync.
- **AI assistant.** Built-in chat edits the dashboard from plain-English
  requests, validating each change before applying it. Runs on Claude; gated by
  an `ANTHROPIC_API_KEY` and disabled gracefully when no key is set.
- **Use as a library.** `import fireflyer as ff`; charts render inline in
  Jupyter via `_repr_html_` or expose their HTML through `to_html()`.
- **Run with Docker or locally.** `docker compose up --build` for the editor
  with hot-reload, or `pip install -e ".[test]"` and `python -m fireflyer.web`.
- **Snapshot test suite.** Each test pairs an input CSV + chart/dashboard
  definition with the exact expected HTML in `tests/snapshots/`.
- **Source-available license.** Apache-2.0 with the Commons Clause.

[Unreleased]: https://github.com/dankor/fireflyer/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/dankor/fireflyer/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/dankor/fireflyer/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/dankor/fireflyer/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/dankor/fireflyer/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/dankor/fireflyer/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/dankor/fireflyer/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/dankor/fireflyer/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/dankor/fireflyer/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dankor/fireflyer/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/dankor/fireflyer/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/dankor/fireflyer/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dankor/fireflyer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dankor/fireflyer/releases/tag/v0.1.0
