/* The lobby tick grid and the ARROW game boxes.
 *
 *   node dashboard/test_arrow_lobby.js
 *
 * No jsdom: the functions under test touch a handful of DOM methods, so
 * those are stubbed here and the real source is lifted out of
 * dashboard.html. That keeps this runnable on a machine that has never
 * had npm install run on it -- which is the machine the event runs on.
 *
 * WHAT THIS GUARDS
 *
 *   * BOTH PANELS CAME UP EMPTY on air -- no ticks, no game boxes, not
 *     even their "nothing committed yet" line -- while the rest of the
 *     page looked perfectly healthy. They were drawn from ffRenderEvent,
 *     which runs after fourteen other renderers, so anything throwing
 *     earlier in that chain took them down silently. They are drawn
 *     first now, each in its own try/catch.
 *
 *   * THE GAME LIST MUST NOT DEPEND ON THE ENGINE BEING RESTARTED. It
 *     used to read the engine's fraggers block, which only exists in a
 *     newer build; against the running engine every box vanished, which
 *     reads as "no games played" on a day when six were. It comes off
 *     the committed matches instead.
 *
 *   * A SYNC THAT OMITS THE ROSTER must not empty the tick grid. The
 *     engine leaves the roster out when it has not changed.
 */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "dashboard.html"), "utf8");

/* Lift the functions under test out of the page by name. */
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

const SOURCE = ["ffCompletedGames", "ffLatestMatch", "ffLatestSummary",
                "ffArrowTickedGames", "ffDrawArrowGames", "ffDrawLobbyTicks"]
  .map(lift).join("\n\n");

let checks = 0; const failures = [];
function check(label, ok, detail) {
  checks++;
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + label +
              (detail ? "  " + detail : ""));
  if (!ok) failures.push(label);
}

/* A DOM just big enough. */
function makeEnv(state) {
  const nodes = {};
  const el = id => (nodes[id] = nodes[id] || {
    id, innerHTML: "", textContent: "", value: "", checked: false,
    dataset: {}, contains: () => false,
    querySelectorAll: () => [], addEventListener: () => {},
  });
  ["ff_lobbyTicks", "ff_lobbyCount", "ff_arrowGames", "ff_arrowLatest"]
    .forEach(el);
  const sandbox = {
    document: { activeElement: null, getElementById: id => nodes[id] || null,
                querySelectorAll: () => [] },
    ffState: state,
    ffEscapeHtml: s => String(s == null ? "" : s)
      .replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])),
    console,
  };
  const vm = require("vm");
  vm.createContext(sandbox);
  vm.runInContext(SOURCE + "\n", sandbox);
  return { nodes, sandbox, vm };
}

function run(state, fn) {
  const env = makeEnv(state);
  require("vm").runInContext(fn, env.sandbox);
  return env.nodes;
}

const ROSTER = { teams: [{ name: "RENU GAMING" }, { name: "TAG" }, { name: "RES" }] };
const MATCHES = [
  { gameNumber: 1, fileName: "MatchResult_1.log",
    teams: [{ teamName: "TAG", rank: 1 }, { teamName: "RES", rank: 2 }] },
  { gameNumber: 2, fileName: "MatchResult_2.log",
    teams: [{ teamName: "RES", rank: 1 }] },
];

console.log("\nthe lobby tick grid");
let n = run({ roster: ROSTER, lobby: { joined: { TAG: true } }, matches: [] },
            "ffDrawLobbyTicks(ffState)");
check("one tick per roster squad",
      (n.ff_lobbyTicks.innerHTML.match(/ff-lobby-tick/g) || []).length === 3,
      (n.ff_lobbyTicks.innerHTML.match(/ff-lobby-tick/g) || []).length + " found");
check("the squad already in is ticked",
      /data-team="TAG"[^>]*checked/.test(n.ff_lobbyTicks.innerHTML));
check("a squad not in is not ticked",
      !/data-team="RES"[^>]*checked/.test(n.ff_lobbyTicks.innerHTML));
check("the count reads right", n.ff_lobbyCount.textContent === "1 of 3 checked in",
      n.ff_lobbyCount.textContent);

// The engine omits the roster from a sync when it has not changed. The
// grid must survive that, which is why it reads ffState and not the
// object handed to the renderer.
n = run({ roster: ROSTER, lobby: { joined: {} }, matches: [] },
        "ffDrawLobbyTicks({})");
check("a sync carrying no roster still draws the grid",
      (n.ff_lobbyTicks.innerHTML.match(/ff-lobby-tick/g) || []).length === 3);

