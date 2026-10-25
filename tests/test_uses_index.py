"""What each dashboard reads — `(dataset, column)` pairs, worked out on save and
kept by the store — so the dataset/dashboard overviews are a lookup, not a parse
of every dashboard. Both stores: the portal's SQL table and paths mode's file."""

import os

import pytest

from fireflyer.web.paths import PathDashboardStore
from fireflyer.web.portal import SqliteStore

PIE = """name: Sales
calcs:
  orders:
    revenue: {agg: sum, formula: amount}
charts:
  p: {type: pie, dataset: orders, title: P, column: status, calc: revenue}
layout:
  - ["@30", "p"]
"""
RAW = """name: Raw
charts:
  t: {type: table, dataset: orders, title: T}
layout:
  - ["@30", "t"]
"""
NONE = 'name: Empty\ncharts: {}\nlayout: []\n'


def _pairs(store):
    return sorted((u.dashboard_name, u.dataset, u.column) for u in store.uses())


@pytest.fixture(params=["sql", "paths"])
def store(request, tmp_path):
    if request.param == "sql":
        return SqliteStore(":memory:")
    return PathDashboardStore(str(tmp_path / "path"), index=str(tmp_path / "data" / "uses.json"))


def test_a_saved_dashboard_says_what_it_reads(store):
    store.create(PIE, "dana")
    store.create(RAW, "dana")
    store.create(NONE, "dana")                          # reads nothing: no rows
    assert _pairs(store) == [
        ("Raw", "orders", ""), ("Raw", "orders", "*"),  # a raw table: every column
        ("Sales", "orders", ""), ("Sales", "orders", "amount"), ("Sales", "orders", "status"),
    ]


def test_saving_and_deleting_keep_it_true(store):
    id = store.create(PIE, "dana")
    store.save(id, PIE.replace("column: status", "column: channel"))
    assert ("Sales", "orders", "channel") in _pairs(store) and ("Sales", "orders", "status") not in _pairs(store)
    store.delete(id)
    assert _pairs(store) == []


def test_a_dashboard_from_before_the_index_is_indexed_when_read():
    store = SqliteStore(":memory:")
    store.create(PIE, "dana")
    store._run("DELETE FROM dashboard_uses", ())
    assert ("Sales", "orders", "status") in _pairs(store)


def test_a_dashboard_file_changed_outside_the_app_is_indexed_again(tmp_path):
    """Paths mode: the files are yours — an edit or a `git pull` isn't a save."""
    store = PathDashboardStore(str(tmp_path / "path"), index=str(tmp_path / "data" / "uses.json"))
    stem = store.create(PIE)
    assert ("Sales", "orders", "status") in _pairs(store)
    file = tmp_path / "path" / "dashboards" / f"{stem}.yaml"
    file.write_text(PIE.replace("column: status", "column: segment"))
    os.utime(file, (file.stat().st_atime, file.stat().st_mtime + 5))
    assert ("Sales", "orders", "segment") in _pairs(store)
    file.unlink()
    assert _pairs(store) == []
