"""AI assistant for the dashboard editor.

A thin wrapper over the Anthropic Messages API: given the current dashboard
YAML and a user request, Claude replies in plain text and — when a change is
warranted — calls the `update_dashboard` tool with the complete new YAML. The
proposed YAML is validated against the real parser before we hand it back, with
a bounded self-repair loop if it doesn't parse.

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

A dashboard has exactly three top-level keys: `datasets`, `charts`, `dashboard`.

```
datasets:
  <id>:
    path: files/orders.csv      # path to a CSV; `path` is the only key

charts:
  <id>:
    type: table | pie | bar | map | number
    dataset: <dataset id>       # must exist under datasets
    title: <string>
    # ...type-specific keys below...

dashboard:
  - <layout item>
  - <layout item>
```

## Chart types and their keys

- table: `search` (bool, default true), `pagination` (rows per page, int; 0 = show all). Shows the first 1000 rows.
- pie: `column` (the column to group & count). Count aggregation only.
- bar: `x` (column for bar groups), `y` (column to stack/break down by). Stacked count bars.
- map: `lat`, `lng` (column names), `grid_size` (hex size, int, default 20), `zoom` (int or omit for auto-fit). Plots points as a hex heatmap.
- number: `column` (the column to aggregate), `agg` (one of `count`, `sum`, `dcount`, `max`, `min`; default `count`), `format` (`compact` big-number abbreviation like `1.42k` — the default — or `full` for all digits). Shows one big scalar KPI. `count` = non-null values, `dcount` = distinct values.

Every chart also accepts an optional `filters` list. Each filter is `{column, op, values}` where `op` is `in` or `ni` (not-in), e.g.
```
filters:
  - column: status
    op: in
    values: [open, pending]
```

## Layout DSL (the `dashboard` list)

Each item is one of:
- Row: a YAML array like `["@40", "orders:3", "status:2"]`. The first element is the row height `"@<units>"` (1 unit = 8px). The rest are widget tokens `"<chart_id>"` or `"<chart_id>:<width>"` — the width is optional.
- Header: a plain string, e.g. `Overview` — a full-width section title.
- Separator: the string `"-"` — a horizontal divider.

Rules (these are validated; broken layouts are rejected):
- Widths are proportions and OPTIONAL — a bare `orders` means `orders:1`. `a:1 b:4` is the same 20/80 split as `a:20 b:80`; `a b c` makes three equal columns. Any positive numbers work — there is no sum-to-100 requirement.
- Vertical merge (a chart spanning rows) = repeat the chart's id **bare** (no width) in the row(s) directly below where it's sized. e.g. `["@40","orders:3","status:2"]` then `["@30","by_day","status"]` — `status` spans both rows, `by_day` fills the left column. The first row sets the sizes; a lower row's other cells fill the leftover width, splitting it by their own widths.
- A chart id may appear more than once ONLY as such a contiguous bare-repeat span. Repeating it WITH a width, across a header/separator, or skipping a row is an error.
- All heights and any given widths MUST be > 0. Every chart id used MUST exist in `charts`; every dataset referenced MUST exist in `datasets`.

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


def _user_turn(message: str, yaml_text: str) -> str:
    return (
        f"{message}\n\nThe current dashboard YAML is:\n```yaml\n{yaml_text}\n```"
    )


def _text_of(content) -> str:
    return "".join(b.text for b in content if b.type == "text").strip()


def run_chat(message: str, yaml_text: str, history: list | None = None) -> dict:
    """Run one assistant turn.

    Returns `{"reply": str, "yaml": str | None}`. `yaml` is the validated new
    document when the model proposed a (parseable) change, else None.
    """
    messages: list = []
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": _user_turn(message, yaml_text)})

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
