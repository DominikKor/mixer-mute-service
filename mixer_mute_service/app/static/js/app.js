// Mixer Mute Service UI (app.js): Status refresh, polling, update notice, sections, GitHub update, events.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// A copy of the last confirmed state with everything locked: shown while the
// service does not answer, so the strips stay in place instead of vanishing.
function staleCopy(state) {
  const channels = {};
  for (const [k, c] of Object.entries(state.channels || {})) channels[k] = { ...c, safe: false };
  return { ...state, mixer_connected: false, channels };
}

function renderPanic() {
  // Label and action both come from SERVICE_ENABLED. While it is unknown the
  // key still disables: when in doubt, stop sending.
  const pb = $("panic-btn");
  const disabled = SERVICE_ENABLED === false;
  pb.textContent = disabled ? "Re-enable" : "Panic (disable)";
  pb.className = disabled ? "primary" : "danger";
}

let refreshSeq = 0;
// The 1 s poll only asks for the mixer state; profiles, cameras, beamer and
// power settings change rarely and are read every 5 s (and after every click).
const REFRESH_SLOW_MS = 5000;
let REFRESH_SLOW_AT = 0;
const SKIPPED = Promise.resolve({ ok: false, skipped: true, status: 0, data: {} });

async function refresh({ light = false } = {}) {
  const seq = ++refreshSeq;
  const slow = !light || Date.now() - REFRESH_SLOW_AT >= REFRESH_SLOW_MS;
  if (slow) REFRESH_SLOW_AT = Date.now();
  const [health, state, desktop, cameras, beamer, power] = await Promise.all([
    api("GET", "/health"), api("GET", "/state"),
    slow ? api("GET", "/desktop/profiles") : SKIPPED,
    slow ? api("GET", "/camera-streams") : SKIPPED,
    slow ? api("GET", "/beamer/state") : SKIPPED,
    slow ? api("GET", "/power/off-config") : SKIPPED,
  ]);
  // Drop a stale response if a newer refresh has since started (e.g. the
  // per-second poll resolving after a click's own refresh) -- otherwise it
  // would overwrite the fresh state with pre-click data.
  if (seq !== refreshSeq) return;
  const stateOk = state.ok && state.data && typeof state.data.channels === "object";
  if (stateOk) {
    STATE_STALE = false;
    CONFIRMED_STATE = LAST_STATE = state.data;
  } else {
    STATE_STALE = true;
    LAST_STATE = staleCopy(CONFIRMED_STATE);
  }
  if (health.ok) SERVICE_ENABLED = !!health.data.enabled;
  else if (stateOk) SERVICE_ENABLED = !!state.data.enabled;
  else SERVICE_ENABLED = null;

  const h = health.ok ? health.data : {};
  if (!health.ok && !stateOk) {
    $("txt-conn").textContent = "service unreachable";
    $("dot-conn").className = "dot bad";
    $("txt-en").textContent = "status unknown";
    $("dot-en").className = "dot bad";
  } else {
    const connected = health.ok ? h.mixer_connected : LAST_STATE.mixer_connected;
    $("dot-conn").className = "dot " + (connected ? "ok" : "bad");
    $("txt-conn").textContent = connected ? "mixer connected" : "mixer offline";
    $("dot-en").className = "dot " + (SERVICE_ENABLED ? "ok" : "bad");
    $("txt-en").textContent = SERVICE_ENABLED ? "sending enabled" : "PANIC (disabled)";
  }
  $("txt-age").textContent = "updates: " + fmtAge(h.last_update_age_seconds);
  renderPanic();
  if (health.ok) noticeNewBuild(h.build_id);
  if (desktop.ok) renderDesktopProfiles(desktop.data);
  if (cameras.ok) renderCameraStreams(cameras.data);
  if (!beamer.skipped) renderBeamerState(beamer.ok ? beamer.data : null);
  if (!power.skipped) renderPowerOffConfig(power.ok ? power.data : null);
  if (stateOk) await bootstrapPersonsFromBackend();
  renderChannels(LAST_STATE);
  renderPersons();
}

// ---- Polling ------------------------------------------------------------------
// Each poll starts its interval after the previous run finished, so slow
// answers (Wi-Fi, a busy service) never pile up, and an error in one run
// does not end the polling. While the page is hidden (other tab, iPad locked)
// nothing is polled; when it shows again every poll runs at once.
const POLLS = [];

