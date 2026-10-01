"""What the BGMI panel reader can actually do, measured on a real frame.

The fixture is an unmodified capture of the observer panel, mid-match,
with three squads wiped and one squad down to its last player -- so it
exercises lit rows, greyed rows, and a card that is a mix of both.

The digit test is LEAVE-ONE-OUT on purpose. Templates cut from the same
crop they are then asked to identify would score 36/36 and prove nothing;
each glyph here is identified using only the OTHER thirty-five.

Run: python ocr/bgmi/test_bgmi_panel.py
"""

import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgmi_panel as bp

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "panel_9cards_1919x1079.png")

# Read off the fixture by eye, in card reading order (slots 03..11).
TRUTH_ALIVE = [4, 0, 4, 0, 4, 0, 4, 4, 1]
TRUTH_KILLS = [
    [0, 0, 0, 0],   # 03 MYT
    [1, 1, 1, 0],   # 04 MMx      -- wiped, kills must still be read
    [3, 2, 1, 1],   # 05 QDK
    [0, 2, 0, 0],   # 06          -- wiped
    [0, 0, 0, 1],   # 07 NEBULA/Aox
    [0, 1, 0, 0],   # 08 4Tw      -- wiped
    [0, 0, 0, 0],   # 09 Raven
    [0, 0, 0, 0],   # 10 NMSz
    [1, 0, 1, 0],   # 11 KOx      -- one alive, three dead
]

failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def main():
    rgb = np.asarray(Image.open(FIXTURE).convert("RGB"))
    luma = bp.to_luma(rgb)

    print("\nalive / dead")
    cards = bp.read_page(rgb)
    got_alive = [sum(1 for p in c["players"] if p["alive"]) for c in cards]
    check("every row classified", all(p["alive"] is not None
                                      for c in cards for p in c["players"]))
    check("alive counts exact", got_alive == TRUTH_ALIVE,
          "got %s" % got_alive)

    # The gap it relies on, stated as a number so a future UI change that
    # narrows it fails here rather than silently halving a team.
    lit, grey = [], []
    for i in range(bp.CARDS_PER_PAGE):
        cx, cy = bp.card_origin(i)
        for p in range(bp.PLAYERS_PER_CARD):
            y = bp.row_y(cy, p)
            strip = luma[y:y + bp.ROW_H, cx + bp.IGN_DX:cx + bp.IGN_DX + bp.IGN_W]
            (lit if TRUTH_ALIVE[i] and cards[i]["players"][p]["alive"] else grey
             ).append(float(np.percentile(strip, 97)))
    gap = min(lit) - max(grey)
    check("lit/greyed separation > 60", gap > 60,
          "lit min %.0f, greyed max %.0f, gap %.0f" % (min(lit), max(grey), gap))

    print("\nkill digits")
    glyphs, labels = [], []
    for i in range(bp.CARDS_PER_PAGE):
        cx, cy = bp.card_origin(i)
        for p in range(bp.PLAYERS_PER_CARD):
            bm = bp.glyph_bitmap(luma, cx + bp.ELIM_DX, bp.row_y(cy, p) - 3,
                                 bp.ELIM_W, bp.ROW_H + 6)
            if bm is not None:
                glyphs.append(bm)
                labels.append(str(TRUTH_KILLS[i][p]))
    check("all 36 digits extracted, lit and greyed alike", len(glyphs) == 36,
          "got %d" % len(glyphs))

    hits = 0
    for i, g in enumerate(glyphs):
        others = {}
        for j, h in enumerate(glyphs):
            if j != i:
                others.setdefault(labels[j], []).append(h)
        hits += (bp.best_match(g, others) == labels[i])
    check("leave-one-out digit accuracy >= 34/36", hits >= 34,
          "got %d/36" % hits)

    print("\nrows and invariants")
    cards = bp.resolve_slots(cards, first_slot=3)
    check("slots run 3..11", [c["slot"] for c in cards] == list(range(3, 12)))

    # Hand the reader perfect digits so team_rows is tested for its summing
    # and not for the OCR already measured above.
    for i, c in enumerate(cards):
        for p in range(4):
            c["players"][p]["kills"] = TRUTH_KILLS[i][p]
    rows = bp.team_rows(cards)
    check("team kills summed", [r["kills"] for r in rows] ==
          [sum(k) for k in TRUTH_KILLS])
    check("wiped teams flagged", [r["slot"] for r in rows if r["eliminated"]]
          == [4, 6, 8])
    check("wiped teams keep their kills",
          next(r for r in rows if r["slot"] == 4)["kills"] == 3)

    # One unreadable player must void that team's score, not shrink it.
    cards[0]["players"][2]["kills"] = None
    check("one unreadable player voids the team total",
          bp.team_rows(cards)[0]["kills"] is None)

    check("kills ignore a drop", bp.accept_kills(5, 3) == 5)
    check("kills ignore a wild jump", bp.accept_kills(5, 19) == 5)
    check("kills take a legal step", bp.accept_kills(5, 6) == 6)
    check("alive is not constrained (revives raise it)",
          not hasattr(bp, "accept_alive"))

    print("\nmerging two capture sources")
    page_a = [{"slot": 7, "alive": 3, "kills": 4}]
    page_b = [{"slot": 7, "alive": 3, "kills": 4}, {"slot": 8, "alive": 0, "kills": 2}]
    merged, conflicts = bp.merge_pages([page_a, page_b])
    check("overlapping sources that agree merge cleanly",
          len(merged) == 2 and not conflicts)
    _, conflicts = bp.merge_pages([page_a, [{"slot": 7, "alive": 2, "kills": 4}]])
    check("disagreement is reported, not silently resolved", len(conflicts) == 1)

    check("consecutive-run check rejects a bad scroll position",
          bp.slots_look_consecutive([3, 4, 5, None, 7], 3) is True
          and bp.slots_look_consecutive([3, 4, 9], 3) is False
          and bp.slots_look_consecutive([None] * 9, 3) is None)

    print("\nKNOWN GAP, not a failure: a player on 10+ kills writes two")
    print("digits into the box. Every count in this fixture is single-digit,")
    print("so two-digit reads are UNTESTED -- they currently fall back to an")
    print("unreadable crop, which voids that team's total rather than")
    print("reporting a wrong one. Needs a frame with a 10-kill player.")

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
