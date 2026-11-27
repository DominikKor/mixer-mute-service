// Mixer Mute Service UI (controls.js): Steuerung: keys and categories, beamer, power off.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Steuerung (control keys) ----------------------------------------------
// Keys whose press is still running. Only the same key waits; any other key
// works at once (one global flag swallowed every press while an Overall ran).
const COMPANION_BUTTON_BUSY = new Set();
const COMPANION_CATEGORY_BUSY = new Set();
let COMPANION_DATA = { categories: [], buttons: [], companion_targets: [] };
let COMPANION_GROUPS = {};
let BEAMER_HIDDEN = null;
let POWER_OFF_CONFIG = null;
let POWER_OFF_CONFIG_SIGNATURE = "";
let POWER_OFF_CONFIG_BUSY = false;
let POWER_OFF_CONFIG_SAVE_PENDING = false;
let POWER_OFF_BUSY = false;

function syncBeamerCommandButtons() {
  const known = typeof BEAMER_HIDDEN === "boolean";
  for (const button of document.querySelectorAll('[data-companion-press="beamer-hide"], [data-companion-press="beamer-unhide"]')) {
    button.disabled = !known;
    button.title = known ? "Zustandsbewusst ausführen" : "Zuerst den aktuellen Beamerzustand festlegen";
  }
}

// The beamer's freeze as the service knows it: true, false, or null (unknown:
// program steps then send nothing). Shown in the Beamer section and the program.
let BEAMER_FROZEN = null;

function renderBeamerState(data) {
  BEAMER_FROZEN = data && typeof data.frozen === "boolean" ? data.frozen : null;
  document.querySelectorAll("[data-beamer-frozen]").forEach(button => {
    button.setAttribute("aria-pressed", String(BEAMER_FROZEN === (button.dataset.beamerFrozen === "true")));
  });
  $("beamer-frozen-badge").hidden = BEAMER_FROZEN !== true;
  // Program head: frozen in amber; unknown quietly (a freeze step would send nothing).
  const badge = $("program-freeze-badge");
  badge.hidden = BEAMER_FROZEN === false || !data;
  badge.classList.toggle("unknown", BEAMER_FROZEN === null);
  badge.textContent = BEAMER_FROZEN === null ? "Freeze: unbekannt" : "Beamer eingefroren";
  badge.title = BEAMER_FROZEN === null
    ? "Der Dienst weiß nicht, ob der Beamer eingefroren ist: Freeze-Schritte senden nichts. In Steuerung unter „Freeze-Zustand festlegen“ setzen."
    : "Der Beamer ist eingefroren: erst ein Schritt „Beamer auftauen“ oder die Taste „Beamer freeze“ zeigt wieder Neues";
  BEAMER_HIDDEN = data && typeof data.hidden === "boolean" ? data.hidden : null;
  const label = BEAMER_HIDDEN === null ? "Zustand unbekannt" : `Ist-Zustand: ${BEAMER_HIDDEN ? "Hide" : "Unhide"}`;
  $("beamer-state-current").textContent = data && data.ok === false ? "Status nicht erreichbar" : label;
  document.querySelectorAll("[data-beamer-state]").forEach(button => {
    button.setAttribute("aria-pressed", String(BEAMER_HIDDEN === (button.dataset.beamerState === "true")));
  });
  syncBeamerCommandButtons();
}

