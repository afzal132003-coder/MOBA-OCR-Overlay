"""
Interactive calibration tool for Valorant's live HUD, organized into three
categories you can run independently:

  valo-ingame     The "SPIKE PLANTED" / "SPIKE DEFUSED" announcement banner
                   zone (draw it generously, wider/taller than the text
                   itself -- same reasoning as MOBA's turtle_announcement
                   calibration, the banner can shift slightly) + the round
                   timer itself (the M:SS clock between the two scores,
                   cropped tight to just the digits) -- that one isn't
                   shown anywhere, it's purely a safety net so the spike
                   badge auto-clears itself once the round moves on, even
                   if the exact end-of-round banner text isn't recognized.
                   Round score itself is NOT calibrated here anymore --
                   an explicit request, OCR on it wasn't reliable enough
                   to be worth it, it's manual-only from the dashboard now.

  valo-character   Ten character-select slot regions (5 defenders/left, 5
                   attackers/right) -- FIXED per-slot boxes on purpose: the
                   character-select screen's layout doesn't reorder by
                   performance the way the post-match scoreboard does, so a
                   tight box per slot is safe here. Draw each one tightly
                   around just that player's agent portrait art (not their
                   name or the lock-in icon) -- this feeds a color-signature
                   best-guess match against the known agent portraits, not
                   literal OCR, so a tight, representative crop matters more
                   than it does for text regions.

  valo-postmatch   The post-match "Individually Sorted" scoreboard: 10 rows
                   x 5 stat columns (ACS, K/D/A, Econ, First Bloods,
                   Plants -- real on-screen left-to-right order), 50
                   INDIVIDUALLY drag-one-box-calibrated cells, same plain
                   flow as the other two categories -- an explicit request,
                   after the earlier grid-line-drag approach (compute all
                   50 cells from 13 draggable divider lines) turned out
                   less reliable than just calibrating each cell directly
                   against the real text. Player identity isn't calibrated
                   at all -- the dashboard's Import Stats table lets you
                   assign each row to a player and reorder rows manually.

Have Valorant's in-game HUD (or a paused frame with the relevant screen
visible) on screen when you run this. Region coordinates are written into
THREE separate files, one per category (valorant_regions_ingame.json,
valorant_regions_character.json, valorant_regions_postmatch.json) --
recalibrating one category never touches another's data. Engine settings
(monitor, tesseract path, etc.) stay in valorant_config.json, separate from
config.json (MOBA) and freefire_config.json, since this is a third,
independent engine.

Usage:
    python calibrate_valorant.py                  # all 3 categories, in order
    python calibrate_valorant.py valo-ingame       # just that category
    python calibrate_valorant.py valo-postmatch    # just the 50-cell scoreboard
    python calibrate_valorant.py valorant_announcement   # one individual box
Run again any time your game window moves or resizes.
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "valorant_config.json"

REGION_FILES = {
    "valo-ingame": Path(__file__).parent / "valorant_regions_ingame.json",
    "valo-character": Path(__file__).parent / "valorant_regions_character.json",
    "valo-postmatch": Path(__file__).parent / "valorant_regions_postmatch.json",
}

CHARSELECT_KEYS = [f"valorant_charselect_team1_{i}" for i in range(5)] + \
                  [f"valorant_charselect_team2_{i}" for i in range(5)]

POSTMATCH_COLS = ["acs", "kda", "econ", "fb", "plants"]
POSTMATCH_COL_LABELS = {"acs": "ACS", "kda": "K/D/A", "econ": "Econ", "fb": "First Bloods", "plants": "Plants"}
POSTMATCH_ROWS = 10
POSTMATCH_KEYS = [
    f"valorant_postmatch_row{r}_{col}"
    for r in range(POSTMATCH_ROWS)
    for col in POSTMATCH_COLS
]

CATEGORIES = {
    "valo-ingame": ["valorant_announcement", "valorant_round_timer"],
    "valo-character": CHARSELECT_KEYS,
    "valo-postmatch": POSTMATCH_KEYS,
}

# Every individually-addressable box, mapped back to which category (and
# therefore which file) each belongs to.
REGION_ORDER = CATEGORIES["valo-ingame"] + CATEGORIES["valo-character"] + CATEGORIES["valo-postmatch"]
KEY_TO_CATEGORY = {}
for _cat, _keys in CATEGORIES.items():
    for _key in _keys:
        KEY_TO_CATEGORY[_key] = _cat

LABELS = {
    "valorant_announcement": "SPIKE PLANTED / SPIKE DEFUSED banner zone - draw it generously, wider/taller than the text itself",
    "valorant_round_timer": "ROUND TIMER (the M:SS clock between the two scores) - crop tight to just the digits, used only as a spike-badge safety net, not displayed anywhere",
}
for i in range(5):
    LABELS[f"valorant_charselect_team1_{i}"] = f"CHAR-SELECT - Team 1 (defenders/left) Player {i+1} - crop tight to just the agent art"
    LABELS[f"valorant_charselect_team2_{i}"] = f"CHAR-SELECT - Team 2 (attackers/right) Player {i+1} - crop tight to just the agent art"
for r in range(POSTMATCH_ROWS):
    for col in POSTMATCH_COLS:
        LABELS[f"valorant_postmatch_row{r}_{col}"] = (
            f"POST-MATCH - Row {r+1} (on-screen top-to-bottom position, NOT a fixed "
            f"player) - {POSTMATCH_COL_LABELS[col]}"
        )


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_regions(category):
    path = REGION_FILES[category]
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_regions(category, regions):
    with open(REGION_FILES[category], "w", encoding="utf-8") as f:
        json.dump(regions, f, indent=2)


def calibrate_single_box(frame, monitor, key):
    """The plain drag-one-box flow used for every key in every category.
    Returns {x,y,w,h} in absolute screen coords, or None if skipped."""
    window_name = f"Select: {LABELS[key]}  (ENTER=confirm, C=skip)"
    box = cv2.selectROI(window_name, frame, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(window_name)
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return None
    return {
        "x": int(x + monitor["left"]), "y": int(y + monitor["top"]),
        "w": int(w), "h": int(h),
    }


def main():
    cfg = load_config()
    monitor_index = cfg.get("monitor", 1)

    requested = sys.argv[1:]
    # Build a single per-category key list to run, whether the request was
    # by category name, individual key, or a mix of both -- a category arg
    # expands to all its keys, an individual key arg adds just that one, and
    # everything gets merged (no duplicates) so e.g. "valo-character
    # valorant_announcement" in one invocation runs both correctly.
    if not requested:
        keys_by_category = {cat: list(keys) for cat, keys in CATEGORIES.items()}
    else:
        keys_by_category = {}
        for arg in requested:
            if arg in CATEGORIES:
                cat = arg
                keys_by_category.setdefault(cat, [])
                for key in CATEGORIES[cat]:
                    if key not in keys_by_category[cat]:
                        keys_by_category[cat].append(key)
            elif arg in REGION_ORDER:
                cat = KEY_TO_CATEGORY[arg]
                keys_by_category.setdefault(cat, [])
                if arg not in keys_by_category[cat]:
                    keys_by_category[cat].append(arg)
            else:
                print(f"Unknown region/category: {arg}")
                print(f"Valid categories: {', '.join(CATEGORIES.keys())}")
                print(f"Valid individual keys: {', '.join(REGION_ORDER)}")
                return

    with mss.mss() as sct:
        print("Available monitors:")
        for i, m in enumerate(sct.monitors):
            print(f"  [{i}] {m}")
        monitor = sct.monitors[monitor_index]
        shot = sct.grab(monitor)
        frame = np.array(shot)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    print(f"\nUsing monitor index {monitor_index}: {monitor}")
    print("A screenshot window will open for each region. Press 'c' to skip one (keeps its previous value).\n")

    for cat, keys in keys_by_category.items():
        print(f"\n--- {cat} ---")
        regions = load_regions(cat)
        for key in keys:
            result = calibrate_single_box(frame, monitor, key)
            if result:
                regions[key] = result
                print(f"Saved {key}: {result}")
            else:
                print(f"Skipped {key} (kept previous value if any)")
        save_regions(cat, regions)
        print(f"-> {len(regions)} regions saved to {REGION_FILES[cat].name}")

    print("\nDone. Each category's regions live in their own file -- recalibrating one "
          "never touches another's data. Re-run this script any time to recalibrate.")


if __name__ == "__main__":
    main()
