"""Portal mode: DB-backed dashboard persistence + a listing gallery.

Editor-only, like the rest of `web/`. This is an owner-approved exception to the
"no persistence / multi-user" anti-goal in architecture.md, kept deliberately
small: dashboards are stored as an opaque YAML text blob (validated by the
backend via `Dashboard.from_yaml`, never decomposed into tables), so every
existing stateless editor route keeps working byte-for-byte.

Two stores share one tiny schema. `SqliteStore` (stdlib, in-memory by default)
powers local dev and the test suite — no service, no driver. `PostgresStore`
powers `python -m fireflyer.portal` at runtime and imports `psycopg` lazily so
the core install and `pip install -e ".[test]"` never need the `.[portal]`
extra. Tests exercise this module directly (no web stack), matching how
params/config_edit logic is kept unit-testable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

import yaml

from fireflyer import column_map
from fireflyer import filters as filters_mod
from fireflyer.dashboard import Dashboard
from fireflyer.datasets import Dataset, Version
from fireflyer.params import COLUMN_TYPES, column_type, type_icon_svg
from fireflyer.web import assets
from fireflyer.web import auth

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboards ("
    " id TEXT PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " author TEXT NOT NULL DEFAULT '',"
    " yaml TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)"
)
# Every saved state of every dashboard, numbered 1, 2, 3… per dashboard, with
# who saved it and when (Unix seconds). What the editor's compare view diffs.
_VERSIONS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboard_versions ("
    " dashboard_id TEXT NOT NULL,"
    " version INTEGER NOT NULL,"
    " yaml TEXT NOT NULL,"
    " author TEXT NOT NULL DEFAULT '',"
    " created_at BIGINT NOT NULL,"
    " PRIMARY KEY (dashboard_id, version))"
)


# What each dashboard reads — `(dataset, column)` pairs from `Dashboard.uses`,
# written on every save — so the overviews (a dashboard's datasets, a dataset's
# dashboards, a column's) are a query, not a parse of every dashboard. Each
# indexed dashboard also has one `('', '')` row, so "indexed, uses nothing" is
# told apart from "not indexed yet" (a dashboard from before the index).
_USES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboard_uses ("
    " dashboard_id TEXT NOT NULL,"
    " dataset TEXT NOT NULL,"
    " column_name TEXT NOT NULL)"
)


@dataclass(frozen=True)
class Use:
    """One thing a dashboard reads: a dataset (`column` ""), one of its columns,
    or every column (`column` "*")."""
    dashboard_id: str
    dashboard_name: str
    dataset: str
    column: str


def _uses_of(yaml: str) -> list[tuple[str, str]]:
    try:
        return Dashboard.uses(yaml)
    except DashboardError:
        return []


@dataclass
class DashboardRow:
    id: str
    name: str
    author: str
    yaml: str
    updated_at: str

    @property
    def description(self) -> str:
        """The YAML's top-level `description:`, read on demand rather than kept
        as a column: like `name`, it's part of the definition, and every store
        already holds the YAML. "" when absent or the YAML doesn't parse."""
        try:
            raw = yaml.safe_load(self.yaml) or {}
        except yaml.YAMLError:
            return ""
        description = raw.get("description") if isinstance(raw, dict) else None
        return description.strip() if isinstance(description, str) else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DashboardVersion:
    """One saved state of a dashboard. `yaml` is "" in a listing — fetched on
    its own when a version is opened."""
    version: int
    created_at: int        # Unix seconds; the browser shows it in local time
    author: str
    yaml: str = ""


def _unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return int(time.time())


class _SqlVersions:
    """Version history over SQL, shared by both stores. A store supplies
    `_run(sql, params)` — `?` placeholders, the store adapts them — and these
    record a version on create and save, list them, and fetch one.

    A save that changes nothing records nothing. A dashboard saved before
    history existed gets its stored state recorded as version 1 on its first
    save, so even an old dashboard can be compared with its first edit."""

    def _latest(self, id: str):
        return self._run(
            "SELECT version, yaml FROM dashboard_versions WHERE dashboard_id = ?"
            " ORDER BY version DESC LIMIT 1", (id,),
        ).fetchone()

    def _record(self, id: str, yaml: str, author: str, at: int | None = None) -> int:
        latest = self._latest(id)
        if latest and latest[1] == yaml:
            return latest[0]
        number = latest[0] + 1 if latest else 1
        self._run(
            "INSERT INTO dashboard_versions (dashboard_id, version, yaml, author, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (id, number, yaml, author, at if at is not None else int(time.time())),
        )
        return number

    def _baseline(self, id: str) -> None:
        """Record what's stored now as version 1, if this dashboard has none."""
        if self._latest(id) is None:
            row = self.get(id)
            if row is not None:
                self._record(id, row.yaml, row.author, _unix(row.updated_at))

    def versions(self, id: str) -> list[DashboardVersion]:
        """Newest first, without their YAML."""
        rows = self._run(
            "SELECT version, created_at, author FROM dashboard_versions"
            " WHERE dashboard_id = ? ORDER BY version DESC", (id,),
        ).fetchall()
        return [DashboardVersion(int(v), int(at), a) for v, at, a in rows]

    def version(self, id: str, number: int) -> DashboardVersion | None:
        row = self._run(
            "SELECT version, created_at, author, yaml FROM dashboard_versions"
            " WHERE dashboard_id = ? AND version = ?", (id, number),
        ).fetchone()
        return DashboardVersion(int(row[0]), int(row[1]), row[2], row[3]) if row else None


class _SqlUses:
    """The `dashboard_uses` index over SQL, shared by both stores (`_run` as in
    `_SqlVersions`). Written on create/save, dropped on delete; a dashboard not
    indexed yet is indexed the first time the index is read."""

    def _index(self, id: str, yaml: str) -> None:
        self._run("DELETE FROM dashboard_uses WHERE dashboard_id = ?", (id,))
        for dataset, column in [("", "")] + _uses_of(yaml):
            self._run(
                "INSERT INTO dashboard_uses (dashboard_id, dataset, column_name) VALUES (?, ?, ?)",
                (id, dataset, column),
            )

    def uses(self) -> list[Use]:
        for id, yaml in self._run(
            "SELECT id, yaml FROM dashboards"
            " WHERE id NOT IN (SELECT dashboard_id FROM dashboard_uses)"
        ).fetchall():
            self._index(str(id), yaml)
        rows = self._run(
            "SELECT u.dashboard_id, d.name, u.dataset, u.column_name FROM dashboard_uses u"
            " JOIN dashboards d ON d.id = u.dashboard_id WHERE u.dataset <> ''"
            " ORDER BY d.name, u.dataset, u.column_name"
        ).fetchall()
        return [Use(str(i), n, ds, c) for i, n, ds, c in rows]


# The Access tab's roles: a name each, and when it was made and last renamed.
_ROLES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS roles ("
    " id TEXT PRIMARY KEY,"
    " name TEXT NOT NULL UNIQUE,"
    " description TEXT NOT NULL DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)"
)


# The Users tab's people (portal mode): a name each, and the roles they hold —
# by role id, so a renamed role stays held.
_USERS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS users ("
    " id TEXT PRIMARY KEY,"
    " name TEXT NOT NULL UNIQUE,"
    " first_name TEXT NOT NULL DEFAULT '',"
    " last_name TEXT NOT NULL DEFAULT '',"
    " email TEXT NOT NULL DEFAULT '',"
    # A salted scrypt hash (auth.hash_password) or "" — never the password; and
    # the key a sign-in session carries, rotated with the password.
    " password_hash TEXT NOT NULL DEFAULT '',"
    # Who proves who they are: `local` (the password above), or the name of the
    # identity provider that does (sso, ldap, …) — see `RoleStore.provision`.
    " source TEXT NOT NULL DEFAULT 'local',"
    " session_key TEXT NOT NULL DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)"
)
_USER_ROLES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS user_roles ("
    " user_id TEXT NOT NULL,"
    " role_id TEXT NOT NULL,"
    " PRIMARY KEY (user_id, role_id))"
)


class RoleError(ValueError):
    """A role (or user) name that's empty or already taken, or roles a user
    can't hold together."""


# Portal mode's built-ins: the role `admin` — every dataset, in full, so no
# dataset's permissions name it — and the user who logs in (`FIREFLYER_USER`),
# who holds it. Neither can be renamed, edited or removed.
ADMIN = "admin"
LOCAL = "local"           # a user who signs in with a password kept here
_ADMIN_ABOUT = "Built-in"
_ADMIN_USER_ID = "admin"


@dataclass
class Role:
    id: str
    name: str
    created_at: str        # ISO UTC, as the dashboards' timestamps
    updated_at: str
    description: str = ""  # what the role is for — under its name, and in tooltips
    builtin: bool = False  # the admin role: shown locked


@dataclass
class User:
    id: str
    name: str
    created_at: str
    updated_at: str
    roles: list[str] = field(default_factory=list)  # the role names held, sorted
    first_name: str = ""                            # these three optional
    last_name: str = ""
    email: str = ""
    builtin: bool = False                           # the admin login: shown locked
    has_password: bool = False                      # whether they can sign in — never the hash
    source: str = LOCAL                             # who proves who they are: here, or a provider

    @property
    def external(self) -> bool:
        """Signs in through another system (SSO, LDAP, …), which owns the name
        and the credentials; only the roles are managed here."""
        return self.source != LOCAL

    @property
    def about(self) -> str:
        """Full name · email — the small line under the user's name, as a
        role's description is under its."""
        full = " ".join(p for p in (self.first_name, self.last_name) if p)
        return " · ".join(p for p in (full, self.email) if p)


