// Mixer Mute Service UI (channels.js): Channel strips: vertical faders and strip colours.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Vertical channel faders ---------------------------------------------
// Only the cap can be dragged (clicking the track never jumps the fader). While
// dragging, the position is sent at most every FADER_SEND_MS and once more on
// release. Every fader command also rewrites the channel's mute flag, so a
// channel whose mute state is unknown cannot be moved at all.

const FADER_SEND_MS = 100;
const FADER_HOLD_MS = 1500;  // after a local move, ignore polled positions this long
const FADER_CAP_PX = 28;     // must match .fader-cap height
const FADERS = {};           // channel -> fader controller state

function faderScaleHtml() {
  return FADER_TICKS.map(db => {
    const top = (1 - dbToFader(db) / FADER_MAX) * 100;
    const label = db === -Infinity ? "−∞" : db > 0 ? `+${db}` : String(db).replace("-", "−");
    return `<div class="fader-tick${db === 0 ? " unity" : ""}" style="top:${top.toFixed(2)}%"><span>${label}</span></div>`;
  }).join("");
}

function faderStripHtml(ch) {
  return `<div class="fader disabled" data-fader="${ch}" role="slider" tabindex="0" aria-orientation="vertical"
      aria-valuemin="0" aria-valuemax="${FADER_MAX}" aria-label="Fader Kanal ${ch}">
      <div class="fader-scale">${faderScaleHtml()}</div>
      <div class="fader-slot"></div>
      <div class="fader-cap"></div>
    </div>
    <div class="fader-db unk">—</div>`;
}

function faderBlockReason(c) {
  if (STATE_STALE) return "Dienst nicht erreichbar";
  if (!LAST_STATE.mixer_connected) return "keine Mixer-Verbindung";
  if (!LAST_STATE.enabled) return "Panic aktiv";
  if (!c || c.fader_value === null || c.fader_value === undefined) return "Faderwert unbekannt";
  if (c.muted === null || c.muted === undefined) return "Mute-Status unbekannt";
  if (!c.safe) return "Kanal unsicher";
  return null;
}

function setFaderVisual(f, value) {
  f.value = value;
  const frac = value === null ? 1 : 1 - value / FADER_MAX;
  f.cap.style.top = `calc(${FADER_CAP_PX / 2}px + ${frac.toFixed(4)} * (100% - ${FADER_CAP_PX}px))`;
  f.db.textContent = formatDb(value);
  f.db.classList.toggle("unk", value === null);
  if (value === null) {
    f.el.removeAttribute("aria-valuenow");
  } else {
    f.el.setAttribute("aria-valuenow", value.toFixed(1));
  }
  f.el.setAttribute("aria-valuetext", formatDb(value));
}

function moveFaderTo(f, value) {
  if (f.failed || f.value === null) return;
  const v = Math.round(Math.max(0, Math.min(FADER_MAX, value)) * 10) / 10;
  if (v === f.value) return;
  setFaderVisual(f, v);
  f.pending = v;
  f.holdUntil = Date.now() + FADER_HOLD_MS;
  pumpFader(f);
}

async function pumpFader(f) {
  if (f.inflight || f.timer || f.pending === null) return;
  const wait = f.lastSent + FADER_SEND_MS - Date.now();
  if (wait > 0) {
    f.timer = setTimeout(() => { f.timer = null; pumpFader(f); }, wait);
    return;
  }
  const value = f.pending;
  f.pending = null;
  f.inflight = true;
  f.lastSent = Date.now();
  const r = await api("POST", `/channels/${f.ch}/fader`, { value });
  f.inflight = false;
  f.holdUntil = Date.now() + FADER_HOLD_MS;
  if (!(r.ok && r.data.ok !== false)) {
    // Stop this gesture and let the next poll show the real position again.
    f.pending = null;
    f.failed = true;
    f.holdUntil = 0;
    toast(`Kanal ${f.ch} Fader: ${r.data.reason || "HTTP " + r.status}`, false);
    return;
  }
  pumpFader(f);
}

function endFaderDrag(f) {
  if (!f.dragging) return;
  f.dragging = false;
  f.el.classList.remove("dragging");
  f.holdUntil = Date.now() + FADER_HOLD_MS;
}

