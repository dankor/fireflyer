# Fireflyer MVP Architecture

## Primary Goal

Fireflyer is a Python library for rapidly transforming CSV files into HTML visualizations.

The primary goal is developer experience.

Given a CSV file and a few lines of Python code, a developer should be able to immediately see a visualization in the browser.

This is an MVP.

The goal is not:

* scalability
* enterprise architecture
* plugin systems
* production readiness

When in doubt:

1. Prefer less code.
2. Prefer fewer abstractions.
3. Prefer hardcoded behavior.
4. Prefer implementation speed over flexibility.
5. Prefer developer experience over architecture purity.

The MVP is expected to be rewritten.

---

# Vision

Fireflyer provides a simple way to visualize CSV files using Python.

Example:

```python
import fireflyer as ff

chart = ff.chart.table(
    dataset="files/orders.csv",
    title="Orders",
)

chart
```

The user writes Python code.

Fireflyer reads the CSV file, generates HTML, and displays the result.

The user does not interact with Polars directly.

Polars is an internal implementation detail.

---

# Scope

Supported in MVP:

* CSV upload → Parquet datasets (in object storage)
* Table chart
* Pie chart
* Dashboards (see Dashboard Layout DSL below)
* FastAPI application
* Browser-based code editor
* HTML rendering
* PicoCSS styling
* Docker development environment

Not supported:

* Authentication
* Authorization
* Multiple users
* SQL
* Data warehouses
* Joins
* Calculated columns
* Caching
* Plugins
* Streaming
* Large dataset optimization
* Realtime updates

Two **owner-approved exceptions** to this list: **Portal mode** (below) adds
persistence + a multi-dashboard listing, scoped to `web/`; and **datasets** are
Parquet in object storage with efficient (pushdown) scans — so "only CSV" and
"large-dataset optimization" no longer hold for the dataset layer. Neither
relaxes the rest: no auth beyond the portal's simple login, no multi-user, no
caching, no SQL/joins/calculated columns.

---

# Core Flow

```text
CSV
 ↓
DataFrame
 ↓
Chart
 ↓
HTML
 ↓
Browser
```

The implementation should remain as close to this flow as possible.

Avoid introducing additional layers unless absolutely necessary.

---

# Charts

A chart describes how a CSV file should be visualized.

Examples:

```python
ff.chart.table(...)
```

```python
ff.chart.pie(...)
```

All charts must have:

* type
* title

Common optional parameter on every chart: `filters` (see Filters below).

Additional parameters are chart-specific.

Examples:

```python
ff.chart.table(
    dataset="files/orders.csv",
    title="Orders",
)
```

```python
ff.chart.pie(
    dataset="files/orders.csv",
    title="Orders by Status",
    column="status",
)
```

Charts are declarations.

A chart does not cache data.

Whenever a chart is rendered:

1. Read CSV.
2. Execute chart logic.
3. Generate HTML.
4. Return HTML.

---

# Chart Specs

Each chart has its own spec file: `fireflyer/chart/<name>/spec.md`.

The spec is the source of truth for what the chart does. It is short — Purpose and Behavior sections only. Anything more detailed lives in the code.

This file lists which charts exist; it does not duplicate their behavior:

* `fireflyer/chart/table/spec.md` — table chart
* `fireflyer/chart/pie/spec.md` — pie chart

When adding a chart, write its `.md` first, then the code. When changing a chart's behavior, update the `.md` in the same commit.

---

# Filters

Every chart accepts an optional `filters` parameter. Filters narrow the chart's data before chart logic runs.

A filter is a small declarative shape:

* `column` — the dataset column to filter on.
* `op` — one of `in`, `ni` (not-in).
* `values` — a list of values to compare against.

`filters` is a list of these; all must match (AND) for a row to pass. There is no `or`, no nesting, no range operators in the MVP.

## Declared filters

In Python:

```python
ff.chart.table(
    dataset="files/orders.csv",
    title="Open orders",
    filters=[
        {"column": "status", "op": "in", "values": ["open", "pending"]},
    ],
)
```

In YAML, the same shape:

```yaml
charts:
  open_orders:
    type: table
    dataset: orders
    title: "Open orders"
    filters:
      - column: status
        op: in
        values: [open, pending]
```

## Crossfiltering

In a dashboard, clicking a chart element (e.g. a pie slice) emits a filter — `{column, op: in, values: [<clicked value>]}` — that is applied to every other chart on the page. This is the only built-in dashboard interaction in the MVP.

Rules:

* Clicking a slice sets that chart's crossfilter on its column. Clicking the same slice again clears it.
* Active crossfilters merge with each chart's declared `filters` by AND. A chart's declared filters are never removed by interaction.
* The emitting chart is exempt from its own crossfilter. The chart that produced the click keeps showing every category with the clicked one visually selected; only other charts apply the filter to their data. (a common crossfilter convention — clicking a slice doesn't reduce the source chart to a single slice.)
* A crossfilter applies to another chart only if that chart's dataset has a column with the same name. Charts without that column ignore it.
* Crossfilter state lives in the dashboard URL as query params — shareable, htmx-friendly, no server-side session.

## One model, two entry points

The Python/YAML `filters` field and the dashboard click path produce the same filter shape and flow through the same single application step before chart logic runs. There is one filter model, used two ways.

---

# Dashboards

A dashboard is a single YAML file. It declares everything it needs: the datasets it reads, the charts it composes from those datasets, and the layout that arranges those charts on a page. One file is the deployable unit.

The Python API (`ff.chart.table(...)`, `ff.chart.pie(...)`) stays for ad-hoc rendering in the web editor. Dashboards are the saved, shareable form.

## File shape

A dashboard YAML has three top-level sections:

```yaml
name: <string>

charts:
  <id>: <chart config>

dashboard:
  - <layout item>
  - <layout item>
```

* `name` — required. A short human-readable title for the whole dashboard,
  part of the definition. `Dashboard.from_yaml` rejects a missing or empty
  `name`; **Portal mode** lists dashboards by it.
* `charts` — mapping of chart id → chart config.
* `dashboard` — the page layout (the layout DSL, below). Either a flat list of
  layout items, or a mapping of tab name → layout list (see **Tabs**).

Chart ids are local to the file. There is no cross-file inclusion in the MVP.

## Datasets

Datasets are **managed, named entities** — not inline paths. A CSV is uploaded,
converted to **Parquet**, and stored in an object store; a chart references a
dataset by its **unique name** (`dataset: orders`). There is **no `datasets:`
block** in the dashboard YAML.

`Dashboard.from_yaml(text, datasets=<store>)` resolves each chart's dataset name
to its Parquet `(uri, storage_options)` at render time; charts read via lazy
`scan_parquet` with projection + predicate pushdown, so only the columns and
row-groups a chart needs are read. Without a resolver (standalone use, tests), a
chart's `dataset` is taken as a Parquet path/URI directly.

The dataset entity (Parquet + a small YAML metadata sidecar: schema, row count,
description, delimiter, author) lives in an `ObjectStore` — a local folder for
dev/tests, S3-compatible **Garage** for portal runtime. Managed in the portal's
**Datasets** gallery tab (upload / preview / rename-with-cascade /
guarded-delete). See **Portal mode** and `fireflyer/datasets.py` /
`fireflyer/storage.py`.

## Charts section

A chart references a dataset by id and carries chart-specific parameters:

```yaml
charts:
  orders_table:
    type: table
    dataset: orders
    title: "Orders"
    search: true
    pagination: 5

  status_pie:
    type: pie
    dataset: orders
    title: "Orders by Status"
    column: status
```

* `type` selects the chart implementation (e.g. `table`, `pie`).
* `dataset` is a dataset **name** (resolved to its stored Parquet at render time; see **Datasets**).
* Remaining keys map directly to that chart's Python constructor arguments. A chart's YAML schema is its constructor — no extra translation layer.

## Chart params & editor modal

The YAML above is authoritative, but the browser editor can also edit a chart's
config through a form. Each chart hovered in the editor shows a **pencil button**
(only while the YAML pane is open); clicking it opens a **modal** whose fields are
the same config, rendered as widgets.

The widgets are a small shared abstraction — the **only** one the project permits
against its otherwise anti-abstraction stance, because it's what makes the editor
usable and it's confined to editor support:

* **Param classes** (`fireflyer/params.py`) — `TextParam`, `DatasetParam`,
  `ColumnParam`, `ChoiceParam`, `IntParam`, `BoolParam`, `FilterListParam`. A
  `Param` renders an autoescaped input, parses the submitted value, and emits it
  back to YAML. New widget kinds are added here once and reused.
* **A chart declares `PARAMS`** — a `list[Param]`, one per constructor field, in
  display order. The chart's YAML schema is still its constructor; `PARAMS` is the
  *editor view* of that schema. A test keeps the two in lockstep. The modal also
  offers a **chart-type dropdown**: changing it re-renders the form for the new
  type (carrying over overlapping values), and saving rewrites the block as that
  type — so a chart can be re-typed from the modal, not just reconfigured.
* **Saving is a surgical edit** (`fireflyer/config_edit.py`): only the edited
  chart's YAML block is rewritten; every other chart, comment, and blank line is
  preserved byte-for-byte. Comments inside the edited block are regenerated. The
  whole document is re-validated with `Dashboard.from_yaml` before it's accepted.

New layout items are added the same way: hovering the editor's left gutter
reveals **"+" buttons**. The per-row one adds a chart to that row (the add
modal). The insert-strip one opens a small **chart / header / separator** menu —
chart opens the add modal (a unique id is generated, the block appended, and a
placement spliced into the layout), while header and separator are inserted
directly (a header defaults to "New header").

**Headers and separators also carry the hover toolbar** (a compact badge in their
top-right corner) with **move**, **edit**, and **delete** — the same buttons as a
chart, addressed by their layout-item index instead of a chart id
(`config_edit.move_layout_item` / `delete_layout_item`, routes
`/chart/config/move-item` and `/chart/config/delete-item`). The badge is **centred
on the top edge** of the item. **Edit** is header-only: it opens the same
inline-rename (double-clicking a header still triggers it), and mirrors move
mode's focus feel — the rest of the dashboard **dims**, its hover affordances are
suppressed, and the **same topbar cancel button** appears. Enter (or blur) saves;
Esc or the cancel button restores the original. A **separator has no edit button**
(nothing to edit), and its wrapper carries extra padding so the thin `<hr>` is an
easy hover target.
**Move** for a header or separator is restricted to **between-rows** placements —
it reuses the between-row strips (rule 4) as its only drop targets, with no
side/merge/unmerge zones, since a header or separator never lives inside a grid.
The two strips flanking the moved item are **hidden** during its move (they'd drop
it back where it already is).

Charts are **rearranged via a move mode**: a chart's move button lights it, dims
the rest, and disables every other interaction, then lights up every valid drop
spot as a blue box (the hovered one previews the placement). The drop zones follow
a fixed set of rules:

1. **Side zones** — every chart has a drop zone on its **left and right edge** (a
   before/after single-row insert).
2. **Common borders are one zone** — where two **non-merged** charts share an edge,
   the two candidate zones collapse into a single drop (no duplicate). Borders
   involving a merged chart are *not* collapsed (see 4).
3. **Merged charts** — a chart that spans rows offers its side zones at **full
   height, both sides**; dropping there makes the moved chart adopt the span. Its
   side always survives dedup and sits next to a neighbour's single-row zone.
4. **Between-rows** — every gap between rows is a drop zone (drop into a new row
   there, including around headers/separators).
5. **Merge-down** — the chart **being moved** gets a single extra zone: a long bar
   down its **centre** into the one row below, shown only if a chart-row sits
   directly below it. Dropping grows *that chart's own* span **down one row**
   (current rows + 1). Only the moved chart, only downward, only one per dashboard.
6. **Unmerge** — if the chart **being moved is itself merged**, it keeps a drop
   zone on **each row it spans** (its shared borders with neighbours, one per
   row). Dropping there puts it back **single-row in that row** — the span is
   removed. This overrides rule 7 for a merged moved chart.
7. **The moved chart otherwise has no side zones** — neither its own edges nor the
   borders it shares with neighbours (they'd be no-ops).
8. **No overlaps** — zones sit next to each other, never on top.

Rule 3's adopt inserts the chart into every row of the span (sized in the first,
bare in the rest). Rule 5's merge-down adds a bare occurrence in the row below, so
the chart inherits and spans one more row. A source row left empty by a move is
dropped.

A chart is removed via the toolbar's **delete** button, which shows a confirm
dialog and then strips the chart's block plus every layout placement (rows left
empty are dropped). A **header or separator** deletes the same way (confirm
dialog), removing just its one layout line.