def _person(first_name: str, last_name: str, email: str) -> tuple[str, str, str]:
    """A user's optional details, trimmed; an email, if given, needs an @."""
    email = email.strip()
    if email and "@" not in email:
        raise RoleError(f"{email!r} isn't an email")
    return first_name.strip(), last_name.strip(), email


def _role_name(name: str, roles: list, keep: str = "", kind: str = "role") -> str:
    """`name` trimmed, checked: not empty, and no other role (than `keep`, the
    one being renamed) has it. Users' names too (`kind`)."""
    name = name.strip()
    if not name:
        raise RoleError(f"a {kind} needs a name")
    if any(r.name == name and r.id != keep for r in roles):
        raise RoleError(f"there is already a {kind} named {name!r}")
    return name


def role_clashes(roles: list[str], permissions: dict[str, dict]) -> list[tuple[str, list[str]]]:
    """(dataset, the roles) for each dataset that more than one of `roles` may
    read — `permissions`: dataset -> its role -> filters. A user holds at most
    one role per dataset, so what they see of it is never ambiguous."""
    clashes = []
    for dataset, perms in sorted(permissions.items()):
        both = sorted(r for r in roles if r in perms)
        if len(both) > 1:
            clashes.append((dataset, both))
    return clashes


def clash_error(user: str, clashes: list[tuple[str, list[str]]]) -> RoleError:
    dataset, roles = clashes[0]
    return RoleError(f"{user} can't hold {' and '.join(roles)}: both may read {dataset}")


class RoleStore:
    """The roles, in a database — the portal's (sqlite, or Postgres with a
    `postgresql://` DSN; `psycopg` imported only then), or a local sqlite file
    when there's no portal. Its own connection, like the dataset catalog's.

    `admin`: the login's user name, in portal mode — the store then always has
    the built-in `admin` role and lists that user first, holding it; both
    locked. "" (local mode, tests) has neither."""

    def __init__(self, dsn: str, admin: str = ""):
        self._pg = dsn.startswith(("postgres://", "postgresql://"))
        if self._pg:
            import psycopg

            self._conn = psycopg.connect(dsn, autocommit=True)
        else:
            if dsn != ":memory:":
                Path(dsn).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._run(_ROLES_SCHEMA)
        self._run(_USERS_SCHEMA)
        self._run(_USER_ROLES_SCHEMA)
        self._add_description()
        self._admin = admin
        if admin and not any(r.name == ADMIN for r in self.roles()):
            self.create_role(ADMIN, _ADMIN_ABOUT)

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._pg:
            sql = sql.replace("?", "%s")
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall() if cursor.description else []
        if not self._pg:
            self._conn.commit()
        return rows

    def _add_description(self) -> None:
        """A roles table from before descriptions gets the column."""
        try:
            self._run("ALTER TABLE roles ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass                                   # there already

    def roles(self) -> list[Role]:
        rows = self._run("SELECT id, name, created_at, updated_at, description FROM roles ORDER BY name")
        builtin = lambda n: bool(self._admin) and n == ADMIN
        return [Role(str(i), n, str(c), str(u), _ADMIN_ABOUT if builtin(n) else d or "", builtin(n))
                for i, n, c, u, d in rows]

    def _unlocked(self, roles_or_users: list, id: str, kind: str) -> None:
        if any(x.id == id and x.builtin for x in roles_or_users):
            raise RoleError(f"the {kind} is built in")

    def create_role(self, name: str, description: str = "") -> str:
        name = _role_name(name, self.roles())
        new_id, now = str(uuid.uuid4()), _now()
        self._run(
            "INSERT INTO roles (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (new_id, name, description.strip(), now, now),
        )
        return new_id

    def rename_role(self, id: str, name: str, description: str | None = None) -> None:
        """A new name — and description, unless None (kept as it is)."""
        self._unlocked(self.roles(), id, "admin role")
        name = _role_name(name, self.roles(), keep=id)
        if description is None:
            self._run("UPDATE roles SET name = ?, updated_at = ? WHERE id = ?", (name, _now(), id))
        else:
            self._run("UPDATE roles SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                      (name, description.strip(), _now(), id))

    def delete_role(self, id: str) -> None:
        self._unlocked(self.roles(), id, "admin role")
        self._run("DELETE FROM user_roles WHERE role_id = ?", (id,))
        self._run("DELETE FROM roles WHERE id = ?", (id,))

    # --- users ---

    def users(self) -> list[User]:
        held: dict[str, list[str]] = {}
        for user_id, role in self._run(
            "SELECT ur.user_id, r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id ORDER BY r.name"
        ):
            held.setdefault(str(user_id), []).append(role)
        rows = self._run(
            "SELECT id, name, created_at, updated_at, first_name, last_name, email,"
            " password_hash <> '', source FROM users ORDER BY name"
        )
        stored = [User(str(i), n, str(c), str(u), held.get(str(i), []), f or "", l or "", e or "",
                       has_password=bool(pw), source=src or LOCAL)
                  for i, n, c, u, f, l, e, pw, src in rows]
        if not self._admin:
            return stored
        # Not a row: the login, first — its name is taken for any other user.
        # Its password is the environment's, not managed here.
        return [User(_ADMIN_USER_ID, self._admin, "", "", [ADMIN], builtin=True, has_password=True)] + stored

    def user(self, name: str) -> User | None:
        return next((u for u in self.users() if u.name == name), None)

    def create_user(self, name: str, role_ids: list[str] = (),
                    first_name: str = "", last_name: str = "", email: str = "") -> str:
        name = _role_name(name, self.users(), kind="user")
        person = _person(first_name, last_name, email)
        new_id, now = str(uuid.uuid4()), _now()
        self._run(
            "INSERT INTO users (id, name, first_name, last_name, email, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id, name, *person, now, now),
        )
        self._hold(new_id, role_ids)
        return new_id

    def edit_user(self, id: str, name: str, role_ids: list[str] = (),
                  first_name: str = "", last_name: str = "", email: str = "") -> None:
        self._unlocked(self.users(), id, "admin user")
        held = self._by_id(id)
        if held and held.external and name.strip() != held.name:
            raise RoleError(f"{held.name}'s name comes from {held.source}")
        name = _role_name(name, self.users(), keep=id, kind="user")
        person = _person(first_name, last_name, email)
        self._run(
            "UPDATE users SET name = ?, first_name = ?, last_name = ?, email = ?, updated_at = ? WHERE id = ?",
            (name, *person, _now(), id),
        )
        self._run("DELETE FROM user_roles WHERE user_id = ?", (id,))
        self._hold(id, role_ids)

    def _by_id(self, id: str) -> User | None:
        return next((u for u in self.users() if u.id == id), None)

    def provision(self, name: str, source: str, first_name: str = "", last_name: str = "",
                  email: str = "") -> str:
        """A user another system (`source`: sso, ldap, …) has just signed in:
        made here the first time — no roles yet, no password — and their
        profile refreshed from the provider after. Returns the key their
        session carries. Refused for a name a local user or the admin holds,
        or another provider's: one system can't sign in as another's user."""
        if source == LOCAL:
            raise RoleError("a local user signs in with their password")
        held = self.user(name)
        if held and (held.builtin or held.source != source):
            raise RoleError(f"{name} doesn't sign in through {source}")
        person = _person(first_name, last_name, email)
        if held is None:
            user_id, now = str(uuid.uuid4()), _now()
            self._run(
                "INSERT INTO users (id, name, first_name, last_name, email, source, session_key,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, _role_name(name, self.users(), kind="user"), *person, source,
                 auth.new_session_key(), now, now),
            )
        elif any(person):
            fields = dict(zip(("first_name", "last_name", "email"), person))
            given = {k: v for k, v in fields.items() if v}
            sets = ", ".join(f"{k} = ?" for k in given)
            self._run(f"UPDATE users SET {sets}, updated_at = ? WHERE id = ?", (*given.values(), _now(), held.id))
        return self.session_key(name)

    def _hold(self, user_id: str, role_ids) -> None:
        known = {r.id for r in self.roles()}
        for role_id in dict.fromkeys(role_ids):
            if role_id in known:
                self._run("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (user_id, role_id))

    # --- passwords: only ever the hash, and it never leaves the store ---

    def set_password(self, id: str, password: str) -> None:
        """A new password for user `id` (the policy checked first) — which
        also rotates their session key, signing them out everywhere."""
        self._unlocked(self.users(), id, "admin user")
        self._local(id)
        try:
            hashed = auth.hash_password(auth.check_policy(password))
        except auth.PasswordError as exc:
            raise RoleError(str(exc)) from exc
        self._run("UPDATE users SET password_hash = ?, session_key = ?, updated_at = ? WHERE id = ?",
                  (hashed, auth.new_session_key(), _now(), id))

    def clear_password(self, id: str) -> None:
        """No password: the user can't sign in, and is signed out."""
        self._unlocked(self.users(), id, "admin user")
        self._local(id)
        self._run("UPDATE users SET password_hash = '', session_key = ?, updated_at = ? WHERE id = ?",
                  (auth.new_session_key(), _now(), id))

    def _local(self, id: str) -> None:
        held = self._by_id(id)
        if held and held.external:
            raise RoleError(f"{held.name} signs in through {held.source}: no password here")

    def verify(self, name: str, password: str) -> str | None:
        """`name` when `password` is theirs (an Authenticator) — else None,
        taking as long for an unknown name, a user with no password, or one
        who signs in elsewhere."""
        rows = self._run("SELECT password_hash FROM users WHERE name = ? AND source = ?", (name, LOCAL))
        stored = rows[0][0] if rows else ""
        return name if auth.password_matches(password, stored) else None

    def session_key(self, name: str) -> str | None:
        """The key `name`'s sessions must carry, or None: no such user, or a
        local one with no password — a session that can't be valid."""
        rows = self._run(
            "SELECT session_key FROM users WHERE name = ? AND (source <> ? OR password_hash <> '')",
            (name, LOCAL),
        )
        return rows[0][0] if rows else None

    def delete_user(self, id: str) -> None:
        self._unlocked(self.users(), id, "admin user")
        self._run("DELETE FROM user_roles WHERE user_id = ?", (id,))
        self._run("DELETE FROM users WHERE id = ?", (id,))


def _name_of(yaml_text: str) -> str:
    """The dashboard's display name, from the YAML's required top-level `name:`
    key. Also validates: `from_yaml` raises DashboardError on an invalid or
    nameless dashboard. Parsing only — never opens the CSV, so a missing dataset
    path is fine."""
    return Dashboard.from_yaml(yaml_text).name


class SqliteStore(_SqlVersions, _SqlUses):
    """Local/test store. `:memory:` for tests, a file path for local portal dev."""

    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False: uvicorn serves requests off a threadpool.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_VERSIONS_SCHEMA)
        self._conn.execute(_USES_SCHEMA)
        self._conn.commit()

    def _run(self, sql: str, params=()):
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor

    def list(self) -> list[DashboardRow]:
        rows = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards"
            " ORDER BY updated_at DESC"
        ).fetchall()
        return [DashboardRow(*r) for r in rows]

    def get(self, id: str) -> DashboardRow | None:
        row = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards WHERE id = ?",
            (id,),
        ).fetchone()
        return DashboardRow(*row) if row else None

    def create(self, yaml: str, author: str = "") -> str:
        name = _name_of(yaml)  # validates + reads the top-level `name:` key
        new_id, now = str(uuid.uuid4()), _now()
        self._conn.execute(
            "INSERT INTO dashboards (id, name, author, yaml, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, name, author, yaml, now, now),
        )
        self._conn.commit()
        self._record(new_id, yaml, author)
        self._index(new_id, yaml)
        return new_id

    def save(self, id: str, yaml: str, author: str = "") -> None:
        # Name is re-derived from the YAML; author (the creator) is left as-is.
        # `author` is who saved — recorded on the version, not the dashboard.
        name = _name_of(yaml)
        self._baseline(id)
        self._conn.execute(
            "UPDATE dashboards SET name = ?, yaml = ?, updated_at = ? WHERE id = ?",
            (name, yaml, _now(), id),
        )
        self._conn.commit()
        self._record(id, yaml, author)
        self._index(id, yaml)

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = ?", (id,))
        self._conn.execute("DELETE FROM dashboard_versions WHERE dashboard_id = ?", (id,))
        self._conn.execute("DELETE FROM dashboard_uses WHERE dashboard_id = ?", (id,))
        self._conn.commit()


