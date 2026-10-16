"""What the editor's YAML code editor asks the server while you type.

Two answers, both pure so they test without the web stack:

- `lint` — the problems in a dashboard's YAML, each pinned to a line and,
  where it can be found, the exact token: what `Dashboard.from_yaml` would
  reject, said where it is rather than as one message for the whole file.
- `schema` — what autocomplete can offer: each chart type's keys and what
  kind of value each takes (straight from the charts' own `PARAMS`, so a new
  chart type completes with no change here), the datasets and their columns,
  and the calcs per dataset.
"""

from __future__ import annotations

import re

import yaml

from fireflyer import calcs as calcs_mod
from fireflyer import config_edit
from fireflyer import inline_data
from fireflyer.dashboard import CHART_TYPES, Dashboard, DashboardError
from fireflyer.params import ChoiceParam


# --- lint ---------------------------------------------------------------------


def lint(text: str, datasets=None) -> list[dict]:
    """The problems in `text`, each `{line, from, to, message, severity}` —
    `line` 1-based, `from`/`to` 0-based columns on it (equal when there's no
    token to underline). Empty when the dashboard is fine."""
    problems = _tab_indents(text)
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        problem = _yaml_problem(text, exc)
        # A tab already said plainly on that line needs no second, cryptic say.
        if not any(p["line"] == problem["line"] for p in problems):
            problems.append(problem)
        return problems
    try:
        Dashboard.from_yaml(text, datasets=datasets)
    except DashboardError as exc:
        problems.append(_dashboard_problem(text, str(exc)))
    return problems


def _tab_indents(text: str) -> list[dict]:
    """A tab in indentation is the classic way a hand-edited YAML file breaks
    — YAML forbids it, and it looks exactly like spaces. Said plainly, on the
    line, before the parser's own more cryptic complaint."""
    out = []
    for n, line in enumerate(text.split("\n"), start=1):
        indent = len(line) - len(line.lstrip(" \t"))
        if "\t" in line[:indent]:
            out.append(_problem(n, 0, indent, "Tab in indentation — YAML only allows spaces"))
    return out


def _yaml_problem(text: str, exc: yaml.YAMLError) -> dict:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    message = " ".join(
        str(part) for part in (getattr(exc, "context", None), getattr(exc, "problem", None)) if part
    ) or str(exc)
    if mark is None:
        return _problem(1, 0, 0, f"Invalid YAML: {message}")
    line = mark.line + 1
    # An unclosed `{` or `[` is only noticed on a later line; the parser knows
    # where it opened, which is where the mistake usually is.
    opened = getattr(exc, "context_mark", None)
    if opened is not None and opened.line != mark.line:
        message += f" (opened on line {opened.line + 1})"
    width = len(_line(text, line))
    col = min(mark.column, width)
    return _problem(line, col, min(col + 1, width), f"Invalid YAML: {message}")


# Messages from a chart class's constructor read as Python; say them plainly.
_UNKNOWN_KEY = re.compile(r"(\w+)\.__init__\(\) got an unexpected keyword argument '([^']+)'")
_MISSING_KEY = re.compile(r"(\w+)\.__init__\(\) missing \d+ required (?:positional |keyword-only )?arguments?: (.+)")


def _plain(message: str) -> str:
    m = _UNKNOWN_KEY.search(message)
    if m:
        return message.replace(m.group(0), f"'{m.group(2)}' isn't a key a {m.group(1).lower()} chart takes")
    m = _MISSING_KEY.search(message)
    if m:
        return message.replace(m.group(0), f"a {m.group(1).lower()} chart needs {m.group(2)}")
    return message


def _dashboard_problem(text: str, message: str) -> dict:
    """Pin a `DashboardError` to where it is. It names what's wrong in quotes
    (`chart 'a': unknown type 'piee'`), so look for those — the most specific,
    the last named, first — inside the chart it's about when it says which,
    falling back to that chart's line, then to the top of the file."""
    message = _plain(message)
    quoted = re.findall(r"'([^']+)'", message)
    scope = (1, len(text.split("\n")))
    chart = re.match(r"chart '([^']+)'", message)
    if chart:
        found = _chart_block(text, chart.group(1))
        if found:
            scope = found
    for token in reversed(quoted):
        hit = _find(text, token, *scope)
        if hit:
            return _problem(*hit, message)
    if chart and scope[0] > 1:
        line = _line(text, scope[0])
        start = len(line) - len(line.lstrip())
        return _problem(scope[0], start, len(line.rstrip()), message)
    return _problem(1, 0, 0, message)


