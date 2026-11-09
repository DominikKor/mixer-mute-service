// Mixer Mute Service UI (stage.js): Stage mode (like Stage Manager).
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Stage (like Stage Manager) ---------------------------------------------
// The sections stay the same live elements; only their place changes: the
// stage (main) or a miniature in the rail. Moves are animated FLIP-style.
const STAGE = { on: false, current: null, order: [], thumbs: {}, raf: 0, anims: [] };
// Scaling limit: with a finger keys must stay big enough to tap; a mouse can
// hit smaller ones. Beyond it the stage scrolls (very long sections only).
const STAGE_MIN_FIT = window.matchMedia && matchMedia("(pointer: coarse)").matches ? 0.85 : 0.6;
const STAGE_ANIM_MS = 440;
const STAGE_FADER_MAX = 300, STAGE_FADER_MIN = 96;
const STAGE_GUTTER = 56;          // free strip on the right of scrolling sections (matches --stage-gutter)

// Programm and full-size channel strips keep their size and scroll on the
// stage (shrinking made fields and faders too small); everything else is
// fitted to the screen.
function stageScrolls(panel) {
  const section = panel.dataset.section;
  return section === "program" || (section === "channels" && !$("channels").classList.contains("compact"));
}

function stageReducedMotion() {
  return window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function initStage() {
  const page = document.querySelector(".page");
  STAGE.order = [...page.querySelectorAll(":scope > .panel[data-section]")];
  const rail = document.createElement("nav");
  rail.id = "stage-rail";
  rail.className = "stage-rail";
  rail.setAttribute("aria-label", "Bereiche");
  const main = document.createElement("main");
  main.id = "stage-main";
  main.className = "stage-main";
  page.prepend(rail, main);
  for (const panel of STAGE.order) {
    const section = panel.dataset.section;
    const thumb = document.createElement("div");
    thumb.className = "stage-thumb";
    thumb.tabIndex = 0;
    thumb.setAttribute("role", "button");
    const label = panel.querySelector(".section-toggle").textContent.trim();
    thumb.setAttribute("aria-label", `${label} anzeigen`);
    thumb.innerHTML = `<div class="stage-thumb-screen"></div><span class="stage-thumb-label">${escapeHtml(label)}</span>`;
    thumb.addEventListener("click", () => stageShow(section));
    thumb.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); stageShow(section); }
    });
    STAGE.thumbs[section] = thumb;
  }
  const observer = new ResizeObserver(scheduleStageLayout);
  observer.observe(main);
  observer.observe(rail);
  STAGE.order.forEach(panel => observer.observe(panel));

  const toggle = $("stage-toggle");
  const prefs = loadUiPrefs();
  // The switch is kept in localStorage and in a cookie the server sets (Safari
  // wipes localStorage of a site unused for 7 days, but not that cookie).
  const cookie = readStageCookie();
  toggle.checked = cookie !== null ? cookie : !!prefs.stage;
  if (cookie === null && typeof prefs.stage === "boolean") rememberStage(prefs.stage);
  if (toggle.checked) setStage(true, false);
  toggle.addEventListener("change", () => {
    const p = loadUiPrefs();
    p.stage = toggle.checked;
    saveUiPrefs(p);
    rememberStage(toggle.checked);
    setStage(toggle.checked, true);
  });
}

function readStageCookie() {
  const match = document.cookie.match(/(?:^|;\s*)mixer_stage=([01])(?:;|$)/);
  return match ? match[1] === "1" : null;
}

function rememberStage(on) {
  api("POST", "/ui/stage", { on }).catch(() => {});
}

function stagePanel(section) {
  return STAGE.order.find(panel => panel.dataset.section === section);
}

// Move a node only when it is not already there. Moving an element reloads
// any iframe inside it (the camera videos), so nothing is moved needlessly;
// where the browser can move without reloading (moveBefore), that is used.
function stagePlace(parent, node, before = null) {
  if (node.parentNode === parent && node.nextSibling === before) return;
  if (typeof parent.moveBefore === "function" && node.isConnected && parent.isConnected) {
    try { parent.moveBefore(node, before); return; } catch (_) { /* fall back below */ }
  }
  parent.insertBefore(node, before);
}

