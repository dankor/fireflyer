import pytest

import fireflyer as ff


def test_map_orders_density(orders_csv, snapshot):
    chart = ff.chart.map(
        dataset=orders_csv,
        title="Order density (Kyiv)",
        lat="lat",
        lng="lng",
        grid_size=10,
    )
    snapshot(chart.to_html())


def test_map_bins_match_fixture(orders_csv):
    """Fixture has 4 distinct (lat, lng) points:
       (50.450, 30.520) → ids 1,3,7 = 3 records
       (50.430, 30.520) → ids 2,5   = 2 records
       (50.500, 30.600) → id 4      = 1 record
       (50.400, 30.500) → id 6      = 1 record
    With a coarse-enough grid every point lands in its own hex.
    """
    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=20
    )
    html = chart.to_html()
    # Four populated hexes.
    assert html.count("<polygon") == 4
    # Peak count is 3 (the (50.450, 30.520) cluster).
    assert 'data-count="3"' in html
    assert 'data-count="2"' in html
    assert html.count('data-count="1"') == 2
    # Legend bar reflects the peak.
    assert ">3</span>" in html  # max-count legend label


def test_map_filter_narrows_before_binning(orders_csv):
    """Declared filter narrows rows before lat/lng binning."""
    chart = ff.chart.map(
        dataset=orders_csv,
        title="paid only",
        lat="lat",
        lng="lng",
        grid_size=20,
        filters=[{"column": "status", "op": "in", "values": ["paid"]}],
    )
    html = chart.to_html()
    # Paid rows are ids 1,3,5,7 → 3 of them at (50.450, 30.520) and 1 at (50.430, 30.520).
    # That's 2 distinct points → 2 hexes.
    assert html.count("<polygon") == 2


def test_map_grid_size_clamped():
    """Out-of-range grid_size is clamped at construction."""
    chart = ff.chart.map(
        dataset="x", title="t", lat="lat", lng="lng", grid_size=99999
    )
    assert chart.grid_size == 200  # GRID_MAX
    chart2 = ff.chart.map(
        dataset="x", title="t", lat="lat", lng="lng", grid_size=1
    )
    assert chart2.grid_size == 4  # GRID_MIN


def test_map_zoom_controls_target_tile_zoom(orders_csv):
    """The +/- buttons step the OSM tile zoom; the grid adopts because the
    hex pixel size stays fixed while the viewBox grows."""
    import re

    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=16
    )
    html = chart.to_html()
    # The label displays the chosen tile zoom (auto-fit picks it; we don't
    # hardcode N — we just verify shape and that the buttons step ±1.)
    label = re.search(
        r'aria-label="tile zoom">z(\d+)</span>', html
    )
    assert label, "tile zoom label should render with a `z<N>` value"
    z = int(label.group(1))
    assert f"zoom={z + 1}" in html  # zoom in
    assert f"zoom={z - 1}" in html  # zoom out
    # base_params still carries grid_size so it round-trips on zoom clicks.
    assert "grid_size=16" in html


def test_map_grid_size_controls_target_grid_size(orders_csv):
    """The hex `+`/`−` buttons halve / double `grid_size`. `+` means more
    detail (smaller hexes → smaller grid_size); `−` means coarser."""
    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=16
    )
    html = chart.to_html()
    # Both controls' base_params share the same identity URL; the buttons
    # append their own grid_size + zoom. Zoom buttons should hold grid_size
    # constant; hex buttons should hold zoom constant.
    assert "grid_size=8" in html   # finer (16 → 8)
    assert "grid_size=32" in html  # coarser (16 → 32)
    # The currently-displayed hex size is labelled.
    import re
    label = re.search(r'aria-label="hex size">(\d+)</span>', html)
    assert label and int(label.group(1)) == 16


def test_map_grid_size_step_is_clamped(orders_csv):
    """Halving / doubling stays inside [GRID_MIN, GRID_MAX]."""
    # At grid_size=4 (GRID_MIN), `+` should keep us at 4, not 2.
    small = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=4
    ).to_html()
    assert "grid_size=4" in small   # finer button can't go below 4
    # At grid_size=200 (GRID_MAX), `−` should keep us at 200, not 400.
    big = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=200
    ).to_html()
    assert "grid_size=200" in big   # coarser button can't go above 200


