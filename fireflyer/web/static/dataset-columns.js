// A dataset's columns, in four modes — as the dashboard editor has its own —
// and its permissions:
//   view     the columns: each one's name (its type icon: what its formula
//            gives, from the built data), formula, example values and the
//            dashboards using it
//   edit     a form: each column a name and a formula over the uploaded
//            columns — rename, drop (✕) or add (＋) columns
//   code     the same as YAML, in a code editor
//   compare  two versions of the mapping side by side (or one against yours);
//            the left one can be restored — loaded as an unsaved edit, which
//            saving builds as the next version (history only grows)
//   access   the roles that may read the dataset — all of it (Full, the column
//            picker's first entry), or the rows their filters let through
//            (the chart builder's filter rows) —
//            from the server (GET …/access); a role not listed may not. Save
//            posts them with the mapping's
//
// The mapping's YAML is the one source of truth: the form writes it as you go,
// the code editor edits it directly, and leaving the code editor has the server
// read it back (POST …/mapping/check) so the form shows what it says. Save
// posts it as the next version, which the server builds in the background; this
// page shows "building", polls, and reloads on the new version — or shows why
// it failed, keeping your edit to fix.
//
// Editor chrome (a dev tool), loaded only on a dataset's page.

import {EditorState} from '@codemirror/state';
import {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection,
} from '@codemirror/view';
import {defaultKeymap, history, historyKeymap, indentWithTab} from '@codemirror/commands';
import {syntaxHighlighting, indentUnit, bracketMatching} from '@codemirror/language';
import {yaml} from '@codemirror/lang-yaml';
import {MergeView} from '@codemirror/merge';
import {yamlStyle, scalarColours, editorTheme} from 'fireflyer/yaml-look';
import {stepPair} from 'fireflyer/diff-view';

const data = JSON.parse(document.getElementById('ds-data').textContent);
const panel = document.getElementById('ds-columns');
const modes = document.getElementById('ds-modes');
const saveBtn = document.getElementById('ds-save');
const discardBtn = document.getElementById('ds-discard');
const statusEl = document.getElementById('ds-status');
const toastEl = document.getElementById('ds-toast');
const base = `/datasets/${encodeURIComponent(data.name)}`;
const look = [yaml(), syntaxHighlighting(yamlStyle), scalarColours, editorTheme];

let rules = data.rules.map(r => ({...r}));
let text = data.mapping;           // the YAML being edited
let codeStale = false;             // edited as YAML since the form last read it
let mode = 'view';
let teardown = () => {};
const dirty = () => text !== data.mapping;

// --- modes -----------------------------------------------------------------------

modes.addEventListener('click', e => {
  const b = e.target.closest('button[data-mode]');
  if (b && b.dataset.mode !== mode) setMode(b.dataset.mode);
});

async function setMode(next, opts = {}) {
  if (mode === 'code' && next !== 'code' && codeStale && !(await reread())) return;
  teardown();
  teardown = () => {};
  mode = next;
  for (const b of modes.querySelectorAll('button')) b.classList.toggle('active', b.dataset.mode === mode);
  panel.dataset.mode = mode;
  ({view: showView, edit: showEdit, code: showCode, compare: showCompare, access: showAccess})[mode](opts);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
  }
  node.append(...children.filter(c => c != null));
  return node;
}

// The type's icon, rendered by the server.
function serviceMark() {
  const mark = el('span', {class: 'col-service', title: 'Service column — in the dataset, never in dashboards'});
  mark.innerHTML = data.serviceIcon;
  return mark;
}

// A column's type icon and kind, from the built data (none yet for a column
// only in an unsaved edit).
function icon(name) {
  const span = document.createElement('span');
  span.innerHTML = (data.kinds[name] || [''])[0];
  return span.firstChild || '';
}

// --- view ------------------------------------------------------------------------

