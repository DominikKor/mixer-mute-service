// Mixer Mute Service UI (song-screens.js): the screens the lyrics are shown on.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Song screens -------------------------------------------------------------
// The lyrics go to one or more screens at once (SONG_SETTINGS.targets: a PC
// and its monitor each, one per PC). Here: the picker in the Liedanzeige
// settings (a toggle per PC, then its monitor), the line in the Liedanzeige
// that says where the lyrics go and which screen failed (SONG_LIVE.screens,
// from the service), and the warning after a key press.

const MAX_SONG_SCREENS = 4;
const SONG_SCREEN_MONITORS = [1, 2, 3, 4];
const SONG_DEFAULT_MONITOR = 2;   // as the service (SongTarget.monitor)

// The service's English reasons, as the operator should read them.
const SONG_SCREEN_REASONS = {
  "display agent unavailable": "nicht erreichbar",
  "display agent rejected the request (update the display service?)": "Anzeige-Dienst lehnt ab – Update nötig?",
  "desktop display is not configured": "Anzeige-Dienst nicht eingerichtet",
  "display agent address must be a LAN address": "keine LAN-Adresse",
};

function songScreenName(screen) {
  return `${targetPcName(screen.ip) || screen.ip} · Monitor ${screen.monitor}`;
}

function songScreenReason(reason) {
  return SONG_SCREEN_REASONS[reason] || reason || "Fehler";
}

// "Streaming PC · Monitor 1: nicht erreichbar; …" of the screens that failed.
function songScreensFailedText(screens) {
  return (screens || []).filter(screen => !screen.ok)
    .map(screen => `${songScreenName(screen)}: ${songScreenReason(screen.reason)}`).join("; ");
}

// After a key press: the screens that did not take it (the others did).
// Said once per change: a PC that stays off is then marked in the
// Liedanzeige and the dock (⚠), not toasted at every verse.
let SONG_SCREENS_FAILED = "";
function noteSongScreensFailed(data) {
  const text = songScreensFailedText(data && data.failed);
  if (text && text !== SONG_SCREENS_FAILED) toast(`Lied nicht überall: ${text}`, false);
  SONG_SCREENS_FAILED = text;
}

// ---- Liedanzeige: where the lyrics go -----------------------------------------

