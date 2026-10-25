"""Datasets — named, versioned uploads, read by charts as Parquet.

A dataset is keyed by a **unique name** (no id): charts reference it by that name
and, per project preference, a rename cascades to dashboards while a delete is
guarded when dashboards still use it (both handled a layer up, in the web app).

**Versions** are `major.minor` — `orders:3.4`:

- every upload is a new **major**, and its original file is kept;
- the **minor** counts changes to the **column mapping** (each column a formula
  over the uploaded ones, `column_map.py`) — one counter for the dataset, never
  reset: an upload keeps it (1.3 → 2.3) unless the mapping it carries forward
  changed (new columns: 2.4), and saving a mapping is the next one (2.5);
- only the **latest** computed Parquet is kept — the one charts read. Older
  versions keep their mapping, so any two can be compared;
- a major can be deleted (with all its minors), except the current one.

Computing a version is a **build** (`build()`), which the web app runs in the
background: until it finishes, charts keep reading the previous file, and if a
column won't read as its type the version is dropped and the previous one
stays, with the error recorded for the page to show.

Everything about a dataset but its files is one **YAML document** — name,
description, versions with their mappings — kept by a catalog: as a file next
to the data in local mode (`ObjectCatalog`, the default), in the portal's
database in portal mode (`SqlCatalog`).

A mapping also carries the dataset's **permissions** (`permissions:`, see
`column_map`): who may read it, and which rows. So they're versioned with the
columns — a change is the next minor — and the published version's are the
ones in force.
"""

from __future__ import annotations

import io
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import yaml

from fireflyer import column_map
from fireflyer import filters as filters_mod
from fireflyer.storage import ObjectStore


class DatasetError(Exception):
    """Bad dataset input (unknown name, duplicate, unreadable CSV, bad mapping)."""


@dataclass
class Column:
    name: str
    dtype: str  # Polars dtype string, e.g. "Int64", "String", "Float64"


@dataclass
class Dataset:
    name: str
    description: str
    delimiter: str
    columns: list[Column]
    rows: int
    author: str
    updated_at: str
    version: str = ""        # the published version, "3.4"; "" until the first build
    building: str = ""       # the version being built, if any
    error: str = ""          # why the last build failed, if it did
    # Service columns: in the dataset, never in the data charts read — so not in
    # `columns`, which is what dashboards and their editors are offered.
    service: list[Column] = field(default_factory=list)


@dataclass
class Version:
    major: int
    minor: int
    created_at: int          # unix seconds
    author: str
    status: str              # "ready" or "building"
    rows: int = 0
    mapping: str = ""
    sources: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.major}.{self.minor}"


def _now() -> int:
    return int(time.time())


def _iso(unix: int) -> str:
    return datetime.fromtimestamp(unix, timezone.utc).isoformat(timespec="seconds")


# Files, per dataset, under `datasets/<name>/`: each major's original, the
# latest computed Parquet and (locally) the YAML. Their own folder, so nothing
# else kept in the same place — a path's dashboard history — can collide with a
# dataset's name.
_ROOT = "datasets/"
# A role's permission that narrows nothing.
FULL = "full"


def _folder(name: str) -> str:
    return f"{_ROOT}{name}/"


def _raw_key(name: str, major: int, ext: str) -> str:
    return f"{_folder(name)}raw/{major}.{ext}"


def _data_key(name: str) -> str:
    return f"{_folder(name)}data.parquet"


def _check_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise DatasetError("dataset name is required")
    if "/" in name or "\\" in name or name.startswith("."):
        raise DatasetError("a dataset name can't contain / or \\ or start with a dot")
    return name


# --- catalogs: where a dataset's YAML document lives ----------------------------


class ObjectCatalog:
    """Local mode: `<name>/dataset.yaml`, beside the dataset's files."""

    def __init__(self, store: ObjectStore):
        self._store = store

    def names(self) -> list[str]:
        return [
            k[len(_ROOT):].removesuffix("/dataset.yaml")
            for k in self._store.list_keys()
            if k.startswith(_ROOT) and k.endswith("/dataset.yaml")
        ]

    def load(self, name: str) -> str | None:
        key = f"{_folder(name)}dataset.yaml"
        return self._store.get(key).decode("utf-8") if self._store.exists(key) else None

    def save(self, name: str, text: str) -> None:
        self._store.put(f"{_folder(name)}dataset.yaml", text.encode("utf-8"))

    def delete(self, name: str) -> None:
        self._store.delete(f"{_folder(name)}dataset.yaml")


