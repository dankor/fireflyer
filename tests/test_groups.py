"""Groups (portal mode): every dataset and dashboard lives in one — each user's
own (`_bob`) or a shared one an admin makes. A chart reads `orders` from its
dashboard's group and `sales:orders` from another; what a viewer reads is
what's theirs and what a role of theirs is granted, its rows narrowed by the
grant's filters on every read."""

import json
import re

import pytest
from starlette.testclient import TestClient

from fireflyer import column_map, groups
from fireflyer.dashboard import Dashboard, qualify_refs, rename_dataset_ref, rename_group_ref
from fireflyer.datasets import DatasetStore, SqlCatalog
from fireflyer.storage import LocalObjectStore
from fireflyer.web import app as app_mod
from fireflyer.web import auth
from fireflyer.web.portal import RoleStore, SqlStore, group_picker

CSV = b"id,status,amount\n1,paid,10\n2,open,5\n3,paid,7\n"
PAID = [{"column": "status", "op": "in", "values": ["paid"]}]


def _yaml(name="Mine", dataset="orders"):
    return (f"name: {name}\ncharts:\n  rows: {{type: table, dataset: {dataset}, title: Rows}}\n"
            'layout:\n  - ["@40", "rows"]\n')


# --- names, references, URLs ------------------------------------------------------------


def test_a_reference_is_bare_in_its_group_and_qualified_elsewhere():
    assert groups.split("orders", "sales") == ("sales", "orders")
    assert groups.split("mkt:orders", "sales") == ("mkt", "orders")
    assert groups.normal("sales:orders", "sales") == "orders"
    assert groups.normal("orders", "mkt") == "orders"
    assert groups.join("sales", "orders", "mkt") == "sales:orders"


@pytest.mark.parametrize("name", ["Sales 🦁", "a/b", "../x", ".hidden", "100%", "~2F", "a\\b", "Зоопарк"])
def test_any_dashboard_name_survives_its_url(name):
    segment = groups.url_name(name)
    assert "/" not in segment and not segment.startswith(".")
    from urllib.parse import unquote
    assert groups.from_url(unquote(segment)) == name


@pytest.mark.parametrize("bad", ["_mine", "sales:x", "my group", ""])
def test_a_shared_groups_name_is_plain_and_not_a_personal_ones(bad):
    with pytest.raises(groups.GroupError):
        groups.check_group_name(bad)
    assert groups.check_group_name("sales-2") == "sales-2"


def test_the_cascades_rewrite_references_wherever_a_dataset_is_named():
    text = ("name: X\ncalcs:\n  sales:orders:\n    n: {agg: count}\n  local_ds:\n    m: {agg: count}\n"
            "charts:\n  a: {type: number, dataset: sales:orders, calc: n}\n"
            "  b: {type: number, dataset: local_ds, calc: m}\n"
            'layout:\n  - ["@10", a, b]\n')
    renamed = rename_dataset_ref(text, "sales:orders", "sales:orders2")
    assert "dataset: sales:orders2," in renamed and "  sales:orders2:\n" in renamed
    moved = rename_group_ref(text, "sales", "revenue")
    assert "dataset: revenue:orders," in moved and "  revenue:orders:\n" in moved
    assert "dataset: local_ds," in moved                      # a bare one stays
    cloned = qualify_refs(text, "mkt")
    assert "dataset: mkt:local_ds," in cloned and "  mkt:local_ds:\n" in cloned
    assert "dataset: sales:orders," in cloned                 # already qualified


def test_inline_data_shadows_a_bare_name_only():
    """`orders` is the dashboard's own CSV; `_admin:orders` the managed one."""
    from fireflyer.dashboard import _Normalizer

    text = (_yaml(dataset="_admin:orders") + "datasets:\n  orders: |\n    a\n    1\n")
    dash = Dashboard.from_yaml(text, datasets=_Normalizer("_admin"))
    assert dash.chart_configs["rows"].kwargs["dataset"] == "_admin:orders"


# --- the portal, two users ---------------------------------------------------------------


