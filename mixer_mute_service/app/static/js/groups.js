// Mixer Mute Service UI (groups.js): Mute groups: list, presets on the service, sync, settings snapshot, group actions.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Mute groups ------------------------------------------------------------
// The service is the source of truth for which channels belong to a group
// (groups.json) and for their saved fader values and descriptions
// (group_presets.json). This page keeps a working copy in localStorage plus
// the open/closed state, which is per device. Group changes made here are
// pushed through one serialized queue, saved values right away; changes made
// on another device are pulled in every few seconds.

const GROUP_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$/;
const GROUP_NAME_HINT = "Name: Buchstaben, Ziffern, Leerzeichen, - und _ (max. 64), beginnt mit Buchstabe oder Ziffer";

function groupNameError(name, ignoreId = null) {
  if (!GROUP_NAME_RE.test(name)) return GROUP_NAME_HINT;
  const taken = loadPersons().some(p => p.id !== ignoreId && p.name.trim().toLowerCase() === name.toLowerCase());
  return taken ? `Eine Mute Gruppe „${name}“ gibt es schon` : null;
}

function loadPersons() {
  try { return JSON.parse(localStorage.getItem("mixer_persons") || "[]"); } catch { return []; }
}
function savePersons(list) {
  try { localStorage.setItem("mixer_persons", JSON.stringify(list)); } catch (_) {}
}

// Names of the groups the service had when this device last looked. A local
// group that is missing on the service was deleted on another device only if
// it was seen there before; otherwise it has simply not been sent yet.
function loadSeenGroupNames() {
  try { return new Set(JSON.parse(localStorage.getItem("mixer_groups_seen") || "[]")); } catch { return new Set(); }
}
function saveSeenGroupNames(names) {
  try { localStorage.setItem("mixer_groups_seen", JSON.stringify([...names])); } catch (_) {}
}

// Make the local list match the service's groups, keeping the local extras.
function mergeBackendGroups(backend) {
  const seen = loadSeenGroupNames();
  const local = loadPersons();
  const byName = new Map(local.map(p => [p.name, p]));
  // Renamed on another device: a group known here vanished and a new one with
  // the same channels appeared. It keeps its id and saved fader values.
  const chKey = chs => JSON.stringify([...(chs || [])].map(Number).sort((a, b) => a - b));
  const vanished = local.filter(p => !name_in(backend, p.name) && seen.has(p.name));
  const renamedFrom = new Map();
  for (const [name, chs] of Object.entries(backend)) {
    if (byName.has(name)) continue;
    const i = vanished.findIndex(p => chKey(personChannelList(p)) === chKey(chs));
    if (i >= 0) renamedFrom.set(name, vanished.splice(i, 1)[0]);
  }
  const merged = [];
  for (const [name, chs] of Object.entries(backend)) {
    const prev = byName.get(name) || renamedFrom.get(name);
    const channels = {};
    for (const ch of chs || []) {
      channels[String(ch)] = (prev && prev.channels && prev.channels[String(ch)]) || { fader_hex: null, set_at: null };
    }
    merged.push({ ...(prev || { id: genId() }), name, channels });
  }
  const dropped = [];
  const renamedIds = new Set([...renamedFrom.values()].map(p => p.id));
  for (const p of local) {
    if (name_in(backend, p.name) || renamedIds.has(p.id)) continue;
    if (personChannelList(p).length && seen.has(p.name)) dropped.push(p.name);
    else merged.push(p);  // no channels yet, or not sent yet
  }
  saveSeenGroupNames(Object.keys(backend));
  // Keep the local order; groups new to this device go to the end.
  const order = new Map(local.map((p, i) => [p.id, i]));
  merged.sort((a, b) => (order.has(a.id) ? order.get(a.id) : 1e9) - (order.has(b.id) ? order.get(b.id) : 1e9));
  const changed = JSON.stringify(merged) !== JSON.stringify(local);
  if (changed) savePersons(merged);
  if (dropped.length) toast(`Auf einem anderen Gerät gelöscht: ${dropped.join(", ")}`, false);
  return changed;
}
function name_in(obj, name) { return Object.prototype.hasOwnProperty.call(obj, name); }

let GROUPS_SIGNATURE = "";
// The service's groups changed: update the group lists of Programm and Steuerung.
function applyBackendGroups(backend) {
  const sig = JSON.stringify(backend);
  if (sig === GROUPS_SIGNATURE) return;
  GROUPS_SIGNATURE = sig;
  PROGRAM_GROUPS = backend;
  COMPANION_GROUPS = backend;
  renderProgramItems();
}

// ---- Saved fader values and descriptions on the service -----------------------
// Safari deletes the localStorage of a site unused for 7 days; the service
// runs weekly. So every saved value goes to the service at once, and the
// service's values win when the page pulls.
let PRESET_PUTS = 0;

