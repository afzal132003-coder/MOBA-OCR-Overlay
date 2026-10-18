"""GNG's cards that the event's own art does not include, drawn in its look.

The package ships the alive table, the Top 4 bar, the status icons and the
fonts. The other cards the graphics draw (the ELIMINATED card, the side
pop-up, the Champion Rush tile, the post-match kit) keep the SS3 package's
GEOMETRY -- the pages already place every field on it -- and are drawn
here in GNG's look, sampled from its art:

    panel        dark brown rows        #2f1307   (47, 19, 7)
    card         brown, lit to the right #5c2d12 -> #3a1a0a (the Top 4 cards)
    accent       orange                 #ffa976   (the status bars, the WR tab)
    slash        dark grey               #2b2b2b  (the rows' angled cuts)
    ink          light                   #e4e4e4
    faces        Moanslight Bold (text), Sddystopian (display)

Written under SS3's file names so the code finds them where it looks:

    python overlay/assets/freefire/gng/make_gng_art.py
"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(HERE, "..", "kit")
FONT = os.path.join(HERE, "font", "MOANSLIGHT-BOLD.OTF")
DISPLAY = os.path.join(HERE, "font", "Sddystopiandemo-GO7xa.otf")

PANEL = (47, 19, 7)
DARK = (29, 14, 6)
CARD_A, CARD_B = (92, 45, 18), (58, 26, 10)
ORANGE = (255, 169, 118)
ORANGE_DEEP = (232, 128, 72)
SLASH = (43, 43, 43)
INK = (228, 228, 228)


def gradient(w, h, a, b, angle_lr=True):
    """a -> b, top-left to bottom-right."""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            f = (x / max(1, w - 1)) * 0.7 + (y / max(1, h - 1)) * 0.3
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))
    return im


def cut_poly(x0, y0, x1, y1, tl=0, tr=0, br=0, bl=0):
    """A box with its corners cut at 45 degrees."""
    return [(x0 + tl, y0), (x1 - tr, y0), (x1, y0 + tr), (x1, y1 - br), (x1 - br, y1),
            (x0 + bl, y1), (x0, y1 - bl), (x0, y0 + tl)]


def paste_shape(canvas, poly, fill_img_or_colour):
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    if isinstance(fill_img_or_colour, tuple):
        layer = Image.new("RGBA", canvas.size, fill_img_or_colour + (255,))
    else:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        layer.paste(fill_img_or_colour.convert("RGBA"), (int(min(xs)), int(min(ys))))
    canvas.paste(layer, (0, 0), mask)


def slashes(canvas, x0, y0, x1, y1, step=46, width=7, colour=SLASH, alpha=150):
    """The rows' faint angled cuts, clipped to the box."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x in range(x0 - (y1 - y0), x1, step):
        d.polygon([(x, y1), (x + width, y1), (x + width + (y1 - y0), y0), (x + (y1 - y0), y0)],
                  fill=colour + (alpha,))
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rectangle([x0, y0, x1, y1], fill=255)
    clipped = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    clipped.paste(layer, (0, 0), mask)
    canvas.alpha_composite(clipped)


def accent(canvas, x, y, w=14, h=5, slant=4):
    """GNG's small orange parallelogram."""
    ImageDraw.Draw(canvas).polygon([(x + slant, y), (x + w + slant, y), (x + w, y + h), (x, y + h)], fill=ORANGE + (255,))


def text(canvas, xy, s, size, fill, font=FONT, anchor="mm", spacing=0):
    f = ImageFont.truetype(font, size)
    d = ImageDraw.Draw(canvas)
    if not spacing:
        d.text(xy, s, font=f, fill=fill, anchor=anchor)
        return
    # letter-spaced, centred on xy
    widths = [d.textlength(ch, font=f) for ch in s]
    total = sum(widths) + spacing * (len(s) - 1)
    x = xy[0] - total / 2
    for ch, w in zip(s, widths):
        d.text((x, xy[1]), ch, font=f, fill=fill, anchor="lm")
        x += w + spacing


def shadowed(canvas, poly, blur=8, alpha=120, dy=4):
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x, y + dy) for x, y in poly], fill=(0, 0, 0, alpha))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))


