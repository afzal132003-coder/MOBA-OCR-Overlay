"""Team graph game labels: the game's number in the event, not its place
in the graph -- "G4" under the night's fourth game, not "G1".

Run: python ocr/freefire/test_game_labels.py
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


def game(stamp, number=None):
    m = {"timestamp": stamp, "teams": [{"teamName": "ALPHA", "killScore": 1, "rankScore": 2, "totalScore": 3, "rank": 1}]}
    if number is not None:
        m["gameNumber"] = number
    return m


def main():
    keep = dict(ff.server_state.get("event") or {})
    try:
        ff.server_state["event"] = dict(keep, currentGame=4)
        g = ff.compute_team_graphs([game("2026-10-07-19-23")], roster_teams=[])
        check("one picked file, current game 4: G4", g["games"] == ["G4"], str(g["games"]))
        g = ff.compute_team_graphs([game("2026-10-07-18-30"), game("2026-10-07-18-57"), game("2026-10-07-19-23")], roster_teams=[])
        check("three picked files: G2 G3 G4", g["games"] == ["G2", "G3", "G4"], str(g["games"]))
        g = ff.compute_team_graphs([game("a", 2), game("b", 5)], roster_teams=[])
        check("committed games keep their own numbers", g["games"] == ["G2", "G5"], str(g["games"]))
        ff.server_state["event"] = dict(keep, currentGame=1)
        g = ff.compute_team_graphs([game("a"), game("b")], roster_teams=[])
        check("current game lower than the games: counted from one", g["games"] == ["G1", "G2"], str(g["games"]))
    finally:
        ff.server_state["event"] = keep
    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
