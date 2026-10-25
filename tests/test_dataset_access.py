"""A dataset's permissions — the roles that may read it, each full or filtered,
kept in its mapping YAML under `permissions:`, so versioned with the columns —
and viewing a dashboard as a role: its charts narrowed by the role's filters,
locked where it has no access."""

import re

import pytest

from fireflyer import column_map
from starlette.datastructures import FormData
from starlette.testclient import TestClient

from fireflyer.dashboard import Dashboard
from fireflyer.web import app as app_mod
from fireflyer.web.portal import (
    RoleStore, access_filter_row, parse_access_form, render_access_form, role_picker,
)

PAID = [{"column": "status", "op": "in", "values": ["paid"]}]


@pytest.fixture
def roles():
    return RoleStore(":memory:")


@pytest.fixture
def datasets(tmp_path, orders_parquet):
    from fireflyer.datasets import DatasetStore
    from fireflyer.storage import make_object_store

    store = DatasetStore(make_object_store({"base": str(tmp_path / "ds")}))
    csv = open(orders_parquet.replace(".parquet", ".csv"), "rb").read()
    store.create("orders", csv)
    store.create("users", csv)
    return store


# --- storage: in the mapping, versioned -----------------------------------------------


def grant(datasets, name, perms):
    """Save `perms` as the dataset's next mapping version, as the page's Save does."""
    datasets.remap(name, column_map.with_permissions(datasets.mapping(name), perms))


def test_permissions_live_in_the_mapping_yaml_and_are_versioned(datasets):
    grant(datasets, "orders", {"viewer": PAID, "admin": []})
    mapping = datasets.mapping("orders")
    assert "permissions:\n  viewer:\n    - {column: status, op: in, values: [paid]}\n  admin: full\n" in mapping
    assert datasets.permissions("orders") == {"viewer": PAID, "admin": []}
    assert datasets.get("orders").version == "1.1"            # a permission change is the next minor

    grant(datasets, "orders", {"admin": []})                   # viewer loses it
    assert datasets.permissions("orders") == {"admin": []}
    # Restoring the earlier version brings its permissions back with it.
    assert column_map.permissions(datasets.restored("orders", "1.1")) == {"viewer": PAID, "admin": []}


def test_a_column_edit_keeps_the_permissions(datasets):
    grant(datasets, "orders", {"viewer": PAID})
    renamed = datasets.mapping("orders").replace("  amount:\n    formula: amount", "  total:\n    formula: amount")
    datasets.remap("orders", renamed)
    assert datasets.permissions("orders") == {"viewer": PAID}


def test_a_new_upload_carries_the_permissions_forward(datasets, orders_parquet):
    grant(datasets, "orders", {"viewer": PAID})
    datasets.replace("orders", open(orders_parquet.replace(".parquet", ".csv"), "rb").read())
    assert datasets.get("orders").version == "2.1"            # the minor holds: nothing changed in the mapping
    assert datasets.permissions("orders") == {"viewer": PAID}


def test_permissions_kept_beside_the_versions_move_into_the_mapping(datasets):
    import yaml

    doc = yaml.safe_load(datasets._catalog.load("orders"))
    doc["permissions"] = {"viewer": PAID, "admin": "full"}
    datasets._catalog.save("orders", yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))
    assert datasets.permissions("orders") == {"viewer": PAID, "admin": []}
    assert "permissions" not in yaml.safe_load(datasets._catalog.load("orders"))


def test_a_roles_grants_gather_every_dataset_naming_it(datasets):
    grant(datasets, "orders", {"viewer": PAID})
    grant(datasets, "users", {"viewer": [], "admin": []})
    assert datasets.grants("viewer") == {"orders": PAID, "users": []}
    assert datasets.grants("guest") == {}


def test_permissions_follow_a_renamed_dataset(datasets):
    grant(datasets, "orders", {"viewer": PAID})
    datasets.rename("orders", "sales")
    assert datasets.grants("viewer") == {"sales": PAID}


def test_a_renamed_or_removed_role_is_renamed_or_removed_in_every_dataset(datasets):
    grant(datasets, "orders", {"viewer": PAID, "admin": []})
    grant(datasets, "users", {"viewer": []})
    datasets.rename_role("viewer", "reader")
    assert datasets.grants("reader") == {"orders": PAID, "users": []}
    datasets.drop_role("reader")
    assert datasets.permissions("orders") == {"admin": []}
    assert datasets.permissions("users") == {}


def test_a_malformed_permission_is_refused(datasets):
    bad = datasets.mapping("orders") + "permissions:\n  viewer:\n    - {column: status, op: like, values: [x]}\n"
    with pytest.raises(Exception, match="permissions of 'viewer'"):
        datasets.remap("orders", bad)


# --- the form ------------------------------------------------------------------------


def _form(*items):
    return FormData(list(items))


