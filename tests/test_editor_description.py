"""The editor's dashboard-description rewrite (`setYamlDescription` in
web/static/editor.js), run for real under Node: it edits the user's YAML, so a
wrong regex would quietly corrupt their file."""

import json
import re
import shutil
import subprocess

import pytest
import yaml

from fireflyer.web.assets import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

SOURCE = (STATIC_DIR / "editor.js").read_text()
FUNCTION = re.search(r"^function setYamlDescription\(.*?^}\n", SOURCE, re.S | re.M).group(0)


def set_description(text, desc):
    script = FUNCTION + f"process.stdout.write(setYamlDescription({json.dumps(text)}, {json.dumps(desc)}));"
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout


BASE = "name: Sales\n\ncharts: {}\nlayout: []\n"


def test_adds_it_right_after_the_name():
    out = set_description(BASE, 'Weekly "revenue"\nby region')
    assert out.startswith('name: Sales\ndescription: "Weekly \\"revenue\\"\\nby region"\n')
    assert yaml.safe_load(out)["description"] == 'Weekly "revenue"\nby region'


def test_replaces_a_one_line_description():
    out = set_description("name: Sales\ndescription: old\ncharts: {}\nlayout: []\n", "new")
    assert yaml.safe_load(out) == {"name": "Sales", "description": "new", "charts": {}, "layout": []}


def test_replaces_a_block_description_whole():
    """A `|` block's indented lines go with it — leaving them would break the file."""
    text = "name: Sales\ndescription: |\n  line one\n  line two\ncharts: {}\nlayout: []\n"
    out = set_description(text, "new")
    assert yaml.safe_load(out) == {"name": "Sales", "description": "new", "charts": {}, "layout": []}


def test_clearing_it_removes_the_key():
    out = set_description("name: Sales\ndescription: old\ncharts: {}\nlayout: []\n", "")
    assert "description" not in out and yaml.safe_load(out)["name"] == "Sales"


def test_a_chart_description_is_left_alone():
    """Only the top-level key: a chart's own is indented, and stays."""
    text = "name: Sales\ncharts:\n  a: {type: number, dataset: d, title: T}\n  b:\n    description: keep me\nlayout: []\n"
    out = set_description(text, "top")
    assert "    description: keep me" in out and yaml.safe_load(out)["description"] == "top"


def test_a_comment_after_it_stays():
    """Changing the description from the editor's title card dropped the
    comment after it."""
    text = "name: Sales\ndescription: old  # what this is for\ncharts: {}\nlayout: []\n"
    out = set_description(text, "new")
    assert 'description: "new" # what this is for\n' in out
    assert yaml.safe_load(out)["description"] == "new"


def test_it_stays_where_it_is():
    text = "name: Sales\ncharts: {}\ndescription: old\nlayout: []\n"
    assert set_description(text, "new").split("\n")[2] == 'description: "new"'


def test_a_comment_line_under_it_is_not_taken_for_its_value():
    text = "name: Sales\ndescription: old\n  # a note\ncharts: {}\nlayout: []\n"
    assert "  # a note" in set_description(text, "new")
