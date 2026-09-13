/*
 * Free Fire broadcast sheet push -- receiving end.
 * ---------------------------------------------------------------
 * Two jobs, two SPREADSHEETS, one deployment. The live status and the
 * results grid are separate files, so each is opened by id below rather
 * than assumed to be whichever sheet this script is attached to.
 *
 *   ALIVE   (live, automatic)  Paired with push_sidetable_to_sheet() in
 *           freefire_engine.py. That side POSTs the SAME live alive/elim
 *           data the Alive Status overlay already shows; this script
 *           ticks the alive checkboxes and writes the elim count where a
 *           person used to do it by hand while watching the stream.
 *
 *   RESULTS (per match, on a button)  Paired with the dashboard's
 *           "Push to RESULTS sheet" button. Writes one match's finishing
 *           position and kills into that match's own column group, so the
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
 *   3. Check the constants below against your actual sheets -- ids, tab
 *      names, which column holds the team name, which columns are the
 *      alive checkboxes, where match 1 starts.
 *   4. Run -> pick doPost -> Run once, and accept the permission prompt.
 *      It will fail with an error about `e` being undefined; that is
 *      expected and harmless. The point is granting the script permission
 *      to open a spreadsheet OTHER than the one it is attached to, which
 *      it cannot ask for later from inside a web request.
 *   5. Deploy -> New deployment -> type "Web app" -> Execute as: Me ->
 *      Who has access: Anyone with the link -> Deploy.
 *   6. Copy the URL it gives you (ends in /exec) into the dashboard's
 *      "Broadcast Sheet Push" card (Free Fire -> Live In-Game Ops), Save,
 *      then hit "Send Test Row" -- DEBUG_CELL below should show a fresh
 *      timestamp the moment that succeeds, whether or not "SHEET PUSH
 *      TEST" matches a real team name in your sheet.
 *   7. If you ever change this file, you have to Deploy -> Manage
 *      deployments -> edit -> New version for the change to actually
 *      take effect -- saving alone does not update a live deployment.
 *
 * Nothing here needs a Google Cloud project, a service account, or any
 * credential file -- it runs as your own account, on your own sheet,
 * only reachable by whoever has this exact URL.
 */

// ---- WHICH SPREADSHEETS ---------------------------------------------------
// The two live in DIFFERENT spreadsheets, so each is opened by id rather
// than assumed to be the one this script is attached to. The id is the
// long string in the sheet's own URL, between /d/ and /edit.
// Leave either blank to use whatever spreadsheet this script is bound to.
var ALIVE_SPREADSHEET_ID = "1XX5CuUlGHPFzxd3PN8YPx6k_WEK0MdbKMus8cNwrNrk";
var RESULTS_SPREADSHEET_ID = "1O6_lIfDB-O7vX50wHmMxii-57ExD3Nxb5KaxjlG01jA";

// ---- ALIVE tab: the live side table --------------------------------------
var SHEET_NAME = "";               // tab name; blank = the first tab in that spreadsheet
var TEAM_COLUMN = "P";             // column holding each row's team name
var ALIVE_START_COLUMN = "Q";      // first of the alive checkbox columns
var ALIVE_COLUMN_COUNT = 4;        // how many alive checkboxes per team (usually the squad size)
var ELIM_COLUMN = "U";             // column holding the elimination count
var DATA_START_ROW = 5;            // first row that actually holds a team (skip headers above it)
var DATA_END_ROW = 16;             // last row that holds a team
var DEBUG_CELL = "Z1";             // any empty cell -- last-received marker, see step 5 above

// ---- RESULTS tab: one column group per match -----------------------------
var RESULTS_SHEET_NAME = "RESULTS";      // the tab holding the per-match grid
var RESULTS_TEAM_COLUMN = "C";           // column holding each row's team name
var RESULTS_FIRST_MATCH_COLUMN = "D";    // match 1's PLACE column
var RESULTS_MATCH_COLUMN_STRIDE = 3;     // PLACE, KILLS, WWCD -- so D/E/F, G/H/I, J/K/L ...
var RESULTS_START_ROW = 4;               // first row that holds a team
var RESULTS_END_ROW = 15;                // last row that holds a team
var RESULTS_MATCH_COUNT = 10;            // how many match column-groups exist

