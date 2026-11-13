"""Portal mode: DB-backed dashboard persistence + a listing gallery.

Editor-only, like the rest of `web/`. This is an owner-approved exception to the
"no persistence / multi-user" anti-goal in architecture.md, kept deliberately
small: dashboards are stored as an opaque YAML text blob (validated by the
backend via `Dashboard.from_yaml`, never decomposed into tables), so every
existing stateless editor route keeps working byte-for-byte.

One store, `SqlStore`, over one tiny schema keyed by dashboard name, in
Postgres — portal mode runs on nothing else (`database_url`) — importing
`psycopg` lazily so the core install and `pip install -e ".[test]"` never need
the `.[portal]` extra; the test suite uses in-memory sqlite. Beside it, the roles and users (`RoleStore`) and the
activity log (`ActivityLog`). Tests exercise this module directly (no web stack), matching how
params/config_edit logic is kept unit-testable.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

import yaml

from fireflyer import column_map
from fireflyer import filters as filters_mod
from fireflyer import groups
from fireflyer.dashboard import Dashboard, DashboardError
from fireflyer.datasets import Dataset, Version
from fireflyer.params import COLUMN_TYPES, column_type, type_icon_svg
from fireflyer.web import assets
from fireflyer.web import auth

# Portal mode runs on Postgres and nothing else; sqlite is local mode's (a
# roles file) and the tests'.
_POSTGRES = ("postgres://", "postgresql://")


def is_postgres(dsn: str) -> bool:
    return dsn.startswith(_POSTGRES)


def database_url(env=os.environ) -> str:
    """Portal mode's database, `DATABASE_URL` — a Postgres one, or portal mode
    doesn't start (SystemExit, saying what to set)."""
    dsn = env.get("DATABASE_URL", "")
    if not is_postgres(dsn):
        raise SystemExit(
            "portal mode needs Postgres: set DATABASE_URL (or portal.yaml's database_url)"
            " to postgresql://user:password@host:5432/db"
        )
    return dsn


# A dashboard is keyed by its group and its name — the YAML's `name:`, unique
# in its group like a dataset's — so the log, a link and a person all say the
# same thing.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboards ("
    " group_name TEXT NOT NULL,"
    " name TEXT NOT NULL,"
    " author TEXT NOT NULL DEFAULT '',"
    " yaml TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL,"
    " PRIMARY KEY (group_name, name))"
)
# Every saved state of every dashboard, numbered 1, 2, 3… per dashboard, with
# who saved it and when (Unix seconds). What the editor's compare view diffs.
_VERSIONS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboard_versions ("
    " group_name TEXT NOT NULL,"
    " dashboard TEXT NOT NULL,"
    " version INTEGER NOT NULL,"
    " yaml TEXT NOT NULL,"
    " author TEXT NOT NULL DEFAULT '',"
    " created_at BIGINT NOT NULL,"
    " PRIMARY KEY (group_name, dashboard, version))"
)


# What each dashboard reads — `(dataset group, dataset, column)` from
# `Dashboard.uses`, written on every save — so the overviews (a dashboard's
# datasets, a dataset's dashboards, a column's) are a query, not a parse of every
# dashboard. Each indexed dashboard also has one `('', '', '')` row, so "indexed,
# uses nothing" is told apart from "not indexed yet" (a dashboard from before
# the index).
_USES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS dashboard_uses ("
    " group_name TEXT NOT NULL,"
    " dashboard TEXT NOT NULL,"
    " dataset_group TEXT NOT NULL,"
    " dataset TEXT NOT NULL,"
    " column_name TEXT NOT NULL)"
)
_TABLES = (_SCHEMA, _VERSIONS_SCHEMA, _USES_SCHEMA)


@dataclass(frozen=True)
class Use:
    """One thing a dashboard reads: a dataset (`column` ""), one of its columns,
    or every column (`column` "*"). `group`: the dashboard's; `dataset_group`:
    the dataset's ("" where there are no groups: the same as the dashboard's)."""
    dashboard_id: str
    dashboard_name: str
    dataset: str
    column: str
    group: str = ""
    dataset_group: str = ""


def _uses_of(yaml: str, group: str = "") -> list[tuple[str, str]]:
    try:
        return Dashboard.uses(yaml, group)
    except DashboardError:
        return []


@dataclass
class DashboardRow:
    id: str
    name: str
    author: str
    yaml: str
    updated_at: str
    group: str = ""

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


# The Access tab's roles: a name each — its key — and when it was made and
# last renamed.
_ROLES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS roles ("
    " name TEXT PRIMARY KEY,"
    " description TEXT NOT NULL DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)"
)


# The Users tab's people (portal mode): a name each — its key — and the roles
# they hold, by name (a rename updates the holdings).
_USERS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS users ("
    " name TEXT PRIMARY KEY,"
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
    " user_name TEXT NOT NULL,"
    " role_name TEXT NOT NULL,"
    " PRIMARY KEY (user_name, role_name))"
)


