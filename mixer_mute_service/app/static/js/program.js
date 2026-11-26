// Mixer Mute Service UI (program.js): Programm: the cue list.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

let PROGRAM_ITEMS = [];
// The empty list's help text (index.html), shown again after a failed load.
const PROGRAM_EMPTY_TEXT = $("program-empty").textContent;
let PROGRAM_GROUPS = {};
let PROGRAM_BUTTONS = [];
let PROGRAM_BUTTON_ALIASES = {};
let PROGRAM_BUTTON_CATEGORIES = {};
let PROGRAM_DESKTOP_PROFILES = [];
let PROGRAM_SHORTCUTS = [];        // [{id, title, ...actions}] from /shortcuts
// Preset choices per camera for the rows' camera line: {cam1: [{value, label}], ...}
let PROGRAM_CAMERA_PRESETS = { cam1: [], cam2: [] };
let PROGRAM_CAMERA_SEQ = null;  // last camera outcome already reported
let PROGRAM_LIGHT_SCENES = [];     // [{id, name}] from /lights/scenes
let PROGRAM_OBS_SCENES = null;     // scene names from OBS; null while OBS was not asked or not reachable
let PROGRAM_DRAFT = false;
let PROGRAM_STATUS = { current_id: null, next_id: null };
const PROGRAM_BUSY = new Set();     // play / clone in progress, per item
const PROGRAM_SAVING = new Set();   // a save is in flight, per item
const PROGRAM_RESAVE = new Set();   // another edit arrived meanwhile
// Hide key: shows what a click does, like the title: struck through while the
// row is shown (click hides it), an open eye once hidden (click shows it again).
const ICON_EYE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
const ICON_EYE_OFF = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7c2 0 3.8.7 5.2 1.6M22 12s-3.6 7-10 7c-2 0-3.8-.7-5.2-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2M3 3l18 18"/></svg>`;
const PROGRAM_FIELDS = [
  "time", "title", "person", "group", "playback", "translation", "lyrics", "song", "light",
  "cam1_preset", "cam2_preset", "camera_wait_seconds", "cam1_scene", "cam2_scene", "desktop", "beamer",
];
const NO_PROGRAM_BEAMER = "Beamer bleibt";
// Hide runs before the row shows anything new, unhide after it (app/api/program.py).
const PROGRAM_BEAMER_OPTIONS = [{ value: "hide", label: "Beamer hide" }, { value: "unhide", label: "Beamer unhide" }];
const PROGRAM_CAMERAS = [["cam1", "Cam 1"], ["cam2", "Cam 2"]];
// Field editors: the program rows and the shortcut dialog share their keys.
const PROGRAM_EDITORS = "#program-items, #shortcut-editor";
function onProgramEditors(type, handler) {
  document.addEventListener(type, event => {
    if (event.target.closest && event.target.closest(PROGRAM_EDITORS)) handler(event);
  });
}

function formatProgramDelay(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const value = Number(seconds);
  const absolute = Math.abs(value);
  const hours = Math.floor(absolute / 3600);
  const minutes = Math.floor((absolute % 3600) / 60);
  const remainder = absolute % 60;
  const body = hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  return `${value > 0 ? "+" : value < 0 ? "−" : "±"}${body}`;
}

function nextProgramTime() {
  const previousTime = PROGRAM_ITEMS.length ? PROGRAM_ITEMS[PROGRAM_ITEMS.length - 1].time : "";
  const nextTime = new Date();
  const parsedTime = /^(\d{2}):(\d{2})$/.exec(previousTime);
  if (parsedTime) nextTime.setHours(Number(parsedTime[1]), Number(parsedTime[2]), 0, 0);
  nextTime.setMinutes(nextTime.getMinutes() + 5);
  return `${String(nextTime.getHours()).padStart(2, "0")}:${String(nextTime.getMinutes()).padStart(2, "0")}`;
}

function programCell(row, label, content, className = "") {
  const cell = document.createElement("td");
  cell.dataset.label = label;
  if (className) cell.className = className;
  cell.appendChild(content);
  row.appendChild(cell);
}

function programInput(field, label, value, type, maxLength) {
  const input = document.createElement("input");
  input.type = type;
  input.dataset.programField = field;
  input.setAttribute("aria-label", label);
  if (maxLength) input.maxLength = maxLength;
  if (field === "time") {
    input.inputMode = "text";
    input.placeholder = "HH:MM";
    input.autocomplete = "off";
    input.setAttribute("pattern", "(?:[01]\\d|2[0-3]):[0-5]\\d");
  }
  input.value = value || "";
  return input;
}

// The options of a select are kept with their signature, so a re-render only
// rebuilds them when the list itself changed.
function setSelectOptions(select, options, placeholder, value) {
  const all = [{ value: "", label: placeholder }, ...options];
  if (value && !options.some(option => option.value === value)) {
    all.push({ value, label: `Nicht verfügbar: ${value}` });
  }
  const sig = JSON.stringify(all);
  if (select.dataset.optionsSig !== sig) {
    select.dataset.optionsSig = sig;
    select.replaceChildren(...all.map(option => {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      return node;
    }));
  }
  select.value = value || "";
}

function programSelect(field, label, value, options, placeholder) {
  const select = document.createElement("select");
  select.dataset.programField = field;
  select.setAttribute("aria-label", label);
  setSelectOptions(select, options, placeholder, value);
  return select;
}

// A row without a group leaves the mutes alone and only runs its actions.
// Such a row is marked red (field and summary chip): usually a group is
// wanted, so a forgotten one should stand out. Shortcuts are not marked.
const NO_PROGRAM_GROUP = "Keine Mute Gruppe";
// A row without a light scene leaves the light as it is.
const NO_PROGRAM_LIGHT = "Licht bleibt";

function programLightOptions(value) {
  const options = PROGRAM_LIGHT_SCENES.map(scene => ({ value: scene.id, label: scene.name }));
  if (value && !options.some(option => option.value === value)) options.push({ value, label: "Gelöschtes Preset" });
  return options;
}

function programGroupOptions() {
  return Object.keys(PROGRAM_GROUPS).map(group => ({ value: group, label: group }));
}

// Playback, Übersetzung and Liedtext open a file (Liedtext: or show a song).
// Rows saved with a key or a desktop profile there keep it (it still runs)
// until it is removed.
const ICON_FILE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/></svg>`;
const ICON_SONG = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>`;
const ICON_CLEAR = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

function buildProgramAction(field, label, item) {
  const link = document.createElement("div");
  link.className = "program-action-link";
  link.dataset.actionField = field;
  const control = document.createElement("input");
  control.type = "hidden";
  control.dataset.programField = field;
  control.value = item[field] || "";
  link.appendChild(control);
  const choose = document.createElement("div");
  choose.className = "program-action-choose";
  if (field === "lyrics") {
    link.appendChild(buildSongPicker(item.song));
    choose.appendChild(programIconButton("song", ICON_SONG, "Lied", "Lied aus der Bibliothek wählen"));
  }
  choose.appendChild(programIconButton("file", ICON_FILE, "Datei", `${label}: Datei oder Pfad angeben`));
  const current = document.createElement("div");
  current.className = "program-action-current";
  current.appendChild(Object.assign(document.createElement("span"), { className: "program-action-legacy" }));
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "program-action-clear";
  clear.dataset.actionClear = field;
  clear.innerHTML = ICON_CLEAR;
  clear.title = `${label} entfernen`;
  clear.setAttribute("aria-label", clear.title);
  link.append(choose, buildFileAction(label, item[field]), current, clear);
  syncProgramAction(link);
  return link;
}

function programIconButton(pick, icon, text, title) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "program-icon-btn";
  button.dataset.actionPick = pick;
  button.innerHTML = `${icon}<span>${text}</span>`;
  button.title = title;
  return button;
}

// Empty: the choice keys. A song, a file or an older key: that, with ✕ to remove it.
function syncProgramAction(link) {
  const control = link.querySelector(":scope > [data-program-field]");
  const value = control.value;
  const songInput = link.querySelector('[data-program-field="song"]');
  const song = songInput ? songInput.value : "";
  const legacy = !!value && !isFileAction(value) && value !== FILE_CHOOSE;
  link.querySelector(".program-action-choose").hidden = !!(value || song);
  const picker = link.querySelector(".song-picker");
  if (picker) picker.hidden = !song;
  link.querySelector(".program-action-current").hidden = !legacy;
  link.querySelector(".program-action-legacy").textContent = legacy ? `${programLegacyLabel(value)} (bisher)` : "";
  link.querySelector(".program-action-clear").hidden = !(value || song);
  syncFileAction(control);
}

