"""
Interactive calibration tool for freefire_engine.py.

Lives in its own ocr/freefire/ folder, separate from the MOBA calibration
scripts (calibrate_hud.py / calibrate_postmatch_gold.py /
calibrate_postmatch_hero.py / calibrate_teamstats.py) one level up -- a
different game with its own config.json equivalent
(freefire_config.json, still in ocr/ alongside freefire_engine.py, not
moved here), so calibrating into the wrong file would silently do nothing.
Takes one screenshot of your chosen monitor, then lets you drag a box
around the killfeed and the 12-team side table. Saves pixel coordinates
into freefire_config.json for freefire_engine.py to use.

Run again any time your game window moves or resizes. To recalibrate only
one region, pass its key as an argument, e.g.:
    python calibrate.py freefire_sidetable
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent.parent / "freefire_config.json"

REGION_ORDER = ["freefire_killfeed", "freefire_sidetable", "freefire_loadout"]

LABELS = {
    "freefire_killfeed": "KILLFEED / KNOCKOUT FEED (the scrolling elimination/knockdown log) - raw text only for now, draw around the whole feed area",
    "freefire_sidetable": "12-TEAM SIDE TABLE (alive status / kills per team) - raw text only for now, draw around the whole table",
    "freefire_loadout": "LOADOUT CAPTURE (the player HUD card - IGN, weapon, active/passive/pet/equipment icons) - draw tightly around just that card, this gets screenshotted on every Num5 press",
}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def main():
    cfg = load_config()
    monitor_index = cfg.get("monitor", 1)

    requested = sys.argv[1:]
    if requested:
        unknown = [k for k in requested if k not in REGION_ORDER]
        if unknown:
            print(f"Unknown region key(s): {', '.join(unknown)}")
            print(f"Valid keys: {', '.join(REGION_ORDER)}")
            return
        keys_to_calibrate = requested
    else:
        keys_to_calibrate = REGION_ORDER

    with mss.mss() as sct:
        print("Available monitors:")
        for i, m in enumerate(sct.monitors):
            print(f"  [{i}] {m}")
        monitor = sct.monitors[monitor_index]
        shot = sct.grab(monitor)
        frame = np.array(shot)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    print(f"\nUsing monitor index {monitor_index}: {monitor}")
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
