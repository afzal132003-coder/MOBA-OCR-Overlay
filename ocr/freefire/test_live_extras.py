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
        {"name": "BRAVO ESPORTS", "shortName": "BRV", "logo": "data:image/png;base64," + "A" * 4000, "players": [{"ign": "BRAVO.ONE"}, {"ign": "BRAVO.TWO"}]},
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
    asyncio.run(E.handle_live_signals([wipes[5]], {5: "BRAVO ESPORTS"}))          # BRAVO has a logo
    te = E.server_state.get("teamEliminated") or {}
    check("the card's state carries no logo image (the pages hold the roster's)",
          te.get("teamName") == "BRAVO ESPORTS" and not te.get("photo"), "%s, %d chars" % (te.get("teamName"), len(str(te))))
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
    check("and is NAMED for its squad, so its elimination card goes up",
          linked["gsNames"].get(7) == "CHARLIE ESPORTS", str(linked["gsNames"]))
    live["gsIgns"][9] = {"999": "SOMEONE.ELSE"}                         # two squads left: no guess
    by = {r["teamName"]: r.get("gsTeam") for r in E.link_live_teams(live, E.server_state["roster"])["rows"]}
    check("with two squads left over it does not guess", by.get("CHARLIE ESPORTS") is None, str(by))

    print("a roster save keeps the short names it did not carry")
    old_r = {"teams": [{"name": "TEAM EVOLUTION", "shortName": "TEV"}, {"name": "LEARN FROM PAST", "shortName": "LFP"}]}
    new_r = {"teams": [{"name": "Team Evolution", "shortName": ""}, {"name": "LEARN FROM PAST", "shortName": "LEARN"},
                       {"name": "NEW TEAM", "shortName": ""}]}
    n = E.keep_short_names(new_r, old_r)
    check("a blank short name is put back from the same team", new_r["teams"][0]["shortName"] == "TEV", str(new_r))
    check("one the save does set is left as set", new_r["teams"][1]["shortName"] == "LEARN")
    check("a team that never had one stays blank", new_r["teams"][2]["shortName"] == "" and n == 1)

    print("revives: who got revived, and by whom")
    REV = (f"""[2026-10-09 20:10:00.000][1][1] [UIModelSpectator] AddPlayer id{A1},name ALPHA.ONE,gsTeam3
[2026-10-09 20:10:00.001][1][2] [UIModelSpectator] AddPlayer id{A2},name ALPHA.TWO,gsTeam3
[2026-10-09 20:10:00.002][1][3] [UIModelSpectator] AddPlayer id{C1},name CHARLIE.ONE,gsTeam7
[2026-10-09 20:10:30.000][1][4] Player {A2} Dead, killed by {C1}
[2026-10-09 20:11:00.000][1][5] Revive Player {A2}, revivePosition=(10.00, 300.00, 20.00)
[2026-10-09 20:11:00.000][1][5] SpectatorItemCountSync : ->{A2}   ->EITEM_TYPE_BUILDING   ->2
[2026-10-09 20:11:00.001][1][5] SpectatorItemCountSync : ->{A1}   ->EITEM_TYPE_PED   ->3
[2026-10-09 20:12:00.000][1][6] Revive Player {A1}, revivePosition=(10.00, 300.00, 20.00)
""" + "".join(f"[2026-10-09 20:12:00.%03d][1][7] SpectatorItemCountSync : ->{C1}   ->EITEM_TYPE_PED   ->3\n" % i
              for i in range(14)))
    rp = Path(tempfile.gettempdir()) / "ff_revive.log"
    io.open(rp, "w", encoding="utf-8").write(REV)
    rlive = E.blank_live_match()
    revs = [e for e in E.read_debugger_events(rp, 0, {}, live=rlive, emit=True)[0] if e["type"] == "revive"]
    check("two revives read", len(revs) == 2, str(revs))
    check("the first: ALPHA.TWO, revived by ALPHA.ONE",
          revs and revs[0]["ign"] == "ALPHA.TWO" and revs[0]["byIgn"] == "ALPHA.ONE", str(revs[:1]))
    check("the second has no teammate's line: no reviver, and never another squad's player",
          len(revs) > 1 and revs[1]["ign"] == "ALPHA.ONE" and revs[1]["byIgn"] == "", str(revs[1:]))
    print("revives on the side table, not as a pop-up")
    E.server_state["liveOps"]["sidetableRows"] = [{"gsTeam": 3, "teamName": "ALPHA ESPORTS"}]
    E.server_state["settings"].pop("reviveRow", None)
    rows = E.revive_rows(revs)
    check("each revive names its team's row and the player",
          [(r["teamName"], r["ign"]) for r in rows] == [("ALPHA ESPORTS", "ALPHA.TWO"), ("ALPHA ESPORTS", "ALPHA.ONE")], str(rows))
    check("each has its own id, so a page never plays one twice", len({r["id"] for r in rows}) == 2)
    E.server_state["settings"]["reviveRow"] = False
    check("the tick off: none", E.revive_rows(revs) == [])
    E.server_state["settings"].pop("reviveRow", None)
    E.server_state["settings"]["sidePopups"] = {"revive": True, "firstBlood": True}
    check("and never a side pop-up", not [p for p in E.detect_side_popups(revs, E.blank_live_match()) if p["type"] == "revive"])

    print("Start From Now never cuts off the match being played")
    L = Path(tempfile.gettempdir()) / "ff_cutoff.log"
    io.open(L, "w", encoding="utf-8").write(
        "[2026-10-11 17:00:00.000][1][1] [UGC] SendJoinMatch, EnableUGC=False\n"
        "[2026-10-11 17:20:00.000][1][2] matchend matchid = 1 / user id = 9\n"
        "[2026-10-11 18:37:50.000][1][3] [UGC] SendJoinMatch, EnableUGC=False\n"
        "[2026-10-11 18:37:50.500][1][4] airline start(1.0, 350.0, 2.0), 50\n"
        "[2026-10-11 18:45:00.000][1][5] Player 1 Dead, killed by 2\n")
    got, began = E.snap_cutoff_to_match("2026-10-11 18:42:46", L)
    check("pressed during a match: back to just before its start",
          got == "2026-10-11 18:36:20" and began == "2026-10-11 18:37:50", "%s %s" % (got, began))
    got, began = E.snap_cutoff_to_match("2026-10-11 18:30:00", L)
    check("pressed between matches: as pressed", got == "2026-10-11 18:30:00" and began is None, str(got))
    got, began = E.snap_cutoff_to_match("2026-10-11 17:10:00", L)
    check("never back past the match before it", got == "2026-10-11 16:58:30" and began == "2026-10-11 17:00:00", str(got))
    io.open(L, "a", encoding="utf-8").write("[2026-10-11 18:38:30.000][1][6] [UGC] SendJoinMatch, EnableUGC=False\n")
    got, began = E.snap_cutoff_to_match("2026-10-11 21:00:00", L)
    check("a start hours ago with no end is not a running match", got == "2026-10-11 21:00:00" and began is None, str(got))
    check("blank stays blank (read the whole log)", E.snap_cutoff_to_match("", L) == ("", None))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    sys.exit(1 if failures else 0)


main()
