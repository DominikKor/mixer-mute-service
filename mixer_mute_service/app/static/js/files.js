// Mixer Mute Service UI (files.js): Files opened straight from a program row, folder browser.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Files opened straight from a program row ------------------------------
// An action can be "file:<ip>|<path>": the display agent on that PC plays
// music files with mpv, starts .exe files and opens anything else with its
// default app. The folder browser only works in a browser on the streaming
// PC itself (the service refuses everyone else); elsewhere the path is typed.

const FILE_CHOOSE = "__file__";
const THIS_PC = "127.0.0.1";
const AUDIO_EXT = /\.(mp3|wav|m4a|aac|flac|ogg|opus|wma)$/i;
let FILE_BROWSE_AVAILABLE = false;

function isFileAction(value) { return typeof value === "string" && value.startsWith("file:"); }

function parseFileAction(value) {
  const rest = value.slice(5);
  const bar = rest.indexOf("|");
  return bar < 0 ? { target: THIS_PC, path: "" } : { target: rest.slice(0, bar), path: rest.slice(bar + 1) };
}

function fileBaseName(path) { return path.split(/[\\/]/).filter(Boolean).pop() || path; }

function fileActionLabel(value) { return `Datei · ${fileBaseName(parseFileAction(value).path)}`; }

function fileKindText(path) {
  if (!path) return "";
  if (!/^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+|\/)/.test(path)) return "Bitte den vollständigen Pfad angeben, z. B. I:\\Musik\\Intro.mp3 (* für einen unbekannten Namensteil)";
  // "*" stands for an unknown part of a name; the newest matching file is used.
  const wild = path.includes("*") ? " · * = unbekannter Namensteil, neueste passende Datei" : "";
  if (AUDIO_EXT.test(path)) return "Spielt ohne Fenster mit mpv" + wild;
  if (/\.exe$/i.test(path)) return "Startet das Programm" + wild;
  return "Öffnet mit der Standard-App von Windows" + wild;
}

function fileTargetOptions() {
  // The service runs on the Streaming PC, so its own agent is 127.0.0.1.
  const streaming = TARGET_PCS.find(pc => pc.id === "streaming");
  const ips = new Set([
    ...TARGET_PCS.filter(pc => pc.id !== "streaming").map(pc => pc.ip),
    ...PROGRAM_DESKTOP_PROFILES.map(p => p.target_ip), SONG_SETTINGS && SONG_SETTINGS.target_ip,
  ]);
  ips.delete(THIS_PC);
  return [
    { value: THIS_PC, label: `${streaming ? streaming.name : "Streaming PC"} (dieser PC)` },
    ...[...ips].filter(Boolean).sort().map(ip => {
      if (targetPcName(ip)) return { value: ip, label: targetPcLabel(ip) };
      const names = PROGRAM_DESKTOP_PROFILES.filter(p => p.target_ip === ip).map(p => p.title).slice(0, 2);
      return { value: ip, label: names.length ? `${ip} · ${names.join(", ")}` : ip };
    }),
  ];
}

function buildFileAction(label, value) {
  const wrap = document.createElement("div");
  wrap.className = "file-action";
  wrap.hidden = true;
  const path = document.createElement("input");
  path.type = "text";
  path.className = "file-path";
  path.placeholder = "Pfad, z. B. I:\\Musik\\Intro.mp3";
  path.autocomplete = "off";
  path.spellcheck = false;
  path.maxLength = 400;
  path.setAttribute("aria-label", `${label}: Pfad der Datei`);
  const row = document.createElement("div");
  row.className = "file-action-row";
  const target = document.createElement("select");
  target.className = "file-target";
  target.setAttribute("aria-label", `${label}: auf welchem PC`);
  const browse = document.createElement("button");
  browse.type = "button";
  browse.className = "file-browse";
  browse.textContent = "Durchsuchen…";
  browse.title = "Datei auf dem Streaming-PC auswählen";
  row.append(target, browse);
  const kind = document.createElement("div");
  kind.className = "file-kind";
  wrap.append(path, row, kind);
  return wrap;
}

// Show the editor for a file action and fill it from the select's value
// (unless the user is typing in it).
function syncFileAction(select) {
  const wrap = select.parentElement.querySelector(".file-action");
  if (!wrap) return;
  const value = select.value;
  const show = isFileAction(value) || value === FILE_CHOOSE;
  wrap.hidden = !show;
  if (!show) return;
  const path = wrap.querySelector(".file-path");
  const target = wrap.querySelector(".file-target");
  const browse = wrap.querySelector(".file-browse");
  const current = isFileAction(value) ? parseFileAction(value) : null;
  const typing = wrap.contains(document.activeElement);
  setSelectOptions(target, fileTargetOptions(), "Ziel-PC", typing ? target.value : (current ? current.target : (target.value || THIS_PC)));
  if (!target.value) target.value = THIS_PC;
  if (!typing && current) path.value = current.path;
  browse.hidden = !FILE_BROWSE_AVAILABLE;
  const text = fileKindText(path.value.trim());
  const kind = wrap.querySelector(".file-kind");
  kind.textContent = text;
  kind.classList.toggle("bad", text.startsWith("Bitte"));
}

