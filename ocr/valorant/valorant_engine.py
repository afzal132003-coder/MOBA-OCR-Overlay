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

CONFIG_PATH = Path(__file__).parent / "valorant_config.json"
STATE_PATH = Path(__file__).parent / "valorant_state.json"

# Calibration regions live in three separate files, one per category, so
# recalibrating post-match doesn't touch the in-game/char-select data and
# vice versa -- calibrate_valorant.py writes each category to its own file.
REGION_FILES = {
    "ingame": Path(__file__).parent / "valorant_regions_ingame.json",
    "character": Path(__file__).parent / "valorant_regions_character.json",
    "postmatch": Path(__file__).parent / "valorant_regions_postmatch.json",
}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    regions = {}
    for path in REGION_FILES.values():
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                regions.update(json.load(f))
    cfg["regions"] = regions
    return cfg


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

# liveScore (the main In-Game HUD's own round score) stays fully manual --
# an explicit request, OCR on it wasn't reliable enough to be worth it, set
# entirely through valSendManualUpdate from the dashboard, same as
# seriesScore always was.
#
# ocrRoundScore is a SEPARATE, reintroduced OCR reading -- an explicit
# request specifically for the Hoax Overlay, which wants to track the real
# on-screen score independently of whatever liveScore is editorially set
# to. Same two-digit-region-either-side-of-the-timer idea the original
# (removed) round-score OCR used, same digit pipeline as MOBA's kills/gold
# (ocr_number/parse_int), just under new region keys and feeding a
# different state field so it can't collide with liveScore's manual-only
# guarantee above.
OCR_SCORE_TEAM1_KEY = "valorant_ocr_team1_score"
OCR_SCORE_TEAM2_KEY = "valorant_ocr_team2_score"

# The round timer (e.g. "0:04", top-center between the two scores) --
# read purely as a SAFETY NET for the spike badge, not displayed anywhere
# itself. Any time the badge is "planted" and this timer jumps UP instead
# of continuing to count down, that means the round has moved on (spike
# defused, exploded, whatever ended it) -- turn the badge back to idle
# regardless of which exact end-of-round banner text fired, since guessing
# every possible "ATTACKERS WIN"-style banner wording is more fragile than
# just watching the timer reset. See ocr_loop()/track_round_timer().
ROUND_TIMER_KEY = "valorant_round_timer"

# The top-center announcement zone -- confirmed against a real clip (not
# assumed): "SPIKE PLANTED" is genuine on-screen text, same toast-style
# banner as MOBA's turtle/lord announcements. Reads generously (whole
# banner, not just the words) same reasoning as turtle_announcement's own
# calibration hint -- the banner can shift slightly.
ANNOUNCEMENT_REGION_KEY = "valorant_announcement"
# A SECOND, separately-calibrated region -- confirmed against real clips
# that the small toast above and this one are NOT the same screen spot:
# the big center-screen round-end result banner ("ATTACKERS/DEFENDERS WON"
# + "Spike Defused"/etc as a subtitle), later replaced in that same spot
# by "BUY PHASE". Read with the same generic ocr_text() pipeline as the
# toast, and its text is just concatenated onto the toast's own reading
# before either gets matched against any regex below -- so "SPIKE DEFUSED"
# said here counts exactly the same as it would in the small toast, and
# "BUY PHASE" (which only ever appears here, never in the small toast) is
# finally something the OCR loop actually sees.
ROUND_BANNER_REGION_KEY = "valorant_round_banner"
# "defused" only, not "defus\w*" -- Valorant shows a "next DEFUSING"
# progress bar WHILE a defuse is in progress (confirmed on a real clip),
# which must NOT fire this early; only the completed-defuse banner should,
# and that reads "DEFUSED" (past tense), not "DEFUSING".
SPIKE_PLANTED_REGEX = re.compile(r"spike\W*planted", re.IGNORECASE)
SPIKE_DEFUSED_REGEX = re.compile(r"spike\W*defused", re.IGNORECASE)
PLANT_DEFUSE_DISPLAY_SECONDS = 6

# Spike badge/bug banner triggers -- separate from (and faster than) the
# plant/defuse TOAST above, an explicit request. "planting" is the
# in-progress planting-animation text (distinct from "planted", past
# tense, so this can't double-fire off the completed-plant banner) --
# catching it turns the badge/bug on the moment the plant STARTS instead
# of waiting for the completed "SPIKE PLANTED" banner. "BUY PHASE" is the
# reliable marker printed at the start of every new round -- seeing it
# turns the badge/bug back off, a second, more immediate way to catch a
# round ending than the round-timer safety net (see ROUND_TIMER_KEY),
# which still runs as a fallback in case this text is ever missed.
SPIKE_PLANTING_REGEX = re.compile(r"\bplanting\b", re.IGNORECASE)
BUY_PHASE_REGEX = re.compile(r"buy\W*phase", re.IGNORECASE)

# Master switch for all of the above -- explicit request after several
# tuning passes (banner text, "planting"/"BUY PHASE", the round-timer red-
# icon color check) still weren't reliable enough in real games. False
# turns OCR OFF entirely for the plant/defuse popup, spike badge, and bug
# banner -- ocr_loop() skips reading/deciding anything for them (see the
# check near its top). The dashboard's manual buttons (including the
# Spike Bug + Badge Up/Down combo buttons) still fully control all three
# by hand either way, since those go through handle_client's message
# handlers, a completely separate code path from this flag. Flip back to
# True once the detection logic gets revisited.
PLANT_DEFUSE_AUTO_DETECT = False

