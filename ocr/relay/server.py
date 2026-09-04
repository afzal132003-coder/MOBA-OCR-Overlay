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
from urllib.parse import urlparse, parse_qs

import websockets

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


def role_for_token(token):
    return TOKEN_ROLES.get(token)


def presence_counts():
    counts = {}
    for page in connected_pages.values():
        counts[page] = counts.get(page, 0) + 1
    return counts


async def broadcast_presence():
    if not connected:
        return
    raw = json.dumps({"type": "presence", "pages": presence_counts()})
    stale = []
    for peer in connected:
        try:
            await peer.send(raw)
        except websockets.exceptions.ConnectionClosed:
            stale.append(peer)
    for peer in stale:
        connected.pop(peer, None)
        connected_pages.pop(peer, None)


async def handler(websocket):
    global last_state_sync

    query = parse_qs(urlparse(websocket.request.path).query)
    token = (query.get("token") or [""])[0]
    role = role_for_token(token)

    if not role:
        await websocket.close(code=4401, reason="invalid or missing token")
        return

    connected[websocket] = role
    connected_pages[websocket] = (query.get("page") or ["unknown"])[0]
    print(f"[connect] role={role} total_connected={len(connected)}")
    await broadcast_presence()
    try:
        if last_state_sync is not None:
            await websocket.send(last_state_sync)

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

            stale = []
            for peer in connected:
                if peer is websocket:
                    continue
                if target_pages is not None and connected_pages.get(peer) not in target_pages:
                    continue
                try:
                    await peer.send(raw)
                except websockets.exceptions.ConnectionClosed:
                    stale.append(peer)
            for peer in stale:
                connected.pop(peer, None)
                connected_pages.pop(peer, None)
    finally:
        connected.pop(websocket, None)
        connected_pages.pop(websocket, None)
        print(f"[disconnect] role={role} total_connected={len(connected)}")
        await broadcast_presence()


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
    async with websockets.serve(handler, "0.0.0.0", PORT, max_size=16 * 1024 * 1024):
        print(f"Relay listening on 0.0.0.0:{PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
