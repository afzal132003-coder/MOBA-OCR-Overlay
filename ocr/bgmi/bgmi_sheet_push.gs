/*
 * BGMI broadcast sheet push -- receiving end.
 * ---------------------------------------------------------------
 * Paired with push_alive_to_sheet() in bgmi_engine.py. That side sends
 * the same live table the dashboard shows; this side ticks the alive
 * checkboxes and writes the team's elimination count, where a person
 * used to do it by hand while watching the stream.
 *
 * WHY THIS IS NOT THE FREE FIRE SCRIPT WITH A DIFFERENT ID
 *
 * The sheet layout is the same -- four checkboxes from column Q, the
 * elim count in U -- but WHICH ROW a team owns is found a different way,
 * and that is the whole point.
 *
 * Free Fire has no slot number anywhere, so its script matches on the
 * team NAME and carries a pile of machinery for near-misses, short
 * names and spellings. BGMI prints the slot beside every squad, and the
 * sheet is ordered by slot, so the join is a number that both sides
 * already agree on. None of that matching machinery is needed here, and
 * carrying it would mean carrying its failure modes too.
 *
 * Two ways to find the row, in order:
 *   1. SLOT_COLUMN, if you set it -- the slot numbers are read out of
 *      the sheet and matched. Survives the rows being reordered.
 *   2. Positional, otherwise -- row = DATA_START_ROW + slot - 1.
 * Prefer 1. Option 2 is right only while the sheet is in slot order,
 * and it has no way to notice when it stops being.
 *
 * SETUP (one time, on YOUR sheet):
 *   1. Open the sheet -> Extensions -> Apps Script.
 *   2. Paste this whole file in, replacing what is there.
 *   3. Check the constants below against your actual sheet.
 *   4. Save, then pick `authorize` in the function dropdown and Run it
 *      once -- it grants permission and prints what it found, so you can
 *      see the wiring is right before anything is deployed. Read the
 *      Execution log underneath.
 *   5. Deploy -> New deployment -> Web app -> Execute as: Me ->
 *      Who has access: Anyone with the link -> Deploy. Paste the URL
 *      into the dashboard's BGMI tab.
 *
 *   After ANY edit you must Deploy -> Manage deployments -> edit ->
 *   Version: New version -> Deploy. The web app serves the DEPLOYED
 *   version, not what is saved in the editor -- an edit that is saved
 *   but not redeployed changes nothing, which is a confusing hour.
 */

var SCRIPT_VERSION = "2026-10-02.1-bgmi-alive-and-results";

// ---- ALIVE: the live side table, its own spreadsheet ---------------------
var SPREADSHEET_ID = "13F4LTctinqHStzFOpurVH4kdRzrJiPzGDvCLGKOD7C8";
var SHEET_NAME = "";            // tab name; blank = the first tab
var SLOT_COLUMN = "";           // column holding the slot number -- SEE THE NOTE BELOW
var ALIVE_START_COLUMN = "Q";   // first of the four alive checkboxes
var ALIVE_COLUMN_COUNT = 4;
var ELIM_COLUMN = "U";          // the team's elimination count
var DATA_START_ROW = 5;         // first row holding a team
var DATA_END_ROW = 20;          // last row holding a team (16 teams: 5..20)

// SET SLOT_COLUMN. It is not optional for this event.
//
// The positional fallback assumes slot 1 sits on DATA_START_ROW, and this
// lobby's slots run 3 to 18 -- so positional would put every team three
// rows above where it belongs, silently and consistently. Point
// SLOT_COLUMN at whichever column of the live sheet holds the slot
// numbers and the rows are found by reading them instead.

// ---- RESULTS: the scoresheet, a DIFFERENT spreadsheet --------------------
//
// One block per game, laid out identically and stacked down the page:
//
//            GAME 1            GAME 2            GAME 3
//   banner   row  2            row 23            row 44
//   headers  row  3            row 24            row 45
//   data     rows 4-19         rows 25-40        rows 46-61
//
// So a game's first data row is RESULTS_FIRST_ROW + (game - 1) * STRIDE,
// and only two columns are ever written: finishes and rank. Everything
// else in the block -- points, totals, the team name, the logo path --
// is the sheet's own formulas, and nothing here touches them.
var RESULTS_SPREADSHEET_ID = "";   // <-- the SCORESHEET's id, from its URL
var RESULTS_SHEET_NAME = "";       // tab name; the dashboard overrides this
var RESULTS_SLOT_COLUMN = "D";     // the block's own slot numbers
var RESULTS_FINISH_COLUMN = "G";   // finishes  (kills)
var RESULTS_RANK_COLUMN = "I";     // placement
var RESULTS_FIRST_ROW = 4;         // first data row of GAME 1
var RESULTS_ROWS_PER_GAME = 16;    // teams per block
var RESULTS_GAME_STRIDE = 21;      // row 4 -> row 25, so 21


