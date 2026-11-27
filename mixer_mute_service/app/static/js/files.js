// Mixer Mute Service UI (files.js): Files opened straight from a program row, folder browser.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Files shown through a Desktop key -----------------------------------------
// A Desktop step of a program row may show another file through its key:
// "via:<profile id>|<path>" (app/row_files.py); the key says which PC, which
// monitor, full screen, position and fade, the step only the file (the editor
// is in program.js, "Pfad…"). Older rows may still have a step
// "file:<ip>|<path>" (the PC chosen in the row) until a Desktop key is chosen.
// "Durchsuchen…" only works in a browser on the streaming PC itself (the
// service refuses everyone else); elsewhere the path is typed. It opens the
// real Windows dialog there (through the display agent); when that is not
// available, the page's own folder list.

const FILE_CHOOSE = "__file__";
const THIS_PC = "127.0.0.1";
let FILE_BROWSE_AVAILABLE = false;

function isFileAction(value) { return typeof value === "string" && value.startsWith("file:"); }
function isViaAction(value) { return typeof value === "string" && value.startsWith("via:"); }
// A file of either form (the chips and the editor treat them alike).
function isRowFile(value) { return isFileAction(value) || isViaAction(value); }

function parseFileAction(value) {
  const rest = value.slice(5);
  const bar = rest.indexOf("|");
  return bar < 0 ? { target: THIS_PC, path: "" } : { target: rest.slice(0, bar), path: rest.slice(bar + 1) };
}

function parseViaAction(value) {
  const rest = value.slice(4);
  const bar = rest.indexOf("|");
  return bar < 0 ? { profile: "", path: "" } : { profile: rest.slice(0, bar), path: rest.slice(bar + 1) };
}

function rowFilePath(value) {
  return isViaAction(value) ? parseViaAction(value).path : isFileAction(value) ? parseFileAction(value).path : "";
}

function fileBaseName(path) { return path.split(/[\\/]/).filter(Boolean).pop() || path; }

function fileActionLabel(value) { return `Datei · ${fileBaseName(rowFilePath(value))}`; }

// The chip text of a row file: the file and its Desktop key (or that one is missing).
function rowFileChipText(value) {
  const name = fileBaseName(rowFilePath(value));
  if (isFileAction(value)) return `${name} · Desktop-Taste fehlt`;
  const profile = PROGRAM_DESKTOP_PROFILES.find(p => p.id === parseViaAction(value).profile);
  return profile ? `${name} · über ${profile.title}` : `${name} · Desktop-Taste gelöscht`;
}

// The Desktop editor's detection (desktop.js profileKindOf, app/profile_kind.py).
function filePathKind(path) {
  return (profileKindOf(path, false) || { kind: "file" }).kind;
}

function fileKindText(path) {
  if (!path) return "";
  if (!/^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+|\/)/.test(path)) return "Bitte den vollständigen Pfad angeben, z. B. I:\\Musik\\Intro.mp3 (* für einen unbekannten Namensteil)";
  // "*" stands for an unknown part of a name; the newest matching file is used.
  const wild = path.includes("*") ? " · * = unbekannter Namensteil, neueste passende Datei" : "";
  const kind = filePathKind(path);
  if (kind === "audio") return "Spielt ohne Fenster mit mpv" + wild;
  if (kind === "image") return "Zeigt das Bild" + wild;
  if (kind === "program") return "Startet das Programm" + wild;
  return "Öffnet mit der Standard-App von Windows" + wild;
}

// "Logo · Anzeige PC · Monitor 2" for the Desktop key select.
function profileOutputLabel(profile) {
  const pc = targetPcName(profile.target_ip) || profile.target_ip;
  if (profile.kind === "audio") return `${profile.title} · ${pc} · Ton`;
  const monitors = profile.monitors && profile.monitors.length > 1 ? `Monitore ${profile.monitors.join(", ")}` : `Monitor ${profile.monitor}`;
  return `${profile.title} · ${pc} · ${monitors}`;
}

function fileProfileOptions() {
  return PROGRAM_DESKTOP_PROFILES.map(profile => ({ value: profile.id, label: profileOutputLabel(profile) }));
}

// Show nothing for an action field (Playback, Übersetzung and a Liedtext file
// are Desktop steps now; the Liedtext cell only chooses the song).
function syncFileAction(_select) {}

// A path typed or picked in a step's path editor: the step takes it.
function commitFileAction(wrap) {
  if (wrap.classList.contains("step-path-editor")) commitStepPath(wrap);
}

onProgramEditors("change", event => {
  const wrap = event.target.closest(".file-action");
  if (wrap) commitFileAction(wrap);
});
onProgramEditors("input", event => {
  const path = event.target.closest(".step-path-editor .file-path");
  if (!path) return;
  const kind = path.closest(".step-path-editor").querySelector(".file-kind");
  const text = fileKindText(path.value.trim());
  kind.textContent = text || "Leer: die Datei der Taste";
  kind.classList.toggle("bad", text.startsWith("Bitte"));
});
onProgramEditors("click", event => {
  const button = event.target.closest(".file-browse");
  if (button) pickFile(button);
});

// ---- Windows dialog (streaming PC only) ----

function fileStartFolder(wrap) {
  let start = "";
  const typed = wrap.querySelector(".file-path").value.trim();
  try { start = localStorage.getItem("mixer_file_browser_path") || ""; } catch (_) {}
  // Start in the folder of the file already chosen, if any.
  if (typed && /[\\/]/.test(typed)) start = typed.replace(/[\\/][^\\/]*$/, "") || start;
  return start;
}

async function pickFile(button) {
  const wrap = button.closest(".file-action");
  if (button.disabled) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Dialog am PC offen…";
  let result;
  try {
    // Waits until a file is picked on the PC's screen (up to 10 minutes).
    result = await api("POST", "/files/pick", { start: fileStartFolder(wrap) }, { timeout: 630000 });
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
  if (result.ok && result.data.ok !== false) {
    if (result.data.path) pickBrowsedFile(result.data.path, wrap);
    return;  // no path: cancelled
  }
  if (result.data && result.data.fallback) {
    openFileBrowser(wrap);  // no Windows dialog here: the folder list instead
    return;
  }
  toast(result.data.reason === "a file dialog is already open on this PC"
    ? "Am Streaming-PC ist schon ein Datei-Dialog offen"
    : `Datei-Dialog: ${result.data.reason || "HTTP " + result.status}`, false);
}

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
  $("file-dialog").showModal();
  showFolder(fileStartFolder(wrap));
}

function pickBrowsedFile(path, wrap = FILE_BROWSER.wrap) {
  if ($("file-dialog").open) $("file-dialog").close();
  try { localStorage.setItem("mixer_file_browser_path", path.replace(/[\\/][^\\/]*$/, "")); } catch (_) {}
  if (!wrap || !wrap.isConnected) return;
  wrap.querySelector(".file-path").value = path;
  commitFileAction(wrap);
}

$("fb-up").addEventListener("click", () => { if (FILE_BROWSER.parent !== null) showFolder(FILE_BROWSER.parent); });
$("fb-cancel").addEventListener("click", () => $("file-dialog").close());

async function loadFileBrowseAvailable() {
  const result = await api("GET", "/files/browse-available");
  FILE_BROWSE_AVAILABLE = !!(result.ok && result.data.available);
  document.querySelectorAll(".program-action-link").forEach(syncProgramAction);
}
