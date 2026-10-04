/* The normal-room team picker in Post-Match review.
 *
 *   node dashboard/test_normal_room.js
 *
 * No jsdom, for the reason given in test_arrow_lobby.js: this has to run
 * on the event machine as it stands.
 *
 * WHAT THIS GUARDS
 *
 *   * A LEAGUE ROOM IS UNTOUCHED. The picker, its status line and the
 *     commit check exist only when the operator has said "normal room".
 *     A league room was working and must keep working exactly as it was.
 *
 *   * ONLY A CONFIDENT MATCH IS PRE-PICKED. A block the engine placed from
 *     enough of its players' UIDs arrives chosen; a block it could not
 *     place, or placed from a single player, waits for the operator.
 *
 *   * A PICK IS THE SAME THING AS A MATCH. It writes teamName, shortName
 *     and logo exactly as the engine's own match does, so standings,
 *     Booyah and the fragger list cannot tell the difference.
 *
 *   * NOTHING INCOMPLETE IS COMMITTED. A block with no team, or two blocks
 *     on one team, is caught before it reaches the standings.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "dashboard.html"), "utf8");

function lift(name) {
  const start = html.indexOf("function " + name + "(");
  if (start === -1) throw new Error("not found in dashboard.html: " + name);
  let i = html.indexOf("{", start), depth = 0, end = -1;
  for (let j = i; j < html.length; j++) {
    if (html[j] === "{") depth++;
    else if (html[j] === "}") { depth--; if (depth === 0) { end = j + 1; break; } }
  }
  return html.slice(start, end);
}

const SOURCE = ["ffNormalRoomActive", "ffRosterTeams", "ffRosterLabel",
                "ffReviewTeamPicker", "ffApplyReviewPick", "ffReviewPickProblems"]
  .map(lift).join("\n\n");

let checks = 0; const failures = [];
function check(label, ok, detail) {
  checks++;
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + label + (detail ? "  " + detail : ""));
  if (!ok) failures.push(label);
}

const ROSTER = { teams: [
  { name: "S8UL ESPORTS", shortName: "S8UL", logo: "s8ul.png" },
  { name: "RES", shortName: "RES", logo: "res.png" },
  { name: "TAG", displayName: "TAG ESPORTS", shortName: "TAG", logo: "tag.png" },
]};

function env(roomType, pending, selects) {
  const sb = {
    ffState: { event: roomType ? { roomType } : {}, roster: ROSTER },
    ffPendingMatch: pending,
    ffEscapeHtml: x => String(x == null ? "" : x),
    document: { querySelectorAll: sel => sel === ".ff-review-team" ? (selects || []) : [] },
    console,
  };
  vm.createContext(sb);
  vm.runInContext(SOURCE, sb);
  return sb;
}

console.log("\na league room is left exactly as it was");
check("no room type set means league", !env(null).ffNormalRoomActive());
check("league means league", !env("league").ffNormalRoomActive());
check("only 'normal' switches the picker on", env("normal").ffNormalRoomActive());

console.log("\nwhat arrives pre-picked");
let sb = env("normal");
let h = sb.ffReviewTeamPicker({ matched: true, needsUidReview: false, teamName: "RES",
                               fileTeamName: "Team 3" }, 0);
check("a confident match arrives chosen", /value="1" selected/.test(h));
check("and the file's own word is shown beside it", /file says: Team 3/.test(h));
h = sb.ffReviewTeamPicker({ matched: false, teamName: "Team 9" }, 1);
check("an unplaced block waits for the operator", /value="" selected/.test(h));
h = sb.ffReviewTeamPicker({ matched: true, needsUidReview: true, teamName: "RES" }, 2);
check("a guess from one player waits too, rather than defaulting",
      /value="" selected/.test(h) && !/value="1" selected/.test(h));
h = sb.ffReviewTeamPicker({ matched: true, teamName: "TAG ESPORTS" }, 3);
check("a team is found by its on-air name", /value="2" selected/.test(h));

console.log("\na pick is the same thing as a match");
const pending = { teams: [{ rank: 1, teamName: "Team 3", fileTeamName: "Team 3",
                            matched: false, needsUidReview: true }] };
sb = env("normal", pending);
sb.ffApplyReviewPick(0, "2");
const t = pending.teams[0];
check("the on-air name", t.teamName === "TAG ESPORTS", t.teamName);
check("the short name and logo", t.shortName === "TAG" && t.logo === "tag.png");
check("marked matched, and no longer flagged", t.matched === true && t.needsUidReview === false);
sb.ffApplyReviewPick(0, "");
check("un-picking puts it back to unmatched", t.matched === false && t.teamName === "Team 3");

console.log("\nnothing incomplete is committed");
const blocks = { teams: [{ rank: 1 }, { rank: 2 }, { rank: 3 }] };
let pr = env("normal", blocks, [
  { dataset: { block: "0" }, value: "0" },
  { dataset: { block: "1" }, value: "" },
  { dataset: { block: "2" }, value: "1" },
]).ffReviewPickProblems();
check("a block with no team is named by its rank", pr.missing.length === 1 && pr.missing[0] === 2,
      JSON.stringify(pr.missing));
pr = env("normal", blocks, [
  { dataset: { block: "0" }, value: "1" },
  { dataset: { block: "1" }, value: "0" },
  { dataset: { block: "2" }, value: "1" },
]).ffReviewPickProblems();
check("a team picked twice is caught, with both ranks",
      pr.twice.length === 1 && pr.twice[0].team === "RES" &&
      pr.twice[0].ranks.join() === "1,3", JSON.stringify(pr.twice));
pr = env("normal", blocks, [
  { dataset: { block: "0" }, value: "0" },
  { dataset: { block: "1" }, value: "1" },
  { dataset: { block: "2" }, value: "2" },
]).ffReviewPickProblems();
check("a complete set of picks has nothing to report",
      !pr.missing.length && !pr.twice.length);

console.log("\n" + checks + " checks, " + failures.length + " failed");
if (failures.length) console.log("failed: " + failures.join(", "));
process.exit(failures.length ? 1 : 0);
