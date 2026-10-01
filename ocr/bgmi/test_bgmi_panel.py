"""What the BGMI panel reader can actually do, measured on a real frame.

The fixture is an unmodified capture of the observer panel, mid-match,
with three squads wiped and one squad down to its last player -- so it
exercises lit rows, greyed rows, and a card that is a mix of both.

TWO THINGS THIS TEST IS CAREFUL ABOUT:

  * The kill-digit score is LEAVE-ONE-OUT. Templates cut from the same
    crop they are then asked to identify would score 36/36 and prove
    nothing; each glyph is identified using only the OTHER thirty-five.

  * The slot-number templates ARE cut from this frame, because one frame
    is all we have. So the slot tests do not claim the matcher is
    accurate -- they claim the page FIT is robust: that losing any one
    card's digits entirely still lands the page on the right slot. Real
    accuracy needs frames from other matches.

Scroll is simulated by cropping rows off the top of the frame. That
exercises the phase arithmetic honestly, but it is not the same as the
game scrolling -- a real mid-scroll frame may also be motion-blurred,
which nothing here tests.

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
FIRST_SLOT = 3
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
TRUTH_CARD_TOPS = [163, 448, 734]

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

    print("\nfinding the grid (it scrolls, and can stop mid-row)")
    def near(got, want, tol=2):
        return len(got) == len(want) and all(abs(a - b) <= tol
                                             for a, b in zip(got, want))

    tops, offset, score = bp.detect_card_tops(luma)
    # Tolerance, not equality: the phase search steps in half-pixels, and a
    # card top one pixel out changes nothing -- the rows it reads are 26px
    # tall. Asserting the exact integer would fail on an immaterial tie.
    check("card rows located", near(tops, TRUTH_CARD_TOPS), "got %s" % tops)
    check("scroll phase found", abs(offset - 185) <= 2, "offset %.1f" % offset)

    recovered = 0
    shifts = [0, 17, 41, 58, 96, 143, 200, 261]
    for s in shifts:
        off, _ = bp.detect_scroll(bp.to_luma(rgb[s:, :]))
        truth = (185 - s) % bp.CARD_PITCH
        err = min(abs(off - truth), bp.CARD_PITCH - abs(off - truth))
        recovered += err <= 2.0
    check("scroll recovered from any phase", recovered == len(shifts),
          "%d of %d within 2px" % (recovered, len(shifts)))

    # A card clipped by the viewport edge must be dropped, not read short.
    clipped, _, _ = bp.detect_card_tops(luma, viewport=(155, 800))
    check("clipped card dropped, not read half-height",
          near(clipped, [163, 448]), "got %s" % clipped)

    print("\nalive / dead")
    page = bp.read_page(rgb)
    cards = page["cards"]
    check("nine whole cards read", len(cards) == 9, "got %d" % len(cards))
    got_alive = [sum(1 for p in c["players"] if p["alive"]) for c in cards]
    check("every row classified", all(p["alive"] is not None
                                      for c in cards for p in c["players"]))
    check("alive counts exact", got_alive == TRUTH_ALIVE, "got %s" % got_alive)

    # The gap it relies on, stated as a number so a future UI change that
    # narrows it fails here rather than silently halving a team.
    lit, grey = [], []
    for i, c in enumerate(cards):
        cx = bp.CARD_X[c["column"]]
        for p in range(bp.PLAYERS_PER_CARD):
            y = bp.row_y(c["top"], p)
            strip = luma[y:y + bp.ROW_H, cx + bp.IGN_DX:cx + bp.IGN_DX + bp.IGN_W]
            (lit if c["players"][p]["alive"] else grey).append(
                float(np.percentile(strip, 97)))
    gap = min(lit) - max(grey)
    check("lit/greyed separation > 60", gap > 60,
          "lit min %.0f, greyed max %.0f, gap %.0f" % (min(lit), max(grey), gap))

    print("\nkill digits (leave-one-out)")
    glyphs, labels = [], []
    for i, c in enumerate(cards):
        cx = bp.CARD_X[c["column"]]
        for p in range(bp.PLAYERS_PER_CARD):
            bm = bp.kill_bitmap(luma, cx, bp.row_y(c["top"], p))
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

    print("\nslot numbers (templates seeded from this frame -- see docstring)")
    slot_templates = {}
    for i, c in enumerate(cards):
        label = "%02d" % (FIRST_SLOT + i)
        for cell, bm in enumerate(c["slot_bitmaps"]):
            if bm is not None:
                slot_templates.setdefault(label[cell], []).append(bm)
    check("both digits read on all nine cards",
          all(b is not None for c in cards for b in c["slot_bitmaps"]))

    # Same digit on different cards vs different digits: the margin that
    # makes the matcher possible at all. Dim (wiped) cards are included.
    within, cross = [], []
    flat = [(d, b) for d, v in slot_templates.items() for b in v]
    for a in range(len(flat)):
        for b in range(a + 1, len(flat)):
            s = bp.similarity(flat[a][1], flat[b][1])
            (within if flat[a][0] == flat[b][0] else cross).append(s)
    check("same digit agrees better than different digits disagree",
          min(within) > max(cross),
          "within min %.3f, cross max %.3f" % (min(within), max(cross)))

    first, margin = bp.fit_slot_run(cards, slot_templates)
    check("page fitted to the right starting slot", first == FIRST_SLOT,
          "got %s (margin %.3f)" % (first, margin))

    # Robustness is the real claim: any one card going unreadable must not
    # move the page, because moving it would relabel every team on screen.
    survived = 0
    for i in range(len(cards)):
        trial = [dict(c) for c in cards]
        trial[i] = dict(trial[i], slot_bitmaps=[None, None])
        got, _ = bp.fit_slot_run(trial, slot_templates)
        survived += (got == FIRST_SLOT)
    check("fit survives any single card being unreadable",
          survived == len(cards), "%d of %d" % (survived, len(cards)))

    wrong, _ = bp.fit_slot_run(cards, {})
    check("no templates means no guess", wrong is None)

    print("\nrows and invariants")
    cards = bp.assign_slots(cards, FIRST_SLOT)
    check("slots run 3..11", [c["slot"] for c in cards] == list(range(3, 12)))

    # Hand the reader perfect digits so team_rows is tested for its summing
    # and not for the OCR already measured above.
    for i, c in enumerate(cards):
        for p in range(4):
            c["players"][p]["kills"] = TRUTH_KILLS[i][p]
    rows = bp.team_rows(cards)
    check("team kills summed", [r["kills"] for r in rows] ==
          [sum(k) for k in TRUTH_KILLS])
    check("wiped teams flagged",
          [r["slot"] for r in rows if r["eliminated"]] == [4, 6, 8])
    check("wiped teams keep their kills",
          next(r for r in rows if r["slot"] == 4)["kills"] == 3)

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

    print("\nheader checksum, and refusing to guess")
    check("store knows it has never seen a 2",
          bp.missing_digits(slot_templates) == ["2"])
    check("header refuses outright on an incomplete store",
          bp.read_header(luma, slot_templates) == (None, None))
    check("most starting slots are unjudgeable while a digit is missing",
          len(bp.unevaluable_starts(9, slot_templates)) >= 20,
          "%d of %d unjudgeable"
          % (len(bp.unevaluable_starts(9, slot_templates)), bp.MAX_SLOT))

    # Learn the 2 from the header's own, purely to reach the path that is
    # otherwise unreachable. This is NOT a claim that a header 2 is a
    # valid slot 2 -- that needs a frame with slot 02, 12 or 20 on it.
    hx, hy, hw, hh = bp.HEADER_BOX
    band = luma[hy:hy + hh, hx:hx + hw]
    white = band >= bp.HEADER_WHITE * float(np.percentile(band, 99.9))
    complete = {k: list(v) for k, v in slot_templates.items()}
    complete["2"] = [bp._bitmap(white[:, 241:267], min_ink=20)]
    check("header reads once every digit is known",
          bp.read_header(luma, complete) == (32, 10),
          "got %s" % (bp.read_header(luma, complete),))
    check("nothing is unjudgeable once the store is complete",
          bp.unevaluable_starts(9, complete) == [])

    # NOT tested here, for want of a second frame: the tighter score and
    # margin read_header uses. They were chosen because the missing 2
    # matched a 7 at 0.746/+0.041 -- over the ordinary guards -- while
    # every true digit in this header scored 0.893 or better. With the
    # completeness refusal in front of it that path is now unreachable, so
    # there is nothing honest to assert about it from one frame.

    print("\nlearning slot digits from a page the operator labelled")
    # The operator scrolls, says where the page starts, and that labels
    # every card on it -- which is the only way to teach a digit that
    # cannot yet be read. It also trusts a typed number, so the guard
    # against a mistyped one is the thing worth testing.
    # NOTE the shape: learn_slot_digits takes the WHOLE store, both
    # drawers -- {"slot": {...}, "kill": {...}} -- not a bare digit map.
    # Handed a bare one it finds no rivals to contradict anything and
    # learns the lot, which is how this test first "passed" while proving
    # nothing.
    def store_of(digits):
        return {"slot": {k: [b.copy() for b in v] for k, v in digits.items()},
                "kill": {}}

    before = {k: len(v) for k, v in slot_templates.items()}

    fresh = store_of(slot_templates)
    _, refused = bp.learn_slot_digits(cards, FIRST_SLOT, fresh)
    check("a correctly labelled page contradicts nothing", refused == [],
          "%d refused" % len(refused))

    poisoned = store_of(slot_templates)
    got2, refused2 = bp.learn_slot_digits(cards, 12, poisoned)
    check("a mistyped starting slot teaches nothing at all", got2 == [],
          "learned %s" % sorted(set(got2)))
    check("and every glyph of it is refused, with a reason",
          len(refused2) >= 15 and all(len(r) == 3 for r in refused2),
          "%d refused" % len(refused2))
    check("the store is left exactly as it was",
          {k: len(v) for k, v in poisoned["slot"].items()} == before)

    print("\nreading player names off the alive panel")
    # The results screen has ranks and names but NO slot numbers, and the
    # alive panel is gone by then -- so the names collected here are the
    # only bridge between the two.
    got, want, lit_ok, dim_ok, n_lit, n_dim = [], [], 0, 0, 0, 0
    for i, c in enumerate(cards):
        cx = bp.CARD_X[c["column"]]
        for p in range(bp.PLAYERS_PER_CARD):
            img = bp.ign_image(luma, cx, bp.row_y(c["top"], p))
            got.append(img)
    check("a name image for every player, lit and greyed alike",
          all(g is not None for g in got), "%d of 36" % sum(g is not None for g in got))

    print("\nmatching a results squad to a slot")
    # The pair that would wreck a tag matcher: two different teams whose
    # tags differ by one character.
    lmez = ["LMEzSPIDY7", "LMEzDragon", "LMEzClockko", "LMEzZOLTH"]
    lmex = ["LETMExGLITCH", "LetMexSteve999", "LMExHUNT", "LetMexAudito7"]
    check("a squad matches itself", bp.squad_similarity(lmez, lmez) > 0.99)
    check("LMEz and LMEx stay far apart",
          bp.squad_similarity(lmez, lmex) < 0.55,
          "%.3f against 1.000 for itself" % bp.squad_similarity(lmez, lmex))

    # Order differs between the two screens, and a squad may show three
    # names where the panel had four.
    check("name order does not matter",
          bp.squad_similarity(list(reversed(lmez)), lmez) > 0.99)
    check("three names still match a four-name squad",
          bp.squad_similarity(lmez[:3], lmez) > 0.99)

    known = {3: lmez, 10: lmex, 6: ["TSxWolfOP", "TSxKiboY", "TSxDiablo", "TSxGokGokGok"]}
    screen = {3: lmez, 10: lmex, 6: known[6]}
    matched, unresolved, free = bp.match_squads(screen, known)
    check("each rank takes its own slot",
          all(matched[r]["slot"] == r for r in matched) and len(matched) == 3,
          "matched %d, unresolved %s" % (len(matched), unresolved))

    # A squad that matches nothing must be handed over, not forced onto
    # whichever slot happened to score least badly.
    odd = {9: ["totallyDifferentOne", "andAnotherEntirely", "nothingLikeIt"]}
    m2, un2, free2 = bp.match_squads(odd, known)
    check("an unrecognised squad is left for the dropdown",
          m2 == {} and un2 == [9], "matched %s" % m2)
    check("and the slots it could still be are offered", sorted(free2) == [3, 6, 10])

    # Two squads cannot both claim one slot.
    twice = {1: lmez, 2: lmez}
    m3, un3, _ = bp.match_squads(twice, known)
    check("one slot cannot be taken twice",
          len([v for v in m3.values() if v["slot"] == 3]) <= 1 and len(un3) >= 1)

    print("\nKNOWN GAPS, not failures:")
    print("  * Every count in this fixture is single-digit, so a player on")
    print("    10+ kills is UNTESTED. It currently voids that team's total")
    print("    rather than reporting a wrong one.")
    print("  * Digit '2' never appears as a slot digit here, so slots 2, 12,")
    print("    20-25 have no template until a frame containing one is seen.")
    print("  * VIEWPORT_TOP/BOTTOM are guessed from an unscrolled frame and")
    print("    need confirming against a genuinely mid-scroll capture.")

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
