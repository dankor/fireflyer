// Runs the editor script (web/static/editor.js) under a hand-built DOM stub,
// so the Python suite can exercise behaviour it otherwise never touches.
//
// The editor's JS is a string inside app.py: pytest renders it, asserts on the
// text, and never executes a line. That is how chart resize shipped broken
// twice — once because a block deletion took `let resize` and the resize
// constants with it, leaving handlers referencing names that no longer existed.
// Reading the source could not catch it; running it does, immediately.
//
// Deliberately no jsdom and no npm (see CLAUDE.md: no frontend tooling). The
// stub covers only what the script touches at load and during a drag; when the
// script starts using something new, this fails loudly with `X is not defined`,
// which is the right prompt to extend it.
//
//   node tests/editor_harness.mjs <script.js> <scenario>
//
// Prints one JSON object on stdout. Scenarios are at the bottom.

import fs from 'node:fs';
import vm from 'node:vm';

const registry = new Map();          // id -> element, so lookups are stable
// Ids the rendered page actually contains. Anything else resolves to null, as
// in a real document: several handlers guard on an element being absent (the
// dashboard-name field exists only in portal/paths mode), and a stub that
// always returns an element would quietly skip those branches.
const presentIds = new Set(
  JSON.parse(fs.readFileSync(process.argv[4] || '/dev/null', 'utf8').trim() || '[]')
);
const windowListeners = {};
const fetches = [];

function makeEl(id = '') {
  const el = {
    id,
    hidden: false,
    value: '',
    textContent: '',
    innerHTML: '',
    disabled: false,
    dataset: {},
    children: [],
    listeners: {},
    style: {
      _props: {},
      gridTemplateRows: '',
      gridTemplateColumns: '',
      setProperty(k, v) { this._props[k] = v; },
      getPropertyValue(k) { return this._props[k] || ''; },
    },
    classList: {
      _set: new Set(),
      add(...c) { c.forEach((x) => this._set.add(x)); },
      remove(...c) { c.forEach((x) => this._set.delete(x)); },
      toggle(c, force) { (force === undefined ? !this._set.has(c) : force) ? this.add(c) : this.remove(c); },
      contains(c) { return this._set.has(c); },
      get value() { return [...this._set].join(' '); },
    },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    querySelectorAll: () => [],
    querySelector: () => null,
    closest: () => null,
    contains: () => false,
    focus() {},
    blur() {},
    remove() {},
    appendChild() {},
    setAttribute() {},
    removeAttribute() {},
    clientWidth: 1000,
    clientHeight: 600,
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 1000, bottom: 600, width: 1000, height: 600 }),
  };
  return el;
}

function byId(id) {
  if (!registry.has(id)) {
    if (presentIds.size && !presentIds.has(id) && !id.startsWith('#')) return null;
    registry.set(id, makeEl(id));
  }
  return registry.get(id);
}

const document = {
  getElementById: byId,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeEl(),
  addEventListener(type, fn) { (byId('#document').listeners[type] ||= []).push(fn); },
  body: byId('#body'),
  documentElement: byId('#html'),
};

const context = {
  document,
  window: {
    addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
    removeEventListener() {},
    innerWidth: 1440,
    innerHeight: 900,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    htmx: { process() {} },
  },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  async fetch(url, options) {
    fetches.push({ url, body: options && options.body });
    // Echo the YAML back the way every editor route does — a payload without
    // it makes callers assign `undefined` and fail far from the cause.
    const yaml = byId('code').value;
    return {
      ok: true,
      json: async () => ({ ok: true, html: '', yaml }),
      text: async () => '',
    };
  },
  FormData: class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } },
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  MutationObserver: class { observe() {} disconnect() {} },
  requestAnimationFrame: (fn) => fn(),
  setTimeout, clearTimeout, console,
  location: { href: '', search: '' },
  history: { replaceState() {} },
  navigator: {},
};
context.globalThis = context;

function fire(listeners, event) {
  for (const fn of listeners || []) fn(event);
}

