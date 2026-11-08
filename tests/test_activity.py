"""The activity log (portal mode): a row per action — who, what, on which
dashboard / dataset / role / user, the response's status code, and the action's
own parameters — in the portal database. Plus the dashboard store it relies on:
a dashboard is keyed by its unique name."""

import sqlite3

import pytest
from starlette.testclient import TestClient

from fireflyer.dashboard import DashboardError
from fireflyer.web import app as app_mod
from fireflyer.web import auth
from fireflyer.web.portal import Activity, ActivityLog, RoleStore, SqlStore

from test_dataset_access import datasets  # noqa: F401

_YAML = """name: Sales
charts:
  rows: {type: table, dataset: orders, title: Rows}
layout:
  - ["@40", "rows"]
"""


@pytest.fixture
def log():
    return ActivityLog(":memory:")


@pytest.fixture
def portal(app_state, monkeypatch, datasets, log):  # noqa: F811
    """A signed-in admin in portal mode, with a fresh log."""
    app_state("store", SqlStore())
    app_state("authenticator", auth.default_authenticator())
    app_state("roles", RoleStore(":memory:", admin=auth.admin_name()))
    app_state("activity", log)
    monkeypatch.setattr(app_mod, "_datasets_in", lambda group: datasets)
    # Not following redirects: each request is one row, not the page it lands on.
    client = TestClient(app_mod.app, follow_redirects=False)
    client.post("/login", data={"username": "admin", "password": "admin"})
    return client


def _rows(log, action=None):
    """Oldest first, the login's left out unless asked for."""
    rows = list(reversed(log.entries(500)))
    return [r for r in rows if r.action == action] if action else [r for r in rows if r.action != "login"]


# --- the store -------------------------------------------------------------------


def test_a_row_is_written_and_read_back(log):
    log.record(Activity("bob", "save", "dashboard", "Sales", 200, {"version": 3}, "10.0.0.1", "Firefox"))
    [row] = log.entries()
    assert (row.actor, row.action, row.object_type, row.object_name, row.status_code) == ("bob", "save", "dashboard", "Sales", 200)
    assert row.detail == {"version": 3} and row.ip == "10.0.0.1" and row.user_agent == "Firefox"
    assert row.at                                          # stamped when written


def test_rows_older_than_the_window_are_pruned():
    log = ActivityLog(":memory:", keep_days=365)
    log.record(Activity("bob", "open", at="2020-01-01T00:00:00+00:00"))
    log.record(Activity("bob", "save"))
    log.prune()
    assert [r.action for r in log.entries()] == ["save"]
    forever = ActivityLog(":memory:", keep_days=0)
    forever.record(Activity("bob", "open", at="2020-01-01T00:00:00+00:00"))
    forever.prune()
    assert len(forever.entries()) == 1


# --- dashboards keyed by name ------------------------------------------------------


def test_a_dashboard_name_is_unique_and_its_id():
    store = SqlStore()
    assert store.create(_YAML) == "Sales"
    with pytest.raises(DashboardError, match="already exists"):
        store.create(_YAML)
    assert store.free_name("Sales") == "Sales 2"
    # Any text: the URL escapes what it must (groups.url_name).
    assert store.create(_YAML.replace("name: Sales", "name: '../a/b 🦁'")) == "../a/b 🦁"
    # Unique within its group only.
    assert store.group("mkt").create(_YAML) == "Sales"
    assert [r.group for r in store.group("mkt").list()] == ["mkt"]


