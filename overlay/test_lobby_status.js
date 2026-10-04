/* The lobby strip's render, and specifically when it animates.
 *
 *   node overlay/test_lobby_status.js
 *
 * No jsdom, for the same reason as dashboard/test_arrow_lobby.js: this
 * has to run on the machine at the event, which has never had npm
 * install run on it. The few DOM calls render() makes are stubbed and the
 * function is lifted out of the page.
 *
 * WHAT THIS GUARDS
 *
 * The strip is on air for the whole of room-create, under a live game,
 * and it is redrawn several times a second. Every animation here is
 * therefore conditional, and every one of those conditions fails
 * silently and looks like a bug on stream:
 *
 *   * THE ENTRANCE MUST PLAY ONCE, on the hidden -> shown edge. A squad
 *     being ticked rebuilds the row; if the entrance were not gated, all
 *     twelve cards would fly in again mid-show, every time anyone
 *     touched a checkbox.
 *
 *   * A SQUAD ALREADY IN WHEN THE STRIP COMES UP MUST NOT LOCK IN. The
 *     lock-in means "this just happened". Replaying it for squads that
 *     arrived ten minutes ago says something false, loudly.
 *
 *   * A CARD CANNOT BOTH ARRIVE AND LOCK IN. Two transforms on one
 *     element cancel each other and the card does neither.
 */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "freefire_lobby_status.html"), "utf8");

function lift(name) {
  const start = html.indexOf("function " + name + "(");
  if (start === -1) throw new Error("not found: " + name);
  let i = html.indexOf("{", start), depth = 0, end = -1;
  for (let j = i; j < html.length; j++) {
    if (html[j] === "{") depth++;
    else if (html[j] === "}") { depth--; if (depth === 0) { end = j + 1; break; } }
  }
  return html.slice(start, end);
}

const SOURCE =
  "let wasJoined = new Set();\nlet lastSignature = '';\nlet wasVisible = false;\n" +
  ["esc", "label", "initials", "render"].map(lift).join("\n\n");

let checks = 0; const failures = [];
function check(label, ok, detail) {
  checks++;
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + label +
              (detail ? "  " + detail : ""));
  if (!ok) failures.push(label);
}

function mkEnv() {
  const nodes = {};
  const mk = id => ({
    id, innerHTML: "", textContent: "", className: "",
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); },
    },
  });
  ["stage", "title", "kicker", "cards", "joined", "total"].forEach(
    id => (nodes[id] = mk(id)));
  const vm = require("vm");
  const sandbox = {
    document: { getElementById: id => nodes[id] || null },
    setTimeout: () => 0,
    JSON, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);
  return { nodes, sandbox, vm };
}

const TEAMS = [{ name: "TAG" }, { name: "RES" }, { name: "RNTX" }];
function state(visible, joined) {
  return {
    roster: { teams: TEAMS },
    lobby: { joined: joined || {} },
    display: { lobbyStatusVisible: visible },
  };
}
function draw(env, st) {
  env.sandbox.__s = st;
  env.vm.runInContext("render(__s);", env.sandbox);
  return env.nodes;
}

console.log("\nthe entrance plays once, on the way in");
let env = mkEnv();
let n = draw(env, state(true, { TAG: true }));
check("cards fly in when the strip first appears",
      /justShown/.test(n.cards.className), n.cards.className);
const idx = (n.cards.innerHTML.match(/--i:(\d+)/g) || [])
  .map(d => Number(d.replace(/\D/g, "")));
check("every card carries its position, in order",
      idx.length === TEAMS.length && idx.every((v, i) => v === i),
      JSON.stringify(idx));
check("the shimmer rides on every card too",
      (n.cards.innerHTML.match(/class="shimmer"/g) || []).length === TEAMS.length);

// A squad gets ticked. The row is rebuilt -- but the strip never left.
n = draw(env, state(true, { TAG: true, RES: true }));
check("ticking a squad does NOT send every card flying again",
      !/justShown/.test(n.cards.className), n.cards.className);
check("and the squad that just arrived locks in",
      /justJoined/.test(n.cards.innerHTML));

console.log("\ngoing back to waiting");
// Clear All, or one squad un-ticked. This used to snap with no
// animation at all -- the only instant move on the strip.
n = draw(env, state(true, { TAG: true }));
check("a squad that drops out plays the un-lock",
      /justLeft/.test(n.cards.innerHTML));

n = draw(env, state(true, {}));
check("Clear All un-locks the one still in",
      (n.cards.innerHTML.match(/justLeft/g) || []).length === 1);

// On the NEXT real change, a card that was already waiting must not be
// marked as having just left. (Drawing the same state twice is a no-op
// by design -- the markup simply stays until something changes, and the
// class is swept by a timer.)
n = draw(env, state(true, { RES: true }));
check("a card already waiting is not marked as having just left",
      (n.cards.innerHTML.match(/justLeft/g) || []).length === 0,
      "only RES should move, and it is joining");
check("while the squad that joined on that same redraw locks in",
      /justJoined/.test(n.cards.innerHTML));

console.log("\nwhat must not animate");
// Already in when the strip comes up: arriving is not the same as
// having arrived.
env = mkEnv();
n = draw(env, state(true, { TAG: true, RES: true }));
check("squads already in do not replay the lock-in on reveal",
      !/justJoined/.test(n.cards.innerHTML));
check("a card never both arrives and locks in",
      !(/justShown/.test(n.cards.className) &&
        /justJoined/.test(n.cards.innerHTML)));

console.log("\noff air and back");
env = mkEnv();
draw(env, state(true, {}));                 // shown, entrance spent
draw(env, state(false, {}));                // hidden
n = draw(env, state(true, { TAG: true }));  // shown again
check("coming back on air plays the entrance again",
      /justShown/.test(n.cards.className));
