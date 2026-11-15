"""Connections: named databases a dataset's data comes from — the store (a
password variable's name, never a password), reading a query into a typed frame, `source:`
in a dataset's mapping, refresh as the next major, the routes, and the usage
analytics shipped with the portal. Databases here are sqlite files; Postgres
only in Docker."""

import json
import os
import pathlib
import re
import sqlite3

import polars as pl
import pytest
import yaml
from starlette.testclient import TestClient

from fireflyer import column_map
from fireflyer import connections as connections_mod
from fireflyer.calcs import compile_formula
from fireflyer.connections import (
    ConnectionStore, ConnectionsError, _with_tables, read,
)
from fireflyer.datasets import DatasetError, DatasetStore
from fireflyer.storage import LocalObjectStore
from fireflyer.web import app as app_mod
from fireflyer.web import usage as usage_mod
from fireflyer.web.portal import render_connections, render_datasets
from fireflyer.web.usage import DASHBOARD, DATASETS


@pytest.fixture
def db(tmp_path):
    """A sqlite database with a `people` table — its URL."""
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.execute("create table people (id integer, name text, score real, joined text)")
    conn.executemany("insert into people values (?, ?, ?, ?)",
                     [(1, "ann", 1.5, "2026-01-02"), (2, "bob", None, "2026-03-04")])
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


@pytest.fixture
def store():
    return ConnectionStore(":memory:")


@pytest.fixture
def passwords(monkeypatch):
    """SALES_PASSWORD allowed and set; what start-up took from the environment
    kept apart for the test."""
    monkeypatch.setattr(connections_mod, "_taken", {})
    monkeypatch.setenv("FIREFLYER_CONNECTION_PASSWORDS", "SALES_PASSWORD, OTHER_PASSWORD")
    monkeypatch.setenv("SALES_PASSWORD", "s3c@r/t")
    monkeypatch.delenv("OTHER_PASSWORD", raising=False)


# --- the store -------------------------------------------------------------------


def test_a_connection_is_created_listed_edited_and_removed(store, db):
    store.create("shop", db, description=" The shop ", author="dana")
    [c] = store.list()
    assert (c.name, c.url, c.description, c.created_by) == ("shop", db, "The shop", "dana")
    store.update("shop", new_name="store", description="Renamed")
    assert store.names() == ["store"] and store.get("store").description == "Renamed"
    store.delete("store")
    assert store.list() == []


def test_a_connection_names_its_password_variable(store, passwords):
    store.create("pg", "postgresql://reader@db:5432/sales", password_env="SALES_PASSWORD")
    c = store.get("pg")
    assert (c.url, c.password_env) == ("postgresql://reader@db:5432/sales", "SALES_PASSWORD")
    assert "s3c@r/t" not in str(store._run("SELECT * FROM connections"))      # the name, never the value
    url = store._open("pg")                       # read when it opens: an object, the value set in it
    assert url.password == "s3c@r/t" and "s3c" not in str(url) and "s3c" not in repr(url)
    store.update("pg", url="postgresql://reader@db2/sales")                   # None keeps it
    assert store.get("pg").password_env == "SALES_PASSWORD"
    store.update("pg", password_env="")
    assert store._open("pg").password is None


def test_a_url_holds_no_password(store, passwords):
    for url in ("postgresql://u:pw@h/d", "postgresql://u@h/d?password=pw"):
        with pytest.raises(ConnectionsError, match="holds no password"):
            store.create("pg", url)
    store.create("pg", "postgresql://u@h/d")
    with pytest.raises(ConnectionsError, match="holds no password"):
        store.update("pg", url="postgresql://u:pw@h/d")


def test_only_an_allowed_variable_is_named(store, passwords, monkeypatch):
    for name in ("FIREFLYER_SECRET", "FIREFLYER_S3_SECRET_KEY", "HOME"):
        with pytest.raises(ConnectionsError, match="isn't an allowed password variable"):
            store.create("pg", "postgresql://u@h/d", password_env=name)
    assert connections_mod.allowed_passwords() == ["PGPASSWORD", "SALES_PASSWORD", "OTHER_PASSWORD"]
    store.create("pg", "postgresql://u@h/d", password_env="SALES_PASSWORD")
    store.create("later", "postgresql://u@h/d", password_env="OTHER_PASSWORD")     # allowed, not set yet
    with pytest.raises(ConnectionsError, match="OTHER_PASSWORD is not set"):
        store.test("later")
    monkeypatch.setenv("FIREFLYER_CONNECTION_PASSWORDS", "")                      # no longer allowed
    with pytest.raises(ConnectionsError, match="SALES_PASSWORD isn't an allowed"):
        store.test("pg")


