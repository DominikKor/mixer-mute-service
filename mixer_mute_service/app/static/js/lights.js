// Mixer Mute Service UI (lights.js): Licht: scenes, stage zones (Art-Net),
// room light (Shelly) and the light settings.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

const LIGHTS = {
  data: null,           // last GET /lights
  dragging: new Set(),  // sliders the user holds: the poll leaves them alone
  sending: {},          // per slider: {busy, again, released} of its one request in flight
  busy: new Set(),      // keys with a request in flight
  pick: new Set(),      // new scene: chosen lights ("zone:left", "color", "circuit:light-tech")
  circuitWant: {},      // room light: state the user chose, not yet confirmed (shown at once)
  circuitSent: {},      // room light: state in flight per circuit
  circuitSeq: 0,        // counts switches: a poll begun before one keeps the confirmed states
};
const LIGHT_SEND_MS = 60;   // while dragging, at most one send per this time
const LIGHT_POLL_MS = 3000;

// Fades run while the request is open (up to 10 s), a Shelly may take
// 2.5 s: a light request waits longer than api()'s default.
function lightTimeout() {
  return ((LIGHTS.data && LIGHTS.data.stage.fade_ms) || 1000) + 15000;
}

function lightPercent(value) { return `${Math.round((value || 0) * 100 / 255)} %`; }

function lightStatus(message) {
  $("light-status").hidden = !message;
  $("light-status").textContent = message || "";
}

// ---- stage and settings sliders ------------------------------------------------

// One slider row: name, optional on/off key, range, value. kind decides what
// a change sends ("zone", "warm"/"cold" or a setting).
function lightSliderRow(container, id, label, kind, max, format, onKey = false) {
  let row = container.querySelector(`[data-light-row="${id}"]`);
  if (row) return row;
  row = document.createElement("div");
  row.className = `light-row light-row-${kind}`;
  row.dataset.lightRow = id;
  // The name; a zone's name can be changed (pencil, stored on the service).
  const nameCell = document.createElement("div");
  nameCell.className = "light-name-cell";
  const name = document.createElement("span");
  name.className = "light-name";
  name.textContent = label;
  nameCell.appendChild(name);
  if (kind === "zone") {
    const pencil = renamePencil(label);
    pencil.dataset.lightRename = `light-zone/${id}`;
    nameCell.appendChild(pencil);
  }
  row.appendChild(nameCell);
  if (onKey) {
    const key = document.createElement("button");
    key.type = "button";
    key.className = "light-zone-key";
    key.dataset.lightZoneKey = id;
    key.setAttribute("aria-label", `${label} an/aus`);
    row.appendChild(key);
  }
  const range = document.createElement("input");
  range.type = "range";
  range.className = `light-range light-range-${kind}`;
  range.min = "0";
  range.max = String(max);
  range.step = "1";
  range.dataset.lightSlider = id;
  range.dataset.lightKind = kind;
  range.setAttribute("aria-label", label);
  const value = document.createElement("span");
  value.className = "light-value";
  row.append(range, value);
  row.dataset.format = format;
  container.appendChild(row);
  return row;
}

function lightFormat(row, raw) {
  const format = row.dataset.format;
  if (format === "seconds") return `${(raw / 1000).toFixed(raw % 1000 ? 2 : 1).replace(".", ",")} s`;
  if (format === "plain") return String(raw);
  return lightPercent(raw);
}

function setLightRow(row, raw) {
  const range = row.querySelector("input");
  if (!LIGHTS.dragging.has(range.dataset.lightSlider)) range.value = String(raw);
  const shown = Number(range.value);
  row.querySelector(".light-value").textContent = lightFormat(row, shown);
  range.style.setProperty("--fill", `${(shown * 100) / Number(range.max)}%`);
  const key = row.querySelector(".light-zone-key");
  if (key) {
    const on = shown > 0;
    key.classList.toggle("on", on);
    key.setAttribute("aria-pressed", String(on));
    key.textContent = on ? "An" : "Aus";
  }
}

