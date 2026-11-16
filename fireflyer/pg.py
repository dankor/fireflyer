"""A Postgres connection that comes back.

Each portal store keeps one connection for the life of the process. When
Postgres restarts or fails over, or a proxy closes an idle connection, that
connection breaks, and without this every request after it would fail until
the process restarts. `connect(dsn)` gives the same `execute` / `rollback` as
a psycopg connection, but opens a new one when the old one is broken, before
the next statement.

A statement that breaks the connection is a different case: the client cannot
know whether the server ran it. The server can commit a write (autocommit) and
then lose the connection before its reply arrives. So only a read — a plain
`select` — runs once more, on a new connection: a read changes nothing. Any
other statement is never sent again; `execute` raises `WriteUnknown`, and the
person who asked for the change checks it before they ask again.

psycopg is imported only here and only when used: the `.[portal]` extra.
"""

from __future__ import annotations

import threading


class WriteUnknown(Exception):
    """The connection broke during a change: it is possibly saved, possibly not."""

    def __init__(self):
        super().__init__("The connection to the database broke during the change. "
                         "The change is possibly saved. Check it before you do it again.")


def connect(dsn: str) -> "Connection":
    return Connection(dsn)


def check(dsn: str, timeout: int = 2) -> None:
    """The readiness probe's question: a new connection to `dsn` answers
    `select 1` — within `timeout` seconds to connect and as many to answer
    (libpq's least is 2). Not the stores' connections: a probe mustn't wait
    behind their work, and a new one proves the credentials too."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True, connect_timeout=timeout,
                         options=f"-c statement_timeout={timeout * 1000}") as conn:
        conn.execute("select 1").fetchone()


def _reads_only(sql: str) -> bool:
    """A plain `select`: running it twice changes nothing. A `with` can hold an
    insert, so it counts as a write."""
    return sql.lstrip()[:6].lower() == "select"


class Connection:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn = self._open()

    def _open(self):
        import psycopg

        return psycopg.connect(self._dsn, autocommit=True)

    def _live(self):
        with self._lock:
            if self._conn.closed or self._conn.broken:
                self._conn = self._open()
            return self._conn

    def execute(self, sql: str, params=()):
        import psycopg

        conn = self._live()
        try:
            return conn.execute(sql, params)
        except psycopg.OperationalError as exc:
            if not conn.broken:
                raise                       # the database said no: not the connection
            if not _reads_only(sql):
                raise WriteUnknown() from exc   # the next statement opens a new connection
            return self._live().execute(sql, params)

    def rollback(self) -> None:
        if not (self._conn.closed or self._conn.broken):
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()
