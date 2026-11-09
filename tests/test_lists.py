"""List columns: text read by `to_list()` into a list of text, and filtered on
what it contains — `in` holds one of the values, `ni` none of them."""

import re

import polars as pl
import pytest

import fireflyer as ff
from fireflyer import column_map, config_edit, filters as filters_mod
from fireflyer.calcs import CalcError, compile_formula
from fireflyer.datasets import DatasetStore
from fireflyer.params import ColumnParam, ParamContext, column_type
from fireflyer.storage import LocalObjectStore

# The shape of the strikes dataset's `parents_generative`: a JSON array of ids, or nothing.
CSV = (
    b"id,parents,tags\n"
    b'1,"[""u1"", ""u2"", ""u3""]",a;b\n'
    b'2,"[""u2""]",b\n'
    b'3,"[""u4"", ""u1""]",\n'
    b"4,,c\n"
    b"5,[],a\n"
)
MAPPING = """columns:
  id:
    formula: to_number(id)
  parents:
    formula: to_list(parents)
  tags:
    formula: to_list(tags, ;)
"""


def _read(formula: str, values: list) -> list:
    expr, _ = compile_formula(formula, pl.col)
    return pl.DataFrame({"x": values}, schema={"x": pl.String}).select(expr.alias("x"))["x"].to_list()


# --- to_list() -----------------------------------------------------------------------


@pytest.mark.parametrize("text, items", [
    ('["a", "b", "c"]', ["a", "b", "c"]),       # JSON
    ("[a, b]", ["a", "b"]),                     # bare brackets
    ("{a,b}", ["a", "b"]),                      # a Postgres array
    ("('a', 'b')", ["a", "b"]),                 # a tuple, single quotes
    ("a, b", ["a", "b"]),                       # plain text
    ("solo", ["solo"]),
    ("[]", []),
    ("[a, , b]", ["a", "b"]),                   # an empty item is no item
])
def test_to_list_reads_every_usual_way_of_writing_a_list(text, items):
    assert _read("to_list(x)", [text]) == [items]


def test_an_empty_cell_is_an_empty_list():
    """A row that lists nothing — which "doesn't contain" keeps."""
    assert _read("to_list(x)", ["", "  ", None, "[]"]) == [[], [], [], []]


def test_to_list_takes_another_separator_quoted_when_its_a_space():
    assert _read("to_list(x, ;)", ["a; b;c"]) == [["a", "b", "c"]]
    assert _read("to_list(x, |)", ["a|b"]) == [["a", "b"]]
    assert _read("to_list(x, ' ')", ["one  two"]) == [["one", "two"]]


@pytest.mark.parametrize("formula, says", [
    ("to_list()", "takes a column name"),
    ("to_list(1)", "takes a column name"),
    ("to_list(x,)", "needs a separator"),
    ("to_list(x", "missing ')'"),
    ("lists(x)", "to_list()"),                  # an unknown function names the real ones
])
def test_a_malformed_to_list_says_how_to_write_it(formula, says):
    with pytest.raises(CalcError, match=re.escape(says)):
        compile_formula(formula, pl.col)


# --- filters: every op against every kind of row --------------------------------------
#
# The truth table, one row kind per case. A list column from `to_list()` is never
# missing (an empty cell is `[]`), but a list from a file can be: it holds nothing too.

ROWS = {
    "holds a value":       ["u1", "u2"],
    "holds another":       ["u3"],
    "holds two of them":   ["u1", "u4"],
    "empty":               [],
    "missing":             None,
}


def _kept(op, values):
    df = pl.DataFrame({"kind": list(ROWS), "p": list(ROWS.values())},
                      schema={"kind": pl.String, "p": pl.List(pl.String)})
    f = filters_mod.normalize([{"column": "p", "op": op, "values": values}])
    return df.filter(*filters_mod.predicates(f, df.schema))["kind"].to_list()


