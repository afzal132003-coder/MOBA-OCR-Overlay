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
const delays = (n.cards.innerHTML.match(/animation-delay:(\d+)ms/g) || [])
  .map(d => Number(d.replace(/\D/g, "")));
check("each card is staggered off the one before",
      delays.length === TEAMS.length &&
      delays.every((d, i) => i === 0 || d > delays[i - 1]),
      JSON.stringify(delays));
check("and the cascade waits for the strip itself to arrive",
      delays[0] > 0, delays[0] + "ms before the first card");

// A squad gets ticked. The row is rebuilt -- but the strip never left.
n = draw(env, state(true, { TAG: true, RES: true }));
check("ticking a squad does NOT send every card flying again",
      !/justShown/.test(n.cards.className), n.cards.className);
check("and the squad that just arrived locks in",
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

console.log("\n" + checks + " checks, " + failures.length + " failed");
if (failures.length) console.log("failed: " + failures.join(", "));
process.exit(failures.length ? 1 : 0);
