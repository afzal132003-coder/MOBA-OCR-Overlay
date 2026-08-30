"""
Valorant Broadcast OCR Engine.

Standalone from ocr_engine.py (MOBA) and freefire_engine.py on purpose --
same reasoning as Free Fire's own engine: a Valorant broadcast day has no
use for MOBA's kills/gold/turtle/lord pipeline or Free Fire's killfeed/
loadout capture, so sharing one engine process across three completely
different games would just be dead weight and cross-wiring risk. Own
config (valorant_config.json), own state file (valorant_state.json), own
WebSocket server -- same default port (8765) as the other two engines,
since only one of the three ever runs at a time (same reason all three
share a port: overlay/dashboard pages don't need different URLs depending
on which engine happens to be up).

Scope for this first build: team/player setup + Prematch (IGN, logo,
player photos), Live Ops (round score OCR -- two simple digit regions,
same pipeline MOBA's kills/gold already use -- plus an operator-triggered
Plant/Defuse popup, since there's no reference yet for what Valorant's
on-screen plant/defuse indicator looks like to calibrate OCR against), and
Post Match (scoreboard entered per-team/per-player in the dashboard, MVP
push, Head-to-Head push). The in-game scoreboard is individually sorted by
ACS with teams told apart only by row background color (green/red) --
reading that automatically needs real calibration against the actual
game, so it starts as manual dashboard entry; automating it is a planned
follow-up once that's been looked at directly.
"""

import asyncio
import base64
import difflib
import json
import re
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
import mss
import pytesseract
import websockets

CONFIG_PATH = Path(__file__).parent / "valorant_config.json"
STATE_PATH = Path(__file__).parent / "valorant_state.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge_defaults(loaded, defaults):
    """Fill in any keys missing from a saved state with defaults,
    recursively. Lets the schema grow without breaking an old
    valorant_state.json."""
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

TEAM1_SCORE_KEY = "valorant_team1_score"
TEAM2_SCORE_KEY = "valorant_team2_score"
REGION_ORDER = [TEAM1_SCORE_KEY, TEAM2_SCORE_KEY]

# The top-center announcement zone -- confirmed against a real clip (not
# assumed): "SPIKE PLANTED" is genuine on-screen text, same toast-style
# banner as MOBA's turtle/lord announcements. Reads generously (whole
# banner, not just the words) same reasoning as turtle_announcement's own
# calibration hint -- the banner can shift slightly.
ANNOUNCEMENT_REGION_KEY = "valorant_announcement"
# "defused" only, not "defus\w*" -- Valorant shows a "next DEFUSING"
# progress bar WHILE a defuse is in progress (confirmed on a real clip),
# which must NOT fire this early; only the completed-defuse banner should,
# and that reads "DEFUSED" (past tense), not "DEFUSING".
SPIKE_PLANTED_REGEX = re.compile(r"spike\W*planted", re.IGNORECASE)
SPIKE_DEFUSED_REGEX = re.compile(r"spike\W*defused", re.IGNORECASE)
PLANT_DEFUSE_DISPLAY_SECONDS = 6

connected_clients = set()
connected_pages = {}
ocr_executor = ThreadPoolExecutor(max_workers=len(REGION_ORDER) + 2)


def default_player_stat_row():
    return {
        "agent": "", "acs": 0, "kills": 0, "deaths": 0, "assists": 0,
        "econ": 0, "firstBloods": 0, "plants": 0, "defuses": 0,
    }