# ---- the ELIMINATED card: SS3's "eliminated bug" geometry --------------------
# panel x 763..1190, y 101..305; photo x 764..1180 / y 108..245 in front of
# the lettering; logo box x 764..835 / y 245..304; name bar x 836..1170 /
# y 253..295; "#N" box x 1148..1190 / y 101..146.
def eliminated():
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    body = cut_poly(763, 101, 1190, 305, tl=0, tr=0, br=14, bl=0)
    shadowed(c, body)
    paste_shape(c, body, gradient(428, 205, CARD_A, CARD_B))
    slashes(c, 763, 101, 1190, 245, step=58, width=9, alpha=90)
    # the word, behind where the photo stands: fitted between the panel's
    # left edge and the "#N" box (x 775..1138)
    size = 78
    while size > 30:
        f = ImageFont.truetype(DISPLAY, size)
        w = sum(ImageDraw.Draw(c).textlength(ch, font=f) for ch in "ELIMINATED") + 2 * 9
        if w <= 355:
            break
        size -= 1
    text(c, (956, 160), "ELIMINATED", size, ORANGE + (255,), font=DISPLAY, spacing=2)
    # the band under the photo
    ImageDraw.Draw(c).rectangle([763, 245, 1190, 305], fill=DARK + (255,))
    ImageDraw.Draw(c).rectangle([763, 245, 1190, 247], fill=ORANGE_DEEP + (255,))
    # logo box
    ImageDraw.Draw(c).rectangle([764, 250, 834, 302], fill=PANEL + (255,))
    ImageDraw.Draw(c).rectangle([764, 250, 834, 302], outline=ORANGE + (255,), width=2)
    # name bar
    bar = [(844, 253), (1170, 253), (1162, 295), (836, 295)]
    paste_shape(c, bar, ORANGE)
    accent(c, 1176, 290, w=10, h=5, slant=4)
    # "#N" box, top-right
    box = cut_poly(1148, 101, 1190, 146, bl=8)
    paste_shape(c, box, DARK)
    ImageDraw.Draw(c).line([(1148, 146), (1190, 146)], fill=ORANGE + (255,), width=2)
    accent(c, 770, 108, w=16, h=5)
    c.save(os.path.join(HERE, "eliminated bug - BLANK.png"), optimize=True)


# ---- the ACHIEVEMENT card (side pop-ups): SS3's geometry ---------------------
# title tab x 143..311 / y 451..500; photo box x 0..135 / y 462..605; the
# number x 135..300 / y 500..575, its label under it; logo box x 0..58 /
# y 592..651; name bar x 58..300 / y 605..651; and the strip in front of the
# photo (the page's "over" clip): the ribbon across its corner.
def achievement():
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    body = cut_poly(0, 462, 301, 652, tr=12)
    shadowed(c, body)
    paste_shape(c, body, gradient(302, 191, CARD_A, CARD_B))
    slashes(c, 135, 500, 301, 604, step=40, width=7, alpha=90)
    # the photo's own box, darker
    ImageDraw.Draw(c).rectangle([0, 462, 135, 605], fill=DARK + (255,))
    # title tab, orange like the Top 4's WR tab
    tab = cut_poly(143, 451, 311, 500, tr=10, bl=10)
    paste_shape(c, tab, ORANGE)
    # the ribbon across the photo's corner (the page draws it in front)
    paste_shape(c, [(82, 606), (136, 579), (136, 595), (112, 606)], ORANGE_DEEP)
    # logo box and name bar
    ImageDraw.Draw(c).rectangle([0, 592, 58, 651], fill=PANEL + (255,))
    ImageDraw.Draw(c).rectangle([0, 592, 58, 651], outline=ORANGE + (255,), width=2)
    paste_shape(c, [(58, 605), (301, 605), (301, 652), (58, 652)], ORANGE)
    accent(c, 280, 588, w=12, h=5)
    c.save(os.path.join(HERE, "prg br ss3 achivement - BLANK.png"), optimize=True)


# ---- the Champion Rush tile: 45 x 45, beside a row ----------------------------
def crown_tile():
    c = Image.new("RGBA", (45, 45), (0, 0, 0, 0))
    paste_shape(c, cut_poly(0, 0, 44, 44, tl=7, br=7), ORANGE)
    d = ImageDraw.Draw(c)
    # a crown, dark on the orange
    d.polygon([(10, 31), (8, 15), (16, 22), (22.5, 11), (29, 22), (37, 15), (35, 31)], fill=DARK + (255,))
    d.rectangle([10, 32, 35, 35], fill=DARK + (255,))
    for cx, cy in ((8, 14), (22.5, 10), (37, 14)):
        d.ellipse([cx - 2.2, cy - 2.2, cx + 2.2, cy + 2.2], fill=DARK + (255,))
    c.save(os.path.join(HERE, "cr_tile.png"), optimize=True)