function poll(fn, ms) {
  const job = { fn, ms, timer: 0, running: false };
  const run = async () => {
    clearTimeout(job.timer);
    if (job.running) return;
    job.running = true;
    try {
      if (!document.hidden) await job.fn();
    } catch (err) {
      console.error(err);
    } finally {
      job.running = false;
      job.timer = setTimeout(run, job.ms);
    }
  };
  job.run = run;
  POLLS.push(job);
  run();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) for (const job of POLLS) job.run();
});

// ---- A new version on the service ---------------------------------------------
// After an update the service runs a new build while this page is still the
// old one. Reloading by itself could interrupt someone mid-edit, so the page
// offers it instead.
function noticeNewBuild(serviceBuild) {
  const mine = resolvedBuildId();
  if (mine === "dev" || !serviceBuild || serviceBuild === "dev" || serviceBuild === mine) return;
  $("reload-banner").hidden = false;
}

// ---- Collapsible sections and channel view --------------------------------
// Open/closed state and the channel view (Voll / Kompakt / Ultra) are
// remembered per device.

function loadUiPrefs() {
  try { return JSON.parse(localStorage.getItem("mixer_ui_prefs") || "{}"); } catch { return {}; }
}
function saveUiPrefs(prefs) {
  try { localStorage.setItem("mixer_ui_prefs", JSON.stringify(prefs)); } catch (_) {}
}

function setSectionOpen(panel, open) {
  panel.classList.toggle("open", open);
  panel.querySelector(".section-toggle").setAttribute("aria-expanded", String(open));
}

function initCompanionEditing() {
  const toggle = $("companion-edit-toggle");
  // Editing also shows the pencils of the categories and of "Alles ausschalten".
  const apply = () => {
    $("companion-categories").classList.toggle("editing", toggle.checked);
    $("power-off-panel").classList.toggle("editing", toggle.checked);
  };
  toggle.checked = !!loadUiPrefs().companionEditing;
  apply();
  toggle.addEventListener("change", () => {
    apply();
    const p = loadUiPrefs();
    p.companionEditing = toggle.checked;
    saveUiPrefs(p);
  });
}

function initSections() {
  const prefs = loadUiPrefs();
  const closed = prefs.closed || {};
  document.querySelectorAll(".panel[data-section]").forEach(panel => {
    setSectionOpen(panel, !closed[panel.dataset.section]);
    panel.querySelector(".section-toggle").addEventListener("click", () => {
      if (STAGE.on) return;  // the stage shows every section whole
      const open = !panel.classList.contains("open");
      setSectionOpen(panel, open);
      const p = loadUiPrefs();
      p.closed = { ...(p.closed || {}), [panel.dataset.section]: !open };
      saveUiPrefs(p);
    });
  });
  initChannelView(prefs);
}

// ---- Arrangement of the sections (per device) -------------------------------
// "Anordnen" shows ◀ ▲ ▼ ▶ in every section head. Sections sit in rows; up to
// three share a row side by side (normal view, wider than 900 px). A row sets
// width and column: the i-th of k sections in a row is 1/k wide and stays in
// the i-th place from the left. On the page every section moves up as far as
// its own columns allow (masonry), so short sections leave no empty space
// under them; a row stays level when its sections would start less than
// SECTION_LEVEL_PX apart. A section that straddles a step (a third under two
// halves) starts under the step, the others in its row still move up. A
// section never changes column for a few pixels.
// The arrows follow what is on screen:
//   ◀ ▶  swap with the neighbour in the row; a section alone in its row joins
//        the row above, on its left (◀) or right (▶) end
//   ▲ ▼  swap places (and widths) with the section right above / below it;
//        a whole-width section swaps with the row above / below; a section
//        sharing a row with nothing above / below it gets the whole width at
//        the top / bottom
// In the one-column views (narrow window, stage) "above" is the section
// before in the order, and an arrow that would change nothing there is off. The rows are kept in the UI prefs; read in order they
// also give the order of the stage miniatures. Only the moved section changes
// place in the page (moving a section reloads the camera videos in it);
// packing only sets grid places.

const SECTION_ROW_MAX = 3;
const SECTION_GRID_COLUMNS = 6;
const SECTION_GRID_ROW_PX = 2;  // .page grid-auto-rows (app.css): packing unit
const SECTION_LEVEL_PX = 120;   // rows closer than this stay level
const SECTION_HEIGHTS = {};     // section -> height in grid rows, measured by packSections
let SECTION_DEFAULT_ORDER = [];
let SECTION_ROWS = [];  // [[section, ...], ...]: rows top to bottom, sections left to right

function sectionPanelsInOrder() {
  return STAGE.order.length ? STAGE.order : [...document.querySelectorAll(".page > .panel[data-section]")];
}

