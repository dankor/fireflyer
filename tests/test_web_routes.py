"""Route-level tests for the dashboard render endpoints.

The suite is otherwise web-stack-free (pure functions, unit-tested), but the
wiring *between* a control's htmx attributes and the route that serves it has no
other cover: a control can post a perfectly good form field to a route that
silently ignores it, and the response is a valid 200 that simply doesn't change.
That happened, so these tests exercise the endpoints for real.
"""

import html as html_mod
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


def test_the_editor_links_its_stylesheets_and_script(client):
    """The editor's CSS and JS are files under web/static, linked — not inlined —
    and each link carries a version so an edited file isn't served from cache."""
    page = client.get("/").text
    for name in ("editor.css", "nav.css", "profile.css", "editor.js"):
        m = re.search(rf'/static/{re.escape(name)}\?v=\d+', page)
        assert m, f"{name} not linked"
        assert client.get(m.group(0)).status_code == 200


def test_static_files_are_open_without_a_login(client):
    """The login page links its stylesheet, so the login gate has to let
    /static through — or the sign-in form renders unstyled."""
    app.state.authenticator, saved = object(), app.state.authenticator
    try:
        assert client.get("/static/login.css").status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 303
    finally:
        app.state.authenticator = saved


# --- global quick filters + URL state ----------------------------------------

from urllib.parse import parse_qs, urlsplit

from fireflyer import filters as filters_mod


def _tokens(html):
    return re.findall(r'<input type="hidden" name="cf" value="([^"]*)">', html)


def _filter(client, *, cf=(), headers=None, **fields):
    data = {"yaml_text": _YAML, "cf": list(cf), **fields}
    response = client.post("/dashboard", data=data, headers=headers or {})
    assert response.status_code == 200
    return response


def test_the_plus_form_adds_a_global_filter(client):
    """The panel posts the chart builder's own fields; the route turns them into
    a global token that every cell then renders with."""
    html = _filter(
        client, filter_column="status", filter_op="ni", filter_values="paid, shipped"
    ).text
    want = filters_mod.global_token("status", "ni", ["paid", "shipped"])
    assert [html_mod.unescape(t) for t in _tokens(html)] == [want]


def test_adding_the_same_global_filter_twice_keeps_one(client):
    """Added, never toggled: resubmitting must not remove it."""
    token = filters_mod.global_token("status", "in", ["paid"])
    html = _filter(
        client, cf=[token], filter_column="status", filter_op="in", filter_values="paid"
    ).text
    assert [html_mod.unescape(t) for t in _tokens(html)] == [token]


def test_a_filter_the_model_rejects_is_not_added(client):
    """A `between` needs two bounds; one would be a token nothing applies."""
    html = _filter(
        client, filter_column="day", filter_op="between", filter_values="2026-06-01"
    ).text
    assert _tokens(html) == []


def test_removing_a_global_filter_toggles_its_token_off(client):
    token = filters_mod.global_token("status", "in", ["paid"])
    html = _filter(client, cf=[token, "b|status=pending"], toggle=token).text
    assert _tokens(html) == ["b|status=pending"]


def test_the_filter_state_is_written_to_the_page_url(client):
    """htmx sends the page's URL; the route answers with that URL carrying the
    new state in `f`, keeping every other parameter."""
    response = _filter(
        client,
        cf=["b|status=paid"],
        filter_column="status", filter_op="ni", filter_values="x",
        headers={"HX-Current-URL": "http://host/d/abc?tab=2&f=stale"},
    )
    url = urlsplit(response.headers["HX-Replace-Url"])
    assert url.path == "/d/abc" and not url.netloc
    query = parse_qs(url.query)
    assert query["tab"] == ["2"]
    assert filters_mod.decode_state(query["f"][0]) == [
        "b|status=paid", filters_mod.global_token("status", "ni", ["x"]),
    ]


