"""The Users tab (portal mode): people in the roles database, each holding
roles — at most one per dataset — and viewing a dashboard as a user."""

import pytest
from starlette.testclient import TestClient

from fireflyer import column_map
from fireflyer.web import app as app_mod
from fireflyer.web.portal import (
    Option, RoleError, RoleStore, multi_select, nav_switch, render_users, role_clashes, role_picker,
)

from test_dataset_access import PAID, _YAML, _statuses, datasets, grant  # noqa: F401


@pytest.fixture
def store():
    return RoleStore(":memory:")


# --- the store -------------------------------------------------------------------


def test_users_are_created_with_roles_listed_edited_and_deleted(store):
    viewer, admin = store.create_role("viewer"), store.create_role("admin")
    bob = store.create_user(" bob ", [viewer], " Bob ", "Smith", "bob@x.com")
    store.create_user("alice")
    assert [(u.name, u.roles) for u in store.users()] == [("alice", []), ("bob", ["viewer"])]
    assert store.user("bob").about == "Bob Smith · bob@x.com" and store.user("alice").about == ""

    store.edit_user(bob, "robert", [admin, viewer, "no-such-role"], email="rob@x.com")
    assert store.user("robert").roles == ["admin", "viewer"]
    assert store.user("robert").about == "rob@x.com"                 # what's left out is cleared
    store.delete_user(bob)
    assert [u.name for u in store.users()] == ["alice"]


def test_a_user_name_is_required_and_unique(store):
    store.create_user("bob")
    with pytest.raises(RoleError):
        store.create_user(" ")
    with pytest.raises(RoleError):
        store.create_user("bob")
    with pytest.raises(RoleError):
        store.create_user("eve", email="not-an-email")


def test_a_renamed_role_stays_held_and_a_removed_one_is_dropped(store):
    viewer = store.create_role("viewer")
    store.create_user("bob", role_ids=[viewer])
    store.rename_role(viewer, "reader")
    assert store.user("bob").roles == ["reader"]
    store.delete_role(viewer)
    assert store.user("bob").roles == []


def test_two_roles_clash_only_where_both_may_read_a_dataset():
    perms = {"orders": {"viewer": PAID, "admin": []}, "users": {"admin": []}}
    assert role_clashes(["viewer", "admin"], perms) == [("orders", ["admin", "viewer"])]
    assert role_clashes(["viewer", "ops"], perms) == []


# --- pages -------------------------------------------------------------------------


def test_the_users_page_lists_users_escaped_with_their_roles(store):
    rid = store.create_role("viewer")
    store.create_user("<bob>", role_ids=[rid], first_name="Bob", last_name="<Smith>")
    html = render_users(store.users(), store.roles(), reads={"viewer": ["orders"]})
    # Full name (and email) under the name, as a role's description; the Edit
    # button carries each for the dialog.
    assert '<span class="sub" title="Bob &lt;Smith&gt;">Bob &lt;Smith&gt;</span>' in html
    assert 'data-first-name="Bob" data-last-name="&lt;Smith&gt;" data-email=""' in html
    assert '<a class="ff-switch-seg active" href="/users">Users</a>' in html
    assert "&lt;bob&gt;" in html and "<bob>" not in html
    assert 'href="/access" title="viewer">viewer</a>' in html
    # The dialog's box for the role carries what it reads, for the clash hints.
    assert (f'value="{rid}" data-label="viewer" data-name="viewer" data-reads="[&quot;orders&quot;]"'
            in html)
    # The roles are the shared multi-select, its script and styles linked.
    assert 'class="ff-multi" data-placeholder="Roles"' in html
    assert "/static/multi-select.js" in html and "/static/multi-select.css" in html
    assert "No users yet" in render_users([], [])


def test_the_multi_select_submits_its_ticked_boxes_and_escapes():
    html = multi_select("tag", [Option("a", "<A>", "first", {"x": '"q"'}, checked=True), Option("b", "B")], "Tags")
    assert ('<label class="ff-multi-item" title="first"><input type="checkbox" name="tag" value="a"'
            ' data-label="&lt;A&gt;" data-x="&quot;q&quot;" checked><span>&lt;A&gt;</span>') in html
    assert 'value="b" data-label="B"><span>B</span></label>' in html
    # The search box belongs to no form: its text is never submitted.
    assert 'class="ff-multi-search" type="text" form="ff-none" placeholder="Tags"' in html
    assert '<span class="ff-multi-none" hidden>' in html
    assert '<span class="ff-multi-none">None yet</span>' in multi_select("tag", [], "Tags")


