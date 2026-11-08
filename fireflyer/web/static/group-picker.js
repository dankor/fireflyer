// The group dropdown (portal mode): its personal | shared switch shows one list
// at a time — the view-as menu's roles | users switch, on every page that has
// the picker. Editor chrome, like the rest of static/.
document.addEventListener('DOMContentLoaded', () => {
  const picker = document.getElementById('ff-group');
  if (!picker) return;
  picker.querySelectorAll('.ff-group-kinds button').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      picker.querySelectorAll('.ff-group-kinds button').forEach(b => b.classList.toggle('active', b === btn));
      picker.querySelectorAll('.ff-group-list').forEach(list => { list.hidden = list.dataset.kind !== btn.dataset.kind; });
    });
  });
});