function pickProgramAction(button) {
  const link = button.closest(".program-action-link");
  const control = link.querySelector(":scope > [data-program-field]");
  const songInput = link.querySelector('[data-program-field="song"]');
  if (button.dataset.actionPick === "file") {
    control.value = FILE_CHOOSE;
    syncProgramAction(link);
    link.querySelector(".file-path").focus();
    return;
  }
  openSongSearch(button, song => {
    if (!song) return;
    songInput.value = song.id;
    control.value = "";  // a song or a file, not both
    syncSongPicker(link.querySelector(".song-picker"));
    syncProgramAction(link);
    const title = button.closest("tr").querySelector('[data-program-field="title"]');
    if (title && !title.value.trim()) title.value = song.title;
    songInput.dispatchEvent(new Event("change", { bubbles: true }));
  }, false);
}

function clearProgramAction(button) {
  const link = button.closest(".program-action-link");
  const control = link.querySelector(":scope > [data-program-field]");
  const songInput = link.querySelector('[data-program-field="song"]');
  control.value = "";
  if (songInput) {
    songInput.value = "";
    syncSongPicker(link.querySelector(".song-picker"));
  }
  syncProgramAction(link);
  control.dispatchEvent(new Event("change", { bubbles: true }));
}

// ---- A select with a single choice becomes a switch ------------------------
// A dropdown with one entry (e.g. one light scene) is a key that
// turns it on and off; the select stays (hidden) and holds the value.

const SINGLE_CHOICE_ICONS = {
  light: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg>`,
  group: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6"/></svg>`,
};

function syncSingleChoice(select) {
  if (select.dataset.programCamera || select.dataset.obsScene) return;
  const real = [...select.options].filter(option => option.value);
  let toggle = select.nextElementSibling && select.nextElementSibling.classList.contains("program-single") ? select.nextElementSibling : null;
  if (real.length > 1) {
    select.hidden = false;
    if (toggle) toggle.hidden = true;
    return;
  }
  if (!toggle) {
    toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "program-single";
    select.after(toggle);
  }
  select.hidden = true;
  toggle.hidden = false;
  const option = real[0];
  const icon = SINGLE_CHOICE_ICONS[select.dataset.programField] || "";
  const text = option ? option.textContent : select.options[0].textContent;
  toggle.innerHTML = `${icon}<span></span>`;
  toggle.querySelector("span").textContent = text;
  toggle.disabled = !option;
  const on = !!option && select.value === option.value;
  toggle.setAttribute("aria-pressed", String(on));
  toggle.title = !option ? "Nichts zur Auswahl" : on ? `${text}: aktiv (antippen: entfernen)` : `${text} verwenden`;
}