# The groups datasets and dashboards live in: each user's own (personal, `_bob`)
# and the shared ones an admin makes. Kept beside the roles and users, which
# are global — one Access tab, one Users tab for every group.
_GROUPS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS portal_groups ("
    " name TEXT PRIMARY KEY,"
    " personal INTEGER NOT NULL DEFAULT 0,"
    " description TEXT NOT NULL DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)"
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


@dataclass
class Role:
    name: str              # its key
    created_at: str        # ISO UTC, as the dashboards' timestamps
    updated_at: str
    description: str = ""  # what the role is for — under its name, and in tooltips
    builtin: bool = False  # the admin role: shown locked


@dataclass
class Group:
    name: str              # its key, and its URL's segment
    created_at: str
    updated_at: str
    personal: bool = False  # a user's own: comes and goes with them
    description: str = ""


@dataclass
class User:
    name: str              # its key
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


def _valid_user(name: str) -> bool:
    try:
        groups.check_user_name(name)
    except groups.GroupError:
        return False
    return True


def _role_name(name: str, roles: list, keep: str = "", kind: str = "role") -> str:
    """`name` trimmed, checked: not empty, fit for a URL (`/access/<name>/…`,
    like a dataset's: no / or \\, no leading dot), and no other role (than
    `keep`, the one being renamed) has it. Users' names too (`kind`) — which
    are stricter: their group, `_<name>`, carries it (`groups.check_user_name`)."""
    name = name.strip()
    if not name:
        raise RoleError(f"a {kind} needs a name")
    if "/" in name or "\\" in name or name.startswith("."):
        raise RoleError(f"a {kind} name can't contain / or \\ or start with a dot")
    if kind == "user" and name != keep and not _valid_user(name):
        raise RoleError("a user name is lowercase latin letters, then letters or digits")
    if any(r.name == name and r.name != keep for r in roles):
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
    """The roles, in a database — the portal's Postgres (`psycopg` imported
    only then), or a local sqlite file when there's no portal. Its own connection, like the dataset catalog's.
    Roles and users are keyed by name.

    `admin`: the login's user name, in portal mode — the store then always has
    the built-in `admin` role and lists that user first, holding it; both
    locked. "" (local mode, tests) has neither."""

    def __init__(self, dsn: str, admin: str = ""):
        self._pg = is_postgres(dsn)
        if self._pg:
            import psycopg

            self._conn = psycopg.connect(dsn, autocommit=True)
        else:
            if dsn != ":memory:":
                Path(dsn).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._migrate_ids()
        self._create()
        self._admin = admin
        if admin:
            if not any(r.name == ADMIN for r in self.roles()):
                self.create_role(ADMIN, _ADMIN_ABOUT)
            self._check_user_names()
            self._ensure_group(groups.ADMIN_GROUP, personal=True)
            for user in self.users()[1:]:
                self._ensure_group(groups.personal(user.name), personal=True)

    def _check_user_names(self) -> None:
        """Each user's group is `_<name>`, so a name must be one a group can
        carry. Users from before the rule stop the portal, named, rather than be
        renamed — a rename changes the name they sign in with."""
        bad = [u.name for u in self.users()[1:] if not _valid_user(u.name)]
        if bad:
            raise SystemExit(
                "rename these users first — a user name is lowercase latin letters, then"
                f" letters or digits: {', '.join(bad)}"
            )

    def _create(self) -> None:
        for schema in (_ROLES_SCHEMA, _USERS_SCHEMA, _USER_ROLES_SCHEMA, _GROUPS_SCHEMA):
            self._run(schema)

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._pg:
            sql = sql.replace("?", "%s")
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall() if cursor.description else []
        if not self._pg:
            self._conn.commit()
        return rows

    def _migrate_ids(self) -> None:
        """Roles and users were once keyed by a UUID (and held by it): rebuild
        those tables keyed by their names, which were unique already. Read by
        column name — older tables don't all have every column."""
        try:
            self._run("SELECT id FROM roles LIMIT 1")
        except Exception:
            if self._pg:
                self._conn.rollback()
            return                                  # new tables, or none yet
        roles, users, held = self._table("roles"), self._table("users"), self._table("user_roles")
        for table in ("user_roles", "users", "roles"):
            self._run(f"DROP TABLE IF EXISTS {table}")
        self._create()
        now = _now()
        role_names = {str(r["id"]): r["name"] for r in roles}
        user_names = {str(u["id"]): u["name"] for u in users}
        for r in roles:
            self._run("INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                      (r["name"], r.get("description") or "", str(r.get("created_at") or now),
                       str(r.get("updated_at") or now)))
        columns = ("first_name", "last_name", "email", "password_hash", "session_key")
        for u in users:
            self._run(
                "INSERT INTO users (name, first_name, last_name, email, password_hash, session_key, source,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (u["name"], *(u.get(c) or "" for c in columns), u.get("source") or LOCAL,
                 str(u.get("created_at") or now), str(u.get("updated_at") or now)),
            )
        for h in held:
            user, role = user_names.get(str(h["user_id"])), role_names.get(str(h["role_id"]))
            if user and role:
                self._run("INSERT INTO user_roles (user_name, role_name) VALUES (?, ?)", (user, role))

    def _table(self, table: str) -> list[dict]:
        """Every row of `table` as {column: value}; [] when there's no such table."""
        try:
            cursor = self._conn.execute(f"SELECT * FROM {table}")
        except Exception:
            if self._pg:
                self._conn.rollback()
            return []
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def roles(self) -> list[Role]:
        rows = self._run("SELECT name, created_at, updated_at, description FROM roles ORDER BY name")
        builtin = lambda n: bool(self._admin) and n == ADMIN
        return [Role(n, str(c), str(u), _ADMIN_ABOUT if builtin(n) else d or "", builtin(n))
                for n, c, u, d in rows]

    def _unlocked(self, roles_or_users: list, name: str, kind: str) -> None:
        if any(x.name == name and x.builtin for x in roles_or_users):
            raise RoleError(f"the {kind} is built in")

    def create_role(self, name: str, description: str = "") -> str:
        name = _role_name(name, self.roles())
        now = _now()
        self._run(
            "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, description.strip(), now, now),
        )
        return name

    def rename_role(self, name: str, new_name: str, description: str | None = None) -> str:
        """A new name — held by the same users — and description, unless None
        (kept as it is). Returns the name now."""
        self._unlocked(self.roles(), name, "admin role")
        new_name = _role_name(new_name, self.roles(), keep=name)
        if description is None:
            self._run("UPDATE roles SET name = ?, updated_at = ? WHERE name = ?", (new_name, _now(), name))
        else:
            self._run("UPDATE roles SET name = ?, description = ?, updated_at = ? WHERE name = ?",
                      (new_name, description.strip(), _now(), name))
        self._run("UPDATE user_roles SET role_name = ? WHERE role_name = ?", (new_name, name))
        return new_name

    def delete_role(self, name: str) -> None:
        self._unlocked(self.roles(), name, "admin role")
        self._run("DELETE FROM user_roles WHERE role_name = ?", (name,))
        self._run("DELETE FROM roles WHERE name = ?", (name,))

    # --- users ---

    def users(self) -> list[User]:
        held: dict[str, list[str]] = {}
        for user, role in self._run("SELECT user_name, role_name FROM user_roles ORDER BY role_name"):
            held.setdefault(user, []).append(role)
        rows = self._run(
            "SELECT name, created_at, updated_at, first_name, last_name, email,"
            " password_hash <> '', source FROM users ORDER BY name"
        )
        stored = [User(n, str(c), str(u), held.get(n, []), f or "", l or "", e or "",
                       has_password=bool(pw), source=src or LOCAL)
                  for n, c, u, f, l, e, pw, src in rows]
        if not self._admin:
            return stored
        # Not a row: the login, first — its name is taken for any other user.
        # Its password is the environment's, not managed here.
        return [User(self._admin, "", "", [ADMIN], builtin=True, has_password=True)] + stored

    def user(self, name: str) -> User | None:
        return next((u for u in self.users() if u.name == name), None)

    def create_user(self, name: str, roles: list[str] = (),
                    first_name: str = "", last_name: str = "", email: str = "") -> str:
        name = _role_name(name, self.users(), kind="user")
        person = _person(first_name, last_name, email)
        now = _now()
        self._run(
            "INSERT INTO users (name, first_name, last_name, email, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, *person, now, now),
        )
        self._hold(name, roles)
        self._ensure_group(groups.personal(name), personal=True)
        return name

    def edit_user(self, name: str, new_name: str, roles: list[str] = (),
                  first_name: str = "", last_name: str = "", email: str = "") -> str:
        """Everything but the name, which never changes: their group, `_<name>`,
        and every reference to its datasets carry it. Returns the name."""
        self._unlocked(self.users(), name, "admin user")
        held = self.user(name)
        if held is None:
            raise RoleError(f"there is no user named {name!r}")
        if new_name.strip() != held.name:
            raise RoleError("a user's name can't change")
        person = _person(first_name, last_name, email)
        self._run(
            "UPDATE users SET first_name = ?, last_name = ?, email = ?, updated_at = ? WHERE name = ?",
            (*person, _now(), name),
        )
        self._run("DELETE FROM user_roles WHERE user_name = ?", (name,))
        self._hold(name, roles)
        return name

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
            now = _now()
            self._run(
                "INSERT INTO users (name, first_name, last_name, email, source, session_key,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_role_name(name, self.users(), kind="user"), *person, source,
                 auth.new_session_key(), now, now),
            )
            self._ensure_group(groups.personal(name), personal=True)
        elif any(person):
            fields = dict(zip(("first_name", "last_name", "email"), person))
            given = {k: v for k, v in fields.items() if v}
            sets = ", ".join(f"{k} = ?" for k in given)
            self._run(f"UPDATE users SET {sets}, updated_at = ? WHERE name = ?", (*given.values(), _now(), name))
        return self.session_key(name)

    def _hold(self, user: str, roles) -> None:
        known = {r.name for r in self.roles()}
        for role in dict.fromkeys(roles):
            if role in known:
                self._run("INSERT INTO user_roles (user_name, role_name) VALUES (?, ?)", (user, role))

    # --- passwords: only ever the hash, and it never leaves the store ---

    def set_password(self, name: str, password: str) -> None:
        """A new password for user `name` (the policy checked first) — which
        also rotates their session key, signing them out everywhere."""
        self._unlocked(self.users(), name, "admin user")
        self._local(name)
        try:
            hashed = auth.hash_password(auth.check_policy(password))
        except auth.PasswordError as exc:
            raise RoleError(str(exc)) from exc
        self._run("UPDATE users SET password_hash = ?, session_key = ?, updated_at = ? WHERE name = ?",
                  (hashed, auth.new_session_key(), _now(), name))

    def clear_password(self, name: str) -> None:
        """No password: the user can't sign in, and is signed out."""
        self._unlocked(self.users(), name, "admin user")
        self._local(name)
        self._run("UPDATE users SET password_hash = '', session_key = ?, updated_at = ? WHERE name = ?",
                  (auth.new_session_key(), _now(), name))

    def _local(self, name: str) -> None:
        held = self.user(name)
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

    def delete_user(self, name: str) -> None:
        """The user, and their group's row — what was in it is the app's to
        remove (`app.users_delete`), from the dashboard and dataset stores."""
        self._unlocked(self.users(), name, "admin user")
        self._run("DELETE FROM user_roles WHERE user_name = ?", (name,))
        self._run("DELETE FROM users WHERE name = ?", (name,))
        self._run("DELETE FROM portal_groups WHERE name = ?", (groups.personal(name),))

    # --- groups ---

    def groups(self) -> list[Group]:
        """Personal groups first (the admin's leading), then the shared ones."""
        rows = self._run("SELECT name, created_at, updated_at, personal, description FROM portal_groups")
        found = [Group(n, str(c), str(u), bool(p), d or "") for n, c, u, p, d in rows]
        order = lambda g: (not g.personal, g.name != groups.ADMIN_GROUP, g.name.lower())
        return sorted(found, key=order)

    def group(self, name: str) -> Group | None:
        return next((g for g in self.groups() if g.name == name), None)

    def _ensure_group(self, name: str, personal: bool = False) -> None:
        if self.group(name) is None:
            now = _now()
            self._run("INSERT INTO portal_groups (name, personal, created_at, updated_at) VALUES (?, ?, ?, ?)",
                      (name, int(personal), now, now))

    def _shared_group(self, name: str, keep: str = "") -> str:
        try:
            name = groups.check_group_name(name)
        except groups.GroupError as exc:
            raise RoleError(str(exc)) from exc
        if name != keep and self.group(name) is not None:
            raise RoleError(f"there is already a group named {name!r}")
        return name

    def create_group(self, name: str, description: str = "") -> str:
        name = self._shared_group(name)
        now = _now()
        self._run("INSERT INTO portal_groups (name, personal, description, created_at, updated_at)"
                  " VALUES (?, 0, ?, ?, ?)", (name, description.strip(), now, now))
        return name

    def _unlocked_group(self, name: str) -> Group:
        held = self.group(name)
        if held is None:
            raise RoleError(f"there is no group named {name!r}")
        if held.personal:
            raise RoleError("a personal group comes and goes with its user")
        return held

    def rename_group(self, name: str, new_name: str, description: str | None = None) -> str:
        self._unlocked_group(name)
        new_name = self._shared_group(new_name, keep=name)
        held = self.group(name)
        self._run("UPDATE portal_groups SET name = ?, description = ?, updated_at = ? WHERE name = ?",
                  (new_name, held.description if description is None else description.strip(), _now(), name))
        return new_name

    def delete_group(self, name: str) -> None:
        self._unlocked_group(name)
        self._run("DELETE FROM portal_groups WHERE name = ?", (name,))


