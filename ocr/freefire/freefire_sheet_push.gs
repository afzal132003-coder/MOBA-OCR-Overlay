/*
 * Free Fire broadcast sheet push -- receiving end.
 * ---------------------------------------------------------------
 * Paired with push_sidetable_to_sheet() in freefire_engine.py. That side
 * POSTs the SAME live alive/elim data the Alive Status overlay already
 * shows; this script's job is just to land it in the right row of your
 * sheet, ticking the alive checkboxes and writing the elim count where a
 * person used to do it by hand while watching the stream.
 *
 * SETUP (one time, on YOUR sheet):
 *   1. Open the sheet -> Extensions -> Apps Script.
 *   2. Delete whatever's in Code.gs and paste this whole file in.
 *   3. Edit the constants below to match your actual sheet -- SHEET_NAME,
 *      which column holds the team name, which columns are the alive
 *      checkboxes, which column is the elim count. Everything else in
 *      this file works off those.
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

// ---- Adjust these to match your actual sheet -----------------------------
var SHEET_NAME = "FF STATS NEW";   // the tab this runs against
var TEAM_COLUMN = "P";             // column holding each row's team name
var ALIVE_START_COLUMN = "Q";      // first of the alive checkbox columns
var ALIVE_COLUMN_COUNT = 4;        // how many alive checkboxes per team (usually the squad size)
var ELIM_COLUMN = "U";             // column holding the elimination count
var DATA_START_ROW = 5;            // first row that actually holds a team (skip headers above it)
var DATA_END_ROW = 16;             // last row that holds a team
var DEBUG_CELL = "Z1";             // any empty cell -- last-received marker, see step 5 above
// ---------------------------------------------------------------------------

function colToIndex_(letter) {
  return letter.toUpperCase().charCodeAt(0) - 64; // "A" -> 1
}

function normalize_(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function doPost(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  var result = { ok: false, matched: 0, total: 0 };

  try {
    var body = JSON.parse(e.postData.contents);
    var rows = body.rows || [];
    result.total = rows.length;

    var teamColIdx = colToIndex_(TEAM_COLUMN);
    var aliveStartIdx = colToIndex_(ALIVE_START_COLUMN);
    var elimColIdx = colToIndex_(ELIM_COLUMN);
    var numDataRows = DATA_END_ROW - DATA_START_ROW + 1;

    // Read every team name once up front rather than per-row -- cheap,
    // and avoids re-reading the same column N times for N pushed rows.
    var teamNames = sheet.getRange(DATA_START_ROW, teamColIdx, numDataRows, 1).getValues();

    rows.forEach(function (row) {
      var wantKey = normalize_(row.team);
      if (!wantKey) return;
      for (var i = 0; i < teamNames.length; i++) {
        if (normalize_(teamNames[i][0]) !== wantKey) continue;
        var sheetRow = DATA_START_ROW + i;

        // aliveCount is left-to-right: first N of the ALIVE_COLUMN_COUNT
        // boxes checked, the rest cleared. null means "unknown right
        // now" (e.g. the squad hasn't been linked to a roster yet) --
        // left untouched rather than guessed at.
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
        break;
      }
    });

    result.ok = true;
  } catch (err) {
    result.error = String(err);
  }

  // Written on EVERY call, matched or not -- the one thing that proves
  // the URL is reachable and the script is running at all, independent
  // of whether any team name in this particular payload matched a row.
  sheet.getRange(DEBUG_CELL).setValue(
    new Date().toLocaleString() + " -- " + result.matched + "/" + result.total + " matched"
  );

  return ContentService.createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}
