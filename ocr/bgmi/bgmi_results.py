"""BGMI end-of-match results screen -- rank, names and finishes per squad.

THE LAYOUT, measured on the event rig's captures (1920x1080, the frame
grab_region() hands everyone):

  * LEFT, fixed: rank 1 (the trophy) and rank 2, one block each.
  * RIGHT, scrolling: rank 3 onwards, one gold block per squad, the rank
    printed large at the block's left.
  * Every block: the squad's player names, three or four of them, each
    with "N finishes" -- that player's kills -- right-aligned beside it.

What this does NOT know is the SLOT. The results screen never prints one.
The slot comes from matching the names against the names read off the
alive panel during the match (bgmi_panel.match_squads), which is why the
panel's name reading matters at all.

Several captures are normal: the right side scrolls, so a lobby takes two
or three to cover. They are merged by rank, and a block caught clipped at
the edge of the scroll loses to the same block read whole elsewhere.
"""

import re

import numpy as np
from PIL import Image

# x ranges, 1920x1080 frame.
LEFT_NAMES = (262, 700)
LEFT_FINISH = (790, 960)
RIGHT_NAMES = (1185, 1640)
RIGHT_FINISH = (1660, 1800)
RIGHT_RANK = (1030, 1140)
RIGHT_GOLD_PROBE = (1022, 1045)     # the bright gold left of the rank number
LEFT_BLOCK_GAP = 90                 # rows further apart than this: next block

ROW_MIN_H, ROW_MAX_H = 16, 40       # one line of name text
VIEW_TOP, VIEW_BOTTOM = 190, 950    # the right column's scroll window


def _white(rgb):
    """White text: bright in all three channels. The gold card is bright in
    red and green but not blue, so this keeps the text and drops the card."""
    return rgb.min(axis=2) > 150


def _runs(profile, threshold=0):
    out, start = [], None
    for i, v in enumerate(profile):
        if v > threshold and start is None:
            start = i
        elif v <= threshold and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(profile)))
    return out


def _text_rows(white, x0, x1, y0=0, y1=None):
    """Lines of text in a column: (top, bottom) of each, whole ones only."""
    y1 = white.shape[0] if y1 is None else y1
    prof = white[y0:y1, x0:x1].sum(axis=1)
    rows = []
    for a, b in _runs(prof, 2):
        if ROW_MIN_H <= b - a <= ROW_MAX_H:
            rows.append((y0 + a, y0 + b))
    return rows


def _gold_blocks(rgb):
    """The right column's squad cards, top to bottom, as (top, bottom)."""
    x0, x1 = RIGHT_GOLD_PROBE
    probe = rgb[:, x0:x1].astype(int)
    gold = ((probe[..., 0] > 110) & (probe[..., 0] - probe[..., 2] > 70)).mean(axis=1)
    return [(a, b) for a, b in _runs(gold, 0.5) if b - a >= 100]


def _crop_text(mono, x0, x1, y0, y1, scale=2):
    """Black text on white, upscaled -- what Tesseract wants.

    Cut against the crop's OWN floor and peak, not a fixed level: the
    names are white, but "N finishes" is grey (~150) and a cut made for
    white text left it as a dotted skeleton Tesseract skipped entirely.
    `mono` is the darkest channel, so the gold card (low in blue) is
    background however bright it is."""
    crop = mono[max(0, y0):y1, x0:x1].astype(np.float32)
    if crop.size == 0:
        return Image.new("L", (10, 10), 255)
    floor, peak = np.percentile(crop, 20), np.percentile(crop, 99.5)
    if peak - floor > 40:
        m = (crop - floor) > 0.45 * (peak - floor)
    else:
        m = np.zeros(crop.shape, dtype=bool)
    img = Image.fromarray(np.where(m, 0, 255).astype(np.uint8))
    return img.resize((img.width * scale, img.height * scale), Image.LANCZOS)