def _name_of(yaml_text: str) -> str:
    """The dashboard's display name, from the YAML's required top-level `name:`
    key. Also validates: `from_yaml` raises DashboardError on an invalid or
    nameless dashboard. Parsing only — never opens the CSV, so a missing dataset
    path is fine."""
    return Dashboard.from_yaml(yaml_text).name


def set_yaml_name(yaml_text: str, name: str) -> str:
    """Rewrite the top-level `name:` line (cloning, a name made unique). `name:`
    is a required column-0 key, so replacing the first such line suffices."""
    return re.sub(r"(?m)^name:.*$", lambda _: f"name: {json.dumps(name, ensure_ascii=False)}", yaml_text, count=1)


class SqlStore:
    """The portal's dashboards: in Postgres (a `postgresql://` DSN — `psycopg`
    imported only then, so this module needs no `.[portal]` extra), or sqlite
    `:memory:` for the tests. One schema, `?` placeholders, adapted for
    Postgres in `_run`.

    A store reads one group's dashboards — `group(name)` gives another's, over
    the same connection; a new one reads the admin's, where everything from
    before groups went. Version history: a save that changes nothing records
    nothing, and a dashboard from before history gets its stored state as
    version 1 on its first save."""

    def __init__(self, dsn: str = ":memory:", group: str = groups.ADMIN_GROUP,
                 _shared: tuple | None = None):
        self.group_name = group
        if _shared is not None:
            self._pg, self._conn = _shared
            return
        self._pg = is_postgres(dsn)
        if self._pg:
            import psycopg  # optional dependency, only needed at portal runtime

            self._conn = psycopg.connect(dsn, autocommit=True)
        else:
            # check_same_thread=False: uvicorn serves requests off a threadpool.
            self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._migrate_ids()
        self._migrate_groups()
        for schema in _TABLES:
            self._run(schema)

    def group(self, name: str) -> "SqlStore":
        return SqlStore("", name, _shared=(self._pg, self._conn))

    def _run(self, sql: str, params=()):
        if self._pg:
            sql = sql.replace("?", "%s")
        cursor = self._conn.execute(sql, params)
        if not self._pg:
            self._conn.commit()
        return cursor

    def _try(self, sql: str):
        """Rows, or None when the statement fails — an old table's shape."""
        try:
            return self._run(sql).fetchall()
        except Exception:
            if self._pg:
                self._conn.rollback()
            return None

    def _rebuild(self) -> None:
        for table in ("dashboard_uses", "dashboard_versions", "dashboards"):
            self._run(f"DROP TABLE IF EXISTS {table}")
        for schema in _TABLES:
            self._run(schema)

    def _insert_old(self, name: str, author: str, yaml_text: str, created, updated) -> None:
        self._run(
            "INSERT INTO dashboards (group_name, name, author, yaml, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (groups.ADMIN_GROUP, name, author, yaml_text, str(created), str(updated)),
        )

    def _insert_old_version(self, name: str, version, yaml_text: str, author: str, at) -> None:
        self._run(
            "INSERT INTO dashboard_versions (group_name, dashboard, version, yaml, author, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", (groups.ADMIN_GROUP, name, version, yaml_text, author, at),
        )

    def _migrate_ids(self) -> None:
        """Dashboards were once keyed by a UUID, and two could share a name:
        rebuild those tables keyed by name — in the admin's group — a repeated
        name made unique ("Sales 2") in its row and YAML. The uses index is
        rebuilt on read."""
        old = self._try("SELECT id, name, author, yaml, created_at, updated_at FROM dashboards ORDER BY created_at")
        if old is None:
            return                                  # new tables, or none yet
        versions = self._try("SELECT dashboard_id, version, yaml, author, created_at FROM dashboard_versions") or []
        self._rebuild()
        names: dict[str, str] = {}
        for id, name, author, yaml_text, created, updated in old:
            unique, n = name, 2
            while unique in names.values():
                unique, n = f"{name} {n}", n + 1
            names[str(id)] = unique
            if unique != name:
                yaml_text = set_yaml_name(yaml_text, unique)
            self._insert_old(unique, author, yaml_text, created, updated)
        for id, version, yaml_text, author, at in versions:
            if str(id) in names:
                self._insert_old_version(names[str(id)], version, yaml_text, author, at)

    def _migrate_groups(self) -> None:
        """Dashboards keyed by name alone, from before groups: into the admin's
        group, history and all."""
        if self._try("SELECT group_name FROM dashboards LIMIT 1") is not None:
            return
        old = self._try("SELECT name, author, yaml, created_at, updated_at FROM dashboards")
        if old is None:
            return
        versions = self._try("SELECT dashboard, version, yaml, author, created_at FROM dashboard_versions") or []
        self._rebuild()
        for row in old:
            self._insert_old(*row)
        for row in versions:
            self._insert_old_version(*row)

    # --- this group's dashboards -------------------------------------------------

    def _row(self, row) -> DashboardRow:
        name, author, yaml_text, updated = row
        return DashboardRow(name, name, author, yaml_text, str(updated), self.group_name)

    def list(self) -> list[DashboardRow]:
        rows = self._run(
            "SELECT name, author, yaml, updated_at FROM dashboards WHERE group_name = ?"
            " ORDER BY updated_at DESC", (self.group_name,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, name: str) -> DashboardRow | None:
        row = self._run(
            "SELECT name, author, yaml, updated_at FROM dashboards WHERE group_name = ? AND name = ?",
            (self.group_name, name),
        ).fetchone()
        return self._row(row) if row else None

    def _free(self, name: str) -> str:
        if self.get(name) is not None:
            raise DashboardError(f"a dashboard named {name!r} already exists")
        return name

    def free_name(self, name: str) -> str:
        """`name`, or the first of "name 2", "name 3"… no dashboard has."""
        unique, n = name, 2
        while self.get(unique) is not None:
            unique, n = f"{name} {n}", n + 1
        return unique

    def create(self, yaml: str, author: str = "") -> str:
        name = self._free(_name_of(yaml))  # validates + reads the top-level `name:` key
        now = _now()
        self._run(
            "INSERT INTO dashboards (group_name, name, author, yaml, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self.group_name, name, author, yaml, now, now),
        )
        self._record(name, yaml, author)
        self._index(name, yaml)
        return name

    def save(self, name: str, yaml: str, author: str = "") -> str:
        """Keep `yaml` as dashboard `name` and return its name now: a changed
        `name:` renames it — history and all — unless another has that name.
        `author` is who saved, recorded on the version; the creator stays."""
        new = _name_of(yaml)
        if new != name:
            self._free(new)
        self._baseline(name)
        g = self.group_name
        self._run(
            "UPDATE dashboards SET name = ?, yaml = ?, updated_at = ? WHERE group_name = ? AND name = ?",
            (new, yaml, _now(), g, name),
        )
        if new != name:
            self._run("UPDATE dashboard_versions SET dashboard = ? WHERE group_name = ? AND dashboard = ?",
                      (new, g, name))
            self._run("DELETE FROM dashboard_uses WHERE group_name = ? AND dashboard = ?", (g, name))
        self._record(new, yaml, author)
        self._index(new, yaml)
        return new

    def delete(self, name: str) -> None:
        for table, key in (("dashboards", "name"), ("dashboard_versions", "dashboard"),
                           ("dashboard_uses", "dashboard")):
            self._run(f"DELETE FROM {table} WHERE group_name = ? AND {key} = ?", (self.group_name, name))

    # --- versions ----------------------------------------------------------------

    def _latest(self, name: str):
        return self._run(
            "SELECT version, yaml FROM dashboard_versions WHERE group_name = ? AND dashboard = ?"
            " ORDER BY version DESC LIMIT 1", (self.group_name, name),
        ).fetchone()

    def _record(self, name: str, yaml: str, author: str, at: int | None = None) -> int:
        latest = self._latest(name)
        if latest and latest[1] == yaml:
            return latest[0]
        number = latest[0] + 1 if latest else 1
        self._run(
            "INSERT INTO dashboard_versions (group_name, dashboard, version, yaml, author, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self.group_name, name, number, yaml, author, at if at is not None else int(time.time())),
        )
        return number

    def _baseline(self, name: str) -> None:
        """Record what's stored now as version 1, if this dashboard has none."""
        if self._latest(name) is None:
            row = self.get(name)
            if row is not None:
                self._record(name, row.yaml, row.author, _unix(row.updated_at))

    def versions(self, name: str) -> list[DashboardVersion]:
        """Newest first, without their YAML."""
        rows = self._run(
            "SELECT version, created_at, author FROM dashboard_versions"
            " WHERE group_name = ? AND dashboard = ? ORDER BY version DESC", (self.group_name, name),
        ).fetchall()
        return [DashboardVersion(int(v), int(at), a) for v, at, a in rows]

    def version(self, name: str, number: int) -> DashboardVersion | None:
        row = self._run(
            "SELECT version, created_at, author, yaml FROM dashboard_versions"
            " WHERE group_name = ? AND dashboard = ? AND version = ?", (self.group_name, name, number),
        ).fetchone()
        return DashboardVersion(int(row[0]), int(row[1]), row[2], row[3]) if row else None

    # --- what dashboards read ------------------------------------------------------

    def _index(self, name: str, yaml: str) -> None:
        g = self.group_name
        self._run("DELETE FROM dashboard_uses WHERE group_name = ? AND dashboard = ?", (g, name))
        for ref, column in [("", "")] + _uses_of(yaml, g):
            dataset_group, dataset = groups.split(ref, g) if ref else ("", "")
            self._run(
                "INSERT INTO dashboard_uses (group_name, dashboard, dataset_group, dataset, column_name)"
                " VALUES (?, ?, ?, ?, ?)", (g, name, dataset_group, dataset, column),
            )

    def _index_missing(self) -> None:
        """Dashboards from before the index: indexed the first time it's read."""
        for g, name, yaml in self._run(
            "SELECT group_name, name, yaml FROM dashboards d WHERE NOT EXISTS (SELECT 1 FROM"
            " dashboard_uses u WHERE u.group_name = d.group_name AND u.dashboard = d.name)"
        ).fetchall():
            self.group(g)._index(name, yaml)

    def _uses(self, where: str, params: tuple) -> list[Use]:
        self._index_missing()
        rows = self._run(
            "SELECT group_name, dashboard, dataset_group, dataset, column_name FROM dashboard_uses"
            f" WHERE dataset <> '' AND {where} ORDER BY group_name, dashboard, dataset, column_name", params,
        ).fetchall()
        return [Use(n, n, ds, c, g, dg) for g, n, dg, ds, c in rows]

    def uses(self) -> list[Use]:
        """What this group's dashboards read, from any group."""
        return self._uses("group_name = ?", (self.group_name,))

    def readers(self) -> list[Use]:
        """Every dashboard, of any group, reading one of this group's datasets."""
        return self._uses("dataset_group = ?", (self.group_name,))

    # --- every group -------------------------------------------------------------------

    def every(self) -> list[DashboardRow]:
        """Every dashboard of every group — for a cascade that reaches them all."""
        rows = self._run("SELECT group_name, name, author, yaml, updated_at FROM dashboards").fetchall()
        return [DashboardRow(n, n, a, y, str(u), g) for g, n, a, y, u in rows]

    def rename_group(self, old: str, new: str) -> None:
        for table in ("dashboards", "dashboard_versions", "dashboard_uses"):
            self._run(f"UPDATE {table} SET group_name = ? WHERE group_name = ?", (new, old))
        self._run("UPDATE dashboard_uses SET dataset_group = ? WHERE dataset_group = ?", (new, old))

    def drop_group(self, name: str) -> None:
        for table in ("dashboards", "dashboard_versions", "dashboard_uses"):
            self._run(f"DELETE FROM {table} WHERE group_name = ?", (name,))


# --- activity log -------------------------------------------------------------
# Who did what, when, to which thing — a security audit trail and the data for
# a usage dashboard later. One row per action, written by `app.py` after the
# response (so `status_code` is what really came back: 200, 303, 400, 403, 409,
# 500…).
# `detail` is the action's own parameters as JSON — never a password, a
# session, or a whole dashboard's YAML (a save's is in its version).

_ACTIVITY_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS activity ("
    " id {id},"
    " at TEXT NOT NULL,"
    " actor TEXT NOT NULL DEFAULT '',"
    " action TEXT NOT NULL,"
    " object_type TEXT NOT NULL DEFAULT '',"
    " object_name TEXT NOT NULL DEFAULT '',"
    " status_code INTEGER NOT NULL DEFAULT 0,"
    " detail TEXT NOT NULL DEFAULT '{{}}',"
    " ip TEXT NOT NULL DEFAULT '',"
    " user_agent TEXT NOT NULL DEFAULT '',"
    # The group the thing acted on is in ("" for what's global: roles, users).
    " group_name TEXT NOT NULL DEFAULT '')"
)
_ACTIVITY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS activity_at ON activity (at)",
    "CREATE INDEX IF NOT EXISTS activity_actor ON activity (actor, at)",
    "CREATE INDEX IF NOT EXISTS activity_object ON activity (object_type, object_name, at)",
)
_DAY = 24 * 60 * 60