@pytest.fixture
def portal(app_state, tmp_path, monkeypatch):
    """Portal mode on sqlite: the env admin (admin/admin), bob and alice, and
    `orders` in alice's group — not shared with anyone yet."""
    roles = RoleStore(":memory:", admin=auth.admin_name())
    app_state("roles", roles)
    app_state("store", SqlStore())
    app_state("datasets", DatasetStore(LocalObjectStore(str(tmp_path)), SqlCatalog(":memory:")))
    app_state("authenticator", auth.AnyOf(auth.default_authenticator(), app_mod._PortalUsers()))
    for user in ("bob", "alice"):
        roles.create_user(user)
        roles.set_password(user, f"{user}-pass-1")
    app_mod.app.state.datasets.group("_alice").create("orders", CSV)
    return roles


def _client(name):
    client = TestClient(app_mod.app, follow_redirects=False)
    password = "admin" if name == "admin" else f"{name}-pass-1"
    assert client.post("/login", data={"username": name, "password": password}).status_code == 303
    return client


def _share(roles, role="viewer", rows=PAID, holder="bob"):
    """alice grants `orders` to a role bob holds."""
    if role not in {r.name for r in roles.roles()}:
        roles.create_role(role)
    roles.edit_user(holder, holder, [role])
    store = app_mod.app.state.datasets.group("_alice")
    store.remap("orders", column_map.with_permissions(store.mapping("orders"), {role: rows}))


def _statuses(html):
    return set(re.findall(r"<td[^>]*>(paid|open)</td>", html))


def _cell(client, yaml_text, group="_bob/Mine"):
    return client.post("/dashboard/cell", data={"yaml_text": yaml_text, "cid": "rows"},
                       headers={"X-FF-Dashboard": group}).text


def test_everyone_starts_in_their_own_group(portal):
    assert _client("bob").get("/").headers["location"] == "/d/_bob"
    assert _client("admin").get("/").headers["location"] == "/d/_admin"
    assert [g.name for g in portal.groups()] == ["_admin", "_alice", "_bob"]


def test_another_groups_dataset_is_locked_until_a_role_shares_it(portal):
    bob = _client("bob")
    mine = _yaml(dataset="_alice:orders")
    assert "fireflyer-no-access" in _cell(bob, mine)
    _share(portal)
    assert _statuses(_cell(bob, mine)) == {"paid"}            # the grant's rows only
    # Its owner reads all of it, bare, in her own group.
    assert _statuses(_cell(_client("alice"), _yaml(), "_alice/Mine")) == {"paid", "open"}
    # And an admin everything.
    assert _statuses(_cell(_client("admin"), mine)) == {"paid", "open"}


def test_a_filters_value_list_shows_only_the_rows_the_viewer_sees(portal):
    _share(portal)
    html = _client("bob").post("/filter/values", data={
        "yaml_text": "", "filter_dataset": "_alice:orders", "filter_column": "status"}).text
    assert "paid" in html and "open" not in html


def test_view_as_is_an_admins_and_no_one_elses(portal):
    _share(portal)
    bob, admin = _client("bob"), _client("admin")
    mine = _yaml(dataset="_alice:orders")
    asked = lambda c, **who: c.post("/dashboard/cell", data={"yaml_text": mine, "cid": "rows", **who},
                                    headers={"X-FF-Dashboard": "_bob/Mine"}).text
    assert _statuses(asked(bob, role="admin")) == {"paid"}     # bob's own view, still
    assert _statuses(asked(bob, user="admin")) == {"paid"}
    assert _statuses(asked(admin, user="bob")) == {"paid"}     # the admin previews bob's
    assert "fireflyer-no-access" in asked(admin, role="guest")


