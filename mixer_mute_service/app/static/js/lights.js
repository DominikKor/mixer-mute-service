// Mixer Mute Service UI (lights.js): Licht: presets, the four stage lights
// (Art-Net; each with on/off, brightness and its own warm/cold colour), the
// room light (Shelly) and two settings.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

const LIGHTS = {
  data: null,           // last GET /lights
  dragging: new Set(),  // sliders the user holds: the poll leaves them alone
  sending: {},          // per slider: {busy, again, released} of its one request in flight
  busy: new Set(),      // keys with a request in flight
  pick: new Set(),      // preset being saved: chosen lights ("zone:left", "circuit:light-tech")
  updating: null,       // id of the preset being overwritten, null for a new one
  renaming: null,       // "stage" / "room": a tap on a light opens its name
  setting: null,        // the open setting below the chips ("fade_ms", "start")
  link: false,          // "Alle gleich": a slider moves all four lights
  circuitWant: {},      // room light: state the user chose, not yet confirmed (shown at once)
  circuitSent: {},      // room light: state in flight per circuit
  circuitSeq: 0,        // counts switches: a poll begun before one keeps the confirmed states
};
const LIGHT_SEND_MS = 60;   // while dragging, at most one send per this time
const LIGHT_POLL_MS = 3000;
const LIGHT_LINK_KEY = "mixer.lights.link";
try { LIGHTS.link = localStorage.getItem(LIGHT_LINK_KEY) === "1"; } catch (_) { /* private mode */ }

// Fades run while the request is open (up to 10 s), a Shelly may take
// 2.5 s: a light request waits longer than api()'s default.
function lightTimeout() {
  return ((LIGHTS.data && LIGHTS.data.stage.fade_ms) || 1000) + 15000;
}

function lightPct(value) { return Math.round((value || 0) * 100 / 255); }
function lightPercent(value) { return `${lightPct(value)} %`; }

function lightStatus(message) {
  $("light-status").hidden = !message;
  $("light-status").textContent = message || "";
}

// ---- colour: one slider warm <-> cold over the two white channels -----------------
// 0..510: from the left only warm (255) with cold mixed in up to 255 at the
// middle, then warm fades out to the right. The stronger channel is always
// full, so every colour has the most light; the brightness channel dims.
// Companion's colour (warm 255, cold 50) is exactly 50.
const TONE_MAX = 510;

function toneOf(fixture) {
  const { warm, cold } = fixture;
  if (warm >= cold) return warm ? Math.round((255 * cold) / warm) : 255;
  return TONE_MAX - Math.round((255 * warm) / cold);
}

function toneChannels(tone) {
  return { warm: Math.min(255, TONE_MAX - tone), cold: Math.min(255, tone) };
}

// The colour a light shows at this tone (for lamps and preset dots).
function toneRgb(tone) {
  const stops = [[255, 160, 64], [255, 238, 214], [178, 212, 255]];
  const t = tone / 255;
  const [a, b, u] = t <= 1 ? [stops[0], stops[1], t] : [stops[1], stops[2], t - 1];
  return a.map((v, i) => Math.round(v + (b[i] - v) * u)).join(", ");
}

function toneText(fixture) { return `W${lightPct(fixture.warm)} K${lightPct(fixture.cold)}`; }

// A lamp (card head, preset dot): colour, glow by brightness, dark when off.
function paintLamp(lamp, fixture) {
  const lit = fixture.on !== false && fixture.level !== 0;
  lamp.style.setProperty("--lamp", toneRgb(toneOf({ warm: fixture.warm ?? 255, cold: fixture.cold ?? 50 })));
  lamp.style.setProperty("--glow", lit ? String(0.3 + (0.7 * (fixture.level ?? 255)) / 255) : "0");
  lamp.dataset.state = fixture.on === false ? "off" : fixture.on === true ? "on" : "keep";
}

// ---- stage lights: one card each ------------------------------------------------------

function fixtureRange(card, zone, kind, label, max) {
  const row = document.createElement("div");
  row.className = `fixture-row fixture-row-${kind}`;
  const name = document.createElement("span");
  name.className = "fixture-label";
  name.textContent = label;
  const range = document.createElement("input");
  range.type = "range";
  range.className = `light-range light-range-${kind}`;
  range.min = "0";
  range.max = String(max);
  range.step = "1";
  range.dataset.lightSlider = `${zone}:${kind}`;
  range.dataset.lightKind = kind;
  range.dataset.zone = zone;
  const value = document.createElement("span");
  value.className = "light-value";
  row.append(name, range, value);
  card.appendChild(row);
}

