"""Structural checks on every chart's stylesheet.

Each chart.css is injected verbatim, so a stale rule left behind by an edit
doesn't error — it just wins the cascade if it sits later in the file, and the
chart quietly keeps its old look. That happened: the bar's stylesheet ended up
holding two `.fireflyer-bar-tooltip` rules, the older one last, so a rebuilt
tooltip card rendered with the *previous* design and no test noticed.
"""

import re
from pathlib import Path

import pytest

CHART_DIR = Path(__file__).resolve().parent.parent / "fireflyer" / "chart"
STYLESHEETS = sorted(CHART_DIR.glob("*/chart.css"))


def _without_comments(css: str) -> str:
    """Blank comments out, keeping newlines so line numbers still line up."""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css, flags=re.S)


def _top_level_rules(css: str) -> list[str]:
    """Selectors of rules at depth 0 — the ones that can shadow each other.

    Rules nested in `@media`/`@container`/`@supports` are excluded: repeating a
    selector inside a conditional block is the whole point of one.
    """
    selectors, depth, buf = [], 0, ""
    for ch in _without_comments(css):
        if ch == "{":
            if depth == 0 and not buf.strip().startswith("@"):
                selectors.append(" ".join(buf.split()))
            depth += 1
            buf = ""
        elif ch == "}":
            depth -= 1
            buf = ""
        elif depth == 0:
            buf += ch
    return selectors


@pytest.mark.parametrize("path", STYLESHEETS, ids=lambda p: p.parent.name)
def test_braces_balance(path):
    css = _without_comments(path.read_text())
    assert css.count("{") == css.count("}"), f"{path.parent.name}: unbalanced braces"


@pytest.mark.parametrize("path", STYLESHEETS, ids=lambda p: p.parent.name)
def test_no_selector_is_defined_twice_at_top_level(path):
    """Two rules for one selector means an edit landed in the copy that loses."""
    seen = [s for s in _top_level_rules(path.read_text())]
    # The theme header legitimately sets `.fireflyer-chart` twice: tokens first,
    # then the card's own box. Everything else must be unique.
    dupes = {s for s in seen if seen.count(s) > 1} - {".fireflyer-chart"}
    assert not dupes, f"{path.parent.name}: duplicated selectors {sorted(dupes)}"


@pytest.mark.parametrize("path", STYLESHEETS, ids=lambda p: p.parent.name)
def test_anchor_positioning_stays_behind_its_supports_guard(path):
    """`position: fixed` + `inset: auto` outside `@supports (anchor-name: --a)`
    pins the card at its *static* position wherever anchoring is unsupported —
    the exact failure the guard exists to prevent."""
    css = _without_comments(path.read_text())
    unguarded = re.sub(r"@supports \(anchor-name: --a\) \{.*?\n\}", "", css, flags=re.S)
    for rule in re.findall(r"\{[^{}]*\}", unguarded):
        assert not ("position: fixed" in rule and "inset: auto" in rule), (
            f"{path.parent.name}: anchored placement outside its @supports guard"
        )


@pytest.mark.parametrize("path", STYLESHEETS, ids=lambda p: p.parent.name)
def test_tooltips_are_opaque(path):
    """A translucent card picks up whatever it opens over, so the same tooltip
    reads as a different shade depending on the item behind it. Chart tooltips
    are opaque (SKILL.md, "Tooltips"). Dashboard chrome is a separate component
    and keeps its own look."""
    css = _without_comments(path.read_text())
    for rule in re.findall(r"\{[^{}]*\}", css):
        if "--ff-tooltip-bg" not in rule:
            continue
        assert "backdrop-filter" not in rule, f"{path.parent.name}: translucent tooltip"
        assert "transparent" not in rule, f"{path.parent.name}: translucent tooltip"


@pytest.mark.parametrize("path", STYLESHEETS, ids=lambda p: p.parent.name)
def test_comments_contain_no_literal_markup(path):
    """A stylesheet is inlined into the chart's own output, so a comment
    mentioning `<th>` or `<rect>` lands in the HTML and gets matched by any test
    that scrapes markup. It has cost real debugging three times: a `<rect>` in a
    bar comment broke six tests, and a `<th>` in a table comment made a route
    test read a CSS block as a table header. Name the element in prose instead.
    """
    template = (path.parent / "chart.html").read_text()
    emitted = set(re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)[\s>]", template))
    for comment in re.findall(r"/\*.*?\*/", path.read_text(), re.S):
        for tag in re.findall(r"<(/?[a-zA-Z][a-zA-Z0-9]*)\s*/?>", comment):
            assert tag.lstrip("/") not in emitted, (
                f"{path.parent.name}: comment writes <{tag}>, which this chart's "
                "template also emits — name the element in prose instead"
            )
