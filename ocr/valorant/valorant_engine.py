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

# Calibration regions live in separate files, one per category, so
# recalibrating post-match doesn't touch the in-game/char-select data and
# vice versa -- calibrate_valorant.py writes each category to its own file.
REGION_FILES = {
    "ingame": Path(__file__).parent / "valorant_regions_ingame.json",
    "character": Path(__file__).parent / "valorant_regions_character.json",
    "postmatch": Path(__file__).parent / "valorant_regions_postmatch.json",
    "livestats": Path(__file__).parent / "valorant_regions_livestats.json",
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
# Set only while relay_client_loop() below has an active connection to a
# cloud relay -- see broadcast_to_page()'s own comment for why this needs
# special handling separate from connected_pages.
relay_websocket = None
# Raised from 4 -- the Live Player Stats panel (LIVESTATS_* below) reads
# 40 individual cells (2 teams x 5 rows x 4 fields), well beyond what 4
# workers can keep up with on the fast main-loop cadence the rest of this
# module uses. Still runs on its own slower cadence (see ocr_loop), not
# every poll, but needs more parallelism to finish that batch quickly
# when it does run.
ocr_executor = ThreadPoolExecutor(max_workers=8)

# Live Player Stats panel -- see the "liveStats" comment in default_state()
# above for the row/assignment split. TEAMS/ROWS/FIELDS describe the 40
# calibrated cells (valorant_livestats_team{1,2}_row{0-4}_{field}), same
# per-cell-box calibration flow as the post-match grid (calibrate_
# valorant.py's valo-postmatch), just a new category (valo-livestats)
# since this reads a completely different screen (the live Tab-held
# scoreboard, not the post-match "Individually Sorted" list).
LIVESTATS_TEAMS = ("team1", "team2")
LIVESTATS_ROWS = 5
LIVESTATS_FIELDS = ("kills", "deaths", "assists", "coins")
# How often (in ocr_loop iterations) the 40-cell livestats batch runs --
# NOT every frame like the round timer/spike checks. Lowered from 10 to 4
# after an explicit "refresh rate feels slow" report -- at the default
# poll_interval_seconds (0.15s) this is now roughly a 0.6s cadence (was
# ~1.5s). Still comfortably affordable: the 40-cell batch itself runs in
# well under 0.6s wall time with ocr_executor's 8 workers, so this doesn't
# risk the loop falling behind. Also directly helps VALUE_CONFIRM_WINDOW's
# own convergence speed below, since a smaller real-world time-per-window
# means a genuine stat change (a kill, a death) reaches its confirmed
# majority sooner.
LIVESTATS_POLL_EVERY_N_FRAMES = 4


def default_player_stat_row():
    return {
        "agent": "", "acs": 0, "kills": 0, "deaths": 0, "assists": 0,
        "econ": 0, "firstBloods": 0, "plants": 0, "defuses": 0,
    }


def default_live_stat_row():
    return {"kills": 0, "deaths": 0, "assists": 0, "coins": 0}


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
        #
        # leftSide picks WHICH of two full background graphics is drawn --
        # assets/leftattacker.png (left/team1 plate red, right/team2 plate
        # teal) or assets/leftdefender.png (colors swapped the other way,
        # left/team1 teal, right/team2 red). Deliberately a manual
        # operator toggle, NOT tied to attackingTeam above -- an explicit
        # request, this is a pure visual/color choice made by hand, not
        # inferred from game state.
        "hoaxOverlay": {"visible": False, "leftSide": "attacker"},

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

        # Live Player Stats panel -- own separate OBS source (overlay/
        # valorant_playerstats.html), own push/pull (liveStatsOverlay.
        # visible). Continuously OCR-fed (LIVESTATS_TEAMS/_ROWS/_FIELDS
        # below) off the in-game Tab-held scoreboard, which an observer's
        # PC keeps held throughout the match -- a genuinely different
        # source screen from the post-match "Individually Sorted" list
        # (see extract_postmatch_grid), so it gets its own calibration
        # category (valo-livestats) and its own state, not reused postMatch
        # data (though agent/name identity below IS still pulled from the
        # shared team1/team2.players and postMatch.players[..].agent, same
        # single-source-of-truth as every other overlay).
        #
        # liveStats[team][row] holds the raw OCR reading for whichever
        # PHYSICAL row position (0-4, top to bottom on screen) that is --
        # NOT necessarily roster player `row`, since the live scoreboard
        # can re-sort/shift position as a match goes on. liveStatsAssign
        # [team][row] says which roster player index (0-4) is CURRENTLY
        # sitting at that physical row -- an explicit manual mapping the
        # operator corrects from the dashboard when the in-game order
        # changes, same "read the numbers, you tell us who" split as
        # postMatch's own Import Stats table, just persistent/live instead
        # of a one-shot capture. Defaults to identity (row i -> player i)
        # as a reasonable starting guess.
        "liveStats": {
            "team1": [default_live_stat_row() for _ in range(5)],
            "team2": [default_live_stat_row() for _ in range(5)],
        },
        "liveStatsAssign": {
            "team1": [0, 1, 2, 3, 4],
            "team2": [0, 1, 2, 3, 4],
        },
        "liveStatsOverlay": {"visible": False},

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
            # Only "visible" resets -- leftSide is a real operator setting
            # (which side is colored attacker/defender), not a "currently
            # showing" toggle, so it should survive a restart same as any
            # other saved setting, not silently snap back to "attacker".
            state["hoaxOverlay"]["visible"] = False
            state["mapVetoOverlay"] = default_state()["mapVetoOverlay"]
            # liveStatsAssign (the row->roster mapping) is a real operator
            # setting too, same reasoning as leftSide above -- only visible
            # resets.
            state["liveStatsOverlay"]["visible"] = False
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


# Debounce for ocrRoundScore/liveStats -- a ROLLING WINDOW majority vote,
# ported directly from ocr_engine.py's confirm_reading() (see that file's
# own comment) after the SAME symptom showed up here: Kills/Deaths/
# Assists/Coins were staying stuck at 0 (or visibly wrong) even on cells
# whose crop preview looked perfectly legible. The original version here
# required the SAME value on VALUE_CONFIRM_FRAMES consecutive polls,
# resetting to zero on ANY single differing read -- exactly the failure
# mode ocr_engine.py already hit and fixed for its own gold/kills HUD: a
# live, compressed video feed misreads an occasional frame even when the
# text is genuinely legible (compression speckle, a glow/shimmer text
# style, a momentary encode artifact), and a strict back-to-back streak
# can end up NEVER reconfirming even though most individual frames read
# correctly, because one bad frame among many good ones keeps resetting
# the count to zero. A window tolerates that -- only a genuine MAJORITY
# of the last VALUE_CONFIRM_WINDOW readings needs to agree, not an
# unbroken run.
#
# Lowered from 5 to 3 after a real capture showed the window itself
# lagging behind a GENUINE value change: Deaths' raw OCR text correctly
# read "13" on screen while the confirmed/displayed value was still stuck
# at "12" -- old "12" readings were still a big enough share of the
# window to keep winning the vote for several more polls even after the
# real value had already changed. Kills/Deaths/Assists/Coins are
# monotonically-increasing match counters (they only go up mid-match),
# so there's little value in a wide window's extra noise tolerance once
# a real change happens -- old, now-stale readings should get flushed out
# and lose the vote quickly, not linger. 3 (majority = 2 of 3) still
# rejects a single one-off misread, just converges to a real change much
# faster than 5 did, especially paired with the faster poll cadence above.
VALUE_CONFIRM_WINDOW = 3
_value_windows = {}
_value_last_confirmed = {}

# Sanity guards for Live Player Stats specifically -- added after watching
# a real live match through the relay (see monitor script used during that
# session) and catching two failure modes the debounce window alone still
# let through:
#
# 1. Coins hit clearly impossible 5-digit readings twice in 90 seconds
#    (e.g. raw text "92050" for a cell that was legitimately ~2050 both
#    before and after) -- the debounce window happened to filter both out
#    that time, but nothing guarantees a bad frame like that can't win 2
#    of 3 in the window on a worse run. Valorant hard-caps credits at
#    9000, so anything outside 0-9000 is categorically impossible and can
#    be discarded before it ever enters the vote, same as a None/unreadable
#    frame.
# 2. Kills/Deaths/Assists are monotonically non-decreasing for the whole
#    match (only livestats_clear, an explicit operator action, ever lowers
#    them) -- yet the CONFIRMED, post-debounce value was observed visibly
#    flickering down and back up over the course of one match (Deaths
#    3->7->3, Kills 6->5, Assists 7->2 and separately 9->0), proving a
#    misread can still win a 2-of-3 majority. A confirmed value that's
#    LOWER than what's already showing can only be a misread and is
#    rejected outright. A confirmed value that JUMPS UP implausibly far in
#    one go (the same capture also caught Kills briefly "confirm" to 20
#    for a cell that was 2 both before and after) is equally impossible
#    for a single confirm cycle and rejected the same way -- without this
#    half of the guard, the pure "never decreases" rule would have
#    permanently locked that cell onto the wrong 20, since the real value
#    of 2 could then never be accepted again (it's a decrease). Coins is
#    deliberately excluded from this second guard -- credits legitimately
#    DROP when a player buys, so only the absolute range check above
#    applies there.
COINS_MAX_CREDITS = 9000
# Confirmed directly by the operator watching a live match: Coins is
# always a multiple of 50 (Valorant's own credit granularity), never an
# arbitrary number. Every reading gets rounded to the nearest multiple
# before entering the debounce window -- see the rounding site in
# ocr_loop() below.
COINS_ROUND_TO = 50
MONOTONIC_LIVESTATS_FIELDS = ("kills", "deaths", "assists")
MONOTONIC_MAX_JUMP = 8


def confirm_value(name, value):
    """value is this frame's raw (unconfirmed) reading for `name`, or None
    if unreadable this frame (a miss -- doesn't enter the window at all,
    so it can't dilute the vote). Returns the confirmed value once the
    window is full AND a value holds a genuine majority (more than half),
    and only once per change (returns None again on subsequent polls that
    keep agreeing with what's already confirmed) -- otherwise None."""
    if value is None:
        return None
    window = _value_windows.setdefault(name, deque(maxlen=VALUE_CONFIRM_WINDOW))
    window.append(value)
    if len(window) < window.maxlen:
        return None
    winner, count = Counter(window).most_common(1)[0]
    if count * 2 <= len(window):
        return None
    if _value_last_confirmed.get(name) == winner:
        return None
    _value_last_confirmed[name] = winner
    return winner


def preprocess(img_bgr, upscale=4):
    """Same digit-tuned pipeline as ocr_engine.py's preprocess() -- see
    that file for the reasoning behind each step (border padding, upscale,
    median blur, OTSU threshold, auto-invert). CLAHE (adaptive local
    contrast) runs before the blur/threshold -- an explicit fix for
    text-on-semi-transparent-background cells (e.g. the Player Stats
    Coins column, white text over a translucent panel with live gameplay
    showing through it) where the actual contrast between text and
    background varies frame to frame and can be quite low, which a
    single global OTSU threshold handles poorly. CLAHE is local/adaptive
    rather than a single global adjustment, so it pulls faint text out
    more reliably without needing to know the real contrast level ahead
    of time, and is a no-op in practice on crops that already have good
    contrast (nothing here to enhance), so applying it to every digit
    crop uniformly is safe rather than needing a per-field opt-in."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
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


# Coins (Player Stats) and Econ (post-match grid) both show a currency
# icon (¤) immediately butted up against the real digits, e.g. "¤4,350" --
# confirmed against a real capture: with TESS_CONFIG's digit-only
# whitelist, Tesseract can't decline to recognize the icon as "not a
# digit" and reliably misreads it as a spurious leading digit (real
# "4,350" -> raw "14350"). Tesseract DOES still segment the icon as its
# own character rather than fusing it into the first real digit (confirmed
# by the same capture: the misread output has one extra leading digit, not
# a garbled first digit), which means there's a genuine pixel-level gap
# between them to find and crop out -- more reliable than trying to guess
# which digit(s) in the OUTPUT TEXT are the icon after the fact, since the
# icon doesn't necessarily misread as the same digit every time.
CURRENCY_ICON_MAX_WIDTH_FRAC = 0.30


def _bright_text_mask(img_bgr, percentile=80):
    """Binary ink=255 mask isolating the game's white Coins/Econ text,
    given the FULL COLOR crop (not grayscale -- this needs the color
    information, see below).

    Ranks every pixel by score = brightness(V) - saturation(S) and keeps
    the top `percentile` -- something bright AND essentially colorless
    (real white text: high V, low S) scores highest; a bright but
    COLORED pixel (a saturated background tint, or a colored object
    behind the semi-transparent panel) scores much lower even at full
    brightness, since its own saturation cancels it out. Confirmed
    directly against a real capture with a bright red distractor patch
    deliberately placed in the background: the digit glyphs stayed
    solid while the distractor scored too low to appear in the mask.

    This replaced an earlier version that computed the same two signals
    (brightness, saturation) but as two separate HARD cutoffs -- reject
    anything with saturation above a fixed number, THEN take a brightness
    percentile of what's left. That worked on the one real sample it was
    tuned against, but broke down at the actual small capture size
    (~70-90px wide cells): anti-aliasing/video-compression blur pushes
    almost every real text pixel's saturation up somewhat, so the hard
    cutoff was leaving only a handful of "pure" pixels -- computing a
    percentile from that tiny, noisy sample produced an unstable cutoff,
    visible directly as a mask that was nearly empty even when the raw
    crop clearly showed legible digits (e.g. a clean "750"/"1,450"/
    "1,950" in the raw preview reading back as 100/150/8). A single
    continuous score with one percentile rank doesn't have that
    cliff-edge failure mode -- a pixel that's 90% of the way to "text"
    still contributes 90% of its score instead of being discarded
    outright, and synthetic testing at the real crop resolution (with
    added noise/blur to mimic video compression) showed it holding a
    stable ~450 ink pixels across noise levels where the old version
    degraded from ~100 down to ~65 ink pixels as noise increased -- the
    difference between solid, OCR-able glyph shapes and scattered dots.
    A light morphological close bridges small gaps within a single
    glyph's anti-aliased edge without merging separate glyphs (a real
    inter-glyph gap is still several pixels wide at this scale)."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(np.int16)
    val = hsv[:, :, 2].astype(np.int16)
    score = val - sat
    cutoff = np.percentile(score, percentile)
    mask = np.where(score >= cutoff, 255, 0).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    return mask