def layout(rgb):
    """Where every squad block and line is, before any OCR.

    Returns a list of blocks: {"side", "rank" (left only), "rankBox"
    (right only), "rows": [(y0, y1)], "clipped"}."""
    white = _white(rgb)
    blocks = []

    # LEFT: ranks 1 and 2, told apart by the gap between them.
    left = _text_rows(white, *LEFT_NAMES)
    groups = []
    for r in left:
        if groups and r[0] - groups[-1][-1][1] <= LEFT_BLOCK_GAP:
            groups[-1].append(r)
        else:
            groups.append([r])
    for rank, rows in enumerate(groups[:2], start=1):
        if 1 <= len(rows) <= 4:
            blocks.append({"side": "left", "rank": rank, "rows": rows,
                           "clipped": False})

    # RIGHT: one gold card per squad.
    for top, bottom in _gold_blocks(rgb):
        rows = _text_rows(white, *RIGHT_NAMES, y0=top, y1=bottom)
        if not 1 <= len(rows) <= 4:
            continue
        clipped = top <= VIEW_TOP + 4 or bottom >= VIEW_BOTTOM - 4
        blocks.append({"side": "right", "rows": rows, "clipped": clipped,
                       "rankBox": (RIGHT_RANK[0], top, RIGHT_RANK[1], bottom)})
    return blocks


FINISH_RE = re.compile(r"(\d+)\s*f")


def read_screen(rgb, ocr_lines, slot_store=None):
    """Every squad block on one results capture.

    `ocr_lines(image, count, digits=False)` reads a stack of lines in one
    pass and returns exactly `count` strings, or a str explaining why not.

    Returns ([{"rank", "names", "finishes", "clipped"}], note)."""
    mono = rgb.min(axis=2)
    blocks = layout(rgb)
    if not blocks:
        return [], "No results blocks found -- is the results screen up?"

    names_imgs, finish_imgs, rank_imgs = [], [], []
    for b in blocks:
        nx, fx = ((LEFT_NAMES, LEFT_FINISH) if b["side"] == "left"
                  else (RIGHT_NAMES, RIGHT_FINISH))
        for y0, y1 in b["rows"]:
            names_imgs.append(_crop_text(mono, nx[0], nx[1], y0 - 4, y1 + 4))
            # "N finishes" sits a few pixels lower than its name on the
            # gold cards, so its band is taken a little deeper.
            finish_imgs.append(_crop_text(mono, fx[0], fx[1], y0 - 6, y1 + 14))
        if b["side"] == "right":
            rank_imgs.append(read_rank(mono, b["rankBox"], slot_store))

    names = ocr_lines(_stack(names_imgs), len(names_imgs))
    if isinstance(names, str):
        return [], "Names: " + names
    finishes = ocr_lines(_stack(finish_imgs), len(finish_imgs), digits=True)
    if isinstance(finishes, str):
        return [], "Finishes: " + finishes
    ranks = rank_imgs

    out, i, r = [], 0, 0
    right_ranks = []
    for b in blocks:
        n = len(b["rows"])
        fin = []
        for text in finishes[i:i + n]:
            fin.append(parse_finishes(text))
        entry = {"names": names[i:i + n], "finishes": fin, "clipped": b["clipped"]}
        i += n
        if b["side"] == "left":
            entry["rank"] = b["rank"]
        else:
            right_ranks.append(ranks[r] if r < len(ranks) else None)
            entry["rank"] = None
            r += 1
        out.append(entry)

    # The right column's ranks are consecutive, so one clear read fixes
    # them all -- and a misread digit is outvoted by its neighbours.
    rights = [e for e in out if e["rank"] is None]
    if rights:
        votes = {}
        for k, val in enumerate(right_ranks):
            if val is not None and 3 <= val <= 25:
                votes[val - k] = votes.get(val - k, 0) + 1
        if votes:
            start = max(votes, key=votes.get)
            for k, e in enumerate(rights):
                e["rank"] = start + k
        else:
            for e in rights:
                out.remove(e)
    return out, ""


def parse_finishes(text):
    """"2 finishes" -> 2. The count, or None when it cannot be told.

    A thin "1" is the glyph Tesseract drops most, leaving just the word --
    but the word itself says it: only one finish is ever "finish", every
    other count is "finishes"."""
    t = (text or "").replace(" ", "")
    m = FINISH_RE.search(t) or re.match(r"(\d+)", t)
    if m:
        return int(m.group(1))
    word = re.sub(r"[^a-z]", "", t.lower())
    if word.startswith("fin") and not word.endswith("es") and len(word) <= 7:
        return 1
    return None