class PostgresStore(_SqlVersions, _SqlUses):
    """Runtime store. Same schema, `%s` placeholders; imports psycopg lazily so
    this module stays importable without the `.[portal]` extra installed."""

    def __init__(self, dsn: str):
        import psycopg  # optional dependency, only needed at portal runtime

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_VERSIONS_SCHEMA)
        self._conn.execute(_USES_SCHEMA)

    def _run(self, sql: str, params=()):
        return self._conn.execute(sql.replace("?", "%s"), params)

    def list(self) -> list[DashboardRow]:
        rows = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards"
            " ORDER BY updated_at DESC"
        ).fetchall()
        return [DashboardRow(str(r[0]), r[1], r[2], r[3], str(r[4])) for r in rows]

    def get(self, id: str) -> DashboardRow | None:
        row = self._conn.execute(
            "SELECT id, name, author, yaml, updated_at FROM dashboards WHERE id = %s",
            (id,),
        ).fetchone()
        if not row:
            return None
        return DashboardRow(str(row[0]), row[1], row[2], row[3], str(row[4]))

    def create(self, yaml: str, author: str = "") -> str:
        name = _name_of(yaml)  # validates + reads the top-level `name:` key
        new_id, now = str(uuid.uuid4()), _now()
        self._conn.execute(
            "INSERT INTO dashboards (id, name, author, yaml, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (new_id, name, author, yaml, now, now),
        )
        self._record(new_id, yaml, author)
        self._index(new_id, yaml)
        return new_id

    def save(self, id: str, yaml: str, author: str = "") -> None:
        # Name is re-derived from the YAML; author (the creator) is left as-is.
        # `author` is who saved — recorded on the version, not the dashboard.
        name = _name_of(yaml)
        self._baseline(id)
        self._conn.execute(
            "UPDATE dashboards SET name = %s, yaml = %s, updated_at = %s WHERE id = %s",
            (name, yaml, _now(), id),
        )
        self._record(id, yaml, author)
        self._index(id, yaml)

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM dashboards WHERE id = %s", (id,))
        self._conn.execute("DELETE FROM dashboard_versions WHERE dashboard_id = %s", (id,))
        self._conn.execute("DELETE FROM dashboard_uses WHERE dashboard_id = %s", (id,))


def make_store(dsn: str | None):
    """Postgres when a DSN is given (portal runtime); otherwise a local sqlite
    file so the portal can be tried without standing up a database."""
    if dsn:
        return PostgresStore(dsn)
    return SqliteStore("portal.db")


# --- gallery page -----------------------------------------------------------
# Editor chrome, not chart output, so f-strings are fine here (the no-f-string
# rule is for chart HTML). User input — dashboard names — is escape()'d; ids are
# UUIDs and timestamps are machine-generated, so they're safe. The styles and
# the dialog openers live in static/ (gallery.css, nav.css, gallery.js).

# Inline-SVG action icons (stroke=currentColor so they follow the button colour).
_ICONS = {
    "pencil": '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
             '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    # An arrow out of a box: a user who signs in through another system.
    "external": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    # A key: a user who can sign in — they have a password.
    "key": '<circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 21 2M16 7l3 3M19 4l2 2"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
              '<path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/>',
    "back": '<path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "code": '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
    "compare": '<rect x="3" y="4" width="7" height="16" rx="1.5"/><rect x="14" y="4" width="7" height="16" rx="1.5"/>'
               '<path d="M6 9h1M6 13h1M17 11h1M17 15h1"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    # An eye, crossed out: a service column — in the dataset, hidden from dashboards.
    "service": '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/>'
               '<circle cx="12" cy="12" r="2.8"/><path d="M4 4l16 16"/>',
    "cross": '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
    # Every row: a table with nothing struck out — a role's full access.
    "full": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 10v10"/>',
    # A person with a shield: a role — who may see what.
    # A padlock: built in, so it can't be changed.
    "lock": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    # People: a role — the many who hold it (the view-as switch's roles).
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
             '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    # A person: a user.
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7.5 8-7.5s8 3.1 8 7.5"/>',
    "role": '<circle cx="9" cy="7" r="3.5"/><path d="M2.5 20c0-3.8 2.9-6.5 6.5-6.5 1.4 0 2.7.4 3.7 1.1"/>'
            '<path d="M18 12.5l3.5 1.4v2.8c0 2.1-1.5 3.7-3.5 4.3-2-.6-3.5-2.2-3.5-4.3v-2.8z"/>',
    "save": '<path d="M5 3h11l4 4v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M8 3v5h7V3"/><rect x="7" y="13" width="10" height="7" rx="1"/>',
}


_THEME_ICONS = {
    "auto": '<path d="M5 20 12 4l7 16M7.5 14h9"/>',
    "light": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
             'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "dark": '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>',
}


def theme_switch() -> str:
    """Auto / Light / Dark — in the topbar of every page, the editor's and the
    gallery's alike (styled in nav.css, which both link). One choice for all:
    kept in the browser (`ffTheme`) and applied as `data-ff-theme` on <html>."""
    labels = {"auto": "Auto (follow OS)", "light": "Light", "dark": "Dark"}
    buttons = "".join(
        f'<button type="button" data-mode="{mode}" title="Theme: {labels[mode]}"'
        f' aria-label="Theme: {labels[mode]}">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{_THEME_ICONS[mode]}</svg></button>'
        for mode in ("auto", "light", "dark")
    )
    return f'<div class="ff-theme" id="theme-switch" role="group" aria-label="Theme">{buttons}</div>'


def _icon(name: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>'
    )