function renderPowerOffConfig(data) {
  if (POWER_OFF_CONFIG_BUSY) return;
  const config = data && Array.isArray(data.available_targets) ? data : null;
  POWER_OFF_CONFIG = config;
  const signature = JSON.stringify(config);
  if (signature === POWER_OFF_CONFIG_SIGNATURE) {
    const selected = new Set(config ? config.selected_targets || [] : []);
    $("power-off-selected").disabled = POWER_OFF_CONFIG_BUSY || POWER_OFF_BUSY
      || !config || (!selected.size && config.beamer !== true);
    return;
  }
  POWER_OFF_CONFIG_SIGNATURE = signature;
  const targets = $("power-off-targets");
  targets.replaceChildren();
  if (!config) {
    $("power-off-status").textContent = "Abschaltauswahl nicht erreichbar";
    $("power-off-status").classList.add("bad");
    $("power-off-selected").disabled = true;
    return;
  }
  const selected = new Set(config.selected_targets || []);
  // One key per target; tapping it toggles whether "Ausschalten" includes it
  // (a hidden checkbox inside the key carries the state).
  const addTarget = (name, meta, checked, data, nameId, defaultName) => {
    const tile = document.createElement("div");
    tile.className = "power-tile";
    tile.dataset.powerName = nameId;
    tile.dataset.name = name;
    tile.dataset.defaultName = defaultName;
    const key = document.createElement("label");
    key.className = "power-off-target";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    input.disabled = POWER_OFF_CONFIG_BUSY;
    Object.assign(input.dataset, data);
    if (data.powerTarget) input.value = data.powerTarget;
    const title = document.createElement("span");
    title.className = "power-off-name";
    title.textContent = name;
    const sub = document.createElement("span");
    sub.className = "power-off-meta";
    sub.textContent = meta;
    key.title = `${name}: ${checked ? "wird mit ausgeschaltet" : "bleibt an"} – tippen zum Umschalten`;
    key.append(input, title, sub);
    tile.append(key);
    // Renaming (its "⋮", shown with "Bearbeiten"): stored on the service for all devices.
    const menu = menuButton(`${name}: umbenennen`, `power:${nameId}`);
    menu.classList.add("tile-menu-btn", "edit-only");
    menu.dataset.powerMenu = nameId;
    tile.append(menu);
    targets.append(tile);
    return tile;
  };
  addTarget(config.beamer_label || "Beamer", "Projektor", config.beamer === true, { powerBeamer: "true" }, "beamer", "Beamer");
  for (const target of config.available_targets) {
    const tile = addTarget(target.label, target.ip, selected.has(target.ip), { powerTarget: target.ip },
      target.ip, target.default_label || target.label);
    tile.classList.add("agent");
    const shutdown = document.createElement("button");
    shutdown.type = "button";
    shutdown.className = "power-agent-shutdown";
    shutdown.dataset.powerAgentShutdown = target.ip;
    shutdown.title = `Nur ${target.label} jetzt herunterfahren (nach 10 Sekunden)`;
    shutdown.setAttribute("aria-label", shutdown.title);
    shutdown.innerHTML = ICON_POWER;
    tile.append(shutdown);
  }
  const status = $("power-off-status");
  status.classList.remove("bad");
  const count = selected.size + (config.beamer === true ? 1 : 0);
  status.textContent = (config.available_targets.length ? "" : "Keine Agent-PCs in den Desktop-Profilen. ")
    + (count ? `${count} ${count === 1 ? "Gerät" : "Geräte"} rot markiert · Agent-PCs fahren 10 s nach dem Befehl herunter`
      : "Geräte antippen, die „Ausschalten“ mit ausschalten soll");
  $("power-off-selected").disabled = POWER_OFF_CONFIG_BUSY || POWER_OFF_BUSY || !count;
}

async function loadPowerOffConfig() {
  const result = await api("GET", "/power/off-config");
  renderPowerOffConfig(result.ok ? result.data : null);
}

async function savePowerOffConfig() {
  if (POWER_OFF_CONFIG_BUSY) {
    POWER_OFF_CONFIG_SAVE_PENDING = true;
    return;
  }
  const body = {
    target_ips: [...new Set([...document.querySelectorAll("[data-power-target]:checked")].map(input => input.dataset.powerTarget))].sort(),
    beamer: Boolean(document.querySelector("[data-power-beamer]")?.checked),
  };
  const savedTargets = [...(POWER_OFF_CONFIG?.selected_targets || [])].sort();
  if (POWER_OFF_CONFIG && JSON.stringify(body.target_ips) === JSON.stringify(savedTargets)
      && body.beamer === POWER_OFF_CONFIG.beamer) {
    POWER_OFF_CONFIG_SAVE_PENDING = false;
    $("power-off-selected").disabled = POWER_OFF_BUSY || (!body.target_ips.length && !body.beamer);
    return;
  }
  POWER_OFF_CONFIG_BUSY = true;
  $("power-off-selected").disabled = true;
  const result = await api("PUT", "/power/off-config", body);
  POWER_OFF_CONFIG_BUSY = false;
  if (!result.ok || result.data.ok === false) {
    POWER_OFF_CONFIG_SAVE_PENDING = false;
    toast(result.data.reason || `HTTP ${result.status}`, false);
    POWER_OFF_CONFIG_SIGNATURE = "";
    await loadPowerOffConfig();
    return;
  }
  POWER_OFF_CONFIG = {
    ...POWER_OFF_CONFIG,
    selected_targets: result.data.selected_targets,
    beamer: result.data.beamer,
  };
  if (POWER_OFF_CONFIG_SAVE_PENDING) {
    POWER_OFF_CONFIG_SAVE_PENDING = false;
    await savePowerOffConfig();
    return;
  }
  POWER_OFF_CONFIG_SIGNATURE = "";
  renderPowerOffConfig(POWER_OFF_CONFIG);
  toast("Abschaltauswahl gespeichert", true);
}