function initSectionOrder() {
  const panels = [...document.querySelectorAll(".page > .panel[data-section]")];
  SECTION_DEFAULT_ORDER = panels.map(panel => panel.dataset.section);
  const prefs = loadUiPrefs();
  const saved = Array.isArray(prefs.sectionRows) ? prefs.sectionRows
    : Array.isArray(prefs.sectionOrder) ? prefs.sectionOrder.map(section => [section]) : null;
  SECTION_ROWS = normalizeSectionRows(saved);
  for (const panel of panels) {
    const label = panel.querySelector(".section-toggle").textContent.trim();
    const box = document.createElement("span");
    box.className = "section-move";
    for (const [dir, text, title] of [["left", "◀", "nach links"], ["up", "▲", "nach oben"],
                                      ["down", "▼", "nach unten"], ["right", "▶", "nach rechts"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.sectionMove = dir;
      button.textContent = text;
      button.title = `${label} ${title}`;
      button.setAttribute("aria-label", button.title);
      button.addEventListener("click", () => moveSection(panel.dataset.section, dir, button));
      box.appendChild(button);
    }
    panel.querySelector(".panel-head").appendChild(box);
  }
  applySectionRows();
  // Heights change all the time (sections open and close, program rows are
  // added, the window is resized). The observer runs after layout and before
  // paint, so a section that grew never overlaps the next one for a frame.
  // Packing only moves sections, it does not resize them: no loop.
  const packer = new ResizeObserver(() => packSections());
  panels.forEach(panel => packer.observe(panel));
  const toggle = $("arrange-toggle");
  toggle.addEventListener("change", () => {
    document.body.classList.toggle("arranging", toggle.checked);
    $("arrange-reset-btn").hidden = !toggle.checked;
  });
  $("arrange-reset-btn").addEventListener("click", () => {
    SECTION_ROWS = normalizeSectionRows(null);
    applySectionRows();
    const p = loadUiPrefs();
    delete p.sectionRows;
    delete p.sectionOrder;
    saveUiPrefs(p);
  });
}

// Unknown names are dropped; sections missing from the saved rows (new ones) get a row at the end.
function normalizeSectionRows(rows) {
  const known = new Set(SECTION_DEFAULT_ORDER), seen = new Set(), result = [];
  for (const row of Array.isArray(rows) ? rows : []) {
    const clean = (Array.isArray(row) ? row : []).filter(s => known.has(s) && !seen.has(s));
    clean.forEach(s => seen.add(s));
    for (let i = 0; i < clean.length; i += SECTION_ROW_MAX) result.push(clean.slice(i, i + SECTION_ROW_MAX));
  }
  for (const section of SECTION_DEFAULT_ORDER) if (!seen.has(section)) result.push([section]);
  return result;
}

// Put the sections in page order and in their grid places. With `before`
// (the order until now) only the sections that are out of order move in the
// page: all but the longest run of sections that kept their order (moving a
// section reloads the camera videos in it).
function applySectionRows(before = null) {
  const order = SECTION_ROWS.flat();
  const bySection = Object.fromEntries(sectionPanelsInOrder().map(panel => [panel.dataset.section, panel]));
  if (STAGE.order.length) STAGE.order = order.map(section => bySection[section]);
  const page = document.querySelector(".page");
  const keep = before ? keptInOrder(before, order) : new Set();
  for (let i = order.length - 1; i >= 0; i--) {
    const section = order[i], next = order[i + 1];
    if (keep.has(section)) continue;
    if (STAGE.on) stagePlace($("stage-rail-list"), STAGE.thumbs[section], next ? STAGE.thumbs[next] : null);
    else stagePlace(page, bySection[section], next ? bySection[next] : null);
  }
  packSections();
  if (STAGE.on) scheduleStageLayout();
}

// The longest common subsequence of two orders, as a set.
function keptInOrder(a, b) {
  const len = a.map(() => new Array(b.length + 1).fill(0));
  len.push(new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      len[i][j] = a[i] === b[j] ? len[i + 1][j + 1] + 1 : Math.max(len[i + 1][j], len[i][j + 1]);
    }
  }
  const keep = new Set();
  for (let i = 0, j = 0; i < a.length && j < b.length;) {
    if (a[i] === b[j]) { keep.add(a[i]); i++; j++; } else if (len[i + 1][j] >= len[i][j + 1]) i++; else j++;
  }
  return keep;
}

// Where every section goes: {section: {start, span, top, bottom}}, columns
// 0..5 of the page grid, tops and bottoms in grid rows. heightOf(section) is
// its height in grid rows; `single`: one column in order (narrow view,
// stage); `level`: SECTION_LEVEL_PX in grid rows. Pure (tests/test_section_layout.py).
function layoutSectionRows(rows, heightOf, { single = false, level = 1 } = {}) {
  const cols = SECTION_GRID_COLUMNS, skyline = new Array(cols).fill(0), layout = {};
  const put = (section, start, span, top) => {
    const bottom = top + heightOf(section);
    layout[section] = { start, span, top, bottom };
    for (let c = start; c < start + span; c++) skyline[c] = bottom;
  };
  for (const row of rows) {
    if (single || row.length === 1) {
      for (const section of row) put(section, 0, cols, Math.max(...skyline));
      continue;
    }
    const span = cols / row.length;
    // A section starts under the lowest bottom in its own columns; one that
    // straddles a step starts under the step, the others still move up.
    const natural = row.map((_, i) => Math.max(...skyline.slice(i * span, (i + 1) * span)));
    const highest = Math.max(...natural);
    row.forEach((section, i) => {
      put(section, i * span, span, highest - natural[i] < level ? highest : natural[i]);
    });
  }
  return layout;
}

// The section right above (the latest bottom among the earlier ones sharing
// a column) or right below (the earliest top among the later ones) `section`.
function sectionNeighbour(rows, section, dir, layout) {
  const order = rows.flat(), at = order.indexOf(section), me = layout[section];
  const overlaps = other => layout[other].start < me.start + me.span && me.start < layout[other].start + layout[other].span;
  let best = null;
  if (dir === "up") {
    for (let i = 0; i < at; i++) {
      if (overlaps(order[i]) && (!best || layout[order[i]].bottom >= layout[best].bottom)) best = order[i];
    }
  } else {
    for (let i = order.length - 1; i > at; i--) {
      if (overlaps(order[i]) && (!best || layout[order[i]].top <= layout[best].top)) best = order[i];
    }
  }
  return best;
}

// The rows after moving `section` in `dir` (layout: layoutSectionRows of
// `rows`), or null when it cannot move that way. Pure.
function sectionRowsAfterMove(rows, section, dir, layout) {
  rows = rows.map(row => [...row]);
  const r = rows.findIndex(row => row.includes(section));
  if (r < 0) return null;
  const row = rows[r], c = row.indexOf(section);
  const alone = row.length === 1;
  if (dir === "left" || dir === "right") {
    const n = dir === "left" ? c - 1 : c + 1;
    if (n >= 0 && n < row.length) {
      [row[c], row[n]] = [row[n], row[c]];
    } else if (alone && r > 0 && rows[r - 1].length < SECTION_ROW_MAX) {
      if (dir === "left") rows[r - 1].unshift(section); else rows[r - 1].push(section);
      rows.splice(r, 1);
    } else return null;
  } else if (alone) {
    const other = dir === "up" ? r - 1 : r + 1;
    if (other < 0 || other >= rows.length) return null;
    [rows[r], rows[other]] = [rows[other], rows[r]];
  } else {
    const other = sectionNeighbour(rows, section, dir, layout);
    if (other) {
      const o = rows.findIndex(x => x.includes(other));
      rows[o][rows[o].indexOf(other)] = section;
      row[c] = other;
    } else if (layout[section].span === SECTION_GRID_COLUMNS) {
      return null;  // one-column view: already whole width, nothing would change on screen
    } else {
      row.splice(c, 1);
      if (dir === "up") rows.unshift([section]); else rows.push([section]);
    }
  }
  return rows;
}

// The layout as it is on screen now (one column when the page is no grid).
function currentSectionLayout() {
  const page = document.querySelector(".page");
  const single = STAGE.on || !page || getComputedStyle(page).display !== "grid";
  return layoutSectionRows(SECTION_ROWS, section => SECTION_HEIGHTS[section] || 1,
                           { single, level: SECTION_LEVEL_PX / SECTION_GRID_ROW_PX });
}

// Measure the sections and put them in their grid places (6 columns, 2 px
// rows). Columns first: a section's height depends on its width. Not on the
// stage; in the narrow one-column view only measured. Then the arrows.
function packSections() {
  const page = document.querySelector(".page");
  if (!page) return;
  if (!STAGE.on) placeSections(page);
  // What an arrow does depends on the layout: refresh after every packing.
  syncSectionMoveButtons();
}

function placeSections(page) {
  const panels = sectionPanelsInOrder();
  const grid = getComputedStyle(page).display === "grid";
  const columns = grid ? currentSectionLayout() : {};
  for (const panel of panels) {
    const place = columns[panel.dataset.section];
    panel.style.gridColumn = place ? `${place.start + 1} / span ${place.span}` : "";
    if (!place) panel.style.gridRow = "";
  }
  for (const panel of panels) {
    const margin = parseFloat(getComputedStyle(panel).marginTop) || 0;
    SECTION_HEIGHTS[panel.dataset.section] = Math.max(1, Math.ceil((panel.offsetHeight + margin) / SECTION_GRID_ROW_PX));
  }
  if (!grid) return;
  const layout = currentSectionLayout();
  for (const panel of panels) {
    const place = layout[panel.dataset.section];
    if (place) panel.style.gridRow = `${place.top + 1} / span ${place.bottom - place.top}`;
  }
}

function movedSectionRows(section, dir, layout = currentSectionLayout()) {
  return sectionRowsAfterMove(SECTION_ROWS, section, dir, layout);
}

function moveSection(section, dir, button) {
  const rows = movedSectionRows(section, dir);
  if (!rows) return;
  const before = SECTION_ROWS.flat();
  SECTION_ROWS = rows;
  applySectionRows(before);
  const panel = sectionPanelsInOrder().find(p => p.dataset.section === section);
  if (!STAGE.on && panel) panel.scrollIntoView({ block: "nearest" });
  const p = loadUiPrefs();
  p.sectionRows = rows;
  delete p.sectionOrder;
  saveUiPrefs(p);
  if (button && !button.disabled) button.focus({ preventScroll: true });
}

function syncSectionMoveButtons() {
  const layout = currentSectionLayout();
  for (const panel of sectionPanelsInOrder()) {
    for (const button of panel.querySelectorAll("[data-section-move]")) {
      button.disabled = !movedSectionRows(panel.dataset.section, button.dataset.sectionMove, layout);
    }
  }
}

const CHANNEL_VIEWS = ["full", "compact", "ultra"];

function applyChannelView(view) {
  const strips = $("channels");
  // Ultra builds on compact (no faders, no "Immer an"), so it keeps that class.
  strips.classList.toggle("compact", view !== "full");
  strips.classList.toggle("ultra", view === "ultra");
  const toggle = document.querySelector(".view-switch");
  toggle.dataset.view = view;
  toggle.querySelector(".view-switch-label").textContent = { full: "Voll", compact: "Kompakt", ultra: "Ultra" }[view];
  document.querySelectorAll("[data-channel-view]").forEach(button => {
    button.setAttribute("aria-checked", String(button.dataset.channelView === view));
  });
}

function initChannelView(prefs) {
  // Earlier versions stored only the on/off Kompakt switch.
  const view = CHANNEL_VIEWS.includes(prefs.channelView) ? prefs.channelView : (prefs.compact ? "compact" : "full");
  applyChannelView(view);
  document.querySelectorAll("[data-channel-view]").forEach(button => button.addEventListener("click", () => {
    applyChannelView(button.dataset.channelView);
    const p = loadUiPrefs();
    p.channelView = button.dataset.channelView;
    delete p.compact;
    saveUiPrefs(p);
  }));
}

// ---- update from GitHub ---------------------------------------------------
// The service cannot update itself; POST /update/start launches a Windows task
// that stops, replaces and restarts it. The dialog keeps polling /update/status
// through the restart and closes itself once the run has finished.

const UPDATE_POLL_MS = 1500;
const UPDATE_TIMEOUT_MS = 10 * 60 * 1000;
const UPDATE_CLOSE_MS = 5000;
const UPDATE = { runId: null, timer: null, startedAt: 0, running: false, sawRestart: false, program: false };

async function fetchUpdateStatus() {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 3000);
  try {
    const res = await fetch("/update/status", { signal: ctrl.signal, cache: "no-store" });
    return res.ok ? await res.json() : null;
  } catch (_) {
    return null;  // service down, e.g. restarting during the update
  } finally {
    clearTimeout(t);
  }
}

