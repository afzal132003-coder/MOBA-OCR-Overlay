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
# The three card columns. Horizontal position is fixed -- the panel only
# ever scrolls vertically.
CARD_X = (93, 630, 1167)

# The vertical rhythm. Both are deliberately fractional: the rows are not
# on whole pixels, and rounding PITCH to 59 walks half a row down by the
# fourth player.
CARD_PITCH = 285.5      # one card row to the next
ROW_PITCH = 58.7        # one player row to the next, inside a card
ROW0 = 22               # first player row, from the card top
ROW_H = 26

CARD_H = int(ROW0 + 3 * ROW_PITCH + ROW_H)    # top of card to end of row 4

# The scrolling viewport, inside the panel. Cards are clipped here, so a
# card is only read when it fits entirely between these two lines.
VIEWPORT_TOP, VIEWPORT_BOTTOM = 155, 1000

IGN_DX, IGN_W = 78, 250        # the name
ELIM_DX, ELIM_W = 378, 32      # the digits after the "/" glyph

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
    band = np.concatenate([luma[:, x + IGN_DX:x + IGN_DX + IGN_W]
                           for x in CARD_X], axis=1)
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


def detect_scroll(luma, step=0.5):
    """Find the grid's current vertical phase, in [0, CARD_PITCH).

    Returns (offset, score). The offset is where the FIRST player row of
    some card sits; which card that is, is a separate question answered by
    the slot numbers.
    """
    prof = row_ink_profile(luma)
    n = len(prof)
    best = (-1.0, 0.0)
    for off in np.arange(0, CARD_PITCH, step):
        c = _comb(off, n)
        total = c.sum()
        if total < 100:
            continue
        score = float((prof * c).sum() / total)
        if score > best[0]:
            best = (score, float(off))
    return best[1], best[0]


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
        card_top = offset - ROW0 + k * CARD_PITCH
        k += 1
        if card_top > bottom:
            break
        if card_top < top - 0.5:
            continue
        if card_top + CARD_H <= bottom + 0.5:
            tops.append(int(round(card_top)))
    return tops, offset, score


# ------------------------------------------------------------------ reading

def row_y(card_top, player):
    return int(card_top + ROW0 + player * ROW_PITCH)


def row_is_alive(luma, card_x, y):
    """Lit name or greyed name. Read off the IGN strip rather than the
    whole row: the "Eliminations" label beside it is dim even for a living
    player, which drags a whole-row average toward the middle."""
    strip = luma[y:y + ROW_H, card_x + IGN_DX:card_x + IGN_DX + IGN_W]
    if strip.size == 0:
        return None
    return float(np.percentile(strip, 97)) > ALIVE_LUMA


def kill_bitmap(luma, card_x, y):
    crop = luma[y - 3:y + ROW_H + 3, card_x + ELIM_DX:card_x + ELIM_DX + ELIM_W]
    if crop.size == 0:
        return None
    mask = _relative_mask(crop)
    return None if mask is None else _bitmap(mask)


def slot_bitmaps(luma, card_x, card_top):
    """The slot number's two digits, each trimmed inside its own cell."""
    out = []
    for a, b in SLOT_CELLS:
        crop = luma[card_top + SLOT_DY:card_top + SLOT_DY + SLOT_H,
                    card_x + a:card_x + b]
        if crop.size == 0:
            out.append(None)
            continue
        mask = _relative_mask(crop, level=0.5, lo=15, hi=99, floor_gap=10)
        out.append(None if mask is None else _bitmap(mask, min_ink=20))
    return out


def read_page(rgb, digit_templates=None, viewport=(VIEWPORT_TOP, VIEWPORT_BOTTOM)):
    """One screenful: every whole card currently visible.

    Slot numbers are left as bitmaps for fit_slot_run() -- read the note
    there for why they are not decided one card at a time.
    """
    luma = to_luma(rgb)
    tops, offset, score = detect_card_tops(luma, viewport)
    cards = []
    for row_i, card_top in enumerate(tops):
        for col_i, cx in enumerate(CARD_X):
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
            cards.append({
                "index": row_i * 3 + col_i,
                "top": card_top,
                "column": col_i,
                "players": players,
                "slot_bitmaps": slot_bitmaps(luma, cx, card_top),
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