function attachFader(ch, el, dbEl) {
  const f = {
    ch, el, db: dbEl, cap: el.querySelector(".fader-cap"), value: null,
    dragging: false, lastY: 0, holdUntil: 0, pending: null, inflight: false,
    lastSent: 0, timer: null, failed: false, state: null,
  };
  FADERS[ch] = f;
  setFaderVisual(f, null);

  f.cap.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (el.classList.contains("disabled")) {
      toast(`Kanal ${ch} Fader gesperrt: ${faderBlockReason(f.state) || "unsicher"}`, false);
      return;
    }
    f.cap.setPointerCapture(e.pointerId);
    f.dragging = true;
    f.failed = false;
    f.lastY = e.clientY;
    el.classList.add("dragging");
    el.focus({ preventScroll: true });
  });
  f.cap.addEventListener("pointermove", (e) => {
    if (!f.dragging) return;
    // On-screen travel: the stage view may show the strip scaled.
    const travel = (el.clientHeight - FADER_CAP_PX) * (el.getBoundingClientRect().height / el.clientHeight || 1);
    const dy = e.clientY - f.lastY;
    f.lastY = e.clientY;
    const fine = e.shiftKey ? 0.25 : 1;  // Shift = fine adjustment
    moveFaderTo(f, f.value - (dy / travel) * FADER_MAX * fine);
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
    f.cap.addEventListener(type, () => endFaderDrag(f));
  }
  el.addEventListener("keydown", (e) => {
    const steps = { ArrowUp: 10, ArrowDown: -10, PageUp: 50, PageDown: -50 };
    if (!(e.key in steps)) return;
    e.preventDefault();
    if (el.classList.contains("disabled")) return;
    f.failed = false;
    moveFaderTo(f, f.value + steps[e.key] * (e.shiftKey ? 0.1 : 1));
  });
}

function syncFader(ch, c) {
  const f = FADERS[ch];
  if (!f) return;
  f.state = c;
  const reason = faderBlockReason(c);
  const enabled = reason === null;
  f.el.classList.toggle("disabled", !enabled);
  f.el.setAttribute("aria-disabled", String(!enabled));
  f.el.title = enabled ? "Ziehen am Griff (Shift = fein), Pfeiltasten" : `Gesperrt: ${reason}`;
  if (!enabled) endFaderDrag(f);
  const busy = f.dragging || f.inflight || f.timer || f.pending !== null || Date.now() < f.holdUntil;
  if (!busy) {
    const v = c && c.fader_value !== undefined ? c.fader_value : null;
    setFaderVisual(f, v);
  }
}

function migrateFaderPresets() {
  // Presets saved before the fader encoding was fixed hold bytes from the old
  // percent/calibration mapping, which are not real fader positions.
  const key = "mixer_fader_presets_float";
  if (storeGet(key)) return;
  const list = loadPersons();
  let cleared = 0;
  for (const p of list) {
    for (const entry of Object.values(p.channels || {})) {
      if (entry && entry.fader_hex) { entry.fader_hex = null; entry.set_at = null; cleared++; }
    }
  }
  savePersons(list);
  storeRemove("mixer_fader_calibration");
  storeSet(key, "1");
  if (cleared) {
    setTimeout(() => toast(`${cleared} gespeicherte Fader-Werte zurückgesetzt (altes Format). Bitte neu mit „Set“ speichern.`, false), 500);
  }
}

function startRenameChannel(ch, currentName) {
  const titleEl = document.querySelector(`[data-rename-ch="${ch}"]`);
  if (!titleEl || !CARDS[ch]) return;
  const wrap = titleEl.closest(".card-title-wrap");
  if (!wrap) return;
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = currentName;
  input.maxLength = 32;
  const pop = buildColorPopover(ch);
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    pop.remove();
    const val = input.value.trim();
    if (!input.dataset.cancel && val && val !== currentName) {
      const names = loadChannelNames();
      names[String(ch)] = val;
      saveChannelNames(names);
    }
    wrap.innerHTML = "";
    wrap.dataset.name = "";
    renderChannels(LAST_STATE);
  };
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { input.dataset.cancel = "1"; input.blur(); }
  });
  // Delayed, so a tap on a colour that takes the focus away still lands.
  input.addEventListener("blur", () => setTimeout(finish, 200));
  wrap.innerHTML = "";
  wrap.appendChild(input);
  if (CARDS[ch]) CARDS[ch].appendChild(pop);
  input.focus();
  input.select();
}

