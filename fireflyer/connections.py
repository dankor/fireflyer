"""Connections — named databases a dataset's data can come from.

A connection is a name, a description and a SQLAlchemy URL
(`postgresql://reader@db:5432/sales`); its password is kept apart, encrypted
with `FIREFLYER_SECRET`, and never leaves the store — not to the browser, not
into a log, not into a dataset. A dataset names its connection by name only, in
its mapping's `source:` (`column_map`), as a permission names a role.

Every connection is a row: none is built in, each can be changed or removed.
A connection to Fireflyer's own database (the portal's `DATABASE_URL`, for usage
analytics — the portal adds one, `internal`, once at setup) offers one more
table than the database has, `dataset_versions`, parsed from the datasets'
documents (which Postgres can't read: they're YAML).

Reading runs the SQL as written, read-only, and builds a frame from what comes
back. Nothing checks the SQL here: the database does, and its error — the
message and, when the driver says, the line — is what a person sees.

Kept in the same database as roles and users: the portal's Postgres, or locally
`fireflyer.db` (sqlite).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import polars as pl

# The table a connection to Fireflyer's own database adds, parsed from the
# datasets' documents.
DATASET_VERSIONS = "dataset_versions"
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS connections ("
    " name TEXT PRIMARY KEY,"
    " description TEXT NOT NULL DEFAULT '',"
    # The SQLAlchemy URL without its password.
    " url TEXT NOT NULL,"
    # The password, encrypted (Fernet, keyed by FIREFLYER_SECRET); "" for none.
    " secret TEXT NOT NULL DEFAULT '',"
    " created_by TEXT NOT NULL DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)"
)


class ConnectionsError(ValueError):
    """A bad connection (name, URL), or a database that refused: the message is
    shown as it is."""


@dataclass
class Connection:
    name: str
    url: str                  # without the password
    description: str = ""
    has_password: bool = False
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --- the store ----------------------------------------------------------------------


class ConnectionStore:
    """The connections, in a database — sqlite (a file, or `:memory:` for the
    tests) or Postgres (`psycopg`, imported only then). `own_url`: Fireflyer's
    own database (portal mode; "" outside it) — a connection to it gets
    `dataset_versions`. `secret`: what passwords are encrypted with."""

    def __init__(self, dsn: str, secret: bytes, own_url: str = ""):
        self._own_url = own_url
        self._key = base64.urlsafe_b64encode(hashlib.sha256(b"connections:" + secret).digest())
        self._pg = dsn.startswith(("postgres://", "postgresql://"))
        if self._pg:
            import psycopg

            self._conn = psycopg.connect(dsn, autocommit=True)
        else:
            if dsn != ":memory:":
                Path(dsn).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._lock = threading.Lock()
        self._run(_SCHEMA)

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._pg:
            sql = sql.replace("?", "%s")
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            if not self._pg:
                self._conn.commit()
            return rows

    # --- reads ---------------------------------------------------------------

    def list(self) -> list[Connection]:
        """By name."""
        rows = self._run("SELECT name, url, description, secret, created_by, created_at, updated_at"
                         " FROM connections ORDER BY lower(name)")
        return [_row(r) for r in rows]

    def names(self) -> list[str]:
        return [c.name for c in self.list()]

    def get(self, name: str) -> Connection | None:
        rows = self._run("SELECT name, url, description, secret, created_by, created_at, updated_at"
                         " FROM connections WHERE name = ?", (name,))
        return _row(rows[0]) if rows else None

    def is_own(self, name: str) -> bool:
        """`name` reads Fireflyer's own database — the one `dataset_versions` is for."""
        found = self.get(name)
        return bool(self._own_url and found and _same_database(found.url, self._own_url))

    # --- writes --------------------------------------------------------------

    def create(self, name: str, url: str, *, description: str = "", password: str = "",
               author: str = "") -> Connection:
        """A new connection. A password written into `url` is taken out of it
        and kept as the password, unless `password` is given too."""
        name = _check_name(name)
        if self.get(name) is not None:
            raise ConnectionsError(f"a connection named {name!r} already exists")
        bare, in_url = _split_url(url)
        now = _now()
        self._run("INSERT INTO connections (name, url, description, secret, created_by, created_at, updated_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (name, bare, description.strip(), self._encrypt(password or in_url), author, now, now))
        return self.get(name)

    def update(self, name: str, *, new_name: str | None = None, url: str | None = None,
               description: str | None = None, password: str | None = None) -> Connection:
        """Change a connection; None keeps a field. `password=""` keeps it too
        (the form never shows it) — `clear_password` removes it."""
        current = self._known(name)
        new_name = _check_name(new_name) if new_name is not None else name
        if new_name != name and self.get(new_name) is not None:
            raise ConnectionsError(f"a connection named {new_name!r} already exists")
        bare, in_url = _split_url(url) if url is not None else (current.url, "")
        secret = password or in_url
        self._run("UPDATE connections SET name = ?, url = ?, description = ?, updated_at = ? WHERE name = ?",
                  (new_name, bare, current.description if description is None else description.strip(),
                   _now(), name))
        if secret:
            self._run("UPDATE connections SET secret = ? WHERE name = ?", (self._encrypt(secret), new_name))
        return self.get(new_name)

    def clear_password(self, name: str) -> None:
        self._known(name)
        self._run("UPDATE connections SET secret = '' WHERE name = ?", (name,))

    def delete(self, name: str) -> None:
        self._known(name)
        self._run("DELETE FROM connections WHERE name = ?", (name,))

    def _known(self, name: str) -> Connection:
        current = self.get(name)
        if current is None:
            raise ConnectionsError(f"unknown connection {name!r}")
        return current

    # --- the database itself -------------------------------------------------

    def url(self, name: str) -> str:
        """`name`'s full URL, password included — for opening it, never shown."""
        rows = self._run("SELECT url, secret FROM connections WHERE name = ?", (name,))
        if not rows:
            raise ConnectionsError(f"unknown connection {name!r}")
        url, secret = rows[0]
        return _with_password(url, self._decrypt(secret)) if secret else url

    def read(self, name: str, query: str, tables: dict[str, list[dict]] | None = None) -> pl.DataFrame:
        """`query`'s result on connection `name`, as a frame. `tables`: extra
        tables the query may name — `dataset_versions`, only on Fireflyer's own
        database."""
        return read(self.url(name), query, tables if self.is_own(name) else None)

    def test(self, name: str) -> None:
        """Open `name` and ask it something; raises `ConnectionsError` with the
        database's own words when that fails."""
        read(self.url(name), "select 1")

    def _encrypt(self, text: str) -> str:
        if not text:
            return ""
        from cryptography.fernet import Fernet

        return Fernet(self._key).encrypt(text.encode()).decode()

    def _decrypt(self, token: str) -> str:
        from cryptography.fernet import Fernet, InvalidToken

        try:
            return Fernet(self._key).decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ConnectionsError("the connection's password can't be read — "
                                   "was FIREFLYER_SECRET changed? Set the password again") from exc


def _row(r: tuple) -> Connection:
    name, url, description, secret, created_by, created_at, updated_at = r
    return Connection(name, url, description, bool(secret), created_by, created_at, updated_at)


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ConnectionsError("a connection's name is letters, digits, - and _")
    return name


# --- URLs: the password apart --------------------------------------------------------


def _split_url(url: str) -> tuple[str, str]:
    """`url` without its password, and the password ("" when none)."""
    from sqlalchemy.engine import URL, make_url
    from sqlalchemy.exc import ArgumentError

    try:
        parsed = make_url((url or "").strip())
    except ArgumentError as exc:
        raise ConnectionsError(f"not a database URL: {exc}") from exc
    # `set(password=None)` would keep it: None there means "unchanged".
    bare = URL.create(parsed.drivername, parsed.username, None, parsed.host, parsed.port,
                      parsed.database, parsed.query)
    return bare.render_as_string(hide_password=False), parsed.password or ""


def _same_database(a: str, b: str) -> bool:
    """`a` and `b` name one database: the same kind, server, port and database
    name — whatever the user or driver. A host name and its address match."""
    from sqlalchemy.engine import make_url

    def where(url):
        backend = url.get_backend_name()
        return backend, url.database or "", url.port or _PORTS.get(backend), _address(url.host)

    return where(make_url(a)) == where(make_url(b))


_PORTS = {"postgresql": 5432, "mysql": 3306, "mssql": 1433, "oracle": 1521}


def _address(host: str | None) -> str:
    import socket

    host = (host or "localhost").lower()
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def _without_password(url: str) -> str:
    return _split_url(url)[0]


def _with_password(url: str, password: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).set(password=password).render_as_string(hide_password=False)


def _engine_url(url: str):
    """A plain `postgresql://` means psycopg 3 here — the driver the portal
    already has (SQLAlchemy would look for psycopg2)."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed


# --- reading ------------------------------------------------------------------------


def read(url: str, query: str, tables: dict[str, list[dict]] | None = None) -> pl.DataFrame:
    """Run `query` on the database at `url` — read-only — and return what it
    gives. The SQL goes as written (with `tables` in front as a `with` block
    when it names one); a failure raises `ConnectionsError` in the database's
    words."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.pool import NullPool

    sql, offset = _with_tables(query.strip().rstrip(";").strip(), tables or {})
    try:
        engine = create_engine(_engine_url(url), poolclass=NullPool)
    except (SQLAlchemyError, ImportError, ValueError) as exc:
        raise ConnectionsError(f"can't open the connection: {exc}") from exc
    try:
        with engine.connect() as conn:
            _read_only(conn, engine.dialect.name)
            result = conn.exec_driver_sql(sql)
            columns = list(result.keys())
            rows = result.fetchall()
    except SQLAlchemyError as exc:
        raise ConnectionsError(_message(exc, offset)) from exc
    finally:
        engine.dispose()
    return _frame(columns, rows)


def _read_only(conn, dialect: str) -> None:
    """Nothing a dataset's SQL runs can change the database."""
    if dialect == "postgresql":
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
    elif dialect == "sqlite":
        conn.exec_driver_sql("PRAGMA query_only = ON")


def _frame(columns: list[str], rows: list) -> pl.DataFrame:
    """The rows as a frame, each column typed by its values. A column twice is
    the one thing refused here: a dataset's columns are named uniquely."""
    seen = set()
    for c in columns:
        if c in seen:
            raise ConnectionsError(f"the query gives two columns named {c!r} — name one of them (`as …`)")
        seen.add(c)
    values = {c: [_plain(r[i]) for r in rows] for i, c in enumerate(columns)}
    frame = pl.DataFrame(values, strict=False) if rows else pl.DataFrame({c: [] for c in columns})
    # A column with nothing in it, or values Polars keeps as objects: text.
    return frame.with_columns(
        pl.col(c).cast(pl.String) for c, t in frame.schema.items() if t in (pl.Null, pl.Object)
    )


def _plain(value):
    """A value as Polars types it: a decimal as a float, JSON as its text."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return value


def _message(exc, offset: int) -> str:
    """The database's own complaint, first line — and the line of the query it
    points at, when the driver says (Postgres does)."""
    orig = getattr(exc, "orig", None) or exc
    text = str(orig).strip().splitlines()[0] if str(orig).strip() else type(orig).__name__
    position = getattr(getattr(orig, "diag", None), "statement_position", None)
    statement = getattr(exc, "statement", None) or ""
    if position and statement:
        at = int(position) - 1 - offset
        if at >= 0:
            text += f" (line {statement[offset:offset + at].count(chr(10)) + 1})"
    return text


# --- `internal`'s extra table -------------------------------------------------------

_VERSION_COLUMNS = (
    ("group_name", "text"), ("dataset", "text"), ("version", "text"), ("current", "boolean"),
    ("status", "text"), ("author", "text"), ("created_at", "bigint"), ("rows", "bigint"),
    ("size", "bigint"), ("data_size", "bigint"), ("error", "text"),
)
_WITH_RE = re.compile(r"^\s*with\s+(recursive\s+)?", re.IGNORECASE)


def _with_tables(query: str, tables: dict[str, list[dict]]) -> tuple[str, int]:
    """`query` with each table it names from `tables` in front, as a `with`
    block over a JSON literal — so it stays plain Postgres and nothing is
    written to the database. Also how far the query moved (for an error's
    line)."""
    named = {t: rows for t, rows in tables.items() if re.search(rf"\b{t}\b", query, re.IGNORECASE)}
    if not named:
        return query, 0
    ctes = ",\n".join(_cte(t, rows) for t, rows in named.items())
    own = _WITH_RE.match(query)
    if own:
        recursive = "with recursive " if own.group(1) else "with "
        head = f"{recursive}{ctes},\n"
        return head + query[own.end():], len(head) - own.end()
    head = f"with {ctes}\n"
    return head + query, len(head)


def _cte(table: str, rows: list[dict]) -> str:
    data = json.dumps(rows, default=str)
    quote = "$ff$"
    if quote in data:                      # can't close early: a value holding it is dropped
        data = data.replace(quote, "")
    columns = ", ".join(f"{c} {t}" for c, t in _VERSION_COLUMNS)
    names = ", ".join(c for c, _ in _VERSION_COLUMNS if c != "created_at")
    return (f"{table} as (select {names}, to_timestamp(created_at) as created_at"
            f" from jsonb_to_recordset({quote}{data}{quote}::jsonb) as t({columns}))")
