"""Portal store + gallery tests. Exercises `fireflyer.web.portal` directly with
an in-memory sqlite store — no web stack, no live Postgres (same rule as the
chat tests). Postgres is only used at portal runtime; here sqlite stands in."""

import pytest

from fireflyer.dashboard import DashboardError
from fireflyer.web.portal import SqlStore, database_url, render_gallery

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
    return SqlStore(":memory:")


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

    renamed = store.save(new_id, _yaml(name="Revenue", title="Total"))

    # The name is the key: the edited `name:` moves the dashboard to it.
    assert renamed == "Revenue" and store.get(new_id) is None
    row = store.get(renamed)
    assert row.id == row.name == "Revenue"
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
    store.create(_yaml(name='"<b onmouseover=alert(1)>x"'), author="dana")
    html = render_gallery(store.list())
    assert "<b onmouseover" not in html
    assert "&lt;b onmouseover=alert(1)&gt;x" in html


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
    html = render_datasets([_ds()], group="sales")
    # Overview nav is the Dashboards | Datasets switch, Datasets segment active
    # — both in the group being looked at.
    assert 'class="ff-switch"' in html
    assert '<a class="ff-switch-seg active" href="/datasets/sales">Datasets</a>' in html
    assert '<a class="ff-switch-seg" href="/d/sales">Dashboards</a>' in html
    assert '<td class="muted">sales</td>' in html                       # the Group column
    assert ">orders<" in html and ">dana<" in html
    assert "openUpload" in html


def test_human_size():
    from fireflyer.web.portal import human_size

    assert [human_size(n) for n in (0, 820, 4300, 38_000, 1_480_000, 5 * 1024**3)] == [
        "0 B", "820 B", "4.2 KB", "37 KB", "1.4 MB", "5.0 GB"]


def test_render_datasets_shows_each_ones_size():
    ds = _ds()
    ds.size = 1_480_000
    html = render_datasets([ds])
    assert ">Size</th>" in html
    assert '<span title="Stored, every upload and the built data: 1,480,000 bytes">1.4 MB</span>' in html


def test_history_menu_shows_each_uploads_size_once():
    from fireflyer.datasets import Version
    from fireflyer.web.portal import _history_menu

    ds = _ds()
    ds.version, ds.data_size = "2.1", 512
    versions = [Version(2, 1, 0, "", "ready", size=2048), Version(2, 0, 0, "", "ready", size=2048),
                Version(1, 0, 0, "", "ready", size=900)]
    html = _history_menu(ds, versions)
    # On the upload's own row (its first version), not on the remap after it.
    assert html.count("Uploaded file: 2,048 bytes") == 1 and ">2.0 KB<" in html
    assert html.index('data-version="2.0"') < html.index("2.0 KB") < html.index('data-version="1.0"')
    assert "Uploaded file: 900 bytes" in html
    # The built data: on the version in use alone, beside its check.
    assert html.count("Built data:") == 1
    row = html[html.index('data-version="2.1"'):html.index('data-version="2.0"')]
    assert "Built data: 512 bytes" in row and 'title="In use"' in row


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
    assert 'action="/datasets/local/orders/delete"' in html    # trash deletes
    assert "usage-dialog" not in html


def test_removing_a_dataset_says_which_other_groups_dashboards_lock():
    html = render_dataset_detail(_ds(), group="sales", elsewhere=[("/d/mkt/Q", "mkt:Q")])
    assert "usage-dialog" not in html and "will lock:\\n  mkt:Q" in html


def test_render_dataset_detail_usage_lists_dashboards():
    html = render_dataset_detail(_ds(), used_by=[("/d/local/Sales", "Sales"), ("/d/local/Ops", "Ops")])
    assert 'class="badge">2<' in html                     # count badge on the trash icon
    assert 'href="/d/local/Sales" target="_blank"' in html  # opens in a new tab
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
    store.create(_yaml(name="Cross").replace("dataset: orders", "dataset: sales:orders"))
    html = render_gallery(store.list(), datasets={"orders", "sales:orders"}, group="_admin")
    assert ">Datasets</th>" in html
    assert '<a class="ref" href="/datasets/_admin/orders" title="orders">orders</a>' in html
    assert '<span class="ref is-missing" title="Not uploaded">gone</span>' in html
    # Another group's, with its group in front.
    assert 'href="/datasets/sales/orders" title="sales:orders">sales:orders</a>' in html


def test_each_dataset_links_the_dashboards_reading_it():
    html = render_datasets([_ds()], used_by={"orders": [("/d/local/Sales", "Sales"), ("/d/mkt/Ops", "mkt:Ops")]})
    assert ">Dashboards</th>" in html
    assert 'href="/d/local/Sales"' in html and ">mkt:Ops</a>" in html
    # Plain local mode has no dashboards to look at: no column.
    assert ">Dashboards</th>" not in render_datasets([_ds()])


def test_every_list_puts_its_add_button_at_the_head_of_the_actions_column():
    from fireflyer.web.portal import list_table, render_access, render_datasets, render_users

    for html, title in [(render_gallery([]), "New dashboard"), (render_datasets([]), "New dataset"),
                        (render_access([]), "New role"), (render_users([], []), "New user")]:
        assert f'<th class="actions"><button class="act" type="button" title="{title}"' in html
        assert 'class="topbar-right"><button class="act"' not in html     # not in the topbar
    empty = list_table(["Name", "Roles"], [], "No users yet", "+")
    assert '<td class="empty-row" colspan="3">No users yet</td>' in empty


# --- the database: Postgres only ------------------------------------------------


@pytest.mark.parametrize("env", [{}, {"DATABASE_URL": "portal.db"}, {"DATABASE_URL": "sqlite:///x.db"}])
def test_portal_mode_wont_start_without_postgres(env):
    with pytest.raises(SystemExit, match="portal mode needs Postgres"):
        database_url(env)


def test_portal_mode_runs_on_a_postgres_url():
    for url in ("postgresql://u:p@db:5432/f", "postgres://u:p@db/f"):
        assert database_url({"DATABASE_URL": url}) == url


def test_the_portal_entrypoint_stops_before_serving_without_postgres(monkeypatch, tmp_path):
    from fireflyer import portal as entrypoint

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FIREFLYER_PORTAL", raising=False)        # main() sets it; put back after
    monkeypatch.setenv("FIREFLYER_PORTAL_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *a, **k: pytest.fail("served without Postgres"))
    with pytest.raises(SystemExit, match="portal mode needs Postgres"):
        entrypoint.main()
