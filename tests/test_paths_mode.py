"""Paths-mode wiring + demo seeding — the request-scoped store selection and
first-run seed that live in `app.py`, exercised directly (no web stack, no HTTP).

The routes are thin callers of these helpers; here we drive the helpers with a
tiny stub request (they only read `request.cookies`), so the logic is covered
without pulling in a test HTTP client.
"""

import pytest

from fireflyer.web import app as app_mod
from fireflyer.web.paths import PathDashboardStore

YAML = """name: Sales
charts:
  a: {type: table, dataset: orders, title: A}
layout:
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


def test_the_demo_folder_is_a_usable_path():
    """The `demo` path is the repo's `demo/` folder, mapped in by compose —
    nothing is copied or seeded, so what you open in the browser is the file in
    git. That only works if the folder is laid out the way a path is."""
    from pathlib import Path

    demo = Path(__file__).resolve().parent.parent / "demo"
    rows = PathDashboardStore(str(demo)).list()
    assert [r.name for r in rows] == ["Orders overview"]


def test_the_demo_dashboard_renders_with_no_store():
    """It carries its own data inline, so a fresh install opens on a working
    example with an empty Datasets tab and nothing uploaded."""
    from pathlib import Path

    import fireflyer as ff

    demo = Path(__file__).resolve().parent.parent / "demo"
    store = PathDashboardStore(str(demo))
    dashboard = ff.Dashboard.from_yaml(store.get(store.list()[0].id).yaml)
    assert len(dashboard.chart_configs) > 5
    for cid in dashboard.chart_configs:
        assert "chart-error" not in dashboard.render_cell(cid, cf_tokens=[]), cid


def test_the_app_serves_that_same_file():
    """`DEFAULT_YAML` (plain local mode's starter) is read from the folder, not
    embedded — the two cannot drift because there is only one copy."""
    from pathlib import Path

    demo = Path(__file__).resolve().parent.parent / "demo"
    on_disk = (demo / "dashboards" / "orders-overview.yaml").read_text()
    assert app_mod.DEFAULT_YAML == on_disk


def test_compose_mounts_the_demo_folder_where_the_app_reads_it():
    """`app.py` reads the starter dashboard from `/app/demo` at import. The
    compose file mounts the source over the image, so a service that mounts
    `./fireflyer` must mount `./demo` too — otherwise the container starts
    against an image copy that its own source has outgrown, or against nothing
    at all, and dies at import. That happened, and only in Docker.
    """
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())

    # The path app.py resolves to, expressed the way compose spells it.
    read_from = "/app/demo"
    checked = 0
    for name, service in compose["services"].items():
        mounts = service.get("volumes") or []
        if not any(m.endswith("/app/fireflyer") for m in mounts):
            continue                       # not a service that runs the app
        checked += 1
        assert any(m.endswith(f":{read_from}") for m in mounts), (
            f"service {name!r} mounts the source but not {read_from}"
        )
    assert checked, "expected at least one service running the app"
