# Bar chart

## Purpose
Display a measure distribution as **stacked** vertical bars, broken down by a second column.

## Behavior
- Reads the dataset.
- Applies the chart's `filters` (see architecture.md "Filters") before grouping.
- Groups by `(x, y)` and sizes each segment by its **measure** (see architecture.md
  "Measures"); with no `measure` it counts records per pair. Empty/undefined
  groups are dropped. Values are formatted by the measure's own `format`.
- Renders one bar per `x` value, sorted by stack total descending.
- Within each bar, segments stack from the baseline upward in `y` order (largest at the bottom). Same `y` value always gets the same color across bars.
- The stack total for each bar is labelled above; the `x` value is labelled below (rotated slightly so long labels like ISO dates don't collide).
- A legend lists each `y` value with its color swatch and total across all bars.
- Categories beyond the palette length recycle colors.
- A stack total sums the segment values, so an **additive** measure (count/sum)
  is what makes a stacked bar meaningful.

## Theming
- Card, text, legend, and tooltip colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.
- Segment **fills** are the fixed categorical palette (theme-independent). The baseline axis and the value/label text are themed (`.fireflyer-bar-axis`/`-value`/`-label` read tokens via CSS rather than inline attributes).

## Parameters
- `dataset: str` — dataset name (or Parquet path standalone).
- `title: str` — chart title.
- `x: str` — column for the bar groups (x-axis labels).
- `y: str` — column for stacking. Each unique `y` value becomes a colored segment within every bar where it appears.
- `measure` — a measure **key** resolved against the dashboard's `measures:`
  block, or an inline measure definition dict standalone. `None` (the default)
  means a per-(x, y) row count.
- `filters: list = []` — declarative pre-filter applied before grouping.

## Editor params
Edit-modal schema (`Bar.PARAMS`): dataset (dropdown), title (text), x (column dropdown),
y (column dropdown), measure (measure dropdown), filters (filter builder). Widgets
live in `fireflyer/params.py`.
