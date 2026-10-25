// The look of YAML in a code editor — token colours, the scalars the grammar
// leaves plain, and the editor's theme — shared by the dashboard's YAML editor
// (yaml-editor.js, diff-view.js) and a dataset's column mapping
// (dataset-columns.js), so YAML reads the same everywhere. Colours come from
// code.css's `--yaml-*` variables, which follow the page's theme.

import {Decoration, EditorView, MatchDecorator, ViewPlugin} from '@codemirror/view';
import {HighlightStyle} from '@codemirror/language';
import {tags as t} from '@lezer/highlight';

// The YAML grammar's tokens, coloured from code.css's `--yaml-*` variables,
// so the editor follows the Auto / Light / Dark switch with the rest.
export const yamlStyle = HighlightStyle.define([
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
export const scalarColours = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = scalarMatcher.createDeco(view); }
  update(u) { this.decorations = scalarMatcher.updateDeco(u, this.decorations); }
}, {decorations: v => v.decorations});


export const editorTheme = EditorView.theme({
  '&': {color: 'var(--text)', backgroundColor: 'var(--panel)', fontSize: '13px'},
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
  // A tight gutter: numbers, then a narrow fold column and a narrow problems
  // column — each only as wide as its marker.
  // A line number is a link to its line (see linkFromGutter).
  '.cm-lineNumbers .cm-gutterElement': {padding: '0 4px 0 10px', minWidth: '24px', opacity: '0.7', cursor: 'pointer'},
  '.cm-foldGutter .cm-gutterElement': {padding: '0 1px', width: '10px'},
  '.cm-gutter-lint': {width: '10px'},
  '.cm-gutter-lint .cm-gutterElement': {padding: '0'},
  '.cm-activeLineGutter': {backgroundColor: 'transparent', color: 'var(--text)'},
  '.cm-activeLineGutter.cm-gutterElement': {opacity: '1'},
  '.cm-ff-fold': {display: 'inline-block', width: '10px', cursor: 'pointer', opacity: '0.45', fontSize: '12px', textAlign: 'center'},
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
  '.cm-lint-marker': {width: '8px', height: '8px', marginTop: '6px'},
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
  // The split chip: one pill, two buttons, a hairline between them.
});
