// Mixer Mute Service UI (menus.js): the one "⋮" menu pattern of the page and
// the shared icons. Every item that can be edited, renamed, reordered or
// deleted (desktop keys, shortcuts, program rows, mute groups, light presets,
// cameras, Steuerung keys, templates) has one "⋮" key; its menu lists what
// can be done. Section heads use the same key for rarely used tools.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- icons (24 x 24, drawn with currentColor) --------------------------------
const ICON_DOTS = `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>`;
const ICON_X = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>`;
const ICON_TRASH = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>`;
const ICON_PENCIL = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg>`;
const ICON_COPY = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>`;
const ICON_EDIT_LIST = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 6h10M4 12h10M4 18h6"/><path d="M17 4v8M13 8h8"/></svg>`;
const ICON_CHECK = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>`;
const ICON_LEFT = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>`;
const ICON_RIGHT = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>`;
const ICON_REFRESH = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>`;
const ICON_MOVE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h11M4 12h7M4 17h11"/><path d="M17 9l3 3-3 3"/></svg>`;
const ICON_POWER = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 3v8M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>`;
const ICON_SLIDERS = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M6 4v6M6 14v6M12 4v2M12 10v10M18 4v10M18 18v2"/><circle cx="6" cy="12" r="2"/><circle cx="12" cy="8" r="2"/><circle cx="18" cy="16" r="2"/></svg>`;
const ICON_FADERS_USE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M6 4v16M12 4v16M18 4v16"/><path d="M3.5 9h5M9.5 14h5M15.5 7h5"/></svg>`;

// ---- the "⋮" key -------------------------------------------------------------
// A 44 x 44 px key with the three dots. `key` names what it belongs to
// ("person:<id>", "preset:<id>", ...): lists that are redrawn while the menu
// is open find the new key by it (data-menu-key), and it stays lit.
function menuButton(label, key) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "menu-btn";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", String(isMenuOpenFor(key)));
  if (key) button.dataset.menuKey = key;
  button.innerHTML = ICON_DOTS;
  return button;
}

// The same key as markup (for lists built from HTML strings).
function menuButtonHtml(label, key, extra = "") {
  return `<button type="button" class="menu-btn" title="${escapeAttr(label)}" aria-label="${escapeAttr(label)}" aria-haspopup="menu" aria-expanded="${isMenuOpenFor(key)}" data-menu-key="${escapeAttr(key)}" ${extra}>${ICON_DOTS}</button>`;
}

// ---- one menu open at a time ---------------------------------------------------
// Two kinds: a fixed menu in index.html (#shortcut-menu, the
// section heads' menus; their buttons carry their own handlers) or one built
// from a list of items (openActionMenu). Both sit under the key that opened
// them (above it near the bottom of the window), take arrow keys and Escape,
// and close on a tap elsewhere, a scroll or a resize.
let OPEN_MENU = null;  // { menu, trigger, key }

function isMenuOpenFor(key) {
  return Boolean(key && OPEN_MENU && OPEN_MENU.key === key);
}

// The key the open menu belongs to; after a redraw the new one with its name.
function menuTrigger() {
  if (!OPEN_MENU) return null;
  const { trigger, key } = OPEN_MENU;
  if (trigger.isConnected || !key) return trigger;
  return document.querySelector(`[data-menu-key="${CSS.escape(key)}"]`) || trigger;
}

function placeMenu(menu, trigger) {
  menu.hidden = false;
  const r = trigger.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  const left = Math.min(Math.max(8, r.right - w), innerWidth - w - 8);
  const top = r.bottom + 4 + h > innerHeight - 8 ? Math.max(8, r.top - h - 4) : r.bottom + 4;
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  const first = menu.querySelector("button:not(:disabled)");
  if (first) first.focus({ preventScroll: true });
}

// A fixed menu from index.html.
function openTileMenu(trigger, menu) {
  closeTileMenu();
  // Inside a modal dialog the menu must live in the dialog (top layer).
  const host = trigger.closest("dialog[open]") || document.body;
  if (menu.parentElement !== host) host.appendChild(menu);
  // The backdrop right under the menu takes every tap outside it.
  const backdrop = menuBackdrop();
  if (backdrop.nextSibling !== menu) host.insertBefore(backdrop, menu);
  backdrop.hidden = false;
  const r = trigger.getBoundingClientRect();
  OPEN_MENU = { menu, trigger, key: trigger.dataset.menuKey || null, top: r.top, left: r.left };
  // The dialog closes (Escape, a save, a cancel key): its menu goes with it,
  // else the menu would count as open with its backdrop out of sight.
  if (host !== document.body) {
    const opened = OPEN_MENU;
    host.addEventListener("close", () => { if (OPEN_MENU === opened) closeTileMenu(); }, { once: true });
  }
  menu._trigger = trigger;
  trigger.setAttribute("aria-expanded", "true");
  placeMenu(menu, trigger);
}

