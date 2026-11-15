"""How `python -m fireflyer.portal` starts: one process unless told to reload,
the proxy's headers trusted where told, and never on the public defaults."""

import pytest

from fireflyer import portal
from fireflyer.web.auth import check_portal_env


@pytest.fixture
def startup(monkeypatch):
    for key in ("FIREFLYER_RELOAD", "FIREFLYER_PORT", "FIREFLYER_FORWARDED_ALLOW_IPS", "FIREFLYER_HSTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FIREFLYER_TLS", "off")
    monkeypatch.setenv("DATABASE_URL", "postgresql://ff@db/ff")
    monkeypatch.setenv("FIREFLYER_PASSWORD", "a-strong-one")
    monkeypatch.setenv("FIREFLYER_SECRET", "random")
    monkeypatch.setattr(portal, "_load_config", lambda: {})
    calls = []
    monkeypatch.setattr(portal.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    return calls


def test_the_portal_is_one_process_without_a_reloader(startup):
    portal.main()
    [run] = startup
    assert (run["host"], run["port"], run["reload"], run["reload_includes"]) == ("0.0.0.0", 8000, False, None)
    assert run["ssl_certfile"] is None and run["forwarded_allow_ips"] == "127.0.0.1"


def test_reload_port_and_the_proxy_are_settings(startup, monkeypatch):
    monkeypatch.setenv("FIREFLYER_RELOAD", "1")
    monkeypatch.setenv("FIREFLYER_PORT", "8080")
    monkeypatch.setenv("FIREFLYER_FORWARDED_ALLOW_IPS", "10.0.0.1")
    portal.main()
    [run] = startup
    assert run["reload"] is True and "*.html" in run["reload_includes"]
    assert run["port"] == 8080 and run["forwarded_allow_ips"] == "10.0.0.1"


@pytest.mark.parametrize("unset", ["FIREFLYER_PASSWORD", "FIREFLYER_SECRET"])
def test_the_portal_wont_start_on_the_public_defaults(startup, monkeypatch, unset):
    monkeypatch.delenv(unset)
    with pytest.raises(SystemExit, match=unset):
        portal.main()
    assert startup == []


def test_a_set_value_is_a_choice_even_admin():
    check_portal_env({"FIREFLYER_PASSWORD": "admin", "FIREFLYER_SECRET": "dev"})
    with pytest.raises(SystemExit, match="FIREFLYER_PASSWORD and FIREFLYER_SECRET"):
        check_portal_env({"FIREFLYER_PASSWORD": "", "FIREFLYER_SECRET": ""})