// ---- OBS live colours (GET /obs/status) -------------------------------------
// As on the Companion buttons: a key whose scene is on air in OBS is red, one
// in the preview (studio mode) green. The transition keys show OBS's
// transition time. One status call per poll for all keys; while OBS does not
// answer no key is coloured.
let OBS_STATUS = null;

// The OBS scene a key puts on air, or whether it sets the transition time.
function obsKeyRole(info) {
  let action = info.native_action;
  if (info.kind === "companion") {
    const target = (COMPANION_DATA.companion_targets || []).find(item => item.id === info.target_button_id);
    action = target && target.native_action;
  }
  if (!action) return {};
  if (action.type === "obs-transition-adjust") return { transition: true };
  const placeholder = action.scene === "Current Scene" || action.scene === "Preview Scene";
  return (action.type === "obs-live" || action.type === "obs-scene") && action.scene && !placeholder ? { scene: action.scene } : {};
}

function applyObsStatus() {
  const status = OBS_STATUS && OBS_STATUS.ok ? OBS_STATUS : null;
  for (const key of document.querySelectorAll("button.companion-key[data-obs-scene]")) {
    const scene = key.dataset.obsScene;
    const program = !!status && status.program === scene;
    const preview = !!status && !program && status.preview === scene;
    key.classList.toggle("obs-program", program);
    key.classList.toggle("obs-preview", preview);
    if (program) key.setAttribute("aria-description", "live in OBS");
    else if (preview) key.setAttribute("aria-description", "in der OBS-Vorschau");
    else key.removeAttribute("aria-description");
  }
  const ms = status && status.transition ? status.transition.duration_ms : null;
  for (const key of document.querySelectorAll("button.companion-key[data-obs-transition]")) {
    if (typeof ms === "number") key.dataset.value = `${ms} ms`;
    else delete key.dataset.value;
  }
}

async function refreshObsStatus() {
  if (!document.querySelector("button.companion-key[data-obs-scene], button.companion-key[data-obs-transition]")) return;
  const result = await api("GET", "/obs/status");
  OBS_STATUS = result.ok ? result.data : null;
  applyObsStatus();
}

function renderCompanionButtons() {
  const wrap = $("companion-categories");
  wrap.replaceChildren();
  for (const category of COMPANION_DATA.categories) {
    const section = document.createElement("section");
    section.className = "companion-category";
    section.dataset.categoryId = category.id;
    const header = document.createElement("div");
    header.className = "companion-category-head";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "companion-category-toggle";
    toggle.dataset.companionCategoryToggle = category.id;
    toggle.setAttribute("aria-expanded", String(category.visible));
    toggle.textContent = category.name;
    const menu = menuButton(`Kategorie ${category.name}: umbenennen`, `category:${category.id}`);
    menu.classList.add("edit-only");
    menu.dataset.categoryMenu = category.id;
    header.append(toggle, menu);

    const grid = document.createElement("div");
    grid.className = "companion-grid";
    grid.hidden = !category.visible;
    for (const info of COMPANION_DATA.buttons.filter(item => item.category_id === category.id)) {
      const item = document.createElement("div");
      item.className = "companion-button-item";
      const press = document.createElement("button");
      press.type = "button";
      press.className = `companion-key ${info.kind === "companion" ? "remote" : info.kind}`;
      press.dataset.companionPress = info.id;
      press.textContent = info.label;
      const obs = obsKeyRole(info);
      if (obs.scene) press.dataset.obsScene = obs.scene;
      if (obs.transition) press.dataset.obsTransition = "";
      press.title = info.kind === "native" ? "Direkt über den lokalen Dienst ausführen" : "Über den sicheren Dienst ausführen";
      // With "Bearbeiten": the key's "⋮" (rename, move, delete).
      const menu = menuButton(`${info.label}: umbenennen, verschieben oder löschen`, `key:${info.id}`);
      menu.classList.add("tile-menu-btn", "edit-only");
      menu.dataset.companionMenu = info.id;
      item.append(press, menu);
      grid.appendChild(item);
    }
    section.append(header, grid);
    wrap.appendChild(section);
  }
  syncBeamerCommandButtons();
  applyObsStatus();
  renderCameraControls();
}

