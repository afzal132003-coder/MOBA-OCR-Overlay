"""The cross-game fragger table.

Run it with no arguments:

    python ocr\freefire\test_fraggers.py

WHAT THIS GUARDS

  * A PLAYER WHO RENAMES MID-SERIES. IGNs change between games -- a
    sponsor tag arrives, a clan prefix goes. Keyed on the name, one
    player becomes two half-rows, which is exactly the fault the team
    standings had before they were keyed on roster identity.

  * A RE-COMMITTED GAME. Fixing game 3 and committing it again must
    land back on game 3, not append a seventh column.

  * THE ORDER. Eliminations decide it; knocks break a tie; then whoever
    needed fewer games.
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


def match(game, teams):
    return {"gameNumber": game, "teams": teams}


def team(name, rank, players):
    return {"name": name, "teamName": name, "rank": rank, "players": players}


def main():
    print("\nthe same player across six games")
    # Same uid, different IGN in game 2 -- one person, one row.
    matches = [
        match(1, [team("NEBULA ESP", 1, [
            {"name": "NBE.Ace", "uid": "100", "kills": 4, "knocks": 2},
            {"name": "NBE.Rex", "uid": "101", "kills": 1, "knocks": 0}])]),
        match(2, [team("NEBULA ESP", 3, [
            {"name": "NBE.Ace|YT", "uid": "100", "kills": 3, "knocks": 1},
            {"name": "NBE.Rex", "uid": "101", "kills": 6, "knocks": 0}])]),
    ]
    out = ff.compute_freefire_fraggers(matches, roster_teams=[])
    rows = out["rows"]
    check("one row per player, not per name", len(rows) == 2,
          "got %d" % len(rows))
    ace = [r for r in rows if r["uid"] == "100"][0]
    check("the renamed player's kills are added up", ace["totalKills"] == 7,
          "%d" % ace["totalKills"])
    check("the latest name is the one shown", ace["ign"] == "NBE.Ace|YT",
          ace["ign"])
    check("per-game kills are kept separately",
          ace["perGame"] == {"1": 4, "2": 3}, str(ace["perGame"]))
    check("the games played are listed", out["games"] == [1, 2],
          str(out["games"]))

    print("\nre-committing a game replaces it")
    again = matches + [match(2, [team("NEBULA ESP", 2, [
        {"name": "NBE.Ace|YT", "uid": "100", "kills": 9, "knocks": 0}])])]
    out2 = ff.compute_freefire_fraggers(again, roster_teams=[])
    ace2 = [r for r in out2["rows"] if r["uid"] == "100"][0]
    check("game 2 is overwritten, not appended",
          ace2["perGame"] == {"1": 4, "2": 9}, str(ace2["perGame"]))
    check("the total is not double counted", ace2["totalKills"] == 13,
          "4 + 9, not 4 + 3 + 9: got %d" % ace2["totalKills"])
    check("and it counts as two games, not three", ace2["games"] == 2,
          "%d" % ace2["games"])

    print("\nthe order")
    ordered = ff.compute_freefire_fraggers([
        match(1, [team("A", 1, [
            {"name": "mostKills", "uid": "1", "kills": 10, "knocks": 0},
            {"name": "tiedLowKnocks", "uid": "2", "kills": 5, "knocks": 1},
            {"name": "tiedHighKnocks", "uid": "3", "kills": 5, "knocks": 9}])]),
    ], roster_teams=[])["rows"]
    check("most eliminations first", ordered[0]["ign"] == "mostKills",
          ordered[0]["ign"])
    check("knocks break the tie", ordered[1]["ign"] == "tiedHighKnocks",
          ordered[1]["ign"])
    check("rank is numbered from 1",
          [r["rank"] for r in ordered] == [1, 2, 3],
          str([r["rank"] for r in ordered]))

    print("\nawkward input")
    odd = ff.compute_freefire_fraggers([
        match(1, [team("A", 1, [
            {"name": "noUid", "uid": "", "kills": 2},
            {"name": "", "uid": "", "kills": 5}])]),
        {"teams": [team("A", 1, [{"name": "noGame", "uid": "9", "kills": 3}])]},
    ], roster_teams=[])
    names = [r["ign"] for r in odd["rows"]]
    check("a player with no uid still appears, keyed on the name",
          "noUid" in names, str(names))
    check("a nameless, uidless entry is dropped", "" not in names, str(names))
    check("a match with no game number is skipped",
          "noGame" not in names, str(names))

    print("\nheadshots, and how much of the series they cover")
    # Game 1 watched (4 kills, 2 headshots); game 2 NOT watched -- no
    # headshots key at all, which is a different fact from zero.
    hs = ff.compute_freefire_fraggers([
        match(1, [team("A", 1, [
            {"name": "Ace", "uid": "100", "kills": 4, "headshots": 2}])]),
        match(2, [team("A", 1, [
            {"name": "Ace", "uid": "100", "kills": 6}])]),
    ], roster_teams=[])["rows"][0]
    check("headshots total only the games that carried them",
          hs["totalHeadshots"] == 2, "%d" % hs["totalHeadshots"])
    check("and the row says how many games that was",
          hs["hsGames"] == 1, "%d of %d games" % (hs["hsGames"], hs["games"]))
    check("the rate divides by those games kills, not the series",
          hs["hsPct"] == 50, "2/4 = 50 pct, got %s" % hs["hsPct"])

    none = ff.compute_freefire_fraggers([
        match(1, [team("A", 1, [{"name": "B", "uid": "1", "kills": 3}])]),
    ], roster_teams=[])["rows"][0]
    check("no headshot data at all gives no rate, not zero pct",
          none["hsPct"] is None, str(none["hsPct"]))
    check("a genuine zero is still a zero",
          ff.compute_freefire_fraggers([match(1, [team("A", 1, [
              {"name": "C", "uid": "2", "kills": 3, "headshots": 0}])])],
              roster_teams=[])["rows"][0]["hsPct"] == 0)

    print("\nthe MVP pick")
    ms = [
        match(1, [team("NEBULA", 1, [
            {"name": "Ace", "uid": "1", "kills": 5, "knocks": 2, "headshots": 3},
            {"name": "Rex", "uid": "2", "kills": 1, "knocks": 0, "headshots": 0}]),
                  team("4ENDS", 2, [
            {"name": "Zen", "uid": "3", "kills": 9, "knocks": 1, "headshots": 1}])]),
    ]
    booyah = ff.compute_freefire_mvp(ms, scope="booyah", roster_teams=[])
    check("the Booyah MVP comes from the WINNING squad, not the top fragger",
          booyah["ign"] == "Ace",
          "Zen had 9 kills but finished 2nd; got %s" % booyah["ign"])
    check("its contribution is a share of that squad",
          booyah["contribution"] == 85.42,
          "Ace 41, Rex 7, pool 48: got %s" % booyah["contribution"])
    across = ff.compute_freefire_mvp(ms, scope="match", roster_teams=[])
    check("the match MVP looks at every team", across["ign"] == "Zen",
          across["ign"])

    # Damage was the documented tiebreak and does not exist, so the rule
    # must settle a tie on its own without falling back to file order.
    tie = ff.compute_freefire_mvp([match(1, [team("A", 1, [
        {"name": "LowHs", "uid": "1", "kills": 4, "knocks": 0, "headshots": 1},
        {"name": "HighHs", "uid": "2", "kills": 4, "knocks": 0, "headshots": 3}])])],
        scope="booyah", roster_teams=[])
    check("a tie on finishes is broken by headshots, not file order",
          tie["ign"] == "HighHs", tie["ign"])

    ov = ff.compute_freefire_mvp(ms, scope="booyah", override_uid="2",
                                 roster_teams=[])
    check("an operator override wins", ov["ign"] == "Rex", ov["ign"])
    check("and the row admits it was not the automatic pick",
          ov["auto"] is False)
    check("the automatic pick says so too", booyah["auto"] is True)

    check("no matches means no MVP, not a crash",
          ff.compute_freefire_mvp([], scope="event", roster_teams=[]) is None)

    print("\nthe ARROW sheet blocks")
    sm = [
        match(1, [team("NEBULA", 1, [
            {"name": "Ace", "uid": "1", "kills": 5, "knocks": 2, "headshots": 3},
            {"name": "Rex", "uid": "2", "kills": 1, "knocks": 0, "headshots": 1}]),
                  team("4ENDS", 2, [
            {"name": "Zen", "uid": "5", "kills": 9, "knocks": 1, "headshots": 4}])]),
    ]
    b = ff.booyah_sheet_rows(sm, roster_teams=[])
    check("Booyah rows come from the winning squad only",
          [r["ign"] for r in b] == ["Ace", "Rex"], str([r["ign"] for r in b]))
    check("head rate is headshots over that players own finishes",
          b[0]["headRate"] == 60.0, "3 of 5: got %s" % b[0]["headRate"])
    check("finish contribution is a share of the squad",
          b[0]["finContri"] == 85.42, "41 of 48: got %s" % b[0]["finContri"])

    m5 = ff.match_fragger_rows(sm, game=1, top=5, roster_teams=[])
    check("the match top-5 crosses every team", m5[0]["ign"] == "Zen",
          m5[0]["ign"])
    check("it is capped at five rows",
          len(ff.match_fragger_rows(sm, game=1, top=5, roster_teams=[])) <= 5)
    check("head contribution is a share of the TEAM headshots, not of kills",
          m5[1]["headContri"] == 75.0,
          "Ace 3 of NEBULA 4: got %s" % m5[1]["headContri"])

    # The two percentages are different numbers and must not be the same
    # field wearing two names.
    ace = [r for r in m5 if r["ign"] == "Ace"][0]
    check("head rate and head contribution are not the same figure",
          ace["headRate"] != ace["headContri"],
          "rate %s vs contri %s" % (ace["headRate"], ace["headContri"]))

    allg = ff.total_fragger_rows(sm, games=None, roster_teams=[])
    one = ff.total_fragger_rows(sm, games=[1], roster_teams=[])
    check("totals with no game filter take everything",
          len(allg) == 3, "%d" % len(allg))
    check("ticking only game 1 still works", len(one) == 3, "%d" % len(one))
    check("ticking a game that has not happened yields nothing",
          ff.total_fragger_rows(sm, games=[6], roster_teams=[]) == [])
    check("no matches at all is empty, not a crash",
          ff.booyah_sheet_rows([], roster_teams=[]) == []
          and ff.match_fragger_rows([], roster_teams=[]) == [])

    # A game the engine never watched has no headshot figure; a rate of
    # 0% would read as "never hit one".
    nohs = ff.match_fragger_rows([match(1, [team("A", 1, [
        {"name": "NoData", "uid": "9", "kills": 4, "knocks": 0}])])],
        roster_teams=[])[0]
    check("no headshot data gives no rate and no contribution",
          nohs["headRate"] is None and nohs["headContri"] is None,
          "%s / %s" % (nohs["headRate"], nohs["headContri"]))

    print("\nrecovering headshots and knocks from a past match")
    # The result file carries only NAME/ID/KILL -- no headshots, no
    # knocks -- and the replay's headshot list is empty. They live in the
    # debugger log, which the client keeps, so a match played with the
    # engine switched off is recoverable rather than lost.
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    log = tmp / "debugger-2026-10-03T18-49-10.log"
    lines = [
        # --- an EARLIER match in the same file, which must not leak ---
        "Player Join, 111, 70000001, OLD.Player, x",
        "PlayKnockDownGunTrace killer=70000001 victim=70000002 headshot=True",
        "Player 70000002 Dead, killed by 70000001",
        "matchend matchid = 1111111111",
        # --- the one we want ---
        "Player Join, 222, 80000001, NEW.Ace, x",
        "Player Join, 333, 80000002, NEW.Rex, x",
        "Player '80000002' Knock Down, by '80000001'",
        "PlayKnockDownGunTrace killer=80000001 victim=80000002 headshot=True",
        "Player 80000002 Dead, killed by 80000001",
        "PlayKnockDownGunTrace killer=80000001 victim=80000003 headshot=False",
        "Player 80000003 Dead, killed by 80000001",
        "matchend matchid = 2222222222",
    ]
    log.write_text("\n".join(lines), encoding="utf-8")

    got = ff.recover_match_extras("2222222222", str(tmp))
    ace = got.get("222") or {}
    check("the wanted match is found and its killer resolved to a uid",
          bool(ace), str(got))
    check("two kills counted", ace.get("kills") == 2, str(ace))
    check("only the headshot one counted as a headshot",
          ace.get("headshots") == 1, str(ace))
    check("the knockdown counted", ace.get("knocks") == 1, str(ace))
    check("the EARLIER match in the same log does not leak in",
          "111" not in got, str(list(got)))
    check("a match that is in no log returns nothing, not a crash",
          ff.recover_match_extras("9999999999", str(tmp)) == {})

    # Handed the folder ABOVE the logs it should still find them: the
    # fetch only knows the result folder, and passing that straight
    # through silently found nothing at all.
    parent = tmp.parent / (tmp.name + "_wrap")
    (parent / "Debugger").mkdir(parents=True, exist_ok=True)
    (parent / "Debugger" / log.name).write_text(
        "\n".join(lines), encoding="utf-8")
    check("the result folder is accepted as well as the Debugger folder",
          bool(ff.recover_match_extras("2222222222", str(parent))))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