def test_the_users_tab_shows_only_where_there_are_people():
    assert 'href="/users"' not in nav_switch("access")
    assert 'href="/users"' in nav_switch("access", users=True)


def test_the_picker_switches_between_roles_and_users(store):
    store.create_role("viewer")
    store.create_user("bob", first_name="Bob", email="bob@x.com")
    html = role_picker(store.roles(), store.users())
    # A roles | users switch heads the menu; the users' list starts hidden.
    assert html.index('data-kind="role"') < html.index('data-kind="user"') < html.index('class="ff-role-list"')
    assert '<div class="ff-role-list" data-kind="user" hidden><button type="button" data-user="bob" title="Bob · bob@x.com">' in html
    assert 'data-role="viewer"' in html[html.index('data-kind="role">'):html.index('data-kind="user" hidden')]
    # The summary's icon follows the pick (editor.js sets `data-as`).
    assert '<details class="ff-pathdd ff-role" id="ff-role" data-as="">' in html
    assert all(f'<span class="ff-role-icon" data-as="{k}">' in html for k in ("", "role", "user"))
    plain = role_picker(store.roles())                     # no users: no switch
    assert "data-user" not in plain and "ff-role-kinds" not in plain
    # A search (one pick, no checkboxes) under the switch — with users or not.
    assert html.index("ff-role-kinds") < html.index('class="ff-role-search" type="search"') < html.index("ff-role-list")
    assert 'class="ff-role-search"' in plain and 'type="checkbox"' not in html
    # The roles' segment shows people; the users' one a person.
    assert html.index('<path d="M16 21v-2a4 4 0 0 0-4-4H6') < html.index('data-kind="user"')


# --- routes ------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, datasets):  # noqa: F811
    monkeypatch.setattr(app_mod, "_dataset_store", lambda request: datasets)
    monkeypatch.setattr(app_mod, "_every_dataset_store", lambda: [datasets])
    monkeypatch.setattr(app_mod, "_users_on", lambda: True)
    monkeypatch.setattr(app_mod, "_is_admin", lambda request: True)   # signed in as an admin
    return TestClient(app_mod.app)


def test_the_users_routes_need_portal_mode(datasets, monkeypatch):  # noqa: F811
    monkeypatch.setattr(app_mod, "_dataset_store", lambda request: datasets)
    c = TestClient(app_mod.app)
    assert c.get("/users").status_code == 404
    assert c.post("/users/new", data={"name": "bob"}).status_code == 404


def test_users_are_added_edited_and_removed_through_the_routes(client):
    roles = app_mod.app.state.roles
    viewer = roles.create_role("viewer")
    client.post("/users/new", data={"name": "bob", "role": [viewer], "last_name": "Smith", "email": "b@x.io"})
    bob = roles.user("bob")
    assert bob.roles == ["viewer"] and bob.about == "Smith · b@x.io" and "bob" in client.get("/users").text
    client.post(f"/users/{bob.id}/edit", data={"name": "robert"})
    assert roles.user("robert").roles == [] and roles.user("bob") is None
    client.post(f"/users/{bob.id}/delete")
    assert roles.users() == []


def test_a_user_cant_hold_two_roles_that_read_one_dataset(client, datasets):  # noqa: F811
    roles = app_mod.app.state.roles
    viewer, admin = roles.create_role("viewer"), roles.create_role("admin")
    grant(datasets, "orders", {"viewer": PAID, "admin": []})
    refused = client.post("/users/new", data={"name": "bob", "role": [viewer, admin]})
    assert refused.status_code == 400 and "both may read orders" in refused.text
    assert roles.users() == []


def test_a_dataset_cant_give_one_users_two_roles_access(client, datasets):  # noqa: F811
    roles = app_mod.app.state.roles
    viewer, admin = roles.create_role("viewer"), roles.create_role("admin")
    roles.create_user("bob", role_ids=[viewer, admin])
    grant(datasets, "orders", {"viewer": PAID})
    form = {"mapping": datasets.mapping("orders"), "role": ["viewer", "admin"]}
    refused = client.post("/datasets/orders/access/yaml", data=form)
    assert refused.status_code == 400 and "bob" in refused.json()["error"]
    # Nor through the YAML itself (code mode), on Save.
    both = column_map.with_permissions(datasets.mapping("orders"), {"viewer": PAID, "admin": []})
    assert client.post("/datasets/orders/mapping", data={"mapping": both}).status_code == 400
    assert datasets.permissions("orders") == {"viewer": PAID}


