"""Portal auth unit tests — exercise `fireflyer.web.auth` directly, no web
stack (same rule as the chat/portal tests). The session cookie's signing and
the credential check are the security-relevant bits; the login form and user
menu are checked for the essentials."""

from types import SimpleNamespace

from fireflyer.web import auth


def _request(token=None):
    cookies = {auth._COOKIE: token} if token is not None else {}
    return SimpleNamespace(cookies=cookies)


def test_password_authenticator_accepts_and_rejects():
    a = auth.PasswordAuthenticator("admin", "admin")
    assert a.verify("admin", "admin") == "admin"
    assert a.verify("admin", "nope") is None
    assert a.verify("root", "admin") is None


def test_default_authenticator_is_admin_admin():
    a = auth.default_authenticator()
    assert a.verify("admin", "admin") == "admin"


def test_default_authenticator_honors_env(monkeypatch):
    monkeypatch.setenv("FIREFLYER_USER", "dana")
    monkeypatch.setenv("FIREFLYER_PASSWORD", "s3cret")
    a = auth.default_authenticator()
    assert a.verify("dana", "s3cret") == "dana"
    assert a.verify("admin", "admin") is None


def test_session_cookie_roundtrips():
    token = auth._sign("admin")
    assert auth.current_user(_request(token)) == "admin"


def test_tampered_cookie_is_rejected():
    token = auth._sign("admin")
    forged = token.replace("admin", "root", 1)  # swap identity, keep old mac
    assert auth.current_user(_request(forged)) is None


def test_no_cookie_is_anonymous():
    assert auth.current_user(_request()) is None
    assert auth.current_user(_request("garbage")) is None


def test_signature_depends_on_secret(monkeypatch):
    token = auth._sign("admin")
    monkeypatch.setenv("FIREFLYER_SECRET", "a-different-secret")
    # A token signed under the old secret must not verify under the new one.
    assert auth.current_user(_request(token)) is None


def test_login_page_status_codes_and_escaping():
    ok = auth.login_page()
    assert ok.status_code == 200
    bad = auth.login_page("Invalid username or password.")
    assert bad.status_code == 401
    assert b"Invalid username or password." in bad.body
    assert b'action="/login"' in ok.body


def test_user_menu_has_logout_and_escapes_identity():
    html = auth.user_menu("<b>admin</b>")
    assert 'action="/logout"' in html
    assert "<b>admin</b>" not in html
    assert "&lt;b&gt;admin&lt;/b&gt;" in html
