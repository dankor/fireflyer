// The YAML pane as a code editor: CodeMirror 6, loaded as ES modules through
// the import map in editor.html, from files vendored into static/vendor by
// scripts/vendor.py — no npm, no build, and no network.
//
// The <textarea id="code"> stays the one source of truth. Everything in
// editor.js — the chart modals, the calcs manager, chat, Save — reads and
// writes `codeEl.value`, and listens for its `input` event. So this editor
// mirrors it both ways rather than replacing it:
//   - an edit here is written into the textarea, and an `input` event fired,
//     exactly as typing into it would;
//   - a write to `codeEl.value` from elsewhere is caught by a setter on the
//     textarea and applied here as the smallest change that gets there, so the
//     cursor, scroll and undo history survive — Cmd/Ctrl+Z undoes a chart-modal
//     edit like any other.
// If this module fails to load, nothing above ran and the plain textarea is
// still there and still works.
//
// What it gives a developer: line numbers, folding, bracket matching, multiple
// cursors and rectangular selection, search and replace, go-to-line, toggle
// comment, smart indentation where Tab indents with spaces (YAML forbids tab
// characters), live problems from the server's own parse, completion that
// knows the dashboard (keys per chart type, chart types, datasets and their
// columns, calcs, ops, chart ids in the layout), and a status bar.
//
// Lines can be linked, as on GitHub: a click on a line number (shift-click for
// a range) or on an outline chip selects and highlights them and puts them in
// the address bar, `?line=12` or `?line=12-20`; a link with it opens the code
// panel there. The highlight follows edits, and the address bar with it.

import {EditorState, Compartment, StateEffect, StateField, RangeSet} from '@codemirror/state';
import {
  EditorView, Decoration, keymap, lineNumbers,
  highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars, drawSelection,
  dropCursor, rectangularSelection, crosshairCursor, showPanel, GutterMarker, gutterLineClass,
} from '@codemirror/view';
import {defaultKeymap, history, historyKeymap, indentWithTab} from '@codemirror/commands';
import {
  indentUnit, foldGutter, foldKeymap, codeFolding, bracketMatching, indentOnInput,
  syntaxHighlighting,
} from '@codemirror/language';
import {searchKeymap, highlightSelectionMatches, search} from '@codemirror/search';
import {
  autocompletion, completionKeymap, acceptCompletion, closeBrackets, closeBracketsKeymap,
} from '@codemirror/autocomplete';
import {linter, lintGutter, lintKeymap, diagnosticCount, openLintPanel} from '@codemirror/lint';
import {yaml} from '@codemirror/lang-yaml';
import {initDiff} from 'fireflyer/diff-view';
import {yamlStyle, scalarColours, editorTheme} from 'fireflyer/yaml-look';

