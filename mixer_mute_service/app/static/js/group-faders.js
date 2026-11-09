// Mixer Mute Service UI (group-faders.js): Small group faders.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

// ---- Small group faders --------------------------------------------------------
// Only a drag that starts on the knob changes the value: a tap on the track
// would otherwise jump the live fader there at once (they save and set right
// away). Keyboard steps still work.
const PERSON_SLIDER_THUMB_PX = 16;  // must match .fader-slider thumb width

function sliderGestureStart(input, e) {
  const r = input.getBoundingClientRect();
  const frac = (Number(input.value) - Number(input.min)) / (Number(input.max) - Number(input.min));
  const thumbX = r.left + PERSON_SLIDER_THUMB_PX / 2 + frac * (r.width - PERSON_SLIDER_THUMB_PX);
  const onThumb = Math.abs(e.clientX - thumbX) <= 22;  // 44 px wide grip
  input.dataset.gesture = onThumb ? "thumb" : "track";
  input.dataset.startValue = input.value;
  PERSON_SLIDER_ACTIVE = true;
  if (!onThumb) e.preventDefault();
}

document.addEventListener("pointerdown", (e) => {
  const input = e.target.closest && e.target.closest("[data-action=person-fader-input]");
  if (input && !input.disabled) sliderGestureStart(input, e);
}, true);
for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, () => {
    if (!PERSON_SLIDER_ACTIVE) return;
    // The range's own change event follows the release; redraw after it.
    setTimeout(() => { PERSON_SLIDER_ACTIVE = false; renderPersons(); }, 0);
  }, true);
}
document.addEventListener("keydown", (e) => {
  const input = e.target.closest && e.target.closest("[data-action=person-fader-input]");
  if (input) input.dataset.gesture = "key";
}, true);

document.addEventListener("input", (e) => {
  const input = e.target.closest("[data-action=person-fader-input]");
  if (!input) return;
  if (input.dataset.gesture === "track") input.value = input.dataset.startValue;
  const label = input.parentElement.querySelector(".fader-label");
  if (label) label.textContent = formatDb(Number(input.value));
});

document.addEventListener("change", (event) => {
  const t = event.target;
  if (t.matches("[data-action=person-channel-description]")) return savePersonChannelDescription(t);
  if (t.matches("[data-always-on-channel]")) return setChannelAlwaysOn(Number(t.dataset.alwaysOnChannel), t.checked);
  if (t.matches("[data-action=person-fader-input]")) {
    const gesture = t.dataset.gesture;
    delete t.dataset.gesture;
    if (gesture === "track") {
      t.value = t.dataset.startValue;
      toast("Zum Verstellen den Griff ziehen", false);
      return;
    }
    setPersonChannelFaderValue(t.dataset.pid, Number(t.dataset.ch), Number(t.value));
  }
});

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const a = btn.dataset.action;
  const ch = btn.dataset.channel;
  switch (a) {
    case "channel-toggle": return toggleChannel(ch);
    case "channel-rename": return startRenameChannel(Number(ch), btn.dataset.name);
    case "person-mute":        return mutePersonChannels(btn.dataset.pid);
    case "person-solo":        return soloPersonChannels(btn.dataset.pid);
    case "person-use-all":     return useAllPersonFaders(btn.dataset.pid);
    case "person-toggle-collapse": return toggleGroupCollapsed(btn.dataset.pid);
    case "person-duplicate":   return duplicatePerson(btn.dataset.pid);
    case "person-settings":    return openSettingsDialog(btn.dataset.pid);
    case "person-rename":      return startRenamePerson(btn.dataset.pid, btn.dataset.name || "");
    case "person-delete": {
      const person = loadPersons().find(p => p.id === btn.dataset.pid);
      const label = person ? `Mute Gruppe „${person.name}“ löschen?` : "Diese Mute Gruppe löschen?";
      confirmDelete(btn, label).then(ok => { if (ok) deletePerson(btn.dataset.pid); });
      return;
    }
    case "person-set-fader":   return setPersonChannelFader(btn.dataset.pid, Number(btn.dataset.ch));
    case "person-use-fader":   return usePersonChannelFader(btn.dataset.pid, Number(btn.dataset.ch));
    case "person-remove-ch":   return removePersonChannel(btn.dataset.pid, Number(btn.dataset.ch));
    case "person-add-ch": {
      const sel = document.getElementById(`add-ch-${btn.dataset.pid}`);
      addPersonChannel(btn.dataset.pid, sel ? Number(sel.value) : null);
      return;
    }
  }
});