// ---- Strip colours --------------------------------------------------------
// One optional colour per channel, stored by the service (channel_colors.json)
// so every device shows the same. Purely cosmetic: nothing is sent to the mixer.

const STRIP_COLORS = {
  red: "255,75,58", orange: "255,159,28", yellow: "241,210,28", green: "62,224,127",
  black: "112,119,130", blue: "79,156,255", purple: "166,107,255", pink: "255,107,193",
};
const STRIP_COLOR_NAMES = {
  red: "Rot", orange: "Orange", yellow: "Gelb", green: "Grün",
  black: "Schwarz", blue: "Blau", purple: "Lila", pink: "Pink",
};
let CHANNEL_COLORS = {};

function applyStripColor(ch) {
  const card = CARDS[ch];
  if (!card) return;
  const color = CHANNEL_COLORS[String(ch)];
  if (color && STRIP_COLORS[color]) {
    card.dataset.color = color;
    card.style.setProperty("--strip-rgb", STRIP_COLORS[color]);
  } else {
    delete card.dataset.color;
    card.style.removeProperty("--strip-rgb");
  }
}

async function loadChannelColors() {
  const r = await api("GET", "/channel-colors");
  if (!r.ok || !r.data || typeof r.data.colors !== "object") return;
  CHANNEL_COLORS = r.data.colors;
  for (const ch of Object.keys(CARDS)) applyStripColor(ch);
}

function markSwatches(pop, color) {
  for (const b of pop.querySelectorAll(".swatch")) {
    b.setAttribute("aria-checked", String((b.dataset.color || null) === color));
  }
}

async function setChannelColor(ch, color, pop) {
  const key = String(ch);
  const prev = CHANNEL_COLORS[key] || null;
  if (color) CHANNEL_COLORS[key] = color; else delete CHANNEL_COLORS[key];
  applyStripColor(ch);
  markSwatches(pop, color);
  const r = await api("POST", `/channels/${ch}/color`, { color });
  if (!(r.ok && r.data.ok !== false)) {
    if (prev) CHANNEL_COLORS[key] = prev; else delete CHANNEL_COLORS[key];
    applyStripColor(ch);
    markSwatches(pop, prev);
    toast(`Kanal ${ch} Farbe: ${r.data.reason || "HTTP " + r.status}`, false);
  }
}

function buildColorPopover(ch) {
  const pop = document.createElement("div");
  pop.className = "color-pop";
  pop.setAttribute("role", "radiogroup");
  pop.setAttribute("aria-label", `Farbe Kanal ${ch}`);
  for (const color of [null, ...Object.keys(STRIP_COLORS)]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch" + (color ? "" : " none");
    if (color) b.style.setProperty("--sw", `rgb(${STRIP_COLORS[color]})`);
    b.dataset.color = color || "";
    b.title = color ? STRIP_COLOR_NAMES[color] : "Keine Farbe";
    b.setAttribute("role", "radio");
    // Keep the focus in the name field where the browser allows it.
    b.addEventListener("mousedown", e => e.preventDefault());
    b.addEventListener("click", () => setChannelColor(ch, color, pop));
    pop.appendChild(b);
  }
  markSwatches(pop, CHANNEL_COLORS[String(ch)] || null);
  return pop;
}

function startRenamePerson(personId, currentName) {
  const nameEl = document.querySelector(`[data-rename-person="${personId}"]`);
  if (!nameEl) return;
  const wrap = nameEl.closest(".person-name-wrap");
  if (!wrap) return;
  PERSONS_HTML = null;  // the DOM is edited by hand below: always redraw after
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = currentName;
  input.maxLength = 64;
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const val = input.value.trim();
    input.remove();  // before redrawing: a focused field would hold the redraw back
    if (save && val && val !== currentName) renamePerson(personId, currentName, val);
    renderPersons(true);
  };
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  wrap.innerHTML = "";
  wrap.appendChild(input);
  input.focus();
  input.select();
}
