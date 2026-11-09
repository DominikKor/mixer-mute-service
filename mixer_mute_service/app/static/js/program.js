// Mixer Mute Service UI (program.js): Programm: the cue list.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

let PROGRAM_ITEMS = [];
let PROGRAM_GROUPS = {};
let PROGRAM_BUTTONS = [];
let PROGRAM_BUTTON_CATEGORIES = {};
let PROGRAM_DESKTOP_PROFILES = [];
// Preset choices per camera for the rows' camera line: {cam1: [{value, label}], ...}
let PROGRAM_CAMERA_PRESETS = { cam1: [], cam2: [] };
let PROGRAM_CAMERA_SEQ = null;  // last camera outcome already reported
let PROGRAM_LIGHT_SCENES = [];     // [{id, name}] from /lights/scenes
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
  "time", "title", "person", "group", "playback", "translation", "lyrics",
  "playback_description", "translation_description", "lyrics_description", "song", "light",
  "cam1_preset", "cam2_preset", "camera_wait_seconds",
];
const PROGRAM_CAMERAS = [["cam1", "Cam 1"], ["cam2", "Cam 2"]];

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
const NO_PROGRAM_GROUP = "Keine Mutegruppe";
// A row without a light scene leaves the light as it is.
const NO_PROGRAM_LIGHT = "Licht bleibt";

function programLightOptions(value) {
  const options = PROGRAM_LIGHT_SCENES.map(scene => ({ value: scene.id, label: scene.name }));
  if (value && !options.some(option => option.value === value)) options.push({ value, label: "Gelöschte Szene" });
  return options;
}

function programGroupOptions() {
  return Object.keys(PROGRAM_GROUPS).map(group => ({ value: group, label: group }));
}

