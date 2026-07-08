# Bar chart

## Purpose
Display a count distribution as **stacked** vertical bars, broken down by a second column.

## Behavior
- Reads the CSV.
- Applies the chart's `filters` (see architecture.md "Filters") before grouping.
- Groups by `(x, y)` and counts records per pair.
- Renders one bar per `x` value, sorted by total count descending.
- Within each bar, segments stack from the baseline upward in `y` order (most common at the bottom). Same `y` value always gets the same color across bars.
- The total count for each bar is labelled above; the `x` value is labelled below (rotated slightly so long labels like ISO dates don't collide).
- A legend lists each `y` value with its color swatch and total count across all bars.
- Categories beyond the palette length recycle colors.
- Only count aggregation is supported.

## Theming
- Card, text, legend, and tooltip colors come from the shared light/dark token set (see architecture.md "Theming"). The chart follows the viewer's OS preference unless a `data-ff-theme="light|dark"` override sits on the chart, the dashboard, or `<html>`; `to_html(theme=...)` forces one palette for standalone rendering.
- Segment **fills** are the fixed categorical palette (theme-independent). The baseline axis and the value/label text are themed (`.fireflyer-bar-axis`/`-value`/`-label` read tokens via CSS rather than inline attributes).

## Parameters
- `dataset: str` — path to the CSV.
- `title: str` — chart title.
- `x: str` — column for the bar groups (x-axis labels).
- `y: str` — column for stacking. Each unique `y` value becomes a colored segment within every bar where it appears.
- `filters: list = []` — declarative pre-filter applied before grouping.

## Editor params
Edit-modal schema (`Bar.PARAMS`): dataset (dropdown), title (text), x (column dropdown),
y (column dropdown), filters (filter builder). Widgets live in `fireflyer/params.py`.