function setUpdateLcd(html, cls) {
  const el = $("update-lcd");
  el.className = "update-lcd" + (cls ? " " + cls : "");
  el.innerHTML = html;
}

function showUpdateLog(lines) {
  const el = $("update-log");
  if (!lines || !lines.length) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = lines.join("\n");
  el.scrollTop = el.scrollHeight;
}

// Second step of the Update key: the display agents (Display Services) of all
// PCs the service sends to (Streaming PC, Anzeige PC, ...), not only of the PC
// this page runs on. After a program update the page reloads first; this flag
// makes the new page carry on with the agents.
const AGENT_STEP_KEY = "mixer_update_agent_step";

// [{pc, name, present, version, latest, current, self_update}] or null.
async function fetchAgentStatus() {
  const r = await api("GET", "/update/agents");
  return r.ok && r.data && r.data.ok !== false && Array.isArray(r.data.agents) ? r.data.agents : null;
}

function agentNeedsUpdate(agents) {
  return !!(agents && agents.some(a => a.present && (!a.self_update || !a.current)));
}

function agentsTooOld(agents) {
  return !!(agents && agents.some(a => a.present && !a.self_update));
}

// The Update key always starts the Display Services again: one that is behind
// through its self-update, a current one by a plain restart.
function agentsToRestart(agents) {
  return (agents || []).filter(a => a.present && a.self_update);
}

