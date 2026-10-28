"""The event fonts the engine sends the relay (see FONT_DIR in server.py):
stored where the pages ask for them, never passed on to a page, only from
the engine's role, and only font files by name into that one folder.

Run it with no arguments:

    python ocr\\relay\\test_relay_fonts.py
"""
import asyncio
import base64
import json
import os
import runpy
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


class FakeWS:
    """Enough of a websockets connection for the relay's handler."""

    def __init__(self, path):
        self.request = SimpleNamespace(path=path)
        self.received = []
        self.inbox = asyncio.Queue()

    async def send(self, raw):
        self.received.append(json.loads(raw))

    async def ping(self):
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


def b64(data):
    return base64.b64encode(data).decode()


def fonts_message(*files):
    return json.dumps({"type": "font_files", "files": [{"name": n, "data": d} for n, d in files]})


async def through_handler(relay, folder):
    """A font message from the engine and one from an admin dashboard, a
    page listening: what is stored, and what reaches the page."""
    # The handler's own globals: run_path hands back a COPY of the module's,
    # so setting FONT_DIR on that one left the handler writing into the
    # real folder -- over the real font.
    handler = relay["handler"]
    handler.__globals__["FONT_DIR"] = folder
    engine, admin = FakeWS("/?token=o-test"), FakeWS("/?token=a-test")
    page = FakeWS("/?token=v-test&page=freefire_alive_status")
    tasks = [asyncio.create_task(handler(ws)) for ws in (page, engine, admin)]
    await asyncio.sleep(0.05)
    await engine.inbox.put(fonts_message(("WinnerSans-Medium.otf", b64(b"OTTO-medium"))))
    await admin.inbox.put(fonts_message(("Sneaky.otf", b64(b"OTTO-admin"))))
    await engine.inbox.put(json.dumps({"type": "state_sync", "data": {"n": 1}}))
    await asyncio.sleep(0.3)
    for ws in (engine, admin, page):
        await ws.inbox.put(None)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return page.received


def main():
    os.environ.update(OCR_TOKEN="o-test", ADMIN_TOKEN="a-test", VIEWER_TOKEN="v-test")
    relay = runpy.run_path(os.path.join(HERE, "server.py"))
    # Never the real folder, whatever happens below.
    relay["handler"].__globals__["FONT_DIR"] = os.path.join(tempfile.gettempdir(), "relay-fonts-test-unused")
    store = relay["store_fonts"]

    print("\nstore_fonts")
    with tempfile.TemporaryDirectory() as tmp:
        folder = os.path.join(tmp, "winner-sans")
        got = store([{"name": "WinnerSans-Bold.otf", "data": b64(b"OTTO-bold")},
                     {"name": "Winner-Sans-Cond-Regular.ttf", "data": b64(b"\x00\x01\x00\x00ttf")}], folder)
        check("font files are written where the pages look", got == ["WinnerSans-Bold.otf", "Winner-Sans-Cond-Regular.ttf"]
              and open(os.path.join(folder, "WinnerSans-Bold.otf"), "rb").read() == b"OTTO-bold", str(got))
        check("an unchanged file is left alone",
              store([{"name": "WinnerSans-Bold.otf", "data": b64(b"OTTO-bold")}], folder) == [])
        check("a changed one is written again",
              store([{"name": "WinnerSans-Bold.otf", "data": b64(b"OTTO-bold-2")}], folder) == ["WinnerSans-Bold.otf"])
        bad = [{"name": n, "data": b64(b"x")} for n in
               ("../escape.otf", "..\\escape.otf", "sub/dir.otf", "/abs.otf", "page.html", "script.js",
                "font.otf.html", ".hidden.otf", "", "a" * 90 + ".otf")]
        check("anything that is not a font file by name goes nowhere", store(bad, folder) == [])
        check("nothing escaped the folder", not os.path.exists(os.path.join(tmp, "escape.otf"))
              and sorted(os.listdir(folder)) == ["Winner-Sans-Cond-Regular.ttf", "WinnerSans-Bold.otf"],
              str(sorted(os.listdir(folder))))
        check("not base64, empty, or over the cap: skipped",
              store([{"name": "a.otf", "data": "not base64!!"}, {"name": "b.otf", "data": ""},
                     {"name": "c.otf", "data": b64(b"x" * (relay["FONT_MAX_BYTES"] + 1))},
                     {"name": "d.otf", "data": 5}, "not a dict"], folder) == [])

    print("\nthrough the handler")
    with tempfile.TemporaryDirectory() as tmp:
        folder = os.path.join(tmp, "winner-sans")
        received = asyncio.run(through_handler(relay, folder))
        names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        check("the engine's font is stored", names == ["WinnerSans-Medium.otf"], str(names))
        check("an admin's is not", "Sneaky.otf" not in names)
        check("no page is sent a font message", not any(m.get("type") == "font_files" for m in received),
              str([m.get("type") for m in received]))
        check("and the page still gets the state after it",
              any(m.get("type") == "state_sync" and (m.get("data") or {}).get("n") == 1 for m in received),
              str([m.get("type") for m in received]))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
