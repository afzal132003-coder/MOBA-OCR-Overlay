"""The 3D map's engine half (map3d.py): the ground learned from where
players walk -- the street under the rooftops, the gaps filled toward sea
level -- and one match packed for the page.

Run: python ocr/freefire/test_map3d.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import map3d

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


CAL = {"name": "Test", "image": "FF Maps/test.png", "size": 600, "ax": 0.4, "cx": 300.0, "az": -0.4, "cz": 300.0}


def main():
    n = 20
    print("\nthe ground")
    rng = np.random.default_rng(1)
    # a street at 10 m with rooftops at 40 m over the left half, a hill at
    # 60 m in one cell on the right; nobody walks the far right edge
    u = rng.uniform(0.0, 0.5, 4000)
    v = rng.uniform(0.0, 1.0, 4000)
    h = np.where(rng.uniform(size=4000) < 0.7, 10.0, 40.0)
    hu = np.full(60, 0.6 + 0.5 / n)
    hv = np.full(60, 0.5 + 0.5 / n)
    t = map3d.build_terrain([(u, v, h), (hu, hv, np.full(60, 60.0))], n=n)
    H = np.array(t["h"]).reshape(n, n)
    check("the street, not the rooftops", abs(H[n // 2, n // 4] - 10) < 2, "%.1f" % H[n // 2, n // 4])
    check("a hill where they climbed", H[n // 2, int(0.6 * n)] > 25, "%.1f" % H[n // 2, int(0.6 * n)])
    check("ground nobody walked settles toward sea level, not the hill",
          H[n // 2, n - 1] < H[n // 2, int(0.6 * n)] - 10, "%.1f" % H[n // 2, n - 1])
    check("the share measured", 0.4 < t["coverage"] < 0.6, str(t["coverage"]))
    check("nothing walked: flat", set(map3d.build_terrain([], n=4)["h"]) == {0.0})

    print("\none match for the page")
    decoded = {"players": [
        {"pid": (3 << 24) | 7, "squad": 3, "name": "ALP.ONE", "land": [12.0, 0, 0],
         "t": [0.0, 1.0, 2.0, 4.0, 6.0, 8.0], "x": [0.0, 10, 20, 30, 40, 50], "z": [0.0, 5, 10, 15, 20, 25],
         "y": [0.0, 400, 200, 12, 12, 13]},
    ]}
    view = {"matchId": "9", "mapId": 1, "mapName": "Test", "duration": 900,
            "squads": [{"squad": 3, "teamName": "ALPHA ESPORTS", "place": 1, "kills": 4}],
            "kills": [{"t": 100.0, "ks": 3, "vs": 5, "at": [0.4, 0.5], "from": [0.41, 0.52]}],
            "zones": [{"stage": 0, "at": [0.5, 0.5], "r": 0.3, "announced": 90, "closing": 200}],
            "plane": {"from": [0, 0.3], "to": [1, 0.7]}}
    events = {"kills": [{"t": 100.4, "killer": (3 << 24) | 7, "killerName": "ALP.ONE",
                         "victim": (5 << 24) | 2, "victimName": "BRV.TWO"}]}
    p = map3d.build_payload(view, decoded, events, CAL, {"n": 2, "h": [0, 0, 0, 0], "coverage": 0.5, "matches": 3},
                            squad_names={3: ("ALPHA ESPORTS", "ALP")})
    pl = p["players"][0]
    # the origin (t 0) is not a place; from the first placed sample (t 1),
    # one every two seconds or more: t 2 is skipped, 4 6 8 kept
    check("the track every two seconds, origin left out", pl["t"] == [1, 4, 6, 8], str(pl["t"]))
    check("heights in decimetres", pl["h"][:2] == [4000, 120], str(pl["h"]))
    check("places as fractions x10000", pl["u"][0] == int(round((0.4 * 10 + 300) / 600 * 10000)), str(pl["u"][0]))
    check("the kill named from the replay", p["kills"][0]["kn"] == "ALP.ONE" and p["kills"][0]["vn"] == "BRV.TWO")
    check("the squad under the roster's name and tag", p["squads"][0]["name"] == "ALPHA ESPORTS" and p["squads"][0]["short"] == "ALP")
    check("the map's width in metres", p["map"]["metres"] == 1500.0, str(p["map"]["metres"]))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