function agentState(a) {
  if (!a.present) return "nicht erreichbar";
  if (a.error) return `nicht neu gestartet: ${escapeHtml(a.error)}`;
  if (!a.self_update) return "zu alt für das automatische Update";
  // Newer agents say whether they find mpv (music); without it nothing plays there.
  const player = "mpv" in a && !a.mpv ? " · mpv nicht gefunden, Musik spielt dort nicht" : "";
  if (a.updated) return `${escapeHtml(a.version)} (aktualisiert und neu gestartet)${player}`;
  if (a.restarted) return `${escapeHtml(a.version)} (neu gestartet)${player}`;
  return (a.current ? `${escapeHtml(a.version)} (aktuell)` : `${escapeHtml(a.version)} → ${escapeHtml(a.latest)}`) + player;
}

function agentLine(agents) {
  if (!agents) return "Display Services: Status unbekannt";
  if (!agents.some(a => a.present)) return "Display Services: keiner erreichbar";
  return agents.map(a => `Display Service ${escapeHtml(a.name)} (${escapeHtml(a.pc)}): ${agentState(a)}`).join("<br>");
}

async function refreshUpdateButton() {
  const [data, agent] = await Promise.all([fetchUpdateStatus(), fetchAgentStatus()]);
  const btn = $("update-btn");
  if (!data) return;
  const agentDue = agentNeedsUpdate(agent);
  // Also with nothing new: the key then restarts the Display Services.
  btn.hidden = !data.available && !agentDue && !agentsToRestart(agent).length;
  btn.classList.toggle("running", !!data.active);
  btn.textContent = data.active ? "Update läuft" : "Update";
  btn.title = [data.installed ? `Installiert: ${data.installed}` : "", agentDue ? "Ein Display Service ist veraltet" : ""]
    .filter(Boolean).join(" — ") || "Mixer Service und die Display Services aktualisieren";
}

