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
from PIL import Image

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


def list_monitors():
    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors]


def choose_monitor(default):
    """Show the screens and ask, rather than silently taking one.

    This rig runs the game on the second screen -- the Free Fire config
    has monitor 2 -- so a calibrator that quietly defaults to 1 hands you
    a screenshot of the wrong display and no clue why. Printing the
    resolutions makes the right answer obvious, and --monitor still skips
    the question for anyone who already knows.
    """
    monitors = list_monitors()
    real = monitors[1:]                       # [0] is the virtual all-screens one
    if len(real) <= 1:
        return 1
    print("Screens on this machine:")
    for i, m in enumerate(real, start=1):
        mark = "  <- current setting" if i == default else ""
        print("   %d.  %d x %d  at (%d, %d)%s"
              % (i, m["width"], m["height"], m["left"], m["top"], mark))
    print("")
    try:
        answer = input("Which screen is the game on? [%d] " % default).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    try:
        pick = int(answer)
    except ValueError:
        print("Not a number -- using %d." % default)
        return default
    if not (1 <= pick <= len(real)):
        print("No screen %d -- using %d." % (pick, default))
        return default
    return pick


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
    # Same rescale the engine does, so what this verifies is what will
    # actually be read -- a check against unscaled pixels would pass or
    # fail for reasons the running engine never sees.
    if (region["w"], region["h"]) != (1920, 1080):
        rgb = np.asarray(Image.fromarray(rgb).resize((1920, 1080), Image.LANCZOS))

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
        print("  NOTE: the picture is %dx%d, not 1920x1080. The engine scales"
              % (region["w"], region["h"]))
        print("  it to the reference size before reading, so this is handled --")
        print("  the numbers above already come from the scaled frame.")
    print("")
    print("  Looks right. %d cards is one full page of the panel."
          % len(cards) if len(cards) == 9 else
          "  %d cards -- expected 9 for a full page. If the panel was"
          " mid-scroll that is normal; otherwise widen the box." % len(cards))
    return True


def main():
    cfg = load_config()
    monitor = int(cfg.get("monitor", 2))
    args = sys.argv[1:]
    asked = "--monitor" in args
    if asked:
        monitor = int(args[args.index("--monitor") + 1])

    print("BGMI calibration -- one region: the Android picture.")
    print("Have the observer TEAM PANEL open in the game.\n")

    if not asked:
        monitor = choose_monitor(monitor)
    cfg["monitor"] = monitor
    print("\nUsing screen %d.\n" % monitor)

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