The toolbar (move + edit + delete), "+" buttons, modal, and their JS are
**editor-only** and gated so they never appear in a deployed `to_html()` render —
same status as the rest of `web/`.

## Dashboard layout DSL

The `dashboard:` section is a **flat YAML list**. Each item is one of:

* **Row** — a YAML array. First element is the row height (`"@<units>"`), remaining elements are widget placements (`"<chart_id>:<width>"`, where width is a relative proportion).
* **Header** — a plain string. Rendered as a full-width section title; not part of the layout grid.
* **Separator** — the string `"-"`. A visual divider between sections; no layout semantics.

There are no other item kinds. There is no nesting.

### Row syntax

```yaml
- ["@<height>", "<chart_id>", "<chart_id>:<width>", ...]
```

* `@<height>` — the row's height in layout units. The rendering engine maps these units to pixels, CSS grid rows, or whatever sizing system it uses.
* `<chart_id>` or `<chart_id>:<width>` — a chart id with an **optional** width. The id MUST exist in the `charts:` section. The width is a **relative proportion** and defaults to `1` when omitted, so `orders` == `orders:1`. `["@40", "a", "b", "c"]` splits the row into equal thirds; `a:1 b:4` is the same 20/80 split as `a:20 b:80`. Widths are rendered as CSS `fr` tracks, so the columns always fill the row exactly.

### Spans (bare-inherit merges)

A chart spans multiple rows by being **sized in one row and repeated bare** (no width) in the row(s) directly below:

```yaml
- ["@40", "orders:3", "status:2"]   # first row sets the sizes
- ["@30", "by_day", "status"]       # `status` bare -> inherits its column, spans down
```

