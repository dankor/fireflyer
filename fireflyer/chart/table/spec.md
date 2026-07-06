# Table chart

## Purpose
Display the contents of a CSV as a tabular HTML view.

## Behavior
- Reads the CSV.
- Reads at most the first 1000 rows.
- Right-aligns numeric columns; left-aligns everything else.
- Formats numeric cells with thousands separators (e.g. `1,234,567`).
- Alternating row backgrounds; hover highlights the row under the cursor.
- Empty cells render blank, not as the string `None`.

## Parameters
- `dataset: str` — path to the CSV.
- `title: str` — chart title.
- `search: bool = True` — render a search input above the table that filters rows by case-insensitive substring match across all columns.
- `pagination: int = 5` — rows per page. `0` disables pagination (show everything in the 1000-row cap).
- `filters: list = []` — declarative pre-filter applied after the 1000-row read and before search/pagination. Each entry is `{column, op (in|ni), values}`. Filters whose `column` is absent from the CSV are silently skipped. See architecture.md "Filters".

## Search + pagination
- Controls are wired with htmx, hitting `/chart/table` with `?q=...&page=N` and swapping the chart fragment in place. No other JavaScript.
- The chart's outer container has a stable id; htmx swaps the whole container so search and pagination state both re-render together.
- Search input fires on keyup (debounced) and resets to page 1.
- Search is applied before pagination — page count reflects filtered row count.
- Pagination footer shows prev / numbered pages / next. Hidden when only one page after filtering.
- When `to_html()` is invoked outside the Fireflyer web app, the controls render but htmx is absent, so they are inert. Acceptable for MVP.

## Editor params
Edit-modal schema (`Table.PARAMS`): dataset (dropdown), title (text), search (checkbox),
pagination (number), filters (filter builder). Widgets live in `fireflyer/params.py`.
