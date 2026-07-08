import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import jinja2
import polars as pl

from fireflyer import filters as filters_mod
from fireflyer.params import ColumnParam, DatasetParam, FilterListParam, IntParam, TextParam

# OpenStreetMap raster tiles. The tile.openstreetmap.org server is okay for
# light personal use as long as we keep request volume low and display the
# attribution; the chart's footer carries the required notice.
TILE_URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_SIZE = 256

# Web Mercator zoom levels supported by OSM. 0 = whole world, ~19 = max detail.
# We cap at 18 to stay inside the tile-set guarantee.
ZOOM_MIN = 1
ZOOM_MAX = 18

# Target Mercator pixel dimensions used to pick a tile zoom: the chart picks
# the largest zoom at which the data still fits inside this box. The values
# are bigger than a typical dashboard cell so the natural SVG ends up larger
# than the visible viewport — the canvas then scrolls instead of squishing
# the map. Hex grid is anchored to Mercator coords, so it pans with the map.
TARGET_W = 800
TARGET_H = 600
MARGIN_PX = 24

HEX_COLOR = "#1FA8C9"
GRID_MIN = 4
GRID_MAX = 200

ENDPOINT = "/chart/map"

_DIR = Path(__file__).parent
_CSS = (_DIR / "chart.css").read_text()
_TEMPLATE = jinja2.Template(
    (_DIR / "chart.html").read_text(),
    autoescape=True,
)


# --- Web Mercator helpers -----------------------------------------------------
#
# All map geometry lives in "world pixel" coords at a chosen zoom: that's the
# Mercator projection scaled so that one tile is exactly TILE_SIZE px. This
# means tiles render at integer (tx*256, ty*256) positions and hex bins use
# the same coordinate space, so the two layers always align.


def _lng_to_world_x(lng: float, z: int) -> float:
    return (lng + 180.0) / 360.0 * TILE_SIZE * (2 ** z)