* The **first row of a span group owns the column sizes.** A bare cell equal to the chart directly above inherits that column and extends the span; every **other** cell in a lower row fills the **leftover** width, splitting it among such cells by their own proportions.
* Because a lower row's cells can be finer than the first row's, the grid is the **union** of every row's column edges: a cell covers the fine columns its range spans. This yields both vertical (row) spans and horizontal (column) spans — e.g. `by_day` above stretches across the two columns `orders`/`status` split, while `status` spans both rows.

### Validation rules

* All heights and any **given** widths MUST be greater than zero. Width is optional (defaults to 1); there is **no** sum requirement.
* The first element of a row MUST be a height token (`"@..."`); the rest MUST be widget tokens (`"<id>"` or `"<id>:<width>"`).
* Every chart id referenced in the layout MUST be declared in `charts:`. A chart's `dataset` names a managed dataset, resolved (to Parquet) at render time.
* A chart id MAY appear more than once ONLY as one **contiguous bare-inherit span** — sized once, then bare in the immediately following row(s) with no header/separator between. Repeating an id **with** a width, across a header/separator, skipping a row, or twice in one row is invalid.

### Rendering model

* Rows render top-to-bottom in document order.
* A run of consecutive rows linked by a bare-inherit span renders as one CSS grid whose columns are the union of the rows' edges; unlinked rows render as independent grids.
* Headers and separators sit between rows and do not participate in any grid, and they break a span.
* Rendering is deterministic — the same YAML always produces the same HTML.

### Tabs

The `dashboard:` section may be either the **flat list** above or a **mapping of
tab name → layout list**. The mapping form splits the page into tabs; each value
is exactly the same layout-list DSL (rows, headers, separators, spans):

```yaml
dashboard:
  Overview:
    - ["@22", "total", "revenue"]
    - ["@40", "orders:3", "status:2"]
  All orders:
    - ["@50", "orders_long"]
```

* A flat list is the no-tabs form and renders exactly as before — tabs are purely
  additive and backward-compatible.
* A tab is a **section delimiter**: it owns every row from its key down to the
  next tab key. Row ordinals, header indices, and item indices are numbered
  **globally in document order across tabs**, so the same layout rules and the
  editor's line-addressing apply unchanged.
* Each tab must contain at least one layout item (no empty tabs). A chart still
  resolves to exactly **one** placement across the whole dashboard — a span may
  not cross a tab boundary, and a chart id may not repeat in two tabs.
* Only the **active tab** is rendered; switching re-fetches the dashboard so a
  tab's charts load lazily (htmx). Crossfilters are **global** — a click filters
  matching charts in every tab. The active tab rides in hidden state so a
  crossfilter click or an edit keeps the current tab. The tab bar is sticky.

Editor gestures (editor-only, gated by `editing`):

* **Add** — the between-rows **"+"** menu always offers a **Tab** item. On a flat
  dashboard the first pick enables tabs by wrapping the whole layout in one tab.
  Once tabbed, a pick splits the current tab at that gap — rows below become the
  new tab, rows above stay in the previous one. Either way the new tab opens a
  **forced rename**: give it a real name, or cancel (Esc / ✕ / blur / keeping the
  default) which **undoes the add**.
* **Rename** — inline, exactly like a header (focus mode, Enter/Esc).
* **Move** — drop the tab into any between-rows slot (reuses the between-row
  strips, like a header/separator move); it repositions the tab's boundary,
  reordering the tabs and reassigning the rows that fall under it. You can
  **switch tabs during the move** (the move stays live, like a cross-tab chart
  move) to reach a row in another tab. The **first tab has no move** — moving its
  boundary would orphan the rows above it.
* **Delete** — a non-first tab merges its rows into the previous tab; deleting the
  **first** tab dissolves **all** tabs back to a flat list (the confirm lists the
  tabs being removed). The first tab's delete button carries a distinct
  "remove all tabs" icon to signal it flattens the whole dashboard.
* **Move a chart across tabs** — enter move mode on a chart, switch tabs (the move
  stays live), and drop it into the destination tab's zones. A move that empties a
  tab dissolves that tab.

Surgical support lives in `config_edit.py` (`add_first_tab`, `insert_tab`,
`set_tab_text`, `move_tab`, `delete_tab`), each a line edit on the mapping
re-validated through `Dashboard.from_yaml`.

## Complete example

```yaml
name: Orders overview

charts:
  orders_table:
    type: table
    dataset: orders
    title: "Orders"

  status_pie:
    type: pie
    dataset: orders
    title: "Orders by Status"
    column: status

dashboard:
  - Overview
  - ["@40", "orders_table:60", "status_pie:40"]

  - "-"

  - Detail
  - ["@30", "orders_table:100"]
```

## Implementation guidance