def _sub(description: str) -> str:
    """A description as the small line under a name, cut with an ellipsis and
    whole on hover."""
    if not description:
        return ""
    return f'<span class="sub" title="{escape(description, quote=True)}">{escape(description)}</span>'


def name_cell(href: str, name: str, description: str = "") -> str:
    """A list row's name, linking to it, with its description as a small line
    under it — cut with an ellipsis, whole on hover — as a selected item's title
    shows it in the topbar."""
    return f'<a class="name" href="{href}">{escape(name)}</a>{_sub(description)}'


def _stamp(iso: str) -> str:
    """A stored UTC time as `<time>`: `2026-10-16 14:52:07 UTC` as written, which
    gallery.js turns into the viewer's local time in the same shape."""
    text = iso.replace("T", " ")[:19]
    return f'<time datetime="{escape(iso)}">{escape(text)} UTC</time>' if text else ""


def _author_cell(author: str, authors: bool) -> str:
    """Who made it — a column only where there are people to name: with no
    login (local mode) every row would read "—"."""
    if not authors:
        return ""
    return f"<td>{escape(author) if author else '—'}</td>"


def _ref_links(links: list[tuple[str, str]], missing: set[str] = frozenset(),
               builtin: set[str] = frozenset()) -> str:
    """What a list row is tied to — the datasets a dashboard reads, the
    dashboards a dataset feeds — as small links. A name in `missing` (a dataset
    a dashboard names that isn't uploaded) is shown, unlinked, so it stands out;
    one in `builtin` (the admin role) in the built-ins' colour."""
    if not links:
        return '<span class="muted">—</span>'
    chips = "".join(
        f'<span class="ref is-missing" title="Not uploaded">{escape(label)}</span>' if label in missing
        else f'<a class="ref{" is-builtin" if label in builtin else ""}" href="{href}"'
             f' title="{escape(label, quote=True)}">{escape(label)}</a>'
        for href, label in links
    )
    return f'<span class="refs">{chips}</span>'


def _dashboard_datasets(row: DashboardRow, known: set[str] | None,
                        uses: dict[str, list[str]] | None) -> str:
    """The uploaded datasets a dashboard reads (inline ones are part of it) —
    from the store's index when given, else worked out from its YAML."""
    names = sorted(uses.get(row.id, []) if uses is not None else Dashboard.dataset_names(row.yaml))
    missing = set(names) - known if known is not None else set()
    return _ref_links([(f"/datasets/{quote(n)}", n) for n in names], missing)


def list_table(heads: list[str], rows: list[str], empty: str, add: str) -> str:
    """A list page's table: `heads` (the column names), one `<tr>` per row,
    and — at the head of the row actions' column — `add`, the page's + (so it
    sits over the actions it goes with). With no rows the head stays, the +
    in it, over `empty`."""
    head = "".join(f"<th>{h}</th>" for h in heads) + f'<th class="actions">{add}</th>'
    body = "".join(rows) or f'<tr><td class="empty-row" colspan="{len(heads) + 1}">{empty}</td></tr>'
    return f'<table class="dash-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _add_button(title: str, onclick: str) -> str:
    return f'<button class="act" type="button" title="{title}" onclick="{onclick}">{_icon("plus")}</button>'


def _row(row: DashboardRow, authors: bool = True, known: set[str] | None = None,
         uses: dict[str, list[str]] | None = None) -> str:
    updated = _stamp(row.updated_at)
    return (
        "<tr>"
        f'<td>{name_cell(f"/d/{row.id}", row.name, row.description)}</td>'
        f"{_author_cell(row.author, authors)}"
        f'<td class="muted">{updated}</td>'
        f"<td>{_dashboard_datasets(row, known, uses)}</td>"
        # The name opens the dashboard, so the row needs no edit button.
        '<td class="actions">'
        f'<button class="act" type="button" title="Clone" data-id="{row.id}"'
        f' data-name="{escape(row.name, quote=True)}" onclick="openClone(this)">{_icon("copy")}</button>'
        f'<form method="post" action="/d/{row.id}/delete"'
        " onsubmit=\"return confirm('Remove this dashboard?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        "</td></tr>"
    )


def _dialog(
    dialog_id: str, form_id: str, action: str, heading: str, ok_label: str,
    description: bool = False, placeholder: str = "Dashboard name",
) -> str:
    # `action` is empty for the clone dialog — set by JS from the clicked row.
    # Only a new dashboard asks for a description: a clone keeps its source's.
    about = (
        '\n    <textarea name="description" placeholder="Description (optional)"></textarea>'
        if description else ""
    )
    return f"""
<dialog id="{dialog_id}">
  <form id="{form_id}" method="post" action="{action}">
    <h3>{heading}</h3>
    <input name="name" id="{form_id}-name" placeholder="{placeholder}"
           autocomplete="off" required>{about}
    <div class="dialog-actions">
      <button type="button" class="cancel"
              onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">{ok_label}</button>
    </div>
  </form>
</dialog>"""



_CHEVRON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>'
)


def back_button(href: str, title: str) -> str:
    """The shared back button (dataset detail + dashboard editor use the same)."""
    t = escape(title, quote=True)
    return f'<a class="ff-back" href="{href}" title="{t}" aria-label="{t}">{_icon("back")}</a>'


def nav_switch(active: str, users: bool = False) -> str:
    """The list pages' left-nav control: a segmented Dashboards | Datasets |
    Access switch (`active` marks the current list) — and Users, with `users`
    (portal mode, where there are people). Not shown on selected-item pages."""
    def seg(href, label, key):
        cls = " active" if key == active else ""
        return f'<a class="ff-switch-seg{cls}" href="{href}">{label}</a>'
    return (
        '<nav class="ff-switch">'
        + seg("/", "Dashboards", "dashboards")
        + seg("/datasets", "Datasets", "datasets")
        + seg("/access", "Access", "access")
        + (seg("/users", "Users", "users") if users else "")
        + "</nav>"
    )


def path_dropdown(paths: list[str] | None, active_path: str | None) -> str:
    """A labelled dropdown showing the active path; picking one switches path.
    Lives on the **right** of the topbar. Empty with no paths (portal / plain)."""
    if not paths:
        return ""
    items = "".join(
        f'<a href="/path/{quote(p)}"'
        f'{" class=\"active\"" if p == active_path else ""}>{escape(p)}</a>'
        for p in paths
    )
    label = escape(active_path or "path")
    return (
        '<details class="ff-pathdd"><summary title="Switch path">'
        f"{label}{_CHEVRON}</summary>"
        f'<div class="ff-pathdd-menu">{items}</div></details>'
    )


def _tip(text: str) -> str:
    """` title="…"` for a role's description — its tooltip — or nothing."""
    return f' title="{escape(text, quote=True)}"' if text else ""


# The view-as menu's search: narrows the list showing (editor.js). One pick —
# not a multi-select.
_ROLE_SEARCH = (
    '<input class="ff-role-search" type="search" placeholder="Search" aria-label="Search"'
    ' autocomplete="off" spellcheck="false">'
    '<span class="ff-role-none" hidden>No matches</span>'
)


def role_picker(roles: list[Role], users: list[User] = ()) -> str:
    """The editor's view-as dropdown (view mode): every role, by name (what
    datasets' `permissions:` name), and — behind a roles | users switch at the
    menu's head, one list at a time — every user. Picking one
    renders the dashboard as that role, or that user (through their roles),
    sees it (`Dashboard.as_role`); picking it again goes back to all of it. The
    summary shows the name, or just the icon. Empty with no roles — there's
    nothing to view as. editor.js does the rest."""
    if not roles:
        return ""
    items = "".join(
        f'<button type="button"{_BUILTIN_ROW if r.builtin else ""} data-role="{escape(r.name, quote=True)}"'
        f'{_tip(r.description)}>{escape(r.name)}</button>'
        for r in roles
    )
    if users:
        people = "".join(
            f'<button type="button"{_BUILTIN_ROW if u.builtin else ""} data-user="{escape(u.name, quote=True)}"'
            f'{_tip(u.about)}>{escape(u.name)}</button>'
            for u in users
        )
        kinds = "".join(
            f'<button type="button" data-kind="{kind}" title="{label}" aria-label="{label}"'
            f'{" class=\"active\"" if kind == "role" else ""}>{_icon(icon)}</button>'
            for kind, icon, label in (("role", "users", "Roles"), ("user", "user", "Users"))
        )
        items = (
            f'<div class="ff-theme ff-role-kinds" role="group" aria-label="View as">{kinds}</div>'
            + _ROLE_SEARCH
            + f'<div class="ff-role-list" data-kind="role">{items}</div>'
            f'<div class="ff-role-list" data-kind="user" hidden>{people}</div>'
        )
    else:
        items = _ROLE_SEARCH + f'<div class="ff-role-list" data-kind="role">{items}</div>'
    return (
        '<details class="ff-pathdd ff-role" id="ff-role" data-as="">'
        '<summary title="View as" aria-label="View as">'
        # The icon says what's picked: people for a role, a person for a user —
        # and the view-as glyph with nothing picked (editor.js sets `data-as`).
        f'<span class="ff-role-icon" data-as="">{_icon("role")}</span>'
        f'<span class="ff-role-icon" data-as="role">{_icon("users")}</span>'
        f'<span class="ff-role-icon" data-as="user">{_icon("user")}</span>'
        f'<span class="ff-role-name" id="ff-role-name"></span>{_CHEVRON}</summary>'
        f'<div class="ff-pathdd-menu">{items}</div></details>'
    )


