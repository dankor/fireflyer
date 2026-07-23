"""Object storage for dataset Parquet files — a tiny seam so datasets work the
same in local mode (a folder on disk) and portal mode (S3-compatible object
storage, e.g. Garage). Charts read through the `(uri, storage_options)` this
returns, so Polars can range-scan the Parquet efficiently either way — only the
columns/row-groups a chart needs are fetched, not the whole file.

`LocalObjectStore` has no dependencies and powers local mode, dev, and the whole
test suite. `S3ObjectStore` imports `boto3` lazily (the `.[portal]` extra) and is
only exercised at portal runtime, so the core install stays dependency-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list_keys(self) -> list[str]: ...

    def source(self, key: str) -> tuple[str, dict | None]:
        """`(uri, storage_options)` to hand to `pl.scan_parquet` for `key`."""
        ...


class LocalObjectStore:
    """Files under a base directory. Used by local mode, dev, and tests."""

    def __init__(self, base: str):
        self._base = Path(base)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._base / key

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list_keys(self) -> list[str]:
        return sorted(
            str(p.relative_to(self._base)) for p in self._base.rglob("*") if p.is_file()
        )

    def source(self, key: str) -> tuple[str, dict | None]:
        # A plain path; Polars scans it directly (no storage options).
        return str(self._path(key)), None


class S3ObjectStore:
    """S3-compatible object storage (Garage/S3). Runtime only — imports boto3
    lazily so this module stays importable without the `.[portal]` extra."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "garage",
    ):
        import boto3  # optional dependency (.[portal])

        self._bucket = bucket
        self._region = region
        self._endpoint = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        # No network I/O here — constructing the store must not crash app
        # startup if the object store isn't reachable yet. The bucket is created
        # out of band (Garage: `garage bucket create`; S3: pre-provisioned);
        # errors surface per request instead.

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            kw = {"Bucket": self._bucket}
            if token:
                kw["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kw)
            keys += [o["Key"] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(keys)

    def source(self, key: str) -> tuple[str, dict | None]:
        # Polars (object_store) range-scans this over HTTP. Garage needs
        # path-style addressing, so virtual-hosted is disabled.
        opts = {
            "aws_access_key_id": self._access_key,
            "aws_secret_access_key": self._secret_key,
            "aws_region": self._region,
            "aws_endpoint_url": self._endpoint,
            "aws_virtual_hosted_style_request": "false",
        }
        return f"s3://{self._bucket}/{key}", opts


def make_object_store(config: dict) -> ObjectStore:
    """Pick a backend: S3/Garage when an `endpoint_url` is configured, else a
    local folder (`base`, default `./data/objects`)."""
    if config.get("endpoint_url"):
        return S3ObjectStore(
            config["bucket"],
            endpoint_url=config["endpoint_url"],
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            region=config.get("region", "garage"),
        )
    return LocalObjectStore(config.get("base", "data/objects"))