def test_the_cell_route_renders_as_the_posted_user(client, datasets):  # noqa: F811
    roles = app_mod.app.state.roles
    viewer = roles.create_role("viewer")
    roles.create_user("bob", role_ids=[viewer])
    roles.create_user("eve")
    grant(datasets, "orders", {"viewer": PAID})
    cell = lambda user: client.post("/dashboard/cell", data={"yaml_text": _YAML, "cid": "rows", "user": user}).text
    assert _statuses(cell("bob")) == {"paid"}
    assert "fireflyer-no-access" in cell("eve")          # no role that reads it
    assert "fireflyer-no-access" in cell("ghost")        # no such user
    html = client.post("/execute?user=bob", content=_YAML).json()["html"]
    assert '<input type="hidden" name="user" value="bob">' in html


# --- the built-ins (portal mode) --------------------------------------------------


@pytest.fixture
def portal_roles(app_state):
    roles = RoleStore(":memory:", admin="root")
    app_state("roles", roles)
    return roles


def test_portal_has_a_locked_admin_role_and_user(portal_roles):
    admin = next(r for r in portal_roles.roles() if r.name == "admin")
    assert admin.builtin and admin.description
    first = portal_roles.users()[0]
    assert (first.name, first.roles, first.builtin) == ("root", ["admin"], True)
    for change in (lambda: portal_roles.rename_role(admin.id, "boss"), lambda: portal_roles.delete_role(admin.id),
                   lambda: portal_roles.edit_user(first.id, "x"), lambda: portal_roles.delete_user(first.id),
                   lambda: portal_roles.create_user("root")):                   # its name is taken
        with pytest.raises(RoleError):
            change()
    assert RoleStore(":memory:").roles() == [] and RoleStore(":memory:").users() == []   # local: none


def test_built_ins_are_locked_and_coloured_on_the_pages(portal_roles):
    viewer = portal_roles.create_role("viewer")
    portal_roles.create_user("bob", [viewer])
    users = render_users(portal_roles.users(), portal_roles.roles())
    root_row = users[users.index('<tr class="is-builtin">'):users.index("bob")]
    assert "act-locked" in root_row and "openUser(this)" not in root_row
    assert 'class="ref is-builtin" href="/access"' in root_row
    assert users.count("act act-locked") == 1                           # bob's row has edit + remove
    from fireflyer.web.portal import render_access
    access = render_access(portal_roles.roles())
    assert access.count('<tr class="is-builtin">') == 1 and access.count("act act-locked") == 1
    access = render_access(portal_roles.roles(), datasets={"admin": ["orders"]})
    assert '<td class="muted">All</td>' in access and 'href="/datasets/orders"' not in access
    assert '<span class="sub" title="Built-in">Built-in</span>' in access
    picker = role_picker(portal_roles.roles(), portal_roles.users())
    assert '<button type="button" class="is-builtin" data-role="admin"' in picker
    assert '<button type="button" class="is-builtin" data-user="root"' in picker


def test_the_admin_role_reads_every_dataset_and_no_permissions_name_it(client, datasets, portal_roles):  # noqa: F811
    cell = lambda **who: client.post("/dashboard/cell", data={"yaml_text": _YAML, "cid": "rows", **who}).text
    assert len(_statuses(cell(role="admin"))) > 1 and len(_statuses(cell(user="root"))) > 1
    # The permissions form neither lists nor writes it.
    form = client.post("/datasets/orders/access/form", data={"mapping": datasets.mapping("orders")}).text
    assert 'data-role="admin"' not in form
    written = client.post("/datasets/orders/access/yaml",
                          data={"mapping": datasets.mapping("orders"), "role": "admin"}).json()["mapping"]
    assert column_map.permissions(written) == {}
    # It reads every dataset, so it clashes with any role that reads one.
    viewer = portal_roles.create_role("viewer")
    grant(datasets, "orders", {"viewer": PAID})
    admin = next(r.id for r in portal_roles.roles() if r.name == "admin")
    assert client.post("/users/new", data={"name": "bob", "role": [admin, viewer]}).status_code == 400
    root = portal_roles.users()[0].id
    assert client.post(f"/users/{root}/delete").status_code == 400
    assert client.post(f"/access/{admin}/delete").status_code == 400


# --- passwords ---------------------------------------------------------------------

from fireflyer.web import auth  # noqa: E402


def test_a_password_is_kept_only_as_a_salted_scrypt_hash():
    one, two = auth.hash_password("correct horse"), auth.hash_password("correct horse")
    assert one.startswith("scrypt$131072$8$1$") and one != two          # a fresh salt each time
    assert auth.password_matches("correct horse", one)
    assert not auth.password_matches("correct hors", one)
    assert not auth.password_matches("", "") and not auth.password_matches("x", "garbage")
    with pytest.raises(auth.PasswordError):
        auth.check_policy("short")
    with pytest.raises(auth.PasswordError):
        auth.check_policy("x" * (auth.MAX_PASSWORD + 1))


