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

            if msg.get("type") == "state_sync":
                last_state_sync = raw

            stale = []
            for peer in connected:
                if peer is websocket:
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
    async with websockets.serve(handler, "0.0.0.0", PORT):
        print(f"Relay listening on 0.0.0.0:{PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