def test_start_up_takes_the_portal_password_out_of_the_environment(passwords):
    env = {"PGPASSWORD": "p@ss&w=rd", "PGHOST": "db"}
    own = connections_mod.take_libpq_password("postgresql://ff@db/ff", env)
    # Out of the environment, so libpq can't give it to another server; the
    # passfile, the same.
    assert "PGPASSWORD" not in env and env["PGPASSFILE"] == os.devnull and env["PGHOST"] == "db"
    assert connections_mod.env_password("PGPASSWORD") == "p@ss&w=rd"         # what `internal` names
    # The portal's own stores get it in their URL, encoded.
    assert own.startswith("postgresql://ff@db/ff?password=")
    assert connections_mod._url_password(own) == "p@ss&w=rd"


def test_a_password_in_the_portal_url_is_the_portal_password(passwords):
    env = {"PGPASSWORD": "other"}
    own = connections_mod.take_libpq_password("postgresql://ff:inline@db/ff", env)
    assert own == "postgresql://ff:inline@db/ff" and "PGPASSWORD" not in env
    assert connections_mod.env_password("PGPASSWORD") == "inline"
    assert connections_mod.take_libpq_password("", {}) == ""                     # local mode: nothing to add


def test_stored_passwords_from_before_are_dropped(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE connections (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',"
                 " url TEXT NOT NULL, secret TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',"
                 " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.executemany("INSERT INTO connections VALUES (?, '', ?, ?, '', 't', 't')", [
        ("internal", "postgresql+psycopg://ff@postgres:5432/ff", "gAAAA-encrypted"),
        ("sales", "postgresql+psycopg://reader@sales/db", "gAAAA-encrypted"),
        ("open", "sqlite:///x.db", ""),
    ])
    conn.commit()
    conn.close()
    store = ConnectionStore(str(path), own_url="postgresql://ff@postgres:5432/ff?password=pw")
    assert {c.name: c.password_env for c in store.list()} == {"internal": "PGPASSWORD", "open": "", "sales": ""}
    assert "secret" not in str(store._run("SELECT * FROM connections"))
    ConnectionStore(str(path))                                                  # once only


@pytest.mark.parametrize("name", ["", "has space", "a/b", "ü"])
def test_a_connection_name_is_plain(store, db, name):
    with pytest.raises(ConnectionsError, match="letters, digits"):
        store.create(name, db)


def test_names_are_unique_and_urls_must_read(store, db):
    store.create("shop", db)
    with pytest.raises(ConnectionsError, match="already exists"):
        store.create("shop", db)
    with pytest.raises(ConnectionsError, match="not a database URL"):
        store.create("other", "nonsense")


def test_a_connection_to_the_own_database_gets_dataset_versions(monkeypatch):
    store = ConnectionStore(":memory:", own_url="postgresql://ff:pw@postgres:5432/ff")
    assert store.list() == []
    store.create("internal", "postgresql://ff@postgres:5432/ff")
    store.create("copy", "postgresql+psycopg://reader@POSTGRES/ff")      # the same database, another way
    store.create("sales", "postgresql://ff@postgres:5432/sales")          # another database there
    assert [store.is_own(n) for n in ("internal", "copy", "sales")] == [True, True, False]
    seen = []
    monkeypatch.setattr(connections_mod, "read", lambda url, query, tables=None: seen.append(tables))
    extra = {"dataset_versions": []}
    for name in ("copy", "sales"):
        store.read(name, "select 1", extra)
    assert seen == [extra, None]                                          # only on its own database
    store.update("internal", description="mine")                          # changed, then removed
    store.delete("internal")
    assert store.names() == ["copy", "sales"]


# --- reading -----------------------------------------------------------------------


def test_a_query_reads_into_a_typed_frame(db):
    frame = read(db, "select id, name, score, joined from people order by id;")
    assert frame.schema == {"id": pl.Int64, "name": pl.String, "score": pl.Float64, "joined": pl.String}
    assert frame.rows() == [(1, "ann", 1.5, "2026-01-02"), (2, "bob", None, "2026-03-04")]


