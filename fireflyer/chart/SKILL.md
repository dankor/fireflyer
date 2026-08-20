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

## Tooltips

Every chart's hover tooltip is the same card, in the same place, showing the same
number. They drifted apart once — the pie showed a *formatted* value while the
bar showed an exact one, and the pie's own two tooltips disagreed with each
other — so treat what follows as the contract, not as suggestions.

### What goes in the card

```
┌──────────────────────────────┐
│ 2026-06                      │  head  — which item this is
│ paid                  86,400 │  row   — series (or calc) name + the value
│ Unique crews per sortie      │  desc  — the calc's `description`, when set
└──────────────────────────────┘
```

The **value sits at the right of its row** (`margin-left: auto`), not on a line
of its own — a card that stacks them reads as a different kind of card and
leaves the row's right-hand side conspicuously empty. A chart with no per-item
identity (the number KPI) drops the `head` and leads with the row; it doesn't
invent a heading or move the value elsewhere.

- **The value is `calcs.exact_value(...)`** — unrounded and thousands-separated.
  Never the chart's formatted value. A `format` token abbreviates (`86.4k`)
  because the *chart* is short of space; the tooltip is where someone goes to
  find out what it stood for, so showing the abbreviation there answers nothing.
  Grouping is part of it: it's shown precisely because the digits get read.
- **Don't show both.** An exact line next to a formatted one is one number too
  many; the exact one wins.
- **`description` only when the calc has one** — no empty row otherwise.
- **A percent only when the share is that chart's subject.** The pie shows one
  (share of total is what a pie *means*); the bar doesn't (share of its own bar
  is a second thing to read past on the way to the number).
- **When there's no rich card, the native `title` shows the same exact value.**
  A chart with no `description` falls back to `title="..."`; if that carries the
  formatted number, the same tooltip reports two different figures depending on
  whether someone wrote a description.

`tests/test_calcs.py::test_every_chart_tooltip_uses_the_same_exact_value` renders
all three chart types on one dashboard and pins them together. Extend it when you
add a chart with a tooltip.

**The table follows the same rules, at two levels.** Its **measure cells** carry
the full card — the row's dimension values as the head, then the calc's name with
its exact value, then the description — because a measure cell is formatted by
the calc's `format` token, so `1.9m $` on screen and `1,943,458` in the card is
exactly what the exact-value rule is for. Its **column headers** carry a
description-only card: a header is a label, and a name is an abbreviation like
any other.

**Give a card only where it adds something.** Dimension and raw-row cells get
none: their text is already the full value, so a card would repeat what's on
screen. That's not just tidiness — a table renders up to a thousand rows, and a
card per cell is page weight you can measure.

**Mark what's hoverable when it isn't obvious.** A chart item under the cursor
invites a hover; a column heading doesn't, so described headers get a dotted
underline and `cursor: help`. Don't extend that to value cells — the row already
highlights under the cursor, and underlining every figure in a numeric column
reads as noise.

### Where it sits

**Positioned at the item it describes**, not centred over the canvas.

**Anchor only to a real CSS box.** `anchor-name` on an SVG shape is not a
reliable anchor — an SVG `<rect>`/`<path>` isn't a CSS box — and the failure is
loud rather than graceful: `position: fixed` with `inset: auto` falls back to the
element's *static* position, so a card lands wherever it happens to sit in the
tooltip list instead of near its item. The number KPI anchors to a `<div>`, so it
can use anchoring freely.

A chart that draws in SVG must therefore **carry its own coordinates**, and treat
anchoring as an enhancement on top of a placement that is already correct:

- The **pie** emits px. It can, because its canvas is a fixed 220×220 with a 1:1
  viewBox, so units *are* pixels.
- The **bar** scales, so px are unknowable server-side. It emits each card's
  position as a **share of the viewBox**, placed with `position: absolute` and
  clamped so the card can't leave the canvas.

**Pass that placement as custom properties (`--ff-tip-x`/`--ff-tip-y`), never as
inline `left`/`top`.** An inline declaration outranks every stylesheet rule
without `!important`, so inline coordinates survive the `inset: auto` in the
`@supports` block while its `position: fixed` still applies — the canvas
coordinates then resolve against the **viewport** and the card lands in the
corner of the page. Through a variable the base rule owns `left`/`top`
(`left: var(--ff-tip-x)`) and the anchored block overrides it as an ordinary
cascade. The pie shipped this bug: it was invisible while the block still used
`!important`, and appeared the moment that was removed.

To get anchoring back on top of that, emit a 1px **marker element inside the
SVG**, in a `<foreignObject>` at the item's own coordinates, and anchor to *it*:

```html
<foreignObject x="143.00" y="79.33" width="1" height="1"
  ><div xmlns="http://www.w3.org/1999/xhtml" style="anchor-name: --ff-<n>-s3"></div
></foreignObject>
```

Two things at once. A `<div>` is a real CSS box, so the anchor can't fail to
resolve. And being *inside* the SVG puts it in the **drawing's** coordinate
space: `preserveAspectRatio: meet` letterboxes the drawing inside its canvas, so
a marker placed at a share of the *canvas* drifts from its item by however much
slack the letterboxing left — and that slack changes with every cell size. In
user space there's no drift at any size.

The card then keeps both properties: it lands beside the right item *and*
escapes every clipping ancestor.

Hang it off the edge **nearest where the eye already is**: the bar anchors to its
segment's *top* (`bottom: anchor(top)`), because bar values are labelled above
and anchoring to the bottom threw the card down to the baseline, far from the
part being pointed at.

