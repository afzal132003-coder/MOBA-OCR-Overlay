"""
Interactive calibration tool.

Takes one screenshot of your chosen monitor, then lets you drag a box
around each stat on screen (kills, objectives, gold, each team's series
score). Saves the pixel coordinates into config.json for ocr_engine.py
to use.

Run this again any time your game window moves or resizes. To
recalibrate only specific regions (leaving the others untouched), pass
their keys as arguments, e.g.:
    python calibrate.py team1_series_score team2_series_score
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "config.json"

REGION_ORDER = [
    "team1_kills", "team1_objectives", "team1_gold",
    "team2_gold", "team2_objectives", "team2_kills",
    "team1_series_score", "team2_series_score",
]

LABELS = {
    "team1_kills": "TEAM 1 - KILLS",
    "team1_objectives": "TEAM 1 - OBJECTIVES / TOWERS",
    "team1_gold": "TEAM 1 - GOLD",
    "team2_gold": "TEAM 2 - GOLD",
    "team2_objectives": "TEAM 2 - OBJECTIVES / TOWERS",
    "team2_kills": "TEAM 2 - KILLS",
    "team1_series_score": "TEAM 1 - SERIES SCORE (single number)",
    "team2_series_score": "TEAM 2 - SERIES SCORE (single number)",
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
    print("A screenshot window will open for each stat, one at a time.")
    print("Drag a box tightly around just the NUMBER, then press ENTER or SPACE to confirm.")
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
    print("\nAll regions saved to config.json. Re-run this script any time to recalibrate.")


if __name__ == "__main__":
    main()
