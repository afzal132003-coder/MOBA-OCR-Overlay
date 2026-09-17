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
import os
import socket
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
import mss

# mss 10 renamed the entry point and warns on every mss.mss() call. The
# old name still works, so this is cosmetic -- but it fires from four
# places on startup and buries anything worth reading in the log.
mss_grabber = getattr(mss, "MSS", None) or mss.mss
import pytesseract
import websockets
import keyboard


# --- DNS that survives a bad minute ------------------------------------
_dns_cache = {}
_real_getaddrinfo = socket.getaddrinfo

# Remembered across restarts, because the failure that prompted this
# happened AT STARTUP -- the engine came up, could not resolve the relay,
# and an in-memory cache is empty at exactly that moment. Kept beside the
# relay token, and out of git for the same reason it is not interesting
# to anyone else: it describes one machine's network, not the project.
_DNS_CACHE_FILE = Path(__file__).resolve().parent.parent / ".dns_cache.json"


def _load_dns_cache():
    try:
        raw = json.loads(_DNS_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for key, entries in raw.items():
        host, _, port = key.rpartition("|")
        try:
            _dns_cache[(host, int(port))] = [
                (fam, typ, proto, canon, tuple(addr))
                for fam, typ, proto, canon, addr in entries
            ]
        except (TypeError, ValueError):
            continue


def _save_dns_cache():
    """Best effort, and never fatal.

    Catching everything rather than just OSError on purpose: this runs
    inside getaddrinfo, so anything raised here would come back out of
    every name lookup the engine makes. A cache that cannot be written is
    worth exactly nothing and must cost exactly nothing -- resolution has
    already succeeded by the time we get here."""
    try:
        _DNS_CACHE_FILE.write_text(json.dumps({
            f"{host}|{port}": [
                [fam, typ, proto, canon, list(addr)]
                for fam, typ, proto, canon, addr in entries
            ]
            for (host, port), entries in _dns_cache.items()
        }), encoding="utf-8")
    except Exception:
        pass


def _getaddrinfo_with_fallback(host, port, *args, **kwargs):
    """Uses the last good answer when a lookup fails outright.

    The relay is reached by NAME, because the TLS certificate is issued to
    the name -- so every reconnect is a fresh DNS lookup, and a free
    dynamic-DNS service having a bad minute takes the engine off air
    exactly as if the server were down. Seen live:

        Relay connection lost/failed ([Errno 11002] getaddrinfo failed)

    11002 is WSATRY_AGAIN: not "no such host", but "ask me later". The
    address had not changed and the relay was up the whole time.

    Only ever a FALLBACK. A successful lookup always wins and replaces
    what is remembered, so an address that genuinely changes is picked up
    the moment DNS can answer again -- this buys resilience without
    pinning anything to a stale IP.

    Patched at the socket layer rather than around the relay connect,
    because asyncio resolves through here too, and so does the sheet
    push: one shim covers every outbound call the engine makes.
    """
    key = (host, port)
    try:
        result = _real_getaddrinfo(host, port, *args, **kwargs)
        if result and _dns_cache.get(key) != result:
            _dns_cache[key] = result
            _save_dns_cache()
        return result
    except socket.gaierror:
        cached = _dns_cache.get(key)
        if not cached:
            raise
        print(f"[dns] {host} didn't resolve -- using the last known address")
        return cached


_load_dns_cache()
socket.getaddrinfo = _getaddrinfo_with_fallback

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


def asset_packages():
    """Every graphics package sitting in overlay/assets/freefire/.

    Scanned rather than listed, because the dashboard's package field
    used to suggest exactly one name -- CLT, hard-coded -- so a package
    that had been dropped into the folder was invisible unless somebody
    remembered to type it exactly. Rebuilt each start for the same reason
    the icon catalogue is: artwork arrives between sessions."""
    root = Path(__file__).parent.parent.parent / "overlay" / "assets" / "freefire"
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))


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
# Everything the engine must not do on the event loop goes through here:
# OCR, the alive-grid read, tailing the debugger log, the Google Sheets
# carry fetch, the sheet push, loadout capture, and encoding crop
# previews for the dashboard.
#
# Two workers could not carry that. The sheet calls alone block for up to
# 10 and 15 seconds, so one slow response took half the pool and two took
# all of it -- and every OCR call in the poll loop then queued behind
# them. Measured live: a loop meant to run four times a second was
# broadcasting once every 4.2 seconds, and a command sent to the engine
# came back after 13.5 seconds or not at all. Elimination ticks were
# landing whenever the pool happened to free up.
#
# Six is not about parallelism -- most of these are waiting on a socket
# or a disk, not on a core. It is about a slow sheet not being able to
# stop the graphic.
ocr_executor = ThreadPoolExecutor(max_workers=6)


def default_state():
    return {
        "settings": {
            "matchResultFolder": "", "safezoneFolder": "",
            # Off by default: see push_sidetable_to_sheet.
            "liveSheetPush": False,
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
            # Hold an elimination off air until the operator confirms it.
            # A squad wipe is the one reading where being wrong is most
            # visible -- the graphic fires, the team sinks down the
            # table, and there's no taking it back. With this on, the
            # detection is shown in the dashboard and nothing reaches
            # air until it's ticked. Off = the previous behaviour, fire
            # as soon as it's detected, for when nobody is watching the
            # dashboard.
            "eliminationApproval": True,
            # The RESULTS spreadsheet's LIVE tab, which already totals the
            # series: team name in C, and Booyah / MP / PP / KP / TP across
            # D..H. The engine reads TP from there rather than adding the
            # matches up again, so the graphic and the sheet can never
            # disagree about a team's total -- the sheet is the one doing
            # the sums and this just reads the answer. Blank id disables
            # the whole thing.
            "livePointsSheetId": "1O6_lIfDB-O7vX50wHmMxii-57ExD3Nxb5KaxjlG01jA",
            "livePointsTab": "LIVE",
            # How long the elimination card holds on screen once it has
            # flown in, in seconds. The fly in and fly out are on top of
            # this -- it is the time the card is actually READABLE, which
            # is what an operator is judging when they change it.
            "elimCardSeconds": 4,
        },
        # Filled in at startup with the engine's own folder -- see the
        # assignment after load_state(). Present here so an old state
        # file still merges cleanly.
        "enginePath": "",
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
                    # Some graphics packages ship the alive table twice:
                    # once with a running-points column and once without.
                    # This picks which of the two airs. Packages with only
                    # one version ignore it entirely.
                    "aliveStatusPoints": False,
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
            # Manual corrections on top of the auto-read alive grid, for
            # whenever a colour glow, a stray effect, or anything else
            # makes one reading wrong for a moment -- an operator watching
            # the real game always outranks an automated read on a live
            # broadcast. Keyed by string index rather than nested arrays
            # so "no override" is simply "key absent", with nothing to
            # pre-size or leave as null. bars: "{row}_{bar}" -> "alive" or
            # "eliminated". elims: "{row}" -> the forced count. See
            # apply_alive_grid_overrides.
            "aliveGridOverrides": {"bars": {}, "elims": {}},
            # Eliminations detected but not yet confirmed by the
            # operator, and the ones that have been. See
            # gate_eliminations: while a wipe is pending, the table
            # keeps showing that squad as it last was, so nothing
            # reaches air ahead of the tick.
            "pendingEliminations": [],
            "approvedEliminations": [],
            # Operator-set markers on a squad, keyed by UPPERCASED team
            # name: {"TSG ARMY": {"zone": true, "fire": false}}. Keyed by
            # team rather than by row because the table re-sorts itself
            # constantly -- a mark belongs to the squad, not to whatever
            # slot it happens to occupy. Neither is detected: being in
            # the zone or on a hot streak is a judgement call, so it's a
            # tick the operator makes.
            "teamMarks": {},
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
# Filled in once the icon helpers below are defined -- see the call after
# match_icon's section. Declared here so an old state file that predates
# it still has the key.
server_state.setdefault("loadoutLibraries", {})
# Where this engine is actually running from, so the dashboard can build
# a calibration command that works on THIS machine. Set every start
# rather than saved: a state file copied between machines (or a repo
# moved) would otherwise hand out a path that no longer exists. The
# dashboard is served from Vercel and has no other way to know it.
server_state["enginePath"] = str(Path(__file__).resolve().parent)
# Rebuilt every start rather than trusted from the saved state: the
# operator can drop new artwork into assets/FFM between sessions, and a
# stale list would hide it.
server_state["assetCatalogue"] = asset_catalogue()
server_state["assetPackages"] = asset_packages()
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
    with mss_grabber() as sct:
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

# Default palette for the alive grid specifically (config's own
# alive_grid_colors overrides this when set -- see build_alive_grid_preview
# and its use in the polling loop). Estimated from reference swatches the
# operator shared directly: a vivid saturated orange/gold for a living
# player, a muted, desaturated brownish-khaki -- NOT black or dark grey --
# for an eliminated one, the same colour whether it's one player down or
# the whole squad wiped. Deliberately its own key, not the pre-existing
# sidetable_colors: that one was calibrated for the old whole-region OCR
# fallback and turned out to have "alive" and "knocked" swapped relative
# to what these bars actually look like -- reusing it here would have
# miscounted every alive player as knocked.
DEFAULT_ALIVE_GRID_PALETTE = {
    "alive": [35, 160, 240],        # BGR -- vivid orange/gold
    "eliminated": [85, 125, 150],   # BGR -- muted brownish-khaki
}


# The blue-zone glow that sweeps the table. Pixels in this hue band are
# the effect, not the bar, and are dropped before anything is measured.
GLOW_HUE = (85, 140)
GLOW_MIN_SAT = 80


def classify_alive_bar(patch_bgr, palette=None):
    """One alive bar -> "alive" / "eliminated" / "unknown".

    Nearest-reference matching, same as before -- that part was never
    wrong. In game the two bars share a hue (both ~18) and differ in
    saturation and brightness: alive is S218/V240, eliminated is
    S110/V150. Comparing against both references separates that
    cleanly.

    What broke it was averaging the WHOLE patch first. Blue is roughly
    complementary to the bar's orange, so the blue-zone glow blending
    over a live bar desaturates it toward exactly what the eliminated
    bar looks like -- measured, ~40% coverage flips it, showing a live
    squad as wiped.

    So the glow is removed BEFORE the average, by hue, and the average
    is taken over what remains. The fix is subtraction, not a different
    discriminator.

    (An earlier attempt replaced the discriminator with hue/saturation
    thresholds instead. Those were read off the OVERLAY ARTWORK -- pure
    gold at S255, neutral grey at S0 -- which is a different thing
    entirely from the in-game bar this looks at. The in-game
    "eliminated" bar is a muted khaki at S110, which sat inside the
    "gold" band, so every dead bar read as alive. Hence the explicit
    numbers above: they're the IN-GAME colours.)

    Returns "unknown" when the glow leaves too little to judge, since a
    gap is worth more than a guess."""
    if patch_bgr is None or patch_bgr.size == 0:
        return "unknown"
    palette = palette or DEFAULT_ALIVE_GRID_PALETTE

    hsv = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2HSV)
    h, s = hsv[:, :, 0], hsv[:, :, 1]
    glow = (h >= GLOW_HUE[0]) & (h <= GLOW_HUE[1]) & (s >= GLOW_MIN_SAT)

    keep = ~glow
    total = patch_bgr.shape[0] * patch_bgr.shape[1]
    if int(keep.sum()) < max(4, total * 0.15):
        return "unknown"          # almost entirely glow this frame

    # Every remaining pixel votes for its own nearest reference, and the
    # majority wins. Averaging first is what a partial glow defeats: a
    # half-covered bar averages to something between the two references
    # and lands on the wrong side, even though most of its pixels are
    # still unambiguously one colour. Hue-masking alone can't rescue
    # that either -- a pixel PART way blended sits at hue 22-89 with low
    # saturation, well outside the glow band, so it survives the mask
    # and drags the mean anyway. Voting makes partial coverage cost
    # proportionally instead of catastrophically.
    pixels = patch_bgr[keep].reshape(-1, 3).astype(np.float32)
    statuses = list(palette.keys())
    refs = np.array([palette[k] for k in statuses], dtype=np.float32)
    distances = np.linalg.norm(pixels[:, None, :] - refs[None, :, :], axis=2)
    votes = np.bincount(distances.argmin(axis=1), minlength=len(statuses))
    return statuses[int(votes.argmax())]


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


