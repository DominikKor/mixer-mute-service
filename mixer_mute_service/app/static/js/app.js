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
  pb.textContent = disabled ? "Senden ein" : "Panic";
  pb.title = disabled ? "Senden an das Pult wieder erlauben" : "Panic: sofort jedes Senden an das Pult sperren";
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
    $("txt-conn").textContent = "Dienst nicht erreichbar";
    $("dot-conn").className = "dot bad";
    $("txt-en").textContent = "Status unbekannt";
    $("dot-en").className = "dot bad";
  } else {
    const connected = health.ok ? h.mixer_connected : LAST_STATE.mixer_connected;
    $("dot-conn").className = "dot " + (connected ? "ok" : "bad");
    $("txt-conn").textContent = connected ? "Mixer verbunden" : "Mixer getrennt";
    $("dot-en").className = "dot " + (SERVICE_ENABLED ? "ok" : "bad");
    $("txt-en").textContent = SERVICE_ENABLED ? "Senden aktiv" : "PANIC: Senden gesperrt";
  }
  $("txt-age").textContent = "Pult-Daten: " + fmtAge(h.last_update_age_seconds);
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
// A gallery: the user sets only the ORDER of the sections and a WIDTH for each
// (⅓, ½, ⅔ or whole). Where they go is worked out like masonry: in order,
// every section drops into the place of its width where it can start highest
// (left first on a tie), so short sections stack under each other next to a
// tall one. There are no rows to get stuck in: every order and every width
// can always be reached and undone.
// Views: "three" (page at least SECTION_THREE_MIN_PX wide): widths as set;
// "two" (narrower, e.g. iPad upright): ⅓ and ½ are half, ⅔ and whole are
// whole; "one" (narrow window below 900 px, and the stage): one column in
// order. A section whose content does not fit its chosen width (measured, or
// --min-section-width in its CSS) gets the next wider one that holds it; the
// width key shows the width it gets, the choice stays for wider screens.
// Calm during a service: a section keeps the place it had (remembered per
// view, also across reloads). While only contents change (program rows, song
// text) it stays there and just moves up or down in its columns, unless
// another place lets it start more than SECTION_RESCUE_PX higher (rare: a
// big hole). After a user's change (opening or closing a section, order,
// width) or while "Anordnen" is on, SECTION_STICK_PX is enough. A view in
// which a section has no place yet (first load, the other iPad orientation,
// a new width) settles for SECTION_SETTLE_MS while contents still arrive;
// only places from settled packing are saved, so half-loaded contents never
// fix a place. In the two-column view the width key keeps its own width
// (half or whole); the widths for wider screens stay as they are.
// "Anordnen" shows in every section head: ⠿ to drag it to another place in
// the order (finger or mouse, not in the one-column view), ◀ ▶ to move it
// earlier / later in the order (as many steps as it takes for the page to
// change) and the width key (⅓ → ½ → ⅔ → whole; in the two-column view
// half ↔ whole). The order also gives the order of the stage miniatures.
// Placing only sets grid places; the elements change place in the page only
// when the order changes, and then only those out of order (moving one
// reloads the camera videos in it).

const SECTION_GRID_COLUMNS = 6;
const SECTION_GRID_ROW_PX = 2;      // .page grid-auto-rows (app.css): placing unit
const SECTION_WIDTHS = [2, 3, 4, 6];  // ⅓, ½, ⅔, whole in grid columns
const SECTION_WIDTH_LABELS = { 2: "⅓", 3: "½", 4: "⅔", 6: "ganz" };
const SECTION_STICK_PX = 120;       // a better place must be this much higher
const SECTION_RESCUE_PX = 300;      // ... and during a service this much
const SECTION_THREE_MIN_PX = 1100;  // page width for thirds
const SECTION_SETTLE_MS = 4000;     // after loading: contents still arrive
const SECTION_HEIGHTS = {};         // section -> height in grid rows, measured by packSections
let SECTION_MIN_SPAN = {};          // section -> grid columns its content needs now (measured)
let SECTION_DEFAULT_ORDER = [];
let SECTION_ORDER = [];             // sections in the user's order
let SECTION_WIDTH = {};             // section -> 2 | 3 | 4 | 6
let SECTION_VIEW_WIDTH = {};        // view -> {section: width}: the width key's choice in that view ("two")
let SECTION_PLACES = {};            // view -> {section: first grid column}, as placed now
let SECTION_SAVED_PLACES = {};      // the same, as saved (only from settled packing)
let SECTION_SETTLE = null;          // {view, until}: that view still settles
let SECTION_FREE_UNTIL = 0;         // until then (performance.now) sections may change place
let SECTION_DRAG = null;

