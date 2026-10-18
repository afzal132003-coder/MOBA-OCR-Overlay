"""RNX's cards that the event's own art does not include, drawn in its look.

The package ships the alive table, the top bar ("TOP5", four cards), the
ELIMINATED pop-up and the status icons. The other cards the graphics draw
(the side pop-up, the Champion Rush tile, the post-match kit and its
screens) keep the SS3 package's GEOMETRY -- the pages already place every
field on it -- and are drawn here in RNX's look, sampled from its art:

    rows / panels    purple                 #600c84   (96, 12, 132)
    elimination      purple                 #541874
    accent           gold                   #fcd02c   (the elims / status cells)
    accent deep      gold                   #fcb814   (the legend bar)
    ink on gold      dark violet            #500864   (the kills, the alive bars)
    deep             near-black violet      #22062f   (the event backdrop's corner)
    face             Winner Sans when its file is in font/ (the event's own,
                     a paid face -- not in this public repo), else TT
                     Supermolot Neue, the squared sports face closest to it

Also the alive table with a twelfth row: the event's art has eleven, and
a twelfth squad would otherwise sit on the legend.

Written under SS3's file names so the code finds them where it looks:

    python overlay/assets/freefire/RNX/make_rnx_art.py
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(HERE, "..", "kit")
SS3 = os.path.join(HERE, "..", "ss3")
FONTS = os.path.join(HERE, "..", "..", "fonts")


def _face(names, fallback):
    for n in names:
        p = os.path.join(HERE, "font", n)
        if os.path.exists(p):
            return p
    return os.path.join(FONTS, fallback)


FONT = _face(["winner-sans.otf", "winner-sans.ttf"], "TTSupermolotNeueTrial-ExtraBold.ttf")
DISPLAY = _face(["winner-sans-black.otf", "winner-sans-black.ttf", "winner-sans.otf", "winner-sans.ttf"],
                "TTSupermolotNeueTrial-Black.ttf")

PANEL = (96, 12, 132)
DEEP = (34, 6, 47)
CARD_A, CARD_B = (112, 26, 150), (66, 10, 92)
GOLD = (252, 208, 44)
GOLD_DEEP = (252, 184, 20)
VIOLET = (80, 8, 100)
SHEEN = (190, 120, 230)
WHITE = (252, 252, 252)


def gradient(w, h, a, b):
    """a -> b, top-left to bottom-right."""
    yy, xx = np.mgrid[0:h, 0:w]
    f = ((xx / max(1, w - 1)) * 0.7 + (yy / max(1, h - 1)) * 0.3)[..., None]
    arr = (np.array(a) * (1 - f) + np.array(b) * f).astype("uint8")
    return Image.fromarray(arr, "RGB")


def cut_poly(x0, y0, x1, y1, tl=0, tr=0, br=0, bl=0):
    """A box with its corners cut at 45 degrees."""
    return [(x0 + tl, y0), (x1 - tr, y0), (x1, y0 + tr), (x1, y1 - br), (x1 - br, y1),
            (x0 + bl, y1), (x0, y1 - bl), (x0, y0 + tl)]


def paste_shape(canvas, poly, fill):
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    if isinstance(fill, tuple):
        layer = Image.new("RGBA", canvas.size, fill + (255,))
    else:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        layer.paste(fill.convert("RGBA"), (int(min(xs)), int(min(ys))))
    canvas.paste(layer, (0, 0), mask)


def sheen(canvas, x0, y0, x1, y1, step=60, width=18, alpha=34):
    """The silky diagonal light the event's purple panels carry."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x in range(x0 - (y1 - y0), x1, step):
        d.polygon([(x, y1), (x + width, y1), (x + width + (y1 - y0), y0), (x + (y1 - y0), y0)],
                  fill=SHEEN + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(5))
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rectangle([x0, y0, x1, y1], fill=255)
    clipped = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    clipped.paste(layer, (0, 0), mask)
    canvas.alpha_composite(clipped)


