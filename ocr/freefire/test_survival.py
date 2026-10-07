"""Survival time, read back from the debugger log for a finished match,
and the result file that two games sharing match id 0 resolve to.

Run: python ocr/freefire/test_survival.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)
import freefire_engine as ff

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


SQ1, SQ2, SQ3 = 1 << 24, 2 << 24, 3 << 24
PLAYERS = [  # uid, pid, ign
    ("1001", SQ1 + 1, "Ace"), ("1002", SQ1 + 2, "Bee"),
    ("2001", SQ2 + 1, "Cat"), ("2002", SQ2 + 2, "Dog"),
    ("3001", SQ3 + 1, "Eel"), ("3002", SQ3 + 2, "Fox"),
]


def line(t, text):
    return "[2026-10-07 03:%02d:%02d.000][1][1] %s\n" % (t // 60, t % 60, text)


def write_fixture(folder):
    dbg = Path(folder) / "Debugger"
    dbg.mkdir()
    out = [line(0, "[UGC] SendJoinMatch, EnableUGC=False,MapID=1,GameMode=1,")]
    out += [line(2, "Player Join, %s, %d, %s, 1" % (u, p, n)) for u, p, n in PLAYERS]
    # Cat dies, comes back from the air, and dies for good later.
    out += [line(100, "Player %d Dead, killed by %d" % (SQ2 + 1, SQ1 + 1)),
            line(130, "Revive Player %d, revivePosition=(1.0, 300.00, 1.0)" % (SQ2 + 1)),
            line(400, "Player %d Dead, killed by %d" % (SQ2 + 1, SQ1 + 2))]
    # Dog is knocked and never gets a Dead line: out when his squad went.
    out += [line(390, "Player '%d' Knock Down, by '%d'" % (SQ2 + 2, SQ1 + 1))]
    # Squad 3: Eel dies at 200, Fox at 250.
    out += [line(200, "Player %d Dead, killed by %d" % (SQ3 + 1, SQ1 + 1)),
            line(250, "Player %d Dead, killed by %d" % (SQ3 + 2, SQ1 + 1))]
    out += [line(600, "SendLogEndGame  matchend matchid = 0 / user id = 1")]
    out.sort(key=lambda l: l[1:20])
    io.open(dbg / "debugger-2026-10-07T02-59-00.log", "w", encoding="utf-8").writelines(out)

    def result(stamp, winner):
        teams = [("ALPHA", 1, ["Ace", "Bee"], ["1001", "1002"]),
                 ("BRAVO", 2, ["Cat", "Dog"], ["2001", "2002"]),
                 ("CHARLIE", 3, ["Eel", "Fox"], ["3001", "3002"])]
        body = ""
        for name, rank, igns, uids in teams:
            if name == "ALPHA":
                name = winner
            body += ("TeamName: %-20s Rank: %-19d KillScore: 3                   RankScore: 5"
                     "                    TotalScore: 8                   \n" % (name, rank))
            for ign, uid in zip(igns, uids):
                body += "NAME: %-20s ID: %-20s KILL: 1                   \n" % (ign, uid)
        return body
    # Two games in rooms without a match id: same id, different times.
    io.open(Path(folder) / "MatchResult_0_2026-09-18-00-09-13.log", "w", encoding="utf-8").write(result("2026-09-18-00-09-13", "OLDTEAM"))
    io.open(Path(folder) / "MatchResult_0_2026-10-07-03-10-00.log", "w", encoding="utf-8").write(result("2026-10-07-03-10-00", "ALPHA"))


def main():
    tmp = tempfile.mkdtemp()
    write_fixture(tmp)
    keep = dict(ff.server_state.get("settings") or {})
    ff.server_state.setdefault("settings", {})["debuggerFolder"] = str(Path(tmp) / "Debugger")
    try:
        print("\nwhich file an id names")
        f, m = ff.find_freefire_match_file(tmp, "0")
        check("a bare id 0 takes the newest file with it", m and m.group("timestamp") == "2026-10-07-03-10-00",
              m.group("timestamp") if m else "")
        f, m = ff.find_freefire_match_file(tmp, "0@2026-09-18-00-09-13")
        check("id@time names one file exactly", m and m.group("timestamp") == "2026-09-18-00-09-13")
        check("the key for an id-0 file carries its time", ff.result_file_key("0", "2026-10-07-03-10-00") == "0@2026-10-07-03-10-00")
        check("a real id is left as it is", ff.result_file_key("2107502057894957056", "x") == "2107502057894957056")

        print("\nsurvival from the log")
        p = ff.build_match_result_payload(tmp, "0")
        surv = {pl["name"]: pl.get("survival") for t in p["teams"] for pl in t["players"]}
        check("the right game was read", any(t["teamName"] == "ALPHA" or "ALPHA" in t.get("fileTeamName", "") for t in p["teams"]),
              str([t["teamName"] for t in p["teams"]]))
        check("a death followed by a revive does not count; the last one does", surv.get("Cat") == 400, str(surv.get("Cat")))
        check("knocked when the squad went: out with the squad", surv.get("Dog") == 400, str(surv.get("Dog")))
        check("a plain final death", (surv.get("Eel"), surv.get("Fox")) == (200, 250), str((surv.get("Eel"), surv.get("Fox"))))
        check("the Booyah squad lasted the whole match", (surv.get("Ace"), surv.get("Bee")) == (600, 600),
              str((surv.get("Ace"), surv.get("Bee"))))
        p_old = ff.build_match_result_payload(tmp, "0@2026-09-18-00-09-13")
        old = [pl.get("survival") for t in p_old["teams"] for pl in t["players"]]
        check("another game with the same id is not given this one's times", all(v is None for v in old), str(old))

        print("\nthe sheet")
        check("m:ss", (ff.fmt_survival(1026), ff.fmt_survival(59), ff.fmt_survival(None)) == ("17:06", "0:59", None))
        rows = ff.booyah_sheet_rows([p])
        check("Booyah rows carry survival", [r["survival"] for r in rows] == ["10:00", "10:00"], str([r["survival"] for r in rows]))
        g1 = {"gameNumber": 1, "teams": [{"teamName": "X", "players": [{"name": "Ace", "uid": "1", "kills": 1, "survival": 600}]}]}
        g2 = {"gameNumber": 2, "teams": [{"teamName": "X", "players": [{"name": "Ace", "uid": "1", "kills": 1, "survival": 300}]}]}
        g3 = {"gameNumber": 3, "teams": [{"teamName": "X", "players": [{"name": "Ace", "uid": "1", "kills": 1}]}]}
        tot = ff.total_fragger_rows([g1, g2, g3], roster_teams=[])
        check("MVP totals: the average over the games that have a figure", tot and tot[0]["survival"] == "7:30",
              str(tot[0]["survival"] if tot else None))
    finally:
        ff.server_state["settings"] = keep

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