def _rank_crop(mono, box):
    """The big rank number, cropped tight to its own ink with a margin --
    a whole card's height of empty gold around it made Tesseract drop it."""
    x0, y0, x1, y1 = box
    crop = mono[y0:y1, x0:x1].astype(np.float32)
    if crop.size == 0:
        return Image.new("L", (10, 10), 255)
    floor, peak = np.percentile(crop, 20), np.percentile(crop, 99.5)
    m = (crop - floor) > 0.45 * max(1.0, peak - floor)
    ys, xs = np.where(m)
    if len(ys) < 20:
        return Image.new("L", (40, 40), 255)
    pad = 12
    sub = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    img = Image.new("L", (sub.shape[1] + 2 * pad, sub.shape[0] + 2 * pad), 255)
    img.paste(Image.fromarray(np.where(sub, 0, 255).astype(np.uint8)), (pad, pad))
    return img


def read_rank(mono, box, slot_store):
    """The big rank number, read with the PANEL's slot-digit matcher.

    Same condensed face as the slot numbers on the alive panel, and that
    store is clean and hole-aware (22 of 22 across two matches' frames).
    Tesseract, tried first, returned nothing for "9", "1" for "11" and
    "5" for a "9" on these. Each tall ink shape is one digit, left to
    right; None unless every digit matches clearly."""
    import bgmi_panel as bp
    from scipy import ndimage
    if not slot_store:
        return None
    x0, y0, x1, y1 = box
    crop = mono[y0:y1, x0:x1].astype(np.float32)
    if crop.size == 0:
        return None
    floor, peak = np.percentile(crop, 20), np.percentile(crop, 99.5)
    if peak - floor < 40:
        return None
    m = (crop - floor) > 0.45 * (peak - floor)
    lab, _ = ndimage.label(m)
    shapes = [sl for sl in ndimage.find_objects(lab) if sl is not None]
    if not shapes:
        return None
    tallest = max(sl[0].stop - sl[0].start for sl in shapes)
    digits = sorted((sl for sl in shapes if sl[0].stop - sl[0].start >= 0.6 * tallest),
                    key=lambda sl: sl[1].start)
    if not 1 <= len(digits) <= 2 or tallest < 30:
        return None
    text = ""
    for sl in digits:
        bm = bp._bitmap(lab[sl] > 0, min_ink=20)
        hit = bp.best_match(bm, slot_store, sim=bp.slot_similarity) if bm is not None else None
        if hit is None:
            return None
        text += hit
    return int(text)


def _stack(images, gap=26):
    if not images:
        return Image.new("L", (10, 10), 255)
    width = max(i.width for i in images)
    height = sum(i.height for i in images) + gap * (len(images) + 1)
    strip = Image.new("L", (width, height), 255)
    y = gap
    for img in images:
        strip.paste(img, (0, y))
        y += img.height + gap
    return strip


def merge(readings):
    """Several captures into one reading per rank.

    A clipped block loses to a whole one; among whole ones, the one with
    the most names read (then the most finishes read) wins."""
    best = {}
    for entries in readings:
        for e in entries:
            if e.get("rank") is None:
                continue
            score = (not e["clipped"], len([n for n in e["names"] if n]),
                     sum(1 for f in e["finishes"] if f is not None))
            if e["rank"] not in best or score > best[e["rank"]][0]:
                best[e["rank"]] = (score, e)
    # A finish one capture could not read is taken from another capture of
    # the same squad -- the same names in the same order, seen again.
    merged = {}
    for rank, (score, e) in sorted(best.items()):
        e = dict(e, finishes=list(e["finishes"]))
        for entries in readings:
            for other in entries:
                if (other.get("rank") == rank and len(other["names"]) == len(e["names"])
                        and other is not e):
                    for k, f in enumerate(e["finishes"]):
                        if f is None and other["finishes"][k] is not None:
                            e["finishes"][k] = other["finishes"][k]
        merged[rank] = e
    return merged
