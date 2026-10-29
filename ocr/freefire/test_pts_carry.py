"""The alive table's PTS. column from ticked result files: each team's
overall = its totals in the ticked games + this game's eliminations + its
placement points once it is out (settings.aliveCarryMatchIds).

Run: python ocr/freefire/test_pts_carry.py
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


GAMES = {
    "g1": [{"teamName": "TSG ARMY", "totalScore": 33}, {"teamName": "TSG PROS", "totalScore": 26},
           {"teamName": "S8UL ESPORTS", "totalScore": 4}],
    "g2": [{"teamName": "TSG ARMY", "totalScore": 29}, {"teamName": "S8UL ESPORTS", "totalScore": 12}],
}


def main():
    real = ff.build_match_result_payload
    keep = dict(ff._carry_points)
    ff.build_match_result_payload = lambda folder, mid, **kw: {"teams": GAMES.get(mid, [])}
    try:
        print("\nthe ticked games' totals")
        carry = ff.carry_from_result_files(["g1", "g2"], "X")
        check("summed per team", carry.get(ff.normalize_for_match("TSG ARMY")) == 62 and
              carry.get(ff.normalize_for_match("S8UL ESPORTS")) == 16, str(carry))
        check("a team in one game only", carry.get(ff.normalize_for_match("TSG PROS")) == 26)
        check("none ticked, none carried", ff.carry_from_result_files([], "X") == {})

        print("\nthe overall in this game")
        ff._carry_points.clear(); ff._carry_points.update(carry)
        rows = ff.apply_live_points([
            {"teamName": "TSG ARMY", "elims": 5, "eliminated": False},
            {"teamName": "TSG PROS", "elims": 3, "eliminated": True, "finishRank": 2},
            {"teamName": "NEW TEAM", "elims": 1, "eliminated": False},
        ])
        pts = {r["teamName"]: r["livePoints"] for r in rows}
        check("carried + this game's eliminations, still in", pts["TSG ARMY"] == 62 + 5, str(pts))
        check("+ its placement points once out (2nd: 9)", pts["TSG PROS"] == 26 + 3 + 9, str(pts))
        check("a team with nothing carried counts from nought", pts["NEW TEAM"] == 1, str(pts))
    finally:
        ff.build_match_result_payload = real
        ff._carry_points.clear(); ff._carry_points.update(keep)

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
