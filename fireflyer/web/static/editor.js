const codeEl = document.getElementById('code');
const outEl = document.getElementById('output');
const outPane = document.getElementById('output-pane');
const refreshBtn = document.getElementById('refresh');
const toastEl = document.getElementById('ff-toast');

// Transient error toast — replaces the old topbar status line for the rare
// config-edit failure messages. Auto-hides after a few seconds.
let toastTimer = null;
function flash(msg) {
  toastEl.textContent = msg;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function() { toastEl.hidden = true; }, 3000);
}

// Theme switch — a 3-segment Auto / Light / Dark control. Sets `data-ff-theme`
// on <html>, which themes the editor chrome, the dashboard preview, and every
// chart inside it (their CSS keys off this attribute on any ancestor). "auto"
// leaves it off so the OS preference wins. The choice persists across reloads.
const themeSwitch = document.getElementById('theme-switch');
const THEME_MODES = ['auto', 'light', 'dark'];
let themeMode = localStorage.getItem('ffTheme') || 'auto';
if (THEME_MODES.indexOf(themeMode) < 0) themeMode = 'auto';
function applyTheme() {
  if (themeMode === 'auto') delete document.documentElement.dataset.ffTheme;
  else document.documentElement.dataset.ffTheme = themeMode;
  themeSwitch.querySelectorAll('button').forEach(function(b) {
    b.classList.toggle('active', b.dataset.mode === themeMode);
  });
  localStorage.setItem('ffTheme', themeMode);
}
themeSwitch.querySelectorAll('button').forEach(function(b) {
  b.addEventListener('click', function() { themeMode = b.dataset.mode; applyTheme(); });
});
applyTheme();

// Which dashboard tab is showing. Threaded to /execute so an edit re-renders
// the same tab, and re-synced from the rendered hidden input after every swap
// (the server clamps it, and a dissolve drops back to a flat, tab-less render).
let activeTab = 0;
function syncActiveTab() {
  const inp = outEl.querySelector('#fireflyer-dashboard input[name="active_tab"]');
  activeTab = inp ? (parseInt(inp.value, 10) || 0) : 0;
}

async function run() {
  outPane.classList.remove('stale');  // re-rendering now: clear the stale overlay
  refreshBtn.disabled = true;
  try {
    // The filters in play live in the address bar (`f`, written by the
    // /dashboard route as you filter), so every render — first load, a shared
    // link, a YAML edit — starts from them instead of dropping them.
    const state = new URLSearchParams(location.search).get('f') || '';
    const res = await fetch('/execute?active_tab=' + activeTab + '&f=' + encodeURIComponent(state), {
      method: 'POST',
      headers: {'Content-Type': 'application/yaml'},
      body: codeEl.value,
    });
    const data = await res.json();
    outEl.innerHTML = data.html;
    // htmx doesn't auto-wire nodes inserted via innerHTML; wire them now.
    if (window.htmx) window.htmx.process(outEl);
    syncActiveTab();
  } catch (e) {
    outEl.innerHTML = '<pre class="error">' + e + '</pre>';
  } finally {
    refreshBtn.disabled = false;
    // Every path that rewrites the YAML — a chart modal, the calcs manager,
    // chat — sets `codeEl.value` and calls run(). Setting `.value` fires no
    // `input` event, so this is where the topbar has to catch up.
    updateSaveState();
    syncNameFromYaml();
  }
}

// --- unsaved-changes (Save) + editable title ------------------------------
// Two independent "dirty" notions: the preview is *stale* vs the last render
// (drives Refresh), and the YAML is *unsaved* vs what's stored (drives Save).
const saveBtn = document.getElementById('ff-save');       // portal only
const nameEl = document.getElementById('ff-dash-name');   // editable title
let savedYaml = codeEl.value;

function markStale() { outPane.classList.add('stale'); }
function updateSaveState() { if (saveBtn) saveBtn.hidden = (codeEl.value === savedYaml); }

// Title <-> YAML `name:` key (two-way). Read/rewrite the top-level line.
function yamlName(text) {
  const m = text.match(/^name:[ \t]*(.*)$/m);
  if (!m) return '';
  let v = m[1].trim();
  if (v.startsWith('"')) { try { return JSON.parse(v); } catch (e) { return v.slice(1, -1); } }
  if (v.startsWith("'") && v.endsWith("'")) return v.slice(1, -1).replace(/''/g, "'");
  return v;
}
function setYamlName(text, name) { return text.replace(/^name:.*$/m, 'name: ' + JSON.stringify(name)); }
function syncNameFromYaml() {
  if (nameEl && document.activeElement !== nameEl) nameEl.textContent = yamlName(codeEl.value);
}

// A manual YAML edit greys out the (now stale) preview and reveals the refresh
// button; it may also change the name or dirty state. Programmatic edits (chat,
// config-edit) call run() directly, which refreshes Save on its own.
codeEl.addEventListener('input', function() { markStale(); updateSaveState(); syncNameFromYaml(); });

async function doSave() {
  if (!saveBtn || saveBtn.hidden) return;   // nothing to save
  const label = saveBtn.textContent;
  saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
  try {
    const res = await fetch(saveBtn.dataset.saveUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({yaml_text: codeEl.value}),
    });
    const data = await res.json();
    if (data.ok) { savedYaml = codeEl.value; saveBtn.textContent = 'Saved \u2713'; }
    else { saveBtn.textContent = 'Save failed'; console.warn('Save failed:', data.error || ''); }
  } catch (e) { saveBtn.textContent = 'Save failed'; }
  finally {
    saveBtn.disabled = false;
    setTimeout(function() { saveBtn.textContent = label; updateSaveState(); }, 1200);
  }
}

if (saveBtn) {
  saveBtn.addEventListener('click', doSave);
  window.addEventListener('keydown', function(e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) { e.preventDefault(); doSave(); }
  });
  // Guard against losing unsaved edits by navigating away (logo / ☰ / reload).
  window.addEventListener('beforeunload', function(e) {
    if (!saveBtn.hidden) { e.preventDefault(); e.returnValue = ''; }
  });
}

if (nameEl) {
  nameEl.setAttribute('contenteditable', 'plaintext-only');
  nameEl.setAttribute('spellcheck', 'false');
  nameEl.setAttribute('title', 'Click to rename');
  nameEl.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); nameEl.blur(); }
    else if (e.key === 'Escape') { e.preventDefault(); nameEl.textContent = yamlName(codeEl.value); nameEl.blur(); }
  });
  nameEl.addEventListener('blur', function() {
    const nm = nameEl.textContent.trim();
    if (!nm || nm === yamlName(codeEl.value)) { nameEl.textContent = yamlName(codeEl.value); return; }
    codeEl.value = setYamlName(codeEl.value, nm);   // rewrite the YAML `name:` key
    nameEl.textContent = nm;
    updateSaveState();   // renaming is an unsaved change (but not a preview change)
  });
}