def test_clearing_every_filter_drops_f_from_the_url(client):
    response = _filter(
        client, cf=["b|status=paid"], toggle="b|status=paid",
        headers={"HX-Current-URL": "http://host/?f=old"},
    )
    assert response.headers["HX-Replace-Url"] == "/"


def test_the_editor_renders_from_the_urls_filter_state(client):
    """A reload or a shared link: /execute starts from `f`."""
    token = filters_mod.global_token("status", "in", ["paid"])
    state = filters_mod.encode_state([token])
    html = client.post(
        f"/execute?f={state}", content=_YAML, headers={"Content-Type": "application/yaml"}
    ).json()["html"]
    assert [html_mod.unescape(t) for t in _tokens(html)] == [token]


def test_the_panels_fields_refetch_as_a_value_picker(client):
    """The panel has no script: picking a column re-renders its fields over
    htmx, and the response stays live so the next change re-fetches too."""
    html = client.post("/filter/fields", data={
        "yaml_text": _YAML, "filter_dataset": "orders", "live": "1",
        "filter_column": "status", "filter_op": "in",
    }).text
    assert re.findall(r'name="filter_value" value="([^"]+)"', html) == [
        "cancelled", "paid", "pending", "shipped",
    ]
    assert html.count('hx-post="/filter/fields"') == 2        # column + op
    # The builder's re-fetch (editor JS) gets the same fields, minus htmx.
    builder = client.post("/filter/fields", data={
        "yaml_text": _YAML, "filter_dataset": "orders",
        "filter_column": "status", "filter_op": "in",
    }).text
    assert "hx-post" not in builder and 'value="shipped"' in builder


def test_ticked_values_add_a_global_filter(client):
    html = _filter(client, filter_column="status", filter_op="in",
                   filter_value=["paid", "shipped"]).text
    want = filters_mod.global_token("status", "in", ["paid", "shipped"])
    assert [html_mod.unescape(t) for t in _tokens(html)] == [want]


def test_a_rows_remove_drops_every_token_behind_it(client):
    html = _filter(
        client, cf=["b|status=paid", "b|status=pending", "x|status=y"],
        remove=["b|status=paid", "b|status=pending"],
    ).text
    assert _tokens(html) == ["x|status=y"]


def test_a_panel_change_reopens_that_panel(client):
    """/dashboard marks the one cell; that cell's own request renders it open."""
    skeleton = _filter(client, cf=["b|status=paid"], remove=["b|status=paid"], open_filter="b").text
    assert '"open_filter": "1"' in skeleton
    cell = _post_cell(client, open_filter="1")
    assert re.search(r'<details [^>]*name="fireflyer-filter" open>', cell)


def test_the_ops_follow_the_column_kind(client):
    """A `str2dt()` column calc is a date: it gets `between` and a day picker.
    Text gets `in` / `not in` only — and a `between` carried over from a date
    column falls back to `in`."""
    def fields(column, op):
        return client.post("/filter/fields", data={
            "yaml_text": _YAML, "filter_dataset": "orders",
            "filter_column": column, "filter_op": op,
        }).text

    date = fields("at", "between")
    assert '<option value="between" selected>' in date and 'class="ff-range' in date
    text = fields("status", "between")
    assert 'value="between"' not in text
    assert '<option value="in" selected>' in text and 'name="filter_value"' in text


def test_a_picked_day_range_adds_a_global_filter(client):
    html = _filter(client, filter_column="at", filter_op="between",
                   filter_from="2026-06-01", filter_to="2026-06-02").text
    want = filters_mod.global_token("at", "between", ["2026-06-01", "2026-06-03"])
    assert [html_mod.unescape(t) for t in _tokens(html)] == [want]


def test_a_column_calc_is_typed_from_its_data(client):
    """`str2dt()` makes a date: its glyph is the date glyph, and the panel's
    column picker says so for every column."""
    html = client.post("/filter/fields", data={
        "yaml_text": _YAML, "filter_dataset": "orders", "filter_column": "at",
    }).text
    from fireflyer.params import type_glyph

    assert f'{type_glyph("date")} <span>at</span>' in html
    assert f'{type_glyph("text")} <span>status</span>' in html
    assert f'{type_glyph("number")} <span>amount</span>' in html