$("new-person-btn").addEventListener("click", openPersonCreateRow);
$("new-snapshot-btn").addEventListener("click", openSnapshotCreateRow);
$("snapshot-cancel-btn").addEventListener("click", closeSnapshotCreateRow);
$("snapshot-save-btn").addEventListener("click", saveCurrentMuteSnapshot);
$("snapshot-name").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); saveCurrentMuteSnapshot(); }
  if (event.key === "Escape") closeSnapshotCreateRow();
});
$("create-person-cancel").addEventListener("click", closePersonCreateRow);
$("create-person-save").addEventListener("click", savePerson);
$("new-person-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); savePerson(); }
  if (e.key === "Escape") { closePersonCreateRow(); }
});
$("panic-btn").addEventListener("click", () => {
  const path = SERVICE_ENABLED === false ? "/panic/enable" : "/panic/disable";
  act("POST", path, undefined, "panic");
});
$("desktop-profiles").addEventListener("click", (event) => {
  const trigger = event.target.closest("button[data-profile-menu]");
  if (trigger) {
    if (trigger.getAttribute("aria-expanded") === "true") closeTileMenu();
    else openTileMenu(trigger);
    return;
  }
  const open = event.target.closest("button[data-display-profile]");
  if (open && !open.disabled) {
    act("POST", `/desktop/${encodeURIComponent(open.dataset.displayProfile)}/open`, undefined, open.dataset.label);
  }
});
$("desktop-profiles").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-audio-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.audioAction;
  const label = button.dataset.label || button.title;
  act(
    "POST",
    `/desktop/${encodeURIComponent(button.dataset.audioProfile)}/audio/${action}`,
    undefined,
    `${label} ${action === "play" ? "Play" : "Stop"}`,
  );
});
$("camera-add-btn").addEventListener("click", () => openCameraDialog());
$("camera-form").addEventListener("submit", saveCamera);
$("camera-cancel").addEventListener("click", () => $("camera-dialog").close());
$("camera-grid").addEventListener("click", async (event) => {
  const edit = event.target.closest("button[data-camera-edit]");
  if (edit) return openCameraDialog(edit.dataset.cameraEdit);
  const remove = event.target.closest("button[data-camera-delete]");
  if (remove) return deleteCamera(remove.dataset.cameraDelete, remove);
  const preset = event.target.closest("button[data-camera-preset]");
  if (preset) return pressCameraPreset(preset);
});
// Hold-to-move (arrows, zoom): move while the key is held (finger, mouse or
// Enter/Space); releasing, sliding off or losing the pointer stops it.
const cameraMoveKey = event => event.target.closest("button[data-camera-move]");
$("camera-grid").addEventListener("pointerdown", (event) => {
  const key = cameraMoveKey(event);
  if (!key || event.button > 0) return;
  event.preventDefault();
  try { key.setPointerCapture(event.pointerId); } catch (_) { /* release still ends the move */ }
  startCameraMove(key);
});
for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
  $("camera-grid").addEventListener(type, (event) => {
    const key = cameraMoveKey(event);
    if (key) stopCameraMoveKey(key);
  });
}
$("camera-grid").addEventListener("keydown", (event) => {
  const key = cameraMoveKey(event);
  if (!key || (event.key !== "Enter" && event.key !== " ")) return;
  event.preventDefault();
  if (!event.repeat) startCameraMove(key);
});
$("camera-grid").addEventListener("keyup", (event) => {
  const key = cameraMoveKey(event);
  if (key && (event.key === "Enter" || event.key === " ")) stopCameraMoveKey(key);
});
$("camera-grid").addEventListener("focusout", (event) => {
  const key = cameraMoveKey(event);
  if (key && key.classList.contains("active")) stopCameraMoveKey(key);
});
$("camera-grid").addEventListener("contextmenu", (event) => {
  if (cameraMoveKey(event)) event.preventDefault();
});
// Never leave a camera moving when the page loses focus or is hidden.
window.addEventListener("blur", stopAllCameraMoves);
window.addEventListener("pagehide", stopAllCameraMoves);
document.addEventListener("visibilitychange", () => { if (document.hidden) stopAllCameraMoves(); });
$("tile-menu").addEventListener("click", (event) => {
  const item = event.target.closest("button[data-menu-action]");
  if (!item) return;
  const profileId = TILE_MENU_PROFILE;
  // The tile's "⋮" key anchors the delete question once the menu is gone.
  const trigger = document.querySelector('[data-profile-menu][aria-expanded="true"]');
  closeTileMenu();
  if (item.dataset.menuAction === "edit") openProfileDialog(profileId);
  else if (item.dataset.menuAction === "duplicate") duplicateDesktopProfile(profileId);
  else if (item.dataset.menuAction === "delete") deleteDesktopProfile(profileId, trigger);
});
$("tile-menu").addEventListener("keydown", (event) => {
  const items = [...$("tile-menu").querySelectorAll("button")];
  const i = items.indexOf(document.activeElement);
  if (event.key === "Escape") { event.preventDefault(); closeTileMenu(true); }
  else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(i + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  } else if (event.key === "Tab") closeTileMenu();
});
// Close on any click elsewhere, and when the page scrolls or resizes.
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest("#tile-menu, [data-profile-menu]")) closeTileMenu();
});
window.addEventListener("scroll", () => closeTileMenu(), { passive: true });
window.addEventListener("resize", () => closeTileMenu());
$("new-profile-btn").addEventListener("click", () => openProfileDialog(null));
$("target-pcs-btn").addEventListener("click", openTargetPcsDialog);
document.addEventListener("click", event => {
  if (event.target.closest("[data-open-target-pcs]")) openTargetPcsDialog();
});
$("target-pcs-form").addEventListener("submit", saveTargetPcs);
$("tp-cancel").addEventListener("click", () => $("target-pcs-dialog").close());
$("profile-form").addEventListener("submit", saveProfile);
$("pf-cancel").addEventListener("click", () => $("profile-dialog").close());
$("pf-title").addEventListener("input", () => {
  if (PROFILE_FORM.mode === "create" && !PROFILE_FORM.idTouched) $("pf-id").value = slugifyProfileId($("pf-title").value.trim());
});
$("pf-id").addEventListener("input", () => { PROFILE_FORM.idTouched = true; });
$("pf-fullscreen").addEventListener("change", syncProfileForm);
$("pf-fade").addEventListener("change", syncProfileForm);
$("profile-form").addEventListener("click", (event) => {
  const kind = event.target.closest("[data-kind]");
  const monitor = event.target.closest("[data-monitor]");
  const pos = event.target.closest("[data-pos]");
  if (kind) PROFILE_FORM.kind = kind.dataset.kind;
  else if (monitor) PROFILE_FORM.monitor = Number(monitor.dataset.monitor);
  else if (pos && !pos.disabled) PROFILE_FORM.position = pos.dataset.pos;
  else return;
  syncProfileForm();
});
// Arrow keys move between the options of a radio group.
$("profile-form").addEventListener("keydown", (event) => {
  const item = event.target.closest("[role=radio]");
  if (!item || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
  const items = [...item.closest("[role=radiogroup]").querySelectorAll("[role=radio]:not(:disabled)")];
  const step = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
  const next = items[(items.indexOf(item) + step + items.length) % items.length];
  event.preventDefault();
  next.click();
  next.focus();
});
