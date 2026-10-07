"""THE REPLAY .bin -- every player's movement through a match, decoded.

Free Fire writes a ReplayInfo_<match>_<stamp>.bin beside each replay's
JSON. Its body is one stream of varints (7 bits a byte, high bit = more):
integers zigzag-coded, floats stored as their raw 32 bits. Two kinds of
message are read here; everything else is skipped.

  A PLAYER RECORD, 19 values:
      0 0 slot | x y z | fx fy fz | a b | vx vy vz | state 0 | q q q
    slot   the player, = PlayerID & 0xFFFFFF (the squad is PlayerID >> 24)
    x y z  position in MILLIMETRES (the JSON's death positions, x1000,
           match these exactly)
    f..    facing x100, then a second unit vector x100
    v..    velocity in mm/s (vy -550 while parachuting)
    q q q  three floats (rotation)
  A CLOCK, every tick:   0 1002 <float>
    rises 0.5 a tick, about three ticks a second. Its scale against match
    time is fitted on the deaths in the JSON (each death's position is in
    the stream at the moment it happened), robustly, so a death matched to
    a spot the player had merely passed earlier does not drag the fit.

Measured on a 17-minute Solara match: ~72,000 records over 47 slots,
35 of 44 deaths within +-1.4 s of the fit. Read-only: nothing here writes
to the Replays folder."""
import json
from pathlib import Path

import numpy as np

CLOCK_ID = 1002


def _varints(raw):
    arr = np.frombuffer(raw, np.uint8)
    ends = np.nonzero(arr < 0x80)[0]
    starts = np.concatenate([[0], ends[:-1] + 1])
    lens = ends - starts + 1
    vals = np.zeros(len(starts), np.uint64)
    for L in range(1, int(lens.max()) + 1 if len(lens) else 1):
        sel = np.nonzero(lens == L)[0]
        if not len(sel):
            continue
        v = np.zeros(len(sel), np.uint64)
        for k in range(L - 1, -1, -1):
            v = (v << np.uint64(7)) | (arr[starts[sel] + k] & 0x7F).astype(np.uint64)
        vals[sel] = v
    return vals


def _signed(vals):
    return (vals >> np.uint64(1)).astype(np.int64) ^ -(vals & np.uint64(1)).astype(np.int64)


def _floats(vals):
    small = vals < np.uint64(2 ** 32)
    out = np.zeros(len(vals), np.float32)
    out[small] = vals[small].astype(np.uint32).view(np.float32)
    return out, small


def _records(vals, sg):
    n = len(vals)
    i = np.arange(6, max(6, n - 16))
    isf = lambda a: (a > 900_000_000) & (a < 4_300_000_000)
    m = ((np.abs(sg[i]) < 1_300_000) & (np.abs(sg[i + 2]) < 1_300_000)
         & (sg[i + 1] > -100_000) & (sg[i + 1] < 500_000)
         & (vals[i - 3] == 0) & (vals[i - 2] == 0)
         & isf(vals[i + 13]) & isf(vals[i + 14]) & isf(vals[i + 15]))
    return i[m]


def _fit_time(clock_at, slot, X, Z, data):
    """match time = k * clock + b, from the deaths: the first moment of a
    player's last stay at the spot the JSON says they died."""
    anchors = []
    for pl in data.get("PlayerHighlightInfos") or []:
        for e in pl.get("DeadEvents") or []:
            p = e.get("position") or {}
            if not p or e.get("TriggerPoint") is None:
                continue
            s = int(e.get("PlayerID") or 0) & 0xFFFFFF
            h = np.nonzero((slot == s) & (np.abs(X - p["x"]) < 1.0) & (np.abs(Z - p["z"]) < 1.0))[0]
            if not len(h):
                continue
            runs = np.split(h, np.nonzero(np.diff(clock_at[h]) > 3)[0] + 1)
            anchors.append((float(clock_at[runs[-1][0]]), float(e["TriggerPoint"])))
    if len(anchors) < 3:
        return 1 / 3.0, 0.0, len(anchors), None
    A = np.array(anchors)
    keep = np.ones(len(A), bool)
    k, b = 1 / 3.0, 0.0
    for _ in range(5):
        k, b = np.polyfit(A[keep, 0], A[keep, 1], 1)
        r = A[:, 1] - (k * A[:, 0] + b)
        keep = np.abs(r) < max(3.0, 2 * r[keep].std())
        if keep.sum() < 3:
            keep = np.ones(len(A), bool)
            break
    r = A[:, 1] - (k * A[:, 0] + b)
    return float(k), float(b), int(keep.sum()), float(r[keep].std())


