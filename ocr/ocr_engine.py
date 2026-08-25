"""
MOBA Broadcast OCR Engine.

Captures small screen regions defined in config.json, reads the numbers
with Tesseract OCR, and relays live state to any connected browser
(overlay.html / dashboard.html) over a local WebSocket server.

Run calibrate.py first to set up the crop regions in config.json.
"""

import asyncio
import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import mss
import pytesseract
import websockets

CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path(__file__).parent / "state.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge_defaults(loaded, defaults):
    """Fill in any keys missing from a saved state with defaults, recursively.
    Lets the schema grow (new tabs/fields) without breaking an old state.json."""
    if not isinstance(loaded, dict) or not isinstance(defaults, dict):
        return loaded
    merged = dict(defaults)
    for key, value in loaded.items():
        if key in defaults:
            merged[key] = deep_merge_defaults(value, defaults[key])
        else:
            merged[key] = value
    return merged


config = load_config()

if config.get("tesseract_path"):
    pytesseract.pytesseract.tesseract_cmd = config["tesseract_path"]

REGION_ORDER = [
    "team1_kills", "team1_objectives", "team1_gold",
    "team2_gold", "team2_objectives", "team2_kills",
    "team1_series_score", "team2_series_score",
]

FIELD_MAP = {
    "team1_kills": ("team1", "kills"),
    "team1_objectives": ("team1", "objectives"),
    "team1_gold": ("team1", "gold"),
    "team2_gold": ("team2", "gold"),
    "team2_objectives": ("team2", "objectives"),
    "team2_kills": ("team2", "kills"),
    "team1_series_score": ("seriesScore", "team1"),
    "team2_series_score": ("seriesScore", "team2"),
}

NUMBER_REGEX = re.compile(r"\d+")
GOLD_REGEX = re.compile(r"(\d+(?:\.\d+)?)\s*([kK])?")

# Tesseract calls are blocking subprocess launches; running the 8 regions
# through a thread pool instead of one-after-another is what actually cuts
# the per-cycle latency down, since they overlap instead of stacking up.
ocr_executor = ThreadPoolExecutor(max_workers=len(REGION_ORDER))

connected_clients = set()


def default_player():
    # No player headshot field — Post Match shows the hero the player
    # picked in Prematch instead, not a player photo.
    return {"damageDealt": 0, "damageTaken": 0}


def empty_hero_slot():
    return {"name": "", "image": ""}


def default_state():
    return {
        # In-Game Live Ops (OCR-driven). "players" is the shared 5-name
        # roster — set once here, reused by Prematch pick labels and by
        # Post Match player rows so it's never entered twice.
        "team1": {"name": "TEAM 1", "logo": "", "kills": 0, "objectives": 0, "gold": 0,
                  "players": ["", "", "", "", ""]},
        "team2": {"name": "TEAM 2", "logo": "", "kills": 0, "objectives": 0, "gold": 0,
                  "players": ["", "", "", "", ""]},
        "seriesScore": {"team1": 0, "team2": 0},
        # Prematch (manual, pick/ban draft). Each slot is {name, image} —
        # "image" is the exact filename in dashboard/assets/heroes/, resolved
        # by the dashboard's hero picker, not guessed from the name.
        "prematch": {
            "context": {"line1": "LEAGUE STAGE", "line2": "DAY 1 MATCH 1"},
            "phase": "PICKING",
            # Which team is currently on the clock — "team1", "team2", or
            # None. Purely a display indicator, doesn't gate anything.
            "activeTeam": None,
            # Timer is either running (timerEndsAt set, counting down to
            # that timestamp) or stopped/paused (timerEndsAt is None and
            # timerRemainingMs holds the frozen value to resume/display).
            "timerEndsAt": None,
            "timerDuration": 30,
            "timerRemainingMs": 30000,
            "bans": {
                "team1": [empty_hero_slot() for _ in range(5)],
                "team2": [empty_hero_slot() for _ in range(5)],
            },
            "picks": {
                "team1": [empty_hero_slot() for _ in range(5)],
                "team2": [empty_hero_slot() for _ in range(5)],
            },
        },
        # Post Match (manual, recap graphic)
        "postMatch": {
            "context": {"line1": "POST MATCH STATS", "line2": "PLAYOFFS / DAY 01"},
            "duration": "",
            "date": "",
            "team1Score": 0,
            "team2Score": 0,
            "stats": {
                "team1": {"gold": 0, "structures": 0, "kills": 0, "turtles": 0},
                "team2": {"gold": 0, "structures": 0, "kills": 0, "turtles": 0},
            },
            "players": {
                "team1": [default_player() for _ in range(5)],
                "team2": [default_player() for _ in range(5)],
            },
        },
    }


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            return deep_merge_defaults(loaded, default_state())
        except (json.JSONDecodeError, OSError):
            pass
    return default_state()


def save_state():
    try:
        tmp_path = STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(server_state, f, indent=2)
        tmp_path.replace(STATE_PATH)
    except OSError:
        pass


server_state = load_state()
locked_fields = set()

# OCR debounce: require the same raw reading N times in a row before trusting it,
# since a single misread frame is common with live video noise/compression.
last_confirmed = {key: None for key in REGION_ORDER}
pending_value = {key: None for key in REGION_ORDER}
pending_count = {key: 0 for key in REGION_ORDER}