def test_the_assistant_and_the_chart_form_get_only_what_the_viewer_reads(portal, monkeypatch):
    seen = {}
    monkeypatch.setattr(app_mod, "CHAT_ENABLED", True)
    monkeypatch.setattr(app_mod.chat_mod, "run_chat",
                        lambda message, yaml, history, datasets: seen.update(datasets=datasets) or {"reply": ""})
    bob = _client("bob")
    ask = lambda: bob.post("/chat", json={"message": "hi", "yaml": _yaml()}, headers={"X-FF-Dashboard": "_bob/Mine"})
    ask()
    assert seen["datasets"] == []
    _share(portal)
    ask()
    assert [d["name"] for d in seen["datasets"]] == ["_alice:orders"]
    form = bob.post("/chart/config/add-form", data={"yaml_text": _yaml(), "add_type": "table"},
                    headers={"X-FF-Dashboard": "_bob/Mine"}).text
    # The dataset field's group pick: bob's own, and alice's, where it is.
    assert 'class="ff-input ff-combo-group"' in form and '<option value="_alice">_alice</option>' in form
    assert 'data-value="_alice:orders" data-group="_alice"><span class="ff-combo-name">orders</span>' in form


def test_a_list_greys_what_isnt_shared_last_and_it_still_opens(portal):
    alice = _client("alice")
    alice.post("/d/_alice/new", data={"name": "Report"})
    alice.post("/d/_alice/Report/save", data={"yaml_text": _yaml("Report")})
    alice.post("/d/_alice/new", data={"name": "Notes"})               # reads nothing: open to all
    bob = _client("bob")
    page = bob.get("/d/_alice").text
    assert page.index(">Notes<") < page.index(">Report<")
    assert '<tr class="is-inactive" title="Some of its data isn\'t shared with you">' in page
    assert "openAdd()" not in page and "/d/_alice/Report/delete" not in page      # not his to change
    # The group itself is greyed in bob's picker, and still a link.
    assert '<a href="/d/_alice" class="is-inactive"' in bob.get("/d/_bob").text
    _share(portal)
    assert "is-inactive" not in bob.get("/d/_alice").text.split("</thead>")[1].split("Notes")[0]


def test_a_dashboard_someone_only_reads_opens_view_only_without_its_yaml(portal):
    alice = _client("alice")
    alice.post("/d/_alice/new", data={"name": "Report"})
    secret = _yaml("Report") + "# a comment only its owner should see\n"
    alice.post("/d/_alice/Report/save", data={"yaml_text": secret})
    _share(portal)
    bob = _client("bob")
    page = bob.get("/d/_alice/Report").text
    assert "only its owner" not in page and 'data-view-only="1"' in page
    assert 'id="ff-save"' not in page and 'data-mode="edit"' not in page and 'id="ff-role"' not in page
    # The editor posts no YAML; the server renders the stored one, and keeps it out of the page.
    rendered = bob.post("/execute", content="", headers={"X-FF-Dashboard": "_alice/Report"}).json()["html"]
    assert "only its owner" not in rendered and '<input type="hidden" name="yaml_text" value="">' in rendered
    assert _statuses(_cell(bob, "", "_alice/Report")) == {"paid"}
    # Nor its versions, nor a save.
    assert bob.get("/d/_alice/Report/versions").status_code == 404
    assert bob.post("/d/_alice/Report/save", data={"yaml_text": secret}).status_code == 403


def test_a_clone_into_another_group_reads_the_same_data(portal):
    alice = _client("alice")
    alice.post("/d/_alice/new", data={"name": "Report"})
    alice.post("/d/_alice/Report/save", data={"yaml_text": _yaml("Report")})
    bob = _client("bob")
    assert bob.post("/d/_alice/Report/clone", data={"name": "Mine", "group": "_alice"}).status_code == 403
    r = bob.post("/d/_alice/Report/clone", data={"name": "Mine", "group": "_bob"})
    assert r.headers["location"] == "/d/_bob/Mine"
    assert "dataset: _alice:orders" in app_mod.app.state.store.group("_bob").get("Mine").yaml


