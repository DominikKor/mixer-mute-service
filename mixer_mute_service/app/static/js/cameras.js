// Mixer Mute Service UI (cameras.js): Camera streams, PTZ keys, tile menu.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- rendering ----------------------------------------------------------

function fmtAge(a) { return a === null || a === undefined ? "—" : `vor ${a.toFixed(1).replace(".", ",")} s`; }

let CAMERA_STREAMS_SIGNATURE = "";
let CAMERA_STREAMS = [];

// Every device gets the WebRTC player (lowest delay), signalled through the
// service's own port; it falls back to HLS by itself when the WebRTC media
// port of the streaming PC cannot be reached (static/camera-player.html).
function cameraPlayerUrl(cameraId) {
  return `/camera-player.html?cam=${encodeURIComponent(cameraId)}`;
}

function renderCameraStreams(data) {
  const grid = $("camera-grid");
  const cameras = data.cameras || [];
  const signature = JSON.stringify({ relay_ready: data.relay_ready, reason: data.reason, cameras });
  if (signature === CAMERA_STREAMS_SIGNATURE) return;
  CAMERA_STREAMS_SIGNATURE = signature;
  CAMERA_STREAMS = cameras;
  const status = $("camera-stream-status");
  status.textContent = data.relay_ready ? (cameras.length ? "MediaMTX bereit" : "") : (data.reason || "MediaMTX-Relay nicht bereit");
  status.classList.toggle("bad", !data.relay_ready);
  stopAllCameraMoves();
  grid.replaceChildren();
  $("camera-empty").hidden = cameras.length > 0;
  for (const camera of cameras) {
    const card = document.createElement("article");
    card.className = "camera-card";
    const head = document.createElement("div");
    head.className = "camera-card-head";
    const title = document.createElement("h3");
    title.className = "camera-card-title";
    title.textContent = camera.name;
    const menu = menuButton(`${camera.name}: bearbeiten, Presets umbenennen, Position speichern oder entfernen`, `camera:${camera.id}`);
    menu.dataset.cameraMenu = camera.id;
    head.append(title, menu);
    const view = document.createElement("div");
    view.className = "camera-view";
    if (data.relay_ready) {
      const frame = document.createElement("iframe");
      frame.src = cameraPlayerUrl(camera.id);
      frame.title = `${camera.name} live`;
      frame.allow = "autoplay; fullscreen";
      frame.allowFullscreen = true;
      view.append(frame);
    } else {
      const message = document.createElement("div");
      message.className = "camera-unavailable";
      message.textContent = data.reason || "MediaMTX-Relay nicht bereit";
      view.append(message);
    }
    const controls = document.createElement("div");
    controls.className = "camera-controls";
    controls.dataset.viscaCamera = viscaCameraId(camera.name);
    controls.hidden = true;
    card.append(head, view, controls);
    grid.append(card);
  }
  renderCameraControls();
}

// ---- PTZ controls in the camera section ----------------------------------
// A stream card named "Cam1"/"Cam2" gets that camera's VISCA presets (catalog
// camera keys with the labels they had in Steuerung, where they no longer
// appear) plus hold-to-move keys: arrows pan/tilt and Weit/Nah zoom while
// held, releasing the key stops the camera.

const CAMERA_LAST_PRESET = {};
// Cameras whose presets are being renamed (⋮ > Presets umbenennen): a tap
// on a preset then opens its name instead of moving the camera.
const CAMERA_RENAMING = new Set();
// Per camera and kind ("pan" / "zoom"): the command chain, the held direction
// and its timers.
const CAMERA_MOVES = {};
const CAMERA_MOVE_KINDS = {
  // The service stops a pan/tilt after 3 s without news, so a held arrow
  // repeats its direction every second.
  pan: { path: "pan-tilt", name: "Schwenken", keepalive: 1000, failsafe: 30000 },
  zoom: { path: "zoom", name: "Zoom", keepalive: 0, failsafe: 15000 },
};
const CAMERA_ARROWS = {
  up: { label: "nach oben neigen", path: "M12 19V5M5.5 11.5L12 5l6.5 6.5" },
  left: { label: "nach links schwenken", path: "M19 12H5M11.5 5.5L5 12l6.5 6.5" },
  down: { label: "nach unten neigen", path: "M12 5v14M5.5 12.5L12 19l6.5-6.5" },
  right: { label: "nach rechts schwenken", path: "M5 12h14M12.5 5.5L19 12l-6.5 6.5" },
};