// What goes in the PLACE column. Set to "rank" because that is what the
// sheet holds: match 1 has BFA on 1 and iQOO TOTAL GAMING on 10, which are
// finishing positions, and the points table below it turns those into
// 12 / 1 with its own formulas.
//   "rank"   -- the finishing position, 1..12.
//   "points" -- the placement POINTS the result file assigns for that
//               finish (1st=12, 2nd=9, ... 11th/12th=0).
// The push sends both numbers, so this is the only thing to change.
var RESULTS_PLACEMENT_VALUE = "rank";

// The third column of each match group is WWCD -- 1 for the team that won
// that match, 0 for everyone else. OFF by default because that column may
// well hold a formula reading the PLACE cell beside it, and writing a
// value would destroy it. Check F4 on the RESULTS tab: if it is a plain
// number rather than a formula, turn this on and the Booyah column fills
// itself in too.
var RESULTS_WRITE_WWCD = false;

/* Sheet label -> the team's name in the dashboard roster.
   Only needed where the sheet's own shorthand shares too little with the
   registered name for the matching below to see they are the same squad.
   On the live-status sheet that is "iQOO TG" (registered as iQOO TOTAL
   GAMING) and "INSANE PWR" (INSANE POWER) -- nothing in either pair lines
   up as text. Add a line here, or change the label in the sheet; either
   works, this just avoids touching a sheet that is already on air.
   Names on the LEFT are what the sheet says, on the RIGHT what the roster
   says. Matching ignores case, spaces and punctuation on both sides. */
var TEAM_ALIASES = {
  "iQOO TG": "iQOO TOTAL GAMING",
  "INSANE PWR": "INSANE POWER"
};

// How short a name may be before containment matching stops trusting it.
// "GODLIKE" inside "GODLIKE ESPORTS" is obviously the same team; "TE"
// inside "TEAM EVOLUTION" obviously is not, and a threshold is the only
// thing between the two.
var MIN_CONTAINMENT_LENGTH = 5;
// ---------------------------------------------------------------------------


/* The spreadsheet an id names, or the one this script is bound to when
   the id is blank. Opening by id is what lets one deployment serve two
   separate files; it needs the permission granted in setup step 4. */
function openBook_(id) {
  return id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
}

/* A tab by name, or the first tab when the name is blank -- so a sheet
   whose tab nobody has bothered to name consistently still works. */
function tabOf_(book, name) {
  if (!book) return null;
  if (!name) return book.getSheets()[0] || null;
  return book.getSheetByName(name);
}

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

/* Row index (0-based, within the block) for a team, or -1.
   Three passes, strongest first, mirroring the ladder the engine uses on
   result-file names (match_roster_team in freefire_engine.py):

     1. exact, on the full name then the short one -- the sheet's team
        column can hold either without anyone having to say which;
     2. the alias table above, for labels that share no text with the
        registered name;
     3. containment either way, which is what catches a sheet saying
        "GODLIKE" or "CLUTZA ESP" where the roster says "GODLIKE ESPORTS"
        or "CLUTZA ESPORTS".

   Ordered this way so a weaker rule can never beat an exact hit: with
   containment first, a sheet holding both "BFA" and "BFA ACADEMY" would
   be a coin toss. */
function findRow_(names, team, short) {
  var wanted = [normalize_(team), normalize_(short)].filter(function (k) { return k; });
  if (!wanted.length) return -1;
  var i, w;

  for (w = 0; w < wanted.length; w++) {
    for (i = 0; i < names.length; i++) {
      if (normalize_(names[i][0]) === wanted[w]) return i;
    }
  }

  for (i = 0; i < names.length; i++) {
    var label = String(names[i][0] || "").trim();
    if (!label) continue;
    for (var key in TEAM_ALIASES) {
      if (normalize_(key) !== normalize_(label)) continue;
      if (wanted.indexOf(normalize_(TEAM_ALIASES[key])) >= 0) return i;
    }
  }

  for (i = 0; i < names.length; i++) {
    var cell = normalize_(names[i][0]);
    if (cell.length < MIN_CONTAINMENT_LENGTH) continue;
    for (w = 0; w < wanted.length; w++) {
      if (wanted[w].length < MIN_CONTAINMENT_LENGTH) continue;
      if (cell.indexOf(wanted[w]) >= 0 || wanted[w].indexOf(cell) >= 0) return i;
    }
  }

  return -1;
}


