"""The engine must never wait on a client.

Run it with no arguments:

    python ocr\\freefire\\test_outbox.py

WHAT THIS GUARDS

The engine used to broadcast with asyncio.gather and a five-second
deadline per client, and its poll loop waited for the gather. One client
that stopped reading -- a dashboard tab the browser had put to sleep, or
the relay link while the relay was stalled -- held the whole engine for
up to five seconds per broadcast, and the deadline did not even free the
socket: a send cancelled while it waits to drain has already written its
frame, so the next one piled in behind it.

A client whose send() never returns is used here, because that is the
fault stated directly. (A real socket on Windows will not reproduce it --
overlapped writes absorb any amount without pushing back. See
ocr/relay/test_relay_fanout.py for that story.)
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FREEFIRE_NO_AUTOSTART", "1")
import freefire_engine as ff

checks = 0
failures = []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label,
                          ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


class FakeClient:
    def __init__(self, stuck=False):
        self.stuck = stuck
        self.received = []

    async def send(self, raw):
        if self.stuck:
            await asyncio.Event().wait()     # never returns
        self.received.append(json.loads(raw))


async def run():
    stuck, fast = FakeClient(stuck=True), FakeClient()
    ff.connected_clients.clear()
    ff.connected_clients.update({stuck, fast})
    ff.connected_pages[stuck] = "freefire_dashboard"
    ff.connected_pages[fast] = "freefire_lobby_status"
    ff.relay_websocket = None

    print("\na client that never reads")
    t = time.time()
    try:
        await asyncio.wait_for(ff.broadcast({
            "type": "state_sync", "data": dict(ff.server_state),
            "locked": []}), 0.5)
        returned = True
    except asyncio.TimeoutError:
        returned = False
    check("a broadcast returns at once, stuck client or not",
          returned, "%.0f ms" % ((time.time() - t) * 1000))

    await ff.push_lobby_update()
    await asyncio.sleep(0.05)
    types = [m.get("type") for m in fast.received]
    check("the other client gets the state", "state_sync" in types, str(types))
    check("and the lobby update", "lobby_update" in types, str(types))

    for n in range(10):
        await ff.broadcast({"type": "state_sync",
                            "data": {"seq": n}, "locked": []})
    await asyncio.sleep(0.05)
    box = ff._outboxes.get(stuck)
    waiting = sum(1 for cell in box.items
                  if cell[0] is not None and cell[0].startswith(ff._STATE_SYNC_PREFIX))
    check("a stuck client holds one waiting state, not ten",
          waiting == 1, "%d waiting" % waiting)
    last = [m for m in fast.received if m.get("type") == "state_sync"][-1]
    check("and the client that reads ends on the latest one",
          last["data"].get("seq") == 9, str(last["data"].get("seq")))

    print("\na superseded roster is never lost")
    slow = FakeClient(stuck=True)
    box = ff._outbox_for(slow)
    await asyncio.sleep(0)                 # its writer takes the first item
    first = json.dumps({"type": "state_sync", "data": {"seq": "in-flight"}})
    full = json.dumps({"type": "state_sync", "data": {"roster": "NEW", "seq": 1}})
    slim = json.dumps({"type": "state_sync", "data": {"seq": 2}})
    full2 = json.dumps({"type": "state_sync", "data": {"roster": "NEW", "seq": 2}})
    await ff._send_guarded(slow, first)
    await asyncio.sleep(0)
    await ff._send_guarded(slow, full, full=full)       # carries the roster
    await ff._send_guarded(slow, slim, full=full2)      # supersedes it, slim
    live = [cell[0] for cell in box.items if cell[0] is not None]
    check("the replacement goes out in its full form",
          live and json.loads(live[-1])["data"].get("roster") == "NEW"
          and json.loads(live[-1])["data"].get("seq") == 2,
          live[-1][:60] if live else "nothing waiting")

    print("\nthe opening snapshot")
    newcomer = FakeClient(stuck=True)
    nbox = ff._outbox_for(newcomer)
    await asyncio.sleep(0)
    await ff._send_guarded(newcomer, json.dumps({"type": "ping"}))
    await asyncio.sleep(0)
    opening = json.dumps({"type": "state_sync", "data": {"roster": "R", "seq": 0}})
    await ff._send_guarded(newcomer, opening, opening=True)
    await ff._send_guarded(newcomer, json.dumps({"type": "state_sync",
                                                 "data": {"seq": 1}}))
    live = [cell[0] for cell in nbox.items if cell[0] is not None]
    check("is never superseded by the next state",
          any('"roster": "R"' in x for x in live), str(len(live)) + " waiting")

    for c in (stuck, fast, slow, newcomer):
        ff._drop_outbox(c)
    ff.connected_clients.clear()


def main():
    asyncio.run(run())
    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