* Parse the YAML once, then validate in three passes: `datasets` → `charts` (resolving dataset refs) → `dashboard` (resolving chart refs, checking given widths are positive and every repeated id forms one contiguous bare-inherit span). A later pass should never need to look back.
* Classify each layout item by shape: array → row, `"-"` → separator, other string → header. Reject anything else with the offending index in the error.
* Build each chart by passing the YAML chart config straight into the chart's Python constructor. No registry; a small `type` → constructor lookup in the loader is enough.
* Emit one CSS-grid (or equivalent) block per row; widths become column tracks, height becomes the row track.
* Keep it boring — no plugin system for new item kinds, no templating inside YAML values, no calculated widths, no responsive breakpoints. If the MVP needs more, add it explicitly.

---

# Rendering

Charts generate HTML.

HTML should be simple and easy to inspect.

The exact HTML structure is an implementation detail.

Use PicoCSS-compatible markup whenever possible.

Avoid introducing frontend build tools.

Do not use:

* npm
* webpack
* vite
* tailwind
* bootstrap

SVG may be used for visual charts.

## Server-rendering, htmx-only

Charts are server-rendered. The browser sees HTML, SVG, CSS, and htmx attributes — nothing else. No Alpine, Stimulus, React, Vue, or hand-written JS in chart output. The only `<script>` tag a chart's host page should load is htmx itself.

Interactivity that needs no server roundtrip (hover, tooltips, row highlight) is built with CSS — `:hover`, `:has()`, attribute selectors, transitions. If a chart needs a piece of dynamic CSS (e.g. one rule per data row), the chart's template generates it inline before the markup. Browsers that support `:has()` are Chrome 105+, Firefox 121+, Safari 15.4+; we do not target older browsers.

Interactivity that needs to re-read data (search, pagination, change of facet) is built with htmx:

* The chart embeds a plain `<form>`, `<input>`, or `<a>` with `hx-get` / `hx-target` / `hx-swap` attributes pointing at its endpoint.
* The endpoint takes the chart's identifying parameters plus display state (e.g. `?q=foo&page=2`) as query params, constructs the chart, and returns an HTML fragment.
* The fragment replaces the chart's outer container in place. No iframe, no full-page navigation.

This is reserved for display state intrinsic to the chart (which page, which filter). Anything that changes the chart's *definition* is still a code edit handled by the web editor's execute button.

## Templates

HTML is produced by Jinja2 templates, not by Python f-strings.

* One `chart.html` template inside each chart's folder (e.g. `fireflyer/chart/table/chart.html`).
* Each chart module computes data (Polars work, slice geometry, etc.) and passes it to its template.
* Templates use autoescape so callers cannot inject HTML through CSV values or chart parameters.

Why Jinja2:

* The template reads as HTML, which is what we are producing.
* Autoescape removes scattered manual `html.escape(...)` calls.
* It is a single, well-known dependency. No custom mini-template language.

Anti-patterns to avoid:

* No template inheritance, no `base.html`, no macros, no custom filters until a second chart actually needs them.
* No template registry or loader abstraction. One Jinja `Environment` constructed at module import is enough.

## Styling

CSS is per-chart, not shared. Each chart owns a `chart.css` file alongside its `chart.py` inside the chart's folder (e.g. `chart/table/chart.css`, `chart/pie/chart.css`).

* Each chart's CSS file is read once at module import and injected into that chart's output inside a `<style>` tag.
* Each stylesheet is self-contained — it includes whatever card chrome, title, and chart-specific rules the chart needs. Rules are namespaced under a chart-specific class (e.g. `.fireflyer-table`, `.fireflyer-pie`) so duplicate base rules across charts collide harmlessly when several charts render on the same page.
* No shared base stylesheet, no CSS `@import`, no build step, no preprocessor, no asset pipeline. Edit the `.css` file directly.

The tradeoff is intentional: a small amount of duplicated CSS in exchange for each chart being self-contained and independently removable.

### Theming

Fireflyer ships a **light and a dark palette**. Colors are never hardcoded in rules — they resolve from `--ff-*` CSS custom properties (`--ff-ink`, `--ff-panel`, `--ff-border`, `--ff-accent`, `--ff-muted`, …).

* **Selection is automatic.** With no override, output follows the viewer's OS via `@media (prefers-color-scheme: dark)`.
* **Override with `data-ff-theme="light|dark"`.** The attribute may sit on the element itself or on any ancestor (the dashboard root, or `<html>` in the editor). The explicit-override selectors carry higher specificity than the media block, so a forced theme always beats the OS preference. `Dashboard.to_html(...)` and every chart's `to_html(...)` accept `theme="dark"|"light"`; omitted (or `"auto"`) emits no attribute.
* **The token blocks live per file.** Each `chart.css` and `dashboard.css` carries its own copy of the four token blocks — base (light), `@media` (auto-dark), and two `[data-ff-theme]` overrides (self + ancestor) — scoped to its root class (`.fireflyer-chart` / `.fireflyer-dashboard`). This duplicates the palette values across files, the same deliberate cost as the no-shared-stylesheet rule above. The two palettes must be kept in sync when a token changes.
* **Fixed (theme-independent) elements.** The categorical data palette (pie slices, bar segments, map hex fill) stays constant so a value keeps its color in either mode. The map's basemap is always a light OSM raster, so its hex overlay — accent fill, white separator strokes, dark count labels — is not themed.
* **`color-scheme`** is set on the dark blocks (and the editor `<html>`) so native controls — `<select>` dropdowns, the table's scrollbar — render dark too. It inherits, so a themed dashboard covers its nested charts.
* **Editor.** A topbar **Theme: Auto / Light / Dark** toggle sets `data-ff-theme` on `<html>` (persisted in `localStorage`), theming the editor chrome, the dashboard preview, and every chart at once.

