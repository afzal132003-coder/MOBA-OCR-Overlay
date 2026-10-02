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

// Which tab holds the game blocks.
//
// LEAVE BOTH BLANK. The dashboard sends the tab with every push -- type
// "AxB" (or its gid) into the BGMI tab's "Sub-sheet" box and press Push.
// Nothing in this file needs editing to change match-day, which is the
// point: a value that lives in the script has to be edited and
// REDEPLOYED to change, and a redeploy forgotten mid-event is the
// failure that looks like the push silently doing nothing.
//
// They remain here only as a default for running authorize() by hand.
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
    // The tab comes from the DASHBOARD, not from editing this file. Name
    // or gid, whichever the operator typed.
    if (body.tab) SHEET_NAME = String(body.tab);
    if (body.gid !== undefined && body.gid !== null && body.gid !== "") {
      SHEET_GID = parseInt(body.gid, 10);
    }
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
    var best = null;

    // EVERY column, then the best -- not the first that looks plausible.
    // An earlier version stopped at the first match and kept landing on
    // column A, which on these tabs is the kill-points lookup (1, 2, 3...
    // with points beside it) sitting directly left of the real block.
    // What separates them is the SECOND game block: a lookup table has
    // nothing 21 rows further down, and a stack of game blocks does.
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
      var score = second * 100 + rising;
      if (!best || score > best.score) {
        best = { col: c, score: score, first: nums[0],
                 last: nums[nums.length - 1], rising: rising, second: second };
      }
    }
    if (!best) continue;

    found++;
    Logger.log(">>> \"" + sh.getName() + "\"  gid " + sh.getSheetId());
    Logger.log("      best column " + indexToCol_(best.col) + ": runs " +
               best.first + " to " + best.last + ", " + best.rising +
               " consecutive steps, " + best.second + "/" + ROWS_PER_GAME +
               " in the GAME 2 block");
    // The headers are the decisive evidence. A tab whose row above the
    // data reads SLOT / PATH / TEAM NAME / Finishes / Pts / Rank is the
    // one, and no amount of number-shape guessing beats reading them.
    if (FIRST_ROW > 1) {
      var head = sh.getRange(FIRST_ROW - 1, 1, 1, Math.min(sh.getLastColumn(), 14))
                   .getValues()[0];
      var shown = [];
      for (var h = 0; h < head.length; h++) {
        var t = String(head[h]).trim();
        if (t) shown.push(indexToCol_(h + 1) + "=" + t.substring(0, 14));
      }
      Logger.log("      headers on row " + (FIRST_ROW - 1) + ":  " +
                 (shown.length ? shown.join("  ") : "(none)"));
    }
    Logger.log("");
  }

  if (!found) {
    Logger.log("No tab matched. Either FIRST_ROW/ROWS_PER_GAME/GAME_STRIDE are");
    Logger.log("wrong for this workbook, or the slot column is past column 15.");
    return;
  }
  Logger.log("PICK THE TAB whose headers read SLOT / PATH / TEAM NAME /");
  Logger.log("Finishes / Pts / Rank -- that is the scoresheet block.");
  Logger.log("");
  Logger.log("CHANGE NOTHING IN THIS FILE. Type that tab name into the");
  Logger.log("Sub-sheet box on the dashboard's BGMI tab, pick the game");
  Logger.log("number, and press Push. Match-day changes there, not here --");
  Logger.log("a tab set in this script would need a redeploy to change, and");
  Logger.log("a redeploy forgotten mid-event looks like the push doing");
  Logger.log("nothing at all.");
}


function authorize() {
  var sheet = pickSheet_(SpreadsheetApp.getActiveSpreadsheet(), SHEET_NAME, SHEET_GID);
  if (!sheet) {
    // Nothing configured, which is the NORMAL state -- the dashboard
    // supplies the tab per push. So show the candidates rather than
    // reporting a failure: what the operator needs here is the name to
    // type into the dashboard, not an error.
    Logger.log("No tab is set in this file, which is expected -- the dashboard");
    Logger.log("sends it with each push. Below are the tabs that look like");
    Logger.log("scoresheet blocks. Type one of these NAMES into the BGMI tab's");
    Logger.log('"Sub-sheet" box in the dashboard.');
    Logger.log("");
    findTheTab();
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