def _chart_block(text: str, cid: str) -> tuple[int, int] | None:
    """The 1-based line range of chart `cid`'s block under `charts:`."""
    lines = text.split("\n")
    in_charts = False
    start = None
    for n, line in enumerate(lines, start=1):
        if line and not line[0].isspace() and not line.startswith("#"):
            if start:
                return start, n - 1
            in_charts = line.startswith("charts:")
            continue
        if in_charts and re.match(rf"\s{{1,4}}{re.escape(cid)}\s*:", line):
            if start:
                return start, n - 1
            start = n
        elif start and re.match(r"\s{1,4}[\w-]+\s*:", line) and _indent(line) <= _indent(lines[start - 1]):
            return start, n - 1
    return (start, len(lines)) if start else None


def _find(text: str, token: str, first: int, last: int):
    """`(line, from, to)` of `token` as a whole word within lines first..last."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")
    for n in range(first, last + 1):
        m = pattern.search(_line(text, n))
        if m:
            return n, m.start(), m.end()
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _line(text: str, n: int) -> str:
    lines = text.split("\n")
    return lines[n - 1] if 0 < n <= len(lines) else ""


def _problem(line: int, start: int, end: int, message: str, severity: str = "error") -> dict:
    return {"line": line, "from": start, "to": end, "message": message, "severity": severity}


# --- schema ---------------------------------------------------------------------


def schema(text: str, store=None) -> dict:
    """What autocomplete can offer for `text`:

    - `charts` — `{type: {key: {"kind", "choices"?}}}` from each chart's
      `PARAMS`; a kind is how a value completes: `dataset`, `column`, `calc`,
      `choice` (its `choices`), `bool`, `int`, `list`, `filters`, `text`;
    - `datasets` — `{name: [columns]}`: the dashboard's inline ones and the
      store's managed ones, column calcs included;
    - `calcs` — `{dataset: [keys]}` for the calcs that are values (what a
      chart's `calc:` takes);
    - `icons` — `{type: svg}`, each chart type's own `icon.svg`, for the
      editor's list of charts.
    Best-effort throughout: YAML that doesn't parse still completes keys."""
    charts = {
        name: {
            "type": {"kind": "type"},
            **{
                p.name: {"kind": p.kind, **({"choices": [str(c) for c in p.choices]} if isinstance(p, ChoiceParam) else {})}
                for p in cls.PARAMS
            },
        }
        for name, cls in CHART_TYPES.items()
    }
    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError:
        config = None
    config = config if isinstance(config, dict) else {}

    datasets: dict[str, list[str]] = {}
    for ds in (store.list() if store is not None and hasattr(store, "list") else []):
        datasets[ds.name] = [c.name for c in ds.columns]
    try:
        inline = inline_data.parse_block(config.get("datasets"))
    except inline_data.InlineDataError:
        inline = {}
    resolve = getattr(store, "source", None)
    for name in inline:
        datasets[name] = config_edit._columns(name, inline_data.resolver(inline, resolve))

    calcs: dict[str, list[str]] = {}
    block = config.get("calcs")
    for dataset, defs in (block.items() if isinstance(block, dict) else []):
        if not isinstance(defs, dict):
            continue
        try:
            cset = calcs_mod.CalcSet.from_defs(defs)
            columns = set(cset.column_keys())
        except calcs_mod.CalcError:
            columns = set()
        calcs[str(dataset)] = [k for k in defs if k not in columns]
        datasets.setdefault(str(dataset), [])
        datasets[str(dataset)] = list(dict.fromkeys(datasets[str(dataset)] + sorted(columns)))
    icons = {name: cls.ICON for name, cls in CHART_TYPES.items()}
    return {"charts": charts, "datasets": datasets, "calcs": calcs, "icons": icons}
