"""BGMI observer team panel -- turning one screenful into rows.

WHY THIS EXISTS AT ALL, and why it is not shaped like freefire_engine.py:

Free Fire hands us the PC client's own debugger log, so that engine reads
a narration of the match and barely needs the screen. BGMI gives us
nothing of the kind -- it is a retail Android build inside BlueStacks,
with no log, no telemetry and no API. Every number here comes off the
pixels, so this module is built around one idea: a reading is published
only when the game's own numbers agree that it is right.

WHAT THE PANEL GIVES US (measured, not assumed, from a 1920x1080
BlueStacks framebuffer -- see fixtures/panel_9cards_1919x1079.png):

  * Team cards in a 3x3 grid. The three columns sit at a fixed x, the
    four player rows inside a card are 58.7px apart, and cards repeat
    every 285.5px down the page.
  * Per player: an IGN, an elimination count, and alive-or-dead carried
    by whether the text is lit or greyed.
  * Per card: the SLOT NUMBER, which is the join key -- the sheet is
    ordered by slot, so a row that knows its slot needs no name matching
    and none of the identity guesswork Free Fire needed.
  * A header, "Remaining N  Team N", computed by the game independently
    of the cards. That is the checksum this whole module leans on.

THE PANEL SCROLLS, AND IT CAN STOP HALFWAY THROUGH A ROW. So no card
sits at a fixed y and nothing may be read from a hardcoded position.
Every frame starts by finding where the grid currently is -- see
detect_card_tops() -- and cards clipped by the viewport edge are dropped
rather than read half-height.

That also means the operator's scroll position cannot tell us which
teams are on screen, so the slot numbers have to be read. They are read
as a GROUP: any nine cards on screen are nine consecutive slots, so the
whole page is fitted to one starting slot rather than nine digits being
trusted one at a time. See fit_slot_run().

THE PANEL IS NOT THE LOBBY. Nine cards is one page of a 16-20 team
lobby, so a full picture needs more than one source. Sources are merged
by slot number, which makes overlapping pages harmless: two pages that
can both see slot 7 must agree about it, and disagreement is a fault
signal rather than a silent last-writer-wins.
"""

import numpy as np
from PIL import Image

# ---------------------------------------------------------------- geometry
#
# EVERYTHING BELOW IS A REFERENCE, measured once on a 1920x1080 frame,
# and every frame is read at its own SCALE relative to it.
#
# That indirection is not decoration. The numbers were first measured on
# a capture of the whole BlueStacks window, where the game picture sits
# inset behind chrome. A capture of the game picture ALONE, scaled up to
# 1920x1080, renders the same layout about 8% larger -- row pitch 63.5
# against 58.7, card pitch 309.5 against 285.5. Hardcoded offsets then
# land between the rows, and the reader reported squads wiped that were
# plainly alive, because it was measuring the gaps.
#
# So the scale is measured per frame from the row rhythm (detect_scroll
# searches pitch as well as phase), and the x origin is set once at
# calibration, since it only moves when the capture region does.
CARD_X = (93, 630, 1167)
COLUMN_PITCH = 537.0          # between card columns, at scale 1

# The vertical rhythm. Both are deliberately fractional: the rows are not
# on whole pixels, and rounding PITCH to 59 walks half a row down by the
# fourth player.
CARD_PITCH = 285.5      # one card row to the next
ROW_PITCH = 58.7        # one player row to the next, inside a card
ROW0 = 22               # first player row, from the card top
ROW_H = 26

CARD_H = int(ROW0 + 3 * ROW_PITCH + ROW_H)    # top of card to end of row 4

# How this capture compares to the reference, and where its first card
# column begins. Set from the config at startup; the scale is also
# re-measured per frame by detect_scroll, which searches pitch as well
# as phase.
GEOM = {"scale": 1.0, "originX": CARD_X[0], "columns": None}


def set_geometry(scale=None, origin_x=None, columns=None):
    if scale:
        GEOM["scale"] = float(scale)
    if origin_x is not None:
        GEOM["originX"] = float(origin_x)
    if columns is not None:
        GEOM["columns"] = [int(c) for c in columns] if columns else None


def card_columns():
    """The three card x positions for THIS capture.

    Measured per column when calibration has found them, rather than
    stepped off one origin at a uniform pitch. The pitch is not quite
    uniform -- or the scale is not quite exact, which comes to the same
    thing -- and six pixels of drift by the third column put its kill box
    half on the digit and half on the next glyph. The first two columns
    read perfectly while the third returned 12 kills for a squad with
    none, which is the kind of wrong that reaches a sheet unnoticed.
    """
    if GEOM["columns"]:
        return list(GEOM["columns"])
    sc = GEOM["scale"]
    return [int(round(GEOM["originX"] + i * COLUMN_PITCH * sc)) for i in range(3)]


