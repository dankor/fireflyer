"""AI assistant for the dashboard editor.

A thin wrapper over the Anthropic Messages API: given the current dashboard
YAML, the available datasets' column schemas (name + type, no data), and a user
request, Claude replies in plain text and — when a change is warranted — calls
the `update_dashboard` tool with the complete new YAML. The proposed YAML is
validated against the real parser before we hand it back, with a bounded
self-repair loop if it doesn't parse.

This lives in `web/` because it's part of the editor (a dev tool), not the
library core. Keep the DSL spec below in sync with `architecture.md` and the
chart `spec.md` files if the layout or chart rules change.
"""

import anthropic

from fireflyer.dashboard import Dashboard, DashboardError

# The user asked for Sonnet specifically.
MODEL = "claude-sonnet-4-6"

# Max round-trips per request. One initial call plus up to two repair attempts
# if the model returns YAML the parser rejects.
MAX_ATTEMPTS = 3

_SYSTEM_TEXT = """You are the AI assistant inside Fireflyer, a tool that turns CSV files into HTML dashboards. You help the user edit a single dashboard YAML file by chatting in plain language.

When the user asks for a change to the datasets, charts, or layout, call the `update_dashboard` tool with the COMPLETE new YAML (never a diff or a fragment). When the user only asks a question or wants advice, reply in text without calling the tool. Always keep a short, friendly text reply explaining what you did or suggesting next steps.

# Dashboard YAML format

A dashboard has these top-level keys: `name`, `charts`, `layout`, and two
OPTIONAL blocks — `calcs` (see Calcs) and `datasets` (see Datasets, for CSV
carried in the file itself). `name` is required — a short human-readable title
for the whole dashboard. Always include it and preserve the existing one unless
asked to rename. Keep a `datasets:` block LAST, after `layout:`.

```
name: <string>                    # required: the dashboard's display name

charts:
  <id>:
    type: table | pie | bar | map | number
    dataset: <dataset name>     # one of the available datasets (see Datasets)
    title: <string>
    # ...type-specific keys below...

layout:
  - <layout item>
  - <layout item>
```

# Datasets

A chart names its data with `dataset: <name>`. A name resolves one of two ways.

**Managed datasets** are uploaded outside the dashboard file. The available ones
and their column schemas (each column's name and type, no data) are listed in
every message. Never invent a managed dataset, file path, or column name — if a
request needs a column that isn't listed, say so instead of guessing.

**Inline datasets** are CSV written into the dashboard itself, under an OPTIONAL
top-level `datasets:` block. The first line is the header; the rest is ordinary
CSV. Put this block LAST in the file, after `layout:`, so the readable parts
stay at the top:

```
datasets:
  order_data: |
    id,status,amount
    1,paid,42
    2,pending,15
```

Use inline data when the user asks you to make up, mock, sample or demo
something, or wants to try an idea before they have real data — invent a
plausible table and chart it in the same edit, rather than asking them to upload
a file. Keep it small (tens of rows is plenty to show a chart working) and give
the columns realistic names and values. An inline name shadows a managed one, so
avoid reusing an existing dataset's name unless the user means to override it.

## Chart types and their keys

Charts no longer carry an aggregation — they reference a named **calc** (see Calcs below). A chart with no `calc` defaults to a row count.

- table: `columns` (list of columns), `measures` (list of calc keys), `sort` (list of keys, `-` = descending, `+`/bare = ascending, most significant first), `search` (bool, default true), `pagination` (rows per page, int; 0 = show all).
  With no `measures` it lists raw rows (first 1000) and `columns` just picks which to show. With `measures` it **groups by** `columns` and shows one aggregated column per measure — `columns: [status]`, `measures: [revenue, order_count]`, `sort: ['-revenue']`. `measures` with no `columns` gives one grand-total row. A measure must be a calc key naming a *value*; a column calc is a dimension and belongs in `columns`. Note the table takes `measures` (a list), while every other chart takes a single `calc`.
- pie: `column` (the category column to group by), `calc` (calc key; the slice size per category). The centre total is the calc re-aggregated over the whole dataset.
- bar: `x` (column for bar groups), `y` (OPTIONAL column to stack/break down by — omit it for plain one-bar-per-category bars with no legend), `calc` (segment size per x,y), `top` (int, keep only the N biggest bars; 0 = all), `direction` (`horizontal` default = categories left-to-right with bars growing up, or `vertical` = categories top-to-bottom with bars growing rightward). Use an additive calc (count/sum) so stacking is meaningful.
- map: `lat`, `lng` (column names), `grid_size` (hex size, int, default 20), `zoom` (int or omit for auto-fit), `calc` (per-hex weight — must be a `count` or `sum` calc). Plots points as a hex heatmap.
- number: `calc` (the scalar KPI to show), `title`. The value is formatted by the calc's own `format`.

## Calcs

`calcs` is an OPTIONAL top-level block, **keyed by dataset name**, then by a calc key unique within that dataset. A chart's `calc:` resolves within its own `dataset:`; a calc may only reference other calcs in the same dataset.

A calc is one of three kinds. NOTHING in the YAML names the kind: `agg` means aggregate, and a bare `formula` is sorted into derived vs column by WHAT IT REFERENCES.
- Aggregate: `agg` (one of `count`, `sum`, `dcount`, `min`, `max`, `avg`) + a row-level `formula` (an expression over COLUMNS, e.g. `amount` or `price * qty`). `count` needs no formula. Optional `filters` (same shape as chart filters) pre-narrow the rows — this is how you express conditional aggregation like "count of won deals".
- Derived: a `formula` over other CALC keys (no `agg`), e.g. `revenue / orders_count`. Computed per group, so it's a true per-group ratio. Its leaves must be aggregate calcs (no derived-of-derived).
- Column: a row-level `formula` (no `agg`, no `filters`) over DATASET COLUMNS rather than sibling calcs. This is a calculated COLUMN, not a value — a DIMENSION you use wherever a column name goes (a chart's `x`, `y`, `column`, a filter, another calc's formula), never as a chart's `calc:`. Column calcs are applied in declaration order, so a later one may use an earlier one's key.
  A column calc may also carry `name` and `description` to **relabel** a column wherever it is displayed, without renaming it: `status: {name: Order status, description: Where the order got to, formula: status}`. A self-reference in a formula means the DATASET COLUMN (a calc cannot reference itself), so this overlays the raw column. The key stays what `columns:`, filters and charts refer to; only the displayed label changes.

So a no-`agg` `formula` is derived when every name in it is another calc that produces a value, and a calculated column when it names a dataset column (or calls `str2dt()`, or uses another column calc). Write neither kind's name down — just the formula.

All three formula kinds allow `+ - * /`, parentheses and numeric literals, plus ONE function: `str2dt(<column>, <pattern>)`. `str2dt()` turns a text or integer column into a real date so it can go on an axis. The pattern is written the way people write dates — `YYYY`, `YY`, `MM` (month), `DD`, `HH`, `mm` (minute), `ss`, `Z` (UTC offset, with or without the colon), with any other characters as literal separators — e.g. `str2dt(order_date, YYYYMMDD)`, `str2dt(created_at, YYYY-MM-DD HH:mm:ss)`, `str2dt(d, DD/MM/YYYY)`, `str2dt(ts, YYYY-MM-DD HH:mm:ssZ)` for `2024-06-26 17:14:03+03:00`, `str2dt(ts, YYYY-MM-DDTHH:mm:ssZ)` for the ISO form. A pattern with a time part (including `Z`) yields a datetime, otherwise a date; an offset is normalized to UTC. `ss` accepts optional fractional seconds and `Z` accepts `+03:00`/`+0300`/bare `Z`, so one pattern covers the usual export variants. Values that don't match the pattern become null and collect into a single `(no date)` bar — if the user reports one of those, their pattern doesn't fit every row. Do NOT quote the pattern itself. DO quote the whole formula whenever the calc is written inline as `{...}` — `{formula: 'str2dt(day, YYYYMMDD)'}` — because str2dt()'s comma would otherwise split the YAML flow mapping. In block style (`formula: str2dt(day, YYYYMMDD)` on its own line) no quoting is needed. A bar chart with a date `x` is ordered chronologically instead of by size, and picks its own time grain (day/week/month/quarter/year — day is the finest) so the bar count stays readable, and windows a long axis to the most recent bars with a scale to move it — there is NO grain/scale key to set, and you must not invent one.

Optional per-calc metadata: `name` (display label), `description`, `format`. A `format` token is `<prefix><0,.pattern>[a]<suffix>` — the `0 , .` run is the number pattern (decimals from digits after `.`, thousands if a `,` is in the integer part), text around it is literal, and an optional `a` after the pattern abbreviates big numbers with a `k`/`m`/`b`/`t` unit (decimals are the max shown — truncated, trailing zeros dropped, so `0.0a` gives 1971 → `1.9k`, 2000 → `2k`). Examples: `0.00$` → `1234.50$`, `$0,0` → `$1,234`, `0.0a $` → `23.4k $`, `0.0%` → `25.0%`. There is NO percentage auto-scaling — for a `0.25` ratio shown as `25%`, multiply in the formula (`* 100`) and use `0.0%`.

```
calcs:
  orders:                              # dataset name
    revenue: {name: Revenue, agg: sum, formula: amount, format: '0.00$'}
    orders_count: {agg: count}
    avg_order_value: {formula: revenue / orders_count, format: '0.00$'}
    order_day: {formula: 'str2dt(order_date, YYYYMMDD)'}   # names a column ⇒ calculated column
```

Every chart also accepts an optional `filters` list. Each filter is `{column, op, values}` where `op` is `in`, `ni` (not-in) or `between`. `between` takes exactly two values (low, high) and is half-open — `low <= v < high`. Example:
```
filters:
  - column: status
    op: in
    values: [open, pending]
```

## Layout DSL (the `layout` list)

Each item is one of:
- Row: a YAML array like `["@40", "orders:3", "status:2"]`. The first element is the row height `"@<units>"` (1 unit = 8px). The rest are widget tokens `"<chart_id>"` or `"<chart_id>:<width>"` — the width is optional.
- Header: a plain string, e.g. `Overview` — a full-width section title.
- Separator: the string `"-"` — a horizontal divider.

Rules (these are validated; broken layouts are rejected):
- Widths are proportions and OPTIONAL — a bare `orders` means `orders:1`. `a:1 b:4` is the same 20/80 split as `a:20 b:80`; `a b c` makes three equal columns. Any positive numbers work — there is no sum-to-100 requirement.
- Vertical merge (a chart spanning rows) = repeat the chart's id **bare** (no width) in the row(s) directly below where it's sized. e.g. `["@40","orders:3","status:2"]` then `["@30","by_day","status"]` — `status` spans both rows, `by_day` fills the left column. The first row sets the sizes; a lower row's other cells fill the leftover width, splitting it by their own widths.
- A chart id may appear more than once ONLY as such a contiguous bare-repeat span. Repeating it WITH a width, across a header/separator, or skipping a row is an error.
- All heights and any given widths MUST be > 0. Every chart id used MUST exist in `charts`; every dataset referenced MUST be one of the available datasets.

# Editing rules

- Preserve the user's existing datasets, charts, and ids unless they ask to change them.
- Only reference columns/datasets that already exist; don't invent CSV files or columns.
- Keep the YAML valid: keys present, ids resolving, spans written as a bare repeat.
- Return the whole file every time you call the tool."""

