"""The replay .bin's time base, on a made-up stream: the frame counter is
found through stray look-alikes, its rate and start are fitted from the
deaths, and the deaths are put on the elimination's own time (the
TriggerPoint runs 3 s behind it).

Run: python ocr/freefire/test_bin_decode.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import bin_decode as bd

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def main():
    rng = random.Random(3)
    RATE, START = 30.3, 50.0                # ticks a second; match seconds at N = 0

    print("the frame counter, through look-alikes")
    vals, true_n = [], []
    for f in range(1, 3001):
        n = 6 * f
        vals += [0, 0, 0, n, 0]
        true_n.append(n)
        vals += [rng.randint(1, 9_000_000) for _ in range(rng.randint(15, 40))]
        if rng.random() < 0.05:              # a stray 0 0 0 N 0 elsewhere
            vals += [0, 0, 0, rng.randint(1, 1_999_999), 0, 7]
    pos, N = bd._ticks(np.array(vals, dtype=np.uint64))
    got = set(N.tolist())
    check("every header found", sum(n in got for n in true_n) >= 0.99 * len(true_n),
          "%d of %d" % (sum(n in got for n in true_n), len(true_n)))
    check("and never going down", bool(np.all(np.diff(N) >= 0)))

    print("the fit: rate and start from the deaths")
    # 40 players, each dying at a known tick: their records sit at the death
    # spot from then on. A few anchors are wrong (a spot passed earlier).
    slot, X, Z, n_at = [], [], [], []
    deaths, events = [], []
    for s in range(1, 41):
        nd = rng.randint(3000, 34000)
        x, z = rng.uniform(-500, 500), rng.uniform(-500, 500)
        for k in range(nd - 600, nd, 30):            # on the move
            slot.append(s); X.append(x + rng.uniform(5, 50)); Z.append(z + rng.uniform(5, 50)); n_at.append(k)
        for k in range(nd, nd + 75, 3):              # down, ~2.5 s
            slot.append(s); X.append(x); Z.append(z); n_at.append(k)
        t_elim = nd / RATE + START
        if s <= 4:
            t_elim += rng.uniform(20, 60)             # wrong anchors
        events.append({"Event": 3, "Time": t_elim, "SParam": "P%d" % s})
        deaths.append({"PlayerID": s, "TriggerPoint": t_elim + 3.0, "position": {"x": x, "z": z}})
    data = {"PlayerHighlightInfos": [{"DeadEvents": deaths}], "Events": events,
            "GameTotalTime": 34100 / RATE + START}
    names = {s: "P%d" % s for s in range(1, 41)}
    off = bd._trigger_offset(data, names)
    check("the trigger runs 3 s behind the elimination", abs(off - 3.0) < 0.01, "%.2f" % off)
    k, b, used, sd = bd._fit_ticks(np.array(n_at, float), np.array(slot), np.array(X), np.array(Z),
                                   data, 34100, off)
    check("rate recovered", abs(1 / k - RATE) < 0.1, "%.2f ticks/s" % (1 / k))
    check("start recovered", abs(b - START) < 1.0, "%.2f s" % b)
    check("the wrong anchors left out", used <= 37 and used >= 34, "%d used" % used)

    print("\n%d checks, %d failed" % (checks, len(failures)))
    sys.exit(1 if failures else 0)


main()
