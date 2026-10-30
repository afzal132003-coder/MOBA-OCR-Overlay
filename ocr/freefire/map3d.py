"""THE MAP IN 3D: one finished match as the 3D map page draws it
(overlay/freefire_map3d.html), and the ground's height under each map,
learned from where players have walked.

The replay .bin gives every player's position in three dimensions --
x, z across the map and y up, in metres (bin_decode). Nothing here comes
from the game's own map files: the ground is the map image, draped over
heights the matches themselves measured.

TERRAIN. A map's ground is never in the data as such, only the players
standing on it -- and on rooftops, in buildings, mid-jump. So each cell of
a grid over the map takes a LOW percentile of the heights walked in it
(the street, not the roof), the cells nobody has walked are filled from
their neighbours, and the whole is lightly smoothed. It fills in as more
matches are decoded; each map's is kept in terrain/<map id>.json and
rebuilt when a new match of that map has been decoded."""
import json
from pathlib import Path

import numpy as np

TERRAIN_DIR = Path(__file__).parent / "terrain"
TERRAIN_N = 96                  # cells across the map image (~15 m each)
TERRAIN_VERSION = 2
TERRAIN_PERCENTILE = 25         # the ground under the rooftops
TERRAIN_MIN_SAMPLES = 3
TERRAIN_DECAY = 0.85            # each ring filled out from walked ground sinks this far toward sea level
GROUND_MAX = 150                # above this a player is in the air (bin_decode's line)
STEP = 2.0                      # seconds between the track samples sent


def uv_of(cal, x, z):
    """Metres -> fractions across and down the map image."""
    return ((cal["ax"] * np.asarray(x, float) + cal["cx"]) / cal["size"],
            (cal["az"] * np.asarray(z, float) + cal["cz"]) / cal["size"])


def map_metres(cal):
    """The map image's width in metres."""
    return float(cal["size"]) / abs(float(cal["ax"]))


def ground_samples(decoded, cal):
    """(u, v, height) for every sample a player spent on the ground: after
    landing, below the air line."""
    us, vs, hs = [], [], []
    for p in decoded.get("players") or []:
        land = (p.get("land") or [None])[0]
        if land is None:
            continue
        t, x, z, y = (np.asarray(p.get(k) or [], float) for k in ("t", "x", "z", "y"))
        if not len(t):
            continue
        m = (t >= land) & (y < GROUND_MAX) & (y > -50)
        if not m.any():
            continue
        u, v = uv_of(cal, x[m], z[m])
        us.append(u)
        vs.append(v)
        hs.append(y[m])
    if not us:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    return np.concatenate(us), np.concatenate(vs), np.concatenate(hs)


def build_terrain(samples, n=TERRAIN_N):
    """{"n", "h": heights row by row (v down, u across), "coverage": the
    share of cells measured} from [(u, v, h), ...] of one map."""
    u = np.concatenate([s[0] for s in samples]) if samples else np.zeros(0)
    v = np.concatenate([s[1] for s in samples]) if samples else np.zeros(0)
    h = np.concatenate([s[2] for s in samples]) if samples else np.zeros(0)
    ok = (u >= 0) & (u < 1) & (v >= 0) & (v < 1)
    u, v, h = u[ok], v[ok], h[ok]
    H = np.full((n, n), np.nan)
    if len(h):
        cell = (np.floor(v * n).astype(int) * n + np.floor(u * n).astype(int))
        order = np.argsort(cell, kind="stable")
        cell, hs = cell[order], h[order]
        bounds = np.flatnonzero(np.diff(cell)) + 1
        for c, part in zip(np.split(cell, bounds), np.split(hs, bounds)):
            if len(part) >= TERRAIN_MIN_SAMPLES:
                H.flat[c[0]] = np.percentile(part, TERRAIN_PERCENTILE)
    measured = ~np.isnan(H)
    coverage = float(measured.mean())
    # sea level: the lowest ground anyone walks
    base = float(np.nanpercentile(H, 5)) if measured.any() else 0.0
    # The gaps, from their neighbours outward, ring by ring -- each ring a
    # step nearer sea level, so the sea and the cliffs nobody climbs settle
    # flat instead of carrying the nearest hilltop out over the water.
    for _ in range(n):
        gap = np.isnan(H)
        if not gap.any():
            break
        P = np.pad(H, 1, constant_values=np.nan)
        stack = np.stack([P[1 + dy:n + 1 + dy, 1 + dx:n + 1 + dx]
                          for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx])
        cnt = (~np.isnan(stack)).sum(0)
        mean = np.where(cnt > 0, np.nansum(stack, 0) / np.maximum(cnt, 1), np.nan)
        grow = gap & (cnt > 0)
        if not grow.any():
            break
        H[grow] = base + (mean[grow] - base) * TERRAIN_DECAY
    H[np.isnan(H)] = base
    # Smoothed: a 3x3 mean, twice.
    for _ in range(2):
        P = np.pad(H, 1, mode="edge")
        H = sum(P[1 + dy:n + 1 + dy, 1 + dx:n + 1 + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)) / 9.0
    return {"n": n, "h": np.round(H, 1).ravel().tolist(), "coverage": round(coverage, 3)}


