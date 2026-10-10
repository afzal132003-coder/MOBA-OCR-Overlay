"""
MOBA OCR Overlay -- Cloud Relay Server.

A small always-on WebSocket hub so the OCR engine (running on whichever PC
has the game up that day), admin dashboard users, and the OBS/vMix overlay
pages can all reach the same live state from anywhere -- not just
localhost. Deploy this on its own (Railway/Render/Fly.io -- NOT Vercel,
which doesn't support long-lived WebSocket connections). See README.md in
this folder for step-by-step deploy instructions.

This folder lives under ocr/ for repo convenience, but it is NOT run
alongside ocr_engine.py -- it gets deployed by itself, separately, to a
cloud host. ocr_engine.py then connects OUT to it as a client, same as
every dashboard/overlay page does.

Three roles, each its own shared secret token (set as environment
variables on whatever host runs this):
  OCR_TOKEN    - ocr_engine.py connects with this. Can read and write.
  ADMIN_TOKEN  - dashboard.html connects with this. Can read and write.
  VIEWER_TOKEN - overlay.html / prematch.html / postmatch.html connect
                 with this. Read-only -- the relay silently drops
                 anything a viewer-role connection tries to send.

This relay does NOT understand the meaning of any message -- it just
authenticates the connection, caches the most recent "state_sync" message
(so a client that joins mid-match isn't blank until the next change), and
fans every other message out to all other connected clients. All the real
logic (OCR reading, state merging, calibration, extraction, turtle timing)
stays exactly where it already was, in ocr_engine.py.

One exception, added for valorant_engine.py's page-targeted broadcasts
(see broadcast_to_page() there): an engine only ever holds ONE connection
to this relay, representing however many real dashboard/overlay browser
tabs are actually on the other end of it -- it has no way to address just
one of them directly the way it can for its own local (non-relay)
clients. A message may carry an optional "_target_pages" list (page names,
matching the same ?page= each client connects with); if present, this
relay narrows its own fan-out to just the peers whose page is in that
list, instead of sending to everyone. Messages without that field (the
MOBA/Free Fire engines never send it) are unaffected -- fanned out to all
peers exactly as before.
"""

import asyncio
import json
import os
import time
from collections import deque
from urllib.parse import urlparse, parse_qs

import websockets
# Imported by name rather than reached for as websockets.exceptions.X.
# requirements.txt pins nothing, so this deploys against whatever version
# is current, and newer ones expose their submodules through a lazy
# import shim where that attribute is not always resolvable. An `except`
# clause is only evaluated when something is raised -- so a reference
# that does not resolve sits there working perfectly until the first
# client disconnects, and then throws AttributeError from inside the
# handler that exists to deal with disconnections.
from websockets.exceptions import ConnectionClosed

PORT = int(os.environ.get("PORT", "8765"))

TOKEN_ROLES = {}
for env_name, role in (("OCR_TOKEN", "ocr"), ("ADMIN_TOKEN", "admin"), ("VIEWER_TOKEN", "viewer")):
    token = os.environ.get(env_name, "").strip()
    if token:
        TOKEN_ROLES[token] = role

# websocket -> role, for every currently-connected client
connected = {}
# websocket -> the "page" query param it connected with (overlay.html,
# prematch.html, postmatch.html each send their own name), so admins can
# see which OBS/vMix browser sources are actually reachable right now.
connected_pages = {}
# The most recent full state_sync payload (raw JSON text), so a client that
# connects between updates still gets the current state immediately instead
# of sitting blank until something changes.
last_state_sync = None

# The last roster seen on a state_sync, kept so the engine does not have
# to send it again every time the table moves.
#
# The engine holds ONE connection here and it is the only link in the
# chain that crosses the sender's own internet upload. The state it
# pushes is ~269KB, of which ~224KB is the roster: twelve teams each
# carrying a base64 logo of around 9KB, unchanged from the first poll of
# a match to the last. The rows that actually changed are ~4.5KB. Several
# times a second that is over 1MB/s of upload, which an ordinary
# connection at a venue cannot carry -- and what it looks like from the
# desk is the graphic trailing the game by seconds while a dashboard on
# the same LAN is instant, and a browser source taking an age to come
# back after a refresh because its opening snapshot is queued behind a
# backlog that never clears.
#
# So the engine may leave the roster out, and this puts it back before
# anything else here sees the message. That keeps the saving on the link
# that is actually short of room, while everything downstream -- the
# fan-out, and the cached snapshot a late joiner is served -- still gets
# a complete state. No page has to know anything about it.
#
# Re-injecting HERE rather than teaching the pages to cache it is
# deliberate. The note further down about only caching full snapshots is
# the same lesson from the Valorant side: a trimmed payload that reaches
# the cache is served to the next page that reconnects, and a page
# rendering a state with no roster shows blank logos and full team names
# where short ones belong. That reached air once already.
last_roster = None

