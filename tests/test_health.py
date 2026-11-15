"""The health probes: `/healthz` (the process answers) and `/ready` (the
portal's database and dataset store answer) — open without a session, bounded
in time, and silent about why."""

import threading
import time

import pytest
from starlette.testclient import TestClient

from fireflyer.datasets import DatasetStore
from fireflyer.storage import LocalObjectStore
from fireflyer.web import app as app_mod
from fireflyer.web.auth import PasswordAuthenticator


@pytest.fixture
def client(app_state, tmp_path):
    app_state("authenticator", PasswordAuthenticator("admin", "password"))
    app_state("datasets", DatasetStore(LocalObjectStore(str(tmp_path / "objects"))))
    return TestClient(app_mod.app)


@pytest.fixture
def database(monkeypatch):
    """A portal database the probe asks — `pg.check` faked; what it raises."""
    asked, failure = [], []
    monkeypatch.setattr(app_mod, "_DATABASE_URL", "postgresql://ff@db/ff?password=pw")

    def check(dsn):
        asked.append(dsn)
        if failure:
            raise failure[0]
    monkeypatch.setattr(app_mod.pg, "check", check)
    return asked, failure


def test_probes_are_open_but_the_portal_still_needs_a_login(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ok"}
    for path in ("/", "/ready/more", "/healthz/x"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_readiness_asks_the_portal_database(client, database):
    asked, failure = database
    assert client.get("/ready").status_code == 200 and asked == ["postgresql://ff@db/ff?password=pw"]
    failure.append(OSError("connection refused"))
    assert client.get("/ready").status_code == 503
    assert client.get("/healthz").status_code == 200                # liveness doesn't ask it


def test_missing_storage_only_affects_readiness(client, tmp_path):
    (tmp_path / "objects").rmdir()
    assert client.get("/ready").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_a_slow_backend_meets_the_deadline_and_blocks_nothing(client, monkeypatch):
    release, started = threading.Event(), threading.Event()

    def blocked():
        started.set()
        release.wait(10)

    monkeypatch.setattr(app_mod, "_READY_SECONDS", 0.5)
    monkeypatch.setattr(app_mod, "_check_backends", blocked)
    try:
        before = time.monotonic()
        assert client.get("/ready").status_code == 503
        assert started.is_set() and time.monotonic() - before < 2
        assert client.get("/healthz").status_code == 200
    finally:
        release.set()


def test_a_failure_says_nothing_of_why(client, database):
    database[1].append(RuntimeError("postgresql://user:super-secret@private-host/db"))
    response = client.get("/ready")
    assert response.status_code == 503 and response.json() == {"status": "unavailable"}


def test_local_mode_has_no_database_to_ask(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_DATABASE_URL", "")
    monkeypatch.setattr(app_mod.pg, "check", lambda dsn: pytest.fail("asked a database"))
    assert client.get("/ready").status_code == 200