function toggleSingleChoice(toggle) {
  const select = toggle.previousElementSibling;
  const option = [...select.options].find(entry => entry.value);
  if (!option) return;
  select.value = select.value === option.value ? "" : option.value;
  syncSingleChoice(select);
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function programLegacyLabel(value) {
  if (value.startsWith("desktop:")) {
    const profile = PROGRAM_DESKTOP_PROFILES.find(p => `desktop:${p.id}` === value);
    return `Desktop · ${profile ? profile.title : value.slice(8)}`;
  }
  // An old duplicate key ID (catalog v10) shows as the key it now runs.
  const id = PROGRAM_BUTTON_ALIASES[value] || value;
  const button = PROGRAM_BUTTONS.find(b => b.id === id);
  if (!button) return value;
  const category = PROGRAM_BUTTON_CATEGORIES[button.category_id];
  const action = button.native_action || {};
  // A camera preset: its number before the name ("CAM1 · 3 · Kanzel").
  const label = action.type === "visca-preset"
    ? `${action.camera.toUpperCase()} · ${action.preset} · ${cameraPresetLabel(button.label)}` : button.label;
  return category ? `${category} · ${label}` : label;
}

// ---- OBS scene of a row ---------------------------------------------------
// Replaces the automatic switch to the moved camera (app/camera_program.py);
// without camera presets OBS switches to it at once.

const NO_OBS_SCENE = "OBS · automatisch";

function programObsOptions(value) {
  const options = (PROGRAM_OBS_SCENES || []).map(scene => ({ value: scene, label: `OBS · ${scene}` }));
  if (value && !options.some(option => option.value === value)) {
    options.push({ value, label: PROGRAM_OBS_SCENES ? `OBS · ${value} (fehlt in OBS)` : `OBS · ${value}` });
  }
  return options;
}

async function loadProgramObsScenes() {
  const result = await api("GET", "/program/obs-scenes", undefined, { timeout: 10000 });
  const scenes = result.ok && result.data && Array.isArray(result.data.scenes) ? result.data.scenes : null;
  if (JSON.stringify(scenes) === JSON.stringify(PROGRAM_OBS_SCENES)) return;
  PROGRAM_OBS_SCENES = scenes;
  renderProgramItems();
}

// ---- Desktop profiles of a row -----------------------------------------------
// Only the chosen profiles show, in the order they run (1., 2., …), each with
// ✕ to remove it; "+" opens the list of the others to add one at the end.

const ICON_SHORTCUT_SAVE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M9 20h6M12 16v4M10 7.5l4 2.5-4 2.5z"/></svg>`;
const ICON_SHORTCUT_LOAD = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v11M7.5 9.5L12 14l4.5-4.5"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></svg>`;
const ICON_EARLIER = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>`;
const ICON_PLUS = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>`;

function buildProgramDesktop(item) {
  const box = document.createElement("div");
  box.className = "program-desktop";
  const hidden = document.createElement("input");
  hidden.type = "hidden";
  hidden.dataset.programField = "desktop";
  hidden.value = (item.desktop || []).join(",");
  const chosen = document.createElement("div");
  chosen.className = "program-desktop-chosen";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "program-desktop-add";
  add.setAttribute("aria-expanded", "false");
  const pick = document.createElement("div");
  pick.className = "program-desktop-pick";
  pick.hidden = true;
  const note = document.createElement("span");
  note.className = "program-desktop-note";
  chosen.append(add, note);
  box.append(hidden, chosen, pick);
  syncProgramDesktop(box);
  return box;
}

function syncProgramDesktop(box) {
  if (!box) return;
  const chosen = box.querySelector("[data-program-field]").value.split(",").filter(Boolean);
  const profiles = PROGRAM_DESKTOP_PROFILES.map(p => ({ id: p.id, title: p.title }));
  const titleOf = id => (profiles.find(p => p.id === id) || {}).title;
  const rest = profiles.filter(p => !chosen.includes(p.id));
  const sig = JSON.stringify([chosen.map(id => [id, titleOf(id) || null]), rest]);
  const add = box.querySelector(".program-desktop-add");
  const pick = box.querySelector(".program-desktop-pick");
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    const chips = chosen.map((id, index) => {
      const chip = document.createElement("span");
      chip.className = "program-desktop-chip" + (titleOf(id) ? "" : " missing");
      if (index > 0) {
        const earlier = document.createElement("button");
        earlier.type = "button";
        earlier.className = "program-desktop-earlier";
        earlier.dataset.choiceEarlier = id;
        earlier.innerHTML = ICON_EARLIER;
        earlier.title = `${titleOf(id) || id} früher ausführen (vor ${titleOf(chosen[index - 1]) || chosen[index - 1]})`;
        earlier.setAttribute("aria-label", earlier.title);
        chip.appendChild(earlier);
      }
      const label = document.createElement("span");
      label.className = "program-desktop-chip-label";
      label.textContent = `${index + 1}. ${titleOf(id) || `Gelöscht (${id})`}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "program-desktop-remove";
      remove.dataset.choiceRemove = id;
      remove.innerHTML = ICON_CLEAR;
      remove.title = `${titleOf(id) || id} entfernen`;
      remove.setAttribute("aria-label", remove.title);
      chip.append(label, remove);
      return chip;
    });
    box.querySelector(".program-desktop-chosen").replaceChildren(...chips, add, box.querySelector(".program-desktop-note"));
    pick.replaceChildren(...rest.map(profile => {
      const key = document.createElement("button");
      key.type = "button";
      key.className = "program-desktop-key";
      key.dataset.choiceAdd = profile.id;
      key.textContent = profile.title;
      key.title = `${profile.title} hinzufügen (läuft als ${chosen.length + 1}.)`;
      return key;
    }));
  }
  add.innerHTML = `${ICON_PLUS}<span>${chosen.length ? "Profil" : "Desktop-Profil"}</span>`;
  // Nothing left to add: a visible note instead of a dead key.
  add.hidden = !rest.length;
  box.querySelector(".program-desktop-note").textContent = !profiles.length ? "Keine Desktop-Profile – unter Desktop anlegen"
    : !rest.length ? "Alle Desktop-Profile gewählt" : "";
  add.title = "Desktop-Profil hinzufügen";
  add.setAttribute("aria-label", add.title);
  if (!rest.length) pick.hidden = true;
  add.setAttribute("aria-expanded", String(!pick.hidden));
}

function toggleProgramDesktopPick(add) {
  const pick = add.closest(".program-desktop").querySelector(".program-desktop-pick");
  pick.hidden = !pick.hidden;
  add.setAttribute("aria-expanded", String(!pick.hidden));
  if (!pick.hidden) pick.querySelector("button")?.focus({ preventScroll: true });
}

function changeProgramDesktop(button) {
  const box = button.closest(".program-desktop");
  const hidden = box.querySelector("[data-program-field]");
  const chosen = hidden.value.split(",").filter(Boolean);
  if (button.dataset.choiceAdd) {
    chosen.push(button.dataset.choiceAdd);
    box.querySelector(".program-desktop-pick").hidden = true;
  }
  let next = button.dataset.choiceRemove ? chosen.filter(id => id !== button.dataset.choiceRemove) : chosen;
  const earlier = button.dataset.choiceEarlier;
  const at = earlier ? next.indexOf(earlier) : -1;
  if (at > 0) next = [...next.slice(0, at - 1), earlier, next[at - 1], ...next.slice(at + 1)];
  hidden.value = [...new Set(next)].join(",");
  syncProgramDesktop(box);
  // Keep the focus on the moved profile's key (or on "+").
  const focus = (earlier && box.querySelector(`[data-choice-earlier="${CSS.escape(earlier)}"]`))
    || (earlier && box.querySelector(`[data-choice-remove="${CSS.escape(earlier)}"]`))
    || box.querySelector(".program-desktop-add:not([hidden])")
    // The last profile added: "+" is gone, so the new profile's ✕.
    || (button.dataset.choiceAdd && box.querySelector(`[data-choice-remove="${CSS.escape(button.dataset.choiceAdd)}"]`));
  if (focus) focus.focus({ preventScroll: true });
  hidden.dispatchEvent(new Event("change", { bubbles: true }));
}

// ---- "Shortcut laden" ----------------------------------------------------------
// Copies a shortcut's fields into a row's own (app/program_items.py
// apply_shortcut): a copy, the fields stay visible and can be changed. Time,
// title, person and song stay the row's; with a song the shortcut's Liedtext
// file is left out (a song or a file, not both).

const SHORTCUT_FIELDS = [
  "group", "playback", "translation", "lyrics", "light", "cam1_preset", "cam2_preset",
  "camera_wait_seconds", "desktop", "cam1_scene", "cam2_scene", "beamer",
];
// The row the picker fills: its id, or "" for the new-row draft. Found again
// when a shortcut is chosen (the list may have been redrawn meanwhile).
let SHORTCUT_LOAD_TARGET = null;

function shortcutLoadRow(target) {
  if (target === null) return null;
  return target ? $("program-items").querySelector(`tr[data-program-id="${CSS.escape(target)}"]`)
    : $("program-items").querySelector(".program-draft-row");
}

function openShortcutLoad(row) {
  if (!row || !row.isConnected) return;
  SHORTCUT_LOAD_TARGET = row.dataset.programId || "";
  const title = row.querySelector('[data-program-field="title"]').value.trim();
  $("shortcut-load-title").textContent = title ? `Shortcut laden: ${title}` : "Shortcut laden";
  const list = $("shortcut-load-list");
  list.replaceChildren(...PROGRAM_SHORTCUTS.map(shortcut => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "shortcut-load-item";
    button.dataset.shortcutLoad = shortcut.id;
    const name = document.createElement("span");
    name.className = "shortcut-load-name";
    name.textContent = shortcut.title;
    const chips = document.createElement("span");
    chips.className = "program-sum-chips";
    renderProgramChips(chips, programChips({ ...shortcut, song: null }));
    button.append(name, chips);
    return button;
  }));
  $("shortcut-load-empty").hidden = PROGRAM_SHORTCUTS.length > 0;
  $("shortcut-load-dialog").showModal();
  (list.querySelector("button") || $("shortcut-load-cancel")).focus();
}

function loadShortcutIntoRow(row, shortcut) {
  if (!row || !row.isConnected) return;
  const current = readProgramRow(row);
  const loaded = { ...current };
  for (const field of SHORTCUT_FIELDS) {
    const value = shortcut[field];
    loaded[field] = Array.isArray(value) ? [...value] : (value ?? null);
  }
  if (current.song) loaded.lyrics = null;
  fillProgramFields(row, loaded, true);
  if (row.dataset.programId) {
    // Saved like any edit of the row; the toast once it is saved (a failed
    // save has its own toast and shows what the service has).
    updateProgramItem(row).then(saved => { if (saved) toast(`Shortcut „${shortcut.title}“ geladen`, true); });
  } else {
    renderProgramSummary(row, { ...loaded, title: loaded.title || "Neuer Programmpunkt" });
    toast(`Shortcut „${shortcut.title}“ geladen – mit ✓ speichern`, true);
  }
}

// ---- Camera presets of a row (app/camera_program.py) ----------------------
// Each row may name a preset per camera. When the row starts, only the camera
// that is not live in OBS moves; OBS switches to it after the wait, then the
// other camera gets its preset (without switching).

function programCameraPresets(cameraButtons) {
  const presets = {};
  for (const [cam, camLabel] of PROGRAM_CAMERAS) {
    const named = {};
    for (const button of cameraButtons) {
      const action = button.native_action || {};
      if (action.type === "visca-preset" && action.camera === cam) {
        named[action.preset] = (button.label || "").replace(/^\s*cam\s*\d\s*(pset\s*)?/i, "").trim();
      }
    }
    presets[cam] = Array.from({ length: 16 }, (_, i) => i + 1).map(n => ({
      value: String(n), label: `${camLabel} · ${n} · ${named[n] || "ohne Namen"}`,
    }));
  }
  return presets;
}

function buildProgramCamera(item) {
  const box = document.createElement("div");
  box.className = "program-camera";
  for (const [cam, camLabel] of PROGRAM_CAMERAS) {
    const field = `${cam}_preset`;
    const placeholder = `${camLabel} · kein Preset`;
    const select = programSelect(field, `${camLabel} Preset`, item[field] ? String(item[field]) : "", PROGRAM_CAMERA_PRESETS[cam], placeholder);
    select.dataset.programCamera = cam;
    select.dataset.placeholder = placeholder;
    box.appendChild(select);
    // Always a dropdown: the scene list comes from OBS and changes.
    const sceneField = `${cam}_scene`;
    const scene = programSelect(sceneField, `${camLabel} OBS-Szene`, item[sceneField] || "", programObsOptions(item[sceneField]), NO_OBS_SCENE);
    scene.dataset.obsScene = cam;
    scene.title = `Auf diese Szene schaltet OBS, wenn ${camLabel} ihr Preset angefahren hat; automatisch: „${camLabel} Live“`;
    box.appendChild(scene);
  }
  const wait = document.createElement("label");
  wait.className = "program-camera-wait";
  wait.title = "So lange fährt die Kamera, bevor OBS auf sie umschaltet";
  const input = programInput("camera_wait_seconds", "Umschalten nach Sekunden", item.camera_wait_seconds ?? 5, "text", 4);
  input.inputMode = "decimal";
  input.placeholder = "5";
  wait.append("Umschalten nach", input, "s");
  box.appendChild(wait);
  return box;
}

// Report a camera outcome once (the switch runs in the background).
function noteCameraProgram(camera) {
  if (!camera || typeof camera.seq !== "number") return;
  const first = PROGRAM_CAMERA_SEQ === null;
  if (!first && camera.seq > PROGRAM_CAMERA_SEQ && ["done", "aborted", "failed", "skipped"].includes(camera.state)) {
    toast(`Kamera: ${camera.message}`, camera.state === "done" || camera.state === "skipped");
  }
  if (first || camera.seq > PROGRAM_CAMERA_SEQ) PROGRAM_CAMERA_SEQ = camera.seq;
}

async function checkCameraProgram() {
  const status = await api("GET", "/program/status");
  if (status.ok && status.data) noteCameraProgram(status.data.camera);
}

// kind: "program" (a row of the list), "draft" (a new row) or "shortcut"
// (the fields of a shortcut, edited in its dialog).
function buildProgramRow(item, draft = false, kind = draft ? "draft" : "program") {
  const row = document.createElement("tr");
  const shortcut = kind === "shortcut";
  if (draft) row.className = "program-draft-row program-open";
  else if (shortcut) row.className = "program-open program-shortcut-row";
  else row.dataset.programId = item.id;

  if (!shortcut) programCell(row, "", buildProgramSummary(draft), "program-summary-cell");
  const edit = (label, content, name) => programCell(row, label, content, `program-edit-cell pc-${name}`);
  if (!shortcut) edit("Uhrzeit", programInput("time", "Uhrzeit", item.time, "text", 5), "time");
  edit(shortcut ? "Name" : "Titel", programInput("title", "Titel", item.title, "text", 120), "title");
  if (!shortcut) edit("Person", programInput("person", "Person", item.person, "text", 80), "person");
  edit("Mute Gruppe", programSelect("group", "Mute Gruppe", item.group, programGroupOptions(), NO_PROGRAM_GROUP), "group");
  edit("Licht", programSelect("light", "Licht-Preset", item.light, programLightOptions(item.light), NO_PROGRAM_LIGHT), "light");

  for (const [field, label] of [["playback", "Playback"], ["translation", "Übersetzung"], ["lyrics", "Liedtext"]]) {
    edit(label, buildProgramAction(field, label, item), field);
  }

  edit("Kamera-Presets · nur auf der Kamera, die nicht live ist", buildProgramCamera(item), "camera");
  edit("Desktop · läuft in dieser Reihenfolge", buildProgramDesktop(item), "desktop");
  edit("Beamer", programSelect("beamer", "Beamer", item.beamer, PROGRAM_BEAMER_OPTIONS, NO_PROGRAM_BEAMER), "beamer");
  if (shortcut) {
    row.querySelectorAll("select[data-program-field]").forEach(syncSingleChoice);
    return row;
  }

  const actions = document.createElement("div");
  actions.className = "program-actions";
  // Left to right: Play (the big key), hide, "⋮"; a draft row: "Shortcut
  // laden" (a row has it in its "⋮"), save, cancel.
  if (draft) {
    const load = document.createElement("button");
    load.type = "button";
    load.className = "program-load-shortcut";
    load.dataset.programAction = "load-shortcut";
    load.innerHTML = `${ICON_SHORTCUT_LOAD}<span>Shortcut laden</span>`;
    load.title = "Einstellungen eines Shortcuts übernehmen (Uhrzeit, Titel, Person und Lied bleiben)";
    actions.appendChild(load);
  }
  const primary = document.createElement("button");
  primary.type = "button";
  primary.className = draft ? "primary" : "program-play";
  primary.dataset.programAction = draft ? "create" : "play";
  if (!draft) primary.dataset.programId = item.id;
  primary.innerHTML = draft ? ICON_CHECK : "Play";
  primary.title = draft ? "Programmpunkt speichern" : "Programmpunkt starten";
  primary.setAttribute("aria-label", draft ? "Programmpunkt speichern" : "Programmpunkt starten");
  actions.appendChild(primary);
  if (draft) {
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "danger";
    cancel.dataset.programAction = "cancel";
    cancel.innerHTML = ICON_X;
    cancel.title = "Bearbeitung abbrechen";
    cancel.setAttribute("aria-label", "Programmpunkt abbrechen");
    actions.appendChild(cancel);
  } else {
    // Grey the row out; "Weiter" then skips it.
    const hide = document.createElement("button");
    hide.type = "button";
    hide.className = "program-hide";
    hide.dataset.programAction = "hide";
    hide.dataset.programId = item.id;
    actions.appendChild(hide);
    // Duplicate, shortcuts and delete sit in the "⋮" menu (programMenuItems).
    const menu = document.createElement("button");
    menu.type = "button";
    menu.className = "program-menu-btn";
    menu.dataset.programAction = "menu";
    menu.dataset.programId = item.id;
    menu.dataset.menuKey = `program:${item.id}`;
    menu.title = "Duplizieren, Shortcut laden oder speichern, löschen";
    menu.setAttribute("aria-haspopup", "menu");
    menu.setAttribute("aria-expanded", "false");
    menu.innerHTML = ICON_DOTS;
    actions.appendChild(menu);
  }
  programCell(row, "Aktion", actions, "program-actions-cell");
  if (draft) {
    renderProgramSummary(row, { ...item, title: item.title || "Neuer Programmpunkt" });
    row.querySelectorAll("select[data-program-field]").forEach(syncSingleChoice);
  } else updateProgramRow(row, item);
  return row;
}

// ---- Compact rows -----------------------------------------------------------
// A row shows one summary line (time, title, what it does); a tap on it opens
// the fields below it for editing. One row is open at a time; the draft row
// is always open.

const ICON_CHEVRON = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`;

function buildProgramSummary(draft) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "program-summary";
  button.dataset.programAction = "toggle";
  button.disabled = draft;
  button.setAttribute("aria-expanded", String(draft));
  button.innerHTML = `<span class="program-sum-when"><span class="program-sum-state"></span><span class="program-sum-time"></span><span class="program-delay">—</span></span>`
    + `<span class="program-sum-main"><span class="program-sum-head"><span class="program-sum-title"></span><span class="program-sum-person"></span></span><span class="program-sum-chips"></span></span>`
    + `<span class="program-sum-chevron">${ICON_CHEVRON}</span>`;
  return button;
}

// What a row does, as short chips (from the saved row, not the fields being edited).
function programChips(item) {
  const chips = [];
  const add = (kind, label, text) => chips.push({ kind, label, text });
  if (item.group) add("group", "Gruppe", item.group);
  if (item.light) add("light", "Licht", (PROGRAM_LIGHT_SCENES.find(scene => scene.id === item.light) || {}).name || "Gelöschtes Preset");
  for (const [field, label] of [["playback", "Playback"], ["translation", "Übersetzung"], ["lyrics", "Liedtext"]]) {
    const value = item[field];
    if (value) add("file", label, isFileAction(value) ? fileBaseName(parseFileAction(value).path) : programLegacyLabel(value));
  }
  if (item.song) {
    const song = SONG_INDEX.get(item.song);
    add("song", "Lied", song ? song.title : item.song.replace(/\.(foil|pptx)$/i, ""));
  }
  for (const [cam, camLabel] of PROGRAM_CAMERAS) {
    const preset = item[`${cam}_preset`];
    if (!preset) continue;
    const option = PROGRAM_CAMERA_PRESETS[cam].find(entry => entry.value === String(preset));
    const scene = item[`${cam}_scene`] ? ` → OBS ${item[`${cam}_scene`]}` : "";
    add("camera", camLabel, (option ? option.label.replace(/^[^·]*·\s*/, "") : `Preset ${preset}`) + scene);
  }
  for (const id of item.desktop || []) add("desktop", "Desktop", (PROGRAM_DESKTOP_PROFILES.find(p => p.id === id) || {}).title || id);
  if (item.beamer) add("beamer", "Beamer", item.beamer);
  return chips;
}

function renderProgramSummary(row, item) {
  const summary = row.querySelector(".program-summary");
  summary.querySelector(".program-sum-time").textContent = item.time || "--:--";
  summary.querySelector(".program-sum-title").textContent = item.title || "Ohne Titel";
  summary.querySelector(".program-sum-person").textContent = item.person || "";
  renderProgramChips(summary.querySelector(".program-sum-chips"), programChips(item), !item.group);
  if (!summary.disabled) summary.title = row.classList.contains("program-open") ? "Zuklappen" : "Bearbeiten";
}

// noGroup: the row has no mute group; a red chip comes first.
function renderProgramChips(box, chips, noGroup = false) {
  const sig = JSON.stringify([chips, noGroup]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  const warn = noGroup ? [Object.assign(document.createElement("span"), {
    className: "program-chip no-group", textContent: NO_PROGRAM_GROUP,
    title: "Dieser Punkt schaltet keine Mute Gruppe – nur seine Aktionen laufen",
  })] : [];
  box.replaceChildren(...warn, ...(chips.length ? chips.map(chip => {
    const element = document.createElement("span");
    element.className = `program-chip ${chip.kind}`;
    const label = document.createElement("b");
    label.textContent = chip.label;
    element.append(label, ` ${chip.text}`);
    element.title = `${chip.label}: ${chip.text}`;
    return element;
  }) : [Object.assign(document.createElement("span"), { className: "program-chip none", textContent: "Keine Aktionen" })]));
}

function setProgramOpen(row, open) {
  if (row.classList.contains("program-draft-row")) return;
  if (open) {
    for (const other of $("program-items").querySelectorAll("tr.program-open[data-program-id]")) {
      if (other !== row) setProgramOpen(other, false);
    }
  }
  row.classList.toggle("program-open", open);
  const summary = row.querySelector(".program-summary");
  summary.setAttribute("aria-expanded", String(open));
  summary.title = open ? "Zuklappen" : "Bearbeiten";
  if (!open && row.contains(document.activeElement) && document.activeElement !== summary) summary.focus();
}

// Bring an existing row up to date in place. The field that has the focus and
// rows with an edit still being saved keep what the user typed.
function updateProgramRow(row, item) {
  fillProgramFields(row, item);
  renderProgramSummary(row, item);
  updateProgramRowState(row, item);
}

// A row's values into its fields (also the new-row draft's). Unless force,
// the field with the focus and rows with an edit still being saved keep what
// the user typed.
function fillProgramFields(row, item, force = false) {
  const editing = !force && (PROGRAM_SAVING.has(item.id) || PROGRAM_RESAVE.has(item.id));
  const groupOptions = programGroupOptions();
  for (const control of row.querySelectorAll("[data-program-field]")) {
    const field = control.dataset.programField;
    // "Datei oder Pfad…" stays chosen while its path is still being entered.
    const keep = !force && (editing || control === document.activeElement || control.value === FILE_CHOOSE);
    if (control.tagName === "SELECT" && control.dataset.programCamera) {
      const value = keep ? control.value : (item[field] ? String(item[field]) : "");
      setSelectOptions(control, PROGRAM_CAMERA_PRESETS[control.dataset.programCamera], control.dataset.placeholder, value);
    } else if (control.tagName === "SELECT") {
      const value = keep ? control.value : item[field];
      if (field === "light") {
        setSelectOptions(control, programLightOptions(value), NO_PROGRAM_LIGHT, value);
        continue;
      }
      if (control.dataset.obsScene) {
        setSelectOptions(control, programObsOptions(value), NO_OBS_SCENE, value);
        continue;
      }
      if (field === "beamer") {
        setSelectOptions(control, PROGRAM_BEAMER_OPTIONS, NO_PROGRAM_BEAMER, value);
        continue;
      }
      setSelectOptions(control, groupOptions, NO_PROGRAM_GROUP, value);
    } else if (!keep) {
      control.value = Array.isArray(item[field]) ? item[field].join(",") : (item[field] ?? "");
    }
  }
  const picker = row.querySelector(".song-picker");
  if (picker) syncSongPicker(picker);
  row.querySelectorAll(".program-action-link").forEach(syncProgramAction);
  row.querySelectorAll(".program-desktop").forEach(syncProgramDesktop);
  row.querySelectorAll("select[data-program-field]").forEach(syncSingleChoice);
}

// Delay, running/next/done and hidden of an existing row.
function updateProgramRowState(row, item) {
  const delay = row.querySelector(".program-delay");
  const seconds = item.last_delay_seconds;
  delay.textContent = formatProgramDelay(seconds);
  delay.className = "program-delay" + (seconds === null || seconds === undefined ? ""
    : seconds > 0 ? " late" : seconds < 0 ? " early" : " ontime");
  delay.title = item.last_played_at ? `Zuletzt geklickt: ${item.last_played_at}` : "Noch nicht gestartet";
  const current = item.id === PROGRAM_STATUS.current_id;
  row.classList.toggle("program-current", current);
  row.classList.toggle("program-next", item.id === PROGRAM_STATUS.next_id);
  row.classList.toggle("program-done", !current && playedToday(item));
  const hidden = !!item.hidden;
  row.classList.toggle("program-hidden", hidden);
  const label = item.title || "Programmpunkt";
  const hide = row.querySelector('[data-program-action="hide"]');
  if (hide && hide.getAttribute("aria-pressed") !== String(hidden)) {
    hide.setAttribute("aria-pressed", String(hidden));
    hide.innerHTML = hidden ? ICON_EYE : ICON_EYE_OFF;
  }
  if (hide) {
    hide.title = hidden ? "Wieder einblenden: „Weiter“ startet ihn wieder" : "Ausblenden: grau, „Weiter“ überspringt ihn";
    hide.setAttribute("aria-label", `${label} ${hidden ? "wieder einblenden" : "ausblenden und überspringen"}`);
  }
  const menu = row.querySelector('[data-program-action="menu"]');
  if (menu) menu.setAttribute("aria-label", `${label}: duplizieren, als Shortcut speichern, Shortcut laden oder löschen`);
}

// Rows are kept and updated in place (focus, cursor and the draft row
// survive); only new rows are built and removed ones dropped.
function renderProgramItems() {
  const tbody = $("program-items");
  const existing = new Map([...tbody.querySelectorAll("tr[data-program-id]")].map(row => [row.dataset.programId, row]));
  const wanted = [];
  for (const item of PROGRAM_ITEMS) {
    let row = existing.get(item.id);
    if (row) { updateProgramRow(row, item); existing.delete(item.id); }
    else row = buildProgramRow(item);
    wanted.push(row);
  }
  for (const row of existing.values()) row.remove();
  let draft = tbody.querySelector(".program-draft-row");
  if (PROGRAM_DRAFT) {
    if (!draft) {
      const last = PROGRAM_ITEMS[PROGRAM_ITEMS.length - 1];
      const group = last && name_in(PROGRAM_GROUPS, last.group) ? last.group : (Object.keys(PROGRAM_GROUPS)[0] || "");
      draft = buildProgramRow({ time: nextProgramTime(), group }, true);
    }
    else {
      const group = draft.querySelector('[data-program-field="group"]');
      setSelectOptions(group, programGroupOptions(), NO_PROGRAM_GROUP, group.value);
      const light = draft.querySelector('[data-program-field="light"]');
      setSelectOptions(light, programLightOptions(light.value), NO_PROGRAM_LIGHT, light.value);
      for (const scene of draft.querySelectorAll("select[data-obs-scene]")) {
        setSelectOptions(scene, programObsOptions(scene.value), NO_OBS_SCENE, scene.value);
      }
      draft.querySelectorAll(".program-desktop").forEach(syncProgramDesktop);
      draft.querySelectorAll(".program-action-link").forEach(syncProgramAction);
      draft.querySelectorAll("select[data-program-field]").forEach(syncSingleChoice);
    }
    wanted.push(draft);
  } else if (draft) {
    draft.remove();
  }
  // Only move rows that are out of place: moving a row would blur its field.
  wanted.forEach((row, i) => { if (tbody.children[i] !== row) tbody.insertBefore(row, tbody.children[i] || null); });
  renderProgramAddRow();
  updateProgramCue();
  renderShortcuts();
  renderSongDock();   // js/song-dock.js: verse keys at the running row
  renderDocDock();    // js/doc-dock.js: page keys for a document at the running row
}

// ---- Shortcuts (app/shortcuts.py) -------------------------------------------
// What a program row does, saved under a name: a key in the Desktop section
// (Play leaves the program's order and start times alone), edited there with
// the same fields as a row, duplicated, and loaded into program rows ("⋮" →
// "Shortcut laden": a copy, see app/program_items.py apply_shortcut).

let PROGRAM_SHORTCUTS_SIG = "";
let SHORTCUT_EDIT_ID = null;  // null: a new shortcut

function renderShortcuts() {
  const wrap = $("desktop-shortcuts");
  const sig = JSON.stringify(PROGRAM_SHORTCUTS.map(s => [s.id, s.title, programChips(s).map(c => c.text)]));
  if (sig !== PROGRAM_SHORTCUTS_SIG) {
    PROGRAM_SHORTCUTS_SIG = sig;
    const tiles = PROGRAM_SHORTCUTS.map(shortcut => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "display-key program-shortcut-key";
      button.dataset.shortcutPlay = shortcut.id;
      button.title = `Shortcut „${shortcut.title}“ ausführen`;
      const led = document.createElement("span");
      led.className = "led safe";
      const title = document.createElement("span");
      title.className = "display-key-title";
      title.textContent = shortcut.title;
      const meta = document.createElement("span");
      meta.className = "display-key-meta";
      meta.textContent = programChips(shortcut).map(chip => chip.text).join(" · ") || "Keine Aktionen";
      button.append(led, title, meta);
      const menu = document.createElement("button");
      menu.type = "button";
      menu.className = "display-menu-btn";
      menu.dataset.shortcutMenu = shortcut.id;
      menu.title = `${shortcut.title}: bearbeiten, duplizieren oder löschen`;
      menu.setAttribute("aria-label", menu.title);
      menu.setAttribute("aria-haspopup", "menu");
      menu.setAttribute("aria-expanded", "false");
      menu.innerHTML = ICON_DOTS;
      const tile = document.createElement("span");
      tile.className = "display-tile program-shortcut";
      tile.append(button, menu);
      return tile;
    });
    const add = document.createElement("button");
    add.type = "button";
    add.className = "display-add";
    add.dataset.shortcutNew = "1";
    add.title = "Neuer Shortcut";
    add.setAttribute("aria-label", "Neuer Shortcut");
    add.innerHTML = '<svg class="plus-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 4v16M4 12h16"/></svg>';
    const label = document.createElement("span");
    label.className = "desktop-launch-status";
    label.textContent = PROGRAM_SHORTCUTS.length ? "Shortcuts" : "Shortcuts – mit + anlegen oder im Programm über „⋮“ speichern";
    wrap.replaceChildren(...tiles, add, label);
  }
  wrap.hidden = false;
  for (const button of wrap.querySelectorAll("[data-shortcut-play]")) {
    button.disabled = PROGRAM_BUSY.has(`shortcut:${button.dataset.shortcutPlay}`);
  }
}

async function saveRowAsShortcut(itemId) {
  const item = PROGRAM_ITEMS.find(entry => entry.id === itemId);
  const result = await api("POST", `/program/items/${encodeURIComponent(itemId)}/shortcut`);
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  toast(`„${item ? item.title : "Programmpunkt"}“ als Shortcut unter Desktop gespeichert`, true);
  await loadProgramItems();
}

function openShortcutDialog(shortcutId) {
  const shortcut = shortcutId ? PROGRAM_SHORTCUTS.find(s => s.id === shortcutId) : null;
  if (shortcutId && !shortcut) return toast("Shortcut nicht gefunden", false);
  SHORTCUT_EDIT_ID = shortcut ? shortcut.id : null;
  $("shortcut-dialog-title").textContent = shortcut ? "Shortcut bearbeiten" : "Neuer Shortcut";
  $("shortcut-error").hidden = true;
  $("shortcut-save").disabled = false;
  $("shortcut-editor").replaceChildren(buildProgramRow(shortcut || { title: "" }, false, "shortcut"));
  $("shortcut-dialog").showModal();
  $("shortcut-editor").querySelector('[data-program-field="title"]').focus();
}

async function saveShortcut(event) {
  event.preventDefault();
  const row = $("shortcut-editor").querySelector("tr");
  const { time, person, ...body } = readProgramRow(row);
  const error = message => { $("shortcut-error").hidden = false; $("shortcut-error").textContent = message; };
  if (!body.title) return error("Bitte einen Namen eingeben.");
  $("shortcut-save").disabled = true;
  const result = SHORTCUT_EDIT_ID
    ? await api("PUT", `/shortcuts/${encodeURIComponent(SHORTCUT_EDIT_ID)}`, body)
    : await api("POST", "/shortcuts", body);
  $("shortcut-save").disabled = false;
  if (!result.ok || result.data.ok === false) return error("Nicht gespeichert: " + (result.data.reason || `HTTP ${result.status}`));
  $("shortcut-dialog").close();
  toast(`Shortcut „${body.title}“ gespeichert`, true);
  await loadProgramItems();
}

async function duplicateShortcut(shortcutId) {
  const result = await api("POST", `/shortcuts/${encodeURIComponent(shortcutId)}/duplicate`);
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  await loadProgramItems();
  openShortcutDialog(result.data.shortcut.id);
}

async function deleteShortcut(shortcutId, anchor) {
  const shortcut = PROGRAM_SHORTCUTS.find(s => s.id === shortcutId);
  if (!shortcut || !anchor || !anchor.isConnected) return;
  if (!(await confirmDelete(anchor, `Shortcut „${shortcut.title}“ löschen?`))) return;
  const result = await api("DELETE", `/shortcuts/${encodeURIComponent(shortcutId)}`);
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  toast(`Shortcut „${shortcut.title}“ gelöscht`, true);
  await loadProgramItems();
}

async function playShortcut(shortcutId, button) {
  const shortcut = PROGRAM_SHORTCUTS.find(s => s.id === shortcutId);
  const key = `shortcut:${shortcutId}`;
  if (PROGRAM_BUSY.has(key)) return;
  PROGRAM_BUSY.add(key);
  button.disabled = true;
  try {
    programPlayToast(await api("POST", `/shortcuts/${encodeURIComponent(shortcutId)}/play`, undefined, { timeout: 30000 }));
    await refreshSongLive();
  } finally {
    PROGRAM_BUSY.delete(key);
    button.disabled = false;
    await refresh();
  }
  if (!shortcut) await loadProgramItems();
}

async function loadProgramItems() {
  const [items, groups, buttons, desktop, status, scenes, shortcuts] = await Promise.all([
    api("GET", "/program/items"), api("GET", "/groups"), api("GET", "/companion/buttons"),
    api("GET", "/desktop/profiles"), api("GET", "/program/status"), api("GET", "/lights/scenes"),
    api("GET", "/shortcuts"),
  ]);
  if (scenes.ok && Array.isArray(scenes.data.scenes)) PROGRAM_LIGHT_SCENES = scenes.data.scenes;
  if (shortcuts.ok && Array.isArray(shortcuts.data.shortcuts)) PROGRAM_SHORTCUTS = shortcuts.data.shortcuts;
  if (status.ok && status.data) PROGRAM_STATUS = status.data;
  if (!items.ok || !Array.isArray(items.data) || !groups.ok || !buttons.ok || !desktop.ok) {
    $("program-empty").hidden = false;
    $("program-empty").textContent = "Programmdaten konnten nicht geladen werden";
    return;
  }
  $("program-empty").textContent = PROGRAM_EMPTY_TEXT;
  PROGRAM_ITEMS = items.data;
  PROGRAM_GROUPS = groups.data;
  // Camera presets and the light and mixer keys left Steuerung but program
  // rows can still run them.
  PROGRAM_BUTTONS = [...(buttons.data.buttons || []), ...(buttons.data.camera_buttons || []), ...(buttons.data.catalog_buttons || [])];
  PROGRAM_BUTTON_ALIASES = buttons.data.button_aliases || {};
  PROGRAM_BUTTON_CATEGORIES = { cameras: "Kameras", light: "Licht", mixer: "Mixer", ...Object.fromEntries((buttons.data.categories || []).map(category => [category.id, category.name])) };
  PROGRAM_DESKTOP_PROFILES = desktop.data.profiles || [];
  PROGRAM_CAMERA_PRESETS = programCameraPresets(buttons.data.camera_buttons || []);
  noteCameraProgram(PROGRAM_STATUS.camera);
  renderProgramItems();
}

// After a scene was saved or deleted in the Licht section.
async function loadProgramLightScenes() {
  const result = await api("GET", "/lights/scenes");
  if (!result.ok || !Array.isArray(result.data.scenes)) return;
  PROGRAM_LIGHT_SCENES = result.data.scenes;
  renderProgramItems();
}

// Starts and the current/next row as other devices see them (every 5 s).
async function refreshProgramState() {
  if (!STAGE.on && !$("program-items").closest(".panel").classList.contains("open")) return;
  const [items, status] = await Promise.all([api("GET", "/program/items"), api("GET", "/program/status")]);
  if (!items.ok || !Array.isArray(items.data) || !status.ok) return;
  PROGRAM_ITEMS = items.data;
  PROGRAM_STATUS = status.data;
  noteCameraProgram(PROGRAM_STATUS.camera);
  renderProgramItems();
}

function playedToday(item) {
  if (!item || !item.last_played_at) return false;
  const d = new Date();
  const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return item.last_played_at.startsWith(today);
}

// "930", "9:30", "9.30", "0930" -> "09:30"; anything else stays as typed
// (and the field shows it as invalid).
function normalizeProgramTime(value) {
  const m = /^\s*(\d{1,2})(?:[:.,h ]?(\d{2}))?\s*$/.exec(value || "");
  if (!m) return (value || "").trim();
  let hours = m[1], minutes = m[2];
  if (minutes === undefined) {
    if (hours.length > 2) return value.trim();
    minutes = "00";
  }
  if (Number(hours) > 23 || Number(minutes) > 59) return value.trim();
  return `${hours.padStart(2, "0")}:${minutes}`;
}

function clockText(date) {
  return [date.getHours(), date.getMinutes(), date.getSeconds()].map(n => String(n).padStart(2, "0")).join(":");
}

// Clock, the next row and the time until it is due.
function updateProgramCue() {
  const now = new Date();
  $("program-clock").textContent = clockText(now);
  const next = PROGRAM_ITEMS.find(item => item.id === PROGRAM_STATUS.next_id);
  const go = $("program-go-btn");
  const count = $("program-cue-count");
  go.disabled = !next || PROGRAM_BUSY.size > 0;
  if (!next) {
    $("program-cue-label").textContent = PROGRAM_ITEMS.length ? "Programm" : "Als Nächstes";
    $("program-cue-title").textContent = PROGRAM_ITEMS.length ? "Ende – alle Punkte gestartet" : "Noch keine Programmpunkte";
    count.textContent = "";
    count.classList.remove("late");
    go.title = "Kein weiterer Programmpunkt";
    return;
  }
  $("program-cue-label").textContent = "Als Nächstes";
  $("program-cue-title").textContent = `${next.time} · ${next.title}${next.person ? " · " + next.person : ""}`;
  go.title = `„${next.title}“ starten`;
  const [h, m] = next.time.split(":").map(Number);
  const due = new Date(now);
  due.setHours(h, m, 0, 0);
  let diff = Math.round((due - now) / 1000);
  if (diff < -12 * 3600) diff += 24 * 3600;
  if (diff > 12 * 3600) diff -= 24 * 3600;
  const abs = Math.abs(diff);
  // Durations, not clock times: "4:12 min", "25 min", "1 h 05 min".
  const text = abs >= 3600
    ? `${Math.floor(abs / 3600)} h ${String(Math.floor(abs % 3600 / 60)).padStart(2, "0")} min`
    : abs >= 600 ? `${Math.floor(abs / 60)} min`
    : `${Math.floor(abs / 60)}:${String(abs % 60).padStart(2, "0")} min`;
  count.textContent = diff >= 0 ? `in ${text}` : `seit ${text} fällig`;
  count.classList.toggle("late", diff < 0);
}

function readProgramRow(row) {
  const result = {};
  for (const field of PROGRAM_FIELDS) {
    const control = row.querySelector(`[data-program-field="${field}"]`);
    if (control && field === "time") control.value = normalizeProgramTime(control.value);
    result[field] = control ? control.value.trim() : "";
  }
  for (const field of ["playback", "translation", "lyrics", "song", "light", "cam1_scene", "cam2_scene", "beamer"]) {
    if (result[field] === FILE_CHOOSE) result[field] = null;  // file chosen, path not entered yet
    result[field] ||= null;
  }
  result.desktop = result.desktop ? result.desktop.split(",").filter(Boolean) : [];
  for (const [cam] of PROGRAM_CAMERAS) {
    result[`${cam}_preset`] = result[`${cam}_preset`] ? Number(result[`${cam}_preset`]) : null;
  }
  result.camera_wait_seconds ||= null;  // empty: the service's default
  return result;
}

async function createProgramItem(row) {
  const button = row.querySelector('[data-program-action="create"]');
  if (button.disabled) return;
  button.disabled = true;
  try {
    const result = await api("POST", "/program/items", readProgramRow(row));
    if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
    PROGRAM_DRAFT = false;
    await loadProgramItems();
    toast("Programmpunkt angelegt", true);
  } finally {
    button.disabled = false;
  }
}

async function cloneProgramItem(itemId, button) {
  const item = PROGRAM_ITEMS.find(entry => entry.id === itemId);
  if (!item || PROGRAM_BUSY.has(itemId)) return;
  PROGRAM_BUSY.add(itemId);
  button.disabled = true;
  try {
    const result = await api("POST", `/program/items/${encodeURIComponent(itemId)}/duplicate`);
    if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
    await loadProgramItems();
    toast(`„${item.title}“ dupliziert`, true);
  } finally {
    PROGRAM_BUSY.delete(itemId);
    button.disabled = false;
  }
}

// One save per item at a time; an edit made while a save is in flight is sent
// right after it (with the row's values at that moment), never dropped.
// Resolves true once the row (with this edit) is saved, false if not.
const PROGRAM_SAVE_RUNS = new Map();  // item id -> the running save's promise

function updateProgramItem(row) {
  const itemId = row.dataset.programId;
  if (!itemId) return Promise.resolve(false);
  if (PROGRAM_SAVING.has(itemId)) {
    PROGRAM_RESAVE.add(itemId);  // the running save sends it right after
    return PROGRAM_SAVE_RUNS.get(itemId) || Promise.resolve(false);
  }
  const run = saveProgramItem(row, itemId);
  PROGRAM_SAVE_RUNS.set(itemId, run);
  run.finally(() => { if (PROGRAM_SAVE_RUNS.get(itemId) === run) PROGRAM_SAVE_RUNS.delete(itemId); });
  return run;
}

async function saveProgramItem(row, itemId) {
  PROGRAM_SAVING.add(itemId);
  let result;
  try {
    do {
      PROGRAM_RESAVE.delete(itemId);
      result = await api("PUT", `/program/items/${encodeURIComponent(itemId)}`, readProgramRow(row));
      if (!result.ok || result.data.ok === false) {
        PROGRAM_RESAVE.delete(itemId);
        toast(result.data.reason || `HTTP ${result.status}`, false);
        break;
      }
    } while (PROGRAM_RESAVE.has(itemId) && row.isConnected);
  } finally {
    PROGRAM_SAVING.delete(itemId);
  }
  if (result.ok && result.data.ok !== false && result.data.item) {
    PROGRAM_ITEMS = PROGRAM_ITEMS.map(item => item.id === itemId ? result.data.item : item);
    renderProgramItems();
    return true;
  }
  await loadProgramItems();  // show what the service really has
  return false;
}

async function toggleProgramHidden(itemId, button) {
  const item = PROGRAM_ITEMS.find(entry => entry.id === itemId);
  if (!item || button.disabled) return;
  const hidden = !item.hidden;
  button.disabled = true;
  try {
    const result = await api("POST", `/program/items/${encodeURIComponent(itemId)}/hidden`, { hidden });
    if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
    toast(hidden ? `„${item.title}“ ausgeblendet – „Weiter“ überspringt ihn` : `„${item.title}“ wieder eingeblendet`, true);
    await loadProgramItems();  // "Als Nächstes" may have changed
  } finally {
    button.disabled = false;
  }
}

async function deleteProgramItem(itemId, button) {
  const item = PROGRAM_ITEMS.find(entry => entry.id === itemId);
  if (!item || !(await confirmDelete(button, `Programmpunkt „${item.title}“ löschen?`))) return;
  const result = await api("DELETE", `/program/items/${encodeURIComponent(itemId)}`);
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  PROGRAM_ITEMS = PROGRAM_ITEMS.filter(entry => entry.id !== itemId);
  renderProgramItems();
}

// A row runs best effort per area (app/api/program.py): what did not work,
// named briefly, in one line.
const PROGRAM_AREA_LABELS = {
  mixer: "Mischpult", light: "Licht", beamer: "Beamer", playback: "Playback", translation: "Übersetzung",
  lyrics: "Liedtext", desktop: "Desktop", song: "Lied", cameras: "Kameras",
};
const PROGRAM_AREA_REASONS = [
  [/no mixer connection|not connected/i, "nicht verbunden"],
  [/link stale|no data from mixer/i, "keine Daten vom Pult"],
  [/panic/i, "Panic aktiv"],
  [/display agent unavailable|unavailable|timed? ?out|no answer/i, "keine Antwort"],
  [/^unknown group$/i, "Mutegruppe gelöscht"],
  [/^unknown button$/i, "Taste gelöscht"],
  [/^unknown light scene$/i, "Lichtszene gelöscht"],
  [/^unknown song$/i, "Lied nicht gefunden"],
  [/^unknown desktop profile/i, "Profil gelöscht"],
  [/song library not loaded/i, "Liedbibliothek nicht geladen"],
  [/switches mutes/i, "Taste schaltet Mutes (die Mutegruppe des Punkts tut das)"],
];

function programReasonText(reason) {
  const known = PROGRAM_AREA_REASONS.find(([pattern]) => pattern.test(reason));
  return known ? known[1] : reason;
}

// "Desktop: Logo: keine Antwort" – the desktop area names its profiles.
function programAreaText(area, entry) {
  const parts = String(entry.reason || "fehlgeschlagen").split("; ").map(part => {
    const named = area === "desktop" && /^(.+?): (.+)$/.exec(part);
    return named ? `${named[1]}: ${programReasonText(named[2])}` : programReasonText(part);
  });
  return `${PROGRAM_AREA_LABELS[area] || area}: ${parts.join(", ")}`;
}

function programPlayToast(result) {
  const title = result.data && result.data.title ? `„${result.data.title}“` : "Programmpunkt";
  // What the camera part said is in this toast; later outcomes come from the status.
  const cameraSeq = result.data && result.data.camera && result.data.camera.seq;
  if (typeof cameraSeq === "number") PROGRAM_CAMERA_SEQ = Math.max(PROGRAM_CAMERA_SEQ ?? 0, cameraSeq);
  const areas = result.ok && result.data && result.data.areas;
  if (areas && result.data.ok === false) {
    // Some areas did not work: one line with all of them, the rest ran.
    const failed = Object.entries(areas).filter(([, entry]) => !entry.ok).map(([area, entry]) => programAreaText(area, entry));
    const ran = Object.values(areas).some(entry => entry.ok || entry.partial);
    toast(`${title} ${ran ? "gestartet" : "– nichts ausgeführt"} – ${failed.join("; ")}`, false);
    noteSongScreensFailed(result.data.song);
    const wait = result.data.camera && result.data.camera.switch_in_seconds;
    if (wait) setTimeout(checkCameraProgram, (wait + 4) * 1000);
    return;
  }
  if (!result.ok || result.data.ok === false) {
    // Name the action that failed and why (e.g. a file missing on the target PC).
    const failed = ((result.data && result.data.linked_actions) || []).find(action => action.ok === false);
    const detail = failed && failed.reason ? ` – ${failed.file ? failed.file + ": " : ""}${failed.reason}` : "";
    if (result.data && result.data.reason === "program moved on") {
      toast("Schon weiter: der nächste Punkt wurde gerade gestartet (anderes Gerät oder doppelt getippt)", false);
      return;
    }
    toast(`${title}: ${(result.data && result.data.reason) || "HTTP " + result.status}${detail}`, false);
  } else {
    const camera = result.data.camera && result.data.camera.message ? ` · ${result.data.camera.message}` : "";
    toast(`${title} gestartet · Verzögerung ${formatProgramDelay(result.data.delay_seconds)}${camera}`, true);
    noteSongScreensFailed(result.data.song);   // js/song-screens.js: a lyrics screen that missed it
    // Switching follows in the background: ask for the outcome once it is due.
    const wait = result.data.camera && result.data.camera.switch_in_seconds;
    if (wait) setTimeout(checkCameraProgram, (wait + 4) * 1000);
  }
}

async function startProgram(key, path, button) {
  if (PROGRAM_BUSY.has(key)) return;
  PROGRAM_BUSY.add(key);
  button.disabled = true;
  updateProgramCue();
  try {
    // "Weiter" names the row it means: if another device (or a double tap)
    // already started it, the service refuses instead of skipping a row.
    const body = key === "next" && PROGRAM_STATUS.next_id ? { expected_next_id: PROGRAM_STATUS.next_id } : undefined;
    programPlayToast(await api("POST", path, body, { timeout: 30000 }));
    await loadProgramItems();
    await refreshSongLive();
  } finally {
    PROGRAM_BUSY.delete(key);
    button.disabled = false;
    updateProgramCue();
    await refresh();
  }
}

function playProgramItem(itemId, button) {
  return startProgram(itemId, `/program/items/${encodeURIComponent(itemId)}/play`, button);
}


$("program-go-btn").addEventListener("click", event => startProgram("next", "/program/next", event.currentTarget));
$("program-export-btn").addEventListener("click", () => {
  const link = document.createElement("a");
  link.href = "/program/export";
  link.download = "programm.json";
  link.click();
});
$("program-import-btn").addEventListener("click", () => $("program-import-file").click());
$("program-import-file").addEventListener("change", async event => {
  const input = event.currentTarget;
  const file = input.files && input.files[0];
  input.value = "";
  if (!file) return;
  if (PROGRAM_DRAFT || PROGRAM_SAVING.size || PROGRAM_RESAVE.size) {
    toast("Bitte zuerst die laufende Bearbeitung abschließen", false);
    return;
  }
  if (!(await confirmDelete($("program-import-btn"), "Die Datei ersetzt das gesamte aktuelle Programm. Weiter?", "Importieren"))) return;
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (_) {
    toast("Die Datei ist kein gültiges JSON", false);
    return;
  }
  const button = $("program-import-btn");
  button.disabled = true;
  try {
    const result = await api("POST", "/program/import", payload, { timeout: 30000 });
    if (!result.ok || result.data.ok === false) {
      toast(result.data.reason || `HTTP ${result.status}`, false);
      return;
    }
    await loadProgramItems();
    toast(`${result.data.items.length} Programmpunkte importiert`, true);
    const warnings = result.data.warnings || [];
    if (warnings.length) {
      // One summary; every detail is in the browser console.
      const shown = warnings.slice(0, 3).map(w => `${w.kind} „${w.name}“`).join(", ");
      toast(`Import: ${warnings.length} ${warnings.length === 1 ? "Verweis" : "Verweise"} nicht gefunden oder mehrdeutig (${shown}${warnings.length > 3 ? ", …" : ""}) – entfernt`, false);
      for (const w of warnings) console.warn(`Import, Zeile ${w.item} (${w.title || "ohne Titel"}): ${w.reason}`);
    }
  } finally {
    button.disabled = false;
  }
});
$("program-reset-btn").addEventListener("click", async event => {
  const button = event.currentTarget;
  if (!(await confirmDelete(button, "Startzeiten und Verzögerungen aller Punkte vergessen? „Weiter“ beginnt dann wieder oben.", "Zurücksetzen"))) return;
  const result = await api("POST", "/program/reset");
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  toast("Ablauf zurückgesetzt", true);
  await loadProgramItems();
});

// The last "row" of the list: dashed and empty, "+ Programmpunkt hinzufügen",
// the same as the head key. Hidden while a new row is open (that is the last
// row then); with no rows at all it also says how else to start (it replaces
// the empty note, which is left for a load error).
function renderProgramAddRow() {
  $("program-add-row").hidden = PROGRAM_DRAFT;
  $("program-add-row-hint").hidden = PROGRAM_ITEMS.length > 0;
  $("program-empty").hidden = true;
}

function startProgramDraft() {
  PROGRAM_DRAFT = true;
  renderProgramItems();
  document.querySelector('#program-items .program-draft-row [data-program-field="title"]')?.focus();
}
$("program-add-btn").addEventListener("click", startProgramDraft);
$("program-add-row").addEventListener("click", startProgramDraft);
$("program-items").addEventListener("change", event => {
  const field = event.target.closest("[data-program-field]");
  if (field && field.dataset.programField === "time") field.value = normalizeProgramTime(field.value);
  const row = field && field.closest("tr");
  if (row && row.dataset.programId) updateProgramItem(row);
  // A new row: its summary shows what it will do as it is filled in.
  if (row && row.classList.contains("program-draft-row")) {
    const draft = readProgramRow(row);
    renderProgramSummary(row, { ...draft, title: draft.title || "Neuer Programmpunkt" });
  }
});
$("program-items").addEventListener("click", event => {
  const button = event.target.closest("[data-program-action]");
  if (!button) return;
  const action = button.dataset.programAction;
  if (action === "toggle") {
    const row = button.closest("tr");
    setProgramOpen(row, !row.classList.contains("program-open"));
    return;
  }
  if (action === "create") return createProgramItem(button.closest("tr"));
  if (action === "cancel") { PROGRAM_DRAFT = false; renderProgramItems(); return; }
  if (action === "menu") return toggleActionMenu(button, () => programMenuItems(button.dataset.programId));
  if (action === "hide") return toggleProgramHidden(button.dataset.programId, button);
  if (action === "load-shortcut") return openShortcutLoad(button.closest("tr"));
  if (action === "play") return playProgramItem(button.dataset.programId, button);
});
// A row's "⋮" (js/menus.js). run gets the row's "⋮" key: it anchors the
// delete question once the menu is gone.
function programMenuItems(itemId) {
  const row = () => $("program-items").querySelector(`tr[data-program-id="${CSS.escape(itemId)}"]`);
  return [
    { label: "Duplizieren", icon: ICON_COPY, run: trigger => cloneProgramItem(itemId, trigger) },
    { label: "Als Shortcut speichern", icon: ICON_SHORTCUT_SAVE, title: "Was der Programmpunkt tut, als Shortcut unter Desktop speichern", run: () => saveRowAsShortcut(itemId) },
    { label: "Shortcut laden", icon: ICON_SHORTCUT_LOAD, title: "Die Einstellungen eines Shortcuts übernehmen (Uhrzeit, Titel, Person und Lied bleiben)", run: () => openShortcutLoad(row()) },
    "-",
    { label: "Löschen", icon: ICON_TRASH, danger: true, run: trigger => deleteProgramItem(itemId, trigger) },
  ];
}
// Keys inside the fields, the same in program rows and in the shortcut editor.
onProgramEditors("click", event => {
  const add = event.target.closest(".program-desktop-add");
  if (add) return toggleProgramDesktopPick(add);
  const choice = event.target.closest("[data-choice-add], [data-choice-remove], [data-choice-earlier]");
  if (choice) return changeProgramDesktop(choice);
  const pick = event.target.closest("[data-action-pick]");
  if (pick) return pickProgramAction(pick);
  const clear = event.target.closest("[data-action-clear]");
  if (clear) return clearProgramAction(clear);
  const single = event.target.closest(".program-single");
  if (single) toggleSingleChoice(single);
});
onProgramEditors("change", event => {
  const link = event.target.closest(".program-action-link");
  if (link) syncProgramAction(link);
});
// The scene list is asked again when a scene is about to be chosen (OBS may have started since).
onProgramEditors("focusin", event => {
  if (event.target.matches("select[data-obs-scene]")) loadProgramObsScenes();
});
// Esc closes the open row (not while a song search or menu is open on top of it).
$("program-items").addEventListener("keydown", event => {
  if (event.key !== "Escape" || SONG_SEARCH || OPEN_MENU) return;
  const row = event.target.closest("tr.program-open[data-program-id]");
  if (row) { event.preventDefault(); setProgramOpen(row, false); }
});
$("desktop-shortcuts").addEventListener("click", event => {
  if (event.target.closest("[data-shortcut-new]")) return openShortcutDialog(null);
  const menu = event.target.closest("[data-shortcut-menu]");
  if (menu) {
    if (menu.getAttribute("aria-expanded") === "true") closeTileMenu();
    else openTileMenu(menu, $("shortcut-menu"));
    return;
  }
  const button = event.target.closest("[data-shortcut-play]");
  if (button && !button.disabled) playShortcut(button.dataset.shortcutPlay, button);
});
$("shortcut-menu").addEventListener("click", event => {
  const entry = event.target.closest("button[data-menu-action]");
  if (!entry || !OPEN_MENU) return;
  const trigger = OPEN_MENU.trigger;
  closeTileMenu();
  const id = trigger.dataset.shortcutMenu;
  if (entry.dataset.menuAction === "edit") openShortcutDialog(id);
  else if (entry.dataset.menuAction === "duplicate") duplicateShortcut(id);
  else if (entry.dataset.menuAction === "delete") deleteShortcut(id, trigger);
});
$("shortcut-form").addEventListener("submit", saveShortcut);
$("shortcut-load-list").addEventListener("click", event => {
  const button = event.target.closest("[data-shortcut-load]");
  const shortcut = button && PROGRAM_SHORTCUTS.find(s => s.id === button.dataset.shortcutLoad);
  if (!shortcut) return;
  const row = shortcutLoadRow(SHORTCUT_LOAD_TARGET);
  SHORTCUT_LOAD_TARGET = null;
  $("shortcut-load-dialog").close();
  loadShortcutIntoRow(row, shortcut);
});
$("shortcut-load-cancel").addEventListener("click", () => {
  SHORTCUT_LOAD_TARGET = null;
  $("shortcut-load-dialog").close();
});
$("shortcut-cancel").addEventListener("click", () => $("shortcut-dialog").close());