# websocket -> whether it connected with rostercache=1.
#
# Putting the roster back for EVERYONE, which is what this did at first,
# fixes the uplink and leaves the fan-out exactly as heavy as it was: the
# engine sends 48KB, and this turns it back into 269KB for every client
# on the far side. Measured with that in place, the engine was publishing
# 3.6 states a second and subscribers here were seeing 0.3 -- the relay's
# own egress had become the bottleneck the uplink used to be.
#
# So a page that has said it can keep a roster is sent the small payload
# untouched, and only a page that has not is served the re-inflated one.
# The cached snapshot stays full whatever happens, because a late joiner
# has no roster to keep yet.
roster_cache_peers = {}


# websocket -> Outbox. Every byte bound for a peer goes through its own.
outboxes = {}

# ONE STATE IN FLIGHT PER PAGE.
#
# The Outbox below coalesces states for a peer whose socket is slow to
# drain. Behind Caddy that never happens: this process hands each frame to
# the proxy on the same machine, which takes it at once, and the backlog
# builds in the proxy's and the kernel's buffers instead -- megabytes per
# page, where nothing here can see it or drop a stale state. Measured on
# 2026-10-10: the graphics on titanium-relay ran 10-19 s behind the engine,
# and six changes in eight never arrived at all.
#
# So every state is followed by a ping, and the next state waits for its
# pong. A browser answers a ping by itself the moment the frames before it
# have arrived -- no page needs to know about any of this, old cached
# copies included -- so the pong says the state has been DELIVERED, across
# the proxy and the far link. Until then, anything newer replaces the
# state waiting: however slow or far the link, a page is one state behind
# the latest at worst, never a queue of them. A pong that does not come
# within DELIVERY_WAIT_SECONDS is given up on, so a page that has gone
# quiet is still sent the latest now and then.
DELIVERY_WAIT_SECONDS = float(os.environ.get("RELAY_DELIVERY_WAIT", "2.5"))
# How often the admins (and the engine) are told how each page is doing.
HEALTH_SECONDS = float(os.environ.get("RELAY_HEALTH_SECONDS", "5"))


