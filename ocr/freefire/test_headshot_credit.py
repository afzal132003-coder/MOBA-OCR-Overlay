"""Headshot counts must match an independent pairing of the log.

Run it with no arguments:

    python ocr\freefire\test_headshot_credit.py

WHAT THIS GUARDS

The client writes the headshot flag on a separate line from the kill or
knock it describes, about a millisecond LATER. Reading the flag at event
time therefore always misses it -- which is why the kill feed's headshot
field read False on every knock for as long as it has existed. The engine
credits the trace backwards instead, to the last event for that
killer-victim pair.

Two ways that goes wrong, and both inflate a leaderboard rather than
emptying it, so neither is obvious on screen:

  * Counting every trace. A trace marks knocks AND kills, so a player who
    knocks an opponent and then finishes them produces two. Added
    together that credits one opponent twice.
  * Letting one event take more than one trace.

MEASURED, NOT ASSUMED

Across a full match: 1,106 traces, every one within 50ms of its own
event, none left over. Widening the window to three seconds matched not
one extra. So the window is not a tuned threshold -- the trace sits next
to its event or not at all.

This test does two things, because the first alone proved toothless.

1. It re-pairs the real log from scratch, by a different method from the
   engine's (a forward scan matching each event to its nearest unused
   trace, rather than the engine's backward credit), and requires the two
   to agree per player.

2. It feeds a HAND-BUILT log through the engine. The real match contains
   no pair that is traced twice with the flag set, so a naive
   count-every-trace implementation scores identically on it -- checked
   by building that naive version and running this test against it, which
   passed. Real data agreeing is not evidence the dedup works, so the
   awkward cases are constructed: a repeated trace, a trace with no event
   before it, and a knock followed by a kill on the same victim.

An earlier count of mine was wrong because it compared cutoff-filtered
traces against unfiltered knocks; both sides here read the same lines.
"""
import io
import os
import re
import sys
import json
import collections
import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
ENGINE_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE
sys.path.insert(0, str(ENGINE_DIR))
os.chdir(HERE)
import freefire_engine as E

LOG = Path(r"D:/Games/Copy of (Sep 16 Update) OB55 PC Client"
           r"/Free Fire_64_Data/Debugger/debugger-2026-09-21T00-51-46.log")
TRACE = re.compile(r"PlayKnockDownGunTrace killer=(\d+) victim=(\d+) headshot=(\w+)")
KNOCK = re.compile(r"Player '(\d+)' Knock Down, by '(\d+)'")
KILL = re.compile(r"Player (\d+) Dead, killed by (\d+)")


def independent(lines):
    """Pairs each event to its nearest unused trace, scanning forward."""
    traces, events = [], []
    for line in lines:
        t = E._debugger_epoch(line[1:24])
        if t is None:
            continue
        m = TRACE.search(line)
        if m:
            traces.append([t, m.group(1), m.group(2), m.group(3) == "True", False])
            continue
        m = KNOCK.search(line)
        if m:
            events.append([t, m.group(2), m.group(1), "knock"])
            continue
        m = KILL.search(line)
        if m and m.group(1) != m.group(2):
            events.append([t, m.group(2), m.group(1), "kill"])
    by_pair = collections.defaultdict(list)
    for i, tr in enumerate(traces):
        by_pair[(tr[1], tr[2])].append(i)
    hs = {"kill": collections.Counter(), "knock": collections.Counter()}
    for ev in events:
        best, bestd = None, None
        for i in by_pair.get((ev[1], ev[2]), ()):
            tr = traces[i]
            if tr[4]:
                continue
            d = abs(tr[0] - ev[0])
            if d <= E.FREEFIRE_HEADSHOT_WINDOW and (bestd is None or d < bestd):
                best, bestd = i, d
        if best is not None:
            traces[best][4] = True
            if traces[best][3]:
                hs[ev[3]][ev[1]] += 1
    return hs, sum(1 for tr in traces if not tr[4])


