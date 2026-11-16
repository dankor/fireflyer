"""The portal's Postgres connection comes back after it breaks (`fireflyer.pg`),
and never sends a change twice: a write whose reply was lost may be committed.
psycopg is faked: the suite needs no database."""

import sys
import types

import pytest

from fireflyer import pg


class FakeConnection:
    def __init__(self, server):
        self.server, self.closed, self.broken = server, False, False

    def execute(self, sql, params=()):
        if self.server.down_for(self):
            self.broken = True
            raise self.server.module.OperationalError("server closed the connection unexpectedly")
        if sql == "bad":
            raise self.server.module.OperationalError("syntax error")      # the database said no
        self.server.ran.append(sql)
        if self.server.lose_reply:                  # committed, then the connection went
            self.server.lose_reply, self.broken = False, True
            raise self.server.module.OperationalError("server closed the connection unexpectedly")
        return sql

    def rollback(self):
        pass


class FakeServer:
    """Connections opened before `restart()` are broken; new ones work."""

    def __init__(self):
        self.module = types.SimpleNamespace(OperationalError=type("OperationalError", (Exception,), {}),
                                            connect=self.connect)
        self.opened, self.ran, self.restarted_at, self.lose_reply = [], [], 0, False

    def connect(self, dsn, autocommit):
        assert autocommit
        self.opened.append(FakeConnection(self))
        return self.opened[-1]

    def restart(self):
        self.restarted_at = len(self.opened)

    def down_for(self, conn):
        return self.opened.index(conn) < self.restarted_at


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setitem(sys.modules, "psycopg", fake.module)
    return fake


def test_a_read_that_finds_it_broken_runs_once_more_on_a_new_connection(server):
    conn = pg.connect("postgresql://db/ff")
    conn.execute("select 1")
    server.restart()
    assert conn.execute("  SELECT 2") == "  SELECT 2"
    assert len(server.opened) == 2 and server.ran == ["select 1", "  SELECT 2"]
    conn.execute("select 3")                                    # and stays on it
    assert len(server.opened) == 2


def test_a_write_whose_reply_was_lost_is_not_sent_again(server):
    """The server committed it; the client only saw the connection break."""
    conn = pg.connect("postgresql://db/ff")
    server.lose_reply = True
    with pytest.raises(pg.WriteUnknown, match="possibly saved"):
        conn.execute("INSERT INTO t VALUES (1)")
    assert server.ran == ["INSERT INTO t VALUES (1)"] and len(server.opened) == 1      # once, not twice
    assert conn.execute("select 1") == "select 1"                  # the next one: a new connection
    assert len(server.opened) == 2


@pytest.mark.parametrize("sql", ["UPDATE t SET a = 1", "DELETE FROM t", "with x as (insert into t values (1)) select 1"])
def test_no_change_runs_again_after_a_break(server, sql):
    conn = pg.connect("postgresql://db/ff")
    server.restart()
    with pytest.raises(pg.WriteUnknown):
        conn.execute(sql)
    assert server.ran == []
    conn.execute(sql)                                              # asked again: a new connection
    assert server.ran == [sql] and len(server.opened) == 2


def test_a_closed_connection_is_opened_before_the_statement(server):
    conn = pg.connect("postgresql://db/ff")
    server.opened[0].closed = True
    conn.execute("select 1")
    assert len(server.opened) == 2 and server.ran == ["select 1"]


def test_an_error_of_the_database_is_not_tried_again(server):
    conn = pg.connect("postgresql://db/ff")
    with pytest.raises(server.module.OperationalError, match="syntax"):
        conn.execute("bad")
    assert len(server.opened) == 1


def test_the_probe_opens_its_own_bounded_connection(monkeypatch):
    opened = []

    class Probe:
        def __init__(self, dsn, **options):
            opened.append((dsn, options))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            opened.append("closed")

        def execute(self, sql):
            assert sql == "select 1"
            return types.SimpleNamespace(fetchone=lambda: (1,))

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=Probe))
    pg.check("postgresql://ff@db/ff")
    assert opened == [("postgresql://ff@db/ff", {"autocommit": True, "connect_timeout": 2,
                                                  "options": "-c statement_timeout=2000"}), "closed"]


def test_the_app_says_a_change_is_possibly_saved(app_state):
    """Never "failed": 503 and `{ok: false, error}`, which the editor shows."""
    from starlette.testclient import TestClient

    from fireflyer.web import app as app_mod

    class LostReply:
        def verify(self, username, password):
            raise pg.WriteUnknown()

    app_state("authenticator", LostReply())
    answer = TestClient(app_mod.app).post("/login", data={"username": "a", "password": "b"})
    assert answer.status_code == 503
    assert answer.json() == {"ok": False, "error": str(pg.WriteUnknown())}
    assert "possibly saved" in answer.json()["error"]


def test_an_upload_whose_catalog_write_is_uncertain_is_not_a_storage_error(monkeypatch):
    """The upload's broad catch is for the object store: an uncertain write
    still gets the app's 503, not "Storage error" (502)."""
    from starlette.testclient import TestClient

    from fireflyer.web import app as app_mod

    class Store:
        def create(self, *args, **kwargs):
            raise pg.WriteUnknown()

    monkeypatch.setattr(app_mod, "_dataset_store", lambda request: Store())
    answer = TestClient(app_mod.app).post("/datasets/local/new", data={"name": "zoo"},
                                          files={"file": ("zoo.csv", b"a\n1\n")})
    assert answer.status_code == 503 and "possibly saved" in answer.json()["error"]
