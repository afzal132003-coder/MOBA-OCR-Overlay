"""BGMI engine -- capture the observer panel, serve it to the dashboard.

Its own process on its own port (8767), the same isolation freefire
(8765) and dota2 (8766) already use, so running this never disturbs a
Free Fire event and the two can sit side by side on one PC.

ON LOAD, because the last thing this dashboard needed was more of it.
Reading one frame costs about 40ms, so even a continuous poll is a
fraction of a core -- but the PREVIEW is a JPEG of the whole panel, and
pushing one of those down the socket on every poll is the part that
actually hurts. So previews are on demand only, one capture per button
press, exactly like the Free Fire alive/elim previews. Nothing is
captured at all while nobody is looking unless continuous polling is
explicitly switched on.

NOTHING IS PUBLISHED THAT CANNOT BE JUSTIFIED. A read reaches the state
only when the slot numbers could be fitted to a consecutive run and no
digit template is missing. Both are reported to the dashboard as the
reason when a read is refused, so a blank table always says why it is
blank.
"""

import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgmi_panel as bp

CONFIG_PATH = Path(__file__).parent / "bgmi_config.json"
STATE_PATH = Path(__file__).parent / "bgmi_state.json"
PORT = 8767

CONNECTED = set()


def default_config():
    return {
        # This rig runs the game on the second screen; calibrate_bgmi.py
        # asks and writes whichever one you pick.
        "monitor": 2,
        # The panel's rectangle on screen. BlueStacks draws window chrome
        # around the Android framebuffer, so this is NOT the window --
        # calibrate it to the game picture itself or every offset in
        # bgmi_panel.py is wrong by the width of a title bar.
        "region": {"x": 0, "y": 0, "w": 1920, "h": 1080},
        # ON by default, and pointing at the same hub the other engines
        # use. Without it the engine listens only on localhost -- while a
        # dashboard opened from the relay talks to the relay, connects
        # happily, and sends captures into a hub where no BGMI engine is
        # listening. That reads as CONNECTED with a button that does
        # nothing, which is the worst shape a fault can take.
        #
        # The token is deliberately NOT here: it is read from
        # ocr/.relay_token or RELAY_TOKEN, which git ignores.
        "relay": {"enabled": True,
                  "url": "wss://titanium-relay.duckdns.org",
                  "token": ""},
        "settings": {
            # Off by default. See the note at the top about load.
            "continuousPoll": False,
            "pollIntervalSeconds": 0.5,
            "lobbySize": 16,
            "firstSlot": 3,          # this lobby numbers its teams 3..18
            # How this capture compares to the reference frame, and where
            # its first card column starts. Both are properties of the
            # capture RECTANGLE, not of any frame, so they are measured
            # once and kept; recalibrating clears them. null means "work
            # them out on the next capture".
            "originX": None,
            "scale": None,
            "columns": None,
            # The broadcast sheet. Off until a URL is set, and even then
            # only pushes when the table has actually changed.
            "sheetWebhookUrl": "",      # the LIVE STATUS sheet's deployment
            "resultsWebhookUrl": "",    # the SCORESHEET's own, separate one
            "sheetTab": "",
            "resultsTab": "",
            "resultsGame": 1,
            "liveSheetPush": False,
            # slot -> team name, filled in before the event. Display only:
            # nothing in the capture or the sheet push depends on it, so a
            # blank list can never stop a match being read.
            "teams": {},
            # Names cost a Tesseract pass per player, so this is the one
            # expensive thing in a capture -- but it is also the only
            # bridge to the results screen, which has no slot numbers.
            "readIgns": True,
        },
    }


def load_config():
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


config = load_config()
templates = bp.load_templates()

server_state = {
    "bgmi": {
        # One entry per slide, keyed by its number as a string. A slide is
        # one screenful of the panel: the operator captures it, scrolls,
        # and captures the next. Kept separately rather than merged on
        # arrival so a re-capture of slide 2 replaces only slide 2, and so
        # the dashboard can show which slide a disagreement came from.
        "slides": {},
        "rows": [],              # the slides merged, by slot
        "conflicts": [],
        "header": {"remaining": None, "teamsAlive": None},
        "readAt": 0,
        "blockedBy": "",
        "teams": {},
    }
}


# ----------------------------------------------------------------- capture

REFERENCE_SIZE = (1920, 1080)

# Bumped whenever the way geometry is MEASURED changes. A stored scale
# and origin are only as good as the code that produced them, and an
# engine that keeps them across an upgrade keeps the old answer -- which
# is how a capture went on using scale 1.095 after the measurement was
# improved to give 1.077, with every box a little off and no sign of why.
GEOM_VERSION = 3


def refresh_config():
    """Re-read the config file, so calibrating does not need a restart.

    The config used to be read once at import. Calibrate while the engine
    is up -- which is the obvious order, since the calibrator tells you to
    restart afterwards and nobody reads that twice -- and the engine went
    on grabbing the OLD region, usually the default full screen 1 with no
    game on it. The capture then found nothing, for a reason nothing on
    screen could explain.

    Reading a small JSON per capture costs nothing worth measuring.
    """
    global config
    fresh = load_config()
    if fresh.get("region"):
        if fresh["region"] != config.get("region"):
            # Recalibrated: the origin measured against the old rectangle
            # means nothing against a new one.
            st = config.setdefault("settings", {})
            st["originX"] = st["scale"] = st["columns"] = None
        config["region"] = fresh["region"]
    if fresh.get("monitor"):
        config["monitor"] = fresh["monitor"]
    return config


