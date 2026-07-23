# Changelog

All notable changes to Fireflyer are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-07-14

### Added

- **Managed datasets.** Datasets are first-class named entities, not inline CSV
  paths: upload a CSV in the new **Datasets** tab → **Parquet** in object storage,
  referenced by **name** (`dataset: orders`); metadata (schema, rows, description,
  delimiter, author) is a YAML sidecar. `datasets.py` (`DatasetStore`) over
  `storage.py` (`ObjectStore`: a local folder, or **Garage/S3** via the
  `.[portal]` extra, chosen by `FIREFLYER_S3_ENDPOINT`).
- **Datasets tab** — list + upload / replace / rename / remove, and a detail view
  (per-column type icons + 20-row preview). **Delete-guard** (can't remove a
  dataset a dashboard uses) and **cascade-rename** (rewrites `dataset:` refs).
- **Local paths mode** (`FIREFLYER_PATHS`, `web/paths.py`) — a non-portal,
  no-DB/no-login way to manage many dashboards + datasets on your filesystem. Each
  host folder you Docker-map is a switchable **path** (dashboards as
  `<path>/dashboards/*.yaml`, datasets in an isolated per-path blob store); a
  **`demo` path** is seeded on first run. Data-free dashboard YAML is meant to be
  kept in git and deployed by a coming CLI/API — a **GitOps** workflow; datasets
  stay server-side.

### Changed

- **Dashboard YAML dropped its `datasets:` block.** `dataset:` is now a name
  resolved to Parquet at render (`Dashboard.from_yaml(text, datasets=<store>)`),
  or a path/URI directly when no store is given. Required top-level keys: `name`,
  `charts`, `dashboard`.
- **Charts read Parquet efficiently** — lazy `scan_parquet` with projection +
  predicate pushdown, so only the needed columns/row-groups are read.
- **Gallery / editor navigation.** List pages lead with a **Dashboards | Datasets**
  switch on the left and (local paths mode) a **path dropdown** on the right;
  selected items (a dataset detail, the dashboard editor) lead with a **back
  button** + name and keep the path dropdown, no switch. The Fireflyer brand text
  is gone from the editor.

## [0.4.0] - 2026-07-10

### Added

- **Portal mode** (`FIREFLYER_PORTAL`, `python -m fireflyer.portal`, compose
  `portal` profile) — an opt-in, DB-backed way to store and browse many
  dashboards, reusing the editor unchanged. `/` becomes a gallery table (name,
  author, last updated) with per-row **Edit / Clone / Remove** and **+ New**; each
  opens in the editor with a **Save** button. Dashboards are stored as an opaque
  YAML blob (validated on save, never decomposed), so every stateless editor route
  keeps working. Two backends in `web/portal.py`: stdlib **sqlite** (dev/tests) and
  **Postgres** (the optional `.[portal]` extra). An owner-approved exception to the
  no-persistence anti-goal, scoped to `web/`.
- **Portal login** (`web/auth.py`) — a simple auth gate, default **admin/admin**,
  with a topbar profile dropdown + **Log out**. A deliberately small, swappable
  backbone — an `Authenticator` protocol and an HMAC session cookie are
  independent, so an SSO/OAuth callback just reuses `set_session` (recipe in
  `architecture.md`); none implemented. Local mode has no login.
- **Required top-level `name:` key** — the dashboard's display name, part of the
  definition (same in local and portal); `Dashboard.from_yaml` rejects an empty
  name, and portal re-derives its listing name from the YAML on every save.
- **Editor topbar + refresh-on-edit preview.** The Run button and status text are
  gone; editing the YAML greys the (still-interactive) preview and shows a **↻
  Refresh** overlay. **Save** appears only when there are unsaved changes (⌘/Ctrl+S,
  with a navigate-away guard), and a **3-segment Auto / Light / Dark** icon theme
  switch replaces the old toggle. Fixed two resize snap-back bugs — block-style
  row-height drags, and column drags on tabbed dashboards.

## [0.3.1] - 2026-07-09

### Added

