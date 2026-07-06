# Map chart

## Purpose
Plot a point dataset (lat/lng) as a pointy-top hex grid heatmap over an OpenStreetMap tile background. Hex shading scales with per-bin record count.

## Behavior
- Reads the CSV.
- Applies the chart's `filters` (see architecture.md "Filters") before binning.
- Drops rows where `lat` or `lng` is null.
- Uses Web Mercator projection. `lat`/`lng` are converted to world-pixel coordinates at the current tile zoom; tiles and hexes share that coordinate space so they always align.
- Tile zoom is either user-set (`zoom` parameter, exposed as `+`/`−` buttons in the UI) or auto-fit. The auto fit picks the largest zoom level at which the data bounding box still fits inside the canvas (plus a small margin).
- Renders one OSM tile `<image href="https://tile.openstreetmap.org/{z}/{x}/{y}.png">` per tile covering the viewBox. Browsers fetch tiles directly; no proxy or caching layer.
- The hex grid uses a constant side length in world-pixel units (`grid_size`). Because the viewBox grows when the tile zoom increases, more hexes naturally fit the same geographic data — the grid "adopts" to the map detail level: zooming in produces finer aggregation, zooming out coarser.
- Each hex renders as an SVG `<polygon>` with a single accent color; opacity scales from `0.15` (single record) to `1.0` (max-count hex).
- Each polygon is wrapped in `<g class="fireflyer-map-hex">` together with a paired `<text class="fireflyer-map-hex-label">{count}</text>` at the hex center. The label is hidden by default and revealed on hex hover via a single CSS rule; a `paint-order: stroke` halo keeps the count readable over any tile content.
- Responsive canvas: the SVG carries `width="100%" height="100%"` plus `preserveAspectRatio="xMidYMid meet"`, so it scales to fill the cell while preserving the viewBox aspect ratio. The `.fireflyer-map-canvas` has `overflow: hidden` — the map always fits its container.
- Two pairs of `−`/`+` controls in the chart header drive interaction, both via htmx round-trips:
  - **Zoom (`z<N>` label)** steps the OSM tile zoom by ±1 (clamped to `[1, 18]`). Tile detail and viewBox extent change; the hex pixel size stays the same so hex density adapts.
  - **Hex (`N` label)** halves or doubles `grid_size` (clamped to `[4, 200]`). `+` makes hexes smaller (more, finer bins); `−` makes them larger (fewer, coarser).
- Carries the required `© OpenStreetMap contributors` attribution in the chart footer, alongside the chosen tile zoom (`z<N>`) and lat/lng bounds.

## Parameters
- `dataset: str` — path to the CSV.
- `title: str` — chart title.
- `lat: str` — column with latitude values.
- `lng: str` — column with longitude values.
- `grid_size: int = 20` — hex side length in world-pixel (Mercator) units. Smaller = finer hexes at any given zoom. Clamped to `[4, 200]`.
- `zoom: int | None = None` — explicit tile zoom level. `None` = auto-fit to the data bbox. Clamped to `[1, 18]` when set.
- `filters: list = []` — declarative pre-filter applied before binning.

## Editor params
Edit-modal schema (`Map.PARAMS`): dataset (dropdown), title (text), lat/lng (column dropdowns),
grid_size (number), zoom (nullable number), filters (filter builder). Widgets live in `fireflyer/params.py`.