def sx(v):
    """A reference-space x, width or height, in this capture's pixels."""
    return int(round(v * GEOM["scale"]))

# The scrolling viewport, inside the panel. Cards are clipped here, so a
# card is only read when it fits entirely between these two lines.
# Widened from 155/1000, which was measured on a capture that still had
# BlueStacks chrome eating the edges. On a capture of the game picture
# alone the panel reaches further, and the old bottom clipped the third
# card row off a frame where it was plainly, fully visible.
VIEWPORT_TOP, VIEWPORT_BOTTOM = 140, 1070

IGN_DX, IGN_W = 78, 250        # the name
# The digits after the "/" glyph, and nothing else on the row. Measured
# in reference units: the "/" ends at 382, the digit runs 385-395, and
# the word "Eliminations" begins at 405. The box was 378-410, which took
# the leading E of "Eliminations" with it and left every kill unread --
# on the reference frame that E sat further right and the box got away
# with it. 383-404 holds one digit with room, or two, and neither
# neighbour.
ELIM_DX, ELIM_W = 383, 21

# The slot number, and the boundary between its two digits. Measured: ink
# spans card-relative x 23-70 with the gap at 46 on every card.
SLOT_DY, SLOT_H = 4, 52
SLOT_CELLS = ((20, 46), (46, 74))

HEADER_BOX = (90, 88, 560, 64)   # "Remaining N  Team N" -- does NOT scroll

PLAYERS_PER_CARD = 4
MAX_SLOT = 25

# Alive and dead are not close. On the reference frame the lit rows sit at
# a 97th-percentile luminance of ~243 and the greyed ones at ~87, with
# nothing whatsoever in between. 160 is the middle of a 133-point gap, so
# it is a threshold in name only -- there is no tuning risk here.
ALIVE_LUMA = 160.0

# Below this, there is no card in that grid place at all.
#
# A wiped squad still has names on it, drawn dim -- measured 87 to 94. An
# empty place in the last row has nothing, and measures 36 to 44. The
# slot number was tried as the test first and is not reliable: the panel
# is semi-transparent, so the game showing through an empty place can
# offer up enough coloured ink to look like one.
EMPTY_LUMA = 60.0

# A knocked player is NOT distinguished from a dead one. That is a
# deliberate omission, not an oversight: the operator asked for alive and
# dead only. It has one consequence worth remembering -- an alive count
# can go UP when a knocked player is revived, so alive is never
# constrained to fall the way kills are constrained to rise. See
# accept_kills().


# ------------------------------------------------------------------ pixels

def to_luma(rgb):
    return rgb.astype(np.float32).mean(axis=2)


def _relative_mask(crop, level=0.55, lo=20, hi=98, floor_gap=12):
    """Ink, judged against the crop's OWN floor and peak.

    An absolute threshold cannot work anywhere in this module: a greyed
    row's ink is darker than a lit row's background, so any fixed cut
    either loses every dead player's numbers or floods on every live one.
    Kills scored by a player who is now dead still count for his team, so
    losing them is not an option.
    """
    floor, peak = np.percentile(crop, lo), np.percentile(crop, hi)
    if peak - floor < floor_gap:
        return None
    return (crop - floor) / (peak - floor) > level


def _bitmap(mask, shape=(22, 14), min_ink=8):
    """Trim ink to its own bounding box, then normalise.

    Trimming is what makes a dim glyph and a bright one compare equal --
    the two masks differ slightly in how much they bleed at the edges, and
    without the trim that difference moves the glyph inside its box. It
    took same-digit agreement from 0.74 to 0.94.
    """
    ys, xs = np.where(mask)
    if len(ys) < min_ink:
        return None
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    out = Image.fromarray((m * 255).astype(np.uint8)).resize(
        (shape[1], shape[0]), Image.LANCZOS)
    return np.asarray(out).astype(np.float32) / 255.0


def similarity(a, b):
    return 1.0 - float(np.abs(a - b).mean())


def best_match(bitmap, templates, min_score=0.70, min_margin=0.04):
    """Nearest template, but only when it is clearly nearest.

    Both guards matter. A low score means the crop is not a digit at all
    (a transition frame, a popup over the panel). A thin margin means two
    digits are tied, and a tie resolved by a hundredth of a point is a
    coin flip -- better to return nothing and let the caller keep the
    value it already trusts.
    """
    scored = sorted(((similarity(bitmap, ref), name)
                     for name, refs in templates.items()
                     for ref in refs), reverse=True)
    if not scored:
        return None
    top_score, top_name = scored[0]
    rival = next((s for s, n in scored if n != top_name), 0.0)
    if top_score < min_score or (top_score - rival) < min_margin:
        return None
    return top_name