def default_state():
    return {
        "team1": {"name": "", "logo": "", "players": ["", "", "", "", ""]},
        "team2": {"name": "", "logo": "", "players": ["", "", "", "", ""]},
        "seriesScore": {"team1": 0, "team2": 0},

        "prematch": {
            "context": {"line1": "", "line2": ""},
        },

        # OCR-fed once the two score regions are calibrated -- same
        # digit-crop pipeline as MOBA's kills/gold. Round score (or map
        # score), not series score (see seriesScore above for that).
        "liveScore": {"team1": 0, "team2": 0},

        # The spike can only be planted by the attacking side and defused
        # by the defending side, and those swap at halftime -- a real game
        # mechanic, not something to guess at, so this is an explicit
        # operator toggle (flip it once at halftime) rather than inferred.
        # Drives which team plantDefuse.team gets set to below.
        "attackingTeam": "team1",

        # OCR-detected from ANNOUNCEMENT_REGION_KEY (SPIKE_PLANTED_REGEX /
        # SPIKE_DEFUSED_REGEX), same toast-detection pattern as MOBA's
        # turtle/lord popups -- "team" is derived (plant -> attackingTeam,
        # defuse -> the other team), not read from the OCR text itself.
        # Manual show/hide (plant_defuse_show/hide) still works as an
        # operator override/fallback, same relationship turtle's manual
        # countdown has with its own OCR detection. Auto-hides after
        # PLANT_DEFUSE_DISPLAY_SECONDS.
        "plantDefuse": {
            "status": "idle", "shownUntil": None,
            "team": None, "type": None,  # type: "plant" | "defuse"
        },

        "postMatch": {
            "duration": "", "date": "",
            "result": {"team1": "victory", "team2": "defeat"},
            # Always roster-ordered (team1.players[i] <-> this players[i]),
            # NOT the on-screen mixed/ACS-sorted order -- see module
            # docstring on why matching a scoreboard row to a roster
            # player/team is a manual step for now.
            "players": {
                "team1": [default_player_stat_row() for _ in range(5)],
                "team2": [default_player_stat_row() for _ in range(5)],
            },
        },

        # Team+player selection; stats pulled from postMatch.players at
        # push time, single-sourced same as the MOBA MVP graphic.
        "mvp": {"team": None, "playerIndex": None},

        # One player per side, compared against each other -- independent
        # of MVP's own selection.
        "headToHead": {
            "team1": {"playerIndex": None},
            "team2": {"playerIndex": None},
        },

        "graphicOverrides": {},
    }


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            state = deep_merge_defaults(loaded, default_state())
            # A mid-display plant/defuse popup doesn't mean anything across
            # a process restart -- always come back up idle, same reasoning
            # as MOBA's turtleTimer/lordTimer reset on load.
            state["plantDefuse"] = default_state()["plantDefuse"]
            return state
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

DEBOUNCE_WINDOW = max(2, config.get("debounce_frames", 7))
last_confirmed = {key: None for key in REGION_ORDER}
reading_window = {key: deque(maxlen=DEBOUNCE_WINDOW) for key in REGION_ORDER}


def confirm_reading(key, raw_value):
    """Rolling-window majority vote, same logic (and same reasoning) as
    ocr_engine.py's version -- a window tolerates the occasional bad frame
    mixed in among mostly-good ones instead of a single misread frame
    resetting a consecutive-match streak back to zero."""
    if raw_value is None:
        return None
    window = reading_window[key]
    window.append(raw_value)
    if len(window) < window.maxlen:
        return None
    value, count = Counter(window).most_common(1)[0]
    if count * 2 <= len(window):
        return None
    if last_confirmed[key] == value:
        return None
    last_confirmed[key] = value
    return value


