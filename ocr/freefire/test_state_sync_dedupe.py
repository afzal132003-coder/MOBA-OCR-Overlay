"""A state a client already has is not sent again, and a change that lands
inside the relay's gap still reaches the relay.

Run: python ocr/freefire/test_state_sync_dedupe.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as E

E.save_state = lambda *a, **k: None
checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


class Fake:
    def __init__(self, name):
        self.name = name
        self.got = []

    async def send(self, data):
        self.got.append(json.loads(data))

    def states(self):
        return [m for m in self.got if m.get("type") == "state_sync"]


async def main():
    page, relay = Fake("page"), Fake("relay")
    E.connected_clients.clear()
    E.connected_clients.update({page, relay})
    E._roster_cache_clients.update({page, relay})
    E.relay_websocket = relay
    E._last_relay_sync_at = 0.0
    E.server_state.setdefault("liveOps", {})["probe"] = 1

    # The first state carries the roster and the next is the slim form:
    # both are new to a client. Counting starts after them.
    await E._broadcast_state_sync()
    await E._broadcast_state_sync()
    await asyncio.sleep(E.FREEFIRE_RELAY_SYNC_GAP + 0.2)
    p0, r0 = len(page.states()), len(relay.states())
    check("primed: roster form then slim form, to both", p0 == 2 and r0 == 2, "%d %d" % (p0, r0))

    print("the same state again and again")
    for _ in range(5):
        await E._broadcast_state_sync()
    await asyncio.sleep(E.FREEFIRE_RELAY_SYNC_GAP + 0.2)
    check("a page is sent nothing", len(page.states()) == p0, str(len(page.states())))
    check("nor is the relay", len(relay.states()) == r0, str(len(relay.states())))

    print("a change")
    E.server_state["liveOps"]["probe"] = 2
    await E._broadcast_state_sync()
    await asyncio.sleep(0.05)
    check("reaches the page at once", len(page.states()) == p0 + 1
          and page.states()[-1]["data"]["liveOps"]["probe"] == 2)
    check("and the relay, its gap being up", len(relay.states()) == r0 + 1, str(len(relay.states())))
    E.server_state["liveOps"]["probe"] = 3
    await E._broadcast_state_sync()
    await asyncio.sleep(0.05)
    check("a second change inside the gap waits", len(relay.states()) == r0 + 1, str(len(relay.states())))
    await asyncio.sleep(E.FREEFIRE_RELAY_SYNC_GAP + 0.2)
    check("and the relay is caught up when the gap is up, with the latest",
          len(relay.states()) == r0 + 2 and relay.states()[-1]["data"]["liveOps"]["probe"] == 3,
          str([m["data"]["liveOps"].get("probe") for m in relay.states()]))
    check("the catch-up gives the page nothing it had", len(page.states()) == p0 + 2, str(len(page.states())))

    print("a handler's own state_sync")
    await E.broadcast({"type": "state_sync", "data": E.server_state, "locked": list(E.locked_fields)})
    await asyncio.sleep(0.05)
    n_page = len(page.states())
    await E._broadcast_state_sync()
    await E._broadcast_state_sync()
    await asyncio.sleep(0.05)
    check("after it, the slim form goes once, not again and again",
          len(page.states()) == n_page + 1, "%d -> %d" % (n_page, len(page.states())))

    print("a client that leaves and comes back")
    E._drop_outbox(page)
    page2 = Fake("page2")
    E.connected_clients.discard(page)
    E.connected_clients.add(page2)
    await E._broadcast_state_sync()
    await asyncio.sleep(0.05)
    check("a new connection is sent the state", len(page2.states()) == 1)

    print("\n%d checks, %d failed" % (checks, len(failures)))
    sys.exit(1 if failures else 0)


asyncio.run(main())
