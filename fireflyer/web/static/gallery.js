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
function openDsRename(btn){
  var f = document.getElementById('ds-rename-form');
  f.action = '/datasets/' + encodeURIComponent(btn.dataset.name) + '/rename';
  var i = document.getElementById('ds-rename-name');
  i.value = btn.dataset.name;
  document.getElementById('ds-rename-desc').value = btn.dataset.desc || '';
  document.getElementById('ds-rename-dialog').showModal(); i.select();
}

// A dataset's page: the name is edited in place, as a dashboard's is in the
// editor. A click renames it and opens a description box beneath, filled from
// the description the name carries. Leaving both (or Cmd/Ctrl+Enter) saves the two together through the
// rename route — which also rewrites every dashboard using the dataset — and
// the page reloads at the new name. Esc puts both back. A refusal (the name is
// taken, say) shows under the box instead of leaving the page.
// Wired once the page has loaded: this file is linked from <head>, so at
// parse time the title it wires doesn't exist yet.
document.addEventListener('DOMContentLoaded', function dsTitle() {
  var title = document.getElementById('ds-title');
  if (!title) return;
  var name = document.getElementById('ds-title-name');
  var about = document.getElementById('ds-title-about');
  var text = document.getElementById('ds-title-about-text');
  var error = document.getElementById('ds-title-error');
  var oldName = name.dataset.name;
  var oldDesc = name.dataset.description || '';
  var saving = false;

  function open() {
    if (!about.hidden) return;
    text.value = oldDesc;
    error.hidden = true;
    about.hidden = false;
  }
  function revert() {
    name.textContent = oldName;
    text.value = oldDesc;
    about.hidden = true;
    error.hidden = true;
    document.activeElement && document.activeElement.blur();
  }
  function save() {
    var newName = name.textContent.trim() || oldName;
    var newDesc = text.value.trim();
    if (newName === oldName && newDesc === oldDesc) { name.textContent = oldName; about.hidden = true; return; }
    if (saving) return;
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
        about.hidden = false;
        name.focus();
      });
    });
  }

  name.addEventListener('focus', open);
  name.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); text.focus(); }
    else if (e.key === 'Escape') { e.preventDefault(); revert(); }
  });
  text.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); save(); }
    else if (e.key === 'Escape') { e.preventDefault(); revert(); }
  });
  title.addEventListener('focusout', function (e) {
    if (!title.contains(e.relatedTarget)) save();
  });
});
