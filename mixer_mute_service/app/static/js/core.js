// Mixer Mute Service UI (core.js): Shared state, build id, per-device storage, fader values,
// requests to the service (api, act), toasts, HTML escaping.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

const $ = (id) => document.getElementById(id);
// LAST_STATE is what the page shows (may carry optimistic changes);
// CONFIRMED_STATE is the last /state the service actually returned.
let LAST_STATE = { channels: {}, enabled: true, mixer_connected: false };
let CONFIRMED_STATE = LAST_STATE;
let STATE_STALE = false;      // the last /state request failed
let SERVICE_ENABLED = null;   // sending enabled (from /health); null = unknown
// The release build writes its id into index.html (meta mixer-build).
const UI_BUILD_ID = (document.querySelector('meta[name="mixer-build"]') || {}).content || "dev";
let PERSONS_BOOTSTRAPPED = false;

function resolvedBuildId() {
  // The release build replaces the placeholder everywhere in index.html, so a
  // comparison with the placeholder text would always match: check the
  // release format (yyyyMMdd-HHmmss-<commit>) instead.
  return /^\d{8}-\d{6}-\w+$/.test(UI_BUILD_ID) ? UI_BUILD_ID : "dev";
}

// Which version runs: a release carries "yyyyMMdd-HHmmss-<commit>" (written
// by build-release.ps1); "dev" means the service runs straight from the
// source code (tests), not from an installed release.
function showBuildId() {
  const id = resolvedBuildId();
  const el = $("txt-build");
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}-(\w+)$/.exec(id);
  if (m) {
    el.textContent = `Stand ${m[3]}.${m[2]}. ${m[4]}:${m[5]}`;
    el.title = `Installiertes Release vom ${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}, Commit ${m[6]}`;
  } else if (id === "dev") {
    el.textContent = "Teststand";
    el.title = "Läuft direkt aus dem Quellcode, nicht aus einem installierten Release";
  } else {
    el.textContent = `Stand ${id}`;
    el.title = `Build ${id}`;
  }
}

// localStorage may be unavailable (private mode, storage full, blocked): the
// page then works without it. Everything per device goes through these.
function storeGet(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}
function storeSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) {}
}
function storeRemove(key) {
  try { localStorage.removeItem(key); } catch (_) {}
}

function applyBuildMigrations() {
  const key = "mixer_ui_build_id";
  const cur = resolvedBuildId();
  const prev = storeGet(key);
  if (prev === cur) return;
  // Channel names are local-only overrides; clear stale values after updates
  // so server-configured names are visible on every device by default.
  storeRemove("mixer_channel_names");
  storeSet(key, cur);
}

// ---- Channel custom names (localStorage) --------------------------------

function loadChannelNames() {
  try { return JSON.parse(storeGet("mixer_channel_names") || "{}"); } catch { return {}; }
}
function saveChannelNames(names) { storeSet("mixer_channel_names", JSON.stringify(names)); }

function getChannelDisplayName(ch, serverName) {
  const names = loadChannelNames();
  return names[String(ch)] || serverName || `Channel ${ch}`;
}

// ---- Fader values -------------------------------------------------------
// The mixer encodes a fader as a little-endian float32 position from 0 (-inf,
// bottom) to 1000 (top). The dB law below is inferred from captures (it matches
// a Behringer-X32-style curve: 750 ~ 0 dB, 1000 = +10 dB) and is only used for
// display -- what is sent is always the position.

const FADER_MAX = 1000;
// Labels below -30 dB crowd together on this law, so the scale jumps to -50.
const FADER_TICKS = [10, 5, 0, -5, -10, -20, -30, -50, -Infinity];

function isValidFaderHex(v) {
  return typeof v === "string" && /^[0-9a-f]{8}$/i.test(v);
}

function faderHexToValue(hex) {
  if (!isValidFaderHex(hex)) return null;
  const dv = new DataView(new ArrayBuffer(4));
  for (let i = 0; i < 4; i++) dv.setUint8(i, parseInt(hex.substr(i * 2, 2), 16));
  const v = dv.getFloat32(0, true);
  return Number.isFinite(v) && v >= 0 && v <= FADER_MAX ? v + 0 : null;
}

