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
