// Mixer Mute Service UI (doc-dock.js): page keys for a document at the running program row.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Document dock ------------------------------------------------------------
// When the running program row opens a document (PDF, PowerPoint, Word: its
// Übersetzung, Playback or Liedtext, or a desktop profile) and a display
// agent shows it page by page (app/documents.py), a slim bar sits at the
// bottom of that row: back, "Übersetzung · Seite 3/12", "Weiter". The song
// dock (song-dock.js) hangs from the row's top edge, so both can be there at
// once, the song on top. Its keys read "‹ Seite" / "Seite ›" in a calm blue,
// never like the song's green "Weiter". The page number comes from GET /documents/current,
// asked again every few seconds while the bar is there (a presenter on the
// PC also turns pages); a key's answer brings the new page at once.
//
// A row opens a document when one of its Desktop steps (or, in older rows,
// Übersetzung / Playback / Liedtext) points at a PDF, PowerPoint or Word file:
// its own path or its Desktop key's. Titles and notes never count.
//
// Hooks: renderProgramItems() (program.js) calls renderDocDock(); it reads
// PROGRAM_ITEMS, PROGRAM_STATUS and DESKTOP_PROFILES.

const DOC_DOCK = { cell: null, rowId: null, state: null, timer: null, asking: false, busy: false, armedAt: 0 };
const DOC_DOCK_POLL_MS = 4000;
// Taps on a bar that just appeared belong to what was there (keep in sync with css/doc-dock.css).
const DOC_DOCK_GUARD_MS = 600;
// Keep in sync with app/documents.py DOCUMENT_SUFFIXES.
const DOC_DOCK_SUFFIX_RE = /\.(pdf|pptx?|pptm|ppsx?|ppsm|odp|docx?|docm|odt|rtf)(?![A-Za-z0-9])/i;
// Older rows: a file in Übersetzung / Playback / Liedtext.
const DOC_DOCK_FIELDS = [["translation", "Übersetzung"], ["playback", null], ["lyrics", null]];

const ICON_DOC_PREV = `<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>`;
const ICON_DOC_NEXT = `<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg>`;

function docDockProfile(id) {
  return (typeof DESKTOP_PROFILES !== "undefined" ? DESKTOP_PROFILES : []).find(p => p.id === id) || null;
}

// The document a step or field value opens: {path, label} or null. Values:
// "<profile id>" (a Desktop key), "via:<profile>|<path>" (that key with
// another file, app/row_files.py), "desktop:<profile>", "file:<ip>|<path>".
// Only paths and keys count, never a row's title or notes.
function docDockValue(value, fieldLabel) {
  if (typeof value !== "string" || !value || value.startsWith("@") || value.startsWith("key:")) return null;
  let profileId = null;
  let path = null;
  if (value.startsWith("via:")) {
    const bar = value.indexOf("|");
    profileId = value.slice(4, bar);
    path = value.slice(bar + 1);
  } else if (value.startsWith("file:")) {
    path = value.slice(value.indexOf("|") + 1);
  } else {
    profileId = value.startsWith("desktop:") ? value.slice(8) : value;
  }
  const profile = profileId ? docDockProfile(profileId) : null;
  if (path === null) path = profile ? String(profile.path || "") : "";
  if (!DOC_DOCK_SUFFIX_RE.test(path)) return null;
  return { path, label: fieldLabel || (profile && profile.title) || "Dokument" };
}

// Whether a program row opens a document; its label (the field's name, or
// the Desktop key's title).
function docDockLabel(item) {
  for (const [field, label] of DOC_DOCK_FIELDS) {
    const found = docDockValue(item[field], label);
    if (found) return found.label;
  }
  for (const step of item.desktop || []) {
    const found = docDockValue(step, null);
    if (found) return found.label;
  }
  return null;
}

// The running row, if it opens a document: {item, row, label}.
function docDockTarget() {
  const id = PROGRAM_STATUS.current_id;
  if (!id) return null;
  const item = PROGRAM_ITEMS.find(entry => entry.id === id);
  const label = item ? docDockLabel(item) : null;
  if (!label) return null;
  const row = $("program-items").querySelector(`tr[data-program-id="${CSS.escape(id)}"]`);
  return row ? { item, row, label } : null;
}

