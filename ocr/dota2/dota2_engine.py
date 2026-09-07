"""
Dota 2 post-match scoreboard reader.

Lives in ocr/dota2/, its own folder with its own config/state -- a
different game from moba/ (Mobile Legends, despite the shared "MOBA"
project name), valorant/ and freefire/, each isolated the same way for
the same reason: nothing here should be able to step on another game's
calibration or state.

SCOPE, deliberately narrow for now: the post-match Overview screen only
(kills/deaths/assists, gold, team score, game duration). No live in-game
capture, no IGN OCR -- player identity is assigned by the operator
afterwards, same "pick who this is" pattern Free Fire's pre-match roster
already uses, because an in-game name carries clan tags and symbols
("Verlin Raed [ZGØ]") that OCR reads unreliably and a human reads
correctly at a glance. This file only answers "what did row N show",
never "whose row N is".

The digit pipeline (preprocess/ocr_number/KDA regex) is carried over
verbatim from valorant_engine.py, not reinvented: that file's own history
records a specific, confirmed-against-real-captures fix for loop-shaped
digits (0/8 collapsing under naive thresholding) -- CUBIC upscale of the
grayscale crop BEFORE OTSU, border added AFTER. Same shapes of digit,
same fix applies here.
"""

import asyncio
import json
import re
from pathlib import Path

import cv2
import mss
import numpy as np
import pytesseract

CONFIG_PATH = Path(__file__).parent / "dota2_config.json"

TEAM_SLOTS = 5


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    tpath = cfg.get("tesseract_path")
    if tpath:
        pytesseract.pytesseract.tesseract_cmd = tpath
    return cfg


def crop_to_bgr(sct, region):
    shot = sct.grab({
        "left": region["x"], "top": region["y"],
        "width": region["w"], "height": region["h"],
    })
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


# ---------------------------------------------------------------------------
# Digit OCR -- carried over from valorant_engine.py's preprocess(), see the
# file-level note above for why this exact pipeline and not a simpler one.
# ---------------------------------------------------------------------------

def preprocess(img_bgr, upscale=4):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.medianBlur(gray, 3)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if thresh.mean() < 127:
        thresh = cv2.bitwise_not(thresh)
    thresh = cv2.copyMakeBorder(thresh, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    return thresh


TESS_CONFIG_NUMBER = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789 "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
TESS_CONFIG_DURATION = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789: "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
# Gold and K/D/A share one calibrated box (they sit stacked in the same
# small column on the real scoreboard), so this reads BOTH lines in one
# pass -- PSM 6 (uniform block of text), not the single-line PSM 7 the
# other fields use, and the whitelist covers both a comma-grouped gold
# figure ("6,260") and slash-separated KDA ("3/8/17").
TESS_CONFIG_PLAYER = (
    "--oem 1 --psm 6 "
    "-c tessedit_char_whitelist=0123456789/, "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)

KDA_TRIPLE_REGEX = re.compile(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
GOLD_REGEX = re.compile(r"\d[\d,]{2,}")
DURATION_REGEX = re.compile(r"(\d{1,3}):(\d{2})")


def ocr_number(img_bgr):
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_NUMBER).strip()
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def ocr_duration(img_bgr):
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_DURATION).strip()
    m = DURATION_REGEX.search(text)
    if not m:
        return None
    minutes, seconds = int(m.group(1)), int(m.group(2))
    return {"minutes": minutes, "seconds": seconds, "totalSeconds": minutes * 60 + seconds,
            "raw": text}


def ocr_player_cell(img_bgr):
    """One player's combined gold + K/D/A crop -> {"gold", "kills",
    "deaths", "assists"}, any of which is None if that line didn't read.
    Kept separate rather than failing the whole cell on one miss -- a
    correct gold figure is still worth keeping even if KDA misreads, and
    vice versa."""
    processed = preprocess(img_bgr, upscale=3)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_PLAYER).strip()

    kda = KDA_TRIPLE_REGEX.search(text)
    kills, deaths, assists = (int(kda.group(1)), int(kda.group(2)), int(kda.group(3))) \
        if kda else (None, None, None)

    # The gold figure is whichever comma-grouped or 3+ digit run ISN'T
    # part of the KDA match, so a KDA line that happens to contain a
    # longer number doesn't get mistaken for gold.
    gold = None
    kda_span = kda.span() if kda else None
    for m in GOLD_REGEX.finditer(text):
        if kda_span and kda_span[0] <= m.start() < kda_span[1]:
            continue
        gold = int(m.group(0).replace(",", ""))
        break

    return {"gold": gold, "kills": kills, "deaths": deaths, "assists": assists, "raw": text}