@dataclass
class Activity:
    actor: str
    action: str
    object_type: str = ""
    object_name: str = ""
    status_code: int = 200
    detail: dict = field(default_factory=dict)
    ip: str = ""
    user_agent: str = ""
    at: str = ""
    group: str = ""


class ActivityLog:
    """The `activity` table, in the portal's Postgres (sqlite `:memory:` for
    the tests). Rows older than `keep_days` are dropped when it
    opens and then once a day as it writes; 0 keeps them all."""

    def __init__(self, dsn: str, keep_days: int = 365):
        self._pg = is_postgres(dsn)
        if self._pg:
            import psycopg

            self._conn = psycopg.connect(dsn, autocommit=True)
        else:
            if dsn != ":memory:":
                Path(dsn).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._run(_ACTIVITY_SCHEMA.format(id="BIGSERIAL PRIMARY KEY" if self._pg else "INTEGER PRIMARY KEY"))
        self._add_group_column()
        for index in _ACTIVITY_INDEXES:
            self._run(index)
        self._keep_days = keep_days
        self._pruned = 0.0
        self.prune()

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._pg:
            sql = sql.replace("?", "%s")
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall() if cursor.description else []
        if not self._pg:
            self._conn.commit()
        return rows

    def _add_group_column(self) -> None:
        """A log from before groups: the column added, empty for its rows."""
        try:
            self._run("SELECT group_name FROM activity LIMIT 1")
        except Exception:
            if self._pg:
                self._conn.rollback()
            self._run("ALTER TABLE activity ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")

    def record(self, entry: Activity) -> None:
        self._run(
            "INSERT INTO activity (at, actor, action, object_type, object_name, status_code, detail, ip,"
            " user_agent, group_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.at or _now(), entry.actor, entry.action, entry.object_type, entry.object_name,
             int(entry.status_code), json.dumps(entry.detail, ensure_ascii=False, default=str),
             entry.ip, entry.user_agent[:512], entry.group),
        )
        if time.time() - self._pruned > _DAY:
            self.prune()

    def prune(self) -> None:
        self._pruned = time.time()
        if self._keep_days > 0:
            cutoff = datetime.fromtimestamp(time.time() - self._keep_days * _DAY, timezone.utc)
            self._run("DELETE FROM activity WHERE at < ?", (cutoff.isoformat(timespec="seconds"),))

    def entries(self, limit: int = 100) -> list[Activity]:
        """The newest first — what a test, or later the usage dashboard, reads."""
        rows = self._run(
            "SELECT actor, action, object_type, object_name, status_code, detail, ip, user_agent, at, group_name"
            " FROM activity ORDER BY id DESC LIMIT ?", (limit,),
        )
        return [Activity(a, ac, ot, on, int(c), json.loads(d), ip, ua, str(at), g)
                for a, ac, ot, on, c, d, ip, ua, at, g in rows]


# --- gallery page -----------------------------------------------------------
# Editor chrome, not chart output, so f-strings are fine here (the no-f-string
# rule is for chart HTML). User input — dashboard names, which are also
# their ids — is escape()'d (and quote()'d in a URL); timestamps are
# machine-generated, so they're safe. The styles and
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
    # A database: a connection, and a dataset whose data comes from one.
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>'
                '<path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    # Two arrows round: run a dataset's query again.
    "refresh": '<path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/>'
               '<path d="M21 3v5h-5M3 21v-5h5"/>',
    # A plug: test a connection.
    "plug": '<path d="M9 2v6M15 2v6"/><path d="M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v4"/>',
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
    "history": '<path d="M3 12a9 9 0 1 0 2.6-6.4L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
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


def human_size(n: int) -> str:
    """Bytes as people read them: `820 B`, `4.2 KB`, `38 KB`, `1.4 MB`."""
    size, unit = float(n), "B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            break
        size /= 1024
    if unit == "B":
        return f"{n} B"
    return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"


