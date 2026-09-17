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
 *   BOOYAH  (rides along with RESULTS)  The squad that just won, on its
 *           own tab: the team name in B1, its player IGNs in B3:B6 and
 *           each one's kills in C3:C6. Written by the same button press
 *           as the RESULTS grid, so the two cannot end up showing
 *           different games. OVERWRITTEN every time on purpose -- it is
 *           a broadcast scratchpad for the Booyah graphic, not a record.
 *           The per-game history is the RESULTS grid's job.
 *
 * The first two are matched by TEAM NAME, never by row position: the
 * sheet's own team column is the source of truth for which row a team
 * owns, so nothing depends on the sheet and the dashboard agreeing about
 * order. The Booyah tab needs no matching -- it has one team on it, and
 * the push says which.
 *
 * SETUP (one time, on YOUR sheet):
 *   1. Open the sheet -> Extensions -> Apps Script.
 *   2. Delete whatever's in Code.gs and paste this whole file in.
 *   3. Check the constants below against your actual sheets -- ids, tab
 *      names, which column holds the team name, which columns are the
 *      alive checkboxes, where match 1 starts.
 *   4. Save (the disk icon, or Ctrl+S). Then pick `authorize` in the
 *      function dropdown next to Run -- it is the first function in this
 *      file -- press Run, and accept the permission prompt.
 *
 *      Do NOT try to run doPost by hand: it needs a real web request and
 *      has nothing to do without one. authorize() exists to do the same
 *      job properly -- it grants the script permission to open a
 *      spreadsheet OTHER than the one it is attached to (which it cannot
 *      ask for later, from inside a web request), and then prints what it
 *      found in both sheets so you can see the wiring is right before
 *      anything is deployed. Read the Execution log underneath.
 *
 *      If the dropdown doesn't list `authorize`, the file hasn't been
 *      saved yet -- save and it appears. Functions whose names end in an
 *      underscore are hidden from that list on purpose; that is why
 *      pushAlive_ and the rest aren't there.
 *   5. Deploy -> New deployment -> type "Web app" -> Execute as: Me ->
 *      Who has access: Anyone with the link -> Deploy.
 *   6. Copy the URL it gives you (ends in /exec) into the dashboard's
 *      "Broadcast Sheet Push" card (Free Fire -> Live In-Game Ops), Save,
 *      then hit "Send Test Row" -- DEBUG_CELL below (AP5 on the
 *      live-status tab) should show a fresh timestamp the moment that
 *      succeeds. It will say 0/1 matched, which is correct: the test
 *      sends a deliberately fake team name, so proving the pipe works
 *      never depends on a real team matching.
 *   7. For the Booyah half: make a tab called "Booyah" in the RESULTS
 *      spreadsheet. Nothing else to set up -- it is written to, never
 *      read from, so whatever else is on it is yours to lay out. If
 *      there is no such tab the results push still works and says so
 *      in the dashboard rather than failing.
 *   8. If you ever change this file, you have to Deploy -> Manage
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
var SHEET_NAME = "LIVESTATUS";     // tab name; blank = whichever tab happens to be first
var TEAM_COLUMN = "P";             // column holding each row's team name
var ALIVE_START_COLUMN = "Q";      // first of the alive checkbox columns
var ALIVE_COLUMN_COUNT = 4;        // how many alive checkboxes per team (usually the squad size)
var ELIM_COLUMN = "U";             // column holding the elimination count
var DATA_START_ROW = 5;            // first row that actually holds a team (skip headers above it)
var DATA_END_ROW = 16;             // last row that holds a team
var DEBUG_CELL = "AP5";            // an empty cell on the live-status tab -- last-received marker

// ---- RESULTS tab: one column group per match -----------------------------
var RESULTS_SHEET_NAME = "RESULTS";      // the tab holding the per-match grid
var RESULTS_TEAM_COLUMN = "C";           // column holding each row's team name
var RESULTS_FIRST_MATCH_COLUMN = "D";    // match 1's PLACE column
var RESULTS_MATCH_COLUMN_STRIDE = 3;     // PLACE, KILLS, WWCD -- so D/E/F, G/H/I, J/K/L ...
var RESULTS_START_ROW = 4;               // first row that holds a team
var RESULTS_END_ROW = 15;                // last row that holds a team
var RESULTS_MATCH_COUNT = 10;            // how many match column-groups exist
// Where a RESULTS push stamps its "last received" marker. On the results
// tab rather than the live-status one, so the push does not have to open
// a second spreadsheet -- worth about a second on every push. Pick an
// empty cell well clear of the match columns.
var RESULTS_DEBUG_CELL = "B1";

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
  "INSANE PWR": "INSANE POWER",
  // The squad the sheets call iQOO OGXTE / iQOO OG X ELITE registers as
  // TEAM ELITE, which is also what the game itself reports. No text is
  // shared, so only this can join them up.
  "iQOO OGXTE": "TEAM ELITE",
  "iQOO OG X ELITE": "TEAM ELITE"
};

