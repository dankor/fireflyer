"""Single sign-on through OpenID Connect (`web/oidc.py`, `/auth/oidc`): the
redirect with state, nonce and PKCE; the code exchange; every check on the ID
token; the user found by subject or made with a free name; and every way it
fails. The provider is a fake one served through httpx's mock transport — it
signs real tokens with a key of its own, and checks the PKCE verifier and the
client's secret as a real one would."""

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import pytest

jwt = pytest.importorskip("jwt")
httpx = pytest.importorskip("httpx")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from fireflyer.web import app as app_mod  # noqa: E402
from fireflyer.web import auth, oidc  # noqa: E402
from fireflyer.web.portal import RoleStore  # noqa: E402

ISSUER = "https://sso.example.com/realms/team"
SETTINGS = oidc.Settings(ISSUER, "fireflyer", "client-secret", "https://testserver/auth/oidc/callback")


class FakeProvider:
    """Discovery, JWKS and a token endpoint. `claims` (per test) go into the
    next ID token; `sign_with` / `algorithm` / `kid` sign it some other way."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.claims, self.codes, self.sign_with, self.algorithm, self.kid = {}, {}, None, "RS256", "k1"
        self.jwks_asked = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/certs"})
        if path.endswith("/certs"):
            self.jwks_asked += 1
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]})
        if path.endswith("/token"):
            return self._token(request)
        return httpx.Response(404)

    def _token(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        basic = base64.b64encode(b"fireflyer:client-secret").decode()
        given = self.codes.pop(form.get("code"), None)                  # a code works once
        if request.headers.get("authorization") != f"Basic {basic}" or given is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
        if challenge.rstrip(b"=").decode() != given["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
        return httpx.Response(200, json={"id_token": self.id_token(given["nonce"]), "access_token": "a"})

    def id_token(self, nonce):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "fireflyer", "sub": "f81d4fae-7dec", "iat": now, "exp": now + 300,
                  "nonce": nonce, "preferred_username": "ann.lee", "given_name": "Ann", "family_name": "Lee",
                  "email": "ann@corp.example"}
        claims.update(self.claims)
        claims = {k: v for k, v in claims.items() if v is not None}
        key = self.sign_with or self.key
        return jwt.encode(claims, key, algorithm=self.algorithm, headers={"kid": self.kid})

    def authorize(self, location: str, code: str = "the-code") -> dict:
        """What the provider does at its sign-in page: keep the request's nonce
        and challenge for the code it gives back. The callback's query."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert (query["response_type"], query["client_id"], query["code_challenge_method"]) == \
            ("code", "fireflyer", "S256")
        assert query["redirect_uri"] == SETTINGS.redirect_uri and "openid" in query["scope"].split()
        self.codes[code] = {"nonce": query["nonce"], "challenge": query["code_challenge"]}
        return {"code": code, "state": query["state"]}


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def roles(app_state):
    store = RoleStore(":memory:", admin="root")
    app_state("roles", store)
    return store


@pytest.fixture
def client(app_state, provider, roles, monkeypatch):
    monkeypatch.setattr(app_mod, "_users_on", lambda: True)
    monkeypatch.setattr(auth, "admin_name", lambda: "root")
    app_state("authenticator", auth.AnyOf(auth.PasswordAuthenticator("root", "root-pass"), app_mod._PortalUsers()))
    app_state("oidc", oidc.Provider(SETTINGS, http=httpx.Client(transport=httpx.MockTransport(provider.handle))))
    return TestClient(app_mod.app, base_url="https://testserver")


def _sign_in(client, provider, **query):
    start = client.get("/auth/oidc", follow_redirects=False)
    assert start.status_code == 303 and start.headers["location"].startswith(f"{ISSUER}/auth?")
    params = {**provider.authorize(start.headers["location"]), **query}
    return client.get("/auth/oidc/callback", params=params, follow_redirects=False)


# --- the flow ---------------------------------------------------------------------------