# ------------------------------------------------------- where is the grid

def row_ink_profile(luma):
    """Ink per screen row, counted only in the IGN columns.

    That is where the rhythm lives: four player rows 58.7px apart, then a
    gap, repeating every card. Counting the whole width would drown it in
    the headers and badges that sit between cards.
    """
    w = sx(IGN_W)
    band = np.concatenate([luma[:, x + sx(IGN_DX):x + sx(IGN_DX) + w]
                           for x in card_columns()], axis=1)
    mask = _relative_mask(band, level=0.55, lo=20, hi=99, floor_gap=1)
    if mask is None:
        return np.zeros(luma.shape[0], dtype=np.float32)
    return mask.sum(axis=1).astype(np.float32)


def _comb(offset, n):
    """Where the player rows would be if the grid started at `offset`."""
    c = np.zeros(n, dtype=np.float32)
    k = 0
    while True:
        base = offset + k * CARD_PITCH
        if base > n:
            break
        for p in range(PLAYERS_PER_CARD):
            y = int(base + p * ROW_PITCH)
            if 0 <= y < n - ROW_H:
                c[y:y + ROW_H] = 1.0
        k += 1
    return c


SCALE_RANGE = (0.88, 1.16)      # the capture sizes seen in practice


def _comb_score(prof, scale, off, n):
    """How well a grid at this scale and phase lands on the ink.

    The sampling window SCALES with the candidate. Fixed at the reference
    26px it quietly favoured smaller scales -- a short window slid between
    two tightly spaced rows catches a higher average than a correct one
    spanning a real row and its margins -- and the search settled three
    pixels and a percent off, which is nothing for the name strip and
    everything for a twelve-pixel digit.
    """
    rp, cp = ROW_PITCH * scale, CARD_PITCH * scale
    win = max(8, int(round(ROW_H * scale)))
    hits, count, k = 0.0, 0, 0
    while True:
        base = off + k * cp
        if base > n:
            break
        for p in range(PLAYERS_PER_CARD):
            y = int(base + p * rp)
            if 0 <= y < n - win:
                hits += float(prof[y:y + win].mean())
                count += 1
        k += 1
    return (hits / count) if count >= 8 else -1.0


def detect_scroll(luma, step=0.5):
    """Find the grid's current vertical phase, in [0, card pitch).

    Returns (offset, score). The offset is where the FIRST player row of
    some card sits; which card that is, is answered by the slot numbers.

    ONLY THE PHASE IS SEARCHED HERE. The scale is a property of the
    capture region, not of the frame, so it is measured once by
    find_geometry and held in GEOM. Searching both together was tried and
    was worse at the thing that matters: the extra freedom let a slightly
    wrong scale score higher than the right one, and the phase came back
    three pixels out -- harmless for the wide name strip, ruinous for a
    twelve-pixel digit.
    """
    prof = row_ink_profile(luma)
    n = len(prof)
    scale = GEOM["scale"]
    cp = CARD_PITCH * scale
    best = (-1.0, 0.0)
    for off in np.arange(0, cp, step):
        sc = _comb_score(prof, scale, off, n)
        if sc > best[0]:
            best = (sc, float(off))
    return best[1], best[0]


def find_geometry(rgb):
    """Measure this capture's scale and x origin. Run at calibration.

    Both are properties of the capture RECTANGLE, not of any one frame --
    a region holding the whole BlueStacks window renders the panel about
    8% smaller than one holding the game picture alone. They are measured
    together because the origin search needs the scale to place its
    boxes.
    """
    luma = to_luma(rgb)
    keep = (GEOM["scale"], GEOM["originX"])
    GEOM["columns"] = None
    best = (-1.0, keep[0])
    for scale in np.arange(SCALE_RANGE[0], SCALE_RANGE[1], 0.005):
        GEOM["scale"] = scale
        prof = row_ink_profile(luma)
        cp = CARD_PITCH * scale
        top = max((_comb_score(prof, scale, off, len(prof))
                   for off in np.arange(0, cp, 1.0)), default=-1.0)
        if top > best[0]:
            best = (top, float(scale))
    # The rows give the scale to about a percent. That is not enough:
    # the columns are 537 apart at reference, so a 1.2% error is 7px per
    # column and 14px by the third -- which pulled the leading "El" of
    # "Eliminations" into the third column's kill box while the first
    # column read cleanly. The rows cannot see that; the columns can.
    #
    # So scale and origin are refined TOGETHER against the coloured slot
    # numbers, which are sharp, in all three columns, and therefore
    # sensitive to the pitch as well as the offset.
    coarse = best[1]
    a = rgb.astype(np.float32)
    lum = a.mean(axis=2)
    mx, mn = a.max(axis=2), a.min(axis=2)
    coloured = (((mx - mn) / np.maximum(mx, 1e-3)) > 0.33) & (lum > 40)

    best_fit = (-1, coarse, GEOM["originX"])
    for scale in np.arange(coarse - 0.03, coarse + 0.03, 0.003):
        GEOM["scale"] = float(scale)
        tops, _, _ = detect_card_tops(lum)
        if not tops:
            continue
        pitch = COLUMN_PITCH * scale
        y0, y1 = sx(SLOT_DY), sx(SLOT_DY + SLOT_H)
        bx0, bx1 = sx(SLOT_CELLS[0][0]), sx(SLOT_CELLS[1][1])
        for x0 in range(0, 240, 2):
            total = 0
            for top in tops:
                for i in range(3):
                    cx = int(round(x0 + i * pitch))
                    total += int(coloured[top + y0:top + y1, cx + bx0:cx + bx1].sum())
            if total > best_fit[0]:
                best_fit = (total, float(scale), float(x0))

    found = {"scale": round(best_fit[1], 4), "originX": int(best_fit[2])}
    GEOM["scale"], GEOM["originX"], GEOM["columns"] = keep[0], keep[1], None
    return found