async function openUpdateDialog() {
  const dlg = $("update-dialog");
  $("update-start").hidden = false;
  $("update-start").disabled = true;
  $("update-cancel").textContent = "Abbrechen";
  $("update-warn").hidden = false;
  $("update-agent-manual").hidden = true;
  showUpdateLog([]);
  setUpdateLcd('<span class="dim">Status wird geladen…</span>');
  if (!dlg.open) dlg.showModal();
  const [data, agent] = await Promise.all([fetchUpdateStatus(), fetchAgentStatus()]);
  if (!data) { setUpdateLcd("Service nicht erreichbar.", "err"); return; }
  if (data.active && data.status) {
    UPDATE.runId = data.status.run_id;  // join a run that is already going
    beginUpdatePolling();
    return;
  }
  UPDATE.program = !!data.available;
  const program = data.available
    ? `Programm: ${escapeHtml(data.installed || "unbekannt")}<br><span class="dim">Die neueste Version wird von GitHub geladen, geprüft und installiert.</span>`
    : `Programm: ${escapeHtml(data.reason || "Update nicht verfügbar.")}`;
  setUpdateLcd(`${program}<br>${agentLine(agent)}`);
  $("update-agent-manual").hidden = !agentsTooOld(agent);
  const restart = agentsToRestart(agent).length > 0;
  if (!data.available && restart && !agentNeedsUpdate(agent)) {
    setUpdateLcd(`${program}<br>${agentLine(agent)}<br><span class="dim">Die Display Services werden neu gestartet.</span>`);
  }
  $("update-start").disabled = !data.available && !agentNeedsUpdate(agent) && !restart;
}