// A crossfilter click or deployed tab button swaps #fireflyer-dashboard via
// htmx (not through run()); keep the JS tab state in sync afterwards. During a
// cross-tab chart move the destination tab's cells load one by one (each an
// htmx swap) and only carry data-cid once loaded, so rebuild the drop zones as
// they arrive.
outEl.addEventListener('htmx:afterSwap', () => {
  syncActiveTab();
  const dash = dashboardEl();
  if (moveCid !== null && dash && dash.classList.contains('ff-move-mode')) {
    const overlay = dash.querySelector('.ff-move-overlay');
    if (overlay) overlay.remove();
    buildMoveZones();
  }
});

// Render the default example immediately so the page isn't empty.
window.addEventListener('DOMContentLoaded', run);
refreshBtn.addEventListener('click', run);
codeEl.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); run(); }
});

// Each panel's ✕ returns to the dashboard; the mode switch opens them. See
// `setMode`, which owns every show/hide.

// Calcs manager overlay: lists a dashboard's calcs per dataset and
// add/edit/deletes them. Server-rendered (like docs); each mutation swaps the
// editor YAML and re-runs, then reloads the list.
const calcsBody = document.getElementById('ff-calcs-body');

async function loadCalcs() {
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  const res = await fetch('/calcs/manager', { method: 'POST', body: fd });
  calcsBody.innerHTML = await res.text();
}

async function openCalcForm(dataset, key) {
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('dataset', dataset);
  fd.append('key', key || '');
  const res = await fetch('/calcs/form', { method: 'POST', body: fd });
  calcsBody.innerHTML = await res.text();
}

// Overlay clicks: add / edit / delete, plus the filter builder rows.
calcsBody.addEventListener('click', async e => {
  const add = e.target.closest('.ff-calcs-add');
  if (add) { openCalcForm(add.dataset.dataset, ''); return; }
  const edit = e.target.closest('.ff-calcs-edit');
  if (edit) { openCalcForm(edit.dataset.dataset, edit.dataset.key); return; }
  const del = e.target.closest('.ff-calcs-del');
  if (del) {
    const fd = new FormData();
    fd.append('yaml_text', codeEl.value);
    fd.append('dataset', del.dataset.dataset);
    fd.append('key', del.dataset.key);
    const res = await fetch('/calcs/delete', { method: 'POST', body: fd });
    const data = await res.json();
    if (!data.ok) { flash(data.error || 'Could not delete.'); return; }
    codeEl.value = data.yaml; run(); loadCalcs();
    return;
  }
  if (e.target.closest('.ff-calcs-cancel')) { loadCalcs(); return; }
  const fadd = e.target.closest('.ff-filter-add');
  if (fadd) {
    const wrap = fadd.closest('.ff-filters');
    wrap.querySelector('.ff-filter-rows').appendChild(
      wrap.querySelector('.ff-filter-tpl').content.cloneNode(true));
    return;
  }
  const fdel = e.target.closest('.ff-filter-del');
  if (fdel) fdel.closest('.ff-filter-row').remove();
});

// Kind dropdown flips the form between aggregate (agg + filters shown) and a
// bare formula (neither) — whether that formula reads as a derived value or a
// calculated column is inferred from what it references.
calcsBody.addEventListener('change', e => {
  if (e.target.name !== 'kind') return;
  e.target.closest('.ff-calcs-form').dataset.kind = e.target.value;
});

// Save the add/edit form: swap the YAML, re-run, and return to the list.
calcsBody.addEventListener('submit', async e => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);
  fd.append('yaml_text', codeEl.value);
  const res = await fetch('/calcs/save', { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) {
    const err = form.querySelector('.ff-modal-error');
    err.textContent = data.error || 'Could not save.'; err.hidden = false;
    return;
  }
  codeEl.value = data.yaml; run(); loadCalcs();
});

const layoutEl = document.getElementById('layout');
// --- Mode switch -----------------------------------------------------------
// One segmented control drives the whole editor. Exactly one mode is active, so
// the lit icon always says what the screen is showing:
//
//   view   the dashboard, nothing else
//   edit   the same dashboard, plus on-canvas editing — no panel, so nothing
//          covers what you are rearranging
//   code / chat / docs / calcs
//                          a panel *beside* the dashboard — every one of them
//                          is used while looking at it, so none overlap
//
// The dashboard itself is full width in every mode; a panel floats over it
// rather than halving it. Mode is a class on `.layout`, so the CSS can hide the
// editing affordances without the JS touching each one.
const MODES = { view: null, edit: null, code: 'ff-yaml', chat: 'ff-chat', docs: 'ff-docs', calcs: 'ff-calcs' };
const modeSwitch = document.getElementById('ff-modes');
let currentMode = 'view';

function setMode(mode) {
  if (!(mode in MODES)) return;
  currentMode = mode;
  // The server always renders the editor chrome; the mode class decides whether
  // it is shown, so switching costs no re-render.
  // Derived from MODES, not a written-out list: `code` was added to the switch
  // and missed here, so leaving code mode left `.mode-code` behind and the
  // dashboard stayed pinned in a column beside an empty one.
  for (const name of Object.keys(MODES)) layoutEl.classList.remove('mode-' + name);
  layoutEl.classList.add('mode-' + mode);
  layoutEl.classList.toggle('panel-open', MODES[mode] !== null);
  for (const [name, panelId] of Object.entries(MODES)) {
    if (!panelId) continue;
    document.getElementById(panelId).hidden = name !== mode;
  }
  for (const b of modeSwitch.querySelectorAll('button')) {
    b.classList.toggle('active', b.dataset.mode === mode);
  }
  if (mode === 'calcs') loadCalcs();
  if (mode === 'code') codeEl.focus();
  if (mode === 'chat') {
    const t = document.getElementById('chat-text');
    if (t) t.focus();
  }
}

modeSwitch.addEventListener('click', e => {
  const btn = e.target.closest('button');
  // Clicking the active mode again drops its panel and goes back to the
  // dashboard — the same "click it off" the old toggles had.
  if (btn) setMode(btn.dataset.mode === currentMode && currentMode !== 'view' ? 'view' : btn.dataset.mode);
});

// Esc leaves any panel. Move mode and the modal handle their own Esc first.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && currentMode !== 'view' && !inMove()) setMode('view');
});

// --- panel width -----------------------------------------------------------
// Drag the divider to resize the panel; the width is remembered across
// sessions, because how much room you want for YAML or the assistant is a
// preference, not a per-visit decision. It lives in localStorage (per browser,
// per origin) rather than in the dashboard, which is shared.
const PANEL_WIDTH_KEY = 'ff-panel-width';
const splitEl = document.getElementById('ff-split');

function setPanelWidth(px, remember) {
  // Both panes stay usable: the panel can't collapse to nothing, and it can't
  // squeeze the dashboard out of the window.
  const width = Math.round(Math.max(240, Math.min(window.innerWidth - 320, px)));
  layoutEl.style.setProperty('--panel-w', width + 'px');
  if (remember) {
    try { localStorage.setItem(PANEL_WIDTH_KEY, String(width)); } catch (err) {}
  }
}

// Restore before first paint of a panel. A blocked or empty store just leaves
// the CSS default in place.
try {
  const saved = parseInt(localStorage.getItem(PANEL_WIDTH_KEY), 10);
  if (saved > 0) setPanelWidth(saved, false);
} catch (err) {}