// Put every section where it belongs for the current state.
function placeStagePanels() {
  const page = document.querySelector(".page");
  const main = $("stage-main"), rail = $("stage-rail");
  if (!STAGE.on) {
    for (const panel of STAGE.order) {
      panel.inert = false;
      panel.classList.remove("stage-flying");
      for (const prop of ["transform", "width", "marginLeft", "marginBottom"]) panel.style[prop] = "";
      stagePlace(page, panel);
    }
    resetStageChannels();
    resetStageCompanion();
    resetStageCameras();
    return;
  }
  // Every section keeps its own thumb in the rail (in page order); the one
  // on stage is just hidden, so only that section ever changes parent.
  for (const panel of STAGE.order) {
    const thumb = STAGE.thumbs[panel.dataset.section];
    if (thumb.parentNode !== rail) rail.appendChild(thumb);
  }
  for (const panel of STAGE.order) {
    const section = panel.dataset.section;
    const thumb = STAGE.thumbs[section];
    const onStage = section === STAGE.current;
    thumb.hidden = onStage;
    panel.inert = !onStage;  // a miniature is not clickable inside
    stagePlace(onStage ? main : thumb.firstElementChild, panel);
  }
}

function setStage(on, animate) {
  if (on === STAGE.on) return;
  const firsts = animate ? stageRects() : null;
  STAGE.on = on;
  document.body.classList.toggle("stage-on", on);
  if (on) {
    const wanted = loadUiPrefs().stageSection;
    STAGE.current = stagePanel(wanted) ? wanted : "channels";
    window.scrollTo(0, 0);
  }
  const shown = STAGE.current && stagePanel(STAGE.current);
  placeStagePanels();
  if (on) layoutStage();
  else if (shown) {
    // Back in the long page, start at the section that was on stage.
    const top = shown.getBoundingClientRect().top + scrollY - document.querySelector("header").offsetHeight - 8;
    window.scrollTo(0, Math.max(0, top));
  }
  if (firsts) stageFlip(firsts);
}

function stageShow(section) {
  if (!STAGE.on || section === STAGE.current || !stagePanel(section)) return;
  const firsts = stageRects();
  STAGE.current = section;
  $("stage-main").scrollTop = 0;
  const p = loadUiPrefs();
  p.stageSection = section;
  saveUiPrefs(p);
  placeStagePanels();
  layoutStage();
  stageFlip(firsts);
}

function scheduleStageLayout() {
  if (!STAGE.on || STAGE.raf) return;
  STAGE.raf = requestAnimationFrame(() => { STAGE.raf = 0; layoutStage(); });
}

// Size the stage, fit every section to it and draw the miniatures.
function layoutStage() {
  if (!STAGE.on) return;
  const main = $("stage-main"), rail = $("stage-rail");
  const W = main.clientWidth, H = main.clientHeight;
  if (!W || !H) return;
  document.querySelector(".page").style.setProperty("--stage-w", `${W}px`);
  for (const panel of STAGE.order) {
    const section = panel.dataset.section;
    const scrolls = stageScrolls(panel);
    const lay = (w) => {
      panel.style.width = `${w}px`;
      if (section === "channels") layoutStageChannels(panel, w, H, scrolls);
      if (section === "companion") layoutStageCompanion(w);
      if (section === "cameras") layoutStageCameras(panel, w, H);
      return panel.offsetHeight;
    };
    let ph = lay(W);
    panel._stageGutter = false;
    if (scrolls && ph > H) {   // it will scroll: make room for the finger strip
      ph = lay(W - STAGE_GUTTER);
      panel._stageGutter = true;
    }
    panel._stageFit = scrolls ? 1 : Math.max(STAGE_MIN_FIT, Math.min(1, H / ph));
    panel._stageHeight = ph;
  }
  // Miniatures keep the stage's proportions and share the rail's height.
  const n = STAGE.order.length - 1;
  const labelH = 26, gap = 12;
  let tw = rail.clientWidth - 8;
  let th = tw * H / W;
  const maxTh = (rail.clientHeight - n * labelH - (n - 1) * gap) / n;
  if (th > maxTh) { th = Math.max(40, maxTh); tw = th * W / H; }
  rail.style.setProperty("--thumb-w", `${Math.floor(tw)}px`);
  rail.style.setProperty("--thumb-h", `${Math.floor(th)}px`);
  for (const panel of STAGE.order) applyStageTransform(panel, W, H, tw);
}

// The transform a section has at rest in its current place.
function stageTransformFor(panel, W, H, tw) {
  const s = panel._stageFit || 1;
  if (panel.parentElement === $("stage-main")) return s < 1 ? `scale(${s})` : "none";
  const k = (tw / W) * s;
  return `translate(${((tw - W * k) / 2).toFixed(2)}px, 0px) scale(${k.toFixed(5)})`;
}

