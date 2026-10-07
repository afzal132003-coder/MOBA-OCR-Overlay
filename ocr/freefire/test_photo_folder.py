"""The photo folder laid out per team: "UID.png" player photos in a folder
per squad, each with its Team_Photo.png, and a DEFAULT folder whose
pictures stand in for a player with no photo and whose Team_Photo stands
in for a squad without one.

Run: python ocr/freefire/test_photo_folder.py
"""
import base64
import io
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


def colour_of(data_url):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGBA").getpixel((5, 5))[:3]


def near(a, b, tol=6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def put(folder, name, colour, size=(123, 142)):
    os.makedirs(folder, exist_ok=True)
    Image.new("RGBA", size, colour + (255,)).save(os.path.join(folder, name))


def main():
    keep = (dict(ff.server_state.get("settings") or {}), ff.server_state.get("roster"))
    tmp = tempfile.mkdtemp()
    # a squad whose folder is not named like the roster team
    put(os.path.join(tmp, "TSG ARMY"), "2451778281.png", (255, 0, 0))
    put(os.path.join(tmp, "TSG ARMY"), "134463391.png", (0, 255, 0))
    put(os.path.join(tmp, "TSG ARMY"), "Team_Photo.png", (0, 0, 255), (832, 274))
    # a squad known only by IGN-named files, with its picture called Team_Name
    put(os.path.join(tmp, "iQOO TOTAL GAMING ESP"), "iQOO.TG.MAFIA.png", (255, 255, 0))
    put(os.path.join(tmp, "iQOO TOTAL GAMING ESP"), "iQOO.TG.WOTA.png", (255, 255, 1))
    put(os.path.join(tmp, "iQOO TOTAL GAMING ESP"), "Team_Name.png", (0, 255, 255), (832, 274))
    # a squad with only its folder name to go on
    put(os.path.join(tmp, "NEBULA ESPORTS"), "Team_Photo.png", (10, 20, 30), (832, 274))
    # the stand-ins
    for i, c in enumerate([(1, 1, 1), (2, 2, 2), (3, 3, 3), (4, 4, 4)], 1):
        put(os.path.join(tmp, "DEFAULT"), "PIC%d.png" % i, c)
    put(os.path.join(tmp, "DEFAULT"), "Team_Photo.png", (9, 9, 9), (832, 274))
    try:
        ff.server_state.setdefault("settings", {})["playerPhotoFolder"] = tmp
        ff.server_state["roster"] = {"teams": [
            {"name": "TSG PROS", "shortName": "TSGP", "players": [
                {"ign": "TSGP.BLOOD14", "uid": "2451778281"}, {"ign": "TSGP.ARJUN", "uid": "134463391"},
                {"ign": "TSGP.SOMI7", "uid": "1691987383"}, {"ign": "TSGP.STRUGGLE", "uid": "612273961"}]},
            {"name": "iQOO TOTAL GAMING ESP", "shortName": "iQOO.TG", "players": [
                {"ign": "iQOO.TG.MAFIA", "uid": ""}, {"ign": "iQOO.TG.WOTA", "uid": ""}]},
            {"name": "NEBULA ESPORTS", "shortName": "NB", "players": [{"ign": "NB.PATLU", "uid": "1477488575"}]},
            {"name": "NO FOLDER", "shortName": "NF", "players": [{"ign": "NF.ONE", "uid": "42"}]},
        ]}
        ff._photo_index["folder"] = None

        print("player photos, in team folders")
        check("by UID inside a team folder", colour_of(ff._photo_lookup("2451778281", "")) == (255, 0, 0))
        check("by IGN when the file has no UID", colour_of(ff._photo_lookup("", "iQOO.TG.MAFIA")) == (255, 255, 0))
        check("the file path, as the sheet is sent it",
              str(ff._photo_path("2451778281", "")).endswith(os.path.join("TSG ARMY", "2451778281.png")))
        check("a Team_Photo is never taken for a player", ff._photo_index["ign"].get("teamphoto") is None)

        print("no photo of their own: the DEFAULT pictures, by roster slot")
        third = ff._photo_lookup("1691987383", "TSGP.SOMI7")
        fourth = ff._photo_lookup("612273961", "TSGP.STRUGGLE")
        check("slot 3 gets PIC3", colour_of(third) == (3, 3, 3), str(colour_of(third)))
        check("slot 4 gets PIC4", colour_of(fourth) == (4, 4, 4), str(colour_of(fourth)))
        stranger = ff._photo_lookup("999999", "Somebody")
        check("someone off the roster still gets one, steadily",
              stranger and stranger == ff._photo_lookup("999999", "Somebody"))
        check("nobody at all gets nothing", ff._photo_lookup("", "") == "")

        print("team photos")
        tp = ff.team_photos()
        check("folder found by its players' UIDs (TSG ARMY -> TSG PROS)",
              near(colour_of(tp.get("TSG PROS", "")), (0, 0, 255)) if tp.get("TSG PROS") else False)
        check("Team_Name.png read as the team photo, folder found by IGNs",
              bool(tp.get("iQOO TOTAL GAMING ESP")) and near(colour_of(tp["iQOO TOTAL GAMING ESP"]), (0, 255, 255)),
              str(tp.get("iQOO TOTAL GAMING ESP") and colour_of(tp["iQOO TOTAL GAMING ESP"])))
        check("folder found by name when no player matches",
              bool(tp.get("NEBULA ESPORTS")) and near(colour_of(tp["NEBULA ESPORTS"]), (10, 20, 30)),
              str(tp.get("NEBULA ESPORTS") and colour_of(tp["NEBULA ESPORTS"])))
        check("a team with no folder is left to the default", "NO FOLDER" not in tp)
        check("the DEFAULT folder's team photo is sent as __default__",
              bool(tp.get("__default__")) and near(colour_of(tp["__default__"]), (9, 9, 9)))
        raw = base64.b64decode(tp["TSG PROS"].split(",", 1)[1])
        check("sent at the eliminated strip's size (416 wide, WebP)",
              Image.open(io.BytesIO(raw)).size == (416, 137) and tp["TSG PROS"].startswith("data:image/webp"))
        print("team logos")
        put(os.path.join(tmp, "TSG ARMY"), "Logo.png", (200, 10, 10), (400, 300))
        ff._photo_index["folder"] = None
        check("a Logo.png is not taken for a player", ff._photo_index["ign"].get("logo") is None)
        ff.server_state["roster"]["teams"][2]["logo"] = "data:image/png;base64,UPLOADED"
        check("a blank logo is filled from the team folder", ff.fill_roster_logos() is True)
        tsg = ff.server_state["roster"]["teams"][0]
        raw = base64.b64decode(tsg["logo"].split(",", 1)[1])
        im = Image.open(io.BytesIO(raw))
        check("fitted inside 200x200, aspect kept", im.size == (200, 150), str(im.size))
        check("an uploaded logo is never replaced",
              ff.server_state["roster"]["teams"][2]["logo"] == "data:image/png;base64,UPLOADED")
        check("nothing new to fill: no change reported", ff.fill_roster_logos() is False)
        ff.server_state["settings"]["playerPhotoFolder"] = ""
        check("no folder set: no team photos", ff.team_photos() == {})
    finally:
        ff.server_state["settings"] = keep[0]
        ff.server_state["roster"] = keep[1]
        ff._photo_index["folder"] = None

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