let splitDragging = false;
splitEl.addEventListener('mousedown', e => {
  e.preventDefault();                  // no text selection while dragging
  splitDragging = true;
  splitEl.classList.add('dragging');
  document.body.style.userSelect = 'none';
});
window.addEventListener('mousemove', e => {
  if (splitDragging) setPanelWidth(e.clientX - layoutEl.getBoundingClientRect().left, false);
});
window.addEventListener('mouseup', () => {
  if (!splitDragging) return;
  splitDragging = false;
  splitEl.classList.remove('dragging');
  document.body.style.userSelect = '';
  // Store once, on release, rather than on every mousemove.
  const current = parseInt(layoutEl.style.getPropertyValue('--panel-w'), 10);
  if (current > 0) setPanelWidth(current, true);
});

// The dashboard, full width, is what you land on. Run it so the switch lights
// its segment rather than starting with none of them active.
setMode('view');

// --- Resize -----------------------------------------------------------------
// Handles render in the output whatever the mode; the CSS shows them only in
// `edit`, and these handlers bail elsewhere — there is nothing to edit into.
//   Rows: drag the bottom edge to change height. 1 unit = 8px, matching
//   HEIGHT_UNIT_PX in dashboard.py.
//   Columns: drag an interior boundary of the (union) column grid to rebalance
//   the two adjacent fine columns; snaps to 10% steps. On release the server
//   (`config_edit.resize_columns`) recomputes each cell's width from the fine
//   columns it spans — so every row those columns belong to is updated (even a
//   drag started on an inherited/lower row) and spanning cells stay bare.
const HEIGHT_UNIT_PX = 8;
const COL_STEP = 10;   // column widths snap to 10% increments while dragging
let resize = null;

function gcd(a, b) { a = Math.abs(a); b = Math.abs(b); while (b) { [a, b] = [b, a % b]; } return a || 1; }
// Reduce a width vector to its smallest whole-number ratio: [80,20] -> [4,1].
function reduceRatio(nums) {
  const g = nums.reduce((acc, n) => gcd(acc, n), 0) || 1;
  return nums.map(n => Math.round(n / g));
}

const editingDisabled = () => currentMode !== 'edit';

// Rewrite the Nth @<height> token in the `layout:` section. `@<n>` is the
// row-height indicator and rows carry exactly one each in order, so the Nth
// token is row `ordinal`'s height. Rewriting the token directly works for any
// YAML style (flow `["@30", ...]` or block `- "@30"`) — an earlier bracket-only
// version silently no-op'd on block-style rows, so drags snapped back.
function setRowUnits(ordinal, units) {
  const text = codeEl.value;
  const dashIdx = text.search(/^layout:/m);
  const re = /@(\d+(?:\.\d+)?)/g;
  re.lastIndex = dashIdx === -1 ? 0 : dashIdx;
  let m, n = 0;
  while ((m = re.exec(text)) !== null) {
    if (n === ordinal) {
      codeEl.value = text.slice(0, m.index) + '@' + units
        + text.slice(m.index + m[0].length);
      return;
    }
    n++;
  }
}

outEl.addEventListener('mousedown', e => {
  if (editingDisabled()) return;
  const rowHandle = e.target.closest('.fireflyer-resize-handle');
  const colHandle = e.target.closest('.fireflyer-resize-col-handle');
  const handle = rowHandle || colHandle;
  if (!handle) return;
  e.preventDefault();
  const row = handle.closest('.fireflyer-dashboard-row');

  if (rowHandle) {
    const track = parseInt(handle.dataset.track, 10);          // 1-based grid row
    const tracks = row.style.gridTemplateRows.split(/\s+/);    // e.g. ["320px","240px"]
    resize = {
      axis: 'row', handle, row, tracks,
      index: track - 1,
      ordinal: parseInt(handle.dataset.rowOrdinal, 10),
      startY: e.clientY,
      startPx: parseFloat(tracks[track - 1]) || 0,
      px: parseFloat(tracks[track - 1]) || 0,
    };
  } else {
    const left = parseInt(handle.dataset.leftCol, 10);          // 0-based
    const cols = row.style.gridTemplateColumns.split(/\s+/);    // e.g. ["3fr","2fr"]
    // Work in row-relative percentages so the drag is independent of the units
    // stored in the YAML (which are arbitrary proportions like 3:2 or 60:40).
    const nums = cols.map(c => parseFloat(c) || 0);
    const total = nums.reduce((a, b) => a + b, 0) || 1;
    const pcts = nums.map(n => n / total * 100);
    resize = {
      axis: 'col', handle, row, left,
      ordinals: handle.dataset.ordinals.split(',').map(Number),
      startX: e.clientX,
      rowWidth: row.clientWidth,
      pcts,
      leftStart: pcts[left],
      pairPct: pcts[left] + pcts[left + 1],
    };
  }
  handle.classList.add('is-dragging');
  document.body.classList.add('fireflyer-resizing-' + resize.axis);
});

window.addEventListener('mousemove', e => {
  if (!resize) return;
  if (resize.axis === 'row') {
    resize.px = Math.max(HEIGHT_UNIT_PX, resize.startPx + (e.clientY - resize.startY));
    resize.tracks[resize.index] = resize.px + 'px';
    resize.row.style.gridTemplateRows = resize.tracks.join(' ');
  } else {
    const deltaPct = (e.clientX - resize.startX) / resize.rowWidth * 100;
    // Snap the boundary to COL_STEP increments, keeping the dragged pair's
    // combined share fixed so the other columns don't move.
    let lp = Math.round((resize.leftStart + deltaPct) / COL_STEP) * COL_STEP;
    lp = Math.max(COL_STEP, Math.min(resize.pairPct - COL_STEP, lp));
    resize.pcts[resize.left] = lp;
    resize.pcts[resize.left + 1] = resize.pairPct - lp;
    resize.row.style.gridTemplateColumns = resize.pcts.map(p => p + 'fr').join(' ');
  }
});

window.addEventListener('mouseup', () => {
  if (!resize) return;
  const r = resize;
  r.handle.classList.remove('is-dragging');
  document.body.classList.remove('fireflyer-resizing-' + r.axis);
  resize = null;
  if (r.axis === 'row') {
    setRowUnits(r.ordinal, Math.max(1, Math.round(r.px / HEIGHT_UNIT_PX)));
    run();  // re-render so the output reflects (and re-validates) the new YAML
  } else {
    // The dragged columns are the group's fine (union) grid, which may not map
    // 1:1 to YAML tokens (spans, bare cells). The server recomputes each cell's
    // width from the fine columns it covers, so every row the columns belong to
    // is updated and spans stay bare. Reduce to the smallest whole-number ratio
    // first, so a 50/50 drag becomes "1:1".
    commitColumnResize(r.ordinals, reduceRatio(r.pcts.map(p => Math.round(p))));
  }
});
async function commitColumnResize(ordinals, widths) {
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('ordinals', ordinals.join(','));
  fd.append('widths', widths.join(','));
  const res = await fetch('/chart/config/resize-columns', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.ok) codeEl.value = data.yaml;
  run();
}

