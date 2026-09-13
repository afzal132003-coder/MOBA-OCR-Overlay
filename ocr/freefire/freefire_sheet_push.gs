/*
 * Free Fire broadcast sheet push -- receiving end.
 * ---------------------------------------------------------------
 * Two jobs, two tabs, one deployment.
 *
 *   ALIVE   (live, automatic)  Paired with push_sidetable_to_sheet() in
 *           freefire_engine.py. That side POSTs the SAME live alive/elim
 *           data the Alive Status overlay already shows; this script
 *           ticks the alive checkboxes and writes the elim count where a
 *           person used to do it by hand while watching the stream.
 *
 *   RESULTS (per match, on a button)  Paired with the dashboard's
 *           "Push to RESULTS sheet" button. Writes one match's placement
 *           points and kills into that match's own column pair, so the
 *           copy/paste into the results grid stops being a manual step.
 *           With every game landing in its own columns, the sheet keeps
 *           the full series -- past Booyahs, past scores -- and its own
 *           overall-standings and tiebreak formulas have real data to
 *           work from.
 *
 * Both are matched by TEAM NAME, never by row position: the sheet's own
 * team column is the source of truth for which row a team owns, so
 * nothing depends on the sheet and the dashboard agreeing about order.
 *
 * SETUP (one time, on YOUR sheet):
 *   1. Open the sheet -> Extensions -> Apps Script.
 *   2. Delete whatever's in Code.gs and paste this whole file in.
 *   3. Edit the constants below to match your actual sheet -- tab names,
 *      which column holds the team name, which columns are the alive
 *      checkboxes, where match 1 starts. Everything else works off those.
 *   4. Deploy -> New deployment -> type "Web app" -> Execute as: Me ->
 *      Who has access: Anyone with the link -> Deploy.
 *   5. Copy the URL it gives you (ends in /exec) into the dashboard's
 *      "Broadcast Sheet Push" card (Free Fire -> Live In-Game Ops), Save,
 *      then hit "Send Test Row" -- DEBUG_CELL below should show a fresh
 *      timestamp the moment that succeeds, whether or not "SHEET PUSH
 *      TEST" matches a real team name in your sheet.
 *   6. If you ever change this file, you have to Deploy -> Manage
 *      deployments -> edit -> New version for the change to actually
 *      take effect -- saving alone does not update a live deployment.
 *
 * Nothing here needs a Google Cloud project, a service account, or any
 * credential file -- it runs as your own account, on your own sheet,
 * only reachable by whoever has this exact URL.
 */

// ---- ALIVE tab: the live side table --------------------------------------
var SHEET_NAME = "FF STATS NEW";   // the tab this runs against
var TEAM_COLUMN = "P";             // column holding each row's team name
var ALIVE_START_COLUMN = "Q";      // first of the alive checkbox columns
var ALIVE_COLUMN_COUNT = 4;        // how many alive checkboxes per team (usually the squad size)
var ELIM_COLUMN = "U";             // column holding the elimination count
var DATA_START_ROW = 5;            // first row that actually holds a team (skip headers above it)
var DATA_END_ROW = 16;             // last row that holds a team
var DEBUG_CELL = "Z1";             // any empty cell -- last-received marker, see step 5 above

// ---- RESULTS tab: one column pair per match ------------------------------
var RESULTS_SHEET_NAME = "RESULTS";      // the tab holding the per-match grid
var RESULTS_TEAM_COLUMN = "C";           // column holding each row's team name
var RESULTS_FIRST_MATCH_COLUMN = "D";    // match 1's PLACEMENT column (kills is the next one over)
var RESULTS_MATCH_COLUMN_STRIDE = 3;     // D -> G -> J: two columns used, one spacer
var RESULTS_START_ROW = 4;               // first row that holds a team
var RESULTS_END_ROW = 15;                // last row that holds a team
var RESULTS_MATCH_COUNT = 10;            // how many match column-pairs exist

// What goes in the PLACEMENT column.
//   "points" -- the placement POINTS the result file already assigns
//               (Free Fire's own table: 1st=12, 2nd=9, ... 11th/12th=0),
//               which is what the copy/paste flow was pasting, and what a
//               sheet that totals D+E needs.
//   "rank"   -- the raw finishing position (1, 2, ... 12), for a sheet
//               that converts placement to points with its own formula.
// The push sends both numbers either way, so this is the only thing to
// change if the sheet wants the other one.
var RESULTS_PLACEMENT_VALUE = "points";
// ---------------------------------------------------------------------------


// Handles multi-letter columns -- match 9 and 10 land in AB and AE, which
// a single-character conversion would get wrong.
function colToIndex_(letter) {
  var s = String(letter || "").toUpperCase().replace(/[^A-Z]/g, "");
  var n = 0;
  for (var i = 0; i < s.length; i++) n = n * 26 + (s.charCodeAt(i) - 64);
  return n;
}