def test_the_store_never_holds_or_hands_out_the_password(portal_roles):
    bob = portal_roles.create_user("bob")
    assert not portal_roles.user("bob").has_password and portal_roles.verify("bob", "") is None
    portal_roles.set_password(bob, "s3cret-pass")
    raw = portal_roles._run("SELECT password_hash FROM users WHERE id = ?", (bob,))[0][0]
    assert "s3cret-pass" not in raw and raw.startswith("scrypt$")
    assert portal_roles.user("bob").has_password and not hasattr(portal_roles.user("bob"), "password_hash")
    assert portal_roles.verify("bob", "s3cret-pass") == "bob"
    assert portal_roles.verify("bob", "wrong-pass!") is None and portal_roles.verify("eve", "s3cret-pass") is None
    with pytest.raises(RoleError):
        portal_roles.set_password(bob, "short")
    with pytest.raises(RoleError):
        portal_roles.set_password(portal_roles.users()[0].id, "the-admins-own")   # the env's, not ours
    key = portal_roles.session_key("bob")
    portal_roles.set_password(bob, "another-pass")
    assert portal_roles.session_key("bob") not in (None, key)            # rotated: old sessions end
    portal_roles.clear_password(bob)
    assert portal_roles.session_key("bob") is None and portal_roles.verify("bob", "another-pass") is None


def test_the_dialog_never_fills_a_password_in(portal_roles):
    bob = portal_roles.create_user("bob")
    portal_roles.set_password(bob, "s3cret-pass")
    html = render_users(portal_roles.users(), portal_roles.roles())
    assert 'name="password" id="user-form-password" type="password" autocomplete="new-password"' in html
    assert 'minlength="8"' in html and "value=" not in html[html.index('name="password"'):html.index("ff-password-show")]
    assert "scrypt$" not in html
    assert 'data-has-password' in html and 'class="ff-key" title="Can sign in"' in html


@pytest.fixture
def portal(monkeypatch, portal_roles, app_state):
    """The app with its real login: the env admin (admin/admin), then the users."""
    monkeypatch.setattr(app_mod, "_users_on", lambda: True)
    app_state("authenticator",
                        auth.AnyOf(auth.PasswordAuthenticator("root", "root-pass"), app_mod._PortalUsers()))
    monkeypatch.setattr(auth, "admin_name", lambda: "root")
    return TestClient(app_mod.app)


def _login(client, name, password):
    return client.post("/login", data={"username": name, "password": password}, follow_redirects=False)


def test_an_admin_sets_a_password_and_the_user_signs_in_with_it(portal, portal_roles):
    assert _login(portal, "root", "root-pass").status_code == 303
    short = portal.post("/users/new", data={"name": "bob", "password": "short"})
    assert short.status_code == 400 and portal_roles.user("bob") is None     # nothing saved
    portal.post("/users/new", data={"name": "bob", "password": "bobs-pass-1"})
    assert portal_roles.user("bob").has_password

    bob = TestClient(app_mod.app)
    assert "Invalid" in _login(bob, "bob", "wrong-pass").text
    assert _login(bob, "bob", "bobs-pass-1").status_code == 303
    assert bob.get("/datasets", follow_redirects=False).status_code == 200
    # Not an admin: no Users tab, and its routes refuse.
    assert 'href="/users"' not in bob.get("/datasets").text
    assert bob.get("/users").status_code == 403
    assert bob.post("/users/new", data={"name": "eve"}).status_code == 403
    assert bob.post("/access/new", data={"name": "boss"}).status_code == 403

    # Editing without a password keeps it; a new one signs bob out everywhere.
    uid = portal_roles.user("bob").id
    portal.post(f"/users/{uid}/edit", data={"name": "bob"})
    assert portal_roles.verify("bob", "bobs-pass-1") == "bob"
    portal.post(f"/users/{uid}/edit", data={"name": "bob", "password": "bobs-pass-2"})
    assert bob.get("/datasets", follow_redirects=False).headers["location"] == "/login"
    assert _login(bob, "bob", "bobs-pass-2").status_code == 303
    portal.post(f"/users/{uid}/edit", data={"name": "bob", "clear_password": "1"})
    assert bob.get("/datasets", follow_redirects=False).status_code == 303   # removed: signed out
    assert "Invalid" in _login(bob, "bob", "bobs-pass-2").text


