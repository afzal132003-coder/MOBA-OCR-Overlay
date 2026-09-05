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
FREEFIRE_ASSETS_DIR = Path(__file__).parent.parent.parent / "overlay" / "assets" / "freefire"

# Below this, a "best match" is reported but flagged low-confidence rather
# than trusted -- a colour signature always returns SOME nearest neighbour
# even when the real icon isn't in the library at all, and silently
# accepting that is how a wrong character ends up on a Booyah graphic.
ICON_MATCH_MIN_CONFIDENCE = 0.55

NUM5_HOTKEY = "num 5"

HEADSHOT_HUNTER_DISPLAY_SECONDS = 6
TEAM_ELIMINATED_DISPLAY_SECONDS = 6

MAX_OCR_DIMENSION = 1920

connected_clients = set()
connected_pages = {}
ocr_executor = ThreadPoolExecutor(max_workers=2)


def default_state():
    return {
        "settings": {"matchResultFolder": "", "safezoneFolder": ""},
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
        "display": {"scoreboardMode": "match"},
        # Pre-match roster, uploaded once per event as a CSV (team, ign,
        # uid per row). Each player's "loadout" is manual-entry text
        # fields (active/passive x3/pet/equipment); "loadoutScreenshot" is
        # the Num5-captured HUD card image -- see loadoutCapture below.
        "roster": {"teams": []},
        # Raw OCR text only, refreshed every capture cycle once the
        # freefire_killfeed/freefire_sidetable regions are calibrated.
        "liveOps": {"killfeedLastText": "", "sidetableLastText": ""},
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


def load_icon_library(library_name):
    """{label: signature} for every image in overlay/assets/freefire/<name>/,
    built once and cached. Returns an empty dict (not an error) when the
    folder doesn't exist yet -- the engine has to keep running and simply
    report "no match" until the operator drops their reference images in."""
    if library_name in _icon_signature_cache:
        return _icon_signature_cache[library_name]

    library = {}
    folder = FREEFIRE_ASSETS_DIR / library_name
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is None:
                continue
            library[path.stem] = _icon_signature(img)
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
        return {"label": None, "confidence": 0.0, "lowConfidence": True,
                "reason": f"no reference images in assets/freefire/{library_name}/"}

    sig = _icon_signature(img_bgr)
    scored = sorted(
        ((float(np.linalg.norm(sig - ref)), label) for label, ref in library.items()),
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
FREEFIRE_MATCH_FILENAME_REGEX = re.compile(
    r"^MatchId_(?P<match_id>\d+)_(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.log\.txt$",
    re.IGNORECASE,
)
FREEFIRE_SAFEZONE_FILENAME_REGEX = re.compile(
    r"^SafeZone_(?P<match_id>\d+)_(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.log\.txt$",
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
                "teamName": name, "matches": 0, "totalKills": 0,
                "totalPoints": 0, "bestRank": None,
            })
            row["matches"] += 1
            row["totalKills"] += team.get("killScore", 0)
            row["totalPoints"] += team.get("totalScore", 0)
            rank = team.get("rank")
            if rank is not None and (row["bestRank"] is None or rank < row["bestRank"]):
                row["bestRank"] = rank
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
