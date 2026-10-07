"""The event's team sheet, as the organiser exports it to CSV: a row per
team (number, name, short name) and a row per player (IGN, UID, role),
with the drive-link column's multi-line cells and notes in between. Also
the plain "team,short,ign,uid" form. Made-up teams: the real sheet stays
out of this public repo.

Run: python ocr/freefire/test_team_bank.py
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


SHEET = ''',,,,,,,,,,,,,,,,,
1,ALPHA ESPORT,ALP,LIVE IGN,UID,ROLE,,,PLAYER PICTURES AND TEAM PHOTO DRIVE LINK - ,,,,UID - LIVE IGN,,,,,LIVE IGN
,Player - 1 - ,,ALP.ONE,1000000001,IGL + NADER ,,,"LINK


",,,,1000000001 - ALP.ONE,,- NOT NEEDED,,,ALP.ONE
,Player - 2 - ,,ALP.TWO,1000000002,RUSHER,,,,,,,1000000002 - ALP.TWO,,,,,ALP.TWO
,Player - 3 - ,,ALP.THREE!,,SUPPORT,,,,,,, - ALP.THREE!,,,,,ALP.THREE!
,,,,,,,,,,,,,,,,,
2,BRAVO GAMING ,BRV,,,,,,PLAYER PICTURES AND TEAM PHOTO DRIVE LINK - ,,,, - ,,,,,
,Player - 1 - ,,BRV.Red,2000000001,Sniper,,,"( ELIMINATED )
",,,,2000000001 - BRV.Red,,,,,BRV.Red
,Player - 2 - ,,BRV.Blue,2000000002,Rusher,,,,,,,2000000002 - BRV.Blue,,,,,BRV.Blue
,Player - 5 - ,,,,,,,,,,, - ,,,,,
,,,,,,,,,,,,#REF!,,,,,
'''


def main():
    print("the organiser's sheet")
    teams = ff.parse_team_sheet(SHEET)
    check("two teams, in order", [t["name"] for t in teams] == ["ALPHA ESPORT", "BRAVO GAMING"],
          str([t["name"] for t in teams]))
    check("short names", [t["shortName"] for t in teams] == ["ALP", "BRV"])
    check("players with IGN, UID and role",
          teams[0]["players"][0] == {"ign": "ALP.ONE", "uid": "1000000001", "role": "IGL + NADER"},
          str(teams[0]["players"][0]))
    check("a player with no UID is kept, UID blank",
          teams[0]["players"][2]["ign"] == "ALP.THREE!" and teams[0]["players"][2]["uid"] == "")
    check("an empty player row is skipped", len(teams[1]["players"]) == 2)
    check("a team marked ELIMINATED carries the note", teams[1]["note"] == "ELIMINATED" and teams[0]["note"] == "")
    check("the multi-line link cell does not become a player", len(teams[0]["players"]) == 3)

    print("plain rows")
    plain = ff.parse_team_sheet("team,short,ign,uid\nALPHA,ALP,A.One,11\nALPHA,ALP,A.Two,12\nBRAVO,BRV,B.One,21\n")
    check("grouped by team", [(t["name"], t["shortName"], len(t["players"])) for t in plain]
          == [("ALPHA", "ALP", 2), ("BRAVO", "BRV", 1)], str(plain))
    three = ff.parse_team_sheet("ALPHA,A.One,11\nALPHA,A.Two,\n")
    check("team,ign,uid", [p["uid"] for p in three[0]["players"]] == ["11", ""], str(three))
    check("nothing in, nothing out", ff.parse_team_sheet("") == [])

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
