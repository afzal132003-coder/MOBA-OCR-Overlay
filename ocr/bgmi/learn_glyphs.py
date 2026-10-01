"""Teach the BGMI reader what a digit looks like, from a frame you label.

The reader matches digits against stored pictures of digits, so it can
only read a digit it has seen before. This is how it sees one.

  Slot digits, from any panel capture where you know the top-left slot:

      python ocr/bgmi/learn_glyphs.py shot.png --first-slot 12

  Kill digits need the counts spelled out, because nothing on screen
  says what they are -- one group of four per card, in reading order:

      python ocr/bgmi/learn_glyphs.py shot.png --first-slot 3 \
          --kills "0000 1110 3211 0200 0001 0100 0000 0000 1010"

WHEN YOU NEED THIS: a digit the store has never seen. The shipped store
is seeded from one frame showing slots 03-11, so it has every digit
except 2 -- which means slots 02, 12 and 20-25 cannot be fitted until a
frame containing one is learned. Any capture scrolled to the top or
bottom of the lobby will do it.

Learning is additive and capped at a few looks per digit, so running this
on a frame it has already seen changes nothing.
"""

import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgmi_panel as bp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--first-slot", type=int, required=True,
                    help="slot number of the top-left card in this frame")
    ap.add_argument("--kills", default="",
                    help='per-card kill counts, e.g. "0000 1110 3211"')
    ap.add_argument("--store", default=None, help="path to bgmi_glyphs.npz")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rgb = np.asarray(Image.open(args.image).convert("RGB"))
    luma = bp.to_luma(rgb)
    page = bp.read_page(rgb)
    cards = page["cards"]
    if not cards:
        print("No whole cards found in that image. Is the panel open, and is")
        print("the image the Android framebuffer rather than the whole window?")
        return 1
    print("%d cards, scroll offset %.1f (confidence %.0f)"
          % (len(cards), page["scroll"], page["scroll_score"]))

    kill_truth = args.kills.split()
    if kill_truth and len(kill_truth) != len(cards):
        print("--kills has %d groups but %d cards were found."
              % (len(kill_truth), len(cards)))
        return 1

    stores = bp.load_templates(args.store)
    before = {k: sum(len(v) for v in d.values()) for k, d in stores.items()}

    for i, c in enumerate(cards):
        label = "%02d" % (args.first_slot + i)
        for cell, bm in enumerate(c["slot_bitmaps"]):
            if bm is not None:
                bp.remember(stores, "slot", label[cell], bm)
        if not kill_truth:
            continue
        group = kill_truth[i]
        if len(group) != bp.PLAYERS_PER_CARD:
            print("card %d: expected %d digits, got %r"
                  % (i, bp.PLAYERS_PER_CARD, group))
            return 1
        cx = bp.CARD_X[c["column"]]
        for p in range(bp.PLAYERS_PER_CARD):
            bm = bp.kill_bitmap(luma, cx, bp.row_y(c["top"], p))
            if bm is not None:
                bp.remember(stores, "kill", group[p], bm)

    after = {k: sum(len(v) for v in d.values()) for k, d in stores.items()}
    for store in ("slot", "kill"):
        digits = sorted(stores.get(store, {}))
        gained = after.get(store, 0) - before.get(store, 0)
        print("  %-5s %2d looks (+%d)  digits: %s"
              % (store, after.get(store, 0), gained,
                 "".join(digits) if digits else "(none)"))
    missing = [d for d in "0123456789" if d not in stores.get("slot", {})]
    if missing:
        print("  slot digits still unseen: %s -- slots containing them "
              "cannot be fitted yet" % ", ".join(missing))

    if args.dry_run:
        print("\n--dry-run, nothing written")
        return 0
    bp.save_templates(stores, args.store)
    print("\nsaved to %s" % (args.store or bp.GLYPH_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
