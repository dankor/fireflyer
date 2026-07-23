"""Local paths-mode dashboard store tests — dashboards as YAML files in a path
folder, no web stack."""

import pytest

from fireflyer.dashboard import DashboardError
from fireflyer.web.paths import PathDashboardStore, list_paths

YAML = """name: Sales
charts:
  a: {type: table, dataset: orders, title: A}
dashboard:
  - ["@20", "a"]
"""


@pytest.fixture
def store(tmp_path):
    return PathDashboardStore(str(tmp_path / "path"))


def test_list_paths(tmp_path):
    base = tmp_path / "paths"
    (base / "projectA").mkdir(parents=True)
    (base / "projectB").mkdir()
    (base / ".hidden").mkdir()
    (base / "note.txt").write_text("x")
    assert list_paths(str(base)) == ["projectA", "projectB"]


def test_list_paths_missing_base(tmp_path):
    assert list_paths(str(tmp_path / "nope")) == []


def test_create_writes_file_named_from_slug(store, tmp_path):
    stem = store.create(YAML)
    assert stem == "sales"
    assert (tmp_path / "path" / "dashboards" / "sales.yaml").exists()
    assert [r.id for r in store.list()] == ["sales"]
    assert store.list()[0].name == "Sales"  # display name from the `name:` key


def test_create_suffixes_on_collision(store):
    a = store.create(YAML)
    b = store.create(YAML)
    assert a == "sales" and b == "sales-2"


def test_get_and_save_roundtrip(store):
    stem = store.create(YAML)
    store.save(stem, YAML.replace("name: Sales", "name: Revenue"))
    row = store.get(stem)
    assert row.name == "Revenue"
    assert "Revenue" in row.yaml


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


def test_create_and_save_reject_invalid_yaml(store):
    with pytest.raises(DashboardError):
        store.create("charts: [not a dashboard]")
    stem = store.create(YAML)
    with pytest.raises(DashboardError):
        store.save(stem, "charts: bad")


def test_delete_removes_file(store):
    stem = store.create(YAML)
    store.delete(stem)
    assert store.list() == []


def test_path_traversal_rejected(store):
    store.create(YAML)
    assert store.get("../secret") is None
    with pytest.raises(DashboardError):
        store.save("../evil", YAML)