def _shell(
    title: str,
    user_menu: str,
    active: str,
    body: str,
    extra: str = "",
    topbar_left: str = "",
    topbar_right: str = "",
    paths: list[str] | None = None,
    active_path: str | None = None,
    detail: bool = False,
    head: str = "",
    users: bool = False,
) -> str:
    """The gallery page frame. Overview pages (dashboards/datasets lists) lead
    with a Dashboards | Datasets switch on the left; in local paths mode the path
    dropdown sits on the **right**. Detail pages (a selected dataset) lead with a
    back button (+ switch, local) in `topbar_left`. Then optional `topbar_right`,
    then `body`. No second nav bar."""
    nav = "" if detail else nav_switch(active, users)
    right = path_dropdown(paths, active_path) + topbar_right + theme_switch() + user_menu
    right_html = f'<span class="topbar-right">{right}</span>' if right else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
{assets.favicons()}
{assets.stylesheets("gallery.css", "nav.css", "profile.css")}
{assets.script("gallery.js")}
{head}
</head>
<body>
<header class="topbar">{nav}{topbar_left}{right_html}</header>
<main class="gallery">{body}</main>
{extra}
</body>
</html>"""


def render_gallery(
    rows: list[DashboardRow], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None, authors: bool = True,
    datasets: set[str] | None = None, uses: dict[str, list[str]] | None = None,
    users: bool = False,
) -> str:
    """`datasets`: the names uploaded, so a dashboard naming one that isn't can
    say so; None links every name. `uses`: dashboard id -> the datasets it
    reads, from the store's index; None works it out from each YAML."""
    body_table = list_table(
        ["Name", *(["Author"] if authors else []), "Last updated", "Datasets"],
        [_row(r, authors, datasets, uses) for r in rows],
        "No dashboards yet", _add_button("New dashboard", "openAdd()"),
    )
    add_dialog = _dialog("add-dialog", "add-form", "/new", "New dashboard", "Create", description=True)
    clone_dialog = _dialog("clone-dialog", "clone-form", "", "Clone dashboard", "Clone")
    clone_dialog = clone_dialog.replace('id="clone-form-name"', 'id="clone-name"')
    return _shell(
        title, user_menu, "dashboards", body_table,
        extra=add_dialog + clone_dialog,
        paths=paths, active_path=active_path, users=users,
    )


# --- datasets gallery -------------------------------------------------------



def _type_icon(dtype: str) -> str:
    """A small badge glyph for a Parquet column type — the same classification
    the filter fields show (`params.column_type`)."""
    kind = column_type(dtype)
    _, name = COLUMN_TYPES[kind]
    if kind == "other":
        name = dtype
    return f'<span class="type-icon" title="{escape(name)}">{type_icon_svg(kind)}</span>'


def _upload_dialog(action: str, heading: str, ok_label: str, dialog_id: str, name_field: bool) -> str:
    # Name and description belong to a new dataset; a new version of one only
    # brings a file (its description is edited from the dataset's title).
    name_rows = (
        '<label>Name</label><input name="name" autocomplete="off" required>'
        '<label>Description</label><textarea name="description"></textarea>'
        if name_field else ""
    )
    return f"""
<dialog class="wide" id="{dialog_id}">
  <form method="post" action="{action}" enctype="multipart/form-data">
    <h3>{heading}</h3>
    {name_rows}
    <label>CSV file</label>
    <input type="file" name="file" accept=".csv,text/csv" required>
    <label>Delimiter</label>
    <select name="delimiter">
      <option value=",">Comma ( , )</option>
      <option value=";">Semicolon ( ; )</option>
      <option value="\t">Tab</option>
      <option value="|">Pipe ( | )</option>
    </select>
    <div class="dialog-actions">
      <button type="button" class="cancel" onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">{ok_label}</button>
    </div>
  </form>
</dialog>"""


def _version_cell(ds: Dataset) -> str:
    """`2.1`, or `2.1 → 2.2` while the next one builds."""
    if ds.building:
        return f"{ds.version} → {ds.building}" if ds.version else f"→ {ds.building}"
    return ds.version


def _dataset_row(ds: Dataset, authors: bool = True,
                 used_by: dict[str, list[tuple[str, str]]] | None = None) -> str:
    href = f"/datasets/{quote(ds.name)}"
    updated = _stamp(ds.updated_at)
    dashboards = "" if used_by is None else (
        "<td>" + _ref_links([(f"/d/{did}", name) for did, name in used_by.get(ds.name, [])]) + "</td>"
    )
    return (
        "<tr>"
        f'<td>{name_cell(href, ds.name, ds.description)}</td>'
        f'<td class="muted mono">{_version_cell(ds)}</td>'
        f'<td class="muted">{len(ds.columns) + len(ds.service)}</td>'
        f'<td class="muted">{ds.rows}</td>'
        f'<td class="muted">{updated}</td>'
        f"{_author_cell(ds.author, authors)}"
        f"{dashboards}"
        # The name opens the dataset — where it's renamed and described — so
        # the row needs no view or edit button of its own.
        '<td class="actions">'
        f'<form method="post" action="/datasets/{quote(ds.name)}/delete"'
        " onsubmit=\"return confirm('Remove this dataset?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        "</td></tr>"
    )


def render_datasets(
    datasets: list[Dataset], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None, authors: bool = True,
    used_by: dict[str, list[tuple[str, str]]] | None = None,
    users: bool = False,
) -> str:
    """`used_by`: dataset name -> the (id, name) of each dashboard reading it;
    None (no dashboards to look at, plain local mode) leaves the column out."""
    table = list_table(
        ["Name", "Version", "Columns", "Rows", "Updated", *(["Author"] if authors else []),
         *(["Dashboards"] if used_by is not None else [])],
        [_dataset_row(d, authors, used_by) for d in datasets],
        "No datasets yet", _add_button("New dataset", "openUpload()"),
    )
    extra = (
        _upload_dialog("/datasets/new", "New dataset", "Upload", "upload-dialog", True)
    )
    return _shell(
        title, user_menu, "datasets", table,
        extra=extra,
        paths=paths, active_path=active_path, users=users,
    )


# --- a dataset's permissions -------------------------------------------------------
# The dataset page's fifth mode: each role, and what it may see of the dataset —
# nothing (the default), all of it, or the rows a set of filters lets through.
# The filter rows are the chart builder's (`params.filter_fields`), so a role's
# filters read and pick values as a chart's do; dataset-columns.js adds/removes
# rows and re-fetches one on a column or op change, as editor.js does.

# A listed role may read the dataset — all of it, or what its filters let
# through; a role that may not isn't listed (the trash takes a role off, the +
# menu puts one back). Its filters read as a chart's filter panel shows them —
# a compact row each (type, column, op, values; ✎ edits, ✕ removes) — over the
# chart builder's own fields (`params.filter_fields`, driven by the shared
# filter-builder.js), where ✓ adds the filter built there. With no filters a
# role has all of it: Full, which the fields' column picker offers first.


def access_filter_row(f: dict, types: dict[str, str]) -> str:
    """One of a role's filters, as a chart's filter panel lists it. Its parts
    ride along as hidden inputs, which the save reads (`parse_filters`)."""
    from fireflyer.params import FILTER_OPS, OP_WORDS, type_glyph

    filt = filters_mod.normalize([f])[0]
    column = escape(filt.column, quote=True)
    vals = escape(filt.values_text, quote=True)
    hidden = (
        f'<input type="hidden" name="filter_column" value="{column}">'
        f'<input type="hidden" name="filter_op" value="{filt.op}">'
        + "".join(f'<input type="hidden" name="filter_value" value="{escape(str(v), quote=True)}">'
                  for v in filt.values)
    )
    return (
        '<tr class="ds-filter">'
        f'<td class="col">{type_glyph(types.get(filt.column))}'
        f'<span class="col-name" title="{column}">{escape(filt.column)}</span>{hidden}</td>'
        f'<td class="op" title="{OP_WORDS[filt.op]}">{dict(FILTER_OPS)[filt.op]}</td>'
        f'<td class="vals" title="{vals}">{vals}</td>'
        '<td class="rm">'
        f'<button type="button" class="ds-filter-edit" title="Edit filter" aria-label="Edit filter on {column}">{_icon("pencil")}</button>'
        f'<button type="button" class="ds-filter-remove" title="Remove filter" aria-label="Remove filter on {column}">{_icon("cross")}</button>'
        "</td></tr>"
    )


def access_draft(fields: str) -> str:
    """The row a filter is built in — the chart builder's fields, then ✓ (add,
    or replace the one being edited) and ✕ (close). Shown only once asked
    for: the role's + or a filter's ✎."""
    return (
        f'<div class="ff-filter-row ds-draft-row">{fields}<span class="ds-draft-acts">'
        f'<button type="button" class="ds-filter-ok" title="Apply filter" aria-label="Apply filter">{_icon("check")}</button>'
        f'<button type="button" class="ds-filter-cancel" title="Cancel" aria-label="Cancel">{_icon("cross")}</button>'
        "</span></div>"
    )