SYSTEM = [{"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}]

UPDATE_TOOL = {
    "name": "update_dashboard",
    "description": (
        "Replace the dashboard YAML with an updated version. Call this whenever "
        "the user's request implies a change to datasets, charts, or layout. "
        "Provide the COMPLETE new YAML document, not a diff or a fragment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "yaml": {
                "type": "string",
                "description": "The complete updated dashboard YAML document.",
            },
            "summary": {
                "type": "string",
                "description": "One or two sentences describing what changed.",
            },
        },
        "required": ["yaml", "summary"],
        "additionalProperties": False,
    },
}

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    # Lazily constructed so importing this module never requires a key; the
    # SDK reads ANTHROPIC_API_KEY from the environment.
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _datasets_block(datasets) -> str:
    """A schema-only listing (column name:type, no data) of the datasets the
    dashboard can reference — so the model builds charts/calcs from real
    columns instead of guessing. `datasets` is a list of
    `{"name": str, "columns": [{"name": str, "dtype": str}, ...]}`."""
    if not datasets:
        return "Available datasets: (none yet — the user must add a dataset first)."
    lines = ["Available datasets (each column shown as name:type; no data):"]
    for d in datasets:
        cols = ", ".join(f"{c['name']}:{c['dtype']}" for c in d.get("columns", []))
        lines.append(f"- {d['name']}: {cols or '(no columns)'}")
    return "\n".join(lines)


