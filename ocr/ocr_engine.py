"""
MOBA Broadcast OCR Engine.

Captures small screen regions defined in config.json, reads the numbers
with Tesseract OCR, and relays live state to any connected browser
(overlay.html / dashboard.html) over a local WebSocket server.

Run calibrate_hud.py first to set up the live in-game HUD's crop regions
in config.json (calibrate_postmatch_gold.py, calibrate_postmatch_hero.py,
and calibrate_teamstats.py cover the post-match screens and per-player
live K/D/A separately).
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

# Per-player live K/D/A for the Team Stats Overlay spotlight graphic --
# deliberately its OWN calibration pass (calibrate_teamstats.py, not
# calibrate_hud.py) since it's 10 regions the operator calibrates separately
# and later, not part of the always-on main HUD. One combined box per
# player around the whole "K/D/A" readout (e.g. "18/0/2"), not three
# separate boxes -- matches how the HUD actually displays it and halves
# the calibration effort (10 clicks instead of 30). Key format "t1p3_kda"
# = team1, player 3 (1-indexed to match how players are labeled everywhere
# in the dashboard) -- parsed by apply_player_kda_value() below rather
# than FIELD_MAP, since a flat (team,field) tuple doesn't have room for
# the player index, and one OCR read here has to fan out into three
# separate livePlayers fields at once.
PLAYER_KDA_KEYS = [f"t{team}p{p}_kda" for team in (1, 2) for p in range(1, 6)]
PLAYER_KDA_KEYS_SET = set(PLAYER_KDA_KEYS)

NUMBER_REGEX = re.compile(r"\d+")
GOLD_REGEX = re.compile(r"(\d+(?:\.\d+)?)\s*([kK])?")
# "18/0/2" -- kills/deaths/assists as shown together on the live HUD.
# Tolerant of stray spaces around the slashes, which Tesseract sometimes
# inserts even with a tight whitelist.
KDA_REGEX = re.compile(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
# Match clock, e.g. "12:34" -- MM:SS counting up over the course of the
# game. Read continuously (unlike turtle's one-shot toast) since it's a
# steady live HUD readout, same debounce path as kills/gold below.
GAME_TIMER_REGEX = re.compile(r"(\d{1,2}):(\d{2})")
GAME_TIMER_REGION_KEY = "game_timer"

# The turtle announcement toast reads e.g. "Turtle spawning in 15s" while
# counting down, then "Turtle Spawned" once it's up. This only needs to
# catch the countdown message once — everything after that (the live
# countdown display, and switching to the "spawned" graphic) is timed
# locally from that one reading, since re-OCRing a toast that changes every
# frame isn't reliable with the debounce logic the numeric fields use.
# Matching bare "spawn.*N" / "spawned" fired on OTHER objective toasts that
# share the same announcement zone too (seen live: "Lord Spawned" also
# matched). Requiring "turt" (not the full "turtle" — tolerant of the
# trailing letters getting OCR-garbled) scopes it to the turtle toast only.
TURTLE_SPAWNING_REGEX = re.compile(r"turt\w*.{0,20}?spawn\w*\D{0,6}(\d{1,3})", re.IGNORECASE | re.DOTALL)
# "Turtle Spawned" is static text while it's on screen (unlike the ticking
# countdown), so it's safe to detect directly and trust the normal
# debounce-two-frames logic — no need to route through our own timer first.
TURTLE_SPAWNED_REGEX = re.compile(r"turt\w*.{0,15}?spawned", re.IGNORECASE | re.DOTALL)
TURTLE_REGION_KEY = "turtle_announcement"
TURTLE_SPAWNED_DISPLAY_SECONDS = 5
# Lord shares the exact same announcement toast zone as turtle (that's
# WHY the turtle regex above had to be narrowed to "turt*" in the first
# place — "Lord Spawned" used to false-positive it). No countdown warning
# exists for Lord in-game, only the spawned announcement, so this is a
# simpler spawned-only detector than turtle's countdown+spawned pair.
LORD_SPAWNED_REGEX = re.compile(r"lord\w*.{0,15}?spawned", re.IGNORECASE | re.DOTALL)
LORD_SPAWNED_DISPLAY_SECONDS = 5
# Guards against a garbage OCR read (e.g. misreading part of the HUD as a
# number) turning into a wildly wrong countdown.
TURTLE_MIN_COUNTDOWN = 1
TURTLE_MAX_COUNTDOWN = 120

# Tesseract calls are blocking subprocess launches; running the regions
# through a thread pool instead of one-after-another is what actually cuts
# the per-cycle latency down, since they overlap instead of stacking up.
# Sized for REGION_ORDER + all 10 PLAYER_KDA_KEYS (only actually used once
# calibrate_teamstats.py has set them up -- zero-cost otherwise, they're
# just skipped) + turtle + game_timer.
ocr_executor = ThreadPoolExecutor(max_workers=len(REGION_ORDER) + len(PLAYER_KDA_KEYS) + 2)

connected_clients = set()
# websocket -> the "page" query param it connected with (overlay.html,
# prematch.html, postmatch.html send their own name; dashboard.html and
# anything older that doesn't send one falls back to "unknown"). Lets the
# dashboard show whether each OBS/vMix browser source is actually reachable.
connected_pages = {}


def default_player():
    # Still no per-player headshot field here — Post Match shows the hero
    # the player picked in Prematch instead. The MVP Stats push graphic's
    # photo is a one-off upload tied to that push (see default_state()'s
    # "mvp" section), not stored per-roster-player like these stats are.
    return {"damageDealt": 0, "damageTaken": 0, "kills": 0, "deaths": 0, "assists": 0}


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
        # Turtle announcement (OCR-driven). "idle" -> "countdown" (seeded
        # once from an OCR'd "spawning in Ns" reading, ticks down locally
        # via endsAt) -> "spawned" (shown until spawnedUntil) -> "idle".
        # lastRawText is whatever the turtle region last OCR'd, kept purely
        # so the dashboard can show it for calibration/debugging.
        "turtleTimer": {
            "status": "idle",
            "countdownSeconds": 0,
            "endsAt": None,
            "spawnedUntil": None,
            "lastRawText": "",
        },
        # Lord announcement (OCR-driven, spawned-only — see LORD_SPAWNED_REGEX
        # comment). "idle" -> "spawned" (shown until spawnedUntil) -> "idle".
        "lordTimer": {
            "status": "idle",
            "spawnedUntil": None,
            "lastRawText": "",
        },
        # Live match clock. Primary source is OCR (see
        # process_game_timer_reading()) once the game_timer region is
        # calibrated -- each confirmed "MM:SS" read directly overwrites
        # "elapsedMs" and status stays "idle" (OCR reads often enough not
        # to need local ticking between updates). Manual Start/Pause/Reset
        # from the dashboard are the fallback for before calibration or
        # whenever the clock isn't visible on screen; while running that
        # way the overlay ticks locally off "startedAt" instead, same as
        # the turtle countdown. Counts UP (elapsed match time), unlike the
        # prematch draft timer which counts down.
        "matchClock": {
            "status": "idle",
            "startedAt": None,
            "elapsedMs": 0,
        },
        # Per-element position/scale nudges from the dashboard's Graphic
        # Fixing tab, e.g. graphicOverrides["prematch"]["logo-team1"] =
        # {"dx":0,"dy":0,"scale":1}. Purely additive on top of each page's
        # base CSS position (applied as a CSS transform) — the base
        # position stays the source of truth, this just nudges it. Keyed
        # by element id, NOT by team, so it doesn't move when sides swap
        # (it's correcting a fixed screen position, not following a team).
        "graphicOverrides": {
            "prematch": {},
            "postmatch": {},
            "overlay": {},
            "turtle": {},
            "lord": {},
            "mvp": {},
            "teamstats": {},
        },
        # Prematch (manual, pick/ban draft). Each slot is {name, image} —
        # "image" is the exact filename in dashboard/assets/heroes/, resolved
        # by the dashboard's hero picker, not guessed from the name.
        "prematch": {
            "context": {"line1": "LEAGUE STAGE", "line2": "DAY 1 MATCH 1"},
            "phase": "PICKING",
            # Which team is currently on the clock — "team1", "team2", or
            # None. Purely a display indicator, doesn't gate anything.
            "activeTeam": None,
            # Which empty pick/ban slot(s) to glow on the overlay, for
            # whichever team the operator has on the clock. "type" is
            # "pick1" (single pick), "pick2" (two picks, for a double-pick
            # round), "ban", or None. "indices" are the exact slot index(es)
            # chosen at the moment the dashboard button was clicked (the
            # then-current first empty slot(s)) — fixed at click time, not
            # recomputed later, so once that specific box is filled the
            # glow just stops instead of jumping to whatever's next empty.
            "highlight": {"team": None, "type": None, "indices": []},
            # Timer is either running (timerEndsAt set, counting down to
            # that timestamp) or stopped/paused (timerEndsAt is None and
            # timerRemainingMs holds the frozen value to resume/display).
            "timerEndsAt": None,
            "timerDuration": 30,
            "timerRemainingMs": 30000,
            # Deliberately separate from the top-level seriesScore (which is
            # OCR-fed and drives the In-Game overlay's live HUD) — this one
            # is manual-only and local to the Prematch graphic.
            "seriesScore": {"team1": 0, "team2": 0},
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
            # Which side shows VICTORY/DEFEAT — set explicitly from the
            # dashboard now instead of always being derived from the score,
            # since the operator needs to be able to override it. Defaults
            # match the old score>=score behavior (team1 wins a 0-0 tie).
            "result": {"team1": "victory", "team2": "defeat"},
        },
        # MVP Stats push graphic. Team + roster index for the stat numbers
        # (single-sourced from postMatch.players); "characterImage" is a
        # filename picked from a small fixed library of transparent
        # character renders in overlay/assets/ (character1.png,
        # character2.png, ...) -- not derived from the Prematch hero pick,
        # since those wiki infobox images aren't transparent cutouts.
        "mvp": {
            "team": None,
            "playerIndex": None,
            "characterImage": "",
        },
        # Live per-player K/D/A, OCR-fed once calibrate_teamstats.py's 10
        # regions are set up (see PLAYER_KDA_KEYS) -- idle (all zero)
        # until then, same as any other uncalibrated region.
        "livePlayers": {
            "team1": [{"kills": 0, "deaths": 0, "assists": 0} for _ in range(5)],
            "team2": [{"kills": 0, "deaths": 0, "assists": 0} for _ in range(5)],
        },
        # Which player each side of the Team Stats Overlay spotlights --
        # operator-picked from the dashboard, drives the corner boxes on
        # overlay.html (merged in, not a separate page).
        # Pulls character portrait from prematch.picks, IGN from
        # team1/team2.players, and stats from livePlayers above, all by
        # this one index -- nothing duplicated here.
        "teamStatsOverlay": {
            "team1": {"playerIndex": None},
            "team2": {"playerIndex": None},
        },
    }


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            state = deep_merge_defaults(loaded, default_state())
            # A mid-countdown/spawned turtle status doesn't mean anything
            # across a process restart (the game has moved on) — always
            # come back up idle rather than resuming a stale timestamp.
            state["turtleTimer"] = default_state()["turtleTimer"]
            state["lordTimer"] = default_state()["lordTimer"]
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


def _swap_locked_field_key(key):
    """team1.kills <-> team2.kills, seriesScore.team1 <-> seriesScore.team2,
    t1p3_kills <-> t2p3_kills, etc. -- swap_team_sides() moves the actual
    livePlayers data between team1[]/team2[], so a lock on "t1p3_kills" has
    to follow it to "t2p3_kills" or the override protection ends up
    guarding the wrong (now-swapped) slot."""
    if key.startswith("team1."):
        return "team2." + key[len("team1."):]
    if key.startswith("team2."):
        return "team1." + key[len("team2."):]
    if key.endswith(".team1"):
        return key[: -len(".team1")] + ".team2"
    if key.endswith(".team2"):
        return key[: -len(".team2")] + ".team1"
    if key.startswith("t1p"):
        return "t2p" + key[len("t1p"):]
    if key.startswith("t2p"):
        return "t1p" + key[len("t2p"):]
    return key


def swap_team_sides():
    """Interchange everything currently tagged team1 <-> team2, everywhere in
    the state: identity (name/logo/players), live OCR stats, series score,
    prematch bans/picks/on-the-clock indicator, and post match stats/result.
    This is a full identity swap, not a display-only flip, so overlay.html
    and prematch.html need no changes at all — they just keep reading
    team1/team2 as always, now holding the other team's data."""
    s = server_state
    s["team1"], s["team2"] = s["team2"], s["team1"]
    s["seriesScore"]["team1"], s["seriesScore"]["team2"] = (
        s["seriesScore"]["team2"], s["seriesScore"]["team1"],
    )

    pm = s.get("prematch")
    if pm:
        if pm.get("activeTeam") == "team1":
            pm["activeTeam"] = "team2"
        elif pm.get("activeTeam") == "team2":
            pm["activeTeam"] = "team1"
        pm["bans"]["team1"], pm["bans"]["team2"] = pm["bans"]["team2"], pm["bans"]["team1"]
        pm["picks"]["team1"], pm["picks"]["team2"] = pm["picks"]["team2"], pm["picks"]["team1"]
        if "seriesScore" in pm:
            pm["seriesScore"]["team1"], pm["seriesScore"]["team2"] = (
                pm["seriesScore"]["team2"], pm["seriesScore"]["team1"],
            )

    pom = s.get("postMatch")
    if pom:
        pom["team1Score"], pom["team2Score"] = pom["team2Score"], pom["team1Score"]
        pom["stats"]["team1"], pom["stats"]["team2"] = pom["stats"]["team2"], pom["stats"]["team1"]
        pom["players"]["team1"], pom["players"]["team2"] = pom["players"]["team2"], pom["players"]["team1"]
        if "result" in pom:
            pom["result"]["team1"], pom["result"]["team2"] = (
                pom["result"]["team2"], pom["result"]["team1"],
            )

    mvp = s.get("mvp")
    if mvp and mvp.get("team") in ("team1", "team2"):
        mvp["team"] = "team2" if mvp["team"] == "team1" else "team1"

    lp = s.get("livePlayers")
    if lp:
        lp["team1"], lp["team2"] = lp["team2"], lp["team1"]

    tso = s.get("teamStatsOverlay")
    if tso:
        tso["team1"], tso["team2"] = tso["team2"], tso["team1"]

    renamed = {_swap_locked_field_key(f) for f in locked_fields}
    locked_fields.clear()
    locked_fields.update(renamed)


