"""DatasetStore tests — exercise `fireflyer.datasets` with a `LocalObjectStore`
in a tmp dir (no live S3). Covers CSV→Parquet conversion, schema/preview,
unique-name enforcement, replace, rename, and delete."""

import pytest

from fireflyer.datasets import DatasetError, DatasetStore
from fireflyer.storage import LocalObjectStore

CSV = b"id,status,amount\n1,paid,11\n2,paid,178\n3,refunded,13\n"


@pytest.fixture
def store(tmp_path):
    return DatasetStore(LocalObjectStore(str(tmp_path / "objects")))


def test_create_converts_and_records_schema(store):
    ds = store.create("orders", CSV, description="Q3 orders", delimiter=",", author="dana")
    assert ds.name == "orders" and ds.rows == 3 and ds.author == "dana"
    assert ds.description == "Q3 orders"
    assert [(c.name, c.dtype) for c in ds.columns] == [
        ("id", "Int64"),
        ("status", "String"),
        ("amount", "Int64"),
    ]
    assert [d.name for d in store.list()] == ["orders"]


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


def test_create_rejects_duplicate_name(store):
    store.create("orders", CSV)
    with pytest.raises(DatasetError):
        store.create("orders", CSV)


def test_create_rejects_blank_name_and_empty_csv(store):
    with pytest.raises(DatasetError):
        store.create("  ", CSV)
    with pytest.raises(DatasetError):
        store.create("x", b"")  # empty upload is unreadable


def test_preview_returns_columns_and_rows(store):
    store.create("orders", CSV)
    cols, rows = store.preview("orders", n=2)
    assert cols == ["id", "status", "amount"]
    assert rows == [[1, "paid", 11], [2, "paid", 178]]


def test_source_is_scannable_parquet(store):
    import polars as pl

    store.create("orders", CSV)
    uri, opts = store.source("orders")
    # A chart reads exactly like this — projected + filtered, not the whole file.
    got = (
        pl.scan_parquet(uri, storage_options=opts)
        .select("status")
        .filter(pl.col("status") == "paid")
        .collect()
        .height
    )
    assert got == 2


def test_replace_keeps_author_updates_data(store):
    store.create("orders", CSV, author="dana")
    store.replace("orders", CSV + b"4,paid,50\n")
    ds = store.get("orders")
    assert ds.rows == 4 and ds.author == "dana"


def test_rename_moves_data_and_frees_old_name(store):
    store.create("orders", CSV)
    store.rename("orders", "sales")
    assert store.get("orders") is None
    assert store.get("sales").rows == 3
    assert [c.name for c in store.get("sales").columns] == ["id", "status", "amount"]


def test_rename_rejects_taken_name(store):
    store.create("orders", CSV)
    store.create("sales", CSV)
    with pytest.raises(DatasetError):
        store.rename("orders", "sales")


def test_delete_removes_dataset(store):
    store.create("orders", CSV)
    store.delete("orders")
    assert store.list() == []


def test_make_object_store_defaults_to_local(tmp_path):
    from fireflyer.storage import LocalObjectStore, make_object_store

    s = make_object_store({"base": str(tmp_path / "objs")})
    assert isinstance(s, LocalObjectStore)
    s.put("k.txt", b"hi")
    assert s.get("k.txt") == b"hi" and s.list_keys() == ["k.txt"]


def test_rename_updates_description(store):
    store.create("orders", CSV, description="old")
    store.rename("orders", "orders", description="new desc")   # same name, edit desc
    assert store.get("orders").description == "new desc"


def test_rename_keeps_description_when_none(store):
    store.create("orders", CSV, description="keep")
    store.rename("orders", "sales")   # description=None -> unchanged
    assert store.get("sales").description == "keep"
