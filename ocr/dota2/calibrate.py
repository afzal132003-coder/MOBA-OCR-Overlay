"""
Interactive calibration tool for dota2_engine.py.

Lives in ocr/dota2/ alongside dota2_engine.py and dota2_config.json --
Dota 2's own folder, same isolation reasoning as valorant/freefire/moba:
different game, different layout, own config and state so nothing gets
tangled together.

This is POST-MATCH ONLY -- the end-game scoreboard (Overview tab), which
is where KDA, gold, team score and game duration are all shown at once.
There is no live in-game capture here and no IGN capture: player identity
is assigned by the operator afterwards (same "pick who this is" pattern
already used for Free Fire's pre-match roster), because in-game names
carry clan tags and symbols ("Verlin Raed [ZGØ]") that are exactly the
kind of text OCR gets wrong and a human reads correctly at a glance.

Ten player boxes (5 per team, top to bottom matching the scoreboard's own
row order) -- each ONE box per player, capturing gold and KDA together
since they sit stacked in the same small column on the real screen. Two
team-score boxes and one duration box round out the thirteen.

    python calibrate.py              # all 13 boxes
    python calibrate.py --monitor 1  # override the configured monitor
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "dota2_config.json"

TEAM_SLOTS = 5

REGION_ORDER = (
    [f"dota2_team1_p{i}" for i in range(TEAM_SLOTS)]
    + [f"dota2_team2_p{i}" for i in range(TEAM_SLOTS)]
    + ["dota2_team1_score", "dota2_team2_score", "dota2_duration"]
)

LABELS = {}
for _i in range(TEAM_SLOTS):
    LABELS[f"dota2_team1_p{_i}"] = (
        f"TEAM 1 (left), PLAYER {_i + 1} of 5 -- box around GOLD and K/D/A together, "
        "top to bottom in the scoreboard's own row order. Not the hero portrait or name."
    )
    LABELS[f"dota2_team2_p{_i}"] = (
        f"TEAM 2 (right), PLAYER {_i + 1} of 5 -- same as team 1: GOLD and K/D/A together."
    )
LABELS["dota2_team1_score"] = "TEAM 1 (left) total score -- the big kill-count number, e.g. '24'"
LABELS["dota2_team2_score"] = "TEAM 2 (right) total score -- the big kill-count number, e.g. '46'"
LABELS["dota2_duration"] = "GAME DURATION -- the MM:SS clock, e.g. '29:51'"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def main():
    cfg = load_config()

    requested = sys.argv[1:]
    monitor_index = cfg.get("monitor", 1)
    if "--monitor" in requested:
        at = requested.index("--monitor")
        try:
            monitor_index = int(requested[at + 1])
        except (IndexError, ValueError):
            print("--monitor needs a number, e.g. --monitor 1")
            return
        del requested[at:at + 2]

    keys_to_calibrate = REGION_ORDER

    with mss.mss() as sct:
        print("\nAvailable monitors:")
        for i, m in enumerate(sct.monitors):
            marker = "  <-- using this one" if i == monitor_index else ""
            label = "all monitors combined" if i == 0 else f"monitor {i}"
            print(f"  [{i}] {label}: {m['width']}x{m['height']} at ({m['left']},{m['top']}){marker}")
        if monitor_index >= len(sct.monitors):
            print(f"\nMonitor {monitor_index} doesn't exist -- this PC has "
                  f"{len(sct.monitors) - 1} screen(s).")
            print("Re-run with a valid one, e.g.:  calibrate.py --monitor 1")
            return
        monitor = sct.monitors[monitor_index]
        shot = sct.grab(monitor)
        frame = np.array(shot)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    print(f"\nUsing monitor index {monitor_index}: {monitor}")
    print("Have the Dota 2 post-match Overview screen up before running this.")
    print("If the screenshot that opens is the WRONG screen, close it and re-run")
    print("with a different number, e.g.:  calibrate.py --monitor 1")
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
    print("\nAll regions saved to dota2_config.json. Re-run this script any time to recalibrate.")


if __name__ == "__main__":
    main()