server_state = load_state()
locked_fields = set()

# OCR debounce: a rolling window of the last N raw readings per key, trusted
# once a genuine majority (not just a strict back-to-back streak) agrees.
# The old version required the SAME value N times in a ROW -- fragile
# against any live-video HUD with a glow/shimmer/gradient text style (gold
# counters especially), where a single noisy frame resets the streak back
# to zero and a value can end up never reconfirming even though most
# individual frames actually read it correctly. A window tolerates the
# occasional bad frame mixed in among mostly-good ones.
DEBOUNCE_WINDOW = max(2, config.get("debounce_frames", 5))
last_confirmed = {key: None for key in REGION_ORDER}
reading_window = {key: deque(maxlen=DEBOUNCE_WINDOW) for key in REGION_ORDER}
# Game timer lives outside REGION_ORDER's numeric HUD batch (own crop/OCR
# call in ocr_loop(), same reason turtle_announcement does), but reuses
# confirm_reading()'s generic debounce -- it just needs its own entry in
# these dicts since they're only pre-seeded from REGION_ORDER.
last_confirmed[GAME_TIMER_REGION_KEY] = None
reading_window[GAME_TIMER_REGION_KEY] = deque(maxlen=DEBOUNCE_WINDOW)
# Same deal for the 10 per-player K/D/A keys -- these DO get captured
# through the main numeric batch in ocr_loop() (unlike game_timer/turtle,
# no special one-off crop/OCR call needed, just a different read_region()
# branch), so they just need debounce-dict entries here. confirm_reading()
# works unchanged on these even though the "value" is a (kills,deaths,
# assists) tuple instead of a single int -- Counter/dict lookups don't care,
# tuples are hashable same as ints.
for _key in PLAYER_KDA_KEYS:
    last_confirmed[_key] = None
    reading_window[_key] = deque(maxlen=DEBOUNCE_WINDOW)


