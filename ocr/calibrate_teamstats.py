"""
Interactive calibration tool for the Team Stats Overlay's live per-player
K/D/A (30 regions: 2 teams x 5 players x kills/deaths/assists).

Separate from calibrate.py (the main HUD's kills/gold/objectives/score)
on purpose -- this is a much bigger, slower calibration pass the operator
does once, on their own time, after the primary live overlay is already
up and running. Writes into the SAME config.json ocr_engine.py already
reads (regions live alongside the main HUD's), just via a different
entry point so the two calibration passes stay independent chores.

Run any time your game window moves/resizes, or to redo a subset:
    python calibrate_teamstats.py t1p1_kills t1p1_deaths t1p1_assists
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "config.json"

REGION_ORDER = [
    f"t{team}p{p}_{stat}"
    for team in (1, 2) for p in range(1, 6) for stat in ("kills", "deaths", "assists")
]

LABELS = {
    f"t{team}p{p}_{stat}": f"TEAM {team} - PLAYER {p} - {stat.upper()}"
    for team in (1, 2) for p in range(1, 6) for stat in ("kills", "deaths", "assists")
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
    print(f"Calibrating {len(keys_to_calibrate)} region(s) -- this is a long one, take your time.")
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
