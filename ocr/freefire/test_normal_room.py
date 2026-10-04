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

    print("\nthe alive table in a normal room")
    # Every row used to be built from the client's team-name narration,
    # with squads only ever joined on as the second half. A normal room
    # writes no names, so the table came out EMPTY. The squads are in the
    # log in every room, and the roster names them by their players.
    live = {
        "teamNames": {},                      # a normal room: no names at all
        "gsIgns": {3: {"301": "S8uL.Bunnyy", "302": "S8uL.Jack07",
                       "303": "S8uL.Noor18", "304": "HENRYY"},
                   7: {"701": "Daafiqqq", "702": "iQOOGxkrish",
                       "703": "SiDAK.07", "704": "Ziyann.11"},
                   9: {"901": "stranger1", "902": "stranger2"}},
        "gsDown": {3: {"301"}, 7: set()},
        "gsKills": {3: 6, 7: 2, 9: 1},
        "wiped": [9],
    }
    roster = {"teams": [
        {"name": "S8UL ESPORTS", "shortName": "S8UL", "players": [
            {"ign": "S8uL.Bunnyy"}, {"ign": "S8uL.Jack07"},
            {"ign": "S8uL.Noor18"}, {"ign": "HENRYY"}]},
        {"name": "RES", "shortName": "RES", "players": [
            {"ign": "Daafiqqq"}, {"ign": "iQOOGxkrish"},
            {"ign": "SiDAK.07"}, {"ign": "Ziyann.11"}]}]}

    keep = dict(ff.server_state.get("event") or {})
    ff.server_state["event"] = dict(keep, roomType="normal")
    rows = {r["teamName"]: r for r in ff.link_live_teams(live, roster)["rows"]}
    check("every squad gets a row", len(rows) == 3, str(sorted(rows)))
    s8 = rows.get("S8UL ESPORTS") or {}
    check("named from the roster by its players", bool(s8), str(sorted(rows)))
    check("with its own kills", s8.get("elims") == 6, str(s8.get("elims")))
    check("and its players down: 3 of 4 alive", s8.get("aliveCount") == 3,
          str(s8.get("aliveCount")))
    check("its short name from the roster", s8.get("short") == "S8UL",
          str(s8.get("short")))
    res = rows.get("RES") or {}
    check("a full squad shows four alive", res.get("aliveCount") == 4,
          str(res.get("aliveCount")))
    odd = rows.get("SQUAD 9") or {}
    check("a squad nobody claims still gets its line, plainly unassigned",
          bool(odd), str(sorted(rows)))
    check("and a wiped squad is out, with its finishing place",
          odd.get("eliminated") is True and odd.get("placement") == 3,
          "%s / %s" % (odd.get("eliminated"), odd.get("placement")))

    print("\na league room is exactly as it was")
    ff.server_state["event"] = dict(keep, roomType="league")
    league = ff.link_live_teams(live, roster)["rows"]
    check("no names in the log means no rows, as before",
          league == [], "%d rows" % len(league))
    # Built from the saved event with the field REMOVED, not copied as
    # is: the operator's own saved event already said "normal" when this
    # was first run, and a copy of it is not an unset room type.
    unset_event = {k: v for k, v in keep.items() if k != "roomType"}
    ff.server_state["event"] = unset_event
    unset = ff.link_live_teams(live, roster)["rows"]
    check("and an event with no room type set is a league room",
          unset == [], "%d rows" % len(unset))
    ff.server_state["event"] = keep

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
