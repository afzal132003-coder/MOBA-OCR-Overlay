"""
Marks where the side table's columns sit, by dragging boxes on a real frame.

Why this exists: the engine previously fell back to guessed width fractions
(team 0-55%, elims 55-75%, bars 75-100%). Measured against a real capture
those were wrong in a way that quietly corrupted the read -- the team
column started at 0% and so swallowed each squad's LOGO, and the OCR duly
returned the logo's artwork as part of the name ("age ARs", "NS NXR",
"| INP", "ee TEAM"). Guessing proportions that the operator can simply
show us is the wrong trade, so this asks.

Three boxes, on a frame with the side table visible:
  1. TEAM NAME text  -- the name only, NOT the logo beside it
  2. ELIMS number
  3. ALIVE bars      -- all four, edge to edge

Only the horizontal extent is used; drag the full height of one row (or
the whole column, either works) and don't worry about vertical precision.

Saves "sidetable_columns" into freefire_config.json.

    python calibrate_sidetable_columns.py            # uses config's monitor
    python calibrate_sidetable_columns.py --monitor 1
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "freefire_config.json"

PROMPTS = [
    ("team", "TEAM NAME text only -- do NOT include the team logo beside it"),
    ("elims", "ELIMS number column"),
    ("alive", "ALIVE bars -- all four, left edge of the first to right edge of the last"),
]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def main():
    cfg = load_config()
    argv = sys.argv[1:]
    monitor_index = cfg.get("monitor", 1)
    if "--monitor" in argv:
        at = argv.index("--monitor")
        try:
            monitor_index = int(argv[at + 1])
        except (IndexError, ValueError):
            print("--monitor needs a number, e.g. --monitor 1")
            return

    region = (cfg.get("regions") or {}).get("freefire_sidetable")
    if not region or region.get("w", 0) <= 0:
        print("freefire_sidetable isn't calibrated yet.")
        print("Run:  calibrate_freefire_live.bat")
        return

    with mss.mss() as sct:
        if monitor_index >= len(sct.monitors):
            print(f"Monitor {monitor_index} doesn't exist -- this PC has "
                  f"{len(sct.monitors) - 1} screen(s).")
            return
        shot = sct.grab({
            "left": region["x"], "top": region["y"],
            "width": region["w"], "height": region["h"],
        })
    table = cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
    width = table.shape[1]

    # The side table is a tall narrow strip; shown at native size it's an
    # awkward target to drag inside, so scale it up for the picking.
    scale = max(1, min(4, 900 // max(1, width)))
    display = cv2.resize(table, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_NEAREST) if scale > 1 else table

    print("\nThe side table was captured. Drag a box around each column in turn,")
    print("then press ENTER. Press C to skip one (keeps its previous value).")
    print("Only the LEFT and RIGHT edges matter -- vertical extent is ignored.\n")

    columns = dict(cfg.get("sidetable_columns") or {})
    for key, prompt in PROMPTS:
        window = f"Drag: {prompt}   (ENTER=confirm, C=skip)"
        box = cv2.selectROI(window, display, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(window)
        x, _, w, _ = box
        if w <= 0:
            print(f"  {key}: skipped (kept {columns.get(key, 'nothing')})")
            continue
        lo = (x / scale) / width
        hi = ((x + w) / scale) / width
        columns[key] = [round(max(0.0, lo), 4), round(min(1.0, hi), 4)]
        print(f"  {key}: {columns[key]}  (x {int(x/scale)}..{int((x+w)/scale)} of {width}px)")

    if not columns:
        print("\nNothing marked -- config unchanged.")
        return

    cfg["sidetable_columns"] = columns
    save_config(cfg)
    print(f"\nSaved sidetable_columns to {CONFIG_PATH.name}:")
    for key, span in columns.items():
        print(f"   {key:6s} {span}")
    print("\nRestart the engine for it to pick these up.")

    missing = [k for k, _ in PROMPTS if k not in columns]
    if missing:
        print(f"\nStill unmarked: {', '.join(missing)} -- those fall back to the")
        print("engine's guessed fractions, which is what produced logo artwork")
        print("inside team names. Re-run to finish them.")


if __name__ == "__main__":
    main()
