"""Archives what the relay sees, and says something when it stops seeing it.

Two jobs, one process, because both want the same thing: a read-only
viewer connection to the relay, on the same box as the relay.

**Archiving.** The relay keeps exactly one snapshot, in memory, so a
client joining mid-match isn't blank -- and that snapshot is gone the
moment relay.service restarts. Nothing anywhere keeps the shape of a
match over time. This writes every distinct state to disk, so a crash
can be recovered from and a finished event can be looked at afterwards
rather than only remembered.

**Watching.** Nobody finds out the engine has stopped until a graphic on
air stops changing, which is the worst possible moment and the worst
possible messenger. The relay knows the instant the connection drops.
This turns that into a Discord message.

Read-only throughout: it connects with the VIEWER token, which the relay
silently drops anything from. It cannot disturb a live show even if it
is broken.

Stdlib plus `websockets`, which relay.service already has in its venv --
no new dependency to install beside it.
"""

import asyncio
import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest

RELAY_URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765")
VIEWER_TOKEN = os.environ.get("VIEWER_TOKEN", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/home/ubuntu/relay-archive"))

# Keep this many days of archive. 93GB free and a day measured at a few
# MB, so this is generous on purpose -- an event is worth more than the
# disk it sits on.
ARCHIVE_KEEP_DAYS = int(os.environ.get("ARCHIVE_KEEP_DAYS", "60"))

# An engine that is connected but has gone quiet is as broken as one that
# has dropped, and looks identical on air. Long enough not to fire during
# a genuinely still lobby.
SILENCE_SECONDS = int(os.environ.get("SILENCE_SECONDS", "300"))

# Don't say the same thing twice in a row, and don't say anything twice
# within this many seconds -- an alert that repeats is an alert people
# learn to ignore.
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN", "300"))

_DATA_URI = re.compile(r"^data:[^;]+;base64,")


def strip_images(obj):
    """Replaces embedded base64 artwork with a note of its size.

    The live state is 238KB, of which 182KB is base64 logos and player
    photos that never change and are already on disk. Archiving those
    every time anything moves would be writing the same pictures
    thousands of times an event."""
    if isinstance(obj, dict):
        return {k: strip_images(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_images(v) for v in obj]
    if isinstance(obj, str) and _DATA_URI.match(obj):
        return f"<image {len(obj)} bytes, stripped>"
    return obj


def post_discord(title, body, colour):
    """Fire-and-forget, and never fatal.

    A monitor that can die while reporting a problem is worse than no
    monitor, because it is trusted."""
    if not DISCORD_WEBHOOK:
        print(f"[watchdog] (no webhook set) {title} -- {body}")
        return
    payload = json.dumps({
        "embeds": [{
            "title": title,
            "description": body,
            "color": colour,
            "footer": {"text": "relay watchdog"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }).encode()
    req = urlrequest.Request(
        DISCORD_WEBHOOK, data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=10) as r:
            r.read()
        print(f"[watchdog] alerted: {title}")
    except Exception as e:
        print(f"[watchdog] couldn't reach Discord: {e}")


class Alerter:
    """Says a thing once, not every time it is still true."""

    def __init__(self):
        self.last_key = None
        self.last_at = 0.0

    def fire(self, key, title, body, colour):
        now = time.time()
        if key == self.last_key and now - self.last_at < ALERT_COOLDOWN:
            return
        self.last_key = key
        self.last_at = now
        post_discord(title, body, colour)


class Archive:
    """One gzipped newline-delimited JSON file per day."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.day = None
        self.fh = None
        self.last_hash = None
        self.written = 0

    def _roll(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day == self.day and self.fh is not None:
            return
        if self.fh is not None:
            self.fh.close()
        self.day = day
        self.fh = gzip.open(self.dir / f"state-{day}.ndjson.gz", "at",
                            encoding="utf-8")
        self.prune()

    def prune(self):
        cutoff = time.time() - ARCHIVE_KEEP_DAYS * 86400
        for f in self.dir.glob("state-*.ndjson.gz"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    print(f"[watchdog] pruned {f.name}")
            except OSError:
                pass

    def write(self, state):
        """Only when something actually changed.

        The engine broadcasts on every poll it considers changed, which
        includes changes the archive does not care about. Hashing what is
        about to be written is the cheapest way to not write the same
        thing four times a second."""
        slim = strip_images(state)
        blob = json.dumps(slim, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        if digest == self.last_hash:
            return False
        self.last_hash = digest
        self._roll()
        self.fh.write(json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(), "state": slim},
            separators=(",", ":")) + "\n")
        self.fh.flush()
        self.written += 1
        return True

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


async def run():
    import websockets

    archive = Archive(ARCHIVE_DIR)
    alerter = Alerter()
    sep = "&" if "?" in RELAY_URL else "?"
    url = f"{RELAY_URL}{sep}token={VIEWER_TOKEN}&page=watchdog"

    engine_seen = False        # has an engine EVER been connected this run
    engine_up = False
    last_state_at = 0.0
    warned_silent = False

    while True:
        try:
            async with websockets.connect(url, open_timeout=15,
                                          ping_interval=20) as ws:
                print(f"[watchdog] attached to {RELAY_URL}")
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        # Quiet is normal. Check the clock and carry on.
                        if (engine_up and last_state_at
                                and time.time() - last_state_at > SILENCE_SECONDS
                                and not warned_silent):
                            warned_silent = True
                            mins = int((time.time() - last_state_at) / 60)
                            alerter.fire(
                                "silent", "Engine has gone quiet",
                                f"Still connected, but nothing has been pushed "
                                f"for {mins} minutes. On air this looks exactly "
                                f"like a frozen graphic.", 0xE8A13A)
                        continue

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    kind = msg.get("type")

                    if kind == "presence":
                        # roles is what the relay authenticated, so it is
                        # the honest answer to "is an engine attached".
                        ocr = (msg.get("roles") or {}).get("ocr", 0)
                        now_up = ocr > 0
                        if now_up and not engine_up:
                            if engine_seen:
                                alerter.fire(
                                    "up", "Engine reconnected",
                                    "It is pushing state to the relay again.",
                                    0x3BA55D)
                            engine_seen = True
                            warned_silent = False
                            print("[watchdog] engine connected")
                        elif engine_up and not now_up:
                            alerter.fire(
                                "down", "Engine disconnected",
                                "Nothing is pushing to the relay. Every "
                                "graphic on air is now frozen at its last "
                                "value.", 0xE03B3B)
                            print("[watchdog] engine GONE")
                        engine_up = now_up

                    elif kind == "state_sync":
                        last_state_at = time.time()
                        if warned_silent:
                            warned_silent = False
                            alerter.fire("resumed", "Engine is pushing again",
                                         "State updates have resumed.", 0x3BA55D)
                        data = msg.get("data")
                        if isinstance(data, dict) and archive.write(data):
                            if archive.written % 200 == 0:
                                print(f"[watchdog] archived "
                                      f"{archive.written} snapshots today")

        except Exception as e:
            # The relay restarting is ordinary -- deploy-webhook does it on
            # every push to main. Reconnect quietly; only a relay that
            # stays down is worth anyone's attention, and that shows up as
            # repeated failures here rather than as one.
            print(f"[watchdog] relay connection lost ({e}); retrying in 5s")
            engine_up = False
            await asyncio.sleep(5)


def main():
    if not VIEWER_TOKEN:
        raise SystemExit("VIEWER_TOKEN is not set -- the relay will refuse us.")
    print(f"[watchdog] archive -> {ARCHIVE_DIR}, keeping {ARCHIVE_KEEP_DAYS} days")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
