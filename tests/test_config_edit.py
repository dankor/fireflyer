"""Tests for the surgical chart-config edit (build form / emit / replace / apply)."""

import pytest
import yaml

import fireflyer as ff
from fireflyer import config_edit as ce


class FakeForm:
    def __init__(self, single=None, multi=None):
        self._single = single or {}
        self._multi = multi or {}

    def get(self, key, default=None):
        return self._single.get(key, default)

    def getlist(self, key):
        return self._multi.get(key, [])


def _doc(csv_path: str) -> str:
    # Includes comments + two charts so we can prove non-target content survives.
    return f"""datasets:
  orders:
    path: {csv_path}

charts:
  revenue:
    type: number
    dataset: orders
    title: Revenue        # a comment inside the block
    column: amount
    agg: sum

  by_status:              # sibling — must stay byte-for-byte
    type: pie
    dataset: orders
    title: By status
    column: status

dashboard:
  - ["@20", "revenue:1", "by_status:1"]
"""


def test_build_form_lists_params(orders_csv):
    html = ce.build_form(_doc(orders_csv), "revenue")
    assert 'name="agg"' in html          # choice widget
    assert 'name="column"' in html       # column widget
    assert "ff-filter-add" in html       # filter builder
    assert 'data-cid="revenue"' in html


def test_build_form_unknown_chart_raises(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="unknown chart"):
        ce.build_form(_doc(orders_csv), "ghost")


def test_build_form_has_type_dropdown_with_all_types(orders_csv):
    html = ce.build_form(_doc(orders_csv), "revenue")
    assert "data-type-select" in html
    for t in ("number", "pie", "bar", "map", "table"):
        assert f'value="{t}"' in html
    assert 'value="number" selected' in html   # revenue is a number chart


def test_build_form_type_override_switches_fields(orders_csv):
    """Overriding the type re-renders the fields for that type: a pie has a
    column but no aggregation."""
    html = ce.build_form(_doc(orders_csv), "revenue", type_override="pie")
    assert 'value="pie" selected' in html
    assert 'name="column"' in html
    assert 'name="agg"' not in html            # agg is number-only


def test_apply_edit_swaps_chart_type(orders_csv):
    """Submitting a different `type` rewrites the block with the new type's
    params and drops the old ones."""
    form = FakeForm(
        single={"type": "pie", "dataset": "orders", "title": "Revenue", "column": "status"},
        multi={"filter_column": [], "filter_op": [], "filter_values": []},
    )
    new_text = ce.apply_edit(_doc(orders_csv), "revenue", form)
    block = yaml.safe_load(new_text)["charts"]["revenue"]
    assert block["type"] == "pie"
    assert block["column"] == "status"
    assert "agg" not in block                  # number-only key gone
    # Sibling untouched; whole doc still valid.
    assert "title: By status" in new_text
    ff.Dashboard.from_yaml(new_text)


def test_replace_chart_block_preserves_siblings_and_reparses(orders_csv):
    text = _doc(orders_csv)
    form = FakeForm(
        single={"dataset": "orders", "title": "Revenue", "column": "amount",
                "agg": "max", "format": "compact"},
        multi={"filter_column": ["status"], "filter_op": ["in"],
               "filter_values": ["paid, pending"]},
    )
    new_text = ce.apply_edit(text, "revenue", form)

    # Target block changed.
    cfg = yaml.safe_load(new_text)["charts"]["revenue"]
    assert cfg["agg"] == "max"
    assert cfg["filters"] == [{"column": "status", "op": "in", "values": ["paid", "pending"]}]

    # Sibling block untouched, verbatim (comment included).
    assert "by_status:              # sibling — must stay byte-for-byte" in new_text
    assert "title: By status" in new_text
    # Datasets + dashboard preserved.
    assert new_text.startswith("datasets:")
    assert '- ["@20", "revenue:1", "by_status:1"]' in new_text
    # Whole doc still parses as a dashboard.
    ff.Dashboard.from_yaml(new_text)


def test_emit_drops_none_and_empty(orders_csv):
    """An unset nullable int (map zoom) and an empty filter list emit no key."""
    text = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  m:
    type: map
    dataset: o
    title: M
    lat: lat
    lng: lng
    grid_size: 16
dashboard:
  - ["@20", "m:1"]
