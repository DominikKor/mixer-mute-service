// Mixer Mute Service UI (songs.js): Songs: live control, presenter keys, new song, song picker.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Songs (FoilPresenter library on the display PC) ----------------------
// The service reads the library through the display agent; one slide at a
// time is shown on the display PC. Here: the song choice in program rows, the
// shared settings, and the live strip with a 16:9 preview of every slide.

let SONG_LIST = [];                // [{id, title, books, slide_count}]
const SONG_INDEX = new Map();
let SONG_SETTINGS = null;
let SONG_LIBRARY = { loaded_at: null, errors: [] };
let SONG_LIVE = { song_id: null, slide: 0, blank: false, song: null };
let SONG_LIVE_FULL = true;   // next poll fetches the song itself (library reloaded)
let SONG_BUSY = false;

async function loadSongs() {
  const [list, settings] = await Promise.all([api("GET", "/songs"), api("GET", "/songs/settings")]);
  if (settings.ok) SONG_SETTINGS = settings.data;
  if (list.ok && Array.isArray(list.data.songs)) {
    SONG_LIST = list.data.songs;
    SONG_LIBRARY = { loaded_at: list.data.loaded_at, errors: list.data.errors || [] };
    SONG_INDEX.clear();
    for (const song of SONG_LIST) SONG_INDEX.set(song.id, song);
    SONG_LIVE_FULL = true;
  }
  document.querySelectorAll(".song-picker").forEach(syncSongPicker);
  renderSongLive();
}

// Slide layout. Keep in sync with _draw_slide() in
// desktop_agent/show_image_agent.py, which draws the real screen.
const SLIDE = { padX: 0.06, padY: 0.07, titleH: 0.12, copyH: 0.08, line: 1.25 };
// Next-slide preview: grey (text colour mixed into the background), at most
// half the text size, never below 20 px on 1080 lines, only in the room under
// the centred text (which never moves for it).
const PREVIEW = { scale: 0.5, min: 20, gap: 0.5, mix: 0.45 };
let SLIDE_CTX = null;

function mixColor(fg, bg, amount) {
  const a = parseInt(fg.slice(1), 16), b = parseInt(bg.slice(1), 16);
  return "#" + [16, 8, 0].map(s => {
    const x = (b >> s) & 255, y = (a >> s) & 255;
    return Math.round(x + (y - x) * amount).toString(16).padStart(2, "0");
  }).join("");
}

// {px, lines} of the next-slide preview, or null where it does not fit.
function previewLayout(ctx, settings, lines, nextLines, px, boxW, boxH, H) {
  if (!nextLines.length) return null;
  const minPx = PREVIEW.min * H / 1080;
  let pp = PREVIEW.scale * px;
  const widest = size => { ctx.font = songSlideFont(size, settings); return Math.max(...nextLines.map(l => ctx.measureText(l).width)); };
  while (pp >= minPx && widest(pp) > boxW) pp *= 0.94;
  if (pp < minPx) return null;
  const free = (boxH - lines.length * px * SLIDE.line) / 2 - PREVIEW.gap * px;
  const count = Math.min(nextLines.length, free > 0 ? Math.floor(free / (pp * SLIDE.line)) : 0);
  return count >= 1 ? { px: pp, lines: nextLines.slice(0, count) } : null;
}

function songSlideFont(px, settings, bold = false) {
  return `${bold ? "700 " : ""}${px}px "${settings.font}", Verdana, sans-serif`;
}

