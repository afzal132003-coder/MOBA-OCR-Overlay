/*
 * BGMI ALIVE push -- goes in the LIVE STATUS sheet ("CODM STATS").
 * ================================================================
 * This script belongs to ONE spreadsheet and never touches another.
 * The results push is a separate file, bgmi_results_push.gs, pasted
 * into the scoresheet's own Apps Script. Two scripts, two sheets, two
 * deployments, two URLs.
 *
 * That split is deliberate. A script bound to a spreadsheet can read and
 * write it with no extra permission; reaching a DIFFERENT file by id
 * needs a broader authorization scope, and when that scope is missing
 * the failure arrives as a permission error in the middle of a match.
 * Each script owning exactly one sheet removes that whole class of
 * problem -- and makes it obvious which script wrote which cell.
 *
 * WHAT IT WRITES: one row per slot, four alive checkboxes ticked left to
 * right, and the team's elimination count. Nothing else on the row.
 *
 * SETUP
 *   1. Open the LIVE STATUS sheet -> Extensions -> Apps Script.
 *   2. Delete what is there, paste this whole file in, Save.
 *   3. Run `authorize` from the function dropdown. Read the log: it
 *      prints the tab, the columns, and what each candidate slot column
 *      actually contains, so you can set SLOT_COLUMN from evidence.
 *   4. Deploy -> New deployment -> Web app -> Execute as: Me ->
 *      Who has access: Anyone with the link -> Deploy.
 *   5. Paste that URL into the dashboard's BGMI tab, "Alive webhook".
 *
 *   After ANY later edit: Deploy -> Manage deployments -> pencil ->
 *   Version: New version -> Deploy. The web app serves the DEPLOYED
 *   version, not what is saved in the editor.
 */

var SCRIPT_VERSION = "2026-10-02.1-bgmi-alive";

var SHEET_NAME = "LIVESTATUS";  // blank = the first tab
var SHEET_GID = null;           // or pin it by gid; name wins when both are set

// WHICH ROW A SLOT OWNS.
//
// Column V holds the slot numbers, 3 to 18, which is what the game
// prints. Rows are READ from it rather than counted, so nothing depends
// on the sheet staying in slot order.
//
// (An earlier version concluded there was no slot column and counted
// from FIRST_SLOT instead. That was wrong: the diagnostic it was based
// on stopped at column T, and V is past it. Counting still works as a
// fallback when SLOT_COLUMN is blank, but reading beats counting --
// a reordered sheet silently breaks the one and not the other.)
var SLOT_COLUMN = "V";
var FIRST_SLOT = 3;             // only used when SLOT_COLUMN is blank

// The column holding team names, for authorize() to print beside the
// mapping so it can be checked by eye.
//
// MUST BE A STABLE COLUMN. Column D on this sheet is a live leaderboard
// -- it sorts itself by points, and read twice an hour apart it gave two
// completely different orders. P is the fixed, slot-ordered one and
// matches the scoresheet. Checking the mapping against a column that
// re-sorts itself tells you nothing.
var TEAM_NAME_COLUMN = "P";

var ALIVE_START_COLUMN = "Q";   // first of the four alive checkboxes
var ALIVE_COLUMN_COUNT = 4;
var ELIM_COLUMN = "U";          // the team's elimination count
var DATA_START_ROW = 5;
var DATA_END_ROW = 20;          // 16 teams: rows 5..20


function doPost(e) {
  var result = { ok: false, version: SCRIPT_VERSION, matched: 0, total: 0 };
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.tab) SHEET_NAME = String(body.tab);
    pushAlive_(body, result);
  } catch (err) {
    result.error = String(err);
  }
  return ContentService.createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}


function pushAlive_(body, result) {
  var sheet = pickSheet_(SpreadsheetApp.getActiveSpreadsheet(), SHEET_NAME, SHEET_GID);
  if (!sheet) { result.error = 'No tab named "' + SHEET_NAME + '".'; return; }

  var rows = body.rows || [];
  result.total = rows.length;
  result.tab = sheet.getName();

  var aliveIdx = colToIndex_(ALIVE_START_COLUMN);
  var elimIdx = colToIndex_(ELIM_COLUMN);
  var count = DATA_END_ROW - DATA_START_ROW + 1;

  var rowOf = {};
  if (SLOT_COLUMN) {
    var raw = sheet.getRange(DATA_START_ROW, colToIndex_(SLOT_COLUMN), count, 1).getValues();
    for (var i = 0; i < raw.length; i++) {
      var n = parseInt(String(raw[i][0]).replace(/[^0-9]/g, ""), 10);
      if (!isNaN(n)) rowOf[n] = DATA_START_ROW + i;
    }
    result.slotsInSheet = Object.keys(rowOf).length;
  }

  var missed = [];
  rows.forEach(function (row) {
    var slot = parseInt(row.slot, 10);
    if (isNaN(slot)) return;

    var sheetRow;
    if (SLOT_COLUMN) {
      sheetRow = rowOf[slot];
      if (!sheetRow) { missed.push(slot); return; }
    } else {
      sheetRow = DATA_START_ROW + (slot - FIRST_SLOT);
      if (sheetRow < DATA_START_ROW || sheetRow > DATA_END_ROW) { missed.push(slot); return; }
    }

    // Ticked left to right: the first N boxes on, the rest cleared. Four
    // alive is all four; one alive is Q only, with R, S and T cleared --
    // the same thing as saying the ticks empty from the right.
    if (row.alive !== null && row.alive !== undefined) {
      var n = Math.max(0, Math.min(ALIVE_COLUMN_COUNT, row.alive));
      var values = [];
      for (var c = 0; c < ALIVE_COLUMN_COUNT; c++) values.push(c < n);
      sheet.getRange(sheetRow, aliveIdx, 1, ALIVE_COLUMN_COUNT).setValues([values]);
    }

    // null means "could not be read this frame", which is not zero. Left
    // alone, so an unreadable frame cannot wipe a score that was right a
    // second ago.
    if (row.kills !== null && row.kills !== undefined) {
      sheet.getRange(sheetRow, elimIdx).setValue(row.kills);
    }
    result.matched++;
  });

  if (missed.length) result.unmatchedSlots = missed;
  result.ok = true;
}


