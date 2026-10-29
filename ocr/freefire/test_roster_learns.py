"""The roster learns its players, so the alive table joins squads by UID
from the lobby on without anyone fetching or assigning them:

  - from a finished game's result file: each team's new players under the
    roster team the file names; never a move, never a new team, and a team
    whose players the roster files elsewhere is left for the operator;
  - from the operator's "which team?" pick: the squad's players under that
    team (made if missing), off any other.

Run: python ocr/freefire/test_roster_learns.py
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


def uids(team):
    return sorted(str(p.get("uid")) for p in team.get("players") or [])


def team(name):
    return next(t for t in ff.server_state["roster"]["teams"] if t["name"] == name)


def result_team(name, players):
    return {"fileTeamName": name, "players": [{"uid": u, "fileName": n, "name": n} for u, n in players]}


def main():
    keep = (copy.deepcopy(ff.server_state.get("roster")), copy.deepcopy(ff._live_match), copy.deepcopy(ff._debugger_id_map))
    try:
        print("\nfrom a result file")
        ff.server_state["roster"] = {"teams": [
            {"name": "ALPHA ESPORTS", "shortName": "ALP", "players": [{"ign": "ALP.ONE", "uid": "11"}]},
            {"name": "BRAVO GAMING", "shortName": "BRV", "players": []},
            {"name": "CHARLIE ESP", "shortName": "CHR", "players": []},
        ]}
        n = ff.learn_roster_from_result([
            result_team("ALPHA ESPORTS", [("11", "ALP.ONE"), ("12", "ALP.TWO")]),
            result_team("BRAVO GAMING", [("21", "BRV.ONE"), ("22", "BRV.TWO")]),
            result_team("NOBODY KNOWS", [("91", "X.ONE")]),
            result_team("CHARLIE ESP", [("11", "ALP.ONE"), ("31", "CHR.ONE")]),
        ])
        check("a team's new player joins it", uids(team("ALPHA ESPORTS")) == ["11", "12"], str(uids(team("ALPHA ESPORTS"))))
        check("an empty team gets its players", uids(team("BRAVO GAMING")) == ["21", "22"])
        check("a name the roster does not have makes no team",
              not any(t["name"] == "NOBODY KNOWS" for t in ff.server_state["roster"]["teams"]))
        check("a team whose player is filed elsewhere is left alone",
              uids(team("CHARLIE ESP")) == [] and uids(team("ALPHA ESPORTS")) == ["11", "12"])
        check("the count", n == 3, str(n))
        check("and twice is nothing new", ff.learn_roster_from_result([
            result_team("BRAVO GAMING", [("21", "BRV.ONE"), ("22", "BRV.TWO")])]) == 0)

        print("\nfrom the operator's pick")
        ff._live_match.clear()
        ff._live_match.update(ff.blank_live_match())
        ff._live_match["gsIgns"] = {5: {"501": "DLT.ONE", "502": "ALP.TWO"}}
        ff._debugger_id_map.update({"501": {"ign": "DLT.ONE", "uid": "41"}, "502": {"ign": "ALP.TWO", "uid": "12"}})
        moved = ff.learn_squad_players(5, "DELTA SQUAD")
        check("a team the roster lacks is made", any(t["name"] == "DELTA SQUAD" for t in ff.server_state["roster"]["teams"]))
        check("with the squad's players", uids(team("DELTA SQUAD")) == ["12", "41"], str(uids(team("DELTA SQUAD"))))
        check("taken off the team they were filed under", uids(team("ALPHA ESPORTS")) == ["11"], str(uids(team("ALPHA ESPORTS"))))
        check("counted", moved == 3, str(moved))

        print("\nthe next game joins by UID")
        live = {"teamNames": {1: "BRAVO GAMING"}, "gsIgns": {9: {"901": "renamed1", "902": "renamed2"}},
                "gsDown": {}, "gsKills": {9: 2}, "wiped": [], "operatorJoins": {}}
        ff._debugger_id_map.update({"901": {"ign": "renamed1", "uid": "21"}, "902": {"ign": "renamed2", "uid": "22"}})
        rows = ff.link_live_teams(live, ff.server_state["roster"])["rows"]
        check("the squad is on its row from the lobby, no pick needed",
              rows and rows[0]["gsTeam"] == 9 and rows[0]["aliveCount"] == 2, str(rows[:1]))
    finally:
        roster, live, ids = keep
        if roster is None:
            ff.server_state.pop("roster", None)
        else:
            ff.server_state["roster"] = roster
        ff._live_match.clear(); ff._live_match.update(live)
        ff._debugger_id_map.clear(); ff._debugger_id_map.update(ids)

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
