"""
Interactive calibration tool for freefire_engine.py.

Lives in ocr/freefire/ alongside freefire_engine.py and freefire_config.json
-- a different game from the MOBA calibration scripts (ocr/moba/) and
Valorant's (ocr/valorant/), each grouped in their own folder with their
own engine + config + state so they don't get tangled together. Takes one
screenshot of your chosen monitor, then lets you drag a box around each
region in turn. Saves pixel coordinates into freefire_config.json for
freefire_engine.py to use.

Run again any time your game window moves or resizes.

Regions are grouped into CATEGORIES, because they're calibrated at
completely different moments: the live boxes need a match in progress, the
loadout boxes need the loadout reveal, and the lobby boxes need the
pre-match lobby -- so being forced through all of them in one pass means
sitting on the wrong screen for most of it. Pass a category name to do just
that group (each also has its own .bat next to this file):

    python calibrate.py ff-live         # killfeed + side table
    python calibrate.py ff-loadout      # the 8 loadout boxes
    python calibrate.py ff-lobby        # the 2 pre-match lobby squad cards

A single region key still works for a one-box touch-up, and category names
and keys can be mixed in one run:

    python calibrate.py freefire_sidetable
    python calibrate.py ff-loadout freefire_killfeed
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "freefire_config.json"

# Pre-match lobby: the roster can be read off the lobby screen before any
# game has been played, which is the one moment the result files can't help
# (they only exist afterwards). Calibrated as the visible squad BLOCKS,
# with the operator scrolling between captures -- twelve fixed team boxes
# would be wrong, since which squads occupy those slots changes as the list
# scrolls.
#
# TWO cards, not four: the lobby shows four but the lower pair sits behind
# the SPECTATOR LIST bar and is routinely clipped, so reading only the
# fully-visible top pair and scrolling more often is the more reliable
# trade.
#
# Each card is TWO boxes -- team name and the player-name column -- rather
# than one box around the whole card. One box was measurably worse: it
# necessarily spans the squad logo, the leader tick and the MAX badges, and
# OCR read those as text. A real capture came back with "sv ARISE ESPORTS"
# for the team and 'ARs.KHAN', 'v', ']', 'ARs.LORD', ']' for the players --
# the tick and badge fragments crowding out two of the four real names.
# Boxes drawn around text only give OCR nothing else to find.
LOBBY_KEYS = [
    "freefire_lobby_block1_team", "freefire_lobby_block1_players",
    "freefire_lobby_block2_team", "freefire_lobby_block2_players",
]

CATEGORIES = {
    "ff-live": [
        "freefire_killfeed",
        "freefire_sidetable",
    ],
    # Four anchor boxes instead of one big region -- see build_alive_grid()
    # in freefire_engine.py. Row 1 / player 1 and player 2 give the
    # horizontal pitch between the 4 alive slots in a row. The vertical
    # pitch comes from row 1 and the LAST row (both player 1), NOT the
    # very next row down -- spanning the full table and dividing by the
    # row count spreads a hand-drawn box's inevitable 1px of imprecision
    # across all 11 gaps, instead of multiplying that same 1px error out
    # 11 times over if it were measured between two adjacent rows. Every
    # other row/player position is derived from those two pitches rather
    # than calibrated individually -- exact instead of auto-detected, and
    # 4 boxes instead of up to 60.
    "ff-alive-grid": [
        "freefire_alive_r1p1",
        "freefire_alive_r1p2",
        "freefire_alive_rlastp1",
        "freefire_alive_r1elim",
        "freefire_alive_r1team",
    ],
    # Optional per-column pins. NOT part of ff-alive-grid: columns 3 and
    # 4 are derived from the p1->p2 spacing and that is right on evenly
    # spaced artwork, so walking every operator through two extra boxes
    # they don't need would be worse than useful. Calibrate one by name
    # when a column's crop actually sits off-centre -- see
    # build_alive_grid, which prefers a drawn box over the derived one.
    "ff-alive-columns": [
        "freefire_alive_r1p3",
        "freefire_alive_r1p4",
    ],
    # The client's own elimination banner -- "#7  ARISE  ELIMINATED"
    # across the top-left when a squad is wiped. Three boxes because the
    # three things it carries are wanted separately: whether a banner is
    # up at all (the word ELIMINATED, which never changes and so can be
    # matched as a picture, every poll, for almost nothing), which squad
    # (the name), and where they finished (the rank).
    #
    # Worth reading even though the side table says who is alive, because
    # the banner says it FIRST, says it unambiguously, and brings the
    # finishing place with it. The alive bars have to be inferred through
    # whatever is happening on screen, and an explosion over a row makes
    # a dead bar and a live one measure the same.
    "ff-elim-banner": [
        "freefire_elim_banner",
        "freefire_elim_banner_team",
        "freefire_elim_banner_rank",
    ],
    "ff-loadout": [
        "freefire_loadout",
        "freefire_loadout_ign",
        "freefire_loadout_active",
        "freefire_loadout_passive1",
        "freefire_loadout_passive2",
        "freefire_loadout_passive3",
        "freefire_loadout_pet",
        "freefire_loadout_equipment",
    ],
    "ff-lobby": LOBBY_KEYS,
}

REGION_ORDER = (
    CATEGORIES["ff-live"] + CATEGORIES["ff-alive-grid"]
    + CATEGORIES["ff-alive-columns"] + CATEGORIES["ff-elim-banner"]
    + CATEGORIES["ff-loadout"] + CATEGORIES["ff-lobby"]
)

CATEGORY_BLURB = {
    "ff-live": "LIVE IN-GAME boxes -- have a match actually running, with the killfeed and 12-team side table on screen.",
    "ff-alive-columns": "ALIVE GRID optional column pins -- only for a column whose crop sits off-centre; normally derived, so normally skipped.",
    "ff-alive-grid": "ALIVE GRID anchors -- have a match running with the 12-team side table visible, same screen as ff-live. Draw TIGHT boxes, exactly matching one indicator/number each time -- these boxes are used to work out the position of every row. The TEAM NAME box is what lets a row be recognised when the client reorders the table mid-match; skip it and rows stay wherever they were assigned by hand.",
    "ff-elim-banner": "ELIMINATION BANNER -- the \"#7  ARISE  ELIMINATED\" strip the client throws up top-left when a squad is wiped. It is only up for a few seconds, so run this with --wait, watch the game, and press ENTER the moment a banner appears; the picture is frozen at that point and you draw on it at your leisure. The boxes are in fixed positions, so any one banner calibrates them for good.",
    "ff-loadout": "LOADOUT boxes -- have a player's loadout card on screen (the one Num5 captures).",
    "ff-lobby": "PRE-MATCH LOBBY boxes (2 squad cards) -- have the lobby team list on screen, scrolled to the top.",
}

LABELS = {
    "freefire_lobby_block1_team": "LOBBY CARD 1 (top-LEFT): TEAM NAME text ONLY - exclude the squad logo on its left and the 'Score: N' on its right",
    "freefire_lobby_block1_players": "LOBBY CARD 1 (top-LEFT): the 4 PLAYER NAMES as one tall box - names only, exclude the tick marks on the left and the MAX badges on the right",
    "freefire_lobby_block2_team": "LOBBY CARD 2 (top-RIGHT): TEAM NAME text ONLY - exclude logo and score",
    "freefire_lobby_block2_players": "LOBBY CARD 2 (top-RIGHT): the 4 PLAYER NAMES as one tall box - names only, no ticks, no MAX badges",
    "freefire_killfeed": "KILLFEED / KNOCKOUT FEED (the scrolling elimination/knockdown log) - raw text only for now, draw around the whole feed area",
    "freefire_sidetable": "12-TEAM SIDE TABLE (alive status / kills per team) - raw text only for now, draw around the whole table",
    "freefire_alive_r1p1": "ALIVE GRID: ROW 1 (topmost team), PLAYER 1 (leftmost) alive indicator - tight box around just that one bar/icon",
    "freefire_alive_r1p2": "ALIVE GRID: ROW 1, PLAYER 2 (next one to the right) alive indicator - same tight box, one slot over",
    "freefire_alive_rlastp1": "ALIVE GRID: LAST ROW (bottom-most team, row 12), PLAYER 1 (leftmost) alive indicator - same tight box as the very first one, but on the LAST row, not the second",
    "freefire_alive_r1elim": "ALIVE GRID: ROW 1's ELIMINATION COUNT number - tight box around just that number",
    "freefire_alive_r1team": "ALIVE GRID: ROW 1's TEAM NAME text - tight box around just the name, excluding the squad logo to its left and the alive bars to its right. Draw it WIDE enough for the LONGEST team name in the lobby, not just row 1's - every row reuses this same width",
    "freefire_elim_banner": "ELIM BANNER: a TIGHT box around the word ELIMINATED itself (the red word on the black bar) - nothing else, no logo, no team name. This one word is identical on every banner, which is what lets the engine spot a banner at all; the coloured bar above it is a different colour for every squad and cannot be used for that",
    "freefire_elim_banner_team": "ELIM BANNER: just the TEAM NAME text on the coloured bar (\"ARISE\") - tight box around the text, excluding the squad logo to its left. Wide enough for the LONGEST name in the lobby",
    "freefire_elim_banner_rank": "ELIM BANNER: just the FINISHING PLACE number, without the # (the \"7\" of \"#7\") - tight box around the digits. This is the place that squad finished, which is what the sheet push wants",
    "freefire_alive_r1p3": "ALIVE GRID (optional): ROW 1, PLAYER 3 alive indicator - only needed if column 3's crop sits off-centre; otherwise it is derived from players 1 and 2",
    "freefire_alive_r1p4": "ALIVE GRID (optional): ROW 1, PLAYER 4 alive indicator - only needed if column 4's crop sits off-centre; otherwise it is derived from players 1 and 2",
    "freefire_loadout": "LOADOUT CARD, WHOLE (the full player HUD card) - kept as the overall visual record; the per-slot boxes below are what actually get identified",
    "freefire_loadout_ign": "LOADOUT: PLAYER IGN - draw tightly around just the name text on the card (this one is read as TEXT, not matched as an icon)",
    "freefire_loadout_active": "LOADOUT: ACTIVE CHARACTER (the BIG character portrait) - draw tightly around just that icon",
    "freefire_loadout_passive1": "LOADOUT: PASSIVE CHARACTER 1 (first of the three small portraits) - tight box around just that icon",
    "freefire_loadout_passive2": "LOADOUT: PASSIVE CHARACTER 2 (second small portrait) - tight box around just that icon",
    "freefire_loadout_passive3": "LOADOUT: PASSIVE CHARACTER 3 (third small portrait) - tight box around just that icon",
    "freefire_loadout_pet": "LOADOUT: PET icon - tight box around just the pet icon",
    "freefire_loadout_equipment": "LOADOUT: EQUIPMENT icon - tight box around just the equipped item icon",
}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def main():
    cfg = load_config()

    # --monitor N overrides freefire_config.json's own "monitor" for this
    # run only. Worth having as a flag rather than a config-only setting:
    # picking the wrong screen is the single most common way calibration
    # goes wrong (you get a screenshot of the desktop instead of the game),
    # and it's much easier to retry with a different number than to stop
    # and hand-edit JSON.
    requested = sys.argv[1:]
    # --wait grabs the screen when you say so, instead of the instant the
    # command starts. Needed for anything that is only on screen for a
    # moment: the elimination banner is up for a few seconds, which is
    # not long enough to alt-tab to a console and type a command, but is
    # ample to press a key you are already holding.
    wait_for_key = "--wait" in requested
    if wait_for_key:
        requested.remove("--wait")
    monitor_index = cfg.get("monitor", 1)
    if "--monitor" in requested:
        at = requested.index("--monitor")
        try:
            monitor_index = int(requested[at + 1])
        except (IndexError, ValueError):
            print("--monitor needs a number, e.g. --monitor 2")
            return
        del requested[at:at + 2]
    if requested:
        # A category expands to its keys, an individual key adds just
        # itself, and the two can be mixed in one run. Order follows
        # REGION_ORDER rather than the order typed, so a mixed run still
        # walks the screen in a sensible sequence, and duplicates collapse.
        unknown = [a for a in requested if a not in REGION_ORDER and a not in CATEGORIES]
        if unknown:
            print(f"Unknown argument(s): {', '.join(unknown)}")
            print(f"Categories: {', '.join(CATEGORIES)}")
            print(f"Region keys: {', '.join(REGION_ORDER)}")
            return
        wanted = set()
        for arg in requested:
            if arg in CATEGORIES:
                wanted.update(CATEGORIES[arg])
            else:
                wanted.add(arg)
        keys_to_calibrate = [k for k in REGION_ORDER if k in wanted]
        for name in (c for c in CATEGORIES if c in requested):
            print(f"\n{CATEGORY_BLURB[name]}")
    else:
        keys_to_calibrate = REGION_ORDER
        print("\nCalibrating EVERY region. These belong to different screens --")
        print("live match, loadout card, and pre-match lobby -- so you'll be on")
        print("the wrong screen for most of them. Prefer one category at a time:")
        for name, blurb in CATEGORY_BLURB.items():
            print(f"  python calibrate.py {name:12s} {blurb.split(' -- ')[0]}")

    with mss.mss() as sct:
        print("\nAvailable monitors:")
        for i, m in enumerate(sct.monitors):
            marker = "  <-- using this one" if i == monitor_index else ""
            label = "all monitors combined" if i == 0 else f"monitor {i}"
            print(f"  [{i}] {label}: {m['width']}x{m['height']} at ({m['left']},{m['top']}){marker}")
        if monitor_index >= len(sct.monitors):
            print(f"\nMonitor {monitor_index} doesn't exist -- this PC has "
                  f"{len(sct.monitors) - 1} screen(s).")
            print("Re-run with a valid one, e.g.:  calibrate.py ff-live --monitor 1")
            return
        monitor = sct.monitors[monitor_index]
        if wait_for_key:
            print("")
            print("  Waiting. Go to the game, and the MOMENT the thing you")
            print("  want to calibrate is on screen, press ENTER here.")
            print("  (The screen is grabbed when you do, and everything after")
            print("   that is drawn on the frozen picture, so it does not")
            print("   matter if it has gone by then.)")
            try:
                input("  ENTER to grab: ")
            except EOFError:
                print("  No console to wait on -- grabbing now.")
        shot = sct.grab(monitor)
        frame = np.array(shot)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    print(f"\nUsing monitor index {monitor_index}: {monitor}")
    print("If the screenshot that opens is the WRONG screen, close it and re-run")
    print("with a different number, e.g.:  calibrate.py ff-live --monitor 1")
    print("A screenshot window will open for each region, one at a time.")
    print("Drag a box around it, then press ENTER or SPACE to confirm.")
    print("Press 'c' to skip a region (keeps its previous value, if any).\n")

    regions = cfg.get("regions", {})

    for key in keys_to_calibrate:
        window_name = f"Select: {LABELS[key]}  (ENTER=confirm, C=skip)"
        box = cv2.selectROI(window_name, frame, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(window_name)
        x, y, w, h = box
        if w > 0 and h > 0:
            regions[key] = {
                "x": int(x + monitor["left"]),
                "y": int(y + monitor["top"]),
                "w": int(w),
                "h": int(h),
            }
            print(f"Saved {key}: {regions[key]}")
            if key == "freefire_elim_banner":
                # Keep the PICTURE, not just the box. This region is the
                # word ELIMINATED, and the engine spots a banner by
                # matching that picture -- which it cannot do until it
                # has one. Learning it at runtime means waiting for a
                # banner and reading it with OCR, and OCR is poor at this
                # particular crop: the word is red on black, and the
                # readers here are built for white text (a real attempt
                # returned "OS"). Right now, though, a banner is
                # definitely on screen -- it is what was just drawn
                # around -- so the picture is simply taken.
                ref = frame[y:y + h, x:x + w]
                ref_path = CONFIG_PATH.parent / "freefire_banner_flag.png"
                cv2.imwrite(str(ref_path), ref)
                print(f"  ...and kept the picture of it: {ref_path}")
                print("     (delete that file and recalibrate if the "
                      "client's banner ever changes)")
        else:
            print(f"Skipped {key} (kept previous value if any)")

    cfg["regions"] = regions
    save_config(cfg)
    print("\nAll regions saved to freefire_config.json. Re-run this script any time to recalibrate.")


if __name__ == "__main__":
    main()