@pytest.mark.parametrize("op, values, kept", [
    # = (in): the list contains any of the values
    ("in", ["u1"],        ["holds a value", "holds two of them"]),
    ("in", ["u1", "u3"],  ["holds a value", "holds another", "holds two of them"]),
    ("in", ["u4"],        ["holds two of them"]),
    ("in", ["nowhere"],   []),
    # ≠ (not in): it contains none of them — an empty or missing list contains none
    ("ni", ["u1"],        ["holds another", "empty", "missing"]),
    ("ni", ["u1", "u3"],  ["empty", "missing"]),
    ("ni", ["nowhere"],   list(ROWS)),
], ids=lambda v: v if isinstance(v, str) else None)
def test_every_op_against_every_kind_of_row(op, values, kept):
    assert _kept(op, values) == kept


def test_in_and_not_in_split_the_rows_between_them():
    """For the same values, every row is kept by exactly one of the two."""
    for values in (["u1"], ["u1", "u3"], ["nowhere"]):
        assert sorted(_kept("in", values) + _kept("ni", values)) == sorted(ROWS)


def test_an_item_is_matched_whole_and_by_case():
    """A value is one whole item, not part of one — `u` finds nothing in `u1`."""
    assert _kept("in", ["u"]) == [] and _kept("in", ["U1"]) == []


def test_a_range_on_a_list_is_refused():
    with pytest.raises(filters_mod.FilterError, match="list column"):
        _kept("between", ["a", "b"])


def test_a_list_reads_as_its_items_joined():
    df = pl.DataFrame({"p": [["u1", "u2"], [], None]}, schema={"p": pl.List(pl.String)})
    assert df.select(filters_mod.as_text("p", df.schema["p"]))["p"].to_list() == ["u1, u2", "", None]


# --- a dataset, end to end ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    datasets = DatasetStore(LocalObjectStore(str(tmp_path)))
    datasets.create("strikes", CSV)
    datasets.remap("strikes", MAPPING)
    return datasets


def test_a_mapping_builds_a_list_column_once(store):
    ds = store.get("strikes")
    types = {c.name: c.dtype for c in ds.columns}
    assert types["parents"] == "List(String)" and types["tags"] == "List(String)"
    assert column_type(types["parents"]) == "list"
    uri, _ = store.source("strikes")
    assert pl.read_parquet(uri)["parents"].to_list() == [["u1", "u2", "u3"], ["u2"], ["u4", "u1"], [], []]
    assert store.examples("strikes")["parents"][:2] == ["u1, u2, u3", "u2"]


def _table(store, filters="", columns="[id, parents, tags]"):
    text = (f"name: T\ncharts:\n  t:\n    type: table\n    dataset: strikes\n    title: Rows\n"
            f"    columns: {columns}\n{filters}layout:\n  - ['@40', t]\n")
    return ff.Dashboard.from_yaml(text, datasets=store).render_cell("t")


def _shown_ids(html):
    return re.findall(r'<tr[^>]*>\s*<td[^>]*>(\d)</td>', html)


def test_a_table_shows_a_lists_items_and_filters_on_what_it_holds(store):
    html = _table(store, "    filters: [{column: parents, op: in, values: [u1]}]\n")
    assert _shown_ids(html) == ["1", "3"]
    assert ">u1, u2, u3<" in html and "[&#39;u1" not in html and "['u1" not in html   # items, not a repr
    html = _table(store, "    filters: [{column: tags, op: ni, values: [a]}]\n")
    assert _shown_ids(html) == ["2", "3", "4"]                     # 3 lists no tags


def test_a_tables_search_finds_a_lists_item(store):
    text = ("name: T\ncharts:\n  t: {type: table, dataset: strikes, title: Rows, columns: [id, parents]}\n"
            "layout:\n  - ['@40', t]\n")
    html = ff.Dashboard.from_yaml(text, datasets=store).render_cell("t", table_query="u4")
    assert _shown_ids(html) == ["3"]


def test_a_click_on_a_row_names_it_by_its_other_columns(store):
    """A list is many values, so it's no part of the row's crossfilter token."""
    html = _table(store)
    tokens = re.findall(r'"toggle": "([^"]+)"', html)
    assert tokens and all("parents" not in t and "tags" not in t for t in tokens)


def test_the_value_picker_lists_a_lists_items(store):
    config = {"datasets": {}}
    choices = config_edit._distinct(config, "strikes", "parents", resolve=store.source)
    assert choices.values == ["u1", "u2", "u3", "u4"]
    assert config_edit._present(config, "strikes", "parents", ["u4", "zz"], resolve=store.source) == ["u4"]