---

# Web Editor

The editor exists only to improve the development experience.

It is not part of the Fireflyer core architecture.

The editor is a temporary development tool.

Purpose:

* write Python code
* execute code
* immediately see generated HTML

Layout:

```text
+-------------------+-------------------+
|                   |                   |
|    Python Code    |   Visualization   |
|                   |                   |
+-------------------+-------------------+
```

Execution is triggered by a button.

No notebook model.

No execution history.

No autosave requirements.

No realtime execution.

---

# Portal mode

Portal mode is an **opt-in** way to store many dashboards in a database and
browse them from a gallery, instead of hand-editing one YAML file. It is an
**owner-approved exception** to the "no persistence / no multiple dashboards"
anti-goal, and — like the editor and the AI assistant — it is **editor-only**,
scoped to `web/`. It is not part of the Fireflyer core. It does not add
authentication, multiple *users*, or caching; those stay out of scope.

## Enabling it

Off by default: `python -m fireflyer.web` serves the usual single-dashboard
editor at `/`. Portal mode is switched on by the `FIREFLYER_PORTAL` environment
variable; the `python -m fireflyer.portal` entrypoint sets it, reads runtime
config from `portal.yaml` (title, database url; the environment overrides the
file), and binds `0.0.0.0` for containers. With it on, `/` becomes the gallery.

## It reuses the editor unchanged

The editor is already **stateless**: every edit route (`/execute`,
`/dashboard`, `/chart/config/*`) takes the current YAML text in from the browser
and returns new YAML — nothing is persisted server-side. Portal mode only wraps
this with a persistence and listing layer, so **no existing edit logic changes**.

New routes, all gated behind the portal flag and addressing dashboards by
**UUID**:

* `GET /` — the gallery: a **table** of stored dashboards (name, author, last
  updated) with per-row **Edit / Clone / Remove** actions and a **+ New
  dashboard** button. New and Clone each prompt for a name in a small modal
  (native `<dialog>`).
* `POST /new` — create a **blank** dashboard with the given name (a valid but
  empty YAML: no datasets, charts, or layout) and open it.
* `POST /d/{id}/clone` — copy an existing dashboard under a new name and open
  the copy.
* `GET /d/{id}` — the normal editor page, seeded with that dashboard's stored
  YAML and given a **Save** button (and a link back to the gallery).
* `POST /d/{id}/save` — validate and persist the edited YAML.
* `POST /d/{id}/delete` — remove it.

## Storage model

A dashboard is stored as an **opaque YAML text blob** — the same YAML the editor
already produces — never decomposed into normalized tables. Decomposing it would
break the surgical, comment-preserving block edits `config_edit.py` relies on.
On every write the backend validates the YAML with `Dashboard.from_yaml`; an
invalid dashboard is rejected and nothing is stored.

The dashboard's **name** is stored in its own column but is the source-of-truth
top-level `name:` key (see **File shape**): the store re-derives the column from
the YAML on every save, so renaming is just editing the `name:` key in the
editor, and New/Clone write the modal-provided name into the YAML. **Author** is
separate metadata (not in the YAML) — the logged-in user, set once at
create/clone and left untouched by saves. Datasets are **not** in the YAML:
charts reference them by name and they live in the dataset store (see the
**Datasets** section), so a dashboard blob is self-contained layout only.

## Two stores, one schema

The store lives in `fireflyer/web/portal.py` behind two interchangeable
backends over one small table (`id, name, author, yaml, created_at,
updated_at`):

* **sqlite** (stdlib) — powers local dev (a file) and the test suite
  (in-memory). No service, no driver.
* **Postgres** (`psycopg`) — the runtime backend for
  `python -m fireflyer.portal`, selected when a `DATABASE_URL` is present.

The Postgres driver is an optional `.[portal]` extra and is imported lazily, so
the core install and `pip install -e ".[test]"` never require a database. Store
logic and the gallery HTML are pure functions in `portal.py` (not `app.py`), so
they unit-test without the web stack — the tests use in-memory sqlite and never
touch a live database, the same rule the AI-assistant tests follow.

