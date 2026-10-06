"""The map graphic's data: zones, the plane's run, kills and each squad's
rotation for a finished match -- and the calibration that puts any game
position on the map images in overlay/assets/FF Maps.

WHERE EACH PIECE COMES FROM

  replay JSON  (Free Fire_64_Data/Replays/ReplayInfo_<id>_<stamp>.json)
      MapID, the start time, every kill (killer, victim, time), every
      death's position, and -- in the per-player KillEvents lists -- where
      the KILLER stood. See replay_json.py for how names are derived.

  debugger log (Free Fire_64_Data/Debugger/debugger-*.log)
      The zone, stage by stage: "Update m_ZoneStatus" gives the circle
      being closed to (InnerCenter, InnerRadius) and the phase. The
      plane's run ("airline start/end"). The map ("SendJoinMatch ...
      MapID=N"). All stamped with wall-clock time.

The two clocks agree: the replay's MatchDateTime is the second the log
writes SendJoinMatch and the airline (checked on a Bermuda match: both
21:33:46). Replay times are seconds from that instant, so a log line's
time minus MatchDateTime is on the same scale -- and late-game deaths do
fall inside the circle that was in force (33 of 40 on that match; the
rest are zone deaths, on the edge).

THE CALIBRATION was fitted from ~45,000 kill positions over 170 matches:
for each map, the scale and offset that land the most kills on the
buildings the map image draws (every image draws them in yellow), then
checked by eye -- kills sit on Shrines and Command Post on Kalahari, on
Moathouse and Brasilia on Purgatory, and so on. x runs right, z runs UP
the image, one scale for both axes. It is for these exact 600px images:
a different image of the same map needs its own fit.
"""

import datetime as _dt
import io
import json
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

import replay_json as rj

# id: name, image (under overlay/assets/), and pixel = a*world + c on the
# image's own size.
MAPS = {
    1:  {"name": "Bermuda",   "image": "FF Maps/600px-Free_Fire_Map_Bermuda_2023.png",
         "size": 600, "ax": 0.3350, "cx": 124.8, "az": -0.3350, "cz": 336.7},
    3:  {"name": "Purgatory", "image": "FF Maps/600px-Map_FF_Purgatory_allmode.jpeg",
         "size": 600, "ax": 0.3325, "cx": 329.9, "az": -0.3325, "cz": 245.1},
    4:  {"name": "Kalahari",  "image": "FF Maps/600px-Map_FF_Kalahari_allmode.jpeg",
         "size": 600, "ax": 0.4300, "cx": 300.3, "az": -0.4300, "cz": 299.3},
    22: {"name": "Nexterra",  "image": "FF Maps/600px-Map_FF_Nexterra_allmode.jpeg",
         "size": 600, "ax": 0.4850, "cx": 300.2, "az": -0.4850, "cz": 300.2},
    29: {"name": "Solara",    "image": "FF Maps/600px-Map_FF_Solara_allmode.jpg.jpeg",
         "size": 600, "ax": 0.4050, "cx": 284.0, "az": -0.4050, "cz": 311.8},
}


def to_uv(map_id, x, z):
    """A game position as a fraction of the map image, (0,0) top-left."""
    m = MAPS.get(int(map_id)) if map_id is not None else None
    if not m or x is None or z is None:
        return None
    return [round((m["ax"] * x + m["cx"]) / m["size"], 4),
            round((m["az"] * z + m["cz"]) / m["size"], 4)]


def radius_uv(map_id, r):
    m = MAPS.get(int(map_id)) if map_id is not None else None
    return round(abs(m["ax"]) * r / m["size"], 4) if m and r else 0


# ---------------------------------------------------------------- the log
ZONE_RE = re.compile(
    r"Update m_ZoneStatus\s*:\s*stageID\s*=\s*(?P<stage>\d+)\s*,\s*"
    r"OuterCenter\s*=\s*\((?P<ox>-?[\d.]+),\s*-?[\d.]+,\s*(?P<oz>-?[\d.]+)\)\s*,\s*"
    r"InnerCenter\s*=\s*\((?P<ix>-?[\d.]+),\s*-?[\d.]+,\s*(?P<iz>-?[\d.]+)\)\s*,\s*"
    r"InnerRadius\s*=\s*(?P<radius>[\d.]+)\s*,\s*"
    r"TimeSpanType\s*=\s*ZONE_TYPE_(?P<phase>\w+)")