function buildDocDock() {
  const cell = document.createElement("td");
  cell.className = "doc-dock";
  cell.innerHTML = `<div class="doc-dock-bar" role="group" aria-label="Dokument am laufenden Programmpunkt">`
    + `<button type="button" class="dd-prev" data-doc-dock="prev" title="Vorherige Seite" aria-label="Vorherige Seite">${ICON_DOC_PREV}<span class="dd-key-text">Seite</span></button>`
    + `<div class="dd-info"><span class="dd-label"></span><span class="dd-title"></span></div>`
    + `<span class="dd-pos" aria-live="polite"></span>`
    + `<button type="button" class="dd-next" data-doc-dock="next" title="Nächste Seite" aria-label="Nächste Seite"><span class="dd-key-text">Seite</span>${ICON_DOC_NEXT}</button>`
    + `</div>`;
  return cell;
}

function closeDocDock() {
  if (DOC_DOCK.cell) DOC_DOCK.cell.remove();
  DOC_DOCK.cell = null;
}

function stopDocDockPoll() {
  clearInterval(DOC_DOCK.timer);
  DOC_DOCK.timer = null;
  DOC_DOCK.rowId = null;
  DOC_DOCK.state = null;
}

async function askDocDock() {
  if (DOC_DOCK.asking || document.hidden) return;
  DOC_DOCK.asking = true;
  const rowId = DOC_DOCK.rowId;
  try {
    const r = await api("GET", "/documents/current");
    if (DOC_DOCK.rowId === rowId && !DOC_DOCK.busy) DOC_DOCK.state = r.ok ? r.data : null;
  } finally {
    DOC_DOCK.asking = false;
  }
  renderDocDock();
}

function renderDocDock() {
  const target = docDockTarget();
  if (!target) { closeDocDock(); stopDocDockPoll(); return; }
  if (DOC_DOCK.rowId !== target.item.id) {
    // Another running row: ask what is shown now (its document may still be on its way).
    DOC_DOCK.rowId = target.item.id;
    DOC_DOCK.state = null;
    clearInterval(DOC_DOCK.timer);
    DOC_DOCK.timer = setInterval(askDocDock, DOC_DOCK_POLL_MS);
    setTimeout(askDocDock, 300);
  }
  const state = DOC_DOCK.state;
  if (!state || !state.shown || !state.pages) { closeDocDock(); return; }
  let cell = DOC_DOCK.cell;
  if (!cell || cell.parentElement !== target.row) {
    closeDocDock();
    cell = DOC_DOCK.cell = buildDocDock();
    target.row.appendChild(cell);
    DOC_DOCK.armedAt = performance.now();
    cell.classList.add("arming");
    setTimeout(() => cell.classList.remove("arming"), DOC_DOCK_GUARD_MS);
  }
  const page = Math.max(1, state.page || 1);
  cell.querySelector(".dd-label").textContent = target.label;
  cell.querySelector(".dd-title").textContent = state.title || "";
  cell.querySelector(".dd-title").title = state.title || "";
  cell.querySelector(".dd-pos").textContent = `Seite ${page}/${state.pages}`;
  cell.classList.toggle("busy", DOC_DOCK.busy);
  cell.querySelector(".dd-prev").disabled = DOC_DOCK.busy || page <= 1;
  cell.querySelector(".dd-next").disabled = DOC_DOCK.busy || page >= state.pages;
}

async function docDockStep(action) {
  const state = DOC_DOCK.state;
  if (!state || DOC_DOCK.busy) return;
  DOC_DOCK.busy = true;
  renderDocDock();
  try {
    const r = await api("POST", `/documents/${action}`, state.pc ? { pc: state.pc } : {});
    if (r.ok && r.data.ok !== false) {
      DOC_DOCK.state = r.data;
    } else {
      toast(`Seite: ${r.data.reason || `HTTP ${r.status}`}`, false);
      if (r.data.shown === false) DOC_DOCK.state = null;
    }
  } finally {
    DOC_DOCK.busy = false;
    renderDocDock();
  }
}

$("program-items").addEventListener("click", event => {
  const button = event.target.closest("[data-doc-dock]");
  if (!button || !button.closest(".doc-dock")) return;
  if (performance.now() - DOC_DOCK.armedAt < DOC_DOCK_GUARD_MS) return;   // just appeared
  docDockStep(button.dataset.docDock);
});

// Back on the page (iPad woke up, tab shown again): ask at once.
document.addEventListener("visibilitychange", () => { if (!document.hidden && DOC_DOCK.rowId) askDocDock(); });