def terrain_for(map_id, cal, decodes):
    """One map's terrain: from its file when it already covers these
    matches, else built from them and saved. decodes: {stem: decoded} --
    every decoded match of this map there is."""
    TERRAIN_DIR.mkdir(exist_ok=True)
    path = TERRAIN_DIR / ("%s.json" % map_id)
    stems = sorted(decodes)
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        if held.get("version") == TERRAIN_VERSION and held.get("stems") == stems:
            return held
    except Exception:
        pass
    built = build_terrain([ground_samples(d, cal) for d in decodes.values()])
    built.update(version=TERRAIN_VERSION, mapId=map_id, stems=stems, matches=len(stems))
    path.write_text(json.dumps(built), encoding="utf-8")
    return built


def _track(p, cal, step=STEP):
    """One player's track for the page: every `step` seconds, t (s), u v
    (fractions x10000) and h (height, decimetres)."""
    ts, xs, zs, ys = p.get("t") or [], p.get("x") or [], p.get("z") or [], p.get("y") or []
    out = {"t": [], "u": [], "v": [], "h": []}
    last_t = None
    n = len(ts)
    for k in range(n):
        x, z, y, t = xs[k], zs[k], ys[k], ts[k]
        if abs(x) < 1 and abs(z) < 1:
            continue                                  # the origin: not placed yet
        if last_t is not None and k < n - 1 and t < last_t + step - 0.05:
            continue
        u, v = uv_of(cal, x, z)
        out["t"].append(int(round(t)))
        out["u"].append(int(round(float(u) * 10000)))
        out["v"].append(int(round(float(v) * 10000)))
        out["h"].append(int(round(y * 10)))
        last_t = t
    return out


def build_payload(view, decoded, events, cal, terrain, squad_names=None, image=None):
    """The 3D page's whole match, in one message:
      map      id, name, image, metres (its width)
      squads   squad, name, short, place, kills
      players  pid, squad, name, land (s), out (s, when eliminated), t/u/v/h
      kills    t, ks / vs (squads), kn / vn (names), at / from (fractions)
      zones    at, r (fractions), announced, closing (s)
      plane    from, to (fractions)
      terrain  n, h (metres, row by row), coverage"""
    squad_names = squad_names or {}
    deaths = {}
    for k in events.get("kills") or []:
        if k.get("victim"):
            deaths.setdefault(int(k["victim"]), k.get("t"))
    players = []
    for p in decoded.get("players") or []:
        tr = _track(p, cal)
        if not tr["t"]:
            continue
        players.append(dict(tr, pid=p.get("pid"), squad=p.get("squad"), name=p.get("name") or "",
                            land=(p.get("land") or [None])[0], out=deaths.get(int(p.get("pid") or 0))))
    # The view's kills carry the places; the replay's events the names.
    named = sorted(events.get("kills") or [], key=lambda k: k.get("t") or 0)
    kills = []
    for k in view.get("kills") or []:
        best = None
        for e in named:
            if abs((e.get("t") or 0) - (k.get("t") or 0)) <= 1.5 and \
                    (not k.get("ks") or not e.get("killer") or (e["killer"] >> 24) == k.get("ks")):
                best = e
                break
        kills.append({"t": k.get("t"), "ks": k.get("ks"), "vs": k.get("vs"), "at": k.get("at"),
                      "from": k.get("from"), "kn": (best or {}).get("killerName") or "",
                      "vn": (best or {}).get("victimName") or ""})
        if best:
            named.remove(best)
    squads = []
    for q in view.get("squads") or []:
        name, short = squad_names.get(q.get("squad"), (q.get("teamName") or "", ""))
        squads.append({"squad": q.get("squad"), "name": name or q.get("teamName") or "",
                       "short": short, "place": q.get("place"), "kills": q.get("kills")})
    return {
        "map": {"id": view.get("mapId"), "name": view.get("mapName") or cal.get("name"),
                "image": image or cal.get("image"), "metres": round(map_metres(cal), 1)},
        "matchId": view.get("matchId"), "playedAt": view.get("playedAt"),
        "duration": view.get("duration"),
        "squads": squads, "players": players, "kills": kills,
        "zones": [{"at": z.get("at"), "r": z.get("r"), "announced": z.get("announced"),
                   "closing": z.get("closing"), "stage": z.get("stage")} for z in view.get("zones") or []],
        "plane": view.get("plane"),
        "terrain": {"n": terrain.get("n"), "h": terrain.get("h"), "coverage": terrain.get("coverage"),
                    "matches": terrain.get("matches")} if terrain else None,
    }