check("and a squad that arrived while it was down does not lock in",
      !/justJoined/.test(n.cards.innerHTML));

console.log("\nthe numbers");
env = mkEnv();
n = draw(env, state(true, { TAG: true, RNTX: true }));
check("the count is of squads actually in", n.joined.textContent === 2,
      String(n.joined.textContent));
check("out of the whole roster", n.total.textContent === 3,
      String(n.total.textContent));
check("both states are named in words, not colour alone",
      /Joined/.test(n.cards.innerHTML) && /Yet To Join/.test(n.cards.innerHTML));

n = draw(env, state(false, { TAG: true }));
check("hidden draws nothing and says nothing",
      !n.stage.classList.contains("visible"));

console.log("\nshort names on the strip");
// 1918px across twelve cards is about 140 each: a registered name of
// any length is three letters and an ellipsis there.
const NAMED = [{ name: "COSMIC ESPORT", shortName: "CE", tagRead: "CSM" },
               { name: "TAG", shortName: "", tagRead: "" },
               { name: "RENU GAMING", shortName: "", tagRead: "RG" },
               { name: "LR7", displayName: "LR SEVEN", shortName: "LR7" }];
function namedState(visible, shortNames){
  return { roster: { teams: NAMED }, lobby: { joined: {}, shortNames: shortNames },
           display: { lobbyStatusVisible: visible } };
}
env = mkEnv();
n = draw(env, namedState(true, true));
check("short name is used when one exists", /CE/.test(n.cards.innerHTML));
check("the tag is the next best thing", /RG/.test(n.cards.innerHTML));
check("a squad with neither keeps its name", /TAG/.test(n.cards.innerHTML));
check("the long name is NOT shown when short names are on",
      !/COSMIC ESPORT/.test(n.cards.innerHTML));

env = mkEnv();
n = draw(env, namedState(true, false));
check("turning it off shows the registered name in full",
      /COSMIC ESPORT/.test(n.cards.innerHTML));
check("and a display name still wins either way",
      /LR SEVEN/.test(n.cards.innerHTML));

env = mkEnv();
n = draw(env, { roster: { teams: NAMED }, lobby: { joined: {} },
                display: { lobbyStatusVisible: true } });
check("short names are the default when nothing is set",
      /CE/.test(n.cards.innerHTML) && !/COSMIC ESPORT/.test(n.cards.innerHTML));

console.log("\ntyping a title must not disturb the cards");
// Both used to share one signature, so a title change tore the row down
// and rebuilt it -- and rebuilding an element restarts every CSS
// animation on it, which is a visible flicker across twelve cards for a
// change that never touched them.
env = mkEnv();
draw(env, state(true, { TAG: true }));
env.nodes.cards.innerHTML = "REBUILT-MARKER";
let st2 = state(true, { TAG: true }); st2.lobby.kicker = "ARROW SHOWDOWN";
n = draw(env, st2);
check("the header text follows", n.kicker.textContent === "ARROW SHOWDOWN",
      n.kicker.textContent);
check("but the cards are NOT rebuilt",
      n.cards.innerHTML === "REBUILT-MARKER");

env.nodes.cards.innerHTML = "REBUILT-MARKER";
let st3 = state(true, { TAG: true, RES: true }); st3.lobby.kicker = "ARROW SHOWDOWN";
n = draw(env, st3);
check("a squad joining still rebuilds them",
      n.cards.innerHTML !== "REBUILT-MARKER");
check("and the count keeps up without a rebuild",
      n.joined.textContent === 2, String(n.joined.textContent));

console.log("\na stale state never undoes a newer tick");
// A full state can be built just before a tick and arrive just after it.
// Applied in that order it flicks the strip back to the old picture.
(function(){
  const vm = require("vm");
  const src = "let _newestLobby = null;\nconst NEWEST_LOBBY_TRUST_MS = 4000;\n" +
    ["_lobbyRev", "_keepNewestLobby"].map(lift).join("\n\n");
  const sb = { Date, Number, Object, console };
  vm.createContext(sb);
  vm.runInContext(src, sb);
  vm.runInContext("_newestLobby = {rev: 5, lobby: {rev: 5, joined: {TAG: true}}," +
                  " visible: true, at: Date.now()};", sb);
  sb.__stale = { lobby: { rev: 4, joined: {} }, display: { lobbyStatusVisible: false } };
  const kept = vm.runInContext("_keepNewestLobby(__stale)", sb);
  check("an older state keeps the newer tick",
        !!kept.lobby.joined.TAG, JSON.stringify(kept.lobby.joined));
  check("and the newer visibility",
        kept.display.lobbyStatusVisible === true);
  sb.__newer = { lobby: { rev: 6, joined: {} }, display: { lobbyStatusVisible: false } };
  const fresh = vm.runInContext("_keepNewestLobby(__newer)", sb);
  check("a NEWER state is taken as it is",
        !fresh.lobby.joined.TAG && fresh.display.lobbyStatusVisible === false);
  // An engine restarted mid-show counts from wherever its saved state
  // left off -- or from nothing. The strip must not ignore it for ever.
  vm.runInContext("_newestLobby.at = Date.now() - 10000;", sb);
  sb.__restart = { lobby: { rev: 1, joined: {} }, display: { lobbyStatusVisible: true } };
  const later = vm.runInContext("_keepNewestLobby(__restart)", sb);
  check("an old guard expires, so a restarted engine is believed",
        !later.lobby.joined.TAG);
})();

console.log("\n" + checks + " checks, " + failures.length + " failed");
if (failures.length) console.log("failed: " + failures.join(", "));
process.exit(failures.length ? 1 : 0);
