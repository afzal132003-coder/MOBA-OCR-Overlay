"""
Interactive calibration tool for the post-match screens (30 regions: the
"Overall" screen's per-player gold, and the "Data" screen's per-player Hero
Damage / Damage Taken).

Was folded into calibrate.py (now calibrate_hud.py) originally; split out
into its own entry point since it's a completely different pair of screens
(post-game, not the live in-game HUD) that the operator calibrates once and
separately, typically right after their first real post-match. Writes into
the SAME config.json ocr_engine.py already reads -- only the calibration
entry point is separate, not the underlying config.

Have the relevant post-match screen (paused/still showing) on screen when
you run this -- these are read live off the screen the same fast way the
in-game kills/gold regions are, not from an uploaded screenshot.

Run again any time your game window moves/resizes, or to redo a subset:
    python calibrate_postmatch.py postgame_gold_team1_0
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "config.json"

POSTGAME_GOLD_KEYS = [
    f"postgame_gold_{team}_{i}" for team in ("team1", "team2") for i in range(5)
]
POSTGAME_BATTLE_KEYS = [
    f"postgame_{field}_{team}_{i}"
    for team in ("team1", "team2") for i in range(5) for field in ("dealt", "taken")
]
REGION_ORDER = POSTGAME_GOLD_KEYS + POSTGAME_BATTLE_KEYS

LABELS = {}
for _team in ("team1", "team2"):
    _team_label = "TEAM 1" if _team == "team1" else "TEAM 2"
    for _i in range(5):
        LABELS[f"postgame_gold_{_team}_{_i}"] = (
            f"POST MATCH (Overall screen) - {_team_label} PLAYER {_i+1} - GOLD"
        )
        LABELS[f"postgame_dealt_{_team}_{_i}"] = (
            f"POST MATCH (Data screen) - {_team_label} PLAYER {_i+1} - HERO DAMAGE"
        )
        LABELS[f"postgame_taken_{_team}_{_i}"] = (
            f"POST MATCH (Data screen) - {_team_label} PLAYER {_i+1} - DAMAGE TAKEN"
        )


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