def strip_leading_icon(img_bgr, gap_px=1, return_cut=False):
    """Returns img_bgr with a leading icon-shaped blob cropped off, if one
    is found; otherwise returns img_bgr unchanged (safe to call on a cell
    that's already just digits -- e.g. box was calibrated tight enough, or
    there's no icon in this field at all).

    Width alone can't truly tell a narrow icon apart from a narrow real
    leading digit (a genuine "16" would look the same shape-wise as
    "icon+6") -- this is only safe to use on fields where the icon is
    KNOWN to always be present in the crop (Coins/Econ, where the operator
    has confirmed there's no way to calibrate it out -- see
    ocr_currency_number's own callers). Requiring at least 3 blobs total
    (icon + 2+ real digits) before stripping anything is a cheap extra
    guard against the narrowest realistic failure mode -- a coins/econ
    value that's genuinely just a single or double digit number with an
    icon that happens to not segment as its own blob this frame -- without
    needing to tell icon and digit shapes apart, which isn't reliable from
    width alone. Uses _bright_text_mask(), not OTSU, for the same noisy-
    background reason as preprocess_currency() below -- OTSU's own blob
    boundaries were unreliable on the same cells that needed this most.

    return_cut=True additionally returns the column index the crop was cut
    at (or None if nothing was stripped) -- purely a debugging aid so the
    dashboard preview can draw a marker showing exactly where the cut
    landed, to tell apart "cut right after the icon" (correct) from "cut
    into the first real digit too" (the icon+digit1 blobs merged, e.g. from
    video compression blur bridging their gap -- see the crop_preview
    composite built around this in ocr_loop())."""
    thresh = _bright_text_mask(img_bgr)
    col_has_ink = (thresh > 0).any(axis=0)
    xs = np.where(col_has_ink)[0]
    if len(xs) == 0:
        return (img_bgr, None) if return_cut else img_bgr
    blobs = []
    start = xs[0]
    prev = xs[0]
    for x in xs[1:]:
        if x - prev > gap_px:
            blobs.append((start, prev))
            start = x
        prev = x
    blobs.append((start, prev))
    if len(blobs) < 3:
        # not enough blobs to confidently call this icon+digits
        return (img_bgr, None) if return_cut else img_bgr
    first_w = blobs[0][1] - blobs[0][0] + 1
    if first_w > img_bgr.shape[1] * CURRENCY_ICON_MAX_WIDTH_FRAC:
        # first blob too wide to be just the icon -- leave alone
        return (img_bgr, None) if return_cut else img_bgr
    crop_start = max(0, blobs[1][0] - gap_px)
    if return_cut:
        return img_bgr[:, crop_start:], crop_start
    return img_bgr[:, crop_start:]


