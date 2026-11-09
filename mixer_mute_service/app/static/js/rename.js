// Mixer Mute Service UI (rename.js): Renaming in place: a pencil key next to
// a name turns the name into a text field (Enter or leaving the field saves,
// Escape cancels). Used by Licht, Steuerung (categories, Alles ausschalten)
// and the program templates.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// The pencil: a 44 x 44 px key (style: button.rename-btn).
function renamePencil(label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "rename-btn";
  button.innerHTML = "&#9998;";
  button.title = `${label} umbenennen`;
  button.setAttribute("aria-label", button.title);
  return button;
}

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
