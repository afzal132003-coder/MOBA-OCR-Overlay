/*
 * BGMI RESULTS push -- goes in the SCORESHEET.
 * ================================================================
 * Its own script, in its own spreadsheet, with its own deployment and
 * its own URL. The live alive table is handled by bgmi_alive_push.gs,
 * pasted into the LIVE STATUS sheet. Neither script can touch the
 * other's file, which is the point: a script bound to a spreadsheet
 * needs no permission to reach outside it, because it never does.
 *
 * WHAT IT WRITES, and nothing else: finishes into column G and rank
 * into column I. The points, the totals, the team names and the logo
 * paths are the sheet's own formulas, and a push that touched them
 * would overwrite the thing that makes the sheet worth having.
 *
 * THE LAYOUT. One block per game, stacked down the page:
 *
 *            GAME 1            GAME 2            GAME 3
 *   banner   row  2            row 23            row 44
 *   headers  row  3            row 24            row 45
 *   data     rows 4-19         rows 25-40        rows 46-61
 *
 * So a game's first data row is FIRST_ROW + (game - 1) * STRIDE, and
 * the slot picks the row within the block.
 *
 * SETUP
 *   1. Open the SCORESHEET -> Extensions -> Apps Script.
 *   2. Delete what is there, paste this whole file in, Save.
 *   3. Run `findTheTab` from the function dropdown FIRST. This workbook
 *      has seventeen tabs; it scans them and names the ones that
 *      actually look like a game-block sheet, with their gids.
 *   4. Set SHEET_GID (or SHEET_NAME) to the one it found, Save, then
 *      run `authorize` to confirm.
 *   5. Deploy -> New deployment -> Web app -> Execute as: Me ->
 *      Who has access: Anyone with the link -> Deploy.
 *   6. Paste that URL into the dashboard's BGMI tab, "Results webhook".
 *
 *   After ANY later edit: Deploy -> Manage deployments -> pencil ->
 *   Version: New version -> Deploy.
 */

var SCRIPT_VERSION = "2026-10-02.1-bgmi-results";

// Which tab holds the game blocks. Leave SHEET_NAME blank and set
// SHEET_GID from `findTheTab` -- a gid never changes, where a tab name
// changes the moment somebody renames it.
var SHEET_NAME = "";
var SHEET_GID = null;

var SLOT_COLUMN = "D";       // the block's own slot numbers
var FINISH_COLUMN = "G";     // finishes (kills)
var RANK_COLUMN = "I";       // placement
var FIRST_ROW = 4;           // first data row of GAME 1
var ROWS_PER_GAME = 16;      // teams per block
var GAME_STRIDE = 21;        // row 4 -> row 25


function doPost(e) {
  var result = { ok: false, version: SCRIPT_VERSION, matched: 0, total: 0 };
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.tab) SHEET_NAME = String(body.tab);
    pushResults_(body, result);
  } catch (err) {
    result.error = String(err);
  }
  return ContentService.createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}