function fixtureCard(zone) {
  const box = $("light-fixtures");
  let card = box.querySelector(`[data-fixture="${zone}"]`);
  if (card) return card;
  card = document.createElement("div");
  card.className = "fixture";
  card.dataset.fixture = zone;
  const head = document.createElement("div");
  head.className = "fixture-head";
  const lamp = document.createElement("span");
  lamp.className = "fixture-lamp";
  lamp.setAttribute("aria-hidden", "true");
  // The name; in rename mode (Bühne ⋮ > Scheinwerfer umbenennen) a key
  // that opens it as a field.
  const nameCell = document.createElement("div");
  nameCell.className = "light-name-cell";
  const name = document.createElement("button");
  name.type = "button";
  name.className = "light-name";
  name.dataset.lightRename = `light-zone/${zone}`;
  name.tabIndex = -1;
  nameCell.append(name);
  const key = document.createElement("button");
  key.type = "button";
  key.className = "light-zone-key";
  key.dataset.fixtureKey = zone;
  head.append(lamp, nameCell, key);
  card.appendChild(head);
  fixtureRange(card, zone, "level", "Hell", 255);
  fixtureRange(card, zone, "tone", "Farbe", TONE_MAX);
  box.appendChild(card);
  return card;
}

// Show one light; a slider the user holds keeps its place.
function setFixture(card, fixture, label, fallback) {
  card.classList.toggle("off", !fixture.on);
  card.dataset.defaultName = fallback;
  card.querySelector(".light-name").textContent = label;
  const nameKey = card.querySelector("[data-light-rename]");
  nameKey.title = LIGHTS.renaming === "stage" ? `${label} umbenennen` : "";
  nameKey.tabIndex = LIGHTS.renaming === "stage" ? 0 : -1;
  const key = card.querySelector(".light-zone-key");
  key.classList.toggle("on", fixture.on);
  key.setAttribute("aria-pressed", String(fixture.on));
  key.textContent = fixture.on ? "An" : "Aus";
  key.setAttribute("aria-label", `${label} ${fixture.on ? "ausschalten" : "einschalten"}`);
  const level = card.querySelector('[data-light-kind="level"]');
  const tone = card.querySelector('[data-light-kind="tone"]');
  level.setAttribute("aria-label", `${label} Helligkeit${fixture.on ? "" : " (aus: wird gemerkt)"}`);
  tone.setAttribute("aria-label", `${label} Farbe warm bis kalt`);
  if (!lightSliderHeld(level)) level.value = String(fixture.level);
  if (!lightSliderHeld(tone)) tone.value = String(toneOf(fixture));
  showFixtureSliders(card, fixture.on);
}

// A slider the user holds keeps its place; with "Alle gleich" so do the
// same sliders of the other three lights (they move with it).
function lightSliderHeld(range) {
  if (LIGHTS.dragging.has(range.dataset.lightSlider)) return true;
  const kind = range.dataset.lightKind;
  return LIGHTS.link && [...LIGHTS.dragging].some(id => id.endsWith(`:${kind}`));
}

// The values next to the sliders and the lamp, from the sliders themselves
// (also while dragging, before the service answered).
function showFixtureSliders(card, on) {
  const level = card.querySelector('[data-light-kind="level"]');
  const tone = card.querySelector('[data-light-kind="tone"]');
  const shown = { on, level: Number(level.value), ...toneChannels(Number(tone.value)) };
  level.style.setProperty("--fill", `${(shown.level * 100) / 255}%`);
  level.closest(".fixture-row").querySelector(".light-value").textContent = lightPercent(shown.level);
  tone.closest(".fixture-row").querySelector(".light-value").textContent = toneText(shown);
  paintLamp(card.querySelector(".fixture-lamp"), shown);
}

function renderFixtures() {
  const data = LIGHTS.data;
  for (const [zone, fixture] of Object.entries(data.stage.fixtures)) {
    const label = data.zone_labels[zone] || zone;
    setFixture(fixtureCard(zone), fixture, label, (data.zone_defaults || {})[zone] || label);
  }
  const link = $("light-link");
  link.setAttribute("aria-pressed", String(LIGHTS.link));
  $("light-fixtures").classList.toggle("linked", LIGHTS.link);
  renderLightBlockHeads();
}

// ---- settings: two chips, one slider below them -------------------------------------