**Anchor to the shape's bounding box, not to a point on a curve.** The pie
anchored each card to the slice's outer-edge point and offset it sideways — and
cards still landed over the donut, because a circle bulges back out past a point
offset horizontally from its upper or lower arc. Clearing a *point* on a curve
doesn't clear the curve. The anchor is now the donut's bounding-box edge on the
slice's side, level with the slice: the bounding box is the outermost the shape
ever gets, so clearing it clears the chart at every angle.

**Keep both placement paths on one geometry.** A chart has two — the anchored one
and the plain-absolute fallback — and they should put the card in the same place,
differing only in whether clipping ancestors can cut it off. Share the offset
through a custom property (`--ff-tip-gap`) rather than repeating a literal, so a
later tweak can't move one path and leave the other behind. Remember that
`flip-inline`/`flip-block` mirror **inset properties** and can't mirror a
transform, so any offset you want mirrored has to live in `left`/`right`/
`top`/`bottom`.

**Fall back on whichever axis can actually overflow.** A card *centred* on its
item hangs off the side of the screen when the item sits in the first or last
column, and a vertical flip never notices — the overflow isn't vertical. So
match the fallbacks to the placement:

```css
/* Centred on its item: it can overflow either way, so cover both. */
position-try-fallbacks: flip-block, flip-inline, flip-block flip-inline;

/* Hung off one side (the pie): overflow is sideways, so one flip covers it. */
position-try-fallbacks: flip-inline;
```

(The bar predates this and still names five `@position-try` blocks, which pin the
card's near edge to the segment rather than mirroring it — that's the case the
keywords can't express.)

Prefer the `flip-*` keywords to hand-written `@position-try` blocks — they mirror
the primary placement, so there's one geometry to keep right instead of six. Only
write a `@position-try` block when a fallback has to differ from a mirror image
of the original.

**No `!important` in the `@supports` block.** It can stop a `@position-try`
block from applying, and it buys nothing: the `@supports` rule has the same
specificity as the in-flow fallback and comes later, so source order already
wins.

### It must never truncate

A tooltip positioned in-flow (absolute inside the chart) is clipped by any
ancestor with non-visible overflow — the chart card
(`.fireflyer-dashboard-cell > .fireflyer-chart` is `overflow: hidden` to contain
scrolling charts) **and** the editor's output pane (`overflow-y: auto`,
`overflow-x: hidden`). No in-flow placement avoids clipping everywhere, because
the surrounding scroll pane always clips at its own edges.

Solve it with **CSS anchor positioning** (no JS — the hard rule), as a
progressive enhancement:

- **Fallback (any browser):** position the card in-flow (`position: absolute`)
  near its target — fine when nothing clips.
- **Enhancement (`@supports (anchor-name: --a)`):** `position: fixed` +
  `position-anchor` + `anchor()` offsets. `position: fixed` is clipped by no
  overflow ancestor, so it escapes the card *and* the pane. Add the
  `@position-try` fallback via `position-try-fallbacks`. Put `anchor-scope: all`
  on the chart root so anchor names don't leak between charts on one dashboard.
- Reveal is CSS-only too: `.fireflyer-<name>:hover` / `:has(... :hover)` toggles
  the card's `opacity`.
- Cards are **translucent** (`color-mix(... 85%, transparent)` +
  `backdrop-filter: blur`) so they read over content without hiding it.

A plain absolute tooltip that the pane can clip is a bug.

### Testing them

Assert on the **markup**, not on strings that also appear in the stylesheet.
Every chart inlines its CSS into its own output, so `"fireflyer-bar-tooltip" in
html` is true even when no tooltip was rendered — the *rule* is there. Anchor
assertions on an attribute (`class="fireflyer-bar-tooltip"`), or slice the
markup first (`html[html.index('<article class="fireflyer-chart'):]`). The same
trap bites `data-active="0"`, `[data-i]` and any `<svg>`/`<path>` count, since
the dashboard chrome has icons of its own.

**One rule per selector.** A stylesheet is injected verbatim, so a stale rule
left behind by an edit doesn't error — it just wins the cascade if it sits later
in the file, and the chart silently keeps its old look. The bar's stylesheet once
held two `.fireflyer-bar-tooltip` rules with the older one last, so a rebuilt
tooltip rendered with the *previous* design and every test still passed: the
markup was right, and the assertions matched the copy that lost.
`tests/test_chart_css_is_coherent.py` now fails on a duplicated top-level
selector, unbalanced braces, an anchored rule outside its `@supports` guard, or a
translucent tooltip. When a rule seems not to take effect, grep the file for a
second copy of the selector before touching the rule you're looking at.

## Wiring a NEW chart type in (5 edits)

1. `fireflyer/chart/__init__.py` — add `from fireflyer.chart.<name> import <Class> as <name>`
   and append `"<name>"` to `__all__`. This exposes `ff.chart.<name>(...)`.
2. `fireflyer/dashboard.py` — add the import and a `"<name>": <Class>` entry to the
   `CHART_TYPES` dict (keep it a plain dict — no registry).
3. `fireflyer/web/chat.py` — add `<name>` to the `type:` union line and a bullet under
   "Chart types and their keys" describing its params. Keep this DSL spec in sync with
   `architecture.md` and the chart `spec.md`.
4. `fireflyer/web/app.py` — add at least one example (ideally showing each variation,
   e.g. different `calc` references) to `DEFAULT_YAML` under `charts:`, define any
   calcs it needs under the top-level `calcs:` block, and place it in the
   `layout:` layout. Row widths are proportions, not percentages.
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
