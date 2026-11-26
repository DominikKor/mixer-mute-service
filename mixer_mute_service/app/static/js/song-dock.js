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

const SONG_DOCK = { cell: null, chipsSig: "", observer: null, played: null, armedAt: 0, live: null };
const SONG_DOCK_MS = 260;   // slide out / in (keep in sync with css/song-dock.css)
// The dock's keys take no taps this long after they appear where something
// else was (a second tap on PLAY, on "Zeigen" or a quick double tap must not
// blank the screen or skip a verse). Keep in sync with css/song-dock.css.
const SONG_DOCK_GUARD_MS = 600;
// Longer when the drawer had to push the PLAY key just tapped away (a stage
// whose program fits and so cannot scroll): a re-tap there lands on the drawer.
const SONG_DOCK_GUARD_PUSHED_MS = 1500;

// Where the drawer could not keep PLAY in place (a stage whose program fits
// and so cannot scroll), its keys that now cover PLAY's old place stay
// inactive -- marked, with a note -- until the next tap somewhere else: a
// re-tap "on PLAY", however late, never blanks or steps the song.
function songDockOverlaps(a, b) {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

function shieldSongDock(rect) {
  const pad = 6;   // a finger lands a little off
  SONG_DOCK.shield = rect ? { left: rect.left - pad, top: rect.top - pad, right: rect.right + pad, bottom: rect.bottom + pad } : null;
  if (SONG_DOCK.cell) applySongDockShield(SONG_DOCK.cell);
}

function applySongDockShield(cell) {
  const shield = SONG_DOCK.shield;
  let any = false;
  cell.querySelectorAll(".sd-main button").forEach(button => {
    const on = !!shield && !!button.offsetParent && songDockOverlaps(button.getBoundingClientRect(), shield);
    button.classList.toggle("sd-shield", on);
    any = any || on;
  });
  cell.classList.toggle("shielded", any);
}

// A tap anywhere else lifts it; taps where PLAY was (also between the keys)
// and on a marked key do not.
document.addEventListener("pointerdown", event => {
  const shield = SONG_DOCK.shield;
  if (!shield) return;
  const point = { left: event.clientX, right: event.clientX + 1, top: event.clientY, bottom: event.clientY + 1 };
  if (songDockOverlaps(point, shield) || (event.target.closest && event.target.closest(".sd-shield"))) return;
  shieldSongDock(null);
}, true);
// PLAY tapped on a row this long ago still counts as "just played here".
const SONG_DOCK_PLAYED_MS = 15000;

function songDockArmed(armedAt, now, guardMs = SONG_DOCK_GUARD_MS) {
  return now - armedAt >= guardMs;
}

// The keys appear (or change): no taps for a moment, they fade in meanwhile.
function armSongDock(cell, guardMs = SONG_DOCK_GUARD_MS) {
  SONG_DOCK.armedAt = performance.now();
  SONG_DOCK.guardMs = guardMs;
  cell.classList.remove("arming");
  void cell.offsetWidth;
  cell.classList.add("arming");
  clearTimeout(SONG_DOCK.armTimer);
  SONG_DOCK.armTimer = setTimeout(() => cell.classList.remove("arming"), guardMs);
}

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
    + `<div class="sd-next-line"><span class="sd-next-label">Nächste Folie</span><span class="sd-line"></span></div>`
    + `<div class="sd-shield-note" role="status">Hier lag PLAY – die markierte Taste geht erst nach einem Tipp woanders</div>`
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

// What scrolls the program: the stage (on the stage) or the page.
function songDockScroller(el) {
  return el.closest("#stage-main") || document.scrollingElement || document.documentElement;
}

// The page always scrolls; the stage only when its section is taller than it
// (else scrolling would hide the top of the program for good, and making it
// scroll would narrow the section for the finger strip).
function songDockCanScroll(scroller) {
  return scroller.id !== "stage-main" || scroller.classList.contains("scroll");
}

// Scroll by px; at the end of the content the program list first gets the
// room it needs (kept while a drawer is open).
function songDockScrollBy(scroller, px) {
  if (!songDockCanScroll(scroller)) return;
  if (px > 0) {
    const free = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop;
    if (free < px) {
      const wrap = $("program-items").closest(".program-table-wrap");
      wrap.style.paddingBottom = `${(parseFloat(wrap.style.paddingBottom) || 0) + Math.ceil(px - free)}px`;
    }
  }
  scroller.scrollTop += px;
}

function songDockClearRoom() {
  $("program-items").closest(".program-table-wrap").style.paddingBottom = "";
}

// Run change() with the browser's own scroll anchoring off (else both would move it).
function songDockNoAnchoring(scroller, change) {
  const elements = [...new Set([document.scrollingElement || document.documentElement, scroller])];
  elements.forEach(el => { el.style.overflowAnchor = "none"; });
  change();
  requestAnimationFrame(() => requestAnimationFrame(() => elements.forEach(el => { el.style.overflowAnchor = ""; })));
}

// A drawer changing above the view: scroll by the height the row gained or lost.
function songDockKeepView(row, change) {
  const scroller = songDockScroller(row);
  songDockNoAnchoring(scroller, () => {
    const before = row.offsetHeight;
    change();
    const grown = row.offsetHeight - before;
    if (grown) songDockScrollBy(scroller, grown);
  });
}

// Keep one element (the PLAY key just tapped) where it is on screen; false
// if it could not stay (a stage that does not scroll).
function songDockKeepElement(el, change) {
  const scroller = songDockScroller(el);
  let kept = true;
  songDockNoAnchoring(scroller, () => {
    const before = el.getBoundingClientRect().top;
    change();
    const moved = el.getBoundingClientRect().top - before;
    if (moved) songDockScrollBy(scroller, moved);
    kept = Math.abs(el.getBoundingClientRect().top - before) < 2;
  });
  return kept;
}

// The row's PLAY key if it was tapped on this page just now (the drawer then
// opens above the row and the row, with the finger on PLAY, stays put).
function songDockPlayedKey(row) {
  const played = SONG_DOCK.played;
  if (!played || played.id !== row.dataset.programId || performance.now() - played.at > SONG_DOCK_PLAYED_MS) return null;
  SONG_DOCK.played = null;
  const key = row.querySelector('[data-program-action="play"]');
  return key && key.isConnected ? key : null;
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
function openSongDock(row, keptByCaller = false) {
  const cell = buildSongDock();
  SONG_DOCK.cell = cell;
  SONG_DOCK.chipsSig = "";
  SONG_DOCK.live = null;
  armSongDock(cell);
  const above = !keptByCaller && songDockAboveView(row.getBoundingClientRect().top);
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
  if (keptByCaller || above) {
    row.classList.add("sd-instant");
    if (above) songDockKeepView(row, place);
    else place();
    requestAnimationFrame(() => requestAnimationFrame(() => row.classList.remove("sd-instant")));
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
function closeSongDock(instant = false) {
  const cell = SONG_DOCK.cell;
  if (!cell) return;
  SONG_DOCK.cell = null;
  SONG_DOCK.shield = null;
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
  if (!row || !row.isConnected || instant) { remove(); return; }
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
  const state = SONG_LIVE.blank ? "blank" : !song ? "off" : "slide";   // black also without a song
  box.classList.toggle("off", state === "off");
  box.classList.toggle("blank", state === "blank");
  box.setAttribute("aria-label", state === "off" ? "Bildschirme: kein Lied" : state === "blank" ? "Bildschirme: schwarz"
    : `Bildschirme: ${song.title}, Folie ${SONG_LIVE.slide + 1}`);
  if (!SONG_SETTINGS || !box.clientWidth) return;
  // layout_rev: the service laid the song out anew (slide sizes, splitting).
  const sig = JSON.stringify([state, song && song.id, song && song.layout_rev, SONG_LIVE.slide, SONG_SETTINGS,
    box.clientWidth, box.clientHeight]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  renderSlide(box, state === "slide" ? songSlide(song, SONG_LIVE.slide, SONG_SETTINGS) : { blank: true }, SONG_SETTINGS);
}

function renderSongDock() {
  const target = songDockTarget();
  if (!target) { closeSongDock(); songDockClearRoom(); return; }
  let cell = SONG_DOCK.cell;
  if (!cell || cell.parentElement !== target.row) {
    const play = songDockPlayedKey(target.row);
    if (play) {
      // Played here: the old drawer goes and the new one comes at once, and
      // the page scrolls so PLAY stays under the finger (the drawer appears
      // above it, never under a second tap).
      const at = play.getBoundingClientRect();
      const kept = songDockKeepElement(play, () => { closeSongDock(true); cell = openSongDock(target.row, true); });
      shieldSongDock(kept ? null : at);
      if (!kept) armSongDock(cell, SONG_DOCK_GUARD_PUSHED_MS);
    } else {
      if (cell) closeSongDock();
      cell = openSongDock(target.row);
    }
  }

  const live = SONG_LIVE.song && SONG_LIVE.song_id === target.item.song ? SONG_LIVE.song : null;
  // The keys and the "Zeigen" line take turns at the same place: no taps for a moment.
  if (SONG_DOCK.live !== null && SONG_DOCK.live !== !!live) armSongDock(cell);
  SONG_DOCK.live = !!live;
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
    applySongDockShield(cell);
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
  applySongDockShield(cell);
}

// PLAY on a row (before program.js starts it): remember it for the drawer.
$("program-items").addEventListener("click", event => {
  const play = event.target.closest('[data-program-action="play"]');
  if (play) SONG_DOCK.played = { id: play.dataset.programId, at: performance.now() };
}, true);

$("program-items").addEventListener("click", event => {
  const button = event.target.closest("[data-song-dock]");
  if (!button || !button.closest(".song-dock")) return;
  if (!songDockArmed(SONG_DOCK.armedAt, performance.now(), SONG_DOCK.guardMs)) return;   // just appeared: a tap meant for what was there
  if (button.classList.contains("sd-shield")) {   // PLAY lay here: a tap elsewhere first
    toast("Diese Taste liegt, wo eben PLAY war – erst woanders tippen, dann geht sie", false);
    return;
  }
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
  else if (action === "slide") songAction("/songs/show", { song_id: target.item.song, slide: Number(button.dataset.slide), ...songLiveRev(target.item.song) });
});