// The width of a section in a view, in grid columns.
// `min`: the section's content needs at least that many columns (see
// sectionMinSpan): then the next wider width of the view, the chosen one stays.
function sectionSpan(width, view, min = 0) {
  const spans = view === "one" ? [SECTION_GRID_COLUMNS] : view === "two" ? [3, SECTION_GRID_COLUMNS] : SECTION_WIDTHS;
  const base = view === "one" ? SECTION_GRID_COLUMNS : view === "two" ? (width <= 3 ? 3 : SECTION_GRID_COLUMNS) : width;
  return spans.find(span => span >= base && span >= min) || SECTION_GRID_COLUMNS;
}

// The fewest grid columns that are `needPx` wide (columns `colPx`, gaps
// `gapPx`); 0 when nothing is needed.
function sectionMinSpan(needPx, colPx, gapPx) {
  if (!(needPx > 0)) return 0;
  for (let span = 1; span < SECTION_GRID_COLUMNS; span++) {
    if (span * colPx + (span - 1) * gapPx >= needPx - 0.5) return span;
  }
  return SECTION_GRID_COLUMNS;
}

// The first columns a section of `span` columns may start at (aligned to its
// width; the last place ends at the right edge).
function sectionStarts(span) {
  const starts = [];
  for (let c = 0; c + span <= SECTION_GRID_COLUMNS; c += span) starts.push(c);
  if (starts[starts.length - 1] !== SECTION_GRID_COLUMNS - span) starts.push(SECTION_GRID_COLUMNS - span);
  return starts;
}

// Where every section goes: {section: {start, span, top, bottom}}, columns
// 0..5 of the page grid, tops and bottoms in grid rows. widthOf(section):
// 2 | 3 | 4 | 6; heightOf(section): grid rows; minSpan: section -> columns
// its content needs. `places` (section -> start) are kept when `keep`, else
// when at most `stick` rows lower than the best place. Pure
// (tests/test_section_layout.py).
function layoutSections(order, widthOf, heightOf, { view = "three", places = {}, stick = 0, keep = false,
                                                     minSpan = {} } = {}) {
  const skyline = new Array(SECTION_GRID_COLUMNS).fill(0), layout = {};
  for (const section of order) {
    const span = sectionSpan(widthOf(section), view, minSpan[section] || 0), starts = sectionStarts(span);
    const topAt = at => Math.max(...skyline.slice(at, at + span));
    let start = starts[0];
    for (const at of starts) if (topAt(at) < topAt(start)) start = at;
    const had = places[section];
    if (starts.includes(had) && (keep || topAt(had) - topAt(start) <= stick)) start = had;
    const top = topAt(start), bottom = top + Math.max(1, heightOf(section));
    layout[section] = { start, span, top, bottom };
    for (let c = start; c < start + span; c++) skyline[c] = bottom;
  }
  return layout;
}

// The order after moving `section` earlier ("left") or later ("right"): as
// many steps as it takes for layoutOf(order) to change; null at the end.
function sectionOrderAfterMove(order, section, dir, layoutOf) {
  const at = order.indexOf(section);
  if (at < 0) return null;
  // Where every section is, whatever the order of the keys.
  const seen = layout => Object.keys(layout).sort().map(s => `${s}:${JSON.stringify(layout[s])}`).join();
  const step = dir === "left" ? -1 : 1, now = seen(layoutOf(order));
  for (let i = at + step; i >= 0 && i < order.length; i += step) {
    const next = order.filter(s => s !== section);
    next.splice(i, 0, section);
    if (seen(layoutOf(next)) !== now) return next;
  }
  return null;
}

// The next width of the width key: the next one that looks different in
// `view` with the content's minimum `min` (null when none does, as in the
// one-column view).
function nextSectionWidth(width, view, min = 0) {
  const i = SECTION_WIDTHS.indexOf(width);
  for (let k = 1; k < SECTION_WIDTHS.length; k++) {
    const next = SECTION_WIDTHS[(i + k) % SECTION_WIDTHS.length];
    if (sectionSpan(next, view, min) !== sectionSpan(width, view, min)) return next;
  }
  return null;
}

