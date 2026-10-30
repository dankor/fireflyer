"""The Access tab: roles in a database (`RoleStore` — the portal's, or a local
sqlite file) and the list page with its routes."""

import pytest
from starlette.testclient import TestClient

from fireflyer.web import app as app_mod
from fireflyer.web.portal import RoleError, RoleStore, render_access


@pytest.fixture
def store():
    return RoleStore(":memory:")


def test_roles_are_created_listed_by_name_renamed_and_deleted(store):
    viewer = store.create_role(" viewer ")
    store.create_role("admin")
    assert [r.name for r in store.roles()] == ["admin", "viewer"]

    before = next(r for r in store.roles() if r.id == viewer)
    store.rename_role(viewer, "reader")
    after = next(r for r in store.roles() if r.id == viewer)
    assert after.name == "reader" and after.created_at == before.created_at

    store.delete_role(viewer)
    assert [r.name for r in store.roles()] == ["admin"]


def test_a_role_name_is_required_and_unique(store):
    admin = store.create_role("admin")
    with pytest.raises(RoleError):
        store.create_role("  ")
    with pytest.raises(RoleError):
        store.create_role("admin")
    store.rename_role(admin, "admin")            # its own name is fine
    other = store.create_role("viewer")
    with pytest.raises(RoleError):
        store.rename_role(other, "admin")


def test_the_access_page_lists_roles_escaped_with_their_actions():
    store = RoleStore(":memory:")
    rid = store.create_role("<ops>")
    html = render_access(store.roles())
    assert '<a class="ff-switch-seg active" href="/access">Access</a>' in html
    assert "&lt;ops&gt;" in html and "<ops>" not in html
    assert f'data-id="{rid}"' in html and f'action="/access/{rid}/delete"' in html
    assert "<time" in html


def test_the_access_page_empty_state():
    assert "No roles yet" in render_access([])


def test_roles_persist_in_a_sqlite_file(tmp_path):
    db = str(tmp_path / "data" / "fireflyer.db")
    RoleStore(db).create_role("admin")
    assert [r.name for r in RoleStore(db).roles()] == ["admin"]


def test_the_routes_create_rename_and_delete(app_state):
    store = RoleStore(":memory:")
    app_state("roles", store)
    client = TestClient(app_mod.app)

    assert client.post("/access/new", data={"name": "admin"}).status_code == 200
    rid = store.roles()[0].id
    assert client.post("/access/new", data={"name": "admin"}).status_code == 400
    client.post(f"/access/{rid}/rename", data={"name": "owner"})
    assert "owner" in client.get("/access").text
    client.post(f"/access/{rid}/delete")
    assert store.roles() == []


def test_access_is_on_every_list_page_even_without_a_dashboard_store(monkeypatch, app_state):
    app_state("roles", RoleStore(":memory:"))
    monkeypatch.setattr(app_mod, "_dash_store", lambda request: None)
    client = TestClient(app_mod.app)
    assert client.get("/access").status_code == 200
    assert 'href="/access"' in client.get("/datasets").text


def test_the_access_page_links_the_datasets_naming_each_role():
    store = RoleStore(":memory:")
    store.create_role("viewer")
    store.create_role("guest")
    html = render_access(store.roles(), datasets={"viewer": ["orders", "users"]})
    assert "<th>Datasets</th>" in html
    viewer = html[html.index(">viewer<"):html.index(">guest<")] if html.index(">viewer<") < html.index(">guest<") \
        else html[html.index(">viewer<"):]
    assert 'href="/datasets/orders"' in viewer and 'href="/datasets/users"' in viewer
    assert "/datasets/" not in html[html.index(">guest<"):html.index(">guest<") + 400].split("</tr>")[0]


def test_a_role_has_a_description_shown_under_its_name(tmp_path):
    db = str(tmp_path / "roles.db")
    store = RoleStore(db)
    rid = store.create_role("viewer", " Reads sales ")
    assert store.roles()[0].description == "Reads sales"
    store.rename_role(rid, "reader")                         # description kept
    assert store.roles()[0].description == "Reads sales"
    store.rename_role(rid, "reader", "Reads everything")
    html = render_access(store.roles())
    assert '<span class="sub" title="Reads everything">Reads everything</span>' in html
    assert 'data-description="Reads everything"' in html and "Edit role" in html


def test_a_roles_table_from_before_descriptions_gets_the_column(tmp_path):
    import sqlite3

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE roles (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
                 " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO roles VALUES ('1', 'viewer', 'x', 'x')")
    conn.commit()
    conn.close()
    assert [(r.name, r.description) for r in RoleStore(db).roles()] == [("viewer", "")]


def test_the_routes_take_a_description(app_state):
    store = RoleStore(":memory:")
    app_state("roles", store)
    client = TestClient(app_mod.app)
    client.post("/access/new", data={"name": "viewer", "description": "Reads"})
    rid = store.roles()[0].id
    client.post(f"/access/{rid}/rename", data={"name": "viewer", "description": "Reads more"})
    assert store.roles()[0].description == "Reads more"
