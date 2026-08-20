import re

import polars as pl
import pytest

import fireflyer as ff
from fireflyer import filters as filters_mod
from fireflyer.chart.table.chart import _page_links


def test_table_orders(orders_parquet, snapshot):
    chart = ff.chart.table(dataset=orders_parquet, title="Orders")
    snapshot(chart.to_html())


def test_table_orders_no_search_no_pagination(orders_parquet, snapshot):
    chart = ff.chart.table(
        dataset=orders_parquet, title="Orders", search=False, pagination=0
    )
    snapshot(chart.to_html())


def test_table_orders_page_two(orders_parquet, snapshot):
    chart = ff.chart.table(dataset=orders_parquet, title="Orders", pagination=3)
    snapshot(chart.to_html(page=2))


def test_table_orders_filtered(orders_parquet, snapshot):
    chart = ff.chart.table(dataset=orders_parquet, title="Orders")
    snapshot(chart.to_html(query="paid"))


def test_table_orders_filtered_no_match(orders_parquet, snapshot):
    chart = ff.chart.table(dataset=orders_parquet, title="Orders")
    snapshot(chart.to_html(query="zzzzz"))


def test_page_links_small_total_shows_all():
    assert _page_links(1, 1) == [1]
    assert _page_links(3, 5) == [1, 2, 3, 4, 5]


def test_page_links_compact_in_middle():
    # current=10 of 200 → first, gap, current ±2, gap, last
    assert _page_links(10, 200) == [1, None, 8, 9, 10, 11, 12, None, 200]


def test_page_links_near_start():
    # current=2 of 200 → first three around current, gap, last
    assert _page_links(2, 200) == [1, 2, 3, 4, None, 200]


def test_page_links_near_end():
    assert _page_links(199, 200) == [1, None, 197, 198, 199, 200]


def test_table_pagination_compact_for_many_pages(csv_to_parquet):
    """Live render with 200 pages emits ~9 links, not 200."""
    lines = ["id,val"] + [f"{i},x" for i in range(1, 101)]
    dataset = csv_to_parquet("\n".join(lines) + "\n", "many")

    chart = ff.chart.table(dataset=dataset, title="t", pagination=5, search=False)
    html = chart.to_html(page=10)  # 100 rows / 5 = 20 pages, current=10

    # Numeric page links rendered (excluding prev/next which are ‹ and ›).
    # Anchor attrs span multiple lines so DOTALL is required.
    numeric = re.findall(
        r'<a class="page-link[^"]*"[^>]*>(\d+)</a>', html, re.DOTALL
    )
    assert sorted(map(int, numeric)) == [1, 8, 9, 10, 11, 12, 20]
    # Two ellipsis spans bracket the current-page window.
    assert html.count('class="page-ellipsis"') == 2


def test_table_orders_declared_filter(orders_parquet, snapshot):
    chart = ff.chart.table(
        dataset=orders_parquet,
        title="Open orders",
        filters=[{"column": "status", "op": "in", "values": ["paid"]}],
    )
    snapshot(chart.to_html())




# --- columns / measures / sort ------------------------------------------------

_CALCS = """
calcs:
  orders:
    order_count: {name: Orders, agg: count}
    revenue: {name: Revenue, agg: sum, formula: amount, format: 0.0a $}
    big_revenue:
      name: Big revenue
      agg: sum
      formula: amount
      filters:
        - {column: status, op: in, values: [paid]}
"""


def _table_dashboard(parquet, chart_yaml):
    return ff.Dashboard.from_yaml(
        f"name: T\n{_CALCS}charts:\n  t:\n    type: table\n    dataset: orders\n"
        f"    title: T\n    pagination: 0\n{chart_yaml}"
        'layout:\n  - ["@30", "t:1"]\n',
        datasets=lambda name: (parquet, None),
    )


def _header_text(body):
    """Header text only. A described column carries its tooltip card *inside*
    the <th>, so the cell's raw contents are more than the label."""
    out = []
    for cell in re.findall(r"<th(?=[ >])[^>]*>(.*?)</th>", body, re.S):
        # Greedy to the last </span>: the card nests spans of its own.
        cell = re.sub(r'<span class="fireflyer-table-tip".*</span>', "", cell, flags=re.S)
        out.append(re.sub(r"<[^>]+>", "", cell).strip())
    return out