def test_someone_elses_dataset_shows_its_columns_and_the_values_they_see(portal):
    bob = _client("bob")
    page = bob.get("/datasets/_alice/orders").text
    assert 'data-readonly="1"' in page and 'id="ds-modes"' not in page and "ds-history" not in page
    assert 'class="col-example"' not in page                          # not shared: no values
    _share(portal)
    page = bob.get("/datasets/_alice/orders").text
    assert 'data-value="paid"' in page and 'data-value="open"' not in page
    assert bob.post("/datasets/_alice/orders/delete").status_code == 403
    assert bob.get("/datasets/_alice/orders/versions").status_code == 403


def test_a_dataset_rename_reaches_every_group(portal):
    bob = _client("bob")
    bob.post("/d/_bob/new", data={"name": "Mine"})
    bob.post("/d/_bob/Mine/save", data={"yaml_text": _yaml(dataset="_alice:orders")})
    alice = _client("alice")
    alice.post("/datasets/_alice/orders/rename", data={"name": "sales"})
    assert "dataset: _alice:sales" in app_mod.app.state.store.group("_bob").get("Mine").yaml
    # Another group's dashboard doesn't hold a dataset: it can go — that chart locks.
    assert alice.post("/datasets/_alice/sales/delete").status_code == 303


def test_a_dataset_its_own_group_reads_is_guarded(portal):
    alice = _client("alice")
    alice.post("/d/_alice/new", data={"name": "Report"})
    alice.post("/d/_alice/Report/save", data={"yaml_text": _yaml("Report")})
    assert alice.post("/datasets/_alice/orders/delete").status_code == 409


def test_one_role_per_dataset_is_per_group(portal):
    """Two groups' `orders` are two datasets: one role each doesn't clash."""
    app_mod.app.state.datasets.group("_bob").create("orders", CSV)
    for group, role in (("_alice", "a"), ("_bob", "b")):
        portal.create_role(role)
        store = app_mod.app.state.datasets.group(group)
        store.remap("orders", column_map.with_permissions(store.mapping("orders"), {role: []}))
    admin = _client("admin")
    assert admin.post("/users/new", data={"name": "carol", "role": ["a", "b"]}).status_code == 303


# --- the Groups tab ---------------------------------------------------------------------


def test_an_admin_adds_renames_and_removes_shared_groups(portal):
    admin = _client("admin")
    assert admin.post("/groups/new", data={"name": "sales"}).status_code == 303
    assert admin.post("/groups/new", data={"name": "_mine"}).status_code == 400
    app_mod.app.state.datasets.group("sales").create("orders", CSV)
    bob = _client("bob")
    bob.post("/d/_bob/new", data={"name": "Mine"})
    bob.post("/d/_bob/Mine/save", data={"yaml_text": _yaml(dataset="sales:orders")})
    page = admin.get("/groups").text
    assert ">sales<" in page and "act-locked" in page                # personal ones locked

    admin.post("/groups/sales/rename", data={"name": "revenue"})
    assert "dataset: revenue:orders" in app_mod.app.state.store.group("_bob").get("Mine").yaml
    assert app_mod.app.state.datasets.group("revenue").get("orders") is not None
    assert admin.post("/groups/_bob/rename", data={"name": "x"}).status_code == 400
    admin.post("/groups/revenue/delete")
    assert portal.group("revenue") is None and app_mod.app.state.datasets.group("revenue").list() == []
    assert bob.get("/groups").status_code == 403


def test_a_removed_user_takes_their_group_with_them(portal):
    bob = _client("bob")
    bob.post("/d/_bob/new", data={"name": "Mine"})
    app_mod.app.state.datasets.group("_bob").create("notes", CSV)
    _client("admin").post("/users/bob/delete")
    assert portal.group("_bob") is None
    assert app_mod.app.state.store.group("_bob").list() == []
    assert app_mod.app.state.datasets.group("_bob").list() == []


