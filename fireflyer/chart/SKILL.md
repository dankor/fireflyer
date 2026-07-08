---
name: chart
description: Create a new Fireflyer chart type or update an existing one (table/pie/bar/map/number). Use when adding a chart, changing a chart's params/rendering/aggregation, or wiring a new `type:` into dashboards. Covers the folder layout, registry wiring, templates, tests, and demo YAML that must all move together.
---

# Add or update a Fireflyer chart

Every chart is a self-contained folder under `fireflyer/chart/<name>/`. Adding or
changing one touches a fixed, small set of files — do all of them in one change or
something breaks (dashboards won't resolve the `type:`, snapshots go stale, the AI
assistant's DSL drifts). Read `architecture.md` and the target chart's `spec.md`
before changing rendering or aggregation behavior. **Ask before editing
`architecture.md`** — it's a controlled spec doc.

Hard rules (from `CLAUDE.md` / `architecture.md`, non-negotiable):
- Server-rendered HTML + inline SVG + CSS + htmx only. **No hand-written JS** in
  chart output (the web editor is the only exception).
- **Jinja2 autoescaped templates** — never build chart HTML with f-strings; CSV
  values must not be able to inject HTML.
- **CSS is per-chart**, namespaced under `.fireflyer-<name>`, read once at import
  and injected inline. No shared stylesheet, no build step, no npm.
- **Colors come from theme tokens, never hardcoded.** Every color is a `var(--ff-*)`
  token; each `chart.css` carries its own copy of the light/dark palette blocks so
  the chart themes standalone. Inline SVG `fill`/`stroke` that must theme move to a
  CSS class. See "Theming" below and `architecture.md` → Styling → Theming.
- Keep `to_html` skimmable in ~15s. Small functions, private `_helpers` in the same
  file, comments explain *why*. No new abstractions/registries/config layers.

## Anatomy of a chart folder

```
fireflyer/chart/<name>/
├── __init__.py    # from fireflyer.chart.<name>.chart import <Class>; __all__ = ["<Class>"]
├── chart.py       # @dataclass <Class>: reads CSV, aggregates, renders _TEMPLATE
├── chart.html     # Jinja2 (autoescaped). First line: <style>{{ css|safe }}</style>
├── chart.css      # namespaced under .fireflyer-<name>; includes the card chrome
└── spec.md        # source of truth for this chart's behavior — update it every change
```

`chart.py` skeleton (mirror `pie/chart.py` for the simplest shape,
`number/chart.py` for a scalar, `bar/chart.py` for crossfilter-clickable):

```python
from dataclasses import dataclass, field
from pathlib import Path
import jinja2, polars as pl
from fireflyer import filters as filters_mod

_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template((_DIR / "chart.html").read_text(), autoescape=True)

@dataclass
class <Class>:
    dataset: str
    title: str
    # ...chart-specific params, with defaults last...
    filters: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)
        # validate params here; raise ValueError with a clear message on bad input

    def to_html(self, *, theme: str | None = None) -> str:
        df = pl.read_csv(self.dataset)
        df = filters_mod.apply(df, self.filters)   # ALWAYS filter before aggregating
        # ...compute...
        return _TEMPLATE.render(
            css=_CSS, title=self.title, ...,
            # "" = no override → follows the OS / an ancestor's data-ff-theme.
            ff_theme=theme if theme in ("dark", "light") else "",
        )

    def _repr_html_(self) -> str: return self.to_html()
    def __str__(self) -> str: return self.to_html()
```

Conventions to copy exactly:
- `chart.css` starts with the **theme token blocks** (copy from any existing chart —
  `pie/chart.css` is the reference), then re-declares the shared `.fireflyer-chart`
  card chrome + `.fireflyer-title` block using `var(--ff-*)` tokens (duplication
  across charts is intentional — collisions are harmless because the rules are
  identical). Add your `.fireflyer-<name>` rules after it. See "Theming" below.
- Stringify polars values for the template with `str(v) if v is not None else ""`.
- Parameter validation raises `ValueError` (or `filters_mod.FilterError`); the
  dashboard parser catches `(TypeError, ValueError)` and re-raises as `DashboardError`,
  so bad YAML gets a clean message. Don't swallow it or use `assert`.

## Theming (light + dark)

Charts ship a light and a dark palette; selection is automatic (OS
`prefers-color-scheme`) and overridable via a `data-ff-theme="light|dark"`
attribute on the chart root or any ancestor. Copy the pattern from an existing
chart — don't invent colors:

- **`chart.css`**: paste the four token blocks from `pie/chart.css` (base = light,
  `@media (prefers-color-scheme: dark)` = auto-dark, then `[data-ff-theme="light"]`
  and `[data-ff-theme="dark"]` self+ancestor overrides), each scoped to
  `.fireflyer-chart`. Trim the token set to what your chart uses; keep the light and
  dark values in sync with the other files. Then reference tokens via `var(--ff-ink)`,
  `var(--ff-panel)`, `var(--ff-border)`, `var(--ff-muted)`, `var(--ff-accent)`, etc.
- **`chart.html`**: the root element takes the override attribute —
  `{% if ff_theme %} data-ff-theme="{{ ff_theme }}"{% endif %}` (on the `<article
  class="fireflyer-chart …">`, or the `.fireflyer-chart-root` wrapper if you use one).
  **No hardcoded colors in inline SVG** — give the element a class and set
  `fill`/`stroke` from a token in `chart.css` (see bar's `.fireflyer-bar-axis`, pie's
  `.fireflyer-pie-hole`). Data-driven fills (a categorical palette) stay inline and are
  intentionally theme-independent.
- **`to_html`**: take `theme: str | None = None` and pass
  `ff_theme=theme if theme in ("dark", "light") else ""` (see skeleton above). Inside a
  dashboard the theme is inherited from the dashboard root, so this only matters for
  standalone rendering.
- **`spec.md`**: add a short `## Theming` section noting which parts follow the token
  set and which (if any) colors are fixed.

## Wiring a NEW chart type in (5 edits)

1. `fireflyer/chart/__init__.py` — add `from fireflyer.chart.<name> import <Class> as <name>`
   and append `"<name>"` to `__all__`. This exposes `ff.chart.<name>(...)`.
2. `fireflyer/dashboard.py` — add the import and a `"<name>": <Class>` entry to the
   `CHART_TYPES` dict (keep it a plain dict — no registry).
3. `fireflyer/web/chat.py` — add `<name>` to the `type:` union line and a bullet under
   "Chart types and their keys" describing its params. Keep this DSL spec in sync with
   `architecture.md` and the chart `spec.md`.
4. `fireflyer/web/app.py` — add at least one example (ideally showing each variation,
   e.g. different `agg` values) to `DEFAULT_YAML` under `charts:` and place it in the
   `dashboard:` layout. Row widths are proportions, not percentages.
5. Declare **`PARAMS`** on the chart class — a `list[Param]`, one per constructor field
   in display order — so the editor's edit modal can build a form for it. Reuse the
   widgets in `fireflyer/params.py`; a sync test asserts `PARAMS` names == constructor
   fields. See **`fireflyer/PARAM_SKILL.md`** for the widget contract. Also add an
   "Editor params" line to the chart's `spec.md`.

If the chart is a **crossfilter source** (clickable, like pie/bar), it also needs a
branch in `dashboard.py`'s `_render_chart` that passes a `crossfilter=` dict and
computes `active` values. Scalar/point charts (number/map/table) skip this.

## Updating an EXISTING chart

- Change `chart.py` / `chart.html` / `chart.css` together with its `spec.md`.
- If you add/rename/remove a param, update the `chat.py` DSL bullet and any
  `DEFAULT_YAML` usage in `app.py`.
- Any render change fails the relevant snapshot — that's the safety net working.

## Tests (snapshot + assertions)

Add `tests/test_<name>.py`. Follow `tests/test_bar.py` / `tests/test_number.py`:
- One `snapshot(chart.to_html())` test per representative variation. The `snapshot`
  fixture (see `tests/conftest.py`) writes `tests/snapshots/<test_name>.html` on first
  run and diffs thereafter. Use the `orders_csv` fixture (seed data in
  `tests/data/orders.csv` — 7 rows).
- Targeted assertions for the data logic (exact aggregated values, filter-before-
  aggregate, param validation errors, and a dashboard round-trip via
  `ff.Dashboard.from_yaml`).

Run:
```bash
python -m pytest tests/test_<name>.py -q          # first run creates snapshots
python -m pytest -q --ignore=tests/test_chat.py   # full suite (test_chat needs the anthropic pkg)
UPDATE_SNAPSHOTS=1 pytest                          # regenerate after an intentional render change — review the diff
```

## Definition of done

- [ ] `fireflyer/chart/<name>/` has `__init__.py`, `chart.py`, `chart.html`,
      `chart.css`, `spec.md`.
- [ ] Wired into `chart/__init__.py`, `dashboard.py` `CHART_TYPES`, `chat.py` DSL,
      `app.py` `DEFAULT_YAML` (with examples).
- [ ] `PARAMS` declared (one per constructor field); sync-guard test passes.
- [ ] `spec.md` matches actual behavior (including its "Editor params" and "Theming" lines).
- [ ] Colors are `var(--ff-*)` tokens (theme blocks copied into `chart.css`); no
      hardcoded colors in CSS or inline SVG; `to_html` takes `theme=` and the root
      renders `data-ff-theme`. Verify by rendering `to_html(theme="dark")`.
- [ ] `tests/test_<name>.py` added; `python -m pytest -q --ignore=tests/test_chat.py`
      is green.
- [ ] No JS in output, templates autoescaped, CSS namespaced, no new abstractions.
