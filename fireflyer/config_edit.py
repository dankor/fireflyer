"""Editing a single chart's config from the browser modal.

Three pure functions, kept out of the web layer so they unit-test without
FastAPI/anthropic:

- `build_form`   — YAML + chart id -> the modal's form HTML (built from the
  chart class's `PARAMS`).
- `apply_edit`   — YAML + chart id + submitted form -> new YAML with only that
  chart's block rewritten (surgical, everything else verbatim).

The thin `app.py` endpoints just call these.
"""

import json
import re
from dataclasses import MISSING, fields as dataclass_fields
from html import escape

import yaml

from fireflyer.dashboard import CHART_TYPES, DashboardError
from fireflyer.params import ParamContext
from fireflyer.scan import scan


class ConfigEditError(ValueError):
    """Raised for an un-editable request (unknown chart, unparseable YAML)."""


def _load(text: str) -> dict:
    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigEditError(f"invalid YAML: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigEditError("YAML is not a mapping")
    return config




def _columns(dataset: str | None, resolve=None) -> list[str]:
    """Column names of a dataset's Parquet, or [] if it can't be read (reads
    only the schema). `dataset` is a name resolved via `resolve` (the app's
    dataset store) or a Parquet path directly."""
    if not dataset:
        return []
    try:
        return list(scan(dataset, resolve).collect_schema().names())
    except Exception:
        return []


def _chart_config(config: dict, cid: str) -> dict:
    charts = config.get("charts")
    if not isinstance(charts, dict) or cid not in charts:
        raise ConfigEditError(f"unknown chart {cid!r}")
    cfg = charts[cid]
    if not isinstance(cfg, dict):
        raise ConfigEditError(f"chart {cid!r} is not a mapping")
    return cfg


def _context(config: dict, cfg: dict, resolve=None) -> ParamContext:
    # `dataset` is a name (resolved via `resolve`) or a Parquet path directly.
    dataset = cfg.get("dataset")
    return ParamContext(
        datasets={}, dataset_id=dataset, columns=_columns(dataset, resolve)
    )


def _type_select(current: str) -> str:
    """The chart-type dropdown. Changing it re-fetches the form (`type_override`)
    so the fields match the new type's PARAMS."""
    opts = "".join(
        f'<option value="{escape(t, quote=True)}"'
        f'{" selected" if t == current else ""}>{escape(t)}</option>'
        for t in CHART_TYPES
    )
    return (
        '<div class="ff-field" data-param="type" data-kind="type">'
        '<label class="ff-field-label">Chart type</label>'
        f'<select class="ff-input" name="type" data-type-select>{opts}</select>'
        '</div>'
    )


def build_form(text: str, cid: str, type_override: str = "", resolve=None) -> str:
    """Render the edit-modal form for chart `cid` against the current YAML.

    `type_override` (set when the user changes the type dropdown) builds the form
    for a different chart type, carrying over the current config's overlapping
    values (dataset, title, filters, shared columns) and defaulting the rest.
    `resolve` (the app's dataset store) turns the chart's dataset name into its
    Parquet so the column dropdown can list its columns."""
    config = _load(text)
    cfg = _chart_config(config, cid)
    ctype = type_override or cfg.get("type")
    cls = CHART_TYPES.get(ctype)
    if cls is None:
        raise ConfigEditError(f"chart {cid!r}: unknown type {ctype!r}")

    ctx = _context(config, cfg, resolve)
    fields = _type_select(ctype) + "".join(
        p.render(cfg.get(p.name), ctx) for p in cls.PARAMS
    )
    return (
        f'<form id="ff-modal-form" data-cid="{escape(cid, quote=True)}">'
        f'<div class="ff-modal-head">'
        f'<span class="ff-modal-title">Edit <b>{escape(cid)}</b></span>'
        f'</div>'
        f'<div class="ff-modal-body">{fields}</div>'
        f'<div class="ff-modal-error" hidden></div>'
        f'<div class="ff-modal-foot">'
        f'<button type="button" class="ff-btn ff-cancel">Cancel</button>'
        f'<button type="submit" class="ff-btn ff-primary">Save</button>'
        f'</div>'
        f'</form>'
    )


# --- surgical block replace ---------------------------------------------------


def _find_block(lines: list[str], cid: str) -> tuple[int, int, int]:
    """Return (start, end, indent) for chart `cid`'s block within `lines`.

    `start`..`end` is the half-open line range of the block (its key line plus
    every deeper-indented line, excluding trailing blank lines). `indent` is the
    key line's indentation. Raises if the block isn't found in flow the parser
    can see (block-style mapping under `charts:`)."""
    charts_i = next(
        (i for i, ln in enumerate(lines) if ln.rstrip() == "charts:"), None
    )
    if charts_i is None:
        raise ConfigEditError("no `charts:` section to edit")

    start = None
    key_indent = 0
    for i in range(charts_i + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent == 0:
            break  # left the charts: section
        stripped = ln.strip()
        if indent > 0 and (stripped == f"{cid}:" or stripped.startswith(f"{cid}:")):
            # Guard against a same-named key nested deeper: only the direct child
            # of charts: (the shallowest key indent) counts.
            start = i
            key_indent = indent
            break
    if start is None:
        raise ConfigEditError(f"chart {cid!r} not found in the YAML text")

    end = start + 1
    last_content = start
    while end < len(lines):
        ln = lines[end]
        if ln.strip() == "":
            end += 1
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent <= key_indent:
            break
        last_content = end
        end += 1
    return start, last_content + 1, key_indent


def emit_chart_block(cid: str, ctype: str, kwargs: dict, params, indent: int) -> str:
    """The chart's YAML block: `<cid>:` at `indent`, `type` first, then each
    param's value in `PARAMS` order. None values and empty lists are dropped, so
    e.g. an unset map zoom or an empty filter list simply omit the key."""
    block: dict = {"type": ctype}
    for p in params:
        value = p.to_yaml(kwargs.get(p.name))
        if value is None:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        block[p.name] = value

    dumped = yaml.safe_dump(
        {cid: block}, sort_keys=False, default_flow_style=False, allow_unicode=True
    ).rstrip("\n")
    pad = " " * indent
    return "\n".join(pad + line if line else line for line in dumped.splitlines())


def replace_chart_block(text: str, cid: str, new_block: str) -> str:
    """Splice `new_block` in for chart `cid`'s block, leaving everything else
    (other charts, comments, blank lines) byte-for-byte intact."""
    lines = text.split("\n")
    start, end, _ = _find_block(lines, cid)
    spliced = lines[:start] + new_block.split("\n") + lines[end:]
    return "\n".join(spliced)


def apply_edit(text: str, cid: str, form) -> str:
    """Parse the submitted `form` through the chart's `PARAMS`, rewrite that
    chart's block, and return the new YAML. Raises `ConfigEditError` /
    `DashboardError` if the result wouldn't parse."""
    config = _load(text)
    cfg = _chart_config(config, cid)
    # The submitted `type` (from the dropdown) can swap the chart type; fall back
    # to the existing type when the form didn't carry one.
    ctype = form.get("type") or cfg.get("type")
    cls = CHART_TYPES.get(ctype)
    if cls is None:
        raise ConfigEditError(f"chart {cid!r}: unknown type {ctype!r}")

    kwargs = {p.name: p.parse(form) for p in cls.PARAMS}
    lines = text.split("\n")
    _, _, indent = _find_block(lines, cid)
    new_block = emit_chart_block(cid, ctype, kwargs, cls.PARAMS, indent)
    new_text = replace_chart_block(text, cid, new_block)

    # Validate the whole document the same way a manual edit would be — this
    # smoke-instantiates every chart, so bad values surface as DashboardError.
    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(new_text)
    return new_text


# --- adding a new chart -------------------------------------------------------
#
# The "+" buttons in the editor's left gutter add a chart via the same modal.
# `build_add_form` renders the create form (defaults for a fresh chart);
# `add_chart` generates a unique id, appends the chart block, and splices a
# placement into the layout — either a new row or an extra cell on an existing
# row. Layout edits assume flow-style rows (`- ["@h", "a:1", ...]`), which is
# what the editor always writes; hand-written block-style rows aren't handled.


def _defaults(cls) -> dict:
    """Constructor defaults for a chart, so the add form starts pre-filled."""
    out = {}
    for f in dataclass_fields(cls):
        if f.default is not MISSING:
            out[f.name] = f.default
        elif f.default_factory is not MISSING:  # e.g. filters -> []
            out[f.name] = f.default_factory()
        else:
            out[f.name] = None
    return out


def build_add_form(text: str, ctype: str, add_mode: str, add_index: str) -> str:
    """Render the create-chart modal for a fresh chart of type `ctype`. Carries
    the placement (`add_mode`/`add_index`) as hidden inputs so the save knows
    where to drop the chart."""
    config = _load(text)
    cls = CHART_TYPES.get(ctype)
    if cls is None:
        raise ConfigEditError(f"unknown type {ctype!r}")

    # No datasets: block anymore — the new chart's dataset starts blank (the
    # author types a dataset name / picks one once the gallery is wired).
    ctx = ParamContext(datasets={}, dataset_id=None, columns=[])
    values = _defaults(cls)
    values["dataset"] = ""
    if not values.get("title"):
        values["title"] = ctype.capitalize()

    hidden = (
        f'<input type="hidden" name="add_mode" value="{escape(add_mode, quote=True)}">'
        f'<input type="hidden" name="add_index" value="{escape(str(add_index), quote=True)}">'
    )
    fields = _type_select(ctype) + "".join(
        p.render(values.get(p.name), ctx) for p in cls.PARAMS
    )
    return (
        '<form id="ff-modal-form" data-mode="add">'
        '<div class="ff-modal-head"><span class="ff-modal-title">Add chart</span></div>'
        f'<div class="ff-modal-body">{hidden}{fields}</div>'
        '<div class="ff-modal-error" hidden></div>'
        '<div class="ff-modal-foot">'
        '<button type="button" class="ff-btn ff-cancel">Cancel</button>'
        '<button type="submit" class="ff-btn ff-primary">Add</button>'
        '</div></form>'
    )


def _unique_cid(config: dict, base: str) -> str:
    existing = set(config.get("charts") or {})
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _section_i(lines: list[str], key: str) -> int:
    i = next((i for i, ln in enumerate(lines) if ln.rstrip() == f"{key}:"), None)
    if i is None:
        raise ConfigEditError(f"no `{key}:` section")
    return i


def _charts_child_indent(lines: list[str]) -> int:
    ci = _section_i(lines, "charts")
    for ln in lines[ci + 1:]:
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        return indent if indent > 0 else 4
    return 4


def _insert_chart_block(text: str, block: str) -> str:
    """Append `block` at the end of the `charts:` section, blank-line separated."""
    lines = text.split("\n")
    ci = _section_i(lines, "charts")
    end = len(lines)
    for i in range(ci + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) == 0:  # next top-level key
            end = i
            break
    last = ci
    for i in range(ci + 1, end):
        if lines[i].strip() != "":
            last = i
    spliced = lines[:last + 1] + ["", *block.split("\n")] + lines[last + 1:]
    return "\n".join(spliced)


_ROW_LINE = re.compile(r"^\s*-\s*\[")


def _row_line_indices(lines: list[str], dash_i: int) -> list[int]:
    """Line indices of flow-style row items under `dashboard:`, in ordinal order."""
    out = []
    for i in range(dash_i + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "":
            continue
        if len(ln) - len(ln.lstrip()) == 0 and ln.rstrip().endswith(":"):
            break
        if _ROW_LINE.match(ln):
            out.append(i)
    return out


def _item_line_indices(lines: list[str], dash_i: int) -> list[int]:
    """Line indices of ALL layout items (rows, headers, separators) under
    `dashboard:`, in document order. `before` positions index into this list, so
    a chart can be dropped into a new row in any gap — including around a header
    or separator, not just between rows."""
    out = []
    for i in range(dash_i + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) == 0 and ln.rstrip().endswith(":"):
            break
        if ln.lstrip().startswith("- "):
            out.append(i)
    return out


def _insert_at(lines: list[str], dash_i: int, before) -> int:
    """Line index to insert a new layout item before. `before` is an index into
    the full item list (rows + headers + separators), or "end" to append."""
    items = _item_line_indices(lines, dash_i)
    if before in ("end", None, ""):
        return (items[-1] + 1) if items else (dash_i + 1)
    k = int(before)
    if 0 <= k < len(items):
        return items[k]
    return (items[-1] + 1) if items else (dash_i + 1)


def _insert_layout_row(text: str, cid: str, before) -> str:
    """Insert a new single-chart row. `before` is a layout-item index to insert
    above, or "end" to append after the last item."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    rows = _row_line_indices(lines, di)
    indent = "  "
    if rows:
        ln = lines[rows[0]]
        indent = ln[:len(ln) - len(ln.lstrip())]
    new_line = f'{indent}- ["@30", "{cid}:1"]'
    insert_at = _insert_at(lines, di, before)
    spliced = lines[:insert_at] + [new_line] + lines[insert_at:]
    return "\n".join(spliced)


def _yaml_inline_str(s: str) -> str:
    """Header text as a YAML scalar: emitted raw when it's plainly a safe string,
    double-quoted (JSON, which is valid YAML) otherwise, so a colon or a leading
    special char can't turn the item into a mapping or break parsing."""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _./()%&+'?!,]*", s) and ":" not in s:
        return s
    return json.dumps(s, ensure_ascii=False)


def _header_line_indices(lines: list[str], dash_i: int) -> list[int]:
    """Line indices of header items (plain-string list items) under `dashboard:`,
    in document order — i.e. not rows (`- [`) and not separators (`- "-"`)."""
    out = []
    for i in range(dash_i + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "":
            continue
        if len(ln) - len(ln.lstrip()) == 0 and ln.rstrip().endswith(":"):
            break
        s = ln.strip()
        if not s.startswith("- "):
            continue
        rest = s[2:].strip()
        if rest.startswith("[") or rest in ('"-"', "'-'", "-"):
            continue
        out.append(i)
    return out


def set_header_text(text: str, index: int, new_text: str) -> str:
    """Rename the `index`-th header in the layout to `new_text`."""
    new_text = new_text.strip()
    if not new_text:
        raise ConfigEditError("header text cannot be empty")
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    headers = _header_line_indices(lines, di)
    if not (0 <= index < len(headers)):
        raise ConfigEditError(f"header {index} not found")
    ln = lines[headers[index]]
    indent = ln[:len(ln) - len(ln.lstrip())]
    lines[headers[index]] = f"{indent}- {_yaml_inline_str(new_text)}"
    result = "\n".join(lines)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


def insert_layout_item(text: str, kind: str, before) -> str:
    """Insert a header or separator into the layout. `kind` is 'header' (a plain
    string item, defaulting to "New header") or 'separator' (the `"-"` item).
    `before` is a layout-item index to insert above, or "end" to append."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    rows = _row_line_indices(lines, di)
    indent = "  "
    if rows:
        ln = lines[rows[0]]
        indent = ln[:len(ln) - len(ln.lstrip())]
    if kind == "header":
        new_line = f"{indent}- New header"
    elif kind == "separator":
        new_line = f'{indent}- "-"'
    else:
        raise ConfigEditError(f"unknown layout item {kind!r}")
    insert_at = _insert_at(lines, di, before)
    new_text = "\n".join(lines[:insert_at] + [new_line] + lines[insert_at:])

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(new_text)
    return new_text


def _item_line(lines: list[str], dash_i: int, index: int) -> int:
    """Line of the layout item at full-list index `index`, guarded to a header
    or separator — rows (`- [ ... ]`) move/delete through their charts, not here."""
    items = _item_line_indices(lines, dash_i)
    if not (0 <= index < len(items)):
        raise ConfigEditError(f"layout item {index} not found")
    li = items[index]
    if lines[li].lstrip()[2:].lstrip().startswith("["):
        raise ConfigEditError("only headers and separators move or delete this way")
    return li


def move_layout_item(text: str, index: int, before) -> str:
    """Move the header/separator at layout-item `index` to before layout-item
    `before` (or "end"). Only between-item gaps are valid targets — a header or
    separator never lives inside a row."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    src = _item_line(lines, di, index)
    insert_at = _insert_at(lines, di, before)
    line = lines.pop(src)
    if insert_at > src:            # removing the source shifted everything below up
        insert_at -= 1
    lines.insert(insert_at, line)
    result = "\n".join(lines)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


def delete_layout_item(text: str, index: int) -> str:
    """Remove the header/separator at layout-item `index`."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    del lines[_item_line(lines, di, index)]
    result = "\n".join(lines)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


# --- tabs ---------------------------------------------------------------------
#
# `dashboard:` is either a flat list of layout items or a **mapping** of tab
# name -> layout list. A tab acts as a section delimiter: it owns every row from
# its key line down to the next tab key. All the ops below are line-surgery on
# that structure, re-validated through `Dashboard.from_yaml`. Because tab keys
# sit one indent level *above* their rows, the flat row/item scanners
# (`_row_line_indices`, `_item_line_indices`) still collect everything across
# tabs in document order — so the existing chart/row gestures keep working.


def _section_end(lines: list[str], dash_i: int) -> int:
    """Index one past the last line of the `dashboard:` section (the next
    top-level key, or EOF)."""
    for i in range(dash_i + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) == 0:
            return i
    return len(lines)


def _dashboard_indent(lines: list[str], dash_i: int) -> int:
    """Indent of the shallowest content under `dashboard:` — the tab-key indent
    when tabbed, or the item indent when flat. Defaults to 2."""
    for i in range(dash_i + 1, _section_end(lines, dash_i)):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        return len(ln) - len(ln.lstrip())
    return 2


def _tab_key_line_indices(lines: list[str], dash_i: int) -> list[int]:
    """Line indices of tab keys (mapping keys at the base indent that aren't
    list items) under `dashboard:`, in document order. Empty when the dashboard
    is flat — so it doubles as the tabbed/flat test."""
    base = _dashboard_indent(lines, dash_i)
    out = []
    for i in range(dash_i + 1, _section_end(lines, dash_i)):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent == base and not ln.lstrip().startswith("- "):
            out.append(i)
    return out


def tab_names(text: str) -> list[str]:
    """Tab names in order (for the delete-first confirm listing). [] when flat."""
    from fireflyer.dashboard import Dashboard

    return [t.name for t in (Dashboard.from_yaml(text).tabs or [])]


def add_first_tab(text: str, name: str = "New tab") -> str:
    """Convert a flat dashboard into a single-tab one: wrap the whole layout in a
    `<name>:` key (rows indent one level deeper). The editor renames it in place
    afterwards."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    if _tab_key_line_indices(lines, di):
        raise ConfigEditError("dashboard already has tabs")
    end = _section_end(lines, di)
    body = lines[di + 1:end]
    if not any(ln.strip() for ln in body):
        raise ConfigEditError("dashboard is empty — nothing to put in a tab")
    indented = ["  " + ln if ln.strip() else ln for ln in body]
    key_line = f"  {_yaml_inline_str(name)}:"
    result = "\n".join(lines[:di + 1] + [key_line] + indented + lines[end:])

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


def insert_tab(text: str, before, name: str = "New tab") -> str:
    """Add a tab by inserting a `<name>:` key at the `before` gap (a layout-item
    index). Splits the current tab there: rows below become the new tab, rows
    above stay in the previous one (indentation already nests them)."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    if not _tab_key_line_indices(lines, di):
        raise ConfigEditError("dashboard is not tabbed yet; add the first tab first")
    base = _dashboard_indent(lines, di)
    key_line = f"{' ' * base}{_yaml_inline_str(name)}:"
    insert_at = _insert_at(lines, di, before)
    result = "\n".join(lines[:insert_at] + [key_line] + lines[insert_at:])

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


def set_tab_text(text: str, index: int, new_name: str) -> str:
    """Rename the `index`-th tab."""
    new_name = new_name.strip()
    if not new_name:
        raise ConfigEditError("tab name cannot be empty")
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    keys = _tab_key_line_indices(lines, di)
    if not (0 <= index < len(keys)):
        raise ConfigEditError(f"tab {index} not found")
    ln = lines[keys[index]]
    indent = ln[:len(ln) - len(ln.lstrip())]
    lines[keys[index]] = f"{indent}{_yaml_inline_str(new_name)}:"
    result = "\n".join(lines)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


def move_tab(text: str, index: int, before) -> str:
    """Move the `index`-th tab's key line to the `before` gap (a layout-item
    index). Delimiter-style: the rows that then fall under it become its content,
    so the move both reorders and repositions the tab boundary. An invalid result
    (orphaned rows, an emptied tab) is rejected by re-validation."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    keys = _tab_key_line_indices(lines, di)
    if not (0 <= index < len(keys)):
        raise ConfigEditError(f"tab {index} not found")
    if index == 0:
        # Moving the first tab's key down orphans the rows above it — there'd be
        # no tab owning them. The editor hides its move button; guard anyway.
        raise ConfigEditError("the first tab can't be moved")
    src = keys[index]
    insert_at = _insert_at(lines, di, before)
    line = lines.pop(src)
    if insert_at > src:            # removing the source shifted everything below up
        insert_at -= 1
    lines.insert(insert_at, line)
    result = "\n".join(lines)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


def delete_tab(text: str, index: int) -> str:
    """Remove a tab. The **first** tab dissolves *all* tabs back to a flat list
    (rows dedent one level); any other tab merges its rows into the previous tab
    (just drop its key line)."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    keys = _tab_key_line_indices(lines, di)
    if not (0 <= index < len(keys)):
        raise ConfigEditError(f"tab {index} not found")

    if index == 0:
        base = _dashboard_indent(lines, di)
        end = _section_end(lines, di)
        key_set = set(keys)
        # Nesting step = how much deeper rows sit than the tab key.
        step = 2
        for i in range(di + 1, end):
            if lines[i].lstrip().startswith("- "):
                step = (len(lines[i]) - len(lines[i].lstrip())) - base
                break
        body = []
        for i in range(di + 1, end):
            ln = lines[i]
            if i in key_set:
                continue                       # drop every tab key
            if ln.strip() == "":
                body.append(ln)
                continue
            indent = len(ln) - len(ln.lstrip())
            body.append(" " * max(base, indent - step) + ln.lstrip())
        result = "\n".join(lines[:di + 1] + body + lines[end:])
    else:
        del lines[keys[index]]
        result = "\n".join(lines)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(result)
    return result


def _add_to_layout_row(text: str, cid: str, ordinal: int) -> str:
    """Append a cell to an existing row (weight 1, since widths are proportions)."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    rows = _row_line_indices(lines, di)
    if not (0 <= ordinal < len(rows)):
        raise ConfigEditError(f"row {ordinal} not found")
    ln = lines[rows[ordinal]]
    pos = ln.rindex("]")
    lines[rows[ordinal]] = ln[:pos] + f', "{cid}:1"' + ln[pos:]
    return "\n".join(lines)


def _remove_from_layout(text: str, cid: str) -> str:
    """Drop `cid`'s cell from every row it appears in; delete rows left empty."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    row_idx = set(_row_line_indices(lines, di))
    out = []
    for i, ln in enumerate(lines):
        if i not in row_idx:
            out.append(ln)
            continue
        toks = re.findall(r'"([^"]*)"', ln)          # ["@h", "a:1", "b:2", ...]
        widgets = toks[1:]
        kept = [w for w in widgets if w.split(":", 1)[0] != cid]
        if len(kept) == len(widgets):
            out.append(ln)                            # cid not in this row
        elif kept:
            indent = ln[:len(ln) - len(ln.lstrip())]
            arr = ", ".join(f'"{t}"' for t in [toks[0], *kept])
            out.append(f"{indent}- [{arr}]")
        # else: row now has no widgets -> drop the line entirely
    return "\n".join(out)


def _remove_chart_block(text: str, cid: str) -> str:
    lines = text.split("\n")
    start, end, _ = _find_block(lines, cid)
    if end < len(lines) and lines[end].strip() == "":
        end += 1                                       # swallow one trailing blank
    del lines[start:end]
    return "\n".join(lines)


def delete_chart(text: str, cid: str) -> str:
    """Remove a chart: its `charts:` block and every layout placement (rows left
    empty are deleted). Returns the new YAML, validated."""
    config = _load(text)
    if cid not in (config.get("charts") or {}):
        raise ConfigEditError(f"unknown chart {cid!r}")
    text = _remove_from_layout(text, cid)
    text = _drop_empty_tabs(text)   # a tab emptied by the delete dissolves
    text = _remove_chart_block(text, cid)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(text)
    return text


def _row_tokens(line: str) -> list[str]:
    """Quoted tokens of a flow row line: [height, "id:w", ...]."""
    return re.findall(r'"([^"]*)"', line)


def _rebuild_row(indent: str, height: str, widgets: list[str]) -> str:
    arr = ", ".join(f'"{t}"' for t in [height, *widgets])
    return f"{indent}- [{arr}]"


def _tid(token: str) -> str:
    """Chart id of a widget token, with or without a `:width` suffix."""
    return token.split(":", 1)[0]


def _fmt_w(w: float) -> str:
    """Format a width: integers stay integers, otherwise trim to 3 decimals."""
    return str(int(round(w))) if abs(w - round(w)) < 1e-9 else str(round(w, 3))


def _col_span(spec: str) -> tuple[int, int]:
    """(start0, span) from a CSS grid-column/-row value: '3' or '2 / span 2'."""
    if "/" in spec:
        start, rest = spec.split("/", 1)
        return int(start) - 1, int(rest.strip().split()[-1])
    return int(spec) - 1, 1


def _dashboard_rows(text: str):
    """(lines, metas) where each row meta is {i, height, tokens, indent}."""
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    metas = []
    for i in _row_line_indices(lines, di):
        toks = _row_tokens(lines[i])
        metas.append({
            "i": i, "height": toks[0], "tokens": toks[1:],
            "indent": lines[i][:len(lines[i]) - len(lines[i].lstrip())],
        })
    return lines, metas


def _emit(lines: list[str], metas: list[dict]) -> str:
    """Re-emit changed row lines (dropping emptied rows), keep everything else."""
    by_line = {m["i"]: m for m in metas}
    out = []
    for i, ln in enumerate(lines):
        m = by_line.get(i)
        if m is None:
            out.append(ln)
        elif m["tokens"]:
            out.append(_rebuild_row(m["indent"], m["height"], m["tokens"]))
        # else: emptied row -> drop
    return "\n".join(out)


def _repair(metas: list[dict]) -> None:
    """Dissolve any span a move left invalid: a chart in several rows is kept only
    in its fullest row (the other occurrences are dropped), so the span collapses
    to where it fits best."""
    occ: dict[str, list[int]] = {}
    for mi, m in enumerate(metas):
        for t in m["tokens"]:
            occ.setdefault(_tid(t), []).append(mi)
    for cid, mis in occ.items():
        if len(mis) < 2:
            continue
        keep = max(mis, key=lambda mi: len(metas[mi]["tokens"]))
        for mi in mis:
            if mi != keep:
                metas[mi]["tokens"] = [t for t in metas[mi]["tokens"] if _tid(t) != cid]


def _drop_empty_tabs(text: str) -> str:
    """Remove any tab whose rows were all moved away — a move that empties a tab
    dissolves it (its key line goes). No-op on a flat dashboard."""
    lines = text.split("\n")
    try:
        di = _section_i(lines, "dashboard")
    except ConfigEditError:
        return text
    keys = _tab_key_line_indices(lines, di)
    if not keys:
        return text
    end = _section_end(lines, di)
    bounds = keys + [end]
    drop = {
        keys[n]
        for n in range(len(keys))
        if not any(
            lines[j].lstrip().startswith("- ")
            for j in range(keys[n] + 1, bounds[n + 1])
        )
    }
    if not drop:
        return text
    return "\n".join(ln for i, ln in enumerate(lines) if i not in drop)


def _finalize(text: str) -> str:
    """Validate the moved layout; drop tabs a move emptied, and if a span was
    broken, dissolve it and re-check."""
    from fireflyer.dashboard import Dashboard

    text = _drop_empty_tabs(text)
    try:
        Dashboard.from_yaml(text)
        return text
    except DashboardError:
        lines, metas = _dashboard_rows(text)
        _repair(metas)
        repaired = _drop_empty_tabs(_emit(lines, metas))
        Dashboard.from_yaml(repaired)   # re-raise if still invalid
        return repaired


def move_placement(text: str, src_cid: str, dst_cid: str, position: str) -> str:
    """Move `src_cid`'s cell next to `dst_cid` (`position` 'before'/'after').

    Span-aware: if `dst_cid` spans several rows (a merge), the newcomer adopts the
    span — inserted as `src:1` in the topmost row and **bare** in the others, so
    it inherits and spans too. Otherwise it's a plain single-row insert (`src:1`)
    and the renderer refills the neighbours automatically. A source row emptied by
    the move is dropped."""
    if src_cid == dst_cid or not src_cid or not dst_cid:
        return text
    lines, metas = _dashboard_rows(text)

    if not any(_tid(t) == src_cid for m in metas for t in m["tokens"]):
        raise ConfigEditError(f"chart {src_cid!r} not in the layout")

    # Pull the moving chart out of every row it's in (covers a spanned source).
    for m in metas:
        m["tokens"] = [t for t in m["tokens"] if _tid(t) != src_cid]

    # Every row that still holds the target, with the target's index in it. More
    # than one row means the target spans (a merge) -> the newcomer adopts it.
    dst_hits = [(mi, idx) for mi, m in enumerate(metas)
                for idx, t in enumerate(m["tokens"]) if _tid(t) == dst_cid]
    if not dst_hits:
        raise ConfigEditError(f"chart {dst_cid!r} not in the layout")

    off = 1 if position == "after" else 0
    if len(dst_hits) > 1:
        # Adopt the span: `src:1` in the topmost row, bare `src` in the rest.
        for n, (mi, idx) in enumerate(dst_hits):
            metas[mi]["tokens"].insert(idx + off, f"{src_cid}:1" if n == 0 else src_cid)
    else:
        mi, idx = dst_hits[0]
        metas[mi]["tokens"].insert(idx + off, f"{src_cid}:1")

    return _finalize(_emit(lines, metas))


def move_to_new_row(text: str, cid: str, before) -> str:
    """Move `cid` out of its current row(s) into a **new** single-chart row.
    `before` is a layout-item index to insert the new row above (so it can land
    around a header/separator), or "end". Removes every occurrence (covers a
    merged source); an emptied row is dropped."""
    if not cid:
        return text
    lines = text.split("\n")
    di = _section_i(lines, "dashboard")
    start = di + 1
    end = len(lines)
    for i in range(start, len(lines)):
        ln = lines[i]
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) == 0:   # next top-level key ends the section
            end = i
            break
    body = lines[start:end]
    row_pos = [j for j, ln in enumerate(body) if _ROW_LINE.match(ln)]
    # All items (rows, headers, separators), so `before` can target any gap.
    item_pos = [j for j, ln in enumerate(body) if ln.lstrip().startswith("- ")]
    indent = "  "
    if row_pos:
        ln0 = body[row_pos[0]]
        indent = ln0[:len(ln0) - len(ln0.lstrip())]

    if not any(_tid(w) == cid for j in row_pos for w in _row_tokens(body[j])[1:]):
        raise ConfigEditError(f"chart {cid!r} not in the layout")

    # A lone chart fills its new row, so the width is irrelevant — emit `:1`.
    new_row = f'{indent}- ["@30", "{cid}:1"]'
    if before in ("end", None, ""):
        anchor_j = len(body)
    else:
        k = int(before)
        anchor_j = item_pos[k] if 0 <= k < len(item_pos) else len(body)

    new_body = []
    for j, ln in enumerate(body):
        if j == anchor_j:
            new_body.append(new_row)
        if j in row_pos:
            toks = _row_tokens(ln)
            kept = [w for w in toks[1:] if _tid(w) != cid]
            if kept:
                r_indent = ln[:len(ln) - len(ln.lstrip())]
                new_body.append(_rebuild_row(r_indent, toks[0], kept))
            # else: emptied row -> drop
        else:
            new_body.append(ln)
    if anchor_j >= len(body):
        new_body.append(new_row)

    result = "\n".join(lines[:start] + new_body + lines[end:])
    return _finalize(result)


def _adjacent_below(lines: list[str], metas: list[dict], mi: int) -> int | None:
    """Meta index of the row directly below `mi` when they're adjacent (nothing
    but blanks/comments between them — a header or separator breaks adjacency)."""
    if mi + 1 >= len(metas):
        return None
    for k in range(metas[mi]["i"] + 1, metas[mi + 1]["i"]):
        s = lines[k].strip()
        if s and not s.startswith("#"):
            return None
    return mi + 1


def move_span(text: str, src_cid: str, dst_cid: str, position: str) -> str:
    """Place `src_cid` as a tall line spanning **`dst`'s whole span plus the row
    directly below it** (merge current row + 1 below). `src:<w>` goes next to
    `dst` in the top row and a bare `src` at the matching spot in every row below,
    so it inherits and spans. If `dst` already spans rows 1-2 and row 3 is
    adjacent, `src` spans rows 1-3. Degrades to a single-row insert when there's
    no adjacent row below. A source row emptied by the move is dropped."""
    if src_cid == dst_cid or not src_cid or not dst_cid:
        return text
    lines, metas = _dashboard_rows(text)
    src_tok = next((t for m in metas for t in m["tokens"] if _tid(t) == src_cid), None)
    if src_tok is None:
        raise ConfigEditError(f"chart {src_cid!r} not in the layout")
    # Keep the chart's current width so the merged column stays the same size.
    src_w = src_tok.split(":", 1)[1] if ":" in src_tok else "1"

    for m in metas:
        m["tokens"] = [t for t in m["tokens"] if _tid(t) != src_cid]

    dst_rows = [mi for mi, m in enumerate(metas)
                if any(_tid(t) == dst_cid for t in m["tokens"])]
    if not dst_rows:
        raise ConfigEditError(f"chart {dst_cid!r} not in the layout")

    off = 1 if position == "after" else 0
    place_at = 0
    for n, mi in enumerate(dst_rows):
        toks = metas[mi]["tokens"]
        di = next(i for i, t in enumerate(toks) if _tid(t) == dst_cid)
        place_at = di + off
        toks.insert(place_at, f"{src_cid}:{src_w}" if n == 0 else src_cid)

    below = _adjacent_below(lines, metas, dst_rows[-1])
    if below is not None:
        toks = metas[below]["tokens"]
        toks.insert(min(place_at, len(toks)), src_cid)   # bare -> inherits -> spans

    return _finalize(_emit(lines, metas))


def merge_down(text: str, cid: str) -> str:
    """Extend `cid`'s own span **down by one row**: add a bare `cid` in the row
    directly below its current last row, so it inherits and grows into it
    (current rows + 1). A single-row chart becomes a 2-row line; a 2-row line
    becomes 3. Errors if there's no adjacent chart-row below."""
    if not cid:
        return text
    lines, metas = _dashboard_rows(text)
    cid_rows = [mi for mi, m in enumerate(metas)
                if any(_tid(t) == cid for t in m["tokens"])]
    if not cid_rows:
        raise ConfigEditError(f"chart {cid!r} not in the layout")
    below = _adjacent_below(lines, metas, cid_rows[-1])
    if below is None:
        raise ConfigEditError(f"no row below {cid!r} to merge into")
    last = metas[cid_rows[-1]]["tokens"]
    idx = next(i for i, t in enumerate(last) if _tid(t) == cid)
    toks = metas[below]["tokens"]
    toks.insert(min(idx, len(toks)), cid)   # bare -> inherits -> span grows down
    return _finalize(_emit(lines, metas))


def resize_columns(text: str, ordinals: list[int], widths: list[float]) -> str:
    """Rewrite a merge group's column widths after a boundary drag.

    `ordinals` are the group's YAML row ordinals (they share one union column
    grid); `widths` is the new fine-column width vector (the rendered grid
    tracks, in that order). Each cell's new width is the sum of the fine columns
    it spans, so dragging a boundary updates **every** row those columns belong
    to — even when the drag started on an inherited (lower) row. A cell that
    spans down from the row above stays **bare**, so the span holds and the first
    row keeps owning the sizes."""
    from fireflyer.dashboard import Dashboard

    ordinals = [int(o) for o in ordinals]
    widths = [float(w) for w in widths]
    if not ordinals or not widths:
        return text
    # Row ordinals are global across tabs, so search flat items *and* every tab's
    # items — otherwise a column drag on a tabbed dashboard finds no group and
    # silently no-ops (the widths snap back).
    dash = Dashboard.from_yaml(text)
    layout_items = (
        dash.items if dash.tabs is None else [it for t in dash.tabs for it in t.items]
    )
    group = next(
        (it for it in layout_items
         if hasattr(it, "placements") and [o for o, _ in it.tracks] == ordinals),
        None,
    )
    if group is None:
        return text
    placed = {p.chart_id: p for p in group.placements}

    lines, metas = _dashboard_rows(text)
    for gi, ordinal in enumerate(ordinals):
        new_tokens = []
        for token in metas[ordinal]["tokens"]:
            cid = _tid(token)
            p = placed.get(cid)
            if p is None:
                new_tokens.append(token)
                continue
            c0, cspan = _col_span(p.grid_column)
            starts_here = _col_span(p.grid_row)[0] == gi
            if starts_here:                       # this row owns the cell
                new_tokens.append(f"{cid}:{_fmt_w(sum(widths[c0:c0 + cspan]))}")
            else:                                 # inherited span -> keep bare
                new_tokens.append(cid)
        metas[ordinal]["tokens"] = new_tokens

    return _finalize(_emit(lines, metas))


def add_chart(text: str, form) -> str:
    """Create a new chart from the submitted add-form and place it in the layout.
    Returns the new YAML (validated), or raises on bad input."""
    config = _load(text)
    ctype = form.get("type") or ""
    cls = CHART_TYPES.get(ctype)
    if cls is None:
        raise ConfigEditError(f"unknown type {ctype!r}")

    cid = _unique_cid(config, ctype)
    kwargs = {p.name: p.parse(form) for p in cls.PARAMS}
    indent = _charts_child_indent(text.split("\n"))
    block = emit_chart_block(cid, ctype, kwargs, cls.PARAMS, indent)
    text = _insert_chart_block(text, block)

    add_mode = form.get("add_mode") or "row"
    add_index = form.get("add_index") or "end"
    if add_mode == "cell":
        text = _add_to_layout_row(text, cid, int(add_index))
    else:
        text = _insert_layout_row(text, cid, add_index)

    from fireflyer.dashboard import Dashboard

    Dashboard.from_yaml(text)
    return text
