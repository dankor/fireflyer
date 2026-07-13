"""Portal auth — a deliberately tiny, swappable authentication backbone.

Editor-only, and active only in portal mode. The default is a single hardcoded
user (admin/admin, overridable via env). It is built around two independent
seams so richer schemes drop in without touching the portal routes:

  1. **Who is allowed in** — the `Authenticator` protocol. Its `verify()`
     checks credentials and returns an identity (or None). Swap it for an
     LDAP/DB user store, an API-key check, anything.
  2. **How the identity is remembered** — a signed session cookie, independent
     of *how* the identity was proven. This is the SSO/OAuth seam: an external
     callback route just validates with the IdP, then calls `set_session()`
     with the resulting identity and reuses the same guard + logout unchanged.

See "Portal mode → Authentication" in architecture.md for the extension recipe.
Nothing here is meant to be hardened production auth — it's the minimum that
gives the portal a login and a logout, with clean places to grow.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from html import escape
from typing import Protocol, runtime_checkable

from fastapi import Request
from fastapi.responses import HTMLResponse

_COOKIE = "ff_session"


# --- session cookie (auth-method-agnostic) ----------------------------------
# The identity is stored in a cookie signed with an HMAC so it can't be forged
# without the secret. Set FIREFLYER_SECRET in any real deployment; the dev
# default is fine for local admin/admin use.


def _secret() -> bytes:
    return os.environ.get("FIREFLYER_SECRET", "fireflyer-dev-secret").encode()


def _sign(identity: str) -> str:
    mac = hmac.new(_secret(), identity.encode(), hashlib.sha256).hexdigest()
    return f"{identity}:{mac}"


def _unsign(token: str) -> str | None:
    identity, _, mac = token.rpartition(":")
    if not identity or not mac:
        return None
    expected = hmac.new(_secret(), identity.encode(), hashlib.sha256).hexdigest()
    return identity if hmac.compare_digest(mac, expected) else None


def current_user(request: Request) -> str | None:
    """The signed-in identity from the request cookie, or None."""
    token = request.cookies.get(_COOKIE)
    return _unsign(token) if token else None


def set_session(response, identity: str) -> None:
    """Remember `identity` on the response. SSO/OAuth callbacks call this too."""
    response.set_cookie(_COOKIE, _sign(identity), httponly=True, samesite="lax")


def clear_session(response) -> None:
    response.delete_cookie(_COOKIE)


# --- who is allowed in (the pluggable part) ---------------------------------


@runtime_checkable
class Authenticator(Protocol):
    """The credential check. Return the identity (e.g. the username) on success,
    or None to reject. Replace with LDAP, a user table, an API key, etc."""

    def verify(self, username: str, password: str) -> str | None: ...


class PasswordAuthenticator:
    """Default: one hardcoded user, admin/admin unless overridden by env
    (FIREFLYER_USER / FIREFLYER_PASSWORD). Constant-time compares so it's not
    trivially timing-probeable."""

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password

    def verify(self, username: str, password: str) -> str | None:
        ok = hmac.compare_digest(username, self._username) and hmac.compare_digest(
            password, self._password
        )
        return username if ok else None


def default_authenticator() -> PasswordAuthenticator:
    return PasswordAuthenticator(
        os.environ.get("FIREFLYER_USER", "admin"),
        os.environ.get("FIREFLYER_PASSWORD", "admin"),
    )


# --- rendered chrome --------------------------------------------------------
# Editor chrome, not chart output, so the escaped-string style matches app.py.


# Profile dropdown styles, injected into both the editor page and the gallery
# (no shared stylesheet by design, so callers include this next to their CSS).
PROFILE_CSS = """
  .ff-profile { position: relative; }
  .ff-profile > summary { list-style: none; cursor: pointer; display: inline-flex;
    align-items: center; gap: 5px; font-size: 13px; color: var(--text);
    background: var(--panel); border: 1px solid var(--border); padding: 5px 10px;
    border-radius: 4px; }
  .ff-profile > summary::-webkit-details-marker { display: none; }
  .ff-profile > summary:hover, .ff-profile[open] > summary { background: var(--bg); }
  .ff-profile-menu { position: absolute; right: 0; top: calc(100% + 6px);
    min-width: 168px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; box-shadow: 0 8px 24px rgba(0,0,0,.18); padding: 6px; z-index: 30; }
  .ff-profile-name { font-size: 12px; color: var(--muted); padding: 6px 8px 8px; }
  .ff-profile-theme { display: flex; justify-content: center; padding: 8px 8px 4px; }
  .ff-profile-item { display: block; width: 100%; text-align: left; background: transparent;
    border: 0; color: var(--text); font-size: 13px; padding: 7px 8px; border-radius: 4px;
    cursor: pointer; }
  .ff-profile-item:hover { background: var(--bg); }
"""


def user_menu(identity: str, extra: str = "") -> str:
    """The profile button — a native `<details>` dropdown showing the username,
    any `extra` menu content (e.g. the theme switch), and a logout action.
    Positioning is left to the caller. Styled by `PROFILE_CSS`, which the caller
    must include."""
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


_LOGIN_CSS = """
  * { box-sizing: border-box; }
  :root { color-scheme: light; --bg:#f5f6f8; --panel:#fff; --border:#e0e0e0;
    --text:#20242b; --muted:#5e6975; --accent:#20a7c9; --accent-hover:#1a8aa6;
    --error:#e04355; }
  @media (prefers-color-scheme: dark) { :root { color-scheme: dark;
    --bg:#0f1620; --panel:#1b2635; --border:#2c384a; --text:#e6e8ec;
    --muted:#a3adbd; --accent:#20a7c9; --accent-hover:#48c4e0; } }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font-family:-apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; }
  .wrap { min-height:100%; display:flex; align-items:center; justify-content:center; }
  form.login { background:var(--panel); border:1px solid var(--border);
    border-radius:10px; padding:28px 26px; width:320px; }
  form.login h1 { font-size:18px; margin:0 0 18px; }
  form.login label { display:block; font-size:12px; color:var(--muted); margin:12px 0 4px; }
  form.login input { width:100%; padding:8px 10px; border:1px solid var(--border);
    border-radius:4px; background:var(--bg); color:var(--text); font-size:14px; }
  form.login button { width:100%; margin-top:18px; padding:9px; border:0;
    border-radius:4px; background:var(--accent); color:#fff; font-size:14px;
    font-weight:500; cursor:pointer; }
  form.login button:hover { background:var(--accent-hover); }
  .err { color:var(--error); font-size:13px; margin-top:12px; }
"""


def login_page(error: str = "", title: str = "Fireflyer Portal") -> HTMLResponse:
    err_html = f'<div class="err">{escape(error)}</div>' if error else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in · {escape(title)}</title>
<style>{_LOGIN_CSS}</style>
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
