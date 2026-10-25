// The filter builder's rows (`params.FilterListParam`): one implementation for
// every page that builds filters — the dashboard editor's chart modal and calcs
// manager, and a dataset's permissions. Editor chrome (a dev tool), loaded as a
// plain script before the page's own.
//
//   +        clones the blank row (`.ff-filter-tpl`) into its `.ff-filters`
//   ×        removes its row
//   a column (the picker is buttons, `params._column_picker`) or an op
//            re-renders that row's fields from the server, so `in` / `not in`
//            list the column's values to tick
//   a search re-fetches just the row's value list, after a pause in typing
//
// Only the row's own fields are sent — the form holds every row, which is why
// this is done here rather than with htmx like the dashboard's filter panel.
//
// A page says where a row's data comes from by setting
// `ffFilterBuilder.context(row)` -> {yaml_text, filter_dataset}.
window.ffFilterBuilder = {
  context: () => ({yaml_text: '', filter_dataset: ''}),
};

(function () {
  const builder = window.ffFilterBuilder;

  function contextForm(row) {
    const fd = new FormData();
    const ctx = builder.context(row);
    fd.append('yaml_text', ctx.yaml_text || '');
    fd.append('filter_dataset', ctx.filter_dataset || '');
    return fd;
  }

  async function refreshRow(row) {
    const fields = row.querySelector('.ff-filter-fields');
    const fd = contextForm(row);
    for (const el of fields.querySelectorAll('[name]')) {
      if (el.type === 'checkbox' && !el.checked) continue;
      fd.append(el.name, el.value);
    }
    const res = await fetch('/filter/fields', {method: 'POST', body: fd});
    if (!res.ok) return;
    fields.outerHTML = await res.text();
    // The date-range picker is htmx, which only wires markup it swapped in.
    if (window.htmx) window.htmx.process(row);
  }

  // A response that arrives after a newer search was sent is dropped, or a
  // slow early one could overwrite the later result.
  let searchTimer = null;
  let searchSeq = 0;
  async function searchValues(row, box) {
    const seq = ++searchSeq;
    const fd = contextForm(row);
    fd.append('filter_column', row.querySelector('[name=filter_column]').value);
    fd.append('filter_q', box.value);
    for (const el of row.querySelectorAll('[name=filter_value]:checked')) fd.append('filter_value', el.value);
    const res = await fetch('/filter/values', {method: 'POST', body: fd});
    if (!res.ok || seq !== searchSeq) return;
    row.querySelector('.ff-filter-choices').outerHTML = await res.text();
  }

  document.addEventListener('click', e => {
    const add = e.target.closest('.ff-filters .ff-filter-add');
    if (add) {
      const wrap = add.closest('.ff-filters');
      wrap.querySelector('.ff-filter-rows').appendChild(wrap.querySelector('.ff-filter-tpl').content.cloneNode(true));
    }
    const del = e.target.closest('.ff-filter-row .ff-filter-del');
    if (del) del.closest('.ff-filter-row').remove();
    // A pick from the column list is ours to apply — the dashboard's own panel
    // does it over htmx. An entry that isn't a column (a page's own, as the
    // permissions' Full) is the page's to handle.
    const item = e.target.closest('.ff-colpick-item');
    const row = item && item.closest('.ff-filter-row');
    if (row && item.dataset.column !== undefined) {
      e.preventDefault();
      row.querySelector('input[name=filter_column]').value = item.dataset.column;
      item.closest('.ff-colpick').open = false;
      refreshRow(row);
    }
    // A click anywhere else closes an open column list, as a native menu would.
    for (const open of document.querySelectorAll('.ff-colpick[open]')) {
      if (!open.contains(e.target)) open.open = false;
    }
  });
  document.addEventListener('change', e => {
    if (e.target.name !== 'filter_column' && e.target.name !== 'filter_op') return;
    const row = e.target.closest('.ff-filter-row');
    if (row) refreshRow(row);
  });
  document.addEventListener('input', e => {
    if (e.target.name !== 'filter_q') return;
    const row = e.target.closest('.ff-filter-row');
    if (!row) return;                       // the dashboard panel searches over htmx
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchValues(row, e.target), 250);
  });
})();
