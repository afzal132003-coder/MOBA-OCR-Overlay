"""Read Lobby From Live Game: every player once, in the squad they joined
last, and each squad placed by the best evidence there is -- the roster's
UIDs, the Team Bank's, the running match, the players' tags -- and never
under the wrong team.

Run: python ocr/freefire/test_live_lobby.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as E

E.save_state = lambda *a, **k: None
checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def pid(squad, slot):
    return (squad << E.GS_TEAM_SHIFT) | slot


T = "[2026-10-11 15:00:%02d.000][1][1] "
LINES = []


def join(sec, squad, slot, uid, ign):
    LINES.append(T % sec + "Player Join, %s, %d, %s,False%d" % (uid, pid(squad, slot), ign, squad))
    LINES.append(T % sec + "[UIModelSpectator] AddPlayer id%d,name%s,gsTeam%d" % (pid(squad, slot), ign, squad))


# three squads; NB.TWO starts in squad 3 and moves to squad 2
join(1, 1, 1, "101", "DG.ONE"); join(1, 1, 2, "102", "DG.TWO")
join(2, 2, 1, "201", "NB.ONE"); join(3, 3, 9, "202", "NB.TWO")
join(3, 3, 1, "301", "WS.ONE"); join(3, 3, 2, "302", "WS.TWO")
join(9, 2, 2, "202", "NB.TWO")                       # the move
for k, (name, tid) in enumerate((("DG", 1), ("NBE", 2), ("WSQ", 3))):
    LINES.append(T % (20 + k) + "OnTeamScoreInited -> TeamName: %s TeamID: %d" % (name, tid))


def main():
    path = Path(tempfile.gettempdir()) / "ff_live_lobby.log"
    io.open(path, "w", encoding="utf-8").write("\n".join(LINES) + "\n")
    found = E.roster_from_debugger_log(path)
    squads, names = found["squads"], found["names"]
    by_gs = {s["gsTeam"]: [p["ign"] for p in s["players"]] for s in squads}

    print("every player once, in the squad they joined last")
    check("the player who moved is in the squad they moved to", "NB.TWO" in by_gs.get("2", []), str(by_gs))
    check("and not still in the one they left", "NB.TWO" not in by_gs.get("3", []), str(by_gs))
    check("six players, not seven", sum(len(v) for v in by_gs.values()) == 6)
    check("the printed names", names == ["DG", "NBE", "WSQ"], str(names))

    roster_uids = [{"name": "DG", "players": [{"uid": "101"}, {"uid": "102"}]},
                   {"name": "NBE", "players": [{"uid": "201"}, {"uid": "202"}]},
                   {"name": "WSQ", "players": [{"uid": "301"}, {"uid": "302"}]}]
    bare = [{"name": t["name"], "players": []} for t in roster_uids]
    bank = [{"name": "DESI GAMERS ESP", "players": [{"uid": "101"}, {"uid": "102"}]},
            {"name": "NEBULA ESPORTS", "players": [{"uid": "201"}, {"uid": "202"}]}]

    print("placed by the roster's UIDs")
    got = E.place_lobby_squads(squads, names, roster_uids)
    check("every squad, exactly", {g: v[0] for g, v in got.items()} == {"1": "DG", "2": "NBE", "3": "WSQ"}, str(got))
    check("said how", {v[1] for v in got.values()} == {"uid"})

    print("placed by the Team Bank, under the name the game printed")
    got = E.place_lobby_squads(squads, names, bare, bank)
    check("DESI GAMERS ESP is DG, NEBULA ESPORTS is NBE",
          got.get("1") == ("DG", "bank") and got.get("2") == ("NBE", "bank"), str(got))
    check("never the bank's own spelling (that would be a second DG)",
          not any(v[0] in ("DESI GAMERS ESP", "NEBULA ESPORTS") for v in got.values()), str(got))
    check("a squad nothing knows is left blank, not guessed", "3" not in got or got["3"][1] == "tags", str(got))
    amb = E.place_lobby_squads(squads, ["DG", "DGE"], bare, bank[:1])
    check("a bank team two printed names could be is left alone", amb.get("1", ("", ""))[1] != "bank", str(amb))

    print("the running match, only where it is this same lobby")
    live = {"names": {1: "DG", 2: "NBE"}, "igns": {1: {"a": "DG.ONE", "b": "DG.TWO"}, 2: {"c": "SOMEONE", "d": "ELSE"}}}
    got = E.place_lobby_squads(squads, names, bare, [], live)
    check("a squad the match holds with the same players is named by it", got.get("1") == ("DG", "live"), str(got))
    check("one whose players differ is not", got.get("2", ("", ""))[1] != "live", str(got))

    print("tags last, and a team only once")
    two = [{"gsTeam": "1", "players": [{"ign": "WSQ.A"}, {"ign": "WSQ.B"}]},
           {"gsTeam": "2", "players": [{"ign": "WSQ.C"}, {"ign": "WSQ.D"}]}]
    got = E.place_lobby_squads(two, ["WSQ", "DG"], bare)
    check("tags place a squad", got.get("1") == ("WSQ", "tags"), str(got))
    check("the same team is never given to a second squad", got.get("2", ("", ""))[0] != "WSQ", str(got))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    sys.exit(1 if failures else 0)


main()