def test_an_empty_result_keeps_its_columns(db):
    frame = read(db, "select id, name from people where id > 9")
    assert frame.columns == ["id", "name"] and frame.height == 0


def test_the_database_says_what_is_wrong(db):
    with pytest.raises(ConnectionsError, match="syntax error"):
        read(db, "selec 1")
    with pytest.raises(ConnectionsError, match="no such table: nobody"):
        read(db, "select * from nobody")


def test_two_columns_of_one_name_are_refused(db):
    with pytest.raises(ConnectionsError, match="two columns named 'id'"):
        read(db, "select id, id from people")


def test_a_query_is_never_committed(db):
    with pytest.raises(ConnectionsError, match="does not return rows"):
        read(db, "delete from people")                  # no rows to read, and rolled back
    assert read(db, "delete from people returning id").height == 2
    assert read(db, "select count(*) as n from people")["n"].item() == 2


def test_internal_tables_go_in_front_as_a_with_block():
    rows = {"dataset_versions": [{"dataset": "orders"}]}
    sql, moved = _with_tables("select * from dataset_versions", rows)
    assert sql.startswith("with dataset_versions as (select ") and sql.endswith("\nselect * from dataset_versions")
    assert sql[moved:] == "select * from dataset_versions"
    # A query with its own `with` keeps it, after ours.
    sql, _ = _with_tables("WITH x as (select 1) select * from x, dataset_versions", rows)
    assert sql.startswith("with dataset_versions as (") and "),\nx as (select 1) select" in sql
    # Not named: nothing added.
    assert _with_tables("select 1", rows) == ("select 1", 0)


# --- the mapping: source:, types ----------------------------------------------------

SOURCE = {"connection": "shop", "query": "select id, name\nfrom people"}


def test_the_source_is_written_first_and_read_back():
    text = column_map.dump([column_map.Rule("id", "to_number(id)")], {"viewer": []}, SOURCE)
    assert text.startswith("source:\n  connection: shop\n  query: |-\n    select id, name\n    from people\ncolumns:")
    assert column_map.source(text) == SOURCE
    # Changing permissions or upgrading keeps it.
    assert column_map.source(column_map.with_permissions(text, {})) == SOURCE
    assert column_map.upgrade(text) == text
    assert column_map.source("columns:\n  id: id\n") is None


@pytest.mark.parametrize("raw, message", [
    ({"connection": "shop"}, "both"), ({"query": "select 1"}, "both"),
    ({"connection": "shop", "query": "q", "extra": 1}, "takes a `connection:`"), ("text", "takes"),
])
def test_a_bad_source_is_refused(raw, message):
    import yaml

    text = yaml.safe_dump({"source": raw, "columns": {"id": "id"}})
    with pytest.raises(column_map.MappingError, match=message):
        column_map.parse(text, ["id"])


def test_a_result_reads_back_as_its_types():
    import datetime as dt

    frame = pl.DataFrame({
        "n": [1], "f": [1.5], "ok": [True], "day": [dt.date(2026, 1, 2)],
        "at": [dt.datetime(2026, 1, 2, 3, 4, 5, 123000, tzinfo=dt.timezone.utc)],
        "local": [dt.datetime(2026, 1, 2, 3, 4, 5)], "tags": [["a", "b"]], "text": ["x"],
    })
    rules = column_map.default(frame.columns, dtypes={c: str(t) for c, t in frame.schema.items()})
    assert [r.formula for r in rules] == [
        "to_number(n)", "to_number(f)", "to_bool(ok)", "str2dt(day, YYYY-MM-DD)",
        "str2dt(at, YYYY-MM-DD HH:mm:ssZ)", "str2dt(local, YYYY-MM-DD HH:mm:ss)", "to_list(tags)", "text",
    ]
    back = column_map.apply(column_map.as_text(frame), rules)
    assert back.schema["ok"] == pl.Boolean and back.schema["day"] == pl.Date
    assert back.schema["tags"] == pl.List(pl.String) and back["at"].item() == frame["at"].item()


