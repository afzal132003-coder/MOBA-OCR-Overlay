/* Drives the dashboard's navigation in a headless DOM.
 *
 * Worth having because this file is a live broadcast tool and all five
 * tab-switch handlers were replaced by one: a tab that silently stops
 * switching is a card nobody can reach mid-match, and that is not the
 * kind of thing a syntax check finds.
 *
 * The page opens five WebSockets at load, so those are stubbed. Nothing
 * else about the page is altered -- it loads the real dashboard.html.
 *
 *   npm install jsdom          (once, anywhere on the box)
 *   node dashboard/test_nav.js
 */
const fs = require("fs");
const path = require("path");

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require("jsdom"));
} catch (e) {
  console.log("jsdom is not installed. Install it once, then re-run:");
  console.log("    npm install jsdom");
  console.log("(or run this from a folder that already has it)");
  process.exit(2);
}

const HTML = path.join(__dirname, "dashboard.html");
let pass = 0, fail = 0;
function check(name, ok, detail) {
  console.log((ok ? "  PASS  " : "  FAIL  ") + name + (detail ? "  " + detail : ""));
  ok ? pass++ : fail++;
}

const vc = new VirtualConsole();           // swallow the page's own noise
const dom = new JSDOM(fs.readFileSync(HTML, "utf8"), {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "https://localhost/dashboard.html",
  virtualConsole: vc,
  beforeParse(win) {
    class FakeWS {
      constructor() { this.readyState = 1; FakeWS.made++; }
      send() {} close() {}
    }
    FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
    FakeWS.made = 0;
    win.WebSocket = FakeWS;
    win.scrollTo = () => {};
    win.Element.prototype.scrollIntoView = function () {};
    win.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  },
});

const win = dom.window, doc = win.document;