def _size(n: int, what: str) -> str:
    """A size, with its exact bytes in the tooltip."""
    title = escape(f"{what}: {n:,} bytes", quote=True)
    return f'<span title="{title}">{human_size(n)}</span>'


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
    from the store's index when given, else worked out from its YAML — each
    as a reference from its group: another group's with a `group:` prefix.
    `known`: the references that exist."""
    refs = sorted(uses.get(row.id, []) if uses is not None else Dashboard.dataset_names(row.yaml, row.group))
    missing = set(refs) - known if known is not None else set()
    return _ref_links([(groups.dataset_url(*groups.split(r, row.group)), r) for r in refs], missing)


def list_table(heads: list[str], rows: list[str], empty: str, add: str) -> str:
    """A list page's table: `heads` (the column names), one `<tr>` per row,
    and — at the head of the row actions' column — `add`, the page's + (so it
    sits over the actions it goes with). With no rows the head stays, the +
    in it, over `empty`."""
    head = "".join(f"<th>{h}</th>" for h in heads) + f'<th class="actions">{add}</th>'
    body = "".join(rows) or f'<tr><td class="empty-row" colspan="{len(heads) + 1}">{empty}</td></tr>'
    return f'<table class="dash-table ff-list"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _add_button(title: str, onclick: str) -> str:
    return f'<button class="act" type="button" title="{title}" onclick="{onclick}">{_icon("plus")}</button>'


# A row whose data isn't all shared with the viewer: listed, greyed, at the
# end — and it still opens, its charts on what they can't read locked.
_INACTIVE_ROW = ' class="is-inactive" title="Some of its data isn\'t shared with you"'


def _row(row: DashboardRow, authors: bool = True, known: set[str] | None = None,
         uses: dict[str, list[str]] | None = None, inactive: bool = False,
         can_write: bool = True) -> str:
    updated = _stamp(row.updated_at)
    url = groups.dashboard_url(row.group, row.id)
    remove = (
        f'<form method="post" action="{escape(url, quote=True)}/delete"'
        " onsubmit=\"return confirm('Remove this dashboard?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
    ) if can_write else ""
    return (
        f"<tr{_INACTIVE_ROW if inactive else ''}>"
        f'<td>{name_cell(url, row.name, row.description)}</td>'
        f'<td class="muted">{escape(row.group)}</td>'
        f"{_author_cell(row.author, authors)}"
        f'<td class="muted">{updated}</td>'
        f"<td>{_dashboard_datasets(row, known, uses)}</td>"
        # The name opens the dashboard, so the row needs no edit button.
        '<td class="actions">'
        f'<button class="act" type="button" title="Clone" data-url="{escape(url, quote=True)}"'
        f' data-name="{escape(row.name, quote=True)}" onclick="openClone(this)">{_icon("copy")}</button>'
        f"{remove}</td></tr>"
    )


def _group_field(choices: list[str] | None, current: str) -> str:
    """Where a clone goes: a pick of the groups the user may write — or, with
    one to go to, just that one."""
    if not choices:
        return ""
    if len(choices) == 1:
        return f'\n    <input type="hidden" name="group" value="{escape(choices[0], quote=True)}">'
    options = "".join(
        f'<option value="{escape(g, quote=True)}"{" selected" if g == current else ""}>{escape(g)}</option>'
        for g in choices
    )
    return f'\n    <select name="group" aria-label="Group" title="Group">{options}</select>'


def _dialog(
    dialog_id: str, form_id: str, action: str, heading: str, ok_label: str,
    description: bool = False, placeholder: str = "Dashboard name", extra: str = "",
) -> str:
    # `action` is empty for the clone dialog — set by JS from the clicked row.
    # Only a new dashboard asks for a description: a clone keeps its source's.
    about = (
        '\n    <textarea name="description" placeholder="Description (optional)"></textarea>'
        if description else ""
    ) + extra
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


def nav_switch(active: str, users: bool = False, group: str = "", connections: bool = False) -> str:
    """The list pages' left-nav control: a segmented Dashboards | Datasets |
    Access switch (`active` marks the current list) — and Users and Groups,
    with `users` (portal mode, an admin), and Connections with `connections`
    (an admin; anyone where there are no logins). The first two stay in
    `group`; with none they open the viewer's own. Not shown on selected-item
    pages."""
    def seg(href, label, key):
        cls = " active" if key == active else ""
        return f'<a class="ff-switch-seg{cls}" href="{href}">{label}</a>'
    where = f"/{groups.url_name(group)}" if group else ""
    return (
        '<nav class="ff-switch">'
        + seg(f"/d{where}" if group else "/", "Dashboards", "dashboards")
        + seg(f"/datasets{where}", "Datasets", "datasets")
        + seg("/access", "Access", "access")
        + (seg("/users", "Users", "users") + seg("/groups", "Groups", "groups") if users else "")
        + (seg("/connections", "Connections", "connections") if connections else "")
        + "</nav>"
    )


def path_dropdown(paths: list[str] | None, active_path: str | None, kind: str = "d") -> str:
    """A labelled dropdown showing the active path; picking one opens that
    path's list (`kind`: `d`, its dashboards, or `datasets`). Lives on the
    **right** of the topbar. Empty with no paths (portal / plain)."""
    if not paths:
        return ""
    items = "".join(
        f'<a href="/{kind}/{groups.url_name(p)}"'
        f'{" class=\"active\"" if p == active_path else ""}>{escape(p)}</a>'
        for p in paths
    )
    label = escape(active_path or "path")
    return (
        '<details class="ff-pathdd"><summary title="Switch path">'
        f"{label}{_CHEVRON}</summary>"
        f'<div class="ff-pathdd-menu">{items}</div></details>'
    )


def group_label(name: str, kind: str = "Group") -> str:
    """The group (or path, `kind`) an opened dashboard or dataset is in, where
    the lists have the picker: the picker's look, read-only — back is the way
    to another."""
    return f'<span class="ff-group-label" title="{kind}" aria-label="{kind}">{escape(name)}</span>'


def group_picker(all_groups: list[Group], current: str, inactive: set[str] = frozenset(),
                 kind: str = "d") -> str:
    """Portal mode's group dropdown, on the right of the topbar: the group
    you're in, and a pick of every group — shared ones and personal ones, one
    list at a time behind a switch at the menu's head (as the view-as menu has
    roles | users). It opens on the shared groups, the ones people work in
    together; on the personal ones only while there's no shared group yet. A
    group with nothing shared with you is greyed (`inactive`) but still opens.
    `kind`: which list a pick opens, `d` or `datasets`. static/group-picker.js
    switches the lists."""
    if not all_groups:
        return ""
    opening = "shared" if any(not g.personal for g in all_groups) else "personal"

    def item(g: Group) -> str:
        classes = " ".join(c for c in ("active" if g.name == current else "",
                                       "is-inactive" if g.name in inactive else "") if c)
        tip = "Nothing here is shared with you" if g.name in inactive else g.description
        cls = f' class="{classes}"' if classes else ""
        return f'<a href="/{kind}/{groups.url_name(g.name)}"{cls}{_tip(tip)}>{escape(g.name)}</a>'

    lists = "".join(
        f'<div class="ff-group-list" data-kind="{k}"{"" if k == opening else " hidden"}>'
        + ("".join(item(g) for g in all_groups if g.personal == (k == "personal"))
           or '<span class="ff-role-none">None yet</span>')
        + "</div>"
        for k in ("shared", "personal")
    )
    kinds = "".join(
        f'<button type="button" data-kind="{k}" title="{label}" aria-label="{label}"'
        f'{" class=active" if k == opening else ""}>{_icon(icon)}</button>'
        for k, icon, label in (("shared", "users", "Shared groups"), ("personal", "user", "Personal groups"))
    )
    return (
        '<details class="ff-pathdd ff-group" id="ff-group"><summary title="Switch group">'
        f"{escape(current)}{_CHEVRON}</summary>"
        f'<div class="ff-pathdd-menu"><div class="ff-theme ff-group-kinds" role="group"'
        f' aria-label="Groups">{kinds}</div>{lists}</div></details>'
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
    picker: str = "",
    detail: bool = False,
    head: str = "",
    users: bool = False,
    group: str = "",
    connections: bool = False,
) -> str:
    """The gallery page frame. Overview pages (dashboards/datasets lists) lead
    with a Dashboards | Datasets switch on the left; on the **right** the group
    picker (`picker`: portal mode's groups, or local paths mode's paths).
    Detail pages (a selected dataset) lead with a back button in `topbar_left`
    and show their group read-only in `picker` (`group_label`): back goes to
    the list, which has the picker.
    Then optional `topbar_right`, then `body`. No second nav bar."""
    nav = "" if detail else nav_switch(active, users, group, connections)
    right = picker + topbar_right + theme_switch() + user_menu
    right_html = f'<span class="topbar-right">{right}</span>' if right else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
{assets.favicons()}
{assets.stylesheets("gallery.css", "nav.css", "profile.css")}
{assets.script("gallery.js")}
{assets.script("group-picker.js")}
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
    picker: str = "", authors: bool = True,
    datasets: set[str] | None = None, uses: dict[str, list[str]] | None = None,
    users: bool = False, group: str = groups.LOCAL_GROUP, inactive: set[str] = frozenset(),
    connections: bool = False,
    can_write: bool = True, clone_groups: list[str] | None = None,
) -> str:
    """Group `group`'s dashboards. `datasets`: the references that exist, so a
    dashboard naming one that doesn't can say so; None links every name.
    `uses`: dashboard id -> the references it reads, from the store's index;
    None works it out from each YAML. `inactive`: the ids whose data isn't all
    shared with the viewer. `can_write`: whether they may add and remove here;
    `clone_groups`: where they may clone to."""
    body_table = list_table(
        ["Name", "Group", *(["Author"] if authors else []), "Last updated", "Datasets"],
        [_row(r, authors, datasets, uses, r.id in inactive, can_write) for r in rows],
        "No dashboards yet", _add_button("New dashboard", "openAdd()") if can_write else "",
    )
    here = groups.url_name(group)
    add_dialog = _dialog("add-dialog", "add-form", f"/d/{here}/new", "New dashboard", "Create",
                         description=True) if can_write else ""
    clone_dialog = _dialog("clone-dialog", "clone-form", "", "Clone dashboard", "Clone",
                           extra=_group_field(clone_groups, group))
    clone_dialog = clone_dialog.replace('id="clone-form-name"', 'id="clone-name"')
    return _shell(
        title, user_menu, "dashboards", body_table,
        extra=add_dialog + clone_dialog,
        picker=picker, users=users, group=group, connections=connections,
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


def _new_dataset_dialog(base: str, sources: list[str] | None) -> str:
    """A new dataset: a name and a description, then a CSV file — or, for one
    who may connect (`sources`, the connections), a query on a database: the
    CSV | SQL switch swaps the fields and where the form posts. The database
    checks the SQL; its error comes back as the page."""
    if not sources:
        return _upload_dialog(f"{base}/new", "New dataset", "Upload", "upload-dialog", True)
    options = "".join(f'<option value="{escape(c, quote=True)}">{escape(c)}</option>' for c in sources)
    kinds = "".join(
        f'<button type="button" data-kind="{k}" title="{t}"{" class=active" if k == "csv" else ""}>{label}</button>'
        for k, label, t in (("csv", "CSV", "Upload a CSV file"), ("sql", "SQL", "A query on a database"))
    )
    csv, sql = escape(f"{base}/new", quote=True), escape(f"{base}/new-query", quote=True)
    return f"""