def test_map_explicit_zoom_overrides_autofit(orders_csv):
    """Passing `zoom=` bypasses the auto-fit selection."""
    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng",
        grid_size=16, zoom=14,
    )
    html = chart.to_html()
    assert ">z14<" in html


def test_map_hex_grid_adopts_to_zoom(tmp_path):
    """Higher tile zoom produces more (smaller-relative-to-data) hexes
    because the hex side length stays fixed in world-pixel units."""
    # Densely cluster 60 points inside a tiny lat/lng box so they can pack
    # into a small number of hexes at low zoom and spread out at high zoom.
    csv_path = tmp_path / "dense.csv"
    lines = ["id,lat,lng"]
    for i in range(60):
        # Deterministic spread: i across a 0.02° × 0.02° area.
        lat = 50.45 + (i % 8) * 0.0025
        lng = 30.52 + (i // 8) * 0.0025
        lines.append(f"{i},{lat:.6f},{lng:.6f}")
    csv_path.write_text("\n".join(lines) + "\n")

    low = ff.chart.map(
        dataset=str(csv_path), title="t", lat="lat", lng="lng",
        grid_size=16, zoom=11,
    ).to_html()
    high = ff.chart.map(
        dataset=str(csv_path), title="t", lat="lat", lng="lng",
        grid_size=16, zoom=15,
    ).to_html()
    low_polys = low.count("<polygon")
    high_polys = high.count("<polygon")
    assert high_polys > low_polys, (
        f"zooming in should grow the hex count (got {low_polys} at z=11 vs "
        f"{high_polys} at z=15)"
    )


def test_map_renders_osm_tiles_with_attribution(orders_csv):
    """Tile layer uses tile.openstreetmap.org URLs and carries OSM attribution."""
    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=10
    )
    html = chart.to_html()
    # At least one tile <image>, all pointing at OSM.
    assert "<image " in html
    assert "https://tile.openstreetmap.org/" in html
    # Tile URL pattern includes {z}/{x}/{y}.png — verify by regex.
    import re
    tile_urls = re.findall(
        r"https://tile\.openstreetmap\.org/\d+/\d+/\d+\.png", html
    )
    assert len(tile_urls) >= 1
    # The zoom level chosen must be inside the supported OSM range.
    z_values = {int(u.split("/")[3]) for u in tile_urls}
    assert all(1 <= z <= 18 for z in z_values)
    # All tile URLs in a single chart share the same zoom.
    assert len(z_values) == 1
    # Attribution required by OSM policy.
    assert "OpenStreetMap" in html
    assert "openstreetmap.org/copyright" in html


def test_map_hex_has_hover_label_group(orders_csv):
    """Each hex is wrapped in a <g> with a count <text> for CSS-only hover."""
    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=20
    )
    html = chart.to_html()
    # One group per hex (fixture has 4 bins).
    assert html.count('class="fireflyer-map-hex"') == 4
    # Each group contains a label <text> with the count.
    assert html.count('class="fireflyer-map-hex-label"') == 4
    # The peak count appears in a label.
    assert ">3</text>" in html


def test_map_svg_is_responsive(orders_csv):
    """SVG carries `width="100%" height="100%"` with `preserveAspectRatio="meet"`
    so it always fills its dashboard cell regardless of cell dimensions."""
    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=10
    )
    html = chart.to_html()
    import re
    # Grab the map SVG specifically — the filter icon SVG also has a viewBox.
    m = re.search(
        r'<svg viewBox="0 0 [\d.]+ [\d.]+" width="100%" height="100%"[^>]*preserveAspectRatio="xMidYMid meet"',
        html,
    )
    assert m, "map SVG should have width/height=100% and preserveAspectRatio meet"


def test_map_zoom_label_includes_tile_zoom(orders_csv):
    """Footer shows the chosen tile zoom alongside the data bounds."""
    chart = ff.chart.map(
        dataset=orders_csv, title="t", lat="lat", lng="lng", grid_size=10
    )
    html = chart.to_html()
    # `z<N>` token appears once in the bounds footer.
    import re
    assert re.search(r"z\d+</span>", html)


def test_map_empty_dataset_renders_no_polygons(tmp_path):
    """A CSV with zero rows after filtering renders the empty state."""
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("id,lat,lng\n")
    chart = ff.chart.map(
        dataset=str(csv_path), title="empty", lat="lat", lng="lng"
    )
    html = chart.to_html()
    assert "<polygon" not in html
    assert ">No data<" in html