function programActionOptions(value) {
  const buttonOptions = PROGRAM_BUTTONS.map(button => {
    const category = PROGRAM_BUTTON_CATEGORIES[button.category_id];
    return { value: button.id, label: category ? `${category} · ${button.label}` : button.label };
  });
  return [
    { value: FILE_CHOOSE, label: "Datei oder Pfad…" },
    // The row's own file (each row has its own, so it is added per select).
    ...(isFileAction(value) ? [{ value, label: fileActionLabel(value) }] : []),
    ...PROGRAM_DESKTOP_PROFILES.map(profile => ({ value: `desktop:${profile.id}`, label: `Desktop · ${profile.title}` })),
    ...buttonOptions,
  ];
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
      value: String(n), label: `${camLabel} · ${named[n] ? `${named[n]} (${n})` : `Preset ${n}`}`,
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

function buildProgramRow(item, draft = false) {
  const row = document.createElement("tr");
  if (draft) row.className = "program-draft-row";
  else row.dataset.programId = item.id;

  programCell(row, "Uhrzeit", programInput("time", "Uhrzeit", item.time, "text", 5));
  const delay = document.createElement("span");
  delay.className = "program-delay";
  delay.textContent = "—";
  programCell(row, "Verzögerung", delay);
  programCell(row, "Titel", programInput("title", "Titel", item.title, "text", 120));
  programCell(row, "Person", programInput("person", "Person", item.person, "text", 80));
  programCell(row, "Mutegruppe", programSelect("group", "Mutegruppe", item.group, programGroupOptions(), NO_PROGRAM_GROUP));
  programCell(row, "Licht", programSelect("light", "Lichtszene", item.light, programLightOptions(item.light), NO_PROGRAM_LIGHT));

  for (const [field, label] of [["playback", "Playback"], ["translation", "Übersetzung"], ["lyrics", "Liedtext"]]) {
    const link = document.createElement("div");
    link.className = "program-action-link";
    if (field === "lyrics") link.appendChild(buildSongPicker(item.song));
    link.appendChild(programSelect(field, label, item[field], programActionOptions(item[field]), "Keine Aktion"));
    link.appendChild(buildFileAction(label, item[field]));
    const descriptionField = `${field}_description`;
    const description = programInput(descriptionField, `${label} Beschreibung`, item[descriptionField], "text", 160);
    description.placeholder = "Beschreibung";
    link.appendChild(description);
    programCell(row, label, link);
  }

  programCell(row, "Kamera-Presets · nur auf der Kamera, die nicht live ist", buildProgramCamera(item));

  const actions = document.createElement("div");
  actions.className = "program-actions";
  if (!draft) {
    // Top right of the row: grey the row out, "Weiter" then skips it.
    const hide = document.createElement("button");
    hide.type = "button";
    hide.className = "program-hide";
    hide.dataset.programAction = "hide";
    hide.dataset.programId = item.id;
    actions.appendChild(hide);
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
    // Duplicate and delete sit in the "⋮" menu (#program-menu).
    const menu = document.createElement("button");
    menu.type = "button";
    menu.className = "program-menu-btn";
    menu.dataset.programAction = "menu";
    menu.dataset.programId = item.id;
    menu.title = "Duplizieren oder löschen";
    menu.setAttribute("aria-haspopup", "menu");
    menu.setAttribute("aria-expanded", "false");
    menu.innerHTML = ICON_DOTS;
    actions.appendChild(menu);
  }
  programCell(row, "Aktion", actions, "program-actions-cell");
  if (!draft) updateProgramRow(row, item);
  return row;
}

// Bring an existing row up to date in place. The field that has the focus and
// rows with an edit still being saved keep what the user typed.
function updateProgramRow(row, item) {
  const editing = PROGRAM_SAVING.has(item.id) || PROGRAM_RESAVE.has(item.id);
  const groupOptions = programGroupOptions();
  for (const control of row.querySelectorAll("[data-program-field]")) {
    const field = control.dataset.programField;
    // "Datei oder Pfad…" stays chosen while its path is still being entered.
    const keep = editing || control === document.activeElement || control.value === FILE_CHOOSE;
    if (control.tagName === "SELECT" && control.dataset.programCamera) {
      const value = keep ? control.value : (item[field] ? String(item[field]) : "");
      setSelectOptions(control, PROGRAM_CAMERA_PRESETS[control.dataset.programCamera], control.dataset.placeholder, value);
    } else if (control.tagName === "SELECT") {
      const value = keep ? control.value : item[field];
      if (field === "light") {
        setSelectOptions(control, programLightOptions(value), NO_PROGRAM_LIGHT, value);
        continue;
      }
      const options = field === "group" ? groupOptions : programActionOptions(value);
      setSelectOptions(control, options, field === "group" ? NO_PROGRAM_GROUP : "Keine Aktion", value);
      if (field !== "group") syncFileAction(control);
    } else if (!keep) {
      control.value = item[field] ?? "";
    }
  }
  const picker = row.querySelector(".song-picker");
  if (picker) syncSongPicker(picker);
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
  if (menu) menu.setAttribute("aria-label", `${label}: duplizieren oder löschen`);
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
      for (const field of ["playback", "translation", "lyrics"]) {
        const sel = draft.querySelector(`[data-program-field="${field}"]`);
        setSelectOptions(sel, programActionOptions(sel.value), "Keine Aktion", sel.value);
        syncFileAction(sel);
      }
    }
    wanted.push(draft);
  } else if (draft) {
    draft.remove();
  }
  // Only move rows that are out of place: moving a row would blur its field.
  wanted.forEach((row, i) => { if (tbody.children[i] !== row) tbody.insertBefore(row, tbody.children[i] || null); });
  $("program-empty").hidden = PROGRAM_ITEMS.length > 0 || PROGRAM_DRAFT;
  updateProgramCue();
}