function mount(ta) {
  // The latest answer from /yaml/assist: what completion can offer.
  let schema = {charts: {}, datasets: {}, calcs: {}, icons: {}};
  // Line wrapping, toggled with Alt+Z as in VS Code and remembered per browser
  // (a viewer's preference; storage may be off, so any failure means "off").
  const wrapping = new Compartment();
  const WRAP_KEY = 'ff-yaml-wrap';
  const wrapOn = () => { try { return localStorage.getItem(WRAP_KEY) === '1'; } catch (e) { return false; } };
  function toggleWrap(v) {
    const on = !wrapOn();
    try { localStorage.setItem(WRAP_KEY, on ? '1' : '0'); } catch (e) { /* not kept; still toggles */ }
    v.dispatch({effects: wrapping.reconfigure(on ? EditorView.lineWrapping : [])});
    return true;
  }
  // The textarea's own `value` accessor, which the setter below wraps; and
  // whether a write is being applied here, so it isn't echoed straight back.
  const nativeValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
  let applying = false;

  const view = new EditorView({
    parent: ta.parentElement,
    state: EditorState.create({doc: ta.value, extensions: [
      lineNumbers({domEventHandlers: {mousedown: linkFromGutter}}),
      linked,
      highlightActiveLineGutter(),
      highlightSpecialChars(),
      history(),
      foldGutter({markerDOM: open => foldMarker(open)}),
      codeFolding(),
      drawSelection(),
      dropCursor(),
      EditorState.allowMultipleSelections.of(true),
      indentOnInput(),
      bracketMatching(),
      closeBrackets(),
      rectangularSelection(),
      crosshairCursor(),
      highlightActiveLine(),
      highlightSelectionMatches(),
      search({top: true}),
      autocompletion({override: [complete], icons: false, activateOnTyping: true}),
      // YAML forbids tab characters: two spaces per level, always.
      indentUnit.of('  '),
      EditorState.tabSize.of(2),
      yaml(),
      syntaxHighlighting(yamlStyle),
      scalarColours,
      linter(problems, {delay: 450}),
      lintGutter(),
      showPanel.of(v => outline(v, () => schema)),
      showPanel.of(statusBar),
      wrapping.of(wrapOn() ? EditorView.lineWrapping : []),
      editorTheme,
      chromeTheme,
      fillPanel,
      keymap.of([
        // A run of the preview, as in the plain textarea.
        {key: 'Mod-Enter', run: () => { if (window.run) window.run(); return true; }},
        {key: 'Alt-z', run: toggleWrap},
        // Tab accepts an open completion; otherwise it indents.
        {key: 'Tab', run: acceptCompletion},
        ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap,
        ...foldKeymap, ...completionKeymap, ...lintKeymap, indentWithTab,
      ]),
      EditorView.updateListener.of(update => {
        // Edits move linked lines; the address bar follows them.
        if (update.startState.field(linked) !== update.state.field(linked)) writeLineParam(update.state);
        if (update.docChanged && !applying) {
          nativeValue.set.call(ta, update.state.doc.toString());
          ta.dispatchEvent(new Event('input', {bubbles: true}));
        }
      }),
    ]}),
  });

  // --- the textarea, kept in step (see the top of the file) ------------------
  Object.defineProperty(ta, 'value', {
    configurable: true,
    get() { return nativeValue.get.call(ta); },
    set(text) {
      nativeValue.set.call(ta, text);
      const current = view.state.doc.toString();
      if (text === current) return;
      // Only the span that differs, so the cursor and undo history around it
      // stay where they were.
      let start = 0;
      while (start < text.length && start < current.length && text[start] === current[start]) start++;
      let endOld = current.length, endNew = text.length;
      while (endOld > start && endNew > start && text[endNew - 1] === current[endOld - 1]) { endOld--; endNew--; }
      applying = true;
      view.dispatch({changes: {from: start, to: endOld, insert: text.slice(start, endNew)}});
      applying = false;
    },
  });
  ta.focus = () => view.focus();
  ta.cmView = view;                    // a handle for anything that needs the editor itself
  followLineLinks(view);
  ta.hidden = true;
  view.dom.classList.add('ff-yaml-editor');

  // --- problems: the server's own parse, pinned to where they are ------------
  async function problems(v) {
    const text = v.state.doc.toString();
    let data;
    try {
      const res = await fetch('/yaml/assist', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({yaml: text}),
      });
      if (!res.ok) return [];
      data = await res.json();
    } catch (e) { return []; }
    schema = data.schema || schema;
    const doc = v.state.doc;
    if (doc.toString() !== text) return [];     // typed on since: a newer check is coming
    return (data.problems || []).map(p => {
      const line = doc.line(Math.min(Math.max(p.line, 1), doc.lines));
      const from = line.from + Math.min(p.from, line.length);
      const to = Math.max(from, line.from + Math.min(p.to, line.length));
      return {from, to, severity: p.severity || 'error', message: p.message};
    });
  }

  // --- completion: what this spot in the dashboard can take ------------------
  function complete(ctx) {
    const line = ctx.state.doc.lineAt(ctx.pos);
    const before = line.text.slice(0, ctx.pos - line.from);
    if (/^\s*#/.test(before)) return null;                    // in a comment
    const where = locate(ctx.state.doc, line.number, before);

    // Layout: a chart id, inside the quotes of a row.
    if (where.section === 'layout') {
      const m = before.match(/["']([\w-]*)$/);
      if (!m) return null;
      return result(ctx.pos - m[1].length, chartIds(ctx.state.doc).map(id => option(id, 'chart')));
    }
    // A value: after `key:` (block or flow style), or inside its `[...]` list.
    const listed = before.match(/(?:^\s*(?:-\s+)?|[{,]\s*)([\w-]+):\s*\[[^\]]*?([\w.\/-]*)$/);
    const value = listed || before.match(/(?:^\s*(?:-\s+)?|[{,]\s*)([\w-]+):\s+([\w.\/-]*)$/);
    if (value) {
      const options = valuesFor(value[1], where, Boolean(listed));
      return options.length ? result(ctx.pos - value[2].length, options) : null;
    }
    // A key.
    const key = before.match(/(?:^\s*(?:-\s+)?|[{,]\s*)([\w-]*)$/);
    if (!key || (!key[1] && !ctx.explicit && !/[{,]\s*$/.test(before))) return null;
    const options = keysFor(where).map(k => ({...option(k.label, k.detail), apply: k.label + ': '}));
    return options.length ? result(ctx.pos - key[1].length, options) : null;
  }

  function keysFor(where) {
    const used = new Set(where.siblings);
    const fresh = list => list.filter(k => !used.has(k.label));
    if (where.section === null || where.depth === 0) {
      return fresh(TOP_KEYS);
    }
    if (where.inFilter) {
      return fresh([{label: 'column', detail: 'column'}, {label: 'op', detail: 'in · ni · between'}, {label: 'values', detail: 'list'}]);
    }
    if (where.section === 'charts' && where.chart) {
      const keys = schema.charts[where.chart.type];
      if (!keys) return fresh([{label: 'type', detail: 'chart type'}]);
      return fresh(Object.entries(keys).map(([k, spec]) => ({label: k, detail: spec.kind})));
    }
    if (where.section === 'calcs' && where.depth >= 3) {
      return fresh(CALC_KEYS);
    }
    if (where.section === 'datasets' && where.depth >= 2) {
      return fresh([{label: 'description', detail: 'text'}, {label: 'csv', detail: 'CSV block'}]);
    }
    return [];
  }

  function valuesFor(key, where, inList) {
    const dataset = where.chart ? where.chart.dataset : where.calcDataset;
    const columns = (schema.datasets[dataset] || []).map(c => option(c, 'column'));
    if (where.inFilter) {
      if (key === 'column') return columns;
      if (key === 'op') return [option('in', '='), option('ni', '≠'), option('between', '↔')];
      return [];
    }
    if (where.section === 'calcs' && key === 'agg') return AGGS.map(a => option(a, 'aggregate'));
    if (where.section === 'calcs' && key === 'formula') return columns;
    if (where.section !== 'charts' || !where.chart) return [];
    if (key === 'type') return Object.keys(schema.charts).map(c => option(c, 'chart type'));
    const spec = (schema.charts[where.chart.type] || {})[key];
    if (!spec) return [];
    switch (spec.kind) {
      case 'dataset': return Object.keys(schema.datasets).map(d => option(d, 'dataset'));
      case 'column': return columns;
      case 'calc': return (schema.calcs[dataset] || []).map(c => option(c, 'calc'));
      case 'choice': return (spec.choices || []).map(c => option(c, key));
      case 'bool': return [option('true', 'bool'), option('false', 'bool')];
      case 'list':
        if (!inList && key !== 'columns' && key !== 'measures') return [];
        return key === 'measures' ? (schema.calcs[dataset] || []).map(c => option(c, 'calc')) : columns;
      default: return [];
    }
  }

  return view;
}

// --- where the cursor is ----------------------------------------------------

const TOP_KEYS = [
  {label: 'name', detail: 'required'}, {label: 'description', detail: 'text'},
  {label: 'calcs', detail: 'by dataset'}, {label: 'charts', detail: 'id → chart'},
  {label: 'layout', detail: 'rows / tabs'}, {label: 'datasets', detail: 'inline CSV'},
];
const CALC_KEYS = [
  {label: 'agg', detail: 'aggregate'}, {label: 'formula', detail: 'expression'},
  {label: 'name', detail: 'label'}, {label: 'description', detail: 'text'},
  {label: 'format', detail: 'number format'}, {label: 'filters', detail: 'list'},
];
const AGGS = ['count', 'sum', 'dcount', 'min', 'max', 'avg'];

const indentOf = text => text.length - text.trimStart().length;
const keyOf = text => { const m = text.match(/^\s*(?:-\s+)?([\w-]+)\s*:/); return m ? m[1] : null; };
const blank = text => !text.trim() || /^\s*#/.test(text);

// The dashboard structure around `lineNo`, read from indentation (and from the
// line itself for one-line `{...}` style): which top-level section, how deep,
// the chart (id, type, dataset) or calc dataset it's in, whether it's inside a
// chart's `filters`, and the keys already written beside the cursor.
function locate(doc, lineNo, before) {
  const lines = [];
  for (let n = 1; n <= doc.lines; n++) lines.push(doc.line(n).text);
  const here = lineNo - 1;
  const myIndent = blank(lines[here]) ? before.length : indentOf(lines[here]);
  const where = {section: null, depth: 0, chart: null, calcDataset: null, inFilter: false, siblings: []};

  // Ancestors: the nearest line above at each smaller indentation.
  const ancestors = [];
  let limit = myIndent;
  for (let n = here - 1; n >= 0 && limit > 0; n--) {
    if (blank(lines[n])) continue;
    const ind = indentOf(lines[n]);
    if (ind < limit) { ancestors.unshift({n, ind, key: keyOf(lines[n])}); limit = ind; }
  }
  if (myIndent === 0 && !/[{,]\s*[\w-]*$/.test(before)) {
    where.siblings = topKeys(lines);
    return where;
  }
  where.section = ancestors.length ? ancestors[0].key : null;
  where.depth = ancestors.length;
  // On one line, in `{...}`: the line's own key is a level too.
  const inFlow = /\{[^}]*$/.test(before);
  if (inFlow) {
    where.depth += 1;
    if (!where.section) where.section = keyOf(lines[here]);
  }
  where.inFilter = ancestors.some(a => a.key === 'filters') || /filters:\s*\[[^\]]*\{[^}]*$/.test(before);

  if (where.section === 'charts') {
    const chartLine = ancestors[1] ? ancestors[1].n : (inFlow ? here : null);
    if (chartLine !== null) where.chart = chartAt(lines, chartLine);
  }
  if (where.section === 'calcs') {
    const dsLine = ancestors[1] ? ancestors[1].n : (inFlow ? here : null);
    if (dsLine !== null) where.calcDataset = keyOf(lines[dsLine]);
  }
  // Keys already written at this level: on this line (flow), or in the block.
  if (inFlow) {
    where.siblings = [...before.matchAll(/[{,]\s*([\w-]+)\s*:/g)].map(m => m[1]);
  } else if (ancestors.length) {
    const parent = ancestors[ancestors.length - 1];
    for (let n = parent.n + 1; n < lines.length; n++) {
      if (blank(lines[n])) continue;
      const ind = indentOf(lines[n]);
      if (ind <= parent.ind) break;
      if (ind === myIndent && n !== here) { const k = keyOf(lines[n]); if (k) where.siblings.push(k); }
    }
  }
  return where;
}

// A chart's id, type and dataset, from its block (or its one line).
function chartAt(lines, n) {
  const ind = indentOf(lines[n]);
  let text = lines[n];
  for (let m = n + 1; m < lines.length; m++) {
    if (!blank(lines[m]) && indentOf(lines[m]) <= ind) break;
    text += '\n' + lines[m];
  }
  const grab = re => { const m = text.match(re); return m ? m[1] : null; };
  return {
    id: keyOf(lines[n]),
    type: grab(/\btype:\s*["']?([\w-]+)/),
    dataset: grab(/\bdataset:\s*["']?([\w.\/-]+)/),
  };
}

function topKeys(lines) {
  return lines.filter(l => /^[\w-]+\s*:/.test(l)).map(keyOf);
}

function chartIds(doc) {
  const lines = [];
  for (let n = 1; n <= doc.lines; n++) lines.push(doc.line(n).text);
  const start = lines.findIndex(l => /^charts\s*:/.test(l));
  if (start < 0) return [];
  let childIndent = null;
  const ids = [];
  for (let n = start + 1; n < lines.length; n++) {
    if (blank(lines[n])) continue;
    const ind = indentOf(lines[n]);
    if (ind === 0) break;
    if (childIndent === null) childIndent = ind;
    if (ind === childIndent) { const k = keyOf(lines[n]); if (k) ids.push(k); }
  }
  return ids;
}

function option(label, detail) { return {label, detail, type: 'property'}; }
function result(from, options) { return {from, options, validFor: /^[\w.\/-]*$/}; }

// --- look --------------------------------------------------------------------

function foldMarker(open) {
  const el = document.createElement('span');
  el.className = 'cm-ff-fold';
  el.textContent = open ? '⌄' : '›';
  el.title = open ? 'Fold' : 'Unfold';
  return el;
}

// The bottom line: where the cursor is, what's selected, the file's language
// and indentation, and whether the dashboard is fine — a click on problems
// opens the list of them.
function statusBar(view) {
  const dom = document.createElement('div');
  dom.className = 'cm-ff-status';
  const pos = document.createElement('span');
  const meta = document.createElement('span');
  meta.className = 'cm-ff-status-meta';
  meta.textContent = 'YAML · 2 spaces';
  meta.title = 'Alt+Z wraps lines · Cmd/Ctrl+F finds · Cmd/Ctrl+/ comments · Cmd/Ctrl+Enter refreshes';
  const health = document.createElement('button');
  health.type = 'button';
  health.className = 'cm-ff-status-health';
  health.addEventListener('click', () => openLintPanel(view));
  dom.append(pos, meta, health);
  function update(state) {
    const sel = state.selection;
    const head = sel.main.head;
    const line = state.doc.lineAt(head);
    const chosen = sel.ranges.reduce((n, r) => n + (r.to - r.from), 0);
    pos.textContent = `Ln ${line.number}, Col ${head - line.from + 1}`
      + (sel.ranges.length > 1 ? ` · ${sel.ranges.length} cursors` : '')
      + (chosen ? ` · ${chosen} selected` : '');
    const problems = diagnosticCount(state);
    health.textContent = problems ? `● ${problems} problem${problems === 1 ? '' : 's'}` : '✓ Valid';
    health.classList.toggle('has-problems', problems > 0);
    health.title = problems ? 'Show problems (F8 for the next)' : 'The dashboard parses';
  }
  update(view.state);
  return {dom, bottom: true, update: u => update(u.state)};
}

// --- outline: a chip per top-level key, to hop there --------------------------
// `charts` is a menu of every chart, each with its type's icon; `calcs`, of
// every calc as `dataset / calc`; `layout`, when it's split into tabs, a menu
// of its tabs; `datasets`, of the inline datasets. A chart's `title` and a
// calc's `name`, when they have one, are the items' tooltips.
// The chip of the section the cursor is in is lit, so it doubles as a "you are
// here". Rebuilt only when the keys or charts change, not on every keystroke.

const MENUS = {charts: 'chart', calcs: 'calc', layout: 'tab', datasets: 'dataset'};
// A tab's or an inline dataset's name is free text, so may be quoted or have
// spaces: `"Sales & ops":`.
const tabOf = text => {
  const m = text.match(/^\s*(?:"([^"]+)"|'([^']+)'|([^\s#'"\-[{][^:#]*?))\s*:(?:\s|$)/);
  return m ? (m[1] || m[2] || m[3]) : null;
};
// `key: value` on a line — its own line, or inside a flow mapping
// (`{type: pie, title: By status}`); quotes dropped, and a trailing `# comment`
// (a `#` after whitespace) is no part of it.
const valueOf = (text, key) => {
  const m = text.match(new RegExp(`(?:^|[\\s{,])${key}\\s*:\\s*("([^"]*)"|'([^']*)'|[^,}]+)`));
  if (!m) return '';
  return (m[2] ?? m[3] ?? m[1].replace(/(^|\s)#.*$/, '')).trim();
};

// Each top-level key with its line, and for the keys with a menu its items —
// `{id, line, type, label, group}`: a chart (its `type`, its `title` as the
// label), a calc (`group` its dataset, its `name` as the label), a tab, an
// inline dataset.
function sections(doc) {
  const out = [];
  let items = null, kind = null, childIndent = null, calcIndent = null, group = '', current = null;
  for (let n = 1; n <= doc.lines; n++) {
    const text = doc.line(n).text;
    if (blank(text)) continue;
    const ind = indentOf(text);
    if (ind === 0) {
      const key = keyOf(text);
      if (!key) continue;
      kind = MENUS[key] || null;
      out.push({key, line: n, items: kind ? [] : null, kind});
      items = kind ? out[out.length - 1].items : null;
      childIndent = calcIndent = current = null;
      continue;
    }
    if (!items) continue;
    if (childIndent === null) childIndent = ind;
    if (kind === 'tab' || kind === 'dataset') {
      if (ind !== childIndent) continue;
      const name = tabOf(text);
      if (name) items.push({id: name, line: n, type: '', label: ''});
    } else if (kind === 'calc') {
      // Two levels: a dataset, then its calcs.
      if (ind === childIndent) {
        group = keyOf(text) || '';
        calcIndent = current = null;
      } else if (calcIndent === null || ind === calcIndent) {
        calcIndent = ind;
        const id = keyOf(text);
        current = id ? {id, group, line: n, type: '', label: valueOf(text, 'name')} : null;
        if (current) items.push(current);
      } else if (current && !current.label && ind > calcIndent && /^\s*name\s*:/.test(text)) {
        current.label = valueOf(text, 'name');
      }
    } else if (ind === childIndent) {
      const id = keyOf(text);
      current = id ? {id, line: n, type: valueOf(text, 'type'), label: valueOf(text, 'title')} : null;
      if (current) items.push(current);
    } else if (current) {
      if (!current.type && /^\s*type\s*:/.test(text)) current.type = valueOf(text, 'type');
      if (!current.label && /^\s*title\s*:/.test(text)) current.label = valueOf(text, 'title');
    }
  }
  return out;
}

// An outline hop links the key's whole block: its line down to the last line
// that belongs to it — deeper-indented, or a `- ` item at its own indent (YAML
// allows a list flush under its key). Trailing blank and comment lines are left
// out: a comment above the next key is that key's.
function jumpTo(view, line) {
  linkLines(view, line, blockEnd(view.state.doc, line), 'start');
}

function blockEnd(doc, line) {
  const own = indentOf(doc.line(line).text);
  let last = line;
  for (let n = line + 1; n <= doc.lines; n++) {
    const text = doc.line(n).text;
    if (blank(text)) continue;
    const ind = indentOf(text);
    if (ind < own || (ind === own && !/^\s*-(\s|$)/.test(text))) break;
    last = n;
  }
  return last;
}

// --- linked lines: `?line=12` / `?line=12-20` -------------------------------
const LINE_PARAM = 'line';
const setLinked = StateEffect.define();

// The linked span as document positions — {from, to}, both at line starts — so
// edits above it carry it along. Null when nothing is linked.
const linked = StateField.define({
  create: () => null,
  update(span, tr) {
    for (const e of tr.effects) if (e.is(setLinked)) return e.value;
    if (!span || !tr.docChanged) return span;
    const from = tr.changes.mapPos(span.from, 1), to = tr.changes.mapPos(span.to, -1);
    const doc = tr.state.doc;
    return {from: doc.lineAt(from).from, to: doc.lineAt(Math.max(from, to)).from};
  },
  provide: field => [
    EditorView.decorations.from(field, span => (view => lineMarks(view.state.doc, span, Decoration.line({class: 'cm-ff-linked'})))),
    gutterLineClass.compute([field], state => lineMarks(state.doc, state.field(field), linkedGutter)),
  ],
});
const linkedGutter = new class extends GutterMarker { elementClass = 'cm-ff-linked-gutter'; }();

function lineMarks(doc, span, mark) {
  if (!span) return RangeSet.empty;
  const marks = [];
  for (let n = doc.lineAt(span.from).number, last = doc.lineAt(span.to).number; n <= last; n++) {
    marks.push(mark.range(doc.line(n).from));
  }
  return RangeSet.of(marks);
}

// Link lines first..last (1-based): highlight them, put the cursor at the
// start — not a selection, which the next keystroke would type over — scroll
// there (`start` for an outline hop, `center` for a link) and write the address
// bar.
function linkLines(view, first, last, y = 'center') {
  const doc = view.state.doc;
  const clamp = n => Math.min(Math.max(1, n), doc.lines);
  [first, last] = [clamp(Math.min(first, last)), clamp(Math.max(first, last))];
  const from = doc.line(first).from;
  view.dispatch({
    selection: {anchor: from},
    effects: [setLinked.of({from, to: doc.line(last).from}), EditorView.scrollIntoView(from, {y, yMargin: 8})],
  });
  view.focus();
}

// A click on a line number links that line; a shift-click stretches the link
// from the line already linked, as on GitHub.
function linkFromGutter(view, block, event) {
  if (event.button !== 0) return false;
  const doc = view.state.doc;
  const line = doc.lineAt(block.from).number;
  const span = view.state.field(linked);
  const anchor = event.shiftKey && span ? doc.lineAt(span.from).number : line;
  const other = event.shiftKey && span && line < anchor ? doc.lineAt(span.to).number : anchor;
  event.preventDefault();
  linkLines(view, Math.min(line, other), Math.max(line, other));
  return true;
}

function writeLineParam(state) {
  const span = state.field(linked);
  let value = null;
  if (span) {
    const [a, b] = [state.doc.lineAt(span.from).number, state.doc.lineAt(span.to).number];
    value = a === b ? `${a}` : `${a}-${b}`;
  }
  setUrlParam(LINE_PARAM, value);
}

function setUrlParam(name, value) {
  const url = new URL(location.href);
  if (value) url.searchParams.set(name, value);
  else url.searchParams.delete(name);
  // `window.` — `history` alone is CodeMirror's undo history, imported above.
  if (url.href !== location.href) window.history.replaceState(window.history.state, '', url);
}

// A link with `?line=` opens the code panel on those lines; leaving the panel
// drops the parameter (and the highlight), as the compare view does its own.
function followLineLinks(view) {
  const editor = window.ffEditor;
  const m = /^(\d+)(?:-(\d+))?$/.exec(new URLSearchParams(location.search).get(LINE_PARAM) || '');
  if (!editor) return;
  editor.onModeChange(mode => {
    if (mode !== 'code' && view.state.field(linked)) view.dispatch({effects: setLinked.of(null)});
  });
  if (!m) return;
  editor.setMode('code');
  // After the panel is shown, so the editor has a size to scroll in.
  requestAnimationFrame(() => linkLines(view, +m[1], +(m[2] || m[1])));
}

function outline(view, getSchema) {
  const dom = document.createElement('div');
  dom.className = 'cm-ff-outline';
  let found = [], signature = '', menu = null;

  function button(className, text, title, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = className;
    b.textContent = text;
    b.title = title;
    b.addEventListener('click', onClick);
    return b;
  }

  // A chip jumps to its key. One with a list (`charts`, a tabbed `layout`) is
  // split: the name still jumps to the key, the ▾ beside it opens the list.
  // A flat layout (no tabs) has nothing to pick from: a plain chip.
  function chip(section) {
    const go = () => { closeMenu(); jumpTo(view, section.line); };
    const goTitle = `Go to ${section.key}: (line ${section.line})`;
    if (!(section.items && section.items.length)) {
      const b = button('cm-ff-chip', section.key, goTitle, go);
      b.dataset.key = section.key;
      return b;
    }
    const n = section.items.length;
    const wrap = document.createElement('span');
    wrap.className = 'cm-ff-chip has-menu';
    wrap.dataset.key = section.key;
    const more = button('cm-ff-chip-more', '▾', `${n} ${section.kind}${n === 1 ? '' : 's'} — pick one to jump to it`,
      () => (menu ? closeMenu() : openMenu(wrap, section)));
    more.setAttribute('aria-haspopup', 'menu');
    wrap.append(button('cm-ff-chip-go', section.key, goTitle, go), more);
    return wrap;
  }

  function openMenu(anchor, section) {
    menu = document.createElement('div');
    menu.className = 'cm-ff-menu';
    const icons = getSchema().icons || {};
    function item(entry) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'cm-ff-menu-item';
      if (section.kind === 'chart') {
        const icon = document.createElement('span');
        icon.className = 'icon';
        // The chart type's own icon.svg, from the server's schema — trusted
        // markup, never the YAML's text.
        icon.innerHTML = icons[entry.type] || '';
        b.append(icon);
      }
      if (entry.group) {                         // a calc: its dataset first
        const group = document.createElement('span');
        group.className = 'group';
        group.textContent = `${entry.group} /`;
        b.append(group);
      }
      const name = document.createElement('span');
      name.className = 'id';
      name.textContent = entry.id;
      b.append(name);
      // What a reader sees: a chart's title, a calc's name (and a chart's type).
      b.title = [entry.label, entry.type && `(${entry.type})`].filter(Boolean).join(' ') || entry.id;
      b.addEventListener('click', () => { closeMenu(); jumpTo(view, entry.line); });
      return b;
    }
    menu.append(...section.items.map(item));
    // Arrows move through the list, Enter (the button's own) jumps, Esc closes.
    menu.addEventListener('keydown', e => {
      const items = [...menu.querySelectorAll('.cm-ff-menu-item')];
      const i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); (items[i + 1] || items[i]).focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); (items[i - 1] || items[i]).focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(); view.focus(); }
    });
    dom.append(menu);
    menu.style.left = `${anchor.offsetLeft}px`;
    anchor.classList.add('is-open');
    anchor.querySelector('.cm-ff-chip-more').setAttribute('aria-expanded', 'true');
    menu.firstChild.focus();
    setTimeout(() => document.addEventListener('mousedown', outside), 0);
  }
  function outside(e) { if (menu && !dom.contains(e.target)) closeMenu(); }
  function closeMenu() {
    if (!menu) return;
    menu.remove();
    menu = null;
    document.removeEventListener('mousedown', outside);
    for (const b of dom.querySelectorAll('[aria-expanded]')) b.removeAttribute('aria-expanded');
    for (const c of dom.querySelectorAll('.is-open')) c.classList.remove('is-open');
  }

  function rebuild(state) {
    found = sections(state.doc);
    const next = JSON.stringify(found);
    if (next === signature) return;
    signature = next;
    closeMenu();
    dom.replaceChildren(...found.map(chip));
    dom.hidden = !found.length;
  }
  // Light the chip of the section holding the cursor.
  function locate(state) {
    const line = state.doc.lineAt(state.selection.main.head).number;
    let here = null;
    for (const s of found) if (s.line <= line) here = s.key;
    for (const b of dom.querySelectorAll('.cm-ff-chip')) b.classList.toggle('is-here', b.dataset.key === here);
  }
  rebuild(view.state);
  locate(view.state);
  return {
    dom, top: true,
    update(u) {
      if (u.docChanged) rebuild(u.state);
      if (u.docChanged || u.selectionSet) locate(u.state);
    },
  };
}

// The editor fills the YAML panel; the compare view's panes size to their text.
const fillPanel = EditorView.theme({'&': {height: '100%'}});

// The editor's own chrome: the outline chips, the status bar, linked lines.
const chromeTheme = EditorView.theme({
  '.cm-ff-linked': {backgroundColor: 'color-mix(in srgb, var(--yaml-number) 16%, transparent)'},
  '.cm-ff-linked-gutter': {color: 'var(--text)', opacity: '1', fontWeight: '600'},
  '.cm-ff-outline': {position: 'relative', display: 'flex', flexWrap: 'wrap', gap: '4px', padding: '6px 10px', fontSize: '12px'},
  '.cm-ff-chip': {border: '1px solid var(--border)', borderRadius: '999px', background: 'none', color: 'var(--muted)', padding: '1px 9px', font: 'inherit', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', cursor: 'pointer', lineHeight: '1.6'},
  '.cm-ff-chip:hover': {color: 'var(--text)', borderColor: 'var(--muted)'},
  '.cm-ff-chip.is-here': {color: 'var(--accent)', borderColor: 'var(--accent)'},
  '.cm-ff-chip.has-menu': {display: 'inline-flex', padding: '0', cursor: 'default', overflow: 'hidden'},
  '.cm-ff-chip-go, .cm-ff-chip-more': {border: 'none', background: 'none', color: 'inherit', font: 'inherit', cursor: 'pointer', lineHeight: '1.6'},
  '.cm-ff-chip-go': {padding: '1px 6px 1px 9px'},
  '.cm-ff-chip-more': {padding: '1px 7px 1px 5px', fontSize: '10px', borderLeft: '1px solid var(--border)'},
  '.cm-ff-chip-go:hover, .cm-ff-chip-more:hover, .cm-ff-chip.is-open .cm-ff-chip-more':
    {color: 'var(--text)', backgroundColor: 'color-mix(in srgb, var(--text) 8%, transparent)'},
  '.cm-ff-chip.is-here .cm-ff-chip-more': {borderLeftColor: 'color-mix(in srgb, var(--accent) 50%, transparent)'},
  '.cm-ff-menu': {position: 'absolute', top: '100%', zIndex: '20', marginTop: '2px', minWidth: '180px', maxWidth: 'calc(100% - 20px)', maxHeight: '20em', overflowY: 'auto', backgroundColor: 'var(--panel)', border: '1px solid var(--border)', borderRadius: '6px', boxShadow: '0 8px 24px rgba(0,0,0,.18)', padding: '4px'},
  '.cm-ff-menu-item': {display: 'flex', width: '100%', gap: '8px', alignItems: 'center', border: 'none', background: 'none', color: 'var(--text)', padding: '3px 8px', borderRadius: '4px', font: 'inherit', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '12.5px', textAlign: 'left', cursor: 'pointer'},
  '.cm-ff-menu-item .id': {overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'},
  '.cm-ff-menu-item .group': {color: 'var(--muted)', flex: '0 0 auto'},
  '.cm-ff-menu-item .icon': {display: 'inline-flex', flex: '0 0 auto', width: '14px', height: '14px', color: 'var(--muted)'},
  '.cm-ff-menu-item .icon svg': {width: '14px', height: '14px'},
  '.cm-ff-menu-item:hover, .cm-ff-menu-item:focus': {outline: 'none', backgroundColor: 'color-mix(in srgb, var(--accent) 18%, transparent)'},
  '.cm-ff-status': {display: 'flex', alignItems: 'center', gap: '14px', padding: '4px 12px', fontSize: '11.5px', color: 'var(--muted)', fontVariantNumeric: 'tabular-nums'},
  '.cm-ff-status-meta': {marginLeft: 'auto'},
  '.cm-ff-status-health': {border: 'none', background: 'none', padding: '0', font: 'inherit', color: 'var(--muted)', cursor: 'pointer'},
  '.cm-ff-status-health.has-problems': {color: 'var(--error)'},
});


// Mounted last: everything above — the theme, the highlighting, the helpers —
// is a `const`, and doesn't exist until its line has run.
const textarea = document.getElementById('code');
if (textarea) mount(textarea);
// The compare view reads with the editor's look: YAML colours, scalars, theme.
initDiff([yaml(), syntaxHighlighting(yamlStyle), scalarColours, editorTheme]);