def _lat_to_world_y(lat: float, z: int) -> float:
    # Clamp at the Mercator pole; tan() blows up outside ±85.05113°.
    lat_c = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat_c)
    return (
        (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * TILE_SIZE
        * (2 ** z)
    )


def _choose_zoom(
    lng_lo: float, lng_hi: float, lat_lo: float, lat_hi: float,
    target_w: float, target_h: float,
) -> int:
    """Largest zoom where the data bbox fits within target_w × target_h pixels.

    Scanning high → low so the first zoom that fits is also the most detailed
    we can show without cropping. Tile zoom is independent of the user's
    `grid_size` (which controls hex density, not geographic scale).
    """
    for z in range(ZOOM_MAX, ZOOM_MIN - 1, -1):
        x_span = _lng_to_world_x(lng_hi, z) - _lng_to_world_x(lng_lo, z)
        # y grows southward: smaller lat → larger world y.
        y_span = _lat_to_world_y(lat_lo, z) - _lat_to_world_y(lat_hi, z)
        if x_span <= target_w and y_span <= target_h:
            return z
    return ZOOM_MIN


# --- Hex grid helpers ---------------------------------------------------------


def _hex_round(q_frac: float, r_frac: float) -> tuple[int, int]:
    s_frac = -q_frac - r_frac
    q = round(q_frac)
    r = round(r_frac)
    s = round(s_frac)
    q_diff = abs(q - q_frac)
    r_diff = abs(r - r_frac)
    s_diff = abs(s - s_frac)
    if q_diff > r_diff and q_diff > s_diff:
        q = -r - s
    elif r_diff > s_diff:
        r = -q - s
    return q, r


def _pixel_to_axial(x: float, y: float, size: float) -> tuple[int, int]:
    q = (math.sqrt(3) / 3 * x - 1 / 3 * y) / size
    r = (2 / 3 * y) / size
    return _hex_round(q, r)


def _hex_center(q: int, r: int, size: float) -> tuple[float, float]:
    return (size * (math.sqrt(3) * q + math.sqrt(3) / 2 * r), size * 3 / 2 * r)


def _hex_corners(cx: float, cy: float, size: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(6):
        # -90° puts the first vertex at the top in SVG's y-down system.
        angle = math.pi / 3 * i - math.pi / 2
        pts.append((cx + size * math.cos(angle), cy + size * math.sin(angle)))
    return pts


def _filters_json(filters: list[filters_mod.Filter]) -> str:
    return json.dumps([f.as_dict() for f in filters], separators=(",", ":"))


@dataclass
class Map:
    dataset: str
    title: str
    lng: str
    lat: str
    # `grid_size` is now hex side length in viewBox (Mercator world) pixels —
    # NOT a cell-count across the canvas. Keeping it constant in world-pixel
    # space means hex coverage scales with the tile zoom: zooming in expands
    # the viewBox in pixels, more hexes naturally fit. That's the "grids
    # adopt to the background map" behavior the user asked for.
    grid_size: int = 20
    # Optional explicit tile zoom. `None` = auto-fit to the data bbox; an int
    # overrides (clamped to [ZOOM_MIN, ZOOM_MAX]). The +/- buttons round-trip
    # this value to step zoom up/down.
    zoom: int | None = None
    filters: list = field(default_factory=list)

    # Editor modal schema — see fireflyer/params.py and the "chart params" skill.
    PARAMS = [
        DatasetParam("dataset", "Dataset"),
        TextParam("title", "Title"),
        ColumnParam("lat", "Latitude column"),
        ColumnParam("lng", "Longitude column"),
        IntParam("grid_size", "Hex size", minimum=GRID_MIN, maximum=GRID_MAX),
        IntParam("zoom", "Tile zoom", minimum=ZOOM_MIN, maximum=ZOOM_MAX, nullable=True),
        FilterListParam("filters", "Filters"),
    ]

    def __post_init__(self) -> None:
        self.filters = filters_mod.normalize(self.filters)
        self.grid_size = max(GRID_MIN, min(GRID_MAX, int(self.grid_size)))
        if self.zoom is not None:
            self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, int(self.zoom)))

    def to_html(self, *, theme: str | None = None) -> str:
        """`theme` forces a palette (`"dark"`/`"light"`); omitted, the chart
        follows the viewer's OS preference (inherited from the dashboard root
        when nested). Only the card chrome is themed — the tile basemap and its
        hex overlay stay fixed."""
        ff_theme = theme if theme in ("dark", "light") else ""
        df = pl.read_csv(self.dataset)
        df = filters_mod.apply(df, self.filters)
        df = df.filter(
            pl.col(self.lat).is_not_null() & pl.col(self.lng).is_not_null()
        )

        chart_id = self._chart_id()
        base_params = self._base_params()

        if df.height == 0:
            return _TEMPLATE.render(
                css=_CSS, title=self.title, vb_w=TARGET_W, vb_h=TARGET_H,
                tiles=[], hexes=[], max_count=0, hex_color=HEX_COLOR,
                chart_id=chart_id, endpoint=ENDPOINT, base_params=base_params,
                grid_size=self.grid_size, zoom=ZOOM_MIN,
                zoom_in=ZOOM_MIN + 1, zoom_out=ZOOM_MIN,
                grid_finer=max(GRID_MIN, self.grid_size // 2),
                grid_coarser=min(GRID_MAX, self.grid_size * 2),
                tile_size=TILE_SIZE, bounds=None, empty=True, ff_theme=ff_theme,
            )

        lng_min = float(df[self.lng].min())
        lng_max = float(df[self.lng].max())
        lat_min = float(df[self.lat].min())
        lat_max = float(df[self.lat].max())

        # Tile zoom: user-controlled when set, otherwise the largest level at
        # which the data still fits the canvas. The +/- buttons round-trip
        # this through `&zoom=` so successive clicks step it.
        if self.zoom is not None:
            z = self.zoom
        else:
            z = _choose_zoom(
                lng_min, lng_max, lat_min, lat_max,
                TARGET_W - 2 * MARGIN_PX, TARGET_H - 2 * MARGIN_PX,
            )

        # Data corners in world Mercator pixels.
        x_west = _lng_to_world_x(lng_min, z)
        x_east = _lng_to_world_x(lng_max, z)
        y_north = _lat_to_world_y(lat_max, z)
        y_south = _lat_to_world_y(lat_min, z)

        # Padded viewBox bounds (still in world pixels).
        x_lo = x_west - MARGIN_PX
        x_hi = x_east + MARGIN_PX
        y_lo = y_north - MARGIN_PX
        y_hi = y_south + MARGIN_PX

        vb_w = x_hi - x_lo
        vb_h = y_hi - y_lo
        offset_x = x_lo
        offset_y = y_lo

        # Tile range covering the viewBox, clamped to the valid OSM range.
        max_tile = (2 ** z) - 1
        tx_min = max(0, math.floor(x_lo / TILE_SIZE))
        tx_max = min(max_tile, math.ceil(x_hi / TILE_SIZE) - 1)
        ty_min = max(0, math.floor(y_lo / TILE_SIZE))
        ty_max = min(max_tile, math.ceil(y_hi / TILE_SIZE) - 1)

        tiles = []
        for ty in range(ty_min, ty_max + 1):
            for tx in range(tx_min, tx_max + 1):
                tiles.append({
                    "url": TILE_URL_TEMPLATE.format(z=z, x=tx, y=ty),
                    "x": tx * TILE_SIZE - offset_x,
                    "y": ty * TILE_SIZE - offset_y,
                })

        # Hex side length in viewBox (world Mercator) pixels. Constant across
        # zoom levels — this is the rule that makes the grid adopt to the map:
        # at higher tile zoom the viewBox grows but the hex pixel size stays
        # the same, so more hexes fit the data extent.
        hex_size = float(self.grid_size)

        bins: dict[tuple[int, int], int] = {}
        for lng_v, lat_v in zip(df[self.lng].to_list(), df[self.lat].to_list()):
            wx = _lng_to_world_x(float(lng_v), z) - offset_x
            wy = _lat_to_world_y(float(lat_v), z) - offset_y
            qr = _pixel_to_axial(wx, wy, hex_size)
            bins[qr] = bins.get(qr, 0) + 1

        max_count = max(bins.values()) if bins else 0

        hexes = []
        for (q, r), count in bins.items():
            cx, cy = _hex_center(q, r, hex_size)
            if cx < -hex_size or cx > vb_w + hex_size:
                continue
            if cy < -hex_size or cy > vb_h + hex_size:
                continue
            pts = _hex_corners(cx, cy, hex_size)
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            opacity = 0.15 + 0.85 * (count / max_count if max_count else 0)
            hexes.append({
                "points": points,
                "opacity": f"{opacity:.3f}",
                "count": count,
                # Center coords drive the hover label; pre-rounded so the
                # template doesn't need formatting logic.
                "cx": f"{cx:.1f}",
                "cy": f"{cy:.1f}",
            })

        # Zoom step targets for the +/- buttons. Clamped here so the URL
        # always carries a value the next request can validate.
        zoom_in = min(ZOOM_MAX, z + 1)
        zoom_out = max(ZOOM_MIN, z - 1)
        # Grid-size step targets. `+` halves grid_size (smaller hexes → more
        # hexes → finer aggregation), `−` doubles it. This matches the map
        # convention that `+` always means "more detail".
        grid_finer = max(GRID_MIN, self.grid_size // 2)
        grid_coarser = min(GRID_MAX, self.grid_size * 2)

        return _TEMPLATE.render(
            css=_CSS,
            title=self.title,
            vb_w=vb_w, vb_h=vb_h,
            tiles=tiles, hexes=hexes,
            hex_color=HEX_COLOR, max_count=max_count,
            chart_id=chart_id, endpoint=ENDPOINT, base_params=base_params,
            grid_size=self.grid_size, zoom=z,
            zoom_in=zoom_in, zoom_out=zoom_out,
            grid_finer=grid_finer, grid_coarser=grid_coarser,
            tile_size=TILE_SIZE,
            bounds={
                "lng_lo": f"{lng_min:.4f}",
                "lng_hi": f"{lng_max:.4f}",
                "lat_lo": f"{lat_min:.4f}",
                "lat_hi": f"{lat_max:.4f}",
                "z": z,
            },
            empty=False,
            ff_theme=ff_theme,
        )

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.to_html()

    def _chart_id(self) -> str:
        # Zoom is part of identity so each step gets a fresh DOM id; htmx
        # outerHTML-swaps cleanly even though the id changes — the swapped
        # element's inner buttons reference the new id.
        parts = [
            self.dataset, self.title, self.lat, self.lng,
            str(self.grid_size),
            str(self.zoom) if self.zoom is not None else "auto",
        ]
        if self.filters:
            parts.append(_filters_json(self.filters))
        digest = hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
        return f"fireflyer-map-{digest}"

    def _base_params(self) -> str:
        # Identity params (dataset + projection columns + filters). `zoom` and
        # `grid_size` are appended by each button so they can be overridden
        # independently — zoom buttons hold grid_size constant, grid buttons
        # hold zoom constant.
        params = {
            "dataset": self.dataset,
            "title": self.title,
            "lat": self.lat,
            "lng": self.lng,
        }
        if self.filters:
            params["filters"] = _filters_json(self.filters)
        return urlencode(params)
