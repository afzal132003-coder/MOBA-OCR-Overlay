"""
Interactive calibration tool for Valorant's live HUD (14 regions):
- Two round-score digit regions (either side of the round timer,
  top-center).
- One announcement region -- the "SPIKE PLANTED" / "SPIKE DEFUSED" banner
  zone, also top-center, just below the score. Draw it generously (wider/
  taller than the banner itself), same reasoning as MOBA's
  turtle_announcement calibration -- the banner can shift slightly.
- One post-match scoreboard region -- the WHOLE "Individually Sorted"
  results table (all 10 rows, both teams). Unlike MOBA's Battle Report/Gold
  calibration (one tight box per fixed player slot), this is deliberately
  ONE big box around the entire table: Valorant's scoreboard is
  individually sorted by ACS, so which row a given player lands on changes
  match to match -- a fixed per-slot box would silently read the wrong
  player's numbers. One full-table capture + fuzzy name-matching (same
  approach as the upload/paste path) works regardless of row order, so
  draw this one generously around the full table, not tight to any one row.
- Ten character-select slot regions (5 defenders/left, 5 attackers/right)
  -- UNLIKE the scoreboard above, these ARE fixed per-slot boxes on
  purpose: the character-select screen's layout doesn't reorder by
  performance the way the post-match scoreboard does, so a tight box per
  slot is safe here. Draw each one tightly around just that player's agent
  portrait art (not their name or the lock-in icon) -- this feeds a
  color-signature best-guess match against the known agent portraits, not
  literal OCR, so a tight, representative crop matters more than it does
  for text regions.

Have Valorant's in-game HUD on screen (a live round, or paused on a frame
where the score is visible) when you run this. Writes into
valorant_config.json for valorant_engine.py to use -- separate from
config.json (MOBA) and freefire_config.json, since this is a third,
independent engine.

Run again any time your game window moves or resizes. To recalibrate only
one region, pass its key as an argument, e.g.:
    python calibrate_valorant.py valorant_team2_score
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "valorant_config.json"

CHARSELECT_KEYS = [f"valorant_charselect_team1_{i}" for i in range(5)] + \
                  [f"valorant_charselect_team2_{i}" for i in range(5)]

REGION_ORDER = [
    "valorant_team1_score", "valorant_team2_score", "valorant_announcement",
    "valorant_postmatch_scoreboard",
] + CHARSELECT_KEYS

LABELS = {
    "valorant_team1_score": "TEAM 1 (left/attacker side) - ROUND SCORE",
    "valorant_team2_score": "TEAM 2 (right/defender side) - ROUND SCORE",
    "valorant_announcement": "SPIKE PLANTED / SPIKE DEFUSED banner zone - draw it generously, wider/taller than the text itself",
    "valorant_postmatch_scoreboard": "POST-MATCH SCOREBOARD - draw around the WHOLE 10-row Individually Sorted table, both teams",
}
for i in range(5):
    LABELS[f"valorant_charselect_team1_{i}"] = f"CHAR-SELECT - Team 1 (defenders/left) Player {i+1} - crop tight to just the agent art"
    LABELS[f"valorant_charselect_team2_{i}"] = f"CHAR-SELECT - Team 2 (attackers/right) Player {i+1} - crop tight to just the agent art"


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
    print("A screenshot window will open for each score, one at a time.")
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
    print("\nAll regions saved to valorant_config.json. Re-run this script any time to recalibrate.")


if __name__ == "__main__":
    main()