def test_to_bool_reads_yes_and_no():
    expr, _ = compile_formula("to_bool(x)", pl.col)
    values = ["true", " Yes", "1", "f", "NO", "0", "", "maybe", None]
    assert pl.DataFrame({"x": values}).select(expr).to_series().to_list() == [
        True, True, True, False, False, False, None, None, None]


# --- datasets from a query -----------------------------------------------------------


@pytest.fixture
def datasets(tmp_path):
    return DatasetStore(LocalObjectStore(str(tmp_path / "objects")))


def test_a_query_dataset_keeps_its_result_and_its_source(datasets, db):
    frame = read(db, "select id, name, score from people")
    ds = datasets.create_query("people", frame, SOURCE, description="Our people", author="dana")
    assert ds.version == "1.0" and [(c.name, c.dtype) for c in ds.columns] == [
        ("id", "Int64"), ("name", "String"), ("score", "Float64")]
    assert datasets.query_source("people") == SOURCE
    [v] = datasets.versions("people")
    assert v.from_query and v.size > 0
    assert datasets._store.exists("datasets/people/raw/1.parquet")


def test_a_refresh_is_the_next_major_and_keeps_the_mapping(datasets, db):
    datasets.create_query("people", read(db, "select id, name from people"), SOURCE)
    edited = datasets.mapping("people").replace("  name:\n    formula: name", "  who:\n    formula: name")
    datasets.remap("people", edited)
    assert datasets.query_source("people") == SOURCE           # a remap keeps the source
    sqlite3.connect(db.removeprefix("sqlite:///")).execute("insert into people values (3, 'cy', 2, null)").connection.commit()
    ds = datasets.refresh("people", read(db, "select id, name, score from people"), author="dana")
    assert ds.version == "2.2" and ds.rows == 3
    # The rename stays; the new column reads back as a number.
    assert [(c.name, c.dtype) for c in ds.columns] == [("id", "Int64"), ("who", "String"), ("score", "Float64")]


def test_a_refresh_with_a_new_query_takes_its_mapping(datasets, db):
    datasets.create_query("people", read(db, "select id, name from people"), SOURCE)
    new = {"connection": "shop", "query": "select id, joined from people"}
    mapping = column_map.dump([column_map.Rule("id", "id")], None, new)
    ds = datasets.refresh("people", read(db, new["query"]), mapping=mapping)
    assert datasets.query_source("people") == new
    # `name` dropped (its column is gone); `joined` is new: as it is (text).
    assert [c.name for c in ds.columns] == ["id", "joined"]


def test_an_upload_drops_the_source(datasets, db):
    datasets.create_query("people", read(db, "select id, name from people"), SOURCE)
    datasets.replace("people", b"id,name\n9,zed\n")
    assert datasets.query_source("people") is None and not datasets.versions("people")[0].from_query


def test_a_failed_refresh_is_recorded_and_the_data_kept(datasets, db):
    datasets.create_query("people", read(db, "select id from people"), SOURCE)
    datasets.record_error("people", "relation does not exist")
    ds = datasets.get("people")
    assert ds.error == "relation does not exist" and ds.version == "1.0" and ds.rows == 2


def test_every_version_is_a_row(datasets, db):
    datasets.create_query("people", read(db, "select id from people"), SOURCE)
    datasets.create("orders", b"id\n1\n")
    rows = {r["dataset"]: r for r in datasets.every_version()}
    assert set(rows) == {"people", "orders"}
    people = rows["people"]
    assert people["version"] == "1.0" and people["current"] and people["rows"] == 2
    assert people["size"] > 0 and people["data_size"] > 0 and "mapping" not in people


# --- the pages --------------------------------------------------------------------


def test_the_connections_page_names_each_password_variable(store, passwords):
    store.create("pg", "postgresql://reader@db/sales", description="Sales", password_env="SALES_PASSWORD")
    store.create("later", "postgresql://reader@db/sales", password_env="OTHER_PASSWORD")
    html = render_connections(store.list(), connections_mod.allowed_passwords(),
                              used={"pg": [("/datasets/x/orders", "orders")]})
    assert "postgresql://reader@db/sales" in html and "s3c" not in html
    assert ">orders<" in html and 'title="Password from SALES_PASSWORD"' in html
    assert 'class="ff-key is-missing" title="OTHER_PASSWORD is not set"' in html
    assert 'data-password-env="SALES_PASSWORD"' in html
    # The dialog picks from the allowed variables; one not set is marked.
    assert '<select name="password_env"' in html and '<option value="SALES_PASSWORD">' in html
    assert '<option value="OTHER_PASSWORD" class="is-unset"' in html and 'type="password"' not in html
    assert 'data-url="/connections/pg/test"' in html and "openConnection(this)" in html