<dialog class="wide" id="upload-dialog">
  <form method="post" action="{csv}" enctype="multipart/form-data" data-csv="{csv}" data-sql="{sql}">
    <div class="conn-head"><h3>New dataset</h3>
      <div class="ds-modes ds-kinds" role="group" aria-label="From">{kinds}</div></div>
    <label>Name</label><input name="name" autocomplete="off" required>
    <label>Description</label><textarea name="description"></textarea>
    <div class="ds-kind" data-kind="csv">
      <label>CSV file</label>
      <input type="file" name="file" accept=".csv,text/csv" required>
      <label>Delimiter</label>
      <select name="delimiter">
        <option value=",">Comma ( , )</option>
        <option value=";">Semicolon ( ; )</option>
        <option value="\t">Tab</option>
        <option value="|">Pipe ( | )</option>
      </select>
    </div>
    <div class="ds-kind" data-kind="sql" hidden>
      <label>Connection</label><select name="connection" disabled>{options}</select>
      <label>Query</label>
      <textarea name="query" class="ff-sql" rows="8" spellcheck="false" required disabled
                placeholder="select …"></textarea>
    </div>
    <div class="dialog-actions">
      <button type="button" class="cancel" onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">Create</button>
    </div>
  </form>
</dialog>"""


def _version_cell(ds: Dataset) -> str:
    """`2.1`, or `2.1 → 2.2` while the next one builds."""
    if ds.building:
        return f"{ds.version} → {ds.building}" if ds.version else f"→ {ds.building}"
    return ds.version


def _dataset_row(ds: Dataset, authors: bool = True,
                 used_by: dict[str, list[tuple[str, str]]] | None = None,
                 group: str = groups.LOCAL_GROUP, inactive: bool = False, can_write: bool = True) -> str:
    href = groups.dataset_url(group, ds.name)
    updated = _stamp(ds.updated_at)
    dashboards = "" if used_by is None else "<td>" + _ref_links(used_by.get(ds.name, [])) + "</td>"
    remove = (
        f'<form method="post" action="{escape(href, quote=True)}/delete"'
        " onsubmit=\"return confirm('Remove this dataset?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
    ) if can_write else ""
    row = ' class="is-inactive" title="Not shared with you"' if inactive else ""
    return (
        f"<tr{row}>"
        f'<td>{name_cell(href, ds.name, ds.description)}</td>'
        f'<td class="muted">{escape(group)}</td>'
        f'<td class="muted mono">{_version_cell(ds)}</td>'
        f'<td class="muted">{len(ds.columns) + len(ds.service)}</td>'
        f'<td class="muted">{ds.rows}</td>'
        f'<td class="muted">{_size(ds.size, "Stored, every upload and the built data")}</td>'
        f'<td class="muted">{updated}</td>'
        f"{_author_cell(ds.author, authors)}"
        f"{dashboards}"
        # The name opens the dataset — where it's renamed and described — so
        # the row needs no view or edit button of its own.
        f'<td class="actions">{remove}</td></tr>'
    )


def render_datasets(
    datasets: list[Dataset], title: str = "Fireflyer Portal", user_menu: str = "",
    picker: str = "", authors: bool = True,
    used_by: dict[str, list[tuple[str, str]]] | None = None,
    users: bool = False, group: str = groups.LOCAL_GROUP, inactive: set[str] = frozenset(),
    connections: bool = False,
    can_write: bool = True,
    sources: list[str] | None = None,
) -> str:
    """Group `group`'s datasets. `used_by`: dataset name -> (href, label) of
    each dashboard reading it; None (no dashboards to look at, plain local
    mode) leaves the column out. `inactive`: the names not shared with the
    viewer. `sources`: the connections a new dataset may read from — for an
    admin; None or empty offers only a file."""
    table = list_table(
        ["Name", "Group", "Version", "Columns", "Rows", "Size", "Updated", *(["Author"] if authors else []),
         *(["Dashboards"] if used_by is not None else [])],
        [_dataset_row(d, authors, used_by, group, d.name in inactive, can_write) for d in datasets],
        "No datasets yet",
        _add_button("New dataset", "openUpload()") if can_write else "",
    )
    base = f"/datasets/{groups.url_name(group)}"
    extra = _new_dataset_dialog(base, sources) if can_write else ""
    return _shell(
        title, user_menu, "datasets", table,
        extra=extra,
        picker=picker, users=users, group=group, connections=connections,
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

    per_role: list[list] = []
    for key, value in form.multi_items():
        if key == "role":
            per_role.append([(key, value)])
        elif per_role:
            per_role[-1].append((key, value))
    return {items[0][1]: parse_filters(_Items(items)) for items in per_role}


# --- access gallery -----------------------------------------------------------


def _role_row(role: Role, datasets: list[tuple[str, str]] | None = None) -> str:
    """`datasets`: (href, label) of the ones whose permissions name the role,
    linked as the dashboards list links a dashboard's; None leaves the column
    out."""
    # The admin role reads every dataset: "All", rather than each one.
    used = "" if datasets is None else (
        '<td class="muted">All</td>' if role.builtin
        else "<td>" + _ref_links(datasets) + "</td>"
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
        f'<button class="act" type="button" title="Edit"'
        f' data-name="{escape(role.name, quote=True)}" data-description="{escape(role.description, quote=True)}"'
        f' onclick="openRename(this)">{_icon("pencil")}</button>'
        f'<form method="post" action="/access/{escape(quote(role.name), quote=True)}/delete"'
        " onsubmit=\"return confirm('Remove this role?')\">"
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
    )


def render_access(
    roles: list[Role], title: str = "Fireflyer Portal", user_menu: str = "",
    picker: str = "",
    datasets: dict[str, list[tuple[str, str]]] | None = None,
    users: bool = False,
    connections: bool = False,
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
        picker=picker, users=users, connections=connections,
    )


# --- connections ------------------------------------------------------------------
# The databases a dataset's data can come from (`fireflyer/connections.py`), by
# name: a URL each (no password shown — it stays in the store), the datasets
# whose `source:` names it, a test, edit and remove — none is built in.


def render_connections(
    connections: list, title: str = "Fireflyer Portal", user_menu: str = "",
    picker: str = "", used: dict[str, list[tuple[str, str]]] | None = None, users: bool = False,
) -> str:
    """Every connection. `used`: name -> (href, label) of the datasets reading
    from it."""
    used = used or {}
    table = list_table(
        ["Name", "URL", "Datasets", "Updated"],
        [_connection_row(c, used.get(c.name, [])) for c in connections],
        "No connections yet", _add_button("New connection", "openConnection()"),
    )
    return _shell(title, user_menu, "connections", table, extra=_connection_dialog(),
                  picker=picker, users=users, connections=True)


def _connection_row(c, datasets: list[tuple[str, str]]) -> str:
    name = escape(quote(c.name, safe=""), quote=True)
    # Its state is its icon: the plug, a spinner while it asks, then a check or
    # a cross — and the database's error under the URL.
    test = (f'<button class="act conn-test" type="button" title="Test the connection"'
            f' data-url="/connections/{name}/test" onclick="testConnection(this)">'
            f'<span class="t-idle">{_icon("plug")}</span><span class="t-busy ds-spin"></span>'
            f'<span class="t-ok">{_icon("check")}</span><span class="t-fail">{_icon("cross")}</span></button>')
    fields = "".join(f' data-{k}="{escape(getattr(c, k), quote=True)}"' for k in ("name", "description", "url"))
    guard = (f"return confirm('Remove this connection?')" if not datasets
             else f"alert({escape(json.dumps('Datasets read from it: ' + ', '.join(l for _, l in datasets)), quote=True)}); return false")
    actions = (
        test
        + f'<button class="act" type="button" title="Edit"{fields}'
        + (" data-has-password" if c.has_password else "")
        + f' onclick="openConnection(this)">{_icon("pencil")}</button>'
        f'<form method="post" action="/connections/{name}/delete" onsubmit="{guard}">'
        f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
    )
    return (
        f'<tr>'
        f'<td><span class="name">{escape(c.name)}</span>{_has_password(c)}{_sub(c.description)}</td>'
        f'<td class="muted mono ff-wrap">{escape(c.url)}<div class="conn-error" hidden></div></td>'
        f"<td>{_ref_links(datasets)}</td>"
        f'<td class="muted">{_stamp(c.updated_at)}</td>'
        f'<td class="actions">{actions}</td></tr>'
    )


def _has_password(c) -> str:
    return f'<span class="ff-key" title="Has a password">{_icon("key")}</span>' if c.has_password else ""


def _connection_dialog() -> str:
    """New and edit, one dialog: the fields — or the same as YAML (code), the
    form's twin. The password is never filled in: empty keeps it."""
    modes = "".join(
        f'<button type="button" data-mode="{m}" title="{t}" aria-label="{t}"'
        f'{" class=active" if m == "form" else ""}>{_icon(i)}</button>'
        for m, i, t in (("form", "pencil", "Fields"), ("code", "code", "YAML"))
    )
    return f"""
<dialog class="wide" id="conn-dialog">
  <form id="conn-form" method="post" action="/connections/new">
    <div class="conn-head"><h3 id="conn-title">New connection</h3>
      <div class="ds-modes conn-modes" role="group" aria-label="Edit as">{modes}</div></div>
    <div class="conn-fields">
      <label>Name</label><input name="name" autocomplete="off" pattern="[A-Za-z0-9_-]+" required>
      <label>Description</label><textarea name="description"></textarea>
      <label>URL</label><input name="url" class="mono" autocomplete="off" spellcheck="false" required
             placeholder="postgresql://user@host:5432/database">
      <label>Password</label>
      <input name="password" type="password" autocomplete="new-password" placeholder="">
    </div>
    <textarea name="yaml" class="ff-sql conn-yaml" rows="7" spellcheck="false" hidden disabled></textarea>
    <div class="dialog-actions">
      <button type="button" class="cancel" onclick="this.closest('dialog').close()">Cancel</button>
      <button type="submit" class="ok">Save</button>
    </div>
  </form>
</dialog>"""


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
        f'<button class="act" type="button" title="Edit"'
        f'{" data-has-password" if user.has_password else ""}'
        + (f' data-source="{escape(user.source, quote=True)}"' if user.external else "")
        + "".join(f' data-{k}="{escape(getattr(user, k.replace("-", "_")), quote=True)}"'
                  for k in ("name", "first-name", "last-name", "email"))
        + f' data-roles="{escape(json.dumps(user.roles), quote=True)}"'
        f' onclick="openUser(this)">{_icon("pencil")}</button>'
        f'<form method="post" action="/users/{escape(quote(user.name), quote=True)}/delete"'
        " onsubmit=\"return confirm('Remove this user, with their group and everything in it?')\">"
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
        Option(r.name, r.name, r.description,
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
        users=True, connections=True,
    )