// ---- BOOYAH tab: whoever won the game that was just pushed ---------------
// Overwritten on every results push, deliberately. This is a broadcast
// scratchpad -- the squad currently being shown as having won -- not a
// record: the per-game history already lives in the RESULTS grid, one
// column group per match, and a second copy here would only be one more
// thing to keep in step.
//
// Blank BOOYAH_SHEET_NAME to switch the whole thing off.
var BOOYAH_SHEET_NAME = "Booyah";        // tab in the RESULTS spreadsheet
var BOOYAH_TEAM_CELL = "B1";             // the winning team's name
var BOOYAH_IGN_COLUMN = "B";             // player IGNs, one per row
var BOOYAH_KILLS_COLUMN = "C";           // that player's kills, beside the IGN
var BOOYAH_PLAYER_START_ROW = 3;         // so B3:B6 / C3:C6 with four players
var BOOYAH_PLAYER_ROWS = 4;

// How short a name may be before containment matching stops trusting it.
// "GODLIKE" inside "GODLIKE ESPORTS" is obviously the same team; "TE"
// inside "TEAM EVOLUTION" obviously is not, and a threshold is the only
// thing between the two.
var MIN_CONTAINMENT_LENGTH = 5;
// ---------------------------------------------------------------------------


/* Run this once from the editor, before deploying.
   ------------------------------------------------------------------
   Two jobs. It triggers Google's permission prompt -- opening a
   spreadsheet by id needs a scope the script cannot request later from
   inside a web request -- and it reports what it can actually see, so a
   wrong id, a renamed tab or an empty team column shows up now rather
   than mid-match.

   Nothing is written. Read the Execution log underneath the editor. */
function authorize() {
  var lines = [];

  function look(what, id, tabName, teamCol, startRow, endRow) {
    try {
      var book = openBook_(id);
      if (!book) { lines.push(what + ": NO SPREADSHEET with id " + id); return; }
      var sheet = tabOf_(book, tabName);
      if (!sheet) {
        lines.push(what + ': opened "' + book.getName() + '" but it has no tab named "' +
                   tabName + '". Tabs in it: ' +
                   book.getSheets().map(function (t) { return t.getName(); }).join(", "));
        return;
      }
      var names = sheet.getRange(startRow, colToIndex_(teamCol), endRow - startRow + 1, 1)
                       .getValues()
                       .map(function (r) { return String(r[0] || "").trim(); })
                       .filter(function (n) { return n; });
      lines.push(what + ': "' + book.getName() + '" -> tab "' + sheet.getName() + '", ' +
                 names.length + " teams in " + teamCol + startRow + ":" + teamCol + endRow +
                 (names.length ? " -- " + names.join(", ") : " -- EMPTY, check the column and rows"));
    } catch (err) {
      lines.push(what + ": FAILED -- " + err);
    }
  }

  look("LIVE STATUS", ALIVE_SPREADSHEET_ID, SHEET_NAME, TEAM_COLUMN, DATA_START_ROW, DATA_END_ROW);
  look("RESULTS", RESULTS_SPREADSHEET_ID, RESULTS_SHEET_NAME, RESULTS_TEAM_COLUMN,
       RESULTS_START_ROW, RESULTS_END_ROW);

  // The Booyah tab, and -- more usefully -- whether the code running
  // here even knows about it. A deployment serving an older version
  // reports BOOYAH as missing from the script itself, which is the
  // answer to "I pasted it, why is nothing arriving".
  if (typeof pushBooyah_ !== "function") {
    lines.push("BOOYAH: this script has NO pushBooyah_ in it -- the editor is " +
               "running an older copy of the file. Paste the current " +
               "freefire_sheet_push.gs over it and save.");
  } else if (!BOOYAH_SHEET_NAME) {
    lines.push("BOOYAH: switched off (BOOYAH_SHEET_NAME is blank).");
  } else {
    try {
      var bBook = openBook_(RESULTS_SPREADSHEET_ID);
      var bTab = tabOf_(bBook, BOOYAH_SHEET_NAME);
      lines.push(bTab
        ? 'BOOYAH: "' + bBook.getName() + '" -> tab "' + bTab.getName() +
          '" found. Team goes in ' + BOOYAH_TEAM_CELL + ', IGNs in ' +
          BOOYAH_IGN_COLUMN + BOOYAH_PLAYER_START_ROW + ':' + BOOYAH_IGN_COLUMN +
          (BOOYAH_PLAYER_START_ROW + BOOYAH_PLAYER_ROWS - 1) + ', kills in ' +
          BOOYAH_KILLS_COLUMN + BOOYAH_PLAYER_START_ROW + ':' + BOOYAH_KILLS_COLUMN +
          (BOOYAH_PLAYER_START_ROW + BOOYAH_PLAYER_ROWS - 1) + '.'
        : 'BOOYAH: opened "' + bBook.getName() + '" but it has NO tab named "' +
          BOOYAH_SHEET_NAME + '". Tabs in it: ' +
          bBook.getSheets().map(function (t) { return t.getName(); }).join(", "));
    } catch (err) {
      lines.push("BOOYAH: FAILED -- " + err);
    }
  }

  var report = lines.join("\n");
  Logger.log(report);
  return report;
}


