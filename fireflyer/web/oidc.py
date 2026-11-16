"""Single sign-on through an OpenID Connect provider — Keycloak, or any other.

Portal mode only, and only when it's configured — all four, or none:

    FIREFLYER_OIDC_ISSUER         https://sso.example.com/realms/team
    FIREFLYER_OIDC_CLIENT_ID      fireflyer
    FIREFLYER_OIDC_CLIENT_SECRET  the client's secret (a confidential client)
    FIREFLYER_OIDC_REDIRECT_URI   https://dashboards.example.com/auth/oidc/callback

The authorization code flow, with PKCE:

1. `start` — `GET /auth/oidc` — sends the browser to the provider with a random
   `state`, `nonce` and PKCE challenge. Those three and the time ride in a
   cookie of their own (`ff_oidc`, signed with FIREFLYER_SECRET, ten minutes):
   no session on the server.
2. `finish` — `GET /auth/oidc/callback` — checks `state` against the cookie,
   swaps the code for tokens (the client's secret and the PKCE verifier), and
   checks the ID token: signed by a key of the provider's JWKS with an
   asymmetric algorithm, its issuer, audience, expiry and nonce. Only then is
   anyone signed in. Nothing else is taken as who someone is: the user-info
   endpoint is never asked.
3. The callback finds the user by issuer and subject — the provider's stable
   id — or makes one: a name from `preferred_username`
   (`RoleStore.free_user_name`), no roles, through the app's `sign_in` with
   source `oidc`. The name never changes after; a new username at the provider
   is the same person here.

Which people may sign in is the provider's to decide (its client's access);
what they may read here, the roles an admin gives them. Logout ends the
session here, not at the provider. Errors stay in the activity log: the page
says only that sign-in failed.

The whole feature is this file. The app adds it with one line —
`app.include_router(oidc.routes(sign_in, roles, log, title))` — and sets
`app.state.oidc = oidc.from_env()`; the routes answer 404 while that is None.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fireflyer.web import auth

SOURCE = "oidc"
COOKIE = "ff_oidc"
COOKIE_PATH = "/auth/oidc"
STATE_SECONDS = 600
_LEEWAY = 60                        # clock skew allowed between us and the provider, in seconds
# Asymmetric only: never `none`, never an HMAC keyed by something the client knows.
_ALGORITHMS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
_SETTINGS = {
    "issuer": "FIREFLYER_OIDC_ISSUER",
    "client_id": "FIREFLYER_OIDC_CLIENT_ID",
    "client_secret": "FIREFLYER_OIDC_CLIENT_SECRET",
    "redirect_uri": "FIREFLYER_OIDC_REDIRECT_URI",
}


class OIDCError(Exception):
    """Sign-in failed. The message is for the activity log, not the page."""


@dataclass(frozen=True)
class Settings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str


def settings(env=os.environ) -> Settings | None:
    """The provider's settings — None when none is set; SystemExit, naming
    what's missing, when only some are."""
    values = {key: env.get(name, "").strip() for key, name in _SETTINGS.items()}
    if not any(values.values()):
        return None
    missing = [_SETTINGS[key] for key, value in values.items() if not value]
    if missing:
        raise SystemExit(f"single sign-on needs {', '.join(missing)} set too")
    return Settings(**values)


def from_env(env=os.environ) -> Provider | None:
    """The provider the settings name, or None when single sign-on is off."""
    found = settings(env)
    return Provider(found) if found else None


def identity(claims: dict) -> tuple[str, str, dict[str, str]]:
    """From a checked ID token: the subject (issuer and `sub` — stable, unique),
    the name wanted for a new user, and the profile kept for them."""
    subject = f"{claims['iss']} {claims['sub']}"
    email = claims.get("email") or ""
    wanted = claims.get("preferred_username") or email.partition("@")[0] or claims.get("given_name") or ""
    profile = {"first_name": claims.get("given_name") or "", "last_name": claims.get("family_name") or "",
               "email": email}
    return subject, wanted, profile


# --- the state cookie: signed, so only we could have written it -------------------------


