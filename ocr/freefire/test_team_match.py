"""Team names against the roster: a short tag must not claim a longer
name it merely sits inside.

Seen live: a roster with NG PROS (tag NG) and LORD ESPORTZ (tag LE) named
the lobby's KING, TeamLegacy and S8UL ESPORTS after them -- "NG" is inside
"KING", "LE" inside "TEAMLEGACY" and "S8ULESPORTS" -- and their points
would have gone to those teams.

Run: python ocr/freefire/test_team_match.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as ff

ROSTER = [{"name": n, "shortName": s, "players": []} for n, s in (
    ("NG PROS", "NG"), ("LORD ESPORTZ", "LE"), ("TSG ARMY", "TSGA"),
    ("CLUTZA ESPORTS", "CTZ"), ("TEAM EVOLUTION", "TEV"), ("IQOOxTG", "TG"),
    ("ASIN", "ASIN"), ("MISS ASSASSIN", "MA"), ("TEAM SMR", "SMR"))]

CASES = [
    # not in the roster: left alone
    ("KING", None), ("TeamLegacy", None), ("S8UL ESPORTS", None),
    # a tag as a whole word, or on its own
    ("NG", "NG PROS"), ("LE", "LORD ESPORTZ"), ("NG PROS", "NG PROS"),
    ("TSGA", "TSG ARMY"), ("CTZ", "CLUTZA ESPORTS"), ("TEV", "TEAM EVOLUTION"),
    # misreads and partial names still land
    ("CT2", "CLUTZA ESPORTS"), ("TEAM EVO", "TEAM EVOLUTION"), ("LORD ESPORT", "LORD ESPORTZ"),
    ("NG PRO", "NG PROS"), ("SMR", "TEAM SMR"),
    # the earlier fix: a whole name beats a fragment of it
    ("ASIN", "ASIN"), ("MISS ASSASIN", "MISS ASSASSIN"),
]


def main():
    ff.server_state["aliases"] = {}
    failures = 0
    for name, want in CASES:
        got = ff.match_roster_team(name, ROSTER)
        got = got and got.get("name")
        ok = got == want
        failures += not ok
        print("  %s  %-14s -> %s%s" % ("PASS" if ok else "FAIL", name, got, "" if ok else "   (want %s)" % want))
    print("\n%d checks, %d failed" % (len(CASES), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