def _strip_tip(cell):
    """Cell text without its tooltip card. Greedy to the last </span>, since the
    card nests spans of its own."""
    cell = re.sub(r'<span class="fireflyer-table-tip".*</span>', "", cell, flags=re.S)
    return re.sub(r"<[^>]+>", "", cell).strip()


def _grid(html):
    body = html[html.index('<article class="fireflyer-chart') :]
    headers = _header_text(body)
    rows = [
        cells
        for cells in (
            [_strip_tip(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        )
        if cells
    ]
    return headers, rows


def test_table_groups_by_columns_and_computes_measures(orders_parquet):
    """`columns` become grouping keys once `measures` is set; each measure is one
    output column, headed by the calc's name and formatted by its token."""
    dash = _table_dashboard(
        orders_parquet,
        "    columns: [status]\n    measures: [order_count, revenue]\n"
        "    sort: ['-revenue']\n",
    )
    headers, rows = _grid(dash.to_html())
    assert headers == ["status", "Orders", "Revenue"]
    # Sorted by revenue descending, and the calc's `0.0a $` format applied.
    assert rows == [["paid", "4", "276 $"], ["cancelled", "1", "30 $"],
                    ["pending", "2", "27 $"]]


def test_table_without_measures_shows_raw_rows(orders_parquet):
    """No measures -> row by row, no aggregation. `columns` then just picks
    which columns to show, in that order."""
    dash = _table_dashboard(
        orders_parquet, "    columns: [status, amount]\n    sort: ['+amount']\n"
    )
    headers, rows = _grid(dash.to_html())
    assert headers == ["status", "amount"]
    assert [r[1] for r in rows] == sorted([r[1] for r in rows], key=int)
    assert len(rows) == 7          # every row, not one per status


def test_table_measures_without_columns_give_one_total_row(orders_parquet):
    """No grouping columns -> a single row of grand totals."""
    dash = _table_dashboard(orders_parquet, "    measures: [order_count, revenue]\n")
    headers, rows = _grid(dash.to_html())
    assert headers == ["Orders", "Revenue"]
    assert rows == [["7", "333 $"]]


def test_table_sort_precedence_is_left_to_right(orders_parquet):
    """The first sort entry is the primary key; `-` descending, `+`/bare asc."""
    dash = _table_dashboard(
        orders_parquet,
        "    columns: [status, day]\n    measures: [revenue]\n"
        "    sort: ['+status', '-revenue']\n",
    )
    _, rows = _grid(dash.to_html())
    statuses = [r[0] for r in rows]
    assert statuses == sorted(statuses)
    # Cells carry the calc's `0.0a $` formatting, so read the number back out.
    paid = [float(re.match(r"[\d.,]+", r[2]).group().replace(",", ""))
            for r in rows if r[0] == "paid"]
    assert paid == sorted(paid, reverse=True)


def test_table_measure_keeps_its_own_filters(orders_parquet):
    """A measure with per-calc filters behaves as it does on any other chart —
    the table doesn't reimplement aggregation, it calls the calc engine."""
    dash = _table_dashboard(
        orders_parquet, "    columns: [status]\n    measures: [big_revenue]\n"
    )
    _, rows = _grid(dash.to_html())
    # `big_revenue` only counts paid rows, so every other status totals nothing.
    by_status = {r[0]: r[1] for r in rows}
    assert by_status["paid"] == "276"
    assert all(v == "" for k, v in by_status.items() if k != "paid")


def test_table_unknown_sort_key_is_skipped(orders_parquet):
    """Consistent with filters naming an absent column: skipped, not raised."""
    dash = _table_dashboard(
        orders_parquet, "    columns: [status]\n    sort: ['-nope']\n"
    )
    headers, rows = _grid(dash.to_html())
    assert headers == ["status"] and len(rows) == 7


def test_table_rejects_an_unknown_measure(orders_parquet):
    """A typo'd measure fails at parse time, like a typo'd `calc:` does."""
    with pytest.raises(ff.DashboardError, match="unknown calc 'nope'"):
        _table_dashboard(orders_parquet, "    measures: [nope]\n")


def test_table_rejects_a_column_calc_as_a_measure(orders_parquet):
    """A column calc is a dimension; it belongs in `columns`, not `measures`."""
    yaml = (
        "name: T\ncalcs:\n  orders:\n"
        "    at: {formula: 'str2dt(day, YYYY-MM-DD)'}\n"
        "charts:\n  t: {type: table, dataset: orders, title: T, measures: [at]}\n"
        'layout:\n  - ["@30", "t:1"]\n'
    )
    with pytest.raises(ff.DashboardError, match="column calc"):
        ff.Dashboard.from_yaml(yaml, datasets=lambda n: (orders_parquet, None))


def test_standalone_table_measures_explain_themselves(orders_parquet):
    """Measures are calc keys, which only a dashboard can resolve — say so
    rather than dying on `None.aggregate`."""
    from fireflyer.calcs import CalcError

    chart = ff.chart.table(
        dataset=orders_parquet, title="T", measures=["revenue"]
    )
    with pytest.raises(CalcError, match="standalone table"):
        chart.to_html()


# --- column header tooltips ---------------------------------------------------

_DESCRIBED = """
calcs:
  orders:
    revenue:
      name: Revenue
      agg: sum
      formula: amount
      description: Sum of order amount
    order_day:
      formula: str2dt(day, YYYY-MM-DD)
      description: Order date, parsed from text
"""


def _described_dashboard(parquet, chart_yaml):
    return ff.Dashboard.from_yaml(
        f"name: T\n{_DESCRIBED}charts:\n  t:\n    type: table\n    dataset: orders\n"
        f"    title: T\n    pagination: 0\n{chart_yaml}"
        'layout:\n  - ["@30", "t:1"]\n',
        datasets=lambda name: (parquet, None),
    )


def _header_cells(html):
    body = html[html.index('<article class="fireflyer-chart') :]
    return re.findall(r"<th(?=[ >])[^>]*>.*?</th>", body, re.S)


def test_described_columns_get_a_header_tooltip(orders_parquet):
    """A header is a label — the calc's `description` is where you find out what
    it stands for, the same role it plays in the pie/bar/number cards. Applies to
    a measure *and* to a column calc shown as a dimension."""
    dash = _described_dashboard(
        orders_parquet,
        "    columns: [order_day, status]\n    measures: [revenue]\n",
    )
    cells = _header_cells(dash.to_html())
    tips = {
        re.search(r'th-label">(.*?)</span>', th).group(1): (
            m.group(1) if (m := re.search(r'tip-desc">(.*?)</span>', th)) else None
        )
        for th in cells
    }
    assert tips == {
        "order_day": "Order date, parsed from text",
        "status": None,               # a real dataset column has no description
        "Revenue": "Sum of order amount",
    }


def test_header_tooltip_anchors_to_its_own_header(orders_parquet):
    """Each card points at the header it belongs to, and the names are unique —
    otherwise every card would open under the first column."""
    dash = _described_dashboard(
        orders_parquet, "    columns: [order_day]\n    measures: [revenue]\n"
    )
    html = dash.to_html()
    described = [th for th in _header_cells(html) if "has-tip" in th]
    assert len(described) == 2
    names = []
    for th in described:
        name = re.search(r"anchor-name: (--ff-tbl-c\d+)", th).group(1)
        assert f"position-anchor: {name}" in th
        names.append(name)
    assert len(set(names)) == len(names)


def test_undescribed_table_emits_no_tooltip_markup(orders_parquet):
    """A calc with no description adds nothing — the plain header is the markup
    this table has always emitted."""
    dash = _described_dashboard(orders_parquet, "    columns: [status]\n")
    body = dash.to_html()
    body = body[body.index('<article class="fireflyer-chart') :]
    assert 'class="fireflyer-table-tip"' not in body
    assert "has-tip" not in body
    assert "anchor-name" not in body


def test_header_tooltip_escapes_the_scrolling_body(orders_parquet):
    """The table body scrolls and the dashboard cell clips, so the card has to
    leave the flow — with fallbacks on both axes (SKILL.md, "Tooltips")."""
    dash = _described_dashboard(orders_parquet, "    measures: [revenue]\n")
    html = dash.to_html()
    block = re.search(r"@supports \(anchor-name: --a\) \{(.*?)\n\}", html, re.S).group(1)
    assert "position: fixed;" in block
    assert "position-try-fallbacks: flip-inline, flip-block, flip-inline flip-block;" in block
    assert "!important" not in block


def _cell_cards(html):
    """(displayed text, card) per measure cell."""
    body = html[html.index('<article class="fireflyer-chart') :]
    out = []
    for td in re.findall(r"<td[^>]*has-tip.*?</td>", body, re.S):
        card = {
            part: (m.group(1) if (m := re.search(rf'tip-{part}">(.*?)</span>', td)) else None)
            for part in ("head", "name", "val", "desc")
        }
        out.append((re.sub(r"<[^>]+>", "", td.split("<span")[0]).strip(), card))
    return out


def test_measure_cells_carry_the_shared_card(orders_parquet):
    """Same card the other charts show for an item: what the row *is*, then the
    calc's name with its exact value, then the description."""
    dash = _described_dashboard(
        orders_parquet,
        "    columns: [order_day, status]\n    measures: [revenue]\n"
        "    sort: ['-revenue']\n",
    )
    cards = _cell_cards(dash.to_html())
    assert cards, "measure cells should have cards"
    _, first = cards[0]
    assert first["name"] == "Revenue"
    assert first["desc"] == "Sum of order amount"
    # The head names the row: its dimension values, joined.
    assert " · " in first["head"]
    assert first["head"].endswith(("paid", "shipped", "cancelled", "pending", "refunded"))


def test_measure_cell_card_shows_the_exact_value(csv_to_parquet):
    """The cell is formatted by the calc's token — `1.9m $` — so the card is
    where the real figure lives. This is the rule the other charts follow."""
    parquet = csv_to_parquet(
        "status,amount\npaid,812345\npaid,431002\npaid,700111\n", "bigmoney"
    )
    dash = ff.Dashboard.from_yaml(
        "name: T\ncalcs:\n  bigmoney:\n"
        "    revenue: {name: Revenue, agg: sum, formula: amount, format: 0.0a $}\n"
        "charts:\n  t: {type: table, dataset: bigmoney, title: T, "
        "columns: [status], measures: [revenue], pagination: 0}\n"
        'layout:\n  - ["@30", "t:1"]\n',
        datasets=lambda name: (parquet, None),
    )
    (shown, card), = _cell_cards(dash.to_html())
    assert shown == "1.9m $"          # abbreviated, because the cell is narrow
    assert card["val"] == "1,943,458"  # exact, thousands-separated


def test_dimension_cells_get_no_card(orders_parquet):
    """A dimension cell already shows its full value — a card would repeat what's
    on screen, and a table can render a thousand rows of them."""
    dash = _described_dashboard(
        orders_parquet, "    columns: [status]\n    measures: [revenue]\n"
    )
    body = dash.to_html()
    body = body[body.index('<article class="fireflyer-chart') :]
    for td in re.findall(r"<td[^>]*>.*?</td>", body, re.S):
        text = _strip_tip(td)
        if text in ("paid", "shipped", "cancelled", "pending", "refunded"):
            assert "has-tip" not in td, text


def test_raw_table_has_no_cell_cards(orders_parquet):
    """No measures, no formatting, nothing to expand — no cards at all."""
    dash = _described_dashboard(orders_parquet, "    columns: [status, amount]\n")
    body = dash.to_html()
    body = body[body.index('<article class="fireflyer-chart') :]
    assert "has-tip" not in body


def test_grand_total_card_has_no_head(orders_parquet):
    """With no grouping columns the row isn't *about* anything, so the card
    leads with the name/value row rather than an empty header."""
    dash = _described_dashboard(orders_parquet, "    measures: [revenue]\n")
    (_, card), = _cell_cards(dash.to_html())
    assert card["head"] is None
    assert card["name"] == "Revenue" and card["val"] == "333"


def test_cell_cards_anchor_to_their_own_cell(orders_parquet):
    """Every cell needs its own anchor name, or all the cards stack on one."""
    dash = _described_dashboard(
        orders_parquet, "    columns: [status]\n    measures: [revenue]\n"
    )
    body = dash.to_html()
    body = body[body.index('<article class="fireflyer-chart') :]
    names = []
    for td in re.findall(r"<td[^>]*has-tip.*?</td>", body, re.S):
        name = re.search(r"anchor-name: (--ff-tbl-[\w-]+)", td).group(1)
        assert f"position-anchor: {name}" in td
        names.append(name)
    assert len(names) > 1 and len(set(names)) == len(names)


# --- row click crossfilter ----------------------------------------------------

def _rows_html(html):
    body = html[html.index('<article class="fireflyer-chart') :]
    return re.findall(r"<tr[^>]*>", body)


def _row_tokens(html):
    body = html[html.index('<article class="fireflyer-chart') :]
    return re.findall(r'"toggle": "(.*?)"', body)


def test_row_click_filters_on_every_dimension(orders_parquet):
    """A row *is* a combination of its dimension values, so the click means the
    whole combination — one token with a part per column, toggling as a unit."""
    dash = _table_dashboard(
        orders_parquet, "    columns: [status, day]\n    measures: [revenue]\n"
    )
    tokens = _row_tokens(dash.render_cell("t", cf_tokens=[]))
    assert tokens
    for token in tokens:
        emitter, _, parts = token.partition("|")
        assert emitter == "t"
        assert [p.split("=")[0] for p in parts.split("|")] == ["status", "day"]
        # A measure is a value, not a dimension — never a filter term.
        assert "revenue" not in token


def test_row_token_round_trips_through_the_filter_model(orders_parquet):
    """The token has to actually select its own row. `filters.predicates`
    compares Polars' string cast, so a token built with Python's `str()` would
    match nothing for a temporal column — this pins the agreement."""
    dash = _table_dashboard(
        orders_parquet,
        "    columns: [status, day]\n    measures: [revenue]\n"
        "    sort: ['-revenue']\n",
    )
    token = _row_tokens(dash.render_cell("t", cf_tokens=[]))[0]
    parsed = filters_mod.decode_tokens([token])
    assert [f.column for f in parsed] == ["status", "day"]

    df = pl.read_parquet(orders_parquet)
    matched = filters_mod.apply(df, parsed)
    assert matched.height > 0, "the token must select the rows it came from"
    assert matched["status"].unique().to_list() == [parsed[0].values[0]]


def test_temporal_dimension_token_matches_its_rows(orders_parquet):
    """A column calc used as a dimension is a real date, not text — the exact
    case where a hand-rolled `str()` and Polars' cast disagree."""
    dash = ff.Dashboard.from_yaml(
        "name: T\ncalcs:\n  orders:\n"
        "    order_day: {formula: 'str2dt(day, YYYY-MM-DD)'}\n"
        "    revenue: {name: Revenue, agg: sum, formula: amount}\n"
        "charts:\n  t: {type: table, dataset: orders, title: T, "
        "columns: [order_day], measures: [revenue], pagination: 0}\n"
        'layout:\n  - ["@30", "t:1"]\n',
        datasets=lambda name: (orders_parquet, None),
    )
    token = _row_tokens(dash.render_cell("t", cf_tokens=[]))[0]
    parsed = filters_mod.decode_tokens([token])

    # Apply against the same scan the chart reads, calcs attached.
    from fireflyer.scan import scan
    lf = scan(orders_parquet, None, dash.calc_sets.get("orders"))
    got = lf.filter(*filters_mod.predicates(parsed, lf.collect_schema().names())).collect()
    assert got.height > 0, f"{token} matched no rows"


def test_raw_row_click_uses_every_shown_column(orders_parquet):
    """With no measures every column is a dimension, so the token names them
    all — clicking a raw row drills to that record."""
    dash = _table_dashboard(orders_parquet, "    columns: [status, amount]\n")
    token = _row_tokens(dash.render_cell("t", cf_tokens=[]))[0]
    assert [p.split("=")[0] for p in token.partition("|")[2].split("|")] == [
        "status", "amount"
    ]


def test_selected_row_is_marked_and_the_rest_dim(orders_parquet):
    """Same selected/unselected treatment the pie and bar use."""
    dash = _table_dashboard(
        orders_parquet, "    columns: [status]\n    measures: [revenue]\n"
    )
    token = _row_tokens(dash.render_cell("t", cf_tokens=[]))[0]
    html = dash.render_cell("t", cf_tokens=[token])
    body = html[html.index('<article class="fireflyer-chart') :]

    assert "has-selection" in body
    rows = _rows_html(html)
    assert sum(1 for r in rows if 'class="active"' in r) == 1
    # The emitter still shows every row — a chart is exempt from its own filter.
    assert sum(1 for r in rows if "hx-post" in r) == 3


def test_standalone_table_rows_are_not_clickable(orders_parquet):
    """No dashboard, no crossfilter to emit into."""
    html = ff.chart.table(dataset=orders_parquet, title="T", pagination=0).to_html()
    assert "hx-post" not in "".join(_rows_html(html))


# --- header width -------------------------------------------------------------

def test_header_label_is_clamped_to_two_lines(orders_parquet):
    """`nowrap` on a header made a long calc name reserve a whole line of column
    width. The label wraps to two lines and ellipses past that."""
    html = ff.chart.table(dataset=orders_parquet, title="T").to_html()
    assert "white-space: nowrap;\n}" not in html.split(".fireflyer-table thead th {")[1][:400]

    start = html.index(".fireflyer-table-th-label {")
    rule = html[start : html.index("}", start)]
    assert "-webkit-line-clamp: 2;" in rule
    assert "overflow: hidden;" in rule
    assert "max-width: var(--ff-header-max);" in rule
    assert "overflow-wrap: anywhere;" in rule   # a long single token can break

    # Every header text sits in the clamped span, not bare in the cell.
    body = html[html.index('<article class="fireflyer-chart') :]
    for cell in re.findall(r"<th(?=[ >])[^>]*>(.*?)</th>", body, re.S):
        assert 'class="fireflyer-table-th-label"' in cell


def test_a_long_header_gets_a_tooltip_even_without_a_description(orders_parquet):
    """The clamp can cut a name off, so the card carries the full one — that's
    the trade that lets the column be narrow."""
    long_name = "Average order value per paying customer"
    assert len(long_name) > 24
    dash = ff.Dashboard.from_yaml(
        "name: T\ncalcs:\n  orders:\n"
        f"    revenue: {{name: {long_name}, agg: sum, formula: amount}}\n"
        "charts:\n  t: {type: table, dataset: orders, title: T, "
        "columns: [status], measures: [revenue], pagination: 0}\n"
        'layout:\n  - ["@30", "t:1"]\n',
        datasets=lambda name: (orders_parquet, None),
    )
    body = dash.to_html()
    body = body[body.index('<article class="fireflyer-chart') :]
    measure_th = next(th for th in re.findall(r"<th(?=[ >])[^>]*>.*?</th>", body, re.S)
                      if long_name in th)
    assert 'class="fireflyer-table-tip-head">' + long_name in measure_th
    # No description, so no underline — the ellipsis is its own signal.
    assert "has-desc" not in measure_th


def test_a_short_header_stays_plain(orders_parquet):
    """Nothing to reveal, so no card and no markup for one."""
    html = ff.chart.table(dataset=orders_parquet, title="T").to_html()
    body = html[html.index('<article class="fireflyer-chart') :]
    assert "has-tip" not in body     # id / status / amount / day / lat / lng


# --- null grouping keys -------------------------------------------------------

@pytest.fixture
def nulls_parquet(tmp_path):
    """Two named rows and three with a null `name`."""
    path = tmp_path / "nulls.parquet"
    pl.DataFrame({
        "name": [None, None, None, "a", "a"],
        "team": ["x", "x", "x", "y", "y"],
        "amount": [10, 20, 30, 5, 7],
        "qty": [1, 2, 3, 4, 5],
    }).write_parquet(path)
    return str(path)


def _nulls_dashboard(parquet, chart_yaml):
    return ff.Dashboard.from_yaml(
        "name: T\ncalcs:\n  nulls:\n"
        "    total: {name: Total, agg: sum, formula: amount}\n"
        "    items: {name: Items, agg: sum, formula: qty}\n"
        "    n: {name: Rows, agg: count}\n"
        f"charts:\n  t:\n    type: table\n    dataset: nulls\n    title: T\n"
        f"    pagination: 0\n{chart_yaml}"
        'layout:\n  - ["@30", "t:1"]\n',
        datasets=lambda name: (parquet, None),
    )


def test_null_grouping_key_is_one_group(nulls_parquet):
    """A join follows SQL, where null never equals null — so the null group
    failed to match itself and every measure landed on its own row, one value
    per row down a diagonal. `group_by` already treats all nulls as one group;
    the join has to agree with it."""
    dash = _nulls_dashboard(
        nulls_parquet, "    columns: [name]\n    measures: [total, items, n]\n"
    )
    _, rows = _grid(dash.to_html())
    assert len(rows) == 2, rows                    # "a" and the null group

    by_name = {r[0]: r[1:] for r in rows}
    assert by_name["a"] == ["12", "9", "2"]        # 5+7, 4+5, 2 rows
    # One row carrying *all three* measures, not three rows carrying one each.
    assert by_name[""] == ["60", "6", "3"]         # 10+20+30, 1+2+3, 3 rows


def test_every_key_null_still_gives_one_row(nulls_parquet):
    """The degenerate case: nothing to group by but nulls."""
    dash = _nulls_dashboard(
        nulls_parquet,
        "    columns: [name]\n    measures: [total]\n"
        "    filters:\n      - {column: team, op: in, values: [x]}\n",
    )
    _, rows = _grid(dash.to_html())
    assert rows == [["", "60"]]


def test_a_row_with_a_null_dimension_is_not_clickable(nulls_parquet):
    """Dropping the null part would emit a token selecting a *superset* of the
    row — `(name=null, team=x)` would filter on `team=x` alone and pull in every
    named row on that team. The filter model has no "is null" op, so the honest
    answer is no click."""
    dash = _nulls_dashboard(
        nulls_parquet, "    columns: [name, team]\n    measures: [total]\n"
    )
    body = dash.render_cell("t", cf_tokens=[])
    body = body[body.index('<article class="fireflyer-chart') :]

    clickable, inert = [], []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", body, re.S):
        cells = [_strip_tip(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if not cells:
            continue
        (clickable if "hx-post" in tr else inert).append(cells)

    assert clickable == [["a", "y", "12"]]
    assert inert == [["", "x", "60"]]
    # And no half-built token leaked into the markup.
    assert "t|team=x" not in body


# --- relabelled columns -------------------------------------------------------

_OVERLAY = """
calcs:
  orders:
    status:
      name: Order status
      description: Where the order got to
      formula: status
    revenue: {name: Revenue, agg: sum, formula: amount}
"""


def _overlay_dashboard(parquet, chart_yaml):
    return ff.Dashboard.from_yaml(
        f"name: T\n{_OVERLAY}charts:\n  t:\n    type: table\n    dataset: orders\n"
        f"    title: T\n    pagination: 0\n{chart_yaml}"
        'layout:\n  - ["@30", "t:1"]\n',
        datasets=lambda name: (parquet, None),
    )


def test_a_relabelled_column_shows_its_display_name(orders_parquet):
    """A column calc's `name` is what a viewer reads, wherever the column shows
    up — without renaming anything the YAML refers to."""
    dash = _overlay_dashboard(
        orders_parquet, "    columns: [status]\n    measures: [revenue]\n"
    )
    headers, rows = _grid(dash.to_html())
    assert headers == ["Order status", "Revenue"]
    # The data is untouched — an overlay relabels, it doesn't rename or reshape.
    assert {r[0] for r in rows} == {"paid", "cancelled", "pending"}


def test_a_relabelled_column_keeps_its_key_in_the_yaml(orders_parquet):
    """`columns:`, the crossfilter token and the filter model all still use the
    key. Only the label changes."""
    dash = _overlay_dashboard(
        orders_parquet, "    columns: [status]\n    measures: [revenue]\n"
    )
    token = _row_tokens(dash.render_cell("t", cf_tokens=[]))[0]
    assert token.startswith("t|status=")
    assert "Order status" not in token


def test_the_filter_indicator_uses_the_display_name(orders_parquet):
    """The indicator names the columns in play, so it has to relabel them too —
    otherwise a chart says "Order status" and its badge says "status"."""
    dash = _overlay_dashboard(
        orders_parquet, "    columns: [status]\n    measures: [revenue]\n"
    )
    token = _row_tokens(dash.render_cell("t", cf_tokens=[]))[0]
    cell = dash.render_cell("t", cf_tokens=[token])

    rows = [
        " ".join(re.sub(r"<[^>]+>", " ", r).split())
        for r in re.findall(r'<div class="fireflyer-filter-row">(.*?)</div>', cell, re.S)
    ]
    # Which group lands first isn't fixed without a `sort:`, so match the label
    # and the value the token actually carried.
    value = token.split("status=")[1]
    assert rows == [f"Order status in {value}"]


def test_the_relabelled_columns_description_reaches_the_header_card(orders_parquet):
    """Same card as before — the overlay just supplies its text."""
    dash = _overlay_dashboard(orders_parquet, "    columns: [status]\n")
    body = dash.to_html()
    body = body[body.index('<article class="fireflyer-chart') :]
    th = next(t for t in re.findall(r"<th(?=[ >])[^>]*>.*?</th>", body, re.S)
              if "Order status" in t)
    assert 'tip-desc">Where the order got to</span>' in th