// Path or PC changed: store "file:<ip>|<path>" in the action select and save.
function commitFileAction(wrap) {
  const select = wrap.parentElement.querySelector("select[data-program-field]");
  const path = wrap.querySelector(".file-path").value.trim();
  const target = wrap.querySelector(".file-target").value || THIS_PC;
  const kind = wrap.querySelector(".file-kind");
  const text = fileKindText(path);
  kind.textContent = text;
  kind.classList.toggle("bad", text.startsWith("Bitte"));
  if (!path || text.startsWith("Bitte")) return;
  const value = `file:${target}|${path}`;
  setSelectOptions(select, programActionOptions(value), "Keine Aktion", value);
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

$("program-items").addEventListener("change", event => {
  const select = event.target.closest("select[data-program-field]");
  if (select && select.parentElement.querySelector(".file-action")) {
    syncFileAction(select);
    if (select.value === FILE_CHOOSE) select.parentElement.querySelector(".file-path").focus();
  }
  const wrap = event.target.closest(".file-action");
  if (wrap) commitFileAction(wrap);
});
$("program-items").addEventListener("input", event => {
  const path = event.target.closest(".file-path");
  if (!path) return;
  const kind = path.parentElement.querySelector(".file-kind");
  const text = fileKindText(path.value.trim());
  kind.textContent = text;
  kind.classList.toggle("bad", text.startsWith("Bitte"));
});
$("program-items").addEventListener("click", event => {
  const button = event.target.closest(".file-browse");
  if (button) openFileBrowser(button.closest(".file-action"));
});

// ---- folder browser (streaming PC only) ----

const FILE_BROWSER = { wrap: null, path: "", parent: null };

async function showFolder(path) {
  const result = await api("GET", `/files/browse?path=${encodeURIComponent(path)}`);
  $("fb-error").hidden = result.ok;
  if (!result.ok) {
    $("fb-error").textContent = result.data.reason || `HTTP ${result.status}`;
    if (path) return showFolder("");  // a remembered folder is gone: back to the drives
    return;
  }
  const data = result.data;
  FILE_BROWSER.path = data.path;
  FILE_BROWSER.parent = data.parent;
  $("fb-path").textContent = data.path || "Laufwerke";
  $("fb-up").disabled = data.parent === null;
  try { if (data.path) localStorage.setItem("mixer_file_browser_path", data.path); } catch (_) {}
  const list = $("fb-list");
  list.replaceChildren();
  const item = (icon, name, className, onClick) => {
    const button = document.createElement("button");
    button.type = "button";
    if (className) button.className = className;
    const i = document.createElement("span");
    i.className = "fb-icon";
    i.textContent = icon;
    const n = document.createElement("span");
    n.textContent = name;
    button.append(i, n);
    button.addEventListener("click", onClick);
    list.appendChild(button);
  };
  for (const dir of data.dirs) item("▸", dir.name, "", () => showFolder(dir.path));
  for (const file of data.files) {
    item(file.kind === "audio" ? "♪" : file.kind === "program" ? "▶" : "·", file.name,
      file.kind === "audio" ? "fb-audio" : "", () => pickBrowsedFile(file.path));
  }
  if (!data.dirs.length && !data.files.length) {
    const empty = document.createElement("div");
    empty.className = "fb-empty";
    empty.textContent = "Leerer Ordner";
    list.appendChild(empty);
  }
  list.scrollTop = 0;
}

function openFileBrowser(wrap) {
  FILE_BROWSER.wrap = wrap;
  let start = "";
  const typed = wrap.querySelector(".file-path").value.trim();
  try { start = localStorage.getItem("mixer_file_browser_path") || ""; } catch (_) {}
  // Start in the folder of the file already chosen, if any.
  if (typed && /[\\/]/.test(typed)) start = typed.replace(/[\\/][^\\/]*$/, "") || start;
  $("file-dialog").showModal();
  showFolder(start);
}

function pickBrowsedFile(path) {
  const wrap = FILE_BROWSER.wrap;
  $("file-dialog").close();
  if (!wrap || !wrap.isConnected) return;
  wrap.querySelector(".file-path").value = path;
  // A file picked here lies on the streaming PC, so it runs there.
  const target = wrap.querySelector(".file-target");
  setSelectOptions(target, fileTargetOptions(), "Ziel-PC", THIS_PC);
  commitFileAction(wrap);
}

$("fb-up").addEventListener("click", () => { if (FILE_BROWSER.parent !== null) showFolder(FILE_BROWSER.parent); });
$("fb-cancel").addEventListener("click", () => $("file-dialog").close());

async function loadFileBrowseAvailable() {
  const result = await api("GET", "/files/browse-available");
  FILE_BROWSE_AVAILABLE = !!(result.ok && result.data.available);
  document.querySelectorAll("#program-items select[data-program-field]").forEach(syncFileAction);
}