def test_the_form_reads_back_each_roles_filters():
    form = _form(
        ("role", "b"),
        ("role", "c"), ("filter_column", "status"), ("filter_op", "in"), ("filter_value", "paid"),
    )
    assert parse_access_form(form) == {"b": [], "c": PAID}       # no filters: full


def test_a_filter_reads_as_a_compact_row_carrying_its_parts():
    html = access_filter_row(PAID[0], {"status": "text"})
    assert 'class="col-name" title="status">status<' in html
    assert '<input type="hidden" name="filter_value" value="paid">' in html
    assert 'class="vals" title="paid">paid<' in html
    assert "ds-filter-edit" in html and "ds-filter-remove" in html


def test_the_form_lists_only_the_roles_with_access(roles):
    for name in ("viewer", "admin", "guest"):
        roles.create_role(name)
    html = render_access_form(
        roles.roles(), {"viewer": PAID, "admin": []},
        lambda f: f"<tr class=\"ds-filter\"><td>{f['column']}</td></tr>", "<i>draft</i>",
    )
    form = html.split("<template")[0]
    listed = re.findall(r'<tr class="ds-access-row is-(\w+)" data-role="(\w+)"', form)
    assert sorted(listed, key=lambda x: x[1]) == [("full", "admin"), ("filter", "viewer")]
    assert "<th>Role</th><th>Filters</th>" in form                # headers
    assert '<td>status</td>' in form and form.count("<i>draft</i>") == 2
    # The + offers the rest: guest only.
    offers = re.findall(r'<button type="button" data-role="(\w+)"( hidden)?>', form)
    assert [r for r, hidden in offers if not hidden] == ["guest"]
    assert 'class="ff-colpick-item ds-full"' in html


def test_the_plus_stays_when_every_role_has_access(roles):
    roles.create_role("viewer")
    html = render_access_form(roles.roles(), {"viewer": []}, lambda f: "", "")
    head = html[:html.index("</thead>")]
    assert '<details class="ds-access-add">' in head                   # in the header, always
    assert "Every role has access" not in html
    assert 'placeholder="New role"' in html                            # made right here


def test_the_view_as_picker_lists_every_role_and_no_none(roles):
    assert role_picker([]) == ""
    roles.create_role("viewer")
    html = role_picker(roles.roles())
    assert re.findall(r'data-role="[^"]*"[^>]*>(\w+)<', html) == ["viewer"]


# --- viewing a dashboard as a role -----------------------------------------------------


def _dashboard(orders_parquet, inline=False):
    data = "\ndatasets:\n  inl: |\n    status\n    paid\n    pending\n" if inline else ""
    return Dashboard.from_yaml(f"""name: T
charts:
  rows: {{type: table, dataset: {orders_parquet}, title: Rows}}
  own: {{type: table, dataset: inl, title: Own}}
layout:
  - ["@40", "rows", "own"]
{data}""" if inline else f"""name: T
charts:
  rows: {{type: table, dataset: {orders_parquet}, title: Rows}}
layout:
  - ["@40", "rows"]
""")


def _statuses(html):
    return set(re.findall(r"<td[^>]*>(paid|pending|cancelled|shipped)</td>", html))


def test_a_role_sees_only_the_rows_its_filters_let_through(orders_parquet):
    dash = _dashboard(orders_parquet).as_role("viewer", {orders_parquet: PAID})
    html = dash.render_cell("rows")
    assert _statuses(html) == {"paid"}
    # Shown in the panel as the role's own row, which can't be removed.
    assert 'class="src-access"' in html
    # Its source: the role's icon and name, as a chart's row shows its own.
    assert '</svg><span class="tip-name">viewer</span>' in html and "Access" not in html


def test_full_access_narrows_nothing(orders_parquet):
    everyone = _statuses(_dashboard(orders_parquet).render_cell("rows"))
    full = _statuses(_dashboard(orders_parquet).as_role("admin", {orders_parquet: []}).render_cell("rows"))
    assert full == everyone and len(everyone) > 1


def test_no_access_locks_the_chart(orders_parquet):
    html = _dashboard(orders_parquet).as_role("guest", {}).render_cell("rows")
    assert "fireflyer-no-access" in html and _statuses(html) == set()


def test_a_role_filter_on_a_missing_column_locks_rather_than_shows_all(orders_parquet):
    grants = {orders_parquet: [{"column": "nope", "op": "in", "values": ["x"]}]}
    html = _dashboard(orders_parquet).as_role("viewer", grants).render_cell("rows")
    assert "fireflyer-no-access" in html


def test_inline_data_is_the_dashboards_own_and_never_locked(orders_parquet):
    dash = _dashboard(orders_parquet, inline=True).as_role("guest", {})
    assert "fireflyer-no-access" not in dash.render_cell("own")


# --- routes ------------------------------------------------------------------------------