def outline_frame(canvas, box, gap=6, width=4):
    """The event's gold frame, offset down-right of a panel (its cards
    sit in one, the way the ELIMINATED pop-up does)."""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(canvas)
    d.rectangle([x0 - gap, y1 + gap - width + 1, x1 + gap, y1 + gap], fill=GOLD + (255,))
    d.rectangle([x1 + gap - width + 1, y0 - gap, x1 + gap, y1 + gap], fill=GOLD + (255,))
    d.rectangle([x0 - gap, y0 - gap, x0 - gap + width - 1, y1 + gap], fill=GOLD + (255,))
    d.rectangle([x0 - gap, y0 - gap, x1 + gap, y0 - gap + width - 1], fill=GOLD + (255,))


def text(canvas, xy, s, size, fill, font=FONT, anchor="mm"):
    ImageDraw.Draw(canvas).text(xy, s, font=ImageFont.truetype(font, size), fill=fill, anchor=anchor)


def shadowed(canvas, poly, blur=8, alpha=120, dy=4):
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x, y + dy) for x, y in poly], fill=(0, 0, 0, alpha))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))


# ---- the alive table with a twelfth row ----------------------------------
# Eleven 39px rows from y 351 on a 49px pitch, the legend bar under them at
# y 892..920. The twelfth is row eleven's strip one pitch lower, and the
# legend goes down with it.
def table_12():
    src = Image.open(os.path.join(HERE, "ALIVE STATS - BLANK.png")).convert("RGBA")
    out = src.copy()
    ImageDraw.Draw(out).rectangle([0, 892, 1919, 1079], fill=(0, 0, 0, 0))
    out.alpha_composite(src.crop((1580, 843, 1920, 892)), (1580, 892))
    out.alpha_composite(src.crop((1580, 892, 1920, 922)), (1580, 941))
    out.save(os.path.join(HERE, "ALIVE STATS - BLANK 12.png"), optimize=True)


# ---- the ACHIEVEMENT card (side pop-ups): SS3's geometry ---------------------
# title tab x 143..311 / y 451..500; photo box x 0..135 / y 462..605; the
# number x 135..300 / y 500..575, its label under it; logo box x 0..58 /
# y 592..651; name bar x 58..300 / y 605..651; and the strip in front of the
# photo (the page's "over" clip): the ribbon across its corner.
def achievement():
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    body = cut_poly(0, 462, 301, 652)
    shadowed(c, body)
    paste_shape(c, body, gradient(302, 191, CARD_A, CARD_B))
    sheen(c, 135, 462, 301, 604)
    ImageDraw.Draw(c).rectangle([0, 462, 135, 605], fill=DEEP + (255,))
    paste_shape(c, cut_poly(143, 451, 311, 500), GOLD)
    paste_shape(c, [(82, 606), (136, 579), (136, 595), (112, 606)], GOLD_DEEP)
    ImageDraw.Draw(c).rectangle([0, 592, 58, 651], fill=PANEL + (255,))
    ImageDraw.Draw(c).rectangle([0, 592, 58, 651], outline=GOLD + (255,), width=2)
    paste_shape(c, [(58, 605), (301, 605), (301, 652), (58, 652)], GOLD)
    ImageDraw.Draw(c).rectangle([303, 466, 306, 656], fill=GOLD + (255,))
    ImageDraw.Draw(c).rectangle([4, 653, 306, 656], fill=GOLD + (255,))
    c.save(os.path.join(HERE, "prg br ss3 achivement - BLANK.png"), optimize=True)


