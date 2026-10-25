"""DatasetStore tests — exercise `fireflyer.datasets` with a `LocalObjectStore`
in a tmp dir (no live S3). Covers CSV→Parquet conversion, schema/preview,
unique-name enforcement, replace, rename, delete — and versions: uploads as
majors, mappings as minors, builds, and both catalogs."""

import pytest

from fireflyer.datasets import DatasetError, DatasetStore
from fireflyer.storage import LocalObjectStore

CSV = b"id,status,amount\n1,paid,11\n2,paid,178\n3,refunded,13\n"


def _rows(store, name, n):
    """The first `n` rows charts would read."""
    import polars as pl

    uri, opts = store.source(name)
    df = pl.scan_parquet(uri, storage_options=opts).head(n).collect()
    return df.columns, [list(r) for r in df.rows()]


@pytest.fixture
def store(tmp_path):
    return DatasetStore(LocalObjectStore(str(tmp_path / "objects")))


def test_create_converts_and_records_schema(store):
    ds = store.create("orders", CSV, description="Q3 orders", delimiter=",", author="dana")
    assert ds.name == "orders" and ds.rows == 3 and ds.author == "dana"
    assert ds.description == "Q3 orders"
    # Every column is text until the mapping says otherwise.
    assert [(c.name, c.dtype) for c in ds.columns] == [
        ("id", "String"),
        ("status", "String"),
        ("amount", "String"),
    ]
    assert ds.version == "1.0" and not ds.building and not ds.error
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


def test_the_data_reads_as_text_until_mapped(store):
    store.create("orders", CSV)
    cols, rows = _rows(store, "orders", 2)
    assert cols == ["id", "status", "amount"]
    assert rows == [["1", "paid", "11"], ["2", "paid", "178"]]


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


# --- versions -------------------------------------------------------------------

TYPED = """columns:
  id:
    formula: to_number(id)
  state:
    formula: status
  amount:
    formula: to_number(amount)
"""


def test_a_mapping_is_the_next_minor_and_types_the_columns(store):
    store.create("orders", CSV)
    ds = store.remap("orders", TYPED, author="sam")
    assert ds.version == "1.1"
    assert [(c.name, c.dtype) for c in ds.columns] == [
        ("id", "Int64"), ("state", "String"), ("amount", "Int64"),
    ]
    assert _rows(store, "orders", 1)[1] == [[1, "paid", 11]]


def test_an_upload_is_the_next_major_and_carries_the_mapping(store):
    store.create("orders", CSV)
    store.remap("orders", TYPED)
    ds = store.replace("orders", b"id,status,amount,note\n9,paid,1.5,x\n")
    assert ds.version == "2.2"            # a new column: the carried mapping changed
    # Kept: id/state/amount as mapped. New: `note`, as text.
    assert [(c.name, c.dtype) for c in ds.columns][-2:] == [("amount", "Float64"), ("note", "String")]  # 1.5
    assert [v.label for v in store.versions("orders")] == ["2.2", "1.1", "1.0"]


def test_every_original_is_kept_and_only_the_latest_parquet(store, tmp_path):
    store.create("orders", CSV)
    store.remap("orders", TYPED)
    store.replace("orders", CSV)
    files = sorted(p.relative_to(tmp_path / "objects").as_posix() for p in (tmp_path / "objects").rglob("*") if p.is_file())
    assert files == [
        "datasets/orders/data.parquet", "datasets/orders/dataset.yaml",
        "datasets/orders/raw/1.csv", "datasets/orders/raw/2.csv",
    ]


def test_a_cast_that_fails_keeps_the_previous_version(store):
    store.create("orders", CSV)
    bad = "columns:\n  status:\n    formula: to_number(status)\n"
    with pytest.raises(DatasetError, match="status.*aren't numbers.*paid"):
        store.remap("orders", bad)
    ds = store.get("orders")
    assert ds.version == "1.0" and "1.1" in ds.error
    assert [v.label for v in store.versions("orders")] == ["1.0"]


def test_a_background_build_publishes_only_when_done(store):
    store.create("orders", CSV)
    ds = store.remap("orders", TYPED, build=False)
    assert ds.version == "1.0" and ds.building == "1.1"   # charts still read 1.0
    assert store.build("orders") == {"status": "state"}  # the rename to cascade
    assert store.get("orders").version == "1.1" and not store.get("orders").building
    assert store.build("orders") is None                  # nothing left to build


def test_a_newer_edit_replaces_one_still_waiting(store):
    store.create("orders", CSV)
    store.remap("orders", TYPED, build=False)
    store.remap("orders", "columns:\n  id: to_number(id)\n", build=False)
    store.build("orders")
    assert [v.label for v in store.versions("orders")] == ["1.1", "1.0"]
    assert store.version("orders", "1.1").mapping == "columns:\n  id:\n    formula: to_number(id)\n"


def test_an_unchanged_or_unfitting_mapping_is_refused(store):
    store.create("orders", CSV)
    with pytest.raises(DatasetError, match="hasn't changed"):
        store.remap("orders", store.mapping("orders"))
    with pytest.raises(DatasetError, match="no column 'nope'"):
        store.remap("orders", "columns:\n  x: nope\n")


