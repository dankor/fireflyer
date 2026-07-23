"""Datasets — named CSV uploads converted to Parquet and kept in object storage.

A dataset is keyed by a **unique name** (no id): charts reference it by that name
and, per project preference, a rename cascades to dashboards while a delete is
guarded when dashboards still use it (both handled a layer up, in the web store).
Here we own only the dataset itself: convert the upload to Parquet, record its
schema/rows/description, and hand charts an efficient `(uri, storage_options)`
Parquet source. Metadata is a small JSON sidecar next to the Parquet in the same
object store, so local (folder) and portal (S3) share one layout.
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import polars as pl
import yaml

from fireflyer.storage import ObjectStore


class DatasetError(Exception):
    """Bad dataset input (unknown name, duplicate, unreadable CSV)."""


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parquet_key(name: str) -> str:
    return f"{name}.parquet"


def _meta_key(name: str) -> str:
    return f"{name}.yaml"


def _read_csv(csv_bytes: bytes, delimiter: str) -> pl.DataFrame:
    try:
        return pl.read_csv(io.BytesIO(csv_bytes), separator=delimiter)
    except Exception as exc:
        raise DatasetError(f"could not read CSV: {exc}") from exc


class DatasetStore:
    """Datasets over an `ObjectStore`: Parquet + a JSON metadata sidecar per
    dataset. Pure logic — unit-tested with a `LocalObjectStore` in a tmp dir, no
    live S3."""

    def __init__(self, store: ObjectStore):
        self._store = store

    # --- reads ---------------------------------------------------------------

    def list(self) -> list[Dataset]:
        out = []
        for key in self._store.list_keys():
            if key.endswith(".yaml"):
                out.append(self._load_meta(key))
        return sorted(out, key=lambda d: d.name.lower())

    def get(self, name: str) -> Dataset | None:
        if not self._store.exists(_meta_key(name)):
            return None
        return self._load_meta(_meta_key(name))

    def preview(self, name: str, n: int = 20) -> tuple[list[str], list[list]]:
        """First `n` rows for the UI preview. Reads only `n` rows (Parquet)."""
        uri, opts = self._store.source(_parquet_key(name))
        df = pl.scan_parquet(uri, storage_options=opts).head(n).collect()
        return df.columns, [list(r) for r in df.rows()]

    def source(self, name: str) -> tuple[str, dict | None]:
        """`(uri, storage_options)` for `pl.scan_parquet` — how charts read it."""
        if not self._store.exists(_parquet_key(name)):
            raise DatasetError(f"unknown dataset {name!r}")
        return self._store.source(_parquet_key(name))

    # --- writes --------------------------------------------------------------

    def create(
        self,
        name: str,
        csv_bytes: bytes,
        *,
        description: str = "",
        delimiter: str = ",",
        author: str = "",
    ) -> Dataset:
        name = name.strip()
        if not name:
            raise DatasetError("dataset name is required")
        if self.get(name) is not None:
            raise DatasetError(f"a dataset named {name!r} already exists")
        return self._write(name, csv_bytes, description, delimiter, author)

    def replace(
        self,
        name: str,
        csv_bytes: bytes,
        *,
        description: str | None = None,
        delimiter: str = ",",
    ) -> Dataset:
        """Re-upload the CSV for an existing dataset (edit). Keeps the author;
        `description=None` keeps the current one."""
        current = self.get(name)
        if current is None:
            raise DatasetError(f"unknown dataset {name!r}")
        desc = current.description if description is None else description
        return self._write(name, csv_bytes, desc, delimiter, current.author)

    def rename(self, old: str, new: str, description: str | None = None) -> Dataset:
        """Edit a dataset's name and/or description (the Parquet is untouched).
        `description=None` keeps the current one."""
        new = new.strip()
        meta = self.get(old)
        if meta is None:
            raise DatasetError(f"unknown dataset {old!r}")
        if not new:
            raise DatasetError("new name is required")
        if new != old and self.get(new) is not None:
            raise DatasetError(f"a dataset named {new!r} already exists")
        if description is not None:
            meta.description = description
        meta.name = new
        meta.updated_at = _now()
        if new != old:
            # Move the Parquet under the new name.
            self._store.put(_parquet_key(new), self._store.get(_parquet_key(old)))
            self._store.put(_meta_key(new), _dump(meta))
            self._store.delete(_parquet_key(old))
            self._store.delete(_meta_key(old))
        else:
            self._store.put(_meta_key(new), _dump(meta))
        return meta

    def delete(self, name: str) -> None:
        self._store.delete(_parquet_key(name))
        self._store.delete(_meta_key(name))

    # --- internals -----------------------------------------------------------

    def _write(
        self, name: str, csv_bytes: bytes, description: str, delimiter: str, author: str
    ) -> Dataset:
        df = _read_csv(csv_bytes, delimiter)
        buf = io.BytesIO()
        df.write_parquet(buf)
        self._store.put(_parquet_key(name), buf.getvalue())
        meta = Dataset(
            name=name,
            description=description,
            delimiter=delimiter,
            columns=[Column(n, str(t)) for n, t in df.schema.items()],
            rows=df.height,
            author=author,
            updated_at=_now(),
        )
        self._store.put(_meta_key(name), _dump(meta))
        return meta

    def _load_meta(self, key: str) -> Dataset:
        raw = yaml.safe_load(self._store.get(key).decode("utf-8"))
        raw["columns"] = [Column(**c) for c in raw["columns"]]
        return Dataset(**raw)


def _dump(meta: Dataset) -> bytes:
    # The dataset entity is stored as YAML (like dashboards), not JSON.
    return yaml.safe_dump(asdict(meta), sort_keys=False).encode("utf-8")