def test_a_person_signs_in_and_becomes_a_user_with_no_roles(client, provider, roles):
    done = _sign_in(client, provider)
    assert done.status_code == 303 and done.headers["location"] == "/"
    cookies = done.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("ff_session="))
    assert "Secure" in session and "HttpOnly" in session and "samesite=lax" in session.lower()
    assert any(c.startswith('ff_oidc=""') or "ff_oidc=;" in c for c in cookies)      # the state, gone
    ann = roles.user("ann.lee")
    assert (ann.source, ann.roles, ann.has_password, ann.about) == ("oidc", [], False, "Ann Lee · ann@corp.example")
    assert roles.group("_ann.lee").personal
    # Signed in: past the login guard (not sent to /login), and not an admin.
    assert client.get("/users", follow_redirects=False).status_code == 403


def test_the_next_sign_in_finds_them_by_subject_whatever_their_username(client, provider, roles):
    _sign_in(client, provider)
    provider.claims = {"preferred_username": "ann.smith", "family_name": "Smith"}    # renamed there
    client.cookies.clear()
    assert _sign_in(client, provider).status_code == 303
    assert [u.name for u in roles.users()] == ["root", "ann.lee"]
    assert roles.user("ann.lee").about == "Ann Smith · ann@corp.example"


def test_a_taken_name_gets_a_number_and_no_one_takes_over_a_user(client, provider, roles):
    roles.create_user("ann.lee")                                    # a local user of that name
    _sign_in(client, provider)
    assert roles.user("ann.lee2").source == "oidc" and roles.user("ann.lee").source == "local"
    provider.claims = {"sub": "another-person"}                    # same username, another subject
    client.cookies.clear()
    _sign_in(client, provider)
    assert roles.user("ann.lee3").source == "oidc"
    provider.claims = {"sub": "a-third", "preferred_username": "root"}     # the admin's name: never
    client.cookies.clear()
    _sign_in(client, provider)
    assert roles.user("root2").source == "oidc" and roles.users()[0].builtin


def test_a_subject_bound_to_a_name_keeps_it(roles):
    roles.provision("dana", "oidc", subject="iss one")
    with pytest.raises(Exception, match="another person"):
        roles.provision("dana", "oidc", subject="iss two")
    assert roles.user_by_subject("oidc", "iss one") == "dana" and roles.user_by_subject("oidc", "x") is None


@pytest.mark.parametrize("wanted, name", [("Ann.Lee", "ann.lee"), ("42bob", "bob"), ("Ölga", "lga"),
                                          (".ann..lee.", "ann.lee"), ("4.bob", "bob"), ("a-b c", "abc"),
                                          ("", "user"), ("___", "user"), ("...", "user")])
def test_a_new_name_is_what_a_user_name_may_hold(roles, wanted, name):
    assert roles.free_user_name(wanted) == name


# --- every way it fails -------------------------------------------------------------------


def _fails(response):
    assert response.status_code == 401 and "Single sign-on failed." in response.text
    assert not any(c.startswith("ff_session=") and "ff_session=;" not in c and 'ff_session=""' not in c
                   for c in response.headers.get_list("set-cookie"))


@pytest.mark.parametrize("claims", [
    {"iss": "https://evil.example.com"},                           # another issuer
    {"aud": "another-client"},                                     # another client's token
    {"aud": ["fireflyer", "other"], "azp": "other"},               # given to another client
    {"exp": int(time.time()) - 600, "iat": int(time.time()) - 900},  # expired
    {"sub": None},                                                 # no subject
    {"nonce": "a-replayed-nonce"},                                 # not this sign-in's
])
def test_a_token_that_fails_a_check_signs_no_one_in(client, provider, roles, claims):
    provider.claims = claims
    _fails(_sign_in(client, provider))
    assert [u.name for u in roles.users()] == ["root"]


def test_a_token_signed_by_another_key_or_no_key_is_refused(client, provider):
    provider.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _fails(_sign_in(client, provider))
    provider.sign_with, provider.algorithm = "client-secret-of-thirty-two-bytes!", "HS256"   # an HMAC
    client.cookies.clear()
    _fails(_sign_in(client, provider))


def test_an_unknown_key_id_fetches_the_keys_once_more(client, provider):
    provider.kid = "rotated"
    _fails(_sign_in(client, provider))
    assert provider.jwks_asked == 2