// slide: {text, title, copyright, next, blank, align}
function renderSlide(box, slide, settings) {
  const W = box.clientWidth, H = box.clientHeight;
  box.replaceChildren();
  box.style.background = settings.background_color;
  box.style.color = settings.text_color;
  if (!W || !H || slide.blank) return;
  const ctx = SLIDE_CTX || (SLIDE_CTX = document.createElement("canvas").getContext("2d"));
  const align = slide.align || settings.text_align || "center";
  const padX = SLIDE.padX * W, padY = SLIDE.padY * H;
  const titleH = slide.title ? SLIDE.titleH * H : 0;
  const copyH = slide.copyright ? SLIDE.copyH * H : 0;
  const boxW = W - 2 * padX, boxH = H - 2 * padY - titleH - copyH;
  const lines = slide.text.split("\n");
  const minPx = 8 * H / 1080;
  let px = settings.font_size * H / 1080;
  while (px > minPx) {
    ctx.font = songSlideFont(px, settings);
    if (Math.max(...lines.map(l => ctx.measureText(l).width)) <= boxW && lines.length * px * SLIDE.line <= boxH) break;
    px *= 0.94;
  }
  px = Math.max(minPx, px);
  const place = (el, top, font, color) => {
    el.style.top = `${top}px`;
    el.style.font = font;
    el.style.textAlign = align;
    if (el.classList.contains("slide-line")) { el.style.left = `${padX}px`; el.style.right = `${padX}px`; }
    if (color) el.style.color = color;
    box.appendChild(el);
  };
  const top = padY + titleH + (boxH - lines.length * px * SLIDE.line) / 2;
  lines.forEach((line, i) => {
    const el = document.createElement("div");
    el.className = "slide-line";
    el.textContent = line;
    place(el, top + i * px * SLIDE.line, songSlideFont(px, settings));
  });
  const preview = slide.next
    ? previewLayout(ctx, settings, lines, slide.next.split("\n"), px, boxW, boxH, H) : null;
  if (preview) {
    const grey = mixColor(settings.text_color, settings.background_color, PREVIEW.mix);
    const previewTop = padY + titleH + boxH - preview.lines.length * preview.px * SLIDE.line;
    preview.lines.forEach((line, i) => {
      const el = document.createElement("div");
      el.className = "slide-line slide-next";
      el.textContent = line;
      place(el, previewTop + i * preview.px * SLIDE.line, songSlideFont(preview.px, settings), grey);
    });
  }
  if (slide.title) {
    const el = document.createElement("div");
    el.className = "slide-title";
    el.textContent = slide.title;
    place(el, padY + titleH / 2, songSlideFont(0.05 * H, settings, true));
  }
  if (slide.copyright) {
    const el = document.createElement("div");
    el.className = "slide-copy";
    el.textContent = slide.copyright;
    place(el, H - padY - copyH / 2, songSlideFont(0.026 * H, settings));
  }
}

// The slides as shown: with "Titelfolie" a title slide first. Slide numbers
// (live, show, thumbnails) count these. Same as SongService.slides().
function songDisplaySlides(song, settings) {
  return settings.title_slide !== false
    ? [{ key: "Titel", text: song.title, title_slide: true }, ...song.slides] : song.slides;
}

// What the display PC shows for slide i: the title slide (title centred,
// copyright below), else the lyrics with title and copyright on the first
// slide (only without a title slide) and the next slide as preview.
function songSlide(song, index, settings) {
  const slides = songDisplaySlides(song, settings);
  const slide = slides[index];
  if (!slide) return { blank: true };
  if (slide.title_slide) {
    return { text: slide.text, title: "", copyright: settings.show_copyright ? song.copyright : "", next: "", align: "center" };
  }
  const first = index === 0;
  const upcoming = slides[index + 1];
  return {
    text: slide.text,
    title: settings.show_title && first ? song.title : "",
    copyright: settings.show_copyright && first ? song.copyright : "",
    next: settings.show_next_preview !== false && upcoming ? upcoming.text : "",
  };
}

function songBooks(song) {
  return (song.books || []).map(b => [b.name && b.name.split(" ").map(w => w[0]).join(""), b.number].filter(Boolean).join(" ")).join(" · ");
}

