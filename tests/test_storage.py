"""The S3 object store as a deployment uses it: path-style for boto3 and Polars,
plain HTTP allowed only for an http:// endpoint, and the readiness check — its
own bucket, its own short timeouts. The last test writes and range-reads real
Parquet over HTTP against a local S3 server (moto), when it's installed."""

import io

import polars as pl
import pytest

from fireflyer import storage
from fireflyer.storage import LocalObjectStore, PrefixedStore

boto3 = pytest.importorskip("boto3")
S3ObjectStore = storage.S3ObjectStore


def _store(endpoint="http://s3.internal:9000", bucket="datasets"):
    return S3ObjectStore(bucket, endpoint_url=endpoint, access_key="key", secret_key="secret", region="us-east-1")


def test_plain_http_is_allowed_only_for_an_http_endpoint():
    uri, opts = _store("http://s3.internal:9000").source("groups/a/x.parquet")
    assert uri == "s3://datasets/groups/a/x.parquet" and opts["aws_allow_http"] == "true"
    _, opts = _store("https://s3.example.com").source("x.parquet")
    assert "aws_allow_http" not in opts                       # TLS checks stay on
    assert opts["aws_virtual_hosted_style_request"] == "false"


def test_boto3_addresses_buckets_by_path_and_the_probe_gives_up_fast():
    store = _store()
    assert store._client.meta.config.s3["addressing_style"] == "path"
    probe = store._probe.meta.config
    assert (probe.connect_timeout, probe.read_timeout, probe.retries["total_max_attempts"]) == (1, 1, 1)
    assert store._client.meta.config.connect_timeout == 60  # uploads keep boto3's own


def test_a_group_store_checks_the_store_it_is_in(tmp_path):
    asked = []

    class Store:
        def check(self):
            asked.append("checked")

    PrefixedStore(Store(), "groups/a/").check()
    assert asked == ["checked"]
    storage.check(object())                                   # no check: taken as ready
    local = LocalObjectStore(str(tmp_path / "data"))
    local.check()
    (tmp_path / "data").rmdir()
    with pytest.raises(OSError):
        storage.check(local)


@pytest.fixture
def s3_server():
    """A local S3 server over plain HTTP (moto), with an empty bucket."""
    server_mod = pytest.importorskip("moto.server")
    server = server_mod.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="key", aws_secret_access_key="secret",
                 region_name="us-east-1").create_bucket(Bucket="datasets")
    yield endpoint
    server.stop()


def test_parquet_written_by_boto3_is_range_read_by_polars_over_http(s3_server):
    store = _store(s3_server)
    store.check()
    frame = pl.DataFrame({"zone": ["a", "b"] * 5000, "weight": range(10000), "other": ["x"] * 10000})
    buffer = io.BytesIO()
    frame.write_parquet(buffer, row_group_size=1000)
    store.put("groups/_admin/datasets/zoo/data.parquet", buffer.getvalue())

    uri, opts = PrefixedStore(store, "groups/_admin/").source("datasets/zoo/data.parquet")
    read = (pl.scan_parquet(uri, storage_options=opts)
            .filter(pl.col("zone") == "b").select(pl.col("weight").sum()).collect())
    assert read.item() == sum(range(1, 10000, 2))
    with pytest.raises(Exception):
        _store(s3_server, bucket="missing").check()