function setMode(mode) {
  // Click the switch the way a user would, rather than poking internals.
  fire(byId('ff-modes').listeners.click, {
    target: { closest: (sel) => (sel === 'button' ? { dataset: { mode } } : null) },
  });
}

// --- scenarios ---------------------------------------------------------------

const scenarios = {
  // Loading at all is the check: strict mode turns an undeclared name into a
  // ReferenceError, and a half-deleted block into a SyntaxError.
  load: () => ({
    windowEvents: Object.keys(windowListeners),
    outputEvents: Object.keys(byId('output').listeners),
    mode: byId('layout').classList.value,
  }),

  // Drag a row's bottom edge down by 80px and commit. The height token in the
  // YAML should grow by 80 / HEIGHT_UNIT_PX = 10 units.
  'row-resize': () => {
    byId('code').value = 'name: T\nlayout:\n  - ["@30", "a"]\n  - ["@40", "b"]\n';
    setMode('edit');

    const row = makeEl('row');
    row.style.gridTemplateRows = '240px';
    const handle = makeEl('handle');
    handle.dataset.track = '1';
    handle.dataset.rowOrdinal = '0';          // the first row, "@30"
    handle.closest = (sel) =>
      sel === '.fireflyer-resize-handle' ? handle :
      sel === '.fireflyer-dashboard-row' ? row : null;

    fire(byId('output').listeners.mousedown, {
      target: handle, clientX: 0, clientY: 100, preventDefault() {},
    });
    fire(windowListeners.mousemove, { clientX: 0, clientY: 180 });
    fire(windowListeners.mouseup, {});
    return { yaml: byId('code').value, tracks: row.style.gridTemplateRows };
  },

  // A drag while not in edit mode must change nothing at all.
  'row-resize-in-view': () => {
    byId('code').value = 'name: T\nlayout:\n  - ["@30", "a"]\n';
    setMode('view');

    const row = makeEl('row');
    row.style.gridTemplateRows = '240px';
    const handle = makeEl('handle');
    handle.dataset.track = '1';
    handle.dataset.rowOrdinal = '0';
    handle.closest = (sel) =>
      sel === '.fireflyer-resize-handle' ? handle :
      sel === '.fireflyer-dashboard-row' ? row : null;

    fire(byId('output').listeners.mousedown, {
      target: handle, clientX: 0, clientY: 100, preventDefault() {},
    });
    fire(windowListeners.mousemove, { clientX: 0, clientY: 180 });
    fire(windowListeners.mouseup, {});
    return { yaml: byId('code').value };
  },

  // Drag a column boundary; on release the widths are POSTed to the server.
  'col-resize': () => {
    byId('code').value = 'name: T\nlayout:\n  - ["@30", "a:50", "b:50"]\n';
    setMode('edit');

    const row = makeEl('row');
    row.style.gridTemplateColumns = '50fr 50fr';
    row.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 300 });
    const handle = makeEl('handle');
    handle.dataset.leftCol = '0';
    handle.dataset.ordinals = '0';
    handle.closest = (sel) =>
      sel === '.fireflyer-resize-col-handle' ? handle :
      sel === '.fireflyer-dashboard-row' ? row : null;

    fire(byId('output').listeners.mousedown, {
      target: handle, clientX: 500, clientY: 0, preventDefault() {},
    });
    fire(windowListeners.mousemove, { clientX: 700, clientY: 0 });
    fire(windowListeners.mouseup, {});
    return { columns: row.style.gridTemplateColumns, fetches: fetches.map((f) => f.url) };
  },
};

const [scriptPath, scenario] = process.argv.slice(2);
try {
  vm.createContext(context);
  vm.runInContext('"use strict";\n' + fs.readFileSync(scriptPath, 'utf8'), context, {
    filename: 'editor.js',
  });
} catch (err) {
  console.log(JSON.stringify({ error: `${err.constructor.name}: ${err.message}` }));
  process.exit(0);
}

try {
  console.log(JSON.stringify({ ok: true, ...scenarios[scenario]() }));
} catch (err) {
  console.log(JSON.stringify({ error: `${err.constructor.name}: ${err.message}` }));
}
