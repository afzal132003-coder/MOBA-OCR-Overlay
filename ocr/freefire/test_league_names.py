"""A league room's printed team name the roster does not know, on a squad
whose players it does: the row takes the roster team the players belong
to -- its name, short name and logo -- instead of a short name made up from
the printed one. Live, 2026-10-29: "OGxELITE" for the four players filed
under "iQOOxOGxTE" (short OG) went on air as OGXE, with no logo.

Run: python ocr/freefire/test_league_names.py
"""
import copy
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


ROSTER = {"teams": [
    {"name": "iQOOxOGxTE", "shortName": "OG", "logo": "data:image/png;base64,OG",
     "players": [{"ign": "TE.Pawdi", "uid": "147"}, {"ign": "iQOO.OGJOHN", "uid": "107"}]},
    {"name": "S8UL ESPORTS", "shortName": "S8UL", "logo": "data:image/png;base64,S8",
     "players": [{"ign": "S8.ONE", "uid": "201"}, {"ign": "S8.TWO", "uid": "202"}]},
]}


def live_match(names, joins):
    """teamNames {tid: printed}; squads 8 (OG's players, under names the
    roster does not have) and 1 (S8UL's); the operator's pairings."""
    ff._debugger_id_map.update({"801": {"ign": "OG.renamed1", "uid": "147"}, "802": {"ign": "OG.renamed2", "uid": "107"},
                                "101": {"ign": "S8.ONE", "uid": "201"}, "102": {"ign": "S8.TWO", "uid": "202"}})
    return {"teamNames": names, "gsIgns": {8: {"801": "OG.renamed1", "802": "OG.renamed2"},
                                           1: {"101": "S8.ONE", "102": "S8.TWO"}},
            "gsDown": {}, "gsKills": {8: 3, 1: 1}, "wiped": [], "operatorJoins": joins}


def row(linked, tid):
    return next(r for r in linked["rows"] if r["teamId"] == tid)


def main():
    keep = copy.deepcopy(ff._debugger_id_map), copy.deepcopy(ff.server_state.get("aliases"))
    ff.server_state["aliases"] = {"teams": {}, "players": {}, "squads": {}}
    try:
        print("\nthe printed name is not the roster's")
        linked = ff.link_live_teams(live_match({1: "OGxELITE", 2: "S8UL ESPORTS"}, {1: 8}), ROSTER)
        og = row(linked, 1)
        check("the row is the roster team its players are in", og["teamName"] == "iQOOxOGxTE", og["teamName"])
        check("with that team's short name", og["short"] == "OG" and og["shortSource"] == "roster", og["short"])
        check("and its own squad's numbers", og["gsTeam"] == 8 and og["elims"] == 3)
        check("the other team is untouched", row(linked, 2)["teamName"] == "S8UL ESPORTS")

        print("\nthe players' team already has a row by name")
        linked = ff.link_live_teams(live_match({1: "OGxELITE", 2: "iQOOxOGxTE"}, {1: 8}), ROSTER)
        names = [r["teamName"] for r in linked["rows"]]
        check("no team is put on the table twice", names.count("iQOOxOGxTE") == 1, str(names))
        check("the unknown name keeps what it printed", row(linked, 1)["teamName"] == "OGxELITE", str(names))
    finally:
        ff._debugger_id_map.clear(); ff._debugger_id_map.update(keep[0])
        if keep[1] is None:
            ff.server_state.pop("aliases", None)
        else:
            ff.server_state["aliases"] = keep[1]

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