// The saved arrangement: {order, widths, viewWidths}. Earlier versions kept
// rows (alone: whole, two: halves, three: thirds; counted after unknown and
// repeated names are dropped, a longer row split into threes) or only an
// order (whole). Unknown sections are dropped, new ones come last, whole.
// viewWidths: the two-column view's own choice, half (3) or whole (6). Pure.
function sectionArrangementFromPrefs(prefs, known) {
  let order = [], widths = {}, viewWidths = {};
  const saved = prefs && prefs.sectionArrangement;
  if (saved && Array.isArray(saved.order)) {
    order = saved.order;
    widths = saved.widths && typeof saved.widths === "object" ? saved.widths : {};
    viewWidths = saved.viewWidths && typeof saved.viewWidths === "object" ? saved.viewWidths : {};
  } else if (prefs && Array.isArray(prefs.sectionRows)) {
    const taken = new Set();
    for (const row of prefs.sectionRows) {
      if (!Array.isArray(row)) continue;
      const clean = row.filter(section => known.includes(section) && !taken.has(section));
      clean.forEach(section => taken.add(section));
      for (let i = 0; i < clean.length; i += 3) {
        const part = clean.slice(i, i + 3);
        for (const section of part) { order.push(section); widths[section] = SECTION_GRID_COLUMNS / part.length; }
      }
    }
  } else if (prefs && Array.isArray(prefs.sectionOrder)) {
    order = prefs.sectionOrder;
  }
  const seen = new Set(), clean = [], cleanWidths = {}, two = {};
  const ownTwo = viewWidths.two && typeof viewWidths.two === "object" ? viewWidths.two : {};
  for (const section of [...order, ...known]) {
    if (!known.includes(section) || seen.has(section)) continue;
    seen.add(section);
    clean.push(section);
    cleanWidths[section] = SECTION_WIDTHS.includes(widths[section]) ? widths[section] : SECTION_GRID_COLUMNS;
    if (ownTwo[section] === 3 || ownTwo[section] === SECTION_GRID_COLUMNS) two[section] = ownTwo[section];
  }
  return { order: clean, widths: cleanWidths, viewWidths: Object.keys(two).length ? { two } : {} };
}

// The width a section has chosen for `view`: the view's own choice, else its width. Pure.
function sectionWidthIn(widths, viewWidths, view, section) {
  const own = viewWidths[view] && viewWidths[view][section];
  return own || widths[section];
}

// A view still has to find places when one of its sections has none. Pure.
function sectionPlacesMissing(order, places) {
  return order.some(section => !Number.isInteger((places || {})[section]));
}

// How a packing may move sections and whether its places are saved. Pure.
//   dragging: everything stays (the dragged one moves by the drag);
//   a user's action, "Anordnen" or a view that still settles: SECTION_STICK_PX;
//   otherwise (contents changing during a service): SECTION_RESCUE_PX.
// Places are saved only when nothing is dragged and the view has settled.
function sectionPackRule({ dragging = false, free = false, arranging = false, settling = false } = {}) {
  const stick = dragging ? "keep" : free || arranging || settling ? "stick" : "rescue";
  return { stick, save: !dragging && !settling };
}

function sectionPanelsInOrder() {
  return STAGE.order.length ? STAGE.order : [...document.querySelectorAll(".page > .panel[data-section]")];
}

function sectionPanel(section) {
  return sectionPanelsInOrder().find(panel => panel.dataset.section === section);
}

function currentSectionView() {
  const page = document.querySelector(".page");
  if (STAGE.on || !page || getComputedStyle(page).display !== "grid") return "one";
  return page.clientWidth >= SECTION_THREE_MIN_PX ? "three" : "two";
}

function sectionLayoutFor(order, view, { drop = null, stick = "stick" } = {}) {
  const places = { ...(SECTION_PLACES[view] || {}) };
  if (drop) delete places[drop];
  const rows = (stick === "rescue" ? SECTION_RESCUE_PX : SECTION_STICK_PX) / SECTION_GRID_ROW_PX;
  return layoutSections(order, section => sectionWidthIn(SECTION_WIDTH, SECTION_VIEW_WIDTH, view, section),
                        section => SECTION_HEIGHTS[section] || 1,
                        { view, places, stick: rows, keep: stick === "keep",
                          minSpan: view === "one" ? {} : SECTION_MIN_SPAN });
}

