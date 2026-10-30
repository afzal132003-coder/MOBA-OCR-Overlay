"""CLASH SQUAD from the log: each player alive / knocked / dead, the round
to the squad left standing, the next round when a player who was out acts
again (or on the clock), the score, and the operator's corrections.

Run: python ocr/freefire/test_clash.py
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


A = [pid(1, i) for i in range(1, 5)]       # squad 1
B = [pid(2, i) for i in range(1, 5)]       # squad 2
JOIN = "".join("[2026-10-31 20:00:00.%03d][1][%d] [UIModelSpectator] AddPlayer id%d,name %s,gsTeam%d\n"
               % (k, k, p, ("ALP." if p in A else "BRV.") + str(k), 1 if p in A else 2)
               for k, p in enumerate(A + B))


def read(text):
    path = Path(tempfile.mkdtemp()) / "debugger.log"
    io.open(path, "w", encoding="utf-8").write(text)
    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    E.read_debugger_events(path, 0, {}, live=E._live_match, emit=False)
    return E.clash_view(E._live_match)


def states(view, side):
    return [p["state"] for p in view["squads"][side]["players"]]


def knock(t, victim, by):
    return "[2026-10-31 20:%s][1][9] Player '%d' Knock Down, by '%d'\n" % (t, victim, by)


def dead(t, victim, by):
    return "[2026-10-31 20:%s][1][9] Player %d Dead, killed by %d\n" % (t, victim, by)


def main():
    keep = dict(E.server_state.get("settings") or {})
    E.server_state.setdefault("settings", {})["csSwap"] = False
    try:
        print("\nin a round")
        v = read(JOIN + knock("01:00.000", B[0], A[0]) + dead("01:05.000", B[1], A[1]))
        check("two squads, left and right", [q["gs"] for q in v["squads"]] == [1, 2])
        check("a knocked player is knocked, a dead one dead",
              states(v, 1) == ["knocked", "dead", "alive", "alive"], str(states(v, 1)))
        v = read(JOIN + knock("01:00.000", B[0], A[0]) +
                 "[2026-10-31 20:01:20.000][1][9] Revive Player %d, revivePosition=(1.00, 2.00, 3.00)\n" % B[0])
        check("revived: alive again", states(v, 1)[0] == "alive", str(states(v, 1)))

        print("\nthe round to the squad left standing")
        wipe = (knock("01:00.000", B[0], A[0]) + dead("01:02.000", B[0], A[0]) + dead("01:04.000", B[1], A[1])
                + dead("01:06.000", B[2], A[2]) + knock("01:08.000", B[3], A[3]))
        v = read(JOIN + wipe)
        check("nobody standing (the last one knocked): the round is over", v["over"] and v["winner"] == 1)
        check("and the other squad has it", [q["wins"] for q in v["squads"]] == [1, 0], str([q["wins"] for q in v["squads"]]))

        print("\nthe next round")
        nxt = knock("01:30.000", A[0], B[2])          # a player B lost last round, fighting again
        v = read(JOIN + wipe + nxt)
        check("a player who ended the round dead, acting: everyone back",
              states(v, 1) == ["alive"] * 4 and not v["over"] and v["round"] == 2, str(states(v, 1)))
        check("and this round's knock is on the board", states(v, 0)[0] == "knocked", str(states(v, 0)))
        check("the score carries", [q["wins"] for q in v["squads"]] == [1, 0])
        v = read(JOIN + wipe)
        over = E._live_match["cs"]["overAt"]
        check("not before the round's few seconds are up", not E.clash_tick(E._live_match, now=over + 2))
        check("or on the clock, a few seconds after the round", E.clash_tick(E._live_match, now=over + E.CLASH_ROUND_RESET + 1) and
              states(E.clash_view(E._live_match), 1) == ["alive"] * 4)

        print("\na round lost on the clock (no wipe)")
        partial = dead("01:00.000", A[0], B[0]) + dead("01:02.000", B[1], A[1])
        v = read(JOIN + partial + knock("02:30.000", B[2], A[0]))   # A[0] was dead: the next round
        check("the next round starts on the first fight", states(v, 0)[0] == "alive" and v["round"] == 2,
              str(states(v, 0)))
        check("nobody given the round", [q["wins"] for q in v["squads"]] == [0, 0])

        print("\nthe operator")
        read(JOIN + wipe)
        E._live_match["cs"]["adjust"][2] = 1
        v = E.clash_view(E._live_match)
        check("a round added by hand", [q["wins"] for q in v["squads"]] == [1, 1])
        E.server_state["settings"]["csSwap"] = True
        v = E.clash_view(E._live_match)
        check("sides swapped", [q["gs"] for q in v["squads"]] == [2, 1])
        E._live_match["gsIgns"][3] = {pid(3, 1): "X"}
        check("a battle royale lobby (more than two squads): no clash board", E.clash_view(E._live_match) is None)
    finally:
        E.server_state["settings"] = keep

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