# ---- the Champion Rush tile: 45 x 45, beside a row ----------------------------
def crown_tile():
    c = Image.new("RGBA", (45, 45), (0, 0, 0, 0))
    paste_shape(c, [(0, 0), (44, 0), (44, 44), (0, 44)], GOLD)
    d = ImageDraw.Draw(c)
    d.rectangle([0, 0, 44, 44], outline=VIOLET + (255,), width=2)
    d.polygon([(10, 31), (8, 15), (16, 22), (22.5, 11), (29, 22), (37, 15), (35, 31)], fill=VIOLET + (255,))
    d.rectangle([10, 32, 35, 35], fill=VIOLET + (255,))
    for cx, cy in ((8, 14), (22.5, 10), (37, 14)):
        d.ellipse([cx - 2.2, cy - 2.2, cx + 2.2, cy + 2.2], fill=VIOLET + (255,))
    c.save(os.path.join(HERE, "cr_tile.png"), optimize=True)


# ---- the post-match KIT (assets/freefire/kit/rnx_*): drawn ------------------
# Same places as SS3's so the pages' title (x 470..1460, y 118..258) and
# round bar (x 697..1044, y 273..320) land on it: gold corner brackets, the
# event's purple-and-gold sweep down the left (as its backdrop has it),
# RNX's wordmark where an event logo would stand, a deep footer.
def wordmark(canvas, cx, cy, size=104):
    text(canvas, (cx + 4, cy + 5), "RNX", size, (0, 0, 0, 110), font=DISPLAY)
    text(canvas, (cx, cy), "RNX", size, GOLD + (255,), font=DISPLAY)


def sweep(canvas):
    """The backdrop's top-left: a purple wedge edged in gold."""
    d = ImageDraw.Draw(canvas)
    d.polygon([(0, 0), (520, 0), (40, 300), (0, 420)], fill=PANEL + (235,))
    d.polygon([(520, 0), (560, 0), (62, 312), (0, 470), (0, 420), (40, 300)], fill=GOLD + (255,))
    d.polygon([(560, 0), (578, 0), (72, 318), (0, 492), (0, 470), (62, 312)], fill=PANEL + (235,))


def kit_frame(round_bar=True, save=True):
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(c)
    sweep(c)
    for x, sx in ((1888, -1),):
        d.polygon([(x, 30), (x + sx * 26, 30), (x + sx * 26, 34), (x + sx * 4, 34), (x + sx * 4, 56), (x, 56)], fill=GOLD + (255,))
    # the bottom-right sweep, gold under deep violet
    d.polygon([(1920, 760), (1920, 1080), (1240, 1080)], fill=GOLD + (255,))
    d.polygon([(1920, 790), (1920, 1080), (1300, 1080)], fill=DEEP + (240,))
    wordmark(c, 1758, 118)
    if round_bar:
        paste_shape(c, [(698, 274), (1044, 274), (1044, 316), (698, 316)], GOLD)
    d.rectangle([0, 922, 1920, 1005], fill=DEEP + (235,))
    d.rectangle([0, 920, 1920, 922], fill=GOLD + (255,))
    if save:
        c.save(os.path.join(KIT, "rnx_frame.png"), optimize=True)
    return c


def kit_zebra():
    """The panels' texture: RNX's purple, lit to the right, its sheen."""
    c = Image.new("RGBA", (485, 330), (0, 0, 0, 0))
    c.paste(gradient(485, 330, CARD_A, CARD_B), (0, 0))
    sheen(c, 0, 0, 485, 330, step=70, width=22, alpha=40)
    c.save(os.path.join(KIT, "rnx_zebra.png"), optimize=True)


def kit_roundbar():
    c = Image.new("RGBA", (347, 47), (0, 0, 0, 0))
    paste_shape(c, [(1, 1), (346, 1), (346, 46), (1, 46)], GOLD)
    c.save(os.path.join(KIT, "rnx_roundbar.png"), optimize=True)


