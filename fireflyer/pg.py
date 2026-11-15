"""A Postgres connection that comes back.

Each portal store keeps one connection for the life of the process. When
Postgres restarts or fails over, or a proxy closes an idle connection, that
connection breaks, and without this every request after it would fail until
the process restarts. `connect(dsn)` gives the same `execute` / `rollback` as
a psycopg connection, but opens a new one when the old one is broken: before a
statement, and once more when a statement finds it broken. The statements run
in autocommit, so one that failed on a broken connection didn't commit, and
running it again on the new one is safe.

psycopg is imported only here and only when used: the `.[portal]` extra.
"""

from __future__ import annotations

import threading


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
        except psycopg.OperationalError:
            if not conn.broken:
                raise                       # the database said no: not the connection
            return self._live().execute(sql, params)

    def rollback(self) -> None:
        if not (self._conn.closed or self._conn.broken):
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()
