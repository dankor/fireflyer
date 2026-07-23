"""Paths-mode wiring + demo seeding — the request-scoped store selection and
first-run seed that live in `app.py`, exercised directly (no web stack, no HTTP).

The routes are thin callers of these helpers; here we drive the helpers with a
tiny stub request (they only read `request.cookies`), so the logic is covered
without pulling in a test HTTP client.
"""

import pytest

from fireflyer.datasets import DatasetStore
from fireflyer.storage import make_object_store
from fireflyer.web import app as app_mod
from fireflyer.web.paths import PathDashboardStore

YAML = """name: Sales
charts:
  a: {type: table, dataset: orders, title: A}
dashboard:
  - ["@20", "a"]
"""


class _Req:
    """Just enough of a request for the store helpers — they only read cookies."""

    def __init__(self, cookies=None):
        self.cookies = cookies or {}


@pytest.fixture
def paths_mode(tmp_path, monkeypatch):
    """Turn on paths mode against tmp dirs: a paths base + an isolated data base,
    with the portal store off so `_paths_mode()` is active."""
    base = tmp_path / "paths"
    base.mkdir()
    monkeypatch.setattr(app_mod, "PATHS_BASE", str(base))
    monkeypatch.setattr(app_mod, "_DATA_BASE", str(tmp_path / "data"))
    monkeypatch.setattr(app_mod.app.state, "store", None)
    return base


def _dataset_store_for(data_base, path):
    return DatasetStore(make_object_store({"base": f"{data_base}/{path}"}))


def test_paths_mode_on_only_when_base_set_and_no_portal(paths_mode, monkeypatch):
    assert app_mod._paths_mode() is True
    monkeypatch.setattr(app_mod, "PATHS_BASE", "")
    assert app_mod._paths_mode() is False


def test_active_path_defaults_to_first_and_honours_cookie(paths_mode):
    (paths_mode / "alpha").mkdir()
    (paths_mode / "beta").mkdir()
    assert app_mod._active_path(_Req()) == "alpha"  # sorted, first
    assert app_mod._active_path(_Req({"ff_path": "beta"})) == "beta"
    # A stale/forged cookie pointing outside the mapped folders falls back.
    assert app_mod._active_path(_Req({"ff_path": "../secret"})) == "alpha"


def test_dash_store_is_per_path(paths_mode):
    (paths_mode / "alpha").mkdir()
    (paths_mode / "beta").mkdir()
    app_mod._dash_store(_Req({"ff_path": "alpha"})).create(YAML)
    assert [r.name for r in app_mod._dash_store(_Req({"ff_path": "alpha"})).list()] == ["Sales"]
    assert app_mod._dash_store(_Req({"ff_path": "beta"})).list() == []


def test_dataset_store_is_isolated_per_path(paths_mode, tmp_path):
    (paths_mode / "alpha").mkdir()
    (paths_mode / "beta").mkdir()
    csv = b"city,n\nNY,1\nLA,2\n"
    app_mod._dataset_store(_Req({"ff_path": "alpha"})).create("cities", csv)
    assert app_mod._dataset_store(_Req({"ff_path": "alpha"})).get("cities") is not None
    assert app_mod._dataset_store(_Req({"ff_path": "beta"})).get("cities") is None


def test_gallery_kwargs_lists_paths_and_active(paths_mode):
    (paths_mode / "alpha").mkdir()
    (paths_mode / "beta").mkdir()
    kw = app_mod._gallery_kwargs(_Req({"ff_path": "beta"}))
    assert kw == {"paths": ["alpha", "beta"], "active_path": "beta"}


def test_gallery_kwargs_empty_outside_paths_mode(paths_mode, monkeypatch):
    monkeypatch.setattr(app_mod, "PATHS_BASE", "")
    assert app_mod._gallery_kwargs(_Req()) == {}


def test_seed_demo_path_creates_dashboard_and_dataset(paths_mode):
    app_mod._seed_demo_path()
    # The starter dashboard lands in the demo path.
    rows = PathDashboardStore(str(paths_mode / "demo")).list()
    assert [r.name for r in rows] == ["Orders overview"]
    # The dataset it references is seeded in the demo path's isolated blob store.
    demo_ds = _dataset_store_for(app_mod._DATA_BASE, "demo")
    assert demo_ds.get("orders") is not None


def test_seed_demo_path_is_non_destructive(paths_mode):
    app_mod._seed_demo_path()
    store = PathDashboardStore(str(paths_mode / "demo"))
    stem = store.list()[0].id
    store.delete(stem)  # user removes the demo dashboard on purpose
    app_mod._seed_demo_path()  # a later boot must not resurrect it
    assert store.list() == []


def test_seed_demo_path_noop_outside_paths_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "PATHS_BASE", "")
    monkeypatch.setattr(app_mod, "_DATA_BASE", str(tmp_path / "data"))
    app_mod._seed_demo_path()  # must be a silent no-op, not raise
    assert not (tmp_path / "data").exists()