function pushResults_(body, result) {
  var game = parseInt(body.game, 10);
  if (isNaN(game) || game < 1) { result.error = "No game number given."; return; }

  var sheet = pickSheet_(SpreadsheetApp.getActiveSpreadsheet(), SHEET_NAME, SHEET_GID);
  if (!sheet) {
    result.error = 'No tab matched name "' + SHEET_NAME + '" or gid ' + SHEET_GID +
      '. Run findTheTab() in the script editor.';
    return;
  }

  var first = FIRST_ROW + (game - 1) * GAME_STRIDE;
  var finIdx = colToIndex_(FINISH_COLUMN);
  var rankIdx = colToIndex_(RANK_COLUMN);

  // Rows are found by READING the block's own slot column, never by
  // counting down from the top -- the slots here start at 3, and a
  // reordered sheet or a different lobby size would otherwise be written
  // into silently and wrongly.
  var slots = sheet.getRange(first, colToIndex_(SLOT_COLUMN), ROWS_PER_GAME, 1).getValues();
  var rowOf = {};
  for (var i = 0; i < slots.length; i++) {
    var n = parseInt(String(slots[i][0]).replace(/[^0-9]/g, ""), 10);
    if (!isNaN(n)) rowOf[n] = first + i;
  }

  result.tab = sheet.getName();
  result.game = game;
  result.blockRows = first + "-" + (first + ROWS_PER_GAME - 1);
  result.slotsInBlock = Object.keys(rowOf).length;

  if (!result.slotsInBlock) {
    result.error = "No slot numbers in column " + SLOT_COLUMN + " rows " +
      result.blockRows + " of tab \"" + sheet.getName() +
      "\". Wrong tab, or the wrong game number.";
    return;
  }

  var rows = body.rows || [];
  result.total = rows.length;
  var missed = [];

  rows.forEach(function (row) {
    var sheetRow = rowOf[parseInt(row.slot, 10)];
    if (!sheetRow) { missed.push(row.slot); return; }
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


function pickSheet_(book, name, gid) {
  if (name) {
    var byName = book.getSheetByName(name);
    if (byName) return byName;
  }
  if (gid || gid === 0) {
    var all = book.getSheets();
    for (var i = 0; i < all.length; i++) if (all[i].getSheetId() === gid) return all[i];
  }
  return null;
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


/* RUN THIS FIRST.
 *
 * Seventeen tabs, and the gid in a pasted URL is whichever one happened
 * to be open at the time -- which is how MVP.D3 got configured as the
 * results tab and reported its column D as "0, M1, KILL". Trying them by
 * hand is seventeen guesses; this is one run.
 *
 * A tab qualifies only if it has a column of consecutive numbers where
 * GAME 1's teams should be AND more numbers one stride below, because
 * that second block is what distinguishes a stack of game blocks from an
 * ordinary list of teams.
 */
function findTheTab() {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  Logger.log("Scoresheet: " + book.getName());
  Logger.log("Looking for a tab laid out as game blocks: " + ROWS_PER_GAME +
             " rows from row " + FIRST_ROW + ", repeating every " + GAME_STRIDE + ".");
  Logger.log("");

  var sheets = book.getSheets(), found = 0;
  for (var i = 0; i < sheets.length; i++) {
    var sh = sheets[i];
    if (sh.getLastRow() < FIRST_ROW + ROWS_PER_GAME) continue;
    var width = Math.min(sh.getLastColumn(), 15);
    for (var c = 1; c <= width; c++) {
      var vals = sh.getRange(FIRST_ROW, c, ROWS_PER_GAME, 1).getValues();
      var nums = [], ok = 0;
      for (var r = 0; r < vals.length; r++) {
        var n = parseInt(String(vals[r][0]).replace(/[^0-9]/g, ""), 10);
        if (!isNaN(n) && n >= 1 && n <= 40) { nums.push(n); ok++; }
      }
      if (ok < ROWS_PER_GAME * 0.9) continue;
      var rising = 0;
      for (var j = 1; j < nums.length; j++) if (nums[j] === nums[j - 1] + 1) rising++;
      if (rising < ROWS_PER_GAME - 3) continue;

      var row2 = FIRST_ROW + GAME_STRIDE, second = 0;
      if (sh.getLastRow() >= row2 + ROWS_PER_GAME) {
        var v2 = sh.getRange(row2, c, ROWS_PER_GAME, 1).getValues();
        for (var k = 0; k < v2.length; k++) {
          var m = parseInt(String(v2[k][0]).replace(/[^0-9]/g, ""), 10);
          if (!isNaN(m)) second++;
        }
      }
      found++;
      Logger.log(">>> \"" + sh.getName() + "\"  gid " + sh.getSheetId());
      Logger.log("      column " + indexToCol_(c) + " runs " + nums[0] + " to " +
                 nums[nums.length - 1] + " (" + rising + " consecutive steps)");
      Logger.log("      " + second + " of " + ROWS_PER_GAME +
                 " numbers in the GAME 2 block too" +
                 (second >= ROWS_PER_GAME * 0.9 ? "   <-- this is the one" : ""));
      Logger.log("");
      break;
    }
  }
  if (!found) {
    Logger.log("No tab matched. Either FIRST_ROW/ROWS_PER_GAME/GAME_STRIDE are");
    Logger.log("wrong for this workbook, or the slot column is past column 15.");
  } else {
    Logger.log("Set SHEET_GID to the gid above, Save, then run authorize().");
  }
}


function authorize() {
  var sheet = pickSheet_(SpreadsheetApp.getActiveSpreadsheet(), SHEET_NAME, SHEET_GID);
  if (!sheet) {
    Logger.log("No tab matched. Run findTheTab() first and set SHEET_GID.");
    return;
  }
  Logger.log("Spreadsheet: " + SpreadsheetApp.getActiveSpreadsheet().getName());
  Logger.log("Writing into tab: " + sheet.getName() + " (gid " + sheet.getSheetId() + ")");
  Logger.log("Finishes -> " + FINISH_COLUMN + ", rank -> " + RANK_COLUMN + ", nothing else.");
  Logger.log("");
  for (var g = 1; g <= 3; g++) {
    var first = FIRST_ROW + (g - 1) * GAME_STRIDE;
    var slots = sheet.getRange(first, colToIndex_(SLOT_COLUMN), ROWS_PER_GAME, 1).getValues();
    Logger.log("GAME " + g + "  rows " + first + "-" + (first + ROWS_PER_GAME - 1) +
               "  slots: " + slots.map(function (r) { return r[0]; }).join(", "));
  }
  Logger.log("");
  Logger.log("Each GAME line should read the slot numbers for that game's block.");
}