def preprocess_currency(img_bgr, upscale=4):
    """Coins/Econ-specific variant of preprocess() -- see
    _bright_text_mask()'s own comment for why a brightness-minus-
    saturation score replaces OTSU here specifically. No CLAHE step (the
    shared preprocess() below still uses it) -- an explicit removal after
    it turned out to actively hurt at this pipeline's real crop size
    (~70-90px wide cells): CLAHE's default 8x8 tile grid means each tile
    covers only a few pixels on a crop this small, local contrast
    "enhancement" over that few a sample amplifies noise rather than
    revealing real signal. _bright_text_mask()'s percentile rank already
    adapts to each crop's own brightness distribution, making a separate
    contrast-equalization pass redundant here.

    Border+upscale happen AFTER masking here, not before like preprocess()
    does it -- an explicit fix after realizing the border was hurting the
    percentile threshold specifically: a 10px replicated border, then 4x
    upscaled, becomes a LARGE fraction of the total image (for a typical
    small cell, often more pixels than the original crop content), all of
    it just the edge pixel's own color repeated. _bright_text_mask()'s
    percentile is computed over the WHOLE image it's given, so computing
    it on the bordered+upscaled version let a big block of replicated
    background pixels skew what "the brightest ~15%" even means. Masking
    the small original crop FIRST keeps the percentile honest, then the
    resulting BINARY mask gets upscaled (NEAREST, not CUBIC -- keeps it
    crisp instead of introducing gray blending at edges) and a plain black
    border (BORDER_CONSTANT, not REPLICATE -- there's no real ink at a
    mask's edge to replicate)."""
    mask = _bright_text_mask(img_bgr)
    mask = cv2.resize(mask, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_NEAREST)
    mask = cv2.medianBlur(mask, 3)
    mask = cv2.copyMakeBorder(mask, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=0)
    return mask


