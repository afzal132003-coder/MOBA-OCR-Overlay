"""GamerCult's cards that the event's own art does not include, drawn in
its look.

The package ships the alive table (twelve rows, the yellow header), the
ELIMINATED pop-up (the squad photo on the yellow, the red name bar, the
red logo box) and the status triangles. The rest is drawn with the RNX
generator (make_rnx_art.py: SS3's geometry for the side pop-up, the
Champion Rush tile, the post-match kit and its screens) in GamerCult's
palette, sampled from its art:

    panels         red           #e30000   (227, 0, 0)
    deep           dark red      #780000   (the footer, the dark cards)
    accent         yellow        #feea3c   (254, 234, 60)
    ink            near-black    #080808   (on the yellow and the white)
    face           Bricolage Grotesque (SIL OFL, overlay/assets/fonts/
                   bricolage-grotesque): 24pt SemiBold for text, 48pt
                   ExtraBold, slanted, for the heavy lettering

and a Top 4 bar of its own (the package has none), in the ELIMINATED
pop-up's language: the red logo box, the red name bar, the white panel
with the win rate and the four status triangles.

    python overlay/assets/freefire/GamerCult/make_gamercult_art.py
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "RNX"))
import make_rnx_art as k  # noqa: E402

FONTS = os.path.join(HERE, "..", "..", "fonts", "bricolage-grotesque")
SANS = os.path.join(FONTS, "BricolageGrotesque_24pt-SemiBold.ttf")
BLACK = os.path.join(FONTS, "BricolageGrotesque_48pt-ExtraBold.ttf")

k.HERE = HERE
k.KIT_NAME = "gamercult"
k.FONT, k.DISPLAY, k.BOLD = SANS, BLACK, 0
k.PANEL = (227, 0, 0)
k.DEEP = (120, 0, 0)
k.CARD_A, k.CARD_B = (236, 18, 12), (169, 2, 1)
k.GOLD = (254, 234, 60)
k.GOLD_DEEP = (245, 205, 20)
k.VIOLET = (8, 8, 8)
k.SHEEN = (255, 150, 120)
RED, YELLOW, INK, WHITE = k.PANEL, k.GOLD, k.VIOLET, k.WHITE
SLANT = 0.21          # the lettering's lean: ELIMINATED's, measured off the art


def slanted(s, size, fill, font=BLACK):
    """s set in the face and leaned as the event's lettering is, on its
    own transparent layer (Pillow has no oblique)."""
    f = ImageFont.truetype(font, size)
    l, t, r, b = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), s, font=f)
    w, h = r - l + int(size * SLANT) + 8, b - t + 8
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((4 - l + int(size * SLANT), 4 - t), s, font=f, fill=fill)
    # x' = x + SLANT * (h - y): the top leans right
    return layer.transform(layer.size, Image.AFFINE, (1, SLANT, -SLANT * h, 0, 1, 0), Image.BICUBIC)


def wordmark(canvas, cx, cy, size=50):
    """GAMERCULT where RNX writes its name: GAMER white, CULT yellow, leaned,
    over a soft shadow."""
    a, b = slanted("GAMER", size, WHITE + (255,)), slanted("CULT", size, YELLOW + (255,))
    w = a.width + b.width - int(size * SLANT)
    # centred where it is asked for, but never nearer the edge than 40px
    x, y = min(int(cx - w / 2), canvas.width - 40 - w), int(cy - a.height / 2)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for layer, dx in ((a, 0), (b, a.width - int(size * SLANT))):
        sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        sh.putalpha(layer.getchannel("A").point(lambda v: v * 110 // 255))
        shadow.alpha_composite(sh, (x + dx + 4, y + 5))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(a, (x, y))
    canvas.alpha_composite(b, (x + a.width - int(size * SLANT), y))


k.wordmark = wordmark


# ---- the TOP 4 bar: four cards across the top -----------------------------
# Each 290 wide, 308 apart from x 353 (centred), y 18..134. In each (from
# its left): the red logo box 0..86 / 26..134; the red name bar 92..290 /
# 18..60, the tag white in it; the white panel 86..288 / 60..134 -- WIN RATE
# small in red at +100 / 68..80, the figure under it, and the four status
# triangles at +190..+276 / 92..116 (drawn by the page, as the icons).
TOP4_CARDS = (353, 661, 969, 1277)


def top4():
    c = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    for x in TOP4_CARDS:
        P = lambda pts: [(x + px, py) for px, py in pts]
        logo = P([(0, 26), (82, 24), (88, 134), (22, 134), (0, 113)])
        bar = P([(104, 18), (288, 14), (291, 58), (100, 62), (96, 34)])
        panel = P([(92, 62), (288, 60), (288, 134), (95, 134)])
        for shape in (panel, logo, bar):
            k.shadowed(c, shape, blur=7, alpha=100, dy=4)
        k.paste_shape(c, panel, WHITE)
        k.paste_shape(c, P([(95, 129), (288, 129), (288, 134), (95, 134)]), YELLOW)
        k.paste_shape(c, logo, RED)
        k.paste_shape(c, bar, RED)
        label = slanted("WIN RATE", 13, RED + (255,))
        c.alpha_composite(label, (x + 98, 64))
    c.save(os.path.join(HERE, "Top 4 - BLANK.png"), optimize=True)


if __name__ == "__main__":
    top4()
    k.achievement()
    k.crown_tile()
    frame = k.kit_frame()
    k.kit_zebra()
    k.kit_roundbar()
    booyah_off = [k.band(-40, 360, 640, -40, 130), [(1440, 130), (1660, 130), (1660, 300), (1440, 300)],
                  [(470, 130), (1460, 130), (1460, 238), (470, 238)], [(735, 236), (1030, 236), (1030, 282), (735, 282)]]
    frame_no_bar = k.kit_frame(round_bar=False, save=False)
    booyah_title = ((500, 150, 1440, 234), ["BOOYAH", "TEAM", "STATS"])
    k.ss3_screen("booyah team stats R - BLANK1.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    k.ss3_screen("booyah team stats character R - BLANK.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    k.ss3_screen("head to head R - BLANK.png", frame, [[(470, 125), (1460, 125), (1460, 262), (470, 262)],
                 [(1794, 380), (1920, 380), (1920, 520), (1794, 520)], [(1255, 860), (1400, 860), (1400, 912), (1255, 912)]],
                 title=((495, 144, 1440, 241), ["HEAD", "TO", "HEAD"]))
    k.drop_spot()
    print("written: the Top 4 bar, the in-game cards, the kit (gamercult_frame / gamercult_zebra / "
          "gamercult_roundbar), and the SS3 screens in GamerCult")