def test_a_user_holding_admin_manages_users(portal, portal_roles):
    admin = next(r.id for r in portal_roles.roles() if r.name == "admin")
    ann = portal_roles.create_user("ann", [admin])
    portal_roles.set_password(ann, "anns-pass-1")
    assert _login(portal, "ann", "anns-pass-1").status_code == 303
    assert portal.get("/users").status_code == 200


# --- users from other systems (SSO, LDAP, …) ----------------------------------------


def test_another_system_provisions_its_users_the_first_time_and_refreshes_them(portal_roles):
    key = portal_roles.provision("dana@corp", "sso", first_name="Dana", email="dana@corp")
    dana = portal_roles.user("dana@corp")
    assert (dana.source, dana.external, dana.roles, dana.has_password) == ("sso", True, [], False)
    assert key and portal_roles.session_key("dana@corp") == key
    assert portal_roles.provision("dana@corp", "sso", last_name="Kim") == key      # same session key
    assert portal_roles.user("dana@corp").about == "Dana Kim · dana@corp"


def test_no_system_signs_in_as_anothers_user(portal_roles):
    portal_roles.create_user("bob")
    portal_roles.provision("dana", "sso")
    for name, source in (("bob", "sso"), ("root", "sso"), ("dana", "ldap"), ("eve", "local")):
        with pytest.raises(RoleError):
            portal_roles.provision(name, source)


def test_an_external_user_has_no_password_here_and_keeps_their_name(portal_roles):
    portal_roles.provision("dana", "sso")
    dana = portal_roles.user("dana").id
    for change in (lambda: portal_roles.set_password(dana, "a-long-pass"),
                   lambda: portal_roles.clear_password(dana),
                   lambda: portal_roles.edit_user(dana, "danielle")):
        with pytest.raises(RoleError):
            change()
    viewer = portal_roles.create_role("viewer")
    portal_roles.edit_user(dana, "dana", [viewer], email="d@corp")                    # roles are ours
    assert portal_roles.user("dana").roles == ["viewer"] and portal_roles.verify("dana", "") is None


def test_the_list_shows_who_signs_in_elsewhere(portal_roles):
    portal_roles.provision("dana", "sso")
    html = render_users(portal_roles.users(), portal_roles.roles())
    assert 'class="ff-key ff-source" title="Signs in through sso"' in html and 'data-source="sso"' in html
    assert '<div class="ff-external" hidden>' in html


class _Ldap:
    """An authenticator for another system: it says who it is."""

    source = "ldap"

    def verify(self, username, password):
        return username if password == "ldap-pass" else None


def test_an_authenticator_with_a_source_signs_its_users_in(portal, portal_roles, app_state):
    app_state("authenticator",
                        auth.AnyOf(auth.PasswordAuthenticator("root", "root-pass"), app_mod._PortalUsers(), _Ldap()))
    client = TestClient(app_mod.app)
    assert _login(client, "lee", "ldap-pass").status_code == 303
    assert portal_roles.user("lee").source == "ldap"
    assert client.get("/datasets", follow_redirects=False).status_code == 200
    # Deleting them signs them out; signing in again makes them anew, with no roles.
    portal_roles.delete_user(portal_roles.user("lee").id)
    assert client.get("/datasets", follow_redirects=False).status_code == 303
    portal_roles.create_user("bob")
    assert "Invalid" in _login(client, "bob", "ldap-pass").text              # not LDAP's to sign in


def test_an_sso_callback_signs_in_through_sign_in(portal, portal_roles):
    from fastapi.responses import RedirectResponse

    from fastapi import Request

    @app_mod.app.get("/auth/test-sso/callback")
    def callback(request: Request):                      # what a provider's callback does, once it trusts who it is
        resp = RedirectResponse("/datasets", status_code=303)
        app_mod.sign_in(request, resp, "sam@corp", "sso", first_name="Sam")
        return resp

    try:
        client = TestClient(app_mod.app)
        assert client.get("/auth/test-sso/callback").status_code == 200     # open, then signed in
        assert portal_roles.user("sam@corp").source == "sso" and portal_roles.user("sam@corp").about == "Sam"
        assert client.get("/datasets", follow_redirects=False).status_code == 200
        # A cookie signed without the user's key — never one sign_in made — is refused.
        bare = TestClient(app_mod.app)
        bare.cookies.set("ff_session", auth._sign("sam@corp"))
        assert bare.get("/datasets", follow_redirects=False).status_code == 303
    finally:
        app_mod.app.router.routes[:] = [r for r in app_mod.app.router.routes
                                        if getattr(r, "path", "") != "/auth/test-sso/callback"]
