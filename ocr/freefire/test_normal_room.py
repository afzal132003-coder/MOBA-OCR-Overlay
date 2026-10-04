"""A normal room: no team names anywhere, so the players are the identity.

Run it with no arguments:

    python ocr\\freefire\\test_normal_room.py

WHAT THIS GUARDS

A league room writes each team's name into the debugger log and the
result file. A normal room does not, so the only thing tying a result
block to a registered team is who was in it. The engine already matches a
block to the roster by its players' UIDs -- and UID wins over the file's
team name once FREEFIRE_TEAM_UID_MIN_VOTES of them agree. This pins that
it does so when the team name is useless, which is the whole of the
normal-room case, and that a block whose players are not in the roster is
left for the operator rather than guessed.

No real normal-room result file exists on the event rig to test against:
every one of its 136 files carries real team names. So the names here are
the plausible kinds of nothing a normal room could write -- blank, a
number, a generic "Team 3" -- and the test holds for all of them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FREEFIRE_NO_AUTOSTART", "1")
import freefire_engine as ff

checks = 0
failures = []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label,
                          ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


ROSTER = {"teams": [
    {"name": "S8UL ESPORTS", "shortName": "S8UL", "logo": "s8ul.png", "players": [
        {"ign": "S8uL.Bunnyy", "uid": "3460475190"},
        {"ign": "S8uL.Jack07", "uid": "2365077886"},
        {"ign": "S8uL.Noor18", "uid": "984855672"},
        {"ign": "HENRYY", "uid": "1342123787"}]},
    {"name": "RES", "shortName": "RES", "logo": "res.png", "players": [
        {"ign": "Daafiqqq", "uid": "497404894"},
        {"ign": "iQOOGxkrish", "uid": "705416506"},
        {"ign": "SiDAK.07", "uid": "415866186"},
        {"ign": "Ziyann.11", "uid": "320392903"}]},
]}


def block(team_name, rank, players):
    return {"teamName": team_name, "rank": rank, "killScore": 0,
            "rankScore": 0, "totalScore": 0,
            "players": [{"name": n, "uid": u, "kills": 1} for n, u in players]}


def main():
    print("\na result block with no usable team name")
    for junk in ("", "3", "Team 3", "SQUAD 7"):
        teams = ff.apply_roster_overrides([
            block(junk, 1, [("Bunnyy", "3460475190"), ("Jack", "2365077886"),
                            ("Noor", "984855672"), ("Henry", "1342123787")])], ROSTER)
        t = teams[0]
        check("named %-10r -> resolved by its players' UIDs" % junk,
              t["matched"] and t["teamName"] == "S8UL ESPORTS",
              "%s / matched=%s" % (t["teamName"], t["matched"]))

    print("\nwhat a resolved block carries")
    t = ff.apply_roster_overrides([
        block("", 1, [("a", "497404894"), ("b", "705416506")])], ROSTER)[0]
    check("the roster team's name", t["teamName"] == "RES", t["teamName"])
    check("its short name and logo, for the graphics",
          t.get("shortName") == "RES" and t.get("logo") == "res.png")
    check("and the file's own word for it, kept for the review",
          t.get("fileTeamName") == "")

    print("\na block nobody can place")
    t = ff.apply_roster_overrides([
        block("Team 9", 4, [("x", "111"), ("y", "222"), ("z", "333")])], ROSTER)[0]
    check("is left unmatched for the operator, not guessed",
          t["matched"] is False, "matched=%s" % t["matched"])

    print("\none UID is not enough on its own")
    # A single player who happens to be on the roster must not drag a
    # block of strangers onto that team: the threshold exists for this.
    t = ff.apply_roster_overrides([
        block("", 5, [("Bunnyy", "3460475190"), ("x", "111"), ("y", "222")])], ROSTER)[0]
    check("one matching UID does not settle it below the threshold",
          (not t["matched"]) or t.get("needsUidReview"),
          "matched=%s review=%s" % (t["matched"], t.get("needsUidReview")))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
