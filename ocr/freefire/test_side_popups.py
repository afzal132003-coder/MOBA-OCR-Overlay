"""Side pop-ups: first blood, kill leader, rampage, squad wipe, clutch,
headshot, teams left -- detected from the kill narration, each only when
switched on, and the player's photo found in a folder of "UID - IGN" files.

Run: python ocr/freefire/test_side_popups.py
"""
import os
import sys
import tempfile

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


def kill(t, killer, kuid, kteam, victim, vuid, vteam, headshot=False):
    return {"type": "kill", "time": "2026-10-07 20:00:%06.3f" % t, "killerIgn": killer,
            "killerUid": kuid, "killerTeam": kteam, "victimIgn": victim, "victimUid": vuid,
            "victimTeam": vteam, "headshot": headshot}


def main():
    keep = (dict(ff.server_state.get("settings") or {}),
            list((ff.server_state.get("liveOps") or {}).get("sidetableRows") or []))
    tmp = tempfile.mkdtemp()
    Image.new("RGBA", (200, 400), (255, 0, 0, 255)).save(os.path.join(tmp, "1001 - Ace.png"))
    Image.new("RGBA", (200, 400), (0, 0, 255, 255)).save(os.path.join(tmp, "OnlyIgn.png"))
    try:
        ff.server_state["liveOps"]["sidetableRows"] = [
            {"gsTeam": 1, "teamName": "ALPHA", "short": "ALP"},
            {"gsTeam": 2, "teamName": "BRAVO", "short": "BRV"}]
        live = {"gsIgns": {1: {"a": "Ace", "b": "B1", "c": "C1", "d": "D1"},
                           2: {"e": "E2", "f": "F2", "g": "G2", "h": "H2"},
                           3: {"i": "I3"}},
                "gsDown": {}, "wiped": []}

        print("\nnothing switched on, nothing announced")
        ff.server_state["settings"] = dict(keep[0], sidePopups={})
        got = ff.detect_side_popups([kill(1, "Ace", "1001", 1, "E2", "2001", 2)], live)
        check("no switches: no pop-ups", got == [])

        print("\nswitched on")
        ff.server_state["settings"] = dict(keep[0], playerPhotoFolder=tmp, sidePopups={
            k: True for k in ff.SIDE_POPUP_TYPES})
        live2 = {"gsIgns": live["gsIgns"], "gsDown": {}, "wiped": []}
        seq, kinds = [], []
        victims = [("E2", "2001"), ("F2", "2002"), ("G2", "2003"), ("H2", "2004")]
        for i, (v, vu) in enumerate(victims):
            live2["gsDown"].setdefault(2, set()).add(v)
            if i == 3:
                live2["wiped"].append(2)
            got = ff.detect_side_popups([kill(2 + i * 5, "Ace", "1001", 1, v, vu, 2,
                                              headshot=(i == 0))], live2)
            kinds.append([p["type"] for p in got])
            seq += got
        check("the first kill is first blood (and its headshot)",
              kinds[0] == ["firstBlood", "headshot"], str(kinds[0]))
        check("a third kill inside 30s is a rampage, and a new kill leader",
              "rampage" in kinds[2] and "killLeader" in kinds[2], str(kinds[2]))
        check("the fourth extends the rampage; the leader is not re-announced",
              "rampage" in kinds[3] and "killLeader" not in kinds[3], str(kinds[3]))
        check("one player taking all four of a squad is a solo wipe",
              "soloWipe" in kinds[3], str(kinds[3]))
        fb = seq[0]
        check("the card names the team the killer's squad is",
              fb["teamName"] == "ALPHA" and fb["ign"] == "Ace", str(fb)[:120])
        check("the player's photo comes from the folder by UID",
              fb["photo"].startswith("data:image/png;base64,"))
        check("and by IGN when the file has no UID",
              ff._photo_lookup("", "OnlyIgn").startswith("data:image/png;base64,"))
        check("a player with no file gets no photo, not an error",
              ff._photo_lookup("999", "Nobody") == "")

        print("\nclutch and teams left")
        live3 = {"gsIgns": {1: {"a": "Ace", "b": "B1"}, 2: {"e": "E2"}}, "gsDown": {1: {"B1"}}, "wiped": []}
        got = ff.detect_side_popups([kill(1, "Ace", "1001", 1, "E2", "2001", 2)], live3)
        check("the last man standing taking a kill is a clutch",
              "clutch" in [p["type"] for p in got], str([p["type"] for p in got]))
        many = {"gsIgns": {i: {"x%d" % i: "P%d" % i} for i in range(1, 13)}, "gsDown": {},
                "wiped": list(range(1, 9))}
        got = ff.detect_side_popups([kill(1, "Ace", "1001", 1, "P1", "9", 9)], many)
        tl = [p for p in got if p["type"] == "teamsLeft"]
        check("eight wipes of twelve at once: ONE announcement, with the count left",
              [p["value"] for p in tl] == [4], str([p["value"] for p in tl]))
        again = ff.detect_side_popups([kill(9, "Ace", "1001", 1, "P2", "8", 8)], many)
        check("and never the same mark twice", not [p for p in again if p["type"] == "teamsLeft"])
    finally:
        ff.server_state["settings"] = keep[0]
        ff.server_state["liveOps"]["sidetableRows"] = keep[1]

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