def _seal(data: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return f"{payload}.{_mac(payload)}"


def _unseal(token: str | None) -> dict | None:
    payload, _, mac = (token or "").rpartition(".")
    if not payload or not hmac.compare_digest(mac, _mac(payload)):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None


def _mac(payload: str) -> str:
    # Its own purpose in the key, so a session cookie can never pass for this one.
    return hmac.new(auth._secret(), b"oidc-state:" + payload.encode(), hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# --- the provider ---------------------------------------------------------------------


class Provider:
    """One OpenID Connect provider: its metadata and keys fetched when first
    needed, then kept; the keys fetched again for a key id they don't have (the
    provider rotated them). `http`: an httpx client — the tests give one that
    answers for a provider of their own."""

    def __init__(self, settings: Settings, http=None):
        self.settings = settings
        self._http = http
        self._meta: dict | None = None
        self._keys: dict | None = None

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=10)
        return self._http

    def _get(self, url: str) -> dict:
        try:
            response = self._client().get(url)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise OIDCError(f"the provider didn't answer {url}: {exc}") from exc

    def meta(self) -> dict:
        if self._meta is None:
            meta = self._get(self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration")
            if meta.get("issuer") != self.settings.issuer:
                raise OIDCError(f"the provider says its issuer is {meta.get('issuer')!r}")
            self._meta = meta
        return self._meta

    def start(self) -> tuple[str, str]:
        """The provider's sign-in URL to send the browser to, and the state
        cookie's value."""
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
        endpoint = self.meta()["authorization_endpoint"]
        url = endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)
        return url, _seal({"state": state, "nonce": nonce, "verifier": verifier, "at": int(time.time())})

    def finish(self, params: dict, cookie: str | None) -> dict:
        """The callback's query (`params`) and the state cookie → the ID
        token's claims, every check passed. OIDCError otherwise."""
        held = _unseal(cookie)
        if held is None:
            raise OIDCError("no sign-in was started here (no state cookie, or not ours)")
        if time.time() - held["at"] > STATE_SECONDS:
            raise OIDCError("the sign-in took too long")
        if params.get("error"):
            raise OIDCError(f"the provider refused: {params['error']} {params.get('error_description', '')}".strip())
        if not hmac.compare_digest(params.get("state", ""), held["state"]):
            raise OIDCError("the state doesn't match")
        if not params.get("code"):
            raise OIDCError("no code")
        return self._check(self._token(params["code"], held["verifier"]), held["nonce"])

    def _token(self, code: str, verifier: str) -> str:
        try:
            response = self._client().post(
                self.meta()["token_endpoint"],
                data={"grant_type": "authorization_code", "code": code,
                      "redirect_uri": self.settings.redirect_uri, "code_verifier": verifier},
                auth=(self.settings.client_id, self.settings.client_secret),
            )
            response.raise_for_status()
            token = response.json().get("id_token")
        except OIDCError:
            raise
        except Exception as exc:
            raise OIDCError(f"the code exchange failed: {exc}") from exc
        if not token:
            raise OIDCError("the provider gave no ID token")
        return token

    def _check(self, token: str, nonce: str) -> dict:
        import jwt

        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in _ALGORITHMS:
                raise OIDCError(f"the ID token is signed with {algorithm!r}")
            claims = jwt.decode(
                token, self._key(header.get("kid")), algorithms=[algorithm],
                audience=self.settings.client_id, issuer=self.settings.issuer, leeway=_LEEWAY,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise OIDCError(f"the ID token is refused: {exc}") from exc
        if not claims.get("sub"):
            raise OIDCError("the ID token names no subject")
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OIDCError("the nonce doesn't match")
        audience = claims["aud"]
        if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != self.settings.client_id:
            raise OIDCError("the ID token was given to another client")
        return claims

    def _key(self, kid: str | None):
        """The provider's public key `kid` — the keys fetched again once when
        it isn't among them."""
        import jwt

        for fresh in (False, True):
            if fresh or self._keys is None:
                keys = jwt.PyJWKSet.from_dict(self._get(self.meta()["jwks_uri"]))
                self._keys = {k.key_id: k for k in keys.keys}
            if kid in self._keys:
                return self._keys[kid].key
            if kid is None and len(self._keys) == 1:
                return next(iter(self._keys.values())).key
        raise OIDCError(f"no key {kid!r} at the provider")


# --- the routes -----------------------------------------------------------------------
# Under `/auth/`, which the login guard leaves open: they run before there's a session.


def routes(sign_in, roles, log, title: str) -> APIRouter:
    """`/auth/oidc` and its callback, on `request.app.state.oidc` (a `Provider`,
    or None: 404). What they need of the app, passed in: `sign_in(request,
    response, name, source, **profile)` starts a session, `roles()` is the
    `RoleStore`, `log(request, action, **detail)` writes the activity log,
    `title` heads the login page."""
    router = APIRouter()

    def failed(request: Request, error: str) -> HTMLResponse:
        """The login page again — "failed", nothing more; why goes to the log."""
        log(request, "login", actor="", source=SOURCE, status_code=401, error=error)
        page = auth.login_page("Single sign-on failed.", title, sso=True)
        page.delete_cookie(COOKIE, path=COOKIE_PATH)
        return page

    @router.get("/auth/oidc")
    def oidc_start(request: Request):
        provider = request.app.state.oidc
        if provider is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            url, state = provider.start()
        except OIDCError as exc:
            return failed(request, str(exc))
        response = RedirectResponse(url, status_code=303)
        # Lax: the provider's redirect back is a top-level GET, which Lax still sends it with.
        response.set_cookie(COOKIE, state, max_age=STATE_SECONDS, path=COOKIE_PATH,
                            httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return response

    @router.get("/auth/oidc/callback")
    def oidc_callback(request: Request):
        """The provider's redirect back: the checks (`Provider.finish`), then
        the user — found by subject, or made with a free name — and their
        session. The state cookie goes either way, so a callback can't be
        replayed."""
        provider = request.app.state.oidc
        if provider is None:
            return HTMLResponse("Not found", status_code=404)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE, path=COOKIE_PATH)
        try:
            claims = provider.finish(dict(request.query_params), request.cookies.get(COOKIE))
            subject, wanted, profile = identity(claims)
            store = roles()
            name = store.user_by_subject(SOURCE, subject) or store.free_user_name(wanted)
            sign_in(request, response, name, SOURCE, subject=subject, **profile)
        except (OIDCError, ValueError) as exc:          # RoleError is a ValueError
            return failed(request, str(exc))
        log(request, "login", actor=name, source=SOURCE)
        return response

    return router
