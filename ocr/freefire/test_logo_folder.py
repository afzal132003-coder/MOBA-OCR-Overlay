"""The team logo folder: a file named after a team (full or short name,
case and punctuation ignored) is its logo, wins over an uploaded one, and
a replaced file updates it. Made-up teams and images.

Run: python ocr/freefire/test_logo_folder.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as ff
from PIL import Image

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def img(path, colour):
    Image.new("RGBA", (64, 64), colour).save(path)


def main():
    d = tempfile.mkdtemp()
    img(os.path.join(d, "Alpha Esports.png"), (255, 0, 0, 255))
    img(os.path.join(d, "brv.png"), (0, 255, 0, 255))
    ff.server_state.setdefault("settings", {})["teamLogoFolder"] = d
    ff.server_state["settings"]["playerPhotoFolder"] = ""
    ff.server_state["roster"] = {"teams": [
        {"name": "ALPHA ESPORTS", "shortName": "ALP", "logo": ""},
        {"name": "BRAVO GAMING", "shortName": "BRV", "logo": "data:image/png;base64,UPLOADED"},
        {"name": "CHARLIE", "shortName": "CHR", "logo": "data:image/png;base64,KEEP"},
    ]}
    print("the team logo folder")
    check("the roster changed", ff.fill_roster_logos() is True)
    t = ff.server_state["roster"]["teams"]
    check("matched on the full name, ignoring case and spaces", t[0]["logo"].startswith("data:image/png;base64,"))
    check("matched on the short name, and wins over an uploaded logo",
          t[1]["logo"].startswith("data:image/png;base64,") and "UPLOADED" not in t[1]["logo"])
    check("a team with no file keeps its own logo", t[2]["logo"].endswith("KEEP"))
    check("nothing changed, nothing reported", ff.fill_roster_logos() is False)
    before = t[0]["logo"]
    time.sleep(1.1)
    img(os.path.join(d, "Alpha Esports.png"), (0, 0, 255, 255))
    os.utime(os.path.join(d, "Alpha Esports.png"))
    check("a replaced file updates the logo", ff.fill_roster_logos() is True and t[0]["logo"] != before)
    ff.server_state["settings"]["teamLogoFolder"] = os.path.join(d, "missing")
    check("a missing folder is not an error", ff.fill_roster_logos() is False)
    print("\n%d checks, %d failed" % (checks, len(failures)))
    sys.exit(1 if failures else 0)


main()
