// The theme: Auto / Light / Dark, one choice for every page (the editor reads
// the same `ffTheme`). Applied now, from <head>, so the page never paints in the
// wrong one; the switch in the topbar is wired once the page has loaded.
(function () {
  var MODES = ['auto', 'light', 'dark'];
  var mode = 'auto';
  try { mode = localStorage.getItem('ffTheme') || 'auto'; } catch (e) { /* storage off: auto */ }
  if (MODES.indexOf(mode) < 0) mode = 'auto';
  function apply() {
    if (mode === 'auto') delete document.documentElement.dataset.ffTheme;
    else document.documentElement.dataset.ffTheme = mode;
    document.querySelectorAll('#theme-switch button').forEach(function (b) {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
    try { localStorage.setItem('ffTheme', mode); } catch (e) { /* not kept */ }
  }
  apply();
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('#theme-switch button').forEach(function (b) {
      b.addEventListener('click', function () { mode = b.dataset.mode; apply(); });
    });
    apply();
  });
})();

// Gallery chrome (dashboards + datasets lists) — a dev tool, exempt from the
// no-JS rule. Native <dialog>s; each opener fills its dialog from the clicked
// row's data-* attributes.
function openAdd(){ document.getElementById('add-dialog').showModal(); }
function openClone(btn){
  var d = document.getElementById('clone-dialog');
  document.getElementById('clone-form').action = '/d/' + btn.dataset.id + '/clone';
  var input = document.getElementById('clone-name');
  input.value = btn.dataset.name + ' (copy)';
  d.showModal(); input.select();
}
function openUpload(){ document.getElementById('upload-dialog').showModal(); }

// A dataset's name and description, as a dashboard's in the editor: the
// topbar shows both (the description's first line, small, under the name), and
// a click opens a card beneath them with a field for each. The topbar follows
// as you type; clicking away or Cmd/Ctrl+Enter saves through the rename route
// — which also rewrites every dashboard using the dataset — and the page
// reloads at the new name. Esc puts both back. A refusal (the name is taken,
// say) shows in the card instead of leaving the page.
// Wired once the page has loaded: this file is linked from <head>, so at
// parse time the title it wires doesn't exist yet.
document.addEventListener('DOMContentLoaded', function dsTitle() {
  var title = document.getElementById('ds-title');
  if (!title) return;
  var head = document.getElementById('ds-title-head');
  var name = document.getElementById('ds-title-name');
  var sub = document.getElementById('ds-title-sub');
  var about = document.getElementById('ds-title-about');
  var nameInput = document.getElementById('ds-title-name-input');
  var text = document.getElementById('ds-title-about-text');
  var error = document.getElementById('ds-title-error');
  var oldName = name.dataset.name;
  var oldDesc = name.dataset.description || '';
  var saving = false;

  function show(n, d) {
    name.textContent = n;
    sub.textContent = d;
    sub.hidden = !d;
  }
  function open() {
    if (!about.hidden) return;
    nameInput.value = oldName;
    text.value = oldDesc;
    error.hidden = true;
    about.hidden = false;
    head.classList.add('is-open');
    nameInput.focus();
    nameInput.select();
  }
  function close() {
    about.hidden = true;
    head.classList.remove('is-open');
  }
  function revert() {
    show(oldName, oldDesc);
    close();
    head.focus();
  }
  function save() {
    if (about.hidden || saving) return;
    var newName = nameInput.value.trim() || oldName;
    var newDesc = text.value.trim();
    if (newName === oldName && newDesc === oldDesc) { show(oldName, oldDesc); close(); return; }
    saving = true;
    var form = new FormData();
    form.append('name', newName);
    form.append('description', newDesc);
    form.append('next', 'detail');
    fetch(name.dataset.action, { method: 'POST', body: form }).then(function (res) {
      if (res.ok) { window.location.href = res.url; return; }
      return res.text().then(function (html) {
        saving = false;
        var box = document.createElement('div');
        box.innerHTML = html;
        error.textContent = (box.textContent || 'Could not save.').trim();
        error.hidden = false;
        nameInput.focus();
      });
    });
  }

  head.addEventListener('click', function () { about.hidden ? open() : save(); });
  head.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); about.hidden ? open() : save(); }
  });
  // The topbar follows the fields as you type.
  nameInput.addEventListener('input', function () { name.textContent = nameInput.value.trim() || oldName; });
  text.addEventListener('input', function () { var d = text.value.trim(); sub.textContent = d; sub.hidden = !d; });
  about.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.preventDefault(); revert(); }
    else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); save(); }
    // Enter in the name moves on to the description, as a form's next field.
    else if (e.key === 'Enter' && e.target === nameInput) { e.preventDefault(); text.focus(); }
  });
  // A click anywhere else saves, as leaving a field does.
  document.addEventListener('mousedown', function (e) {
    if (!about.hidden && !title.contains(e.target)) save();
  });
});

// Times arrive as <time datetime> in UTC — the server doesn't know the viewer's
// zone; show them in it, in the editor's one shape: 2026-10-16 14:52:07.
document.addEventListener('DOMContentLoaded', function(){
  function pad(n){ return String(n).padStart(2, '0'); }
  document.querySelectorAll('time[datetime]').forEach(function(t){
    var d = new Date(t.getAttribute('datetime'));
    if (isNaN(d)) return;
    t.textContent = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  });
});

// The topbar's dropdowns — path, profile, a dataset's versions — are native
// <details>, which stay open until their own summary is clicked again. Close
// one on a click anywhere outside it, or on Esc, so opening anything else (the
// title card, another dropdown) never leaves two open at once.
(function () {
  var MENUS = 'details.ff-pathdd[open], details.ff-profile[open], details.ds-history[open]';
  document.addEventListener('mousedown', function (e) {
    document.querySelectorAll(MENUS).forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') document.querySelectorAll(MENUS).forEach(function (d) { d.open = false; });
  });
})();
