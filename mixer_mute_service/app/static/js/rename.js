// Mixer Mute Service UI (rename.js): Renaming in place: "Umbenennen" in an
// item's "⋮" menu (or a tap in a rename mode) turns the name into a text
// field (Enter or leaving the field saves, Escape cancels). Used by Licht,
// Kameras, Steuerung (keys, categories, Alles ausschalten) and the program
// templates.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- rename modes (stage lights, room light, camera presets) ---------------
// A mode makes taps open names instead of switching lights or moving a
// camera. Only one runs at a time, and it never stays on by accident: it
// ends with "Fertig", when another mode starts, when its section is folded,
// and after RENAME_MODE_MS without a rename (so a later tap on a room light
// switches it again).
const RENAME_MODE_MS = 30000;
let RENAME_MODE = null;  // { box, end, timer }

// Starts a mode for `box` (the element whose names are tapped); end() puts
// the page back to normal.
function startRenameMode(box, end) {
  endRenameMode();
  RENAME_MODE = { box, end, timer: 0 };
  touchRenameMode();
}

function touchRenameMode() {
  if (!RENAME_MODE) return;
  clearTimeout(RENAME_MODE.timer);
  RENAME_MODE.timer = setTimeout(endRenameMode, RENAME_MODE_MS);
}

function endRenameMode() {
  const mode = RENAME_MODE;
  if (!mode) return;
  RENAME_MODE = null;
  clearTimeout(mode.timer);
  mode.end();
}

function renameModeFor(box) {
  return Boolean(RENAME_MODE && RENAME_MODE.box === box);
}

document.addEventListener("click", event => {
  const head = event.target.closest(".section-toggle");
  if (head && RENAME_MODE && head.closest(".panel")?.contains(RENAME_MODE.box)) endRenameMode();
});

// Show a text field in `slot` (its other contents are hidden meanwhile).
// save(name) gets the trimmed new name; an empty name is passed on too when
// `allowEmpty` (meaning: back to the built-in name, shown as placeholder).
function startInlineRename(slot, current, { save, placeholder = "", maxLength = 40, allowEmpty = false }) {
  if (slot.querySelector(":scope > input.inline-rename")) return;
  const hidden = [...slot.children].filter(child => !child.hidden);
  hidden.forEach(child => { child.hidden = true; });
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-input inline-rename";
  input.value = current;
  input.placeholder = placeholder;
  input.maxLength = maxLength;
  input.autocomplete = "off";
  input.setAttribute("aria-label", `Neuer Name für ${current || placeholder}`);
  let done = false;
  const finish = async keep => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    input.remove();
    hidden.forEach(child => { child.hidden = false; });
    if (keep && value !== current && (value || allowEmpty)) await save(value);
    touchRenameMode();  // a rename mode keeps running while names are changed
  };
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); finish(true); }
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  // Taps in the field must not reach the key or tile around it.
  for (const type of ["click", "pointerdown"]) input.addEventListener(type, event => event.stopPropagation());
  slot.appendChild(input);
  input.focus();
  input.select();
}

// Saves a name with one request; toast on failure. Returns whether it worked.
async function saveName(method, path, name, label) {
  const result = await api(method, path, { name });
  if (!result.ok || result.data.ok === false) {
    toast(`${label}: ${result.data.reason || "HTTP " + result.status}`, false);
    return false;
  }
  return true;
}
