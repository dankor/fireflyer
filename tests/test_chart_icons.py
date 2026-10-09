"""Guard: every chart type ships an icon.

The icon names a chart wherever its type is shown — the filter panel's source
column and the editor's chart-type picker — so a new chart without one would
leave a blank there. It lives as `icon.svg` beside `chart.css`, and is drawn in
`currentColor` so it follows whatever colour the surrounding text has.
"""

from pathlib import Path

from fireflyer.dashboard import CHART_TYPES

CHART_DIR = Path(__file__).resolve().parents[1] / "fireflyer" / "chart"


def test_every_chart_folder_has_an_icon_file():
    for name in CHART_TYPES:
        assert (CHART_DIR / name / "icon.svg").is_file(), f"{name} has no icon.svg"


def test_every_chart_class_carries_its_icon():
    for name, cls in CHART_TYPES.items():
        icon = cls.ICON
        assert icon.startswith("<svg") and icon.endswith("</svg>"), name
        assert 'viewBox="0 0 16 16"' in icon, name
        # Themed by the text around it, never a fixed colour.
        assert 'stroke="currentColor"' in icon, name
