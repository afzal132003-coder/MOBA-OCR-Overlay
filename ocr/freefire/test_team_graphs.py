"""The team graphs' table: per team, per game, keyed as the standings are.

Run: python ocr/freefire/test_team_graphs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as ff

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def team(name, rank, kills, place):
    return {"teamName": name, "rank": rank, "killScore": kills,
            "rankScore": place, "totalScore": kills + place}


def main():
    roster = [{"name": "BFA", "shortName": "BFA"}, {"name": "TSG ARMY", "shortName": "TSG"}]
    g2 = {"timestamp": 200, "teams": [team("TSG ARMY", 1, 12, 12), team("BFA", 2, 5, 9)]}
    g1 = {"timestamp": 100, "teams": [team("BFA", 1, 8, 12)]}
    out = ff.compute_team_graphs([g2, g1], roster)
    by = {t["name"]: t for t in out["teams"]}

    print("\nthe team graphs' table")
    check("games run in series order, by timestamp", out["games"] == ["G1", "G2"])
    check("each game's numbers land in its own column",
          [g and g["total"] for g in by["BFA"]["games"]] == [20, 14])
    check("a team that did not play a game has a gap there, not a zero",
          by["TSG ARMY"]["games"][0] is None and by["TSG ARMY"]["games"][1]["total"] == 24)
    check("short names come from the roster", by["TSG ARMY"]["short"] == "TSG")
    check("kills, placement and rank are all carried",
          by["TSG ARMY"]["games"][1] == {"kills": 12, "place": 12, "total": 24, "rank": 1})

    standings = {r["teamName"]: r for r in ff.compute_freefire_standings([g1, g2], roster)}
    for name, t in by.items():
        total = sum(g["total"] for g in t["games"] if g)
        check("%s: the graphs' total equals the points table's" % name,
              total == standings[name]["totalPoints"], "%s vs %s" % (total, standings[name]["totalPoints"]))

    empty = ff.compute_team_graphs([], roster)
    check("no games: an empty table, not an error", empty == {"games": [], "teams": []})

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
