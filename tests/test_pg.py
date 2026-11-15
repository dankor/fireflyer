"""The portal's Postgres connection comes back after it breaks (`fireflyer.pg`).
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
        return sql

    def rollback(self):
        pass


class FakeServer:
    """Connections opened before `restart()` are broken; new ones work."""

    def __init__(self):
        self.module = types.SimpleNamespace(OperationalError=type("OperationalError", (Exception,), {}),
                                            connect=self.connect)
        self.opened, self.ran, self.restarted_at = [], [], 0

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


def test_a_broken_connection_is_opened_again(server):
    conn = pg.connect("postgresql://db/ff")
    conn.execute("select 1")
    server.restart()
    # The statement that finds it broken runs once more, on a new connection.
    assert conn.execute("select 2") == "select 2"
    assert len(server.opened) == 2 and server.ran == ["select 1", "select 2"]
    conn.execute("select 3")                                    # and stays on it
    assert len(server.opened) == 2


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