connected_clients = set()
connected_pages = {}
ocr_executor = ThreadPoolExecutor(max_workers=4)


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
            "map": "",
        },

        # Fully manual -- an explicit request, OCR on it wasn't reliable
        # enough to be worth it. Set from the dashboard's Round Score card
        # only; can be a deliberately DIFFERENT number from the real game
        # score for creative/broadcast reasons, which is why it's kept
        # separate from ocrRoundScore below rather than reusing one field
        # for both. Drives the Round Score on the main In-Game HUD.
        "liveScore": {"team1": 0, "team2": 0},

        # A SECOND, independent round-score reading -- OCR-fed (same
        # digit-crop pipeline as MOBA's kills/gold), explicitly kept
        # separate from liveScore above because that one is deliberately
        # manual/editorial and this one is meant to track the real on-
        # screen score. Currently only consumed by the Hoax Overlay screen
        # within overlay/valorant_ingame.html (own file until merged into
        # the main HUD's file, see that file's own header comment) --
        # liveScore, not this, still drives the main In-Game HUD screen.
        # Calibrate via valo-ingame (valorant_ocr_team1_score/valorant_ocr_
        # team2_score).
        "ocrRoundScore": {"team1": 0, "team2": 0},

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

        # A separate, PERSISTENT spike-planted indicator (the small hex
        # badge hanging off the round-score bar) -- distinct from
        # plantDefuse above (a 6-second toast). This has no auto-hide: once
        # the spike is planted it should stay visible on the HUD for the
        # rest of the round, not vanish after 6 seconds like the toast
        # does. Driven off the SAME OCR detection as plantDefuse (see
        # process_plant_defuse_reading) -- no separate calibration needed,
        # despite the badge being visually a different element. Manual
        # push/push-down (spike_badge_show/spike_badge_hide) still works as
        # an operator override, same relationship as plantDefuse's own
        # manual buttons. mode is "idle" (white, below context text),
        # "planted" (red, above context text), or "hidden" (not drawn at
        # all -- a manual-only override, OCR never sets this one, for
        # rounds where the operator doesn't want the badge competing for
        # attention at all).
        "spikeBadge": {"mode": "idle"},

        # A separate, fully-manual banner from spikeBadge above -- two
        # complete pre-made graphics (SPIKEPLANTED_BUG.png / spikedefused.
        # png, both 1920x1080 with the ribbon art pre-positioned at the
        # same spot on that canvas) toggled entirely by the operator, no
        # OCR involved at all. Lives on the main In-Game HUD screen only.
        # Resets to hidden on load, same as every other "currently showing"
        # manual overlay toggle in this project.
        "bugBanner": {"mode": "hidden"},

        # The full hoax-overlay.png-styled bar (own team plates + score) --
        # lives on the SAME overlay/valorant_ingame.html file as the main
        # HUD (merged into it, an explicit request -- was its own separate
        # OBS source/file until then), but MUTUALLY EXCLUSIVE with it:
        # pushing this up (hoax_overlay_show/hide) pulls the main HUD
        # screen off, not stacked on top of it -- see that file's own
        # applyState(), which reads this field directly for both screens'
        # visibility, no separate "which screen is showing" field needed.
        # spikeBadge above still separately controls the badge's own idle/
        # planted color+animation, but that badge only ever renders on the
        # main HUD screen, not this one.
        "hoaxOverlay": {"visible": False},

        # Map veto banner (assets/mapcurrentnextdecider_left.png +
        # _right.png -- CURRENT/NEXT/DECIDER and BHARAT GAMING MASTERS
        # labels baked into that art, split into two independently-
        # positioned corner pieces per an explicit correction, only the
        # map name + picking team's logo per slot are dynamic) -- lives on
        # the SAME Hoax
        # Overlay page/OBS source as hoaxOverlay above but with its OWN
        # independent push/pull toggle (mapVetoOverlay.visible), an
        # explicit request, since the two are shown at different points in
        # a broadcast (map veto vs mid-match score) even though they share
        # a page. team is "team1"/"team2"/None (None = no logo shown, e.g.
        # for a decider neither side picked).
        "mapVeto": {
            "current": {"map": "", "team": None},
            "next": {"map": "", "team": None},
            "decider": {"map": "", "team": None},
        },
        "mapVetoOverlay": {"visible": False},

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

        # Which of the two layouts valorant_mvp.html is currently showing --
        # a momentary display toggle (like hoaxOverlay/spikeBadge), NOT part
        # of the mvp/headToHead player selections above, which persist
        # across restarts on their own. The MVP and Head-to-Head content
        # live in the SAME overlay page/OBS source (an explicit request --
        # the sponsor branding strip stays on screen through the
        # transition instead of the whole source swapping), so this just
        # picks which content is currently animated in. "hidden" pulls
        # both off screen (sponsor branding still stays visible). Resets
        # to "mvp" on load, same as every other "currently showing" toggle.
        "mvpScreenMode": "mvp",

        "graphicOverrides": {},

        # Per-agent portrait pan/zoom, independent of graphicOverrides above
        # (which is keyed by fixed DOM element/slot id) -- this is keyed by
        # AGENT NAME instead, since the same agent can land in any slot
        # depending on who picks them. Two separate sub-objects because
        # Character Pick's slot box (159x179) and Team Chemistry Stats' row
        # box (99x76) are different aspect ratios, so the same crop doesn't
        # necessarily look right in both. {dx, dy} are object-position
        # percentage-point shifts (pan), {scale} is an extra zoom multiplier
        # on top of the pre-cropped portrait's own baseline framing -- see
        # the Character Fixing tab in the dashboard's Graphic Fixing page.
        "characterFraming": {"characterpick": {}, "teamchemistry": {}},
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
            state["spikeBadge"] = default_state()["spikeBadge"]
            state["bugBanner"] = default_state()["bugBanner"]
            state["mvpScreenMode"] = default_state()["mvpScreenMode"]
            state["hoaxOverlay"] = default_state()["hoaxOverlay"]
            state["mapVetoOverlay"] = default_state()["mapVetoOverlay"]
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