// A zone row after a rename: its name, the key's and slider's labels.
function lightRowName(row, label, fallback) {
  row.querySelector(".light-name").textContent = label;
  row.dataset.defaultName = fallback;
  const pencil = row.querySelector("[data-light-rename]");
  if (pencil) { pencil.title = `${label} umbenennen`; pencil.setAttribute("aria-label", pencil.title); }
  row.querySelector("input[data-light-slider]").setAttribute("aria-label", label);
  const key = row.querySelector(".light-zone-key");
  if (key) key.setAttribute("aria-label", `${label} an/aus`);
}

function renderLights() {
  const data = LIGHTS.data;
  if (!data) return;
  const stage = data.stage;
  const faders = $("light-faders");
  for (const [zone, label] of Object.entries(data.zone_labels)) {
    const row = lightSliderRow(faders, zone, label, "zone", 255, "percent", true);
    lightRowName(row, label, (data.zone_defaults || {})[zone] || label);
    setLightRow(row, stage.zones[zone] || 0);
  }
  setLightRow(lightSliderRow(faders, "warm", "Warm", "warm", 255, "percent"), stage.warm);
  setLightRow(lightSliderRow(faders, "cold", "Kalt", "cold", 255, "percent"), stage.cold);
  const settings = $("light-settings");
  setLightRow(lightSliderRow(settings, "start", "Einschalten auf", "start", 255, "percent"), stage.start);
  setLightRow(lightSliderRow(settings, "fade_ms", "Übergang", "fade_ms", 10000, "seconds"), stage.fade_ms);
  settings.querySelector('[data-light-slider="fade_ms"]').step = "250";
  setLightRow(lightSliderRow(settings, "steps", "Dimmschritt", "steps", 255, "percent"), stage.steps);
  settings.querySelector('[data-light-slider="steps"]').min = "5";
  renderLightCircuits(data.circuits);
  renderLightScenes(data.scenes);
  renderLightPick();
}

async function sendLightSlider(range, fade) {
  const kind = range.dataset.lightKind;
  const value = Number(range.value);
  let result;
  if (kind === "zone") {
    result = await api("POST", "/lights/stage", { zones: { [range.dataset.lightSlider]: value }, fade }, { timeout: lightTimeout() });
  } else if (kind === "warm" || kind === "cold") {
    result = await api("POST", "/lights/stage", { [kind]: value, fade }, { timeout: lightTimeout() });
  } else {
    result = await api("PUT", "/lights/settings", { [kind]: value });
  }
  if (!result.ok || result.data.ok === false) {
    toast(`Licht: ${result.data.reason || "HTTP " + result.status}`, false);
    return;
  }
  if (LIGHTS.data && result.data.stage) LIGHTS.data.stage = result.data.stage;
}

// One request per slider at a time, always with the slider's newest value.
// Requests that overlapped could arrive out of order and set an older value
// after a newer one: the stage jumped back and forth while dragging.
async function scheduleLightSend(range) {
  const id = range.dataset.lightSlider;
  const state = lightSendState(id);
  if (state.busy) { state.again = true; return; }
  state.busy = true;
  try {
    do {
      // At most one send per LIGHT_SEND_MS; moves meanwhile only update the value.
      const wait = LIGHT_SEND_MS - (Date.now() - state.last);
      if (wait > 0) await new Promise(resolve => setTimeout(resolve, wait));
      state.again = false;
      state.last = Date.now();
      await sendLightSlider(range, false);
    } while (state.again);
  } finally {
    state.busy = false;
    // Let go meanwhile: the poll may update this slider again.
    if (state.released) { state.released = false; LIGHTS.dragging.delete(id); }
  }
}

function lightSendState(id) {
  return LIGHTS.sending[id] || (LIGHTS.sending[id] = { busy: false, again: false, released: false, last: 0 });
}

// The slider was let go: free it for the poll now, or once its send is done.
function releaseLightSlider(id) {
  const state = lightSendState(id);
  if (state.busy) state.released = true;
  else LIGHTS.dragging.delete(id);
}