def _user_turn(message: str, yaml_text: str, datasets) -> str:
    return (
        f"{message}\n\n"
        f"{_datasets_block(datasets)}\n\n"
        f"The current dashboard YAML is:\n```yaml\n{yaml_text}\n```"
    )


def _text_of(content) -> str:
    return "".join(b.text for b in content if b.type == "text").strip()


def run_chat(
    message: str,
    yaml_text: str,
    history: list | None = None,
    datasets: list | None = None,
) -> dict:
    """Run one assistant turn.

    `datasets` is the schema-only listing of available datasets (see
    `_datasets_block`) so the model can build charts and calcs from real
    columns. Returns `{"reply": str, "yaml": str | None}`; `yaml` is the
    validated new document when the model proposed a (parseable) change.
    """
    messages: list = []
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append(
        {"role": "user", "content": _user_turn(message, yaml_text, datasets)}
    )

    client = _get_client()
    reply_parts: list[str] = []
    new_yaml: str | None = None
    last_error: str | None = None

    for _ in range(MAX_ATTEMPTS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=SYSTEM,
            tools=[UPDATE_TOOL],
            messages=messages,
        )
        text = _text_of(response.content)
        if text:
            reply_parts.append(text)

        tool_use = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if tool_use is None:
            break  # plain reply — a question answered or advice given

        candidate = tool_use.input.get("yaml", "")
        summary = tool_use.input.get("summary", "")
        try:
            Dashboard.from_yaml(candidate)
        except DashboardError as exc:
            # Hand the error back and let the model try once more.
            last_error = str(exc)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "is_error": True,
                    "content": (
                        f"That dashboard YAML is invalid: {exc}. "
                        "Fix it and call update_dashboard again."
                    ),
                }],
            })
            continue

        new_yaml = candidate
        if summary:
            reply_parts.append(summary)
        break

    if new_yaml is None and last_error:
        reply_parts.append(f"(I couldn't produce a valid layout: {last_error})")

    reply = "\n\n".join(p for p in reply_parts if p) or "Done."
    return {"reply": reply, "yaml": new_yaml}