// items: [{ label, icon, run(trigger), danger, disabled, title }] and "-" for
// a dividing line. run gets the menu's key (it anchors a delete question).
function openActionMenu(trigger, items) {
  const menu = actionMenuElement();
  menu.replaceChildren();
  for (const item of items) {
    if (!item) continue;
    if (item === "-") {
      const line = document.createElement("div");
      line.className = "menu-sep";
      line.setAttribute("role", "separator");
      menu.appendChild(line);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "menuitem");
    if (item.danger) button.classList.add("danger");
    button.disabled = Boolean(item.disabled);
    if (item.title) button.title = item.title;
    button.innerHTML = item.icon || "";
    const text = document.createElement("span");
    text.textContent = item.label;
    button.appendChild(text);
    button._run = item.run;
    menu.appendChild(button);
  }
  openTileMenu(trigger, menu);
}

// The "⋮" key's tap: open its menu, or close it when it is already open.
function toggleActionMenu(trigger, items) {
  const key = trigger.dataset.menuKey || null;
  if (OPEN_MENU && (OPEN_MENU.trigger === trigger || (key && OPEN_MENU.key === key))) { closeTileMenu(); return; }
  openActionMenu(trigger, typeof items === "function" ? items() : items);
}

function actionMenuElement() {
  let menu = $("action-menu");
  if (menu) return menu;
  menu = document.createElement("div");
  menu.id = "action-menu";
  menu.className = "tile-menu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;
  document.body.appendChild(menu);
  initMenuKeys(menu);
  menu.addEventListener("click", event => {
    const item = event.target.closest("button");
    if (!item || item.disabled || !item._run) return;
    const trigger = menuTrigger();
    closeTileMenu();
    item._run(trigger);
  });
  return menu;
}

function closeTileMenu(refocus = false) {
  if (!OPEN_MENU) return;
  const trigger = menuTrigger();
  const { menu } = OPEN_MENU;
  OPEN_MENU = null;
  menu.hidden = true;
  menuBackdrop().hidden = true;
  for (const key of new Set([trigger, menu._trigger])) {
    if (key) key.setAttribute("aria-expanded", "false");
  }
  if (refocus && trigger && trigger.isConnected) trigger.focus();
}

function initMenuKeys(menu) {
  menu.addEventListener("keydown", event => {
    const items = [...menu.querySelectorAll("button:not(:disabled)")];
    const i = items.indexOf(document.activeElement);
    if (event.key === "Escape") { event.preventDefault(); closeTileMenu(true); }
    else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      items[(i + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    } else if (event.key === "Tab") closeTileMenu();
  });
}

for (const menu of document.querySelectorAll(".tile-menu")) {
  initMenuKeys(menu);
  // Section-head menus: their keys keep their own handlers; the menu closes
  // after the tap (the handler has already run, a delete question already
  // stands at the key's place).
  if (menu.classList.contains("panel-menu")) {
    menu.addEventListener("click", event => {
      if (event.target.closest("button")) closeTileMenu();
    });
  }
}
// The section heads' "⋮" keys (data-panel-menu: the id of their menu).
document.addEventListener("click", event => {
  const trigger = event.target.closest("button[data-panel-menu]");
  if (!trigger) return;
  if (OPEN_MENU && OPEN_MENU.trigger === trigger) closeTileMenu();
  else openTileMenu(trigger, $(trigger.dataset.panelMenu));
});
// A tap elsewhere only closes the menu: it must not also press a key, move
// a fader or start a camera. A transparent backdrop under the open menu
// covers the page, so such a tap lands on the backdrop and nothing below
// gets any event (sliders included: their jump is the browser's own default
// action). It lies under the header, so Panic and the status stay reachable
// with one tap; a tap there also closes the menu. In a modal dialog the
// backdrop goes into the dialog with the menu.
function menuBackdrop() {
  let backdrop = $("menu-backdrop");
  if (backdrop) return backdrop;
  backdrop = document.createElement("div");
  backdrop.id = "menu-backdrop";
  backdrop.className = "menu-backdrop";
  backdrop.hidden = true;
  backdrop.setAttribute("aria-hidden", "true");
  backdrop.addEventListener("click", () => closeTileMenu());
  document.body.appendChild(backdrop);
  return backdrop;
}

// Taps above the backdrop (the header) close the menu and still do their job.
document.addEventListener("pointerdown", event => {
  if (!OPEN_MENU || OPEN_MENU.menu.contains(event.target) || event.target.closest(".menu-backdrop")) return;
  const key = event.target.closest("[data-menu-key]");
  if (OPEN_MENU.trigger.contains(event.target) || (key && OPEN_MENU.key && key.dataset.menuKey === OPEN_MENU.key)) return;
  closeTileMenu();
});
// Any scroll that moves the key (the page, the stage, a list) closes the
// menu; a scroll elsewhere (a song list following the slide) does not.
document.addEventListener("scroll", () => {
  if (!OPEN_MENU) return;
  const trigger = menuTrigger();
  const r = trigger.getBoundingClientRect();
  if (!trigger.isConnected || Math.abs(r.top - OPEN_MENU.top) > 2 || Math.abs(r.left - OPEN_MENU.left) > 2) closeTileMenu();
}, { passive: true, capture: true });
window.addEventListener("resize", () => closeTileMenu());
