// Compare versions: two YAMLs side by side, as a git client shows a diff. Each
// side is any saved version of the dashboard — numbered, with its local time
// and who saved it — or "Now", what's in the editor (unsaved edits and all).
// Changed lines and words are marked, unchanged stretches fold away, and the
// left side can be restored into the editor as an ordinary unsaved edit.
// It is the editor's `diff` mode: open while the mode is on, gone when it isn't
// — the mode switch and Esc (both editor.js's) are the way in and out.
//
// What's compared rides in the address bar, `?compare=v3..v5`, so a link opens
// the same comparison. Only saved versions can be named: with unsaved edits
// there's nothing another reader (or a reload) could see, so no parameter.
//
// Loaded by yaml-editor.js, which hands it the editor's look (YAML colours and
// theme) so both read the same. Talks to editor.js only through
// `window.ffEditor`; to the server through /d/<id>/versions.

import {MergeView} from '@codemirror/merge';
import {EditorState} from '@codemirror/state';
import {EditorView, lineNumbers} from '@codemirror/view';

export function initDiff(look) {
  const panel = document.getElementById('ff-diff');
  if (!panel) return;
  const pickA = document.getElementById('ff-diff-a');
  const pickB = document.getElementById('ff-diff-b');
  const stats = document.getElementById('ff-diff-stats');
  const restore = document.getElementById('ff-diff-restore');
  const body = document.getElementById('ff-diff-body');
  const editor = window.ffEditor;
  const texts = new Map();          // a saved version's YAML, once fetched
  let merge = null;
  // A comparison the address bar asked for, taken once by the first open().
  let asked = parseAsked(new URLSearchParams(location.search).get(PARAM));

  editor.onModeChange(mode => (mode === 'diff' ? open() : close()));
  if (asked) editor.setMode('diff');
  document.getElementById('ff-diff-swap').addEventListener('click', () => {
    [pickA.value, pickB.value] = [pickB.value, pickA.value];
    render();
  });
  // Step both sides through history: one version older, one newer.
  const older = document.getElementById('ff-diff-older');
  const newer = document.getElementById('ff-diff-newer');
  older.addEventListener('click', () => step(-1));
  newer.addEventListener('click', () => step(+1));
  function step(delta) {
    const pair = stepPair(chronology(), pickA.value, pickB.value, delta);
    if (pair) { [pickA.value, pickB.value] = pair; render(); }
  }
  // Oldest first, "now" last — the order steps walk; "saved" (no history) isn't in it.
  // (Not `history`: that's the browser's, which setParam writes the URL through.)
  const chronology = () => [...pickA.options].map(o => o.value).filter(v => v !== 'saved').reverse();
  pickA.addEventListener('change', render);
  pickB.addEventListener('change', render);
  restore.addEventListener('click', async () => {
    editor.setYaml(await textOf(pickA.value));
    editor.flash(`${labelOf(pickA)} restored into the editor — unsaved until you save.`, 5000);
    await fill(pickA.value, 'now');
  });

  async function open() {
    panel.hidden = false;
    const versions = await listVersions();
    const dirty = editor.yaml !== editor.savedYaml;
    // Unsaved edits: the last save against them. Nothing unsaved: the change
    // the last save made — the version before it against now.
    const newest = versions[0], before = versions[1];
    let left = dirty ? (newest ? key(newest) : 'saved') : (before ? key(before) : newest ? key(newest) : 'saved');
    let right = 'now';
    const known = new Set(versions.map(key));
    if (asked && known.has(asked[0]) && known.has(asked[1])) [left, right] = asked;
    asked = null;
    await fill(left, right, versions);
  }

  function close() {
    panel.hidden = true;
    setParam(null);
    if (merge) { merge.destroy(); merge = null; }
  }

  async function fill(left, right, versions) {
    versions = versions || await listVersions();
    const dirty = editor.yaml !== editor.savedYaml;
    const options = [{value: 'now', label: dirty ? 'Now · unsaved' : 'Now'}];
    // No saved history (the single-dashboard editor, or one never saved): the
    // state the page opened with is the other thing worth comparing.
    if (!versions.length) options.push({value: 'saved', label: editor.dashId ? 'Saved' : 'As opened'});
    for (const v of versions) options.push({value: key(v), label: describe(v, editor.stamp)});
    for (const pick of [pickA, pickB]) {
      pick.replaceChildren(...options.map(o => new Option(o.label, o.value)));
    }
    pickA.value = options.some(o => o.value === left) ? left : options[options.length - 1].value;
    pickB.value = right;
    await render();
  }

  async function render() {
    const [a, b] = await Promise.all([textOf(pickA.value), textOf(pickB.value)]);
    if (merge) merge.destroy();
    const side = text => ({doc: text, extensions: [
      lineNumbers(), ...look, EditorState.readOnly.of(true), EditorView.editable.of(false),
    ]});
    merge = new MergeView({
      a: side(a), b: side(b), parent: body,
      gutter: true, highlightChanges: true,
      collapseUnchanged: {margin: 3, minSize: 6},
    });
    const {added, removed} = count(merge);
    stats.textContent = added || removed ? `+${added} −${removed}` : 'No differences';
    stats.classList.toggle('is-same', !added && !removed);
    // A past state can be brought back — never "now" onto itself.
    const canRestore = pickA.value !== 'now' && a !== editor.yaml;
    restore.hidden = !canRestore;
    restore.textContent = `Restore ${labelOf(pickA).split(' · ')[0]}`;
    restore.title = 'Load the left side into the editor (unsaved; undo with Cmd/Ctrl+Z)';
    older.hidden = !stepPair(chronology(), pickA.value, pickB.value, -1);
    newer.hidden = !stepPair(chronology(), pickA.value, pickB.value, +1);
    setParam(await nameable());
  }

  // `v3..v5` for the two sides, or null when they can't be named: unsaved
  // edits, or a side with no version (no history yet). "Now" with nothing
  // unsaved is the latest version, so it's named as that.
  async function nameable() {
    if (editor.yaml !== editor.savedYaml) return null;
    const versions = await listVersions();
    if (!versions.length) return null;
    const name = value => (value === 'now' ? key(versions[0]) : /^v\d+$/.test(value) ? value : null);
    const [a, b] = [name(pickA.value), name(pickB.value)];
    return a && b ? `${a}..${b}` : null;
  }

  async function listVersions() {
    if (!editor.dashId) return [];
    try {
      const res = await fetch(`/d/${encodeURIComponent(editor.dashId)}/versions`);
      return res.ok ? (await res.json()).versions : [];
    } catch (e) { return []; }
  }

  async function textOf(value) {
    if (value === 'now') return editor.yaml;
    if (value === 'saved') return editor.savedYaml;
    if (!texts.has(value)) {
      const res = await fetch(`/d/${encodeURIComponent(editor.dashId)}/versions/${value.slice(1)}`);
      texts.set(value, res.ok ? (await res.json()).yaml : '');
    }
    return texts.get(value);
  }

  const key = v => 'v' + v.version;
  function setParam(value) {
    const url = new URL(location.href);
    if (value) url.searchParams.set(PARAM, value);
    else url.searchParams.delete(PARAM);
    if (url.href !== location.href) history.replaceState(history.state, '', url);
  }
  const labelOf = pick => pick.options[pick.selectedIndex] ? pick.options[pick.selectedIndex].text : '';
}