function initLightSliders(container) {
  container.addEventListener("pointerdown", event => {
    const range = event.target.closest("input[data-light-slider]");
    if (range) LIGHTS.dragging.add(range.dataset.lightSlider);
  });
  container.addEventListener("input", event => {
    const range = event.target.closest("input[data-light-slider]");
    if (!range) return;
    LIGHTS.dragging.add(range.dataset.lightSlider);
    setLightRow(range.closest(".light-row"), Number(range.value));
    // Settings are only saved once on release; the light follows the drag.
    if (!["start", "fade_ms", "steps"].includes(range.dataset.lightKind)) scheduleLightSend(range);
  });
  container.addEventListener("change", async event => {
    const range = event.target.closest("input[data-light-slider]");
    if (!range) return;
    // The final value: queued behind a send still running, never beside it.
    lightSendState(range.dataset.lightSlider).released = true;
    await scheduleLightSend(range);
  });
  // A drag that ends outside the slider (finger slid off) still lets go.
  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => {
      for (const id of [...LIGHTS.dragging]) releaseLightSlider(id);
    });
  }
  container.addEventListener("click", async event => {
    const key = event.target.closest("[data-light-zone-key]");
    if (!key || LIGHTS.busy.has(key)) return;
    const zone = key.dataset.lightZoneKey;
    const current = LIGHTS.data ? LIGHTS.data.stage.zones[zone] || 0 : 0;
    const target = current > 0 ? 0 : (LIGHTS.data ? LIGHTS.data.stage.start : 255) || 255;
    await lightRequest(key, "POST", "/lights/stage", { zones: { [zone]: target } });
  });
}

// ---- room light -------------------------------------------------------------------

function renderLightCircuits(circuits) {
  const box = $("light-circuits");
  for (const circuit of circuits) {
    let key = box.querySelector(`[data-light-circuit="${circuit.id}"]`);
    if (!key) {
      // Tile: the on/off key and its pencil (renaming, stored on the service).
      const tile = document.createElement("div");
      tile.className = "light-circuit-tile";
      key = document.createElement("button");
      key.type = "button";
      key.className = "light-circuit";
      key.dataset.lightCircuit = circuit.id;
      key.innerHTML = '<span class="light-lamp" aria-hidden="true"></span><span class="light-circuit-name"></span><span class="light-circuit-state"></span>';
      const pencil = renamePencil(circuit.label);
      pencil.dataset.lightRename = `light-circuit/${circuit.id}`;
      tile.append(key, pencil);
      box.appendChild(tile);
    }
    const tile = key.closest(".light-circuit-tile");
    tile.dataset.defaultName = circuit.default_label || circuit.label;
    tile.dataset.name = circuit.label;
    const pencil = tile.querySelector("[data-light-rename]");
    pencil.title = `${circuit.label} umbenennen`;
    pencil.setAttribute("aria-label", pencil.title);
    key.querySelector(".light-circuit-name").textContent = circuit.label;
    // A tapped key shows its new state at once; "pending" until confirmed.
    const pending = circuit.id in LIGHTS.circuitWant;
    const on = pending ? LIGHTS.circuitWant[circuit.id] : circuit.on;
    const state = on === true ? "on" : on === false ? "off" : "unknown";
    key.dataset.state = state;
    key.classList.toggle("pending", pending);
    key.querySelector(".light-circuit-state").textContent = { on: "An", off: "Aus", unknown: "Offline" }[state];
    key.setAttribute("aria-pressed", String(on === true));
    key.title = state === "unknown" ? `${circuit.label}: nicht erreichbar – antippen versucht einzuschalten` : `${circuit.label} ${state === "on" ? "ausschalten" : "einschalten"}`;
  }
}

// Room light: the keys show the new state at once, the service's answer
// confirms it (or a failure brings the real state back). The circuits that
// answer go in one request, every unreachable one in its own: a dead
// Shelly holds up nobody (and seven requests at once would queue in the
// browser). One request per circuit at a time; a tap meanwhile is sent
// after it (the last tap wins).
function switchRoomLight(states) {
  Object.assign(LIGHTS.circuitWant, states);
  LIGHTS.circuitSeq++;
  if (LIGHTS.data) renderLightCircuits(LIGHTS.data.circuits);
  sendRoomLight();
}