"""
    form = FakeForm(
        single={"dataset": "o", "title": "M", "lat": "lat", "lng": "lng",
                "grid_size": "20", "zoom": ""},  # zoom blank -> None
        multi={"filter_column": [], "filter_op": [], "filter_values": []},  # no filters
    )
    new_text = ce.apply_edit(text, "m", form)
    block = yaml.safe_load(new_text)["charts"]["m"]
    assert "zoom" not in block           # None dropped
    assert "filters" not in block        # empty list dropped
    assert block["grid_size"] == 20


def test_build_add_form_defaults_and_placement(orders_csv):
    html = ce.build_add_form(_doc(orders_csv), "table", "row", "end")
    assert "data-type-select" in html
    assert 'name="add_mode" value="row"' in html
    assert 'name="add_index" value="end"' in html
    assert 'value="orders" selected' in html   # default dataset pre-filled
    assert ">Add<" in html                       # Add button, not Save


def test_add_chart_new_row_at_end(orders_csv):
    form = FakeForm(
        single={"type": "pie", "dataset": "orders", "title": "By status",
                "column": "status", "add_mode": "row", "add_index": "end"},
        multi={"filter_column": [], "filter_op": [], "filter_values": []},
    )
    new_text = ce.add_chart(_doc(orders_csv), form)
    cfg = yaml.safe_load(new_text)
    # New chart added under charts:, id derived from type.
    assert cfg["charts"]["pie"]["type"] == "pie"
    assert cfg["charts"]["pie"]["column"] == "status"
    # New row appended in the layout.
    assert '"pie:1"' in new_text
    ff.Dashboard.from_yaml(new_text)


def test_add_chart_to_existing_row(orders_csv):
    form = FakeForm(
        single={"type": "table", "dataset": "orders", "title": "Orders",
                "add_mode": "cell", "add_index": "0"},
        multi={"filter_column": [], "filter_op": [], "filter_values": []},
    )
    new_text = ce.add_chart(_doc(orders_csv), form)
    # The one dashboard row (revenue + by_status) gained a third cell.
    row = [ln for ln in new_text.splitlines() if '"revenue:1"' in ln][0]
    assert '"table:1"' in row
    ff.Dashboard.from_yaml(new_text)


def test_add_chart_generates_unique_id(orders_csv):
    """Adding a second chart of a type that already exists gets a suffixed id."""
    form = FakeForm(
        single={"type": "pie", "dataset": "orders", "title": "P", "column": "status",
                "add_mode": "row", "add_index": "end"},
        multi={"filter_column": [], "filter_op": [], "filter_values": []},
    )
    once = ce.add_chart(_doc(orders_csv), form)
    twice = ce.add_chart(once, form)
    charts = yaml.safe_load(twice)["charts"]
    assert "pie" in charts and "pie_2" in charts


def test_delete_chart_from_shared_row_keeps_siblings(orders_csv):
    """Deleting one chart in a two-chart row leaves the other in place."""
    new_text = ce.delete_chart(_doc(orders_csv), "by_status")
    cfg = yaml.safe_load(new_text)
    assert "by_status" not in cfg["charts"]        # block gone
    assert "revenue" in cfg["charts"]              # sibling kept
    row = [ln for ln in new_text.splitlines() if '"revenue:1"' in ln][0]
    assert "by_status" not in row                  # placement gone
    ff.Dashboard.from_yaml(new_text)


def test_delete_chart_drops_now_empty_row(orders_csv):
    """A chart that is the only cell in its row takes the row with it."""
    text = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: pie, dataset: o, title: B, column: status}}
dashboard:
  - ["@20", "a:1"]
  - ["@20", "b:1"]
"""
    new_text = ce.delete_chart(text, "a")
    assert '"a:1"' not in new_text
    assert '"b:1"' in new_text                     # other row survives
    assert new_text.count("- [") == 1              # a's row dropped
    ff.Dashboard.from_yaml(new_text)


def test_delete_unknown_chart_raises(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="unknown chart"):
        ce.delete_chart(_doc(orders_csv), "ghost")


def test_insert_header_before_row(orders_csv):
    new_text = ce.insert_layout_item(_doc(orders_csv), "header", "0")
    lines = [ln.strip() for ln in new_text.splitlines()]
    assert "- New header" in lines
    # It lands above the first row (which holds revenue).
    hi = lines.index("- New header")
    ri = next(i for i, ln in enumerate(lines) if '"revenue:1"' in ln)
    assert hi < ri
    ff.Dashboard.from_yaml(new_text)


def test_insert_separator_at_end(orders_csv):
    new_text = ce.insert_layout_item(_doc(orders_csv), "separator", "end")
    assert new_text.rstrip().endswith('- "-"')
    ff.Dashboard.from_yaml(new_text)


def test_insert_unknown_kind_raises(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="unknown layout item"):
        ce.insert_layout_item(_doc(orders_csv), "widget", "end")