def detect_card_tops(luma, viewport=(VIEWPORT_TOP, VIEWPORT_BOTTOM)):
    """The y of every card row FULLY inside the viewport.

    A clipped card is skipped, not read short. Half a card is four player
    rows minus some, and the ones missing are missing silently -- a squad
    would simply appear to have fewer players, which is indistinguishable
    from them being dead.
    """
    top, bottom = viewport
    offset, score = detect_scroll(luma)
    tops = []
    k = -2
    while True:
        card_top = offset - sx(ROW0) + k * CARD_PITCH * GEOM["scale"]
        k += 1
        if card_top > bottom:
            break
        if card_top < top - 0.5:
            continue
        if card_top + sx(CARD_H) <= bottom + 0.5:
            tops.append(int(round(card_top)))
    return tops, offset, score


# ------------------------------------------------------------------ reading

def row_y(card_top, player):
    return int(card_top + sx(ROW0) + player * ROW_PITCH * GEOM["scale"])


def row_is_alive(luma, card_x, y):
    """Lit name or greyed name. Read off the IGN strip rather than the
    whole row: the "Eliminations" label beside it is dim even for a living
    player, which drags a whole-row average toward the middle."""
    strip = luma[y:y + sx(ROW_H), card_x + sx(IGN_DX):card_x + sx(IGN_DX) + sx(IGN_W)]
    if strip.size == 0:
        return None
    return float(np.percentile(strip, 97)) > ALIVE_LUMA


def ign_image(luma, card_x, y, scale=4):
    """The player's name, prepared for an OCR pass.

    Thresholded against the crop's own floor and peak, like every other
    reading here, and for the sharpest version of the same reason: a dead
    player's name is drawn dimmer than a living player's BACKGROUND. An
    absolute cut returned nothing at all for the fifteen dead rows on the
    reference frame -- and those are exactly the names still needed, since
    a squad wiped at minute three must still be identifiable on the
    results screen twenty minutes later.

    Returned black-on-white and upscaled, which is what Tesseract wants.
    """
    crop = luma[y - 3:y + sx(ROW_H) + 3,
                card_x + sx(IGN_DX):card_x + sx(IGN_DX) + sx(IGN_W)]
    if crop.size == 0:
        return None
    floor, peak = np.percentile(crop, 15), np.percentile(crop, 99)
    if peak - floor < 10:
        return None
    mask = (crop - floor) / (peak - floor) > 0.45
    if mask.sum() < 12:
        return None
    img = Image.fromarray((255 - mask * 255).astype(np.uint8))
    return img.resize((img.width * scale, img.height * scale), Image.LANCZOS)


ELIM_SCAN = (352, 450)        # the stretch holding "/", the digits, "Elim..."


