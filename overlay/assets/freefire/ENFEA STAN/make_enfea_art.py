"""ENFEA STAN's cards that the event's own art does not include, drawn in
its look.

The package ships the alive table (STANFEST and JIBITO plates on it), the
four-card Top 4 bar and the ELIMINATED pop-up, in RNX's family -- the same
designer's kit, the same Winner Sans -- so the rest is drawn with the RNX
generator (make_rnx_art.py: SS3's geometry for the side pop-up, the Champion
Rush tile, the post-match kit and its screens) in ENFEA's palette, sampled
from its art:

    rows           indigo        #180044   (24, 0, 68)
    cards          purple        #33096b   (51, 9, 107)
    accent         gold          #ffcc00   (the cards; the table's is #fcd02c)
    ink on gold    violet        #510a66   (the kills, the alive bars)

and its own branding where RNX writes a wordmark: the STANFEST plate (top
right of the frame) and the JIBITO plate (the footer), both cut from its
alive table. Also its table with a twelfth row, the legend and JIBITO
going down one row with it.

    python "overlay/assets/freefire/ENFEA STAN/make_enfea_art.py"
"""
import os
import sys
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "RNX"))
import make_rnx_art as k  # noqa: E402

k.HERE = HERE
k.KIT_NAME = "enfea"
k.PANEL = (51, 9, 107)
k.DEEP = (18, 0, 46)
k.CARD_A, k.CARD_B = (64, 16, 130), (28, 4, 72)
k.GOLD = (255, 204, 0)
k.GOLD_DEEP = (250, 178, 0)
k.VIOLET = (81, 10, 102)
k.SHEEN = (150, 100, 230)

TABLE = Image.open(os.path.join(HERE, "ALIVE STATS - BLANK.png")).convert("RGBA")
STANFEST = TABLE.crop((1676, 245, 1920, 303))      # 244 x 58
JIBITO = TABLE.crop((1801, 940, 1920, 970))        # 119 x 30


def wordmark(canvas, cx, cy, size=None):
    """The event's own plate where RNX writes its name: STANFEST, 1.25x."""
    p = STANFEST.resize((int(STANFEST.width * 1.25), int(STANFEST.height * 1.25)), Image.LANCZOS)
    canvas.alpha_composite(p, (int(cx - p.width / 2), int(cy - p.height / 2)))


k.wordmark = wordmark
_frame = k.kit_frame


def kit_frame(round_bar=True, save=True):
    c = _frame(round_bar=round_bar, save=False)
    # JIBITO at the footer's right end
    c.alpha_composite(JIBITO, (1880 - JIBITO.width, 963 - JIBITO.height // 2))
    if save:
        c.save(os.path.join(k.KIT, "enfea_frame.png"), optimize=True)
    return c


k.kit_frame = kit_frame


# Eleven 39px rows from y 351 on a 49px pitch; the legend at y 894..919 and
# JIBITO at 940..969 under them. The twelfth row is row eleven's strip one
# pitch lower, and everything under it goes down with it.
def table_12():
    out = TABLE.copy()
    ImageDraw.Draw(out).rectangle([0, 883, 1919, 1079], fill=(0, 0, 0, 0))
    out.alpha_composite(TABLE.crop((1580, 842, 1920, 891)), (1580, 891))
    out.alpha_composite(TABLE.crop((1580, 893, 1920, 975)), (1580, 942))
    out.save(os.path.join(HERE, "ALIVE STATS - BLANK 12.png"), optimize=True)


if __name__ == "__main__":
    table_12()
    k.achievement()
    k.crown_tile()
    frame = kit_frame()
    k.kit_zebra()
    k.kit_roundbar()
    booyah_off = [k.band(-40, 360, 640, -40, 130), [(1440, 130), (1660, 130), (1660, 300), (1440, 300)],
                  [(470, 130), (1460, 130), (1460, 238), (470, 238)], [(735, 236), (1030, 236), (1030, 282), (735, 282)]]
    frame_no_bar = kit_frame(round_bar=False, save=False)
    booyah_title = ((500, 150, 1440, 234), ["BOOYAH", "TEAM", "STATS"])
    k.ss3_screen("booyah team stats R - BLANK1.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    k.ss3_screen("booyah team stats character R - BLANK.png", frame_no_bar, booyah_off, title=booyah_title, bar=(746, 240, 1018, 275))
    k.ss3_screen("head to head R - BLANK.png", frame, [[(470, 125), (1460, 125), (1460, 262), (470, 262)],
                 [(1794, 380), (1920, 380), (1920, 520), (1794, 520)], [(1255, 860), (1400, 860), (1400, 912), (1255, 912)]],
                 title=((495, 144, 1440, 241), ["HEAD", "TO", "HEAD"]))
    k.drop_spot()
    print("written with %s: the 12-row table, the in-game cards, the kit (enfea_frame / enfea_zebra / "
          "enfea_roundbar), and the SS3 screens in ENFEA" % os.path.basename(k.FONT))
