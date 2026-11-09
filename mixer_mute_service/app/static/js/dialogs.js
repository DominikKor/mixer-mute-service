// Mixer Mute Service UI (dialogs.js): Delete confirmation popup.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Delete confirmation -------------------------------------------------
// Instead of the browser's confirm() box, the delete key grows into a small
// popup at its own position: the question, "Abbrechen" and "Löschen". The
// popup lives on <body>, so lists that re-render every second cannot remove
// it, and scrolls with the page. Asked from inside an open modal dialog
// (e.g. "Ersetzen" in the channel settings), it lives in that dialog instead:
// a modal dialog sits in the browser's top layer, above everything else on
// the page, and leaves the rest of the page unclickable. A click elsewhere, Escape or a change of the
// window width (iPad turned) cancels; height-only resizes (Safari's address
// bar) do not.
let DELETE_CONFIRM = null;

function closeDeleteConfirm(confirmed) {
  const state = DELETE_CONFIRM;
  if (!state) return;
  DELETE_CONFIRM = null;
  document.removeEventListener("pointerdown", state.onOutside, true);
  document.removeEventListener("keydown", state.onKey, true);
  window.removeEventListener("resize", state.onResize);
  state.pop.remove();
  if (state.anchor.isConnected) state.anchor.focus({ preventScroll: true });
  state.resolve(confirmed);
}

// Resolves true when "Löschen" is pressed, false otherwise.
function confirmDelete(anchor, question, okLabel = "Löschen") {
  closeDeleteConfirm(false);
  return new Promise(resolve => {
    const pop = document.createElement("div");
    pop.className = "delete-confirm";
    pop.setAttribute("role", "alertdialog");
    const text = document.createElement("div");
    text.className = "delete-confirm-text";
    text.id = "delete-confirm-text";
    text.textContent = question;
    pop.setAttribute("aria-labelledby", text.id);
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Abbrechen";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "key-mute";
    ok.textContent = okLabel;
    const actions = document.createElement("div");
    actions.className = "delete-confirm-actions";
    actions.append(cancel, ok);
    pop.append(text, actions);
    const dialog = anchor.closest("dialog[open]");
    // In a dialog: fixed to the window, so the dialog's own scrolling or
    // clipping cannot hide it.
    if (dialog) pop.style.position = "fixed";
    (dialog || document.body).appendChild(pop);
    // The dialog closed meanwhile: the question goes with it.
    if (dialog) dialog.addEventListener("close", () => {
      if (DELETE_CONFIRM && DELETE_CONFIRM.pop === pop) closeDeleteConfirm(false);
    }, { once: true });

    // Right edge and top on the delete key (it ends its row), kept on screen.
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight, margin = 8;
    const left = Math.min(Math.max(margin, r.right - w), innerWidth - w - margin);
    const top = Math.min(Math.max(margin, r.top), innerHeight - h - margin);
    pop.style.left = `${left + (dialog ? 0 : scrollX)}px`;
    pop.style.top = `${top + (dialog ? 0 : scrollY)}px`;
    // Start at the key's size and place, then grow into the popup.
    pop.style.transformOrigin = `${r.left + r.width / 2 - left}px ${r.top + r.height / 2 - top}px`;
    pop.style.transform = `scale(${Math.max(r.width / w, .1)}, ${Math.max(r.height / h, .1)})`;
    pop.style.opacity = "0.4";
    pop.getBoundingClientRect();
    pop.style.transform = "none";
    pop.style.opacity = "1";

    const state = {
      pop, anchor, resolve,
      onOutside: (e) => { if (!pop.contains(e.target)) closeDeleteConfirm(false); },
      onKey: (e) => { if (e.key === "Escape") { e.preventDefault(); closeDeleteConfirm(false); } },
      width: innerWidth,
      onResize: () => { if (innerWidth !== state.width) closeDeleteConfirm(false); },
    };
    DELETE_CONFIRM = state;
    cancel.addEventListener("click", () => closeDeleteConfirm(false));
    ok.addEventListener("click", () => closeDeleteConfirm(true));
    document.addEventListener("pointerdown", state.onOutside, true);
    document.addEventListener("keydown", state.onKey, true);
    window.addEventListener("resize", state.onResize);
    cancel.focus({ preventScroll: true });
  });
}