AIR_START_RE = re.compile(r"airline start\((?P<x>-?[\d.]+),\s*-?[\d.]+,\s*(?P<z>-?[\d.]+)\)")
AIR_END_RE = re.compile(r"airline end\((?P<x>-?[\d.]+),\s*-?[\d.]+,\s*(?P<z>-?[\d.]+)\)")
MAP_RE = re.compile(r"SendJoinMatch\b.*?\bMapID=(?P<map>\d+)")
LOG_NAME_RE = re.compile(r"debugger-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})\.log$")


def _log_start(path):
    m = LOG_NAME_RE.search(path.name)
    if not m:
        return None
    return _dt.datetime.strptime("%s %s:%s:%s" % m.groups(), "%Y-%m-%d %H:%M:%S")


def logs_covering(debugger_dir, start, end):
    """The log file(s) a match's lines are in: the last one opened before
    it started, and any opened while it ran."""
    folder = Path(debugger_dir) if debugger_dir else None
    if not folder or not folder.is_dir():
        return []
    dated = sorted((s, p) for p in folder.glob("debugger-*.log")
                   for s in [_log_start(p)] if s)
    before = [p for s, p in dated if s <= start]
    during = [p for s, p in dated if start < s <= end]
    return before[-1:] + during


def read_zone_story(debugger_dir, start, end):
    """Zones, plane and map for the match that ran from start to end.

    Only lines that can matter are parsed: a log is ~100 MB and this runs
    on demand, so the cheap substring test comes first."""
    lo = (start - _dt.timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (end + _dt.timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
    story = {"zones": [], "airline": None, "mapId": None}
    last = None
    for path in logs_covering(debugger_dir, start, end):
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not ("m_ZoneStatus" in line or "airline" in line or "SendJoinMatch" in line):
                    continue
                ts = line[1:20]
                if not (lo <= ts <= hi):
                    continue
                t = (_dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - start).total_seconds()
                m = ZONE_RE.search(line)
                if m:
                    key = (m["stage"], m["phase"], m["ix"], m["iz"], m["radius"])
                    if key == last:
                        continue           # the client repeats each line
                    last = key
                    story["zones"].append({
                        "stage": int(m["stage"]), "phase": m["phase"], "t": t,
                        "x": float(m["ix"]), "z": float(m["iz"]), "r": float(m["radius"]),
                        "fromX": float(m["ox"]), "fromZ": float(m["oz"])})
                    continue
                m = AIR_START_RE.search(line)
                if m and t < 60:
                    story["airline"] = dict(story["airline"] or {}, start=[float(m["x"]), float(m["z"])])
                    continue
                m = AIR_END_RE.search(line)
                if m and t < 60:
                    story["airline"] = dict(story["airline"] or {}, end=[float(m["x"]), float(m["z"])])
                    continue
                m = MAP_RE.search(line)
                if m and t < 60:
                    story["mapId"] = int(m["map"])
    return story


def stage_circles(zone_lines):
    """One circle per stage: where it closes to, when it was announced and
    when it began to close. A stage's first line with a radius announces
    it; the moving final circle (RANDOM_PREMOVE/MOVE) is the last stage."""
    out = {}
    for z in zone_lines:
        if not z["r"]:
            continue
        s = out.get(z["stage"])
        if s is None:
            s = out[z["stage"]] = {"stage": z["stage"], "x": z["x"], "z": z["z"], "r": z["r"],
                                   "announced": z["t"], "closing": None}
        # A moving circle keeps its stage and moves its centre: the last
        # word is where it went.
        s["x"], s["z"], s["r"] = z["x"], z["z"], z["r"]
        if z["phase"] in ("SHRINK", "RANDOM_MOVE") and s["closing"] is None:
            s["closing"] = z["t"]
    return [out[k] for k in sorted(out)]


# ------------------------------------------------------------ the replay
KILLER_JOIN_SECONDS = 3.0


def killer_positions(data):
    """{(killer id, time): (x, z)} from the per-player KillEvents lists,
    which repeat each kill once per highlight category -- de-duplicated by
    killer and time. Not every file has them (they can be stripped)."""
    out = {}
    for player in data.get("PlayerHighlightInfos") or []:
        for e in player.get("KillEvents") or []:
            pos = e.get("position") or {}
            if pos.get("x") is None or e.get("TriggerPoint") is None:
                continue
            out[(e.get("PlayerID"), round(e["TriggerPoint"], 2))] = (pos["x"], pos.get("z"))
    return out


def _killer_spot(spots, pid, t):
    best, spot = KILLER_JOIN_SECONDS, None
    for (kid, kt), xz in spots.items():
        if kid == pid and abs(kt - t) < best:
            best, spot = abs(kt - t), xz
    return spot


def phase_of(t, circles):
    """1 once the first circle is drawn, 2 after the second, ... 0 before."""
    n = 0
    for c in circles:
        if c["announced"] is not None and c["announced"] <= t:
            n += 1
    return n


def build(replay_path, debugger_dir=None, squad_teams=None, story=None):
    """Everything the map graphic draws for one finished match.

    squad_teams: {squad number: team name}, decided by the caller (the
    engine knows the roster and the result file; this module does not).
    Positions stay in game units; to_uv() places them, so the map table
    above is the only place calibration lives."""
    data = rj.load(replay_path)
    names = rj.player_names(data)
    feed = rj.kills(data, names)
    start = _dt.datetime.strptime(data.get("MatchDateTime"), "%Y-%m-%d-%H-%M-%S")
    duration = float(data.get("GameTotalTime") or 0.0)
    if story is None:
        story = read_zone_story(debugger_dir, start, start + _dt.timedelta(seconds=duration))
    map_id = data.get("MapID") or story.get("mapId")
    circles = stage_circles(story.get("zones") or [])
    spots = killer_positions(data)
    squad_teams = squad_teams or {}

    def uv(x, z):
        return to_uv(map_id, x, z)

    kills = []
    for k in feed:
        ks = _killer_spot(spots, k["killerId"], k["time"])
        # Kept to positions and squads: this rides in every state_sync
        # while it is loaded, so names (which the map never prints) stay out.
        kills.append({
            "t": round(k["time"], 1),
            "ks": k["killerSquad"], "vs": k["victimSquad"],
            "at": uv(k["x"], k["z"]),                  # where the victim fell
            "from": uv(ks[0], ks[1]) if ks else None,  # where the shot came from
        })

    wipes = {w["squad"]: w for w in rj.squad_eliminations(data, names)}
    by_squad = defaultdict(list)            # squad -> [(t, (u, v))]
    for k in kills:
        if k["from"]:
            by_squad[k["ks"]].append((k["t"], k["from"]))
        if k["at"]:
            by_squad[k["vs"]].append((k["t"] + 3.0, k["at"]))

    squads = []
    for sq in sorted({pid >> rj.SQUAD_SHIFT for pid in names}):
        pts = sorted(by_squad.get(sq) or [])
        per_phase = defaultdict(list)
        for t, p in pts:
            per_phase[phase_of(t, circles)].append(p)
        path = []
        for ph in sorted(per_phase):
            ps = per_phase[ph]
            path.append({"phase": ph, "n": len(ps),
                         "at": [round(sum(p[0] for p in ps) / len(ps), 4),
                                round(sum(p[1] for p in ps) / len(ps), 4)]})
        w = wipes.get(sq) or {}
        squads.append({
            "squad": sq,
            "teamName": squad_teams.get(sq) or w.get("teamName") or "",
            "kills": sum(1 for k in kills if k["ks"] == sq),
            "place": w.get("place"),
            "out": round(w["time"], 1) if w.get("time") is not None else None,
            "path": path,
            "last": pts[-1][1] if pts else None,
        })
    squads.sort(key=lambda s: (s["place"] is None, s["place"] or 99))

    plane = None
    air = story.get("airline") or {}
    if air.get("start") and air.get("end"):
        plane = {"from": uv(*air["start"]), "to": uv(*air["end"])}

    m = MAPS.get(int(map_id)) if map_id is not None else None
    return {
        "matchId": str(data.get("MatchID") or ""),
        "playedAt": start.strftime("%Y-%m-%d %H:%M"),
        "duration": round(duration, 1),
        "mapId": map_id,
        "mapName": m["name"] if m else ("Map %s" % map_id),
        "image": m["image"] if m else "",
        "zones": [{"stage": c["stage"], "at": uv(c["x"], c["z"]), "r": radius_uv(map_id, c["r"]),
                   "meters": c["r"], "announced": round(c["announced"], 1),
                   "closing": round(c["closing"], 1) if c["closing"] is not None else None}
                  for c in circles],
        "plane": plane,
        "kills": kills,
        "squads": squads,
    }


# ------------------------------------------------------------- live zone
def live_view(zone):
    """liveOps.zone (the engine's live feed) placed on the map: every
    circle announced so far, which one is closing, the plane. Empty until
    the log has said which map this is."""
    zone = zone or {}
    map_id = zone.get("mapId")
    if map_id is None or int(map_id) not in MAPS:
        return None
    seen = {}
    for h in zone.get("history") or []:
        if h.get("radius"):
            seen[h.get("stage")] = h
    now = zone.get("now") or {}
    if now.get("radius"):
        seen[now.get("stage")] = dict(now)
    circles = [{"stage": s, "at": to_uv(map_id, *h["center"]), "r": radius_uv(map_id, h["radius"]),
                "meters": h["radius"], "phase": h.get("phase")}
               for s, h in sorted(seen.items())]
    air = zone.get("airline") or {}
    m = MAPS[int(map_id)]
    return {
        "mapId": int(map_id), "mapName": m["name"], "image": m["image"],
        "zones": circles,
        "stage": now.get("stage"), "phase": now.get("phase"),
        "plane": ({"from": to_uv(map_id, *air["start"]), "to": to_uv(map_id, *air["end"])}
                  if air.get("start") and air.get("end") else None),
    }


# --------------------------------------------------------------- listing
REPLAY_NAME_RE = re.compile(r"ReplayInfo_(?P<id>\d+)_(?P<stamp>[\d-]+)\.json$")


def list_replays(replays_dir, limit=30):
    """Newest first: match id, when, map -- the map read from each file,
    so only the newest `limit` are opened."""
    folder = Path(replays_dir) if replays_dir else None
    if not folder or not folder.is_dir():
        return []
    files = []
    for p in folder.glob("ReplayInfo_*.json"):
        m = REPLAY_NAME_RE.search(p.name)
        if m:
            files.append((m["stamp"], m["id"], p))
    files.sort(reverse=True)
    out = []
    for stamp, mid, p in files[:limit]:
        try:
            d = rj.load(p)
        except Exception:
            continue
        map_id = d.get("MapID")
        out.append({"matchId": mid, "playedAt": stamp, "mapId": map_id,
                    "mapName": MAPS.get(map_id, {}).get("name", "Map %s" % map_id),
                    "room": d.get("RoomName") or "", "path": str(p)})
    return out


def find_replay(replays_dir, match_id=None):
    folder = Path(replays_dir) if replays_dir else None
    if not folder or not folder.is_dir():
        return None
    pattern = "ReplayInfo_%s_*.json" % match_id if match_id else "ReplayInfo_*.json"
    found = sorted(folder.glob(pattern), key=lambda p: p.name.split("_")[-1])
    return found[-1] if found else None


def squad_teams_from_result(names, result_teams):
    """{squad: team name}: each squad's players' names looked up in the
    result file's teams, the majority winning."""
    owner = {}
    for t in result_teams or []:
        for p in t.get("players") or []:
            for key in (p.get("name"), p.get("fileName")):
                if key:
                    owner[key.strip().upper()] = t.get("teamName") or ""
    votes = defaultdict(Counter)
    for pid, n in (names or {}).items():
        team = owner.get((n or "").strip().upper())
        if team:
            votes[pid >> rj.SQUAD_SHIFT][team] += 1
    return {sq: c.most_common(1)[0][0] for sq, c in votes.items()}


# ------------------------------------------------------------ drop spots
DROP_WINDOW_SECONDS = 150.0


def drops_from_match(view, within=DROP_WINDOW_SECONDS):
    """{team name: [u, v]}: where each squad was first seen fighting or
    falling, if that was early enough to be its landing area.

    A starting point for the drop-spot graphic, not a measurement of the
    landing itself -- the replay JSON has no positions before the first
    fight, so a squad that landed quietly has nothing here and is left for
    the operator to place."""
    first = {}
    for k in (view or {}).get("kills") or []:
        if k.get("t") is None or k["t"] > within:
            continue
        for sq, at in ((k.get("ks"), k.get("from")), (k.get("vs"), k.get("at"))):
            if sq is None or not at:
                continue
            if sq not in first or k["t"] < first[sq][0]:
                first[sq] = (k["t"], at)
    out = {}
    for s in (view or {}).get("squads") or []:
        name = (s.get("teamName") or "").strip()
        if name and s.get("squad") in first:
            out[name.upper()] = first[s["squad"]][1]
    return out