function doPost(e) {
  var result = { ok: false, version: SCRIPT_VERSION, matched: 0, total: 0 };
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.kind === "results") {
      if (body.tab) RESULTS_SHEET_NAME = String(body.tab);
      pushResults_(body, result);
    } else {
      if (body.tab) SHEET_NAME = String(body.tab);
      pushAlive_(body, result);
    }
  } catch (err) {
    result.error = String(err);
  }
  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}


function pushResults_(body, result) {
  if (!RESULTS_SPREADSHEET_ID) {
    result.error = "RESULTS_SPREADSHEET_ID is not set -- paste the scoresheet's id into this script.";
    return;
  }
  var game = parseInt(body.game, 10);
  if (isNaN(game) || game < 1) { result.error = "No game number given."; return; }

  var book = SpreadsheetApp.openById(RESULTS_SPREADSHEET_ID);
  var sheet = RESULTS_SHEET_NAME ? book.getSheetByName(RESULTS_SHEET_NAME)
                                 : book.getSheets()[0];
  if (!sheet) {
    result.error = 'No tab named "' + RESULTS_SHEET_NAME + '". Tabs here: ' +
      book.getSheets().map(function (s) { return s.getName(); }).join(", ");
    return;
  }

  var first = RESULTS_FIRST_ROW + (game - 1) * RESULTS_GAME_STRIDE;
  var slotIdx = colToIndex_(RESULTS_SLOT_COLUMN);
  var finIdx = colToIndex_(RESULTS_FINISH_COLUMN);
  var rankIdx = colToIndex_(RESULTS_RANK_COLUMN);

  // Rows are found by READING the block's own slot column, never by
  // counting down from the top. The slots here start at 3, and a sheet
  // whose rows are reordered or whose lobby is a different size would
  // otherwise be written into silently and wrongly.
  var slots = sheet.getRange(first, slotIdx, RESULTS_ROWS_PER_GAME, 1).getValues();
  var rowOf = {};
  for (var i = 0; i < slots.length; i++) {
    var n = parseInt(String(slots[i][0]).replace(/[^0-9]/g, ""), 10);
    if (!isNaN(n)) rowOf[n] = first + i;
  }

  result.tab = sheet.getName();
  result.game = game;
  result.blockRows = first + "-" + (first + RESULTS_ROWS_PER_GAME - 1);
  result.slotsInBlock = Object.keys(rowOf).length;

  var rows = body.rows || [];
  result.total = rows.length;
  var missed = [];

  rows.forEach(function (row) {
    var slot = parseInt(row.slot, 10);
    var sheetRow = rowOf[slot];
    if (!sheetRow) { missed.push(slot); return; }
    // Only these two cells. Points and totals are the sheet's formulas.
    if (row.finishes !== null && row.finishes !== undefined) {
      sheet.getRange(sheetRow, finIdx).setValue(row.finishes);
    }
    if (row.rank !== null && row.rank !== undefined) {
      sheet.getRange(sheetRow, rankIdx).setValue(row.rank);
    }
    result.matched++;
  });

  if (missed.length) result.unmatchedSlots = missed;
  result.ok = true;
}


function pushAlive_(body, result) {
  var book = SPREADSHEET_ID ? SpreadsheetApp.openById(SPREADSHEET_ID)
                            : SpreadsheetApp.getActiveSpreadsheet();
  var sheet = SHEET_NAME ? book.getSheetByName(SHEET_NAME) : book.getSheets()[0];
  if (!sheet) {
    result.error = 'No tab named "' + SHEET_NAME + '". Tabs here: ' +
      book.getSheets().map(function (s) { return s.getName(); }).join(", ");
    return;
  }

  var rows = body.rows || [];
  result.total = rows.length;
  result.tab = sheet.getName();

  var aliveIdx = colToIndex_(ALIVE_START_COLUMN);
  var elimIdx = colToIndex_(ELIM_COLUMN);
  var count = DATA_END_ROW - DATA_START_ROW + 1;

  // Read the slot column once, not per pushed row.
  var slotRows = null;
  if (SLOT_COLUMN) {
    var raw = sheet.getRange(DATA_START_ROW, colToIndex_(SLOT_COLUMN), count, 1).getValues();
    slotRows = {};
    for (var i = 0; i < raw.length; i++) {
      var n = parseInt(String(raw[i][0]).replace(/[^0-9]/g, ""), 10);
      if (!isNaN(n)) slotRows[n] = DATA_START_ROW + i;
    }
    result.slotsInSheet = Object.keys(slotRows).length;
  }

  var missed = [];
  rows.forEach(function (row) {
    var slot = parseInt(row.slot, 10);
    if (isNaN(slot)) return;

    var sheetRow;
    if (slotRows) {
      sheetRow = slotRows[slot];
      // Named rather than silently dropped. A slot with no row is a team
      // whose alive count never reaches the sheet, and the only symptom
      // of that is a row that quietly never moves.
      if (!sheetRow) { missed.push(slot); return; }
    } else {
      sheetRow = DATA_START_ROW + slot - 1;
      if (sheetRow < DATA_START_ROW || sheetRow > DATA_END_ROW) { missed.push(slot); return; }
    }

    // Ticked left to right: the first N boxes on, the rest cleared. Four
    // alive is all four; one alive is Q only, with R, S and T cleared --
    // which is the same as saying the ticks empty from the right.
    if (row.alive !== null && row.alive !== undefined) {
      var n = Math.max(0, Math.min(ALIVE_COLUMN_COUNT, row.alive));
      var values = [];
      for (var c = 0; c < ALIVE_COLUMN_COUNT; c++) values.push(c < n);
      sheet.getRange(sheetRow, aliveIdx, 1, ALIVE_COLUMN_COUNT).setValues([values]);
    }

    // null means "could not be read this frame", which is not the same as
    // zero. Left alone rather than written, so an unreadable frame cannot
    // wipe a score that was right a second ago.
    if (row.kills !== null && row.kills !== undefined) {
      sheet.getRange(sheetRow, elimIdx).setValue(row.kills);
    }

    result.matched++;
  });

  if (missed.length) result.unmatchedSlots = missed;
  result.ok = true;
}