def kill_bitmap(luma, card_x, y):
    """The kill count, found by the "/" that always precedes it.

    Anchored on the slash rather than on an absolute x. Six pixels of
    drift by the third column -- from the column pitch not being quite
    even, or the scale not quite exact -- put a fixed box half on the
    digit and half on the next glyph, and the third column returned
    twelve kills for a squad with none while the first two read
    perfectly. The slash is on every row, immediately left of the number,
    so measuring from it costs nothing and cannot drift.

    Groups of ink across the strip run: slash, the digits, then the word
    "Eliminations". The second group is the number, however wide.
    """
    x0 = card_x + sx(ELIM_SCAN[0])
    x1 = card_x + sx(ELIM_SCAN[1])
    crop = luma[y - 3:y + sx(ROW_H) + 3, x0:x1]
    if crop.size == 0:
        return None
    mask = _relative_mask(crop)
    if mask is None:
        return None

    cols = mask.any(axis=0)
    groups, start = [], None
    for i, on in enumerate(cols):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= 2:
                groups.append((start, i))
            start = None
    if start is not None and len(cols) - start >= 2:
        groups.append((start, len(cols)))
    if len(groups) < 2:
        return None

    # groups[0] is the slash; the number is what comes next, and a
    # two-digit number is two groups a hair apart, so neighbours within a
    # couple of pixels are taken together.
    a, b = groups[1]
    for nxt_a, nxt_b in groups[2:]:
        if nxt_a - b <= max(2, sx(3)):
            b = nxt_b
        else:
            break
    return _bitmap(mask[:, a:b])


def slot_bitmaps(luma, card_x, card_top):
    """The slot number's two digits, each trimmed inside its own cell."""
    out = []
    for a, b in SLOT_CELLS:
        crop = luma[card_top + sx(SLOT_DY):card_top + sx(SLOT_DY) + sx(SLOT_H),
                    card_x + sx(a):card_x + sx(b)]
        if crop.size == 0:
            out.append(None)
            continue
        mask = _relative_mask(crop, level=0.5, lo=15, hi=99, floor_gap=10)
        out.append(None if mask is None else _bitmap(mask, min_ink=20))
    return out


def find_origin_x(rgb, lo=0, hi=240, step=2):
    """Where the first card column starts, found by trying.

    Scored on COLOURED ink inside the slot-number box. The slot number is
    the only coloured thing on a card, so that box is either full of it or
    empty -- a sharp target. Scoring on ink generally is not: the IGN box
    is wide enough to catch real text while sixty pixels out of place, so
    that objective happily picked an origin where the slot and kill boxes
    sat on blank card.

    The rows are found once, up front; the candidates only move the boxes
    sideways. Searching the scale per candidate as well took minutes.

    Run at calibration -- the origin only moves when the capture region
    does.
    """
    a = rgb.astype(np.float32)
    lum = a.mean(axis=2)
    mx, mn = a.max(axis=2), a.min(axis=2)
    coloured = (((mx - mn) / np.maximum(mx, 1e-3)) > 0.33) & (lum > 40)

    tops, _, _ = detect_card_tops(lum)          # also settles the scale
    if not tops:
        return GEOM["originX"], 0
    pitch = COLUMN_PITCH * GEOM["scale"]
    y0, y1 = sx(SLOT_DY), sx(SLOT_DY + SLOT_H)
    bx0, bx1 = sx(SLOT_CELLS[0][0]), sx(SLOT_CELLS[1][1])

    best = (-1, GEOM["originX"])
    for x0 in range(lo, hi, step):
        total = 0
        for top in tops:
            for i in range(3):
                cx = int(round(x0 + i * pitch))
                total += int(coloured[top + y0:top + y1, cx + bx0:cx + bx1].sum())
        if total > best[0]:
            best = (total, x0)
    return best[1], best[0]


def read_page(rgb, digit_templates=None, viewport=(VIEWPORT_TOP, VIEWPORT_BOTTOM)):
    """One screenful: every whole card currently visible.

    Slot numbers are left as bitmaps for fit_slot_run() -- read the note
    there for why they are not decided one card at a time.
    """
    luma = to_luma(rgb)
    tops, offset, score = detect_card_tops(luma, viewport)
    cards = []
    for row_i, card_top in enumerate(tops):
        for col_i, cx in enumerate(card_columns()):
            players = []
            for p in range(PLAYERS_PER_CARD):
                y = row_y(card_top, p)
                kills = None
                if digit_templates:
                    bm = kill_bitmap(luma, cx, y)
                    if bm is not None:
                        hit = best_match(bm, digit_templates)
                        if hit is not None:
                            kills = int(hit)
                players.append({"alive": row_is_alive(luma, cx, y), "kills": kills})
            slots = slot_bitmaps(luma, cx, card_top)
            # Is there a card here at all? Judged on the names, which a
            # wiped squad still has and an empty place does not.
            lit = max((float(np.percentile(
                luma[row_y(card_top, p):row_y(card_top, p) + sx(ROW_H),
                     cx + sx(IGN_DX):cx + sx(IGN_DX) + sx(IGN_W)], 97))
                for p in range(PLAYERS_PER_CARD)
                if luma[row_y(card_top, p):row_y(card_top, p) + sx(ROW_H),
                        cx + sx(IGN_DX):cx + sx(IGN_DX) + sx(IGN_W)].size),
                default=0.0)
            cards.append({
                "index": row_i * 3 + col_i,
                "top": card_top,
                "column": col_i,
                "players": players,
                "slot_bitmaps": slots,
                # A grid position with no card on it at all. The last row
                # of a 16-team lobby has one card and two empty places,
                # and an empty place reads as four dead players -- which
                # is how slots 19 and 20 appeared, wiped, in a lobby that
                # stops at 18.
                "present": lit > EMPTY_LUMA,
                "lit": round(lit, 1),
            })
    return {"cards": cards, "scroll": offset, "scroll_score": score,
            "card_rows": len(tops)}


