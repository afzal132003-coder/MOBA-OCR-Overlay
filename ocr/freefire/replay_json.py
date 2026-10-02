"""Read a Free Fire ReplayInfo_*.json into something a map can be drawn from.

WHY THIS EXISTS, and why there is no .bin decoder beside it.

A finished match leaves two files in Free Fire_64_Data/Replays:

    ReplayInfo_<id>_<stamp>.bin     14-21 MB, undocumented binary
    ReplayInfo_<id>_<stamp>.json      0.3 MB, plain JSON

The binary holds the full replay -- every player's position on every
tick, which is what drives a moving-dot map. Decoding it is real work
against an undocumented format.

The JSON turns out to hold most of what a broadcast actually wants, and
it needs no decoding at all:

  * Every kill, with killer and victim, to the millisecond.
  * Every player's NAME, derived rather than stored (see below).
  * Every death's POSITION on the map.
  * The squad each player belongs to, from the id.
  * Match metadata: map, duration, player count, room name.

So this reads the JSON. What it cannot give is continuous movement --
the path a squad took between fights lives only in the .bin.

HOW THE NAMES ARE DERIVED, because it is not obvious and it took a
wrong turn first. The JSON never states "player 67108879 is called X".
It has an `Events` list where each kill writes TWO entries at the same
instant:

    Event 3  PlayerID = the KILLER   SParam = the victim's name
    Event 4  PlayerID = the VICTIM   SParam = the killer's name

Each one names the OTHER party. Pair them by timestamp and both ids are
named at once. Collecting SParam per PlayerID directly -- the obvious
first attempt -- gives 43 of 48 players several contradictory names,
because it reads every id as if SParam described it.

On the reference match this derives all 48 names with zero conflicts.
"""

import io
import json
import os
from collections import Counter, defaultdict

# The squad is packed into the top byte of the player id, the same shift
# freefire_engine.py already uses for the live log (GS_TEAM_SHIFT).
SQUAD_SHIFT = 24

# A death is matched to the kill that caused it by time, and the gap is
# not noise -- it is a constant. Measured across every death of the
# reference match: minimum 2.87s, median 3.00, maximum 3.00. The client
# removes the body a fixed three seconds after the kill lands.
#
# So the window only has to straddle three. It was first set to 2.0 on
# the assumption the two timestamps would nearly agree, which joined
# exactly nothing and made the feed look positionless.
DEATH_JOIN_SECONDS = 6.0

KILL_EVENT, DEATH_EVENT = 3, 4


def load(path):
    """The file, as a dict. Written with a BOM, which json.load rejects."""
    with io.open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def player_names(data):
    """{player id: name}, derived from the kill pairs. See the module note."""
    at_time = defaultdict(dict)
    for e in data.get("Events") or []:
        if e.get("Event") in (KILL_EVENT, DEATH_EVENT):
            at_time[round(e.get("Time", 0.0), 3)][e["Event"]] = e

    votes = defaultdict(Counter)
    for got in at_time.values():
        killer, victim = got.get(KILL_EVENT), got.get(DEATH_EVENT)
        if not killer or not victim:
            continue
        # Each entry names the OTHER party, so they are crossed over.
        if isinstance(victim.get("SParam"), str):
            votes[killer["PlayerID"]][victim["SParam"]] += 1
        if isinstance(killer.get("SParam"), str):
            votes[victim["PlayerID"]][killer["SParam"]] += 1

    # Counted rather than last-wins so one odd entry cannot rename a
    # player who was named consistently thirty times.
    return {pid: c.most_common(1)[0][0] for pid, c in votes.items() if c}