// Filters over the columns, in the header itself: the type (a funnel in the
// icon slot), a name search (a lens that opens a box in place of "Column") and
// the dashboards (the "Dashboards" heading). Type and dashboards are checkbox
// lists — several at once, none checked means no filter. Kept while you switch
// modes; only the rows redraw, so the search box keeps focus.
const viewFilter = {q: '', kinds: new Set(), dash: new Set()};
const SVG = (path, size = 14) =>
  `<svg viewBox="0 0 16 16" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.5" `
  + `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
const FUNNEL = '<path d="M2.5 3h11L9.3 8.3v4.2L6.7 13.8V8.3z"/>';
const LENS = '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/>';
// A funnel, crossed out: uncheck everything in a filter's list.
const RESET = '<path d="M2.5 3h11L9.3 8.3v4.2L6.7 13.8V8.3z"/><path d="M11 10.5l3.5 3.5M14.5 10.5 11 14"/>';
const NOT_USED = '-';

function showView() {
  const body = el('tbody');
  const draw = () => body.replaceChildren(...rules.filter(shown).map(r => el('tr', {},
    el('td', {}, icon(r.name), el('span', {class: 'col-name'}, r.name), r.service ? serviceMark() : null),
    el('td', {class: 'muted mono'}, r.formula),
    el('td', {class: 'ex'}, examples(r.name)),
    el('td', {}, usedIn(r.name)))));
  // The type is the icon before the name — what the formula gave, from the built data.
  const head = el('thead', {}, el('tr', {},
    el('th', {}, columnHeading(draw)), el('th', {}, 'Formula'), el('th', {}, 'Examples'),
    el('th', {}, dashboardHeading(draw))));
  panel.replaceChildren(el('table', {class: 'ds-cols'}, head, body));
  draw();
}

function shown(r) {
  const {q, kinds, dash} = viewFilter;
  if (q && !r.name.toLowerCase().includes(q.toLowerCase())) return false;
  if (kinds.size && !kinds.has((data.kinds[r.name] || [])[1])) return false;
  if (!dash.size) return true;
  const users = data.usedIn[r.name] || [];
  return (dash.has(NOT_USED) && !users.length) || users.some(([id]) => dash.has(id));
}

// A checkbox list under `anchor`: `options` are {value, label, icon?}; ticking
// one updates `selected` and calls `onChange`. Its first row resets the list —
// shown only while something's checked. Closes on a click outside or Esc.
let openMenu = null;
function checkMenu(anchor, options, selected, onChange) {
  if (openMenu) { const was = openMenu.anchor; closeMenu(); if (was === anchor) return; }
  const boxes = [];
  const reset = el('button', {type: 'button', class: 'ds-check-reset', hidden: !selected.size,
    onclick: () => { selected.clear(); boxes.forEach(b => { b.checked = false; }); changed(); }},
    el('span', {class: 'ds-check-icon'}), el('span', {}, 'Reset'));
  reset.firstChild.innerHTML = SVG(RESET);
  const changed = () => { reset.hidden = !selected.size; onChange(); };
  const menu = el('div', {class: 'ds-check-menu', role: 'menu'}, reset, ...options.map(o => {
    const box = el('input', {type: 'checkbox', checked: selected.has(o.value),
      onchange: e => { e.target.checked ? selected.add(o.value) : selected.delete(o.value); changed(); }});
    boxes.push(box);
    const mark = el('span', {class: 'ds-check-icon'});
    mark.innerHTML = o.icon || '';
    return el('label', {class: 'ds-check-item'}, box, o.icon ? mark : null, el('span', {}, o.label));
  }));
  anchor.closest('th').append(menu);
  openMenu = {menu, anchor};
  setTimeout(() => document.addEventListener('mousedown', outside), 0);
  document.addEventListener("keydown", onMenuKey, true);
}
function outside(e) { if (openMenu && !openMenu.menu.contains(e.target) && !openMenu.anchor.contains(e.target)) closeMenu(); }
function onMenuKey(e) { if (e.key === 'Escape' && openMenu) { e.preventDefault(); e.stopPropagation(); closeMenu(); } }
function closeMenu() {
  if (!openMenu) return;
  openMenu.menu.remove();
  openMenu = null;
  document.removeEventListener('mousedown', outside);
  document.removeEventListener("keydown", onMenuKey, true);
}

// A filter button's look: highlighted with a count while it filters.
function paintFilter(button, count, label) {
  button.classList.toggle('active', count > 0);
  button.dataset.count = count || '';
  button.title = count ? `${label}: ${count} checked` : `Filter by ${label.toLowerCase()}`;
  button.setAttribute('aria-label', button.title);
}

// [type] Column [search] — the funnel opens the types there are as a checkbox
// list; the lens opens the search.
function columnHeading(draw) {
  const kinds = [...new Map(Object.values(data.kinds).map(([html, kind]) => [kind, html]))];
  const type = el('button', {type: 'button', class: 'ds-th-btn ds-th-count'});
  type.innerHTML = SVG(FUNNEL);
  type.addEventListener('click', () => checkMenu(type, kinds.map(([kind, html]) => ({value: kind, label: kind, icon: html})),
    viewFilter.kinds, () => { paintFilter(type, viewFilter.kinds.size, 'Type'); draw(); }));
  paintFilter(type, viewFilter.kinds.size, 'Type');

  const label = el('span', {}, 'Column');
  const search = el('input', {
    type: 'search', class: 'ds-th-search', value: viewFilter.q, placeholder: 'Search columns',
    'aria-label': 'Search columns', spellcheck: 'false',
    oninput: e => { viewFilter.q = e.target.value.trim(); lens.classList.toggle('active', !!viewFilter.q); draw(); },
    onkeydown: e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); search.value = ''; search.dispatchEvent(new Event('input')); close(); } },
    onblur: () => { if (!viewFilter.q) close(); },
  });
  const lens = el('button', {type: 'button', class: 'ds-th-btn' + (viewFilter.q ? ' active' : ''),
    title: 'Search columns', 'aria-label': 'Search columns', onclick: () => open()});
  lens.innerHTML = SVG(LENS);
  const open = () => { label.hidden = lens.hidden = true; search.hidden = false; search.focus(); };
  const close = () => { label.hidden = lens.hidden = false; search.hidden = true; };
  if (viewFilter.q) { label.hidden = lens.hidden = true; } else { search.hidden = true; }
  return el('span', {class: 'ds-th'}, type, label, lens, search);
}

// "Dashboards" opens not used + each dashboard as a checkbox list.
function dashboardHeading(draw) {
  const dashboards = new Map(Object.values(data.usedIn).flat().map(([id, name]) => [id, name]));
  const button = el('button', {type: 'button', class: 'ds-th-btn ds-th-label ds-th-count'});
  button.innerHTML = SVG(FUNNEL, 12) + '<span>Dashboards</span>';
  const options = [{value: NOT_USED, label: 'Not used'}, ...[...dashboards].map(([id, name]) => ({value: id, label: name}))];
  button.addEventListener('click', () => checkMenu(button, options, viewFilter.dash,
    () => { paintFilter(button, viewFilter.dash.size, 'Dashboards'); draw(); }));
  paintFilter(button, viewFilter.dash.size, 'Dashboards');
  return el('span', {class: 'ds-th'}, button);
}

// A column's first distinct values as uploaded, each in a label — what makes
// its type obvious at a glance.
function examples(source) {
  return el('span', {class: 'col-examples'},
    ...(data.examples[source] || []).map(v => el('span', {class: 'col-example', style: `--n:${v.length}`,
      title: v, 'data-value': v}, v)));
}

// A click on an example shows its whole value — the label may cut it short —
// in a small popover as plain text, to select as you would any text, with a
// copy button beside it (a check for a moment once copied; where the clipboard
// is refused — not https/localhost — the text is selected instead, ready for
// Cmd/Ctrl+C). A click elsewhere or Esc closes it; a click inside it doesn't.
const COPY = '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3.5A1.5 1.5 0 0 0 9 2H3.5A1.5 1.5 0 0 0 2 3.5V9a1.5 1.5 0 0 0 1.5 1.5h2"/>';
const COPIED = '<path d="M3 8.5 6.5 12 13 4.5"/>';
let valuePop = null;
panel.addEventListener('click', e => {
  const label = e.target.closest('.col-example');
  if (!label) return;
  closeValue();
  const rect = label.getBoundingClientRect();
  const value = label.dataset.value ?? label.textContent;
  const text = el('span', {class: 'ds-value-text'}, value);
  const copy = el('button', {type: 'button', class: 'ds-value-copy', title: 'Copy', 'aria-label': 'Copy',
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(value);
        copy.innerHTML = SVG(COPIED);
        setTimeout(() => { copy.innerHTML = SVG(COPY); }, 1200);
      } catch (err) {
        const range = document.createRange();
        range.selectNodeContents(text);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
      }
    }});
  copy.innerHTML = SVG(COPY);
  valuePop = el('div', {class: 'ds-value-pop', role: 'tooltip'}, text, copy);
  document.body.append(valuePop);
  valuePop.style.left = `${Math.min(rect.left + scrollX, scrollX + innerWidth - valuePop.offsetWidth - 12)}px`;
  valuePop.style.top = `${rect.bottom + scrollY + 6}px`;
  e.stopPropagation();
});
document.addEventListener('mousedown', e => {
  if (valuePop && !valuePop.contains(e.target) && !e.target.closest('.col-example')) closeValue();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && valuePop) closeValue(); });
function closeValue() {
  if (valuePop) { valuePop.remove(); valuePop = null; }
}

// The dashboards whose charts read the column, as links (the server works it
// out from what each chart declares it reads).
function usedIn(column) {
  const links = (data.usedIn[column] || []).map(([id, name]) => el('a', {class: 'ref', href: `/d/${id}`, title: name}, name));
  return links.length ? el('span', {class: 'refs'}, ...links) : el('span', {class: 'muted'}, '—');
}

function table(heads, rows) {
  return el('table', {class: 'ds-cols'},
    el('thead', {}, el('tr', {}, ...heads.map(h => el('th', {}, h)))),
    el('tbody', {}, ...rows));
}

// --- edit ------------------------------------------------------------------------

// Each column a name and a formula (the calc grammar over the uploaded columns:
// `status`, `to_number(qty)`, `str2dt(day, YYYY-MM-DD)`); ✕ drops one, ＋ adds
// one. The type isn't chosen — it's what the formula gives.
function showEdit() {
  const rows = rules.map((r, i) => {
    const name = el('input', {
      value: r.name, 'aria-label': 'Column name', spellcheck: 'false',
      oninput: e => { r.name = e.target.value.trim(); changed(); },
    });
    const formula = el('input', {
      class: 'mono', value: r.formula, 'aria-label': `${r.name} formula`, spellcheck: 'false',
      placeholder: 'e.g. to_number(qty)',
      oninput: e => { r.formula = e.target.value.trim(); changed(); },
    });
    const drop = el('button', {
      type: 'button', class: 'col-drop', title: 'Drop this column', 'aria-label': `Drop ${r.name}`,
      onclick: () => { rules.splice(i, 1); changed(); showEdit(); },
    }, '✕');
    // A service column stays in the dataset but never reaches dashboards.
    const service = el('button', {
      type: 'button', class: 'col-service-toggle' + (r.service ? ' active' : ''),
      title: r.service ? 'Service column — click to allow it in dashboards' : 'Make it a service column (not for dashboards)',
      'aria-pressed': r.service ? 'true' : 'false', 'aria-label': `Service column ${r.name}`,
      onclick: () => { r.service = !r.service; changed(); showEdit(); },
    });
    service.innerHTML = data.serviceIcon;
    // The icon's slot is kept even before a column has a type (new, renamed).
    return el('tr', {}, el('td', {}, el('span', {class: 'col-icon'}, icon(r.name)), name),
      el('td', {}, formula), el('td', {class: 'col-actions'}, service, drop));
  });
  const add = el('button', {
    type: 'button', class: 'col-add', title: 'Add a column', 'aria-label': 'Add a column',
    onclick: () => {
      rules.push({name: '', formula: ''});
      showEdit();
      const inputs = panel.querySelectorAll('input[aria-label="Column name"]');
      inputs[inputs.length - 1].focus();
    },
  }, '＋');
  panel.replaceChildren(table(['Name', 'Formula', ''], rows), el('div', {class: 'col-add-row'}, add));
}

function changed() {
  text = dump(rules);
  updateSave();
}

// The YAML the server writes (column_map.dump): each column's formula on a
// line of its own. A column still without a name is left out until it has one.
function dump(list) {
  const lines = list.filter(r => r.name)
    .map(r => `  ${scalar(r.name)}:\n    formula: ${scalar(r.formula || r.name)}` + (r.service ? '\n    service: true' : ''));
  return 'columns:\n' + lines.join('\n') + '\n' + permissionsBlock(text);
}

// The mapping's `permissions:` block as written — the form above edits only the
// columns, so a column edit keeps the permissions as they are.
function permissionsBlock(yaml) {
  const lines = yaml.split('\n');
  const start = lines.findIndex(l => /^permissions:/.test(l));
  if (start < 0) return '';
  let end = start + 1;
  while (end < lines.length && (lines[end] === '' || /^[\s#]/.test(lines[end]))) end++;
  return lines.slice(start, end).join('\n').replace(/\n*$/, '\n');
}

function scalar(s) {
  // Letters of any script stay as they are — `Підрозділ` needs no quotes.
  const plain = /^[\p{L}_][\p{L}\p{N}_ .\/()*+-]*$/u.test(s) && !/\s$/.test(s)
    && !/^(true|false|yes|no|null|on|off|y|n)$/i.test(s);
  return plain ? s : JSON.stringify(s);   // a JSON string is a YAML string
}

// --- code ------------------------------------------------------------------------

function showCode() {
  const host = el('div', {class: 'ds-code'});
  panel.replaceChildren(host);
  const view = new EditorView({
    parent: host,
    state: EditorState.create({doc: text, extensions: [
      lineNumbers(), highlightActiveLineGutter(), highlightActiveLine(), history(), drawSelection(),
      bracketMatching(), indentUnit.of('  '), EditorState.tabSize.of(2), ...look,
      keymap.of([{key: 'Mod-s', run: () => { save(); return true; }},
        ...defaultKeymap, ...historyKeymap, indentWithTab]),
      EditorView.updateListener.of(u => {
        if (!u.docChanged) return;
        text = u.state.doc.toString();
        codeStale = true;
        updateSave();
      }),
    ]}),
  });
  view.focus();
  teardown = () => view.destroy();
}

// The server reads the YAML back into rules — the form shows what it says.
async function reread() {
  const res = await post('/mapping/check', text);
  if (!res.ok) { toast(res.error); return false; }
  rules = res.rules;
  codeStale = false;
  return true;
}

// --- compare ---------------------------------------------------------------------

const texts = new Map();

// The mapping's revisions — one per minor (an upload that kept the mapping is
// the same revision) — newest first, each standing for its newest version.
function revisions() {
  const seen = new Set();
  return data.versions.filter(v => !seen.has(minorOf(v.version)) && seen.add(minorOf(v.version)));
}
const minorOf = label => String(label).split('.')[1];
// Any version -> the version standing for its revision in the pickers.
const revisionOf = label => (revisions().find(v => minorOf(v.version) === minorOf(label)) || {}).version;

function showCompare({left} = {}) {
  const options = revisions().map(v => ({value: v.version, label: describe(v)}));
  if (dirty()) options.unshift({value: 'now', label: 'Now · unsaved'});
  const pickA = el('select', {class: 'ff-diff-pick', 'aria-label': 'Left side'});
  const pickB = el('select', {class: 'ff-diff-pick', 'aria-label': 'Right side'});
  for (const pick of [pickA, pickB]) pick.append(...options.map(o => el('option', {value: o.value}, o.label)));
  const stats = el('span', {class: 'ff-diff-stats'});
  const swap = el('button', {type: 'button', class: 'ff-diff-icon', title: 'Swap sides', 'aria-label': 'Swap sides',
    onclick: () => { [pickA.value, pickB.value] = [pickB.value, pickA.value]; render(); }}, '⇄');
  const restore = el('button', {type: 'button', class: 'ff-diff-restore', hidden: true,
    onclick: () => restoreVersion(pickA.value)});
  // Step both sides through the revisions: one older, one newer. Oldest first,
  // "now" last — the order the steps walk.
  const chronology = () => [...pickA.options].map(o => o.value).reverse();
  const stepper = (delta, title, path) => {
    const b = el('button', {type: 'button', class: 'ff-diff-icon', title, 'aria-label': title, hidden: true,
      onclick: () => {
        const pair = stepPair(chronology(), pickA.value, pickB.value, delta);
        if (pair) { [pickA.value, pickB.value] = pair; render(); }
      }});
    b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${path}"/></svg>`;
    return b;
  };
  const older = stepper(-1, 'One revision older (both sides)', 'M6 9l6 6 6-6');
  const newer = stepper(+1, 'One revision newer (both sides)', 'M6 15l6-6 6 6');
  const body = el('div', {class: 'ff-diff-body'});
  panel.replaceChildren(el('div', {class: 'ff-diff-bar'}, pickA, swap, pickB, older, newer, stats, restore), body);

  // Yours against what's in use; or, nothing unsaved, the last change made.
  const current = revisionOf(data.version || (data.versions[0] || {}).version);
  const before = revisions().find(v => v.version !== current && v.status === 'ready');
  const right = dirty() ? 'now' : current;
  left = left && revisionOf(left);
  pickA.value = left || (dirty() ? current : (before || {}).version || current);
  pickB.value = left && left === right ? (before || {}).version || right : right;
  pickA.onchange = pickB.onchange = render;

  let merge = null;
  async function render() {
    const [a, b] = await Promise.all([textOf(pickA.value), textOf(pickB.value)]);
    if (merge) merge.destroy();
    const side = doc => ({doc, extensions: [lineNumbers(), ...look, EditorState.readOnly.of(true), EditorView.editable.of(false)]});
    merge = new MergeView({a: side(a), b: side(b), parent: body, gutter: true, highlightChanges: true});
    let added = 0, removed = 0;
    const lines = (doc, from, to) => (to > from ? doc.lineAt(Math.max(from, to - 1)).number - doc.lineAt(from).number + 1 : 0);
    for (const c of merge.chunks) {
      removed += lines(merge.a.state.doc, c.fromA, c.toA);
      added += lines(merge.b.state.doc, c.fromB, c.toB);
    }
    stats.textContent = added || removed ? `+${added} −${removed}` : 'No differences';
    stats.classList.toggle('is-same', !added && !removed);
    older.hidden = !stepPair(chronology(), pickA.value, pickB.value, -1);
    newer.hidden = !stepPair(chronology(), pickA.value, pickB.value, +1);
    // An older version can be brought back — never "now", nor what you have.
    const back = pickA.value !== 'now' ? await restoredOf(pickA.value) : null;
    restore.hidden = back === null || back === text;
    restore.replaceChildren(el('span', {class: 'ff-diff-restore-icon'}), `#${minorOf(pickA.value)}`);
    restore.title = `Restore #${minorOf(pickA.value)} — unsaved until you save, which builds it as the next version`;
    restore.setAttribute('aria-label', `Restore #${minorOf(pickA.value)}`);
  }
  render();
  teardown = () => { if (merge) merge.destroy(); };
}

