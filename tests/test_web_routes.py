"""Route-level tests for the dashboard render endpoints.

The suite is otherwise web-stack-free (pure functions, unit-tested), but the
wiring *between* a control's htmx attributes and the route that serves it has no
other cover: a control can post a perfectly good form field to a route that
silently ignores it, and the response is a valid 200 that simply doesn't change.
That happened, so these tests exercise the endpoints for real.
"""

import re

import pytest

from starlette.testclient import TestClient

from fireflyer.web.app import app


@pytest.fixture
def client():
    return TestClient(app)


# These dashboards carry their own data, so the suite doesn't depend on a store
# having been seeded — which it no longer is, and which quietly made these tests
# pass off a leftover file on the developer's disk.
_DATA = """
datasets:
  orders: |
    id,day,status,amount
    1,2026-06-01,paid,42
    2,2026-06-01,pending,15
    3,2026-06-02,paid,80
    4,2026-06-02,shipped,11
    5,2026-06-03,paid,7
    6,2026-06-03,cancelled,30
    7,2026-06-04,pending,12
"""

_YAML = """name: Route test
calcs:
  orders:
    at: {formula: 'str2dt(day, YYYY-MM-DD)'}
    n: {agg: count}
charts:
  b: {type: bar, dataset: orders, title: T, x: at, y: status, calc: n}
layout:
  - ["@40", "b"]
""" + _DATA


def _labels(html):
    return re.findall(r'class="fireflyer-bar-label"[^>]*>([^<]+)<', html)


def _post_cell(client, **extra):
    data = {"yaml_text": _YAML, "cid": "b", "col": "1", "row": "1"}
    data.update(extra)
    response = client.post("/dashboard/cell", data=data)
    assert response.status_code == 200
    return response.text


def test_cell_route_applies_set_grain(client):
    """The grain buttons post `set_grain` to /dashboard/cell; the route has to
    actually apply it. Ignoring it returns a valid 200 that renders the *same*
    cell — a click that does nothing, which is how this broke."""
    default = _labels(_post_cell(client))
    assert len(default) > 1                        # one bar per day in the sample
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d", d) for d in default)

    yearly = _labels(_post_cell(client, set_grain="b|year"))
    assert len(yearly) == 1                        # every day rolls into one year
    assert re.fullmatch(r"\d{4}", yearly[0])


def test_cell_route_returns_grain_state_out_of_band(client):
    """A cell-scoped swap doesn't touch the page-level inputs, so the changed
    grain has to ride back out-of-band or the next request reverts it."""
    html = _post_cell(client, set_grain="b|year")
    assert 'id="ff-grain-state"' in html
    assert 'hx-swap-oob="true"' in html
    assert 'value="b|year"' in html

    # No grain change: no out-of-band block, just the cell.
    assert "hx-swap-oob" not in _post_cell(client)


def test_cell_route_keeps_existing_grain_tokens(client):
    """Other charts' tokens survive a change to this one."""
    html = _post_cell(client, grain=["other|month"], set_grain="b|year")
    state = re.search(r'id="ff-grain-state"[^>]*>(.*?)</span>', html, re.S).group(1)
    assert 'value="other|month"' in state
    assert 'value="b|year"' in state


_TABLE_YAML = """name: Route test
calcs:
  orders:
    order_count: {name: Orders, agg: count}
    revenue: {name: Revenue, agg: sum, formula: amount}
charts:
  t:
    type: table
    dataset: orders
    title: By status
    columns: [status]
    measures: [order_count, revenue]
    sort: ['-revenue']
    pagination: 2
layout:
  - ["@40", "t"]
""" + _DATA


def _headers(html):
    """Header text only. A described column carries its tooltip card *inside*
    the <th>, so the cell's raw contents are more than the label."""
    # From the chart markup, never the whole response: the stylesheet is inlined
    # into it, so a rule or comment can match a markup pattern (SKILL.md,
    # "Testing them").
    html = html[html.index('<article class="fireflyer-chart') :]
    out = []
    for cell in re.findall(r"<th(?=[ >])[^>]*>(.*?)</th>", html, re.S):
        # Greedy to the last </span>: the card nests spans of its own.
        cell = re.sub(r'<span class="fireflyer-table-tip".*</span>', "", cell, flags=re.S)
        out.append(re.sub(r"<[^>]+>", "", cell).strip())
    return out