def test_deleting_a_major_drops_its_minors_but_never_the_one_in_use(store, tmp_path):
    store.create("orders", CSV)
    store.remap("orders", TYPED)
    store.replace("orders", CSV)
    with pytest.raises(DatasetError, match="in use"):
        store.delete_major("orders", 2)
    store.delete_major("orders", 1)
    assert [v.label for v in store.versions("orders")] == ["2.1"]
    assert not (tmp_path / "objects" / "datasets" / "orders" / "raw" / "1.csv").exists()


def test_renaming_moves_every_file_and_the_history(store):
    store.create("orders", CSV)
    store.remap("orders", TYPED)
    store.rename("orders", "sales")
    assert store.get("orders") is None
    assert store.get("sales").version == "1.1"
    assert [v.label for v in store.versions("sales")] == ["1.1", "1.0"]


def test_the_portal_keeps_the_yaml_in_its_database(tmp_path):
    from fireflyer.datasets import SqlCatalog

    catalog = SqlCatalog(":memory:")
    store = DatasetStore(LocalObjectStore(str(tmp_path / "objects")), catalog)
    store.create("orders", CSV, author="dana")
    store.remap("orders", TYPED)
    assert catalog.names() == ["orders"]
    assert "version" not in (catalog.load("orders") or "")[:0]   # a YAML text column
    assert "  state:\n        formula: status\n" in catalog.load("orders")
    assert not (tmp_path / "objects" / "datasets" / "orders" / "dataset.yaml").exists()
    assert store.get("orders").version == "1.1"


def test_a_dataset_from_before_versions_becomes_1_0_with_its_types(tmp_path):
    import io

    import polars as pl
    import yaml

    from fireflyer.storage import LocalObjectStore as L

    objects = L(str(tmp_path / "objects"))
    buf = io.BytesIO()
    pl.DataFrame({"id": [1, 2], "status": ["a", "b"]}).write_parquet(buf)
    objects.put("old.parquet", buf.getvalue())
    objects.put("old.yaml", yaml.safe_dump({
        "name": "old", "description": "d", "delimiter": ",", "rows": 2, "author": "dana",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "columns": [{"name": "id", "dtype": "Int64"}, {"name": "status", "dtype": "String"}],
    }).encode())
    ds = DatasetStore(objects).get("old")
    assert ds.version == "1.0" and ds.description == "d"
    assert [(c.name, c.dtype) for c in ds.columns] == [("id", "Int64"), ("status", "String")]
    assert "  id:\n    formula: to_number(id)\n" in DatasetStore(objects).mapping("old")
    assert not objects.exists("old.parquet")


@pytest.mark.parametrize("bad", ["a/b", "../x", ".hidden", "a\\b"])
def test_a_name_that_would_leave_its_folder_is_refused(store, bad):
    with pytest.raises(DatasetError, match="can't contain"):
        store.create(bad, CSV)


def test_examples_come_from_the_data_charts_read(store):
    """Computed and renamed columns included — keyed by the columns charts see."""
    store.create("orders", CSV)
    store.remap("orders", TYPED + "  big:\n    formula: to_number(amount) * 10\n")
    assert store.examples("orders") == {
        "id": ["1", "2", "3"], "state": ["paid", "refunded"], "amount": ["11", "178", "13"],
        "big": ["110", "1780", "130"],
    }


def test_restoring_an_older_version_is_the_next_one(store):
    """Going back builds anew — history only grows, so it can be undone too."""
    store.create("orders", CSV)
    store.remap("orders", TYPED)
    ds = store.remap("orders", store.restored("orders", "1.0"))
    assert ds.version == "1.2"
    assert store.version("orders", "1.2").mapping == store.version("orders", "1.0").mapping


def test_restoring_a_version_of_an_older_upload_fits_it_to_the_latest(store):
    store.create("orders", CSV)
    store.remap("orders", TYPED)
    store.replace("orders", b"id,amount,note\n1,2,x\n")       # `status` gone, `note` new
    assert store.restored("orders", "1.1") == (
        "columns:\n  id:\n    formula: to_number(id)\n  amount:\n    formula: to_number(amount)\n"
        "  note:\n    formula: note\n"
    )


def test_a_service_column_is_in_the_dataset_but_not_in_what_charts_read(store):
    """Built, typed and shown — but left out of the data, so no chart can use it,
    and out of the columns dashboards' editors are offered."""
    import polars as pl

    store.create("orders", CSV)
    ds = store.remap("orders", "columns:\n  id:\n    formula: id\n    service: true\n  amount: to_number(amount)\n")
    assert [c.name for c in ds.columns] == ["amount"]
    assert [(c.name, c.dtype) for c in ds.service] == [("id", "String")]
    uri, opts = store.source("orders")
    assert pl.scan_parquet(uri).collect_schema().names() == ["amount"]
    assert store.examples("orders")["id"] == ["1", "2", "3"]          # still shown on its page


def test_the_minor_counts_mapping_changes_and_never_resets(store):
    """An upload keeps it unless the mapping it carries changes; a saved mapping
    is the next one, whatever the major."""
    store.create("orders", CSV)                                   # 1.0
    store.remap("orders", TYPED)                                  # 1.1
    assert store.replace("orders", CSV).version == "2.1"          # same columns: same mapping
    assert store.remap("orders", TYPED + "  big: to_number(amount) * 2\n").version == "2.2"
    assert store.replace("orders", CSV).version == "3.2"