def grab_region():
    """One screenshot of the configured rectangle, scaled to 1920x1080.

    Every offset in bgmi_panel.py -- card columns, row pitch, the slot
    and kill boxes -- was measured on a 1920x1080 frame. The game picture
    inside a BlueStacks window is not that size: this rig's is 1779x998,
    about 7% short, which is enough to walk the reader off the rows
    entirely by the bottom of a card.

    Rather than demand a particular emulator resolution, the frame is
    resized to the reference once, here, and everything downstream keeps
    working in the coordinates it was measured in. A 7% rescale is mild
    and the glyph matching is shape-based, so it costs nothing worth
    measuring -- and it means a changed window size does not silently
    break the read.
    """
    import mss
    r = config.get("region") or {}
    box = {"left": int(r.get("x", 0)), "top": int(r.get("y", 0)),
           "width": int(r.get("w", 1920)), "height": int(r.get("h", 1080))}
    with mss.mss() as sct:
        shot = sct.grab(box)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    if img.size != REFERENCE_SIZE:
        img = img.resize(REFERENCE_SIZE, Image.LANCZOS)
    return np.asarray(img)


ANCHOR_FLOOR = 0.07


def read_frame(rgb, declared_slot=None, learn=True):
    """One slide of the panel, read against the slot the operator declared.

    THE DECLARED SLOT IS THE MECHANISM; the numbers on screen are the
    check. The operator scrolls the panel and says where the page starts,
    which is something they can see and we cannot reliably infer: an
    arbitrary scroll position gives no clue which teams are on it, and
    the fit that would otherwise have to supply it needs a complete digit
    store to be sound.

    Reading them still matters -- it is what catches a typo. When the
    store can read the numbers and they disagree with what was typed, the
    capture is REFUSED rather than published under either, because one of
    the two is wrong and guessing which would put a team's kills on
    another team's row in the sheet.

    Returns (rows, header, diagnostics, blocked_reason). `rows` is empty
    whenever blocked_reason is set -- a refusal never half-publishes.
    """
    luma = bp.to_luma(rgb)
    page = bp.read_page(rgb, digit_templates=kill_store())
    cards = page["cards"]
    diag = {
        "scale": round(bp.GEOM["scale"], 3),
        "cards": len(cards),
        "cardRows": page["card_rows"],
        "scroll": round(page["scroll"], 1),
        "scrollScore": round(page["scroll_score"], 1),
        "fitMargin": round(bp.LAST_FIT["margin"], 3),
        "declaredSlot": declared_slot,
        "readSlot": None,
        "slotMargin": 0.0,
        "learned": [],
        "refusedToLearn": [],
        "missingDigits": bp.missing_digits(templates.get("slot", {})),
    }

    if not cards:
        return [], {}, diag, ("No team cards found. Is the panel open, and is "
                              "the capture region on the game picture rather "
                              "than the BlueStacks window?")

    # A panel caught mid-scroll fits two phases about equally well, and
    # the wrong one puts every row in a gap. Refused rather than read.
    # Unless the slot numbers themselves say the rows are right. The last
    # screenful of a lobby has one card on its bottom row, which weakens
    # the row rhythm until a settled frame scored 0.093 and was refused
    # as "still scrolling". A frame that really is mid-scroll puts every
    # card's number off where its card says it should be; one where every
    # number sits exactly in place is aligned, whatever the rhythm scored.
    #
    # Never below ANCHOR_FLOOR, though. A frame blurred by the scroll -- two
    # positions half a row apart blended together -- still shows its slot
    # numbers in place, but its names are half-bright and would read as
    # dead. Measured: a settled last slide 0.093, such a blend 0.060, the
    # frame actually caught moving on the event rig 0.01.
    anchored = (bp.LAST_FIT["margin"] >= ANCHOR_FLOOR
                and bp.slots_confirm_rows(cards))
    diag["anchoredBySlots"] = anchored
    if bp.LAST_FIT["margin"] < bp.MIN_FIT_MARGIN and not anchored:
        return [], {}, diag, (
            "The rows did not line up clearly (margin %.2f, want %.2f) -- the "
            "panel was most likely still scrolling. Let it settle and capture "
            "again." % (bp.LAST_FIT["margin"], bp.MIN_FIT_MARGIN))

    # Drop the empty places at the end of the last row before anything
    # counts them as teams.
    while cards and not cards[-1].get("present", True):
        cards.pop()
    diag["cards"] = len(cards)
    if not cards:
        return [], {}, diag, ("The panel is open but no card has a slot number "
                              "on it -- is this the team panel?")
    gap = [c["index"] for c in cards if not c.get("present", True)]
    if gap:
        return [], {}, diag, (
            "There is an empty place among the cards (position %s), which "
            "should not happen -- the panel fills left to right. Re-capture; "
            "if it persists the grid is being read in the wrong place."
            % ", ".join(str(g) for g in gap))

    if declared_slot is None:
        return [], {}, diag, ("Say which slot this slide starts at. The panel "
                              "can stop anywhere, so nothing on screen says "
                              "which teams these nine are.")

    # THE ARITHMETIC CHECK, which needs no OCR and catches the mistake the
    # digit check would have caught if a digit were missing.
    #
    # The panel cannot scroll past its own end, so the last screenful of a
    # sixteen-team lobby shows slots 10-18, not 12-20. Declaring 12 there
    # files every row two slots high -- quietly, and it looks like a
    # perfectly ordinary table afterwards.
    settings = config.get("settings") or {}
    lobby = int(settings.get("lobbySize") or 0)
    first_slot = int(settings.get("firstSlot") or 3)
    if lobby:
        last_slot = first_slot + lobby - 1
        would_end = declared_slot + len(cards) - 1
        if declared_slot < first_slot or would_end > last_slot:
            suggest = max(first_slot, last_slot - len(cards) + 1)
            return [], {}, diag, (
                "You said this slide starts at %d, but %d cards from there "
                "reach slot %d -- and this lobby has %d teams, slots %d to %d. "
                "The panel cannot scroll past its end, so the last screenful "
                "starts at %d. Try %d."
                % (declared_slot, len(cards), would_end, lobby,
                   first_slot, last_slot, suggest, suggest))

    # Learn from what was declared. This is the way out of the deadlock:
    # digit 2 could never be read because digit 2 had never been seen, and
    # a labelled page needs no templates to teach from.
    if learn:
        got, refused = bp.learn_slot_digits(cards, declared_slot, templates)
        diag["learned"] = sorted(set(got))
        diag["refusedToLearn"] = refused
        if got:
            bp.save_templates(templates)
        diag["missingDigits"] = bp.missing_digits(templates.get("slot", {}))

    # Now the check, but only once the store can actually read a number.
    if not diag["missingDigits"]:
        read_first, margin = bp.fit_slot_run(cards, templates.get("slot", {}))
        diag["readSlot"], diag["slotMargin"] = read_first, round(margin, 3)
        if read_first is not None and read_first != declared_slot:
            return [], {}, diag, (
                "You said this slide starts at %d, but the numbers on screen "
                "read %d. One of those is wrong, and publishing either would "
                "put a team's kills on another team's row — scroll to where "
                "you meant, or correct the number."
                % (declared_slot, read_first))

    bp.assign_slots(cards, declared_slot)
    rows = bp.team_rows(cards)

    # Names, while the panel is still up. This is the only chance: the
    # alive panel goes away at the whistle and the results screen that
    # replaces it has ranks but no slots.
    if (config.get("settings") or {}).get("readIgns", True):
        try:
            got = collect_igns(luma, cards)
            if isinstance(got, str):
                diag["ignsRead"], diag["ignNote"] = 0, got
            else:
                diag["ignsRead"] = got
        except Exception as e:
            diag["ignsRead"] = 0
            diag["ignNote"] = str(e)
    remaining, teams_alive = bp.read_header(luma, templates.get("slot", {}))
    header = {"remaining": remaining, "teamsAlive": teams_alive}
    return rows, header, diag, ""