function replaceSelectOptions(select, options, placeholder) {
  select.replaceChildren();
  const first = document.createElement("option");
  first.value = "";
  first.textContent = placeholder;
  select.appendChild(first);
  for (const option of options) {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    select.appendChild(element);
  }
  select.disabled = options.length === 0;
}

function updateCompanionButtonForm() {
  const kind = $("companion-button-kind").value;
  let options = [];
  if (kind === "companion") {
    options = COMPANION_DATA.companion_targets.map(button => ({ value: button.id, label: button.label }));
  } else if (kind === "channel") {
    options = Object.entries(LAST_STATE.channels || {}).map(([channel, info]) => ({
      value: channel, label: `${info.name || `Channel ${channel}`} (ch ${channel})`,
    }));
  } else {
    options = Object.keys(COMPANION_GROUPS).map(group => ({ value: group, label: group }));
  }
  replaceSelectOptions($("companion-button-target"), options, "Ziel wählen …");
  $("companion-button-operation-field").hidden = kind === "companion";
}

async function loadCompanionButtons() {
  const [buttons, groups] = await Promise.all([api("GET", "/companion/buttons"), api("GET", "/groups")]);
  if (!buttons.ok || !Array.isArray(buttons.data.buttons)) {
    $("companion-categories").textContent = "Steuerungs-Tasten konnten nicht geladen werden";
    return;
  }
  COMPANION_DATA = { categories: [], companion_targets: [], ...buttons.data };
  if (groups.ok) COMPANION_GROUPS = groups.data;
  renderCompanionButtons();
}

async function saveCompanionCategory(event) {
  event.preventDefault();
  const name = $("companion-category-name").value.trim();
  const result = await api("POST", "/companion/categories", { name });
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  $("companion-category-dialog").close();
  await loadCompanionButtons();
  toast(`Kategorie ${name} erstellt`, true);
}

async function saveCompanionButton(event) {
  event.preventDefault();
  const kind = $("companion-button-kind").value;
  const body = { label: $("companion-button-name").value.trim(), category_id: $("companion-button-category").value, kind };
  if (kind === "companion") body.target_button_id = $("companion-button-target").value;
  else if (kind === "channel") {
    body.channel = Number($("companion-button-target").value);
    body.operation = $("companion-button-operation").value;
  } else {
    body.group = $("companion-button-target").value;
    body.operation = $("companion-button-operation").value;
  }
  const result = await api("POST", "/companion/buttons", body);
  if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
  $("companion-button-dialog").close();
  await loadCompanionButtons();
  toast("Taste erstellt", true);
}

function openSnapshotCreateRow() {
  $("snapshot-create-row").classList.add("visible");
  $("snapshot-name").value = "";
  $("snapshot-name").focus();
}

function closeSnapshotCreateRow() {
  $("snapshot-create-row").classList.remove("visible");
  $("snapshot-name").value = "";
}

