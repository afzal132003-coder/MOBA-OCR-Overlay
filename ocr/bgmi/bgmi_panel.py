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

  * Nine team cards in a 3x3 grid, rigidly spaced. Card rows are 285.5px
    apart, the three columns sit at a fixed x, and the four player rows
    inside a card are 58.7px apart. Calibrate once, not per event.
  * Per player: an IGN, an elimination count, and alive-or-dead carried
    by whether the text is lit or greyed.
  * Per card: the SLOT NUMBER, which is the join key -- the sheet is
    ordered by slot, so a row that knows its slot needs no name matching
    and none of the identity guesswork Free Fire needed.
  * A header, "Remaining N  Team N", computed by the game independently
    of the cards. That is the checksum this whole module leans on.

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
# Card origins within the Android framebuffer -- the top-left of each
# card. CARD_X is the three columns, CARD_Y the three rows.
CARD_X = (93, 630, 1167)
CARD_Y = (163, 448, 734)

# Inside a card. ROW0 is the first player row's offset from the card top;
# PITCH is deliberately fractional -- the rows are not on whole pixels and
# rounding it to 59 walks half a row down by the fourth player.
ROW0, PITCH, ROW_H = 22, 58.7, 26

IGN_DX, IGN_W = 78, 250        # the name
ELIM_DX, ELIM_W = 378, 32      # the digits after the "/" glyph
SLOT_DX, SLOT_DY, SLOT_W, SLOT_H = 20, 4, 54, 52

HEADER_BOX = (90, 88, 560, 64)   # "Remaining N  Team N"

PLAYERS_PER_CARD = 4
CARDS_PER_PAGE = 9

# Alive and dead are not close. On the reference frame the lit rows sit at
# a 97th-percentile luminance of ~243 and the greyed ones at ~87, with
# nothing whatsoever in between. 160 is the middle of a 156-point gap, so
# it is a threshold in name only -- there is no tuning risk here.
ALIVE_LUMA = 160.0

# A knocked player is NOT distinguished from a dead one. That is a
# deliberate omission, not an oversight: the operator asked for alive and
# dead only. It has one consequence worth remembering -- an alive count
# can go UP when a knocked player is revived, so alive is never
# constrained to fall the way kills are constrained to rise. See
# accept_kills().


def card_origin(index):
    """Card index 0..8, reading order, to its (x, y) in the frame."""
    return CARD_X[index % 3], CARD_Y[index // 3]


def row_y(card_y, player):
    return int(card_y + ROW0 + player * PITCH)


# ------------------------------------------------------------------ pixels

def to_luma(rgb):
    return rgb.astype(np.float32).mean(axis=2)


def to_saturation(rgb):
    """Used only for the slot number, which is the one coloured thing in
    its corner -- the names and counts around it are white or grey."""
    a = rgb.astype(np.float32)
    mx, mn = a.max(axis=2), a.min(axis=2)
    return (mx - mn) / np.maximum(mx, 1e-3)


def row_is_alive(luma, card_x, y):
    """Lit name or greyed name. Read off the IGN strip rather than the
    whole row: the "Eliminations" label beside it is dim even for a living
    player, which drags a whole-row average toward the middle."""
    strip = luma[y:y + ROW_H, card_x + IGN_DX:card_x + IGN_DX + IGN_W]
    if strip.size == 0:
        return None
    return float(np.percentile(strip, 97)) > ALIVE_LUMA


def glyph_bitmap(luma, x, y, w, h, shape=(22, 14)):
    """A digit, normalised so a dead player's dim text and a living
    player's bright text produce the same bitmap.

    The threshold is taken RELATIVE to this crop's own floor and peak.
    An absolute one cannot work: a greyed row's ink is darker than a lit
    row's background, so any fixed cut either loses every dead player's
    kills or floods on every live one. Kills scored by a player who is now
    dead still count for the team, so losing them is not an option.
    """
    crop = luma[y:y + h, x:x + w]
    if crop.size == 0:
        return None
    floor, peak = np.percentile(crop, 20), np.percentile(crop, 98)
    if peak - floor < 12:            # blank box, no ink at all
        return None
    mask = (crop - floor) / (peak - floor) > 0.55
    ys, xs = np.where(mask)
    if len(ys) < 8:
        return None
    mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    out = Image.fromarray((mask * 255).astype(np.uint8)).resize(
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


# ------------------------------------------------------------------ reading

def read_page(rgb, templates=None):
    """One screenful: nine cards, four players each.

    Slot numbers are left to resolve_slots() -- see why there.
    """
    luma = to_luma(rgb)
    cards = []
    for i in range(CARDS_PER_PAGE):
        cx, cy = card_origin(i)
        players = []
        for p in range(PLAYERS_PER_CARD):
            y = row_y(cy, p)
            alive = row_is_alive(luma, cx, y)
            kills = None
            if templates:
                bm = glyph_bitmap(luma, cx + ELIM_DX, y - 3, ELIM_W, ROW_H + 6)
                if bm is not None:
                    hit = best_match(bm, templates)
                    if hit is not None:
                        kills = int(hit)
            players.append({"alive": alive, "kills": kills})
        cards.append({"card": i, "players": players})
    return cards


def resolve_slots(cards, first_slot):
    """Give every card its slot number.

    The panel lists teams in slot order and never reorders them, so the
    nine cards on a page are always nine CONSECUTIVE slots. That turns
    nine shaky OCR reads into one number -- where the page starts -- and
    the operator already knows it, because each capture source is parked
    on a fixed scroll position (source A on slots 1-9, source B on 10-18).

    Reading the numbers off the screen is therefore a CHECK, not the
    mechanism. That ordering is the point: a misread digit cannot silently
    move a team's kills onto another team's row in the sheet, which is the
    single worst thing this module could do.
    """
    for i, c in enumerate(cards):
        c["slot"] = first_slot + i
    return cards


def slots_look_consecutive(read_numbers, first_slot):
    """Does what we read off the screen agree with where we think we are?

    Only the numbers actually read are judged; None means that card's
    number could not be read confidently, which is not evidence either
    way.
    """
    seen = [(i, n) for i, n in enumerate(read_numbers) if n is not None]
    if not seen:
        return None                      # no evidence, not a failure
    return all(n == first_slot + i for i, n in seen)


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
    disagreement means one of them was mid-repaint or occluded, and
    picking a winner would just hide that.
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