# Round-timer jump tracking for the spike badge safety net (see
# ROUND_TIMER_KEY above). _last_round_timer_seconds is a FROZEN baseline
# from before a suspected jump -- it deliberately does NOT update on every
# poll while a jump is being confirmed, only once the jump is confirmed or
# abandoned. That matters because once a real round transition happens the
# new timer immediately resumes counting DOWN (e.g. 100, 99, 98...), so
# only the very first post-jump reading is actually higher than the one
# right before it -- comparing every frame against the immediately-prior
# frame (instead of a frozen baseline) would see that first jump, then see
# "99 < 100" on the next frame and wrongly conclude nothing happened.
# Comparing against a frozen pre-jump baseline instead lets consecutive
# elevated-but-decreasing readings still count as confirming the same
# jump, which is what a real round transition looks like.
_last_round_timer_seconds = None
_timer_jump_streak = 0
TIMER_JUMP_CONFIRM_FRAMES = 2


def track_round_timer(seconds):
    """Call once per poll with the raw (unconfirmed) timer reading, or
    None if unreadable this frame. Returns True if this reading confirms
    the round just moved on (only meaningful while spikeBadge is
    "planted" -- caller decides what to do with True)."""
    global _last_round_timer_seconds, _timer_jump_streak
    if seconds is None:
        return False
    if _last_round_timer_seconds is None:
        _last_round_timer_seconds = seconds
        return False
    jumped = seconds > _last_round_timer_seconds + 2
    if jumped:
        _timer_jump_streak += 1
    else:
        _timer_jump_streak = 0
        _last_round_timer_seconds = seconds  # only move the baseline when NOT mid-jump
    if _timer_jump_streak >= TIMER_JUMP_CONFIRM_FRAMES:
        _last_round_timer_seconds = seconds
        _timer_jump_streak = 0
        return True
    return False


# Debounce for the badge/bug's OCR-text and color triggers -- an explicit
# fix after the badge started popping up "unnecessarily": a single bad
# frame (an OCR misread that happens to contain "planting"/"buy phase", or
# a stray red-pixel-ratio blip in the timer crop) is enough to flip a
# one-shot check, so each trigger now has to see the SAME condition true on
# TRIGGER_CONFIRM_FRAMES consecutive polls before it's trusted -- same
# "don't trust a single frame" reasoning as track_round_timer above, just
# per-condition instead of one frozen baseline.
_trigger_streaks = {}
TRIGGER_CONFIRM_FRAMES = 3


def confirm_trigger(name, condition):
    """condition is this frame's raw (unconfirmed) True/False reading for
    trigger `name`. Returns True only once the same True has been seen
    TRIGGER_CONFIRM_FRAMES times in a row; any False resets that streak to
    zero immediately, so a real, sustained state (the banner/icon actually
    on screen for more than a couple frames) still confirms quickly, but an
    isolated misread can't."""
    streak = _trigger_streaks.get(name, 0)
    streak = streak + 1 if condition else 0
    _trigger_streaks[name] = streak
    return streak >= TRIGGER_CONFIRM_FRAMES


# Debounce for ocrRoundScore (see OCR_SCORE_TEAM1_KEY/OCR_SCORE_TEAM2_KEY
# above) -- same "don't trust a single frame" idea as confirm_trigger, but
# for a numeric reading rather than a yes/no condition: the SAME digit
# value has to be read on VALUE_CONFIRM_FRAMES consecutive polls before
# it's accepted, so one misread frame (OCR briefly reading "8" as "3", say)
# can't flicker the displayed score.
_value_streaks = {}
VALUE_CONFIRM_FRAMES = 3


def confirm_value(name, value):
    """value is this frame's raw (unconfirmed) reading for `name`, or None
    if unreadable this frame. Returns the confirmed value once the same
    reading has been seen VALUE_CONFIRM_FRAMES times in a row, else None
    (nothing to act on yet this frame)."""
    prev_value, streak = _value_streaks.get(name, (None, 0))
    if value is None:
        _value_streaks[name] = (None, 0)
        return None
    streak = streak + 1 if value == prev_value else 1
    _value_streaks[name] = (value, streak)
    return value if streak >= VALUE_CONFIRM_FRAMES else None


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


TESS_CONFIG_TIMER = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789: "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
ROUND_TIMER_REGEX = re.compile(r"(\d{1,2}):(\d{2})")


def ocr_round_timer(img_bgr):
    """Returns total seconds parsed from an "M:SS" reading, or None. Not a
    debounced/confirmed value like the scores -- this is read raw, every
    poll, purely to detect when it jumps UP (see track_round_timer)."""
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_TIMER)
    match = ROUND_TIMER_REGEX.search(text.strip())
    if not match:
        return None
    minutes, seconds = int(match.group(1)), int(match.group(2))
    if seconds > 59:
        return None
    return minutes * 60 + seconds