function renderSongLive() {
  const wrap = $("song-live");
  const loaded = SONG_LIST.length > 0;
  wrap.hidden = !loaded && !SONG_LIVE.song;
  const song = SONG_LIVE.song;
  wrap.classList.toggle("idle", !song);
  $("song-live-title").textContent = song ? song.title
    : SONG_LIVE.blank ? "Bildschirm schwarz" : "Kein Lied auf dem Bildschirm";
  $("song-live-pos").textContent = song
    ? (SONG_LIVE.blank ? "Schwarz" : `${SONG_LIVE.slide + 1} / ${songDisplaySlides(song, SONG_SETTINGS || {}).length}`) : "";
  for (const id of ["song-prev-btn", "song-next-btn", "song-blank-btn", "song-hide-btn"]) $(id).disabled = !song || SONG_BUSY;
  $("song-blank-btn").classList.toggle("on", !!SONG_LIVE.blank);
  if (!song || !SONG_SETTINGS) return;
  const preview = $("song-preview");
  preview.classList.toggle("blank", !!SONG_LIVE.blank);
  renderSlide(preview, SONG_LIVE.blank ? { blank: true } : songSlide(song, SONG_LIVE.slide, SONG_SETTINGS), SONG_SETTINGS);
  const strip = $("song-slides");
  const slides = songDisplaySlides(song, SONG_SETTINGS);
  const sig = JSON.stringify([song.id, song.slides.length, SONG_SETTINGS]);
  if (strip.dataset.sig !== sig) {
    strip.dataset.sig = sig;
    strip.replaceChildren(...slides.map((slide, i) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "song-thumb";
      button.dataset.songSlide = String(i);
      button.title = `Folie ${i + 1}${slide.key ? " · " + slide.key : ""} anzeigen`;
      const box = document.createElement("div");
      box.className = "slide-box";
      button.appendChild(box);
      return button;
    }));
    // Thumbnails need their size before they can be drawn.
    requestAnimationFrame(() => strip.querySelectorAll(".song-thumb").forEach((button, i) => {
      const box = button.querySelector(".slide-box");
      renderSlide(box, songSlide(song, i, SONG_SETTINGS), SONG_SETTINGS);
      const tag = document.createElement("span");
      tag.className = "slide-tag";
      tag.textContent = slides[i].key || String(i + 1);
      box.appendChild(tag);
    }));
  }
  strip.querySelectorAll(".song-thumb").forEach((button, i) => {
    button.classList.toggle("current", !SONG_LIVE.blank && i === SONG_LIVE.slide);
    button.disabled = SONG_BUSY;
  });
}

async function refreshSongLive() {
  if (!STAGE.on && !$("program-items").closest(".panel").classList.contains("open")) return;
  // The song itself (all slides) only comes along when it changed.
  const have = SONG_LIVE.song && !SONG_LIVE_FULL ? `?have=${encodeURIComponent(SONG_LIVE.song_id)}` : "";
  const result = await api("GET", "/songs/live" + have);
  if (result.ok && result.data && !SONG_BUSY) {
    const data = result.data;
    if (data.song_unchanged && data.song_id === SONG_LIVE.song_id) data.song = SONG_LIVE.song;
    if (data.song_unchanged && !data.song) return;   // changed meanwhile: next poll fetches it
    SONG_LIVE = data;
    SONG_LIVE_FULL = false;
    renderSongLive();
  }
}

async function songAction(path, body) {
  if (SONG_BUSY) return;
  SONG_BUSY = true;
  renderSongLive();
  try {
    const result = await api("POST", path, body);
    if (!result.ok || result.data.ok === false) toast(`Lied: ${result.data.reason || "HTTP " + result.status}`, false);
    else SONG_LIVE = result.data;
  } finally {
    SONG_BUSY = false;
    renderSongLive();
  }
}

function songNext() { if (SONG_LIVE.song) songAction("/songs/next"); }
function songPrev() { if (SONG_LIVE.song) songAction("/songs/prev"); }
function songBlankToggle() {
  if (!SONG_LIVE.song) {
    // No song: black screen on the display PC, and off again.
    songAction(SONG_LIVE.blank ? "/songs/hide" : "/songs/blank");
    return;
  }
  if (SONG_LIVE.blank) songAction("/songs/show", { song_id: SONG_LIVE.song_id, slide: SONG_LIVE.slide });
  else songAction("/songs/blank");
}