def preprocess(img_bgr, upscale=4):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # Calibrated boxes are often cropped right against the digits; a small
    # replicated border stops antialiased edge pixels from being read as
    # part of the digit (a common cause of misreads like 1 -> 7 or 0 -> 8,
    # and -- seen live on the K/D/A regions -- a lone "1"'s serif/flag
    # getting split off into what Tesseract reads as its own leading "2",
    # producing "21" instead of "1"). Bumped from 6px to 10px after that;
    # still cheap (this happens before the 4x upscale) but gives a tight
    # crop's edge pixels more room to blend into the border instead of
    # getting classified as a separate stroke.
    gray = cv2.copyMakeBorder(gray, 10, 10, 10, 10, cv2.BORDER_REPLICATE)
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
TESS_CONFIG_TIMER = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789: "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
TESS_CONFIG_KDA = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789/ "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)


def ocr_number(img_bgr):
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG)
    return text.strip()


def ocr_timer(img_bgr):
    """Same digit-tuned preprocess() pipeline as ocr_number() -- the game
    clock is small plain digits on the HUD same as kills/gold, not the
    glowy announcement text ocr_text() exists for -- just a colon added
    to the whitelist."""
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_TIMER)
    return text.strip()


def ocr_kda(img_bgr):
    """Same digit-tuned preprocess() pipeline as ocr_number()/ocr_timer()
    -- the per-player "K/D/A" readout is small plain HUD digits same as
    kills/gold, just with a "/" added to the whitelist instead of ":"."""
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_KDA)
    return text.strip()


def ocr_text(img_bgr):
    """Reads whatever text is in the crop (used for the turtle toast).
    Deliberately NOT using preprocess()/the digit pipeline above — that
    applies a hard black/white threshold tuned for tiny plain digits on a
    flat background, which was wiping out this glowy/gradient game-
    announcement text entirely (reading nothing at all, not even garbage).
    ocr_lines (defined further down, used for screenshot import) only
    grayscales the image and lets Tesseract's own layout analysis handle
    it, which is what actually reads real words reliably."""
    return " ".join(line["text"] for line in ocr_lines(img_bgr)).strip()


def parse_turtle_countdown(text):
    """Pulls the seconds out of a 'Turtle spawning in Ns'-style reading.
    Returns None if the text doesn't look like that (including if it's the
    already-spawned toast, which has no trailing number)."""
    match = TURTLE_SPAWNING_REGEX.search(text)
    if not match:
        return None
    seconds = int(match.group(1))
    if not (TURTLE_MIN_COUNTDOWN <= seconds <= TURTLE_MAX_COUNTDOWN):
        return None
    return seconds


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


def parse_kda(text):
    """"18/0/2" -> (18, 0, 2). None on anything that doesn't look like a
    plausible K/D/A reading (garbage OCR, missing a segment)."""
    if text is None:
        return None
    match = KDA_REGEX.search(text)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def parse_kda_spaced(text):
    """Post-match screen's K/D/A readout has no slash between the three
    numbers, just a gap (e.g. "18 0 2") -- pulls out the first three digit
    runs regardless of what's between them (space or stray OCR noise),
    rather than matching an exact separator character like parse_kda()
    does. None if fewer than three numbers were found."""
    if text is None:
        return None
    numbers = re.findall(r"\d+", text)
    if len(numbers) < 3:
        return None
    return tuple(int(n) for n in numbers[:3])


