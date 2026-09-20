"""The log alone must be able to fill the whole alive table.

Run it with no arguments:

    python ocr\freefire\test_log_only_table.py

WHAT THIS GUARDS

With the table forced to "Debugger log only" there is no OCR behind it.
Whatever link_live_teams() produces IS what goes on air, so a row it
cannot fill is a row that is blank during a live match -- there is no
longer a screen read quietly making up the difference.

Three separate things have to hold for every team, and they fail
independently:

  * a NAME, which comes from OnTeamScoreInited and needs no join at all
  * a KILL COUNT, from the client's kill narration -- the number that
    cannot be misread, unlike a digit OCR'd off a moving table. It must
    come from gsKills and NOT from the running TeamScore, which folds in
    a team's placement bonus the moment that team is eliminated
  * ALIVE BARS, which are the one part that DOES need the roster, because
    the client numbers teams one way for names and another way for
    players and connects the two nowhere (see link_live_teams)

and a fourth thing that is not about filling the table at all:

  * a squad with nobody standing must be published as ELIMINATED. The
    client does not reliably print a wipe line for every squad -- on a
    live match four teams the game itself was dimming had all four
    players down and no wipe line, and went on air as alive.

Only the third can fail from a bad roster, and it is the one that matters
most on air, so it is checked per row rather than in aggregate.

Checked at several points THROUGH the match, not only at the end. A table
that is right at the final whistle and blank for the first five minutes
would pass an end-state check and fail on air.

THE ROW COUNT IS CHECKED SEPARATELY, AND THAT MATTERS

Every other check here is a ratio against the rows that exist, so a table
that collapses to ONE row scores a perfect 1/1 on all of them. That is not
hypothetical: seeding the running score at zero in the parser made a fresh
lobby look like a finished game to the roll-over, which cleared the team
names, so each init line wiped the one before it -- eleven teams arrived
as one and every ratio still read 100%. The count is therefore compared
against the teams the log NAMED, and against the other slices.

THE CUTOFF

settings.debuggerStartAt makes the reader skip every line stamped before
it. It is an operator setting that persists, and one left over from a
previous session skips the whole of an older log -- which looks exactly
like a log that says nothing. This test clears it, and says so, because
an earlier version of this measurement reported "0 rows" for that reason
and nearly had the log path blamed for it.
"""
import io
import os
import sys
import json
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
# A directory argument points it at another copy of the engine, which is
# how it was checked against a version carrying the collapse bug -- a
# check worth being able to repeat, since this test reported a perfect
# score against that bug before the row count was compared.
ENGINE_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE
sys.path.insert(0, str(ENGINE_DIR))
os.chdir(HERE)
import freefire_engine as E

LOG = Path(r"D:/Games/Copy of (Sep 16 Update) OB55 PC Client"
           r"/Free Fire_64_Data/Debugger/debugger-2026-09-19T19-01-01.log")
MATCH = "2101314517399746560"
SLICES = 6


def main():
    if not LOG.exists():
        print("SKIP: no reference log at", LOG)
        return 0
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = ""
    print("engine under test:", E.__file__)
    print("cutoff cleared for the test (settings.debuggerStartAt)")

    roster = (json.load(io.open("freefire_state.json", encoding="utf-8"))
              .get("roster") or {})
    print("roster teams:", len(roster.get("teams") or []))

    lines = io.open(LOG, encoding="utf-8", errors="replace").readlines()
    start = next(i for i, l in enumerate(lines)
                 if "EventTypeEnterGame" in l and i > 40000)
    end = next(i for i, l in enumerate(lines)
               if "matchend matchid = " + MATCH in l)

    tmp = Path(tempfile.gettempdir()) / "ff_log_only.log"
    failures = 0
    counts = []
    print("\n%-6s %-6s %-7s %-7s %s" % ("at", "rows", "named", "kills", "alive bars"))
    for n in range(1, SLICES + 1):
        cut = start + int((end - start) * n / SLICES)
        io.open(tmp, "w", encoding="utf-8").writelines(lines[start:cut])
        E._live_match.clear()
        E._live_match.update(E.blank_live_match())
        E.read_debugger_events(tmp, 0, {}, live=E._live_match, emit=False)
        rows = E.link_live_teams(E._live_match, roster)["rows"]

        # Against the log's own list of teams, so this cannot be
        # satisfied by a table that quietly shrank.
        expected = len(E._live_match.get("teamNames") or {})
        counts.append(len(rows))
        named = sum(1 for r in rows if r.get("teamName"))
        kills = sum(1 for r in rows if r.get("elims") is not None)
        bars = sum(1 for r in rows if r.get("bars"))
        # Kills are published after the whistle too now: the guard that
        # withheld them existed only because the running score was
        # inflated, and a kill count has no placement in it to inflate.
        #
        # Pinned to gsKills by value, not just checked for presence --
        # "some number is there" is exactly what the inflated score
        # satisfied while being wrong on every dead row.
        gs_kills = E._live_match.get("gsKills") or {}
        scores = E._live_match.get("teamScores") or {}
        wrong_source = [r for r in rows
                        if r.get("gsTeam") is not None
                        and r.get("elims") != gs_kills.get(r["gsTeam"], 0)]
        # A squad with nobody standing is out, wipe line or no wipe line.
        alive_but_empty = [r for r in rows
                           if r.get("aliveCount") == 0 and not r.get("eliminated")]
        bad = (not rows or len(rows) != expected or expected < 2
               or named != len(rows) or bars != len(rows)
               or kills != len(rows) or wrong_source or alive_but_empty)
        failures += bool(bad)
        print("%-6s %-6d %-7s %-7s %s   %s"
              % ("%d/%d" % (n, SLICES), len(rows),
                 "%d/%d" % (named, len(rows)),
                 "%d/%d" % (kills, len(rows)),
                 "%d/%d" % (bars, len(rows)),
                 "FAIL" if bad else "ok"))
        if bad:
            for r in wrong_source:
                print("      %r elims=%s but gsKills=%s (score=%s) -- the "
                      "kill count is not coming from the kill narration"
                      % (r.get("teamName"), r.get("elims"),
                         gs_kills.get(r["gsTeam"], 0),
                         scores.get(r.get("teamId"))))
            for r in alive_but_empty:
                print("      %r has nobody standing but is published as "
                      "alive -- it will show on air as still in the game"
                      % r.get("teamName"))
            for r in rows:
                if not r.get("bars"):
                    print("      no alive bars for %r -- its squad did not "
                          "join through the roster" % r.get("teamName"))

    # The lobby does not change size mid-match, so neither should this.
    if len(set(counts)) > 1:
        failures += 1
        print("\nrow count changed through the match: %s -- the table is "
              "gaining or losing teams while it runs" % counts)

    print("\nRESULT:", "the log can fill the table unaided" if not failures
          else "%d problem(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