## Datasets tab

The gallery's overview pages lead with a **Dashboards | Datasets** switch. A
selected dataset is a **detail** page led by a **back button** (no switch — that's
for the list pages); in local paths mode it, like the dashboard editor, carries
the path dropdown on the right, so the selected-item pages are consistent (portal
detail is back-only).
The Datasets tab manages datasets (see the
**Datasets** section above): upload a CSV (name, description, delimiter) →
converted to Parquet and stored; a detail view shows the schema (per-column type
icon) and a data preview. A dataset **can't be
removed** while a dashboard references it (the trash shows the count and lists
those dashboards), and **renaming cascades** — every dashboard's `dataset:`
refs are rewritten (`Dashboard.dataset_names()` + `rename_dataset_ref()`). The
`DatasetStore` (`datasets.py`) sits over an `ObjectStore` (`storage.py`): a
local folder for dev/tests, S3-compatible **Garage** at runtime
(`FIREFLYER_S3_ENDPOINT`, `boto3` in the `.[portal]` extra, imported lazily).
`app.state.datasets` resolves dataset names in the render routes; the store is
unit-tested with a local folder, never a live object store.

## Authentication

Portal mode is gated behind a login (`fireflyer/web/auth.py`). The default is a
single hardcoded user — **admin / admin**, overridable with `FIREFLYER_USER` /
`FIREFLYER_PASSWORD`. This is intentionally minimal; it is *not* hardened
production auth. It exists so the portal isn't wide open, and so richer schemes
have a clean place to plug in. Auth is on whenever portal mode is; local
single-dashboard mode has no login.

The design is built from **two independent seams**, and that separation is the
whole point — it's what makes advanced schemes easy:

1. **Who is allowed in** — the `Authenticator` protocol. Its one method,
   `verify(username, password) -> identity | None`, is the credential check.
   The default `PasswordAuthenticator` compares against the configured user.
   Swap it for anything — an LDAP bind, a database user table, an API-key
   lookup — by implementing that single method and setting
   `app.state.authenticator`.
2. **How the identity is remembered** — a session in an HMAC-signed cookie
   (`set_session` / `current_user` / `clear_session`), signed with
   `FIREFLYER_SECRET`. This is completely independent of *how* the identity was
   proven.

A single middleware guard redirects any unauthenticated request to `/login`;
`GET/POST /login` and `POST /logout` are the only auth routes. The topbar shows
the signed-in username and a **Log out** button, in both the gallery and the
editor.

### Adding SSO / OAuth (not implemented — the recipe)

Because "how you remember the identity" is separate from "how you proved it," an
external-IdP flow (Google, Okta, SAML, OIDC…) slots in **without touching the
portal routes or the guard**:

1. Add a provider route pair — e.g. `GET /login/oauth` that redirects to the
   IdP, and `GET /auth/callback` that exchanges the code and verifies the token.
2. In the callback, on success call `set_session(response, <identity>)` — the
   same session layer the password flow already uses.
3. Point the login page's button at your provider route instead of (or
   alongside) the password form.

The guard, logout, `current_user`, and every portal route stay exactly as they
are: they only ever ask "is there a valid session?", never "how did you log
in?". Enterprise concerns beyond this seam — per-user ownership/sharing (extend
the store's rows with the identity `current_user` returns), roles, token
refresh — are deliberately out of scope for the MVP; the seams are here so they
can be added without a rewrite.

## Local paths mode (no database)

Portal mode needs a database. **Local paths mode** (`fireflyer/web/paths.py`) is
the *non-portal* way to manage many dashboards + datasets — against your own
filesystem, with **no DB and no login**. It's for the intended local workflow:
you **develop a folder of dashboards on your machine**, then later push them to a
server with the command-line tool / API (both to come). That upload covers
**dashboards only** — datasets are managed on the server side (each environment
owns its own data), and a dashboard just references them by name, so the same
YAML works locally and remotely.

Turn it on with **`FIREFLYER_PATHS`** — a base directory inside the container.
**Each host folder you Docker-map under that base is a switchable "path"** — a
labelled **path dropdown** on the **right** of the topbar, present on the gallery,
the dataset detail, and the dashboard editor (the Dashboards | Datasets switch is
on the left, but **only on the list pages** — selected items lead with just a
back button); there is no
filesystem browser and no in-app path management — you add / remove / repoint a
path by **editing the compose volume mappings and restarting** (deliberately the
only mechanism — it keeps the mapping explicit and host-controlled). Unset → `/`
stays the single-dashboard editor; set → `/` and `/datasets` become the same
gallery and editor as portal, minus auth. The active path rides in an `ff_path` cookie
(`GET /path/{name}` sets it, validated against the mapped-folder list).

