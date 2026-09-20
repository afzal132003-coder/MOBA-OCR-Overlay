"""Utility counts must read totals, not pickups, and ignore the dead.

Run it with no arguments:

    python ocr\freefire\test_utility_board.py

WHAT THIS GUARDS

The client syncs item counts for the WHOLE lobby, not only the spectated
player -- measured, all 44 players appear. The count is an absolute
total, but a pickup writes TWO lines at the same millisecond: the amount
picked up, then the new total. On an ammo pickup that is "->30" followed
by "->60".

Reading the FIRST of those two is the trap. It turns a squad sitting on
sixty rounds into a squad on thirty, and it is invisible on air because
both numbers are plausible. Drops write the pair in reverse, and two
events in one millisecond produce three or more lines, so the rule is
simply: the last value for a player and item wins.

The other half is who gets counted. A squad's utility is what the players
still standing can use, so the dead are excluded and wiped squads do not
appear at all. Leaning on the client zeroing a corpse's inventory would
make the figure depend on it having got round to syncing the body.

The synthetic log carries a pickup pair, a drop pair, a triple, a dead
player holding stock, and a squad with nobody left -- none of which can
be relied on to appear together in any given real match.

Two things about the synthetic log have to be real, and both caught me
out while writing it. The AddPlayer lines must be in the client's own
shape, "AddPlayer id16777217,nameUP_ONE,gsTeam1" -- an invented shape
parses as nothing. And the player ids must be real ones, because the
squad is ENCODED in the id (id >> 24): small invented ids like 11 and 21
all resolve to squad 0, so every death lands on the wrong squad and the
board looks broken when the engine is fine.
"""
import io
import os
import sys
import json
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
ENGINE_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE
sys.path.insert(0, str(ENGINE_DIR))
os.chdir(HERE)
import freefire_engine as E

LOG = Path(r"D:/Games/Copy of (Sep 16 Update) OB55 PC Client"
           r"/Free Fire_64_Data/Debugger/debugger-2026-09-21T00-51-46.log")

SYNTH = """\
[2026-09-21 01:00:00.000][1][1] [UIModelSpectator] AddPlayer id16777217,nameUP_ONE,gsTeam1
[2026-09-21 01:00:00.001][1][2] [UIModelSpectator] AddPlayer id16777218,nameDEAD_ONE,gsTeam1
[2026-09-21 01:00:00.002][1][3] [UIModelSpectator] AddPlayer id33554433,nameWIPED_ONE,gsTeam2
[2026-09-21 01:00:01.000][1][4] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_BUILDING   ->2
[2026-09-21 01:00:02.000][1][5] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_BUILDING   ->3
[2026-09-21 01:00:02.000][1][6] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_BUILDING   ->5
[2026-09-21 01:00:03.000][1][7] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_MEDKIT   ->4
[2026-09-21 01:00:03.000][1][8] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_MEDKIT   ->1
[2026-09-21 01:00:04.000][1][9] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_SUPER_MEDKIT   ->2
[2026-09-21 01:00:05.000][1][10] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_INHALER   ->1
[2026-09-21 01:00:05.100][1][11] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_INHALER   ->2
[2026-09-21 01:00:05.100][1][12] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_INHALER   ->9
[2026-09-21 01:00:05.100][1][13] SpectatorItemCountSync : ->16777217   ->EITEM_TYPE_INHALER   ->7
[2026-09-21 01:00:06.000][1][14] SpectatorItemCountSync : ->16777218   ->EITEM_TYPE_BUILDING   ->6
[2026-09-21 01:00:07.000][1][15] SpectatorItemCountSync : ->33554433   ->EITEM_TYPE_BUILDING   ->9
[2026-09-21 01:00:08.000][1][16] Player 16777218 Dead, killed by 50331649
[2026-09-21 01:00:09.000][1][17] Player 33554433 Dead, killed by 50331649
"""

# UP_ONE's expected totals, each the LAST value written for that item:
#   gloo     2 then the pair (3, 5)            -> 5
#   medkits  the pair (4, 1) -- a drop -- plus SUPER_MEDKIT 2 -> 1 + 2 = 3
#   inhalers 1 then the triple (2, 9, 7)       -> 7
WANT = {"gloo": 5, "medkits": 3, "inhalers": 7}


def synthetic():
    path = Path(tempfile.gettempdir()) / "ff_util_synth.log"
    io.open(path, "w", encoding="utf-8").write(SYNTH)
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = ""
    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    E.read_debugger_events(path, 0, {}, live=E._live_match, emit=False)
    board = E.utility_board(E._live_match)
    fails = 0

    if len(board) != 1:
        fails += 1
        print("  FAIL: expected one surviving squad, got %d -- %s"
              % (len(board), [(r["gsTeam"], r["alive"]) for r in board]))
        return fails
    row = board[0]
    if row["alive"] != 1:
        fails += 1
        print("  FAIL: a dead player is still being counted (alive=%d, want 1)"
              % row["alive"])
    for key, want in WANT.items():
        got = row.get(key)
        if got != want:
            fails += 1
            print("  FAIL: %s read %s, want %s -- a pickup line is being taken "
                  "for a total" % (key, got, want))
    if not fails:
        print("  ok: totals not pickups (gloo=%d medkits=%d inhalers=%d), "
              "dead excluded, wiped squad absent"
              % (row["gloo"], row["medkits"], row["inhalers"]))
    return fails


def real():
    if not LOG.exists():
        print("  SKIP: no reference log at", LOG)
        return 0
    roster = (json.load(io.open("freefire_state.json", encoding="utf-8"))
              .get("roster") or {})
    lines = io.open(LOG, encoding="utf-8", errors="replace").readlines()
    path = Path(tempfile.gettempdir()) / "ff_util_real.log"
    io.open(path, "w", encoding="utf-8").writelines(lines[:int(len(lines) * 0.94)])
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = ""
    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    E.read_debugger_events(path, 0, {}, live=E._live_match, emit=False)
    board = E.utility_board(E._live_match, E.link_live_teams(E._live_match, roster))
    fails = 0
    if len(board) < 2:
        fails += 1
        print("  FAIL: only %d squads on the board mid-match" % len(board))
    # Shape, not exact values: a squad cannot hold a negative or a silly
    # number of gloo walls, and somebody must be carrying something.
    for r in board:
        for key in ("gloo", "medkits", "inhalers", "grenades"):
            if r[key] < 0 or r[key] > 40 * max(1, r["alive"]):
                fails += 1
                print("  FAIL: %s has %s=%d with %d alive"
                      % (r["team"], key, r[key], r["alive"]))
    if not any(r["gloo"] for r in board):
        fails += 1
        print("  FAIL: no squad is carrying a single gloo wall")
    if not fails:
        top = board[0]
        print("  ok: %d squads, most gloo = %s with %d across %d players"
              % (len(board), top["team"] or "?", top["gloo"], top["alive"]))
    return fails


def main():
    print("engine under test:", E.__file__)
    print("synthetic:")
    fails = synthetic()
    print("real match, mid-game:")
    fails += real()
    print("\nRESULT:", "utility counts hold up" if not fails
          else "%d problem(s)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