$("song-next-btn").addEventListener("click", songNext);
$("song-prev-btn").addEventListener("click", songPrev);
$("song-blank-btn").addEventListener("click", songBlankToggle);
$("song-hide-btn").addEventListener("click", () => songAction("/songs/hide"));
$("song-slides").addEventListener("click", event => {
  const button = event.target.closest("[data-song-slide]");
  if (button && SONG_LIVE.song) songAction("/songs/show", { song_id: SONG_LIVE.song_id, slide: Number(button.dataset.songSlide) });
});
$("song-choose-btn").addEventListener("click", event => openSongSearch(event.currentTarget, song => {
  if (song) songAction("/songs/show", { song_id: song.id, slide: 0 });
}, false));
new ResizeObserver(() => { if (SONG_LIVE.song && SONG_SETTINGS) renderSongLive(); }).observe($("song-preview"));

// ---- Presenter keys (whenever the page is open) ----------------------------
// Clickers send Page Down / Page Up, many also "b" or "." for a black screen.
//   Page Down: next slide of the song on screen; once the song is done (dark
//        after its last slide) or none is shown, the next program row, like
//        the big Weiter key
//   →, space: next slide only -- never a program row (a stray key on a
//        keyboard must not switch mute groups live)
//   back (Page Up, ←): previous slide (the program never goes back)
//   b or .: black screen and back on;  h: Beamer hide / unhide
// Not while typing in a field or with a dialog open.

function presenterNext() {
  const song = SONG_LIVE.song;
  if (song && !(SONG_LIVE.blank && SONG_LIVE.slide >= songDisplaySlides(song, SONG_SETTINGS || {}).length - 1)) return songNext();
  const go = $("program-go-btn");
  if (go.disabled) return toast(PROGRAM_BUSY.size ? "Programmpunkt startet noch" : "Kein weiterer Programmpunkt", false);
  startProgram("next", "/program/next", go);
}

let BEAMER_KEY_BUSY = false;
async function presenterBeamerToggle() {
  if (BEAMER_KEY_BUSY) return;
  BEAMER_KEY_BUSY = true;
  const id = BEAMER_HIDDEN === true ? "beamer-unhide" : "beamer-hide";
  try {
    const result = await api("POST", `/companion/buttons/${id}/press`);
    if (!result.ok || result.data.ok === false) {
      toast(`Beamer: ${result.data.reason || "HTTP " + result.status}`, false);
      return;
    }
    const state = await api("GET", "/beamer/state");
    renderBeamerState(state.ok ? state.data : null);
    toast(id === "beamer-hide" ? "Beamer hide" : "Beamer unhide", true);
  } finally {
    BEAMER_KEY_BUSY = false;
  }
}

document.addEventListener("keydown", event => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
  // A control that used the key itself (a channel fader takes Page Up/Down and
  // the arrows) must not also start the next program row.
  if (event.defaultPrevented) return;
  const t = event.target;
  if (t.closest && t.closest("input, textarea, select, dialog, [contenteditable]")) return;
  if (t.closest && t.closest("[role=slider], [role=radio], [role=radiogroup], [role=menu], .fader")) return;
  if (document.querySelector("dialog[open]")) return;
  // Space on a focused key would also press that key.
  if (event.key === " " && t.closest && t.closest("button")) return;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === "PageDown") { event.preventDefault(); presenterNext(); }
  else if (["ArrowRight", " "].includes(key)) { if (SONG_LIVE.song) { event.preventDefault(); songNext(); } }
  else if (["PageUp", "ArrowLeft"].includes(key)) { if (SONG_LIVE.song) { event.preventDefault(); songPrev(); } }
  else if (key === "b" || key === ".") { event.preventDefault(); songBlankToggle(); }
  else if (key === "h") { event.preventDefault(); presenterBeamerToggle(); }
});

// ---- new song -----------------------------------------------------------------
// Pasted text is split into slides at blank lines; one slide may be the
// refrain. The order is made by the switches (refrain first / after every
// verse / last) and can then be edited freely as "1 R 2 R". The service writes
// the song as .foil into the library on the display PC.

const NEW_SONG = { refrain: null, manualOrder: false };

function newSongSlides() {
  return $("ns-text").value.replace(/\r\n?/g, "\n").split(/\n[ \t]*\n/).map(t => t.trim()).filter(Boolean);
}

