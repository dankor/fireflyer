// A SQL field as a code editor: plain text — line numbers, Tab / Shift-Tab
// indent, undo, the current line lit, brackets matched. No SQL grammar: the
// query goes to the database as written, and the database checks it.
//
// Two uses: the dataset page's edit mode (`sqlEditor`, a view of its own), and
// a form's <textarea data-sql-editor> (the new-dataset dialog), which stays as
// the field the form posts — hidden, written on every change.
//
// Editor chrome (a dev tool), like the rest of web/static.

import {EditorState} from '@codemirror/state';
import {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection,
  placeholder,
} from '@codemirror/view';
import {defaultKeymap, history, historyKeymap, indentWithTab} from '@codemirror/commands';
import {indentUnit, bracketMatching} from '@codemirror/language';
import {editorTheme} from 'fireflyer/yaml-look';

// A field's background, not a panel's; a line number is just a number here.
const fieldTheme = EditorView.theme({
  '&': {backgroundColor: 'var(--bg)'},
  '.cm-gutters': {backgroundColor: 'var(--bg)'},
  '.cm-lineNumbers .cm-gutterElement': {cursor: 'default'},
});

export function sqlEditor({parent, doc = '', readOnly = false, hint = '', onChange, onSave}) {
  return new EditorView({
    parent,
    state: EditorState.create({doc, extensions: [
      lineNumbers(), highlightActiveLineGutter(), highlightActiveLine(), history(), drawSelection(),
      bracketMatching(), indentUnit.of('  '), EditorState.tabSize.of(2), EditorView.lineWrapping,
      editorTheme, fieldTheme,
      hint ? placeholder(hint) : [],
      EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly),
      keymap.of([
        ...(onSave ? [{key: 'Mod-s', run: () => { onSave(); return true; }}] : []),
        ...defaultKeymap, ...historyKeymap, indentWithTab,
      ]),
      EditorView.updateListener.of(u => { if (u.docChanged && onChange) onChange(u.state.doc.toString()); }),
    ]}),
  });
}

// The textarea stays — what the form posts, its `disabled` still deciding
// whether it does — and the editor sits before it. A hidden field can't show
// the browser's "required" hint, so an empty query is stopped here instead.
function upgrade(area) {
  const host = document.createElement('div');
  host.className = 'ff-sql';
  area.before(host);
  area.hidden = true;
  const required = area.required;
  area.required = false;
  const view = sqlEditor({
    parent: host, doc: area.value, hint: area.placeholder,
    onChange: text => { area.value = text; },
  });
  area.form?.addEventListener('submit', e => {
    if (required && !area.disabled && !area.value.trim()) { e.preventDefault(); view.focus(); }
  });
}

document.querySelectorAll('textarea[data-sql-editor]').forEach(upgrade);