class SqlCatalog:
    """Portal mode: a `datasets` table in the portal's database — sqlite (a file,
    or `:memory:` for tests) or Postgres (a `postgresql://` DSN; `psycopg` is
    imported only then)."""

    def __init__(self, dsn: str):
        self._pg = dsn.startswith(("postgres://", "postgresql://"))
        if self._pg:
            import psycopg

            self._conn = psycopg.connect(dsn, autocommit=True)
        else:
            if dsn != ":memory:":
                Path(dsn).parent.mkdir(parents=True, exist_ok=True)
            # Builds run on a worker thread; the lock keeps one statement at a time.
            self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._lock = threading.Lock()
        self._run("CREATE TABLE IF NOT EXISTS datasets (name TEXT PRIMARY KEY, doc TEXT NOT NULL)")

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._pg:
            sql = sql.replace("?", "%s")
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            if not self._pg:
                self._conn.commit()
            return rows

    def names(self) -> list[str]:
        return [r[0] for r in self._run("SELECT name FROM datasets")]

    def load(self, name: str) -> str | None:
        rows = self._run("SELECT doc FROM datasets WHERE name = ?", (name,))
        return rows[0][0] if rows else None

    def save(self, name: str, text: str) -> None:
        if self.load(name) is None:
            self._run("INSERT INTO datasets (name, doc) VALUES (?, ?)", (name, text))
        else:
            self._run("UPDATE datasets SET doc = ? WHERE name = ?", (text, name))

    def delete(self, name: str) -> None:
        self._run("DELETE FROM datasets WHERE name = ?", (name,))


# --- the store ---------------------------------------------------------------------