async function putPreset(groupName, ch, fields) {
  PRESET_PUTS++;
  try {
    const r = await api("PUT", `/group-presets/${encodeURIComponent(groupName)}/${ch}`, fields);
    if (!r.ok || r.data.ok === false) {
      toast(`${groupName}: Kanal ${ch} nicht auf dem Dienst gespeichert (${r.data.reason || "HTTP " + r.status})`, false);
      return false;
    }
    return true;
  } finally {
    PRESET_PUTS--;
  }
}

// Take over the service's values. Values only this device has (saved before
// they moved to the service, or a duplicated group) are sent once. Returns
// whether the local list changed.
async function syncPresetsFromBackend() {
  if (PRESET_PUTS) return false;
  const r = await api("GET", "/group-presets");
  if (!r.ok || !r.data || typeof r.data !== "object" || Array.isArray(r.data) || PRESET_PUTS) return false;
  const server = r.data;
  const backend = PROGRAM_GROUPS || {};
  const list = loadPersons();
  const uploads = [];
  let changed = false;
  for (const p of list) {
    const remote = server[p.name] || {};
    const members = new Set((backend[p.name] || []).map(String));
    for (const [ch, entry] of Object.entries(p.channels || {})) {
      const saved = remote[ch];
      if (saved) {
        const merged = { ...entry, fader_hex: saved.fader_hex, set_at: saved.set_at, description: saved.description || "" };
        if (JSON.stringify(merged) !== JSON.stringify(entry)) { p.channels[ch] = merged; changed = true; }
      } else if (members.has(ch) && (isValidFaderHex(entry.fader_hex) || entry.description)) {
        const fields = {};
        if (isValidFaderHex(entry.fader_hex)) fields.fader_hex = entry.fader_hex;
        if (entry.description) fields.description = entry.description;
        uploads.push([p.name, ch, fields]);
      }
    }
  }
  if (changed) savePersons(list);
  for (const [name, ch, fields] of uploads) {
    const u = await api("PUT", `/group-presets/${encodeURIComponent(name)}/${ch}`, fields);
    if (!u.ok) console.warn(`preset upload ${name}/${ch}:`, u.data.reason || u.status);
  }
  return changed;
}

async function bootstrapPersonsFromBackend() {
  if (PERSONS_BOOTSTRAPPED) return;
  const r = await api("GET", "/groups");
  if (!r.ok || !r.data || typeof r.data !== "object") return;
  PERSONS_BOOTSTRAPPED = true;
  // Local groups the service never had (from the times when groups lived only
  // in this browser, or a change that did not get through) are sent once now.
  // A group the service had and lost was deleted on another device: it is not
  // brought back from here.
  let backend = r.data;
  const seen = loadSeenGroupNames();
  let created = false;
  for (const p of loadPersons()) {
    const chs = personChannelList(p);
    if (!chs.length || name_in(backend, p.name) || seen.has(p.name) || !GROUP_NAME_RE.test(p.name)) continue;
    const c = await api("POST", "/groups", { name: p.name, channels: chs });
    if (c.ok && c.data.ok !== false) created = true;
  }
  if (created) {
    const again = await api("GET", "/groups");
    if (again.ok && again.data && typeof again.data === "object") backend = again.data;
  }
  mergeBackendGroups(backend);
  applyBackendGroups(backend);
  await syncPresetsFromBackend();
  normalizeCollapsedGroups();
  renderPersons();
}

// Pull changes made on other devices (skipped while local changes are queued).
async function pullGroupsFromBackend() {
  if (!PERSONS_BOOTSTRAPPED || GROUP_SYNC_PENDING) return;
  const r = await api("GET", "/groups");
  if (!r.ok || !r.data || typeof r.data !== "object" || GROUP_SYNC_PENDING) return;
  const groupsChanged = mergeBackendGroups(r.data);
  applyBackendGroups(r.data);
  const presetsChanged = await syncPresetsFromBackend();
  if (groupsChanged || presetsChanged) renderPersons();
}

function loadCollapsedGroups() {
  try { return JSON.parse(localStorage.getItem("mixer_person_collapsed") || "{}"); } catch { return {}; }
}
function saveCollapsedGroups(state) {
  try { localStorage.setItem("mixer_person_collapsed", JSON.stringify(state || {})); } catch (_) {}
}
function isGroupCollapsed(personId) {
  const state = loadCollapsedGroups();
  return state[String(personId)] !== false;
}
function toggleGroupCollapsed(personId) {
  const state = loadCollapsedGroups();
  state[String(personId)] = !isGroupCollapsed(personId);
  saveCollapsedGroups(state);
  renderPersons(true);
}

function normalizeCollapsedGroups() {
  const list = loadPersons();
  if (!list.length) return;
  const state = loadCollapsedGroups();
  let changed = false;
  for (const p of list) {
    if (typeof p.name === "string" && p.name.trim().toLowerCase() === "all") {
      const key = String(p.id);
      if (state[key] !== true) {
        state[key] = true;
        changed = true;
      }
    }
  }
  if (changed) saveCollapsedGroups(state);
}
function genId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }

// ---- Pushing group changes to the service ------------------------------------
// Every change only edits localStorage and then queues a sync. The syncs run
// one after another and always send the current local list, so quick edits
// (add, add, remove) cannot overtake each other. A failing group is reported
// and skipped; the others are still sent.

let GROUP_SYNC = Promise.resolve();
let GROUP_SYNC_PENDING = 0;
const GROUP_SYNC_REMOVED = new Set();   // names deleted locally

// Run a step in the sync queue (pulls from the service wait meanwhile).
function queueGroupSync(step) {
  GROUP_SYNC_PENDING++;
  GROUP_SYNC = GROUP_SYNC.then(step).catch(() => {}).finally(() => { GROUP_SYNC_PENDING--; });
  return GROUP_SYNC;
}

function syncPersonGroupsToBackend({ removed = null } = {}) {
  if (removed) GROUP_SYNC_REMOVED.add(removed);
  return queueGroupSync(runGroupSync);
}

async function runGroupSync() {
  const current = await api("GET", "/groups");
  if (!current.ok || !current.data || typeof current.data !== "object") {
    toast("Mute Gruppen konnten nicht mit dem Dienst synchronisiert werden", false);
    return;
  }
  const known = new Set(Object.keys(current.data));
  const synced = new Set();   // names whose channels now match on the service
  const persons = loadPersons();
  const localNames = new Set(persons.map(p => p.name.trim()));
  const fail = (name, r) => toast(`${name}: Synchronisierung fehlgeschlagen (${(r.data && r.data.reason) || "HTTP " + r.status})`, false);

  for (const person of persons) {
    const name = person.name.trim();
    if (!GROUP_NAME_RE.test(name)) { toast(`${name}: ${GROUP_NAME_HINT}`, false); continue; }
    const channels = personChannelList(person);
    const encodedName = encodeURIComponent(name);
    let result = null;
    if (!channels.length) {
      if (!known.has(name)) continue;
      result = await api("POST", `/groups/${encodedName}/delete`);
      if (result.ok) known.delete(name);
    } else if (known.has(name)) {
      const same = JSON.stringify([...current.data[name]].sort((a, b) => a - b)) === JSON.stringify(channels);
      if (same) { synced.add(name); continue; }
      result = await api("POST", `/groups/${encodedName}/update`, { channels });
    } else {
      result = await api("POST", "/groups", { name, channels });
      if (result.ok && result.data.ok !== false) known.add(name);
    }
    if (result && (!result.ok || result.data.ok === false)) fail(name, result);
    else if (channels.length) synced.add(name);
  }

  for (const name of [...GROUP_SYNC_REMOVED]) {
    GROUP_SYNC_REMOVED.delete(name);
    if (localNames.has(name) || !known.has(name)) continue;
    const r = await api("POST", `/groups/${encodeURIComponent(name)}/delete`);
    if (!r.ok || r.data.ok === false) fail(name, r);
  }
  const after = await api("GET", "/groups");
  if (after.ok && after.data && typeof after.data === "object") applyBackendGroups(after.data);
  await syncPresetsFromBackend();   // values of new (e.g. duplicated) groups
}

// The service renames the group together with its Programm rows, Steuerung
// keys and Overall macros. A group that only exists here (no channels yet) is
// just renamed locally.
function renamePerson(personId, oldName, newName) {
  const err = groupNameError(newName, personId);
  if (err) { toast(err, false); return; }
  return queueGroupSync(async () => {
    const r = await api("POST", `/groups/${encodeURIComponent(oldName)}/rename`, { name: newName });
    if (!r.ok && r.status !== 404) {
      toast(`Umbenennen fehlgeschlagen: ${(r.data && r.data.reason) || "HTTP " + r.status}`, false);
      return;
    }
    const list = loadPersons();
    const p = list.find(x => x.id === personId);
    if (p) { p.name = newName; savePersons(list); }
    if (r.ok) {
      const seen = loadSeenGroupNames();
      seen.delete(oldName);
      seen.add(newName);
      saveSeenGroupNames(seen);
      const moved = (r.data.program_items || 0) + (r.data.buttons || 0);
      toast(moved ? `„${oldName}“ heißt jetzt „${newName}“ (${moved} Verweise angepasst)` : `„${oldName}“ heißt jetzt „${newName}“`, true);
      const groups = await api("GET", "/groups");
      if (groups.ok) applyBackendGroups(groups.data);
      await Promise.all([loadProgramItems(), loadCompanionButtons()]);
    }
    renderPersons(true);
  });
}

// ---- Channel settings snapshot of a mute group -------------------------------
// Read-only: the service reads gain/HPF/EQ/gate/compressor from the mixer's
// state dump and saves them per group; nothing is sent to the mixer.
let SETTINGS_GROUP = null;

