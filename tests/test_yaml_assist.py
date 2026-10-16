"""The YAML editor's server side (`fireflyer/yaml_assist.py`): problems pinned
to where they are, and what autocomplete can offer."""

import pytest

from fireflyer import yaml_assist


def _only(problems):
    assert len(problems) == 1, problems
    return problems[0]


def test_a_valid_dashboard_has_no_problems():
    assert yaml_assist.lint("name: X\ncharts: {}\nlayout: []\n") == []


@pytest.mark.parametrize("text, line, token, says", [
    # The chart type that doesn't exist — underlined, not the chart's line.
    ("name: X\ncharts:\n  a: {type: piee, dataset: d, title: T}\nlayout: []\n", 3, "piee", "unknown type 'piee'"),
    # A key the chart doesn't take, said plainly (not as a Python TypeError).
    ("name: X\ncharts:\n  a:\n    type: pie\n    dataset: d\n    title: T\n    column: c\n    bogus: 1\nlayout: []\n",
     8, "bogus", "'bogus' isn't a key a pie chart takes"),
    # A layout row naming a chart that isn't there.
    ("name: X\ncharts: {}\nlayout:\n  - [\"@20\", \"zz\"]\n", 4, "zz", "unknown chart 'zz'"),
])
def test_a_dashboard_problem_is_pinned_to_its_token(text, line, token, says):
    p = _only(yaml_assist.lint(text))
    assert p["line"] == line and says in p["message"]
    assert text.split("\n")[line - 1][p["from"]:p["to"]] == token


def test_a_missing_key_is_said_plainly_on_its_chart():
    p = _only(yaml_assist.lint("name: X\ncharts:\n  a: {type: pie, dataset: d, title: T}\nlayout: []\n"))
    assert "a pie chart needs 'column'" in p["message"] and p["line"] == 3


def test_a_yaml_syntax_error_says_where_the_bracket_opened():
    """An unclosed `{` is only noticed on a later line."""
    p = _only(yaml_assist.lint("name: X\ncharts:\n  a: {type: pie\nlayout: []\n"))
    assert p["message"].startswith("Invalid YAML") and "(opened on line 3)" in p["message"]


def test_a_tab_in_indentation_is_said_plainly_once():
    p = _only(yaml_assist.lint("name: X\ncharts:\n\ta: 1\nlayout: []\n"))
    assert p == {"line": 3, "from": 0, "to": 1, "severity": "error",
                 "message": "Tab in indentation — YAML only allows spaces"}


def test_the_schema_comes_from_the_charts_own_params():
    """A new chart type would complete with no change to the editor."""
    s = yaml_assist.schema("name: X\ncharts: {}\nlayout: []\n")
    assert sorted(s["charts"]) == ["bar", "map", "number", "pie", "table"]
    assert s["charts"]["bar"]["direction"] == {"kind": "choice", "choices": ["horizontal", "vertical"]}
    assert s["charts"]["pie"]["column"] == {"kind": "column"}
    assert s["charts"]["pie"]["type"] == {"kind": "type"}


def test_the_schema_knows_the_dashboards_data():
    text = """name: X
calcs:
  sales:
    revenue: {agg: sum, formula: amount}
    day_at: {formula: 'str2dt(day, YYYY-MM-DD)'}
charts: {}
layout: []
datasets:
  sales: |
    region,amount,day
    north,10,2026-06-01
"""
    s = yaml_assist.schema(text)
    assert s["datasets"]["sales"] == ["region", "amount", "day", "day_at"]   # column calcs too
    assert s["calcs"]["sales"] == ["revenue"]                               # values only


def test_the_schema_survives_yaml_that_does_not_parse():
    s = yaml_assist.schema("name: X\ncharts:\n  a: {type: pie\n")
    assert s["charts"] and s["datasets"] == {} and s["calcs"] == {}


def test_the_schema_carries_each_chart_types_icon():
    """For the editor's list of charts, an icon before each name."""
    s = yaml_assist.schema("name: X\ncharts: {}\nlayout: []\n")
    assert set(s["icons"]) == set(s["charts"])
    assert all(svg.startswith("<svg") for svg in s["icons"].values())