async function saveCurrentMuteSnapshot() {
  const name = $("snapshot-name").value.trim();
  if (!name) { toast("Bitte einen Namen eingeben", false); return; }
  const persons = loadPersons();
  if (persons.some(person => person.name.trim().toLocaleLowerCase() === name.toLocaleLowerCase())) {
    toast("Eine Mute Gruppe mit diesem Namen existiert bereits", false);
    return;
  }

  const saveButton = $("snapshot-save-btn");
  saveButton.disabled = true;
  try {
    const result = await api("POST", "/groups/from-current-status", { name });
    if (!result.ok || result.data.ok === false) {
      const details = Array.isArray(result.data.details)
        ? `: ${result.data.details.map(item => `ch${item.channel} ${item.reason}`).join(", ")}`
        : "";
      toast(`Mute Gruppe nicht erstellt: ${result.data.reason || "HTTP " + result.status}${details}`, false);
      return;
    }
    const channels = {};
    for (const channel of result.data.channels) {
      channels[String(channel)] = { fader_hex: result.data.fader_hex[String(channel)], set_at: Date.now() };
    }
    persons.push({ id: genId(), name: result.data.group, channels });
    savePersons(persons);
    closeSnapshotCreateRow();
    renderPersons();
    toast(`Mute Gruppe „${name}“ mit ${result.data.channels.length} ungemuteten Channels erstellt`, true);
  } catch (_) {
    toast("Mute Gruppe konnte nicht erstellt werden", false);
  } finally {
    saveButton.disabled = false;
  }
}

$("new-companion-category").addEventListener("click", () => {
  $("companion-category-name").value = "";
  $("companion-category-dialog").showModal();
  $("companion-category-name").focus();
});
$("new-companion-button").addEventListener("click", () => {
  replaceSelectOptions($("companion-button-category"), COMPANION_DATA.categories.map(category => ({ value: category.id, label: category.name })), "Kategorie wählen …");
  $("companion-button-kind").value = "companion";
  $("companion-button-name").value = "";
  updateCompanionButtonForm();
  $("companion-button-dialog").showModal();
  $("companion-button-name").focus();
});
$("companion-category-form").addEventListener("submit", saveCompanionCategory);
$("companion-button-form").addEventListener("submit", saveCompanionButton);
$("companion-button-kind").addEventListener("change", updateCompanionButtonForm);
$("companion-category-cancel").addEventListener("click", () => $("companion-category-dialog").close());
$("companion-button-cancel").addEventListener("click", () => $("companion-button-dialog").close());