function fmtSigned(v, unit) { return `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(1)} ${unit}`; }
function fmtHz(v) { return v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 1 : 0)} kHz` : `${Math.round(v)} Hz`; }
function fmtMs(v) { return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`; }
function onOff(on) { return `<span class="tag">${on ? "AN" : "AUS"}</span>`; }

function settingsRowHtml(channel, s) {
  const eqLines = s.eq.bands.map(b =>
    `<div class="line">${fmtHz(b.freq_hz)} ${fmtSigned(b.gain_db, "dB")} · Q ${b.q.toFixed(2)}</div>`).join("");
  const g = s.gate, c = s.compressor;
  return `<tr>
    <td class="ch">${escapeHtml(s.name || "Kanal " + channel)}<div class="muted-text">CH ${escapeHtml(String(channel))}</div></td>
    <td>${fmtSigned(s.gain_db, "dB")}</td>
    <td class="${s.hpf.on ? "" : "off"}">${onOff(s.hpf.on)}${fmtHz(s.hpf.freq_hz)}</td>
    <td class="${s.eq.on ? "" : "off"}">${onOff(s.eq.on)}${eqLines}</td>
    <td class="${g.on ? "" : "off"}">${onOff(g.on)}<div class="line">Schwelle ${fmtSigned(g.threshold_db, "dB")}</div>
      <div class="line">Range ${fmtSigned(g.range_db, "dB")}</div>
      <div class="line">A ${fmtMs(g.attack_ms)} · H ${fmtMs(g.hold_ms)} · R ${fmtMs(g.release_ms)}</div></td>
    <td class="${c.on ? "" : "off"}">${onOff(c.on)}<div class="line">Schwelle ${fmtSigned(c.threshold_db, "dB")} · ${c.ratio.toFixed(1)}:1</div>
      <div class="line">A ${fmtMs(c.attack_ms)} · R ${fmtMs(c.release_ms)}</div>
      <div class="line">Make-up ${fmtSigned(c.makeup_db, "dB")}</div></td>
  </tr>`;
}

function renderSettingsSnapshot(data) {
  const table = $("settings-table");
  if (!data) {
    $("settings-lcd").textContent = "Noch nicht gespeichert";
    table.innerHTML = `<div class="settings-empty">Für diese Gruppe sind noch keine Einstellungen gespeichert. „Jetzt speichern“ übernimmt den aktuellen Stand vom Pult.</div>`;
    $("settings-save").textContent = "Jetzt speichern";
    return;
  }
  const when = data.captured_at ? new Date(data.captured_at) : null;
  const skipped = (data.skipped || []).length ? ` · Busse nicht enthalten: ${data.skipped.join(", ")}` : "";
  $("settings-lcd").textContent = when
    ? `Gespeichert am ${when.toLocaleDateString("de-DE")} um ${when.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}${skipped}`
    : "Gespeichert";
  const channels = Object.keys(data.channels).sort((a, b) => Number(a) - Number(b));
  table.innerHTML = `<table><thead><tr><th>Kanal</th><th>Gain</th><th>Hochpass</th><th>EQ</th><th>Gate</th><th>Kompressor</th></tr></thead>
    <tbody>${channels.map(ch => settingsRowHtml(ch, data.channels[ch])).join("")}</tbody></table>`;
  $("settings-save").textContent = "Neu speichern";
}

async function openSettingsDialog(personId) {
  const person = loadPersons().find(p => p.id === personId);
  if (!person) return;
  SETTINGS_GROUP = person.name;
  $("settings-title").textContent = `Kanal-Einstellungen · ${person.name}`;
  $("settings-lcd").textContent = "Wird geladen …";
  $("settings-table").innerHTML = "";
  $("settings-dialog").showModal();
  const r = await api("GET", `/groups/${encodeURIComponent(person.name)}/settings-snapshot`);
  if (SETTINGS_GROUP !== person.name) return;
  if (r.ok && r.data.ok !== false) renderSettingsSnapshot(r.data);
  else if (r.status === 404) renderSettingsSnapshot(null);
  else $("settings-lcd").textContent = (r.data && r.data.reason) || "Dienst nicht erreichbar";
}

async function saveSettingsSnapshot() {
  const group = SETTINGS_GROUP;
  const button = $("settings-save");
  if (!group || button.disabled) return;
  if (button.textContent === "Neu speichern"
      && !(await confirmDelete(button, "Gespeicherte Einstellungen durch den aktuellen Stand ersetzen?", "Ersetzen"))) return;
  button.disabled = true;
  try {
    const r = await api("POST", `/groups/${encodeURIComponent(group)}/settings-snapshot`);
    if (SETTINGS_GROUP !== group) return;
    if (!r.ok || r.data.ok === false) {
      const details = (r.data && r.data.details || []).map(d => `Kanal ${d.channel}: ${d.reason}`).join(", ");
      toast(`Nicht gespeichert: ${(r.data && r.data.reason) || "HTTP " + r.status}${details ? " (" + details + ")" : ""}`, false);
      return;
    }
    renderSettingsSnapshot(r.data);
    toast(`Einstellungen von „${group}“ gespeichert`, true);
  } finally {
    button.disabled = false;
  }
}