function initSectionOrder() {
  const panels = [...document.querySelectorAll(".page > .panel[data-section]")];
  SECTION_DEFAULT_ORDER = panels.map(panel => panel.dataset.section);
  const prefs = loadUiPrefs();
  const saved = sectionArrangementFromPrefs(prefs, SECTION_DEFAULT_ORDER);
  SECTION_ORDER = saved.order;
  SECTION_WIDTH = saved.widths;
  SECTION_VIEW_WIDTH = saved.viewWidths;
  SECTION_PLACES = prefs.sectionPlaces && typeof prefs.sectionPlaces === "object" ? prefs.sectionPlaces : {};
  SECTION_SAVED_PLACES = SECTION_PLACES;
  for (const panel of panels) {
    const section = panel.dataset.section;
    const label = panel.querySelector(".section-toggle").textContent.trim();
    const box = document.createElement("span");
    box.className = "section-move";
    const key = (kind, text, title) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset[kind] = "";
      button.textContent = text;
      button.title = `${label} ${title}`;
      button.setAttribute("aria-label", button.title);
      box.appendChild(button);
      return button;
    };
    const handle = key("sectionDrag", "⠿", "ziehen (an eine andere Stelle)");
    handle.addEventListener("pointerdown", event => startSectionDrag(panel, event));
    for (const [dir, text, title] of [["left", "◀", "weiter nach vorne"], ["right", "▶", "weiter nach hinten"]]) {
      const button = key("sectionMove", text, title);
      button.dataset.sectionMove = dir;
      button.addEventListener("click", () => moveSection(section, dir, button));
    }
    key("sectionWidth", "", "Breite").addEventListener("click", () => cycleSectionWidth(section));
    panel.querySelector(".panel-head").appendChild(box);
    // Opening or closing a section lets the others move into the space.
    panel.querySelector(".section-toggle").addEventListener("click", () => {
      SECTION_FREE_UNTIL = Math.max(SECTION_FREE_UNTIL, performance.now() + 600);
    });
  }
  applySectionOrder();
  // Heights change all the time (sections open and close, program rows are
  // added, the window is resized). The observer runs after layout and before
  // paint, so a section that grew never overlaps the next one for a frame.
  // Placing only moves sections, it does not resize them: no loop.
  const packer = new ResizeObserver(() => packSections());
  panels.forEach(panel => packer.observe(panel));
  const toggle = $("arrange-toggle");
  toggle.addEventListener("change", () => {
    document.body.classList.toggle("arranging", toggle.checked);
    $("arrange-reset-btn").hidden = !toggle.checked;
    $("arrange-hint").hidden = !toggle.checked;
    packSections();
  });
  $("arrange-reset-btn").addEventListener("click", () => {
    const before = [...SECTION_ORDER];
    const fresh = sectionArrangementFromPrefs({}, SECTION_DEFAULT_ORDER);
    SECTION_ORDER = fresh.order;
    SECTION_WIDTH = fresh.widths;
    SECTION_VIEW_WIDTH = {};
    SECTION_PLACES = {};
    SECTION_SAVED_PLACES = {};
    saveSectionArrangement();
    applySectionOrder(before, { animate: true });
  });
}

function saveSectionArrangement() {
  const p = loadUiPrefs();
  p.sectionArrangement = { order: SECTION_ORDER, widths: SECTION_WIDTH, viewWidths: SECTION_VIEW_WIDTH };
  p.sectionPlaces = SECTION_SAVED_PLACES;
  delete p.sectionRows;
  delete p.sectionOrder;
  saveUiPrefs(p);
}