$("companion-categories").addEventListener("click", async (event) => {
  const categoryMenu = event.target.closest("[data-category-menu]");
  if (categoryMenu) {
    const category = COMPANION_DATA.categories.find(item => item.id === categoryMenu.dataset.categoryMenu);
    if (!category) return;
    toggleActionMenu(categoryMenu, [{ label: "Umbenennen", icon: ICON_PENCIL, run: () => {
      const head = $("companion-categories").querySelector(`[data-category-id="${CSS.escape(category.id)}"] .companion-category-head`);
      if (head) startInlineRename(head, category.name, { save: async name => {
        if (await saveName("POST", `/companion/categories/${encodeURIComponent(category.id)}/rename`, name, "Kategorie")) {
          await loadCompanionButtons();
        }
      } });
    } }]);
    return;
  }
  const keyMenu = event.target.closest("[data-companion-menu]");
  if (keyMenu) {
    const info = COMPANION_DATA.buttons.find(button => button.id === keyMenu.dataset.companionMenu);
    if (info) toggleActionMenu(keyMenu, () => companionKeyMenuItems(info));
    return;
  }
  const toggle = event.target.closest("[data-companion-category-toggle]");
  if (toggle) {
    const category = COMPANION_DATA.categories.find(item => item.id === toggle.dataset.companionCategoryToggle);
    if (!category || COMPANION_CATEGORY_BUSY.has(category.id)) return;
    COMPANION_CATEGORY_BUSY.add(category.id);
    toggle.disabled = true;
    try {
      const result = await api("POST", `/companion/categories/${encodeURIComponent(category.id)}/visibility`, { visible: !category.visible });
      if (!result.ok || result.data.ok === false) toast(result.data.reason || `HTTP ${result.status}`, false);
      else await loadCompanionButtons();
    } finally {
      COMPANION_CATEGORY_BUSY.delete(category.id);
      if (toggle.isConnected) toggle.disabled = false;
    }
    return;
  }
  const button = event.target.closest("[data-companion-press]");
  if (!button) return;
  // "Bearbeiten": a key never fires (a tap that misses its "⋮" by a few
  // pixels would otherwise switch the mixer, OBS or the beamer); it opens
  // the key's menu instead.
  if (companionEditing()) {
    const info = COMPANION_DATA.buttons.find(item => item.id === button.dataset.companionPress);
    const menu = button.closest(".companion-button-item")?.querySelector("[data-companion-menu]");
    if (info && menu) toggleActionMenu(menu, () => companionKeyMenuItems(info));
    return;
  }
  const pressId = button.dataset.companionPress;
  if (COMPANION_BUTTON_BUSY.has(pressId)) return;
  COMPANION_BUTTON_BUSY.add(pressId);
  button.disabled = true;
  try {
    const result = await api("POST", `/companion/buttons/${encodeURIComponent(pressId)}/press`);
    if (!result.ok || result.data.ok === false) toast(`${button.textContent}: ${result.data.reason || "HTTP " + result.status}`, false);
    else {
      const isBeamerMode = button.dataset.companionPress === "beamer-hide" || button.dataset.companionPress === "beamer-unhide";
      // Beamer on/off also set the remembered state (a started beamer is un-hidden).
      const isBeamerPower = button.dataset.companionPress === "beamer-power-on" || button.dataset.companionPress === "beamer-power-off";
      if (isBeamerMode || isBeamerPower) {
        const state = await api("GET", "/beamer/state");
        renderBeamerState(state.ok ? state.data : null);
      }
      if (isBeamerMode) {
        toast(result.data.changed === false ? `${button.textContent}: bereits gesetzt, kein Toggle` : `${button.textContent}: ausgeführt`, true);
      } else {
        // Overall: the camera cut follows after the wait; say so.
        const camera = result.data.camera && result.data.camera.message;
        toast(`${button.textContent}: ausgeführt${camera ? ` – ${camera}` : ""}`, true);
        // Done, but something needs a look (e.g. an OBS source was renamed).
        if (result.data.warning) toast(`${button.textContent}: ${result.data.warning}`, false);
      }
      if (button.classList.contains("channel") || button.classList.contains("group")) await refresh();
      // Show the cut (or the new transition time) without waiting for the poll.
      if (button.dataset.obsScene !== undefined || button.dataset.obsTransition !== undefined) refreshObsStatus();
    }
  } finally {
    COMPANION_BUTTON_BUSY.delete(pressId);
    button.disabled = (button.dataset.companionPress === "beamer-hide" || button.dataset.companionPress === "beamer-unhide")
      && typeof BEAMER_HIDDEN !== "boolean";
  }
});
$("beamer-state-control").addEventListener("click", async (event) => {
  const frozenKey = event.target.closest("[data-beamer-frozen]");
  if (frozenKey && !frozenKey.disabled) {
    const frozen = frozenKey.dataset.beamerFrozen === "true";
    frozenKey.disabled = true;
    const result = await api("POST", "/beamer/state", { frozen });
    frozenKey.disabled = false;
    if (!result.ok || result.data.ok === false) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
    const state = await api("GET", "/beamer/state");
    renderBeamerState(state.ok ? state.data : null);
    toast(`Freeze-Zustand auf ${frozen ? "eingefroren" : "live"} gesetzt`, true);
    return;
  }
  const button = event.target.closest("[data-beamer-state]");
  if (!button || button.disabled) return;
  const hidden = button.dataset.beamerState === "true";
  button.disabled = true;
  const result = await api("POST", "/beamer/state", { hidden });
  button.disabled = false;
  if (!result.ok || result.data.ok === false) {
    toast(result.data.reason || `HTTP ${result.status}`, false);
    return;
  }
  const state = await api("GET", "/beamer/state");
  renderBeamerState(state.ok ? state.data : null);
  toast(`Beamer-Ist-Zustand auf ${hidden ? "Hide" : "Unhide"} gesetzt`, true);
});
$("power-off-targets").addEventListener("change", event => {
  if (event.target.matches("[data-power-target], [data-power-beamer]")) savePowerOffConfig();
});
$("power-off-targets").addEventListener("click", async event => {
  const menu = event.target.closest("[data-power-menu]");
  if (menu) {
    toggleActionMenu(menu, [{ label: "Umbenennen", icon: ICON_PENCIL, run: anchor => {
      const tile = (anchor || menu).closest(".power-tile");
      if (!tile) return;
      const current = tile.dataset.name === tile.dataset.defaultName ? "" : tile.dataset.name;
      startInlineRename(tile, current, {
        placeholder: tile.dataset.defaultName, allowEmpty: true,
        save: async name => {
          if (await saveName("PUT", `/names/power-target/${encodeURIComponent(tile.dataset.powerName)}`, name, "Name")) {
            await loadPowerOffConfig();
          }
        },
      });
    } }]);
    return;
  }
  const button = event.target.closest("button[data-power-agent-shutdown]");
  if (!button || button.disabled) return;
  const targetIp = button.dataset.powerAgentShutdown;
  const target = POWER_OFF_CONFIG?.available_targets.find(item => item.ip === targetIp);
  if (!target || !(await confirmDelete(button, `${target.label} (${targetIp}) in 10 Sekunden herunterfahren?`, "Herunterfahren"))) return;
  button.disabled = true;
  const result = await api("POST", `/power/agents/${encodeURIComponent(targetIp)}/shutdown`);
  button.disabled = false;
  if (!result.ok || result.data.ok === false) toast(result.data.reason || `HTTP ${result.status}`, false);
  else toast(`${target.label}: Herunterfahren eingeleitet`, true);
});
$("power-off-selected").addEventListener("click", async event => {
  const button = event.currentTarget;
  if (POWER_OFF_BUSY || button.disabled) return;
  if (!(await confirmDelete(button, "Ausgewählte Agent-PCs und den Beamer wirklich ausschalten?", "Ausschalten"))) return;
  POWER_OFF_BUSY = true;
  button.disabled = true;
  const result = await api("POST", "/power/off");
  POWER_OFF_BUSY = false;
  if (result.ok && result.data.ok !== false) {
    toast("Ausschalten wurde eingeleitet", true);
  } else {
    const failed = (result.data.results || []).filter(item => item.ok === false);
    const details = failed.map(item => `${item.target}: ${item.reason || "nicht erreichbar"}`).join(", ");
    toast(details || result.data.reason || `HTTP ${result.status}`, false);
  }
  await refresh();
});
function companionEditing() {
  return $("companion-categories").classList.contains("editing");
}