function pickSheet_(book, name, gid) {
  if (name) {
    var byName = book.getSheetByName(name);
    if (byName) return byName;
  }
  if (gid || gid === 0) {
    var all = book.getSheets();
    for (var i = 0; i < all.length; i++) if (all[i].getSheetId() === gid) return all[i];
  }
  return name ? null : book.getSheets()[0];
}


function colToIndex_(letters) {
  var s = String(letters).toUpperCase(), n = 0;
  for (var i = 0; i < s.length; i++) n = n * 26 + (s.charCodeAt(i) - 64);
  return n;
}

function indexToCol_(n) {
  var s = "";
  while (n > 0) { var r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = (n - r - 1) / 26; }
  return s;
}


/* Run this by hand after pasting the script in.
 *
 * It does not just say "column B looks right" -- a previous version did
 * exactly that and pointed at a column numbered 1 to 16 while the game's
 * slots run 3 to 18, which is the wrong answer arrived at confidently.
 * So it PRINTS THE CONTENTS of every candidate column instead, and lets
 * the person who can see the sheet decide.
 */
function authorize() {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  Logger.log("Spreadsheet: " + book.getName());
  Logger.log("Tabs: " + book.getSheets().map(
    function (s) { return s.getName() + " (gid " + s.getSheetId() + ")"; }).join(", "));

  var sheet = pickSheet_(book, SHEET_NAME, SHEET_GID);
  if (!sheet) { Logger.log('NO TAB NAMED "' + SHEET_NAME + '"'); return; }
  Logger.log("Writing into tab: " + sheet.getName());
  Logger.log("Alive checkboxes " + ALIVE_START_COLUMN + ".." +
             indexToCol_(colToIndex_(ALIVE_START_COLUMN) + ALIVE_COLUMN_COUNT - 1) +
             ", elims " + ELIM_COLUMN + ", rows " + DATA_START_ROW + "-" + DATA_END_ROW);
  Logger.log("");

  var count = DATA_END_ROW - DATA_START_ROW + 1;

  // THE CHECK THAT MATTERS: which slot lands on which row, with that
  // row's team name beside it. Everything else here is background.
  Logger.log("SLOT -> ROW, with the team currently on that row:");
  Logger.log("");
  var names = sheet.getRange(DATA_START_ROW, colToIndex_(TEAM_NAME_COLUMN),
                             count, 1).getValues();
  var slotCol = SLOT_COLUMN
    ? sheet.getRange(DATA_START_ROW, colToIndex_(SLOT_COLUMN), count, 1).getValues()
    : null;
  for (var i = 0; i < count; i++) {
    var row = DATA_START_ROW + i;
    var slot = slotCol
      ? parseInt(String(slotCol[i][0]).replace(/[^0-9]/g, ""), 10)
      : FIRST_SLOT + i;
    Logger.log("   slot " + (slot < 10 ? " " : "") + slot +
               "  ->  row " + row + "   " + String(names[i][0]).substring(0, 22));
  }
  Logger.log("");
  Logger.log(SLOT_COLUMN
    ? 'Rows are read from column "' + SLOT_COLUMN + '".'
    : "Rows are counted from FIRST_SLOT = " + FIRST_SLOT + ". If the team " +
      "beside each slot above is the team the game shows in that slot, this " +
      "is right. If every team is off by the same amount, change FIRST_SLOT.");
  Logger.log("");

  var width = Math.min(sheet.getLastColumn(), 26);
  Logger.log("For reference, what is in rows " + DATA_START_ROW + "-" +
             DATA_END_ROW + " column by column:");
  for (var c = 1; c <= width; c++) {
    var vals = sheet.getRange(DATA_START_ROW, c, count, 1).getValues();
    var cells = vals.map(function (r) { return String(r[0]).substring(0, 12); });
    if (!cells.filter(function (v) { return v !== ""; }).length) continue;
    Logger.log("  " + indexToCol_(c) + ":  " + cells.join(" | "));
  }
}
