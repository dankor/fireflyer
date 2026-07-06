from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"


@pytest.fixture
def orders_csv() -> str:
    return str(DATA_DIR / "orders.csv")


@pytest.fixture
def snapshot(request):
    """Compare a string against tests/snapshots/<test_name>.html.

    Set UPDATE_SNAPSHOTS=1 to regenerate.
    """
    import os

    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = SNAPSHOTS_DIR / f"{request.node.name}.html"

    def check(actual: str) -> None:
        if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
            path.write_text(actual)
            return
        expected = path.read_text()
        assert actual == expected, f"snapshot mismatch ({path})"

    return check
