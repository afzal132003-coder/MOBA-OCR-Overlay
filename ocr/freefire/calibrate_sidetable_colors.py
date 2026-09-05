"""
Measures the side table's alive/knocked/eliminated bar colours from a real
frame, and checks the column split, writing both into freefire_config.json.

Why this exists rather than thresholds hardcoded in the engine: the actual
bar colours depend on Free Fire's own palette AND on the capture pipeline
it arrives through (OBS colour space, encoder, any capture-card
processing). Picking numbers without measuring them on the real setup
would be a guess presented as a measurement, and the failure mode is
quiet -- a slightly-off threshold reports wrong alive counts on air rather
than erroring. So: point it at a live frame, click the actual bars, and it
records what they really are.

Run with the game showing the side table with a MIX of states visible
(some squads full, at least one knocked, at least one eliminated):

    python calibrate_sidetable_colors.py

Then it walks you through clicking one example of each state.
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

CONFIG_PATH = Path(__file__).parent / "freefire_config.json"
STATES = ["alive", "knocked", "eliminated"]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def grab_sidetable(cfg):
    region = cfg.get("regions", {}).get("freefire_sidetable")
    if not region or region.get("w", 0) <= 0:
        print("freefire_sidetable isn't calibrated yet.")
        print("Run:  python calibrate.py freefire_sidetable")
        return None
    with mss.mss() as sct:
        shot = sct.grab({
            "left": region["x"], "top": region["y"],
            "width": region["w"], "height": region["h"],
        })
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def sample_colour(img, state):
    """Click one bar of the given state; returns its mean BGR.

    Averaged over a small box around the click rather than the single
    pixel under the cursor, so a slightly off-centre click or a bar with
    a gradient/border doesn't record an unrepresentative value."""
    picked = {}
    display = img.copy()
    scale = max(1, 400 // max(1, img.shape[1]))
    if scale > 1:
        display = cv2.resize(display, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_NEAREST)

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            picked["xy"] = (int(x / scale), int(y / scale))

    window = f"Click a bar that is: {state.upper()}   (then any key)"
    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse)
    while True:
        cv2.imshow(window, display)
        key = cv2.waitKey(20) & 0xFF
        if "xy" in picked or key != 255:
            break
    cv2.destroyWindow(window)

    if "xy" not in picked:
        return None
    x, y = picked["xy"]
    half = 3
    patch = img[max(0, y - half):y + half + 1, max(0, x - half):x + half + 1]
    if patch.size == 0:
        return None
    return [round(float(v), 1) for v in patch.reshape(-1, 3).mean(axis=0)]


def main():
    cfg = load_config()
    img = grab_sidetable(cfg)
    if img is None:
        return

    print("The side table was captured. For each state you'll get a window:")
    print("click one bar that is in that state, then press any key.")
    print("Press a key WITHOUT clicking to skip a state.\n")

    palette = dict(cfg.get("sidetable_colors") or {})
    for state in STATES:
        colour = sample_colour(img, state)
        if colour is None:
            print(f"  {state}: skipped (kept {palette.get(state, 'nothing')})")
            continue
        palette[state] = colour
        print(f"  {state}: BGR {colour}")

    if not palette:
        print("\nNothing sampled -- config unchanged.")
        return

    cfg["sidetable_colors"] = palette
    save_config(cfg)
    print(f"\nSaved sidetable_colors to {CONFIG_PATH.name}.")
    print("The engine will now classify bars against these measured values")
    print("instead of its coarse fallback. Re-run this if the capture setup,")
    print("resolution, or OBS colour settings change.")

    missing = [s for s in STATES if s not in palette]
    if missing:
        print(f"\nStill unmeasured: {', '.join(missing)}.")
        print("Those states will be matched to whichever sampled colour is")
        print("nearest, which may be wrong -- re-run when the table shows them.")


if __name__ == "__main__":
    main()