class DatasetStore:
    """Datasets over an `ObjectStore` (the files) and a catalog (their YAML).
    Pure logic — unit-tested with a `LocalObjectStore` in a tmp dir, no live S3."""

    def __init__(self, store: ObjectStore, catalog=None):
        self._store = store
        self._catalog = catalog or ObjectCatalog(store)
        self._migrate_unversioned()

    # --- reads ---------------------------------------------------------------

    def list(self) -> list[Dataset]:
        out = [self.get(n) for n in self._catalog.names()]
        return sorted((d for d in out if d), key=lambda d: d.name.lower())

    def get(self, name: str) -> Dataset | None:
        doc = self._load(name)
        if doc is None:
            return None
        current = _find(doc, doc.get("current"))
        building = next((v for v in doc["versions"] if v["status"] == "building"), None)
        return Dataset(
            name=doc["name"],
            description=doc.get("description", ""),
            delimiter=doc.get("delimiter", ","),
            columns=[Column(c["name"], c["dtype"]) for c in (current or {}).get("columns", []) if not c.get("service")],
            service=[Column(c["name"], c["dtype"]) for c in (current or {}).get("columns", []) if c.get("service")],
            rows=(current or {}).get("rows", 0),
            author=doc.get("author", ""),
            updated_at=_iso(doc.get("updated_at", 0)),
            version=doc.get("current") or "",
            building=_label(building) if building else "",
            error=doc.get("error", ""),
        )

    def versions(self, name: str) -> list[Version]:
        """Newest first, mappings included."""
        doc = self._require(name)
        return [_version(v) for v in sorted(doc["versions"], key=_order, reverse=True)]

    def version(self, name: str, label: str) -> Version | None:
        v = _find(self._require(name), label)
        return _version(v) if v else None

    def restored(self, name: str, label: str) -> str:
        """Version `label`'s mapping, fitted to the latest upload — what saving
        it again would build: its columns whose formulas still find what they
        read, then the upload's columns that are new since (as a re-upload
        carries a mapping forward). Going back is a new version, not a rewind:
        history only grows, so going back can be undone."""
        doc = self._require(name)
        old = _find(doc, label)
        if old is None:
            raise DatasetError(f"{name!r} has no version {label}")
        latest = _latest(doc)
        rules = column_map.parse(old["mapping"], old["sources"])
        return column_map.dump(column_map.default(latest["sources"], rules, old["sources"]),
                               column_map.permissions(old["mapping"]))

    # --- permissions -----------------------------------------------------------

    def permissions(self, name: str) -> dict[str, list[dict]]:
        """Role name -> its filters on dataset `name` (`[]`: full), for each
        role that may read it — the published version's (`column_map`)."""
        doc = self._require(name)
        return column_map.permissions((_find(doc, doc.get("current")) or _latest(doc))["mapping"])

    def grants(self, role: str) -> dict[str, list[dict]]:
        """Dataset -> `role`'s filters on it (`[]`: full), for each dataset it
        may read — what viewing a dashboard as `role` applies."""
        out = {}
        for name in self._catalog.names():
            try:
                perms = self.permissions(name)
            except DatasetError:
                continue
            if role in perms:
                out[name] = perms[role]
        return out

    def rows_seen(self, name: str, filters: list[dict]) -> int | None:
        """How many of `name`'s rows `filters` let through — what a role with
        them sees. None when one names a column the data lacks: viewing as that
        role locks the dataset rather than skip the filter (`Dashboard.as_role`)."""
        uri, options = self.source(name)
        lf = pl.scan_parquet(uri, storage_options=options)
        wanted = filters_mod.normalize(filters)
        if any(f.column not in lf.collect_schema().names() for f in wanted):
            return None
        preds = filters_mod.predicates(wanted, lf.collect_schema().names())
        return lf.filter(*preds).select(pl.len()).collect().item() if preds else lf.select(pl.len()).collect().item()

    def rename_role(self, old: str, new: str, author: str = "") -> None:
        """A role renamed: every dataset naming it names it anew — a new version
        of each one's mapping."""
        self._edit_permissions(lambda p: {new if r == old else r: fs for r, fs in p.items()}, author)

    def drop_role(self, role: str, author: str = "") -> None:
        """A role removed: no dataset names it any more."""
        self._edit_permissions(lambda p: {r: fs for r, fs in p.items() if r != role}, author)

    def _edit_permissions(self, change, author: str) -> None:
        for name in self._catalog.names():
            doc = self._load(name)
            if not doc or not doc.get("versions"):
                continue
            latest = _latest(doc)["mapping"]
            before = column_map.permissions(latest)
            after = change(before)
            if after != before:
                self.remap(name, column_map.with_permissions(latest, after), author=author)

    def mapping(self, name: str) -> str:
        """The YAML of the version being built, or else of the published one."""
        doc = self._require(name)
        return _latest(doc)["mapping"]

    def examples(self, name: str, n: int = 5) -> dict[str, list[str]]:
        """Up to `n` distinct values per column, from the data charts read (its
        first thousand rows) — computed columns included; shown beside each
        column. Before the first build, the upload's own columns."""
        doc = self._require(name)
        latest = _latest(doc)
        raw = _read(self._store.get(_raw_key(name, latest["major"], latest["raw"])),
                    doc.get("delimiter", ","), latest["raw"], rows=1000)
        if not self._store.exists(_data_key(name)):
            return column_map.examples(raw, n)
        uri, opts = self._store.source(_data_key(name))
        out = column_map.examples(pl.scan_parquet(uri, storage_options=opts).head(1000).collect(), n)
        # Service columns aren't in that file: worked out from the upload here.
        current = _find(doc, doc.get("current")) or latest
        service = [r for r in column_map.parse(current["mapping"], current["sources"]) if r.service]
        if service:
            try:
                out.update(column_map.examples(column_map.apply(column_map.as_text(raw), service), n))
            except column_map.MappingError:
                pass
        return out

    def source(self, name: str) -> tuple[str, dict | None]:
        """`(uri, storage_options)` for `pl.scan_parquet` — how charts read it."""
        if not self._store.exists(_data_key(name)):
            doc = self._load(name)
            if doc is not None:
                raise DatasetError(f"dataset {name!r} is still being built")
            raise DatasetError(f"unknown dataset {name!r}")
        return self._store.source(_data_key(name))

    # --- writes: each starts a version; `build` makes it real ----------------

    def create(
        self,
        name: str,
        csv_bytes: bytes,
        *,
        description: str = "",
        delimiter: str = ",",
        author: str = "",
        build: bool = True,
    ) -> Dataset:
        """A new dataset, version 1.0, every column as text. `build=False`
        leaves the build to the caller (the web app runs it in the background)."""
        name = _check_name(name)
        if self._load(name) is not None:
            raise DatasetError(f"a dataset named {name!r} already exists")
        sources = _read(csv_bytes, delimiter, "csv").columns
        now = _now()
        doc = {
            "name": name, "description": description, "delimiter": delimiter,
            "author": author, "created_at": now, "updated_at": now,
            "current": None, "error": "", "versions": [],
        }
        self._store.put(_raw_key(name, 1, "csv"), csv_bytes)
        doc["versions"].append(_new_version(1, 0, sources, column_map.default(sources), author, "csv"))
        self._save(doc)
        return self._finish(name, build)

    def replace(
        self,
        name: str,
        csv_bytes: bytes,
        *,
        description: str | None = None,
        delimiter: str = ",",
        author: str = "",
        build: bool = True,
    ) -> Dataset:
        """A new upload: the next major, its mapping carried forward from the
        latest one. `description=None` keeps the current one."""
        doc = self._require(name)
        sources = _read(csv_bytes, delimiter, "csv").columns
        latest = _latest(doc)
        previous = column_map.parse(latest["mapping"], latest["sources"])
        rules = column_map.default(sources, previous, latest["sources"])
        perms = column_map.permissions(latest["mapping"])       # who may read it carries over too
        major = max(v["major"] for v in doc["versions"]) + 1
        self._store.put(_raw_key(name, major, "csv"), csv_bytes)
        _drop_building(doc)
        minor = _next_minor(doc, column_map.dump(rules, perms))
        doc["versions"].append(_new_version(major, minor, sources, rules, author, "csv", perms))
        doc["delimiter"] = delimiter
        doc["error"] = ""                        # a new try: the last failure is history
        if description is not None:
            doc["description"] = description
        self._save(doc)
        return self._finish(name, build)

    def remap(self, name: str, mapping: str, *, author: str = "", build: bool = True) -> Dataset:
        """A new column mapping: the next minor (the dataset's one counter), on
        the latest major. Raises `DatasetError` when the YAML doesn't read or
        doesn't fit the upload."""
        doc = self._require(name)
        latest = _latest(doc)
        try:
            rules = column_map.parse(mapping, latest["sources"])
            perms = column_map.permissions(mapping)
        except column_map.MappingError as exc:
            raise DatasetError(str(exc)) from exc
        text = column_map.dump(rules, perms)
        if text == latest["mapping"]:
            raise DatasetError("the mapping hasn't changed")
        if latest["status"] == "building":
            # Not built yet: this edit replaces it. Its number follows the same
            # rule against what's built — and an edit back to exactly that is
            # nothing to build at all.
            doc["versions"].remove(latest)
            base = _latest(doc) if doc["versions"] else None
            if base is not None and base["major"] == latest["major"] and base["mapping"] == text:
                self._save(doc)
                return self.get(name)
            minor = _next_minor(doc, text) if doc["versions"] else latest["minor"]
            doc["versions"].append(_new_version(latest["major"], minor, latest["sources"],
                                                rules, author, latest["raw"], perms))
        else:
            minor = max(v["minor"] for v in doc["versions"]) + 1
            doc["versions"].append(
                _new_version(latest["major"], minor, latest["sources"], rules, author, latest["raw"], perms)
            )
        doc["error"] = ""
        self._save(doc)
        return self._finish(name, build)

    def build(self, name: str) -> dict[str, str] | None:
        """Compute the version waiting to be built, if any. On success it's
        published — the Parquet replaced, the version current — and the column
        renames it made are returned (old -> new), for the dashboards that need
        rewriting. On failure it's dropped, the previous version stays, and the
        error is recorded on the dataset. None when there was nothing to build."""
        doc = self._load(name)
        pending = next((v for v in (doc or {}).get("versions", []) if v["status"] == "building"), None)
        if pending is None:
            return None
        before = _find(doc, doc.get("current"))
        try:
            raw = _read(self._store.get(_raw_key(name, pending["major"], pending["raw"])),
                        doc.get("delimiter", ","), pending["raw"])
            rules = column_map.parse(pending["mapping"], pending["sources"])
            out = column_map.apply(column_map.as_text(raw), rules)
        except (DatasetError, column_map.MappingError) as exc:
            return self._fail(name, pending, str(exc))
        # Service columns are built (a formula can fail on them too) and kept in
        # the schema, but left out of the file charts read.
        service = [r.name for r in rules if r.service]
        buf = io.BytesIO()
        out.drop(service).write_parquet(buf)
        # Re-read: a newer edit may have replaced this version while it built.
        doc = self._load(name)
        mine = _find(doc, _label(pending))
        if mine is None or mine["status"] != "building":
            return None
        self._store.put(_data_key(name), buf.getvalue())
        mine.update(status="ready", rows=out.height, columns=[
            {"name": n, "dtype": str(t), **({"service": True} if n in service else {})}
            for n, t in out.schema.items()
        ])
        doc.update(current=_label(mine), error="", updated_at=_now())
        self._save(doc)
        if before is None:
            return {}
        return column_map.renames(column_map.parse(before["mapping"], before["sources"]), rules)

    def rename(self, old: str, new: str, description: str | None = None) -> Dataset:
        """Edit a dataset's name and/or description. `description=None` keeps the
        current one."""
        doc = self._require(old)
        new = _check_name(new)
        if new != old and self._load(new) is not None:
            raise DatasetError(f"a dataset named {new!r} already exists")
        if description is not None:
            doc["description"] = description
        doc["updated_at"] = _now()
        if new != old:
            for key in self._keys(old):
                self._store.put(_folder(new) + key[len(_folder(old)):], self._store.get(key))
                self._store.delete(key)
            self._catalog.delete(old)
            doc["name"] = new
        self._save(doc)
        return self.get(new)

    def delete_major(self, name: str, major: int) -> None:
        """Drop an upload — its original file and every version made from it.
        Never the one in use, nor one being built."""
        doc = self._require(name)
        latest = _latest(doc)
        current = _find(doc, doc.get("current"))
        if major in (latest["major"], (current or {}).get("major")):
            raise DatasetError(f"version {major} is the one in use; upload a newer file first")
        gone = [v for v in doc["versions"] if v["major"] == major]
        if not gone:
            raise DatasetError(f"{name!r} has no version {major}")
        self._store.delete(_raw_key(name, major, gone[0]["raw"]))
        doc["versions"] = [v for v in doc["versions"] if v["major"] != major]
        self._save(doc)

    def delete(self, name: str) -> None:
        for key in self._keys(name):
            self._store.delete(key)
        self._catalog.delete(name)

    # --- internals -----------------------------------------------------------

    def _finish(self, name: str, build: bool) -> Dataset:
        if build:
            self.build(name)
            error = self.get(name).error
            if error:
                raise DatasetError(error)
        return self.get(name)

    def _fail(self, name: str, pending: dict, error: str) -> None:
        doc = self._load(name)
        doc["versions"] = [v for v in doc["versions"] if _label(v) != _label(pending)]
        doc["error"] = f"{_label(pending)}: {error}"
        if not doc["versions"]:                  # a first upload that never built
            self.delete(name)
            return None
        self._save(doc)
        return None

    def _keys(self, name: str) -> list[str]:
        return [k for k in self._store.list_keys() if k.startswith(_folder(name))]

    def _load(self, name: str) -> dict | None:
        text = self._catalog.load(name)
        if not text:
            return None
        doc = yaml.safe_load(text)
        # Mappings saved under the earlier, wider set of types read as today's.
        for v in doc.get("versions", []):
            v["mapping"] = column_map.upgrade(v.get("mapping", ""))
        # Permissions kept beside the versions, before they were part of the
        # mapping: into the published and the latest mapping, as they stood.
        loose = doc.pop("permissions", None)
        if loose is not None and doc.get("versions"):
            grants = {r: [] if spec == FULL else list(spec) for r, spec in loose.items()}
            for v in {id(v): v for v in (_find(doc, doc.get("current")), _latest(doc)) if v}.values():
                v["mapping"] = column_map.with_permissions(v["mapping"], grants)
            self._save(doc)
        return doc

    def _require(self, name: str) -> dict:
        doc = self._load(name)
        if doc is None:
            raise DatasetError(f"unknown dataset {name!r}")
        return doc

    def _save(self, doc: dict) -> None:
        self._catalog.save(doc["name"], _dump(doc))

    def _migrate_unversioned(self) -> None:
        """Datasets from before versions — `<name>.parquet` + `<name>.yaml` with
        guessed types — become version 1.0, their file kept as the original and
        their types as the mapping, so dashboards read them exactly as before."""
        keys = set(self._store.list_keys())
        for meta_key in [k for k in keys if "/" not in k and k.endswith(".yaml")]:
            name = meta_key.removesuffix(".yaml")
            if f"{name}.parquet" not in keys or self._catalog.load(name) is not None:
                continue
            old = yaml.safe_load(self._store.get(meta_key).decode("utf-8"))
            data = self._store.get(f"{name}.parquet")
            columns = [(c["name"], c["dtype"]) for c in old.get("columns", [])]
            version = _new_version(1, 0, [c for c, _ in columns], column_map.from_dtypes(columns),
                                   old.get("author", ""), "parquet")
            version.update(status="ready", rows=old.get("rows", 0),
                           columns=[{"name": c, "dtype": t} for c, t in columns])
            updated = _unix(old.get("updated_at", ""))
            self._store.put(_raw_key(name, 1, "parquet"), data)
            self._store.put(_data_key(name), data)
            self._save({
                "name": name, "description": old.get("description", ""),
                "delimiter": old.get("delimiter", ","), "author": old.get("author", ""),
                "created_at": updated, "updated_at": updated,
                "current": "1.0", "error": "", "versions": [version],
            })
            self._store.delete(f"{name}.parquet")
            self._store.delete(meta_key)