// --- AI assistant -----------------------------------------------------------
// Sends the message + current YAML + prior turns to /chat. The reply is shown
// in the log; if the assistant returns new YAML, it replaces the editor and
// re-renders through the same run() path as a manual edit.
// The assistant lives in a left-pane overlay toggled from the topbar, mirroring
// the docs/calcs overlays on the right. It stays available whether or not a
// key is configured (a disabled build shows a setup notice inside).

const chatForm = document.getElementById('chat-form');
const chatLog = document.getElementById('chat-log');
const chatText = document.getElementById('chat-text');
const chatSend = document.getElementById('chat-send');
const chatHistory = [];  // plain {role, content} text turns sent each request

function addChatMsg(role, text) {
  const el = document.createElement('div');
  el.className = 'chat-msg ' + role;
  el.textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

async function sendChat(message) {
  addChatMsg('user', message);
  chatSend.disabled = true;
  const pending = addChatMsg('assistant', '…');
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message, yaml: codeEl.value, history: chatHistory}),
    });
    const data = await res.json();
    pending.remove();
    if (!data.ok) {
      addChatMsg('error', data.reply || 'Something went wrong.');
      return;
    }
    addChatMsg('assistant', data.reply);
    chatHistory.push({role: 'user', content: message});
    chatHistory.push({role: 'assistant', content: data.reply});
    if (data.yaml) {
      codeEl.value = data.yaml;
      run();  // reflect the assistant's change in the output panel
    }
  } catch (e) {
    pending.remove();
    addChatMsg('error', String(e));
  } finally {
    chatSend.disabled = false;
  }
}

if (chatForm) {
  chatForm.addEventListener('submit', e => {
    e.preventDefault();
    const msg = chatText.value.trim();
    if (!msg) return;
    chatText.value = '';
    sendChat(msg);
  });
  // Enter sends; Shift+Enter inserts a newline.
  chatText.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); chatForm.requestSubmit(); }
  });
}

// --- Edit-chart modal -------------------------------------------------------
// The pencil on each chart opens a form built server-side from that chart's
// PARAMS. Save posts the fields back; the server rewrites just that chart's
// YAML block and returns the whole document, which we swap in and re-run.
const modalOverlay = document.getElementById('ff-modal-overlay');
const modalBox = document.getElementById('ff-modal');
let editingCid = null;   // chart being edited, or null when adding a new one
let addTarget = null;    // {mode, index} placement while adding

function closeModal() {
  modalOverlay.classList.remove('open');
  modalBox.innerHTML = '';
  editingCid = null;
  addTarget = null;
}

async function openChartEditor(cid) {
  editingCid = cid;
  addTarget = null;
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('cid', cid);
  const res = await fetch('/chart/config/form', { method: 'POST', body: fd });
  modalBox.innerHTML = await res.text();
  modalOverlay.classList.add('open');
}

// Open the same modal in "add" mode. `mode` is 'row' (new row) or 'cell' (add to
// an existing row); `index` is the insert-before ordinal / 'end', or the row
// ordinal for 'cell'.
async function openAddChart(mode, index) {
  editingCid = null;
  addTarget = { mode, index };
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('add_type', 'table');
  fd.append('add_mode', mode);
  fd.append('add_index', index);
  const res = await fetch('/chart/config/add-form', { method: 'POST', body: fd });
  modalBox.innerHTML = await res.text();
  modalOverlay.classList.add('open');
}

// Show a confirm dialog before deleting a chart.
function openDeleteConfirm(cid) {
  editingCid = null;
  addTarget = null;
  modalBox.innerHTML =
    '<div class="ff-modal-head"><span class="ff-modal-title">Delete chart</span></div>' +
    '<div class="ff-modal-body"><p class="ff-confirm-text"></p></div>' +
    '<div class="ff-modal-error" hidden></div>' +
    '<div class="ff-modal-foot">' +
    '<button type="button" class="ff-btn ff-cancel">Cancel</button>' +
    '<button type="button" class="ff-btn ff-danger" data-delete-cid>Delete</button>' +
    '</div>';
  // textContent so the id can't inject markup.
  modalBox.querySelector('.ff-confirm-text').textContent =
    'Delete "' + cid + '"? It will be removed from the dashboard.';
  modalBox.querySelector('[data-delete-cid]').dataset.deleteCid = cid;
  modalOverlay.classList.add('open');
}

// Same confirm dialog for a header/separator, keyed by its layout-item index.
function openItemDeleteConfirm(index, kind) {
  editingCid = null;
  addTarget = null;
  modalBox.innerHTML =
    '<div class="ff-modal-head"><span class="ff-modal-title">Delete ' + kind + '</span></div>' +
    '<div class="ff-modal-body"><p class="ff-confirm-text"></p></div>' +
    '<div class="ff-modal-error" hidden></div>' +
    '<div class="ff-modal-foot">' +
    '<button type="button" class="ff-btn ff-cancel">Cancel</button>' +
    '<button type="button" class="ff-btn ff-danger" data-delete-item>Delete</button>' +
    '</div>';
  modalBox.querySelector('.ff-confirm-text').textContent =
    'Delete this ' + kind + '? It will be removed from the dashboard.';
  modalBox.querySelector('[data-delete-item]').dataset.deleteItem = index;
  modalOverlay.classList.add('open');
}

// Pencil edits, trash deletes, gutter "+" adds. All ignored while the pane is hidden.
outEl.addEventListener('click', e => {
  // Switching tabs is a view action (not an edit), so allow it even in Preview
  // where the editor pane is hidden. During a move the capture handler below
  // gets the click first, so this only fires when not moving.
  const tabSwitchNav = e.target.closest('.fireflyer-tab-switch');
  if (tabSwitchNav) { switchTab(parseInt(tabSwitchNav.dataset.tabIndex, 10)); return; }
  if (editingDisabled()) return;
  // Charts carry data-cid; headers/separators carry data-item-index; tabs carry
  // data-tab-index. The move, edit and delete buttons are shared markup, so
  // branch on which identifier is present.
  const move = e.target.closest('.fireflyer-move-btn');
  if (move) {
    if (move.dataset.cid) enterMove(move.dataset.cid);
    else if (move.dataset.tabIndex !== undefined) enterTabMove(move.dataset.tabIndex, move.closest('.fireflyer-tab-wrap'));
    else enterItemMove(move.dataset.itemIndex, move.closest('.fireflyer-dashboard-item'));
    return;
  }
  const edit = e.target.closest('.fireflyer-edit-btn');
  if (edit) {
    if (edit.dataset.cid) openChartEditor(edit.dataset.cid);
    else if (edit.dataset.tabIndex !== undefined) startTabEdit(edit.closest('.fireflyer-tab-wrap').querySelector('.fireflyer-tab-switch'));
    else startHeaderEdit(edit.closest('.fireflyer-dashboard-item').querySelector('.fireflyer-dashboard-header'));
    return;
  }
  const del = e.target.closest('.fireflyer-delete-btn');
  if (del) {
    if (del.dataset.cid) openDeleteConfirm(del.dataset.cid);
    else if (del.dataset.tabIndex !== undefined) openTabDeleteConfirm(del.dataset.tabIndex);
    else openItemDeleteConfirm(del.dataset.itemIndex, del.closest('.fireflyer-dashboard-item').dataset.kind);
    return;
  }
  const addCell = e.target.closest('.fireflyer-add-cell');
  if (addCell) { openAddChart('cell', addCell.dataset.row); return; }
  const addRow = e.target.closest('.fireflyer-add-row-btn');
  if (addRow) { showAddMenu(addRow, addRow.closest('.fireflyer-add-row').dataset.before); return; }
});