def parse_game_timer(text):
    """"MM:SS" -> elapsed milliseconds. None on anything that doesn't look
    like a plausible clock reading (garbage OCR, or seconds >= 60)."""
    match = GAME_TIMER_REGEX.search(text)
    if not match:
        return None
    minutes, seconds = int(match.group(1)), int(match.group(2))
    if seconds >= 60:
        return None
    return (minutes * 60 + seconds) * 1000


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


def presence_counts():
    counts = {}
    for page in connected_pages.values():
        counts[page] = counts.get(page, 0) + 1
    return counts


async def broadcast_presence():
    await broadcast({"type": "presence", "pages": presence_counts()})


def confirm_reading(key, raw_value):
    """None readings (nothing OCR'd this frame) don't enter the window at
    all -- they're not evidence of any particular value, just a miss, so
    they shouldn't dilute the vote. Only once the window is full does the
    majority value (if any single value holds more than half the window)
    get trusted -- a plurality isn't enough, since e.g. 2 different bad
    reads plus 1 good one in a window of 5 shouldn't let the good read win
    by default; it needs an actual majority of the samples agreeing."""
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


def apply_ocr_value(key, value):
    team, field = FIELD_MAP[key]
    if f"{team}.{field}" in locked_fields:
        return False
    if server_state[team][field] == value:
        return False
    server_state[team][field] = value
    return True


def apply_player_kda_value(key, value):
    """"t1p3_kda" -> livePlayers.team1[2] = {kills,deaths,assists}, all
    three fields from the one combined OCR read at once -- see
    PLAYER_KDA_KEYS' comment for why this isn't in FIELD_MAP. The key
    itself (e.g. "t1p3_kda") doubles as its own locked_fields entry --
    unlike apply_ocr_value's "team.field" strings, there's no separate
    namespacing needed since PLAYER_KDA_KEYS is already unique per player.
    A manual per-stat override (dashboard's "playerStat" message) can still
    patch just one of the three fields directly -- this function only
    handles the combined OCR path."""
    if key in locked_fields:
        return False
    team = f"team{key[1]}"
    player_idx = int(key[3:].split("_", 1)[0]) - 1
    kills, deaths, assists = value
    row = server_state["livePlayers"][team][player_idx]
    if row["kills"] == kills and row["deaths"] == deaths and row["assists"] == assists:
        return False
    row["kills"], row["deaths"], row["assists"] = kills, deaths, assists
    return True


def read_region(key, img_bgr):
    if key in PLAYER_KDA_KEYS_SET:
        return parse_kda(ocr_kda(img_bgr))
    text = ocr_number(img_bgr)
    if key.endswith("_gold"):
        return parse_gold(text)
    return parse_int(text)


def process_turtle_reading(text, now_ms):
    """Advances the turtle status state machine. Runs every cycle regardless
    of whether there's a fresh OCR reading this frame, since the countdown
    -> spawned -> idle transitions are timed locally, not re-OCR'd. Returns
    True if server_state changed (so the caller knows to save+broadcast)."""
    tt = server_state["turtleTimer"]
    changed = False

    if tt["status"] == "countdown" and tt["endsAt"] is not None and now_ms >= tt["endsAt"]:
        tt["status"] = "spawned"
        tt["spawnedUntil"] = now_ms + TURTLE_SPAWNED_DISPLAY_SECONDS * 1000
        tt["endsAt"] = None
        changed = True
    elif tt["status"] == "spawned" and tt["spawnedUntil"] is not None and now_ms >= tt["spawnedUntil"]:
        tt["status"] = "idle"
        tt["spawnedUntil"] = None
        changed = True

    # Only look for a new trigger while idle, so a still-visible toast
    # doesn't re-trigger mid-countdown or restart the spawned graphic while
    # it's already showing. "Spawned" is checked first since it's the
    # unambiguous, reliably-static signal — the countdown text is a bonus
    # if this game happens to show a pre-warning too.
    if tt["status"] == "idle" and text:
        if TURTLE_SPAWNED_REGEX.search(text):
            tt["status"] = "spawned"
            tt["spawnedUntil"] = now_ms + TURTLE_SPAWNED_DISPLAY_SECONDS * 1000
            tt["endsAt"] = None
            tt["lastRawText"] = text
            changed = True
        else:
            seconds = parse_turtle_countdown(text)
            if seconds is not None:
                tt["status"] = "countdown"
                tt["countdownSeconds"] = seconds
                tt["endsAt"] = now_ms + seconds * 1000
                tt["lastRawText"] = text
                changed = True

    return changed


def process_lord_reading(text, now_ms):
    """Same idea as process_turtle_reading, but spawned-only (no countdown
    phase) — reads the same shared announcement text each cycle. Returns
    True if server_state changed."""
    lt = server_state["lordTimer"]
    changed = False

    if lt["status"] == "spawned" and lt["spawnedUntil"] is not None and now_ms >= lt["spawnedUntil"]:
        lt["status"] = "idle"
        lt["spawnedUntil"] = None
        changed = True

    if lt["status"] == "idle" and text and LORD_SPAWNED_REGEX.search(text):
        lt["status"] = "spawned"
        lt["spawnedUntil"] = now_ms + LORD_SPAWNED_DISPLAY_SECONDS * 1000
        lt["lastRawText"] = text
        changed = True

    return changed


