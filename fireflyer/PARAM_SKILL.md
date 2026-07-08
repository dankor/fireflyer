---
name: param
description: Add or change a Fireflyer editor param widget, or wire a chart's PARAMS. Use when the edit-chart modal needs a new input type (dropdown, number, toggle, list builder…), when a chart gains/loses/renames a constructor field, or when changing how a param renders/parses/serializes. Covers params.py, the chart PARAMS declaration, config_edit.py, and the sync guard test.
---

# Fireflyer editor params

The editor's **edit-chart modal** is built from reusable widget classes in
`fireflyer/params.py`. Each chart lists its editable config as `PARAMS`; the
modal renders those, and saving rewrites just that chart's YAML block. This is
the one abstraction the project allows against its anti-abstraction rule — keep
it lean and editor-focused. Read `architecture.md` → "Chart params & editor
modal" and `CLAUDE.md` before changing behavior.

## The `Param` contract (`fireflyer/params.py`)

Every widget subclasses `Param` and implements three things:

```python
class MyParam(Param):
    kind = "mykind"                    # data-attr hook for the editor JS, if needed

    def render(self, value, ctx) -> str:   # autoescaped HTML input, name == self.name
        return self._wrap(f'<input class="ff-input" name="{escape(self.name, quote=True)}" ...>')

    def parse(self, form):                 # form.get(name) / form.getlist(name) -> python value
        return (form.get(self.name) or "").strip()

    def to_yaml(self, value):              # value as it appears in YAML (default: unchanged)
        return value
```

- `render` receives the **current value** and a `ParamContext` (`datasets` id→path,
  `dataset_id`, `columns` — the chart dataset's column names). Use `_wrap(inner)`
  for the label+field shell, `_options(values, current)` for `<select>` options.
- **Always autoescape** interpolated values (`html.escape(..., quote=True)` for
  attributes) — CSV/dataset values must not inject markup. Same rule as chart
  templates. Never build widget HTML that bypasses escaping.
- `parse(form)` takes anything with `.get`/`.getlist` (Starlette FormData in the
  app, `FakeForm` in tests). Return the Python value the constructor expects.
- `to_yaml` returning `None` or an empty list/dict makes the emitter **drop the
  key** (e.g. an unset nullable `IntParam` like the map's `zoom`).

Existing widgets to reuse before writing a new one: `TextParam`, `DatasetParam`,
`ColumnParam`, `ChoiceParam(choices)`, `IntParam(minimum, maximum, step, nullable)`,
`BoolParam`, `FilterListParam`. Add a new subclass **only** when none fit.

Note: form controls are styled by the editor page (`app.py`) via the `.ff-input`
class and the theme tokens — a new widget that uses `class="ff-input"` inherits
light/dark theming for free; don't hardcode colors in widget HTML.

## Wiring a chart's params

In the chart's `chart.py`, declare a `PARAMS` class attribute — one `Param` per
constructor field, in the order the modal should show them. (`type` is not a
Param — the modal renders a chart-type dropdown itself, from `CHART_TYPES`, and
`config_edit.py` handles swapping the type on save.)

```python
from fireflyer.params import DatasetParam, TextParam, ColumnParam, ChoiceParam, FilterListParam

@dataclass
class Number:
    dataset: str
    title: str
    column: str
    agg: str = "count"
    format: str = "compact"
    filters: list = field(default_factory=list)

    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("column", "Column"),
        ChoiceParam("agg", "Aggregation", AGGREGATIONS),
        ChoiceParam("format", "Format", FORMATS),
        FilterListParam("filters", "Filters"),
    ]
```

`PARAMS` is a plain class attribute on the dataclass (no type annotation, so it's
not a field). **`PARAMS` names must exactly match the constructor fields** —
`tests/test_chart_params_match_constructor.py` enforces it, so add a Param the
moment you add a constructor field (and vice versa).

## Save path (`fireflyer/config_edit.py`) — usually no change needed

`build_form(text, cid)` renders the modal; `apply_edit(text, cid, form)` parses
each Param, rebuilds the chart's YAML block via `emit_chart_block`, and
**surgically replaces** just that block (`replace_chart_block`) — siblings,
comments, and blank lines elsewhere stay byte-for-byte. It then re-validates with
`Dashboard.from_yaml`. You only touch this file if you change how a block is
emitted or located. Keep this logic here (pure) — not in `app.py` — so it
unit-tests without FastAPI/anthropic.

Adding a chart reuses the same widgets: `build_add_form(text, ctype, add_mode,
add_index)` renders the create modal (constructor defaults via `_defaults`), and
`add_chart(text, form)` generates a unique id, appends the block, and splices a
placement into the `dashboard:` list (flow-style rows only). New param classes
work in both paths automatically — no extra wiring.

The thin endpoints in `web/app.py` are `POST /chart/config/form` + `/save` (edit)
and `/add-form` + `/create` (add); each returns form HTML or `{ok, yaml|error}`.
The pencil, gutter "+" buttons, and modal JS live in the editor page (`app.py`
INDEX) and are gated by the `editing` flag so they never ship in `to_html()`.

## Tests

- `tests/test_params.py` — per widget: `render` emits the right control with the
  current value selected/checked; `parse` round-trips; `to_yaml` shape. Use the
  `FakeForm` helper there.
- `tests/test_config_edit.py` — surgical replace preserves siblings/comments and
  re-parses; `emit` drops None/empty; an invalid value surfaces as `DashboardError`.
- `tests/test_chart_params_match_constructor.py` — the PARAMS↔constructor guard.

Run: `python -m pytest tests/test_params.py tests/test_config_edit.py tests/test_chart_params_match_constructor.py -q`,
then the full suite `python -m pytest -q --ignore=tests/test_chat.py`.

## Definition of done

- [ ] New widget (if any) is a `Param` subclass in `params.py`, autoescaped,
      with `render`/`parse`/`to_yaml`.
- [ ] Affected chart's `PARAMS` lists every constructor field, in display order.
- [ ] Sync-guard + param + config-edit tests pass; full suite green.
- [ ] Chart `spec.md`'s "Editor params" line updated; docs still accurate.
