# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and human contributors working in this repo.

## What Fireflyer is

A Python library that turns a CSV into an HTML visualization in a few lines of code. The user imports `fireflyer as ff`, calls `ff.chart.table(...)`, `ff.chart.pie(...)`, `ff.chart.bar(...)`, `ff.chart.map(...)`, or `ff.chart.number(...)`, and gets HTML back. Dashboards compose charts from a single YAML file. Polars is an internal implementation detail — the user never touches it.

Core flow, kept deliberately flat:

```
CSV → DataFrame → Chart → HTML → Browser
```

Charts are declarations, not stateful objects. Every render re-reads the CSV, re-executes chart logic, and re-generates HTML. No caching.

**`architecture.md` is the authoritative spec** for the layout DSL, filter model, and rendering rules. Read it before changing dashboard or chart behavior. Per project preference, **ask before editing `architecture.md`** — it's a controlled spec doc.

## Repository layout

```
fireflyer/
├── chart/
│   ├── table/  pie/  bar/  map/  number/   # each: chart.py, chart.html, chart.css, spec.md
│   └── __init__.py                         # exposes table/pie/bar/map/number as ff.chart.*
├── dashboard.py                        # YAML dashboard: parse, validate, render
├── filters.py                          # one filter model (declared + crossfilter)
├── params.py                           # editor param widgets (Text/Choice/Column/Int/Bool/FilterList)
├── config_edit.py                      # edit-modal: build form, surgical YAML block replace
├── dashboard.html / skeleton.html / cell.html   # dashboard Jinja templates
├── dashboard.css                       # dashboard-level styling (injected into output)
└── web/                                # FastAPI app + browser editor (dev tool, not core)
    ├── app.py                          # editor page, /execute, /dashboard, /chart/config/*, /chat
    └── chat.py                         # AI assistant: Anthropic SDK + DSL system prompt
tests/                                  # snapshot-based; snapshots/ holds expected HTML
files/ , tests/data/                    # sample CSVs (orders.csv)
architecture.md                         # authoritative spec
```

Each chart folder is the modularity boundary: `chart.py` computes data, `chart.html` (Jinja2, autoescaped) renders it, `chart.css` is injected once per chart, `spec.md` is the source of truth for that chart's behavior. **When changing a chart, update its `spec.md` in the same change.**

## Running things

```bash
docker compose up --build     # editor at http://127.0.0.1:8000 (mounts source + files/, hot-reload)
# or locally:
pip install -e ".[test]"      # deps: polars, fastapi, uvicorn, jinja2, pyyaml, python-multipart
python -m fireflyer.web        # browser editor at http://127.0.0.1:8000
pytest                         # snapshot suite
UPDATE_SNAPSHOTS=1 pytest      # regenerate snapshots after an intentional render change
```

Docker: `Dockerfile` + `docker-compose.yml` run uvicorn bound to `0.0.0.0`; the local `python -m fireflyer.web` entrypoint stays on `127.0.0.1`. The compose service bind-mounts `./fireflyer` and `./files`, so edits hot-reload without a rebuild — rebuild (`--build`) only when `pyproject.toml` changes.

If the venv breaks after moving the repo (stale editable path), re-run `pip install -e ".[test]"`.

## Versioning

The version lives in `pyproject.toml` (`version = "X.Y.Z"`), mirrored by `CHANGELOG.md` and the `vX.Y.Z` git tag. **Every release commit must bump the version** — follow Semantic Versioning: patch for fixes, minor for new backward-compatible features, major for breaking changes (pre-1.0, a breaking change may still go in a minor). When bumping: update `pyproject.toml`, move the `[Unreleased]` notes under a new `## [X.Y.Z] - <date>` heading in `CHANGELOG.md`, and update the compare links at the bottom of the changelog.

## Testing approach

Snapshot-based. Each test pairs an input CSV + chart/dashboard definition with expected HTML in `tests/snapshots/<test_name>.html` (see `tests/conftest.py` for the `snapshot` fixture). The goal is to verify the **exact** generated HTML.

- A rendering change will fail the relevant snapshot. If the change is intentional, regenerate with `UPDATE_SNAPSHOTS=1` and **review the diff** before committing.
- `dashboard.css` is injected into every dashboard render, so editing it changes the dashboard snapshot even when markup is unchanged — that's expected.

## Rendering rules (enforced, not stylistic)