def process_game_timer_reading(text, now_ms):
    """A confirmed "MM:SS" OCR reading resyncs the clock to a locally
    ticking stopwatch (status "running", startedAt backdated so
    now - startedAt == confirmed) instead of freezing a static display
    value -- the old version set status "idle" with a frozen elapsedMs on
    every confirm, so the on-screen number only ever moved once per
    debounce window and visibly jumped in multi-second steps (5:02 -> 5:20)
    rather than counting up smoothly. Reusing "running" here means the
    client's existing per-second setInterval tick (same code path the
    manual Start button already drives) takes over between OCR confirms,
    and each new confirm just re-anchors it. Ignores a reconfirmation that
    lands within a second of where the local tick already predicts, so a
    clock that's tracking correctly doesn't broadcast (and visually hitch)
    every single debounce window for no reason. Manual Start/Pause/Reset on
    the dashboard still work as a fallback when this region isn't
    calibrated or the clock isn't on screen (e.g. a draft/loading screen).
    Returns True if server_state changed."""
    if text is None:
        return False
    parsed_ms = parse_game_timer(text)
    confirmed = confirm_reading(GAME_TIMER_REGION_KEY, parsed_ms)
    if confirmed is None:
        return False
    if GAME_TIMER_REGION_KEY in locked_fields:
        return False
    mc = server_state["matchClock"]
    new_started_at = now_ms - confirmed
    if mc["status"] == "running" and mc["startedAt"] is not None and abs(mc["startedAt"] - new_started_at) < 1000:
        return False
    mc["status"] = "running"
    mc["startedAt"] = new_started_at
    mc["elapsedMs"] = confirmed
    return True


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
            # PLAYER_KDA_KEYS rides along in the same flat dict/batch as
            # REGION_ORDER -- read_region() and confirm_reading() are both
            # already generic per-key, so as long as the region isn't
            # calibrated (the common case until calibrate_teamstats.py has
            # been run) this loop just skips all 10 of them for free.
            for key in REGION_ORDER + PLAYER_KDA_KEYS:
                region = regions.get(key)
                if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                    continue
                crops[key] = crop_to_bgr(sct, region)

            # Kill switch — flip "turtle_enabled" back to true in
            # config.json (and restart) once ready to resume; the
            # calibrated region and manual dashboard buttons are untouched
            # either way, this only pauses automatic OCR-driven detection.
            turtle_region = regions.get(TURTLE_REGION_KEY) if config.get("turtle_enabled", True) else None
            turtle_crop = None
            if turtle_region and turtle_region.get("w", 0) > 0 and turtle_region.get("h", 0) > 0:
                turtle_crop = crop_to_bgr(sct, turtle_region)

            game_timer_region = regions.get(GAME_TIMER_REGION_KEY)
            game_timer_crop = None
            if game_timer_region and game_timer_region.get("w", 0) > 0 and game_timer_region.get("h", 0) > 0:
                game_timer_crop = crop_to_bgr(sct, game_timer_region)

            # The slow part is the Tesseract subprocess call itself. Running
            # all regions through the thread pool at once means the total
            # wait per cycle is roughly one OCR call, not stacked up one by one.
            keys = list(crops.keys())
            ocr_tasks = [
                loop.run_in_executor(ocr_executor, read_region, key, crops[key])
                for key in keys
            ]
            # Optional one-off text regions, appended in a fixed order so
            # the results can be pulled back out by name below rather than
            # by fragile positional slicing.
            text_region_names = []
            for name, crop, ocr_fn in (
                ("turtle", turtle_crop, ocr_text),
                ("game_timer", game_timer_crop, ocr_timer),
            ):
                if crop is not None:
                    ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_fn, crop))
                    text_region_names.append(name)
            results = await asyncio.gather(*ocr_tasks)

            numeric_results = results[:len(keys)]
            text_results = dict(zip(text_region_names, results[len(keys):]))
            turtle_raw_text = text_results.get("turtle")
            game_timer_raw_text = text_results.get("game_timer")

            changed = False
            for key, parsed in zip(keys, numeric_results):
                confirmed = confirm_reading(key, parsed)
                if confirmed is None:
                    continue
                apply_fn = apply_player_kda_value if key in PLAYER_KDA_KEYS_SET else apply_ocr_value
                if apply_fn(key, confirmed):
                    changed = True

            if process_turtle_reading(turtle_raw_text, int(time.time() * 1000)):
                changed = True

            # Independent kill switch from turtle_enabled — lets Lord
            # detection be paused on its own without also disabling turtle,
            # even though they read the exact same crop/OCR pass.
            if config.get("lord_enabled", True) and process_lord_reading(turtle_raw_text, int(time.time() * 1000)):
                changed = True

            if process_game_timer_reading(game_timer_raw_text, int(time.time() * 1000)):
                changed = True

            # Push crop previews every couple of cycles so the dashboard can
            # show exactly what OCR is looking at, without flooding the socket.
            if frame_counter % 2 == 0:
                for key, img_bgr in crops.items():
                    data_url = crop_to_data_url(img_bgr)
                    if data_url:
                        await broadcast({"type": "crop_preview", "region": key, "image": data_url})
                if turtle_crop is not None:
                    data_url = crop_to_data_url(turtle_crop)
                    if data_url:
                        await broadcast({
                            "type": "crop_preview", "region": TURTLE_REGION_KEY,
                            "image": data_url, "text": turtle_raw_text or "",
                        })
                if game_timer_crop is not None:
                    data_url = crop_to_data_url(game_timer_crop)
                    if data_url:
                        await broadcast({
                            "type": "crop_preview", "region": GAME_TIMER_REGION_KEY,
                            "image": data_url, "text": game_timer_raw_text or "",
                        })

            if changed:
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            frame_counter += 1
            await asyncio.sleep(interval)



# ---------------------------------------------------------------------------
# Post Match screenshot import. One-shot (not polled), so this can afford a
# slower/heavier full-image OCR pass that the live per-frame loop can't. The
# battle report screenshot's exact pixel layout isn't something we can
# calibrate ahead of time the way the live regions are, so instead of
# guessing fixed crop boxes we find each player's name wherever Tesseract
# actually reads it (fuzzy-matched against the existing roster) and then
# only look for stat numbers near that name's row. Whatever it gets wrong,
# the dashboard shows for manual correction before anything is applied.
# ---------------------------------------------------------------------------

# Accepts both comma-grouped ("26,978", from the Data/damage screen) and
# plain digit ("11146", from the Overall/gold screen) formats — matching
# only the comma-grouped form was silently dropping every gold value.
STAT_NUMBER_REGEX = re.compile(r"^\d{1,3}(?:,\d{3})*$|^\d+$")


def decode_image_data_url(data_url):
    header, _, b64data = data_url.partition(",")
    raw = base64.b64decode(b64data)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def normalize_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def fuzzy_match_name(candidate_text, roster_names):
    """roster_names: [(team, index, name), ...]. Returns (team, index, name,
    score) for the best match, or None if nothing clears the threshold."""
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


# Full-page image_to_data is the expensive part of extraction (a second or
# more per call) — downscaling oversized uploads cuts that down a lot with
# negligible accuracy loss on this kind of bold UI text. Left alone for
# anything already at/under a normal screenshot size.
MAX_OCR_DIMENSION = 1920


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
    """Builds both word-level tokens (for numbers, which render as separate
    tokens even on the same visual row) and Tesseract's own line-grouped
    text (for names, which can legitimately span a couple of words like "DS
    Jazzz") from a SINGLE image_to_data() result — extract_battle_report
    used to call image_to_data twice on the same image for this, which
    doubled the wait for no benefit."""
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


def ocr_words(img_bgr):
    words, _ = _words_and_lines_from_data(_image_to_data(img_bgr))
    return words


def ocr_lines(img_bgr):
    _, lines = _words_and_lines_from_data(_image_to_data(img_bgr))
    return lines


