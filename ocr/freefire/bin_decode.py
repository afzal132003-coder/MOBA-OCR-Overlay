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
  A FRAME COUNTER, heading each block of records:   0 0 0 <N> 0
    the game's tick, ~30.3 a second. The client stopped writing the clock
    above (every replay from 2026-10-08 12:33 on has none), and timing by
    the record count instead squeezed the end of a match by minutes -- so
    without a clock the counter is the time base: the longest run of it
    that never goes down (stray 0 0 0 N 0 elsewhere cannot break that),
    fitted on the deaths with its rate held to what a tick can be.

Measured on a 17-minute Solara match: ~72,000 records over 47 slots,
35 of 44 deaths within +-1.4 s of the fit. Read-only: nothing here writes
to the Replays folder."""
import bisect
import json
from pathlib import Path

import numpy as np

CLOCK_ID = 1002
# Bumped when the decode changes, so cached decodes are made again.
VERSION = 2
TICK_RATE = (28.0, 33.0)        # frame-counter ticks a second, the range believed


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


def _trigger_offset(data, names):
    """How far a death's TriggerPoint runs behind the elimination itself
    (Events, code 3: PlayerID eliminated SParam) -- 3.0 s on every replay
    measured. The fit is put on the elimination's own time: the time the
    kill feed, the zones and the circle table all run on, so a player
    leaves the map as their elimination comes up, not three seconds on."""
    at = {}
    for e in data.get("Events") or []:
        if e.get("Event") == 3 and e.get("SParam") is not None:
            at.setdefault(str(e["SParam"]), []).append(float(e.get("Time") or 0))
    gaps = []
    for pl in data.get("PlayerHighlightInfos") or []:
        for e in pl.get("DeadEvents") or []:
            pid = e.get("PlayerID")
            nm = (names or {}).get(pid) or (names or {}).get(str(pid))
            if nm is None or e.get("TriggerPoint") is None:
                continue
            # each death against the elimination just before it: a player
            # can have more than one (a knock, a revive)
            near = [float(e["TriggerPoint"]) - t for t in at.get(str(nm), [])
                    if 0 <= float(e["TriggerPoint"]) - t <= 15]
            if near:
                gaps.append(min(near))
    return float(np.median(gaps)) if gaps else 0.0


def _fit_time(clock_at, slot, X, Z, data, offset=0.0):
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
            anchors.append((float(clock_at[runs[-1][0]]), float(e["TriggerPoint"]) - offset))
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


def _ticks(vals):
    """(positions, N): the frame counter heading each block of records --
    0 0 0 N 0 -- as the longest chain of such values that never goes down."""
    v = vals
    p = np.nonzero((v[:-4] == 0) & (v[1:-3] == 0) & (v[2:-2] == 0) & (v[4:] == 0)
                   & (v[3:-1] > 0) & (v[3:-1] < 2_000_000))[0]
    N = v[p + 3].astype(np.int64)
    tails, tails_k, prev = [], [], np.full(len(N), -1, np.int64)
    for k in range(len(N)):
        n = int(N[k])
        j = bisect.bisect_right(tails, n)
        if j > 0:
            prev[k] = tails_k[j - 1]
        if j == len(tails):
            tails.append(n)
            tails_k.append(k)
        else:
            tails[j] = n
            tails_k[j] = k
    chain, k = [], (tails_k[-1] if tails_k else -1)
    while k >= 0:
        chain.append(k)
        k = int(prev[k])
    keep = np.array(chain[::-1], dtype=np.int64)
    if not len(keep):
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    return p[keep] + 3, N[keep]


def _fit_ticks(n_at, slot, X, Z, data, n_last, offset=0.0):
    """match time = k * N + b for the frame counter: the rate held to
    TICK_RATE, and of every pair of deaths' readings the line most of the
    others agree with (within 1.5 s) -- then least squares on those."""
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
            runs = np.split(h, np.nonzero(np.diff(n_at[h]) > 30)[0] + 1)
            anchors.append((float(n_at[runs[-1][0]]), float(e["TriggerPoint"]) - offset))
    # the end of the recording is the end of the match
    if n_last and data.get("GameTotalTime"):
        anchors.append((float(n_last), float(data["GameTotalTime"])))
    if len(anchors) < 3:
        k = 2 / sum(TICK_RATE)
        b = float(data.get("GameTotalTime") or 0) - k * float(n_last or 0)
        return k, b, len(anchors), None
    A = np.array(anchors)
    lo, hi = 1 / TICK_RATE[1], 1 / TICK_RATE[0]
    best, best_kb = -1, None
    for i in range(len(A)):
        dn = A[:, 0] - A[i, 0]
        ok = np.abs(dn) > 300
        if not ok.any():
            continue
        ks = (A[ok, 1] - A[i, 1]) / dn[ok]
        for k in ks[(ks >= lo) & (ks <= hi)]:
            b = A[i, 1] - k * A[i, 0]
            n_in = int((np.abs(A[:, 1] - (k * A[:, 0] + b)) < 1.5).sum())
            if n_in > best:
                best, best_kb = n_in, (float(k), float(b))
    if best_kb is None:
        k = 2 / sum(TICK_RATE)
        b = float(np.median(A[:, 1] - k * A[:, 0]))
    else:
        k, b = best_kb
        keep = np.abs(A[:, 1] - (k * A[:, 0] + b)) < 1.5
        if keep.sum() >= 3:
            k2, b2 = np.polyfit(A[keep, 0], A[keep, 1], 1)
            if lo <= k2 <= hi:
                k, b = float(k2), float(b2)
    r = A[:, 1] - (k * A[:, 0] + b)
    keep = np.abs(r) < 1.5
    return float(k), float(b), int(keep.sum()), float(r[keep].std()) if keep.any() else None


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
    # Every time base the file has, fitted; the tightest kept. Where both
    # are there the counter usually wins (+-0.7 s against the clock's
    # +-1.9 s on the same replays).
    offset = _trigger_offset(data, names)
    fits = []
    if len(clk):
        k_ = np.searchsorted(clk, idx) - 1
        at = np.where(k_ >= 0, fl[clk][np.clip(k_, 0, None)], 0.0).astype(float)
        fits.append(("clock", at) + _fit_time(at, slot, X, Z, data, offset))
    pos, N = _ticks(vals)
    if len(N) >= 100 and N[-1] > 1000:
        j = np.searchsorted(pos, idx) - 1
        at = np.where(j >= 0, N[np.clip(j, 0, None)], 0).astype(float)
        fits.append(("ticks", at) + _fit_ticks(at, slot, X, Z, data, int(N[-1]), offset))
    if not fits:
        # neither: the record count, the roughest of guides
        at = np.arange(len(idx), dtype=float) * 0.05
        fits.append(("records", at) + _fit_time(at, slot, X, Z, data, offset))
    good = [f for f in fits if f[5] is not None and f[4] >= 3]
    base, clock_at, k, b, used, sd = min(good, key=lambda f: f[5]) if good else fits[0]
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
        "version": VERSION,
        "records": int(len(idx)),
        "fit": {"k": round(k, 6), "b": round(b, 2), "anchors": used, "base": base,
                "offset": round(offset, 2),
                "sd": round(sd, 2) if sd is not None else None},
        "players": players,
    }
