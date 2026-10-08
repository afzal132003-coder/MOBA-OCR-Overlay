"""Loadout icon matching: captures made the way the HUD draws them -- the
game's portrait on a coloured panel, a dark skill strip along the bottom,
shrunk to HUD size, a little noise -- come back as the right character;
an empty box says it holds no icon; a hand correction is learned.

Run: python ocr/freefire/test_icon_match.py
"""
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
import freefire_engine as ff

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def hud_capture(label, panel, size=(48, 54), strip=True, seed=0):
    """The portrait as a passive slot shows it."""
    ref = cv2.imread(str(ff.FREEFIRE_ASSETS_DIR / (label + ".png")), cv2.IMREAD_UNCHANGED)
    a = ref[..., 3:4].astype(np.float32) / 255.0
    img = ref[..., :3].astype(np.float32) * a + np.array(panel, np.float32) * (1 - a)
    w, h = size
    img = cv2.resize(img, (w, w), interpolation=cv2.INTER_AREA)
    out = np.zeros((h, w, 3), np.float32) + np.array(panel, np.float32)
    out[:w] = img[:h] if w > h else img
    if strip:
        out[int(h * 0.82):] = (40, 25, 20)
    rng = np.random.default_rng(seed)
    out += rng.normal(0, 4, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def main():
    refs, _ = ff.load_icon_refs("characters")
    names = {k: v[1] for k, v in refs.items()}
    # distinct, named characters only (the dump has a few duplicates)
    pick = [k for k, n in names.items() if n and not n.isdigit() and n.lower() not in ("nil", "ni")]
    random.Random(7).shuffle(pick)
    print("the game's portraits, as the HUD draws them")
    right, trusted = 0, 0
    panels = [(80, 110, 200), (90, 150, 90), (120, 100, 90)]
    for i, label in enumerate(pick[:24]):
        got = ff.match_icon(hud_capture(label, panels[i % 3], seed=i), "characters")
        if (got.get("name") or "").upper() == names[label].upper():
            right += 1
            trusted += 0 if got["lowConfidence"] else 1
    check("24 characters, every one named right", right == 24, "%d/24" % right)
    check("and trusted (not flagged)", trusted >= 22, "%d/24 trusted" % trusted)

    print("no icon in the box")
    blank = (np.zeros((54, 48, 3)) + (34, 30, 38)).astype(np.uint8)
    got = ff.match_icon(blank, "characters")
    check("an empty panel names nobody", got["label"] is None and "no icon" in got.get("reason", ""), str(got))

    print("a hand correction is learned")
    tmp = tempfile.mkdtemp()
    old_dir = ff.LEARNED_ICONS_DIR
    try:
        ff.LEARNED_ICONS_DIR = ff.Path(tmp)
        crop = hud_capture(pick[0], (60, 60, 160), seed=99)
        p = os.path.join(tmp, "g1_t0_p0_passive1.png")
        cv2.imwrite(p, crop)
        check("the crop is kept", ff.learn_icon("characters", pick[1], p))
        refs2, learned = ff.load_icon_refs("characters")
        check("and read back as a reference", pick[1] in learned)
        again = ff.match_icon(crop, "characters")
        check("the same capture now matches the corrected character",
              (again.get("name") or "").upper() == names[pick[1]].upper(), str(again.get("name")))
    finally:
        ff.LEARNED_ICONS_DIR = old_dir
        ff._icon_ref_cache.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (checks, len(failures)))
    sys.exit(1 if failures else 0)


main()