# --- groups gallery -----------------------------------------------------------
# Portal mode, admins only: every group — each user's own, locked (it comes and
# goes with them), and the shared ones, which an admin adds, renames (every
# `group:` reference follows) and removes (with what's in them).


def _group_row(group: Group, dashboards: int, datasets: int, readers: int) -> str:
    """`readers`: dashboards in other groups reading its datasets — a removal
    locks their charts, and the confirm says so."""
    here = groups.url_name(group.name)
    counts = (
        f'<td><a class="ref" href="/d/{here}" title="Its dashboards">{dashboards}</a></td>'
        f'<td><a class="ref" href="/datasets/{here}" title="Its datasets">{datasets}</a></td>'
    )
    if group.personal:
        actions = f'<span class="act act-locked" title="A user\'s own group — it comes and goes with them">{_icon("lock")}</span>'
    else:
        warn = (f"Remove {group.name} with its {dashboards} dashboard(s) and {datasets} dataset(s)?"
                + (f" {readers} dashboard(s) in other groups read its data — their charts on it will lock."
                   if readers else ""))
        actions = (
            f'<button class="act" type="button" title="Edit" data-action="/groups/{here}/rename"'
            f' data-name="{escape(group.name, quote=True)}" data-description="{escape(group.description, quote=True)}"'
            f' onclick="openRename(this)">{_icon("pencil")}</button>'
            f'<form method="post" action="/groups/{here}/delete"'
            f' onsubmit="return confirm({escape(json.dumps(warn), quote=True)})">'
            f'<button class="act act-danger" type="submit" title="Remove">{_icon("trash")}</button></form>'
        )
    return (
        f'<tr{_BUILTIN_ROW if group.name == groups.ADMIN_GROUP else ""}>'
        f'<td><span class="name">{escape(group.name)}</span>{_sub(group.description)}</td>'
        f"{counts}"
        f'<td class="muted">{_stamp(group.updated_at)}</td>'
        f'<td class="actions">{actions}</td></tr>'
    )


def render_groups(
    all_groups: list[Group], title: str = "Fireflyer Portal", user_menu: str = "",
    counts: dict[str, tuple[int, int, int]] | None = None,
) -> str:
    """`counts`: group -> (its dashboards, its datasets, other groups'
    dashboards reading them)."""
    counts = counts or {}
    table = list_table(
        ["Name", "Dashboards", "Datasets", "Updated"],
        [_group_row(g, *counts.get(g.name, (0, 0, 0))) for g in all_groups],
        "No groups yet", _add_button("New group", "openAdd()"),
    )
    add_dialog = _dialog("add-dialog", "add-form", "/groups/new", "New group", "Create",
                         description=True, placeholder="Group name")
    rename_dialog = _dialog("rename-dialog", "rename-form", "", "Edit group", "Save",
                            description=True, placeholder="Group name")
    return _shell(title, user_menu, "groups", table, extra=add_dialog + rename_dialog, users=True,
                  connections=True)


