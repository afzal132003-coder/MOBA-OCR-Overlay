"""
Free Fire Max Broadcast OCR Engine.

Standalone from ocr_engine.py (the MOBA engine) on purpose -- a Free Fire
broadcast day has zero use for MOBA's kills/gold/objectives HUD capture,
turtle/lord detection, or pick/ban draft state, so running that engine
just to get Free Fire's file ingest + live-ops OCR would be pure dead
weight. This has its own config (freefire_config.json), its own state
file (freefire_state.json), and its own WebSocket server -- same default
port (8765) as ocr_engine.py since the two are meant to be run one at a
time, never both, so overlay/dashboard pages don't need different URLs
depending on which engine is up.

Open dashboard/dashboard.html's FreeFire Max tab in a browser (it talks
to this engine over its own WebSocket connection, separate from the MOBA
one) and overlay/freefire_scoreboard.html + overlay/freefire_booyah.html
in OBS as Browser Sources.

Loadout capture (Num5) needs a global keyboard hook, which on Windows
generally requires this process to be running as Administrator to see
key presses while a game window has focus -- if Num5 isn't registering,
that's the first thing to check.
"""

import asyncio
import base64
import difflib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
import mss
import pytesseract
import websockets
import keyboard

CONFIG_PATH = Path(__file__).parent / "freefire_config.json"
STATE_PATH = Path(__file__).parent / "freefire_state.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge_defaults(loaded, defaults):
    """Fill in any keys missing from a saved state with defaults, recursively.
    Lets the schema grow without breaking an old freefire_state.json."""
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

# Raw text OCR only for both -- killfeed events and side-table rows both
# need real captured sample text before structured parsing can be written
# against them (same reasoning as the MOBA turtle toast originally).
FREEFIRE_KILLFEED_REGION_KEY = "freefire_killfeed"
FREEFIRE_SIDETABLE_REGION_KEY = "freefire_sidetable"
# Player HUD card, kept as the whole-card screenshot it always was -- the
# per-slot regions below supersede it for actual data, but a capture of
# the entire card is still worth having as the visual record an operator
# can eyeball when a slot match looks wrong.
FREEFIRE_LOADOUT_REGION_KEY = "freefire_loadout"

# Per-slot loadout calibration -- an explicit request to stop treating the
# card as one opaque screenshot and actually identify what's in it: which
# character, which three passives, which pet, which equipment, against
# which IGN. Each is its own calibrated box because they sit at fixed but
# unrelated spots on the card, and each icon slot gets matched against its
# own reference library (see match_icon) rather than OCR'd -- these are
# artwork, not text. The IGN slot is the one exception and is read as
# text.
FREEFIRE_LOADOUT_SLOT_KEYS = {
    "active": "freefire_loadout_active",
    "passive1": "freefire_loadout_passive1",
    "passive2": "freefire_loadout_passive2",
    "passive3": "freefire_loadout_passive3",
    "pet": "freefire_loadout_pet",
    "equipment": "freefire_loadout_equipment",
}
FREEFIRE_LOADOUT_IGN_KEY = "freefire_loadout_ign"

# Which reference-image folder each slot matches against. Characters cover
# both the active and the three passive slots -- in Free Fire those are the
# same pool of character portraits, just used in different roles, so one
# library serves all four rather than four duplicate copies.
FREEFIRE_ICON_LIBRARIES = {
    "active": "characters",
    "passive1": "characters",
    "passive2": "characters",
    "passive3": "characters",
    "pet": "pets",
    "equipment": "equipment",
}
# The operator's existing asset dump, exported straight from the game:
# flat files named by Free Fire's own numeric asset ID (101xxxxxx female
# characters, 102xxxxxx male). Characters therefore live in the ROOT of
# this folder, with pets/ and equipment/ as subfolders to be added later --
# that convention fits the dump as it already exists rather than making
# the operator reorganise 84 files to suit the code.
FREEFIRE_ASSETS_DIR = Path(__file__).parent.parent.parent / "overlay" / "assets" / "FFM"
# Optional {"101000005": "Kelly", ...} beside the images. Matching works
# without it (labels are just the numeric IDs), but a broadcast graphic
# needs a real name, and this lets those be filled in incrementally
# without renaming files or touching code.
FREEFIRE_ICON_NAMES_FILE = "names.json"

# Below this, a "best match" is reported but flagged low-confidence rather
# than trusted -- a colour signature always returns SOME nearest neighbour
# even when the real icon isn't in the library at all, and silently
# accepting that is how a wrong character ends up on a Booyah graphic.
#
# Measured against the real 84-portrait dump, degraded to what a captured
# HUD icon actually looks like (44px, 60% brightness, compression noise):
# every one of the 80 distinct portraits matched itself, and the only
# four that "failed" turned out to be byte-identical duplicate pairs in
# the dump (101000001/101000004, 101000018/101100018, 101888888/101999999,
# 102000019/102200019) -- all four scored 0.000 and were flagged rather
# than guessed at, which is the behaviour wanted for a genuinely
# ambiguous icon.
ICON_MATCH_MIN_CONFIDENCE = 0.55

# Transparent reference pixels get flattened onto this value -- roughly the
# dark card the game draws these icons over. See load_reference_image.
ICON_BACKDROP_VALUE = 30

NUM5_HOTKEY = "num 5"

HEADSHOT_HUNTER_DISPLAY_SECONDS = 6
TEAM_ELIMINATED_DISPLAY_SECONDS = 6

MAX_OCR_DIMENSION = 1920

connected_clients = set()
connected_pages = {}
ocr_executor = ThreadPoolExecutor(max_workers=2)


def default_state():
    return {
        "settings": {
            "matchResultFolder": "", "safezoneFolder": "",
            # Where the client writes its debugger-*.log files. Same folder
            # as the result files on a standard install; kept separate so a
            # setup that relocates one doesn't break the other.
            "debuggerFolder": "",
            # What happens to a result row that couldn't be resolved against
            # the roster (unknown team, or a UID/name that isn't registered).
            # "allow" keeps the row using whatever the file said; "flag"
            # keeps it but marks it for review; "drop" excludes it entirely.
            # An explicit request to control this rather than have the
            # engine decide: a scrim with guest teams wants "allow", while
            # a final where every roster entry is verified wants unknown
            # rows kept out of the standings rather than quietly scoring.
            "unmatchedPolicy": "flag",
        },
        "currentMatchId": None,
        "currentContext": "",
        "knownContexts": [],
        "currentSafezone": None,
        "matches": [],
        "standings": [],
        # Event-level context the operator sets once before the day starts:
        # the broadcast title ("FFM CLT WEEKLY SCRIMS - GRAND FINALS DAY 1"),
        # how many games the series runs, and which game is CURRENTLY being
        # played/captured. currentGame is what the dashboard's game dropdown
        # drives -- both the Num5 loadout capture and a committed match
        # result get tagged with it, so per-game data stays separated
        # instead of everything piling into one undifferentiated list.
        # maps[] is per-game, index 0 = game 1.
        "event": {
            "matchTitle": "",
            "roundName": "",
            "totalGames": 6,
            "currentGame": 1,
            "maps": [],
        },
        # Which view freefire_scoreboard.html shows -- "match" (latest
        # committed match's own results) or "overall" (cumulative
        # standings). freefire_booyah.html has no mode: it always shows
        # the latest committed match's rank-1 team.
        # pointsTableVisible drives freefire_points_table.html (the overall
        # standings graphic), pushed/pulled by the operator like every other
        # overlay rather than showing itself whenever data changes.
        "display": {"scoreboardMode": "match", "pointsTableVisible": False,
                    "scoreboardVisible": True},
        # Pre-match roster, uploaded once per event as a CSV (team, ign,
        # uid per row). Each player's "loadout" is manual-entry text
        # fields (active/passive x3/pet/equipment); "loadoutScreenshot" is
        # the Num5-captured HUD card image -- see loadoutCapture below.
        "roster": {"teams": []},
        # Raw OCR text only, refreshed every capture cycle once the
        # freefire_killfeed/freefire_sidetable regions are calibrated.
        # killfeedLastText/sidetableLastText stay as the raw OCR text (still
        # the honest record of what was actually read). sidetableRows is the
        # structured parse layered on top -- see parse_sidetable.
        "liveOps": {
            "killfeedLastText": "", "sidetableLastText": "",
            "sidetableRows": [], "sidetableUsedPalette": False,
            # Structured kill/knock feed read from the client's debugger
            # log -- real IGNs, not OCR. See read_debugger_events.
            "killEvents": [],
        },
        # Sequential Num5 loadout capture. "pointer" indexes into the
        # flattened roster (team-then-player order, see
        # flatten_roster_players()) -- each Num5 press while "active" is
        # true screenshots the calibrated freefire_loadout region into
        # whichever player is currently at that index, then advances it.
        # Deliberately NOT auto-armed on engine start -- the operator only
        # wants this live during the loadout-reveal window, not any time
        # they happen to bump the Num5 key.
        "loadoutCapture": {"active": False, "pointer": 0},
        # Manually-triggered side popups -- nothing in the game feed
        # reliably signals "this player just hit N headshots" or "this
        # team was just eliminated" for auto-detection (same reasoning as
        # MOBA's turtle/lord manual override), so these are operator-shown,
        # auto-hiding after a few seconds like the turtle/lord popups do.
        "headshotHunter": {
            "status": "idle", "shownUntil": None,
            "playerName": "", "headshots": 0, "photo": "",
        },
        "teamEliminated": {
            "status": "idle", "shownUntil": None,
            "teamName": "", "rank": None, "photo": "",
        },
    }