// A version's mapping fitted to the latest upload — what restoring it loads.
const fitted = new Map();
async function restoredOf(label) {
  if (!fitted.has(label)) {
    const res = await fetch(`${base}/versions/${encodeURIComponent(label)}/restore`);
    fitted.set(label, res.ok ? (await res.json()).mapping : null);
  }
  return fitted.get(label);
}

// Load it as an unsaved edit, then show what saving it would change.
async function restoreVersion(label) {
  const mapping = await restoredOf(label);
  if (mapping === null) return;
  text = mapping;
  if (!(await reread())) return;
  updateSave();
  toast(`#${minorOf(label)} restored · unsaved`);
  setMode('compare');
}

async function textOf(value) {
  if (value === 'now') return text;
  if (!texts.has(value)) {
    const res = await fetch(`${base}/versions/${encodeURIComponent(value)}`);
    texts.set(value, res.ok ? (await res.json()).mapping : '');
  }
  return texts.get(value);
}

// `v3.4 · 2026-10-16 14:05:31 · dana` — local time; no author in local mode.
// `#3 · 2026-10-16 14:05:31 · dana · in use` — a mapping revision (the minor),
// local time; no author in local mode.
function describe(v) {
  const inUse = minorOf(v.version) === minorOf(data.version);
  return [`#${minorOf(v.version)}`, stamp(v.created_at * 1000), v.author, inUse ? 'in use' : '']
    .filter(Boolean).join(' · ');
}

