"""The wipe banner's who-wiped-whom and the live elimination race, from a
short made-up client log read the way the engine reads the real one.

Run: python ocr/freefire/test_live_extras.py
"""
import asyncio
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


A1, A2, B1, B2, C1 = pid(3, 1), pid(3, 2), pid(5, 1), pid(5, 2), pid(7, 1)
LOG = f"""[2026-10-09 20:00:00.000][1][1] [UIModelSpectator] AddPlayer id{A1},name ALPHA.ONE,gsTeam3
[2026-10-09 20:00:00.001][1][2] [UIModelSpectator] AddPlayer id{A2},name ALPHA.TWO,gsTeam3
[2026-10-09 20:00:00.002][1][3] [UIModelSpectator] AddPlayer id{B1},name BRAVO.ONE,gsTeam5
[2026-10-09 20:00:00.003][1][4] [UIModelSpectator] AddPlayer id{B2},name BRAVO.TWO,gsTeam5
[2026-10-09 20:00:00.004][1][5] [UIModelSpectator] AddPlayer id{C1},name CHARLIE.ONE,gsTeam7
[2026-10-09 20:01:00.000][1][6] Player {B1} Dead, killed by {A1}
[2026-10-09 20:01:05.000][1][7] Player {B2} Dead, killed by {A1}
[2026-10-09 20:01:05.100][1][8] PCInGameFlagManager: Team 5 eliminated
[2026-10-09 20:02:00.000][1][9] Player {C1} Dead, killed by {C1}
[2026-10-09 20:02:00.100][1][10] PCInGameFlagManager: Team 7 eliminated
"""


def main():
    path = Path(tempfile.gettempdir()) / "ff_live_extras.log"
    io.open(path, "w", encoding="utf-8").write(LOG)
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = ""
    E.server_state["settings"]["autoTeamEliminated"] = True
    E.server_state["settings"]["eliminationApproval"] = False
    E.server_state["roster"] = {"teams": [
        {"name": "ALPHA ESPORTS", "shortName": "ALP", "players": [{"ign": "ALPHA.ONE"}, {"ign": "ALPHA.TWO"}]},
        {"name": "BRAVO ESPORTS", "shortName": "BRV", "players": [{"ign": "BRAVO.ONE"}, {"ign": "BRAVO.TWO"}]},
        {"name": "CHARLIE ESPORTS", "shortName": "CHR", "players": [{"ign": "CHARLIE.ONE"}]}]}
    E.server_state.setdefault("liveOps", {})["wipedBy"] = {}
    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    events, offset, signals = E.read_debugger_events(path, 0, {}, live=E._live_match, emit=True)

    print("who wiped whom, from the log")
    wipes = {s["gsTeam"]: s for s in signals if s.get("type") == "team_wiped"}
    check("squad 5's wipe names squad 3", (wipes.get(5) or {}).get("by") == 3, str(wipes.get(5)))
    check("with squad 3's players", sorted((wipes.get(5) or {}).get("byIgns") or []) == ["ALPHA.ONE", "ALPHA.TWO"])
    check("squad 7 died to the zone: nobody named", (wipes.get(7) or {}).get("by") is None, str(wipes.get(7)))

    print("the banner's data, for the in-game source")
    asyncio.run(E.handle_live_signals(signals, {3: "ALPHA ESPORTS", 5: "BRAVO ESPORTS", 7: "CHARLIE ESPORTS"}))
    wb = E.server_state["liveOps"].get("wipedBy") or {}
    check("BRAVO ESPORTS wiped by ALPHA ESPORTS", (wb.get("BRAVO ESPORTS") or {}).get("by") == "ALPHA ESPORTS", str(wb))
    check("no banner for a zone death", "CHARLIE ESPORTS" not in wb, str(wb))

    print("the elimination race")
    id_map = {str(A1): {"ign": "ALPHA.ONE", "uid": "111"}, str(A2): {"ign": "ALPHA.TWO", "uid": "112"}}
    E._live_match["playerKills"] = {str(A1): 2, str(A2): 1}
    linked = {"rows": [{"gsTeam": 3, "teamName": "ALPHA ESPORTS", "short": "ALP"},
                       {"gsTeam": 5, "teamName": "BRAVO ESPORTS", "short": "BRV"}]}
    race = E.frag_race(E._live_match, linked, id_map)
    check("most eliminations first", [r["ign"] for r in race] == ["ALPHA.ONE", "ALPHA.TWO"], str(race))
    check("with their team", race and race[0]["teamName"] == "ALPHA ESPORTS" and race[0]["short"] == "ALP")
    check("and their count", race and race[0]["kills"] == 2)
    E._live_match["playerKills"] = {str(A1): 2, str(A2): 1, str(B1): 5}
    E._live_match["wiped"] = [5]
    race = E.frag_race(E._live_match, linked, {**id_map, str(B1): {"ign": "BRAVO.ONE", "uid": "211"}})
    check("a player of a wiped squad still ranks, marked out",
          race[0]["ign"] == "BRAVO.ONE" and race[0]["teamOut"] and not race[1]["teamOut"], str(race[:2]))
    check("never more than three", len(E.frag_race(E._live_match, linked, id_map, top=3)) <= 3)

    print("the last team left takes the last squad left")
    E.server_state.setdefault("aliases", {})["squads"] = {}
    live = E.blank_live_match()
    live["teamNames"] = {1: "ALPHA ESPORTS", 2: "BRAVO ESPORTS", 3: "CHARLIE ESPORTS"}
    live["gsIgns"] = {3: {str(A1): "ALPHA.ONE", str(A2): "ALPHA.TWO"}, 5: {str(B1): "BRAVO.ONE", str(B2): "BRAVO.TWO"},
                      7: {str(C1): "SUB.PLAYER"}}                        # CHARLIE plays a substitute
    linked = E.link_live_teams(live, E.server_state["roster"])
    by = {r["teamName"]: r.get("gsTeam") for r in linked["rows"]}
    check("the two that match their roster join as before", by.get("ALPHA ESPORTS") == 3 and by.get("BRAVO ESPORTS") == 5, str(by))
    check("the team whose players match nothing takes the one squad left", by.get("CHARLIE ESPORTS") == 7, str(by))
    live["gsIgns"][9] = {"999": "SOMEONE.ELSE"}                         # two squads left: no guess
    by = {r["teamName"]: r.get("gsTeam") for r in E.link_live_teams(live, E.server_state["roster"])["rows"]}
    check("with two squads left over it does not guess", by.get("CHARLIE ESPORTS") is None, str(by))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    sys.exit(1 if failures else 0)


main()
