"""Dashboard version history: every save numbered, timed and attributed, for
the editor's compare view. Both stores — the portal's SQL one and local paths
mode's files — behave the same."""

import pytest

from fireflyer.web.paths import PathDashboardStore
from fireflyer.web.portal import SqliteStore

V1 = "name: Sales\ncharts: {}\nlayout: []\n"
V2 = "name: Sales\ndescription: Weekly.\ncharts: {}\nlayout: []\n"
V3 = "name: Sales\ndescription: Monthly.\ncharts: {}\nlayout: []\n"


@pytest.fixture(params=["sql", "paths"])
def store(request, tmp_path):
    if request.param == "sql":
        return SqliteStore(":memory:")
    return PathDashboardStore(str(tmp_path / "path"), history=str(tmp_path / "data" / "history"))


def test_each_save_is_the_next_version_with_its_author(store):
    id = store.create(V1, "dana")
    store.save(id, V2, author="sam")
    store.save(id, V3, author="dana")
    assert [(v.version, v.author) for v in store.versions(id)] == [(3, "dana"), (2, "sam"), (1, "dana")]
    assert [store.version(id, n).yaml for n in (1, 2, 3)] == [V1, V2, V3]


def test_a_save_that_changes_nothing_records_nothing(store):
    id = store.create(V1, "dana")
    store.save(id, V1, author="sam")
    assert [v.version for v in store.versions(id)] == [1]


def test_versions_carry_a_unix_time_and_list_without_their_yaml(store):
    import time

    id = store.create(V1, "dana")
    (v,) = store.versions(id)
    assert abs(v.created_at - time.time()) < 60 and v.yaml == ""


def test_an_unknown_version_is_none(store):
    id = store.create(V1, "dana")
    assert store.version(id, 9) is None


def test_deleting_a_dashboard_drops_its_history(store):
    id = store.create(V1, "dana")
    store.save(id, V2, author="sam")
    store.delete(id)
    assert store.versions(id) == []


def test_a_dashboard_from_before_history_gets_its_state_as_version_1():
    """Its first save afterwards shouldn't leave nothing to compare against."""
    store = SqliteStore(":memory:")
    id = store.create(V1, "dana")
    store._run("DELETE FROM dashboard_versions", ())       # as if created before history
    store.save(id, V2, author="sam")
    assert [(v.version, v.author) for v in store.versions(id)] == [(2, "sam"), (1, "dana")]
    assert store.version(id, 1).yaml == V1


def test_local_history_stays_out_of_the_dashboards_folder(tmp_path):
    """The dashboards folder is yours, and may be a git repo."""
    store = PathDashboardStore(str(tmp_path / "path"), history=str(tmp_path / "data" / "history"))
    id = store.create(V1)
    store.save(id, V2)
    assert sorted(p.name for p in (tmp_path / "path" / "dashboards").iterdir()) == [f"{id}.yaml"]
    assert sorted(p.name for p in (tmp_path / "data" / "history" / id).iterdir()) == ["1.json", "2.json"]


def test_no_history_folder_means_no_history(tmp_path):
    store = PathDashboardStore(str(tmp_path / "path"))
    id = store.create(V1)
    store.save(id, V2)
    assert store.versions(id) == []
