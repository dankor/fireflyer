import re
from pathlib import Path
from urllib.parse import quote_plus

import pytest

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"

# The dataset path is absolute (from `orders_csv`), so it embeds the machine's
# checkout location into rendered HTML — raw in dashboard YAML, URL-encoded in
# table/map htmx query strings, and (because the table/map DOM id is a SHA-1 of
# the chart config, path included) in those ids too. Normalize all three forms
# to stable tokens so snapshots are portable across machines (local macOS vs
# Linux CI); the production output is unchanged.
_DATA_TOKEN = "<TEST_DATA>"
_DATA_ABS = str(DATA_DIR)
_CHART_ID_RE = re.compile(r"(fireflyer-(?:table|map)-)[0-9a-f]{10}")


def _portable(text: str) -> str:
    text = text.replace(_DATA_ABS, _DATA_TOKEN).replace(
        quote_plus(_DATA_ABS), _DATA_TOKEN
    )
    return _CHART_ID_RE.sub(r"\1<ID>", text)


@pytest.fixture
def orders_csv() -> str:
    return str(DATA_DIR / "orders.csv")


@pytest.fixture
def orders_parquet() -> str:
    """Datasets are Parquet now; charts scan this. Path lives under DATA_DIR so
    the snapshot fixture normalizes it (and the SHA chart ids) the same way."""
    return str(DATA_DIR / "orders.parquet")


@pytest.fixture
def csv_to_parquet(tmp_path):
    """Write ad-hoc CSV text as Parquet (charts scan Parquet) and return the
    path. For tests that build their own data and assert on structure, not a
    snapshot."""
    import io

    import polars as pl

    def make(csv_text: str, name: str = "d") -> str:
        path = tmp_path / f"{name}.parquet"
        pl.read_csv(io.BytesIO(csv_text.encode())).write_parquet(path)
        return str(path)

    return make


@pytest.fixture
def snapshot(request):
    """Compare a string against tests/snapshots/<test_name>.html.

    Set UPDATE_SNAPSHOTS=1 to regenerate.
    """
    import os

    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = SNAPSHOTS_DIR / f"{request.node.name}.html"

    def check(actual: str) -> None:
        actual = _portable(actual)
        if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
            path.write_text(actual)
            return
        expected = path.read_text()
        assert actual == expected, f"snapshot mismatch ({path})"

    return check
