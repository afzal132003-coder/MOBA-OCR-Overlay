"""A normal room: no team names anywhere, so the players are the identity.

Run it with no arguments:

    python ocr\\freefire\\test_normal_room.py

WHAT THIS GUARDS

A league room writes each team's name into the debugger log and the
result file. A normal room does not, so the only thing tying a result
block to a registered team is who was in it. The engine already matches a
block to the roster by its players' UIDs -- and UID wins over the file's
team name once FREEFIRE_TEAM_UID_MIN_VOTES of them agree. This pins that
it does so when the team name is useless, which is the whole of the
normal-room case, and that a block whose players are not in the roster is
left for the operator rather than guessed.

No real normal-room result file exists on the event rig to test against:
every one of its 136 files carries real team names. So the names here are
the plausible kinds of nothing a normal room could write -- blank, a
number, a generic "Team 3" -- and the test holds for all of them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FREEFIRE_NO_AUTOSTART", "1")
import freefire_engine as ff

checks = 0
failures = []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label,
                          ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


ROSTER = {"teams": [
    {"name": "S8UL ESPORTS", "shortName": "S8UL", "logo": "s8ul.png", "players": [
        {"ign": "S8uL.Bunnyy", "uid": "3460475190"},
        {"ign": "S8uL.Jack07", "uid": "2365077886"},
        {"ign": "S8uL.Noor18", "uid": "984855672"},
        {"ign": "HENRYY", "uid": "1342123787"}]},
    {"name": "RES", "shortName": "RES", "logo": "res.png", "players": [
        {"ign": "Daafiqqq", "uid": "497404894"},
        {"ign": "iQOOGxkrish", "uid": "705416506"},
        {"ign": "SiDAK.07", "uid": "415866186"},
        {"ign": "Ziyann.11", "uid": "320392903"}]},
]}


def block(team_name, rank, players):
    return {"teamName": team_name, "rank": rank, "killScore": 0,
            "rankScore": 0, "totalScore": 0,
            "players": [{"name": n, "uid": u, "kills": 1} for n, u in players]}


def main():
    print("\na result block with no usable team name")
    for junk in ("", "3", "Team 3", "SQUAD 7"):
        teams = ff.apply_roster_overrides([
            block(junk, 1, [("Bunnyy", "3460475190"), ("Jack", "2365077886"),
                            ("Noor", "984855672"), ("Henry", "1342123787")])], ROSTER)
        t = teams[0]
        check("named %-10r -> resolved by its players' UIDs" % junk,
              t["matched"] and t["teamName"] == "S8UL ESPORTS",
              "%s / matched=%s" % (t["teamName"], t["matched"]))

    print("\nwhat a resolved block carries")
    t = ff.apply_roster_overrides([
        block("", 1, [("a", "497404894"), ("b", "705416506")])], ROSTER)[0]
    check("the roster team's name", t["teamName"] == "RES", t["teamName"])
    check("its short name and logo, for the graphics",
          t.get("shortName") == "RES" and t.get("logo") == "res.png")
    check("and the file's own word for it, kept for the review",
          t.get("fileTeamName") == "")

    print("\na block nobody can place")
    t = ff.apply_roster_overrides([
        block("Team 9", 4, [("x", "111"), ("y", "222"), ("z", "333")])], ROSTER)[0]
    check("is left unmatched for the operator, not guessed",
          t["matched"] is False, "matched=%s" % t["matched"])

    print("\none UID is not enough on its own")
    # A single player who happens to be on the roster must not drag a
    # block of strangers onto that team: the threshold exists for this.
    t = ff.apply_roster_overrides([
        block("", 5, [("Bunnyy", "3460475190"), ("x", "111"), ("y", "222")])], ROSTER)[0]
    check("one matching UID does not settle it below the threshold",
          (not t["matched"]) or t.get("needsUidReview"),
          "matched=%s review=%s" % (t["matched"], t.get("needsUidReview")))

    print("\nthe alive table in a normal room")
    # Every row used to be built from the client's team-name narration,
    # with squads only ever joined on as the second half. A normal room
    # writes no names, so the table came out EMPTY. The squads are in the
    # log in every room, and the roster names them by their players.
    live = {
        "teamNames": {},                      # a normal room: no names at all
        "gsIgns": {3: {"301": "S8uL.Bunnyy", "302": "S8uL.Jack07",
                       "303": "S8uL.Noor18", "304": "HENRYY"},
                   7: {"701": "Daafiqqq", "702": "iQOOGxkrish",
                       "703": "SiDAK.07", "704": "Ziyann.11"},
                   9: {"901": "stranger1", "902": "stranger2"}},
        "gsDown": {3: {"301"}, 7: set()},
        "gsKills": {3: 6, 7: 2, 9: 1},
        "wiped": [9],
    }
    roster = {"teams": [
        {"name": "S8UL ESPORTS", "shortName": "S8UL", "players": [
            {"ign": "S8uL.Bunnyy"}, {"ign": "S8uL.Jack07"},
            {"ign": "S8uL.Noor18"}, {"ign": "HENRYY"}]},
        {"name": "RES", "shortName": "RES", "players": [
            {"ign": "Daafiqqq"}, {"ign": "iQOOGxkrish"},
            {"ign": "SiDAK.07"}, {"ign": "Ziyann.11"}]}]}

    keep = dict(ff.server_state.get("event") or {})
    ff.server_state["event"] = dict(keep, roomType="normal")
    rows = {r["teamName"]: r for r in ff.link_live_teams(live, roster)["rows"]}
    check("every squad gets a row", len(rows) == 3, str(sorted(rows)))
    s8 = rows.get("S8UL ESPORTS") or {}
    check("named from the roster by its players", bool(s8), str(sorted(rows)))
    check("with its own kills", s8.get("elims") == 6, str(s8.get("elims")))
    check("and its players down: 3 of 4 alive", s8.get("aliveCount") == 3,
          str(s8.get("aliveCount")))
    check("its short name from the roster", s8.get("short") == "S8UL",
          str(s8.get("short")))
    res = rows.get("RES") or {}
    check("a full squad shows four alive", res.get("aliveCount") == 4,
          str(res.get("aliveCount")))
    odd = rows.get("SQUAD 9") or {}
    check("a squad nobody claims still gets its line, plainly unassigned",
          bool(odd), str(sorted(rows)))
    check("and a wiped squad is out, with its finishing place",
          odd.get("eliminated") is True and odd.get("placement") == 3,
          "%s / %s" % (odd.get("eliminated"), odd.get("placement")))

    print("\na league room is exactly as it was")
    ff.server_state["event"] = dict(keep, roomType="league")
    league = ff.link_live_teams(live, roster)["rows"]
    check("no names in the log means no rows, as before",
          league == [], "%d rows" % len(league))
    # Built from the saved event with the field REMOVED, not copied as
    # is: the operator's own saved event already said "normal" when this
    # was first run, and a copy of it is not an unset room type.
    unset_event = {k: v for k, v in keep.items() if k != "roomType"}
    ff.server_state["event"] = unset_event
    unset = ff.link_live_teams(live, roster)["rows"]
    check("and an event with no room type set is a league room",
          unset == [], "%d rows" % len(unset))
    ff.server_state["event"] = keep

    print("\na normal room's result is scored here, because the client does not")
    # Read off a real one: every block nameless, KillScore, RankScore and
    # TotalScore all 0 -- only the rank and the players' KILL are real.
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "normal_room_result.log")
    raw = ff.parse_freefire_match_result(open(fixture, encoding="utf-8-sig").read())
    check("the real file really is nameless and unscored",
          all(not t["teamName"] and t["killScore"] == t["rankScore"] == t["totalScore"] == 0
              for t in raw))
    scored = ff.score_unscored_blocks(raw)
    first = scored[0]
    check("kills are its players' kills added up",
          first["killScore"] == sum(pl["kills"] for pl in first["players"]) == 18,
          str(first["killScore"]))
    check("placement from the table: first is 12", first["rankScore"] == 12)
    check("and the total is the two together -- TSG on 30",
          first["totalScore"] == 30, str(first["totalScore"]))
    last = scored[-1]
    check("twelfth with no kills is a real nought", last["totalScore"] == 0)
    check("each is marked as scored here", all(t.get("scoredBy") == "engine" for t in scored))

    league = [{"teamName": "S8UL ESPORTS", "rank": 1, "killScore": 24,
               "rankScore": 12, "totalScore": 36,
               "players": [{"kills": 99}]}]
    ff.score_unscored_blocks(league)
    check("a league result keeps the client's own scores, untouched",
          league[0]["totalScore"] == 36 and "scoredBy" not in league[0])
    named_nil = [{"teamName": "TAG", "rank": 12, "killScore": 0, "rankScore": 0,
                  "totalScore": 0, "players": [{"kills": 5}]}]
    ff.score_unscored_blocks(named_nil)
    check("a NAMED block on nought is left as the client wrote it",
          named_nil[0]["totalScore"] == 0 and "scoredBy" not in named_nil[0])

    print("\na nameless block named from the Mapping tab")
    aliases = ff.server_state.setdefault("aliases", {"teams": {}, "players": {}, "squads": {}})
    keep_sq = dict(aliases.get("squads") or {})
    aliases["squads"] = {ff._ign_key("Daafiqqq"): "RES",
                         ff._ign_key("iQOOGxkrish"): "RES"}
    bare = {"teams": [{"name": "RES", "players": []}]}
    t = ff.apply_roster_overrides([block("", 2, [("Daafiqqq", "1"), ("iQOOGxkrish", "2"),
                                                  ("x", "3")])], bare)[0]
    check("players named as a squad during the match name the result",
          t["matched"] and t["teamName"] == "RES", "%s / %s" % (t["teamName"], t["matched"]))
    one = ff.apply_roster_overrides([block("", 3, [("Daafiqqq", "1"), ("y", "4"),
                                                    ("z", "5")])], bare)[0]
    check("one named player is not enough to settle it", not one["matched"])
    league_t = ff.apply_roster_overrides([block("SOMEBODY", 1, [("Daafiqqq", "1"),
                                                               ("iQOOGxkrish", "2")])], bare)[0]
    check("and a NAMED (league) block never consults it",
          league_t["teamName"] != "RES" or league_t.get("fileTeamName") == "RES",
          league_t["teamName"])
    aliases["squads"] = keep_sq

    print("\nnaming a squad, with nothing in the roster yet")
    # The live case: twelve squads on the table as SQUAD n, a roster with
    # no teams at all, and the only way to name a squad refused every
    # name the roster did not already have. Naming one now creates it.
    import asyncio, json as _json
    from types import SimpleNamespace

    class FakeDash:
        def __init__(self, messages):
            self.request = SimpleNamespace(path="/?page=freefire_dashboard")
            self._in = list(messages)
            self.sent = []
        async def send(self, raw):
            self.sent.append(_json.loads(raw))
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not self._in:
                raise StopAsyncIteration
            return self._in.pop(0)

    keep_roster = ff.server_state.get("roster")
    keep_lobby = ff.server_state["liveOps"].get("lobbyPlayers")
    keep_event = dict(ff.server_state.get("event") or {})
    keep_save = ff.save_state
    ff.save_state = lambda *a, **k: None          # never touch the real file
    try:
        ff.server_state["roster"] = {"teams": []}
        ff.server_state["liveOps"]["lobbyPlayers"] = [
            {"pid": "16777217", "uid": "503039951", "ign": "CTZ\u00d7STEVE19", "gsTeam": 1, "team": ""},
            {"pid": "16777257", "uid": "2323297341", "ign": "CTZ\u00d7S4IF24", "gsTeam": 1, "team": ""},
            {"pid": "16777247", "uid": "2104644627", "ign": "SAKSHMM GOD", "gsTeam": 1, "team": ""},
            {"pid": "16777226", "uid": "3573182434", "ign": "RAGHAVv.01!", "gsTeam": 1, "team": ""}]
        dash = FakeDash([_json.dumps({"type": "freefire_fill_roster_from_lobby",
                                      "assignments": {"1": "CTZ"}})])

        async def go():
            await ff.handle_client(dash)
            await asyncio.sleep(0.05)
        asyncio.run(go())
        reply = [m for m in dash.sent if m.get("type") == "freefire_fill_roster_result"]
        reply = reply[-1] if reply else {}
        teams = ff.server_state["roster"]["teams"]
        check("an empty roster gains the team that was typed",
              [t["name"] for t in teams] == ["CTZ"], str([t["name"] for t in teams]))
        check("with the squad's four players and their IDs",
              sorted(p["uid"] for p in teams[0]["players"]) ==
              sorted(["503039951", "2323297341", "2104644627", "3573182434"]))
        check("and the reply says a team was made",
              reply.get("teamsCreated") == 1 and reply.get("added") == 4, str(reply)[:120])

        ff.server_state["event"] = dict(keep_event, roomType="normal")
        live = {"teamNames": {},
                "gsIgns": {1: {"16777217": "CTZ\u00d7STEVE19", "16777257": "CTZ\u00d7S4IF24",
                               "16777247": "SAKSHMM GOD", "16777226": "RAGHAVv.01!"}},
                "gsDown": {1: set()}, "gsKills": {1: 3}, "wiped": []}
        rows = ff.link_live_teams(live, ff.server_state["roster"])["rows"]
        check("and the alive row is named CTZ from then on",
              [r["teamName"] for r in rows] == ["CTZ"], str([r["teamName"] for r in rows]))

        dash2 = FakeDash([_json.dumps({"type": "freefire_fill_roster_from_lobby",
                                       "assignments": {"1": "ctz"}})])
        asyncio.run(ff.handle_client(dash2))
        check("naming it again, in any case, joins the team rather than twinning it",
              len(ff.server_state["roster"]["teams"]) == 1)

        dash3 = FakeDash([_json.dumps({"type": "freefire_fill_roster_from_lobby",
                                       "assignments": {}})])
        ff.server_state["roster"] = {"teams": []}
        asyncio.run(ff.handle_client(dash3))
        check("and a squad nobody named never invents a team",
              ff.server_state["roster"]["teams"] == [])
    finally:
        ff.save_state = keep_save
        ff.server_state["roster"] = keep_roster
        ff.server_state["liveOps"]["lobbyPlayers"] = keep_lobby
        ff.server_state["event"] = keep_event

    print("\ntwo squads, one team: tonight's eleven rows")
    # The TSG squad's four players had been saved under 4ENDS ESP, which
    # also held one real 4ENDS player. Both squads read as 4ENDS ESP, the
    # overlay keyed them by name, and twelve squads went out as eleven.
    import asyncio as _aio, json as _js
    from types import SimpleNamespace as _NS
    keep_event2 = dict(ff.server_state.get("event") or {})
    keep_roster2 = ff.server_state.get("roster")
    keep_lobby2 = ff.server_state["liveOps"].get("lobbyPlayers")
    keep_save2 = ff.save_state
    ff.save_state = lambda *a, **k: None
    try:
        ff.server_state["event"] = dict(keep_event2, roomType="normal")
        tsg = [("TSG-LEGEND2I", "1"), ("TSG-NOVA", "2"), ("TSG-ICONIC30", "3"), ("TSG-ADDYY17", "4")]
        four = [("Piyushh17!", "5"), ("4ENDS.BELUGA", "6"), ("Anshu26!", "7"), ("HNE-YAGO.18", "8")]
        roster = {"teams": [
            {"name": "4ENDS ESP", "players": [{"ign": i, "uid": u} for i, u in tsg + four[:1]]},
            {"name": "TSG ARMY", "players": []}]}
        ff.server_state["roster"] = roster
        live = {"teamNames": {},
                "gsIgns": {5: {u: i for i, u in four}, 12: {u: i for i, u in tsg}},
                "gsDown": {}, "gsKills": {5: 15, 12: 0}, "wiped": []}
        out = ff.link_live_teams(live, roster)
        names = [r["teamName"] for r in out["rows"]]
        check("both squads still get a row of their own",
              len(names) == 2 and len(set(names)) == 2, str(names))
        check("the squad with more players on the team keeps its name",
              "4ENDS ESP" in names, str(names))
        clash = [u for u in out["unresolved"] if u.get("claimedBy")]
        check("and the other is offered for naming, with the clash spelt out",
              len(clash) == 1 and clash[0]["claimedBy"] == "4ENDS ESP",
              str(clash)[:120])

        ff.server_state["liveOps"]["lobbyPlayers"] = (
            [{"uid": u, "ign": i, "gsTeam": 12, "team": ""} for i, u in tsg] +
            [{"uid": u, "ign": i, "gsTeam": 5, "team": ""} for i, u in four])

        class _Dash:
            def __init__(self, m):
                self.request = _NS(path="/?page=freefire_dashboard"); self._in = [m]; self.sent = []
            async def send(self, raw): self.sent.append(_js.loads(raw))
            def __aiter__(self): return self
            async def __anext__(self):
                if not self._in: raise StopAsyncIteration
                return self._in.pop(0)
        d = _Dash(_js.dumps({"type": "freefire_fill_roster_from_lobby",
                             "assignments": {"12": "TSG ARMY"}}))
        _aio.run(ff.handle_client(d))
        r = [m for m in d.sent if m.get("type") == "freefire_fill_roster_result"][-1]
        teams = {t["name"]: t for t in ff.server_state["roster"]["teams"]}
        check("naming the squad moves its players off the wrong team",
              [p["ign"] for p in teams["4ENDS ESP"]["players"]] == ["Piyushh17!"],
              str([p["ign"] for p in teams["4ENDS ESP"]["players"]]))
        check("onto the right one", len(teams["TSG ARMY"]["players"]) == 4)
        check("and says how many it moved", r.get("moved") == 4, str(r.get("moved")))
        names = sorted(x["teamName"] for x in ff.link_live_teams(live, ff.server_state["roster"])["rows"])
        check("after which both squads are named correctly",
              names == ["4ENDS ESP", "TSG ARMY"], str(names))

        ff.server_state["event"] = dict(keep_event2, roomType="league")
        live_l = dict(live, teamNames={})
        check("a league room is not touched by any of it",
              ff.link_live_teams(live_l, roster)["rows"] == [])
    finally:
        ff.save_state = keep_save2
        ff.server_state["event"] = keep_event2
        ff.server_state["roster"] = keep_roster2
        ff.server_state["liveOps"]["lobbyPlayers"] = keep_lobby2

    print("\nthe director board: slot big, short name")
    keep_ev3 = dict(ff.server_state.get("event") or {})
    try:
        live_d = {"fights": [[1000.0, 3, 9, "kill"], [1001.0, 9, 3, "knock"]],
                  "gsKills": {3: 5, 9: 1}, "gsIgns": {3: {"a": "x"}, 9: {"b": "y"}},
                  "wiped": [], "revivePoints": [], "zone": {}}
        linked_d = {"gsNames": {3: "TEAM APEX", 9: "VASIYO ESP"},
                    "rows": [{"gsTeam": 3, "teamId": None, "short": "APEX"},
                             {"gsTeam": 9, "teamId": None, "short": "VE"}]}
        ff.server_state["event"] = dict(keep_ev3, roomType="normal", roomSlots={})
        keep_ro3 = ff.server_state.get("roster")
        ff.server_state["roster"] = {"teams": [
            {"name": "BFA", "shortName": "BFA"},
            {"name": "VASIYO ESP", "shortName": "VE"},
            {"name": "TEAM APEX", "shortName": "APEX"}]}
        feed = ff.director_feed(live_d, linked_d, now=1005.0)
        inf = feed["engagements"][0]["teamInfo"]
        check("nothing pasted: a normal room's slot is the team's place in "
              "the Pre-Match roster, matched by its players' IGNs",
              {i["short"]: i["slot"] for i in inf} == {"APEX": 3, "VE": 2}, str(inf))
        check("never the log's own squad number",
              not any(i["slot"] in (9,) for i in inf), str(inf))
        check("with the short name the alive table uses",
              sorted(i["short"] for i in inf) == ["APEX", "VE"], str(inf))
        check("the call carries the same", len(feed["call"]["teamInfo"]) == 2)
        check("and the kill leader", feed["killLeader"]["info"]["short"] == "APEX")
        ff.server_state["event"] = dict(keep_ev3, roomType="league")
        linked_l = {"gsNames": linked_d["gsNames"],
                    "rows": [{"gsTeam": 3, "teamId": 7, "short": "APEX"},
                             {"gsTeam": 9, "teamId": None, "short": "VE"}]}
        inf_l = {i["short"]: i["slot"] for i in
                 ff.director_feed(live_d, linked_l, now=1005.0)["engagements"][0]["teamInfo"]}
        check("a league room shows the room's team number", inf_l["APEX"] == 7, str(inf_l))
        check("and no number it does not have, rather than the log's",
              inf_l["VE"] is None, str(inf_l))
        ff.server_state["event"] = dict(keep_ev3, roomType="normal", roomSlots={})
        linked_u = {"gsNames": {3: "TEAM APEX"},
                    "rows": [{"gsTeam": 3, "short": "APEX"}, {"gsTeam": 9, "short": "S9"}]}
        inf_u = {i["short"]: i["slot"] for i in
                 ff.director_feed(live_d, linked_u, now=1005.0)["engagements"][0]["teamInfo"]}
        check("a squad no roster team claims gets no slot, not the log's number",
              inf_u.get("S9") is None and inf_u.get("APEX") == 3, str(inf_u))
    finally:
        ff.server_state["event"] = keep_ev3
        ff.server_state["roster"] = keep_ro3

    print("\nroom slots pasted in Pre-Match")
    keep_ev4 = dict(ff.server_state.get("event") or {})
    keep_ro4 = ff.server_state.get("roster")
    try:
        ff.server_state["roster"] = {"teams": [
            {"name": "TEAM APEX", "shortName": "APEX"},
            {"name": "VASIYO ESP", "shortName": "VE"}]}
        live_s = {"fights": [[1000.0, 3, 9, "kill"]], "gsKills": {3: 1},
                  "gsIgns": {3: {"a": "x"}, 9: {"b": "y"}, 4: {"c": "z"}},
                  "wiped": [], "revivePoints": [], "zone": {}}
        linked_s = {"gsNames": {3: "TEAM APEX", 9: "VASIYO ESP"},
                    "rows": [{"gsTeam": 3, "short": "APEX"}, {"gsTeam": 9, "short": "VE"}]}
        ff.server_state["event"] = dict(keep_ev4, roomType="normal",
                                        roomSlots={"2": "team apex", "11": "VE"})
        got = {i["short"]: i["slot"] for i in
               ff.director_feed(live_s, linked_s, now=1003.0)["engagements"][0]["teamInfo"]}
        check("a pasted full name gives the panel's slot", got.get("APEX") == 2, str(got))
        check("so does a short name", got.get("VE") == 11, str(got))
        ff.server_state["event"]["roomSlots"] = {"2": "team apex"}
        got = {i["short"]: i["slot"] for i in
               ff.director_feed(live_s, linked_s, now=1003.0)["engagements"][0]["teamInfo"]}
        check("a paste is the whole answer: a team left out of it gets no "
              "number, rather than one that could clash",
              got.get("VE") is None and got.get("APEX") == 2, str(got))
        ff.server_state["event"]["roomSlots"] = {
            str(i + 1): n for i, n in enumerate(
                ["BFA", "RNTX", "ASSASSINS", "NG PROS", "VASIYO ESP", "W SQUAD",
                 "TSG ARMY", "DESI GAMER", "iQOO OGXTE", "NEBULA ESP",
                 "TEAM APEX", "4ENDS ESP"])}
        got = {i["short"]: i["slot"] for i in
               ff.director_feed(live_s, linked_s, now=1003.0)["engagements"][0]["teamInfo"]}
        check("tonight's paste: TEAM APEX is slot 11, VASIYO ESP slot 5",
              got == {"APEX": 11, "VE": 5}, str(got))
        ff.server_state["event"] = dict(keep_ev4, roomType="league",
                                        roomSlots={"2": "TEAM APEX"})
        got = {i["short"]: i["slot"] for i in
               ff.director_feed(live_s, linked_s, now=1003.0)["engagements"][0]["teamInfo"]}
        check("a league room ignores pasted slots", got.get("APEX") is None, str(got))
    finally:
        ff.server_state["event"] = keep_ev4
        ff.server_state["roster"] = keep_ro4

    print("\na short name from another lobby is not someone else's team")
    roster_m = [{"name": "LR7 ESP", "shortName": ""}, {"name": "NG PROS", "shortName": ""},
                {"name": "HEAD HUNTERS", "shortName": ""}]
    check("an old lobby's RES is not LR7 ESP (generic ESP set aside)",
          ff.match_roster_team("RES", roster_m) is None,
          str((ff.match_roster_team("RES", roster_m) or {}).get("name")))
    check("but LR7 still finds LR7 ESP",
          (ff.match_roster_team("LR7", roster_m) or {}).get("name") == "LR7 ESP")
    check("and a near-spelling still matches",
          (ff.match_roster_team("HEAD HUNTER", roster_m) or {}).get("name") == "HEAD HUNTERS")

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