def _headed_doc(csv_path: str) -> str:
    return f"""datasets:
  o: {{path: {csv_path}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: pie, dataset: o, title: B, column: status}}
dashboard:
  - Overview
  - ["@20", "a:1"]
  - Details
  - "-"
  - ["@20", "b:1"]
"""


def test_set_header_text_by_index(orders_csv):
    text = _headed_doc(orders_csv)
    out = ce.set_header_text(text, 1, "Line items")   # second header = "Details"
    lines = [ln.strip() for ln in out.splitlines()]
    assert "- Overview" in lines                       # first header untouched
    assert "- Line items" in lines
    assert "- Details" not in lines
    assert '- "-"' in lines                            # separator intact
    ff.Dashboard.from_yaml(out)


def test_set_header_text_quotes_when_unsafe(orders_csv):
    """A colon would otherwise turn the item into a mapping, so it's quoted."""
    out = ce.set_header_text(_headed_doc(orders_csv), 0, "Revenue: paid")
    assert '- "Revenue: paid"' in out
    ff.Dashboard.from_yaml(out)


def test_set_header_text_validates(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="header 9 not found"):
        ce.set_header_text(_headed_doc(orders_csv), 9, "x")
    with pytest.raises(ce.ConfigEditError, match="cannot be empty"):
        ce.set_header_text(_headed_doc(orders_csv), 0, "   ")


# _headed_doc layout items, by full-list index:
#   0 Overview (header)  1 [a] (row)  2 Details (header)  3 "-" (sep)  4 [b] (row)
def _item_lines(text: str) -> list[str]:
    di = text.splitlines().index("dashboard:")
    return [ln.strip() for ln in text.splitlines()[di + 1:] if ln.strip()]


def test_move_layout_item_separator_between_rows(orders_csv):
    # Separator (item 3) up to before the first row (item 1).
    out = ce.move_layout_item(_headed_doc(orders_csv), 3, "1")
    items = _item_lines(out)
    assert items.index('- "-"') < items.index('- ["@20", "a:1"]')
    assert items.count('- "-"') == 1
    ff.Dashboard.from_yaml(out)


def test_move_layout_item_header_to_end(orders_csv):
    # "Overview" (item 0) to the end of the layout.
    out = ce.move_layout_item(_headed_doc(orders_csv), 0, "end")
    assert _item_lines(out)[-1] == "- Overview"
    ff.Dashboard.from_yaml(out)


def test_move_layout_item_rejects_a_row(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="only headers and separators"):
        ce.move_layout_item(_headed_doc(orders_csv), 1, "end")


def test_move_layout_item_out_of_range(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="layout item 9 not found"):
        ce.move_layout_item(_headed_doc(orders_csv), 9, "end")


def test_delete_layout_item_header(orders_csv):
    out = ce.delete_layout_item(_headed_doc(orders_csv), 2)   # "Details"
    items = _item_lines(out)
    assert "- Details" not in items
    assert "- Overview" in items                              # other header intact
    assert '- "-"' in items                                   # separator intact
    ff.Dashboard.from_yaml(out)


def test_delete_layout_item_separator(orders_csv):
    out = ce.delete_layout_item(_headed_doc(orders_csv), 3)   # the "-"
    assert '- "-"' not in _item_lines(out)
    ff.Dashboard.from_yaml(out)


def test_delete_layout_item_rejects_a_row(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="only headers and separators"):
        ce.delete_layout_item(_headed_doc(orders_csv), 1)


def _move_doc(csv_path: str) -> str:
    return f"""datasets:
  o: {{path: {csv_path}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  b: {{type: pie, dataset: o, title: B, column: status}}
  c: {{type: number, dataset: o, title: C, column: amount, agg: sum}}
dashboard:
  - ["@20", "a:3", "b:2"]
  - ["@20", "c:1"]
"""


def test_move_reorder_within_row(orders_csv):
    out = ce.move_placement(_move_doc(orders_csv), "b", "a", "before")
    row = [ln for ln in out.splitlines() if "@20" in ln][0]
    assert row.strip() == '- ["@20", "b:1", "a:3"]'   # b moved ahead of a, re-inserted at :1
    ff.Dashboard.from_yaml(out)


def test_move_across_rows_drops_empty_source(orders_csv):
    out = ce.move_placement(_move_doc(orders_csv), "c", "a", "after")
    rows = [ln.strip() for ln in out.splitlines() if "@20" in ln]
    # c joined row 0 (row 1 gone) as :1; a and b keep their own widths.
    assert rows == ['- ["@20", "a:3", "c:1", "b:2"]']
    ff.Dashboard.from_yaml(out)