def _normalize_polarity(binary_img):
    """Tesseract wants dark text on a light background. A threshold pass
    can land on either polarity depending on which side of the crop
    happens to be the majority, so this flips it back whenever the
    result came out mostly black -- the correct case is a small dark
    digit on a large light field, never the reverse."""
    return cv2.bitwise_not(binary_img) if binary_img.mean() < 127 else binary_img


def ocr_small_number(img_bgr, upscale=4):
    """Reads a small standalone number (an elim count).

    Needed because the general sparse-text pass used for the rest of the
    table demonstrably misses these: on a mock-up of the real layout it
    found the two-digit "10" but silently dropped every single-digit
    count, which is a whole column of zeros that look like real data.

    Tries a few different ways of turning the crop into clean black-on-
    white before handing it to Tesseract, in order, and returns the
    first one that actually reads a digit -- a real capture behind a
    semi-transparent panel (background scenery, particle effects,
    colour glows) doesn't threshold reliably one way every time, and the
    common case (a clean, high-contrast crop) still only costs the one
    fast attempt, since this stops as soon as something reads.

      1. A pure-white colour mask -- the digit itself is confirmed white
         text, so checking that ALL THREE channels are simultaneously
         bright isolates it from a background element that's merely
         bright in one channel (the alive bars' orange, an occasional
         blue glow sweep) without being anywhere near true white.
      2. Otsu's threshold on a lightly blurred crop -- the original
         approach, kept as a fallback for whenever the digit isn't pure
         white (a colour cast from a glow effect, say).
      3. Adaptive threshold -- copes with uneven background brightness
         across the crop better than Otsu's single global cutoff can.

    Same overall approach the Valorant engine needed for its own
    small-digit cells, extended with the colour-mask pass once this
    project's own reference swatches confirmed the digit's actual
    colour rather than assuming it."""
    if img_bgr is None or img_bgr.size == 0:
        return None
    big = cv2.resize(img_bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)

    white_mask = cv2.inRange(big, (200, 200, 200), (255, 255, 255))
    blurred = cv2.medianBlur(gray, 3)
    _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)

    candidates = [
        _normalize_polarity(cv2.bitwise_not(white_mask)),
        _normalize_polarity(otsu),
        _normalize_polarity(adaptive),
    ]

    for variant in candidates:
        bordered = cv2.copyMakeBorder(variant, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
        text = pytesseract.image_to_string(bordered, config=TESS_CONFIG_DIGITS).strip()
        match = re.search(r"\d{1,3}", text)
        if match:
            return int(match.group())
    return None


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

    The vertical pitch comes from the LAST row's anchor, not the next
    one down, deliberately -- any pixel of imprecision in a hand-drawn
    box is unavoidable, and measuring the gap between two ADJACENT rows
    then multiplying it out compounds that same error at every one of
    the 11 rows in between (a 1px error becomes an 11px drift by row
    12). Measuring across the full span and dividing by the row count
    spreads that same 1px of imprecision across all 11 gaps instead,
    which is why row 10 read the wrong pixels entirely on a real capture
    that used the adjacent-row version of this.

    Returns None if any anchor isn't calibrated yet, so callers can fall
    back to the existing log/OCR path without special-casing "half
    calibrated"."""
    r1p1 = regions.get("freefire_alive_r1p1")
    r1p2 = regions.get("freefire_alive_r1p2")
    rlastp1 = regions.get("freefire_alive_rlastp1")
    r1elim = regions.get("freefire_alive_r1elim")
    if not (r1p1 and r1p2 and rlastp1 and r1elim):
        return None

    bar_gap_x = r1p2["x"] - r1p1["x"]
    row_gap_y = (rlastp1["y"] - r1p1["y"]) / max(1, rows - 1)
    elim_dx = r1elim["x"] - r1p1["x"]
    elim_dy = r1elim["y"] - r1p1["y"]

    # Columns 3 and 4 are normally DERIVED from the p1->p2 spacing, which
    # assumes the four slots are evenly spaced. They are on this artwork,
    # but "assumes" is doing real work there -- so an optional anchor for
    # any column overrides the derived position with a measured one. Left
    # uncalibrated (the usual case) nothing changes; drawn, that column
    # is pinned exactly where the operator put it, which is the fix when
    # one crop sits off-centre and the rest are fine.
    columns = []
    for p in range(players):
        pinned = regions.get(f"freefire_alive_r1p{p + 1}")
        if pinned and p > 1:                     # p1/p2 already define the grid
            columns.append((pinned["x"], pinned["w"], pinned["h"]))
        else:
            base = r1p2 if p == 1 else r1p1
            columns.append((r1p1["x"] + p * bar_gap_x,
                            base["w"] if p == 1 else r1p1["w"],
                            base["h"] if p == 1 else r1p1["h"]))

    grid = []
    for row in range(rows):
        # row_gap_y is a float (see above) -- rounded to the nearest
        # pixel here, once, rather than left to propagate into every
        # box's coordinates as a fraction mss.grab() can't use.
        row_y = round(r1p1["y"] + row * row_gap_y)
        bars = [
            {"x": cx, "y": row_y, "w": cw, "h": ch}
            for (cx, cw, ch) in columns
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
            # classify_alive_bar, NOT classify_bar: the average-colour
            # version flips a live squad to eliminated under the
            # blue-zone glow. See its docstring.
            classify_alive_bar(c)
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


# Last elim count that actually READ for each grid row, by row index.
# The digit pass fails on the odd frame -- a glow sweep crossing the
# number, a particle effect, a frame caught mid-animation -- and an
# unread frame used to emit null, which blanked the KILLS cell on air
# and then filled it back in a quarter-second later: a visible blink,
# and worse, a changed value every poll, which defeated the overlay's
# own "only redraw when something changed" guard and made the whole
# table repaint constantly.
#
# Holding the last good number instead is the honest trade: an elim
# count only ever climbs during a match, so a briefly stale one is
# never WRONG in a way a blank isn't -- it's just late by a poll or
# two. Cleared when a new match starts, so nothing carries across
# games.
_alive_grid_last_elims = {}


# A squad's realistic ceiling for one match. Twelve squads of four is
# 48 players, so a whole lobby yields ~44 kills total -- one squad
# taking 35 of them is already beyond anything real, and anything above
# that is the digit pass having read something that isn't the elim
# count (two numbers run together, a fragment of the team name, a
# timer). Kept generous on purpose: this only has to catch nonsense,
# not police a plausible score.
FREEFIRE_MAX_TEAM_ELIMS = 35


def apply_team_marks(rows):
    """Attaches the operator's zone/fire ticks to each published row.

    Carried on the row itself rather than looked up separately in the
    overlay, so every consumer of sidetableRows sees them the same way
    and none of them needs its own copy of the team-name matching."""
    marks = server_state["liveOps"].get("teamMarks") or {}
    for row in rows:
        mark = marks.get((row.get("teamName") or "").strip().upper()) or {}
        row["inZone"] = bool(mark.get("zone"))
        row["onFire"] = bool(mark.get("fire"))
    return rows


# team -> the bars/count it last showed while still alive, so a wipe
# awaiting approval can keep displaying that instead of the truth.
_last_alive_by_team = {}


# How many polls in a row must agree that a squad is wiped before the
# wipe is believed at all.
#
# A wipe is the single most consequential reading this system takes: it
# fires an animation, a card, a re-sort, and points -- and on a false one
# an operator is handed a tick that says a living squad is out. Yet it
# was acted on from a single frame, while a mere KILL COUNT already has
# to agree across a window before it is published.
#
# Four polls is about a second. A real wipe is permanent, so a second
# costs nothing and no viewer can tell; a flicker from a glow sweep, a
# particle effect or a frame caught mid-animation never survives it.
FREEFIRE_WIPE_CONFIRM_FRAMES = 4

# team -> consecutive polls it has read as wiped.
_wipe_streak = {}


def wipe_is_confirmed(key, wiped):
    """True once a squad has read as wiped for long enough to believe.

    The streak resets the moment a single read says otherwise, which is
    what makes this a confirmation rather than a delay: one credible
    "still alive" is enough to throw the whole thing out."""
    if not wiped:
        _wipe_streak.pop(key, None)
        return False
    n = _wipe_streak.get(key, 0) + 1
    _wipe_streak[key] = n
    return n >= FREEFIRE_WIPE_CONFIRM_FRAMES


def gate_eliminations(rows):
    """Holds a detected squad wipe off air until the operator ticks it.

    Done HERE rather than in the overlay on purpose: the overlay already
    animates whatever transition it sees, so if the published rows
    simply don't show the wipe yet, nothing downstream needs to know
    this exists. The moment it's approved the rows flip, the overlay
    sees a normal alive-to-eliminated transition, and the flyby and the
    re-sort happen exactly as they always did. One place to reason
    about, and the sheet push gets the same held value for free.

    A wipe with no remembered alive state is let straight through --
    that's an engine started mid-match, where inventing a "still alive"
    reading would be worse than showing what was actually read.

    Off (settings.eliminationApproval false) this does nothing at all."""
    live = server_state["liveOps"]
    if not server_state.get("settings", {}).get("eliminationApproval", True):
        if live.get("pendingEliminations"):
            live["pendingEliminations"] = []
        return rows

    approved = {t.strip().upper() for t in (live.get("approvedEliminations") or [])}
    # The last rows that actually went to air, as a fallback picture of a
    # squad while it was still up.
    last_published = {}
    for prev in (live.get("sidetableRows") or []):
        k = (prev.get("teamName") or "").strip().upper()
        if k and not prev.get("eliminated") and prev.get("aliveCount") is not None:
            last_published[k] = (list(prev.get("bars") or []), prev.get("aliveCount"))
    pending = []
    for row in rows:
        team = (row.get("teamName") or "").strip()
        key = team.upper()
        read_wiped = bool(row.get("eliminated")) or row.get("aliveCount") == 0
        # Counted before anything else is decided, so a read that says
        # "alive" clears the streak even on a row we go on to skip.
        wiped = wipe_is_confirmed(key, read_wiped)
        if read_wiped and not wiped:
            # Read as wiped, but not for long enough to believe yet. Show
            # what it last really was and say nothing -- an unconfirmed
            # wipe must not reach the operator's tick list, because a tick
            # is a decision and there is nothing decided yet.
            #
            # The same two places to look as a held wipe, and for the same
            # reason: leaving the dead bars in place would put an
            # unconfirmed wipe on air off a single frame, which is the
            # thing this whole check exists to stop. With neither, there
            # is nothing honest to show and it goes through as read.
            remembered = _last_alive_by_team.get(key) or last_published.get(key)
            if remembered:
                row["bars"], row["aliveCount"] = list(remembered[0]), remembered[1]
                row["eliminated"] = False
            continue
        if not wiped:
            if row.get("aliveCount") is not None:
                _last_alive_by_team[key] = (list(row.get("bars") or []), row.get("aliveCount"))
            continue
        if key in approved:
            continue
        # A held wipe has to keep LOOKING alive, not merely be called
        # alive. Holding one while leaving the dead bars it just read put
        # rows with grey indicators into the living half of the table,
        # sorted among squads that were actually still in -- so the one
        # genuinely alive squad sat near the bottom under a pile of
        # dead-looking rows. Whatever is shown has to be a picture of
        # that squad while it was still up.
        #
        # Two places to find one: what this gate last saw of it, and
        # failing that the row last PUBLISHED for it, which is a reading
        # that actually went to air.
        remembered = _last_alive_by_team.get(key) or last_published.get(key)
        if not remembered:
            # Never seen alive by either -- an engine attached mid-match,
            # or a row that has read dead from the moment it was first
            # understood.
            #
            # This used to go straight through, on the grounds that
            # inventing an "alive" reading was worse than showing what was
            # actually read. That was the wrong trade. It put eliminations
            # on air that nobody ticked, and worse, such a squad never
            # reached the tick list at all -- so it could not be approved
            # even after the fact, and never got its card. Exactly the one
            # thing this setting exists to prevent.
            #
            # It waits like every other wipe, shown as still in until the
            # tick. A full squad is the state every team starts a match
            # in, so it is the least wrong thing to show for a squad
            # nothing is known about.
            width = len(row.get("bars") or []) or 4
            remembered = (["alive"] * width, width)
        pending.append({"teamName": team, "elims": row.get("elims")})
        row["bars"], row["aliveCount"] = list(remembered[0]), remembered[1]
        row["eliminated"] = False
        row["awaitingApproval"] = True

    if pending != live.get("pendingEliminations"):
        live["pendingEliminations"] = pending
    return rows


# Free Fire's own placement table, the one the result file bakes into
# RankScore. Needed live because a squad's points jump the moment it is
# wiped, and the result file for this match does not exist yet.
FREEFIRE_PLACEMENT_POINTS = {
    1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0,
}

# When the alive grid last produced real rows, and how long its word
# stands afterwards.
#
# Three things build this table and they do not agree. The log reader is
# the awkward one: starting into a match already in progress it skips the
# log's history on purpose -- otherwise every old elimination would fire
# onto air -- so it believes the lobby is fresh, everyone alive, nobody
# with kills. That picture then went out on any poll the grid missed, and
# the table flipped between two truths several times a minute: squads
# dying and coming back, positions handed to teams that were fine.
#
# So the grid holds the table while it is reading. The log keeps every
# other job it has -- the kill feed, match start and end, fetching the
# result file -- because none of those are in dispute. It simply stops
# overwriting a reading that is better than its own.
#
# The window is generous: a grid that misses a few polls in a row is
# still the best source in the room, and going quiet for fifteen seconds
# is what a real stoppage looks like.
_grid_rows_at = 0.0
GRID_AUTHORITY_SECONDS = 15

# A Free Fire lobby. Used wherever the roster cannot say how many
# squads are in -- which is a real state, not a theoretical one: the
# roster is empty right up until someone fills it in.
FREEFIRE_DEFAULT_LOBBY = 12

# team -> the position it finished this match in, filled as squads die.
_finish_ranks = {}


def _lobby_size(rows=None):
    """How many squads started this match.

    Read off the ROSTER, not off the live rows. The row list is whatever
    the grid managed to make out on a given poll, so a single unread row
    -- a name that did not resolve, two rows that read as the same squad
    -- made the lobby look one smaller and shifted every finishing
    position from that moment on. A real match dealt 1..11 where it owed
    2..12, handing out 1st to a squad that went out ninth.

    Falls back to the rows, then to twelve, which is a Free Fire lobby.
    """
    teams = ((server_state.get("roster") or {}).get("teams") or [])
    named = sum(1 for t in teams if (t.get("name") or "").strip())
    if named:
        return named
    if rows is None:
        rows = (server_state.get("liveOps") or {}).get("sidetableRows") or []
    return len(rows) or FREEFIRE_DEFAULT_LOBBY


def assign_finish_ranks(rows):
    """Works out where each wiped squad finished, as it happens.

    A squad's finishing position is simply how many teams were still in
    when it went out, itself included: the first wipe of a twelve-team
    lobby finishes 12th, the next 11th, and so on. Counted at the moment
    of the transition rather than derived afterwards, because afterwards
    every dead team looks alike.

    Cleared when a match restarts -- nothing eliminated means a new game,
    the same signal the overlay uses to reset its own ordering."""
    global _finish_ranks
    live = [r for r in rows if not r.get("eliminated")]
    # "Nobody is out" is not the same as "a new match". A wipe held for
    # the operator's tick is not shown as eliminated either, so treating
    # that as a fresh lobby cleared the very list the tick is made from --
    # the pending wipes vanished before anyone could approve them.
    holding = any(r.get("awaitingApproval") for r in rows)
    if len(live) == len(rows) and not holding:
        # A fresh lobby. Everything that belongs to the LAST match goes
        # with it -- positions, and the approvals that released each of
        # those wipes.
        #
        # The approvals especially: that list was never cleared, so a
        # squad ticked in game 1 stayed ticked for the rest of the event
        # and its wipe in every later game went to air with nobody
        # approving it. The gate quietly stopped working for exactly the
        # teams that had used it most.
        _finish_ranks = {}
        ops = server_state.get("liveOps") or {}
        del _elim_card_queue[:]      # last match's cards are not owed
        if ops.get("approvedEliminations"):
            ops["approvedEliminations"] = []
        if ops.get("pendingEliminations"):
            ops["pendingEliminations"] = []
        return rows

    named = [(r, (r.get("teamName") or "").strip().upper()) for r in rows]

    # Squads that are out and have never been given a position. There is
    # usually one; there can be several at once when the table first
    # resolves, or when the engine is started into a match already in
    # progress.
    # Give back the place of anyone who is alive again, BEFORE counting.
    #
    # This used to happen at the bottom of the function, after positions
    # had been handed out -- so a squad that flickered to "eliminated"
    # for a poll and back took a place with it, and the next real
    # elimination was counted as though one team had already finished.
    # CLUTZA went out first of twelve and was given 11th: twelve, minus a
    # position held by a squad that was alive and well. The stale entry
    # was then dropped at the bottom, taking the evidence with it.
    #
    # A squad that is alive has not finished. Nothing else here is true
    # until that is.
    for row, name in named:
        if name and not row.get("eliminated"):
            _finish_ranks.pop(name, None)

    fresh = [(r, n) for r, n in named if n and r.get("eliminated") and n not in _finish_ranks]

    # Positions are handed out from the bottom of what is left, one each.
    # Giving them all len(live) + 1 -- which is what this did -- put five
    # squads on 6th and three on 5th in a single real match, and only the
    # first of each group could ever fire a card. Order among squads that
    # transition in the same pass is arbitrary, but the positions are not
    # shared.
    # Positions are handed out from the bottom of what is left, one each,
    # and never reissued to a second squad.
    #
    # Without the "taken" check a position could be given out twice: a
    # squad whose read flickers back to alive loses its position (see the
    # loop below), and the next assignment -- computed from a live count
    # that has since changed -- lands on a number somebody else already
    # holds. The live table had DESI GAMER and TEAM APEX both on 10th with
    # 11th unused, which is two squads claiming one finish.
    taken = set(_finish_ranks.values())
    # One place behind everyone still playing -- the same rule the tick
    # uses, so a squad gets the same answer whether the operator released
    # it or it went through on its own. The squads going out in THIS pass
    # already read eliminated, so they are added back on: three at once
    # take the three places above the survivors, in order.
    fresh_keys = {n for _, n in fresh}
    finished_before = sum(
        1 for r in rows
        if r.get("eliminated")
        and (r.get("teamName") or "").strip().upper() not in fresh_keys
    )
    # Dealt from the places that are actually FREE, highest first, rather
    # than by counting down from a starting number.
    #
    # Counting down decremented once per squad with no floor, so a batch
    # of eliminations -- which is exactly what a resync produces -- ran
    # past 1 into 0 and then negatives. Live: finishing positions of 0,
    # -1, -2, -3, -4, -5 and -6 on one table. The old "skip what is
    # taken" check could not help, because it stopped at 1 and the
    # decrement after it did not.
    #
    # A place is a number between 1 and the lobby size that nobody else
    # holds. Enumerating those directly cannot produce anything else, and
    # if there are somehow more squads going out than places left, the
    # extras simply get none -- which shows as a blank corner rather than
    # as a squad finishing minus sixth.
    lobby = _lobby_size(rows)
    free = [p for p in range(lobby, 0, -1) if p not in taken]
    for (row, name), place in zip(fresh, free):
        _finish_ranks[name] = place
        taken.add(place)

    # The card is for a squad going out, not for the table catching up.
    # Several at once means a resync -- a restart mid-match, or the grid
    # resolving for the first time -- and announcing one of them would be
    # picking a name out of a hat and putting it on air.
    if len(fresh) == 1:
        row, name = fresh[0]
        announce_elimination(row, _finish_ranks[name])

    for row, name in named:
        if not name:
            continue
        if row.get("eliminated"):
            row["finishRank"] = _finish_ranks.get(name)
        else:
            _finish_ranks.pop(name, None)
    return rows


# Fallback when the setting is missing -- see settings.elimCardSeconds.
FREEFIRE_ELIM_CARD_SECONDS = 4

# How long a card keeps once another squad is waiting behind it.
#
# A tick is an operator pressing a button and expecting the graphic to
# move. Strict four-second turns meant a tick could show nothing for most
# of a ten-count, which reads as the button not working; 1.2s was still
# long enough to feel like waiting. At 0.4s the card in front is already
# on its way out before the operator has looked up, and the new one lands
# inside a second including the round trip.
#
# A card with nobody behind it is untouched and still gets its full hold.
# This only ever applies when the operator is ticking faster than the
# graphic can play, which is exactly when they want it to hurry.
FREEFIRE_ELIM_CARD_RUSH_MS = 400

# Squads waiting for a card, in the order they went out.
_elim_card_queue = []


def drain_elim_card_queue():
    """Shows the next squad waiting for a card, once the last has had its
    time. Called on the poll, so waiting costs nothing."""
    if not _elim_card_queue:
        return False
    te = server_state.get("teamEliminated") or {}
    now_ms = int(time.time() * 1000)
    if te.get("status") == "shown" and (te.get("shownUntil") or 0) > now_ms:
        return False
    nxt = _elim_card_queue.pop(0)
    seconds = server_state.get("settings", {}).get("elimCardSeconds") or FREEFIRE_ELIM_CARD_SECONDS
    te["status"] = "shown"
    te["shownUntil"] = now_ms + int(float(seconds) * 1000)
    te["teamName"] = nxt["teamName"]
    te["rank"] = nxt["rank"]
    te["kills"] = nxt["kills"]
    server_state["teamEliminated"] = te
    print(f"[elimination] {nxt['teamName']} finished #{nxt['rank']} -- card fired (queued)")
    return True


def finish_position_for(key, rows):
    """Where a squad finishes: one place behind everyone still playing.

    Two teams left standing when a third goes out means that third
    finished 3rd. Counted off the table itself, every time, rather than
    from a running tally -- because a tally drifts and this cannot. The
    tally version put a squad 1st while two teams were still alive, from
    a stale entry left by a squad that had flickered to "eliminated" and
    back.

    The squad going out is excluded by NAME rather than assumed dead or
    alive: a wipe held for the operator's tick still reads alive, one
    that already went through reads eliminated, and the answer has to be
    the same either way. Whether it was held is a fact about the operator
    and has nothing to do with where the squad finished.

    Never higher than the lobby -- if the grid loses a row for a poll
    that is a reason to be careful, not to invent a 13th place.
    """
    rows = rows or (server_state.get("liveOps") or {}).get("sidetableRows") or []
    # Counted by who has ALREADY finished, not by who is left.
    #
    # The two agree whenever the table is complete, and disagree exactly
    # when it is not: a row the grid loses for a poll is neither alive
    # nor eliminated, so counting the living quietly loses a place and
    # every position after it is one out. Counting the finished against
    # the lobby size -- which comes from the roster and does not flicker
    # -- a missing row changes nothing at all.
    finished = sum(
        1 for r in rows
        if r.get("eliminated")
        and (r.get("teamName") or "").strip().upper() != key
    )
    lobby = _lobby_size(rows)
    return max(1, min(lobby - finished, lobby))



def claim_finish_rank(key, rows):
    """Fixes where a squad finished, at the moment it is decided.

    A squad finishes in the position equal to how many squads are still
    in, itself included -- the same rule assign_finish_ranks applies when
    a wipe reaches air. Used when the OPERATOR decides it, by ticking: a
    held wipe has no position yet, because the position is assigned when
    the gate releases it on the next poll, and the card is raised on the
    tick itself. That left the card printing an empty corner for the
    first eliminations of a match, when nothing had a position yet.

    Positions already handed out are skipped, so deciding one by tick can
    never collide with one dealt automatically."""
    if key in _finish_ranks:
        return _finish_ranks[key]
    taken = set(_finish_ranks.values())
    pos = max(1, finish_position_for(key, rows))
    while pos in taken and pos > 1:
        pos -= 1
    _finish_ranks[key] = pos
    return pos


def reset_alive_for_new_match(reason=""):
    """Everything that belongs to ONE game, cleared in one place.

    Two things start a game: the log's own match_start line, and the
    operator saying so. The button exists because that line does not
    always come -- a custom room restarted without it, an engine attached
    between games, a game that ended while the engine was down. Both go
    through here so the button can never be a weaker reset than the
    automatic one, which is the opposite of what it is for.

    The table is left looking like a fresh lobby rather than emptied: a
    blank table between games is indistinguishable from a broken one, and
    the next poll overwrites these values with real readings anyway.
    """
    global _finish_ranks
    _alive_grid_last_elims.clear()
    _alive_grid_elim_recent.clear()
    _alive_grid_last_bars.clear()
    _alive_grid_settle.clear()
    _alive_grid_accepted_bars.clear()
    _alive_grid_bars_pending.clear()
    _last_alive_by_team.clear()
    _wipe_streak.clear()
    _last_known_kills.clear()
    _finish_ranks = {}
    del _elim_card_queue[:]

    ops = server_state.setdefault("liveOps", {})
    ops["approvedEliminations"] = []
    ops["pendingEliminations"] = []
    ops["teamMarks"] = {}
    for row in (ops.get("sidetableRows") or []):
        width = len(row.get("bars") or []) or 4
        row["bars"] = ["alive"] * width
        row["aliveCount"] = width
        row["eliminated"] = False
        row["elims"] = 0
        row["finishRank"] = None
        row.pop("awaitingApproval", None)

    # Any card mid-flight belongs to the game that just ended.
    server_state["teamEliminated"] = {"status": "idle", "shownUntil": None,
                                      "teamName": "", "rank": None,
                                      "photo": "", "kills": 0}
    print("[alive] reset for a new game" + (f" ({reason})" if reason else ""))


def announce_elimination(row, finish_rank):
    """Puts the eliminated squad on the top-centre card, automatically.

    Fired from the point where a squad is first given a finishing
    position. That is downstream of gate_eliminations, which was taken as
    enough -- it is not. The gate has one way through without a tick: a
    wipe it has no alive reading for, which is every engine started into a
    running match and every row just pointed at a squad already out. Those
    reach here, take a position, and put a card on air that nobody
    approved.

    So the tick is checked here too, on its own account. While "confirm
    eliminations" is on, a squad that is not in the approved list does not
    get a card, however it arrived. The setting says before they go ON
    AIR, and this is the loudest thing that goes on air.

    The card is also not touched while it is already showing someone: two
    squads can go out within a second of each other, and swapping the
    name out from under a card mid-animation reads as a glitch rather
    than as two eliminations. The second waits in a queue and gets its
    own card as soon as the first has had its time.
    """
    settings = server_state.get("settings", {})
    if settings.get("eliminationApproval", True):
        live = server_state.get("liveOps") or {}
        approved = {t.strip().upper() for t in (live.get("approvedEliminations") or [])}
        if (row.get("teamName") or "").strip().upper() not in approved:
            return

    te = server_state.get("teamEliminated") or {}
    now_ms = int(time.time() * 1000)
    if te.get("status") == "shown" and (te.get("shownUntil") or 0) > now_ms:
        # It is not dropped, though: near the end of a match two or three
        # squads go within seconds of each other, and an operator who ticks
        # three should see three cards. It waits its turn instead.
        name = (row.get("teamName") or "")
        if name and not any(q["teamName"] == name for q in _elim_card_queue):
            _elim_card_queue.append({
                "teamName": name,
                "rank": finish_rank,
                "kills": _sheet_elim_value(row),
            })
            # Someone is behind it now, so the card on screen gives up
            # the rest of its hold rather than making the operator watch
            # it out before their tick shows anything.
            te["shownUntil"] = min(te.get("shownUntil") or 0,
                                   now_ms + FREEFIRE_ELIM_CARD_RUSH_MS)
            server_state["teamEliminated"] = te
        return
    te["status"] = "shown"
    seconds = server_state.get("settings", {}).get("elimCardSeconds") or FREEFIRE_ELIM_CARD_SECONDS
    te["shownUntil"] = now_ms + int(float(seconds) * 1000)
    te["teamName"] = row.get("teamName") or ""
    te["rank"] = finish_rank
    te["kills"] = _sheet_elim_value(row)
    # Left as-is rather than cleared: an operator-chosen photo for this
    # squad should still win, and the overlay falls back to the roster's
    # own badge when there isn't one.
    server_state["teamEliminated"] = te
    print(f"[elimination] {te['teamName']} finished #{finish_rank} -- card fired")


# team (normalised) -> TP as the LIVE tab last reported it.
_carry_points = {}


def apply_live_points(rows):
    """The number the PTS. column shows: what a team brought into this
    match, plus what it has earned so far in it.

    Carried total comes from the LIVE tab; this match contributes its
    kills immediately, and its placement points only once the squad is
    actually out -- until then there is no placement to award, and
    guessing one would have the column jumping every time a squad moved
    up or down the table.

    Left as None when nothing is carried and nothing is known, so the
    column stays blank rather than claiming a confident zero."""
    for row in rows:
        key = normalize_for_match(row.get("teamName"))
        carry = _carry_points.get(key)
        # The SAME guarded accessor the sheet push uses, not a fresh
        # `elims or score`. score still holds a count the publish guard
        # rejected as impossible, so reading it directly would put the
        # rejected number back -- straight into a running total, where it
        # is even harder to spot than in a kills column. Measured: a
        # bogus 77 turned a 40-point team into 117.
        kills = _sheet_elim_value(row)
        placement = FREEFIRE_PLACEMENT_POINTS.get(row.get("finishRank")) if row.get("finishRank") else None
        # Always a number, never blank.
        #
        # It used to go blank when nothing was known, on the grounds that
        # an unknown total should not claim a confident zero. On air that
        # reads as broken, not as careful: three squads sat with an empty
        # PTS column while the rest showed numbers, and the honest answer
        # for a squad with nothing carried and nothing scored is nought.
        row["livePoints"] = (carry or 0) + (kills or 0) + (placement or 0)
        row["carryPoints"] = carry
    return rows


def _fetch_live_carry(sheet_id, tab):
    """Reads team name and TP off the LIVE tab.

    Columns are found by their HEADER, not by position: the tab reads
    C=team then Booyah / MP / PP / KP / TP, and inserting a column one
    day should not silently start publishing kill points as totals.
    """
    import urllib.request, urllib.parse, csv, io as _io

    def fetch(cells):
        url = ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=%s"
               "&range=%s&headers=0" % (sheet_id, urllib.parse.quote(tab), cells))
        with urllib.request.urlopen(url, timeout=10) as resp:
            return list(csv.reader(_io.StringIO(resp.read().decode("utf-8", "replace"))))

    # Header row and data row fetched SEPARATELY. Asked for C1:H13 in one
    # request, this endpoint decides row 1 is a header, folds it into the
    # CSV's own column labels and then hands back a blank first line -- so
    # the header arrives empty and nothing matches. headers=0 does not
    # stop it. Asked for C1:H1 on its own, the same endpoint returns the
    # header perfectly. Two small requests every twenty seconds is a fair
    # price for a lookup that cannot silently return nothing.
    head = fetch("C1:H1")
    if not head or not head[0]:
        return {}
    header = [h.strip().upper() for h in head[0]]
    try:
        tp = header.index("TP")
    except ValueError:
        return {}
    out, unmatched = {}, []
    roster_teams = ((server_state.get("roster") or {}).get("teams")) or []
    for line in fetch("C2:H13"):
        if len(line) <= tp:
            continue
        label = (line[0] or "").strip()
        if not label:
            continue
        try:
            value = int(float(line[tp] or 0))
        except (TypeError, ValueError):
            continue
        # Keyed by the ROSTER's name, because that is what the live rows
        # carry. The tab is labelled the way the graphics are -- "TAG",
        # "GODLIKE", "iQOO TG" -- so a straight string lookup would find
        # almost nothing. match_roster_team is the same ladder the result
        # files go through: taught alias, then exact on name or short
        # name, then containment.
        team = match_roster_team(label, roster_teams) if roster_teams else None
        if team and team.get("name"):
            out[normalize_for_match(team["name"])] = value
        else:
            out[normalize_for_match(label)] = value
            unmatched.append(label)
    if unmatched:
        # Surfaced rather than swallowed: a label nobody can place is a
        # team whose running total silently never appears on the graphic.
        server_state["liveOps"]["carryUnmatched"] = unmatched
    else:
        server_state["liveOps"].pop("carryUnmatched", None)
    return out


_last_carry_fetch_at = 0.0
_last_carry_error_at = 0.0


async def refresh_live_carry_points():
    """Pulls the carried totals in on a slow timer. Slow on purpose: a
    team's series total only changes when a match is committed, so
    polling it faster buys nothing and spends someone's quota."""
    global _last_carry_fetch_at, _last_carry_error_at, _carry_points
    settings = server_state.get("settings", {})
    sheet_id = (settings.get("livePointsSheetId") or "").strip()
    if not sheet_id:
        return
    now = time.time()
    if now - _last_carry_fetch_at < FREEFIRE_CARRY_REFRESH_SECONDS:
        return
    _last_carry_fetch_at = now
    tab = (settings.get("livePointsTab") or "LIVE").strip()
    try:
        loop = asyncio.get_running_loop()
        fetched = await loop.run_in_executor(ocr_executor, _fetch_live_carry, sheet_id, tab)
        if fetched:
            _carry_points = fetched
    except Exception as e:
        if now - _last_carry_error_at > 60:
            _last_carry_error_at = now
            print(f"[live points] couldn't read the {tab} tab -- {e}")


# How often to re-read the carried totals. They move once a match, so
# this is about being current within a break, not within a gunfight.
FREEFIRE_CARRY_REFRESH_SECONDS = 20


def sanitise_published_elims(rows):
    """Last guard before anything reaches the overlay or the sheet: a
    kill count that can't be a kill count is published as nothing at
    all.

    The debounce in hold_last_good_elims only ever protected the alive
    GRID. The log path writes sidetableRows straight from the client's
    own running team score, and the OCR fallback from its own read --
    neither passed through any check, so a row the grid isn't covering
    (unassigned, or before it's calibrated) went to air unfiltered.
    That's how a table of 92 / 77 / 75 / 43 appeared: those are far past
    anything a single match can produce, so whatever the client was
    reporting there, it wasn't this game's kills.

    Blanking beats publishing: an empty KILLS cell reads as "not known
    yet", while a wrong number reads as fact -- and with the table
    ranked by kills, a wrong number also drags that team to the top."""
    for row in rows:
        value = row.get("elims")
        if value is not None and value > FREEFIRE_MAX_TEAM_ELIMS:
            row["elims"] = None
            row["elimsImplausible"] = True
    return rows


# How many polls in a row must agree before a new number is believed.
# At a 0.25s poll that's about half a second -- invisible on air, and
# enough that a one-frame misread never reaches the overlay, since
# garbage from a glow sweep or a particle effect is different garbage
# each frame while the real number sits perfectly still.
FREEFIRE_ELIM_AGREE_FRAMES = 2

# How many polls to stop trusting a row's elim box after its alive bars
# change. The client animates that row when a player goes down or comes
# back -- the number cross-fades, slides, or is briefly covered -- and a
# crop taken mid-animation is a picture of a digit halfway through
# becoming another one. Reading it produces a number that is not any
# team's score. At 0.25s a poll this is about a second of silence on
# that row, after which the normal agreement rule applies, so a real
# change lands roughly a second and a half late -- unnoticeable on air,
# and the entire point is that what does land is real.
FREEFIRE_ELIM_SETTLE_FRAMES = 4

# How many of the last FREEFIRE_ELIM_WINDOW genuine reads must agree
# before a new value is believed. Counted over a window rather than
# consecutively -- see hold_last_good_elims for the two ways consecutive
# counting starved a busy row.
FREEFIRE_ELIM_WINDOW = 5

# A count that goes DOWN is wrong, so it has to work much harder.
#
# Within a match a squad's eliminations only ever accumulate. 13 followed
# by 3 is not a squad losing ten kills; it is a digit dropped off a
# two-digit number, which is the single most common way this misreads --
# and under the ordinary rule two agreeing frames were enough to publish
# it, because garbage repeats often enough at two.
#
# So a drop is held to a higher bar: it must keep reading that way across
# most of the window before it is believed. A real drop can still happen
# -- a row reassigned to a different squad, a match reset -- and both of
# those clear the remembered value anyway, so this rule never sees them.
#
# What it cannot do is tell a persistent misread from the truth, so a
# drop that survives the higher bar is published AND flagged, and the
# dashboard says so rather than quietly changing the number.
FREEFIRE_ELIM_DROP_AGREE_FRAMES = 4

# row index -> the last few genuine reads, newest last
_alive_grid_elim_recent = {}
# row index -> the bar pattern last seen, and polls left before this
# row's elim box is trusted again. See FREEFIRE_ELIM_SETTLE_FRAMES.
_alive_grid_last_bars = {}
_alive_grid_settle = {}


# How many polls a CHANGED bar pattern must persist before it goes on
# air. Second line of defence behind the per-pixel vote in
# classify_alive_bar: the vote survives a glow covering up to half a
# bar, but a sweep at full strength can cover more than that for an
# instant. A real elimination persists; a sweep moves on. Half a second
# at the current poll rate.
FREEFIRE_BARS_AGREE_FRAMES = 2

_alive_grid_accepted_bars = {}    # row index -> the bar pattern on air
_alive_grid_bars_pending = {}     # row index -> (candidate pattern, polls seen)


def hold_stable_bars(grid_rows, remember=True):
    """Publishes a row's bars only once the same pattern has been read
    on consecutive polls, so a momentary misread can't put a live squad
    on air as wiped (or vice versa).

    aliveCount is recomputed from whatever is published, not from the
    raw read, so the count and the bars can never disagree.

    remember=False applies the same rules without advancing anything --
    the preview must show what's on air without a look counting as a
    reading."""
    for i, row in enumerate(grid_rows):
        raw = row.get("bars") or []
        # The live path carries bars as plain status strings; the preview
        # carries {status, preview} dicts so the crop can be shown beside
        # the reading. Same rules either way -- read the status out, write
        # it back in place, and leave the dict's crop alone.
        as_dicts = bool(raw) and isinstance(raw[0], dict)
        reading = tuple(b["status"] if as_dicts else b for b in raw)
        accepted = _alive_grid_accepted_bars.get(i)

        if accepted is None or len(accepted) != len(reading):
            accepted = reading            # first sight: nothing to compare to
            if remember:
                _alive_grid_accepted_bars[i] = reading
        elif reading != accepted:
            candidate, seen = _alive_grid_bars_pending.get(i, (None, 0))
            seen = seen + 1 if candidate == reading else 1
            if seen >= FREEFIRE_BARS_AGREE_FRAMES:
                accepted = reading
                if remember:
                    _alive_grid_accepted_bars[i] = reading
                    _alive_grid_bars_pending.pop(i, None)
            elif remember:
                _alive_grid_bars_pending[i] = (reading, seen)
        elif remember:
            _alive_grid_bars_pending.pop(i, None)

        if as_dicts:
            for b, status in zip(raw, accepted):
                b["status"] = status
        else:
            row["bars"] = list(accepted)
        row["aliveCount"] = sum(1 for b in accepted if b == "alive")
    return grid_rows


def hold_last_good_elims(grid_rows, remember=True):
    """Decides what elim count to actually publish for each row.

    A value has to be read FREEFIRE_ELIM_AGREE_FRAMES polls in a row
    before it's believed; until then the last believed value stands. An
    unread frame changes nothing.

    This replaces an earlier rule that also required counts never to go
    DOWN. That was true of real scores and disastrous in practice: a
    misread of 9 on a team sitting on 0 passed the plausibility check
    (9 is a perfectly believable score), and from then on every correct
    reading of 0 was BELOW it and therefore rejected as impossible. One
    bad frame poisoned that row for the rest of the match and, with the
    table ranked by kills, parked that team near the top the whole time.

    Agreement doesn't have that failure mode: whatever is really on
    screen wins within two polls, because it's the only value that can
    show up twice in a row. The absolute cap stays -- it rejects
    nonsense without ever blocking a plausible number.

    remember=False runs the identical rules without advancing any of the
    counters, which is what the dashboard preview needs: it must report
    the number that's on air without a look counting as a reading."""
    for i, row in enumerate(grid_rows):
        reading = row.get("elims")
        accepted = _alive_grid_last_elims.get(i)

        # Is this row's own UI mid-animation? Its bars changing is the
        # one reliable signal we have that the client is animating that
        # row right now, and that is exactly when its number is
        # untrustworthy -- which is why the garbage always showed up
        # "whenever someone got eliminated or came back".
        bars = tuple(row.get("bars") or ())
        previous_bars = _alive_grid_last_bars.get(i)
        settling = _alive_grid_settle.get(i, 0)
        bars_changed = previous_bars is not None and bars != previous_bars
        # Armed only when NOT already settling. Re-arming on every change
        # made the freeze open-ended: in a fight the bars move every
        # poll or two, each one restarting the timer, so that row's count
        # could sit frozen for as long as the fight lasted -- which is
        # the "ON AIR updates late" being seen. Bounded at
        # FREEFIRE_ELIM_SETTLE_FRAMES from the FIRST change instead, so
        # the worst case is a fixed second, not the length of the fight.
        if bars_changed and settling == 0:
            settling = FREEFIRE_ELIM_SETTLE_FRAMES
        elif settling > 0:
            settling -= 1
        if remember:
            _alive_grid_last_bars[i] = bars
            _alive_grid_settle[i] = settling

        if reading is not None and reading > FREEFIRE_MAX_TEAM_ELIMS:
            reading = None          # nonsense, treat as unread
        if settling > 0:
            reading = None          # mid-animation: nothing here is real yet

        # Agreement is counted over a WINDOW of recent genuine reads, not
        # over consecutive polls. Two separate things starved the old
        # consecutive rule, and the busiest row on the table hit both:
        #
        #   - A settling frame reads as None, and None used to clear the
        #     pending counter. A squad whose bars keep changing settles
        #     constantly, so its counter was wiped before it could ever
        #     reach two -- its count could sit stale indefinitely while
        #     every quieter row updated fine.
        #   - A two-digit number that occasionally misreads (21, 2, 21,
        #     27, 21) never produces two IDENTICAL reads back to back,
        #     even though the true value is clearly the most common one.
        #
        # Counting occurrences in the last few genuine reads handles
        # both: real values recur, garbage rarely repeats itself.
        # Frames with nothing to read don't enter the window at all, so
        # a settle simply pauses progress rather than undoing it.
        if reading is not None:
            recent = _alive_grid_elim_recent.get(i, [])
            recent = (recent + [reading])[-FREEFIRE_ELIM_WINDOW:]
            if remember:
                _alive_grid_elim_recent[i] = recent
            # A lower number than what is on air has to clear a higher
            # bar -- see FREEFIRE_ELIM_DROP_AGREE_FRAMES.
            dropping = accepted is not None and reading < accepted
            needed = (FREEFIRE_ELIM_DROP_AGREE_FRAMES if dropping
                      else FREEFIRE_ELIM_AGREE_FRAMES)
            if reading != accepted and recent.count(reading) >= needed:
                if dropping:
                    print(f"[elims] row {i + 1}: {accepted} -> {reading} "
                          f"(a DROP, confirmed {recent.count(reading)}x) -- "
                          f"worth checking that row's crop")
                    row["elimsDropped"] = [accepted, reading]
                accepted = reading
                if remember:
                    _alive_grid_last_elims[i] = reading
                    _alive_grid_elim_recent[i] = [reading]
            elif dropping:
                # Still arguing for a lower number. Say so while it does.
                row["elimsSuspect"] = [accepted, reading,
                                       recent.count(reading), needed]

        row["elims"] = accepted          # None until something is believed
        # True when this frame didn't confirm what's being published --
        # either it read nothing, or it read something not yet agreed.
        # A row that stays held is either genuinely unreadable or being
        # fed garbage, and either way it's worth seeing in the preview.
        row["elimsHeld"] = accepted is not None and reading != accepted
    return grid_rows


def apply_alive_grid_overrides(grid_rows, overrides):
    """Layers the operator's manual corrections on top of the auto-read
    grid, BEFORE identity is joined on -- so a forced bar/elim value
    feeds the same aliveCount/elims everything downstream (the overlay,
    the sheet push, the preview) already reads, rather than being a
    separate thing that has to be reconciled later.

    overrides is liveOps.aliveGridOverrides: {"bars": {"{row}_{bar}":
    status}, "elims": {"{row}": count}}. Absent key = no override for
    that slot, so a freshly-added row/bar with nothing forced yet behaves
    exactly as if this function weren't called at all."""
    bar_overrides = (overrides or {}).get("bars") or {}
    elim_overrides = (overrides or {}).get("elims") or {}
    out = []
    for i, row in enumerate(grid_rows):
        bars = [
            bar_overrides.get(f"{i}_{b}", status)
            for b, status in enumerate(row["bars"])
        ]
        elims = row["elims"]
        if str(i) in elim_overrides:
            elims = elim_overrides[str(i)]
        out.append({
            "bars": bars,
            "aliveCount": sum(1 for b in bars if b == "alive"),
            "elims": elims,
        })
    return out


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


def build_alive_grid_preview(crops, palette=None):
    """On-demand debug view of one fresh capture: every bar's classified
    status WITH the actual crop it was classified from, and the elim
    number WITH its crop, so an operator can see exactly what the grid is
    reading rather than trusting the numbers blind. Not part of the
    regular per-poll path -- built only when the dashboard asks for it
    (freefire_fetch_alive_grid_preview), same reasoning as the loadout
    capture crops: sending this every poll would be the same payload-bloat
    problem that keeps coming up elsewhere in this project."""
    rows_preview = []
    for bar_crops, elim_crop in crops:
        bars = []
        for c in bar_crops:
            status = classify_alive_bar(c)
            bars.append({
                "status": status,
                "preview": crop_to_data_url(c, scale=6) if c is not None and c.size else "",
            })
        elims = (ocr_small_number(elim_crop)
                 if elim_crop is not None and elim_crop.size else None)
        rows_preview.append({
            "aliveCount": sum(1 for b in bars if b["status"] == "alive"),
            "elims": elims,
            "bars": bars,
            "elimPreview": crop_to_data_url(elim_crop, scale=4) if elim_crop is not None and elim_crop.size else "",
        })
    return rows_preview


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


def _icon_signature(img_bgr, size=32):
    """A standardised, centre-weighted grayscale thumbnail of the icon.

    This was a 4x4 grid of average BGR -- 48 numbers, nearly all of which
    described the BACKGROUND. In game the portraits sit on a coloured
    panel (salmon, green, slate) that the reference art does not have, so
    the tint swamped the face: measured against real captures with the
    answer known by eye, the true character came back 66th, 42nd and 34th
    out of eighty. That is not matching, it is a lottery with a
    confidence number attached.

    Three things fix it, and each was measured on those same captures
    rather than assumed.

    Grayscale, standardised. Subtracting the mean and dividing by the
    spread means a flat colour cast shifts every pixel alike and falls
    straight out, along with the HUD's dimming of a knocked player's
    card. What is left is where the light and dark actually sit, which is
    the face rather than the panel behind it.

    Centre-weighted. The background lives around the edge and the face is
    in the middle, so the middle is what counts. This was the single
    ingredient that mattered -- every configuration that scored well had
    it, and every one that dropped it scored worse.

    And enough resolution to tell two faces apart. 32x32 standardised is
    a thousand numbers about the picture instead of forty-eight about its
    colour; past that it stops helping, so it stops there.

    Those same three captures now come back 1st, 3rd and 2nd. Not
    perfect -- similar faces (dark hair, beard) still trade places, which
    is why the match carries its runners-up and why an operator can
    correct one.
    """
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
    g -= g.mean()
    spread = g.std()
    if spread > 1e-6:
        g /= spread
    global _ICON_WINDOW
    if _ICON_WINDOW is None or _ICON_WINDOW.shape[0] != size:
        yy, xx = np.mgrid[0:size, 0:size]
        c = (size - 1) / 2.0
        r = np.sqrt((yy - c) ** 2 + (xx - c) ** 2) / (size / 2.0)
        _ICON_WINDOW = np.clip(1.45 - r, 0.0, 1.0).astype(np.float32)
    return (g * _ICON_WINDOW).reshape(-1)


# Built once, on first use -- see _icon_signature.
_ICON_WINDOW = None


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


def icon_library_report():
    """What each reference library actually holds, right now.

    A slot that cannot be identified shows a dash, and a dash looks the
    same whether the box is uncalibrated, the folder is missing, or the
    icon simply is not in it. Pets and equipment read as blank for a whole
    event because neither folder existed -- the reason was sitting in a
    tooltip nobody hovers. Reported as a number the operator can see.
    """
    report = {}
    for library_name in sorted(set(FREEFIRE_ICON_LIBRARIES.values())):
        folder = (FREEFIRE_ASSETS_DIR if library_name == "characters"
                  else FREEFIRE_ASSETS_DIR / library_name)
        # Counted off the folder rather than by loading the library: this
        # runs at startup and on every reload, and decoding eighty-odd
        # PNGs to answer "how many are there" is work for nothing.
        files, named = [], 0
        if folder.is_dir():
            names = load_icon_names(folder)
            files = [f for f in sorted(folder.iterdir())
                     if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
            named = sum(1 for f in files
                        if names.get(f.stem, "").strip()
                        and not names.get(f.stem, "").strip().isdigit()
                        and names.get(f.stem, "").strip() != f.stem)
        report[library_name] = {
            "folder": str(folder),
            "exists": folder.is_dir(),
            "images": len(files),
            "named": named,
            "slots": sorted(k for k, v in FREEFIRE_ICON_LIBRARIES.items()
                            if v == library_name),
        }
    return report


def reload_icon_libraries():
    """Re-reads the reference folders, and re-stamps the names onto
    captures that have already been taken.

    Two separate problems, one button.

    The signatures are cached for the life of the process, so artwork
    added or renamed while the engine is running had no effect at all --
    which reads as "I added the pets and nothing happened".

    And a capture stores the name as it stood when the shot was taken, so
    naming a character afterwards left every earlier capture showing the
    file id. A whole lobby's worth of cards read 102000052 where the
    operator had since typed Oscar, and no amount of renaming fixed them
    because nothing ever looked again. Re-stamping is by LABEL, which is
    the file the match actually landed on and does not change.
    """
    _icon_signature_cache.clear()
    libraries = {name: load_icon_library(name)
                 for name in set(FREEFIRE_ICON_LIBRARIES.values())}

    renamed = 0
    for team in ((server_state.get("roster") or {}).get("teams") or []):
        for player in (team.get("players") or []):
            for entry in (player.get("loadouts") or {}).values():
                for slot, got in (entry.get("slots") or {}).items():
                    label = (got or {}).get("label")
                    if not label:
                        continue
                    lib = libraries.get(FREEFIRE_ICON_LIBRARIES.get(slot, ""), {})
                    ref = lib.get(label)
                    if ref and got.get("name") != ref[1]:
                        got["name"] = ref[1]
                        renamed += 1

    report = icon_library_report()
    server_state["loadoutLibraries"] = report
    total = sum(v["images"] for v in report.values())
    print(f"[loadout] reference images reloaded -- {total} in all, "
          f"{renamed} earlier capture(s) renamed")
    return report, renamed


def _report_icon_libraries_at_startup():
    try:
        server_state["loadoutLibraries"] = icon_library_report()
    except Exception as e:                      # never block startup on this
        print(f"[loadout] couldn't survey the reference folders: {e}")


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
    best_ref, best_name = library[best_label]

    # The runner-up has to be a genuinely DIFFERENT character, or the
    # confidence number lies about what it measures.
    #
    # The asset dump ships the same picture under more than one id --
    # 101000018/101100018 (Kapella) and 102000019/102200019 (Wolfrahh) are
    # byte-identical pairs, as are 101000001/101000004 and
    # 101888888/101999999. A character can also legitimately appear twice
    # under one name when the operator names a base and an awakened
    # portrait the same thing. Either way the runner-up sits at the same
    # distance as the best, so 1 - best/runner_up collapses to 0.000 and
    # the capture is flagged red no matter how good it was. Measured by
    # feeding every reference image back in as a perfect capture: Kapella
    # and Wolfrahh scored 0.000, every other named character scored 1.000.
    #
    # So walk past anything that is the same name or the same picture and
    # compare against the nearest real alternative instead.
    runner_up = None
    for distance, label in scored[1:]:
        ref, name = library[label]
        if name.strip().upper() == best_name.strip().upper():
            continue
        if float(np.linalg.norm(ref - best_ref)) <= 1e-6:
            continue
        runner_up = distance
        break

    if runner_up is None or runner_up <= 1e-6:
        # Single-image library, or nothing left that isn't the same
        # character -- report the match but never as a confident one.
        confidence = 0.0
    else:
        confidence = max(0.0, 1.0 - (best_distance / runner_up))
    # The runners-up, carried with the answer.
    #
    # Two faces of the same build -- dark hair, beard, same lighting --
    # still trade places, and no threshold makes that not so. What helps
    # is that the right one is almost always a place or two behind: on
    # the captures measured by hand it sat 1st, 3rd and 2nd. Offering
    # them turns "it read wrong" into one click, which is worth far more
    # than another decimal place of confidence.
    alts = []
    seen = {best_name.strip().upper()}
    for distance, label in scored[1:]:
        name = library[label][1]
        key = (name or "").strip().upper()
        if key in seen:
            continue
        seen.add(key)
        alts.append({"label": label, "name": name})
        if len(alts) >= 3:
            break

    return {
        "label": best_label,
        "name": library[best_label][1],
        "confidence": round(confidence, 3),
        "lowConfidence": confidence < ICON_MATCH_MIN_CONFIDENCE,
        "alts": alts,
    }


# Surveyed once at import, so the dashboard has the picture on its first
# sync rather than after the first capture.
_report_icon_libraries_at_startup()


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
    """POSTs to the Apps Script web app and returns whatever it answered.

    The live alive push ignores the return value -- it fires many times a
    minute and nothing useful could be done with a reply -- but the
    per-match results push is a deliberate operator action, so it reports
    back how many rows the script actually matched.

    Apps Script answers a POST with a 302 to script.googleusercontent.com
    and the real body is behind it; urllib follows that on its own, and by
    then the script has already run."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


# The last payload actually delivered, so an unchanged table isn't
# rewritten on every poll. Cleared on a failed send -- see below.
_last_sheet_payload = None
# True while a write is on the wire, so concurrent pushes can't stack up
# and land out of order.
_sheet_push_inflight = False


# team (normalised) -> the last kill count actually read for it this
# match. See _sheet_elim_value.
_last_known_kills = {}


def _sheet_elim_value(row):
    """What the sheet should show as this row's kill count.

    elims is withheld once a match ends (see link_live_teams), while
    score holds the same number and stays put after the whistle -- so
    falling back to score is what stops the sheet blanking the instant a
    match is over. That part is deliberate and still happens.

    What it must NOT do is resurrect a number the publish guard just
    rejected. sanitise_published_elims blanks an impossible count and
    flags the row, but score still holds that very number, so the
    fallback handed it straight to the sheet: 43, 77 and 75 appeared
    against squads the graphic was correctly showing on 0 and 2. The
    overlay and the sheet disagreed because only one of them was reading
    the guarded value.

    Withheld means withheld -- and score is checked against the same cap
    on its own account, since a row can carry an impossible score without
    ever having been through the flagging path.
    """
    key = normalize_for_match(row.get("teamName"))
    value = row.get("elims")
    if value is not None:
        if key:
            _last_known_kills[key] = value
        return value
    if row.get("elimsImplausible"):
        return None
    score = row.get("score")
    if score is not None and score > FREEFIRE_MAX_TEAM_ELIMS:
        return None
    if score is not None:
        if key:
            _last_known_kills[key] = score
        return score

    # Neither field has a number any more, which happens for the rest of
    # the match once it ends: elims is withheld on purpose, and the grid
    # path never fills score, so the fallback above has nothing to fall
    # back to.
    #
    # The graphic remembers -- effectiveKills() in the overlay keeps the
    # last count it saw, which is why the ELIMS column kept showing 15
    # while the PTS column beside it counted zero kills and published
    # placement points alone. Two numbers from one row disagreeing on the
    # same screen. The engine remembers it too now, so the graphic, the
    # points and the sheet are all reading the same figure.
    return _last_known_kills.get(key)


async def push_sidetable_to_sheet(rows):
    """The LIVE alive/elim table, written to the broadcast sheet as it
    changes. Off by default -- see liveSheetPush.

    This is not the post-match results push. That one is on demand, from
    the operator picking a game, and is untouched by this setting: a
    finished match still goes to RESULTS exactly as before.
    """
    global _last_sheet_push_error_at, _last_sheet_payload, _sheet_push_inflight

    # Off unless the operator asks for it.
    #
    # Apps Script answers in its own time, and this runs from the poll
    # loop, so a slow response holds one of the engine's worker threads
    # for up to fifteen seconds. Worth it when the sheet is on a screen
    # someone is watching; pure cost on a day when nobody opens it, which
    # is most days.
    if not server_state.get("settings", {}).get("liveSheetPush"):
        return

    url = (server_state.get("settings", {}).get("sheetWebhookUrl") or "").strip()
    if not url or not rows:
        return
    # The roster's short name for each team, sent alongside the full one.
    # A broadcast sheet labels its rows the way the graphics do -- "TEV",
    # "TAG" -- not with the registered name, and three letters is too
    # little for the receiving script to match on text alone without
    # risking the wrong row ("TE" sits inside "TEAM EVOLUTION"). Sending
    # the short name makes those rows an exact hit instead of a guess.
    # Without it TEAM EVOLUTION, TEAM APEX GAMING and TEAM ELITE reached
    # no row at all, so their ticks and kill counts never appeared.
    shorts = {}
    for team in ((server_state.get("roster") or {}).get("teams") or []):
        name = normalize_for_match(team.get("name"))
        if name and team.get("shortName"):
            shorts[name] = team["shortName"]

    # One line per squad, and none for a row with no name. A misread name
    # can have two rows claiming the same squad; sending both wrote that
    # team's cells twice in one push, with whichever arrived last winning,
    # so the sheet could show another row's numbers under its name. The
    # first reading wins and the rest is dropped.
    sent = set()
    sheet_rows = []
    for r in rows:
        name = (r.get("teamName") or r.get("rawText") or "").strip()
        if not name or name.upper() in sent:
            continue
        sent.add(name.upper())
        sheet_rows.append({
            "team": name,
            "short": shorts.get(normalize_for_match(r.get("teamName")), ""),
            "aliveCount": r.get("aliveCount"),
            "elims": _sheet_elim_value(r),
        })
    payload = {"rows": sheet_rows}
    # Nothing to say, nothing to send. A match sits unchanged for most of
    # its length -- twelve rows all alive, no kills yet -- and rewriting
    # the same twelve rows four times a second is a write the sheet has to
    # process, a quota it has to spend, and a cell that visibly repaints
    # for anyone looking at it.
    if payload == _last_sheet_payload:
        return

    # One write on the wire at a time. The poll fires these without
    # waiting, and a POST outlasts several polls, so without this they
    # stack up and land out of order -- which is its own way of making a
    # cell flicker. Dropping this one is safe: the value is still in
    # sidetableRows, so the next poll sends it, and only the newest
    # version of the table ever reaches the sheet.
    if _sheet_push_inflight:
        return
    _sheet_push_inflight = True
    _last_sheet_payload = payload

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(ocr_executor, _post_sheet_payload, url, payload)
    except Exception as e:
        # Forget what was sent, so the next poll retries rather than
        # treating a failed write as already delivered.
        _last_sheet_payload = None
        now = time.time()
        if now - _last_sheet_push_error_at > 10:
            _last_sheet_push_error_at = now
            print(f"[sheet push] couldn't reach the webhook -- {e}")
    finally:
        _sheet_push_inflight = False


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


def _standings_identity(team, roster_teams):
    """(key, display name, short name) for one result-file team row.

    One function so the aggregation and the last-game lookup can never
    disagree about which standings row a team belongs to."""
    name = (team.get("teamName") or "").strip()
    if not name:
        return None, "", ""
    short = team.get("shortName", "")
    roster_team = match_roster_team(name, roster_teams) if roster_teams else None
    if roster_team:
        name = (roster_team.get("name") or name).strip()
        short = roster_team.get("shortName") or short
    # Normalised key even after resolving, so two spellings that both fail
    # to resolve ("TSG ARMY" and "TSG  Army") still land in one row.
    return (normalize_for_match(name) or name), name, short


def _last_match_of_series(matches):
    """The final game, for the rulebook's last tiebreaker.

    By timestamp when every match carries one, otherwise the order they
    were committed in -- sorting a mixed set by a missing timestamp would
    quietly promote the ones without to the front of the series."""
    if not matches:
        return None
    if all(m.get("timestamp") for m in matches):
        return sorted(matches, key=lambda m: m["timestamp"])[-1]
    return matches[-1]


def compute_freefire_standings(matches, roster_teams=None):
    """Totals every committed match into one table, aggregating on the
    ROSTER's identity rather than on whatever string each match happened
    to be committed with.

    That distinction is the whole job here. A match committed before the
    roster had short names in it stored the result file's own name --
    "TEV", "NBE", "4NDS" -- while one committed afterwards resolves to
    "TEAM EVOLUTION", "NEBULA ESP", "4ENDS ESP". Keyed on the literal
    string, the same squad becomes two rows with half the points each.
    Measured on the real state file: two games of eleven teams produced
    TWENTY rows, and the only two that survived intact were the two whose
    file name happens to equal their roster name.

    Resolving here rather than only at commit time also means fixing a
    roster entry (or teaching an alias) repairs matches already in the
    book, instead of leaving them mis-keyed forever."""
    if roster_teams is None:
        roster_teams = ((server_state.get("roster") or {}).get("teams")) or []
    agg = {}
    for match in matches:
        for team in match.get("teams", []):
            key, name, short = _standings_identity(team, roster_teams)
            if not key:
                continue
            row = agg.setdefault(key, {
                "teamName": name, "shortName": short,
                "matches": 0, "totalKills": 0, "placementPoints": 0,
                "totalPoints": 0, "bestRank": None, "booyahs": 0,
                "lastGameRank": None,
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

    # Where each team finished in the LAST game of the series -- the
    # rulebook's final tiebreaker. Read after the loop rather than tracked
    # inside it, because "last" is a property of the series, not of the
    # order matches happen to be iterated in.
    last_match = _last_match_of_series(matches)
    if last_match:
        for team in last_match.get("teams", []):
            key, _, _ = _standings_identity(team, roster_teams)
            if key in agg:
                agg[key]["lastGameRank"] = team.get("rank")

    # Every registered team appears, whether or not they have played yet:
    # a squad that missed a game is still in the event, and a standings
    # graphic with a hole in it reads as a bug rather than as a result.
    # setdefault, so a team that HAS played keeps its real numbers.
    for team in roster_teams:
        name = (team.get("name") or "").strip()
        if not name:
            continue
        agg.setdefault(normalize_for_match(name) or name, {
            "teamName": name, "shortName": team.get("shortName", ""),
            "matches": 0, "totalKills": 0, "placementPoints": 0,
            "totalPoints": 0, "bestRank": None, "booyahs": 0,
            "lastGameRank": None,
        })

    standings = list(agg.values())
    # The event's own tiebreakers, in the rulebook's order:
    #   total points, then Booyahs, then total eliminations across the
    #   series, then placement in the LAST game of the series.
    # Team name is appended as a backstop so a set of teams still level
    # after all four never falls back to the order they happened to appear
    # in the result file -- which is what decided it before: reversing the
    # file reversed the standings.
    #
    # Placement points are deliberately absent. totalScore is
    # rankScore + killScore in every row the client writes (checked against
    # the real file), so by the time points and eliminations are both
    # level, placement points are level too and could never decide
    # anything.
    standings.sort(key=lambda r: (
        -r["totalPoints"],
        -r["booyahs"],
        -r["totalKills"],
        r["lastGameRank"] if r["lastGameRank"] is not None else 999,
        r["teamName"].upper(),
    ))
    return standings


# How long any one client gets to accept a message before the rest stop
# waiting for it.
#
# A send that never completes used to wedge whoever called broadcast. That
# is survivable in the poll loop -- the next poll tries again -- but
# handle_client calls it BEFORE its read loop starts, so the engine's own
# relay connection could sit there, fully connected and being written to,
# having never once begun listening. State flowed out; nothing came back.
# Every elimination tick, zone and fire mark sent over the relay was
# accepted by the relay and delivered to an engine that was not reading.
#
# Measured while it was happening: 113ms round trip to the engine on
# localhost, no reply whatsoever through the relay, with the relay itself
# forwarding between two other clients in 23ms.
SEND_TIMEOUT_SECONDS = 5


async def _send_guarded(client, data):
    """One client's problem stays one client's problem."""
    try:
        await asyncio.wait_for(client.send(data), SEND_TIMEOUT_SECONDS)
    except Exception:
        # Slow, wedged, or gone. Its own handler tidies it up when the
        # connection actually closes; nothing here should wait for that.
        pass


async def broadcast(message):
    if not connected_clients:
        return
    data = json.dumps(message)
    # A snapshot: a client connecting or disconnecting mid-fanout must not
    # change the set being iterated.
    await asyncio.gather(*[_send_guarded(c, data) for c in list(connected_clients)],
                         return_exceptions=True)


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

    with mss_grabber() as sct:
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
    # Guarded for the same reason, and more sharply: everything below this
    # line is the read loop, so anything that blocks here leaves a client
    # connected and mute. A first snapshot that cannot be delivered is no
    # reason to never listen to that client again -- the next state change
    # sends another one anyway.
    await _send_guarded(websocket, json.dumps({
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
                        raw = await loop.run_in_executor(
                            ocr_executor, _post_sheet_payload, url, test_payload)
                        # Reaching the URL is not the same as the script
                        # running. A deployment set to "Anyone with a Google
                        # account", or one that needs re-authorising, answers
                        # HTTP 200 with a sign-in PAGE -- so simply not
                        # raising used to be reported as success while
                        # nothing whatsoever had been written to the sheet.
                        # The only proof is the script's own JSON reply.
                        try:
                            answer = json.loads(raw)
                        except Exception:
                            answer = None
                        if not isinstance(answer, dict) or "matched" not in answer:
                            await websocket.send(json.dumps({
                                "type": "freefire_sheet_push_test_result", "ok": False,
                                "error": "The URL answered, but with a page rather than the "
                                         "script's reply -- nothing was written. Re-deploy the "
                                         "web app with Who has access set to \"Anyone\".",
                            }))
                        else:
                            await websocket.send(json.dumps({
                                "type": "freefire_sheet_push_test_result", "ok": True,
                                "matched": answer.get("matched"),
                                "total": answer.get("total"),
                                "scriptError": answer.get("error"),
                            }))
                    except Exception as e:
                        await websocket.send(json.dumps({
                            "type": "freefire_sheet_push_test_result",
                            "ok": False, "error": str(e),
                        }))
            elif payload.get("type") == "freefire_results_push":
                # One match's placement points and kills into that match's
                # own column pair on the RESULTS tab -- the copy/paste step
                # the operator was doing by hand after every game.
                #
                # The rows come from the dashboard rather than being built
                # here, because the match worth pushing is often the one
                # under REVIEW: fetched, checked, not yet committed. That
                # only exists in the dashboard (see ffQmeSourceMatch), and
                # having two places decide "which match is current" is how
                # they end up disagreeing. This side owns the webhook URL
                # and the network call, which the browser can't do itself.
                url = (server_state.get("settings", {}).get("sheetWebhookUrl") or "").strip()
                rows = payload.get("rows") or []
                try:
                    match_number = int(payload.get("match"))
                except (TypeError, ValueError):
                    match_number = 0
                if not url:
                    await websocket.send(json.dumps({
                        "type": "freefire_results_push_result",
                        "ok": False, "error": "No webhook URL saved yet -- set it in Broadcast Sheet Push.",
                    }))
                elif not rows:
                    await websocket.send(json.dumps({
                        "type": "freefire_results_push_result",
                        "ok": False, "error": "Nothing to push -- fetch or commit a match first.",
                    }))
                elif match_number < 1:
                    await websocket.send(json.dumps({
                        "type": "freefire_results_push_result",
                        "ok": False, "error": f"Match number {payload.get('match')!r} isn't valid.",
                    }))
                else:
                    body = {"kind": "results", "match": match_number, "rows": rows}
                    try:
                        loop = asyncio.get_running_loop()
                        raw = await loop.run_in_executor(
                            ocr_executor, _post_sheet_payload, url, body)
                        try:
                            answer = json.loads(raw)
                        except Exception:
                            # A sheet that isn't shared, or a deployment
                            # that needs re-authorising, answers with an
                            # HTML sign-in page rather than JSON. Say that
                            # instead of showing the operator raw markup.
                            answer = {"ok": False,
                                      "error": "The web app answered with a page, not a result -- "
                                               "re-deploy it with access set to \"Anyone with the link\"."}
                        answer.setdefault("ok", False)
                        answer["type"] = "freefire_results_push_result"
                        answer["match"] = match_number
                        await websocket.send(json.dumps(answer))
                    except Exception as e:
                        await websocket.send(json.dumps({
                            "type": "freefire_results_push_result",
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
            elif payload.get("type") == "alive_status_points":
                server_state["display"]["aliveStatusPoints"] = bool(payload.get("on"))
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
            elif payload.get("type") == "freefire_set_alive_bar_override":
                # Forces one bar's status regardless of what the colour
                # read says -- an operator watching the real game
                # overrides an automated read on a live broadcast, not
                # the other way round. An empty/missing status clears
                # the override and goes back to auto. See
                # apply_alive_grid_overrides.
                try:
                    row = int(payload.get("row", -1))
                    bar = int(payload.get("bar", -1))
                except (TypeError, ValueError):
                    row, bar = -1, -1
                status = (payload.get("status") or "").strip()
                overrides = server_state["liveOps"].setdefault(
                    "aliveGridOverrides", {"bars": {}, "elims": {}})
                bar_overrides = overrides.setdefault("bars", {})
                if 0 <= row < FREEFIRE_ALIVE_GRID_ROWS and 0 <= bar < SIDETABLE_PLAYERS_PER_TEAM:
                    key = f"{row}_{bar}"
                    if status in ("alive", "eliminated"):
                        bar_overrides[key] = status
                    else:
                        bar_overrides.pop(key, None)
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_set_alive_elim_override":
                # Same idea, for one row's elim count -- forces it past
                # whatever OCR did or didn't read. Blank clears back to
                # auto.
                try:
                    row = int(payload.get("row", -1))
                except (TypeError, ValueError):
                    row = -1
                raw = payload.get("elims", "")
                overrides = server_state["liveOps"].setdefault(
                    "aliveGridOverrides", {"bars": {}, "elims": {}})
                elim_overrides = overrides.setdefault("elims", {})
                if 0 <= row < FREEFIRE_ALIVE_GRID_ROWS:
                    key = str(row)
                    if str(raw).strip() == "":
                        elim_overrides.pop(key, None)
                        # Also drop the held value for this row. Counts
                        # never decrease, so a misread that slipped past
                        # the plausibility check (a believable-looking 7
                        # that was never real) would otherwise be stuck
                        # for the rest of the match with no way back --
                        # clearing the force field is the way to say
                        # "forget what you think you saw and re-read".
                        _alive_grid_last_elims.pop(row, None)
                        _alive_grid_elim_recent.pop(row, None)
                        _alive_grid_settle.pop(row, None)
                    else:
                        try:
                            elim_overrides[key] = int(raw)
                        except (TypeError, ValueError):
                            pass
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_set_team_mark":
                # zone / fire markers. Stored against the team, cleared
                # by unticking, and wiped entirely on a new match.
                team = (payload.get("team") or "").strip().upper()
                mark = payload.get("mark")
                if team and mark in ("zone", "fire"):
                    marks = server_state["liveOps"].setdefault("teamMarks", {})
                    entry = marks.setdefault(team, {})
                    entry[mark] = bool(payload.get("on"))
                    if not any(entry.values()):
                        marks.pop(team, None)
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_approve_elimination":
                # The tick. Moves one squad from pending to approved, at
                # which point gate_eliminations stops holding its wipe
                # back and the overlay sees the transition normally.
                team = (payload.get("team") or "").strip()
                live = server_state["liveOps"]
                approved = live.setdefault("approvedEliminations", [])
                if team and team not in approved:
                    approved.append(team)
                    live["pendingEliminations"] = [
                        p for p in (live.get("pendingEliminations") or [])
                        if (p.get("teamName") or "").strip().upper() != team.upper()
                    ]
                    # The tick raises the card, not just the row.
                    #
                    # The card is normally raised at the moment a squad is
                    # first given a finishing position. A squad that
                    # reached air without a tick -- the gate's one way
                    # through, for a wipe it had no alive reading to hold
                    # with -- already has its position by the time anyone
                    # ticks it, so it would never be "newly positioned"
                    # again and never got a card at all. Ticking it is the
                    # moment, so this is where it fires.
                    key = team.strip().upper()
                    rows_now = live.get("sidetableRows") or []
                    row = next((r for r in rows_now
                                if (r.get("teamName") or "").strip().upper() == key), None)
                    if row is not None:
                        # A held wipe has no position yet -- it is given one
                        # when the gate releases it, a poll after this. The
                        # card is raised HERE, so the position is decided
                        # here too, or it prints an empty corner.
                        rank = row.get("finishRank") or claim_finish_rank(key, rows_now)
                        row["finishRank"] = rank
                        announce_elimination(row, rank)
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_set_loadout_slot":
                # The operator naming a slot outright. A matcher that is
                # right most of the time still needs this, and without it
                # a wrong read had to be lived with or re-shot.
                try:
                    player = server_state["roster"]["teams"][int(payload["team"])]                                          ["players"][int(payload["player"])]
                    entry = player["loadouts"][str(payload["game"])]
                    slot = str(payload["slot"])
                    label = payload.get("label") or ""
                    lib = load_icon_library(FREEFIRE_ICON_LIBRARIES.get(slot, ""))
                    ref = lib.get(label)
                    if ref is not None:
                        entry.setdefault("slots", {})[slot] = {
                            "label": label, "name": ref[1],
                            "confidence": 1.0, "lowConfidence": False,
                            "manual": True,
                        }
                        save_state()
                        await broadcast({"type": "state_sync", "data": server_state,
                                         "locked": list(locked_fields)})
                except (KeyError, IndexError, ValueError, TypeError) as e:
                    print(f"[loadout] couldn't set that slot: {e}")
            elif payload.get("type") == "freefire_reload_icon_library":
                # Artwork added, or a name typed into names.json, while
                # the engine is up. Without this neither takes effect
                # until a restart, and the names never reach captures
                # already taken at all.
                report, renamed = reload_icon_libraries()
                save_state()
                await broadcast({"type": "state_sync", "data": server_state,
                                 "locked": list(locked_fields)})
                await websocket.send(json.dumps({
                    "type": "freefire_icon_library_reloaded",
                    "libraries": report, "renamed": renamed,
                }))
            elif payload.get("type") == "freefire_reset_alive":
                # "This game is over, start the next one." The log's
                # match_start does this by itself when it arrives; this is
                # for when it does not.
                reset_alive_for_new_match("operator")
                save_state()
                await broadcast({"type": "state_sync", "data": server_state,
                                 "locked": list(locked_fields)})
            elif payload.get("type") == "freefire_fetch_alive_grid_preview":
                # One fresh, on-demand capture -- not the regular per-poll
                # path, so this can afford to also encode every crop as a
                # preview image (see build_alive_grid_preview) without
                # that riding along on every single state_sync.
                grid = build_alive_grid(config.get("regions", {}))
                if grid is None:
                    await websocket.send(json.dumps({
                        "type": "freefire_alive_grid_preview",
                        "error": "The alive grid isn't calibrated yet -- run the ff-alive-grid calibration first.",
                    }))
                else:
                    with mss_grabber() as sct:
                        crops = capture_alive_grid_crops(sct, grid)
                    loop = asyncio.get_running_loop()
                    rows_preview = await loop.run_in_executor(
                        ocr_executor, build_alive_grid_preview, crops,
                        config.get("alive_grid_colors") or DEFAULT_ALIVE_GRID_PALETTE,
                    )
                    # Same hold the live path applies, so the preview
                    # answers "what is on air right now" rather than
                    # "what did this one capture see" -- those diverged,
                    # and the preview saying (unread) while the overlay
                    # showed a number is exactly that gap. remember=False:
                    # looking must not count as a reading.
                    hold_stable_bars(rows_preview, remember=False)
                    hold_last_good_elims(rows_preview, remember=False)
                    # Layer the same manual overrides the real per-poll
                    # path applies (apply_alive_grid_overrides) -- shown
                    # here too, marked, so the preview reflects what's
                    # ACTUALLY feeding the overlay/sheet push right now,
                    # not just what was freshly read off the screen.
                    overrides = server_state["liveOps"].get("aliveGridOverrides") or {}
                    bar_overrides = overrides.get("bars") or {}
                    elim_overrides = overrides.get("elims") or {}
                    row_teams = server_state["liveOps"].get("aliveRowTeams") or []
                    for i, r in enumerate(rows_preview):
                        r["team"] = row_teams[i] if i < len(row_teams) else ""
                        for b, bar in enumerate(r["bars"]):
                            forced = bar_overrides.get(f"{i}_{b}")
                            if forced:
                                bar["status"] = forced
                                bar["overridden"] = True
                        r["aliveCount"] = sum(1 for b in r["bars"] if b["status"] == "alive")
                        if str(i) in elim_overrides:
                            r["elims"] = elim_overrides[str(i)]
                            r["elimOverridden"] = True
                    await websocket.send(json.dumps({
                        "type": "freefire_alive_grid_preview", "rows": rows_preview,
                    }))
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

        elif signal["type"] == "match_start":
            # Last game's held elim counts must not carry into this one --
            # see _alive_grid_last_elims. Every row starts unread again.
            reset_alive_for_new_match("match_start in the log")
            changed = True

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
    # Written below when the grid produces rows, read above when the log
    # decides whether to yield the table -- both in this function, so
    # without this the read raises UnboundLocalError and the engine dies.
    global _grid_rows_at
    # 0.25s (4/sec), not the 1.0s this used to default to. Measured
    # against the real calibrated setup with OCR out of the loop (see the
    # comment above the OCR gating below): finding the log, tailing it,
    # joining the roster, and serializing the state_sync payload together
    # cost ~4ms -- under 2% of even a 0.25s budget. The 1.0s figure dated
    # back to when OCR ran every tick and needed the room; nothing left
    # in the normal case needs it anymore.
    interval = config.get("poll_interval_seconds", 0.25)
    loop = asyncio.get_running_loop()

    with mss_grabber() as sct:
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
                    grid_is_live = (time.time() - _grid_rows_at) < GRID_AUTHORITY_SECONDS
                    if linked["rows"]:
                        log_sidetable_rows = linked["rows"]
                        if (not grid_is_live
                                and linked["rows"] != server_state["liveOps"].get("sidetableRows")):
                            published = apply_live_points(assign_finish_ranks(apply_team_marks(gate_eliminations(sanitise_published_elims(linked["rows"])))))
                            server_state["liveOps"]["sidetableRows"] = published
                            server_state["liveOps"]["sidetableSource"] = "log"
                            changed = True

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
                    published = apply_live_points(assign_finish_ranks(apply_team_marks(gate_eliminations(sanitise_published_elims(parsed["rows"])))))
                    ff_live["sidetableRows"] = published
                    ff_live["sidetableUsedPalette"] = parsed["usedPalette"]
                    ff_live["sidetableSource"] = "ocr"
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
                    config.get("alive_grid_colors") or DEFAULT_ALIVE_GRID_PALETTE,
                )
                grid_rows = hold_stable_bars(grid_rows)
                grid_rows = hold_last_good_elims(grid_rows)
                grid_rows = apply_alive_grid_overrides(
                    grid_rows, server_state["liveOps"].get("aliveGridOverrides"))
                row_teams = server_state["liveOps"].get("aliveRowTeams") or []
                grid_named_rows = apply_alive_grid_identities(grid_rows, row_teams)
                if grid_named_rows:
                    _grid_rows_at = time.time()
                    # Join the grid's rows onto whatever the log/OCR path
                    # left behind, THROUGH THE ROSTER -- not by comparing
                    # the two teamName strings directly.
                    #
                    # They are not always the same string for the same
                    # squad. The grid names a row from the operator's own
                    # assignment, which is always a roster name
                    # ("iQOO TOTAL GAMING"). link_live_teams() names one
                    # from the client's log and only rewrites it to the
                    # roster name IF its matcher resolved it -- when it
                    # doesn't, the row keeps whatever the room called it
                    # ("iQOO TG"). An exact-string join then misses, and
                    # BOTH rows survive: the same team twice, one showing
                    # the grid's fresh number and one the log's, which is
                    # exactly the "wrong number on air" this fixes.
                    # Resolving both sides through match_roster_team()
                    # collapses them onto one canonical key.
                    roster_teams = (server_state.get("roster") or {}).get("teams", []) or []

                    def _resolved_key(name):
                        """Canonical roster key, or None if this name
                        can't be tied to a roster team at all."""
                        resolved = match_roster_team(name, roster_teams)
                        canonical = (resolved or {}).get("name")
                        return _ign_key(canonical) if canonical else None

                    by_key = {}
                    for r in grid_named_rows:
                        k = _resolved_key(r["teamName"]) or _ign_key(r["teamName"])
                        by_key[k] = r

                    # The grid first, because it is what is on the screen
                    # right now, then anything previously published that
                    # the grid has not resolved yet -- and never more rows
                    # than there are teams in the lobby.
                    #
                    # A previously-published row used to be kept whenever
                    # it still resolved to SOME roster team, whether or
                    # not the grid had produced it. Change the roster
                    # between groups and the last group's rows resolve
                    # against the new one and never leave: eleven rows off
                    # the grid plus three ghosts, a fourteen-row table for
                    # a twelve-team lobby, with the graphic silently
                    # showing the first twelve. Two squads that WERE
                    # playing simply did not appear, and the ghosts
                    # brought scores of their own (30, 57) into the points
                    # column.
                    base = server_state["liveOps"].get("sidetableRows") or []

                    # The cap must NOT be read off the list it is capping.
                    # _lobby_size() falls back to the row count when the
                    # roster has no names in it -- and the roster is empty
                    # more often than you would think -- so once a ghost
                    # had got in, len(base) grew to fit it and the cap
                    # agreed with the ghost from then on. Roster first,
                    # then what the grid can actually see right now, then
                    # a standard Free Fire lobby.
                    roster_named = sum(
                        1 for t in roster_teams if (t.get("name") or "").strip())
                    if roster_named and roster_named >= len(grid_named_rows):
                        lobby = roster_named
                    else:
                        # No roster, or one still being typed. Someone
                        # filling it in mid-match must not have two names
                        # evict ten squads the grid can plainly see, so
                        # a roster smaller than the grid's reading is
                        # treated as absent.
                        lobby = max(len(grid_named_rows),
                                    FREEFIRE_DEFAULT_LOBBY)
                    merged, seen = [], set()

                    for r in grid_named_rows:
                        key = _resolved_key(r["teamName"]) or _ign_key(r["teamName"])
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append(r)

                    for r in base:
                        if len(merged) >= lobby:
                            break
                        key = _resolved_key(r.get("teamName"))
                        # A row that ties to no roster team at all has
                        # nothing to add: it cannot be matched, and its
                        # number is the client's cumulative score rather
                        # than this match's kills. That is how one squad
                        # was once on air twice.
                        if key is None or key in seen:
                            continue
                        seen.add(key)
                        merged.append(r)
                    if merged != server_state["liveOps"].get("sidetableRows"):
                        published = apply_live_points(assign_finish_ranks(apply_team_marks(gate_eliminations(sanitise_published_elims(merged)))))
                        server_state["liveOps"]["sidetableRows"] = published
                        server_state["liveOps"]["sidetableSource"] = "grid"
                        changed = True

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

            # Anyone still waiting for a card gets it as soon as the one
            # before has had its time.
            if drain_elim_card_queue():
                changed = True

            # Carried series totals, on their own slow timer.
            await refresh_live_carry_points()

            # The sheet is written ONCE per poll, here, from whatever the
            # table finally says -- never from inside the three paths that
            # build it.
            #
            # Those paths run in sequence and each overwrites
            # sidetableRows wholesale: the log path first, then the OCR
            # fallback, then the alive grid, which is deliberately last so
            # it has the final word. Pushing from inside each one meant the
            # sheet received every intermediate answer -- the log's numbers
            # and then the grid's, four times a second, two different sets
            # of values. The dashboard only ever renders the settled state,
            # which is exactly why it looked steady there while the sheet
            # flickered.
            #
            # The rows that go on air are the rows the sheet gets, and it
            # gets them once.
            # Fired, not awaited: a POST to Apps Script takes a second or
            # more, and this loop runs four times a second. Awaiting it
            # would stall log tailing, OCR and every state_sync behind a
            # write to a spreadsheet.
            asyncio.create_task(
                push_sidetable_to_sheet(server_state["liveOps"].get("sidetableRows") or []))

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
    # A token written in the config is a token anyone can read: these
    # files are committed and this repo is public, so the relay's write
    # credential sat on GitHub. Config is still honoured for anyone
    # running a private fork, but the secret belongs outside git --
    # RELAY_TOKEN in the environment, or ocr/.relay_token, which
    # .gitignore keeps out.
    if not token:
        token = os.environ.get("RELAY_TOKEN", "").strip()
    if not token:
        try:
            token = (Path(__file__).resolve().parent.parent / ".relay_token"
                     ).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if not url or not token:
        print("Relay is enabled in freefire_config.json but 'url'/'token' aren't both set -- skipping relay connection.")
        return
    separator = "&" if "?" in url else "?"
    connect_url = f"{url}{separator}token={token}"
    global relay_websocket
    while True:
        try:
            # Patience with ITSELF.
            #
            # The default is a ping every 20s and 20s to answer, and this
            # engine cannot always keep that: it captures, OCRs and merges
            # twelve rows four times a second, and when that bunches up it
            # stops servicing its own socket for long enough to decide the
            # relay is dead -- and hangs up on a relay that was answering
            # perfectly. Measured: nine such drops in ten minutes, off the
            # relay about one minute in six, with every elimination tick
            # pressed in those windows going nowhere.
            #
            # The close frame named the culprit: the relay logged
            # "RECEIVED 1011 keepalive ping timeout", so it was this side
            # giving up. Two minutes' grace is far longer than any stall
            # seen, while a genuinely dead relay still errors the socket
            # immediately -- which is the case that actually matters.
            async with websockets.connect(connect_url,
                                          ping_interval=20,
                                          ping_timeout=120) as relay_ws:
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