const LIGHT_SETTINGS = {
  fade_ms: { label: "Übergang", max: 10000, step: 250, hint: "So lange blenden Presets und An/Aus über." },
  start: { label: "Einschalten auf", max: 255, step: 1, hint: "Helligkeit für einen Scheinwerfer ohne gemerkten Wert und für die Overall-Tasten." },
};

function settingText(kind, raw) {
  if (kind === "fade_ms") return `${(raw / 1000).toFixed(raw % 100 ? 2 : 1).replace(".", ",")} s`;
  return lightPercent(raw);
}

function renderLightSettings() {
  const stage = LIGHTS.data.stage;
  for (const chip of document.querySelectorAll("[data-light-setting]")) {
    const kind = chip.dataset.lightSetting;
    const open = LIGHTS.setting === kind;
    chip.querySelector(".light-chip-value").textContent = settingText(kind, stage[kind]);
    chip.setAttribute("aria-expanded", String(open));
  }
  const pop = $("light-setting-pop");
  pop.hidden = !LIGHTS.setting;
  if (!LIGHTS.setting) return;
  const kind = LIGHTS.setting;
  const spec = LIGHT_SETTINGS[kind];
  let range = pop.querySelector("input");
  if (!range || range.dataset.lightKind !== kind) {
    pop.innerHTML = '<div class="fixture-row"><span class="fixture-label"></span><input type="range" class="light-range light-range-setting" min="0" /><span class="light-value"></span></div><div class="light-setting-hint"></div>';
    range = pop.querySelector("input");
    range.max = String(spec.max);
    range.step = String(spec.step);
    range.dataset.lightSlider = kind;
    range.dataset.lightKind = kind;
    range.setAttribute("aria-label", spec.label);
    pop.querySelector(".fixture-label").textContent = spec.label;
    pop.querySelector(".light-setting-hint").textContent = spec.hint;
  }
  if (!LIGHTS.dragging.has(kind)) range.value = String(stage[kind]);
  showSettingSlider(range);
}

function showSettingSlider(range) {
  const raw = Number(range.value);
  range.style.setProperty("--fill", `${(raw * 100) / Number(range.max)}%`);
  range.closest(".fixture-row").querySelector(".light-value").textContent = settingText(range.dataset.lightKind, raw);
}

function renderLights() {
  if (!LIGHTS.data) return;
  renderFixtures();
  renderLightSettings();
  renderLightCircuits(LIGHTS.data.circuits);
  renderLightScenes(LIGHTS.data.scenes);
  renderLightPick();
  renderLightBlockHeads();
}

// ---- the three blocks fold on their own -------------------------------------------
// Folded per device (localStorage, also without it the page works); a
// folded block's head says in a few words what it holds.
const LIGHT_BLOCKS_KEY = "mixer_light_blocks_folded";

function lightBlocksFolded() {
  try { return new Set(JSON.parse(storeGet(LIGHT_BLOCKS_KEY) || "[]")); } catch (_) { return new Set(); }
}

function setLightBlockFolded(block, folded) {
  const all = lightBlocksFolded();
  if (folded) all.add(block); else all.delete(block);
  storeSet(LIGHT_BLOCKS_KEY, JSON.stringify([...all]));
  applyLightBlocks();
}

function applyLightBlocks() {
  const folded = lightBlocksFolded();
  for (const block of document.querySelectorAll("[data-light-block]")) {
    const name = block.dataset.lightBlock;
    // Renaming in a block keeps it open (its names are what is being changed).
    const isFolded = folded.has(name) && LIGHTS.renaming !== name;
    block.classList.toggle("folded", isFolded);
    block.querySelector(".light-block-body").hidden = isFolded;
    const toggle = block.querySelector("[data-light-block-toggle]");
    toggle.setAttribute("aria-expanded", String(!isFolded));
    const title = block.querySelector(".light-block-title").textContent;
    toggle.title = `${title} ${isFolded ? "aufklappen" : "zuklappen"}`;
  }
  renderLightBlockHeads();
}

