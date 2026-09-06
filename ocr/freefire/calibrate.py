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
# TWO blocks, not four: the lobby shows four cards but the lower pair sits
# behind the SPECTATOR LIST bar and is routinely clipped, so reading only
# the fully-visible top pair and scrolling more often is the more reliable
# trade.
LOBBY_KEYS = [f"freefire_lobby_block{i}" for i in range(1, 3)]

CATEGORIES = {
    "ff-live": [
        "freefire_killfeed",
        "freefire_sidetable",
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
    CATEGORIES["ff-live"] + CATEGORIES["ff-loadout"] + CATEGORIES["ff-lobby"]
)

CATEGORY_BLURB = {
    "ff-live": "LIVE IN-GAME boxes -- have a match actually running, with the killfeed and 12-team side table on screen.",
    "ff-loadout": "LOADOUT boxes -- have a player's loadout card on screen (the one Num5 captures).",
    "ff-lobby": "PRE-MATCH LOBBY boxes (2 squad cards) -- have the lobby team list on screen, scrolled to the top.",
}

LABELS = {
    "freefire_lobby_block1": "LOBBY BLOCK 1 (top-LEFT squad card) - draw around ONE squad's whole block: its team name AND its player names underneath. Use a fully-visible card, not one clipped by the spectator bar",
    "freefire_lobby_block2": "LOBBY BLOCK 2 (top-RIGHT squad card) - same, the squad beside the first",
    "freefire_killfeed": "KILLFEED / KNOCKOUT FEED (the scrolling elimination/knockdown log) - raw text only for now, draw around the whole feed area",
    "freefire_sidetable": "12-TEAM SIDE TABLE (alive status / kills per team) - raw text only for now, draw around the whole table",
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
        else:
            print(f"Skipped {key} (kept previous value if any)")

    cfg["regions"] = regions
    save_config(cfg)
    print("\nAll regions saved to freefire_config.json. Re-run this script any time to recalibrate.")


if __name__ == "__main__":
    main()