setTimeout(() => {
  const $ = (s) => doc.querySelector(s);
  const on = (sel) => doc.querySelector(sel + ".active");

  console.log("\nunified tab handler — one code path, four naming schemes");
  check("one handler is wired for every game",
        typeof win.navShowTab === "function" && typeof win.navShowGame === "function");

  // Every game's tabs, driven through the same handler.
  const cases = [
    ["moba",     ".tab-btn",       "tab",    "tab-",    "postmatch"],
    ["valorant", ".val-tab-btn",   "valtab", "valtab-", "liveops"],
    ["dota2",    ".dota2-tab-btn", "d2tab",  "d2tab-",  null],
    ["freefire", ".ff-tab-btn",    "fftab",  "fftab-",  "mapping"],
  ];
  cases.forEach(([game, cls, attr, prefix, tab]) => {
    if (!tab) return;
    win.navShowGame(game);
    const btn = doc.querySelector("#category-" + game + " " + cls + "[data-" + attr + "='" + tab + "']");
    if (!btn) { check(game + " has a " + tab + " tab", false); return; }
    btn.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    const panel = doc.getElementById(prefix + tab);
    check(game + " / " + tab + " switches", panel && panel.classList.contains("active"));
  });

  console.log("\nposition survives a reload (fix 1)");
  win.navShowGame("freefire");
  win.navShowTab("freefire", "liveops");
  check("hash tracks where you are", win.location.hash === "#freefire/liveops",
        win.location.hash);
  win.navShowGame("moba");
  check("hash follows a game change", win.location.hash.startsWith("#moba"),
        win.location.hash);
  // Simulate the reload: set the hash and re-apply it.
  win.history.replaceState(null, "", "#valorant/postmatch");
  win.navApplyHash();
  check("a reload lands where you left it",
        doc.getElementById("valtab-postmatch").classList.contains("active") &&
        doc.getElementById("category-valorant").classList.contains("active"));
  win.history.replaceState(null, "", "#nosuchgame/nope");
  win.navApplyHash();
  check("a junk hash changes nothing",
        doc.getElementById("category-valorant").classList.contains("active"));

  console.log("\nscoping — a game's handler cannot reach another game's panels");
  win.navShowGame("moba");
  win.navShowTab("moba", "prematch");
  const ffStill = doc.getElementById("fftab-liveops");
  check("switching MOBA leaves Free Fire's own tab state alone",
        ffStill && ffStill.classList.contains("active"));

  console.log("\nengine dots (fix 3)");
  win.navPaintStatus();
  const dots = doc.querySelectorAll(".category-btn .nav-dot");
  check("a dot per game that has an engine", dots.length === 5,
        dots.length + " dots");
  check("dots read the socket state", doc.querySelectorAll(".nav-dot.up").length > 0,
        doc.querySelectorAll(".nav-dot.up").length + " up");
  const quiet = doc.querySelector(".category-btn[data-category='cs2'] .nav-dot");
  check("no dot on a game with no engine", !quiet);

  console.log("\ndropdown helper (fix 2)");
  const sel = doc.createElement("select");
  doc.body.appendChild(sel);
  check("fills a select", win.setOptions(sel, ["a", "b", "c"]) && sel.options.length === 3);
  check("skips an unchanged rebuild", win.setOptions(sel, ["a", "b", "c"]) === false);
  sel.value = "b";
  win.setOptions(sel, ["a", "b", "c", "d"]);
  check("keeps the selection across a real rebuild", sel.value === "b", sel.value);
  sel.focus();
  const before = sel.options.length;
  check("refuses to redraw a select being used",
        win.setOptions(sel, ["x", "y"]) === false && sel.options.length === before);
  sel.blur();

  console.log("\ncard index and folding (fixes 4 and 5)");
  const idx = doc.querySelectorAll(".card-index");
  check("heavy tabs got a jump list", idx.length > 0, idx.length + " tabs indexed");
  const heavy = doc.querySelector("#fftab-liveops .card-index");
  check("the heaviest static tab is one of them", !!heavy,
        heavy ? heavy.querySelectorAll("a").length + " links" : "missing");
  // Cards built when a tab is first opened must be picked up too -- the
  // graphics tabs ship almost empty and fill themselves on show.
  const gfx = doc.getElementById("tab-graphics");
  const beforeN = gfx.querySelectorAll("[data-nav-card]").length;
  win.navShowTab("moba", "graphics");
  check("late-built cards get enhanced on show",
        gfx.querySelectorAll("[data-nav-card]").length >= beforeN,
        gfx.querySelectorAll("[data-nav-card]").length + " enhanced");
  check("no duplicate index after re-running",
        gfx.querySelectorAll(":scope > .card-index").length <= 1);
  const carets = doc.querySelectorAll("#fftab-liveops .card-caret").length;
  win.navShowTab("freefire", "liveops");
  check("re-running adds no second caret",
        doc.querySelectorAll("#fftab-liveops .card-caret").length === carets,
        carets + " carets");
  const head = doc.querySelector("#category-bgmi .card-head");
  check("card headings became controls", !!head && head.getAttribute("role") === "button");
  if (head) {
    const card = head.closest(".card");
    head.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    check("a card folds", card.classList.contains("folded"));
    check("and says so to a screen reader", head.getAttribute("aria-expanded") === "false");
    head.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
    check("and unfolds", !card.classList.contains("folded"));
  }

  console.log("\njump to any card (fix 7)");
  win.navOpenPalette();
  check("palette opens", doc.getElementById("navPalette").classList.contains("on"));
  doc.getElementById("navPaletteInput").value = "";
  win.navFilterPalette();
  let all = JSON.parse(doc.getElementById("navPaletteList").dataset.hits || "[]");
  check("it indexed cards across the dashboard", all.length >= 20,
        all.length + " shown (capped at 40)");
  const games = new Set(all.map((i) => i.game));
  check("from more than one game", games.size >= 2, [...games].join(", "));
  doc.getElementById("navPaletteInput").value = "booyah";
  win.navFilterPalette();
  const rows = doc.querySelectorAll(".nav-pal-row");
  check("filters on what you type", rows.length > 0 && rows.length < 20,
        rows.length + " hits");
  const hits = JSON.parse(doc.getElementById("navPaletteList").dataset.hits || "[]");
  if (hits.length) {
    win.navGoPalette(hits[0]);
    check("choosing one switches game and tab",
          doc.getElementById("category-" + hits[0].game).classList.contains("active"),
          hits[0].game + " / " + hits[0].tab);
  }
  check("palette closes", !doc.getElementById("navPalette").classList.contains("on"));

  console.log("\n" + (pass + fail) + " checks, " + fail + " failed");
  process.exit(fail ? 1 : 0);
}, 1200);