def _rows_cell(count) -> str:
    """How many rows the role sees: a number, or a lock when one of its filters
    names a column the data lacks (it would see nothing)."""
    text = "—" if count is None else f"{count:,}"
    tip = "A filter names a column the data lacks" if count is None else "Rows this role sees"
    return f'<td class="ds-access-rows" title="{tip}">{text}</td>'


def _access_row(name: str, filters: list, chip, draft: str, count=None, description: str = "") -> str:
    """One listed role: its name; its filters — or Full, with none — over the
    draft row, closed; and the lists' own icon buttons: + (a filter, for a
    filtered role — a full one's Full opens the draft) and the trash (no
    access)."""
    level = "filter" if filters else "full"
    label = escape(name, quote=True)
    return (
        f'<tr class="ds-access-row is-{level}" data-role="{label}">'
        f'<td class="ds-access-role"><input type="hidden" name="role" value="{label}">'
        f'<span class="name"{_tip(description)}>{escape(name)}</span></td>'
        '<td class="ds-access-filters">'
        # Full is also how a full role gets its first filter: it opens the
        # draft at the columns, so the toolbar needs no + for it.
        f'<button type="button" class="ds-full-chip" title="All rows — pick a column to narrow"'
        f' aria-label="Full access of {label}: add a filter">{_icon("full")}<span>Full</span>{_CHEVRON}</button>'
        '<table class="ds-filter-table"><colgroup><col class="c-col"><col class="c-op"><col><col class="c-act"></colgroup>'
        f'<tbody>{"".join(chip(f) for f in filters)}</tbody></table>'
        f'<div class="ds-draft">{draft}</div></td>'
        f"{_rows_cell(count)}"
        '<td class="ds-role-cell"><div class="ds-role-tools">'
        f'<button type="button" class="act ds-tool-add" title="Add a filter" aria-label="Add a filter for {label}">{_icon("plus")}</button>'
        f'<button type="button" class="act act-danger ds-tool-del" title="Remove access" aria-label="Remove access of {label}">{_icon("trash")}</button>'
        "</div></td></tr>"
    )


def render_access_form(roles: list[Role], access: dict[str, list[dict]], chip, draft: str,
                       counts: dict[str, int | None] | None = None) -> str:
    """`access`: role name -> its filters (`[]`: full), as the dataset's
    `permissions:` hold them (`DatasetStore.permissions`). `chip(filter)` is
    `access_filter_row` for this dataset's column types; `draft` its blank
    `access_draft`. Only the roles with access are listed; the + menu offers
    the rest, each as a ready row (`ds-access-row-tpl`, filled in by
    dataset-columns.js). A role is named, as in the YAML. `counts`: role name
    -> the rows it sees (`DatasetStore.rows_seen`)."""
    counts = counts or {}
    rows = "".join(
        _access_row(r.name, access[r.name], chip, draft, counts.get(r.name), r.description)
        for r in roles if r.name in access
    )
    offers = "".join(
        f'<button type="button" data-role="{escape(r.name, quote=True)}"{_tip(r.description)}'
        f'{" hidden" if r.name in access else ""}>{escape(r.name)}</button>'
        for r in roles
    )
    # Always there, at the head of the actions column: the roles without
    # access, then a field that makes a new one — on this page, given access
    # at once (dataset-columns.js posts it to /access/new).
    add = (
        '<details class="ds-access-add">'
        f'<summary class="act" title="Give a role access" aria-label="Give a role access">{_icon("plus")}</summary>'
        f'<div class="ff-pathdd-menu">{offers}'
        f'<label class="ds-access-new">{_icon("plus")}'
        '<input type="text" placeholder="New role" aria-label="New role" autocomplete="off" spellcheck="false">'
        "</label></div></details>"
    )
    return (
        '<form class="ds-access" id="ds-access" autocomplete="off">'
        '<table class="dash-table ds-access-table"><thead><tr><th>Role</th><th>Filters</th>'
        f'<th class="ds-access-rows">Rows</th><th class="ds-role-cell">{add}</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<template class="ds-access-row-tpl">{_access_row("", [], chip, draft)}</template>'
        f'<template class="ds-draft-tpl">{draft}</template>'
        # The column picker's first entry, ahead of the columns.
        '<template class="ds-full-tpl"><button type="button" class="ff-colpick-item ds-full">'
        f'{_icon("full")}<span>Full</span></button></template></form>'
    )


class _Items:
    """A slice of a submitted form, for `params.parse_filters` (which reads
    `multi_items()`)."""

    def __init__(self, items):
        self._items = items

    def multi_items(self):
        return self._items


def parse_access_form(form) -> dict[str, list[dict]]:
    """What `render_access_form` submits, back as role name -> filters (`[]`:
    full) — the listed roles; one not listed has no access. Each role's
    filters follow its `role` input, in document order. (The draft row isn't
    sent: dataset-columns.js leaves it out.)"""
    from fireflyer.params import parse_filters

    groups: list[list] = []
    for key, value in form.multi_items():
        if key == "role":
            groups.append([(key, value)])
        elif groups:
            groups[-1].append((key, value))
    return {items[0][1]: parse_filters(_Items(items)) for items in groups}


# --- access gallery -----------------------------------------------------------


def _role_row(role: Role, datasets: list[str] | None = None) -> str:
    """`datasets`: the ones whose permissions name the role, linked as the
    dashboards list links a dashboard's; None leaves the column out."""
    # The admin role reads every dataset: "All", rather than each one.
    used = "" if datasets is None else (
        '<td class="muted">All</td>' if role.builtin
        else "<td>" + _ref_links([(f"/datasets/{quote(n)}", n) for n in datasets]) + "</td>"
    )
    return (
        f'<tr{_BUILTIN_ROW if role.builtin else ""}>'
        f'<td><span class="name">{escape(role.name)}</span>{_sub(role.description)}</td>'
        f'<td class="muted">{_stamp(role.updated_at)}</td>'
        f"{used}"
        f'<td class="actions">{_locked() if role.builtin else _role_actions(role)}</td></tr>'
    )


# A built-in's row: in the accent colour, so it reads as the service one.
_BUILTIN_ROW = ' class="is-builtin"'


def _locked() -> str:
    """In place of a built-in's edit and remove: a padlock, saying why."""
    return f'<span class="act act-locked" title="Built in — can\'t be changed">{_icon("lock")}</span>'


def _role_actions(role: Role) -> str:
    return (
        f'<button class="act" type="button" title="Edit" data-id="{role.id}"'
        f' data-name="{escape(role.name, quote=True)}" data-description="{escape(role.description, quote=True)}"'
        f' onclick="openRename(this)">{_icon("pencil")}</button>'
        f'<form method="post" action="/access/{role.id}/delete"'
        " onsubmit=\"return confirm('Remove this role?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
    )


def render_access(
    roles: list[Role], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None,
    datasets: dict[str, list[str]] | None = None,
    users: bool = False,
) -> str:
    """The Access list: every role, by name, with when it last changed and —
    `datasets`, role name -> the datasets naming it — where it's used."""
    table = list_table(
        ["Name", "Updated", *(["Datasets"] if datasets is not None else [])],
        [_role_row(r, None if datasets is None else datasets.get(r.name, [])) for r in roles],
        "No roles yet", _add_button("New role", "openAdd()"),
    )
    add_dialog = _dialog("add-dialog", "add-form", "/access/new", "New role", "Create",
                         description=True, placeholder="Role name")
    rename_dialog = _dialog("rename-dialog", "rename-form", "", "Edit role", "Save",
                            description=True, placeholder="Role name")
    return _shell(
        title, user_menu, "access", table,
        extra=add_dialog + rename_dialog,
        paths=paths, active_path=active_path, users=users,
    )


# --- users gallery ------------------------------------------------------------
# Portal mode's people, each holding some roles — at most one per dataset
# (`role_clashes`), so what a user sees of a dataset is one role's filters.


def _user_row(user: User) -> str:
    roles = _ref_links([("/access", r) for r in user.roles], builtin={ADMIN})
    return (
        f'<tr{_BUILTIN_ROW if user.builtin else ""}>'
        f'<td><span class="name">{escape(user.name)}</span>{_key(user)}{_sub(user.about)}</td>'
        f"<td>{roles}</td>"
        f'<td class="muted">{_stamp(user.updated_at)}</td>'
        f'<td class="actions">{_locked() if user.builtin else _user_actions(user)}</td></tr>'
    )


def _key(user: User) -> str:
    """Beside the name, how a user signs in: through another system (its
    name in the tooltip), with a password (a key), or not at all (nothing)."""
    if user.external:
        tip = escape(f"Signs in through {user.source}", quote=True)
        return f'<span class="ff-key ff-source" title="{tip}">{_icon("external")}</span>'
    return f'<span class="ff-key" title="Can sign in">{_icon("key")}</span>' if user.has_password else ""