function renderSongScreens() {
  const box = $("song-screens");
  // The last outcome per screen, else the configured screens.
  const reported = Array.isArray(SONG_LIVE.screens) && SONG_LIVE.screens.length ? SONG_LIVE.screens : null;
  const screens = reported || ((SONG_SETTINGS && SONG_SETTINGS.targets) || []).map(t => ({ ...t, ok: null }));
  const sig = JSON.stringify([screens, TARGET_PCS.map(pc => [pc.ip, pc.name])]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  const label = document.createElement("span");
  label.className = "song-screens-label";
  label.textContent = screens.length > 1 ? "Anzeige auf" : "Anzeige";
  box.replaceChildren(label, ...screens.map(screen => {
    const chip = document.createElement("span");
    chip.className = "song-screen" + (screen.ok === false ? " failed" : screen.ok ? " ok" : "");
    chip.textContent = songScreenName(screen) + (screen.ok === false ? ` – ${songScreenReason(screen.reason)}` : "");
    return chip;
  }));
  box.hidden = !screens.length;
}

// ---- Settings dialog: one toggle per PC, then that PC's monitor ------------------

let SONG_FORM_TARGETS = [];   // [{ip, monitor}] as chosen in the dialog

function songTargetPcs() {
  const pcs = TARGET_PCS.map(pc => ({ ip: pc.ip, name: pc.name }));
  for (const target of SONG_FORM_TARGETS) {
    if (!pcs.some(pc => pc.ip === target.ip)) pcs.push({ ip: target.ip, name: `Andere IP` });
  }
  return pcs;
}

function renderSongTargets() {
  const box = $("sg-targets");
  box.replaceChildren(...songTargetPcs().map(pc => {
    const target = SONG_FORM_TARGETS.find(t => t.ip === pc.ip);
    const row = document.createElement("div");
    row.className = "song-target" + (target ? " on" : "");
    row.dataset.songTarget = pc.ip;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "song-target-toggle";
    toggle.dataset.songTargetToggle = pc.ip;
    toggle.setAttribute("aria-pressed", String(!!target));
    toggle.title = target ? "Hier keine Liedtexte mehr zeigen" : "Liedtexte auch hier zeigen";
    toggle.innerHTML = `<span class="song-target-box" aria-hidden="true"></span><span class="song-target-name"></span><span class="song-target-ip"></span>`;
    toggle.querySelector(".song-target-name").textContent = pc.name;
    toggle.querySelector(".song-target-ip").textContent = pc.ip;
    const monitors = document.createElement("div");
    monitors.className = "monitor-picker song-target-monitors";
    monitors.setAttribute("role", "radiogroup");
    monitors.setAttribute("aria-label", `Monitor auf ${pc.name}`);
    monitors.append(...SONG_SCREEN_MONITORS.map(n => {
      const key = document.createElement("button");
      key.type = "button";
      key.className = "monitor-key";
      key.setAttribute("role", "radio");
      key.dataset.songTargetMonitor = String(n);
      key.setAttribute("aria-checked", String(!!target && target.monitor === n));
      key.textContent = String(n);
      key.title = target ? `Monitor ${n}` : `Liedtexte auf ${pc.name}, Monitor ${n} zeigen`;
      return key;
    }));
    row.append(toggle, monitors);
    return row;
  }));
}

// The PC's monitor chosen before (also when it was switched off meanwhile).
const SONG_TARGET_LAST_MONITOR = {};

function toggleSongTarget(ip, monitor = null) {
  const index = SONG_FORM_TARGETS.findIndex(t => t.ip === ip);
  if (index >= 0) {
    SONG_TARGET_LAST_MONITOR[ip] = SONG_FORM_TARGETS[index].monitor;
    SONG_FORM_TARGETS.splice(index, 1);
  } else if (SONG_FORM_TARGETS.length >= MAX_SONG_SCREENS) {
    songFormError(`Höchstens ${MAX_SONG_SCREENS} Bildschirme`);
    return;
  } else {
    SONG_FORM_TARGETS.push({ ip, monitor: monitor || SONG_TARGET_LAST_MONITOR[ip] || SONG_DEFAULT_MONITOR });
    // In the order of the PCs (the first screen is the old "monitor").
    const order = songTargetPcs().map(pc => pc.ip);
    SONG_FORM_TARGETS.sort((a, b) => order.indexOf(a.ip) - order.indexOf(b.ip));
  }
  songFormError(null);
  renderSongTargets();
}

function setSongTargets(targets) {
  SONG_FORM_TARGETS = (targets || []).map(t => ({ ip: t.ip, monitor: t.monitor }));
  renderSongTargets();
}

// The PCs' addresses changed (dialog "PCs…"): the chosen screens move along.
function moveSongTargets(moves) {
  if (!SONG_FORM_TARGETS.length) return;
  SONG_FORM_TARGETS = SONG_FORM_TARGETS.map(t => ({ ...t, ip: moves[t.ip] || t.ip }));
  renderSongTargets();
}

$("sg-targets").addEventListener("click", event => {
  const toggle = event.target.closest("[data-song-target-toggle]");
  if (toggle) { toggleSongTarget(toggle.dataset.songTargetToggle); return; }
  const key = event.target.closest("[data-song-target-monitor]");
  if (!key) return;
  const ip = key.closest("[data-song-target]").dataset.songTarget;
  const monitor = Number(key.dataset.songTargetMonitor);
  const target = SONG_FORM_TARGETS.find(t => t.ip === ip);
  // A monitor of a PC that is off switches it on with that monitor.
  if (!target) { toggleSongTarget(ip, monitor); return; }
  target.monitor = monitor;
  renderSongTargets();
});
