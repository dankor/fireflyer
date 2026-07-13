"""Portal store + gallery tests. Exercises `fireflyer.web.portal` directly with
an in-memory sqlite store — no web stack, no live Postgres (same rule as the
chat tests). Postgres is only used at portal runtime; here sqlite stands in."""

import pytest

from fireflyer.dashboard import DashboardError
from fireflyer.web.portal import SqliteStore, render_gallery

# Minimal valid dashboard. `from_yaml` only parses (it never opens the CSV), so
# the dataset path need not exist for validation to pass. The listing name comes
# from the top-level `name:` key — the store never takes a separate name.
def _yaml(name: str = "Sales", title: str = "KPI") -> str:
    return f"""name: {name}
datasets:
  o: {{path: x.csv}}
charts:
  kpi: {{type: number, dataset: o, title: {title}, column: amount, agg: sum}}
dashboard:
  Main:
    - ["@100", "kpi"]
"""


VALID_YAML = _yaml()
INVALID_YAML = "charts: [not, a, dashboard]"


@pytest.fixture
def store():
    return SqliteStore(":memory:")


def test_create_derives_name_from_yaml(store):
    new_id = store.create(VALID_YAML)

    rows = store.list()
    assert [r.name for r in rows] == ["Sales"]  # from the `name:` key
    assert rows[0].id == new_id
    assert store.get(new_id).yaml == VALID_YAML


def test_create_rejects_missing_name_key(store):
    no_name = VALID_YAML.replace("name: Sales\n", "")
    with pytest.raises(DashboardError):
        store.create(no_name)
    assert store.list() == []


def test_get_missing_returns_none(store):
    assert store.get("does-not-exist") is None


def test_create_records_author(store):
    new_id = store.create(VALID_YAML, author="dana")
    assert store.get(new_id).author == "dana"


def test_save_updates_name_and_yaml_but_keeps_author(store):
    new_id = store.create(VALID_YAML, author="dana")

    store.save(new_id, _yaml(name="Revenue", title="Total"))

    row = store.get(new_id)
    assert row.name == "Revenue"  # re-derived from the edited `name:` key
    assert "Total" in row.yaml
    assert row.author == "dana"  # author (creator) is preserved across saves


def test_delete_removes_row(store):
    new_id = store.create(VALID_YAML)
    store.delete(new_id)
    assert store.list() == []


def test_create_rejects_invalid_yaml(store):
    with pytest.raises(DashboardError):
        store.create(INVALID_YAML)
    assert store.list() == []


def test_save_rejects_invalid_yaml(store):
    new_id = store.create(VALID_YAML)
    with pytest.raises(DashboardError):
        store.save(new_id, INVALID_YAML)
    # The bad save left the stored YAML untouched.
    assert store.get(new_id).yaml == VALID_YAML


def test_gallery_is_a_table_with_author_and_actions(store):
    store.create(VALID_YAML, author="dana")
    html = render_gallery(store.list())
    assert "<table" in html
    for header in ("Name", "Author", "Last updated"):
        assert f">{header}</th>" in html
    assert ">dana<" in html
    # per-row actions + top add button + clone/add dialogs
    assert "Edit" in html and "Clone" in html and "Remove" in html
    assert "openAdd()" in html and 'id="add-dialog"' in html
    assert "openClone(this)" in html and 'id="clone-dialog"' in html


def test_gallery_escapes_dashboard_names(store):
    store.create(_yaml(name='"<script>alert(1)</script>"'), author="dana")
    html = render_gallery(store.list())
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_gallery_empty_state():
    html = render_gallery([])
    assert "No dashboards yet" in html
