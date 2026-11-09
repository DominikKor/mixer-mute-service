// Mixer Mute Service UI (templates.js): Program templates.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- program templates: save the program, apply it with the day's songs ----
//
// The songs come as text, one per line (or one by one from the library). The
// service matches each line (POST /songs/match); a line it cannot place gets
// a "Wählen" key. The preview is the service's own result (dry run), so what
// is shown is exactly what "Programm ersetzen" writes.

const TEMPLATES = { list: [], selected: null, matches: [], picked: new Map(), seq: 0, timer: null };
const SLOT_TITLE_RE = /lied|lobpreis|song|worship|anbetung/i;

function songById(id) { return SONG_INDEX.get(id) || null; }

// A key that needs a second tap: true when it was already armed.
function armKey(button, label) {
  if (button.dataset.armed) {
    clearTimeout(Number(button.dataset.armed));
    delete button.dataset.armed;
    button.classList.remove("armed");
    button.textContent = button.dataset.label;
    return true;
  }
  button.dataset.label = button.textContent;
  button.textContent = label;
  button.classList.add("armed");
  button.dataset.armed = String(setTimeout(() => {
    delete button.dataset.armed;
    button.classList.remove("armed");
    button.textContent = button.dataset.label;
  }, 6000));
  return false;
}

function disarmKey(button) {
  if (button.dataset.armed) armKey(button, "");
}

function templateTab(save) {
  $("tp-tab-apply").setAttribute("aria-selected", String(!save));
  $("tp-tab-save").setAttribute("aria-selected", String(save));
  $("tp-apply").hidden = save;
  $("tp-save").hidden = !save;
  closeSongSearch();
  if (save) renderTemplateRows();
}

function renderTemplateList() {
  const list = $("tp-list");
  list.replaceChildren();
  if (!TEMPLATES.list.length) {
    const empty = document.createElement("div");
    empty.className = "tp-empty";
    empty.textContent = "Noch keine Vorlage. Unter „Speichern“ das aktuelle Programm als Vorlage ablegen.";
    list.appendChild(empty);
  }
  for (const template of TEMPLATES.list) {
    const item = document.createElement("div");
    item.className = "tp-item";
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "tp-pick";
    pick.setAttribute("role", "radio");
    pick.setAttribute("aria-checked", String(template.id === TEMPLATES.selected));
    const name = document.createElement("span");
    name.className = "tp-name";
    name.textContent = template.name;
    const meta = document.createElement("span");
    meta.className = "tp-meta";
    meta.textContent = `${template.rows.length} Punkte · ${template.slot_count} ${template.slot_count === 1 ? "Liedplatz" : "Liedplätze"}`;
    pick.append(name, meta);
    pick.addEventListener("click", () => {
      TEMPLATES.selected = template.id;
      $("tp-name").value = template.name;
      renderTemplateList();
      updateTemplatePreview();
    });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "tp-delete";
    del.textContent = "×";
    del.title = `Vorlage „${template.name}“ löschen`;
    del.setAttribute("aria-label", del.title);
    del.addEventListener("click", async () => {
      if (!armKey(del, "Löschen?")) return;
      const result = await api("DELETE", `/program/templates/${encodeURIComponent(template.id)}`);
      if (!result.ok) { toast(result.data.reason || `HTTP ${result.status}`, false); return; }
      toast(`Vorlage „${template.name}“ gelöscht`, true);
      if (TEMPLATES.selected === template.id) TEMPLATES.selected = null;
      await loadTemplates();
    });
    const pencil = renamePencil(`Vorlage „${template.name}“`);
    pencil.addEventListener("click", () => startInlineRename(item, template.name, { maxLength: 60, save: async name => {
      if (await saveName("PUT", `/program/templates/${encodeURIComponent(template.id)}`, name, "Vorlage")) {
        if ($("tp-name").value === template.name) $("tp-name").value = name;
        await loadTemplates();
      }
    } }));
    item.append(pick, pencil, del);
    list.appendChild(item);
  }
}

async function loadTemplates() {
  const result = await api("GET", "/program/templates");
  TEMPLATES.list = result.ok && Array.isArray(result.data.templates) ? result.data.templates : [];
  if (!TEMPLATES.list.some(t => t.id === TEMPLATES.selected)) TEMPLATES.selected = TEMPLATES.list[0]?.id || null;
  renderTemplateList();
  updateTemplatePreview();
}

// The songs to apply: each line's match, or what was picked for that line.
function templateSongs() {
  return TEMPLATES.matches.map(match => {
    const picked = TEMPLATES.picked.has(match.line) ? TEMPLATES.picked.get(match.line) : match.song_id;
    return { song_id: picked && songById(picked) ? picked : null, text: match.line };
  });
}

