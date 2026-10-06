"""The map graphic's data: calibration, the zone story read from a log,
a finished match built from a replay, and the live match knowing its map.

Run: python ocr/freefire/test_map_analysis.py
"""
import datetime as dt
import io
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)
import map_analysis as ma
import replay_json as rj
import freefire_engine as E

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


ZONE = ("[{ts}][1][7] [InitByMessage] Update m_ZoneStatus : stageID = {s}, OuterCenter = "
        "({ox}, 0.00, {oz}), InnerCenter = ({ix}, 0.00, {iz}), InnerRadius = {r}, "
        "TimeSpanType = ZONE_TYPE_{ph}, Received TimeSpanType = ZONE_TYPE_{ph}\n")


def synth_log(folder):
    lines = [
        "[2026-10-06 21:33:40.000][1][1] [SceneEdit] Room Proto_MATCHMAKINGSUSS_NTF recv, GameMode: 1, MapId: 4\n",
        "[2026-10-06 21:33:46.140][1][2] [UGC] SendJoinMatch, EnableUGC=False,MapID=4,GameMode=1,MatchMode=3,\n",
        "[2026-10-06 21:33:46.200][1][3] airline start(195.0320, 350.0000, -987.6390), 50\n",
        "[2026-10-06 21:33:46.200][1][3] airline end(683.2170, 350.0000, 834.2920)\n",
        "[2026-10-06 21:33:52.000][1][4] OnTeamScoreInited -> TeamName: BRAVO TeamID: 2\n",
        ZONE.format(ts="2026-10-06 21:33:57.000", s=0, ox=544, oz=4, ix=544, iz=4, r=0, ph="STABLE"),
        ZONE.format(ts="2026-10-06 21:35:16.000", s=0, ox=544, oz=4, ix=368.97, iz=122.05, r=550, ph="PRE_SHRINK"),
        ZONE.format(ts="2026-10-06 21:35:17.000", s=0, ox=544, oz=4, ix=368.97, iz=122.05, r=550, ph="PRE_SHRINK"),
        ZONE.format(ts="2026-10-06 21:37:16.000", s=0, ox=544, oz=4, ix=368.97, iz=122.05, r=550, ph="SHRINK"),
        ZONE.format(ts="2026-10-06 21:41:16.000", s=1, ox=368.97, oz=122.05, ix=352.85, iz=200.88, r=300, ph="PRE_SHRINK"),
        ZONE.format(ts="2026-10-06 21:41:56.000", s=1, ox=368.97, oz=122.05, ix=352.85, iz=200.88, r=300, ph="SHRINK"),
        ZONE.format(ts="2026-10-06 21:50:21.000", s=5, ox=398, oz=247, ix=457.67, iz=239.56, r=30, ph="RANDOM_PREMOVE"),
        ZONE.format(ts="2026-10-06 21:50:51.000", s=5, ox=398, oz=247, ix=460.00, iz=240.00, r=30, ph="RANDOM_MOVE"),
        # the next match: must not be read into this one
        ZONE.format(ts="2026-10-06 22:30:00.000", s=0, ox=1, oz=1, ix=2, iz=2, r=550, ph="PRE_SHRINK"),
    ]
    path = Path(folder) / "debugger-2026-10-06T15-05-29.log"
    io.open(path, "w", encoding="utf-8").writelines(lines)
    # an older log that must not be opened for this match
    io.open(Path(folder) / "debugger-2026-10-05T10-00-00.log", "w").write(
        ZONE.format(ts="2026-10-05 11:00:00.000", s=0, ox=0, oz=0, ix=9, iz=9, r=550, ph="PRE_SHRINK"))
    return path