def test_the_state_must_match_and_be_ours_and_fresh(client, provider, monkeypatch):
    _fails(_sign_in(client, provider, state="forged"))
    client.cookies.clear()
    start = client.get("/auth/oidc", follow_redirects=False)
    query = provider.authorize(start.headers["location"])
    client.cookies.clear()                                         # no state cookie: not started here
    _fails(client.get("/auth/oidc/callback", params=query, follow_redirects=False))
    now = time.time
    monkeypatch.setattr(oidc.time, "time", lambda: now() + oidc.STATE_SECONDS + 5)
    _fails(_sign_in(client, provider))                             # too late
    tampered = oidc._seal({"state": "s", "nonce": "n", "verifier": "v", "at": 0})[:-4] + "0000"
    assert oidc._unseal(tampered) is None and oidc._unseal(None) is None


def test_a_callback_works_once(client, provider):
    start = client.get("/auth/oidc", follow_redirects=False)
    query = provider.authorize(start.headers["location"])
    state_cookie = client.cookies.get("ff_oidc")
    assert client.get("/auth/oidc/callback", params=query, follow_redirects=False).status_code == 303
    client.cookies.clear()
    client.cookies.set("ff_oidc", state_cookie, path="/auth/oidc")          # replayed, cookie and all
    _fails(client.get("/auth/oidc/callback", params=query, follow_redirects=False))   # the code is spent


def test_the_provider_refusing_or_down_is_a_failed_sign_in(client, provider, app_state):
    _fails(_sign_in(client, provider, error="access_denied"))
    app_state("oidc", oidc.Provider(SETTINGS, http=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(503)))))
    _fails(client.get("/auth/oidc", follow_redirects=False))


def test_the_routes_are_a_module_of_their_own_and_log_why_nothing_secret(provider, roles):
    """`oidc.routes` on a bare app: all it needs of one is what it's given."""
    from fastapi import FastAPI

    logged, signed = [], []
    bare = FastAPI()
    bare.state.oidc = oidc.Provider(SETTINGS, http=httpx.Client(transport=httpx.MockTransport(provider.handle)))
    bare.include_router(oidc.routes(
        sign_in=lambda request, response, name, source, **profile: signed.append((name, source)),
        roles=lambda: roles, log=lambda request, action, **detail: logged.append((action, detail)), title="T"))
    client = TestClient(bare, base_url="https://testserver")
    assert _sign_in(client, provider).status_code == 303
    assert signed == [("ann.lee", "oidc")] and logged[-1] == ("login", {"actor": "ann.lee", "source": "oidc"})

    provider.claims = {"aud": "another-client"}
    client.cookies.clear()
    _fails(_sign_in(client, provider))
    action, detail = logged[-1]
    assert action == "login" and detail["status_code"] == 401 and "audience" in detail["error"].lower()
    assert "client-secret" not in str(detail) and "eyJ" not in str(detail)       # no secret, no token


# --- settings and the page --------------------------------------------------------------


def test_settings_are_all_or_none():
    assert oidc.settings({}) is None
    with pytest.raises(SystemExit, match="FIREFLYER_OIDC_CLIENT_SECRET, FIREFLYER_OIDC_REDIRECT_URI"):
        oidc.settings({"FIREFLYER_OIDC_ISSUER": ISSUER, "FIREFLYER_OIDC_CLIENT_ID": "fireflyer"})
    full = {"FIREFLYER_OIDC_ISSUER": ISSUER, "FIREFLYER_OIDC_CLIENT_ID": "fireflyer",
            "FIREFLYER_OIDC_CLIENT_SECRET": "s", "FIREFLYER_OIDC_REDIRECT_URI": "https://x/auth/oidc/callback"}
    assert oidc.settings(full).client_id == "fireflyer"


def test_the_login_page_offers_it_only_when_it_is_on(client, app_state):
    assert 'href="/auth/oidc"' in client.get("/login").text
    app_state("oidc", None)
    assert 'href="/auth/oidc"' not in client.get("/login").text
    assert client.get("/auth/oidc").status_code == 404
    assert client.get("/auth/oidc/callback").status_code == 404


def test_a_provider_naming_another_issuer_is_refused(client, app_state):
    def handle(request):
        return httpx.Response(200, json={"issuer": "https://elsewhere", "authorization_endpoint": "x"})
    app_state("oidc", oidc.Provider(SETTINGS, http=httpx.Client(transport=httpx.MockTransport(handle))))
    _fails(client.get("/auth/oidc", follow_redirects=False))