def preprocess(img_bgr, upscale=4):
    """Same digit-tuned pipeline as ocr_engine.py's preprocess() -- see
    that file for the reasoning behind each step (border padding, upscale,
    median blur, OTSU threshold, auto-invert)."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.copyMakeBorder(gray, 10, 10, 10, 10, cv2.BORDER_REPLICATE)
    gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.medianBlur(gray, 3)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if thresh.mean() < 127:
        thresh = cv2.bitwise_not(thresh)
    return thresh


TESS_CONFIG = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789 "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
NUMBER_REGEX = re.compile(r"\d+")


def ocr_number(img_bgr):
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG)
    return text.strip()


def parse_int(text):
    match = NUMBER_REGEX.search(text)
    if not match:
        return None
    return int(match.group())


def read_region(key, img_bgr):
    return parse_int(ocr_number(img_bgr))


MAX_OCR_DIMENSION = 1920


def _image_to_data(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    scale = 1.0
    longest = max(h, w)
    if longest > MAX_OCR_DIMENSION:
        scale = MAX_OCR_DIMENSION / longest
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    data = pytesseract.image_to_data(gray, config="--oem 1 --psm 11", output_type=pytesseract.Output.DICT)
    if scale != 1.0:
        inv = 1.0 / scale
        for key in ("left", "top", "width", "height"):
            data[key] = [int(round(v * inv)) for v in data[key]]
    return data


def ocr_lines(img_bgr):
    data = _image_to_data(img_bgr)
    lines = {}
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(text)
    return [" ".join(words) for words in lines.values()]


def ocr_text(img_bgr):
    """Reads whatever text is in the crop (used for the plant/defuse
    announcement banner). Deliberately NOT using preprocess()/the digit
    pipeline above -- same reasoning as MOBA's turtle toast: that pipeline
    is tuned for tiny plain digits on a flat background and wipes out
    stylized game-announcement text entirely."""
    return " ".join(ocr_lines(img_bgr)).strip()


# ---------------------------------------------------------------------------
# Post-match scoreboard screenshot import. Unlike MOBA's Battle Report/Gold
# screens (two fixed 5-player columns, one per team, so a match to "team1"
# vs "team2" is just "which half of the screen"), Valorant's own post-match
# "Individually Sorted" screen is ONE list of all 10 players mixed together
# and sorted by ACS, with team told apart only by row background color --
# not something worth building real color-detection CV for blind. Instead,
# same trick as MOBA's own upload/extract fallback: fuzzy-match each OCR'd
# name against the FULL roster (both teams combined), which tells us team
# and roster index directly from the name match itself, regardless of where
# on screen or in what order the row appears. The dashboard shows every
# result for manual review/correction before anything is applied.
# ---------------------------------------------------------------------------

STAT_NUMBER_REGEX = re.compile(r"^\d{1,3}(?:,\d{3})*$|^\d+$")
# The K/D/A column can OCR as one slash-joined token ("18/3/5") or as three
# separate word tokens depending on how tight Tesseract's word boxes land on
# a given screenshot -- handled as a single case here so callers don't need
# to guess which form they'll get.
KDA_TRIPLE_REGEX = re.compile(r"^(\d+)/(\d+)/(\d+)$")


def decode_image_data_url(data_url):
    header, _, b64data = data_url.partition(",")
    raw = base64.b64decode(b64data)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def normalize_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def fuzzy_match_name(candidate_text, roster_names):
    """roster_names: [(team, index, name), ...]. Returns (team, index, name,
    score) for the best match, or None if nothing clears the threshold. Same
    matching logic as ocr_engine.py's fuzzy_match_name()."""
    norm_candidate = normalize_name(candidate_text)
    if len(norm_candidate) < 2:
        return None
    best = None
    for team, idx, name in roster_names:
        norm_name = normalize_name(name)
        if not norm_name:
            continue
        if norm_name in norm_candidate or norm_candidate in norm_name:
            shorter, longer = sorted([norm_name, norm_candidate], key=len)
            score = 0.9 + 0.1 * (len(shorter) / len(longer))
        else:
            score = difflib.SequenceMatcher(None, norm_candidate, norm_name).ratio()
        if best is None or score > best[3]:
            best = (team, idx, name, score)
    return best if best and best[3] >= 0.45 else None


def build_roster_names():
    names = []
    for team in ("team1", "team2"):
        players = server_state.get(team, {}).get("players", ["", "", "", "", ""])
        for idx, name in enumerate(players):
            if name:
                names.append((team, idx, name))
    return names