def _user_actions(user: User) -> str:
    return (
        f'<button class="act" type="button" title="Edit" data-id="{user.id}"'
        f'{" data-has-password" if user.has_password else ""}'
        + (f' data-source="{escape(user.source, quote=True)}"' if user.external else "")
        + "".join(f' data-{k}="{escape(getattr(user, k.replace("-", "_")), quote=True)}"'
                  for k in ("name", "first-name", "last-name", "email"))
        + f' data-roles="{escape(json.dumps(user.roles), quote=True)}"'
        f' onclick="openUser(this)">{_icon("pencil")}</button>'
        f'<form method="post" action="/users/{user.id}/delete"'
        " onsubmit=\"return confirm('Remove this user?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
    )


@dataclass
class Option:
    """One of a `multi_select`'s choices. `about`: a muted line beside the
    label (and its tooltip); `data`: extra `data-*` on its checkbox, for a
    page's own script."""
    value: str
    label: str
    about: str = ""
    data: dict[str, str] = field(default_factory=dict)
    checked: bool = False


def multi_select(name: str, options: list[Option], placeholder: str) -> str:
    """A multi-select dropdown with search: the picked options as chips, a
    search box after them, and a checkbox per option in the list under it —
    the boxes submit as `name` (repeated), so the form reads like a checkbox
    group. Driven by static/multi-select.js + multi-select.css, which the page
    links."""
    items = "".join(
        f'<label class="ff-multi-item"{_tip(o.about)}>'
        f'<input type="checkbox" name="{escape(name, quote=True)}" value="{escape(o.value, quote=True)}"'
        f' data-label="{escape(o.label, quote=True)}"'
        + "".join(f' data-{k}="{escape(v, quote=True)}"' for k, v in o.data.items())
        + f'{" checked" if o.checked else ""}><span>{escape(o.label)}</span>'
        + (f'<span class="ff-multi-about">{escape(o.about)}</span>' if o.about else "")
        + "</label>"
        for o in options
    )
    ph = escape(placeholder, quote=True)
    return (
        f'<div class="ff-multi" data-placeholder="{ph}">'
        '<div class="ff-multi-box">'
        # `form` names none, so the search's text is never submitted.
        f'<input class="ff-multi-search" type="text" form="ff-none" placeholder="{ph}" aria-label="{ph}"'
        ' autocomplete="off" spellcheck="false" role="combobox" aria-expanded="false">'
        "</div>"
        f'<div class="ff-multi-list" role="listbox" aria-multiselectable="true" hidden>{items}'
        f'<span class="ff-multi-none"{" hidden" if options else ""}>{"No matches" if options else "None yet"}</span>'
        "</div></div>"
    )


# The user dialog's password: set by the admin, never shown back — the field is
# always empty (`new-password`, so a browser offers to make one rather than
# fill in the admin's own). On an edit, empty keeps the current one and the
# trash removes it (gallery.js). The policy is checked again on the server.
_PASSWORD_FIELD = (
    '<div class="ff-password">'
    '<input name="password" id="user-form-password" type="password" autocomplete="new-password"'
    f' minlength="{auth.MIN_PASSWORD}" maxlength="{auth.MAX_PASSWORD}" spellcheck="false"'
    ' placeholder="Password (optional)" aria-label="Password">'
    f'<button type="button" class="ff-password-show" title="Show password" aria-label="Show password">{_icon("eye")}</button>'
    f'<button type="button" class="ff-password-clear" title="Remove password" aria-label="Remove password" hidden>{_icon("trash")}</button>'
    '<input type="hidden" name="clear_password" value="">'
    "</div>"
    # In its place for a user from another system, which owns the credentials.
    f'<div class="ff-external" hidden>{_icon("external")}<span></span></div>'
)


def _user_dialog(roles: list[Role], reads: dict[str, list[str]]) -> str:
    """One dialog for a new user and an edited one (gallery.js `openUser` fills
    it from the row). The roles are a `multi_select`, each option carrying the
    datasets it may read (`reads`: role name -> them), so picking one turns off
    the roles that would read a dataset twice — the server checks again
    (`role_clashes`)."""
    boxes = multi_select("role", [
        Option(r.id, r.name, r.description,
               {"name": r.name, "reads": json.dumps(reads.get(r.name, [])),
                **({"builtin": "1"} if r.builtin else {})})
        for r in roles
    ], "Roles")
    return f"""
<dialog id="user-dialog">
  <form id="user-form" method="post" action="/users/new">
    <h3>User</h3>
    <input name="name" id="user-form-name" placeholder="User name" autocomplete="off" required>
    <input name="first_name" placeholder="First name (optional)" autocomplete="off">
    <input name="last_name" placeholder="Surname (optional)" autocomplete="off">
    <input name="email" type="email" placeholder="Email (optional)" autocomplete="off">
    {_PASSWORD_FIELD}
    {boxes}
    <div class="dialog-actions">
      <button type="button" class="cancel"
              onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">Save</button>
    </div>
  </form>
</dialog>"""


def render_users(
    users: list[User], roles: list[Role], title: str = "Fireflyer Portal", user_menu: str = "",
    paths: list[str] | None = None, active_path: str | None = None,
    reads: dict[str, list[str]] | None = None,
) -> str:
    """The Users list: every user, by name, with the roles they hold.
    `reads`: role name -> the datasets it may read, for the dialog."""
    table = list_table(
        ["Name", "Roles", "Updated"], [_user_row(u) for u in users],
        "No users yet", _add_button("New user", "openUser()"),
    )
    return _shell(
        title, user_menu, "users", table,
        head=assets.stylesheets("multi-select.css") + "\n" + assets.script("multi-select.js"),
        extra=_user_dialog(roles, reads or {}),
        paths=paths, active_path=active_path, users=True,
    )


def render_dataset_detail(
    ds: Dataset,
    title: str = "Fireflyer Portal",
    user_menu: str = "",
    used_by: list[tuple[str, str]] | None = None,
    paths: list[str] | None = None,
    active_path: str | None = None,
    mapping: str = "",
    versions: list[Version] | None = None,
    examples: dict[str, list[str]] | None = None,
    authors: bool = True,
    used_in: dict[str, list[tuple[str, str]]] | None = None,
) -> str:
    # The trash button: when the dataset is in use it shows a count badge and
    # opens the list of dashboards (open in a new tab) instead of deleting; when
    # nothing uses it, it deletes.
    used_by = used_by or []
    if used_by:
        trash = (
            f'<button class="act act-danger" type="button" title="Used by {len(used_by)} dashboard(s)"'
            ' onclick="document.getElementById(\'usage-dialog\').showModal()">'
            f'{_icon("trash")}<span class="badge">{len(used_by)}</span></button>'
        )
        links = "".join(
            f'<li><a href="/d/{did}" target="_blank" rel="noopener">{escape(name)}</a></li>'
            for did, name in used_by
        )
        usage_dialog = f"""
<dialog id="usage-dialog">
  <form method="dialog" style="padding:22px">
    <h3>Used by {len(used_by)} dashboard(s)</h3>
    <ul class="link-list">{links}</ul>
    <div class="dialog-actions"><button class="ok">Close</button></div>
  </form>
</dialog>"""
    else:
        trash = (
            f'<form method="post" action="/datasets/{quote(ds.name)}/delete" style="display:inline"'
            " onsubmit=\"return confirm('Remove this dataset?')\">"
            f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        )
        usage_dialog = ""

    # Header (back + name/description + actions) lives in the topbar, as a
    # dashboard's does in the editor: the name with the description's first
    # line under it, together one button opening a card with a field for each
    # (gallery.js `dsTitle`), so the toolbar needs neither an edit button nor
    # an ⓘ.
    desc = escape(ds.description, quote=True)
    tname = (
        '<span class="thead" id="ds-title-head" role="button" tabindex="0" aria-haspopup="dialog"'
        ' aria-label="Rename or describe the dataset">'
        f'<span class="tname" id="ds-title-name" data-name="{escape(ds.name, quote=True)}"'
        f' data-description="{desc}" data-action="/datasets/{quote(ds.name)}/rename">'
        f'{escape(ds.name)}</span>'
        f'<span class="tsub" id="ds-title-sub"{"" if ds.description else " hidden"}>'
        f"{escape(ds.description)}</span>"
        "</span>"
    )
    tabout = (
        '<span class="ff-title-about" id="ds-title-about" role="dialog" aria-label="Name and description" hidden>'
        '<input id="ds-title-name-input" type="text" aria-label="Dataset name" placeholder="Name"'
        ' spellcheck="false" autocomplete="off">'
        '<textarea id="ds-title-about-text" rows="3" placeholder="Add a description"'
        ' aria-label="Dataset description"></textarea>'
        '<span class="ff-title-error" id="ds-title-error" hidden></span></span>'
    )
    versions = versions or []
    # A selected item leads with the back button, the columns' mode switch and
    # the name, then the version — which opens the history. No Dashboards |
    # Datasets switch (that's for the lists); the path dropdown still rides on
    # the right in local paths mode (added by `_shell`).
    topbar_left = (
        '<div class="topbar-lead">'
        + back_button("/datasets", "Back to datasets")
        + _column_modes()
        + f'<div class="topbar-title" id="ds-title">{tname}{tabout}</div>'
        + _history_menu(ds, versions, authors)
        + "</div>"
    )
    topbar_right = (
        '<span class="ff-savebar" role="group" aria-label="Unsaved changes">'
        '<button class="ff-discard" id="ds-discard" type="button" title="Discard unsaved changes"'
        f' aria-label="Discard unsaved changes" hidden>{_icon("cross")}</button>'
        '<button class="ff-save" id="ds-save" type="button" title="Save (⌘/Ctrl+S)" aria-label="Save"'
        f' hidden>{_icon("save")}</button></span>'
        '<span class="detail-actions">'
        '<button class="act" title="Upload a new file (the next version)"'
        f" onclick=\"document.getElementById('replace-dialog').showModal()\">{_icon('upload')}</button>"
        f"{trash}</span>"
    )
    unreadable = _mapping_error(mapping, versions)
    status = (f'<div class="ds-status is-error" id="ds-status">{escape(unreadable)}</div>'
              if unreadable else _build_status(ds))
    body = f"""
  {status}
  <section class="ds-columns" id="ds-columns" data-mode="view">{_columns_view(ds, mapping, versions, examples or {}, used_in or {})}</section>
  <div class="ds-toast" id="ds-toast" role="status" hidden></div>
  <script type="application/json" id="ds-data">{_page_data(ds, mapping, versions, examples or {}, used_in or {})}</script>"""
    replace = _upload_dialog(
        f"/datasets/{quote(ds.name)}/replace", "Upload a new version", "Upload", "replace-dialog", False
    )
    page_head = (
        assets.stylesheets("code.css", "filters.css")
        # htmx for the permissions' filter rows: their date-range picker is htmx.
        + '<script src="/static/vendor/htmx@1.9.12.min.js"></script>\n'
        + assets.script("filter-builder.js")
        + assets.import_map("yaml-look.js", "diff-view.js")
        + assets.module("dataset-columns.js")
    )
    return _shell(
        title, user_menu, "datasets", body,
        extra=replace + usage_dialog,
        topbar_left=topbar_left, topbar_right=topbar_right,
        paths=paths, active_path=active_path, detail=True, head=page_head,
    )