def render_dataset_detail(
    ds: Dataset,
    title: str = "Fireflyer Portal",
    user_menu: str = "",
    used_by: list[tuple[str, str]] | None = None,
    group_label: str = "",
    mapping: str = "",
    versions: list[Version] | None = None,
    examples: dict[str, list[str]] | None = None,
    authors: bool = True,
    used_in: dict[str, list[tuple[str, str]]] | None = None,
    group: str = groups.LOCAL_GROUP,
    readonly: bool = False,
    elsewhere: list[tuple[str, str]] | None = None,
    ref: str = "",
    refresh: bool = False,
    sources: list[str] | None = None,
) -> str:
    """Group `group`'s dataset `ds`. `used_by`: (href, label) of the dashboards
    in its own group that read it — while there are any it can't be removed;
    `elsewhere`: those of other groups, which a removal turns into locks.
    `readonly`: someone else's — its columns and examples only, nothing to
    change, no service column, version history or permissions. `sources`:
    the connections its query may read from — for one who may change it; None
    shows a query dataset's SQL read-only."""
    if readonly:
        return _dataset_read_only(ds, title, user_menu, group_label, mapping, versions or [],
                                  examples or {}, used_in or {}, group)
    url = groups.dataset_url(group, ds.name)
    # The trash button: when the dataset is in use in its group it shows a count
    # badge and opens the list of dashboards (open in a new tab) instead of
    # deleting; when nothing there uses it, it deletes — after saying which
    # other groups' dashboards lose it.
    used_by = used_by or []
    if used_by:
        trash = (
            f'<button class="act act-danger" type="button" title="Used by {len(used_by)} dashboard(s)"'
            ' onclick="document.getElementById(\'usage-dialog\').showModal()">'
            f'{_icon("trash")}<span class="badge">{len(used_by)}</span></button>'
        )
        links = "".join(
            f'<li><a href="{escape(href, quote=True)}" target="_blank" rel="noopener">{escape(name)}</a></li>'
            for href, name in used_by
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
        lose = "".join(f"\n  {label}" for _, label in elsewhere or [])
        warn = "Remove this dataset?" + (
            f"\nThese dashboards in other groups read it — their charts on it will lock:{lose}" if lose else "")
        trash = (
            f'<form method="post" action="{escape(url, quote=True)}/delete" style="display:inline"'
            f' onsubmit="return confirm({escape(json.dumps(warn), quote=True)})">'
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
        f' data-description="{desc}" data-action="{escape(url, quote=True)}/rename">'
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
        + back_button(f"/datasets/{groups.url_name(group)}", "Back to datasets")
        + _column_modes()
        + f'<div class="topbar-title" id="ds-title">{tname}{tabout}</div>'
        + _history_menu(ds, versions, authors, group)
        + "</div>"
    )
    topbar_right = (
        '<span class="ff-savebar" role="group" aria-label="Unsaved changes">'
        '<button class="ff-discard" id="ds-discard" type="button" title="Discard unsaved changes"'
        f' aria-label="Discard unsaved changes" hidden>{_icon("cross")}</button>'
        '<button class="ff-save" id="ds-save" type="button" title="Save (⌘/Ctrl+S)" aria-label="Save"'
        f' hidden>{_icon("save")}</button></span>'
        '<span class="detail-actions">'
        + (f'<form method="post" action="{escape(url, quote=True)}/refresh" style="display:inline">'
           '<button class="act" type="submit" title="Refresh: run the query again (the next version)"'
           f' aria-label="Refresh">{_icon("refresh")}</button></form>' if refresh else "")
        + '<button class="act" title="Upload a new file (the next version)"'
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
  <script type="application/json" id="ds-data">{_page_data(ds, mapping, versions, examples or {}, used_in or {}, group, ref, sources)}</script>"""
    replace = _upload_dialog(f"{url}/replace", "Upload a new version", "Upload", "replace-dialog", False)
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
        picker=group_label, detail=True, head=page_head,
    )


def _dataset_read_only(ds: Dataset, title: str, user_menu: str, group_label: str, mapping: str,
                       versions: list[Version], examples: dict[str, list[str]],
                       used_in: dict[str, list[tuple[str, str]]], group: str) -> str:
    """Someone else's dataset: back, its name and description, and its columns
    — no service column — with the examples the viewer's role lets them see."""
    head = (
        f'<span class="thead"><span class="tname">{escape(ds.name)}</span>'
        f'<span class="tsub"{"" if ds.description else " hidden"}>{escape(ds.description)}</span></span>'
    )
    topbar_left = (
        '<div class="topbar-lead">'
        + back_button(f"/datasets/{groups.url_name(group)}", "Back to datasets")
        + f'<div class="topbar-title" id="ds-title" data-readonly="1">{head}</div>'
        + "</div>"
    )
    seen = Dataset(**{**ds.__dict__, "service": []})
    body = (f'<section class="ds-columns" id="ds-columns" data-mode="view">'
            f"{_columns_view(seen, mapping, versions, examples, used_in, service=False)}</section>")
    return _shell(title, user_menu, "datasets", body, topbar_left=topbar_left, picker=group_label, detail=True)


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
    its formula gave (text, a number from to_number(), a date from str2dt(),
    yes / no from to_bool(), a list from to_list())."""
    out = {}
    for c in ds.columns + ds.service:
        kind = column_type(c.dtype)
        out[c.name] = (_type_icon(c.dtype), COLUMN_TYPES[kind][1] if kind in COLUMN_TYPES else c.dtype)
    return out


def _columns_view(ds: Dataset, mapping: str, versions: list[Version], examples: dict[str, list[str]],
                  used_in: dict[str, list[tuple[str, str]]], service: bool = True) -> str:
    """`used_in`: column -> (href, label) of the dashboards reading it.
    `service`: whether service columns are shown (to its group, not others)."""
    kinds = _kinds(ds)
    rows = "".join(
        "<tr>"
        f"<td>{kinds.get(r.name, ('', ''))[0]}<span class=\"col-name\">{escape(r.name)}</span>"
        + (_service_mark() if r.service else "") + "</td>"
        f'<td class="muted mono">{escape(r.formula)}</td>'
        f'<td class="ex">{_examples(examples.get(r.name, []))}</td>'
        f"<td>{_ref_links(used_in.get(r.name, []))}</td></tr>"
        for r in _mapping_rules(ds, mapping, versions) if service or not r.service
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


def _history_menu(ds: Dataset, versions: list[Version], authors: bool = True,
                  ds_group: str = groups.LOCAL_GROUP) -> str:
    """The version chip — `v3.4` — opening every version, grouped by upload.
    A version opens the comparison with the one in use; an upload that isn't
    the one in use can be deleted, with its versions."""
    in_use = {ds.version.split(".")[0], ds.building.split(".")[0]} - {""}
    uploads = []
    for major in sorted({v.major for v in versions}, reverse=True):
        mine = [v for v in versions if v.major == major]
        delete = "" if str(major) in in_use else (
            f'<form class="ds-major-delete" method="post" action="{escape(groups.dataset_url(ds_group, ds.name), quote=True)}/versions/{major}/delete"'
            f' onsubmit="return confirm(\'Delete upload {major} and its {len(mine)} version(s)?\')">'
            f'<button class="act act-danger" type="submit" title="Delete upload {major} and its versions"'
            f' aria-label="Delete upload {major}">{_icon("trash")}</button></form>'
        )
        # The upload's file is the group's first version's; the rest only
        # remapped its columns, so the size shows once, there.
        first = min(mine, key=lambda v: v.minor)
        items = "".join(
            f'<button type="button" class="ds-ver" data-version="{v.label}">'
            f'<span class="ds-ver-label">{v.label}</span>'
            f'<span class="muted ds-size">{_upload_size(v) if v is first else ""}</span>'
            f'<span class="muted">{_stamp(datetime.fromtimestamp(v.created_at, timezone.utc).isoformat())}</span>'
            + (f'<span class="muted">{escape(v.author or "")}</span>' if authors else "")
            # The version in use is the one whose data is built and kept.
            + (f'<span class="muted ds-built">{_size(ds.data_size, "Built data")}</span>'
               f'<span class="ds-mark" title="In use" aria-label="In use">{_icon("check")}</span>'
               if v.label == ds.version else "")
            + ('<span class="ds-mark" title="Building" aria-label="Building"><span class="ds-spin"></span></span>'
               if v.status == "building" else "")
            + "</button>"
            for v in mine
        )
        # One group per upload, set apart by a rule — no heading (named for
        # screen readers only); an upload that can go shows just a trash icon,
        # in its own strip beside the group's rows.
        uploads.append(f'<div class="ds-major" role="group" aria-label="Upload {major}">{delete}{items}</div>')
    label = f"v{ds.version}" if ds.version else '<span class="ds-spin" title="Building"></span>'
    return (
        '<details class="ds-history" id="ds-history">'
        f'<summary title="Versions">{label}</summary>'
        f'<div class="ds-history-menu">{"".join(uploads)}</div></details>'
    )


def _upload_size(v: Version) -> str:
    """An upload's size — after a database icon when a query made it."""
    if not v.from_query:
        return _size(v.size, "Uploaded file")
    return (f'<span class="ds-from-query" title="From a database query">{_icon("database")}</span>'
            + _size(v.size, "Query result"))


def _build_status(ds: Dataset) -> str:
    """One line while a version builds, or when the last one failed."""
    if ds.building:
        return (f'<div class="ds-status is-building" id="ds-status" title="Building {ds.building}">'
                f'<span class="ds-spin"></span>{ds.building}</div>')
    if ds.error:
        return f'<div class="ds-status is-error" id="ds-status">{escape(ds.error)}</div>'
    return '<div class="ds-status" id="ds-status" hidden></div>'


def _page_data(ds: Dataset, mapping: str, versions: list[Version], examples: dict[str, list[str]],
               used_in: dict[str, list[tuple[str, str]]], group: str = groups.LOCAL_GROUP,
               ref: str = "", sources: list[str] | None = None) -> str:
    """What dataset-columns.js works from. `</` is escaped so no value can end
    the <script> it sits in."""
    rules = _mapping_rules(ds, mapping, versions)
    data = {
        "name": ds.name,
        # Its address, for the page's requests; and the reference a filter on it
        # names — a role's draft row reads it as a chart in its group would.
        "url": groups.dataset_url(group, ds.name),
        "ref": ref or ds.name,
        "group": group,
        "mapping": mapping,
        "rules": [{"name": r.name, "formula": r.formula, "service": r.service} for r in rules],
        "serviceIcon": _icon("service"),
        "databaseIcon": _icon("database"),
        "historyIcon": _icon("history"),
        # A query dataset's `source:` — edit mode shows its SQL; `connections`
        # (None: read-only) are what it may switch to.
        "source": column_map.source(mapping),
        "history": column_map.history(mapping),     # uploads to keep before the one in use
        "connections": sources,
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
