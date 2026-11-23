"""`python -m fireflyer.portal` — launch the editor in portal mode.

Portal mode stores dashboards in a database and lists them in a gallery (see
`fireflyer.web.portal` for the store itself). This entrypoint reads runtime
config from `portal.yaml` (title, database url, tls), lets the environment
override it (`DATABASE_URL`, `FIREFLYER_PORTAL_TITLE`, `FIREFLYER_TLS*`), flips
`FIREFLYER_PORTAL` on, and serves on 0.0.0.0 port `FIREFLYER_LISTEN_PORT` (8000)
for containers — over **HTTPS** unless told otherwise (see `tls_settings`), as one process (`FIREFLYER_RELOAD`
for a reloader in development). It stops at once without Postgres, the admin's
password or the session secret. The plain `python -m fireflyer.web`
entrypoint stays on 127.0.0.1 in single-dashboard mode, over plain HTTP.
"""

import datetime as dt
import ipaddress
import os
from pathlib import Path

import uvicorn
import yaml

from fireflyer.web.auth import check_portal_env
from fireflyer.web.portal import database_url

# The names a made-up local certificate covers, unless `tls.hosts` says more.
LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1"]
# Browsers refuse a certificate valid for more than 398 days; a local one is
# remade a month before it runs out.
_CERT_DAYS = 397
_RENEW_BEFORE = dt.timedelta(days=30)


def _load_config() -> dict:
    path = Path(os.environ.get("FIREFLYER_PORTAL_CONFIG", "portal.yaml"))
    if path.exists():
        return yaml.safe_load(path.read_text()) or {}
    return {}


# --- https -------------------------------------------------------------------------


def tls_settings(cfg: dict, env: dict) -> dict:
    """How to serve, from `portal.yaml`'s `tls:` and the environment (which
    wins): a dict with `mode` and, unless it's off, the `cert` and `key` files.

    - **your certificate** — `cert` + `key` (FIREFLYER_TLS_CERT / _KEY): a
      team's, for its own domain. Mode `cert`.
    - **auto** (the default) — no certificate given: one made here for
      `hosts` (FIREFLYER_TLS_HOSTS, comma-separated; localhost by default),
      kept in `dir` (FIREFLYER_TLS_DIR, `data/tls`), so local development is
      HTTPS from the first start. Self-signed: the browser asks once.
    - **off** — FIREFLYER_TLS=off: plain HTTP, for behind a proxy that does
      TLS itself (and sends X-Forwarded-Proto, so cookies still go Secure)."""
    tls = cfg.get("tls") or {}
    mode = (env.get("FIREFLYER_TLS") or tls.get("mode") or "auto").lower()
    cert = env.get("FIREFLYER_TLS_CERT") or tls.get("cert")
    key = env.get("FIREFLYER_TLS_KEY") or tls.get("key")
    if mode == "off":
        return {"mode": "off"}
    if cert or key:
        if not (cert and key):
            raise SystemExit("tls: a certificate needs both `cert` and `key`")
        for path in (cert, key):
            if not Path(path).is_file():
                raise SystemExit(f"tls: no such file {path}")
        return {"mode": "cert", "cert": str(cert), "key": str(key)}
    if mode != "auto":
        raise SystemExit(f"tls: mode is auto, off, or a cert + key — not {mode!r}")
    hosts = env.get("FIREFLYER_TLS_HOSTS")
    hosts = [h.strip() for h in hosts.split(",")] if hosts else (tls.get("hosts") or LOCAL_HOSTS)
    folder = Path(env.get("FIREFLYER_TLS_DIR") or tls.get("dir") or "data/tls")
    cert_path, key_path = local_certificate(folder, [h for h in hosts if h])
    return {"mode": "auto", "cert": str(cert_path), "key": str(key_path)}


def local_certificate(folder: Path, hosts: list[str]) -> tuple[Path, Path]:
    """A self-signed certificate for `hosts` in `folder` — the one already
    there while it covers them and isn't about to run out, else a new one.
    The key is readable by its owner only."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    cert_path, key_path = folder / "local-cert.pem", folder / "local-key.pem"
    names = [_san(h) for h in hosts]
    if cert_path.is_file() and key_path.is_file():
        old = x509.load_pem_x509_certificate(cert_path.read_bytes())
        covered = set(old.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        fresh = old.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) > _RENEW_BEFORE
        if fresh and set(names) <= covered:
            return cert_path, key_path

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fireflyer local")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=_CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _san(host: str):
    """A host as the certificate names it: an IP address, or a DNS name."""
    from cryptography import x509

    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


# --- port --------------------------------------------------------------------------


def listen_port(env: dict) -> int:
    """The port to listen on: FIREFLYER_LISTEN_PORT, else a numeric
    FIREFLYER_PORT, else 8000.

    Kubernetes gives a pod `<SERVICE>_PORT=tcp://<ip>:<port>` for every
    Service, so a Service named `fireflyer` sets FIREFLYER_PORT to a URL. That
    value is ignored, not read: the Service's port can differ from the port of
    the container."""
    if value := env.get("FIREFLYER_LISTEN_PORT"):
        return _port("FIREFLYER_LISTEN_PORT", value)
    value = env.get("FIREFLYER_PORT", "")
    if not value or value.startswith("tcp://"):
        return 8000
    return _port("FIREFLYER_PORT", value)


def _port(name: str, value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if 0 < port < 65536:
        return port
    raise SystemExit(f"{name} must be a port number from 1 to 65535, not {value!r}")


def main() -> None:
    cfg = _load_config()
    os.environ["FIREFLYER_PORTAL"] = "1"
    # Env wins over the file, so a container can override without editing yaml.
    if cfg.get("title") and "FIREFLYER_PORTAL_TITLE" not in os.environ:
        os.environ["FIREFLYER_PORTAL_TITLE"] = cfg["title"]
    if cfg.get("database_url") and "DATABASE_URL" not in os.environ:
        os.environ["DATABASE_URL"] = cfg["database_url"]
    database_url()                    # Postgres, or stop here and say so
    check_portal_env()                # the admin's password and the session secret, set

    tls = tls_settings(cfg, os.environ)
    # HSTS only with a team's own certificate: telling a browser to insist on
    # HTTPS for localhost would follow it to every other local app.
    if tls["mode"] == "cert":
        os.environ.setdefault("FIREFLYER_HSTS", "1")
    port = listen_port(os.environ)
    scheme = "http" if tls["mode"] == "off" else "https"
    print(f"Fireflyer portal on {scheme}://127.0.0.1:{port} (tls: {tls['mode']})", flush=True)

    # One process, no reloader: what a deployment runs. FIREFLYER_RELOAD=1 for
    # development (the local compose sets it) restarts on a code change.
    reload = os.environ.get("FIREFLYER_RELOAD", "").lower() in {"1", "true", "yes", "on"}

    uvicorn.run(
        "fireflyer.web.app:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
        reload_includes=["*.py", "*.css", "*.html"] if reload else None,
        ssl_certfile=tls.get("cert"),
        ssl_keyfile=tls.get("key"),
        # Behind a proxy that does TLS: trust its X-Forwarded-Proto / -For from
        # the addresses given (a proxy container's, or "*" on a private network).
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FIREFLYER_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
