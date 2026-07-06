import re

import fireflyer as ff
from fireflyer.chart.table.chart import _page_links


def test_table_orders(orders_csv, snapshot):
    chart = ff.chart.table(dataset=orders_csv, title="Orders")
    snapshot(chart.to_html())


def test_table_orders_no_search_no_pagination(orders_csv, snapshot):
    chart = ff.chart.table(
        dataset=orders_csv, title="Orders", search=False, pagination=0
    )
    snapshot(chart.to_html())


def test_table_orders_page_two(orders_csv, snapshot):
    chart = ff.chart.table(dataset=orders_csv, title="Orders", pagination=3)
    snapshot(chart.to_html(page=2))


def test_table_orders_filtered(orders_csv, snapshot):
    chart = ff.chart.table(dataset=orders_csv, title="Orders")
    snapshot(chart.to_html(query="paid"))


def test_table_orders_filtered_no_match(orders_csv, snapshot):
    chart = ff.chart.table(dataset=orders_csv, title="Orders")
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


def test_table_pagination_compact_for_many_pages(tmp_path):
    """Live render with 200 pages emits ~9 links, not 200."""
    csv_path = tmp_path / "many.csv"
    lines = ["id,val"] + [f"{i},x" for i in range(1, 101)]
    csv_path.write_text("\n".join(lines) + "\n")

    chart = ff.chart.table(dataset=str(csv_path), title="t", pagination=5, search=False)
    html = chart.to_html(page=10)  # 100 rows / 5 = 20 pages, current=10

    # Numeric page links rendered (excluding prev/next which are ‹ and ›).
    # Anchor attrs span multiple lines so DOTALL is required.
    numeric = re.findall(
        r'<a class="page-link[^"]*"[^>]*>(\d+)</a>', html, re.DOTALL
    )
    assert sorted(map(int, numeric)) == [1, 8, 9, 10, 11, 12, 20]
    # Two ellipsis spans bracket the current-page window.
    assert html.count('class="page-ellipsis"') == 2


def test_table_orders_declared_filter(orders_csv, snapshot):
    chart = ff.chart.table(
        dataset=orders_csv,
        title="Open orders",
        filters=[{"column": "status", "op": "in", "values": ["paid"]}],
    )
    snapshot(chart.to_html())


