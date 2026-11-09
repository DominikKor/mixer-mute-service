// Mixer Mute Service UI (song-settings.js): Shared song settings dialog.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- shared song settings dialog -------------------------------------------

const SONG_FORM = { monitor: 2, align: "center" };

function songFormSettings() {
  return {
    library_path: $("sg-path").value.trim(),
    target_ip: $("sg-ip").value.trim(),
    monitor: SONG_FORM.monitor,
    fullscreen: $("sg-fullscreen").checked,
    font: $("sg-font").value.trim(),
    font_size: Number($("sg-size").value) || 72,
    text_color: $("sg-text-color").value,
    background_color: $("sg-bg-color").value,
    title_slide: $("sg-title-slide").checked,
    show_title: $("sg-show-title").checked,
    show_copyright: $("sg-show-copyright").checked,
    text_align: SONG_FORM.align,
    show_next_preview: $("sg-show-next").checked,
  };
}

function renderSongSample() {
  const settings = songFormSettings();
  // The song on screen, or a fixed example: its title slide (if switched on)
  // and its first lyrics slide.
  const song = SONG_LIVE.song || {
    title: "Großer Gott, wir loben dich", copyright: "Text: Ignaz Franz 1771",
    slides: [{ text: "Großer Gott, wir loben dich,\nHerr, wir preisen deine Stärke.\nVor dir neigt die Erde sich\nund bewundert deine Werke." },
             { text: "Alles, was dich preisen kann,\nCherubim und Seraphinen" }],
  };
  const titled = settings.title_slide;
  $("sg-show-title").disabled = titled;
  $("sg-show-title-wrap").classList.toggle("off", titled);
  $("sg-sample-title").hidden = !titled;
  $("sg-sample-title").parentElement.classList.toggle("single", !titled);
  if (titled) renderSlide($("sg-sample-title"), songSlide(song, 0, settings), settings);
  renderSlide($("sg-sample"), songSlide(song, titled ? 1 : 0, settings), settings);
}

function setSongAlign(align) {
  SONG_FORM.align = ["left", "center", "right"].includes(align) ? align : "center";
  $("sg-align").querySelectorAll("[data-align]").forEach(b => b.setAttribute("aria-checked", String(b.dataset.align === SONG_FORM.align)));
}

function songLibraryStatus() {
  if (!SONG_LIBRARY.loaded_at) return "Bibliothek noch nicht geladen.";
  const when = new Date(SONG_LIBRARY.loaded_at * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
  const errors = SONG_LIBRARY.errors.length ? ` · ${SONG_LIBRARY.errors.length} nicht lesbar` : "";
  return `${SONG_LIST.length} Lieder · geladen ${when}${errors}`;
}

function songFormError(message) {
  $("sg-error").hidden = !message;
  $("sg-error").textContent = message || "";
}

function setSongMonitor(n) {
  SONG_FORM.monitor = n;
  $("sg-monitor").querySelectorAll("[data-monitor]").forEach(b => b.setAttribute("aria-checked", String(Number(b.dataset.monitor) === n)));
}

async function openSongDialog() {
  await loadSongs();
  const s = SONG_SETTINGS || {};
  $("sg-path").value = s.library_path || "";
  $("sg-ip").value = s.target_ip || "";
  $("sg-font").value = s.font || "Verdana";
  $("sg-size").value = s.font_size || 72;
  $("sg-text-color").value = s.text_color || "#ffffff";
  $("sg-bg-color").value = s.background_color || "#000000";
  $("sg-fullscreen").checked = s.fullscreen !== false;
  $("sg-title-slide").checked = s.title_slide !== false;
  $("sg-show-title").checked = s.show_title !== false;
  $("sg-show-copyright").checked = s.show_copyright !== false;
  $("sg-show-next").checked = s.show_next_preview !== false;
  setSongAlign(s.text_align || "center");
  $("sg-monitor").replaceChildren(...[1, 2, 3, 4].map(n => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "monitor-key";
    b.setAttribute("role", "radio");
    b.dataset.monitor = String(n);
    b.textContent = String(n);
    return b;
  }));
  setSongMonitor(s.monitor || 2);
  $("sg-library-status").textContent = songLibraryStatus();
  songFormError(null);
  $("song-dialog").showModal();
  renderSongSample();
}

async function saveSongSettings() {
  const result = await api("PUT", "/songs/settings", songFormSettings());
  if (!result.ok || result.data.ok === false) { songFormError(result.data.reason || `HTTP ${result.status}`); return false; }
  SONG_SETTINGS = result.data.settings;
  songFormError(null);
  renderSongLive();
  return true;
}

$("song-settings-btn").addEventListener("click", openSongDialog);
$("sg-cancel").addEventListener("click", () => $("song-dialog").close());
$("sg-monitor").addEventListener("click", event => {
  const b = event.target.closest("[data-monitor]");
  if (b) { setSongMonitor(Number(b.dataset.monitor)); }
});
$("sg-align").addEventListener("click", event => {
  const b = event.target.closest("[data-align]");
  if (b) { setSongAlign(b.dataset.align); renderSongSample(); }
});
$("song-form").addEventListener("input", renderSongSample);
$("song-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("sg-save").disabled = true;
  try {
    if (await saveSongSettings()) { $("song-dialog").close(); toast("Liedanzeige gespeichert", true); }
  } finally {
    $("sg-save").disabled = false;
  }
});
$("sg-reload").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  $("sg-library-status").textContent = "Lade Bibliothek vom Anzeige-PC…";
  try {
    if (!(await saveSongSettings())) return;
    // The display PC reads every song file first: allow it up to 30 s.
    const result = await api("POST", "/songs/library/reload", undefined, { timeout: 40000 });
    if (!result.ok || result.data.ok === false) { songFormError(result.data.reason || `HTTP ${result.status}`); return; }
    await loadSongs();
    toast(`${result.data.count} Lieder geladen`, true);
  } finally {
    button.disabled = false;
    $("sg-library-status").textContent = songLibraryStatus();
    renderSongSample();
  }
});