async function sendRoomLight() {
  const ids = Object.keys(LIGHTS.circuitWant).filter(id => !(id in LIGHTS.circuitSent));
  if (!ids.length) return;
  const known = new Set((LIGHTS.data ? LIGHTS.data.circuits : []).filter(c => c.on !== null).map(c => c.id));
  const groups = ids.filter(id => !known.has(id)).map(id => [id]);
  if (ids.some(id => known.has(id))) groups.unshift(ids.filter(id => known.has(id)));
  const failed = (await Promise.all(groups.map(sendRoomCircuits))).flat();
  if (failed.length) {
    // One message for the whole tap ("Alle an": every dead circuit named).
    const labels = Object.fromEntries((LIGHTS.data ? LIGHTS.data.circuits : []).map(c => [c.id, c.label]));
    toast(`Licht nicht geschaltet: ${failed.map(f => labels[f.id] || f.id).join(", ")} (${failed[0].reason})`, false);
  }
}

// Switch some circuits in one request; returns [{id, reason}] of those that failed.
async function sendRoomCircuits(ids) {
  const states = Object.fromEntries(ids.map(id => [id, LIGHTS.circuitWant[id]]));
  Object.assign(LIGHTS.circuitSent, states);
  const result = await api("POST", "/lights/circuits", { states }, { timeout: lightTimeout() });
  LIGHTS.circuitSeq++;
  const confirmed = result.data.circuits;
  for (const id of ids) {
    delete LIGHTS.circuitSent[id];
    if (LIGHTS.circuitWant[id] === states[id]) delete LIGHTS.circuitWant[id];
    const circuit = LIGHTS.data && LIGHTS.data.circuits.find(c => c.id === id);
    if (circuit && confirmed && id in confirmed) circuit.on = confirmed[id];
  }
  const ok = result.ok && result.data.ok !== false;
  // No answer from the service at all: show the real state again.
  if (!ok && !confirmed) loadLights();
  if (LIGHTS.data) renderLightCircuits(LIGHTS.data.circuits);
  if (ids.some(id => id in LIGHTS.circuitWant)) sendRoomLight();  // tapped again meanwhile
  if (ok) return [];
  const errors = result.data.errors;
  if (errors) return Object.keys(errors).map(id => ({ id, reason: "nicht erreichbar" }));
  return ids.map(id => ({ id, reason: result.data.reason || `HTTP ${result.status}` }));
}

// ---- scenes ---------------------------------------------------------------------------