$("settings-close").addEventListener("click", () => $("settings-dialog").close());
$("settings-save").addEventListener("click", saveSettingsSnapshot);
$("settings-dialog").addEventListener("close", () => { SETTINGS_GROUP = null; });

function openPersonCreateRow() {
  const row = $("person-create-row");
  const input = $("new-person-name");
  row.classList.add("visible");
  input.value = "";
  input.focus();
}

function closePersonCreateRow() {
  const row = $("person-create-row");
  const input = $("new-person-name");
  row.classList.remove("visible");
  input.value = "";
}

function savePerson() {
  const name = $("new-person-name").value.trim();
  const err = name ? groupNameError(name) : "Bitte einen Namen eingeben";
  if (err) { toast(err, false); $("new-person-name").focus(); return; }
  const list = loadPersons();
  list.push({ id: genId(), name, channels: {} });
  savePersons(list);
  syncPersonGroupsToBackend();
  closePersonCreateRow();
  renderPersons(true);
}

function deletePerson(id) {
  const previous = loadPersons();
  const removed = previous.find(p => p.id === id);
  savePersons(previous.filter(p => p.id !== id));
  if (removed) syncPersonGroupsToBackend({ removed: removed.name });
  renderPersons(true);
}

function duplicatePerson(id) {
  const list = loadPersons();
  const src = list.find(p => p.id === id);
  if (!src) return;
  let name = `${src.name} copy`;
  const names = list.map(p => p.name);
  for (let n = 2; names.includes(name); n++) name = `${src.name} copy ${n}`;
  if (!GROUP_NAME_RE.test(name)) { toast(GROUP_NAME_HINT, false); return; }
  list.push({ id: genId(), name, channels: JSON.parse(JSON.stringify(src.channels || {})) });
  savePersons(list);
  syncPersonGroupsToBackend();
  renderPersons(true);
  toast(`Mute Gruppe dupliziert als "${name}"`, true);
}

// "Set" stores the channel's current fader position -- only a fresh, safe
// value the service has actually observed.
function setPersonChannelFader(personId, ch) {
  const c = LAST_STATE.channels && LAST_STATE.channels[ch];
  const reason = channelUnsafeReason(LAST_STATE, ch) || (c && !isValidFaderHex(c.fader_hex) ? "Faderwert unbekannt" : null);
  if (reason) { toast(`Kanal ${ch}: nicht gespeichert – ${reason}`, false); return; }
  const list = loadPersons();
  const p = list.find(p => p.id === personId);
  if (!p) return;
  if (!p.channels) p.channels = {};
  const key = String(ch);
  p.channels[key] = { ...(p.channels[key] || {}), fader_hex: c.fader_hex, set_at: Date.now() };
  savePersons(list);
  renderPersons(true);
  putPreset(p.name, ch, { fader_hex: c.fader_hex }).then(ok => {
    if (ok) toast(`Kanal ${ch}: ${formatDb(c.fader_value)} gespeichert`, true);
  });
}

// ---- Group actions: all or nothing ---------------------------------------
// A group action checks every channel it would touch against a fresh /state
// and sends nothing if any of them is unsafe. If the service still blocks a
// command mid-way (the state changed between check and send), the rest is not
// sent; channels already sent stay as they are -- no automatic recovery writes.

function channelUnsafeReason(state, ch) {
  if (state === LAST_STATE && STATE_STALE) return "Dienst nicht erreichbar";
  if (!state.mixer_connected) return "keine Mixer-Verbindung";
  if (!state.enabled) return "Panic aktiv";
  const c = state.channels && state.channels[ch];
  if (!c) return "Kanal unbekannt";
  if (c.fader_value === null || c.fader_value === undefined) return "Faderwert unbekannt";
  if (c.muted === null || c.muted === undefined) return "Mute-Status unbekannt";
  if (!c.safe) return "Kanal unsicher";
  return null;
}

// buildSteps(state) returns the commands as [{ ch, path, body }], in send order.
async function runGroupAction(label, buildSteps) {
  const st = await api("GET", "/state");
  if (!st.ok || !st.data || !st.data.channels) { toast(`${label}: Status nicht abrufbar, nichts gesendet`, false); return; }
  const steps = buildSteps(st.data);
  if (!steps.length) { toast(`${label}: nichts zu tun`, true); await refresh(); return; }
  const blocked = steps
    .map(s => ({ ch: s.ch, reason: channelUnsafeReason(st.data, s.ch) }))
    .filter(b => b.reason);
  if (blocked.length) {
    toast(`${label} blockiert, nichts gesendet — ${blocked.map(b => `ch${b.ch}: ${b.reason}`).join(", ")}`, false);
    await refresh();
    return;
  }
  let sent = 0;
  for (const s of steps) {
    const r = await api("POST", s.path, s.body);
    if (!(r.ok && r.data.ok !== false)) {
      toast(`${label} abgebrochen bei ch${s.ch}: ${r.data.reason || "HTTP " + r.status} (${sent} von ${steps.length} gesendet)`, false);
      await refresh();
      return;
    }
    sent++;
  }
  toast(`${label}: ${sent} ok`, true);
  await refresh();
}

