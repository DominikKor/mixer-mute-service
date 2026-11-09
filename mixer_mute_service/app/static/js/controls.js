// Mixer Mute Service UI (controls.js): Steuerung: keys and categories, beamer, power off.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Steuerung (control keys) ----------------------------------------------
let COMPANION_BUTTON_BUSY = false;
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

function renderBeamerState(data) {
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
  const addTarget = (name, meta, checked, data) => {
    const tile = document.createElement("div");
    tile.className = "power-tile";
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
    targets.append(tile);
    return tile;
  };
  addTarget("Beamer", "Projektor", config.beamer === true, { powerBeamer: "true" });
  for (const target of config.available_targets) {
    const tile = addTarget(target.label, target.ip, selected.has(target.ip), { powerTarget: target.ip });
    tile.classList.add("agent");
    const shutdown = document.createElement("button");
    shutdown.type = "button";
    shutdown.className = "power-agent-shutdown";
    shutdown.dataset.powerAgentShutdown = target.ip;
    shutdown.title = `Nur ${target.label} jetzt herunterfahren (nach 10 Sekunden)`;
    shutdown.setAttribute("aria-label", shutdown.title);
    shutdown.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 3v8M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>';
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
    header.appendChild(toggle);

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
      press.title = info.kind === "native" ? "Direkt über den lokalen Dienst ausführen" : "Über den sicheren Dienst ausführen";
      const tools = document.createElement("div");
      tools.className = "companion-button-tools";
      const categorySelect = document.createElement("select");
      categorySelect.className = "companion-category-select";
      categorySelect.dataset.companionCategorySelect = info.id;
      categorySelect.setAttribute("aria-label", `${info.label}: Kategorie`);
      for (const option of COMPANION_DATA.categories) {
        const element = document.createElement("option");
        element.value = option.id;
        element.textContent = option.name;
        element.selected = option.id === category.id;
        categorySelect.appendChild(element);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "companion-delete danger";
      remove.dataset.companionDelete = info.id;
      remove.setAttribute("aria-label", `${info.label} löschen`);
      remove.title = "Button löschen";
      remove.innerHTML = ICON_TRASH;
      tools.append(categorySelect, remove);
      item.append(press, tools);
      grid.appendChild(item);
    }
    section.append(header, grid);
    wrap.appendChild(section);
  }
  syncBeamerCommandButtons();
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
  toast("Button erstellt", true);
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
  const remove = event.target.closest("[data-companion-delete]");
  if (remove) {
    const info = COMPANION_DATA.buttons.find(button => button.id === remove.dataset.companionDelete);
    if (!info || !(await confirmDelete(remove, `Button „${info.label}“ löschen?`))) return;
    const result = await api("POST", `/companion/buttons/${encodeURIComponent(info.id)}/delete`);
    if (!result.ok || result.data.ok === false) toast(result.data.reason || `HTTP ${result.status}`, false);
    else { await loadCompanionButtons(); toast(`Button ${info.label} gelöscht`, true); }
    return;
  }
  const button = event.target.closest("[data-companion-press]");
  if (!button || COMPANION_BUTTON_BUSY) return;
  COMPANION_BUTTON_BUSY = true;
  button.disabled = true;
  try {
    const result = await api("POST", `/companion/buttons/${encodeURIComponent(button.dataset.companionPress)}/press`);
    if (!result.ok || result.data.ok === false) toast(`${button.textContent}: ${result.data.reason || "HTTP " + result.status}`, false);
    else {
      const isBeamerMode = button.dataset.companionPress === "beamer-hide" || button.dataset.companionPress === "beamer-unhide";
      if (isBeamerMode) {
        const state = await api("GET", "/beamer/state");
        renderBeamerState(state.ok ? state.data : null);
        toast(result.data.changed === false ? `${button.textContent}: bereits gesetzt, kein Toggle` : `${button.textContent}: ausgeführt`, true);
      } else {
        toast(`${button.textContent}: ausgeführt`, true);
      }
      if (button.classList.contains("channel") || button.classList.contains("group")) await refresh();
    }
  } finally {
    COMPANION_BUTTON_BUSY = false;
    button.disabled = (button.dataset.companionPress === "beamer-hide" || button.dataset.companionPress === "beamer-unhide")
      && typeof BEAMER_HIDDEN !== "boolean";
  }
});
$("beamer-state-control").addEventListener("click", async (event) => {
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
  renderBeamerState(result.data);
  toast(`Beamer-Ist-Zustand auf ${hidden ? "Hide" : "Unhide"} gesetzt`, true);
});
$("power-off-targets").addEventListener("change", event => {
  if (event.target.matches("[data-power-target], [data-power-beamer]")) savePowerOffConfig();
});
$("power-off-targets").addEventListener("click", async event => {
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
$("companion-categories").addEventListener("change", async (event) => {
  const select = event.target.closest("[data-companion-category-select]");
  if (!select) return;
  const button = COMPANION_DATA.buttons.find(item => item.id === select.dataset.companionCategorySelect);
  if (!button) return;
  const result = await api("POST", `/companion/buttons/${encodeURIComponent(button.id)}/update`, { label: button.label, category_id: select.value });
  if (!result.ok || result.data.ok === false) toast(result.data.reason || `HTTP ${result.status}`, false);
  await loadCompanionButtons();
});