def kills(data, names=None):
    """Every kill: who, whom, when -- and where the victim fell.

    The position comes from the victim's DeadEvent, matched by time.
    DeadEvents are one-per-death and carry a clean position; the
    KillEvents lists look richer but are highlight-reel segments that
    repeat across players' reels and outnumber the real kills three to
    one, so they are not used for the feed.
    """
    names = names if names is not None else player_names(data)

    deaths = []
    for player in data.get("PlayerHighlightInfos") or []:
        for e in player.get("DeadEvents") or []:
            pos = e.get("position") or {}
            deaths.append({
                "pid": e.get("PlayerID"),
                "time": e.get("TriggerPoint"),
                "x": pos.get("x"), "y": pos.get("y"), "z": pos.get("z"),
            })

    at_time = defaultdict(dict)
    for e in data.get("Events") or []:
        if e.get("Event") in (KILL_EVENT, DEATH_EVENT):
            at_time[round(e.get("Time", 0.0), 3)][e["Event"]] = e

    out = []
    for t in sorted(at_time):
        got = at_time[t]
        killer, victim = got.get(KILL_EVENT), got.get(DEATH_EVENT)
        if not killer or not victim:
            continue
        vid = victim["PlayerID"]
        spot = None
        best = DEATH_JOIN_SECONDS
        for dth in deaths:
            if dth["pid"] != vid or dth["time"] is None:
                continue
            # The death FOLLOWS its kill, so a death before this one
            # belongs to an earlier knock, not to this.
            gap = dth["time"] - t
            if -0.5 <= gap < best:
                best, spot = gap, dth
        out.append({
            "time": t,
            "killerId": killer["PlayerID"],
            "killer": names.get(killer["PlayerID"], ""),
            "killerSquad": killer["PlayerID"] >> SQUAD_SHIFT,
            "victimId": vid,
            "victim": names.get(vid, ""),
            "victimSquad": vid >> SQUAD_SHIFT,
            "x": spot["x"] if spot else None,
            "z": spot["z"] if spot else None,
        })
    return out


def squads(data, names=None):
    """{squad number: [player names]}, from the ids."""
    names = names if names is not None else player_names(data)
    out = defaultdict(list)
    for pid, name in sorted(names.items()):
        out[pid >> SQUAD_SHIFT].append(name)
    return dict(out)


def summary(path):
    """Everything a map or a timeline needs, from one file."""
    data = load(path)
    names = player_names(data)
    feed = kills(data, names)
    placed = [k for k in feed if k["x"] is not None]

    tally = Counter(k["killerSquad"] for k in feed)
    return {
        "file": os.path.basename(path),
        "matchId": str(data.get("MatchID") or ""),
        "map": data.get("MapID"),
        "room": data.get("RoomName") or "",
        "playedAt": data.get("MatchDateTime") or "",
        "durationSeconds": round(data.get("GameTotalTime") or 0.0, 1),
        "playerCount": data.get("PlayerCount"),
        "players": names,
        "squads": squads(data, names),
        "kills": feed,
        "killsPlaced": len(placed),
        "killsBySquad": dict(tally),
        "bounds": _bounds(placed),
    }


def _bounds(placed):
    """The ground-plane extent of the positions, for scaling a map image."""
    if not placed:
        return None
    xs = [k["x"] for k in placed]
    zs = [k["z"] for k in placed]
    return {"minX": min(xs), "maxX": max(xs), "minZ": min(zs), "maxZ": max(zs)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python replay_json.py <ReplayInfo_*.json>")
        raise SystemExit(2)
    s = summary(sys.argv[1])
    print("%s  map %s  %s players  %.0fs  %s"
          % (s["matchId"], s["map"], s["playerCount"], s["durationSeconds"], s["room"]))
    print("%d players named, %d squads, %d kills (%d with a position)"
          % (len(s["players"]), len(s["squads"]), len(s["kills"]), s["killsPlaced"]))
    print()
    for k in s["kills"][:10]:
        where = ("at %7.1f, %7.1f" % (k["x"], k["z"])) if k["x"] is not None else "no position"
        print("  %7.2fs  %-18s (sq %2d)  ->  %-18s (sq %2d)  %s"
              % (k["time"], k["killer"][:18], k["killerSquad"],
                 k["victim"][:18], k["victimSquad"], where))