// Put the sections in page order and in their grid places. With `before`
// (the order until now) only the sections that are out of order move in the
// page: all but the longest run of sections that kept their order (moving a
// section reloads the camera videos in it).
function applySectionOrder(before = null, { animate = false } = {}) {
  const order = SECTION_ORDER;
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
  packSections({ free: true, animate });
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

// Measure the sections and put them in their grid places (6 columns, 2 px
// rows); then the arrange keys. Widths first: a section's height depends on
// its width. Not on the stage; in the one-column view only measured.
// `free`: sections may change place (see the top of this part); `animate`:
// sections glide to their new places (arrange actions).
function packSections({ free = false, animate = false } = {}) {
  const page = document.querySelector(".page");
  if (!page) return;
  if (!STAGE.on) placeSections(page, free || performance.now() < SECTION_FREE_UNTIL, animate);
  syncSectionMoveButtons();
}

// A view in which a section has no saved place yet settles for
// SECTION_SETTLE_MS (once per page load and view: by the next time the
// contents are there); at the end it is packed once more and its places
// are saved.
const SECTION_SETTLED_VIEWS = new Set();
function sectionViewSettling(view) {
  const now = performance.now();
  if (view !== "one" && !SECTION_SETTLED_VIEWS.has(view)
      && sectionPlacesMissing(SECTION_ORDER, SECTION_SAVED_PLACES[view])) {
    SECTION_SETTLED_VIEWS.add(view);
    SECTION_SETTLE = { view, until: now + SECTION_SETTLE_MS };
    setTimeout(() => packSections(), SECTION_SETTLE_MS + 50);
  }
  return !!SECTION_SETTLE && SECTION_SETTLE.view === view && now < SECTION_SETTLE.until;
}

function placeSections(page, free, animate) {
  const panels = sectionPanelsInOrder();
  const view = currentSectionView();
  const rule = sectionPackRule({ dragging: !!SECTION_DRAG, free, settling: sectionViewSettling(view),
                                 arranging: document.body.classList.contains("arranging") });
  const firsts = animate && !stageReducedMotion() ? new Map(panels.map(p => [p, p.getBoundingClientRect()])) : null;
  // Measuring must not move the page: the rows stay as they are (the page
  // keeps its height, Safari keeps the scroll place) and every section is
  // put at the left edge with the width to measure; a definite column keeps
  // it in the 6 grid columns (overlapping is fine meanwhile). First at the
  // chosen widths: a section whose content is wider (or that sets
  // --min-section-width) gets the next wider width that holds it; then the
  // heights at the widths it gets. The scroll place is restored in case.
  const scrollTop = window.scrollY;
  const setSpans = min => panels.forEach(panel => {
    const width = sectionWidthIn(SECTION_WIDTH, SECTION_VIEW_WIDTH, view, panel.dataset.section);
    const span = sectionSpan(width, view, min[panel.dataset.section] || 0);
    panel.style.gridColumn = view === "one" ? "" : `1 / span ${span}`;
    if (view === "one") panel.style.gridRow = "";
  });
  setSpans({});
  SECTION_MIN_SPAN = view === "one" ? {} : sectionMinSpans(page, panels);
  if (Object.keys(SECTION_MIN_SPAN).length) setSpans(SECTION_MIN_SPAN);
  for (const panel of panels) {
    const margin = parseFloat(getComputedStyle(panel).marginTop) || 0;
    SECTION_HEIGHTS[panel.dataset.section] = Math.max(1, Math.ceil((panel.offsetHeight + margin) / SECTION_GRID_ROW_PX));
  }
  if (view === "one") return;
  const layout = sectionLayoutFor(SECTION_ORDER, view, { stick: rule.stick });
  for (const panel of panels) {
    const place = layout[panel.dataset.section];
    panel.style.gridColumn = `${place.start + 1} / span ${place.span}`;
    panel.style.gridRow = `${place.top + 1} / span ${place.bottom - place.top}`;
  }
  if (Math.abs(window.scrollY - scrollTop) > 1 && !SECTION_DRAG) window.scrollTo(window.scrollX, scrollTop);
  const places = Object.fromEntries(SECTION_ORDER.map(section => [section, layout[section].start]));
  SECTION_PLACES = { ...SECTION_PLACES, [view]: places };
  if (rule.save && JSON.stringify(places) !== JSON.stringify(SECTION_SAVED_PLACES[view] || {})) {
    SECTION_SAVED_PLACES = { ...SECTION_SAVED_PLACES, [view]: places };
    saveSectionArrangement();
  }
  if (firsts) glideSections(firsts);
}

// The grid columns each section's content needs (only those that need more
// than they have): content wider than the section (its scrollWidth), or the
// section's --min-section-width.
function sectionMinSpans(page, panels) {
  const style = getComputedStyle(page);
  const gap = parseFloat(style.columnGap) || 0;
  const inner = page.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0);
  const col = (inner - (SECTION_GRID_COLUMNS - 1) * gap) / SECTION_GRID_COLUMNS;
  const min = {};
  for (const panel of panels) {
    const declared = parseFloat(getComputedStyle(panel).getPropertyValue("--min-section-width")) || 0;
    const content = panel.scrollWidth > panel.clientWidth + 1 ? panel.scrollWidth + panel.offsetWidth - panel.clientWidth : 0;
    const span = sectionMinSpan(Math.max(declared, content), col, gap);
    if (span) min[panel.dataset.section] = span;
  }
  return min;
}