function personChannelList(p) {
  return Object.keys(p.channels || {}).map(Number).sort((a, b) => a - b);
}

async function useAllPersonFaders(personId) {
  const p = loadPersons().find(p => p.id === personId);
  if (!p) return;
  const chs = personChannelList(p).filter(ch => p.channels[String(ch)].fader_hex);
  if (!chs.length) { toast("Keine gespeicherten Faderwerte", false); return; }
  await runGroupAction(`${p.name} Use All`, () => chs.map(ch => ({
    ch, path: `/channels/${ch}/recall-fader`, body: { fader_hex: p.channels[String(ch)].fader_hex },
  })));
}

async function mutePersonChannels(personId) {
  const p = loadPersons().find(p => p.id === personId);
  if (!p) return;
  const chs = personChannelList(p);
  if (!chs.length) { toast("Keine Kanäle zugewiesen", false); return; }
  // Channels already observed muted need no command; the main L/R output is
  // never switched by a group.
  await runGroupAction(`${p.name} Mute`, (state) => chs
    .filter(ch => !(state.channels[ch] && (state.channels[ch].muted === true || state.channels[ch].always_on
      || state.channels[ch].main_output)))
    .map(ch => ({ ch, path: `/channels/${ch}/mute` })));
}

async function soloPersonChannels(personId) {
  const p = loadPersons().find(p => p.id === personId);
  if (!p) return;
  const groupChs = personChannelList(p);
  if (!groupChs.length) { toast("Keine Kanäle zugewiesen", false); return; }
  // Mute the others first: if the action is cut short, more stays muted
  // rather than less. The main L/R output is never switched by a group
  // (muting it would silence the whole room).
  await runGroupAction(`${p.name} Solo`, (state) => {
    const chans = state.channels || {};
    const inGroup = new Set(groupChs);
    const others = Object.keys(chans).map(Number).sort((a, b) => a - b).filter(ch => !inGroup.has(ch));
    return [
      ...others.filter(ch => chans[ch].muted !== true && !chans[ch].always_on && !chans[ch].main_output)
        .map(ch => ({ ch, path: `/channels/${ch}/mute` })),
      ...groupChs.filter(ch => !(chans[ch] && (chans[ch].muted === false || chans[ch].main_output)))
        .map(ch => ({ ch, path: `/channels/${ch}/unmute` })),
    ];
  });
}

async function usePersonChannelFader(personId, ch) {
  const list = loadPersons();
  const p = list.find(p => p.id === personId);
  if (!p || !p.channels || !p.channels[String(ch)] || !p.channels[String(ch)].fader_hex) {
    toast(`Kanal ${ch}: kein gespeicherter Faderwert`, false); return;
  }
  const fader_hex = p.channels[String(ch)].fader_hex;
  const saved = faderHexToValue(fader_hex);
  const c = LAST_STATE.channels && LAST_STATE.channels[ch];
  const label = getChannelDisplayName(ch, c && c.name);
  // The small fader next to "Use" already moves the channel when it saves, so
  // a "Use" right after it changes nothing -- say so instead of a bare "ok".
  const already = c && saved !== null && c.fader_value !== null && Math.abs(c.fader_value - saved) < 0.05;
  const r = await api("POST", `/channels/${ch}/recall-fader`, { fader_hex });
  if (r.ok && r.data.ok !== false) {
    toast(already
      ? `${label}: steht bereits auf dem gespeicherten Wert ${formatDb(saved)}`
      : `${label}: auf gespeicherten Wert ${formatDb(saved)} gesetzt`, true);
  } else {
    toast(`${label}: ${r.data.reason || "HTTP " + r.status}`, false);
  }
  await refresh();
}

async function setPersonChannelFaderValue(personId, ch, value) {
  const fader_hex = faderValueToHex(value);
  const list = loadPersons();
  const p = list.find(x => x.id === personId);
  if (!p) return;
  if (!p.channels) p.channels = {};
  const key = String(ch);
  p.channels[key] = { ...(p.channels[key] || {}), fader_hex, set_at: Date.now() };
  savePersons(list);
  renderPersons(true);
  putPreset(p.name, ch, { fader_hex });

  const r = await api("POST", `/channels/${ch}/fader`, { value });
  if (r.ok && r.data.ok !== false) {
    toast(`${p.name}: ch ${ch} Fader ${formatDb(value)} gesetzt`, true);
  } else {
    toast(`${p.name}: ch ${ch} ${r.data.reason || "HTTP " + r.status}`, false);
  }
  await refresh();
}