# Once the spike is planted, Valorant swaps the M:SS countdown in this SAME
# box for a red spike icon (confirmed against a real clip) -- no digits
# left for ocr_round_timer to read at all. Detecting that icon by color
# instead (a plain red-pixel-ratio check, same "match by color, not literal
# pixels" reasoning as match_agent_by_color, just simpler since this is one
# distinctive color rather than 25+ portraits to tell apart) is a fast,
# purely-visual THIRD trigger for the spike badge/bug, on top of the
# "planting" text trigger and the full "SPIKE PLANTED" banner -- reuses the
# round timer's existing calibration, nothing new to set up.
#
# Color ratio ALONE turned out unreliable in both directions: a high
# threshold (0.40, tried first after color-only false-positived on ordinary
# HUD red) missed the real icon too, because a lot of the icon's own crop
# is dark background/outline, not solid red fill -- the red-colored portion
# alone doesn't reliably clear a high bar. So this now ALSO requires
# ocr_round_timer() to have found no M:SS digits in the same crop this
# frame (see the call site in ocr_loop) -- real countdown digits always
# parse fine, so "digits failed to parse" + "some red present" together is
# a much more specific icon signal than either alone, and lets the color
# side of the check use a lower, more forgiving ratio.
# Loosened again -- 0.15 at r>110/g+60/b+60 was still missing the real icon
# on a real clip. The icon is a red RING/badge shape around a light center
# glyph, not a solid red disc, so even a generously-drawn crop is a lot of
# non-red pixels (badge interior, dark stone background behind it) no
# matter how the color match itself is tuned -- demanding a big fraction of
# the WHOLE crop be red was the wrong shape of check. Lowered the ratio
# and loosened the color match to catch darker/desaturated reds too
# (compression artifacts, HUD transparency).
SPIKE_ICON_RED_RATIO = 0.06  # fraction of pixels that must look "red enough"
# Any crop clearing THIS ratio is red enough to trust on its own, no matter
# what ocr_round_timer() thinks it read -- a real M:SS reading essentially
# never has this much red in it, so this is a safety valve in case the
# "digits failed to parse" gate (see detect_spike_icon's caller in
# ocr_loop) is ever wrong about a frame that's obviously the icon.
SPIKE_ICON_RED_RATIO_OVERRIDE = 0.30


def spike_icon_red_ratio(img_bgr):
    """The raw fraction of "red enough" pixels in a round-timer crop --
    split out from detect_spike_icon() so the OCR loop can also surface
    this number in the dashboard's crop preview (see ocr_loop), letting the
    SPIKE_ICON_RED_RATIO threshold above get tuned against a real observed
    value instead of guessed blind."""
    b = img_bgr[:, :, 0].astype(np.int16)
    g = img_bgr[:, :, 1].astype(np.int16)
    r = img_bgr[:, :, 2].astype(np.int16)
    red_mask = (r > 80) & (r > g + 30) & (r > b + 30)
    return float(red_mask.mean())


def detect_spike_icon(img_bgr):
    """Returns True if this round-timer crop looks like the red spike icon
    rather than plain white/gray countdown digits."""
    return spike_icon_red_ratio(img_bgr) >= SPIKE_ICON_RED_RATIO


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
# Post-match scoreboard grid read. Valorant's post-match "Individually
# Sorted" screen is ONE list of all 10 players mixed together and sorted by
# ACS, with team told apart only by row background color -- there's no way
# to know from the screen alone which physical row is which roster player.
# This used to fuzzy-match OCR'd names against the roster to recover
# identity; per an explicit request that was dropped as unreliable. Instead
# this reads the 10 rows' stats directly off a per-cell calibrated grid (see
# calibrate_valorant.py's valo-postmatch step -- 10 rows x 5 stat columns,
# 50 boxes total) in on-screen top-to-bottom order, with NO identity
# attached at all. The dashboard's Import Stats table lets the operator
# assign each row to a player via a dropdown and reorder rows with the
# up/down arrows -- same "read the numbers, you tell us who" split MOBA's
# own screenshot import already uses.
# ---------------------------------------------------------------------------

# The K/D/A column OCRs as one slash-joined token, e.g. "18/3/5".
KDA_TRIPLE_REGEX = re.compile(r"^(\d+)/(\d+)/(\d+)$")

TESS_CONFIG_KDA = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789/ "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)

# Real on-screen left-to-right order: ACS, K/D/A, Econ, First Bloods,
# Plants -- each is now its OWN individually-calibrated box (drag-one-box,
# same flow as the ingame/char-select regions) rather than a computed grid
# subdivision, per an explicit request that the grid-line-drag approach
# wasn't reliable enough.
POSTMATCH_GRID_COLS = ["acs", "kda", "econ", "fb", "plants"]
POSTMATCH_GRID_ROWS = 10