n = run({ roster: { teams: [] }, lobby: {}, matches: [] },
        "ffDrawLobbyTicks(ffState)");
check("an empty roster says so instead of going blank",
      /No teams in the roster/.test(n.ff_lobbyTicks.innerHTML),
      JSON.stringify(n.ff_lobbyTicks.innerHTML.slice(0, 48)));

console.log("\nthe completed-game boxes");
// The running engine publishes no fraggers block. The boxes must still
// appear, because the matches are right there.
n = run({ roster: ROSTER, lobby: {}, matches: MATCHES }, "ffDrawArrowGames()");
check("a game box per committed game, with no fraggers block present",
      (n.ff_arrowGames.innerHTML.match(/ff-arrow-game/g) || []).length === 2,
      (n.ff_arrowGames.innerHTML.match(/ff-arrow-game/g) || []).length + " found");
check("every game is ticked by default (the usual push is all of them)",
      (n.ff_arrowGames.innerHTML.match(/checked/g) || []).length === 2);

// A corrected result is committed again under the same game number.
n = run({ roster: ROSTER, lobby: {},
          matches: MATCHES.concat([{ gameNumber: 2, teams: [] }]) },
        "ffDrawArrowGames()");
check("a game committed twice offers one box, not two",
      (n.ff_arrowGames.innerHTML.match(/ff-arrow-game/g) || []).length === 2);

n = run({ roster: ROSTER, lobby: {}, matches: [] }, "ffDrawArrowGames()");
check("no matches says so rather than showing nothing",
      /No committed matches yet/.test(n.ff_arrowGames.innerHTML),
      JSON.stringify(n.ff_arrowGames.innerHTML.slice(0, 48)));

console.log("\nwhat the latest-game readout says");
n = run({ roster: ROSTER, lobby: {}, matches: MATCHES }, "ffDrawArrowGames()");
check("it names the game and who won it",
      /Game 2/.test(n.ff_arrowLatest.textContent) &&
      /RES/.test(n.ff_arrowLatest.textContent),
      n.ff_arrowLatest.textContent.slice(0, 70));

// "Latest" is the last COMMITTED match, not the highest game number:
// re-committing game 1 to fix it makes game 1 the one just worked on.
n = run({ roster: ROSTER, lobby: {},
          matches: MATCHES.concat([{ gameNumber: 1,
            teams: [{ teamName: "TAG", rank: 1 }] }]) },
        "ffDrawArrowGames()");
check("the latest is the last committed, not the highest numbered",
      /Game 1/.test(n.ff_arrowLatest.textContent),
      n.ff_arrowLatest.textContent.slice(0, 60));

n = run({ roster: ROSTER, lobby: {}, matches: [] }, "ffDrawArrowGames()");
check("with nothing fetched or committed it says so",
      /Nothing fetched or committed yet/.test(n.ff_arrowLatest.textContent),
      n.ff_arrowLatest.textContent.slice(0, 60));


// A match fetched but NOT committed is the usual thing to push: the
// Booyah graphic goes up while the result is still being reviewed.
n = (function(){
  const vm = require("vm");
  const nodes = {};
  const el = id => (nodes[id] = nodes[id] || { id, innerHTML: "", textContent: "",
    value: "", checked: false, dataset: {}, contains: () => false,
    querySelectorAll: () => [], addEventListener: () => {} });
  ["ff_lobbyTicks","ff_lobbyCount","ff_arrowGames","ff_arrowLatest"].forEach(el);
  const sandbox = { document: { activeElement: null,
      getElementById: id => nodes[id] || null, querySelectorAll: () => [] },
    ffState: { roster: ROSTER, lobby: {}, matches: [] },
    ffPendingMatch: { matchId: "2106394579182532608",
      fileName: "MatchResult_2026-10-03-20-29-58.log",
      teams: [{ teamName: "S8UL ESPORTS", rank: 1 }] },
    ffEscapeHtml: x => String(x == null ? "" : x), console };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE + "\nffDrawArrowGames();", sandbox);
  return nodes;
})();
check("a fetched-but-uncommitted match is what the buttons will push",
      /UNDER REVIEW/.test(n.ff_arrowLatest.textContent) &&
      /S8UL ESPORTS/.test(n.ff_arrowLatest.textContent),
      n.ff_arrowLatest.textContent.slice(0, 80));

console.log("\n" + checks + " checks, " + failures.length + " failed");
if (failures.length) console.log("failed: " + failures.join(", "));
process.exit(failures.length ? 1 : 0);