function faderValueToHex(value) {
  const dv = new DataView(new ArrayBuffer(4));
  dv.setFloat32(0, value, true);
  let out = "";
  for (let i = 0; i < 4; i++) out += dv.getUint8(i).toString(16).padStart(2, "0");
  return out;
}

function faderToDb(value) {
  const f = value / FADER_MAX;
  if (f <= 0) return -Infinity;
  if (f >= 0.5) return 40 * f - 30;
  if (f >= 0.25) return 80 * f - 50;
  if (f >= 0.0625) return 160 * f - 70;
  return 480 * f - 90;
}

function dbToFader(db) {
  let f;
  if (db <= -90) f = 0;
  else if (db >= -10) f = (db + 30) / 40;
  else if (db >= -30) f = (db + 50) / 80;
  else if (db >= -60) f = (db + 70) / 160;
  else f = (db + 90) / 480;
  return Math.max(0, Math.min(FADER_MAX, f * FADER_MAX));
}

function formatDb(value) {
  if (value === null || value === undefined) return "—";
  const db = faderToDb(value);
  if (db === -Infinity) return "−∞";
  const r = Math.round(db * 10) / 10;
  return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r).toFixed(1) + " dB";
}

// ---- Shared helpers: talking to the service, messages, escaping ----------

// The "⋮" of every menu key (desktop tiles, Programm rows, mute groups).
const ICON_DOTS = `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>`;

// Never throws: a network error comes back like a failed request
// (status 0), so every caller handles both the same way. data is always an
// object or an array. A request that gets no answer within ``timeout`` ms is
// given up (a hanging request would otherwise freeze whatever waits for it).
// Two GETs of the same path at the same time share one request.
const API_TIMEOUT_MS = 10000;

const API_GETS = new Map();

function api(method, path, body, { timeout = API_TIMEOUT_MS } = {}) {
  if (method !== "GET" || body !== undefined) return apiRequest(method, path, body, timeout);
  if (!API_GETS.has(path)) {
    API_GETS.set(path, apiRequest(method, path, body, timeout).finally(() => API_GETS.delete(path)));
  }
  // Each caller gets its own copy: some edit the data they receive.
  return API_GETS.get(path).then(r => ({ ...r, data: structuredCloneSafe(r.data) }));
}

function structuredCloneSafe(data) {
  try { return structuredClone(data); } catch (_) { return JSON.parse(JSON.stringify(data)); }
}

async function apiRequest(method, path, body, timeout) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const opt = { method, headers: {}, signal: ctrl.signal };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  try {
    let res;
    try {
      res = await fetch(path, opt);
    } catch (_) {
      const reason = ctrl.signal.aborted ? "service did not answer in time" : "service unreachable";
      return { ok: false, status: 0, data: { reason } };
    }
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (data === null || typeof data !== "object") data = {};
    return { ok: res.ok, status: res.status, data };
  } finally {
    clearTimeout(timer);
  }
}

// Report the outcome of a control/CRUD action, then refresh. Returns whether
// the service accepted it.
async function act(method, path, body, label) {
  const r = await api(method, path, body);
  const ok = r.ok && r.data.ok !== false;
  if (ok) {
    toast(`${label}: ok`, true);
  } else {
    const reason = r.data.reason || `HTTP ${r.status}`;
    const extra = r.data.age_seconds !== undefined ? ` (age ${r.data.age_seconds}s)` : "";
    let detail = "";
    if (Array.isArray(r.data.details)) {
      detail = " — " + r.data.details.map(d => `ch${d.channel}: ${d.reason}`).join(", ");
    }
    toast(`${label}: ${reason}${extra}${detail}`, false);
  }
  await refresh();
  return ok;
}

function toast(msg, ok = true) {
  const el = document.createElement("div");
  el.className = "toast " + (ok ? "ok" : "err");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function escapeHtml(s) { return s.replace(/[&<>"']/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m])); }

function escapeAttr(s) { return escapeHtml(s); }
