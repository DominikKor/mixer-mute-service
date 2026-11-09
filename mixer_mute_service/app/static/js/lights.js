// Mixer Mute Service UI (lights.js): Licht: scenes, stage zones (Art-Net),
// room light (Shelly) and the light settings.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

const LIGHTS = {
  data: null,           // last GET /lights
  dragging: new Set(),  // sliders the user holds: the poll leaves them alone
  sendTimers: {},       // per slider: the next throttled send
  busy: new Set(),      // keys with a request in flight
};
const LIGHT_SEND_MS = 120;  // while dragging, at most one send per this time
const LIGHT_POLL_MS = 3000;

// Fades run while the request is open (up to 10 s), the Shellys may take
// 2.5 s each: a light request waits longer than api()'s default.
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
  const name = document.createElement("span");
  name.className = "light-name";
  name.textContent = label;
  row.appendChild(name);
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

function renderLights() {
  const data = LIGHTS.data;
  if (!data) return;
  const stage = data.stage;
  const faders = $("light-faders");
  for (const [zone, label] of Object.entries(data.zone_labels)) {
    setLightRow(lightSliderRow(faders, zone, label, "zone", 255, "percent", true), stage.zones[zone] || 0);
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

function scheduleLightSend(range) {
  const id = range.dataset.lightSlider;
  if (LIGHTS.sendTimers[id]) return;  // the pending send picks up the newest value
  LIGHTS.sendTimers[id] = setTimeout(async () => {
    delete LIGHTS.sendTimers[id];
    await sendLightSlider(range, false);
  }, LIGHT_SEND_MS);
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
    const id = range.dataset.lightSlider;
    clearTimeout(LIGHTS.sendTimers[id]);
    delete LIGHTS.sendTimers[id];
    await sendLightSlider(range, false);
    LIGHTS.dragging.delete(id);
  });
  // A drag that ends outside the slider (finger slid off) still lets go.
  for (const type of ["pointerup", "pointercancel"]) {
    document.addEventListener(type, () => {
      for (const id of [...LIGHTS.dragging]) if (!LIGHTS.sendTimers[id]) LIGHTS.dragging.delete(id);
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
      key = document.createElement("button");
      key.type = "button";
      key.className = "light-circuit";
      key.dataset.lightCircuit = circuit.id;
      key.innerHTML = '<span class="light-lamp" aria-hidden="true"></span><span class="light-circuit-name"></span><span class="light-circuit-state"></span>';
      box.appendChild(key);
    }
    key.querySelector(".light-circuit-name").textContent = circuit.label;
    const state = circuit.on === true ? "on" : circuit.on === false ? "off" : "unknown";
    key.dataset.state = state;
    key.querySelector(".light-circuit-state").textContent = { on: "An", off: "Aus", unknown: "?" }[state];
    key.setAttribute("aria-pressed", String(circuit.on === true));
    key.title = state === "unknown" ? `${circuit.label}: antwortet nicht – antippen schaltet ein` : `${circuit.label} ${state === "on" ? "ausschalten" : "einschalten"}`;
  }
}

async function switchRoomLight(states, button) {
  await lightRequest(button, "POST", "/lights/circuits", { states });
}

// ---- scenes ---------------------------------------------------------------------------

function renderLightScenes(scenes) {
  const box = $("light-scenes");
  const sig = JSON.stringify(scenes.map(s => [s.id, s.name]));
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
    apply.title = `Szene „${scene.name}“ einblenden`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "light-scene-delete";
    del.dataset.lightSceneDelete = scene.id;
    del.innerHTML = ICON_TRASH;
    del.setAttribute("aria-label", `Szene „${scene.name}“ löschen`);
    del.title = del.getAttribute("aria-label");
    item.append(apply, del);
    box.appendChild(item);
  }
}

async function saveLightScene(replace = false) {
  const name = $("light-scene-name").value.trim();
  if (!name) { $("light-scene-name").focus(); return; }
  const button = $("light-scene-save");
  button.disabled = true;
  try {
    const result = await api("POST", "/lights/scenes", { name, replace }, { timeout: lightTimeout() });
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
    $("light-scene-create").classList.remove("visible");
    await loadLights();
    loadProgramLightScenes();
  } finally {
    button.disabled = false;
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
  const result = await api("GET", "/lights", undefined, { timeout: lightTimeout() });
  if (!result.ok || result.data.ok === false) {
    lightStatus(result.status === 503 ? "Lichtsteuerung ist auf diesem Dienst nicht verfügbar." : `Licht nicht erreichbar: ${result.data.reason || "HTTP " + result.status}`);
    return;
  }
  const unknown = result.data.circuits.filter(c => c.on === null).length;
  lightStatus(unknown ? `${unknown} von ${result.data.circuits.length} Raumlicht-Kreisen antworten nicht.` : "");
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
  if (key) switchRoomLight({ [key.dataset.lightCircuit]: key.dataset.state !== "on" }, key);
});
$("light-room-on").addEventListener("click", event => {
  const ids = (LIGHTS.data ? LIGHTS.data.circuits : []).map(c => c.id);
  if (ids.length) switchRoomLight(Object.fromEntries(ids.map(id => [id, true])), event.currentTarget);
});
$("light-room-off").addEventListener("click", event => {
  const ids = (LIGHTS.data ? LIGHTS.data.circuits : []).map(c => c.id);
  if (ids.length) switchRoomLight(Object.fromEntries(ids.map(id => [id, false])), event.currentTarget);
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
  const apply = event.target.closest("[data-light-scene]");
  if (apply) {
    const result = await lightRequest(apply, "POST", `/lights/scenes/${encodeURIComponent(apply.dataset.lightScene)}/apply`);
    if (result && result.ok && result.data.ok !== false) toast(`Szene „${result.data.scene}“ eingeblendet`, true);
    return;
  }
  const del = event.target.closest("[data-light-scene-delete]");
  if (!del) return;
  const name = del.previousElementSibling.textContent;
  if (!(await confirmDelete(del, `Szene „${name}“ löschen? Programmpunkte mit dieser Szene starten dann nicht mehr.`))) return;
  await lightRequest(del, "DELETE", `/lights/scenes/${encodeURIComponent(del.dataset.lightSceneDelete)}`);
  loadProgramLightScenes();
});
$("light-scene-add").addEventListener("click", () => {
  $("light-scene-create").classList.add("visible");
  $("light-scene-name").focus();
});
$("light-scene-cancel").addEventListener("click", () => $("light-scene-create").classList.remove("visible"));
$("light-scene-save").addEventListener("click", () => saveLightScene());
$("light-scene-name").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); saveLightScene(); }
  if (event.key === "Escape") $("light-scene-create").classList.remove("visible");
});