class Outbox:
    """Everything bound for one peer, written by that peer's own task.

    WHY THIS EXISTS. The fan-out used to send each engine message to every
    peer and wait for all of them -- up to five seconds each -- before it
    read the engine's NEXT message. So the slowest page on the relay set
    the pace for every other page, whatever game it belonged to. A
    backgrounded browser tab is enough: the browser throttles it, its
    socket stops draining, and the relay sits out the full deadline on it.

    Measured from a subscriber on the live relay: inter-arrival gaps of
    exactly 5003ms -- the deadline to the millisecond -- five of them over
    two seconds in 25s, with bursts of messages 0ms apart after each.
    Holes then bursts is what one blocked writer looks like from the far
    side. On air it was a lobby strip that took longer than twelve
    seconds to show a tick, while the round trip to this machine was 18ms.

    Now the reader only ever drops a message into each outbox and moves
    on, and each peer's own task drains it at whatever speed that peer
    manages. A slow peer delays nobody but itself.

    STATE IS COALESCED. A state_sync is a whole snapshot, so if one is
    still waiting when a newer one arrives the old one is never sent: a
    slow peer catches up by receiving the latest state once, not by
    working through a backlog of stale ones it would only overwrite.
    Everything else keeps its order. The superseded snapshot's place is
    simply skipped, so nothing newer than it is overtaken by anything
    older.

    THE ONE THING A NEWER STATE MAY NOT DROP is a roster. A peer that
    keeps rosters is sent states without one, and only gets the roster on
    the message where it changed -- so if that message is superseded
    before it is written, the replacement goes out in its full form
    instead, or the peer would carry the old roster for the rest of the
    match.
    """

    # Room for a burst of ordinary messages. Only a peer that has stopped
    # reading entirely ever reaches it, and then the oldest go first.
    LIMIT = 256

    def __init__(self, ws):
        self.ws = ws
        self.items = deque()      # cells: [payload, carries_roster, is_state]
        self.state_cell = None    # the state_sync still waiting, if any
        self.wake = asyncio.Event()
        # The state now on its way: when it was sent, and the pong that
        # says it arrived (see DELIVERY_WAIT_SECONDS).
        self.sent_at = None
        self.pong = None
        self.trips = deque(maxlen=30)      # (when, seconds to deliver)
        self.task = asyncio.create_task(self._run())

    def put(self, payload):
        """An ordinary message: kept, and sent in order."""
        self._append([payload, False, False])

    def put_state(self, payload, carries_roster, full_payload):
        cell = self.state_cell
        if cell is not None:
            if cell[1] and not carries_roster:
                payload, carries_roster = full_payload, True
            cell[0] = None                       # superseded -- skipped
        self.state_cell = [payload, carries_roster, True]
        self._append(self.state_cell)

    def health(self):
        now = time.monotonic()
        recent = [d for t, d in self.trips if now - t <= 30]
        waiting = self.pong is not None and not self.pong.done() and self.sent_at is not None
        return {"lastMs": round(self.trips[-1][1] * 1000) if self.trips else None,
                "worstMs": round(max(recent) * 1000) if recent else None,
                "waitingMs": round((now - self.sent_at) * 1000) if waiting else 0}

    async def _track_delivery(self):
        """A ping behind the state just sent; its pong, recorded."""
        sent = self.sent_at = time.monotonic()
        try:
            self.pong = await self.ws.ping()
        except Exception:
            self.pong = None
            return

        def landed(f):
            if not f.cancelled() and f.exception() is None:
                self.trips.append((time.monotonic(), time.monotonic() - sent))
        self.pong.add_done_callback(landed)

    def _append(self, cell):
        if len(self.items) >= self.LIMIT:
            dropped = self.items.popleft()
            if dropped is self.state_cell:
                self.state_cell = None
        self.items.append(cell)
        self.wake.set()

    async def _run(self):
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                while self.items:
                    cell = self.items[0]
                    # A state while the last is still on its way waits for
                    # it to land -- left at the head, and still the state
                    # cell, so a newer one replaces it there.
                    if (cell[2] and cell[0] is not None
                            and self.pong is not None and not self.pong.done()):
                        left = self.sent_at + DELIVERY_WAIT_SECONDS - time.monotonic()
                        if left > 0:
                            try:
                                await asyncio.wait_for(asyncio.shield(self.pong), left)
                            except (asyncio.TimeoutError, ConnectionClosed):
                                pass
                        self.pong = None
                        continue
                    self.items.popleft()
                    if cell is self.state_cell:
                        self.state_cell = None
                    if cell[0] is None:
                        continue
                    # No deadline here, deliberately. A send waits for the
                    # socket to drain, and cancelling that wait does not
                    # un-send anything -- it only lets the next frame pile
                    # in behind it, so a stuck peer's buffer would grow
                    # without limit. Waiting holds at most one frame in
                    # flight, the queue behind it coalesces, and nobody
                    # else is waiting on this task. A peer that is
                    # genuinely gone is closed by the keepalive, and the
                    # send then raises.
                    await self.ws.send(cell[0])
                    if cell[2]:
                        await self._track_delivery()
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as e:
            print("[outbox] writer for a peer stopped: %r" % (e,))

    def close(self):
        self.task.cancel()


def role_for_token(token):
    return TOKEN_ROLES.get(token)


def presence_counts():
    counts = {}
    for page in connected_pages.values():
        counts[page] = counts.get(page, 0) + 1
    return counts


