/**
 * ARROW sheet push -- Booyah stats, the match top-5, and the series totals.
 *
 * Bind this to the spreadsheet that holds the BOOYAH-ARROW and ARROW MVP
 * tabs, deploy it once as a web app, and paste the /exec URL into the
 * dashboard. Nothing in here needs editing by hand: every range below is
 * already set to the layout that was asked for.
 *
 *   Deploy > New deployment > Web app
 *     Execute as:       Me
 *     Who has access:   Anyone
 *
 * WHAT IT WRITES
 *
 *   BOOYAH-ARROW   A2:A5  team        C2:C5  player
 *                  D2:D5  player photo (its file path on the engine PC)
 *                  E2:E5  elims       F2:F5  knocks
 *                  G2:G5  head rate   H2:H5  finish contribution
 *
 *   ARROW MVP      the match top 5, five rows:
 *                  N2:N6  team        P2:P6  player
 *                  R2:R6  elims       S2:S6  knocks
 *                  T2:T6  headshots   U2:U6  head contribution
 *                  V2:V6  finish contribution
 *
 *   ARROW MVP      the series totals, from row 2 down:
 *                  B      team        D      player
 *                  E      player photo (its file path on the engine PC)
 *                  F      elims       G      knocks
 *                  H      headshots   I      head contribution
 *                  J      finish contribution
 *
 * Survival time is not written (columns I on BOOYAH-ARROW and K on
 * ARROW MVP are left alone).
 *
 * EVERY BLOCK IS CLEARED BEFORE IT IS WRITTEN. A push of four Booyah
 * players over a previous five, or of eight totals over a previous
 * twenty, would otherwise leave the tail of the old push sitting there
 * looking exactly like current data. Only the columns listed above are
 * touched -- anything the sheet computes beside them is left alone.
 */

var BOOYAH_TAB = "BOOYAH-ARROW";
var MVP_TAB = "ARROW MVP";

// BOOYAH-ARROW: four players, rows 2-5.
var BOOYAH_FIRST_ROW = 2;
var BOOYAH_ROWS = 4;
var BOOYAH_COLUMNS = {team: "A", ign: "C", photo: "D", elims: "E", knocks: "F",
                      headRate: "G", finContri: "H"};
// Survival time is no longer written: column I (and K below) are left
// alone -- not cleared, not written -- for whatever the sheet keeps there.

// ARROW MVP, the match block: five players, rows 2-6.
var MATCH_FIRST_ROW = 2;
var MATCH_ROWS = 5;
var MATCH_COLUMNS = {team: "N", ign: "P", elims: "R", knocks: "S",
                     headshots: "T", headContri: "U", finContri: "V"};

// ARROW MVP, the totals block: row 2 downwards, however many there are.
var TOTAL_FIRST_ROW = 2;
var TOTAL_COLUMNS = {team: "B", ign: "D", photo: "E", elims: "F", knocks: "G",
                     headshots: "H", headContri: "I", finContri: "J"};
// How far down a clear reaches. Comfortably past a full lobby of 12
// squads at four players, so a shorter push can never leave a longer
// one's tail behind.
var TOTAL_CLEAR_ROWS = 200;

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    var what = body.what || "";
    if (what === "booyah")      return reply(pushBooyah(body.rows || []));
    if (what === "matchTop")    return reply(pushMatchTop(body.rows || []));
    if (what === "totals")      return reply(pushTotals(body.rows || []));
    if (what === "clearTotals") return reply(clearTotals());
    return reply({ok: false, error: "Unknown 'what': " + what});
  } catch (err) {
    return reply({ok: false, error: String(err)});
  }
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
      .setMimeType(ContentService.MimeType.JSON);
}

function tab(name) {
  var sheet = SpreadsheetApp.getActive().getSheetByName(name);
  if (!sheet) {
    throw new Error("No tab named '" + name + "'. Tabs here: " +
        SpreadsheetApp.getActive().getSheets().map(function (s) {
          return s.getName();
        }).join(", "));
  }
  return sheet;
}

/* A value the sheet should show as empty rather than as a zero. The
   engine sends null for a figure it genuinely does not have -- a
   headshot rate for a game it never watched -- and "" keeps that
   distinction visible instead of inventing a nought. */
function cell(value) {
  return (value === null || value === undefined) ? "" : value;
}

// Fields written as plain text rather than left for the sheet to guess.
var TEXT_FIELDS = {survival: true, photo: true};

function writeColumn(sheet, column, firstRow, values, asText) {
  if (!values.length) return;
  var out = values.map(function (v) { return [cell(v)]; });
  var range = sheet.getRange(column + firstRow + ":" + column + (firstRow + out.length - 1));
  if (asText) range.setNumberFormat("@");
  range.setValues(out);
}

function clearColumns(sheet, columns, firstRow, rowCount) {
  Object.keys(columns).forEach(function (key) {
    var c = columns[key];
    sheet.getRange(c + firstRow + ":" + c + (firstRow + rowCount - 1))
         .clearContent();
  });
}

function pushBlock(sheetName, columns, firstRow, rowCount, rows, fields) {
  var sheet = tab(sheetName);
  clearColumns(sheet, columns, firstRow, rowCount);
  var use = rows.slice(0, rowCount);
  fields.forEach(function (f) {
    writeColumn(sheet, columns[f], firstRow, use.map(function (r) {
      return r[f];
    }), TEXT_FIELDS[f] === true);
  });
  return {ok: true, tab: sheetName, written: use.length,
          cleared: rowCount};
}

function pushBooyah(rows) {
  return pushBlock(BOOYAH_TAB, BOOYAH_COLUMNS, BOOYAH_FIRST_ROW,
                   BOOYAH_ROWS, rows,
                   ["team", "ign", "photo", "elims", "knocks", "headRate", "finContri"]);
}

function pushMatchTop(rows) {
  return pushBlock(MVP_TAB, MATCH_COLUMNS, MATCH_FIRST_ROW,
                   MATCH_ROWS, rows,
                   ["team", "ign", "elims", "knocks", "headshots",
                    "headContri", "finContri"]);
}

function pushTotals(rows) {
  return pushBlock(MVP_TAB, TOTAL_COLUMNS, TOTAL_FIRST_ROW,
                   TOTAL_CLEAR_ROWS, rows,
                   ["team", "ign", "photo", "elims", "knocks", "headshots",
                    "headContri", "finContri"]);
}

function clearTotals() {
  var sheet = tab(MVP_TAB);
  clearColumns(sheet, TOTAL_COLUMNS, TOTAL_FIRST_ROW, TOTAL_CLEAR_ROWS);
  return {ok: true, tab: MVP_TAB, cleared: TOTAL_CLEAR_ROWS};
}

/**
 * Run this once from the editor before deploying. It asks for the
 * permissions the web app needs and prints the tabs it can see, so a
 * misspelled tab name is caught here rather than mid-event.
 */
function authorize() {
  var ss = SpreadsheetApp.getActive();
  Logger.log("Spreadsheet: " + ss.getName());
  Logger.log("Tabs: " + ss.getSheets().map(function (s) {
    return "'" + s.getName() + "'";
  }).join(", "));
  [BOOYAH_TAB, MVP_TAB].forEach(function (name) {
    Logger.log(ss.getSheetByName(name)
        ? "FOUND  '" + name + "'"
        : "MISSING '" + name + "'  <-- fix the name above or the tab");
  });
}
