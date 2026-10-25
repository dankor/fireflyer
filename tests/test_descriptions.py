"""Descriptions: optional text on a dashboard, a chart and a dataset.

A dashboard's and a dataset's show as an ⓘ with a tooltip beside the name, in
the editor's toolbar and in the gallery lists; a chart's, and its dataset's, at
the top of the chart's filter panel."""

import json
import html as html_mod
import re
from html import unescape

import pytest

import fireflyer as ff
from fireflyer import inline_data
from fireflyer.dashboard import DashboardError
from fireflyer.datasets import Column, Dataset
from fireflyer.web.portal import (
    DashboardRow, name_cell, render_dataset_detail, render_datasets, render_gallery,
)

_YAML = """name: Sales
description: Weekly revenue, refreshed every Monday.
charts:
  a: {type: table, dataset: sales, title: Rows, description: One row per order.}
layout:
  - ["@30", "a"]
datasets:
  sales:
    description: Orders exported from the shop.
    csv: |
      region,amount
      north,10
      south,5
"""


def _tip(html):
    m = re.search(r'<span class="ff-info"[^>]*data-tip="([^"]*)"', html)
    return unescape(m.group(1)) if m else None


# --- YAML --------------------------------------------------------------------


def test_the_dashboard_reads_its_description():
    assert ff.Dashboard.from_yaml(_YAML).description == "Weekly revenue, refreshed every Monday."
    assert ff.Dashboard.from_yaml(_YAML.replace("description: Weekly", "#")).description == ""


def test_a_description_must_be_text():
    with pytest.raises(DashboardError, match="description` must be text"):
        ff.Dashboard.from_yaml(_YAML.replace("Weekly revenue, refreshed every Monday.", "[1, 2]"))


def test_an_inline_dataset_takes_a_longer_form_with_a_description():
    raw = {"a": "x\n1\n", "b": {"description": " Orders ", "csv": "y\n2\n"}}
    assert inline_data.parse_block(raw) == {"a": "x\n1\n", "b": "y\n2\n"}
    assert inline_data.descriptions(raw) == {"b": "Orders"}


def test_the_longer_form_rejects_a_misspelt_key():
    """`desc:` would otherwise be silently ignored."""
    with pytest.raises(inline_data.InlineDataError, match=r"unknown key\(s\) \['desc'\]"):
        inline_data.parse_block({"b": {"desc": "x", "csv": "y\n2\n"}})


# --- filter panel --------------------------------------------------------------


def test_the_filter_panel_shows_the_charts_and_its_datasets_description():
    html = ff.Dashboard.from_yaml(_YAML).render_cell("a")
    panel = html[html.index('class="fireflyer-filter-panel"'):]
    assert '<p class="fireflyer-filter-about">One row per order.</p>' in panel
    ds = panel[panel.index('class="ds-title"'):panel.index('class="ds-updated"')]
    assert 'class="fireflyer-info"' in ds and 'data-tip="Orders exported from the shop."' in ds
    # The chart's comes first: it's what the viewer is looking at.
    assert panel.index("fireflyer-filter-about") < panel.index("ds-name")


def test_no_description_means_no_empty_blocks():
    bare = _YAML.replace(", description: One row per order.", "")
    html = ff.Dashboard.from_yaml(bare).render_cell("a")
    assert "fireflyer-filter-about" not in html


# --- gallery + editor ------------------------------------------------------------


def _sub(html):
    m = re.search(r'<span class="sub" title="([^"]*)">', html)
    return html_mod.unescape(m.group(1)) if m else None


def test_a_name_cell_puts_the_description_under_the_name():
    cell = name_cell("/d/x", "Sales", 'Says "hi" <b>')
    assert cell.startswith('<a class="name" href="/d/x">Sales</a><span class="sub"')
    assert 'title="Says &quot;hi&quot; &lt;b&gt;"' in cell and "<b>" not in cell
    assert name_cell("/d/x", "Sales") == '<a class="name" href="/d/x">Sales</a>'


def test_the_dashboards_list_shows_each_description():
    row = DashboardRow(id="x", name="Sales", author="dana", yaml=_YAML, updated_at="2026-10-13T10:00:00")
    assert row.description == "Weekly revenue, refreshed every Monday."
    assert _sub(render_gallery([row])) == "Weekly revenue, refreshed every Monday."
    broken = DashboardRow(id="y", name="B", author="", yaml="name: [", updated_at="2026-10-13T10:00:00")
    assert broken.description == ""


def _ds(description="Q3 orders"):
    return Dataset(
        name="orders", description=description, delimiter=",",
        columns=[Column("id", "Int64")], rows=3, author="dana",
        updated_at="2026-07-14T10:00:00",
    )


def test_the_datasets_list_shows_the_description_under_the_name():
    assert _sub(render_datasets([_ds()])) == "Q3 orders"
    # (The dataset's own page: see the in-place edit test below.)
    assert _sub(render_datasets([_ds("")])) is None


def test_the_dataset_page_edits_its_name_and_description_from_its_title():
    """No edit button in its toolbar: the title — the name with the
    description under it — is the control, opening a card with a field for
    each, as a dashboard's title is in the editor."""
    page = render_dataset_detail(_ds())
    topbar = page[page.index("<header"):page.index("</header>")]
    name = re.search(r'<span class="tname"[^>]*>', topbar).group(0)
    assert "contenteditable" not in topbar
    assert re.search(r'<span class="thead" id="ds-title-head" role="button"', topbar)
    assert 'id="ds-title-sub">Q3 orders<' in topbar
    assert 'data-action="/datasets/orders/rename"' in name
    assert 'id="ds-title-about" role="dialog"' in topbar
    assert 'id="ds-title-name-input"' in topbar and "<textarea" in topbar
    assert 'title="Edit"' not in topbar and "openDsRename" not in page
    # No ⓘ either — a click on the name shows the description; the name carries it.
    assert _tip(topbar) is None and 'data-description="Q3 orders"' in name