def recombine():
    """Merge every captured slide into one table, keyed by slot.

    Slides may overlap -- a scroll of less than a full page is normal --
    and where two of them can see the same slot they must agree. A clash
    is reported rather than resolved: both were captured from the same
    game, so a disagreement means one was taken mid-scroll or while the
    panel was covered, and picking a winner would hide that.
    """
    state = server_state["bgmi"]
    pages = [s["rows"] for s in state["slides"].values() if s.get("rows")]
    rows, conflicts = bp.merge_pages(pages)
    state["rows"] = rows
    state["conflicts"] = [
        {"slot": c[0], "a": c[1], "b": c[2]} for c in conflicts]

    # The checksum only means anything once the slides between them cover
    # the lobby. On a partial view our totals are legitimately lower than
    # the header's, and asserting otherwise would reject every good frame.
    lobby = int((config.get("settings") or {}).get("lobbySize") or 0)
    head = state.get("header") or {}
    head["complete"] = bool(lobby and len(rows) >= lobby)
    head["agrees"] = bp.agrees_with_header(
        rows, head.get("remaining"), head.get("teamsAlive"), head["complete"])
    head["covered"] = len(rows)
    head["lobbySize"] = lobby
    state["header"] = head
    return rows


# ------------------------------------------------------------ player names
#
# WHY THESE ARE READ AT ALL, given nothing on the live overlay needs them:
# the results screen at the end of a match has NO slot numbers. It has
# ranks and it has player names. The alive panel has slot numbers and the
# same player names. So the names are the only bridge between the two,
# and they have to be collected while the alive panel is still up --
# it disappears the moment the match ends.
#
# They are kept RAW, exactly as the OCR read them, never tidied. The
# match at the other end is fuzzy, and a cleaned-up name is a name that
# has been guessed at twice.

IGN_VOTE_PATH = Path(__file__).parent / "bgmi_igns.json"
_ign_votes = {}


