# Changelog

All notable changes to Fireflyer are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-07-10

### Added

- **Portal mode** — an opt-in, DB-backed way to store and browse many
  dashboards, reusing the existing editor unchanged. Enabled with
  `python -m fireflyer.portal` (reads `portal.yaml`) or the compose `portal`
  profile. `/` becomes a gallery of stored dashboards — a table of name,
  author, and last-updated with per-row **Edit / Clone / Remove** actions and a
  **+ New dashboard** button; New and Clone each prompt for a name in a modal.
  New dashboards start **blank**; each opens in the normal editor with a
  **Save** button. Dashboards are stored as an opaque YAML text blob (validated
  by `Dashboard.from_yaml` on save, never decomposed into tables), so every
  stateless editor route keeps working byte-for-byte. Rows also carry an
  **author** (the logged-in user, recorded at create/clone).
- **Portal login** — portal mode is gated behind a simple auth (`web/auth.py`),
  default **admin/admin** (`FIREFLYER_USER`/`FIREFLYER_PASSWORD`); a topbar
  **profile** dropdown shows the username with a **Log out** action. It's a deliberately small,
  swappable backbone: an `Authenticator` protocol (the credential check) and an
  HMAC-signed session cookie (how the identity is remembered) are independent,
  so an SSO/OAuth callback just reuses `set_session` — no route changes. No
  advanced provider is implemented; the extension recipe is documented in
  `architecture.md`. Local single-dashboard mode has no login.
- **Editor topbar** reorganized — left: a **☰ Dashboards** link and the
  Fireflyer **logo** (both link to the gallery in portal), then an **editable
  dashboard title** after a dot separator (click to rename → rewrites the YAML
  `name:` key, and editing `name:` in the YAML updates the title); right:
  **Save**, Preview,
  a **3-segment Auto / Light / Dark theme switch**, and the profile button.
  **Save only appears when there are unsaved changes**, saves on click or
  ⌘/Ctrl+S, and warns before you navigate away with unsaved edits. The theme
  control is a **3-segment icon switch** (A / sun / moon for Auto / Light /
  Dark, inline SVG, no text) — in the profile
  dropdown in portal mode, standalone in the topbar in local mode.
- **Refresh-on-edit preview.** The topbar **Run** button and status text are
  gone. Editing the YAML now greys out the (stale) preview and reveals a **↻
  Refresh** button over the output pane; clicking it re-renders. The greyed
  preview stays **interactive** (row/column resize keeps working). Two resize
  snap-back bugs were fixed: row-height drags now persist for **block-style**
  dashboard rows (the height rewrite was flow-style only), and **column** drags
  now persist on **tabbed** dashboards (`resize_columns` searched flat `.items`
  only, which are empty when the layout is tabbed, so it silently no-op'd). Save
  feedback shows on the Save button itself, and rare edit errors use a toast.
- **Required top-level `name:` key** in the dashboard YAML — the dashboard's
  display name, part of the definition (not portal metadata), so it works the
  same in local and portal mode. `Dashboard.from_yaml` now rejects a dashboard
  with no (or empty) `name`. Portal lists dashboards by it and re-derives the
  listing name from the YAML on every save (no separate name field); the
  gallery's "new" form seeds the typed name into the YAML's `name:` key. The store lives in `fireflyer/web/portal.py` behind two
  backends: stdlib **sqlite** (local/dev + tests) and **Postgres**
  (`python -m fireflyer.portal`); the Postgres driver is an optional `.[portal]`
  extra so the core install and test suite never require a database. Portal is
  an owner-approved exception to the "no persistence/multi-user" anti-goal,
  scoped to `web/`. Auth and per-dataset storage are intentionally out of scope
  for this first cut.

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

[Unreleased]: https://github.com/dankor/fireflyer/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/dankor/fireflyer/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/dankor/fireflyer/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/dankor/fireflyer/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dankor/fireflyer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dankor/fireflyer/releases/tag/v0.1.0
