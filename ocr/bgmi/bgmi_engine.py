"""BGMI engine -- capture the observer panel, serve it to the dashboard.

Its own process on its own port (8767), the same isolation freefire
(8765) and dota2 (8766) already use, so running this never disturbs a
Free Fire event and the two can sit side by side on one PC.

ON LOAD, because the last thing this dashboard needed was more of it.
Reading one frame costs about 40ms, so even a continuous poll is a
fraction of a core -- but the PREVIEW is a JPEG of the whole panel, and
pushing one of those down the socket on every poll is the part that
actually hurts. So previews are on demand only, one capture per button
press, exactly like the Free Fire alive/elim previews. Nothing is
captured at all while nobody is looking unless continuous polling is
explicitly switched on.

NOTHING IS PUBLISHED THAT CANNOT BE JUSTIFIED. A read reaches the state
only when the slot numbers could be fitted to a consecutive run and no
digit template is missing. Both are reported to the dashboard as the
reason when a read is refused, so a blank table always says why it is
blank.
"""

import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgmi_panel as bp

CONFIG_PATH = Path(__file__).parent / "bgmi_config.json"
STATE_PATH = Path(__file__).parent / "bgmi_state.json"
PORT = 8767

CONNECTED = set()


def default_config():
    return {
        "monitor": 1,
        # The panel's rectangle on screen. BlueStacks draws window chrome
        # around the Android framebuffer, so this is NOT the window --
        # calibrate it to the game picture itself or every offset in
        # bgmi_panel.py is wrong by the width of a title bar.
        "region": {"x": 0, "y": 0, "w": 1920, "h": 1080},
        "relay": {"enabled": False, "url": "", "token": ""},
        "settings": {
            # Off by default. See the note at the top about load.
            "continuousPoll": False,
            "pollIntervalSeconds": 0.5,
            "lobbySize": 16,
        },
    }


def load_config():
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


config = load_config()
templates = bp.load_templates()

server_state = {
    "bgmi": {
        "rows": [],
        "header": {"remaining": None, "teamsAlive": None},
        "diagnostics": {},
        "readAt": 0,
        "blockedBy": "",
    }
}


# ----------------------------------------------------------------- capture

def grab_region():
    """One screenshot of the configured rectangle, as RGB."""
    import mss
    r = config.get("region") or {}
    box = {"left": int(r.get("x", 0)), "top": int(r.get("y", 0)),
           "width": int(r.get("w", 1920)), "height": int(r.get("h", 1080))}
    with mss.mss() as sct:
        shot = sct.grab(box)
    return np.asarray(Image.frombytes("RGB", shot.size, shot.rgb))


def read_frame(rgb):
    """Everything bgmi_panel can tell us about one frame.

    Returns (rows, header, diagnostics, blocked_reason). `rows` is empty
    whenever blocked_reason is set -- a refusal never half-publishes.
    """
    luma = bp.to_luma(rgb)
    page = bp.read_page(rgb, digit_templates=templates.get("kill"))
    cards = page["cards"]
    diag = {
        "cards": len(cards),
        "cardRows": page["card_rows"],
        "scroll": round(page["scroll"], 1),
        "scrollScore": round(page["scroll_score"], 1),
        "firstSlot": None,
        "slotMargin": 0.0,
        "missingDigits": bp.missing_digits(templates.get("slot", {})),
    }

    if not cards:
        return [], {}, diag, ("No team cards found. Is the panel open, and is "
                              "the capture region on the game picture rather "
                              "than the BlueStacks window?")

    if diag["missingDigits"]:
        return [], {}, diag, (
            "Digit %s has never been seen, so the slot numbers cannot be "
            "trusted -- most starting positions become unjudgeable. Capture a "
            "frame scrolled so a slot containing it is on screen, then run "
            "learn_glyphs.py on it." % ", ".join(diag["missingDigits"]))

    first, margin = bp.fit_slot_run(cards, templates.get("slot", {}))
    diag["firstSlot"], diag["slotMargin"] = first, round(margin, 3)
    if first is None:
        return [], {}, diag, ("The slot numbers did not fit any consecutive "
                              "run clearly (margin %.3f). Mid-scroll, or the "
                              "panel is partly covered." % margin)

    bp.assign_slots(cards, first)
    rows = bp.team_rows(cards)
    remaining, teams_alive = bp.read_header(luma, templates.get("slot", {}))
    header = {"remaining": remaining, "teamsAlive": teams_alive}

    # The checksum, when the page happens to hold the whole lobby. A
    # partial page legitimately totals less than the header, so this is
    # reported rather than enforced -- enforcing it on a 9-of-16 view
    # would reject every good frame.
    lobby = int((config.get("settings") or {}).get("lobbySize") or 0)
    header["complete"] = bool(lobby and len(rows) >= lobby)
    header["agrees"] = bp.agrees_with_header(
        rows, remaining, teams_alive, header["complete"])
    return rows, header, diag, ""


# ----------------------------------------------------------------- preview