# --- version records (plain dicts in the YAML document) -----------------------------


def _new_version(major: int, minor: int, sources: list[str], rules, author: str, raw: str,
                 permissions: dict | None = None) -> dict:
    return {
        "major": major, "minor": minor, "status": "building",
        "created_at": _now(), "author": author, "rows": 0,
        "raw": raw, "sources": list(sources), "columns": [],
        "mapping": column_map.dump(rules, permissions),
    }


def _label(v: dict) -> str:
    return f"{v['major']}.{v['minor']}"


def _order(v: dict) -> tuple[int, int]:
    return v["major"], v["minor"]


def _find(doc: dict, label: str | None) -> dict | None:
    return next((v for v in doc["versions"] if _label(v) == label), None) if label else None


def _latest(doc: dict) -> dict:
    return max(doc["versions"], key=_order)


def _next_minor(doc: dict, mapping: str) -> int:
    """The minor for a new version with `mapping`: the latest version's when the
    mapping is the same (an upload that changed nothing in it), else the next
    of the dataset's one counter."""
    latest = _latest(doc)
    if latest["mapping"] == mapping:
        return latest["minor"]
    return max(v["minor"] for v in doc["versions"]) + 1


def _drop_building(doc: dict) -> None:
    """A new upload supersedes a version still waiting to be built."""
    doc["versions"] = [v for v in doc["versions"] if v["status"] != "building"]


def _version(v: dict) -> Version:
    return Version(v["major"], v["minor"], v["created_at"], v.get("author", ""), v["status"],
                   v.get("rows", 0), v["mapping"], list(v["sources"]))


def _read(data: bytes, delimiter: str, kind: str, rows: int | None = None) -> pl.DataFrame:
    """An original file — every CSV column as text; the mapping types them."""
    try:
        if kind == "parquet":
            return pl.read_parquet(io.BytesIO(data), n_rows=rows)
        return pl.read_csv(io.BytesIO(data), separator=delimiter, infer_schema=False, n_rows=rows)
    except Exception as exc:
        raise DatasetError(f"could not read CSV: {exc}") from exc


def _unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (TypeError, ValueError):
        return _now()


def _dump(doc: dict) -> str:
    # Mappings are YAML themselves: kept as block text so the document reads well.
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True)


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper, text: str):
    style = "|" if "\n" in text else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)


_Dumper.add_representer(str, _str)