def test_the_datasets_list_offers_a_query_dataset_to_whoever_may_connect():
    assert "ds-kinds" not in render_datasets([])                          # a file only
    html = render_datasets([], sources=["shop", "internal"])
    assert html.count('title="New dataset"') == 1                          # one +, a CSV | SQL switch
    assert 'data-kind="sql"' in html and '<option value="internal">' in html
    assert 'data-sql="/datasets/local/new-query"' in html
    # The query is a code editor: the textarea stays, as what the form posts.
    assert "data-sql-editor" in html and "sql-editor.js" in html and '"fireflyer/sql-editor"' in html
    assert "sql-editor.js" not in render_datasets([])


# --- routes -----------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, datasets):
    monkeypatch.setattr(app_mod, "_datasets_in", lambda group: datasets)
    return TestClient(app_mod.app)


def test_connections_are_managed_through_the_routes(client, db):
    conns = app_mod.app.state.connections
    client.post("/connections/new", data={"name": "shop", "url": db, "description": "Shop"})
    assert conns.get("shop").description == "Shop" and ">shop<" in client.get("/connections").text
    assert client.post("/connections/shop/test").json() == {"ok": True}
    # YAML instead of the fields.
    client.post("/connections/shop/edit",
                data={"yaml": f"name: shop\ndescription: From YAML\nurl: {db}\npassword_env: PGPASSWORD\n"})
    assert (conns.get("shop").description, conns.get("shop").password_env) == ("From YAML", "PGPASSWORD")
    client.post("/connections/shop/edit", data={"name": "shop", "url": db, "password_env": ""})
    assert conns.get("shop").password_env == ""
    client.post("/connections/new", data={"yaml": "name: bad\nurl: sqlite:////nowhere/x.db\n"})
    assert client.post("/connections/bad/test").json()["ok"] is False
    assert client.post("/connections/new", data={"yaml": "name: x\nport: 1\n"}).status_code == 400
    refused = client.post("/connections/new", data={"yaml": f"name: x\nurl: {db}\npassword: pw\n"})
    assert refused.status_code == 400 and "password_env" in refused.text
    client.post("/connections/bad/delete")
    assert conns.names() == ["shop"]


def test_a_query_dataset_is_created_refreshed_and_guards_its_connection(client, datasets, db):
    app_mod.app.state.connections.create("shop", db)
    made = client.post("/datasets/local/new-query", data={
        "name": "people", "connection": "shop", "query": "select id, name from people"})
    assert made.status_code == 200 and datasets.get("people").version == "1.0"
    page = client.get("/datasets/local/people").text
    assert 'aria-label="Refresh"' in page and "From a database query" in page
    client.post("/datasets/local/people/refresh")
    assert datasets.get("people").version == "2.0"
    # The database's error is the answer.
    bad = client.post("/datasets/local/new-query", data={"name": "x", "connection": "shop", "query": "selec"})
    assert bad.status_code == 400 and "syntax error" in bad.text
    # A connection a dataset reads from can't go; a rename follows into it.
    assert client.post("/connections/shop/delete").status_code == 409
    client.post("/connections/shop/edit", data={"name": "store", "url": db})
    assert datasets.query_source("people")["connection"] == "store"


def test_a_changed_query_in_the_yaml_is_run_and_checked(client, datasets, db):
    app_mod.app.state.connections.create("shop", db)
    client.post("/datasets/local/new-query", data={"name": "people", "connection": "shop",
                                                    "query": "select id from people"})
    text = datasets.mapping("people").replace("select id from people", "select id, score from people")
    checked = client.post("/datasets/local/people/mapping/check", data={"mapping": text}).json()
    assert [r["name"] for r in checked["rules"]] == ["id"]
    assert checked["source"] == {"connection": "shop", "query": "select id, score from people"}  # edit mode's SQL
    assert client.post("/datasets/local/people/mapping", data={"mapping": text}).status_code == 200
    assert [c.name for c in datasets.get("people").columns] == ["id", "score"]
    wrong = text.replace("from people", "from nobody")
    answer = client.post("/datasets/local/people/mapping", data={"mapping": wrong})
    assert answer.status_code == 400 and "no such table: nobody" in answer.json()["error"]