// A key's "⋮" (with "Bearbeiten"): rename, move to another category, delete.
function companionKeyMenuItems(info) {
  const others = COMPANION_DATA.categories.filter(category => category.id !== info.category_id);
  return [
    { label: "Umbenennen", icon: ICON_PENCIL, run: () => renameCompanionKey(info) },
    others.length ? "-" : null,
    ...others.map(category => ({ label: `Nach „${category.name}“ verschieben`, icon: ICON_MOVE,
      run: () => updateCompanionKey(info, { category_id: category.id }) })),
    "-",
    { label: "Löschen", icon: ICON_TRASH, danger: true, run: anchor => deleteCompanionKey(info, anchor) },
  ];
}

async function updateCompanionKey(info, change) {
  const body = { label: info.label, category_id: info.category_id, ...change };
  const result = await api("POST", `/companion/buttons/${encodeURIComponent(info.id)}/update`, body);
  if (!result.ok || result.data.ok === false) toast(result.data.reason || `HTTP ${result.status}`, false);
  await loadCompanionButtons();
}

function renameCompanionKey(info) {
  const press = $("companion-categories").querySelector(`[data-companion-press="${CSS.escape(info.id)}"]`);
  if (!press) return;
  startInlineRename(press.closest(".companion-button-item"), info.label, { maxLength: 40, save: async name => {
    await updateCompanionKey(info, { label: name });
  } });
}

async function deleteCompanionKey(info, anchor) {
  if (!(await confirmDelete(anchor, `Taste „${info.label}“ löschen?`))) return;
  const result = await api("POST", `/companion/buttons/${encodeURIComponent(info.id)}/delete`);
  if (!result.ok || result.data.ok === false) toast(result.data.reason || `HTTP ${result.status}`, false);
  else { await loadCompanionButtons(); toast(`Taste „${info.label}“ gelöscht`, true); }
}