function viscaCameraId(name) {
  return (name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function cameraPresetLabel(label) {
  return label.replace(/^\s*cam\s*\d+\s*/i, "").replace(/^pset\s+/i, "").trim() || label;
}

// VISCA preset numbers the service recalls and stores (app/native_controls.py).
const CAMERA_PRESET_MIN = 1;
const CAMERA_PRESET_MAX = 16;

// Each camera's presets (catalog keys and presets saved here), by number.
function cameraPresetLists() {
  const presets = {};
  for (const button of COMPANION_DATA.camera_buttons || []) {
    const action = button.native_action;
    if (!action || action.type !== "visca-preset") continue;
    (presets[action.camera] = presets[action.camera] || []).push({
      id: button.id, label: button.label, preset: action.preset, saved: Boolean(button.saved),
      defaultLabel: button.default_label || button.label,
    });
  }
  for (const list of Object.values(presets)) list.sort((a, b) => a.preset - b.preset);
  return presets;
}

// A typed preset number, or null when it is not a whole number 1 to 16.
function cameraPresetNumber(text) {
  const value = String(text || "").trim();
  if (!/^\d{1,2}$/.test(value)) return null;
  const n = Number(value);
  return n >= CAMERA_PRESET_MIN && n <= CAMERA_PRESET_MAX ? n : null;
}

function renderCameraControls() {
  const presets = cameraPresetLists();
  for (const controls of document.querySelectorAll(".camera-controls")) {
    const cam = controls.dataset.viscaCamera;
    const list = presets[cam] || [];
    // Only cameras the catalog knows (cam1, cam2) get controls.
    controls.hidden = !list.length;
    const name = controls.closest(".camera-card").querySelector(".camera-card-title").textContent;
    // Rebuild only on a real change, so a held key is never replaced.
    const obsKeys = cameraObsKeys(cam);
    const signature = JSON.stringify([name, list, obsKeys]);
    if (controls.dataset.signature === signature) continue;
    controls.dataset.signature = signature;
    stopCameraMoves(cam);
    if (!list.length) { controls.replaceChildren(); continue; }
    const c = escapeAttr(cam);
    const n = escapeAttr(name);
    const arrows = Object.entries(CAMERA_ARROWS).map(([direction, arrow]) => `
        <button type="button" class="camera-move-key camera-arrow" data-camera-move="pan" data-direction="${direction}" data-camera="${c}"
          title="${n} ${arrow.label}" aria-label="${n} ${arrow.label}, gedrückt halten">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${arrow.path}"/></svg></button>`).join("");
    controls.innerHTML = `
      <div class="camera-presets-head">
        <div class="camera-controls-label">Presets</div>
        <form class="camera-recall" data-camera-recall="${c}" novalidate>
          <input type="text" inputmode="numeric" pattern="[0-9]*" maxlength="2" class="camera-recall-number" data-camera="${c}"
            placeholder="Nr." autocomplete="off" aria-label="${n}: Preset-Nummer ${CAMERA_PRESET_MIN} bis ${CAMERA_PRESET_MAX}" title="Preset-Nummer ${CAMERA_PRESET_MIN} bis ${CAMERA_PRESET_MAX}" />
          <button type="submit" class="camera-recall-go" aria-label="${n}: Preset mit dieser Nummer anfahren">Anfahren</button>
        </form>
      </div>
      <div class="rename-bar" data-camera-rename-bar hidden><span>Preset antippen zum Umbenennen</span><button type="button" class="primary" data-camera-rename-done="${c}">Fertig</button></div>
      <div class="camera-presets">${list.map(p => `
        <button type="button" class="camera-preset" data-camera-preset="${escapeAttr(p.id)}" data-camera="${c}" data-preset="${p.preset}"
          data-label="${escapeAttr(p.label)}" data-default-label="${escapeAttr(p.defaultLabel)}" data-saved="${p.saved}"
          aria-pressed="${CAMERA_LAST_PRESET[cam] === p.id}" title="Preset ${p.preset}: ${escapeAttr(cameraPresetLabel(p.label))}"><b class="camera-preset-num">${p.preset}</b> · ${escapeHtml(cameraPresetLabel(p.label))}</button>`).join("")}
      </div>
      <div class="camera-controls-label">Schwenken &amp; Zoom – gedrückt halten</div>
      <div class="camera-ptz${obsKeys.length ? "" : " no-obs"}">${arrows}
        <button type="button" class="camera-move-key camera-zoom-key" data-camera-move="zoom" data-direction="out" data-camera="${c}"
          title="${n} Zoom − (weiter), gedrückt halten" aria-label="${n} Zoom minus, herauszoomen, gedrückt halten">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M7.5 10.5h6"/></svg></button>
        <button type="button" class="camera-move-key camera-zoom-key" data-camera-move="zoom" data-direction="in" data-camera="${c}"
          title="${n} Zoom + (näher), gedrückt halten" aria-label="${n} Zoom plus, hineinzoomen, gedrückt halten">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M7.5 10.5h6M10.5 7.5v6"/></svg></button>
        ${obsKeys.map(cameraObsKeyHtml).join("")}
      </div>`;
    setCameraRenaming(cam, CAMERA_RENAMING.has(cam));
  }
  // New Live/Bibel keys get their colour now, not at the next poll.
  if (typeof applyObsStatus === "function") applyObsStatus();
}

// ---- Live and Bibel keys of a camera ----------------------------------------
// Shortcuts to the Steuerung keys "Cam N Live" and "Bibel CamN": the same
// key IDs, pressed through the same route, coloured by the same OBS status
// (controls.js applyObsStatus: red = on air, green = preview / Bibel text
// shown in a scene that is not on air). A key the user removed from
// Steuerung is not shown here either (it could not be pressed).
const CAMERA_OBS_KEYS = [
  { kind: "live", id: cam => `native-obs-${cam}-live` },
  { kind: "bible", id: cam => `native-obs-bible-${cam}` },
];

function cameraObsKeys(cam) {
  const keys = [];
  for (const { kind, id } of CAMERA_OBS_KEYS) {
    const info = (COMPANION_DATA.buttons || []).find(item => item.id === id(cam));
    if (!info) continue;
    const role = obsKeyRole(info);
    keys.push({ kind, id: info.id, label: info.label, scene: role.scene || null, overlay: role.overlay || null });
  }
  return keys;
}

function cameraObsKeyHtml(key) {
  const role = key.overlay
    ? ` data-obs-overlay-scene="${escapeAttr(key.overlay.scene)}" data-obs-overlay-source="${escapeAttr(key.overlay.source)}"`
    : key.scene ? ` data-obs-scene="${escapeAttr(key.scene)}"` : "";
  return `
        <button type="button" class="companion-key native camera-obs-key camera-obs-${key.kind}" data-camera-obs-press="${escapeAttr(key.id)}"${role}
          title="${escapeAttr(key.label)} (wie in der Steuerung)">${escapeHtml(key.label)}</button>`;
}

const CAMERA_OBS_BUSY = new Set();

// Exactly the Steuerung key's press (POST /companion/buttons/{id}/press).
async function pressCameraObsKey(key) {
  const id = key.dataset.cameraObsPress;
  if (CAMERA_OBS_BUSY.has(id)) return;
  CAMERA_OBS_BUSY.add(id);
  key.disabled = true;
  try {
    const result = await api("POST", `/companion/buttons/${encodeURIComponent(id)}/press`);
    if (!result.ok || result.data.ok === false) {
      toast(`${key.textContent.trim()}: ${result.data.reason || "HTTP " + result.status}`, false);
      return;
    }
    if (result.data.warning) toast(`${key.textContent.trim()}: ${result.data.warning}`, false);
    refreshObsStatus();  // show the cut without waiting for the poll
  } finally {
    CAMERA_OBS_BUSY.delete(id);
    key.disabled = false;
  }
}

// Rename mode of one camera's presets on or off.
function setCameraRenaming(cam, on) {
  if (on) CAMERA_RENAMING.add(cam); else CAMERA_RENAMING.delete(cam);
  for (const controls of document.querySelectorAll(".camera-controls")) {
    if (controls.dataset.viscaCamera !== cam) continue;
    controls.classList.toggle("renaming", on);
    const bar = controls.querySelector("[data-camera-rename-bar]");
    if (bar) bar.hidden = !on;
  }
}

// The preset's name as a field; stored on the service with the camera in
// front ("CAM1 Kanzel"), so program rows still tell the cameras apart.
function startCameraPresetRename(key) {
  const cam = key.dataset.camera;
  const fallback = cameraPresetLabel(key.dataset.defaultLabel);
  const shown = key.dataset.label === key.dataset.defaultLabel ? "" : cameraPresetLabel(key.dataset.label);
  let slot = key.parentElement;
  if (!slot.classList.contains("camera-preset-slot")) {
    slot = document.createElement("div");
    slot.className = "camera-preset-slot";
    key.replaceWith(slot);
    slot.appendChild(key);
  }
  // A preset saved here has no catalog name to fall back to.
  const saved = key.dataset.saved === "true";
  startInlineRename(slot, saved ? cameraPresetLabel(key.dataset.label) : shown, {
    placeholder: fallback, allowEmpty: !saved, maxLength: 58,
    save: async name => {
      const label = name ? `${cam.toUpperCase()} ${name}` : "";
      if (await saveName("PUT", `/camera-presets/${encodeURIComponent(key.dataset.cameraPreset)}/label`, label, "Preset")) {
        await loadCompanionButtons();
        loadProgramItems();  // program rows list the presets by name
      }
    },
  });
}

async function pressCameraPreset(key) {
  if (key.closest(".camera-controls.renaming")) return startCameraPresetRename(key);
  if (key.disabled) return;
  key.disabled = true;
  try {
    const result = await api("POST", `/companion/buttons/${encodeURIComponent(key.dataset.cameraPreset)}/press`);
    if (!result.ok || result.data.ok === false) {
      toast(`${key.title}: ${result.data.reason || "HTTP " + result.status}`, false);
      return;
    }
    markCameraPreset(key.dataset.camera, key.dataset.cameraPreset);
  } finally {
    key.disabled = false;
  }
}

// The camera's last recalled preset stays lit (none: a number without a key).
function markCameraPreset(cam, id) {
  CAMERA_LAST_PRESET[cam] = id;
  document.querySelectorAll(`[data-camera-preset][data-camera="${cam}"]`).forEach(b => {
    b.setAttribute("aria-pressed", String(b.dataset.cameraPreset === id));
  });
}

// "Nr. [  ] Anfahren": any preset number, also one without a name here. The
// service runs it exactly like a preset key.
async function recallCameraPresetNumber(form) {
  const cam = form.dataset.cameraRecall;
  const input = form.querySelector(".camera-recall-number");
  const go = form.querySelector(".camera-recall-go");
  const preset = cameraPresetNumber(input.value);
  input.setAttribute("aria-invalid", String(preset === null));
  if (preset === null) {
    toast(`Preset-Nummer ${CAMERA_PRESET_MIN} bis ${CAMERA_PRESET_MAX} eingeben`, false);
    input.focus();
    return;
  }
  if (go.disabled) return;
  go.disabled = true;
  try {
    const result = await api("POST", `/cameras/${encodeURIComponent(cam)}/preset/recall`, { preset });
    if (!result.ok || result.data.ok === false) {
      toast(`${cam.toUpperCase()} Preset ${preset}: ${result.data.reason || "HTTP " + result.status}`, false);
      return;
    }
    const known = (cameraPresetLists()[cam] || []).find(p => p.preset === preset);
    markCameraPreset(cam, known ? known.id : null);
    input.blur();  // the iPad keyboard goes away
  } finally {
    go.disabled = false;
  }
}

// ---- Aktuelle Position speichern -----------------------------------------
// Number and name. Suggested is the lowest number that has no name here and
// that nothing uses (program rows, shortcuts, templates, Steuerung keys
// running a preset). The camera stores its current position at that
// number, the service names it. A number with a name or a user is only
// overwritten after asking. "No name in the app" is not "free": the camera
// may hold a position there that Companion or the remote stored.
let CAMERA_PRESET_DIALOG = null;  // { cam, cameraName, used: {number: [labels]} | null }

function cameraPresetAt(cam, preset) {
  return (cameraPresetLists()[cam] || []).find(p => p.preset === preset) || null;
}

function cameraPresetUsers(preset) {
  const used = CAMERA_PRESET_DIALOG && CAMERA_PRESET_DIALOG.used;
  return (used && used[String(preset)]) || [];
}

function nextFreeCameraPreset(cam) {
  const named = new Set((cameraPresetLists()[cam] || []).map(p => p.preset));
  for (let n = CAMERA_PRESET_MIN; n <= CAMERA_PRESET_MAX; n++) {
    if (!named.has(n) && !cameraPresetUsers(n).length) return n;
  }
  return null;
}

// "A, B, C und 2 weitere"
function cameraPresetUserList(users, limit = 4) {
  const shown = users.slice(0, limit).join(", ");
  return users.length > limit ? `${shown} und ${users.length - limit} weitere` : shown;
}

async function openCameraPresetDialog(cam, cameraName) {
  const usage = await api("GET", `/cameras/${encodeURIComponent(cam)}/presets/usage`);
  // Without the list the service still asks on saving (409 with its users).
  const used = usage.ok && usage.data.ok !== false ? usage.data.used || {} : null;
  CAMERA_PRESET_DIALOG = { cam, cameraName, used };
  const free = nextFreeCameraPreset(cam);
  $("camera-preset-title").textContent = `${cameraName}: aktuelle Position speichern`;
  $("camera-preset-number").value = free === null ? "" : String(free);
  $("camera-preset-name").value = "";
  $("camera-preset-error").hidden = true;
  updateCameraPresetHint();
  $("camera-preset-dialog").showModal();
  $("camera-preset-name").focus();
}

function updateCameraPresetHint() {
  const state = CAMERA_PRESET_DIALOG;
  if (!state) return;
  const hint = $("camera-preset-hint");
  const raw = $("camera-preset-number").value;
  const preset = cameraPresetNumber(raw);
  const existing = preset === null ? null : cameraPresetAt(state.cam, preset);
  const users = preset === null ? [] : cameraPresetUsers(preset);
  hint.classList.toggle("warn", Boolean(existing || users.length));
  if (preset === null) {
    hint.textContent = raw.trim() || nextFreeCameraPreset(state.cam) !== null
      ? `Nummer ${CAMERA_PRESET_MIN} bis ${CAMERA_PRESET_MAX} eingeben.`
      : `Alle Nummern ${CAMERA_PRESET_MIN} bis ${CAMERA_PRESET_MAX} haben einen Namen oder werden benutzt. Eine wählen, um sie zu überschreiben.`;
  } else if (existing || users.length) {
    const what = existing ? `Nr. ${preset} ist „${cameraPresetLabel(existing.label)}“` : `Nr. ${preset} hat in der App keinen Namen`;
    const by = users.length ? `, benutzt von: ${cameraPresetUserList(users)}` : "";
    hint.textContent = `${what}${by}. Speichern fragt, ob überschrieben werden soll.`;
  } else {
    hint.textContent = `Nr. ${preset} hat in der App keinen Namen und wird nirgends benutzt`
      + `${state.used ? "" : " (Benutzung nicht geprüft)"}. Die Kamera speichert dort ihre aktuelle Position;`
      + " was sie dort vielleicht schon hatte (Companion, Fernbedienung), wird ersetzt.";
  }
}

async function saveCameraPreset(event) {
  event.preventDefault();
  const state = CAMERA_PRESET_DIALOG;
  if (!state) return;
  const error = $("camera-preset-error");
  const saveKey = $("camera-preset-save");
  const preset = cameraPresetNumber($("camera-preset-number").value);
  const name = $("camera-preset-name").value.trim().replace(/\s+/g, " ");
  const problem = preset === null ? `Nummer ${CAMERA_PRESET_MIN} bis ${CAMERA_PRESET_MAX} eingeben.` : !name ? "Einen Namen eingeben." : "";
  error.textContent = problem;
  error.hidden = !problem;
  if (problem) { (preset === null ? $("camera-preset-number") : $("camera-preset-name")).focus(); return; }
  if (saveKey.disabled) return;
  saveKey.disabled = true;
  try {
    const path = `/cameras/${encodeURIComponent(state.cam)}/presets`;
    let result = await api("POST", path, { preset, name });
    if (result.status === 409 && result.data.exists) {
      // The service's answer: its name here and who uses the number
      // (program rows, shortcuts, templates, keys).
      const users = result.data.users || [];
      const what = result.data.name ? `Nr. ${preset} „${result.data.name}“` : `Nr. ${preset}`;
      const by = users.length ? ` Benutzt von: ${cameraPresetUserList(users, 6)}.` : "";
      const question = `${what} überschreiben?${by} Die Kamera speichert dort ihre aktuelle Position.`;
      if (!(await confirmDelete(saveKey, question, "Überschreiben"))) return;
      result = await api("POST", path, { preset, name, overwrite: true });
    }
    if (!result.ok || result.data.ok === false) {
      error.textContent = result.data.reason || `HTTP ${result.status}`;
      error.hidden = false;
      return;
    }
    $("camera-preset-dialog").close();
    toast(`${state.cameraName}: Preset ${preset} · ${name} gespeichert`, true);
    await loadCompanionButtons();
    markCameraPreset(state.cam, result.data.button && result.data.button.id);
    loadProgramItems();  // program rows list the presets by name
  } finally {
    saveKey.disabled = false;
  }
}

function cameraMoveState(camera, kind) {
  const id = `${camera}:${kind}`;
  return CAMERA_MOVES[id] || (CAMERA_MOVES[id] = { camera, kind, chain: Promise.resolve(), direction: null, timer: null, keepalive: null });
}

// Commands of one camera and kind go out strictly in order, so a quick tap
// never lets "stop" overtake its move.
function sendCameraMove(state, direction) {
  const kind = CAMERA_MOVE_KINDS[state.kind];
  state.chain = state.chain
    .then(() => api("POST", `/cameras/${encodeURIComponent(state.camera)}/${kind.path}`, { direction }))
    .then(result => {
      if (!result.ok || result.data.ok === false) toast(`${kind.name} ${state.camera}: ${result.data.reason || "HTTP " + result.status}`, false);
    })
    .catch(() => toast(`${kind.name} ${state.camera}: Dienst nicht erreichbar`, false));
}

function startCameraMove(key) {
  const state = cameraMoveState(key.dataset.camera, key.dataset.cameraMove);
  const direction = key.dataset.direction;
  if (state.direction === direction) return;
  if (state.direction) stopCameraMove(state);
  const kind = CAMERA_MOVE_KINDS[state.kind];
  state.direction = direction;
  sendCameraMove(state, direction);
  key.classList.add("active");
  if (kind.keepalive) {
    // Only while the previous command is through, so a slow network never
    // piles up requests.
    state.keepalive = setInterval(() => {
      if (state.direction === direction && !state.pending) {
        state.pending = true;
        sendCameraMove(state, direction);
        state.chain = state.chain.finally(() => { state.pending = false; });
      }
    }, kind.keepalive);
  }
  // Failsafe: never keep moving if a release got lost.
  state.timer = setTimeout(() => stopCameraMove(state), kind.failsafe);
}

function stopCameraMove(state) {
  if (!state || !state.direction) return;
  state.direction = null;
  clearTimeout(state.timer);
  clearInterval(state.keepalive);
  document.querySelectorAll(`[data-camera-move="${state.kind}"][data-camera="${state.camera}"].active`).forEach(b => b.classList.remove("active"));
  sendCameraMove(state, "stop");
}

function stopCameraMoves(camera) {
  for (const state of Object.values(CAMERA_MOVES)) if (state.camera === camera) stopCameraMove(state);
}

function stopAllCameraMoves() {
  for (const state of Object.values(CAMERA_MOVES)) stopCameraMove(state);
}

function stopCameraMoveKey(key) {
  stopCameraMove(CAMERA_MOVES[`${key.dataset.camera}:${key.dataset.cameraMove}`]);
}

let DESKTOP_PROFILES_SIGNATURE = "";
function renderDesktopProfiles(data) {
  const wrap = $("desktop-profiles");
  MISSION_SPOTLIGHT = data.mission_spotlight || null;
  const signature = JSON.stringify({ ready: data.ready, profiles: data.profiles || [] });
  if (signature === DESKTOP_PROFILES_SIGNATURE) return;
  DESKTOP_PROFILES_SIGNATURE = signature;

  DESKTOP_PROFILES = data.profiles || [];
  // Program rows offer the same profiles.
  PROGRAM_DESKTOP_PROFILES = DESKTOP_PROFILES;
  renderProgramItems();
  const status = $("desktop-launch-status");
  const addTile = $("new-profile-btn");
  wrap.querySelectorAll(".display-tile").forEach(el => el.remove());
  for (const profile of data.profiles || []) {
    const audio = profile.kind === "audio";
    const where = targetPcName(profile.target_ip) || profile.target_ip;
    const screens = profile.monitors && profile.monitors.length > 1 ? `Monitore ${profile.monitors.join(", ")}` : `Monitor ${profile.monitor}`;
    const meta = audio ? `${where} · Audio${profile.fade ? ` · Fade ${String(profile.fade_seconds).replace(".", ",")} s` : ""}` : `${where} · ${screens}${profile.fullscreen ? " · Vollbild" : ""}`;
    const spotlight = profile.mission_spotlight && MISSION_SPOTLIGHT
      ? `Spotlight Q${MISSION_SPOTLIGHT.quarter} · Woche ${MISSION_SPOTLIGHT.week} · ` : "";
    const button = document.createElement("button");
    button.className = "display-key";
    if (audio) {
      button.dataset.audioProfile = profile.id;
      button.dataset.audioAction = "play";
    } else {
      button.dataset.displayProfile = profile.id;
    }
    button.dataset.label = profile.title;
    button.disabled = !data.ready;
    button.title = audio ? `${profile.title} auf ${profile.target_ip} abspielen` : `${profile.title} · ${meta}`;
    const led = document.createElement("span");
    led.className = "led" + (data.ready ? " safe" : "");
    const title = document.createElement("span");
    title.className = "display-key-title";
    title.textContent = profile.title;
    const metaEl = document.createElement("span");
    metaEl.className = "display-key-meta";
    metaEl.textContent = spotlight + meta;
    button.append(led, title, metaEl);
    const stop = document.createElement("button");
    if (audio) {
      stop.type = "button";
      stop.className = "display-audio-stop";
      stop.dataset.audioProfile = profile.id;
      stop.dataset.audioAction = "stop";
      stop.dataset.label = `Audio ${profile.target_ip}`;
      stop.textContent = "Stop";
      stop.title = profile.fade ? `Musik auf ${profile.target_ip} langsam ausblenden und stoppen` : `Musik auf ${profile.target_ip} stoppen`;
      stop.setAttribute("aria-label", stop.title);
      stop.disabled = !data.ready;
    } else if (profile.kind !== "program") {
      // Something shown (an image, a video, a document): its stop closes it
      // on its PC, started by hand or by the program. Programs (OBS) have none.
      stop.type = "button";
      stop.className = "display-audio-stop";
      stop.dataset.displayClose = profile.id;
      stop.dataset.label = profile.title;
      stop.textContent = "Stop";
      stop.title = `${profile.title} auf ${profile.target_ip} schließen`;
      stop.setAttribute("aria-label", stop.title);
      stop.disabled = !data.ready;
    }
    const closable = audio || profile.kind !== "program";
    // Edit/duplicate/delete sit in the "⋮" menu; it works even while the agent is not set up.
    const menuBtn = menuButton(`${profile.title}: bearbeiten, duplizieren oder löschen`, `profile:${profile.id}`);
    menuBtn.classList.add("tile-menu-btn");
    menuBtn.dataset.profileMenu = profile.id;
    const tile = document.createElement("span");
    tile.className = "display-tile" + (audio ? " audio" : "") + (closable ? " has-stop" : "");
    tile.append(button);
    if (closable) tile.append(stop);
    tile.append(menuBtn);
    wrap.insertBefore(tile, addTile);
  }
  if (OPEN_MENU && OPEN_MENU.key && OPEN_MENU.key.startsWith("profile:")) closeTileMenu();
  status.textContent = !DESKTOP_PROFILES.length ? "Noch keine Profile – mit + anlegen"
    : data.ready ? "Profile bereit" : "Desktop-Agent nicht eingerichtet";
}

let CAMERA_FORM_ID = null;
function cameraFormError(message, field) {
  document.querySelectorAll("#camera-form .invalid").forEach(el => el.classList.remove("invalid"));
  const error = $("camera-form-error");
  error.hidden = !message;
  error.textContent = message || "";
  if (field) { field.classList.add("invalid"); field.focus(); }
}

function openCameraDialog(cameraId = null) {
  const camera = cameraId ? CAMERA_STREAMS.find(item => item.id === cameraId) : null;
  if (cameraId && !camera) return toast("Kamera nicht gefunden", false);
  CAMERA_FORM_ID = camera ? camera.id : null;
  $("camera-dialog-title").textContent = camera ? "Kamera bearbeiten" : "Kamera hinzufügen";
  $("camera-name").value = camera ? camera.name : "";
  $("camera-url").value = "";
  $("camera-url").placeholder = camera ? "Leer lassen, um die Adresse beizubehalten" : "rtsp://192.168.5.21:554/stream1";
  $("camera-username").value = "";
  $("camera-password").value = "";
  $("camera-auth-hint").textContent = camera && camera.has_credentials
    ? "Adresse und Zugangsdaten gespeichert. Leere Felder behalten die bisherigen Werte bei."
    : "Zugangsdaten werden lokal gespeichert und nicht in der Kameraliste angezeigt. Eine leere Adresse behält den bisherigen Pfad bei.";
  cameraFormError(null);
  $("camera-save").disabled = false;
  $("camera-dialog").showModal();
  $("camera-name").focus();
}

function readCameraForm() {
  const name = $("camera-name").value.trim();
  const rtspUrl = $("camera-url").value.trim();
  if (!name) { cameraFormError("Bitte einen Namen eingeben.", $("camera-name")); return null; }
  if (!rtspUrl && !CAMERA_FORM_ID) {
    cameraFormError("Bitte eine RTSP-Adresse eingeben.", $("camera-url"));
    return null;
  }
  if (rtspUrl) {
    try {
      const parsed = new URL(rtspUrl);
      if (!["rtsp:", "rtsps:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error();
    } catch (_) {
      cameraFormError("RTSP-Adresse ohne Benutzername/Passwort eingeben, z. B. rtsp://192.168.5.21:554/stream1", $("camera-url"));
      return null;
    }
  }
  return {
    name,
    rtsp_url: rtspUrl,
    username: $("camera-username").value,
    password: $("camera-password").value,
  };
}

async function saveCamera(event) {
  event.preventDefault();
  const body = readCameraForm();
  if (!body) return;
  cameraFormError(null);
  $("camera-save").disabled = true;
  const result = CAMERA_FORM_ID
    ? await api("PUT", `/camera-streams/${encodeURIComponent(CAMERA_FORM_ID)}`, body)
    : await api("POST", "/camera-streams", body);
  $("camera-save").disabled = false;
  if (!result.ok || result.data.ok === false) {
    cameraFormError(result.data.reason || `HTTP ${result.status}`);
    return;
  }
  $("camera-dialog").close();
  CAMERA_STREAMS_SIGNATURE = "";
  await loadCameraStreams();
  toast(result.data.relay_ready ? `${body.name}: gespeichert` : (result.data.reason || "Kamera gespeichert; MediaMTX fehlt"), !!result.data.relay_ready);
}

async function deleteCamera(cameraId, button) {
  const camera = CAMERA_STREAMS.find(item => item.id === cameraId);
  if (!camera || !(await confirmDelete(button, `Kamera „${camera.name}“ entfernen?`))) return;
  const result = await api("DELETE", `/camera-streams/${encodeURIComponent(cameraId)}`);
  if (!result.ok || result.data.ok === false) {
    toast(result.data.reason || `HTTP ${result.status}`, false);
    return;
  }
  CAMERA_STREAMS_SIGNATURE = "";
  await loadCameraStreams();
  toast(`${camera.name}: entfernt`, true);
}

async function loadCameraStreams() {
  const result = await api("GET", "/camera-streams");
  if (result.ok) renderCameraStreams(result.data);
  else {
    const status = $("camera-stream-status");
    status.textContent = result.data.reason || "Kamera-Status nicht erreichbar";
    status.classList.add("bad");
  }
}

// The copy ("… (Kopie)") is placed right after the original; its editor opens
// so it can be renamed or pointed at another file straight away.
async function duplicateDesktopProfile(profileId) {
  const profile = DESKTOP_PROFILES.find(p => p.id === profileId);
  const result = await api("POST", `/desktop/profiles/${encodeURIComponent(profileId)}/duplicate`);
  if (!result.ok || result.data.ok === false) {
    toast(result.data.reason || `HTTP ${result.status}`, false);
    return;
  }
  toast(`„${profile ? profile.title : profileId}“ dupliziert`, true);
  DESKTOP_PROFILES_SIGNATURE = "";
  await refresh();
  if (DESKTOP_PROFILES.some(p => p.id === result.data.id)) openProfileDialog(result.data.id);
}

async function deleteDesktopProfile(profileId, anchor) {
  const profile = DESKTOP_PROFILES.find(p => p.id === profileId);
  const question = `„${profile ? profile.title : profileId}“ löschen?`;
  if (!anchor || !anchor.isConnected || !(await confirmDelete(anchor, question))) return;
  const result = await api("DELETE", `/desktop/profiles/${encodeURIComponent(profileId)}`);
  if (!result.ok || result.data.ok === false) {
    toast(result.data.reason || `HTTP ${result.status}`, false);
    return;
  }
  DESKTOP_PROFILES_SIGNATURE = "";
  await refresh();
}

const ICON_PRESET_SAVE = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/></svg>`;

$("camera-grid").addEventListener("click", event => {
  const key = event.target.closest("button[data-camera-obs-press]");
  if (key && !key.disabled) pressCameraObsKey(key);
});
$("camera-grid").addEventListener("submit", event => {
  const form = event.target.closest("form[data-camera-recall]");
  if (!form) return;
  event.preventDefault();
  recallCameraPresetNumber(form);
});
$("camera-grid").addEventListener("input", event => {
  if (event.target.classList.contains("camera-recall-number")) event.target.removeAttribute("aria-invalid");
});
$("camera-preset-form").addEventListener("submit", saveCameraPreset);
$("camera-preset-number").addEventListener("input", updateCameraPresetHint);
$("camera-preset-cancel").addEventListener("click", () => $("camera-preset-dialog").close());
$("camera-preset-dialog").addEventListener("close", () => { CAMERA_PRESET_DIALOG = null; });

// The pencil next to "Presets": renaming on/off for that camera.
$("camera-grid").addEventListener("click", event => {
  const done = event.target.closest("[data-camera-rename-done]");
  if (done) { endRenameMode(); return; }
  const trigger = event.target.closest("[data-camera-menu]");
  if (!trigger) return;
  const camera = CAMERA_STREAMS.find(item => item.id === trigger.dataset.cameraMenu);
  if (!camera) return;
  const cam = viscaCameraId(camera.name);
  const controls = trigger.closest(".camera-card").querySelector(".camera-controls");
  toggleActionMenu(trigger, [
    { label: "Bearbeiten", icon: ICON_PENCIL, run: () => openCameraDialog(camera.id) },
    controls && !controls.hidden ? {
      label: CAMERA_RENAMING.has(cam) ? "Umbenennen beenden" : "Presets umbenennen", icon: ICON_EDIT_LIST,
      run: () => {
        if (CAMERA_RENAMING.has(cam)) { endRenameMode(); return; }
        startRenameMode(controls, () => setCameraRenaming(cam, false));
        setCameraRenaming(cam, true);
      },
    } : null,
    controls && !controls.hidden ? {
      label: "Aktuelle Position speichern", icon: ICON_PRESET_SAVE, run: () => openCameraPresetDialog(cam, camera.name),
    } : null,
    "-",
    { label: "Entfernen", icon: ICON_TRASH, danger: true, run: anchor => deleteCamera(camera.id, anchor) },
  ]);
});