function renderTemplateMatches() {
  const list = $("tp-matches");
  list.replaceChildren();
  if (!TEMPLATES.matches.length) {
    const empty = document.createElement("li");
    empty.className = "tp-empty";
    empty.textContent = SONG_LIST.length ? "Noch keine Lieder eingegeben." : "Bibliothek noch nicht geladen – unter „Liedanzeige“ laden.";
    list.appendChild(empty);
    return;
  }
  const songs = templateSongs();
  TEMPLATES.matches.forEach((match, i) => {
    const song = songs[i].song_id ? songById(songs[i].song_id) : null;
    const li = document.createElement("li");
    li.className = "tp-match" + (song ? "" : " missing");
    const no = document.createElement("span");
    no.className = "tp-no";
    no.textContent = String(i + 1);
    const texts = document.createElement("span");
    texts.className = "tp-texts";
    const line = document.createElement("span");
    line.className = "tp-line";
    line.textContent = match.line;
    const found = document.createElement("span");
    found.className = "tp-song";
    found.textContent = song ? `♪ ${song.title}`
      : match.candidates.length ? "Nicht eindeutig – bitte wählen" : "Nicht gefunden – kommt als Text in den Titel";
    texts.append(found, line);
    const choose = document.createElement("button");
    choose.type = "button";
    choose.textContent = song ? "Ändern" : "Wählen";
    choose.setAttribute("aria-label", `Lied für Zeile ${i + 1} wählen`);
    choose.addEventListener("click", () => {
      const candidates = match.candidates.map(c => songById(c.song_id)).filter(Boolean);
      openSongSearch(choose, picked => {
        TEMPLATES.picked.set(match.line, picked ? picked.id : null);
        renderTemplateMatches();
        updateTemplatePreview();
      }, true, { candidates });
    });
    li.append(no, texts, choose);
    list.appendChild(li);
  });
}

async function matchTemplateSongs() {
  const lines = $("tp-songs").value.split("\n").map(l => l.trim()).filter(Boolean).slice(0, 40);
  const seq = ++TEMPLATES.seq;
  const result = lines.length ? await api("POST", "/songs/match", { lines }) : { ok: true, data: { matches: [] } };
  if (seq !== TEMPLATES.seq) return;  // typed on meanwhile
  TEMPLATES.matches = result.ok && Array.isArray(result.data.matches) ? result.data.matches : [];
  renderTemplateMatches();
  updateTemplatePreview();
}

async function updateTemplatePreview() {
  const list = $("tp-preview");
  const template = TEMPLATES.list.find(t => t.id === TEMPLATES.selected);
  const apply = $("tp-apply-btn");
  disarmKey(apply);
  apply.disabled = !template;
  if (!template) {
    list.replaceChildren();
    $("tp-hint").textContent = "";
    return;
  }
  const songs = templateSongs();
  const seq = TEMPLATES.seq;
  const result = await api("POST", `/program/templates/${encodeURIComponent(template.id)}/apply`, { dry_run: true, songs });
  if (seq !== TEMPLATES.seq || template.id !== TEMPLATES.selected) return;
  list.replaceChildren();
  if (!result.ok || !Array.isArray(result.data.rows)) {
    $("tp-hint").textContent = result.data.reason || `Vorschau nicht möglich (HTTP ${result.status})`;
    return;
  }
  const daySongs = new Set(songs.map(s => s.song_id).filter(Boolean));
  for (const row of result.data.rows) {
    const li = document.createElement("li");
    if (row.song && daySongs.has(row.song)) li.className = "song";
    const time = document.createElement("span");
    time.className = "tp-time";
    time.textContent = row.time;
    const title = document.createElement("span");
    title.className = "tp-title";
    title.textContent = row.title;
    const song = document.createElement("span");
    song.className = "tp-psong";
    song.textContent = row.song ? `♪ ${songById(row.song)?.title || row.song}` : "";
    li.append(time, title, song);
    list.appendChild(li);
  }
  const extra = Math.max(0, songs.length - template.slot_count);
  const empty = Math.max(0, template.slot_count - songs.length);
  const missing = songs.filter(s => !s.song_id).length;
  $("tp-hint").textContent = [
    extra ? `${extra} ${extra === 1 ? "Lied wird" : "Lieder werden"} nach dem letzten Liedplatz angehängt.` : "",
    empty ? `${empty} ${empty === 1 ? "Liedplatz bleibt" : "Liedplätze bleiben"} ohne Lied.` : "",
    missing ? `${missing} ohne Lied aus der Bibliothek (Text steht im Titel).` : "",
    PROGRAM_ITEMS.length ? `Ersetzt das aktuelle Programm (${PROGRAM_ITEMS.length} Punkte).` : "",
  ].filter(Boolean).join(" ");
}