# ---- the post-match KIT (assets/freefire/kit/gng_*): drawn, not recoloured --
# The SS3 frame carries the SS3 event's and PRG's branding (logos, lettered
# ribbons, the socials footer), none of which belongs on GNG's broadcast.
# Same places as SS3's so the pages' title (x 470..1460, y 118..258) and
# round bar (x 697..1044, y 273..320) land on it: corner brackets, plain
# orange ribbons, GNG's wordmark where the event logo stood, a dark footer.
import numpy as np


def ribbon(canvas, p0, p1, width, a=ORANGE_DEEP, b=ORANGE, dashes=True):
    """A plain diagonal strip from p0 to p1, a->b along it, GNG's dark
    parallelograms riding on it."""
    (x0, y0), (x1, y1) = p0, p1
    L = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** .5
    nx, ny = -(y1 - y0) / L * width / 2, (x1 - x0) / L * width / 2
    poly = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
    w, h = canvas.size
    yy, xx = np.mgrid[0:h, 0:w]
    t = ((xx - x0) * (x1 - x0) + (yy - y0) * (y1 - y0)) / (L * L)
    t = np.clip(t, 0, 1)[..., None]
    grad = (np.array(a) * (1 - t) + np.array(b) * t).astype("uint8")
    layer = Image.fromarray(np.dstack([grad, np.full((h, w), 255, "uint8")]), "RGBA")
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    canvas.paste(layer, (0, 0), mask)
    if dashes:
        d = ImageDraw.Draw(canvas)
        for k in range(1, int(L // 90)):
            cx, cy = x0 + (x1 - x0) * k * 90 / L, y0 + (y1 - y0) * k * 90 / L
            ux, uy = (x1 - x0) / L, (y1 - y0) / L
            d.polygon([(cx - ux * 9 + nx * .35, cy - uy * 9 + ny * .35), (cx + ux * 3 + nx * .35, cy + uy * 3 + ny * .35),
                       (cx + ux * 9 - nx * .35, cy + uy * 9 - ny * .35), (cx - ux * 3 - nx * .35, cy - uy * 3 - ny * .35)],
                      fill=DARK + (200,))


def wordmark(canvas, cx, cy, size=118):
    """GNG, in its display face, where an event logo would stand."""
    text(canvas, (cx + 4, cy + 5), "GNG", size, (0, 0, 0, 110), font=DISPLAY, spacing=4)
    text(canvas, (cx, cy), "GNG", size, ORANGE + (255,), font=DISPLAY, spacing=4)


def kit_frame(round_bar=True, save=True):
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(c)
    # corner brackets, as SS3's
    for x, sx in ((32, 1), (1888, -1)):
        d.rectangle(sorted([x, x + sx * 26]) + [30, 33], fill=ORANGE + (255,)) if False else None
        d.polygon([(x, 30), (x + sx * 26, 30), (x + sx * 26, 34), (x + sx * 4, 34), (x + sx * 4, 56), (x, 56)], fill=ORANGE + (255,))
    ribbon(c, (-30, 330), (600, -30), 44)
    ribbon(c, (1830, 478), (1950, 358), 40, dashes=False)
    ribbon(c, (1080, 1100), (1240, 900), 26, dashes=False)
    wordmark(c, 1758, 118)
    # the round bar (the page writes the round's name on it)
    if round_bar:
        paste_shape(c, [(706, 274), (1044, 274), (1036, 316), (698, 316)], ORANGE)
        accent(c, 1048, 308, w=12, h=5)
    # the footer: dark, an orange rule on top, nothing written on it
    d.rectangle([0, 922, 1920, 1005], fill=DARK + (235,))
    d.rectangle([0, 920, 1920, 922], fill=ORANGE + (255,))
    for x in range(60, 1900, 220):
        accent(c, x, 1012, w=16, h=4)
    if save:
        c.save(os.path.join(KIT, "gng_frame.png"), optimize=True)
    return c


def kit_zebra():
    """The panels' texture: GNG's brown, lit to the right, its angled cuts."""
    c = Image.new("RGBA", (485, 330), (0, 0, 0, 0))
    c.paste(gradient(485, 330, CARD_A, CARD_B), (0, 0))
    slashes(c, 0, 0, 485, 330, step=52, width=10, alpha=70)
    accent(c, 18, 14, w=18, h=5)
    c.save(os.path.join(KIT, "gng_zebra.png"), optimize=True)


def kit_roundbar():
    c = Image.new("RGBA", (347, 47), (0, 0, 0, 0))
    paste_shape(c, [(9, 1), (346, 1), (338, 46), (1, 46)], ORANGE)
    c.save(os.path.join(KIT, "gng_roundbar.png"), optimize=True)


# ---- SS3's screens in GNG's palette, its branding taken off ------------------
def recolour(arr):
    """SS3's reds -> GNG's browns (shading kept), its yellows and oranges ->
    GNG's orange, blacks -> GNG's dark brown; whites stay."""
    rgb = arr[..., :3].astype(float) / 255
    mx, mn = rgb.max(-1), rgb.min(-1)
    v, sat = mx, np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hue = np.degrees(np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)) % 360
    out = arr[..., :3].astype(float).copy()
    dark, card_a, orange = np.array(DARK, float), np.array(CARD_A, float), np.array(ORANGE, float)
    red = (sat > .35) & ((hue < 22) | (hue > 330))
    warm = (sat > .25) & (hue >= 22) & (hue < 75)
    grey = sat <= .25
    # reds: brown, as bright as the red was
    f = np.clip(v / .92, 0, 1.15)[..., None]
    out[red] = (dark + (card_a - dark) * f)[red]
    # yellows / oranges: GNG's orange, keeping the shading
    out[warm] = (orange * np.clip(.55 + .45 * v, 0, 1)[..., None])[warm]
    # blacks and greys toward GNG's dark brown, whites left white
    low = grey & (v < .55)
    out[low] = (dark * (.6 + .8 * v)[..., None])[low]
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


SS3 = os.path.join(HERE, "..", "ss3")


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


# ---- PREFERRED DROP SPOT, drawn: SS3's composition had its branding woven
# through it (two lettered ribbons, the PRG and SS3 logos). Every part the
# map page uses is where SS3's was, measured from its art:
#   the map box border x 65..1004 / y 58..997 (3px; the page puts it back
#   over the map from this image), the MATCH tag x 65..231 / y 910..947 and
#   the map-name tag x 65..280 / y 948..998 (likewise), the round bar at
#   y 435..482 centred on x 1600, the team panel x 1055..1855 / y 541..996
#   with twelve 150x27 name bars at x 1090/1283/1477/1672, y 662/799/935.
def drop_spot():
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(c)
    # ribbons behind the map (it covers them) and across the open right
    ribbon(c, (-40, 1040), (1180, -40), 46)
    ribbon(c, (1860, 300), (1960, 200), 34, dashes=False)
    # the map box border
    for box in ((65, 58, 1004, 60), (65, 995, 1004, 997), (65, 58, 67, 997), (1002, 58, 1004, 997)):
        d.rectangle(box, fill=ORANGE + (255,))
    # the tags under the map: MATCH (dark text) and the map's name (light)
    paste_shape(c, [(65, 910), (231, 910), (231, 935), (219, 947), (65, 947)], ORANGE)
    paste_shape(c, [(65, 948), (280, 948), (280, 984), (266, 998), (65, 998)], PANEL)
    d.rectangle([65, 948, 68, 998], fill=ORANGE + (255,))
    # the title, white with the second line in GNG's orange
    fit_text(c, (1250, 232, 1800, 300), "PREFERRED", DISPLAY, (240, 236, 232, 255))
    fit_text(c, (1080, 316, 1800, 410), "DROP SPOT", DISPLAY, ORANGE + (255,))
    # the round bar
    paste_shape(c, [(1440, 435), (1772, 435), (1760, 482), (1428, 482)], ORANGE)
    accent(c, 1776, 476, w=12, h=5)
    # the team panel and its twelve name bars
    panel = cut_poly(1055, 541, 1855, 996, tr=18, bl=18)
    shadowed(c, panel)
    paste_shape(c, panel, gradient(801, 456, CARD_A, CARD_B))
    slashes(c, 1055, 541, 1855, 996, step=64, width=10, alpha=70)
    for y in (662, 799, 935):
        for x in (1090, 1283, 1477, 1672):
            paste_shape(c, [(x + 6, y), (x + 150, y), (x + 144, y + 27), (x, y + 27)], ORANGE)
    accent(c, 1070, 552, w=18, h=5)
    wordmark(c, 1758, 118)
    for x, sx in ((32, 1), (1888, -1)):
        d.polygon([(x, 30), (x + sx * 26, 30), (x + sx * 26, 34), (x + sx * 4, 34), (x + sx * 4, 56), (x, 56)], fill=ORANGE + (255,))
    c.save(os.path.join(HERE, "prg br ss3 drop spot - BLANK.png"), optimize=True)


def gng_title(canvas, box, words):
    """A title as the pages' GNG titles are set: the display face, white,
    the last word in orange, a drop shadow, as large as fits the box."""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(canvas)
    size = int((y1 - y0) * 1.3)
    while size > 20:
        f = ImageFont.truetype(DISPLAY, size)
        widths = [d.textlength(w, font=f) for w in words]
        space = d.textlength(" ", font=f) * 1.2
        total = sum(widths) + space * (len(words) - 1)
        l, t, r, b = d.textbbox((0, 0), " ".join(words), font=f)
        if total <= x1 - x0 and b - t <= y1 - y0:
            break
        size -= 1
    x = (x0 + x1) / 2 - total / 2
    y = y0 + ((y1 - y0) - (b - t)) / 2 - t
    for i, (w, wd) in enumerate(zip(words, widths)):
        colour = ORANGE if i == len(words) - 1 and len(words) > 1 else (242, 236, 230)
        d.text((x + 4, y + 5), w, font=f, fill=(0, 0, 0, 90))
        d.text((x, y), w, font=f, fill=colour + (255,))
        x += wd + space


def ss3_screen(name, frame, polys=(), keep_frame_diff=True, title=None, bar=None):
    scr = np.asarray(Image.open(os.path.join(SS3, name)).convert("RGBA")).copy()
    if keep_frame_diff:
        # the SS3 frame's own pixels go: GNG's frame replaces them
        f = np.asarray(Image.open(os.path.join(KIT, "ss3_frame.png")).convert("RGBA")).astype(int)
        same = (f[..., 3] > 40) & (np.abs(scr.astype(int) - f).sum(-1) < 40)
        scr[same, 3] = 0
    # SS3's logo, top left, whose soft edge outlives the frame match
    scr = clear(scr, list(polys) + [[(55, 45), (205, 45), (205, 125), (55, 125)]])
    scr = recolour(scr)
    out = frame.copy()
    out.alpha_composite(Image.fromarray(scr, "RGBA"))
    if title:
        gng_title(out, title[0], title[1])
    if bar:
        # one clean bar where the page writes the round's name
        x0, y0, x1, y1 = bar
        paste_shape(out, [(x0 + 8, y0), (x1, y0), (x1 - 8, y1), (x0, y1)], ORANGE)
        accent(out, x1 + 4, y1 - 6, w=12, h=5)
    out.save(os.path.join(HERE, name), optimize=True)


if __name__ == "__main__":
    eliminated()
    achievement()
    crown_tile()
    frame = kit_frame()
    kit_zebra()
    kit_roundbar()
    # BOOYAH! TEAM STATS (both slides): its own lettered ribbon top-left and
    # the hashtag logo beside the title come off
    # ...and SS3's own title (white halftone) and the bar under it: GNG's
    # title drawn in their place, one clean bar where the page writes the
    # round (booyah x 746..1018 / y 240..275; H2H's is the frame's own)
    booyah_off = [band(-40, 360, 640, -40, 130), [(1440, 130), (1660, 130), (1660, 300), (1440, 300)],
                  [(470, 130), (1460, 130), (1460, 238), (470, 238)], [(735, 236), (1030, 236), (1030, 282), (735, 282)]]
    frame_no_bar = kit_frame(round_bar=False, save=False)
    booyah_title = ((500, 150, 1440, 234), ["BOOYAH", "TEAM", "STATS"])
    ss3_screen("booyah team stats R - BLANK1.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    ss3_screen("booyah team stats character R - BLANK.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    # SS3's own title; the tail of its lettered SURVIVOR SERIES ribbon where
    # it crosses the right-hand card
    ss3_screen("head to head R - BLANK.png", frame, [[(470, 125), (1460, 125), (1460, 262), (470, 262)],
               [(1794, 380), (1920, 380), (1920, 520), (1794, 520)]],
               title=((495, 144, 1440, 241), ["HEAD", "TO", "HEAD"]))
    drop_spot()
    print("written: the in-game cards, the kit (gng_frame / gng_zebra / gng_roundbar), and the SS3 screens in GNG")
