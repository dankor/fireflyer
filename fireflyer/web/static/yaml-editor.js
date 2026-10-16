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

import {EditorState, Compartment} from '@codemirror/state';
import {
  EditorView, Decoration, MatchDecorator, ViewPlugin, keymap, lineNumbers,
  highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars, drawSelection,
  dropCursor, rectangularSelection, crosshairCursor, showPanel,
} from '@codemirror/view';
import {defaultKeymap, history, historyKeymap, indentWithTab} from '@codemirror/commands';
import {
  indentUnit, foldGutter, foldKeymap, codeFolding, bracketMatching, indentOnInput,
  syntaxHighlighting, HighlightStyle,
} from '@codemirror/language';
import {searchKeymap, highlightSelectionMatches, search} from '@codemirror/search';
import {
  autocompletion, completionKeymap, acceptCompletion, closeBrackets, closeBracketsKeymap,
} from '@codemirror/autocomplete';
import {linter, lintGutter, lintKeymap, diagnosticCount, openLintPanel} from '@codemirror/lint';
import {yaml} from '@codemirror/lang-yaml';
import {tags as t} from '@lezer/highlight';

function mount(ta) {
  // The latest answer from /yaml/assist: what completion can offer.
  let schema = {charts: {}, datasets: {}, calcs: {}};
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
      lineNumbers(),
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
      showPanel.of(statusBar),
      wrapping.of(wrapOn() ? EditorView.lineWrapping : []),
      editorTheme,
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

// The YAML grammar's tokens, coloured from editor.css's `--yaml-*` variables,
// so the editor follows the Auto / Light / Dark switch with the rest.
const yamlStyle = HighlightStyle.define([
  {tag: [t.definition(t.propertyName), t.propertyName], color: 'var(--yaml-key)'},
  {tag: [t.string, t.special(t.string)], color: 'var(--yaml-string)'},
  {tag: [t.number, t.integer, t.float], color: 'var(--yaml-number)'},
  {tag: [t.bool, t.null, t.atom, t.keyword], color: 'var(--yaml-keyword)'},
  {tag: [t.comment, t.lineComment, t.blockComment], color: 'var(--yaml-comment)', fontStyle: 'italic'},
  {tag: [t.punctuation, t.separator, t.bracket, t.squareBracket, t.brace], color: 'var(--yaml-punct)'},
  {tag: [t.labelName, t.typeName, t.meta, t.processingInstruction], color: 'var(--yaml-meta)'},
]);

// The YAML grammar reads `42`, `true` and `null` as plain text, like any other
// unquoted value. A value that is exactly one gets its own colour here.
const scalarMatcher = new MatchDecorator({
  regexp: /(?<=(?:^|[:\[{,-])\s*)(-?\d+(?:\.\d+)?|true|false|null|~)(?=\s*(?:[,\]}#]|$))/g,
  decoration: m => Decoration.mark({class: /\d/.test(m[1]) ? 'cm-yaml-number' : 'cm-yaml-keyword'}),
});
const scalarColours = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = scalarMatcher.createDeco(view); }
  update(u) { this.decorations = scalarMatcher.updateDeco(u, this.decorations); }
}, {decorations: v => v.decorations});

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

const editorTheme = EditorView.theme({
  '&': {height: '100%', color: 'var(--text)', backgroundColor: 'var(--panel)', fontSize: '13px'},
  '&.cm-focused': {outline: 'none'},
  '.cm-scroller': {fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', lineHeight: '1.6'},
  '.cm-content': {padding: '10px 0', caretColor: 'var(--text)'},
  '.cm-cursor, .cm-dropCursor': {borderLeftColor: 'var(--text)', borderLeftWidth: '2px'},
  '&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground':
    {backgroundColor: 'color-mix(in srgb, var(--accent) 24%, transparent)'},
  '.cm-activeLine': {backgroundColor: 'color-mix(in srgb, var(--text) 4%, transparent)'},
  '.cm-selectionMatch': {backgroundColor: 'color-mix(in srgb, var(--accent) 14%, transparent)'},
  '.cm-matchingBracket, &.cm-focused .cm-matchingBracket':
    {backgroundColor: 'color-mix(in srgb, var(--accent) 22%, transparent)', outline: '1px solid color-mix(in srgb, var(--accent) 50%, transparent)'},
  '.cm-gutters': {backgroundColor: 'var(--panel)', color: 'var(--muted)', border: 'none', borderRight: '1px solid var(--border)'},
  '.cm-lineNumbers .cm-gutterElement': {padding: '0 8px 0 14px', minWidth: '28px', opacity: '0.7'},
  '.cm-activeLineGutter': {backgroundColor: 'transparent', color: 'var(--text)'},
  '.cm-activeLineGutter.cm-gutterElement': {opacity: '1'},
  '.cm-ff-fold': {display: 'inline-block', width: '12px', cursor: 'pointer', opacity: '0.45', fontSize: '12px'},
  '.cm-ff-fold:hover': {opacity: '1'},
  '.cm-foldPlaceholder': {backgroundColor: 'color-mix(in srgb, var(--accent) 14%, transparent)', border: 'none', color: 'var(--text)', padding: '0 6px', borderRadius: '3px'},
  '.cm-tooltip': {backgroundColor: 'var(--panel)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: '6px', boxShadow: '0 8px 24px rgba(0,0,0,.18)', overflow: 'hidden'},
  '.cm-tooltip-autocomplete > ul': {fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '12.5px', maxHeight: '18em'},
  '.cm-tooltip-autocomplete > ul > li': {padding: '3px 10px !important', lineHeight: '1.5'},
  '.cm-tooltip-autocomplete > ul > li[aria-selected]': {backgroundColor: 'color-mix(in srgb, var(--accent) 22%, transparent)', color: 'var(--text)'},
  '.cm-completionDetail': {color: 'var(--muted)', fontStyle: 'normal', marginLeft: '1.2em', fontSize: '11px'},
  '.cm-completionMatchedText': {textDecoration: 'none', color: 'var(--accent)', fontWeight: '600'},
  '.cm-tooltip-lint': {padding: '0'},
  '.cm-diagnostic': {padding: '6px 10px', borderLeftWidth: '3px'},
  '.cm-diagnostic-error': {borderLeftColor: 'var(--error)'},
  '.cm-lintRange-error': {backgroundImage: 'none', textDecoration: 'wavy underline var(--error)', textUnderlineOffset: '3px'},
  '.cm-lint-marker': {width: '0.9em', height: '0.9em'},
  '.cm-panels': {backgroundColor: 'var(--panel)', color: 'var(--text)'},
  '.cm-panels-top': {borderBottom: '1px solid var(--border)'},
  '.cm-panels-bottom': {borderTop: '1px solid var(--border)'},
  '.cm-search': {padding: '8px 10px', fontSize: '12px', display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center'},
  '.cm-search input, .cm-search button, .cm-textfield, .cm-button':
    {fontSize: '12px', borderRadius: '4px', border: '1px solid var(--border)', backgroundColor: 'var(--bg)', color: 'var(--text)', backgroundImage: 'none', padding: '3px 8px', margin: '0'},
  '.cm-search label': {display: 'inline-flex', alignItems: 'center', gap: '4px', color: 'var(--muted)'},
  '.cm-search [name=close]': {border: 'none', background: 'none', fontSize: '16px', color: 'var(--muted)', padding: '0 4px'},
  '.cm-searchMatch': {backgroundColor: 'color-mix(in srgb, var(--yaml-number) 28%, transparent)', outline: '1px solid color-mix(in srgb, var(--yaml-number) 55%, transparent)'},
  '.cm-searchMatch-selected': {backgroundColor: 'color-mix(in srgb, var(--accent) 40%, transparent)'},
  '.cm-yaml-number': {color: 'var(--yaml-number)'},
  '.cm-yaml-keyword': {color: 'var(--yaml-keyword)'},
  '.cm-ff-status': {display: 'flex', alignItems: 'center', gap: '14px', padding: '4px 12px', fontSize: '11.5px', color: 'var(--muted)', fontVariantNumeric: 'tabular-nums'},
  '.cm-ff-status-meta': {marginLeft: 'auto'},
  '.cm-ff-status-health': {border: 'none', background: 'none', padding: '0', font: 'inherit', color: 'var(--muted)', cursor: 'pointer'},
  '.cm-ff-status-health.has-problems': {color: 'var(--error)'},
});

// Mounted last: everything above — the theme, the highlighting, the helpers —
// is a `const`, and doesn't exist until its line has run.
const textarea = document.getElementById('code');
if (textarea) mount(textarea);
