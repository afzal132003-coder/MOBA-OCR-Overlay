"""Finishing places (assign_finish_ranks): the first squad out of twelve
finishes 12th, the next 11th -- and that holds when the squads are renamed
mid-match (a normal room's lobby read names them SQUAD 1..12, the operator
types the real names in), and when the engine restarts into a match with
several squads already out, which are dealt their places in the order they
actually went out.

Live, 2026-10-28: renamed after going out, four old names kept 12th..9th
taken, and every elimination after was dealt from 8th down.

Run: python ocr/freefire/test_finish_ranks.py
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


REAL = ["DBZ ESPORTS", "GHOST PROTOCOL", "BLACK ATTEMPT", "TARUN GANG", "BLR", "LAST ZONE",
        "FAKE WORLD", "WHITE HOUSE", "KARAVEX ESP", "RANA", "ALPHA GAMERS", "OLD SCHOOL"]


def rows_named(names):
    return [{"teamName": n, "gsTeam": i + 1, "eliminated": False, "aliveCount": 4} for i, n in enumerate(names)]


def roster(names):
    ff.server_state["roster"] = {"teams": [{"name": n} for n in names]}


def out(rows, gs, seq):
    """Squad `gs` loses its last player, the `seq`-th death of the match."""
    rows[gs - 1].update(eliminated=True, aliveCount=0)
    ff._live_match.setdefault("gsLastDeath", {})[gs] = seq


def ranks(rows):
    return {r["teamName"]: r.get("finishRank") for r in rows if r.get("eliminated")}


def main():
    keep = (copy.deepcopy(ff.server_state.get("roster")), copy.deepcopy(ff._live_match), dict(ff._finish_ranks),
            ff.announce_elimination)
    announced = []
    ff.announce_elimination = lambda row, rank: announced.append((row["teamName"], rank))
    try:
        print("\nrenamed mid-match")
        ff._finish_ranks.clear()
        ff._live_match.clear()
        squads = ["SQUAD %d" % i for i in range(1, 13)]
        roster(squads)
        rows = rows_named(squads)
        ff.assign_finish_ranks(rows)
        out(rows, 2, 10); ff.assign_finish_ranks(rows)
        out(rows, 7, 20); ff.assign_finish_ranks(rows)
        check("first out 12th, then 11th", ranks(rows) == {"SQUAD 2": 12, "SQUAD 7": 11}, str(ranks(rows)))
        # the operator types the real names in: the roster, then the table
        roster(REAL)
        renamed = rows_named(REAL)
        for r, old in zip(renamed, rows):
            r.update(eliminated=old["eliminated"], aliveCount=old["aliveCount"])
        rows = renamed
        ff.assign_finish_ranks(rows)
        check("the renamed squads keep their places, in the order they went out",
              ranks(rows) == {"GHOST PROTOCOL": 12, "FAKE WORLD": 11}, str(ranks(rows)))
        out(rows, 3, 30); ff.assign_finish_ranks(rows)
        check("the next one out is 10th, not 8th", ranks(rows).get("BLACK ATTEMPT") == 10, str(ranks(rows)))
        check("and its card says so", announced[-1] == ("BLACK ATTEMPT", 10), str(announced[-1:]))
        check("no old name still holds a place", not any(k.startswith("SQUAD") for k in ff._finish_ranks),
              str(sorted(ff._finish_ranks)))

        print("\nan engine restarted into the match")
        ff._finish_ranks.clear()
        ff._live_match.clear()
        roster(REAL)
        rows = rows_named(REAL)
        # five out -- listed in table order, gone out in another
        for gs, seq in ((1, 50), (3, 10), (4, 20), (6, 30), (12, 40)):
            out(rows, gs, seq)
        n_before = len(announced)
        ff.assign_finish_ranks(rows)
        check("dealt in the order they went out",
              ranks(rows) == {"BLACK ATTEMPT": 12, "TARUN GANG": 11, "LAST ZONE": 10, "OLD SCHOOL": 9, "DBZ ESPORTS": 8},
              str(ranks(rows)))
        check("a batch is not announced", len(announced) == n_before)
        out(rows, 10, 60); ff.assign_finish_ranks(rows)
        check("and the next one out is 7th", ranks(rows).get("RANA") == 7, str(ranks(rows)))

        print("\na squad not read for a poll")
        ff._finish_ranks.clear()
        ff._live_match.clear()
        roster(REAL)
        rows = rows_named(REAL)
        out(rows, 5, 5); ff.assign_finish_ranks(rows)
        ff.assign_finish_ranks([r for r in rows if r["teamName"] != "BLR"])   # BLR missing this poll
        out(rows, 8, 9); ff.assign_finish_ranks(rows)
        check("keeps its place (it is still in the roster)", ranks(rows) == {"BLR": 12, "WHITE HOUSE": 11}, str(ranks(rows)))
    finally:
        roster_k, live_k, ranks_k, ann = keep
        if roster_k is None:
            ff.server_state.pop("roster", None)
        else:
            ff.server_state["roster"] = roster_k
        ff._live_match.clear(); ff._live_match.update(live_k)
        ff._finish_ranks.clear(); ff._finish_ranks.update(ranks_k)
        ff.announce_elimination = ann

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