# ---- SS3's screens in RNX's palette, its branding taken off ------------------
def recolour(arr):
    """SS3's reds -> RNX's purples (shading kept), its yellows and oranges
    -> RNX's gold, blacks -> RNX's deep violet; whites stay."""
    rgb = arr[..., :3].astype(float) / 255
    mx, mn = rgb.max(-1), rgb.min(-1)
    v, sat = mx, np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hue = np.degrees(np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)) % 360
    out = arr[..., :3].astype(float).copy()
    deep, card_a, gold = np.array(DEEP, float), np.array(CARD_A, float), np.array(GOLD, float)
    red = (sat > .35) & ((hue < 22) | (hue > 330))
    warm = (sat > .25) & (hue >= 22) & (hue < 75)
    grey = sat <= .25
    f = np.clip(v / .92, 0, 1.15)[..., None]
    out[red] = (deep + (card_a - deep) * f)[red]
    out[warm] = (gold * np.clip(.6 + .4 * v, 0, 1)[..., None])[warm]
    low = grey & (v < .55)
    out[low] = (deep * (.6 + .8 * v)[..., None])[low]
    res = arr.copy()
    res[..., :3] = np.clip(out, 0, 255).astype("uint8")
    return res


def clear(arr, polys):
    """Takes these regions off (transparent): the SS3 / PRG branding."""
    m = Image.new("L", (arr.shape[1], arr.shape[0]), 0)
    for poly in polys:
        ImageDraw.Draw(m).polygon(poly, fill=255)
    arr[np.asarray(m) > 0, 3] = 0
    return arr


def band(x0, y0, x1, y1, width):
    """A diagonal strip's outline (to clear one of SS3's lettered ribbons)."""
    L = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** .5
    nx, ny = -(y1 - y0) / L * width / 2, (x1 - x0) / L * width / 2
    return [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]


def fit_text(canvas, box, s, font, fill, align="right"):
    """s set as large as fits the box (x0, y0, x1, y1), aligned in it."""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(canvas)
    size = int((y1 - y0) * 1.6)
    while size > 10:
        f = ImageFont.truetype(font, size)
        l, t, r, b = d.textbbox((0, 0), s, font=f)
        if r - l <= x1 - x0 and b - t <= y1 - y0:
            break
        size -= 1
    x = x1 - (r - l) - l if align == "right" else x0 - l
    d.text((x, y0 - t + ((y1 - y0) - (b - t)) / 2), s, font=f, fill=fill)


def rnx_title(canvas, box, words):
    """A title as the pages' RNX titles are set: the heavy face, white, the
    last word in gold, a drop shadow, as large as fits the box."""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(canvas)
    size = int((y1 - y0) * 1.3)
    while size > 20:
        f = ImageFont.truetype(DISPLAY, size)
        widths = [d.textlength(w, font=f) for w in words]
        space = d.textlength(" ", font=f) * 1.1
        total = sum(widths) + space * (len(words) - 1)
        l, t, r, b = d.textbbox((0, 0), " ".join(words), font=f)
        if total <= x1 - x0 and b - t <= y1 - y0:
            break
        size -= 1
    x = (x0 + x1) / 2 - total / 2
    y = y0 + ((y1 - y0) - (b - t)) / 2 - t
    for i, (w, wd) in enumerate(zip(words, widths)):
        colour = GOLD if i == len(words) - 1 and len(words) > 1 else WHITE
        d.text((x + 4, y + 5), w, font=f, fill=(0, 0, 0, 110))
        d.text((x, y), w, font=f, fill=colour + (255,))
        x += wd + space