* A path's **dashboards are `<path>/dashboards/*.yaml`** — files you own and
  commit — via `PathDashboardStore`, which mirrors the portal store surface
  (`list/get/create/save/delete` over `DashboardRow`) so **every route is
  store-agnostic**. A dashboard's **id is its filename stem** (slugged from the
  `name:` key, path-traversal guarded).
* A path's **datasets are an isolated blob store per path** at
  `<FIREFLYER_DATA>/<path>/` — Parquet you upload through the web, kept out of
  your dashboard files (they're not part of what you commit or later upload).
* On first run a **`demo` path is seeded** so the gallery opens on a working
  example: the starter dashboard in `demo/dashboards/`, plus the `orders`
  dataset it references in the demo path's blob store. Seeding is best-effort and
  non-destructive — the dashboard is written only when the path is brand new, so
  it never clobbers your edits.

`app.py` stays mode-agnostic through request-scoped `_dash_store(request)` /
`_dataset_store(request)` helpers: portal → the DB store / dataset singleton;
paths mode → the active path's `PathDashboardStore` / per-path blob store.
`paths.py` logic is unit-tested without the web stack (`tests/test_paths.py`).

---

# Testing

Tests are snapshot-based.

Each test contains:

* input CSV
* chart definition
* expected HTML output

Suggested structure:

```text
tests/
├── data/
│   ├── orders.csv
│   └── users.csv
│
├── table/
│   └── snapshots/
│
└── pie/
    └── snapshots/
```

The goal of tests is to verify generated HTML.

---

# Dashboards as code (GitOps)

A dashboard is a plain YAML file — self-contained layout that references datasets
**by name** and carries no data. That makes dashboards **code you can version and
deploy like anything else**, and it's the whole point of local paths mode: you
author a path of dashboards locally (browser editor, AI assistant, or by hand),
keep it in **your own git repo**, and review changes as diffs and pull requests.

The intended deployment path (a command-line tool + API, both to come) lets
anyone build a **GitOps workflow** around that folder:

* **Author** locally in paths mode (`FIREFLYER_PATHS`) and commit the
  `dashboards/*.yaml` to your repo.
* **Validate in CI** — the same `Dashboard.from_yaml` check the editor runs on
  every save is a cheap "does it parse and lay out?" gate for a pull request.
* **Deploy on merge** — the tool pushes the folder's dashboards to a running
  Fireflyer instance (a portal, or any target environment). Each dashboard is
  keyed by its slug/name, so a deploy is an idempotent upsert.

**Datasets are intentionally out of this loop.** They live on the *target*
environment (each server owns its own data — the per-path blob store is the
authoring-side equivalent), and dashboards reference them by name, so the same
YAML deploys unchanged to any environment that already has those datasets. The
deploy tool therefore ships **dashboards only**; data isn't something you commit
or push through this path.

This is deliberately *your* pipeline, not Fireflyer's: the tool provides the
folder format and the upsert, and you wire whatever git host / CI you like around
it.

---

# Project Structure

Suggested MVP structure:

```text
fireflyer/
├── chart/
│   ├── __init__.py
│   ├── table/
│   │   ├── __init__.py
│   │   ├── chart.py
│   │   ├── chart.html
│   │   ├── chart.css
│   │   └── spec.md
│   └── pie/
│       ├── __init__.py
│       ├── chart.py
│       ├── chart.html
│       ├── chart.css
│       └── spec.md
│
├── web/
│
└── tests/
```

Each chart lives in its own folder. The four files (`chart.py`, `chart.html`, `chart.css`, `spec.md`) co-locate inside it. No shared `templates/`, `styles/`, or `docs/` subdirectories — the chart folder is the modularity boundary.

Keep the structure simple.

Avoid creating:

* service layers
* repositories
* registries
* plugin frameworks
* dependency injection systems

until they are actually needed.

---

# Code Style

Code should be approachable to a reader who has never seen the project before. Every chart module should read top-to-bottom like a story: constants, helpers, then the chart class.

Rules:

* **Small functions.** If a method grows past one screen, lift a piece out into a clearly named helper in the same file. Helpers stay private (`_name`) and live next to the code that uses them — no `utils.py`, no cross-file plumbing.
* **Named locals over dense expressions.** A two-line list comprehension is fine. A four-line one is a helper or a regular `for` loop.
* **Comments explain *why*, not *what*.** Code with clear names already says what it does. Use comments for: geometry, magic numbers tied to another file (e.g. SVG coordinates matching a template's `viewBox`), edge cases, and anything a fresh reader would otherwise have to reverse-engineer.
* **No clever code.** If a one-liner makes the reader stop and think, expand it. Boring is a feature.
* **Delete dead code.** No commented-out blocks, no "kept for later" branches. Git remembers.

If a chart's `to_html` method cannot be skimmed in fifteen seconds, it is too complicated. Simplify before adding features.

---

# Final Rule

If a solution feels overly generic, configurable, extensible, or enterprise-oriented, it is probably not appropriate for the MVP.

Choose the simplest implementation that works.