def load_igns():
    global _ign_votes
    try:
        _ign_votes = json.loads(IGN_VOTE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _ign_votes = {}


def save_igns():
    try:
        IGN_VOTE_PATH.write_text(json.dumps(_ign_votes, indent=1), encoding="utf-8")
    except OSError:
        pass


def ocr_text(img):
    import subprocess
    import tempfile
    tess = (config.get("tesseractPath")
            or r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    path = tempfile.mktemp(suffix=".png")
    img.save(path)
    try:
        out = subprocess.run(
            [tess, path, "stdout", "--psm", "7", "-c",
             "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "abcdefghijklmnopqrstuvwxyz0123456789"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return "".join(out.split())


IGN_GAP = 26          # white space between stacked names, in the strip


def ocr_lines(img, count):
    """One Tesseract pass over a stack of names, split back into lines.

    THIRTY-SIX NAMES USED TO BE THIRTY-SIX PROCESSES. A Tesseract call is
    about 150ms here, almost all of it spawning the process rather than
    reading anything, so a capture spent 3.7 seconds on names alone --
    past the point where the dashboard gives up waiting and the button
    sits on "Reading..." forever. Stacked into one tall strip it is a
    single call.

    Returns None unless exactly `count` lines come back. Tesseract drops a
    line it finds empty, and a dropped line shifts every name after it
    onto the wrong player -- which would be far worse than reading none,
    because names are what the results screen is matched against. Names do
    not change, so losing one pass costs nothing: the next capture reads
    them again.
    """
    import subprocess
    import tempfile
    tess = (config.get("tesseractPath")
            or r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    path = tempfile.mktemp(suffix=".png")
    img.save(path)
    try:
        out = subprocess.run(
            [tess, path, "stdout", "--psm", "6", "-c",
             "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "abcdefghijklmnopqrstuvwxyz0123456789"],
            capture_output=True, text=True, timeout=40).stdout
    except Exception as e:
        # A missing or wrong tesseractPath lands here, and used to vanish
        # without trace -- names simply never appeared and nothing said
        # why. Reported instead.
        return "Tesseract could not be run (%s). Check tesseractPath." % e
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    lines = [("".join(l.split())) for l in out.splitlines() if l.strip()]
    if len(lines) != count:
        return ("Read %d name lines from %d players -- skipped this pass "
                "rather than risk pairing names to the wrong seats."
                % (len(lines), count))
    return lines


def collect_igns(luma, cards):
    """Read every name on this page and add it to that seat's tally.

    Voted rather than overwritten because the same seat is read many times
    across a match. A name does not change, so the reading that comes back
    most often is the one to trust, and a single frame caught mid-scroll
    or mid-repaint cannot displace twenty good ones.
    """
    seats, images = [], []
    for c in cards:
        slot = c.get("slot")
        if slot is None:
            continue
        cx = CARD_X_OF(c)
        for p in range(bp.PLAYERS_PER_CARD):
            img = bp.ign_image(luma, cx, bp.row_y(c["top"], p), scale=3)
            if img is None:
                continue
            seats.append((slot, p))
            images.append(img)
    if not images:
        return 0

    # One strip, names stacked with white between them. The gap has to be
    # wide enough that Tesseract treats them as separate lines and not as
    # one wrapped paragraph.
    width = max(i.width for i in images)
    height = sum(i.height for i in images) + IGN_GAP * (len(images) + 1)
    strip = Image.new("L", (width, height), 255)
    y = IGN_GAP
    for img in images:
        strip.paste(img.convert("L"), (0, y))
        y += img.height + IGN_GAP

    texts = ocr_lines(strip, len(images))
    if isinstance(texts, str):
        # One line dropped anywhere voids the whole page -- 35 lines for 36
        # names lost all nine squads on the event rig. Read card by card
        # instead: slower, but a bad line then costs that one card only.
        texts = []
        i = 0
        while i < len(seats):
            j = i
            while j < len(seats) and seats[j][0] == seats[i][0]:
                j += 1
            card = images[i:j]
            w = max(im.width for im in card)
            h = sum(im.height for im in card) + IGN_GAP * (len(card) + 1)
            sub = Image.new("L", (w, h), 255)
            y = IGN_GAP
            for im in card:
                sub.paste(im.convert("L"), (0, y))
                y += im.height + IGN_GAP
            got = ocr_lines(sub, len(card))
            texts.extend(got if isinstance(got, list) else [""] * len(card))
            i = j

    changed = 0
    for (slot, p), text in zip(seats, texts):
        if len(text) < 3:
            continue
        seat = _ign_votes.setdefault(str(slot), {}).setdefault(str(p), {})
        seat[text] = seat.get(text, 0) + 1
        changed += 1
    if changed:
        save_igns()
    return changed


def CARD_X_OF(card):
    """Where this card's column actually is in THIS capture.

    Was bp.CARD_X -- the reference frame's columns, ignoring this
    capture's scale and origin. On the event rig that sat ~50px right of
    the names, so every name was read with its first letters missing
    ("enNizhal7k" for "WRavenNizhal7k") and the results screen could not
    be matched against any of them."""
    return bp.card_columns()[card["column"]]


def kill_store():
    """Templates for reading a kill count: the kill store, backed by the
    slot store for digits it has never seen.

    THE KILL STORE ONLY HAS 0 TO 3. It was seeded from one frame where no
    squad had more than three kills, so there was no 4 to match against
    and a 4 quietly became the nearest thing available -- a 3. A squad on
    /4 /1 /0 /2 was reported with 6 kills instead of 7, and nothing about
    that number looks wrong on a sheet.

    The slot numbers cover 0 to 9 and are the same face at a different
    size -- on the reference frame they read its kill digits 34 of 36
    with nothing wrong. Returned as a FALLBACK LIST rather than merged in:
    merging puts every slot digit in front of every kill digit as a rival,
    and two counts the kill store read cleanly became ties and were
    refused. Tried in order, the close match wins where it exists and the
    other store is only reached for when there is nothing.
    """
    return [templates.get("kill") or {}, templates.get("slot") or {}]


def ign_map():
    """slot -> the four names, each the most-voted reading for its seat."""
    out = {}
    for slot, seats in _ign_votes.items():
        names = []
        for p in range(bp.PLAYERS_PER_CARD):
            votes = seats.get(str(p)) or {}
            names.append(max(votes, key=votes.get) if votes else "")
        out[int(slot)] = names
    return out


load_igns()
server_state["bgmi"]["teams"] = (config.get("settings") or {}).get("teams") or {}


# -------------------------------------------------------------- sheet push

_last_sheet_payload = None


def push_alive_to_sheet(force=False):
    """The merged table, into the broadcast sheet.

    One row per slot: four checkboxes ticked left to right for the alive
    count, and the team's elimination total beside them. The sheet is
    ordered by slot and the game prints the slot, so this needs none of
    the name matching the Free Fire push carries -- which is most of that
    script, and all of its failure modes.

    A slot whose kills could not be read sends null rather than zero, and
    the receiving end leaves that cell alone. Writing a zero because a
    frame was unreadable would wipe a score that was right a moment ago,
    and nothing downstream could tell the difference.
    """
    global _last_sheet_payload
    settings = config.get("settings") or {}
    url = (settings.get("sheetWebhookUrl") or "").strip()
    if not url:
        return {"ok": False, "error": "No sheet webhook URL set."}
    rows = server_state["bgmi"].get("rows") or []
    if not rows:
        return {"ok": False, "error": "Nothing captured yet."}

    payload = {
        "tab": (settings.get("sheetTab") or "").strip(),
        "rows": [{"slot": r["slot"], "alive": r["alive"], "kills": r.get("kills")}
                 for r in rows],
    }
    # A match sits unchanged for most of its length. Rewriting the same
    # sixteen rows four times a second is a write the sheet must process,
    # a quota it spends, and a cell that visibly repaints for anyone
    # watching it.
    if not force and payload == _last_sheet_payload:
        return {"ok": True, "skipped": "unchanged"}

    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            answer = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    _last_sheet_payload = payload
    return answer


def push_results_to_sheet(game, rows):
    """One finished match into the scoresheet: finishes and rank per slot.

    A different spreadsheet from the live table, and a different shape --
    the scoresheet stacks one block per game down the page, so the game
    number picks the block and the slot picks the row inside it.

    Only two cells per team are written. The points, the totals, the team
    names and the logo paths are all the sheet's own formulas, and a push
    that touched them would overwrite the thing that makes the sheet
    worth having.
    """
    settings = config.get("settings") or {}
    # Its own URL. The scoresheet has its own bound script and its own
    # deployment, so results never travel through the live sheet's -- and
    # neither script can reach the other's spreadsheet at all.
    url = (settings.get("resultsWebhookUrl") or "").strip()
    if not url:
        return {"ok": False, "error":
                "No RESULTS webhook URL set (that is a separate deployment "
                "from the alive one, in the scoresheet's own Apps Script)."}
    if not rows:
        return {"ok": False, "error": "No result rows to push."}
    try:
        game = int(game)
    except (TypeError, ValueError):
        return {"ok": False, "error": "No game number given."}

    payload = {
        "game": game,
        "tab": (settings.get("resultsTab") or "").strip(),
        "rows": [{"slot": r.get("slot"), "finishes": r.get("finishes"),
                  "rank": r.get("rank")} for r in rows],
    }
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----------------------------------------------------------------- preview

def annotate(rgb, cards, rows, diag=None, declared=None):
    """Draw what the reader saw onto the frame.

    The point of a preview is to be disagreed with. A picture of the panel
    tells the operator nothing they cannot see in the game; a picture with
    the reader's own boxes and verdicts on it lets them spot a row read as
    dead that plainly is not, which is the whole reason to look.
    """
    im = Image.fromarray(rgb).convert("RGB")
    d = ImageDraw.Draw(im)
    cols = bp.card_columns()
    by_index = {i: r for i, r in enumerate(rows)}

    # Every card the reader looked at, drawn where it ACTUALLY looked --
    # at this capture's scale and origin. Drawing them at the reference
    # positions made the boxes sit a card-width right of the cards they
    # described, which is worse than no boxes: it suggests the reader is
    # looking somewhere it is not.
    for c in cards:
        cx, top = cols[c["column"]], c["top"]
        w, h = bp.sx(520), bp.sx(bp.CARD_H)

        if not c.get("present", True):
            # An empty place in the last row. Said so plainly rather than
            # drawn as four dead players, which is what it looks like to
            # anything counting dots.
            d.rectangle([cx, top, cx + w, top + h], outline=(70, 80, 95), width=2)
            d.text((cx + 8, top + 8), "no card here", fill=(125, 139, 156))
            continue

        d.rectangle([cx, top, cx + w, top + h], outline=(90, 200, 255), width=2)
        row = by_index.get(c["index"])
        label = ("slot %s  %d/4 alive  %s kills"
                 % (row["slot"], row["alive"],
                    "--" if row["kills"] is None else row["kills"])) if row else                 ("%d/4 alive" % sum(1 for pl in c["players"] if pl["alive"]))
        d.text((cx + 6, max(0, top - 16)), label, fill=(255, 220, 120))
        for p in range(bp.PLAYERS_PER_CARD):
            y = bp.row_y(top, p)
            alive = c["players"][p]["alive"]
            d.ellipse([cx + bp.sx(64), y + 8, cx + bp.sx(74), y + 18],
                      fill=(110, 230, 160) if alive else (230, 90, 90))

    # The facts of the read, ON the picture. "What is it actually looking
    # at" is the first question whenever a capture comes out wrong, and a
    # preview that cannot answer it sends you back to the config file.
    if diag is not None:
        r = diag.get("region") or {}
        lines = [
            "region %sx%s at %s,%s  (scaled to 1920x1080)"
            % (r.get("w"), r.get("h"), r.get("x"), r.get("y")),
            "whole cards found: %s      scroll offset %s (fit %s)"
            % (diag.get("cards"), diag.get("scroll"), diag.get("scrollScore")),
            "you said this slide starts at slot %s" % declared,
        ]
        box_h = 18 * len(lines) + 12
        d.rectangle([0, 0, im.width, box_h], fill=(6, 9, 12))
        for i, text in enumerate(lines):
            d.text((10, 6 + i * 18), text, fill=(154, 167, 181))
    return im


def to_data_url(im, max_width=1100, quality=62):
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ------------------------------------------------------------------ serving

async def broadcast(msg):
    if not CONNECTED:
        return
    payload = json.dumps(msg)
    for ws in list(CONNECTED):
        try:
            await ws.send(payload)
        except Exception:
            pass


RESULT_DIR = Path(__file__).parent / "result_captures"


def capture_result():
    """A screenshot of the results screen, kept and handed back.

    NOT READ YET. The results screen has its own layout -- two fixed
    ranks on the left, the rest scrolling on the right, blocks of
    unequal height -- and reading it needs geometry measured against a
    capture from THIS rig, which did not exist until now.

    So this grabs and saves. The operator gets the picture to check
    against, the file is kept for the reader to be built from, and the
    ranks go in by hand in the meantime. The alternative was to ship
    nothing until the reader was finished, during an event.
    """
    refresh_config()
    rgb = grab_region()
    try:
        RESULT_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y-%m-%d-%H-%M-%S")
        path = RESULT_DIR / ("result_%s.png" % stamp)
        Image.fromarray(rgb).save(path)
        saved = str(path)
    except Exception as e:
        saved = "could not save: %s" % e
    state = server_state["bgmi"]
    state["resultShot"] = saved
    try:
        state["result"] = read_results()
    except Exception as e:
        state["result"] = {"error": str(e), "rows": [], "unresolved": []}
    return to_data_url(Image.fromarray(rgb)), saved


RESULT_WINDOW = 600   # seconds: captures this recent belong to one results screen


def _ocr_lines_any(img, count, digits=False):
    """ocr_lines, optionally restricted to what "N finishes" can contain."""
    if not digits:
        return ocr_lines(img, count)
    import subprocess
    import tempfile
    tess = (config.get("tesseractPath")
            or r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    path = tempfile.mktemp(suffix=".png")
    img.save(path)
    try:
        out = subprocess.run(
            [tess, path, "stdout", "--psm", "6", "-c",
             "tessedit_char_whitelist=0123456789finshe"],
            capture_output=True, text=True, timeout=40).stdout
    except Exception as e:
        return "Tesseract could not be run (%s)." % e
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    lines = ["".join(l.split()) for l in out.splitlines() if l.strip()]
    if len(lines) != count:
        return "Read %d lines for %d." % (len(lines), count)
    return lines


def read_results():
    """Every results capture of the last RESULT_WINDOW seconds, read,
    merged by rank, and matched to slots by player names.

    The results screen prints rank, names and finishes but no slot. The
    slot comes from the names collected off the alive panel during the
    match (ign_map). A rank whose names match no slot clearly is left
    unresolved -- listed with its names for the operator -- never guessed."""
    import bgmi_results as br
    now = time.time()
    shots = sorted(p for p in RESULT_DIR.glob("result_*.png")
                   if now - p.stat().st_mtime <= RESULT_WINDOW)
    readings, notes, seen = [], [], set()
    for p in shots:
        raw = p.read_bytes()
        key = hash(raw)
        if key in seen:
            continue
        seen.add(key)
        rgb = np.asarray(Image.open(p).convert("RGB"))
        entries, note = br.read_screen(rgb, _ocr_lines_any, templates.get("slot") or {})
        if note:
            notes.append("%s: %s" % (p.name, note))
        readings.append(entries)
    merged = br.merge(readings)
    matched, unresolved, free = bp.match_squads(
        {rank: e["names"] for rank, e in merged.items()}, ign_map())
    # THE LAST ONE LEFT. When every other rank is matched by name and
    # exactly one rank and one lobby slot remain, they are each other --
    # that is arithmetic, not a guess. Seen on the event rig: one card's
    # names failed to read, and its squad was the only one unplaced.
    settings = config.get("settings") or {}
    lobby, first = int(settings.get("lobbySize") or 0), int(settings.get("firstSlot") or 3)
    if lobby and len(unresolved) == 1:
        taken = {m["slot"] for m in matched.values()}
        left = [s for s in range(first, first + lobby) if s not in taken]
        if len(left) == 1:
            matched[unresolved[0]] = {"slot": left[0], "score": None,
                                      "byElimination": True}
            unresolved = []
    rows = []
    for rank, e in merged.items():
        fin = e["finishes"]
        total = None if any(f is None for f in fin) else sum(fin)
        m = matched.get(rank)
        rows.append({"rank": rank, "slot": m["slot"] if m else None,
                     "score": m.get("score") if m else None,
                     "byElimination": bool(m and m.get("byElimination")),
                     "finishes": total, "players": fin, "names": e["names"]})
    return {"rows": rows, "captures": len(readings), "notes": notes,
            "unresolved": [{"rank": r, "names": merged[r]["names"]} for r in unresolved],
            "freeSlots": free, "readAt": now}


SETTLE_TRIES = 9        # first look plus eight re-grabs...
SETTLE_WAIT = 0.25      # ...a quarter second apart: two seconds at most


def capture_slide(slide, declared_slot, with_preview=True, learn=True):
    """Capture one slide: read it, keep it, re-merge every slide.

    A slide that cannot be read replaces nothing. Re-capturing slide 2
    after a bad frame must not take the good slide 2 away with it --
    leaving the last good one standing is what lets the operator simply
    press it again.
    """
    refresh_config()
    rgb = grab_region()

    # The x origin is measured once and kept. The scale is measured every
    # frame by the row search, so only this needs remembering -- and only
    # until the capture region changes, which rewrites it.
    # Before anything is measured or read: is the panel actually up?
    #
    # Calibrating against the lobby wrote scale 0.925 / origin 28 into
    # the config and kept them, which would have made every read of the
    # real panel wrong for the rest of the event. A frame that is not the
    # panel is refused here, and nothing is saved from it.
    ok, dark = bp.panel_present(bp.to_luma(rgb))
    if not ok:
        state = server_state["bgmi"]
        state["blockedBy"] = (
            "That is not the team panel -- the capture region is showing "
            "another screen (the lobby or room list, most likely). Only %d%% "
            "of it is dark where the panel is about 97%%. Open the observer "
            "panel and press capture again; nothing was changed." % round(dark * 100)
        )
        diag = {"panelDark": round(dark, 3), "cards": 0,
                "region": dict(config.get("region") or {})}
        state["diagnostics"] = diag
        state["readAt"] = time.time()
        # The preview is still returned, unannotated. Being told the wrong
        # screen is up is useful; being shown WHICH screen is up is what
        # actually ends the hunt.
        return to_data_url(annotate(rgb, [], [], diag, declared_slot)) if with_preview else ""

    settings = config.setdefault("settings", {})
    if settings.get("geomVersion") != GEOM_VERSION:
        settings["scale"] = settings["originX"] = None
        settings["geomVersion"] = GEOM_VERSION
    if settings.get("originX") is None or settings.get("scale") is None:
        found = bp.find_geometry(rgb, digit_templates=kill_store())
        settings.update(found)
        settings["geomVersion"] = GEOM_VERSION
        save_config(config)
        print("Measured this capture: scale %.3f, columns %s. Saved."
              % (found["scale"], found.get("columns")))
    bp.set_geometry(scale=settings["scale"], origin_x=settings["originX"],
                    columns=settings.get("columns"))
    rows, header, diag, blocked = read_frame(rgb, declared_slot, learn)
    # STILL SCROLLING? Wait for it instead of sending the operator back to
    # press again. The panel coasts after a swipe, so a press made the
    # moment it looks still can catch it moving (margin 0.01 on the event
    # rig). Re-grab every quarter second until a frame settles; a refused
    # frame teaches nothing, since learning comes after this check.
    tries = 1
    while (blocked and blocked.startswith("The rows did not line up")
           and tries < SETTLE_TRIES):
        time.sleep(SETTLE_WAIT)
        rgb = grab_region()
        rows, header, diag, blocked = read_frame(rgb, declared_slot, learn)
        tries += 1
    diag["settleTries"] = tries
    if blocked and blocked.startswith("The rows did not line up"):
        blocked += (" (Waited %.1f s for it to settle.)" % ((tries - 1) * SETTLE_WAIT))
    state = server_state["bgmi"]
    diag["region"] = dict(config.get("region") or {})

    if not blocked:
        state["slides"][str(slide)] = {
            "slide": slide,
            "firstSlot": declared_slot,
            "rows": rows,
            "readAt": time.time(),
        }
        if header.get("remaining") is not None:
            state["header"].update(header)
        # What the poll loop will keep refreshing: the panel is presumed
        # to still be where the operator last pointed it.
        state["activeSlide"] = slide
        state["igns"] = ign_map()
        recombine()
        # Straight on to the sheet when asked for, and only when the table
        # actually changed -- see push_alive_to_sheet.
        if (config.get("settings") or {}).get("liveSheetPush"):
            try:
                state["sheet"] = push_alive_to_sheet(force=False)
            except Exception as e:
                state["sheet"] = {"ok": False, "error": str(e)}

    state["readAt"] = time.time()
    state["blockedBy"] = blocked
    state["diagnostics"] = diag

    preview = ""
    if with_preview:
        page = bp.read_page(rgb, digit_templates=kill_store())
        cards = page["cards"]
        if cards and declared_slot is not None:
            bp.assign_slots(cards, declared_slot)
        preview = to_data_url(
            annotate(rgb, cards, rows if rows else bp.team_rows(cards),
                     diag, declared_slot)
            if cards else annotate(rgb, [], [], diag, declared_slot))
    return preview


async def handle_client(websocket, path=None):
    """One connection.

    EVERY MESSAGE IS WRAPPED ON ITS OWN. This used to be a single try
    around the whole loop, so any error inside any handler -- a screen
    grab that failed, a bad region, anything -- ended the loop, dropped
    the connection and said nothing at all. The dashboard reconnected a
    moment later and showed CONNECTED, so the only symptom was a button
    that appeared to do nothing, with no error anywhere to explain it.

    Now a failing message is reported back and the connection carries on.
    """
    CONNECTED.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "state_sync", "data": server_state}))
        async for raw in websocket:
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            kind = payload.get("type")
            try:
                await handle_message(websocket, kind, payload)
            except Exception as e:
                import traceback
                traceback.print_exc()
                try:
                    await websocket.send(json.dumps({
                        "type": "bgmi_error", "kind": kind, "error": repr(e)}))
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        CONNECTED.discard(websocket)


async def handle_message(websocket, kind, payload):
    """One message. Raised errors are caught by the caller, reported to
    the dashboard and the connection kept."""

    if kind == "bgmi_capture_slide":
        slide = int(payload.get("slide") or 1)
        first = payload.get("firstSlot")
        first = int(first) if first not in (None, "") else None
        loop = asyncio.get_running_loop()
        preview = await loop.run_in_executor(
            None, capture_slide, slide, first,
            bool(payload.get("preview", True)),
            bool(payload.get("learn", True)))
        await websocket.send(json.dumps({
            "type": "bgmi_read_result",
            "slide": slide,
            "preview": preview,
            "bgmi": server_state["bgmi"],
        }))
        await broadcast({"type": "state_sync", "data": server_state})

    elif kind == "bgmi_set_teams":
        teams = payload.get("teams") or {}
        clean = {}
        for slot, name in teams.items():
            name = str(name or "").strip()
            if name:
                clean[str(int(slot))] = name
        (config.setdefault("settings", {}))["teams"] = clean
        save_config(config)
        server_state["bgmi"]["teams"] = clean
        await broadcast({"type": "state_sync", "data": server_state})
        await websocket.send(json.dumps({
            "type": "bgmi_teams_saved", "count": len(clean)}))

    elif kind == "bgmi_clear_slides":
        # A new match, not a correction. Kept separate from
        # re-capturing one slide, which must never drop the others.
        server_state["bgmi"]["slides"] = {}
        server_state["bgmi"]["header"] = {
            "remaining": None, "teamsAlive": None}
        recombine()
        server_state["bgmi"]["blockedBy"] = ""
        await broadcast({"type": "state_sync", "data": server_state})

    elif kind == "bgmi_push_sheet":
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, push_alive_to_sheet, bool(payload.get("force", True)))
        await websocket.send(json.dumps({
            "type": "bgmi_sheet_result", "result": answer}))

    elif kind == "bgmi_capture_result":
        loop = asyncio.get_running_loop()
        preview, saved = await loop.run_in_executor(None, capture_result)
        result = server_state["bgmi"].get("result") or {}
        by_slot = {r["slot"]: r for r in result.get("rows") or [] if r.get("slot") is not None}
        rows = []
        for r in server_state["bgmi"].get("rows") or []:
            hit = by_slot.get(r["slot"])
            rows.append({"slot": r["slot"],
                         "kills": hit["finishes"] if hit else r.get("kills"),
                         "rank": hit["rank"] if hit else None,
                         "fromResults": bool(hit)})
        for slot, hit in by_slot.items():
            if not any(x["slot"] == slot for x in rows):
                rows.append({"slot": slot, "kills": hit["finishes"],
                             "rank": hit["rank"], "fromResults": True})
        rows.sort(key=lambda x: x["slot"])
        matched = len(by_slot)
        summary = ("read %d rank(s) from %d capture(s), matched %d to slots"
                   % (len(result.get("rows") or []), result.get("captures", 0), matched))
        if result.get("unresolved"):
            summary += "; NOT matched: " + "; ".join(
                "#%d %s" % (u["rank"], "/".join(u["names"][:2]))
                for u in result["unresolved"])
        if result.get("error"):
            summary = "results reader failed: " + result["error"]
        await websocket.send(json.dumps({
            "type": "bgmi_result_shot", "preview": preview,
            "saved": (saved + " -- " + summary) if saved else summary,
            "rows": rows, "result": result}))

    elif kind == "bgmi_push_results":
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, push_results_to_sheet,
            payload.get("game"), payload.get("rows") or [])
        await websocket.send(json.dumps({
            "type": "bgmi_results_result", "result": answer}))

    elif kind == "bgmi_set_settings":
        (config.setdefault("settings", {})
         ).update(payload.get("settings") or {})
        save_config(config)
        await broadcast({"type": "state_sync", "data": server_state})

    elif kind == "bgmi_set_region":
        region = payload.get("region") or {}
        if all(k in region for k in ("x", "y", "w", "h")):
            config["region"] = {k: int(region[k]) for k in ("x", "y", "w", "h")}
            save_config(config)
        await websocket.send(json.dumps({
            "type": "bgmi_region", "region": config["region"]}))

    elif kind == "bgmi_reload_glyphs":
        global templates
        templates = bp.load_templates()
        await websocket.send(json.dumps({
            "type": "bgmi_glyphs",
            "slot": sorted(templates.get("slot", {})),
            "kill": sorted(templates.get("kill", {})),
            "missing": bp.missing_digits(templates.get("slot", {})),
        }))
async def poll_loop():
    """Keep the slide that is ON SCREEN fresh, off unless asked for.

    Only one slide can be polled, because only one is in front of the
    camera -- so this re-captures the last one the operator took, on the
    assumption they left the panel where they put it. The other slides
    keep their last good reading, which is the right answer for a page
    nobody is looking at: it does not go stale, it just stops moving.

    No preview rides along. That is the expensive half, and nobody is
    necessarily watching.
    """
    while True:
        settings = config.get("settings") or {}
        state = server_state["bgmi"]
        active = state.get("activeSlide")
        if settings.get("continuousPoll") and active is not None:
            held = state["slides"].get(str(active)) or {}
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, capture_slide, active, held.get("firstSlot"),
                    False, False)
                await broadcast({"type": "state_sync", "data": server_state})
            except Exception as e:
                print("poll failed: %s" % e)
            await asyncio.sleep(float(settings.get("pollIntervalSeconds") or 0.5))
        else:
            await asyncio.sleep(0.5)


async def relay_client_loop():
    import websockets
    relay = config.get("relay") or {}
    if not relay.get("enabled"):
        return
    url, token = relay.get("url", ""), relay.get("token", "")
    if not token:
        token = os.environ.get("RELAY_TOKEN", "").strip()
    if not token:
        try:
            token = (Path(__file__).resolve().parent.parent / ".relay_token"
                     ).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if not url or not token:
        print("Relay enabled but url/token not both set -- skipping relay.")
        return
    sep = "&" if "?" in url else "?"
    connect_url = "%s%stoken=%s&page=bgmi_engine" % (url, sep, token)
    while True:
        try:
            async with websockets.connect(connect_url) as ws:
                print("Connected to cloud relay at %s" % url)
                await handle_client(ws)
        except Exception as e:
            print("Relay connection lost/failed (%s); retrying in 3s..." % e)
        await asyncio.sleep(3)


async def main():
    import websockets
    print("BGMI engine on ws://localhost:%d" % PORT)
    missing = bp.missing_digits(templates.get("slot", {}))
    if missing:
        # NOT a blocker, and the old wording said otherwise. Since slides
        # carry a declared starting slot, that typed number is what places
        # the rows; reading the digits on screen is only the check that
        # catches a typo. A missing digit disables that check and nothing
        # else -- captures work exactly as before.
        print("Digit %s not seen yet, so the typed slot is taken on trust "
              "and not cross-checked against the screen." % ", ".join(missing))
        print("Capturing a slide that starts at 12 teaches it, and the check "
              "turns itself on.")
    async with websockets.serve(handle_client, "localhost", PORT):
        await asyncio.gather(relay_client_loop(), poll_loop(), asyncio.Future())


if __name__ == "__main__":
    if "--serve" in sys.argv:
        asyncio.run(main())
    else:
        # Without --serve: one read against the current screen, printed.
        # Have the panel open before running it.
        first = None
        for arg in sys.argv[1:]:
            if arg.startswith("--first-slot="):
                first = int(arg.split("=", 1)[1])
        if first is None:
            print("Pass --first-slot=N: the panel can stop anywhere, so "
                  "nothing on screen says which teams are on it.")
            raise SystemExit(2)
        rows, header, diag, blocked = read_frame(grab_region(), first, learn=False)
        print(json.dumps({"rows": rows, "header": header,
                          "diagnostics": diag, "blockedBy": blocked}, indent=2))