def _words_and_lines_from_data(data):
    """Same word/line split as ocr_engine.py's version -- word-level tokens
    (for numbers, which render as separate tokens even on the same visual
    row) plus Tesseract's own line-grouped text (for names, which can span
    a couple of words)."""
    words = []
    lines = {}
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < 25:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append({"text": text, "x": x, "y": y, "w": w, "h": h, "cx": x + w / 2, "cy": y + h / 2})
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        entry = lines.setdefault(key, {"words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
        entry["words"].append(text)
        entry["x0"] = min(entry["x0"], x)
        entry["y0"] = min(entry["y0"], y)
        entry["x1"] = max(entry["x1"], x + w)
        entry["y1"] = max(entry["y1"], y + h)
    line_list = []
    for entry in lines.values():
        line_list.append({
            "text": " ".join(entry["words"]),
            "x": entry["x0"], "y": entry["y0"],
            "w": entry["x1"] - entry["x0"], "h": entry["y1"] - entry["y0"],
            "cx": (entry["x0"] + entry["x1"]) / 2, "cy": (entry["y0"] + entry["y1"]) / 2,
        })
    return words, line_list


def extract_postmatch_scoreboard(img_bgr, roster_names):
    """Returns one row per roster slot Tesseract's name-matching found (not
    necessarily all 10) with a rowY for sorting top-to-bottom -- the
    dashboard fills any gaps in manually. Column order left to right on the
    real screen is ACS, K/D/A, ECON, First Bloods, Plants, Defuses, so among
    the number-shaped tokens found to the right of a matched name, the first
    plain number is ACS, a slash-triple (however it tokenized) is K/D/A, and
    the remaining plain numbers fill Econ/First Bloods/Plants/Defuses in
    that order."""
    number_words, lines = _words_and_lines_from_data(_image_to_data(img_bgr))
    img_h = img_bgr.shape[0]

    best_by_slot = {}
    for line in lines:
        match = fuzzy_match_name(line["text"], roster_names)
        if not match:
            continue
        team, idx, name, score = match
        slot = (team, idx)
        if slot not in best_by_slot or score > best_by_slot[slot][1]:
            best_by_slot[slot] = (line, score, name)

    rows = []
    for (team, idx), (line, score, name) in best_by_slot.items():
        row_cy = line["cy"]
        row_window = max(line["h"], img_h * 0.02) * 2.2
        same_row = [
            w for w in number_words
            if abs(w["cy"] - row_cy) <= row_window and w["cx"] > line["cx"]
        ]
        same_row.sort(key=lambda w: w["x"])

        kills = deaths = assists = None
        plain_numbers = []
        for w in same_row:
            triple = KDA_TRIPLE_REGEX.match(w["text"])
            if triple:
                kills, deaths, assists = int(triple.group(1)), int(triple.group(2)), int(triple.group(3))
            elif STAT_NUMBER_REGEX.match(w["text"]):
                plain_numbers.append(int(w["text"].replace(",", "")))

        acs = plain_numbers[0] if len(plain_numbers) > 0 else None
        rest = plain_numbers[1:]
        econ = rest[0] if len(rest) > 0 else None
        first_bloods = rest[1] if len(rest) > 1 else None
        plants = rest[2] if len(rest) > 2 else None
        defuses = rest[3] if len(rest) > 3 else None

        rows.append({
            "team": team, "playerIndex": idx, "rosterName": name,
            "ocrName": line["text"], "matchScore": round(score, 2),
            "rowY": line["cy"],
            "acs": acs, "kills": kills, "deaths": deaths, "assists": assists,
            "econ": econ, "firstBloods": first_bloods, "plants": plants, "defuses": defuses,
        })
    rows.sort(key=lambda r: r["rowY"])
    return rows


CROP_PREVIEW_MAX_DIMENSION = 500


def crop_to_bgr(sct, region):
    shot = sct.grab({
        "left": region["x"], "top": region["y"],
        "width": region["w"], "height": region["h"],
    })
    img = np.array(shot)
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def crop_to_data_url(img_bgr, scale=3):
    h, w = img_bgr.shape[:2]
    longest = max(h, w)
    effective_scale = min(scale, CROP_PREVIEW_MAX_DIMENSION / longest)
    big = cv2.resize(img_bgr, None, fx=effective_scale, fy=effective_scale,
                      interpolation=cv2.INTER_AREA if effective_scale < 1 else cv2.INTER_NEAREST)
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


def presence_counts():
    counts = {}
    for page in connected_pages.values():
        counts[page] = counts.get(page, 0) + 1
    return counts


async def broadcast_presence():
    await broadcast({"type": "presence", "pages": presence_counts()})


def apply_live_score(key, value):
    team = "team1" if key == TEAM1_SCORE_KEY else "team2"
    if f"liveScore.{team}" in locked_fields:
        return False
    if server_state["liveScore"][team] == value:
        return False
    server_state["liveScore"][team] = value
    return True


def swap_team_sides():
    """Interchange everything currently tagged team1 <-> team2 -- same
    full-identity-swap approach as MOBA's swap_team_sides() (see
    ocr_engine.py), so overlay pages need no changes at all, they just
    keep reading team1/team2 as always, now holding the other side's
    data."""
    s = server_state
    s["team1"], s["team2"] = s["team2"], s["team1"]
    s["seriesScore"]["team1"], s["seriesScore"]["team2"] = (
        s["seriesScore"]["team2"], s["seriesScore"]["team1"],
    )
    s["liveScore"]["team1"], s["liveScore"]["team2"] = (
        s["liveScore"]["team2"], s["liveScore"]["team1"],
    )
    s["attackingTeam"] = "team2" if s.get("attackingTeam") == "team1" else "team1"

    pd = s.get("plantDefuse")
    if pd and pd.get("team") in ("team1", "team2"):
        pd["team"] = "team2" if pd["team"] == "team1" else "team1"

    pom = s.get("postMatch")
    if pom:
        pom["players"]["team1"], pom["players"]["team2"] = pom["players"]["team2"], pom["players"]["team1"]
        if "result" in pom:
            pom["result"]["team1"], pom["result"]["team2"] = (
                pom["result"]["team2"], pom["result"]["team1"],
            )

    mvp = s.get("mvp")
    if mvp and mvp.get("team") in ("team1", "team2"):
        mvp["team"] = "team2" if mvp["team"] == "team1" else "team1"

    h2h = s.get("headToHead")
    if h2h:
        h2h["team1"], h2h["team2"] = h2h["team2"], h2h["team1"]

    renamed = set()
    for f in locked_fields:
        if f.startswith("liveScore.team1"):
            renamed.add("liveScore.team2" + f[len("liveScore.team1"):])
        elif f.startswith("liveScore.team2"):
            renamed.add("liveScore.team1" + f[len("liveScore.team2"):])
        else:
            renamed.add(f)
    locked_fields.clear()
    locked_fields.update(renamed)


def process_plant_defuse_reading(text, now_ms):
    """Advances the plant/defuse popup, same state-machine shape as MOBA's
    process_turtle_reading -- runs every cycle regardless of whether
    there's a fresh OCR reading, since auto-hide is timed locally, not
    re-OCR'd. Only looks for a new trigger while idle, so a still-visible
    banner doesn't re-trigger every frame it stays on screen. Team is
    derived from event type + attackingTeam (a real game mechanic: only
    attackers plant, only defenders defuse), not read from the OCR text.
    Returns True if server_state changed."""
    pd = server_state["plantDefuse"]
    changed = False

    if pd["status"] == "shown" and pd["shownUntil"] is not None and now_ms >= pd["shownUntil"]:
        pd["status"] = "idle"
        pd["shownUntil"] = None
        changed = True

    if pd["status"] == "idle" and text:
        attacking = server_state.get("attackingTeam", "team1")
        defending = "team2" if attacking == "team1" else "team1"
        event_type = None
        team = None
        if SPIKE_DEFUSED_REGEX.search(text):
            event_type, team = "defuse", defending
        elif SPIKE_PLANTED_REGEX.search(text):
            event_type, team = "plant", attacking
        if event_type:
            pd["status"] = "shown"
            pd["shownUntil"] = now_ms + PLANT_DEFUSE_DISPLAY_SECONDS * 1000
            pd["team"] = team
            pd["type"] = event_type
            changed = True

    return changed


async def handle_client(websocket, path=None):
    connected_clients.add(websocket)
    try:
        query = parse_qs(urlparse(websocket.request.path).query)
        page = (query.get("page") or ["unknown"])[0]
    except Exception:
        page = "unknown"
    connected_pages[websocket] = page
    await broadcast_presence()
    await websocket.send(json.dumps({
        "type": "state_sync", "data": server_state, "locked": list(locked_fields),
    }))
    try:
        async for message in websocket:
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue

            msg_type = payload.get("type")

            if msg_type == "manual_update":
                data = payload.get("data", {})
                for team in ("team1", "team2"):
                    if team in data:
                        server_state[team].update(data[team])
                if "seriesScore" in data:
                    server_state["seriesScore"].update(data["seriesScore"])
                if "prematch" in data:
                    server_state["prematch"] = data["prematch"]
                if "liveScore" in data:
                    server_state["liveScore"].update(data["liveScore"])
                if "attackingTeam" in data:
                    server_state["attackingTeam"] = data["attackingTeam"]
                if "postMatch" in data:
                    server_state["postMatch"] = data["postMatch"]
                if "mvp" in data:
                    server_state["mvp"] = data["mvp"]
                if "headToHead" in data:
                    server_state["headToHead"] = data["headToHead"]
                if "graphicOverrides" in data:
                    server_state["graphicOverrides"] = data["graphicOverrides"]
                for field in payload.get("lock", []):
                    locked_fields.add(field)
                for field in payload.get("unlock", []):
                    locked_fields.discard(field)
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "plant_defuse_show":
                now_ms = int(time.time() * 1000)
                pd = server_state["plantDefuse"]
                pd["status"] = "shown"
                pd["shownUntil"] = now_ms + PLANT_DEFUSE_DISPLAY_SECONDS * 1000
                pd["team"] = payload.get("team")
                pd["type"] = payload.get("eventType")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "plant_defuse_hide":
                server_state["plantDefuse"]["status"] = "idle"
                server_state["plantDefuse"]["shownUntil"] = None
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "swap_sides":
                swap_team_sides()
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "extract_postmatch_scoreboard":
                data_url = payload.get("image", "")
                if data_url:
                    loop = asyncio.get_running_loop()
                    img = await loop.run_in_executor(ocr_executor, decode_image_data_url, data_url)
                    rows = await loop.run_in_executor(
                        ocr_executor, extract_postmatch_scoreboard, img, build_roster_names(),
                    )
                    await websocket.send(json.dumps({"type": "postmatch_scoreboard_extracted", "rows": rows}))
    finally:
        connected_clients.discard(websocket)
        connected_pages.pop(websocket, None)
        await broadcast_presence()


async def ocr_loop():
    interval = config.get("poll_interval_seconds", 0.15)
    loop = asyncio.get_running_loop()

    with mss.mss() as sct:
        frame_counter = 0
        while True:
            regions = config.get("regions", {})
            crops = {}
            for key in REGION_ORDER:
                region = regions.get(key)
                if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                    continue
                crops[key] = crop_to_bgr(sct, region)

            announcement_region = regions.get(ANNOUNCEMENT_REGION_KEY)
            announcement_crop = None
            if announcement_region and announcement_region.get("w", 0) > 0 and announcement_region.get("h", 0) > 0:
                announcement_crop = crop_to_bgr(sct, announcement_region)

            keys = list(crops.keys())
            ocr_tasks = [
                loop.run_in_executor(ocr_executor, read_region, key, crops[key])
                for key in keys
            ]
            if announcement_crop is not None:
                ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_text, announcement_crop))
            results = await asyncio.gather(*ocr_tasks)

            numeric_results = results[:len(keys)]
            announcement_raw_text = results[len(keys)] if announcement_crop is not None else None

            changed = False
            for key, parsed in zip(keys, numeric_results):
                confirmed = confirm_reading(key, parsed)
                if confirmed is None:
                    continue
                if apply_live_score(key, confirmed):
                    changed = True

            if process_plant_defuse_reading(announcement_raw_text, int(time.time() * 1000)):
                changed = True

            if frame_counter % 2 == 0:
                for key, img_bgr in crops.items():
                    data_url = crop_to_data_url(img_bgr)
                    if data_url:
                        await broadcast({"type": "crop_preview", "region": key, "image": data_url})
                if announcement_crop is not None:
                    data_url = crop_to_data_url(announcement_crop)
                    if data_url:
                        await broadcast({
                            "type": "crop_preview", "region": ANNOUNCEMENT_REGION_KEY,
                            "image": data_url, "text": announcement_raw_text or "",
                        })

            if changed:
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            frame_counter += 1
            await asyncio.sleep(interval)