def test_move_noop_and_unknown(orders_csv):
    doc = _move_doc(orders_csv)
    assert ce.move_placement(doc, "a", "a", "before") == doc      # onto itself
    with pytest.raises(ce.ConfigEditError, match="not in the layout"):
        ce.move_placement(doc, "ghost", "a", "before")


def test_move_to_new_row_between_rows(orders_csv):
    """Drop into the gap before row 1: b leaves row 0 into its own row there."""
    out = ce.move_to_new_row(_move_doc(orders_csv), "b", "1")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows == ['- ["@20", "a:3"]', '- ["@30", "b:1"]', '- ["@20", "c:1"]']
    ff.Dashboard.from_yaml(out)


def test_move_to_new_row_at_end(orders_csv):
    out = ce.move_to_new_row(_move_doc(orders_csv), "a", "end")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows[0] == '- ["@20", "b:2"]'       # a left row 0
    assert rows[-1] == '- ["@30", "a:1"]'       # new lone row (width irrelevant)
    ff.Dashboard.from_yaml(out)


def test_move_to_new_row_unknown(orders_csv):
    with pytest.raises(ce.ConfigEditError, match="not in the layout"):
        ce.move_to_new_row(_move_doc(orders_csv), "ghost", "end")


def _merge_doc(csv_path: str) -> str:
    # `status` spans the first two rows: sized in row 1, repeated bare below.
    return f"""datasets:
  o: {{path: {csv_path}}}
charts:
  orders: {{type: table, dataset: o, title: O}}
  by_day: {{type: bar, dataset: o, title: D, x: day, y: status}}
  status: {{type: pie, dataset: o, title: S, column: status}}
  kpi: {{type: number, dataset: o, title: K, column: amount, agg: sum}}
dashboard:
  - ["@40", "orders:3", "status:2"]
  - ["@30", "by_day", "status"]
  - ["@20", "kpi:1"]
"""


def test_move_next_to_spanning_chart_adopts(orders_csv):
    """Next to the spanning chart (`status`) the newcomer adopts the span:
    `kpi:1` in the first row, bare `kpi` in the sibling so it inherits and spans."""
    out = ce.move_placement(_merge_doc(orders_csv), "kpi", "status", "before")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows == ['- ["@40", "orders:3", "kpi:1", "status:2"]',
                    '- ["@30", "by_day", "kpi", "status"]']
    assert "grid-row: 1 / span 2" in ff.Dashboard.from_yaml(out).to_html()


def test_move_next_to_merge_member_single_row(orders_csv):
    """Next to a single-row cell (`orders`), the chart lands in that one row only.
    No spacer — the sibling's `by_day` fills and column-spans the leftover, so
    `status` keeps spanning."""
    out = ce.move_placement(_merge_doc(orders_csv), "kpi", "orders", "after")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows == ['- ["@40", "orders:3", "kpi:1", "status:2"]',
                    '- ["@30", "by_day", "status"]']
    assert "grid-row: 1 / span 2" in ff.Dashboard.from_yaml(out).to_html()


def test_move_merge_member_out_dissolves_span(orders_csv):
    """Moving a chart out of a merge group (its removal would misalign the
    spanning chart) repairs the layout: the span collapses to its fuller row and
    the result stays valid."""
    out = ce.move_to_new_row(_merge_doc(orders_csv), "orders", "end")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    # status keeps its fuller row (with by_day); orders becomes its own row.
    assert '- ["@30", "by_day", "status"]' in rows
    assert '- ["@30", "orders:1"]' in rows
    assert not any("orders" in r and "status" in r for r in rows)
    ff.Dashboard.from_yaml(out)


def test_move_span_across_two_rows(orders_csv):
    """`move_span` places the chart spanning a row and the row directly below:
    `src:1` in the top row, bare `src` below so it inherits and spans."""
    doc = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  orders: {{type: table, dataset: o, title: O}}
  by_day: {{type: bar, dataset: o, title: D, x: day, y: status}}
  status: {{type: pie, dataset: o, title: S, column: status}}
dashboard:
  - ["@40", "orders"]
  - ["@30", "by_day"]
  - ["@20", "status:3"]