function removePersonChannel(personId, ch) {
  const list = loadPersons();
  const p = list.find(p => p.id === personId);
  if (p && p.channels) delete p.channels[String(ch)];
  savePersons(list);
  syncPersonGroupsToBackend();
  renderPersons(true);
}

function addPersonChannel(personId, ch) {
  if (!ch) return;
  const list = loadPersons();
  const p = list.find(p => p.id === personId);
  if (!p) return;
  if (!p.channels) p.channels = {};
  if (!p.channels[String(ch)]) p.channels[String(ch)] = { fader_hex: null, set_at: null };
  savePersons(list);
  syncPersonGroupsToBackend();
  renderPersons(true);
}

const ICON_SLIDERS = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M6 4v6M6 14v6M12 4v2M12 10v10M18 4v10M18 18v2"/><circle cx="6" cy="12" r="2"/><circle cx="12" cy="8" r="2"/><circle cx="18" cy="16" r="2"/></svg>`;
const ICON_COPY = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>`;
const ICON_CHECK = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>`;
const ICON_X = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>`;
const ICON_TRASH = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>`;

// While a group's small fader is held, the cards must not be redrawn: that
// would drop the slider out from under the finger (iPad) or the mouse.
let PERSON_SLIDER_ACTIVE = false;

// force: the user changed something here, redraw even if a field has focus
// (the field being edited is not part of that change).
function renderPersons(force = false) {
  const focused = document.activeElement;
  if (PERSON_SLIDER_ACTIVE) return;
  const typing = focused && (focused.tagName === "SELECT" || (focused.tagName === "INPUT" && focused.type !== "range"));
  if (!force && typing && ($("persons").contains(focused) || $("person-create-row").contains(focused))) return;
  setPersonsHtml($("persons"), buildPersonsHtml());
}