# -------------------------------------------------------------- slot numbers

def fit_slot_run(cards, digit_templates, min_margin=0.015):
    """Decide the whole page's slots at once.

    The cards on screen are always CONSECUTIVE slots, so there are only
    about twenty possible answers for a page, not ten per digit per card.
    Every candidate start is scored against every digit we can see and the
    best one wins -- so a single smudged digit is outvoted by the
    seventeen around it instead of silently relabelling one team.

    That direction matters more than the accuracy does. A misread digit
    must never move one team's kills onto another team's row in the sheet,
    which is the worst thing this module could do, and fitting the page as
    a run means a bad digit degrades the CONFIDENCE of the whole page
    rather than corrupting one row of it.

    Returns (first_slot, margin) or (None, 0.0) when nothing fits clearly.
    """
    if not digit_templates:
        return None, 0.0
    scores = {}
    for start in range(1, MAX_SLOT + 1):
        total, seen = 0.0, 0
        for i, c in enumerate(cards):
            label = "%02d" % (start + i)
            for cell, bm in enumerate(c.get("slot_bitmaps") or []):
                if bm is None:
                    continue
                refs = digit_templates.get(label[cell])
                if not refs:
                    continue
                total += max(similarity(bm, r) for r in refs)
                seen += 1
        if seen:
            scores[start] = total / seen
    if not scores:
        return None, 0.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) == 1:
        return ranked[0][0], 1.0
    margin = ranked[0][1] - ranked[1][1]
    if margin < min_margin:
        return None, margin
    return ranked[0][0], margin


def assign_slots(cards, first_slot):
    for i, c in enumerate(cards):
        c["slot"] = first_slot + i
    return cards


# -------------------------------------------------------------- invariants

def team_rows(cards):
    """Cards to one row per team: alive out of four, and kills summed.

    Kills come back None for the whole team if ANY of its four players
    could not be read. A team's score is the sum of four numbers, and a
    partial sum is not a smaller truth -- it is a wrong number, and it
    would reach the sheet looking exactly as confident as a right one.
    """
    rows = []
    for c in cards:
        alive = sum(1 for p in c["players"] if p["alive"])
        ks = [p["kills"] for p in c["players"]]
        rows.append({
            "slot": c.get("slot"),
            "alive": alive,
            "eliminated": alive == 0,
            "kills": None if any(k is None for k in ks) else sum(ks),
        })
    return rows


def merge_pages(pages):
    """Several capture sources into one table, keyed by slot.

    Where two pages can see the same slot they must agree. A conflict is
    reported rather than resolved: the two sources are the same game, so
    disagreement means one of them was mid-repaint, mid-scroll or
    occluded, and picking a winner would just hide that.
    """
    merged, conflicts = {}, []
    for rows in pages:
        for r in rows:
            slot = r.get("slot")
            if slot is None:
                continue
            prev = merged.get(slot)
            if prev is None:
                merged[slot] = r
            elif (prev["alive"], prev["kills"]) != (r["alive"], r["kills"]):
                conflicts.append((slot, prev, r))
    return [merged[s] for s in sorted(merged)], conflicts


def agrees_with_header(rows, remaining, teams_alive, complete):
    """The game's own totals against ours.

    This is the whole reason the module can claim accuracy. "Remaining"
    and "Team" are computed by BGMI from its own state, so when our sum
    matches them the table is right for reasons that have nothing to do
    with how well the OCR did.

    It can only be applied when the sources cover the WHOLE lobby --
    `complete`. On a partial view our totals are legitimately lower than
    the header's, and asserting otherwise would reject every good frame.
    """
    if not complete or remaining is None or teams_alive is None:
        return None
    return (sum(r["alive"] for r in rows) == remaining
            and sum(1 for r in rows if r["alive"] > 0) == teams_alive)