"""
    out = ce.move_span(doc, "status", "orders", "after")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    # keeps the chart's own width (3), not reset to 1; bare below so it spans
    assert rows == ['- ["@40", "orders", "status:3"]', '- ["@30", "by_day", "status"]']
    assert "grid-row: 1 / span 2" in ff.Dashboard.from_yaml(out).to_html()


def test_move_span_onto_merged_chart_extends_to_three_rows(orders_csv):
    """Merging onto a chart that already spans 2 rows makes the moved chart span
    that whole span + 1 row below (rows 1-3)."""
    doc = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  orders: {{type: table, dataset: o, title: O}}
  by_day: {{type: bar, dataset: o, title: D, x: day, y: status}}
  status: {{type: pie, dataset: o, title: S, column: status}}
  density: {{type: table, dataset: o, title: De}}
  kpi: {{type: number, dataset: o, title: K, column: amount, agg: sum}}
dashboard:
  - ["@20", "orders", "status"]
  - ["@20", "by_day", "status"]
  - ["@20", "density"]
  - ["@20", "kpi"]
"""
    out = ce.move_span(doc, "kpi", "status", "after")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows == ['- ["@20", "orders", "status", "kpi:1"]',
                    '- ["@20", "by_day", "status", "kpi"]',
                    '- ["@20", "density", "kpi"]']
    html = ff.Dashboard.from_yaml(out).to_html()
    assert "grid-row: 1 / span 2" in html   # status spans 2
    assert "grid-row: 1 / span 3" in html   # kpi spans 3


def test_merge_down_extends_own_span(orders_csv):
    """`merge_down` grows a chart's own span down one row: a single-row chart
    becomes a 2-row line; a 2-row line becomes 3."""
    doc = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  orders: {{type: table, dataset: o, title: O}}
  status: {{type: pie, dataset: o, title: S, column: status}}
  byday: {{type: bar, dataset: o, title: D, x: day, y: status}}
dashboard:
  - ["@20", "orders", "status"]
  - ["@20", "byday"]
"""
    out = ce.merge_down(doc, "orders")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows == ['- ["@20", "orders", "status"]', '- ["@20", "orders", "byday"]']
    assert "grid-row: 1 / span 2" in ff.Dashboard.from_yaml(out).to_html()


def test_merge_down_no_row_below_errors(orders_csv):
    doc = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  orders: {{type: table, dataset: o, title: O}}
dashboard:
  - ["@20", "orders"]
"""
    with pytest.raises(ce.ConfigEditError, match="no row below"):
        ce.merge_down(doc, "orders")


def test_move_span_no_row_below_is_single_insert(orders_csv):
    """With no adjacent row below the target, span degrades to a single insert."""
    doc = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  orders: {{type: table, dataset: o, title: O}}
  status: {{type: pie, dataset: o, title: S, column: status}}
dashboard:
  - ["@40", "status:1"]
  - ["@30", "orders"]
"""
    out = ce.move_span(doc, "status", "orders", "before")
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows == ['- ["@30", "status:1", "orders"]']
    ff.Dashboard.from_yaml(out)


def test_resize_columns_owner_row(orders_csv):
    """Dragging the boundary of a merge group rewrites the owner row's widths and
    keeps the spanning chart bare below."""
    out = ce.resize_columns(_merge_doc(orders_csv), [0, 1], [1, 1])
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows[0] == '- ["@40", "orders:1", "status:1"]'
    assert rows[1] == '- ["@30", "by_day:1", "status"]'   # status stays bare
    ff.Dashboard.from_yaml(out)


def test_resize_columns_from_inherited_row(orders_csv):
    """Resizing a boundary owned by a lower (inherited) row updates that row's
    cells only, leaving the first row's sizes and the span intact."""
    doc = f"""datasets:
  o: {{path: {orders_csv}}}
charts:
  a: {{type: table, dataset: o, title: A}}
  x: {{type: table, dataset: o, title: X}}
  y: {{type: table, dataset: o, title: Y}}
  pie: {{type: pie, dataset: o, title: P, column: status}}
dashboard:
  - ["@20", "a", "pie"]
  - ["@20", "x", "y", "pie"]
"""
    out = ce.resize_columns(doc, [0, 1], [1.5, 0.5, 2])
    rows = [ln.strip() for ln in out.splitlines() if "@" in ln]
    assert rows[0] == '- ["@20", "a:2", "pie:2"]'          # first row unchanged shape
    assert rows[1] == '- ["@20", "x:1.5", "y:0.5", "pie"]'  # x/y resized, pie bare
    assert "grid-template-columns: 1.5fr 0.5fr 2fr" in ff.Dashboard.from_yaml(out).to_html()


def test_apply_edit_invalid_value_raises(orders_csv):
    """A bad enum value surfaces as a DashboardError (the whole doc is validated)."""
    text = _doc(orders_csv)
    form = FakeForm(
        single={"dataset": "orders", "title": "Revenue", "column": "amount",
                "agg": "median", "format": "compact"},  # median is not a valid agg
        multi={"filter_column": [], "filter_op": [], "filter_values": []},
    )
    with pytest.raises(ff.DashboardError, match="unknown agg"):
        ce.apply_edit(text, "revenue", form)