function buildPersonsHtml() {
  const list = loadPersons();
  if (!list.length) return `<div class="muted-text">Noch keine Mute Gruppen. Oben erstellen.</div>`;
  let html = "";
  const chans = LAST_STATE.channels || {};
  const allChNums = Object.keys(chans).map(Number).sort((a, b) => a - b);

  for (const person of list) {
    const collapsed = isGroupCollapsed(person.id);
    const assignedChs = Object.keys(person.channels || {}).map(Number).sort((a, b) => a - b);
    // The LED lights when every channel of the group is observed muted.
    // "Immer an" channels are left out, as the service does for group toggle.
    const ledChs = assignedChs.filter(ch => !(chans[ch] && (chans[ch].always_on || chans[ch].main_output)));
    const allMuted = ledChs.length > 0 && ledChs.every(ch => chans[ch] && chans[ch].muted === true);
    const availableChs = allChNums.filter(ch => !assignedChs.includes(ch));

    const rows = assignedChs.map(ch => {
      const entry = person.channels[String(ch)] || {};
      const chInfo = chans[ch];
      const chName = escapeHtml(getChannelDisplayName(ch, chInfo && chInfo.name));
      const savedValue = faderHexToValue(entry.fader_hex);
      const faderSaved = entry.fader_hex
        ? `<code class="fader-saved" title="${escapeAttr(entry.fader_hex)}">${savedValue === null ? "ungültig" : formatDb(savedValue)}</code>`
        : `<span class="fader-saved none">—</span>`;
      const liveValue = chInfo && chInfo.fader_value !== undefined ? chInfo.fader_value : null;
      // Start at the saved value, else the live one; with neither the slider is
      // locked instead of starting at the bottom (-inf).
      const baseValue = savedValue !== null ? savedValue : liveValue;
      const setAt = entry.set_at ? new Date(entry.set_at).toLocaleTimeString() : "nie";
      const description = escapeAttr(entry.description || "");
      // Two lines per channel so three groups fit side by side: name, saved
      // value and keys on top, the fader below.
      return `<div class="pc-row">
        <div class="pc-name">${chName} <span class="ch-tag">CH ${ch}</span></div>
        <div class="pc-saved">${faderSaved} <span class="set-at" title="Gespeichert um">${setAt}</span></div>
        <div class="pc-keys">
          <button data-action="person-set-fader" data-pid="${escapeAttr(person.id)}" data-ch="${ch}">Set</button>
          <button data-action="person-use-fader" data-pid="${escapeAttr(person.id)}" data-ch="${ch}" class="primary" ${entry.fader_hex ? "" : "disabled"}>Use</button>
          <button data-action="person-remove-ch" data-pid="${escapeAttr(person.id)}" data-ch="${ch}" class="danger" title="Kanal entfernen" aria-label="Kanal entfernen">&#x2715;</button>
        </div>
        <label class="pc-description">
          <input type="text" maxlength="120" data-action="person-channel-description" data-pid="${escapeAttr(person.id)}" data-ch="${ch}" value="${description}" placeholder="Beschreibung für diesen Channel" aria-label="Beschreibung ${chName}" />
        </label>
        <div class="person-fader-wrap">
          <input
            type="range"
            min="0"
            max="${FADER_MAX}"
            step="1"
            class="fader-slider"
            data-action="person-fader-input"
            data-pid="${escapeAttr(person.id)}"
            data-ch="${ch}"
            value="${baseValue === null ? 0 : baseValue.toFixed(0)}"
            ${baseValue === null ? 'disabled title="Faderwert unbekannt"' : ""}
            aria-label="Fader ${chName}"
          />
          <span class="fader-label">${formatDb(baseValue)}</span>
        </div>
      </div>`;
    }).join("");

    const addSelect = availableChs.length
      ? `<div class="add-ch-row">
          <select id="add-ch-${escapeAttr(person.id)}" aria-label="Kanal hinzufügen">
            <option value="">Kanal hinzufügen …</option>
            ${availableChs.map(ch => {
              return `<option value="${ch}">${escapeHtml(getChannelDisplayName(ch, chans[ch] && chans[ch].name))} (ch ${ch})</option>`;
            }).join("")}
          </select>
          <button data-action="person-add-ch" data-pid="${escapeAttr(person.id)}">Hinzufügen</button>
        </div>`
      : "";

    html += `<div class="person-card" data-card="${escapeAttr(person.id)}">
      <div class="person-head">
        <div class="person-name-wrap">
          <button class="collapse-btn" data-action="person-toggle-collapse" data-pid="${escapeAttr(person.id)}" title="${collapsed ? "Ausklappen" : "Einklappen"}" aria-expanded="${!collapsed}"><span class="collapse-chip" aria-hidden="true">${collapsed ? "▸" : "▾"}</span></button>
          <span class="led${allMuted ? " mute" : ""}" title="${allMuted ? "alle Kanäle gemutet" : "nicht alle Kanäle gemutet"}"></span>
          <span class="person-name" data-rename-person="${escapeAttr(person.id)}">${escapeHtml(person.name)}</span>
          <button class="rename-btn" data-action="person-rename" data-pid="${escapeAttr(person.id)}" data-name="${escapeAttr(person.name)}" title="Umbenennen" aria-label="${escapeAttr(person.name)} umbenennen">&#9998;</button>
        </div>
        <div class="person-tools">
          <button class="icon-btn" data-action="person-settings" data-pid="${escapeAttr(person.id)}" title="Kanal-Einstellungen (Gain, HPF, EQ, Gate, Kompressor)" aria-label="${escapeAttr(person.name)}: Kanal-Einstellungen">${ICON_SLIDERS}</button>
          <button class="icon-btn" data-action="person-duplicate" data-pid="${escapeAttr(person.id)}" title="Duplizieren" aria-label="${escapeAttr(person.name)} duplizieren">${ICON_COPY}</button>
          <button class="icon-btn danger" data-action="person-delete" data-pid="${escapeAttr(person.id)}" title="Löschen" aria-label="${escapeAttr(person.name)} löschen">${ICON_TRASH}</button>
        </div>
      </div>
      <div class="person-actions">
        <button data-action="person-mute" data-pid="${escapeAttr(person.id)}" class="key-mute">Mute</button>
        <button data-action="person-solo" data-pid="${escapeAttr(person.id)}" class="key-solo">Solo</button>
        <button data-action="person-use-all" data-pid="${escapeAttr(person.id)}" ${Object.values(person.channels || {}).some(e => e.fader_hex) ? "" : "disabled"}>Use All</button>
      </div>
      <div class="person-body ${collapsed ? "collapsed" : ""}">
      ${assignedChs.length ? `<div class="person-channels">${rows}</div>`
        : `<div class="muted-text" style="font-size:12px;margin-bottom:8px;">Keine Kanäle zugewiesen.</div>`}
      ${addSelect}
      </div>
    </div>`;
  }
  return html;
}

// renderPersons runs with every 1 s refresh. Replacing the cards each time
// swapped every button under the user's finger, so a tap that spanned a
// refresh was lost. The DOM is now only replaced when the markup changed.
let PERSONS_HTML = null;
// Only the cards whose markup changed are swapped: a live change in one group
// (an LED, a fader) leaves the buttons of the other groups under the finger.
function setPersonsHtml(wrap, html) {
  if (html === PERSONS_HTML) return;
  PERSONS_HTML = html;
  const next = document.createElement("template");
  next.innerHTML = html;
  const fresh = [...next.content.children];
  const old = [...wrap.children];
  const sameCards = fresh.length === old.length
    && fresh.every((el, i) => el.dataset.card && el.dataset.card === old[i].dataset.card);
  if (!sameCards) { wrap.replaceChildren(...fresh); return; }
  fresh.forEach((el, i) => { if (el.outerHTML !== old[i].outerHTML) old[i].replaceWith(el); });
}