function renderLightScenes(scenes) {
  const box = $("light-scenes");
  const sig = JSON.stringify([scenes.map(s => [s.id, s.name, s.stage, s.circuits]), LIGHTS.data && LIGHTS.data.zone_labels]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren();
  if (!scenes.length) {
    const empty = document.createElement("div");
    empty.className = "light-empty";
    empty.textContent = "Noch keine Szene. Licht einstellen und mit „+ Szene“ speichern.";
    box.appendChild(empty);
    return;
  }
  for (const scene of scenes) {
    const item = document.createElement("div");
    item.className = "light-scene";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "light-scene-key";
    apply.dataset.lightScene = scene.id;
    apply.textContent = scene.name;
    apply.title = `Szene „${scene.name}“ einblenden · ${lightSceneParts(scene)}`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "light-scene-delete";
    del.dataset.lightSceneDelete = scene.id;
    del.innerHTML = ICON_TRASH;
    del.setAttribute("aria-label", `Szene „${scene.name}“ löschen`);
    del.title = del.getAttribute("aria-label");
    const pencil = renamePencil(`Szene „${scene.name}“`);
    pencil.dataset.lightSceneRename = scene.id;
    item.append(apply, pencil, del);
    box.appendChild(item);
  }
}

// What a scene sets, for its key's title: only these lights change.
function lightSceneParts(scene) {
  const data = LIGHTS.data;
  const zoneLabels = (data && data.zone_labels) || {};
  const circuitLabels = Object.fromEntries(((data && data.circuits) || []).map(c => [c.id, c.label]));
  const stage = Object.keys(scene.stage.zones).map(z => zoneLabels[z] || z);
  if (scene.stage.warm !== null && scene.stage.warm !== undefined) stage.push("Farbe");
  const room = Object.keys(scene.circuits).map(c => circuitLabels[c] || c);
  return [stage.length ? `Bühne: ${stage.join(", ")}` : "", room.length ? `Raumlicht: ${room.join(", ")}` : ""]
    .filter(Boolean).join(" · ");
}

// ---- new scene: which lights belong to it ------------------------------------------

function lightPickKey(box, id, label, now, disabled = false) {
  let key = box.querySelector(`[data-light-pick="${id}"]`);
  if (!key) {
    key = document.createElement("button");
    key.type = "button";
    key.className = "light-pick-key";
    key.dataset.lightPick = id;
    key.innerHTML = '<span class="light-pick-name"></span><span class="light-pick-now"></span>';
    box.appendChild(key);
  }
  const chosen = LIGHTS.pick.has(id) && !disabled;
  key.querySelector(".light-pick-name").textContent = label;
  key.querySelector(".light-pick-now").textContent = now;
  key.disabled = disabled;
  key.setAttribute("aria-pressed", String(chosen));
  key.title = disabled ? `${label}: antwortet nicht, kann nicht gespeichert werden`
    : `${label} ${chosen ? "nicht mehr in die Szene nehmen" : "in die Szene nehmen"}`;
  return key;
}

function renderLightPick() {
  const data = LIGHTS.data;
  if (!data || !$("light-scene-create").classList.contains("visible")) return;
  const stage = $("light-pick-stage");
  for (const [zone, label] of Object.entries(data.zone_labels)) {
    lightPickKey(stage, `zone:${zone}`, label, lightPercent(data.stage.zones[zone]));
  }
  // Warm and cold together: "100/20 %".
  lightPickKey(stage, "color", "Farbe", `${lightPercent(data.stage.warm).replace(" %", "")}/${lightPercent(data.stage.cold)}`);
  const room = $("light-pick-room");
  for (const circuit of data.circuits) {
    const unknown = circuit.on !== true && circuit.on !== false;
    if (unknown) LIGHTS.pick.delete(`circuit:${circuit.id}`);
    lightPickKey(room, `circuit:${circuit.id}`, circuit.label, unknown ? "?" : circuit.on ? "An" : "Aus", unknown);
  }
  for (const all of document.querySelectorAll("[data-light-pick-all]")) {
    const keys = [...$(`light-pick-${all.dataset.lightPickAll}`).querySelectorAll("[data-light-pick]:not(:disabled)")];
    const every = keys.length > 0 && keys.every(k => LIGHTS.pick.has(k.dataset.lightPick));
    all.textContent = every ? "Keine" : "Alle";
    all.setAttribute("aria-label", `${every ? "Keine" : "Alle"} ${all.dataset.lightPickAll === "stage" ? "Bühnenlichter" : "Raumlicht-Kreise"} wählen`);
  }
  // Nothing chosen: nothing to save.
  $("light-scene-save").disabled = !LIGHTS.pick.size;
}

function openLightSceneCreate() {
  LIGHTS.pick.clear();
  $("light-scene-create").classList.add("visible");
  renderLightPick();
  $("light-scene-name").focus();
}

function closeLightSceneCreate() {
  $("light-scene-create").classList.remove("visible");
  LIGHTS.pick.clear();
}

async function saveLightScene(replace = false) {
  const name = $("light-scene-name").value.trim();
  if (!name) { $("light-scene-name").focus(); return; }
  if (!LIGHTS.pick.size) return;
  const picked = [...LIGHTS.pick];
  const lights = {
    zones: picked.filter(id => id.startsWith("zone:")).map(id => id.slice(5)),
    color: LIGHTS.pick.has("color"),
    circuits: picked.filter(id => id.startsWith("circuit:")).map(id => id.slice(8)),
  };
  const button = $("light-scene-save");
  button.disabled = true;
  try {
    const result = await api("POST", "/lights/scenes", { name, replace, lights }, { timeout: lightTimeout() });
    if (result.status === 409 && !replace) {
      if (await confirmDelete(button, `Es gibt schon eine Szene „${name}“. Mit dem aktuellen Licht ersetzen?`, "Ersetzen")) {
        button.disabled = false;
        return saveLightScene(true);
      }
      return;
    }
    if (!result.ok || result.data.ok === false) { toast(`Szene: ${result.data.reason || "HTTP " + result.status}`, false); return; }
    const unknown = result.data.unknown_circuits || [];
    toast(`Szene „${result.data.scene.name}“ gespeichert` + (unknown.length ? ` (${unknown.length === 1 ? "1 Raumlicht-Kreis" : unknown.length + " Raumlicht-Kreise"} ohne Antwort nicht enthalten)` : ""), !unknown.length);
    $("light-scene-name").value = "";
    closeLightSceneCreate();
    await loadLights();
    loadProgramLightScenes();
  } finally {
    button.disabled = !LIGHTS.pick.size && $("light-scene-create").classList.contains("visible");
  }
}

// ---- requests and loading ----------------------------------------------------------

// A key that sends one request: busy while it runs, toast on failure, reload.
async function lightRequest(button, method, path, body) {
  if (LIGHTS.busy.has(button)) return null;
  LIGHTS.busy.add(button);
  button.classList.add("busy");
  try {
    const result = await api(method, path, body, { timeout: lightTimeout() });
    if (!result.ok || result.data.ok === false) {
      const errors = result.data.errors ? ` (${Object.keys(result.data.errors).length} ohne Antwort)` : "";
      toast(`Licht: ${result.data.reason || "HTTP " + result.status}${errors}`, false);
    }
    await loadLights();
    return result;
  } finally {
    LIGHTS.busy.delete(button);
    button.classList.remove("busy");
  }
}

async function loadLights() {
  const seq = LIGHTS.circuitSeq;
  const result = await api("GET", "/lights", undefined, { timeout: lightTimeout() });
  if (!result.ok || result.data.ok === false) {
    lightStatus(result.status === 503 ? "Lichtsteuerung ist auf diesem Dienst nicht verfügbar." : `Licht nicht erreichbar: ${result.data.reason || "HTTP " + result.status}`);
    return;
  }
  const unknown = result.data.circuits.filter(c => c.on === null).map(c => c.label);
  lightStatus(unknown.length ? `Raumlicht nicht erreichbar: ${unknown.join(", ")}.` : "");
  // A switch began or ended meanwhile: this answer may show the old state.
  if (seq !== LIGHTS.circuitSeq && LIGHTS.data) {
    const known = Object.fromEntries(LIGHTS.data.circuits.map(c => [c.id, c.on]));
    for (const circuit of result.data.circuits) {
      if (circuit.id in known) circuit.on = known[circuit.id];
    }
  }
  LIGHTS.data = result.data;
  renderLights();
}

// Polled; only while the section is open (or on the stage).
async function refreshLights() {
  if (!STAGE.on && !$("lights-body").closest(".panel").classList.contains("open")) return;
  if (LIGHTS.dragging.size) return;
  await loadLights();
}

initLightSliders($("light-faders"));
initLightSliders($("light-settings"));
$("light-circuits").addEventListener("click", event => {
  const key = event.target.closest("[data-light-circuit]");
  if (key) switchRoomLight({ [key.dataset.lightCircuit]: key.dataset.state !== "on" });
});
$("light-room-on").addEventListener("click", () => {
  const ids = (LIGHTS.data ? LIGHTS.data.circuits : []).map(c => c.id);
  if (ids.length) switchRoomLight(Object.fromEntries(ids.map(id => [id, true])));
});
$("light-room-off").addEventListener("click", () => {
  const ids = (LIGHTS.data ? LIGHTS.data.circuits : []).map(c => c.id);
  if (ids.length) switchRoomLight(Object.fromEntries(ids.map(id => [id, false])));
});
$("light-stage-on").addEventListener("click", event => {
  if (!LIGHTS.data) return;
  const start = LIGHTS.data.stage.start || 255;
  // A zone that is already on keeps its level.
  const zones = Object.fromEntries(Object.entries(LIGHTS.data.stage.zones).map(([z, v]) => [z, v || start]));
  lightRequest(event.currentTarget, "POST", "/lights/stage", { zones });
});
$("light-stage-off").addEventListener("click", event => {
  if (!LIGHTS.data) return;
  const zones = Object.fromEntries(Object.keys(LIGHTS.data.stage.zones).map(z => [z, 0]));
  lightRequest(event.currentTarget, "POST", "/lights/stage", { zones });
});
$("light-scenes").addEventListener("click", async event => {
  const rename = event.target.closest("[data-light-scene-rename]");
  if (rename) {
    const item = rename.closest(".light-scene");
    const current = item.querySelector(".light-scene-key").textContent;
    startInlineRename(item, current, { save: async name => {
      if (await saveName("PUT", `/lights/scenes/${encodeURIComponent(rename.dataset.lightSceneRename)}`, name, "Szene")) {
        await loadLights();
        loadProgramLightScenes();
      }
    } });
    return;
  }
  const apply = event.target.closest("[data-light-scene]");
  if (apply) {
    const result = await lightRequest(apply, "POST", `/lights/scenes/${encodeURIComponent(apply.dataset.lightScene)}/apply`);
    if (result && result.ok && result.data.ok !== false) toast(`Szene „${result.data.scene}“ eingeblendet`, true);
    return;
  }
  const del = event.target.closest("[data-light-scene-delete]");
  if (!del) return;
  const name = del.closest(".light-scene").querySelector(".light-scene-key").textContent;
  if (!(await confirmDelete(del, `Szene „${name}“ löschen? Programmpunkte mit dieser Szene starten dann nicht mehr.`))) return;
  await lightRequest(del, "DELETE", `/lights/scenes/${encodeURIComponent(del.dataset.lightSceneDelete)}`);
  loadProgramLightScenes();
});
$("light-scene-add").addEventListener("click", openLightSceneCreate);
$("light-scene-cancel").addEventListener("click", closeLightSceneCreate);
$("light-scene-save").addEventListener("click", () => saveLightScene());
$("light-scene-name").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); saveLightScene(); }
  if (event.key === "Escape") closeLightSceneCreate();
});
$("light-scene-create").addEventListener("click", event => {
  const key = event.target.closest("[data-light-pick]");
  const all = event.target.closest("[data-light-pick-all]");
  if (key && !key.disabled) {
    const id = key.dataset.lightPick;
    if (LIGHTS.pick.has(id)) LIGHTS.pick.delete(id); else LIGHTS.pick.add(id);
  } else if (all) {
    const keys = [...$(`light-pick-${all.dataset.lightPickAll}`).querySelectorAll("[data-light-pick]:not(:disabled)")];
    const every = keys.every(k => LIGHTS.pick.has(k.dataset.lightPick));
    for (const k of keys) {
      if (every) LIGHTS.pick.delete(k.dataset.lightPick); else LIGHTS.pick.add(k.dataset.lightPick);
    }
  } else return;
  renderLightPick();
});

// Pencils of the zones and room light circuits: the name is stored on the
// service; an empty name goes back to the built-in one.
for (const box of [$("light-faders"), $("light-circuits")]) {
  box.addEventListener("click", event => {
    const pencil = event.target.closest("[data-light-rename]");
    if (!pencil) return;
    const slot = pencil.closest(".light-name-cell, .light-circuit-tile");
    const holder = pencil.closest("[data-default-name]");
    const current = slot.querySelector(".light-name, .light-circuit-name").textContent;
    const fallback = holder ? holder.dataset.defaultName : "";
    startInlineRename(slot, current === fallback ? "" : current, {
      placeholder: fallback, allowEmpty: true,
      save: async name => {
        if (await saveName("PUT", `/names/${pencil.dataset.lightRename}`, name, "Name")) await loadLights();
      },
    });
  });
}
