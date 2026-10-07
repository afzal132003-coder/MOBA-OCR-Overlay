/* BUNDLED SOURCES.

   A Free Fire page loaded inside one of the combined sources
   (freefire_postmatch.html, freefire_ingame.html) arrives with ?embed=1.
   It must not open its own connection: the bundle holds ONE and hands each
   message down -- a dozen pages each pulling the full state over the relay
   would be a dozen times the traffic for one OBS source.

   So this swaps in a stand-in WebSocket before the page's own script runs.
   The page's code drives it exactly as it drives a real one (onopen,
   onmessage with the same raw JSON text, send), so no page needed its
   connection code rewritten, and every page still works on its own as a
   separate source when opened without ?embed=1. */
(function(){
  if(new URLSearchParams(location.search).get("embed") !== "1") return;
  window.FF_EMBEDDED = true;

  function Embedded(url){
    const self = this;
    this.url = url;
    this.readyState = 0;
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    this._listeners = {};
    window.addEventListener("message", e => {
      if(e.source !== window.parent) return;
      const d = e.data;
      if(!d || d.__ffBundle !== "msg") return;
      const evt = { data: d.data };
      if(self.onmessage) self.onmessage(evt);
      (self._listeners.message || []).forEach(fn => fn(evt));
    });
    setTimeout(() => {
      self.readyState = 1;
      if(self.onopen) self.onopen({});
      (self._listeners.open || []).forEach(fn => fn({}));
      // Asks the bundle for the latest state, so a page that finishes
      // loading after the first sync still starts complete.
      window.parent.postMessage({ __ffBundle: "ready" }, "*");
    }, 0);
  }
  Embedded.prototype.send = function(m){ window.parent.postMessage({ __ffBundle: "send", data: m }, "*"); };
  Embedded.prototype.close = function(){};
  Embedded.prototype.addEventListener = function(type, fn){ (this._listeners[type] = this._listeners[type] || []).push(fn); };
  Embedded.CONNECTING = 0; Embedded.OPEN = 1; Embedded.CLOSING = 2; Embedded.CLOSED = 3;
  window.WebSocket = Embedded;
})();
