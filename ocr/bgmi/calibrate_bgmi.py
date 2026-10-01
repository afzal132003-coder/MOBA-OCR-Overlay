"""
Interactive calibration for bgmi_engine.py.

BGMI needs exactly ONE region: the Android picture itself. That is the
whole calibration, and it is also the one thing that is easy to get
subtly wrong -- BlueStacks draws a title bar across the top and a
toolbar down the right, and if either is included then every offset in
bgmi_panel.py is out by its width and nothing reads.

So this does not just save coordinates and hope. It saves them, then
immediately reads the panel back through the real reader and tells you
what it found. Nine cards means it is right. Zero means it is not, and
it says what is most likely wrong rather than leaving you to guess.

    python calibrate_bgmi.py                 # use the monitor in the config
    python calibrate_bgmi.py --monitor 1     # pick a different screen

Have the OBSERVER TEAM PANEL open in the game before running this.
"""

import json
import sys
from pathlib import Path

import cv2
import mss
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import bgmi_panel as bp

CONFIG_PATH = Path(__file__).parent / "bgmi_config.json"


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def grab(monitor):
    with mss.mss() as sct:
        if monitor >= len(sct.monitors):
            print("Monitor %d does not exist -- this machine has %d."
                  % (monitor, len(sct.monitors) - 1))
            sys.exit(2)
        shot = sct.grab(sct.monitors[monitor])
        return np.array(shot)[:, :, :3], sct.monitors[monitor]


def verify(region, monitor):
    """Read the panel back through the real reader, and say what happened."""
    box = {"left": region["x"], "top": region["y"],
           "width": region["w"], "height": region["h"]}
    with mss.mss() as sct:
        shot = sct.grab(box)
    rgb = np.array(shot)[:, :, :3][:, :, ::-1]          # BGRA -> RGB

    page = bp.read_page(rgb)
    cards = page["cards"]
    print("")
    print("  picture size      %d x %d" % (region["w"], region["h"]))
    print("  card rows found   %d" % page["card_rows"])
    print("  whole cards       %d" % len(cards))
    print("  scroll offset     %.1f  (match strength %.0f)"
          % (page["scroll"], page["scroll_score"]))

    if not cards:
        print("")
        print("  NOTHING READ. The usual causes, in order:")
        print("   1. The box included the BlueStacks title bar or the right-hand")
        print("      toolbar. Drag it to the game picture only -- the very edge")
        print("      of the rendered frame, no window chrome.")
        print("   2. The team panel was not open in the game.")
        print("   3. The emulator is not running at 1920x1080. Check Settings ->")
        print("      Display; bgmi_panel.py's offsets are measured against that.")
        return False

    alive = sum(1 for c in cards for p in c["players"] if p["alive"])
    print("  players read       %d, of which %d alive"
          % (len(cards) * bp.PLAYERS_PER_CARD, alive))

    if region["w"] != 1920 or region["h"] != 1080:
        print("")
        print("  NOTE: the picture is not 1920x1080. Every offset in")
        print("  bgmi_panel.py was measured at that size, so a different one")
        print("  will drift. Set the emulator's display to 1920x1080.")
    print("")
    print("  Looks right. %d cards is one full page of the panel."
          % len(cards) if len(cards) == 9 else
          "  %d cards -- expected 9 for a full page. If the panel was"
          " mid-scroll that is normal; otherwise widen the box." % len(cards))
    return True


def main():
    cfg = load_config()
    monitor = int(cfg.get("monitor", 1))
    args = sys.argv[1:]
    if "--monitor" in args:
        monitor = int(args[args.index("--monitor") + 1])

    print("BGMI calibration -- one region: the Android picture.")
    print("Monitor %d. Have the observer TEAM PANEL open in the game.\n" % monitor)

    frame, mon = grab(monitor)
    print("Drag a box around the GAME PICTURE ONLY.")
    print("Not the BlueStacks window -- leave out the title bar at the top")
    print("and the toolbar down the right-hand side.")
    print("Enter or Space to accept, C to cancel.\n")

    window = "BGMI - drag the game picture, then press Enter"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, min(1600, frame.shape[1]), min(900, frame.shape[0]))
    box = cv2.selectROI(window, frame, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()

    if not box or box[2] < 50 or box[3] < 50:
        print("Cancelled -- nothing saved.")
        return 1

    region = {"x": int(mon["left"]) + int(box[0]),
              "y": int(mon["top"]) + int(box[1]),
              "w": int(box[2]), "h": int(box[3])}
    cfg["monitor"] = monitor
    cfg["region"] = region
    save_config(cfg)
    print("Saved to bgmi_config.json: x=%d y=%d w=%d h=%d"
          % (region["x"], region["y"], region["w"], region["h"]))

    print("\nReading it back through the real panel reader...")
    ok = verify(region, monitor)
    print("\nRestart the BGMI engine so it picks this up:")
    print("    python bgmi_engine.py --serve")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