function renderTemplateRows() {
  const rows = $("tp-rows");
  rows.replaceChildren();
  $("tp-save-btn").disabled = !PROGRAM_ITEMS.length;
  if (!PROGRAM_ITEMS.length) {
    const empty = document.createElement("div");
    empty.className = "tp-empty";
    empty.textContent = "Das Programm ist leer – erst Programmpunkte anlegen.";
    rows.appendChild(empty);
    return;
  }
  for (const item of PROGRAM_ITEMS) {
    const row = document.createElement("div");
    row.className = "tp-row";
    const label = document.createElement("label");
    label.className = "compact-switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.slot = item.id;
    input.checked = Boolean(item.song) || SLOT_TITLE_RE.test(item.title);
    const track = document.createElement("span");
    track.className = "track";
    label.append(input, track, "Liedplatz");
    const time = document.createElement("span");
    time.className = "tp-time";
    time.textContent = item.time;
    const title = document.createElement("span");
    title.className = "tp-title";
    title.textContent = item.title;
    const song = document.createElement("span");
    song.className = "tp-psong";
    song.textContent = item.song ? `♪ ${songById(item.song)?.title || item.song}` : "";
    row.append(label, time, title, song);
    rows.appendChild(row);
  }
}

function templateError(id, message) {
  $(id).hidden = !message;
  $(id).textContent = message || "";
}

async function openTemplateDialog() {
  closeSongSearch();
  templateError("tp-apply-error", "");
  templateError("tp-save-error", "");
  $("tp-songs").value = "";
  TEMPLATES.matches = [];
  TEMPLATES.picked.clear();
  $("template-dialog").showModal();
  await Promise.all([loadSongs(), loadProgramItems()]);
  await loadTemplates();
  $("tp-name").value = TEMPLATES.list.find(t => t.id === TEMPLATES.selected)?.name || "";
  templateTab(!TEMPLATES.list.length);
  renderTemplateMatches();
}

$("program-template-btn").addEventListener("click", openTemplateDialog);
$("tp-tab-apply").addEventListener("click", () => templateTab(false));
$("tp-tab-save").addEventListener("click", () => templateTab(true));
$("template-dialog").querySelectorAll(".tp-close").forEach(b => b.addEventListener("click", () => $("template-dialog").close()));
$("template-dialog").addEventListener("close", closeSongSearch);
$("template-dialog").addEventListener("scroll", closeSongSearch);
$("tp-songs").addEventListener("input", () => {
  clearTimeout(TEMPLATES.timer);
  TEMPLATES.timer = setTimeout(matchTemplateSongs, 250);
});
$("tp-add-song").addEventListener("click", event => {
  openSongSearch(event.currentTarget, song => {
    const area = $("tp-songs");
    const text = area.value.replace(/\s+$/, "");
    area.value = (text ? text + "\n" : "") + song.title;
    TEMPLATES.picked.set(song.title, song.id);
    matchTemplateSongs();
  }, false);
});
$("tp-apply-btn").addEventListener("click", async event => {
  const button = event.currentTarget;
  const template = TEMPLATES.list.find(t => t.id === TEMPLATES.selected);
  if (!template) return;
  if (PROGRAM_ITEMS.length && !armKey(button, `Wirklich ersetzen? (${PROGRAM_ITEMS.length} Punkte)`)) return;
  button.disabled = true;
  try {
    const result = await api("POST", `/program/templates/${encodeURIComponent(template.id)}/apply`, { songs: templateSongs() });
    if (!result.ok || result.data.ok === false) {
      templateError("tp-apply-error", result.data.reason || `HTTP ${result.status}`);
      return;
    }
    $("template-dialog").close();
    toast(`Vorlage „${template.name}“ angewendet: ${result.data.items.length} Programmpunkte`, true);
    for (const warning of result.data.warnings || []) toast(`${warning.title}: ${warning.reason}`, false);
    await loadProgramItems();
  } finally {
    button.disabled = false;
  }
});
$("tp-save-btn").addEventListener("click", async event => {
  const button = event.currentTarget;
  const name = $("tp-name").value.trim();
  if (!name) { templateError("tp-save-error", "Bitte einen Namen eingeben."); $("tp-name").focus(); return; }
  const replace = button.classList.contains("armed");
  if (replace) armKey(button, "");
  const slots = [...$("tp-rows").querySelectorAll("input[data-slot]:checked")].map(input => input.dataset.slot);
  button.disabled = true;
  try {
    const result = await api("POST", "/program/templates", { name, slots, replace });
    if (result.status === 409) {
      templateError("tp-save-error", `Es gibt schon eine Vorlage „${name}“.`);
      armKey(button, "Vorlage ersetzen?");
      return;
    }
    if (!result.ok || result.data.ok === false) {
      templateError("tp-save-error", result.data.reason || `HTTP ${result.status}`);
      return;
    }
    templateError("tp-save-error", "");
    toast(`Vorlage „${result.data.template.name}“ gespeichert (${result.data.template.slot_count} Liedplätze)`, true);
    TEMPLATES.selected = result.data.template.id;
    await loadTemplates();
    templateTab(false);
  } finally {
    button.disabled = false;
  }
});
$("tp-name").addEventListener("input", () => { disarmKey($("tp-save-btn")); templateError("tp-save-error", ""); });
