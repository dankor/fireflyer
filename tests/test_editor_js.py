"""Behaviour tests for the editor's JavaScript (`web/static/editor.js`).

The rest of the suite renders that script and asserts on its *text*, never
running a line of it. Chart resize shipped broken twice under exactly that gap:
once when a block deletion carried off `let resize` and the resize constants,
leaving handlers referencing names that no longer existed. Every text assertion
still passed, because the strings were all present — just no longer connected.

These run the script for real, in Node's `vm` under a hand-built DOM stub
(`editor_harness.mjs`), and drive a drag end to end. No jsdom and no npm, per
the project's no-frontend-tooling rule: Node is a runtime we ask for, not a
toolchain we depend on. Skipped when it isn't installed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fireflyer.web.app import DEFAULT_YAML, _theme_switch, render_editor_page
from fireflyer.web.assets import STATIC_DIR

HARNESS = Path(__file__).resolve().parent / "editor_harness.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to execute the editor script"
)


@pytest.fixture(scope="module")
def editor_js(tmp_path_factory):
    """The script as the browser gets it, plus the ids the page really has."""
    page = render_editor_page(DEFAULT_YAML, theme=_theme_switch())
    script = (STATIC_DIR / "editor.js").read_text()

    out = tmp_path_factory.mktemp("editor")
    (out / "editor.js").write_text(script)
    (out / "ids.json").write_text(
        json.dumps(sorted(set(re.findall(r'id="([\w-]+)"', page))))
    )
    return out


def run_scenario(editor_js, name):
    result = subprocess.run(
        ["node", str(HARNESS), str(editor_js / "editor.js"), name,
         str(editor_js / "ids.json")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert "error" not in payload, payload["error"]
    # An unhandled rejection prints after the JSON and would otherwise pass
    # silently — the editor's own paths are async.
    assert "UnhandledPromiseRejection" not in result.stderr, result.stderr
    return payload


def test_the_script_parses(editor_js):
    """`node --check` on the script the browser is actually served. A block
    deletion that cuts mid-function is a syntax error no text assertion sees."""
    result = subprocess.run(
        ["node", "--check", str(editor_js / "editor.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_the_script_loads_in_strict_mode(editor_js):
    """Running it top-to-bottom under `"use strict"` turns an undeclared name
    into a ReferenceError at load, and proves the handlers are wired to the
    elements they think they are."""
    loaded = run_scenario(editor_js, "load")

    # The drag handlers live on window; the delegating ones on the output pane.
    assert {"mousemove", "mouseup"} <= set(loaded["windowEvents"])
    assert {"mousedown", "click"} <= set(loaded["outputEvents"])
    # View is where you land.
    assert loaded["mode"] == "mode-view"


def test_dragging_a_row_handle_rewrites_the_height(editor_js):
    """The whole point of the row handle: drag the bottom edge, and the row's
    `@<height>` token in the YAML follows. 240px + 80px = 320px, and one unit is
    8px, so `@30` becomes `@40`."""
    result = run_scenario(editor_js, "row-resize")

    assert result["tracks"] == "320px", "the grid track follows the pointer"
    assert '"@40", "a"' in result["yaml"], result["yaml"]
    # Only the dragged row changes.
    assert '"@40", "b"' in result["yaml"]


def test_dragging_a_row_handle_does_nothing_outside_edit_mode(editor_js):
    """Resizing rewrites YAML, so it belongs to edit mode alone — elsewhere the
    handler bails before touching anything."""
    result = run_scenario(editor_js, "row-resize-in-view")
    assert result["yaml"] == 'name: T\nlayout:\n  - ["@30", "a"]\n'


def test_dragging_a_column_boundary_rebalances_and_commits(editor_js):
    """A 50/50 row dragged 200px right across a 1000px row becomes 70/30, and
    the release posts the new widths for the server to fold into every row those
    columns belong to."""
    result = run_scenario(editor_js, "col-resize")

    assert result["columns"] == "70fr 30fr"
    assert "/chart/config/resize-columns" in result["fetches"]