# ---- PREFERRED DROP SPOT, drawn: every part the map page uses where SS3's
# was (see make_gng_art.py): the map box border x 65..1004 / y 58..997, the
# MATCH tag x 65..231 / y 910..947 and the map-name tag x 65..280 /
# y 948..998, the round bar at y 435..482 centred on x 1600, the team panel
# x 1055..1855 / y 541..996 with twelve 150x27 name bars at
# x 1090/1283/1477/1672, y 662/799/935.
def drop_spot():
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(c)
    sweep(c)
    for box in ((65, 58, 1004, 60), (65, 995, 1004, 997), (65, 58, 67, 997), (1002, 58, 1004, 997)):
        d.rectangle(box, fill=GOLD + (255,))
    paste_shape(c, [(65, 910), (231, 910), (231, 947), (65, 947)], GOLD)
    paste_shape(c, [(65, 948), (280, 948), (280, 998), (65, 998)], PANEL)
    d.rectangle([65, 948, 68, 998], fill=GOLD + (255,))
    fit_text(c, (1250, 232, 1800, 300), "PREFERRED", DISPLAY, WHITE + (255,))
    fit_text(c, (1080, 316, 1800, 410), "DROP SPOT", DISPLAY, GOLD + (255,))
    paste_shape(c, [(1428, 435), (1772, 435), (1772, 482), (1428, 482)], GOLD)
    panel = (1055, 541, 1855, 996)
    shadowed(c, cut_poly(*panel))
    paste_shape(c, cut_poly(*panel), gradient(801, 456, CARD_A, CARD_B))
    sheen(c, *panel)
    outline_frame(c, panel)
    for y in (662, 799, 935):
        for x in (1090, 1283, 1477, 1672):
            paste_shape(c, [(x, y), (x + 150, y), (x + 150, y + 27), (x, y + 27)], GOLD)
    wordmark(c, 1758, 118)
    c.save(os.path.join(HERE, "prg br ss3 drop spot - BLANK.png"), optimize=True)


def ss3_screen(name, frame, polys=(), keep_frame_diff=True, title=None, bar=None):
    scr = np.asarray(Image.open(os.path.join(SS3, name)).convert("RGBA")).copy()
    if keep_frame_diff:
        # the SS3 frame's own pixels go: RNX's frame replaces them
        f = np.asarray(Image.open(os.path.join(KIT, "ss3_frame.png")).convert("RGBA")).astype(int)
        same = (f[..., 3] > 40) & (np.abs(scr.astype(int) - f).sum(-1) < 40)
        scr[same, 3] = 0
    # SS3's logo, top left, whose soft edge outlives the frame match
    scr = clear(scr, list(polys) + [[(55, 45), (205, 45), (205, 125), (55, 125)]])
    scr = recolour(scr)
    out = frame.copy()
    out.alpha_composite(Image.fromarray(scr, "RGBA"))
    if title:
        rnx_title(out, title[0], title[1])
    if bar:
        x0, y0, x1, y1 = bar
        paste_shape(out, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], GOLD)
    out.save(os.path.join(HERE, name), optimize=True)


if __name__ == "__main__":
    table_12()
    achievement()
    crown_tile()
    frame = kit_frame()
    kit_zebra()
    kit_roundbar()
    # as GNG's: SS3's lettered ribbon, hashtag logo, its own title and the
    # bar under it come off the booyah slides; RNX's title and one clean
    # bar (x 746..1018 / y 240..275, where the page writes the round) go on
    booyah_off = [band(-40, 360, 640, -40, 130), [(1440, 130), (1660, 130), (1660, 300), (1440, 300)],
                  [(470, 130), (1460, 130), (1460, 238), (470, 238)], [(735, 236), (1030, 236), (1030, 282), (735, 282)]]
    frame_no_bar = kit_frame(round_bar=False, save=False)
    booyah_title = ((500, 150, 1440, 234), ["BOOYAH", "TEAM", "STATS"])
    ss3_screen("booyah team stats R - BLANK1.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    ss3_screen("booyah team stats character R - BLANK.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    # SS3's own title; the tail of its lettered SURVIVOR SERIES ribbon where
    # it crosses the right-hand card (and an orange stripe under that
    # card, halftone that recolours to speckle)
    ss3_screen("head to head R - BLANK.png", frame, [[(470, 125), (1460, 125), (1460, 262), (470, 262)],
               [(1794, 380), (1920, 380), (1920, 520), (1794, 520)], [(1255, 860), (1400, 860), (1400, 912), (1255, 912)]],
               title=((495, 144, 1440, 241), ["HEAD", "TO", "HEAD"]))
    drop_spot()
    print("written with %s / %s: the 12-row table, the in-game cards, the kit (rnx_frame / rnx_zebra / "
          "rnx_roundbar), and the SS3 screens in RNX" % (os.path.basename(FONT), os.path.basename(DISPLAY)))
