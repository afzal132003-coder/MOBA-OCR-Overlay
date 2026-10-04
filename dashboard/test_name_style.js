/* "Team names shown as" -- full or short on the ON-AIR alive table.
 *
 *   node dashboard/test_name_style.js
 *
 * No jsdom, for the reason given in test_arrow_lobby.js: this has to run
 * on the event machine as it stands.
 *
 * WHAT THIS GUARDS
 *
 *   * THE DROPDOWN REACHES THE GRAPHIC. It used to change only the
 *     dashboard's own table; the operator picked Full and the alive table
 *     on air stayed on short names. It now sends settings.aliveNameStyle,
 *     merged, and the overlay follows it.
 *
 *   * NOTHING SET MEANS SHORT. That is what the graphic has always shown,
 *     so an engine that has never seen the setting changes nothing.
 *
 *   * THE DROPDOWN SHOWS WHAT IS ON AIR, read back from the engine, so two
 *     screens cannot each believe something different is going out.
 *
 *   * THE ROOM-SLOT GRID IS REDRAWN ON EVERY SYNC. It was only redrawn on
 *     the rare extras message, so a save that did not stick looked saved.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const dash = fs.readFileSync(path.join(__dirname, "dashboard.html"), "utf8");
const overlay = fs.readFileSync(
  path.join(__dirname, "..", "overlay", "freefire_alive_status.html"), "utf8");

function lift(src, name) {
  const start = src.indexOf("function " + name + "(");
  if (start === -1) throw new Error("not found: " + name);
  let i = src.indexOf("{", start), depth = 0, end = -1;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (depth === 0) { end = j + 1; break; } }
  }
  return src.slice(start, end);
}

let checks = 0;
const failures = [];
function check(label, ok, detail) {
  checks++;
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + label + (ok || !detail ? "" : "  " + detail));
  if (!ok) failures.push(label);
}

// ---------------------------------------------------------------- overlay
console.log("\nthe on-air label");
const ov = vm.createContext({});
vm.runInContext(lift(overlay, "teamLabel"), ov);
const team = { name: "VASIYO ESP", shortName: "VE" };
const row = { teamName: "VASIYO ESP", short: "VE" };
check("nothing set: short, as it has always been",
      ov.teamLabel(team, row, { settings: {} }) === "VE");
check("no settings at all: short",
      ov.teamLabel(team, row, {}) === "VE");
check("short", ov.teamLabel(team, row, { settings: { aliveNameStyle: "short" } }) === "VE");
check("full", ov.teamLabel(team, row, { settings: { aliveNameStyle: "full" } }) === "VASIYO ESP");
check("full prefers the roster's display name",
      ov.teamLabel({ name: "VASIYO ESP", displayName: "Vasiyo Esports", shortName: "VE" }, row,
                   { settings: { aliveNameStyle: "full" } }) === "Vasiyo Esports");
check("full with no roster team: the row's name",
      ov.teamLabel(null, { teamName: "SQUAD 4", short: "S4" },
                   { settings: { aliveNameStyle: "full" } }) === "SQUAD 4");
check("short with no short name anywhere: the full name",
      ov.teamLabel({ name: "BFA" }, { teamName: "BFA" }, { settings: {} }) === "BFA");
check("the label is drawn through teamLabel",
      /const label = teamLabel\(team, row, state\);/.test(overlay));
check("and a changed label is fitted to its box",
      /nameEl\.textContent = label;\s*fitName\(nameEl\);/.test(overlay));

// -------------------------------------------------------------- dashboard
console.log("\nthe dropdown");
function box(value) {
  return { value, listeners: {}, addEventListener(t, f) { this.listeners[t] = f; } };
}
const sent = [];
const sel = box("short");
const ctx = vm.createContext({
  document: {
    activeElement: null,
    getElementById: id => id === "ff_aliveNameStyle" ? sel
      : { textContent: "" },
  },
  localStorage: { setItem() {}, getItem() { return null; } },
  WebSocket: { OPEN: 1 },
  console,
});
vm.runInContext(
  "var ffAliveNameStyle = 'full'; var renders = 0;" +
  "var ffState = { settings: { sheetWebhookUrl: 'keep-me', liveSheetPush: true } };" +
  "var ffWs = { readyState: 1, send: s => sent.push(JSON.parse(s)) };" +
  "function ffRenderAliveGridRows(){ renders++; }" +
  lift(dash, "ffSyncAliveNameStyle"), ctx);
ctx.sent = sent;

// the change handler is an IIFE in the page; run it against the stub box
const iifeStart = dash.indexOf('const box = document.getElementById("ff_aliveNameStyle");');
const iifeOpen = dash.lastIndexOf("(() => {", iifeStart);
const iifeClose = dash.indexOf("})();", iifeStart) + 5;
vm.runInContext(dash.slice(iifeOpen, iifeClose), ctx);

check("first sync: nothing set, so the dropdown says Short",
      (vm.runInContext("ffSyncAliveNameStyle(); ffAliveNameStyle", ctx) === "short") &&
      sel.value === "short");
sel.value = "full";
sel.listeners.change();
const msg = sent[sent.length - 1] || {};
const settings = (((msg.data || {}).freefire) || {}).settings || {};
check("picking Full sends it to the engine", msg.type === "manual_update" &&
      settings.aliveNameStyle === "full", JSON.stringify(msg));
check("merged: the other settings go back untouched",
      settings.sheetWebhookUrl === "keep-me" && settings.liveSheetPush === true,
      JSON.stringify(settings));
check("and the dashboard table relabels at once",
      vm.runInContext("renders", ctx) >= 1);
vm.runInContext("ffState = { settings: { aliveNameStyle: 'full' } }", ctx);
sel.value = "short";
vm.runInContext("ffSyncAliveNameStyle()", ctx);
check("a sync sets the dropdown to what is on air", sel.value === "full");
ctx.document.activeElement = sel;
sel.value = "short";
vm.runInContext("ffState = { settings: {} }; ffSyncAliveNameStyle()", ctx);
check("but never while the operator has it open", sel.value === "short");

console.log("\nthe room-slot grid");
const syncAt = dash.search(/if\(msg\.type === "state_sync"\)\{\r?\n\s*\/\* The Free Fire engine leaves/);
const syncBody = dash.slice(syncAt, dash.indexOf("ffHydrate(ffState);", syncAt));
check("is redrawn on every Free Fire state_sync", /ffDrawRoomSlots\(\)/.test(syncBody));
check("and the name style is read back there too", /ffSyncAliveNameStyle\(\)/.test(syncBody));
check("a redraw from the engine leaves unsaved edits alone",
      /if \(!slots && ffRoomSlotsDirty\) return;/.test(lift(dash, "ffDrawRoomSlots")));

console.log("\n" + checks + " checks, " + failures.length + " failed");
if (failures.length) { console.log("failed: " + failures.join(", ")); process.exit(1); }