// FLIP: sections that changed place glide there from where they were.
function glideSections(firsts) {
  for (const [panel, first] of firsts) {
    if (SECTION_DRAG && panel === SECTION_DRAG.panel) continue;
    if (panel._glide) panel._glide.cancel();
    const last = panel.getBoundingClientRect();
    const dx = first.left - last.left, dy = first.top - last.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    panel._glide = panel.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
                                 { duration: 220, easing: "ease-out" });
  }
}

function moveSection(section, dir, button) {
  const view = currentSectionView();
  const order = sectionOrderAfterMove(SECTION_ORDER, section, dir,
                                      next => sectionLayoutFor(next, view, { drop: section }));
  if (!order) return;
  const before = SECTION_ORDER;
  SECTION_ORDER = order;
  forgetSectionPlace(section);
  applySectionOrder(before, { animate: true });
  const panel = sectionPanel(section);
  if (!STAGE.on && panel) panel.scrollIntoView({ block: "nearest" });
  if (button && !button.disabled) button.focus({ preventScroll: true });
}

// In the two-column view the key sets that view's own width (half or
// whole): the widths for wider screens are not touched.
function cycleSectionWidth(section) {
  const view = currentSectionView();
  const width = sectionWidthIn(SECTION_WIDTH, SECTION_VIEW_WIDTH, view, section);
  const next = nextSectionWidth(width, view, view === "one" ? 0 : SECTION_MIN_SPAN[section] || 0);
  if (!next) return;
  if (view === "two") {
    SECTION_VIEW_WIDTH = { ...SECTION_VIEW_WIDTH, two: { ...(SECTION_VIEW_WIDTH.two || {}), [section]: sectionSpan(next, "two") } };
  } else {
    SECTION_WIDTH = { ...SECTION_WIDTH, [section]: next };
  }
  forgetSectionPlace(section);
  saveSectionArrangement();
  packSections({ free: true, animate: true });
}

// A section the user moved or resized looks for its best place again (in
// every view; a view it has no place in settles when it is next shown).
function forgetSectionPlace(section) {
  const forget = all => Object.fromEntries(Object.entries(all).map(([view, places]) => {
    const rest = { ...places };
    delete rest[section];
    return [view, rest];
  }));
  SECTION_PLACES = forget(SECTION_PLACES);
  SECTION_SAVED_PLACES = forget(SECTION_SAVED_PLACES);
}

function syncSectionMoveButtons() {
  const view = currentSectionView();
  const layoutOf = section => next => sectionLayoutFor(next, view, { drop: section });
  for (const panel of sectionPanelsInOrder()) {
    const section = panel.dataset.section;
    for (const button of panel.querySelectorAll("[data-section-move]")) {
      button.disabled = !sectionOrderAfterMove(SECTION_ORDER, section, button.dataset.sectionMove, layoutOf(section));
    }
    const width = panel.querySelector("[data-section-width]");
    if (width) {
      // The width as it is on screen; when the content needs more than the
      // chosen one, the title says so (the choice is kept for wider screens).
      const min = view === "one" ? 0 : SECTION_MIN_SPAN[section] || 0;
      const chosen = sectionWidthIn(SECTION_WIDTH, SECTION_VIEW_WIDTH, view, section);
      const span = sectionSpan(chosen, view, min), next = nextSectionWidth(chosen, view, min);
      const widened = span !== sectionSpan(chosen, view);
      width.textContent = SECTION_WIDTH_LABELS[span];
      width.disabled = !next;
      width.classList.toggle("widened", widened);
      width.title = `${panel.querySelector(".section-toggle").textContent.trim()} Breite: ${SECTION_WIDTH_LABELS[span]}`
        + (widened ? ` (gewählt ${SECTION_WIDTH_LABELS[chosen]}, der Inhalt braucht hier mehr Platz)` : "")
        + (next ? `; antippen: ${SECTION_WIDTH_LABELS[sectionSpan(next, view, min)]}` : "");
      width.setAttribute("aria-label", width.title);
    }
    const handle = panel.querySelector("[data-section-drag]");
    if (handle) handle.hidden = view === "one";
  }
}

