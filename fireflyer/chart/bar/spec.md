# Bar chart

## Purpose
Display a calc distribution as **stacked** vertical bars, broken down by a second column.

## Behavior
- Reads the dataset.
- Applies the chart's `filters` (see architecture.md "Filters") before grouping.
- Labelling, zero-dropping, ordering and totals all happen **in Polars**, in a
  few vectorized passes; only the final SVG geometry walks rows in Python, and
  by then they're already filtered and ordered.
- Groups by `(x, y)` — or by `x` alone when `y` is omitted — and sizes each
  segment by its **calc** (see architecture.md
  "Calcs"); with no `calc` it counts records per pair. Empty/undefined
  groups are dropped, and so are **zeros** — a zero draws nothing, so it gets
  no segment, no legend row and no slot on the axis. Values are formatted by the
  calc's own `format`.
- The **sideways** layout pins its drawing to the left edge of the cell
  (`preserveAspectRatio="xMinYMid"`), since it reads left-to-right off a label
  gutter and dead space there is wasted. Upright bars stay centred
  (`xMidYMid`) — pinning those left only moves the gap to the right.
- Bars pack from the **left** of the plot at a fixed slot pitch, with leftover
  width collecting on the right and the baseline stopping where the bars do —
  the same way the sideways layout packs from the top and leaves leftover height
  at the bottom. Dividing the whole plot between a handful of bars instead left
  them marooned at the third-points with the chart's own emptiness between them.
- Renders one bar per `x` value, sorted by stack total descending — **unless `x`
  is temporal** (a `str2dt()` column calc, say), in which case the axis reads as a
  timeline and is sorted chronologically instead.
- **A temporal axis picks its own time grain.** There is no setting: the chart
  counts how many buckets each candidate grain (day → week → month → quarter →
  year) would produce and takes the **finest that fits `MAX_BARS`**, so a year of
  daily rows becomes ~12 months rather than 365 hairlines. It counts real buckets
  rather than estimating from the calendar span, so three rows spread over two
  years stay on `day`. **Day is the floor** — sub-daily timestamps all land in
  their day's bucket (an hourly bar chart is a different chart, not a grain of
  this one). Year is the ceiling — a long enough range overflows the cap and
  scrolls.
- Bucketing happens **before** the calc aggregates, so a month's bar is the calc
  over that month's rows, not a roll-up of daily results (which would break
  `avg`/`dcount`).
- Labels follow the grain: `2026-06-01` (day, and the Monday for a week),
  `2026-06` (month), `2026 Q2` (quarter), `2026` (year). Rows whose value is
  null — usually a `str2dt()` pattern that doesn't match every row — collect
  into one `(no date)` bar sorted last, rather than a blank one.
- **Grain picker.** Inside a dashboard a segmented control sits **directly
  beneath the plot**, on the axis it governs — `A` (auto) then `Y Q M W D`,
  coarsest to finest — and re-renders the chart at the chosen grain. `A`'s
  tooltip names the grain the automatic pick landed on. It's a sibling
  of the canvas, not a child, so it stays put when a long axis scrolls. The choice is per viewer, not per dashboard: it rides the
  page's hidden inputs beside the crossfilter tokens, so it survives crossfilter
  clicks and tab switches but is never written to the YAML. `A` puts the chart
  back on automatic. Standalone (no dashboard) there's no endpoint to post to,
  so no picker — the same rule crossfilter follows.
- **Long axis scrolls.** Few categories fill the cell as before; once there are
  too many for each bar to keep a minimum width (~28px), the plot grows and the
  canvas **scrolls horizontally** so every bar stays readable (the title and
  legend stay put). Each bar owns a **slot** — its width plus one gap — and the
  gap is a share of that slot, so once the plot hits its ceiling (`MAX_PLOT_W`,
  about 400 bars at full width) the bars thin out rather than the canvas growing
  without bound. Deriving the gap from the slot is also what keeps widths
  positive: a fixed gap used to survive the cap and eat the whole plot.
