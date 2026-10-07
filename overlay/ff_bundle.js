/* THE COMBINED SOURCE -- one connection, several Free Fire graphics.

   Used by freefire_postmatch.html and freefire_ingame.html. Each graphic
   is its own page in a full-screen iframe (?embed=1, see ff_embed.js), so
   every graphic keeps its own look, layout and animation untouched; this
   only owns the connection and decides who is told what.

   WHO GETS WHAT. A state goes to a graphic only while it is up (and once
   more as it comes down, so it can animate out) -- a hidden graphic has
   nothing to draw, and copying the state into ten pages twenty times a
   second is real work for an OBS browser source. Two exceptions: a state
   that carries the roster (logos) goes to every page, because the engine
   sends the roster only when it changes and a page that missed it would
   go up without logos; and anything that is not a state (photos, pop-ups)
   goes to every page, which ignores what is not for it. */
function ffBundle(PAGE, CHILDREN){
  const params = new URLSearchParams(location.search);
  const sameOrigin = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
  const relay = params.get("relay") || localStorage.getItem("moba_relay") || sameOrigin;
  const token = params.get("token") || localStorage.getItem("moba_token") || "";
  const WS_URL = relay
    ? relay + (relay.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) + "&page=" + PAGE + "&rostercache=1"
    : "ws://localhost:8765?page=" + PAGE + "&rostercache=1";
  // ?preview=1 is passed down, and then every graphic is fed always.
  const PREVIEW = params.get("preview") === "1";
  const pass = new URLSearchParams();
  params.forEach((v, k) => { if(k !== "relay" && k !== "token") pass.set(k, v); });
  pass.set("embed", "1");

  let ws, lastRosterMsg = null, lastStateMsg = null, lastState = null;
  const kids = CHILDREN.map(c => {
    const f = document.createElement("iframe");
    f.src = c.page + ".html?" + pass.toString();
    f.setAttribute("allowtransparency", "true");
    f.style.cssText = "position:absolute;left:0;top:0;width:1920px;height:1080px;border:0;background:transparent;";
    // A graphic that is down is not just blank but HIDDEN, frame and all:
    // some pages (match insights) paint a full-screen background.
    f.style.visibility = (c.active && !PREVIEW) ? "hidden" : "visible";
    document.body.appendChild(f);
    return { c, f, ready: false, wasOn: false, hideTimer: 0 };
  });
  // Shown the moment it goes up; hidden a beat after it comes down, so
  // its own exit animation plays first.
  function showFrame(k, on){
    if(!k.c.active || PREVIEW) return;
    clearTimeout(k.hideTimer);
    if(on) k.f.style.visibility = "visible";
    else k.hideTimer = setTimeout(() => { k.f.style.visibility = "hidden"; }, 1500);
  }
  const post = (k, raw) => { if(k.ready && k.f.contentWindow) k.f.contentWindow.postMessage({ __ffBundle: "msg", data: raw }, "*"); };
  const isOn = (k, st) => PREVIEW || !k.c.active || !!k.c.active(st);

  window.addEventListener("message", e => {
    const k = kids.find(x => x.f.contentWindow === e.source);
    if(!k || !e.data) return;
    if(e.data.__ffBundle === "ready"){
      k.ready = true;
      if(lastRosterMsg) post(k, lastRosterMsg);
      if(lastStateMsg && lastStateMsg !== lastRosterMsg) post(k, lastStateMsg);
      k.wasOn = lastState ? isOn(k, lastState) : false;
      showFrame(k, k.wasOn);
    } else if(e.data.__ffBundle === "send" && ws && ws.readyState === 1){
      ws.send(e.data.data);
    }
  });

  function connect(){
    ws = new WebSocket(WS_URL);
    ws.onmessage = evt => {
      const raw = evt.data;
      let msg;
      try { msg = JSON.parse(raw); } catch(e){ return; }
      if(msg.type === "state_sync" && msg.data){
        const withRoster = !!msg.data.roster;
        if(withRoster) lastRosterMsg = raw;
        lastStateMsg = raw;
        lastState = msg.data;
        kids.forEach(k => {
          const on = isOn(k, msg.data);
          if(withRoster || on || k.wasOn) post(k, raw);
          if(on !== k.wasOn) showFrame(k, on);
          k.wasOn = on;
        });
      } else {
        kids.forEach(k => post(k, raw));
      }
    };
    ws.onclose = () => setTimeout(connect, 2000);
    ws.onerror = () => ws.close();
  }
  connect();
}