def ocr_currency_number(img_bgr):
    """Same as ocr_number(), but for currency cells (Coins/Econ) that have
    a leading ¤ icon touching the digits (strip_leading_icon()) AND can
    have a noisy/textured background OTSU handles badly
    (preprocess_currency(), not the shared preprocess())."""
    stripped = strip_leading_icon(img_bgr)
    processed = preprocess_currency(stripped)
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

            # crops kept per column so a preview image can be sent back
            # alongside the extracted numbers -- an explicit request, so
            # the operator can actually SEE what each cell captured (e.g.
            # whether Econ's crop caught real digits or just an icon)
            # instead of only ever seeing the resulting 0.
            crop_previews = {}

            kda_crop = read_cell(keys["kda"], sct)
            kda = ocr_kda_triple(kda_crop) if kda_crop is not None else None
            kills, deaths, assists = kda if kda else (0, 0, 0)
            if kda_crop is not None:
                crop_previews["kda"] = crop_to_data_url(kda_crop)

            acs_crop = read_cell(keys["acs"], sct)
            acs = parse_int(ocr_number(acs_crop)) if acs_crop is not None else None
            if acs_crop is not None:
                crop_previews["acs"] = crop_to_data_url(acs_crop)

            econ_crop = read_cell(keys["econ"], sct)
            # Currency-aware path -- Econ has the same leading ¤ icon
            # touching its digits as Coins does, see ocr_currency_number.
            econ = parse_int(ocr_currency_number(econ_crop)) if econ_crop is not None else None
            if econ_crop is not None:
                crop_previews["econ"] = crop_to_data_url(econ_crop)

            fb_crop = read_cell(keys["fb"], sct)
            first_bloods = parse_int(ocr_number(fb_crop)) if fb_crop is not None else None
            if fb_crop is not None:
                crop_previews["fb"] = crop_to_data_url(fb_crop)

            plants_crop = read_cell(keys["plants"], sct)
            plants = parse_int(ocr_number(plants_crop)) if plants_crop is not None else None
            if plants_crop is not None:
                crop_previews["plants"] = crop_to_data_url(plants_crop)

            rows.append({
                "rowIndex": row,
                "acs": acs or 0, "kills": kills, "deaths": deaths, "assists": assists,
                "econ": econ or 0, "firstBloods": first_bloods or 0, "plants": plants or 0,
                "crops": crop_previews,
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
    OBS overlay HUD page has any use for them, so sending them to every
    connected_clients socket (broadcast()'s default) was pure wasted
    bandwidth on every one of those. This is an explicit fix for that --
    an operator reported high data usage.

    The relay socket (if connected) is ALWAYS included, regardless of
    `page` -- from this engine's own point of view that one connection
    represents however many real dashboard/overlay tabs are actually on
    the other end of it (it never gets its own accurate page= identity),
    so this engine has no way to know locally whether skipping it would
    wrongly deprive a legitimate relay-connected recipient. Instead the
    message carries "_target_pages" so the RELAY server itself (see
    ocr/relay/server.py) can narrow its own fan-out down to just the
    matching page(s) on its end, where the real per-client page identity
    actually is known."""
    targets = [c for c, p in connected_pages.items() if p == page]
    if relay_websocket is not None and relay_websocket not in targets:
        targets.append(relay_websocket)
    if not targets:
        return
    data = json.dumps({**message, "_target_pages": [page]})
    await asyncio.gather(*[c.send(data) for c in targets], return_exceptions=True)


def dashboard_connected():
    """Whether it's worth generating dashboard-only data at all (crop
    previews) this cycle -- checked BEFORE broadcast_to_page() even gets
    called, so it needs the same relay awareness as that function: a
    direct local connection tagged "valorant_dashboard" is a sure thing,
    but a relay connection's own tag is always "unknown" (see
    broadcast_to_page's comment), so this engine has no way to confirm a
    dashboard tab specifically is open on the far end of it -- only that
    the relay itself is reachable. Erring toward "yes, generate it" when
    relay is connected (even if no one's actually looking at the
    calibration screen right now) is the safer trade -- the alternative is
    silently never sending previews to any relay-connected dashboard at
    all, which is the bug this exists to avoid repeating."""
    return "valorant_dashboard" in connected_pages.values() or relay_websocket is not None


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

    live = s.get("liveStats")
    if live:
        live["team1"], live["team2"] = live["team2"], live["team1"]
    assign = s.get("liveStatsAssign")
    if assign:
        assign["team1"], assign["team2"] = assign["team2"], assign["team1"]

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
                if "liveStats" in data:
                    # Manual per-cell override for the Player Stats OCR --
                    # sent as a full replace (same pattern as mapVeto/
                    # postMatch above), paired with a lock/unlock entry
                    # like "liveStats.team1.2.kills" so the OCR loop knows
                    # to leave that one cell alone afterward.
                    server_state["liveStats"] = data["liveStats"]
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

            elif msg_type == "hoax_set_left_side":
                side = payload.get("side")
                if side in ("attacker", "defender"):
                    server_state["hoaxOverlay"]["leftSide"] = side
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

            elif msg_type == "livestats_overlay_show":
                server_state["liveStatsOverlay"]["visible"] = True
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "livestats_overlay_hide":
                server_state["liveStatsOverlay"]["visible"] = False
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "livestats_set_assign":
                # {team, row, playerIndex} -- which roster player (0-4) is
                # currently sitting at physical scoreboard row `row` (0-4)
                # for `team`. Operator correction for when the live
                # scoreboard's own order shifts mid-match.
                team = payload.get("team")
                row = payload.get("row")
                player_index = payload.get("playerIndex")
                if (
                    team in LIVESTATS_TEAMS
                    and isinstance(row, int) and 0 <= row < LIVESTATS_ROWS
                    and isinstance(player_index, int) and 0 <= player_index < 5
                ):
                    server_state["liveStatsAssign"][team][row] = player_index
                    save_state()
                    await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            elif msg_type == "livestats_clear":
                # Explicit request: reset all 40 cells back to 0 once a
                # game is over, so stale numbers from the finished match
                # don't linger into the next one before fresh OCR readings
                # confirm. Also drops any per-cell overrides left locked
                # from the last match -- otherwise a lock from a prior
                # game would keep blocking real OCR updates in the next
                # one. Row->player assignment is left alone -- that's a
                # roster/lineup setting, not match-specific data, no
                # reason to make the operator redo it every game.
                server_state["liveStats"] = {
                    "team1": [default_live_stat_row() for _ in range(LIVESTATS_ROWS)],
                    "team2": [default_live_stat_row() for _ in range(LIVESTATS_ROWS)],
                }
                for f in list(locked_fields):
                    if f.startswith("liveStats."):
                        locked_fields.discard(f)
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
            # WHICH pages need a fresh state_sync this cycle, not just
            # whether anything changed -- an explicit fix for high data
            # usage over the cloud relay. state_sync always carries the
            # FULL server_state (team logos and any other uploaded images
            # included, each easily hundreds of KB as base64), and this
            # loop can now update multiple times a second (liveStats/
            # ocrRoundScore); broadcasting that to every connected page
            # regardless of relevance meant pages like valorant_mvp.html --
            # which reads none of the OCR-driven fields below -- were
            # getting the full payload resent every cycle for no reason,
            # over a real internet relay connection. ocrRoundScore only
            # feeds the Hoax section of valorant_ingame.html; liveStats
            # only feeds valorant_playerstats.html; spikeBadge/bugBanner/
            # plantDefuse/mapVeto (below, only when PLANT_DEFUSE_AUTO_
            # DETECT is on) only render on valorant_ingame.html too --
            # none of these ever need to reach valorant_mvp/
            # valorant_teamchemistry/valorant_characterpick, so those
            # pages now only get updated via explicit manual_update
            # actions (still a plain broadcast() -- those are infrequent
            # operator clicks, not a continuous drain). The dashboard
            # always needs everything, so it's in every set below.
            changed_pages = set()

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
                    changed_pages.update(("valorant_dashboard", "valorant_ingame"))

            if frame_counter % 6 == 0 and dashboard_connected():
                for key, (crop, raw_value) in score_crops.items():
                    data_url = crop_to_data_url(crop)
                    if data_url:
                        await broadcast_to_page("valorant_dashboard", {
                            "type": "crop_preview", "region": key,
                            "image": data_url, "text": str(raw_value) if raw_value is not None else "",
                        })

            # Live Player Stats -- see LIVESTATS_TEAMS/_ROWS/_FIELDS above.
            # Runs on its own slower cadence (LIVESTATS_POLL_EVERY_N_FRAMES),
            # independent of PLANT_DEFUSE_AUTO_DETECT below (a separate
            # feature). All 40 cells are captured then OCR'd in parallel via
            # ocr_executor/asyncio.gather in one batch -- a sequential pass
            # at this cell count would be far too slow to be worth running
            # at all. confirm_value() debounces each cell same as
            # ocrRoundScore above, so one misread frame can't flicker a
            # displayed stat. Respects a per-cell operator lock
            # (liveStats.{team}.{row}.{field} in locked_fields), same
            # override pattern as ocrRoundScore -- an explicit request,
            # since OCR misreads on this many cells are inevitable
            # sometimes. Crop previews go out on this same cadence, to the
            # dashboard only (see broadcast_to_page's own reasoning) -- an
            # explicit request for visibility into what each cell is
            # actually reading (e.g. whether "Coins" is picking up the
            # currency icon instead of/along with the digits).
            if frame_counter % LIVESTATS_POLL_EVERY_N_FRAMES == 0:
                cell_keys = []
                crops = []
                for team in LIVESTATS_TEAMS:
                    for row in range(LIVESTATS_ROWS):
                        for field in LIVESTATS_FIELDS:
                            key = f"valorant_livestats_{team}_row{row}_{field}"
                            region = regions.get(key)
                            if not region or region.get("w", 0) <= 0 or region.get("h", 0) <= 0:
                                continue
                            cell_keys.append((team, row, field, key))
                            crops.append(crop_to_bgr(sct, region))
                if cell_keys:
                    # Coins gets the currency-aware OCR path (strips a
                    # leading ¤ icon before reading digits, see
                    # ocr_currency_number) -- Kills/Deaths/Assists have no
                    # icon and use the plain digit path unchanged.
                    texts = await asyncio.gather(*[
                        loop.run_in_executor(
                            ocr_executor,
                            ocr_currency_number if field == "coins" else ocr_number,
                            crop,
                        )
                        for (_, _, field, _), crop in zip(cell_keys, crops)
                    ])
                    send_previews = dashboard_connected()
                    for (team, row, field, key), text, crop in zip(cell_keys, texts, crops):
                        raw_value = parse_int(text)
                        if field == "coins" and raw_value is not None:
                            if not (0 <= raw_value <= COINS_MAX_CREDITS):
                                # Categorically impossible -- discard before
                                # it can dilute the debounce vote at all,
                                # same as an unreadable frame. See
                                # COINS_MAX_CREDITS.
                                raw_value = None
                            else:
                                # Coins only ever comes in multiples of
                                # COINS_ROUND_TO -- confirmed directly by the
                                # operator watching the live match. Rounding
                                # every reading to the nearest one corrects
                                # exactly the residual noise the mask rework
                                # still leaves (a last-digit miss like
                                # reading 1458 for a true 1450), same
                                # principle as the range clamp above but
                                # fixing near-misses instead of only
                                # discarding impossible ones.
                                raw_value = round(raw_value / COINS_ROUND_TO) * COINS_ROUND_TO
                        confirmed = confirm_value(key, raw_value)
                        lock_key = f"liveStats.{team}.{row}.{field}"
                        if confirmed is not None and lock_key not in locked_fields:
                            current = server_state["liveStats"][team][row][field]
                            if field in MONOTONIC_LIVESTATS_FIELDS and (
                                confirmed < current or confirmed - current > MONOTONIC_MAX_JUMP
                            ):
                                pass  # impossible for a live match counter -- see MONOTONIC_LIVESTATS_FIELDS
                            elif current != confirmed:
                                server_state["liveStats"][team][row][field] = confirmed
                                changed_pages.update(("valorant_dashboard", "valorant_playerstats"))
                        if send_previews:
                            # For Coins, preview the FINAL black/white
                            # image Tesseract actually reads (icon
                            # stripped, contrast-enhanced, thresholded --
                            # the exact same preprocess() pipeline
                            # ocr_number() itself runs), not the raw
                            # calibrated box -- an explicit debugging aid
                            # so a bad reading's real cause (icon not
                            # fully removed, digits clipped, contrast too
                            # low even after CLAHE) is visible directly
                            # instead of guessed at from the text output
                            # alone.
                            if field == "coins":
                                # Composite: the RAW crop (icon + all digits,
                                # untouched) on top with a red line marking
                                # exactly where strip_leading_icon() cut, over
                                # the final black/white mask Tesseract actually
                                # reads on the bottom -- lets a bad reading's
                                # cause be told apart at a glance: a cut that
                                # lands right after a narrow icon-shaped glyph
                                # is correct, one that eats into the first
                                # digit too (icon+digit1 blobs merged, e.g.
                                # from video compression blur bridging their
                                # gap) shows up as the red line sitting deep
                                # inside the visible digit string instead of
                                # right after the icon. preprocess_currency()
                                # already upscales 4x, don't also apply
                                # crop_to_data_url's own default 3x on top of
                                # that.
                                stripped, cut_x = strip_leading_icon(crop, return_cut=True)
                                mask_bgr = cv2.cvtColor(preprocess_currency(stripped), cv2.COLOR_GRAY2BGR)
                                raw_marked = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
                                if cut_x is not None:
                                    mark_x = min(raw_marked.shape[1] - 2, cut_x * 4)
                                    raw_marked[:, mark_x:mark_x + 2] = (0, 0, 255)
                                raw_marked = cv2.copyMakeBorder(raw_marked, 4, 4, 10, 10, cv2.BORDER_CONSTANT, value=(60, 60, 60))
                                target_w = max(raw_marked.shape[1], mask_bgr.shape[1])
                                if raw_marked.shape[1] < target_w:
                                    raw_marked = cv2.copyMakeBorder(raw_marked, 0, 0, 0, target_w - raw_marked.shape[1], cv2.BORDER_CONSTANT, value=(60, 60, 60))
                                if mask_bgr.shape[1] < target_w:
                                    mask_bgr = cv2.copyMakeBorder(mask_bgr, 0, 0, 0, target_w - mask_bgr.shape[1], cv2.BORDER_CONSTANT, value=(0, 0, 0))
                                separator = np.full((2, target_w, 3), (0, 255, 255), dtype=np.uint8)
                                preview_source = np.vstack([raw_marked, separator, mask_bgr])
                                data_url = crop_to_data_url(preview_source, scale=1)
                            else:
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
                if changed_pages:
                    save_state()
                    for page in changed_pages:
                        await broadcast_to_page(page, {"type": "state_sync", "data": server_state, "locked": list(locked_fields)})
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
                changed_pages.update(("valorant_dashboard", "valorant_ingame"))

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
                changed_pages.update(("valorant_dashboard", "valorant_ingame"))

            # Spike badge safety net -- see ROUND_TIMER_KEY/track_round_timer
            # above. Only acts while the badge is actually showing planted;
            # a timer jump the rest of the time is just a normal new round
            # starting up, nothing to correct.
            if track_round_timer(timer_seconds) and server_state["spikeBadge"]["mode"] == "planted":
                server_state["spikeBadge"]["mode"] = "idle"
                server_state["bugBanner"]["mode"] = "hidden"
                changed_pages.update(("valorant_dashboard", "valorant_ingame"))

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
            if frame_counter % 6 == 0 and dashboard_connected():
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

            if changed_pages:
                save_state()
                for page in changed_pages:
                    await broadcast_to_page(page, {"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            frame_counter += 1
            await asyncio.sleep(interval)


async def relay_client_loop():
    """Same optional cloud-relay pattern as ocr_engine.py/freefire_engine.py
    -- no-op if relay isn't configured in valorant_config.json."""
    global relay_websocket
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
                relay_websocket = relay_ws
                await handle_client(relay_ws)
        except Exception as e:
            print(f"Relay connection lost/failed ({e}); retrying in 3s...")
        finally:
            relay_websocket = None
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