- **Server-rendered, htmx-only.** Chart output is HTML + inline SVG + CSS + htmx attributes. No Alpine/Stimulus/React/Vue and no hand-written JS in chart output. The only `<script>` a chart's host page loads is htmx.
- **Jinja2 templates, autoescaped** — never build chart HTML with f-strings. CSV values and params must not be able to inject HTML.
- **CSS is per-chart**, namespaced under a chart class (`.fireflyer-table`, `.fireflyer-pie`, …), read once at import and injected inline. No shared base stylesheet, no `@import`, no build step.
- The **web editor is exempt** from the no-JS rule: it's a dev tool, not core architecture. Vanilla JS lives in `web/app.py`'s page (Run, Hide YAML, drag-to-resize, chat, the edit modal). Keep it there — don't leak editor JS into chart or dashboard output. Editor-only markup (resize handles, the per-chart pencil button) is gated behind an `editing` flag so it never ships in `to_html()`.

## AI assistant (`web/chat.py`)

The editor's chat sends the current YAML + request to Claude (`claude-sonnet-4-6`, via the official `anthropic` SDK). Claude replies in text and, when a change is wanted, calls an `update_dashboard` tool with the **complete** new YAML; `run_chat` validates it with `Dashboard.from_yaml` before returning, with a bounded repair loop. Notes:

- The key comes from `ANTHROPIC_API_KEY` (loaded from `.env` via `python-dotenv` at app startup), stays server-side, and gates the feature (`CHAT_ENABLED`). Tests fake the client — never call the live API in the suite.
- `chat.py` embeds a condensed DSL spec as the (prompt-cached) system prompt. **Keep it in sync** with `architecture.md` and the chart `spec.md` files when the layout or chart rules change.
- This is editor-only and not part of the library core — same status as the rest of `web/`.

## Chart params & edit modal (`params.py`, `config_edit.py`)

The editor renders a hover **toolbar** on every chart (edit + delete buttons, only while the YAML pane is open — gated by an `editing` flag threaded through `render_skeleton`/`render_cell`/the async routes). Edit opens a modal form mirroring the chart's YAML config with real widgets; delete shows an "are you sure?" confirm, then `config_edit.delete_chart` removes the chart's block and all its layout placements (dropping now-empty rows). See `architecture.md`, "Chart params & editor modal".

