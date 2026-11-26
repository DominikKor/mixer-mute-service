// Mixer Mute Service UI (song-dock.js): verse keys docked at the running program row.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Song dock ----------------------------------------------------------------
// When the running program row shows a song, a drawer slides out of the
// row's top edge, above its summary line: on the left a small preview of what
// the screens show (renderSlide from songs.js, also black or "Aus"), on the
// right back, a chip per slide (tap to jump), Aus, Schwarz and the big
// "Weiter", under them the first line of the next slide. It drives the same
// song as the Liedanzeige section, through the same calls (songAction in
// songs.js), and is redrawn from the same state (SONG_LIVE), so both always
// show the same. The presenter keys (Page Down/Up, arrows, B) work as before.
//
// States: the row's song is on screen (or black within it) -> the keys;
// another song or none on screen -> the preview and a line with "Zeigen"
// (shows the row's song from its first slide); the row is no longer running
// or has no song -> the drawer slides back in and is removed.
//
// Placement: the drawer is absolutely placed at the top of the row (inside
// its border) and the row's top padding grows by the drawer's height
// (--sd-h on the row, both animated), so the row's own grid stays untouched.
//
// Hooks: renderProgramItems() (program.js) and renderSongLive() (songs.js)
// call renderSongDock(); it only reads PROGRAM_ITEMS, PROGRAM_STATUS,
// SONG_LIVE, SONG_SETTINGS and SONG_BUSY.

const SONG_DOCK = { cell: null, chipsSig: "", observer: null };
const SONG_DOCK_MS = 260;   // slide out / in (keep in sync with css/song-dock.css)

