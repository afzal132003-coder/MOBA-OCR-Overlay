"""A normal room's squads matched to roster teams by who is in them -- by
UID as well as by IGN, since in-game names drift from the registered ones
and a UID does not. Made-up players.

Run: python ocr/freefire/test_squad_uid_match.py
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


ROSTER = {"teams": [
    {"name": "ALPHA", "players": [{"ign": "ALP.One", "uid": "1001"}, {"ign": "ALP.Two", "uid": "1002"},
                                  {"ign": "ALP.Three", "uid": "1003"}]},
    {"name": "BRAVO", "players": [{"ign": "BRV.One", "uid": "2001"}, {"ign": "BRV.Two", "uid": "2002"}]},
]}


def main():
    print("by UID")
    check("in-game names nothing like the sheet: the UIDs still find the team",
          ff.resolve_team_from_igns(["RACS.Someone", "KaRna<26"], ROSTER, ["1001", "1002"]) == "ALPHA")
    check("UIDs outweigh one look-alike IGN",
          ff.resolve_team_from_igns(["ALP.One", "x", "y"], ROSTER, ["9", "2001", "2002"]) == "BRAVO")
    print("unchanged without UIDs")
    check("IGNs alone still match", ff.resolve_team_from_igns(["BRV.One", "BRV.Two"], ROSTER) == "BRAVO")
    check("nothing known: no team", ff.resolve_team_from_igns(["nobody"], ROSTER, ["42"]) == "")
    check("blank UIDs never match a player with no UID",
          ff.resolve_team_from_igns(["x"], {"teams": [{"name": "C", "players": [{"ign": "c", "uid": ""}]}]}, ["", None]) == "")

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
