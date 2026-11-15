"""Connections — named databases a dataset's data can come from.

A connection is a name, a description, a SQLAlchemy URL
(`postgresql+psycopg://reader@db:5432/sales`), as SQLAlchemy reads it — the
driver too, never a password — and the **name** of the environment variable
that holds its password (`password_env`). Nothing here keeps a password: the
value is read when the connection opens and goes to the driver beside the URL,
never into a URL string, the store, the browser, a log or a dataset. A dataset
names its connection by name only, in its mapping's `source:` (`column_map`),
as a permission names a role.

A connection may name only a variable the deployment allows
(`allowed_passwords`): `PGPASSWORD`, the portal's own, and those listed in
`FIREFLYER_CONNECTION_PASSWORDS`. If an admin could name any variable, they
could send `FIREFLYER_SECRET` to a server of their own as a password.

libpq (under psycopg) reads `PGPASSWORD` and `~/.pgpass` by itself for a
connection that gives no password: a connection to another server would get
the portal's password unasked. So `take_libpq_password` takes `PGPASSWORD` out
of the process's environment at start-up and keeps it here, and points
`PGPASSFILE` at nothing.

A connection to Fireflyer's own database (the portal's `DATABASE_URL`, for usage
analytics — the portal adds one, `internal`, once at setup) offers one more
table than the database has, `dataset_versions`, parsed from the datasets'
documents (which Postgres can't read: they're YAML).

Reading runs the SQL as written and builds a frame from what comes back. It
never commits: SQLAlchemy rolls back what the query did when the connection
closes, whatever the database — Fireflyer knows no dialect. A connection's
own user decides what it may do (give it a read-only one). Nothing checks the SQL here: the database does, and its error — the
message and, when the driver says, the line — is what a person sees.

Kept in the same database as roles and users: the portal's Postgres, or locally
`fireflyer.db` (sqlite).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import polars as pl

from fireflyer import pg

# The table a connection to Fireflyer's own database adds, parsed from the
# datasets' documents.
DATASET_VERSIONS = "dataset_versions"
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

# The portal's own password: what `internal` names, always allowed.
OWN_PASSWORD = "PGPASSWORD"
# The other variables a connection may name, comma-separated — the deployment's.
PASSWORDS_ALLOWED = "FIREFLYER_CONNECTION_PASSWORDS"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS connections ("
    " name TEXT PRIMARY KEY,"
    " description TEXT NOT NULL DEFAULT '',"
    # The SQLAlchemy URL: never a password in it.
    " url TEXT NOT NULL,"
    # The environment variable that holds the password; "" for none.
    " password_env TEXT NOT NULL DEFAULT '',"
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
    url: str                  # never a password in it
    description: str = ""
    password_env: str = ""    # the variable with its password; "" for none
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
    `dataset_versions`."""

    def __init__(self, dsn: str, own_url: str = ""):
        self._own_url = own_url
        self._pg = dsn.startswith(("postgres://", "postgresql://"))
        if self._pg:
            self._conn = pg.connect(dsn)
        else:
            if dsn != ":memory:":
                Path(dsn).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._lock = threading.Lock()
        self._run(_SCHEMA)
        self._drop_secrets()

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._pg:
            sql = sql.replace("?", "%s")
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            if not self._pg:
                self._conn.commit()
            return rows

    def _drop_secrets(self) -> None:
        """Passwords were once kept here, encrypted: the column goes, and a
        connection to the portal's own database names the portal's password —
        the others name none until an admin gives them a variable."""
        with self._lock:
            columns = [d[0] for d in self._conn.execute("SELECT * FROM connections LIMIT 0").description]
        if "secret" not in columns:
            return
        self._run("ALTER TABLE connections ADD COLUMN password_env TEXT NOT NULL DEFAULT ''")
        held = self._run("SELECT name, url FROM connections WHERE secret <> ''")
        for name, url in held:
            if self._own_url and _same_database(url, self._own_url):
                self._run("UPDATE connections SET password_env = ? WHERE name = ?", (OWN_PASSWORD, name))
        self._run("ALTER TABLE connections DROP COLUMN secret")

    # --- reads ---------------------------------------------------------------

    def list(self) -> list[Connection]:
        """By name."""
        rows = self._run("SELECT name, url, description, password_env, created_by, created_at, updated_at"
                         " FROM connections ORDER BY lower(name)")
        return [_row(r) for r in rows]

    def names(self) -> list[str]:
        return [c.name for c in self.list()]

    def get(self, name: str) -> Connection | None:
        rows = self._run("SELECT name, url, description, password_env, created_by, created_at, updated_at"
                         " FROM connections WHERE name = ?", (name,))
        return _row(rows[0]) if rows else None

    def is_own(self, name: str) -> bool:
        """`name` reads Fireflyer's own database — the one `dataset_versions` is for."""
        found = self.get(name)
        return bool(self._own_url and found and _same_database(found.url, self._own_url))

    # --- writes --------------------------------------------------------------

    def create(self, name: str, url: str, *, description: str = "", password_env: str = "",
               author: str = "") -> Connection:
        """A new connection: `url` without a password, `password_env` the
        variable that holds it (an allowed one), or "" for none."""
        name = _check_name(name)
        if self.get(name) is not None:
            raise ConnectionsError(f"a connection named {name!r} already exists")
        url, password_env = _check_url(url), _check_variable(password_env)
        now = _now()
        self._run("INSERT INTO connections (name, url, description, password_env, created_by, created_at,"
                  " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (name, url, description.strip(), password_env, author, now, now))
        return self.get(name)

    def update(self, name: str, *, new_name: str | None = None, url: str | None = None,
               description: str | None = None, password_env: str | None = None) -> Connection:
        """Change a connection; None keeps a field."""
        current = self._known(name)
        new_name = _check_name(new_name) if new_name is not None else name
        if new_name != name and self.get(new_name) is not None:
            raise ConnectionsError(f"a connection named {new_name!r} already exists")
        self._run("UPDATE connections SET name = ?, url = ?, description = ?, password_env = ?, updated_at = ?"
                  " WHERE name = ?",
                  (new_name, current.url if url is None else _check_url(url),
                   current.description if description is None else description.strip(),
                   current.password_env if password_env is None else _check_variable(password_env),
                   _now(), name))
        return self.get(new_name)

    def delete(self, name: str) -> None:
        self._known(name)
        self._run("DELETE FROM connections WHERE name = ?", (name,))

    def _known(self, name: str) -> Connection:
        current = self.get(name)
        if current is None:
            raise ConnectionsError(f"unknown connection {name!r}")
        return current

    # --- the database itself -------------------------------------------------

    def _open(self, name: str):
        """`name`'s URL to open: a SQLAlchemy `URL`, its variable's password
        set in it — an object, never rendered into a string."""
        from sqlalchemy.engine import make_url

        found = self._known(name)
        url = make_url(found.url)
        if not found.password_env:
            return url
        if problem := password_problem(found.password_env):
            raise ConnectionsError(problem)
        return url.set(password=env_password(found.password_env))

    def read(self, name: str, query: str, tables: dict[str, list[dict]] | None = None) -> pl.DataFrame:
        """`query`'s result on connection `name`, as a frame. `tables`: extra
        tables the query may name — `dataset_versions`, only on Fireflyer's own
        database."""
        return read(self._open(name), query, tables if self.is_own(name) else None)

    def test(self, name: str) -> None:
        """Open `name` and ask it something; raises `ConnectionsError` with the
        database's own words when that fails."""
        read(self._open(name), "select 1")


def _row(r: tuple) -> Connection:
    return Connection(*r)


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ConnectionsError("a connection's name is letters, digits, - and _")
    return name


# --- passwords: a variable's name, its value read when a connection opens ------------

# What `take_libpq_password` took out of the environment, by variable name.
_taken: dict[str, str] = {}


def take_libpq_password(own_url: str = "", env=os.environ) -> str:
    """At start-up: take `PGPASSWORD` out of `env` and keep it here, and point
    `PGPASSFILE` at nothing — libpq would give either to any Postgres server a
    connection names without a password. The portal's own password — one in
    `own_url`, else `PGPASSWORD`'s — is what a connection naming `PGPASSWORD`
    gets. Returns `own_url` with that password in it, for the portal's stores
    (libpq no longer finds it on its own)."""
    from_env = env.pop(OWN_PASSWORD, None)
    env["PGPASSFILE"] = os.devnull
    in_url = _url_password(own_url) if own_url else ""
    own = in_url or from_env or _taken.get(OWN_PASSWORD)
    if own:
        _taken[OWN_PASSWORD] = own
    if own_url and own and not in_url:
        from urllib.parse import urlencode

        own_url += ("&" if "?" in own_url else "?") + urlencode({"password": own})
    return own_url


def env_password(name: str) -> str | None:
    """Variable `name`'s value — from the environment, or what start-up took
    out of it; None when it isn't set."""
    return os.environ.get(name, _taken.get(name))


def allowed_passwords(env=os.environ) -> list[str]:
    """The variables a connection may name: `PGPASSWORD`, the portal's own,
    then `FIREFLYER_CONNECTION_PASSWORDS`'s, in its order."""
    listed = [v.strip() for v in env.get(PASSWORDS_ALLOWED, "").split(",") if v.strip()]
    return list(dict.fromkeys([OWN_PASSWORD, *listed]))


def password_problem(name: str) -> str:
    """Why variable `name` gives no password — "" when it does."""
    if name not in allowed_passwords():
        return f"{name} isn't an allowed password variable ({PASSWORDS_ALLOWED} lists them)"
    if env_password(name) is None:
        return f"{name} is not set"
    return ""


def _check_variable(name: str) -> str:
    name = (name or "").strip()
    if name and name not in allowed_passwords():
        raise ConnectionsError(f"{name} isn't an allowed password variable ({PASSWORDS_ALLOWED} lists them)")
    return name


def _parse(url: str):
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        return make_url((url or "").strip())
    except ArgumentError as exc:
        raise ConnectionsError(f"not a database URL: {exc}") from exc


def _url_password(url: str) -> str:
    """The password written into `url` — in it (`user:pass@`) or in its query
    (`?password=`); "" when none."""
    parsed = _parse(url)
    return parsed.password or parsed.query.get("password") or ""


def _check_url(url: str) -> str:
    """`url`, trimmed — refused with a password in it."""
    url = (url or "").strip()
    if _url_password(url):
        raise ConnectionsError("a URL holds no password: name the variable that holds it instead")
    return url


def without_password(url: str) -> str:
    """`url` with its password taken out, wherever it is written."""
    from sqlalchemy.engine import URL

    parsed = _parse(url)
    query = {k: v for k, v in parsed.query.items() if k != "password"}
    # `set(password=None)` would keep it: None there means "unchanged".
    bare = URL.create(parsed.drivername, parsed.username, None, parsed.host, parsed.port,
                      parsed.database, query)
    return bare.render_as_string(hide_password=False)


# --- URLs ------------------------------------------------------------------------------


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


# --- reading ------------------------------------------------------------------------


def read(url, query: str, tables: dict[str, list[dict]] | None = None) -> pl.DataFrame:
    """Run `query` on the database at `url` (text, or a SQLAlchemy `URL` with
    its password set) and return what it gives; nothing
    is committed. The SQL goes as written (with `tables` in front as a `with` block
    when it names one); a failure raises `ConnectionsError` in the database's
    words."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.pool import NullPool

    sql, offset = _with_tables(query.strip().rstrip(";").strip(), tables or {})
    try:
        engine = create_engine(url, poolclass=NullPool)    # the URL as written
    except (SQLAlchemyError, ImportError, ValueError) as exc:
        raise ConnectionsError(f"can't open the connection: {exc}") from exc
    try:
        with engine.connect() as conn:          # no commit(): closing rolls it back
            result = conn.exec_driver_sql(sql)
            columns = list(result.keys())
            rows = result.fetchall()
    except SQLAlchemyError as exc:
        raise ConnectionsError(_message(exc, offset)) from exc
    finally:
        engine.dispose()
    return _frame(columns, rows)


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
