"""How `python -m fireflyer.portal` starts: one process unless told to reload,
the proxy's headers trusted where told, and never on the public defaults."""

import pytest

from fireflyer import portal
from fireflyer.web.auth import check_portal_env


@pytest.fixture
def startup(monkeypatch):
    for key in ("FIREFLYER_RELOAD", "FIREFLYER_PORT", "FIREFLYER_LISTEN_PORT",
                "FIREFLYER_FORWARDED_ALLOW_IPS", "FIREFLYER_HSTS"):
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
    monkeypatch.setenv("FIREFLYER_LISTEN_PORT", "8080")
    monkeypatch.setenv("FIREFLYER_FORWARDED_ALLOW_IPS", "10.0.0.1")
    portal.main()
    [run] = startup
    assert run["reload"] is True and "*.html" in run["reload_includes"]
    assert run["port"] == 8080 and run["forwarded_allow_ips"] == "10.0.0.1"


# Kubernetes sets `<SERVICE>_PORT=tcp://<ip>:<port>` for a Service, so one named
# `fireflyer` collides with FIREFLYER_PORT. The Service's port is not the port
# to listen on.
K8S_SERVICE = "tcp://10.0.0.10:8000"


@pytest.mark.parametrize("env, port", [
    ({}, 8000),
    ({"FIREFLYER_PORT": "8080"}, 8080),
    ({"FIREFLYER_PORT": K8S_SERVICE}, 8000),
    ({"FIREFLYER_PORT": "tcp://10.0.0.10:9000"}, 8000),
    ({"FIREFLYER_PORT": K8S_SERVICE, "FIREFLYER_LISTEN_PORT": "9090"}, 9090),
    ({"FIREFLYER_PORT": "8080", "FIREFLYER_LISTEN_PORT": "9090"}, 9090),
])
def test_the_listening_port(env, port):
    assert portal.listen_port(env) == port


def test_an_injected_service_url_does_not_stop_the_start(startup, monkeypatch):
    monkeypatch.setenv("FIREFLYER_PORT", K8S_SERVICE)
    portal.main()
    [run] = startup
    assert run["port"] == 8000


@pytest.mark.parametrize("env", [
    {"FIREFLYER_LISTEN_PORT": "http"},
    {"FIREFLYER_LISTEN_PORT": "0"},
    {"FIREFLYER_LISTEN_PORT": "70000"},
    {"FIREFLYER_PORT": "eighty"},
    {"FIREFLYER_PORT": "http://10.0.0.10:8000"},
])
def test_a_wrong_port_is_named(env):
    [name] = env
    with pytest.raises(SystemExit, match=name):
        portal.listen_port(env)


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