// Verse numbers (1, 2, …) skip the refrain, as in the .foil file.
function newSongKeys(slides) {
  let n = 0;
  return slides.map((_, i) => (i === NEW_SONG.refrain ? "R" : String(++n)));
}

function newSongDefaultOrder(slides) {
  const keys = newSongKeys(slides);
  const verses = keys.filter(k => k !== "R");
  if (NEW_SONG.refrain === null) return verses;
  const start = $("ns-ref-start").checked, each = $("ns-ref-each").checked, end = $("ns-ref-end").checked;
  const order = start ? ["R"] : [];
  verses.forEach((verse, i) => {
    order.push(verse);
    const last = i === verses.length - 1;
    if ((each && !last) || (last && end)) order.push("R");
  });
  if (!verses.length) order.push("R");
  return order;
}

// "1 R 2 R" -> slide indices, or an error text.
function newSongParseOrder(slides) {
  const keys = newSongKeys(slides);
  const tokens = $("ns-order").value.toUpperCase().split(/[\s,;]+/).filter(Boolean)
    .map(t => (t === "REF" || t === "REFRAIN" ? "R" : t));
  if (!tokens.length) return { error: "Die Reihenfolge ist leer." };
  const bad = tokens.filter(t => !keys.includes(t));
  if (bad.length) return { error: `Unbekannt: ${bad.join(", ")} – verfügbar: ${keys.join(" ")}` };
  return { order: tokens.map(t => keys.indexOf(t)) };
}

function renderNewSong() {
  const slides = newSongSlides();
  if (NEW_SONG.refrain !== null && NEW_SONG.refrain >= slides.length) NEW_SONG.refrain = null;
  const keys = newSongKeys(slides);
  const list = $("ns-slides");
  list.replaceChildren();
  if (!slides.length) {
    const empty = document.createElement("div");
    empty.className = "ns-empty";
    empty.textContent = "Noch kein Text – oben einfügen, Strophen durch eine Leerzeile trennen.";
    list.appendChild(empty);
  } else {
    const row = (index, key, first) => {
      const label = document.createElement("label");
      label.className = "ns-slide" + (index !== null && index === NEW_SONG.refrain ? " refrain" : "");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "ns-refrain";
      radio.value = index === null ? "" : String(index);
      radio.checked = index === NEW_SONG.refrain;
      const k = document.createElement("span");
      k.className = "ns-key";
      k.textContent = key;
      const f = document.createElement("span");
      f.className = "ns-first";
      f.textContent = first;
      label.append(radio, k, f);
      list.appendChild(label);
    };
    row(null, "–", "Kein Refrain");
    slides.forEach((text, i) => row(i, keys[i] === "R" ? "Ref" : keys[i], text.split("\n")[0]));
  }
  const hasRefrain = NEW_SONG.refrain !== null;
  for (const id of ["ns-ref-start", "ns-ref-each", "ns-ref-end"]) $(id).disabled = !hasRefrain;
  if (!NEW_SONG.manualOrder) $("ns-order").value = newSongDefaultOrder(slides).join(" ");
  const parsed = slides.length ? newSongParseOrder(slides) : { error: "" };
  const hint = $("ns-order-hint");
  hint.textContent = parsed.error || "Strophen-Nummern und R für den Refrain, frei änderbar.";
  hint.classList.toggle("bad", !!parsed.error);
  const preview = $("ns-preview");
  preview.replaceChildren();
  if (!parsed.order) return;
  const settings = SONG_SETTINGS || { font: "Verdana", font_size: 72, text_color: "#ffffff", background_color: "#000000", show_title: true, show_copyright: true };
  // The new song as it will be shown (title slide included).
  const song = {
    title: $("ns-title").value.trim(), copyright: $("ns-copyright").value.trim(),
    slides: parsed.order.map(index => ({ text: slides[index], key: keys[index] === "R" ? "Ref" : keys[index] })),
  };
  const shown = songDisplaySlides(song, settings);
  const boxes = shown.map(() => {
    const box = document.createElement("div");
    box.className = "slide-box";
    preview.appendChild(box);
    return box;
  });
  requestAnimationFrame(() => shown.forEach((slide, i) => {
    renderSlide(boxes[i], songSlide(song, i, settings), settings);
    const tag = document.createElement("span");
    tag.className = "slide-tag";
    tag.textContent = slide.key;
    boxes[i].appendChild(tag);
  }));
}