def extract_battle_report(img_bgr, roster_names):
    """Returns one row per roster slot Tesseract's name-matching found (not
    necessarily all 10) — the dashboard fills gaps in manually. Column order
    within a row, left to right, is Hero Damage, Turret Damage, Damage
    Taken, Teamfight% (matches the game's own Battle Report screen), so
    among the numbers found near a matched name we take the 1st as hero
    damage and the 3rd as damage taken."""
    number_words, lines = _words_and_lines_from_data(_image_to_data(img_bgr))
    img_h, img_w = img_bgr.shape[:2]

    best_by_slot = {}
    for line in lines:
        match = fuzzy_match_name(line["text"], roster_names)
        if not match:
            continue
        team, idx, name, score = match
        slot = (team, idx)
        if slot not in best_by_slot or score > best_by_slot[slot][1]:
            best_by_slot[slot] = (line, score, name)

    number_words = [w for w in number_words if STAT_NUMBER_REGEX.match(w["text"])]

    rows = []
    for (team, idx), (line, score, name) in best_by_slot.items():
        row_cy = line["cy"]
        row_window = max(line["h"], img_h * 0.03) * 2.2
        same_row = [n for n in number_words if abs(n["cy"] - row_cy) <= row_window]
        if team == "team1":
            same_row = [n for n in same_row if n["cx"] < img_w * 0.5]
        else:
            same_row = [n for n in same_row if n["cx"] >= img_w * 0.5]
        same_row.sort(key=lambda n: n["x"])
        values = [int(n["text"].replace(",", "")) for n in same_row]
        rows.append({
            "team": team,
            "playerIndex": idx,
            "rosterName": name,
            "ocrName": line["text"],
            "matchScore": round(score, 2),
            "heroDamage": values[0] if len(values) > 0 else None,
            "damageTaken": values[2] if len(values) > 2 else None,
            "candidateNumbers": values,
        })
    rows.sort(key=lambda r: (r["team"], r["playerIndex"]))
    return rows


GOLD_CANDIDATE_REGEX = re.compile(r"(\d+(?:\.\d+)?)\s*[kK]\b|\b(\d{4,6})\b")


def extract_gold_candidates(img_bgr):
    """No reference layout to anchor on for this screenshot, so this just
    surfaces every gold-shaped number found (k-suffixed, or a bare
    4-6-digit number) with its position — the dashboard presents them as
    candidates rather than silently guessing which two are the team totals."""
    words = ocr_words(img_bgr)
    candidates = []
    for word in words:
        text = word["text"].replace(",", "").replace("$", "")
        match = GOLD_CANDIDATE_REGEX.search(text)
        if not match:
            continue
        if match.group(1):
            value = int(round(float(match.group(1)) * 1000))
        else:
            value = int(match.group(2))
        candidates.append({"value": value, "rawText": word["text"], "x": word["x"], "y": word["y"]})
    return candidates


def extract_gold_report(img_bgr, roster_names):
    """From the game's own post-match "Overall" screen (KDA + gold + rating
    per player — the one with per-player numbers, not a single team total
    printed anywhere). Gold is picked as the LARGEST number found on each
    matched name's row rather than a fixed column position, since gold
    sits on a different side of the KDA numbers for team1 vs team2 (the
    whole row mirrors around the center) but is always in the thousands
    while kills/deaths/assists are always small — so "biggest number in
    the row" is robust without needing to know the exact column order."""
    number_words, lines = _words_and_lines_from_data(_image_to_data(img_bgr))
    img_h, img_w = img_bgr.shape[:2]

    best_by_slot = {}
    for line in lines:
        match = fuzzy_match_name(line["text"], roster_names)
        if not match:
            continue
        team, idx, name, score = match
        slot = (team, idx)
        if slot not in best_by_slot or score > best_by_slot[slot][1]:
            best_by_slot[slot] = (line, score, name)

    number_words = [w for w in number_words if STAT_NUMBER_REGEX.match(w["text"])]

    rows = []
    for (team, idx), (line, score, name) in best_by_slot.items():
        row_cy = line["cy"]
        row_window = max(line["h"], img_h * 0.03) * 2.2
        same_row = [n for n in number_words if abs(n["cy"] - row_cy) <= row_window]
        if team == "team1":
            same_row = [n for n in same_row if n["cx"] < img_w * 0.5]
        else:
            same_row = [n for n in same_row if n["cx"] >= img_w * 0.5]
        values = sorted(int(n["text"].replace(",", "")) for n in same_row)
        rows.append({
            "team": team,
            "playerIndex": idx,
            "rosterName": name,
            "ocrName": line["text"],
            "matchScore": round(score, 2),
            "gold": values[-1] if values else None,
            "candidateNumbers": values,
        })
    rows.sort(key=lambda r: (r["team"], r["playerIndex"]))
    return rows


def parse_calibrated_number(text):
    """Like parse_int, but strips thousands separators first — parse_int's
    plain \\d+ would otherwise stop at the first one and silently return
    just the leading digits (e.g. "26,978" -> 26). Also strips periods:
    ocr_number()'s digit whitelist (0123456789.kK) has no comma at all, so
    Tesseract reads a comma-formatted number's comma AS a period
    ("26,978" -> "26.978") — not just cosmetic noise to ignore."""
    if not text:
        return None
    match = NUMBER_REGEX.search(text.replace(",", "").replace(".", "").replace(" ", ""))
    return int(match.group()) if match else None


# Post Match live screen regions — calibrated the exact same way as the
# in-game kills/gold HUD (calibrate_postmatch_gold.py / calibrate_postmatch_hero.py
# / calibrate_postmatch_kda.py, cv2.selectROI against the actual screen),
# just pointed at the post-game "Overall" (gold), "Data" (Hero Damage /
# Damage Taken), and K/D/A screens instead. This is what makes
# extraction fast: a handful of tiny screen crops through the proven
# digit-only OCR pipeline, not a full-page OCR pass over an uploaded image.
POSTGAME_GOLD_KEYS = [
    f"postgame_gold_{team}_{i}" for team in ("team1", "team2") for i in range(5)
]
POSTGAME_BATTLE_KEYS = [
    f"postgame_{field}_{team}_{i}"
    for team in ("team1", "team2") for i in range(5) for field in ("dealt", "taken")
]
# One combined box per player around their whole "K D A" reading on the
# post-match screen -- SPACE-separated here, unlike the in-game live HUD's
# "18/0/2" slash-separated version (calibrate_teamstats.py), so it needs
# its own parser (parse_kda_spaced below) even though the region-count
# shape (10, one per player) matches.
POSTGAME_KDA_KEYS = [
    f"postgame_kda_{team}_{i}" for team in ("team1", "team2") for i in range(5)
]


def postgame_regions_configured(keys):
    regions = config.get("regions", {})
    return all(
        regions.get(k, {}).get("w", 0) > 0 and regions.get(k, {}).get("h", 0) > 0
        for k in keys
    )


