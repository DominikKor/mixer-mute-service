// Mixer Mute Service UI (cameras.js): Camera streams, PTZ keys, tile menu.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- rendering ----------------------------------------------------------

function fmtAge(a) { return a === null || a === undefined ? "—" : a.toFixed(2) + "s"; }

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
    const tools = document.createElement("div");
    tools.className = "camera-card-tools";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.dataset.cameraEdit = camera.id;
    edit.title = `${camera.name} bearbeiten`;
    edit.setAttribute("aria-label", edit.title);
    edit.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg>';
    const remove = document.createElement("button");
    remove.type = "button";
    remove.dataset.cameraDelete = camera.id;
    remove.title = `${camera.name} entfernen`;
    remove.setAttribute("aria-label", remove.title);
    remove.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>';
    tools.append(edit, remove);
    head.append(title, tools);
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

function renderCameraControls() {
  const presets = {};
  for (const button of COMPANION_DATA.camera_buttons || []) {
    const action = button.native_action;
    if (!action || action.type !== "visca-preset") continue;
    (presets[action.camera] = presets[action.camera] || []).push({ id: button.id, label: button.label, preset: action.preset });
  }
  for (const controls of document.querySelectorAll(".camera-controls")) {
    const cam = controls.dataset.viscaCamera;
    const list = (presets[cam] || []).sort((a, b) => a.preset - b.preset);
    // Only cameras the catalog knows (cam1, cam2) get controls.
    controls.hidden = !list.length;
    const name = controls.closest(".camera-card").querySelector(".camera-card-title").textContent;
    // Rebuild only on a real change, so a held key is never replaced.
    const signature = JSON.stringify([name, list]);
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
      <div class="camera-controls-label">Presets</div>
      <div class="camera-presets">${list.map(p => `
        <button type="button" class="camera-preset" data-camera-preset="${escapeAttr(p.id)}" data-camera="${c}"
          aria-pressed="${CAMERA_LAST_PRESET[cam] === p.id}" title="${escapeAttr(p.label)} (Preset ${p.preset})">${escapeHtml(cameraPresetLabel(p.label))}</button>`).join("")}
      </div>
      <div class="camera-controls-label">Schwenken &amp; Zoom – gedrückt halten</div>
      <div class="camera-ptz">${arrows}
        <button type="button" class="camera-move-key camera-zoom-key" data-camera-move="zoom" data-direction="in" data-camera="${c}" aria-label="${n} hineinzoomen, gedrückt halten">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M7.5 10.5h6M10.5 7.5v6"/></svg>Nah</button>
        <button type="button" class="camera-move-key camera-zoom-key" data-camera-move="zoom" data-direction="out" data-camera="${c}" aria-label="${n} herauszoomen, gedrückt halten">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M7.5 10.5h6"/></svg>Weit</button>
      </div>`;
  }
}

async function pressCameraPreset(key) {
  if (key.disabled) return;
  key.disabled = true;
  try {
    const result = await api("POST", `/companion/buttons/${encodeURIComponent(key.dataset.cameraPreset)}/press`);
    if (!result.ok || result.data.ok === false) {
      toast(`${key.title}: ${result.data.reason || "HTTP " + result.status}`, false);
      return;
    }
    CAMERA_LAST_PRESET[key.dataset.camera] = key.dataset.cameraPreset;
    document.querySelectorAll(`[data-camera-preset][data-camera="${key.dataset.camera}"]`).forEach(b => {
      b.setAttribute("aria-pressed", String(b === key));
    });
  } finally {
    key.disabled = false;
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
  const signature = JSON.stringify({ ready: data.ready, profiles: data.profiles || [] });
  if (signature === DESKTOP_PROFILES_SIGNATURE) return;
  DESKTOP_PROFILES_SIGNATURE = signature;

  DESKTOP_PROFILES = data.profiles || [];
  const status = $("desktop-launch-status");
  const addTile = $("new-profile-btn");
  wrap.querySelectorAll(".display-tile").forEach(el => el.remove());
  for (const profile of data.profiles || []) {
    const audio = profile.kind === "audio";
    const where = targetPcName(profile.target_ip) || profile.target_ip;
    const meta = audio ? `${where} · Audio${profile.fade ? ` · Fade ${String(profile.fade_seconds).replace(".", ",")} s` : ""}` : `${where} · Monitor ${profile.monitor}${profile.fullscreen ? " · Vollbild" : ""}`;
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
    metaEl.textContent = meta;
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
    }
    // Edit/duplicate/delete sit in the "⋮" menu; it works even while the agent is not set up.
    const menuBtn = document.createElement("button");
    menuBtn.className = "display-menu-btn";
    menuBtn.dataset.profileMenu = profile.id;
    menuBtn.title = `${profile.title}: bearbeiten, duplizieren oder löschen`;
    menuBtn.setAttribute("aria-label", menuBtn.title);
    menuBtn.setAttribute("aria-haspopup", "menu");
    menuBtn.setAttribute("aria-expanded", "false");
    menuBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>';
    const tile = document.createElement("span");
    tile.className = audio ? "display-tile audio" : "display-tile";
    tile.append(button);
    if (audio) tile.append(stop);
    tile.append(menuBtn);
    wrap.insertBefore(tile, addTile);
  }
  closeTileMenu();
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

// ---- tile menu ("⋮") ------------------------------------------------------
// One shared menu, placed under the dots of the tile it was opened from.

let TILE_MENU_PROFILE = null;

function openTileMenu(trigger) {
  closeTileMenu();
  const menu = $("tile-menu");
  TILE_MENU_PROFILE = trigger.dataset.profileMenu;
  trigger.setAttribute("aria-expanded", "true");
  menu.hidden = false;
  const r = trigger.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  const left = Math.min(Math.max(8, r.right - w), innerWidth - w - 8);
  const top = r.bottom + 4 + h > innerHeight - 8 ? Math.max(8, r.top - h - 4) : r.bottom + 4;
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.querySelector("button").focus({ preventScroll: true });
}

function closeTileMenu(refocus = false) {
  const menu = $("tile-menu");
  if (menu.hidden) return;
  menu.hidden = true;
  const trigger = document.querySelector(`[data-profile-menu][aria-expanded="true"]`);
  if (trigger) {
    trigger.setAttribute("aria-expanded", "false");
    if (refocus) trigger.focus();
  }
  TILE_MENU_PROFILE = null;
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