// ---- Dragging a section (finger or mouse) on its ⠿ key ----
// While dragging, the section follows the pointer and the others make room:
// over the upper half of another section it goes before it, over the lower
// half after it. Only grid places change meanwhile; the page order of the
// elements is set when the section is let go.
function startSectionDrag(panel, event) {
  if (SECTION_DRAG || currentSectionView() === "one" || (event.button !== undefined && event.button !== 0)) return;
  event.preventDefault();
  const handle = event.currentTarget, rect = panel.getBoundingClientRect();
  try { handle.setPointerCapture(event.pointerId); } catch (_) { /* events still reach the key */ }
  SECTION_DRAG = { panel, handle, pointerId: event.pointerId, before: [...SECTION_ORDER],
                   grabX: event.clientX - rect.left, grabY: event.clientY - rect.top, target: null };
  panel.classList.add("section-dragging");
  // Let go (pointerup), taken over by the system (pointercancel) or the key
  // lost the pointer some other way (lostpointercapture, e.g. it was hidden):
  // the drag always ends; only a real let-go keeps the new order.
  const kinds = ["pointerup", "pointercancel", "lostpointercapture"];
  const move = e => { if (SECTION_DRAG && e.pointerId === SECTION_DRAG.pointerId) dragSectionTo(e.clientX, e.clientY); };
  const end = e => {
    if (!SECTION_DRAG || e.pointerId !== SECTION_DRAG.pointerId) return;
    handle.removeEventListener("pointermove", move);
    kinds.forEach(kind => handle.removeEventListener(kind, end));
    endSectionDrag(e.type !== "pointerup");
  };
  handle.addEventListener("pointermove", move);
  kinds.forEach(kind => handle.addEventListener(kind, end));
}

function dragSectionTo(x, y) {
  const drag = SECTION_DRAG, section = drag.panel.dataset.section;
  // Scroll along near the window's top and bottom edge.
  if (y < 60) window.scrollBy(0, -16); else if (y > innerHeight - 60) window.scrollBy(0, 16);
  const over = document.elementsFromPoint(x, y).map(el => el.closest(".page > .panel[data-section]"))
    .find(p => p && p !== drag.panel);
  if (over) {
    const r = over.getBoundingClientRect(), after = y > r.top + r.height / 2;
    const key = `${over.dataset.section}:${after}`;
    if (key !== drag.target) {
      drag.target = key;
      const order = SECTION_ORDER.filter(s => s !== section);
      order.splice(order.indexOf(over.dataset.section) + (after ? 1 : 0), 0, section);
      if (order.join() !== SECTION_ORDER.join()) {
        SECTION_ORDER = order;
        forgetSectionPlace(section);
        drag.panel.style.transform = "";
        packSections({ free: true, animate: true });
      }
    }
  }
  drag.panel.style.transform = "";
  const home = drag.panel.getBoundingClientRect();
  drag.panel.style.transform = `translate(${x - drag.grabX - home.left}px, ${y - drag.grabY - home.top}px)`;
}

// Let go: the new order is kept; a cancelled drag (the system took over the
// touch, the key lost the pointer) puts the old one back.
function endSectionDrag(cancelled = false) {
  const drag = SECTION_DRAG;
  SECTION_DRAG = null;
  const first = drag.panel.getBoundingClientRect();
  drag.panel.style.transform = "";
  drag.panel.classList.remove("section-dragging");
  if (cancelled && SECTION_ORDER.join() !== drag.before.join()) {
    SECTION_ORDER = drag.before;
    saveSectionArrangement();  // placing may have saved the dragged order meanwhile
    packSections({ free: true, animate: true });
  } else if (SECTION_ORDER.join() !== drag.before.join()) {
    saveSectionArrangement();
    applySectionOrder(drag.before);
  }
  glideSections(new Map([[drag.panel, first]]));
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
