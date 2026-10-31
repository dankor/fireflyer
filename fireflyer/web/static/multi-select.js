// A multi-select dropdown with search (`portal.multi_select`) — editor chrome,
// usable on any page that links this file and multi-select.css. The ticked
// checkboxes in its list *are* the value (they submit with the form); the
// chips in the box are painted from them. Typing in the box narrows the list;
// ↑ ↓ move, Enter ticks, Backspace in an empty box drops the last chip, Esc
// or a click elsewhere closes. A page that ticks boxes itself calls
// `ffMulti.paint(el)`. A disabled box can't be ticked (its title says why).
(function () {
  function paint(multi) {
    var box = multi.querySelector('.ff-multi-box');
    var search = multi.querySelector('.ff-multi-search');
    box.querySelectorAll('.ff-multi-chip').forEach(function (c) { c.remove(); });
    multi.querySelectorAll('.ff-multi-item input:checked').forEach(function (input) {
      var chip = document.createElement('span');
      chip.className = 'ff-multi-chip' + ('builtin' in input.dataset ? ' is-builtin' : '');
      chip.textContent = input.dataset.label;
      var x = document.createElement('button');
      x.type = 'button';
      x.textContent = '×';
      x.title = x.ariaLabel = 'Remove ' + input.dataset.label;
      x.dataset.value = input.value;
      chip.appendChild(x);
      box.insertBefore(chip, search);
    });
    search.placeholder = multi.querySelector('.ff-multi-item input:checked') ? '' : multi.dataset.placeholder;
  }

  function items(multi, visible) {
    var all = Array.prototype.slice.call(multi.querySelectorAll('.ff-multi-item'));
    return visible ? all.filter(function (i) { return !i.hidden; }) : all;
  }

  function show(multi, open) {
    multi.querySelector('.ff-multi-list').hidden = !open;
    multi.querySelector('.ff-multi-search').setAttribute('aria-expanded', String(open));
  }

  function narrow(multi) {
    var q = multi.querySelector('.ff-multi-search').value.trim().toLowerCase();
    var first = null;
    items(multi).forEach(function (item) {
      item.hidden = !!q && item.textContent.toLowerCase().indexOf(q) < 0;
      item.classList.remove('is-active');
      if (!item.hidden && !first) first = item;
    });
    if (first && q) first.classList.add('is-active');
    multi.querySelector('.ff-multi-none').hidden = !!first;
    show(multi, true);
  }

  function toggle(multi, input, on) {
    if (input.disabled) return;
    input.checked = on === undefined ? !input.checked : on;
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function multiOf(el) { return el && el.closest && el.closest('.ff-multi'); }

  document.addEventListener('change', function (e) {
    var multi = multiOf(e.target);
    if (multi && e.target.closest('.ff-multi-item')) paint(multi);
  });
  document.addEventListener('focusin', function (e) {
    var multi = multiOf(e.target);
    if (multi && e.target.matches('.ff-multi-search')) narrow(multi);
  });
  document.addEventListener('input', function (e) {
    var multi = multiOf(e.target);
    if (multi && e.target.matches('.ff-multi-search')) narrow(multi);
  });
  document.addEventListener('mousedown', function (e) {
    var multi = multiOf(e.target);
    if (!multi) return;
    var chipX = e.target.closest('.ff-multi-chip button');
    if (chipX) {
      e.preventDefault();
      var input = multi.querySelector('.ff-multi-item input[value="' + CSS.escape(chipX.dataset.value) + '"]');
      if (input) toggle(multi, input, false);
      return;
    }
    // A click on the box (not the list) opens it, and keeps focus in the search.
    if (e.target.closest('.ff-multi-box')) {
      if (e.target !== multi.querySelector('.ff-multi-search')) e.preventDefault();
      multi.querySelector('.ff-multi-search').focus();
      narrow(multi);
    } else if (e.target.closest('.ff-multi-list')) {
      e.preventDefault();                       // keep focus in the search
      var item = e.target.closest('.ff-multi-item');
      if (item) toggle(multi, item.querySelector('input'));
    }
  });
  // The label's own click would tick the box a second time.
  document.addEventListener('click', function (e) {
    if (multiOf(e.target) && e.target.closest('.ff-multi-item')) e.preventDefault();
  });
  document.addEventListener('focusout', function (e) {
    var multi = multiOf(e.target);
    if (multi && !multi.contains(e.relatedTarget)) show(multi, false);
  });
  document.addEventListener('keydown', function (e) {
    var multi = multiOf(e.target);
    if (!multi || !e.target.matches('.ff-multi-search')) return;
    var list = items(multi, true);
    var at = list.findIndex(function (i) { return i.classList.contains('is-active'); });
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!list.length) return;
      show(multi, true);
      var next = list[(at + (e.key === 'ArrowDown' ? 1 : list.length - 1 + (at < 0 ? 1 : 0))) % list.length];
      list.forEach(function (i) { i.classList.toggle('is-active', i === next); });
      next.scrollIntoView({block: 'nearest'});
    } else if (e.key === 'Enter') {
      e.preventDefault();                       // never submits the form
      if (at >= 0) toggle(multi, list[at].querySelector('input'));
    } else if (e.key === 'Backspace' && !e.target.value) {
      var ticked = multi.querySelectorAll('.ff-multi-item input:checked');
      if (ticked.length) toggle(multi, ticked[ticked.length - 1], false);
    } else if (e.key === 'Escape' && !multi.querySelector('.ff-multi-list').hidden) {
      e.preventDefault();                       // close the list, not a dialog
      e.stopPropagation();
      show(multi, false);
    }
  });
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.ff-multi').forEach(paint);
  });

  window.ffMulti = {paint: paint};
})();