def test_dashboards_from_before_groups_move_into_the_admins(tmp_path):
    path = str(tmp_path / "portal.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE dashboards (name TEXT PRIMARY KEY, author TEXT NOT NULL DEFAULT '',"
               " yaml TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("CREATE TABLE dashboard_versions (dashboard TEXT NOT NULL, version INTEGER NOT NULL,"
               " yaml TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL,"
               " PRIMARY KEY (dashboard, version))")
    db.execute("CREATE TABLE dashboard_uses (dashboard TEXT NOT NULL, dataset TEXT NOT NULL, column_name TEXT NOT NULL)")
    db.execute("INSERT INTO dashboards VALUES ('Sales', 'dana', ?, '2026-01-01', '2026-01-01')", (_YAML,))
    db.execute("INSERT INTO dashboard_versions VALUES ('Sales', 1, ?, 'dana', 0)", (_YAML,))
    db.commit()
    db.close()

    store = SqlStore(path)
    assert store.group_name == "_admin" and store.get("Sales").author == "dana"
    assert [v.version for v in store.versions("Sales")] == [1]
    assert SqlStore(path).group("other").list() == []


def test_saving_a_new_name_renames_it_with_its_history_unless_taken():
    store = SqlStore()
    store.create(_YAML, "dana")
    store.create(_YAML.replace("Sales", "Other"))
    assert store.save("Sales", _YAML.replace("Sales", "Revenue"), "sam") == "Revenue"
    assert store.get("Sales") is None
    assert [v.version for v in store.versions("Revenue")] == [2, 1]
    assert {u.dashboard_id for u in store.uses()} == {"Other", "Revenue"}
    with pytest.raises(DashboardError, match="already exists"):
        store.save("Revenue", _YAML.replace("Sales", "Other"))


def test_dashboards_keyed_by_uuid_are_migrated_to_their_names(tmp_path):
    """The tables from before: a UUID id, names that could repeat."""
    path = str(tmp_path / "portal.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE dashboards (id TEXT PRIMARY KEY, name TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',"
               " yaml TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("CREATE TABLE dashboard_versions (dashboard_id TEXT NOT NULL, version INTEGER NOT NULL,"
               " yaml TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL,"
               " PRIMARY KEY (dashboard_id, version))")
    db.execute("CREATE TABLE dashboard_uses (dashboard_id TEXT NOT NULL, dataset TEXT NOT NULL, column_name TEXT NOT NULL)")
    for id, at in (("u-1", "2026-01-01"), ("u-2", "2026-02-01")):
        db.execute("INSERT INTO dashboards VALUES (?, 'Sales', 'dana', ?, ?, ?)", (id, _YAML, at, at))
        db.execute("INSERT INTO dashboard_versions VALUES (?, 1, ?, 'dana', 0)", (id, _YAML))
    db.commit()
    db.close()

    store = SqlStore(path)
    assert sorted(r.name for r in store.list()) == ["Sales", "Sales 2"]
    assert store.get("Sales 2").yaml.startswith('name: "Sales 2"')    # the YAML says it too
    assert [v.version for v in store.versions("Sales 2")] == [1]
    assert {u.dashboard_id for u in store.uses()} == {"Sales", "Sales 2"}
    assert SqlStore(path).get("Sales") is not None                    # once only


# --- what the routes log -----------------------------------------------------------


def test_logins_and_logouts_are_logged_a_failed_one_as_401(portal, log):
    TestClient(app_mod.app, follow_redirects=False).post("/login", data={"username": "eve", "password": "guess"})
    portal.post("/logout")
    rows = _rows(log, "login") + _rows(log, "logout")
    assert [(r.actor, r.status_code) for r in rows] == [("admin", 303), ("eve", 401), ("admin", 303)]
    assert rows[0].detail == {"source": "local"} and rows[1].detail == {}   # never the password
    assert rows[0].user_agent == "testclient"


def test_a_dashboards_life_is_logged_by_name(portal, log):
    assert portal.get("/").headers["location"] == "/d/_admin"         # the admin's own group
    portal.get("/d/_admin")
    portal.post("/d/_admin/new", data={"name": "Sales"})
    portal.get("/d/_admin/Sales?role=viewer")
    portal.post("/d/_admin/Sales/save", data={"yaml_text": _YAML})
    assert portal.post("/d/_admin/Sales/save", data={"yaml_text": _YAML.replace("Sales", "Revenue")}).json() == {
        "ok": True, "id": "_admin/Revenue", "url": "/d/_admin/Revenue"}
    portal.post("/d/_admin/Revenue/clone", data={"name": ""})
    portal.post("/d/_admin/Revenue/delete")
    assert [(r.action, r.object_type, r.object_name, r.status_code) for r in _rows(log)] == [
        ("open", "gallery", "dashboards", 200),
        ("create", "dashboard", "Sales", 303),
        ("open", "dashboard", "Sales", 200),
        ("save", "dashboard", "Sales", 200),
        ("save", "dashboard", "Revenue", 200),
        ("clone", "dashboard", "Revenue (copy)", 303),
        ("delete", "dashboard", "Revenue", 303),
    ]
    assert {r.group for r in _rows(log)} == {"_admin"}         # the group, a column of its own
    opened, saved, renamed = _rows(log)[2:5]
    assert opened.detail == {"role": "viewer"}
    assert saved.detail == {"version": 2}                     # the YAML is in that version, not here
    assert renamed.detail == {"version": 3, "renamed_from": "Sales"}


def test_a_taken_name_is_refused_and_logged(portal, log):
    portal.post("/d/_admin/new", data={"name": "Sales"})
    assert portal.post("/d/_admin/new", data={"name": "Sales"}).status_code == 400
    assert [(r.action, r.status_code) for r in _rows(log)] == [("create", 303), ("create", 400)]


def test_renders_filters_and_tabs_are_logged_with_their_values(portal, log):
    headers = {"X-FF-Dashboard": "_admin%2FSales%20%C3%A9"}   # the editor's, encoded: group/name
    portal.post("/execute?active_tab=1&role=viewer", content=_YAML, headers=headers)
    portal.post("/dashboard", headers=headers, data={
        "yaml_text": _YAML, "filter_column": "status", "filter_op": "in", "filter_value": ["paid", "void"]})
    portal.post("/dashboard", data={"yaml_text": _YAML, "toggle": "rows|status|paid"})
    portal.post("/dashboard", data={"yaml_text": _YAML, "active_tab": "2"})
    refresh, added, clicked, tab = _rows(log)
    assert (refresh.action, refresh.object_name, refresh.group) == ("refresh", "Sales é", "_admin")
    assert refresh.detail == {"tab": 1, "role": "viewer"}
    assert added.action == "filter"
    assert added.detail["added"] == [{"column": "status", "op": "in", "values": ["paid", "void"]}]
    assert (clicked.action, clicked.object_name, clicked.detail["toggle"]) == ("crossfilter", "Sales", "rows|status|paid")
    assert (tab.action, tab.detail) == ("tab", {"tab": 2})


def test_a_renders_cells_are_not_logged_but_paging_one_is(portal, log):
    cell = {"yaml_text": _YAML, "cid": "rows"}
    portal.post("/dashboard/cell", data=cell)
    portal.post("/dashboard/cell", data={**cell, "table_page": "2"})
    portal.post("/dashboard/cell", data={**cell, "table_q": "paid"})
    assert [(r.action, r.detail.get("page"), r.detail.get("q")) for r in _rows(log)] == [
        ("page", 2, None), ("search", None, "paid")]


def test_editor_gestures_are_logged_a_refused_one_as_400(portal, log):
    two = _YAML.replace('layout:\n  - ["@40", "rows"]', 'layout:\n  - ["@40", "rows"]\n  - "# Header"')
    portal.post("/chart/config/header", data={"yaml_text": two, "index": "0", "text": "Top"})
    portal.post("/chart/config/delete", data={"yaml_text": _YAML, "cid": "nope"})
    header, refused = _rows(log)
    assert (header.action, header.object_name, header.status_code) == ("header", "Sales", 200)
    assert header.detail == {"index": 0, "text": "Top"}
    assert (refused.action, refused.status_code, refused.detail["cid"]) == ("delete_chart", 400, "nope")
    assert "error" in refused.detail


def test_a_chart_edit_keeps_its_form_but_not_the_yaml(portal, log):
    form = {"cid": "rows", "type": "table", "dataset": "orders", "title": "All rows"}
    portal.post("/chart/config/save", data={"yaml_text": _YAML, **form})
    [edit] = _rows(log)
    assert (edit.action, edit.status_code, edit.detail) == ("edit_chart", 200, form)


def test_user_changes_say_a_password_was_set_never_what_it_is(portal, log):
    viewer = app_mod.app.state.roles.create_role("viewer")
    portal.post("/users/new", data={"name": "bob", "role": [viewer], "password": "s3cret-pass"})
    portal.post("/users/" + app_mod.app.state.roles.user("bob").name + "/delete")
    created, deleted = _rows(log)
    assert (created.action, created.object_type, created.object_name, created.status_code) == ("create", "user", "bob", 303)
    assert created.detail == {"roles": ["viewer"], "password": "set"}
    assert "s3cret-pass" not in str([r.detail for r in log.entries()])
    assert (deleted.action, deleted.object_name) == ("delete", "bob")


def test_a_refusal_is_logged_with_its_code(portal, log, monkeypatch):
    monkeypatch.setattr(app_mod, "_is_admin", lambda request: False)
    assert portal.post("/users/new", data={"name": "bob"}).status_code == 403
    assert portal.post("/access/new", data={"name": "viewer"}).status_code == 403
    assert [(r.object_type, r.status_code) for r in _rows(log)] == [("user", 403), ("role", 403)]


def test_dataset_actions_are_logged_a_guarded_delete_as_409(portal, log):
    portal.post("/d/_admin/new", data={"name": "Sales"})
    portal.post("/d/_admin/Sales/save", data={"yaml_text": _YAML})
    portal.get("/datasets/_admin/orders")
    assert portal.post("/datasets/_admin/orders/delete").status_code == 409
    assert [(r.action, r.object_type, r.object_name, r.status_code) for r in _rows(log)][2:] == [
        ("open", "dataset", "orders", 200), ("delete", "dataset", "orders", 409)]


def test_the_editor_beacon_logs_only_its_own_actions(portal, log):
    assert portal.post("/activity", data={"action": "discard", "dashboard": "_admin/Sales"}).status_code == 204
    assert portal.post("/activity", data={"action": "mode", "dashboard": "_admin/Sales", "mode": "edit"}).status_code == 204
    assert portal.post("/activity", data={"action": "delete"}).status_code == 400
    assert [(r.action, r.group, r.object_name, r.detail) for r in _rows(log)] == [
        ("discard", "_admin", "Sales", {}), ("mode", "_admin", "Sales", {"mode": "edit"})]


def test_a_log_that_cant_be_written_doesnt_fail_the_request(portal, log, monkeypatch):
    def broken(entry):
        raise RuntimeError("database gone")
    monkeypatch.setattr(log, "record", broken)
    assert portal.get("/d/_admin").status_code == 200


def test_nothing_is_logged_outside_portal_mode(app_state, datasets, monkeypatch):  # noqa: F811
    app_state("activity", None)
    monkeypatch.setattr(app_mod, "_datasets_in", lambda group: datasets)
    assert TestClient(app_mod.app).post("/execute", content=_YAML).json()["ok"]