def role_counts():
    """How many of each role are connected.

    Pages alone cannot answer "is an OCR engine attached", which is the
    one thing worth alerting on: an engine connects with a token and no
    page parameter, so it lands in the "unknown" bucket alongside
    anything else that forgot one. The role is what the relay actually
    authenticated, so the role is what gets reported.

    Additive -- older pages ignore the extra field."""
    counts = {}
    for role in connected.values():
        counts[role] = counts.get(role, 0) + 1
    return counts


async def broadcast_presence():
    if not connected:
        return
    raw = json.dumps({"type": "presence", "pages": presence_counts(),
                      "roles": role_counts()})
    # Into each outbox, never awaited. This runs on every connect and
    # disconnect, BEFORE a new peer is sent its first state -- and it used
    # to be a bare send to every peer in turn with no deadline at all, so
    # one stuck peer held every page that connected after it blank until
    # it cleared. That was the browser source that "loads late" after a
    # refresh.
    for peer in list(connected):
        box = outboxes.get(peer)
        if box is not None:
            box.put(raw)


async def handler(websocket):
    global last_state_sync, last_roster

    query = parse_qs(urlparse(websocket.request.path).query)
    token = (query.get("token") or [""])[0]
    role = role_for_token(token)

    if not role:
        await websocket.close(code=4401, reason="invalid or missing token")
        return

    connected[websocket] = role
    connected_pages[websocket] = (query.get("page") or ["unknown"])[0]
    # A page that says it keeps the last roster it was sent can be given
    # the small payload as it arrived, instead of the re-inflated one.
    roster_cache_peers[websocket] = (query.get("rostercache") or ["0"])[0] == "1"
    outboxes[websocket] = Outbox(websocket)
    _start_health()
    print(f"[connect] role={role} total_connected={len(connected)}")
    await broadcast_presence()
    try:
        # Tell a client what it actually is.
        #
        # A viewer that sends anything has it silently dropped, by
        # design -- but "silently" meant an admin dashboard opened with
        # the VIEWER token showed live data, said CONNECTED, and threw
        # every tick away without a word. Indistinguishable from the
        # whole system being broken, and it cost a live session.
        #
        # Additive: anything that does not know this message ignores it.
        outboxes[websocket].put(json.dumps({"type": "whoami", "role": role}))

        # The opening snapshot is an ORDINARY message, not a state: it
        # must never be superseded by the next lean one, because this
        # peer has no roster yet and a state without one would leave it
        # with blank logos for the rest of the match.
        if last_state_sync is not None:
            outboxes[websocket].put(last_state_sync)

        async for raw in websocket:
            if role == "viewer":
                continue  # read-only role -- silently drop anything it sends

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # See the file-level comment above -- an engine can't address
            # just one of its own relay-connected pages directly, since it
            # only ever holds this one connection representing all of
            # them. target_pages is None (fan out to everyone, the
            # original/default behavior) unless the sender explicitly
            # narrowed it.
            target_pages = msg.get("_target_pages")

            # Put the roster back before this message is cached or sent
            # on. See last_roster above for why the engine is allowed to
            # leave it out in the first place.
            # `raw` is what a page that cannot keep a roster gets, and
            # what is cached. `lean` is the message exactly as it arrived,
            # for pages that can -- None when there is nothing to save.
            lean = None
            if msg.get("type") == "state_sync":
                data = msg.get("data")
                if isinstance(data, dict):
                    if data.get("roster") is not None:
                        last_roster = data["roster"]
                    elif last_roster is not None:
                        lean = raw
                        data["roster"] = last_roster
                        # Re-serialised, because `raw` is what actually
                        # gets cached and forwarded below.
                        raw = json.dumps(msg)

            if msg.get("type") == "state_sync" and target_pages is None:
                # Only cache FULL, untargeted state_sync messages as "the
                # current state" for late joiners -- an explicit fix after
                # a real capture showed a newly-(re)connecting page
                # sometimes getting served a stale, WRONGLY-SCOPED snapshot
                # instead. valorant_engine.py's own bandwidth optimization
                # sends some fields (e.g. team logos) trimmed out via
                # _target_pages to just the one page that doesn't need
                # them (valorant_playerstats) -- if THAT narrowed message
                # got cached here as the global "last known state", the
                # next unrelated page to (re)connect (e.g. valorant_ingame,
                # which DOES need the real logo) would get served the
                # trimmed version and render it as if it were the real
                # current state, visible live as the team logo flickering
                # off and back on every time that page's connection blips
                # and reconnects (routine over a real internet relay).
                # Caching only full snapshots means a late joiner always
                # gets a complete one; it may be a poll cycle stale, but
                # never missing fields that were only trimmed for a
                # DIFFERENT page's benefit.
                last_state_sync = raw

            # Into each peer's outbox and straight on to the next message.
            # Nothing here waits on any peer -- see Outbox.
            is_state = msg.get("type") == "state_sync"
            for peer in list(connected):
                if peer is websocket:
                    continue
                if target_pages is not None and connected_pages.get(peer) not in target_pages:
                    continue
                box = outboxes.get(peer)
                if box is None:
                    continue
                use_lean = lean is not None and roster_cache_peers.get(peer)
                payload = lean if use_lean else raw
                if is_state:
                    box.put_state(payload, carries_roster=not use_lean,
                                  full_payload=raw)
                else:
                    box.put(payload)
    finally:
        connected.pop(websocket, None)
        connected_pages.pop(websocket, None)
        roster_cache_peers.pop(websocket, None)
        box = outboxes.pop(websocket, None)
        if box is not None:
            box.close()
        print(f"[disconnect] role={role} total_connected={len(connected)}")
        await broadcast_presence()


