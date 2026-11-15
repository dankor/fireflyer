"""Usage analytics, shipped with the portal: a connection to Fireflyer's own
database (`internal`), datasets over it and a **Fireflyer Usage** dashboard on
them, in the admin's group.

What ships is files beside this one, read once at import:

- `datasets/<name>.yaml` — one dataset each: its `description:` and its
  `query:`;
- `dashboard.yaml` — the dashboard on them.

They're made **once**, when the portal starts and the admin's group has none of
the datasets (`app._set_up_usage`). After that they're ordinary: an admin
edits or removes the connection, edits the datasets' SQL and refreshes them by
hand, and an upgrade never changes them.

The SQL gives usernames, never other personal data: no email or names, no IP
address or browser, no password hash or session key. `dataset_versions` is the
table a connection to Fireflyer's own database adds, parsed from the datasets'
documents.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_HERE = Path(__file__).parent

CONNECTION = "internal"
CONNECTION_DESCRIPTION = "Fireflyer's own database: who uses what"


def connection_url(database_url: str) -> str:
    """The portal's `DATABASE_URL` — a `postgresql://` DSN, as psycopg reads
    it — as a SQLAlchemy URL, which names its driver: psycopg, the one the
    portal has."""
    from sqlalchemy.engine import make_url

    return make_url(database_url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _dataset(path: Path) -> tuple[str, str]:
    """`(description, SQL)` from a dataset's YAML file."""
    doc = yaml.safe_load(path.read_text())
    return doc["description"], doc["query"].strip()


# name -> (description, SQL)
DATASETS: dict[str, tuple[str, str]] = {
    p.stem: _dataset(p) for p in sorted((_HERE / "datasets").glob("*.yaml"))
}

# The dashboard on them. Refreshing a dataset (its page's ↻) brings it up to date.
DASHBOARD = (_HERE / "dashboard.yaml").read_text()
DASHBOARD_NAME = yaml.safe_load(DASHBOARD)["name"]
