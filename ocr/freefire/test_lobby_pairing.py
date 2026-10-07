"""The Live Lobby names each squad from the running match as well as from
the roster: a squad whose players are not in the roster yet (a new lobby)
is named by the engine's own squad<->team pairing (kill timing, tags,
eliminations), under the roster's spelling where it has one. Made-up
players.

Run: python ocr/freefire/test_lobby_pairing.py
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


live = {"gsIgns": {3: {301: "QX.Ace", 302: "QX.Bee"}, 5: {501: "Zed", 502: "Yan"}, 7: {701: "Lone"}}}
ids = {"301": {"uid": "9301", "ign": "QX.Ace"}, "302": {"uid": "9302", "ign": "QX.Bee"},
       "501": {"uid": "9501", "ign": "Zed"}, "502": {"uid": "9502", "ign": "Yan"}, "701": {"uid": "9701", "ign": "Lone"}}

ff.server_state.setdefault("roster", {})["teams"] = [{"name": "QUANTUM X ESPORTS", "shortName": "QX", "players": []},
                                                     {"name": "KNOWN SQUAD", "players": []}]
linked = {"gsNames": {5: "KNOWN SQUAD"}, "gsInferred": {3: "QUANTUM X", 5: "SOMETHING ELSE", 7: "NEWTEAM"}}
rows = ff.live_lobby_players(live, ids, linked)
team = {r["ign"]: r["team"] for r in rows}

print("\nnaming squads")
check("the roster's own answer wins", team["Zed"] == "KNOWN SQUAD", team["Zed"])
check("a squad the roster cannot name takes the engine's pairing, under the roster's spelling",
      team["QX.Ace"] == "QUANTUM X ESPORTS" and team["QX.Bee"] == "QUANTUM X ESPORTS", team["QX.Ace"])
check("a team the roster has never heard of keeps the client's name", team["Lone"] == "NEWTEAM", team["Lone"])
check("UIDs come along", {r["ign"]: r["uid"] for r in rows}["QX.Ace"] == "9301")
rows2 = ff.live_lobby_players(live, ids, {"gsNames": {}})
check("with no pairing yet, squads are left unnamed", all(r["team"] == "" for r in rows2))

print("\n%d checks, %d failed" % (checks, len(failures)))
sys.exit(1 if failures else 0)