async function loadProgramItems() {
  const [items, groups, buttons, desktop, status, scenes] = await Promise.all([
    api("GET", "/program/items"), api("GET", "/groups"), api("GET", "/companion/buttons"),
    api("GET", "/desktop/profiles"), api("GET", "/program/status"), api("GET", "/lights/scenes"),
  ]);
  if (scenes.ok && Array.isArray(scenes.data.scenes)) PROGRAM_LIGHT_SCENES = scenes.data.scenes;
  if (status.ok && status.data) PROGRAM_STATUS = status.data;
  if (!items.ok || !Array.isArray(items.data) || !groups.ok || !buttons.ok || !desktop.ok) {
    $("program-empty").hidden = false;
    $("program-empty").textContent = "Programmdaten konnten nicht geladen werden";
    return;
  }
  $("program-empty").textContent = "Noch keine Programmpunkte.";
  PROGRAM_ITEMS = items.data;
  PROGRAM_GROUPS = groups.data;
  // Camera presets and the light and mixer keys left Steuerung but program
  // rows can still run them.
  PROGRAM_BUTTONS = [...(buttons.data.buttons || []), ...(buttons.data.camera_buttons || []), ...(buttons.data.catalog_buttons || [])];
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
  for (const field of ["playback", "translation", "lyrics", "song", "light"]) {
    if (result[field] === FILE_CHOOSE) result[field] = null;  // file chosen, path not entered yet
    result[field] ||= null;
  }
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
async function updateProgramItem(row) {
  const itemId = row.dataset.programId;
  if (!itemId) return;
  if (PROGRAM_SAVING.has(itemId)) { PROGRAM_RESAVE.add(itemId); return; }
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
  } else {
    await loadProgramItems();  // show what the service really has
  }
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

function programPlayToast(result) {
  const title = result.data && result.data.title ? `„${result.data.title}“` : "Programmpunkt";
  // What the camera part said is in this toast; later outcomes come from the status.
  const cameraSeq = result.data && result.data.camera && result.data.camera.seq;
  if (typeof cameraSeq === "number") PROGRAM_CAMERA_SEQ = Math.max(PROGRAM_CAMERA_SEQ ?? 0, cameraSeq);
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
$("program-reset-btn").addEventListener("click", async event => {
  const button = event.currentTarget;
  if (!(await confirmDelete(button, "Startzeiten und Verzögerungen aller Punkte vergessen? „Weiter“ beginnt dann wieder oben.", "Zurücksetzen"))) return;
  const result = await api("POST", "/program/reset");
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  toast("Ablauf zurückgesetzt", true);
  await loadProgramItems();
});

$("program-add-btn").addEventListener("click", () => {
  PROGRAM_DRAFT = true;
  renderProgramItems();
  document.querySelector('#program-items .program-draft-row [data-program-field="title"]')?.focus();
});
$("program-items").addEventListener("change", event => {
  const field = event.target.closest("[data-program-field]");
  if (field && field.dataset.programField === "time") field.value = normalizeProgramTime(field.value);
  const row = field && field.closest("tr");
  if (row && row.dataset.programId) updateProgramItem(row);
});
$("program-items").addEventListener("click", event => {
  const button = event.target.closest("[data-program-action]");
  if (!button) return;
  const action = button.dataset.programAction;
  if (action === "create") return createProgramItem(button.closest("tr"));
  if (action === "cancel") { PROGRAM_DRAFT = false; renderProgramItems(); return; }
  if (action === "menu") {
    if (button.getAttribute("aria-expanded") === "true") closeTileMenu();
    else openTileMenu(button, $("program-menu"));
    return;
  }
  if (action === "hide") return toggleProgramHidden(button.dataset.programId, button);
  if (action === "play") return playProgramItem(button.dataset.programId, button);
});
$("program-menu").addEventListener("click", event => {
  const entry = event.target.closest("button[data-menu-action]");
  if (!entry || !OPEN_MENU) return;
  // The row's "⋮" key anchors the delete question once the menu is gone.
  const trigger = OPEN_MENU.trigger;
  closeTileMenu();
  if (entry.dataset.menuAction === "clone") cloneProgramItem(trigger.dataset.programId, trigger);
  else if (entry.dataset.menuAction === "delete") deleteProgramItem(trigger.dataset.programId, trigger);
});
