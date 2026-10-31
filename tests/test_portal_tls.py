"""HTTPS for portal mode (`fireflyer.portal`): a team's own certificate, a
self-signed one made for local development, or off behind a proxy — and the
session cookie Secure over HTTPS, HSTS only with a real certificate."""

import datetime as dt
import stat

import pytest
from starlette.testclient import TestClient

from fireflyer import portal
from fireflyer.web import app as app_mod
from fireflyer.web import auth


def _x509():
    """`cryptography` makes the local certificate (the `.[portal]` and `.[test]`
    extras); without it, only the certificate tests are skipped."""
    return pytest.importorskip("cryptography.x509")


def _names(cert_path):
    x509 = _x509()
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(n.value) for n in sans}, cert


def test_auto_makes_a_local_certificate_once_and_keeps_it(tmp_path):
    _x509()
    tls = portal.tls_settings({}, {"FIREFLYER_TLS_DIR": str(tmp_path)})
    assert tls["mode"] == "auto"
    names, cert = _names(tmp_path / "local-cert.pem")
    assert names == {"localhost", "127.0.0.1", "::1"}
    assert cert.not_valid_after_utc - cert.not_valid_before_utc <= dt.timedelta(days=398)   # browsers' cap
    assert stat.S_IMODE((tmp_path / "local-key.pem").stat().st_mode) == 0o600
    again = portal.tls_settings({}, {"FIREFLYER_TLS_DIR": str(tmp_path)})
    assert _names(tmp_path / "local-cert.pem")[1].serial_number == cert.serial_number and again == tls


def test_auto_remakes_it_for_new_hosts(tmp_path):
    _x509()
    portal.tls_settings({}, {"FIREFLYER_TLS_DIR": str(tmp_path)})
    portal.tls_settings({"tls": {"hosts": ["localhost", "dash.example.com", "10.0.0.5"]}},
                        {"FIREFLYER_TLS_DIR": str(tmp_path)})
    assert _names(tmp_path / "local-cert.pem")[0] == {"localhost", "dash.example.com", "10.0.0.5"}


def test_a_teams_certificate_wins_and_off_is_plain_http(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x"), key.write_text("y")
    cfg = {"tls": {"mode": "auto", "cert": str(cert), "key": str(key)}}
    assert portal.tls_settings(cfg, {}) == {"mode": "cert", "cert": str(cert), "key": str(key)}
    assert portal.tls_settings(cfg, {"FIREFLYER_TLS": "off"}) == {"mode": "off"}
    for bad in ({"FIREFLYER_TLS_CERT": str(cert)},                       # no key
                {"FIREFLYER_TLS_CERT": str(cert), "FIREFLYER_TLS_KEY": str(tmp_path / "none")},
                {"FIREFLYER_TLS": "maybe"}):
        with pytest.raises(SystemExit):
            portal.tls_settings({}, {**bad, "FIREFLYER_TLS_DIR": str(tmp_path)})


@pytest.fixture
def login(monkeypatch, app_state):
    app_state("authenticator", auth.PasswordAuthenticator("admin", "admin-pass"))
    monkeypatch.setattr(auth, "admin_name", lambda: "admin")
    return lambda base: TestClient(app_mod.app, base_url=base).post(
        "/login", data={"username": "admin", "password": "admin-pass"}, follow_redirects=False)


def test_the_session_cookie_is_secure_over_https_only(login):
    assert "Secure" in login("https://testserver").headers["set-cookie"]
    plain = login("http://testserver").headers["set-cookie"]
    assert "Secure" not in plain and "HttpOnly" in plain and "SameSite=lax" in plain


def test_hsts_only_when_asked_and_over_https(monkeypatch):
    client = TestClient(app_mod.app, base_url="https://testserver")
    assert "strict-transport-security" not in client.get("/login").headers
    monkeypatch.setenv("FIREFLYER_HSTS", "1")
    assert client.get("/login").headers["strict-transport-security"] == "max-age=31536000"
    assert "strict-transport-security" not in TestClient(app_mod.app).get("/login").headers