async def capture_postgame_regions(keys):
    """One-shot live screen grab (NOT the continuous poll loop) of the given
    calibrated regions, then the same fast digit-only OCR as the live HUD.
    Returns {key: value_or_None}."""
    loop = asyncio.get_running_loop()
    regions = config.get("regions", {})
    with mss.mss() as sct:
        crops = {
            key: crop_to_bgr(sct, regions[key])
            for key in keys
            if regions.get(key, {}).get("w", 0) > 0 and regions.get(key, {}).get("h", 0) > 0
        }
    result_keys = list(crops.keys())
    texts = await asyncio.gather(*[
        loop.run_in_executor(ocr_executor, ocr_number, crops[key]) for key in result_keys
    ])
    values = {key: parse_calibrated_number(text) for key, text in zip(result_keys, texts)}
    for key in keys:
        values.setdefault(key, None)
    return values


def build_gold_rows_from_capture(values):
    rows = []
    for team in ("team1", "team2"):
        for i in range(5):
            rows.append({
                "team": team, "playerIndex": i,
                "gold": values.get(f"postgame_gold_{team}_{i}"),
                "calibrated": True,
            })
    return rows


def build_battle_report_rows_from_capture(values):
    rows = []
    for team in ("team1", "team2"):
        for i in range(5):
            rows.append({
                "team": team, "playerIndex": i,
                "heroDamage": values.get(f"postgame_dealt_{team}_{i}"),
                "damageTaken": values.get(f"postgame_taken_{team}_{i}"),
                "calibrated": True,
            })
    return rows


async def capture_postgame_kda_regions():
    """Same one-shot live screen grab as capture_postgame_regions(), but
    each of the 10 regions holds a combined "K D A" reading (three numbers)
    instead of a single one, so it needs its own capture path rather than
    reusing the generic per-region single-value one."""
    loop = asyncio.get_running_loop()
    regions = config.get("regions", {})
    with mss.mss() as sct:
        crops = {
            key: crop_to_bgr(sct, regions[key])
            for key in POSTGAME_KDA_KEYS
            if regions.get(key, {}).get("w", 0) > 0 and regions.get(key, {}).get("h", 0) > 0
        }
    result_keys = list(crops.keys())
    texts = await asyncio.gather(*[
        loop.run_in_executor(ocr_executor, ocr_number, crops[key]) for key in result_keys
    ])
    values = {key: parse_kda_spaced(text) for key, text in zip(result_keys, texts)}
    for key in POSTGAME_KDA_KEYS:
        values.setdefault(key, None)
    return values


def build_postgame_kda_rows_from_capture(values):
    rows = []
    for team in ("team1", "team2"):
        for i in range(5):
            kda = values.get(f"postgame_kda_{team}_{i}")
            rows.append({
                "team": team, "playerIndex": i,
                "kills": kda[0] if kda else None,
                "deaths": kda[1] if kda else None,
                "assists": kda[2] if kda else None,
                "calibrated": True,
            })
    return rows


