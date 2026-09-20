"""The zone feed must survive a match rollover and not repeat itself.

Run it with no arguments:

    python ocr\freefire\test_zone_feed.py

WHAT THIS GUARDS

Two things that both look like "no data" rather than "wrong data", which
is why neither is obvious on a graphic:

  * THE FLIGHT PATH BEING THROWN AWAY. The client writes the plane's run
    about six seconds BEFORE the first team-name line of the same match.
    The rollover fires on that team line and clears the live match, so
    the airline was captured and then immediately discarded -- every
    match, forever. It has to be carried across, like the squad list is.
    Both rollovers do this, and both had the bug.

  * THE HISTORY FILLING WITH DUPLICATES. The client repeats the same
    m_ZoneStatus line several times per stage. Recording each one would
    turn a nine-stage match into dozens of identical entries and wake the
    publish path on every repeat.

The zone numbers themselves are checked for shape, not for exact values:
stages must not go backwards, and a circle must not grow within a stage.
Those hold for any match, where specific coordinates hold only for one.

One exception, found by this test rejecting a correct engine: the opening
STABLE entry carries radius 0, meaning no circle has been drawn yet. The
first real circle is therefore not the circle growing.
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

# A previous match that ends, then a new one that opens with its flight
# path BEFORE its team lines -- the ordering that lost the airline.
SYNTH = """\
[2026-09-21 01:00:00.000][1][1] OnTeamScoreInited -> TeamName: ALPHA TeamID: 1
[2026-09-21 01:00:01.000][1][2] OnTeamScoreChanged -> TeamID: 1 TeamScore: 3
[2026-09-21 01:00:02.000][1][3] Player 22 Dead, killed by 11
[2026-09-21 01:10:00.000][1][4] airline start(-775.6540, 370.0000, 160.9630), 40
[2026-09-21 01:10:00.000][1][5] airline end(573.3790, 370.0000, -76.9080)
[2026-09-21 01:10:06.000][1][6] OnTeamScoreInited -> TeamName: BRAVO TeamID: 2
[2026-09-21 01:10:07.000][1][7] [InitByMessage] Update m_ZoneStatus : stageID = 0, OuterCenter = (0.00, 0.00, 0.00), InnerCenter = (10.00, 0.00, -20.00), InnerRadius = 500, TimeSpanType = ZONE_TYPE_PRE_SHRINK
[2026-09-21 01:10:08.000][1][8] [InitByMessage] Update m_ZoneStatus : stageID = 0, OuterCenter = (0.00, 0.00, 0.00), InnerCenter = (10.00, 0.00, -20.00), InnerRadius = 500, TimeSpanType = ZONE_TYPE_PRE_SHRINK
[2026-09-21 01:10:09.000][1][9] [InitByMessage] Update m_ZoneStatus : stageID = 0, OuterCenter = (0.00, 0.00, 0.00), InnerCenter = (10.00, 0.00, -20.00), InnerRadius = 500, TimeSpanType = ZONE_TYPE_SHRINK
"""


def read(path):
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = ""
    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    E.read_debugger_events(path, 0, {}, live=E._live_match, emit=False)
    return E._live_match


def synthetic():
    path = Path(tempfile.gettempdir()) / "ff_zone_synth.log"
    io.open(path, "w", encoding="utf-8").write(SYNTH)
    lm = read(path)
    fails = 0

    air = lm.get("airline") or {}
    if not (air.get("start") and air.get("end")):
        fails += 1
        print("  FAIL: the flight path did not survive the rollover -- got %r" % (air,))
    else:
        print("  ok: flight path survives a rollover -> start=%s end=%s"
              % (air["start"], air["end"]))

    hist = lm.get("zoneHistory") or []
    # Three lines, two of them identical: two entries, not three.
    if len(hist) != 2:
        fails += 1
        print("  FAIL: 3 zone lines with one repeat gave %d history entries, want 2"
              % len(hist))
        for h in hist:
            print("      %s" % h)
    else:
        print("  ok: a repeated zone line is not recorded twice")
    return fails


def real():
    if not LOG.exists():
        print("  SKIP: no reference log at", LOG)
        return 0
    lm = read(LOG)
    hist = lm.get("zoneHistory") or []
    fails = 0
    print("  %d zone changes, %d stages"
          % (len(hist), len({h["stage"] for h in hist})))
    if len(hist) < 3:
        fails += 1
        print("  FAIL: only %d zone changes read -- the feed is not working" % len(hist))
    for a, b in zip(hist, hist[1:]):
        if b["stage"] < a["stage"]:
            fails += 1
            print("  FAIL: stage went backwards, %s then %s" % (a["stage"], b["stage"]))
        # radius 0 is the opening STABLE entry: no circle has been drawn
        # yet, so the first real one is not the circle "growing".
        if (a["radius"] and b["radius"] > a["radius"]
                and b["stage"] == a["stage"]):
            fails += 1
            print("  FAIL: the circle grew within stage %s, %s then %s"
                  % (a["stage"], a["radius"], b["radius"]))
    if any(a == b for a, b in zip(hist, hist[1:])):
        fails += 1
        print("  FAIL: consecutive identical entries in the history")
    if not fails and hist:
        print("  ok: stages only advance, circles only shrink, no repeats")
        print("  final circle: stage %s, radius %s" % (hist[-1]["stage"], hist[-1]["radius"]))
    return fails


def main():
    print("engine under test:", E.__file__)
    print("synthetic:")
    fails = synthetic()
    print("real match:")
    fails += real()
    print("\nRESULT:", "zone feed holds up" if not fails else "%d problem(s)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
