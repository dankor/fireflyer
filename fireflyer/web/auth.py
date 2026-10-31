"""Portal auth — a deliberately tiny, swappable authentication backbone.

Editor-only, and active only in portal mode. The default is a single hardcoded
user (admin/admin, overridable via env). It is built around two independent
seams so richer schemes drop in without touching the portal routes:

  1. **Who is allowed in** — the `Authenticator` protocol. Its `verify()`
     checks credentials and returns an identity (or None). Swap it for an
     LDAP/DB user store, an API-key check, anything.
  2. **How the identity is remembered** — a signed session cookie, independent
     of *how* the identity was proven. This is the SSO/OAuth seam: an external
     callback route (under `/auth/`, which the guard leaves open) validates with
     the IdP, then calls `app.sign_in(request, response, identity, source)` — which makes
     them a user the first time — and reuses the same guard + logout unchanged.

See "Portal mode → Authentication" in architecture.md for the extension recipe.
Nothing here is meant to be hardened production auth — it's the minimum that
gives the portal a login and a logout, with clean places to grow.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from html import escape
from typing import Protocol, runtime_checkable

from fastapi import Request
from fastapi.responses import HTMLResponse

from fireflyer.web import assets

_COOKIE = "ff_session"


# --- session cookie (auth-method-agnostic) ----------------------------------
# The identity is stored in a cookie signed with an HMAC so it can't be forged
# without the secret. Set FIREFLYER_SECRET in any real deployment; the dev
# default is fine for local admin/admin use.


def _secret() -> bytes:
    return os.environ.get("FIREFLYER_SECRET", "fireflyer-dev-secret").encode()


# The cookie carries the identity and a session key — a user's random key,
# rotated when their password is set or removed, so that signs them out
# everywhere (the app checks it, `session`); "" for the env login.


def _sign(identity: str, key: str = "") -> str:
    payload = base64.urlsafe_b64encode(f"{identity}\0{key}".encode()).decode()
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{mac}"


def _unsign(token: str) -> tuple[str, str] | None:
    payload, _, mac = token.rpartition(":")
    if not payload or not mac:
        return None
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        identity, _, key = base64.urlsafe_b64decode(payload.encode()).decode().partition("\0")
    except ValueError:
        return None
    return (identity, key) if identity else None


def session(request: Request) -> tuple[str, str] | None:
    """(identity, session key) from the request cookie, or None."""
    token = request.cookies.get(_COOKIE)
    return _unsign(token) if token else None


def current_user(request: Request) -> str | None:
    """The signed-in identity from the request cookie, or None."""
    signed = session(request)
    return signed[0] if signed else None


def set_session(response, identity: str, key: str = "", secure: bool = False) -> None:
    """Remember `identity` on the response — the cookie alone. Portal mode
    starts every session through `app.sign_in`, which finds (or makes) the
    user and their key; an SSO/OAuth callback calls that, not this."""
    # `secure` over HTTPS (said by the caller, from the request): the browser
    # then never sends the session over plain HTTP.
    response.set_cookie(_COOKIE, _sign(identity, key), httponly=True, samesite="lax", secure=secure)


def clear_session(response) -> None:
    response.delete_cookie(_COOKIE)


# --- passwords -----------------------------------------------------------------
# Only a salted scrypt hash is ever stored — never the password — in the form
# `scrypt$<n>$<r>$<p>$<salt>$<hash>`, so the cost can rise later without
# breaking the hashes already kept. OWASP's scrypt minimum: n=2^17, r=8, p=1
# (128 MiB, ~0.2 s).

_SCRYPT = (2 ** 17, 8, 1)
_MAXMEM = 2 ** 28
MIN_PASSWORD, MAX_PASSWORD = 8, 256   # the upper bound keeps hashing cheap to refuse


class PasswordError(ValueError):
    """A password the policy refuses."""


def check_policy(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise PasswordError(f"a password needs at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise PasswordError(f"a password has at most {MAX_PASSWORD} characters")
    return password


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=32)


def hash_password(password: str) -> str:
    """The stored form of `password` (after `check_policy`): a fresh salt each time."""
    n, r, p = _SCRYPT
    salt = secrets.token_bytes(16)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(_scrypt(password, salt, n, r, p))}"


def password_matches(password: str, stored: str) -> bool:
    """Whether `password` is the one `stored` was made from — compared in
    constant time. A stored "" (no password) never matches; it's still hashed
    against a stand-in, so a missing password takes as long as a wrong one."""
    try:
        scheme, n, r, p, salt, want = (stored or _STAND_IN).split("$")
        got = _scrypt(password[:MAX_PASSWORD], base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return bool(stored) and scheme == "scrypt" and hmac.compare_digest(got, base64.b64decode(want))


# What an unknown user, or one with no password, is checked against.
_STAND_IN = "scrypt$131072$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def new_session_key() -> str:
    return secrets.token_urlsafe(16)


# --- who is allowed in (the pluggable part) ---------------------------------


@runtime_checkable
class Authenticator(Protocol):
    """The credential check. Return the identity (e.g. the username) on success,
    or None to reject. Replace with LDAP, a user table, an API key, etc.

    An authenticator may also say who it is — `source = "ldap"`, say (default:
    `local`, a password kept here). Users it lets in are then made, the first
    time, as that source's users (`app.sign_in` → `RoleStore.provision`)."""

    def verify(self, username: str, password: str) -> str | None: ...