def roster_from_matches(matches):
    """Builds a roster from committed match results.

    Uses the file's ORIGINAL names (fileTeamName/fileName) in preference to
    the resolved ones: a roster is what results get matched against, so
    seeding it with names that were themselves the output of matching would
    be circular -- and with an empty roster nothing resolved anyway, so the
    resolved fields are just copies. Later matches fill in players missing
    from earlier ones (a squad that fielded a substitute), without
    disturbing anyone already recorded."""
    teams = {}
    for match in matches:
        for team in match.get("teams", []):
            name = (team.get("fileTeamName") or team.get("teamName") or "").strip()
            if not name:
                continue
            entry = teams.setdefault(name, {"name": name, "shortName": "", "logo": "", "players": []})
            known = {p["uid"] for p in entry["players"] if p.get("uid")}
            for player in team.get("players", []):
                uid = str(player.get("uid") or "").strip()
                ign = (player.get("fileName") or player.get("name") or "").strip()
                if not (uid or ign) or (uid and uid in known):
                    continue
                entry["players"].append({"ign": ign, "uid": uid})
                if uid:
                    known.add(uid)
    return list(teams.values())


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            state = deep_merge_defaults(loaded, default_state())
            # Bootstrap the roster on startup too, not only when a match is
            # committed. Hooking this to the commit path alone left an
            # already-committed match unable to seed anything: the state
            # came back with matches but an empty roster and stayed that
            # way until the operator happened to commit again. Same
            # empty-only guard, so a curated roster is never touched.
            if not (state.get("roster", {}).get("teams") or []):
                bootstrapped = roster_from_matches(state.get("matches", []))
                if bootstrapped:
                    state["roster"] = {"teams": bootstrapped}
                    print(f"Roster was empty on load -- bootstrapped "
                          f"{len(bootstrapped)} teams from committed results.")
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


# ---------------------------------------------------------------------------
# Low-level capture/OCR helpers (copied from ocr_engine.py's generic image
# pipeline -- these have no MOBA-specific coupling).
# ---------------------------------------------------------------------------

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


