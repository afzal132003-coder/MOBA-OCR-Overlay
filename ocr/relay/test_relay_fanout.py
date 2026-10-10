"""One slow page must not hold up any other page on the relay.

Run it with no arguments:

    python ocr\\relay\\test_relay_fanout.py            # the relay in this folder
    python ocr\\relay\\test_relay_fanout.py OTHER.py   # any server.py, to compare

WHAT THIS GUARDS

On the live relay a lobby strip waited more than twelve seconds for a
tick while the round trip to the relay was 18ms. A subscriber there saw
gaps of exactly 5003ms -- the relay's per-peer send deadline -- with
bursts after each: the fan-out waited on every peer before reading the
engine's next message, so the slowest page anywhere on the relay, of any
game, paced every other page.

HOW

The relay's real handler() is run in-process against fake connections.
One of them is a page whose send() NEVER completes -- a backgrounded tab
the browser has throttled, as the relay experiences it. A real socket
cannot be used for this on Windows: overlapped writes and loopback
buffering absorb tens of megabytes without ever pushing back, so the
relay never blocks and the broken version passes. (An earlier socket
version of this test did exactly that, four times.) A send that never
returns is the fault itself, stated directly.
"""
import asyncio
import json
import os
import runpy
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "server.py")

checks = 0
failures = []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label,
                          ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


class FakeWS:
    """Enough of a websockets connection for the relay's handler."""

    def __init__(self, path, stuck=False):
        self.request = SimpleNamespace(path=path)
        self.stuck = stuck
        self.received = []                  # (time, parsed message)
        self.inbox = asyncio.Queue()

    async def send(self, raw):
        if self.stuck:
            await asyncio.Event().wait()    # never returns
        self.received.append((time.time(), json.loads(raw)))

    async def ping(self):
        """A browser answers at once: the pong is here."""
        f = asyncio.get_running_loop().create_future()
        f.set_result(0.0)
        return f

    async def close(self, code=None, reason=None):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


class SlowLink(FakeWS):
    """A page far away behind the proxy: every send is TAKEN at once (the
    proxy and the kernel buffer it), and arrives when a link of `rate`
    bytes a second gets to it -- and its pong with it, as a browser's
    does. `pongs=False` is a page that never answers (only the deadline
    gets it anything)."""

    def __init__(self, path, rate, pongs=True):
        super().__init__(path)
        self.rate, self.pongs, self.free_at = rate, pongs, 0.0

    async def send(self, raw):
        now = time.time()
        self.free_at = max(now, self.free_at) + len(raw) / self.rate
        self.received.append((self.free_at, json.loads(raw)))

    async def ping(self):
        loop = asyncio.get_running_loop()
        f = loop.create_future()
        if self.pongs:
            loop.call_later(max(0.0, self.free_at - time.time()),
                            lambda: f.done() or f.set_result(0.0))
        return f


async def slow_link(relay, pongs=True, rate=50_000):
    """The engine offers twice what the link carries for four seconds:
    how far behind is the page, and does it end on the latest state?"""
    handler = relay["handler"]
    engine = FakeWS("/?token=o-test")
    page = SlowLink("/?token=v-test&page=freefire_map&rostercache=1", rate=rate, pongs=pongs)
    tasks = [asyncio.create_task(handler(ws)) for ws in (page, engine)]
    await asyncio.sleep(0.05)
    sent, pad = {}, "x" * 20_000
    for n in range(20):
        sent[n] = time.time()
        await engine.inbox.put(json.dumps({"type": "state_sync", "data": {"seq": n, "pad": pad}}))
        await asyncio.sleep(0.2)
    end = time.time()
    await asyncio.sleep(3.0)
    states = [(t, m["data"]["seq"]) for t, m in page.received if m.get("type") == "state_sync"]
    for ws in (engine, page):
        await ws.inbox.put(None)
    for tk in tasks:
        tk.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # how stale the page's picture was when the engine stopped: the newest
    # state it had by then, against the newest that existed
    had = max([seq for t, seq in states if t <= end] or [-1])
    behind = (end - sent[had]) if had >= 0 else float("inf")
    last_at = max((t for t, seq in states if seq == 19), default=None)
    return behind, (last_at - sent[19]) if last_at else None, len(states)


