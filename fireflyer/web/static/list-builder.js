// The chart form's ordered lists (`params.ListParam`: a table's columns,
// measures and sort). Editor chrome, loaded before editor.js.
//
//   the grip   drag a row to reorder (a drop lands before or after the row
//              under the pointer, by which half it's in)
//   ✕          removes the row
//   the arrow  (sort) flips ascending / descending: `name` / `-name`
//   +          a searchable menu of what may be added; what's listed already
//              is hidden, and a list fed by its siblings (`data-from`, sort's
//              columns + measures) offers just their entries
//
// Every change rewrites the hidden input — the comma-separated text the form
// has always submitted — and fires `change` on it, so the preview follows.
(function () {
  function hidden(list) { return list.querySelector(':scope > input[type=hidden]'); }
  function items(list) { return [...list.querySelectorAll(':scope > .ff-list-items > .ff-list-item')]; }
  function bare(value) { return value.replace(/^[+-]/, ''); }

  function sync(list) {
    const input = hidden(list);
    input.value = items(list).map(li => li.dataset.value).join(', ');
    refresh(list.closest('form') || list);       // a sibling's choices may follow this one
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }

  // A sibling list's choices: its entries — or, for an empty list of columns
  // (a table's: every column), everything its + offers. An empty list of
  // calcs is none.
  function entriesOf(form, name) {
    const list = form.querySelector('.ff-list[data-name="' + CSS.escape(name) + '"]');
    if (!list) return [];
    const listed = items(list).map(li => bare(li.dataset.value));
    if (listed.length || list.dataset.reads !== 'columns') return listed;
    return [...list.querySelectorAll('.ff-list-option')].map(o => o.dataset.value);
  }

  // What the menu offers: not what's listed, and for a list fed by its
  // siblings, only what they hold; narrowed by the search.
  function addable(list) {
    const taken = new Set(items(list).map(li => bare(li.dataset.value)));
    let allowed = null;
    if (list.dataset.from) {
      const form = list.closest('form');
      allowed = new Set(list.dataset.from.split(' ').flatMap(n => entriesOf(form, n)));
    }
    return [...list.querySelectorAll('.ff-list-option')]
      .filter(o => !taken.has(o.dataset.value) && (!allowed || allowed.has(o.dataset.value)));
  }

  function refilter(list) {
    const ok = new Set(addable(list));
    const q = list.querySelector('.ff-list-search').value.trim().toLowerCase();
    let shown = 0;
    for (const option of list.querySelectorAll('.ff-list-option')) {
      const hit = ok.has(option) && (!q || option.dataset.value.toLowerCase().includes(q));
      option.hidden = !hit;
      if (hit) shown++;
    }
    list.querySelector('.ff-list-none').hidden = shown > 0;
  }

  // The + only while there's something to add — none once every choice is
  // listed (or, for sort, once its siblings offer nothing new).
  function refresh(root) {
    for (const list of root.querySelectorAll('.ff-list')) {
      const menu = list.querySelector('.ff-list-add');
      menu.hidden = !addable(list).length;
      if (menu.hidden) menu.open = false;
    }
  }
  // The chart form is built anew on open, and on a new type or dataset:
  // editor.js calls this then.
  window.ffListBuilder = { refresh };

  function prepareMenu(list) {
    const search = list.querySelector('.ff-list-search');
    search.value = '';
    refilter(list);
    setTimeout(() => search.focus(), 0);
  }

  // A new row: the server's blank one (`.ff-list-row`), named, with the
  // option's own type icon.
  function newRow(list, option) {
    const value = option.dataset.value;
    const li = list.querySelector('template.ff-list-row').content.firstElementChild.cloneNode(true);
    li.dataset.value = value;
    const name = li.querySelector('.ff-list-name');
    name.textContent = value;
    const glyph = option.querySelector('.ff-type-glyph');
    if (glyph) name.before(glyph.cloneNode(true));
    li.querySelector('.ff-list-del').setAttribute('aria-label', 'Remove ' + value);
    return li;
  }

  function add(list, option) {
    list.querySelector(':scope > .ff-list-items').appendChild(newRow(list, option));
    list.querySelector('.ff-list-add').open = false;
    sync(list);
  }

  function flip(button) {
    const li = button.closest('.ff-list-item');
    const desc = !li.dataset.value.startsWith('-');
    li.dataset.value = (desc ? '-' : '') + bare(li.dataset.value);
    button.classList.toggle('is-desc', desc);
    const label = desc ? 'Descending — click for ascending' : 'Ascending — click for descending';
    button.title = label;
    button.setAttribute('aria-label', label);
    sync(li.closest('.ff-list'));
  }

  document.addEventListener('click', e => {
    const list = e.target.closest('.ff-list');
    if (!list) return;
    const del = e.target.closest('.ff-list-del');
    if (del) { del.closest('.ff-list-item').remove(); sync(list); return; }
    const dir = e.target.closest('.ff-list-dir');
    if (dir) { flip(dir); return; }
    const option = e.target.closest('.ff-list-option');
    if (option) { e.preventDefault(); add(list, option); }
  });

  document.addEventListener('toggle', e => {
    const menu = e.target;
    if (menu.classList && menu.classList.contains('ff-list-add') && menu.open) prepareMenu(menu.closest('.ff-list'));
  }, true);

  // The menu's search: typing narrows it, Enter adds the first match, Esc
  // closes the menu (and not the modal under it).
  document.addEventListener('input', e => {
    if (e.target.classList.contains('ff-list-search')) refilter(e.target.closest('.ff-list'));
  });
  document.addEventListener('keydown', e => {
    if (!e.target.classList || !e.target.classList.contains('ff-list-search')) return;
    const list = e.target.closest('.ff-list');
    if (e.key === 'Enter') {
      e.preventDefault();                       // not the form's submit
      const first = list.querySelector('.ff-list-option:not([hidden])');
      if (first) add(list, first);
    } else if (e.key === 'Escape') {
      e.stopPropagation();
      list.querySelector('.ff-list-add').open = false;
    }
  }, true);
  // A click outside an open menu closes it, as a native one would.
  document.addEventListener('mousedown', e => {
    for (const open of document.querySelectorAll('.ff-list-add[open]')) {
      if (!open.contains(e.target)) open.open = false;
    }
  });

  // Reordering: native drag and drop, within one list only.
  let dragged = null;
  document.addEventListener('dragstart', e => {
    const li = e.target.closest && e.target.closest('.ff-list-item');
    if (!li) return;
    dragged = li;
    li.classList.add('is-dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', li.dataset.value);   // Firefox starts a drag only with data
  });
  document.addEventListener('dragover', e => {
    if (!dragged) return;
    const over = e.target.closest && e.target.closest('.ff-list-item');
    if (!over || over === dragged || over.parentNode !== dragged.parentNode) return;
    e.preventDefault();
    const box = over.getBoundingClientRect();
    const after = e.clientY > box.top + box.height / 2;
    over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
  });
  document.addEventListener('drop', e => { if (dragged) e.preventDefault(); });
  document.addEventListener('dragend', () => {
    if (!dragged) return;
    dragged.classList.remove('is-dragging');
    const list = dragged.closest('.ff-list');
    dragged = null;
    if (list) sync(list);
  });
})();