def _image_to_data(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    scale = 1.0
    longest = max(h, w)
    if longest > MAX_OCR_DIMENSION:
        scale = MAX_OCR_DIMENSION / longest
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    data = pytesseract.image_to_data(
        gray, config="--oem 1 --psm 11", output_type=pytesseract.Output.DICT,
    )
    if scale != 1.0:
        inv = 1.0 / scale
        for key in ("left", "top", "width", "height"):
            data[key] = [int(round(v * inv)) for v in data[key]]
    return data


def _words_and_lines_from_data(data):
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


def ocr_lines(img_bgr):
    _, lines = _words_and_lines_from_data(_image_to_data(img_bgr))
    return lines


def ocr_text(img_bgr):
    """Reads whatever text is in the crop, letting Tesseract's own layout
    analysis handle it (no hard black/white digit threshold -- that's
    tuned for tiny plain numbers, not full lines of game-UI text)."""
    return " ".join(line["text"] for line in ocr_lines(img_bgr)).strip()


# ---------------------------------------------------------------------------
# Live kill feed, read from the client's own debugger log.
#
# This supersedes OCR'ing the on-screen killfeed for anything structured.
# The client writes, to a plain text log it maintains itself:
#
#   Player Join, 6645536022, 117440522, SUMON::30,False
#   Player 117440553 Dead, killed by 67108888
#   Player '117440553' Knock Down, by '67108869'
#   @zwj PlayKnockDownGunTrace killer=67108869 victim=117440553 headshot=True
#
# The join lines are the key: they tie the runtime player id used by every
# combat line to the account UID and IGN, and those UIDs match the
# post-match result file exactly (verified against a real match). So a kill
# resolves to real names with no OCR and no guessing, which the on-screen
# killfeed can't offer -- that returned things like
# 'ARYAN "Y U ae YutaFckHard~' from the same match.
#
# Strictly read-only and append-aware: the file is open in the running
# game, so this only ever reads forward from the last offset it saw and
# never writes, truncates or locks anything.
# ---------------------------------------------------------------------------

DEBUGGER_JOIN_REGEX = re.compile(
    r"Player Join,\s*(?P<uid>\d+),\s*(?P<pid>\d+),\s*(?P<ign>.*?),\s*\w+\s*$"
)
DEBUGGER_KILL_REGEX = re.compile(
    r"Player\s+(?P<victim>\d+)\s+Dead,\s+killed by\s+(?P<killer>\d+)"
)
DEBUGGER_KNOCK_REGEX = re.compile(
    r"Player\s+'(?P<victim>\d+)'\s+Knock Down,\s*by\s*'(?P<killer>\d+)'"
)
DEBUGGER_HEADSHOT_REGEX = re.compile(
    r"PlayKnockDownGunTrace\s+killer=(?P<killer>\d+)\s+victim=(?P<victim>\d+)\s+headshot=(?P<hs>True|False)"
)
DEBUGGER_TS_REGEX = re.compile(r"^\[(?P<ts>[\d\-]+\s[\d:.]+)\]")

# Kept bounded: a full match produces hundreds of events and the whole lot
# rides along in every state_sync.
MAX_KILL_EVENTS = 60


def find_latest_debugger_log(folder):
    path = Path(folder) if folder else None
    if not path or not path.is_dir():
        return None
    logs = [f for f in path.glob("debugger-*.log") if f.is_file()]
    return max(logs, key=lambda f: f.stat().st_mtime) if logs else None


def read_debugger_events(log_path, offset, id_map):
    """Reads new lines since `offset`, updating id_map in place.

    Returns (events, new_offset). A partial trailing line is left for the
    next pass rather than parsed half-written -- the game is still
    appending to this file while we read it."""
    events = []
    try:
        size = log_path.stat().st_size
        # A smaller file than last time means the client rolled over to a
        # new log; start from the beginning rather than seeking past the end.
        if size < offset:
            offset = 0
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
    except OSError:
        return [], offset

    if "\n" in chunk:
        complete, _, remainder = chunk.rpartition("\n")
        new_offset -= len(remainder.encode("utf-8", errors="replace"))
    else:
        return [], offset

    headshots = {}
    for line in complete.splitlines():
        join = DEBUGGER_JOIN_REGEX.search(line)
        if join:
            id_map[join.group("pid")] = {
                "uid": join.group("uid"),
                "ign": join.group("ign").strip(),
            }
            continue

        hs = DEBUGGER_HEADSHOT_REGEX.search(line)
        if hs:
            headshots[(hs.group("killer"), hs.group("victim"))] = hs.group("hs") == "True"
            continue

        kill = DEBUGGER_KILL_REGEX.search(line)
        knock = None if kill else DEBUGGER_KNOCK_REGEX.search(line)
        match = kill or knock
        if not match:
            continue

        killer_id, victim_id = match.group("killer"), match.group("victim")
        killer = id_map.get(killer_id, {})
        victim = id_map.get(victim_id, {})
        ts = DEBUGGER_TS_REGEX.match(line.strip())
        events.append({
            "type": "kill" if kill else "knock",
            "time": ts.group("ts") if ts else "",
            "killerIgn": killer.get("ign", ""), "killerUid": killer.get("uid", ""),
            "victimIgn": victim.get("ign", ""), "victimUid": victim.get("uid", ""),
            # Unresolved ids are surfaced rather than hidden: a name that
            # silently reads as blank on a graphic is worse than one an
            # operator can see failed to resolve.
            "resolved": bool(killer.get("ign") and victim.get("ign")),
            "headshot": headshots.get((killer_id, victim_id), False),
        })
    return events, new_offset


# ---------------------------------------------------------------------------
# Pre-match lobby read.
#
# The lobby is the only place a roster exists BEFORE any game is played --
# the result files that otherwise supply team names, IGNs and UIDs are
# written after a match, so on day one there's nothing else to read.
#
# Only four squads are on screen at a time out of twelve, so this reads the
# four visible blocks and the operator scrolls and captures again. Results
# accumulate by team name rather than by position, since position means
# nothing once the list has scrolled.
#
# UIDs aren't shown in the default view at all -- the lobby has its own
# UID toggle that swaps the names for IDs. That makes a UID pass a separate
# capture the operator opts into, pairing by row order within a block.
# ---------------------------------------------------------------------------

LOBBY_BLOCK_KEYS = [f"freefire_lobby_block{i}" for i in range(1, 5)]
# Trailing rank/level tags ("MAX", "Lv.60") and the per-team score readout
# sit on the same lines as the names and would otherwise be captured as
# part of them.
LOBBY_NOISE_REGEX = re.compile(
    r"\b(MAX|LV\.?\s*\d+|SCORE\s*[:.]?\s*\d+)\b", re.IGNORECASE,
)
# Leading list numbers only ("1.", "2)"). Deliberately conservative: an
# earlier version also stripped leading v/V/> to catch the tick glyph beside
# a squad leader, and it silently ate the first letter of a real IGN
# ("Vyx.Aryav>01" -> "yx.Aryav>01"). A stray tick left on a name is
# something the operator can see and fix during review; a name quietly
# missing its first character looks correct and isn't.
LOBBY_LEAD_JUNK_REGEX = re.compile(r"^\s*\d{1,2}\s*[\.\)]\s*")


def clean_lobby_line(text):
    cleaned = LOBBY_NOISE_REGEX.sub(" ", text or "")
    cleaned = LOBBY_LEAD_JUNK_REGEX.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def parse_lobby_block(img_bgr):
    """One squad card -> {"teamName", "players": [ign, ...]}.

    The first legible line is the team name and the rest are players --
    positional rather than pattern-based, because a team name and an IGN
    are the same kind of string and nothing distinguishes them by content.
    Capped at five so a stray line picked up from the next card down
    can't inflate a squad."""
    lines = ocr_lines(img_bgr) if img_bgr is not None and img_bgr.size else []
    ordered = [clean_lobby_line(l["text"]) for l in sorted(lines, key=lambda l: l["cy"])]
    ordered = [t for t in ordered if t]
    if not ordered:
        return None
    return {"teamName": ordered[0], "players": ordered[1:6]}


def capture_lobby_blocks(regions_cfg, uid_pass=False):
    """Reads all four visible lobby blocks in one screen grab."""
    blocks = []
    with mss.mss() as sct:
        for key in LOBBY_BLOCK_KEYS:
            region = regions_cfg.get(key)
            if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                blocks.append(None)
                continue
            crop = crop_to_bgr(sct, region)
            parsed = parse_lobby_block(crop)
            if parsed:
                parsed["uidPass"] = uid_pass
            blocks.append(parsed)
    return blocks


# ---------------------------------------------------------------------------
# 12-team side table -- structured read.
#
# Each row is TEAM / ELIMS / ALIVE, where ALIVE is four coloured bars (one
# per squad member) rather than text. So the row is read in two different
# ways: OCR for the name and elim count, colour classification for the
# bars, because there is nothing for OCR to read in a coloured bar.
#
# Rows are located from where OCR actually found text rather than by
# slicing the region into 12 equal bands. The table re-sorts constantly
# during a match and doesn't always hold 12 rows (teams drop out), so a
# fixed slice would drift out of alignment the moment the row count or
# spacing changed. Anchoring to the detected text keeps the bar sampling
# tied to the row it belongs to.
#
# The bar colours are NOT hardcoded here. They're read from config
# ("sidetable_colors"), because the actual RGB values depend on the game's
# own palette and on the capture pipeline (OBS colour space, compression),
# and inventing thresholds without a real captured frame to measure would
# be a guess dressed up as a measurement. classify_bar falls back to a
# broad hue/brightness heuristic until real values are calibrated, and
# reports which method it used so the dashboard can show that it's running
# on the fallback rather than measured values.
# ---------------------------------------------------------------------------

SIDETABLE_PLAYERS_PER_TEAM = 4
# Fractions of the calibrated region's width where each column sits.
# Overridable per-setup in config, since a 12-team table's proportions can
# differ between resolutions and aspect ratios.
SIDETABLE_DEFAULT_COLUMNS = {"team": [0.0, 0.55], "elims": [0.55, 0.75], "alive": [0.75, 1.0]}

SIDETABLE_ROW_REGEX = re.compile(r"^(?P<name>.+?)\s+(?P<elims>\d+)\s*$")


def classify_bar(patch_bgr, palette=None):
    """One alive/knocked/eliminated bar -> a status string.

    palette, when configured, is {status: [B, G, R]} measured from a real
    frame; the nearest one wins. Without it this falls back to a coarse
    saturation/brightness rule, which is deliberately conservative: a dark
    bar is a dead slot, a bright saturated one is alive, and anything in
    between is reported as "unknown" rather than guessed, so an
    uncalibrated setup shows gaps instead of confidently wrong player
    counts."""
    mean = patch_bgr.reshape(-1, 3).mean(axis=0)
    if palette:
        best_status, best_distance = None, None
        for status, colour in palette.items():
            distance = float(np.linalg.norm(mean - np.array(colour, dtype=np.float32)))
            if best_distance is None or distance < best_distance:
                best_status, best_distance = status, distance
        return {"status": best_status, "method": "palette", "distance": round(best_distance, 1)}

    hsv = cv2.cvtColor(np.uint8([[mean]]), cv2.COLOR_BGR2HSV)[0][0]
    _, saturation, value = int(hsv[0]), int(hsv[1]), int(hsv[2])
    if value < 70:
        return {"status": "eliminated", "method": "fallback"}
    if saturation > 90 and value > 120:
        return {"status": "alive", "method": "fallback"}
    return {"status": "unknown", "method": "fallback"}


TESS_CONFIG_DIGITS = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789 "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)


def ocr_small_number(img_bgr, upscale=4):
    """Reads a small standalone number (an elim count).

    Needed because the general sparse-text pass used for the rest of the
    table demonstrably misses these: on a mock-up of the real layout it
    found the two-digit "10" but silently dropped every single-digit
    count, which is a whole column of zeros that look like real data. A
    digit-whitelisted single-line pass over an upscaled, thresholded crop
    reads them reliably -- same approach the Valorant engine needed for
    its own small-digit cells."""
    if img_bgr is None or img_bgr.size == 0:
        return None
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.medianBlur(gray, 3)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if thresh.mean() < 127:
        thresh = cv2.bitwise_not(thresh)
    thresh = cv2.copyMakeBorder(thresh, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    text = pytesseract.image_to_string(thresh, config=TESS_CONFIG_DIGITS).strip()
    match = re.search(r"\d{1,3}", text)
    return int(match.group()) if match else None


def find_bar_columns(alive_strip, expected=SIDETABLE_PLAYERS_PER_TEAM):
    """Learns the x-positions of the status-bar slots by pooling evidence
    across ALL rows at once.

    A per-row search can't work: an ELIMINATED bar is dark against a dark
    column and simply isn't findable on its own, so a squad that's been
    wiped yields no bars at all and reads as "no data" instead of "all
    dead" -- measured, that's exactly what happened (an all-eliminated row
    returned zero bars). But every row shares the same x layout, so bars
    that ARE visible somewhere in the table reveal where the slots sit for
    every row, including the rows where they're invisible.

    Returns x-ranges within the strip, or [] if nothing was detectable."""
    if alive_strip.size == 0:
        return []
    height, width = alive_strip.shape[:2]
    gray = cv2.cvtColor(alive_strip, cv2.COLOR_BGR2GRAY)
    # Column-wise "brightest thing anywhere in this column" -- a slot with
    # even one lit bar in the whole table shows up as a peak.
    profile = gray.max(axis=0).astype(np.float32)
    if profile.max() - profile.min() < 15:
        return []
    lit = profile > (profile.min() + (profile.max() - profile.min()) * 0.45)

    spans, start = [], None
    for x, on in enumerate(lit):
        if on and start is None:
            start = x
        elif not on and start is not None:
            spans.append((start, x - 1))
            start = None
    if start is not None:
        spans.append((start, len(lit) - 1))

    spans = [s for s in spans if (s[1] - s[0] + 1) >= max(2, width * 0.03)]
    if len(spans) > expected:
        # Keep the widest `expected` spans, then restore left-to-right order.
        spans = sorted(sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[:expected])
    return spans


def find_bar_rows(alive_strip, spans):
    """Row centre-lines, found from the bar column itself rather than from
    OCR -- so a row survives its team name failing to read.

    Restricted to the x-ranges the bars actually occupy (`spans`) so
    background texture between slots can't register as a row. A row that
    is entirely eliminated is dark and won't be found here; that's fine,
    since such a row is only lost if its name ALSO failed to read, and the
    two failures are independent."""
    if alive_strip.size == 0 or not spans:
        return []
    gray = cv2.cvtColor(alive_strip, cv2.COLOR_BGR2GRAY)
    columns = np.hstack([gray[:, s0:s1 + 1] for s0, s1 in spans])
    profile = columns.max(axis=1).astype(np.float32)
    if profile.max() - profile.min() < 15:
        return []
    lit = profile > (profile.min() + (profile.max() - profile.min()) * 0.45)

    centres, start = [], None
    for y, on in enumerate(lit):
        if on and start is None:
            start = y
        elif not on and start is not None:
            if (y - start) >= 2:
                centres.append((start + y - 1) / 2.0)
            start = None
    if start is not None and (len(lit) - start) >= 2:
        centres.append((start + len(lit) - 1) / 2.0)
    return centres


def find_status_bars(alive_strip):
    """Locates the actual status bars in the ALIVE column as bounding boxes.

    Deliberately finds the bars rather than assuming N evenly-spaced slots
    across the column. Measured against a mock-up of the real layout, the
    even-split assumption mis-sampled the outer bars entirely -- the bars
    don't span the full column width, so slot 4 landed on background and
    reported a live player as dead. Detecting the shapes removes the
    dependency on padding, bar width and inter-bar gaps, none of which the
    engine can know ahead of a real capture.

    Bars are found as blobs that stand out from the column's own dark
    background; a size filter drops specks and anything spanning most of
    the strip (a divider line or border rather than a bar)."""
    if alive_strip.size == 0:
        return []
    height, width = alive_strip.shape[:2]
    gray = cv2.cvtColor(alive_strip, cv2.COLOR_BGR2GRAY)
    # OTSU splits "bar" from "gap" without needing an absolute threshold,
    # which matters because an all-eliminated row is uniformly dark and an
    # all-alive row uniformly bright.
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if mask.mean() > 127:
        mask = cv2.bitwise_not(mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 2 or h < 2:
            continue
        if w > width * 0.6 or h > height * 0.6:
            continue
        boxes.append((x, y, w, h))
    return boxes


def group_into_rows(items, key_y, tolerance):
    """Clusters items into rows by vertical proximity. Used for both OCR
    words and detected bars so the two end up on a common row grid."""
    rows = []
    for item in sorted(items, key=key_y):
        y = key_y(item)
        placed = False
        for row in rows:
            if abs(row["y"] - y) <= tolerance:
                row["items"].append(item)
                row["y"] = (row["y"] * (len(row["items"]) - 1) + y) / len(row["items"])
                placed = True
                break
        if not placed:
            rows.append({"y": y, "items": [item]})
    return rows


def parse_sidetable(img_bgr, columns=None, palette=None):
    """Reads the side table into per-team rows.

    Returns {"rows": [...], "usedPalette": bool}. Each row carries the OCR
    text it came from so a mis-split is debuggable from the dashboard
    rather than only visible as a wrong number on air.

    Words are clustered into rows by vertical position rather than trusting
    Tesseract's own line grouping: in sparse-text mode it routinely splits
    a team's name and its elim count into separate "lines", which produced
    phantom rows ("RNTX" and "10" as two teams) and left every elim count
    at zero."""
    cols = columns or SIDETABLE_DEFAULT_COLUMNS
    height, width = img_bgr.shape[:2]
    if height < 10 or width < 10:
        return {"rows": [], "usedPalette": bool(palette)}

    def slice_x(name):
        lo, hi = cols.get(name, SIDETABLE_DEFAULT_COLUMNS[name])
        return max(0, int(lo * width)), min(width, int(hi * width))

    team_x0, _ = slice_x("team")
    elims_x0, elims_x1 = slice_x("elims")
    alive_x0, alive_x1 = slice_x("alive")

    # Team names only -- the elim count is read per-row afterwards with a
    # digit-tuned pass, because the sparse text pass that reads names
    # cannot be trusted with small standalone numbers (see ocr_small_number).
    name_strip = img_bgr[:, team_x0:elims_x0]
    words, _ = _words_and_lines_from_data(_image_to_data(name_strip)) if name_strip.size else ([], [])

    # Row tolerance scales with text height so it adapts to resolution
    # instead of being a pixel constant tuned to one capture size.
    typical_h = float(np.median([w["h"] for w in words])) if words else 12.0
    tolerance = max(4.0, typical_h * 0.6)

    word_rows = group_into_rows(words, lambda w: w["cy"], tolerance)
    alive_strip = img_bgr[:, alive_x0:alive_x1]
    bar_spans = find_bar_columns(alive_strip)

    # Rows are the UNION of "where text was found" and "where bars were
    # found", not just the former. A team whose name fails to OCR would
    # otherwise vanish entirely, taking its elim count and alive bars with
    # it -- measured: a legible 3-character team name was missed outright
    # by Tesseract (not even at low confidence), silently dropping that
    # whole squad from the table. Losing a name is recoverable; losing the
    # row is not, because nothing downstream can tell that a team is even
    # missing.
    row_centres = [wr["y"] for wr in word_rows]
    for bar_y in find_bar_rows(alive_strip, bar_spans):
        if all(abs(bar_y - existing) > tolerance for existing in row_centres):
            row_centres.append(bar_y)
    row_centres.sort()

    rows = []
    for centre_y in row_centres:
        word_row = min(word_rows, key=lambda wr: abs(wr["y"] - centre_y), default=None)
        if word_row is not None and abs(word_row["y"] - centre_y) <= tolerance:
            ordered = sorted(word_row["items"], key=lambda w: w["x"])
            name = " ".join(w["text"] for w in ordered).strip()
        else:
            name = ""

        half = max(3, int(typical_h * 0.9))
        y0, y1 = max(0, int(centre_y - half)), min(height, int(centre_y + half))

        elims_patch = img_bgr[y0:y1, elims_x0:elims_x1]
        elims = ocr_small_number(elims_patch)

        bars = []
        for (sx0, sx1) in bar_spans:
            patch = img_bgr[y0:y1, alive_x0 + sx0:alive_x0 + sx1 + 1]
            bars.append(classify_bar(patch, palette) if patch.size
                        else {"status": "unknown", "method": "empty"})

        rows.append({
            "teamName": name,
            # None (not 0) when the count couldn't be read at all, so the
            # dashboard can show "unread" rather than a confident zero --
            # a team on 0 elims and a team whose count failed to OCR are
            # very different things to put on a broadcast. Same reasoning
            # for an empty teamName: the row is real, its label just didn't
            # read, and that's something to show rather than hide.
            "elims": elims,
            "bars": [b["status"] for b in bars],
            "barDetail": bars,
            "aliveCount": sum(1 for b in bars if b["status"] == "alive"),
            "nameRead": bool(name),
            "rawText": name,
        })
    return {"rows": rows, "usedPalette": bool(palette)}


# ---------------------------------------------------------------------------
# Loadout icon identification.
#
# These slots hold artwork, not text, so OCR has nothing to read -- they get
# matched against a reference library instead. Same colour-signature
# approach the Valorant engine uses for agent portraits: split the image
# into a grid and average each cell, which captures rough colour AND its
# spatial arrangement (telling a mostly-dark icon with a bright top apart
# from one bright all over), far more robust than a single average colour
# and far cheaper than real template matching.
#
# Libraries are just folders of images -- the FILENAME is the label, so
# adding a newly released character means dropping "Kenta.png" into
# characters/ with no code change and no name table to maintain. That's a
# deliberate difference from the Valorant engine's hardcoded AGENT_FILE_MAP,
# which has to be edited by hand every time Riot ships an agent.
#
# Signatures are built once on first use and cached, since rebuilding them
# per capture would re-read and resize every reference image on every
# single Num5 press.
# ---------------------------------------------------------------------------

_icon_signature_cache = {}


def _icon_signature(img_bgr, grid=4):
    """Average BGR per cell of a grid x grid split, normalised for overall
    brightness. The normalisation matters here in a way it doesn't for
    Valorant portraits: the same character icon renders noticeably dimmer
    on a knocked/greyed-out HUD card than on a healthy one, and without it
    that brightness shift alone can outweigh the actual colour differences
    between two different characters."""
    resized = cv2.resize(img_bgr, (grid * 8, grid * 8), interpolation=cv2.INTER_AREA)
    cells = []
    h, w = resized.shape[:2]
    for gy in range(grid):
        for gx in range(grid):
            y0, y1 = h * gy // grid, h * (gy + 1) // grid
            x0, x1 = w * gx // grid, w * (gx + 1) // grid
            cell = resized[y0:y1, x0:x1]
            cells.append(cell.reshape(-1, 3).mean(axis=0))
    sig = np.array(cells, dtype=np.float32)
    mean = sig.mean()
    if mean > 1e-6:
        sig = sig / mean
    return sig


def load_reference_image(path):
    """Reads a reference icon and flattens any transparency onto the same
    dark background the game draws these cards on.

    This matters rather than being a detail: 80 of the 84 character
    portraits ship with a real alpha channel, and reading them with
    IMREAD_COLOR silently drops it, leaving whatever garbage sits in the
    transparent RGB pixels. The captured screen crop has no alpha at all --
    it's already composited by the game -- so a reference kept unflattened
    is being compared against something structurally different."""
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        img = (img[:, :, :3].astype(np.float32) * alpha
               + ICON_BACKDROP_VALUE * (1.0 - alpha)).astype(np.uint8)
    elif img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def load_icon_names(folder):
    path = folder / FREEFIRE_ICON_NAMES_FILE
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Blank values are skipped rather than honoured, so a template with
        # every ID listed and only some filled in behaves correctly -- an
        # unfilled entry falls back to showing the ID instead of rendering
        # an empty name onto a graphic.
        return {str(k): str(v).strip() for k, v in data.items() if str(v).strip()}
    except (json.JSONDecodeError, OSError):
        return {}


def load_icon_library(library_name):
    """{label: (signature, display_name)} for a reference folder, built once
    and cached. Returns empty (not an error) when the folder doesn't exist
    -- the engine keeps running and reports "no match" until the operator
    adds images, which is the actual state for pets/equipment today.

    "characters" reads the flat root of the asset dump; everything else
    reads a subfolder of the same name. See FREEFIRE_ASSETS_DIR."""
    if library_name in _icon_signature_cache:
        return _icon_signature_cache[library_name]

    folder = FREEFIRE_ASSETS_DIR if library_name == "characters" else FREEFIRE_ASSETS_DIR / library_name
    library = {}
    if folder.is_dir():
        names = load_icon_names(folder)
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            img = load_reference_image(path)
            if img is None:
                continue
            library[path.stem] = (_icon_signature(img), names.get(path.stem, path.stem))
    _icon_signature_cache[library_name] = library
    return library


def match_icon(img_bgr, library_name):
    """Returns {"label", "confidence", "lowConfidence"} for the closest
    reference image, or a null label when the library is empty.

    Confidence is a 0-1 rescaling of the distance to the best match
    relative to the second-best. Comparing against the runner-up rather
    than using raw distance is what makes the number meaningful: a crop
    that sits 'somewhat near' every icon in the library is genuinely
    ambiguous and scores low, while one that's decisively closer to one
    icon than all others scores high, which is exactly the distinction an
    operator needs when deciding whether to trust a match."""
    library = load_icon_library(library_name)
    if not library:
        where = "assets/FFM/" if library_name == "characters" else f"assets/FFM/{library_name}/"
        return {"label": None, "name": None, "confidence": 0.0, "lowConfidence": True,
                "reason": f"no reference images in {where}"}

    sig = _icon_signature(img_bgr)
    scored = sorted(
        ((float(np.linalg.norm(sig - ref)), label) for label, (ref, _) in library.items()),
    )
    best_distance, best_label = scored[0]
    if len(scored) > 1:
        runner_up = scored[1][0]
        confidence = 0.0 if runner_up <= 1e-6 else max(0.0, 1.0 - (best_distance / runner_up))
    else:
        # Single-image library -- nothing to compare against, so report it
        # as a match but never as a confident one.
        confidence = 0.0
    return {
        "label": best_label,
        "name": library[best_label][1],
        "confidence": round(confidence, 3),
        "lowConfidence": confidence < ICON_MATCH_MIN_CONFIDENCE,
    }


# ---------------------------------------------------------------------------
# Free Fire Max -- post-match result & safezone log file parsing. Both file
# types are written directly by the game client, not screenshots, so
# there's no OCR involved here -- just reading and parsing plain text.
# Fields are label-delimited ("TeamName:", "Rank:", ...) with variable-width
# space padding depending on name length, so parsing anchors on the labels
# themselves rather than fixed character columns.
#
# Example team block (one per squad, followed by exactly 4 player lines):
#   TeamName: Team Tufan     Rank: 1     KillScore: 48     RankScore: 12     TotalScore: 60
#   NAME: SAIKYO.01          ID: 2281027273     KILL: 7
#
# RankScore already matches Free Fire's own placement-points table
# (1st=12, 2nd=9, 3rd=8, 4th=7, 5th=6, 6th=5, 7th=4, 8th=3, 9th=2, 10th=1,
# 11th/12th=0) -- the game client bakes it into the file, so it's read
# directly rather than recomputed.
# ---------------------------------------------------------------------------

FREEFIRE_TEAM_LINE_REGEX = re.compile(
    r"TeamName:\s*(?P<name>.*?)\s*Rank:\s*(?P<rank>\d+)\s*"
    r"KillScore:\s*(?P<killscore>\d+)\s*RankScore:\s*(?P<rankscore>\d+)\s*"
    r"TotalScore:\s*(?P<totalscore>\d+)\s*$"
)
FREEFIRE_PLAYER_LINE_REGEX = re.compile(
    r"NAME:\s*(?P<name>.*?)\s*ID:\s*(?P<id>\d+)\s*KILL:\s*(?P<kill>\d+)\s*$"
)
# Results live in "MatchResult_<id>_<stamp>.log", confirmed against a real
# finished match. Two things this got wrong before, both of which produced
# the same misleading "no file found"/"nothing parsed" result:
#
#   1. The prefix. The client writes TWO files per match: "MatchId_..." at
#      match START, which stays empty forever (3 bytes, just a BOM), and
#      "MatchResult_..." at match END, which holds the actual table. This
#      matched the former, so it only ever found an empty file.
#   2. The extension is plain ".log", not ".log.txt".
#
# MatchId_ is deliberately NOT accepted as an alternative. The finder picks
# the newest file by timestamp, and a new match's empty MatchId_ marker is
# newer than the previous match's real MatchResult_ -- so allowing both
# would make the previous result unreadable the moment the next game
# started, which is exactly when an operator goes looking for it.
FREEFIRE_MATCH_FILENAME_REGEX = re.compile(
    r"^MatchResult_(?P<match_id>\d+)_(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.log(?:\.txt)?$",
    re.IGNORECASE,
)
FREEFIRE_SAFEZONE_FILENAME_REGEX = re.compile(
    r"^SafeZone_(?P<match_id>\d+)_(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.log(?:\.txt)?$",
    re.IGNORECASE,
)
FREEFIRE_SAFEZONE_COORD_REGEX = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def parse_freefire_match_result(text):
    teams = []
    current = None
    for raw_line in text.split("\n"):
        line = raw_line.lstrip("\ufeff").strip()
        if not line:
            continue
        team_match = FREEFIRE_TEAM_LINE_REGEX.match(line)
        if team_match:
            current = {
                "teamName": team_match.group("name").strip(),
                "rank": int(team_match.group("rank")),
                "killScore": int(team_match.group("killscore")),
                "rankScore": int(team_match.group("rankscore")),
                "totalScore": int(team_match.group("totalscore")),
                "players": [],
            }
            teams.append(current)
            continue
        player_match = FREEFIRE_PLAYER_LINE_REGEX.match(line)
        if player_match and current is not None:
            current["players"].append({
                "name": player_match.group("name").strip(),
                "uid": player_match.group("id"),
                "kills": int(player_match.group("kill")),
            })
    teams.sort(key=lambda t: t["rank"])
    return teams


# ---------------------------------------------------------------------------
# Resolving what the result file says against what the operator configured.
#
# The game client writes whatever name the team happened to register with,
# which is routinely NOT what should go on the broadcast: a team entered as
# "total" needs to show as "iQOO TOTAL GAMING" (or its short name "IQTG"),
# and a player's in-game display name can be anything on the day while the
# operator has already registered the IGN they want shown against that
# player's UID. An explicit request: the roster is the source of truth for
# BOTH, with the file only supplying the numbers.
#
# UID is matched exactly and wins outright -- it's a stable numeric account
# ID, so a hit there is certain in a way no name comparison can be. Team
# names have no such ID in the file, so they fall back to a three-step
# ladder: exact normalized match, then containment either direction (which
# is what actually catches "total" inside "iqoototalgaming" -- a plain
# similarity ratio scores that pair only ~0.45 and would lose to the
# threshold), then fuzzy ratio as a last resort.
# ---------------------------------------------------------------------------

TEAM_NAME_MIN_RATIO = 0.6
PLAYER_NAME_MIN_RATIO = 0.6


def normalize_for_match(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def match_roster_team(file_team_name, roster_teams):
    """Returns the roster team dict this result-file team name refers to,
    or None if nothing matches confidently enough."""
    target = normalize_for_match(file_team_name)
    if not target:
        return None

    candidates = []
    for team in roster_teams:
        for candidate_name in (team.get("name"), team.get("shortName")):
            norm = normalize_for_match(candidate_name)
            if norm:
                candidates.append((norm, team))

    for norm, team in candidates:
        if norm == target:
            return team
    # Containment before fuzzy -- see the block comment above for why.
    for norm, team in candidates:
        if target in norm or norm in target:
            return team

    best_team, best_ratio = None, 0.0
    for norm, team in candidates:
        ratio = difflib.SequenceMatcher(None, target, norm).ratio()
        if ratio > best_ratio:
            best_team, best_ratio = team, ratio
    return best_team if best_ratio >= TEAM_NAME_MIN_RATIO else None


def match_roster_player(file_player, roster_team):
    """UID first (exact, authoritative), then name similarity within that
    team only -- never across teams, since two squads can easily carry
    similar-looking names and a cross-team 'correction' would be worse
    than leaving the file's own name alone."""
    if not roster_team:
        return None
    players = roster_team.get("players", []) or []

    file_uid = str(file_player.get("uid") or "").strip()
    if file_uid:
        for player in players:
            if str(player.get("uid") or "").strip() == file_uid:
                return player

    target = normalize_for_match(file_player.get("name"))
    if not target:
        return None
    best_player, best_ratio = None, 0.0
    for player in players:
        ratio = difflib.SequenceMatcher(
            None, target, normalize_for_match(player.get("ign")),
        ).ratio()
        if ratio > best_ratio:
            best_player, best_ratio = player, ratio
    return best_player if best_ratio >= PLAYER_NAME_MIN_RATIO else None


def apply_unmatched_policy(teams, policy):
    """Applies the operator's chosen handling for rows that didn't resolve
    against the roster. "drop" is the only one that changes what's in the
    list; "allow" and "flag" both keep every row, differing only in whether
    the dashboard is told to highlight it. Unmatched PLAYERS never drop a
    row on their own -- a team with one unregistered stand-in still has a
    real result that belongs in the standings, and silently deleting the
    whole squad over one player would be worse than showing the name the
    file gave."""
    if policy == "drop":
        return [t for t in teams if t.get("matched")]
    if policy == "allow":
        for team in teams:
            team["needsReview"] = False
            for player in team.get("players", []):
                player["needsReview"] = False
        return teams
    for team in teams:  # "flag" (default)
        team["needsReview"] = not team.get("matched", False)
        for player in team.get("players", []):
            player["needsReview"] = not player.get("matched", False)
    return teams


def apply_roster_overrides(teams, roster):
    """Rewrites parsed result rows to use the operator's configured team
    name/short name and player IGNs wherever a confident match exists.

    Each row keeps what the file itself said in `fileTeamName`/`fileName`
    so the dashboard can show both -- an operator reviewing an import needs
    to see that "total" became "iQOO TOTAL GAMING" to catch a bad match,
    which is impossible if the original is silently discarded. `matched`
    flags rows where nothing lined up, so those can be surfaced rather
    than quietly passing through unresolved."""
    roster_teams = (roster or {}).get("teams", []) or []
    for team in teams:
        file_team_name = team.get("teamName", "")
        team["fileTeamName"] = file_team_name
        roster_team = match_roster_team(file_team_name, roster_teams)
        team["matched"] = roster_team is not None
        if roster_team:
            team["teamName"] = roster_team.get("name") or file_team_name
            team["shortName"] = roster_team.get("shortName") or ""
            team["logo"] = roster_team.get("logo") or ""
        for player in team.get("players", []):
            file_player_name = player.get("name", "")
            player["fileName"] = file_player_name
            roster_player = match_roster_player(player, roster_team)
            player["matched"] = roster_player is not None
            if roster_player:
                player["name"] = roster_player.get("ign") or file_player_name
                player["photo"] = roster_player.get("photo") or ""
    return teams


def parse_freefire_safezone(text):
    text = text.lstrip("\ufeff")
    m = FREEFIRE_SAFEZONE_COORD_REGEX.search(text)
    if not m:
        return None
    return (float(m.group(1)), float(m.group(2)))


def find_freefire_latest_match_file(folder):
    folder_path = Path(folder) if folder else None
    if not folder_path or not folder_path.is_dir():
        return (None, None)
    best = None
    for f in folder_path.iterdir():
        m = FREEFIRE_MATCH_FILENAME_REGEX.match(f.name)
        if m and (best is None or m.group("timestamp") > best[0]):
            best = (m.group("timestamp"), f, m)
    return (best[1], best[2]) if best else (None, None)


def find_freefire_latest_safezone_file(folder, match_id):
    folder_path = Path(folder) if folder else None
    if not folder_path or not folder_path.is_dir():
        return (None, None)
    best = None
    for f in folder_path.iterdir():
        m = FREEFIRE_SAFEZONE_FILENAME_REGEX.match(f.name)
        if not m or m.group("match_id") != str(match_id):
            continue
        if best is None or m.group("timestamp") > best[0]:
            best = (m.group("timestamp"), f, m)
    return (best[1], best[2]) if best else (None, None)


def compute_freefire_standings(matches):
    agg = {}
    for match in matches:
        for team in match.get("teams", []):
            name = (team.get("teamName") or "").strip()
            if not name:
                continue
            row = agg.setdefault(name, {
                "teamName": name, "shortName": team.get("shortName", ""),
                "matches": 0, "totalKills": 0, "placementPoints": 0,
                "totalPoints": 0, "bestRank": None, "booyahs": 0,
            })
            row["matches"] += 1
            row["totalKills"] += team.get("killScore", 0)
            # Placement points come from the file's own RankScore, not
            # derived as (total - kills): the game client already bakes its
            # placement table into the file, and recomputing it would
            # silently disagree the moment a format or scoring rule differs.
            row["placementPoints"] += team.get("rankScore", 0)
            row["totalPoints"] += team.get("totalScore", 0)
            if team.get("shortName") and not row.get("shortName"):
                row["shortName"] = team["shortName"]
            rank = team.get("rank")
            if rank is not None and (row["bestRank"] is None or rank < row["bestRank"]):
                row["bestRank"] = rank
            # BOOYAH = a first-place finish. Counted here rather than
            # derived later from bestRank, which only records the single
            # best result and so can't distinguish one win from five.
            if rank == 1:
                row["booyahs"] += 1
    standings = list(agg.values())
    standings.sort(key=lambda r: (-r["totalPoints"], -r["totalKills"]))
    return standings


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


# ---------------------------------------------------------------------------
# Num5 loadout capture. keyboard.add_hotkey() runs its callback on a
# background thread the `keyboard` library manages itself -- NOT the
# asyncio event loop this whole engine otherwise runs on -- so the
# callback can only do the synchronous screen-grab part directly; touching
# server_state or broadcasting has to hop back onto the event loop via
# asyncio.run_coroutine_threadsafe(), the standard way to call async code
# from a foreign thread. main_loop is captured once in main() at startup
# for exactly that handoff.
# ---------------------------------------------------------------------------

main_loop = None

# Debugger-log tail position and runtime-id -> player mapping. Module level
# because the mapping is built from join lines seen once at match start and
# must survive every later poll that reads only new combat lines.
_debugger_offset = 0
_debugger_path = None
_debugger_id_map = {}


def flatten_roster_players(roster):
    """[(team_index, player_index), ...] in team-then-player order --
    the exact sequence the operator walks through with 48 Num5 presses,
    so the dashboard's capture grid and this pointer stay in lockstep."""
    result = []
    for ti, team in enumerate(roster.get("teams", [])):
        for pi in range(len(team.get("players", []))):
            result.append((ti, pi))
    return result


def capture_region(sct, region_key):
    """Grabs one calibrated region, or None if it isn't calibrated yet --
    an uncalibrated slot is a normal state (the operator may only have set
    up some of them), not an error worth aborting the whole capture over."""
    region = config.get("regions", {}).get(region_key)
    if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
        return None
    return crop_to_bgr(sct, region)


def on_num5_pressed():
    """Runs on the `keyboard` library's own thread. Deliberately does ONLY
    the screen grabs here and hands everything else off: what's on screen
    is gone the moment the operator's card changes, so capture has to be
    immediate, while icon matching and OCR can happen a few milliseconds
    later on the event loop without racing the screen."""
    lc = server_state.get("loadoutCapture", {})
    if not lc.get("active"):
        return
    flat = flatten_roster_players(server_state.get("roster", {}))
    pointer = lc.get("pointer", 0)
    if pointer >= len(flat) or main_loop is None:
        return

    with mss.mss() as sct:
        crops = {
            slot: capture_region(sct, key)
            for slot, key in FREEFIRE_LOADOUT_SLOT_KEYS.items()
        }
        crops["ign"] = capture_region(sct, FREEFIRE_LOADOUT_IGN_KEY)
        crops["card"] = capture_region(sct, FREEFIRE_LOADOUT_REGION_KEY)

    if all(crop is None for crop in crops.values()):
        print("Num5 pressed but no loadout regions are calibrated -- nothing captured.")
        return

    team_index, player_index = flat[pointer]
    asyncio.run_coroutine_threadsafe(
        apply_loadout_capture(team_index, player_index, crops), main_loop
    )


def capture_dir_for(game_number):
    return Path(__file__).parent / "captures" / f"game{game_number}"


def identify_loadout(crops):
    """Matches each icon slot against its library and reads the IGN slot as
    text. Returns the per-slot results plus the crops re-encoded for
    preview, so the dashboard can show what was captured next to what it
    was identified as -- an explicit request, since a match is only
    trustworthy if the operator can see the picture it came from."""
    slots = {}
    for slot, library_name in FREEFIRE_ICON_LIBRARIES.items():
        crop = crops.get(slot)
        if crop is None:
            slots[slot] = {"label": None, "confidence": 0.0, "lowConfidence": True,
                           "reason": "region not calibrated"}
            continue
        slots[slot] = match_icon(crop, library_name)

    ign_crop = crops.get("ign")
    ign_text = ocr_text(ign_crop) if ign_crop is not None else ""
    return slots, ign_text


async def apply_loadout_capture(team_index, player_index, crops):
    try:
        player = server_state["roster"]["teams"][team_index]["players"][player_index]
    except (IndexError, KeyError):
        return  # roster changed under us (re-imported mid-capture) -- drop this one

    loop = asyncio.get_running_loop()
    slots, ign_text = await loop.run_in_executor(ocr_executor, identify_loadout, crops)

    game_number = server_state.get("event", {}).get("currentGame", 1)

    # Crops go to disk, not into server_state. Seven images per player
    # across a full 60-player lobby is megabytes of base64 that would then
    # ride along on EVERY state_sync broadcast -- the exact payload-bloat
    # problem that had to be fixed on the Valorant side after it showed up
    # as real bandwidth cost during a live match. State keeps the labels
    # (tiny); the dashboard pulls the pictures on demand, per player, via
    # freefire_fetch_loadout_capture.
    out_dir = capture_dir_for(game_number)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for slot, crop in crops.items():
            if crop is not None:
                cv2.imwrite(str(out_dir / f"t{team_index}_p{player_index}_{slot}.png"), crop)
        saved = True
    except OSError as e:
        print(f"Couldn't save loadout crops for t{team_index}p{player_index}: {e}")
        saved = False

    loadouts = player.setdefault("loadouts", {})
    loadouts[str(game_number)] = {
        "slots": slots,
        "ignRead": ign_text,
        "capturedAt": int(time.time() * 1000),
        "hasCrops": saved,
    }

    server_state["loadoutCapture"]["pointer"] += 1
    save_state()
    await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})


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

            if payload.get("type") == "manual_update":
                data = payload.get("data", {})
                if "freefire" in data:
                    server_state.update(data["freefire"])
                    server_state["standings"] = compute_freefire_standings(
                        server_state.get("matches", [])
                    )
                    # Bootstrap the roster from committed results when it's
                    # still empty. A result file already carries exactly what
                    # the roster needs -- team name, IGN and UID for all 12
                    # squads -- so making the operator retype it (or even
                    # click an import) after the first game is busywork.
                    # Guarded on "empty" so it can never overwrite a roster
                    # someone has curated: short names, logos and IGN
                    # overrides are all things the result file doesn't know.
                    if not (server_state.get("roster", {}).get("teams") or []):
                        bootstrapped = roster_from_matches(server_state.get("matches", []))
                        if bootstrapped:
                            server_state["roster"] = {"teams": bootstrapped}
                            print(f"Roster was empty -- bootstrapped {len(bootstrapped)} "
                                  f"teams from the committed match result.")
                for field in payload.get("lock", []):
                    locked_fields.add(field)
                for field in payload.get("unlock", []):
                    locked_fields.discard(field)
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "freefire_fetch_match":
                # Read-only lookup -- does NOT touch server_state. The
                # dashboard reviews the parsed result and commits it via a
                # normal manual_update (data.freefire.matches) only once
                # the operator confirms it.
                folder = payload.get("folder") or server_state.get("settings", {}).get("matchResultFolder", "")
                try:
                    file_path, name_match = find_freefire_latest_match_file(folder)
                    if not file_path:
                        await websocket.send(json.dumps({
                            "type": "freefire_match_result", "teams": [], "matchId": None,
                            "error": f"No MatchId_*.log.txt found in '{folder}'.",
                        }))
                    else:
                        text = file_path.read_text(encoding="utf-8-sig")
                        teams = parse_freefire_match_result(text)
                        # Resolve against the configured roster before the
                        # dashboard ever sees it -- see apply_roster_overrides.
                        teams = apply_roster_overrides(teams, server_state.get("roster", {}))
                        policy = server_state.get("settings", {}).get("unmatchedPolicy", "flag")
                        teams = apply_unmatched_policy(teams, policy)
                        await websocket.send(json.dumps({
                            "type": "freefire_match_result",
                            "matchId": name_match.group("match_id"),
                            "timestamp": name_match.group("timestamp"),
                            "fileName": file_path.name,
                            "gameNumber": server_state.get("event", {}).get("currentGame", 1),
                            "teams": teams,
                            "unmatchedTeams": [t["fileTeamName"] for t in teams if not t.get("matched")],
                            "error": None if teams else "File found but no team blocks could be parsed from it.",
                        }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "freefire_match_result", "teams": [], "matchId": None, "error": str(e),
                    }))
            elif payload.get("type") == "freefire_fetch_safezone":
                folder = payload.get("folder") or server_state.get("settings", {}).get("safezoneFolder", "")
                match_id = payload.get("matchId") or server_state.get("currentMatchId")
                try:
                    if not match_id:
                        await websocket.send(json.dumps({
                            "type": "freefire_safezone_result",
                            "error": "No matchId yet -- fetch a match result first.",
                        }))
                    else:
                        file_path, name_match = find_freefire_latest_safezone_file(folder, match_id)
                        if not file_path:
                            await websocket.send(json.dumps({
                                "type": "freefire_safezone_result",
                                "error": f"No SafeZone_{match_id}_*.log.txt found in '{folder}'.",
                            }))
                        else:
                            text = file_path.read_text(encoding="utf-8-sig")
                            coord = parse_freefire_safezone(text)
                            await websocket.send(json.dumps({
                                "type": "freefire_safezone_result",
                                "matchId": match_id,
                                "x": coord[0] if coord else None,
                                "y": coord[1] if coord else None,
                                "timestamp": name_match.group("timestamp"),
                                "fileName": file_path.name,
                                "error": None if coord else "File found but coordinates couldn't be parsed.",
                            }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "freefire_safezone_result", "error": str(e),
                    }))
            elif payload.get("type") == "freefire_fetch_loadout_capture":
                # On-demand crop retrieval -- see apply_loadout_capture for
                # why these live on disk instead of in state.
                try:
                    team_index = int(payload.get("teamIndex", -1))
                    player_index = int(payload.get("playerIndex", -1))
                    game_number = int(payload.get("gameNumber")
                                      or server_state.get("event", {}).get("currentGame", 1))
                except (TypeError, ValueError):
                    team_index = player_index = -1
                    game_number = 1
                images = {}
                folder = capture_dir_for(game_number)
                if team_index >= 0 and player_index >= 0 and folder.is_dir():
                    prefix = f"t{team_index}_p{player_index}_"
                    for path in folder.glob(f"{prefix}*.png"):
                        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                        if img is None:
                            continue
                        slot = path.stem[len(prefix):]
                        data_url = crop_to_data_url(img, scale=2)
                        if data_url:
                            images[slot] = data_url
                await websocket.send(json.dumps({
                    "type": "freefire_loadout_capture",
                    "teamIndex": team_index, "playerIndex": player_index,
                    "gameNumber": game_number,
                    "images": images,
                    "error": None if images else "No saved crops found for that player/game.",
                }))
            elif payload.get("type") == "freefire_capture_lobby":
                # Read-only: hands the parsed blocks back for review and
                # never touches the roster itself. Merging is the
                # dashboard's call, since only the operator can tell a
                # genuine new squad from an OCR variant of one already
                # captured on an earlier scroll.
                uid_pass = bool(payload.get("uidPass"))
                try:
                    loop = asyncio.get_running_loop()
                    blocks = await loop.run_in_executor(
                        ocr_executor, capture_lobby_blocks,
                        config.get("regions", {}), uid_pass,
                    )
                    calibrated = sum(
                        1 for k in LOBBY_BLOCK_KEYS
                        if (config.get("regions", {}).get(k) or {}).get("w", 0) > 0
                    )
                    await websocket.send(json.dumps({
                        "type": "freefire_lobby_capture",
                        "blocks": blocks, "uidPass": uid_pass,
                        "error": None if calibrated else
                                 "No lobby blocks calibrated yet -- run calibrate.py ff-lobby first.",
                    }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "freefire_lobby_capture", "blocks": [], "uidPass": uid_pass,
                        "error": str(e),
                    }))
            elif payload.get("type") == "scoreboard_show":
                server_state["display"]["scoreboardVisible"] = True
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "scoreboard_hide":
                server_state["display"]["scoreboardVisible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "points_table_show":
                server_state["display"]["pointsTableVisible"] = True
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "points_table_hide":
                server_state["display"]["pointsTableVisible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "loadout_capture_arm":
                server_state["loadoutCapture"]["active"] = True
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "loadout_capture_disarm":
                server_state["loadoutCapture"]["active"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "loadout_capture_skip":
                # Advance without capturing -- e.g. a player never actually
                # showed on screen for their turn.
                flat = flatten_roster_players(server_state.get("roster", {}))
                if server_state["loadoutCapture"]["pointer"] < len(flat):
                    server_state["loadoutCapture"]["pointer"] += 1
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "loadout_capture_set_pointer":
                try:
                    pointer = int(payload.get("pointer", 0))
                except (TypeError, ValueError):
                    pointer = 0
                flat = flatten_roster_players(server_state.get("roster", {}))
                server_state["loadoutCapture"]["pointer"] = max(0, min(pointer, len(flat)))
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "headshot_hunter_show":
                now_ms = int(time.time() * 1000)
                hh = server_state["headshotHunter"]
                hh["status"] = "shown"
                hh["shownUntil"] = now_ms + HEADSHOT_HUNTER_DISPLAY_SECONDS * 1000
                hh["playerName"] = payload.get("playerName", "")
                try:
                    hh["headshots"] = int(payload.get("headshots", 0))
                except (TypeError, ValueError):
                    hh["headshots"] = 0
                hh["photo"] = payload.get("photo", "")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "headshot_hunter_hide":
                server_state["headshotHunter"]["status"] = "idle"
                server_state["headshotHunter"]["shownUntil"] = None
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "team_eliminated_show":
                now_ms = int(time.time() * 1000)
                te = server_state["teamEliminated"]
                te["status"] = "shown"
                te["shownUntil"] = now_ms + TEAM_ELIMINATED_DISPLAY_SECONDS * 1000
                te["teamName"] = payload.get("teamName", "")
                try:
                    te["rank"] = int(payload.get("rank")) if payload.get("rank") not in (None, "") else None
                except (TypeError, ValueError):
                    te["rank"] = None
                te["photo"] = payload.get("photo", "")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "team_eliminated_hide":
                server_state["teamEliminated"]["status"] = "idle"
                server_state["teamEliminated"]["shownUntil"] = None
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
    finally:
        connected_clients.discard(websocket)
        connected_pages.pop(websocket, None)
        await broadcast_presence()


async def ocr_loop():
    """Only captures the two Free Fire live-ops regions -- there's no
    numeric HUD pipeline here at all, unlike ocr_engine.py's REGION_ORDER
    loop, since none of that applies to Free Fire."""
    interval = config.get("poll_interval_seconds", 1.0)
    loop = asyncio.get_running_loop()

    with mss.mss() as sct:
        frame_counter = 0
        while True:
            regions = config.get("regions", {})

            killfeed_region = regions.get(FREEFIRE_KILLFEED_REGION_KEY)
            killfeed_crop = None
            if killfeed_region and killfeed_region.get("w", 0) > 0 and killfeed_region.get("h", 0) > 0:
                killfeed_crop = crop_to_bgr(sct, killfeed_region)

            sidetable_region = regions.get(FREEFIRE_SIDETABLE_REGION_KEY)
            sidetable_crop = None
            if sidetable_region and sidetable_region.get("w", 0) > 0 and sidetable_region.get("h", 0) > 0:
                sidetable_crop = crop_to_bgr(sct, sidetable_region)

            ocr_tasks = []
            text_region_names = []
            for name, crop in (("killfeed", killfeed_crop), ("sidetable", sidetable_crop)):
                if crop is not None:
                    ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_text, crop))
                    text_region_names.append(name)

            changed = False
            if ocr_tasks:
                results = await asyncio.gather(*ocr_tasks)
                text_results = dict(zip(text_region_names, results))
                killfeed_raw_text = text_results.get("killfeed")
                sidetable_raw_text = text_results.get("sidetable")

                ff_live = server_state["liveOps"]
                if killfeed_raw_text is not None and killfeed_raw_text != ff_live.get("killfeedLastText", ""):
                    ff_live["killfeedLastText"] = killfeed_raw_text
                    changed = True
                if sidetable_raw_text is not None and sidetable_raw_text != ff_live.get("sidetableLastText", ""):
                    ff_live["sidetableLastText"] = sidetable_raw_text
                    changed = True

            # Live kill feed from the client's debugger log. Cheap (a
            # forward read of whatever was appended since last poll), so it
            # runs every cycle rather than on the slower OCR cadence.
            global _debugger_offset, _debugger_path, _debugger_id_map
            debugger_folder = (server_state.get("settings", {}).get("debuggerFolder")
                               or server_state.get("settings", {}).get("matchResultFolder", ""))
            if debugger_folder:
                candidate = find_latest_debugger_log(
                    Path(debugger_folder) / "Debugger"
                ) or find_latest_debugger_log(debugger_folder)
                if candidate is not None:
                    if candidate != _debugger_path:
                        # New log file -- a fresh session. Start from the top
                        # so the join lines that build the id mapping are
                        # picked up; without them every later kill line
                        # resolves to blank names.
                        _debugger_path = candidate
                        _debugger_offset = 0
                        _debugger_id_map = {}
                    new_events, _debugger_offset = await loop.run_in_executor(
                        ocr_executor, read_debugger_events,
                        candidate, _debugger_offset, _debugger_id_map,
                    )
                    if new_events:
                        feed = server_state["liveOps"].get("killEvents") or []
                        feed = (feed + new_events)[-MAX_KILL_EVENTS:]
                        server_state["liveOps"]["killEvents"] = feed
                        changed = True

            # Structured side-table parse. Runs off the same crop the raw
            # OCR above used, on the executor since it does its own OCR pass
            # plus per-bar colour sampling.
            if sidetable_crop is not None:
                parsed = await loop.run_in_executor(
                    ocr_executor, parse_sidetable, sidetable_crop,
                    config.get("sidetable_columns"), config.get("sidetable_colors"),
                )
                ff_live = server_state["liveOps"]
                if parsed["rows"] != ff_live.get("sidetableRows"):
                    ff_live["sidetableRows"] = parsed["rows"]
                    ff_live["sidetableUsedPalette"] = parsed["usedPalette"]
                    changed = True
            else:
                killfeed_raw_text = None
                sidetable_raw_text = None

            if frame_counter % 2 == 0:
                if killfeed_crop is not None:
                    data_url = crop_to_data_url(killfeed_crop)
                    if data_url:
                        await broadcast({
                            "type": "crop_preview", "region": FREEFIRE_KILLFEED_REGION_KEY,
                            "image": data_url, "text": killfeed_raw_text or "",
                        })
                if sidetable_crop is not None:
                    data_url = crop_to_data_url(sidetable_crop)
                    if data_url:
                        await broadcast({
                            "type": "crop_preview", "region": FREEFIRE_SIDETABLE_REGION_KEY,
                            "image": data_url, "text": sidetable_raw_text or "",
                        })

            if changed:
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            frame_counter += 1
            await asyncio.sleep(interval)


async def relay_client_loop():
    """Optional, additive: if freefire_config.json has a "relay" section
    with enabled=true, also connect OUT to the cloud relay as a client, so
    admins/overlays reachable over the internet get the same live state as
    anyone on localhost. Reuses handle_client() unchanged, same as
    ocr_engine.py's version -- no-op if relay isn't configured."""
    relay_cfg = config.get("relay", {})
    if not relay_cfg.get("enabled"):
        return
    url = relay_cfg.get("url", "")
    token = relay_cfg.get("token", "")
    if not url or not token:
        print("Relay is enabled in freefire_config.json but 'url'/'token' aren't both set -- skipping relay connection.")
        return
    separator = "&" if "?" in url else "?"
    connect_url = f"{url}{separator}token={token}"
    while True:
        try:
            async with websockets.connect(connect_url) as relay_ws:
                print(f"Connected to cloud relay at {url}")
                await handle_client(relay_ws)
        except Exception as e:
            print(f"Relay connection lost/failed ({e}); retrying in 3s...")
        await asyncio.sleep(3)


async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    try:
        keyboard.add_hotkey(NUM5_HOTKEY, on_num5_pressed)
        print(f"Loadout capture armed on '{NUM5_HOTKEY}' (only fires while loadoutCapture.active is true)")
    except Exception as e:
        print(f"Couldn't register the {NUM5_HOTKEY} hotkey ({e}) -- loadout capture won't work.")
        print("On Windows this usually means the process needs to run as Administrator.")

    host = config.get("server_host", "localhost")
    port = config.get("server_port", 8765)
    async with websockets.serve(handle_client, host, port):
        print(f"Free Fire OCR engine running at ws://{host}:{port}")
        print("Open dashboard.html's FreeFire Max tab in a browser, and")
        print("overlay/freefire_scoreboard.html + overlay/freefire_booyah.html in OBS as Browser Sources.")
        await asyncio.gather(ocr_loop(), relay_client_loop())


if __name__ == "__main__":
    asyncio.run(main())
