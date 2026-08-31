"""
Interactive calibration tool for Valorant's live HUD, organized into three
categories you can run independently:

  valo-ingame     Two round-score digit regions (either side of the round
                   timer, top-center) + the "SPIKE PLANTED" / "SPIKE
                   DEFUSED" announcement banner zone (draw it generously,
                   wider/taller than the text itself -- same reasoning as
                   MOBA's turtle_announcement calibration, the banner can
                   shift slightly).

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

  valo-postmatch   The post-match "Individually Sorted" scoreboard, as a
                   10-row x 5-column GRID (50 cells: K/D/A, ACS, K/D, First
                   Bloods, Plants per row) instead of one big blob region.
                   Player identity isn't read here at all -- the dashboard's
                   Import Stats table lets you assign each row to a player
                   and reorder rows manually, so only the STAT columns get
                   boxes. This is an interactive multi-step flow (see
                   calibrate_postmatch_grid() below), not a plain
                   drag-one-box-per-key loop like the other two categories.

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
    python calibrate_valorant.py valo-postmatch    # just the grid
    python calibrate_valorant.py valorant_team2_score   # one individual box
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

CATEGORIES = {
    "valo-ingame": ["valorant_team1_score", "valorant_team2_score", "valorant_announcement"],
    "valo-character": CHARSELECT_KEYS,
    "valo-postmatch": None,  # special interactive grid flow, not a plain key list
}

# All individually-addressable single-box keys (everything except the
# postmatch grid, which isn't one box), mapped back to which category (and
# therefore which file) each belongs to.
REGION_ORDER = CATEGORIES["valo-ingame"] + CATEGORIES["valo-character"]
KEY_TO_CATEGORY = {}
for _cat in ("valo-ingame", "valo-character"):
    for _key in CATEGORIES[_cat]:
        KEY_TO_CATEGORY[_key] = _cat

LABELS = {
    "valorant_team1_score": "TEAM 1 (left/attacker side) - ROUND SCORE",
    "valorant_team2_score": "TEAM 2 (right/defender side) - ROUND SCORE",
    "valorant_announcement": "SPIKE PLANTED / SPIKE DEFUSED banner zone - draw it generously, wider/taller than the text itself",
}
for i in range(5):
    LABELS[f"valorant_charselect_team1_{i}"] = f"CHAR-SELECT - Team 1 (defenders/left) Player {i+1} - crop tight to just the agent art"
    LABELS[f"valorant_charselect_team2_{i}"] = f"CHAR-SELECT - Team 2 (attackers/right) Player {i+1} - crop tight to just the agent art"

POSTMATCH_COLS = ["kda", "acs", "kd", "fb", "plants"]
POSTMATCH_COL_LABELS = {"kda": "K/D/A", "acs": "ACS", "kd": "K/D", "fb": "First Bloods", "plants": "Plants"}
POSTMATCH_ROWS = 10


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
    """The plain drag-one-box flow used for every valo-ingame/valo-character
    key. Returns {x,y,w,h} in absolute screen coords, or None if skipped."""
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


def calibrate_postmatch_grid(frame, monitor):
    """Interactive, three-step flow for the post-match stat grid:

      1. Drag ONE box around the whole 10-row stat area -- from the left
         edge of the K/D/A column to the right edge of the Plants column,
         top of row 1 to bottom of row 10. Do NOT include the player name
         column -- names aren't calibrated here.
      2. Drag 4 CYAN vertical divider lines (shown on an upscaled crop of
         that box) onto the real column boundaries, AND 9 ORANGE horizontal
         divider lines onto the real row boundaries -- both start evenly
         spaced and are independently draggable, so rows don't have to be
         perfectly even if the real table isn't. Click near a line to grab
         it (nearest one wins, whichever axis is closer); R resets both
         sets back to even spacing.
      3. A final confirmation window draws all 50 computed cells as green
         grid lines over the FULL frame -- press ENTER to save, R to redo
         from step 1, or ESC to cancel.

    Returns {region_key: {x,y,w,h}} in absolute screen coords (50 keys), or
    None if cancelled at any step.
    """
    win1 = ("Postmatch grid: drag ONE box around the WHOLE 10-row stat area "
            "(K/D/A through Plants ONLY, not the name column). ENTER=confirm")
    box = cv2.selectROI(win1, frame, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(win1)
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return None

    n_cols = len(POSTMATCH_COLS)
    scale = max(1.0, min(4.0, 1400 / max(w, 1)))
    crop = frame[y:y + h, x:x + w]
    disp = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    disp_h, disp_w = disp.shape[:2]

    def even_col_dividers():
        return [disp_w * i / n_cols for i in range(1, n_cols)]

    def even_row_dividers():
        return [disp_h * i / POSTMATCH_ROWS for i in range(1, POSTMATCH_ROWS)]

    col_dividers = even_col_dividers()
    row_dividers = even_row_dividers()
    dragging = {"kind": None, "idx": None}

    def nearest(divs, p):
        if not divs:
            return None
        idx = min(range(len(divs)), key=lambda i: abs(divs[i] - p))
        return idx if abs(divs[idx] - p) < 14 else None

    def on_mouse(event, mx, my, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            col_idx = nearest(col_dividers, mx)
            row_idx = nearest(row_dividers, my)
            col_dist = abs(col_dividers[col_idx] - mx) if col_idx is not None else 1e9
            row_dist = abs(row_dividers[row_idx] - my) if row_idx is not None else 1e9
            if col_idx is None and row_idx is None:
                dragging["kind"], dragging["idx"] = None, None
            elif col_dist <= row_dist:
                dragging["kind"], dragging["idx"] = "col", col_idx
            else:
                dragging["kind"], dragging["idx"] = "row", row_idx
        elif event == cv2.EVENT_MOUSEMOVE and dragging["kind"] is not None:
            if dragging["kind"] == "col":
                col_dividers[dragging["idx"]] = float(max(4, min(disp_w - 4, mx)))
            else:
                row_dividers[dragging["idx"]] = float(max(4, min(disp_h - 4, my)))
        elif event == cv2.EVENT_LBUTTONUP:
            dragging["kind"], dragging["idx"] = None, None

    win2 = ("Drag orange lines onto column edges (" +
            " | ".join(POSTMATCH_COL_LABELS[c] for c in POSTMATCH_COLS) +
            "), drag cyan lines onto each of the 10 row edges.   "
            "ENTER=confirm  R=reset  ESC=cancel")
    cv2.namedWindow(win2)
    cv2.setMouseCallback(win2, on_mouse)

    cancelled = False
    while True:
        canvas = disp.copy()
        for ry in row_dividers:
            ryi = int(ry)
            cv2.line(canvas, (0, ryi), (disp_w, ryi), (255, 210, 0), 2)
        for dx in col_dividers:
            dxi = int(dx)
            cv2.line(canvas, (dxi, 0), (dxi, disp_h), (0, 200, 255), 2)
        cv2.imshow(win2, canvas)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 32):  # ENTER / SPACE
            break
        if key == ord('r'):
            col_dividers = even_col_dividers()
            row_dividers = even_row_dividers()
        if key == 27:  # ESC
            cancelled = True
            break
    cv2.destroyWindow(win2)
    if cancelled:
        return None

    col_bounds_disp = [0.0] + sorted(col_dividers) + [float(disp_w)]
    col_bounds = [b / scale for b in col_bounds_disp]  # back to crop-local px
    row_bounds_disp = [0.0] + sorted(row_dividers) + [float(disp_h)]
    row_bounds = [b / scale for b in row_bounds_disp]

    regions = {}
    for r in range(POSTMATCH_ROWS):
        y0 = y + monitor["top"] + row_bounds[r]
        y1 = y + monitor["top"] + row_bounds[r + 1]
        for c, col_name in enumerate(POSTMATCH_COLS):
            x0 = x + monitor["left"] + col_bounds[c]
            x1 = x + monitor["left"] + col_bounds[c + 1]
            regions[f"valorant_postmatch_row{r}_{col_name}"] = {
                "x": int(round(x0)), "y": int(round(y0)),
                "w": int(round(x1 - x0)), "h": int(round(y1 - y0)),
            }

    preview = frame.copy()
    for key, reg in regions.items():
        px0, py0 = reg["x"] - monitor["left"], reg["y"] - monitor["top"]
        px1, py1 = px0 + reg["w"], py0 + reg["h"]
        cv2.rectangle(preview, (px0, py0), (px1, py1), (0, 220, 0), 1)
    win3 = "Confirm the 50-cell grid (green). ENTER=save  R=redo from step 1  ESC=cancel"
    cv2.imshow(win3, preview)
    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 32):
            cv2.destroyWindow(win3)
            return regions
        if key == ord('r'):
            cv2.destroyWindow(win3)
            return calibrate_postmatch_grid(frame, monitor)
        if key == 27:
            cv2.destroyWindow(win3)
            return None


def main():
    cfg = load_config()
    monitor_index = cfg.get("monitor", 1)

    requested = sys.argv[1:]
    if not requested:
        run_categories = list(CATEGORIES.keys())
        run_keys = []
    else:
        run_categories, run_keys = [], []
        for arg in requested:
            if arg in CATEGORIES:
                run_categories.append(arg)
            elif arg in REGION_ORDER:
                run_keys.append(arg)
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

    # Individual keys: group by category so each category's file is loaded
    # and saved once, not once per key.
    keys_by_category = {}
    for key in run_keys:
        keys_by_category.setdefault(KEY_TO_CATEGORY[key], []).append(key)

    for cat, keys in keys_by_category.items():
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

    for cat in run_categories:
        regions = load_regions(cat)
        if cat == "valo-postmatch":
            print("\n--- valo-postmatch: 10-row x 5-column stat grid ---")
            new_regions = calibrate_postmatch_grid(frame, monitor)
            if new_regions:
                regions.update(new_regions)
                print(f"Saved {len(new_regions)} postmatch grid cells.")
            else:
                print("Postmatch grid calibration cancelled -- kept previous values.")
        else:
            print(f"\n--- {cat} ---")
            for key in CATEGORIES[cat]:
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