function applyStageTransform(panel, W, H, tw) {
  const onStage = panel.parentElement === $("stage-main");
  const s = panel._stageFit || 1;
  panel.style.transform = stageTransformFor(panel, W, H, tw);
  // On stage a scaled section is centred and takes only its scaled height.
  panel.style.marginLeft = onStage && s < 1 ? `${((W - W * s) / 2).toFixed(1)}px` : "";
  panel.style.marginBottom = onStage && s < 1 ? `${(-(panel._stageHeight || 0) * (1 - s)).toFixed(1)}px` : "";
  if (onStage) {
    const main = $("stage-main");
    main.classList.toggle("scroll", (panel._stageHeight || 0) * s > H + 1);
    main.classList.toggle("gutter", !!panel._stageGutter);
  }
}

// Channels: pick the column count and fader length that show every strip at
// the largest fader without scrolling (compact strips: the widest tiles).
function layoutStageChannels(panel, W, H, fullFaders) {
  const strips = $("channels");
  const cards = [...strips.children].filter(c => c.classList.contains("card"));
  if (!cards.length) return;
  const compact = strips.classList.contains("compact");
  // Narrow gaps on stage: 12 strips (84 px: fader + padding) fit an iPad row.
  const gapX = 6, gapY = 10;
  strips.style.columnGap = `${gapX}px`;
  strips.style.rowGap = `${gapY}px`;
  const head = panel.querySelector(".panel-head").offsetHeight;
  const bodyPad = parseFloat(getComputedStyle(strips.parentElement).paddingBottom) || 0;
  const availH = H - head - bodyPad - 2;
  const fader = !compact && cards[0].querySelector(".fader");
  const faderH = fader ? fader.offsetHeight : 0;
  const fixed = Math.max(...cards.map(c => c.offsetHeight)) - faderH;
  // Ultra rows need a little more width than compact tiles for name + mute key.
  const minW = strips.classList.contains("ultra") ? 128 : compact ? 124 : 80;
  const n = cards.length;
  if (fullFaders || compact) {
    // Full-length faders (the stage scrolls) or compact tiles: as few rows as
    // the width allows, spread evenly over them.
    let most = 1;
    while (most < n && (most + 1) * minW + most * gapX <= W) most++;
    const rows = Math.ceil(n / most);
    const cols = Math.ceil(n / rows);  // same rows, wider strips
    strips.style.maxWidth = "none";
    strips.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    if (fullFaders) strips.style.setProperty("--fader-h", `${STAGE_FADER_MAX}px`);
    return;
  }
  let best = null;
  for (let c = 1; c <= n; c++) {
    if (c > 1 && c * minW + (c - 1) * gapX > W) break;
    const rows = Math.ceil(n / c);
    const per = (availH - (rows - 1) * gapY) / rows;
    const f = compact ? 0 : Math.min(STAGE_FADER_MAX, Math.floor(per - fixed));
    const fits = compact ? per >= fixed : f >= STAGE_FADER_MIN;
    const cand = { c, rows, f, fits };
    if (!best
        || (cand.fits && !best.fits)
        || (cand.fits && best.fits && !compact && cand.f > best.f)
        || (!cand.fits && !best.fits && cand.rows < best.rows)) best = cand;
  }
  strips.style.maxWidth = "none";
  strips.style.gridTemplateColumns = `repeat(${best.c}, minmax(0, 1fr))`;
  if (!compact) strips.style.setProperty("--fader-h", `${best.fits ? best.f : STAGE_FADER_MIN}px`);
}

// Steuerung: categories as tiles in lanes; a category with many keys spans
// several lanes so it is not one long column. Row spans are measured
// (masonry), dense packing fills the gaps.
function layoutStageCompanion(W) {
  const root = $("companion-categories");
  const cats = [...root.children].filter(c => c.classList.contains("companion-category"));
  if (!cats.length) return;
  const gap = 12, laneMin = 230;
  const keys = cats.map(c => c.querySelector(".companion-grid[hidden]") ? 0 : c.querySelectorAll(".companion-key").length);
  const total = keys.reduce((a, b) => a + b, 0);
  const apply = (lanes) => {
    root.style.gridTemplateColumns = `repeat(${lanes}, minmax(0, 1fr))`;
    const perLane = Math.max(1, total / lanes);
    cats.forEach((cat, i) => {
      const span = Math.min(lanes, Math.max(1, Math.ceil(keys[i] / perLane - 0.15)));
      cat.style.gridColumn = `span ${span}`;
    });
    for (const cat of cats) cat.style.gridRowEnd = `span ${Math.ceil((cat.offsetHeight + gap) / 2)}`;
    return root.offsetHeight;
  };
  // A few lane counts; the lowest result wins.
  const most = Math.max(1, Math.floor((W + gap) / (laneMin + gap)));
  let best = most, bestH = Infinity;
  for (let lanes = most; lanes >= Math.max(1, most - 2); lanes--) {
    const h = apply(lanes);
    if (h < bestH - 1) { bestH = h; best = lanes; }
  }
  apply(best);
}

