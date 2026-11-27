// Mixer Mute Service UI (desktop.js): Desktop profile editor.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- desktop profile editor ---------------------------------------------
// Saves go to config.yaml: the server only adds the new profile or changes the
// edited values in place, everything else in the file stays as written.

let DESKTOP_PROFILES = [];
// Today's quarter and week from the service: {year, quarter, week, subfolder, file_prefix}.
let MISSION_SPOTLIGHT = null;
// kind: taken from the path (profileKindFor); stored: the edited profile as
// it was ({path, kind, mission_spotlight}), null for a new one.
const PROFILE_FORM = { mode: "create", id: null, idTouched: false, kind: "file", stored: null, monitors: [1], position: "top-left" };
const MAX_MONITORS = 5;
const KIND_INFO = {
  image: { placeholder: "I:\\Bilder\\GemeindegabenQR.png",
           hint: "Bilddatei (PNG, JPG …) auf dem Ziel-PC. Der Desktop-Agent zeigt sie an; Esc blendet sie aus." },
  program: { placeholder: "C:\\Program Files\\App\\app.exe",
             hint: "Pfad zur .exe auf dem Ziel-PC (ein unbekannter Teil mit *, aber mit Endung: obs64*.exe). Argumente werden einzeln und ohne Shell übergeben." },
  file: { placeholder: "D:\\Folien\\Predigt.pptx",
          hint: "Beliebige Datei auf dem Ziel-PC – Windows öffnet sie mit der Standard-App (PowerPoint, PDF …)." },
  audio: { placeholder: "I:\\Musik\\Lied.mp3 oder https://youtu.be/…",
           hint: "Audiodatei auf dem Ziel-PC oder ein YouTube-Link (wird erkannt). mpv muss dort installiert und über PATH oder MPV_PATH erreichbar sein, für YouTube zusätzlich yt-dlp." },
};
// "*" in a path stands for an unknown part of a name (the display agent fills
// it in on the target PC; with several matches the newest file is used).
const WILDCARD_HINT = " Unbekannter Teil eines Namens: * (z. B. Ansage_*.mp3; passen mehrere, gilt die neueste Datei).";
const POSITION_LABELS = {
  "top-left": "Oben links", "top-right": "Oben rechts", "center": "Mitte",
  "bottom-left": "Unten links", "bottom-right": "Unten rechts",
};
const PROFILE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const IPV4_RE = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const WIN_ABS_PATH_RE = /^([A-Za-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+)/;
// A YouTube link instead of a path (audio only; the display agent plays its
// sound with mpv and yt-dlp). Same check as app/youtube.py and the agent.
const YOUTUBE_URL_RE = /^https?:\/\/(?:(?:www|m|music)\.)?(?:youtube\.com\/(?:watch\?(?:[^\s#]*&)?v=[\w-]{6,}|shorts\/[\w-]{6,}|live\/[\w-]{6,}|embed\/[\w-]{6,})|youtu\.be\/[\w-]{6,})\S*$/;
const YOUTUBE_HINT = "YouTube-Link erkannt: der Ziel-PC spielt den Ton ohne Fenster ab (mpv + yt-dlp), mit Ein- und Ausblenden wie bei einer Datei.";

function isYoutubeUrl(value) {
  return value.length <= 500 && YOUTUBE_URL_RE.test(value);
}

// Capitals in scheme or host ("HTTPS://YouTube.com/…") lower-cased when that
// makes a YouTube link, as the service does when saving (app/youtube.py).
function normalizeYoutubeUrl(value) {
  const lowered = value.replace(/^([A-Za-z]+:\/\/[^\/?#\s]*)/, m => m.toLowerCase());
  return lowered !== value && isYoutubeUrl(lowered) ? lowered : value;
}

// ---- the kind comes from the path ---------------------------------------------
// Same rules as the service (app/profile_kind.py, which decides on saving;
// tests/test_profile_kind.py checks both agree): a YouTube link is audio,
// image and audio extensions image and audio, .exe a program, everything
// else a file (Windows opens it with its default app). A path without an
// extension and a Mission Spotlight folder say nothing: an edited profile
// keeps its kind, a new one is a file.
// Only what the display agent can show (Pillow): .svg and .heic are files.
const PROFILE_IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico"]);
const PROFILE_AUDIO_EXT = new Set([".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff", ".mka"]);
const PROFILE_PROGRAM_EXT = new Set([".exe"]);
// What a file profile opens, for the label only.
const PROFILE_FILE_LABELS = {
  ".mp4": "Video", ".mov": "Video", ".mkv": "Video", ".avi": "Video", ".wmv": "Video", ".webm": "Video",
  ".m4v": "Video", ".mpg": "Video", ".mpeg": "Video", ".pdf": "PDF", ".ppt": "Präsentation",
  ".pptx": "Präsentation", ".pps": "Präsentation", ".ppsx": "Präsentation", ".odp": "Präsentation",
  ".doc": "Dokument", ".docx": "Dokument", ".odt": "Dokument", ".txt": "Text", ".xls": "Tabelle", ".xlsx": "Tabelle",
  ".lnk": "Verknüpfung (wird von Windows ausgeführt)", ".url": "Internet-Verknüpfung",
  ".bat": "Skript (wird von Windows ausgeführt)", ".cmd": "Skript (wird von Windows ausgeführt)", ".html": "Webseite", ".htm": "Webseite",
};
const PROFILE_KIND_LABELS = { image: "Bild", audio: "Audio", program: "Programm", file: "Datei" };

// {kind, label} the path asks for, or null when it says nothing (no extension).
function profileKindOf(path, spotlight) {
  const value = normalizeYoutubeUrl((path || "").trim());
  if (isYoutubeUrl(value)) return { kind: "audio", label: "YouTube-Audio" };
  if (spotlight) return null;  // a folder: the kind is not in the path
  const name = value.split(/[\\/]/).pop();
  if (!name || name.endsWith("*") || !name.replace(/^\.+|\.+$/g, "").includes(".")) return null;
  const ext = "." + name.split(".").pop().toLowerCase();
  if (PROFILE_IMAGE_EXT.has(ext)) return { kind: "image", label: "Bild" };
  if (PROFILE_AUDIO_EXT.has(ext)) return { kind: "audio", label: "Audio" };
  if (PROFILE_PROGRAM_EXT.has(ext)) return { kind: "program", label: "Programm" };
  return { kind: "file", label: PROFILE_FILE_LABELS[ext] || "Datei" };
}

// The kind the service will store (app/profile_kind.profile_kind): an edited
// profile keeps its kind while path and Mission Spotlight stay; a path that
// says nothing keeps the old kind, a new profile then becomes a file.
function profileKindFor(path, spotlight, stored) {
  if (stored && (stored.path || "") === (path || "") && !!stored.mission_spotlight === !!spotlight) {
    return { ...(profileKindOf(path, spotlight) || {}), kind: stored.kind, label: PROFILE_KIND_LABELS[stored.kind], kept: true };
  }
  const found = profileKindOf(path, spotlight);
  if (found) return found;
  // Mission Spotlight keeps an image or audio profile as it is (a program
  // cannot be one); a new one opens the week's file.
  const keep = stored && !(spotlight && stored.kind === "program");
  const kind = keep ? stored.kind : "file";
  if (spotlight) return { kind, label: `${PROFILE_KIND_LABELS[kind]} der Woche (Mission Spotlight)`, kept: !!keep };
  return { kind, label: PROFILE_KIND_LABELS[kind], kept: !!keep, unknown: true };
}

function syncProfilePath() {
  syncProfileForm();
}

function slugifyProfileId(title) {
  const base = title.toLowerCase()
    .replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss")
    .normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 56) || "profil";
  const taken = new Set(DESKTOP_PROFILES.map(p => p.id));
  let id = base, n = 2;
  while (taken.has(id)) id = `${base}-${n++}`;
  return id;
}

function setRadio(groupEl, attr, value) {
  groupEl.querySelectorAll(`[${attr}]`).forEach(el => {
    const on = el.getAttribute(attr) === String(value);
    el.setAttribute("aria-checked", String(on));
    el.tabIndex = on ? 0 : -1;
  });
}

// Images and documents (PDF, PowerPoint, Word: the display agent shows their
// pages itself) may show on several monitors at once (the same on each);
// programs and other files open on one. Keep in step with app/documents.py.
const PROFILE_DOCUMENT_RE = /\.(pdf|pptx?|pptm|ppsx?|ppsm|odp|docx?|docm|odt|rtf)$/i;

function profileMultiMonitor() {
  return PROFILE_FORM.kind === "image"
    || (PROFILE_FORM.kind === "file" && PROFILE_DOCUMENT_RE.test($("pf-path").value.trim()));
}

function renderMonitorKeys() {
  const wrap = $("pf-monitor");
  const multi = profileMultiMonitor();
  if (!multi) PROFILE_FORM.monitors = PROFILE_FORM.monitors.slice(0, 1);
  const count = Math.max(MAX_MONITORS, ...PROFILE_FORM.monitors);
  if (wrap.children.length !== count) {
    wrap.innerHTML = "";
    for (let i = 1; i <= count; i++) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "monitor-key";
      b.dataset.monitor = String(i); b.textContent = String(i);
      b.setAttribute("aria-label", `Monitor ${i}`);
      wrap.appendChild(b);
    }
  }
  wrap.setAttribute("role", multi ? "group" : "radiogroup");
  wrap.querySelectorAll("[data-monitor]").forEach(b => {
    const on = PROFILE_FORM.monitors.includes(Number(b.dataset.monitor));
    b.setAttribute("role", multi ? "checkbox" : "radio");
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = multi || on ? 0 : -1;
  });
  $("pf-monitor-hint").textContent = multi
    ? `von links gezählt · mehrere wählbar: ${PROFILE_FORM.kind === "image" ? "gleiches Bild" : "gleiche Seite"} auf jedem`
    : "von links gezählt";
}

function pickMonitor(n) {
  if (!profileMultiMonitor()) { PROFILE_FORM.monitors = [n]; return; }
  const set = new Set(PROFILE_FORM.monitors);
  if (set.has(n)) { if (set.size > 1) set.delete(n); } else set.add(n);
  PROFILE_FORM.monitors = [...set].sort((a, b) => a - b);
}

function syncProfileForm() {
  const pathValue = $("pf-path").value.trim();
  const detected = profileKindFor(pathValue, $("pf-spotlight").checked, PROFILE_FORM.stored);
  const kind = PROFILE_FORM.kind = detected.kind;
  // What the path makes of the profile, read-only (there is no choice).
  const label = $("pf-kind-detected");
  label.hidden = !pathValue;
  label.textContent = detected.unknown
    ? `${detected.kept ? "Bleibt" : "Erkannt"}: ${detected.label} – keine Dateiendung erkannt`
    : `Erkannt: ${detected.label}`;
  label.dataset.kind = kind;
  // Mission Spotlight: the path is a folder; the week's file is picked by date.
  $("pf-spotlight-row").hidden = kind === "program" && !$("pf-spotlight").checked;
  const spotlight = $("pf-spotlight").checked;
  $("pf-spotlight-text").textContent = spotlight ? "Ein" : "Aus";
  const now = MISSION_SPOTLIGHT;
  $("pf-spotlight-hint").textContent = now
    ? `Heute: ${now.year} · ${now.quarter}. Quartal · Woche ${now.week} des Quartals`
    : "Quartal und Woche werden geladen…";
  $("pf-path-label").textContent = spotlight ? "Ordner auf dem Ziel-PC" : "Pfad auf dem Ziel-PC";
  $("pf-path").placeholder = spotlight ? "I:\\Mission Spotlight" : "z. B. I:\\Bilder\\QR.png, D:\\Videos\\Intro.mp4 oder https://youtu.be/…";
  const youtube = kind === "audio" && isYoutubeUrl($("pf-path").value.trim());
  const folder = $("pf-path").value.trim().replace(/[\\/]+$/, "") || "Ordner";
  $("pf-path-hint").textContent = spotlight
    ? `Ordner mit den Unterordnern „${now ? now.year : "JJJJ"}_1.Quartal“ … „${now ? now.year : "JJJJ"}_4.Quartal“. Gewählt wird die Datei, deren Name mit der zweistelligen Woche beginnt`
      + (now ? `, jetzt: ${folder}\\${now.subfolder}\\${now.file_prefix}… (z. B. ${now.file_prefix}_Video.mp4)` : "")
      + ". Passen mehrere, gilt die neueste."
    : youtube ? YOUTUBE_HINT : pathValue ? KIND_INFO[kind].hint + WILDCARD_HINT
      : "Die Art ergibt sich aus der Dateiendung: Bild, Audio (auch YouTube-Link), Programm (.exe), sonst öffnet Windows die Datei mit der Standard-App." + WILDCARD_HINT;
  $("pf-path-hint").classList.toggle("youtube", youtube && !spotlight);
  $("pf-args-wrap").hidden = kind !== "program";
  $("pf-display-row").hidden = kind === "audio";
  $("pf-fade-row").hidden = kind !== "audio";
  const fade = $("pf-fade").checked;
  $("pf-fade-text").textContent = fade ? "Ein" : "Aus";
  $("pf-fade-seconds").disabled = !fade;
  renderMonitorKeys();
  const full = $("pf-fullscreen").checked;
  $("pf-fullscreen-text").textContent = full ? "Ein" : "Aus";
  const picker = $("pf-position");
  picker.classList.toggle("fullscreen", full);
  setRadio(picker, "data-pos", PROFILE_FORM.position);
  picker.querySelectorAll(".pos-zone").forEach(z => { z.disabled = full; });
  $("pf-position-caption").textContent = full
    ? "Vollbild füllt den ganzen Monitor"
    : `${POSITION_LABELS[PROFILE_FORM.position]} · Fenster behält seine Größe`;
}

function profileFormError(msg, field) {
  document.querySelectorAll("#profile-form .invalid").forEach(el => el.classList.remove("invalid"));
  const box = $("pf-error");
  box.hidden = !msg;
  box.textContent = msg || "";
  if (field) { field.classList.add("invalid"); field.focus(); }
  if (msg) box.scrollIntoView({ block: "nearest" });
}

// ---- Target PCs (Streaming PC, Anzeige PC) --------------------------------
// Fields that send to another PC offer these two instead of an IP field; their
// names and addresses live in the "Ziel-PCs" window. Settings still store the
// IP, so an address that matches neither PC stays selectable ("Andere IP").

let TARGET_PCS = [
  { id: "streaming", name: "Streaming PC", ip: "192.168.188.20" },
  { id: "display", name: "Anzeige PC", ip: "192.168.188.11" },
];

async function loadTargetPcs() {
  const result = await api("GET", "/target-pcs");
  if (result.ok && Array.isArray(result.data.pcs)) TARGET_PCS = result.data.pcs;
}

function targetPcName(ip) {
  const pc = TARGET_PCS.find(item => item.ip === ip);
  return pc ? pc.name : null;
}

function targetPcLabel(ip) {
  const name = targetPcName(ip);
  return name ? `${name} (${ip})` : ip;
}

// Fill a PC <select>; keeps an address that is neither of the two PCs.
function fillTargetPcSelect(select, ip) {
  const options = TARGET_PCS.map(pc => ({ value: pc.ip, label: `${pc.name} (${pc.ip})` }));
  if (ip && !TARGET_PCS.some(pc => pc.ip === ip)) options.push({ value: ip, label: `Andere IP (${ip})` });
  select.replaceChildren(...options.map(option => {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    return element;
  }));
  select.value = ip && options.some(option => option.value === ip) ? ip : options[options.length > 1 ? 1 : 0].value;
}

function targetPcsError(message, field) {
  document.querySelectorAll("#target-pcs-form .invalid").forEach(el => el.classList.remove("invalid"));
  $("tp-error").hidden = !message;
  $("tp-error").textContent = message || "";
  if (field) { field.classList.add("invalid"); field.focus(); }
}

async function openTargetPcsDialog() {
  await loadTargetPcs();
  for (const pc of TARGET_PCS) {
    $(`tp-${pc.id}-name`).value = pc.name;
    $(`tp-${pc.id}-ip`).value = pc.ip;
  }
  targetPcsError(null);
  $("tp-save").disabled = false;
  $("target-pcs-dialog").showModal();
}

async function saveTargetPcs(event) {
  event.preventDefault();
  const pcs = TARGET_PCS.map(pc => ({ id: pc.id, name: $(`tp-${pc.id}-name`).value.trim(), ip: $(`tp-${pc.id}-ip`).value.trim() }));
  for (const pc of pcs) {
    if (!pc.name) return targetPcsError("Bitte einen Namen eingeben.", $(`tp-${pc.id}-name`));
    if (!IPV4_RE.test(pc.ip)) return targetPcsError(`IP-Adresse ungültig, z. B. ${pc.id === "display" ? "192.168.188.11" : "192.168.188.20"}`, $(`tp-${pc.id}-ip`));
  }
  if (pcs[0].ip === pcs[1].ip) return targetPcsError("Die beiden PCs brauchen verschiedene IP-Adressen.", $("tp-display-ip"));
  // Selections still open in other dialogs follow a changed address.
  const moves = Object.fromEntries(TARGET_PCS.map(old => [old.ip, pcs.find(pc => pc.id === old.id).ip]).filter(([a, b]) => a !== b));
  $("tp-save").disabled = true;
  const result = await api("PUT", "/target-pcs", { pcs });
  $("tp-save").disabled = false;
  if (!result.ok || result.data.ok === false) return targetPcsError(result.data.reason || `HTTP ${result.status}`);
  TARGET_PCS = result.data.pcs;
  for (const select of [$("pf-ip"), $("sg-ip")]) {
    if (select.options.length) fillTargetPcSelect(select, moves[select.value] || select.value);
  }
  moveSongTargets(moves);
  $("target-pcs-dialog").close();
  const moved = result.data.moved || {};
  const problems = moved.problems || [];
  if (problems.length) toast(`Ziel-PCs gespeichert, aber nicht umgestellt: ${problems.join("; ")}`, false);
  else toast("Ziel-PCs gespeichert", true);
  DESKTOP_PROFILES_SIGNATURE = "";
  await Promise.all([refresh(), loadSongs()]);
}

function openProfileDialog(profileId) {
  const existing = profileId ? DESKTOP_PROFILES.find(p => p.id === profileId) : null;
  if (profileId && !existing) return toast("Profil nicht gefunden", false);
  const p = existing || {
    title: "", kind: "file", path: "", args: [], fullscreen: true, monitor: 1, position: "top-left",
    fade: false, fade_seconds: 5,
    target_ip: (TARGET_PCS.find(pc => pc.id === "display") || TARGET_PCS[0]).ip,
  };
  Object.assign(PROFILE_FORM, {
    mode: existing ? "edit" : "create", id: existing ? existing.id : null, idTouched: false,
    kind: p.kind, monitors: p.monitors && p.monitors.length ? [...p.monitors] : [p.monitor], position: p.position,
    stored: existing ? { path: existing.path || "", kind: existing.kind, mission_spotlight: !!existing.mission_spotlight } : null,
  });
  $("profile-title").textContent = existing ? `Profil bearbeiten` : "Neues Profil";
  $("pf-title").value = p.title;
  $("pf-id").value = existing ? existing.id : "";
  $("pf-id").disabled = !!existing;
  $("pf-id").title = existing ? "Die ID eines bestehenden Profils bleibt gleich" : "Wird aus dem Namen erzeugt";
  $("pf-path").value = p.path || "";
  $("pf-args").value = (p.args || []).join("\n");
  fillTargetPcSelect($("pf-ip"), p.target_ip);
  $("pf-fullscreen").checked = !!p.fullscreen;
  $("pf-fade").checked = !!p.fade;
  $("pf-fade-seconds").value = p.fade_seconds ?? 5;
  $("pf-spotlight").checked = !!p.mission_spotlight;
  profileFormError(null);
  $("pf-save").disabled = false;
  syncProfileForm();
  $("profile-dialog").showModal();
  $("pf-title").focus();
}

function readProfileForm() {
  const title = $("pf-title").value.trim();
  const id = $("pf-id").value.trim();
  const path = normalizeYoutubeUrl($("pf-path").value.trim());
  const ip = $("pf-ip").value.trim();
  syncProfileForm();
  const kind = PROFILE_FORM.kind;
  const args = kind === "program"
    ? $("pf-args").value.split(/\r?\n/).map(a => a.trim()).filter(Boolean) : [];
  if (!title) return profileFormError("Bitte einen Namen eingeben.", $("pf-title"));
  if (!PROFILE_ID_RE.test(id)) return profileFormError("ID: nur Buchstaben, Ziffern, - und _ (max. 64).", $("pf-id"));
  const youtube = isYoutubeUrl(path);
  const spotlight = $("pf-spotlight").checked;
  if (spotlight && (youtube || !WIN_ABS_PATH_RE.test(path) || path.includes("*"))) {
    return profileFormError("Mission Spotlight: vollständigen Ordner angeben (ohne *), z. B. I:\\Mission Spotlight", $("pf-path"));
  }
  if (youtube && kind !== "audio") return profileFormError("Ein YouTube-Link spielt nur als Audio.", $("pf-path"));
  if (!youtube && !WIN_ABS_PATH_RE.test(path)) return profileFormError("Pfad: vollständiger Windows-Pfad, z. B. " + KIND_INFO[kind].placeholder.split(" oder ")[0] + (kind === "audio" ? ", oder ein YouTube-Link" : ""), $("pf-path"));
  if (kind === "program" && !/(\.exe|\*)$/i.test(path)) return profileFormError("Ein Programm muss auf eine .exe zeigen, mit * z. B. obs64*.exe.", $("pf-path"));
  if (!IPV4_RE.test(ip)) return profileFormError("Bitte einen Ziel-PC wählen.", $("pf-ip"));
  // Only audio fades; other kinds keep the defaults (and config.yaml stays short).
  const fade = kind === "audio" && $("pf-fade").checked;
  let fadeSeconds = 5;
  if (kind === "audio") {
    fadeSeconds = Number($("pf-fade-seconds").value.replace(",", "."));
    if (!$("pf-fade-seconds").value.trim() || !(fadeSeconds >= 0.5 && fadeSeconds <= 30)) return profileFormError("Dauer: 0,5 bis 30 Sekunden", $("pf-fade-seconds"));
  }
  if (PROFILE_FORM.monitors.some(m => m > MAX_MONITORS)) return profileFormError(`Monitor: 1 bis ${MAX_MONITORS}`, $("pf-monitor"));
  const monitors = profileMultiMonitor() ? PROFILE_FORM.monitors : PROFILE_FORM.monitors.slice(0, 1);
  return {
    // No kind: the service takes it from the path (as shown in the dialog).
    id, title, path, args, target_ip: ip, fullscreen: $("pf-fullscreen").checked,
    monitor: monitors[0], monitors: monitors.length > 1 ? monitors : [], position: PROFILE_FORM.position,
    fade, fade_seconds: fadeSeconds, mission_spotlight: spotlight,
  };
}

async function saveProfile(event) {
  event.preventDefault();
  const body = readProfileForm();
  if (!body) return;
  profileFormError(null);
  $("pf-save").disabled = true;
  const edit = PROFILE_FORM.mode === "edit";
  const r = edit
    ? await api("PUT", `/desktop/profiles/${encodeURIComponent(PROFILE_FORM.id)}`, body)
    : await api("POST", "/desktop/profiles", body);
  $("pf-save").disabled = false;
  if (!r.ok || r.data.ok === false) {
    return profileFormError("Nicht gespeichert: " + (r.data.reason || `HTTP ${r.status}`));
  }
  $("profile-dialog").close();
  toast(`${body.title}: ${edit ? "geändert" : "angelegt"}`, true);
  DESKTOP_PROFILES_SIGNATURE = "";
  await refresh();
}

// Cards are built once per channel and then updated in place, so a fader that
// is being dragged is never replaced by the once-per-second refresh.
const CARDS = {};
let CARD_KEYS = "";

function buildChannelCard(ch) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="scribble">
      <div class="scribble-top"><span>CH ${ch}</span><span class="solo-tag" hidden title="Am Pult auf Solo (Kopfhörer); bleibt bei Mute und Fader erhalten">SOLO</span><span class="led"></span></div>
      <div class="card-title-wrap"></div>
    </div>
    <div class="fader-strip">${faderStripHtml(ch)}</div>
    <button class="mute-key unk" data-action="channel-toggle" data-channel="${ch}" disabled>MUTE</button>
    <label class="always-on-control" title="Bei Gruppen-Mute und Solo nicht stummschalten">
      <input type="checkbox" data-always-on-channel="${ch}">
      <span class="track"></span><span class="label">Immer an</span>
    </label>`;
  // The level readout sits in the scribble strip under the name.
  const db = card.querySelector(".fader-db");
  card.querySelector(".scribble").appendChild(db);
  CARDS[ch] = card;
  attachFader(ch, card.querySelector(".fader"), db);
  return card;
}

function updateChannelCard(ch, c) {
  const card = CARDS[ch];
  const known = c.muted !== null && c.muted !== undefined;
  card.className = "card " + (known ? (c.muted ? "muted" : "unmuted") : "");

  const displayName = getChannelDisplayName(ch, c.name || ("Channel " + ch));
  const titleWrap = card.querySelector(".card-title-wrap");
  if (!titleWrap.querySelector("input") && titleWrap.dataset.name !== displayName) {
    titleWrap.dataset.name = displayName;
    titleWrap.innerHTML = `
      <button class="card-title-btn" data-action="channel-rename" data-channel="${ch}" data-name="${escapeAttr(displayName)}"
        title="${escapeAttr(displayName)} – umbenennen, Farbe" aria-label="${escapeAttr(displayName)} umbenennen">
        <span class="card-title" data-rename-ch="${ch}">${escapeHtml(displayName).replace(/([+\/_-])/g, '$1<span class="name-break">&#8203;</span>')}</span><span class="rename-icon" aria-hidden="true">&#9998;</span>
      </button>`;
  }

  card.querySelector(".solo-tag").hidden = c.solo !== true;
  const led = card.querySelector(".led");
  led.className = "led " + (c.safe ? "safe" : "unsafe");
  led.title = (c.safe ? "sicher" : "unsicher: " + (faderBlockReason(c) || "Kanal unsicher"))
    + ` · Fader gelesen ${fmtAge(c.fader_age_seconds)}`;

  // Toggling needs an observed mute state; without one the key stays locked.
  const key = card.querySelector(".mute-key");
  const busy = TOGGLE_BUSY.has(String(ch));
  key.className = "mute-key " + (!known ? "unk" : c.muted ? "on" : "off") + (busy ? " busy" : "");
  key.disabled = !known || STATE_STALE;
  key.textContent = known ? "MUTE" : "MUTE ?";
  key.setAttribute("aria-pressed", known ? String(c.muted) : "mixed");
  key.title = STATE_STALE ? "Dienst nicht erreichbar – gesperrt"
    : !known ? "Mute-Status unbekannt – gesperrt"
    : c.muted ? "Gemutet – klicken zum Unmuten" : "Live – klicken zum Muten";
  const alwaysOn = card.querySelector("[data-always-on-channel]");
  if (c.main_output) {
    // The main L/R output is always left alone by groups and solo.
    alwaysOn.checked = true;
    alwaysOn.disabled = true;
    const control = alwaysOn.closest(".always-on-control");
    control.classList.add("fixed");
    control.title = "Hauptausgang: Gruppen und Solo schalten ihn nie";
  } else {
    if (document.activeElement !== alwaysOn) alwaysOn.checked = Boolean(c.always_on);
    alwaysOn.disabled = ALWAYS_ON_BUSY.has(String(ch)) || STATE_STALE;
  }
  applyStripColor(ch);
  syncFader(ch, c);
}

function renderChannels(state) {
  const wrap = $("channels");
  const chans = state.channels || {};
  const keys = Object.keys(chans).map(Number).sort((a, b) => a - b);
  wrap.classList.toggle("stale", STATE_STALE);
  if (keys.join(",") !== CARD_KEYS) {
    CARD_KEYS = keys.join(",");
    wrap.innerHTML = "";
    for (const k of Object.keys(CARDS)) delete CARDS[k];
    for (const k of Object.keys(FADERS)) { clearTimeout(FADERS[k].timer); delete FADERS[k]; }
    for (const ch of keys) wrap.appendChild(buildChannelCard(ch));
  }
  for (const ch of keys) updateChannelCard(ch, chans[ch]);
}