def test_a_list_column_is_no_axis_or_slice():
    """A pie's column, a bar's x: one value per row — a list is left out."""
    ctx = ParamContext(columns=["id", "parents"], column_types={"id": "number", "parents": "list"})
    html = ColumnParam("column", "Column").render(None, ctx)
    assert ">id<" in html and ">parents<" not in html


# --- every place a filter is set -------------------------------------------------------
#
# The rows of CSV: 1 [u1 u2 u3] · 2 [u2] · 3 [u4 u1] · 4 [] (empty cell) · 5 [] ("[]").

def _dashboard(store, extra_calcs="", chart="t: {type: table, dataset: strikes, title: Rows, columns: [id, parents]}"):
    text = (f"name: T\ncalcs:\n  strikes:\n    n: {{agg: count}}\n{extra_calcs}"
            f"charts:\n  {chart}\nlayout:\n  - ['@40', {chart.split(':')[0]}]\n")
    return ff.Dashboard.from_yaml(text, datasets=store)


def _number(html):
    return re.findall(r'class="fireflyer-number-value[^"]*"[^>]*>\s*([^<\s]+)', html)


@pytest.mark.parametrize("op, values, ids", [
    ("in", ["u1"], ["1", "3"]),
    ("in", ["u2", "u4"], ["1", "2", "3"]),
    ("ni", ["u1"], ["2", "4", "5"]),
    ("ni", ["u1", "u2"], ["4", "5"]),
])
def test_a_charts_own_filter(store, op, values, ids):
    html = _table(store, f"    filters: [{{column: parents, op: {op}, values: {values}}}]\n", "[id, parents]")
    assert _shown_ids(html) == ids


def test_a_calcs_own_filter_counts_what_contains(store):
    """A conditional count: orders whose parents hold u1, and those whose don't."""
    calcs = ("    with_u1: {agg: count, filters: [{column: parents, op: in, values: [u1]}]}\n"
             "    without_u1: {agg: count, filters: [{column: parents, op: ni, values: [u1]}]}\n")
    for key, expected in (("with_u1", "2"), ("without_u1", "3")):
        html = _dashboard(store, calcs, f"c: {{type: number, dataset: strikes, title: N, calc: {key}}}").render_cell("c")
        assert _number(html) == [expected]


def test_a_number_charts_filter(store):
    html = _dashboard(store, chart="c: {type: number, dataset: strikes, title: N, calc: n, "
                      "filters: [{column: parents, op: ni, values: [u2]}]}").render_cell("c")
    assert _number(html) == ["3"]                                  # 3, 4 and 5


@pytest.mark.parametrize("op, values, ids", [
    ("in", ["u4"], ["3"]),
    ("ni", ["u4"], ["1", "2", "4", "5"]),
])
def test_a_viewers_global_quick_filter(store, op, values, ids):
    """Typed into a chart's filter panel, applied across the dashboard."""
    token = filters_mod.global_token("parents", op, values)
    assert _shown_ids(_dashboard(store).render_cell("t", cf_tokens=[token])) == ids


@pytest.mark.parametrize("op, values, ids", [
    ("in", ["u1"], ["1", "3"]),
    ("ni", ["u1"], ["2", "4", "5"]),
])
def test_a_roles_rows(store, op, values, ids):
    """A role granted the dataset with a filter sees just those rows."""
    grants = {"strikes": [{"column": "parents", "op": op, "values": values}]}
    assert _shown_ids(_dashboard(store).as_role("viewer", grants).render_cell("t")) == ids


def test_a_roles_rows_counted_on_the_dataset_page(store):
    assert store.rows_seen("strikes", [{"column": "parents", "op": "in", "values": ["u1"]}]) == 2
    assert store.rows_seen("strikes", [{"column": "parents", "op": "ni", "values": ["u1"]}]) == 3


def test_filters_on_a_list_and_another_column_and_together(store):
    html = _table(store, "    filters: [{column: parents, op: ni, values: [u1]}, {column: tags, op: in, values: [c]}]\n",
                  "[id, parents]")
    assert _shown_ids(html) == ["4"]