def decode(bin_path, names=None, data=None, step=1.0):
    """{players: [...], fit: {...}} for one replay .bin.

    names: {PlayerID: name} (the replay JSON's player list); data: the
    replay JSON itself, for the deaths the clock is fitted on. Each
    player's track is sampled every `step` seconds: t, x, z, y in metres."""
    bin_path = Path(bin_path)
    if data is None:
        data = json.loads(bin_path.with_suffix(".json").read_text(encoding="utf-8-sig"))
    raw = bin_path.read_bytes()
    vals = _varints(raw)
    sg = _signed(vals)
    fl, small = _floats(vals)

    clk = np.nonzero(small & (vals > 1_000_000_000) & (np.roll(vals, 1) == CLOCK_ID)
                     & (np.roll(vals, 2) == 0))[0]
    idx = _records(vals, sg)
    slot = vals[idx - 1].astype(np.int64)
    X, Y, Z = sg[idx] / 1000.0, sg[idx + 1] / 1000.0, sg[idx + 2] / 1000.0
    VY = sg[idx + 9] / 1000.0
    if len(clk):
        k_ = np.searchsorted(clk, idx) - 1
        clock_at = np.where(k_ >= 0, fl[clk][np.clip(k_, 0, None)], 0.0).astype(float)
    else:
        clock_at = np.arange(len(idx), dtype=float) * 0.05
    k, b, used, sd = _fit_time(clock_at, slot, X, Z, data)
    T = k * clock_at + b

    pids = {}
    for pid in (names or {}):
        try:
            pids[int(pid) & 0xFFFFFF] = int(pid)
        except (TypeError, ValueError):
            pass
    players = []
    for s in sorted(set(slot.tolist())):
        pid = pids.get(s)
        if pid is None:
            continue
        sel = np.nonzero(slot == s)[0]
        if len(sel) < 3:
            continue
        t, x, z, y, vy = T[sel], X[sel], Z[sel], Y[sel], VY[sel]
        # one sample per step: the last record in each bucket
        bucket = np.floor(t / step).astype(np.int64)
        last = np.nonzero(np.diff(np.append(bucket, bucket[-1] + 1)) != 0)[0]
        t2, x2, z2, y2 = t[last], x[last], z[last], y[last]
        # landing: the first record on the ground after being airborne
        airborne = np.nonzero(y > 150)[0]
        ground = np.nonzero((y < 150) & (np.abs(vy) < 0.3))[0]
        if len(airborne):
            ground = ground[ground > airborne[0]]
        land = None
        if len(ground):
            g = ground[0]
            land = [round(float(t[g]), 1), round(float(x[g]), 1), round(float(z[g]), 1)]
        # on the ground: distance, top speed, time faster than running
        on = t2 >= (land[0] if land else t2[0])
        dx = np.hypot(np.diff(x2[on]), np.diff(z2[on]))
        dt = np.maximum(np.diff(t2[on]), 1e-6)
        sp = dx / dt
        ok = sp < 60                 # a jump in the record, not travel
        players.append({
            "slot": s, "pid": pid, "squad": pid >> 24,
            "name": (names or {}).get(pid) or (names or {}).get(str(pid)) or "",
            "t": np.round(t2, 1).tolist(), "x": np.round(x2, 1).tolist(),
            "z": np.round(z2, 1).tolist(), "y": np.round(y2, 1).tolist(),
            "land": land,
            "distance": round(float(dx[ok].sum()), 0),
            "vehicleSeconds": round(float(dt[ok & (sp > 9)].sum()), 0),
            "topSpeed": round(float(sp[ok].max()) if ok.any() else 0.0, 1),
        })
    return {
        "records": int(len(idx)),
        "fit": {"k": round(k, 5), "b": round(b, 2), "anchors": used,
                "sd": round(sd, 2) if sd is not None else None},
        "players": players,
    }
