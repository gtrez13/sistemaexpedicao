const toastArea = document.getElementById('toastArea');

function toast(msg, type = "primary") {
  if (!toastArea) return;
  const el = document.createElement("div");
  el.className = `alert alert-${type} shadow-sm`;
  el.style.borderRadius = "14px";
  el.style.marginBottom = "10px";
  el.innerHTML = msg;
  toastArea.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}