function stamp(when) {
  const d = new Date(when);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + ` ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// The history menu: a version opens the comparison with the one in use.
document.getElementById('ds-history').addEventListener('click', e => {
  const b = e.target.closest('.ds-ver');
  if (!b) return;
  e.currentTarget.open = false;
  setMode('compare', {left: b.dataset.version});
});

// --- permissions -----------------------------------------------------------------
// They're part of the mapping (`permissions:`), so versioned with it: the form
// is built by the server from the YAML being edited, and each change is written
// back into that YAML (POST …/access/yaml) — Save, Discard, compare and code
// mode all see it as one more edit of the mapping. The form is kept across
// modes and rebuilt only when the YAML changed elsewhere.

let accessForm = null;
let accessFor = null;          // the YAML the form was built from, or last wrote

// The form's roles and filters — not the draft rows, which hold a filter still
// being built.
function accessData() {
  const fd = new FormData();
  for (const el of accessForm.elements) {
    if (!el.name || el.closest('.ds-draft, template')) continue;
    if ((el.type === 'checkbox' || el.type === 'radio') && !el.checked) continue;
    fd.append(el.name, el.value);
  }
  return fd;
}

async function showAccess() {
  if (!accessForm || accessFor !== text) {
    const res = await fetch(`${base}/access/form`, {method: 'POST', body: new URLSearchParams({mapping: text})});
    const box = document.createElement('div');
    box.innerHTML = await res.text();
    if (mode !== 'access') return;           // switched away while it loaded
    const form = box.querySelector('form');
    if (!form) { accessForm = null; panel.replaceChildren(...box.childNodes); return; }  // no roles, or bad YAML
    accessForm = form;
    accessFor = text;
    wireAccess(form);
  }
  panel.replaceChildren(accessForm);
  if (window.htmx) window.htmx.process(accessForm);
}

// The form, into the YAML's `permissions:`. Ordered: each waits for the last,
// so the YAML never goes back to an older state.
let accessWrite = Promise.resolve();
function syncAccess() {
  accessWrite = accessWrite.then(async () => {
    const fd = accessData();
    fd.append('mapping', text);
    const res = await fetch(`${base}/access/yaml`, {method: 'POST', body: fd});
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { toast(body.error || 'Could not apply'); return; }
    text = accessFor = body.mapping;
    updateSave();
  });
}

// The filter rows are the chart builder's (`FilterListParam`), run by the same
// script, filter-builder.js: here it only needs telling that a row's data is
// this dataset's, with no dashboard behind it.
if (window.ffFilterBuilder) {
  window.ffFilterBuilder.context = () => ({yaml_text: '', filter_dataset: data.name});
}

function wireAccess(form) {
  const roleTpl = form.querySelector('template.ds-access-row-tpl');
  const draftTpl = form.querySelector('template.ds-draft-tpl');
  const fullTpl = form.querySelector('template.ds-full-tpl');
  const menu = form.querySelector('.ds-access-add');
  const tbody = form.querySelector('.ds-access-table > tbody');

  // Full — all rows, no filter — is the draft's column picker's first entry: a
  // level, not a column, so it's added here, drawn apart from the columns.
  // Watched, since filter-builder.js re-renders the fields on its own.
  const addFull = scope => {
    for (const list of scope.querySelectorAll('.ds-draft .ff-colpick-list')) {
      if (!list.querySelector('.ds-full')) list.prepend(fullTpl.content.cloneNode(true));
    }
  };
  new MutationObserver(() => addFull(form)).observe(form, {childList: true, subtree: true});

  // A role with no filters is full: it shows Full where its filters would be.
  function paint(role) {
    const full = !role.querySelector('tr.ds-filter');
    role.classList.toggle('is-full', full);
    role.classList.toggle('is-filter', !full);
    recount(role);
  }
  // The Rows column: what the role's filters let through, counted again
  // whenever they change (only if they did — paint runs often).
  async function recount(role) {
    const fd = new FormData();
    for (const el of role.querySelectorAll('.ds-filter-table input[name]')) fd.append(el.name, el.value);
    const key = new URLSearchParams(fd).toString();
    const cell = role.querySelector('.ds-access-rows');
    if (cell.dataset.key === key) return;
    cell.dataset.key = key;
    const res = await fetch(`${base}/access/rows`, {method: 'POST', body: fd});
    const body = await res.json().catch(() => ({}));
    if (cell.dataset.key !== key) return;      // changed again meanwhile
    cell.textContent = body.rows == null ? '—' : body.rows.toLocaleString('en-US');
  }
  // The draft row, blank, open (the role's +) or closed.
  function resetDraft(role, open = false) {
    const editing = role.querySelector('tr.ds-filter.is-editing');
    if (editing) editing.classList.remove('is-editing');
    const draft = role.querySelector('.ds-draft');
    draft.replaceChildren(draftTpl.content.cloneNode(true));
    draft.classList.toggle('is-open', open);
    addFull(role);
    paint(role);
  }

  // ✓: the draft, read by the server into a filter, as a compact row — in
  // place of the one being edited, or added after the others.
  async function addFilter(role) {
    const draft = role.querySelector('.ds-draft');
    const fd = new FormData();
    for (const el of draft.querySelectorAll('[name]')) {
      if (el.type === 'checkbox' && !el.checked) continue;
      fd.append(el.name, el.value);
    }
    const res = await fetch(`${base}/access/filter`, {method: 'POST', body: fd});
    const html = await res.text();
    if (!res.ok) {
      const box = document.createElement('div');
      box.innerHTML = html;
      toast(box.textContent.trim() || 'Not a filter yet');
      return;
    }
    const holder = document.createElement('tbody');
    holder.innerHTML = html;
    const row = holder.firstElementChild;
    const editing = role.querySelector('tr.ds-filter.is-editing');
    if (editing) editing.replaceWith(row);
    else role.querySelector('.ds-filter-table tbody').appendChild(row);
    resetDraft(role);                          // closed again
    syncAccess();
  }

  // ✎: the filter back in the draft's fields, to change; ✓ puts it back.
  async function editFilter(role, row) {
    resetDraft(role, true);
    row.classList.add('is-editing');
    const fd = new FormData();
    fd.append('yaml_text', '');
    fd.append('filter_dataset', data.name);
    for (const el of row.querySelectorAll('input[name]')) fd.append(el.name, el.value);
    const res = await fetch('/filter/fields', {method: 'POST', body: fd});
    if (!res.ok) return;
    role.querySelector('.ds-draft .ff-filter-fields').outerHTML = await res.text();
    if (window.htmx) window.htmx.process(role);
  }

  // Only roles with access are listed: the + menu offers the others.
  const syncMenu = () => {
    const listed = new Set([...tbody.querySelectorAll('tr[data-role]')].map(r => r.dataset.role));
    const offers = [...menu.querySelectorAll('button[data-role]')];
    for (const b of offers) b.hidden = listed.has(b.dataset.role);
  };
  // A new role, made here: created on the Access list's route, then given
  // access to this dataset like any other offered role.
  const newRole = menu.querySelector('.ds-access-new input');
  newRole.addEventListener('keydown', async e => {
    if (e.key === 'Escape') { newRole.value = ''; menu.open = false; return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const name = newRole.value.trim();
    if (!name) return;
    const res = await fetch('/access/new', {method: 'POST', body: new URLSearchParams({name})});
    if (!res.ok) {
      const box = document.createElement('div');
      box.innerHTML = await res.text();
      toast(box.textContent.trim() || 'Could not create the role');
      return;
    }
    const offer = document.createElement('button');
    offer.type = 'button';
    offer.dataset.role = name;
    offer.textContent = name;
    menu.querySelector('.ff-pathdd-menu').insertBefore(offer, newRole.closest('label'));
    newRole.value = '';
    giveAccess(offer);
    syncAccess();
  });
  function giveAccess(b) {
    const role = roleTpl.content.firstElementChild.cloneNode(true);
    role.dataset.role = b.dataset.role;
    role.querySelector('input[name=role]').value = b.dataset.role;
    role.querySelector('.name').textContent = b.dataset.role;
    tbody.appendChild(role);
    resetDraft(role);
    if (window.htmx) window.htmx.process(role);
    menu.open = false;
    syncMenu();
  }

  // Runs before filter-builder.js (this form, then the document), which does
  // the draft's fields; this does the rest.
  form.addEventListener('click', e => {
    const role = e.target.closest('.ds-access-row');
    const full = e.target.closest('.ds-full');
    if (full) {                                // all of it: drop every filter
      full.closest('.ff-colpick').open = false;
      for (const row of role.querySelectorAll('tr.ds-filter')) row.remove();
      resetDraft(role);
    }
    if (e.target.closest('.ds-tool-add, .ds-full-chip')) {
      resetDraft(role, true);
      // Straight to the columns — once this click is done, or the builder's
      // close-on-click-elsewhere would shut the list again at once.
      const pick = role.querySelector('.ds-draft .ff-colpick');
      setTimeout(() => { pick.open = true; });
    }
    if (e.target.closest('.ds-filter-ok')) addFilter(role);
    if (e.target.closest('.ds-filter-cancel')) resetDraft(role);
    const edit = e.target.closest('.ds-filter-edit');
    if (edit) editFilter(role, edit.closest('tr.ds-filter'));
    const remove = e.target.closest('.ds-filter-remove');
    if (remove) {
      const row = remove.closest('tr.ds-filter');
      if (row.classList.contains('is-editing')) resetDraft(role);
      row.remove();
      paint(role);
    }
    const offer = e.target.closest('.ds-access-add button[data-role]');
    if (offer) giveAccess(offer);
    const revoke = e.target.closest('.ds-tool-del');
    if (revoke) { role.remove(); syncMenu(); }
    if (full || remove || offer || revoke) syncAccess();
  });
  form.addEventListener('submit', e => e.preventDefault());

  addFull(form);
  for (const role of tbody.querySelectorAll('.ds-access-row')) {
    // Counted by the server already: remember for what, so paint won't ask.
    const fd = new FormData();
    for (const el of role.querySelectorAll('.ds-filter-table input[name]')) fd.append(el.name, el.value);
    role.querySelector('.ds-access-rows').dataset.key = new URLSearchParams(fd).toString();
    paint(role);
  }
}

// The + menu closes on a click anywhere else, as a native menu would.
document.addEventListener('click', e => {
  const open = document.querySelector('.ds-access-add[open]');
  if (open && !open.contains(e.target)) open.open = false;
});


// --- save, and the build it starts -----------------------------------------------

function updateSave() {
  saveBtn.hidden = discardBtn.hidden = !dirty();
}

// Discard: back to the mapping in use, in whatever mode you're in.
discardBtn.addEventListener('click', () => {
  if (!confirm('Discard your unsaved changes?')) return;
  text = data.mapping;
  rules = data.rules.map(r => ({...r}));
  codeStale = false;
  accessForm = null;                         // re-read from the server
  updateSave();
  setMode(mode);
});

async function save() {
  if (!dirty()) return;
  if (mode === 'code' && codeStale && !(await reread())) return;
  saveBtn.disabled = true;
  const res = await post('/mapping', text);
  saveBtn.disabled = false;
  if (!res.ok) { toast(res.error); return; }
  showStatus('building', res.building);
  watch();
}

// Poll until the version is built: reload on it, or say why it failed.
async function watch() {
  const res = await fetch(`${base}/status`);
  const s = res.ok ? await res.json() : {};
  if (s.building) { setTimeout(watch, 1000); return; }
  if (s.error && s.version === data.version) {
    showStatus('error', s.error);
    toast(s.error);
    return;
  }
  data.mapping = text;                       // saved: leaving is safe now
  location.reload();
}

function showStatus(kind, message) {
  statusEl.hidden = false;
  statusEl.className = `ds-status is-${kind}`;
  statusEl.title = kind === 'building' ? `Building ${message}` : '';
  statusEl.replaceChildren(...(kind === 'building' ? [el('span', {class: 'ds-spin'})] : []), message);
}

async function post(path, mapping) {
  try {
    const res = await fetch(base + path, {method: 'POST', body: new URLSearchParams({mapping})});
    const body = await res.json();
    return res.ok ? {ok: true, ...body} : {ok: false, error: body.error || 'Could not save'};
  } catch (e) {
    return {ok: false, error: String(e)};
  }
}

let toastTimer = 0;
function toast(message) {
  toastEl.textContent = message;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 6000);
}

saveBtn.addEventListener('click', save);
window.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
});
window.addEventListener('beforeunload', e => { if (dirty()) { e.preventDefault(); e.returnValue = ''; } });
if (data.building) watch();
showView();                      // the server drew the table; this adds its filters