def build_roster_names():
    roster = []
    for team in ("team1", "team2"):
        for i, name in enumerate(server_state[team].get("players", [])):
            if name:
                roster.append((team, i, name))
    return roster


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
                if "graphicOverrides" in data:
                    server_state["graphicOverrides"] = data["graphicOverrides"]
                if "mvp" in data:
                    server_state["mvp"] = data["mvp"]
                if "teamStatsOverlay" in data:
                    server_state["teamStatsOverlay"] = data["teamStatsOverlay"]
                # Single-value override for one of the 30 K/D/A regions --
                # {team, playerIndex, stat, value}, paired with a
                # "t{n}p{n}_{stat}" lock key (same string apply_player_stat_value
                # checks against locked_fields) so a live OCR read doesn't
                # immediately clobber it back.
                if "playerStat" in data:
                    ps = data["playerStat"]
                    team, idx, stat = ps["team"], int(ps["playerIndex"]), ps["stat"]
                    server_state["livePlayers"][team][idx][stat] = int(ps["value"])
                for field in payload.get("lock", []):
                    locked_fields.add(field)
                for field in payload.get("unlock", []):
                    locked_fields.discard(field)
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "swap_sides":
                swap_team_sides()
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "turtle_manual_countdown":
                # Manual override — unlike the OCR path this isn't gated on
                # being idle, so the operator can always force a fresh
                # countdown even mid-countdown or mid-spawned-display.
                try:
                    seconds = int(payload.get("seconds"))
                except (TypeError, ValueError):
                    seconds = None
                if seconds and TURTLE_MIN_COUNTDOWN <= seconds <= TURTLE_MAX_COUNTDOWN:
                    now_ms = int(time.time() * 1000)
                    tt = server_state["turtleTimer"]
                    tt["status"] = "countdown"
                    tt["countdownSeconds"] = seconds
                    tt["endsAt"] = now_ms + seconds * 1000
                    tt["spawnedUntil"] = None
                    tt["lastRawText"] = "(manual)"
                    save_state()
                    await broadcast({
                        "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                    })
            elif payload.get("type") == "turtle_manual_spawned":
                now_ms = int(time.time() * 1000)
                tt = server_state["turtleTimer"]
                tt["status"] = "spawned"
                tt["endsAt"] = None
                tt["spawnedUntil"] = now_ms + TURTLE_SPAWNED_DISPLAY_SECONDS * 1000
                tt["lastRawText"] = "(manual)"
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "turtle_manual_reset":
                tt = server_state["turtleTimer"]
                tt["status"] = "idle"
                tt["endsAt"] = None
                tt["spawnedUntil"] = None
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "lord_manual_spawned":
                now_ms = int(time.time() * 1000)
                lt = server_state["lordTimer"]
                lt["status"] = "spawned"
                lt["spawnedUntil"] = now_ms + LORD_SPAWNED_DISPLAY_SECONDS * 1000
                lt["lastRawText"] = "(manual)"
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "lord_manual_reset":
                lt = server_state["lordTimer"]
                lt["status"] = "idle"
                lt["spawnedUntil"] = None
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "matchclock_start":
                now_ms = int(time.time() * 1000)
                mc = server_state["matchClock"]
                if mc["status"] != "running":
                    mc["status"] = "running"
                    mc["startedAt"] = now_ms - mc["elapsedMs"]  # resume from wherever it was paused
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "matchclock_pause":
                now_ms = int(time.time() * 1000)
                mc = server_state["matchClock"]
                if mc["status"] == "running":
                    mc["elapsedMs"] = now_ms - mc["startedAt"]
                    mc["status"] = "idle"
                    mc["startedAt"] = None
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "matchclock_reset":
                server_state["matchClock"] = {"status": "idle", "startedAt": None, "elapsedMs": 0}
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "matchclock_set":
                # Manual correction -- same backdated-startedAt resync
                # process_game_timer_reading() uses, so a manual fix also
                # keeps ticking smoothly forward from the corrected instant
                # instead of freezing. Also doubles as the Override
                # lock/unlock endpoint (same lock/unlock arrays every other
                # manual_update path uses) -- locking "game_timer" here stops
                # process_game_timer_reading() from fighting the correction
                # on its next confirmed OCR read.
                now_ms = int(time.time() * 1000)
                elapsed_ms = payload.get("elapsedMs")
                if elapsed_ms is not None:
                    try:
                        elapsed_ms = int(elapsed_ms)
                    except (TypeError, ValueError):
                        elapsed_ms = None
                if elapsed_ms is not None:
                    mc = server_state["matchClock"]
                    mc["status"] = "running"
                    mc["startedAt"] = now_ms - elapsed_ms
                    mc["elapsedMs"] = elapsed_ms
                for field in payload.get("lock", []):
                    locked_fields.add(field)
                for field in payload.get("unlock", []):
                    locked_fields.discard(field)
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "capture_postmatch_gold":
                # Fast path — have the post-game "Overall" screen up on the
                # monitor right now; this grabs the calibrated regions live,
                # same mechanism as the in-game kills/gold HUD. No image
                # upload involved at all, which is what makes it fast.
                try:
                    if postgame_regions_configured(POSTGAME_GOLD_KEYS):
                        values = await capture_postgame_regions(POSTGAME_GOLD_KEYS)
                        rows = build_gold_rows_from_capture(values)
                        await websocket.send(json.dumps({"type": "gold_extract_result", "rows": rows}))
                    else:
                        await websocket.send(json.dumps({
                            "type": "gold_extract_result", "rows": [], "candidates": [],
                            "error": "Gold regions aren't calibrated yet — run calibrate_postmatch_gold.py first.",
                        }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "gold_extract_result", "candidates": [], "error": str(e),
                    }))
            elif payload.get("type") == "capture_postmatch_battle_report":
                try:
                    if postgame_regions_configured(POSTGAME_BATTLE_KEYS):
                        values = await capture_postgame_regions(POSTGAME_BATTLE_KEYS)
                        rows = build_battle_report_rows_from_capture(values)
                        await websocket.send(json.dumps({"type": "battle_report_result", "rows": rows}))
                    else:
                        await websocket.send(json.dumps({
                            "type": "battle_report_result", "rows": [],
                            "error": "Battle report regions aren't calibrated yet — run calibrate_postmatch_hero.py first.",
                        }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "battle_report_result", "rows": [], "error": str(e),
                    }))
            elif payload.get("type") == "capture_postmatch_kda":
                try:
                    if postgame_regions_configured(POSTGAME_KDA_KEYS):
                        values = await capture_postgame_kda_regions()
                        rows = build_postgame_kda_rows_from_capture(values)
                        await websocket.send(json.dumps({"type": "postmatch_kda_result", "rows": rows}))
                    else:
                        await websocket.send(json.dumps({
                            "type": "postmatch_kda_result", "rows": [],
                            "error": "K/D/A regions aren't calibrated yet — run calibrate_postmatch_kda.py first.",
                        }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "postmatch_kda_result", "rows": [], "error": str(e),
                    }))
            elif payload.get("type") == "extract_battle_report":
                # Fallback path — an uploaded screenshot, fuzzy name-matched.
                # Slower (full-page OCR), only used when the live-capture
                # regions above haven't been calibrated.
                loop = asyncio.get_running_loop()
                try:
                    img = decode_image_data_url(payload.get("image", ""))
                    rows = await loop.run_in_executor(
                        ocr_executor, extract_battle_report, img, build_roster_names(),
                    )
                    await websocket.send(json.dumps({"type": "battle_report_result", "rows": rows}))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "battle_report_result", "rows": [], "error": str(e),
                    }))
            elif payload.get("type") == "extract_gold":
                loop = asyncio.get_running_loop()
                try:
                    img = decode_image_data_url(payload.get("image", ""))
                    rows = await loop.run_in_executor(
                        ocr_executor, extract_gold_report, img, build_roster_names(),
                    )
                    if rows:
                        await websocket.send(json.dumps({"type": "gold_extract_result", "rows": rows}))
                    else:
                        candidates = await loop.run_in_executor(ocr_executor, extract_gold_candidates, img)
                        await websocket.send(json.dumps({"type": "gold_extract_result", "rows": [], "candidates": candidates}))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "gold_extract_result", "candidates": [], "error": str(e),
                    }))
    finally:
        connected_clients.discard(websocket)
        connected_pages.pop(websocket, None)
        await broadcast_presence()


async def relay_client_loop():
    """Optional, additive: if config.json has a "relay" section with
    enabled=true, also connect OUT to a cloud relay (ocr/relay/server.py,
    deployed separately) as a client, so admins/overlays reachable over the
    internet get the same live state as anyone on localhost. Reuses
    handle_client() unchanged -- from ocr_engine's point of view the relay
    connection is just one more client in connected_clients, so broadcast()
    already reaches it with zero other code changes. No-op (returns
    immediately) if relay isn't configured, so local-only setups are
    completely unaffected."""
    relay_cfg = config.get("relay", {})
    if not relay_cfg.get("enabled"):
        return
    url = relay_cfg.get("url", "")
    token = relay_cfg.get("token", "")
    if not url or not token:
        print("Relay is enabled in config.json but 'url'/'token' aren't both set -- skipping relay connection.")
        return
    separator = "&" if "?" in url else "?"
    connect_url = f"{url}{separator}token={token}"
    while True:
        try:
            # Same max_size bump as the relay server itself needs (see
            # ocr/relay/server.py) -- the default 1 MiB limit was rejecting
            # state_sync the moment a team logo was uploaded, closing with
            # code 1009 and retrying forever without ever actually staying
            # connected.
            async with websockets.connect(connect_url, max_size=16 * 1024 * 1024) as relay_ws:
                print(f"Connected to cloud relay at {url}")
                await handle_client(relay_ws)
        except Exception as e:
            print(f"Relay connection lost/failed ({e}); retrying in 3s...")
        await asyncio.sleep(3)


async def main():
    host = config.get("server_host", "localhost")
    port = config.get("server_port", 8765)
    # Same max_size bump as above, for local (non-relay) connections --
    # dashboard.html/overlay.html connecting directly to localhost hit the
    # exact same default 1 MiB ceiling once a logo's in state_sync.
    async with websockets.serve(handle_client, host, port, max_size=16 * 1024 * 1024):
        print(f"OCR relay server running at ws://{host}:{port}")
        print("Open overlay/overlay.html in OBS as a Browser Source, and")
        print("dashboard/dashboard.html in a normal browser tab.")
        await asyncio.gather(ocr_loop(), relay_client_loop())


if __name__ == "__main__":
    asyncio.run(main())