/* Run this from the editor to prove the Booyah half works, without
   touching the RESULTS grid.

   Writes one obviously-fake squad into the Booyah tab. If the tab fills
   in, the code in THIS editor is fine and anything still missing after a
   real push is the DEPLOYMENT serving an older version -- saving the
   editor does not update a live /exec URL, only Deploy -> Manage
   deployments -> edit -> New version does.

   Read the Execution log underneath, then clear the tab or just push a
   real match over it. */
function testBooyah() {
  var result = {};
  pushBooyah_(openBook_(RESULTS_SPREADSHEET_ID), {
    team: "TEST -- delete me",
    players: [
      { ign: "TEST.PLAYER.1", kills: 9 },
      { ign: "TEST.PLAYER.2", kills: 4 },
      { ign: "TEST.PLAYER.3", kills: 2 },
      { ign: "TEST.PLAYER.4", kills: 0 }
    ]
  }, result);
  var report = "testBooyah: " + (result.booyah || "(nothing reported)");
  Logger.log(report);
  return report;
}


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

  // Same button, same moment, same game -- see pushBooyah_.
  pushBooyah_(sheet.getParent(), body.booyah, result);

  result.ok = true;
  return sheet;
}


/* The winning squad of the match just pushed, onto its own tab.

   Called from pushResults_ so one button press does both -- splitting it
   into a second action is how the two end up showing different games.

   Never fatal. The RESULTS write is the one that matters and has already
   happened by the time this runs; a missing tab or a renamed column
   should be reported, not turned into a failed push that invites someone
   to press the button again and rewrite the grid. */
function pushBooyah_(book, booyah, result) {
  if (!BOOYAH_SHEET_NAME) return;
  if (!booyah || !booyah.team) {
    result.booyah = "no team finished 1st in that match -- nothing written";
    return;
  }

  var sheet = tabOf_(book, BOOYAH_SHEET_NAME);
  if (!sheet) {
    result.booyah = 'no tab named "' + BOOYAH_SHEET_NAME + '" -- nothing written';
    return;
  }

  try {
    sheet.getRange(BOOYAH_TEAM_CELL).setValue(booyah.team);

    // Padded to the full block rather than written short: last game's
    // fourth player must not be left sitting under this game's third.
    var players = booyah.players || [];
    var block = [];
    for (var i = 0; i < BOOYAH_PLAYER_ROWS; i++) {
      var p = players[i];
      block.push(p ? [p.ign || "", (p.kills === null || p.kills === undefined) ? "" : p.kills]
                   : ["", ""]);
    }

    var ignCol = colToIndex_(BOOYAH_IGN_COLUMN);
    var killsCol = colToIndex_(BOOYAH_KILLS_COLUMN);
    if (killsCol === ignCol + 1) {
      // The normal case -- B and C -- is one write.
      sheet.getRange(BOOYAH_PLAYER_START_ROW, ignCol, BOOYAH_PLAYER_ROWS, 2)
           .setValues(block);
    } else {
      // Non-adjacent columns still work, just as two writes.
      sheet.getRange(BOOYAH_PLAYER_START_ROW, ignCol, BOOYAH_PLAYER_ROWS, 1)
           .setValues(block.map(function (r) { return [r[0]]; }));
      sheet.getRange(BOOYAH_PLAYER_START_ROW, killsCol, BOOYAH_PLAYER_ROWS, 1)
           .setValues(block.map(function (r) { return [r[1]]; }));
    }

    var written = players.slice(0, BOOYAH_PLAYER_ROWS).length;
    result.booyah = booyah.team + " (" + written + " player" +
                    (written === 1 ? "" : "s") + ")";
    if (players.length > BOOYAH_PLAYER_ROWS) {
      result.booyah += " -- " + (players.length - BOOYAH_PLAYER_ROWS) +
                       " more in the squad than there are rows for";
    }
  } catch (err) {
    result.booyah = "failed -- " + err;
  }
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
  //
  // Reuses the sheet the push already opened wherever it can.
  // SpreadsheetApp.openById costs around a second, and opening the LIVE
  // STATUS file purely to stamp a marker was adding that second to every
  // RESULTS push -- a third of the total, paid for a debug convenience,
  // while a caster waited. Measured: 2.9s for a call that writes nothing
  // else at all.
  try {
    var debugSheet = sheet;
    if (!debugSheet || result.kind === "alive") {
      debugSheet = tabOf_(openBook_(ALIVE_SPREADSHEET_ID), SHEET_NAME);
    }
    if (debugSheet) {
      // On the results sheet the alive tab's cell reference is
      // meaningless, so each has its own.
      var cell = (debugSheet === sheet && result.kind === "results")
        ? RESULTS_DEBUG_CELL : DEBUG_CELL;
      debugSheet.getRange(cell).setValue(
        new Date().toLocaleString() + " -- " + (result.kind || "?") + " " +
        result.matched + "/" + result.total + " matched" +
        (result.error ? " -- " + result.error : "")
      );
    }
  } catch (err2) { /* the marker is a convenience, never a reason to fail */ }

  return ContentService.createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}