def identify(authenticator, username: str, password: str) -> tuple[str, str] | None:
    """(identity, source) from whichever authenticator accepts, or None."""
    if isinstance(authenticator, AnyOf):
        return authenticator.identify(username, password)
    identity = authenticator.verify(username, password)
    return (identity, getattr(authenticator, "source", "local")) if identity else None


class PasswordAuthenticator:
    """Default: one hardcoded user, admin/admin unless overridden by env
    (FIREFLYER_USER / FIREFLYER_PASSWORD). Constant-time compares so it's not
    trivially timing-probeable."""

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password

    def verify(self, username: str, password: str) -> str | None:
        # Bytes, not str: compare_digest takes only ASCII strings.
        ok = hmac.compare_digest(username.encode(), self._username.encode()) and hmac.compare_digest(
            password.encode(), self._password.encode()
        )
        return username if ok else None


class AnyOf:
    """Several authenticators, tried in turn: the first to accept wins — the
    env login, then the portal's users (`RoleStore.verify`)."""

    def __init__(self, *authenticators: Authenticator):
        self._all = authenticators

    def verify(self, username: str, password: str) -> str | None:
        found = self.identify(username, password)
        return found[0] if found else None

    def identify(self, username: str, password: str) -> tuple[str, str] | None:
        for a in self._all:
            found = identify(a, username, password)
            if found:
                return found
        return None


def admin_name() -> str:
    """The one login's user name — also the portal's built-in admin user."""
    return os.environ.get("FIREFLYER_USER", "admin")


def default_authenticator() -> PasswordAuthenticator:
    return PasswordAuthenticator(admin_name(), os.environ.get("FIREFLYER_PASSWORD", "admin"))


# --- rendered chrome --------------------------------------------------------
# Editor chrome, not chart output, so f-strings are fine. Styles in static/.


def user_menu(identity: str, extra: str = "") -> str:
    """The profile button — a native `<details>` dropdown showing the username,
    any `extra` menu content (e.g. the theme switch), and a logout action.
    Positioning is left to the caller. Styled by static/profile.css, which the
    caller must link."""
    ident = escape(identity)
    return (
        '<details class="ff-profile">'
        f'<summary title="Account">{ident} ▾</summary>'
        '<div class="ff-profile-menu">'
        f'<div class="ff-profile-name">Signed in as <b>{ident}</b></div>'
        f"{extra}"
        '<form method="post" action="/logout" style="margin:0">'
        '<button class="ff-profile-item" type="submit">Log out</button></form>'
        "</div></details>"
    )


def login_page(error: str = "", title: str = "Fireflyer Portal") -> HTMLResponse:
    err_html = f'<div class="err">{escape(error)}</div>' if error else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in · {escape(title)}</title>
{assets.favicons()}
{assets.stylesheets("login.css")}
</head>
<body>
<div class="wrap">
  <form class="login" method="post" action="/login">
    <h1>{escape(title)}</h1>
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
    {err_html}
  </form>
</div>
</body>
</html>"""
    # 401 on the re-render after a failed attempt; 200 for the initial form.
    return HTMLResponse(html, status_code=401 if error else 200)
