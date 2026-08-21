"""Inline datasets — CSV carried in the dashboard YAML itself."""

import re

import pytest

import fireflyer as ff
from fireflyer import inline_data


_CSV = "region,amount\nnorth,10\nsouth,5\nnorth,7\n"


def _dashboard(extra_charts="", store=None, csv=_CSV):
    yaml = (
        "name: Inline\n"
        "calcs:\n  sales:\n    total: {name: Total, agg: sum, formula: amount}\n"
        "charts:\n"
        "  t: {type: table, dataset: sales, title: T, columns: [region],"
        " measures: [total], pagination: 0}\n"
        f"{extra_charts}"
        'layout:\n  - ["@30", "t:1"]\n'
        "datasets:\n  sales: |\n"
        + "".join(f"    {line}\n" for line in csv.strip().split("\n"))
    )
    return ff.Dashboard.from_yaml(yaml, datasets=store)


def _rows(dashboard, cid="t"):
    html = dashboard.render_cell(cid, cf_tokens=[])
    body = html[html.index('<article class="fireflyer-chart') :]
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = [
            re.sub(r"<[^>]+>", "|", c).strip("|").split("|")[0]
            for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        ]
        if cells:
            out.append(cells)
    return out


# --- parsing ------------------------------------------------------------------


def test_block_parses_to_name_and_text():
    parsed = inline_data.parse_block({"a": "x\n1\n", "b": "y\n2\n"})
    assert parsed == {"a": "x\n1\n", "b": "y\n2\n"}


def test_absent_block_is_empty():
    assert inline_data.parse_block(None) == {}


@pytest.mark.parametrize("raw", [["not", "a", "mapping"], {"a": None}, {"a": "   "}])
def test_a_malformed_block_explains_itself(raw):
    with pytest.raises(inline_data.InlineDataError) as err:
        inline_data.parse_block(raw)
    assert str(err.value)


@pytest.mark.parametrize("text", ["files/orders.csv", "s3://bucket/o.parquet", "id,amount"])
def test_the_block_takes_csv_and_not_a_path(text):
    """`datasets:` is inline-only. A path would otherwise parse as a perfectly
    valid one-column, zero-row table and fail much later, complaining about a
    column the chart names."""
    with pytest.raises(inline_data.InlineDataError, match="not a\npath|not a path"):
        inline_data.parse_block({"orders": text})


def test_a_broken_csv_names_the_dataset():
    """The message has to say *which* block is wrong — a dashboard may carry
    several, and Polars' own error doesn't know their names."""
    with pytest.raises(inline_data.InlineDataError, match="prices"):
        inline_data.materialize("prices", "a,b\n1,2,3,4,5\n")


# --- the checksum cache -------------------------------------------------------


def test_identical_csv_reuses_one_parquet():
    """Re-rendering a dashboard must not re-parse its data on every keystroke."""
    first = inline_data.materialize("d", _CSV)
    assert inline_data.materialize("d", _CSV) == first


def test_an_edited_csv_lands_on_a_new_parquet():
    """Content-addressed, so an edit can't be served the stale file."""
    first = inline_data.materialize("d", _CSV)
    assert inline_data.materialize("d", _CSV + "east,3\n") != first


def test_the_same_csv_under_two_names_shares_a_file():
    """Keyed by content alone — two dashboards with the same sample data don't
    convert it twice."""
    assert inline_data.materialize("one", _CSV) == inline_data.materialize("two", _CSV)


# --- rendering ----------------------------------------------------------------


def test_a_dashboard_can_carry_its_own_data():
    """No store, no upload, no seeding — the file stands alone."""
    # Sorted: group order isn't fixed without a `sort:` on the chart.
    assert sorted(_rows(_dashboard())) == [["north", "17"], ["south", "5"]]


def test_inline_shadows_a_managed_dataset_of_the_same_name():
    """The block is part of the file being rendered, so reading someone else's
    stored data instead would be a surprise."""
    dashboard = _dashboard(store=lambda name: ("tests/data/orders.parquet", None))
    assert sorted(_rows(dashboard)) == [["north", "17"], ["south", "5"]]


def test_a_name_not_defined_inline_falls_through_to_the_store(orders_parquet):
    yaml = (
        "name: Mixed\n"
        "charts:\n"
        "  a: {type: table, dataset: stored, title: A, pagination: 0}\n"
        "  b: {type: table, dataset: local, title: B, pagination: 0}\n"
        'layout:\n  - ["@30", "a:1"]\n  - ["@30", "b:1"]\n'
        "datasets:\n  local: |\n    only_here\n    1\n"
    )
    dashboard = ff.Dashboard.from_yaml(
        yaml, datasets=lambda name: (orders_parquet, None)
    )
    stored = dashboard.render_cell("a", cf_tokens=[])
    assert "status" in stored          # came from the store
    assert "only_here" in dashboard.render_cell("b", cf_tokens=[])


def test_a_broken_block_renders_an_error_not_a_traceback():
    """Same contract as any other unreadable chart data."""
    with pytest.raises(ff.DashboardError, match="datasets"):
        ff.Dashboard.from_yaml(
            "name: T\ncharts:\n  t: {type: table, dataset: d, title: T}\n"
            'layout:\n  - ["@30", "t:1"]\n'
            "datasets: [not, a, mapping]\n"
        )


# --- how it meets the rest of the system --------------------------------------


def test_inline_names_do_not_pin_a_managed_dataset():
    """`dataset_names` powers the portal's delete-guard. A chart reading inline
    data doesn't hold a stored dataset hostage, nor want renaming when one moves."""
    yaml = (
        "name: T\n"
        "charts:\n"
        "  a: {type: table, dataset: managed_one, title: A}\n"
        "  b: {type: table, dataset: local_one, title: B}\n"
        'layout:\n  - ["@30", "a:1"]\n  - ["@30", "b:1"]\n'
        "datasets:\n  local_one: |\n    x\n    1\n"
    )
    assert ff.Dashboard.dataset_names(yaml) == {"managed_one"}


def test_the_edit_modal_offers_inline_columns():
    """A chart on an inline dataset would otherwise get an empty column
    dropdown — the store has never heard of its data."""
    from fireflyer import config_edit

    yaml = (
        "name: T\n"
        "charts:\n  t: {type: table, dataset: sales, title: T}\n"
        'layout:\n  - ["@30", "t:1"]\n'
        "datasets:\n  sales: |\n    region,amount,rep\n    north,10,ann\n"
    )
    form = config_edit.build_form(yaml, "t")
    offered = set(re.findall(r'<option value="([^"]*)"', form))
    assert {"region", "amount", "rep"} <= offered


def test_calcs_run_against_inline_data():
    """A column calc materializes onto the inline scan like any other."""
    yaml = (
        "name: T\n"
        "calcs:\n  d:\n    day: {name: Day, formula: 'str2dt(raw_day, YYYY-MM-DD)'}\n"
        "    n: {name: Rows, agg: count}\n"
        "charts:\n"
        "  t: {type: table, dataset: d, title: T, columns: [day], measures: [n],"
        " pagination: 0, sort: ['+day']}\n"
        'layout:\n  - ["@30", "t:1"]\n'
        "datasets:\n  d: |\n    raw_day\n    2026-06-02\n    2026-06-01\n    2026-06-01\n"
    )
    rows = _rows(ff.Dashboard.from_yaml(yaml))
    assert rows == [["2026-06-01", "2"], ["2026-06-02", "1"]]