// Insert-row "+" opens a small menu: chart (modal), header, or separator.
const addMenu = document.getElementById('ff-addmenu');
let addMenuBefore = null;

function showAddMenu(btn, before) {
  addMenuBefore = before;
  const r = btn.getBoundingClientRect();
  addMenu.style.left = (r.right + 6) + 'px';
  addMenu.style.top = r.top + 'px';
  addMenu.hidden = false;
}
function hideAddMenu() { addMenu.hidden = true; addMenuBefore = null; }

async function insertLayoutItem(kind, before) {
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('kind', kind);
  fd.append('before', before);
  const res = await fetch('/chart/config/insert-item', { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) { flash(data.error || 'Could not insert.'); return; }
  codeEl.value = data.yaml;
  run();
}

addMenu.addEventListener('click', e => {
  const opt = e.target.closest('[data-add-kind]');
  if (!opt) return;
  const kind = opt.dataset.addKind;
  const before = addMenuBefore;
  hideAddMenu();
  if (kind === 'chart') openAddChart('row', before);
  else if (kind === 'tab') insertTab(before);
  else insertLayoutItem(kind, before);
});

// Dismiss the menu on an outside click or Escape.
document.addEventListener('click', e => {
  if (!addMenu.hidden && !addMenu.contains(e.target) && !e.target.closest('.fireflyer-add-row-btn')) hideAddMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideAddMenu(); });

// Double-click a header (edit mode) to rename it inline. Enter/blur saves,
// Escape cancels; the save rewrites the header line and re-renders.
outEl.addEventListener('dblclick', e => {
  if (editingDisabled() || inMove()) return;
  const h = e.target.closest('.fireflyer-dashboard-header.fireflyer-editable');
  if (h) startHeaderEdit(h);
});

// Editing a header borrows move mode's focus feel: the rest of the dashboard dims
// and its hover affordances are suppressed (.ff-focus-mode), and the same topbar
// cancel button appears. `currentHeaderFinish` lets that button / Esc cancel the
// active edit. Enter (or blur) saves; Esc / cancel button restores the original.
// Inline rename shared by headers and tabs: `el` becomes contentEditable, the
// dashboard enters focus mode (dims the rest, shows the topbar cancel), and
// `saveFn(text)` runs on Enter/blur. `sourceEl` stays lit while editing.
// `onCancel` (optional) runs instead of restoring the text when the edit is
// cancelled — used to undo a just-added tab.
let currentHeaderFinish = null;
function beginInlineEdit(el, sourceEl, saveFn, onCancel) {
  const original = el.textContent;
  const dash = dashboardEl();
  el.contentEditable = 'true';
  el.classList.add('editing');
  if (dash) dash.classList.add('ff-focus-mode');
  if (sourceEl) sourceEl.classList.add('ff-edit-source');
  positionMoveDiscard();
  moveDiscard.hidden = false;
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  let done = false;
  function finish(save) {
    if (done) return;
    done = true;
    currentHeaderFinish = null;
    el.contentEditable = 'false';
    el.classList.remove('editing');
    if (dash) dash.classList.remove('ff-focus-mode');
    if (sourceEl) sourceEl.classList.remove('ff-edit-source');
    moveDiscard.hidden = true;
    el.removeEventListener('keydown', onKey);
    el.removeEventListener('blur', onBlur);
    const text = el.textContent.trim();
    if (save && text && text !== original) saveFn(text);
    // With onCancel set (a just-added tab), anything short of a real new name —
    // Esc, blur, or keeping the default — undoes the add: name it or cancel.
    else if (onCancel) onCancel();
    else el.textContent = original;           // plain rename -> restore the text
  }
  currentHeaderFinish = finish;
  function onKey(ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
    else if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
  }
  function onBlur() { finish(true); }
  el.addEventListener('keydown', onKey);
  el.addEventListener('blur', onBlur);
}

function startHeaderEdit(h) {
  beginInlineEdit(h, h.closest('.fireflyer-dashboard-item'), text => saveHeader(h.dataset.headerIndex, text));
}
function startTabEdit(el) {
  if (!el) return;
  beginInlineEdit(el, el.closest('.fireflyer-tab-wrap'), text => saveTab(el.dataset.tabIndex, text));
}
// A freshly added tab: force a name. `revertYaml` is the pre-add document, so
// cancelling (Esc/✕/blur/keeping the default) removes the just-added tab.
function startTabEditForNew(el, revertYaml) {
  if (!el) { return; }
  beginInlineEdit(
    el, el.closest('.fireflyer-tab-wrap'),
    text => saveTab(el.dataset.tabIndex, text),
    () => { codeEl.value = revertYaml; run(); },
  );
}

async function saveHeader(index, text) {
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('index', index);
  fd.append('text', text);
  const res = await fetch('/chart/config/header', { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) { flash(data.error || 'Could not rename.'); return; }
  codeEl.value = data.yaml;
  run();
}

// --- Tabs -------------------------------------------------------------------
// The tab bar's editor gestures. Switching re-runs on the chosen tab (only its
// charts load). Add/rename/move/delete post to the tab config routes, swap the
// returned YAML in, and re-run — same pattern as the header/chart gestures.
async function postTab(url, params) {
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  for (const [k, v] of Object.entries(params)) fd.append(k, v);
  const res = await fetch(url, { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) { flash(data.error || 'Tab action failed.'); return null; }
  codeEl.value = data.yaml;
  return data;
}

async function switchTab(index) {
  // A chart move OR a tab move can span tabs — keep whichever is active alive so
  // the target row can be found in another tab.
  const chartMove = moveCid;
  const tabMove = moveTabIndex;
  activeTab = index;
  await run();
  if (chartMove !== null) enterMove(chartMove);
  else if (tabMove !== null) enterTabMove(tabMove, outEl.querySelector('.fireflyer-tab-wrap[data-tab-index="' + tabMove + '"]'));
}

// "Tab" in the between-rows "+" menu. On a flat dashboard the first pick enables
// tabs by wrapping the whole layout in one tab; once tabbed, a pick splits the
// current tab at that gap. Either way the new tab opens for a forced rename —
// cancelling undoes the add (reverting to `prevYaml`).
async function insertTab(before) {
  const prevYaml = codeEl.value;
  if (!outEl.querySelector('.fireflyer-tabs')) {
    if (await postTab('/chart/config/tab-add-first', {})) {
      activeTab = 0;
      await run();
      startTabEditForNew(outEl.querySelector('.fireflyer-tab-switch[data-tab-index="0"]'), prevYaml);
    }
    return;
  }
  if (await postTab('/chart/config/tab-insert', { before })) {
    await run();
    // insert_tab names the new tab "New tab"; the forced rename keeps at most one
    // such tab around, so this reliably finds the one just added.
    startTabEditForNew(outEl.querySelector('.fireflyer-tab-switch[data-tab-name="New tab"]'), prevYaml);
  }
}

function saveTab(index, name) {
  postTab('/chart/config/tab-rename', { index, name }).then(d => { if (d) run(); });
}

// First tab dissolves every tab back to a flat list; any other merges into the
// previous. Read the tab names from the bar so the first-tab confirm can list
// what will be removed.
function openTabDeleteConfirm(index) {
  editingCid = null;
  addTarget = null;
  const names = [...outEl.querySelectorAll('.fireflyer-tab-switch')].map(t => t.textContent);
  const first = String(index) === '0';
  const msg = first
    ? 'Deleting the first tab removes all tabs and flattens the dashboard. Tabs removed: ' + names.join(', ') + '.'
    : 'Delete tab "' + (names[index] || '') + '"? Its charts merge into the previous tab.';
  modalBox.innerHTML =
    '<div class="ff-modal-head"><span class="ff-modal-title">Delete tab</span></div>' +
    '<div class="ff-modal-body"><p class="ff-confirm-text"></p></div>' +
    '<div class="ff-modal-error" hidden></div>' +
    '<div class="ff-modal-foot">' +
    '<button type="button" class="ff-btn ff-cancel">Cancel</button>' +
    '<button type="button" class="ff-btn ff-danger" data-delete-tab>Delete</button>' +
    '</div>';
  modalBox.querySelector('.ff-confirm-text').textContent = msg;
  modalBox.querySelector('[data-delete-tab]').dataset.deleteTab = index;
  modalOverlay.classList.add('open');
}

// --- Move mode --------------------------------------------------------------
// Click a chart's "move" button to enter move mode: the picked chart stays lit,
// every other interaction (resize, edit, add, crossfilter) is turned off, and
// every valid spot lights up as a blue box — the column zones (place before/after
// a cell) plus the between-rows strips (into a new row). The hovered box goes
// solid as a placement preview. Click a box to commit (server rewrites the
// layout, we re-render); Esc/Cancel exits.
// A move is either a chart (moveCid set) or a header/separator (moveItemIndex
// set). A header/separator can only land between rows, so it uses just the
// between-row strips — no side/merge zones.
let moveCid = null;
let moveItemIndex = null;
let moveTabIndex = null;
const inMove = () => moveCid !== null || moveItemIndex !== null || moveTabIndex !== null;
const dashboardEl = () => outEl.querySelector('.fireflyer-dashboard');
const moveDiscard = document.getElementById('ff-move-cancel');
const outputPane = outEl.closest('.pane');

// Align the cancel button's left edge with the output pane so it covers exactly
// the right side, no matter where the pane split is dragged.
function positionMoveDiscard() {
  moveDiscard.style.left = outputPane.getBoundingClientRect().left + 'px';
}
new ResizeObserver(() => { if (inMove() || currentHeaderFinish) positionMoveDiscard(); }).observe(outputPane);

async function postMove(url, params) {
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  for (const [k, v] of Object.entries(params)) fd.append(k, v);
  const res = await fetch(url, { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) { flash(data.error || 'Could not move.'); return; }
  codeEl.value = data.yaml;
  run();
}

// Build the drop zones for move mode, per the documented rules:
//  R1/R3/R4 — a SIDE zone on the left and right of every chart (except the
//    dragged one), at the chart's own height; a merged chart's cell is tall, so
//    its sides are full height and dropping there adopts its span.
//  R2 — common borders collapse to ONE drop (dedup), keeping the taller side.
//  R6 — no side zone on the dragged chart, nor on any border it shares.
//  R5 — a SINGLE merge bar, only for the dragged chart, only if a chart-row is
//    directly below it: a long bar down its centre into that row — drop to grow
//    it down one row (merge_down).
//  (between-rows: the whole-width add-row strips + internal-group gaps.)
function buildMoveZones() {
  const dash = dashboardEl();
  if (!dash) return;
  const overlay = document.createElement('div');
  overlay.className = 'ff-move-overlay';
  const base = dash.getBoundingClientRect();
  const cells = [...dash.querySelectorAll('.fireflyer-dashboard-cell[data-cid]')];
  const breaks = [...dash.querySelectorAll('.fireflyer-dashboard-header, .fireflyer-dashboard-separator')]
    .map(el => { const r = el.getBoundingClientRect(); return (r.top + r.bottom) / 2; });
  const srcCell = dash.querySelector('.fireflyer-dashboard-cell.ff-move-source');
  // A MERGED dragged chart keeps its shared borders (R3): each is a per-row
  // unmerge zone — dropping there puts it back single-row in that row. A plain
  // dragged chart suppresses them (R6).
  const srcMerged = srcCell && (srcCell.style.gridRow || '').includes('span');

  // Candidate side zones: both edges of every chart, grouped into visual rows.
  const bands = new Map();
  cells.forEach(c => {
    const k = Math.round(c.getBoundingClientRect().top);
    if (!bands.has(k)) bands.set(k, []);
    bands.get(k).push(c);
  });
  const cand = [];
  bands.forEach(band => {
    const sorted = band.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
    sorted.forEach((cell, i) => {
      if (cell.dataset.cid === moveCid) return;                        // R6: not the dragged chart itself
      const r = cell.getBoundingClientRect();
      const merged = (cell.style.gridRow || '').includes('span');
      const leftMoved = !srcMerged && i > 0 && sorted[i - 1].dataset.cid === moveCid;    // R6 (R3 keeps them)
      const rightMoved = !srcMerged && i < sorted.length - 1 && sorted[i + 1].dataset.cid === moveCid;
      if (!leftMoved) cand.push({ x: r.left, top: r.top, bottom: r.bottom, h: r.height, dst: cell.dataset.cid, pos: 'before', merged });
      if (!rightMoved) cand.push({ x: r.right, top: r.top, bottom: r.bottom, h: r.height, dst: cell.dataset.cid, pos: 'after', merged });
    });
  });
  dedupBorders(cand).forEach(z => addZone(overlay, base, z.x, z.top, z.h, z.dst, z.pos));

  // R5: the one merge bar, for the dragged chart, if a chart-row sits below it.
  if (srcCell) {
    const r = srcCell.getBoundingClientRect();
    const belowBottom = rowBelow(cells, r, breaks);
    if (belowBottom !== null) addMergeZone(overlay, base, (r.left + r.right) / 2, r.top, belowBottom - r.top);
  }

  dash.querySelectorAll('.fireflyer-dashboard-row').forEach(row => {
    const rc = [...row.querySelectorAll('.fireflyer-dashboard-cell[data-cid]')];
    if (rc.length) addInternalRowZones(overlay, base, row, rc);
  });
  dash.appendChild(overlay);
}
// Collapse duplicate common borders (R2) — but only between NON-merged charts. A
// merged chart keeps its own full-height side zone (R5), so two zones on one
// border are dropped to one only when neither is merged; a merged chart's side
// always survives and sits next to the neighbour's.
function dedupBorders(zones) {
  const kept = [];
  zones.forEach(z => {
    const dup = !z.merged && kept.find(k =>
      !k.merged && Math.abs(k.x - z.x) < 14 && z.top < k.bottom - 4 && z.bottom > k.top + 4);
    if (!dup) kept.push(z);
  });
  return kept;
}
// Bottom y of the row directly below rect `r` — the nearest row of cells whose
// top sits below r, with no header/separator between. null if there's none.
function rowBelow(cells, r, breaks) {
  let nextTop = Infinity;
  cells.forEach(c => {
    const t = c.getBoundingClientRect().top;
    if (t > r.bottom - 4 && t < nextTop) nextTop = t;
  });
  if (!isFinite(nextTop)) return null;
  if (breaks.some(y => y > r.bottom && y < nextTop)) return null;
  // The row's own height is its SHORTEST cell — a cell that itself spans further
  // down (e.g. a 2-row pie) must not stretch the merge bar past one row.
  let bottom = null;
  cells.forEach(c => {
    const cr = c.getBoundingClientRect();
    if (Math.abs(cr.top - nextTop) < 4) bottom = Math.min(bottom === null ? cr.bottom : bottom, cr.bottom);
  });
  return bottom;
}
function addMergeZone(overlay, base, cx, top, height) {
  const z = document.createElement('div');
  z.className = 'ff-move-zone ff-move-zone-span';
  z.style.left = (cx - base.left) + 'px';
  z.style.top = (top - base.top) + 'px';
  z.style.height = height + 'px';
  z.dataset.mergeDown = '1';
  overlay.appendChild(z);
}
// A multi-row group renders as one grid; add a horizontal box at each boundary
// between its rows so a chart can be dropped into its own row between them.
function addInternalRowZones(overlay, base, row, cells) {
  const ordinals = (row.dataset.ordinals || '').split(',').filter(Boolean);
  if (ordinals.length < 2) return;
  const rr = row.getBoundingClientRect();
  const tops = [...new Set(cells.map(c => Math.round(c.getBoundingClientRect().top)))].sort((a, b) => a - b);
  for (let i = 1; i < tops.length && i < ordinals.length; i++) {
    const z = document.createElement('div');
    z.className = 'ff-move-zone-h';
    z.style.left = (rr.left - base.left) + 'px';
    z.style.top = (tops[i] - base.top) + 'px';
    z.style.width = rr.width + 'px';
    z.dataset.before = ordinals[i];   // new row before this YAML row
    overlay.appendChild(z);
  }
}
function addZone(overlay, base, x, top, height, dst, position) {
  const z = document.createElement('div');
  z.className = 'ff-move-zone';
  z.style.left = (x - base.left) + 'px';
  z.style.top = (top - base.top) + 'px';
  z.style.height = height + 'px';
  z.dataset.dst = dst;
  z.dataset.position = position;
  overlay.appendChild(z);
}

function enterMove(cid) {
  moveCid = cid;
  const dash = dashboardEl();
  if (dash) {
    dash.classList.add('ff-move-mode');
    const src = dash.querySelector('.fireflyer-dashboard-cell[data-cid="' + cid + '"]');
    if (src) src.classList.add('ff-move-source');
    buildMoveZones();
  }
  positionMoveDiscard();
  moveDiscard.hidden = false;
}
// Header/separator move: no drop-zone overlay — the between-row strips (already
// lit by ff-move-mode) are the only valid targets.
function enterItemMove(index, itemEl) {
  moveItemIndex = index;
  const dash = dashboardEl();
  if (dash) {
    dash.classList.add('ff-move-mode');
    if (itemEl) {
      itemEl.classList.add('ff-move-source');
      // The strips right above and below the item are no-ops (they'd drop it back
      // where it already is), so hide them — only real relocations stay lit.
      [itemEl.previousElementSibling, itemEl.nextElementSibling].forEach(el => {
        if (el && el.classList.contains('fireflyer-add-row')) el.classList.add('ff-move-hidden-strip');
      });
    }
  }
  positionMoveDiscard();
  moveDiscard.hidden = false;
}
// Tab move: like a header/separator move, its only drop targets are the
// between-row strips (already lit by ff-move-mode). Dropping moves the tab's
// key line — reordering it and reassigning the rows that fall under it.
function enterTabMove(index, wrapEl) {
  moveTabIndex = index;
  const dash = dashboardEl();
  if (dash) {
    dash.classList.add('ff-move-mode');
    if (wrapEl) wrapEl.classList.add('ff-move-source');
  }
  positionMoveDiscard();
  moveDiscard.hidden = false;
}
function exitMove() {
  const dash = dashboardEl();
  if (dash) {
    dash.classList.remove('ff-move-mode');
    dash.querySelectorAll('.ff-move-source').forEach(el => el.classList.remove('ff-move-source'));
    dash.querySelectorAll('.ff-move-hidden-strip').forEach(el => el.classList.remove('ff-move-hidden-strip'));
    const overlay = dash.querySelector('.ff-move-overlay');
    if (overlay) overlay.remove();
  }
  moveDiscard.hidden = true;
  moveCid = null;
  moveItemIndex = null;
  moveTabIndex = null;
}

// While moving, capture clicks: a blue box commits, anything else cancels — and
// the event is swallowed so nothing else (crossfilter, chart controls) fires.
outEl.addEventListener('click', e => {
  if (!inMove()) return;
  e.preventDefault();
  e.stopPropagation();
  // Switching tabs mid-move lets a chart or a tab boundary be dropped into
  // another tab: the move stays alive across the re-render (switchTab re-enters
  // it), so the target row can be found there. (A header/separator move stays in
  // the current view.)
  const tabSwitch = e.target.closest('.fireflyer-tab-switch');
  if (tabSwitch) {
    if (moveCid !== null || moveTabIndex !== null) switchTab(parseInt(tabSwitch.dataset.tabIndex, 10));
    return;
  }
  const strip = e.target.closest('.fireflyer-add-row');
  // Tab move: only a between-row strip is valid — it repositions the tab's
  // boundary there (reordering the tab and reassigning the rows below it).
  if (moveTabIndex !== null) {
    if (strip) {
      const index = moveTabIndex;
      exitMove();
      postMove('/chart/config/tab-move', { index, before: strip.dataset.before });
    }
    return;
  }
  // Header/separator: only a between-row strip is a valid drop; anything else
  // is a miss (move mode stays active until Esc/Cancel or an outside click).
  if (moveItemIndex !== null) {
    if (strip) {
      const index = moveItemIndex;
      exitMove();
      postMove('/chart/config/move-item', { index, before: strip.dataset.before });
    }
    return;
  }
  const zone = e.target.closest('.ff-move-zone, .ff-move-zone-h');
  const src = moveCid;
  if (zone && zone.dataset.mergeDown) {
    exitMove();
    postMove('/chart/config/merge-down', { cid: src });
  } else if (zone && zone.dataset.dst) {
    exitMove();
    postMove('/chart/config/move', { src, dst: zone.dataset.dst, position: zone.dataset.position });
  } else if (zone && zone.dataset.before !== undefined) {
    exitMove();
    postMove('/chart/config/new-row', { src, before: zone.dataset.before });
  } else if (strip) {
    exitMove();
    postMove('/chart/config/new-row', { src, before: strip.dataset.before });
  }
  // A miss inside the pane (a chart or blank space) does nothing — the click is
  // already swallowed, so move mode stays active. Only Esc/Cancel or a click
  // outside the pane exits.
}, true);

// Block every other pointer interaction (resize starts on mousedown) while moving.
outEl.addEventListener('mousedown', e => { if (inMove()) e.stopPropagation(); }, true);

// During a header edit, cancel on mousedown and preventDefault so the header
// keeps focus (a plain click would blur it first and save). Move mode uses click.
moveDiscard.addEventListener('mousedown', e => {
  if (currentHeaderFinish) { e.preventDefault(); currentHeaderFinish(false); }
});
moveDiscard.addEventListener('click', () => { if (inMove()) exitMove(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && inMove()) exitMove(); });
// A click anywhere outside the output pane (editor, topbar, blank space) cancels
// the move — clicks inside the pane are handled above (a zone commits, a miss
// cancels), and are stopped from reaching here.
document.addEventListener('click', e => {
  if (inMove() && !outEl.contains(e.target)) exitMove();
});

// Confirm-delete button inside the modal.
modalBox.addEventListener('click', async e => {
  const chartBtn = e.target.closest('[data-delete-cid]');
  const itemBtn = e.target.closest('[data-delete-item]');
  const tabBtn = e.target.closest('[data-delete-tab]');
  if (!chartBtn && !itemBtn && !tabBtn) return;
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  let url;
  if (chartBtn) { fd.append('cid', chartBtn.dataset.deleteCid); url = '/chart/config/delete'; }
  else if (itemBtn) { fd.append('index', itemBtn.dataset.deleteItem); url = '/chart/config/delete-item'; }
  else { fd.append('index', tabBtn.dataset.deleteTab); url = '/chart/config/tab-delete'; }
  const res = await fetch(url, { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) {
    const err = modalBox.querySelector('.ff-modal-error');
    err.textContent = data.error || 'Could not delete.';
    err.hidden = false;
    return;
  }
  codeEl.value = data.yaml;
  closeModal();
  run();
});

// Backdrop / Cancel / Escape close.
modalOverlay.addEventListener('click', e => {
  if (e.target === modalOverlay || e.target.closest('.ff-cancel')) closeModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && modalOverlay.classList.contains('open')) closeModal();
});

// Swapping the chart type re-fetches the form so its fields match the new
// type's params (overlapping values carry over from the saved config).
modalBox.addEventListener('change', async e => {
  const sel = e.target.closest('[data-type-select]');
  if (!sel) return;
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  let endpoint;
  if (editingCid === null) {           // add mode: rebuild the create form
    fd.append('add_type', sel.value);
    fd.append('add_mode', addTarget.mode);
    fd.append('add_index', addTarget.index);
    endpoint = '/chart/config/add-form';
  } else {                            // edit mode: rebuild for the new type
    fd.append('cid', editingCid);
    fd.append('type_override', sel.value);
    endpoint = '/chart/config/form';
  }
  const res = await fetch(endpoint, { method: 'POST', body: fd });
  modalBox.innerHTML = await res.text();
});

// Filter builder: add clones the blank-row template; the × removes a row.
modalBox.addEventListener('click', e => {
  const add = e.target.closest('.ff-filter-add');
  if (add) {
    const wrap = add.closest('.ff-filters');
    const tpl = wrap.querySelector('.ff-filter-tpl');
    wrap.querySelector('.ff-filter-rows').appendChild(tpl.content.cloneNode(true));
    return;
  }
  const del = e.target.closest('.ff-filter-del');
  if (del) del.closest('.ff-filter-row').remove();
});

// Filter builder (chart modal and calcs manager): a new column or op re-renders
// that row's fields, so `in` / `not in` list the column's values to tick. Only
// this row's fields are sent — the form holds every row, which is why this is
// done here rather than with htmx like the dashboard's filter panel.
async function refreshFilterRow(row) {
  const fields = row.querySelector('.ff-filter-fields');
  const form = row.closest('form');
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('filter_dataset', form && form.elements.dataset ? form.elements.dataset.value : '');
  for (const el of fields.querySelectorAll('[name]')) {
    if (el.type === 'checkbox' && !el.checked) continue;
    fd.append(el.name, el.value);
  }
  const res = await fetch('/filter/fields', { method: 'POST', body: fd });
  if (res.ok) fields.outerHTML = await res.text();
}
document.addEventListener('change', e => {
  const name = e.target.name;
  if (name !== 'filter_column' && name !== 'filter_op') return;
  const row = e.target.closest('.ff-filter-row');
  if (row) refreshFilterRow(row);
});

// The date-range picker is htmx (the same control the dashboard's filter panel
// uses), but htmx only wires markup it swapped in itself — not the modal's and
// the calcs manager's, which this script inserts. Watching both covers every
// path that fills them; `process` skips what it has already wired.
if (typeof MutationObserver !== 'undefined') {
  const wireHtmx = new MutationObserver(records => {
    if (!window.htmx) return;
    for (const r of records) window.htmx.process(r.target);
  });
  for (const el of [modalBox, calcsBody]) wireHtmx.observe(el, { childList: true, subtree: true });
}

// A row's value search: re-fetch just its list after a pause in typing, so the
// box keeps focus. Ticked values ride along and stay listed. A response that
// arrives after a newer search was sent is dropped, or a slow early one could
// overwrite the later result.
let searchTimer = null;
let searchSeq = 0;
async function searchFilterValues(row, box) {
  const seq = ++searchSeq;
  const form = row.closest('form');
  const fd = new FormData();
  fd.append('yaml_text', codeEl.value);
  fd.append('filter_dataset', form && form.elements.dataset ? form.elements.dataset.value : '');
  fd.append('filter_column', row.querySelector('[name=filter_column]').value);
  fd.append('filter_q', box.value);
  for (const el of row.querySelectorAll('[name=filter_value]:checked')) fd.append('filter_value', el.value);
  const res = await fetch('/filter/values', { method: 'POST', body: fd });
  if (!res.ok || seq !== searchSeq) return;
  row.querySelector('.ff-filter-choices').outerHTML = await res.text();
}
document.addEventListener('input', e => {
  if (e.target.name !== 'filter_q') return;
  const row = e.target.closest('.ff-filter-row');
  if (!row) return;                       // the dashboard panel searches over htmx
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => searchFilterValues(row, e.target), 250);
});

// Save/Add: submit fields + current YAML; on success swap the editor and re-run.
// Edit posts to /save (needs cid); add posts to /create (placement is in the
// form's hidden inputs).
modalBox.addEventListener('submit', async e => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);
  fd.append('yaml_text', codeEl.value);
  let endpoint;
  if (editingCid === null) {
    endpoint = '/chart/config/create';
  } else {
    fd.append('cid', editingCid);
    endpoint = '/chart/config/save';
  }
  const res = await fetch(endpoint, { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) {
    const err = form.querySelector('.ff-modal-error');
    err.textContent = data.error || 'Could not save.';
    err.hidden = false;
    return;
  }
  codeEl.value = data.yaml;
  closeModal();
  run();
});