function openNewSongDialog() {
  NEW_SONG.refrain = null;
  NEW_SONG.manualOrder = false;
  for (const id of ["ns-title", "ns-copyright", "ns-text", "ns-order"]) $(id).value = "";
  $("ns-ref-start").checked = false;
  $("ns-ref-each").checked = true;
  $("ns-ref-end").checked = true;
  $("ns-error").hidden = true;
  $("new-song-dialog").showModal();
  renderNewSong();
  $("ns-title").focus();
}

$("song-new-btn").addEventListener("click", openNewSongDialog);
$("ns-cancel").addEventListener("click", () => $("new-song-dialog").close());
$("new-song-form").addEventListener("input", event => {
  if (event.target.id === "ns-order") NEW_SONG.manualOrder = true;
  if (event.target.name === "ns-refrain") {
    NEW_SONG.refrain = event.target.value === "" ? null : Number(event.target.value);
    NEW_SONG.manualOrder = false;
  }
  if (["ns-ref-start", "ns-ref-each", "ns-ref-end"].includes(event.target.id)) NEW_SONG.manualOrder = false;
  renderNewSong();
});
$("new-song-form").addEventListener("submit", async event => {
  event.preventDefault();
  const slides = newSongSlides();
  const parsed = newSongParseOrder(slides);
  const error = !$("ns-title").value.trim() ? "Bitte einen Titel eingeben."
    : !slides.length ? "Bitte den Liedtext eingeben." : parsed.error;
  $("ns-error").hidden = !error;
  $("ns-error").textContent = error || "";
  if (error) return;
  const save = $("ns-save");
  save.disabled = true;
  try {
    const result = await api("POST", "/songs/create", {
      title: $("ns-title").value.trim(), copyright: $("ns-copyright").value.trim(),
      slides, refrain: NEW_SONG.refrain, order: parsed.order,
    }, { timeout: 15000 });
    if (!result.ok || result.data.ok === false) {
      $("ns-error").hidden = false;
      $("ns-error").textContent = result.data.reason || `HTTP ${result.status}`;
      return;
    }
    $("new-song-dialog").close();
    toast(`„${result.data.song.title}“ in der Bibliothek gespeichert`, true);
    await loadSongs();
  } finally {
    save.disabled = false;
  }
});

// ---- song choice: a button in the row, a search popover on <body> ----------

function buildSongPicker(value) {
  const wrap = document.createElement("div");
  wrap.className = "song-picker";
  const hidden = document.createElement("input");
  hidden.type = "hidden";
  hidden.dataset.programField = "song";
  hidden.value = value || "";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "song-pick-btn";
  wrap.append(hidden, button);
  syncSongPicker(wrap);
  return wrap;
}

function syncSongPicker(wrap) {
  const id = wrap.querySelector('[data-program-field="song"]').value;
  const song = SONG_INDEX.get(id);
  const button = wrap.querySelector(".song-pick-btn");
  button.classList.toggle("empty", !id);
  button.textContent = id ? `♪ ${song ? song.title : id.replace(/\.foil$/i, "")}` : "♪ Lied wählen";
  button.title = id ? (song ? `${song.title} · ${song.slide_count} Folien` : "Lied nicht in der geladenen Bibliothek") : "Lied aus der FoilPresenter-Bibliothek";
}

let SONG_SEARCH = null;

function closeSongSearch() {
  if (!SONG_SEARCH) return;
  document.removeEventListener("pointerdown", SONG_SEARCH.onOutside, true);
  SONG_SEARCH.pop.remove();
  SONG_SEARCH = null;
}