def name_similarity(a, b):
    import difflib
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def squad_similarity(screen_names, known_names):
    """How well one results-screen squad matches one slot's squad.

    Scored as SETS, not name against name in order -- the results screen
    lists players in its own order, and a squad may show three names
    where the alive panel had four. Each screen name takes its best
    partner among the known ones and that partner is then spent, so two
    screen names cannot both score against the same player.

    Averaging over the screen's own names, rather than over four, is what
    stops a three-player squad being penalised a quarter of its score for
    having three players.
    """
    screen = [n for n in (screen_names or []) if n]
    pool = [n for n in (known_names or []) if n]
    if not screen or not pool:
        return 0.0
    total, left = 0.0, list(pool)
    for name in screen:
        if not left:
            break
        best = max(range(len(left)), key=lambda i: name_similarity(name, left[i]))
        total += name_similarity(name, left[best])
        left.pop(best)
    return total / len(screen)


def match_squads(screen_squads, known_squads, min_score=0.55, min_margin=0.08):
    """Assign each results-screen squad to a slot, or to nobody.

    screen_squads: {rank: [names]}   known_squads: {slot: [names]}

    Confident pairs first, and each one taken removes both sides from
    everything that follows -- which is the whole advantage of treating
    this as an assignment rather than as N separate lookups. A rank that
    is ambiguous on its own is often decided by every other rank being
    certain, leaving only one slot it can be.

    Anything still unclear is returned unassigned, for the operator to
    pick from a dropdown. It is NOT guessed at: the names here are two
    OCR passes away from the truth, read off two different screens at two
    different sizes, and a wrong pairing puts one team's placement on
    another team's row. A blank the operator fills is visible; a wrong
    answer that looks confident is not.
    """
    pairs = []
    for rank, names in screen_squads.items():
        for slot, known in known_squads.items():
            pairs.append((squad_similarity(names, known), rank, slot))
    pairs.sort(reverse=True)

    taken_rank, taken_slot, matched = set(), set(), {}
    for score, rank, slot in pairs:
        if rank in taken_rank or slot in taken_slot or score < min_score:
            continue
        # The margin is against the best OTHER slot still free for this
        # rank -- a pairing is only safe if the runner-up is clearly worse.
        rival = max((s for s, r, sl in pairs
                     if r == rank and sl != slot and sl not in taken_slot),
                    default=0.0)
        if score - rival < min_margin:
            continue
        matched[rank] = {"slot": slot, "score": round(score, 3),
                         "margin": round(score - rival, 3)}
        taken_rank.add(rank)
        taken_slot.add(slot)

    unresolved = sorted(r for r in screen_squads if r not in matched)
    free = sorted(s for s in known_squads if s not in taken_slot)
    return matched, unresolved, free


def accept_kills(previous, reading):
    """Kills only ever rise, and a frame may miss a step but never invent
    one. Anything below what we already published is a misread; a jump far
    above it is one too.

    Deliberately NOT applied to alive counts -- a revived player sends
    that number back up, so monotonicity there would freeze a team as dead
    the moment it was knocked.
    """
    if reading is None:
        return previous
    if previous is None:
        return reading
    if reading < previous or reading > previous + 3:
        return previous
    return reading


# ------------------------------------------------------------- template store
#
# Two stores, kept apart on purpose. The slot number is drawn ~50px tall
# and the kill count ~20px, and the same digit at those two sizes does not
# normalise to the same bitmap -- the stroke-to-gap ratio of the stencil
# font changes. Matching a kill digit against a slot template was good for
# about a coin flip.

GLYPH_PATH = __file__.replace("bgmi_panel.py", "bgmi_glyphs.npz")


def load_templates(path=None):
    """Returns {"slot": {...}, "kill": {...}}, empty if nothing is saved."""
    import os
    path = path or GLYPH_PATH
    out = {"slot": {}, "kill": {}}
    if not os.path.exists(path):
        return out
    with np.load(path) as data:
        for key in data.files:
            store, digit, _ = key.split("::")
            out.setdefault(store, {}).setdefault(digit, []).append(data[key])
    return out


def save_templates(stores, path=None):
    arrays = {}
    for store, digits in stores.items():
        for digit, refs in digits.items():
            for i, ref in enumerate(refs):
                arrays["%s::%s::%d" % (store, digit, i)] = ref
    np.savez_compressed(path or GLYPH_PATH, **arrays)


MAX_VARIANTS = 4


def remember(stores, store, digit, bitmap, near=0.97):
    """Keep a new look at a digit, unless we already have one like it.

    Capped because variants are compared linearly on every frame, and
    because an unbounded store slowly fills with near-duplicates of
    whatever digit happens to be on screen most -- which for kills is
    zero, by a wide margin.
    """
    refs = stores.setdefault(store, {}).setdefault(digit, [])
    if any(similarity(bitmap, r) >= near for r in refs):
        return False
    if len(refs) >= MAX_VARIANTS:
        return False
    refs.append(bitmap)
    return True