def main():
    print("\ncalibration")
    for mid, m in ma.MAPS.items():
        u, v = ma.to_uv(mid, 0, 0)
        check("%s: the world origin lands on the image" % m["name"], 0 <= u <= 1 and 0 <= v <= 1, "%.3f,%.3f" % (u, v))
    a, b = ma.to_uv(4, 0, 0), ma.to_uv(4, 0, 100)
    check("z runs up the image", b[1] < a[1])
    check("a radius scales with the map", abs(ma.radius_uv(22, 600) - 0.485) < 0.001)
    check("an unknown map places nothing", ma.to_uv(99, 1, 1) is None)

    print("\nthe zone story from the log")
    tmp = tempfile.mkdtemp()
    synth_log(tmp)
    start = dt.datetime(2026, 10, 6, 21, 33, 46)
    end = start + dt.timedelta(seconds=1025)
    logs = ma.logs_covering(tmp, start, end)
    check("only the log the match is in is opened", [p.name for p in logs] == ["debugger-2026-10-06T15-05-29.log"],
          str([p.name for p in logs]))
    story = ma.read_zone_story(tmp, start, end)
    check("the map is read from SendJoinMatch", story["mapId"] == 4, str(story["mapId"]))
    check("the plane's run is read", story["airline"] == {"start": [195.032, -987.639], "end": [683.217, 834.292]},
          str(story["airline"]))
    check("a repeated zone line is kept once, the next match's not at all",
          len(story["zones"]) == 7, str(len(story["zones"])))
    circles = ma.stage_circles(story["zones"])
    check("one circle per stage with a radius", [c["stage"] for c in circles] == [0, 1, 5])
    c0 = circles[0]
    check("announced and closing times are seconds from the start",
          (c0["announced"], c0["closing"]) == (90.0, 210.0), str((c0["announced"], c0["closing"])))
    check("a moving final circle ends where it moved to", (circles[-1]["x"], circles[-1]["z"]) == (460.0, 240.0))
    check("phases count circles announced", (ma.phase_of(60, circles), ma.phase_of(100, circles),
                                             ma.phase_of(500, circles)) == (0, 1, 2))

    print("\na finished match from the replay fixture")
    fixture = HERE / "fixtures" / "replay_sample.json"
    data = rj.load(fixture)
    names = rj.player_names(data)
    st = dt.datetime.strptime(data["MatchDateTime"], "%Y-%m-%d-%H-%M-%S")
    story2 = {"mapId": None, "airline": {"start": [-500.0, -500.0], "end": [500.0, 500.0]},
              "zones": [{"stage": 0, "phase": "PRE_SHRINK", "t": 90.0, "x": 0.0, "z": 0.0, "r": 550.0, "fromX": 0, "fromZ": 0},
                        {"stage": 1, "phase": "PRE_SHRINK", "t": 450.0, "x": 50.0, "z": 50.0, "r": 300.0, "fromX": 0, "fromZ": 0}]}
    some = {sorted({pid >> rj.SQUAD_SHIFT for pid in names})[0]: "ALPHA ESPORTS"}
    out = ma.build(fixture, None, some, story=story2)
    feed = rj.kills(data, names)
    check("the map comes from the replay", out["mapId"] == 22 and out["mapName"] == "Nexterra")
    check("every kill with a death position is placed",
          sum(1 for k in out["kills"] if k["at"]) == sum(1 for k in feed if k["x"] is not None))
    check("every squad is listed", len(out["squads"]) == len({pid >> 24 for pid in names}), str(len(out["squads"])))
    check("a squad named by the caller keeps that name", any(s["teamName"] == "ALPHA ESPORTS" for s in out["squads"]))
    check("squads are in finishing order", [s["place"] for s in out["squads"]][:3] == [1, 2, 3],
          str([s["place"] for s in out["squads"]][:3]))
    paths = [p for s in out["squads"] for p in s["path"]]
    check("rotation points carry their zone phase", paths and all(p["phase"] in (0, 1, 2) for p in paths))
    check("the plane is placed", out["plane"]["from"] == ma.to_uv(22, -500, -500))
    check("zones are placed with their radius", out["zones"][0]["r"] == ma.radius_uv(22, 550))
    check("no player names ride along", not any("killer" in k or "victim" in k for k in out["kills"]))

    print("\nnaming squads")
    res = [{"teamName": "ALPHA", "players": [{"name": "Ace"}, {"name": "Bee"}]},
           {"teamName": "BRAVO", "players": [{"name": "Cee"}]}]
    got = ma.squad_teams_from_result({(1 << 24) + 1: "ace", (1 << 24) + 2: "Bee", (2 << 24) + 1: "CEE",
                                      (3 << 24) + 1: "Nobody"}, res)
    check("players' names decide their squad's team, case-blind", got == {1: "ALPHA", 2: "BRAVO"}, str(got))

    print("\nthe live match knows its map")
    log = Path(tmp) / "debugger-2026-10-06T15-05-29.log"
    E.server_state.setdefault("settings", {})["debuggerStartAt"] = ""
    E._live_match.clear()
    E._live_match.update(E.blank_live_match())
    E._live_match["gsKills"] = {1: 3}       # a match already counted, so a rollover fires
    E.read_debugger_events(log, 0, {}, live=E._live_match, emit=False)
    check("a rollover did fire (the old match's counts are gone)", not E._live_match.get("gsKills"),
          str(E._live_match.get("gsKills")))
    check("mapId is read and survives the rollover", E._live_match.get("mapId") == 4, str(E._live_match.get("mapId")))
    view = ma.live_view({"mapId": 4, "now": {"stage": 1, "phase": "SHRINK", "center": [352.85, 200.88], "radius": 300.0},
                         "history": [{"stage": 0, "center": [368.97, 122.05], "radius": 550.0}],
                         "airline": {"start": [195.0, -987.6], "end": [683.2, 834.3]}})
    check("the live view places every circle so far", [z["stage"] for z in view["zones"]] == [0, 1]
          and view["mapName"] == "Kalahari" and view["plane"])
    check("no map, no live view", ma.live_view({"mapId": None}) is None)

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
