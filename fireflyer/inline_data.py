"""Inline datasets — CSV written straight into the dashboard YAML.

    datasets:
      order_data: |
        id,status,amount
        1,paid,42
        2,pending,15

The point is prototyping without leaving the dashboard: you (or the assistant)
can invent a table, chart it, and iterate, with no upload step and nothing to
clean up afterwards. A managed dataset is still the right home for real data —
inline is for the small, throwaway, self-contained case, and a dashboard that
carries its own data is a single file you can paste to someone.

The first line is the header. Everything else is ordinary CSV.

Charts read Parquet, so each block is converted once and cached **by content
checksum**: the same CSV always maps to the same file, an edit maps to a new
one, and re-rendering a dashboard re-uses what's already there instead of
re-parsing on every keystroke. The cache lives in the system temp dir — it is
derived data, safe to delete at any time, and rebuilt on demand.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path

import polars as pl


class InlineDataError(ValueError):
    """Raised for a malformed `datasets:` block — message is shown to the user."""


CACHE_DIR = Path(tempfile.gettempdir()) / "fireflyer-inline"


def parse_block(raw) -> dict[str, str]:
    """The top-level `datasets:` block -> `{name: csv text}`. Absent -> `{}`."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InlineDataError(
            "`datasets` must be a mapping of name -> CSV text, e.g.\n"
            "  datasets:\n    orders: |\n      id,amount\n      1,42"
        )
    out: dict[str, str] = {}
    for name, text in raw.items():
        if not isinstance(text, str) or not text.strip():
            raise InlineDataError(
                f"dataset {str(name)!r}: must be CSV text (use `|` for a block "
                "so line breaks are kept)"
            )
        # The block takes CSV and nothing else — no paths, no URIs. A single
        # line is either a mistaken path or a header with no rows, and both
        # would otherwise parse as a valid one-column, zero-row table and fail
        # much later with a baffling message about a missing column.
        if "\n" not in text.strip():
            raise InlineDataError(
                f"dataset {str(name)!r}: `datasets:` holds inline CSV, not a "
                "path — it needs a header line and at least one row:\n"
                "  datasets:\n    orders: |\n      id,amount\n      1,42\n"
                "To read a file, upload it under Datasets and reference it by "
                "name instead."
            )
        out[str(name)] = text
    return out


def _digest(csv_text: str) -> str:
    return hashlib.sha256(csv_text.encode()).hexdigest()[:16]


def materialize(name: str, csv_text: str) -> str:
    """Path to the Parquet for one inline dataset, converting it if needed.

    Keyed by the CSV's checksum, so an unchanged block costs a `stat` and an
    edited one lands on a fresh path. The write goes to a temp file and is then
    renamed: a rename is atomic, so two workers converting the same block at
    once can't leave a half-written Parquet for a reader to trip over.
    """
    target = CACHE_DIR / f"{_digest(csv_text)}.parquet"
    if target.exists():
        return str(target)

    try:
        frame = pl.read_csv(io.StringIO(csv_text))
    except Exception as exc:                      # polars raises several types
        raise InlineDataError(f"dataset {name!r}: {exc}") from exc
    if frame.width == 0:
        raise InlineDataError(f"dataset {name!r}: no columns — is the header row there?")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    frame.write_parquet(tmp)
    os.replace(tmp, target)
    return str(target)


def resolver(inline: dict[str, str], base=None):
    """A `name -> (uri, storage_options)` resolver that consults `inline` first.

    A dashboard's own block wins over a managed dataset of the same name: it is
    part of the file being rendered, so reading someone else's stored data
    instead would be a surprise. Anything it doesn't define falls through to
    `base`, and with no `base` the name is treated as a path — the standalone
    behaviour. Returns `base` unchanged when there is nothing inline, so the
    common case adds no indirection.
    """
    if not inline:
        return base

    def resolve(name: str):
        if name in inline:
            return materialize(name, inline[name]), None
        return base(name) if base is not None else (name, None)

    return resolve