function normalize_(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

// Row index (0-based, within the block) for a team name, or -1. Tries the
// full name first and the short name second, so the sheet's team column
// can hold either without anyone having to say which.
function findRow_(names, team, short) {
  var wanted = [normalize_(team), normalize_(short)].filter(function (k) { return k; });
  for (var w = 0; w < wanted.length; w++) {
    for (var i = 0; i < names.length; i++) {
      if (normalize_(names[i][0]) === wanted[w]) return i;
    }
  }
  return -1;
}


function pushAlive_(body, result) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) { result.error = 'No tab named "' + SHEET_NAME + '".'; return sheet; }

  var rows = body.rows || [];
  result.total = rows.length;

  var teamColIdx = colToIndex_(TEAM_COLUMN);
  var aliveStartIdx = colToIndex_(ALIVE_START_COLUMN);
  var elimColIdx = colToIndex_(ELIM_COLUMN);
  var numDataRows = DATA_END_ROW - DATA_START_ROW + 1;

  // Read every team name once up front rather than per-row -- cheap, and
  // avoids re-reading the same column N times for N pushed rows.
  var teamNames = sheet.getRange(DATA_START_ROW, teamColIdx, numDataRows, 1).getValues();

  rows.forEach(function (row) {
    var i = findRow_(teamNames, row.team, row.short);
    if (i < 0) return;
    var sheetRow = DATA_START_ROW + i;

    // aliveCount is left-to-right: first N of the ALIVE_COLUMN_COUNT
    // boxes checked, the rest cleared. null means "unknown right now"
    // (e.g. the squad hasn't been linked to a roster yet) -- left
    // untouched rather than guessed at.
    if (row.aliveCount !== null && row.aliveCount !== undefined) {
      var n = Math.max(0, Math.min(ALIVE_COLUMN_COUNT, row.aliveCount));
      var values = [];
      for (var c = 0; c < ALIVE_COLUMN_COUNT; c++) values.push(c < n);
      sheet.getRange(sheetRow, aliveStartIdx, 1, ALIVE_COLUMN_COUNT).setValues([values]);
    }

    if (row.elims !== null && row.elims !== undefined) {
      sheet.getRange(sheetRow, elimColIdx).setValue(row.elims);
    }

    result.matched++;
  });

  result.ok = true;
  return sheet;
}


function pushResults_(body, result) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(RESULTS_SHEET_NAME);
  if (!sheet) { result.error = 'No tab named "' + RESULTS_SHEET_NAME + '".'; return sheet; }

  var match = Number(body.match);
  if (!(match >= 1 && match <= RESULTS_MATCH_COUNT)) {
    result.error = "Match number " + body.match + " is outside 1.." + RESULTS_MATCH_COUNT + ".";
    return sheet;
  }

  var rows = body.rows || [];
  result.total = rows.length;
  result.match = match;

  // Match 1 sits at RESULTS_FIRST_MATCH_COLUMN, and every match after it
  // is one stride further right: D/E, G/H, J/K, ... The placement column
  // is the pair's first, kills its second.
  var placementCol = colToIndex_(RESULTS_FIRST_MATCH_COLUMN) +
                     (match - 1) * RESULTS_MATCH_COLUMN_STRIDE;
  var killsCol = placementCol + 1;
  var teamColIdx = colToIndex_(RESULTS_TEAM_COLUMN);
  var numDataRows = RESULTS_END_ROW - RESULTS_START_ROW + 1;
  var teamNames = sheet.getRange(RESULTS_START_ROW, teamColIdx, numDataRows, 1).getValues();

  // Built as one block and written in a single setValues call rather than
  // two writes per team: a 12-team push is 24 round trips that way, and
  // Apps Script charges for every one of them.
  var block = [];
  for (var i = 0; i < numDataRows; i++) block.push([null, null]);

  var unmatched = [];
  rows.forEach(function (row) {
    var i = findRow_(teamNames, row.team, row.short);
    if (i < 0) { unmatched.push(row.team); return; }
    var placement = (RESULTS_PLACEMENT_VALUE === "rank") ? row.rank : row.placement;
    block[i] = [
      (placement === null || placement === undefined) ? null : placement,
      (row.kills === null || row.kills === undefined) ? null : row.kills
    ];
    result.matched++;
  });

  // null leaves a cell alone, so a team the push didn't cover keeps
  // whatever is already there rather than being wiped by this write.
  sheet.getRange(RESULTS_START_ROW, placementCol, numDataRows, 2).setValues(block);

  if (unmatched.length) result.unmatched = unmatched;
  result.ok = true;
  return sheet;
}


function doPost(e) {
  var result = { ok: false, matched: 0, total: 0 };
  var sheet = null;

  try {
    var body = JSON.parse(e.postData.contents);
    // Defaults to the alive push so an engine that predates this file's
    // RESULTS half still works against it unchanged.
    var kind = body.kind || "alive";
    result.kind = kind;
    sheet = (kind === "results") ? pushResults_(body, result) : pushAlive_(body, result);
  } catch (err) {
    result.error = String(err);
  }

  // Written on EVERY call, matched or not -- the one thing that proves
  // the URL is reachable and the script is running at all, independent
  // of whether any team name in this particular payload matched a row.
  try {
    var debugSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
    if (debugSheet) {
      debugSheet.getRange(DEBUG_CELL).setValue(
        new Date().toLocaleString() + " -- " + (result.kind || "?") + " " +
        result.matched + "/" + result.total + " matched" +
        (result.error ? " -- " + result.error : "")
      );
    }
  } catch (err2) { /* the marker is a convenience, never a reason to fail */ }

  return ContentService.createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}
