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

ASSET_NAMES_PATH = FREEFIRE_ASSETS_DIR / "names.json"
# The operator's own curated pull of full-body character renders, kept
# separate from the icon dump: source ids there (e.g. "710000117") are
# Free Fire's OUTFIT/skin bundle renders, not a per-character identity --
# one character has many skins, and the render is dominated by the
# outfit, not the face. There is no lookup table in the game's own files
# tying a skin render back to a base character, so this folder's naming
# has to be done by eye, same as the icon library, and separately from it.
FULLSIZE_DIR = FREEFIRE_ASSETS_DIR / "Full Size Character"
FULLSIZE_NAMES_PATH = FULLSIZE_DIR / "names.json"


def load_asset_names():
    """Reads assets/FFM/names.json, the dump's own id -> name stub.

    Returns {} rather than raising if it is missing or malformed: naming is
    a convenience layered on top of the icon matching, and a bad file
    should cost the operator a dropdown label, not the engine."""
    try:
        with open(ASSET_NAMES_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if isinstance(data, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_asset_names(names):
    """Writes the naming back beside the artwork it describes.

    Every id in the folder is kept as a key even when unnamed, so the file
    stays a complete checklist of what still needs a name rather than only
    listing what is done."""
    merged = {}
    for path in sorted(FREEFIRE_ASSETS_DIR.glob("*.png")):
        merged[path.stem] = names.get(path.stem, "")
    merged.update({k: v for k, v in names.items() if v})
    try:
        with open(ASSET_NAMES_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False, sort_keys=True)
    except OSError as e:
        print(f"Could not write {ASSET_NAMES_PATH.name}: {e}")


def load_fullsize_names():
    try:
        with open(FULLSIZE_NAMES_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if isinstance(data, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_fullsize_names(names):
    merged = {}
    if FULLSIZE_DIR.is_dir():
        for path in sorted(FULLSIZE_DIR.glob("*.png")):
            merged[path.stem] = names.get(path.stem, "")
    merged.update({k: v for k, v in names.items() if v})
    try:
        with open(FULLSIZE_NAMES_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False, sort_keys=True)
    except OSError as e:
        print(f"Could not write {FULLSIZE_NAMES_PATH.name}: {e}")


def fullsize_catalogue():
    if not FULLSIZE_DIR.is_dir():
        return []
    return [{"id": path.stem} for path in sorted(FULLSIZE_DIR.glob("*.png"))]


def asset_catalogue():
    """Every icon in the dump, grouped the way the operator has to think
    about it. Ids beginning 101/102 are Free Fire's female/male character
    ranges; everything non-numeric in the dump so far is equipment."""
    entries = []
    for path in sorted(FREEFIRE_ASSETS_DIR.glob("*.png")):
        asset_id = path.stem
        if asset_id.startswith("101"):
            kind = "character"
        elif asset_id.startswith("102"):
            kind = "character"
        elif asset_id.isdigit():
            kind = "unknown"
        else:
            kind = "equipment"
        entries.append({"id": asset_id, "kind": kind})
    return entries

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

# Dashboard crop previews are judged by eye for framing, never re-read, so
# they get lossy compression -- see crop_to_data_url.
PREVIEW_JPEG_QUALITY = 85

connected_clients = set()
connected_pages = {}
# The one socket this engine opens OUT to the cloud relay, if configured.
# Tracked separately because it is the one connection whose page= identity
# is meaningless -- see broadcast_to_page().
relay_websocket = None
# region key -> the last preview data URL actually sent, so an unchanged
# crop isn't re-encoded onto the wire every poll. Cleared whenever a client
# connects, so a dashboard opened onto a still screen still gets one.
last_preview_sent = {}
ocr_executor = ThreadPoolExecutor(max_workers=2)


def default_state():
    return {
        "settings": {
            "matchResultFolder": "", "safezoneFolder": "",
            # Where the client writes its debugger-*.log files. Same folder
            # as the result files on a standard install; kept separate so a
            # setup that relocates one doesn't break the other.
            "debuggerFolder": "",
            # Both of these put something on air without the operator
            # pressing anything, so both can be switched off. On by default
            # because the whole point is that the graphic lands at the
            # moment it happened rather than whenever someone noticed.
            "autoTeamEliminated": True,
            "autoFetchOnMatchEnd": True,
            # Ignore everything the client logged before this moment.
            # Two or three events can run on one machine in a day and the
            # client keeps appending to the same log, so without a cutoff
            # the 9pm show would inherit the 5pm one's matches, knockdowns
            # and eliminations. Format matches the log's own timestamps,
            # "YYYY-MM-DD HH:MM:SS"; blank means read the whole file.
            "debuggerStartAt": "",
            # Champion Rush: the points a team must reach before a Booyah
            # can crown them. See compute_champion_rush.
            "championRushThreshold": 110,
            # What happens to a result row that couldn't be resolved against
            # the roster (unknown team, or a UID/name that isn't registered).
            # "allow" keeps the row using whatever the file said; "flag"
            # keeps it but marks it for review; "drop" excludes it entirely.
            # An explicit request to control this rather than have the
            # engine decide: a scrim with guest teams wants "allow", while
            # a final where every roster entry is verified wants unknown
            # rows kept out of the standings rather than quietly scoring.
            "unmatchedPolicy": "flag",
            # A Google Apps Script Web App URL (see push_sidetable_to_sheet
            # below). When set, every real change to the live alive/elim
            # side table is POSTed there too, so an operator's broadcast
            # sheet updates itself from the same log-driven data the Alive
            # Status overlay already shows -- instead of a person ticking
            # checkboxes by hand while watching the stream. Blank disables
            # it entirely; nothing is sent anywhere by default.
            "sheetWebhookUrl": "",
        },
        "currentMatchId": None,
        "currentContext": "",
        "knownContexts": [],
        "currentSafezone": None,
        "matches": [],
        "standings": [],
        # Row order for the "Export for Sheet" card -- which roster team
        # name goes on which line of the copy/paste output, since an
        # operator's own spreadsheet has a fixed row per team that rarely
        # matches rank or roster order. Persisted (rather than kept only in
        # the browser tab) so it survives a dashboard reload instead of
        # silently resetting mid-event -- see ffQmeRender/ffQmeSeedFromRoster
        # in dashboard.html.
        "qmeRowOrder": [],
        # MVP of the LATEST committed match. Empty string means "auto" --
        # highest kills, damage as the tiebreak, picked fresh from that
        # match's own players every time. Set to a uid to override the
        # pick by hand (a tie the operator wants to break a specific way,
        # or a stat the formula doesn't capture). Keyed by uid rather than
        # name so it survives a display-name change and never collides
        # across two players sharing one.
        "mvpOverride": "",
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
            # Which artwork set the overlays draw from:
            # overlay/assets/freefire/<assetFolder>/. Two or three different
            # companies' events can run on the same day, each with its own
            # graphics package, so the overlays can't hardcode one -- the
            # operator names the event here and every overlay follows.
            "assetFolder": "CLT",
        },
        # Which view freefire_scoreboard.html shows -- "match" (latest
        # committed match's own results) or "overall" (cumulative
        # standings). freefire_booyah.html has no mode: it always shows
        # the latest committed match's rank-1 team.
        # pointsTableVisible drives freefire_points_table.html (the overall
        # standings graphic), pushed/pulled by the operator like every other
        # overlay rather than showing itself whenever data changes.
        "display": {"scoreboardMode": "match", "pointsTableVisible": False,
                    "scoreboardVisible": True,
                    # Champion Rush emblem beside an activated team's name.
                    # Off by default: it is a format the operator opts into,
                    # and the artwork may not exist in every package.
                    "championRushBadgeVisible": False,
                    # The live 12-team side table overlay. Off by default:
                    # it belongs on air during a match, not between them.
                    "aliveStatusVisible": False,
                    # The Booyah team stats card. Its own source, so it does
                    # not contend with the scoreboard/points table pair.
                    "booyahStatsVisible": False,
                    # Second Booyah slide: the winning squad's loadout
                    # (character/weapon/pet/equipment) instead of
                    # eliminations/knocks/contribution. Its own toggle since
                    # the two slides are shown one at a time, not together.
                    "booyahLoadoutVisible": False,
                    # MVP card, Game Summary and the Elimination & Damage
                    # Report -- each its own browser source, each off until
                    # pushed, same reasoning as every other graphic here.
                    "mvpVisible": False,
                    "gameSummaryVisible": False,
                    "damageReportVisible": False},
        # Pre-match roster, uploaded once per event as a CSV (team, ign,
        # uid per row). Each player's "loadout" is manual-entry text
        # fields (active/passive x3/pet/equipment); "loadoutScreenshot" is
        # the Num5-captured HUD card image -- see loadoutCapture below.
        "roster": {"teams": []},
        # Operator-taught corrections for names the matcher cannot resolve
        # on its own. "LT ESPORT" in a result file against "LT ESPORTS" in
        # the roster is the everyday case; a player who changed IGN
        # mid-event is the other. Kept separate from the roster so a roster
        # re-import never wipes what was taught, and consulted BEFORE the
        # fuzzy matcher, so an explicit mapping always wins over a guess.
        "aliases": {"teams": {}, "players": {}, "squads": {}},
        # Asset id -> the character/equipment name an operator has given it.
        # The dump is named by Free Fire's own numeric ids, which nobody can
        # read off a dropdown, so naming them once turns the whole library
        # into something pickable. Mirrored to assets/FFM/names.json so the
        # work survives a state reset and can be shared between machines.
        "assetNames": {},
        "fullSizeNames": {},
        # Per-graphic nudges: graphicOverrides["scoreboardLogo"] etc ->
        # {dx, dy, scale}, applied on top of each graphic's built-in
        # position. Same pattern the MOBA/Valorant "Graphic Fixing" tab
        # already uses (state.graphicOverrides there); this is Free Fire's
        # own copy since the two engines don't share state.
        "graphicOverrides": {},
        # What is actually in the dump, so the dashboard can render the
        # library without a directory listing of its own. Static per
        # install, refreshed at startup.
        "assetCatalogue": [],
        "fullSizeCatalogue": [],
        # What the system currently cannot resolve, for the Mapping tab to
        # offer. Rebuilt from live state rather than accumulated, so an
        # entry disappears the moment it stops being a problem.
        "pending": {"teams": [], "players": [], "squads": []},
        # Derived from the committed matches -- see compute_champion_rush.
        "championRush": {"threshold": 110, "activated": [], "champion": "",
                         "championGame": None},
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
            # Which roster team the operator has put on each on-screen row
            # of the live side table, index = row position (0 = topmost).
            # Blank means "not assigned yet" -- see build_alive_grid /
            # apply_alive_grid_identities. Set from the dashboard, once
            # per match; the client's own live side table doesn't reorder
            # rows mid-match, so this shouldn't need touching again until
            # the next game.
            # 12, matching FREEFIRE_ALIVE_GRID_ROWS below -- written as a
            # literal here since that constant isn't defined yet this
            # early in the file, and default_state() runs at import time.
            "aliveRowTeams": [""] * 12,
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
            # The squad's kill count at the moment it went out, shown under
            # the name on the graphic.
            "kills": 0,
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
            # names.json is the shared record of what each asset id is, so
            # it seeds an empty state rather than the state being the only
            # copy. A state that already has names keeps them: it is the
            # one being edited live.
            if not state.get("assetNames"):
                from_file = {k: v for k, v in load_asset_names().items() if v}
                if from_file:
                    state["assetNames"] = from_file
                    print(f"Loaded {len(from_file)} asset name(s) from names.json.")
            if not state.get("fullSizeNames"):
                from_file = {k: v for k, v in load_fullsize_names().items() if v}
                if from_file:
                    state["fullSizeNames"] = from_file
                    print(f"Loaded {len(from_file)} full-size character name(s).")
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
# Rebuilt every start rather than trusted from the saved state: the
# operator can drop new artwork into assets/FFM between sessions, and a
# stale list would hide it.
server_state["assetCatalogue"] = asset_catalogue()
server_state["fullSizeCatalogue"] = fullsize_catalogue()
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


def crop_to_data_url(img_bgr, scale=1, jpeg_quality=None):
    """Encodes a crop for a dashboard preview <img>.

    The defaults are the point of this function. It used to upscale 3x
    with NEAREST and always encode PNG, which cost 2.2 MB for the two live
    regions -- sent every second poll, so roughly 9 Mbps of sustained
    uplink to the relay. Everything the dashboard asked for afterwards
    queued behind that: "Fetch latest match result" took seconds on the
    operator's screen for a lookup measured at under 10 ms of actual work.

    Both halves of the cost were waste. The upscale bought nothing -- the
    dashboard shows these at width:100% inside a half-width column, so the
    browser threw the extra pixels away after they had been paid for over
    the wire twice (engine -> relay -> browser). And PNG is the wrong
    codec for a screenshot: JPEG at q85 is another 10x smaller with no
    difference that matters for judging whether a box is framed right,
    which is all these are for -- the OCR text itself travels as text in
    the same message, not read back off the image.

    scale/jpeg_quality stay available for the loadout capture, which is
    one-shot, small, and compared against reference artwork by eye.
    """
    img = img_bgr
    if scale != 1:
        img = cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    if jpeg_quality is None:
        ok, buf = cv2.imencode(".png", img)
        mime = "png"
    else:
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        mime = "jpeg"
    if not ok:
        return None
    b64 = base64.b64encode(buf).decode("ascii")
    return f"data:image/{mime};base64,{b64}"


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

# The client also narrates the whole match at team level, which is where
# the 12-team side table's contents actually come from -- reading them off
# the screen with OCR was always a lossy re-derivation of numbers the game
# had already written down.
#
#   OnTeamScoreInited   the 12 team names, once, as the match loads
#   OnTeamScoreChanged  a team's running score; broadcast for all 12
#                       whenever any one of them changes
#   PCInGameFlagManager a squad wipe. Checked against 5 full matches: each
#                       of these lines lands in the SAME MILLISECOND as
#                       that squad's last death, 11/11 in every match.
#
# Note OnTeamScoreChanged is the live KILL count during the match, but the
# client adds placement points to it at the end -- the last value of the
# match equals the result file's TotalScore, verified 12/12. So it is only
# safe to read as "kills" while the match is still running.
DEBUGGER_TEAM_INIT_REGEX = re.compile(
    r"OnTeamScoreInited -> TeamName:\s*(?P<name>.+?)\s+TeamID:\s*(?P<tid>\d+)\s*$"
)
DEBUGGER_TEAM_SCORE_REGEX = re.compile(
    r"OnTeamScoreChanged -> TeamID:\s*(?P<tid>\d+)\s+TeamScore:\s*(?P<score>\d+)"
)
DEBUGGER_TEAM_WIPE_REGEX = re.compile(
    r"PCInGameFlagManager: Team (?P<gs_team>\d+) eliminated"
)
DEBUGGER_SPECTATOR_ADD_REGEX = re.compile(
    r"\[UIModelSpectator\] AddPlayer id(?P<pid>\d+),name(?P<ign>.*?),gsTeam(?P<gs_team>\d+)"
)
# A dead player can come back -- Free Fire has revival points -- so a death
# is not the same as being out, and the death count per squad runs well
# past four. This is what puts a player back on the alive side.
DEBUGGER_REVIVE_REGEX = re.compile(r"Revive Player\s+(?P<pid>\d+),")
# Written the instant the match ends. Both the result file and the replay
# JSON are on disk by the same second (checked across 7 matches), so this
# line is a safe trigger to go and read them.
DEBUGGER_MATCH_END_REGEX = re.compile(r"matchend matchid = (?P<match_id>\d+)")

# The client packs the squad number into the high bits of a runtime player
# id, so every kill, knock, death and revive line says which squad it
# concerns without any lookup. Verified against every AddPlayer line in a
# session's log, where the client states the team explicitly: 653/653.
GS_TEAM_SHIFT = 24


def gs_team_of(player_id):
    """The squad number encoded in a runtime player id."""
    try:
        return int(player_id) >> GS_TEAM_SHIFT
    except (TypeError, ValueError):
        return 0


def blank_live_match():
    """Live match state derived from the log, rebuilt at each match start.

    Two team numbering spaces exist and they do NOT agree -- TeamID (used
    by the score lines) and gsTeam (used by player ids and wipe lines).
    Measured on one match: they matched for 0 of 12 teams. Both are
    therefore kept separately and only ever joined through the roster, in
    link_live_teams()."""
    return {
        "matchId": None,
        "ended": False,
        "teamNames": {},   # TeamID -> name, from OnTeamScoreInited
        "teamScores": {},  # TeamID -> running score
        "gsIgns": {},      # gsTeam -> {pid: ign}
        "gsDown": {},      # gsTeam -> {pid} currently dead and not revived
        "gsKills": {},     # gsTeam -> kills credited to that squad
        "playerKnocks": {},# runtime player id -> knockdowns credited
        "wiped": [],       # gsTeam, in the order the client wiped them
    }

# Kept bounded: a full match produces hundreds of events and the whole lot
# rides along in every state_sync.
MAX_KILL_EVENTS = 60


def find_latest_debugger_log(folder):
    path = Path(folder) if folder else None
    if not path or not path.is_dir():
        return None
    logs = [f for f in path.glob("debugger-*.log") if f.is_file()]
    return max(logs, key=lambda f: f.stat().st_mtime) if logs else None


def read_debugger_events(log_path, offset, id_map, live=None, emit=True):
    """Reads new lines since `offset`, updating id_map and `live` in place.

    Returns (events, new_offset, signals). A partial trailing line is left
    for the next pass rather than parsed half-written -- the game is still
    appending to this file while we read it.

    `signals` are the things that need to happen OUTSIDE this function --
    a squad wipe to put on air, a match that just ended. They're returned
    rather than acted on because this runs on a worker thread, where
    touching server_state or broadcasting would be a data race.

    emit=False still parses everything and builds the same state, but
    returns no signals. That is how the first pass over an already-written
    log is done: restarting the engine mid-event otherwise re-reads the
    whole session from the top and replays every elimination in it, which
    now means firing all of them onto the broadcast. History has to be
    caught up on silently; only what happens after that goes on air."""
    events = []
    signals = []
    if live is None:
        live = blank_live_match()

    def signal(payload):
        if emit:
            signals.append(payload)

    def roll_over_if_finished():
        """Starts a fresh match the first time the next one speaks.

        Deliberately NOT triggered by the team-name lines, which would be
        the obvious choice: the client writes its Player Join lines BEFORE
        OnTeamScoreInited, so resetting there threw away the very roster
        the new match had just announced. That cost the squad-to-team
        mapping for most of each match -- placement came out right for 20
        of 77 eliminations. Rolling over on whichever line arrives first
        instead: 77/77."""
        if live["ended"]:
            live.clear()
            live.update(blank_live_match())
            signal({"type": "match_start"})
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
        return [], offset, signals

    if "\n" in chunk:
        complete, _, remainder = chunk.rpartition("\n")
        new_offset -= len(remainder.encode("utf-8", errors="replace"))
    else:
        return [], offset, signals

    # Compared as plain strings, which works because the client writes
    # "[YYYY-MM-DD HH:MM:SS.mmm]" -- a format that sorts chronologically --
    # and because a regex per line over a 26 MB file is not free.
    cutoff = (server_state.get("settings", {}).get("debuggerStartAt") or "").strip()[:19]

    headshots = {}
    for line in complete.splitlines():
        if cutoff and line[:1] == "[" and line[1:20] < cutoff:
            continue
        join = DEBUGGER_JOIN_REGEX.search(line)
        if join:
            roll_over_if_finished()
            id_map[join.group("pid")] = {
                "uid": join.group("uid"),
                "ign": join.group("ign").strip(),
            }
            live["gsIgns"].setdefault(gs_team_of(join.group("pid")), {})[
                join.group("pid")] = join.group("ign").strip()
            continue

        # --- team-level narration: names, running scores, squad wipes ---
        init = DEBUGGER_TEAM_INIT_REGEX.search(line)
        if init:
            roll_over_if_finished()
            live["teamNames"][int(init.group("tid"))] = init.group("name").strip()
            continue

        score = DEBUGGER_TEAM_SCORE_REGEX.search(line)
        if score:
            live["teamScores"][int(score.group("tid"))] = int(score.group("score"))
            continue

        add = DEBUGGER_SPECTATOR_ADD_REGEX.search(line)
        if add:
            roll_over_if_finished()
            live["gsIgns"].setdefault(int(add.group("gs_team")), {})[
                add.group("pid")] = add.group("ign").strip()
            continue

        wipe = DEBUGGER_TEAM_WIPE_REGEX.search(line)
        if wipe:
            gs_team = int(wipe.group("gs_team"))
            if gs_team not in live["wiped"]:
                live["wiped"].append(gs_team)
                ts = DEBUGGER_TS_REGEX.match(line.strip())
                signal({
                    "type": "team_wiped", "gsTeam": gs_team,
                    "order": len(live["wiped"]),
                    "total": len(live["teamNames"]) or 12,
                    # The squad's own players travel WITH the signal rather
                    # than being looked up afterwards. One read can contain
                    # a whole match's tail plus the next match's opening
                    # lines, and the roll-over in between reassigns every
                    # gsTeam number -- resolving later then names the wrong
                    # squad. Measured on a coarse replay: 67 of 71
                    # placements right when resolved after the fact,
                    # 71/71 when carried like this.
                    "igns": list(live["gsIgns"].get(gs_team, {}).values()),
                    # Carried for the same reason as the IGNs -- the squad
                    # is out, so this is its final kill count.
                    "kills": live["gsKills"].get(gs_team, 0),
                    "time": ts.group("ts") if ts else "",
                })
            continue

        revive = DEBUGGER_REVIVE_REGEX.search(line)
        if revive:
            pid = revive.group("pid")
            live["gsDown"].get(gs_team_of(pid), set()).discard(pid)
            continue

        end = DEBUGGER_MATCH_END_REGEX.search(line)
        if end:
            live["matchId"] = end.group("match_id")
            live["ended"] = True
            # Resolved to UIDs here, while the id map still belongs to the
            # match that just finished -- runtime ids are reused by the
            # next one.
            by_uid = {}
            for pid, count in live["playerKnocks"].items():
                uid = (id_map.get(pid) or {}).get("uid")
                if uid:
                    by_uid[str(uid)] = count
            signal({"type": "match_end", "matchId": end.group("match_id"),
                    "knocks": by_uid})
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
        # The client writes zone, fall and other self-inflicted deaths as
        # "killed by" the victim themselves. They are real deaths, so they
        # still count toward being knocked out, but they are NOT kills:
        # crediting them put every affected squad one ahead of the result
        # file. Excluding them made team kill counts exact -- 84/84 across
        # seven matches, against 73/84 before -- and keeps a nonsense
        # "X eliminated X" line out of the feed.
        self_inflicted = killer_id == victim_id
        if kill:
            live["gsDown"].setdefault(gs_team_of(victim_id), set()).add(victim_id)
        if self_inflicted:
            continue
        if kill:
            team = gs_team_of(killer_id)
            live["gsKills"][team] = live["gsKills"].get(team, 0) + 1
        else:
            # Knockdowns are the only per-player stat the result file does
            # not carry, so they are counted here and travel with the match
            # end for the Booyah card.
            live["playerKnocks"][killer_id] = live["playerKnocks"].get(killer_id, 0) + 1
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
            "killerTeam": gs_team_of(killer_id),
            "victimTeam": gs_team_of(victim_id),
        })
    return events, new_offset, signals


# ---------------------------------------------------------------------------
# Pre-match lobby read.
#
# The lobby is the only place a roster exists BEFORE any game is played --
# the result files that otherwise supply team names, IGNs and UIDs are
# written after a match, so on day one there's nothing else to read.
#
# Reads the two fully-visible squad cards per capture and the operator
# scrolls and captures again. Two rather than four: the lobby shows four
# cards, but the lower pair sits behind the SPECTATOR LIST bar and is
# routinely clipped, so a clipped card would contribute a half-read team
# name and missing players. Results accumulate by team name rather than by
# position, since position means nothing once the list has scrolled.
#
# UIDs aren't shown in the default view at all -- the lobby has its own
# UID toggle that swaps the names for IDs. That makes a UID pass a separate
# capture the operator opts into, pairing by row order within a block.
# ---------------------------------------------------------------------------

# Each card is two boxes: the team name and the player-name column. See
# calibrate.py for why one whole-card box read the tick glyph and MAX
# badges as player names.
LOBBY_CARDS = [
    {"team": "freefire_lobby_block1_team", "players": "freefire_lobby_block1_players"},
    {"team": "freefire_lobby_block2_team", "players": "freefire_lobby_block2_players"},
]
LOBBY_BLOCK_KEYS = [k for card in LOBBY_CARDS for k in card.values()]
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


def is_plausible_lobby_name(text):
    """Rejects OCR fragments that aren't names.

    Needed even with tight boxes: a real capture produced 'v', ']' and 'Vv'
    as separate lines -- the squad leader's tick glyph and the MAX badge
    borders read as text -- and those were being stored as players, pushing
    real names out of the four slots. Requires a couple of alphanumerics
    and rejects strings that are mostly punctuation, which no Free Fire IGN
    is (they lean on letters and digits even when decorated)."""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 3:
        return False
    alnum = sum(c.isalnum() for c in stripped)
    return alnum >= 2 and alnum >= len(stripped) * 0.4


def read_lobby_lines(img_bgr):
    """Cleaned, plausible text lines from a lobby crop, top to bottom."""
    if img_bgr is None or img_bgr.size == 0:
        return []
    lines = ocr_lines(img_bgr)
    ordered = [clean_lobby_line(l["text"]) for l in sorted(lines, key=lambda l: l["cy"])]
    return [t for t in ordered if is_plausible_lobby_name(t)]


def capture_lobby_blocks(regions_cfg, uid_pass=False):
    """Reads both visible lobby cards in one screen grab.

    Team name and players come from separate calibrated boxes, so a card
    with an unreadable name still contributes its players and vice versa --
    with a single box, one bad read took the whole squad with it."""
    cards = []
    with mss.mss() as sct:
        for card in LOBBY_CARDS:
            def crop(key):
                region = regions_cfg.get(key)
                if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                    return None
                return crop_to_bgr(sct, region)

            team_lines = read_lobby_lines(crop(card["team"]))
            player_lines = read_lobby_lines(crop(card["players"]))
            if not team_lines and not player_lines:
                cards.append(None)
                continue
            cards.append({
                "teamName": team_lines[0] if team_lines else "",
                # Capped at five: a stray line bleeding in from the card
                # below shouldn't inflate a squad past its roster slots.
                "players": player_lines[:5],
                "uidPass": uid_pass,
            })
    return cards


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
# The alive grid -- a faster, more precise alternative to both paths above
# for the specific numbers a broadcast sheet or side-table graphic needs
# RIGHT NOW: how many of a team's 4 are alive, and their elim count.
#
# link_live_teams() (the log path) has team identity for free but is
# capped at however long the game client takes to flush its debugger log
# to disk -- measured at roughly 12-14 seconds behind real time. That
# delay lives in the log file itself; no amount of faster reading on this
# end gets around it. parse_sidetable() (the OCR fallback) reads the
# screen every poll, so it isn't delayed the same way, but it identifies
# each row by OCR-reading the team name text, which is measurably less
# reliable than the log path's roster-linked identity.
#
# This drops OCR text-reading entirely. Identity comes from the operator
# picking which roster team sits on which on-screen row (see
# liveOps.aliveRowTeams, set via freefire_set_alive_row_team) -- a manual
# step, but a ONE-TIME one per match rather than a per-poll OCR guess, and
# the row order in Free Fire's own live side table doesn't reshuffle
# mid-match. What's left to read off the screen is purely: is this one
# bar bright or dark (classify_bar -- colour distance, no OCR at all), and
# what's this one number (ocr_small_number -- a tiny digit-only pass).
# Both are cheap enough to run every single poll (0.25s), so the ceiling
# here is the poll interval, not a log-flush delay.
# ---------------------------------------------------------------------------

FREEFIRE_ALIVE_GRID_ROWS = 12


def build_alive_grid(regions, rows=FREEFIRE_ALIVE_GRID_ROWS,
                      players=SIDETABLE_PLAYERS_PER_TEAM):
    """Derives the full ROWS x PLAYERS alive-bar grid, plus one elim-count
    box per row, from the 4 hand-drawn anchor boxes (calibrate.py's
    ff-alive-grid category). Every position past those 4 is a fixed
    offset -- exact, not auto-detected by scanning pixels for bar edges.

    Returns None if any anchor isn't calibrated yet, so callers can fall
    back to the existing log/OCR path without special-casing "half
    calibrated"."""
    r1p1 = regions.get("freefire_alive_r1p1")
    r1p2 = regions.get("freefire_alive_r1p2")
    r2p1 = regions.get("freefire_alive_r2p1")
    r1elim = regions.get("freefire_alive_r1elim")
    if not (r1p1 and r1p2 and r2p1 and r1elim):
        return None

    bar_gap_x = r1p2["x"] - r1p1["x"]
    row_gap_y = r2p1["y"] - r1p1["y"]
    elim_dx = r1elim["x"] - r1p1["x"]
    elim_dy = r1elim["y"] - r1p1["y"]

    grid = []
    for row in range(rows):
        row_y = r1p1["y"] + row * row_gap_y
        bars = [
            {"x": r1p1["x"] + p * bar_gap_x, "y": row_y,
             "w": r1p1["w"], "h": r1p1["h"]}
            for p in range(players)
        ]
        elim_box = {"x": r1p1["x"] + elim_dx, "y": row_y + elim_dy,
                    "w": r1elim["w"], "h": r1elim["h"]}
        grid.append({"bars": bars, "elim": elim_box})
    return grid


def capture_alive_grid_crops(sct, grid):
    """The sync half: crop every box. Cheap (a memory copy per box, same
    as the killfeed/sidetable crops elsewhere in the polling loop), so
    this runs directly on the main loop's own mss instance -- mss isn't
    meant to be shared across threads, so grabbing has to happen here,
    not in the executor."""
    crops = []
    for row in grid:
        bar_crops = [crop_to_bgr(sct, box) for box in row["bars"]]
        elim_crop = crop_to_bgr(sct, row["elim"])
        crops.append((bar_crops, elim_crop))
    return crops


def classify_alive_grid_crops(crops, palette=None):
    """The CPU half: colour-classify each bar (classify_bar -- a mean
    colour distance, negligible cost) and OCR each row's elim number
    (ocr_small_number -- the one real cost here, which is why this whole
    function runs in the executor rather than on the polling loop).
    Returns rows in ON-SCREEN ORDER -- top to bottom, same order every
    poll -- with no team identity attached; see
    apply_alive_grid_identities for where that's joined on."""
    rows_out = []
    for bar_crops, elim_crop in crops:
        bars = [
            classify_bar(c, palette)["status"] if c is not None and c.size else "unknown"
            for c in bar_crops
        ]
        elims = (ocr_small_number(elim_crop)
                 if elim_crop is not None and elim_crop.size else None)
        rows_out.append({
            "bars": bars,
            "aliveCount": sum(1 for b in bars if b == "alive"),
            "elims": elims,
        })
    return rows_out


def apply_alive_grid_identities(grid_rows, row_teams):
    """Joins the position-only grid rows onto the operator's own row ->
    team assignment (liveOps.aliveRowTeams), producing rows in the same
    shape link_live_teams()/parse_sidetable() already produce, so they
    drop straight into liveOps.sidetableRows for the Alive Status overlay
    and the sheet push to consume unchanged.

    A row with no assignment yet is skipped entirely -- returning it with
    a blank name would show up as a real, nameless team on the overlay,
    which is worse than that row simply not being in the grid's
    contribution yet (the caller merges this with the log/OCR fallback,
    so an unassigned row still gets SOMETHING once one of those has it)."""
    rows = []
    for i, row in enumerate(grid_rows):
        team = (row_teams[i] if i < len(row_teams) else "") or ""
        if not team:
            continue
        rows.append({
            "teamName": team,
            "elims": row["elims"],
            "score": row["elims"],
            "bars": row["bars"],
            "barDetail": [{"status": b} for b in row["bars"]],
            "aliveCount": row["aliveCount"],
            "eliminated": row["aliveCount"] == 0,
            "nameRead": True,
            "rawText": team,
            "source": "grid",
        })
    return rows


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

# How many of a squad's ~4 player UIDs have to land on the SAME roster team
# before that identification is trusted outright, no matter what the file's
# TeamName said. Name-based matching alone can't be trusted in a non-league
# lobby: the file's TeamName there is often whatever the room auto-assigned
# rather than anything a roster was ever built around, and it can
# coincidentally look enough like a DIFFERENT registered team's name to clear
# match_roster_team's fuzzy-ratio threshold. A UID can't lie about who's
# playing, so it gets the final word -- see match_roster_team_by_uid and its
# use in apply_roster_overrides.
FREEFIRE_TEAM_UID_MIN_VOTES = 2


def normalize_for_match(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def team_aliases():
    return ((server_state.get("aliases") or {}).get("teams") or {})


def player_aliases():
    return ((server_state.get("aliases") or {}).get("players") or {})


def squad_aliases():
    """IGN -> roster team name, taught when an operator maps a squad the
    matcher could not place. Squad resolution only ever needs to know which
    team a player is on, so this is kept as its own map rather than being
    forced through the roster's UID identity."""
    return ((server_state.get("aliases") or {}).get("squads") or {})


def match_roster_team(file_team_name, roster_teams):
    """Returns the roster team dict this result-file team name refers to,
    or None if nothing matches confidently enough.

    An operator-taught alias is checked first and short-circuits the rest:
    the whole point of teaching one is that the automatic matching got it
    wrong or gave up, so letting the fuzzy pass have another say would
    defeat it."""
    target = normalize_for_match(file_team_name)
    if not target:
        return None

    mapped = team_aliases().get(_ign_key(file_team_name))
    if mapped:
        for team in roster_teams:
            if (team.get("name") or "").strip() == mapped:
                return team

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


def match_roster_team_by_uid(players, roster_teams):
    """The roster team most of these players' UIDs belong to, by vote.

    Returns (team, votes) -- votes is how many of the given players' UIDs
    landed on that team's roster, out of however many were non-blank. A
    file team with no UID overlapping ANY roster team returns (None, 0),
    which callers should treat as "UID has nothing to say here", not as a
    conflict -- that's the normal case for a squad that hasn't been
    registered at all yet."""
    file_uids = {str(p.get("uid") or "").strip() for p in players} - {""}
    if not file_uids:
        return (None, 0)
    best_team, best_votes = None, 0
    for team in roster_teams:
        roster_uids = {str(p.get("uid") or "").strip()
                       for p in team.get("players", []) or []} - {""}
        votes = len(file_uids & roster_uids)
        if votes > best_votes:
            best_team, best_votes = team, votes
    return (best_team, best_votes)


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
        name_team = match_roster_team(file_team_name, roster_teams)
        team_players = team.get("players", []) or []
        uid_team, uid_votes = match_roster_team_by_uid(team_players, roster_teams)
        uid_confident = uid_votes >= FREEFIRE_TEAM_UID_MIN_VOTES

        # UID wins outright once it clears the vote threshold, exactly like
        # match_roster_player already does for individual players -- it
        # can't lie about who's in the lobby. Below that threshold there
        # isn't enough UID evidence to override anything, so a name match
        # (if any) is used as before, but flagged for a manual look rather
        # than trusted quietly -- see needsUidReview below.
        roster_team = uid_team if uid_confident else (name_team or uid_team)

        team["matched"] = roster_team is not None
        team["needsUidReview"] = False
        if uid_confident and name_team is not None and name_team is not uid_team:
            # The two signals actively disagree: the file's own team name
            # points at one roster team while the player UIDs -- which
            # can't be spoofed by an auto-generated lobby name -- point at
            # another. UID is trusted for the actual result, but this is
            # exactly the kind of mix-up that would otherwise put a
            # squad's kills on the wrong team's line in the standings.
            team["needsUidReview"] = True
            team["reviewReason"] = (
                f'Team name matched "{name_team.get("name")}", but '
                f'{uid_votes} player UID(s) say this is actually '
                f'"{uid_team.get("name")}" -- verify before committing.'
            )
        elif not uid_confident and roster_team is not None:
            file_uid_count = len({str(p.get("uid") or "").strip()
                                   for p in team_players} - {""})
            if roster_team is name_team:
                team["needsUidReview"] = True
                team["reviewReason"] = (
                    f'Matched "{roster_team.get("name")}" by team name only -- '
                    f'just {uid_votes} of {file_uid_count} player UID(s) confirmed '
                    f'it. Double-check before committing.'
                )
            else:
                team["needsUidReview"] = True
                team["reviewReason"] = (
                    f'The file\'s team name didn\'t match any roster team, but '
                    f'{uid_votes} of {file_uid_count} player UID(s) suggest this '
                    f'might be "{roster_team.get("name")}" -- verify before committing.'
                )
        if roster_team:
            # displayName is what goes on air; name stays the thing the
            # matcher works against. They are separate so an operator can
            # write "The MVPs" for the graphics without breaking the match
            # against the file's "THE MVPS".
            team["teamName"] = (roster_team.get("displayName")
                                or roster_team.get("name") or file_team_name)
            team["shortName"] = roster_team.get("shortName") or ""
            team["logo"] = roster_team.get("logo") or ""
        for player in team.get("players", []):
            file_player_name = player.get("name", "")
            player["fileName"] = file_player_name
            roster_player = match_roster_player(player, roster_team)
            player["matched"] = roster_player is not None
            if roster_player:
                # Same split as the team above: displayIgn is for air, ign
                # is what the matcher uses, and the UID is untouched by
                # either -- so a player can be shown under a different name
                # without losing their identity across games.
                player["name"] = (roster_player.get("displayIgn")
                                  or roster_player.get("ign") or file_player_name)
                player["photo"] = roster_player.get("photo") or ""
    record_pending_from_result(teams)
    return teams


def record_pending_from_result(teams):
    """Files anything a result import could not resolve for the Mapping tab.

    Merged rather than replaced, and keyed by the source name, so a team
    that failed to match in game 3 is still offered after game 4 imports
    cleanly -- an operator who was busy at the time can come back to it.
    Anything the operator has since taught an alias for drops out."""
    pending = server_state.setdefault(
        "pending", {"teams": [], "players": [], "squads": []})
    known_teams = team_aliases()
    known_players = player_aliases()

    by_name = {t["name"]: t for t in pending.get("teams", []) if t.get("name")}
    by_ign = {p["ign"]: p for p in pending.get("players", []) if p.get("ign")}

    for team in teams:
        source = (team.get("fileTeamName") or "").strip()
        if source and not team.get("matched") and _ign_key(source) not in known_teams:
            by_name[source] = {"name": source, "seenAs": team.get("teamName", "")}
        for player in team.get("players", []) or []:
            ign = (player.get("fileName") or "").strip()
            if ign and not player.get("matched") and _ign_key(ign) not in known_players:
                by_ign[ign] = {
                    "ign": ign,
                    "uid": str(player.get("uid") or ""),
                    "team": source,
                }

    pending["teams"] = sorted(by_name.values(), key=lambda t: t["name"].upper())
    pending["players"] = sorted(by_ign.values(), key=lambda p: p["ign"].upper())


def _ign_key(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def resolve_team_from_igns(igns, roster):
    """The roster team most of these players belong to, or "".

    A squad is identified by who is in it rather than by the number the
    client gave it, because those numbers are reassigned every match."""
    roster_teams = (roster or {}).get("teams", []) or []
    aliases = player_aliases()
    taught = squad_aliases()
    votes = {}
    for index, team in enumerate(roster_teams):
        name = (team.get("name") or "").strip()
        for ign in igns or []:
            if taught.get(_ign_key(ign)) == name:
                votes[index] = votes.get(index, 0) + 1
        keys = {_ign_key(p.get("ign")) for p in (team.get("players") or [])}
        uids = {str(p.get("uid") or "").strip() for p in (team.get("players") or [])}
        for ign in igns or []:
            key = _ign_key(ign)
            # A taught player mapping points at a UID, which is what
            # actually identifies someone across an IGN change.
            if key in keys or aliases.get(key) in uids:
                votes[index] = votes.get(index, 0) + 1
    if not votes:
        return ""
    return (roster_teams[max(votes, key=votes.get)] or {}).get("name") or ""


def link_live_teams(live, roster):
    """Joins the client's two team numbering spaces through the roster.

    TeamID carries the names and the running score. gsTeam carries the
    players, their deaths and the wipe signal. Nothing in the log connects
    the two -- measured on a real match, they agreed for 0 of 12 teams --
    so the roster is used as the meeting point: a gsTeam is identified by
    matching its players' IGNs, a TeamID by matching the name the client
    printed, and where both land on the same roster team they are two
    halves of one squad.

    Returns {"rows": [...], "gsNames": {gsTeam: name}}. Rows keep the shape
    parse_sidetable() produced, so the same consumers work off either
    source and the OCR path stays a drop-in fallback.

    With no roster configured this still returns usable rows -- the names
    and scores come straight from the log -- just without the alive counts,
    which are the half that needs the join."""
    roster_teams = (roster or {}).get("teams", []) or []
    ign_to_team = {}
    for index, team in enumerate(roster_teams):
        for player in team.get("players", []) or []:
            key = _ign_key(player.get("ign"))
            if key:
                ign_to_team[key] = index

    taught = squad_aliases()
    name_to_index = {(t.get("name") or "").strip(): i
                     for i, t in enumerate(roster_teams)}
    gs_to_roster = {}
    for gs_team, players in live.get("gsIgns", {}).items():
        votes = {}
        for ign in players.values():
            index = ign_to_team.get(_ign_key(ign))
            if index is None:
                index = name_to_index.get(taught.get(_ign_key(ign)))
            if index is not None:
                votes[index] = votes.get(index, 0) + 1
        if votes:
            gs_to_roster[gs_team] = max(votes, key=votes.get)

    roster_to_gs = {}
    for gs_team, index in gs_to_roster.items():
        roster_to_gs.setdefault(index, gs_team)

    ended = live.get("ended")
    wiped = live.get("wiped", [])
    rows = []
    for tid, name in sorted(live.get("teamNames", {}).items()):
        roster_team = match_roster_team(name, roster_teams)
        index = roster_teams.index(roster_team) if roster_team in roster_teams else None
        gs_team = roster_to_gs.get(index) if index is not None else None

        bars = []
        alive_count = None
        if gs_team is not None:
            squad = live.get("gsIgns", {}).get(gs_team, {})
            down = live.get("gsDown", {}).get(gs_team, set())
            size = len(squad) or len(((roster_team or {}).get("players")) or []) or 4
            if gs_team in wiped:
                bars = ["dead"] * size
            else:
                bars = ["dead"] * min(len(down), size)
                bars += ["alive"] * (size - len(bars))
            alive_count = sum(1 for b in bars if b == "alive")

        rows.append({
            "teamName": (roster_team or {}).get("name") or name,
            # The client's running score is the kill count DURING the match
            # and gains placement points at the end -- verified, the final
            # value equals the result file's TotalScore 12/12. Publishing it
            # as "elims" after the whistle would silently inflate every
            # team, so it is withheld once the match has ended and the
            # result file (which separates the two) takes over.
            "elims": None if ended else live.get("teamScores", {}).get(tid),
            "score": live.get("teamScores", {}).get(tid),
            "bars": bars,
            "barDetail": [{"status": b} for b in bars],
            "aliveCount": alive_count,
            "eliminated": gs_team in wiped if gs_team is not None else None,
            "placement": (len(live.get("teamNames", {})) - wiped.index(gs_team))
                         if (gs_team is not None and gs_team in wiped) else None,
            "nameRead": True,
            "rawText": name,
            "teamId": tid,
            "gsTeam": gs_team,
            "source": "log",
        })

    gs_names = {}
    for gs_team, index in gs_to_roster.items():
        gs_names[gs_team] = (roster_teams[index] or {}).get("name") or ""

    # Squads present in the match that no roster team claims. These are the
    # ones whose alive count shows as unknown and whose elimination graphic
    # is skipped, so they are exactly what the Mapping tab needs to offer.
    unresolved = []
    for gs_team, players in sorted(live.get("gsIgns", {}).items()):
        if gs_team in gs_to_roster or not players:
            continue
        unresolved.append({
            "gsTeam": gs_team,
            "igns": sorted(players.values()),
            "eliminated": gs_team in wiped,
        })
    return {"rows": rows, "gsNames": gs_names, "unresolved": unresolved}


# ---------------------------------------------------------------------------
# Pushing the live alive/elim side table to an operator's own broadcast
# sheet, replacing a person ticking checkboxes by hand while watching the
# stream with the SAME log-driven data the Alive Status overlay already
# shows -- see link_live_teams above and parse_sidetable's OCR fallback.
#
# The receiving end is a Google Apps Script Web App the operator deploys
# on their own sheet (see freefire_sheet_push.gs alongside this file) --
# deliberately not the Sheets API with a service-account key, which would
# mean managing a credential file with write access to their sheet. A
# script they control, that only accepts whatever shape they wrote it to
# accept, keeps that entirely on their side.
#
# Fire-and-forget: urllib's request call is synchronous, so it runs in the
# OCR thread pool via run_in_executor rather than blocking the polling
# loop, and every failure is swallowed (with a throttled print) rather
# than raised -- a slow or unreachable sheet must never stall live
# capture, which is the one thing this system cannot afford to do.
# ---------------------------------------------------------------------------

_last_sheet_push_error_at = 0.0


def _post_sheet_payload(url, payload):
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


async def push_sidetable_to_sheet(rows):
    global _last_sheet_push_error_at
    url = (server_state.get("settings", {}).get("sheetWebhookUrl") or "").strip()
    if not url or not rows:
        return
    payload = {
        "rows": [
            {
                "team": r.get("teamName") or r.get("rawText") or "",
                "aliveCount": r.get("aliveCount"),
                # elims is withheld once a match ends (see link_live_teams);
                # score holds the same number and stays put after the
                # whistle, so the sheet's count doesn't blank out the
                # instant the match is over.
                "elims": r.get("elims") if r.get("elims") is not None else r.get("score"),
            }
            for r in rows
        ],
    }
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(ocr_executor, _post_sheet_payload, url, payload)
    except Exception as e:
        now = time.time()
        if now - _last_sheet_push_error_at > 10:
            _last_sheet_push_error_at = now
            print(f"[sheet push] couldn't reach the webhook -- {e}")


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


def find_freefire_match_file(folder, match_id):
    """The result file for one specific match, rather than the newest.

    The match-end line in the log names the match that just finished, so
    the auto-fetch can ask for exactly that one instead of hoping the
    newest file on disk is it."""
    folder_path = Path(folder) if folder else None
    if not folder_path or not folder_path.is_dir():
        return (None, None)
    for f in folder_path.iterdir():
        m = FREEFIRE_MATCH_FILENAME_REGEX.match(f.name)
        if m and m.group("match_id") == str(match_id):
            return (f, m)
    return (None, None)


def build_match_result_payload(folder, match_id=None, knocks=None):
    """Reads and resolves a match result into the dashboard's review payload.

    Shared by the operator's Fetch button and the automatic fetch that runs
    when the log reports a match ended, so both deliver an identical
    message and the dashboard needs no idea which one it came from."""
    if match_id:
        file_path, name_match = find_freefire_match_file(folder, match_id)
    else:
        file_path, name_match = find_freefire_latest_match_file(folder)
    if not file_path:
        which = f"for match {match_id}" if match_id else "MatchResult_*.log"
        return {
            "type": "freefire_match_result", "teams": [], "matchId": None,
            "error": f"No result file {which} found in '{folder}'.",
        }
    teams = parse_freefire_match_result(file_path.read_text(encoding="utf-8-sig"))
    # Resolve against the configured roster before the dashboard ever sees
    # it -- see apply_roster_overrides.
    teams = apply_roster_overrides(teams, server_state.get("roster", {}))
    policy = server_state.get("settings", {}).get("unmatchedPolicy", "flag")
    teams = apply_unmatched_policy(teams, policy)
    if knocks:
        for team in teams:
            for player in team.get("players", []) or []:
                player["knocks"] = knocks.get(str(player.get("uid") or ""), 0)
    return {
        "type": "freefire_match_result",
        "matchId": name_match.group("match_id"),
        "timestamp": name_match.group("timestamp"),
        "fileName": file_path.name,
        "gameNumber": server_state.get("event", {}).get("currentGame", 1),
        "teams": teams,
        "unmatchedTeams": [t["fileTeamName"] for t in teams if not t.get("matched")],
        "error": None if teams else "File found but no team blocks could be parsed from it.",
    }


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


def compute_champion_rush(matches, threshold=110):
    """Champion Rush: reach the threshold, then win a game to be crowned.

    Two separate things happen, in order. A team ACTIVATES the moment its
    running total reaches the threshold. A team is CROWNED when it takes a
    Booyah in a game it ENTERED already activated -- which is why the
    points from a match are added only after that match's Booyah has been
    checked. Crossing the line and winning in the same game activates but
    does not crown; the win has to come afterwards, which is the whole
    shape of the format.

    Returns the activation order, who was crowned and in which game, and
    each team's running total, so the dashboard can show how close the
    rest are rather than only naming the leader."""
    totals = {}
    activated = []
    activated_at = {}
    champion, champion_game = "", None

    for index, match in enumerate(matches, 1):
        teams = match.get("teams", []) or []

        if champion == "":
            for team in teams:
                name = (team.get("teamName") or "").strip()
                if name and team.get("rank") == 1 and name in activated_at:
                    champion, champion_game = name, index
                    break

        for team in teams:
            name = (team.get("teamName") or "").strip()
            if not name:
                continue
            totals[name] = totals.get(name, 0) + (team.get("totalScore") or 0)
            if totals[name] >= threshold and name not in activated_at:
                activated_at[name] = index
                activated.append({"teamName": name, "game": index,
                                  "points": totals[name]})

    return {
        "threshold": threshold,
        "activated": activated,
        "champion": champion,
        "championGame": champion_game,
        "totals": [{"teamName": n, "points": p,
                    "activated": n in activated_at,
                    "needs": max(0, threshold - p)}
                   for n, p in sorted(totals.items(), key=lambda kv: -kv[1])],
    }


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


async def broadcast_to_page(page, message):
    """Same as broadcast(), but only to clients connected as `page`.

    Crop previews are read by exactly one thing -- the dashboard's
    calibration panel. Sending them to every connected socket meant every
    OBS overlay browser source was also being fed a couple of megabytes a
    poll it had no handler for and simply dropped. The Valorant engine hit
    this first (see its broadcast_to_page); this is the same fix, for the
    same reason, in the engine that hadn't had it yet.

    The relay socket is ALWAYS included whatever `page` is. From here that
    single connection stands in for however many real dashboard and
    overlay tabs are on the far side of it, and it has no page identity of
    its own, so this engine cannot tell whether dropping it would strand a
    legitimate recipient. Instead the message carries "_target_pages" and
    the relay narrows its own fan-out, where the per-client page identity
    genuinely is known."""
    targets = [c for c, p in connected_pages.items() if p == page]
    if relay_websocket is not None and relay_websocket not in targets:
        targets.append(relay_websocket)
    if not targets:
        return
    data = json.dumps({**message, "_target_pages": [page]})
    await asyncio.gather(*[c.send(data) for c in targets], return_exceptions=True)


def dashboard_connected():
    """Whether it's worth encoding preview images at all this cycle.

    A local socket tagged "freefire_dashboard" is certain. A relay
    connection's own tag is always "unknown", so reachability of the relay
    is the most this engine can establish -- and erring toward "encode it"
    there is the safer trade, since the alternative failure is a
    relay-connected dashboard that silently never shows a preview."""
    return "freefire_dashboard" in connected_pages.values() or relay_websocket is not None


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
_live_match = blank_live_match()


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
    global _debugger_offset, _debugger_path
    connected_clients.add(websocket)
    try:
        query = parse_qs(urlparse(websocket.request.path).query)
        page = (query.get("page") or ["unknown"])[0]
    except Exception:
        page = "unknown"
    connected_pages[websocket] = page
    # A dashboard opening onto a frozen screen would otherwise sit blank,
    # since previews are only sent when the crop changes.
    last_preview_sent.clear()
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
                    server_state["championRush"] = compute_champion_rush(
                        server_state.get("matches", []),
                        server_state.get("settings", {}).get("championRushThreshold", 110),
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
            elif payload.get("type") == "freefire_list_matches":
                # Every result file in the folder, not just the newest --
                # for re-adding an earlier game after it's been cleared
                # from standings (by mistake or on purpose) or missed.
                # Clearing standings never touches these files, so nothing
                # here is actually lost by that action; this is just the
                # way back.
                folder = payload.get("folder") or server_state.get("settings", {}).get("matchResultFolder", "")
                folder_path = Path(folder) if folder else None
                files = []
                if folder_path and folder_path.is_dir():
                    for f in folder_path.iterdir():
                        m = FREEFIRE_MATCH_FILENAME_REGEX.match(f.name)
                        if m:
                            files.append({
                                "matchId": m.group("match_id"),
                                "timestamp": m.group("timestamp"),
                                "fileName": f.name,
                            })
                    files.sort(key=lambda r: r["timestamp"], reverse=True)
                already = {m.get("matchId") for m in server_state.get("matches", [])}
                for f in files:
                    f["committed"] = f["matchId"] in already
                await websocket.send(json.dumps({"type": "freefire_match_list", "files": files}))
            elif payload.get("type") == "freefire_fetch_match":
                # Read-only lookup -- does NOT touch server_state. The
                # dashboard reviews the parsed result and commits it via a
                # normal manual_update (data.freefire.matches) only once
                # the operator confirms it.
                folder = payload.get("folder") or server_state.get("settings", {}).get("matchResultFolder", "")
                try:
                    await websocket.send(json.dumps(
                        build_match_result_payload(folder, payload.get("matchId"))
                    ))
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
            elif payload.get("type") == "freefire_sheet_push_test":
                # Unlike the real push (fire-and-forget, errors swallowed --
                # see push_sidetable_to_sheet), this one is a direct request
                # for a yes/no answer, so it waits for the POST and reports
                # exactly what happened. A fixed, obviously-fake team name
                # rather than real data: the point is confirming the pipe
                # works, not previewing a real row, and it must never look
                # like a genuine result if something upstream goes stale.
                # Tests whatever URL the dashboard has typed in right now
                # (payload["url"]) rather than only whatever was last saved,
                # so Test works before Save has been clicked.
                url = (payload.get("url")
                       or server_state.get("settings", {}).get("sheetWebhookUrl") or "").strip()
                if not url:
                    await websocket.send(json.dumps({
                        "type": "freefire_sheet_push_test_result",
                        "ok": False, "error": "No webhook URL saved yet.",
                    }))
                else:
                    test_payload = {"rows": [
                        {"team": "SHEET PUSH TEST", "aliveCount": 3, "elims": 7},
                    ]}
                    try:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            ocr_executor, _post_sheet_payload, url, test_payload)
                        await websocket.send(json.dumps({
                            "type": "freefire_sheet_push_test_result", "ok": True,
                        }))
                    except Exception as e:
                        await websocket.send(json.dumps({
                            "type": "freefire_sheet_push_test_result",
                            "ok": False, "error": str(e),
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
                # The scoreboard and the points table share one browser
                # source, so pushing either one pulls the other down --
                # on a single source they would otherwise stack.
                server_state["display"]["scoreboardVisible"] = True
                server_state["display"]["pointsTableVisible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "scoreboard_hide":
                server_state["display"]["scoreboardVisible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "points_table_show":
                # See scoreboard_show -- one source, one graphic at a time.
                server_state["display"]["pointsTableVisible"] = True
                server_state["display"]["scoreboardVisible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "points_table_hide":
                server_state["display"]["pointsTableVisible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_clear_matches":
                # Committed matches have no event boundary of their own --
                # compute_freefire_standings sums every match ever
                # committed on this engine, with nothing to say "these
                # eleven are from this afternoon's scrims, not tonight's
                # show." An operator starting a new event needs a way to
                # drop the old ones without touching the roster they just
                # built for the new one, which is why this is its own
                # action rather than folded into the log cutoff above --
                # that one only affects the LIVE feed; committed history
                # is untouched by it.
                #
                # Nothing on disk is lost: the result files still exist,
                # so any of these can be re-fetched and re-committed if
                # cleared by mistake.
                server_state["matches"] = []
                server_state["currentMatchId"] = None
                server_state["standings"] = compute_freefire_standings([])
                server_state["championRush"] = compute_champion_rush(
                    [], server_state.get("settings", {}).get("championRushThreshold", 110))
                save_state()
                print("[live] cleared all committed matches -- standings reset for a new event")
                await broadcast({"type": "state_sync", "data": server_state,
                                 "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_debugger_cutoff":
                # Everything derived from the log is per-match and rebuilt
                # from it, so moving the cutoff has to drop what was
                # derived under the old one -- otherwise the previous
                # event's squads and scores sit in the live table until the
                # next match happens to overwrite them.
                cutoff = (payload.get("startAt") or "").strip()
                server_state.setdefault("settings", {})["debuggerStartAt"] = cutoff
                _live_match.clear()
                _live_match.update(blank_live_match())
                # Re-read from the top: the cutoff may have moved BACK, and
                # the lines it now admits are behind the current offset.
                # The catch-up pass this triggers is silent, so nothing
                # from the newly-admitted stretch goes on air.
                _debugger_path = None
                _debugger_offset = 0
                server_state["liveOps"]["sidetableRows"] = []
                server_state["liveOps"]["killEvents"] = []
                server_state["pending"]["squads"] = []
                save_state()
                print(f"[live] ignoring log entries before {cutoff or 'the start of the file'}")
                await broadcast({"type": "state_sync", "data": server_state,
                                 "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_asset_name":
                # Name one icon. Written straight through to names.json as
                # well as the state, so the naming lives beside the artwork
                # and a machine that has never run this engine still has it.
                asset_id = str(payload.get("assetId") or "").strip()
                if asset_id:
                    names = server_state.setdefault("assetNames", {})
                    name = (payload.get("name") or "").strip()
                    if name:
                        names[asset_id] = name
                    else:
                        names.pop(asset_id, None)
                    save_asset_names(names)
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state,
                                     "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_fullsize_name":
                asset_id = str(payload.get("assetId") or "").strip()
                if asset_id:
                    names = server_state.setdefault("fullSizeNames", {})
                    name = (payload.get("name") or "").strip()
                    if name:
                        names[asset_id] = name
                    else:
                        names.pop(asset_id, None)
                    save_fullsize_names(names)
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state,
                                     "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_set_character":
                # Manual override for a slot the Num5 capture never got.
                # Recorded as manual so the dashboard can show which cards
                # were chosen by hand rather than identified, and so a later
                # real capture is visibly replacing a human decision.
                uid = str(payload.get("uid") or "").strip()
                game = str(payload.get("gameNumber") or "")
                slot = payload.get("slot") or "active"
                label = (payload.get("label") or "").strip()
                target = None
                for team in (server_state.get("roster") or {}).get("teams", []) or []:
                    for player in team.get("players", []) or []:
                        if str(player.get("uid") or "").strip() == uid:
                            target = player
                            break
                    if target:
                        break
                if target is not None and game:
                    entry = target.setdefault("loadouts", {}).setdefault(game, {})
                    slots = entry.setdefault("slots", {})
                    if label:
                        slots[slot] = {"label": label, "confidence": 1.0,
                                       "lowConfidence": False, "manual": True}
                    else:
                        slots.pop(slot, None)
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state,
                                     "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_map_alias":
                # Teach (or, with a blank target, forget) one mapping. Done
                # as its own message rather than a manual_update so the
                # matching pending entry is dropped in the same step -- a
                # taught name that stayed on the to-do list would keep
                # asking to be mapped forever.
                kind = payload.get("kind")
                source = (payload.get("source") or "").strip()
                target = (payload.get("target") or "").strip()
                if kind in ("teams", "players", "squads") and source:
                    aliases = server_state.setdefault(
                        "aliases", {"teams": {}, "players": {}, "squads": {}})
                    table = aliases.setdefault(kind, {})
                    if target:
                        table[_ign_key(source)] = target
                    else:
                        table.pop(_ign_key(source), None)

                    pending = server_state.setdefault(
                        "pending", {"teams": [], "players": [], "squads": []})
                    if kind == "teams":
                        pending["teams"] = [t for t in pending.get("teams", [])
                                            if _ign_key(t.get("name")) != _ign_key(source)]
                    elif kind == "players":
                        pending["players"] = [p for p in pending.get("players", [])
                                              if _ign_key(p.get("ign")) != _ign_key(source)]
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state,
                                     "locked": list(locked_fields)})
            elif payload.get("type") in ("champion_badge_show", "champion_badge_hide"):
                server_state["display"]["championRushBadgeVisible"] = (
                    payload["type"] == "champion_badge_show")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") in ("booyah_stats_show", "booyah_stats_hide"):
                server_state["display"]["booyahStatsVisible"] = (
                    payload["type"] == "booyah_stats_show")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") in ("booyah_loadout_show", "booyah_loadout_hide"):
                server_state["display"]["booyahLoadoutVisible"] = (
                    payload["type"] == "booyah_loadout_show")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") in ("mvp_show", "mvp_hide"):
                server_state["display"]["mvpVisible"] = (
                    payload["type"] == "mvp_show")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") in ("game_summary_show", "game_summary_hide"):
                server_state["display"]["gameSummaryVisible"] = (
                    payload["type"] == "game_summary_show")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") in ("damage_report_show", "damage_report_hide"):
                server_state["display"]["damageReportVisible"] = (
                    payload["type"] == "damage_report_show")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") in ("alive_status_show", "alive_status_hide"):
                server_state["display"]["aliveStatusVisible"] = (
                    payload["type"] == "alive_status_show")
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
            elif payload.get("type") == "freefire_set_alive_row_team":
                # Targeted mutation of ONE slot in aliveRowTeams, not a
                # manual_update -- liveOps also holds killEvents and
                # sidetableRows, which change many times a second during a
                # fight, and a client pushing a whole replacement liveOps
                # object here could clobber those with a stale copy of its
                # own. See build_alive_grid/apply_alive_grid_identities.
                try:
                    row = int(payload.get("row", -1))
                except (TypeError, ValueError):
                    row = -1
                rows = server_state["liveOps"].setdefault("aliveRowTeams", [""] * FREEFIRE_ALIVE_GRID_ROWS)
                if 0 <= row < len(rows):
                    rows[row] = (payload.get("team") or "").strip()
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
                try:
                    te["kills"] = int(payload.get("kills") or 0)
                except (TypeError, ValueError):
                    te["kills"] = 0
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


# Set when the log says a match ended, cleared once its result file has
# been read. The file lands in the same second as the line across every
# match checked, but "same second" isn't "already flushed", so the fetch
# retries rather than giving up on the first miss.
_pending_match_fetch = None
_pending_match_since = None  # time.time() when the match_end signal arrived
_pending_match_tries = 0     # attempts made, for the log line only
# Knockdowns for the match now waiting to be fetched. Snapshotted at the
# match-end line rather than read later: the live state rolls over to the
# next match as soon as its first line arrives, and the fetch may still be
# retrying by then.
_pending_match_knocks = {}
# A TIME budget, not a tick count. This used to be a fixed 15 polls, which
# quietly meant 15 real seconds at the old 1-second poll interval -- and
# silently dropped to 3.75 seconds the moment the loop was sped up to run
# 4x/sec, well under how long the result file can actually take to settle
# on disk under real conditions (antivirus scanning, a busy disk during a
# live show). That was reported as "auto-fetch stopped working, had to
# press Fetch myself" -- it hadn't stopped, it was just giving up before
# the file existed. Tied to wall-clock time instead, so it stays correct
# regardless of how fast the loop polls.
MAX_AUTO_FETCH_SECONDS = 30


async def handle_live_signals(signals, gs_names):
    """Acts on what the log just reported. Returns whether state changed.

    Both behaviours here are opt-out via settings, because they put things
    on air by themselves: an operator who wants to call the elimination
    graphic manually should not have the engine doing it underneath them."""
    global _pending_match_fetch, _pending_match_since, _pending_match_tries, _pending_match_knocks
    settings = server_state.get("settings", {})
    changed = False

    for signal in signals:
        if signal["type"] == "team_wiped" and settings.get("autoTeamEliminated", True):
            name = resolve_team_from_igns(signal.get("igns"), server_state.get("roster", {}))
            if not name:
                name = gs_names.get(signal["gsTeam"], "")
            if not name:
                # Unresolved squad -- the roster is what turns a gsTeam
                # number into a name, so with no roster loaded there is
                # nothing to put on the graphic. Better to skip than to
                # broadcast "Team 7".
                continue
            roster_team = match_roster_team(name, (server_state.get("roster") or {}).get("teams", []))
            total = signal.get("total") or len(_live_match.get("teamNames", {})) or 12
            te = server_state["teamEliminated"]
            te["status"] = "shown"
            te["shownUntil"] = int(time.time() * 1000) + TEAM_ELIMINATED_DISPLAY_SECONDS * 1000
            te["teamName"] = name
            te["rank"] = total - signal["order"] + 1
            te["kills"] = signal.get("kills", 0)
            te["photo"] = (roster_team or {}).get("logo") or ""
            changed = True
            print(f"[live] {name} eliminated -- placing {te['rank']}")

        elif signal["type"] == "match_end":
            _pending_match_fetch = signal["matchId"]
            _pending_match_since = time.time()
            _pending_match_knocks = signal.get("knocks") or {}
            _pending_match_tries = 0
            print(f"[live] match {signal['matchId']} ended")

    if _pending_match_fetch and settings.get("autoFetchOnMatchEnd", True):
        folder = settings.get("matchResultFolder", "")
        payload = build_match_result_payload(folder, _pending_match_fetch,
                                             _pending_match_knocks)
        _pending_match_tries += 1
        if payload.get("teams"):
            payload["auto"] = True
            await broadcast_to_page("freefire_dashboard", payload)
            print(f"[live] auto-fetched {payload['fileName']} "
                  f"({len(payload['teams'])} teams) -- review and commit")
            _pending_match_fetch = None
        elif time.time() - _pending_match_since >= MAX_AUTO_FETCH_SECONDS:
            print(f"[live] gave up auto-fetching match {_pending_match_fetch} "
                  f"after {_pending_match_tries} attempts over "
                  f"{MAX_AUTO_FETCH_SECONDS}s: {payload.get('error')}")
            _pending_match_fetch = None

    return changed


async def ocr_loop():
    """Only captures the two Free Fire live-ops regions -- there's no
    numeric HUD pipeline here at all, unlike ocr_engine.py's REGION_ORDER
    loop, since none of that applies to Free Fire."""
    # 0.25s (4/sec), not the 1.0s this used to default to. Measured
    # against the real calibrated setup with OCR out of the loop (see the
    # comment above the OCR gating below): finding the log, tailing it,
    # joining the roster, and serializing the state_sync payload together
    # cost ~4ms -- under 2% of even a 0.25s budget. The 1.0s figure dated
    # back to when OCR ran every tick and needed the room; nothing left
    # in the normal case needs it anymore.
    interval = config.get("poll_interval_seconds", 0.25)
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

            alive_grid = build_alive_grid(regions)
            alive_grid_crops = capture_alive_grid_crops(sct, alive_grid) if alive_grid else None

            changed = False
            log_sidetable_rows = None
            killfeed_raw_text = None
            sidetable_raw_text = None

            # Live kill feed from the client's debugger log. Cheap (a
            # forward read of whatever was appended since last poll), so it
            # runs every cycle rather than on the slower OCR cadence. Moved
            # to run BEFORE the OCR block below (it used to run after) so
            # this tick already knows whether the log covered the kill feed
            # and side table before deciding whether OCR needs to run at
            # all -- see the note on ocr_tasks below for why that matters.
            global _debugger_offset, _debugger_path, _debugger_id_map
            debugger_folder = (server_state.get("settings", {}).get("debuggerFolder")
                               or server_state.get("settings", {}).get("matchResultFolder", ""))
            if debugger_folder:
                candidate = find_latest_debugger_log(
                    Path(debugger_folder) / "Debugger"
                ) or find_latest_debugger_log(debugger_folder)
                if candidate is not None:
                    catching_up = False
                    if candidate != _debugger_path:
                        # New log file. Start from the top so the join lines
                        # that build the id mapping are picked up; without
                        # them every later kill line resolves to blank names.
                        #
                        # But everything already in the file has ALREADY
                        # happened. On a restart mid-event that is the whole
                        # session -- seven matches, seventy-seven squad
                        # eliminations -- and with the graphic firing
                        # automatically that would put every one of them on
                        # air in a burst. So the catch-up pass is silent:
                        # same parsing, same state, no signals.
                        _debugger_path = candidate
                        _debugger_offset = 0
                        _debugger_id_map = {}
                        _live_match.clear()
                        _live_match.update(blank_live_match())
                        catching_up = True
                    new_events, _debugger_offset, signals = await loop.run_in_executor(
                        ocr_executor, read_debugger_events,
                        candidate, _debugger_offset, _debugger_id_map, _live_match,
                        not catching_up,
                    )
                    if catching_up:
                        # The feed is history too -- it would flood the
                        # dashboard with a session's worth of old kills.
                        new_events = []
                        print(f"[live] caught up on {candidate.name} "
                              f"({_debugger_offset:,} bytes) -- watching from here")
                    if new_events:
                        feed = server_state["liveOps"].get("killEvents") or []
                        feed = (feed + new_events)[-MAX_KILL_EVENTS:]
                        server_state["liveOps"]["killEvents"] = feed
                        changed = True

                    # The 12-team side table, straight from the client's own
                    # narration rather than read back off the screen.
                    linked = link_live_teams(_live_match, server_state.get("roster", {}))
                    if linked["rows"]:
                        log_sidetable_rows = linked["rows"]
                        if linked["rows"] != server_state["liveOps"].get("sidetableRows"):
                            server_state["liveOps"]["sidetableRows"] = linked["rows"]
                            server_state["liveOps"]["sidetableSource"] = "log"
                            changed = True
                            asyncio.create_task(push_sidetable_to_sheet(linked["rows"]))

                    squads = linked.get("unresolved", [])
                    if squads != server_state["pending"].get("squads"):
                        server_state["pending"]["squads"] = squads
                        changed = True

                    if await handle_live_signals(signals, linked["gsNames"]):
                        changed = True

            # Raw OCR text, for the dashboard's own "Last OCR reading"
            # debug panel -- NOT for the actual broadcast graphics anymore.
            # A debugger folder makes the kill feed here fully redundant
            # (it's a straight text dump of the same feed the log already
            # reads structured, with real names, headshot flags and UIDs),
            # and the log-driven side table just proved out above makes the
            # sidetable OCR pass redundant too, for THIS tick. Skipping both
            # when the log already covers them was the actual fix for "the
            # alive table isn't fast" -- two Tesseract passes (~0.2s and
            # ~0.35s measured) were running on every single 1-second poll
            # for output nothing downstream depended on any more, competing
            # for the same executor threads as the log tailing and the
            # structured fallback parse below. Still runs as a genuine
            # fallback when there's no debugger folder configured at all,
            # or (for the side table specifically) before a roster exists
            # to link a squad to a name.
            ocr_tasks = []
            text_region_names = []
            if killfeed_crop is not None and not debugger_folder:
                ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_text, killfeed_crop))
                text_region_names.append("killfeed")
            if sidetable_crop is not None and not log_sidetable_rows:
                ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_text, sidetable_crop))
                text_region_names.append("sidetable")

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

            # Structured side-table parse -- FALLBACK ONLY, for whenever the
            # log-driven table above didn't have rows to give this tick (no
            # debugger folder configured, or the roster can't yet link a
            # squad to a name). It used to run unconditionally and
            # overwrite the log-driven table on every single poll -- since
            # this ran strictly after that block, its uncalibrated,
            # OCR-derived rows (with the known-bad grey "alive" colour)
            # were clobbering the correct data every cycle it ran, which
            # was every cycle the side table region was calibrated. The
            # Live Alive Status overlay reads exactly this field, so this
            # was silently overriding good data with bad the whole time
            # the log-driven table has existed. Column/colour calibration
            # for this path is no longer expected to be done -- the log
            # table needs neither -- so this now only matters as a safety
            # net before a roster exists to link squads against.
            if sidetable_crop is not None and not log_sidetable_rows:
                parsed = await loop.run_in_executor(
                    ocr_executor, parse_sidetable, sidetable_crop,
                    config.get("sidetable_columns"), config.get("sidetable_colors"),
                )
                ff_live = server_state["liveOps"]
                if parsed["rows"] != ff_live.get("sidetableRows"):
                    ff_live["sidetableRows"] = parsed["rows"]
                    ff_live["sidetableUsedPalette"] = parsed["usedPalette"]
                    ff_live["sidetableSource"] = "ocr"
                    asyncio.create_task(push_sidetable_to_sheet(parsed["rows"]))
                    changed = True

            # The alive grid, once calibrated -- see the block comment
            # above capture_alive_grid_crops. Runs AFTER both the log
            # path and the OCR fallback above so it always gets the last
            # word for any row it has an operator-assigned identity for:
            # both of those overwrite sidetableRows wholesale, and this
            # merges on top rather than being overwritten in turn. A team
            # not yet assigned a row keeps whatever the log/OCR path
            # already gave it -- this only ever adds or upgrades data,
            # never removes a row the other paths are still supplying.
            if alive_grid_crops is not None:
                grid_rows = await loop.run_in_executor(
                    ocr_executor, classify_alive_grid_crops, alive_grid_crops,
                    config.get("sidetable_colors"),
                )
                row_teams = server_state["liveOps"].get("aliveRowTeams") or []
                grid_named_rows = apply_alive_grid_identities(grid_rows, row_teams)
                if grid_named_rows:
                    by_name = {r["teamName"]: r for r in grid_named_rows}
                    base = server_state["liveOps"].get("sidetableRows") or []
                    merged, seen = [], set()
                    for r in base:
                        name = r.get("teamName")
                        if name in by_name:
                            merged.append(by_name[name])
                            seen.add(name)
                        else:
                            merged.append(r)
                    for name, r in by_name.items():
                        if name not in seen:
                            merged.append(r)
                    if merged != server_state["liveOps"].get("sidetableRows"):
                        server_state["liveOps"]["sidetableRows"] = merged
                        server_state["liveOps"]["sidetableSource"] = "grid"
                        changed = True
                        asyncio.create_task(push_sidetable_to_sheet(merged))

            # Previews go to the dashboard only, and only when the crop
            # actually changed since the last one sent. Both guards are
            # about latency, not tidiness: this block was pushing megabytes
            # a poll into the relay connection, and every dashboard request
            # -- fetch match, commit, capture lobby -- waited its turn
            # behind that queue.
            if frame_counter % 2 == 0 and dashboard_connected():
                for region_key, crop, raw_text in (
                    (FREEFIRE_KILLFEED_REGION_KEY, killfeed_crop, killfeed_raw_text),
                    (FREEFIRE_SIDETABLE_REGION_KEY, sidetable_crop, sidetable_raw_text),
                ):
                    if crop is None:
                        continue
                    data_url = crop_to_data_url(crop, jpeg_quality=PREVIEW_JPEG_QUALITY)
                    if not data_url or last_preview_sent.get(region_key) == data_url:
                        continue
                    last_preview_sent[region_key] = data_url
                    await broadcast_to_page("freefire_dashboard", {
                        "type": "crop_preview", "region": region_key,
                        "image": data_url, "text": raw_text or "",
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
    global relay_websocket
    while True:
        try:
            async with websockets.connect(connect_url) as relay_ws:
                print(f"Connected to cloud relay at {url}")
                # Recorded so broadcast_to_page() can always include it --
                # it is the only client whose page= tag says nothing about
                # who is really on the other end.
                relay_websocket = relay_ws
                try:
                    await handle_client(relay_ws)
                finally:
                    relay_websocket = None
        except Exception as e:
            relay_websocket = None
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