async def run():
    os.environ.update(OCR_TOKEN="o-test", ADMIN_TOKEN="a-test",
                      VIEWER_TOKEN="v-test", RELAY_HEALTH_SECONDS="0.3")
    relay = runpy.run_path(SERVER)
    handler = relay["handler"]

    engine = FakeWS("/?token=o-test")
    stuck = FakeWS("/?token=v-test&page=dota2_dashboard", stuck=True)
    strip = FakeWS("/?token=v-test&page=freefire_lobby_status&rostercache=1")
    tasks = [asyncio.create_task(handler(ws)) for ws in (stuck, strip, engine)]
    await asyncio.sleep(0.05)

    sent = {}
    for n in range(10):
        stamp = time.time()
        await engine.inbox.put(json.dumps({
            "type": "state_sync", "data": {"roster": {"teams": []}, "seq": n}}))
        await engine.inbox.put(json.dumps({
            "type": "lobby_update", "n": n,
            "_target_pages": ["freefire_lobby_status"]}))
        sent[n] = stamp
        await asyncio.sleep(0.1)

    await asyncio.sleep(2.0)

    ticks = {m["n"]: t for t, m in strip.received if m.get("type") == "lobby_update"}
    late = [(t - sent[n]) * 1000 for n, t in ticks.items()]
    states = [m for _, m in strip.received if m.get("type") == "state_sync"]

    print("\nrelay under test: %s" % SERVER)
    print("ticks received by the strip: %d of 10" % len(ticks))
    if late:
        print("worst tick latency: %.0f ms" % max(late))

    check("every tick reaches the strip while another page is stuck",
          len(ticks) == 10, "%d of 10" % len(ticks))
    check("and promptly -- inside 100ms",
          bool(late) and max(late) < 100,
          "worst %.0f ms" % (max(late) if late else -1))
    check("the strip ends on the latest state, not a stale one",
          bool(states) and states[-1]["data"].get("seq") == 9,
          "last seq %s" % (states[-1]["data"].get("seq") if states else None))

    # The stuck page's backlog must stay bounded however long it is stuck:
    # superseded states are dropped, not queued up behind each other.
    box = (relay.get("outboxes") or {}).get(stuck)
    waiting = 0
    if box is not None:
        waiting = sum(1 for cell in box.items
                      if cell[0] is not None and '"state_sync"' in cell[0])
    check("a stuck page holds at most one waiting state, not ten",
          box is not None and waiting <= 1,
          ("%d waiting" % waiting) if box is not None else "no per-peer outbox at all")

    for ws in (engine, stuck, strip):
        await ws.inbox.put(None)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # A FAR PAGE ON A SLOW LINK (Bengaluru, 2026-10-11): behind the proxy
    # nothing pushes back, so the relay must wait for each state to land.
    behind, last, n = await slow_link(relay)
    print("slow link: %.2f s behind when the engine stopped, latest state landed after %s s, %d of 20 sent"
          % (behind, "%.2f" % last if last else "never", n))
    check("a page on a slow link stays within a second of the engine", behind < 1.0, "%.2f s behind" % behind)
    check("and ends on the latest state, promptly", last is not None and last < 1.0, str(last))
    check("it is sent what its link can carry, not everything", n < 20, "%d of 20" % n)
    behind, last, n = await slow_link(relay, pongs=False, rate=10_000_000)
    check("a page that never answers a ping is still sent the latest, by the deadline",
          last is not None and last < relay["DELIVERY_WAIT_SECONDS"] + 1.0, str(last))

    # THE HEALTH REPORT: the dashboard is told how each page is doing.
    handler = relay["handler"]
    admin = FakeWS("/?token=a-test&page=freefire_dashboard")
    page = SlowLink("/?token=v-test&page=freefire_ingame", rate=1_000_000)
    engine = FakeWS("/?token=o-test")
    tasks = [asyncio.create_task(handler(ws)) for ws in (admin, page, engine)]
    await asyncio.sleep(0.05)
    await engine.inbox.put(json.dumps({"type": "state_sync", "data": {"seq": 1}}))
    await asyncio.sleep(0.9)
    reports = [m for _, m in admin.received if m.get("type") == "relay_health"]
    row = next((r for r in (reports[-1]["peers"] if reports else []) if r["page"] == "freefire_ingame"), None)
    check("the dashboard is sent a health report naming each page", row is not None, str(reports[-1:]))
    check("with how long its latest state took to land", row is not None and row.get("lastMs") is not None
          and row.get("waitingMs") == 0, str(row))
    for ws in (admin, page, engine):
        await ws.inbox.put(None)
    for tk in tasks:
        tk.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main():
    asyncio.run(run())
    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