- **CI + release automation** (`.github/workflows/`). On every PR into `main`,
  `ci.yml` runs the pytest suite and checks that `pyproject.toml`'s version was
  bumped versus `main`; both are intended to be required status checks so a PR
  can't merge until tests pass and the version is updated. On merge to `main`,
  `tag-on-merge.yml` reads the version and pushes the matching `vX.Y.Z` tag.
  Snapshot tests were made checkout-path independent (the absolute dataset path
  and the SHA-1 chart ids derived from it are normalized in the `snapshot`
  fixture) so the suite passes on CI, not just the author's machine.

## [0.3.0] - 2026-07-08

### Added

- **Dark theme.** Dashboards and every chart now ship a light *and* a dark
  palette as CSS custom properties. Selection is automatic —
  it follows the viewer's OS preference (`prefers-color-scheme`) — and can be
  forced with a `data-ff-theme="light|dark"` attribute: `Dashboard.to_html(...)`
  and each chart's `to_html(...)` take a `theme="dark"|"light"` argument, and an
  explicit choice overrides the OS preference. The browser editor gains a topbar
  **Theme: Auto / Light / Dark** toggle (persisted in `localStorage`) that themes
  the editor chrome, the dashboard preview, and all charts at once. Tokens are
  mirrored across `dashboard.css` and each chart's `chart.css` (no shared
  stylesheet, by design); the map's basemap tiles and hex overlay stay fixed
  since the tiles are always a light raster.
- **Contributor guides tracked in-repo.** The chart- and param-authoring guides
  now live in the repository as `fireflyer/chart/SKILL.md` and
  `fireflyer/PARAM_SKILL.md` (previously only local, gitignored Claude Code
  skills). `CLAUDE.md` links both, and the skills point at these files as the
  single source of truth.

## [0.2.0] - 2026-07-08

### Added

- **Dashboard tabs.** A dashboard's `dashboard:` section can now be a mapping of
  tab name → layout list, splitting the page into tabs; a flat list stays the
  no-tabs form, unchanged. Only the active tab's charts load — switching is lazy
  via htmx — the tab bar is sticky, and crossfilters stay global across tabs. In
  the browser editor: add a tab from the row **"+"** menu (with a forced rename;
  cancelling undoes the add), rename a tab inline, move a tab's boundary between
  rows (switching tabs to reach a target row), and delete a tab — a non-first tab
  merges into the previous, and the first dissolves all tabs back to a flat list.
  Charts can be moved across tabs in move mode.

## [0.1.0] - 2026-07-07

First public release — an early MVP. Under heavy development and not yet
production-ready.

### Added

- **Charts from CSV.** `table`, `pie`, `bar`, `map`, and `number` chart types.
  Each takes a `dataset` and `title` plus a couple of chart-specific fields, and
  renders to standalone HTML (server-rendered, htmx-only, no build step).
- **YAML dashboards.** Declare datasets, charts, and page layout in a single
  YAML file via a compact layout DSL (`["@<height>", "<chart>:<width>", ...]`,
  headers, and separators). Render with `Dashboard.from_yaml(text).to_html()`.
- **Crossfiltering.** Click a chart value and every other chart narrows to
  match, with no page reload. Fixed `filters` can also be declared per chart.
- **Browser editor.** Two-pane editor: write YAML on the left, live preview on
  the right. Add charts from a menu with a form, drag rows and columns to
  resize, move charts, and edit each chart through a modal. Every edit is
  written back as clean YAML so the visual and code views stay in sync.
- **AI assistant.** Built-in chat edits the dashboard from plain-English
  requests, validating each change before applying it. Runs on Claude; gated by
  an `ANTHROPIC_API_KEY` and disabled gracefully when no key is set.
- **Use as a library.** `import fireflyer as ff`; charts render inline in
  Jupyter via `_repr_html_` or expose their HTML through `to_html()`.
- **Run with Docker or locally.** `docker compose up --build` for the editor
  with hot-reload, or `pip install -e ".[test]"` and `python -m fireflyer.web`.
- **Snapshot test suite.** Each test pairs an input CSV + chart/dashboard
  definition with the exact expected HTML in `tests/snapshots/`.
- **Source-available license.** Apache-2.0 with the Commons Clause.

[Unreleased]: https://github.com/dankor/fireflyer/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/dankor/fireflyer/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dankor/fireflyer/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/dankor/fireflyer/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/dankor/fireflyer/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dankor/fireflyer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dankor/fireflyer/releases/tag/v0.1.0