def test_the_search_route_returns_only_the_list(client):
    """Only the list is swapped, so the search box keeps focus while typing."""
    html = client.post("/filter/values", data={
        "yaml_text": _YAML, "filter_dataset": "orders", "filter_column": "status",
        "filter_q": "p", "filter_value": ["shipped"],
    }).text
    assert html.startswith('<div class="ff-filter-choices"') and "filter_q" not in html
    assert re.findall(r'name="filter_value" value="([^"]+)"', html) == [
        "shipped", "paid", "pending",
    ]


def test_the_range_route_applies_a_click(client):
    html = client.get("/filter/range", params={
        "from": "2026-06-03", "to": "", "month": "2026-06", "pick": "2026-06-10",
    }).text
    assert '<input name="filter_to" value="2026-06-10" hidden>' in html


def test_a_fresh_range_picker_opens_on_the_datas_latest_month(client):
    html = client.post("/filter/fields", data={
        "yaml_text": _YAML, "filter_dataset": "orders",
        "filter_column": "at", "filter_op": "between",
    }).text
    assert re.findall(r'class="ff-cal-title">([^<]+)<', html)[0] == "June 2026"


def test_a_picked_range_adds_a_global_filter_from_the_one_field(client):
    html = _filter(client, filter_column="at", filter_op="between",
                   filter_from="2026-06-01", filter_to="2026-06-02").text
    want = filters_mod.global_token("at", "between", ["2026-06-01", "2026-06-03"])
    assert [html_mod.unescape(t) for t in _tokens(html)] == [want]


def test_editing_loads_the_filter_into_the_bottom_row(client):
    """The pencil: the filter's own fields, filled in, with ✓ and ✕."""
    token = filters_mod.global_token("status", "ni", ["paid"])
    html = client.post("/filter/edit", data={
        "yaml_text": _YAML, "filter_dataset": "orders", "cid": "b", "token": token,
    }).text
    assert '<option value="status" selected>' in html
    assert '<option value="ni" selected>' in html and 'value="paid" checked' in html
    replace = re.search(r'<input name="replace" value="([^"]*)" hidden>', html).group(1)
    assert html_mod.unescape(replace) == token
    assert 'title="Save filter"' in html and 'title="Cancel edit"' in html
    blank = client.post("/filter/edit", data={
        "yaml_text": _YAML, "filter_dataset": "orders", "cid": "b",
    }).text
    assert 'name="replace"' not in blank and 'title="Add a global filter"' in blank


def test_saving_an_edit_replaces_the_filter_in_place(client):
    old = filters_mod.global_token("status", "in", ["paid"])
    other = filters_mod.global_token("amount", "in", ["42"])
    html = _filter(client, cf=["b|status=x", old, other], replace=old,
                   filter_column="status", filter_op="ni", filter_value=["pending"]).text
    assert [html_mod.unescape(t) for t in _tokens(html)] == [
        "b|status=x", filters_mod.global_token("status", "ni", ["pending"]), other,
    ]


def test_the_range_route_takes_a_typed_date(client):
    html = client.get("/filter/range", params={
        "from": "2026-06-03", "to": "", "month": "2026-06", "typed_to": "2026-06-10",
    }).text
    assert '<input name="filter_to" value="2026-06-10" hidden>' in html


# --- icons -------------------------------------------------------------------


def test_every_page_links_the_logo_as_its_icon(client):
    """The editor; the gallery and login pages render through their own
    builders, so they're checked as functions. Login matters most: it's open
    before sign-in, and /static is too, so the icon loads there."""
    from fireflyer.web import auth, portal

    pages = [
        client.get("/").text,
        portal.render_gallery([]),
        auth.login_page().body.decode(),
    ]
    for page in pages:
        assert '<link rel="icon" type="image/svg+xml" href="/static/logo.svg?v=' in page