const PARAM = 'compare';

// Both sides moved `delta` places along `order` (oldest first), keeping their
// distance — v3⇄v4 one older is v2⇄v3 — or null when either would fall off it
// or isn't on it.
export function stepPair(order, a, b, delta) {
  const i = order.indexOf(a), j = order.indexOf(b);
  if (i < 0 || j < 0) return null;
  const [ni, nj] = [i + delta, j + delta];
  if (Math.min(ni, nj) < 0 || Math.max(ni, nj) >= order.length) return null;
  return [order[ni], order[nj]];
}

// `v3..v5` → ['v3', 'v5']; anything else → null.
function parseAsked(text) {
  const m = /^(v\d+)\.\.(v\d+)$/.exec(text || '');
  return m ? [m[1], m[2]] : null;
}

// `v7 · 2026-10-16 14:05:31 · dana` — the time in the viewer's own zone.
// Local mode has no login, so no author: just the number and the time.
function describe(v, stamp) {
  return [`v${v.version}`, stamp(v.created_at * 1000), v.author].filter(Boolean).join(' · ');
}

// Lines removed on the left and added on the right, over every changed chunk.
function count(merge) {
  let added = 0, removed = 0;
  const lines = (doc, from, to) => (to > from ? doc.lineAt(Math.max(from, to - 1)).number - doc.lineAt(from).number + 1 : 0);
  for (const chunk of merge.chunks) {
    removed += lines(merge.a.state.doc, chunk.fromA, chunk.toA);
    added += lines(merge.b.state.doc, chunk.fromB, chunk.toB);
  }
  return {added, removed};
}
