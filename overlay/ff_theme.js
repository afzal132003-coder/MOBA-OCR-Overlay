/* Shared helpers for the themed Free Fire cards (see ff_theme.css). */

// "ss3" for the Survivor Series package, "arrow" for everything else --
// PRG ARROW, and the older packages that never had art for these cards.
function ffTheme(state){
  const ev = (state && state.event) || {};
  const pick = String(new URLSearchParams(location.search).get("theme") || ev.assetFolder || "").toLowerCase();
  // ARROW only when an ARROW package is picked; everything else draws in
  // the SS3 red (a package with no art of its own for these, like CLT or
  // GC, used to fall to the green ARROW look).
  return pick.includes("arrow") ? "arrow" : "ss3";
}
// CHIRAYU GOLD LEAGUE is the SS3 package copied: the same cards, drawn
// from its own recoloured art (assets/freefire/CHIRAYU/) and, on top of
// body.ss3, body.chirayu for the kit's navy / slate / gold.
function ffChirayu(state){
  const ev = (state && state.event) || {};
  return String(new URLSearchParams(location.search).get("theme") || ev.assetFolder || "")
    .toLowerCase().includes("chirayu");
}
// The folder an SS3-layout card takes its art from.
function ffArtDir(state){
  return "assets/freefire/" + (ffChirayu(state) ? "CHIRAYU" : "ss3") + "/";
}
function ffApplyTheme(state){
  const t = ffTheme(state);
  document.body.classList.toggle("ss3", t === "ss3");
  document.body.classList.toggle("arrow", t !== "ss3");
  document.body.classList.toggle("chirayu", t === "ss3" && ffChirayu(state));
  return t;
}
function ffTeam(name, roster){
  const key = String(name || "").trim().toUpperCase();
  if(!key) return null;
  return ((roster && roster.teams) || []).find(t =>
    [t.name, t.displayName, t.shortName].some(n => String(n || "").trim().toUpperCase() === key)) || null;
}
function ffRosterPlayer(uid, ign, roster){
  const u = String(uid || ""), g = String(ign || "").trim().toUpperCase();
  for(const t of (roster && roster.teams) || []){
    for(const p of t.players || []){
      if((u && String(p.uid || "") === u) || (g && String(p.ign || "").trim().toUpperCase() === g)) return { team: t, player: p };
    }
  }
  return null;
}
function ffEsc(s){ return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
const ffTwo = v => v == null ? "--" : String(v).padStart(2, "0");
function ffMmss(t){ if(t == null) return "--"; t = Math.max(0, Math.round(t)); return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0"); }
// Shrinks a box's text until it fits -- measured on an inner span, since a
// centred box does not report overflow on its left.
function ffSetText(node, text, max, min){
  node.innerHTML = "";
  const s = document.createElement("span"); s.textContent = text == null ? "" : text; node.appendChild(s);
  node.style.fontSize = "";
  let size = parseFloat(getComputedStyle(node).fontSize);
  const limit = max || node.clientWidth - 8;
  while(s.offsetWidth > limit && size > (min || 10)){ size -= 0.5; node.style.fontSize = size + "px"; }
}
// Numbers count up as they come in; format(v) draws each step.
function ffCountUp(node, to, format, delay){
  const fmt = format || (v => String(v));
  if(to == null || typeof to !== "number"){ node.textContent = to == null ? "--" : String(to); return; }
  if(document.body.classList.contains("still")){ node.textContent = fmt(to); return; }
  node.textContent = fmt(0);
  const start = performance.now() + (delay || 0) * 1000, dur = 700;
  const step = now => {
    const f = Math.max(0, Math.min(1, (now - start) / dur));
    node.textContent = fmt(Math.round(to * (1 - Math.pow(1 - f, 3))));
    if(f < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
// The standard connection, roster-carry included; onMsg gets every
// message, onState every full state.
function ffConnect(page, onState, onMsg){
  const params = new URLSearchParams(location.search);
  const sameOrigin = (location.protocol === "https:" && location.hostname && !/^(localhost|127\.|\[?::1)/.test(location.hostname))
    ? "wss://" + location.host : "";
  const relay = params.get("relay") || localStorage.getItem("moba_relay") || sameOrigin;
  const token = params.get("token") || localStorage.getItem("moba_token") || "";
  const url = relay
    ? relay + (relay.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) + "&page=" + page + "&rostercache=1"
    : "ws://localhost:8765?page=" + page + "&rostercache=1";
  let lastRoster = null;
  (function go(){
    const ws = new WebSocket(url);
    ws.onmessage = evt => {
      const msg = JSON.parse(evt.data);
      if(onMsg) onMsg(msg);
      if(msg.type !== "state_sync" || !msg.data) return;
      if(msg.data.roster) lastRoster = msg.data.roster;
      else if(lastRoster) msg.data.roster = lastRoster;
      onState(msg.data);
    };
    ws.onclose = () => setTimeout(go, 2000);
    ws.onerror = () => ws.close();
  })();
}

// The title, shrunk until its text fits its box (SS3: between the ribbon
// and the event logo). Measured on the text itself, not the box.
function ffFitTitle(el, again){
  if(!el) return;
  // Measured again once the fonts have loaded: before that the fallback
  // face is narrower, and the title fitted to it ran off its box.
  if(!again){
    if(document.fonts && document.fonts.status !== "loaded") document.fonts.ready.then(() => ffFitTitle(el, true));
    // and once more shortly after: a stylesheet added late declares its
    // fonts after the first check.
    setTimeout(() => ffFitTitle(el, true), 1200);
  }
  // CHIRAYU: the last word in orange-gold, the rest black.
  const words = (el.textContent || "").trim().split(/\s+/);
  const gold = document.body.classList.contains("chirayu") && words.length > 1;
  if(gold && !el.querySelector(".w2")){
    el.textContent = words.slice(0, -1).join(" ") + " ";
    const w = document.createElement("span"); w.className = "w2"; w.textContent = words[words.length - 1];
    el.appendChild(w);
  } else if(!gold && el.querySelector(".w2")){
    el.textContent = words.join(" ");
  }
  el.style.fontSize = "";
  const r = document.createRange();
  r.selectNodeContents(el);
  let size = parseFloat(getComputedStyle(el).fontSize);
  while(r.getBoundingClientRect().width > el.clientWidth - 10 && size > 40){
    size -= 2; el.style.fontSize = size + "px";
  }
}