def annotate(rgb, cards, rows):
    """Draw what the reader saw onto the frame.

    The point of a preview is to be disagreed with. A picture of the panel
    tells the operator nothing they cannot see in the game; a picture with
    the reader's own boxes and verdicts on it lets them spot a row read as
    dead that plainly is not, which is the whole reason to look.
    """
    im = Image.fromarray(rgb).convert("RGB")
    d = ImageDraw.Draw(im)
    by_index = {c["index"]: c for c in cards}
    for i, row in enumerate(rows):
        c = by_index.get(i)
        if not c:
            continue
        cx, top = bp.CARD_X[c["column"]], c["top"]
        d.rectangle([cx, top, cx + 520, top + bp.CARD_H],
                    outline=(90, 200, 255), width=2)
        d.text((cx + 6, max(0, top - 16)),
               "slot %s  %d/4 alive  %s kills"
               % (row["slot"], row["alive"],
                  "--" if row["kills"] is None else row["kills"]),
               fill=(255, 220, 120))
        for p in range(bp.PLAYERS_PER_CARD):
            y = bp.row_y(top, p)
            alive = c["players"][p]["alive"]
            d.ellipse([cx + 64, y + 8, cx + 74, y + 18],
                      fill=(110, 230, 160) if alive else (230, 90, 90))
    return im


def to_data_url(im, max_width=1100, quality=62):
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ------------------------------------------------------------------ serving

async def broadcast(msg):
    if not CONNECTED:
        return
    payload = json.dumps(msg)
    for ws in list(CONNECTED):
        try:
            await ws.send(payload)
        except Exception:
            pass


def do_read(with_preview):
    rgb = grab_region()
    rows, header, diag, blocked = read_frame(rgb)
    server_state["bgmi"] = {
        "rows": rows,
        "header": header,
        "diagnostics": diag,
        "readAt": time.time(),
        "blockedBy": blocked,
    }
    preview = ""
    if with_preview:
        page = bp.read_page(rgb, digit_templates=templates.get("kill"))
        cards = page["cards"]
        if cards and not blocked:
            bp.assign_slots(cards, diag.get("firstSlot") or 1)
        preview = to_data_url(annotate(rgb, cards, rows) if cards
                              else Image.fromarray(rgb))
    return preview


async def handle_client(websocket, path=None):
    CONNECTED.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "state_sync", "data": server_state}))
        async for raw in websocket:
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            kind = payload.get("type")

            if kind == "bgmi_read_now":
                loop = asyncio.get_running_loop()
                preview = await loop.run_in_executor(
                    None, do_read, bool(payload.get("preview", True)))
                await websocket.send(json.dumps({
                    "type": "bgmi_read_result",
                    "preview": preview,
                    "bgmi": server_state["bgmi"],
                }))
                await broadcast({"type": "state_sync", "data": server_state})

            elif kind == "bgmi_set_settings":
                (config.setdefault("settings", {})
                 ).update(payload.get("settings") or {})
                save_config(config)
                await broadcast({"type": "state_sync", "data": server_state})

            elif kind == "bgmi_set_region":
                region = payload.get("region") or {}
                if all(k in region for k in ("x", "y", "w", "h")):
                    config["region"] = {k: int(region[k]) for k in ("x", "y", "w", "h")}
                    save_config(config)
                await websocket.send(json.dumps({
                    "type": "bgmi_region", "region": config["region"]}))

            elif kind == "bgmi_reload_glyphs":
                global templates
                templates = bp.load_templates()
                await websocket.send(json.dumps({
                    "type": "bgmi_glyphs",
                    "slot": sorted(templates.get("slot", {})),
                    "kill": sorted(templates.get("kill", {})),
                    "missing": bp.missing_digits(templates.get("slot", {})),
                }))
    except Exception:
        pass
    finally:
        CONNECTED.discard(websocket)


async def poll_loop():
    """Continuous reading, off unless asked for. No preview rides along --
    that is the expensive half and nobody is necessarily watching."""
    while True:
        settings = config.get("settings") or {}
        if settings.get("continuousPoll"):
            try:
                await asyncio.get_running_loop().run_in_executor(None, do_read, False)
                await broadcast({"type": "state_sync", "data": server_state})
            except Exception as e:
                print("poll failed: %s" % e)
            await asyncio.sleep(float(settings.get("pollIntervalSeconds") or 0.5))
        else:
            await asyncio.sleep(0.5)


async def relay_client_loop():
    import websockets
    relay = config.get("relay") or {}
    if not relay.get("enabled"):
        return
    url, token = relay.get("url", ""), relay.get("token", "")
    if not token:
        token = os.environ.get("RELAY_TOKEN", "").strip()
    if not token:
        try:
            token = (Path(__file__).resolve().parent.parent / ".relay_token"
                     ).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if not url or not token:
        print("Relay enabled but url/token not both set -- skipping relay.")
        return
    sep = "&" if "?" in url else "?"
    connect_url = "%s%stoken=%s&page=bgmi_engine" % (url, sep, token)
    while True:
        try:
            async with websockets.connect(connect_url) as ws:
                print("Connected to cloud relay at %s" % url)
                await handle_client(ws)
        except Exception as e:
            print("Relay connection lost/failed (%s); retrying in 3s..." % e)
        await asyncio.sleep(3)


async def main():
    import websockets
    print("BGMI engine on ws://localhost:%d" % PORT)
    missing = bp.missing_digits(templates.get("slot", {}))
    if missing:
        print("Digit %s unseen -- slot numbers cannot be fitted until it is "
              "learned (see learn_glyphs.py)." % ", ".join(missing))
    async with websockets.serve(handle_client, "localhost", PORT):
        await asyncio.gather(relay_client_loop(), poll_loop(), asyncio.Future())


if __name__ == "__main__":
    if "--serve" in sys.argv:
        asyncio.run(main())
    else:
        # Without --serve: one read against the current screen, printed.
        # Have the panel open before running it.
        rows, header, diag, blocked = read_frame(grab_region())
        print(json.dumps({"rows": rows, "header": header,
                          "diagnostics": diag, "blockedBy": blocked}, indent=2))