function pushAlive_(body, result) {
  var sheet = tabOf_(openBook_(ALIVE_SPREADSHEET_ID), SHEET_NAME);
  if (!sheet) { result.error = 'No tab named "' + SHEET_NAME + '" in the live-status sheet.'; return sheet; }

  var rows = body.rows || [];
  result.total = rows.length;

  var teamColIdx = colToIndex_(TEAM_COLUMN);
  var aliveStartIdx = colToIndex_(ALIVE_START_COLUMN);
  var elimColIdx = colToIndex_(ELIM_COLUMN);
  var numDataRows = DATA_END_ROW - DATA_START_ROW + 1;

  // Read every team name once up front rather than per-row -- cheap, and
  // avoids re-reading the same column N times for N pushed rows.
  var teamNames = sheet.getRange(DATA_START_ROW, teamColIdx, numDataRows, 1).getValues();

  var unmatched = [];
  rows.forEach(function (row) {
    var i = findRow_(teamNames, row.team, row.short);
    // Named rather than silently dropped: a team that finds no row is a
    // team whose alive count never reaches the sheet, and until this said
    // so the only symptom was a row that quietly never moved.
    if (i < 0) { if (row.team) unmatched.push(row.team); return; }
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

  if (unmatched.length) result.unmatched = unmatched;
  result.ok = true;
  return sheet;
}


function pushResults_(body, result) {
  var sheet = tabOf_(openBook_(RESULTS_SPREADSHEET_ID), RESULTS_SHEET_NAME);
  if (!sheet) { result.error = 'No tab named "' + RESULTS_SHEET_NAME + '" in the results sheet.'; return sheet; }

  var match = Number(body.match);
  if (!(match >= 1 && match <= RESULTS_MATCH_COUNT)) {
    result.error = "Match number " + body.match + " is outside 1.." + RESULTS_MATCH_COUNT + ".";
    return sheet;
  }

  var rows = body.rows || [];
  result.total = rows.length;
  result.match = match;

  // Match 1 sits at RESULTS_FIRST_MATCH_COLUMN, and every match after it
  // is one stride further right: D/E/F, G/H/I, J/K/L ... PLACE first,
  // KILLS second, WWCD third.
  var placementCol = colToIndex_(RESULTS_FIRST_MATCH_COLUMN) +
                     (match - 1) * RESULTS_MATCH_COLUMN_STRIDE;
  var width = RESULTS_WRITE_WWCD ? 3 : 2;
  var teamColIdx = colToIndex_(RESULTS_TEAM_COLUMN);
  var numDataRows = RESULTS_END_ROW - RESULTS_START_ROW + 1;
  var teamNames = sheet.getRange(RESULTS_START_ROW, teamColIdx, numDataRows, 1).getValues();

  // Built as one block and written in a single setValues call rather than
  // a write per cell: a 12-team push is two or three dozen round trips
  // that way, and Apps Script charges for every one of them.
  var block = [];
  for (var i = 0; i < numDataRows; i++) {
    var blank = [];
    for (var c = 0; c < width; c++) blank.push(null);
    block.push(blank);
  }

  var unmatched = [];
  rows.forEach(function (row) {
    var i = findRow_(teamNames, row.team, row.short);
    if (i < 0) { unmatched.push(row.team); return; }
    var placement = (RESULTS_PLACEMENT_VALUE === "rank") ? row.rank : row.placement;
    var cells = [
      (placement === null || placement === undefined) ? null : placement,
      (row.kills === null || row.kills === undefined) ? null : row.kills
    ];
    // WWCD is derived rather than sent: a Booyah IS finishing first, so
    // there is nothing the payload could say that rank doesn't already.
    if (RESULTS_WRITE_WWCD) cells.push(row.rank === 1 ? 1 : 0);
    block[i] = cells;
    result.matched++;
  });

  // null leaves a cell alone, so a team the push didn't cover keeps
  // whatever is already there rather than being wiped by this write.
  sheet.getRange(RESULTS_START_ROW, placementCol, numDataRows, width).setValues(block);

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
    var debugSheet = tabOf_(openBook_(ALIVE_SPREADSHEET_ID), SHEET_NAME);
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