/* "A" -> 1, "Q" -> 17, "AA" -> 27. */
function colToIndex_(letters) {
  var s = String(letters).toUpperCase();
  var n = 0;
  for (var i = 0; i < s.length; i++) n = n * 26 + (s.charCodeAt(i) - 64);
  return n;
}


/* Run this once by hand after pasting the script in. It asks for the
 * permission doPost cannot ask for later (from inside a web request),
 * and then prints what it can see, so the wiring is checked before
 * anything is deployed rather than after it silently does nothing. */
function authorize() {
  var book = SPREADSHEET_ID ? SpreadsheetApp.openById(SPREADSHEET_ID)
                            : SpreadsheetApp.getActiveSpreadsheet();
  Logger.log("Spreadsheet: " + book.getName());
  Logger.log("Tabs: " + book.getSheets().map(function (s) { return s.getName(); }).join(", "));

  var sheet = SHEET_NAME ? book.getSheetByName(SHEET_NAME) : book.getSheets()[0];
  if (!sheet) { Logger.log('NO TAB NAMED "' + SHEET_NAME + '"'); return; }
  Logger.log("Writing into tab: " + sheet.getName());
  Logger.log("Alive checkboxes: " + ALIVE_START_COLUMN + " .. " +
             String.fromCharCode(colToIndex_(ALIVE_START_COLUMN) + ALIVE_COLUMN_COUNT - 1 + 64) +
             "   elims: " + ELIM_COLUMN +
             "   rows " + DATA_START_ROW + "-" + DATA_END_ROW);

  if (SLOT_COLUMN) {
    var count = DATA_END_ROW - DATA_START_ROW + 1;
    var raw = sheet.getRange(DATA_START_ROW, colToIndex_(SLOT_COLUMN), count, 1).getValues();
    Logger.log("Slot column " + SLOT_COLUMN + " reads: " +
               raw.map(function (r) { return r[0]; }).join(", "));
  } else {
    Logger.log("*** SLOT_COLUMN IS BLANK. Rows would be taken positionally, " +
               "slot 1 -> row " + DATA_START_ROW + ". This lobby's slots start " +
               "at 3, so every team would land three rows above where it " +
               "belongs. Set SLOT_COLUMN before using this. ***");
  }

  Logger.log("");
  Logger.log("---- RESULTS (the scoresheet) ----");
  if (!RESULTS_SPREADSHEET_ID) {
    Logger.log("RESULTS_SPREADSHEET_ID is not set. Paste the scoresheet's id " +
               "(the long string in its URL) and run this again.");
    return;
  }
  var rbook = SpreadsheetApp.openById(RESULTS_SPREADSHEET_ID);
  Logger.log("Scoresheet: " + rbook.getName());
  Logger.log("Tabs: " + rbook.getSheets().map(function (s) { return s.getName(); }).join(", "));
  var rsheet = RESULTS_SHEET_NAME ? rbook.getSheetByName(RESULTS_SHEET_NAME)
                                  : rbook.getSheets()[0];
  if (!rsheet) { Logger.log('NO TAB NAMED "' + RESULTS_SHEET_NAME + '"'); return; }

  // Print the first few game blocks with the slots actually found in each,
  // so the stride is confirmed against the real sheet rather than assumed.
  for (var g = 1; g <= 3; g++) {
    var first = RESULTS_FIRST_ROW + (g - 1) * RESULTS_GAME_STRIDE;
    var slots = rsheet.getRange(first, colToIndex_(RESULTS_SLOT_COLUMN),
                                RESULTS_ROWS_PER_GAME, 1).getValues();
    Logger.log("GAME " + g + "  rows " + first + "-" +
               (first + RESULTS_ROWS_PER_GAME - 1) +
               "  slots: " + slots.map(function (r) { return r[0]; }).join(", "));
  }
  Logger.log("Writing finishes into " + RESULTS_FINISH_COLUMN +
             " and rank into " + RESULTS_RANK_COLUMN + ", nothing else.");
}
