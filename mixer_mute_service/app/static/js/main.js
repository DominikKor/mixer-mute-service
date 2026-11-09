// Mixer Mute Service UI (main.js): Start: first loads and the polls.
// Classic script: all files share one global scope and load in the order
// of index.html; main.js runs last and starts everything.

loadCompanionButtons();
// Desktop keys name their PC once the list is in.
loadTargetPcs().then(() => { DESKTOP_PROFILES_SIGNATURE = ""; });
initSections();
initStage();
initCompanionEditing();
applyBuildMigrations();
loadProgramItems();
poll(refreshProgramState, 5000);
loadSongs().then(() => poll(refreshSongLive, 1500));
loadFileBrowseAvailable();
setInterval(updateProgramCue, 1000);
showBuildId();
normalizeCollapsedGroups();
migrateFaderPresets();
poll(() => refresh({ light: true }), 1000);
poll(pullGroupsFromBackend, 5000);
poll(refreshUpdateButton, 30000);
poll(loadChannelColors, 5000);
loadLights().then(() => poll(refreshLights, LIGHT_POLL_MS));