_YAML = """name: Route
charts:
  rows: {type: table, dataset: orders, title: Rows}
layout:
  - ["@40", "rows"]
"""


@pytest.fixture
def client(monkeypatch, datasets):
    monkeypatch.setattr(app_mod, "_dataset_store", lambda request: datasets)
    monkeypatch.setattr(app_mod, "_every_dataset_store", lambda: [datasets])
    return TestClient(app_mod.app)


def test_the_permissions_form_reads_and_writes_the_posted_mapping(client, datasets):
    app_mod.app.state.roles.create_role("viewer")
    base = datasets.mapping("orders")
    form = {"mapping": base, "role": "viewer", "filter_column": "status", "filter_op": "in", "filter_value": "paid"}
    written = client.post("/datasets/orders/access/yaml", data=form).json()["mapping"]
    assert column_map.permissions(written) == {"viewer": PAID}
    assert written.startswith(base.rstrip("\n"))                 # the columns as they were

    page = client.post("/datasets/orders/access/form", data={"mapping": written}).text
    assert 'class="ds-filter"' in page and '<input type="hidden" name="filter_value" value="paid">' in page
    assert "ds-draft-row" in page and "<th>Role</th>" in page

    full = client.post("/datasets/orders/access/yaml", data={"mapping": written, "role": "viewer"}).json()
    assert column_map.permissions(full["mapping"]) == {"viewer": []}   # no filters: full
    # A role that doesn't exist isn't written.
    ghost = client.post("/datasets/orders/access/yaml", data={"mapping": written, "role": "ghost"}).json()
    assert column_map.permissions(ghost["mapping"]) == {}


def test_a_built_filter_comes_back_as_its_compact_row(client):
    row = client.post("/datasets/orders/access/filter",
                      data={"filter_column": "status", "filter_op": "in", "filter_value": "paid"})
    assert row.status_code == 200 and '<tr class="ds-filter">' in row.text
    assert client.post("/datasets/orders/access/filter", data={"filter_column": "status"}).status_code == 400


def test_renaming_or_removing_a_role_reaches_the_datasets(client, datasets):
    rid = app_mod.app.state.roles.create_role("viewer")
    grant(datasets, "orders", {"viewer": PAID})
    client.post(f"/access/{rid}/rename", data={"name": "reader"})
    assert datasets.permissions("orders") == {"reader": PAID}
    client.post(f"/access/{rid}/delete")
    assert datasets.permissions("orders") == {}


def test_the_cell_route_renders_as_the_posted_role(client, datasets):
    grant(datasets, "orders", {"viewer": PAID})
    cell = lambda role: client.post("/dashboard/cell", data={"yaml_text": _YAML, "cid": "rows", "role": role}).text
    assert _statuses(cell("viewer")) == {"paid"}
    assert "fireflyer-no-access" in cell("guest")
    assert len(_statuses(cell(""))) > 1


def test_the_skeleton_carries_the_role_to_every_cell(client):
    html = client.post("/execute?role=viewer", content=_YAML).json()["html"]
    assert '<input type="hidden" name="role" value="viewer">' in html


def test_the_dataset_page_has_a_permissions_mode(client):
    assert 'data-mode="access"' in client.get("/datasets/orders").text


# --- rows a role sees ----------------------------------------------------------------------


def test_rows_seen_counts_what_the_filters_let_through(datasets, orders_parquet):
    import polars as pl

    total = pl.read_parquet(orders_parquet).height
    paid = pl.read_parquet(orders_parquet).filter(pl.col("status") == "paid").height
    assert datasets.rows_seen("orders", []) == total
    assert datasets.rows_seen("orders", PAID) == paid < total
    # A filter on a column the data lacks: the role sees nothing (it's locked).
    assert datasets.rows_seen("orders", [{"column": "nope", "op": "in", "values": ["x"]}]) is None


def test_the_rows_column_and_its_recount(client, datasets):
    app_mod.app.state.roles.create_role("viewer")
    grant(datasets, "orders", {"viewer": PAID})
    page = client.post("/datasets/orders/access/form", data={"mapping": datasets.mapping("orders")}).text
    paid = datasets.rows_seen("orders", PAID)
    assert '<th class="ds-access-rows">Rows</th>' in page
    assert f'<td class="ds-access-rows" title="Rows this role sees">{paid}</td>' in page
    recount = client.post("/datasets/orders/access/rows",
                          data={"filter_column": "status", "filter_op": "in", "filter_value": "paid"})
    assert recount.json() == {"rows": paid}


def test_a_roles_description_is_its_tooltip_in_the_picker_and_the_permissions():
    roles = RoleStore(":memory:")
    roles.create_role("viewer", "Reads sales")
    assert 'data-role="viewer" title="Reads sales"' in role_picker(roles.roles())
    html = render_access_form(roles.roles(), {"viewer": []}, lambda f: "", "")
    assert '<span class="name" title="Reads sales">viewer</span>' in html