// What a folded head shows: Presets the active one(s), Raumlicht how many
// circuits are on (and how many do not answer), Bühne how many lights are on.
function renderLightBlockHeads() {
  const data = LIGHTS.data;
  const set = (name, text) => { document.querySelector(`[data-light-summary="${name}"]`).textContent = text; };
  if (!data) { for (const name of ["presets", "room", "stage"]) set(name, ""); return; }
  const active = data.scenes.filter(s => s.active).map(s => s.name);
  const last = data.scenes.find(s => s.id === data.last_scene);
  set("presets", active.length ? active.join(", ")
    : last ? `${last.name} (geändert)`
    : data.scenes.length ? "keins aktiv" : "keine");
  const states = data.circuits.map(c => (c.id in LIGHTS.circuitWant ? LIGHTS.circuitWant[c.id] : c.on));
  const on = states.filter(v => v === true).length;
  const unknown = states.filter(v => v !== true && v !== false).length;
  set("room", [on ? `${on} an` : "alle aus", unknown ? `${unknown} ?` : ""].filter(Boolean).join(" · "));
  const lit = Object.values(data.stage.fixtures).filter(f => f.on).length;
  set("stage", lit ? `${lit} an` : "alle aus");
}

// ---- sliders: one request per slider at a time ---------------------------------------

async function sendLightSlider(range, fade) {
  const kind = range.dataset.lightKind;
  const value = Number(range.value);
  let result;
  if (kind === "level" || kind === "tone") {
    // "Alle gleich": the same value for all four lights.
    const zones = LIGHTS.link ? Object.keys(LIGHTS.data.stage.fixtures) : [range.dataset.zone];
    const change = kind === "level" ? { level: value } : toneChannels(value);
    result = await api("POST", "/lights/stage", { zones: Object.fromEntries(zones.map(z => [z, change])), fade }, { timeout: lightTimeout() });
  } else {
    result = await api("PUT", "/lights/settings", { [kind]: value });
  }
  if (!result.ok || result.data.ok === false) {
    toast(`Licht: ${result.data.reason || "HTTP " + result.status}`, false);
    return;
  }
  if (LIGHTS.data && result.data.stage) {
    LIGHTS.data.stage = result.data.stage;
    renderFixtures();
    renderLightSettings();
  }
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
    // Let go meanwhile: the poll may update this slider again; the presets
    // show at once whether the light still matches one.
    if (state.released) {
      state.released = false;
      LIGHTS.dragging.delete(id);
      if (!LIGHTS.dragging.size) loadLights();
    }
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

// While dragging: the slider's own card (and with "Alle gleich" the other
// cards) show the new value at once.
function previewLightSlider(range) {
  const kind = range.dataset.lightKind;
  if (kind !== "level" && kind !== "tone") { showSettingSlider(range); return; }
  const cards = LIGHTS.link ? [...$("light-fixtures").querySelectorAll("[data-fixture]")] : [range.closest("[data-fixture]")];
  for (const card of cards) {
    const other = card.querySelector(`[data-light-kind="${kind}"]`);
    if (other !== range) other.value = range.value;
    showFixtureSliders(card, !card.classList.contains("off"));
  }
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
    previewLightSlider(range);
    // Settings are only saved once on release; the light follows the drag.
    if (!(range.dataset.lightKind in LIGHT_SETTINGS)) scheduleLightSend(range);
  });
  container.addEventListener("change", async event => {
    const range = event.target.closest("input[data-light-slider]");
    if (!range) return;
    // The final value: queued behind a send still running, never beside it.
    lightSendState(range.dataset.lightSlider).released = true;
    await scheduleLightSend(range);
  });
}
// A drag that ends outside the slider (finger slid off) still lets go.
for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, () => {
    for (const id of [...LIGHTS.dragging]) releaseLightSlider(id);
  });
}

// ---- room light -------------------------------------------------------------------