# --- a dataset's columns: view / edit / code / compare -----------------------------
# The page's columns panel switches between four modes, as the dashboard editor
# does: the mapping read (view), edited in a form (edit) or as YAML (code), and
# two versions of it side by side (compare). The view is rendered here; the
# rest is static/dataset-columns.js, from the JSON the page carries.

_COLUMN_MODES = (
    ("view", "eye", "Columns"),
    ("edit", "pencil", "Edit names and types"),
    ("code", "code", "Edit as YAML"),
    ("compare", "compare", "Compare versions"),
    ("access", "role", "Permissions"),
)


def _column_modes() -> str:
    buttons = "".join(
        f'<button type="button" data-mode="{mode}" title="{label}" aria-label="{label}"'
        f'{" class=active" if mode == "view" else ""}>{_icon(icon)}</button>'
        for mode, icon, label in _COLUMN_MODES
    )
    return f'<div class="ds-modes" id="ds-modes" role="group" aria-label="Columns">{buttons}</div>'


def _mapping_rules(ds: Dataset, mapping: str, versions: list[Version]) -> list[column_map.Rule]:
    latest = versions[0] if versions else None
    try:
        return column_map.parse(mapping, latest.sources if latest else [])
    except column_map.MappingError:
        return []


def _mapping_error(mapping: str, versions: list[Version]) -> str:
    """Why the mapping can't be read, if it can't — said, not an empty table."""
    try:
        column_map.parse(mapping, versions[0].sources if versions else [])
    except column_map.MappingError as exc:
        return str(exc)
    return ""


SERVICE_TITLE = "Service column — in the dataset, never in dashboards"


def _service_mark() -> str:
    return (f'<span class="col-service" title="{SERVICE_TITLE}" aria-label="{SERVICE_TITLE}">'
            f'{_icon("service")}</span>')


def _kinds(ds: Dataset) -> dict[str, tuple[str, str]]:
    """Column name -> (icon, kind) from the built data: a column's type is what
    its formula gave (text, a number from to_number(), a date from str2dt())."""
    out = {}
    for c in ds.columns + ds.service:
        kind = column_type(c.dtype)
        out[c.name] = (_type_icon(c.dtype), COLUMN_TYPES[kind][1] if kind in COLUMN_TYPES else c.dtype)
    return out


def _columns_view(ds: Dataset, mapping: str, versions: list[Version], examples: dict[str, list[str]],
                  used_in: dict[str, list[tuple[str, str]]]) -> str:
    kinds = _kinds(ds)
    rows = "".join(
        "<tr>"
        f"<td>{kinds.get(r.name, ('', ''))[0]}<span class=\"col-name\">{escape(r.name)}</span>"
        + (_service_mark() if r.service else "") + "</td>"
        f'<td class="muted mono">{escape(r.formula)}</td>'
        f'<td class="ex">{_examples(examples.get(r.name, []))}</td>'
        f"<td>{_ref_links([(f'/d/{did}', n) for did, n in used_in.get(r.name, [])])}</td></tr>"
        for r in _mapping_rules(ds, mapping, versions)
    )
    return (
        # The type is the icon before the name — what the column's formula gave,
        # read from the built data. "Dashboards": the ones whose charts read it.
        '<table class="ds-cols"><thead><tr><th>Column</th><th>Formula</th><th>Examples</th><th>Dashboards</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _examples(values: list[str]) -> str:
    """A column's first distinct values as it was uploaded, each in a label."""
    return '<span class="col-examples">' + "".join(
        f'<span class="col-example" style="--n:{len(v)}" title="{escape(v, quote=True)}"'
        f' data-value="{escape(v, quote=True)}">{escape(v)}</span>'
        for v in values
    ) + "</span>"


def _history_menu(ds: Dataset, versions: list[Version], authors: bool = True) -> str:
    """The version chip — `v3.4` — opening every version, grouped by upload.
    A version opens the comparison with the one in use; an upload that isn't
    the one in use can be deleted, with its versions."""
    in_use = {ds.version.split(".")[0], ds.building.split(".")[0]} - {""}
    groups = []
    for major in sorted({v.major for v in versions}, reverse=True):
        mine = [v for v in versions if v.major == major]
        delete = "" if str(major) in in_use else (
            f'<form class="ds-major-delete" method="post" action="/datasets/{quote(ds.name)}/versions/{major}/delete"'
            f' onsubmit="return confirm(\'Delete upload {major} and its {len(mine)} version(s)?\')">'
            f'<button class="act act-danger" type="submit" title="Delete upload {major} and its versions"'
            f' aria-label="Delete upload {major}">{_icon("trash")}</button></form>'
        )
        items = "".join(
            f'<button type="button" class="ds-ver" data-version="{v.label}">'
            f'<span class="ds-ver-label">{v.label}</span>'
            f'<span class="muted">{_stamp(datetime.fromtimestamp(v.created_at, timezone.utc).isoformat())}</span>'
            + (f'<span class="muted">{escape(v.author or "")}</span>' if authors else "")
            + (f'<span class="ds-mark" title="In use" aria-label="In use">{_icon("check")}</span>'
               if v.label == ds.version else "")
            + ('<span class="ds-mark" title="Building" aria-label="Building"><span class="ds-spin"></span></span>'
               if v.status == "building" else "")
            + "</button>"
            for v in mine
        )
        # One group per upload, set apart by a rule — no heading (named for
        # screen readers only); an upload that can go shows just a trash icon,
        # in its own strip beside the group's rows.
        groups.append(f'<div class="ds-major" role="group" aria-label="Upload {major}">{delete}{items}</div>')
    label = f"v{ds.version}" if ds.version else '<span class="ds-spin" title="Building"></span>'
    return (
        '<details class="ds-history" id="ds-history">'
        f'<summary title="Versions">{label}</summary>'
        f'<div class="ds-history-menu">{"".join(groups)}</div></details>'
    )


def _build_status(ds: Dataset) -> str:
    """One line while a version builds, or when the last one failed."""
    if ds.building:
        return (f'<div class="ds-status is-building" id="ds-status" title="Building {ds.building}">'
                f'<span class="ds-spin"></span>{ds.building}</div>')
    if ds.error:
        return f'<div class="ds-status is-error" id="ds-status">{escape(ds.error)}</div>'
    return '<div class="ds-status" id="ds-status" hidden></div>'


def _page_data(ds: Dataset, mapping: str, versions: list[Version], examples: dict[str, list[str]],
               used_in: dict[str, list[tuple[str, str]]]) -> str:
    """What dataset-columns.js works from. `</` is escaped so no value can end
    the <script> it sits in."""
    rules = _mapping_rules(ds, mapping, versions)
    data = {
        "name": ds.name,
        "mapping": mapping,
        "rules": [{"name": r.name, "formula": r.formula, "service": r.service} for r in rules],
        "serviceIcon": _icon("service"),
        "examples": examples,
        "usedIn": used_in,
        # Column name -> [icon, kind] from the built data (a column not built
        # yet — an unsaved edit — has none).
        "kinds": _kinds(ds),
        "version": ds.version,
        "building": ds.building,
        "versions": [
            {"version": v.label, "created_at": v.created_at, "author": v.author, "status": v.status}
            for v in versions
        ],
    }
    return json.dumps(data).replace("</", "<\\/")