// Kameras: as many videos per row as give the biggest picture, sized so
// that every camera fits the stage without scrolling.
function layoutStageCameras(panel, W, H) {
  const grid = $("camera-grid");
  const cards = [...grid.children].filter(c => c.classList.contains("camera-card"));
  grid.style.gridTemplateColumns = "";
  grid.style.justifyContent = "";
  if (!cards.length) return;
  const gap = 12;
  const other = panel.offsetHeight - grid.offsetHeight;   // section head, status line, padding
  const availH = H - other - 2;
  const n = cards.length;
  // Everything of a card except the 16:9 video: head and PTZ controls. The
  // controls wrap with the card width, so measure again after the first fit.
  for (let pass = 0; pass < 2; pass++) {
    const chrome = Math.max(...cards.map(card =>
      card.offsetHeight - (card.querySelector(".camera-view")?.offsetHeight || 0))) || 52;
    let best = { c: 1, w: 0 };
    for (let c = 1; c <= n; c++) {
      const rows = Math.ceil(n / c);
      const byWidth = (W - (c - 1) * gap) / c;
      const byHeight = ((availH - (rows - 1) * gap) / rows - chrome) * 16 / 9;
      const w = Math.min(byWidth, byHeight);
      if (w > best.w + 0.5) best = { c, w };
    }
    const w = Math.max(160, Math.floor(best.w));
    grid.style.gridTemplateColumns = `repeat(${best.c}, ${w}px)`;
    grid.style.justifyContent = "center";
  }
}

function resetStageCameras() {
  const grid = $("camera-grid");
  if (!grid) return;
  grid.style.gridTemplateColumns = "";
  grid.style.justifyContent = "";
}

function resetStageCompanion() {
  const root = $("companion-categories");
  root.style.gridTemplateColumns = "";
  for (const cat of root.children) { cat.style.gridColumn = ""; cat.style.gridRowEnd = ""; }
}

function resetStageChannels() {
  const strips = $("channels");
  strips.style.maxWidth = "";
  strips.style.gridTemplateColumns = "";
  strips.style.columnGap = "";
  strips.style.rowGap = "";
  strips.style.removeProperty("--fader-h");
}

function stageRects() {
  const rects = new Map();
  for (const panel of STAGE.order) rects.set(panel, panel.getBoundingClientRect());
  return rects;
}

// FLIP: every section starts where it was on screen and glides to its new
// place; the miniature's own scale is part of the end state.
function stageFlip(firsts) {
  STAGE.anims.forEach(a => a.cancel());
  STAGE.anims = [];
  if (stageReducedMotion()) return;
  document.body.classList.add("stage-anim");
  const pending = [];
  for (const panel of STAGE.order) {
    const first = firsts.get(panel);
    const end = panel.style.transform && panel.style.transform !== "none" ? panel.style.transform : "";
    panel.style.transform = "none";
    const origin = panel.getBoundingClientRect();   // untransformed layout box
    panel.style.transform = end;
    const last = panel.getBoundingClientRect();
    if (!first || !last.width || !first.width) continue;
    const r = first.width / last.width;
    const a = { x: last.left - origin.left, y: last.top - origin.top };
    const dx = first.left - origin.left - a.x * r, dy = first.top - origin.top - a.y * r;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(r - 1) < 0.002) continue;
    panel.classList.add("stage-flying");
    const anim = panel.animate([
      { transform: `translate(${dx}px, ${dy}px) scale(${r}) ${end}` },
      { transform: `translate(0px, 0px) scale(1) ${end}` },
    ], { duration: STAGE_ANIM_MS, easing: "cubic-bezier(.2, .8, .2, 1)" });
    STAGE.anims.push(anim);
    pending.push(anim.finished.catch(() => {}).then(() => panel.classList.remove("stage-flying")));
  }
  Promise.all(pending).then(() => {
    if (!STAGE.anims.some(a => a.playState === "running")) document.body.classList.remove("stage-anim");
  });
}

loadCompanionButtons();
initSections();
initStage();
initCompanionEditing();
applyBuildMigrations();