- **Windowed axis + scale.** Past `MAX_WINDOW_BARS` buckets the chart does not
  draw the whole axis: it renders that many, defaulting to the **most recent**,
  and a **scale** underneath moves the window: the whole range as
  `SCALE_SEGMENTS` hoverable segments with the current window lit, flanked by
  ‹ / › one-window steps. Each segment names the dates it would show in a
  tooltip revealed on hover, so **moving across the scale reads the timeline out
  as you go**; clicking one jumps there — a plain htmx post carrying
  `<cid>|<grain>|<offset>`. Grain and scale changes re-render **only this
  chart's cell** (`/dashboard/cell`, targeting `closest
  .fireflyer-dashboard-cell`): they change nothing for the other charts, unlike
  crossfilter, which still replaces the whole dashboard. The window keeps the SVG, the DOM and the label count
  bounded however long the range is.

  Segments rather than a drag-slider: a range input drags natively, but reading
  its *live* value to label the position needs JavaScript, which chart output
  forbids (architecture.md, "Server-rendering, htmx-only"). Hovering discrete
  elements gives the same read-as-you-move feedback with CSS alone — the same
  `:hover` reveal the bar segments already use.
- **The chart is the same size at every grain.** Any view shown whole uses the
  fixed `FIT_PLOT_W` plot, so the viewBox is *identical* (~2.5:1) at two bars and
  at thirty — switching grain redraws the same box rather than resizing the
  chart. The SVG is absolutely positioned to fill the canvas, which is what makes
  `height: 100%` resolve; against an auto-height parent it falls back to the
  viewBox's intrinsic aspect, and a tall one then overflowed the cell into a
  vertical scrollbar. `preserveAspectRatio` letterboxes inside the box, so the
  drawing is always fully visible. Bar width is capped at `MAX_BAR_W` so a
  two-bar year view spreads out instead of rendering two billboards.
  A windowed axis never scrolls — the scale moves the window instead.

  Windowing needs somewhere to post to, so it only applies **inside a
  dashboard**; standalone the whole axis is drawn and scrolls, since there'd be
  no way back to the hidden buckets. Changing grain resets to the default window
  (the grain buttons carry no offset). Legend totals cover the drawn window.
- **Lens scrollbar.** In scroll mode (an unwindowed long axis — standalone, or
  a categorical one) the canvas's horizontal scrollbar is styled up into a
  grabbable lens: drag the thumb to scroll, click the track to jump. The platform
  default is invisible until you already scroll (macOS overlay scrollbars), which
  makes a long axis look broken rather than scrollable.
- Within each bar, segments stack from the baseline upward in `y` order (largest at the bottom). Same `y` value always gets the same color across bars.
- The stack total for each bar is labelled above; the `x` value is labelled below (rotated slightly so long labels like ISO dates don't collide).
- A legend lists each `y` value as a **colour swatch and a label** — no numbers;
  the totals are already labelled on the bars and in the hover tooltips.
- More series than fit are reached with a **pager** (`◀ n/m ▶`), not a
  scrollbar — the same control the pie legend uses. Every series is still drawn
  in the bars, so paging only ever hides legend rows. `LEGEND_PAGE_SIZE` is
  fixed for the same reason it is on the pie: how many entries actually fit
  depends on label lengths and the cell's pixel width, neither knowable
  server-side. The pager posts to `/dashboard/cell`, so only this chart
  re-renders, and the page is ephemeral (a full re-render returns to page one).
  Standalone there's no endpoint to post to, so the whole legend renders.
- Inside a dashboard, clicking a segment crossfilters **both** of its
  dimensions — a segment *is* an (x, y) cell. Both parts ride in one token, so
  they toggle as a unit: clicking the same segment again clears both halves,
  never one. A bucketed x contributes a **half-open `between` range** (`2026-06`
  is `>= 2026-06-01, < 2026-07-01`), since an exact-value filter would match
  only the first day of the bucket; a categorical x contributes the exact value.
  Because the selection is a whole token, the chart highlights the **one cell**
  that was clicked — matching on the series value alone lit that colour in every
  bucket.
- The **legend rows also crossfilter**, but a row means "this series", so it
  stays one-dimensional (`y` only). Selected series stay at full opacity; the
  rest fade.
  Its **placement adapts to the cell, with no setting**: a horizontal row
  **above** the plot by default, switching to a right-hand column only in an
  extremely wide, short cell. The drawing is `min(width, aspect × height)`
  across, so a right-hand legend costs ~150px of width while one above costs
  ~26px of height — and width is what a bar chart is short of. Above therefore
  wins until the cell is past roughly 3:1, which a container query decides
  (`@container ffbar (min-aspect-ratio: 3/1)`), since the cell's size isn't
  knowable server-side. Container queries need a definite size, which only a
  dashboard cell guarantees, so the container is gated behind the
  `fireflyer-bar-embedded` class; standalone the default stands. The legend is
  moved with `order`, not in the markup, so it still reads after the chart.
- Categories beyond the palette length recycle colors.
- Each segment has a hover tooltip: the bucket as a **header**, the series and
  its **unrounded** value, and the calc's `description` when it has one. The
  value is the exact figure, thousands-separated, not the axis's abbreviated
  one — an abbreviation exists because the axis is short of space, and a tooltip
  is where you go to find out what it stood for, so the digits are grouped to be
  read. No percent: a share of the bar is a second thing
  to read past on the way to the number.
  The card hangs off the **top edge of its segment**, placed from that segment's
  own geometry (emitted inline as a share of the viewBox) and clamped so it can't
  leave the canvas. It anchors to a 1px **marker element carried inside the SVG**
  (a `<foreignObject>` at the segment's top-centre), never to the SVG shape: a
  shape isn't a CSS box, and a failed anchor drops a `position: fixed` card at
  its static position — which put cards beside the wrong bar. Being inside the
  SVG also puts the marker in the drawing's coordinate space, so letterboxing
  can't shift it away from its segment. Anchoring to a real box can't fail that way, so the card also
  escapes the chart card and the scroll pane rather than truncating. See SKILL.md
  "Tooltips".
- A stack total sums the segment values, so an **additive** calc (count/sum)
  is what makes a stacked bar meaningful.

## Theming
- Card, text, legend, and tooltip colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.
- Segment **fills** are the fixed categorical palette (theme-independent). The baseline axis and the value/label text are themed (`.fireflyer-bar-axis`/`-value`/`-label` read tokens via CSS rather than inline attributes).

## Parameters
- `dataset: str` — dataset name (or Parquet path standalone).
- `title: str` — chart title.
- `x: str` — column for the bar groups (x-axis labels). May be a **column calc**
  key as well as a real dataset column — that's how a text date becomes a proper
  date axis: `order_day: {formula: 'str2dt(day, YYYYMMDD)'}` (quoted — inline,
  str2dt()'s comma would otherwise split the YAML flow mapping).
- `y: str = ""` — **optional** column for stacking. Each unique `y` value
  becomes a colored segment within every bar where it appears. Omit it for a
  plain one-bar-per-category chart: a single series, no stacking, no legend (one
  unnamed series has nothing to tell apart), and a click filters the one
  dimension there is.
- `calc` — a calc **key** resolved against the dashboard's `calcs:`
  block, or an inline calc definition dict standalone. `None` (the default)
  means a per-(x, y) row count.
- `top: int = 0` — keep only the `top` biggest bars (0 = all). "Biggest" is by
  **calc value**, not by position: on a date axis it keeps the N busiest buckets
  and still draws them oldest-first, because a timeline that jumped to the
  largest bucket would stop being a timeline. Dropped rows leave the frame, so
  the value scale and the legend describe the bars actually drawn.
- `direction: str = "horizontal"` — which axis the categories run along.
  `horizontal` (the default, and what the chart has always drawn) runs them
  left-to-right with the bars growing upward and long labels tilted −30°.
  `vertical` runs them top-to-bottom with the bars growing rightward from a
  left-hand axis and labels written flat in a gutter — which is the point of it,
  since a long category name needs no tilting there. The gutter is **measured
  from the labels** (clamped between `VERT_LABEL_MIN` and `VERT_LABEL_MAX`), so
  short ones don't reserve room for long ones that aren't there. Sideways bands are a fixed
  height, so the viewBox grows downward with the category count and the canvas
  scrolls vertically rather than squeezing. *Note the naming describes the
  category axis, which is the opposite of the convention some chart libraries
  use.*
- `filters: list = []` — declarative pre-filter applied before grouping.

## Editor params
Edit-modal schema (`Bar.PARAMS`): dataset (dropdown), title (text), x (column dropdown),
y (column dropdown), calc (calc dropdown), filters (filter builder). Widgets
live in `fireflyer/params.py`.