def decode_image_data_url(data_url):
    header, _, b64data = data_url.partition(",")
    raw = base64.b64decode(b64data)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def ocr_kda_triple(img_bgr):
    """Returns (kills, deaths, assists) or None for a "K/D/A" cell."""
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_KDA).strip()
    match = KDA_TRIPLE_REGEX.match(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def extract_postmatch_grid(regions_cfg, img_bgr=None):
    """Reads all calibrated postmatch cells (up to 10 rows x 5 stat
    columns, 50 individually-calibrated boxes), either cropped out of an
    already-loaded img_bgr (the upload path, treated as a full monitor-
    resolution screenshot so the calibrated screen coordinates still line
    up) or captured live off-screen (the capture path). Returns a list of
    row dicts in top-to-bottom on-screen order -- a row with no calibrated
    cells at all is skipped entirely, but a row missing just some of its 5
    cells still comes back with 0 for whichever ones aren't calibrated/
    readable."""
    def read_cell(region_key, sct):
        region = regions_cfg.get(region_key)
        if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
            return None
        if img_bgr is not None:
            x, y, w, h = region["x"], region["y"], region["w"], region["h"]
            crop = img_bgr[max(0, y):y + h, max(0, x):x + w]
            return crop if crop.size else None
        return crop_to_bgr(sct, region)

    def build_rows(sct):
        rows = []
        for row in range(POSTMATCH_GRID_ROWS):
            keys = {col: f"valorant_postmatch_row{row}_{col}" for col in POSTMATCH_GRID_COLS}
            if not any(regions_cfg.get(k) for k in keys.values()):
                continue

            kda_crop = read_cell(keys["kda"], sct)
            kda = ocr_kda_triple(kda_crop) if kda_crop is not None else None
            kills, deaths, assists = kda if kda else (0, 0, 0)

            acs_crop = read_cell(keys["acs"], sct)
            acs = parse_int(ocr_number(acs_crop)) if acs_crop is not None else None

            econ_crop = read_cell(keys["econ"], sct)
            econ = parse_int(ocr_number(econ_crop)) if econ_crop is not None else None

            fb_crop = read_cell(keys["fb"], sct)
            first_bloods = parse_int(ocr_number(fb_crop)) if fb_crop is not None else None

            plants_crop = read_cell(keys["plants"], sct)
            plants = parse_int(ocr_number(plants_crop)) if plants_crop is not None else None

            rows.append({
                "rowIndex": row,
                "acs": acs or 0, "kills": kills, "deaths": deaths, "assists": assists,
                "econ": econ or 0, "firstBloods": first_bloods or 0, "plants": plants or 0,
            })
        return rows

    if img_bgr is not None:
        return build_rows(None)
    with mss.mss() as sct:
        return build_rows(sct)


# ---------------------------------------------------------------------------
# Character-select agent auto-detection. NOT text OCR -- a color-signature
# best-guess match against the known agent portrait images, since agent
# identity on that screen is conveyed by art, not text. Deliberately a
# "suggest, then let the operator confirm/correct via Agent Pick" tool,
# same safety net every other extraction feature in this project already
# uses -- color-only matching is inherently less certain than reading
# text, especially since these agent portraits are a different art style
# (fan-art renders) than whatever the live character-select screen's own
# icons actually look like. Treat a match as a starting point, not a
# verdict. AGENT_FILE_MAP is the same name->filename table as the
# dashboard/overlay JS copies -- kept in sync manually, same as those.
# ---------------------------------------------------------------------------

AGENTS_DIR = Path(__file__).parent.parent.parent / "overlay" / "assets" / "Valorant Agents"
AGENT_FILE_MAP = {
    "Astra": "Astra.png", "Breach": "Breach.png", "Brimstone": "Brimstone.png",
    "Chamber": "Chamber.png", "Fade": "Fade.png", "Harbor": "Harbor.png",
    "Jett": "Jett.png", "KAY/O": "Kayo.png", "Killjoy": "KillJoy.png",
    "Neon": "Neon.png", "Omen": "Omen.png", "Phoenix": "Phoenix.png",
    "Raze": "Raze.png", "Reyna": "Reyna.png", "Sage": "Sage.png",
    "Skye": "Skye.png", "Sova": "Sova.png", "Cypher": "Sypher.png",
    "Viper": "Viper.png", "Yoru": "Yoru.png",
    "Iso": "Iso.png", "Waylay": "Waylay.png", "Gekko": "Gekko.png", "Tejo": "Tejo.png",
    "Clove": "Clove.png", "Deadlock": "Deadlock.png", "Vyse": "Vyse.png",
}

_agent_signatures = None  # lazily built once: {agent_name: [16 avg-BGR cells]}


def _color_signature(img_bgr, grid=4):
    """Average color per cell of a grid x grid split -- captures rough
    color AND spatial layout (tells a mostly-dark agent with a bright head
    apart from one that's bright all over), more robust than a single
    global average color."""
    h, w = img_bgr.shape[:2]
    cells = []
    for gy in range(grid):
        for gx in range(grid):
            y0, y1 = h * gy // grid, h * (gy + 1) // grid
            x0, x1 = w * gx // grid, w * (gx + 1) // grid
            cell = img_bgr[y0:y1, x0:x1]
            cells.append((0.0, 0.0, 0.0) if cell.size == 0 else tuple(cell.reshape(-1, 3).mean(axis=0)))
    return cells


def _build_agent_signatures():
    global _agent_signatures
    if _agent_signatures is not None:
        return _agent_signatures
    sigs = {}
    for name, filename in AGENT_FILE_MAP.items():
        path = AGENTS_DIR / filename
        if not path.exists():
            continue
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            continue
        if img.ndim == 3 and img.shape[2] == 4:
            # Composite onto mid-gray so transparent margins (agent
            # portraits carry a lot of it) don't skew the signature toward
            # whatever the crop's own background color happens to be.
            alpha = img[:, :, 3:4].astype(float) / 255.0
            bgr = img[:, :, :3].astype(float)
            gray_bg = np.full_like(bgr, 128.0)
            img = (bgr * alpha + gray_bg * (1 - alpha)).astype(np.uint8)
        sigs[name] = _color_signature(img)
    _agent_signatures = sigs
    return sigs


def match_agent_by_color(img_bgr):
    """Returns (agent_name, confidence 0-1) for the closest-matching known
    agent portrait, or (None, 0.0) if no signatures could be built at all.
    confidence is a rough 1-minus-normalized-distance readout, not a
    calibrated probability -- good enough to flag "not sure" cases for the
    operator, not to trust blindly."""
    sigs = _build_agent_signatures()
    if not sigs:
        return None, 0.0
    query = _color_signature(img_bgr)
    best_name, best_dist = None, None
    for name, sig in sigs.items():
        dist = sum(
            sum((a - b) ** 2 for a, b in zip(qc, sc)) ** 0.5
            for qc, sc in zip(query, sig)
        )
        if best_dist is None or dist < best_dist:
            best_name, best_dist = name, dist
    max_dist = 441.7 * 16  # sqrt(3*255^2) per cell, times 16 cells
    confidence = max(0.0, 1 - (best_dist / max_dist))
    return best_name, round(confidence, 2)


def capture_and_match_charselect(regions_cfg):
    """One-shot live capture of all 10 calibrated character-select slots
    (fixed positions -- unlike the post-match scoreboard, this screen
    doesn't reorder by performance, so per-slot calibration is valid
    here), each matched independently against the known agent portraits."""
    results = []
    with mss.mss() as sct:
        for team in ("team1", "team2"):
            for i in range(5):
                key = f"valorant_charselect_{team}_{i}"
                region = regions_cfg.get(key)
                if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                    results.append({"team": team, "playerIndex": i, "agent": None, "confidence": 0.0})
                    continue
                img = crop_to_bgr(sct, region)
                agent, confidence = match_agent_by_color(img)
                results.append({"team": team, "playerIndex": i, "agent": agent, "confidence": confidence})
    return results


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


async def broadcast_to_page(page, message):
    """Same as broadcast(), but only to clients connected as `page` (see
    connected_pages, populated from the ?page= query param each client
    connects with). Crop previews (base64 PNG images, several per OCR poll)
    are only ever read by dashboard.html's own crop_preview handler -- no
    OBS overlay HUD page, and no cloud relay hop, has any use for them, so
    sending them to every connected_clients socket (broadcast()'s default)
    was pure wasted bandwidth on every one of those. This is an explicit
    fix for that -- an operator reported high data usage."""
    targets = [c for c, p in connected_pages.items() if p == page]
    if not targets:
        return
    data = json.dumps(message)
    await asyncio.gather(*[c.send(data) for c in targets], return_exceptions=True)


def presence_counts():
    counts = {}
    for page in connected_pages.values():
        counts[page] = counts.get(page, 0) + 1
    return counts


async def broadcast_presence():
    await broadcast({"type": "presence", "pages": presence_counts()})


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
    s["ocrRoundScore"]["team1"], s["ocrRoundScore"]["team2"] = (
        s["ocrRoundScore"]["team2"], s["ocrRoundScore"]["team1"],
    )
    s["attackingTeam"] = "team2" if s.get("attackingTeam") == "team1" else "team1"

    for slot in s.get("mapVeto", {}).values():
        if slot.get("team") in ("team1", "team2"):
            slot["team"] = "team2" if slot["team"] == "team1" else "team1"

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
        elif f.startswith("ocrRoundScore.team1"):
            renamed.add("ocrRoundScore.team2" + f[len("ocrRoundScore.team1"):])
        elif f.startswith("ocrRoundScore.team2"):
            renamed.add("ocrRoundScore.team1" + f[len("ocrRoundScore.team2"):])
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
    Every OCR-text condition below is run through confirm_trigger() (see
    above) -- called unconditionally, every frame, so its streak keeps
    building even while the toast is "shown" and this function isn't
    acting on it yet. Returns True if server_state changed."""
    pd = server_state["plantDefuse"]
    changed = False

    if pd["status"] == "shown" and pd["shownUntil"] is not None and now_ms >= pd["shownUntil"]:
        pd["status"] = "idle"
        pd["shownUntil"] = None
        changed = True

    defused_confirmed = confirm_trigger("spike_defused_banner", bool(text) and SPIKE_DEFUSED_REGEX.search(text) is not None)
    planted_confirmed = confirm_trigger("spike_planted_banner", bool(text) and SPIKE_PLANTED_REGEX.search(text) is not None)
    planting_confirmed = confirm_trigger("planting", bool(text) and SPIKE_PLANTING_REGEX.search(text) is not None)
    buy_phase_confirmed = confirm_trigger("buy_phase", bool(text) and BUY_PHASE_REGEX.search(text) is not None)

    if pd["status"] == "idle":
        attacking = server_state.get("attackingTeam", "team1")
        defending = "team2" if attacking == "team1" else "team1"
        event_type = None
        team = None
        if defused_confirmed:
            event_type, team = "defuse", defending
        elif planted_confirmed:
            event_type, team = "plant", attacking
        if event_type:
            pd["status"] = "shown"
            pd["shownUntil"] = now_ms + PLANT_DEFUSE_DISPLAY_SECONDS * 1000
            pd["team"] = team
            pd["type"] = event_type
            server_state["spikeBadge"]["mode"] = "planted" if event_type == "plant" else "idle"
            # The bug banner (SPIKEPLANTED_BUG / spikedefused.png art) now
            # follows the same OCR trigger as the spike badge, an explicit
            # request -- no separate manual push needed for the common
            # case, the dashboard buttons stay as an override for when OCR
            # misses it.
            server_state["bugBanner"]["mode"] = "planted" if event_type == "plant" else "defused"
            changed = True

    # Spike badge/bug banner: independent of the toast state machine above
    # (checked every frame, not gated behind pd["status"] == "idle") --
    # "planting" turns them on the moment the plant animation starts,
    # "BUY PHASE" turns them back off at the start of the next round. Each
    # only acts if it'd actually change something, both to avoid spamming
    # state_sync broadcasts every frame the text stays on screen, and so
    # BUY PHASE doesn't stomp an operator's explicit "hidden" override.
    if planting_confirmed and server_state["spikeBadge"]["mode"] != "planted":
        server_state["spikeBadge"]["mode"] = "planted"
        server_state["bugBanner"]["mode"] = "planted"
        changed = True
    elif buy_phase_confirmed and server_state["spikeBadge"]["mode"] == "planted":
        server_state["spikeBadge"]["mode"] = "idle"
        server_state["bugBanner"]["mode"] = "hidden"
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
                if "mapVeto" in data:
                    server_state["mapVeto"] = data["mapVeto"]
                if "graphicOverrides" in data:
                    server_state["graphicOverrides"] = data["graphicOverrides"]
                if "characterFraming" in data:
                    server_state["characterFraming"] = data["characterFraming"]
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

            elif msg_type == "spike_badge_show":
                server_state["spikeBadge"]["mode"] = "planted"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "spike_badge_hide":
                server_state["spikeBadge"]["mode"] = "idle"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "spike_badge_disappear":
                server_state["spikeBadge"]["mode"] = "hidden"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "bug_banner_show_planted":
                server_state["bugBanner"]["mode"] = "planted"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "bug_banner_show_defused":
                server_state["bugBanner"]["mode"] = "defused"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "bug_banner_hide":
                server_state["bugBanner"]["mode"] = "hidden"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "mvp_screen_show_headtohead":
                server_state["mvpScreenMode"] = "headtohead"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "mvp_screen_show_mvp":
                server_state["mvpScreenMode"] = "mvp"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "mvp_screen_hide":
                server_state["mvpScreenMode"] = "hidden"
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "hoax_overlay_show":
                server_state["hoaxOverlay"]["visible"] = True
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "hoax_overlay_hide":
                server_state["hoaxOverlay"]["visible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "map_veto_show":
                server_state["mapVetoOverlay"]["visible"] = True
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "map_veto_hide":
                server_state["mapVetoOverlay"]["visible"] = False
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
                        ocr_executor, extract_postmatch_grid, config.get("regions", {}), img,
                    )
                    await websocket.send(json.dumps({"type": "postmatch_scoreboard_extracted", "rows": rows}))

            elif msg_type == "capture_postmatch_scoreboard":
                # Fast path -- reads the 50-cell calibrated grid live (not
                # the continuous poll loop, a one-shot capture same as
                # MOBA's capture_postmatch_gold/battle_report).
                regions = config.get("regions", {})
                if not any(k.startswith("valorant_postmatch_row") for k in regions):
                    await websocket.send(json.dumps({
                        "type": "postmatch_scoreboard_extracted", "rows": [],
                        "error": "Postmatch grid isn't calibrated yet -- run calibrate_valorant.py valo-postmatch first.",
                    }))
                else:
                    loop = asyncio.get_running_loop()
                    rows = await loop.run_in_executor(
                        ocr_executor, extract_postmatch_grid, regions, None,
                    )
                    await websocket.send(json.dumps({"type": "postmatch_scoreboard_extracted", "rows": rows}))

            elif msg_type == "capture_charselect":
                loop = asyncio.get_running_loop()
                results = await loop.run_in_executor(
                    ocr_executor, capture_and_match_charselect, config.get("regions", {}),
                )
                await websocket.send(json.dumps({"type": "charselect_detected", "results": results}))
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
            changed = False

            # ocrRoundScore -- see OCR_SCORE_TEAM1_KEY/OCR_SCORE_TEAM2_KEY
            # and confirm_value() above. Runs unconditionally (NOT gated
            # behind PLANT_DEFUSE_AUTO_DETECT below -- this is a separate
            # feature, independent of the plant/defuse pipeline). Respects
            # an operator lock the same way the old apply_live_score() did,
            # via the shared locked_fields set. score_crops keeps each
            # crop+raw reading around for the dashboard preview broadcast
            # further down, same pattern as timer_crop/timer_seconds.
            score_crops = {}
            for team, key in (("team1", OCR_SCORE_TEAM1_KEY), ("team2", OCR_SCORE_TEAM2_KEY)):
                region = regions.get(key)
                if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                    continue
                crop = crop_to_bgr(sct, region)
                raw_value = parse_int(await loop.run_in_executor(ocr_executor, ocr_number, crop))
                score_crops[key] = (crop, raw_value)
                confirmed = confirm_value(key, raw_value)
                if confirmed is None or f"ocrRoundScore.{team}" in locked_fields:
                    continue
                if server_state["ocrRoundScore"][team] != confirmed:
                    server_state["ocrRoundScore"][team] = confirmed
                    changed = True

            if frame_counter % 6 == 0 and "valorant_dashboard" in connected_pages.values():
                for key, (crop, raw_value) in score_crops.items():
                    data_url = crop_to_data_url(crop)
                    if data_url:
                        await broadcast_to_page("valorant_dashboard", {
                            "type": "crop_preview", "region": key,
                            "image": data_url, "text": str(raw_value) if raw_value is not None else "",
                        })

            if not PLANT_DEFUSE_AUTO_DETECT:
                # Whole plant/defuse/spike-badge OCR pipeline switched off
                # -- explicit request after several tuning passes still
                # weren't reliable enough. The spike badge, bug banner, and
                # plant/defuse popup are all still fully controllable by
                # hand from the dashboard buttons (including the combo
                # buttons); this just stops OCR from reading/deciding
                # anything for them. Flip PLANT_DEFUSE_AUTO_DETECT back to
                # True above once the detection logic gets revisited.
                if changed:
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
                frame_counter += 1
                await asyncio.sleep(interval)
                continue

            announcement_region = regions.get(ANNOUNCEMENT_REGION_KEY)
            announcement_crop = None
            if announcement_region and announcement_region.get("w", 0) > 0 and announcement_region.get("h", 0) > 0:
                announcement_crop = crop_to_bgr(sct, announcement_region)

            round_banner_region = regions.get(ROUND_BANNER_REGION_KEY)
            round_banner_crop = None
            if round_banner_region and round_banner_region.get("w", 0) > 0 and round_banner_region.get("h", 0) > 0:
                round_banner_crop = crop_to_bgr(sct, round_banner_region)

            timer_region = regions.get(ROUND_TIMER_KEY)
            timer_crop = None
            if timer_region and timer_region.get("w", 0) > 0 and timer_region.get("h", 0) > 0:
                timer_crop = crop_to_bgr(sct, timer_region)

            ocr_tasks = []
            if announcement_crop is not None:
                ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_text, announcement_crop))
            if round_banner_crop is not None:
                ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_text, round_banner_crop))
            if timer_crop is not None:
                ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_round_timer, timer_crop))
            results = await asyncio.gather(*ocr_tasks)

            idx = 0
            announcement_raw_text = None
            if announcement_crop is not None:
                announcement_raw_text = results[idx]
                idx += 1
            round_banner_raw_text = None
            if round_banner_crop is not None:
                round_banner_raw_text = results[idx]
                idx += 1
            timer_seconds = results[idx] if timer_crop is not None else None

            # Concatenated, not read separately -- "SPIKE DEFUSED"/"BUY
            # PHASE" said in either region counts the same either way (see
            # ROUND_BANNER_REGION_KEY above for why there are two regions
            # at all).
            combined_announcement_text = " ".join(
                t for t in (announcement_raw_text, round_banner_raw_text) if t
            )

            if process_plant_defuse_reading(combined_announcement_text, int(time.time() * 1000)):
                changed = True

            # Spike icon in the round-timer box -- see detect_spike_icon
            # above. Normally requires BOTH some red color present AND no
            # M:SS digits parsed from the very same crop this frame -- real
            # countdown digits always parse fine, so pairing "digits
            # failed" with "red present" is what makes a forgiving color
            # threshold safe. An overwhelming red ratio (SPIKE_ICON_RED_
            # RATIO_OVERRIDE) skips that gate entirely -- a safety valve in
            # case the digit-parse check is ever wrong about an obviously-
            # icon frame. Then run through confirm_trigger() same as the
            # OCR-text triggers in process_plant_defuse_reading, so a
            # single stray frame can't pop the badge on its own -- only
            # acts once confirmed AND it'd actually flip the mode.
            icon_seen = False
            if timer_crop is not None:
                red_ratio = spike_icon_red_ratio(timer_crop)
                icon_seen = (
                    red_ratio >= SPIKE_ICON_RED_RATIO_OVERRIDE
                    or (timer_seconds is None and red_ratio >= SPIKE_ICON_RED_RATIO)
                )
            icon_confirmed = confirm_trigger("spike_icon", icon_seen)
            if icon_confirmed and server_state["spikeBadge"]["mode"] != "planted":
                server_state["spikeBadge"]["mode"] = "planted"
                server_state["bugBanner"]["mode"] = "planted"
                changed = True

            # Spike badge safety net -- see ROUND_TIMER_KEY/track_round_timer
            # above. Only acts while the badge is actually showing planted;
            # a timer jump the rest of the time is just a normal new round
            # starting up, nothing to correct.
            if track_round_timer(timer_seconds) and server_state["spikeBadge"]["mode"] == "planted":
                server_state["spikeBadge"]["mode"] = "idle"
                server_state["bugBanner"]["mode"] = "hidden"
                changed = True

            # Crop previews (base64 PNG images) are ONLY read by the
            # dashboard's own calibration UI -- no OBS overlay page, and no
            # cloud relay hop, ever does anything with them. Skipping the
            # whole block (no encoding work, no send) whenever no dashboard
            # tab is connected, and sending what IS generated only to
            # dashboard sockets (broadcast_to_page, not broadcast), was an
            # explicit fix for high data usage -- these were going out to
            # every connected client (every OBS source, plus over the
            # internet on any relay connection) for no reason. Also dropped
            # to every 6th frame (was every 2nd) -- calibration preview
            # doesn't need to be smoother than that.
            if frame_counter % 6 == 0 and "valorant_dashboard" in connected_pages.values():
                if announcement_crop is not None:
                    data_url = crop_to_data_url(announcement_crop)
                    if data_url:
                        await broadcast_to_page("valorant_dashboard", {
                            "type": "crop_preview", "region": ANNOUNCEMENT_REGION_KEY,
                            "image": data_url, "text": announcement_raw_text or "",
                        })
                if round_banner_crop is not None:
                    data_url = crop_to_data_url(round_banner_crop)
                    if data_url:
                        await broadcast_to_page("valorant_dashboard", {
                            "type": "crop_preview", "region": ROUND_BANNER_REGION_KEY,
                            "image": data_url, "text": round_banner_raw_text or "",
                        })
                if timer_crop is not None:
                    data_url = crop_to_data_url(timer_crop)
                    if data_url:
                        # When digits parsed, show them; otherwise show the
                        # spike-icon red-ratio reading instead of leaving
                        # this blank -- lets SPIKE_ICON_RED_RATIO get tuned
                        # against a real number instead of guessed blind.
                        if timer_seconds is not None:
                            timer_debug_text = f"{timer_seconds}s"
                        else:
                            timer_debug_text = f"no digits (red={spike_icon_red_ratio(timer_crop):.2f})"
                        await broadcast_to_page("valorant_dashboard", {
                            "type": "crop_preview", "region": ROUND_TIMER_KEY,
                            "image": data_url, "text": timer_debug_text,
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