async function startUpdate() {
  $("update-start").disabled = true;
  if (!UPDATE.program) return runAgentStep("");
  // Ending a hung earlier run first can take up to ~40 s (app/updater.py).
  const r = await api("POST", "/update/start", undefined, { timeout: 60000 });
  if (!(r.ok && r.data.ok)) {
    setUpdateLcd(`Update nicht gestartet: ${escapeHtml(r.data.reason || "HTTP " + r.status)}`, "err");
    $("update-start").disabled = false;
    return;
  }
  UPDATE.runId = r.data.run_id;
  beginUpdatePolling();
}

function beginUpdatePolling() {
  UPDATE.running = true;
  UPDATE.sawRestart = false;
  UPDATE.startedAt = Date.now();
  $("update-start").hidden = true;
  $("update-warn").hidden = true;
  $("update-cancel").textContent = "Im Hintergrund weiter";
  setUpdateLcd('<span class="dim">Update gestartet…</span>');
  clearTimeout(UPDATE.timer);
  UPDATE.timer = setTimeout(pollUpdate, UPDATE_POLL_MS);
}

async function pollUpdate() {
  if (!UPDATE.running) return;
  if (Date.now() - UPDATE.startedAt > UPDATE_TIMEOUT_MS) {
    finishUpdate(false, "Keine Rückmeldung vom Update. Details im Log unter C:\\ProgramData\\MixerMuteService\\logs\\update.log.");
    return;
  }
  const data = await fetchUpdateStatus();
  if (!UPDATE.running) return;  // closed or finished while waiting
  if (!data) {
    UPDATE.sawRestart = true;
    setUpdateLcd('Service startet neu…<br><span class="dim">Die Seite verbindet sich danach automatisch wieder.</span>');
  } else {
    const st = data.status;
    showUpdateLog(data.log);
    if (st && st.run_id === UPDATE.runId) {
      if (st.state === "up_to_date") {
        UPDATE.running = false;
        return runAgentStep(`Programm: ${escapeHtml(st.message || "bereits aktuell.")}`);
      }
      if (st.state === "updated") {
        // The new page goes on with the Display Service of this PC.
        try { sessionStorage.setItem(AGENT_STEP_KEY, st.message || "Programm aktualisiert."); } catch (_) {}
        return finishUpdate(true, escapeHtml(st.message || "Aktualisiert."), true);
      }
      if (st.state === "failed") return finishUpdate(false, "Update fehlgeschlagen: " + escapeHtml(st.message || ""));
      setUpdateLcd(escapeHtml(st.message || "Update läuft…") + (st.to ? `<br><span class="dim">${escapeHtml(st.from || "?")} → ${escapeHtml(st.to)}</span>` : ""));
    }
  }
  UPDATE.timer = setTimeout(pollUpdate, UPDATE_POLL_MS);
}

function finishUpdate(ok, message, reload) {
  UPDATE.running = false;
  clearTimeout(UPDATE.timer);
  refreshUpdateButton();
  $("update-cancel").textContent = "Schließen";
  if (!ok) { setUpdateLcd(message, "err"); return; }  // stays open so the error can be read
  let left = Math.round(UPDATE_CLOSE_MS / 1000);
  const tick = () => {
    setUpdateLcd(`${message}<br><span class="dim">Schließt in ${left} s${reload ? " und lädt die neue Version" : ""}…</span>`, "ok");
    if (left-- <= 0) {
      $("update-dialog").close();
      if (reload) location.reload();
      return;
    }
    UPDATE.timer = setTimeout(tick, 1000);
  };
  tick();
}

// Start every Display Service again (all PCs at once): one that is behind gets
// the new version, a current one a plain restart. programLine says how the
// program step ended ("" when there was none).
async function runAgentStep(programLine) {
  const head = programLine ? `${programLine}<br>` : "";
  $("update-start").hidden = true;
  $("update-warn").hidden = true;
  setUpdateLcd(`${head}<span class="dim">Display Services werden geprüft…</span>`);
  const agents = await fetchAgentStatus();
  const manual = agentsTooOld(agents);
  $("update-agent-manual").hidden = !manual;
  const due = agentsToRestart(agents);
  if (!due.length) return finishUpdate(!manual, `${head}${agentLine(agents)}`, false);
  const verb = due.some(a => !a.current) ? "aktualisiert bzw. neu gestartet" : "neu gestartet";
  setUpdateLcd(`${head}Display Services werden ${verb}…<br><span class="dim">${due.map(a => escapeHtml(a.name)).join(", ")}</span>`);
  // Each agent restarts and is waited for (up to 45 s each, all at once).
  const r = await api("POST", "/update/agents", undefined, { timeout: 120000 });
  if (!Array.isArray(r.data.agents)) {
    return finishUpdate(false, `${head}Display Services nicht neu gestartet: ${escapeHtml(r.data.reason || "HTTP " + r.status)}`);
  }
  finishUpdate(r.ok && r.data.ok !== false && !manual, `${head}${agentLine(r.data.agents)}`, false);
}