SYNTH = """[2026-09-21 01:00:00.000][1][1] OnTeamScoreInited -> TeamName: ALPHA TeamID: 1
[2026-09-21 01:00:01.000][1][2] [UIModelSpectator] AddPlayer gsTeam:1 playerId:11 ign:SHOOTER
[2026-09-21 01:00:02.000][1][3] Player '22' Knock Down, by '11'
[2026-09-21 01:00:02.001][1][4] @zwj PlayKnockDownGunTrace killer=11 victim=22 headshot=True
[2026-09-21 01:00:02.004][1][5] @zwj PlayKnockDownGunTrace killer=11 victim=22 headshot=True
[2026-09-21 01:00:03.000][1][6] Player 22 Dead, killed by 11
[2026-09-21 01:00:03.001][1][7] @zwj PlayKnockDownGunTrace killer=11 victim=22 headshot=True
[2026-09-21 01:00:09.000][1][8] @zwj PlayKnockDownGunTrace killer=11 victim=33 headshot=True
"""


def synthetic():
    """The cases the real log does not contain.

    One knock and one kill on the same victim, each with its own trace,
    plus a repeat of the knock's trace and a stray trace with no event
    before it. Correct answer: ONE headshot knock and ONE headshot kill.
    The repeat and the stray must both count for nothing.
    """
    import tempfile
    path = Path(tempfile.gettempdir()) / "ff_hs_synth.log"
    io.open(path, "w", encoding="utf-8").write(SYNTH)
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = ""
    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    E.read_debugger_events(path, 0, {}, live=E._live_match, emit=False)
    lm = E._live_match
    got_k = sum((lm.get("playerHsKills") or {}).values())
    got_n = sum((lm.get("playerHsKnocks") or {}).values())
    ok = (got_k == 1 and got_n == 1)
    print("synthetic: headshot kills=%d (want 1), headshot knocks=%d (want 1)  %s"
          % (got_k, got_n, "ok" if ok else "FAIL"))
    if not ok:
        print("    a repeated trace or a trace with no event was counted")
    return 0 if ok else 1


def main():
    if not LOG.exists():
        print("SKIP: no reference log at", LOG)
        return 0
    print("engine under test:", E.__file__)
    cutoff = ((json.load(io.open("freefire_state.json", encoding="utf-8"))
               .get("settings") or {}).get("debuggerStartAt") or "")
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = cutoff

    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    E.read_debugger_events(LOG, 0, {}, live=E._live_match, emit=False)
    lm = E._live_match

    # The engine rolls over between matches, so it holds only the LAST
    # one. The independent pass is given the same stretch: from the final
    # rollover point onward, found the same way the engine finds it.
    lines = [l for l in io.open(LOG, encoding="utf-8", errors="replace")
             if l[:1] == "[" and (not cutoff or l[1:20] >= cutoff)]
    starts = [i for i, l in enumerate(lines) if "OnTeamScoreInited" in l]
    last_run = 0
    for i in range(1, len(starts)):
        if starts[i] - starts[i - 1] > 50:
            last_run = starts[i]
    lines = lines[last_run:]

    hs, leftover = independent(lines)
    mine_k = {k: v for k, v in (lm.get("playerHsKills") or {}).items() if v}
    mine_n = {k: v for k, v in (lm.get("playerHsKnocks") or {}).items() if v}
    theirs_k = {k: v for k, v in hs["kill"].items() if v}
    theirs_n = {k: v for k, v in hs["knock"].items() if v}

    print("engine      : %d headshot kills, %d headshot knocks"
          % (sum(mine_k.values()), sum(mine_n.values())))
    print("independent : %d headshot kills, %d headshot knocks"
          % (sum(theirs_k.values()), sum(theirs_n.values())))
    print("traces the independent pass could not place:", leftover)

    failures = 0
    for label, mine, theirs in (("kills", mine_k, theirs_k),
                                ("knocks", mine_n, theirs_n)):
        for pid in sorted(set(mine) | set(theirs)):
            a, b = mine.get(pid, 0), theirs.get(pid, 0)
            if a != b:
                failures += 1
                print("  MISMATCH %-6s player %s: engine %d, independent %d"
                      % (label, pid, a, b))
    # A leaderboard of nothing would agree with a leaderboard of nothing.
    if sum(mine_k.values()) + sum(mine_n.values()) == 0:
        failures += 1
        print("  the engine counted no headshots at all -- nothing was tested")

    print()
    failures += synthetic()

    print("\nRESULT:", "counts verified two ways" if not failures
          else "%d problem(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