_health_task = None


def _start_health():
    global _health_task
    if _health_task is None or _health_task.done():
        _health_task = asyncio.ensure_future(_health_loop())


async def _health_loop():
    """Every HEALTH_SECONDS, to the admins and the engine: each page, and
    how long its latest state took to land -- so a source falling behind
    is seen on the dashboard, not discovered on air."""
    while True:
        await asyncio.sleep(HEALTH_SECONDS)
        try:
            peers = []
            for ws, page in list(connected_pages.items()):
                if connected.get(ws) != "viewer":
                    continue
                box = outboxes.get(ws)
                peers.append(dict({"page": page}, **(box.health() if box is not None else {})))
            raw = json.dumps({"type": "relay_health", "at": int(time.time() * 1000),
                              "waitMs": int(DELIVERY_WAIT_SECONDS * 1000), "peers": peers})
            for ws, role in list(connected.items()):
                if role in ("admin", "ocr"):
                    box = outboxes.get(ws)
                    if box is not None:
                        box.put(raw)
        except Exception as e:
            print("[health] %r" % (e,))


async def main():
    if not TOKEN_ROLES:
        print(
            "WARNING: none of OCR_TOKEN / ADMIN_TOKEN / VIEWER_TOKEN are set "
            "-- every connection attempt will be rejected. Set them as "
            "environment variables on your hosting platform."
        )
    # Default max_size (1 MiB) is too small once state_sync carries an
    # embedded base64 team logo or two -- the relay was closing every
    # connection with code 1009 ("message too big") the moment a real
    # state_sync tried to pass through, which broke the connection in an
    # endless connect/reject/retry loop. 16 MiB comfortably covers a couple
    # of uploaded logo images plus everything else in server_state.
    # Bind address, because "reachable from the internet" should be a
    # decision rather than a default. Behind Caddy the relay only ever
    # needs to answer localhost, and a directly-reachable port is an
    # unencrypted way in to the same service -- tokens in clear text on
    # the wire, for no gain. Default stays 0.0.0.0 so a plain local run
    # still works; the deployed unit sets HOST=127.0.0.1.
    host = os.environ.get("HOST", "0.0.0.0")
    # Patience with a busy engine.
    #
    # The default is a ping every 20s and a 20s grace, and the OCR engine
    # blows through that: it captures, OCRs and merges twelve rows four
    # times a second, and when that bunches up it stops answering pings
    # for long enough to be hung up on. Measured live: nine keepalive
    # timeouts in ten minutes, the engine off the relay about one minute
    # in six -- and every elimination tick pressed during those windows
    # was silently dropped, because there was nothing on the other end.
    #
    # A minute's grace still catches a genuinely dead peer (the socket
    # itself errors long before that when a machine really goes), while
    # no longer punishing a working engine for being busy.
    async with websockets.serve(handler, host, PORT,
                                max_size=16 * 1024 * 1024,
                                ping_interval=20, ping_timeout=60):
        print(f"Relay listening on {host}:{PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