// After the reload that follows a program update: carry on with the agent.
function resumeAgentStep() {
  let programLine = null;
  try {
    programLine = sessionStorage.getItem(AGENT_STEP_KEY);
    sessionStorage.removeItem(AGENT_STEP_KEY);
  } catch (_) {}
  if (programLine === null) return;
  $("update-cancel").textContent = "Schließen";
  showUpdateLog([]);
  $("update-agent-manual").hidden = true;
  $("update-dialog").showModal();
  runAgentStep(`Programm: ${escapeHtml(programLine)}`);
}

function closeUpdateDialog() {
  // While an update runs, closing only hides the dialog; the run continues.
  clearTimeout(UPDATE.timer);
  UPDATE.running = false;
  $("update-dialog").close();
  refreshUpdateButton();
}

$("update-btn").addEventListener("click", openUpdateDialog);
resumeAgentStep();
$("update-start").addEventListener("click", startUpdate);
$("update-cancel").addEventListener("click", closeUpdateDialog);
$("update-dialog").addEventListener("cancel", (e) => { e.preventDefault(); closeUpdateDialog(); });

// ---- events -------------------------------------------------------------

// Optimistically reflect an action in the UI right away. Only the shown copy
// (LAST_STATE) is changed, never CONFIRMED_STATE: the follow-up refresh shows
// the server's answer, and if that refresh fails the page falls back to the
// confirmed state, so a failed action never stays on screen as if it worked.
function optimistic(channels, muted) {
  refreshSeq++;  // a poll already in flight carries pre-click state; drop it
  LAST_STATE = { ...LAST_STATE, channels: { ...LAST_STATE.channels } };
  for (const ch of channels) {
    const c = LAST_STATE.channels[ch];
    if (c && c.safe) LAST_STATE.channels[ch] = { ...c, muted: muted === "toggle" ? !c.muted : muted };
  }
  renderChannels(LAST_STATE);
}

// One toggle per channel at a time: a double tap while the first request is
// still pending would otherwise mute and immediately unmute again.
const TOGGLE_BUSY = new Set();
async function toggleChannel(ch) {
  if (TOGGLE_BUSY.has(ch)) return;
  TOGGLE_BUSY.add(ch);
  try {
    optimistic([ch], "toggle");
    await act("POST", `/channels/${ch}/toggle`, undefined, `ch${ch} toggle`);
  } finally {
    TOGGLE_BUSY.delete(ch);
    renderChannels(LAST_STATE);
  }
}

const ALWAYS_ON_BUSY = new Set();
async function setChannelAlwaysOn(ch, enabled) {
  const key = String(ch);
  const channel = LAST_STATE.channels && LAST_STATE.channels[key];
  if (!channel || ALWAYS_ON_BUSY.has(key)) return;
  refreshSeq++;
  LAST_STATE = { ...LAST_STATE, channels: { ...LAST_STATE.channels, [key]: { ...channel, always_on: enabled } } };
  ALWAYS_ON_BUSY.add(key);
  renderChannels(LAST_STATE);
  try {
    const result = await api("POST", `/channels/${ch}/always-on`, { enabled });
    if (!result.ok || result.data.ok === false) {
      LAST_STATE = { ...LAST_STATE, channels: { ...LAST_STATE.channels, [key]: channel } };
      toast(`ch${ch}: ${result.data.reason || "HTTP " + result.status}`, false);
    } else {
      toast(`ch${ch}: ${enabled ? "Immer an" : "Gruppen-Schutz aus"}`, true);
    }
  } finally {
    ALWAYS_ON_BUSY.delete(key);
    await refresh();
  }
}

function savePersonChannelDescription(input) {
  const list = loadPersons();
  const person = list.find(item => item.id === input.dataset.pid);
  const channel = person && person.channels && person.channels[String(input.dataset.ch)];
  if (!channel) return;
  channel.description = input.value.trim();
  savePersons(list);
  putPreset(person.name, input.dataset.ch, { description: channel.description });
  // The field already shows the new text. Only remember the new markup instead
  // of redrawing: a redraw now would swap the key the user is tapping (the tap
  // that took the focus away is what fired this change).
  PERSONS_HTML = buildPersonsHtml();
}
