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
charts:
  kpi: {{type: number, dataset: orders, title: {title}}}
layout:
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
    # per-row actions (the name opens it — no edit button) + top add button +
    # clone/add dialogs
    assert 'class="name" href="/d/' in html and 'title="Edit"' not in html
    assert 'title="Clone"' in html and 'title="Remove"' in html
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


# --- datasets gallery rendering ---------------------------------------------

from fireflyer.datasets import Column, Dataset
from fireflyer.web.portal import render_dataset_detail, render_datasets


def _ds(name="orders"):
    return Dataset(
        name=name, description="Q3 orders", delimiter=",",
        columns=[Column("id", "Int64"), Column("status", "String")],
        rows=3, author="dana", updated_at="2026-07-14T10:00:00",
    )


def test_render_datasets_table():
    html = render_datasets([_ds()])
    # Overview nav is the Dashboards | Datasets switch, Datasets segment active.
    assert 'class="ff-switch"' in html
    assert '<a class="ff-switch-seg active" href="/datasets">Datasets</a>' in html
    assert ">orders<" in html and ">dana<" in html
    assert "openUpload" in html


def test_render_datasets_escapes_name():
    html = render_datasets([_ds(name="<script>x</script>")])
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_render_datasets_empty_state():
    assert "No datasets yet" in render_datasets([])


def test_render_dataset_detail_is_its_columns_without_a_data_preview():
    html = render_dataset_detail(_ds())
    assert 'id="ds-columns"' in html
    assert "ds-preview" not in html and 'class="preview"' not in html


def test_render_dataset_detail_delete_when_unused():
    html = render_dataset_detail(_ds())   # used_by empty
    assert 'action="/datasets/orders/delete"' in html    # trash deletes
    assert "usage-dialog" not in html


def test_render_dataset_detail_usage_lists_dashboards():
    html = render_dataset_detail(_ds(), used_by=[("id1", "Sales"), ("id2", "Ops")])
    assert 'class="badge">2<' in html                     # count badge on the trash icon
    assert 'href="/d/id1" target="_blank"' in html        # opens in a new tab
    assert ">Sales<" in html and ">Ops<" in html
    assert 'class="warn"' not in html                     # no red text


def test_the_lists_name_authors_only_where_there_is_a_login(store):
    """Local mode has no login, so an Author column would read "—" on every row."""
    store.create(VALID_YAML, author="dana")
    assert ">Author</th>" in render_gallery(store.list())
    local = render_gallery(store.list(), authors=False)
    assert ">Author</th>" not in local and ">dana<" not in local and "<td>—</td>" not in local


def test_each_dashboard_links_the_datasets_it_reads(store):
    """One that isn't uploaded shows, unlinked, so it stands out."""
    store.create(VALID_YAML, author="dana")                              # reads `orders`
    store.create(_yaml(name="Old").replace("dataset: orders", "dataset: gone"), author="dana")
    html = render_gallery(store.list(), datasets={"orders"})
    assert ">Datasets</th>" in html
    assert '<a class="ref" href="/datasets/orders" title="orders">orders</a>' in html
    assert '<span class="ref is-missing" title="Not uploaded">gone</span>' in html


def test_each_dataset_links_the_dashboards_reading_it():
    html = render_datasets([_ds()], used_by={"orders": [("id1", "Sales"), ("id2", "Ops")]})
    assert ">Dashboards</th>" in html
    assert 'href="/d/id1"' in html and ">Ops</a>" in html
    # Plain local mode has no dashboards to look at: no column.
    assert ">Dashboards</th>" not in render_datasets([_ds()])