def preprocess(img_bgr, upscale=4):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # Calibrated boxes are often cropped right against the digits; a small
    # replicated border stops antialiased edge pixels from being read as
    # part of the digit (a common cause of misreads like 1 -> 7 or 0 -> 8).
    gray = cv2.copyMakeBorder(gray, 6, 6, 6, 6, cv2.BORDER_REPLICATE)
    gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    # Median blur (not Gaussian) removes video-compression speckle without
    # smearing digit edges, which matters a lot at this small a source size.
    gray = cv2.medianBlur(gray, 3)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if thresh.mean() < 127:
        thresh = cv2.bitwise_not(thresh)
    return thresh


TESS_CONFIG = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789.kK "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)


def ocr_number(img_bgr):
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG)
    return text.strip()


def parse_gold(text):
    match = GOLD_REGEX.search(text)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2):
        value *= 1000
    return int(round(value))


def parse_int(text):
    match = NUMBER_REGEX.search(text)
    if not match:
        return None
    return int(match.group())


def crop_to_bgr(sct, region):
    shot = sct.grab({
        "left": region["x"], "top": region["y"],
        "width": region["w"], "height": region["h"],
    })
    img = np.array(shot)
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def crop_to_data_url(img_bgr, scale=3):
    big = cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", big)
    if not ok:
        return None
    b64 = base64.b64encode(buf).decode("ascii")
    return f"data:image/png;base64,{b64}"


async def broadcast(message):
    if not connected_clients:
        return
    data = json.dumps(message)
    await asyncio.gather(*[c.send(data) for c in connected_clients], return_exceptions=True)


def confirm_reading(key, raw_value):
    if raw_value is None:
        return None
    if pending_value[key] == raw_value:
        pending_count[key] += 1
    else:
        pending_value[key] = raw_value
        pending_count[key] = 1
    if pending_count[key] >= config.get("debounce_frames", 2) and last_confirmed[key] != raw_value:
        last_confirmed[key] = raw_value
        return raw_value
    return None


def apply_ocr_value(key, value):
    team, field = FIELD_MAP[key]
    if f"{team}.{field}" in locked_fields:
        return False
    if server_state[team][field] == value:
        return False
    server_state[team][field] = value
    return True


def read_region(key, img_bgr):
    text = ocr_number(img_bgr)
    if key.endswith("_gold"):
        return parse_gold(text)
    return parse_int(text)


async def ocr_loop():
    interval = config.get("poll_interval_seconds", 0.35)
    regions = config["regions"]
    loop = asyncio.get_running_loop()

    with mss.mss() as sct:
        frame_counter = 0
        while True:
            # Screen grabs are cheap (a few ms) and mss isn't thread-safe, so
            # these stay sequential in the main thread.
            crops = {}
            for key in REGION_ORDER:
                region = regions.get(key)
                if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                    continue
                crops[key] = crop_to_bgr(sct, region)

            # The slow part is the Tesseract subprocess call itself. Running
            # all regions through the thread pool at once means the total
            # wait per cycle is roughly one OCR call, not eight stacked up.
            keys = list(crops.keys())
            results = await asyncio.gather(*[
                loop.run_in_executor(ocr_executor, read_region, key, crops[key])
                for key in keys
            ])

            changed = False
            for key, parsed in zip(keys, results):
                confirmed = confirm_reading(key, parsed)
                if confirmed is not None and apply_ocr_value(key, confirmed):
                    changed = True

            # Push crop previews every couple of cycles so the dashboard can
            # show exactly what OCR is looking at, without flooding the socket.
            if frame_counter % 2 == 0:
                for key, img_bgr in crops.items():
                    data_url = crop_to_data_url(img_bgr)
                    if data_url:
                        await broadcast({"type": "crop_preview", "region": key, "image": data_url})

            if changed:
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            frame_counter += 1
            await asyncio.sleep(interval)


async def handle_client(websocket, path=None):
    connected_clients.add(websocket)
    await websocket.send(json.dumps({
        "type": "state_sync", "data": server_state, "locked": list(locked_fields),
    }))
    try:
        async for message in websocket:
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue

            if payload.get("type") == "manual_update":
                data = payload.get("data", {})
                for team in ("team1", "team2"):
                    if team in data:
                        server_state[team].update(data[team])
                if "seriesScore" in data:
                    server_state["seriesScore"].update(data["seriesScore"])
                # Prematch/Post Match are manual-only (no OCR writer to race
                # against), so the dashboard just sends its full current
                # section each time and it fully replaces the old one.
                if "prematch" in data:
                    server_state["prematch"] = data["prematch"]
                if "postMatch" in data:
                    server_state["postMatch"] = data["postMatch"]
                for field in payload.get("lock", []):
                    locked_fields.add(field)
                for field in payload.get("unlock", []):
                    locked_fields.discard(field)
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
    finally:
        connected_clients.discard(websocket)


async def main():
    host = config.get("server_host", "localhost")
    port = config.get("server_port", 8765)
    async with websockets.serve(handle_client, host, port):
        print(f"OCR relay server running at ws://{host}:{port}")
        print("Open overlay/overlay.html in OBS as a Browser Source, and")
        print("dashboard/dashboard.html in a normal browser tab.")
        await ocr_loop()


if __name__ == "__main__":
    asyncio.run(main())