def test_the_picker_splits_personal_and_shared_groups(portal):
    """It opens on the shared groups, even from your own."""
    portal.create_group("sales", "Revenue team")
    html = group_picker(portal.groups(), "_bob", inactive={"_alice"})
    assert '<div class="ff-group-list" data-kind="shared"><a href="/d/sales" title="Revenue team">' in html
    assert '<button type="button" data-kind="shared" title="Shared groups" aria-label="Shared groups" class=active>' in html
    personal = html[html.index('data-kind="personal" hidden>'):]
    assert '<a href="/d/_bob" class="active">_bob</a>' in personal and "sales" not in personal
    assert 'class="is-inactive" title="Nothing here is shared with you">_alice</a>' in personal


def test_the_chart_modals_preview_reads_as_its_viewer(portal):
    """Editing a chart in his own group on alice's data, bob's preview shows a
    lock until it's shared, then just the rows his role is granted."""
    bob = _client("bob")
    form = {"yaml_text": _yaml("Mine", "_alice:orders"), "cid": "rows",
            "type": "table", "dataset": "_alice:orders", "title": "Rows"}
    preview = lambda: bob.post("/chart/config/preview", data=form,
                               headers={"X-FF-Dashboard": "_bob/Mine"}).text
    assert "fireflyer-no-access" in preview()
    _share(portal)
    assert _statuses(preview()) == {"paid"}


def test_only_the_lists_have_the_picker(portal):
    """A dashboard or a dataset you opened has back, and its group read-only."""
    admin = _client("admin")
    app_mod.app.state.store.group("_alice").create(_yaml("Report"))
    assert 'id="ff-group"' in admin.get("/d/_alice").text
    assert 'id="ff-group"' in admin.get("/datasets/_alice").text
    for page in (admin.get("/d/_alice/Report"), admin.get("/datasets/_alice/orders"),
                 _client("bob").get("/datasets/_alice/orders")):              # the last read-only
        assert page.status_code == 200 and 'id="ff-group"' not in page.text
        assert 'class="ff-back"' in page.text
        assert '<span class="ff-group-label" title="Group" aria-label="Group">_alice</span>' in page.text


def test_the_picker_opens_on_personal_groups_while_there_are_no_shared_ones(portal):
    html = group_picker(portal.groups(), "_bob")
    assert '<div class="ff-group-list" data-kind="personal">' in html
    assert '<div class="ff-group-list" data-kind="shared" hidden><span class="ff-role-none">None yet</span>' in html


def test_datasets_from_before_groups_move_in_and_bad_names_are_fitted(portal, monkeypatch):
    """Start-up in portal mode: the files into `_admin`, and a name the rule
    refuses renamed — the dashboards reading it following."""
    import fireflyer.datasets as datasets_mod

    root = app_mod.app.state.datasets
    before = DatasetStore(root._store)                        # files at the top, as before groups
    with monkeypatch.context() as m:
        m.setattr(datasets_mod, "_check_name", lambda name: name)   # names were looser then
        before.create("my data", CSV)
    # Its row went into `_admin` when the catalog opened.
    root._catalog.save("my data", root._store.get("datasets/my data/dataset.yaml").decode())
    app_mod.app.state.store.create(_yaml("Old", "my data"))

    app_mod._move_into_groups()
    assert not any(k.startswith("datasets/") for k in root._store.list_keys())
    assert root.group("_admin").names() == ["my-data"]
    assert root.group("_admin").get("my-data").rows == 3
    assert "dataset: my-data" in app_mod.app.state.store.get("Old").yaml
    assert app_mod.app.state.store.versions("Old")[0].author == auth.admin_name()
    assert not (tmp := root._store._base / "datasets").exists(), f"{tmp} left behind, empty"
    assert not (root._store._base / "groups/_admin/datasets/my data").exists()


@pytest.mark.parametrize("name", ["a/b", "../up", "100% ~2F", "Зоопарк 🦁"])
def test_any_dashboard_name_opens_at_its_url(portal, name):
    admin = _client("admin")
    made = admin.post("/d/_admin/new", data={"name": name})
    assert made.headers["location"] == f"/d/_admin/{groups.url_name(name)}"
    page = admin.get(made.headers["location"])
    assert page.status_code == 200 and app_mod.app.state.store.get(name) is not None