function songMatches(query) {
  const q = query.trim().toLocaleLowerCase("de");
  if (!q) return SONG_LIST.slice(0, 40);
  const words = q.split(/\s+/);
  return SONG_LIST.filter(song => {
    const hay = `${song.title} ${(song.books || []).map(b => `${b.name} ${b.number}`).join(" ")}`.toLocaleLowerCase("de");
    return words.every(w => hay.includes(w));
  }).slice(0, 40);
}

// onPick(song or null); allowNone adds "Kein Lied". Inside a modal dialog
// (everything outside it is inert) the popover opens in the dialog; options
// .candidates are listed first while the search is empty.
function openSongSearch(anchor, onPick, allowNone = true, options = {}) {
  closeSongSearch();
  const pop = document.createElement("div");
  pop.className = "song-search";
  pop.innerHTML = '<input type="search" class="pf-input" placeholder="Titel oder Liederbuch-Nummer" aria-label="Lied suchen" autocomplete="off" /><div class="song-search-list" role="listbox"></div>';
  const dialog = anchor.closest("dialog[open]");
  (dialog || document.body).appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const width = pop.offsetWidth;
  const left = Math.min(Math.max(8, r.left), innerWidth - width - 8);
  if (dialog) {
    pop.style.position = "fixed";  // placed after the first render (placeInDialog)
    pop.style.left = `${left}px`;
  } else {
    pop.style.left = `${left + scrollX}px`;
    pop.style.top = `${r.bottom + 4 + scrollY}px`;
  }
  const input = pop.querySelector("input");
  const list = pop.querySelector(".song-search-list");
  const pick = song => { closeSongSearch(); onPick(song); };
  const render = () => {
    const candidates = !input.value.trim() && options.candidates?.length ? options.candidates : null;
    const matches = candidates || songMatches(input.value);
    list.replaceChildren();
    if (allowNone) {
      const none = document.createElement("button");
      none.type = "button";
      none.textContent = "Kein Lied";
      none.addEventListener("click", () => pick(null));
      list.appendChild(none);
    }
    if (!SONG_LIST.length) {
      const empty = document.createElement("div");
      empty.className = "song-search-empty";
      empty.textContent = "Bibliothek noch nicht geladen – unter „Liedanzeige“ laden.";
      list.appendChild(empty);
    }
    matches.forEach((song, i) => {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "option");
      if (i === 0 && input.value.trim()) button.classList.add("active");
      const title = document.createElement("span");
      title.textContent = song.title;
      const books = document.createElement("span");
      books.className = "books";
      books.textContent = songBooks(song);
      button.append(title, books);
      button.addEventListener("click", () => pick(song));
      list.appendChild(button);
    });
  };
  input.addEventListener("input", render);
  input.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeSongSearch(); anchor.focus(); }
    if (event.key === "Enter") { event.preventDefault(); const first = songMatches(input.value)[0]; if (first) pick(first); }
  });
  const onOutside = event => { if (!pop.contains(event.target) && event.target !== anchor) closeSongSearch(); };
  document.addEventListener("pointerdown", onOutside, true);
  SONG_SEARCH = { pop, onOutside };
  render();
  if (dialog) {
    // Below the key, or above it when there is more room there; the list
    // shrinks so the whole popover stays on screen.
    const below = innerHeight - r.bottom - 12, above = r.top - 12;
    const room = below >= 300 || below >= above ? below : above;
    list.style.maxHeight = `${Math.max(120, Math.min(320, room - (pop.offsetHeight - list.offsetHeight)))}px`;
    if (room === below) pop.style.top = `${r.bottom + 4}px`;
    else pop.style.bottom = `${innerHeight - r.top + 4}px`;
  }
  input.focus();
}

$("program-items").addEventListener("click", event => {
  const button = event.target.closest(".song-pick-btn");
  if (!button) return;
  const wrap = button.closest(".song-picker");
  const row = button.closest("tr");
  openSongSearch(button, song => {
    const hidden = wrap.querySelector('[data-program-field="song"]');
    hidden.value = song ? song.id : "";
    syncSongPicker(wrap);
    const title = row.querySelector('[data-program-field="title"]');
    if (song && title && !title.value.trim()) title.value = song.title;
    hidden.dispatchEvent(new Event("change", { bubbles: true }));
  });
});