def learn_slot_digits(cards, first_slot, stores, contradiction=0.90):
    """Learn slot digits from a page the OPERATOR has labelled.

    The operator scrolls the panel and says where the page starts, so the
    slot of every card on it follows. That is a better teacher than any
    fit we could run -- it needs no templates to begin with, which is the
    only way out of the standing deadlock where digit 2 cannot be read
    because digit 2 has never been seen.

    It also trusts a typed number, so there is one guard. A bitmap that
    already looks like a DIFFERENT digit is not learned: mistype the
    starting slot and every digit on screen would otherwise be filed
    under the wrong name, which does not merely fail to help -- it
    poisons a store that was working. Refusing those and reporting them
    turns a typo into a visible disagreement instead.
    """
    learned, refused = [], []
    for i, c in enumerate(cards):
        label = "%02d" % (first_slot + i)
        for cell, bm in enumerate(c.get("slot_bitmaps") or []):
            if bm is None:
                continue
            want = label[cell]
            rival, rival_score = None, 0.0
            for digit, refs in (stores.get("slot") or {}).items():
                if digit == want:
                    continue
                for ref in refs:
                    s = similarity(bm, ref)
                    if s > rival_score:
                        rival, rival_score = digit, s
            if rival is not None and rival_score >= contradiction:
                refused.append((want, rival, round(rival_score, 3)))
                continue
            if remember(stores, "slot", want, bm):
                learned.append(want)
    return learned, refused


# ------------------------------------------------------------------- header
#
# "Remaining 32  Team 10" -- the game's own totals, and the only thing on
# screen computed independently of the cards. See agrees_with_header().

HEADER_WHITE = 0.85     # fraction of the band's peak that counts as a number
HEADER_DIGIT_GAP = 40   # a wider gap than this starts a new number


def missing_digits(templates):
    """Which of 0-9 this store has never seen."""
    return [d for d in "0123456789" if not templates.get(d)]


def unevaluable_starts(card_count, templates, max_slot=MAX_SLOT):
    """Candidate starting slots that cannot be judged at all, because some
    digit in them has no template.

    fit_slot_run SKIPS those candidates, which is not the same as ruling
    them out. If the page really is at slot 12 and there is no "2", the
    fit quietly settles on the best candidate it can evaluate and is
    confidently wrong -- every team on screen relabelled. Anything that
    publishes must check this comes back empty.
    """
    gone = set(missing_digits(templates))
    if not gone:
        return []
    out = []
    for start in range(1, max_slot + 1):
        labels = "".join("%02d" % (start + i) for i in range(card_count))
        if gone & set(labels):
            out.append(start)
    return out


def read_header(luma, templates, box=HEADER_BOX,
                min_score=0.80, min_margin=0.06):
    """The two numbers in the header, or (None, None).

    The labels are grey and the numbers are white -- measured at 163-171
    against a clipped 255, an 84-point gap. That is the whole trick: it
    picks the numbers out without knowing where they sit, so a count
    dropping from two digits to one (and shoving "Team" leftward) changes
    nothing. Matching letters as digits was the alternative, and "g" came
    back as a confident "0".

    REFUSES OUTRIGHT on an incomplete digit store. A header number can
    contain any digit, so a store missing one does not read that number
    badly -- it reads it as a DIFFERENT NUMBER. With no "2" learned, the
    reference frame's "Remaining 32" came back as a perfectly confident
    37. A checksum that is quietly wrong is worse than no checksum, since
    this is the thing that decides whether everything else is trusted.

    Thresholds are tighter here than elsewhere for the same reason: that
    false 7 scored 0.746 with a 0.041 margin, while every true digit in
    this header scored 0.893 or better.
    """
    if missing_digits(templates):
        return None, None
    x, y, w, h = box
    band = luma[y:y + h, x:x + w]
    if band.size == 0:
        return None, None
    mask = band >= HEADER_WHITE * float(np.percentile(band, 99.9))
    cols = mask.any(axis=0)

    groups, start = [], None
    for i, on in enumerate(cols):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= 3:
                groups.append((start, i))
            start = None
    if start is not None and len(cols) - start >= 3:
        groups.append((start, len(cols)))

    numbers, current = [], []
    for g in groups:
        if current and g[0] - current[-1][1] > HEADER_DIGIT_GAP:
            numbers.append(current)
            current = []
        current.append(g)
    if current:
        numbers.append(current)

    out = []
    for digits in numbers[:2]:
        text = ""
        for a, b in digits:
            sub = mask[:, a:b]
            bm = _bitmap(sub, min_ink=20)
            hit = (best_match(bm, templates, min_score, min_margin)
                   if bm is not None else None)
            if hit is None:
                text = ""
                break
            text += hit
        out.append(int(text) if text else None)
    while len(out) < 2:
        out.append(None)
    return out[0], out[1]