function renderLightCircuits(circuits) {
  renderLightBlockHeads();  // a folded Raumlicht head counts what is on
  const box = $("light-circuits");
  for (const circuit of circuits) {
    let key = box.querySelector(`[data-light-circuit="${circuit.id}"]`);
    if (!key) {
      // Tile: the on/off key (in rename mode a tap opens its name instead).
      const tile = document.createElement("div");
      tile.className = "light-circuit-tile";
      key = document.createElement("button");
      key.type = "button";
      key.className = "light-circuit";
      key.dataset.lightCircuit = circuit.id;
      key.dataset.lightRename = `light-circuit/${circuit.id}`;
      key.innerHTML = '<span class="light-lamp" aria-hidden="true"></span><span class="light-circuit-name"></span><span class="light-circuit-state"></span>';
      tile.append(key);
      box.appendChild(tile);
    }
    const tile = key.closest(".light-circuit-tile");
    tile.dataset.defaultName = circuit.default_label || circuit.label;
    tile.dataset.name = circuit.label;
    key.querySelector(".light-circuit-name").textContent = circuit.label;
    // A tapped key shows its new state at once; "pending" until confirmed.
    const pending = circuit.id in LIGHTS.circuitWant;
    const on = pending ? LIGHTS.circuitWant[circuit.id] : circuit.on;
    const state = on === true ? "on" : on === false ? "off" : "unknown";
    key.dataset.state = state;
    key.classList.toggle("pending", pending);
    key.querySelector(".light-circuit-state").textContent = { on: "An", off: "Aus", unknown: "Offline" }[state];
    key.setAttribute("aria-pressed", String(on === true));
    // stale: the last question got no answer, the state is the last known
    // one. Only the tooltip says so: a Shelly missing one answer must not
    // make the key flicker.
    const stale = circuit.stale === true && !pending ? " (letzte Abfrage ohne Antwort)" : "";
    key.title = LIGHTS.renaming === "room" ? `${circuit.label} umbenennen` : state === "unknown" ? `${circuit.label}: nicht erreichbar – antippen versucht einzuschalten` : `${circuit.label} ${state === "on" ? "ausschalten" : "einschalten"}${stale}`;
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

// ---- presets ----------------------------------------------------------------------------

function renderLightScenes(scenes) {
  const box = $("light-scenes");
  const data = LIGHTS.data;
  const sig = JSON.stringify([scenes, data.last_scene, data.zone_labels, data.circuits.map(c => c.label)]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren();
  $("light-presets-hint").hidden = !scenes.length;
  if (!scenes.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state light-empty";
    empty.textContent = "Noch kein Preset. Licht einstellen und mit „+ Preset“ speichern.";
    box.appendChild(empty);
    return;
  }
  for (const scene of scenes) {
    const changed = !scene.active && scene.id === data.last_scene;
    const tile = document.createElement("div");
    tile.className = "preset";
    tile.classList.toggle("active", scene.active);
    tile.classList.toggle("changed", changed);
    tile.dataset.sceneId = scene.id;
    const key = document.createElement("button");
    key.type = "button";
    key.className = "preset-key";
    key.dataset.lightScene = scene.id;
    key.setAttribute("aria-pressed", String(scene.active));
    key.title = `Preset „${scene.name}“ einblenden · ${lightSceneParts(scene)}`;
    const name = document.createElement("span");
    name.className = "preset-name";
    name.textContent = scene.name;
    const state = document.createElement("span");
    state.className = "preset-state";
    state.textContent = scene.active ? "aktiv" : changed ? "geändert" : "";
    const top = document.createElement("span");
    top.className = "preset-top";
    top.append(name, state);
    key.append(top, presetSummary(scene));
    // Rename, overwrite, move and delete: the tile's "⋮", as everywhere.
    const menu = menuButton(`Preset „${scene.name}“: umbenennen, überschreiben, verschieben oder löschen`, `preset:${scene.id}`);
    menu.classList.add("tile-menu-btn");
    menu.dataset.presetMenu = scene.id;
    tile.append(key, menu);
    box.appendChild(tile);
  }
}

function presetMenuItems(scene) {
  const ids = LIGHTS.data.scenes.map(s => s.id);
  const index = ids.indexOf(scene.id);
  return [
    { label: "Umbenennen", icon: ICON_PENCIL, run: () => renameLightScene(scene) },
    { label: "Mit aktuellem Licht überschreiben…", icon: ICON_REFRESH, run: () => openLightSceneCreate(scene) },
    "-",
    { label: "Nach vorne", icon: ICON_LEFT, disabled: index <= 0, run: () => movePreset(scene.id, -1) },
    { label: "Nach hinten", icon: ICON_RIGHT, disabled: index >= ids.length - 1, run: () => movePreset(scene.id, 1) },
    "-",
    { label: "Löschen", icon: ICON_TRASH, danger: true, run: anchor => deleteLightScene(scene, anchor) },
  ];
}

function renameLightScene(scene) {
  const tile = $("light-scenes").querySelector(`.preset[data-scene-id="${CSS.escape(scene.id)}"]`);
  if (!tile) return;
  startInlineRename(tile, scene.name, { save: async name => {
    if (await saveName("PUT", `/lights/scenes/${encodeURIComponent(scene.id)}`, name, "Preset")) {
      await loadLights();
      loadProgramLightScenes();
    }
  } });
}

async function deleteLightScene(scene, anchor) {
  if (!(await confirmDelete(anchor, `Preset „${scene.name}“ löschen? Programmpunkte mit diesem Preset starten dann nicht mehr.`))) return;
  const result = await api("DELETE", `/lights/scenes/${encodeURIComponent(scene.id)}`, undefined, { timeout: lightTimeout() });
  if (!result.ok || result.data.ok === false) toast(`Preset: ${result.data.reason || "HTTP " + result.status}`, false);
  else toast(`Preset „${scene.name}“ gelöscht`, true);
  await loadLights();
  loadProgramLightScenes();
}

// Rename mode of the stage lights or the room light circuits (their block's
// "⋮"): a tap on a name opens it as a field; "Fertig" ends it.
function setLightRenaming(which) {
  if (which) startRenameMode($(which === "stage" ? "light-fixtures" : "light-circuits"), () => showLightRenaming(null));
  else if (LIGHTS.renaming) endRenameMode();
  showLightRenaming(which);
}

function showLightRenaming(which) {
  LIGHTS.renaming = which;
  for (const bar of document.querySelectorAll("[data-rename-bar]")) bar.hidden = bar.dataset.renameBar !== which;
  $("light-fixtures").classList.toggle("renaming", which === "stage");
  $("light-circuits").classList.toggle("renaming", which === "room");
  if (LIGHTS.data) { renderFixtures(); renderLightCircuits(LIGHTS.data.circuits); }
  applyLightBlocks();
}

function startLightRename(target) {
  const slot = target.closest(".light-name-cell, .light-circuit-tile");
  const holder = target.closest("[data-default-name]");
  const current = target.matches(".light-name") ? target.textContent : target.querySelector(".light-circuit-name").textContent;
  const fallback = holder ? holder.dataset.defaultName : "";
  startInlineRename(slot, current === fallback ? "" : current, {
    placeholder: fallback, allowEmpty: true,
    save: async name => {
      if (await saveName("PUT", `/names/${target.dataset.lightRename}`, name, "Name")) await loadLights();
    },
  });
}

// The tile's small picture: a dot per stage light (its colour and brightness,
// a ring when the preset switches it off, a dash when the preset leaves it
// alone) and how many room light circuits it switches on and off.
function presetSummary(scene) {
  const summary = document.createElement("span");
  summary.className = "preset-summary";
  const dots = document.createElement("span");
  dots.className = "preset-dots";
  for (const zone of Object.keys(LIGHTS.data.zone_labels)) {
    const dot = document.createElement("span");
    dot.className = "preset-dot";
    const fixture = scene.stage.fixtures[zone];
    if (fixture) paintLamp(dot, fixture); else dot.dataset.state = "none";
    dots.appendChild(dot);
  }
  summary.appendChild(dots);
  const room = Object.values(scene.circuits);
  if (room.length) {
    const on = room.filter(Boolean).length;
    const text = document.createElement("span");
    text.className = "preset-room";
    text.textContent = [on ? `${on} an` : "", room.length - on ? `${room.length - on} aus` : ""].filter(Boolean).join(" · ");
    text.title = "Raumlicht-Kreise";
    summary.appendChild(text);
  }
  return summary;
}

// What a preset sets, for its key's title: only these lights change.
function lightSceneParts(scene) {
  const data = LIGHTS.data;
  const circuitLabels = Object.fromEntries(data.circuits.map(c => [c.id, c.label]));
  const stage = Object.entries(scene.stage.fixtures).map(([zone, f]) => {
    const label = data.zone_labels[zone] || zone;
    if (f.on === false) return `${label} aus`;
    return f.level === null || f.level === undefined ? label : `${label} ${lightPercent(f.level)}`;
  });
  const room = Object.entries(scene.circuits).map(([c, on]) => `${circuitLabels[c] || c} ${on ? "an" : "aus"}`);
  return [stage.length ? `Bühne: ${stage.join(", ")}` : "", room.length ? `Raumlicht: ${room.join(", ")}` : ""]
    .filter(Boolean).join(" · ");
}

async function movePreset(sceneId, step) {
  const ids = LIGHTS.data.scenes.map(s => s.id);
  const from = ids.indexOf(sceneId);
  const to = from + step;
  if (from < 0 || to < 0 || to >= ids.length) return;
  [ids[from], ids[to]] = [ids[to], ids[from]];
  const result = await api("POST", "/lights/scenes/order", { ids });
  if (!result.ok || result.data.ok === false) toast(`Preset: ${result.data.reason || "HTTP " + result.status}`, false);
  await loadLights();
  loadProgramLightScenes();
}

// ---- saving a preset: which lights belong to it ------------------------------------

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
    : `${label} ${chosen ? "nicht mehr ins Preset nehmen" : "ins Preset nehmen"}`;
  return key;
}

function renderLightPick() {
  const data = LIGHTS.data;
  if (!data || !$("light-scene-create").classList.contains("visible")) return;
  const stage = $("light-pick-stage");
  for (const [zone, label] of Object.entries(data.zone_labels)) {
    const f = data.stage.fixtures[zone];
    lightPickKey(stage, `zone:${zone}`, label, f.on ? lightPercent(f.level) : "Aus");
  }
  const room = $("light-pick-room");
  // Overwriting a preset: a circuit of it that does not answer keeps the
  // state the preset had (the service keeps it); otherwise it cannot be saved.
  const old = LIGHTS.updating && data.scenes.find(s => s.id === LIGHTS.updating);
  for (const circuit of data.circuits) {
    const unknown = circuit.on !== true && circuit.on !== false;
    const keeps = unknown && old && circuit.id in old.circuits;
    if (unknown && !keeps) LIGHTS.pick.delete(`circuit:${circuit.id}`);
    const now = keeps ? `? bleibt ${old.circuits[circuit.id] ? "an" : "aus"}` : unknown ? "?" : circuit.on ? "An" : "Aus";
    lightPickKey(room, `circuit:${circuit.id}`, circuit.label, now, unknown && !keeps);
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

// New preset (scene null): every stage light chosen. Overwriting one: its
// name and lights are chosen, the current light is saved into it.
function openLightSceneCreate(scene = null) {
  LIGHTS.pick.clear();
  LIGHTS.updating = scene ? scene.id : null;
  if (scene) {
    for (const zone of Object.keys(scene.stage.fixtures)) LIGHTS.pick.add(`zone:${zone}`);
    for (const circuit of Object.keys(scene.circuits)) LIGHTS.pick.add(`circuit:${circuit}`);
  } else if (LIGHTS.data) {
    for (const zone of Object.keys(LIGHTS.data.zone_labels)) LIGHTS.pick.add(`zone:${zone}`);
  }
  $("light-scene-name").value = scene ? scene.name : "";
  $("light-scene-create-title").textContent = scene ? `Preset „${scene.name}“ überschreiben` : "Neues Preset";
  $("light-scene-save").textContent = scene ? "Mit aktuellem Licht überschreiben" : "Aktuelles Licht speichern";
  $("light-scene-create").classList.add("visible");
  renderLightPick();
  $("light-scene-name").focus();
}

function closeLightSceneCreate() {
  $("light-scene-create").classList.remove("visible");
  LIGHTS.pick.clear();
  LIGHTS.updating = null;
}

async function saveLightScene(replace = false) {
  const name = $("light-scene-name").value.trim();
  if (!name) { $("light-scene-name").focus(); return; }
  if (!LIGHTS.pick.size) return;
  const picked = [...LIGHTS.pick];
  const lights = {
    zones: picked.filter(id => id.startsWith("zone:")).map(id => id.slice(5)),
    circuits: picked.filter(id => id.startsWith("circuit:")).map(id => id.slice(8)),
  };
  const body = { name, replace, lights };
  if (LIGHTS.updating) body.id = LIGHTS.updating;
  const button = $("light-scene-save");
  button.disabled = true;
  try {
    const result = await api("POST", "/lights/scenes", body, { timeout: lightTimeout() });
    if (result.status === 409 && !replace && !LIGHTS.updating) {
      if (await confirmDelete(button, `Es gibt schon ein Preset „${name}“. Mit dem aktuellen Licht überschreiben?`, "Überschreiben")) {
        button.disabled = false;
        return saveLightScene(true);
      }
      return;
    }
    if (!result.ok || result.data.ok === false) { toast(`Preset: ${result.data.reason || "HTTP " + result.status}`, false); return; }
    const unknown = result.data.unknown_circuits || [];
    const kept = result.data.kept_circuits || [];
    const count = n => (n === 1 ? "1 Raumlicht-Kreis" : `${n} Raumlicht-Kreise`);
    const notes = [
      unknown.length ? `${count(unknown.length)} ohne Antwort nicht enthalten` : "",
      kept.length ? `${count(kept.length)} ohne Antwort, bisheriger Zustand behalten` : "",
    ].filter(Boolean);
    toast(`Preset „${result.data.scene.name}“ gespeichert` + (notes.length ? ` (${notes.join("; ")})` : ""), !notes.length);
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

// ---- events ---------------------------------------------------------------------------

initLightSliders($("light-fixtures"));
initLightSliders($("light-setting-pop"));

$("light-fixtures").addEventListener("click", event => {
  const name = event.target.closest("[data-light-rename]");
  if (name && LIGHTS.renaming === "stage") { startLightRename(name); return; }
  const key = event.target.closest("[data-fixture-key]");
  if (key && LIGHTS.data) {
    const zone = key.dataset.fixtureKey;
    // On again at the brightness it had; the slider never jumps to 0.
    const on = !LIGHTS.data.stage.fixtures[zone].on;
    lightRequest(key, "POST", "/lights/stage", { zones: { [zone]: { on } } });
  }
});
$("light-stage-on").addEventListener("click", event => {
  if (!LIGHTS.data) return;
  const zones = Object.fromEntries(Object.keys(LIGHTS.data.stage.fixtures).map(z => [z, { on: true }]));
  lightRequest(event.currentTarget, "POST", "/lights/stage", { zones });
});
$("light-stage-off").addEventListener("click", event => {
  if (!LIGHTS.data) return;
  const zones = Object.fromEntries(Object.keys(LIGHTS.data.stage.fixtures).map(z => [z, { on: false }]));
  lightRequest(event.currentTarget, "POST", "/lights/stage", { zones });
});
$("light-link").addEventListener("click", () => {
  LIGHTS.link = !LIGHTS.link;
  try { localStorage.setItem(LIGHT_LINK_KEY, LIGHTS.link ? "1" : "0"); } catch (_) { /* private mode */ }
  if (LIGHTS.data) renderFixtures();
});
for (const chip of document.querySelectorAll("[data-light-setting]")) {
  chip.addEventListener("click", () => {
    LIGHTS.setting = LIGHTS.setting === chip.dataset.lightSetting ? null : chip.dataset.lightSetting;
    if (LIGHTS.data) renderLightSettings();
  });
}

$("light-circuits").addEventListener("click", event => {
  const key = event.target.closest("[data-light-circuit]");
  if (key && LIGHTS.renaming === "room") { startLightRename(key); return; }
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

$("light-scenes").addEventListener("click", async event => {
  const tile = event.target.closest(".preset");
  const scene = tile && LIGHTS.data && LIGHTS.data.scenes.find(s => s.id === tile.dataset.sceneId);
  if (!scene) return;
  const trigger = event.target.closest("[data-preset-menu]");
  if (trigger) { toggleActionMenu(trigger, () => presetMenuItems(scene)); return; }
  const apply = event.target.closest("[data-light-scene]");
  if (apply) {
    tile.classList.add("applying");
    try {
      await lightRequest(apply, "POST", `/lights/scenes/${encodeURIComponent(scene.id)}/apply`);
    } finally {
      tile.classList.remove("applying");
    }
  }
});
$("light-scene-add").addEventListener("click", () => openLightSceneCreate());
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

// The blocks' "⋮": renaming the lights (stored on the service; an empty
// name goes back to the built-in one).
for (const toggle of document.querySelectorAll("[data-light-block-toggle]")) {
  toggle.addEventListener("click", () => {
    const block = toggle.dataset.lightBlockToggle;
    if (LIGHTS.renaming === block) setLightRenaming(null);
    setLightBlockFolded(block, toggle.getAttribute("aria-expanded") === "true");
  });
}
applyLightBlocks();

$("light-stage-menu").addEventListener("click", event => toggleActionMenu(event.currentTarget, [
  { label: LIGHTS.renaming === "stage" ? "Umbenennen beenden" : "Scheinwerfer umbenennen", icon: ICON_PENCIL,
    run: () => setLightRenaming(LIGHTS.renaming === "stage" ? null : "stage") },
]));
$("light-room-menu").addEventListener("click", event => toggleActionMenu(event.currentTarget, [
  { label: LIGHTS.renaming === "room" ? "Umbenennen beenden" : "Kreise umbenennen", icon: ICON_PENCIL,
    run: () => setLightRenaming(LIGHTS.renaming === "room" ? null : "room") },
]));
for (const done of document.querySelectorAll("[data-rename-done]")) {
  done.addEventListener("click", () => setLightRenaming(null));
}