- **`params.py`** holds the shared widget classes (`TextParam`, `DatasetParam`, `ColumnParam`, `ChoiceParam`, `IntParam`, `BoolParam`, `FilterListParam`). Each `Param` knows how to `render` an autoescaped input, `parse` the submitted value, and emit `to_yaml`. Widgets live here, not in chart code, so every chart reuses them.
- **Each chart declares `PARAMS`** — a `list[Param]` class attribute, one per constructor field, in display order. The modal also shows a **chart-type dropdown** above the params; changing it re-fetches the form (`build_form(..., type_override=)`) so the fields match the new type, and saving can swap the type. This is why chart files are a bit bigger. A test (`tests/test_chart_params_match_constructor.py`) asserts `PARAMS` names == the dataclass fields, so they can't drift.
- **`config_edit.py`** builds the form (`build_form`) and applies a save (`apply_edit`) by **surgically replacing only that chart's YAML block** — siblings, comments, and formatting elsewhere stay byte-for-byte. Comments *inside* the edited block are regenerated (documented limitation). The whole doc is re-validated via `Dashboard.from_yaml` before returning.
- **Adding to the layout**: the editor's left gutter shows hover **"+" buttons**. The per-row one (`.fireflyer-add-cell`) adds a chart to that row (the add modal). The insert-strip one (`.fireflyer-add-row`) opens a small **menu — chart / header / separator**: chart uses the add modal (`build_add_form` → `/chart/config/create`), while header and separator insert directly via `config_edit.insert_layout_item` → `/chart/config/insert-item` (a header defaults to text "New header"). A header can be **renamed in place** — double-click it in edit mode (`config_edit.set_header_text` → `/chart/config/header`, located by header index). **Headers and separators also get the hover toolbar** (compact badge, top-right) with **move / edit / delete**, addressed by their **layout-item index** (not a chart id): move → `config_edit.move_layout_item` (`/chart/config/move-item`), delete → `config_edit.delete_layout_item` (`/chart/config/delete-item`, confirm dialog). Edit is **header-only** (opens the same inline rename) — a separator has **no edit button** (and its wrapper gets padding so the thin `<hr>` is hoverable; the badge is centred on the item's top edge). Header edit mirrors move mode's focus feel: the dashboard gets `.ff-focus-mode` (dims everything but the edited header, suppresses hover UI, hides add strips) and the same topbar cancel button shows — `currentHeaderFinish` lets the button (mousedown+`preventDefault` so it cancels instead of blur-saving) or Esc restore the original; Enter/blur saves. Their **move is between-rows only**: it skips `buildMoveZones` and reuses the lit add-row strips as the sole drop targets (`enterItemMove` also hides the two strips flanking the moved item, which would be no-op drops; the shared move state is `moveCid` for charts vs `moveItemIndex` for items, unified by `inMove()`). Charts are rearranged via a **move mode** (not native drag-and-drop): a chart's **move** button lights the chart, dims the rest, and turns off every other interaction (resize, edit, add, crossfilter). Every valid spot lights up as a **blue drop-zone box** — a client-built overlay of column zones (before/after each cell; geometry-derived so merged/spanning cells align) plus **add-row strips in every gap between layout items** (drop into a new row there — `data-before` is a **layout-item index** into the full rows+headers+separators list, so it works around headers and separators too, not just between rows). The hovered box goes solid as a placement preview. The zones follow the **8 move-mode rules documented in `architecture.md`** — the client build is in `buildMoveZones`: side zones on every chart's left/right edge (`move_placement` → `/chart/config/move`); **common borders dedup** to one drop (`dedupBorders` keeps the taller candidate, so a **merged** chart's full-height side wins → dropping there adopts the span); between-rows via the add-row strips/internal gaps (`move_to_new_row` → `/chart/config/new-row`); a single **merge-down** bar (`.ff-move-zone-span`) down the *moved* chart's centre into the row below — `config_edit.merge_down` → `/chart/config/merge-down` adds a bare occurrence so *that chart's own* span grows down one row (only the moved chart, only downward, only one per dashboard); and the moved chart gets **no side zones**, nor its shared borders — **except** a **merged** moved chart, whose shared borders stay as **per-row unmerge** zones (`srcMerged` gate); dropping one is a plain `move_placement` that lands it single-row in that row (span removed). `dedupBorders` never collapses a border involving a merged chart. `move_span` still exists (place a chart spanning a target's whole span + 1 below) and is tested, but the editor's merge gesture now uses `merge_down`. If moving a member out breaks a span, `_finalize` repairs it by collapsing the broken span into its fullest remaining row. Inserts use **width 1** (`:1`); the first row's sizes drive the layout. **Column resize** on a merge group posts to `config_edit.resize_columns` (route `/chart/config/resize-columns`): it recomputes each cell's width from the fine (union) columns it spans, so dragging a boundary updates every row those columns belong to — even from an inherited/lower row — and spanning cells stay bare. Esc/Cancel exits; an emptied source row is dropped. Move-mode clicks/mousedowns are captured (`stopPropagation`) so nothing else fires. `add_chart` generates a unique id, appends the chart block, and splices a placement into the `dashboard:` list (flow-style rows only). All gated by `editing`.
- To add a widget type: implement a `Param` subclass in `params.py`, then reference it from a chart's `PARAMS`. Use the **`chart params` skill**. Pure logic lives in `params.py`/`config_edit.py` (not `app.py`) so it unit-tests without the web stack.

## Non-negotiable constraints (from architecture.md)

Explicit anti-goals. Do not add them, even if they seem like good engineering:

- **No abstractions for future flexibility.** No service layers, repositories, registries, plugin frameworks, or DI containers until actually needed. The dashboard's `type → class` lookup is a plain dict — keep it that way. (One **deliberate, owner-approved exception**: the `params.py` widget layer, which exists to power the editor's edit modal. It earns its keep — don't take it as license for more abstractions.)
- **No frontend tooling.** No npm, webpack, vite, tailwind, or bootstrap. PicoCSS-compatible markup and inline SVG only.
- **No production concerns.** No auth, multi-user, caching, streaming, large-dataset optimization, or realtime updates.
- **No chart features beyond the MVP spec.** Aggregation is count-only for pie/bar/map. Joins, calculated columns, SQL, and export are out of scope. Table reads at most the first 1000 rows.

When in doubt: less code, fewer abstractions, hardcoded behavior, developer experience over architectural purity. The MVP is expected to be rewritten — if a solution feels generic, configurable, or extensible, it's probably wrong for this stage.

## Code style

Code should read top-to-bottom like a story to someone who's never seen it: constants, helpers, then the class. Small functions; lift anything past one screen into a private `_helper` in the same file (no `utils.py`). Named locals over dense one-liners. Comments explain *why* (geometry, magic numbers tied to a template's `viewBox`, edge cases), not *what*. Delete dead code — git remembers. If a chart's `to_html` can't be skimmed in fifteen seconds, simplify before adding features.