def _page_data(html: str) -> dict:
    return json.loads(re.search(r'id="ds-data">(.*?)</script>', html, re.S).group(1))


def test_edit_mode_gets_the_query_and_who_may_change_it(client, monkeypatch, datasets, db):
    app_mod.app.state.connections.create("shop", db)
    client.post("/datasets/local/new-query", data={"name": "people", "connection": "shop",
                                                    "query": "select id from people"})
    data = _page_data(client.get("/datasets/local/people").text)
    assert data["source"] == {"connection": "shop", "query": "select id from people"}
    assert data["connections"] == ["shop"]                       # an admin may switch it
    monkeypatch.setattr(app_mod, "_users_on", lambda: True)
    monkeypatch.setattr(app_mod, "_is_admin", lambda request: False)
    monkeypatch.setattr(app_mod, "_can_write", lambda request, group=None: True)
    data = _page_data(client.get("/datasets/local/people").text)
    assert data["source"]["query"] == "select id from people" and data["connections"] is None   # read-only


def test_only_an_admin_connects(client, monkeypatch, datasets, db):
    monkeypatch.setattr(app_mod, "_users_on", lambda: True)
    monkeypatch.setattr(app_mod, "_is_admin", lambda request: False)
    monkeypatch.setattr(app_mod, "_can_write", lambda request, group=None: True)
    assert client.get("/connections").status_code == 403
    assert client.post("/connections/new", data={"name": "shop", "url": db}).status_code == 403
    refused = client.post("/datasets/local/new-query", data={"name": "p", "connection": "shop", "query": "select 1"})
    assert refused.status_code == 403
    # Nor through a dataset's YAML.
    datasets.create("orders", b"id\n1\n")
    text = column_map.dump([column_map.Rule("id", "id")], None, SOURCE)
    assert client.post("/datasets/local/orders/mapping", data={"mapping": text}).status_code == 403


# --- usage analytics ------------------------------------------------------------------


def test_the_usage_dashboard_is_valid_and_reads_the_usage_datasets():
    from fireflyer.dashboard import Dashboard

    dash = Dashboard.from_yaml(DASHBOARD)
    assert dash.name == "Fireflyer Usage"
    import yaml

    read_by_charts = {c["dataset"] for c in yaml.safe_load(DASHBOARD)["charts"].values()}
    assert read_by_charts <= set(DATASETS)


def test_usage_ships_as_files_beside_its_code():
    folder = pathlib.Path(usage_mod.__file__).parent
    assert sorted(DATASETS) == sorted(p.stem for p in (folder / "datasets").glob("*.yaml"))
    for name, (description, sql) in DATASETS.items():
        assert description, name
        assert sql.lower().startswith(("select", "with")), name
    assert DASHBOARD == (folder / "dashboard.yaml").read_text()
    assert usage_mod.DASHBOARD_NAME == "Fireflyer Usage"


def test_the_usage_connection_names_its_driver():
    # A connection's URL goes to SQLAlchemy as written; the portal's DSN gets
    # the driver it has.
    # No password in it, wherever the DSN has one: it names PGPASSWORD.
    for dsn in ("postgresql://ff:pw@postgres:5432/ff", "postgresql://ff@postgres:5432/ff?password=pw"):
        assert usage_mod.connection_url(dsn) == "postgresql+psycopg://ff@postgres:5432/ff"


def test_the_usage_errors_say_why():
    assert "->> 'error' as error" in DATASETS["usage_activity"][1]
    assert "error" in yaml.safe_load(DASHBOARD)["charts"]["errors"]["columns"]


def test_every_usage_header_is_text():          # a bare `On` or `Yes` would be a bool
    for calcs in yaml.safe_load(DASHBOARD)["calcs"].values():
        assert all(isinstance(calc["name"], str) for calc in calcs.values())


def test_the_usage_sql_exposes_usernames_only():
    sql = " ".join(q for _, q in DATASETS.values()).lower()
    for secret in ("password_hash", "session_key", "email", "first_name", "last_name", " ip", "user_agent"):
        assert secret not in sql