# ---------------------------------------------------------------------------
# Whole-scoreboard capture.
# ---------------------------------------------------------------------------

def capture_postmatch(config=None):
    """Grabs and OCRs every calibrated region in one pass. Returns a dict
    shaped for the operator to then assign IGNs onto -- slot order is
    top-to-bottom, matching the scoreboard's own row order, same as the
    5 fixed player rows Free Fire's roster already assumes per squad."""
    config = config or load_config()
    regions = config.get("regions", {})

    def region_crop(sct, key):
        r = regions.get(key)
        if not r or r.get("w", 0) <= 0 or r.get("h", 0) <= 0:
            return None
        return crop_to_bgr(sct, r)

    result = {
        "team1": {"score": None, "players": [None] * TEAM_SLOTS},
        "team2": {"score": None, "players": [None] * TEAM_SLOTS},
        "duration": None,
    }

    with mss.mss() as sct:
        for team_key, team_num in (("team1", 1), ("team2", 2)):
            for i in range(TEAM_SLOTS):
                crop = region_crop(sct, f"dota2_{team_key}_p{i}")
                if crop is not None:
                    result[team_key]["players"][i] = ocr_player_cell(crop)

            score_crop = region_crop(sct, f"dota2_{team_key}_score")
            if score_crop is not None:
                result[team_key]["score"] = ocr_number(score_crop)

        duration_crop = region_crop(sct, "dota2_duration")
        if duration_crop is not None:
            result["duration"] = ocr_duration(duration_crop)

    return result


# ---------------------------------------------------------------------------
# Roster + a tiny local server. Deliberately minimal, not a copy of Free
# Fire's engine -- there is no live loop here to hang this off of, so it
# is its own small asyncio server: hold the pre-match roster (2 teams x 5
# names, entered once before the match the same way Free Fire's operator
# picks names rather than trusting OCR on them), persist it, and answer a
# "capture now" request by running capture_postmatch() and labelling each
# slot with whichever roster name occupies it -- same top-to-bottom slot
# order the calibration boxes were drawn in.
# ---------------------------------------------------------------------------

STATE_PATH = Path(__file__).parent / "dota2_state.json"

PORT = 8766  # separate from freefire (8765) / moba+valorant's port, its own process


def default_state():
    return {
        "roster": {
            "team1": {"name": "", "players": ["", "", "", "", ""]},
            "team2": {"name": "", "players": ["", "", "", "", ""]},
        },
        "lastCapture": None,
    }


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = default_state()
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return default_state()


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def label_with_roster(capture, roster):
    """Merges a capture_postmatch() result with the roster by slot order
    -- capture supplies the numbers, roster supplies who they belong to."""
    for team_key in ("team1", "team2"):
        team_roster = roster.get(team_key, {})
        names = team_roster.get("players", [""] * TEAM_SLOTS)
        capture[team_key]["teamName"] = team_roster.get("name", "")
        for i, player in enumerate(capture[team_key]["players"]):
            if player is not None:
                player["name"] = names[i] if i < len(names) else ""
    return capture


async def handle_client(websocket):
    server_state = load_state()
    await websocket.send(json.dumps({"type": "state", "data": server_state}))
    async for raw in websocket:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if payload.get("type") == "save_roster":
            server_state["roster"] = payload.get("roster", server_state["roster"])
            save_state(server_state)
            await websocket.send(json.dumps({"type": "state", "data": server_state}))

        elif payload.get("type") == "capture":
            cfg = load_config()
            if not cfg.get("regions"):
                await websocket.send(json.dumps({
                    "type": "capture_result", "error": "No regions calibrated yet -- run calibrate.py first.",
                }))
                continue
            data = capture_postmatch(cfg)
            data = label_with_roster(data, server_state["roster"])
            server_state["lastCapture"] = data
            save_state(server_state)
            await websocket.send(json.dumps({"type": "capture_result", "data": data}))


async def main():
    import websockets
    print(f"Dota 2 post-match server on ws://localhost:{PORT}")
    print("Open dota2_dashboard.html in a browser.")
    async with websockets.serve(handle_client, "localhost", PORT):
        await asyncio.Future()


if __name__ == "__main__":
    import sys
    if "--serve" in sys.argv:
        asyncio.run(main())
    else:
        # Quick manual check without the server: have the Dota 2
        # post-match Overview screen up, run `python dota2_engine.py`,
        # read the printed numbers against the real screen.
        cfg = load_config()
        if not cfg.get("regions"):
            print("No regions calibrated yet. Run calibrate.py first.")
        else:
            data = capture_postmatch(cfg)
            print(json.dumps(data, indent=2))