def _body_rows(html):
    """Cell text only — a measure cell carries its tooltip card inside the
    <td>, so the cell's raw contents are more than the value."""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = []
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S):
            cell = re.sub(r'<span class="fireflyer-table-tip".*</span>', "", cell,
                          flags=re.S)
            cells.append(re.sub(r"<[^>]+>", "", cell).strip())
        if cells:
            rows.append(cells)
    return rows


def test_table_paging_keeps_its_measures(client):
    """Regression: an aggregated table paged through /chart/table would come
    back as raw rows. That route rebuilds the chart from URL params and has no
    access to the dashboard's `calcs:` block, so the measures simply vanished —
    a 200 with the wrong table in it. The controls post to /dashboard/cell
    instead, which re-parses the YAML and so still has the calcs."""
    # /dashboard returns the skeleton — each cell fetches itself, so the table
    # markup only exists in a cell response.
    first = client.post("/dashboard/cell", data={"yaml_text": _TABLE_YAML, "cid": "t"})
    assert first.status_code == 200
    assert _headers(first.text) == ["status", "Orders", "Revenue"]

    second = client.post(
        "/dashboard/cell",
        data={"yaml_text": _TABLE_YAML, "cid": "t", "table_page": "2"},
    )
    assert second.status_code == 200
    # Still grouped, still the calc's display name — not the raw columns.
    assert _headers(second.text) == ["status", "Orders", "Revenue"]
    assert "amount" not in _headers(second.text)

    # ...and it is genuinely a different page of the same grouping.
    assert _body_rows(second.text) != _body_rows(first.text)


def test_table_search_reaches_the_cell_route(client):
    """The search box posts `table_q`; the route has to read that name."""
    response = client.post(
        "/dashboard/cell",
        data={"yaml_text": _TABLE_YAML, "cid": "t", "table_q": "paid"},
    )
    assert response.status_code == 200
    rows = _body_rows(response.text)
    assert rows and all(row[0] == "paid" for row in rows), rows


_BROKEN_YAML = """name: Route test
calcs:
  orders:
    revenue: {name: Revenue, agg: sum, formula: no_such_column}
    n: {name: Rows, agg: count}
charts:
  broken: {type: pie, dataset: orders, title: Broken, column: status, calc: revenue}
  fine: {type: number, dataset: orders, title: Rows, calc: n}
layout:
  - ["@30", "broken:50", "fine:50"]
""" + _DATA


def test_a_chart_that_cannot_render_returns_an_error_card(client):
    """A cell arrives by htmx, which only swaps a 2xx — so an exception used to
    leave the placeholder spinning forever with the reason buried in the server
    log. The commonest cause is a dashboard that outran its data (a calc naming
    a column the dataset no longer has), which is exactly what someone can fix
    once they can see it."""
    response = client.post(
        "/dashboard/cell", data={"yaml_text": _BROKEN_YAML, "cid": "broken"}
    )
    assert response.status_code == 200, "a non-2xx leaves htmx showing the spinner"
    assert "fireflyer-chart-error" in response.text
    # The message has to name the actual problem, not just "something failed".
    assert "no_such_column" in response.text
    # ...and the cell wrapper survives, so the grid doesn't collapse.
    assert 'class="fireflyer-dashboard-cell"' in response.text


def test_one_broken_chart_does_not_take_the_others_down(client):
    """Cells render independently; a bad neighbour is not contagious."""
    response = client.post(
        "/dashboard/cell", data={"yaml_text": _BROKEN_YAML, "cid": "fine"}
    )
    assert response.status_code == 200
    assert "fireflyer-chart-error" not in response.text
    assert "fireflyer-number" in response.text


def test_the_error_card_drops_the_query_plan(client):
    """Polars appends its resolved plan to an error — pages of it, saying
    nothing a reader can act on. Only the first line reaches the card."""
    response = client.post(
        "/dashboard/cell", data={"yaml_text": _BROKEN_YAML, "cid": "broken"}
    )
    message = re.search(r'error-msg">(.*?)</div>', response.text, re.S).group(1)
    assert "\n" not in message.strip()
    assert "RESOLVED" not in message.upper() and "Parquet SCAN" not in message