const ICON_DOCK_PREV = `<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>`;
const ICON_DOCK_NEXT = `<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg>`;
const ICON_DOCK_NOTE = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>`;

// The running row, if it has a song: {item, row}.
function songDockTarget() {
  const id = PROGRAM_STATUS.current_id;
  if (!id) return null;
  const item = PROGRAM_ITEMS.find(entry => entry.id === id);
  if (!item || !item.song) return null;
  const row = $("program-items").querySelector(`tr[data-program-id="${CSS.escape(id)}"]`);
  return row ? { item, row } : null;
}

function buildSongDock() {
  const cell = document.createElement("td");
  cell.className = "song-dock";
  cell.innerHTML = `<div class="song-dock-drawer" role="group" aria-label="Lied am laufenden Programmpunkt">`
    + `<div class="slide-box sd-preview" role="img" aria-label="Was die Bildschirme zeigen"></div>`
    + `<div class="sd-main">`
    + `<div class="song-dock-bar">`
    + `<button type="button" class="sd-prev" data-song-dock="prev" title="Vorherige Folie (Bild auf, ←)" aria-label="Vorherige Folie">${ICON_DOCK_PREV}</button>`
    + `<div class="sd-mid"><div class="sd-chips" role="group" aria-label="Folien"></div></div>`
    + `<span class="sd-status"><button type="button" class="sd-warn" data-song-dock="warn" hidden>⚠</button><span class="sd-pos" aria-live="polite"></span></span>`
    // "Aus" (closes the lyrics everywhere) away from "Weiter": "Schwarz" sits between them.
    + `<button type="button" class="sd-hide danger" data-song-dock="hide" title="Liedfenster schließen (auf allen Bildschirmen)">Aus</button>`
    + `<button type="button" class="sd-blank" data-song-dock="blank" title="Bildschirm schwarz / wieder an (Taste B oder .)" aria-pressed="false">Schwarz</button>`
    + `<button type="button" class="sd-next program-play" data-song-dock="next" title="Nächste Folie (Bild ab, →, Leertaste); nach der letzten: schwarz"><span class="sd-next-text">Weiter</span>${ICON_DOCK_NEXT}</button>`
    + `</div>`
    + `<div class="sd-next-line"><span class="sd-next-label">Als Nächstes</span><span class="sd-line"></span></div>`
    + `<div class="song-dock-off"><span class="sd-note">${ICON_DOCK_NOTE}</span><span class="sd-off-text"></span>`
    + `<button type="button" class="sd-show" data-song-dock="show" title="Das Lied dieses Programmpunkts ab der ersten Folie zeigen">Zeigen</button></div>`
    + `</div></div>`;
  const strip = cell.querySelector(".sd-chips");
  strip.addEventListener("scroll", () => songDockStripEdges(strip), { passive: true });
  // A mouse wheel scrolls hidden chips into view (fingers and trackpads swipe).
  strip.addEventListener("wheel", event => {
    const max = strip.scrollWidth - strip.clientWidth;
    if (max <= 1 || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
    if ((event.deltaY < 0 && strip.scrollLeft <= 0) || (event.deltaY > 0 && strip.scrollLeft >= max - 1)) return;
    event.preventDefault();
    strip.scrollLeft += event.deltaY;
  }, { passive: false });
  new ResizeObserver(() => songDockStripEdges(strip)).observe(strip);
  new ResizeObserver(() => { if (cell.isConnected) renderSongDockPreview(cell); }).observe(cell.querySelector(".sd-preview"));
  return cell;
}

// The page scrolls in normal mode: a drawer opening or closing above what is
// on screen (also partly, e.g. under the header) would push the view. Then
// it changes at once and the page scrolls by exactly the height it gained or
// lost, in the same frame, with the browser's own scroll anchoring off for
// that moment (else both would move it). The same in every browser, with or
// without scroll anchoring (Safari).
function songDockAboveView(y) {
  if (STAGE.on) return false;
  const header = document.querySelector("header");
  return y < (header ? header.getBoundingClientRect().bottom : 0);
}

function songDockKeepView(row, change) {
  const root = document.scrollingElement || document.documentElement;
  root.style.overflowAnchor = "none";
  const before = row.getBoundingClientRect().height;
  change();
  const grown = row.getBoundingClientRect().height - before;
  if (grown) window.scrollBy(0, grown);
  requestAnimationFrame(() => requestAnimationFrame(() => { root.style.overflowAnchor = ""; }));
}

// The drawer's own height (it is not squeezed by the cell, which clips it);
// the layout height, not the drawn one (the stage scales sections while they
// fly in and out).
function songDockHeight(cell) {
  return cell.querySelector(".song-dock-drawer").offsetHeight;
}

function setSongDockHeight(row, px) {
  row.style.setProperty("--sd-h", `${px}px`);
}

// Slide out of the row's top edge: laid out closed first, so the height animates.
function openSongDock(row) {
  const cell = buildSongDock();
  SONG_DOCK.cell = cell;
  SONG_DOCK.chipsSig = "";
  const instant = songDockAboveView(row.getBoundingClientRect().top);
  const place = () => {
    // The row's own top padding (read before it is docked; a drawer still
    // sliding in on this row keeps the value it read).
    if (!row.classList.contains("song-docked")) row.style.setProperty("--sd-pad", getComputedStyle(row).paddingTop);
    setSongDockHeight(row, 0);
    row.appendChild(cell);
    row.classList.add("song-docked");
    void cell.getBoundingClientRect();
    setSongDockHeight(row, songDockHeight(cell));
  };
  if (instant) {
    row.classList.add("sd-instant");
    songDockKeepView(row, place);
    requestAnimationFrame(() => row.classList.remove("sd-instant"));
  } else {
    place();
  }
  // The drawer's height follows its content (other state, narrower screen).
  if (SONG_DOCK.observer) SONG_DOCK.observer.disconnect();
  SONG_DOCK.observer = new ResizeObserver(() => {
    if (SONG_DOCK.cell === cell && cell.parentElement === row) setSongDockHeight(row, songDockHeight(cell));
  });
  SONG_DOCK.observer.observe(cell.querySelector(".song-dock-drawer"));
  return cell;
}

// Slide back into the row's top edge, then leave the row.
function closeSongDock() {
  const cell = SONG_DOCK.cell;
  if (!cell) return;
  SONG_DOCK.cell = null;
  if (SONG_DOCK.observer) { SONG_DOCK.observer.disconnect(); SONG_DOCK.observer = null; }
  const row = cell.parentElement;
  const remove = () => {
    cell.remove();
    if (row && !row.querySelector("td.song-dock")) {
      row.classList.remove("song-docked");
      row.style.removeProperty("--sd-h");
      row.style.removeProperty("--sd-pad");
    }
  };
  cell.querySelectorAll("button").forEach(button => { button.disabled = true; });
  if (!row || !row.isConnected) { remove(); return; }
  if (songDockAboveView(cell.getBoundingClientRect().top)) {
    row.classList.add("sd-instant");
    songDockKeepView(row, remove);
    requestAnimationFrame(() => row.classList.remove("sd-instant"));
    return;
  }
  setSongDockHeight(row, 0);
  if (stageReducedMotion()) remove();
  else setTimeout(() => { if (cell.isConnected) remove(); }, SONG_DOCK_MS + 40);
}

// Short chip text: the slide's key (1, 2, Ref, …), "T" for the title slide.
function songDockChipText(slide, index) {
  if (slide.title_slide) return "T";
  const key = String(slide.key || "").trim();
  if (!key) return String(index + 1);
  if (/^(ref|refrain|chorus|r)$/i.test(key)) return "R";
  return key.length > 3 ? key.slice(0, 3) : key;
}

function songDockFirstLine(slide) {
  return String((slide && slide.text) || "").split("\n").map(line => line.trim()).find(Boolean) || "";
}

function renderSongDockChips(cell, song, slides) {
  const strip = cell.querySelector(".sd-chips");
  const sig = JSON.stringify([song.id, slides.map(slide => [slide.key, slide.title_slide || false])]);
  if (SONG_DOCK.chipsSig === sig) return strip;
  SONG_DOCK.chipsSig = sig;
  strip.replaceChildren(...slides.map((slide, i) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "sd-chip";
    chip.dataset.songDock = "slide";
    chip.dataset.slide = String(i);
    chip.textContent = songDockChipText(slide, i);
    const line = songDockFirstLine(slide);
    chip.title = `Folie ${i + 1}${slide.title_slide ? " · Titel" : slide.key ? " · " + slide.key : ""}${line ? ": " + line : ""}`;
    chip.setAttribute("aria-label", chip.title);
    return chip;
  }));
  return strip;
}

// Hidden chips: the strip fades out on the side where more are.
function songDockStripEdges(strip) {
  const max = strip.scrollWidth - strip.clientWidth;
  strip.classList.toggle("more-left", max > 1 && strip.scrollLeft > 1);
  strip.classList.toggle("more-right", max > 1 && strip.scrollLeft < max - 1);
}

// Keep the current chip in view inside its strip (never scrolls the page).
function songDockScrollChip(strip, chip) {
  songDockStripEdges(strip);
  if (!chip || strip.scrollWidth <= strip.clientWidth) return;
  const left = chip.offsetLeft - strip.offsetLeft;
  if (left < strip.scrollLeft || left + chip.offsetWidth > strip.scrollLeft + strip.clientWidth) {
    strip.scrollTo({ left: Math.max(0, left - (strip.clientWidth - chip.offsetWidth) / 2), behavior: stageReducedMotion() ? "auto" : "smooth" });
  }
}

// The preview: what the screens show (this song, another one, black or
// nothing), drawn like the Liedanzeige draws it (renderSlide, songs.js).
function renderSongDockPreview(cell) {
  const box = cell.querySelector(".sd-preview");
  const song = SONG_LIVE.song;
  const state = !song ? "off" : SONG_LIVE.blank ? "blank" : "slide";
  box.classList.toggle("off", state === "off");
  box.classList.toggle("blank", state === "blank");
  box.setAttribute("aria-label", state === "off" ? "Bildschirme: kein Lied" : state === "blank" ? "Bildschirme: schwarz"
    : `Bildschirme: ${song.title}, Folie ${SONG_LIVE.slide + 1}`);
  if (!SONG_SETTINGS || !box.clientWidth) return;
  const sig = JSON.stringify([state, song && song.id, SONG_LIVE.slide, SONG_SETTINGS, box.clientWidth, box.clientHeight]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  renderSlide(box, state === "slide" ? songSlide(song, SONG_LIVE.slide, SONG_SETTINGS) : { blank: true }, SONG_SETTINGS);
}

function renderSongDock() {
  const target = songDockTarget();
  if (!target) { closeSongDock(); return; }
  let cell = SONG_DOCK.cell;
  if (cell && cell.parentElement !== target.row) { closeSongDock(); cell = null; }
  if (!cell) cell = openSongDock(target.row);

  const live = SONG_LIVE.song && SONG_LIVE.song_id === target.item.song ? SONG_LIVE.song : null;
  cell.classList.toggle("live", !!live);
  cell.classList.toggle("busy", SONG_BUSY);   // keys wait for the last press (no grey flash)
  if (!live) {
    const known = SONG_INDEX.get(target.item.song);
    const title = known ? known.title : target.item.song.replace(/\.(foil|pptx)$/i, "");
    cell.querySelector(".sd-off-text").textContent = known
      ? `${title} – nicht auf dem Bildschirm` : `${title} – nicht in der Liedbibliothek`;
    const show = cell.querySelector(".sd-show");
    show.hidden = !known;
    show.disabled = SONG_BUSY;
    renderSongDockPreview(cell);
    return;
  }

  const slides = songDisplaySlides(live, SONG_SETTINGS || {});
  const last = slides.length - 1;
  const index = Math.min(Math.max(0, SONG_LIVE.slide), last);
  const blank = !!SONG_LIVE.blank;
  const ended = blank && index >= last;
  const strip = renderSongDockChips(cell, live, slides);
  let current = null;
  strip.querySelectorAll(".sd-chip").forEach((chip, i) => {
    const on = !blank && i === index;
    chip.classList.toggle("current", on);
    chip.setAttribute("aria-current", on ? "true" : "false");
    if (on) current = chip;
  });
  songDockScrollChip(strip, current);
  // Under the keys: what "Weiter" brings next.
  cell.querySelector(".sd-line").textContent = ended ? "Ende – der Bildschirm bleibt schwarz"
    : blank ? `wieder Folie ${index + 1}: ${songDockFirstLine(slides[index])}`
    : index >= last ? "schwarz (Ende des Lieds)" : songDockFirstLine(slides[index + 1]);
  renderSongDockPreview(cell);
  cell.querySelector(".sd-pos").textContent = `${index + 1}/${slides.length}`;
  // A screen that did not take the last step (song-screens.js): tap for which and why.
  const failed = songScreensFailedText(SONG_LIVE.screens);
  const warn = cell.querySelector(".sd-warn");
  warn.hidden = !failed;
  warn.title = failed ? `Nicht überall gezeigt: ${failed}` : "";
  warn.setAttribute("aria-label", warn.title || "Alle Bildschirme zeigen das Lied");
  const blankKey = cell.querySelector(".sd-blank");
  blankKey.classList.toggle("on", blank);
  blankKey.setAttribute("aria-pressed", String(blank));
  const next = cell.querySelector(".sd-next");
  next.querySelector(".sd-next-text").textContent = ended ? "Ende" : "Weiter";
  next.setAttribute("aria-label", ended ? "Lied zu Ende" : "Nächste Folie");
  cell.querySelector(".sd-prev").disabled = SONG_BUSY || (!blank && index === 0);
  next.disabled = SONG_BUSY || ended;
  blankKey.disabled = SONG_BUSY;
  cell.querySelector(".sd-hide").disabled = SONG_BUSY;
  strip.querySelectorAll(".sd-chip").forEach(chip => { chip.disabled = SONG_BUSY; });
}

$("program-items").addEventListener("click", event => {
  const button = event.target.closest("[data-song-dock]");
  if (!button || !button.closest(".song-dock")) return;
  const target = songDockTarget();
  if (!target) return;
  const action = button.dataset.songDock;
  if (action === "warn") { toast(button.title, false); return; }
  if (action === "show") songAction("/songs/show", { song_id: target.item.song, slide: 0 });
  else if (SONG_LIVE.song_id !== target.item.song) return;   // another song took the screen meanwhile
  else if (action === "prev") songPrev();
  else if (action === "next") songNext();
  else if (action === "blank") songBlankToggle();
  else if (action === "hide") songAction("/songs/hide");
  else if (action === "slide") songAction("/songs/show", { song_id: target.item.song, slide: Number(button.dataset.slide) });
});