async def relay_client_loop():
    """Same optional cloud-relay pattern as ocr_engine.py/freefire_engine.py
    -- no-op if relay isn't configured in valorant_config.json."""
    relay_cfg = config.get("relay", {})
    if not relay_cfg.get("enabled"):
        return
    url = relay_cfg.get("url", "")
    token = relay_cfg.get("token", "")
    if not url or not token:
        print("Relay is enabled in valorant_config.json but 'url'/'token' aren't both set -- skipping relay connection.")
        return
    separator = "&" if "?" in url else "?"
    connect_url = f"{url}{separator}token={token}"
    while True:
        try:
            async with websockets.connect(connect_url, max_size=16 * 1024 * 1024) as relay_ws:
                print(f"Connected to cloud relay at {url}")
                await handle_client(relay_ws)
        except Exception as e:
            print(f"Relay connection lost/failed ({e}); retrying in 3s...")
        await asyncio.sleep(3)


async def main():
    host = config.get("server_host", "localhost")
    port = config.get("server_port", 8765)
    async with websockets.serve(handle_client, host, port, max_size=16 * 1024 * 1024):
        print(f"Valorant OCR engine running at ws://{host}:{port}")
        print("Open dashboard.html's Valorant tab in a browser, and the")
        print("Valorant overlay pages in OBS as Browser Sources.")
        await asyncio.gather(ocr_loop(), relay_client_loop())


if __name__ == "__main__":
    asyncio.run(main())
